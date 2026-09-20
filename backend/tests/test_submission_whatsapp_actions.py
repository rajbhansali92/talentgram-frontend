"""Approve + Upload / Approve + Send — Submission Review actions
(2026-09-20). Covers agents.modules.submission_whatsapp_actions: project/
talent identification from a submission_id, the pre-dispatch error paths
(no linked talent, no WhatsApp group, no casting group, no marked media),
the double-click idempotency guard, and that a dispatched request reuses
the EXISTING UPLOAD/SEND pipelines (no parallel scan_requests shape).

Full worker-side auto-approve-on-success (services/media_assignment_worker.py)
is exercised directly for UPLOAD (SEND's own is already covered by
test_media_send.py's approval-lifecycle suite) to prove the new
operation_ok field and the reused set_decision hook actually fire.
"""
import os
# Same reasoning as test_media_send.py's own top-of-file override: SEND's
# pre-dispatch preview scan (casting_pipeline._preview_send_marks) polls
# whatsapp_scan_requests for a worker response that never arrives in
# tests — bound the wait instead of eating the 20s production default.
# Must be set BEFORE the agents modules are imported (read once as
# module-level constants).
os.environ.setdefault("SEND_PREVIEW_POLL_INTERVAL_SEC", "0.05")
os.environ.setdefault("SEND_PREVIEW_MAX_WAIT_SEC", "1.5")

