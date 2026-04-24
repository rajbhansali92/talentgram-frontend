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
    _slugify,
    current_admin,
    current_team_or_admin,
    db,
    put_object,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["projects"])


@router.post("/projects")
async def create_project(payload: ProjectIn, admin: dict = Depends(current_team_or_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
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
async def list_projects(admin: dict = Depends(current_team_or_admin)):
    items = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return items


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
    res = await db.projects.delete_many({"id": {"$in": ids}})
    sub_res = await db.submissions.delete_many({"project_id": {"$in": ids}})
    logger.info(
        "BULK DELETE /projects by admin=%s removed=%d submissions_cascade=%d",
        admin.get("email"), res.deleted_count, sub_res.deleted_count,
    )
    return {
        "ok": True,
        "requested": len(ids),
        "deleted": res.deleted_count,
        "missing": len(ids) - res.deleted_count,
        "cascaded_submissions": sub_res.deleted_count,
    }


@router.delete("/projects/{pid}")
async def delete_project(pid: str, admin: dict = Depends(current_admin)):
    logger.info(
        "DELETE /projects/%s requested by admin=%s (role=%s)",
        pid, admin.get("email"), admin.get("role"),
    )
    res = await db.projects.delete_one({"id": pid})
    if not res.deleted_count:
        logger.warning("DELETE /projects/%s failed — not found", pid)
        raise HTTPException(404, "Project not found")
    # Cascade: drop the project's submissions (they can never be revived once
    # the parent project is gone) — keeps listings consistent.
    sub_res = await db.submissions.delete_many({"project_id": pid})
    logger.info(
        "DELETE /projects/%s succeeded (by %s); cascade removed %d submissions",
        pid, admin.get("email"), sub_res.deleted_count,
    )
    return {"ok": True, "deleted_id": pid, "cascaded_submissions": sub_res.deleted_count}


@router.post("/projects/{pid}/material")
async def add_material(
    pid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_team_or_admin),
):
    if category not in MATERIAL_CATEGORIES:
        raise HTTPException(400, "Invalid category (script|image|audio|video_file)")
    project = await db.projects.find_one({"id": pid})
    if not project:
        raise HTTPException(404, "Project not found")

    content_type = file.content_type or "application/octet-stream"
    if category == "video_file" and not content_type.startswith("video/"):
        raise HTTPException(400, "Reference video must be a video file")

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    # Segregated storage path for reference videos
    subdir = "videos" if category == "video_file" else "materials"
    path = f"{APP_NAME}/projects/{pid}/{subdir}/{uuid.uuid4()}.{ext}"
    data = await file.read()

    # Enforce size limit for reference videos (100 MB)
    if category == "video_file" and len(data) > MAX_VIDEO_FILE_BYTES:
        raise HTTPException(
            400,
            f"Reference video too large ({len(data) // (1024 * 1024)} MB). Max {MAX_VIDEO_FILE_BYTES // (1024 * 1024)} MB.",
        )

    result = put_object(path, data, content_type)
    material = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": content_type,
        "original_filename": file.filename,
        "size": result.get("size", len(data)),
        "created_at": _now(),
        # Explicit scope — project material is bound to this project only
        "scope": "project_material",
        "project_id": pid,
    }
    await db.projects.update_one({"id": pid}, {"$push": {"materials": material}})
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


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
