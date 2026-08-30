"""Public submission flow + admin review."""
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


import time
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile, BackgroundTasks
from pydantic import BaseModel, Field
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
    actor_stamp,
    cloudinary_upload,
    create_or_resume_submission_doc,
    upload_and_track_asset,
    compute_age,
    compute_effective_age,
    current_admin,
    current_team_or_admin,
    db,
    logger,
    decode_submitter,
    make_access_token,
    make_token,
    remove_synced_media_from_global_talent,
    sync_media_to_global_talent,
    media_url,
    video_poster_url,
    video_needs_compat_delivery,
    compat_video_delivery_url,
    update_talent_cover_cache,
    normalize_email,
    verify_email_ownership,
    rate_limit_ok,
    client_ip,
    sign_r2_media_if_needed,
    build_talent_submission_view,
    resolve_canonical_talent,
    build_minimal_talent_from_form,
    REUSABLE_MEDIA_CATEGORIES,
    mark_reusable_media_pending,
    TRUSTED_DEVICE_COOKIE,
    grant_trusted_device,
    resolve_trusted_device,
    revoke_trusted_device,
    set_trusted_device_cookie,
    clear_trusted_device_cookie,
)
from drive_backup import (
    drive_enabled,
    enqueue_drive_upload,
)
from notifications import fanout as notify_fanout

router = APIRouter(prefix="/api", tags=["submissions"])


def has_been_submitted_once(sub: dict) -> bool:
    """True once a submission has been successfully finalized at least once.

    Issue 2 — the Global Talent Profile may only be updated from an ORIGINAL
    submission (and the separate Talent Invite / Profile Update flow). Every
    workflow that touches a submission AFTER its first successful submit is a
    resubmission/edit (resubmit, update, replace media, edit, admin-reopen →
    submit again, …) and must NEVER sync media into the global profile.

    The check is intentionally based on "has this submission ever been
    submitted?" rather than a single status value, so it is robust across all
    current and future edit flows:
      • `submitted_at` is stamped on the first finalize and is NEVER cleared
        afterwards (a monotonic, edit-flow-proof flag); and
      • `status in {submitted, updated}` as a belt-and-suspenders fallback.
    """
    if sub is None:
        return False
    if sub.get("submitted_at"):
        return True
    return sub.get("status") in ("submitted", "updated")


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


def _prefill_video_item(m: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one talent/submission/application media item as a prefill
    intro-video entry. Extracted so `build_prefill_media`'s three-tier
    fallback below doesn't repeat this dict literal three times."""
    return {
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


async def build_prefill_media(talent: Dict[str, Any], email: Optional[str] = None) -> List[Dict[str, Any]]:
    """Canonical prefill-media builder (Media Library Foundation, Phase 4
    item 1). The single source of truth for turning a talent's Global
    Profile media (`talent.media`) into the `prefill_media` list every
    entry point returns — replaces three previously independent, slightly
    divergent implementations that used to live separately in
    `/public/prefill`, `start_submission`, and `routers/auth.py`'s
    `_get_talent_profile_response`.

    Portfolio-type images (indian/western/portfolio→image) come straight
    from `talent.get("media")`, using the more defensive of the two
    previously-diverging filters (checks `resource_type`/`content_type`,
    not just category, so a miscategorized video item can never end up in
    the images list).

    The introduction video is derived ONLY from the talent's own Global
    Profile media (`talents.media`) — never from a prior submission or
    application. Falling back to historical submission/application video
    was a bug (Talent Profile Migration, Phase 1): it could resurface a
    video from Project A when prefilling Project B even after the talent
    replaced their profile video, violating "always the current profile,
    never a historical project." If the profile has no video, prefill
    simply has no video — the talent picks one via the media library
    (Phase 3/4) or uploads fresh.

    `email` is accepted for signature compatibility with existing callers
    but is no longer used by this function.
    """
    prefill_images: List[Dict[str, Any]] = []
    for m in (talent.get("media") or []):
        category = m.get("category")
        if category == "portfolio":
            category = "image"
        resource_type = m.get("resource_type") or "image"
        is_image = resource_type == "image" or (
            category not in {"video", "intro_video"}
            and not (m.get("content_type") or "").startswith("video/")
        )
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

    # Intro video — always and only from the talent's current Global Profile
    # (talents.media). No fallback to db.submissions/db.applications: a
    # historical project's video must never resurface as another project's
    # prefill (Talent Profile Migration, Phase 1 fix).
    latest_intro = None
    for m in (talent.get("media") or []):
        if m.get("category") in {"video", "intro_video"} and m.get("url"):
            latest_intro = _prefill_video_item(m)
            break

    if latest_intro:
        prefill_images.append(latest_intro)

    return deduplicate_media(prefill_images)


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


_PREFILL_TALENT_PROJECTION = {
    "_id": 0, "id": 1, "name": 1, "age": 1, "dob": 1, "height": 1,
    "phone": 1, "location": 1, "ethnicity": 1, "gender": 1, "bio": 1,
    "instagram_handle": 1, "instagram_followers": 1, "work_links": 1,
    "media": 1, "cover_media_id": 1, "skills": 1,
}


async def _build_prefill_response(talent: dict, email: str) -> dict:
    """The one canonical "recognized talent" payload shape — shared by
    `/public/prefill` (portal-token path) and `/public/trusted-device/recognize`
    (cookie path) so both recognition mechanisms feed the frontend identically."""
    name = talent.get("name") or ""
    parts = name.split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    prefill_media = await build_prefill_media(talent, email=email)
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
        "prefill_media": prefill_media,
    }


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
    if not await verify_email_ownership(authorization, email, request):
        # Prevent anonymous PII leak, but allow frontend to prompt verification
        # if the email already exists in the system.
        talent_exists = await db.talents.find_one(
            {"$or": [
                {"normalized_email": email},
                {"email": email},
                {"source.talent_email": email}
            ]},
            {"_id": 1}
        )
        existing_sub = await db.submissions.find_one(
            {"talent_email": email},
            {"_id": 1}
        )
        if talent_exists or existing_sub:
            return {"exists": True}
        return {}

    talent = await db.talents.find_one(
        {"$or": [
            {"normalized_email": email},
            {"email": email},
            {"source.talent_email": email}
        ]},
        _PREFILL_TALENT_PROJECTION,
    )
    if not talent:
        return {}
    return await _build_prefill_response(talent, email)


@router.get("/public/trusted-device/recognize")
async def trusted_device_recognize(request: Request, response: Response):
    """Cookie-based silent recognition for project submission — the trusted-
    device analog of `/public/prefill`, but proves identity from an HttpOnly
    cookie instead of a client-supplied email + Bearer token. No email param:
    the cookie itself is the proof. Rotates the cookie on every successful use.
    """
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many lookups — please slow down")
    raw = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    resolved = await resolve_trusted_device(raw)
    if not resolved:
        raise HTTPException(401, "No trusted device recognized")
    talent = resolved["talent"]
    set_trusted_device_cookie(response, resolved["new_raw_token"])
    email = normalize_email(talent.get("email") or talent.get("normalized_email") or "")
    payload = await _build_prefill_response(talent, email)
    payload["email"] = email
    return payload


@router.post("/public/trusted-device/forget")
async def trusted_device_forget(request: Request, response: Response):
    """"Not you? Sign in as someone else" — revokes exactly the device
    presenting this cookie. Does not touch any other device's trust."""
    raw = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    await revoke_trusted_device(raw)
    clear_trusted_device_cookie(response)
    return {"ok": True}



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
        owns = await verify_email_ownership(authorization, email, request)
        if not owns:
            raise HTTPException(
                403,
                "Please verify your email to continue. We'll send you a one-time code.",
            )

    return await create_or_resume_submission_doc(
        project, email, payload.name, payload.phone, payload.alternate_contact_number,
        payload.form_data, created_from="talent_link",
    )


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
        update.update(actor_stamp(submitter))
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
            label_name = {"image": "Portfolio", "indian": "Indian look", "western": "Western look", "selfie": "Selfie", "profiles": "Profiles", "full_length": "Full Length", "side_profile": "Side Profile", "ethnic": "Ethnic Look", "additional_portfolio": "Additional Portfolio"}.get(category, category)
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
    operation_id = str(uuid.uuid4())
    prev_items = []
    if category in single_slot:
        old_sub = await db.submissions.find_one({"id": sid}, {"media": 1})
        if old_sub and "media" in old_sub:
            prev_items = [m for m in old_sub["media"] if m.get("category") == category]

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

    # Phase 4 (consolidation): was an inline copy of the same lookup
    # _resolve_submission_talent() already implements; zero behavior change.
    tid, tname = await _resolve_submission_talent(sub)

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
        operation_id=operation_id,
    )
    is_video = rt == "video"
    is_image = rt == "image"
    # Cloudinary rearchitecture P4 — serve the uploaded original; the
    # browser-compat exception (one canonical lazy f_mp4) fires only when the
    # stored container/codec can't play natively.
    _delivery_url = result["url"]
    _needs_compat = is_video and video_needs_compat_delivery(result.get("format"), result.get("video_codec"))
    if _needs_compat:
        _c = compat_video_delivery_url(result["public_id"])
        if _c:
            _delivery_url = _c
    _poster = video_poster_url(result["public_id"]) if is_video else None
    media = {
        "id": media_id,
        "category": category,
        "url": _delivery_url,
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
        "thumbnail_url": _poster if is_video else (media_url(result["public_id"], preset="thumb", resource_type=result["resource_type"]) if is_image else None),
        "poster_url": _poster,
        "origin": "project",  # Media Library Foundation (Phase 4 item 1) — freshly uploaded during this submission, not from the Global Profile.
    }
    if _needs_compat:
        media["original_url"] = result["url"]
        media["needs_compat_delivery"] = True
    if category == "take":
        media["label"] = (label or "").strip() or f"Take {existing_takes + 1}"
    # Talent Profile Migration, Phase 4 — a reusable-category upload no
    # longer auto-syncs to the Talent Profile (see the removed sync call
    # below). It's flagged pending until the talent explicitly consents via
    # POST /media-consent. Audition takes are untouched — never flagged.
    mark_reusable_media_pending(media)

    if category in single_slot:
        await db.submissions.update_one(
            {"id": sid},
            {"$pull": {"media": {"category": category}}}
        )

    patch: Dict[str, Any] = {"$push": {"media": media}}
    # Re-upload after finalize flips status back to "updated" and decision → pending
    was_finalized = has_been_submitted_once(sub)
    re_approval = True
    set_patch: Dict[str, Any] = {}
    if was_finalized:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1, "brand_name": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        set_patch["status"] = "updated"
        set_patch["updated_at"] = _now()
        if re_approval:
            set_patch["decision"] = "pending"
    set_patch.update(actor_stamp(submitter))
    if set_patch:
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

    # Talent Profile Migration, Phase 4 — no more auto-mirroring into the
    # global talent record here. A reusable-category item was just flagged
    # `profile_sync_status="pending"` above; it only reaches db.talents.media
    # if the talent explicitly chooses "Update my Talent Profile" via
    # POST /media-consent (or, as a retry safety net, in finalize()'s
    # existing bulk sync pass). Audition takes were never mirrored either
    # way. The old unconditional sync call that used to live here is gone —
    # this endpoint no longer decides whether the profile changes.

    if category in single_slot and prev_items:
        # Defer old asset deletion until database update is verified
        for pi in prev_items:
            from core import safe_cleanup_media_storage, remove_synced_media_from_global_talent
            # Reference-aware (Production Certification, Phase 4 item 4): `pi`
            # may be a prefilled/reused item from the talent's Media Library
            # (build_prefill_media() copies by value — same public_id/
            # stream_uid), so replacing it here must not blindly destroy an
            # asset the Library, or another submission, still depends on.
            await safe_cleanup_media_storage(pi, scope="submission", parent_id=sid, operation_id=operation_id)
            if not was_finalized:
                await remove_synced_media_from_global_talent(sub, pi["id"])

    # Talent Profile Migration, Phase 4 fix: mark_reusable_media_pending()
    # above may have just flagged this upload profile_sync_status="pending",
    # but a raw db.submissions.find_one() response never carries the derived
    # `pending_media_consent` field (only build_talent_submission_view()
    # computes it — see core.py). Without this, the frontend's consent
    # dialog never appears right after upload (applySubmissionResponse()
    # only updates pendingMediaConsent when the key is present), so the
    # talent had no way to answer "only this project" vs "update my
    # profile" until some other GET happened to refresh it. Deliberately NOT
    # routing through the full build_talent_submission_view() here — that
    # also resolves the canonical talent, rebuilds library_media, and
    # queries approved feedback, all needless extra work on the hot upload
    # path for a field derivable from `updated["media"]` alone.
    updated["pending_media_consent"] = [
        m for m in (updated.get("media") or []) if m.get("profile_sync_status") == "pending"
    ]
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
            
    if category == "take" or category in LEGACY_TAKE_CATEGORIES:
        proj = await db.projects.find_one(
            {"id": sub.get("project_id")}, {"_id": 0, "submission_requirements": 1}
        )
        takes_vis = (proj or {}).get("submission_requirements", {}).get("audition_takes_visibility")
        if takes_vis == "hidden":
            raise HTTPException(403, "Audition takes are not enabled for this project")

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

    # Cloudinary rearchitecture P4 — NO eager transformation and NO incoming
    # transformation. The browser uploads the file and Cloudinary stores exactly
    # one canonical asset (the original). Thumbnails/posters are lazy,
    # single-canonical delivery URLs computed at /complete; a browser-compat
    # video transcode is an explicit exception decided at /complete, never
    # requested here. `eager`/`transformation` stay in the response (always
    # null) so the frontend's existing shape is unchanged.
    eager = None
    transformation = None

    import time
    import cloudinary.utils

    timestamp = int(time.time())
    params = {
        "folder": folder,
        "public_id": public_id,
        "timestamp": timestamp,
    }

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
    # P4 — from Cloudinary's upload response, so the backend can decide the
    # browser-compatibility exception without transcoding by default.
    format: Optional[str] = None
    video_codec: Optional[str] = None


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

    # Cloudinary rearchitecture P4 — store the uploaded ORIGINAL. No eager
    # derivative is requested any more, so `payload.eager` is (correctly)
    # empty; `payload.url` is Cloudinary's `secure_url` for the canonical asset.
    thumbnail_url = None
    poster_url = None
    needs_compat_delivery = False

    if is_video:
        url = payload.url  # the original
        # Browser-compatibility EXCEPTION (explicit, gated): only when the
        # uploaded container/codec can't play natively do we point `url` at a
        # single canonical lazy f_mp4 delivery. h264/webm/etc. serve as-is.
        if video_needs_compat_delivery(payload.format, payload.video_codec):
            compat = compat_video_delivery_url(payload.url)
            if compat:
                url = compat
                needs_compat_delivery = True
        poster_url = video_poster_url(payload.url)  # one canonical lazy string, persisted
    else:
        url = payload.url
        thumbnail_url = media_url(payload.public_id, preset="thumb", resource_type="image")

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
        "origin": "project",  # Media Library Foundation (Phase 4 item 1) — freshly uploaded during this submission, not from the Global Profile.
    }
    if needs_compat_delivery:
        media["original_url"] = payload.url
        media["needs_compat_delivery"] = True

    existing_takes = 0
    if category == "take":
        existing_takes = sum(
            1 for m in sub.get("media", [])
            if m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        )
        media["label"] = (payload.label or "").strip() or f"Take {existing_takes + 1}"
    # Talent Profile Migration, Phase 4 — see submission_upload() for the
    # full rationale; flagged pending instead of auto-syncing below.
    mark_reusable_media_pending(media)

    # Phase 4 (consolidation): was an inline copy of the same lookup
    # _resolve_submission_talent() already implements; zero behavior change.
    tid, tname = await _resolve_submission_talent(sub)

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
    was_finalized = has_been_submitted_once(sub)
    re_approval = True
    set_patch: Dict[str, Any] = {}
    if was_finalized:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1, "brand_name": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        set_patch["status"] = "updated"
        set_patch["updated_at"] = _now()
        if re_approval:
            set_patch["decision"] = "pending"
    set_patch.update(actor_stamp(submitter))
    if set_patch:
        patch["$set"] = set_patch

    await db.submissions.update_one({"id": sid}, patch)
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    # Talent Profile Migration, Phase 4 — no auto-sync here anymore; see
    # submission_upload() for the full rationale.

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

    # Same enrichment fix as submission_upload() above — this direct-upload
    # completion path can also flag profile_sync_status="pending" via
    # mark_reusable_media_pending(), and a raw find_one() response never
    # carries the derived pending_media_consent field the consent dialog
    # reads from. Same reasoning for not using build_talent_submission_view()
    # here either — see the comment at its other call site.
    updated["pending_media_consent"] = [
        m for m in (updated.get("media") or []) if m.get("profile_sync_status") == "pending"
    ]
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
        {"$set": {"media.$.label": new_label, **actor_stamp(submitter)}},
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
    target_media = next((m for m in (sub.get("media") or []) if m.get("id") == mid), None)
    already_submitted = has_been_submitted_once(sub)
    patch: Dict[str, Any] = {"$pull": {"media": {"id": mid}}}
    set_patch: Dict[str, Any] = {}
    if already_submitted:
        set_patch["status"] = "updated"
        set_patch["decision"] = "pending"
        set_patch["updated_at"] = _now()
    set_patch.update(actor_stamp(submitter))
    if set_patch:
        patch["$set"] = set_patch
    await db.submissions.update_one({"id": sid}, patch)
    # Phase 3 v37i — keep the global talent profile in sync, but ONLY while the
    # submission is still ORIGINAL. Deleting media from an already-submitted
    # submission is a resubmission/edit and must NOT mutate the global profile
    # (Issue 2). The submission's own media is still pulled above.
    if not already_submitted:
        await remove_synced_media_from_global_talent(sub, mid)
    # Parity sprint: best-effort delete the backing storage (Stream/R2/Cloudinary)
    # + tracking row so deletes don't leave orphans. Never fails the user action.
    # Reference-aware (Production Certification, Phase 4 item 4): `target_media`
    # may be a prefilled/reused item from the talent's Media Library — removing
    # it from THIS submission must not destroy an asset the Library, or another
    # submission, still depends on.
    if target_media:
        from core import safe_cleanup_media_storage
        await safe_cleanup_media_storage(target_media, scope="submission", parent_id=sid)
    return {"ok": True}


