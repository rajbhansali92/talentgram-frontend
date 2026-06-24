"""Public submission flow + admin review."""
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


import time
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError
import cloudinary
from core import (
    APP_NAME,
    DEFAULT_FIELD_VISIBILITY,
    DIRECT_VIDEO_UPLOAD,
    DIRECT_VIDEO_CATEGORIES,
    MAX_AUDITION_VIDEO_SECONDS,
    LEGACY_TAKE_CATEGORIES,
    MAX_SUBMISSION_IMAGES,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_TAKES,
    MAX_SUBMISSION_VIDEO_BYTES,
    MAX_IMAGES_PER_CATEGORY,
    PORTFOLIO_IMAGE_CATEGORIES,
    SUBMISSION_DECISIONS,
    SUBMISSION_UPLOAD_CATEGORIES,
    CLOUDINARY_CLOUD_NAME,
    audition_submission_folder,
    audition_video_transformation,
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
    upload_and_track_asset,
    compute_age,
    compute_effective_age,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_access_token,
    make_token,
    normalize_instagram_handle,
    remove_synced_media_from_global_talent,
    sync_media_to_global_talent,
    media_url,
    video_poster_url,
    update_talent_cover_cache,
    normalize_email,
    verify_email_ownership,
    rate_limit_ok,
    client_ip,
)
from drive_backup import (
    drive_enabled,
    enqueue_drive_upload,
    submission_folder_url,
)
from notifications import fanout as notify_fanout

router = APIRouter(prefix="/api", tags=["submissions"])


async def update_talent_submission_metrics(email: str):
    norm_email = normalize_email(email)
    if not norm_email:
        return
    cursor = db.submissions.find({
        "talent_email": norm_email,
        "status": {"$ne": "draft"}
    }).sort("submitted_at", 1)
    subs = await cursor.to_list(length=1000)
    if not subs:
        await db.talents.update_one(
            {"$or": [
                {"normalized_email": norm_email},
                {"email": norm_email},
                {"source.talent_email": norm_email}
            ]},
            {"$set": {
                "first_submission_at": None,
                "last_submission_at": None,
                "total_submissions": 0
            }}
        )
        return
    
    submitted_dates = []
    for s in subs:
        dt = s.get("submitted_at") or s.get("created_at")
        if dt:
            submitted_dates.append(dt)
            
    if submitted_dates:
        first_sub = min(submitted_dates)
        last_sub = max(submitted_dates)
    else:
        first_sub = None
        last_sub = None
        
    await db.talents.update_one(
        {"$or": [
            {"normalized_email": norm_email},
            {"email": norm_email},
            {"source.talent_email": norm_email}
        ]},
        {"$set": {
            "first_submission_at": first_sub,
            "last_submission_at": last_sub,
            "total_submissions": len(subs)
        }}
    )


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
# P0-3: explicit allow-list of project fields that may ever reach an
# unauthenticated talent on the public submission page. Anything NOT in this
# set (e.g. client_budget/agency margin, created_by, future internal notes)
# can never leak by accident — new internal fields are private by default.
_PUBLIC_PROJECT_FIELDS = {
    "id", "slug", "brand_name", "brand_link", "character", "shoot_dates",
    "budget_per_day", "commission_percent", "medium_usage", "director",
    "production_house", "additional_details", "video_links",
    "competitive_brand_enabled", "custom_questions", "talent_budget",
    "require_reapproval_on_edit", "hide_budget_from_talent", "status",
    "submission_requirements", "materials", "created_at",
}


