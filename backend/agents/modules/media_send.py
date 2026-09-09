"""SEND workflow (2026-08-24) — an INDEPENDENT consumer of the same marked
WhatsApp source media the UPLOAD workflow (media_assignment.py) already
resolves. UPLOAD's path is WhatsApp media -> Cloudinary -> submission.
SEND's path is WhatsApp media -> the Casting Pipeline WhatsApp group
directly. Neither depends on the other's output:

    MARKED WHATSAPP MEDIA
            |
      +-----+-----+
      |           |
   UPLOAD       SEND
      |           |
 submission   casting group

SEND deliberately does NOT read submission.media[], media_assignments, or
any "uploaded" status anywhere — its own idempotency lives entirely in
this module's `media_sends` collection, keyed on the source WhatsApp
media's own identity (never on anything UPLOAD produced).

Talent identity is still resolved the SAME way UPLOAD does — this module
imports (never duplicates) media_assignment.resolve_authoritative_talent_for_upload,
media_assignment.validate_candidates, and the source-resolution machinery
in whatsapp-worker/mark_scan.py, which both workflows share unchanged.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import db, _submission_to_client_shape
from agents.modules.media_assignment import (
    DOWNLOAD_STATUS_PENDING,
    MAX_SCAN_MESSAGES,
    SCAN_REQUESTS_COLLECTION,
    SCAN_STATUS_PENDING,
    _now,
    build_send_targets,
    slot_key,
)

MEDIA_SENDS_COLLECTION = "media_sends"
FORM_SENDS_COLLECTION = "form_sends"
COMPLETION_MARKERS_COLLECTION = "send_completion_markers"
SEND_APPROVALS_COLLECTION = "send_form_approvals"

SEND_STATUS_MARKED = "marked"
SEND_STATUS_SENT = "sent"
SEND_STATUS_FAILED = "failed"

# The final "everything for this talent/project has gone out" marker (Phase
# 5/7, 2026-08-26) — sent last, once takes/intro/form/pictures have ALL
# either succeeded this run or were already sent in an earlier run. Its own
# idempotency is a fourth, independent collection (never inferred from
# media_sends/form_sends being "all sent" at read time, which would have to
# be recomputed correctly on every single read) — one row per
# (talent, project, destination_group), written only once.
MARKER_TEXT = "☑️"

# Admin-approval snapshot for the outgoing SEND form (Phase 2/4,
# 2026-08-26). Distinct from FORM_SENDS_COLLECTION, which records SEND
# ATTEMPTS (content_hash + sent/failed) — this collection instead records
# the APPROVAL itself: the admin-edited field overrides and the exact
# rendered message they approved, kept stable across a retry so a resumed
# or re-run "send" never regenerates different wording than what was
# actually approved. Statuses:
#   "pending"   — shown to the admin, awaiting a reply (approve/edit/cancel)
#   "approved"  — admin approved; this exact `message`/content_hash is what
#                 gets sent, including on any later retry of this same
#                 talent/project/destination while still incomplete
#   "completed" — the full SEND operation (media + form + marker) finished
#                 successfully; terminal, audit-only — a later "send" for
#                 this same talent/project starts a fresh approval draft
SEND_APPROVAL_STATUS_PENDING = "pending"
SEND_APPROVAL_STATUS_APPROVED = "approved"
SEND_APPROVAL_STATUS_COMPLETED = "completed"


async def ensure_indexes() -> None:
    """Called once at backend startup alongside media_assignment.ensure_indexes
    (see server.py) — safe to call repeatedly. The unique key mirrors
    media_assignments' own (talent_id, project_id, source_message_id,
    source_thumbnail_hash) exactly — including source_thumbnail_hash so
    two different tiles of the same album never collide — plus
    destination_group, since the SAME source media could in principle be
    sent to more than one destination and each is its own independent
    send."""
    await db[MEDIA_SENDS_COLLECTION].create_index(
        [
            ("talent_id", 1), ("project_id", 1), ("source_message_id", 1),
            ("source_thumbnail_hash", 1), ("destination_group", 1),
        ],
        unique=True, name="uniq_talent_project_source_message_hash_destination",
    )
    # form_sends (2026-08-25) — completely independent of media_sends
    # (never a shared row/flag): a project's approved-submission text
    # message has its own identity keyed on WHICH VERSION of the
    # submission was sent (content_hash), so an edited-then-reapproved
    # submission is eligible to be resent even though the same
    # talent/project/destination already has an earlier successful send.
    await db[FORM_SENDS_COLLECTION].create_index(
        [
            ("talent_id", 1), ("project_id", 1), ("destination_group", 1), ("content_hash", 1),
        ],
        unique=True, name="uniq_talent_project_destination_content_hash",
    )
    # send_completion_markers (2026-08-26) — one row per (talent, project,
    # destination): the ☑️ marker is sent at most once per SEND operation,
    # regardless of how many retries it took to get every item out.
    await db[COMPLETION_MARKERS_COLLECTION].create_index(
        [("talent_id", 1), ("project_id", 1), ("destination_group", 1)],
        unique=True, name="uniq_talent_project_destination_marker",
    )
    # send_form_approvals (2026-08-26) — one row per (talent, project,
    # destination): holds the admin's current draft/approved outgoing SEND
    # form, independent of send attempts.
    await db[SEND_APPROVALS_COLLECTION].create_index(
        [("talent_id", 1), ("project_id", 1), ("destination_group", 1)],
        unique=True, name="uniq_talent_project_destination_approval",
    )


async def create_send_scan_request(
    *, talent_id: str, talent_label: str, project_id: str, project_label: str,
    group_name: str, destination_group: str,
    form_message: Optional[str] = None, submission_id: Optional[str] = None,
    content_hash: Optional[str] = None, source_type: str = "group",
    preview_only: bool = False, skip_validation: bool = False,
    multi_scan_group_id: Optional[str] = None, total_sources: Optional[int] = None,
) -> str:
    """Same shape/lifecycle as media_assignment.create_scan_request — mode
    stays "scan" (the worker's scan logic is 100% shared/unchanged between
    UPLOAD and SEND); `workflow: "send"` is the ONLY marker the backend
    orchestrator (services/media_assignment_worker.py) needs to branch
    into SEND-specific post-scan handling instead of UPLOAD's.

    `form_message` (2026-08-25, None when the form was already sent for
    this exact content_hash) rides along on the SAME request so the
    orchestrator can attach it to the eventual mode="send" worker request
    — sent BEFORE any media forward, per the required ordering.
    `submission_id`/`content_hash` are carried through so the orchestrator
    can mark the form_sends row sent/failed once the worker reports back.

    `source_type` ("group" | "phone", SEND Path B — Production fix,
    2026-09-03): a talent whose only WhatsApp presence is a direct
    number, no group. `group_name` then holds the phone digits instead
    of a group/contact name — same field, reused, never a second schema
    — and the worker (mark_scan.py's _open_source_chat) opens it via the
    proven wa.me deep-link instead of a sidebar-search. Defaults to
    "group" so this is purely additive: every existing caller (this
    function's own default, and every request the worker has ever
    claimed) is completely unaffected.

    `preview_only` (SEND confirmation media preview — Production fix,
    2026-09-03): a READ-ONLY discovery pass casting_pipeline.py's
    _preview_send_marks creates so the confirmation card can show WHICH
    marked media will actually be forwarded, before the admin approves
    anything. See services/media_assignment_worker.py's
    _process_scan_done — a preview_only request finishes directly once
    scanned, never proceeds to mode="download"/"send", and never writes
    to media_assignments/media_sends. Defaults to False; every real send
    (the only other caller of this function) is unaffected.

    `skip_validation` (Production fix — mixed-source SEND, 2026-09-08):
    a talent's marked media can legitimately live in EITHER their
    WhatsApp group OR their individual WhatsApp chat, and a single SEND
    can require BOTH in the same operation (one item marked in each).
    validate_candidates' own ambiguity/dedup logic only works correctly
    across a talent/project's FULL set of marks scanned together — a
    per-source request that ran validate_candidates on its own slice in
    isolation could never detect "this role was marked twice, once in
    each source" (real ambiguity) or correctly dedupe/order a mixed set.
    Used two ways: (a) casting_pipeline._preview_send_marks' own
    synchronous multi-source preview scan (bounded wait, purely
    informational, deleted immediately after reading), and (b) the REAL
    async execution dispatch's per-source scan requests (see
    `multi_scan_group_id` below) — both need the worker's raw,
    unvalidated `candidates` returned rather than a per-source
    validate_candidates call (mirroring the existing scan_probe
    diagnostic short-circuit's shape, but for real production use).
    Defaults to False; every existing caller (single-source preview and
    the real worker-driven post-scan validation in
    media_assignment_worker.py) is unaffected.

    `multi_scan_group_id`/`total_sources` (Production fix — mixed-source
    SEND execution, 2026-09-08): a real SEND approval must stay fast and
    non-blocking (ack immediately, resolve asynchronously — the whole
    reason this worker/orchestrator poll-loop architecture exists at
    all; a synchronous scan-and-wait at approval time was tried and
    reverted — it turns a worker hiccup or a normal multi-second scan
    into a hard approval failure instead of the async design's graceful
    eventual completion). For a talent with MULTIPLE configured sources,
    _send_one_pair creates one scan request PER source, all sharing the
    same `multi_scan_group_id` and each knowing `total_sources` — the
    orchestrator (media_assignment_worker._process_scan_done) waits
    until every sibling has been individually scanned before merging
    their candidates and validating once, exactly mirroring what the
    synchronous preview path does inline, just spread safely across
    multiple async poll-loop passes instead of one blocking wait. None
    for a normal single-source send (every existing caller) — that path
    is completely unaffected, still transitions directly scan->send on
    its own doc exactly as before."""
    req_id = str(uuid.uuid4())
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "scan",
        "workflow": "send",
        "status": SCAN_STATUS_PENDING,
        "group_name": group_name,
        "source_type": source_type,
        "preview_only": preview_only,
        "skip_validation": skip_validation,
        "multi_scan_group_id": multi_scan_group_id,
        "total_sources": total_sources,
        "destination_group": destination_group,
        "talent_id": talent_id,
        "talent_label": talent_label,
        "project_id": project_id,
        "project_label": project_label,
        "max_messages": MAX_SCAN_MESSAGES,
        "candidates": None,
        "send_targets": None,
        "download_results": None,
        "report": None,
        "form_message": form_message,
        "submission_id": submission_id,
        "content_hash": content_hash,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
    })
    return req_id


async def already_sent(talent_id: str, project_id: str, destination_group: str) -> List[Dict[str, Any]]:
    return await db[MEDIA_SENDS_COLLECTION].find(
        {
            "talent_id": talent_id, "project_id": project_id,
            "destination_group": destination_group, "send_status": SEND_STATUS_SENT,
        },
        {"_id": 0},
    ).to_list(200)


async def record_send(
    *, talent_id: str, project_id: str, destination_group: str,
    group_name: str, group_id: Optional[str], mark: Dict[str, Any], created_by: str,
) -> Dict[str, Any]:
    """Upserts a `marked`-status row keyed on the unique index — safe to
    call repeatedly for the same source media without creating
    duplicates. Mirrors media_assignment.record_assignment exactly,
    scoped to this collection instead."""
    doc = {
        "send_id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "project_id": project_id,
        "destination_group": destination_group,
        "source_group_id": group_id,
        "source_group_name": group_name,
        "source_message_id": mark.get("resolved_source_message_id"),
        "source_media_type": mark.get("source_media_type"),
        "source_thumbnail_hash": mark.get("quoted_thumbnail_hash"),
        "source_sender": mark.get("source_sender"),
        "source_timestamp": mark.get("source_timestamp"),
        "album_tile_index": mark.get("album_tile_index"),
        "mark_reply_message_id": mark.get("reply_message_id"),
        "mark_reply_text": mark.get("mark_text"),
        "mark_target_contact_id": mark.get("mention_lid"),
        "media_role": mark.get("media_role"),
        "take_number": mark.get("take_number"),
        "send_status": SEND_STATUS_MARKED,
        "created_at": _now(),
        "created_by": created_by,
    }
    try:
        await db[MEDIA_SENDS_COLLECTION].insert_one(doc)
        return doc
    except Exception:
        existing = await db[MEDIA_SENDS_COLLECTION].find_one(
            {
                "talent_id": talent_id, "project_id": project_id,
                "destination_group": destination_group,
                "source_message_id": doc["source_message_id"],
                "source_thumbnail_hash": doc["source_thumbnail_hash"],
            },
            {"_id": 0},
        )
        return existing or doc


async def mark_send_status(
    talent_id: str, project_id: str, source_message_id: str, source_thumbnail_hash: str,
    destination_group: str, status: str, **extra,
) -> None:
    """source_thumbnail_hash is required, not optional — same reasoning as
    media_assignment.mark_assignment_status: for an album, source_message_id
    alone matches every tile sharing that album.

    A success write always clears any stale `error` from an earlier failed
    attempt on this same row — without this, a retry that later succeeds
    could leave send_status="sent" sitting next to a non-null `error` from
    the earlier failure, which is misleading to anyone reading the record."""
    if status == SEND_STATUS_SENT:
        extra.setdefault("error", None)
    await db[MEDIA_SENDS_COLLECTION].update_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "source_message_id": source_message_id, "source_thumbnail_hash": source_thumbnail_hash,
            "destination_group": destination_group,
        },
        {"$set": {"send_status": status, **extra}},
    )


async def already_sent_form(
    talent_id: str, project_id: str, destination_group: str, content_hash: str,
) -> Optional[Dict[str, Any]]:
    """A form-send is "already done" only for THIS exact content_hash — a
    submission edited (and re-approved) after an earlier send produces a
    different hash and is eligible again, independent of any prior send's
    outcome for the old content."""
    return await db[FORM_SENDS_COLLECTION].find_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "destination_group": destination_group, "content_hash": content_hash,
            "send_status": SEND_STATUS_SENT,
        },
        {"_id": 0},
    )


async def record_form_send(
    *, talent_id: str, project_id: str, destination_group: str,
    submission_id: str, content_hash: str, created_by: str,
) -> Dict[str, Any]:
    """Upserts a `marked`-status row keyed on the unique index — mirrors
    record_send exactly, scoped to FORM_SENDS_COLLECTION. Independent of
    media_sends: media succeeding never marks the form sent, and vice
    versa (see _process_scan_done's SEND branch for the actual ordering)."""
    doc = {
        "form_send_id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "project_id": project_id,
        "destination_group": destination_group,
        "submission_id": submission_id,
        "content_hash": content_hash,
        "send_status": SEND_STATUS_MARKED,
        "created_at": _now(),
        "created_by": created_by,
    }
    try:
        await db[FORM_SENDS_COLLECTION].insert_one(doc)
        return doc
    except Exception:
        existing = await db[FORM_SENDS_COLLECTION].find_one(
            {
                "talent_id": talent_id, "project_id": project_id,
                "destination_group": destination_group, "content_hash": content_hash,
            },
            {"_id": 0},
        )
        return existing or doc


async def mark_form_send_status(
    talent_id: str, project_id: str, destination_group: str, content_hash: str,
    status: str, **extra,
) -> None:
    """Same error-hygiene rule as mark_send_status: a successful write
    always clears any stale `error` left over from an earlier failed
    attempt on this row."""
    if status == SEND_STATUS_SENT:
        extra.setdefault("error", None)
    await db[FORM_SENDS_COLLECTION].update_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "destination_group": destination_group, "content_hash": content_hash,
        },
        {"$set": {"send_status": status, **extra}},
    )


