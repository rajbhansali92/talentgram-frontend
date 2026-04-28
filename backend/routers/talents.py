"""Talent CRUD + media management."""
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pymongo.errors import DuplicateKeyError
from core import (
    APP_NAME,
    BulkDeleteIn,
    TalentIn,
    TalentOut,
    _now,
    _paginate_params,
    _paginated,
    cloudinary_destroy,
    cloudinary_upload,
    current_admin,
    current_team_or_admin,
    db,
    enrich_talent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["talents"])


@router.post("/talents", response_model=TalentOut)
async def create_talent(payload: TalentIn, admin: dict = Depends(current_team_or_admin)):
    """Phase 0: email is the canonical identity. If a talent with this
    email already exists, MERGE non-empty incoming fields into the
    existing record instead of inserting a duplicate. Admins can still
    create email-less talents (e.g. legacy) — those bypass the dedup.
    """
    doc = payload.model_dump()
    raw_email = (doc.get("email") or "")
    if isinstance(raw_email, str):
        raw_email = raw_email.strip().lower() or None
        doc["email"] = raw_email

    if raw_email:
        existing = await db.talents.find_one(
            {"$or": [
                {"email": raw_email},
                {"source.talent_email": raw_email},
            ]},
            {"_id": 0},
        )
        if existing:
            # Fill empty fields ONLY (never overwrite existing data).
            patch: Dict[str, Any] = {}
            for k, v in doc.items():
                if k in {"id", "media", "created_at", "created_by"}:
                    continue
                if v in (None, "", [], {}):
                    continue
                if not existing.get(k):
                    patch[k] = v
            # Always canonicalise email to lower-case.
            if existing.get("email") != raw_email:
                patch["email"] = raw_email
            if patch:
                await db.talents.update_one({"id": existing["id"]}, {"$set": patch})
                existing.update(patch)
            existing.pop("created_by", None)
            return enrich_talent(existing)

    doc.update({
        "id": str(uuid.uuid4()),
        "media": [],
        # Phase 0: standardised source shape.
        "source": {
            "type": "admin",
            "talent_email": raw_email,
            "reference_id": None,
        },
        "created_at": _now(),
        "created_by": admin["id"],
    })
    try:
        await db.talents.insert_one(doc)
    except DuplicateKeyError:
        # Race: parallel create won. Re-fetch and merge.
        existing = await db.talents.find_one({"email": raw_email}, {"_id": 0})
        if existing:
            existing.pop("created_by", None)
            return enrich_talent(existing)
        raise HTTPException(409, "Talent with this email already exists")
    doc.pop("_id", None)
    doc.pop("created_by", None)
    return enrich_talent(doc)


@router.get("/talents")
async def list_talents(
    q: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    cursor = db.talents.find(query, {"_id": 0, "created_by": 0}).sort(
        "created_at", -1
    )
    if page is None:
        talents = await cursor.to_list(2000)
        return [enrich_talent(t) for t in talents]
    skip, limit, p, s = _paginate_params(page, size)
    total = await db.talents.count_documents(query)
    talents = await cursor.skip(skip).limit(limit).to_list(limit)
    return _paginated([enrich_talent(t) for t in talents], total, p, s)


@router.get("/talents/{tid}")
async def get_talent(tid: str, admin: dict = Depends(current_team_or_admin)):
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    if not t:
        raise HTTPException(404, "Talent not found")
    return enrich_talent(t)


@router.put("/talents/{tid}", response_model=TalentOut)
async def update_talent(tid: str, payload: TalentIn, admin: dict = Depends(current_team_or_admin)):
    update = payload.model_dump()
    # Phase 0: canonicalise email; reject email re-assignment that would
    # collide with another talent.
    if isinstance(update.get("email"), str):
        update["email"] = update["email"].strip().lower() or None
    if update.get("email"):
        clash = await db.talents.find_one(
            {"email": update["email"], "id": {"$ne": tid}}, {"_id": 0, "id": 1}
        )
        if clash:
            raise HTTPException(409, "Another talent already has this email")
    try:
        res = await db.talents.update_one({"id": tid}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "Another talent already has this email")
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@router.post("/talents/bulk-delete")
async def bulk_delete_talents(
    payload: BulkDeleteIn, admin: dict = Depends(current_admin)
):
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(400, "No ids provided")
    logger.info(
        "BULK DELETE /talents by admin=%s count=%d ids=%s",
        admin.get("email"), len(ids), ids[:10],
    )
    res = await db.talents.delete_many({"id": {"$in": ids}})
    logger.info(
        "BULK DELETE /talents by admin=%s removed=%d (of %d requested)",
        admin.get("email"), res.deleted_count, len(ids),
    )
    return {
        "ok": True,
        "requested": len(ids),
        "deleted": res.deleted_count,
        "missing": len(ids) - res.deleted_count,
    }


@router.delete("/talents/{tid}")
async def delete_talent(tid: str, admin: dict = Depends(current_admin)):
    logger.info(
        "DELETE /talents/%s requested by admin=%s (role=%s)",
        tid, admin.get("email"), admin.get("role"),
    )
    res = await db.talents.delete_one({"id": tid})
    if not res.deleted_count:
        logger.warning("DELETE /talents/%s failed — not found", tid)
        raise HTTPException(404, "Talent not found")
    logger.info("DELETE /talents/%s succeeded (by %s)", tid, admin.get("email"))
    return {"ok": True, "deleted_id": tid}


@router.post("/talents/{tid}/media", response_model=TalentOut)
async def add_media(
    tid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_team_or_admin),
):
    if category not in {"indian", "western", "portfolio", "video"}:
        raise HTTPException(400, "Invalid category")
    talent = await db.talents.find_one({"id": tid})
    if not talent:
        raise HTTPException(404, "Talent not found")

    media_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/talents/{tid}"
    data = await file.read()
    rt = "video" if category == "video" else "image"
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
        "size": result.get("bytes") or len(data),
        "created_at": _now(),
        "scope": "talent_portfolio",
        "talent_id": tid,
    }
    await db.talents.update_one({"id": tid}, {"$push": {"media": media}})
    # set cover if none
    if not talent.get("cover_media_id") and category in {"indian", "western", "portfolio"}:
        await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": media["id"]}})
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@router.delete("/talents/{tid}/media/{mid}")
async def delete_media(tid: str, mid: str, admin: dict = Depends(current_admin)):
    talent = await db.talents.find_one({"id": tid}, {"_id": 0, "media": 1})
    if not talent:
        raise HTTPException(404, "Talent not found")
    target = next((m for m in (talent.get("media") or []) if m.get("id") == mid), None)
    if not target:
        raise HTTPException(404, "Media not found")
    res = await db.talents.update_one({"id": tid}, {"$pull": {"media": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Media not found")
    pid = target.get("public_id")
    if pid:
        rt = target.get("resource_type") or ("video" if target.get("category") == "video" else "image")
        cloudinary_destroy(pid, resource_type=rt)
    return {"ok": True}


@router.post("/talents/{tid}/cover/{mid}")
async def set_cover(tid: str, mid: str, admin: dict = Depends(current_team_or_admin)):
    res = await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": mid}})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    return {"ok": True}