@router.get("/public/projects/{slug}")
async def public_project(slug: str):
    project = await db.projects.find_one({"slug": slug}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")
    # Strict allow-list: drop everything that is not explicitly talent-facing.
    project = {k: v for k, v in project.items() if k in _PUBLIC_PROJECT_FIELDS}
    # Gate budget visibility: if admin has toggled "Hide Budget From Talent",
    # strip budget_per_day and talent_budget from the public payload.
    if project.get("hide_budget_from_talent"):
        project.pop("budget_per_day", None)
        project.pop("talent_budget", None)
    return project


@router.get("/public/prefill")
async def prefill_for_email(
    email: str,
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """Prefill lookup endpoint: requires valid submitter session token in the headers."""
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many lookups — please slow down")

    email = normalize_email(email)
    if not email or "@" not in email:
        return {}

    # Remedy IDOR: require PROOF OF OWNERSHIP of the queried email. Accepts a
    # portal token (OTP/Google) or an existing submitter credential bound to
    # this email (see verify_email_ownership). This is the same gate used by
    # the apply/submission start flows, so the frontend only needs to present
    # the portal token it already holds after verification.
    if not await verify_email_ownership(authorization, email):
        # Prevent email enumeration: generic empty schema on invalid auth.
        return {}

    talent = await db.talents.find_one(
        {"$or": [
            {"normalized_email": email},
            {"email": email},
            {"source.talent_email": email}
        ]},
        {
            "_id": 0, "name": 1, "age": 1, "dob": 1, "height": 1,
            "phone": 1, "location": 1, "ethnicity": 1, "gender": 1, "bio": 1,
            "instagram_handle": 1, "instagram_followers": 1, "work_links": 1,
            "media": 1, "cover_media_id": 1, "skills": 1,
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
        category = m.get("category")
        if category == "portfolio":
            category = "image"
        resource_type = m.get("resource_type") or "image"
        is_image = resource_type == "image" or (category not in {"video", "intro_video"} and not (m.get("content_type") or "").startswith("video/"))
        if is_image and m.get("url"):
            prefill_images.append({
                "id": m.get("id"),
                "category": category or "image",
                "url": m.get("url"),
                "public_id": m.get("public_id"),
                "resource_type": "image",
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
        "skills": talent.get("skills") or [],
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
async def start_submission(
    slug: str,
    payload: SubmissionStartIn,
    request: Request = None,
    authorization: Optional[str] = Header(None),
):
    """Start OR resume a submission.

    If a submission already exists for (project, email), returns a fresh token
    that unlocks edits — this is the retest / re-upload entry point. The
    decision is NOT reset here; only `finalize` flips it back to pending.

    P0-2: token + persistent access_token issuance, and any talent-media
    prefill, are gated behind proof of email ownership whenever there is
    pre-existing data for the email (an existing submission OR a canonical
    talent profile). A brand-new (project, email) with no talent record has no
    PII to leak, so it keeps the friction-free first-time flow.
    """
    project = await db.projects.find_one({"slug": slug})
    if not project:
        raise HTTPException(404, "Project not found")
    email = normalize_email(payload.email)
    if not email:
        raise HTTPException(400, "Invalid email address")

    # P1-4: burst / enumeration protection (per-IP + per-(project,email)).
    # `request` is always injected over HTTP; only None for in-process direct
    # calls (tests), which are not an attack surface and skip the limiter.
    if request is not None:
        ip = client_ip(request)
        if not rate_limit_ok(f"sub:ip:{ip}", limit=20, window_seconds=60.0):
            raise HTTPException(429, "Too many attempts — please try again shortly")
        if not rate_limit_ok(f"sub:{slug}:{email}", limit=10, window_seconds=300.0):
            raise HTTPException(429, "Too many attempts for this email — please try again later")

    existing = await db.submissions.find_one({
        "project_id": project["id"],
        "talent_email": email,
    })

    # P0-2: gate when pre-existing data exists for this email.
    talent_exists = await db.talents.find_one(
        {"$or": [
            {"normalized_email": email},
            {"email": email},
            {"source.talent_email": email},
        ]},
        {"_id": 1},
    )
    if existing or talent_exists:
        owns = await verify_email_ownership(authorization, email)
        if not owns:
            raise HTTPException(
                403,
                "Please verify your email to continue. We'll send you a one-time code.",
            )

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
            {"$or": [
                {"normalized_email": email},
                {"email": email},
                {"source.talent_email": email}
            ]},
            {"age": 1, "dob": 1, "media": 1}
        )
        if talent_doc:
            talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)
            # Image prefill: fetch existing image, indian, western look images from master profile
            for m in (talent_doc.get("media") or []):
                category = m.get("category")
                if category == "portfolio":
                    category = "image"
                if category in {"image", "indian", "western"} and m.get("url"):
                    prefill_media.append({
                        "id": m.get("id"),
                        "category": category,
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

    cb_visible = True
    fv_defaults = {**DEFAULT_FIELD_VISIBILITY, "competitive_brand": cb_visible}

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
        "field_visibility": fv_defaults,
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
            norm_email = normalize_email(email)
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"normalized_email": norm_email},
                    {"email": norm_email},
                    {"source.talent_email": norm_email}
                ]},
                {"age": 1, "dob": 1}
            )
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
        if ct in {"image/bmp", "image/tiff"} or fn.endswith((".bmp", ".tiff")):
            raise HTTPException(400, "BMP and TIFF formats are not supported. Please upload JPEG, PNG, or HEIC.")
        if not (ct.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))):
            raise HTTPException(400, "Unsupported image format. Please upload JPG, PNG, WEBP, or HEIC.")

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
    
    if is_video_slot:
        asset_type = "intro_video" if category == "intro_video" else "audition_video"
    else:
        asset_type = "profile_image"
        
    keep_orig = (asset_type != "audition_video")
    
    tid = sub.get("talent_id")
    tname = sub.get("talent_name")
    if not tid:
        norm_email = normalize_email(sub.get("talent_email"))
        if norm_email:
            talent_doc = await db.talents.find_one({
                "$or": [
                    {"normalized_email": norm_email},
                    {"email": norm_email},
                    {"source.talent_email": norm_email}
                ]
            })
            if talent_doc:
                tid = talent_doc.get("id")
                tname = talent_doc.get("name")
    if not tid:
        tid = "unknown_talent"

    result = await upload_and_track_asset(
        data,
        resource_type=rt,
        content_type=file.content_type,
        asset_type=asset_type,
        talent_id=tid,
        talent_name=tname,
        project_id=sub.get("project_id"),
        submission_id=sid,
        keep_original=keep_orig,
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
        "thumbnail_url": result.get("thumbnail_url") if (is_video and result.get("thumbnail_url")) else (media_url(result["public_id"], preset="thumb", resource_type=result["resource_type"]) if is_image else None),
        "poster_url": result.get("thumbnail_url") if (is_video and result.get("thumbnail_url")) else (video_poster_url(result["public_id"]) if is_video else None),
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


class SignUploadIn(BaseModel):
    category: str
    filename: str


@router.post("/public/submissions/{sid}/upload/sign")
async def submission_sign_upload(
    sid: str,
    payload: SignUploadIn,
    authorization: Optional[str] = Header(None),
):
    from core import DIRECT_UPLOAD_ENABLED
    if not DIRECT_UPLOAD_ENABLED:
        raise HTTPException(400, "Direct uploads are currently disabled")
        
    submitter = await decode_submitter(authorization)

    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    
    category = payload.category
    filename = payload.filename
    
    if category not in SUBMISSION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
        
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
        
    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}
    
    if category in PORTFOLIO_IMAGE_CATEGORIES:
        existing = sum(1 for m in sub.get("media", []) if m.get("category") == category)
        if existing >= MAX_IMAGES_PER_CATEGORY:
            raise HTTPException(400, f"Limit reached")
            
    if category == "take":
        existing_takes = sum(
            1 for m in sub.get("media", []) 
            if m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        )
        if existing_takes >= MAX_SUBMISSION_TAKES:
            raise HTTPException(400, f"Maximum takes reached")

    single_slot = {"intro_video", "take_1", "take_2", "take_3"}
    if category in single_slot:
        await db.submissions.update_one(
            {"id": sid}, {"$pull": {"media": {"category": category}}}
        )

    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/submissions/{sid}"
    public_id = media_id
    rt = "video" if is_video_slot else "image"
    
    eager = None
    transformation = None
    
    if is_video_slot:
        if category == "intro_video":
            eager = "w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4|w_600,h_338,c_fill,q_auto,f_jpg"
        else:
            transformation = "w_1280,h_720,c_limit,q_auto,vc_auto"
            eager = "w_600,h_338,c_fill,q_auto,f_jpg"
    else:
        eager = "w_400,c_fill,dpr_auto,f_auto,q_auto"

    import time
    import cloudinary.utils
    
    timestamp = int(time.time())
    params = {
        "folder": folder,
        "public_id": public_id,
        "timestamp": timestamp,
    }
    if eager:
        params["eager"] = eager
    if transformation:
        params["transformation"] = transformation
        
    api_secret = cloudinary.config().api_secret
    signature = cloudinary.utils.api_sign_request(params, api_secret)
    
    return {
        "signature": signature,
        "timestamp": timestamp,
        "api_key": cloudinary.config().api_key,
        "cloud_name": cloudinary.config().cloud_name,
        "folder": folder,
        "public_id": public_id,
        "resource_type": rt,
        "eager": eager,
        "transformation": transformation,
        "media_id": media_id,
    }


