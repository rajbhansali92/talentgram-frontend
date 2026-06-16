"""Open talent applications — project-independent signups.

Flow:
  POST  /api/public/apply           -> start (email is unique identifier)
  PUT   /api/public/apply/{aid}     -> save form_data incrementally
  POST  /api/public/apply/{aid}/upload  -> images / intro_video
  DELETE /api/public/apply/{aid}/media/{mid}
  POST  /api/public/apply/{aid}/finalize -> lock submission
  GET   /api/public/apply/{aid}     -> load in-progress (auth via submitter token)

Admin:
  GET   /api/applications                -> list all
  GET   /api/applications/{aid}          -> full doc
  POST  /api/applications/{aid}/decision -> approve/reject (approval pushes to master Talents DB)
"""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pymongo.errors import DuplicateKeyError

from core import (
    APP_NAME,
    APPLICATION_DECISIONS,
    APPLICATION_UPLOAD_CATEGORIES,
    MAX_APPLICATION_IMAGES,
    MAX_IMAGES_PER_CATEGORY,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_VIDEO_BYTES,
    ApplicationStartIn,
    SubmissionDecisionIn,
    SubmissionUpdateIn,
    _now,
    _paginate_params,
    _paginated,
    _resolve_cover_url,
    cloudinary_upload,
    compute_age,
    compute_effective_age,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_token,
    media_url,
    normalize_instagram_handle,
    video_poster_url,
    update_talent_cover_cache,
)
from drive_backup import drive_enabled, enqueue_drive_upload

router = APIRouter(prefix="/api", tags=["applications"])

from pydantic import BaseModel, Field

class ProfileRequirements(BaseModel):
    name: str = "required"  # "required" | "optional"
    location: str = "required"  # "required" | "optional"
    instagram_handle: str = "required"  # "required" | "optional"
    instagram_followers: str = "required"  # "required" | "optional"

class PortfolioRequirements(BaseModel):
    portfolio: str = "required"  # "required" | "optional"
    indian: str = "required"  # "required" | "optional"
    western: str = "required"  # "required" | "optional"
    video: str = "required"  # "required" | "optional"

class OnboardingConfig(BaseModel):
    profile_requirements: ProfileRequirements
    portfolio_requirements: PortfolioRequirements

DEFAULT_ONBOARDING_CONFIG = {
    "profile_requirements": {
        "name": "required",
        "location": "required",
        "instagram_handle": "required",
        "instagram_followers": "required",
    },
    "portfolio_requirements": {
        "portfolio": "required",
        "indian": "required",
        "western": "required",
        "video": "required",
    }
}

class ProfileConfigIn(BaseModel):
    title: str
    profile_requirements: ProfileRequirements
    portfolio_requirements: PortfolioRequirements

@router.get("/public/onboarding-config")
async def get_public_onboarding_config(profile: Optional[str] = None):
    if profile:
        config = await db.profile_configs.find_one({"id": profile})
        if config:
            return {
                "profile_requirements": config.get("profile_requirements", DEFAULT_ONBOARDING_CONFIG["profile_requirements"]),
                "portfolio_requirements": config.get("portfolio_requirements", DEFAULT_ONBOARDING_CONFIG["portfolio_requirements"])
            }
    # Fall back to global/default config
    config = await db.profile_configs.find_one({"key": "global_onboarding"})
    if not config:
        return DEFAULT_ONBOARDING_CONFIG
    return {
        "profile_requirements": config.get("profile_requirements", DEFAULT_ONBOARDING_CONFIG["profile_requirements"]),
        "portfolio_requirements": config.get("portfolio_requirements", DEFAULT_ONBOARDING_CONFIG["portfolio_requirements"])
    }