class MediaFromLibraryIn(BaseModel):
    talent_media_id: str


@router.post("/public/submissions/{sid}/media/from-library")
async def submission_add_media_from_library(
    sid: str, payload: MediaFromLibraryIn, authorization: Optional[str] = Header(None)
):
    """Talent Profile Migration, Phase 3 — attach an existing reusable Talent
    Profile media item to THIS submission by reference. No upload, no new
    Cloudinary/Stream asset: the new submission media item shares the exact
    same `public_id`/`url`/`resource_type` as the source, and carries
    `source_talent_media_id` back to it (a live pointer, reconciled on every
    resume — see `build_talent_submission_view`). Mirrors the single-slot and
    per-category-cap rules `submission_upload` already enforces for real
    uploads, so a talent can't use the picker to bypass them.
    """
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    talent = await resolve_canonical_talent(email=sub.get("talent_email"))
    if not talent:
        raise HTTPException(404, "No Talent Profile found for this submission")

    # Ownership check: build_prefill_media() only ever returns items from
    # THIS resolved talent's own `media[]` — an id that isn't in that list
    # either belongs to someone else or doesn't exist. Reusing the same
    # canonical builder every other prefill path uses means there is no
    # second, divergent notion of "what's in my library" to keep in sync.
    library_media = await build_prefill_media(talent, email=talent.get("email"))
    lib_item = next((m for m in library_media if m.get("id") == payload.talent_media_id), None)
    if not lib_item:
        raise HTTPException(404, "Media not found in your Talent Profile")

    category = lib_item.get("category")
    single_slot = {"intro_video", "take_1", "take_2", "take_3"}

    if category in PORTFOLIO_IMAGE_CATEGORIES:
        existing = sum(1 for m in (sub.get("media") or []) if m.get("category") == category)
        if existing >= MAX_IMAGES_PER_CATEGORY:
            label_name = {"image": "Portfolio", "indian": "Indian look", "western": "Western look", "selfie": "Selfie", "profiles": "Profiles", "full_length": "Full Length", "side_profile": "Side Profile", "ethnic": "Ethnic Look", "additional_portfolio": "Additional Portfolio"}.get(category, category)
            raise HTTPException(400, f"{label_name} image limit reached ({MAX_IMAGES_PER_CATEGORY})")

    old_slot_items = [m for m in (sub.get("media") or []) if m.get("category") == category] if category in single_slot else []

    new_item = {k: v for k, v in lib_item.items() if k != "id"}
    new_item.update({
        "id": str(uuid.uuid4()),
        "source_talent_media_id": lib_item["id"],
        "origin": "global",
        "created_at": _now(),
    })

    was_finalized = has_been_submitted_once(sub)
    push_patch: Dict[str, Any] = {"$push": {"media": new_item}}
    set_patch: Dict[str, Any] = {}
    if was_finalized:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        set_patch["status"] = "updated"
        set_patch["updated_at"] = _now()
        if re_approval:
            set_patch["decision"] = "pending"
    set_patch.update(actor_stamp(submitter))
    if set_patch:
        push_patch["$set"] = set_patch

    # Atomically guarded push: the filter only matches if no item with this
    # source_talent_media_id exists RIGHT NOW. A plain read-then-decide
    # check (read `sub`, decide, then write) has a window where two
    # concurrent requests (two browser tabs, a double-fire) both see "not
    # yet selected" and both push — this closes that race at the database
    # level instead. If another concurrent request already added it,
    # matched_count is 0 and this is a no-op: re-fetch and return the
    # (already correct) current state rather than duplicating.
    result = await db.submissions.update_one(
        {"id": sid, "media.source_talent_media_id": {"$ne": lib_item["id"]}},
        push_patch,
    )
    if result.matched_count == 0:
        fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
        return await build_talent_submission_view(fresh_sub)

    # Item copied FROM talents.media already exists there by construction —
    # mirroring it back via sync_media_to_global_talent() would be a
    # guaranteed no-op (its own dedup check matches on the shared
    # public_id), so it's deliberately not called here.

    if category in single_slot and old_slot_items:
        # Now safe to evict whatever else was occupying the single slot —
        # excluded by the NEW item's own (freshly generated) id, so this can
        # never remove what was just pushed above regardless of timing.
        await db.submissions.update_one(
            {"id": sid},
            {"$pull": {"media": {"category": category, "id": {"$ne": new_item["id"]}}}},
        )
        from core import safe_cleanup_media_storage
        for pi in old_slot_items:
            # Reference-aware: `pi` may itself be a Library reference (no
            # physical delete needed) or a real upload (safe to clean up
            # once nothing else points at it) — safe_cleanup_media_storage
            # checks is_media_asset_referenced() either way.
            await safe_cleanup_media_storage(pi, scope="submission", parent_id=sid)
            if not was_finalized:
                await remove_synced_media_from_global_talent(sub, pi["id"])

    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return await build_talent_submission_view(fresh_sub)


