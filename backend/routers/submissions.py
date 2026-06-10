"""Public submission flow + admin review."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pymongo.errors import DuplicateKeyError
import cloudinary
from core import (
    APP_NAME,
    DEFAULT_FIELD_VISIBILITY,
    LEGACY_TAKE_CATEGORIES,
    MAX_SUBMISSION_IMAGES,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_TAKES,
    MAX_SUBMISSION_VIDEO_BYTES,
    MAX_IMAGES_PER_CATEGORY,
    PORTFOLIO_IMAGE_CATEGORIES,
    SUBMISSION_DECISIONS,
    SUBMISSION_UPLOAD_CATEGORIES,
    AdminSubmissionEditIn,
    SubmissionDecisionIn,
    SubmissionStartIn,
    SubmissionUpdateIn,
    _now,
    _paginate_params,
    _paginated,
    _public_project,
    _resolve_cover_url,
    _submission_to_client_shape,
    cloudinary_upload,
    compute_age,
    compute_effective_age,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_access_token,
    make_token,
    remove_synced_media_from_global_talent,
    sync_media_to_global_talent,
    media_url,
    video_poster_url,
    update_talent_cover_cache,
)
from drive_backup import (
    drive_enabled,
    enqueue_drive_upload,
    submission_folder_url,
)
from notifications import fanout as notify_fanout

router = APIRouter(prefix="/api", tags=["submissions"])


def deduplicate_media(media_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_public_ids = set()
    seen_urls = set()
    deduped = []
    for m in media_list:
        pub_id = m.get("public_id")
        url = m.get("url")
        if pub_id:
            if pub_id in seen_public_ids:
                continue
            seen_public_ids.add(pub_id)
            if url:
                seen_urls.add(url)
        elif url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        deduped.append(m)
    return deduped


# --------------------------------------------------------------------------
# Public (talent-facing) flow
# --------------------------------------------------------------------------
@router.get("/public/projects/{slug}")
async def public_project(slug: str):
    project = await db.projects.find_one({"slug": slug}, {"_id": 0, "created_by": 0, "client_budget": 0})
    if not project:
        raise HTTPException(404, "Project not found")
    # Gate budget visibility: if admin has toggled "Hide Budget From Talent",
    # strip budget_per_day and talent_budget from the public payload.
    if project.get("hide_budget_from_talent"):
        project.pop("budget_per_day", None)
        project.pop("talent_budget", None)
    return project


@router.get("/public/prefill")
async def prefill_for_email(email: str, request: Request):
    """Public lookup: if the email matches an approved talent, return safe fields
    so the audition form can pre-fill. Returns 204-style empty dict if not found.

    Phase 2 unified schema: returns the full set of non-sensitive identity
    fields. Media / source / notes / created_by are NEVER included.

    Phase 0: rate-limited to **20 lookups per minute per IP** to mitigate
    email-probing. The limiter is a sliding-window in-memory counter — fine
    for our scale; replace with Redis once we run multi-replica.
    """
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many lookups — please slow down")
    email = (email or "").strip().lower()
    if "@" not in email:
        return {}
    talent = await db.talents.find_one(
        {"$or": [{"email": email}, {"source.talent_email": email}]},
        # Strict allowlist projection. Storage paths / source / notes never
        # leave this endpoint. `media` IS included so the prefill confirmation
        # card can show a "Is this you?" thumbnail derived via _resolve_cover_url.
        {
            "_id": 0, "name": 1, "age": 1, "dob": 1, "height": 1,
            "phone": 1, "location": 1, "ethnicity": 1, "gender": 1, "bio": 1,
            "instagram_handle": 1, "instagram_followers": 1, "work_links": 1,
            "media": 1, "cover_media_id": 1,
        },
    )
    if not talent:
        return {}
    name = talent.get("name") or ""
    parts = name.split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""

    # Image prefill: fetch existing image, indian, western look images from master profile
    prefill_images = []
    for m in (talent.get("media") or []):
        if m.get("category") in {"image", "indian", "western"} and m.get("url"):
            prefill_images.append({
                "id": m.get("id"),
                "category": m.get("category"),
                "url": m.get("url"),
                "public_id": m.get("public_id"),
                "resource_type": m.get("resource_type") or "image",
                "content_type": m.get("content_type") or "image/jpeg",
                "original_filename": m.get("original_filename"),
                "size": m.get("size") or 0,
                "created_at": m.get("created_at") or _now(),
            })

    # Intro video prefill: priority 1: db.talents.media
    latest_intro = None
    for m in (talent.get("media") or []):
        if m.get("category") in {"video", "intro_video"} and m.get("url"):
            latest_intro = {
                "id": m.get("id"),
                "category": "intro_video",
                "url": m.get("url"),
                "public_id": m.get("public_id"),
                "resource_type": m.get("resource_type") or "video",
                "content_type": m.get("content_type") or "video/mp4",
                "original_filename": m.get("original_filename"),
                "size": m.get("size") or 0,
                "created_at": m.get("created_at") or _now(),
            }
            break

    # Priority 2: db.submissions
    if not latest_intro:
        latest_sub = await db.submissions.find_one(
            {
                "talent_email": email,
                "media.category": {"$in": ["intro_video", "video"]}
            },
            sort=[("submitted_at", -1), ("created_at", -1)]
        )
        if latest_sub:
            for m in (latest_sub.get("media") or []):
                if m.get("category") in {"intro_video", "video"} and m.get("url"):
                    latest_intro = {
                        "id": m.get("id"),
                        "category": "intro_video",
                        "url": m.get("url"),
                        "public_id": m.get("public_id"),
                        "resource_type": m.get("resource_type") or "video",
                        "content_type": m.get("content_type") or "video/mp4",
                        "original_filename": m.get("original_filename"),
                        "size": m.get("size") or 0,
                        "created_at": m.get("created_at") or _now(),
                    }
                    break

    if not latest_intro:
        latest_app = await db.applications.find_one(
            {
                "talent_email": email,
                "media.category": {"$in": ["intro_video", "video"]}
            },
            sort=[("created_at", -1)]
        )
        if latest_app:
            for m in (latest_app.get("media") or []):
                if m.get("category") in {"intro_video", "video"} and m.get("url"):
                    latest_intro = {
                        "id": m.get("id"),
                        "category": "intro_video",
                        "url": m.get("url"),
                        "public_id": m.get("public_id"),
                        "resource_type": m.get("resource_type") or "video",
                        "content_type": m.get("content_type") or "video/mp4",
                        "original_filename": m.get("original_filename"),
                        "size": m.get("size") or 0,
                        "created_at": m.get("created_at") or _now(),
                    }
                    break

    return {
        "first_name": first,
        "last_name": last,
        "age": talent.get("age") if talent.get("age") is not None else (compute_age(talent.get("dob")) if talent.get("dob") else None),
        "dob": talent.get("dob"),
        "phone": talent.get("phone"),
        "height": talent.get("height"),
        "location": talent.get("location"),
        "ethnicity": talent.get("ethnicity"),
        "gender": talent.get("gender"),
        "bio": talent.get("bio"),
        "instagram_handle": talent.get("instagram_handle"),
        "instagram_followers": talent.get("instagram_followers"),
        "work_links": talent.get("work_links") or [],
        # Cloudinary cover thumbnail for the confirmation prompt. Resolved
        # via _resolve_cover_url so it picks the cover_media_id, falling
        # back to the first portfolio/indian/western image. None when the
        # talent has no usable image — the card hides the thumb gracefully.
        "image_url": _resolve_cover_url(talent),
        "prefill_media": deduplicate_media(prefill_images + ([latest_intro] if latest_intro else [])),
    }



# Sliding-window rate limiter for the prefill endpoint. 20 reqs / 60 s / IP.
_PREFILL_BUCKET: Dict[str, list] = {}
_PREFILL_LIMIT = 20
_PREFILL_WINDOW = 60.0


def _prefill_rate_limit_ok(request: Request) -> bool:
    import time
    now = time.monotonic()
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    bucket = _PREFILL_BUCKET.setdefault(ip, [])
    # Drop expired
    cutoff = now - _PREFILL_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _PREFILL_LIMIT:
        return False
    bucket.append(now)
    return True


@router.post("/public/projects/{slug}/submission")
async def start_submission(slug: str, payload: SubmissionStartIn):
    """Start OR resume a submission.

    If a submission already exists for (project, email), returns a fresh token
    that unlocks edits — this is the retest / re-upload entry point. The
    decision is NOT reset here; only `finalize` flips it back to pending.
    """
    project = await db.projects.find_one({"slug": slug})
    if not project:
        raise HTTPException(404, "Project not found")
    email = payload.email.lower().strip()

    existing = await db.submissions.find_one({
        "project_id": project["id"],
        "talent_email": email,
    })
    if existing:
        sid = existing["id"]
        # Reuse the existing persistent access_token, or mint one if legacy
        # records pre-date this feature.
        atk = existing.get("access_token")
        if not atk:
            atk = make_access_token()
            await db.submissions.update_one({"id": sid}, {"$set": {"access_token": atk}})
        token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
        return {
            "id": sid,
            "token": token,
            "access_token": atk,
            "resumed": True,
            "status": existing.get("status", "draft"),
        }

    fd = payload.form_data or {}
    talent_age = None
    prefill_media = []
    if email:
        talent_doc = await db.talents.find_one(
            {"$or": [{"email": email}, {"source.talent_email": email}]},
            {"age": 1, "dob": 1, "media": 1}
        )
        if talent_doc:
            talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)
            # Image prefill: fetch existing image, indian, western look images from master profile
            for m in (talent_doc.get("media") or []):
                if m.get("category") in {"image", "indian", "western"} and m.get("url"):
                    prefill_media.append({
                        "id": m.get("id"),
                        "category": m.get("category"),
                        "url": m.get("url"),
                        "public_id": m.get("public_id"),
                        "resource_type": m.get("resource_type") or "image",
                        "content_type": m.get("content_type") or "image/jpeg",
                        "original_filename": m.get("original_filename"),
                        "size": m.get("size") or 0,
                        "created_at": m.get("created_at") or _now(),
                    })
            
            # Intro video prefill: priority 1: db.talents.media
            latest_intro = None
            for m in (talent_doc.get("media") or []):
                if m.get("category") in {"video", "intro_video"} and m.get("url"):
                    latest_intro = {
                        "id": m.get("id"),
                        "category": "intro_video",
                        "url": m.get("url"),
                        "public_id": m.get("public_id"),
                        "resource_type": m.get("resource_type") or "video",
                        "content_type": m.get("content_type") or "video/mp4",
                        "original_filename": m.get("original_filename"),
                        "size": m.get("size") or 0,
                        "created_at": m.get("created_at") or _now(),
                    }
                    break

            # Priority 2: db.submissions
            if not latest_intro:
                latest_sub = await db.submissions.find_one(
                    {
                        "talent_email": email,
                        "media.category": {"$in": ["intro_video", "video"]}
                    },
                    sort=[("submitted_at", -1), ("created_at", -1)]
                )
                if latest_sub:
                    for m in (latest_sub.get("media") or []):
                        if m.get("category") in {"intro_video", "video"} and m.get("url"):
                            latest_intro = {
                                "id": m.get("id"),
                                "category": "intro_video",
                                "url": m.get("url"),
                                "public_id": m.get("public_id"),
                                "resource_type": m.get("resource_type") or "video",
                                "content_type": m.get("content_type") or "video/mp4",
                                "original_filename": m.get("original_filename"),
                                "size": m.get("size") or 0,
                                "created_at": m.get("created_at") or _now(),
                            }
                            break
            
            if not latest_intro:
                latest_app = await db.applications.find_one(
                    {
                        "talent_email": email,
                        "media.category": {"$in": ["intro_video", "video"]}
                    },
                    sort=[("created_at", -1)]
                )
                if latest_app:
                    for m in (latest_app.get("media") or []):
                        if m.get("category") in {"intro_video", "video"} and m.get("url"):
                            latest_intro = {
                                "id": m.get("id"),
                                "category": "intro_video",
                                "url": m.get("url"),
                                "public_id": m.get("public_id"),
                                "resource_type": m.get("resource_type") or "video",
                                "content_type": m.get("content_type") or "video/mp4",
                                "original_filename": m.get("original_filename"),
                                "size": m.get("size") or 0,
                                "created_at": m.get("created_at") or _now(),
                            }
                            break
            
            if latest_intro:
                prefill_media.append(latest_intro)

    submitted_age_override_val = None
    override_active = fd.get("overrideAge") or fd.get("override_age")
    if override_active and fd.get("submitted_age_override") not in (None, ""):
        try:
            submitted_age_override_val = int(fd["submitted_age_override"])
        except Exception:
            pass

    effective_age_val = compute_effective_age(fd, talent_age)

    sid = str(uuid.uuid4())
    atk = make_access_token()
    doc = {
        "id": sid,
        "project_id": project["id"],
        "project_slug": slug,
        "talent_name": payload.name,
        "talent_email": email,
        "talent_phone": payload.phone,
        "form_data": fd,
        "field_visibility": {**DEFAULT_FIELD_VISIBILITY},
        "submitted_age_override": submitted_age_override_val,
        "effective_age": effective_age_val,
        "media": deduplicate_media(prefill_media),
        "status": "draft",
        "decision": "pending",
        "access_token": atk,
        "created_at": _now(),
        "submitted_at": None,
    }
    try:
        await db.submissions.insert_one(doc)
    except DuplicateKeyError:
        # Race: parallel start hit the unique (project_id, talent_email)
        # index. Fall through to the existing-submission resume path.
        existing = await db.submissions.find_one({
            "project_id": project["id"],
            "talent_email": email,
        })
        if existing:
            sid = existing["id"]
            atk = existing.get("access_token")
            if not atk:
                atk = make_access_token()
                await db.submissions.update_one({"id": sid}, {"$set": {"access_token": atk}})
            token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
            return {
                "id": sid,
                "token": token,
                "access_token": atk,
                "resumed": True,
                "status": existing.get("status", "draft"),
            }
        raise HTTPException(409, "Submission already exists for this email")
    token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
    return {"id": sid, "token": token, "access_token": atk, "resumed": False, "status": "draft"}


@router.put("/public/submissions/{sid}")
async def submission_update(
    sid: str,
    payload: SubmissionUpdateIn,
    authorization: Optional[str] = Header(None),
):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        merged_fd = {**(sub.get("form_data") or {}), **payload.form_data}
        update["form_data"] = merged_fd
        fn = payload.form_data.get("first_name") or merged_fd.get("first_name")
        ln = payload.form_data.get("last_name") or merged_fd.get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")

        talent_age = None
        email = sub.get("talent_email")
        if email:
            talent_doc = await db.talents.find_one({"$or": [{"email": email}, {"source.talent_email": email}]}, {"age": 1, "dob": 1})
            if talent_doc:
                talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)

        submitted_age_override_val = None
        override_active = merged_fd.get("overrideAge") or merged_fd.get("override_age")
        if override_active and merged_fd.get("submitted_age_override") not in (None, ""):
            try:
                submitted_age_override_val = int(merged_fd["submitted_age_override"])
            except Exception:
                pass

        update["submitted_age_override"] = submitted_age_override_val
        update["effective_age"] = compute_effective_age(merged_fd, talent_age)
    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@router.post("/public/submissions/{sid}/upload")
async def submission_upload(
    request: Request,
    sid: str,
    category: str = Form(...),
    label: Optional[str] = Form(None),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    if category not in SUBMISSION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    ct = (file.content_type or "").lower()
    fn = (file.filename or "").lower()
    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}

    # Validation of content type / format (P5)
    if is_video_slot:
        if not (ct.startswith("video/") or fn.endswith((".mp4", ".mov", ".avi", ".webm", ".mkv", ".3gp"))):
            raise HTTPException(400, "Unsupported video format. Please upload MP4, MOV, or WEBM.")
    else:
        # Image categories
        if ct in {"image/bmp", "image/tiff", "image/heic", "image/heif"} or fn.endswith((".bmp", ".tiff", ".heic", ".heif")):
            raise HTTPException(400, "HEIC, BMP, and TIFF formats are not supported. Please upload JPEG or PNG.")
        if not (ct.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp"))):
            raise HTTPException(400, "Unsupported image format. Please upload JPG, PNG, or WEBP.")

    if category in PORTFOLIO_IMAGE_CATEGORIES:
        # Phase 3: per-category cap (10 each) — NOT a combined total.
        existing = sum(
            1 for m in sub.get("media", []) if m.get("category") == category
        )
        if existing >= MAX_IMAGES_PER_CATEGORY:
            label_name = {"image": "Portfolio", "indian": "Indian look", "western": "Western look"}.get(category, category)
            raise HTTPException(400, f"{label_name} image limit reached ({MAX_IMAGES_PER_CATEGORY})")

    if category == "take":
        existing_takes = sum(
            1
            for m in sub.get("media", [])
            if m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        )
        if existing_takes >= MAX_SUBMISSION_TAKES:
            raise HTTPException(
                400,
                f"Maximum {MAX_SUBMISSION_TAKES} takes reached — delete one to add another",
            )

    # Single-slot replacement: intro video + legacy fixed takes
    single_slot = {"intro_video", "take_1", "take_2", "take_3"}
    if category in single_slot:
        await db.submissions.update_one(
            {"id": sid}, {"$pull": {"media": {"category": category}}}
        )

    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/submissions/{sid}"

    # P2-E — Reject oversized uploads BEFORE reading the body into RAM.
    raw_cl = request.headers.get("content-length")
    if raw_cl is not None:
        try:
            declared_bytes = int(raw_cl)
        except ValueError:
            declared_bytes = 0
        if is_video_slot and declared_bytes > MAX_SUBMISSION_VIDEO_BYTES:
            cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
            raise HTTPException(
                413,
                f"Video is too large. Max {cap_mb} MB — please compress and retry.",
            )
        if category in PORTFOLIO_IMAGE_CATEGORIES and declared_bytes > MAX_SUBMISSION_IMAGE_BYTES:
            cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
            raise HTTPException(
                413, f"Image is too large. Max {cap_mb} MB per image."
            )

    data = await file.read()

    # Secondary size check against the actual bytes read
    size_bytes = len(data)
    if is_video_slot and size_bytes > MAX_SUBMISSION_VIDEO_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
        raise HTTPException(
            400,
            f"Video is too large ({mb} MB). Max {cap_mb} MB — please compress and retry.",
        )
    if category in PORTFOLIO_IMAGE_CATEGORIES and size_bytes > MAX_SUBMISSION_IMAGE_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(
            400, f"Image is too large ({mb} MB). Max {cap_mb} MB per image."
        )

    # v37m — direct Cloudinary upload.
    rt = "video" if is_video_slot else "image"
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=media_id,
        resource_type=rt,
        content_type=file.content_type,
        keep_original=False,
    )
    is_video = rt == "video"
    is_image = rt == "image"
    media = {
        "id": media_id,
        "category": category,
        "url": result["url"],
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("bytes") or size_bytes,
        "created_at": _now(),
        "scope": "submission",
        "submission_id": sid,
        "project_id": sub["project_id"],
        "duration": result.get("duration"),
        "thumbnail_url": media_url(result["public_id"], preset="thumb", resource_type=result["resource_type"]) if is_image else None,
        "poster_url": video_poster_url(result["public_id"]) if is_video else None,
    }
    if category == "take":
        media["label"] = (label or "").strip() or f"Take {existing_takes + 1}"

    patch: Dict[str, Any] = {"$push": {"media": media}}
    # Re-upload after finalize flips status back to "updated" and decision → pending
    was_finalized = sub.get("status") in ("submitted", "updated")
    re_approval = True
    if was_finalized:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1, "brand_name": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        set_patch = {
            "status": "updated",
            "updated_at": _now(),
        }
        if re_approval:
            set_patch["decision"] = "pending"
        patch["$set"] = set_patch
    await db.submissions.update_one({"id": sid}, patch)
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})

    # Notify admins on retake — but only when the submission was already
    # finalized (uploads during the initial flow are too noisy).
    if was_finalized:
        project = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
        )
        brand = (project or {}).get("brand_name") or "Project"
        talent_name = sub.get("talent_name") or sub.get("talent_email") or "A talent"
        cat_label = (
            "intro video" if category == "intro_video"
            else "audition take" if category == "take"
            else "image"
        )
        await notify_fanout(
            db,
            type="submission_retake",
            title=f"{talent_name} uploaded a new {cat_label}",
            body=(f"{brand} — submission moved back to pending."
                  if re_approval else f"{brand} — added to existing decision."),
            payload={"submission_id": sid, "project_id": sub["project_id"], "category": category},
        )

    # ------------------------------------------------------------------
    # Secondary backup → Google Drive (best-effort, non-blocking).
    # Spawns a detached asyncio task; failures are logged and queued for
    # retry. NEVER affects the primary upload result returned above.
    # ------------------------------------------------------------------
    if drive_enabled():
        project = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
        )
        brand = (project or {}).get("brand_name") or sub.get("project_slug") or "Unknown"
        enqueue_drive_upload(db, media, updated, brand, data)

    # ------------------------------------------------------------------
    # Phase 3 v37i — mirror image-category media into the global talent
    # record so the talent's profile (/admin/talents/:id) reflects every
    # portfolio image they've uploaded across all projects. Idempotent
    # via source_submission_media_id; no-op for intro_video/take.
    # ------------------------------------------------------------------
    await sync_media_to_global_talent(updated, media)

    return updated


@router.patch("/public/submissions/{sid}/media/{mid}")
async def submission_update_media(
    sid: str,
    mid: str,
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(None),
):
    """Patch a take's label. Only `take` media supports this today."""
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    target = next((m for m in (sub.get("media") or []) if m.get("id") == mid), None)
    if not target:
        raise HTTPException(404, "Media not found")
    if target.get("category") != "take":
        raise HTTPException(400, "Only renamable takes can be patched")
    new_label = (payload.get("label") or "").strip()
    if not new_label:
        raise HTTPException(400, "Label cannot be empty")
    await db.submissions.update_one(
        {"id": sid, "media.id": mid},
        {"$set": {"media.$.label": new_label}},
    )
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@router.delete("/public/submissions/{sid}/media/{mid}")
async def submission_delete_media(
    sid: str, mid: str, authorization: Optional[str] = Header(None)
):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    patch: Dict[str, Any] = {"$pull": {"media": {"id": mid}}}
    if sub.get("status") in ("submitted", "updated"):
        patch["$set"] = {
            "status": "updated",
            "decision": "pending",
            "updated_at": _now(),
        }
    await db.submissions.update_one({"id": sid}, patch)
    # Phase 3 v37i — keep the global talent profile in sync.
    await remove_synced_media_from_global_talent(sub, mid)
    return {"ok": True}


