"""Append-only audit trail for Simple Assistant executions (Phase 3).

Reuses the existing agent-platform audit **collection**
(``whatsapp_agent_audit_log``) and its indexes/retention rather than
standing up a parallel store — an admin can already view it via
``GET /api/agents/whatsapp/audit-log``. Simple Assistant rows carry the
same top-level fields every agent turn does, plus one structured
``sa_action`` sub-document with the Phase-3 specifics (project, per-talent
previous → new state, per-talent outcome).

Every real mutation writes exactly one row here — success or failure —
and it is also the idempotency ledger (a completed row is keyed by
``sa_action.plan_id``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import db

COLLECTION = "whatsapp_agent_audit_log"
AGENT_ID = "simple-assistant"
logger = logging.getLogger(__name__)


async def find_completed(plan_id: str) -> Optional[dict]:
    """A prior successful/attempted execution of this exact plan, if any —
    the idempotency guard. Returns None if never executed."""
    try:
        return await db[COLLECTION].find_one(
            {"agent_id": AGENT_ID, "sa_action.plan_id": plan_id, "sa_action.executed": True}
        )
    except Exception:
        logger.exception("simple_assistant audit idempotency lookup failed")
        return None


async def record(
    *,
    plan_id: str,
    conversation_id: Optional[str],
    user: Dict[str, Any],
    action_type: str,
    project_id: Optional[str],
    project_label: Optional[str],
    target_stage: Optional[str],
    outcomes: List[Dict[str, Any]],
    executed: bool,
    error: Optional[str] = None,
) -> None:
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1

    doc = {
        "timestamp": datetime.now(timezone.utc),
        "agent_id": AGENT_ID,
        "group_name": "__web__",
        "sender_phone": f"user:{user.get('id') or user.get('email') or 'unknown'}",
        "raw_message": f"[simple-assistant] {action_type}",
        "conversation_id": conversation_id,
        "parsed_intent": action_type,
        "confirmation_action": "execute" if executed else "rejected",
        "execution_result": None if error else _summary(counts),
        "error": error,
        "sa_action": {
            "plan_id": plan_id,
            "executed": executed,
            "action_type": action_type,
            "actor_id": user.get("id"),
            "actor_email": user.get("email"),
            "actor_role": user.get("role"),
            "project_id": project_id,
            "project_label": project_label,
            "target_stage": target_stage,
            "counts": counts,
            "talents": [
                {
                    "talent_id": o.get("talent_id"),
                    "talent_label": o.get("talent_label"),
                    "previous_state": o.get("from"),
                    "new_state": o.get("to"),
                    "outcome": o.get("status"),
                    "reason": o.get("reason"),
                }
                for o in outcomes
            ],
        },
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        # Never let an audit failure take down the user's response — same
        # deliberate swallow as agents/audit.py.
        logger.exception("failed to write simple_assistant audit row")


def _summary(counts: Dict[str, int]) -> str:
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no changes"


async def record_status(
    *,
    conversation_id: Optional[str],
    user: Dict[str, Any],
    query_kind: str,                     # "delivery" | "inbound" | "activity" | "retry_blocked" | "inbound_intelligence"
    talent_id: Optional[str] = None,
    talent_label: Optional[str] = None,
    project_id: Optional[str] = None,
    project_label: Optional[str] = None,
    plan_id: Optional[str] = None,
    result: Optional[str] = None,
    action_type: str = "inspect_whatsapp_status",
) -> None:
    """Read-only inspection marker. Writes ONE row to the SA audit trail —
    never touches whatsapp_jobs / whatsapp_batches / worker state, and NEVER
    records a proposed response as a sent one (`executed` is always False)."""
    doc = {
        "timestamp": datetime.now(timezone.utc),
        "agent_id": AGENT_ID,
        "group_name": "__web__",
        "sender_phone": f"user:{user.get('id') or user.get('email') or 'unknown'}",
        "raw_message": f"[simple-assistant] {action_type}",
        "conversation_id": conversation_id,
        "parsed_intent": action_type,
        "confirmation_action": "inspect",
        "execution_result": result or "read-only",
        "error": None,
        "sa_action": {
            "executed": False,
            "action_type": action_type,
            "query_kind": query_kind,
            "actor_id": user.get("id"),
            "actor_email": user.get("email"),
            "actor_role": user.get("role"),
            "talent_id": talent_id,
            "talent_label": talent_label,
            "project_id": project_id,
            "project_label": project_label,
            "inspected_plan_id": plan_id,
        },
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("failed to write simple_assistant status-inspection audit row")


async def record_ai(
    *,
    action_type: str,                  # "generate_ai_response" | "approve_ai_response"
    executed: bool,
    conversation_id: Optional[str],
    user: Dict[str, Any],
    plan_id: Optional[str],
    inbound_id: Optional[str],
    talent_id: Optional[str] = None,
    talent_label: Optional[str] = None,
    project_id: Optional[str] = None,
    project_label: Optional[str] = None,
    topic: Optional[str] = None,
    answerability: Optional[str] = None,
    draft_hash: Optional[str] = None,
    fact_hash: Optional[str] = None,
    destination: Optional[str] = None,
    destination_type: Optional[str] = None,
    batch_id: Optional[str] = None,
    result: Optional[str] = None,
    edited: bool = False,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    latency_ms: Optional[int] = None,
    validation: Optional[str] = None,
) -> None:
    """One row per AI generation / approval. `executed` is True ONLY for an
    approved-and-queued send — a proposed/generated draft is never `executed`.
    Stores hashes + ids + safe metrics (model id, provider name, latency,
    validation verdict), NEVER the prompt, provider key, or raw model
    output."""
    doc = {
        "timestamp": datetime.now(timezone.utc),
        "agent_id": AGENT_ID,
        "group_name": "__web__",
        "sender_phone": f"user:{user.get('id') or user.get('email') or 'unknown'}",
        "raw_message": f"[simple-assistant] {action_type}",
        "conversation_id": conversation_id,
        "parsed_intent": action_type,
        "confirmation_action": "approve" if (executed and action_type == "approve_ai_response") else "inspect",
        "execution_result": result or ("queued" if executed else "draft"),
        "error": None,
        "sa_action": {
            "executed": executed,
            "action_type": action_type,
            "actor_id": user.get("id"),
            "actor_email": user.get("email"),
            "actor_role": user.get("role"),
            "plan_id": plan_id,
            "inbound_id": inbound_id,
            "talent_id": talent_id,
            "talent_label": talent_label,
            "project_id": project_id,
            "project_label": project_label,
            "topic": topic,
            "answerability": answerability,
            "draft_hash": draft_hash,
            "fact_hash": fact_hash,
            "destination": destination,
            "destination_type": destination_type,
            "batch_id": batch_id,
            "edited": edited,
            "model": model,
            "provider": provider,
            "latency_ms": latency_ms,
            "validation": validation,
        },
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("failed to write simple_assistant ai-response audit row")


async def record_comm(
    *,
    plan_id: str,
    conversation_id: Optional[str],
    user: Dict[str, Any],
    action_type: str,
    project_id: Optional[str],
    project_label: Optional[str],
    talent_id: Optional[str],
    talent_label: Optional[str],
    destination: Optional[str],
    destination_type: Optional[str],
    media_ids: List[Optional[str]],
    outcomes: List[Dict[str, Any]],
    batch_ids: List[str],
    executed: bool,
) -> None:
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    doc = {
        "timestamp": datetime.now(timezone.utc),
        "agent_id": AGENT_ID,
        "group_name": "__web__",
        "sender_phone": f"user:{user.get('id') or user.get('email') or 'unknown'}",
        "raw_message": f"[simple-assistant] {action_type}",
        "conversation_id": conversation_id,
        "parsed_intent": action_type,
        "confirmation_action": "send" if executed else "rejected",
        "execution_result": _summary(counts),
        "error": None,
        "sa_action": {
            "plan_id": plan_id,
            "executed": executed,
            "action_type": action_type,
            "actor_id": user.get("id"),
            "actor_email": user.get("email"),
            "actor_role": user.get("role"),
            "project_id": project_id,
            "project_label": project_label,
            "talent_id": talent_id,
            "talent_label": talent_label,
            # never the media bytes/URLs — ids + types only
            "destination": destination,
            "destination_type": destination_type,
            "media_ids": [m for m in media_ids if m],
            "counts": counts,
            "batch_ids": batch_ids,
            "outcomes": [
                {"label": o.get("label"), "status": o.get("status"), "reason": o.get("reason")}
                for o in outcomes
            ],
        },
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("failed to write simple_assistant comm audit row")


async def record_submission(
    *,
    plan_id: str,
    conversation_id: Optional[str],
    user: Dict[str, Any],
    project_id: Optional[str],
    project_label: Optional[str],
    talent_id: Optional[str],
    talent_label: Optional[str],
    submission_id: Optional[str],
    outcomes: List[Dict[str, Any]],
    executed: bool,
    action_type: str = "attach_media",
    file_meta: Optional[Dict[str, Any]] = None,
) -> None:
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    sa_action = {
        "plan_id": plan_id,
        "executed": executed,
        "action_type": action_type,
        "actor_id": user.get("id"),
        "actor_email": user.get("email"),
        "actor_role": user.get("role"),
        "project_id": project_id,
        "project_label": project_label,
        "talent_id": talent_id,
        "talent_label": talent_label,
        "submission_id": submission_id,
    }
    if file_meta:
        # audit-safe file metadata ONLY — no bytes, no signed URL, no public_id
        sa_action["file"] = {
            k: file_meta.get(k) for k in ("filename", "size", "content_type", "sha256", "target_category")
            if file_meta.get(k) is not None
        }
    doc = {
        "timestamp": datetime.now(timezone.utc),
        "agent_id": AGENT_ID,
        "group_name": "__web__",
        "sender_phone": f"user:{user.get('id') or user.get('email') or 'unknown'}",
        "raw_message": f"[simple-assistant] {action_type}",
        "conversation_id": conversation_id,
        "parsed_intent": action_type,
        "confirmation_action": ("upload" if action_type == "upload_audition" else "attach") if executed else "rejected",
        "execution_result": _summary(counts),
        "error": None,
        "sa_action": {
            **sa_action,
            # media ids + per-item outcome only — never URLs / bytes
            "previous_state": [
                {"label": o.get("label"), "status": o.get("status")}
                for o in outcomes
            ],
            "counts": counts,
            "outcomes": [
                {"label": o.get("label"), "status": o.get("status"), "reason": o.get("reason"),
                 "submission_media_id": None}
                for o in outcomes
            ],
        },
    }
    try:
        await db[COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("failed to write simple_assistant submission audit row")