async def already_sent_marker(talent_id: str, project_id: str, destination_group: str) -> bool:
    doc = await db[COMPLETION_MARKERS_COLLECTION].find_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {"_id": 0},
    )
    return doc is not None


async def prepare_send_targets(
    *, talent_id: str, project_id: str, destination_group: str,
    assignments: List[Dict[str, Any]],
    default_source_type: str = "group", default_group_name: Optional[str] = None,
    created_by: str = "whatsapp-agent",
) -> Tuple[List[Dict[str, Any]], int, bool, List[Dict[str, Any]]]:
    """The complete "turn validated assignments into a dispatchable SEND
    request" step — record_send bookkeeping, already-sent filtering,
    build_send_targets' ordering/caption/per-target-source logic, and the
    completion-marker gate — extracted into ONE shared function
    (Production fix — mixed-source SEND, 2026-09-08) so BOTH the async
    worker-orchestrator path (media_assignment_worker._process_scan_done,
    single source) and the synchronous multi-source approval path
    (casting_pipeline._send_one_pair, where assignments may span BOTH a
    talent's group and their individual chat) share identical logic —
    never two competing implementations of this bookkeeping.

    Each assignment's own `source_group_name` (present when it came from
    the multi-source scan) is used for its own record_send row; a
    single-source assignment (no source_group_name of its own) falls
    back to `default_group_name`, preserving the exact original
    single-source behavior unchanged.

    Returns (send_targets, form_insert_index, send_marker_on_success,
    already)."""
    already = await already_sent(talent_id, project_id, destination_group)
    already_slots = {
        slot_key(a["media_role"], a.get("take_number"), a.get("source_message_id"), a.get("source_thumbnail_hash"))
        for a in already
    }
    for m in assignments:
        await record_send(
            talent_id=talent_id, project_id=project_id, destination_group=destination_group,
            group_name=m.get("source_group_name") or default_group_name, group_id=None,
            mark=m, created_by=created_by,
        )
    send_targets, form_insert_index = build_send_targets(
        assignments, already_slots, destination_group, talent_id, project_id,
        default_source_type=default_source_type, default_source_group_name=default_group_name,
    )
    marker_already_sent = await already_sent_marker(talent_id, project_id, destination_group)
    send_marker_on_success = not marker_already_sent
    return send_targets, form_insert_index, send_marker_on_success, already


