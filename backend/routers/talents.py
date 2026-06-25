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
    upload_and_track_asset,
    compute_age,
    current_admin,
    current_team_or_admin,
    db,
    enrich_talent,
    media_url,
    video_poster_url,
    resolve_cover_media,
    update_talent_cover_cache,
    normalize_email,
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
    # FEATURE 1: whatsapp_group_name is admin-only — non-admins cannot set it.
    if admin.get("role") != "admin":
        doc["whatsapp_group_name"] = None
    raw_email = normalize_email(doc.get("email"))
    doc["email"] = raw_email
    doc["normalized_email"] = raw_email

    if raw_email:
        existing = await db.talents.find_one(
            {"$or": [
                {"normalized_email": raw_email},
                {"email": raw_email},
                {"source.talent_email": raw_email},
            ]},
            {"_id": 0},
        )
        if existing:
            from core import merge_talent_profile
            merged_talent = await merge_talent_profile(existing, doc, "admin_edit")
            merged_talent.pop("created_by", None)
            return enrich_talent(merged_talent)

    doc.update({
        "id": str(uuid.uuid4()),
        "media": [],
        "status": "SUBMITTED",
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
# List projection — a deny-list excluding internal/provenance/financial fields.
#
# COVER IMAGE ARCHITECTURE:
# cover_url is a denormalized scalar field written by set_cover, add_media
# (auto-cover), and delete_media (when cover item is deleted). It mirrors
# the URL that _resolve_cover_url(media[]) would return, but is stored
# directly so roster cards never need to walk media[].
#
# This guarantees: roster cover == detail cover, regardless of array
# insertion order, array size, or $slice position.
#
# NOTE: media[] is NOT excluded here — it is still returned because the Browse
# Talent modal renders it in the per-talent preview drawer. (Trimming it would
# require lazy per-talent media loading; tracked as a separate scale follow-up.)
_LIST_PROJECTION = {
    "_id": 0,
    "created_by": 0,
    "source": 0,        # Internal provenance — not rendered in list UI
    "notes": 0,         # Long-form text — not needed by list cards
    # Least-privilege: internal/financial fields that NO list consumer
    # (Browse Talent modal, roster, link generator) renders. Excluding them
    # keeps internal casting state out of the browser payload/devtools and
    # trims the list response. Single-talent views (/talents/{id}) are
    # unaffected — they fetch the full document.
    "admin_flags": 0,       # internal casting flags
    "internal_status": 0,   # internal lifecycle state
    "commission_data": 0,   # financial terms
    "client_feedback": 0,   # internal client notes
    "whatsapp_group_name": 0,  # ops field (WhatsApp engine uses its own API)
}


def _enrich_list(doc: dict) -> dict:
    """Lightweight enrichment for list responses.

    Reads the denormalized cover_thumbnail_url (set by update_talent_cover_cache).
    Falls back to cover_url if absent. Sets media_count from the stored field if present.
    Computes age from dob. Returns doc in-place.
    """
    # 1. Age derivation
    dob = doc.get("dob")
    if dob:
        computed = compute_age(dob)
        if computed is not None:
            doc["age"] = computed

    # 2. Dynamic media cover resolution if stored fields are missing or empty
    media_list = doc.get("media") or []
    media_item = resolve_cover_media(doc)
    
    # 3. Retrieve denormalized values, falling back to dynamic media resolution
    cover_url = doc.get("cover_url") or (media_item.get("url") if media_item else None)
    
    cover_thumb = doc.get("cover_thumbnail_url")
    if not cover_thumb and media_item:
        pid = media_item.get("public_id")
        if pid:
            rt = media_item.get("resource_type") or "image"
            cover_thumb = media_url(pid, preset="roster", resource_type=rt)
        else:
            cover_thumb = media_item.get("url")

    # 4. Set fields (image_url, cover_url, cover_thumbnail_url)
    doc["cover_url"] = cover_url
    doc["cover_thumbnail_url"] = cover_thumb
    doc["image_url"] = cover_thumb or cover_url or None

    # media_count: optional stored field
    if "media_count" not in doc:
        doc["media_count"] = len(media_list)

    return doc


@router.get("/talents")
async def list_talents(
    q: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    limit: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    if status:
        query: Dict[str, Any] = {"status": status}
    else:
        query: Dict[str, Any] = {"status": {"$nin": ["DRAFT", "ARCHIVED"]}}
    if q:
        needle = q.strip()
        terms = [t for t in re.split(r"[\s+\-,;]+", needle) if t]
        if terms:
            and_clauses = []
            for term in terms:
                rgx = {"$regex": re.escape(term), "$options": "i"}
                and_clauses.append({
                    "$or": [
                        {"name": rgx},
                        {"email": rgx},
                        {"instagram_handle": rgx},
                        {"location.city": rgx},
                        {"location.country": rgx},
                        {"gender": rgx},
                        {"category": rgx},
                        {"skills": rgx},
                        {"interested_in": rgx},
                    ]
                })
            query["$and"] = and_clauses
    # List projection excludes internal/provenance fields (see _LIST_PROJECTION).
    # NOTE: media[] IS returned — the Browse Talent modal renders it in the
    # per-talent preview drawer. Roster cards prefer the denormalized
    # cover_url/cover_thumbnail_url scalars (maintained by set_cover / add_media
    # / delete_media) and do not walk media[].
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
        "status": {"$ne": "DRAFT"},
        "$or": [
            {"name": rgx},
            {"email": rgx},
            {"phone": rgx},
            {"instagram_handle": rgx},
            {"location.city": rgx},
            {"location.country": rgx},
            {"gender": rgx},
            {"category": rgx},
            {"skills": rgx},
            {"interested_in": rgx},
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
        {"id": {"$in": ids}, "status": {"$ne": "DRAFT"}},
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


@router.get("/talents/migration/report")
async def get_migration_report(admin: dict = Depends(current_admin)):
    """Fetch the latest duplicate media cleanup migration report."""
    report = await db.migration_reports.find_one(sort=[("timestamp", -1)])
    if not report:
        raise HTTPException(404, "No migration reports found")
    if "_id" in report:
        del report["_id"]
    return report


@router.get("/talents/{tid}")
async def get_talent(tid: str, admin: dict = Depends(current_team_or_admin)):
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "created_by": 0})
    if not t:
        raise HTTPException(404, "Talent not found")
    return enrich_talent(t)


@router.put("/talents/{tid}", response_model=TalentOut)
async def update_talent(tid: str, payload: TalentIn, admin: dict = Depends(current_team_or_admin)):
    update = payload.model_dump()
    # FEATURE 1: whatsapp_group_name is admin-only. Drop it for non-admins so the
    # existing value is preserved (not overwritten).
    if admin.get("role") != "admin":
        update.pop("whatsapp_group_name", None)
    email = normalize_email(update.get("email"))
    update["email"] = email
    update["normalized_email"] = email

    existing = await db.talents.find_one({"id": tid})
    if not existing:
        raise HTTPException(404, "Talent not found")

    if email:
        clash = await db.talents.find_one(
            {"normalized_email": email, "id": {"$ne": tid}}, {"_id": 0, "id": 1}
        )
        if clash:
            raise HTTPException(409, "Another talent already has this email")

    # Track changes for the audit log
    AUTO_UPDATE_FIELDS = {
        "instagram_handle", "instagram_followers", "location", "bio",
        "skills", "work_links", "interested_in", "languages", "phone",
        "alternate_contact_number"
    }
    REVIEW_FIELDS = {
        "dob", "gender", "height", "ethnicity"
    }

    changed_fields = []
    old_values = {}
    new_values = {}

    for field in AUTO_UPDATE_FIELDS | REVIEW_FIELDS:
        incoming_val = update.get(field)
        existing_val = existing.get(field)
        if existing_val != incoming_val:
            changed_fields.append(field)
            old_values[field] = existing_val
            new_values[field] = incoming_val

    # Also check if email changed
    if existing.get("email") != email:
        changed_fields.append("email")
        old_values["email"] = existing.get("email")
        new_values["email"] = email

    try:
        res = await db.talents.update_one({"id": tid}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "Another talent already has this email")
    if not res.matched_count:
        raise HTTPException(404, "Talent not found")

    if changed_fields:
        audit_log = {
            "talent_id": tid,
            "email": email or existing.get("email"),
            "source": "admin_edit",
            "changed_fields": changed_fields,
            "old_values": old_values,
            "new_values": new_values,
            "timestamp": _now(),
        }
        await db.profile_audits.insert_one(audit_log)

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


class BulkTagPayload(BaseModel):
    ids: List[str] = Field(..., min_length=1)
    tag_id: str = Field(..., min_length=1)


@router.post("/talents/bulk-assign-tag")
async def bulk_assign_tag(
    payload: BulkTagPayload,
    admin: dict = Depends(current_team_or_admin),
):
    """Bulk assign an existing tag to multiple talents."""
    tag = await db.tags.find_one({"id": payload.tag_id}, {"_id": 0})
    if not tag:
        raise HTTPException(404, "Tag not found")
    
    tag_obj = {"id": tag["id"], "name": tag["name"]}
    
    # Update talents: push tag_obj to tags array if the tag_id is not already present in the tags array.
    res = await db.talents.update_many(
        {"id": {"$in": payload.ids}, "tags.id": {"$ne": payload.tag_id}},
        {"$push": {"tags": tag_obj}}
    )
    return {"ok": True, "modified_count": res.modified_count}


@router.post("/talents/bulk-remove-tag")
async def bulk_remove_tag(
    payload: BulkTagPayload,
    admin: dict = Depends(current_team_or_admin),
):
    """Bulk remove a tag from multiple talents."""
    res = await db.talents.update_many(
        {"id": {"$in": payload.ids}},
        {"$pull": {"tags": {"id": payload.tag_id}}}
    )
    return {"ok": True, "modified_count": res.modified_count}



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
    data = await file.read()
    rt = "video" if category == "video" else "image"
    asset_type = "portfolio_video" if category == "video" else "profile_image"

    result = await upload_and_track_asset(
        data,
        resource_type=rt,
        content_type=file.content_type,
        asset_type=asset_type,
        talent_id=tid,
        talent_name=talent.get("name"),
        user_id=admin.get("id"),
        keep_original=True,
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
        "duration": result.get("duration"),
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



# ---------------------------------------------------------------------------
# Tag Management — centralized admin label system
# ---------------------------------------------------------------------------

class TagCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class TagRenameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


def _normalize_tag_name(raw: str) -> str:
    """Lowercase + strip whitespace. Used for unique-index dedup."""
    return raw.strip().lower()


def _tag_doc(tag_id: str, name: str) -> dict:
    from core import _now
    normalized = _normalize_tag_name(name)
    return {
        "id": tag_id,
        "name": name.strip(),
        "normalized_name": normalized,
        "created_at": _now(),
    }


@router.get("/tags")
async def list_tags(admin: dict = Depends(current_team_or_admin)):
    """Return all admin tags sorted alphabetically."""
    docs = await db.tags.find({}, {"_id": 0}).sort("name", 1).to_list(5000)
    return {"ok": True, "tags": docs}


@router.post("/tags")
async def create_tag(payload: TagCreateIn, admin: dict = Depends(current_team_or_admin)):
    """Create a new unique admin tag (normalized, case-insensitive dedup).
    Both team members and admins may create tags.
    """
    from pymongo.errors import DuplicateKeyError as DKE
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Tag name cannot be empty")
    normalized = _normalize_tag_name(name)
    # Check if already exists (idempotent — return existing)
    existing = await db.tags.find_one({"normalized_name": normalized}, {"_id": 0})
    if existing:
        return {"ok": True, "tag": existing, "created": False}
    tag_id = str(uuid.uuid4())
    doc = _tag_doc(tag_id, name)
    try:
        await db.tags.insert_one(doc)
    except DKE:
        # Race — fetch and return the winner
        existing = await db.tags.find_one({"normalized_name": normalized}, {"_id": 0})
        if existing:
            return {"ok": True, "tag": existing, "created": False}
        raise HTTPException(409, "Tag already exists")
    doc.pop("_id", None)
    logger.info("Tag created id=%s name=%r by %s", tag_id, name, admin.get("email"))
    return {"ok": True, "tag": doc, "created": True}


@router.put("/tags/{tag_id}")
async def rename_tag(
    tag_id: str,
    payload: TagRenameIn,
    admin: dict = Depends(current_team_or_admin),
):
    """Rename a tag and cascade the new display name to all talent documents.
    Uses MongoDB array positional filter to update only the matching embedded object.
    """
    from pymongo.errors import DuplicateKeyError as DKE
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Tag name cannot be empty")
    normalized = _normalize_tag_name(name)
    # Block if normalized name collides with a different tag
    clash = await db.tags.find_one({"normalized_name": normalized, "id": {"$ne": tag_id}})
    if clash:
        raise HTTPException(409, "Another tag with this name already exists")
    try:
        res = await db.tags.update_one(
            {"id": tag_id},
            {"$set": {"name": name, "normalized_name": normalized}},
        )
    except DKE:
        raise HTTPException(409, "Another tag with this name already exists")
    if not res.matched_count:
        raise HTTPException(404, "Tag not found")
    # Cascade: update denormalized `name` in every talent that holds this tag.
    await db.talents.update_many(
        {"tags.id": tag_id},
        {"$set": {"tags.$[elem].name": name}},
        array_filters=[{"elem.id": tag_id}],
    )
    tag = await db.tags.find_one({"id": tag_id}, {"_id": 0})
    logger.info("Tag renamed id=%s new_name=%r by %s", tag_id, name, admin.get("email"))
    return {"ok": True, "tag": tag}


@router.delete("/tags/{tag_id}")
async def delete_tag(
    tag_id: str,
    admin: dict = Depends(current_admin),  # Admin-only: deletion is destructive
):
    """Globally delete a tag and strip it from every talent document.
    Restricted to admin role only. Uses atomic $pull to maintain consistency.
    """
    res = await db.tags.delete_one({"id": tag_id})
    if not res.deleted_count:
        raise HTTPException(404, "Tag not found")
    # Cascade: remove the embedded tag object from all talent records atomically.
    update_res = await db.talents.update_many(
        {"tags.id": tag_id},
        {"$pull": {"tags": {"id": tag_id}}},
    )
    logger.info(
        "Tag deleted id=%s — stripped from %d talents by admin=%s",
        tag_id, update_res.modified_count, admin.get("email"),
    )
    return {"ok": True, "stripped_from": update_res.modified_count}


from bson import ObjectId
from bson.errors import InvalidId

def get_talent_query(tid: str) -> dict:
    if not tid:
        return {"id": ""}
    try:
        if len(tid) == 24:
            return {"$or": [{"id": tid}, {"_id": ObjectId(tid)}]}
    except (InvalidId, TypeError, ValueError):
        pass
    return {"id": tid}


@router.post("/talents/{tid}/tag/{tag_id}")
async def assign_tag_to_talent(
    tid: str,
    tag_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Assign an existing admin tag to a specific talent.
    Idempotent — repeated assignment is silently skipped.
    """
    logger.info("assign_tag_to_talent: tid=%r, tag_id=%r", tid, tag_id)
    if not tid or tid in ("null", "undefined"):
        logger.warning("assign_tag_to_talent: Invalid/empty talent ID %r", tid)
        raise HTTPException(400, "Invalid talent ID")
    if not tag_id or tag_id in ("null", "undefined"):
        logger.warning("assign_tag_to_talent: Invalid/empty tag ID %r", tag_id)
        raise HTTPException(400, "Invalid tag ID")

    tag = await db.tags.find_one({"id": tag_id}, {"_id": 0})
    if not tag:
        raise HTTPException(404, "Tag not found")
        
    query = get_talent_query(tid)
    talent = await db.talents.find_one(query, {"_id": 0, "tags": 1, "id": 1})
    if not talent:
        logger.warning("assign_tag_to_talent: Talent not found for query %r", query)
        raise HTTPException(404, "Talent not found")
        
    # Idempotency check
    existing_ids = [t.get("id") for t in (talent.get("tags") or [])]
    if tag_id in existing_ids:
        return {"ok": True, "skipped": True}
        
    tag_obj = {"id": tag["id"], "name": tag["name"]}
    await db.talents.update_one(query, {"$push": {"tags": tag_obj}})
    return {"ok": True, "tag": tag_obj}


@router.delete("/talents/{tid}/tag/{tag_id}")
async def remove_tag_from_talent(
    tid: str,
    tag_id: str,
    admin: dict = Depends(current_team_or_admin),
):
    """Remove a tag from a specific talent (does NOT delete the global tag)."""
    logger.info("remove_tag_from_talent: tid=%r, tag_id=%r", tid, tag_id)
    if not tid or tid in ("null", "undefined"):
        logger.warning("remove_tag_from_talent: Invalid/empty talent ID %r", tid)
        raise HTTPException(400, "Invalid talent ID")
    if not tag_id or tag_id in ("null", "undefined"):
        logger.warning("remove_tag_from_talent: Invalid/empty tag ID %r", tag_id)
        raise HTTPException(400, "Invalid tag ID")

    query = get_talent_query(tid)
    res = await db.talents.update_one(
        query,
        {"$pull": {"tags": {"id": tag_id}}},
    )
    if not res.matched_count:
        logger.warning("remove_tag_from_talent: Talent not found for query %r", query)
        raise HTTPException(404, "Talent not found")
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

