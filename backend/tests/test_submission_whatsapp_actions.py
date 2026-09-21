"""Approve + Upload / Approve + Send — Submission Review actions
(2026-09-20, redesigned 2026-09-21). Covers agents.modules.
submission_whatsapp_actions AND agents.modules.submission_action_queue
together: project/talent identification from a submission_id, the
pre-dispatch error paths (no linked talent, no WhatsApp group, no casting
group), the double-click idempotency guard, and — the 2026-09-21
redesign's own core safety property — that a SEND action's readiness is
decided EXCLUSIVELY from mark_intent_ids its own background verification
loop's freshest live scan actually observed, never from a historical
(talent_id, project_id)-wide database read (the proven-unsafe
`get_ready_assignments` shortcut this redesign removed).

Full worker-side auto-approve-on-success (services/media_assignment_worker.py)
is exercised directly for UPLOAD (SEND's own is already covered by
test_media_send.py's approval-lifecycle suite) to prove the new
operation_ok field and the reused set_decision hook actually fire.
"""
import os
# Same reasoning as test_media_send.py's own top-of-file override: SEND's
# background verification loop polls whatsapp_scan_requests for a worker
# response that never arrives on its own in tests — bound the wait
# instead of eating the 20s production default, and shrink the queue's
# own retry backoff/ceiling so a "never resolves" test completes in
# under a second rather than minutes. Must be set BEFORE the agents
# modules are imported (read once as module-level constants).
os.environ.setdefault("SEND_PREVIEW_POLL_INTERVAL_SEC", "0.05")
os.environ.setdefault("SEND_PREVIEW_MAX_WAIT_SEC", "1.0")
os.environ.setdefault("SEND_PREVIEW_TOTAL_MAX_WAIT_SEC", "1.2")
os.environ.setdefault("SEND_ACTION_RETRY_BACKOFF_SEC", "0.05")
os.environ.setdefault("SEND_ACTION_MAX_VERIFY_ATTEMPTS", "3")
# Shrunk to match the shrunk SEND_PREVIEW_TOTAL_MAX_WAIT_SEC above (1.2s) —
# still comfortably larger than a single test attempt's own scan wait, so
# the late-tombstone-recovery check (get_freshly_resolved_if_complete)
# still has a real, meaningful window to prove against in tests.
os.environ.setdefault("SEND_ACTION_LATE_RESOLUTION_WINDOW_SEC", "5")

import asyncio
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.modules import media_assignment as ma  # noqa: E402
from agents.modules import mark_intent as mi  # noqa: E402
from agents.modules import submission_whatsapp_actions as swa  # noqa: E402
from agents.modules import submission_action_queue as queue  # noqa: E402
from services import media_assignment_worker as orch  # noqa: E402