async def create_send_dispatch_from_approved_plan(
    *, talent_id: str, project_id: str, talent_label: str, project_label: str,
    destination_group: str, assignments: List[Dict[str, Any]],
    default_source_type: str, default_group_name: Optional[str],
    form_message: Optional[str], submission_id: Optional[str], content_hash: Optional[str],
    created_by: str = "whatsapp-agent",
) -> str:
    """Dispatch a REAL SEND execution directly from an ALREADY-RESOLVED
    assignments list — no fresh WhatsApp scan, no second
    validate_candidates pass (Production fix — Issue 1, 2026-09-09: "two
    marked audition takes but only one was sent"). `assignments` is
    exactly the list casting_pipeline._preview_send_marks resolved and
    showed the admin in the SEND confirmation card (persisted on the
    approval row via save_send_preview_cache as `preview_assignments`) —
    the SAME immutable plan the admin approved, never rebuilt from
    scratch at execution time.

    ROOT CAUSE this replaces: the OLD approval path
    (casting_pipeline._send_one_pair) dispatched a BRAND NEW
    mode="scan" request per configured source, which the worker
    re-scanned live and the backend re-validated via validate_candidates
    a SECOND time, independent of whatever the confirmation preview had
    found moments earlier. Two independent live WhatsApp scans of the
    same chat have no guarantee of finding an identical set of replies
    (WhatsApp Web's virtualized message list, scroll-position/timing —
    see mark_scan.py's _dump_window docstring for the exact mechanism);
    a mark visible to the FIRST scan (preview) could legitimately be
    missing from the SECOND (execution) scan's rendered window through
    no fault of any key/identity logic — every key used throughout this
    module (slot_key, the media_sends unique index) already keys
    correctly on (media_role, take_number), so Take 1 and Take 2 were
    never at risk of colliding/overwriting each other ONCE both were
    captured as candidates; the loss happened one level up, at "was Take
    1 even in this scan's window at all" — a question the OLD code asked
    TWICE, independently, with no guarantee of the same answer both
    times.

    Fix: ask it ONCE (at confirmation-preview time) and REUSE that exact
    answer for execution — this function is the reuse path.
    `prepare_send_targets` still runs (idempotency bookkeeping,
    already-sent filtering, ordering) — nothing about correctness there
    was ever the issue; only the redundant live re-scan is skipped.
    Mixed-source assignments (each item's own source_type/
    source_group_name, set by the multi-source preview scan) pass
    through build_send_targets exactly as before — per-item source is
    never collapsed to one shared value.

    Inserts the scan_requests doc directly in the SAME "ready for the
    worker's mode=\"send\" pickup" shape media_assignment_worker.py's own
    scan-done branches already produce (status=DOWNLOAD_STATUS_PENDING),
    so the ENTIRE downstream pipeline — claim endpoint, mark_scan.py's
    _run_send, /download-result, _process_download_done's report/
    idempotency/marker/post-approval logic — is reused completely
    unchanged. Returns the new request's id."""
    send_targets, form_insert_index, send_marker_on_success, already = await prepare_send_targets(
        talent_id=talent_id, project_id=project_id, destination_group=destination_group,
        assignments=assignments, default_source_type=default_source_type,
        default_group_name=default_group_name, created_by=created_by,
    )
    req_id = str(uuid.uuid4())
    now = _now()
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "send",
        "workflow": "send",
        "status": DOWNLOAD_STATUS_PENDING,
        "group_name": default_group_name,
        "source_type": default_source_type,
        "destination_group": destination_group,
        "talent_id": talent_id, "project_id": project_id,
        "talent_label": talent_label, "project_label": project_label,
        "send_targets": send_targets,
        "form_insert_index": form_insert_index,
        "send_marker_on_success": send_marker_on_success,
        "form_message": form_message,
        "submission_id": submission_id,
        "content_hash": content_hash,
        "send_dispatched_at": now,  # phase-timing instrumentation, diagnostic only
        "pending_report_context": {
            "talent_label": talent_label, "project_label": project_label,
            "destination_group": destination_group, "already": already,
            "submission_id": submission_id, "content_hash": content_hash,
            "form_message_included": bool(form_message),
            "marker_attempted": send_marker_on_success,
        },
        "download_results": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    })
    return req_id