class CompleteUploadIn(BaseModel):
    media_id: str
    category: str
    label: Optional[str] = None
    public_id: str
    url: str
    bytes: int
    duration: Optional[float] = None
    content_type: Optional[str] = None
    original_filename: Optional[str] = None
    eager: Optional[List[dict]] = None


@router.post("/public/submissions/{sid}/upload/complete")
async def submission_complete_upload(
    sid: str,
    payload: CompleteUploadIn,
    authorization: Optional[str] = Header(None),
):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
        
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
        
    category = payload.category
    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}
    is_video = is_video_slot
    is_image = not is_video_slot
    
    thumbnail_url = None
    poster_url = None
    eager_list = payload.eager or []
    
    if is_video:
        poster_url = next((x.get("secure_url") for x in eager_list if x.get("format") == "jpg"), None)
        compressed_mp4 = next((x.get("secure_url") for x in eager_list if x.get("format") == "mp4"), None)
        url = compressed_mp4 or payload.url
    else:
        url = payload.url
        thumbnail_url = media_url(payload.public_id, preset="thumb", resource_type="image")
        
    if not poster_url and is_video:
        poster_url = video_poster_url(payload.public_id)
        
    media = {
        "id": payload.media_id,
        "category": category,
        "url": url,
        "public_id": payload.public_id,
        "resource_type": "video" if is_video else "image",
        "content_type": payload.content_type or ("video/mp4" if is_video else "image/jpeg"),
        "original_filename": payload.original_filename,
        "size": payload.bytes,
        "created_at": _now(),
        "scope": "submission",
        "submission_id": sid,
        "project_id": sub["project_id"],
        "duration": payload.duration,
        "thumbnail_url": poster_url if is_video else thumbnail_url,
        "poster_url": poster_url if is_video else None,
    }
    
    existing_takes = 0
    if category == "take":
        existing_takes = sum(
            1 for m in sub.get("media", []) 
            if m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        )
        media["label"] = (payload.label or "").strip() or f"Take {existing_takes + 1}"

    tid = sub.get("talent_id")
    tname = sub.get("talent_name")
    if not tid:
        norm_email = normalize_email(sub.get("talent_email"))
        if norm_email:
            talent_doc = await db.talents.find_one({
                "$or": [
                    {"normalized_email": norm_email},
                    {"email": norm_email},
                    {"source.talent_email": norm_email}
                ]
            })
            if talent_doc:
                tid = talent_doc.get("id")
                tname = talent_doc.get("name")
    if not tid:
        tid = "unknown_talent"
        
    asset_type = "profile_image"
    if is_video:
        asset_type = "intro_video" if category == "intro_video" else "audition_video"
        
    await db.asset_metadata.insert_one({
        "id": payload.media_id,
        "public_id": payload.public_id,
        "folder": f"{APP_NAME}/submissions/{sid}",
        "resource_type": "video" if is_video else "image",
        "asset_type": asset_type,
        "talent_id": tid,
        "talent_name": tname,
        "project_id": sub.get("project_id"),
        "submission_id": sid,
        "file_size": payload.bytes,
        "created_at": _now(),
        "status": "completed"
    })
    
    patch: Dict[str, Any] = {"$push": {"media": media}}
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
    await sync_media_to_global_talent(updated, media)
    
    if was_finalized:
        brand = (proj or {}).get("brand_name") or sub.get("project_slug") or "Project"
        talent_name = sub.get("talent_name") or sub.get("talent_email") or "A talent"
        await notify_fanout(
            db,
            type="submission_updated",
            title=f"{talent_name} updated their submission",
            body=f"{brand} — back to pending review.",
            payload={"submission_id": sid, "project_id": sub["project_id"]},
        )
        
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


# ==========================================================================
# Architecture C — direct browser→Cloudinary audition-video upload
# (feature-flagged; images & all other flows unchanged)
# ==========================================================================

_DIRECT_VIDEO_CATS = {"intro_video", "take", "take_1", "take_2", "take_3"}


class VideoSignatureIn(BaseModel):
    category: str
    label: Optional[str] = None
    content_type: Optional[str] = None