@router.get("/admin/onboarding-config")
async def get_admin_onboarding_config(admin: dict = Depends(current_team_or_admin)):
    config = await db.profile_configs.find_one({"key": "global_onboarding"})
    if not config:
        return DEFAULT_ONBOARDING_CONFIG
    return {
        "profile_requirements": config.get("profile_requirements", DEFAULT_ONBOARDING_CONFIG["profile_requirements"]),
        "portfolio_requirements": config.get("portfolio_requirements", DEFAULT_ONBOARDING_CONFIG["portfolio_requirements"])
    }

@router.put("/admin/onboarding-config")
async def update_admin_onboarding_config(payload: OnboardingConfig, admin: dict = Depends(current_team_or_admin)):
    config_dict = payload.dict()
    await db.profile_configs.update_one(
        {"key": "global_onboarding"},
        {"$set": {
            "profile_requirements": config_dict["profile_requirements"],
            "portfolio_requirements": config_dict["portfolio_requirements"]
        }},
        upsert=True
    )
    return {"ok": True, "config": config_dict}

# CRUD for multiple custom profile onboarding configs
@router.get("/admin/profile-configs")
async def list_admin_profile_configs(admin: dict = Depends(current_team_or_admin)):
    configs = await db.profile_configs.find({}, {"_id": 0}).to_list(1000)
    return configs

@router.get("/admin/profile-configs/{id}")
async def get_admin_profile_config(id: str, admin: dict = Depends(current_team_or_admin)):
    config = await db.profile_configs.find_one({"id": id}, {"_id": 0})
    if not config:
        raise HTTPException(404, "Profile configuration not found")
    return config

@router.post("/admin/profile-configs")
async def create_admin_profile_config(payload: ProfileConfigIn, admin: dict = Depends(current_team_or_admin)):
    config_id = str(uuid.uuid4())
    doc = {
        "id": config_id,
        "title": payload.title,
        "profile_requirements": payload.profile_requirements.dict(),
        "portfolio_requirements": payload.portfolio_requirements.dict(),
        "created_at": _now()
    }
    await db.profile_configs.insert_one(doc)
    doc.pop("_id", None)
    return doc

@router.put("/admin/profile-configs/{id}")
async def update_admin_profile_config(id: str, payload: ProfileConfigIn, admin: dict = Depends(current_team_or_admin)):
    existing = await db.profile_configs.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Profile configuration not found")
    await db.profile_configs.update_one(
        {"id": id},
        {"$set": {
            "title": payload.title,
            "profile_requirements": payload.profile_requirements.dict(),
            "portfolio_requirements": payload.portfolio_requirements.dict(),
        }}
    )
    return {"ok": True}

@router.delete("/admin/profile-configs/{id}")
async def delete_admin_profile_config(id: str, admin: dict = Depends(current_team_or_admin)):
    existing = await db.profile_configs.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Profile configuration not found")
    await db.profile_configs.delete_one({"id": id})
    return {"ok": True}


