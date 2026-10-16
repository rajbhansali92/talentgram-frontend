"""Finalize idempotency (incident 2026-10-14).

A talent's browser aborted the finalize request, so the client could not know
whether finalize had run. A blind repeat of finalize is a "retest": it resets
the admin decision to pending and re-notifies. These tests pin the
`Idempotency-Key` protection:

  * a repeated key replays the stored result with NO side effects;
  * a crash/cancel midway (before or after the status write, before or after
    a notification) is resumed exactly once — never duplicating a
    notification, never flipping first-submission → retest, never resetting
    an admin decision made in between;
  * a NEW key after a completed finalize is a deliberate resubmission and
    behaves exactly like the pre-existing retest path;
  * no header → the legacy path, unchanged.

Real-DB integration tests (local Mongo), same pattern as
test_portal_token_on_finalize.py. Notifications are recorded by replacing the
two outbound seams (`notify_fanout`, `enqueue_internal_whatsapp_notification`)
so the assertions are exact and nothing is actually sent.
"""
import asyncio
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from motor.motor_asyncio import AsyncIOMotorClient

import core
import routers.submissions as rsub
from core import SubmissionUpdateIn, create_or_resume_submission_doc

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MTAG = "TEST_FINALIZE_IDEM_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]

_FORM = {
    "first_name": "First", "last_name": "Last", "height": "5'7\"",
    "location": "Mumbai", "availability": {"status": "yes"}, "budget": {"status": "accept"},
}


class Outbox:
    def __init__(self):
        self.fanouts = []
        self.whatsapp = []
        self.fail_fanout_times = 0

    async def fanout(self, db, **kw):
        self.fanout_kwargs = kw
        if self.fail_fanout_times > 0:
            self.fail_fanout_times -= 1
            raise RuntimeError("simulated crash while notifying")
        self.fanouts.append(kw["type"])

    def whatsapp_enqueue(self, sub, event_type, *a, **k):
        self.whatsapp.append(event_type)
        self.whatsapp_kwargs = k
        if k.get("on_enqueued"):                 # the real task awaits this once the batch is stored
            asyncio.ensure_future(k["on_enqueued"]())


@pytest.fixture
def outbox(monkeypatch):
    ob = Outbox()
    monkeypatch.setattr(rsub, "notify_fanout", ob.fanout)
    monkeypatch.setattr(rsub, "enqueue_internal_whatsapp_notification", ob.whatsapp_enqueue)
    return ob


@pytest.fixture(autouse=True)
def _real_db_for_this_file():
    prior_core, prior_sub = core.db, rsub.db
    core.db = _real_db
    rsub.db = _real_db
    try:
        yield
    finally:
        core.db = prior_core
        rsub.db = prior_sub


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    """A project + a draft submission with a complete legacy-valid form."""
    project = {
        "id": f"{_MTAG}proj_{_uuid.uuid4().hex[:8]}",
        "slug": f"{_MTAG.lower()}slug-{_uuid.uuid4().hex[:6]}",
        "brand_name": "Idempotency Test Project",
    }
    await _real_db.projects.insert_one(project)
    email = f"{_MTAG.lower()}{_uuid.uuid4().hex[:8]}@example.com"
    start = await create_or_resume_submission_doc(
        project, email, "Idem Potent", None, None, {}, created_from="talent_link",
    )
    sid, token = start["id"], start["token"]
    auth = f"Bearer {token}"
    await rsub.submission_update(sid, SubmissionUpdateIn(form_data=dict(_FORM)), authorization=auth)
    ctx = {"project": project, "sid": sid, "auth": auth, "email": email}
    try:
        yield ctx
    finally:
        sub = await _real_db.submissions.find_one({"id": sid}, {"talent_id": 1})
        if sub and sub.get("talent_id"):
            await _real_db.talents.delete_many({"id": sub["talent_id"]})
        await _real_db.talents.delete_many({"email": email})
        await _real_db.submissions.delete_many({"id": sid})
        await _real_db.projects.delete_many({"id": project["id"]})
        await _real_db.casting_pipeline.delete_many({"project_id": project["id"]})


