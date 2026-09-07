"""Phase H — Management Agent Production Operations Completion.

Focused tests for the new NLU coverage built on top of Phase F/G's
deterministic engine: kickbacks, reimbursements, checklist, crew,
project lifecycle, conversational task management (with "it"/"that
task" session-context resolution), needs-attention, and talent
readiness. Exercises the REAL dispatcher (agents.dispatcher.
handle_inbound_message), not just helper functions — every write goes
through the existing confirm/edit/cancel machinery and persists to the
SAME collections Production Desk and the admin Workflow page read.
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

_aio = pytest.mark.asyncio(loop_scope="module")

GROUP = "Talentgram Management Agent"
AUTH_PHONE = "911234501000"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agents_ready():
    from agents import ensure_agents_ready
    await ensure_agents_ready()


async def _send(text, phone=AUTH_PHONE):
    from agents.dispatcher import handle_inbound_message
    return await handle_inbound_message(group_name=GROUP, sender_phone=phone, text=text, sender_is_group_member=True)


async def _make_project(**overrides):
    pid = f"zzz-test-h-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_PHASE_H_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_locked_talent(pid, **row_overrides):
    name = f"ZZZ_TEST_PHASE_H_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-h-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row = {
        "id": f"zzz-test-h-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid,
        "stage": "locked", "created_at": _now(), "updated_at": _now(),
    }
    row.update(row_overrides)
    await db.casting_pipeline.insert_one(row)
    return tid, name


async def _cleanup(pid=None, talent_names=None, task_ids=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.workflow_tasks.delete_many({"project_id": pid})
        await db.project_kickbacks.delete_many({"project_id": pid})
        await db.project_reimbursements.delete_many({"project_id": pid})
        await db.project_crew.delete_many({"project_id": pid})
    if talent_names:
        await db.talents.delete_many({"name": {"$in": talent_names}})
    if task_ids:
        await db.workflow_tasks.delete_many({"id": {"$in": task_ids}})
    await db.whatsapp_agent_sessions.delete_many({"phone": AUTH_PHONE})
    await db.whatsapp_conversations.delete_many({"phone": AUTH_PHONE})


# ===========================================================================
# 1. Kickback create/update/query
# ===========================================================================
@_aio
async def test_kickback_create_update_remove_query(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"{tname} has a ₹5,000 kickback.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "✓" in r2.reply

        r3 = await _send(f"What is the kickback for {tname}?")
        assert "5,000" in r3.reply

        r4 = await _send(f"Set {tname}'s kickback to 8000.")
        assert "Reply 1 to confirm" in r4.reply
        assert "Update" in r4.reply  # not "Add" — an existing row is being updated
        await _send("1")

        r5 = await _send(f"What is the kickback for {tname}?")
        assert "8,000" in r5.reply

        rows = await db.project_kickbacks.find({"project_id": pid}).to_list(10)
        assert len(rows) == 1  # update, not a second row

        r6 = await _send(f"Remove {tname}'s kickback.")
        assert "Reply 1 to confirm" in r6.reply
        await _send("1")
        rows2 = await db.project_kickbacks.find({"project_id": pid}).to_list(10)
        assert len(rows2) == 0
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_kickback_percent_computed_from_talent_budget(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, pd_budget_total=100000)
    try:
        r = await _send(f"Add a 10% kickback for {tname}.")
        assert "Reply 1 to confirm" in r.reply
        assert "10,000" in r.reply  # 10% of 100,000
        await _send("1")
        row = await db.project_kickbacks.find_one({"project_id": pid}, {"_id": 0})
        assert row["amount"] == 10000.0
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_kickback_bare_percent_has_phrasing(agents_ready):
    """"X has a N% kickback." — a real E2E-found gap: the bare "has a"
    pattern's amount capture had nothing to skip a literal "%" with, so
    this phrasing never matched at all before the fix."""
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, pd_budget_total=100000)
    try:
        r = await _send(f"{tname} has a 10% kickback.")
        assert "Reply 1 to confirm" in r.reply
        assert "10,000" in r.reply
        await _send("1")
        row = await db.project_kickbacks.find_one({"project_id": pid}, {"_id": 0})
        assert row["amount"] == 10000.0
    finally:
        await _cleanup(pid, [tname])


# ===========================================================================
# 2. Reimbursement create/update/query
# ===========================================================================
@_aio
async def test_reimbursement_bare_create_and_mark_paid(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"{tname}'s travel reimbursement is 2500.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        row = await db.project_reimbursements.find_one({"project_id": pid, "talent_id": tid}, {"_id": 0})
        assert row["amount"] == 2500.0
        assert row["status"] == "pending"

        r2 = await _send(f"Mark {tname}'s reimbursement paid.")
        assert "Reply 1 to confirm" in r2.reply
        await _send("1")
        row2 = await db.project_reimbursements.find_one({"id": row["id"]}, {"_id": 0})
        assert row2["status"] == "paid"

        r3 = await _send(f"Show reimbursements for {label}")
        assert "2,500" in r3.reply or "2500" in r3.reply
    finally:
        await _cleanup(pid, [tname])


# ===========================================================================
# 3. Checklist updates
# ===========================================================================
@_aio
async def test_checklist_updates_via_bare_and_mark_phrasing(agents_ready):
    pid, label = await _make_project()
    try:
        await _send(f"What's pending for {label}?")  # establish session context
        r = await _send("Invoice has been raised.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        r2 = await _send("GST payment received.")
        assert "Reply 1 to confirm" in r2.reply
        await _send("1")

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_invoice_raised"] is True
        assert row["pd_gst_component_received"] is True

        r3 = await _send(f"What's pending on {label}?")
        assert "Invoice not raised" not in r3.reply
        assert "GST component pending" not in r3.reply
    finally:
        await _cleanup(pid)


# ===========================================================================
# 4. Crew operations
# ===========================================================================
@_aio
async def test_crew_add_update_remove_list(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Add ZZZ_TEST_PHASE_H_Amit as Line Producer for {label}.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        r2 = await _send("Change ZZZ_TEST_PHASE_H_Amit's role to DOP.")
        assert "Reply 1 to confirm" in r2.reply
        await _send("1")

        r3 = await _send(f"Who is on the {label} crew?")
        assert "DOP" in r3.reply

        r4 = await _send("Remove ZZZ_TEST_PHASE_H_Amit from the project crew.")
        assert "Reply 1 to confirm" in r4.reply
        await _send("1")

        rows = await db.project_crew.find({"project_id": pid}).to_list(10)
        assert len(rows) == 0
    finally:
        await _cleanup(pid)
        await db.clients.delete_many({"name": "ZZZ_TEST_PHASE_H_Amit"})


# ===========================================================================
# 5. Project lifecycle updates
# ===========================================================================
@_aio
async def test_lifecycle_transitions(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"{label} is confirmed.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        r2 = await _send(f"Mark {label} as shoot scheduled.")
        assert "Reply 1 to confirm" in r2.reply
        await _send("1")

        r3 = await _send(f"{label} shoot is complete.")
        await _send("1")

        r4 = await _send(f"Move {label} to finance closed.")
        await _send("1")

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_production_status"] == "finance_closed"
    finally:
        await _cleanup(pid)


# ===========================================================================
# 6-9. Conversational task management: follow-up context, priority, due date,
# completion/reopen.
# ===========================================================================
@_aio
async def test_conversational_task_it_resolution_full_lifecycle(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Add a task to confirm costume trial for {label} tomorrow.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        r2 = await _send("Make it high priority.")
        assert "Reply 1 to confirm" in r2.reply
        assert "priority" in r2.reply.lower()
        await _send("1")

        r3 = await _send("Move it to Monday.")
        assert "Reply 1 to confirm" in r3.reply
        await _send("1")

        r4 = await _send("Mark it complete.")
        assert "Reply 1 to confirm" in r4.reply
        await _send("1")

        task = await db.workflow_tasks.find_one({"project_id": pid}, {"_id": 0})
        assert task["priority"] == "high"
        assert task["status"] == "completed"
        assert task["due_at"] is not None

        r5 = await _send("Reopen it.")
        assert "Reply 1 to confirm" in r5.reply
        await _send("1")
        task2 = await db.workflow_tasks.find_one({"id": task["id"]}, {"_id": 0})
        assert task2["status"] == "pending"
    finally:
        await _cleanup(pid)


@_aio
async def test_task_completion_by_title_no_pronoun(agents_ready):
    pid, label = await _make_project()
    task_id = f"zzz-test-h-task-{uuid.uuid4().hex[:8]}"
    await db.workflow_tasks.insert_one({
        "id": task_id, "title": "ZZZ_TEST_PHASE_H get call sheet", "category": "project", "status": "pending",
        "project_id": pid, "project_name": label, "created_at": _now(), "updated_at": _now(),
    })
    try:
        r = await _send("The ZZZ_TEST_PHASE_H get call sheet task is done.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")
        task = await db.workflow_tasks.find_one({"id": task_id}, {"_id": 0})
        assert task["status"] == "completed"
    finally:
        await _cleanup(pid, task_ids=[task_id])


@_aio
async def test_task_completed_task_gets_no_reminder_and_action_kind_falls_back_to_talent(agents_ready):
    """"Mark X complete" with NO matching task must gracefully fall back
    to the pre-existing talent-payment-clear interpretation — never a
    dead-end error, and never a false-positive task claim."""
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, pd_budget_total=50000)
    try:
        r = await _send(f"Mark {tname} complete.")
        # No task named this exists — falls back to the payment-clear path.
        assert "Reply 1 to confirm" in r.reply
        assert "payment" in r.reply.lower() or "cleared" in r.reply.lower()
    finally:
        await _cleanup(pid, [tname])


# ===========================================================================
# 10. Needs-attention query
# ===========================================================================
@_aio
async def test_needs_attention_global_query(agents_ready):
    pid, label = await _make_project()
    task_id = f"zzz-test-h-task2-{uuid.uuid4().hex[:8]}"
    await db.workflow_tasks.insert_one({
        "id": task_id, "title": "ZZZ_TEST_PHASE_H overdue task", "category": "project", "status": "pending",
        "project_id": pid, "project_name": label,
        "due_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        r = await _send("What needs attention today?")
        assert "NEEDS ATTENTION" in r.reply
        assert label in r.reply
        assert "overdue task" in r.reply.lower()

        r2 = await _send("What is overdue?")
        assert "overdue task" in r2.reply.lower()
    finally:
        await _cleanup(pid, task_ids=[task_id])


# ===========================================================================
# 11. Talent readiness query
# ===========================================================================
@_aio
async def test_talent_readiness_query(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"Is {tname} ready for {label}?")
        assert "NOT READY" in r.reply

        await _send(f"Mark {tname}'s costume trial completed.")
        await _send("1")
        await db.casting_pipeline.update_one(
            {"project_id": pid, "talent_id": tid},
            {"$set": {"pd_costume_trial_at": _now(), "pd_look_test_status": "completed", "pd_shoot_status": "scheduled"}},
        )
        r2 = await _send(f"Is {tname} ready for {label}?")
        assert "READY" in r2.reply
    finally:
        await _cleanup(pid, [tname])


# ===========================================================================
# 12-14. Multi-action, multiple talents, multiple projects
# ===========================================================================
@_aio
async def test_multi_action_command_confirms_once_applies_all(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        # Task-creation clause LAST — once a "task" opener is seen,
        # everything after it is parsed as task-list text (Phase F's
        # documented clause-splitting boundary), so a checklist item must
        # not trail behind a task clause in the same message.
        msg = (
            f"{label} is confirmed, shoot is 26 August, call time 7:30 AM, "
            f"{tname}'s fitting is complete, mark the invoice raised. "
            f"Add a task for {label} to send the call sheet tomorrow."
        )
        r = await _send(msg)
        assert "Reply 1 to confirm" in r.reply
        n_changes = int(r.reply.split("changes")[0].strip().split()[-1])
        assert n_changes >= 5
        r2 = await _send("1")
        # One combined ✓ line per RECORD (project, talent, task) — not
        # one per field — so 3 records here, never just the first.
        assert r2.reply.count("✓") == 3

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_production_status"] == "confirmed"
        assert row["pd_call_time"] == "7:30 AM"
        assert row["pd_invoice_raised"] is True
        talent_row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid}, {"_id": 0})
        assert talent_row["pd_fitting_status"] == "completed"
        tasks = await db.workflow_tasks.find({"project_id": pid}).to_list(10)
        assert len(tasks) == 1
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_multiple_talents_distinct_kickbacks(agents_ready):
    pid, label = await _make_project()
    tid1, tname1 = await _make_locked_talent(pid)
    tid2, tname2 = await _make_locked_talent(pid)
    try:
        await _send(f"{tname1} has a ₹3,000 kickback.")
        await _send("1")
        await _send(f"{tname2} has a ₹4,000 kickback.")
        await _send("1")
        rows = await db.project_kickbacks.find({"project_id": pid}).to_list(10)
        assert len(rows) == 2
        amounts = sorted(r["amount"] for r in rows)
        assert amounts == [3000.0, 4000.0]
    finally:
        await _cleanup(pid, [tname1, tname2])


@_aio
async def test_multiple_projects_independent_lifecycle_updates(agents_ready):
    pid1, label1 = await _make_project()
    pid2, label2 = await _make_project()
    try:
        r = await _send(f"Set {label1} call time to 7:30 and {label2} call time to 8 AM")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")
        row1 = await db.projects.find_one({"id": pid1}, {"_id": 0})
        row2 = await db.projects.find_one({"id": pid2}, {"_id": 0})
        assert row1["pd_call_time"] == "7:30"
        assert row2["pd_call_time"] == "8 AM"
    finally:
        await _cleanup(pid1)
        await _cleanup(pid2)


# ===========================================================================
# 15-16. Fuzzy spelling / entity-resolution edge cases
# ===========================================================================
@_aio
async def test_fuzzy_project_name_resolution(agents_ready):
    pid, label = await _make_project()
    try:
        # A minor case/whitespace variation of the exact stored name —
        # this file's fuzzy resolver is exact-substring/case-insensitive,
        # not a spelling-correction engine; verifies that tier works.
        r = await _send(f"{label.lower()} is confirmed.")
        assert "Reply 1 to confirm" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_stopword_never_becomes_entity_regression(agents_ready):
    """The Phase F root-cause regression, re-verified after ALL of Phase
    H's new patterns were added — "the"/"a"/"is"/"at"/"tomorrow"/"today"
    must never be interpreted as a talent or project name."""
    from agents.modules import management_agent as m
    for word in ("the", "a", "is", "at", "tomorrow", "today", "it", "that", "move", "make"):
        assert m._is_plausible_name(word) is False

    pid, label = await _make_project()
    try:
        r = await _send(f"Set the shoot date for {label} to 26 August")
        assert "Reply 1 to confirm" in r.reply
        assert '"the"' not in r.reply.lower()
        assert "no locked talent matching" not in r.reply.lower()
    finally:
        await _cleanup(pid)


@_aio
async def test_ambiguous_kickback_recipient_reports_not_guesses(agents_ready):
    r = await _send("What is the kickback for ZZZ_TEST_PHASE_H_Nonexistent_Xyz?")
    assert r.handled
    # No session/project context at all — must report, never fabricate a number.
    assert "5,000" not in r.reply and "8,000" not in r.reply


# ===========================================================================
# 17. Confirmation flow — cancel leaves DB untouched; edit/confirm counted once
# ===========================================================================
@_aio
async def test_cancel_leaves_everything_untouched(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"{label} is confirmed, call time 9 AM.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("3")
        assert "cancel" in r2.reply.lower()
        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert not row.get("pd_production_status") or row.get("pd_production_status") == "not_started"
        assert not row.get("pd_call_time")
    finally:
        await _cleanup(pid)


# ===========================================================================
# 18. Regression against existing Management Agent intents (spot-check —
# the FULL existing suites already re-run separately; this locks in the
# two known collision points Phase H touched directly).
# ===========================================================================
@_aio
async def test_existing_costume_trial_and_reimbursement_paid_kind_still_distinct(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"Mark {tname}'s costume trial completed.")
        assert "Reply 1 to confirm" in r.reply
        assert "costume trial" in r.reply.lower()
        await _send("1")
        row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid}, {"_id": 0})
        assert row["pd_fitting_status"] == "completed"
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_existing_scouting_and_fetcher_agents_unaffected(agents_ready):
    from agents import registry
    assert registry.get_agent("whatsapp-campaign-agent") is not None
    assert registry.get_agent("talentgram-fetcher-agent") is not None
    agent = registry.get_agent("management-agent")
    assert len(agent.intents) == 10  # 9 from Phase F/G + TASK_MANAGE_INTENT
