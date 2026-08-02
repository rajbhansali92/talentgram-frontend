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
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import cloudinary
import cloudinary.api
import cloudinary.utils
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, BackgroundTasks
from pymongo.errors import DuplicateKeyError


from core import (
    APP_NAME,
    APPLICATION_DECISIONS,
    APPLICATION_UPLOAD_CATEGORIES,
    CLOUDINARY_CLOUD_NAME,
    MAX_APPLICATION_IMAGES,
    MAX_AUDITION_VIDEO_SECONDS,
    MAX_IMAGES_PER_CATEGORY,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_VIDEO_BYTES,
    MEDIA_COPY_EXCLUDE_FIELDS,
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
    parse_height_to_inches,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_token,
    media_url,
    normalize_instagram_handle,
    video_poster_url,
    update_talent_cover_cache,
    normalize_email,
    verify_email_ownership,
    rate_limit_ok,
    client_ip,
    sign_r2_media_if_needed,
    resolve_canonical_talent,
    merge_talent_profile,
    sync_media_to_global_talent,
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


def normalize_location_str(val) -> str:
    """Safely extract a display string from a location value.

    The frontend stores location as a list of dicts, e.g. [{"city": "Mumbai"}],
    but legacy data or direct API calls may send a plain string.
    Returns a stripped string (empty string if absent/empty).
    """
    if not val:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        if not val:
            return ""
        first = val[0]
        if isinstance(first, dict):
            # Extract city, state, or country — whichever is present
            return (
                str(first.get("city") or first.get("state") or first.get("country") or "")
            ).strip()
        return str(first).strip()
    return str(val).strip()


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
# ---------------------------------------------------------------------------
# Talent → Application reconciliation
# ---------------------------------------------------------------------------


async def _find_talent_by_email(email: str) -> Optional[Dict]:
    return await resolve_canonical_talent(email=email)


# Maps talent.media[].category → application media category
_TALENT_TO_APP_CATEGORY: Dict[str, str] = {
    "portfolio": "image",
    "image": "image",
    "indian": "indian",
    "western": "western",
    "video": "intro_video",
    "intro_video": "intro_video",
    "headshot": "headshot",
    "headshots": "headshot",
    "additional_portfolio": "additional_portfolio",
}


def split_full_name(name: str) -> tuple[str, str]:
    name = (name or "").strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    first = " ".join(parts[:-1])
    last = parts[-1]
    return first, last


async def _reconcile_draft_from_talent(app_doc: Dict, talent: Dict, aid: str) -> None:
    """Hydrate a sparse draft application from an existing talent profile.

    Rules:
    - Only fills fields that are missing/empty in the application form_data, unless force_refresh is True.
    - Never overwrites data already present in the application, unless force_refresh is True.
    - Media is copied/synced if stale compared to db.talents.
    """
    fd = app_doc.get("form_data") or {}
    patch: Dict[str, Any] = {}

    t_updated = talent.get("updated_at")
    app_snapshot = app_doc.get("talent_profile_updated_at")
    force_refresh = False
    if t_updated and (not app_snapshot or t_updated > app_snapshot):
        force_refresh = True

    # Full Name hydration
    talent_name = talent.get("name", "").strip()
    if talent_name:
        first, last = split_full_name(talent_name)
        if force_refresh or not fd.get("first_name"):
            patch["form_data.first_name"] = first
        if force_refresh or not fd.get("last_name"):
            patch["form_data.last_name"] = last
        if force_refresh or not app_doc.get("talent_name"):
            patch["talent_name"] = talent_name

    # Scalar form_data fields
    for field in ("location", "instagram_handle", "instagram_followers",
                  "bio", "height", "ethnicity", "gender", "dob"):
        talent_val = talent.get(field)
        if force_refresh:
            if talent_val not in (None, "", [], {}):
                patch[f"form_data.{field}"] = talent_val
        else:
            if not fd.get(field) and talent_val:
                patch[f"form_data.{field}"] = talent_val

    # List form_data fields
    for field in ("skills", "work_links", "interested_in"):
        talent_val = talent.get(field)
        if force_refresh:
            if talent_val not in (None, "", [], {}):
                patch[f"form_data.{field}"] = talent_val
        else:
            if not (fd.get(field) or []) and (talent_val or []):
                patch[f"form_data.{field}"] = talent_val

    # Media hydration — ADDITIVE ONLY (Release Blocker fix, see
    # docs/claude/08_DECISION_LOG.md D27). This function runs on every
    # `GET /public/apply/{aid}` of a non-submitted draft (a plain page
    # refresh, no user action) whenever the applicant's email matches an
    # existing talent record. The previous implementation compared
    # `len(app_media)` to `len(talent_media)` as a "has the Library
    # changed" proxy and, on any mismatch, wholesale-replaced
    # `app_doc.media` — but a count mismatch is also the completely normal
    # state for a returning talent who has uploaded MORE photos to this
    # application than exist in their Library. That heuristic could not
    # tell "Library changed" apart from "applicant added their own
    # photos", so it silently discarded the applicant's own uploads (no
    # storage cleanup either — an orphan leak on top of the data loss).
    #
    # Root cause: application.media[] items carry no marker distinguishing
    # "copied from the talent's Library" from "the applicant's own
    # upload" — unlike submission.media[], which got an `origin` field in
    # Phase 4.1 for exactly this reason. Rather than add a new marker
    # field (a schema change), this reconciliation uses the fact that a
    # hydrated item is ALWAYS created with the SAME `id` as its source
    # talent.media item (see the copy below): an application media item
    # is "already reflecting this Library item" if and only if some
    # existing item shares that same id. So the fix is purely additive —
    # copy any Library item not yet present (by id) into the draft, and
    # NEVER remove or replace an existing item for any reason. This also
    # makes it safe for already-hydrated drafts created before this fix
    # shipped, with no migration needed: their hydrated items already
    # share ids with `talent_media`, so they're recognized as "already
    # present" and are neither duplicated nor touched.
    app_media = app_doc.get("media") or []
    talent_media = talent.get("media") or []
    existing_ids = {m.get("id") for m in app_media if m.get("id")}

    # Provider-agnostic field copy (Production Certification, Phase 4 item
    # 4 — same fix as core.sync_media_to_global_talent()): copy every
    # field the talent's Library item has, except
    # core.MEDIA_COPY_EXCLUDE_FIELDS, instead of hand-picking fields. The
    # old whitelist here silently dropped `provider`/`stream_uid`/
    # `thumbnail_url`/`mime`, which breaks long-term lifecycle management
    # (delete/cleanup) for any hydrated video — see
    # core.is_media_asset_referenced() and safe_cleanup_media_storage(),
    # which both need `stream_uid` to protect/clean up Cloudflare Stream
    # assets correctly. Shared with sync_media_to_global_talent()'s
    # identical mirror logic (Release Preparation cleanup) rather than
    # maintaining two copies of the same field list.
    new_items = []
    for m in talent_media:
        mid = m.get("id")
        if not mid or mid in existing_ids:
            continue
        # Guard against a mirror-then-rehydrate loop: `upload_application_media`
        # unconditionally mirrors every fresh application upload into
        # `talents.media` (sync_media_to_global_talent, a new id each time,
        # tagged `source_application_media_id` back to the original). Without
        # this check, that mirror would look like "a new Library item" on the
        # very next reconciliation and get copied straight back into the
        # application — duplicating the applicant's own upload under a second
        # id. Skip any talent item that's just a mirror of something this
        # application already has.
        src_app_mid = m.get("source_application_media_id")
        if src_app_mid and src_app_mid in existing_ids:
            continue
        a_cat = _TALENT_TO_APP_CATEGORY.get(m.get("category", ""))
        if not a_cat:
            continue
        item = {k: v for k, v in m.items() if k not in MEDIA_COPY_EXCLUDE_FIELDS}
        item.update({
            "id": mid,
            "category": a_cat,
            "content_type": m.get("content_type", "application/octet-stream"),
            "size": m.get("size", 0),
            "created_at": m.get("created_at") or _now(),
            "scope": "application",
        })
        new_items.append(item)
    if new_items:
        patch["media"] = app_media + new_items

    if t_updated:
        patch["talent_profile_updated_at"] = t_updated

    if patch:
        await db.applications.update_one({"id": aid}, {"$set": patch})
        # Keep app_doc updated in-memory for immediate use / return
        for k, v in patch.items():
            if "." in k:
                parts = k.split(".", 1)
                if parts[0] == "form_data":
                    if "form_data" not in app_doc:
                        app_doc["form_data"] = {}
                    app_doc["form_data"][parts[1]] = v
            else:
                app_doc[k] = v


@router.post("/public/apply")
async def start_application(
    payload: ApplicationStartIn,
    request: Request = None,
    authorization: Optional[str] = Header(None),
):
    email = normalize_email(payload.email)
    if not email:
        raise HTTPException(400, "Invalid email address")

    # P1-4: burst / enumeration protection. Keyed per-IP and per-email so a
    # single attacker can neither sweep many emails from one IP nor hammer one
    # victim email from a botnet of IPs. `request` is always injected over
    # HTTP; it is only None for in-process direct calls (tests), which are not
    # an attack surface and skip the limiter.
    if request is not None:
        ip = client_ip(request)
        if not rate_limit_ok(f"apply:ip:{ip}", limit=20, window_seconds=60.0):
            raise HTTPException(429, "Too many attempts — please try again shortly")
        if not rate_limit_ok(f"apply:email:{email}", limit=10, window_seconds=300.0):
            raise HTTPException(429, "Too many attempts for this email — please try again later")

    talent = await _find_talent_by_email(email)
    talent_age = None
    if talent:
        talent_age = talent.get("age") or (compute_age(talent.get("dob")) if talent.get("dob") else None)

    existing = await db.applications.find_one({"talent_email": email})

    # P0-1: an anonymous caller must NOT be able to mint a submitter token for
    # an email that already has data. Whenever an application OR a canonical
    # talent profile already exists for this email, require proof of ownership
    # (OTP/Google portal token, or an existing valid submitter credential).
    # Brand-new emails (no existing record => no PII to leak / nothing to
    # reset) keep the friction-free first-time apply flow.
    if existing or talent:
        owns = await verify_email_ownership(authorization, email)
        if not owns:
            raise HTTPException(
                403,
                "Please verify your email to continue. We'll send you a one-time code.",
            )
    if existing:
        aid = existing["id"]
        token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
        if existing.get("status") == "submitted":
            # Reset submitted application to draft
            await db.applications.update_one(
                {"id": aid},
                {"$set": {
                    "status": "draft",
                    "form_data": {
                        "first_name": payload.first_name.strip(),
                        "last_name": payload.last_name.strip(),
                    },
                    "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
                    "talent_phone": payload.phone,
                    "alternate_contact_number": payload.alternate_contact_number,
                    "media": [],
                    "submitted_at": None,
                    "decision": "pending",
                    "talent_profile_updated_at": None,
                    "access_token": token,
                    "profile_id": payload.profile_id or existing.get("profile_id"),
                }}
            )
            # Reconcile immediately
            if talent:
                refreshed = await db.applications.find_one({"id": aid})
                await _reconcile_draft_from_talent(refreshed or existing, talent, aid)
            return {"id": aid, "token": token, "resumed": True}
        else:
            # Resume existing draft
            await db.applications.update_one(
                {"id": aid},
                {"$set": {
                    "form_data.first_name": payload.first_name.strip(),
                    "form_data.last_name": payload.last_name.strip(),
                    "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
                    "talent_phone": payload.phone,
                    "alternate_contact_number": payload.alternate_contact_number,
                    "access_token": token,
                    "profile_id": payload.profile_id or existing.get("profile_id"),
                }},
            )
            if talent:
                refreshed = await db.applications.find_one({"id": aid})
                await _reconcile_draft_from_talent(refreshed or existing, talent, aid)
            return {"id": aid, "token": token, "resumed": True}

    # If it is a completely new application
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
        "alternate_contact_number": payload.alternate_contact_number,
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
        "talent_profile_updated_at": None,
    }
    try:
        await db.applications.insert_one(doc)
        if talent:
            await _reconcile_draft_from_talent(doc, talent, aid)
    except DuplicateKeyError:
        # Race: another tab/device created it
        existing = await db.applications.find_one({"talent_email": email})
        if existing:
            aid = existing["id"]
            token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
            if existing.get("status") == "submitted":
                await db.applications.update_one(
                    {"id": aid},
                    {"$set": {
                        "status": "draft",
                        "form_data": {
                            "first_name": payload.first_name.strip(),
                            "last_name": payload.last_name.strip(),
                        },
                        "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
                        "talent_phone": payload.phone,
                        "alternate_contact_number": payload.alternate_contact_number,
                        "media": [],
                        "submitted_at": None,
                        "decision": "pending",
                        "talent_profile_updated_at": None,
                        "access_token": token,
                        "profile_id": payload.profile_id or existing.get("profile_id"),
                    }}
                )
                if talent:
                    refreshed = await db.applications.find_one({"id": aid})
                    await _reconcile_draft_from_talent(refreshed or existing, talent, aid)
                return {"id": aid, "token": token, "resumed": True}
            else:
                await db.applications.update_one(
                    {"id": aid},
                    {"$set": {
                        "access_token": token,
                        "profile_id": payload.profile_id or existing.get("profile_id"),
                    }}
                )
                if talent:
                    refreshed = await db.applications.find_one({"id": aid})
                    await _reconcile_draft_from_talent(refreshed or existing, talent, aid)
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

    # Lazy reconciliation: the localStorage resume path skips POST /apply
    # entirely, so reconciliation from startApplication never runs.
    # Guard: only attempt on non-submitted drafts.
    email = (app_doc.get("talent_email") or "").lower().strip()
    if app_doc.get("status") != "submitted":
        if email:
            talent = await _find_talent_by_email(email)
            if talent:
                await _reconcile_draft_from_talent(app_doc, talent, aid)
                # Re-fetch so we return the freshly hydrated document
                app_doc = await db.applications.find_one({"id": aid}, {"_id": 0}) or app_doc

    response_data = dict(app_doc)
    if email:
        talent = await _find_talent_by_email(email)
        if talent:
            name_parts = talent.get("name", "").strip().split(" ", 1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            response_data["talent"] = {
                "first_name": first_name,
                "last_name": last_name,
                "phone": talent.get("phone") or "",
            }

    return sign_r2_media_if_needed(response_data)


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
        ph = merged.get("phone")
        if ph:
            update["talent_phone"] = ph
        alt_ph = merged.get("alternate_contact_number")
        if alt_ph:
            update["alternate_contact_number"] = alt_ph

        # Effective age resolves strictly from DOB — no override fields.
        dob = merged.get("dob")
        effective_age_val = compute_age(dob) if dob else None
        # Fallback: check existing talent record for a stored age
        if effective_age_val is None:
            email = app_doc.get("talent_email")
            if email:
                talent_doc = await resolve_canonical_talent(email=email)
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
        if ct in {"image/bmp", "image/tiff"} or fn.endswith((".bmp", ".tiff")):
            raise HTTPException(400, "BMP and TIFF formats are not supported. Please upload JPEG, PNG, or HEIC.")
        if not (ct.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))):
            raise HTTPException(400, "Unsupported image format. Please upload JPG, PNG, WEBP, or HEIC.")

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

    from core import sync_media_to_global_talent
    await sync_media_to_global_talent(updated, media)

    # Drive secondary backup — applications don't have a brand, use a stable bucket.
    if drive_enabled():
        media["scope"] = "application"
        enqueue_drive_upload(db, media, updated, "_Applications", data)

    return updated