def _key():
    return f"k-{_uuid.uuid4().hex}"


async def _finalize(env, key=None):
    return await rsub.submission_finalize(
        env["sid"], Response(), authorization=env["auth"], idempotency_key=key,
    )


async def _sub(env):
    return await _real_db.submissions.find_one({"id": env["sid"]})


async def _attempt(env, key):
    return await rsub._get_finalize_attempt(env["sid"], key)


async def _admin_decides(env, decision):
    await _real_db.submissions.update_one({"id": env["sid"]}, {"$set": {"decision": decision}})


# ---------------------------------------------------------------------------
# Repeated finalization
# ---------------------------------------------------------------------------
async def test_repeated_key_replays_without_side_effects_and_keeps_admin_decision(env, outbox):
    key = _key()
    first = await _finalize(env, key)
    assert first["ok"] and first["status"] == "submitted" and first["resubmitted"] is False
    assert outbox.fanouts == ["submission_new"] and outbox.whatsapp == ["NEW SUBMISSION"]

    await _admin_decides(env, "shortlisted")
    before = await _sub(env)

    again = await _finalize(env, key)   # the "did it work?" repeat
    assert again["ok"] and again["status"] == "submitted" and again["resubmitted"] is False
    assert again["replayed"] is True
    assert again["talent_id"] == first["talent_id"]

    after = await _sub(env)
    assert after["decision"] == "shortlisted", "a repeat must never reset an established decision"
    assert after["status"] == "submitted", "a repeat must not turn the first submission into a retest"
    assert after["submitted_at"] == before["submitted_at"]
    assert outbox.fanouts == ["submission_new"], "no duplicate admin notification"
    assert outbox.whatsapp == ["NEW SUBMISSION"], "no duplicate internal WhatsApp"


async def test_replay_returns_current_portal_token_and_never_stores_it(env, outbox):
    key = _key()
    first = await _finalize(env, key)
    assert first["portal_token"]
    attempt = await _attempt(env, key)
    assert "portal_token" not in (attempt["result"] or {}), "credential must not be stored on the submission"
    again = await _finalize(env, key)
    assert again["portal_token"] == first["portal_token"]


async def test_budget_decision_preserved_across_finalize_and_replay(env, outbox):
    await rsub.submission_update(
        env["sid"], SubmissionUpdateIn(form_data={"budget": {"status": "custom", "value": "50000"}}),
        authorization=env["auth"],
    )
    key = _key()
    await _finalize(env, key)
    await _finalize(env, key)
    fd = (await _sub(env))["form_data"]
    assert fd["budget"] == {"status": "custom", "value": "50000"}


async def test_no_header_is_the_unchanged_legacy_path(env, outbox):
    first = await _finalize(env, None)
    assert first["status"] == "submitted" and "replayed" not in first
    assert "finalize_attempts" not in (await _sub(env))
    await _admin_decides(env, "approved")
    second = await _finalize(env, None)      # legacy: a repeat is a retest
    assert second["status"] == "updated" and second["resubmitted"] is True
    assert (await _sub(env))["decision"] == "pending"
    assert outbox.fanouts == ["submission_new", "submission_updated"]
    assert "dedupe_key" not in outbox.fanout_kwargs, "legacy path must not change the fan-out call"
    assert outbox.whatsapp_kwargs == {}, "legacy path must not change the WhatsApp enqueue call"


async def test_malformed_key_is_rejected_not_ignored(env, outbox):
    with pytest.raises(HTTPException) as e:
        await _finalize(env, "bad key with spaces")
    assert e.value.status_code == 400
    assert outbox.fanouts == []