from tests.test_media_assignment import (  # noqa: E402
    GUNWANTI_LID, _cleanup, _mark, _seed_project, _seed_submission, _seed_talent,
)
from tests.test_media_send import _with_simulated_send_preview  # noqa: E402

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _seed_full(*, with_group=True, with_casting_group=True, decision="pending", link_talent=True):
    tag = uuid.uuid4().hex[:8]
    project_id = await _seed_project(
        f"SWA Project {tag}",
        whatsapp_casting_group_name=(f"SWA Casting {tag}" if with_casting_group else ""),
    )
    talent_id = await _seed_talent(
        f"SWA Talent {tag}",
        whatsapp_group_name=(f"SWA Talent {tag} x Talentgram" if with_group else ""),
        email=f"swa.{tag}@example.com",
    )
    submission_id = await _seed_submission(project_id, talent_id, f"swa.{tag}@example.com", decision=decision)
    if not link_talent:
        await db.submissions.update_one({"id": submission_id}, {"$set": {"talent_id": None}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    return project_id, talent_id, submission_id, tag


async def _cleanup_full(project_id, talent_id, submission_id):
    req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
    await db[mi.MARK_INTENTS_COLLECTION].delete_many({"talent_id": talent_id})
    await db[queue.ACTIONS_COLLECTION].delete_many({"project_id": project_id})
    await _cleanup(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
    await db[swa.ACTION_LOCKS_COLLECTION].delete_many({"_id": {"$regex": f"^{project_id}:"}})


async def _drive_send_action_to_terminal(action_id: str, *, timeout: float = 5.0) -> dict:
    """Test-only driver for the new background verification loop — no
    real persistent task runs during tests, so this repeatedly performs
    exactly what services/media_assignment_worker.py's own
    _send_action_verification_loop does for THIS SPECIFIC action (advance
    it one real step at a time) until it reaches a terminal-ish state
    (MEDIA_RESOLVED-and-dispatched, COMPLETED, or FAILED) or the timeout
    elapses.

    Deliberately fetches and advances the action DIRECTLY by id, rather
    than going through queue.claim_next_send_action_due()'s own
    deliberately UNSCOPED global "oldest due action" query — that
    function is correct for production (there is exactly one real
    verification loop servicing the entire queue) but is the wrong tool
    for a test that wants to deterministically drive ONE specific action
    while the shared test database may transiently hold other tests'
    actions too."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        action = await queue.get_action(action_id)
        if action["state"] in (queue.STATE_COMPLETED, queue.STATE_FAILED) or (
            action["state"] == queue.STATE_MEDIA_RESOLVED and action.get("dispatch_scan_request_id")
        ):
            return action
        next_attempt_at = action["next_attempt_at"]
        if next_attempt_at.tzinfo is None:
            # Mongo round-trips datetimes as naive UTC (BSON has no tz);
            # queue._now() is tz-aware — normalize for this test-only
            # comparison. Production code never compares these in Python
            # at all (claim_next_send_action_due's own due-ness check is
            # a server-side Mongo query operator, immune to this).
            from datetime import timezone as _tz
            next_attempt_at = next_attempt_at.replace(tzinfo=_tz.utc)
        if action["state"] in (queue.STATE_QUEUED, queue.STATE_VERIFYING) and next_attempt_at <= queue._now():
            await queue.advance_send_action(action)
        else:
            await asyncio.sleep(0.02)
    return await queue.get_action(action_id)


async def _run_send_with_candidates(project_id: str, submission_id: str, talent_id: str, candidates: list, *, worker_id=None) -> dict:
    """Full end-to-end SEND drive: dispatch (returns immediately, QUEUED),
    simulate the worker answering the verification scan with the given
    raw candidates, and drive the background loop to a terminal-ish
    state. Returns the final action document."""
    action = await swa.dispatch_approve_send(project_id, submission_id, worker_id=worker_id)
    worker_task = _with_simulated_send_preview(talent_id, project_id, candidates)
    final = await _drive_send_action_to_terminal(action["id"])
    if not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, AssertionError):
            pass
    else:
        await worker_task
    return final


# ---------------------------------------------------------------------------
# Dispatch lock — the atomic double-click/concurrent-dispatch guard
# (Production-safety fix, 2026-09-20). Unaffected by the 2026-09-21
# redesign — still guards the fast, DB-only resolution step before an
# action document is created.
# ---------------------------------------------------------------------------
async def test_dispatch_lock_only_one_concurrent_acquirer_wins():
    key = (f"lock-test-proj-{uuid.uuid4().hex[:8]}", f"lock-test-sub-{uuid.uuid4().hex[:8]}", "send")
    try:
        results = await asyncio.gather(
            swa._acquire_dispatch_lock(*key), swa._acquire_dispatch_lock(*key), swa._acquire_dispatch_lock(*key),
        )
        assert sorted(results) == [False, False, True], (
            "exactly one of three truly concurrent acquirers must win the lock, got %r" % (results,)
        )
    finally:
        await swa._release_dispatch_lock(*key)


async def test_dispatch_lock_release_allows_reacquisition():
    key = (f"lock-test-proj-{uuid.uuid4().hex[:8]}", f"lock-test-sub-{uuid.uuid4().hex[:8]}", "upload")
    try:
        assert await swa._acquire_dispatch_lock(*key) is True
        assert await swa._acquire_dispatch_lock(*key) is False
        await swa._release_dispatch_lock(*key)
        assert await swa._acquire_dispatch_lock(*key) is True
    finally:
        await swa._release_dispatch_lock(*key)


async def test_dispatch_lock_stale_lock_is_reclaimed_not_blocked_forever():
    """A backend crash between acquire and release must never permanently
    block every future retry — a lock older than LOCK_STALE_AFTER_S is
    treated as abandoned and reclaimed."""
    from datetime import timedelta
    project_id, submission_id, action = f"lock-test-proj-{uuid.uuid4().hex[:8]}", f"lock-test-sub-{uuid.uuid4().hex[:8]}", "send"
    await db[swa.ACTION_LOCKS_COLLECTION].insert_one({
        "_id": swa._lock_id(project_id, submission_id, action),
        "created_at": swa._now() - timedelta(seconds=swa.LOCK_STALE_AFTER_S + 30),
    })
    try:
        assert await swa._acquire_dispatch_lock(project_id, submission_id, action) is True
    finally:
        await swa._release_dispatch_lock(project_id, submission_id, action)


async def test_dispatch_lock_fresh_lock_is_never_reclaimed():
    project_id, submission_id, action = f"lock-test-proj-{uuid.uuid4().hex[:8]}", f"lock-test-sub-{uuid.uuid4().hex[:8]}", "send"
    assert await swa._acquire_dispatch_lock(project_id, submission_id, action) is True
    try:
        assert await swa._acquire_dispatch_lock(project_id, submission_id, action) is False
    finally:
        await swa._release_dispatch_lock(project_id, submission_id, action)


# ---------------------------------------------------------------------------
# Duplicate-action protection (Phase 8 #18) — a concurrent double-click
# must never create more than one QUEUED/in-flight action for the same
# (project, submission, type). Under the new architecture SEND no longer
# raises synchronously on "no marked media" (that's now discovered
# asynchronously) — the safety property to prove is "at most one action
# document", not "one of the two calls raised".
# ---------------------------------------------------------------------------
async def test_approve_send_concurrent_double_click_creates_only_one_action():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        results = await asyncio.gather(
            swa.dispatch_approve_send(project_id, submission_id),
            swa.dispatch_approve_send(project_id, submission_id),
            return_exceptions=True,
        )
        action_ids = {r["id"] for r in results if not isinstance(r, Exception)}
        assert len(action_ids) == 1, f"concurrent double-click must never create more than one action, got {action_ids}"
        count = await db[queue.ACTIONS_COLLECTION].count_documents(
            {"project_id": project_id, "submission_id": submission_id, "action_type": "send"}
        )
        assert count == 1
        for r in results:
            if isinstance(r, Exception):
                assert isinstance(r, swa.SubmissionActionError), f"unexpected exception type: {r!r}"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_concurrent_double_click_creates_only_one_action():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        results = await asyncio.gather(
            swa.dispatch_approve_upload(project_id, submission_id),
            swa.dispatch_approve_upload(project_id, submission_id),
            return_exceptions=True,
        )
        action_ids = {r["id"] for r in results if not isinstance(r, Exception)}
        assert len(action_ids) == 1, f"concurrent double-click must never create more than one action, got {action_ids}"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# dispatch_approve_upload — resolution + error paths (unaffected by the
# 2026-09-21 SEND redesign; UPLOAD's own dispatch mechanics are untouched)
# ---------------------------------------------------------------------------
async def test_approve_upload_dispatches_scan_request_with_submission_context():
    project_id, talent_id, submission_id, tag = await _seed_full()
    try:
        action = await swa.dispatch_approve_upload(project_id, submission_id)
        assert action["action_type"] == "upload"
        assert action["dispatch_scan_request_id"]
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": action["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "scan"
        assert doc["talent_id"] == talent_id
        assert doc["project_id"] == project_id
        assert doc["submission_id"] == submission_id
        assert doc["approve_on_success"] is True
        assert doc["group_name"] == f"SWA Talent {tag} x Talentgram"
        assert doc["action_id"] == action["id"]
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_requires_linked_talent():
    project_id, talent_id, submission_id, _ = await _seed_full(link_talent=False)
    try:
        with pytest.raises(swa.SubmissionActionError) as exc:
            await swa.dispatch_approve_upload(project_id, submission_id)
        assert exc.value.code == "no_linked_talent"
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 0
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_requires_whatsapp_group():
    project_id, talent_id, submission_id, _ = await _seed_full(with_group=False)
    try:
        with pytest.raises(swa.SubmissionActionError) as exc:
            await swa.dispatch_approve_upload(project_id, submission_id)
        assert exc.value.code == "no_whatsapp_group"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_double_click_reuses_same_in_flight_action():
    """No duplicate operations from double-clicking — a second dispatch
    while the first action hasn't reached a terminal state returns the
    SAME action, never a second one."""
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        first = await swa.dispatch_approve_upload(project_id, submission_id)
        second = await swa.dispatch_approve_upload(project_id, submission_id)
        assert first["id"] == second["id"]
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 1
        assert await db[queue.ACTIONS_COLLECTION].count_documents({"project_id": project_id}) == 1
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_new_action_allowed_after_prior_one_finished():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        first = await swa.dispatch_approve_upload(project_id, submission_id)
        await db[ma.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": first["dispatch_scan_request_id"]}, {"$set": {"status": ma.STATUS_FINISHED, "operation_ok": True}},
        )
        await queue.sync_from_finished_request(
            await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": first["dispatch_scan_request_id"]}, {"_id": 0}),
        )
        second = await swa.dispatch_approve_upload(project_id, submission_id)
        assert second["id"] != first["id"]
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 2
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# dispatch_approve_send — pre-dispatch (fast, DB-only) error paths.
# These are all still synchronous and unchanged: only the "marked media"
# question itself moved to the background.
# ---------------------------------------------------------------------------
async def test_approve_send_requires_casting_group():
    project_id, talent_id, submission_id, _ = await _seed_full(with_casting_group=False)
    try:
        with pytest.raises(swa.SubmissionActionError) as exc:
            await swa.dispatch_approve_send(project_id, submission_id)
        assert exc.value.code == "no_casting_group"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_send_requires_linked_talent():
    project_id, talent_id, submission_id, _ = await _seed_full(link_talent=False)
    try:
        with pytest.raises(swa.SubmissionActionError) as exc:
            await swa.dispatch_approve_send(project_id, submission_id)
        assert exc.value.code == "no_linked_talent"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_send_returns_immediately_queued_never_blocks_on_scan():
    """The core behavioral fix (2026-09-21): dispatch_approve_send must
    return in well under a second, in QUEUED state, regardless of how
    slow the underlying WhatsApp scan will turn out to be — it must never
    block the HTTP caller on a live scan again."""
    import time
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        t0 = time.monotonic()
        action = await swa.dispatch_approve_send(project_id, submission_id)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"dispatch_approve_send must return immediately, took {elapsed:.2f}s"
        assert action["state"] == queue.STATE_QUEUED
        assert action["action_type"] == "send"
        assert action["mark_intent_ids"] == []
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id, "mode": "send"}) == 0
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_send_no_marked_media_eventually_reports_honestly():
    """No real worker ever answers here, so the background verification
    loop exhausts SEND_ACTION_MAX_VERIFY_ATTEMPTS (set to 3 for this test
    file) against zero candidates every time -> FAILED, code
    no_marked_media (a pure, real, unmocked timeout each attempt — the
    same contract _scan_and_validate_multi_source always had)."""
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        final = await _drive_send_action_to_terminal(action["id"], timeout=15.0)
        assert final["state"] == queue.STATE_FAILED, final
        assert final["error_code"] in ("verification_timeout", "no_marked_media")
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id, "mode": "send"}) == 0
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# Real production incident (2026-09-21) — Kushagre Dua / "Snapdragon
# Computer (Male Start up founder)". FOUR real sibling projects shared one
# casting group, all named "Snapdragon Computer (...)". Both of Kushagre's
# WhatsApp marks ("Mark audition take for Snapdragon computers" /
# "...introduction...") genuinely existed and were scanned, but
# media_assignment.validate_candidates CORRECTLY and safely excluded them
# (a real tie among sibling projects — proven necessary by the earlier,
# separately-fixed Limca Film1/Film2 and Tapti AI App (Ananya)/(Neelam)
# cross-contamination incidents; loosening that matching was explicitly
# investigated and rejected — see the forensic report). The actual, safe
# bug was that the Action Queue then told the admin "No marked WhatsApp
# media found... mark it first" — false; the media WAS marked. These tests
# prove the fix: an accurate, actionable message that names the real
# ambiguity/mismatch instead.
# ---------------------------------------------------------------------------
async def test_ambiguous_sibling_project_mark_reports_accurately_not_as_missing():
    """Two real sibling projects (both containing "Snapdragon" and
    "Computer") share one casting group — mirrors the real 4-sibling
    incident shape with the minimum needed to prove it. A mark generic
    enough to tie between them must surface as marked_media_project_
    ambiguous, naming both projects, never the misleading "mark the media
    first" message — the media WAS marked."""
    tag = uuid.uuid4().hex[:6]
    group = f"Snapdragon Casting {tag}"
    project_id = await _seed_project(f"Snapdragon Computer (Alpha) {tag}", whatsapp_casting_group_name=group)
    sibling_id = await _seed_project(f"Snapdragon Computer (Beta) {tag}", whatsapp_casting_group_name=group)
    talent_id = await _seed_talent(f"Kushagre {tag}", whatsapp_group_name=f"Kushagre {tag} x Talentgram", email=f"kushagre.{tag}@example.com")
    submission_id = await _seed_submission(project_id, talent_id, f"kushagre.{tag}@example.com", decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        worker_task = _with_simulated_send_preview(talent_id, project_id, [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark Snapdragon Computer {tag} take", source_message_id="MEDIA-AMBIG-TAKE", media_type="video"),
        ])
        final = await _drive_send_action_to_terminal(action["id"], timeout=15.0)
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, AssertionError):
                pass
        else:
            await worker_task

        assert final["state"] == queue.STATE_FAILED, final
        assert final["error_code"] == "marked_media_project_ambiguous", final
        assert "mark the audition takes/introduction on WhatsApp first" not in final["error_message"], final
        assert f"Snapdragon Computer (Alpha) {tag}" in final["error_message"], final
        assert f"Snapdragon Computer (Beta) {tag}" in final["error_message"], final
        assert final["retryable"] is True, final
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"project_id": {"$in": [project_id, sibling_id]}})
        await _cleanup_full(project_id, talent_id, submission_id)
        await db.projects.delete_one({"id": sibling_id})


async def test_mismatched_other_project_mark_reports_accurately_not_as_missing():
    """A mark that confidently and uniquely names a DIFFERENT, unrelated
    project (never a sibling — no shared-word ambiguity at all) must
    surface as marked_media_wrong_project, naming that other project, and
    must NEVER be assigned to the requested action's own project."""
    tag = uuid.uuid4().hex[:6]
    group = f"Shared Casting {tag}"
    project_id = await _seed_project(f"Snapdragon Computer (Alpha) {tag}", whatsapp_casting_group_name=group)
    other_id = await _seed_project(f"Pepsi Diwali Campaign {tag}", whatsapp_casting_group_name=group)
    talent_id = await _seed_talent(f"Kushagre {tag}", whatsapp_group_name=f"Kushagre {tag} x Talentgram", email=f"kushagre.{tag}@example.com")
    submission_id = await _seed_submission(project_id, talent_id, f"kushagre.{tag}@example.com", decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        worker_task = _with_simulated_send_preview(talent_id, project_id, [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"Mark take 1 for Pepsi Diwali Campaign {tag}", source_message_id="MEDIA-WRONG-PROJECT", media_type="video"),
        ])
        final = await _drive_send_action_to_terminal(action["id"], timeout=15.0)
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, AssertionError):
                pass
        else:
            await worker_task

        assert final["state"] == queue.STATE_FAILED, final
        assert final["error_code"] == "marked_media_wrong_project", final
        assert "mark the audition takes/introduction on WhatsApp first" not in final["error_message"], final
        assert f"Pepsi Diwali Campaign {tag}" in final["error_message"], final
        # Never actually assigned/dispatched to the requested project.
        assert final.get("mark_intent_ids") in (None, []), final
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id, "mode": "send"}) == 0
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"project_id": {"$in": [project_id, other_id]}})
        await _cleanup_full(project_id, talent_id, submission_id)
        await db.projects.delete_one({"id": other_id})