# --------------------------------------------------------------------------
# Public (talent-facing)
# --------------------------------------------------------------------------
@router.post("/public/apply")
async def start_application(payload: ApplicationStartIn):
    email = payload.email.lower().strip()
    # Email is the unique identifier — reuse the in-progress draft if exists
    existing = await db.applications.find_one({"talent_email": email})
    if existing:
        if existing.get("status") == "submitted":
            raise HTTPException(
                409,
                "An application already exists for this email. Please contact the team for updates.",
            )
        # Resume draft
        aid = existing["id"]
        token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
        # Update base fields in case they changed
        await db.applications.update_one(
            {"id": aid},
            {"$set": {
                "form_data.first_name": payload.first_name.strip(),
                "form_data.last_name": payload.last_name.strip(),
                "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
                "talent_phone": payload.phone,
                "access_token": token,
                "profile_id": payload.profile_id or existing.get("profile_id"),
            }},
        )
        return {"id": aid, "token": token, "resumed": True}

    talent_age = None
    talent_doc = await db.talents.find_one({"$or": [{"email": email}, {"source.talent_email": email}]}, {"age": 1, "dob": 1})
    if talent_doc:
        talent_age = talent_doc.get("age") or (compute_age(talent_doc.get("dob")) if talent_doc.get("dob") else None)

    aid = str(uuid.uuid4())
    form_data = {
        "first_name": payload.first_name.strip(),
        "last_name": payload.last_name.strip(),
    }
    effective_age_val = compute_effective_age(form_data, talent_age)

    doc = {
        "id": aid,
        "talent_email": email,
        "talent_phone": payload.phone,
        "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
        "form_data": form_data,
        "submitted_age_override": None,
        "effective_age": effective_age_val,
        "media": [],
        "status": "draft",
        "decision": "pending",
        "created_at": _now(),
        "submitted_at": None,
        "profile_id": payload.profile_id,
    }
    try:
        await db.applications.insert_one(doc)
    except DuplicateKeyError:
        # Race: another tab/device created an application for this email
        # in parallel. Fall back to that one (resume).
        existing = await db.applications.find_one({"talent_email": email})
        if existing:
            aid = existing["id"]
            token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
            await db.applications.update_one(
                {"id": aid},
                {"$set": {
                    "access_token": token,
                    "profile_id": payload.profile_id or existing.get("profile_id"),
                }}
            )
            return {"id": aid, "token": token, "resumed": True}
        raise HTTPException(409, "An application already exists for this email")
    token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
    return {"id": aid, "token": token, "resumed": False}


async def _check_app_token(authorization: Optional[str], aid: str) -> Dict[str, Any]:
    data = await decode_submitter(authorization)
    if not data or data.get("sid") != aid or data.get("kind") != "application":
        raise HTTPException(401, "Invalid application token")
    return data


@router.get("/public/apply/{aid}")
async def get_application(aid: str, authorization: Optional[str] = Header(None)):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid}, {"_id": 0})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    return app_doc


@router.put("/public/apply/{aid}")
async def update_application(
    aid: str,
    payload: SubmissionUpdateIn,
    authorization: Optional[str] = Header(None),
):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        merged = {**(app_doc.get("form_data") or {}), **payload.form_data}
        update["form_data"] = merged
        fn = merged.get("first_name")
        ln = merged.get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or app_doc.get("talent_name")

        # Effective age resolves strictly from DOB — no override fields.
        dob = merged.get("dob")
        effective_age_val = compute_age(dob) if dob else None
        # Fallback: check existing talent record for a stored age
        if effective_age_val is None:
            email = app_doc.get("talent_email")
            if email:
                talent_doc = await db.talents.find_one(
                    {"$or": [{"email": email}, {"source.talent_email": email}]},
                    {"age": 1, "dob": 1},
                )
                if talent_doc:
                    effective_age_val = (
                        compute_age(talent_doc["dob"]) if talent_doc.get("dob")
                        else talent_doc.get("age")
                    )
        update["effective_age"] = effective_age_val
    if update:
        await db.applications.update_one({"id": aid}, {"$set": update})
    return await db.applications.find_one({"id": aid}, {"_id": 0})


