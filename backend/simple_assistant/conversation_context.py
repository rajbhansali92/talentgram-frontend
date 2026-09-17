"""Phase 12 — bounded, project-scoped conversation-continuity read adapter.

READ-ONLY. Given a talent + a RESOLVED project + the current inbound
message, this returns a small, time-bounded slice of the recent
back-and-forth so the Phase 9/10 draft can read like a continuation of the
same conversation instead of a cold standalone answer.

What it combines (all already captured by earlier phases — nothing new is
written anywhere):

  * inbound   — backend/inbound_messages.py `whatsapp_inbound_messages`
                (Phase 7), filtered to THIS talent AND THIS project.
  * outbound  — the EXISTING WhatsApp Engine's `whatsapp_jobs.message_body`
                for batches that are provably tied to THIS project:
                  - `whatsapp_batches` with project_id == the scope project
                  - batch_ids recorded on Simple Assistant's own approved
                    `approve_ai_response` audit rows for (talent, project)
                Only delivered jobs (status sent / verified) are used.

Hard boundaries (enforced by having no such imports / calls here):
  * NO LLM. NO send / batch / job creation. NO Playwright.
  * NO pipeline / submission / talent / project / Cloudinary mutation.
  * NEVER crosses project scope: with no resolved project_id this returns
    an empty context (never a talent's whole WhatsApp history).
  * The hidden-budget rule (Phase 9/10) is preserved — when
    `hide_budget_from_talent` is set, every amount token is redacted from
    every historical line BEFORE it can reach the model prompt.

Feature-flagged: SA_CONVERSATION_CONTEXT_ENABLED (default OFF). When off,
`build()` returns an empty context and the Phase 9/10 prompt is byte-for-
byte what it was before Phase 12.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from simple_assistant.readonly_db import RDB

logger = logging.getLogger(__name__)

_SA_AGENT_ID = "simple-assistant"
_DELIVERED = ("sent", "verified")

# Same currency/amount shape the Phase 9 validator uses — kept local so this
# module has no import cycle with ai_response.
_MONEY_RE = re.compile(
    r"(?:₹|\brs\.?\s?|\binr\s?)\s?[\d][\d,]*(?:\.\d+)?|"
    r"\b\d[\d,]{2,}(?:\.\d+)?\s?(?:k|lakh|lakhs|cr|crore)\b",
    re.I,
)


def is_enabled() -> bool:
    return os.environ.get("SA_CONVERSATION_CONTEXT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def history_limit() -> int:
    try:
        return max(1, min(int(os.environ.get("SA_CONVERSATION_HISTORY_LIMIT", "8")), 20))
    except ValueError:
        return 8


def history_hours() -> int:
    try:
        return max(1, min(int(os.environ.get("SA_CONVERSATION_HISTORY_HOURS", "168")), 720))
    except ValueError:
        return 168


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Any) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _redact_amounts(text: str) -> str:
    return _MONEY_RE.sub("[amount]", text or "")


def _empty(reason: str, *, window_hours: int, limit: int) -> Dict[str, Any]:
    return {
        "recent_messages": [],
        "truncated": False,
        "window_hours": window_hours,
        "limit": limit,
        "digest": hashlib.sha256(b"[]").hexdigest(),
        "source_metadata": {"reason": reason, "inbound_count": 0, "outbound_count": 0,
                            "budget_redacted": False},
    }


async def _project_batch_ids(project_id: str, talent_id: str) -> List[str]:
    ids: set[str] = set()
    # a) casting/campaign sends the engine itself tagged with this project
    for b in await RDB.whatsapp_batches.find(
        {"project_id": project_id}, {"_id": 0, "id": 1}
    ).to_list(500):
        if b.get("id"):
            ids.add(b["id"])
    # b) Simple Assistant's own approved replies for exactly this (talent, project)
    rows = await RDB.whatsapp_agent_audit_log.find(
        {"agent_id": _SA_AGENT_ID, "sa_action.executed": True,
         "sa_action.action_type": "approve_ai_response",
         "sa_action.talent_id": talent_id, "sa_action.project_id": project_id},
        {"_id": 0, "sa_action": 1},
    ).to_list(100)
    for r in rows:
        bid = (r.get("sa_action") or {}).get("batch_id")
        if bid:
            ids.add(bid)
    return sorted(ids)


async def _outbound_messages(project_id: str, talent_id: str, cutoff: datetime) -> List[Dict[str, Any]]:
    batch_ids = await _project_batch_ids(project_id, talent_id)
    if not batch_ids:
        return []
    jobs = await RDB.whatsapp_jobs.find(
        {"batch_id": {"$in": batch_ids}, "talent_id": talent_id,
         "status": {"$in": list(_DELIVERED)}},
        {"_id": 0, "message_body": 1, "sent_at": 1, "created_at": 1},
    ).to_list(200)
    out: List[Dict[str, Any]] = []
    for j in jobs:
        ts = _aware(j.get("sent_at")) or _aware(j.get("created_at"))
        if not ts or ts < cutoff:
            continue
        body = (j.get("message_body") or "").strip()
        if not body:
            continue
        out.append({"direction": "out", "timestamp": ts, "message_text": body,
                    "source": "whatsapp_engine"})
    return out


async def _inbound_messages(
    project_id: str, talent_id: str, cutoff: datetime, exclude_id: Optional[str],
) -> List[Dict[str, Any]]:
    docs = await RDB.whatsapp_inbound_messages.find(
        {"talent_id": talent_id, "project_id": project_id},
        {"_id": 0, "id": 1, "message_text": 1, "received_at": 1},
    ).sort("received_at", -1).to_list(200)
    out: List[Dict[str, Any]] = []
    for d in docs:
        if exclude_id and d.get("id") == exclude_id:
            continue
        ts = _aware(d.get("received_at"))
        if not ts or ts < cutoff:
            continue
        out.append({"direction": "in", "timestamp": ts,
                    "message_text": (d.get("message_text") or "").strip(),
                    "source": "whatsapp_worker"})
    return out


async def build(
    *,
    talent_id: Optional[str],
    project_id: Optional[str],
    current_inbound_id: Optional[str] = None,
    project: Optional[dict] = None,
) -> Dict[str, Any]:
    """→ {recent_messages: [{direction, timestamp, message_text, source}],
          truncated, window_hours, limit, digest, source_metadata}.

    Chronological (oldest → newest). Bounded by BOTH a message count and a
    time window. Returns an empty context (never raises) when the feature is
    off, the talent is unknown, or — the project-isolation guarantee — the
    project could not be resolved."""
    limit, hours = history_limit(), history_hours()
    if not is_enabled():
        return _empty("feature_disabled", window_hours=hours, limit=limit)
    if not talent_id:
        return _empty("no_talent", window_hours=hours, limit=limit)
    if not project_id:
        # STEP 6 — project isolation is mandatory. With no reliable project we
        # do NOT fall back to unscoped history.
        return _empty("no_resolved_project", window_hours=hours, limit=limit)

    try:
        cutoff = _now() - timedelta(hours=hours)
        inbound = await _inbound_messages(project_id, talent_id, cutoff, current_inbound_id)
        outbound = await _outbound_messages(project_id, talent_id, cutoff)
    except Exception:
        logger.exception("conversation_context: retrieval failed (non-fatal)")
        return _empty("retrieval_error", window_hours=hours, limit=limit)

    merged = sorted(inbound + outbound, key=lambda m: m["timestamp"])
    truncated = len(merged) > limit
    merged = merged[-limit:]

    redact = bool((project or {}).get("hide_budget_from_talent"))
    rows: List[Dict[str, Any]] = []
    for m in merged:
        txt = m["message_text"]
        if redact:
            txt = _redact_amounts(txt)
        rows.append({
            "direction": m["direction"],
            "timestamp": m["timestamp"].isoformat(),
            "message_text": txt,
            "source": m["source"],
        })

    digest = hashlib.sha256(
        "␟".join(f'{r["direction"]}␞{r["message_text"]}' for r in rows).encode("utf-8")
    ).hexdigest()

    return {
        "recent_messages": rows,
        "truncated": truncated,
        "window_hours": hours,
        "limit": limit,
        "digest": digest,
        "source_metadata": {
            "reason": "ok",
            "inbound_count": sum(1 for r in rows if r["direction"] == "in"),
            "outbound_count": sum(1 for r in rows if r["direction"] == "out"),
            "budget_redacted": redact,
        },
    }


def render_for_prompt(cc: Optional[dict]) -> List[Dict[str, str]]:
    """The minimal shape the LLM sees — direction + text only, NO ids, NO
    phone numbers, NO internal metadata."""
    rows = (cc or {}).get("recent_messages") or []
    out: List[Dict[str, str]] = []
    for r in rows:
        who = "talent" if r["direction"] == "in" else "talentgram"
        out.append({"from": who, "text": r["message_text"]})
    return out
