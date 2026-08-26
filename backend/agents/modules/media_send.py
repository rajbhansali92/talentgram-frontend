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
from typing import Any, Dict, List, Optional

from core import db, _submission_to_client_shape
from agents.modules.media_assignment import (
    MAX_SCAN_MESSAGES,
    SCAN_REQUESTS_COLLECTION,
    SCAN_STATUS_PENDING,
    _now,
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
    content_hash: Optional[str] = None,
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
    can mark the form_sends row sent/failed once the worker reports back."""
    req_id = str(uuid.uuid4())
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "scan",
        "workflow": "send",
        "status": SCAN_STATUS_PENDING,
        "group_name": group_name,
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
        if lines:
            lines.append("")
        value = "" if value in (None, [], {}) else str(value).strip()
        lines.append(f"{label}:\n{value}" if value else f"{label}:")

    _add("Project Name", project_label)
    _add("Name", talent_label)
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