class VideoCompleteIn(BaseModel):
    public_id: str
    secure_url: Optional[str] = None
    url: Optional[str] = None
    resource_type: Optional[str] = "video"
    bytes: Optional[int] = 0
    duration: Optional[float] = None
    format: Optional[str] = None


async def _resolve_submission_talent(sub: dict):
    """Resolve (talent_id, talent_name) for a submission — same logic as the
    Railway upload path so the Cloudinary folder is identical."""
    tid = sub.get("talent_id")
    tname = sub.get("talent_name")
    if not tid:
        norm_email = normalize_email(sub.get("talent_email"))
        if norm_email:
            t = await db.talents.find_one({
                "$or": [
                    {"normalized_email": norm_email},
                    {"email": norm_email},
                    {"source.talent_email": norm_email},
                ]
            })
            if t:
                tid = t.get("id")
                tname = t.get("name")
    if not tid:
        tid = "unknown_talent"
    return tid, tname


def _category_from_cloudinary_tags(tags) -> Optional[str]:
    """Derive the audition category ONLY from backend-generated Cloudinary tags.
    Never trust a client-supplied category. Unknown ⇒ None (quarantine)."""
    for t in (tags or []):
        if isinstance(t, str) and t.startswith("category="):
            c = t.split("=", 1)[1].strip()
            if c in _DIRECT_VIDEO_CATS:
                return c
    return None


async def attach_video_media(sub: dict, asset: dict, category: str, label: Optional[str] = None) -> Optional[dict]:
    """Attach a Cloudinary video asset to a submission. Single-slot for
    intro_video; dedup by public_id (idempotent); preserves the re-approval
    flip used by the Railway upload path."""
    from datetime import datetime, timezone  # noqa: F401
    sid = sub["id"]
    public_id = asset.get("public_id")
    if not public_id:
        return None
    # Idempotency: never attach the same public_id twice.
    for m in (sub.get("media") or []):
        if m.get("public_id") == public_id:
            return m

    secure = asset.get("secure_url") or asset.get("url")
    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "url": secure,
        "public_id": public_id,
        "resource_type": "video",
        "content_type": "video/mp4",
        "original_filename": None,
        "size": asset.get("bytes") or 0,
        "created_at": _now(),
        "scope": "submission",
        "submission_id": sid,
        "project_id": sub.get("project_id"),
        "duration": asset.get("duration"),
        "thumbnail_url": video_poster_url(public_id),
        "poster_url": video_poster_url(public_id),
        "source": "direct_upload",
    }
    if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
        media["label"] = (label or "").strip() or "Take"

    # Single-slot replacement for intro_video (cannot mix $pull and $push on the
    # same field in one update, so pull first).
    if category == "intro_video":
        await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"category": "intro_video"}}})

    push: Dict[str, Any] = {"$push": {"media": media}}
    fresh = await db.submissions.find_one({"id": sid})
    if fresh and fresh.get("status") in ("submitted", "updated"):
        proj = await db.projects.find_one(
            {"id": sub.get("project_id")}, {"_id": 0, "require_reapproval_on_edit": 1}
        )
        set_patch = {"status": "updated", "updated_at": _now()}
        if bool((proj or {}).get("require_reapproval_on_edit", True)):
            set_patch["decision"] = "pending"
        push["$set"] = set_patch
    await db.submissions.update_one({"id": sid}, push)
    try:
        await db.asset_metadata.update_one(
            {"public_id": public_id},
            {"$set": {"upload_status": "completed", "updated_at": datetime.now(timezone.utc)}},
        )
    except Exception as e:
        logger.warning(f"attach_video_media: asset_metadata flip failed {public_id}: {e}")
    return media


async def reconcile_submission_videos(sid: str) -> None:
    """Finalize safety net: attach any audition video that reached Cloudinary
    (scoped to this submission's folder) but isn't yet on the submission.
    No-op when the feature flag is off. Idempotent + folder-scoped + category
    gated; audition takes can never become a globally-synced category."""
    if not DIRECT_VIDEO_UPLOAD:
        return
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        return
    tid, tname = await _resolve_submission_talent(sub)
    folder = audition_submission_folder(tid, tname, sub.get("project_id"), sid)
    try:
        resp = cloudinary.api.resources(
            resource_type="video", type="upload", prefix=folder,
            tags=True, context=True, max_results=100,
        )
    except Exception as e:
        logger.warning(f"reconcile_submission_videos: cloudinary list failed for {sid}: {e}")
        return
    existing_pids = {m.get("public_id") for m in (sub.get("media") or []) if m.get("public_id")}
    take_count = sum(
        1 for m in (sub.get("media") or [])
        if m.get("category") in ("take",) or m.get("category") in LEGACY_TAKE_CATEGORIES
    )
    for a in resp.get("resources", []):
        pid = a.get("public_id")
        if not pid or pid in existing_pids:
            continue
        category = _category_from_cloudinary_tags(a.get("tags"))
        if category is None:
            # SAFETY: never default an uncategorized asset to a synced category.
            logger.warning(f"reconcile: quarantining uncategorized asset {pid} on submission {sid}")
            continue
        if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
            if take_count >= MAX_SUBMISSION_TAKES:
                continue
            take_count += 1
        attached = await attach_video_media(sub, a, category)
        if attached:
            existing_pids.add(pid)
        sub = await db.submissions.find_one({"id": sid})  # refresh for single-slot pulls


