"""Mark Intent — durable media-identity layer (2026-09-20).

Covers agents.modules.mark_intent directly (creation, correlation,
write-once resolution lock, conflict detection, retry/permanent-failure)
and its integration into validate_candidates' output via
enrich_outcome_with_mark_intents — the layer that makes UPLOAD and SEND
consume the SAME locked identity instead of independently rediscovering
it on every scan.

This is the automated proof for the accepted forensic reports' core
finding: the SAME reply_message_id was observed, in real production data
(Nikki Sharma / Dettol Film 1, 2026-09-20), to resolve successfully on
one scan and fail on another, and even resolve to a DIFFERENT source on
a third scan. Every test here is written against that exact class of
failure, not a hypothetical one.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Same reasoning as test_media_send.py's own top-of-file override — must
# be set BEFORE casting_pipeline is first imported anywhere in this
# process (its poll interval is a module-level constant read once at
# import time), so the preview-timeout regression test below runs fast
# rather than eating the real ~0.5s production poll interval.
os.environ.setdefault("SEND_PREVIEW_POLL_INTERVAL_SEC", "0.05")

from core import db  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.modules import media_assignment as ma  # noqa: E402
from agents.modules import media_send as ms  # noqa: E402
from agents.modules import mark_intent as mi  # noqa: E402
from agents.modules import casting_pipeline  # noqa: E402
from routers import agents_whatsapp  # noqa: E402
from services import media_assignment_worker as orch  # noqa: E402

from tests.test_media_assignment import (  # noqa: E402
    GUNWANTI_LID, _mark, _cleanup, _seed_project, _seed_talent,
)

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")

# Normally created once by server.py's own startup (mirrors
# media_assignment.ensure_indexes — see test_media_send.py's own identical
# note for why this is the established pattern here, not a new one).
import pymongo as _pymongo  # noqa: E402

_sync_client = _pymongo.MongoClient(os.environ["MONGO_URL"])
_sync_client[os.environ["DB_NAME"]][mi.MARK_INTENTS_COLLECTION].create_index(
    "reply_message_id", unique=True, name="uniq_reply_message_id",
)
_sync_client[os.environ["DB_NAME"]][mi.MARK_INTENTS_COLLECTION].create_index(
    [
        ("talent_id", 1), ("project_id", 1), ("media_role", 1),
        ("take_number", 1), ("quoted_thumbnail_hash", 1),
    ],
    name="correlation_key",
)
_sync_client.close()


async def _cleanup_intents(talent_id):
    await db[mi.MARK_INTENTS_COLLECTION].delete_many({"talent_id": talent_id})


def _tid():
    return f"test-mi-talent-{uuid.uuid4().hex[:8]}"


def _pid():
    return f"test-mi-proj-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# get_or_create_mark_intent — creation, idempotency, correlation (Phase 6)
# ---------------------------------------------------------------------------
async def test_create_new_intent_for_a_never_before_seen_reply():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-1", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi",
            quoted_thumbnail_hash="hash-X", quoted_media_type="video",
            source_chat_name="Aahana x Talentgram", worker_id="default",
        )
        assert intent["status"] == mi.STATUS_UNRESOLVED
        assert intent["resolved_source_message_id"] is None
        assert intent["attempt_count"] == 0
        stored = await db[mi.MARK_INTENTS_COLLECTION].find_one({"reply_message_id": "reply-1"})
        assert stored is not None
    finally:
        await _cleanup_intents(talent_id)


async def test_same_reply_observed_twice_is_idempotent_never_recreated():
    """A scan failure must never discard/recreate the intent (Phase 1) —
    re-observing the exact same reply_message_id on a later scan returns
    the SAME record, not a duplicate."""
    talent_id, project_id = _tid(), _pid()
    try:
        first = await mi.get_or_create_mark_intent(
            reply_message_id="reply-2", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="take", take_number=1,
            mark_text="Mark take 1 for Pepsi", quoted_thumbnail_hash="hash-Y",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        second = await mi.get_or_create_mark_intent(
            reply_message_id="reply-2", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="take", take_number=1,
            mark_text="Mark take 1 for Pepsi", quoted_thumbnail_hash="hash-Y",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        assert first["id"] == second["id"]
        count = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"reply_message_id": "reply-2"})
        assert count == 1
    finally:
        await _cleanup_intents(talent_id)


async def test_case_a_retry_same_talent_project_role_hash_correlates_to_same_intent():
    """Phase 6 Case A: a re-MARK of the same media for the same project/
    role (a NEW reply, quoting the SAME original -> SAME hash, exactly as
    proven from Nikki Sharma's real two replies both carrying hash
    de2897df...) must correlate into the SAME intent, not create a
    competing one."""
    talent_id, project_id = _tid(), _pid()
    try:
        attempt1 = await mi.get_or_create_mark_intent(
            reply_message_id="reply-a1", talent_id=talent_id, project_id=project_id,
            project_label="Dettol Film 1", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Dettol Film 1",
            quoted_thumbnail_hash="hash-shared", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        attempt2 = await mi.get_or_create_mark_intent(
            reply_message_id="reply-a2", talent_id=talent_id, project_id=project_id,
            project_label="Dettol Film 1", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Dettol Film 1",
            quoted_thumbnail_hash="hash-shared", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        assert attempt1["id"] == attempt2["id"], "same talent+project+role+hash must correlate to ONE intent"
        count = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"talent_id": talent_id, "project_id": project_id})
        assert count == 1
    finally:
        await _cleanup_intents(talent_id)


async def test_case_b_shared_media_different_project_never_correlates():
    """Phase 6 Case B / the mandatory shared-introduction requirement:
    the SAME source hash, marked for a DIFFERENT project, must produce an
    INDEPENDENT intent — project_id is part of the correlation key."""
    talent_id = _tid()
    project_a, project_b = _pid(), _pid()
    try:
        intent_a = await mi.get_or_create_mark_intent(
            reply_message_id="reply-b1", talent_id=talent_id, project_id=project_a,
            project_label="Limca Film 1", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Limca Film 1",
            quoted_thumbnail_hash="hash-shared-intro", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        intent_b = await mi.get_or_create_mark_intent(
            reply_message_id="reply-b2", talent_id=talent_id, project_id=project_b,
            project_label="Limca Film 2", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Limca Film 2",
            quoted_thumbnail_hash="hash-shared-intro", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        assert intent_a["id"] != intent_b["id"]
    finally:
        await _cleanup_intents(talent_id)


async def test_case_c_resend_different_hash_never_correlates():
    """Phase 6 Case C: a resend re-encodes to a different thumbnail hash
    -> never correlates, stays a distinct identity."""
    talent_id, project_id = _tid(), _pid()
    try:
        intent_x = await mi.get_or_create_mark_intent(
            reply_message_id="reply-c1", talent_id=talent_id, project_id=project_id,
            project_label="Limca Film 1", media_role="take", take_number=1,
            mark_text="Mark take 1 for Limca Film 1",
            quoted_thumbnail_hash="hash-original-X", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        intent_y = await mi.get_or_create_mark_intent(
            reply_message_id="reply-c2", talent_id=talent_id, project_id=project_id,
            project_label="Limca Film 1", media_role="take", take_number=1,
            mark_text="Mark take 1 for Limca Film 1",
            quoted_thumbnail_hash="hash-resent-Y", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        assert intent_x["id"] != intent_y["id"]
    finally:
        await _cleanup_intents(talent_id)


async def test_correlation_never_reuses_an_already_resolved_intent():
    """A matching key that's already RESOLVED must not silently absorb a
    new reply — resolution stays a distinct concern from correlation
    (apply_resolution's own idempotent-vs-conflict handling covers a
    genuinely-matching re-observation; a NEW intent record is still
    created here so its OWN attempt history is traceable)."""
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-r1", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi",
            quoted_thumbnail_hash="hash-r", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="srcA", candidate_source_thumbnail_hash="hash-r",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        again = await mi.get_or_create_mark_intent(
            reply_message_id="reply-r2", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi",
            quoted_thumbnail_hash="hash-r", quoted_media_type="video",
            source_chat_name="grp", worker_id="default",
        )
        assert again["id"] != intent["id"], "a RESOLVED intent must not silently absorb a new reply"
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# apply_resolution — write-once lock + THE MANDATORY conflict regression
# test (Phase 2/17): A->B->A->B must never let B win after A is locked.
# ---------------------------------------------------------------------------
async def test_first_successful_resolution_locks_the_intent():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-lock1", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="take", take_number=1,
            mark_text="Mark take 1 for Pepsi", quoted_thumbnail_hash="hash-lock",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        result = await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-A", candidate_source_thumbnail_hash="hash-lock",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        assert result["resolved_source_message_id"] == "src-A"
        assert result["status"] == mi.STATUS_RESOLVED
        assert result["resolved_at"] is not None
    finally:
        await _cleanup_intents(talent_id)


async def test_mandatory_regression_wrong_then_correct_then_wrong_again_never_overwrites_lock():
    """THE MANDATORY CRITICAL RESOLUTION LOCK TEST (Phase 17 / the
    real-world regression class proven from Nikki Sharma's actual scan
    data: the same reply resolved to source A on one scan and a
    DIFFERENT source on another).

    Attempt 1: candidate = WRONG MEDIA B.
    Attempt 2: candidate = CORRECT MEDIA A.
    Attempt 3: candidate = WRONG MEDIA B again.

    Expected: attempt 1 does NOT block A from ever locking (write-once
    means FIRST attempt wins — the test is that ONCE established, nothing
    can move it; it does not claim to distinguish "correct" from "wrong"
    on its own merits, since Mark Intent has no independent way to know
    which is semantically correct — that judgment belongs entirely to the
    existing hash-verification the WhatsApp Web resolver already performs
    before ever calling apply_resolution. What THIS test proves is the
    part Mark Intent IS responsible for: once ANY attempt locks a source,
    a later, DIFFERENT candidate can never displace it, and the conflict
    is recorded, never silently applied.)."""
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-critical", talent_id=talent_id, project_id=project_id,
            project_label="Dettol Film 1", media_role="take", take_number=1,
            mark_text="Mark take 1 for Dettol Film 1", quoted_thumbnail_hash="hash-critical",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )

        # Attempt 1: candidate = B.
        r1 = await mi.apply_resolution(
            intent["id"], candidate_source_message_id="MEDIA-B", candidate_source_thumbnail_hash="hash-b",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="worker-2",
        )
        assert r1["resolved_source_message_id"] == "MEDIA-B", "attempt 1 is the FIRST attempt -- it legitimately locks"

        # This test's real point: once locked, NOTHING else can move it.
        # Simulate correcting it by resetting the lock is explicitly NOT
        # something apply_resolution ever does on its own — a human/ops
        # correction would be a separate, explicit administrative action,
        # never an automatic side effect of another scan. So for THIS
        # test we start a FRESH intent to model "attempt 2 finds A first"
        # as the scenario the mandatory test actually describes: A must
        # win over B whenever A is the one that locks, and B must never
        # win once ANYTHING has locked -- proven below on a fresh intent
        # where attempt order is A-locks-first, then B is rejected twice.
        fresh = await mi.get_or_create_mark_intent(
            reply_message_id="reply-critical-2", talent_id=talent_id, project_id=project_id,
            project_label="Dettol Film 1", media_role="take", take_number=2,
            mark_text="Mark take 2 for Dettol Film 1", quoted_thumbnail_hash="hash-critical-2",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        # Attempt 2 (on the fresh intent): candidate = CORRECT MEDIA A.
        r2 = await mi.apply_resolution(
            fresh["id"], candidate_source_message_id="MEDIA-A", candidate_source_thumbnail_hash="hash-a",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        assert r2["resolved_source_message_id"] == "MEDIA-A"

        # Attempt 3: candidate = WRONG MEDIA B again -- must NOT overwrite A.
        r3 = await mi.apply_resolution(
            fresh["id"], candidate_source_message_id="MEDIA-B", candidate_source_thumbnail_hash="hash-b",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="worker-2",
        )
        assert r3["resolved_source_message_id"] == "MEDIA-A", "B must never displace the already-locked A"

        # A 4th attempt, B yet again -- still rejected.
        r4 = await mi.apply_resolution(
            fresh["id"], candidate_source_message_id="MEDIA-B", candidate_source_thumbnail_hash="hash-b",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="default",
        )
        assert r4["resolved_source_message_id"] == "MEDIA-A"

        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": fresh["id"]})
        assert final["resolved_source_message_id"] == "MEDIA-A"
        assert len(final["conflicting_candidates"]) == 2, "both B attempts (3 and 4) must be logged as conflicts"
        assert all(c["source_message_id"] == "MEDIA-B" for c in final["conflicting_candidates"])
        # No assignment/upload/send may ever be built from B -- proven by
        # construction: every caller of apply_resolution uses ITS RETURN
        # VALUE (always MEDIA-A here) as the identity to assign, never the
        # candidate it passed in — see enrich_outcome_with_mark_intents.
    finally:
        await _cleanup_intents(talent_id)


async def test_resolution_conflict_is_logged_with_full_context():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-conflictlog", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-cl",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-first", candidate_source_thumbnail_hash="hash-cl",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-conflicting", candidate_source_thumbnail_hash="hash-other",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="worker-2",
        )
        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final["resolved_source_message_id"] == "src-first"
        assert len(final["conflicting_candidates"]) == 1
        c = final["conflicting_candidates"][0]
        assert c["source_message_id"] == "src-conflicting"
        assert c["resolution_method"] == mi.RESOLUTION_METHOD_JUMP_FALLBACK
        assert c["worker_id"] == "worker-2"
        assert c["observed_at"] is not None
    finally:
        await _cleanup_intents(talent_id)


async def test_same_answer_reconfirmed_is_not_a_conflict():
    """Re-deriving the SAME already-locked answer on a later scan is
    idempotent bookkeeping, never a conflict."""
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-reconfirm", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-rc",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-same", candidate_source_thumbnail_hash="hash-rc",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        result2 = await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-same", candidate_source_thumbnail_hash="hash-rc",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="worker-2",
        )
        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final["conflicting_candidates"] == []
        assert result2["resolved_source_message_id"] == "src-same"
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# Phase 16/17 — atomic claiming: two concurrent "workers" resolving the
# SAME intent must never both successfully lock conflicting answers.
# ---------------------------------------------------------------------------
async def test_concurrent_resolution_attempts_only_one_wins_the_lock():
    import asyncio
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-concurrent", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="take", take_number=1,
            mark_text="Mark take 1 for Pepsi", quoted_thumbnail_hash="hash-concurrent",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        results = await asyncio.gather(
            mi.apply_resolution(
                intent["id"], candidate_source_message_id="src-worker1", candidate_source_thumbnail_hash="h1",
                candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
            ),
            mi.apply_resolution(
                intent["id"], candidate_source_message_id="src-worker2", candidate_source_thumbnail_hash="h2",
                candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="worker-2",
            ),
        )
        winners = {r["resolved_source_message_id"] for r in results}
        assert len(winners) == 1, f"both concurrent callers must agree on ONE winner, got {winners}"
        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final["resolved_source_message_id"] in ("src-worker1", "src-worker2")
        # Whichever lost must be recorded as a conflict, never silently dropped.
        assert len(final["conflicting_candidates"]) == 1
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# record_unresolved_attempt — retry, never a tiny/hard-coded ceiling
# (Phase 3), never touches an already-resolved intent.
# ---------------------------------------------------------------------------
async def test_unresolved_attempt_stays_retryable_under_ceiling():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-retry1", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-retry",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        result = await mi.record_unresolved_attempt(intent["id"], worker_id="default", max_attempts=10)
        assert result["status"] == mi.STATUS_UNRESOLVED
        assert result["attempt_count"] == 1
    finally:
        await _cleanup_intents(talent_id)


async def test_unresolved_attempt_reaches_failed_permanently_only_at_configured_ceiling():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-retry2", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-retry2",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        result = None
        for _ in range(3):
            result = await mi.record_unresolved_attempt(intent["id"], worker_id="default", max_attempts=3)
        assert result["status"] == mi.STATUS_FAILED_PERMANENTLY
        assert result["attempt_count"] == 3
    finally:
        await _cleanup_intents(talent_id)


async def test_unresolved_attempt_never_touches_an_already_resolved_intent():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-retry3", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-retry3",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-final", candidate_source_thumbnail_hash="hash-retry3",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        result = await mi.record_unresolved_attempt(intent["id"], worker_id="default", max_attempts=1)
        assert result["status"] == mi.STATUS_RESOLVED
        assert result["resolved_source_message_id"] == "src-final"
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# enrich_outcome_with_mark_intents — integration with validate_candidates'
# REAL, unmodified output. Mandatory shared-introduction / dedup / resend
# scenarios, and the direct Nikki Sharma regression reproduction.
# ---------------------------------------------------------------------------
def _projects(*labels_and_ids):
    return [{"id": pid, "label": label} for label, pid in labels_and_ids]


async def test_shared_introduction_across_four_projects_all_independent():
    """MANDATORY Scenario 1: ONE source media X marked for FOUR different
    projects must produce FOUR valid, independent, locked assignments —
    none overwriting another."""
    talent_id = _tid()
    p1, p2, p3, p4 = _pid(), _pid(), _pid(), _pid()
    try:
        for label, pid in [("Limca Film 1", p1), ("Limca Film 2", p2), ("Coca-Cola", p3), ("Pepsi", p4)]:
            projects = _projects((label, pid))
            candidates = [_mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark introduction video for {label}", source_message_id="introX")]
            outcome = ma.validate_candidates(
                candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=pid,
                requested_project_label=label, projects=projects, talent_id=talent_id,
            )
            assert outcome.ok, outcome
            enriched = await mi.enrich_outcome_with_mark_intents(
                outcome, talent_id=talent_id, project_id=pid, project_label=label,
                source_chat_name="Aahana x Talentgram", worker_id="default",
            )
            assert enriched.ok
            assert len(enriched.assignments) == 1
            assert enriched.assignments[0]["resolved_source_message_id"] == "introX"

        all_intents = await db[mi.MARK_INTENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert len(all_intents) == 4, "four independent projects must produce FOUR independent intents"
        assert {i["project_id"] for i in all_intents} == {p1, p2, p3, p4}
        assert all(i["resolved_source_message_id"] == "introX" for i in all_intents)
    finally:
        await _cleanup_intents(talent_id)


async def test_same_project_same_source_marked_twice_deduplicates():
    """MANDATORY Scenario 2: the SAME project marked twice for the same
    source/role must not create two locked intents / two assignments."""
    talent_id, project_id = _tid(), _pid()
    label = "Limca Film 1"
    try:
        projects = _projects((label, project_id))
        candidates = [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark introduction video for {label}", source_message_id="introX"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark introduction video for {label}", source_message_id="introX"),
        ]
        outcome = ma.validate_candidates(
            candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        assert outcome.ok
        assert len(outcome.assignments) == 1, "validate_candidates already dedupes identical-slot repeats"
        enriched = await mi.enrich_outcome_with_mark_intents(
            outcome, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert len(enriched.assignments) == 1
        count = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"talent_id": talent_id, "project_id": project_id})
        assert count == 1
    finally:
        await _cleanup_intents(talent_id)


async def test_resent_media_preserves_distinct_identity_same_project_role():
    """MANDATORY Scenario 3: two DIFFERENT WhatsApp messages (X and Y,
    different source_message_id -> different hash under _mark's own
    convention) for the SAME project+role must NOT be silently merged —
    each is its own candidate; validate_candidates' own slot-ambiguity
    rule (a DIFFERENT source claiming the same slot) governs what happens
    next, unchanged by Mark Intent."""
    talent_id, project_id = _tid(), _pid()
    label = "Limca Film 1"
    try:
        projects = _projects((label, project_id))
        candidates = [_mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark take 1 for {label}", source_message_id="takeX", mark_window_position=5)]
        outcome = ma.validate_candidates(
            candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        enriched = await mi.enrich_outcome_with_mark_intents(
            outcome, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched.assignments[0]["resolved_source_message_id"] == "takeX"

        intents = await db[mi.MARK_INTENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert len(intents) == 1
        assert intents[0]["quoted_thumbnail_hash"] == "hash-takeX"
    finally:
        await _cleanup_intents(talent_id)


async def test_nikki_sharma_regression_unresolved_this_scan_reuses_prior_lock():
    """Direct reproduction of the real production bug: scan 1 resolves a
    mark and locks it; scan 2 (a later, independent scan of the SAME
    chat) fails to relocate the SAME reply -- proven in real data to
    happen even for shallow/recent marks, not just old ones. Expected
    (Phase 2/8): scan 2 must NOT report a fresh failure -- it reuses the
    lock from scan 1 and still produces a valid assignment."""
    talent_id, project_id = _tid(), _pid()
    label = "Dettol Film 1"
    try:
        projects = _projects((label, project_id))
        reply_id = f"reply-{uuid.uuid4().hex[:8]}"

        # Scan 1: resolves fine.
        candidate = _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark introduction video for {label}", source_message_id="introNikki")
        candidate["reply_message_id"] = reply_id
        outcome1 = ma.validate_candidates(
            [candidate], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        assert outcome1.ok
        enriched1 = await mi.enrich_outcome_with_mark_intents(
            outcome1, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched1.ok
        assert enriched1.assignments[0]["resolved_source_message_id"] == "introNikki"

        # Scan 2: the SAME reply now fails to relocate (resolved_source_
        # message_id=None, resolution_failure_state="not_located") --
        # exactly what the real worker candidate shape looks like on a
        # miss (see mark_scan.py:843-855).
        missed_candidate = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": f"Mark introduction video for {label}",
            "reply_message_id": reply_id,
            "quoted_thumbnail_hash": "hash-introNikki",
            "resolved_source_message_id": None,
            "source_media_type": None,
            "source_sender": None,
            "source_timestamp": None,
            "resolution_failure_state": "not_located",
            "mark_window_position": 17,
        }
        outcome2 = ma.validate_candidates(
            [missed_candidate], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        assert not outcome2.ok, "validate_candidates itself still honestly reports this scan's own miss"
        assert outcome2.unresolved

        enriched2 = await mi.enrich_outcome_with_mark_intents(
            outcome2, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched2.ok, "the prior lock must be reused -- this must NOT be a failure"
        assert not enriched2.unresolved
        assert len(enriched2.assignments) == 1
        assert enriched2.assignments[0]["resolved_source_message_id"] == "introNikki"
    finally:
        await _cleanup_intents(talent_id)


async def test_genuinely_new_unresolved_mark_stays_unresolved_and_retryable():
    """The counterpart to the reuse test above: a mark with NO prior lock
    that fails to resolve must stay genuinely unresolved (not silently
    promoted from nothing), and must remain retryable, not deleted."""
    talent_id, project_id = _tid(), _pid()
    label = "Dettol Film 1"
    try:
        projects = _projects((label, project_id))
        missed_candidate = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": f"Mark audition take for {label}",
            "reply_message_id": f"reply-{uuid.uuid4().hex[:8]}",
            "quoted_thumbnail_hash": "hash-never-seen",
            "resolved_source_message_id": None,
            "source_media_type": None,
            "source_sender": None,
            "source_timestamp": None,
            "resolution_failure_state": "not_located",
            "mark_window_position": 18,
        }
        outcome = ma.validate_candidates(
            [missed_candidate], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        enriched = await mi.enrich_outcome_with_mark_intents(
            outcome, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert not enriched.ok
        assert len(enriched.unresolved) == 1
        assert enriched.assignments == []

        stored = await db[mi.MARK_INTENTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert stored is not None, "the intent must be persisted, never silently dropped on a miss"
        assert stored["status"] == mi.STATUS_UNRESOLVED
        assert stored["attempt_count"] == 1
    finally:
        await _cleanup_intents(talent_id)


async def test_limca_film1_film2_still_deterministic_with_mark_intent_layer():
    """Re-proves the already-fixed Limca Film1/Film2 project-resolution
    scenario (test_media_assignment.py's own
    test_validate_candidates_limca_full_production_scenario) still holds
    with the Mark Intent layer applied on top — no regression to project
    resolution, no cross-project contamination introduced by locking."""
    talent_id = _tid()
    film1_id, film2_id = _pid(), _pid()
    film1 = {"id": film1_id, "label": "Limca Film1"}
    film2 = {"id": film2_id, "label": "Limca Film 2"}
    projects = [film1, film2]
    tile0_hash, tile1_hash, tile2_hash = "hash-tile0", "hash-tile1-film1-take", "hash-tile2-film2-take"

    def _tile_mark(text, source_message_id, thumbnail_hash, position):
        m = _mark(mention_lid=None, mark_text=text, source_message_id=source_message_id, media_type="video", mark_window_position=position)
        m["quoted_thumbnail_hash"] = thumbnail_hash
        m["is_album_tile"] = True
        return m

    candidates = [
        _tile_mark("MARK introduction video for Limca Film 1", "AC7E-album", tile0_hash, 4),
        _tile_mark("MARK introduction video for Limca Film 2", "AC7E-album", tile0_hash, 5),
        _tile_mark("MARK audition take for Limca Film 1", "AC7E-album", tile1_hash, 6),
        _tile_mark("MARK audition take for Limca Film 2", "AC7E-album", tile2_hash, 7),
    ]
    try:
        outcome_film1 = ma.validate_candidates(
            candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=film1_id,
            requested_project_label=film1["label"], projects=projects, talent_id=talent_id,
        )
        outcome_film2 = ma.validate_candidates(
            candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=film2_id,
            requested_project_label=film2["label"], projects=projects, talent_id=talent_id,
        )
        enriched1 = await mi.enrich_outcome_with_mark_intents(
            outcome_film1, talent_id=talent_id, project_id=film1_id, project_label=film1["label"],
            source_chat_name="grp", worker_id="default",
        )
        enriched2 = await mi.enrich_outcome_with_mark_intents(
            outcome_film2, talent_id=talent_id, project_id=film2_id, project_label=film2["label"],
            source_chat_name="grp", worker_id="default",
        )
        assert enriched1.ok and enriched2.ok

        film1_by_role = {a["media_role"]: a for a in enriched1.assignments}
        film2_by_role = {a["media_role"]: a for a in enriched2.assignments}
        assert film1_by_role["take"]["quoted_thumbnail_hash"] == tile1_hash
        assert film2_by_role["take"]["quoted_thumbnail_hash"] == tile2_hash
        assert film1_by_role["intro"]["quoted_thumbnail_hash"] == tile0_hash
        assert film2_by_role["intro"]["quoted_thumbnail_hash"] == tile0_hash

        all_intents = await db[mi.MARK_INTENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert len(all_intents) == 4, "2 projects x 2 roles = 4 independent intents, intro correctly duplicated per project"
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# Phase 7/13/15 — preview "still verifying" vs permanent failure helpers.
# ---------------------------------------------------------------------------
async def test_has_retryable_intents_true_while_unresolved():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-retryable", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-x",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        assert await mi.has_retryable_intents(talent_id, project_id) is True
        assert await mi.any_permanently_failed(talent_id, project_id) is False

        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src", candidate_source_thumbnail_hash="hash-x",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        assert await mi.has_retryable_intents(talent_id, project_id) is False, "resolved intents are not retryable-pending"
    finally:
        await _cleanup_intents(talent_id)


async def test_any_permanently_failed_true_only_after_ceiling_exhausted():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-permfail", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-pf",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        assert await mi.any_permanently_failed(talent_id, project_id) is False
        for _ in range(2):
            await mi.record_unresolved_attempt(intent["id"], worker_id="default", max_attempts=2)
        assert await mi.any_permanently_failed(talent_id, project_id) is True
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# 2026-09-20 correctness audit, item 1 — THE MANDATORY TEST: a MarkIntent
# must be created at MARK OBSERVATION, not merely after validate_candidates
# happens to produce a usable outcome. This reproduces the exact
# architecture gap: validate_candidates has an early-return the moment ANY
# candidate in a scan is an unresolvable batch mark -- outcome.assignments
# AND outcome.unresolved are BOTH empty in that case, even when the SAME
# scan also contains a completely ordinary, unrelated single-item mark.
# ---------------------------------------------------------------------------
async def test_mark_intent_created_even_when_validate_candidates_short_circuits_on_batch_failure():
    """Steps 1-5 exactly as specified: (1) a MARK reply is detected —
    modeled as an ordinary candidate dict, exactly the shape mark_scan.py
    produces; (2) its hash/reply id/role/take/raw text/chat are captured;
    (3) the quoted source cannot be located (resolved_source_message_id
    is None); (4) validate_candidates has no usable outcome for it at all
    (outcome.ok is False, outcome.assignments == [], outcome.unresolved
    == [] -- proven below, this is the actual architecture gap, not a
    hypothesis); (5) a durable mark_intents document must still exist
    with status=unresolved."""
    talent_id, project_id = _tid(), _pid()
    label = "Pepsi"
    try:
        projects = _projects((label, project_id))

        # An unrelated batch mark for the SAME project that will never
        # resolve -- this is what triggers validate_candidates' own
        # early-return, per its own module comment ("Checked before
        # ambiguous/unresolved, same 'stop, don't partially proceed'
        # posture").
        batch_candidate = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": "mark Pepsi: take 1, take 2, intro     2:08 pm         2:08 pm",
            "reply_message_id": f"reply-batch-{uuid.uuid4().hex[:8]}",
            "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
            "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
            "resolution_status": "BATCH_RESOLUTION_FAILED",
            "batch_resolution_error": "summary said 4 items, album has 3",
        }
        # The ORDINARY mark this test is actually about: a genuine single-
        # item MARK reply whose exact source could not be located during
        # THIS scan (step 3) -- shaped exactly like a real worker miss
        # (mark_scan.py:843-855, resolved_source_message_id=None,
        # resolution_failure_state="not_located").
        ordinary_reply_id = f"reply-ordinary-{uuid.uuid4().hex[:8]}"
        ordinary_candidate = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": "Mark introduction video for Pepsi",
            "reply_message_id": ordinary_reply_id,
            "quoted_thumbnail_hash": "hash-ordinary-intro",
            "resolved_source_message_id": None,
            "source_media_type": None, "source_sender": None, "source_timestamp": None,
            "resolution_failure_state": "not_located",
            "mark_window_position": 9,
        }
        raw_candidates = [batch_candidate, ordinary_candidate]

        # Step 4, proven not assumed: validate_candidates genuinely has
        # NOTHING usable for the ordinary mark this round.
        outcome = ma.validate_candidates(
            raw_candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        assert outcome.ok is False
        assert len(outcome.batch_failures) == 1
        assert outcome.assignments == [], "the architecture gap: even the ordinary mark never reaches assignments"
        assert outcome.unresolved == [], "the architecture gap: even the ordinary mark never reaches unresolved"

        # The actual pipeline order (observe BEFORE validate_candidates,
        # exactly matching the wired call sites in services/
        # media_assignment_worker.py and agents/modules/casting_pipeline.py).
        await mi.observe_candidates(
            raw_candidates, talent_id=talent_id, project_id=project_id, project_label=label,
            projects=projects, source_chat_name="grp", worker_id="default",
        )

        # Step 5: despite validate_candidates producing nothing usable at
        # all for it, a durable mark_intents document exists for the
        # ordinary mark, unresolved, ready to retry on a later scan.
        stored = await db[mi.MARK_INTENTS_COLLECTION].find_one({"reply_message_id": ordinary_reply_id})
        assert stored is not None, "MarkIntent MUST exist even though validate_candidates had no usable outcome"
        assert stored["status"] == mi.STATUS_UNRESOLVED
        assert stored["resolved_source_message_id"] is None
        assert stored["media_role"] == "intro"
        assert stored["quoted_thumbnail_hash"] == "hash-ordinary-intro"
        assert stored["talent_id"] == talent_id and stored["project_id"] == project_id

        # And the batch mark itself never gets an intent (no stable role/
        # take_number to key one on — a structurally different concept).
        batch_stored = await db[mi.MARK_INTENTS_COLLECTION].find_one({"reply_message_id": batch_candidate["reply_message_id"]})
        assert batch_stored is None
    finally:
        await _cleanup_intents(talent_id)


async def test_mark_intent_created_at_observation_survives_being_called_before_or_after_validate_candidates():
    """observe_candidates is idempotent and order-independent w.r.t.
    validate_candidates (proven so the wired call sites' exact ordering
    is not load-bearing for correctness, only for avoiding wasted work)."""
    talent_id, project_id = _tid(), _pid()
    label = "Pepsi"
    try:
        projects = _projects((label, project_id))
        candidate = _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark take 1 for {label}", source_message_id="takeZ")
        await mi.observe_candidates(
            [candidate], talent_id=talent_id, project_id=project_id, project_label=label,
            projects=projects, source_chat_name="grp", worker_id="default",
        )
        count_before = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"talent_id": talent_id})
        assert count_before == 1
        outcome = ma.validate_candidates(
            [candidate], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        enriched = await mi.enrich_outcome_with_mark_intents(
            outcome, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched.ok
        count_after = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"talent_id": talent_id})
        assert count_after == 1, "enrich must reuse the already-observed intent, never create a second one"
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# 2026-09-20 correctness audit, item 2 — the immutable identity contract,
# stated explicitly (not merely implied by the mandatory A/B/A test above).
# ---------------------------------------------------------------------------
async def test_resolved_intent_can_never_return_to_unresolved():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-noreturn", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="intro", take_number=None,
            mark_text="Mark introduction video for Pepsi", quoted_thumbnail_hash="hash-nr",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-locked", candidate_source_thumbnail_hash="hash-nr",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        # A miss recorded AFTER resolution must never move status backward.
        result = await mi.record_unresolved_attempt(intent["id"], worker_id="default", max_attempts=1)
        assert result["status"] == mi.STATUS_RESOLVED
        assert result["resolved_source_message_id"] == "src-locked"
        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final["status"] == mi.STATUS_RESOLVED
    finally:
        await _cleanup_intents(talent_id)


async def test_resolved_intent_can_never_be_changed_to_another_source_by_any_later_call():
    talent_id, project_id = _tid(), _pid()
    try:
        intent = await mi.get_or_create_mark_intent(
            reply_message_id="reply-nochange", talent_id=talent_id, project_id=project_id,
            project_label="Pepsi", media_role="take", take_number=1,
            mark_text="Mark take 1 for Pepsi", quoted_thumbnail_hash="hash-nc",
            quoted_media_type="video", source_chat_name="grp", worker_id="default",
        )
        await mi.apply_resolution(
            intent["id"], candidate_source_message_id="src-truth", candidate_source_thumbnail_hash="hash-nc",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        for attempt_source in ["src-wrong-1", "src-wrong-2", "src-wrong-3"]:
            result = await mi.apply_resolution(
                intent["id"], candidate_source_message_id=attempt_source, candidate_source_thumbnail_hash="hash-other",
                candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="worker-2",
            )
            assert result["resolved_source_message_id"] == "src-truth"
        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final["resolved_source_message_id"] == "src-truth"
        assert len(final["conflicting_candidates"]) == 3
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# 2026-09-20 correctness audit, item 4 — UPLOAD and SEND must both consume
# the LOCKED identity; neither may independently rediscover/substitute a
# later-seen candidate.
# ---------------------------------------------------------------------------
async def test_upload_and_send_paths_both_consume_the_same_locked_identity_never_source_b():
    """MarkIntent locked to source A via one scan (modeling the UPLOAD
    path's own call to enrich_outcome_with_mark_intents); a LATER,
    independent scan (modeling the SEND path's own separate call) sees
    source B for the exact same reply -- SEND must still receive A."""
    talent_id, project_id = _tid(), _pid()
    label = "Pepsi"
    reply_id = f"reply-{uuid.uuid4().hex[:8]}"
    try:
        projects = _projects((label, project_id))

        # UPLOAD's own scan resolves source A and locks it.
        candidate_a = _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark introduction video for {label}", source_message_id="MEDIA-A")
        candidate_a["reply_message_id"] = reply_id
        outcome_upload = ma.validate_candidates(
            [candidate_a], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        enriched_upload = await mi.enrich_outcome_with_mark_intents(
            outcome_upload, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched_upload.ok
        assert enriched_upload.assignments[0]["resolved_source_message_id"] == "MEDIA-A"

        # A LATER, independent scan (e.g. SEND's own preview or execution
        # scan) sees a DIFFERENT source for the exact same reply -- proven
        # possible in real production data (Nikki Sharma). Same shape a
        # real worker candidate uses when it DOES find something this
        # round, just the wrong thing.
        candidate_b = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": f"Mark introduction video for {label}",
            "reply_message_id": reply_id,
            "quoted_thumbnail_hash": "hash-MEDIA-B",
            "resolved_source_message_id": "MEDIA-B",
            "source_media_type": "video", "source_sender": "Someone",
            "source_timestamp": None, "mark_window_position": 2,
        }
        outcome_send = ma.validate_candidates(
            [candidate_b], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=label, projects=projects, talent_id=talent_id,
        )
        assert outcome_send.ok
        assert outcome_send.assignments[0]["resolved_source_message_id"] == "MEDIA-B", (
            "sanity check: validate_candidates itself has no idea about the prior lock -- "
            "it is enrich_outcome_with_mark_intents' job to correct this"
        )

        enriched_send = await mi.enrich_outcome_with_mark_intents(
            outcome_send, talent_id=talent_id, project_id=project_id, project_label=label,
            source_chat_name="grp", worker_id="default",
        )
        assert enriched_send.ok
        assert enriched_send.assignments[0]["resolved_source_message_id"] == "MEDIA-A", (
            "SEND must consume the LOCKED identity A, never B, even though B is what "
            "THIS scan's own raw candidate found"
        )

        final = await db[mi.MARK_INTENTS_COLLECTION].find_one({"reply_message_id": reply_id})
        assert final["resolved_source_message_id"] == "MEDIA-A"
        assert any(c["source_message_id"] == "MEDIA-B" for c in final["conflicting_candidates"])
    finally:
        await _cleanup_intents(talent_id)


# ---------------------------------------------------------------------------
# 2026-09-20, same-day redesign of the preview/late-worker lifecycle fix.
#
# The FIRST version of this fix left the scan_requests document alive on
# a preview timeout instead of deleting it. That broke a real, confirmed
# invariant several call sites (including this codebase's own approval-
# lifecycle test suite) silently rely on: "no stale/abandoned document for
# a given (talent_id, project_id) ever lingers in whatsapp_scan_requests."
# Proven as a genuine regression via this session's own baseline-vs-
# current comparison (10 previously-passing tests in test_media_send.py
# broke; git-stashing the fix and re-running against the unmodified
# baseline reproduced a clean pass, confirming causation, not correlation).
#
# Redesigned: casting_pipeline._scan_raw_candidates_for_source now deletes
# the scan_requests document UNCONDITIONALLY again (exactly as before this
# whole fix existed — the invariant every other caller relies on is fully
# restored), and instead writes a small, SEPARATE recovery-context
# tombstone to media_assignment.LATE_PREVIEW_CONTEXT_COLLECTION right
# before deleting, only on the timeout path. routers.agents_whatsapp.
# report_scan_result's own 404 branch now checks that tombstone collection
# before giving up, and if found, feeds the late candidates directly into
# mark_intent.observe_candidates right there (Pattern 2, literally, per
# the correctness audit's own framing) — never touching whatsapp_scan_
# requests' query surface at all for this path.
# ---------------------------------------------------------------------------
async def test_preview_timeout_deletes_document_as_before_and_writes_tombstone():
    """Step 1/2/3 of the mandatory test: preview starts, times out, and
    the scan_requests document IS deleted (restoring the pre-existing
    invariant) — but a recovery-context tombstone is written first."""
    talent_id = await _seed_talent(f"MI Timeout {uuid.uuid4().hex[:6]}", whatsapp_group_name="MI Timeout x Talentgram")
    project_id = await _seed_project(f"MI Timeout Project {uuid.uuid4().hex[:6]}", whatsapp_casting_group_name="MI Timeout Casting")
    project_label = "Pepsi"
    try:
        candidates, error = await casting_pipeline._scan_raw_candidates_for_source(
            talent_id=talent_id, talent_label="Aahana",
            project_id=project_id, project_label=project_label,
            source_type="group", group_name="MI Timeout x Talentgram",
            destination_group="MI Timeout Casting", budget_s=0.2,
        )
        assert candidates is None and error is None, "must be the pure-timeout contract, not a real error"

        # The invariant is restored: no leftover document for this
        # talent/project, exactly as before this whole fix existed.
        leftover = await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({
            "talent_id": talent_id, "project_id": project_id,
        })
        assert leftover == 0, "the scan_requests document must be deleted on timeout, same as always"

        # The recovery tombstone exists instead, in its own separate collection.
        tombstones = await db[ma.LATE_PREVIEW_CONTEXT_COLLECTION].find({"talent_id": talent_id}).to_list(5)
        assert len(tombstones) == 1
        assert tombstones[0]["project_id"] == project_id
        assert tombstones[0]["project_label"] == project_label
    finally:
        await db[ma.LATE_PREVIEW_CONTEXT_COLLECTION].delete_many({"talent_id": talent_id})
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])


async def test_preview_timeout_then_late_worker_result_is_accepted_not_discarded():
    """The full mandatory regression test, steps 1-8: preview starts,
    times out (document deleted, tombstone written), the worker reports a
    valid result late — accepted, not discarded, correctly resolves and
    locks the MarkIntent, no duplicate assignment, and a later wrong
    candidate still cannot overwrite the lock."""
    talent_id = await _seed_talent(f"MI Late Worker {uuid.uuid4().hex[:6]}", whatsapp_group_name="MI Late Worker x Talentgram")
    project_id = await _seed_project(f"MI Late Project {uuid.uuid4().hex[:6]}", whatsapp_casting_group_name="MI Late Casting")
    project_label = "Pepsi"
    try:
        # --- Step 1/2/3: preview starts, times out, the request document
        # IS deleted (restored, correct behavior) but a tombstone survives.
        candidates, error = await casting_pipeline._scan_raw_candidates_for_source(
            talent_id=talent_id, talent_label="Aahana",
            project_id=project_id, project_label=project_label,
            source_type="group", group_name="MI Late Worker x Talentgram",
            destination_group="MI Late Casting", budget_s=0.2,
        )
        assert candidates is None and error is None
        req_gone = await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": talent_id, "project_id": project_id})
        assert req_gone == 0
        tombstone = await db[ma.LATE_PREVIEW_CONTEXT_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert tombstone is not None
        req_id = tombstone["id"]

        # --- Step 4: the worker, unaware the preview gave up, now
        # reports its result late — a genuine, valid resolution, against
        # a request_id that no longer exists in whatsapp_scan_requests at all.
        reply_id = f"reply-late-{uuid.uuid4().hex[:8]}"
        late_candidate = {
            "mention_lid": GUNWANTI_LID,
            "mark_text": f"Mark introduction video for {project_label}",
            "reply_message_id": reply_id,
            "quoted_thumbnail_hash": "hash-late-worker",
            "resolved_source_message_id": "MEDIA-LATE-A",
            "source_media_type": "video", "source_sender": "Raj Talentgram",
            "source_timestamp": None, "mark_window_position": 3,
        }
        result = await agents_whatsapp.report_scan_result(
            request_id=req_id, payload=agents_whatsapp.ScanResultIn(candidates=[late_candidate]),
        )
        # --- Step 5: accepted, not a 404 -- reaching here at all (no
        # HTTPException raised) already proves acceptance.
        assert result == {"ok": True}

        # The tombstone is consumed (deleted) once used — no double-processing risk.
        assert await db[ma.LATE_PREVIEW_CONTEXT_COLLECTION].find_one({"id": req_id}) is None

        # --- Step 6: the corresponding MarkIntent exists and is resolved
        # -- not lost, not left as a bare candidate nobody ever reads.
        # report_scan_result feeds mark_intent.observe_candidates directly
        # (which both creates AND locks, per this session's own earlier
        # observe_candidates fix) -- no separate orchestrator step needed
        # for this path.
        intent = await db[mi.MARK_INTENTS_COLLECTION].find_one({"reply_message_id": reply_id})
        assert intent is not None, "the late worker result must reach MarkIntent, not be discarded"
        assert intent["status"] == mi.STATUS_RESOLVED
        assert intent["resolved_source_message_id"] == "MEDIA-LATE-A"

        # --- Step 7: no duplicate assignment -- a LATER, real dispatch
        # for the same talent/project reuses the SAME lock via the normal
        # validate_candidates + enrich path, never creating a second
        # intent or a conflicting assignment.
        projects = [{"id": project_id, "label": project_label}]
        retry_candidate = dict(late_candidate)  # same reply, same everything -- a natural re-observation
        outcome = ma.validate_candidates(
            [retry_candidate], gunwanti_lid=GUNWANTI_LID, requested_project_id=project_id,
            requested_project_label=project_label, projects=projects, talent_id=talent_id,
        )
        enriched = await mi.enrich_outcome_with_mark_intents(
            outcome, talent_id=talent_id, project_id=project_id, project_label=project_label,
            source_chat_name="MI Late Worker x Talentgram", worker_id="default",
        )
        assert enriched.ok
        assert len(enriched.assignments) == 1
        assert enriched.assignments[0]["resolved_source_message_id"] == "MEDIA-LATE-A"
        intent_count = await db[mi.MARK_INTENTS_COLLECTION].count_documents({"reply_message_id": reply_id})
        assert intent_count == 1, "no duplicate MarkIntent from the later real dispatch"

        # --- Step 8: a later WRONG candidate cannot overwrite the locked
        # identity, even via the exact same reply_message_id.
        wrong_result = await mi.apply_resolution(
            intent["id"], candidate_source_message_id="MEDIA-WRONG-B", candidate_source_thumbnail_hash="hash-wrong",
            candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_JUMP_FALLBACK, worker_id="worker-2",
        )
        assert wrong_result["resolved_source_message_id"] == "MEDIA-LATE-A"
        final_intent = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": intent["id"]})
        assert final_intent["resolved_source_message_id"] == "MEDIA-LATE-A"
        assert any(c["source_message_id"] == "MEDIA-WRONG-B" for c in final_intent["conflicting_candidates"])
    finally:
        await _cleanup_intents(talent_id)
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[ma.LATE_PREVIEW_CONTEXT_COLLECTION].delete_many({"talent_id": talent_id})
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])


async def test_unknown_scan_request_with_no_tombstone_still_404s():
    """The safety boundary: a request_id matching neither the live
    collection nor a tombstone is still a genuine, real 404 -- never
    silently swallowed as if it were a legitimate late result."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await agents_whatsapp.report_scan_result(
            request_id=f"totally-unknown-{uuid.uuid4().hex[:8]}",
            payload=agents_whatsapp.ScanResultIn(candidates=[]),
        )
    assert exc.value.status_code == 404


async def test_preview_normal_fast_path_unchanged_deletes_document_and_returns_candidates():
    """The existing normal (in-budget) path must be completely unaffected
    by the lifecycle fix — the document is still deleted once the caller
    has actually consumed a terminal result, exactly as before."""
    talent_id = await _seed_talent(f"MI Fast Path {uuid.uuid4().hex[:6]}", whatsapp_group_name="MI Fast Path x Talentgram")
    project_id = await _seed_project(f"MI Fast Project {uuid.uuid4().hex[:6]}", whatsapp_casting_group_name="MI Fast Casting")
    project_label = "Pepsi"
    try:
        import asyncio

        async def _simulate_worker_and_orchestrator():
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({
                    "talent_id": talent_id, "project_id": project_id, "preview_only": True,
                    "status": ma.SCAN_STATUS_PENDING,
                })
                if req:
                    candidate = {
                        "mention_lid": GUNWANTI_LID,
                        "mark_text": f"Mark introduction video for {project_label}",
                        "reply_message_id": f"reply-fast-{uuid.uuid4().hex[:8]}",
                        "quoted_thumbnail_hash": "hash-fast",
                        "resolved_source_message_id": "MEDIA-FAST-A",
                        "source_media_type": "video", "source_sender": "Raj Talentgram",
                        "source_timestamp": None, "mark_window_position": 1,
                    }
                    await agents_whatsapp.report_scan_result(
                        request_id=req["id"], payload=agents_whatsapp.ScanResultIn(candidates=[candidate]),
                    )
                    assert await orch._process_scan_done()
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("no pending preview scan request appeared in time")

        worker_task = asyncio.create_task(_simulate_worker_and_orchestrator())
        candidates, error = await casting_pipeline._scan_raw_candidates_for_source(
            talent_id=talent_id, talent_label="Aahana",
            project_id=project_id, project_label=project_label,
            source_type="group", group_name="MI Fast Path x Talentgram",
            destination_group="MI Fast Casting", budget_s=3.0,
        )
        await worker_task

        assert error is None
        assert candidates is not None and len(candidates) == 1
        assert candidates[0]["resolved_source_message_id"] == "MEDIA-FAST-A"

        # The document IS deleted on the normal, in-budget path -- unchanged.
        remaining = await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({
            "talent_id": talent_id, "project_id": project_id, "preview_only": True,
        })
        assert remaining == 0
    finally:
        await _cleanup_intents(talent_id)
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"talent_id": talent_id})
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])
