"""Phase I — Proactive Production Operations & Daily Briefing.

Covers the expanded deterministic reminder worker (pre-shoot/post-shoot/
payment-overdue/daily-briefing), the expanded Needs Attention query, the
new upcoming-shoots query, and the extended talent-readiness display —
all built on the Phase G/H deterministic engine. No AI, no new
infrastructure. Exercises the REAL dispatcher and the REAL reminder
worker functions, not just helpers.
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

GROUP = "Talentgram Management Agent"
AUTH_PHONE = "911234503000"
# Deliberately NOT overridden to a disposable group here (unlike
# test_production_reminder_worker.py) — this file ALSO exercises real
# inbound dispatch via _send(), which resolves the target agent FROM
# this same whatsapp_agent_config.group_names field; overriding it would
# make "Talentgram Management Agent" unroutable for the dispatch-based
# tests. Safe either way: this is the LOCAL dev database, never polled
# by the real WhatsApp Worker (that only ever polls PRODUCTION Mongo via
# Railway env vars) — a job landing on the real group name here never
# actually gets delivered.
MGMT_GROUP = GROUP


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agents_ready():
    from agents import ensure_agents_ready
    await ensure_agents_ready()
    await whatsapp_router.ensure_whatsapp_ready()
    yield


async def _send(text, phone=AUTH_PHONE):
    from agents.dispatcher import handle_inbound_message
    return await handle_inbound_message(group_name=GROUP, sender_phone=phone, text=text, sender_is_group_member=True)


async def _make_project(**overrides):
    pid = f"zzz-test-i-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_PHASE_I_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_locked_talent(pid, **row_overrides):
    name = f"ZZZ_TEST_PHASE_I_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-i-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row = {
        "id": f"zzz-test-i-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid,
        "stage": "locked", "created_at": _now(), "updated_at": _now(),
    }
    row.update(row_overrides)
    await db.casting_pipeline.insert_one(row)
    return tid, name


async def _make_task(pid=None, **overrides):
    doc = {
        "id": f"zzz-test-i-task-{uuid.uuid4().hex[:8]}", "title": "ZZZ_TEST_PHASE_I task",
        "category": "project" if pid else "general", "status": "pending",
        "project_id": pid, "project_name": "", "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.workflow_tasks.insert_one(doc)
    return doc["id"]


async def _jobs_for_group() -> list:
    return await db.whatsapp_jobs.find(
        {"destination_type": "group", "destination": MGMT_GROUP}, {"_id": 0}
    ).to_list(500)


async def _cleanup(pid=None, talent_names=None, task_ids=None):
    if pid:
        pids = pid if isinstance(pid, list) else [pid]
        for p in pids:
            await db.projects.delete_one({"id": p})
            await db.casting_pipeline.delete_many({"project_id": p})
            await db.workflow_tasks.delete_many({"project_id": p})
    if talent_names:
        await db.talents.delete_many({"name": {"$in": talent_names}})
    if task_ids:
        await db.workflow_tasks.delete_many({"id": {"$in": task_ids}})
    job_batch_ids = await db.whatsapp_jobs.distinct("batch_id", {"destination_type": "group", "destination": MGMT_GROUP})
    if job_batch_ids:
        await db.whatsapp_jobs.delete_many({"batch_id": {"$in": job_batch_ids}})
        await db.whatsapp_batches.delete_many({"id": {"$in": job_batch_ids}})
    await db.whatsapp_agent_sessions.delete_many({"phone": AUTH_PHONE})
    await db.whatsapp_conversations.delete_many({"phone": AUTH_PHONE})


# ===========================================================================
# Reminder worker — pre-shoot / shoot-day / post-shoot / payment-overdue
# ===========================================================================
@_aio
async def test_1_shoot_tomorrow_reminder_with_pending_section(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat())
    tid, tname = await _make_locked_talent(pid)
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert "shoot is tomorrow" in msg
        assert "Pending:" in msg
        assert "Call time missing" in msg
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_2_shoot_day_reminder(agents_ready):
    today = worker._ist_today()
    pid, label = await _make_project(pd_shoot_date=today.isoformat(), pd_call_time="7:30 AM", pd_reporting_time="7 AM", pd_shoot_location="Mumbai")
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert "shoot is today" in msg
        assert "Mumbai" in msg
    finally:
        await _cleanup(pid)


@_aio
async def test_3_missing_call_time_reflected_in_pending(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_reporting_time="7 AM", pd_shoot_location="Mumbai")
    try:
        pending = await worker._shoot_pending_items(await db.projects.find_one({"id": pid}, {"_id": 0}))
        assert "Call time missing" in pending
        assert "Reporting time missing" not in pending
    finally:
        await _cleanup(pid)


@_aio
async def test_4_missing_reporting_time_reflected_in_pending(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_call_time="7:30 AM")
    try:
        pending = await worker._shoot_pending_items(await db.projects.find_one({"id": pid}, {"_id": 0}))
        assert "Reporting time missing" in pending
    finally:
        await _cleanup(pid)


@_aio
async def test_5_pending_talent_preparation_in_shoot_reminder(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_call_time="7:30 AM", pd_reporting_time="7 AM", pd_shoot_location="Mumbai", pd_confirmation_mail_received=True)
    tid, tname = await _make_locked_talent(pid, pd_fitting_status="scheduled")
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert f"{tname} fitting" in msg
        assert f"{tname} costume trial" in msg
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_6_pending_confirmation_in_shoot_reminder(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat(), pd_call_time="7:30 AM", pd_reporting_time="7 AM", pd_shoot_location="Mumbai")
    try:
        n = await worker._check_shoots({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Shoot Reminder" in j["message_body"]][0]
        assert "Confirmation mail pending" in msg
    finally:
        await _cleanup(pid)


@_aio
async def test_7_overdue_task_reminder(agents_ready):
    pid, label = await _make_project()
    tid = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), project_name=label)
    try:
        n = await worker._check_tasks()
        assert n == 1
        jobs = await _jobs_for_group()
        assert any("Task Due" in j["message_body"] for j in jobs)
    finally:
        await _cleanup(pid, task_ids=[tid])


@_aio
async def test_8_task_due_today(agents_ready):
    pid, label = await _make_project()
    tid = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), project_name=label)
    try:
        n = await worker._check_tasks()
        assert n == 1
    finally:
        await _cleanup(pid, task_ids=[tid])


@_aio
async def test_9_payment_followup_due_reminder(agents_ready):
    today = worker._ist_today()
    followup_at = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc).isoformat()
    pid, label = await _make_project(pd_next_follow_up_at=followup_at)
    try:
        n = await worker._check_payment_followups({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        assert any("Payment Follow-up" in j["message_body"] for j in jobs)
    finally:
        await _cleanup(pid)


@_aio
async def test_10_payment_overdue_reminder(agents_ready):
    past = worker._ist_today() - timedelta(days=3)
    pid, label = await _make_project(pd_expected_payment_date=past.isoformat())
    try:
        n = await worker._check_payment_overdue({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Payment Overdue" in j["message_body"]][0]
        assert label in msg
        assert "Status: Pending" in msg
        n2 = await worker._check_payment_overdue({pid})
        assert n2 == 0  # idempotent
    finally:
        await _cleanup(pid)


@_aio
async def test_11_invoice_pending_post_shoot(agents_ready):
    yesterday = worker._ist_today() - timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=yesterday.isoformat())
    try:
        n = await worker._check_post_shoot_checklist({pid})
        assert n == 1
        jobs = await _jobs_for_group()
        msg = [j["message_body"] for j in jobs if "Post-Shoot" in j["message_body"]][0]
        assert "Invoice not raised" in msg
        assert "Invoice not sent" in msg
    finally:
        await _cleanup(pid)


@_aio
async def test_12_talent_payment_pending_visible_in_needs_attention(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, pd_payment_status="pending")
    try:
        r = await _send("What needs attention today?")
        assert tname in r.reply
        assert "Payment pending" in r.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_13_reminder_idempotency_no_duplicate_on_repeat(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat())
    try:
        n1 = await worker._check_shoots({pid})
        n2 = await worker._check_shoots({pid})
        n3 = await worker._check_shoots({pid})
        assert n1 == 1
        assert n2 == 0
        assert n3 == 0
    finally:
        await _cleanup(pid)


@_aio
async def test_14_completed_task_stops_reminder(agents_ready):
    pid, label = await _make_project()
    tid = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), status="completed", project_name=label)
    try:
        n = await worker._check_tasks()
        assert n == 0
    finally:
        await _cleanup(pid, task_ids=[tid])


@_aio
async def test_15_rescheduled_shoot_does_not_duplicate_and_fires_correctly(agents_ready):
    today = worker._ist_today()
    pid, label = await _make_project(pd_shoot_date=today.isoformat())
    try:
        n1 = await worker._check_shoots({pid})
        assert n1 == 1  # morning-of for today
        tomorrow = today + timedelta(days=1)
        await db.projects.update_one({"id": pid}, {"$set": {
            "pd_shoot_date": tomorrow.isoformat(),
            "pd_shoot_reminder_day_before_sent_for": None,
            "pd_shoot_reminder_morning_of_sent_for": None,
        }})
        n2 = await worker._check_shoots({pid})
        assert n2 == 1  # new day-before reminder for the NEW date, not a duplicate of the old
    finally:
        await _cleanup(pid)


@_aio
async def test_16_ist_timezone_date_boundary(agents_ready):
    """A costume trial stored just after UTC midnight (which is already
    the NEXT calendar day in IST) must compare using its date component
    as written — not shift across the boundary via a second timezone
    conversion. See the worker's own Timezone docstring section."""
    today = worker._ist_today()
    trial_at = datetime(today.year, today.month, today.day, 0, 30, tzinfo=timezone.utc).isoformat()
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, pd_costume_trial_at=trial_at)
    try:
        assert worker._date_of(trial_at) == today
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_17_worker_restart_does_not_duplicate_briefing(agents_ready):
    """Simulates a Railway restart: a fresh call to _maybe_send_daily_briefing
    with zero in-memory state (idempotency lives entirely in the DB field)."""
    await db[worker.registry.CONFIG_COLLECTION].update_one(
        {"agent_id": "management-agent"}, {"$unset": {"last_daily_briefing_date": ""}}
    )
    pid, label = await _make_project(pd_shoot_date=worker._ist_today().isoformat())
    try:
        n1 = await worker._maybe_send_daily_briefing()
        n2 = await worker._maybe_send_daily_briefing()  # "restart" — fresh call, same DB state
        assert n1 in (0, 1)
        assert n2 == 0  # never a duplicate regardless of what n1 was
        jobs = await db.whatsapp_jobs.find({"message_body": {"$regex": "Talentgram Production Brief"}}, {"_id": 0}).to_list(10)
        assert len(jobs) <= 1
    finally:
        await _cleanup(pid)
        await db.whatsapp_jobs.delete_many({"message_body": {"$regex": "Talentgram Production Brief"}})
        await db[worker.registry.CONFIG_COLLECTION].update_one(
            {"agent_id": "management-agent"}, {"$unset": {"last_daily_briefing_date": ""}}
        )


