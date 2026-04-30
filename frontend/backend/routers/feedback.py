"""Moderated client → talent feedback relay.

Core principle: clients NEVER communicate with talents directly. Every piece
of feedback (text or voice) starts as `pending / admin_only` and only becomes
visible to the talent when an admin explicitly approves it.

Endpoints:
  Client (viewer-token):
    POST /api/public/links/{slug}/feedback        — text feedback (JSON)
    POST /api/public/links/{slug}/feedback/voice  — voice feedback (multipart)

  Admin (admin/team auth):
    GET  /api/admin/feedback                      — list (filters)
    POST /api/admin/feedback/{fid}/approve
    POST /api/admin/feedback/{fid}/reject
    POST /api/admin/feedback/{fid}/edit           — edit text content

  Public (submission-token, talent-facing):
    Approved feedback is surfaced inline by GET /api/public/submissions/{sid}.
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from core import (
    APP_NAME,
    ClientTextFeedbackIn,
    FEEDBACK_STATUSES,
    FeedbackEditIn,
    MAX_FEEDBACK_AUDIO_BYTES,
    MAX_FEEDBACK_TEXT_LEN,
    _now,
    _paginate_params,
    _paginated,
    current_team_or_admin,
    db,
    decode_viewer,
    cloudinary_upload,
)
from notifications import fanout as notify_fanout

router = APIRouter(prefix="/api", tags=["feedback"])
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _ensure_link_includes_subject(
    link: dict, submission_id: str, talent_id: str, project_id: str
) -> None:
    """Sanity check: the (submission_id, talent_id, project_id) trio must
    correspond to a real submission AND be reachable from this link.

    Reachable means either:
      • the link manually curates this submission_id, OR
      • the link auto-pulls from this project_id, OR
      • the link manually curates this talent_id (M1 individual share)
    """
    sub = await db.submissions.find_one(
        {"id": submission_id}, {"_id": 0, "id": 1, "project_id": 1, "talent_id": 1}
    )
    if not sub:
        raise HTTPException(404, "Submission not found")
    if sub.get("project_id") != project_id:
        raise HTTPException(400, "project_id does not match submission")
    sub_ids = link.get("submission_ids") or []
    talent_ids = link.get("talent_ids") or []
    if submission_id in sub_ids:
        return
    if link.get("auto_pull") and link.get("auto_project_id") == project_id:
        return
    if talent_id and talent_id in talent_ids:
        return
    raise HTTPException(403, "Subject not part of this link")


async def _persist_feedback(
    *,
    link: dict,
    viewer: dict,
    fb_type: str,
    talent_id: str,
    submission_id: str,
    project_id: str,
    text: Optional[str] = None,
    content_url: Optional[str] = None,
    content_type: Optional[str] = None,
) -> dict:
    """Insert a feedback row + fan-out admin notification. Returns the doc."""
    fid = str(uuid.uuid4())
    now = _now()
    doc = {
        "id": fid,
        "type": fb_type,
        "text": text,
        "content_url": content_url,
        "content_type": content_type,
        "talent_id": talent_id,
        "submission_id": submission_id,
        "project_id": project_id,
        "link_id": link["id"],
        "created_by": "client",
        "client_viewer_email": viewer["email"],
        "client_viewer_name": viewer.get("name"),
        "status": "pending",
        "visibility": "admin_only",
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "rejected_by": None,
        "edited_at": None,
        "edited_by": None,
    }
    await db.feedback.insert_one(doc)
    doc.pop("_id", None)

    # Notify admins/team — title carries enough context for review screen.
    project = await db.projects.find_one(
        {"id": project_id}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or "Project"
    label = "voice note" if fb_type == "voice" else "feedback"
    await notify_fanout(
        db,
        type="client_feedback_new",
        title=f"New client {label} from {viewer.get('name') or viewer['email']}",
        body=f"{brand} — awaiting moderation.",
        payload={
            "feedback_id": fid,
            "submission_id": submission_id,
            "project_id": project_id,
            "talent_id": talent_id,
            "type": fb_type,
        },
    )
    return doc


def _client_feedback_view(doc: dict) -> dict:
    """Strip admin-only + reviewer fields when surfacing to talent."""
    return {
        "id": doc["id"],
        "type": doc["type"],
        "text": doc.get("text"),
        "content_url": doc.get("content_url"),
        "content_type": doc.get("content_type"),
        "approved_at": doc.get("approved_at"),
        "created_at": doc.get("created_at"),
        # Project + submission anchors only — never expose viewer email or link_id.
        "project_id": doc.get("project_id"),
        "submission_id": doc.get("submission_id"),
    }


# --------------------------------------------------------------------------
# Public — client feedback creation
# --------------------------------------------------------------------------
@router.post("/public/links/{slug}/feedback")
async def create_text_feedback(
    slug: str,
    payload: ClientTextFeedbackIn,
    authorization: Optional[str] = Header(None),
):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    await _ensure_link_includes_subject(
        link, payload.submission_id, payload.talent_id, payload.project_id
    )
    doc = await _persist_feedback(
        link=link,
        viewer=viewer,
        fb_type="text",
        talent_id=payload.talent_id,
        submission_id=payload.submission_id,
        project_id=payload.project_id,
        text=payload.text.strip(),
    )
    return doc


@router.post("/public/links/{slug}/feedback/voice")
async def create_voice_feedback(
    slug: str,
    talent_id: str = Form(...),
    submission_id: str = Form(...),
    project_id: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    viewer = decode_viewer(authorization)
    if not viewer or viewer.get("slug") != slug:
        raise HTTPException(401, "Identity required")
    link = await db.links.find_one({"slug": slug}, {"_id": 0})
    if not link:
        raise HTTPException(404, "Link not found")
    await _ensure_link_includes_subject(link, submission_id, talent_id, project_id)

    ct = (file.content_type or "").lower()
    if not (ct.startswith("audio/") or ct in {"video/webm", "video/ogg"}):
        # Browsers sometimes send recorder blobs as video/webm — accept the common ones.
        raise HTTPException(400, "Only audio recordings are accepted")
    data = await file.read()
    if len(data) > MAX_FEEDBACK_AUDIO_BYTES:
        cap_mb = MAX_FEEDBACK_AUDIO_BYTES // (1024 * 1024)
        raise HTTPException(400, f"Voice note too large — max {cap_mb} MB")

    feedback_id = str(uuid.uuid4())
    folder = f"{APP_NAME}/feedback/{submission_id}"
    result = cloudinary_upload(
        data,
        folder=folder,
        public_id=feedback_id,
        resource_type="video",  # Cloudinary stores audio under the video resource type
        content_type=ct or "application/octet-stream",
    )
    doc = await _persist_feedback(
        link=link,
        viewer=viewer,
        fb_type="voice",
        talent_id=talent_id,
        submission_id=submission_id,
        project_id=project_id,
        content_url=result["url"],
        content_type=ct,
    )
    return doc


# --------------------------------------------------------------------------
# Admin — moderation
# --------------------------------------------------------------------------
@router.get("/admin/feedback")
async def list_feedback(
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    submission_id: Optional[str] = None,
    page: Optional[int] = None,
    size: Optional[int] = None,
    admin: dict = Depends(current_team_or_admin),
):
    query: Dict[str, Any] = {}
    if status:
        if status not in FEEDBACK_STATUSES:
            raise HTTPException(400, "Invalid status filter")
        query["status"] = status
    if project_id:
        query["project_id"] = project_id
    if submission_id:
        query["submission_id"] = submission_id
    cursor = db.feedback.find(query, {"_id": 0}).sort("created_at", -1)
    if page is None:
        return await cursor.to_list(5000)
    skip, limit, p, s = _paginate_params(page, size)
    total = await db.feedback.count_documents(query)
    items = await cursor.skip(skip).limit(limit).to_list(limit)
    return _paginated(items, total, p, s)


@router.get("/admin/feedback/{fid}")
async def get_feedback(fid: str, admin: dict = Depends(current_team_or_admin)):
    doc = await db.feedback.find_one({"id": fid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Feedback not found")
    return doc


async def _set_decision(
    fid: str, *, status: str, admin: dict, share: bool
) -> dict:
    fb = await db.feedback.find_one({"id": fid}, {"_id": 0})
    if not fb:
        raise HTTPException(404, "Feedback not found")
    now = _now()
    update: Dict[str, Any] = {"status": status, "updated_at": now}
    if status == "approved":
        update.update({
            "approved_at": now,
            "approved_by": admin.get("id"),
            "visibility": "shared_with_talent" if share else "admin_only",
        })
    elif status == "rejected":
        update.update({
            "rejected_at": now,
            "rejected_by": admin.get("id"),
            "visibility": "admin_only",
        })
    await db.feedback.update_one({"id": fid}, {"$set": update})

    # Notify the rest of the team (other admins/team members) so multi-user
    # workflows stay in sync. We intentionally do NOT push to the talent's
    # device — the talent fetches approved feedback the next time they open
    # their submission link (see GET /api/public/submissions/{sid}).
    project = await db.projects.find_one(
        {"id": fb["project_id"]}, {"_id": 0, "brand_name": 1}
    )
    brand = (project or {}).get("brand_name") or "Project"
    if status == "approved":
        await notify_fanout(
            db,
            type="feedback_approved",
            title=f"Feedback shared with talent",
            body=f"{brand} — moderated and released.",
            payload={
                "feedback_id": fid,
                "submission_id": fb["submission_id"],
                "project_id": fb["project_id"],
            },
            actor_id=admin.get("id"),
        )
    elif status == "rejected":
        await notify_fanout(
            db,
            type="feedback_rejected",
            title="Feedback rejected",
            body=f"{brand} — not shared with talent.",
            payload={
                "feedback_id": fid,
                "submission_id": fb["submission_id"],
                "project_id": fb["project_id"],
            },
            actor_id=admin.get("id"),
        )
    return await db.feedback.find_one({"id": fid}, {"_id": 0})


@router.post("/admin/feedback/{fid}/approve")
async def approve_feedback(fid: str, admin: dict = Depends(current_team_or_admin)):
    return await _set_decision(fid, status="approved", admin=admin, share=True)


@router.post("/admin/feedback/{fid}/reject")
async def reject_feedback(fid: str, admin: dict = Depends(current_team_or_admin)):
    return await _set_decision(fid, status="rejected", admin=admin, share=False)


@router.post("/admin/feedback/{fid}/edit")
async def edit_feedback(
    fid: str,
    payload: FeedbackEditIn,
    admin: dict = Depends(current_team_or_admin),
):
    fb = await db.feedback.find_one({"id": fid}, {"_id": 0, "type": 1})
    if not fb:
        raise HTTPException(404, "Feedback not found")
    if fb.get("type") != "text":
        raise HTTPException(400, "Only text feedback can be edited")
    new_text = payload.text.strip()
    if not new_text:
        raise HTTPException(400, "Text cannot be empty")
    if len(new_text) > MAX_FEEDBACK_TEXT_LEN:
        raise HTTPException(400, "Text too long")
    now = _now()
    await db.feedback.update_one(
        {"id": fid},
        {"$set": {
            "text": new_text,
            "edited_at": now,
            "edited_by": admin.get("id"),
            "updated_at": now,
        }},
    )
    return await db.feedback.find_one({"id": fid}, {"_id": 0})


@router.delete("/admin/feedback/{fid}")
async def delete_feedback(
    fid: str, admin: dict = Depends(current_team_or_admin)
):
    res = await db.feedback.delete_one({"id": fid})
    if not res.deleted_count:
        raise HTTPException(404, "Feedback not found")
    return {"ok": True}


# --------------------------------------------------------------------------
# Talent-facing helper (consumed by submissions router)
# --------------------------------------------------------------------------
async def list_approved_feedback_for_talent(submission_id: str) -> List[dict]:
    """Return ONLY approved+shared feedback rows for the given submission.

    This is the ONLY surface through which a talent can ever see client
    feedback. Pending/rejected/admin_only rows are silently filtered out.
    """
    cur = db.feedback.find(
        {
            "submission_id": submission_id,
            "status": "approved",
            "visibility": "shared_with_talent",
        },
        {"_id": 0},
    ).sort("approved_at", 1)
    rows = await cur.to_list(500)
    return [_client_feedback_view(r) for r in rows]
