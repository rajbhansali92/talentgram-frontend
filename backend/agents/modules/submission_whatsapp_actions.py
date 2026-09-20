"""Approve + Upload / Approve + Send — Submission Review actions
(2026-09-20, redesigned 2026-09-21 to fix stale-media risk).

Dispatches the EXISTING WhatsApp mark-based UPLOAD/SEND pipelines
(media_assignment.create_scan_request / media_send.
create_send_dispatch_from_approved_plan) directly from a Submission
Review button click, instead of a WhatsApp UPLOAD/SEND chat command.
This is deliberately NOT a parallel workflow: both actions insert the
SAME scan_requests document shapes the WhatsApp-triggered flows already
produce, so services/media_assignment_worker.py's existing background
loop (claim, download, upload/send, completion report, auto-approve-on-
success) handles every downstream step unchanged.

2026-09-21 redesign: dispatch_approve_send no longer blocks synchronously
on a single bounded live scan inside the HTTP handler (the root cause of
the "Couldn't verify marked media" incident — a slow/cold WhatsApp Web
scan threw away the ENTIRE attempt on timeout, with no durable record
ever created). It now creates a durable agents.modules.
submission_action_queue action IMMEDIATELY and returns — a dedicated
background loop (services/media_assignment_worker.py's
_send_action_verification_loop) performs the real, repeated, live scans,
explicitly associating this ONE action with the mark_intent_ids its own
freshest scan actually observed (never a historical talent/project-wide
read — see submission_action_queue.py's own module docstring for the
full architecture). dispatch_approve_upload is unaffected in its own
dispatch mechanics (it never blocked on a preview) but is now ALSO
wrapped in the same durable action record for queue visibility.

Scope note: both actions require the submission to already be linked to a
talent record (sub["talent_id"] already resolved, e.g. by a prior ordinary
Approve or the initial submission-intake flow) — neither duplicates
routers.submissions.set_decision's own talent-creation fallback. A
submission with no linked talent yet must be approved normally first.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from core import db
from agents import registry
from agents.modules import media_assignment
from agents.modules import submission_action_queue as queue


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The single authoritative source for which worker a NEW Submission Review
# Center dispatch (Approve + Upload / Approve + Send) targets when the
# caller (routers/submissions.py's HTTP endpoints — see their own
# docstrings) doesn't pass one explicitly, which today is every real
# caller (2026-09-20 worker-affinity pre-deployment review). Deliberately
# a named, centrally-documented, env-overridable constant rather than the
# literal "default" duplicated independently inside dispatch_approve_upload/
# dispatch_approve_send's own signatures — one source both read, so there
# is exactly one place to change if the routing policy ever changes,
# instead of two call sites that could silently drift apart.
#
# Defaults to registry.DEFAULT_WORKER_ID ("default", i.e. Worker 1) because
# Worker 2 is CONFIRMED (Worker 2 investigation, 2026-09-20) still running
# stale, pre-download-reattach code with a live legacy native-Forward SEND
# path — it must not receive any NEW Approve + Upload/Send job until it is
# upgraded to the same commit as Worker 1 (see the deployment-sequencing
# section of that review). This is a deliberate, explicit policy choice,
# never inferred from the browser/frontend (the HTTP endpoints above take
# no worker_id from the client at all) and never a bare hard-coded literal.
PRIMARY_WORKER_ID = os.environ.get("PRIMARY_WHATSAPP_WORKER_ID", registry.DEFAULT_WORKER_ID)


# A short-lived dispatch lock (Production-safety fix, 2026-09-20) — closes
# a race the queue's own find_in_flight_action alone cannot: two requests
# arriving within the same instant (a rapid double-click, two browser
# tabs, or an HTTP-level retry), both seeing "nothing in flight yet"
# before either has finished creating its own action document, would
# otherwise both create one. Keyed on Mongo's own `_id` (always uniquely
# enforced, no extra index/migration needed): the SECOND insert for the
# same (project, submission, action) raises a duplicate-key error and
# loses the race atomically, unlike a find-then-insert check. Always
# released in the caller's `finally`, success or failure, so a genuine
# retry after a real failure is never blocked — only genuinely concurrent
# dispatches are.
ACTION_LOCKS_COLLECTION = "submission_whatsapp_action_locks"

# Stale-lock safety net: this lock is only ever held for the duration of
# ONE dispatch call (fast DB-only resolution + action-document creation,
# never the WhatsApp scan itself anymore) — a lock older than this can
# only mean the backend process died mid-dispatch, so it's treated as
# abandoned and cleared rather than blocking every retry forever.
LOCK_STALE_AFTER_S = 30


def _lock_id(project_id: str, submission_id: str, action: str) -> str:
    return f"{project_id}:{submission_id}:{action}"


async def _acquire_dispatch_lock(project_id: str, submission_id: str, action: str) -> bool:
    lock_id = _lock_id(project_id, submission_id, action)
    try:
        await db[ACTION_LOCKS_COLLECTION].insert_one({"_id": lock_id, "created_at": _now()})
        return True
    except Exception:  # DuplicateKeyError — another dispatch may hold it
        pass
    stale_cutoff = _now() - timedelta(seconds=LOCK_STALE_AFTER_S)
    deleted = await db[ACTION_LOCKS_COLLECTION].delete_one({"_id": lock_id, "created_at": {"$lt": stale_cutoff}})
    if deleted.deleted_count == 0:
        return False
    try:
        await db[ACTION_LOCKS_COLLECTION].insert_one({"_id": lock_id, "created_at": _now()})
        return True
    except Exception:
        return False


async def _release_dispatch_lock(project_id: str, submission_id: str, action: str) -> None:
    await db[ACTION_LOCKS_COLLECTION].delete_one({"_id": _lock_id(project_id, submission_id, action)})


class SubmissionActionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


async def _resolve_common(project_id: str, submission_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], str, str]:
    """Returns (submission, project, talent_label, source_whatsapp_group_name)."""
    sub = await db.submissions.find_one({"id": submission_id, "project_id": project_id}, {"_id": 0})
    if not sub:
        raise SubmissionActionError("submission_not_found", "Submission not found.")
    talent_id = sub.get("talent_id")
    if not talent_id:
        raise SubmissionActionError(
            "no_linked_talent",
            "This submission isn't linked to a talent record yet — approve it normally first, then retry.",
        )
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not project:
        raise SubmissionActionError("project_not_found", "Project not found.")
    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0})
    if not talent:
        raise SubmissionActionError("talent_not_found", "Linked talent record not found.")
    talent_label = talent.get("name") or sub.get("talent_name") or "Talent"
    group_name = (talent.get("whatsapp_group_name") or "").strip()
    if not group_name:
        raise SubmissionActionError(
            "no_whatsapp_group",
            f"{talent_label} has no WhatsApp group configured — the mark-based workflow requires one.",
        )
    return sub, project, talent_label, group_name


async def dispatch_approve_upload(project_id: str, submission_id: str, *, worker_id: Optional[str] = None) -> Dict[str, Any]:
    """Identify submission -> talent -> project -> marked WhatsApp media,
    then dispatch the EXISTING scan/download/upload pipeline
    (media_assignment.create_scan_request), with approve_on_success=True
    so services/media_assignment_worker.py auto-approves this exact
    submission once every download this run genuinely succeeds. Wrapped
    in a durable submission_action_queue action for queue visibility —
    the underlying dispatch mechanics are completely unchanged (this
    never blocked on a preview scan; it dispatches immediately and the
    worker's own existing scan+download lifecycle tracks it durably).
    Returns the action document. Raises SubmissionActionError on any
    pre-dispatch validation failure — nothing is dispatched in that case.

    `worker_id` (worker-affinity pre-deployment review, 2026-09-20): the
    caller (routers/submissions.py's HTTP endpoint) never passes one — an
    explicit override exists only for tests/future callers. A missing
    worker_id resolves to PRIMARY_WORKER_ID BEFORE the request ever
    reaches create_scan_request, so a NEW job's worker_id is always a
    concrete, explicitly-assigned identity by the time it's persisted."""
    worker_id = worker_id or PRIMARY_WORKER_ID
    in_flight = await queue.find_in_flight_action(project_id, submission_id, queue.ACTION_TYPE_UPLOAD)
    if in_flight:
        return in_flight

    if not await _acquire_dispatch_lock(project_id, submission_id, "upload"):
        in_flight = await queue.find_in_flight_action(project_id, submission_id, queue.ACTION_TYPE_UPLOAD)
        if in_flight:
            return in_flight
        raise SubmissionActionError(
            "action_in_progress", "This action is already being started — please wait a moment and retry.",
        )
    try:
        sub, project, talent_label, group_name = await _resolve_common(project_id, submission_id)
        project_label = project.get("brand_name") or "(untitled project)"

        identity = await media_assignment.get_gunwanti_identity()
        if not identity or not identity.get("lid"):
            raise SubmissionActionError(
                "identity_not_configured",
                "The WhatsApp agent identity is not configured yet — contact an admin.",
            )

        action = await queue.create_action(
            action_type=queue.ACTION_TYPE_UPLOAD, project_id=project_id, talent_id=sub["talent_id"],
            talent_label=talent_label, project_label=project_label, submission_id=submission_id,
            worker_id=worker_id, created_by="admin", source_group_name=group_name,
        )
        req_id = await media_assignment.create_scan_request(
            talent_id=sub["talent_id"], talent_label=talent_label,
            project_id=project_id, project_label=project_label,
            group_name=group_name, worker_id=worker_id,
            submission_id=submission_id, approve_on_success=True,
        )
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": req_id}, {"$set": {"action_id": action["id"]}},
        )
        # UPLOAD's own worker-side scan+download lifecycle is the SAME
        # unchanged mechanism whether or not this queue exists — treat
        # dispatch as immediately "media resolution handed off"; the
        # fine-grained DOWNLOADING/UPLOADING states are then derived
        # read-only from the linked document (see derive_display_state).
        await db[queue.ACTIONS_COLLECTION].update_one(
            {"id": action["id"]},
            {"$set": {"state": queue.STATE_MEDIA_RESOLVED, "updated_at": _now()}},
        )
        await queue.mark_dispatched(action["id"], req_id)
        return await queue.get_action(action["id"])
    finally:
        await _release_dispatch_lock(project_id, submission_id, "upload")


async def dispatch_approve_send(
    project_id: str, submission_id: str, *, worker_id: Optional[str] = None, approved_by: str = "admin",
) -> Dict[str, Any]:
    """Resolve the project's configured WhatsApp casting group, then
    create a durable submission_action_queue action and return
    IMMEDIATELY — no live WhatsApp scan happens inline in this call
    anymore. The background verification loop (services/
    media_assignment_worker.py's _send_action_verification_loop) performs
    the real scans and, once THIS action's own freshest scan is fully
    resolved, dispatches DIRECTLY from that already-resolved plan
    (media_send.create_send_dispatch_from_approved_plan, via
    submission_action_queue._dispatch_send) — never a native WhatsApp
    Forward, never a guess. Returns the action document (state QUEUED)
    for status polling / the Action Queue panel. Raises
    SubmissionActionError on any pre-dispatch (fast, DB-only)
    validation failure — nothing is queued in that case.

    `worker_id`: same resolution rule as dispatch_approve_upload's own —
    see its docstring."""
    worker_id = worker_id or PRIMARY_WORKER_ID
    in_flight = await queue.find_in_flight_action(project_id, submission_id, queue.ACTION_TYPE_SEND)
    if in_flight:
        return in_flight

    if not await _acquire_dispatch_lock(project_id, submission_id, "send"):
        in_flight = await queue.find_in_flight_action(project_id, submission_id, queue.ACTION_TYPE_SEND)
        if in_flight:
            return in_flight
        raise SubmissionActionError(
            "action_in_progress", "This action is already being started — please wait a moment and retry.",
        )
    try:
        sub, project, talent_label, group_name = await _resolve_common(project_id, submission_id)
        project_label = project.get("brand_name") or "(untitled project)"
        destination_group = (project.get("whatsapp_casting_group_name") or "").strip()
        if not destination_group:
            raise SubmissionActionError(
                "no_casting_group",
                f"{project_label} has no WhatsApp casting group configured yet — add one in Edit Project first.",
            )

        action = await queue.create_action(
            action_type=queue.ACTION_TYPE_SEND, project_id=project_id, talent_id=sub["talent_id"],
            talent_label=talent_label, project_label=project_label, submission_id=submission_id,
            worker_id=worker_id, created_by=approved_by,
            destination_group=destination_group, source_group_name=group_name,
        )
        return action
    finally:
        await _release_dispatch_lock(project_id, submission_id, "send")


async def retry_action(action_id: str, project_id: str, submission_id: str) -> Dict[str, Any]:
    """Explicit admin Retry (Phase 8 #14) — only valid for a FAILED,
    retryable action belonging to this exact (project, submission).
    For SEND, resets the action back to QUEUED; the background
    verification loop picks it up on its own next cycle. For UPLOAD,
    resets it AND immediately re-dispatches create_scan_request again
    (mirroring the original dispatch — UPLOAD has no background
    verification loop of its own to pick it back up)."""
    action = await queue.get_action(action_id)
    if not action or action["project_id"] != project_id or action["submission_id"] != submission_id:
        raise SubmissionActionError("action_not_found", "Action not found.")
    action = await queue.retry_action(action_id, created_by="admin")

    if action["action_type"] == queue.ACTION_TYPE_UPLOAD:
        sub, project, talent_label, group_name = await _resolve_common(project_id, submission_id)
        project_label = project.get("brand_name") or "(untitled project)"
        req_id = await media_assignment.create_scan_request(
            talent_id=sub["talent_id"], talent_label=talent_label,
            project_id=project_id, project_label=project_label,
            group_name=group_name, worker_id=action["worker_id"],
            submission_id=submission_id, approve_on_success=True,
        )
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": req_id}, {"$set": {"action_id": action_id}},
        )
        await db[queue.ACTIONS_COLLECTION].update_one(
            {"id": action_id}, {"$set": {"state": queue.STATE_MEDIA_RESOLVED, "updated_at": _now()}},
        )
        await queue.mark_dispatched(action_id, req_id)
        action = await queue.get_action(action_id)

    return action