# ---------------------------------------------------------------------------
# Deliberate resubmission keeps the existing retest behaviour
# ---------------------------------------------------------------------------
async def test_new_key_after_completed_finalize_is_a_genuine_retest(env, outbox):
    await _finalize(env, _key())
    await _admin_decides(env, "approved")
    await rsub.submission_update(
        env["sid"], SubmissionUpdateIn(form_data={"bio": "edited after approval"}), authorization=env["auth"],
    )
    result = await _finalize(env, _key())     # talent presses "Update Submission"
    assert result["status"] == "updated" and result["resubmitted"] is True
    assert "replayed" not in result
    assert (await _sub(env))["decision"] == "pending", "deliberate update still returns to pending review"
    assert outbox.fanouts == ["submission_new", "submission_updated"]


# ---------------------------------------------------------------------------
# Concurrent requests
# ---------------------------------------------------------------------------
async def test_concurrent_same_key_runs_side_effects_exactly_once(env, outbox):
    key = _key()
    results = await asyncio.gather(
        _finalize(env, key), _finalize(env, key), _finalize(env, key), return_exceptions=True,
    )
    oks = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, HTTPException)]
    assert len(oks) >= 1
    assert all(c.status_code == 409 for c in conflicts), results
    assert outbox.fanouts == ["submission_new"], "exactly one admin notification"
    assert outbox.whatsapp == ["NEW SUBMISSION"]
    sub = await _sub(env)
    assert sub["status"] == "submitted"
    assert sum(1 for a in sub["finalize_attempts"] if a["key"] == key) == 1


async def test_in_progress_attempt_answers_409_until_stale(env, outbox):
    key = _key()
    action, _ = await rsub._claim_finalize_attempt(env["sid"], key)
    assert action == "run"
    with pytest.raises(HTTPException) as e:
        await _finalize(env, key)
    assert e.value.status_code == 409
    assert outbox.fanouts == []
    # Make it stale (the worker died) → the next try resumes and completes.
    await rsub._set_attempt_fields(
        env["sid"], key, {"started_at": datetime.now(timezone.utc) - timedelta(seconds=600)},
    )
    result = await _finalize(env, key)
    assert result["ok"] and result["status"] == "submitted"
    assert outbox.fanouts == ["submission_new"]


# ---------------------------------------------------------------------------
# Interrupted requests: server dies / is cancelled midway
# ---------------------------------------------------------------------------
async def test_crash_while_notifying_resumes_without_duplicate_or_retest_flip(env, outbox):
    key = _key()
    outbox.fail_fanout_times = 1
    with pytest.raises(RuntimeError):
        await _finalize(env, key)          # status written, notification crashed
    att = await _attempt(env, key)
    assert att["state"] == "failed" and att["patched"] is True and att["is_retest"] is False
    assert (await _sub(env))["status"] == "submitted"

    await _admin_decides(env, "shortlisted")   # admin acts while the talent retries

    result = await _finalize(env, key)         # same attempt, resumed
    assert result["ok"] and result["status"] == "submitted" and result["resubmitted"] is False
    sub = await _sub(env)
    assert sub["status"] == "submitted", "resume must not become a retest of its own first half"
    assert sub["decision"] == "shortlisted", "resume must not clobber the admin's decision"
    assert outbox.fanouts == ["submission_new"], "the notification that crashed is re-sent exactly once — recoverable, not lost"
    assert outbox.whatsapp == ["NEW SUBMISSION"], "the stage that had not run yet runs exactly once"
    assert (await _attempt(env, key))["state"] == "done"

    # ...and a further repeat is a pure replay.
    again = await _finalize(env, key)
    assert again["replayed"] is True
    assert outbox.whatsapp == ["NEW SUBMISSION"]


