"""Session Context — cross-command conversational state for agents whose
commands span many separate turns without repeating themselves (e.g.
"Project 4" -> "Show Approved" -> "Move 2,5,8"), as opposed to
`conversation.py`'s whatsapp_conversations, which holds exactly one
in-progress intent's field-collection state and is deleted the moment that
intent is approved/edited/cancelled (see conversation.clear_conversation) —
it cannot outlive a single command.

One mutable document per (agent_id, phone), TTL-refreshed on every turn so
an actively-chatting user's context survives, while an abandoned session
self-cleans. Collection: whatsapp_agent_sessions.

Domain-agnostic on purpose (mirrors conversation.py's own scope): this
module has no idea what a "project" or "pipeline" is — it just persists
whatever a domain module's executor tells it to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core import db

COLLECTION = "whatsapp_agent_sessions"

DEFAULT_TTL_MINUTES = 75


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_session(agent_id: str, phone: str) -> Optional[dict]:
    session = await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})
    if session and is_expired(session):
        await clear_session(agent_id, phone)
        return None
    return session


def is_expired(session: dict) -> bool:
    expires_at = session.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return _now() >= expires_at


async def update_session(
    agent_id: str,
    phone: str,
    *,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    **patch: Any,
) -> Dict[str, Any]:
    """Patch fields on the session (creating it if this is the first turn)
    and bump its TTL — every turn extends the window, same pattern as
    conversation.update_conversation."""
    now = _now()
    to_set = dict(patch)
    to_set["updated_at"] = now
    to_set["expires_at"] = now + timedelta(minutes=ttl_minutes)
    await db[COLLECTION].update_one(
        {"agent_id": agent_id, "phone": phone},
        {
            "$set": to_set,
            "$setOnInsert": {"agent_id": agent_id, "phone": phone, "created_at": now},
        },
        upsert=True,
    )
    return await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})


async def clear_session(agent_id: str, phone: str) -> None:
    await db[COLLECTION].delete_one({"agent_id": agent_id, "phone": phone})