async def record_marker_sent(talent_id: str, project_id: str, destination_group: str, created_by: str) -> None:
    """Upserts idempotently — safe to call more than once for the same
    (talent, project, destination) triple (mirrors record_send/
    record_form_send's own insert-then-swallow-duplicate-key pattern)."""
    try:
        await db[COMPLETION_MARKERS_COLLECTION].insert_one({
            "talent_id": talent_id, "project_id": project_id, "destination_group": destination_group,
            "sent_at": _now(), "created_by": created_by,
        })
    except Exception:
        pass


async def get_send_approval(talent_id: str, project_id: str, destination_group: str) -> Optional[Dict[str, Any]]:
    return await db[SEND_APPROVALS_COLLECTION].find_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {"_id": 0},
    )


# SEND confirmation media-preview cache (Production fix, 2026-09-03) —
# the pre-approval WhatsApp scan (casting_pipeline.py's
# _preview_send_marks) is real WhatsApp latency, not a DB read; without
# caching, EVERY edit turn ("Age = 24" re-rendering the confirmation
# card) would re-trigger a fresh multi-second scan just to redraw a form
# field that has nothing to do with marked media — a direct violation of
# Part 22's speed requirement. Cached on the SAME SEND_APPROVALS_COLLECTION
# row as the draft/approval itself (a separate, independently-$set field
# group — see save_send_approval_draft's own docstring on why this is
# safe: MongoDB's $set only touches the keys given, so this and that
# function never clobber each other), reused only while that row is
# still "pending" (never across an "approved"/"completed" boundary — a
# fresh send must never silently inherit a past operation's media
# discovery) and only within SEND_PREVIEW_CACHE_TTL_SEC of being
# computed (an admin who leaves a draft open for a long time gets a
# fresh scan on their next turn, not a possibly-stale one).
SEND_PREVIEW_CACHE_TTL_SEC = 300


