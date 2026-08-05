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
from agents import request_scope

COLLECTION = "whatsapp_agent_sessions"

DEFAULT_TTL_MINUTES = 75


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_key(agent_id: str, phone: str):
    return ("session", agent_id, phone)


async def get_session(agent_id: str, phone: str) -> Optional[dict]:
    # Real, measured redundancy: within a single turn, more than one hook
    # can independently ask for the same session (e.g. parse_edits_async
    # fetches it, then build_confirmation fetches it again moments later
    # in the same turn) — request_scope's cache is reset at the top of
    # every dispatch turn, so this can never serve stale data ACROSS
    # turns, only avoid a genuinely redundant round trip WITHIN one.
    found, cached = request_scope.cache_get(_cache_key(agent_id, phone))
    if found:
        return cached
    with request_scope.stage("conversation_state"):
        session = await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})
    if session and is_expired(session):
        await clear_session(agent_id, phone)
        return None
    request_scope.cache_set(_cache_key(agent_id, phone), session)
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
    with request_scope.stage("conversation_state"):
        await db[COLLECTION].update_one(
            {"agent_id": agent_id, "phone": phone},
            {
                "$set": to_set,
                "$setOnInsert": {"agent_id": agent_id, "phone": phone, "created_at": now},
            },
            upsert=True,
        )
        result = await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})
    # Keep the per-turn cache in sync with the write, so a get_session
    # call later in this SAME turn sees the freshly patched doc, not a
    # stale pre-write snapshot.
    request_scope.cache_set(_cache_key(agent_id, phone), result)
    return result


async def clear_session(agent_id: str, phone: str) -> None:
    with request_scope.stage("conversation_state"):
        await db[COLLECTION].delete_one({"agent_id": agent_id, "phone": phone})
    request_scope.cache_set(_cache_key(agent_id, phone), None)
