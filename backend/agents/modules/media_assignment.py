"""Media-Assignment mechanism (Phase 1, 2026-08-22) — turns an
`@Gunwanti + mark <project> <role>` WhatsApp reply, made inside a talent's
own group, into an exact attachment on the correct Talentgram submission.

Built on Phase 0's proven mechanism (see the "ticklish-cuddling-willow"
plan): a reply's quoted-media thumbnail hashes byte-identically to its
source message's own thumbnail, and the mention resolves to a stable
WhatsApp LID — never a display name, never a timestamp/position guess.

Module boundary: this file owns interpretation (mark-text parsing, identity
validation, project/role resolution, ambiguity/failure detection,
idempotency, the assignment audit record). It never talks to WhatsApp
directly — that's whatsapp-worker/mark_scan.py's job (open the group, hash
thumbnails, extract mention/mark text, and — once told exactly which
messages are needed — download their bytes). The two sides communicate only
through the `whatsapp_scan_requests` collection's documented shape below,
so either side can be re-deployed independently.

Bounded, on-demand only: a scan request is created once per `upload`
command and claimed once by the worker — there is no continuous polling of
any talent group anywhere in this design.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import db
from agents.modules import casting_pipeline_nlu as nlu

IDENTITY_COLLECTION = "whatsapp_agent_identity"
SCAN_REQUESTS_COLLECTION = "whatsapp_scan_requests"
ASSIGNMENTS_COLLECTION = "media_assignments"

# The worker never scrolls WhatsApp Web further back than this for a scan —
# "smallest practical history window", not "scan indefinitely" (see plan).
# The source of truth is always the @Gunwanti + mark + hash match, never
# how wide this window happens to be; a mark outside it is a resolution
# failure, not a wider retry.
MAX_SCAN_MESSAGES = 300

# whatsapp_scan_requests.status lifecycle:
#   pending_scan -> (worker) -> scan_done | scan_failed
#   -> (backend orchestrator) -> pending_download | finished (nothing to
#      download, or every mark failed validation)
#   pending_download -> (worker) -> download_done | download_failed
#   -> (backend orchestrator) -> finished
SCAN_STATUS_PENDING = "pending_scan"
SCAN_STATUS_DONE = "scan_done"
SCAN_STATUS_FAILED = "scan_failed"
DOWNLOAD_STATUS_PENDING = "pending_download"
DOWNLOAD_STATUS_DONE = "download_done"
DOWNLOAD_STATUS_FAILED = "download_failed"
STATUS_FINISHED = "finished"

ASSIGN_STATUS_MARKED = "marked"
ASSIGN_STATUS_RESOLVING = "resolving"
ASSIGN_STATUS_UPLOADED = "uploaded"
ASSIGN_STATUS_FAILED = "failed"

MEDIA_ROLES = ("take", "intro", "photos")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_gunwanti_identity() -> Optional[Dict[str, str]]:
    """{"name", "phone", "lid"} — LID is the only field ever compared
    against a mention; phone is reference metadata only (see plan)."""
    return await db[IDENTITY_COLLECTION].find_one({}, {"_id": 0})


async def ensure_indexes() -> None:
    """Called once at backend startup (see server.py) — safe to call
    repeatedly, create_index is idempotent.

    2026-08-23: the original unique key (talent_id, project_id,
    source_message_id) was correct for single-message marks but wrong for
    albums — every tile in an album shares the SAME source_message_id (the
    album's own data-id), so two tiles marked for the same talent+project
    collided on this index; record_assignment's except-fallback would
    silently return tile 1's row when tile 2 was inserted. Added
    source_thumbnail_hash (unique per tile) to the key. Old index dropped
    by name first since Mongo errors on redefining an existing index name
    with different keys."""
    try:
        await db[ASSIGNMENTS_COLLECTION].drop_index("uniq_talent_project_source_message")
    except Exception:
        pass  # never existed, or already dropped — fine either way
    await db[ASSIGNMENTS_COLLECTION].create_index(
        [("talent_id", 1), ("project_id", 1), ("source_message_id", 1), ("source_thumbnail_hash", 1)],
        unique=True, name="uniq_talent_project_source_message_hash",
    )
    await db[SCAN_REQUESTS_COLLECTION].create_index("created_at")


# ---------------------------------------------------------------------------
# Mark-text parsing — "google take 1" / "google intro" / "intro google" /
# "introduction google" / "google introduction" / "google photos", case-
# insensitive, position-independent. The literal word "mark" itself is
# stripped by the caller before this runs (see extract_role_and_project);
# kept as a defensive re-check here too since this function may be called
# directly in tests.
# ---------------------------------------------------------------------------
_MARK_KEYWORD_RE = re.compile(r"\bmark\b", re.IGNORECASE)
_TAKE_RE = re.compile(r"\btake\s*([1-9][0-9]?)\b", re.IGNORECASE)
_INTRO_RE = re.compile(r"\bintro(?:duction)?\b", re.IGNORECASE)
_PHOTOS_RE = re.compile(r"\bphotos?\b", re.IGNORECASE)


@dataclass
class ParsedMark:
    project_fragment: str
    media_role: str  # "take" | "intro" | "photos"
    take_number: Optional[int] = None


def extract_role_and_project(raw_mark_text: str) -> Optional[ParsedMark]:
    """None means "contains 'mark' but not a shape we recognize" (e.g. no
    role keyword at all) — the caller reports this as an unresolvable mark,
    never guesses a role."""
    if not raw_mark_text or not _MARK_KEYWORD_RE.search(raw_mark_text):
        return None
    working = _MARK_KEYWORD_RE.sub(" ", raw_mark_text, count=1)

    take_m = _TAKE_RE.search(working)
    if take_m:
        media_role, take_number = "take", int(take_m.group(1))
        working = _TAKE_RE.sub(" ", working, count=1)
    elif _INTRO_RE.search(working):
        media_role, take_number = "intro", None
        working = _INTRO_RE.sub(" ", working, count=1)
    elif _PHOTOS_RE.search(working):
        media_role, take_number = "photos", None
        working = _PHOTOS_RE.sub(" ", working, count=1)
    else:
        return None

    project_fragment = re.sub(r"\s+", " ", working).strip()
    if not project_fragment:
        return None
    return ParsedMark(project_fragment=project_fragment, media_role=media_role, take_number=take_number)


# ---------------------------------------------------------------------------
# Scan-request lifecycle (backend side of the state machine described in
# the module docstring). whatsapp-worker/mark_scan.py claims/writes these
# same documents — the shape here is the contract between the two services.
# ---------------------------------------------------------------------------
async def create_scan_request(
    *, talent_id: str, talent_label: str, project_id: str, project_label: str, group_name: str,
) -> str:
    req_id = str(uuid.uuid4())
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "scan",
        "status": SCAN_STATUS_PENDING,
        "group_name": group_name,
        "talent_id": talent_id,
        "talent_label": talent_label,
        "project_id": project_id,
        "project_label": project_label,
        "max_messages": MAX_SCAN_MESSAGES,
        "candidates": None,
        "download_targets": None,
        "download_results": None,
        "report": None,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
    })
    return req_id


async def get_scan_request(request_id: str) -> Optional[Dict[str, Any]]:
    return await db[SCAN_REQUESTS_COLLECTION].find_one({"id": request_id}, {"_id": 0})


# ---------------------------------------------------------------------------
# Candidate validation — turns the worker's raw {lid, mark_text,
# quoted_thumbnail_hash, reply_message_id, resolved_source_message_id}
# candidates into a validated assignment set for ONE requested project.
# Every rule below is a direct restatement of the spec's "SAFETY RULES" —
# nothing here guesses; every branch either resolves cleanly or reports a
# specific, named failure.
# ---------------------------------------------------------------------------
@dataclass
class ValidationOutcome:
    ok: bool
    assignments: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    ambiguous: Optional[Dict[str, Any]] = None  # {"media_role", "take_number", "candidates": [...]}
    unresolved: List[Dict[str, Any]] = dataclass_field(default_factory=list)  # marks with no hash match
    error: Optional[str] = None


def validate_candidates(
    candidates: List[Dict[str, Any]],
    *,
    gunwanti_lid: str,
    requested_project_id: str,
    requested_project_label: str,
    projects: List[Dict[str, str]],
    talent_id: str,
    project_id_for_label: Optional[str] = None,
) -> ValidationOutcome:
    """`candidates` is the worker's raw scan result — every reply in the
    bounded window that contains the literal word "mark", REGARDLESS of
    mention validity (the worker stays a dumb I/O layer; this function
    owns the identity check, per the module docstring)."""
    valid_marks: List[Dict[str, Any]] = []
    for c in candidates:
        if (c.get("mention_lid") or "") != gunwanti_lid:
            # A bare "Gunwanti"/"Talentgram Team" text mention with no real
            # WhatsApp @mention, or a mention of someone else entirely,
            # never qualifies — never falls back to display-name matching.
            continue
        parsed = extract_role_and_project(c.get("mark_text") or "")
        if parsed is None:
            continue  # contains "mark" but no recognizable role -> not a candidate at all
        match = nlu.resolve_project_by_name(parsed.project_fragment, projects)
        if not match.project or match.project["id"] != requested_project_id:
            continue  # belongs to a different (or unresolved) project -> ignored, never mixed in
        valid_marks.append({
            **c, "media_role": parsed.media_role, "take_number": parsed.take_number,
        })

    # Group by (media_role, take_number) to detect duplicate marks of the
    # exact same slot — never auto-pick between them.
    by_slot: Dict[tuple, List[Dict[str, Any]]] = {}
    for m in valid_marks:
        key = (m["media_role"], m["take_number"])
        by_slot.setdefault(key, []).append(m)

    for key, marks in by_slot.items():
        # Distinct source media resolving to the SAME slot is the
        # ambiguity the spec calls out; the SAME source media marked twice
        # (identical resolved_source_message_id) is harmless idempotent
        # duplication, not ambiguity.
        distinct_sources = {m.get("resolved_source_message_id") for m in marks}
        if len(distinct_sources) > 1:
            media_role, take_number = key
            return ValidationOutcome(
                ok=False,
                ambiguous={
                    "media_role": media_role, "take_number": take_number,
                    "candidates": marks,
                },
            )

    unresolved = [m for m in valid_marks if not m.get("resolved_source_message_id")]
    if unresolved:
        return ValidationOutcome(ok=False, unresolved=unresolved)

    # Dedupe identical-slot/identical-source repeats (e.g. the same mark
    # sent twice by accident) down to one assignment per slot.
    seen_slots = set()
    assignments = []
    for m in valid_marks:
        key = (m["media_role"], m["take_number"])
        if key in seen_slots:
            continue
        seen_slots.add(key)
        assignments.append(m)

    return ValidationOutcome(ok=True, assignments=assignments)


# ---------------------------------------------------------------------------
# media_assignments bookkeeping — the persistent, auditable record. Unique
# index on (talent_id, project_id, source_message_id) (see ensure_indexes)
# is what makes a second `upload` run idempotent: an insert for an
# already-recorded source message is a harmless duplicate-key no-op, and
# assignment_status="uploaded" rows are reported, never re-uploaded.
# ---------------------------------------------------------------------------
async def already_uploaded(talent_id: str, project_id: str) -> List[Dict[str, Any]]:
    return await db[ASSIGNMENTS_COLLECTION].find(
        {"talent_id": talent_id, "project_id": project_id, "assignment_status": ASSIGN_STATUS_UPLOADED},
        {"_id": 0},
    ).to_list(200)


async def record_assignment(
    *, talent_id: str, project_id: str, normalized_project: str, group_name: str,
    group_id: Optional[str], mark: Dict[str, Any], created_by: str,
) -> Dict[str, Any]:
    """Upserts a `marked`-status row keyed on the unique index — safe to
    call repeatedly for the same source message without creating
    duplicates. Returns the (possibly pre-existing) document."""
    doc = {
        "assignment_id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "project_id": project_id,
        "source_group_id": group_id,
        "source_group_name": group_name,
        "source_message_id": mark.get("resolved_source_message_id"),
        "source_media_type": mark.get("source_media_type"),
        "source_thumbnail_hash": mark.get("quoted_thumbnail_hash"),
        "source_sender": mark.get("source_sender"),
        "source_timestamp": mark.get("source_timestamp"),
        "mark_reply_message_id": mark.get("reply_message_id"),
        "mark_reply_text": mark.get("mark_text"),
        "mark_target_phone": mark.get("mention_phone"),
        "mark_target_contact_id": mark.get("mention_lid"),
        "normalized_project": normalized_project,
        "media_role": mark.get("media_role"),
        "take_number": mark.get("take_number"),
        "original_label": mark.get("mark_text"),
        "assignment_status": ASSIGN_STATUS_MARKED,
        "created_at": _now(),
        "created_by": created_by,
    }
    try:
        await db[ASSIGNMENTS_COLLECTION].insert_one(doc)
        return doc
    except Exception:
        # Matches the unique index exactly (talent_id, project_id,
        # source_message_id, source_thumbnail_hash) — for an album,
        # source_message_id alone is shared by every tile, so this MUST
        # include the hash or it can return a different tile's row.
        existing = await db[ASSIGNMENTS_COLLECTION].find_one(
            {
                "talent_id": talent_id, "project_id": project_id,
                "source_message_id": doc["source_message_id"],
                "source_thumbnail_hash": doc["source_thumbnail_hash"],
            },
            {"_id": 0},
        )
        return existing or doc


async def mark_assignment_status(
    talent_id: str, project_id: str, source_message_id: str, source_thumbnail_hash: str, status: str, **extra
) -> None:
    """source_thumbnail_hash is required, not optional — for an album,
    source_message_id alone matches every tile sharing that album; without
    the hash, update_one could silently update the wrong tile's row."""
    await db[ASSIGNMENTS_COLLECTION].update_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "source_message_id": source_message_id, "source_thumbnail_hash": source_thumbnail_hash,
        },
        {"$set": {"assignment_status": status, **extra}},
    )


def submission_label(media_role: str, take_number: Optional[int]) -> str:
    """The label shown on the SUBMISSION itself, where the project is
    already implicit (unlike role_label, used for cross-project WhatsApp
    chat reports, which prefixes the project name) — "Take 1",
    "Introduction", "Photo" — matching the codebase's own "Take N"
    submission-media-naming convention (routers/submissions.py)."""
    if media_role == "take" and take_number:
        return f"Take {take_number}"
    if media_role == "intro":
        return "Introduction"
    if media_role == "photos":
        return "Photo"
    return media_role.capitalize()


def role_label(media_role: str, take_number: Optional[int], project_label: str) -> str:
    if media_role == "take" and take_number:
        return f"{project_label} Take {take_number}"
    if media_role == "intro":
        return f"{project_label} Intro"
    if media_role == "photos":
        return f"{project_label} Photos"
    return f"{project_label} {media_role}"
