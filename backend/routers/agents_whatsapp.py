"""HTTP surface for the WhatsApp Agent Platform.

Two kinds of routes:
  - `POST /inbound` — the transport seam. Any inbound-message source
    (a future WhatsApp Web DOM listener, or a Cloud API webhook) posts a
    normalized {group_name, sender_phone, text} event here; everything
    downstream is transport-agnostic. Protected by a shared secret since
    it's the one endpoint in this router with no admin session — see
    AGENTS_INBOUND_SECRET.
  - `/config/*` — admin-only CRUD over which WhatsApp groups/numbers route
    to which agent_id (the `whatsapp_agent_config` collection), so a group
    rename or a new allowed number never requires a code change.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from core import (
    create_or_resume_submission_doc,
    cloudinary_upload,
    current_admin,
    db,
    media_url,
    video_poster_url,
)
from agents import registry, tasks
from agents.dispatcher import handle_inbound_message
from agents.modules import media_assignment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/whatsapp", tags=["WhatsApp Agents"])

INBOUND_SECRET = os.environ.get("AGENTS_INBOUND_SECRET", "")


class InboundMessageIn(BaseModel):
    group_name: str = Field(..., min_length=1)
    sender_phone: str = Field(..., min_length=1)
    text: str = Field(default="")
    sender_name: Optional[str] = None
    message_id: Optional[str] = None
    # Set by the transport when it was able to determine, at receive time,
    # whether the sender is a current participant of the WhatsApp group —
    # only meaningful for agents configured with security_mode="group_members".
    # None means "transport couldn't tell" and is treated as not-a-member.
    sender_is_group_member: Optional[bool] = None
    # Voice transport interface — no transport populates these yet (no STT
    # engine is wired up), but the conversation engine already understands
    # both: a transcript's confidence score (gates a low-confidence
    # transcript behind an "Is that correct?" confirmation), and
    # media_type="voice_note" for a voice note that couldn't be
    # transcribed at all (with empty `text`) so the user gets a clear
    # reply instead of the message being silently dropped.
    transcript_confidence: Optional[float] = None
    media_type: Optional[str] = None
    # Concurrent Task Engine (2026-08-05) — the WhatsApp message id this
    # inbound message is a reply TO, if the transport could determine one.
    # None (the default, and what every existing transport call already
    # sends) means "not a reply" — dispatcher.py's task-routing branch is
    # then always skipped, so this is fully backward compatible.
    replied_to_message_id: Optional[str] = None
    # Step 2B (2026-08-05) — the inner text of the reply's "quoted message"
    # block, if the transport could read one (see whatsapp-worker/
    # inbound.py's _extract_reply_context). None means either "not a
    # reply" or "couldn't read the quote" — either way the task-routing
    # tier that uses this is simply skipped, fully backward compatible.
    replied_quoted_text: Optional[str] = None


class TaskSentIn(BaseModel):
    agent_id: str = Field(..., min_length=1)
    operation_id: str = Field(..., min_length=1)
    message_id: str = Field(..., min_length=1)


@router.get("/known-groups")
async def known_groups(x_internal_secret: Optional[str] = Header(default=None)):
    """Flat, de-duplicated list of every WhatsApp group name currently
    mapped to an active agent, across all agents. This is the ONLY thing a
    transport (the Playwright worker, or any future one) needs from the
    Agent Registry to decide which chats are worth watching at all — it
    never needs to know which agent owns which group, just which group
    names matter, so group names are never hardcoded in the transport.
    Same shared-secret gate as /inbound since it's still an unauthenticated
    (no admin session) endpoint."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    names: set[str] = set()
    cursor = db[registry.CONFIG_COLLECTION].find({"active": True})
    async for cfg in cursor:
        for g in cfg.get("group_names") or []:
            if g and g.strip():
                names.add(g.strip())
    return {"groups": sorted(names)}