@router.post("/public/submissions/{sid}/video-signature")
async def video_signature(
    sid: str,
    payload: VideoSignatureIn,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Issue a short-lived signed Cloudinary upload for ONE audition video slot.
    Transformation/folder/public_id/category are pinned server-side."""
    from datetime import datetime, timezone
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many requests — please slow down")
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    category = (payload.category or "").strip()
    if category not in _DIRECT_VIDEO_CATS:
        raise HTTPException(400, "Invalid video category")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
        existing_takes = sum(
            1 for m in (sub.get("media") or [])
            if m.get("category") in ("take",) or m.get("category") in LEGACY_TAKE_CATEGORIES
        )
        if existing_takes >= MAX_SUBMISSION_TAKES:
            raise HTTPException(400, f"Maximum {MAX_SUBMISSION_TAKES} takes reached — delete one to add another")

    tid, tname = await _resolve_submission_talent(sub)
    folder = audition_submission_folder(tid, tname, sub.get("project_id"), sid)
    leaf = "intro_video" if category == "intro_video" else f"take_{uuid.uuid4().hex}"
    public_id = f"{folder}/{leaf}"

    # Pinned, string-encoded transformation + eager poster (signed verbatim).
    transformation = "c_limit,h_720,w_1280/q_auto,vc_auto"
    eager = "c_fill,h_338,w_600,q_auto/f_jpg"
    tags = f"submission_id={sid},project_id={sub.get('project_id')},talent_id={tid},category={category},asset_kind=audition_video"
    context = f"category={category}|label={(payload.label or '').strip()}"
    timestamp = int(time.time())
    params_to_sign = {
        "timestamp": timestamp,
        "folder": folder,
        "public_id": public_id,
        "transformation": transformation,
        "format": "mp4",
        "eager": eager,
        "eager_async": "true",
        "overwrite": "true",
        "tags": tags,
        "context": context,
    }
    cfg = cloudinary.config()
    signature = cloudinary.utils.api_sign_request(params_to_sign, cfg.api_secret)

    try:
        await db.asset_metadata.update_one(
            {"public_id": public_id},
            {"$set": {
                "public_id": public_id, "submission_id": sid, "talent_id": tid,
                "project_id": sub.get("project_id"), "category": category,
                "asset_type": "intro_video" if category == "intro_video" else "audition_video",
                "resource_type": "video", "upload_status": "pending",
                "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"video-signature: pending asset_metadata write failed {public_id}: {e}")

    return {
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key": cfg.api_key,
        "timestamp": timestamp,
        "signature": signature,
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload",
        "params": {
            "folder": folder, "public_id": public_id, "transformation": transformation,
            "format": "mp4", "eager": eager, "eager_async": "true", "overwrite": "true",
            "tags": tags, "context": context,
        },
        "max_duration_seconds": MAX_AUDITION_VIDEO_SECONDS,
    }


@router.post("/public/submissions/{sid}/video-complete")
async def video_complete(
    sid: str,
    payload: VideoCompleteIn,
    authorization: Optional[str] = Header(None),
):
    """Optimistic fast-path: attach a just-uploaded direct video. Category and
    folder are validated server-side; finalize reconciliation is the safety net
    if this never fires."""
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    tid, tname = await _resolve_submission_talent(sub)
    folder = audition_submission_folder(tid, tname, sub.get("project_id"), sid)
    public_id = (payload.public_id or "").strip()
    # Folder scoping — reject assets that don't belong to this submission.
    if not public_id.startswith(folder + "/"):
        raise HTTPException(400, "Asset does not belong to this submission")

    category = None
    duration = payload.duration
    asset: Dict[str, Any] = {
        "public_id": public_id,
        "secure_url": payload.secure_url or payload.url,
        "bytes": payload.bytes,
        "duration": payload.duration,
    }
    try:
        res = cloudinary.api.resource(public_id, resource_type="video", tags=True)
        category = _category_from_cloudinary_tags(res.get("tags"))
        duration = res.get("duration", duration)
        asset = res
    except Exception as e:
        logger.warning(f"video-complete: resource fetch failed {public_id}: {e}")
        leaf = public_id.rsplit("/", 1)[-1]
        if leaf == "intro_video":
            category = "intro_video"
        elif leaf.startswith("take_"):
            category = "take"
    if category is None:
        raise HTTPException(400, "Could not determine media category")

    if duration is not None and float(duration) > MAX_AUDITION_VIDEO_SECONDS:
        try:
            cloudinary.uploader.destroy(public_id, resource_type="video")
        except Exception:
            pass
        raise HTTPException(400, f"Audition video must be {MAX_AUDITION_VIDEO_SECONDS // 60} minutes or less.")

    media = await attach_video_media(sub, asset, category)
    return {"ok": True, "media": media}


@router.post("/public/submissions/{sid}/finalize")
async def submission_finalize(sid: str, authorization: Optional[str] = Header(None)):
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    # Architecture C — attach any direct-upload audition videos that reached
    # Cloudinary but weren't attached (lost /video-complete). No-op when the
    # flag is off. Runs BEFORE media validation + the global-sync block so the
    # existing category gate still protects audition takes.
    await reconcile_submission_videos(sid)
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    form = sub.get("form_data") or {}
    project = await db.projects.find_one({"id": sub["project_id"]})
    if not project:
        raise HTTPException(404, "Project not found")

    requirements = project.get("submission_requirements")
    if requirements and requirements.get("strictness") == "strict":
        fields_config = requirements.get("fields") or {}

        # 1. Standard Profile Fields
        if fields_config.get("name") == "required":
            if not (form.get("first_name") or "").strip() or not (form.get("last_name") or "").strip():
                raise HTTPException(400, "First and Last Name are required")
        if fields_config.get("email") == "required" and not (sub.get("talent_email") or "").strip():
            raise HTTPException(400, "Email is required")
        if fields_config.get("phone") == "required" and not (form.get("phone") or "").strip():
            raise HTTPException(400, "Phone is required")
        if fields_config.get("dob") == "required" and not (form.get("dob") or "").strip():
            raise HTTPException(400, "Date of Birth is required")
        if fields_config.get("age") == "required" and form.get("age") is None:
            raise HTTPException(400, "Age is required")
        if fields_config.get("height") == "required" and not (form.get("height") or "").strip():
            raise HTTPException(400, "Height is required")
        loc_val = form.get("location")
        is_loc_empty = not loc_val or (isinstance(loc_val, str) and not loc_val.strip())
        if fields_config.get("location") == "required" and is_loc_empty:
            raise HTTPException(400, "Current Location is required")
        if fields_config.get("gender") == "required" and not (form.get("gender") or "").strip():
            raise HTTPException(400, "Gender is required")
        if fields_config.get("ethnicity") == "required" and not (form.get("ethnicity") or "").strip():
            raise HTTPException(400, "Ethnicity is required")
        if fields_config.get("instagram_handle") == "required" and not (form.get("instagram_handle") or "").strip():
            raise HTTPException(400, "Instagram Handle is required")
        if fields_config.get("instagram_followers") == "required" and not (form.get("instagram_followers") or "").strip():
            raise HTTPException(400, "Instagram Followers is required")
        if fields_config.get("bio") == "required" and not (form.get("bio") or "").strip():
            raise HTTPException(400, "Bio is required")
        if fields_config.get("competitive_brand") == "required" and not (form.get("competitive_brand") or "").strip():
            raise HTTPException(400, "Competitive Brand is required")

        if fields_config.get("availability") == "required":
            avail = form.get("availability") or {}
            if isinstance(avail, str):
                avail = {"status": "yes" if avail else "", "note": avail}
            status = (avail.get("status") or "").strip()
            if status not in {"yes", "no"}:
                raise HTTPException(400, "Please confirm your availability")
            if status == "no" and not (avail.get("note") or "").strip():
                raise HTTPException(400, "Please share your alternate availability")

        if fields_config.get("budget_expectation") == "required":
            budget = form.get("budget") or {}
            if isinstance(budget, str):
                budget = {"status": "accept" if budget else "", "value": budget}
            bstatus = (budget.get("status") or "").strip()
            if bstatus not in {"accept", "custom"}:
                raise HTTPException(400, "Please confirm the budget")
            if bstatus == "custom" and not (budget.get("value") or "").strip():
                raise HTTPException(400, "Please enter your expected budget")

        if requirements.get("interested_in") == "required":
            if not form.get("interested_in"):
                raise HTTPException(400, "Please select at least one casting interest")

        # 2. Custom Questions
        custom_reqs = requirements.get("custom_questions") or {}
        custom_answers = form.get("custom_answers") or {}
        for cq in project.get("custom_questions") or []:
            qid = cq.get("id")
            if qid and custom_reqs.get(qid) == "required":
                if not str(custom_answers.get(qid) or "").strip():
                    raise HTTPException(400, f"Question '{cq.get('question')}' is required")

        # 3. Media
        media_list = sub.get("media") or []
        intro_req = requirements.get("intro_video")
        if intro_req == "required":
            has_intro = any(m.get("category") == "intro_video" for m in media_list)
            if not has_intro:
                raise HTTPException(400, "Introduction Video is required")

        min_takes = int(requirements.get("min_audition_takes") or 0)
        if min_takes > 0:
            takes_count = sum(1 for m in media_list if m.get("category") in {"take", "take_1", "take_2", "take_3"})
            if takes_count < min_takes:
                raise HTTPException(400, f"Please upload at least {min_takes} audition take(s)")

        portfolio_reqs = requirements.get("portfolio") or {}
        for category, label_name in [("image", "Portfolio (General)"), ("indian", "Indian Look"), ("western", "Western Look")]:
            min_count = int(portfolio_reqs.get(category) or 0)
            if min_count > 0:
                count = sum(1 for m in media_list if m.get("category") == category)
                if count < min_count:
                    raise HTTPException(400, f"{label_name} requires at least {min_count} image(s)")

        # 4. Work Links
        min_links = int(requirements.get("min_work_links") or 0)
        if min_links > 0:
            links_count = len(form.get("work_links") or [])
            if links_count < min_links:
                raise HTTPException(400, f"Please add at least {min_links} work link(s)")

        # 5. Skills & Special Abilities
        skills_reqs = requirements.get("skills") or {}
        user_skills = form.get("skills") or []
        SKILLS_CATEGORIES = {
            "Dance": ["Hip Hop", "Contemporary", "Bollywood", "Bharatanatyam", "Kathak", "Salsa", "Ballet"],
            "Music": ["Singer", "Piano", "Keyboard", "Guitar", "Violin", "Drums", "Flute", "Ukulele", "DJ", "Beatboxing", "Rapper", "Composer", "Music Producer"],
            "Sports & Fitness": ["Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing", "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket", "Football", "Basketball", "Tennis", "Badminton"],
            "Action & Stunts": ["Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu", "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour", "Sword Fighting"],
            "Vehicle Skills": ["Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle", "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat", "Ride Jet Ski"],
            "Performance": ["Actor", "Voice Artist", "Dancer", "Singer", "Host", "Anchor", "Model", "Theatre Artist", "Improvisation", "Stand-up Comedy"],
            "Special Skills": ["Skateboarding", "Roller Skating", "Ice Skating", "Surfing", "Scuba Diving", "Fire Performance", "Juggling"],
            "Languages": ["English", "Hindi", "Spanish", "French", "Mandarin Chinese", "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati", "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Urdu", "Other"]
        }
        for cat, req in skills_reqs.items():
            if req:
                valid_skills = SKILLS_CATEGORIES.get(cat) or []
                if not any(s in valid_skills for s in user_skills):
                    raise HTTPException(400, f"At least one skill from category '{cat}' is required")

        # 6. Conditional Rules
        conditional_rules = requirements.get("conditional_rules") or []
        for rule in conditional_rules:
            qid = rule.get("question_id")
            trigger = rule.get("trigger_value")
            video_label = rule.get("video_label")
            if qid and trigger and video_label:
                ans = str(custom_answers.get(qid) or "").strip().lower()
                if ans == str(trigger).strip().lower():
                    has_cond_video = any(
                        m.get("category") in {"take", "intro_video", "take_1", "take_2", "take_3"}
                        and str(m.get("label") or "").strip().lower() == video_label.strip().lower()
                        for m in media_list
                    )
                    if not has_cond_video:
                        raise HTTPException(400, f"Conditional requirement '{video_label}' is missing")
    else:
        # Fallback legacy validation rules
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

    # Auto-expire pending assets older than 30 minutes to prevent indefinitely blocking the user
    from datetime import datetime, timezone, timedelta
    timeout_limit = datetime.now(timezone.utc) - timedelta(minutes=30)
    await db.asset_metadata.update_many(
        {
            "submission_id": sid,
            "upload_status": "pending",
            "created_at": {"$lt": timeout_limit}
        },
        {
            "$set": {
                "upload_status": "failed",
                "error_reason": "Upload timed out (30 minutes limit exceeded)",
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    # Verify that all Cloudinary uploads associated with this submission have completed.
    active_public_ids = [m["public_id"] for m in sub.get("media", []) if m.get("public_id")]
    if active_public_ids:
        pending_assets = await db.asset_metadata.find_one({
            "submission_id": sid,
            "public_id": {"$in": active_public_ids},
            "upload_status": "pending"
        })
        if pending_assets:
            raise HTTPException(400, "Cloudinary uploads are still in progress. Please wait until uploads are complete.")

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
    # ------------------------------------------------------------------
    # Auto-link/update to global Talent DB (dedupe by email).
    # ------------------------------------------------------------------
    email = normalize_email(sub.get("talent_email"))
    talent_doc = None
    if sub.get("talent_id"):
        talent_doc = await db.talents.find_one({"id": sub["talent_id"]}, {"_id": 0})
    if not talent_doc and email:
        talent_doc = await db.talents.find_one(
            {"$or": [
                {"normalized_email": email},
                {"email": email},
                {"source.talent_email": email},
            ]},
            {"_id": 0},
        )

    if talent_doc:
        from core import merge_talent_profile
        # Merge fields (Task 4 & 6)
        form_to_merge = dict(form)
        form_to_merge["email"] = email
        form_to_merge["normalized_email"] = email
        if "phone" not in form_to_merge or not form_to_merge["phone"]:
            form_to_merge["phone"] = sub.get("talent_phone")
        
        # Exception: Project-specific overrides for location must remain separate
        form_to_merge.pop("location", None)
        
        await merge_talent_profile(talent_doc, form_to_merge, "project_submission")
        await update_talent_cover_cache(talent_doc["id"])
    else:
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
            "normalized_email": email or None,
            "phone": (form.get("phone") or sub.get("talent_phone") or None),
            "age": age_val,
            "dob": (form.get("dob") or None),
            "height": (form.get("height") or None),
            "location": (form.get("location") or None),
            "ethnicity": (form.get("ethnicity") or None),
            "gender": (form.get("gender") or None),
            "instagram_handle": normalize_instagram_handle(form.get("instagram_handle") or None),
            "instagram_followers": (form.get("instagram_followers") or None),
            "bio": (form.get("bio") or None),
            "skills": [s for s in (form.get("skills") or []) if isinstance(s, str) and s.strip()],
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
            "status": "SUBMITTED",
            "created_at": _now(),
            "updated_at": _now(),
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
                    {"normalized_email": email},
                    {"email": email},
                    {"source.talent_email": email},
                ]},
                {"_id": 0},
            )
            if talent_doc:
                from core import merge_talent_profile
                form_to_merge = dict(form)
                form_to_merge["email"] = email
                form_to_merge["normalized_email"] = email
                if "phone" not in form_to_merge or not form_to_merge["phone"]:
                    form_to_merge["phone"] = sub.get("talent_phone")
                form_to_merge.pop("location", None)
                await merge_talent_profile(talent_doc, form_to_merge, "project_submission")
                await update_talent_cover_cache(talent_doc["id"])
    if talent_doc:
        patch["talent_id"] = talent_doc["id"]

    await db.submissions.update_one({"id": sid}, {"$set": patch})

    # Sync all uploads retroactively into the talent's global media.
    # Idempotent via source_submission_media_id.
    finalized_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if finalized_sub and talent_doc:
        # Enforce replacement policy: clear existing canonical media for the incoming categories
        incoming_categories = set()
        cat_mapping = {
            "image": "portfolio",
            "portfolio": "portfolio",
            "indian": "indian",
            "western": "western",
            "video": "video",
            "intro_video": "video",
            "headshot": "headshot",
            "headshots": "headshot",
            "additional_portfolio": "additional_portfolio"
        }
        for m in finalized_sub.get("media") or []:
            cat = m.get("category")
            if cat in cat_mapping:
                incoming_categories.add(cat_mapping[cat])

        if incoming_categories:
            await db.talents.update_one(
                {"id": talent_doc["id"]},
                {"$pull": {"media": {"category": {"$in": list(incoming_categories)}}}}
            )

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
    await update_talent_submission_metrics(email)
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
    # List projection. NOTE: the recruiter list cards DO render derived data
    # from media[] (intro/takes/image counts) and form_data (Qs count,
    # location/age for sort), and client-side search/filter/sort operate over
    # the whole list — so only the internal field_visibility toggle map is
    # stripped here. A future lightweight-summary projection (server-computed
    # counts) would require a coordinated card refactor; tracked separately.
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

    if sub.get("decision") == payload.decision:
        return {"ok": True}

    # Resolve talent_id if it is missing/null (fallback matching/creation logic)
    resolved_talent_id = sub.get("talent_id")
    if not resolved_talent_id:
        email = normalize_email(sub.get("talent_email"))
        talent_doc = None
        if email:
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"normalized_email": email},
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
                "normalized_email": email or None,
                "phone": (form.get("phone") or sub.get("talent_phone") or None),
                "age": age_val,
                "dob": (form.get("dob") or None),
                "height": (form.get("height") or None),
                "location": (form.get("location") or None),
                "ethnicity": (form.get("ethnicity") or None),
                "gender": (form.get("gender") or None),
                "instagram_handle": normalize_instagram_handle(form.get("instagram_handle") or None),
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
                "status": "SUBMITTED",
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
                        {"normalized_email": email},
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

    email = (sub.get("talent_email") or "").lower().strip()
    if email:
        await update_talent_submission_metrics(email)

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
    """Retrieve full, individual submission details (admin only).
    
    Augmented to include talent_portfolio_media: media items from the talent's
    global profile (db.talents) with categories 'portfolio' or
    'additional_portfolio'. These are NOT stored on the submission itself —
    they live on the talent record and represent the talent's own portfolio
    library independent of any specific project submission.
    """
    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")

    # Augment response with talent-level portfolio media (read-only, view-only).
    # Portfolio media lives on db.talents, not on the submission document.
    talent_portfolio_media: list = []
    talent_id = sub.get("talent_id")
    talent_email = sub.get("talent_email")

    talent_doc = None
    if talent_id:
        talent_doc = await db.talents.find_one({"id": talent_id}, {"_id": 0, "media": 1})
    if not talent_doc and talent_email:
        norm_email = normalize_email(talent_email)
        talent_doc = await db.talents.find_one(
            {"$or": [
                {"normalized_email": norm_email},
                {"email": norm_email},
                {"source.talent_email": norm_email},
            ]},
            {"_id": 0, "media": 1},
        )

    if talent_doc:
        PORTFOLIO_FETCH_CATEGORIES = {"portfolio", "additional_portfolio", "portfolio_general"}
        # Apply any per-submission visibility overrides so the recruiter's
        # Client/Hidden/Internal choices persist across reloads and drive the
        # in-page client preview. The talent record itself is never mutated.
        tmv = sub.get("talent_media_visibility") or {}
        for m in talent_doc.get("media") or []:
            if m.get("category") in PORTFOLIO_FETCH_CATEGORIES:
                # Ensure public_id items have resolvable URLs before including.
                if m.get("url") or m.get("public_id"):
                    item = dict(m)
                    ov = tmv.get(item.get("id"))
                    if isinstance(ov, dict):
                        if "client_visible" in ov:
                            item["client_visible"] = ov["client_visible"]
                        if "internal_only" in ov:
                            item["internal_only"] = ov["internal_only"]
                    talent_portfolio_media.append(item)

    result = dict(sub)
    result["talent_portfolio_media"] = talent_portfolio_media
    return result


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
            norm_email = normalize_email(email)
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"normalized_email": norm_email},
                    {"email": norm_email},
                    {"source.talent_email": norm_email}
                ]},
                {"age": 1, "dob": 1}
            )
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

    # Per-submission visibility overrides for talent-level portfolio media.
    # Merge so partial updates don't drop prior overrides. No media is copied —
    # only a small id->flags map is stored on the submission.
    if payload.talent_media_visibility is not None:
        current_tmv = sub.get("talent_media_visibility") or {}
        update["talent_media_visibility"] = {**current_tmv, **payload.talent_media_visibility}

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


@router.post("/projects/{pid}/submissions/{sid}/admin-media")
async def admin_add_media(
    pid: str,
    sid: str,
    file: UploadFile = File(...),
    category: str = Form("image"),
    label: Optional[str] = Form(None),
    admin: dict = Depends(current_team_or_admin),
):
    """Admin attaches extra project-specific media to a submission.

    These assets are stored ONLY on the submission document (db.submissions).
    The master talent profile (db.talents) is never modified.
    """
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    data = await file.read()
    size_bytes = len(data)
    ct = (file.content_type or "").lower()
    is_video = ct.startswith("video/") or category in ("intro_video", "take")
    is_pdf = ct == "application/pdf" or category == "pdf"

    if is_pdf:
        rt = "raw"
    elif is_video:
        rt = "video"
    else:
        rt = "image"

    media_id = f"adm_{str(uuid.uuid4())[:8]}"
    folder = f"talentgram/admin_media/{pid}/{sid}"

    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=media_id,
        resource_type=rt,
        content_type=file.content_type,
        keep_original=False,
    )

    media_obj: Dict[str, Any] = {
        "id": media_id,
        "category": category,
        "url": result["url"],
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("bytes") or size_bytes,
        "created_at": _now(),
        "scope": "admin_added",
        "submission_id": sid,
        "project_id": pid,
        "admin_added": True,
        "admin_added_by": admin.get("email"),
        "label": (label or "").strip() or category,
        "client_visible": True,
        "duration": result.get("duration"),
        "poster_url": video_poster_url(result["public_id"]) if is_video else None,
        "thumbnail_url": (
            media_url(result["public_id"], preset="thumb", resource_type=result["resource_type"])
            if rt == "image" else None
        ),
    }

    await db.submissions.update_one({"id": sid}, {"$push": {"media": media_obj}})
    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return fresh_sub


@router.delete("/projects/{pid}/submissions/{sid}/media/{media_id}")
async def admin_remove_media_item(
    pid: str,
    sid: str,
    media_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Admin removes a specific media item from a submission.

    Works for both admin-added assets and original talent-uploaded media.
    The master talent profile (db.talents) is never modified.
    """
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"id": media_id}}})
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