async def get_cached_send_preview(
    talent_id: str, project_id: str, destination_group: str,
) -> Optional[Tuple[Optional[List[Dict[str, Any]]], Optional[str]]]:
    """Returns (assignments, error) from a still-fresh cached preview, or
    None if there is no usable cache (never scanned yet, the approval
    has since moved past "pending", or the cache has aged out) — the
    caller must then run a fresh scan itself. `error=None` with
    assignments=[] is a valid, real cached result (genuinely no marked
    media found), distinguished from "no cache at all" by this
    function's own None return.

    The TTL check is a query filter, not a Python-side datetime
    subtraction — pymongo/motor hand back `preview_computed_at` as an
    offset-NAIVE datetime even though _now() wrote it timezone-aware
    (BSON dates carry no tzinfo; the driver's own representation, not a
    real ambiguity), so subtracting it from a fresh aware _now() raises
    TypeError. Letting MongoDB compare BSON-to-BSON sidesteps that
    entirely — the same pattern services/media_assignment_worker.py's
    own _reap_stuck_claims already uses for its claimed_at cutoff."""
    cutoff = _now().timestamp() - SEND_PREVIEW_CACHE_TTL_SEC
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    doc = await db[SEND_APPROVALS_COLLECTION].find_one(
        {
            "talent_id": talent_id, "project_id": project_id, "destination_group": destination_group,
            "status": SEND_APPROVAL_STATUS_PENDING,
            "preview_computed_at": {"$exists": True, "$gte": cutoff_dt},
        },
        {"_id": 0, "preview_assignments": 1, "preview_error": 1},
    )
    if not doc:
        return None
    return doc.get("preview_assignments"), doc.get("preview_error")


