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

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
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
    cloudinary_upload,
    compute_age,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_token,
)
from drive_backup import drive_enabled, enqueue_drive_upload

router = APIRouter(prefix="/api", tags=["applications"])


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
        # Update base fields in case they changed
        await db.applications.update_one(
            {"id": aid},
            {"$set": {
                "form_data.first_name": payload.first_name.strip(),
                "form_data.last_name": payload.last_name.strip(),
                "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
                "talent_phone": payload.phone,
            }},
        )
        token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
        return {"id": aid, "token": token, "resumed": True}

    aid = str(uuid.uuid4())
    doc = {
        "id": aid,
        "talent_email": email,
        "talent_phone": payload.phone,
        "talent_name": f"{payload.first_name} {payload.last_name}".strip(),
        "form_data": {
            "first_name": payload.first_name.strip(),
            "last_name": payload.last_name.strip(),
        },
        "media": [],
        "status": "draft",
        "decision": "pending",
        "created_at": _now(),
        "submitted_at": None,
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
            return {"id": aid, "token": token, "resumed": True}
        raise HTTPException(409, "An application already exists for this email")
    token = make_token({"role": "submitter", "sid": aid, "kind": "application"}, days=7)
    return {"id": aid, "token": token, "resumed": False}


def _check_app_token(authorization: Optional[str], aid: str) -> Dict[str, Any]:
    data = decode_submitter(authorization)
    if not data or data.get("sid") != aid or data.get("kind") != "application":
        raise HTTPException(401, "Invalid application token")
    return data


@router.get("/public/apply/{aid}")
async def get_application(aid: str, authorization: Optional[str] = Header(None)):
    _check_app_token(authorization, aid)
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
    _check_app_token(authorization, aid)
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
    if update:
        await db.applications.update_one({"id": aid}, {"$set": update})
    return await db.applications.find_one({"id": aid}, {"_id": 0})


@router.post("/public/apply/{aid}/upload")
async def upload_application_media(
    aid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    _check_app_token(authorization, aid)
    if category not in APPLICATION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category (image|indian|western|intro_video)")
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

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
    data = await file.read()

    size_bytes = len(data)
    if category == "intro_video" and size_bytes > MAX_SUBMISSION_VIDEO_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
        raise HTTPException(
            400,
            f"Video is too large ({mb} MB). Max {cap_mb} MB — please compress and retry.",
        )
    if category in ("image", "indian", "western") and size_bytes > MAX_SUBMISSION_IMAGE_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(
            400, f"Image is too large ({mb} MB). Max {cap_mb} MB per image."
        )

    # Cloudinary auto-detects video/image/raw by content type. Server-side
    # resize is dropped (v37m) — Cloudinary URL transforms (f_auto, q_auto,
    # w_1600) do this on demand at delivery time.
    rt = "video" if (category == "intro_video" or (file.content_type or "").startswith("video/")) else "image"
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=media_id,
        resource_type=rt,
        content_type=file.content_type,
    )
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
    _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")
    await db.applications.update_one({"id": aid}, {"$pull": {"media": {"id": mid}}})
    return {"ok": True}


@router.post("/public/apply/{aid}/finalize")
async def finalize_application(aid: str, authorization: Optional[str] = Header(None)):
    _check_app_token(authorization, aid)
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    fd = app_doc.get("form_data") or {}
    for field in ("first_name", "last_name", "dob", "height", "location", "gender"):
        if not (fd.get(field) or "").strip() if isinstance(fd.get(field), str) else not fd.get(field):
            raise HTTPException(400, f"{field.replace('_',' ').title()} is required")
    media = app_doc.get("media", [])
    # Phase 1 v37c: balanced media requirement.
    # - At least 1 portfolio/headshot image is REQUIRED (so admins always
    #   have a recognisable photo to review).
    # - Introduction video and additional images are OPTIONAL (recommended).
    img_count = sum(1 for m in media if m["category"] in ("image", "indian", "western"))
    if img_count < 1:
        raise HTTPException(
            400,
            "Please upload at least 1 clear profile/headshot image to continue.",
        )
    await db.applications.update_one(
        {"id": aid},
        {"$set": {"status": "submitted", "submitted_at": _now()}},
    )
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
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if decision:
        query["decision"] = decision
    cursor = db.applications.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None:
        return await cursor.to_list(5000)
    skip, limit, p, s = _paginate_params(page, size)
    total = await db.applications.count_documents(query)
    items = await cursor.skip(skip).limit(limit).to_list(limit)
    return _paginated(items, total, p, s)


@router.get("/applications/{aid}")
async def get_admin_application(aid: str, admin: dict = Depends(current_team_or_admin)):
    app_doc = await db.applications.find_one({"id": aid}, {"_id": 0})
    if not app_doc:
        raise HTTPException(404, "Application not found")
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
            for key in ("email", "phone", "age", "dob", "height", "location", "ethnicity", "gender", "instagram_handle", "instagram_followers", "bio"):
                if not existing.get(key) and talent.get(key):
                    update[key] = talent[key]
            # work_links: extend (dedupe) only if existing list is empty.
            if not (existing.get("work_links") or []) and talent.get("work_links"):
                update["work_links"] = talent["work_links"]
            if not existing.get("cover_media_id") and talent.get("cover_media_id"):
                update["cover_media_id"] = talent["cover_media_id"]
            await db.talents.update_one({"id": existing["id"]}, {"$set": update})
            await db.applications.update_one(
                {"id": aid}, {"$set": {"talent_id": existing["id"], "merged": True}}
            )
            return {"ok": True, "talent_id": existing["id"], "merged": True}
        else:
            await db.talents.insert_one(talent)
            await db.applications.update_one(
                {"id": aid}, {"$set": {"talent_id": talent["id"], "merged": False}}
            )
            return {"ok": True, "talent_id": talent["id"], "merged": False}
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
        })
        if new_cat in ("portfolio", "indian", "western") and not cover_mid:
            cover_mid = mid
    dob = (fd.get("dob") or "").strip() or None
    age = compute_age(dob) if dob else None
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
        "instagram_handle": fd.get("instagram_handle") or None,
        "instagram_followers": fd.get("instagram_followers") or None,
        "bio": fd.get("bio") or None,
        "work_links": [w for w in (fd.get("work_links") or []) if isinstance(w, str) and w.strip()],
        "cover_media_id": cover_mid,
        "media": new_media,
        "source": {
            "type": "self_onboard",
            "talent_email": app_doc.get("talent_email"),
            "reference_id": app_doc["id"],
        },
        "created_at": _now(),
        "created_by": admin_id,
    }
