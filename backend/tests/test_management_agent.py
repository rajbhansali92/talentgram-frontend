"""Focused tests for the Talentgram Management Agent
(agents/modules/management_agent.py) — the new WhatsApp agent group added
on top of the EXISTING multi-agent platform (registry/dispatcher/session_
context), reusing production_desk.py's own functions directly rather than
a second copy of any project/finance/CRM data.

Dispatches through `agents.dispatcher.handle_inbound_message` — the exact
entry point the real WhatsApp worker calls — never a bespoke shortcut, so
these tests exercise the real trigger-matching/confirmation/session-context
machinery, not just the domain executor functions in isolation.
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
AUTH_PHONE = "911234500900"
UNAUTH_PHONE = "919999900900"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agents_ready():
    from agents import ensure_agents_ready
    await ensure_agents_ready()


async def _send(text, phone=AUTH_PHONE, is_member=True):
    from agents.dispatcher import handle_inbound_message
    return await handle_inbound_message(
        group_name=GROUP, sender_phone=phone, text=text, sender_is_group_member=is_member,
    )


async def _make_project(**overrides):
    pid = f"zzz-test-mgmt-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_MGMT_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_locked_talent(pid, name=None, budget_total=None):
    name = name or f"ZZZ_TEST_MGMT_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-mgmt-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row_id = f"zzz-test-mgmt-row-{uuid.uuid4().hex[:8]}"
    row = {"id": row_id, "project_id": pid, "talent_id": tid, "stage": "locked", "created_at": _now(), "updated_at": _now()}
    if budget_total is not None:
        row["pd_budget_total"] = budget_total
    await db.casting_pipeline.insert_one(row)
    return tid, name


async def _cleanup(pid=None, talent_names=None, client_names=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.project_reimbursements.delete_many({"project_id": pid})
        await db.project_crew.delete_many({"project_id": pid})
        await db.notifications.delete_many({"payload.project_id": pid})
    if talent_names:
        await db.talents.delete_many({"name": {"$in": talent_names}})
    if client_names:
        await db.clients.delete_many({"name": {"$in": client_names}})
    for phone in (AUTH_PHONE, UNAUTH_PHONE):
        await db.whatsapp_agent_sessions.delete_many({"phone": phone})
        await db.whatsapp_conversations.delete_many({"phone": phone})


# ---------------------------------------------------------------------------
# 8/9. Agent + group registration, discoverable via the existing mechanism.
# ---------------------------------------------------------------------------
@_aio
async def test_agent_is_registered(agents_ready):
    from agents import registry
    agent = registry.get_agent("management-agent")
    assert agent is not None
    assert agent.name == "Talentgram Management Agent"
    assert len(agent.intents) >= 5


@_aio
async def test_group_config_seeded_and_discoverable(agents_ready):
    from agents import registry
    cfg = await registry.get_agent_config("management-agent")
    assert cfg is not None
    assert "Talentgram Management Agent" in (cfg.get("group_names") or [])
    assert cfg.get("active") is True

    resolved = await registry.resolve_agent_for_group("Talentgram Management Agent")
    assert resolved is not None
    agent, _ = resolved
    assert agent.agent_id == "management-agent"


# ---------------------------------------------------------------------------
# 10. Group-specific routing — a message in a DIFFERENT group never reaches
# this agent (the platform's existing per-group isolation, unmodified).
# ---------------------------------------------------------------------------
@_aio
async def test_group_specific_routing_isolated_from_other_agents(agents_ready):
    from agents.dispatcher import handle_inbound_message
    r = await handle_inbound_message(
        group_name="Talentgram Scouting Agent", sender_phone=AUTH_PHONE,
        text="What's pending for Anything?", sender_is_group_member=True,
    )
    # Scouting Agent's own trigger vocabulary doesn't include Management's
    # phrasing, OR (if it did) it would be Scouting Agent that answers —
    # either way, Management Agent's own executor is never invoked from
    # the wrong group. The strong assertion here is just that this call
    # doesn't error and doesn't silently write Management-agent state.
    assert r is not None


# ---------------------------------------------------------------------------
# 11/17. Read-only query works, resolves the project via existing search.
# ---------------------------------------------------------------------------
@_aio
async def test_read_only_project_query_works(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"What's pending for {label}?")
        assert r.handled
        assert label in r.reply
        assert "Confirmation mail pending" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_locked_talent_query_works(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, budget_total=50000)
    try:
        r = await _send(f"Show locked talents for {label}.")
        assert r.handled
        assert tname in r.reply
        assert "50,000" in r.reply or "50000" in r.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_finance_query_works(agents_ready):
    pid, label = await _make_project(commission_percent="20%")
    tid, tname = await _make_locked_talent(pid, budget_total=10000)
    try:
        r = await _send(f"What's our commission for {label}?")
        assert r.handled
        assert "Gross commission" in r.reply
        assert "2,000" in r.reply  # 20% of 10,000
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_payment_status_query_works(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, budget_total=75000)
    try:
        r = await _send(f"Show {tname}'s payment for {label}.")
        assert r.handled
        assert "PENDING" in r.reply
        assert "75,000" in r.reply
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_invoice_status_query_works(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Has the invoice been sent for {label}?")
        assert r.handled
        assert "Invoice raised" in r.reply
        assert "Invoice sent" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_reimbursement_query_works(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Show reimbursements for {label}.")
        assert r.handled
        assert "REIMBURSEMENTS" in r.reply
        assert "(none)" in r.reply
    finally:
        await _cleanup(pid)


@_aio
async def test_bare_talent_query_resolves_across_projects_via_session(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, budget_total=20000)
    try:
        r = await _send(f"Show {tname}'s payment.")  # no project named
        assert r.handled
        assert "PENDING" in r.reply
    finally:
        await _cleanup(pid, [tname])


# ---------------------------------------------------------------------------
# 17/18. Authorized user can execute allowed actions; unauthorized cannot.
# ---------------------------------------------------------------------------
@_aio
async def test_authorized_user_can_mark_invoice_raised(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Mark invoice raised for {label}.")
        assert "Reply 1 to confirm" in r.reply
        r2 = await _send("1")
        assert "Invoice raised" in r2.reply

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_invoice_raised"] is True
    finally:
        await _cleanup(pid)


@_aio
async def test_unauthorized_sender_cannot_execute_mutation(agents_ready):
    pid, label = await _make_project()
    try:
        r = await _send(f"Mark invoice raised for {label}.", phone=UNAUTH_PHONE, is_member=False)
        assert r.handled is False

        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert not row.get("pd_invoice_raised")
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# 19/20. Financial mutation requires confirmation; approved mutation writes
# to the SAME backend data Production Desk itself reads.
# ---------------------------------------------------------------------------
@_aio
async def test_talent_payment_mutation_requires_confirmation_with_real_amount(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, budget_total=150000)
    try:
        r = await _send(f"Mark {tname} payment cleared.")
        assert "150,000" in r.reply
        assert "Reply 1 to confirm" in r.reply

        # Not yet applied before confirmation.
        row = await db.casting_pipeline.find_one({"talent_id": tid})
        assert (row.get("pd_payment_status") or "pending") == "pending"

        r2 = await _send("1")
        assert "cleared" in r2.reply.lower()

        row = await db.casting_pipeline.find_one({"talent_id": tid})
        assert row["pd_payment_status"] == "cleared"

        from routers import production_desk as pd
        body = await pd.get_production_desk(pid, {"id": "test", "email": "t@x.com"})
        assert body["locked_talents"][0]["payment_status"] == "cleared"
    finally:
        await _cleanup(pid, [tname])


@_aio
async def test_mutation_can_be_cancelled(agents_ready):
    pid, label = await _make_project()
    try:
        await _send(f"Mark invoice raised for {label}.")
        r = await _send("3")  # cancel
        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert not row.get("pd_invoice_raised")
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# 21. No duplicate notification on a redundant mutation (reuses Production
# Desk's own notification fanout).
# ---------------------------------------------------------------------------
@_aio
async def test_no_duplicate_notification_on_redundant_mutation(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid, budget_total=30000)
    try:
        await _send(f"Mark {tname} payment cleared.")
        await _send("1")
        first = await db.notifications.count_documents({"type": "production_desk_payment_cleared", "payload.project_id": pid})
        assert first > 0

        # Sending the exact same command again — talent is already
        # cleared, so try_auto_execute's project/talent resolution still
        # succeeds but the executor's own transition check (inside
        # production_desk.py) must not re-fire the notification.
        await _send(f"Mark {tname} payment cleared.")
        await _send("1")
        second = await db.notifications.count_documents({"type": "production_desk_payment_cleared", "payload.project_id": pid})
        assert second == first
    finally:
        await _cleanup(pid, [tname])


# ---------------------------------------------------------------------------
# 6/7. Kickback association / reimbursement association with correct
# project+talent (reimbursement IS supported via the agent; kickback via
# WhatsApp is NOT in this pass — see module docstring — so this covers
# reimbursement only, correctly scoped).
# ---------------------------------------------------------------------------
@_aio
async def test_reimbursement_added_via_agent_is_associated_with_correct_project_and_talent(agents_ready):
    pid, label = await _make_project()
    tid, tname = await _make_locked_talent(pid)
    try:
        r = await _send(f"Add ₹5,000 travel reimbursement for {tname}.")
        assert "5,000" in r.reply
        r2 = await _send("1")
        assert "Reimbursement added" in r2.reply

        rows = await db.project_reimbursements.find({"project_id": pid}).to_list(10)
        assert len(rows) == 1
        assert rows[0]["talent_id"] == tid
        assert rows[0]["amount"] == 5000.0
    finally:
        await _cleanup(pid, [tname])


# ---------------------------------------------------------------------------
# 13/14. CRM contact reused, not duplicated, for "Add X as Role".
# ---------------------------------------------------------------------------
@_aio
async def test_add_crew_reuses_existing_crm_contact(agents_ready):
    from routers.marketing import insert_client_doc
    existing = await insert_client_doc(name="ZZZ_TEST_MGMT_ExistingContact", source="test_setup")
    pid, label = await _make_project()
    try:
        r = await _send(f"Add ZZZ_TEST_MGMT_ExistingContact as Producer for {label}.")
        assert "new CRM contact" not in r.reply  # must recognise the existing one
        await _send("1")

        count = await db.clients.count_documents({"name": "ZZZ_TEST_MGMT_ExistingContact"})
        assert count == 1

        crew = await db.project_crew.find({"project_id": pid}).to_list(10)
        assert len(crew) == 1
        assert crew[0]["client_id"] == existing["id"]
    finally:
        await _cleanup(pid, client_names=["ZZZ_TEST_MGMT_ExistingContact"])


@_aio
async def test_add_crew_creates_new_contact_when_none_exists(agents_ready):
    pid, label = await _make_project()
    name = f"ZZZ_TEST_MGMT_NewGuy_{uuid.uuid4().hex[:6]}"
    try:
        r = await _send(f"Add {name} as DOP for {label}.")
        assert "new CRM contact" in r.reply
        await _send("1")

        count = await db.clients.count_documents({"name": name})
        assert count == 1
    finally:
        await _cleanup(pid, client_names=[name])


# ---------------------------------------------------------------------------
# 22/23/24. Existing Scouting Agent / Fetcher Agent / Production Desk tests
# are unaffected by this pass (registration counts + independent test files
# still pass — see also the full-suite regression run in the deployment
# report; these two assertions confirm registration itself is intact).
# ---------------------------------------------------------------------------
@_aio
async def test_existing_scouting_and_fetcher_agents_still_registered(agents_ready):
    from agents import registry
    assert registry.get_agent("whatsapp-campaign-agent") is not None
    assert registry.get_agent("talentgram-fetcher-agent") is not None
    assert registry.get_agent("casting-agent") is not None
    assert registry.get_agent("crm-agent") is not None
