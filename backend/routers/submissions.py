"""Public submission flow + admin review."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pymongo.errors import DuplicateKeyError

from core import (
    APP_NAME,
    DEFAULT_FIELD_VISIBILITY,
    IMAGE_RESIZE_MAX_WIDTH,
    LEGACY_TAKE_CATEGORIES,
    MAX_SUBMISSION_IMAGES,
    MAX_SUBMISSION_IMAGE_BYTES,
    MAX_SUBMISSION_TAKES,
    MAX_SUBMISSION_VIDEO_BYTES,
    PORTFOLIO_IMAGE_CATEGORIES,
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
from drive_backup import (
    drive_enabled,
    enqueue_drive_upload,
    submission_folder_url,
)
from notifications import fanout as notify_fanout

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
async def prefill_for_email(email: str, request: Request):
    """Public lookup: if the email matches an approved talent, return safe fields
    so the audition form can pre-fill. Returns 204-style empty dict if not found.

    Phase 2 unified schema: returns the full set of non-sensitive identity
    fields. Media / source / notes / created_by are NEVER included.

    Phase 0: rate-limited to **20 lookups per minute per IP** to mitigate
    email-probing. The limiter is a sliding-window in-memory counter — fine
    for our scale; replace with Redis once we run multi-replica.
    """
    if not _prefill_rate_limit_ok(request):
        raise HTTPException(429, "Too many lookups — please slow down")
    email = (email or "").strip().lower()
    if "@" not in email:
        return {}
    talent = await db.talents.find_one(
        {"$or": [{"email": email}, {"source.talent_email": email}]},
        # Strict allowlist projection. Media / storage paths / source / notes
        # never leave this endpoint.
        {
            "_id": 0, "name": 1, "age": 1, "dob": 1, "height": 1,
            "phone": 1, "location": 1, "ethnicity": 1, "gender": 1, "bio": 1,
            "instagram_handle": 1, "instagram_followers": 1, "work_links": 1,
        },
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
        "dob": talent.get("dob"),
        "phone": talent.get("phone"),
        "height": talent.get("height"),
        "location": talent.get("location"),
        "ethnicity": talent.get("ethnicity"),
        "gender": talent.get("gender"),
        "bio": talent.get("bio"),
        "instagram_handle": talent.get("instagram_handle"),
        "instagram_followers": talent.get("instagram_followers"),
        "work_links": talent.get("work_links") or [],
    }


# Sliding-window rate limiter for the prefill endpoint. 20 reqs / 60 s / IP.
_PREFILL_BUCKET: Dict[str, list] = {}
_PREFILL_LIMIT = 20
_PREFILL_WINDOW = 60.0


def _prefill_rate_limit_ok(request: Request) -> bool:
    import time
    now = time.monotonic()
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    bucket = _PREFILL_BUCKET.setdefault(ip, [])
    # Drop expired
    cutoff = now - _PREFILL_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _PREFILL_LIMIT:
        return False
    bucket.append(now)
    return True


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
    try:
        await db.submissions.insert_one(doc)
    except DuplicateKeyError:
        # Race: parallel start hit the unique (project_id, talent_email)
        # index. Fall through to the existing-submission resume path.
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
        raise HTTPException(409, "Submission already exists for this email")
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

    if category in PORTFOLIO_IMAGE_CATEGORIES:
        existing = sum(
            1 for m in sub.get("media", []) if m["category"] in PORTFOLIO_IMAGE_CATEGORIES
        )
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
    if category in PORTFOLIO_IMAGE_CATEGORIES and size_bytes > MAX_SUBMISSION_IMAGE_BYTES:
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
    # client view loads fast. Original is retained for downloads. Applies to
    # generic + indian + western look images.
    if category in PORTFOLIO_IMAGE_CATEGORIES:
        resized = resize_image_bytes(data, max_width=IMAGE_RESIZE_MAX_WIDTH)
        if resized and len(resized) < size_bytes:
            resized_path = f"{APP_NAME}/submissions/{sid}/{uuid.uuid4()}_1600.jpg"
            r2 = put_object(resized_path, resized, "image/jpeg")
            media["resized_storage_path"] = r2["path"]
            media["resized_size"] = len(resized)

    patch: Dict[str, Any] = {"$push": {"media": media}}
    # Re-upload after finalize flips status back to "updated" and decision → pending
    was_finalized = sub.get("status") in ("submitted", "updated")
    re_approval = True
    if was_finalized:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1, "brand_name": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        set_patch = {
            "status": "updated",
            "updated_at": _now(),
        }
        if re_approval:
            set_patch["decision"] = "pending"
        patch["$set"] = set_patch
    await db.submissions.update_one({"id": sid}, patch)
    updated = await db.submissions.find_one({"id": sid}, {"_id": 0})

    # Notify admins on retake — but only when the submission was already
    # finalized (uploads during the initial flow are too noisy).
    if was_finalized:
        project = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
        )
        brand = (project or {}).get("brand_name") or "Project"
        talent_name = sub.get("talent_name") or sub.get("talent_email") or "A talent"
        cat_label = (
            "intro video" if category == "intro_video"
            else "audition take" if category == "take"
            else "image"
        )
        await notify_fanout(
            db,
            type="submission_retake",
            title=f"{talent_name} uploaded a new {cat_label}",
            body=(f"{brand} — submission moved back to pending."
                  if re_approval else f"{brand} — added to existing decision."),
            payload={"submission_id": sid, "project_id": sub["project_id"], "category": category},
        )

    # ------------------------------------------------------------------
    # Secondary backup → Google Drive (best-effort, non-blocking).
    # Spawns a detached asyncio task; failures are logged and queued for
    # retry. NEVER affects the primary upload result returned above.
    # ------------------------------------------------------------------
    if drive_enabled():
        project = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
        )
        brand = (project or {}).get("brand_name") or sub.get("project_slug") or "Unknown"
        enqueue_drive_upload(db, media, updated, brand, data)

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
    # Phase 1 v37c: media is fully optional on the audition submission
    # flow. Talents can ship a "form-only" submission and add intro
    # video / takes / portfolio images later via Refine. Caps on the
    # per-upload endpoint still apply (MAX_SUBMISSION_IMAGES, MAX_TAKES,
    # size limits). This block is intentionally empty — no media
    # minimums are enforced at finalize.

    # First-time finalize vs retest finalize
    is_retest = sub.get("status") in ("submitted", "updated")
    new_status = "updated" if is_retest else "submitted"
    patch: Dict[str, Any] = {
        "status": new_status,
        "submitted_at": sub.get("submitted_at") or _now(),
    }
    re_approval = True
    if is_retest:
        proj = await db.projects.find_one(
            {"id": sub["project_id"]}, {"_id": 0, "require_reapproval_on_edit": 1}
        )
        re_approval = bool((proj or {}).get("require_reapproval_on_edit", True))
        if re_approval:
            patch["decision"] = "pending"
        patch["updated_at"] = _now()

    # ------------------------------------------------------------------
    # Auto-link to global Talent DB (dedupe by email).
    # First-time finalize only — retest never overwrites global talent data.
    # Uses the SAME broad $or lookup as /apply approval so the merge logic
    # is consistent across all entry points (Phase 0).
    # ------------------------------------------------------------------
    if not is_retest and not sub.get("talent_id"):
        email = (sub.get("talent_email") or "").lower().strip()
        talent_doc = None
        if email:
            talent_doc = await db.talents.find_one(
                {"$or": [
                    {"email": email},
                    {"source.talent_email": email},
                ]},
                {"_id": 0},
            )
        if talent_doc:
            # Q5 (Phase 2 schema unification): "fill empty only" sync. The
            # admin's hand-edits are sacred — we only fill blanks from the
            # talent's latest submission, never overwrite. Media is also
            # never merged here (per Phase 0 spec).
            update: Dict[str, Any] = {}
            unified_fields = (
                "phone", "dob", "height", "location", "ethnicity",
                "gender", "instagram_handle", "instagram_followers", "bio",
            )
            for key in unified_fields:
                if not talent_doc.get(key):
                    val = form.get(key) if key != "phone" else (form.get("phone") or sub.get("talent_phone"))
                    if val:
                        update[key] = val
            if not talent_doc.get("age"):
                age_val = None
                if form.get("age") not in (None, ""):
                    try:
                        age_val = int(form["age"])
                    except Exception:
                        age_val = None
                if age_val is not None:
                    update["age"] = age_val
            new_links = [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()]
            if new_links and not (talent_doc.get("work_links") or []):
                update["work_links"] = new_links
            if update:
                await db.talents.update_one(
                    {"id": talent_doc["id"]}, {"$set": update}
                )
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
                "phone": (form.get("phone") or sub.get("talent_phone") or None),
                "age": age_val,
                "dob": (form.get("dob") or None),
                "height": (form.get("height") or None),
                "location": (form.get("location") or None),
                "ethnicity": (form.get("ethnicity") or None),
                "gender": (form.get("gender") or None),
                "instagram_handle": (form.get("instagram_handle") or None),
                "instagram_followers": (form.get("instagram_followers") or None),
                "bio": (form.get("bio") or None),
                "work_links": [w for w in (form.get("work_links") or []) if isinstance(w, str) and w.strip()],
                "notes": f"Auto-created from audition submission for project {sub.get('project_id')}",
                # Phase 0 — `source` is ALWAYS an object with the exact shape
                # {type, talent_email, reference_id} so the merge $or lookup
                # works symmetrically across all entry points.
                "source": {
                    "type": "audition_submission",
                    "talent_email": email or None,
                    "reference_id": sid,
                },
                "media": [],                 # keep global media separate (spec: media must NOT merge)
                "cover_media_id": None,
                "created_at": _now(),
                "created_by": "auto-audition",
            }
            try:
                await db.talents.insert_one(new_talent)
                new_talent.pop("_id", None)
                talent_doc = new_talent
            except DuplicateKeyError:
                # Race: another submission for the same email finalised in
                # parallel. Re-fetch the winner and link to it.
                talent_doc = await db.talents.find_one(
                    {"$or": [
                        {"email": email},
                        {"source.talent_email": email},
                    ]},
                    {"_id": 0},
                )
        if talent_doc:
            patch["talent_id"] = talent_doc["id"]

    await db.submissions.update_one({"id": sid}, {"$set": patch})

    # Fan out an admin notification — first-time finalize vs retest variant.
    project = await db.projects.find_one(
        {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or sub.get("project_slug") or "Project"
    talent_name = sub.get("talent_name") or sub.get("talent_email") or "A talent"
    if is_retest:
        await notify_fanout(
            db,
            type="submission_updated",
            title=f"{talent_name} updated their submission",
            body=f"{brand} — back to pending review.",
            payload={"submission_id": sid, "project_id": sub["project_id"]},
        )
    else:
        await notify_fanout(
            db,
            type="submission_new",
            title=f"New submission from {talent_name}",
            body=f"{brand} — awaiting your review.",
            payload={"submission_id": sid, "project_id": sub["project_id"]},
        )
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
    # Surface ONLY approved+shared client feedback. The talent never sees
    # pending/rejected/admin_only rows. This is the single approved channel
    # for client→talent communication (relay through admin moderation).
    from routers.feedback import list_approved_feedback_for_talent
    sub["client_feedback"] = await list_approved_feedback_for_talent(sid)
    return sub


# --------------------------------------------------------------------------
# Admin review
# --------------------------------------------------------------------------
@router.get("/submissions/{sid}/drive")
async def submission_drive_link(
    sid: str, admin: dict = Depends(current_team_or_admin)
):
    """Return the Google Drive folder URL for a submission (admin only).

    Lazily creates the folder path if it doesn't yet exist — useful for
    submissions that have only synced partially. Returns 404 if Drive
    backup is disabled.
    """
    if not drive_enabled():
        raise HTTPException(404, "Google Drive backup is not configured")
    sub = await db.submissions.find_one({"id": sid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    project = await db.projects.find_one(
        {"id": sub["project_id"]}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or "Unknown"
    url = submission_folder_url(brand, sid)
    if not url:
        raise HTTPException(503, "Drive folder lookup failed")
    return {"url": url, "brand": brand, "submission_id": sid}


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
    decision: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {"project_id": pid}
    if decision:
        if decision not in SUBMISSION_DECISIONS:
            raise HTTPException(400, "Invalid decision filter")
        query["decision"] = decision
    if status:
        query["status"] = status
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
    sub = await db.submissions.find_one({"id": sid, "project_id": pid}, {"_id": 0})
    if not sub:
        raise HTTPException(404, "Submission not found")
    prev = sub.get("decision")
    res = await db.submissions.update_one(
        {"id": sid, "project_id": pid},
        {"$set": {"decision": payload.decision, "decided_at": _now()}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submission not found")
    # Fanout — only when the decision actually changes (avoid noise on idempotent calls)
    if prev != payload.decision:
        project = await db.projects.find_one({"id": pid}, {"_id": 0, "brand_name": 1})
        brand = (project or {}).get("brand_name") or "Project"
        talent_name = sub.get("talent_name") or sub.get("talent_email") or "Submission"
        await notify_fanout(
            db,
            type="submission_decision",
            title=f"{talent_name} marked as {payload.decision}",
            body=f"{brand}",
            payload={"submission_id": sid, "project_id": pid, "decision": payload.decision},
            actor_id=admin.get("id"),
        )
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