@router.post("/inbound")
async def inbound_message(
    payload: InboundMessageIn,
    x_internal_secret: Optional[str] = Header(default=None),
):
    # http_request/http_response — the FastAPI/Starlette-level overhead
    # AROUND handle_inbound_message's own work (which logs its own
    # dispatch_timing/dispatch_breakdown lines). Pydantic body parsing and
    # the ASGI request itself happen before this function is even called,
    # so this measures everything this function is responsible for: the
    # shared-secret check, the dispatcher call, and response construction
    # — subtracting the dispatcher's own dispatch_ms from this total
    # isolates pure HTTP-layer overhead (Phase 1 of the latency
    # investigation, matching the requested "Backend HTTP Request" /
    # "HTTP Response" categories).
    t_http_start = time.monotonic()
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    t_auth_done = time.monotonic()
    result = await handle_inbound_message(
        group_name=payload.group_name,
        sender_phone=payload.sender_phone,
        text=payload.text,
        sender_name=payload.sender_name,
        sender_is_group_member=payload.sender_is_group_member,
        transcript_confidence=payload.transcript_confidence,
        media_type=payload.media_type,
        replied_to_message_id=payload.replied_to_message_id,
        replied_quoted_text=payload.replied_quoted_text,
    )
    t_dispatch_done = time.monotonic()
    # operation_id is only ever set when `reply` is a task's confirmation/
    # clarification card (Concurrent Task Engine) — None for every CRM
    # turn and every casting-agent turn that isn't task-related, so
    # existing callers that ignore this new field see no behavior change.
    response = {"handled": result.handled, "reply": result.reply, "operation_id": result.operation_id}
    http_request_ms = (t_auth_done - t_http_start) * 1000
    http_response_ms = (time.monotonic() - t_dispatch_done) * 1000
    logger.info(
        "inbound_http_timing group=%r http_request_ms=%.1f dispatch_ms=%.1f http_response_ms=%.1f",
        payload.group_name, http_request_ms, (t_dispatch_done - t_auth_done) * 1000, http_response_ms,
    )
    return response


@router.post("/task-sent")
async def task_sent(
    payload: TaskSentIn,
    x_internal_secret: Optional[str] = Header(default=None),
):
    """Concurrent Task Engine (2026-08-05) — the worker calls this right
    after it actually sends a task's confirmation/clarification card and
    learns the WhatsApp message id that card got. Patches
    agents.tasks.confirmation_message_id, which is what makes the task
    reply-addressable from this point on (see agents/tasks.py's module
    docstring). Same shared-secret gate as /inbound; a no-op (204, not an
    error) if the operation is unknown/already cleared — the worker fires
    this best-effort and should never fail loudly over a race with the
    task's own natural completion."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await tasks.set_confirmation_message_id(payload.agent_id, payload.operation_id, payload.message_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Media-Assignment (Phase 1, 2026-08-22) — bounded, on-demand bridge between
# the backend orchestrator (services/media_assignment_worker.py) and the
# WhatsApp Worker (whatsapp-worker/mark_scan.py). The worker is a dumb
# WhatsApp I/O layer here: it claims a request, does exactly what the
# request's `mode` says, and reports back — it never decides identity,
# project matching, or ambiguity; that all lives in agents/modules/
# media_assignment.py, on the backend side. See the "ticklish-cuddling-
# willow" plan for the full design and the module docstrings on both sides
# for the exact state machine.
# ---------------------------------------------------------------------------
class ScanResultIn(BaseModel):
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    # TEMPORARY (2026-08-23) — debug-only field for the E2E investigation;
    # remove once the scan-resolution mismatch is root-caused and fixed.
    debug: Optional[Dict[str, Any]] = None


class DownloadResultIn(BaseModel):
    results: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    # form_send_result (2026-08-25, SEND workflow only): the worker's own
    # outcome for req.get("form_message"), if one was included — None when
    # no form_message was sent on this request (already sent earlier, or
    # this isn't a SEND request at all). Kept fully separate from
    # `results`/media, matching form_sends' independent idempotency.
    form_send_result: Optional[Dict[str, Any]] = None


@router.get("/gunwanti-identity")
async def gunwanti_identity(x_internal_secret: Optional[str] = Header(default=None)):
    """Worker-side identity config lookup (not currently used by the
    worker for validation — the worker never checks identity, see module
    docstring above — but exposed for diagnostics/parity with
    /known-groups)."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    identity = await media_assignment.get_gunwanti_identity()
    return identity or {}


