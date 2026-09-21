"""Submission Action Queue — durable Approve + Upload / Approve + Send
actions (2026-09-21 architecture fix).

Root cause this replaces: `dispatch_approve_send` used to block
SYNCHRONOUSLY inside the HTTP request handler on a single, bounded
(~20-22s) live WhatsApp scan (casting_pipeline._preview_send_marks). If
that one scan timed out, EVERYTHING was thrown away — no durable record
of the attempt ever existed, the admin saw a bare error, and a click
seconds later started completely from scratch. A same-day attempted fix
(`mark_intent.get_ready_assignments(talent_id, project_id)` — a
"trust whatever's already resolved for this talent/project" read) was
proven unsafe by direct test: a resolved-but-SUPERSEDED MarkIntent for a
slot a fresh, not-yet-observed re-MARK is about to replace is
indistinguishable, from a bare (talent_id, project_id) read, from a
genuinely still-current one — nothing observes a WhatsApp reply until
some scan's raw candidates actually include it, so a historical read has
no way to know "is this still the latest mark for this slot". That
function has been removed entirely.

The correct fix, per the accepted architecture review: identity for "is
this SEND ready" belongs to the ACTION, never to (talent_id,
project_id) alone. This module is that action:

  - created durably (this collection) the INSTANT Approve + Upload/Send
    is clicked — before any scan, so a timed-out preview is never
    indistinguishable from "nothing happened";
  - for SEND, verified by a dedicated background loop
    (services/media_assignment_worker.py's new
    _send_action_verification_loop) that performs REAL, repeated,
    unbounded-in-total (bounded per-attempt, exactly as before) live
    scans via casting_pipeline._scan_and_validate_multi_source —
    completely UNCHANGED code, just called from a new location;
  - EVERY retry's mark_intent_ids come ONLY from THAT retry's own fresh
    scan output (outcome.assignments[*]["mark_intent_id"], populated by
    the existing, unchanged mark_intent.enrich_outcome_with_mark_intents)
    — never from a database-wide historical read. A re-MARK the CURRENT
    retry's own scan did not observe simply cannot appear in this
    action's mark_intent_ids, at any point;
  - once every required slot's mark_intent_id is RESOLVED (per that
    SAME fresh scan's own outcome.ok), the action hands off to the
    EXISTING, UNCHANGED media_send.create_send_dispatch_from_approved_plan
    / whatsapp_scan_requests / worker / Attach->Photos&videos->Send
    pipeline — nothing about actual media transport is touched here.

For UPLOAD, media_assignment.create_scan_request already dispatches
immediately (never blocks on a preview) and already tracks its own full
scan->download lifecycle durably in one whatsapp_scan_requests document
— this module wraps it in the SAME queue-visible Action record for UI
consistency, without changing its own mechanics at all.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core import db

logger = logging.getLogger("submission_action_queue")

ACTIONS_COLLECTION = "submission_actions"

ACTION_TYPE_UPLOAD = "upload"
ACTION_TYPE_SEND = "send"

# Coarse states this module writes directly and controls the meaning of.
# DOWNLOADING/UPLOADING/SENDING/VERIFYING_RESULT (the fine-grained,
# in-progress execution states Phase 5 asked for) are deliberately NOT
# written here at all — they are DERIVED, read-only, from the linked
# whatsapp_scan_requests document's own EXISTING fields at read time (see
# derive_display_state below). This is a deliberate risk-reduction
# choice: the actual UPLOAD/SEND execution phase tracking inside
# services/media_assignment_worker.py and whatsapp-worker/mark_scan.py is
# proven, production code that Phase 7 explicitly says not to touch; a
# read-only projection needs zero new write points scattered through
# that code, so it cannot regress it.
STATE_QUEUED = "QUEUED"
STATE_VERIFYING = "VERIFYING"
STATE_MEDIA_RESOLVED = "MEDIA_RESOLVED"
STATE_DOWNLOADING = "DOWNLOADING"
STATE_UPLOADING = "UPLOADING"
STATE_SENDING = "SENDING"
STATE_VERIFYING_RESULT = "VERIFYING_RESULT"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"

# Only these terminal/near-terminal states are ever written by THIS
# module directly; DOWNLOADING/UPLOADING/SENDING/VERIFYING_RESULT are
# derived (see above) and never appear in a raw stored document's own
# `state` field except transiently in test fixtures.
_WRITTEN_STATES = {
    STATE_QUEUED, STATE_VERIFYING, STATE_MEDIA_RESOLVED, STATE_COMPLETED, STATE_FAILED,
}

# Generous verification ceiling (mirrors mark_intent.py's own
# DEFAULT_MAX_RESOLUTION_ATTEMPTS reasoning: "do NOT hard-code a tiny
# retry window"). Each attempt is one full, real, bounded live scan
# (~20-22s when it has to wait out a genuine timeout) — 15 attempts is
# comfortably enough real-world retry room for a slow/cold WhatsApp Web
# scan to eventually warm up and succeed, while still being a genuine,
# finite ceiling a stuck action can hit and surface to the admin with a
# Retry button, never silently spinning forever.
MAX_SEND_VERIFY_ATTEMPTS = int(os.environ.get("SEND_ACTION_MAX_VERIFY_ATTEMPTS", "15"))
# Spacing between verification attempts for one action (on top of
# whatever a single attempt's own scan wait already took) — gives
# WhatsApp Web's own state a moment to settle between live scans rather
# than hammering it back-to-back.
SEND_VERIFY_RETRY_BACKOFF_SEC = float(os.environ.get("SEND_ACTION_RETRY_BACKOFF_SEC", "5"))
# Window for mark_intent.get_freshly_resolved_if_complete's "did a scan
# just resolve this, moments ago, via the late-tombstone path" check
# (2026-09-21 live-production fix). Must comfortably exceed a single
# attempt's own scan budget (casting_pipeline._SEND_PREVIEW_TOTAL_MAX_
# WAIT_SEC, ~22s) plus a margin for the late worker result to actually
# land and get written — the proven live case was a real result landing
# ~26s after dispatch against a 22s scan budget. Deliberately NOT tied
# to MAX_SEND_VERIFY_ATTEMPTS' cumulative window: this only ever looks
# at THIS one just-completed attempt's own timing, never reaching back
# into an earlier attempt's stale resolution.
SEND_LATE_RESOLUTION_WINDOW_SEC = float(os.environ.get("SEND_ACTION_LATE_RESOLUTION_WINDOW_SEC", "35"))
# Per-attempt live-scan wait budget for THIS background verification loop
# specifically (root-cause fix, 2026-09-21 — real production incident:
# Juhi Vyas / Dettol Film 1 SEND, 15/15 attempts a pure timeout with no
# MarkIntent/project-matching problem whatsoever). casting_pipeline._
# scan_and_validate_multi_source's own default budget (~20-22s total) is
# sized for the SYNCHRONOUS /inbound preview-card handler, which must stay
# well under the worker's ~35s dispatch-claim ceiling — see that
# function's own "ONE shared budget across ALL sources" comment. This
# background loop has no such HTTP-request-lifetime constraint (it's
# already an async poll loop scheduled via next_attempt_at), so it can
# safely wait long enough for the worker's REAL, unchanged scan/jump-
# fallback resolution work to actually finish and report back, instead of
# the backend giving up first every time. Real worker logs for the Juhi
# incident showed a genuine, correct chat-open + 2-candidate jump-fallback
# round trip taking ~39s end to end — comfortably under this budget, with
# margin for a third marked item or a slower render.
SEND_VERIFY_SCAN_BUDGET_SEC = float(os.environ.get("SEND_ACTION_VERIFY_SCAN_BUDGET_SEC", "55"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes() -> None:
    await db[ACTIONS_COLLECTION].create_index("id", unique=True, name="uniq_action_id")
    await db[ACTIONS_COLLECTION].create_index(
        [("state", 1), ("next_attempt_at", 1)], name="verify_due_idx",
    )
    await db[ACTIONS_COLLECTION].create_index(
        [("project_id", 1), ("submission_id", 1), ("action_type", 1)], name="dedupe_idx",
    )
    await db[ACTIONS_COLLECTION].create_index([("created_at", -1)], name="recent_idx")


async def find_in_flight_action(project_id: str, submission_id: str, action_type: str) -> Optional[Dict[str, Any]]:
    """Duplicate-job protection (Phase 8 #18) — an action for this exact
    (project, submission, type) that hasn't reached a terminal state yet
    means one is already in flight; a second click reattaches to it
    instead of creating a competing one."""
    return await db[ACTIONS_COLLECTION].find_one(
        {
            "project_id": project_id, "submission_id": submission_id, "action_type": action_type,
            "state": {"$nin": [STATE_COMPLETED, STATE_FAILED]},
        },
        {"_id": 0}, sort=[("created_at", -1)],
    )


async def create_action(
    *, action_type: str, project_id: str, talent_id: str, talent_label: str,
    project_label: str, submission_id: str, worker_id: str, created_by: str,
    destination_group: Optional[str] = None, source_group_name: Optional[str] = None,
) -> Dict[str, Any]:
    now = _now()
    doc = {
        "id": str(uuid.uuid4()),
        "action_type": action_type,
        "project_id": project_id, "talent_id": talent_id, "talent_label": talent_label,
        "project_label": project_label, "submission_id": submission_id,
        "destination_group": destination_group, "source_group_name": source_group_name,
        "worker_id": worker_id,
        "state": STATE_QUEUED,
        # The core fix — populated ONLY from this action's own fresh scan
        # output, never from a talent/project-wide historical read.
        "mark_intent_ids": [],
        "scan_request_id": None,       # in-flight VERIFICATION-phase preview scan, if any
        "dispatch_scan_request_id": None,  # the real EXECUTION-phase scan_requests doc, once dispatched
        "attempt_count": 0,
        "next_attempt_at": now,
        "error_code": None, "error_message": None, "retryable": False,
        "report": None,
        "created_at": now, "updated_at": now, "created_by": created_by,
    }
    await db[ACTIONS_COLLECTION].insert_one(doc)
    logger.info(
        "SUBMISSION_ACTION_CREATED action_id=%s type=%s talent_id=%s project_id=%s submission_id=%s",
        doc["id"], action_type, talent_id, project_id, submission_id,
    )
    doc.pop("_id", None)
    return doc


async def get_action(action_id: str) -> Optional[Dict[str, Any]]:
    return await db[ACTIONS_COLLECTION].find_one({"id": action_id}, {"_id": 0})


async def list_actions(*, limit: int = 100, include_terminal_minutes: int = 30) -> List[Dict[str, Any]]:
    """Queue-panel feed: every non-terminal action, plus terminal
    (COMPLETED/FAILED) ones from the last `include_terminal_minutes` —
    so a just-finished job stays visible for a bit instead of vanishing
    the instant it completes, without the list growing unbounded."""
    cutoff = _now() - timedelta(minutes=include_terminal_minutes)
    cur = db[ACTIONS_COLLECTION].find(
        {
            "$or": [
                {"state": {"$nin": [STATE_COMPLETED, STATE_FAILED]}},
                {"state": {"$in": [STATE_COMPLETED, STATE_FAILED]}, "updated_at": {"$gte": cutoff}},
            ],
        },
        {"_id": 0},
    ).sort("created_at", -1).limit(limit)
    docs = await cur.to_list(limit)
    for d in docs:
        d["display_state"] = await derive_display_state(d)
    return docs


async def derive_display_state(action: Dict[str, Any]) -> str:
    """Read-only projection of the fine-grained DOWNLOADING/UPLOADING/
    SENDING/VERIFYING_RESULT states from the linked
    whatsapp_scan_requests document's own EXISTING fields — see this
    module's own docstring for why this is derived rather than written
    from new hook points scattered through the proven worker-orchestrator
    code. Falls back to the action's own stored (coarse) state whenever
    there's no dispatch document yet, or its fields don't map cleanly."""
    state = action.get("state")
    if state in (STATE_COMPLETED, STATE_FAILED, STATE_QUEUED, STATE_VERIFYING, STATE_MEDIA_RESOLVED):
        # QUEUED/VERIFYING/MEDIA_RESOLVED/COMPLETED/FAILED are always
        # authoritative as stored — only the gap between "dispatched" and
        # "finished" benefits from a closer read.
        req_id = action.get("dispatch_scan_request_id")
        if state != STATE_MEDIA_RESOLVED or not req_id:
            return state
    req_id = action.get("dispatch_scan_request_id")
    if not req_id:
        return state
    from agents.modules import media_assignment as ma
    doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one(
        {"id": req_id}, {"_id": 0, "mode": 1, "status": 1, "download_results": 1, "ack_result": 1, "send_claimed_at": 1, "claimed_at": 1},
    )
    if not doc:
        return state
    mode = doc.get("mode")
    status = doc.get("status")
    if status == "finished":
        return state  # terminal sync happens via _finish's own hook, not here
    if mode == "download":
        if doc.get("claimed_at") and not doc.get("download_results"):
            return STATE_DOWNLOADING
        if doc.get("download_results"):
            return STATE_UPLOADING
        return STATE_DOWNLOADING
    if mode == "send":
        if doc.get("send_claimed_at") and doc.get("ack_result") is None:
            return STATE_SENDING
        if doc.get("ack_result") is not None:
            return STATE_VERIFYING_RESULT
        return STATE_DOWNLOADING
    return state


async def mark_dispatched(action_id: str, dispatch_scan_request_id: str) -> None:
    """The action's media is resolved and the EXISTING, unchanged
    execution pipeline (media_assignment.create_scan_request /
    media_send.create_send_dispatch_from_approved_plan) has been handed
    the exact locked assignments — from here on, services/
    media_assignment_worker.py's own existing state machine drives it to
    completion exactly as it always has for every non-queue caller too."""
    await db[ACTIONS_COLLECTION].update_one(
        {"id": action_id},
        {"$set": {"dispatch_scan_request_id": dispatch_scan_request_id, "updated_at": _now()}},
    )


async def sync_from_finished_request(request_doc: Dict[str, Any]) -> None:
    """Called from services/media_assignment_worker.py's single shared
    `_finish()` completion point (both UPLOAD and SEND, success or
    failure) — additive: does nothing unless the finished scan_requests
    document carries an `action_id` (only documents this queue itself
    dispatched ever do; every other existing caller — WhatsApp chat
    commands, bulk send, etc. — is completely unaffected)."""
    action_id = request_doc.get("action_id")
    if not action_id:
        return
    ok = bool(request_doc.get("operation_ok"))
    report = request_doc.get("report")
    # Production fix, 2026-09-21 (Akarsh Kumar Gowda / Snapdragon Computer)
    # — a send-but-unverified item no longer blocks COMPLETED (see
    # mark_scan.py's all_media_ok fix), but that nuance must not become
    # invisible just because the action now shows green. This flag lets
    # the frontend still surface "delivery unconfirmed for N item(s)" on
    # an otherwise-successful action, instead of silently dropping it.
    has_unverified = any(
        (r or {}).get("send_state") == "MESSAGE_SENT_BUT_NOT_VERIFIED"
        for r in (request_doc.get("download_results") or [])
    )
    if ok:
        await db[ACTIONS_COLLECTION].update_one(
            {"id": action_id},
            {"$set": {
                "state": STATE_COMPLETED, "report": report, "error_code": None,
                "error_message": None, "retryable": False,
                "has_unverified_media": has_unverified, "updated_at": _now(),
            }},
        )
        logger.info("SUBMISSION_ACTION_COMPLETED action_id=%s has_unverified_media=%s", action_id, has_unverified)
    else:
        await db[ACTIONS_COLLECTION].update_one(
            {"id": action_id},
            {"$set": {
                "state": STATE_FAILED, "report": report,
                "error_code": "execution_failed",
                "error_message": report or "The WhatsApp operation did not complete successfully.",
                "retryable": True, "updated_at": _now(),
            }},
        )
        logger.warning("SUBMISSION_ACTION_FAILED action_id=%s report=%r", action_id, report)


async def fail_action(action_id: str, *, code: str, message: str, retryable: bool) -> None:
    await db[ACTIONS_COLLECTION].update_one(
        {"id": action_id},
        {"$set": {
            "state": STATE_FAILED, "error_code": code, "error_message": message,
            "retryable": retryable, "updated_at": _now(),
        }},
    )
    logger.warning("SUBMISSION_ACTION_FAILED action_id=%s code=%s message=%r retryable=%s", action_id, code, message, retryable)


async def claim_next_send_action_due() -> Optional[Dict[str, Any]]:
    """Atomic, ordered dequeue (mirrors services/media_assignment_
    worker.py's own find_one_and_update-with-sort pattern for
    scan_requests) — one action at a time, oldest-due first, so N queued
    SEND actions round-robin through the single live-scan resource (one
    worker = one WhatsApp Web browser session = one scan at a time,
    regardless of how many actions are waiting) rather than one action
    hogging every attempt back-to-back. The immediate small
    next_attempt_at push is a light optimistic claim — cheap insurance
    against ever running more than one backend replica; harmless when
    there is only one, which is production's current configuration."""
    now = _now()
    return await db[ACTIONS_COLLECTION].find_one_and_update(
        {
            "action_type": ACTION_TYPE_SEND,
            "state": {"$in": [STATE_QUEUED, STATE_VERIFYING]},
            "next_attempt_at": {"$lte": now},
        },
        {"$set": {"next_attempt_at": now + timedelta(seconds=2), "updated_at": now}},
        sort=[("next_attempt_at", 1)],
        return_document=True,
        projection={"_id": 0},
    )


async def advance_send_action(action: Dict[str, Any]) -> bool:
    """One verification step for a SEND action — exactly one real, bounded
    live scan attempt (casting_pipeline._scan_and_validate_multi_source,
    completely unchanged), then either: still nothing conclusive (stay
    VERIFYING, schedule the next attempt), a genuine failure (batch/
    ambiguous/permanently-failed-media/attempt-ceiling), or full
    resolution (MEDIA_RESOLVED, mark_intent_ids populated from THIS
    scan's own output only, then hand off to the existing dispatch
    pipeline). Returns True (this function only runs when the caller has
    already confirmed the action is due)."""
    from agents.modules import casting_pipeline, mark_intent

    action_id = action["id"]
    if action["state"] == STATE_QUEUED:
        await db[ACTIONS_COLLECTION].update_one({"id": action_id}, {"$set": {"state": STATE_VERIFYING, "updated_at": _now()}})

    sources = [("group", action["source_group_name"])]
    outcome, error = await casting_pipeline._scan_and_validate_multi_source(
        talent_id=action["talent_id"], talent_label=action["talent_label"],
        project_id=action["project_id"], project_label=action["project_label"],
        destination_group=action["destination_group"], sources=sources,
        total_budget_s=SEND_VERIFY_SCAN_BUDGET_SEC,
    )
    attempt_count = action.get("attempt_count", 0) + 1

    async def _retry(mark_intent_ids: Optional[List[str]] = None, last_error: Optional[str] = None) -> None:
        update: Dict[str, Any] = {
            "attempt_count": attempt_count,
            "next_attempt_at": _now() + timedelta(seconds=SEND_VERIFY_RETRY_BACKOFF_SEC),
            "updated_at": _now(),
        }
        if mark_intent_ids is not None:
            update["mark_intent_ids"] = mark_intent_ids
        if last_error is not None:
            update["error_message"] = last_error
        await db[ACTIONS_COLLECTION].update_one({"id": action_id}, {"$set": update})

    if outcome is None:
        if error:
            if attempt_count >= MAX_SEND_VERIFY_ATTEMPTS:
                await fail_action(action_id, code="scan_error", message=error, retryable=True)
            else:
                await _retry(last_error=error)
            return True
        # Pure timeout (error is None) — before treating this as "still
        # nothing conclusive", check whether the late-worker/tombstone
        # path (report_scan_result's own 404 branch, unchanged) already
        # resolved this action's marks moments ago on a code path this
        # attempt's own _scan_and_validate_multi_source call never sees.
        # See get_freshly_resolved_if_complete's own docstring for why
        # this is safe and not a reincarnation of the removed
        # get_ready_assignments shortcut.
        late_assignments = await mark_intent.get_freshly_resolved_if_complete(
            action["talent_id"], action["project_id"],
            within_seconds=SEND_LATE_RESOLUTION_WINDOW_SEC,
        )
        if late_assignments:
            logger.info(
                "SUBMISSION_ACTION_LATE_RESOLUTION_CAUGHT action_id=%s mark_intent_ids=%s",
                action_id, [a["mark_intent_id"] for a in late_assignments],
            )
            await _resolve_and_dispatch(action_id, late_assignments, attempt_count)
            return True
        if attempt_count >= MAX_SEND_VERIFY_ATTEMPTS:
            await fail_action(
                action_id, code="verification_timeout",
                message=(
                    f"Could not verify marked media for {action['talent_label']} / {action['project_label']} "
                    f"after {attempt_count} attempts — WhatsApp Web may be slow right now. Retry when ready."
                ),
                retryable=True,
            )
        else:
            await _retry()
        return True

    if outcome.batch_failures:
        names = "; ".join((b.get("mark_text") or "").strip() for b in outcome.batch_failures)
        await fail_action(
            action_id, code="batch_unresolved",
            message=(
                f"Some marked media couldn't be resolved to exact WhatsApp source items: {names}. "
                "Re-check the mark and album, then retry."
            ),
            retryable=False,
        )
        return True
    if outcome.ambiguous:
        role, take = outcome.ambiguous.get("media_role"), outcome.ambiguous.get("take_number")
        slot = f"Take {take}" if role == "take" else (role or "media").capitalize()
        await fail_action(
            action_id, code="ambiguous_mark",
            message=(
                f"{action['project_label']} {slot} has been marked twice, pointing to two "
                "different source items — please resolve the duplicate mark before sending."
            ),
            retryable=False,
        )
        return True
    if outcome.unresolved:
        mark_intent_ids = [a["mark_intent_id"] for a in outcome.assignments if a.get("mark_intent_id")]
        reply_ids = [u.get("reply_message_id") for u in outcome.unresolved if u.get("reply_message_id")]
        permanently_failed = False
        if reply_ids:
            permanently_failed = await db[mark_intent.MARK_INTENTS_COLLECTION].count_documents(
                {"reply_message_id": {"$in": reply_ids}, "status": mark_intent.STATUS_FAILED_PERMANENTLY},
            ) > 0
        if permanently_failed:
            await fail_action(
                action_id, code="media_not_located",
                message=(
                    "Some marked media could not be located after repeated attempts — please "
                    "re-send the MARK reply on the original media, then retry."
                ),
                retryable=True,
            )
        elif attempt_count >= MAX_SEND_VERIFY_ATTEMPTS:
            await fail_action(
                action_id, code="verification_timeout",
                message=(
                    f"Still verifying some marked media for {action['talent_label']} / {action['project_label']} "
                    f"after {attempt_count} attempts. Retry when ready."
                ),
                retryable=True,
            )
        else:
            await _retry(mark_intent_ids=mark_intent_ids)
        return True

    # outcome.ok — fully resolved, by THIS scan's own fresh output only.
    if not outcome.assignments:
        # Real production incident, 2026-09-21 (Kushagre Dua / Snapdragon
        # Computer (Male Start up founder) — the WhatsApp group shares its
        # casting group with THREE other "Snapdragon Computer (...)"
        # sibling projects). The marks genuinely existed and were scanned,
        # but media_assignment.validate_candidates correctly and safely
        # excluded them as project_ambiguous/project_mismatch (a real tie
        # among several sibling projects, or a confident match to a
        # DIFFERENT one — see that function's own "SAFETY RULES" for why
        # this must never be guessed, proven necessary by the real Limca
        # Film1/Film2 and Tapti AI App (Ananya)/(Neelam) cross-
        # contamination incidents). The correct, safe fix is NOT to loosen
        # that matching — it's to stop telling the admin "nothing was
        # marked" when something WAS marked and deliberately, correctly
        # rejected. Surfacing the true reason (and exactly how to fix the
        # mark text) is the fix: the admin re-marks with a specific enough
        # project reference, which then resolves uniquely and safely.
        if outcome.project_ambiguous:
            all_candidate_labels = sorted({
                p["label"] for item in outcome.project_ambiguous for p in (item.get("ambiguous_projects") or [])
            })
            await fail_action(
                action_id, code="marked_media_project_ambiguous",
                message=(
                    f"Marked WhatsApp media WAS found for {action['talent_label']}, but its project "
                    f"reference could not be safely narrowed to {action['project_label']} — it matches "
                    f"{len(all_candidate_labels)} similarly-named projects equally well: "
                    f"{'; '.join(all_candidate_labels)}. Please re-send the MARK reply on WhatsApp with a "
                    f"more specific project name (e.g. include the part in parentheses, like "
                    f"\"{action['project_label']}\"), then retry."
                ),
                retryable=True,
            )
            return True
        if outcome.project_mismatch:
            other_labels = sorted({item.get("matched_project_label") for item in outcome.project_mismatch if item.get("matched_project_label")})
            await fail_action(
                action_id, code="marked_media_wrong_project",
                message=(
                    f"Marked WhatsApp media WAS found for {action['talent_label']}, but it was marked for "
                    f"a different project ({'; '.join(other_labels)}), not {action['project_label']} — it was "
                    "correctly not assigned here. Mark the audition takes/introduction for "
                    f"{action['project_label']} specifically, then retry."
                ),
                retryable=True,
            )
            return True
        await fail_action(
            action_id, code="no_marked_media",
            message=(
                f"No marked WhatsApp media found for {action['talent_label']} / {action['project_label']} "
                "yet — mark the audition takes/introduction on WhatsApp first."
            ),
            retryable=True,
        )
        return True

    await _resolve_and_dispatch(action_id, outcome.assignments, attempt_count)
    return True


async def _resolve_and_dispatch(action_id: str, assignments: List[Dict[str, Any]], attempt_count: int) -> None:
    """Shared tail for both ways an action can become ready: its own
    scan directly succeeding, or (see get_freshly_resolved_if_complete's
    own docstring) a late tombstone-recovered report resolving things a
    few seconds after this same attempt's own scan call returned a pure
    timeout. Either way, mark_intent_ids come only from already-locked
    MarkIntent documents this action's own scan activity produced —
    never a historical read spanning outside this action's own attempts."""
    mark_intent_ids = [a["mark_intent_id"] for a in assignments if a.get("mark_intent_id")]
    await db[ACTIONS_COLLECTION].update_one(
        {"id": action_id},
        {"$set": {
            "state": STATE_MEDIA_RESOLVED, "mark_intent_ids": mark_intent_ids,
            "attempt_count": attempt_count, "updated_at": _now(),
        }},
    )
    logger.info("SUBMISSION_ACTION_MEDIA_RESOLVED action_id=%s mark_intent_ids=%s", action_id, mark_intent_ids)

    action = await get_action(action_id)
    try:
        await _dispatch_send(action, assignments)
    except Exception as exc:
        # Robustness gap closed (2026-09-21 review): without this, an
        # unexpected failure partway through _dispatch_send (e.g. a
        # transient DB/network error calling media_send's own functions)
        # would leave the action stuck in MEDIA_RESOLVED forever — its
        # dispatch_scan_request_id never gets set, and
        # claim_next_send_action_due only ever looks at QUEUED/VERIFYING,
        # so nothing would ever retry it again. The admin would see a
        # permanently frozen "Media resolved…" row with no error and no
        # Retry button. Converting to a normal, visible, retryable
        # failure here means retry_action's own reset-to-QUEUED path
        # (which re-enters advance_send_action from the top, including a
        # fresh live scan) is what recovers it — never a silent freeze.
        logger.exception("SUBMISSION_ACTION_DISPATCH_FAILED action_id=%s", action_id)
        await fail_action(
            action_id, code="dispatch_failed",
            message=f"Media was resolved but dispatching the send failed unexpectedly: {exc}",
            retryable=True,
        )


async def _dispatch_send(action: Dict[str, Any], assignments: List[Dict[str, Any]]) -> None:
    """Hands the already-resolved, write-once-locked assignments to the
    EXISTING, UNCHANGED media_send.create_send_dispatch_from_approved_plan
    -> whatsapp_scan_requests -> worker claim -> Attach/Photos&videos/Send
    pipeline. Nothing about actual media transport is touched here — this
    is the SAME call dispatch_approve_send made inline before this fix,
    just relocated to run once the background verification loop has
    confirmed readiness instead of inline in the HTTP handler."""
    from agents.modules import media_assignment, media_send
    from agents.modules.casting_pipeline import _send_approval_overrides

    action_id = action["id"]
    talent_id, project_id = action["talent_id"], action["project_id"]
    destination_group = action["destination_group"]
    submission_id = action["submission_id"]
    approved_by = action["created_by"]

    sub = await db.submissions.find_one({"id": submission_id, "project_id": project_id}, {"_id": 0})
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not sub or not project:
        await fail_action(
            action_id, code="submission_or_project_missing",
            message="The submission or project could not be re-read at dispatch time.", retryable=True,
        )
        return

    existing = await media_send.get_send_approval(talent_id, project_id, destination_group)
    overrides = _send_approval_overrides(existing)
    form_built = media_send.build_form_send_message(sub, project, action["talent_label"], action["project_label"], overrides)

    await media_send.save_send_approval_draft(
        talent_id=talent_id, project_id=project_id, destination_group=destination_group,
        submission_id=submission_id, overrides=overrides,
        message=form_built["message"], content_hash=form_built["content_hash"],
    )
    await media_send.approve_send_form(talent_id, project_id, destination_group, approved_by=approved_by)

    form_message: Optional[str] = None
    already_form = await media_send.already_sent_form(talent_id, project_id, destination_group, form_built["content_hash"])
    if not already_form:
        await media_send.record_form_send(
            talent_id=talent_id, project_id=project_id, destination_group=destination_group,
            submission_id=submission_id, content_hash=form_built["content_hash"], created_by=approved_by,
        )
        form_message = form_built["message"]

    req_id = await media_send.create_send_dispatch_from_approved_plan(
        talent_id=talent_id, project_id=project_id, talent_label=action["talent_label"], project_label=action["project_label"],
        destination_group=destination_group, assignments=assignments,
        default_source_type="group", default_group_name=action["source_group_name"],
        form_message=form_message, submission_id=submission_id, content_hash=form_built["content_hash"],
        created_by=approved_by, worker_id=action["worker_id"],
    )
    # Additive tag — no existing reader of whatsapp_scan_requests is
    # affected by this field's presence; only this queue's own
    # sync_from_finished_request (called from _finish's completion hook)
    # ever looks for it.
    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one({"id": req_id}, {"$set": {"action_id": action_id}})
    await mark_dispatched(action_id, req_id)


async def retry_action(action_id: str, *, created_by: str) -> Dict[str, Any]:
    """Explicit admin-triggered retry of a FAILED action — resets it back
    to QUEUED/attempt_count=0 rather than creating a brand-new action
    document, so the queue shows one row's history, not a proliferating
    chain. Only valid from a terminal FAILED, retryable state."""
    action = await get_action(action_id)
    if not action:
        raise ValueError(f"submission action {action_id} not found")
    if action["state"] != STATE_FAILED or not action.get("retryable"):
        raise ValueError(f"submission action {action_id} is not in a retryable FAILED state (state={action['state']!r})")
    now = _now()
    await db[ACTIONS_COLLECTION].update_one(
        {"id": action_id},
        {"$set": {
            "state": STATE_QUEUED, "attempt_count": 0, "next_attempt_at": now,
            "mark_intent_ids": [], "scan_request_id": None, "dispatch_scan_request_id": None,
            "error_code": None, "error_message": None, "retryable": False,
            "report": None, "updated_at": now,
        }},
    )
    logger.info("SUBMISSION_ACTION_RETRY action_id=%s retried_by=%s", action_id, created_by)
    return await get_action(action_id)
