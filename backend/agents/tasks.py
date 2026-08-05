"""Concurrent Task Store — many independently-addressable pending
operations per (agent_id, phone/group), instead of `conversation.py`'s one
pending conversation per phone.

This is ADDITIVE, not a replacement: `conversation.py`'s single-slot model
is untouched and keeps working exactly as before (that's what "reply with a
bare digit, no reply-to-message" still resolves against — see
dispatcher.py's routing priority). A domain module that wants concurrent,
reply-addressable operations (currently only casting-agent) ALSO creates a
task here on every fresh trigger, alongside the existing
`conversation.start_conversation` call it already makes. CRM never creates
tasks, so this module — and the dispatcher's reply-to-task routing branch
that reads it — has zero effect on CRM's behaviour.

One document per operation (not per phone), keyed by `operation_id` — the
SAME id shown in the confirmation card, used for the audit log entry, and
reused as the undo record's operation_id. `confirmation_message_id` is the
WhatsApp message id of the most recent confirmation/clarification card this
task sent; set separately (via `set_confirmation_message_id`) once the
worker learns it — see whatsapp-worker/inbound.py's post-send report and
routers/agents_whatsapp.py's `/task-sent` endpoint. Until that arrives, a
task exists but isn't yet reply-addressable — the caller falls back to
today's non-reply continuation for that turn, which already works
identically either way.

Collection: whatsapp_agent_tasks. TTL-indexed on `expires_at` (see
agents/__init__.py's index bootstrap) so an abandoned task self-cleans;
`clear_task` also deletes explicitly and immediately on any terminal state
(completed/cancelled/expired/archived) — matching "auto cleanup: remove
ONLY that operation, never affects others" (this collection has many
concurrent docs per phone, unlike conversation.py's single one).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from core import db
from agents import request_scope

COLLECTION = "whatsapp_agent_tasks"

DEFAULT_TTL_MINUTES = 30

# Operation lifecycle — each task progresses through a subset of these
# (not every task needs "clarifying"; auto-confirm skips straight from
# "created" to "executing"). Only ONE of completed/cancelled/expired/
# archived is ever reached — all four are terminal and immediately
# clear_task-ed, never left lingering for the TTL alone to catch.
STATUS_CREATED = "created"
STATUS_CLARIFYING = "clarifying"
STATUS_CONFIRMING = "confirming"
STATUS_APPROVED = "approved"
STATUS_EXECUTING = "executing"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_ARCHIVED = "archived"

# CP-YYYYMMDD-NNN, human-scannable in a rich confirmation card (matches the
# example format given for this sprint) while still being globally unique
# per day via a random 3-digit suffix — collisions are astronomically
# unlikely for this traffic volume, and operation_id is enforced unique at
# the index level regardless, so a collision would simply fail the insert
# rather than silently double-assign.
def _generate_operation_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:4].upper()
    return f"CP-{today}-{suffix}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_task(
    *,
    agent_id: str,
    phone: str,
    group_name: str,
    sender_name: Optional[str],
    intent_id: str,
    collected: Optional[Dict[str, str]] = None,
    original_text: str = "",
    reply_message_id: Optional[str] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> dict:
    """Creates one new, independent task — never replaces an existing one
    (that's the entire point: N of these can coexist for the same phone,
    unlike conversation.start_conversation's upsert-replace)."""
    now = _now()
    doc = {
        "agent_id": agent_id,
        "phone": phone,
        "group_name": group_name,
        "sender_name": sender_name,
        "intent_id": intent_id,
        "collected": collected or {},
        "original_text": original_text,
        "status": STATUS_CREATED,
        "confirmation_message_id": None,
        "reply_message_id": reply_message_id,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(minutes=ttl_minutes),
    }
    # _generate_operation_id's 4-hex-char/day suffix (65536 slots) makes a
    # same-day collision unlikely for real traffic but not impossible under
    # heavy volume — retry with a freshly generated id on the (rare) unique-
    # index conflict rather than letting it surface as an unhandled 500.
    with request_scope.op("create_task", "conversation_state", collection=COLLECTION, cache=None):
        for _ in range(5):
            doc["operation_id"] = _generate_operation_id()
            try:
                await db[COLLECTION].insert_one(dict(doc))
                break
            except DuplicateKeyError:
                continue
        else:
            raise RuntimeError("create_task: exhausted retries generating a unique operation_id")
    return doc


async def get_task(agent_id: str, operation_id: str) -> Optional[dict]:
    with request_scope.op("get_task", "conversation_state", collection=COLLECTION, cache="miss"):
        return await db[COLLECTION].find_one({"agent_id": agent_id, "operation_id": operation_id})


async def get_task_by_confirmation_message_id(agent_id: str, message_id: str) -> Optional[dict]:
    """The reply-to-message resolver: WhatsApp reply -> message id -> this
    lookup -> operation_id -> resume only that operation. Never scans or
    inspects any OTHER pending task."""
    if not message_id:
        return None
    with request_scope.op("load_reply_mapping", "conversation_state", collection=COLLECTION, cache="miss"):
        return await db[COLLECTION].find_one(
            {"agent_id": agent_id, "confirmation_message_id": message_id}
        )


async def list_pending_tasks(agent_id: str, phone: str) -> List[dict]:
    """Every task still in flight for this phone — used only for the
    "existing session fallback" tier (lowest routing priority) and
    diagnostics, never for reply resolution (which always goes straight to
    ONE task by confirmation_message_id, not a scan)."""
    with request_scope.op("list_pending_tasks", "conversation_state", collection=COLLECTION, cache="miss"):
        cursor = db[COLLECTION].find({"agent_id": agent_id, "phone": phone})
        return await cursor.to_list(50)


async def update_task(
    agent_id: str, operation_id: str, *, ttl_minutes: int = DEFAULT_TTL_MINUTES, **patch: Any,
) -> Optional[dict]:
    now = _now()
    to_set = dict(patch)
    to_set["updated_at"] = now
    to_set["expires_at"] = now + timedelta(minutes=ttl_minutes)
    with request_scope.op("save_pending_operation", "conversation_state", collection=COLLECTION, cache=None):
        await db[COLLECTION].update_one(
            {"agent_id": agent_id, "operation_id": operation_id}, {"$set": to_set},
        )
    return await get_task(agent_id, operation_id)


async def set_confirmation_message_id(agent_id: str, operation_id: str, message_id: str) -> None:
    """Called once the worker reports back which WhatsApp message id its
    just-sent confirmation/clarification card actually got (see
    routers/agents_whatsapp.py's /task-sent endpoint) — this is what makes
    the task reply-addressable from this point on."""
    with request_scope.op("save_pending_operation", "conversation_state", collection=COLLECTION, cache=None):
        await db[COLLECTION].update_one(
            {"agent_id": agent_id, "operation_id": operation_id},
            {"$set": {"confirmation_message_id": message_id, "updated_at": _now()}},
        )


async def clear_task(agent_id: str, operation_id: str) -> None:
    """Removes ONLY this one operation — every other pending task for the
    same or a different phone is completely untouched (this collection
    holds many independent docs, never a single shared slot)."""
    with request_scope.op("clear_completed_operation", "conversation_state", collection=COLLECTION, cache=None):
        await db[COLLECTION].delete_one({"agent_id": agent_id, "operation_id": operation_id})


def is_expired(task: dict) -> bool:
    expires_at = task.get("expires_at")
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return _now() >= expires_at