@router.post("/public/apply/{aid}/upload")
async def upload_application_media(
    request: Request,
    aid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    await _check_app_token(authorization, aid)
    if category not in APPLICATION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category (image|indian|western|intro_video)")
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    ct = (file.content_type or "").lower()
    fn = (file.filename or "").lower()
    is_video = category == "intro_video"
    is_image = category in ("image", "indian", "western")

    # Validation of content type / format (P5)
    if is_video:
        if not (ct.startswith("video/") or fn.endswith((".mp4", ".mov", ".avi", ".webm", ".mkv", ".3gp"))):
            raise HTTPException(400, "Unsupported video format. Please upload MP4, MOV, or WEBM.")
    else:
        # Image categories
        if ct in {"image/bmp", "image/tiff", "image/heic", "image/heif"} or fn.endswith((".bmp", ".tiff", ".heic", ".heif")):
            raise HTTPException(400, "HEIC, BMP, and TIFF formats are not supported. Please upload JPEG or PNG.")
        if not (ct.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp"))):
            raise HTTPException(400, "Unsupported image format. Please upload JPG, PNG, or WEBP.")

    if category in ("image", "indian", "western"):
        # Phase 3: per-category cap (10 each) — NOT a combined total.
        existing = sum(
            1 for m in app_doc.get("media", []) if m.get("category") == category
        )
        if existing >= MAX_IMAGES_PER_CATEGORY:
            label = {"image": "Portfolio", "indian": "Indian look", "western": "Western look"}.get(category, category)
            raise HTTPException(400, f"{label} image limit reached ({MAX_IMAGES_PER_CATEGORY})")
    if category == "intro_video":
        # Single slot; replace previous if exists
        await db.applications.update_one(
            {"id": aid}, {"$pull": {"media": {"category": "intro_video"}}}
        )

    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/applications/{aid}"

    # P2-E — Reject oversized uploads BEFORE reading the body into RAM.
    raw_cl = request.headers.get("content-length")
    if raw_cl is not None:
        try:
            declared_bytes = int(raw_cl)
        except ValueError:
            declared_bytes = 0
        if is_video and declared_bytes > MAX_SUBMISSION_VIDEO_BYTES:
            cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
            raise HTTPException(
                413,
                f"Video is too large. Max {cap_mb} MB — please compress and retry.",
            )
        if is_image and declared_bytes > MAX_SUBMISSION_IMAGE_BYTES:
            cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
            raise HTTPException(
                413, f"Image is too large. Max {cap_mb} MB per image."
            )

    data = await file.read()

    # Secondary check on actual bytes
    size_bytes = len(data)
    if is_video and size_bytes > MAX_SUBMISSION_VIDEO_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
        raise HTTPException(
            400,
            f"Video is too large ({mb} MB). Max {cap_mb} MB — please compress and retry.",
        )
    if is_image and size_bytes > MAX_SUBMISSION_IMAGE_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(
            400, f"Image is too large ({mb} MB). Max {cap_mb} MB per image."
        )

    # Cloudinary auto-detects video/image/raw by content type.
    rt = "video" if (category == "intro_video" or (file.content_type or "").startswith("video/")) else "image"
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=media_id,
        resource_type=rt,
        content_type=file.content_type,
        keep_original=False,
    )
    is_video_uploaded = rt == "video"
    is_image_uploaded = rt == "image"
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
        "scope": "application",
        "application_id": aid,
        "duration": result.get("duration"),
        "thumbnail_url": media_url(result["public_id"], preset="thumb", resource_type=result["resource_type"]) if is_image_uploaded else None,
        "poster_url": video_poster_url(result["public_id"]) if is_video_uploaded else None,
    }
    await db.applications.update_one({"id": aid}, {"$push": {"media": media}})
    updated = await db.applications.find_one({"id": aid}, {"_id": 0})

    # Drive secondary backup — applications don't have a brand, use a stable bucket.
    if drive_enabled():
        media["scope"] = "application"
        enqueue_drive_upload(db, media, updated, "_Applications", data)

    return updated


@router.delete("/public/apply/{aid}/media/{mid}")
async def delete_application_media(
    aid: str, mid: str, authorization: Optional[str] = Header(None)
):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")
    await db.applications.update_one({"id": aid}, {"$pull": {"media": {"id": mid}}})
    return {"ok": True}


@router.post("/public/apply/{aid}/edit")
async def edit_application(aid: str, authorization: Optional[str] = Header(None)):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    await db.applications.update_one(
        {"id": aid},
        {"$set": {"status": "draft"}}
    )
    return {"ok": True, "status": "draft"}


