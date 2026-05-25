"""Talent CRUD + media management."""
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
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
    compute_age,
    current_admin,
    current_team_or_admin,
    db,
    enrich_talent,
    media_url,
    video_poster_url,
    resolve_cover_media,
    update_talent_cover_cache,
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


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
# List projection: fetches only the scalar fields the roster card renders.
#
# COVER IMAGE ARCHITECTURE:
# cover_url is a denormalized scalar field written by set_cover, add_media
# (auto-cover), and delete_media (when cover item is deleted). It mirrors
# the URL that _resolve_cover_url(media[]) would return, but is stored
# directly so the list endpoint never needs to fetch or walk media[].
#
# This guarantees: roster cover == detail cover, regardless of array
# insertion order, array size, or $slice position.
#
# Payload per talent: ~250 bytes (single URL string, zero array data).
_LIST_PROJECTION = {
    "_id": 0,
    "created_by": 0,
    "media": 0,         # Never needed — cover_url is the resolved scalar
    "source": 0,        # Internal provenance — not rendered in list UI
    "notes": 0,         # Long-form text — not needed by list cards
}


def _enrich_list(doc: dict) -> dict:
    """Lightweight enrichment for list responses.

    Reads the denormalized cover_thumbnail_url (set by update_talent_cover_cache).
    Falls back to cover_url if absent. Sets media_count from the stored field if present.
    Computes age from dob. Returns doc in-place.
    """
    cover_thumb = doc.get("cover_thumbnail_url")
    cover_url = doc.get("cover_url")
    
    doc["image_url"] = cover_thumb or cover_url or None

    # media_count: optional stored field (set when cover is written).
    # Falls back to 0 if absent; exact counts shown on detail page.
    if "media_count" not in doc:
        doc["media_count"] = 0
    # Remove internal scalars from the public payload
    doc.pop("cover_url", None)
    doc.pop("cover_thumbnail_url", None)
    # Age derivation
    dob = doc.get("dob")
    if dob:
        computed = compute_age(dob)
        if computed is not None:
            doc["age"] = computed
    return doc


@router.get("/talents")
async def list_talents(
    q: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    limit: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if q:
        query["name"] = {"$regex": q, "$options": "i"}
    # List projection: scalar fields only — no media[] fetch.
    # cover_url is the denormalized cover scalar maintained by set_cover /
    # add_media / delete_media. Zero array walk per roster row.
    cursor = db.talents.find(query, _LIST_PROJECTION).sort("created_at", -1)
    if page is None and limit is None:
        talents = await cursor.to_list(2000)
        return [_enrich_list(t) for t in talents]
    skip, page_size, p, s = _paginate_params(page, size, limit)
    total = await db.talents.count_documents(query)
    talents = await cursor.skip(skip).limit(page_size).to_list(page_size)
    return _paginated([_enrich_list(t) for t in talents], total, p, s)





# ---------------------------------------------------------------------------
# Lightweight search + bulk-by-id helpers — power the Casting Pipeline's
# Quick Add (live search) and pipeline-row hydration. Kept lightweight: only
# the fields the kanban card needs, capped at 30 hits, two-char minimum.
# Routes are declared BEFORE `/talents/{tid}` so FastAPI matches the literal
# path first and doesn't treat "search"/"bulk" as a talent id.
# ---------------------------------------------------------------------------
def _talent_lite(t: dict) -> dict:
    """Trim a talent doc to the fields the pipeline UI actually renders."""
    enriched = enrich_talent(t) or {}
    return {
        "id": enriched.get("id"),
        "name": enriched.get("name"),
        "email": enriched.get("email"),
        "phone": enriched.get("phone"),
        "instagram_handle": enriched.get("instagram_handle"),
        "image_url": enriched.get("image_url"),
    }


@router.get("/talents/search")
async def search_talents(
    q: str = "",
    admin: dict = Depends(current_team_or_admin),
):
    """Multi-field talent lookup for the pipeline Quick Add.

    Matches against `name`, `email`, `phone`, `instagram_handle` (case
    insensitive, substring). Returns up to 30 lightweight records. A short
    query (<2 chars after strip) returns an empty list rather than every
    talent in the database — keeps the UI snappy and avoids accidental
    full-table reads.
    """
    needle = (q or "").strip()
    if len(needle) < 2:
        return {"success": True, "data": []}

    rgx = {"$regex": re.escape(needle), "$options": "i"}
    query = {
        "$or": [
            {"name": rgx},
            {"email": rgx},
            {"phone": rgx},
            {"instagram_handle": rgx},
        ]
    }
    cursor = db.talents.find(
        query,
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "email": 1,
            "phone": 1,
            "instagram_handle": 1,
            "cover_media_id": 1,
            "media": 1,
        },
    ).limit(30)
    docs = await cursor.to_list(30)
    return {"success": True, "data": [_talent_lite(t) for t in docs]}


