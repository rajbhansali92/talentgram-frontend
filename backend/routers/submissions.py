"""Public submission flow + admin review."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from core import (
    APP_NAME,
    DEFAULT_FIELD_VISIBILITY,
    IMAGE_RESIZE_MAX_WIDTH,
    LEGACY_TAKE_CATEGORIES,
    MAX_SUBMISSION_IMAGES,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_TAKES,
    MAX_SUBMISSION_VIDEO_BYTES,
    MIN_SUBMISSION_IMAGES,
    SUBMISSION_DECISIONS,
    SUBMISSION_UPLOAD_CATEGORIES,
    AdminSubmissionEditIn,
    SubmissionDecisionIn,
    SubmissionStartIn,
    SubmissionUpdateIn,
    _now,
    _paginate_params,
    _paginated,
    _public_project,
    _submission_to_client_shape,
    current_admin,
    current_team_or_admin,
    db,
    decode_submitter,
    make_token,
    put_object,
    resize_image_bytes,
)

router = APIRouter(prefix="/api", tags=["submissions"])


# --------------------------------------------------------------------------
# Public (talent-facing) flow
# --------------------------------------------------------------------------
@router.get("/public/projects/{slug}")
async def public_project(slug: str):
    project = await db.projects.find_one({"slug": slug}, {"_id": 0, "created_by": 0, "client_budget": 0})
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
    """Start OR resume a submission.

    If a submission already exists for (project, email), returns a fresh token
    that unlocks edits — this is the retest / re-upload entry point. The
    decision is NOT reset here; only `finalize` flips it back to pending.
    """
    project = await db.projects.find_one({"slug": slug})
    if not project:
        raise HTTPException(404, "Project not found")
    email = payload.email.lower().strip()

    existing = await db.submissions.find_one({
        "project_id": project["id"],
        "talent_email": email,
    })
    if existing:
        sid = existing["id"]
        token = make_token({"role": "submitter", "sid": sid, "slug": slug}, days=3)
        return {
            "id": sid,
            "token": token,
            "resumed": True,
            "status": existing.get("status", "draft"),
        }

    sid = str(uuid.uuid4())
    doc = {
        "id": sid,
        "project_id": project["id"],
        "project_slug": slug,
        "talent_name": payload.name,
        "talent_email": email,
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
    return {"id": sid, "token": token, "resumed": False, "status": "draft"}


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
    label: Optional[str] = Form(None),
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

    if category == "image":
        existing = sum(1 for m in sub.get("media", []) if m["category"] == "image")
        if existing >= MAX_SUBMISSION_IMAGES:
            raise HTTPException(400, f"Image limit reached ({MAX_SUBMISSION_IMAGES})")

    if category == "take":
        existing_takes = sum(
            1
            for m in sub.get("media", [])
            if m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        )
        if existing_takes >= MAX_SUBMISSION_TAKES:
            raise HTTPException(
                400,
                f"Maximum {MAX_SUBMISSION_TAKES} takes reached — delete one to add another",
            )

    # Single-slot replacement: intro video + legacy fixed takes
    single_slot = {"intro_video", "take_1", "take_2", "take_3"}
    if category in single_slot:
        await db.submissions.update_one(
            {"id": sid}, {"$pull": {"media": {"category": category}}}
        )

    ext = (file.filename or "bin").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/submissions/{sid}/{uuid.uuid4()}.{ext}"
    data = await file.read()

    # Enforce size caps up front so oversized bodies never make it to storage.
    # Videos (intro/takes) are capped at 150 MB; images at 25 MB raw.
    is_video_slot = category in {"intro_video", "take", "take_1", "take_2", "take_3"}
    size_bytes = len(data)
    if is_video_slot and size_bytes > MAX_SUBMISSION_VIDEO_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_VIDEO_BYTES // (1024 * 1024)
        raise HTTPException(
            400,
            f"Video is too large ({mb} MB). Max {cap_mb} MB — please compress and retry.",
        )
    if category == "image" and size_bytes > MAX_SUBMISSION_IMAGE_BYTES:
        mb = size_bytes // (1024 * 1024)
        cap_mb = MAX_SUBMISSION_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(
            400, f"Image is too large ({mb} MB). Max {cap_mb} MB per image."
        )

    result = put_object(path, data, file.content_type or "application/octet-stream")
    media = {
        "id": str(uuid.uuid4()),
        "category": category,
        "storage_path": result["path"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("size", size_bytes),
        "created_at": _now(),
        "scope": "submission",
        "submission_id": sid,
        "project_id": sub["project_id"],
    }
    if category == "take":
        media["label"] = (label or "").strip() or f"Take {existing_takes + 1}"

    # Generate an optimised 1600px JPEG variant for portfolio images so the
    # client view loads fast. Original is retained for downloads.
    if category == "image":
        resized = resize_image_bytes(data, max_width=IMAGE_RESIZE_MAX_WIDTH)
        if resized and len(resized) < size_bytes:
            resized_path = f"{APP_NAME}/submissions/{sid}/{uuid.uuid4()}_1600.jpg"
            r2 = put_object(resized_path, resized, "image/jpeg")
            media["resized_storage_path"] = r2["path"]
            media["resized_size"] = len(resized)

    patch: Dict[str, Any] = {"$push": {"media": media}}
    # Re-upload after finalize flips status back to "updated" and decision → pending
    if sub.get("status") in ("submitted", "updated"):
        patch["$set"] = {
            "status": "updated",
            "decision": "pending",
            "updated_at": _now(),
        }
    await db.submissions.update_one({"id": sid}, patch)
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})
    return updated


@router.patch("/public/submissions/{sid}/media/{mid}")
async def submission_update_media(
    sid: str,
    mid: str,
    payload: Dict[str, Any],
    authorization: Optional[str] = Header(None),
):
    """Patch a take's label. Only `take` media supports this today."""
    submitter = decode_submitter(authorization)
    if not submitter or submitter.get("sid") != sid:
        raise HTTPException(401, "Invalid submission token")
    sub = await db.submissions.find_one({"id": sid})
    if not sub:
        raise HTTPException(404, "Submission not found")
    target = next((m for m in (sub.get("media") or []) if m.get("id") == mid), None)
    if not target:
        raise HTTPException(404, "Media not found")
    if target.get("category") != "take":
        raise HTTPException(400, "Only renamable takes can be patched")
    new_label = (payload.get("label") or "").strip()
    if not new_label:
        raise HTTPException(400, "Label cannot be empty")
    await db.submissions.update_one(
        {"id": sid, "media.id": mid},
        {"$set": {"media.$.label": new_label}},
    )
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
    patch: Dict[str, Any] = {"$pull": {"media": {"id": mid}}}
    if sub.get("status") in ("submitted", "updated"):
        patch["$set"] = {
            "status": "updated",
            "decision": "pending",
            "updated_at": _now(),
        }
    await db.submissions.update_one({"id": sid}, patch)
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
    has_any_take = any(
        m["category"] == "take" or m["category"] in LEGACY_TAKE_CATEGORIES
        for m in media
    )
    img_count = sum(1 for m in media if m["category"] == "image")
    if not has_intro:
        raise HTTPException(400, "Introduction video is required")
    if not has_any_take:
        raise HTTPException(400, "At least one audition take is required")
    if img_count < MIN_SUBMISSION_IMAGES:
        raise HTTPException(400, f"At least {MIN_SUBMISSION_IMAGES} images are required (you have {img_count})")

    # First-time finalize vs retest finalize
    is_retest = sub.get("status") in ("submitted", "updated")
    new_status = "updated" if is_retest else "submitted"
    patch: Dict[str, Any] = {
        "status": new_status,
        "submitted_at": sub.get("submitted_at") or _now(),
    }
    if is_retest:
        patch["decision"] = "pending"
        patch["updated_at"] = _now()

    # ------------------------------------------------------------------
    # Auto-link to global Talent DB (dedupe by email).
    # First-time finalize only — retest never overwrites global talent data.
    # ------------------------------------------------------------------
    if not is_retest and not sub.get("talent_id"):
        email = (sub.get("talent_email") or "").lower().strip()
        talent_doc = None
        if email:
            talent_doc = await db.talents.find_one({"email": email}, {"_id": 0})
        if not talent_doc:
            # Build a minimal talent record from the submission's form_data.
            full_name = (
                f"{(form.get('first_name') or '').strip()} "
                f"{(form.get('last_name') or '').strip()}"
            ).strip() or sub.get("talent_name") or "Unnamed"
            age_val = None
            if form.get("age") not in (None, ""):
                try:
                    age_val = int(form["age"])
                except Exception:
                    age_val = None
            new_talent = {
                "id": str(uuid.uuid4()),
                "name": full_name,
                "email": email or None,
                "phone": sub.get("talent_phone"),
                "age": age_val,
                "dob": (form.get("dob") or None),
                "height": (form.get("height") or None),
                "location": (form.get("location") or None),
                "ethnicity": None,
                "instagram_handle": (form.get("instagram_handle") or None),
                "instagram_followers": None,
                "work_links": [],
                "notes": f"Auto-created from audition submission for project {sub.get('project_id')}",
                "source": "audition_submission",
                "media": [],                 # keep global media separate (spec: media must NOT merge)
                "cover_media_id": None,
                "created_at": _now(),
                "created_by": "auto-audition",
            }
            await db.talents.insert_one(new_talent)
            new_talent.pop("_id", None)
            talent_doc = new_talent
        patch["talent_id"] = talent_doc["id"]

    await db.submissions.update_one({"id": sid}, {"$set": patch})
    return {
        "ok": True,
        "status": new_status,
        "resubmitted": is_retest,
        "talent_id": patch.get("talent_id") or sub.get("talent_id"),
    }


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
async def list_approved_submissions(
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    """All approved submissions across every project (admin convenience for Link picker)."""
    query = {"decision": "approved"}
    cursor = db.submissions.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None:
        subs = await cursor.to_list(5000)
        total = None
        p = s = None
    else:
        skip, limit, p, s = _paginate_params(page, size)
        total = await db.submissions.count_documents(query)
        subs = await cursor.skip(skip).limit(limit).to_list(limit)
    projects = await db.projects.find({}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(2000)
    pmap = {p["id"]: p.get("brand_name") for p in projects}
    out: List[Dict[str, Any]] = []
    for sub in subs:
        shape = _submission_to_client_shape(sub)
        out.append({
            "id": sub["id"],
            "talent_name": shape["name"],
            "project_id": sub.get("project_id"),
            "project_brand": pmap.get(sub.get("project_id")),
            "cover_media_id": shape.get("cover_media_id"),
            "media": shape.get("media"),
            "created_at": sub.get("created_at"),
        })
    if page is None:
        return out
    return _paginated(out, total, p, s)


@router.get("/projects/{pid}/submissions")
async def list_submissions(
    pid: str,
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query = {"project_id": pid}
    cursor = db.submissions.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None:
        return await cursor.to_list(5000)
    skip, limit, p, s = _paginate_params(page, size)
    total = await db.submissions.count_documents(query)
    items = await cursor.skip(skip).limit(limit).to_list(limit)
    return _paginated(items, total, p, s)


@router.post("/projects/{pid}/submissions/{sid}/decision")
async def set_decision(
    pid: str,
    sid: str,
    payload: SubmissionDecisionIn,
    admin: dict = Depends(current_team_or_admin),
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
    admin: dict = Depends(current_team_or_admin),
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
