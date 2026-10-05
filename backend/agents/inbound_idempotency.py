"""Backend-side execution boundary for one inbound WhatsApp `message_id`.

Production incident (2026-09-29/30): the WhatsApp worker's own claim
(whatsapp-worker/inbound.py, `whatsapp_inbound_seen`) already protects a
message from being *claimed* twice, but carried no protection at the
backend if the worker process crashed AFTER the backend had already fully
executed a command (Fletcher SHOW ME, a casting ADD/MOVE/SEND) and
returned a reply — the worker's own "mark this claim completed" write
never landed, so its 180s stale-claim recovery later re-claimed the SAME
message_id and re-dispatched it to the backend, which had no way to know
this wasn't a genuinely new message. The backend re-ran the command from
scratch and returned a fresh reply, which the worker then sent as a
second, unsolicited outbound WhatsApp message. Confirmed directly against
production: a `whatsapp_agent_audit_log` row proving "Show me Fanny
Gandhi's form for Carter" executed successfully at 13:26:21 on 2026-09-29,
correlated with its `whatsapp_inbound_seen` claim still sitting
"in_progress" 15+ hours later — the backend had no record it had already
done this work.

This module closes that gap AT THE BACKEND, independent of whatever the
worker's own claim state says: `claim_or_get_result` is called once, right
before a command actually executes, keyed by the exact same `message_id`
the worker already sends in every /inbound POST. It reuses the worker's
own durable `whatsapp_inbound_seen` collection (same shared Mongo
database) rather than introduce a new one — writing only backend_-prefixed
fields onto the SAME message_id-keyed document, so it can never collide
with or be read by the worker's own status/claimed_at/worker_id fields.

The invariant this gives the caller: for one message_id, the wrapped
command body runs at most once. A crash strictly between the command
finishing and persist_result() landing (a backend-process crash mid this
one Mongo write, not a worker crash) is the one residual gap this cannot
close without two-phase-commit machinery this codebase doesn't have —
documented, not hidden. It is a much narrower window (this process's own
single next await) than the incident this closes (an unbounded window
across an entire worker restart), and is NOT what the production incident
traced to.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from pymongo.errors import DuplicateKeyError

from core import db

logger = logging.getLogger(__name__)

COLLECTION = "whatsapp_inbound_seen"

# A real Fletcher/casting command normally finishes in well under a
# second; the slowest known synchronous path (SHARE Instagram's live
# WhatsApp recipient search) is bounded at ~20s
# (_RECIPIENT_SEARCH_MAX_WAIT_SEC, see whatsapp-worker/inbound.py's own
# _INBOUND_DISPATCH_TIMEOUT_SEC comment). A claim still "executing" past
# this is not a slow command — it is a backend process that died mid
# command (this endpoint's own await never returned) and is eligible for
# ONE recovery attempt. Deliberately far short of the worker's own 180s
# claim-recovery window: that window exists to tolerate a whole worker
# restart cycle, not a single in-process await.
_STALE_EXECUTING_TIMEOUT_SEC = 90.0

EXECUTE = "execute"
RETURN_STORED = "return_stored"
STILL_EXECUTING = "still_executing"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def claim_or_get_result(message_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Atomic claim-or-return-stored-result for one message_id.

    Returns (EXECUTE, None) — this call is the one that must run the
    command now, then call persist_result() with the outcome.

    Returns (RETURN_STORED, result) — the command already ran to
    completion for this message_id (by this call or an earlier one); the
    caller MUST NOT run it again and should return `result` as-is.

    Returns (STILL_EXECUTING, None) — a different, not-yet-stale attempt
    is genuinely in flight right now (a rare true concurrent-dispatch
    race, not the crash-recovery case). The caller should return a
    no-op/no-reply response; the worker's own retry (same message_id)
    will converge once that other attempt finishes.
    """
    now = _now()
    try:
        res = await db[COLLECTION].update_one(
            {"message_id": message_id, "backend_execution_status": {"$exists": False}},
            {
                "$set": {"backend_execution_status": "executing", "backend_dispatch_started_at": now},
                "$setOnInsert": {"message_id": message_id, "created_at": now},
            },
            upsert=True,
        )
        won = res.modified_count == 1 or res.upserted_id is not None
    except DuplicateKeyError:
        won = False
    except Exception:
        # Infra hiccup on the claim attempt itself — fail closed like the
        # worker's own _claim_message does: do NOT execute, so a flaky
        # Mongo write can never silently produce a duplicate execution.
        # The worker will simply retry this message_id later.
        logger.exception("inbound_idempotency: claim attempt failed for %s (treating as not-won)", message_id)
        return STILL_EXECUTING, None

    if won:
        logger.info("inbound_idempotency: EXECUTION_CLAIMED message_id=%r", message_id)
        return EXECUTE, None

    try:
        existing = await db[COLLECTION].find_one({"message_id": message_id})
    except Exception:
        logger.exception("inbound_idempotency: post-claim lookup failed for %s", message_id)
        return STILL_EXECUTING, None

    status = (existing or {}).get("backend_execution_status")
    if status == "executed":
        stored = (existing or {}).get("backend_result") or {"handled": False, "reply": None, "operation_id": None}
        logger.info("inbound_idempotency: EXECUTION_ALREADY_DONE message_id=%r — returning stored result, not re-running", message_id)
        return RETURN_STORED, stored

    if status == "executing":
        started_raw = (existing or {}).get("backend_dispatch_started_at")
        started = _as_aware_utc(started_raw)
        stale_for = (now - started).total_seconds() if started else None
        if started is not None and stale_for is not None and stale_for > _STALE_EXECUTING_TIMEOUT_SEC:
            recovered = await db[COLLECTION].find_one_and_update(
                {"message_id": message_id, "backend_dispatch_started_at": started_raw},
                {"$set": {"backend_dispatch_started_at": now}},
            )
            if recovered is not None:
                logger.warning(
                    "inbound_idempotency: EXECUTION_RECOVERY message_id=%r recovered a backend-side "
                    "claim stale for %.0fs (backend process likely died mid-command — a real gap; "
                    "see module docstring)", message_id, stale_for,
                )
                return EXECUTE, None
            # Someone else's recovery attempt won the race first.
            return STILL_EXECUTING, None
        return STILL_EXECUTING, None

    # No document, or a document with no backend_execution_status yet
    # (a genuine race with a concurrent claim attempt that hasn't
    # committed its $set yet) — safest to treat as still-settling rather
    # than risk a second concurrent execution.
    return STILL_EXECUTING, None


async def persist_result(message_id: str, result: Dict[str, Any]) -> None:
    """Marks a message_id's backend execution as durably complete with its
    result, IMMEDIATELY after the command finishes, before the HTTP
    response is sent — the moment after which any later dispatch for the
    same message_id, from a recovered worker claim or anything else, must
    return this SAME result instead of re-running the command. Best-effort
    persistence-wise (never raises into the caller): a write failure here
    is the one residual gap documented in the module docstring."""
    try:
        await db[COLLECTION].update_one(
            {"message_id": message_id},
            {"$set": {"backend_execution_status": "executed", "backend_executed_at": _now(), "backend_result": result}},
        )
    except Exception:
        logger.exception("inbound_idempotency: failed to persist result for %s (residual duplicate-execution risk on next replay)", message_id)