# ---------------------------------------------------------------------------
# Worker-affinity — a dispatched SEND/UPLOAD request must always carry
# the resolved worker_id, exactly as before this redesign.
# ---------------------------------------------------------------------------
async def test_approve_send_dispatches_send_request_with_correct_worker_id():
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        final = await _run_send_with_candidates(
            project_id, submission_id, talent_id,
            [_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                   source_message_id=f"swa-take1-{tag}", media_type="video")],
            worker_id="wa-worker-2",
        )
        assert final["state"] == queue.STATE_MEDIA_RESOLVED, final
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "send"
        assert doc["worker_id"] == "wa-worker-2", doc
        assert doc["action_id"] == final["id"]
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_dispatches_upload_request_with_correct_worker_id():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        action = await swa.dispatch_approve_upload(project_id, submission_id, worker_id="wa-worker-2")
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": action["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "scan"
        assert doc["worker_id"] == "wa-worker-2", doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_with_no_worker_override_still_gets_explicit_worker_id():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        action = await swa.dispatch_approve_upload(project_id, submission_id)
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": action["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc is not None
        assert doc["worker_id"] is not None and doc["worker_id"] != "", doc
        assert doc["worker_id"] == swa.PRIMARY_WORKER_ID, doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_send_with_no_worker_override_still_gets_explicit_worker_id():
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        final = await _run_send_with_candidates(
            project_id, submission_id, talent_id,
            [_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                   source_message_id=f"swa-noworker-{tag}", media_type="video")],
        )
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc is not None
        assert doc["worker_id"] is not None and doc["worker_id"] != "", doc
        assert doc["worker_id"] == swa.PRIMARY_WORKER_ID, doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_create_scan_request_rejects_none_worker_id():
    with pytest.raises(ValueError, match="worker_id is required"):
        await ma.create_scan_request(
            talent_id="t-x", talent_label="X", project_id="p-x", project_label="P",
            group_name="X x Talentgram", worker_id=None,
        )
    assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": "t-x"}) == 0


