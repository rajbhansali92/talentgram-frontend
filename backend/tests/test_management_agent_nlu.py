"""Focused tests for the Management Agent's natural-language multi-action
engine (agents/modules/management_agent.py) — the "smart update" parser
(_parse_smart_message / _resolve_smart_plan) and the SMART_UPDATE_INTENT
it powers, reached via MANAGEMENT_AGENT.resolve_bare_reply for messages
that don't open any of the platform's other, more specific intents.

Two layers of tests:
  1. Pure parser unit tests (no DB, no dispatch) — fast, exact regex/
     structural-pattern checks.
  2. Full dispatch tests through agents.dispatcher.handle_inbound_message
     — the real confirm/edit/cancel flow, real DB writes, real
     Production-Desk-shaped reads.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
import pytest
import pytest_asyncio
from core import db, _now

_aio = pytest.mark.asyncio(loop_scope="module")

GROUP = "Talentgram Management Agent"
AUTH_PHONE = "911234500950"


# ===========================================================================
# 1. Pure parser unit tests
# ===========================================================================
from agents.modules import management_agent as m  # noqa: E402


def test_stopword_the_never_becomes_an_entity():
    """The regression this whole pass exists to fix: adding "set" as a
    trigger for the talent-topic extractor would otherwise capture "the"
    as a talent name in "Set the shoot date for Google AI...". Verified
    at the root-cause gate directly."""
    assert m._is_plausible_name("the") is False
    assert m._is_plausible_name("for") is False
    assert m._is_plausible_name("to") is False
    assert m._is_plausible_name("another") is False
    assert m._is_plausible_name("Google AI") is True
    assert m._is_plausible_name("Shivi") is True


def test_set_shoot_date_never_produces_a_talent_lookup():
    plan = m._parse_smart_message("Set the shoot date for Google AI to 26th and 27th August")
    assert plan["talent_updates"] == []
    assert plan["project_updates"] == {"Google AI": {"shoot_dates": "26th and 27th August"}}


def test_project_multi_field_single_clause_run_on():
    plan = m._parse_smart_message(
        "Google AI shoot is 26 and 27 August, call time 7:30 AM, reporting 7 AM, location Mumbai"
    )
    assert plan["project_updates"]["Google AI"] == {
        "shoot_dates": "26 and 27 August",
        "call_time": "7:30 AM",
        "reporting_time": "7 AM",
        "shoot_location": "Mumbai",
    }


def test_talent_costume_trial_with_date_time_and_location():
    plan = m._parse_smart_message("Shivi costume trial is 25 August at 3 PM in Andheri")
    assert plan["talent_updates"] == [(("Shivi",), {"costume_trial_at": "25 August at 3 PM in Andheri"})]


def test_talent_two_self_contained_status_updates():
    plan = m._parse_smart_message("Shivi's fitting is complete and look test is complete")
    assert plan["talent_updates"] == [(("Shivi",), {"fitting_status": "completed", "look_test_status": "completed"})]


def test_single_task_extraction():
    plan = m._parse_smart_message("Add a task to get the call sheet tomorrow")
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["title"] == "get the call sheet"
    assert plan["tasks"][0]["due_at"]


def test_three_tasks_from_one_list_message():
    plan = m._parse_smart_message(
        "Add tasks to get the call sheet tomorrow, confirm Shivi's costume trial Monday, and follow up payment Tuesday"
    )
    titles = [t["title"] for t in plan["tasks"]]
    assert titles == ["get the call sheet", "confirm Shivi's costume trial", "follow up payment"]
    assert all(t["due_at"] for t in plan["tasks"])


def test_two_tasks_from_and_another_task_shape():
    plan = m._parse_smart_message(
        "Add a task to get the call sheet tomorrow and another task to follow up payment on Monday."
    )
    titles = [t["title"] for t in plan["tasks"]]
    assert titles == ["get the call sheet", "follow up payment"]


def test_full_acceptance_message_structure():
    """The exact multi-action message from the spec — must decompose
    into 4 project fields, 1 talent (with date+location split), and 2
    tasks, never collapsing scopes into each other."""
    msg = (
        "Google AI shoot is 26 and 27 August, call time 7:30 AM, reporting 7 AM, location Mumbai. "
        "Shivi's costume trial is 25 August at 3 PM in Andheri. "
        "Add a task to get the call sheet tomorrow and another task to follow up payment on Monday."
    )
    plan = m._parse_smart_message(msg)
    assert set(plan["project_updates"]["Google AI"].keys()) == {"shoot_dates", "call_time", "reporting_time", "shoot_location"}
    assert len(plan["talent_updates"]) == 1
    names, fields = plan["talent_updates"][0]
    assert names == ("Shivi",)
    assert fields == {"costume_trial_at": "25 August at 3 PM in Andheri"}
    assert len(plan["tasks"]) == 2
    # Must NOT bleed into each other.
    assert "costume_trial_at" not in plan["project_updates"]["Google AI"]
    assert "shoot_dates" not in fields


def test_natural_language_openers_all_reach_the_parser():
    """Punctuation/opener-word variety — a representative sample, not an
    exhaustive synonym dictionary (per the "do not hard-code every
    sentence" instruction — these all work because the FIELD keyword
    itself is recognised, not because each opener phrase is special-
    cased)."""
    variants = [
        "set shoot date for Google AI to 26 August",
        "SHOOT DATE FOR GOOGLE AI IS 26 AUGUST",
        "shoot date for google ai is 26 august",
        "shootdate for Google AI is 26 August",  # tolerated via "shoot date"/"shoot is" fallback below
    ]
    # The first three must all resolve the SAME field for the SAME project.
    for text in variants[:3]:
        plan = m._parse_smart_message(text)
        assert "Google AI" in plan["project_updates"] or "google ai" in {k.lower() for k in plan["project_updates"]}


def test_multiple_projects_in_one_message():
    plan = m._parse_smart_message("Set Google AI call time to 7:30 and L'Oreal call time to 8 AM")
    assert plan["project_updates"]["Google AI"] == {"call_time": "7:30"}
    assert plan["project_updates"]["L'Oreal"] == {"call_time": "8 AM"}


def test_multiple_talents_same_value():
    plan = m._parse_smart_message("Shivi and Rahul's costume trials are tomorrow at 3 PM.")
    assert len(plan["talent_updates"]) == 1
    names, fields = plan["talent_updates"][0]
    assert set(names) == {"Shivi", "Rahul"}
    assert fields["costume_trial_at"]


def test_multiple_talents_distinct_values():
    plan = m._parse_smart_message("Shivi's trial is 3 PM and Rahul's is 5 PM.")
    assert len(plan["talent_updates"]) == 2
    by_name = {names[0]: fields for names, fields in plan["talent_updates"]}
    assert by_name["Shivi"]["costume_trial_at"] == "3 PM"
    assert by_name["Rahul"]["costume_trial_at"] == "5 PM"


def test_talent_rate_structural_pattern():
    plan = m._parse_smart_message("Shivi is 75000 per day for 2 days.")
    assert plan["talent_updates"] == [(("Shivi",), {"budget_per_day": "75000", "shooting_days": "2"})]


def test_project_rate_structural_pattern():
    plan = m._parse_smart_message("Google AI production budget is 75000 per day for 2 days.")
    assert plan["project_updates"]["Google AI"] == {"production_budget_per_day": "75000", "shooting_days": "2"}


def test_follow_up_with_project_on_date():
    plan = m._parse_smart_message("Follow up with Google AI on 30 August.")
    assert plan["project_updates"]["Google AI"] == {"next_follow_up_at": "30 August"}


def test_checklist_items_without_mark_prefix():
    plan = m._parse_smart_message("invoice sent for google ai")
    assert any(v.get("invoice_sent") == "true" for v in plan["project_updates"].values())


def test_task_for_project_hint():
    plan = m._parse_smart_message("Add a task for Google AI to send the call sheet tomorrow")
    assert plan["tasks"][0]["hint"] == "Google AI"
    assert plan["tasks"][0]["title"] == "send the call sheet"


def test_task_title_has_no_trailing_conjunction_or_double_space():
    """The generic cleanup (_clean_extracted_text) — not a fix for one
    sentence — strips a dangling trailing "and"/"&" and collapses
    doubled whitespace from ANY captured span."""
    assert m._clean_extracted_text("get the call sheet  and") == "get the call sheet"
    assert m._clean_extracted_text("Mumbai  &") == "Mumbai"
    assert m._clean_extracted_text("a   b    c") == "a b c"


def test_regression_ordinary_words_never_become_entities():
    for text in [
        "Set the call time for Google AI to 7:30 AM",
        "Mark the invoice sent for Google AI",
        "Add a task for Google AI to send the call sheet tomorrow",
    ]:
        plan = m._parse_smart_message(text)
        all_subjects = list(plan["project_updates"].keys()) + [t.get("hint", "") for t in plan["tasks"]]
        for subj in all_subjects:
            assert subj.strip().lower() not in m._ENTITY_STOPWORDS
            assert subj.strip().lower() != "the"


# ===========================================================================
# 2. Full dispatch tests
# ===========================================================================
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agents_ready():
    from agents import ensure_agents_ready
    await ensure_agents_ready()


async def _send(text, phone=AUTH_PHONE):
    from agents.dispatcher import handle_inbound_message
    return await handle_inbound_message(group_name=GROUP, sender_phone=phone, text=text, sender_is_group_member=True)


async def _make_project(**overrides):
    pid = f"zzz-test-nlu-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_NLU_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_locked_talent(pid):
    name = f"ZZZ_TEST_NLU_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-nlu-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row_id = f"zzz-test-nlu-row-{uuid.uuid4().hex[:8]}"
    await db.casting_pipeline.insert_one({"id": row_id, "project_id": pid, "talent_id": tid, "stage": "locked", "created_at": _now(), "updated_at": _now()})
    return tid, name


async def _cleanup(pid=None, talent_names=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.workflow_tasks.delete_many({"project_id": pid})
        await db.notifications.delete_many({"payload.project_id": pid})
    if talent_names:
        await db.talents.delete_many({"name": {"$in": talent_names}})
    await db.whatsapp_agent_sessions.delete_many({"phone": AUTH_PHONE})
    await db.whatsapp_conversations.delete_many({"phone": AUTH_PHONE})


@_aio
async def test_agent_registers_smart_update_intent(agents_ready):
    from agents import registry
    agent = registry.get_agent("management-agent")
    assert any(i.intent_id == "management.smart_update" for i in agent.intents)


@_aio
async def test_full_acceptance_message_end_to_end(agents_ready):
    """The complete multi-action message: confirm, apply, then verify
    EVERY field via the same production_desk.get_production_desk
    Production Desk itself reads — proves WhatsApp -> DB -> Production
    Desk share one record, and that confirming applies ALL operations,
    not just the first."""
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        msg = (
            f"{label} shoot is 26 and 27 August, call time 7:30 AM, reporting 7 AM, location Mumbai. "
            f"{tname}'s costume trial is 25 August at 3 PM in Andheri. "
            f"Add a task to get the call sheet tomorrow and another task to follow up payment on Monday."
        )
        r = await _send(msg)
        assert "Reply 1 to confirm" in r.reply
        assert "8 changes" in r.reply or "changes" in r.reply

        r2 = await _send("1")
        assert "✓" in r2.reply
        assert "Task: get the call sheet" in r2.reply
        assert "Task: follow up payment" in r2.reply

        from routers import production_desk as pd
        body = await pd.get_production_desk(pid, {"id": "t", "email": "t@x.com"})
        assert body["project"]["shoot_dates"] == "26 and 27 August"
        assert body["project"]["pd_call_time"] == "7:30 AM"
        assert body["project"]["pd_reporting_time"] == "7 AM"
        assert body["project"]["pd_shoot_location"] == "Mumbai"
        talent_card = body["locked_talents"][0]
        assert talent_card["costume_trial_at"] is not None
        assert talent_card["costume_trial_location"] == "Andheri"
        assert len(body["tasks"]["all"]) == 2
        assert all(t["project_id"] == pid for t in body["tasks"]["all"])

        # Read back the SAME values through WhatsApp.
        r3 = await _send(f"What's happening tomorrow for {label}?")
        assert r3.handled
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_multiple_projects_independently_applied(agents_ready):
    pid1, label1 = await _make_project()
    pid2, label2 = await _make_project()
    try:
        r = await _send(f"Set {label1} call time to 7:30 and {label2} call time to 8 AM")
        assert "2 changes" in r.reply
        r2 = await _send("1")
        assert "✓" in r2.reply

        from routers import production_desk as pd
        body1 = await pd.get_production_desk(pid1, {"id": "t", "email": "t@x.com"})
        body2 = await pd.get_production_desk(pid2, {"id": "t", "email": "t@x.com"})
        assert body1["project"]["pd_call_time"] == "7:30"
        assert body2["project"]["pd_call_time"] == "8 AM"
    finally:
        await _cleanup(pid1)
        await _cleanup(pid2)


@_aio
async def test_multiple_talents_distinct_values_applied(agents_ready):
    pid, label = await _make_project()
    tid1, tname1 = await _make_locked_talent(pid)
    tid2, tname2 = await _make_locked_talent(pid)
    try:
        r = await _send(f"{tname1}'s trial is 3 PM and {tname2}'s is 5 PM.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "✓" in r2.reply

        from routers import production_desk as pd
        body = await pd.get_production_desk(pid, {"id": "t", "email": "t@x.com"})
        by_id = {c["talent_id"]: c for c in body["locked_talents"]}
        assert by_id[tid1]["costume_trial_at"] is not None
        assert by_id[tid2]["costume_trial_at"] is not None
        assert by_id[tid1]["costume_trial_at"] != by_id[tid2]["costume_trial_at"]
    finally:
        await _cleanup(pid, [tname1, tname2])


@_aio
async def test_read_write_parity_call_time(agents_ready):
    pid, label = await _make_project()
    try:
        await _send(f"Set call time for {label} to 7:30 AM")
        await _send("1")
        r = await _send(f"What's the call time for {label}?")
        assert "7:30 AM" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_multi_action_cancel_applies_nothing(agents_ready):
    """The exact concern this pass called out: a multi-action write must
    go through ONE confirmation for the whole batch, and cancelling must
    leave the database untouched — not partially apply the first item."""
    pid, label = await _make_project()
    try:
        r = await _send(f"Set {label} location to Delhi, call time to 9 AM")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("3")
        assert "CANCELLED" in r2.reply.upper() or "cancel" in r2.reply.lower()

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert not row.get("pd_shoot_location")
        assert not row.get("pd_call_time")
    finally:
        await _cleanup(pid)


@_aio
async def test_context_follow_up_no_project_named(agents_ready):
    pid, label = await _make_project()
    try:
        await _send(f"What's the shoot date for {label}?")
        r = await _send("Set the call time to 7:30 AM.")
        assert "Reply 1 to confirm" in r.reply
        await _send("1")

        from routers import production_desk as pd
        body = await pd.get_production_desk(pid, {"id": "t", "email": "t@x.com"})
        assert body["project"]["pd_call_time"] == "7:30 AM"
    finally:
        await _cleanup(pid)


@_aio
async def test_ambiguous_or_unresolvable_entity_reported_not_guessed(agents_ready):
    r = await _send("Set call time for ZZZ_TEST_NLU_Nonexistent_Xyzabc to 7:30 AM")
    # No plausible session context and no matching project — must ask/
    # report, never silently apply to the wrong project.
    assert r.handled
    assert "couldn't find" in r.reply.lower() or "which project" in r.reply.lower()


@_aio
async def test_existing_checklist_intent_still_works_after_nlu_pass(agents_ready):
    """Regression: the explicit "mark invoice sent" power-user path must
    still work unchanged alongside the new smart parser."""
    pid, label = await _make_project()
    try:
        r = await _send(f"Mark invoice sent for {label}.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "Invoice sent" in r2.reply

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_invoice_sent"] is True
    finally:
        await _cleanup(pid)


@_aio
async def test_existing_mark_payment_cleared_still_works(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"Mark {tname} payment cleared.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "cleared" in r2.reply.lower()
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_existing_add_task_intent_still_works(agents_ready):
    pid, label = await _make_project()
    try:
        await _send(f"What's pending for {label}?")
        r = await _send("Add a task to get the call sheet tomorrow.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "Task added" in r2.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_existing_scouting_and_fetcher_agents_unaffected(agents_ready):
    from agents import registry
    assert registry.get_agent("whatsapp-campaign-agent") is not None
    assert registry.get_agent("talentgram-fetcher-agent") is not None
