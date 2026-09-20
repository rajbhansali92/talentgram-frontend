"""Mark Intent — durable, immutable media-identity layer (2026-09-20).

Root cause this closes (see the accepted forensic reports from this same
investigation): WhatsApp Web virtualizes its message DOM, so the EXISTING
scan mechanism (whatsapp-worker/mark_scan.py's _dump_window + jump-to-
quoted fallback — left completely UNCHANGED by this module, per explicit
instruction) is a non-deterministic, single-shot computation. Proven
directly from production data: the SAME reply_message_id resolved
successfully on one scan, failed on the very next scan 83 seconds later,
and on a THIRD scan resolved to a DIFFERENT resolved_source_message_id
than the second scan had found (Nikki Sharma / Dettol Film 1, 2026-09-20,
11:38-11:41). validate_candidates (media_assignment.py) had no memory
between scans — each one recomputed identity from scratch and discarded
whatever it learned on failure.

This module does NOT touch WhatsApp Web navigation, does NOT touch
validate_candidates' own safety rules (project mismatch/ambiguous, slot
dedup, recency tie-break — all untouched, still the single source of
truth for "is this mark valid for this project"), and does NOT touch the
existing media_assignments/media_sends uniqueness model (already correct
— scoped by (talent_id, project_id, source_message_id, source_thumbnail_
hash[, destination_group]), which already allows the same source media to
be independently assigned to any number of projects; see
media_assignment.py:227-230 / media_send.py:90-96).

What THIS module adds, sitting strictly between the worker's raw scan
candidates and validate_candidates' own output:

  1. A durable MarkIntent record, created the FIRST time any scan
     observes a given reply, independent of whether that scan resolved
     it — a failed scan must never discard what the reply itself already
     told us (reply_message_id, quoted_thumbnail_hash/media_type, mark
     text, talent/project/role).
  2. A write-once resolution lock: the first scan that successfully
     resolves an intent's source becomes authoritative forever. Enforced
     with an ATOMIC MongoDB find_one_and_update filtered on
     "resolved_source_message_id: None" — this is what "atomic claiming"
     means here (Phase 16/17 of the spec): the safety property needed is
     "two concurrent writers never both successfully lock conflicting
     answers", which a single-document compare-and-set guarantees
     regardless of which worker/process races to write it, with no
     separate distributed-lock mechanism required (the codebase already
     uses this exact pattern for its own scan-request claim endpoint —
     see routers/agents_whatsapp.py's find_one_and_update-based claim).
  3. A later scan that returns a DIFFERENT source for an
     already-resolved intent is REJECTED, not applied — logged as
     MARK_INTENT_RESOLUTION_CONFLICT, existing lock stays authoritative.
  4. A later scan that fails to relocate an ALREADY-resolved intent's
     source doesn't need to — the lock is reused directly. This is the
     actual fix for the proven bug: once resolved, UPLOAD/SEND stop
     depending on every subsequent scan re-finding the same message.
  5. A scan that fails to resolve a NOT-yet-locked intent just records
     the miss and keeps it retryable (unresolved/resolving), up to a
     generous, configurable attempt ceiling before FAILED_PERMANENTLY.

Correlation (Phase 6 of the spec) — deciding whether a NEW reply is
"another attempt at the same logical assignment" (Case A) vs "legitimate
reuse of the same source for a different project" (Case B) vs "a
resend, must stay distinguishable" (Case C) — uses exactly the tuple
(talent_id, project_id, media_role, take_number, quoted_thumbnail_hash)
as the correlation key:
  - Case A: same talent+project+role+hash -> project_id is the SAME ->
    correlates into the existing UNRESOLVED intent (same logical
    assignment, another attempt).
  - Case B: same hash, DIFFERENT project_id -> never correlates (project_id
    is part of the key) -> independent intent, independent assignment —
    exactly the shared-introduction requirement.
  - Case C: a resend re-encodes to a different quoted_thumbnail_hash
    almost always -> never correlates -> independent intent, distinct
    identity preserved, never silently merged.
This is empirically validated against real production data: Nikki
Sharma's two "Mark introduction video" replies (11:31 and 11:39, quoting
the SAME original media) carried the IDENTICAL quoted_thumbnail_hash
"de2897df..." in the actual scan records — confirming this key correctly
identifies "same logical mark, second attempt" from real evidence, not
merely from a hypothesis.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core import db
from agents.modules import media_assignment as _ma
from agents.modules import casting_pipeline_nlu as _nlu

logger = logging.getLogger("mark_intent")

MARK_INTENTS_COLLECTION = "mark_intents"

STATUS_UNRESOLVED = "unresolved"
STATUS_RESOLVING = "resolving"
STATUS_RESOLVED = "resolved"
STATUS_FAILED_PERMANENTLY = "failed_permanently"

RESOLUTION_METHOD_PRIMARY = "primary_window_lookup"
RESOLUTION_METHOD_JUMP_FALLBACK = "jump_fallback"

# Generous, configurable (Phase 3 — "do NOT hard-code a tiny retry
# window"). Each "attempt" is one scan pass that observed this intent and
# failed to relocate its source — with scans dispatched on ordinary
# UPLOAD/SEND retries and the background retry pass (once built), a
# default of 40 gives days of real-world retry room before a human is
# ever told to re-MARK, while still being a genuine, finite ceiling —
# never silently infinite.
DEFAULT_MAX_RESOLUTION_ATTEMPTS = int(os.environ.get("MARK_INTENT_MAX_ATTEMPTS", "40"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes() -> None:
    """Called once at backend startup alongside media_assignment.ensure_indexes
    / media_send.ensure_indexes (see server.py) — safe to call repeatedly,
    create_index is idempotent."""
    await db[MARK_INTENTS_COLLECTION].create_index(
        "reply_message_id", unique=True, name="uniq_reply_message_id",
    )
    await db[MARK_INTENTS_COLLECTION].create_index(
        [
            ("talent_id", 1), ("project_id", 1), ("media_role", 1),
            ("take_number", 1), ("quoted_thumbnail_hash", 1),
        ],
        name="correlation_key",
    )
    await db[MARK_INTENTS_COLLECTION].create_index([("status", 1)], name="status_idx")


async def has_retryable_intents(talent_id: str, project_id: str) -> bool:
    """Phase 7/13/15 — distinguishes a genuine "still verifying" state
    from silence. Used by the SEND preview's pure-timeout branch (the
    underlying scan never finished within the preview's own bounded
    budget — see casting_pipeline._scan_and_validate_multi_source's own
    (None, None) contract): if at least one intent for this talent/
    project is UNRESOLVED/RESOLVING (not yet exhausted its retry
    ceiling), the honest message is "still verifying", never "failed"."""
    return await db[MARK_INTENTS_COLLECTION].count_documents({
        "talent_id": talent_id, "project_id": project_id,
        "status": {"$in": [STATUS_UNRESOLVED, STATUS_RESOLVING]},
    }) > 0


async def any_permanently_failed(talent_id: str, project_id: str) -> bool:
    """Phase 7/13/15 — the companion check for outcome.unresolved: only
    when at least one intent has genuinely exhausted its retry ceiling
    should the admin be told to re-MARK; otherwise the honest message is
    "still verifying", not a hard failure."""
    return await db[MARK_INTENTS_COLLECTION].count_documents({
        "talent_id": talent_id, "project_id": project_id,
        "status": STATUS_FAILED_PERMANENTLY,
    }) > 0


async def get_freshly_resolved_if_complete(
    talent_id: str, project_id: str, *, within_seconds: float,
) -> Optional[List[Dict[str, Any]]]:
    """Closes a gap caught LIVE in production (2026-09-21): a SEND
    action's own per-attempt scan can genuinely take longer than its
    bounded budget (~20-22s) for a busy, heavily-marked group — the
    worker's real answer still lands, via the EXISTING, unchanged
    late-worker/tombstone mechanism (routers.agents_whatsapp.
    report_scan_result's own 404 branch feeding observe_candidates
    directly), but on a code path SEPARATE from
    casting_pipeline._scan_and_validate_multi_source's own (None, None)
    pure-timeout return — so submission_action_queue's verification loop
    never directly observes that its OWN just-triggered scan actually
    succeeded a few seconds late, and would otherwise blindly keep
    retrying (or exhaust its attempt ceiling) even though the answer is
    sitting there, freshly resolved, seconds old. Proven live: a real
    Limca Film1 SEND action logged 10 consecutive "pure timeout" attempts
    while the correct MarkIntents had already been resolved via a late
    report within the FIRST attempt's own window.

    Deliberately NOT a reincarnation of the proven-unsafe
    get_ready_assignments this module removed: that function trusted a
    resolved intent from ANY point in the past, indistinguishable from
    one silently superseded by an unobserved re-mark. This one requires
    POSITIVE, RECENT evidence — at least one intent for this (talent,
    project) resolved within the last `within_seconds` — proving a real
    scan actually ran moments ago and is the direct source of this
    answer, never a stale historical value. `within_seconds` is
    deliberately small, tied to the caller's own verification cadence.
    If a re-mark for an already-resolved slot exists and was visible to
    that SAME recent scan, observe_candidates would already have
    recorded it (a new unresolved intent, or a second resolved one
    colliding on the slot) — both cases are still refused below by the
    unchanged all-resolved / no-collision checks, exactly as
    get_ready_assignments' own removed logic required. If NOTHING was
    resolved recently, returns None — the caller's existing
    has_retryable_intents/any_permanently_failed messaging is unaffected."""
    cutoff = _now() - timedelta(seconds=within_seconds)
    docs = await db[MARK_INTENTS_COLLECTION].find(
        {"talent_id": talent_id, "project_id": project_id},
    ).to_list(200)
    if not docs:
        return None

    def _aware(dt):
        # Motor/pymongo hand back naive UTC datetimes on read (BSON has no
        # tz), while _now()/cutoff above are tz-aware — same normalization
        # this codebase already applies at every other Mongo-datetime
        # comparison site (e.g. media_lifecycle.py, core.py's session
        # expiry checks). Without this, every real (non-test) call here
        # would raise on the very first comparison.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    if not any(d.get("resolved_at") and _aware(d["resolved_at"]) >= cutoff for d in docs):
        return None  # nothing resolved recently -- no evidence a scan just ran

    by_slot: Dict[tuple, List[Dict[str, Any]]] = {}
    for d in docs:
        if d.get("status") != STATUS_RESOLVED:
            return None  # something still pending or permanently failed -- not fully ready
        slot = _slot_key(d["media_role"], d.get("take_number"))
        by_slot.setdefault(slot, []).append(d)

    assignments: List[Dict[str, Any]] = []
    for slot, group in by_slot.items():
        if len(group) > 1:
            logger.warning(
                "MARK_INTENT slot collision in get_freshly_resolved_if_complete talent_id=%s "
                "project_id=%s slot=%s intent_ids=%s — refusing to guess, not ready",
                talent_id, project_id, slot, [g["id"] for g in group],
            )
            return None
        d = group[0]
        assignments.append({
            "media_role": d["media_role"],
            "take_number": d.get("take_number"),
            "resolved_source_message_id": d["resolved_source_message_id"],
            "quoted_thumbnail_hash": d.get("resolved_source_thumbnail_hash"),
            "source_media_type": d.get("resolved_source_media_type"),
            "album_tile_index": d.get("resolved_album_tile_index"),
            "is_album_tile": d.get("resolved_is_album_tile", False),
            "reply_message_id": d.get("reply_message_id"),
            "mark_text": d.get("mark_text"),
            "mark_intent_id": d["id"],
        })
    return assignments or None


async def get_or_create_mark_intent(
    *, reply_message_id: str, talent_id: str, project_id: str, project_label: str,
    media_role: str, take_number: Optional[int], mark_text: str,
    quoted_thumbnail_hash: Optional[str], quoted_media_type: Optional[str],
    source_chat_name: str, worker_id: str,
) -> Dict[str, Any]:
    """Idempotent per reply_message_id (never re-created, never mutated by
    mere re-observation of the SAME reply). Correlates a NEW reply against
    an existing UNRESOLVED/RESOLVING intent sharing the exact
    (talent_id, project_id, media_role, take_number, quoted_thumbnail_hash)
    key — see module docstring for why this key is the correct Case
    A/B/C discriminator. A RESOLVED or FAILED_PERMANENTLY intent is never
    correlated into: a matching key appearing again after resolution is
    treated as a fresh, independent observation — harmless, since
    apply_resolution's own idempotent-vs-conflict handling covers it
    correctly either way (same answer -> no-op; different -> logged
    conflict, existing lock untouched)."""
    existing = await db[MARK_INTENTS_COLLECTION].find_one({"reply_message_id": reply_message_id})
    if existing:
        return existing

    correlated = None
    if quoted_thumbnail_hash is not None:
        correlated = await db[MARK_INTENTS_COLLECTION].find_one({
            "talent_id": talent_id, "project_id": project_id,
            "media_role": media_role, "take_number": take_number,
            "quoted_thumbnail_hash": quoted_thumbnail_hash,
            "status": {"$in": [STATUS_UNRESOLVED, STATUS_RESOLVING]},
        })

    if correlated:
        await db[MARK_INTENTS_COLLECTION].update_one(
            {"id": correlated["id"]},
            {"$addToSet": {"reply_message_ids": reply_message_id}},
        )
        logger.info(
            "MARK_INTENT_ALREADY_EXISTS mark_intent_id=%s new_reply_message_id=%s "
            "original_reply_message_id=%s talent_id=%s project_id=%s role=%s take_number=%s worker_id=%s",
            correlated["id"], reply_message_id, correlated.get("reply_message_id"),
            talent_id, project_id, media_role, take_number, worker_id,
        )
        correlated = dict(correlated)
        correlated["reply_message_ids"] = sorted(set(correlated.get("reply_message_ids", []) + [reply_message_id]))
        return correlated

    doc = {
        "id": str(uuid.uuid4()),
        "reply_message_id": reply_message_id,
        "reply_message_ids": [reply_message_id],
        "source_chat_name": source_chat_name,
        "talent_id": talent_id, "project_id": project_id, "project_label": project_label,
        "media_role": media_role, "take_number": take_number,
        "mark_text": mark_text,
        "quoted_thumbnail_hash": quoted_thumbnail_hash,
        "quoted_media_type": quoted_media_type,
        "created_at": _now(),
        "status": STATUS_UNRESOLVED,
        "attempt_count": 0,
        "last_attempt_at": None,
        "resolved_source_message_id": None,
        "resolved_source_thumbnail_hash": None,
        "resolved_source_media_type": None,
        "resolved_album_tile_index": None,
        "resolved_is_album_tile": False,
        "resolved_at": None,
        "resolution_method": None,
        "created_by_worker_id": worker_id,
        "conflicting_candidates": [],
    }
    try:
        await db[MARK_INTENTS_COLLECTION].insert_one(doc)
    except Exception:
        # Unique-index race on reply_message_id: another concurrent path
        # inserted the exact same reply between our read and write above.
        # Never fatal — the other insert is authoritative, fetch it.
        existing = await db[MARK_INTENTS_COLLECTION].find_one({"reply_message_id": reply_message_id})
        if existing:
            return existing
        raise
    logger.info(
        "MARK_INTENT_CREATED mark_intent_id=%s reply_message_id=%s talent_id=%s project_id=%s "
        "role=%s take_number=%s worker_id=%s",
        doc["id"], reply_message_id, talent_id, project_id, media_role, take_number, worker_id,
    )
    return doc


async def apply_resolution(
    mark_intent_id: str, *,
    candidate_source_message_id: str,
    candidate_source_thumbnail_hash: Optional[str],
    candidate_source_media_type: Optional[str],
    resolution_method: str,
    worker_id: str,
    candidate_album_tile_index: Optional[int] = None,
    candidate_is_album_tile: bool = False,
) -> Dict[str, Any]:
    """Write-once lock (Phase 2/17). `candidate_source_message_id` must be
    a real, non-None resolution this attempt actually found — a failed
    attempt never calls this, it calls record_unresolved_attempt instead.

    `candidate_album_tile_index`/`candidate_is_album_tile` (SEND-preview
    resolved-reuse fix, 2026-09-20): persisted alongside the source
    identity so a LATER caller building a send-ready assignment purely
    from this locked intent (get_ready_assignments — no fresh scan
    candidate available at all) can still address the exact album tile
    the worker's own download path requires (whatsapp-worker/mark_scan.py
    defaults a missing album_tile_index to tile 0 for videos, and skips
    the tile-specific photo path entirely when it's absent — either would
    silently risk the WRONG media without this). Never used to pick
    between competing tiles; still exactly the value the original
    resolving scan observed on the raw candidate.

    Returns the CURRENT, authoritative mark_intent document after the
    operation — callers MUST use ITS resolved_source_message_id/hash for
    any downstream assignment, never the raw candidate's own values,
    because this call's own candidate may have LOST the atomic
    compare-and-set to an earlier attempt (the exact case the mandatory
    regression test proves: attempt 1=B, attempt 2=A, attempt 3=B again
    — this function returns A after every one of those three calls once
    attempt 2 has locked it, regardless of what B claims)."""
    now = _now()
    updated = await db[MARK_INTENTS_COLLECTION].find_one_and_update(
        {"id": mark_intent_id, "resolved_source_message_id": None},
        {
            "$set": {
                "resolved_source_message_id": candidate_source_message_id,
                "resolved_source_thumbnail_hash": candidate_source_thumbnail_hash,
                "resolved_source_media_type": candidate_source_media_type,
                "resolved_album_tile_index": candidate_album_tile_index,
                "resolved_is_album_tile": bool(candidate_is_album_tile),
                "resolved_at": now,
                "resolution_method": resolution_method,
                "status": STATUS_RESOLVED,
                "last_attempt_at": now,
            },
            "$inc": {"attempt_count": 1},
        },
        return_document=True,
    )
    if updated is not None:
        logger.info(
            "MARK_INTENT_RESOLVED mark_intent_id=%s resolved_source_message_id=%s "
            "resolution_method=%s worker_id=%s attempt_count=%s",
            mark_intent_id, candidate_source_message_id, resolution_method, worker_id,
            updated.get("attempt_count"),
        )
        return updated

    # Lock already held (by this exact answer, or a conflicting one) —
    # the atomic filter above matched zero documents, meaning
    # resolved_source_message_id is already non-None. Fetch the
    # authoritative current state and compare.
    current = await db[MARK_INTENTS_COLLECTION].find_one({"id": mark_intent_id})
    if current is None:
        raise ValueError(f"mark_intent {mark_intent_id} not found during resolution — must not happen")

    if current.get("resolved_source_message_id") == candidate_source_message_id:
        # Idempotent re-confirmation of the SAME already-locked answer —
        # not a conflict, just bookkeeping.
        await db[MARK_INTENTS_COLLECTION].update_one(
            {"id": mark_intent_id},
            {"$set": {"last_attempt_at": now}, "$inc": {"attempt_count": 1}},
        )
        return current

    logger.warning(
        "MARK_INTENT_RESOLUTION_CONFLICT mark_intent_id=%s existing_source=%s existing_hash=%s "
        "rejected_candidate_source=%s rejected_candidate_hash=%s resolution_method=%s worker_id=%s "
        "talent_id=%s project_id=%s role=%s take_number=%s observed_at=%s",
        mark_intent_id, current.get("resolved_source_message_id"), current.get("resolved_source_thumbnail_hash"),
        candidate_source_message_id, candidate_source_thumbnail_hash, resolution_method, worker_id,
        current.get("talent_id"), current.get("project_id"), current.get("media_role"), current.get("take_number"), now,
    )
    await db[MARK_INTENTS_COLLECTION].update_one(
        {"id": mark_intent_id},
        {
            "$push": {"conflicting_candidates": {
                "source_message_id": candidate_source_message_id,
                "source_thumbnail_hash": candidate_source_thumbnail_hash,
                "resolution_method": resolution_method,
                "worker_id": worker_id,
                "observed_at": now,
            }},
            "$set": {"last_attempt_at": now},
            "$inc": {"attempt_count": 1},
        },
    )
    return current


async def record_unresolved_attempt(
    mark_intent_id: str, *, worker_id: str, max_attempts: int = DEFAULT_MAX_RESOLUTION_ATTEMPTS,
) -> Dict[str, Any]:
    """A scan pass ran and did NOT relocate the source this time (Phase 3)
    — never a permanent failure by itself. Stays retryable
    (unresolved/resolving) unless the configured attempt ceiling is
    genuinely exhausted. Never touches an intent that is already resolved
    — a stale/late miss racing a since-successful resolution is simply
    ignored, the lock is never at risk from this path."""
    now = _now()
    current = await db[MARK_INTENTS_COLLECTION].find_one({"id": mark_intent_id})
    if current is None:
        raise ValueError(f"mark_intent {mark_intent_id} not found — must not happen")
    if current.get("resolved_source_message_id") is not None:
        # Already resolved by a different attempt — this miss is stale.
        return current

    new_attempt_count = current.get("attempt_count", 0) + 1
    new_status = STATUS_FAILED_PERMANENTLY if new_attempt_count >= max_attempts else STATUS_UNRESOLVED
    updated = await db[MARK_INTENTS_COLLECTION].find_one_and_update(
        {"id": mark_intent_id, "resolved_source_message_id": None},
        {"$set": {"status": new_status, "last_attempt_at": now}, "$inc": {"attempt_count": 1}},
        return_document=True,
    )
    if updated is None:
        # Resolved concurrently between our read and this write — fine,
        # the lock won; re-fetch and return the authoritative state.
        return await db[MARK_INTENTS_COLLECTION].find_one({"id": mark_intent_id})

    if new_status == STATUS_FAILED_PERMANENTLY:
        logger.warning(
            "MARK_INTENT_PERMANENT_FAILURE mark_intent_id=%s attempt_count=%s talent_id=%s project_id=%s "
            "role=%s take_number=%s worker_id=%s",
            mark_intent_id, updated.get("attempt_count"), updated.get("talent_id"), updated.get("project_id"),
            updated.get("media_role"), updated.get("take_number"), worker_id,
        )
    else:
        logger.info(
            "MARK_INTENT_RESOLUTION_RETRY mark_intent_id=%s attempt_count=%s talent_id=%s project_id=%s "
            "role=%s take_number=%s worker_id=%s",
            mark_intent_id, updated.get("attempt_count"), updated.get("talent_id"), updated.get("project_id"),
            updated.get("media_role"), updated.get("take_number"), worker_id,
        )
    return updated


def _slot_key(media_role: str, take_number: Optional[int]) -> tuple:
    return (media_role, take_number)


async def observe_candidates(
    candidates: List[Dict[str, Any]], *, talent_id: str, project_id: str, project_label: str,
    projects: List[Dict[str, str]], source_chat_name: str, worker_id: str,
) -> None:
    """THE unconditional "MARK observed -> persist MarkIntent" step
    (2026-09-20 correctness audit finding). Called on the RAW worker scan
    candidates, BEFORE validate_candidates — deliberately INDEPENDENT of
    validate_candidates' own control flow, never contingent on it.

    Why this exists as a separate function rather than folding into
    enrich_outcome_with_mark_intents (which only ever sees
    validate_candidates' OUTPUT): validate_candidates has an early-return
    branch — `if batch_failures: return ValidationOutcome(ok=False,
    batch_failures=batch_failures, ...)` (media_assignment.py) — taken
    the MOMENT ANY candidate in the scan is an unresolvable batch mark.
    When that fires, `outcome.assignments` and `outcome.unresolved` are
    BOTH empty, even if the SAME scan also contained a completely
    ordinary, cleanly-resolvable "Mark take 1 for X" candidate sitting
    right next to the bad batch mark. A version of this module that only
    ever read outcome.assignments/outcome.unresolved would silently skip
    creating a MarkIntent for that ordinary candidate for as long as the
    unrelated batch mark keeps triggering the early return — the exact
    architecture flaw flagged in this audit: MarkIntent creation must
    never depend on validate_candidates' own gating logic, only on "was
    this reply observed as an ordinary single-item mark".

    Mirrors validate_candidates' OWN admission rule for ONLY the two
    cases that mean "this candidate is a real, single-item mark that
    belongs (or defaults) to the requested project" — reusing the exact
    same extract_role_and_project / resolve_project_by_name functions
    validate_candidates itself calls (never a re-implementation of that
    logic, never a second competing matcher):
      - confidently matches the requested project -> observe it.
      - matches nothing confidently and has no near-miss suggestions
        either (genuinely unrelated text) -> defaults to the requested
        project, same as validate_candidates' own case 4 -> observe it.
    Deliberately does NOT create an intent for a candidate that
    confidently matches a DIFFERENT project, or is ambiguous/suggests
    another project — that candidate isn't for THIS project at all; it
    will get its own intent the next time a scan is scoped to whichever
    project it actually belongs to. Batch marks (`resolution_status ==
    "BATCH_RESOLUTION_FAILED"`) are skipped here too — a batch mark has
    no single stable (media_role, take_number) to key an intent on until
    it's been resolved to individual tiles, a structurally different
    concept this module does not redesign.

    2026-09-20 (preview/late-worker lifecycle fix, same-day follow-up):
    ALSO locks resolution, not merely metadata, whenever the raw
    candidate itself already carries a resolved_source_message_id —
    proven necessary by this exact function's own regression test: the
    orchestrator's skip_validation branch (services/media_assignment_
    worker.py) is, for a late/abandoned SEND preview, the ONLY code that
    ever sees a worker's late scan-result at all (its caller,
    casting_pipeline._scan_and_validate_multi_source, already gave up and
    will never call enrich_outcome_with_mark_intents for it). If this
    function only persisted metadata (reply id/hash/role) and left
    resolution-locking entirely to enrich_outcome_with_mark_intents, a
    late worker result would keep the intent durably UNRESOLVED forever
    — the metadata survives, but the actual valuable payload (which
    exact source message the worker found) is still lost. Locking here
    too closes that gap; apply_resolution's own atomic write-once
    guarantee makes this safe to also run again later from
    enrich_outcome_with_mark_intents for the SAME candidate (idempotent
    re-confirmation, never a conflict — see apply_resolution's own
    "same answer reconfirmed" handling).

    Idempotent and side-effect-free to call repeatedly on the same
    candidates (get_or_create_mark_intent's own idempotency;
    apply_resolution's own write-once idempotency)."""
    for c in candidates:
        if c.get("resolution_status") == "BATCH_RESOLUTION_FAILED":
            continue
        reply_message_id = c.get("reply_message_id")
        if not reply_message_id:
            continue
        parsed = _ma.extract_role_and_project(c.get("mark_text") or "")
        if parsed is None:
            continue
        match = _nlu.resolve_project_by_name(parsed.project_fragment, projects)
        if match.project:
            if match.project["id"] != project_id:
                continue  # confidently belongs to a DIFFERENT project -- not observed here
        elif match.ambiguous or match.suggestions:
            continue  # ambiguous/near-miss -- never guessed, not observed here
        # else: no confident match and no suggestions at all -> genuinely
        # unrelated text -> defaults to the requested project, same as
        # validate_candidates' own case 4.
        intent = await get_or_create_mark_intent(
            reply_message_id=reply_message_id,
            talent_id=talent_id, project_id=project_id, project_label=project_label,
            media_role=parsed.media_role, take_number=parsed.take_number,
            mark_text=c.get("mark_text") or "",
            quoted_thumbnail_hash=c.get("quoted_thumbnail_hash"),
            quoted_media_type=c.get("source_media_type"),
            source_chat_name=source_chat_name, worker_id=worker_id,
        )
        candidate_source = c.get("resolved_source_message_id")
        if candidate_source:
            await apply_resolution(
                intent["id"],
                candidate_source_message_id=candidate_source,
                candidate_source_thumbnail_hash=c.get("quoted_thumbnail_hash"),
                candidate_source_media_type=c.get("source_media_type"),
                resolution_method=RESOLUTION_METHOD_PRIMARY,
                worker_id=worker_id,
                candidate_album_tile_index=c.get("album_tile_index"),
                candidate_is_album_tile=bool(c.get("is_album_tile")),
            )


async def enrich_outcome_with_mark_intents(
    outcome: Any, *, talent_id: str, project_id: str, project_label: str,
    source_chat_name: str, worker_id: str,
) -> Any:
    """Phase 8 — the actual integration point. Wraps validate_candidates'
    OWN, UNCHANGED outcome (its project-mismatch/ambiguous/batch-failure/
    slot-dedup/recency-tie-break logic are never touched here — this
    function never re-derives or second-guesses any of those decisions)
    with the durable Mark Intent layer:

      - Every candidate validate_candidates accepted this round as a
        resolved assignment gets its intent created/locked (first
        successful resolution wins, enforced atomically — see
        apply_resolution). The assignment's own resolved_source_message_id/
        quoted_thumbnail_hash are then OVERWRITTEN with the intent's
        locked values, which may differ from what THIS scan found if an
        earlier scan already locked a different (correct) answer — this
        is the write-once guarantee actually taking effect.
      - Every candidate validate_candidates left unresolved this round
        ALSO gets its intent created/retried. If that intent was already
        resolved by an EARLIER scan, its locked identity is reused to
        build a valid assignment anyway — this is the direct fix for the
        proven Nikki Sharma bug: once resolved, a mark never again
        depends on every subsequent scan re-finding the same message.
        If a promoted assignment's slot collides with one already
        produced above, the promotion is skipped and logged — two
        assignments never coexist for the same slot, matching
        validate_candidates' own ambiguity philosophy.

    Returns a NEW object of the same shape as `outcome` (same dataclass
    type), never mutates the one passed in. `ok` is upgraded from False to
    True ONLY when the sole reason it was False was `unresolved` entries
    that this pass fully promoted via an existing lock — batch_failures/
    ambiguous/project_mismatch/project_ambiguous are NEVER reinterpreted,
    exactly as validate_candidates itself decided."""
    from dataclasses import replace

    locked_assignments: List[Dict[str, Any]] = []
    used_slots: set = set()

    for m in outcome.assignments:
        intent = await get_or_create_mark_intent(
            reply_message_id=m.get("reply_message_id"),
            talent_id=talent_id, project_id=project_id,
            project_label=project_label,
            media_role=m["media_role"], take_number=m.get("take_number"),
            mark_text=m.get("mark_text", ""),
            quoted_thumbnail_hash=m.get("quoted_thumbnail_hash"),
            quoted_media_type=m.get("source_media_type"),
            source_chat_name=source_chat_name, worker_id=worker_id,
        )
        result = await apply_resolution(
            intent["id"],
            candidate_source_message_id=m["resolved_source_message_id"],
            candidate_source_thumbnail_hash=m.get("quoted_thumbnail_hash"),
            candidate_source_media_type=m.get("source_media_type"),
            resolution_method=RESOLUTION_METHOD_PRIMARY,
            worker_id=worker_id,
            candidate_album_tile_index=m.get("album_tile_index"),
            candidate_is_album_tile=bool(m.get("is_album_tile")),
        )
        locked = {
            **m,
            "resolved_source_message_id": result["resolved_source_message_id"],
            "quoted_thumbnail_hash": result.get("resolved_source_thumbnail_hash") or m.get("quoted_thumbnail_hash"),
            "source_media_type": result.get("resolved_source_media_type") or m.get("source_media_type"),
            "album_tile_index": result.get("resolved_album_tile_index") if result.get("resolved_album_tile_index") is not None else m.get("album_tile_index"),
            "is_album_tile": result.get("resolved_is_album_tile") if result.get("resolved_is_album_tile") is not None else m.get("is_album_tile"),
            "mark_intent_id": intent["id"],
        }
        locked_assignments.append(locked)
        used_slots.add(_slot_key(locked["media_role"], locked.get("take_number")))

    still_unresolved: List[Dict[str, Any]] = []
    for m in outcome.unresolved:
        intent = await get_or_create_mark_intent(
            reply_message_id=m.get("reply_message_id"),
            talent_id=talent_id, project_id=project_id,
            project_label=project_label,
            media_role=m["media_role"], take_number=m.get("take_number"),
            mark_text=m.get("mark_text", ""),
            quoted_thumbnail_hash=m.get("quoted_thumbnail_hash"),
            quoted_media_type=m.get("source_media_type"),
            source_chat_name=source_chat_name, worker_id=worker_id,
        )
        if intent.get("resolved_source_message_id"):
            slot = _slot_key(m["media_role"], m.get("take_number"))
            if slot in used_slots:
                logger.warning(
                    "MARK_INTENT slot collision on promotion mark_intent_id=%s slot=%s — "
                    "skipping promotion, an assignment already occupies this slot",
                    intent["id"], slot,
                )
                still_unresolved.append(m)
                continue
            promoted = {
                **m,
                "resolved_source_message_id": intent["resolved_source_message_id"],
                "quoted_thumbnail_hash": intent.get("resolved_source_thumbnail_hash") or m.get("quoted_thumbnail_hash"),
                "source_media_type": intent.get("resolved_source_media_type") or m.get("source_media_type"),
                "album_tile_index": intent.get("resolved_album_tile_index") if intent.get("resolved_album_tile_index") is not None else m.get("album_tile_index"),
                "is_album_tile": intent.get("resolved_is_album_tile") if intent.get("resolved_is_album_tile") is not None else m.get("is_album_tile"),
                "mark_intent_id": intent["id"],
            }
            locked_assignments.append(promoted)
            used_slots.add(slot)
            logger.info(
                "MARK_INTENT reused prior lock for a candidate unresolved this scan "
                "mark_intent_id=%s resolved_source_message_id=%s",
                intent["id"], intent["resolved_source_message_id"],
            )
        else:
            await record_unresolved_attempt(intent["id"], worker_id=worker_id)
            still_unresolved.append(m)

    upgraded_ok = outcome.ok
    if (
        not outcome.ok and not outcome.batch_failures and not outcome.ambiguous
        and not still_unresolved and locked_assignments
    ):
        upgraded_ok = True

    return replace(
        outcome,
        ok=upgraded_ok,
        assignments=locked_assignments,
        unresolved=still_unresolved,
    )


async def get_by_ids(mark_intent_ids: List[str]) -> List[Dict[str, Any]]:
    """Action-scoped lookup (2026-09-21 architecture review) — the SAFE
    replacement for the removed `get_ready_assignments(talent_id,
    project_id)` shortcut, which was proven unsafe: a bare (talent_id,
    project_id) read cannot distinguish a resolved-but-superseded
    MarkIntent from a still-current one, because a brand-new re-MARK on
    WhatsApp is invisible to mark_intents until SOME scan's raw
    candidates actually include it — there is categorically no
    "as-of-now this is still the latest mark" signal available from a
    historical database read alone.

    The correct identity boundary is the ACTION, not the (talent,
    project) pair — see submission_action_queue.py. An action explicitly
    records which mark_intent_ids belong to it, populated ONLY from that
    action's own live scan results (mark_intent.observe_candidates /
    enrich_outcome_with_mark_intents, called on THIS action's freshest
    scan, exactly as before — completely unchanged). This function does
    nothing more than fetch those SPECIFIC, already-decided documents by
    id — it performs no scan, no talent/project-wide query, no slot
    matching, and cannot select between competing candidates; the
    caller already knows exactly which ids it wants."""
    if not mark_intent_ids:
        return []
    docs = await db[MARK_INTENTS_COLLECTION].find(
        {"id": {"$in": list(mark_intent_ids)}},
    ).to_list(len(mark_intent_ids))
    by_id = {d["id"]: d for d in docs}
    return [by_id[i] for i in mark_intent_ids if i in by_id]