MEDIA_CONSENT_DECISIONS = {"only_this_project", "update_profile"}


async def apply_media_consent_decision(sub: dict, decision: str, media_ids: Optional[List[str]] = None) -> int:
    """Talent Profile Migration, Phase 4 — the single place a consent
    decision is ever applied. Every reusable-category upload, regardless of
    which of the four construction sites created it (submission_upload,
    submission_upload_complete, attach_video_media, video_complete's R2
    branch), lands here.

    `media_ids=None` (default, used by the talent flow — UNCHANGED): every
    currently-`profile_sync_status="pending"` item on this submission is
    resolved by ONE decision in one call — a batch of 5 uploads is one
    decision, not five, satisfying "the dialog must appear only once per
    submission session, aggregate them".

    `media_ids=[...]` (Admin Mode's per-item "Save to Master Profile"
    checkbox, Phase 2): only the pending items whose id is in the list are
    resolved — lets one batch of uploads have some items promoted to the
    Talent Profile and others left project-only, instead of one blanket
    choice for the whole batch. Still the exact same resolution logic below,
    just scoped to a subset.

    "update_profile": syncs each pending item into db.talents.media via the
    exact same sync_media_to_global_talent() every other path already used
    — no duplicated sync logic. Preserves the pre-existing "retest never
    touches the global profile" rule (Issue 2) that every other upload path
    already enforces: if this submission was already finalized once, the
    decision is still recorded, but the actual sync is skipped, exactly
    matching how a plain re-upload during a retest already behaves today.

    "only_this_project": marks them resolved without ever touching
    db.talents — nothing syncs, nothing changes on the profile.

    Returns the number of items resolved (0 if nothing was pending, e.g. a
    stale/duplicate client call after the talent already answered).
    """
    media = sub.get("media") or []
    pending = [m for m in media if m.get("profile_sync_status") == "pending"]
    if media_ids is not None:
        wanted = set(media_ids)
        pending = [m for m in pending if m.get("id") in wanted]
    if not pending:
        return 0
    pending_ids = [m["id"] for m in pending]

    if decision == "update_profile" and not has_been_submitted_once(sub):
        for m in pending:
            await sync_media_to_global_talent(sub, m, skip_cover_cache=True)
        talent = await resolve_canonical_talent(email=sub.get("talent_email"))
        if talent:
            await update_talent_cover_cache(talent["id"])
        new_status = "synced"
    else:
        # Either an explicit "only this project" choice, or "update_profile"
        # requested during a retest (Issue 2 — never honored, same as every
        # other upload path). Either way: no sync, just mark resolved.
        new_status = "declined"

    # Phase 5 (consent-decision atomicity): targeted array-filter update on
    # only the items just resolved, instead of a whole-array read-modify-write
    # $set. Matches the atomic-per-item pattern every other mutator in this
    # file already uses ($push/$pull) — closes the race window where a
    # concurrent write to submission.media (e.g. a new upload landing between
    # this function's read and its write) could be silently lost.
    await db.submissions.update_one(
        {"id": sub["id"]},
        {"$set": {"media.$[elem].profile_sync_status": new_status}},
        array_filters=[{"elem.id": {"$in": pending_ids}}],
    )
    return len(pending)


class MediaConsentIn(BaseModel):
    decision: str
    # Phase 2 — Admin Mode's per-item "Save to Master Profile" checkbox.
    # None (default) preserves the exact existing talent-flow behavior
    # (resolve every pending item). A list scopes resolution to just those
    # media ids — see apply_media_consent_decision()'s docstring.
    media_ids: Optional[List[str]] = None


@router.post("/public/submissions/{sid}/media-consent")
async def submission_media_consent(
    sid: str, payload: MediaConsentIn, authorization: Optional[str] = Header(None)
):
    """Talent Profile Migration, Phase 4 — the talent's answer to "how would
    you like to use this media?" for every currently-pending reusable
    upload on this submission. See apply_media_consent_decision() for what
    actually happens; this endpoint is just the auth + validation wrapper.
    """
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    if payload.decision not in MEDIA_CONSENT_DECISIONS:
        raise HTTPException(400, "Invalid decision")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    await apply_media_consent_decision(sub, payload.decision, media_ids=payload.media_ids)

    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return await build_talent_submission_view(fresh_sub)


# ==========================================================================
# Architecture C — direct browser→Cloudinary audition-video upload
# (feature-flagged; images & all other flows unchanged)
# ==========================================================================

_DIRECT_VIDEO_CATS = {"intro_video", "take", "take_1", "take_2", "take_3"}


class VideoSignatureIn(BaseModel):
    category: str
    label: Optional[str] = None
    content_type: Optional[str] = None
    # Re-sign an in-progress chunked upload: pass the public_id from the first
    # signature so a fresh timestamp/signature targets the SAME asset (prevents
    # stale-signature failures on multi-hour uploads). Validated server-side.
    public_id: Optional[str] = None


class VideoCompleteIn(BaseModel):
    public_id: str
    secure_url: Optional[str] = None
    url: Optional[str] = None
    resource_type: Optional[str] = "video"
    bytes: Optional[int] = 0
    duration: Optional[float] = None
    format: Optional[str] = None
    label: Optional[str] = None


async def _resolve_submission_talent(sub: dict):
    """Resolve (talent_id, talent_name) for a submission — same logic as the
    Railway upload path so the Cloudinary folder is identical.

    Phase 4 (Canonical Architecture Redesign, consolidation): the lookup
    below used to inline its own copy of the three-clause canonical $or;
    now calls the single shared resolve_canonical_talent() (core.py) —
    identical query, identical no-projection full-document return shape,
    zero behavior change."""
    tid = sub.get("talent_id")
    tname = sub.get("talent_name")
    if not tid:
        norm_email = normalize_email(sub.get("talent_email"))
        if norm_email:
            t = await resolve_canonical_talent(email=norm_email)
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