@router.post("/public/submissions/{sid}/finalize")
async def submission_finalize(sid: str, authorization: Optional[str] = Header(None)):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    form = sub.get("form_data") or {}
    for field in ("first_name", "last_name", "height", "location"):
        if not (form.get(field) or "").strip():
            raise HTTPException(400, f"{field.replace('_',' ').title()} is required")
    avail = form.get("availability") or {}
    if isinstance(avail, str):
        avail = {"status": "yes" if avail else "", "note": avail}
    status = (avail.get("status") or "").strip()
    if status not in {"yes", "no"}:
        raise HTTPException(400, "Please confirm your availability")
    if status == "no" and not (avail.get("note") or "").strip():
        raise HTTPException(400, "Please share your alternate availability")
    budget = form.get("budget") or {}
    if isinstance(budget, str):
        budget = {"status": "accept" if budget else "", "value": budget}
    bstatus = (budget.get("status") or "").strip()
    if bstatus not in {"accept", "custom"}:
        raise HTTPException(400, "Please confirm the budget")
    if bstatus == "custom" and not (budget.get("value") or "").strip():
        raise HTTPException(400, "Please enter your expected budget")
    # Phase 1 v37c: media is fully optional on the audition submission
    # flow. Talents can ship a "form-only" submission and add intro
    # video / takes / portfolio images later via Refine. Caps on the
    # per-upload endpoint still apply (MAX_SUBMISSION_IMAGES, MAX_TAKES,
    # size limits). This block is intentionally empty — no media
    # minimums are enforced at finalize.

    # First-time finalize vs retest finalize
    is_retest = sub.get("status") in ("submitted", "updated")
    new_status = "updated" if is_retest else "submitted"
    patch: Dict[str, Any] = {
        "status": new_status,
        "submitted_at": sub.get("submitted_at") or _now(),
    }
    re_approval = True
    if is_retest:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        if re_approval:
            patch["decision"] = "pending"
        patch["updated_at"] = _now()

    # ------------------------------------------------------------------
    # Auto-link to global Talent DB (dedupe by email).
    # First-time finalize only — retest never overwrites global talent data.
    # Uses the SAME broad $or lookup as /apply approval so the merge logic
    # is consistent across all entry points (Phase 0).
    # ------------------------------------------------------------------
    if not is_retest and not sub.get("talent_id"):
        email = (sub.get("talent_email") or "").lower().strip()
        talent_doc = None
        if email:
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"email": email},
                    {"source.talent_email": email},
                ]},
                {"_id": 0},
            )
        if talent_doc:
            # Q5 (Phase 2 schema unification): "fill empty only" sync. The
            # admin's hand-edits are sacred — we only fill blanks from the
            # talent's latest submission, never overwrite. Media is also
            # never merged here (per Phase 0 spec).
            update: Dict[str, Any] = {}
            unified_fields = (
                "phone", "dob", "height", "location", "ethnicity",
                "gender", "instagram_handle", "instagram_followers", "bio",
            )
            for key in unified_fields:
                if not talent_doc.get(key):
                    val = form.get(key) if key != "phone" else (form.get("phone") or sub.get("talent_phone"))
                    if val:
                        update[key] = val
            if not talent_doc.get("age"):
                age_val = None
                if form.get("age") not in (None, ""):
                    try:
                        age_val = int(form["age"])
                    except Exception:
                        age_val = None
                if age_val is not None:
                    update["age"] = age_val
            new_links = [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()]
            if new_links and not (talent_doc.get("work_links") or []):
                update["work_links"] = new_links
            if update:
                await db.talents.update_one(
                    {"id": talent_doc["id"]}, {"$set": update}
                )
                await update_talent_cover_cache(talent_doc["id"])
        if not talent_doc:
            # Build a minimal talent record from the submission's form_data.
            full_name = (
                f"{(form.get('first_name') or '').strip()} "
                f"{(form.get('last_name') or '').strip()}"
            ).strip() or sub.get("talent_name") or "Unnamed"
            age_val = None
            if form.get("age") not in (None, ""):
                try:
                    age_val = int(form["age"])
                except Exception:
                    age_val = None
            new_talent = {
                "id": str(uuid.uuid4()),
                "name": full_name,
                "email": email or None,
                "phone": (form.get("phone") or sub.get("talent_phone") or None),
                "age": age_val,
                "dob": (form.get("dob") or None),
                "height": (form.get("height") or None),
                "location": (form.get("location") or None),
                "ethnicity": (form.get("ethnicity") or None),
                "gender": (form.get("gender") or None),
                "instagram_handle": (form.get("instagram_handle") or None),
                "instagram_followers": (form.get("instagram_followers") or None),
                "bio": (form.get("bio") or None),
                "work_links": [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()],
                "notes": f"Auto-created from audition submission for project {sub.get('project_id')}",
                # Phase 0 — `source` is ALWAYS an object with the exact shape
                # {type, talent_email, reference_id} so the merge $or lookup
                # works symmetrically across all entry points.
                "source": {
                    "type": "audition_submission",
                    "talent_email": email or None,
                    "reference_id": sid,
                },
                "media": [],                 # keep global media separate (spec: media must NOT merge)
                "cover_media_id": None,
                "created_at": _now(),
                "created_by": "auto-audition",
            }
            try:
                await db.talents.insert_one(new_talent)
                await update_talent_cover_cache(new_talent["id"])
                new_talent.pop("_id", None)
                talent_doc = new_talent
            except DuplicateKeyError:
                # Race: another submission for the same email finalised in
                # parallel. Re-fetch the winner and link to it.
                talent_doc = await db.talents.find_one(
                    {"$or": [
                        {"email": email},
                        {"source.talent_email": email},
                    ]},
                    {"_id": 0},
                )
        if talent_doc:
            patch["talent_id"] = talent_doc["id"]

    await db.submissions.update_one({"id": sid}, {"$set": patch})

    # Phase 3 v37i — at first-time finalize, the talent record is created
    # (or matched) above. ALL pre-finalize image uploads (image/indian/
    # western) need to be retroactively mirrored into the talent's global
    # media so the Global Profile reflects what was uploaded during this
    # submission. Idempotent via source_submission_media_id.
    if not is_retest:
        finalized_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
        if finalized_sub:
            for m in finalized_sub.get("media") or []:
                await sync_media_to_global_talent(finalized_sub, m)

    # Auto-create pipeline entry on first-time finalize.
    # Ensures every submitted talent automatically appears in the casting
    # pipeline at ask_to_test. Best-effort — never blocks the finalize
    # response. Retest finalizes skip this block (row already exists).
    if not is_retest:
        resolved_talent_id = patch.get("talent_id") or sub.get("talent_id")
        if resolved_talent_id:
            from routers.casting_pipeline import (
                ensure_pipeline_from_finalized_submission,
                sync_pipeline_from_submission,
            )
            await ensure_pipeline_from_finalized_submission(
                project_id=sub["project_id"],
                talent_id=resolved_talent_id,
            )
            # Bug fix: if the admin had already set a non-pending decision on
            # this submission BEFORE the talent finalized, the entry above was
            # created at ask_to_test (the default). Immediately apply the
            # existing decision so the card lands in the correct lane.
            # `patch["decision"]` reflects the re-approval reset; use the
            # CURRENT DB decision (from the fresh patch write) instead.
            current_decision = patch.get("decision") or sub.get("decision")
            if current_decision and current_decision != "pending":
                await sync_pipeline_from_submission(
                    project_id=sub["project_id"],
                    talent_id=resolved_talent_id,
                    decision=current_decision,
                )

    # Fan out an admin notification — first-time finalize vs retest variant.
    project = await db.projects.find_one(
        {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or sub.get("project_slug") or "Project"
    talent_name = sub.get("talent_name") or sub.get("talent_email") or "A talent"
    if is_retest:
        await notify_fanout(
            db,
            type="submission_updated",
            title=f"{talent_name} updated their submission",
            body=f"{brand} — back to pending review.",
            payload={"submission_id": sid, "project_id": sub["project_id"]},
        )
    else:
        await notify_fanout(
            db,
            type="submission_new",
            title=f"New submission from {talent_name}",
            body=f"{brand} — awaiting your review.",
            payload={"submission_id": sid, "project_id": sub["project_id"]},
        )
    return {
        "ok": True,
        "status": new_status,
        "resubmitted": is_retest,
        "talent_id": patch.get("talent_id") or sub.get("talent_id"),
    }


@router.get("/public/submissions/{sid}")
async def public_submission(sid: str, authorization: Optional[str] = Header(None)):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    # Surface ONLY approved+shared client feedback. The talent never sees
    # pending/rejected/admin_only rows. This is the single approved channel
    # for client→talent communication (relay through admin moderation).
    from routers.feedback import list_approved_feedback_for_talent
    sub["client_feedback"] = await list_approved_feedback_for_talent(sid)
    return sub


# --------------------------------------------------------------------------
# Public resume-by-token endpoint
# --------------------------------------------------------------------------
@router.get("/public/projects/{slug}/submission/me")
async def get_my_submission_by_token(slug: str, atk: str):
    """Persistent identity resume endpoint.

    Given a long-lived opaque access_token (atk) that was issued when the
    talent first submitted, return the full submission state so the frontend
    can bypass the identity gate and render the dashboard directly.

    This endpoint is intentionally unauthenticated (no JWT required) because
    the access_token itself IS the credential — it is a 256-bit random secret
    stored in the DB, functionally equivalent to a session cookie.
    """
    if not atk or len(atk) < 10:
        raise HTTPException(400, "access_token is required")
    sub = await db.submissions.find_one(
        {"access_token": atk, "project_slug": slug},
        {"_id": 0},
    )
    if not sub:
        raise HTTPException(404, "Submission not found or token invalid")
    from routers.feedback import list_approved_feedback_for_talent
    sub["client_feedback"] = await list_approved_feedback_for_talent(sub["id"])
    return sub


# --------------------------------------------------------------------------
# Admin review
# --------------------------------------------------------------------------
@router.get("/submissions/{sid}/drive")
async def submission_drive_link(
    sid: str, admin: dict = Depends(current_team_or_admin)
):
    """Return the Google Drive folder URL for a submission (admin only).

    Lazily creates the folder path if it doesn't yet exist — useful for
    submissions that have only synced partially. Returns 404 if Drive
    backup is disabled.
    """
    if not drive_enabled():
        raise HTTPException(404, "Google Drive backup is not configured")
    sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    project = await db.projects.find_one(
        {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or "Unknown"
    url = submission_folder_url(brand, sid)
    if not url:
        raise HTTPException(503, "Drive folder lookup failed")
    return {"url": url, "brand": brand, "submission_id": sid}


@router.get("/submissions/approved")
async def list_approved_submissions(
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    """All approved submissions across every project (admin convenience for Link picker)."""
    query = {"decision": "approved"}
    # P2-B: project only the fields the Link-picker UI renders. Excludes
    # form_data (large nested object) and raw media metadata.
    list_proj = {
        "_id": 0, "id": 1, "project_id": 1, "talent_name": 1,
        "talent_email": 1, "cover_media_id": 1,
        "media": {"$slice": 10},   # cap at first 10 for cover resolution
        "created_at": 1,
    }
    cursor = db.submissions.find(query, list_proj).sort("created_at", -1)
    if page is None:
        subs = await cursor.to_list(5000)
        total = None
        p = s = None
    else:
        skip, limit, p, s = _paginate_params(page, size)
        total = await db.submissions.count_documents(query)
        subs = await cursor.skip(skip).limit(limit).to_list(limit)
    # P2-B: fetch only the projects referenced by this result set, not
    # all 2 000+ projects in the DB.
    seen_pids = list({sub.get("project_id") for sub in subs if sub.get("project_id")})
    pmap: Dict[str, Any] = {}
    if seen_pids:
        proj_docs = await db.projects.find(
            {"id": {"$in": seen_pids}},
            {"_id": 0, "id": 1, "brand_name": 1},
        ).to_list(len(seen_pids))
        pmap = {pr["id"]: pr.get("brand_name") for pr in proj_docs}
    out: List[Dict[str, Any]] = []
    for sub in subs:
        shape = _submission_to_client_shape(sub)
        out.append({
            "id": sub["id"],
            "talent_name": shape["name"],
            "project_id": sub.get("project_id"),
            "project_brand": pmap.get(sub.get("project_id")),
            "cover_media_id": shape.get("cover_media_id"),
            "media": shape.get("media"),
            "created_at": sub.get("created_at"),
        })
    if page is None:
        return out
    return _paginated(out, total, p, s)


@router.get("/projects/{pid}/submissions")
async def list_submissions(
    pid: str,
    decision: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    limit: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {"project_id": pid}
    if decision:
        if decision not in SUBMISSION_DECISIONS:
            raise HTTPException(400, "Invalid decision filter")
        query["decision"] = decision
    if status:
        query["status"] = status
    # P3-F: Lightweight list projection — strips form_data (large nested
    # object), media[] (per-image metadata), and field_visibility from the
    # admin review list. The recruiter list renders only summary fields.
    # Full form_data + media are fetched on individual submission GET.
    # NOTE: talent_name is stored at top-level (not inside form_data) — safe.
    _SUB_LIST_PROJ = {
        "_id": 0,
        "field_visibility": 0, # internal toggle map — not rendered in list
    }
    cursor = db.submissions.find(query, _SUB_LIST_PROJ).sort("created_at", -1)
    if page is None and limit is None:
        return await cursor.to_list(5000)
    skip, page_size, p, s = _paginate_params(page, size, limit)
    total = await db.submissions.count_documents(query)
    items = await cursor.skip(skip).limit(page_size).to_list(page_size)
    return _paginated(items, total, p, s)


@router.get("/projects/{pid}/submissions/stats")
async def submissions_stats(
    pid: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Filter-chip counts for the project review queue.

    P1-A: Single $facet aggregation replaces 6 sequential count_documents
    calls, reducing MongoDB round-trips from 6 to 1 per page load.
    Compound indexes (project_id, decision) and (project_id, status) make
    each facet branch index-covered.
    """
    pipeline = [
        {"$match": {"project_id": pid}},
        {"$facet": {
            "all":      [{"$count": "n"}],
            "pending":  [{"$match": {"decision": "pending"}},  {"$count": "n"}],
            "approved": [{"$match": {"decision": "approved"}}, {"$count": "n"}],
            "hold":     [{"$match": {"decision": "hold"}},     {"$count": "n"}],
            "rejected": [{"$match": {"decision": "rejected"}}, {"$count": "n"}],
            "ask_to_test": [{"$match": {"decision": "ask_to_test"}}, {"$count": "n"}],
            "shortlisted": [{"$match": {"decision": "shortlisted"}}, {"$count": "n"}],
            "does_not_work_for_this": [{"$match": {"decision": "does_not_work_for_this"}}, {"$count": "n"}],
            "updated":  [{"$match": {"status": "updated"}},   {"$count": "n"}],
        }},
    ]
    results = await db.submissions.aggregate(pipeline).to_list(1)
    facets = results[0] if results else {}
    def _n(key: str) -> int:
        bucket = facets.get(key) or []
        return bucket[0]["n"] if bucket else 0
    return {
        "all":      _n("all"),
        "pending":  _n("pending"),
        "approved": _n("approved"),
        "hold":     _n("hold"),
        "rejected": _n("rejected"),
        "ask_to_test": _n("ask_to_test"),
        "shortlisted": _n("shortlisted"),
        "does_not_work_for_this": _n("does_not_work_for_this"),
        "updated":  _n("updated"),
    }


@router.post("/projects/{pid}/submissions/{sid}/decision")
async def set_decision(
    pid: str,
    sid: str,
    payload: SubmissionDecisionIn,
    admin: dict = Depends(current_team_or_admin),
):
    if payload.decision not in SUBMISSION_DECISIONS:
        raise HTTPException(400, "Invalid decision")
    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")

    # Resolve talent_id if it is missing/null (fallback matching/creation logic)
    resolved_talent_id = sub.get("talent_id")
    if not resolved_talent_id:
        email = (sub.get("talent_email") or "").lower().strip()
        talent_doc = None
        if email:
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"email": email},
                    {"source.talent_email": email},
                ]},
                {"_id": 0},
            )
        if not talent_doc:
            # Build a minimal talent record from the submission's form_data.
            form = sub.get("form_data") or {}
            full_name = (
                f"{(form.get('first_name') or '').strip()} "
                f"{(form.get('last_name') or '').strip()}"
            ).strip() or sub.get("talent_name") or "Unnamed"
            age_val = None
            if form.get("age") not in (None, ""):
                try:
                    age_val = int(form["age"])
                except Exception:
                    age_val = None
            new_talent = {
                "id": str(uuid.uuid4()),
                "name": full_name,
                "email": email or None,
                "phone": (form.get("phone") or sub.get("talent_phone") or None),
                "age": age_val,
                "dob": (form.get("dob") or None),
                "height": (form.get("height") or None),
                "location": (form.get("location") or None),
                "ethnicity": (form.get("ethnicity") or None),
                "gender": (form.get("gender") or None),
                "instagram_handle": (form.get("instagram_handle") or None),
                "instagram_followers": (form.get("instagram_followers") or None),
                "bio": (form.get("bio") or None),
                "work_links": [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()],
                "notes": f"Auto-created from decision on submission {sid} for project {pid}",
                "source": {
                    "type": "audition_submission",
                    "talent_email": email or None,
                    "reference_id": sid,
                },
                "media": [],
                "cover_media_id": None,
                "created_at": _now(),
                "created_by": "auto-decision-sync",
            }
            try:
                await db.talents.insert_one(new_talent)
                await update_talent_cover_cache(new_talent["id"])
                talent_doc = new_talent
            except DuplicateKeyError:
                # Race: another finalized in parallel. Re-fetch
                talent_doc = await db.talents.find_one(
                    {"$or": [
                        {"email": email},
                        {"source.talent_email": email},
                    ]},
                    {"_id": 0},
                )
        if talent_doc:
            resolved_talent_id = talent_doc["id"]
            # Save resolved talent_id back to the submission document
            await db.submissions.update_one(
                {"id": sid, "project_id": pid},
                {"$set": {"talent_id": resolved_talent_id}}
            )
            # Ensure pipeline row is present at ask_to_test (or default)
            from routers.casting_pipeline import ensure_pipeline_from_finalized_submission
            await ensure_pipeline_from_finalized_submission(
                project_id=pid,
                talent_id=resolved_talent_id,
            )

    prev = sub.get("decision")
    
    # Status History Log transition
    transition = {
        "from_status": prev or "pending",
        "to_status": payload.decision,
        "timestamp": _now(),
        "admin_email": admin.get("email") or "admin@example.com",
        "note": payload.note or ""
    }
    
    res = await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {
            "$set": {"decision": payload.decision, "decided_at": _now()},
            "$push": {"status_history": transition}
        },
    )
    if not res.matched_count:
        raise HTTPException(404, "Submission not found")
        
    # Auto-generate Immutable Package snapshot on Approve (or when decision matches approved/shortlisted/ask_to_test)
    # The spec specifies "When Approve & Forward is clicked: generate immutable snapshot: client_package_snapshot"
    if payload.decision == "approved":
        from core import generate_submission_snapshot
        fresh_sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
        snap_project = await db.projects.find_one({"id": pid}, {"_id": 0, "id": 1, "custom_questions": 1}) if pid else None
        new_snapshot = generate_submission_snapshot(fresh_sub, admin.get("email") or "admin@example.com", project=snap_project)
        
        old_snapshots = fresh_sub.get("client_package_snapshots") or []
        if fresh_sub.get("client_package_snapshot"):
            old_snapshots = [fresh_sub["client_package_snapshot"]] + old_snapshots
            
        await db.submissions.update_one(
            {"id": sid, "project_id": pid},
            {"$set": {
                "client_package_snapshot": new_snapshot,
                "client_package_snapshots": old_snapshots
            }}
        )

    # Fanout — only when the decision actually changes (avoid noise on idempotent calls)
    if prev != payload.decision:
        # Re-fetch the submission AFTER the update so we have the freshest talent_id.
        fresh_sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
        resolved_talent_id = (fresh_sub or sub).get("talent_id")

        if resolved_talent_id:
            # Auto-sync casting pipeline: bump the matching pipeline row to the decision's canonical stage.
            from routers.casting_pipeline import sync_pipeline_from_submission
            await sync_pipeline_from_submission(
                project_id=pid,
                talent_id=resolved_talent_id,
                decision=payload.decision,
            )

        project = await db.projects.find_one({"id": pid}, {"_id": 0, "brand_name": 1})
        brand = (project or {}).get("brand_name") or "Project"
        talent_name = sub.get("talent_name") or sub.get("talent_email") or "Submission"
        await notify_fanout(
            db,
            type="submission_decision",
            title=f"{talent_name} marked as {payload.decision}",
            body=f"{brand}",
            payload={"submission_id": sid, "project_id": pid, "decision": payload.decision},
            actor_id=admin.get("id"),
        )

    return {"ok": True}


@router.get("/projects/{pid}/submissions/{sid}")
async def get_admin_submission(
    pid: str,
    sid: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Retrieve full, individual submission details (admin only)."""
    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    return sub


@router.put("/projects/{pid}/submissions/{sid}")
async def admin_edit_submission(
    pid: str,
    sid: str,
    payload: AdminSubmissionEditIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Admin can edit form_data, toggle per-field visibility, and curate/reorder media for the client view."""
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    
    update: Dict[str, Any] = {}

    # Phase 4 Data Safety: Back up original submitted values to original_form_data and original_media on first override.
    if "original_form_data" not in sub:
        update["original_form_data"] = sub.get("form_data") or {}
    if "original_media" not in sub:
        update["original_media"] = sub.get("media") or []

    if payload.form_data is not None:
        merged_fd = {**(sub.get("form_data") or {}), **payload.form_data}
        update["form_data"] = merged_fd
        fn = merged_fd.get("first_name")
        ln = merged_fd.get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")

        talent_age = None
        email = sub.get("talent_email")
        if email:
            talent_doc = await db.talents.find_one({"$or": [{"email": email}, {"source.talent_email": email}]}, {"age": 1, "dob": 1})
            if talent_doc:
                talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)

        submitted_age_override_val = None
        override_active = merged_fd.get("overrideAge") or merged_fd.get("override_age")
        if override_active and merged_fd.get("submitted_age_override") not in (None, ""):
            try:
                submitted_age_override_val = int(merged_fd["submitted_age_override"])
            except Exception:
                pass

        update["submitted_age_override"] = submitted_age_override_val
        update["effective_age"] = compute_effective_age(merged_fd, talent_age)

    if payload.field_visibility is not None:
        current_fv = sub.get("field_visibility") or {**DEFAULT_FIELD_VISIBILITY}
        update["field_visibility"] = {**current_fv, **payload.field_visibility}

    # Curation History Revision Restore OR Curation Save
    if payload.restore_revision_id:
        revisions = sub.get("media_revision_history") or []
        rev = next((r for r in revisions if r.get("id") == payload.restore_revision_id), None)
        if not rev:
            raise HTTPException(400, "Curation revision not found")
        update["media"] = rev.get("media") or []
    elif payload.media is not None:
        current_media = sub.get("media") or []
        media_by_id = {m.get("id"): m for m in current_media if m.get("id")}
        
        updated_media = []
        for m in payload.media:
            mid = m.get("id")
            if mid and mid in media_by_id:
                # Merge incoming curated properties to preserve old system fields (public_id, content_type, size, url, etc.)
                existing = media_by_id[mid]
                merged = {**existing}
                for k in ["client_visible", "internal_only", "featured_for_client", "primary_take", "featured", "client_cover", "label", "category"]:
                    if k in m:
                        merged[k] = m[k]
                updated_media.append(merged)
            else:
                # Fallback for new/unmatched items
                updated_media.append(m)
        update["media"] = updated_media

    # Auto-create curation history revision
    if payload.form_data is not None or payload.field_visibility is not None or payload.media is not None or payload.restore_revision_id:
        final_media = update.get("media") if ("media" in update) else (sub.get("media") or [])
        rev_id = str(uuid.uuid4())[:8]
        revision = {
            "id": rev_id,
            "timestamp": _now(),
            "admin_email": admin.get("email") or "admin@example.com",
            "media": final_media,
            "note": f"Restored revision {payload.restore_revision_id}" if payload.restore_revision_id else "Saved curations",
        }
        current_history = sub.get("media_revision_history") or []
        update["media_revision_history"] = [revision] + current_history

    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
        
    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    
    # Optional dynamic snapshot regeneration inside PUT
    if payload.regenerate_snapshot:
        from core import generate_submission_snapshot
        snap_project = await db.projects.find_one({"id": fresh_sub.get("project_id") or ""}, {"_id": 0, "id": 1, "custom_questions": 1}) if fresh_sub.get("project_id") else None
        new_snapshot = generate_submission_snapshot(fresh_sub, admin.get("email") or "admin@example.com", project=snap_project)
        old_snapshots = fresh_sub.get("client_package_snapshots") or []
        if fresh_sub.get("client_package_snapshot"):
            old_snapshots = [fresh_sub["client_package_snapshot"]] + old_snapshots
            
        await db.submissions.update_one(
            {"id": sid},
            {"$set": {
                "client_package_snapshot": new_snapshot,
                "client_package_snapshots": old_snapshots
            }}
        )
        fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
        
    return fresh_sub


@router.delete("/projects/{pid}/submissions/{sid}")
async def delete_submission(
    pid: str, sid: str, admin: dict = Depends(current_admin)
):
    res = await db.submissions.delete_one({"id": sid, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Submission not found")
    return {"ok": True}


@router.post("/projects/{pid}/submissions/{sid}/snapshot")
async def regenerate_submission_snapshot_endpoint(
    pid: str,
    sid: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Explicitly regenerate the immutable client package snapshot for a submission."""
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")
        
    from core import generate_submission_snapshot
    snap_project = await db.projects.find_one({"id": pid}, {"_id": 0, "id": 1, "custom_questions": 1}) if pid else None
    new_snapshot = generate_submission_snapshot(sub, admin.get("email") or "admin@example.com", project=snap_project)
    
    old_snapshots = sub.get("client_package_snapshots") or []
    if sub.get("client_package_snapshot"):
        old_snapshots = [sub["client_package_snapshot"]] + old_snapshots
        
    await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {"$set": {
            "client_package_snapshot": new_snapshot,
            "client_package_snapshots": old_snapshots
        }}
    )
    
    # Auto-create history revision for the snapshot regeneration
    rev_id = str(uuid.uuid4())[:8]
    revision = {
        "id": rev_id,
        "timestamp": _now(),
        "admin_email": admin.get("email") or "admin@example.com",
        "media": sub.get("media") or [],
        "note": "Regenerated Client Package Snapshot",
    }
    await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {"$push": {"media_revision_history": {"$each": [revision], "$position": 0}}}
    )
    
    return {"ok": True, "snapshot": new_snapshot}