class BulkIdsIn(BaseModel):
    """Body for /talents/bulk — list of talent UUIDs to hydrate."""
    ids: List[str] = Field(default_factory=list)


@router.post("/talents/bulk")
async def bulk_talents(
    payload: BulkIdsIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Hydrate a list of talent ids in one round-trip.

    Used by the pipeline frontend to enrich kanban rows (which store only
    `talent_id`) with name/email/image. Preserves the **input order** so the
    caller doesn't have to re-sort client-side. Missing ids are silently
    dropped; the response stays well-formed.
    """
    ids = [i for i in (payload.ids or []) if isinstance(i, str) and i]
    if not ids:
        return {"success": True, "data": []}

    cursor = db.talents.find(
        {"id": {"$in": ids}},
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "email": 1,
            "phone": 1,
            "instagram_handle": 1,
            "cover_media_id": 1,
            "media": 1,
        },
    )
    docs = await cursor.to_list(len(ids))
    by_id = {d["id"]: _talent_lite(d) for d in docs if d.get("id")}
    ordered = [by_id[i] for i in ids if i in by_id]
    return {"success": True, "data": ordered}


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
    await update_talent_cover_cache(tid)
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
        "size": result.get("bytes") or len(data),
        "created_at": _now(),
        "scope": "talent_portfolio",
        "talent_id": tid,
        "thumbnail_url": media_url(result["public_id"], preset="roster", resource_type=result["resource_type"]) if is_image else None,
        "poster_url": video_poster_url(result["public_id"]) if is_video else None,
    }
    await db.talents.update_one({"id": tid}, {"$push": {"media": media}})
    # Auto-assign cover on first image upload
    if not talent.get("cover_media_id") and category in {"indian", "western", "portfolio"}:
        await db.talents.update_one(
            {"id": tid},
            {"$set": {"cover_media_id": media["id"]}},
        )
    await update_talent_cover_cache(tid)
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    return enrich_talent(t)


@router.delete("/talents/{tid}/media/{mid}")
async def delete_media(tid: str, mid: str, admin: dict = Depends(current_admin)):
    talent = await db.talents.find_one({"id": tid}, {"_id": 0, "media": 1, "cover_media_id": 1})
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
    # If the deleted item was the current cover, clear the cover ID reference first
    if talent.get("cover_media_id") == mid:
        await db.talents.update_one(
            {"id": tid},
            {"$set": {"cover_media_id": None}}
        )
    await update_talent_cover_cache(tid)
    return {"ok": True}


@router.post("/talents/{tid}/cover/{mid}")
async def set_cover(tid: str, mid: str, admin: dict = Depends(current_team_or_admin)):
    """Set the cover image for a talent.

    Writes cover_media_id (the item id reference) AND cover_url/cover_thumbnail_url
    via update_talent_cover_cache.
    """
    res = await db.talents.update_one({"id": tid}, {"$set": {"cover_media_id": mid}})
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")
    await update_talent_cover_cache(tid)
    updated_talent = await db.talents.find_one({"id": tid}, {"cover_url": 1})
    return {"ok": True, "cover_url": updated_talent.get("cover_url")}

