"""Undo Store — short-lived, one-shot "last operation" memory so a domain
module's executor can offer "reply UNDO to restore" without inventing its
own storage. Domain-agnostic (mirrors conversation.py / session_context.py):
this module has no idea what a "move" is, it just persists whatever
free-form `operation` dict a domain module hands it and gives it back once.

One document per (agent_id, phone) — a fresh operation replaces whatever
was there before (only the MOST RECENT operation is undoable, matching
"reply UNDO within 5 minutes" being about the last thing you just did, not
an arbitrary history). Collection: whatsapp_agent_undo.

Expiry is exposed as two distinct outcomes rather than collapsed into one
"not found" — `get_undo` returns the raw doc (or None) and leaves the
caller to check `is_expired`, because the domain module needs to tell
"Undo period has expired." apart from "No recent operation available to
undo." (see agents/modules/casting_pipeline.py's _undo_executor).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core import db

COLLECTION = "whatsapp_agent_undo"

DEFAULT_TTL_MINUTES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def store_undo(
    agent_id: str, phone: str, operation: Dict[str, Any], *, ttl_minutes: int = DEFAULT_TTL_MINUTES
) -> None:
    """Replace whatever undo record existed for this (agent_id, phone) with
    a fresh one — a new successful operation supersedes any prior
    still-undoable one."""
    now = _now()
    doc = {
        "agent_id": agent_id,
        "phone": phone,
        "operation": operation,
        "created_at": now,
        "expires_at": now + timedelta(minutes=ttl_minutes),
    }
    await db[COLLECTION].replace_one({"agent_id": agent_id, "phone": phone}, doc, upsert=True)


async def get_undo(agent_id: str, phone: str) -> Optional[dict]:
    return await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})


def is_expired(doc: dict) -> bool:
    expires_at = doc.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return _now() >= expires_at


async def clear_undo(agent_id: str, phone: str) -> None:
    await db[COLLECTION].delete_one({"agent_id": agent_id, "phone": phone})