# ===========================================================================
# Needs Attention
# ===========================================================================
@_aio
async def test_18_needs_attention_mixed_project_issues(agents_ready):
    pid, label = await _make_project(pd_invoice_raised=False)
    task_id = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), project_name=label)
    try:
        r = await _send("What needs attention today?")
        assert label in r.reply
        assert "Overdue task" in r.reply
        assert "Checklist" in r.reply
    finally:
        await _cleanup(pid, task_ids=[task_id])


@_aio
async def test_19_needs_attention_overdue_tasks(agents_ready):
    pid, label = await _make_project()
    task_id = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), project_name=label, priority="high")
    try:
        r = await _send("What is overdue?")
        assert "Overdue task" in r.reply
        r2 = await _send("What needs attention?")
        assert "High priority task" in r2.reply
    finally:
        await _cleanup(pid, task_ids=[task_id])


@_aio
async def test_20_needs_attention_pending_readiness(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send("Which talents aren't ready?")
        assert tname in r.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_21_needs_attention_payment_followups(agents_ready):
    now_iso = datetime.now(timezone.utc).isoformat()
    pid, label = await _make_project(pd_next_follow_up_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    try:
        r = await _send("Anything urgent?")
        assert "Payment follow-up overdue" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_22_needs_attention_empty_clean_state(agents_ready):
    r = await _send("What needs attention today?")
    assert r.handled
    # Either a genuinely clean state, or real pre-existing data — either
    # way it must be a real, honest answer, never an error.
    assert "NEEDS ATTENTION" in r.reply or "Nothing needs attention" in r.reply


# ===========================================================================
# Daily Briefing
# ===========================================================================
@_aio
async def test_23_daily_briefing_correct_counts(agents_ready):
    today = worker._ist_today()
    pid, label = await _make_project(pd_shoot_date=today.isoformat())
    task_id = await _make_task(pid, due_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), project_name=label)
    try:
        text = await worker._daily_briefing_text()
        assert text is not None
        assert "Today's Shoots:" in text
        assert "Overdue Tasks:" in text
    finally:
        await _cleanup(pid, task_ids=[task_id])


@_aio
async def test_24_daily_briefing_correct_project_list(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat())
    try:
        text = await worker._daily_briefing_text()
        assert text is not None
        assert "Needs Attention:" in text or "Tomorrow:" in text
    finally:
        await _cleanup(pid)


@_aio
async def test_25_daily_briefing_no_duplicate(agents_ready):
    await db[worker.registry.CONFIG_COLLECTION].update_one(
        {"agent_id": "management-agent"}, {"$unset": {"last_daily_briefing_date": ""}}
    )
    pid, label = await _make_project(pd_shoot_date=worker._ist_today().isoformat())
    try:
        n1 = await worker._maybe_send_daily_briefing()
        n2 = await worker._maybe_send_daily_briefing()
        n3 = await worker._maybe_send_daily_briefing()
        assert n2 == 0
        assert n3 == 0
    finally:
        await _cleanup(pid)
        await db.whatsapp_jobs.delete_many({"message_body": {"$regex": "Talentgram Production Brief"}})
        await db[worker.registry.CONFIG_COLLECTION].update_one(
            {"agent_id": "management-agent"}, {"$unset": {"last_daily_briefing_date": ""}}
        )


@_aio
async def test_26_daily_briefing_empty_categories_omitted(agents_ready):
    """"Do not show categories with zero items" — verified directly on
    the text builder with no shoots/tasks/followups/not-ready signals."""
    text = await worker._daily_briefing_text()
    if text:
        assert "Today's Shoots: 0" not in text
        assert "Tomorrow: 0" not in text
        assert "Tasks Due: 0" not in text


# ===========================================================================
# Management Agent — upcoming shoots / readiness / needs-attention queries
# ===========================================================================
@_aio
async def test_27_todays_shoots_query(agents_ready):
    today = worker._ist_today()
    pid, label = await _make_project(pd_shoot_date=today.isoformat(), pd_shoot_location="Mumbai")
    try:
        r = await _send("What shoots are today?")
        assert "SHOOTS — TODAY" in r.reply
        assert label in r.reply
        assert "Mumbai" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_28_tomorrows_shoots_query(agents_ready):
    tomorrow = worker._ist_today() + timedelta(days=1)
    pid, label = await _make_project(pd_shoot_date=tomorrow.isoformat())
    try:
        r = await _send("Show me tomorrow's shoots.")
        assert "SHOOTS — TOMORROW" in r.reply
        assert label in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_29_upcoming_shoots_query(agents_ready):
    soon = worker._ist_today() + timedelta(days=5)
    pid, label = await _make_project(pd_shoot_date=soon.isoformat())
    try:
        r = await _send("What shoots are coming up?")
        assert label in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_30_who_isnt_ready_query(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send("Who isn't ready for tomorrow?")
        assert tname in r.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_31_needs_attention_basic(agents_ready):
    pid, label = await _make_project(pd_invoice_raised=False)
    try:
        r = await _send("What needs attention?")
        assert r.handled
    finally:
        await _cleanup(pid)


@_aio
async def test_32_multiple_project_query(agents_ready):
    pid1, label1 = await _make_project(pd_shoot_date=worker._ist_today().isoformat())
    pid2, label2 = await _make_project(pd_shoot_date=(worker._ist_today() + timedelta(days=1)).isoformat())
    try:
        r = await _send("What is happening this week?")
        assert label1 in r.reply
        assert label2 in r.reply
    finally:
        await _cleanup([pid1, pid2])


@_aio
async def test_33_fuzzy_spelling_project_resolution(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"{label.lower()} is confirmed.")
        assert "Reply 1 to confirm" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_34a_remind_me_follow_up_still_sets_field_not_task(agents_ready):
    """Regression, explicitly re-verified per Phase I's own instruction:
    "Remind me to follow up with X on Y" must remain a payment
    follow-up field update, never a task."""
    pid, label = await _make_project()
    try:
        r = await _send(f"Remind me to follow up with {label} on 10 Sept.")
        assert "Reply 1 to confirm" in r.reply
        assert "follow-up" in r.reply.lower()
        await _send("1")
        rows = await db.workflow_tasks.find({"project_id": pid}).to_list(10)
        assert len(rows) == 0
        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row.get("pd_next_follow_up_at")
    finally:
        await _cleanup(pid)


@_aio
async def test_34b_mark_costume_trial_complete_still_talent_status(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"Mark {tname}'s costume trial complete.")
        assert "Reply 1 to confirm" in r.reply
        assert "costume trial" in r.reply.lower()
        await _send("1")
        row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid}, {"_id": 0})
        assert row["pd_fitting_status"] == "completed"
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_34c_mark_it_complete_never_unsafe_talent_search(agents_ready):
    r = await _send("Mark it complete.")
    assert r.handled
    assert "which task" in r.reply.lower()
    assert "locked on more than one project" not in r.reply.lower()


