"""Approve + Upload / Approve + Send — Submission Review actions (2026-09-20).

Dispatches the EXISTING WhatsApp mark-based UPLOAD/SEND pipelines
(media_assignment.create_scan_request / media_send.
create_send_dispatch_from_approved_plan / casting_pipeline._preview_send_marks)
directly from a Submission Review button click, instead of a WhatsApp
UPLOAD/SEND chat command. This is deliberately NOT a parallel workflow:
both actions insert the SAME scan_requests document shapes the WhatsApp-
triggered flows already produce, so services/media_assignment_worker.py's
existing background loop (claim, download, upload/send, completion
report, auto-approve-on-success) handles every downstream step unchanged.
See media_assignment.create_scan_request's submission_id/approve_on_success
params and media_send.create_send_dispatch_from_approved_plan's own
submission_id/content_hash pending_report_context wiring — both already
reused by the WhatsApp-command paths; this module is the second caller.

Scope note: both actions require the submission to already be linked to a
talent record (sub["talent_id"] already resolved, e.g. by a prior ordinary
Approve or the initial submission-intake flow) — neither duplicates
routers.submissions.set_decision's own talent-creation fallback. A
submission with no linked talent yet must be approved normally first.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import db
from agents import registry
from agents.modules import media_assignment, media_send
from agents.modules.casting_pipeline import _preview_send_marks, _send_approval_overrides


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
# a real race _find_in_flight_request alone cannot: dispatch_approve_send's
# own pre-dispatch marked-media scan (_preview_send_marks) can legitimately
# run for several seconds (up to the production SEND_PREVIEW_MAX_WAIT_SEC
# bound) BEFORE any scan_requests doc exists at all, so two requests
# arriving within that window (a rapid double-click, two browser tabs, or
# an HTTP-level retry) would both see "nothing in flight" and both go on
# to dispatch an independent SEND — a genuine duplicate casting-group
# message, not just a duplicate scan. Keyed on Mongo's own `_id` (always
# uniquely enforced, no extra index/migration needed): the SECOND insert
# for the same (project, submission, action) raises a duplicate-key error
# and loses the race atomically, unlike a find-then-insert check. Always
# released in the caller's `finally`, success or failure, so a genuine
# retry after a real failure is never blocked — only genuinely concurrent
# dispatches are.
ACTION_LOCKS_COLLECTION = "submission_whatsapp_action_locks"

# Stale-lock safety net: this lock is only ever held for the duration of
# ONE dispatch call (resolution + the bounded preview scan, at most tens
# of seconds) — the real WhatsApp work happens later, asynchronously,
# AFTER the lock is released. A lock older than this can only mean the
# backend process died mid-dispatch (never a legitimately slow dispatch),
# so it's treated as abandoned and cleared rather than blocking every
# retry forever.
LOCK_STALE_AFTER_S = 120


def _lock_id(project_id: str, submission_id: str, action: str) -> str:
    return f"{project_id}:{submission_id}:{action}"


async def _acquire_dispatch_lock(project_id: str, submission_id: str, action: str) -> bool:
    lock_id = _lock_id(project_id, submission_id, action)
    try:
        await db[ACTION_LOCKS_COLLECTION].insert_one({"_id": lock_id, "created_at": _now()})
        return True
    except Exception:  # DuplicateKeyError — another dispatch may hold it
        pass
    # Someone holds it — but if it's stale (abandoned by a crashed
    # process), reclaim it atomically: only a delete that actually
    # matched the STALE doc may proceed, so a genuinely-live lock
    # (refreshed/replaced by its real owner) is never stolen out from
    # under it.
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


async def _find_in_flight_request(project_id: str, submission_id: str, mode: str) -> Optional[str]:
    """Idempotency guard against double-clicking (Production requirement,
    2026-09-20) — "PENDING/PROCESSING dedupe": a scan_requests doc for
    this exact (project, submission) pair that hasn't reached the
    terminal STATUS_FINISHED yet means an identical Approve + Upload/Send
    is already in flight, so a second click reattaches to that SAME
    request instead of dispatching a genuinely new one (which could
    otherwise download/send/upload the same media twice). `mode` scopes
    the check to "scan"/"download" (UPLOAD's own mode sequence) or
    "send" (SEND's), since an in-flight UPLOAD should never block a
    separate SEND for the same submission, and vice versa."""
    modes = ["scan", "download"] if mode == "upload" else ["send"]
    doc = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find_one(
        {
            "project_id": project_id, "submission_id": submission_id,
            "mode": {"$in": modes},
            "status": {"$ne": media_assignment.STATUS_FINISHED},
        },
        {"_id": 0, "id": 1},
        sort=[("created_at", -1)],
    )
    return doc["id"] if doc else None


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


async def dispatch_approve_upload(project_id: str, submission_id: str, *, worker_id: Optional[str] = None) -> str:
    """Identify submission -> talent -> project -> marked WhatsApp media,
    then dispatch the EXISTING scan/download/upload pipeline
    (media_assignment.create_scan_request), with approve_on_success=True
    so services/media_assignment_worker.py auto-approves this exact
    submission once every download this run genuinely succeeds (mirrors
    SEND's own existing auto-approve hook). Returns the scan_requests id
    for status polling. Raises SubmissionActionError on any pre-dispatch
    validation failure — nothing is dispatched in that case.

    `worker_id` (worker-affinity pre-deployment review, 2026-09-20): the
    caller (routers/submissions.py's HTTP endpoint) never passes one — an
    explicit override exists only for tests/future callers. A missing
    worker_id resolves to PRIMARY_WORKER_ID (the one authoritative,
    centrally-documented policy constant this module defines) BEFORE the
    request ever reaches create_scan_request, so a NEW job's worker_id is
    always a concrete, explicitly-assigned identity by the time it's
    persisted — the None/absent-means-legacy-Worker-1 compatibility rule
    is claim-time-only and belongs to pre-existing documents, never to a
    job being created right now."""
    worker_id = worker_id or PRIMARY_WORKER_ID
    in_flight = await _find_in_flight_request(project_id, submission_id, "upload")
    if in_flight:
        return in_flight

    if not await _acquire_dispatch_lock(project_id, submission_id, "upload"):
        # Someone else is dispatching RIGHT NOW — their scan_requests doc
        # should appear within moments; check once more before telling the
        # caller to retry, rather than silently no-op'ing.
        in_flight = await _find_in_flight_request(project_id, submission_id, "upload")
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

        return await media_assignment.create_scan_request(
            talent_id=sub["talent_id"], talent_label=talent_label,
            project_id=project_id, project_label=project_label,
            group_name=group_name, worker_id=worker_id,
            submission_id=submission_id, approve_on_success=True,
        )
    finally:
        await _release_dispatch_lock(project_id, submission_id, "upload")


async def dispatch_approve_send(
    project_id: str, submission_id: str, *, worker_id: Optional[str] = None, approved_by: str = "admin",
) -> str:
    """Resolve the project's configured WhatsApp casting group, identify
    the exact marked media already resolved for this talent/project (the
    SAME bounded preview scan the WhatsApp SEND confirmation card uses —
    casting_pipeline._preview_send_marks), build the EXISTING SEND form
    message, then dispatch DIRECTLY from that already-resolved plan
    (media_send.create_send_dispatch_from_approved_plan) — never a native
    WhatsApp Forward, never a second live re-scan at execution time.
    Carries submission_id through so the worker's existing SEND
    completion branch auto-approves this exact submission only once
    every media item + form + the ☑️ completion marker have genuinely
    succeeded, then sends the existing talent acknowledgement. Returns
    the scan_requests id for status polling. Raises SubmissionActionError
    on any pre-dispatch validation failure — nothing is sent in that
    case.

    `worker_id` (worker-affinity pre-deployment review, 2026-09-20): same
    resolution rule as dispatch_approve_upload's own — see its docstring.
    The real caller (routers/submissions.py) never passes one; it always
    resolves to PRIMARY_WORKER_ID before create_send_dispatch_from_approved_plan
    is ever called, so the persisted document's worker_id is never None."""
    worker_id = worker_id or PRIMARY_WORKER_ID
    in_flight = await _find_in_flight_request(project_id, submission_id, "send")
    if in_flight:
        return in_flight

    # Acquired BEFORE the preview scan below (not just before the eventual
    # scan_requests insert) — that scan can legitimately run for several
    # seconds with no scan_requests doc in existence yet, which is exactly
    # the window _find_in_flight_request alone cannot cover. See the lock's
    # own module-level docstring for the full race this closes.
    if not await _acquire_dispatch_lock(project_id, submission_id, "send"):
        in_flight = await _find_in_flight_request(project_id, submission_id, "send")
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

        talent_id = sub["talent_id"]
        sources: List[Tuple[str, str]] = [("group", group_name)]
        assignments, error = await _preview_send_marks(
            talent_id=talent_id, talent_label=talent_label,
            project_id=project_id, project_label=project_label,
            destination_group=destination_group, sources=sources,
        )
        if assignments is None:
            raise SubmissionActionError(
                "marked_media_unresolved",
                error or "Couldn't verify marked media for this talent/project right now — try again shortly.",
            )
        if not assignments:
            raise SubmissionActionError(
                "no_marked_media",
                f"No marked WhatsApp media found for {talent_label} / {project_label} yet — "
                "mark the audition takes/introduction on WhatsApp first.",
            )

        existing = await media_send.get_send_approval(talent_id, project_id, destination_group)
        overrides = _send_approval_overrides(existing)
        form_built = media_send.build_form_send_message(sub, project, talent_label, project_label, overrides)

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

        return await media_send.create_send_dispatch_from_approved_plan(
            talent_id=talent_id, project_id=project_id, talent_label=talent_label, project_label=project_label,
            destination_group=destination_group, assignments=assignments,
            default_source_type="group", default_group_name=group_name,
            form_message=form_message, submission_id=submission_id, content_hash=form_built["content_hash"],
            created_by=approved_by, worker_id=worker_id,
        )
    finally:
        await _release_dispatch_lock(project_id, submission_id, "send")


async def get_action_status(request_id: str, project_id: str, submission_id: str) -> Dict[str, Any]:
    """Frontend-friendly status for polling a dispatched request. `done`
    is True only once the worker has fully finished (success or failure)
    — mirrors STATUS_FINISHED, the SAME terminal status _finish() always
    writes for both UPLOAD and SEND, success or failure alike (failures
    are reported in `report`, never a separate status; the scan_requests
    doc itself carries no separate structured success flag).

    `ok` is deliberately NOT inferred by sniffing `report`'s text (fragile
    — the exact wording is free-form worker-report prose, not a contract).
    It's also deliberately NOT the submission's own `decision` field alone
    — that could already read "approved" from a completely unrelated
    earlier action, which would false-positive a run that actually failed
    this time. Instead `ok` reads `operation_ok`, a plain field services/
    media_assignment_worker.py sets on THIS EXACT scan_request doc only
    inside the same success-gated block that calls routers.submissions.
    set_decision (zero failed items, and for SEND also a genuinely-
    successful ☑️ marker) — see that file's SEND and UPLOAD completion
    branches.

    `decision` (Production-safety fix, 2026-09-20) is a SEPARATE, freshly
    read field: the real WhatsApp media operation succeeding
    (operation_ok=True) and the submission's decision actually having
    flipped to "approved" are two different writes — set_decision is
    called in a try/except that deliberately never rolls the media back
    on failure (see media_assignment_worker.py's own comment), so a rare
    set_decision failure would otherwise leave operation_ok=True while
    the submission is still "pending". The caller must show the REAL
    decision it reads here, never assume "ok implies approved"."""
    doc = await media_assignment.get_scan_request(request_id)
    if not doc or doc.get("project_id") != project_id or doc.get("submission_id") != submission_id:
        return {"found": False, "done": True, "status": "not_found", "report": None, "ok": False, "decision": None}
    status = doc.get("status")
    done = status == media_assignment.STATUS_FINISHED
    report = doc.get("report")
    ok: Optional[bool] = bool(doc.get("operation_ok")) if done else None
    decision = None
    if done:
        sub = await db.submissions.find_one({"id": submission_id, "project_id": project_id}, {"_id": 0, "decision": 1})
        decision = sub.get("decision") if sub else None
    return {"found": True, "done": done, "status": status, "report": report, "ok": ok, "decision": decision}
