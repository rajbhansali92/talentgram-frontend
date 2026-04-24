"""Public submission flow + admin review."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from core import (
    APP_NAME,
    DEFAULT_FIELD_VISIBILITY,
    MAX_SUBMISSION_IMAGES,
    MIN_SUBMISSION_IMAGES,
    SUBMISSION_DECISIONS,
    SUBMISSION_UPLOAD_CATEGORIES,
    AdminSubmissionEditIn,
    SubmissionDecisionIn,
    SubmissionStartIn,
    SubmissionUpdateIn,
    _now,
    _public_project,
    _submission_to_client_shape,
    current_admin,
    db,
    decode_submitter,
    make_token,
    put_object,
)

router = APIRouter(prefix="/api", tags=["submissions"])


# --------------------------------------------------------------------------
# Public (talent-facing) flow
# --------------------------------------------------------------------------
@router.get("/public/projects/{slug}")
async def public_project(slug: str):
    project = await db.projects.find_one({"slug": slug}, {"_id": 0, "created_by": 0})
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/public/prefill")
async def prefill_for_email(email: str):
    """Public lookup: if the email matches an approved talent, return safe fields
    so the audition form can pre-fill. Returns 204-style empty dict if not found.

    Only NON-SENSITIVE fields are exposed (never DOB, gender, bio, or media paths):
      first_name, last_name, age (computed), height, location, instagram_handle, instagram_followers
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return {}
    talent = await db.talents.find_one(
        {"$or": [{"email": email}, {"source.talent_email": email}]},
        {"_id": 0, "created_by": 0, "media": 0, "bio": 0, "dob": 0, "gender": 0, "ethnicity": 0, "work_links": 0, "cover_media_id": 0},
    )
    if not talent:
        return {}
    name = talent.get("name") or ""
    parts = name.split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return {
        "first_name": first,
        "last_name": last,
        "age": talent.get("age"),
        "height": talent.get("height"),
        "location": talent.get("location"),
        "instagram_handle": talent.get("instagram_handle"),
        "instagram_followers": talent.get("instagram_followers"),
    }


@router.post("/public/projects/{slug}/submission")
async def start_submission(slug: str, payload: SubmissionStartIn):
    project = await db.projects.find_one({"slug": slug})
    if not project:
        raise HTTPException(404, "Project not found")
    sid = str(uuid.uuid4())
    doc = {
        "id": sid,
        "project_id": project["id"],
        "project_slug": slug,
        "talent_name": payload.name,
        "talent_email": payload.email.lower(),
        "talent_phone": payload.phone,
        "form_data": payload.form_data or {},
        "field_visibility": {**DEFAULT_FIELD_VISIBILITY},
        "media": [],
        "status": "draft",
        "decision": "pending",
        "created_at": _now(),
        "submitted_at": None,
    }
    await db.submissions.insert_one(doc)
    token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
    return {"id": sid, "token": token}


