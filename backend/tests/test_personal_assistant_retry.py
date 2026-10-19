"""Personal Assistant — Retry is safe to double-tap (regression for a pre-existing race).

Two overlapping Retry requests for the same partly failed send both read the failed job; the slower one then reached the engine's
`retry_job` after the faster one had re-queued it and got "400 Only failed or skipped jobs can be retried" — a misleading error for
something that had in fact worked. `retry_task` now treats exactly that case (the job is no longer failed because another request
moved it on) as "already handled"; everything else still propagates. The engine's own retry route is deliberately unchanged.

Same harness as test_personal_assistant.py (real FastAPI app + real local dev Mongo, throwaway rows). Nothing is sent."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import asyncio

import pytest
from fastapi import HTTPException

from core import db
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, alice, anon, bob, cleanup, confirm, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")
PROMPT = "Send the Qzx Casting Call to Rivka Zenith and Nehara Zolt for Streaming Now"


async def _partial_send(anon, alice, world):
    """A queued send of two messages: Rivka's was delivered, Nehara's failed. → (task, delivered_job_id, failed_job_id)."""
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await _add_rows(world["amazon"], world["rivka"], world["nehara"])
    t = (await say(anon, alice, PROMPT))["tasks"][0]
    await confirm(anon, alice, t["id"], worker=world["w1"])
    jobs = {j["talent_id"]: j for j in await _jobs([world["rivka"], world["nehara"]])}
    sent, failed = jobs[world["rivka"]]["id"], jobs[world["nehara"]]["id"]
    await db.whatsapp_jobs.update_one({"id": sent}, {"$set": {"status": "sent", "verification_status": "verified"}})
    await db.whatsapp_jobs.update_one({"id": failed}, {"$set": {"status": "failed", "error_message": "number not on WhatsApp"}})
    assert (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]["status"] == "partial"   # persists the status
    return t, sent, failed


async def _status(job_id):
    return (await db.whatsapp_jobs.find_one({"id": job_id}, {"_id": 0, "status": 1}))["status"]


async def test_eight_concurrent_retries_repeatedly_are_all_ok_or_already_handled_and_queue_once(anon, alice, world):
    for _ in range(12):
        t, sent, failed = await _partial_send(anon, alice, world)
        before = await db.whatsapp_jobs.count_documents({"talent_id": {"$in": [world["rivka"], world["nehara"]]}})
        rs = await asyncio.gather(*[anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"]) for _ in range(8)])
        assert all(r.status_code in (200, 409) for r in rs), [(r.status_code, r.text[:100]) for r in rs]           # never the misleading 400
        assert any(r.status_code == 200 for r in rs)
        assert await _status(failed) == "pending" and await _status(sent) == "sent"                                # re-queued once; delivered one untouched
        assert await db.whatsapp_jobs.count_documents({"talent_id": {"$in": [world["rivka"], world["nehara"]]}}) == before     # no new jobs, no duplicates
        await db.whatsapp_jobs.delete_many({"talent_id": {"$in": [world["rivka"], world["nehara"]]}})
        await db.whatsapp_batches.delete_many({"project_id": world["amazon"]})


@pytest.mark.parametrize("state, requeued, handled", [
    ("pending", 1, 0),            # another request re-queued it and it is still waiting
    ("sending", 1, 0),            # …and the worker has already picked it up
    ("sent", 0, 1),               # …and it has since gone out: handled, but this flow cannot prove it re-queued it
    ("sent_unverified", 0, 1),
])
async def test_losing_the_race_is_absorbed_and_counted_in_exactly_one_audit_bucket(anon, alice, world, monkeypatch, state, requeued, handled):
    """Forces the interleaving: another request re-queues the job (and the worker may advance it) right before this one's retry_job runs."""
    import routers.whatsapp as wa
    t, sent, failed = await _partial_send(anon, alice, world)
    real = wa.retry_job

    async def raced(batch_id, job_id, admin):
        await real(batch_id, job_id, admin=admin)                                                       # the other tap wins …
        await db.whatsapp_jobs.update_one({"id": job_id}, {"$set": {"status": state}})                  # … and the worker moves it on …
        return await real(batch_id, job_id, admin=admin)                                                # … so this one is refused with the engine's 400

    monkeypatch.setattr(wa, "retry_job", raced)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 200, r.text
    assert await _status(failed) == state and await _status(sent) == "sent"                           # nothing was changed back or re-queued by the loser
    audits = await db.assistant_audit.find({"task_id": t["id"], "action": "retry"}).to_list(5)
    assert len(audits) == 1
    a = audits[0]
    assert (a["jobs"], a["already_requeued"], a["already_handled_not_requeued"]) == (0, requeued, handled), a      # each job in exactly one bucket


