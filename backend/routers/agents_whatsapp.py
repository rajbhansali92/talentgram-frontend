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
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from core import current_admin, db
from agents import registry, tasks
from agents.dispatcher import handle_inbound_message

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
