"""Personal Assistant — persistence. Everything is keyed by `owner_id` taken from the authenticated JWT user
(never from the request body); every read/update filters on it, so one user can never see, confirm, cancel or
retry another user's command or task, even with the id.

Collections (additive; nothing existing is touched):
  assistant_commands  one row per submitted message          unique (owner_id, client_request_id)
  assistant_tasks     one row per action parsed from it      index  (owner_id, created_at)
  assistant_messages  the user's private conversation        index  (owner_id, created_at)
  assistant_audit     consequential actions (ids only — no message bodies, no private media URLs)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from core import db

NEEDS_CLARIFICATION = "needs_clarification"
AWAITING_CONFIRMATION = "awaiting_confirmation"
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = {COMPLETED, PARTIAL, FAILED, CANCELLED}
ACTIVE = {QUEUED, RUNNING}
MAX_MESSAGES = 300

_indexes_ready = False


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    await db.assistant_commands.create_index([("owner_id", 1), ("client_request_id", 1)], unique=True, name="pa_cmd_owner_req")
    await db.assistant_tasks.create_index([("owner_id", 1), ("created_at", -1)], name="pa_task_owner_created")
    await db.assistant_tasks.create_index([("owner_id", 1), ("id", 1)], unique=True, name="pa_task_owner_id")
    await db.assistant_messages.create_index([("owner_id", 1), ("created_at", -1)], name="pa_msg_owner_created")
    _indexes_ready = True


def fingerprint(task: dict) -> str:
    """A short hash of everything an approval is for: the action kind and the exact resolved entities (talent / project ids, stage,
    template, recipient / destination group, and the content to be sent — a media link counts as "a link", not its token, so merely
    minting it later does not change it). Approving sends this back; if the plan has changed in any material way the hash differs and
    the approval is refused. Names and warnings are deliberately NOT part of it."""
    import hashlib
    import json
    p = task.get("params") or {}
    rec = p.get("recipient") or {}
    prev = task.get("preview") or {}
    body = {
        "k": task.get("kind"), "dep": sorted(task.get("depends_on") or []),
        "t": [e.get("id") for e in p.get("talents") or []], "p": [e.get("id") for e in p.get("projects") or []],
        "stage": p.get("stage"), "tpl": p.get("template_id"), "lk": p.get("lookup"),
        "rec": [rec.get("kind"), rec.get("id"), rec.get("project_id"), rec.get("phone"), rec.get("state")] if rec else None,
        "grp": prev.get("group"),
        "items": [[i.get("name"), i.get("state") or i.get("text")] for i in prev.get("items") or []],
        "dest": [[d.get("name"), d.get("kind"), d.get("text")] for d in prev.get("destinations") or []],
        "pre": sorted({(m.get("talent_id"), m.get("project_id")) for m in (p.get("prereq") or {}).get("missing") or [] if isinstance(m, dict)}),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:20]


def public(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return doc
    out = {k: v for k, v in doc.items() if k not in ("_id", "owner_id", "idempotency_key")}
    out["fingerprint"] = fingerprint(doc)
    return out


# -------------------------------------------------------------------------------------------------- commands
async def claim_command(owner_id: str, text: str, client_request_id: str, context: dict) -> tuple[dict, bool]:
    """Insert the command once per (owner, client_request_id). A double click / network retry returns the
    ORIGINAL command (created=False) instead of planning — and therefore creating tasks — twice."""
    await ensure_indexes()
    doc = {"id": new_id(), "owner_id": owner_id, "client_request_id": client_request_id, "text": text,
           "created_at": now(), "task_ids": [], "reply": None}
    try:
        await db.assistant_commands.insert_one(dict(doc))
        return doc, True
    except DuplicateKeyError:
        existing = await db.assistant_commands.find_one({"owner_id": owner_id, "client_request_id": client_request_id}, {"_id": 0})
        return existing, False


async def finish_command(owner_id: str, command_id: str, task_ids: List[str], reply: str) -> None:
    await db.assistant_commands.update_one({"owner_id": owner_id, "id": command_id}, {"$set": {"task_ids": task_ids, "reply": reply}})


# ----------------------------------------------------------------------------------------------------- tasks
async def insert_tasks(tasks: List[dict]) -> None:
    if tasks:
        await db.assistant_tasks.insert_many([dict(t) for t in tasks])


async def get_task(owner_id: str, task_id: str) -> Optional[dict]:
    return await db.assistant_tasks.find_one({"owner_id": owner_id, "id": task_id}, {"_id": 0})


async def list_tasks(owner_id: str, limit: int = 40, command_id: Optional[str] = None) -> List[dict]:
    q: Dict[str, Any] = {"owner_id": owner_id}
    if command_id:
        q["command_id"] = command_id
    return await db.assistant_tasks.find(q, {"_id": 0}).sort([("created_at", -1), ("seq", 1)]).limit(limit).to_list(limit)


async def update_task(owner_id: str, task_id: str, patch: dict, *, only_status: Optional[List[str]] = None) -> Optional[dict]:
    """Atomic update owned by `owner_id`; with `only_status` it is a compare-and-set (returns None if the task
    was not in one of those statuses — i.e. someone else already moved it)."""
    q: Dict[str, Any] = {"owner_id": owner_id, "id": task_id}
    if only_status:
        q["status"] = {"$in": only_status}
    patch = {**patch, "updated_at": now()}
    return await db.assistant_tasks.find_one_and_update(q, {"$set": patch}, return_document=True, projection={"_id": 0})


# -------------------------------------------------------------------------------------------------- messages
async def add_message(owner_id: str, role: str, text: str, command_id: Optional[str] = None) -> None:
    await ensure_indexes()
    await db.assistant_messages.insert_one({"id": new_id(), "owner_id": owner_id, "role": role, "text": text[:4000],
                                            "command_id": command_id, "created_at": now()})
    extra = await db.assistant_messages.count_documents({"owner_id": owner_id}) - MAX_MESSAGES
    if extra > 0:
        old = await db.assistant_messages.find({"owner_id": owner_id}, {"_id": 1}).sort("created_at", 1).limit(extra).to_list(extra)
        await db.assistant_messages.delete_many({"_id": {"$in": [o["_id"] for o in old]}})


async def history(owner_id: str, limit: int = 60) -> List[dict]:
    rows = await db.assistant_messages.find({"owner_id": owner_id}, {"_id": 0, "owner_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return list(reversed(rows))


async def clear_history(owner_id: str) -> int:
    return (await db.assistant_messages.delete_many({"owner_id": owner_id})).deleted_count


# ----------------------------------------------------------------------------------------------------- audit
async def audit(user: dict, action: str, task: dict, result: str, extra: Optional[dict] = None) -> None:
    """Who did what to which ids and how it ended. Deliberately no message text, phone numbers or media URLs."""
    p = task.get("params") or {}
    await db.assistant_audit.insert_one({
        "id": new_id(), "at": now(), "user_id": user.get("id"), "email": user.get("email"), "role": user.get("role"),
        "action": action, "task_id": task.get("id"), "kind": task.get("kind"),
        "talent_ids": [t.get("id") for t in p.get("talents", []) if t.get("id")],
        "project_ids": [x.get("id") for x in p.get("projects", []) if x.get("id")],
        "template_id": p.get("template_id"), "worker_id": p.get("worker_id"), "stage": p.get("stage"),
        "result": result, **(extra or {}),
    })