class SignAppUploadIn(BaseModel):
    category: str
    filename: str


@router.post("/public/apply/{aid}/upload/sign")
async def sign_application_upload(
    aid: str,
    payload: SignAppUploadIn,
    authorization: Optional[str] = Header(None),
):
    from core import DIRECT_UPLOAD_ENABLED
    if not DIRECT_UPLOAD_ENABLED:
        raise HTTPException(400, "Direct uploads are currently disabled")
        
    await _check_app_token(authorization, aid)

    category = payload.category
    filename = payload.filename
    
    if category not in APPLICATION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
    if category == "intro_video":
        raise HTTPException(400, "intro_video must use chunked video-signature endpoint")
        
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    existing = sum(1 for m in app_doc.get("media", []) if m.get("category") == category)
    if existing >= MAX_IMAGES_PER_CATEGORY:
        raise HTTPException(400, "Limit reached")

    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/applications/{aid}"
    public_id = media_id
    rt = "image"
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
        "transformation": None,
        "media_id": media_id,
    }


class CompleteAppUploadIn(BaseModel):
    media_id: str
    category: str
    public_id: str
    url: str
    bytes: int
    duration: Optional[float] = None
    content_type: Optional[str] = None
    original_filename: Optional[str] = None
    eager: Optional[List[dict]] = None