@router.post("/scan-requests/claim")
async def claim_scan_request(x_internal_secret: Optional[str] = Header(default=None)):
    """Atomic claim of the oldest pending scan OR download request — same
    find_one_and_update claim pattern worker.py's poll_and_process_jobs
    already uses for send jobs. Returns {} (no request body-less 204, to
    keep the worker's polling loop simple) when nothing is pending."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    now = datetime.now(timezone.utc)
    doc = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find_one_and_update(
        {"status": {"$in": [media_assignment.SCAN_STATUS_PENDING, media_assignment.DOWNLOAD_STATUS_PENDING]}},
        {"$set": {"status": "processing", "claimed_at": now}},
        sort=[("created_at", 1)],
        return_document=True,
    )
    if not doc:
        return {}
    doc.pop("_id", None)
    return doc


@router.post("/scan-requests/{request_id}/scan-result")
async def report_scan_result(
    request_id: str, payload: ScanResultIn,
    x_internal_secret: Optional[str] = Header(default=None),
):
    """Worker reports the raw candidate list after a bounded scan (see
    mark_scan.py) — no filtering/interpretation done here beyond storing
    it; agents/modules/media_assignment.py's validate_candidates (run by
    the backend orchestrator loop) owns everything downstream of this."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    status = media_assignment.SCAN_STATUS_FAILED if payload.error else media_assignment.SCAN_STATUS_DONE
    res = await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": request_id},
        {"$set": {
            "status": status, "candidates": payload.candidates, "scan_error": payload.error,
            "debug": payload.debug,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Unknown scan request")
    return {"ok": True}


@router.post("/scan-requests/{request_id}/download-result")
async def report_download_result(
    request_id: str, payload: DownloadResultIn,
    x_internal_secret: Optional[str] = Header(default=None),
):
    """Worker reports per-item download/handoff outcomes after a
    `mode="download"` request — the actual media bytes for each
    successfully-downloaded item were already POSTed individually to
    /media-upload; this just marks the overall request finished so the
    backend orchestrator can compose the final report."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    status = media_assignment.DOWNLOAD_STATUS_FAILED if payload.error else media_assignment.DOWNLOAD_STATUS_DONE
    res = await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": request_id},
        {"$set": {
            "status": status, "download_results": payload.results, "download_error": payload.error,
            "form_send_result": payload.form_send_result,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Unknown scan request")
    return {"ok": True}


@router.post("/media-upload")
async def media_upload(
    file: UploadFile = File(...),
    talent_id: str = Form(...),
    project_id: str = Form(...),
    media_role: str = Form(...),
    take_number: Optional[str] = Form(None),
    original_label: str = Form(...),
    source_message_id: str = Form(...),
    source_thumbnail_hash: str = Form(...),
    source_media_type: str = Form(...),
    source_group_id: Optional[str] = Form(None),
    source_group_name: Optional[str] = Form(None),
    source_sender: Optional[str] = Form(None),
    source_timestamp: Optional[str] = Form(None),
    mark_reply_message_id: Optional[str] = Form(None),
    mark_reply_text: Optional[str] = Form(None),
    mark_target_phone: Optional[str] = Form(None),
    mark_target_contact_id: Optional[str] = Form(None),
    x_internal_secret: Optional[str] = Header(default=None),
):
    """Worker hands off ONE downloaded WhatsApp media file here, to attach
    to the correct Talentgram submission — the only place actual media
    bytes cross the worker->backend boundary (scan/download-result above
    carry metadata only). Mirrors routers/submissions.py's admin_add_media
    two-step shape (upload bytes, then $push onto submission.media[]),
    just server-resolved by (talent_id, project_id) instead of an
    admin-authenticated (project_id, submission_id) pair, and additionally
    upserts the media_assignments audit row this feature's idempotency
    depends on.

    Talent must have an email on file — create_or_resume_submission_doc
    (like every other submission-creation path in this app) keys a
    submission by (project_id, talent_email). A WhatsApp-sourced talent
    with no email cannot get a submission created here; this is reported
    back to the caller (the backend orchestrator), never silently worked
    around with a synthesized email."""
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    talent = await db.talents.find_one({"id": talent_id})
    if not talent:
        raise HTTPException(status_code=404, detail="Talent not found")
    email = (talent.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=422,
            detail=f"{talent.get('name') or talent_id} has no email on file — required before a "
                    "submission can be created. Add one in Talentgram first.",
        )

    sub_result = await create_or_resume_submission_doc(
        project, email, talent.get("name") or "", talent.get("phone"), None, None,
        created_by="whatsapp-agent", created_from="whatsapp_media_assignment", talent_id=talent_id,
    )
    sid = sub_result["id"]

    data = await file.read()
    is_video = source_media_type == "video" or (file.content_type or "").startswith("video/")
    rt = "video" if is_video else "image"
    media_id = f"wa_{uuid.uuid4().hex[:8]}"
    folder = f"talentgram/projects/{project_id}/auditions/{talent_id}/whatsapp_media"

    result = cloudinary_upload(
        data, folder=folder, public_id=media_id, resource_type=rt,
        content_type=file.content_type, keep_original=False,
    )

    take_number_int: Optional[int] = None
    if take_number:
        try:
            take_number_int = int(take_number)
        except ValueError:
            take_number_int = None

    # Submission media category (2026-08-25 fix): must match the ONE
    # canonical value the Submission Review Center / Requirement Engine
    # recognize for each media type — verified against every other write
    # path in the codebase (routers/submissions.py, routers/
    # applications.py) and the frontend's own getCuratedMedia/
    # requirementEngine checks. "take" was already correct (media_role
    # IS the literal string "take"); "intro" was NOT — the recognized
    # category is "intro_video", not the bare role name. A real
    # production incident (Sharvari Kashid / Tapti AI App (Ananya))
    # proved this exactly: the Introduction video uploaded successfully
    # and had a real Cloudinary asset, but was invisible in Submission
    # Review Center ("Not submitted") because its category was "intro".
    # "photos" is intentionally left mapping to itself here — untouched,
    # out of scope for this fix.
    SUBMISSION_MEDIA_CATEGORY_BY_ROLE = {"take": "take", "intro": "intro_video"}
    media_obj: Dict[str, Any] = {
        "id": media_id,
        "category": SUBMISSION_MEDIA_CATEGORY_BY_ROLE.get(media_role, media_role),
        "url": result["url"],
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "content_type": file.content_type or "application/octet-stream",
        "original_filename": file.filename,
        "size": result.get("bytes") or len(data),
        "created_at": datetime.now(timezone.utc),
        "scope": "whatsapp_media_assignment",
        "submission_id": sid,
        "project_id": project_id,
        "admin_added": True,
        "admin_added_by": "whatsapp-agent",
        "label": original_label,
        "client_visible": True,
        "duration": result.get("duration"),
        "poster_url": video_poster_url(result["public_id"]) if rt == "video" else None,
        "thumbnail_url": (
            media_url(result["public_id"], preset="thumb", resource_type=rt) if rt == "image" else None
        ),
        "origin": "whatsapp",
        # Media-Assignment provenance — never present on any other media
        # item, kept here (in addition to the media_assignments collection)
        # so a submission's media list is independently auditable without a
        # join.
        "source_message_id": source_message_id,
        "source_thumbnail_hash": source_thumbnail_hash,
    }
    await db.submissions.update_one({"id": sid}, {"$push": {"media": media_obj}})

    await media_assignment.mark_assignment_status(
        talent_id, project_id, source_message_id, source_thumbnail_hash, media_assignment.ASSIGN_STATUS_UPLOADED,
        submission_id=sid, submission_media_id=media_id,
    )

    # Verify — never trust the write alone (matches this codebase's
    # existing "don't claim success on HTTP 200 alone" principle).
    fresh = await db.submissions.find_one({"id": sid, "media.id": media_id}, {"_id": 0, "id": 1})
    if not fresh:
        raise HTTPException(status_code=500, detail="Upload succeeded but could not be verified on the submission")

    return {"submission_id": sid, "media_id": media_id, "url": result["url"]}


class AgentConfigUpdate(BaseModel):
    group_names: Optional[List[str]] = None
    allowed_senders: Optional[List[str]] = None
    active: Optional[bool] = None
    # "allowlist" (default) or "group_members" — see agents/registry.py's
    # is_sender_allowed for what each mode means.
    security_mode: Optional[str] = None


def _serialise_config(doc: dict) -> dict:
    return {
        "agent_id": doc["agent_id"],
        "group_names": doc.get("group_names") or [],
        "allowed_senders": doc.get("allowed_senders") or [],
        "security_mode": doc.get("security_mode") or "allowlist",
        "active": doc.get("active", True),
        "updated_at": doc.get("updated_at"),
        # Set by the worker when a mapped group cannot be found in the
        # connected WhatsApp account — a configuration error the operator
        # must fix, surfaced so it is visible without reading worker logs.
        "config_status": doc.get("config_status"),
        "config_error": doc.get("config_error"),
        "config_error_group": doc.get("config_error_group"),
        "config_error_at": doc.get("config_error_at"),
    }


@router.get("/agents")
async def list_agents(_admin: dict = Depends(current_admin)):
    """All registered agents (code-level) alongside their current routing
    config (DB-level), for an admin settings screen."""
    out = []
    for agent in registry.list_agents():
        cfg = await registry.get_agent_config(agent.agent_id)
        out.append({
            "agent_id": agent.agent_id,
            "name": agent.name,
            "module": agent.module,
            "intents": [i.intent_id for i in agent.intents],
            "config": _serialise_config(cfg) if cfg else None,
        })
    return out


@router.get("/config/{agent_id}")
async def get_config(agent_id: str, _admin: dict = Depends(current_admin)):
    doc = await registry.get_agent_config(agent_id)
    if not doc:
        raise HTTPException(status_code=404, detail="No config for this agent_id")
    return _serialise_config(doc)


@router.put("/config/{agent_id}")
async def update_config(agent_id: str, payload: AgentConfigUpdate, _admin: dict = Depends(current_admin)):
    if not registry.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent_id")
    upd = {}
    if payload.group_names is not None:
        upd["group_names"] = [g.strip() for g in payload.group_names if g.strip()]
    if payload.allowed_senders is not None:
        upd["allowed_senders"] = [n.strip() for n in payload.allowed_senders if n.strip()]
    if payload.active is not None:
        upd["active"] = payload.active
    if payload.security_mode is not None:
        if payload.security_mode not in ("allowlist", "group_members"):
            raise HTTPException(400, "security_mode must be 'allowlist' or 'group_members'")
        upd["security_mode"] = payload.security_mode
    if not upd:
        raise HTTPException(status_code=400, detail="No fields to update")
    from datetime import datetime, timezone
    upd["updated_at"] = datetime.now(timezone.utc)
    # Correcting the config clears any INVALID_CONFIGURATION flag the worker
    # raised, so the operator sees the error resolve as soon as they fix the
    # mapping (the worker re-probes the group on its next groups refresh).
    unset = {"config_status": "", "config_error": "",
             "config_error_group": "", "config_error_at": ""}
    res = await db[registry.CONFIG_COLLECTION].update_one(
        {"agent_id": agent_id}, {"$set": upd, "$unset": unset}, upsert=True
    )
    doc = await registry.get_agent_config(agent_id)
    return _serialise_config(doc)


@router.get("/audit-log")
async def list_audit_log(
    agent_id: Optional[str] = None,
    limit: int = 100,
    _admin: dict = Depends(current_admin),
):
    query = {"agent_id": agent_id} if agent_id else {}
    cursor = db["whatsapp_agent_audit_log"].find(query).sort("timestamp", -1).limit(min(limit, 500))
    items = await cursor.to_list(length=None)
    for it in items:
        it["_id"] = str(it["_id"])
    return items
