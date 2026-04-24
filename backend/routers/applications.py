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

from core import (
    APP_NAME,
    APPLICATION_DECISIONS,
    APPLICATION_UPLOAD_CATEGORIES,
    MAX_APPLICATION_IMAGES,
    MIN_APPLICATION_IMAGES,
    ApplicationStartIn,
    SubmissionDecisionIn,
    SubmissionUpdateIn,
    _now,
    compute_age,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_token,
    put_object,
)

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
    await db.applications.insert_one(doc)
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
        raise HTTPException(400, "Invalid category (image|intro_video)")
    app_doc = await db.applications.find_one({"id": aid})
    if not app_doc:
        raise HTTPException(404, "Application not found")
    if app_doc.get("status") == "submitted":
        raise HTTPException(400, "Application already finalized")

    if category == "image":
        existing = sum(1 for m in app_doc.get("media", []) if m["category"] == "image")
        if existing >= MAX_APPLICATION_IMAGES:
            raise HTTPException(400, f"Image limit reached ({MAX_APPLICATION_IMAGES})")
    if category == "intro_video":
        # Single slot; replace previous if exists
        await db.applications.update_one(
            {"id": aid}, {"$pull": {"media": {"category": "intro_video"}}}
        )

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/applications/{aid}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("size", len(data)),
        "created_at": _now(),
        "scope": "application",
        "application_id": aid,
    }
    await db.applications.update_one({"id": aid}, {"$push": {"media": media}})
    return await db.applications.find_one({"id": aid}, {"_id": 0})


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
    if not any(m["category"] == "intro_video" for m in media):
        raise HTTPException(400, "Introduction video is required")
    img_count = sum(1 for m in media if m["category"] == "image")
    if img_count < MIN_APPLICATION_IMAGES:
        raise HTTPException(
            400,
            f"At least {MIN_APPLICATION_IMAGES} images required (you have {img_count})",
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
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if decision:
        query["decision"] = decision
    items = await db.applications.find(query, {"_id": 0}).sort("created_at", -1).to_list(5000)
    return items


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
            for key in ("email", "age", "dob", "height", "location", "ethnicity", "gender", "instagram_handle", "instagram_followers", "bio"):
                if not existing.get(key) and talent.get(key):
                    update[key] = talent[key]
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
        elif cat == "intro_video":
            new_cat = "video"
        else:
            continue
        mid = str(uuid.uuid4())
        new_media.append({
            "id": mid,
            "category": new_cat,
            "storage_path": m["storage_path"],
            "content_type": m.get("content_type", "application/octet-stream"),
            "original_filename": m.get("original_filename"),
            "size": m.get("size", 0),
            "created_at": _now(),
            "scope": "talent_portfolio",
            "talent_id": tid,
        })
        if new_cat == "portfolio" and not cover_mid:
            cover_mid = mid
    dob = (fd.get("dob") or "").strip() or None
    age = compute_age(dob) if dob else None
    return {
        "id": tid,
        "name": f"{fd.get('first_name','')} {fd.get('last_name','')}".strip() or app_doc.get("talent_name"),
        "email": app_doc.get("talent_email"),
        "age": age,
        "dob": dob,
        "height": fd.get("height") or None,
        "location": fd.get("location") or None,
        "ethnicity": None,
        "gender": fd.get("gender") or None,
        "instagram_handle": fd.get("instagram_handle") or None,
        "instagram_followers": fd.get("instagram_followers") or None,
        "bio": fd.get("bio") or None,
        "work_links": [],
        "cover_media_id": cover_mid,
        "media": new_media,
        "source": {
            "type": "open_application",
            "application_id": app_doc["id"],
            "talent_email": app_doc.get("talent_email"),
        },
        "created_at": _now(),
        "created_by": admin_id,
    }
