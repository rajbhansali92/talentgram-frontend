"""Background MarkIntent retry sweep (2026-09-27, Phase 2B forensic audit fix).

Root cause this closes: `agents/modules/mark_intent.py`'s own module
docstring has always described retries as "dispatched on ordinary
UPLOAD/SEND retries and the background retry pass (once built)" — direct
production evidence (a live forensic audit, read-only Mongo query against
the real cluster) proved that background pass never existed. 18 intents
sat `unresolved`/`resolving`, several 6+ days old, several frozen at
`attempt_count=0` since the moment they were created — resolution
depended entirely on a human happening to retry a WhatsApp SEND/UPLOAD
command whose live scan happened to re-surface the exact stuck reply,
which the SAME evidence showed isn't even reliable when a human DOES
retry (WhatsApp Web's virtualized message list; see mark_intent.py's own
module docstring for the proven Nikki Sharma non-determinism this whole
layer exists to fix).

This module is the missing background pass. It does NOT scan every
WhatsApp group indiscriminately and does NOT reintroduce continuous
global monitoring — each sweep cycle claims exactly ONE currently-
eligible unresolved/resolving MarkIntent (oldest-swept-first, rate-
limited via `last_swept_at`/`MARK_INTENT_SWEEP_MIN_INTERVAL_SEC` — see
`mark_intent.claim_next_sweepable_intent`) and dispatches a scan
targeted at ONLY that intent's own already-known `(talent_id, project_id,
source_chat_name)` — the exact chat the original MARK reply was observed
in, never a broader search. The scan itself reuses the existing,
unmodified worker-claim/scan/report pipeline
(`agents.modules.media_assignment.create_scan_request` -> the worker's
own claim loop -> `routers/agents_whatsapp.py:report_scan_result` ->
`services/media_assignment_worker.py:_process_scan_done`'s own `sweep`
branch, added alongside this module) — no new WhatsApp DOM automation
code exists anywhere in this module; it only ever creates ordinary
`whatsapp_scan_requests` documents the existing infrastructure already
knows how to process.

Structural template: `services/media_assignment_worker.py`'s own
`_send_action_verification_loop` (claim-one-due-item -> do bounded work ->
sleep, restart-safe, no leader-election needed since the atomic
`find_one_and_update` claim is what makes concurrent execution safe, not
there being only one process) — the third sibling loop in that same
family, kept in its own module rather than added to that already very
large file.

Worker/`WORKER_ID` compatibility: every dispatched scan carries the
ORIGINAL worker_id the intent was first observed under
(`created_by_worker_id`, stamped once at MarkIntent creation and never
reassigned), so a sweep always re-scans through the SAME authenticated
WhatsApp session that originally saw the reply, never an arbitrary or
default one.

Feature-flagged OFF by default (`MARK_INTENT_SWEEP_ENABLED`), matching
this codebase's established pattern for shipping a new automated
WhatsApp-triggering capability dormant until explicitly enabled — this
sweep creates real scan_requests that a real worker claims and acts on
(a real, bounded WhatsApp Web scan), so it is deliberately not on by
default even though the loop itself is always started (checking the flag
is the very first thing each cycle does, before touching the database at
all, so "flag off" costs nothing beyond one flag read per cycle).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from core import db

logger = logging.getLogger(__name__)

MARK_INTENT_SWEEP_ENABLED = os.environ.get("MARK_INTENT_SWEEP_ENABLED", "false").lower() in ("1", "true", "yes")

# Deliberately slow (not "bounded, rate-limited" in the same sub-second
# sense as media_assignment_worker.py's own scan-completion loops, which
# only ever poll Mongo — this loop's own work, when it finds something,
# triggers a REAL bounded WhatsApp Web scan via the existing worker
# pipeline, so the cadence at which NEW scans get dispatched must itself
# stay gentle. One claim attempt per cycle; MARK_INTENT_SWEEP_MIN_INTERVAL_SEC
# (see mark_intent.py) is the real "don't hammer WhatsApp" bound — this
# just governs how promptly a newly-eligible intent gets picked up.
POLL_SEC = float(os.environ.get("MARK_INTENT_SWEEP_POLL_SEC", "60"))

_sweep_task: Optional[asyncio.Task] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_sweep_cycle() -> bool:
    """One claim-and-dispatch attempt. Exposed (not just internal to the
    loop) so tests can call it directly/repeatedly without waiting on
    POLL_SEC. Returns True if an intent was claimed and a scan dispatched
    (or the claimed intent had to be skipped for a data-integrity reason
    — either way, real work happened this cycle), False if nothing was
    currently eligible."""
    if not MARK_INTENT_SWEEP_ENABLED:
        return False

    from agents.modules import mark_intent, media_assignment

    intent = await mark_intent.claim_next_sweepable_intent()
    if not intent:
        return False

    talent_id = intent.get("talent_id")
    project_id = intent.get("project_id")
    project_label = intent.get("project_label") or ""
    group_name = intent.get("source_chat_name") or ""
    worker_id = intent.get("created_by_worker_id") or "default"

    if not (talent_id and project_id and group_name):
        # Data-integrity guard, not an expected path — every intent this
        # module's own claim query can select was created by
        # get_or_create_mark_intent, which always sets these fields. Skip
        # rather than crash the loop; last_swept_at was already stamped
        # by the claim itself, so this won't be picked again until the
        # normal rate-limit window passes.
        logger.warning(
            "MARK_INTENT_SWEEP_SKIPPED_INCOMPLETE mark_intent_id=%s talent_id=%s project_id=%s group_name=%r",
            intent.get("id"), talent_id, project_id, group_name,
        )
        return True

    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0, "name": 1})
    talent_label = (talent or {}).get("name") or "talent"

    req_id = await media_assignment.create_scan_request(
        talent_id=talent_id, talent_label=talent_label,
        project_id=project_id, project_label=project_label,
        group_name=group_name, worker_id=worker_id,
    )
    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": req_id},
        {"$set": {"sweep": True, "sweep_target_mark_intent_id": intent["id"]}},
    )
    logger.info(
        "MARK_INTENT_SWEEP_DISPATCHED mark_intent_id=%s scan_request_id=%s talent_id=%s project_id=%s "
        "group_name=%r worker_id=%s attempt_count=%s",
        intent["id"], req_id, talent_id, project_id, group_name, worker_id, intent.get("attempt_count"),
    )
    return True


async def _sweep_loop() -> None:
    logger.info("mark_intent_sweep: starting background MarkIntent retry sweep (enabled=%s)...", MARK_INTENT_SWEEP_ENABLED)
    while True:
        try:
            did_work = await run_sweep_cycle()
        except Exception:
            logger.exception("mark_intent_sweep: unexpected error in sweep cycle")
            did_work = False
        await asyncio.sleep(POLL_SEC if not did_work else 1.0)


def start_mark_intent_sweep() -> None:
    global _sweep_task
    if _sweep_task and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop())
