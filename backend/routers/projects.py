"""Project CRUD, materials, forward-to-link."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core import (
    APP_NAME,
    COMMISSION_OPTIONS,
    DEFAULT_VISIBILITY,
    MATERIAL_CATEGORIES,
    MAX_VIDEO_FILE_BYTES,
    ForwardToLinkIn,
    ProjectIn,
    _now,
    _slugify,
    current_admin,
    db,
    put_object,
)

router = APIRouter(prefix="/api", tags=["projects"])


@router.post("/projects")
async def create_project(payload: ProjectIn, admin: dict = Depends(current_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    doc = payload.model_dump()
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
async def list_projects(admin: dict = Depends(current_admin)):
    items = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return items


@router.get("/projects/{pid}")
async def get_project(pid: str, admin: dict = Depends(current_admin)):
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Project not found")
    return p


@router.put("/projects/{pid}")
async def update_project(pid: str, payload: ProjectIn, admin: dict = Depends(current_admin)):
    if payload.commission_percent and payload.commission_percent not in COMMISSION_OPTIONS:
        raise HTTPException(400, "Invalid commission_percent")
    res = await db.projects.update_one({"id": pid}, {"$set": payload.model_dump()})
    if not res.matched_count:
        raise HTTPException(404, "Project not found")
    p = await db.projects.find_one({"id": pid}, {"_id": 0})
    return p


@router.delete("/projects/{pid}")
async def delete_project(pid: str, admin: dict = Depends(current_admin)):
    res = await db.projects.delete_one({"id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Project not found")
    return {"ok": True}


@router.post("/projects/{pid}/material")
async def add_material(
    pid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    admin: dict = Depends(current_admin),
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
async def commission_options(admin: dict = Depends(current_admin)):
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