async def attach_video_media(sub: dict, asset: dict, category: str, label: Optional[str] = None, submitter: Optional[dict] = None) -> Optional[dict]:
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
    # Cloudinary rearchitecture P4 — serve the uploaded original. The
    # browser-compat exception (one canonical lazy f_mp4) fires only when the
    # stored container/codec can't play natively.
    delivery_url = secure
    needs_compat = video_needs_compat_delivery(
        asset.get("format"), (asset.get("video") or {}).get("codec"),
    )
    if needs_compat:
        compat = compat_video_delivery_url(public_id)
        if compat:
            delivery_url = compat
    poster = video_poster_url(public_id)
    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "url": delivery_url,
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
        "thumbnail_url": poster,
        "poster_url": poster,
        "source": "direct_upload",
        "origin": "project",  # Media Library Foundation (Phase 4 item 1) — freshly uploaded during this submission, not from the Global Profile.
    }
    if needs_compat:
        media["original_url"] = secure
        media["needs_compat_delivery"] = True
    if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
        media["label"] = (label or "").strip() or "Take"
    # Talent Profile Migration, Phase 4 — this helper has never called
    # sync_media_to_global_talent(); it relied entirely on finalize()'s bulk
    # catch-all to ever reach db.talents.media. Under the consent model that
    # catch-all now only applies once the talent has explicitly chosen
    # "Update my Talent Profile" — flag intro_video here too so this upload
    # path can't silently bypass consent just because it never had a sync
    # call of its own.
    mark_reusable_media_pending(media)

    # Single-slot replacement for intro_video (cannot mix $pull and $push on the
    # same field in one update, so pull first).
    if category == "intro_video":
        await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"category": "intro_video"}}})

    push: Dict[str, Any] = {"$push": {"media": media}}
    fresh = await db.submissions.find_one({"id": sid})
    set_patch: Dict[str, Any] = {}
    if fresh and fresh.get("status") in ("submitted", "updated"):
        proj = await db.projects.find_one(
            {"id": sub.get("project_id")}, {"_id": 0, "require_reapproval_on_edit": 1}
        )
        set_patch["status"] = "updated"
        set_patch["updated_at"] = _now()
        if bool((proj or {}).get("require_reapproval_on_edit", True)):
            set_patch["decision"] = "pending"
    set_patch.update(actor_stamp(submitter))
    if set_patch:
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

    # Server-side enforcement of the project's audition-takes visibility —
    # the frontend already hides the whole takes UI when this is "hidden",
    # but that's a UI convenience only; without this check any client that
    # still sends a sign request (a stale tab loaded before an admin
    # tightened the setting, a direct API call, etc.) gets a valid signed
    # upload regardless of project config.
    if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
        proj = await db.projects.find_one(
            {"id": sub.get("project_id")}, {"_id": 0, "submission_requirements": 1}
        )
        takes_vis = (proj or {}).get("submission_requirements", {}).get("audition_takes_visibility")
        if takes_vis == "hidden":
            raise HTTPException(403, "Audition takes are not enabled for this project")

    tid, tname = await _resolve_submission_talent(sub)
    folder = audition_submission_folder(tid, tname, sub.get("project_id"), sid)

    is_resign = bool(payload.public_id)
    if is_resign:
        # Re-sign the SAME in-progress target. The public_id is a leaf
        # (e.g. "intro_video" or "take_abcd1234"); validate it matches
        # the expected format so a token can never sign arbitrary IDs.
        import re as _re
        if not _re.fullmatch(r"intro_video|take_[0-9a-f]{8}", payload.public_id):
            raise HTTPException(400, "Invalid public_id for this submission")
        public_id = payload.public_id
    else:
        # First signature for a new upload: enforce the take limit (a re-sign is
        # a continuation of an existing in-flight upload, not a new take).
        if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
            existing_takes = sum(
                1 for m in (sub.get("media") or [])
                if m.get("category") in ("take",) or m.get("category") in LEGACY_TAKE_CATEGORIES
            )
            if existing_takes >= MAX_SUBMISSION_TAKES:
                raise HTTPException(400, f"Maximum {MAX_SUBMISSION_TAKES} takes reached — delete one to add another")
        public_id = "intro_video" if category == "intro_video" else f"take_{uuid.uuid4().hex[:8]}"

    # Cloudinary rearchitecture P4 — NO eager transformation. The browser
    # uploads the audition video and Cloudinary stores exactly one canonical
    # asset (the original). No 720p transcode, no 4K processing, no poster
    # generated at upload. The poster is a lazy single-canonical URL persisted
    # at /video-complete; a browser-compat transcode, if ever needed, is an
    # explicit exception decided there.
    tags = f"submission_id={sid},project_id={sub.get('project_id')},talent_id={tid},category={category},asset_kind=audition_video"
    import urllib.parse
    safe_label = urllib.parse.quote((payload.label or "").strip())
    context = f"category={category}|label={safe_label}"
    timestamp = int(time.time())
    params_to_sign = {
        "timestamp": timestamp,
        "folder": folder,
        "public_id": public_id,
        "overwrite": "true",
        "tags": tags,
        "context": context,
    }
    cfg = cloudinary.config()
    signature = cloudinary.utils.api_sign_request(params_to_sign, cfg.api_secret)

    try:
        full_public_id = f"{folder}/{public_id}"
        await db.asset_metadata.update_one(
            {"public_id": full_public_id},
            {"$set": {
                "public_id": full_public_id, "submission_id": sid, "talent_id": tid,
                "project_id": sub.get("project_id"), "category": category,
                "asset_type": "intro_video" if category == "intro_video" else "audition_video",
                "resource_type": "video", "upload_status": "pending",
                "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"video-signature: pending asset_metadata write failed {folder}/{public_id}: {e}")

    return {
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key": cfg.api_key,
        "timestamp": timestamp,
        "signature": signature,
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload",
        "params": {
            "folder": folder, "public_id": public_id,
            "overwrite": "true", "tags": tags,
            "context": context,
        },
        "max_duration_seconds": MAX_AUDITION_VIDEO_SECONDS,
    }


@router.post("/public/submissions/{sid}/video-complete")
async def video_complete(
    sid: str,
    payload: VideoCompleteIn,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None),
):
    """Optimistic fast-path: attach a just-uploaded direct video. Category and
    folder are validated server-side; finalize reconciliation is the safety net
    if this never fires."""
    from core import generate_r2_presigned_url, trigger_cloudinary_transcode
    from datetime import datetime, timezone
    submitter = await decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    tid, tname = await _resolve_submission_talent(sub)
    folder = audition_submission_folder(tid, tname, sub.get("project_id"), sid)
    public_id = (payload.public_id or "").strip()

    is_r2 = public_id.startswith("raw-uploads/")
    if is_r2:
        if not public_id.startswith(f"raw-uploads/submissions/{sid}/"):
            raise HTTPException(400, "Asset does not belong to this submission")

        # Hardening: only accept a public_id that a genuine video-signature
        # call actually issued (it upserts this exact asset_metadata row at
        # signature time). Without this, any caller holding a valid
        # submitter token for this sid could fabricate an R2-shaped
        # public_id that was never signed or uploaded, registering a
        # phantom "processing" media row and enqueueing a real Cloudflare
        # Stream transcode job for an object that doesn't exist. New
        # video-signature calls can never mint a fresh R2 key anymore
        # (Cloudinary-only), so this can only ever match a session that was
        # legitimately issued before that change.
        existing_meta = await db.asset_metadata.find_one({"public_id": public_id})
        if not existing_meta or existing_meta.get("upload_status") not in ("pending", "processing"):
            raise HTTPException(400, "No matching upload session for this asset")

        parts = public_id.split("/")
        category = parts[3]
        leaf_pid = parts[4].split(".")[0]

        r2_read_url = generate_r2_presigned_url(public_id, "GET", expiry=86400)

        # Register the media asset in Mongo with processing status
        media = {
            "id": str(uuid.uuid4()),
            "category": category,
            "url": None,  # Transcoding in progress
            "public_id": f"{folder}/{leaf_pid}",
            "resource_type": "video",
            "content_type": "video/mp4",
            "original_filename": None,
            "size": payload.bytes or 0,
            "created_at": _now(),
            "scope": "submission",
            "submission_id": sid,
            "project_id": sub.get("project_id"),
            "duration": None,
            "thumbnail_url": None,
            "poster_url": None,
            "status": "processing",
            "source": "direct_upload",
            "origin": "project",  # Media Library Foundation (Phase 4 item 1) — freshly uploaded during this submission, not from the Global Profile.
        }
        if category in ("take",) or category in LEGACY_TAKE_CATEGORIES:
            media["label"] = (payload.label or "").strip() or "Take"
        # Talent Profile Migration, Phase 4 — same as attach_video_media():
        # this R2/Cloudflare-Stream registration path has never called
        # sync_media_to_global_talent() either; flag it pending so consent
        # is still required regardless of which upload path created it.
        mark_reusable_media_pending(media)

        # Single-slot replacement for intro_video: Defer physical deletion until transcode webhook completes
        operation_id = str(uuid.uuid4())
        if category == "intro_video":
            await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"category": "intro_video"}}})

        push_patch: Dict[str, Any] = {"$push": {"media": media}}
        stamp = actor_stamp(submitter)
        if stamp:
            push_patch["$set"] = stamp
        await db.submissions.update_one({"id": sid}, push_patch)

        # Update metadata state to "processing"
        try:
            await db.asset_metadata.update_one(
                {"public_id": public_id},
                {"$set": {"upload_status": "processing", "updated_at": datetime.now(timezone.utc), "operation_id": operation_id}},
            )
        except Exception as e:
            logger.warning(f"video-complete R2: asset_metadata update failed: {e}")

        # Enqueue Cloudinary fetch and transcode job in background
        background_tasks.add_task(
            trigger_cloudinary_transcode,
            media_id=media["id"],
            r2_url=r2_read_url,
            folder=folder,
            public_id=leaf_pid,
            # Cloudinary rearchitecture P4 — no eager transcode. (This branch is
            # the retired R2→Cloudinary pipeline, reachable only by a pre-
            # retirement upload session; kept inert.)
            eager_transformation=None,
            scope="submission",
            parent_id=sid,
            category=category,
            label=payload.label if category == "take" else None,
            operation_id=operation_id,
        )

        return {"ok": True, "media": media}

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
        # P6 (media lifecycle): route the reject-on-upload cleanup through the
        # gate. `just_uploaded_reject` — the asset was created seconds ago in
        # this same request and nothing can reference it — so the gate clears an
        # immediate physical destroy for this brand-new orphan.
        try:
            from media_lifecycle import delete_if_safe, DeletionContext
            await delete_if_safe(
                db,
                {"public_id": public_id, "resource_type": "video", "category": category or "take",
                 "ownership": {"owner_type": "project_submission", "owner_id": sub.get("id"),
                               "submission_id": sub.get("id"), "project_id": sub.get("project_id")}},
                ctx=DeletionContext(just_uploaded_reject=True),
                destroyer=lambda m: cloudinary.uploader.destroy(m["public_id"], resource_type="video"),
            )
        except Exception:
            pass
        raise HTTPException(400, f"Audition video must be {MAX_AUDITION_VIDEO_SECONDS // 60} minutes or less.")

    media = await attach_video_media(sub, asset, category, submitter=submitter)
    return {"ok": True, "media": media}


async def _enqueue_internal_whatsapp_notification_task(submission: dict, event_type: str, decision: Optional[str] = None):
    try:
        project_id = submission.get("project_id")
        project = await db.projects.find_one({"id": project_id})
        project_name = "Unknown Project"
        if project:
            # Projects store the display name in `brand_name` (the field used
            # everywhere else). `title`/`name` don't exist on the document, so the
            # old lookup always fell through to "Unknown Project".
            project_name = project.get("brand_name") or project.get("title") or project.get("name") or "Unknown Project"

        form = submission.get("form_data") or {}
        talent_name = (
            f"{(form.get('first_name') or '').strip()} "
            f"{(form.get('last_name') or '').strip()}"
        ).strip() or submission.get("talent_name") or "A talent"
        talent_phone = form.get("phone") or submission.get("talent_phone") or "No Phone"

        import urllib.parse
        quoted_talent_name = urllib.parse.quote(talent_name)
        review_link = f"https://review.talentgramagency.com/admin/projects/{project_id}/submissions?search={quoted_talent_name}"
        timestamp = _now()

        # Clean, scannable notifications — no asset counts / internal details.
        if event_type == "SUBMISSION UPDATED":
            message_body = (
                f"🔄 *SUBMISSION UPDATED*\n\n"
                f"Talent:\n{talent_name}\n\n"
                f"Project:\n{project_name}\n\n"
                f"Review:\n{review_link}\n\n"
                f"Updated:\n{timestamp}"
            )
        elif event_type == "DECISION CHANGED":
            # Preserved unchanged (no active trigger — decision changes are
            # intentionally silent — kept to avoid altering unrelated behavior).
            decision_str = str(decision).upper() if decision else "UNKNOWN"
            message_body = (
                f"*SUBMISSION DECISION CHANGED*\n"
                f"Project: {project_name}\n"
                f"Talent: {talent_name} ({talent_phone})\n"
                f"Decision: {decision_str}\n"
                f"Review Link: {review_link}\n"
                f"Timestamp: {timestamp}"
            )
        else:  # NEW SUBMISSION (default)
            message_body = (
                f"🆕 *NEW SUBMISSION RECEIVED*\n\n"
                f"Talent:\n{talent_name}\n\n"
                f"Project:\n{project_name}\n\n"
                f"Review:\n{review_link}\n\n"
                f"Submitted:\n{timestamp}"
            )

        # Config setting lookup
        cfg = await db.whatsapp_config.find_one({"key": "internal_notification_group_name"})
        group_name = cfg.get("value") if (cfg and cfg.get("value")) else "Talentgram Operations"

        batch_id = str(uuid.uuid4())
        batch_doc = {
            "id": batch_id,
            "source_type": "INTERNAL_NOTIFICATION",
            "source_label": f"Internal Notification for {project_name}",
            "project_id": project_id,
            "project_name": project_name,
            "template_id": "internal_notification",
            "template_slug": "internal_notification",
            "variable_data": {},
            "media_url": None,
            "is_dry_run": False,
            "status": "pending",
            "total_jobs": 1,
            "sent_count": 0,
            "failed_count": 0,
            "unconfirmed_count": 0,
            "created_by": "system",
            "created_at": timestamp,
            "started_at": None,
            "completed_at": None,
        }

        job_doc = {
            "id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "template_id": "internal_notification",
            "template_name": "Internal Notification",
            "source": "INTERNAL_NOTIFICATION",
            "source_id": project_id,
            "recipient_kind": "INTERNAL_GROUP",
            "recipient_id": "internal_notification_group",
            "talent_id": None,
            "talent_name": group_name,
            "destination_type": "group",
            "destination": group_name,
            "message_body": message_body,
            "media_url": None,
            "is_dry_run": False,
            "status": "pending",
            "attempt_count": 0,
            "last_attempted_at": None,
            "sent_at": None,
            "error_message": None,
            "worker_picked_at": None,
            "created_at": timestamp,
        }

        await db.whatsapp_batches.insert_one(batch_doc)
        await db.whatsapp_jobs.insert_one(job_doc)
        logger.info(f"Successfully enqueued internal WhatsApp notification for {event_type}")

    except Exception as e:
        logger.warning(f"Error in background internal WhatsApp notification task: {e}", exc_info=True)


def enqueue_internal_whatsapp_notification(submission: dict, event_type: str, decision: Optional[str] = None):
    try:
        import asyncio
        asyncio.create_task(_enqueue_internal_whatsapp_notification_task(submission, event_type, decision))
    except Exception as e:
        logger.warning(f"Failed to schedule internal WhatsApp notification task: {e}", exc_info=True)


