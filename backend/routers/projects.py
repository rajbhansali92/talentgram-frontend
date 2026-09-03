"""Project CRUD, materials, forward-to-link."""
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core import (
    APP_NAME,
    BulkDeleteIn,
    COMMISSION_OPTIONS,
    DEFAULT_VISIBILITY,
    MATERIAL_CATEGORIES,
    MAX_VIDEO_FILE_BYTES,
    ForwardToLinkIn,
    ProjectIn,
    _clean_budget_lines,
    _now,
    _paginate_params,
    _paginated,
    _slugify,
    current_admin,
    current_team_or_admin,
    db,
    cloudinary_upload,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["projects"])


@router.post("/projects")
async def create_project(payload: ProjectIn, admin: dict = Depends(current_team_or_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    if payload.status not in ["ongoing", "hold", "complete", "locked"]:
        raise HTTPException(400, "Invalid status")
    doc = payload.model_dump()
    doc["talent_budget"] = _clean_budget_lines(doc.get("talent_budget"))
    doc["client_budget"] = _clean_budget_lines(doc.get("client_budget"))
    doc.update({
        "id": str(uuid.uuid4()),
        "slug": _slugify(payload.brand_name),
        "materials": [],
        "created_at": _now(),
        "created_by": admin["id"],
    })
    await db.projects.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/projects")
async def list_projects(
    page: Optional[int] = None,
    size: Optional[int] = None,
    status: Optional[str] = None,
    include_deleted: bool = False,
    admin: dict = Depends(current_team_or_admin),
):
    # P7: exclude soft-deleted projects from the operational list. `?include_deleted=true`
    # is the explicit admin/historical opt-in.
    from core import active_only
    query = active_only({"status": status} if status else {}, include_deleted=include_deleted)
    cursor = db.projects.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None:
        return await cursor.to_list(2000)
    skip, limit, p, s = _paginate_params(page, size)
    total = await db.projects.count_documents(query)
    items = await cursor.skip(skip).limit(limit).to_list(limit)
    return _paginated(items, total, p, s)


@router.get("/projects/{pid}")
async def get_project(pid: str, admin: dict = Depends(current_team_or_admin)):
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


@router.put("/projects/{pid}")
async def update_project(pid: str, payload: ProjectIn, admin: dict = Depends(current_team_or_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    if payload.status not in ["ongoing", "hold", "complete", "locked"]:
        raise HTTPException(400, "Invalid status")
    patch = payload.model_dump()
    patch["talent_budget"] = _clean_budget_lines(patch.get("talent_budget"))
    patch["client_budget"] = _clean_budget_lines(patch.get("client_budget"))
    res = await db.projects.update_one({"id": pid}, {"$set": patch})
    if not res.matched_count:
        raise HTTPException(404, "Project not found")
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


@router.post("/projects/bulk-delete")
async def bulk_delete_projects(
    payload: BulkDeleteIn, admin: dict = Depends(current_admin)
):
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(400, "No ids provided")
    logger.info(
        "BULK DELETE /projects by admin=%s count=%d ids=%s",
        admin.get("email"), len(ids), ids[:10],
    )
    # P6 (media lifecycle): soft-delete + ledger, NO Cloudinary mass-delete.
    from media_lifecycle import record_owner_teardown, get_retention_days, STATE_DELETED
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    retention_days = await get_retention_days(db)
    subs = await db.submissions.find({"project_id": {"$in": ids}}, {"_id": 0}).to_list(50000)
    enqueued = 0
    for s in subs:
        summ = await record_owner_teardown(
            db, s.get("media") or [], context_kind="project", context_id=s.get("project_id"),
            actor=admin.get("email"), retention_days=retention_days, now=now,
        )
        enqueued += summ.get("audition_enqueued", 0)

    res = await db.projects.update_many({"id": {"$in": ids}}, {"$set": {
        "status": STATE_DELETED, "lifecycle_state": STATE_DELETED,
        "deleted_at": now.isoformat(), "deleted_by": admin.get("email"),
    }})
    sub_res = await db.submissions.update_many({"project_id": {"$in": ids}}, {"$set": {
        "lifecycle_state": STATE_DELETED, "deleted_at": now.isoformat(),
    }})
    pipeline_res = await db.casting_pipeline.delete_many({"project_id": {"$in": ids}})
    asset_res = await db.asset_metadata.delete_many({"project_id": {"$in": ids}})

    logger.info(
        "BULK DELETE /projects by admin=%s soft-deleted=%d submissions_cascade=%d "
        "audition_media_pending=%d (NO Cloudinary asset deleted)",
        admin.get("email"), res.modified_count, sub_res.modified_count, enqueued,
    )
    return {
        "ok": True,
        "requested": len(ids),
        "deleted": res.modified_count,
        "missing": len(ids) - res.modified_count,
        "soft_deleted": True,
        "cascaded_submissions": sub_res.modified_count,
        "cascaded_pipeline": pipeline_res.deleted_count,
        "cascaded_assets": asset_res.deleted_count,
        "audition_media_pending_deletion": enqueued,
        "cloudinary_assets_deleted": 0,
    }


@router.delete("/projects/{pid}")
async def delete_project(pid: str, admin: dict = Depends(current_admin)):
    """Soft-delete a project (Cloudinary rearchitecture, P6 — media lifecycle).

    P6 change: this no longer runs a Cloudinary folder-prefix mass-delete — that
    scheme missed ``admin_media/`` and ``submissions/`` assets AND could destroy
    copy-by-value global talent media that merely happened to sit under the
    project folder. Deletion now flows through ``media_lifecycle``.

    Instead:
      * the project + its submissions are marked deleted (soft),
      * each submission's PROJECT-owned audition media is recorded in the
        ``pending_media_deletions`` ledger with the configured retention window,
      * GLOBAL talent media is left completely untouched,
      * NO Cloudinary asset is physically deleted here — P8/P9 own the purge.
    """
    from media_lifecycle import record_owner_teardown, get_retention_days, STATE_DELETED
    from datetime import datetime, timezone

    proj = await db.projects.find_one({"id": pid}, {"_id": 0, "id": 1})
    if not proj:
        logger.warning("DELETE /projects/%s failed — not found", pid)
        raise HTTPException(404, "Project not found")

    now = datetime.now(timezone.utc)
    retention_days = await get_retention_days(db)
    subs = await db.submissions.find({"project_id": pid}, {"_id": 0}).to_list(10000)

    totals = {"audition_enqueued": 0, "global_skipped": 0, "unknown_enqueued": 0,
              "still_referenced_skipped": 0, "no_asset": 0}
    for s in subs:
        summ = await record_owner_teardown(
            db, s.get("media") or [], context_kind="project", context_id=pid,
            actor=admin.get("email"), retention_days=retention_days, now=now,
        )
        for k in totals:
            totals[k] += summ.get(k, 0)

    # Soft-delete: project + its submissions stay as historical records.
    await db.projects.update_one({"id": pid}, {"$set": {
        "status": STATE_DELETED, "lifecycle_state": STATE_DELETED,
        "deleted_at": now.isoformat(), "deleted_by": admin.get("email"),
    }})
    sub_res = await db.submissions.update_many({"project_id": pid}, {"$set": {
        "lifecycle_state": STATE_DELETED, "deleted_at": now.isoformat(),
    }})
    pipeline_res = await db.casting_pipeline.delete_many({"project_id": pid})
    asset_res = await db.asset_metadata.delete_many({"project_id": pid})

    logger.info(
        "DELETE /projects/%s soft-deleted by %s; %d submissions marked deleted, "
        "audition media enqueued=%d, global media untouched=%d, unknown=%d, still-referenced=%d "
        "(NO Cloudinary asset deleted)",
        pid, admin.get("email"), sub_res.modified_count,
        totals["audition_enqueued"], totals["global_skipped"],
        totals["unknown_enqueued"], totals["still_referenced_skipped"],
    )
    return {
        "ok": True,
        "deleted_id": pid,
        "soft_deleted": True,
        "cascaded_submissions": sub_res.modified_count,
        "cascaded_pipeline": pipeline_res.deleted_count,
        "cascaded_assets": asset_res.deleted_count,
        "audition_media_pending_deletion": totals["audition_enqueued"],
        "global_talent_media_untouched": totals["global_skipped"],
        "retention_days": retention_days,
        "cloudinary_assets_deleted": 0,
    }


async def attach_project_material(
    pid: str,
    category: str,
    data: bytes,
    filename: Optional[str],
    content_type: Optional[str],
) -> Dict[str, Any]:
    """Upload one material file to Cloudinary and push it onto
    ``project.materials[]``. Single source of truth for the material
    descriptor shape + folder layout + validation — shared by the
    ``POST /projects/{pid}/material`` route (manual upload) and the AI
    Casting Desk's Gate-1 project creation, so a material attached by the
    AI is byte-for-byte identical to one a human uploads.

    Returns the updated project document (``_id`` stripped).
    """
    if category not in MATERIAL_CATEGORIES:
        raise HTTPException(400, "Invalid category (script|image|audio|video_file)")
    project = await db.projects.find_one({"id": pid})
    if not project:
        raise HTTPException(404, "Project not found")

    content_type = content_type or "application/octet-stream"
    if category == "video_file" and not content_type.startswith("video/"):
        raise HTTPException(400, "Reference video must be a video file")

    material_id = str(uuid.uuid4())
    # Segregated folder for reference videos vs other materials
    subdir = "videos" if category == "video_file" else "materials"
    folder = f"{APP_NAME}/projects/{pid}/{subdir}"

    # Enforce size limit for reference videos (100 MB)
    if category == "video_file" and len(data) > MAX_VIDEO_FILE_BYTES:
        raise HTTPException(
            400,
            f"Reference video too large ({len(data) // (1024 * 1024)} MB). Max {MAX_VIDEO_FILE_BYTES // (1024 * 1024)} MB.",
        )

    import os
    _, ext = os.path.splitext(filename or "")
    upload_public_id = f"{material_id}{ext.lower()}" if ext else material_id

    rt = "video" if category == "video_file" else "auto"
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=upload_public_id,
        resource_type=rt,
        content_type=content_type,
    )
    material = {
        "id": material_id,
        "category": category,
        "url": result["url"],
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "content_type": content_type,
        "original_filename": filename,
        "size": result.get("bytes") or len(data),
        "created_at": _now(),
        # Explicit scope — project material is bound to this project only
        "scope": "project_material",
        "project_id": pid,
    }
    await db.projects.update_one({"id": pid}, {"$push": {"materials": material}})
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


@router.post("/projects/{pid}/material")
async def add_material(
    pid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_team_or_admin),
):
    data = await file.read()
    return await attach_project_material(
        pid, category, data, file.filename, file.content_type
    )


@router.delete("/projects/{pid}/material/{mid}")
async def delete_material(pid: str, mid: str, admin: dict = Depends(current_admin)):
    res = await db.projects.update_one({"id": pid}, {"$pull": {"materials": {"id": mid}}})
    if not res.modified_count:
        raise HTTPException(404, "Material not found")
    return {"ok": True}


@router.get("/projects/meta/commission-options")
async def commission_options(admin: dict = Depends(current_team_or_admin)):
    return {"options": COMMISSION_OPTIONS}


@router.post("/projects/{pid}/forward-to-link")
async def forward_to_link(
    pid: str,
    payload: ForwardToLinkIn,
    admin: dict = Depends(current_admin),
):
    """Generate a client portfolio link that REFERENCES approved submissions directly.
    Submissions stay inside the project — they are never copied into the master `talents` collection."""
    if not payload.submission_ids:
        raise HTTPException(400, "Select at least one submission")
    project = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")

    approved = await db.submissions.find(
        {
            "id": {"$in": payload.submission_ids},
            "project_id": pid,
            "decision": "approved",
        },
        {"_id": 0, "id": 1},
    ).to_list(5000)
    approved_ids = {s["id"] for s in approved}
    if not approved_ids:
        raise HTTPException(400, "No approved submissions match the selection")

    ordered_submission_ids = [sid for sid in payload.submission_ids if sid in approved_ids]

    vis = {**DEFAULT_VISIBILITY, **(payload.visibility or {})}
    title = f"Talentgram x {project['brand_name']}"
    link_doc = {
        "id": str(uuid.uuid4()),
        "slug": _slugify(title),
        "title": title,
        "brand_name": project["brand_name"],
        "talent_ids": [],
        "submission_ids": ordered_submission_ids,
        "visibility": vis,
        "is_public": True,
        "password": None,
        "notes": f"Forwarded from project: {project['brand_name']}",
        "created_at": _now(),
        "created_by": admin["id"],
    }
    await db.links.insert_one(link_doc)
    link_doc.pop("_id", None)
    link_doc["view_count"] = 0
    link_doc["unique_viewers"] = 0
    return link_doc