async def test_crash_before_status_write_reruns_cleanly(env, outbox, monkeypatch):
    key = _key()
    real = rsub.notify_fanout
    # Fail validation first: clear a required field, attempt, then fix + retry.
    await rsub.submission_update(env["sid"], SubmissionUpdateIn(form_data={"height": ""}), authorization=env["auth"])
    with pytest.raises(HTTPException) as e:
        await _finalize(env, key)
    assert e.value.status_code == 400
    att = await _attempt(env, key)
    assert att["state"] == "failed" and att["patched"] is False
    assert outbox.fanouts == []
    assert (await _sub(env)).get("status") == "draft"

    await rsub.submission_update(env["sid"], SubmissionUpdateIn(form_data={"height": "5'7\""}), authorization=env["auth"])
    result = await _finalize(env, key)
    assert result["status"] == "submitted"
    assert outbox.fanouts == ["submission_new"]


async def test_cancelled_request_is_resumable(env, outbox):
    """A client disconnect cancels the handler task mid-flight."""
    key = _key()
    gate = asyncio.Event()

    async def slow_fanout(db, **kw):
        gate.set()
        await asyncio.sleep(30)

    rsub.notify_fanout = slow_fanout   # restored by the `outbox` monkeypatch teardown
    task = asyncio.create_task(_finalize(env, key))
    await asyncio.wait_for(gate.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    att = await _attempt(env, key)
    assert att["state"] == "failed" and att["patched"] is True

    rsub.notify_fanout = outbox.fanout
    result = await _finalize(env, key)
    assert result["ok"] and result["status"] == "submitted"
    assert outbox.fanouts == ["submission_new"], "the cancelled notification is delivered once on resume"
    assert outbox.whatsapp == ["NEW SUBMISSION"]


async def test_resumed_genuine_retest_does_not_reset_a_decision_made_in_between(env, outbox):
    await _finalize(env, _key())                       # original submission
    await _admin_decides(env, "approved")
    key = _key()
    outbox.fail_fanout_times = 1
    with pytest.raises(RuntimeError):
        await _finalize(env, key)                      # retest patched (decision→pending), notify crashed
    att = await _attempt(env, key)
    assert att["is_retest"] is True and att["patched"] is True
    assert (await _sub(env))["decision"] == "pending"

    await _admin_decides(env, "shortlisted")           # admin reviews the update
    result = await _finalize(env, key)
    assert result["status"] == "updated" and result["resubmitted"] is True
    assert (await _sub(env))["decision"] == "shortlisted"


# ---------------------------------------------------------------------------
# Recovery: the status endpoint tells THIS attempt apart from an earlier one
# ---------------------------------------------------------------------------
async def test_finalize_status_distinguishes_this_attempt_from_an_earlier_finalize(env, outbox):
    await _finalize(env, _key())                       # earlier, completed finalize
    sub = await _sub(env)
    assert sub["status"] == "submitted"

    mine = _key()
    st = await rsub.submission_finalize_status(env["sid"], mine, authorization=env["auth"])
    assert st == {"state": "none"}, "submission status alone would have said 'submitted'"

    action, _ = await rsub._claim_finalize_attempt(env["sid"], mine)
    st = await rsub.submission_finalize_status(env["sid"], mine, authorization=env["auth"])
    assert st == {"state": "in_progress"}

    await rsub._set_attempt_fields(env["sid"], mine, {"started_at": datetime.now(timezone.utc) - timedelta(seconds=600)})
    st = await rsub.submission_finalize_status(env["sid"], mine, authorization=env["auth"])
    assert st == {"state": "failed"}, "a stale in-progress attempt is reported as retryable"

    await _finalize(env, mine)
    st = await rsub.submission_finalize_status(env["sid"], mine, authorization=env["auth"])
    assert st == {"state": "done"}


async def test_finalize_status_requires_the_submission_token(env, outbox):
    with pytest.raises(HTTPException) as e:
        await rsub.submission_finalize_status(env["sid"], _key(), authorization="Bearer nope")
    assert e.value.status_code == 401


# ---------------------------------------------------------------------------
# Bookkeeping is internal
# ---------------------------------------------------------------------------
async def test_attempt_bookkeeping_is_not_exposed_to_the_talent(env, outbox):
    await _finalize(env, _key())
    put = await rsub.submission_update(
        env["sid"], SubmissionUpdateIn(form_data={"bio": "x"}), authorization=env["auth"],
    )
    assert "finalize_attempts" not in put
    view = await core.build_talent_submission_view(await _real_db.submissions.find_one({"id": env["sid"]}, {"_id": 0}))
    assert "finalize_attempts" not in view


async def test_attempt_history_is_bounded(env, outbox):
    for _ in range(rsub.FINALIZE_ATTEMPTS_KEEP + 3):
        await _finalize(env, _key())
    assert len((await _sub(env))["finalize_attempts"]) == rsub.FINALIZE_ATTEMPTS_KEEP


# ---------------------------------------------------------------------------
# Over real HTTP (FastAPI header binding, route registration, CORS, 409 mapping).
# The tests above call the handler directly, which skips all of that.
# ---------------------------------------------------------------------------
import httpx
from server import app, cors_origins


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _hdrs(env, key=None):
    h = {"Authorization": env["auth"]}
    if key:
        h["Idempotency-Key"] = key
    return h


async def test_http_header_binds_and_repeat_is_a_replay(env, outbox):
    key = _key()
    async with _client() as c:
        r1 = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, key))
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "submitted" and "replayed" not in r1.json()
        await _admin_decides(env, "approved")
        r2 = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, key))
        assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert (await _sub(env))["decision"] == "approved"
    assert outbox.fanouts == ["submission_new"]