async def test_create_scan_request_rejects_empty_string_worker_id():
    with pytest.raises(ValueError, match="worker_id is required"):
        await ma.create_scan_request(
            talent_id="t-x2", talent_label="X2", project_id="p-x2", project_label="P",
            group_name="X2 x Talentgram", worker_id="",
        )
    assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": "t-x2"}) == 0


async def test_create_send_dispatch_from_approved_plan_rejects_none_worker_id():
    from agents.modules import media_send as ms
    with pytest.raises(ValueError, match="worker_id is required"):
        await ms.create_send_dispatch_from_approved_plan(
            talent_id="t-y", project_id="p-y", talent_label="Y", project_label="P",
            destination_group="Dest Y",
            assignments=[{"media_role": "take", "take_number": 1, "resolved_source_message_id": "src-y", "quoted_thumbnail_hash": "hash-y"}],
            default_source_type="group", default_group_name="Y x Talentgram",
            form_message=None, submission_id=None, content_hash=None,
            worker_id=None,
        )
    assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": "t-y"}) == 0


# ---------------------------------------------------------------------------
# Worker-side auto-approve-on-success (UPLOAD) — unaffected by the SEND
# redesign; mirrors SEND's own, already-covered hook in test_media_send.py.
# ---------------------------------------------------------------------------
async def _seed_uploaded_assignment(*, talent_id, project_id, submission_id, source_message_id, source_thumbnail_hash, take_number=1):
    await db[ma.ASSIGNMENTS_COLLECTION].insert_one({
        "id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "media_role": "take", "take_number": take_number,
        "source_message_id": source_message_id, "source_thumbnail_hash": source_thumbnail_hash,
        "assignment_status": ma.ASSIGN_STATUS_UPLOADED, "created_at": _now(),
    })
    await db.submissions.update_one(
        {"id": submission_id},
        {"$push": {"media": {"source_message_id": source_message_id, "role": "take", "take_number": take_number}}},
    )


async def _insert_upload_download_done(*, talent_id, project_id, submission_id, download_targets, results, action_id=None):
    req_id = str(uuid.uuid4())
    doc = {
        "id": req_id, "mode": "download", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "submission_id": submission_id,
        "download_targets": download_targets, "download_results": results,
        "pending_report_context": {
            "talent_label": "Test Talent", "project_label": "Test Project",
            "already": [], "upload_advisory": "",
            "submission_id": submission_id, "approve_on_success": True,
        },
        "created_at": _now(), "updated_at": _now(),
    }
    if action_id:
        doc["action_id"] = action_id
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one(doc)
    return req_id


