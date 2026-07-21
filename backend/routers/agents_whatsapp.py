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

import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from core import current_admin, db
from agents import registry
from agents.dispatcher import handle_inbound_message

router = APIRouter(prefix="/api/agents/whatsapp", tags=["WhatsApp Agents"])

INBOUND_SECRET = os.environ.get("AGENTS_INBOUND_SECRET", "")


class InboundMessageIn(BaseModel):
    group_name: str = Field(..., min_length=1)
    sender_phone: str = Field(..., min_length=1)
    text: str = Field(default="")
    sender_name: Optional[str] = None
    message_id: Optional[str] = None


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
    if INBOUND_SECRET and x_internal_secret != INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = await handle_inbound_message(
        group_name=payload.group_name,
        sender_phone=payload.sender_phone,
        text=payload.text,
        sender_name=payload.sender_name,
    )
    return {"handled": result.handled, "reply": result.reply}


class AgentConfigUpdate(BaseModel):
    group_names: Optional[List[str]] = None
    allowed_senders: Optional[List[str]] = None
    active: Optional[bool] = None


def _serialise_config(doc: dict) -> dict:
    return {
        "agent_id": doc["agent_id"],
        "group_names": doc.get("group_names") or [],
        "allowed_senders": doc.get("allowed_senders") or [],
        "active": doc.get("active", True),
        "updated_at": doc.get("updated_at"),
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
    if not upd:
        raise HTTPException(status_code=400, detail="No fields to update")
    from datetime import datetime, timezone
    upd["updated_at"] = datetime.now(timezone.utc)
    res = await db[registry.CONFIG_COLLECTION].update_one(
        {"agent_id": agent_id}, {"$set": upd}, upsert=True
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