async def list_queue(*, limit: int = 100) -> Dict[str, Any]:
    """The Action Queue panel's own feed — every non-terminal action plus
    recently-terminal ones, independent of any single Submission Review
    page (Phase 5's own explicit requirement)."""
    actions = await queue.list_actions(limit=limit)
    return {"actions": actions}


async def get_action_status(action_id: str, project_id: str, submission_id: str) -> Dict[str, Any]:
    """Frontend-friendly status for polling a dispatched action. `done`
    is True only once the action has reached a terminal state (COMPLETED
    or FAILED). `ok` is exactly `state == COMPLETED` — the action's own
    state IS the authoritative success/failure signal now (previously
    derived by sniffing a scan_requests document's `operation_ok` field;
    that derivation still happens, just one layer down, inside
    submission_action_queue.sync_from_finished_request, which is the
    ONLY thing allowed to move an action to COMPLETED).

    `decision` (Production-safety fix, 2026-09-20, preserved unchanged)
    is a SEPARATE, freshly read field: the real WhatsApp media operation
    succeeding and the submission's decision actually having flipped to
    "approved" are two different writes — the caller must show the REAL
    decision read here, never assume "ok implies approved"."""
    action = await queue.get_action(action_id)
    if not action or action.get("project_id") != project_id or action.get("submission_id") != submission_id:
        return {"found": False, "done": True, "status": "not_found", "report": None, "ok": False, "decision": None}
    state = action.get("state")
    done = state in (queue.STATE_COMPLETED, queue.STATE_FAILED)
    decision = None
    if done:
        sub = await db.submissions.find_one({"id": submission_id, "project_id": project_id}, {"_id": 0, "decision": 1})
        decision = sub.get("decision") if sub else None
    return {
        "found": True, "done": done, "status": state, "display_state": await queue.derive_display_state(action),
        "report": action.get("report"), "ok": (state == queue.STATE_COMPLETED) if done else None,
        "decision": decision, "error_code": action.get("error_code"), "error_message": action.get("error_message"),
        "retryable": action.get("retryable", False), "action": action,
    }