async def save_send_preview_cache(
    talent_id: str, project_id: str, destination_group: str,
    *, assignments: Optional[List[Dict[str, Any]]], error: Optional[str],
) -> None:
    """Upserts just the preview fields — safe to call before
    save_send_approval_draft has ever created the row for this attempt
    (a fresh "send" previews media before it has anything else to
    persist)."""
    await db[SEND_APPROVALS_COLLECTION].update_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {
            "$set": {
                "preview_assignments": assignments, "preview_error": error,
                "preview_computed_at": _now(),
            },
            "$setOnInsert": {
                "talent_id": talent_id, "project_id": project_id, "destination_group": destination_group,
                "created_at": _now(),
            },
        },
        upsert=True,
    )


async def save_send_approval_draft(
    *, talent_id: str, project_id: str, destination_group: str, submission_id: str,
    overrides: Dict[str, str], message: str, content_hash: str,
) -> Dict[str, Any]:
    """Persists the CURRENT draft of the outgoing SEND form (raw submission
    values plus whatever the admin has edited so far) as "pending" —
    overwrites any earlier pending draft for this same (talent, project,
    destination), but never touches an "approved"/"completed" row (a fresh
    edit turn only ever runs while a draft is still "pending"; once
    approved, this function is not called again for the same attempt)."""
    now = _now()
    doc = {
        "talent_id": talent_id, "project_id": project_id, "destination_group": destination_group,
        "submission_id": submission_id, "overrides": overrides, "message": message,
        "content_hash": content_hash, "status": SEND_APPROVAL_STATUS_PENDING,
        "updated_at": now,
    }
    await db[SEND_APPROVALS_COLLECTION].update_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return doc


