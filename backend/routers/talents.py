"""Talent CRUD + media management."""
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core import (
    APP_NAME,
    TalentIn,
    TalentOut,
    _now,
    current_admin,
    db,
    enrich_talent,
    put_object,
)

router = APIRouter(prefix="/api", tags=["talents"])


@router.post("/talents", response_model=TalentOut)
async def create_talent(payload: TalentIn, admin: dict = Depends(current_admin)):
    doc = payload.model_dump()
    doc.update({
        "id": str(uuid.uuid4()),
        "media": [],
        "created_at": _now(),
        "created_by": admin["id"],
    })
    await db.talents.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("created_by", None)
    return enrich_talent(doc)


@router.get("/talents")
async def list_talents(
    q: Optional[str] = None,
    admin: dict = Depends(current_admin),
):
    query: Dict[str, Any] = {}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    talents = await db.talents.find(query, {"_id": 0, "created_by": 0}).sort("created_at", -1).to_list(2000)
    return [enrich_talent(t) for t in talents]


@router.get("/talents/{tid}")
async def get_talent(tid: str, admin: dict = Depends(current_admin)):
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    if not t:
        raise HTTPException(404, "Talent not found")
    return enrich_talent(t)


@router.put("/talents/{tid}", response_model=TalentOut)
async def update_talent(tid: str, payload: TalentIn, admin: dict = Depends(current_admin)):
    update = payload.model_dump()
    res = await db.talents.update_one({"id": tid}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@router.delete("/talents/{tid}")
async def delete_talent(tid: str, admin: dict = Depends(current_admin)):
    res = await db.talents.delete_one({"id": tid})
    if not res.deleted_count:
        raise HTTPException(404, "Talent not found")
    return {"ok": True}


@router.post("/talents/{tid}/media", response_model=TalentOut)
async def add_media(
    tid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
):
    if category not in {"indian", "western", "portfolio", "video"}:
        raise HTTPException(400, "Invalid category")
    talent = await db.talents.find_one({"id": tid})
    if not talent:
        raise HTTPException(404, "Talent not found")

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/talents/{tid}/{uuid.uuid4()}.{ext}"
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
        # Explicit scope — talent media is global portfolio media, tied only to the talent.
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
    res = await db.talents.update_one({"id": tid}, {"$pull": {"media": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Media not found")
    return {"ok": True}


@router.post("/talents/{tid}/cover/{mid}")
async def set_cover(tid: str, mid: str, admin: dict = Depends(current_admin)):
    res = await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": mid}})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    return {"ok": True}
