"""Conversation State Manager.

One mutable document per (agent_id, phone) tracks an in-progress
multi-turn command. Modelled on two existing patterns in this codebase:
`db.applications` (single doc per session, `status` drives the step,
idempotent `$set` patches) and `db.otp_codes` (short-lived pending state
with `expires_at` TTL + a turn counter). A phone number can only have one
active conversation per agent at a time — a new trigger message restarts
it (see dispatcher.py).

Collection: whatsapp_conversations. TTL-indexed on `expires_at` (see
core.py's index bootstrap) so abandoned conversations self-clean; nothing
here needs a background sweep job.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core import db

COLLECTION = "whatsapp_conversations"

DEFAULT_TTL_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_conversation(agent_id: str, phone: str) -> Optional[dict]:
    return await db[COLLECTION].find_one({"agent_id": agent_id, "phone": phone})


def is_expired(conv: dict) -> bool:
    expires_at = conv.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return _now() >= expires_at


async def start_conversation(
    *,
    agent_id: str,
    phone: str,
    group_name: str,
    intent_id: str,
    collected: Optional[Dict[str, str]] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict:
    """Create a fresh conversation, replacing any prior one for this
    (agent_id, phone) pair (upsert — restarting is always safe)."""
    now = _now()
    doc = {
        "agent_id": agent_id,
        "phone": phone,
        "group_name": group_name,
        "intent_id": intent_id,
        "step": "collecting",
        "collected": collected or {},
        "turn_count": 0,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(minutes=ttl_minutes),
    }
    await db[COLLECTION].replace_one(
        {"agent_id": agent_id, "phone": phone}, doc, upsert=True
    )
    return doc


async def update_conversation(
    agent_id: str,
    phone: str,
    *,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    **patch: Any,
) -> None:
    """Patch fields on the conversation and bump its TTL — every turn
    extends the "reasonable pause" window rather than counting down from
    the original start time."""
    now = _now()
    to_set = dict(patch)
    to_set["updated_at"] = now
    to_set["expires_at"] = now + timedelta(minutes=ttl_minutes)
    await db[COLLECTION].update_one(
        {"agent_id": agent_id, "phone": phone},
        {"$set": to_set, "$inc": {"turn_count": 1}},
    )


async def clear_conversation(agent_id: str, phone: str) -> None:
    await db[COLLECTION].delete_one({"agent_id": agent_id, "phone": phone})