async def test_upload_orchestrator_auto_approves_submission_on_full_success():
    project_id, talent_id, submission_id, tag = await _seed_full(decision="pending")
    target = {
        "source_message_id": "src-swa-a", "media_role": "take", "take_number": 1,
        "source_thumbnail_hash": "hash-swa-a", "original_label": "Take 1",
    }
    req_id = await _insert_upload_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        download_targets=[target], results=[{"ok": True, "source_message_id": "src-swa-a"}],
    )
    await _seed_uploaded_assignment(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        source_message_id="src-swa-a", source_thumbnail_hash="hash-swa-a",
    )
    try:
        assert await orch._process_download_done()
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc["status"] == ma.STATUS_FINISHED
        assert doc.get("operation_ok") is True
        sub = await db.submissions.find_one({"id": submission_id}, {"_id": 0})
        assert sub["decision"] == "approved", sub
        assert sub.get("decided_at")
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_upload_orchestrator_never_auto_approves_on_partial_failure():
    project_id, talent_id, submission_id, tag = await _seed_full(decision="pending")
    targets = [
        {"source_message_id": "src-swa-b1", "media_role": "take", "take_number": 1,
         "source_thumbnail_hash": "hash-swa-b1", "original_label": "Take 1"},
        {"source_message_id": "src-swa-b2", "media_role": "take", "take_number": 2,
         "source_thumbnail_hash": "hash-swa-b2", "original_label": "Take 2"},
    ]
    req_id = await _insert_upload_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        download_targets=targets,
        results=[
            {"ok": True, "source_message_id": "src-swa-b1"},
            {"ok": False, "source_message_id": "src-swa-b2", "error": "download timed out"},
        ],
    )
    await _seed_uploaded_assignment(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        source_message_id="src-swa-b1", source_thumbnail_hash="hash-swa-b1",
    )
    try:
        assert await orch._process_download_done()
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc["status"] == ma.STATUS_FINISHED
        assert not doc.get("operation_ok")
        sub = await db.submissions.find_one({"id": submission_id}, {"_id": 0})
        assert sub["decision"] == "pending", sub
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# get_action_status — now action_id-based (Phase 8 #10: queue persistence
# / status reflects the durable action, not a bare scan_requests read).
# ---------------------------------------------------------------------------
async def test_get_action_status_unknown_action_reports_not_found():
    result = await swa.get_action_status("no-such-action", "no-such-project", "no-such-submission")
    assert result["found"] is False
    assert result["done"] is True
    assert result["ok"] is False