async def test_http_without_header_is_legacy_retest(env, outbox):
    async with _client() as c:
        await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env))
        r2 = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env))
    assert r2.json()["status"] == "updated"
    assert outbox.fanouts == ["submission_new", "submission_updated"]


async def test_http_concurrent_same_key_single_effect(env, outbox):
    key = _key()
    async with _client() as c:
        rs = await asyncio.gather(*[
            c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, key)) for _ in range(5)
        ])
    codes = sorted(r.status_code for r in rs)
    assert set(codes) <= {200, 409} and 200 in codes, codes
    assert outbox.fanouts == ["submission_new"] and outbox.whatsapp == ["NEW SUBMISSION"]
    assert (await _sub(env))["status"] == "submitted"


async def test_http_status_route_and_auth(env, outbox):
    key = _key()
    async with _client() as c:
        r = await c.get(f"/api/public/submissions/{env['sid']}/finalize-status", params={"key": key}, headers=_hdrs(env))
        assert r.status_code == 200 and r.json() == {"state": "none"}
        await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, key))
        r = await c.get(f"/api/public/submissions/{env['sid']}/finalize-status", params={"key": key}, headers=_hdrs(env))
        assert r.json() == {"state": "done"}
        r = await c.get(f"/api/public/submissions/{env['sid']}/finalize-status", params={"key": key})
        assert r.status_code == 401
        r = await c.get(f"/api/public/submissions/{env['sid']}/finalize-status", params={"key": key},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


async def test_http_in_progress_maps_to_409_and_bad_key_to_400(env, outbox):
    key = _key()
    await rsub._claim_finalize_attempt(env["sid"], key)
    async with _client() as c:
        r = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, key))
        assert r.status_code == 409
        r = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers=_hdrs(env, "no spaces allowed!"))
        assert r.status_code == 400
        r = await c.post(f"/api/public/submissions/{env['sid']}/finalize", headers={"Idempotency-Key": key})
        assert r.status_code == 401, "no token → still rejected before any claim"


async def test_cors_preflight_allows_the_idempotency_key_header(env):
    origin = next(o for o in cors_origins if o.startswith("http"))
    async with _client() as c:
        r = await c.options(
            f"/api/public/submissions/{env['sid']}/finalize",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization,content-type,idempotency-key"},
        )
    assert r.status_code == 200, r.text
    assert "idempotency-key" in r.headers.get("access-control-allow-headers", "").lower()