import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.modules import media_assignment as ma  # noqa: E402
from agents.modules import submission_whatsapp_actions as swa  # noqa: E402
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
    await _cleanup(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
    await db[swa.ACTION_LOCKS_COLLECTION].delete_many({"_id": {"$regex": f"^{project_id}:"}})


# ---------------------------------------------------------------------------
# Dispatch lock — the atomic double-click/concurrent-dispatch guard
# (Production-safety fix, 2026-09-20). _find_in_flight_request alone
# cannot cover dispatch_approve_send's pre-dispatch preview scan, which
# can run for several real seconds with no scan_requests doc in existence
# yet — these tests exercise the lock primitive directly, proving exactly
# one of two truly concurrent callers wins, independent of any WhatsApp
# scan machinery.
# ---------------------------------------------------------------------------
async def test_dispatch_lock_only_one_concurrent_acquirer_wins():
    import asyncio
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
        # swa._now() (a real datetime), NOT core._now() (which returns an
        # ISO string) — must match what _acquire_dispatch_lock itself
        # writes, or the staleness comparison below is comparing apples
        # to oranges.
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


async def test_approve_send_concurrent_double_click_dispatches_only_once():
    """End-to-end proof of the fix this review found: two truly
    concurrent dispatch_approve_send calls for the SAME submission (the
    real double-click/HTTP-retry scenario) must never both create a
    scan_requests doc — the second must reattach to the first's request
    id (via the lock forcing it to lose the race, then _find_in_flight_
    request finding the winner's freshly-created doc) or cleanly fail,
    but never independently dispatch a second SEND."""
    import asyncio
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        results = await asyncio.gather(
            swa.dispatch_approve_send(project_id, submission_id),
            swa.dispatch_approve_send(project_id, submission_id),
            return_exceptions=True,
        )
        send_docs = await db[ma.SCAN_REQUESTS_COLLECTION].count_documents(
            {"project_id": project_id, "mode": "send"}
        )
        assert send_docs <= 1, f"concurrent double-click must never create more than one SEND dispatch, got {send_docs}"
        # In this test environment there is no real marked media, so both
        # calls are expected to fail with a SubmissionActionError (not a
        # dispatched request) — the real assertion above already proves
        # no duplicate dispatch happened; this just confirms neither call
        # crashed with anything unexpected.
        for r in results:
            if isinstance(r, Exception):
                assert isinstance(r, swa.SubmissionActionError), f"unexpected exception type: {r!r}"
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# dispatch_approve_upload — resolution + error paths
# ---------------------------------------------------------------------------
async def test_approve_upload_dispatches_scan_request_with_submission_context():
    project_id, talent_id, submission_id, tag = await _seed_full()
    try:
        req_id = await swa.dispatch_approve_upload(project_id, submission_id)
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "scan"
        assert doc["talent_id"] == talent_id
        assert doc["project_id"] == project_id
        assert doc["submission_id"] == submission_id
        assert doc["approve_on_success"] is True
        assert doc["group_name"] == f"SWA Talent {tag} x Talentgram"
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


async def test_approve_upload_double_click_reuses_same_in_flight_request():
    """No duplicate operations from double-clicking — a second dispatch
    while the first scan_request hasn't reached STATUS_FINISHED returns
    the SAME request id, never a second one."""
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        first = await swa.dispatch_approve_upload(project_id, submission_id)
        second = await swa.dispatch_approve_upload(project_id, submission_id)
        assert first == second
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 1
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_new_request_allowed_after_prior_one_finished():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        first = await swa.dispatch_approve_upload(project_id, submission_id)
        await db[ma.SCAN_REQUESTS_COLLECTION].update_one({"id": first}, {"$set": {"status": ma.STATUS_FINISHED}})
        second = await swa.dispatch_approve_upload(project_id, submission_id)
        assert second != first
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 2
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# dispatch_approve_send — resolution + error paths (no live WhatsApp, so
# _preview_send_marks always times out/finds nothing here — exercised via
# its "no marked media" / "unresolved" branches, never a real scan).
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


async def test_approve_send_no_marked_media_reports_honestly():
    """No real worker is running, so the bounded preview scan (SEND_PREVIEW_*
    env overrides set in test_media_send.py's import) finds an empty
    scan_done with zero candidates -> zero assignments, never an error --
    covers the "identify marked media" step's honest-empty-result path."""
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        with pytest.raises(swa.SubmissionActionError) as exc:
            await swa.dispatch_approve_send(project_id, submission_id)
        assert exc.value.code in ("no_marked_media", "marked_media_unresolved")
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents(
            {"project_id": project_id, "mode": "send"}
        ) == 0
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# Worker-affinity fix (2026-09-20, real production incident): a Limca Film1
# SEND request dispatched for Worker 1 was instead claimed and processed by
# Worker 2, because dispatch_approve_send -> create_send_dispatch_from_
# approved_plan never wrote a worker_id onto the persisted document at all.
# Both dispatch_approve_send and dispatch_approve_upload now accept and
# thread their own worker_id argument straight through to the scan_requests
# document routers.agents_whatsapp.claim_scan_request's new worker-scoped
# filter reads back (see test_media_assignment.py's test_claim_* suite for
# the claim-side half of this fix).
# ---------------------------------------------------------------------------
async def test_approve_send_dispatches_send_request_with_correct_worker_id():
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        worker = _with_simulated_send_preview(talent_id, project_id, [
            _mark(
                mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                source_message_id=f"swa-take1-{tag}", media_type="video",
            ),
        ])
        req_id = await swa.dispatch_approve_send(project_id, submission_id, worker_id="wa-worker-2")
        await worker
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "send"
        assert doc["worker_id"] == "wa-worker-2", doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_upload_dispatches_upload_request_with_correct_worker_id():
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        req_id = await swa.dispatch_approve_upload(project_id, submission_id, worker_id="wa-worker-2")
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc is not None
        assert doc["mode"] == "scan"
        assert doc["worker_id"] == "wa-worker-2", doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


# ---------------------------------------------------------------------------
# NEW job ownership must be explicit (2026-09-20 pre-deployment review) —
# the None/absent-means-Worker-1 compatibility rule in
# _scan_request_worker_filter exists ONLY for documents that already
# existed before the worker-affinity fix; it must never silently absorb a
# BRAND NEW job whose caller forgot to resolve a real worker identity.
# Two layers prove this: (1) dispatch_approve_upload/dispatch_approve_send
# with no worker_id override still persist a concrete, non-None worker_id
# (resolved from submission_whatsapp_actions.PRIMARY_WORKER_ID BEFORE the
# document is ever inserted); (2) the lower-level create_* functions that
# actually perform the insert reject worker_id=None outright, so even a
# caller that bypasses the dispatch layer entirely can never persist a
# NEW ownerless document.
# ---------------------------------------------------------------------------
async def test_approve_upload_with_no_worker_override_still_gets_explicit_worker_id():
    """The real production call shape — routers/submissions.py never
    passes worker_id at all — must never persist worker_id=None."""
    project_id, talent_id, submission_id, _ = await _seed_full()
    try:
        req_id = await swa.dispatch_approve_upload(project_id, submission_id)
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc is not None
        assert doc["worker_id"] is not None and doc["worker_id"] != "", doc
        assert doc["worker_id"] == swa.PRIMARY_WORKER_ID, doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_approve_send_with_no_worker_override_still_gets_explicit_worker_id():
    """Same rule for SEND — dispatch_approve_send's default call shape
    must resolve a concrete worker_id before create_send_dispatch_from_
    approved_plan is ever reached, never pass None through."""
    project_id, talent_id, submission_id, tag = await _seed_full()
    project_label = f"SWA Project {tag}"
    try:
        worker = _with_simulated_send_preview(talent_id, project_id, [
            _mark(
                mention_lid=GUNWANTI_LID, mark_text=f"mark audition take 1 for {project_label}",
                source_message_id=f"swa-noworker-{tag}", media_type="video",
            ),
        ])
        req_id = await swa.dispatch_approve_send(project_id, submission_id)
        await worker
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc is not None
        assert doc["worker_id"] is not None and doc["worker_id"] != "", doc
        assert doc["worker_id"] == swa.PRIMARY_WORKER_ID, doc
    finally:
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_create_scan_request_rejects_none_worker_id():
    """Defensive insertion-time guard, independent of the dispatch layer
    above — even a caller that bypasses submission_whatsapp_actions
    entirely can never persist a NEW ownerless UPLOAD document."""
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
    """Defensive insertion-time guard for the SEND equivalent — same rule,
    same reasoning: this is the exact function that (before the
    worker-affinity fix) silently wrote worker_id=None onto every SEND
    request dispatched through Approve + Send."""
    from agents.modules import media_send as ms
    with pytest.raises(ValueError, match="worker_id is required"):
        await ms.create_send_dispatch_from_approved_plan(
            talent_id="t-y", project_id="p-y", talent_label="Y", project_label="P",
            destination_group="Dest Y",
            assignments=[{"media_role": "take", "take_number": 1, "source_message_id": "src-y", "source_thumbnail_hash": "hash-y"}],
            default_source_type="group", default_group_name="Y x Talentgram",
            form_message=None, submission_id=None, content_hash=None,
            worker_id=None,
        )
    assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": "t-y"}) == 0


# ---------------------------------------------------------------------------
# Worker-side auto-approve-on-success (UPLOAD) — mirrors SEND's own,
# already-covered hook in test_media_send.py's approval-lifecycle suite.
# ---------------------------------------------------------------------------
async def _seed_uploaded_assignment(*, talent_id, project_id, submission_id, source_message_id, source_thumbnail_hash, take_number=1):
    """already_uploaded() (media_assignment.py) is reconciliation-based —
    an assignment row alone isn't enough, it must ALSO have a matching
    submission.media[] entry by source_message_id, or the row is treated
    as stale/excluded. Mirrors both writes together so tests exercise the
    real reconciled-uploaded state, not a shape already_uploaded ignores."""
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


async def _insert_upload_download_done(*, talent_id, project_id, submission_id, download_targets, results):
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "download", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "submission_id": submission_id,
        "download_targets": download_targets, "download_results": results,
        "pending_report_context": {
            "talent_label": "Test Talent", "project_label": "Test Project",
            "already": [], "upload_advisory": "",
            "submission_id": submission_id, "approve_on_success": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
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
# get_action_status
# ---------------------------------------------------------------------------
async def test_get_action_status_unknown_request_reports_not_found():
    result = await swa.get_action_status("no-such-request", "no-such-project", "no-such-submission")
    assert result["found"] is False
    assert result["done"] is True
    assert result["ok"] is False


async def test_get_action_status_reflects_operation_ok_after_finish():
    project_id, talent_id, submission_id, tag = await _seed_full(decision="pending")
    target = {
        "source_message_id": "src-swa-c", "media_role": "take", "take_number": 1,
        "source_thumbnail_hash": "hash-swa-c", "original_label": "Take 1",
    }
    req_id = await _insert_upload_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        download_targets=[target], results=[{"ok": True, "source_message_id": "src-swa-c"}],
    )
    await _seed_uploaded_assignment(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id,
        source_message_id="src-swa-c", source_thumbnail_hash="hash-swa-c",
    )
    try:
        pending = await swa.get_action_status(req_id, project_id, submission_id)
        assert pending["done"] is False
        assert pending["ok"] is None

        assert await orch._process_download_done()
        finished = await swa.get_action_status(req_id, project_id, submission_id)
        assert finished["done"] is True
        assert finished["ok"] is True
        assert finished["decision"] == "approved"
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)


async def test_get_action_status_never_implies_approved_when_decision_write_failed():
    """Production-safety fix (2026-09-20): operation_ok=True means the
    real WhatsApp media operation succeeded — it is NOT a promise that
    set_decision's own separate write also succeeded (that call is
    wrapped in a try/except in media_assignment_worker.py that explicitly
    never rolls the media back on failure). Simulates exactly that: a
    finished, operation_ok=True doc whose submission was NEVER actually
    flipped to "approved". The caller (this repo's frontend) must see
    ok=True but decision="pending", never synthesize "approved" from ok
    alone."""
    project_id, talent_id, submission_id, _ = await _seed_full(decision="pending")
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "project_id": project_id, "submission_id": submission_id,
        "status": ma.STATUS_FINISHED, "operation_ok": True, "report": "UPLOAD COMPLETE",
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        result = await swa.get_action_status(req_id, project_id, submission_id)
        assert result["ok"] is True
        assert result["decision"] == "pending", (
            "decision must reflect the REAL submission state, never be assumed 'approved' from ok=True alone"
        )
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await _cleanup_full(project_id, talent_id, submission_id)