@router.post("/public/apply/{aid}/upload/complete")
async def complete_application_upload(
    aid: str,
    payload: CompleteAppUploadIn,
    authorization: Optional[str] = Header(None),
):
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    category = payload.category
    if category == "intro_video":
        raise HTTPException(400, "intro_video must use chunked video-complete endpoint")

    url = payload.url
    thumbnail_url = media_url(payload.public_id, preset="thumb", resource_type="image")

    media = {
        "id": payload.media_id,
        "category": category,
        "url": url,
        "public_id": payload.public_id,
        "resource_type": "image",
        "content_type": payload.content_type or "image/jpeg",
        "original_filename": payload.original_filename,
        "size": payload.bytes,
        "created_at": _now(),
        "scope": "application",
        "application_id": aid,
        "duration": None,
        "thumbnail_url": thumbnail_url,
        "poster_url": None,
    }

    await db.applications.update_one({"id": aid}, {"$push": {"media": media}})

    # ITEM 1 — asset_metadata parity with the Project Submission Link. Submission's
    # image upload/complete writes a "completed" ledger row; apply previously did
    # not, leaving the storage ledger inconsistent. Mirror the reference shape
    # (idempotent upsert by public_id). Image storage is unchanged (Cloudinary).
    talent = await _find_talent_by_email(app_doc.get("talent_email") or "")
    tid = (talent or {}).get("id") or "unknown_talent"
    tname = (talent or {}).get("name")
    try:
        await db.asset_metadata.update_one(
            {"public_id": payload.public_id},
            {"$set": {
                "id": payload.media_id,
                "public_id": payload.public_id,
                "folder": f"{APP_NAME}/applications/{aid}",
                "resource_type": "image",
                "asset_type": "profile_image",
                "talent_id": tid,
                "talent_name": tname,
                "application_id": aid,
                "file_size": payload.bytes,
                "created_at": _now(),
                "status": "completed",
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"app-upload-complete: asset_metadata write failed {payload.public_id}: {e}")

    updated = await db.applications.find_one({"id": aid}, {"_id": 0})

    from core import sync_media_to_global_talent
    await sync_media_to_global_talent(updated, media)

    return updated


class AppVideoSignatureIn(BaseModel):
    category: str
    label: Optional[str] = None
    content_type: Optional[str] = None
    public_id: Optional[str] = None


@router.post("/public/apply/{aid}/video-signature")
async def app_video_signature(
    aid: str,
    payload: AppVideoSignatureIn,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Issue a short-lived signed Cloudinary upload for the application intro video slot."""
    from datetime import datetime, timezone
    from routers.submissions import _prefill_rate_limit_ok
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many requests — please slow down")
    await _check_app_token(authorization, aid)
    category = (payload.category or "").strip()
    if category != "intro_video":
        raise HTTPException(400, "Invalid application video category")
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    folder = f"{APP_NAME}/applications/{aid}"
    is_resign = bool(payload.public_id)
    # An R2 re-sign carries the FULL object key (the client echoes back the
    # `public_id` we returned, which is the key itself). Carry it through
    # verbatim — re-deriving the key from it would nest the whole key inside a
    # fresh prefix and sign a DIFFERENT object than the upload is targeting.
    resign_r2_key = None

    if is_resign:
        # Support R2 key structures on resign
        is_r2_key = payload.public_id.startswith("raw-uploads/")
        if is_r2_key:
            if not payload.public_id.startswith(f"raw-uploads/applications/{aid}/"):
                raise HTTPException(400, "Invalid public_id for this application")
            public_id = payload.public_id
            resign_r2_key = payload.public_id
        else:
            if payload.public_id != "intro_video":
                raise HTTPException(400, "Invalid public_id for this application")
            public_id = payload.public_id
    else:
        # Pre-pull existing intro video to keep it clean
        await db.applications.update_one(
            {"id": aid}, {"$pull": {"media": {"category": "intro_video"}}}
        )
        public_id = "intro_video"

    from core import ENABLE_R2_MEDIA_PIPELINE, generate_r2_presigned_url
    if ENABLE_R2_MEDIA_PIPELINE:
        # P2 fix: the R2 leaf must be unique per FRESH upload attempt (a
        # re-sign still reuses resign_r2_key verbatim, unaffected). The old
        # fixed leaf meant re-recording while Cloudflare Stream was still
        # fetching the PREVIOUS object overwrote it mid-fetch, corrupting that
        # ingest and silently clobbering its asset_metadata row via the second
        # upload's upsert on the same key. Scoped to the R2 leaf only —
        # Cloudinary's public_id (used when ENABLE_R2_MEDIA_PIPELINE is off)
        # is untouched; its overwrite:"true" replacement is a different,
        # non-racy mechanism, and a unique id there would leak Cloudinary
        # storage instead of fixing anything.
        r2_leaf = f"{public_id}_{uuid.uuid4().hex[:8]}" if not resign_r2_key else public_id
        r2_key = resign_r2_key or f"raw-uploads/applications/{aid}/{category}/{r2_leaf}.mp4"
        upload_url = generate_r2_presigned_url(r2_key, "PUT")
        try:
            await db.asset_metadata.update_one(
                {"public_id": r2_key},
                {"$set": {
                    "public_id": r2_key,
                    "application_id": aid,
                    "category": category,
                    "asset_type": "intro_video",
                    "resource_type": "video",
                    "upload_status": "pending",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"app-video-signature R2: pending asset_metadata write failed: {e}")
        return {
            "use_r2": True,
            "upload_url": upload_url,
            "public_id": r2_key,
            "folder": folder,
            "filename": f"{public_id}.mp4",
            "max_duration_seconds": MAX_AUDITION_VIDEO_SECONDS,
        }

    eager = "c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4|c_fill,h_338,w_600,q_auto/f_jpg"
    tags = f"application_id={aid},category=intro_video,asset_kind=application_video"
    timestamp = int(time.time())
    params_to_sign = {
        "timestamp": timestamp,
        "folder": folder,
        "public_id": public_id,
        "eager": eager,
        "eager_async": "true",
        "overwrite": "true",
        "tags": tags,
    }
    cfg = cloudinary.config()
    signature = cloudinary.utils.api_sign_request(params_to_sign, cfg.api_secret)

    try:
        full_public_id = f"{folder}/{public_id}"
        await db.asset_metadata.update_one(
            {"public_id": full_public_id},
            {"$set": {
                "public_id": full_public_id,
                "application_id": aid,
                "category": category,
                "asset_type": "intro_video",
                "resource_type": "video",
                "upload_status": "pending",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"app-video-signature: pending asset_metadata write failed {folder}/{public_id}: {e}")

    return {
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key": cfg.api_key,
        "timestamp": timestamp,
        "signature": signature,
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload",
        "params": {
            "folder": folder,
            "public_id": public_id,
            "eager": eager,
            "eager_async": "true",
            "overwrite": "true",
            "tags": tags,
        },
        "max_duration_seconds": MAX_AUDITION_VIDEO_SECONDS,
    }


class AppVideoCompleteIn(BaseModel):
    public_id: str
    secure_url: Optional[str] = None
    url: Optional[str] = None
    bytes: int
    duration: Optional[float] = None
    format: Optional[str] = None


@router.post("/public/apply/{aid}/video-complete")
async def app_video_complete(
    aid: str,
    payload: AppVideoCompleteIn,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None),
):
    """Attach the just-uploaded chunked video to the application."""
    from datetime import datetime, timezone
    from core import generate_r2_presigned_url, trigger_cloudinary_transcode
    await _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    folder = f"{APP_NAME}/applications/{aid}"
    public_id = (payload.public_id or "").strip()

    is_r2 = public_id.startswith("raw-uploads/")
    if is_r2:
        if not public_id.startswith(f"raw-uploads/applications/{aid}/"):
            raise HTTPException(400, "Asset does not belong to this application")

        parts = public_id.split("/")
        category = parts[3]
        leaf_pid = parts[4].split(".")[0]

        r2_read_url = generate_r2_presigned_url(public_id, "GET", expiry=86400)

        # Register the media asset in Mongo with processing status
        media = {
            "id": str(uuid.uuid4()),
            "category": "intro_video",
            "url": None,  # Transcoding in progress
            "public_id": f"{folder}/{leaf_pid}",
            "resource_type": "video",
            "content_type": "video/mp4",
            "original_filename": None,
            "size": payload.bytes or 0,
            "created_at": _now(),
            "scope": "application",
            "application_id": aid,
            "duration": None,
            "thumbnail_url": None,
            "poster_url": None,
            "status": "processing",
        }

        # Single-slot replacement for intro_video
        await db.applications.update_one({"id": aid}, {"$pull": {"media": {"category": "intro_video"}}})
        await db.applications.update_one({"id": aid}, {"$push": {"media": media}})

        # Update metadata state to "processing"
        try:
            await db.asset_metadata.update_one(
                {"public_id": public_id},
                {"$set": {"upload_status": "processing", "updated_at": datetime.now(timezone.utc)}},
            )
        except Exception as e:
            logger.warning(f"app-video-complete R2: asset_metadata update failed: {e}")

        # Enqueue Cloudinary transcode job in background
        background_tasks.add_task(
            trigger_cloudinary_transcode,
            media_id=media["id"],
            r2_url=r2_read_url,
            folder=folder,
            public_id=leaf_pid,
            eager_transformation="c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4|c_fill,h_338,w_600,q_auto/f_jpg",
            scope="application",
            parent_id=aid,
            category="intro_video",
        )

        return {"ok": True, "media": media}

    if not public_id.startswith(folder + "/"):
        raise HTTPException(400, "Asset does not belong to this application")

    duration = payload.duration
    asset: Dict[str, Any] = {
        "public_id": public_id,
        "secure_url": payload.secure_url or payload.url,
        "bytes": payload.bytes,
        "duration": payload.duration,
    }
    try:
        res = cloudinary.api.resource(public_id, resource_type="video", tags=True)
        duration = res.get("duration", duration)
        asset = res
    except Exception as e:
        logger.warning(f"app-video-complete: resource fetch failed {public_id}: {e}")

    media = {
        "id": str(uuid.uuid4()),
        "category": "intro_video",
        "url": asset.get("secure_url") or asset.get("url"),
        "public_id": public_id,
        "resource_type": "video",
        "content_type": "video/mp4",
        "original_filename": None,
        "size": asset.get("bytes") or 0,
        "created_at": _now(),
        "scope": "application",
        "application_id": aid,
        "duration": duration,
        "thumbnail_url": video_poster_url(public_id),
        "poster_url": video_poster_url(public_id),
    }

    await db.applications.update_one({"id": aid}, {"$push": {"media": media}})
    try:
        await db.asset_metadata.update_one(
            {"public_id": public_id},
            {"$set": {"upload_status": "completed", "updated_at": datetime.now(timezone.utc)}},
        )
    except Exception as e:
        logger.warning(f"app-video-complete: asset_metadata update failed {public_id}: {e}")

    updated = await db.applications.find_one({"id": aid}, {"_id": 0})
    from core import sync_media_to_global_talent
    await sync_media_to_global_talent(updated, media)

    return {"ok": True, "media": media}


class AppVideoUploadEventIn(BaseModel):
    public_id: str
    stage: str
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    bytes_transferred: Optional[int] = None
    upload_duration_ms: Optional[float] = None
    # Diagnostic-only fields (P0 real-user-failure investigation) — see the
    # matching model in routers/submissions.py for the full rationale.
    attempt_duration_ms: Optional[float] = None
    ms_since_last_progress: Optional[float] = None
    visibility_events: Optional[List[Dict[str, Any]]] = None
    file_size: Optional[int] = None
    file_type: Optional[str] = None
    client: Optional[Dict[str, Any]] = None


@router.post("/public/apply/{aid}/video-upload-event")
async def app_video_upload_event(
    aid: str,
    payload: AppVideoUploadEventIn,
    authorization: Optional[str] = Header(None),
):
    """Best-effort upload telemetry beacon (P0 upload-reliability fix). See
    submissions.video_upload_event for the rationale — same contract, mirrored
    for the Talent Invite Link's application flow. Never blocks or fails the
    upload itself — always 200s."""
    from core import record_upload_telemetry
    try:
        await _check_app_token(authorization, aid)
    except HTTPException:
        return {"ok": False}
    if not payload.public_id.startswith(f"raw-uploads/applications/{aid}/"):
        return {"ok": False}
    await record_upload_telemetry(
        payload.public_id,
        payload.stage,
        error_type=payload.error_type,
        error_message=payload.error_message,
        retry_count=payload.retry_count,
        bytes_transferred=payload.bytes_transferred,
        upload_duration_ms=payload.upload_duration_ms,
        client_info=payload.client,
        extra={
            "attempt_duration_ms": payload.attempt_duration_ms,
            "ms_since_last_progress": payload.ms_since_last_progress,
            "visibility_events": payload.visibility_events,
            "file_size": payload.file_size,
            "file_type": payload.file_type,
        },
    )
    return {"ok": True}


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
    target_media = next((m for m in (app_doc.get("media") or []) if m.get("id") == mid), None)
    await db.applications.update_one({"id": aid}, {"$pull": {"media": {"id": mid}}})

    from core import remove_synced_media_from_global_talent
    await remove_synced_media_from_global_talent(app_doc, mid)
    # Parity sprint: best-effort storage cleanup (mirrors submission delete).
    # Reference-aware (Production Certification, Phase 4 item 4): `target_media`
    # may be a talent-media item hydrated by value into this application (see
    # the `should_hydrate_media` block above) — must not destroy an asset the
    # talent's Library, or a submission, still depends on.
    if target_media:
        from core import safe_cleanup_media_storage
        await safe_cleanup_media_storage(target_media, scope="application", parent_id=aid)

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


async def _promote_application_to_talent(
    app_doc: dict, admin_id: Optional[str], source: str
) -> tuple[Optional[str], bool]:
    """Create-or-merge this application's data into `db.talents` — the one
    place that does so, used by both `finalize_application` (talent-initiated,
    the normal path under the Talent Profile Migration Phase 2 policy: every
    completed application becomes a Talent immediately) and
    `set_application_decision` (a back-compat fallback for applications
    finalized before this migration that have no `talent_id` yet). Mirrors
    `/submit` finalize: field merge via `merge_talent_profile()` (protects
    admin-owned REVIEW_FIELDS instead of silently overwriting them), media
    via per-item `sync_media_to_global_talent()` (safe/additive — replaces
    the old full-array `$set` that could wipe reusable media not part of
    this one application). Returns (talent_id, merged).
    """
    email = normalize_email(app_doc.get("talent_email"))
    if not email:
        return None, False
    fd = app_doc.get("form_data") or {}
    dob = (fd.get("dob") or "").strip() or None

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

    form_to_merge = {
        "name": name_combined or app_doc.get("talent_name") or "Unnamed",
        "email": email,
        "normalized_email": email,
        "phone": fd.get("phone") or app_doc.get("talent_phone") or None,
        "alternate_contact_number": fd.get("alternate_contact_number") or app_doc.get("alternate_contact_number") or None,
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
        "interested_in": interested_in,
    }

    async def _merge_into(talent_doc: dict) -> str:
        # Phase 1 — Canonical Profile Monotonicity: gate on the application's
        # own talent_profile_updated_at (already stamped by
        # _reconcile_draft_from_talent while the application was still a
        # draft) exactly like /submit's finalize does with its
        # talent_profile_snapshot_at — same fail-safe semantics, same reason:
        # form_to_merge is this application's own possibly-stale snapshot,
        # regardless of whether a talent or an admin triggered this call.
        await merge_talent_profile(
            talent_doc, form_to_merge, source,
            snapshot_at=app_doc.get("talent_profile_updated_at"),
        )
        for m in app_doc.get("media", []) or []:
            await sync_media_to_global_talent(app_doc, m, skip_cover_cache=True)
        await update_talent_cover_cache(talent_doc["id"])
        return talent_doc["id"]

    existing_talent = await resolve_canonical_talent(email=email)
    if existing_talent:
        return await _merge_into(existing_talent), True

    new_talent = _application_to_talent(app_doc, admin_id or "auto-application")
    try:
        await db.talents.insert_one(new_talent)
        await update_talent_cover_cache(new_talent["id"])
        return new_talent["id"], False
    except DuplicateKeyError:
        # Race: another finalize/approval for the same email won.
        existing_talent = await resolve_canonical_talent(email=email)
        if existing_talent:
            return await _merge_into(existing_talent), True
        return None, False


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
        if not normalize_location_str(fd.get("location")):
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

    # Talent Profile Migration, Phase 2: every completed application becomes
    # a canonical Talent Profile immediately — project decision (admin
    # approve/reject) never gates whether a Talent exists.
    updated_app = await db.applications.find_one({"id": aid})
    talent_id, merged = await _promote_application_to_talent(updated_app, None, "profile_application")
    if talent_id:
        await db.applications.update_one(
            {"id": aid},
            {"$set": {"talent_id": talent_id, "merged": merged}},
        )
    return {"ok": True, "talent_id": talent_id, "merged": merged}


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
        return [sign_r2_media_if_needed(_with_image_url(a)) for a in items]
    skip, page_size, p, s = _paginate_params(page, size, limit)
    total = await db.applications.count_documents(query)
    items = await cursor.skip(skip).limit(page_size).to_list(page_size)
    return _paginated([sign_r2_media_if_needed(_with_image_url(a)) for a in items], total, p, s)


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
    return sign_r2_media_if_needed(_with_image_url(app_doc))


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

    if app_doc.get("decision") == payload.decision:
        return {"ok": True, "talent_id": app_doc.get("talent_id"), "merged": app_doc.get("merged", True)}

    # Persist decision. Talent Profile Migration, Phase 2: project decision
    # (approve/reject) never gates whether a Talent exists — the canonical
    # Talent Profile is created/merged at finalize time now, for every
    # completed application, regardless of how it's later decided.
    await db.applications.update_one(
        {"id": aid},
        {"$set": {"decision": payload.decision, "decided_at": _now(), "decided_by": admin["id"]}},
    )

    # Back-compat fallback only: an application finalized before this
    # migration (or missing talent_id for any other reason) has no linked
    # Talent yet. Promote it now, via the exact same helper finalize uses,
    # so there is still only one implementation of this operation.
    if payload.decision == "approved" and not app_doc.get("talent_id"):
        talent_id, merged = await _promote_application_to_talent(app_doc, admin["id"], "application_approval")
        if talent_id:
            await db.applications.update_one(
                {"id": aid}, {"$set": {"talent_id": talent_id, "merged": merged}}
            )
            return {"ok": True, "talent_id": talent_id, "merged": merged}
    return {"ok": True, "talent_id": app_doc.get("talent_id"), "merged": app_doc.get("merged", False)}


def _application_to_talent(app_doc: dict, admin_id: str) -> dict:
    fd = app_doc.get("form_data") or {}
    tid = str(uuid.uuid4())
    new_media: List[dict] = []
    cover_mid: Optional[str] = None
    for m in app_doc.get("media", []) or []:
        cat = m.get("category")
        if cat in ("image", "portfolio"):
            new_cat = "portfolio"
        elif cat == "indian":
            new_cat = "indian"
        elif cat == "western":
            new_cat = "western"
        elif cat in ("intro_video", "video"):
            new_cat = "video"
        elif cat in ("headshot", "headshots"):
            new_cat = "headshot"
        elif cat == "additional_portfolio":
            new_cat = "additional_portfolio"
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
            "source_application_media_id": m.get("id"),
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
    email = normalize_email(app_doc.get("talent_email"))
    return {
        "id": tid,
        "name": f"{fd.get('first_name','')} {fd.get('last_name','')}".strip() or app_doc.get("talent_name"),
        "email": email,
        "normalized_email": email,
        "phone": (fd.get("phone") or app_doc.get("talent_phone") or None),
        "alternate_contact_number": (fd.get("alternate_contact_number") or app_doc.get("alternate_contact_number") or None),
        "age": age,
        "dob": dob,
        "height": fd.get("height") or None,
        "height_inches": parse_height_to_inches(fd.get("height")),
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
            "talent_email": email,
            "reference_id": app_doc["id"],
        },
        "status": "SUBMITTED",
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": admin_id,
    }