# ---------------------------------------------------------------------------
# Notification recoverability: dedupe-keyed sends + marker after the write
# ---------------------------------------------------------------------------
import notifications as notif


@pytest_asyncio.fixture(loop_scope="module")
async def two_admins():
    ids = [f"{_MTAG}admin_{_uuid.uuid4().hex[:6]}" for _ in range(2)]
    await _real_db.users.insert_many([{"id": i, "status": "active", "email": f"{i}@example.com"} for i in ids])
    try:
        yield ids
    finally:
        await _real_db.users.delete_many({"id": {"$in": ids}})


async def test_fanout_with_dedupe_key_is_repeatable_and_repairs_partial_delivery(two_admins):
    key = f"finalize:{_MTAG}{_uuid.uuid4().hex}:admin"
    try:
        everyone = await notif._active_user_ids(_real_db)
        assert set(two_admins) <= set(everyone)
        # A crashed first attempt reached only ONE recipient.
        await _real_db.notifications.insert_one({
            "id": "x1", "recipient_id": two_admins[0], "type": "submission_new", "title": "t", "body": "b",
            "payload": {}, "dedupe_key": key, "created_at": "2026-10-14T00:00:00+00:00",
        })
        await notif.fanout(_real_db, type="submission_new", title="t", body="b", payload={}, dedupe_key=key)
        await notif.fanout(_real_db, type="submission_new", title="t", body="b", payload={}, dedupe_key=key)
        rows = await _real_db.notifications.find({"dedupe_key": key}).to_list(1000)
        per = {}
        for r in rows:
            per[r["recipient_id"]] = per.get(r["recipient_id"], 0) + 1
        assert set(per) == set(everyone), "every active recipient was reached (the missed one repaired)"
        assert set(per.values()) == {1}, "nobody notified twice"
    finally:
        await _real_db.notifications.delete_many({"dedupe_key": key})


async def test_fanout_without_key_is_unchanged(two_admins):
    marker = f"{_MTAG}{_uuid.uuid4().hex}"
    try:
        n1 = await notif.fanout(_real_db, type="submission_new", title=marker, body="b", payload={"m": marker})
        n2 = await notif.fanout(_real_db, type="submission_new", title=marker, body="b", payload={"m": marker})
        assert n1 == n2 and n1 >= 2
        assert await _real_db.notifications.count_documents({"payload.m": marker}) == 2 * n1
        assert await _real_db.notifications.count_documents({"payload.m": marker, "dedupe_key": {"$exists": True}}) == 0
    finally:
        await _real_db.notifications.delete_many({"payload.m": marker})


async def test_crash_in_the_marker_gap_neither_duplicates_nor_loses_the_admin_notification(env, two_admins, monkeypatch):
    """The send succeeded, the process died before the 'notified' marker."""
    wa = []
    monkeypatch.setattr(rsub, "enqueue_internal_whatsapp_notification", lambda *a, **k: wa.append(k))
    key = _key()
    real_set = rsub._set_attempt_fields
    state = {"armed": True}

    async def crash_once_on_notified(sid, k, fields):
        if state["armed"] and "notified" in fields:
            state["armed"] = False
            raise RuntimeError("process died between the send and its marker")
        return await real_set(sid, k, fields)

    monkeypatch.setattr(rsub, "_set_attempt_fields", crash_once_on_notified)
    try:
        with pytest.raises(RuntimeError):
            await _finalize(env, key)
        sid = env["sid"]
        n_after_crash = await _real_db.notifications.count_documents({"payload.submission_id": sid})
        assert n_after_crash >= 2, "the first attempt did deliver"
        assert (await _attempt(env, key))["notified"] is False

        result = await _finalize(env, key)               # resume re-runs the send
        assert result["ok"] and result["status"] == "submitted"
        assert await _real_db.notifications.count_documents({"payload.submission_id": sid}) == n_after_crash, \
            "re-running the send did not duplicate"
        assert (await _attempt(env, key))["notified"] is True
        assert len(wa) == 1 and wa[0]["dedupe_key"].endswith(":wa") and callable(wa[0]["on_enqueued"])
    finally:
        await _real_db.notifications.delete_many({"payload.submission_id": env["sid"]})