@router.post("/public/submissions/{sid}/finalize")
async def submission_finalize(sid: str, response: Response, authorization: Optional[str] = Header(None)):
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

    # Talent Profile Migration, Phase 3: the immutable snapshot about to be
    # frozen must reflect the current Talent Profile for anything still
    # library-derived, and `removed_from_profile` must be accurate at the
    # moment of freeze — not whatever it happened to say when the talent
    # last opened the page.
    from core import reconcile_submission_media
    finalize_talent = await resolve_canonical_talent(email=sub.get("talent_email"))
    if finalize_talent:
        finalize_library_media = await build_prefill_media(finalize_talent, email=finalize_talent.get("email"))
        if await reconcile_submission_media(sub, finalize_library_media):
            sub = await db.submissions.find_one({"id": sid})

    # Talent Profile Migration, Phase 4: a submission cannot be finalized
    # while a reusable upload's fate ("only this project" vs "update my
    # Talent Profile") is still unanswered — the talent must resolve every
    # pending item via POST /media-consent first. This is the deterministic
    # gate: nothing can slip through un-consented just because the talent
    # never got to (or dismissed) the dialog.
    if any(m.get("profile_sync_status") == "pending" for m in (sub.get("media") or [])):
        raise HTTPException(
            400,
            "Please choose how to use your new photo/video uploads before submitting.",
        )

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
        if fields_config.get("competitive_brand") == "required":
            has_brand_exp = form.get("has_competitive_brand_experience")
            # An explicit NONE (false) is itself a complete, valid answer —
            # the free-text field only needs to be non-empty when the
            # talent answered YES (true). Unanswered (anything else) still
            # blocks, same as before.
            if has_brand_exp is True:
                if not (form.get("competitive_brand") or "").strip():
                    raise HTTPException(400, "Competitive Brand is required")
            elif has_brand_exp is not False:
                raise HTTPException(400, "Competitive Brand is required")

        if fields_config.get("availability") == "required":
            avail = form.get("availability") or {}
            if isinstance(avail, str):
                avail = {"status": "yes" if avail else "", "note": avail}
            status = (avail.get("status") or "").strip()
            # "partial" (simplified-wizard UX, 2026-08): available on some but
            # not all shoot dates. Same note-required contract as "no", just a
            # different question ("which days" vs. "why not / alternates").
            if status not in {"yes", "partial", "no"}:
                raise HTTPException(400, "Please confirm your availability")
            if status in ("partial", "no") and not (avail.get("note") or "").strip():
                raise HTTPException(400, "Please share your availability details")

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

        # 3. Media — intentionally NOT enforced. Project submission no longer
        # collects or requires media (intro video / audition takes /
        # portfolio images); portfolio management lives exclusively in the
        # talent dashboard now. Requirement config (intro_video,
        # audition_takes_visibility, portfolio.*) is left in the project
        # schema untouched — it's informational only at this point, not a
        # finalize gate.

        # 4. Work Links
        min_links = int(requirements.get("min_work_links") or 0)
        links_vis = requirements.get("work_links_visibility")
        if not links_vis:
            links_vis = "required" if min_links > 0 else "optional"
        if links_vis == "required":
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

        # 6. Conditional Rules — also media-only (a conditional video
        # requirement), so also intentionally not enforced; see note at "3.
        # Media" above.
    else:
        # Fallback legacy validation rules
        for field in ("first_name", "last_name", "height"):
            if not (form.get(field) or "").strip():
                raise HTTPException(400, f"{field.replace('_',' ').title()} is required")
        # location may be a structured array (post-2026-06-12 migration) or a
        # legacy string — guard both shapes. Calling .strip() on a list raised
        # AttributeError → unhandled 500 on every legacy-validation finalize.
        loc_val = form.get("location")
        if not loc_val or (isinstance(loc_val, str) and not loc_val.strip()):
            raise HTTPException(400, "Location is required")
        avail = form.get("availability") or {}
        if isinstance(avail, str):
            avail = {"status": "yes" if avail else "", "note": avail}
        status = (avail.get("status") or "").strip()
        if status not in {"yes", "partial", "no"}:
            raise HTTPException(400, "Please confirm your availability")
        if status in ("partial", "no") and not (avail.get("note") or "").strip():
            raise HTTPException(400, "Please share your availability details")
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

    # First-time finalize vs retest finalize. Based on "has this submission
    # ever been submitted?" so EVERY post-first-submit workflow (resubmit,
    # update, edit, admin-reopen → submit again, …) is treated as a retest.
    is_retest = has_been_submitted_once(sub)
    new_status = "updated" if is_retest else "submitted"
    patch: Dict[str, Any] = {
        "status": new_status,
        "submitted_at": sub.get("submitted_at") or _now(),
    }
    stamp = actor_stamp(submitter)
    if stamp:
        patch.update(stamp)
        # Admin Mode "Upload on Behalf" — record who actually clicked finalize,
        # not just who last touched the draft. Talent-driven finalizes leave
        # this unset (absence = talent-submitted, per the audit-trail schema).
        patch["submitted_by"] = stamp["last_modified_by"]
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

    if is_retest:
        # Resubmission / edit finalize (status was already submitted/updated).
        # NEVER overwrite the global Talent Profile or its media from a
        # resubmission — those are project-specific corrections. Only an
        # ORIGINAL first-time submission (and the separate Talent Invite /
        # Profile Update flow) may update the master profile. See Issue 2.
        pass
    elif talent_doc:
        from core import merge_talent_profile
        # Merge fields (Task 4 & 6)
        form_to_merge = dict(form)
        form_to_merge["email"] = email
        form_to_merge["normalized_email"] = email
        if "phone" not in form_to_merge or not form_to_merge["phone"]:
            form_to_merge["phone"] = sub.get("talent_phone")
        
        # Exception: Project-specific overrides for location must remain separate
        form_to_merge.pop("location", None)

        await merge_talent_profile(
            talent_doc, form_to_merge, "project_submission",
            snapshot_at=sub.get("talent_profile_snapshot_at"),
        )
        await update_talent_cover_cache(talent_doc["id"])
    else:
        # Build a minimal talent record from the submission's form_data.
        new_talent = build_minimal_talent_from_form(
            form,
            email=email,
            talent_name=sub.get("talent_name"),
            talent_phone=sub.get("talent_phone"),
            alternate_contact_number=sub.get("alternate_contact_number"),
            reference_id=sid,
            notes=f"Auto-created from audition submission for project {sub.get('project_id')}",
            created_by="auto-audition",
            include_skills=True,
            include_updated_at=True,
        )
        from core import insert_talent_or_recover
        talent_doc, recovered = await insert_talent_or_recover(
            new_talent, email=email, context=f"submission_finalize sid={sid}",
        )
        if recovered and talent_doc:
            # Race: another submission for the same email finalised in
            # parallel — merge into the winner, same as before.
            from core import merge_talent_profile
            form_to_merge = dict(form)
            form_to_merge["email"] = email
            form_to_merge["normalized_email"] = email
            if "phone" not in form_to_merge or not form_to_merge["phone"]:
                form_to_merge["phone"] = sub.get("talent_phone")
            form_to_merge.pop("location", None)
            await merge_talent_profile(
                talent_doc, form_to_merge, "project_submission",
                snapshot_at=sub.get("talent_profile_snapshot_at"),
            )
            await update_talent_cover_cache(talent_doc["id"])
    if talent_doc:
        patch["talent_id"] = talent_doc["id"]
        # A first-time submission is the earliest point a brand-new talent's
        # record exists — grant device trust here too, not just at OTP/Google
        # verify, so recognition works on their very next project link.
        await grant_trusted_device(response, talent_doc["id"])

    await db.submissions.update_one({"id": sid}, {"$set": patch})

    # Sync all uploads retroactively into the talent's global media.
    # Idempotent via source_submission_media_id.
    finalized_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    # Original submissions only — a resubmission/edit must never mirror its
    # media into the global Talent Profile (Issue 2).
    if not is_retest and finalized_sub and talent_doc:
        # Talent Profile Migration, Phase 4: a "declined" reusable item
        # (talent chose "only this project") must not participate in either
        # step below — not the category wipe, and not the resync. Without
        # this filter, declining would still silently wipe the EXISTING
        # canonical media in that category, which is exactly the kind of
        # auto-update the consent flow exists to prevent. Items with no
        # profile_sync_status (audition takes, from-library selections) are
        # unaffected — same as before Phase 4.
        syncable_media = [
            m for m in (finalized_sub.get("media") or [])
            if m.get("profile_sync_status") != "declined"
        ]

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
        for m in syncable_media:
            cat = m.get("category")
            if cat in cat_mapping:
                incoming_categories.add(cat_mapping[cat])

        if incoming_categories:
            await db.talents.update_one(
                {"id": talent_doc["id"]},
                {"$pull": {"media": {"category": {"$in": list(incoming_categories)}}}}
            )

        # P2-A: skip the per-item cover recompute (O(N²)); recompute once after.
        for m in syncable_media:
            await sync_media_to_global_talent(finalized_sub, m, skip_cover_cache=True)
        await update_talent_cover_cache(talent_doc["id"])

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
    # Internal WhatsApp fires ONLY here — at finalize, the single business event
    # that means the talent has *completed* a submission (first-time) or a
    # *completed* resubmission (is_retest, triggered by pressing "Update
    # Submission"). Media-upload handlers are NOT used: they run when a file
    # uploads, before the talent commits, so they'd alert on an incomplete edit.
    # finalize is called exactly once per commit → exactly one notification.
    if finalized_sub_for_notify := await db.submissions.find_one({"id": sid}, {"_id": 0}):
        enqueue_internal_whatsapp_notification(
            finalized_sub_for_notify,
            "SUBMISSION UPDATED" if is_retest else "NEW SUBMISSION",
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
    # Canonical talent-facing shape (feedback + signed media) — see
    # build_talent_submission_view in core.py for the single place this
    # logic lives.
    return await build_talent_submission_view(sub)


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
    # Canonical talent-facing shape — see build_talent_submission_view in
    # core.py.
    return await build_talent_submission_view(sub)


# --------------------------------------------------------------------------
# Admin review
# --------------------------------------------------------------------------
# Issue #9: the per-submission "Open Google Drive folder" link endpoint was
# removed — Talentgram serves all media from Cloudinary and the Review Center
# no longer surfaces a Drive folder button. (The optional, env-gated Drive
# backup worker is a separate concern and is left untouched.)


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
        items = await cursor.to_list(5000)
        return [sign_r2_media_if_needed(item) for item in items]
    skip, page_size, p, s = _paginate_params(page, size, limit)
    total = await db.submissions.count_documents(query)
    items = await cursor.skip(skip).limit(page_size).to_list(page_size)
    items = [sign_r2_media_if_needed(item) for item in items]
    return _paginated(items, total, p, s)


def _build_admin_prefill_form_data(talent: dict, email: str) -> Dict[str, Any]:
    """Shared by admin-start and batch-start — same fields `/public/prefill`
    returns, reshaped into the keys the submission page's own `form` state
    (and saveForm()) already use, so the resume effect that runs right after
    admin-start populates every field exactly like a talent's own resumed
    session would."""
    name = talent.get("name") or ""
    parts = name.split(" ", 1)
    return {
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "email": email,
        "phone": talent.get("phone") or "",
        "dob": talent.get("dob"),
        "age": str(talent.get("age")) if talent.get("age") is not None else "",
        "height": talent.get("height") or "",
        "location": talent.get("location") or [],
        "gender": talent.get("gender") or "",
        "ethnicity": talent.get("ethnicity") or "",
        "instagram_handle": talent.get("instagram_handle") or "",
        "instagram_followers": talent.get("instagram_followers") or "",
        "bio": talent.get("bio") or "",
        "work_links": talent.get("work_links") or [],
        "skills": talent.get("skills") or [],
    }


def _project_wants_category(requirements: dict, category: str) -> bool:
    """Smart Defaults gate (item 11) — same "hidden" opt-out convention
    `_submission_to_client_shape` already uses for visibility, reused here to
    decide which reusable categories are worth auto-attaching. Absence of an
    explicit "hidden" override means the project is treated as wanting it
    (matches the existing default-visible convention), not as excluding it —
    an unconfigured project still gets useful defaults."""
    reqs = requirements or {}
    if category == "intro_video":
        return reqs.get("intro_video") != "hidden"
    vis_key = "portfolio_image_visibility" if category == "image" else f"portfolio_{category}_visibility"
    return reqs.get(vis_key) != "hidden"


async def _auto_attach_existing_media(sid: str, talent: dict, project: dict) -> int:
    """Smart Defaults (item 11) — on a BRAND NEW draft only (callers must
    guard on `resumed is False`), copy-by-reference any of the talent's
    existing reusable-category Library media into the new draft, scoped to
    categories the project actually asks for. Reuses the exact same
    copy-by-reference item shape `submission_add_media_from_library`
    constructs for a single manual pick — this just does it in bulk, once,
    at creation time. No race-guard needed (nothing else can be concurrently
    editing a submission that didn't exist a moment ago), no single-slot
    eviction needed (nothing occupies the slot yet on a fresh doc).
    """
    library_media = await build_prefill_media(talent, email=talent.get("email"))
    if not library_media:
        return 0
    requirements = (project or {}).get("submission_requirements") or {}
    per_category_count: Dict[str, int] = {}
    to_attach: List[Dict[str, Any]] = []
    for lib_item in library_media:
        category = lib_item.get("category")
        if category not in REUSABLE_MEDIA_CATEGORIES:
            continue
        if not _project_wants_category(requirements, category):
            continue
        if category in PORTFOLIO_IMAGE_CATEGORIES:
            if per_category_count.get(category, 0) >= MAX_IMAGES_PER_CATEGORY:
                continue
            per_category_count[category] = per_category_count.get(category, 0) + 1
        elif category == "intro_video":
            # Single slot — build_prefill_media already returns at most one
            # intro_video item, but stay defensive against future changes.
            if any(m["category"] == "intro_video" for m in to_attach):
                continue
        new_item = {k: v for k, v in lib_item.items() if k != "id"}
        new_item.update({
            "id": str(uuid.uuid4()),
            "source_talent_media_id": lib_item["id"],
            "origin": "global",
            "created_at": _now(),
        })
        to_attach.append(new_item)
    if not to_attach:
        return 0
    await db.submissions.update_one({"id": sid}, {"$push": {"media": {"$each": to_attach}}})
    return len(to_attach)


@router.post("/projects/{pid}/talents/{talent_id}/submissions/admin-start")
async def admin_start_submission(
    pid: str,
    talent_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Admin Mode "Upload on Behalf" entry point.

    Creates (or resumes) a submission for `talent_id` on project `pid` and
    mints a submitter token exactly like `start_submission` does for the
    talent-facing link — but with no OTP gate (the admin's own authenticated
    session is the identity proof) and with an `acting_admin_email` claim
    baked into the token so every subsequent request through the existing
    public submission endpoints is attributed via `actor_stamp()`.

    Returns the same response shape as `start_submission`
    (`id, token, access_token, resumed, status`) so the frontend's admin-mode
    bootstrap can reuse the exact same handling code.
    """
    project = await db.projects.find_one({"id": pid})
    if not project:
        raise HTTPException(404, "Project not found")
    talent = await db.talents.find_one({"id": talent_id})
    if not talent:
        raise HTTPException(404, "Talent not found")
    email = normalize_email(talent.get("email") or (talent.get("source") or {}).get("talent_email"))
    if not email:
        raise HTTPException(400, "Talent has no email on file — add one before creating a submission")

    # Prefill form_data — only applied on first CREATE
    # (create_or_resume_submission_doc never touches form_data when
    # resuming an existing draft) — admin edits after that are never
    # silently overwritten by a later admin-start call.
    prefill_form_data = _build_admin_prefill_form_data(talent, email)

    result = await create_or_resume_submission_doc(
        project, email, talent.get("name") or "", talent.get("phone"), None,
        prefill_form_data, created_by=admin.get("email"), created_from="admin_upload", talent_id=talent_id,
    )
    # Smart Defaults (item 11) — same insert-only guard as form_data prefill
    # above: never touch an already-existing draft's media on a later
    # admin-start call (resume), only a genuinely brand-new one.
    if not result.get("resumed") and project.get("auto_attach_existing_media", True):
        await _auto_attach_existing_media(result["id"], talent, project)
    # Re-mint the token with the acting_admin_email claim (create_or_resume_submission_doc
    # mints a plain submitter token; admin-start needs the extra attribution claim).
    token = make_token(
        {"role": "submitter", "sid": result["id"], "slug": project["slug"], "acting_admin_email": admin.get("email")},
        days=3,
    )
    return {**result, "token": token, "talent_name": talent.get("name")}


class BatchStartIn(BaseModel):
    talent_ids: List[str]


@router.post("/projects/{pid}/submissions/batch-start")
async def admin_batch_start_submissions(
    pid: str,
    payload: BatchStartIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Pipeline batch draft creation (item 8) — create-or-resume a draft for
    every selected talent in one call. Reuses the exact same
    create_or_resume_submission_doc + Smart Defaults auto-attach
    admin_start_submission already uses per-talent — this is a loop over
    that same logic, not a second creation path. Never finalizes, never
    mints tokens (those are minted lazily, per-talent, when the admin
    actually opens one via the existing admin-start — keeps token lifetimes
    short and avoids minting 40 tokens nobody may ever use).
    """
    project = await db.projects.find_one({"id": pid})
    if not project:
        raise HTTPException(404, "Project not found")
    auto_attach = project.get("auto_attach_existing_media", True)

    results: Dict[str, Any] = {}
    for talent_id in payload.talent_ids:
        talent = await db.talents.find_one({"id": talent_id})
        if not talent:
            results[talent_id] = {"error": "Talent not found"}
            continue
        email = normalize_email(talent.get("email") or (talent.get("source") or {}).get("talent_email"))
        if not email:
            results[talent_id] = {"error": "Talent has no email on file"}
            continue
        prefill_form_data = _build_admin_prefill_form_data(talent, email)
        result = await create_or_resume_submission_doc(
            project, email, talent.get("name") or "", talent.get("phone"), None,
            prefill_form_data, created_by=admin.get("email"), created_from="admin_upload", talent_id=talent_id,
        )
        if not result.get("resumed") and auto_attach:
            await _auto_attach_existing_media(result["id"], talent, project)
        results[talent_id] = {"submission_id": result["id"], "resumed": result.get("resumed", False)}
    return results


@router.post("/projects/{pid}/submissions/{sid}/admin-token")
async def admin_submission_token(
    pid: str,
    sid: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Review Center Quick Edit — mints a short-lived attributed submitter
    token for an EXISTING submission, so Review Center's Replace/Add Images
    actions can drive the exact same public upload endpoints
    (`/public/submissions/{sid}/upload/sign` etc.) that Admin Mode and the
    talent flow already use, instead of a third upload implementation.

    Deliberately short-lived (1 day, vs admin-start's 3) — this token is
    fetched once per Review Center session for in-place media edits, not
    held across a multi-day drafting session the way Admin Mode's is.
    Carries the same `acting_admin_email` claim, so every edit made through
    it is attributed via `actor_stamp()` exactly like Admin Mode's uploads.
    """
    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0, "id": 1, "project_slug": 1})
    if not sub:
        raise HTTPException(404, "Submission not found")
    token = make_token(
        {"role": "submitter", "sid": sid, "slug": sub.get("project_slug"), "acting_admin_email": admin.get("email")},
        days=1,
    )
    return {"token": token}


class BulkPendingCountIn(BaseModel):
    project_ids: List[str]


@router.post("/projects/submissions/pending-count")
async def bulk_pending_submissions_count(
    payload: BulkPendingCountIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Total pending-review submission count across the given project ids.

    Dashboard's "Pending Reviews" KPI previously summed this by firing one
    /projects/{pid}/submissions/stats request per active project — an N+1
    that turns into hundreds of parallel requests at agency scale (500+
    projects). This replaces that fan-out with a single aggregation.
    """
    if not payload.project_ids:
        return {"pending": 0}
    count = await db.submissions.count_documents({
        "project_id": {"$in": payload.project_ids},
        "decision": "pending",
    })
    return {"pending": count}


class LookupByTalentIn(BaseModel):
    talent_emails: List[str] = Field(default_factory=list)
    talent_ids: List[str] = Field(default_factory=list)


@router.post("/projects/{pid}/submissions/lookup-by-talent")
async def submissions_lookup_by_talent(
    pid: str,
    payload: LookupByTalentIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Batched submission-status lookup for the Pipeline board's "Create
    Submission / Continue Draft / Open Submission" quick action.

    Matches by email primarily: `talent_id` is only ever set on a submission
    at admin-created time or once `_resolve_submission_talent` later resolves
    it, so a pre-existing talent-created draft may still have `talent_id`
    unset — an id-only filter would miss it. Mirrors the single-aggregation
    batching pattern in `bulk_pending_submissions_count` above instead of a
    per-card N+1 (one request per Pipeline board load, not one per card).
    """
    emails = [normalize_email(e) for e in payload.talent_emails if e]
    ids = [i for i in payload.talent_ids if i]
    if not emails and not ids:
        return {}
    or_clauses = []
    if emails:
        or_clauses.append({"talent_email": {"$in": emails}})
    if ids:
        or_clauses.append({"talent_id": {"$in": ids}})
    items = await db.submissions.find(
        {"project_id": pid, "$or": or_clauses},
        {"_id": 0, "id": 1, "status": 1, "decision": 1, "talent_email": 1, "talent_id": 1, "created_from": 1},
    ).to_list(len(emails) + len(ids) + 50)
    out: Dict[str, Any] = {}
    for it in items:
        entry = {
            "submission_id": it["id"],
            "status": it.get("status"),
            "decision": it.get("decision"),
            "created_from": it.get("created_from", "talent_link"),
        }
        email = it.get("talent_email")
        if email:
            out[email] = entry
        tid = it.get("talent_id")
        if tid:
            out[tid] = entry
    return out


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

    # 2026-08-16: an unchanged decision is only a true no-op when the
    # submission is already linked to a talent. A submission can be
    # "approved" with no talent_id if the talent-creation fallback below
    # failed at the time (e.g. the historical, now-fixed phone-uniqueness
    # collision) -- re-approving the SAME decision must still retry that
    # fallback in that case, not silently short-circuit forever. This does
    # NOT change behavior for a submission whose talent_id is already
    # present (still an unconditional no-op below, same as before), nor
    # for an actual decision change (first condition alone already false).
    if sub.get("decision") == payload.decision and sub.get("talent_id"):
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
            new_talent = build_minimal_talent_from_form(
                form,
                email=email,
                talent_name=sub.get("talent_name"),
                talent_phone=sub.get("talent_phone"),
                alternate_contact_number=sub.get("alternate_contact_number"),
                reference_id=sid,
                notes=f"Auto-created from decision on submission {sid} for project {pid}",
                created_by="auto-decision-sync",
                include_skills=False,
                include_updated_at=False,
            )
            from core import insert_talent_or_recover
            talent_doc, _recovered = await insert_talent_or_recover(
                new_talent, email=email, context=f"set_decision sid={sid} pid={pid}",
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
        else:
            # Talent resolution/creation still did not succeed (see
            # insert_talent_or_recover's own log line above for the exact
            # cause) -- the decision write below still proceeds (matching
            # existing behavior), but this must be loudly visible rather
            # than indistinguishable from a normal successful approval,
            # especially now that this branch can be reached on a RETRY
            # of an already-set decision, not just the first attempt.
            logger.error(
                "set_decision: submission %s (project %s) decision=%s but "
                "talent resolution/creation still failed -- no talent_id "
                "linked, no talent created.",
                sid, pid, payload.decision,
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

    # Issue #1/#10: no frozen snapshots. The client-facing package is rendered
    # live from the current submission on every request, so approving no longer
    # needs to freeze an immutable copy — the client link always reflects the
    # latest visibility/approval state.

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
        # Client/Hidden choices persist across reloads and drive the in-page
        # client preview. The talent record itself is never mutated.
        # Issue #2/#3: surface the 2-state model — legacy `internal_only`
        # collapses into client_visible=False.
        tmv = sub.get("talent_media_visibility") or {}
        for m in talent_doc.get("media") or []:
            if m.get("category") in PORTFOLIO_FETCH_CATEGORIES:
                # Ensure public_id items have resolvable URLs before including.
                if m.get("url") or m.get("public_id"):
                    item = dict(m)
                    if item.pop("internal_only", None) is True:
                        item["client_visible"] = False
                    ov = tmv.get(item.get("id"))
                    if isinstance(ov, dict):
                        if ov.get("internal_only") is True:
                            item["client_visible"] = False
                        elif "client_visible" in ov:
                            item["client_visible"] = ov["client_visible"]
                    talent_portfolio_media.append(item)

    # Fetch project visibility defaults
    from core import map_link_visibility_to_submission, DEFAULT_VISIBILITY
    link = await db.links.find_one({"auto_project_id": pid}, {"_id": 0, "visibility": 1})
    if link and link.get("visibility"):
        project_default_visibility = map_link_visibility_to_submission(link["visibility"])
    else:
        project_default_visibility = map_link_visibility_to_submission(DEFAULT_VISIBILITY)

    result = dict(sub)
    result["talent_portfolio_media"] = talent_portfolio_media
    result["project_default_visibility"] = project_default_visibility
    return sign_r2_media_if_needed(result)


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
    #
    # Issue #2/#3: the visibility model is now just Client / Hidden. Fold any
    # legacy `internal_only` into `client_visible=False` and never persist it.
    if payload.talent_media_visibility is not None:
        current_tmv = sub.get("talent_media_visibility") or {}
        incoming_tmv = {}
        for mid, ov in (payload.talent_media_visibility or {}).items():
            if not isinstance(ov, dict):
                continue
            cv = ov.get("client_visible")
            if ov.get("internal_only") is True:
                cv = False
            incoming_tmv[mid] = {"client_visible": cv if cv is not None else True}
        merged_tmv = {**current_tmv, **incoming_tmv}
        # Drop the deprecated flag from any previously-stored entries too.
        for mid, ov in merged_tmv.items():
            if isinstance(ov, dict) and "internal_only" in ov:
                cv = False if ov.get("internal_only") is True else ov.get("client_visible", True)
                merged_tmv[mid] = {"client_visible": cv}
        update["talent_media_visibility"] = merged_tmv

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
                for k in ["client_visible", "featured_for_client", "primary_take", "featured", "client_cover", "label", "category"]:
                    if k in m:
                        merged[k] = m[k]
                # Issue #2/#3: Client / Hidden only. Fold any incoming or
                # legacy `internal_only` into client_visible=False and strip it.
                if m.get("internal_only") is True or existing.get("internal_only") is True:
                    if "client_visible" not in m:
                        merged["client_visible"] = False
                merged.pop("internal_only", None)
                updated_media.append(merged)
            else:
                # Fallback for new/unmatched items
                m.pop("internal_only", None)
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

    # Issue #1/#10: no frozen snapshots. Curation edits (visibility, field
    # overrides, media order) take effect on the client link immediately
    # because the link renders live — there is nothing to regenerate.

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
        "origin": "project",  # Media Library Foundation (Phase 4 item 1) — admin-added, stored only on this submission (see docstring above), never touches the Global Profile.
    }

    await db.submissions.update_one({"id": sid}, {"$push": {"media": media_obj}})
    try:
        await db.asset_metadata.insert_one({
            "id": media_id,
            "public_id": result["public_id"],
            "folder": folder,
            "resource_type": rt,
            "asset_type": "admin_upload",
            "talent_id": sub.get("talent_id") or "unknown_talent",
            "talent_name": sub.get("talent_name") or "",
            "project_id": pid,
            "submission_id": sid,
            "file_size": result.get("bytes") or size_bytes,
            "created_at": _now(),
            "status": "completed"
        })
    except Exception as e:
        logger.warning(f"admin-media: asset_metadata write failed: {e}")
    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return fresh_sub


@router.post("/projects/{pid}/submissions/{sid}/admin-media-v2/sign")
async def admin_add_media_sign(
    pid: str,
    sid: str,
    payload: SignUploadIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Admin-authed counterpart to `submission_sign_upload` — same signed
    direct-to-Cloudinary flow `UploadManagerContext.uploadFile()` already
    drives for every talent-facing category (compression, progress, retry),
    reused here so Review Center's "Admin Added Media" gets the real upload
    engine instead of the old plain-multipart bypass (`admin_add_media`
    above, kept for pdf/raw and as a fallback — see `admin_add_media_complete`
    docstring). Image/video categories only; `pdf` stays on the old endpoint.
    """
    from core import DIRECT_UPLOAD_ENABLED
    if not DIRECT_UPLOAD_ENABLED:
        raise HTTPException(400, "Direct uploads are currently disabled")
    category = payload.category
    if category not in SUBMISSION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}
    media_id = f"adm_{str(uuid.uuid4())[:8]}"
    folder = f"talentgram/admin_media/{pid}/{sid}"
    public_id = media_id
    rt = "video" if is_video_slot else "image"

    # Cloudinary rearchitecture P4 — NO eager transformation. The admin uploads
    # the audition file and Cloudinary stores exactly one canonical asset (the
    # original). The old `w_1280,h_720,…,f_mp4` eager existed to guarantee a
    # browser-playable derivative for admins sourcing non-native containers
    # (QuickTime .mov, screen recordings); that concern is now handled as an
    # explicit, format/codec-gated exception at /admin-media-v2/complete —
    # NOT by transcoding every upload. Images get no eager thumbnail; the
    # thumbnail is a lazy single-canonical URL.
    eager = None
    transformation = None

    import time as _time
    import cloudinary.utils as _cu
    timestamp = int(_time.time())
    params = {"folder": folder, "public_id": public_id, "timestamp": timestamp}
    api_secret = cloudinary.config().api_secret
    signature = _cu.api_sign_request(params, api_secret)

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


@router.post("/projects/{pid}/submissions/{sid}/admin-media-v2/complete")
async def admin_add_media_complete(
    pid: str,
    sid: str,
    payload: CompleteUploadIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Completes an admin-media-v2 sign→direct-Cloudinary upload, preserving
    the exact semantics `admin_add_media` above already established
    (`scope: "admin_added"`, `admin_added_by`, `client_visible: True`
    default) — this media stays intentionally DISTINGUISHABLE in Review
    Center, unlike Admin Submission ("Upload on Behalf") media which is
    intentionally indistinguishable from a talent's own. Different concept,
    not folded together.
    """
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")

    category = payload.category
    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}
    thumbnail_url = None
    poster_url = None
    needs_compat_delivery = False
    if is_video_slot:
        # P4 — store the uploaded original. Browser-compat exception only.
        url = payload.url
        if video_needs_compat_delivery(payload.format, payload.video_codec):
            compat = compat_video_delivery_url(payload.url)
            if compat:
                url = compat
                needs_compat_delivery = True
        poster_url = video_poster_url(payload.url)
    else:
        url = payload.url
        thumbnail_url = media_url(payload.public_id, preset="thumb", resource_type="image")

    media_obj: Dict[str, Any] = {
        "id": payload.media_id,
        "category": category,
        "url": url,
        "public_id": payload.public_id,
        "resource_type": "video" if is_video_slot else "image",
        "content_type": payload.content_type or ("video/mp4" if is_video_slot else "image/jpeg"),
        "original_filename": payload.original_filename,
        "size": payload.bytes,
        "created_at": _now(),
        "scope": "admin_added",
        "submission_id": sid,
        "project_id": pid,
        "admin_added": True,
        "admin_added_by": admin.get("email"),
        "label": (payload.label or "").strip() or category,
        "client_visible": True,
        "duration": payload.duration,
        "poster_url": poster_url if is_video_slot else None,
        "thumbnail_url": thumbnail_url,
        "origin": "project",
    }
    if needs_compat_delivery:
        media_obj["original_url"] = payload.url
        media_obj["needs_compat_delivery"] = True
    await db.submissions.update_one({"id": sid}, {"$push": {"media": media_obj}})
    try:
        await db.asset_metadata.insert_one({
            "id": payload.media_id,
            "public_id": payload.public_id,
            "folder": f"talentgram/admin_media/{pid}/{sid}",
            "resource_type": media_obj["resource_type"],
            "asset_type": "admin_upload",
            "talent_id": sub.get("talent_id") or "unknown_talent",
            "talent_name": sub.get("talent_name") or "",
            "project_id": pid,
            "submission_id": sid,
            "file_size": payload.bytes,
            "created_at": _now(),
            "status": "completed",
        })
    except Exception as e:
        logger.warning(f"admin-media-v2: asset_metadata write failed: {e}")
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

    from media_lifecycle import delete_if_safe, DeletionContext

    target = next((m for m in (sub.get("media") or []) if m.get("id") == media_id), None)

    # 1. drop the application/submission reference (as before)
    await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"id": media_id}}})
    # keep the global profile in sync (mirror pull) — unchanged behaviour
    try:
        from core import remove_synced_media_from_global_talent
        await remove_synced_media_from_global_talent(sub, media_id)
    except Exception as e:
        logger.warning("admin_remove_media_item: mirror pull failed for %s: %s", media_id, e)

    # 2–4. evaluate ownership + all other references; mark PENDING only if safe;
    #      never a blind Cloudinary destroy (P6 — media lifecycle).
    outcome = None
    if target:
        res = await delete_if_safe(
            db, {**target, "submission_id": sid, "project_id": pid},
            ctx=DeletionContext(actor=admin.get("email"),
                                exclude_collection="submissions", exclude_parent_id=sid),
        )
        outcome = res.get("outcome")
        logger.info("admin_remove_media_item %s/%s: lifecycle outcome=%s", sid, media_id, outcome)

    fresh_sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if fresh_sub is not None:
        fresh_sub["_media_lifecycle_outcome"] = outcome
    return fresh_sub


@router.delete("/projects/{pid}/submissions/{sid}")
async def delete_submission(
    pid: str, sid: str, admin: dict = Depends(current_admin)
):
    """Soft-delete a submission (Cloudinary rearchitecture, P6 — media lifecycle).

    The submission stays as a historical record (``lifecycle_state=deleted`` +
    ``deleted_at``). Its PROJECT-owned audition media is recorded in the
    ``pending_media_deletions`` ledger with the configured retention window;
    GLOBAL talent media is left untouched; NO Cloudinary asset is deleted here.
    """
    from media_lifecycle import record_owner_teardown, get_retention_days, STATE_DELETED
    from datetime import datetime, timezone

    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")

    now = datetime.now(timezone.utc)
    retention_days = await get_retention_days(db)
    summ = await record_owner_teardown(
        db, sub.get("media") or [], context_kind="submission", context_id=sid,
        actor=admin.get("email"), retention_days=retention_days, now=now,
    )
    await db.submissions.update_one({"id": sid, "project_id": pid}, {"$set": {
        "lifecycle_state": STATE_DELETED, "deleted_at": now.isoformat(),
        "deleted_by": admin.get("email"),
    }})
    logger.info("DELETE submission %s soft-deleted by %s; audition media pending=%d "
                "global untouched=%d (NO Cloudinary asset deleted)",
                sid, admin.get("email"), summ["audition_enqueued"], summ["global_skipped"])
    return {"ok": True, "soft_deleted": True, "retention_days": retention_days,
            "audition_media_pending_deletion": summ["audition_enqueued"],
            "global_talent_media_untouched": summ["global_skipped"],
            "cloudinary_assets_deleted": 0}


@router.post("/projects/{pid}/submissions/{sid}/snapshot")
async def regenerate_submission_snapshot_endpoint(
    pid: str,
    sid: str,
    admin: dict = Depends(current_team_or_admin),
):
    """DEPRECATED (Issue #1/#10). Retained for ONE release only.

    Client-facing rendering is now always live (single engine), so refreshing a
    frozen snapshot has no effect on what clients see. The Review Center no
    longer surfaces a "Refresh Client Snapshot" button. This endpoint is kept
    in place for one release so an older cached frontend (during a rolling
    deploy) or any external caller does not 404. It writes the deprecated
    `client_package_snapshot` field but nothing reads it. Remove next release.
    """
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
    return {"ok": True, "snapshot": new_snapshot, "deprecated": True}


# ===========================================================================
# TEMP TEST TOOL — REMOVE AFTER WHATSAPP VALIDATION
# ===========================================================================
@router.post("/admin/whatsapp/test-internal-notification")
async def test_internal_notification_endpoint(admin: dict = Depends(current_admin)):
    """Temporary test endpoint to verify WhatsApp operations group notifications."""
    timestamp = _now()
    
    # 1. Read internal notification group configuration exactly like production
    cfg = await db.whatsapp_config.find_one({"key": "internal_notification_group_name"})
    group_name = cfg.get("value") if (cfg and cfg.get("value")) else "Talentgram Operations Test"

    # Construct the payload
    message_body = (
        "🚨 *TALENTGRAM INTERNAL NOTIFICATION TEST*\n\n"
        "Environment: Production\n"
        f"Timestamp: {timestamp}\n\n"
        "This is a test message generated from the internal notification system.\n\n"
        "If you are reading this, the following path is working:\n"
        "Backend\n"
        "→ WhatsApp Queue\n"
        "→ Railway Worker\n"
        "→ WhatsApp Group\n\n"
        "Test completed successfully."
    )

    batch_id = str(uuid.uuid4())
    batch_doc = {
        "id": batch_id,
        "source_type": "INTERNAL_NOTIFICATION",
        "source_label": "Internal Notification Test Message",
        "project_id": "test-project",
        "project_name": "Test Project",
        "template_id": "internal_notification",
        "template_slug": "internal_notification",
        "variable_data": {},
        "media_url": None,
        "is_dry_run": False,
        "status": "pending",
        "total_jobs": 1,
        "sent_count": 0,
        "failed_count": 0,
        "unconfirmed_count": 0,
        "created_by": "system",
        "created_at": timestamp,
        "started_at": None,
        "completed_at": None,
    }

    job_id = str(uuid.uuid4())
    job_doc = {
        "id": job_id,
        "batch_id": batch_id,
        "template_id": "internal_notification",
        "template_name": "Internal Notification",
        "source": "INTERNAL_NOTIFICATION",
        "source_id": "test-project",
        "recipient_kind": "INTERNAL_GROUP",
        "recipient_id": "internal_notification_group",
        "talent_id": None,
        "talent_name": group_name,
        "destination_type": "group",
        "destination": group_name,
        "message_body": message_body,
        "media_url": None,
        "is_dry_run": False,
        "status": "pending",
        "attempt_count": 0,
        "last_attempted_at": None,
        "sent_at": None,
        "error_message": None,
        "worker_picked_at": None,
        "created_at": timestamp,
    }

    await db.whatsapp_batches.insert_one(batch_doc)
    await db.whatsapp_jobs.insert_one(job_doc)
    logger.info("Successfully queued internal test WhatsApp notification")

    return {
        "success": True,
        "batch_id": batch_id,
        "job_id": job_id,
        "group_name": group_name,
    }


# ============================================================================
# Submission Diagnostics & Telemetry Pipeline
# ============================================================================

class SubmissionDiagnosticIn(BaseModel):
    device_id: str
    project_slug: str
    request_url: str
    page_url: str
    axios_code: Optional[str] = None
    axios_message: Optional[str] = None
    response_status: Optional[int] = None
    response_headers: Optional[Dict[str, str]] = None
    is_timeout: bool
    is_network: bool
    is_cancellation: bool
    user_agent: str
    platform: Optional[str] = None
    language: Optional[str] = None
    is_online: Optional[bool] = None
    connection_info: Optional[Dict[str, Any]] = None
    viewport_width: int
    viewport_height: int
    device_pixel_ratio: float
    referrer: Optional[str] = None
    is_whatsapp: bool
    is_instagram: bool
    is_facebook: bool
    is_safari: bool
    is_chrome: bool
    is_in_app: bool
    sw_controller_present: bool
    sw_registration_status: str
    sw_version: Optional[str] = None
    sw_script_url: Optional[str] = None
    sw_waiting_present: bool
    sw_installing_present: bool
    app_version: Optional[str] = None
    build_timestamp: Optional[str] = None
    frontend_build_id: Optional[str] = None
    commit_sha: Optional[str] = None
    environment: str
    time_taken_ms: int
    retry_attempt_count: int
    retry_succeeded: bool
    failure_type: str
    browser: Optional[str] = None
    browser_version: Optional[str] = None
    os: Optional[str] = None
    os_version: Optional[str] = None
    device_type: Optional[str] = None
    x_railway_request_id: Optional[str] = None
    x_request_id: Optional[str] = None
    traceparent: Optional[str] = None


_DIAGNOSTICS_BUCKET: Dict[str, List[float]] = {}
_DIAGNOSTICS_WINDOW = 3600.0  # 1 hour
_DIAGNOSTICS_LIMIT = 10     # max 10 requests per IP per hour


def _diagnostics_rate_limit_ok(request: Request) -> bool:
    now = time.monotonic()
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    bucket = _DIAGNOSTICS_BUCKET.setdefault(ip, [])
    cutoff = now - _DIAGNOSTICS_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _DIAGNOSTICS_LIMIT:
        return False
    bucket.append(now)
    return True


@router.post("/public/diagnostics")
async def create_public_diagnostics(
    payload: SubmissionDiagnosticIn,
    request: Request
):
    if not _diagnostics_rate_limit_ok(request):
        raise HTTPException(429, "Too many diagnostics reports — please slow down")
    
    # 5-minute duplicate suppression check
    from datetime import datetime, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    existing = await db.submission_diagnostics.find_one({
        "device_id": payload.device_id,
        "project_slug": payload.project_slug,
        "failure_type": payload.failure_type,
        "axios_message": payload.axios_message,
        "created_at": {"$gte": cutoff}
    })
    if existing:
        return {"ok": True, "suppressed": True}

    doc = payload.model_dump()
    doc["created_at"] = _now()  # timestamp
    
    # Store in MongoDB
    await db.submission_diagnostics.insert_one(doc)
    return {"ok": True}


@router.get("/admin/diagnostics")
async def list_admin_diagnostics(
    project_slug: Optional[str] = None,
    failure_type: Optional[str] = None,
    response_status: Optional[int] = None,
    retry_succeeded: Optional[bool] = None,
    device: Optional[str] = None,  # whatsapp, instagram, facebook, in_app
    browser: Optional[str] = None,  # safari, chrome
    date_from: Optional[str] = None, # ISO string
    date_to: Optional[str] = None,   # ISO string
    page: int = 1,
    size: int = 50,
    admin: dict = Depends(current_team_or_admin)
):
    query = {}
    if project_slug:
        query["project_slug"] = project_slug
    if failure_type:
        query["failure_type"] = failure_type
    if response_status is not None:
        query["response_status"] = response_status
    if retry_succeeded is not None:
        query["retry_succeeded"] = retry_succeeded
    
    if device:
        if device == "whatsapp":
            query["is_whatsapp"] = True
        elif device == "instagram":
            query["is_instagram"] = True
        elif device == "facebook":
            query["is_facebook"] = True
        elif device == "in_app":
            query["is_in_app"] = True
            
    if browser:
        if browser == "safari":
            query["is_safari"] = True
        elif browser == "chrome":
            query["is_chrome"] = True

    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        query["created_at"] = date_q

    cursor = db.submission_diagnostics.find(query, {"_id": 0}).sort("created_at", -1)
    skip = (page - 1) * size
    total = await db.submission_diagnostics.count_documents(query)
    docs = await cursor.skip(skip).limit(size).to_list(length=size)
    return {"total": total, "results": docs, "page": page, "size": size}


@router.get("/admin/diagnostics/summary")
async def get_diagnostics_summary(
    admin: dict = Depends(current_team_or_admin)
):
    pipeline = [
        {
            "$group": {
                "_id": {
                    "failure_type": "$failure_type",
                    "axios_message": "$axios_message",
                    "response_status": "$response_status",
                },
                "occurrences": {"$sum": 1},
                "first_seen": {"$min": "$created_at"},
                "last_seen": {"$max": "$created_at"},
                "projects": {"$addToSet": "$project_slug"},
                "devices": {
                    "$push": {
                        "is_whatsapp": "$is_whatsapp",
                        "is_instagram": "$is_instagram",
                        "is_facebook": "$is_facebook",
                        "is_safari": "$is_safari",
                        "is_chrome": "$is_chrome",
                        "is_in_app": "$is_in_app",
                        "user_agent": "$user_agent"
                    }
                }
            }
        },
        {"$sort": {"occurrences": -1}}
    ]
    raw_results = await db.submission_diagnostics.aggregate(pipeline).to_list(length=200)
    
    formatted = []
    for r in raw_results:
        devices_set = set()
        for d in r.get("devices", []):
            if d.get("is_whatsapp"):
                devices_set.add("WhatsApp WebView")
            elif d.get("is_instagram"):
                devices_set.add("Instagram WebView")
            elif d.get("is_facebook"):
                devices_set.add("Facebook WebView")
            elif d.get("is_in_app"):
                devices_set.add("In-App Browser")
            elif d.get("is_safari"):
                devices_set.add("Safari")
            elif d.get("is_chrome"):
                devices_set.add("Chrome")
            else:
                devices_set.add("Unknown Browser")
        
        formatted.append({
            "failure_type": r["_id"].get("failure_type"),
            "axios_message": r["_id"].get("axios_message"),
            "response_status": r["_id"].get("response_status"),
            "occurrences": r["occurrences"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "projects": r["projects"],
            "affected_devices": list(devices_set)
        })
    return formatted


@router.get("/admin/diagnostics/metrics")
async def get_diagnostics_metrics(
    admin: dict = Depends(current_team_or_admin)
):
    total = await db.submission_diagnostics.count_documents({})
    recovered = await db.submission_diagnostics.count_documents({"retry_succeeded": True})
    
    pipeline_types = [
        {"$group": {"_id": "$failure_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    type_counts = await db.submission_diagnostics.aggregate(pipeline_types).to_list(length=100)
    
    pipeline_daily = [
        {
            "$project": {
                "date": {"$substr": ["$created_at", 0, 10]}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "count": {"$sum": 1}
            }
        },
        {"$sort": {"_id": 1}}
    ]
    daily_counts = await db.submission_diagnostics.aggregate(pipeline_daily).to_list(length=30)
    
    return {
        "total_failures": total,
        "recovered_failures": recovered,
        "failure_types": [{"type": item["_id"] or "unknown", "count": item["count"]} for item in type_counts],
        "daily_failures": [{"date": item["_id"] or "unknown", "count": item["count"]} for item in daily_counts]
    }