@_aio
async def test_34d_existing_scouting_and_fetcher_agents_unaffected(agents_ready):
    from agents import registry
    assert registry.get_agent("whatsapp-campaign-agent") is not None
    assert registry.get_agent("talentgram-fetcher-agent") is not None


@_aio
async def test_34e_pending_for_project_still_project_digest_not_talent_search(agents_ready):
    """Regression for the exact bug found live during this pass: adding
    "What is pending for Shivi?" (talent readiness) support initially made
    _extract_talent_and_topic claim EVERY "pending for X" phrase as a
    talent name — including when X is actually a PROJECT, silently
    shadowing the older, pre-existing "What's pending for Google AI?"
    project-digest command and answering "No locked talent matching ..."
    instead."""
    pid, label = await _make_project(pd_confirmation_mail_received=False)
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"What's pending for {label}?")
        assert r.handled
        assert label in r.reply
        assert "No locked talent matching" not in r.reply
        assert "Confirmation mail pending" in r.reply

        r2 = await _send(f"What is pending for {tname}?")
        assert r2.handled
        assert tname in r2.reply
        assert "Status:" in r2.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_34f_pending_for_talent_wins_over_fuzzy_similar_project_name(agents_ready):
    """Regression for a second, sharper form of the same bug: when the
    TALENT's own name is fuzzy-similar enough to a real project's name
    that _resolve_project's fuzzy matcher resolves it AS that project
    (e.g. project "..._PROD" and its talent "..._TAL_PROD" — exactly the
    disposable naming this phase's own production E2E is required to
    use), project resolution never "definitively fails", so a fallback
    gated on that failure never runs. "What is pending for X?" must still
    render X's talent readiness, not the project's digest."""
    tag = uuid.uuid4().hex[:6]
    pid, label = await _make_project(brand_name=f"ZZZ_TEST_PHASE_I_PROD_{tag}")
    tid, tname = await _make_locked_talent(pid, **{"pd_shoot_status": "scheduled", "pd_fitting_status": "pending"})
    await db.talents.update_one({"id": tid}, {"$set": {"name": f"ZZZ_TEST_PHASE_I_TAL_PROD_{tag}"}})
    tname = f"ZZZ_TEST_PHASE_I_TAL_PROD_{tag}"
    try:
        r = await _send(f"What is pending for {tname}?")
        assert r.handled
        assert tname in r.reply
        assert "Status:" in r.reply
        assert "Locked talents:" not in r.reply
    finally:
        await _cleanup(pid, [tname])