async def test_whatsapp_enqueue_is_deduped_and_reports_completion(env):
    sub = await _real_db.submissions.find_one({"id": env["sid"]}, {"_id": 0})
    key = f"finalize:{env['sid']}:{_key()}:wa"
    done = []

    async def on_enqueued():
        done.append(1)

    try:
        for _ in range(2):
            await rsub._enqueue_internal_whatsapp_notification_task(
                sub, "NEW SUBMISSION", dedupe_key=key, on_enqueued=on_enqueued,
            )
        batches = await _real_db.whatsapp_batches.find({"dedupe_key": key}).to_list(10)
        assert len(batches) == 1, "the main notification is stored once"
        assert len(done) == 2, "completion is reported both when it wrote and when it found it already written"
    finally:
        await _real_db.whatsapp_batches.delete_many({"project_id": env["project"]["id"]})
        await _real_db.whatsapp_jobs.delete_many({"source_id": env["project"]["id"]})


async def test_whatsapp_enqueue_without_key_behaves_as_before(env):
    sub = await _real_db.submissions.find_one({"id": env["sid"]}, {"_id": 0})
    try:
        await rsub._enqueue_internal_whatsapp_notification_task(sub, "NEW SUBMISSION")
        await rsub._enqueue_internal_whatsapp_notification_task(sub, "NEW SUBMISSION")
        main = await _real_db.whatsapp_batches.count_documents(
            {"project_id": env["project"]["id"], "template_id": "internal_notification"})
        assert main == 2, "no key → no dedupe, exactly the legacy behaviour"
        assert await _real_db.whatsapp_batches.count_documents(
            {"project_id": env["project"]["id"], "dedupe_key": {"$exists": True}}) == 0
    finally:
        await _real_db.whatsapp_batches.delete_many({"project_id": env["project"]["id"]})
        await _real_db.whatsapp_jobs.delete_many({"source_id": env["project"]["id"]})


# ---------------------------------------------------------------------------
# One finalize at a time per submission (two tabs / devices, different keys)
# ---------------------------------------------------------------------------
async def test_a_different_key_is_turned_away_while_another_attempt_is_running(env, outbox):
    a, b = _key(), _key()
    assert (await rsub._claim_finalize_attempt(env["sid"], a))[0] == "run"
    with pytest.raises(HTTPException) as e:
        await _finalize(env, b)
    assert e.value.status_code == 409
    assert outbox.fanouts == []
    assert (await _sub(env)).get("status") == "draft"
    # ...and once the running attempt is stale (its worker died) the other proceeds.
    await rsub._set_attempt_fields(env["sid"], a, {"started_at": datetime.now(timezone.utc) - timedelta(seconds=900)})
    result = await _finalize(env, b)
    assert result["ok"] and result["status"] == "submitted"
    assert outbox.fanouts == ["submission_new"]


async def test_two_tabs_with_different_keys_concurrently_produce_one_first_submission(env, outbox):
    results = await asyncio.gather(_finalize(env, _key()), _finalize(env, _key()), return_exceptions=True)
    oks = [r for r in results if isinstance(r, dict)]
    assert len(oks) >= 1
    assert oks[0]["status"] == "submitted"
    # a loser is a clean 409 — or, if it started after the winner finished, a legitimate retest
    for r in results:
        assert isinstance(r, dict) or (isinstance(r, HTTPException) and r.status_code == 409), r
    assert outbox.fanouts.count("submission_new") == 1