@router.post("/public/apply/{aid}/finalize")
async def finalize_application(aid: str, authorization: Optional[str] = Header(None)):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    fd = app_doc.get("form_data") or {}

    config = None
    profile_id = app_doc.get("profile_id")
    if profile_id:
        try:
            config = await db.profile_configs.find_one({"id": profile_id})
        except Exception:
            config = None

    if not config:
        try:
            config = await db.profile_configs.find_one({"key": "global_onboarding"})
        except Exception:
            config = None

    if not config:
        config = DEFAULT_ONBOARDING_CONFIG

    prof_reqs = config.get("profile_requirements") or DEFAULT_ONBOARDING_CONFIG["profile_requirements"]
    port_reqs = config.get("portfolio_requirements") or DEFAULT_ONBOARDING_CONFIG["portfolio_requirements"]

    # 1. Profile Requirements Validation
    if prof_reqs.get("name") == "required":
        if not (fd.get("first_name") or "").strip() or not (fd.get("last_name") or "").strip():
            raise HTTPException(400, "Full Name is required")

    if prof_reqs.get("location") == "required":
        if not (fd.get("location") or "").strip():
            raise HTTPException(400, "Current Location is required")

    if prof_reqs.get("instagram_handle") == "required":
        if not (fd.get("instagram_handle") or "").strip():
            raise HTTPException(400, "Instagram Handle is required")

    if prof_reqs.get("instagram_followers") == "required":
        if not fd.get("instagram_followers"):
            raise HTTPException(400, "Instagram Followers is required")

    # 2. Portfolio Requirements Validation
    media = app_doc.get("media", [])

    if port_reqs.get("portfolio") == "required":
        port_count = sum(1 for m in media if m.get("category") == "image")
        if port_count < 1:
            raise HTTPException(400, "Portfolio Images are required")

    if port_reqs.get("indian") == "required":
        indian_count = sum(1 for m in media if m.get("category") == "indian")
        if indian_count < 1:
            raise HTTPException(400, "Indian Look Images are required")

    if port_reqs.get("western") == "required":
        western_count = sum(1 for m in media if m.get("category") == "western")
        if western_count < 1:
            raise HTTPException(400, "Western Look Images are required")

    if port_reqs.get("video") == "required":
        video_count = sum(1 for m in media if m.get("category") == "intro_video")
        if video_count < 1:
            raise HTTPException(400, "Introduction Video is required")
    await db.applications.update_one(
        {"id": aid},
        {"$set": {"status": "submitted", "submitted_at": _now()}},
    )

    # Fetch updated application document for talent sync
    updated_app = await db.applications.find_one({"id": aid})
    email = updated_app.get("talent_email")
    if email:
        email = email.lower().strip()
        existing_talent = await db.talents.find_one(
            {"$or": [{"email": email}, {"source.talent_email": email}]}
        )
        if existing_talent:
            fd = updated_app.get("form_data") or {}
            dob = (fd.get("dob") or "").strip() or None
            age = compute_age(dob) if dob else None
            
            new_media = []
            cover_mid = None
            for m in updated_app.get("media", []) or []:
                cat = m.get("category")
                if cat == "image":
                    new_cat = "portfolio"
                elif cat == "indian":
                    new_cat = "indian"
                elif cat == "western":
                    new_cat = "western"
                elif cat == "intro_video":
                    new_cat = "video"
                else:
                    continue
                mid = m.get("id") or str(uuid.uuid4())
                new_media.append({
                    "id": mid,
                    "category": new_cat,
                    "url": m.get("url"),
                    "public_id": m.get("public_id"),
                    "resource_type": m.get("resource_type"),
                    "content_type": m.get("content_type", "application/octet-stream"),
                    "original_filename": m.get("original_filename"),
                    "size": m.get("size", 0),
                    "created_at": m.get("created_at") or _now(),
                    "scope": "talent_portfolio",
                    "talent_id": existing_talent["id"],
                    "duration": m.get("duration"),
                    "poster_url": m.get("poster_url"),
                })
                if new_cat in ("portfolio", "indian", "western") and not cover_mid:
                    cover_mid = mid

            VALID_INTERESTS = {
                "Acting", "Modeling", "Print Campaigns", "TV Commercials",
                "Digital Ads", "Instagram Collaborations", "Influencer Campaigns",
                "Social Media Collaborations", "Fashion Campaigns", "Brand Shoots",
                "Music Videos", "OTT / Film Projects", "Event Appearances", "Hosting / Anchoring",
            }
            raw_interests = fd.get("interested_in")
            if not isinstance(raw_interests, list):
                raw_interests = [raw_interests] if raw_interests else []
            interested_in = [i for i in raw_interests if isinstance(i, str) and i.strip() in VALID_INTERESTS]

            first_name = fd.get("first_name") or ""
            last_name = fd.get("last_name") or ""
            name_combined = f"{first_name} {last_name}".strip()

            raw_work_links = fd.get("work_links")
            if not isinstance(raw_work_links, list):
                raw_work_links = [raw_work_links] if raw_work_links else []
            work_links = [w for w in raw_work_links if isinstance(w, str) and w.strip()]

            raw_skills = fd.get("skills")
            if not isinstance(raw_skills, list):
                raw_skills = [raw_skills] if raw_skills else []
            skills = [s for s in raw_skills if isinstance(s, str) and s.strip()]

            update = {
                "name": name_combined or updated_app.get("talent_name") or "Unnamed",
                "phone": (fd.get("phone") or updated_app.get("talent_phone") or None),
                "age": age,
                "dob": dob,
                "height": fd.get("height") or None,
                "location": fd.get("location") or None,
                "ethnicity": fd.get("ethnicity") or None,
                "gender": fd.get("gender") or None,
                "instagram_handle": normalize_instagram_handle(fd.get("instagram_handle") or None) if fd.get("instagram_handle") else None,
                "instagram_followers": fd.get("instagram_followers") or None,
                "bio": fd.get("bio") or None,
                "work_links": work_links,
                "skills": skills,
                "cover_media_id": cover_mid,
                "interested_in": interested_in,
                "media": new_media,
            }
            await db.talents.update_one({"id": existing_talent["id"]}, {"$set": update})
            await update_talent_cover_cache(existing_talent["id"])
    return {"ok": True}


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
@router.get("/applications")
async def list_applications(
    status: Optional[str] = None,
    decision: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    limit: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if decision:
        query["decision"] = decision
    cursor = db.applications.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None and limit is None:
        items = await cursor.to_list(5000)
        return [_with_image_url(a) for a in items]
    skip, page_size, p, s = _paginate_params(page, size, limit)
    total = await db.applications.count_documents(query)
    items = await cursor.skip(skip).limit(page_size).to_list(page_size)
    return _paginated([_with_image_url(a) for a in items], total, p, s)


@router.get("/applications/stats")
async def applications_stats(admin: dict = Depends(current_team_or_admin)):
    """Lightweight counts for the filter chips.

    P1-B: Single $facet aggregation replaces 5 sequential count_documents
    calls, reducing MongoDB round-trips from 5 to 1 per page load.
    Compound index (status, decision) makes facet branches index-covered.
    """
    pipeline = [
        {"$facet": {
            "all":      [{"$count": "n"}],
            "pending":  [{"$match": {"status": "submitted", "decision": "pending"}}, {"$count": "n"}],
            "approved": [{"$match": {"decision": "approved"}}, {"$count": "n"}],
            "rejected": [{"$match": {"decision": "rejected"}}, {"$count": "n"}],
            "drafts":   [{"$match": {"status": "draft"}},    {"$count": "n"}],
        }},
    ]
    results = await db.applications.aggregate(pipeline).to_list(1)
    facets = results[0] if results else {}
    def _n(key: str) -> int:
        bucket = facets.get(key) or []
        return bucket[0]["n"] if bucket else 0
    return {
        "all":      _n("all"),
        "pending":  _n("pending"),
        "approved": _n("approved"),
        "rejected": _n("rejected"),
        "drafts":   _n("drafts"),
    }


@router.get("/applications/{aid}")
async def get_admin_application(aid: str, admin: dict = Depends(current_team_or_admin)):
    app_doc = await db.applications.find_one({"id": aid}, {"_id": 0})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    return _with_image_url(app_doc)


def _with_image_url(app_doc: dict) -> dict:
    """Add a top-level ``image_url`` (Cloudinary cover URL or None) so
    frontends don't need to walk media[]. Frontend-safe — never returns
    the literal string ``"undefined"``."""
    if app_doc is None:
        return app_doc
    app_doc["image_url"] = _resolve_cover_url(app_doc) or None
    return app_doc


@router.post("/applications/{aid}/decision")
async def set_application_decision(
    aid: str,
    payload: SubmissionDecisionIn,
    admin: dict = Depends(current_admin),
):
    if payload.decision not in APPLICATION_DECISIONS:
        raise HTTPException(400, "Invalid decision")
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")

    # Persist decision
    await db.applications.update_one(
        {"id": aid},
        {"$set": {"decision": payload.decision, "decided_at": _now(), "decided_by": admin["id"]}},
    )

    # On approval, copy into master Talents DB (merge if email already exists)
    if payload.decision == "approved":
        talent = _application_to_talent(app_doc, admin["id"])
        email = talent["email"]
        # Broad email-based dedup: match any talent whose top-level email OR
        # source.talent_email matches (covers manual adds, prior applications, and
        # legacy project-forwarded submissions).
        existing = await db.talents.find_one(
            {"$or": [{"email": email}, {"source.talent_email": email}]}
        )
        if existing:
            # Merge: append new media, fill empty fields, never overwrite
            new_media = existing.get("media", []) + [
                m for m in talent["media"] if m["id"] not in {x["id"] for x in existing.get("media", [])}
            ]
            update = {"media": new_media}
            for key in ("email", "phone", "age", "dob", "height", "location", "ethnicity", "gender", "instagram_handle", "instagram_followers", "bio", "skills"):
                if not existing.get(key) and talent.get(key):
                    update[key] = talent[key]
            # work_links: extend (dedupe) only if existing list is empty.
            if not (existing.get("work_links") or []) and talent.get("work_links"):
                update["work_links"] = talent["work_links"]
            if not existing.get("cover_media_id") and talent.get("cover_media_id"):
                update["cover_media_id"] = talent["cover_media_id"]
            # interested_in: merge unique categories from application into existing profile.
            existing_interests = set(existing.get("interested_in") or [])
            incoming_interests = set(talent.get("interested_in") or [])
            merged_interests = sorted(existing_interests | incoming_interests)
            if merged_interests:
                update["interested_in"] = merged_interests
            await db.talents.update_one({"id": existing["id"]}, {"$set": update})
            await update_talent_cover_cache(existing["id"])
            await db.applications.update_one(
                {"id": aid}, {"$set": {"talent_id": existing["id"], "merged": True}}
            )
            return {"ok": True, "talent_id": existing["id"], "merged": True}
        else:
            try:
                await db.talents.insert_one(talent)
                await update_talent_cover_cache(talent["id"])
                await db.applications.update_one(
                    {"id": aid}, {"$set": {"talent_id": talent["id"], "merged": False}}
                )
                return {"ok": True, "talent_id": talent["id"], "merged": False}
            except DuplicateKeyError:
                existing = await db.talents.find_one(
                    {"$or": [{"email": email}, {"source.talent_email": email}]}
                )
                if existing:
                    new_media = existing.get("media", []) + [
                        m for m in talent["media"] if m["id"] not in {x["id"] for x in existing.get("media", [])}
                    ]
                    update = {"media": new_media}
                    for key in ("email", "phone", "age", "dob", "height", "location", "ethnicity", "gender", "instagram_handle", "instagram_followers", "bio", "skills"):
                        if not existing.get(key) and talent.get(key):
                            update[key] = talent[key]
                    if not (existing.get("work_links") or []) and talent.get("work_links"):
                        update["work_links"] = talent["work_links"]
                    if not existing.get("cover_media_id") and talent.get("cover_media_id"):
                        update["cover_media_id"] = talent["cover_media_id"]
                    existing_interests = set(existing.get("interested_in") or [])
                    incoming_interests = set(talent.get("interested_in") or [])
                    merged_interests = sorted(existing_interests | incoming_interests)
                    if merged_interests:
                        update["interested_in"] = merged_interests
                    await db.talents.update_one({"id": existing["id"]}, {"$set": update})
                    await update_talent_cover_cache(existing["id"])
                    await db.applications.update_one(
                        {"id": aid}, {"$set": {"talent_id": existing["id"], "merged": True}}
                    )
                    return {"ok": True, "talent_id": existing["id"], "merged": True}
                raise
    return {"ok": True}


def _application_to_talent(app_doc: dict, admin_id: str) -> dict:
    fd = app_doc.get("form_data") or {}
    tid = str(uuid.uuid4())
    new_media: List[dict] = []
    cover_mid: Optional[str] = None
    for m in app_doc.get("media", []) or []:
        cat = m.get("category")
        if cat == "image":
            new_cat = "portfolio"
        elif cat == "indian":
            new_cat = "indian"
        elif cat == "western":
            new_cat = "western"
        elif cat == "intro_video":
            new_cat = "video"
        else:
            continue
        mid = str(uuid.uuid4())
        new_media.append({
            "id": mid,
            "category": new_cat,
            "url": m.get("url"),
            "public_id": m.get("public_id"),
            "resource_type": m.get("resource_type"),
            "content_type": m.get("content_type", "application/octet-stream"),
            "original_filename": m.get("original_filename"),
            "size": m.get("size", 0),
            "created_at": _now(),
            "scope": "talent_portfolio",
            "talent_id": tid,
            "duration": m.get("duration"),
            "poster_url": m.get("poster_url"),
        })
        if new_cat in ("portfolio", "indian", "western") and not cover_mid:
            cover_mid = mid
    dob = (fd.get("dob") or "").strip() or None
    age = compute_age(dob) if dob else None
    # interested_in: self-selected public work categories from the onboarding form.
    # Validate against a safe allow-list to prevent arbitrary string injection.
    VALID_INTERESTS = {
        "Acting", "Modeling", "Print Campaigns", "TV Commercials",
        "Digital Ads", "Instagram Collaborations", "Influencer Campaigns",
        "Social Media Collaborations", "Fashion Campaigns", "Brand Shoots",
        "Music Videos", "OTT / Film Projects", "Event Appearances", "Hosting / Anchoring",
    }
    raw_interests = fd.get("interested_in") or []
    interested_in = [i for i in raw_interests if isinstance(i, str) and i.strip() in VALID_INTERESTS]
    return {
        "id": tid,
        "name": f"{fd.get('first_name','')} {fd.get('last_name','')}".strip() or app_doc.get("talent_name"),
        "email": app_doc.get("talent_email"),
        "phone": (fd.get("phone") or app_doc.get("talent_phone") or None),
        "age": age,
        "dob": dob,
        "height": fd.get("height") or None,
        "location": fd.get("location") or None,
        "ethnicity": fd.get("ethnicity") or None,
        "gender": fd.get("gender") or None,
        "instagram_handle": normalize_instagram_handle(fd.get("instagram_handle") or None),
        "instagram_followers": fd.get("instagram_followers") or None,
        "bio": fd.get("bio") or None,
        "work_links": [w for w in (fd.get("work_links") or []) if isinstance(w, str) and w.strip()],
        "skills": [s for s in (fd.get("skills") or []) if isinstance(s, str) and s.strip()],
        "cover_media_id": cover_mid,
        "interested_in": interested_in,
        "tags": [],
        "media": new_media,
        "source": {
            "type": "self_onboard",
            "talent_email": app_doc.get("talent_email"),
            "reference_id": app_doc["id"],
        },
        "status": "SUBMITTED",
        "created_at": _now(),
        "created_by": admin_id,
    }