async def test_get_action_status_reflects_completion_after_finish():
    project_id, talent_id, submission_id, tag = await _seed_full(decision="pending")
    action = await swa.dispatch_approve_upload(project_id, submission_id)
    target = {
        "source_message_id": "src-swa-c", "media_role": "take", "take_number": 1,
        "source_thumbnail_hash": "hash-swa-c", "original_label": "Take 1",
    }
    await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": action["dispatch_scan_request_id"]})
    req_id = await _insert_upload_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        download_targets=[target], results=[{"ok": True, "source_message_id": "src-swa-c"}],
        action_id=action["id"],
    )
    await queue.mark_dispatched(action["id"], req_id)
    await _seed_uploaded_assignment(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        source_message_id="src-swa-c", source_thumbnail_hash="hash-swa-c",
    )
    try:
        pending = await swa.get_action_status(action["id"], project_id, submission_id)
        assert pending["done"] is False
        assert pending["ok"] is None

        assert await orch._process_download_done()
        finished = await swa.get_action_status(action["id"], project_id, submission_id)
        assert finished["done"] is True
        assert finished["ok"] is True
        assert finished["decision"] == "approved"
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_get_action_status_never_implies_approved_when_decision_write_failed():
    """Production-safety fix (2026-09-20, preserved through the redesign):
    operation_ok=True means the real WhatsApp media operation succeeded —
    it is NOT a promise that set_decision's own separate write also
    succeeded."""
    project_id, talent_id, submission_id, _ = await _seed_full(decision="pending")
    action = await queue.create_action(
        action_type="upload", project_id=project_id, talent_id=talent_id, talent_label="X",
        project_label="P", submission_id=submission_id, worker_id="default", created_by="admin",
    )
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "project_id": project_id, "submission_id": submission_id,
        "status": ma.STATUS_FINISHED, "operation_ok": True, "report": "UPLOAD COMPLETE",
        "action_id": action["id"], "created_at": _now(), "updated_at": _now(),
    })
    await queue.sync_from_finished_request(
        await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0}),
    )
    try:
        result = await swa.get_action_status(action["id"], project_id, submission_id)
        assert result["ok"] is True
        assert result["decision"] == "pending", (
            "decision must reflect the REAL submission state, never be assumed 'approved' from ok=True alone"
        )
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# THE CORE SAFETY PROPERTY (2026-09-21 architecture review, Phase 8 #7):
# a SEND action's readiness is decided EXCLUSIVELY from mark_intent_ids
# its OWN background verification loop's freshest live scan observed —
# never from a historical (talent_id, project_id)-wide MarkIntent read.
# These are the direct successors to the removed, proven-unsafe
# get_ready_assignments tests.
# ---------------------------------------------------------------------------
async def test_old_resolved_plus_new_unobserved_mark_never_sends_stale_media():
    """Required test 1 — the exact scenario the architecture review
    proved unsafe: an OLD, resolved MarkIntent exists for Take 1, but the
    admin's fresh re-MARK has NOT been observed by any scan yet. The
    action's own verification scan (simulated here as finding ZERO
    candidates — the "re-MARK not observed" case) must NEVER use the old
    resolved intent; the action must stay unresolved/failed, never
    dispatch using the stale source."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    # Seed an OLD, already-resolved MarkIntent for this exact slot.
    old_intent = await mi.get_or_create_mark_intent(
        reply_message_id=f"reply-old-{tag}", talent_id=talent_id, project_id=project_id,
        project_label=project_label, media_role="take", take_number=1,
        mark_text=f"mark audition take 1 for {project_label}", quoted_thumbnail_hash="hash-OLD",
        quoted_media_type="video", source_chat_name=f"SWA Talent {tag} x Talentgram", worker_id="default",
    )
    await mi.apply_resolution(
        old_intent["id"], candidate_source_message_id="MEDIA-OLD-STALE", candidate_source_thumbnail_hash="hash-OLD",
        candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
    )
    try:
        # The action's own scan finds NOTHING (the re-MARK hasn't been
        # observed yet) — simulated as an empty candidate list, which
        # drives casting_pipeline._scan_and_validate_multi_source's own
        # real (None, None) pure-timeout contract.
        final = await _run_send_with_candidates(project_id, submission_id, talent_id, [])
        assert final["state"] == queue.STATE_FAILED, final
        assert "MEDIA-OLD-STALE" not in str(final.get("mark_intent_ids", [])), final
        assert final.get("dispatch_scan_request_id") is None, (
            "must never reach dispatch using the stale resolved intent"
        )
        # And directly: no scan_requests SEND doc was ever created for
        # this project — nothing was sent, stale or otherwise.
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id, "mode": "send"}) == 0
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_old_resolved_plus_new_observed_mark_uses_new_source_only():
    """Required test 2 — once the re-MARK IS observed by this action's
    own scan (a fresh candidate with a DIFFERENT quoted_thumbnail_hash,
    correlating to an independent MarkIntent per Case C), the action must
    resolve to and dispatch with the NEW source — never the old one,
    never a blend."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    old_intent = await mi.get_or_create_mark_intent(
        reply_message_id=f"reply-old2-{tag}", talent_id=talent_id, project_id=project_id,
        project_label=project_label, media_role="take", take_number=1,
        mark_text=f"mark audition take 1 for {project_label}", quoted_thumbnail_hash="hash-OLD2",
        quoted_media_type="video", source_chat_name=f"SWA Talent {tag} x Talentgram", worker_id="default",
    )
    await mi.apply_resolution(
        old_intent["id"], candidate_source_message_id="MEDIA-OLD2", candidate_source_thumbnail_hash="hash-OLD2",
        candidate_source_media_type="video", resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
    )
    try:
        new_candidate = _mark(
            mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
            source_message_id="MEDIA-NEW-REMARK", media_type="video",
        )
        new_candidate["quoted_thumbnail_hash"] = "hash-NEW-REMARK"
        final = await _run_send_with_candidates(project_id, submission_id, talent_id, [new_candidate])
        assert final["state"] == queue.STATE_MEDIA_RESOLVED, final
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final["dispatch_scan_request_id"]}, {"_id": 0})
        assert len(doc["send_targets"]) == 1
        assert doc["send_targets"][0]["source_message_id"] == "MEDIA-NEW-REMARK"
        assert doc["send_targets"][0]["source_message_id"] != "MEDIA-OLD2"
        # The old MarkIntent itself remains untouched/historical.
        stale = await db[mi.MARK_INTENTS_COLLECTION].find_one({"id": old_intent["id"]})
        assert stale["resolved_source_message_id"] == "MEDIA-OLD2"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_shared_introduction_across_two_projects_end_to_end():
    """Required test 3/19 — the same introduction source legitimately
    shared by two different projects' SEND actions must each
    independently resolve and dispatch correctly, with zero cross-talk."""
    tag = uuid.uuid4().hex[:8]
    talent_id = await _seed_talent(f"Shared Talent {tag}", whatsapp_group_name=f"Shared Talent {tag} x Talentgram", email=f"shared.{tag}@example.com")
    project1_id = await _seed_project(f"Shared Film1 {tag}", whatsapp_casting_group_name=f"Shared Casting1 {tag}")
    project2_id = await _seed_project(f"Shared Film2 {tag}", whatsapp_casting_group_name=f"Shared Casting2 {tag}")
    sub1 = await _seed_submission(project1_id, talent_id, f"shared.{tag}@example.com")
    sub2 = await _seed_submission(project2_id, talent_id, f"shared.{tag}@example.com")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    try:
        intro1 = _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark introduction for Shared Film1 {tag}", source_message_id="MEDIA-SHARED-INTRO", media_type="video")
        intro1["quoted_thumbnail_hash"] = "hash-shared-intro"
        final1 = await _run_send_with_candidates(project1_id, sub1, talent_id, [intro1])
        assert final1["state"] == queue.STATE_MEDIA_RESOLVED, final1

        intro2 = _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark introduction for Shared Film2 {tag}", source_message_id="MEDIA-SHARED-INTRO", media_type="video")
        intro2["quoted_thumbnail_hash"] = "hash-shared-intro"
        final2 = await _run_send_with_candidates(project2_id, sub2, talent_id, [intro2])
        assert final2["state"] == queue.STATE_MEDIA_RESOLVED, final2

        doc1 = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final1["dispatch_scan_request_id"]}, {"_id": 0})
        doc2 = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final2["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc1["send_targets"][0]["source_message_id"] == "MEDIA-SHARED-INTRO"
        assert doc2["send_targets"][0]["source_message_id"] == "MEDIA-SHARED-INTRO"
        assert set(final1["mark_intent_ids"]).isdisjoint(final2["mark_intent_ids"]), (
            "each project's action must be associated with its OWN independent MarkIntent, never shared"
        )
    finally:
        await db[mi.MARK_INTENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[queue.ACTIONS_COLLECTION].delete_many({"talent_id": talent_id})
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup(talent_ids=[talent_id], project_ids=[project1_id, project2_id], scan_request_ids=req_ids, submission_ids=[sub1, sub2])


async def test_late_mark_intent_resolution_after_deadline_action_continues():
    """Required test 6/20 — the direct successor to the Film 1 proven
    timing (correct resolution landing 5-7s AFTER a bounded preview's own
    deadline): the FIRST verification attempt finds nothing (simulating
    a cold/slow scan), the SECOND attempt (this action's own retry, not
    a different one) finds and resolves it. The action must continue and
    succeed, never permanently fail after just one empty attempt."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)

        # Attempt 1: no worker ever answers this scan at all — a genuine,
        # real (unmocked) timeout, exactly the proven Film 1/Film 2
        # "cold scan" characteristic (never a successful-but-empty scan,
        # which is a materially different, already-covered case —
        # test_approve_send_no_marked_media_eventually_reports_honestly).
        # Advances THIS action directly (see _drive_send_action_to_
        # terminal's own docstring for why tests never use
        # claim_next_send_action_due's deliberately unscoped global
        # query).
        await queue.advance_send_action(action)
        mid = await queue.get_action(action["id"])
        assert mid["state"] == queue.STATE_VERIFYING, mid
        assert mid["attempt_count"] == 1, mid

        # Attempt 2 (this SAME action's own retry): the mark now appears.
        # Note: _run_send_with_candidates would dispatch a NEW action, so
        # this drives the SAME existing action's next attempt directly.
        real_candidate = _mark(
            mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
            source_message_id="MEDIA-LATE-RESOLVE", media_type="video",
        )
        worker_task = _with_simulated_send_preview(talent_id, project_id, [real_candidate])
        final = await _drive_send_action_to_terminal(action["id"], timeout=5.0)
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, AssertionError):
                pass
        else:
            await worker_task

        assert final["state"] == queue.STATE_MEDIA_RESOLVED, final
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc["send_targets"][0]["source_message_id"] == "MEDIA-LATE-RESOLVE"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_late_tombstone_recovery_caught_within_same_attempt_no_blind_retry_loop():
    """Regression test for a bug caught LIVE in production (2026-09-21,
    action_id 8ce521a4-625e-4b19-8a51-c66b16abe18c, Raj Mehta / Limca
    Film1): a real worker result can land via the EXISTING, unchanged
    late-worker/tombstone mechanism (routers.agents_whatsapp.
    report_scan_result's own 404 branch) a few seconds AFTER this
    action's own _scan_and_validate_multi_source call already returned
    its pure (None, None) timeout — a code path advance_send_action's
    per-attempt scan never itself observes. Before the fix, this looped
    blind retries (mark_intent_ids stuck at []) even though the correct
    MarkIntent was already sitting there, freshly resolved. This test
    proves the SAME first attempt now catches it directly via
    mark_intent.get_freshly_resolved_if_complete, without needing (and
    without waiting for) a second scan attempt at all."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)

        # Simulate the late-tombstone path having ALREADY written a fully
        # resolved MarkIntent for this exact (talent, project) moments
        # ago — exactly what report_scan_result's 404 branch does when a
        # late worker result lands, entirely independent of (and never
        # touched by) this test's own upcoming advance_send_action call.
        late_intent = await mi.get_or_create_mark_intent(
            reply_message_id=f"reply-late-tombstone-{tag}", talent_id=talent_id, project_id=project_id,
            project_label=project_label, media_role="take", take_number=1,
            mark_text=f"mark audition take 1 for {project_label}", quoted_thumbnail_hash="hash-TOMBSTONE",
            quoted_media_type="video", source_chat_name=f"SWA Talent {tag} x Talentgram", worker_id="default",
        )
        await mi.apply_resolution(
            late_intent["id"], candidate_source_message_id="MEDIA-TOMBSTONE-RECOVERED",
            candidate_source_thumbnail_hash="hash-TOMBSTONE", candidate_source_media_type="video",
            resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )

        # No worker ever answers THIS action's own scan (no
        # _with_simulated_send_preview task started) — a genuine,
        # real (unmocked, shrunk-to-1.2s) pure timeout, exactly the
        # proven production characteristic.
        result = await queue.advance_send_action(action)
        assert result is True

        mid = await queue.get_action(action["id"])
        assert mid["state"] == queue.STATE_MEDIA_RESOLVED, mid
        assert mid["attempt_count"] == 1, (
            "must be caught within the SAME first attempt — a second attempt "
            "would mean the blind-retry-loop bug is still present"
        )
        assert mid["mark_intent_ids"] == [late_intent["id"]], mid

        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": mid["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc["send_targets"][0]["source_message_id"] == "MEDIA-TOMBSTONE-RECOVERED"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_late_tombstone_recovery_ignores_stale_resolution_outside_window():
    """The other half of the same fix's safety property: a MarkIntent
    resolved LONG ago (outside SEND_ACTION_LATE_RESOLUTION_WINDOW_SEC)
    must NOT be picked up as if it were a fresh late-tombstone result —
    that would be exactly the proven-unsafe get_ready_assignments
    shortcut this module's whole redesign removed. With no fresh
    evidence, a pure timeout must fall through to the normal
    stay-VERIFYING/retry path, never a silent resolve."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)

        stale_intent = await mi.get_or_create_mark_intent(
            reply_message_id=f"reply-stale-{tag}", talent_id=talent_id, project_id=project_id,
            project_label=project_label, media_role="take", take_number=1,
            mark_text=f"mark audition take 1 for {project_label}", quoted_thumbnail_hash="hash-STALE",
            quoted_media_type="video", source_chat_name=f"SWA Talent {tag} x Talentgram", worker_id="default",
        )
        await mi.apply_resolution(
            stale_intent["id"], candidate_source_message_id="MEDIA-STALE-OLD",
            candidate_source_thumbnail_hash="hash-STALE", candidate_source_media_type="video",
            resolution_method=mi.RESOLUTION_METHOD_PRIMARY, worker_id="default",
        )
        # Backdate resolved_at well outside the test's 5s window, exactly
        # as a genuinely old resolution (from a prior, unrelated action)
        # would look.
        from datetime import timedelta
        await db[mi.MARK_INTENTS_COLLECTION].update_one(
            {"id": stale_intent["id"]}, {"$set": {"resolved_at": mi._now() - timedelta(seconds=120)}},
        )

        result = await queue.advance_send_action(action)
        assert result is True

        mid = await queue.get_action(action["id"])
        assert mid["state"] == queue.STATE_VERIFYING, mid
        assert mid.get("mark_intent_ids") in (None, []), (
            "a stale resolution outside the recency window must never be silently reused"
        )
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_send_transport_pipeline_untouched_dispatch_reaches_existing_mechanism():
    """Required test 9 — proves the resolved action hands off to the
    EXACT SAME, unchanged media_send.create_send_dispatch_from_approved_plan
    -> whatsapp_scan_requests(mode="send") -> worker-claim pipeline every
    other SEND caller uses — never a new/parallel transport."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        final = await _run_send_with_candidates(
            project_id, submission_id, talent_id,
            [_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                   source_message_id="MEDIA-TRANSPORT-CHECK", media_type="video")],
        )
        assert final["state"] == queue.STATE_MEDIA_RESOLVED
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": final["dispatch_scan_request_id"]}, {"_id": 0})
        assert doc["mode"] == "send" and doc["workflow"] == "send"
        assert doc["status"] in (ma.DOWNLOAD_STATUS_PENDING, "pending")
        assert doc["send_targets"][0]["source_message_id"] == "MEDIA-TRANSPORT-CHECK"
        assert doc["send_targets"][0]["destination_group"]
        # This is the SAME document shape/collection every other SEND
        # caller (WhatsApp chat commands, bulk send) already produces —
        # services/media_assignment_worker.py's existing _worker_loop
        # (unchanged) is what claims and drives it from here.
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_retry_failed_send_action_resets_and_can_succeed():
    """Required test 14 — explicit admin Retry on a FAILED, retryable
    action resets it, and the background loop can then succeed it (a
    fresh scan this time finds the mark)."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        # Drive the existing action to failure first (empty candidates,
        # ceiling reached quickly per this file's env overrides).
        worker_task = _with_simulated_send_preview(talent_id, project_id, [])
        try:
            failed = await _drive_send_action_to_terminal(action["id"], timeout=10.0)
        finally:
            if not worker_task.done():
                worker_task.cancel()
                try:
                    await worker_task
                except (asyncio.CancelledError, AssertionError):
                    pass
        assert failed["state"] == queue.STATE_FAILED, failed
        assert failed["retryable"] is True

        retried = await swa.retry_action(action["id"], project_id, submission_id)
        assert retried["state"] == queue.STATE_QUEUED
        assert retried["attempt_count"] == 0
        assert retried["mark_intent_ids"] == []

        worker_task2 = _with_simulated_send_preview(talent_id, project_id, [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                  source_message_id="MEDIA-AFTER-RETRY", media_type="video"),
        ])
        final = await _drive_send_action_to_terminal(action["id"], timeout=5.0)
        if not worker_task2.done():
            worker_task2.cancel()
            try:
                await worker_task2
            except (asyncio.CancelledError, AssertionError):
                pass
        else:
            await worker_task2
        assert final["state"] == queue.STATE_MEDIA_RESOLVED, final
        assert final["id"] == action["id"], "retry reuses the SAME action document, never a new one"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_list_queue_shows_active_and_recent_terminal_actions():
    """Required test 10/12 — the queue feed is independent of any single
    Submission Review page and shows multiple actions at once."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project2_id, talent2_id, submission2_id, tag2 = await _seed_full()
    try:
        a1 = await swa.dispatch_approve_upload(project_id, submission_id)
        a2 = await swa.dispatch_approve_send(project2_id, submission2_id)
        feed = await swa.list_queue()
        ids = {a["id"] for a in feed["actions"]}
        assert a1["id"] in ids
        assert a2["id"] in ids
        by_id = {a["id"]: a for a in feed["actions"]}
        assert by_id[a1["id"]]["talent_id"] == talent_id
        assert by_id[a2["id"]]["talent_id"] == talent2_id
        assert "display_state" in by_id[a1["id"]]
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)
        await _cleanup_full(project2_id, talent2_id, submission2_id)


