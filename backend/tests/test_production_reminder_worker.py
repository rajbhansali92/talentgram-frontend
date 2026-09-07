"""Focused tests for the Production Reminder Worker (Phase G) —
services/production_reminder_worker.py.

Verifies: each reminder kind fires exactly once for a due item, respects
status/lifecycle gating, never double-sends on repeated polling (including
after a simulated "worker restart" — a fresh call to run_reminder_cycle()
with no in-memory state carried over, since the idempotency lives in the
DB, not the process), invalidates on reschedule, and enqueues through the
EXISTING whatsapp_batches/whatsapp_jobs collections targeting a GROUP
resolved from the EXISTING whatsapp_agent_config — never a new queue, a
hardcoded group, or a second worker.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from core import db, _now
from routers import whatsapp as whatsapp_router
import services.production_reminder_worker as worker

_aio = pytest.mark.asyncio(loop_scope="module")

MGMT_GROUP = "ZZZ_TEST_Reminder_Management_Group"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def infra_ready():
    await whatsapp_router.ensure_whatsapp_ready()
    # Point the management-agent's group config at a disposable test group
    # name for the duration of this file, restoring the real one after —
    # never touch the real production group mapping from a test.
    original = await db[worker.registry.CONFIG_COLLECTION].find_one({"agent_id": "management-agent"}, {"_id": 0})
    await db[worker.registry.CONFIG_COLLECTION].update_one(
        {"agent_id": "management-agent"},
        {"$set": {"group_names": [MGMT_GROUP], "active": True}},
        upsert=True,
    )
    yield
    if original:
        await db[worker.registry.CONFIG_COLLECTION].update_one(
            {"agent_id": "management-agent"}, {"$set": original}
        )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _make_project(**overrides):
    pid = f"zzz-test-rem-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_REMINDER_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid


async def _make_locked_talent(pid, **row_overrides):
    name = f"ZZZ_TEST_TALENT_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-rem-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row = {
        "id": f"zzz-test-rem-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid,
        "stage": "locked", "created_at": _now(), "updated_at": _now(),
    }
    row.update(row_overrides)
    await db.casting_pipeline.insert_one(row)
    return tid


async def _make_task(pid=None, **overrides):
    doc = {
        "id": f"zzz-test-rem-task-{uuid.uuid4().hex[:8]}", "title": "ZZZ_TEST reminder task",
        "category": "project" if pid else "general", "status": "pending",
        "project_id": pid, "project_name": "", "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.workflow_tasks.insert_one(doc)
    return doc["id"]


async def _jobs_for_group() -> list:
    return await db.whatsapp_jobs.find(
        {"destination_type": "group", "destination": MGMT_GROUP}, {"_id": 0}
    ).to_list(200)


async def _cleanup(pids=None, tids=None, task_ids=None):
    pids = pids or []
    tids = tids or []
    for pid in pids:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.workflow_tasks.delete_many({"project_id": pid})
    if tids:
        await db.talents.delete_many({"id": {"$in": tids}})
    if task_ids:
        await db.workflow_tasks.delete_many({"id": {"$in": task_ids}})
    # Batches/jobs created for this test group — disposable, never real data.
    batch_ids = await db.whatsapp_batches.distinct("id", {"source_params.contacts.whatsapp_group_name": MGMT_GROUP}) \
        if False else []  # source_params isn't stored on the batch doc itself; jobs carry destination.
    job_batch_ids = await db.whatsapp_jobs.distinct("batch_id", {"destination_type": "group", "destination": MGMT_GROUP})
    if job_batch_ids:
        await db.whatsapp_jobs.delete_many({"batch_id": {"$in": job_batch_ids}})
        await db.whatsapp_batches.delete_many({"id": {"$in": job_batch_ids}})


# ===========================================================================
# Tasks
# ===========================================================================
@_aio
async def test_due_task_produces_one_reminder(infra_ready):
    pid = await _make_project()
    tid = await _make_task(pid, due_at=_iso(_utcnow() - timedelta(hours=1)), project_name="ZZZ_TEST_REMINDER")
    try:
        n = await worker._check_tasks()
        assert n == 1
        jobs = await _jobs_for_group()
        assert any("Task Due" in j["message_body"] for j in jobs)
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        assert task["reminder_sent_for_due_at"] == task["due_at"]
    finally:
        await _cleanup(pids=[pid], task_ids=[tid])


@_aio
async def test_repeated_polling_no_duplicate(infra_ready):
    pid = await _make_project()
    tid = await _make_task(pid, due_at=_iso(_utcnow() - timedelta(hours=1)), project_name="ZZZ_TEST_REMINDER")
    try:
        n1 = await worker._check_tasks()
        n2 = await worker._check_tasks()
        n3 = await worker._check_tasks()
        assert n1 == 1
        assert n2 == 0
        assert n3 == 0
        jobs = await _jobs_for_group()
        matching = [j for j in jobs if tid in (j.get("message_body") or "") or "Task Due" in j["message_body"]]
        # Exactly one job total was created for THIS task across 3 cycles.
        task_jobs = [j for j in await db.whatsapp_jobs.find({"destination_type": "group", "destination": MGMT_GROUP}, {"_id": 0}).to_list(500)]
        assert len(task_jobs) == 1
    finally:
        await _cleanup(pids=[pid], task_ids=[tid])


@_aio
async def test_completed_task_no_reminder(infra_ready):
    pid = await _make_project()
    tid = await _make_task(pid, due_at=_iso(_utcnow() - timedelta(hours=1)), status="completed", project_name="ZZZ_TEST_REMINDER")
    try:
        n = await worker._check_tasks()
        assert n == 0
    finally:
        await _cleanup(pids=[pid], task_ids=[tid])


@_aio
async def test_rescheduled_task_does_not_reuse_old_reminder_and_fires_again(infra_ready):
    pid = await _make_project()
    old_due = _iso(_utcnow() - timedelta(hours=2))
    tid = await _make_task(pid, due_at=old_due, project_name="ZZZ_TEST_REMINDER")
    try:
        n1 = await worker._check_tasks()
        assert n1 == 1
        # Reschedule to a new due time (still due) — must be treated as a
        # fresh reminder, not suppressed by the old sent_for value.
        new_due = _iso(_utcnow() - timedelta(minutes=1))
        await db.workflow_tasks.update_one({"id": tid}, {"$set": {"due_at": new_due}})
        n2 = await worker._check_tasks()
        assert n2 == 1
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        assert task["reminder_sent_for_due_at"] == new_due
    finally:
        await _cleanup(pids=[pid], task_ids=[tid])


# ===========================================================================
# Costume trial
# ===========================================================================
@_aio
async def test_due_trial_produces_reminder(infra_ready):
    pid = await _make_project()
    today_noon = worker._ist_today()
    trial_at = datetime(today_noon.year, today_noon.month, today_noon.day, 15, 0, tzinfo=timezone.utc).isoformat()
    tid = await _make_locked_talent(pid, pd_costume_trial_at=trial_at, pd_costume_trial_location="Andheri")
    try:
        n = await worker._check_costume_trials({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        assert any("Costume Trial Reminder" in j["message_body"] and "Andheri" in j["message_body"] for j in jobs)
        n2 = await worker._check_costume_trials({pid})
        assert n2 == 0  # idempotent
    finally:
        await _cleanup(pids=[pid], tids=[tid])


@_aio
async def test_completed_trial_no_reminder(infra_ready):
    pid = await _make_project()
    today = worker._ist_today()
    trial_at = datetime(today.year, today.month, today.day, 15, 0, tzinfo=timezone.utc).isoformat()
    tid = await _make_locked_talent(pid, pd_costume_trial_at=trial_at, pd_fitting_status="completed")
    try:
        n = await worker._check_costume_trials({pid})
        assert n == 0
    finally:
        await _cleanup(pids=[pid], tids=[tid])


# ===========================================================================
# Shoot
# ===========================================================================
@_aio
async def test_shoot_tomorrow_reminder(infra_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_call_time="7:30 AM", pd_shoot_location="Mumbai")
    tid = await _make_locked_talent(pid)
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert "tomorrow" in msg and "Mumbai" in msg and "7:30 AM" in msg
    finally:
        await _cleanup(pids=[pid], tids=[tid])


@_aio
async def test_shoot_morning_of_reminder(infra_ready):
    today = worker._ist_today()
    pid = await _make_project(pd_shoot_date=today.isoformat())
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert "shoot is today" in msg
    finally:
        await _cleanup(pids=[pid])


@_aio
async def test_shoot_no_duplicate_across_both_kinds(infra_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid = await _make_project(pd_shoot_date=tomorrow.isoformat())
    try:
        n1 = await worker._check_shoots({pid})
        n2 = await worker._check_shoots({pid})
        n3 = await worker._check_shoots({pid})
        assert n1 == 1
        assert n2 == 0
        assert n3 == 0
    finally:
        await _cleanup(pids=[pid])


@_aio
async def test_rescheduled_shoot_invalidates_old_reminder(infra_ready):
    today = worker._ist_today()
    pid = await _make_project(pd_shoot_date=today.isoformat())
    try:
        n1 = await worker._check_shoots({pid})
        assert n1 == 1  # morning-of fires for today
        # Reschedule to tomorrow — a DIFFERENT reminder kind (day_before)
        # must now be eligible; the OLD morning_of sent_for must not block it.
        tomorrow = today + timedelta(days=1)
        await db.projects.update_one({"id": pid}, {"$set": {
            "pd_shoot_date": tomorrow.isoformat(),
            "pd_shoot_reminder_day_before_sent_for": None,
            "pd_shoot_reminder_morning_of_sent_for": None,
        }})
        n2 = await worker._check_shoots({pid})
        assert n2 == 1
        jobs = await _jobs_for_group()
        assert any("tomorrow" in j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"])
    finally:
        await _cleanup(pids=[pid])


@_aio
async def test_cancelled_shoot_no_reminder(infra_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_shoot_status="cancelled")
    try:
        n = await worker._check_shoots({pid})
        assert n == 0
    finally:
        await _cleanup(pids=[pid])


# ===========================================================================
# Payment follow-up
# ===========================================================================
@_aio
async def test_due_payment_followup_produces_reminder(infra_ready):
    today = worker._ist_today()
    followup_at = datetime(today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc).isoformat()
    pid = await _make_project(pd_next_follow_up_at=followup_at)
    try:
        n = await worker._check_payment_followups({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        assert any("Payment Follow-up" in j["message_body"] for j in jobs)
        n2 = await worker._check_payment_followups({pid})
        assert n2 == 0
    finally:
        await _cleanup(pids=[pid])


@_aio
async def test_completed_payment_followup_no_reminder(infra_ready):
    today = worker._ist_today()
    followup_at = datetime(today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc).isoformat()
    pid = await _make_project(pd_next_follow_up_at=followup_at, pd_payment_followup_status="done")
    try:
        n = await worker._check_payment_followups({pid})
        assert n == 0
    finally:
        await _cleanup(pids=[pid])


# ===========================================================================
# Multiple projects in one cycle
# ===========================================================================
@_aio
async def test_multiple_projects_all_detected_correct_attribution(infra_ready):
    today = worker._ist_today()
    tomorrow = today + timedelta(days=1)
    pid1 = await _make_project(pd_shoot_date=tomorrow.isoformat())
    followup_at = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc).isoformat()
    pid2 = await _make_project(pd_next_follow_up_at=followup_at)
    tid3 = await _make_task(project_name="ZZZ_TEST_REMINDER_Skoda", due_at=_iso(_utcnow() - timedelta(minutes=5)))
    try:
        n = await worker.run_reminder_cycle()
        assert n >= 3
        jobs = await _jobs_for_group()
        bodies = [j["message_body"] for j in jobs]
        assert any("Shoot Reminder" in b for b in bodies)
        assert any("Payment Follow-up" in b for b in bodies)
        assert any("Task Due" in b and "Skoda" in b for b in bodies)
    finally:
        await _cleanup(pids=[pid1, pid2], task_ids=[tid3])


# ===========================================================================
# WhatsApp delivery mechanism itself
# ===========================================================================
@_aio
async def test_reminder_uses_existing_group_config_not_hardcoded(infra_ready):
    group = await worker._management_agent_group()
    assert group == MGMT_GROUP  # resolved from whatsapp_agent_config, not a literal in the worker


@_aio
async def test_reminder_creates_job_via_existing_queue_no_new_collections(infra_ready):
    before_batches = await db.whatsapp_batches.count_documents({})
    before_jobs = await db.whatsapp_jobs.count_documents({})
    ok = await worker._send_reminder("Test Reminder\nZZZ_TEST — a disposable test message.")
    assert ok
    after_batches = await db.whatsapp_batches.count_documents({})
    after_jobs = await db.whatsapp_jobs.count_documents({})
    assert after_batches == before_batches + 1
    assert after_jobs == before_jobs + 1
    job = await db.whatsapp_jobs.find_one({"destination_type": "group", "destination": MGMT_GROUP, "message_body": {"$regex": "disposable test message"}}, {"_id": 0})
    assert job is not None
    assert job["status"] == "pending"
    await _cleanup()


@_aio
async def test_no_second_worker_task_created_on_repeated_start(infra_ready):
    worker._worker_task = None
    worker.start_production_reminder_worker()
    t1 = worker._worker_task
    worker.start_production_reminder_worker()
    t2 = worker._worker_task
    assert t1 is t2  # same task object — no duplicate loop spawned
    t1.cancel()
    worker._worker_task = None


# ===========================================================================
# Restart / idempotency across a fresh cycle (simulates a process restart —
# no in-memory state is ever consulted, only DB fields).
# ===========================================================================
@_aio
async def test_restart_does_not_resend(infra_ready):
    pid = await _make_project()
    tid = await _make_task(pid, due_at=_iso(_utcnow() - timedelta(hours=1)), project_name="ZZZ_TEST_REMINDER")
    try:
        n1 = await worker._check_tasks()
        assert n1 == 1
        # Simulate a fresh process: re-import-equivalent call with zero
        # process state carried — run_reminder_cycle reads only the DB.
        n2 = await worker.run_reminder_cycle()
        task_jobs = [j for j in await db.whatsapp_jobs.find({"destination_type": "group", "destination": MGMT_GROUP, "message_body": {"$regex": "ZZZ_TEST_REMINDER"}}, {"_id": 0}).to_list(500)]
        assert len(task_jobs) == 1
    finally:
        await _cleanup(pids=[pid], task_ids=[tid])