async def test_a_mixed_retry_counts_each_job_in_its_own_bucket(anon, alice, world, monkeypatch):
    """Two failed jobs: one is re-queued normally by this request, one lost a race and was already pending."""
    import routers.whatsapp as wa
    t, sent, failed = await _partial_send(anon, alice, world)
    await db.whatsapp_jobs.update_one({"id": sent}, {"$set": {"status": "failed", "error_message": "chat not opened"}})      # both messages failed
    real = wa.retry_job

    async def raced(batch_id, job_id, admin):
        if job_id == failed:
            await real(batch_id, job_id, admin=admin)
        return await real(batch_id, job_id, admin=admin)

    monkeypatch.setattr(wa, "retry_job", raced)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 200 and await _status(failed) == "pending" and await _status(sent) == "pending"
    a = await db.assistant_audit.find_one({"task_id": t["id"], "action": "retry"})
    assert (a["jobs"], a["already_requeued"], a["already_handled_not_requeued"]) == (1, 1, 0), a


async def test_a_job_that_failed_again_after_the_race_is_not_absorbed(anon, alice, world, monkeypatch):
    """The follow-up read shows 'failed' (re-queued, then failed again): that is not one of the absorbed states → the 400 propagates."""
    import routers.whatsapp as wa
    t, sent, failed = await _partial_send(anon, alice, world)
    real = wa.retry_job

    async def raced(batch_id, job_id, admin):
        await real(batch_id, job_id, admin=admin)
        await db.whatsapp_jobs.update_one({"id": job_id}, {"$set": {"status": "pending"}})
        try:
            return await real(batch_id, job_id, admin=admin)                                            # refused: it is pending …
        finally:
            await db.whatsapp_jobs.update_one({"id": job_id}, {"$set": {"status": "failed"}})           # … but fails again before the follow-up read

    monkeypatch.setattr(wa, "retry_job", raced)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 400 and await db.assistant_audit.count_documents({"task_id": t["id"], "action": "retry"}) == 0


async def test_a_failed_message_is_still_retried_normally(anon, alice, world):
    t, sent, failed = await _partial_send(anon, alice, world)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 200 and await _status(failed) == "pending" and await _status(sent) == "sent"
    again = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])            # nothing failed any more → the existing, honest 409
    assert again.status_code == 409


async def test_engine_retry_still_requeues_failed_and_skipped_and_refuses_everything_else(anon, alice, world):
    t, sent, failed = await _partial_send(anon, alice, world)
    batch = (await db.whatsapp_jobs.find_one({"id": failed}, {"_id": 0, "batch_id": 1}))["batch_id"]
    url = lambda j: f"/api/whatsapp/batches/{batch}/jobs/{j}/retry"
    for state in ("sent", "sending", "pending", "sent_unverified"):                                 # may already have gone out, or is in flight/queued
        await db.whatsapp_jobs.update_one({"id": failed}, {"$set": {"status": state}})
        r = await anon.post(url(failed), headers=alice["headers"])
        assert r.status_code == 400 and await _status(failed) == state, state
    await db.whatsapp_jobs.update_one({"id": failed}, {"$set": {"status": "skipped"}})
    assert (await anon.post(url(failed), headers=alice["headers"])).status_code == 200 and await _status(failed) == "pending"
    await db.whatsapp_jobs.update_one({"id": failed}, {"$set": {"status": "failed"}})
    assert (await anon.post(url(failed), headers=alice["headers"])).status_code == 200 and await _status(failed) == "pending"
    assert (await anon.post(url("no-such-job"), headers=alice["headers"])).status_code == 404
    assert await _status(sent) == "sent"


async def test_the_assistant_never_requeues_a_delivered_or_in_flight_message(anon, alice, world):
    t, sent, failed = await _partial_send(anon, alice, world)
    await db.whatsapp_jobs.update_one({"id": failed}, {"$set": {"status": "sending"}})
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 409 and await _status(failed) == "sending" and await _status(sent) == "sent"


async def test_genuine_errors_still_propagate(anon, alice, world, monkeypatch):
    import routers.whatsapp as wa
    t, sent, failed = await _partial_send(anon, alice, world)

    async def refuses(batch_id, job_id, admin):                     # a 400 while the job is STILL failed is a real refusal, not a race
        raise HTTPException(400, "Only failed or skipped jobs can be retried")
    monkeypatch.setattr(wa, "retry_job", refuses)
    r = await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert r.status_code == 400 and await _status(failed) == "failed"

    async def vanished(batch_id, job_id, admin):
        raise HTTPException(404, "Job not found")
    monkeypatch.setattr(wa, "retry_job", vanished)
    assert (await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])).status_code == 404

    async def boom(batch_id, job_id, admin):
        raise RuntimeError("database exploded")
    monkeypatch.setattr(wa, "retry_job", boom)
    with pytest.raises(RuntimeError):                                # unexpected errors are never swallowed
        await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])
    assert await _status(failed) == "failed"
    assert (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]["status"] == "partial"     # the task is untouched