@router.put("/public/submissions/{sid}")
async def submission_update(
    sid: str,
    payload: SubmissionUpdateIn,
    authorization: Optional[str] = Header(None),
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        update["form_data"] = {**(sub.get("form_data") or {}), **payload.form_data}
        fn = payload.form_data.get("first_name") or update["form_data"].get("first_name")
        ln = payload.form_data.get("last_name") or update["form_data"].get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")
    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@router.post("/public/submissions/{sid}/upload")
async def submission_upload(
    sid: str,
    category: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    if category not in SUBMISSION_UPLOAD_CATEGORIES:
        raise HTTPException(400, "Invalid category")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")

    if category == "image":
        existing = sum(1 for m in sub.get("media", []) if m["category"] == "image")
        if existing >= MAX_SUBMISSION_IMAGES:
            raise HTTPException(400, f"Image limit reached ({MAX_SUBMISSION_IMAGES})")

    single_slot = {"intro_video", "take_1", "take_2", "take_3"}
    if category in single_slot:
        await db.submissions.update_one(
            {"id": sid}, {"$pull": {"media": {"category": category}}}
        )

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/submissions/{sid}/{uuid.uuid4()}.{ext}"
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
        "scope": "submission",
        "submission_id": sid,
        "project_id": sub["project_id"],
    }
    await db.submissions.update_one({"id": sid}, {"$push": {"media": media}})
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@router.delete("/public/submissions/{sid}/media/{mid}")
async def submission_delete_media(
    sid: str, mid: str, authorization: Optional[str] = Header(None)
):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("status") == "submitted":
        raise HTTPException(400, "Submission already finalized")
    await db.submissions.update_one({"id": sid}, {"$pull": {"media": {"id": mid}}})
    return {"ok": True}


@router.post("/public/submissions/{sid}/finalize")
async def submission_finalize(sid: str, authorization: Optional[str] = Header(None)):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    form = sub.get("form_data") or {}
    for field in ("first_name", "last_name", "height", "location"):
        if not (form.get(field) or "").strip():
            raise HTTPException(400, f"{field.replace('_',' ').title()} is required")
    avail = form.get("availability") or {}
    if isinstance(avail, str):
        avail = {"status": "yes" if avail else "", "note": avail}
    status = (avail.get("status") or "").strip()
    if status not in {"yes", "no"}:
        raise HTTPException(400, "Please confirm your availability")
    if status == "no" and not (avail.get("note") or "").strip():
        raise HTTPException(400, "Please share your alternate availability")
    budget = form.get("budget") or {}
    if isinstance(budget, str):
        budget = {"status": "accept" if budget else "", "value": budget}
    bstatus = (budget.get("status") or "").strip()
    if bstatus not in {"accept", "custom"}:
        raise HTTPException(400, "Please confirm the budget")
    if bstatus == "custom" and not (budget.get("value") or "").strip():
        raise HTTPException(400, "Please enter your expected budget")
    media = sub.get("media", [])
    has_intro = any(m["category"] == "intro_video" for m in media)
    has_take1 = any(m["category"] == "take_1" for m in media)
    img_count = sum(1 for m in media if m["category"] == "image")
    if not has_intro:
        raise HTTPException(400, "Introduction video is required")
    if not has_take1:
        raise HTTPException(400, "Take 1 is required")
    if img_count < MIN_SUBMISSION_IMAGES:
        raise HTTPException(400, f"At least {MIN_SUBMISSION_IMAGES} images are required (you have {img_count})")
    await db.submissions.update_one(
        {"id": sid},
        {"$set": {"status": "submitted", "submitted_at": _now()}},
    )
    return {"ok": True}


@router.get("/public/submissions/{sid}")
async def public_submission(sid: str, authorization: Optional[str] = Header(None)):
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    return sub


# --------------------------------------------------------------------------
# Admin review
# --------------------------------------------------------------------------
@router.get("/submissions/approved")
async def list_approved_submissions(admin: dict = Depends(current_admin)):
    """All approved submissions across every project (admin convenience for Link picker)."""
    subs = await db.submissions.find(
        {"decision": "approved"},
        {"_id": 0},
    ).sort("created_at", -1).to_list(5000)
    projects = await db.projects.find({}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(2000)
    pmap = {p["id"]: p.get("brand_name") for p in projects}
    out: List[Dict[str, Any]] = []
    for s in subs:
        shape = _submission_to_client_shape(s)
        out.append({
            "id": s["id"],
            "talent_name": shape["name"],
            "project_id": s.get("project_id"),
            "project_brand": pmap.get(s.get("project_id")),
            "cover_media_id": shape.get("cover_media_id"),
            "media": shape.get("media"),
            "created_at": s.get("created_at"),
        })
    return out


@router.get("/projects/{pid}/submissions")
async def list_submissions(pid: str, admin: dict = Depends(current_admin)):
    subs = await db.submissions.find(
        {"project_id": pid}, {"_id": 0}
    ).sort("created_at", -1).to_list(5000)
    return subs


@router.post("/projects/{pid}/submissions/{sid}/decision")
async def set_decision(
    pid: str,
    sid: str,
    payload: SubmissionDecisionIn,
    admin: dict = Depends(current_admin),
):
    if payload.decision not in SUBMISSION_DECISIONS:
        raise HTTPException(400, "Invalid decision")
    res = await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {"$set": {"decision": payload.decision, "decided_at": _now()}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submission not found")
    return {"ok": True}


@router.put("/projects/{pid}/submissions/{sid}")
async def admin_edit_submission(
    pid: str,
    sid: str,
    payload: AdminSubmissionEditIn,
    admin: dict = Depends(current_admin),
):
    """Admin can edit form_data and toggle per-field visibility for the client view."""
    sub = await db.submissions.find_one({"id": sid, "project_id": pid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    update: Dict[str, Any] = {}
    if payload.form_data is not None:
        update["form_data"] = {**(sub.get("form_data") or {}), **payload.form_data}
        fn = update["form_data"].get("first_name") or sub.get("form_data", {}).get("first_name")
        ln = update["form_data"].get("last_name") or sub.get("form_data", {}).get("last_name")
        if fn or ln:
            update["talent_name"] = f"{fn or ''} {ln or ''}".strip() or sub.get("talent_name")
    if payload.field_visibility is not None:
        current_fv = sub.get("field_visibility") or {**DEFAULT_FIELD_VISIBILITY}
        update["field_visibility"] = {**current_fv, **payload.field_visibility}
    if update:
        await db.submissions.update_one({"id": sid}, {"$set": update})
    out = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return out


@router.delete("/projects/{pid}/submissions/{sid}")
async def delete_submission(
    pid: str, sid: str, admin: dict = Depends(current_admin)
):
    res = await db.submissions.delete_one({"id": sid, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Submission not found")
    return {"ok": True}