async def approve_send_form(
    talent_id: str, project_id: str, destination_group: str, approved_by: str,
) -> Optional[Dict[str, Any]]:
    """Freezes the current draft as "approved" — from this point on, the
    stored `message`/content_hash is the exact approved snapshot: never
    regenerated from the (possibly since-edited) submission again, on this
    attempt or a later retry, until the operation reaches "completed"."""
    now = _now()
    await db[SEND_APPROVALS_COLLECTION].update_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {"$set": {"status": SEND_APPROVAL_STATUS_APPROVED, "approved_at": now, "approved_by": approved_by}},
    )
    return await get_send_approval(talent_id, project_id, destination_group)


async def complete_send_approval(talent_id: str, project_id: str, destination_group: str) -> None:
    """Marks the approval record terminal once the full SEND operation
    (media + form + ☑️ marker) has finished successfully — kept (not
    deleted) as an audit trail of what was approved; a later "send" for
    this same talent/project starts a brand new draft rather than resuming
    this one."""
    await db[SEND_APPROVALS_COLLECTION].update_one(
        {"talent_id": talent_id, "project_id": project_id, "destination_group": destination_group},
        {"$set": {"status": SEND_APPROVAL_STATUS_COMPLETED, "completed_at": _now()}},
    )


_AVAILABILITY_LABELS = {
    "yes": "Available",
    "partial": "Available only some days",
    "no": "Not available",
}
_BUDGET_LABELS = {
    "accept": "Accepts budget",
    "custom": "Counter-offer",
}


def _format_location(location: Any) -> str:
    """location is a list of {"city", "country"} dicts (never a bare
    string) — reduce it to a clean human-readable "City, Country" (joining
    multiple entries with "; "), never the raw list/dict."""
    def _one(loc: Any) -> str:
        if isinstance(loc, dict):
            city = (loc.get("city") or "").strip()
            country = (loc.get("country") or "").strip()
            return ", ".join(p for p in (city, country) if p)
        return str(loc or "").strip()

    if isinstance(location, list):
        parts = [_one(loc) for loc in location]
        return "; ".join(p for p in parts if p)
    if isinstance(location, dict):
        return _one(location)
    return str(location or "").strip()


def _format_availability(availability: Any) -> str:
    """availability is a {"status", "note"} dict — render the human label,
    appending the free-text note only if present."""
    if not isinstance(availability, dict):
        return str(availability or "").strip()
    label = _AVAILABILITY_LABELS.get(availability.get("status"), "")
    note = (availability.get("note") or "").strip()
    if label and note:
        return f"{label} — {note}"
    return label or note


def _format_budget(budget: Any) -> str:
    """budget is a {"status", "value"} dict — render the human label, with
    the submitted counter-offer value appended when present."""
    if not isinstance(budget, dict):
        return str(budget or "").strip()
    label = _BUDGET_LABELS.get(budget.get("status"), "")
    value = (budget.get("value") or "").strip()
    if value:
        return f"{label} ({value})" if label else value
    return label


def _format_instagram_link(handle: Any) -> str:
    """instagram_handle is a bare username — the outgoing form must carry
    an actual clickable URL, not just the handle."""
    h = str(handle or "").strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    return f"https://instagram.com/{h.lstrip('@')}"