# ---------------------------------------------------------------------------
# Worker-restart resilience (Phase 8 #15/#16) — the queue is entirely
# DB-state-driven, never tracked only in an in-process asyncio task, so a
# backend restart at any point before dispatch simply means the NEXT
# process's own instance of _send_action_verification_loop picks the
# action back up from its last durably-written state. These tests
# simulate "restart" the same way the real loop would recover: by acting
# on the action purely from what's in the database, with no reference to
# any in-memory task from "before".
# ---------------------------------------------------------------------------
async def test_worker_restart_while_action_queued_is_picked_up_fresh():
    """Required test 15 — an action created but never advanced at all
    (the backend died the instant after Approve + Send was clicked, before
    its own first verification tick) must still be found and advanced by
    a fresh claim — nothing about it depended on any in-memory state."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        assert action["state"] == queue.STATE_QUEUED
        # Simulate "restart": a brand-new call to claim_next_send_action_due
        # (exactly what a freshly-started process's loop would do first),
        # scoped to just this one action by pre-filtering other stray
        # test data out of the way isn't needed here since we assert on
        # the specific action after a direct advance instead.
        await queue.advance_send_action(await queue.get_action(action["id"]))
        after = await queue.get_action(action["id"])
        assert after["state"] == queue.STATE_VERIFYING
        assert after["attempt_count"] == 1
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_worker_restart_while_verifying_resumes_attempt_count_not_reset():
    """Required test 16 — an action already mid-verification (attempt_count
    > 0) when the backend restarts must resume from its own durably
    persisted attempt_count, never silently reset to 0 (which could
    extend its effective retry ceiling indefinitely across repeated
    restarts) and never lose its partially-observed mark_intent_ids."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)
        await queue.advance_send_action(await queue.get_action(action["id"]))
        mid = await queue.get_action(action["id"])
        assert mid["attempt_count"] == 1
        # "Restart": nothing but a fresh read-and-advance from the DB —
        # no reference to any prior in-memory object.
        fresh_read = await queue.get_action(action["id"])
        await queue.advance_send_action(fresh_read)
        after = await queue.get_action(action["id"])
        assert after["attempt_count"] == 2, "attempt_count must resume, never reset, across a simulated restart"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_worker_restart_while_sending_relies_on_existing_reap_mechanism():
    """Required test 17 — once an action is MEDIA_RESOLVED and dispatched,
    everything from there on is the EXISTING, unchanged execution
    pipeline (services/media_assignment_worker.py's own _reap_stuck_claims
    + _worker_loop), not touched by this redesign at all. Confirms that
    existing mechanism still reaps an orphaned "processing" claim for a
    mode="send" document exactly as it always has — proving this
    redesign didn't accidentally bypass or weaken it."""
    from datetime import timedelta
    project_id, talent_id, submission_id, tag = await _seed_full()
    try:
        req_id = str(uuid.uuid4())
        await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
            "id": req_id, "mode": "send", "status": "processing",
            "talent_id": talent_id, "project_id": project_id, "submission_id": submission_id,
            "claimed_at": orch._now() - timedelta(seconds=orch.STUCK_CLAIM_TIMEOUT_S + 60),
            "created_at": _now(), "updated_at": _now(),
        })
        orch._last_reap = 0  # force the throttle to allow an immediate reap in this test
        await orch._reap_stuck_claims()
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc["status"] == ma.DOWNLOAD_STATUS_FAILED
        assert "orphaned" in (doc.get("download_error") or "")
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_dispatch_failure_becomes_retryable_failed_never_stuck_in_media_resolved():
    """Robustness gap closed in review (2026-09-21): a resolved action
    whose actual dispatch call throws (e.g. a transient error inside
    media_send's own functions) must surface as a normal, visible,
    retryable FAILED action — never freeze silently in MEDIA_RESOLVED
    forever with no error and no way for claim_next_send_action_due (which
    only ever looks at QUEUED/VERIFYING) to pick it up again."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        action = await swa.dispatch_approve_send(project_id, submission_id)

        # Corrupt the submission's own project_id linkage so
        # _dispatch_send's own project re-read finds nothing -- deliberately
        # a NATURAL failure path (submission_or_project_missing), not a
        # patched-in exception, so this proves the real code's own
        # behavior end-to-end.
        await db.submissions.update_one({"id": submission_id}, {"$set": {"project_id": "does-not-exist"}})

        final = await _run_send_with_candidates(
            project_id, submission_id, talent_id,
            [_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                   source_message_id="MEDIA-DISPATCH-FAIL", media_type="video")],
        )
        assert final["state"] == queue.STATE_FAILED, final
        assert final["retryable"] is True
        assert final["error_code"] in ("submission_or_project_missing", "dispatch_failed")
        assert final.get("dispatch_scan_request_id") is None
    finally:
        await db.submissions.update_one({"id": submission_id}, {"$set": {"project_id": project_id}})
        await _cleanup_full(project_id, talent_id, submission_id)