def _format_talent_submission_name(name: str) -> str:
    """Talentgram's standard submission-name presentation format
    (Production fix, 2026-09-08): "First L." — the first token of the
    talent's full name, a space, the first letter of the LAST token,
    and a period. Applied ONLY here, at the single presentation
    boundary build_form_send_message already is for every SEND form
    surface (preview, edit view, approval snapshot, and the actual
    outgoing message all funnel through this one function) — never
    touches the talent's canonical name in the database, never used for
    identity/lookup (talent_id/canonical name resolution happens
    entirely upstream of this, in _resolve_send_target, unaffected).

    A single-token name ("Madonna" — no surname at all) is returned
    unchanged, never fabricating a surname initial. Deterministic and
    whitespace-tolerant: `.split()` with no argument collapses any
    amount of internal whitespace and strips leading/trailing, so
    "  Shivi   Rajput  " normalizes the same as "Shivi Rajput" before
    formatting. Never crashes on an empty/missing name."""
    tokens = (name or "").split()
    if len(tokens) <= 1:
        return (name or "").strip()
    first, last = tokens[0], tokens[-1]
    return f"{first} {last[0].upper()}."


# Canonical override keys an admin's edit can target (Phase 2/4) — every
# fixed field of the outgoing form except the two identity fields (Project
# Name / Name), which are resolved from the command itself, not editable
# as form content. Custom questions are keyed by their own question text
# (see build_form_send_message below), not by a fixed key here.
OVERRIDABLE_FIELD_LABELS: Dict[str, str] = {
    "age": "Age",
    "height": "Height",
    "location": "Current Location",
    "availability": "Availability",
    "competitive_brand": "Competitive Brand",
    "instagram_link": "Instagram Link",
    "budget": "Budget",
}

# Natural-language field EXCLUSION (Production fix, Issue 2) — "Exclude
# Instagram Link" means REMOVE that field from the outgoing form, never
# "set its value to something". A plain "" override already existed for
# blanking a value while still SHOWING the field's label with nothing
# under it; this sentinel is a distinct, explicit "omit this field's line
# entirely" signal — see build_form_send_message's _add below.
EXCLUDED_FIELD_VALUE = "\x00EXCLUDED\x00"


def build_form_send_message(
    sub: Dict[str, Any], project: Optional[Dict[str, Any]], talent_label: str, project_label: str,
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Formats an APPROVED submission as the outgoing SEND form — a fixed,
    minimal client-facing field set (never the internal/raw fields UPLOAD's
    Client View shows), built from the SAME single-source-of-truth shape
    (core._submission_to_client_shape) that already drives Client
    View/PDF/download-bundle, never a second, independently-invented
    field-extraction path. Returns both the message text and its
    content_hash (see already_sent_form/record_form_send).

    `overrides` (Phase 2/4, 2026-08-26) — admin edits made during the
    approval step, keyed by OVERRIDABLE_FIELD_LABELS' keys (fixed fields)
    or by the literal question text (custom questions); present and
    non-empty for a given key means "use this value instead of the
    submission's own", including blanking a field out entirely with "" —
    absent means "use the submission's value unchanged". Never mutates the
    submission itself; this is purely how the OUTGOING MESSAGE is rendered."""
    shape = _submission_to_client_shape(sub, project=project)
    overrides = overrides or {}

    lines: List[str] = []

    def _add(label: str, value: Any, override_key: Optional[str] = None) -> None:
        if override_key is not None and override_key in overrides:
            value = overrides[override_key]
        if value == EXCLUDED_FIELD_VALUE:
            # "Exclude Instagram Link" etc. — the field's line (label AND
            # value) is omitted from the outgoing form entirely, never
            # shown as a blank/empty field.
            return
        if lines:
            lines.append("")
        value = "" if value in (None, [], {}) else str(value).strip()
        lines.append(f"{label}:\n{value}" if value else f"{label}:")

    _add("Project Name", project_label)
    _add("Name", _format_talent_submission_name(talent_label))
    _add("Age", shape.get("age"), "age")
    _add("Height", shape.get("height"), "height")
    _add("Current Location", _format_location(shape.get("location")), "location")
    _add("Availability", _format_availability(shape.get("availability")), "availability")
    _add("Competitive Brand", shape.get("competitive_brand"), "competitive_brand")
    _add("Instagram Link", _format_instagram_link(shape.get("instagram_handle")), "instagram_link")

    custom_answers = shape.get("custom_answers") or []
    for qa in custom_answers:
        question = (qa.get("question") or "").strip()
        if question:
            _add(question, qa.get("answer"), question)

    _add("Budget", _format_budget(shape.get("budget")), "budget")

    message = "\n".join(lines).strip()
    content_hash = hashlib.sha256(
        json.dumps({"message": message}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"message": message, "content_hash": content_hash}
