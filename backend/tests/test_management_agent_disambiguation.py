"""Management Agent — Resumable Project Disambiguation (2026-09-27).

Previously, when Management Agent's project resolver
(agents/modules/management_agent.py::_resolve_project) found more than one
matching project, it printed a numbered list and then gave up — the
conversation was cleared and the user had to resend the ENTIRE command with
the exact project name. This is the real bug the brief for this task
described: "Mark payment received for Hingle Project" -> ambiguous list ->
a bare "1" reply was NOT understood, silently dropped, or misapplied.

This file exercises the fix: the ambiguous branch now calls into the SAME
shared Interactive Disambiguation Engine (agents/disambiguation.py +
dispatcher.py's _advance_disambiguation) Scouting's own recipient-ambiguity
flow already uses in production, so a bare numbered/named reply resumes
THIS SAME intent with the exact field substituted, and the executor runs
with EXACTLY the same fields a originally-unambiguous command would have
had it named the project precisely.

Dispatches entirely through agents.dispatcher.handle_inbound_message (the
real WhatsApp entry point) against the real local dev DB — never a mocked
resolver — so a passing test here is proof the live parser/resolver/
dispatcher/disambiguation-engine chain actually produces the resumable
behavior end to end, not just that some helper function returns the right
dataclass.

Test data uses a random per-test tag so two runs never collide, and this
module's own project/talent rows are deleted in `finally` blocks, matching
every other file in this test suite's own convention (never a shared
fixture DB).
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
OTHER_AUTH_PHONE = "911234500901"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agents_ready():
    from agents import ensure_agents_ready
    await ensure_agents_ready()


async def _send(text, phone=AUTH_PHONE, is_member=True):
    from agents.dispatcher import handle_inbound_message
    return await handle_inbound_message(
        group_name=GROUP, sender_phone=phone, text=text, sender_is_group_member=is_member,
    )


async def _make_project(tag, suffix, **overrides):
    pid = f"zzz-test-mgmt-dis-{tag}-{suffix}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_DISAMBIG_{tag} {suffix}", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_locked_talent(pid, name=None, budget_total=None):
    name = name or f"ZZZ_TEST_MGMT_DIS_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-mgmt-dis-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    row_id = f"zzz-test-mgmt-dis-row-{uuid.uuid4().hex[:8]}"
    row = {"id": row_id, "project_id": pid, "talent_id": tid, "stage": "locked", "created_at": _now(), "updated_at": _now()}
    if budget_total is not None:
        row["pd_budget_total"] = budget_total
    await db.casting_pipeline.insert_one(row)
    return tid, name


async def _cleanup(pids=None, talent_names=None, client_names=None, phones=None):
    for pid in (pids or []):
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.project_reimbursements.delete_many({"project_id": pid})
        await db.project_crew.delete_many({"project_id": pid})
        await db.workflow_tasks.delete_many({"project_id": pid})
    if talent_names:
        await db.talents.delete_many({"name": {"$in": talent_names}})
    if client_names:
        await db.clients.delete_many({"name": {"$in": client_names}})
    for phone in (phones or [AUTH_PHONE, OTHER_AUTH_PHONE]):
        await db.whatsapp_agent_sessions.delete_many({"phone": phone})
        await db.whatsapp_conversations.delete_many({"phone": phone})
        await db.whatsapp_agent_disambiguation.delete_many({"phone": phone})


async def _make_ambiguous_pair(tag=None):
    """Two real projects sharing one distinctive word — mirrors the proven
    "Tira" / "Tira Talkies" ambiguity shape from
    tests/test_project_matching_hotfix.py's own unit-level test. Querying
    with just the shared tag reliably produces resolution.ambiguous with
    both candidates via the REAL resolve_project_by_name, not a mock."""
    tag = tag or uuid.uuid4().hex[:8]
    pid_a, label_a = await _make_project(tag, "Films")
    pid_b, label_b = await _make_project(tag, "Talkies")
    return tag, (pid_a, label_a), (pid_b, label_b)


# ===========================================================================
# 1-3. Numbered-reply selection (1/2/3) resumes and resolves.
# ===========================================================================
@_aio
async def test_ambiguous_then_reply_1_selects_first_candidate_and_resumes(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        r1 = await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        assert r1.handled
        assert "found multiple projects" in r1.reply.lower()
        assert label_a in r1.reply and label_b in r1.reply

        r2 = await _send("1")
        assert r2.handled
        assert "Reply 1 to confirm" in r2.reply
        assert label_a in r2.reply  # resumed intent shows the PICKED project

        r3 = await _send("1")
        assert "Client payment marked received" in r3.reply

        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert row_a["pd_payment_in_received"] is True
        assert not row_b.get("pd_payment_in_received")
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_ambiguous_then_reply_2_selects_second_candidate(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        r2 = await _send("2")
        assert label_b in r2.reply
        r3 = await _send("1")
        assert "Client payment marked received" in r3.reply

        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert not row_a.get("pd_payment_in_received")
        assert row_b["pd_payment_in_received"] is True
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_ambiguous_three_way_reply_by_name_selects_third_candidate(agents_ready):
    # Pre-existing, documented limitation of the SHARED disambiguation
    # engine (agents/dispatcher.py::_advance_disambiguation), not
    # introduced by this task: a bare "3" is intercepted by
    # parse_confirmation_reply as "cancel" (mirroring the "Reply 1 to
    # confirm, 2 to edit, 3 to cancel" card vocabulary) BEFORE
    # disambiguation.resolve_reply ever sees it, so a 3rd+ candidate can
    # only be picked by name — exactly why format_prompt's own footer says
    # "or simply type the <entity> name." Confirmed unrelated to Management:
    # the same collision would hit Scouting's own recipient disambiguation
    # for any 3-candidate list.
    tag = uuid.uuid4().hex[:8]
    pid_a, label_a = await _make_project(tag, "Films")
    pid_b, label_b = await _make_project(tag, "Talkies")
    pid_c, label_c = await _make_project(tag, "Studios")
    try:
        r1 = await _send(f"Mark invoice raised for ZZZ_DISAMBIG_{tag}")
        assert label_a in r1.reply and label_b in r1.reply and label_c in r1.reply
        r2 = await _send(label_c)
        assert label_c in r2.reply
        r3 = await _send("1")
        assert "Invoice raised" in r3.reply
        row_c = await db.projects.find_one({"id": pid_c}, {"_id": 0})
        assert row_c["pd_invoice_raised"] is True
    finally:
        await _cleanup([pid_a, pid_b, pid_c])


# ===========================================================================
# 4-7. Invalid replies never execute and never silently guess.
# ===========================================================================
@_aio
async def test_invalid_number_zero_does_not_execute(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        r = await _send("0")
        assert "didn't catch that" in r.reply.lower()
        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert not row_a.get("pd_payment_in_received")
        assert not row_b.get("pd_payment_in_received")
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_invalid_number_out_of_range_does_not_execute(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        for bad in ("4", "99"):
            r = await _send(bad)
            assert "didn't catch that" in r.reply.lower()
        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert not row_a.get("pd_payment_in_received")
        assert not row_b.get("pd_payment_in_received")
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_non_number_reply_does_not_execute(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        r = await _send("abc")
        assert "didn't catch that" in r.reply.lower()
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_unrelated_project_name_text_reply_does_not_silently_guess(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        r = await _send("Project A")
        assert "didn't catch that" in r.reply.lower()
        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert not row_a.get("pd_payment_in_received")
        assert not row_b.get("pd_payment_in_received")
    finally:
        await _cleanup([pid_a, pid_b])


# ===========================================================================
# 8. Pending state survives an invalid selection — a valid reply afterward
# still resolves the SAME original pending disambiguation.
# ===========================================================================
@_aio
async def test_pending_state_survives_invalid_selection(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        bad = await _send("xyz")
        assert "didn't catch that" in bad.reply.lower()

        good = await _send("1")
        assert label_a in good.reply
        assert "Reply 1 to confirm" in good.reply
        r3 = await _send("1")
        assert "Client payment marked received" in r3.reply
    finally:
        await _cleanup([pid_a, pid_b])


# ===========================================================================
# 9-10. Idempotency: pending state clears on success; a repeated approval
# does not re-execute the mutation.
# ===========================================================================
@_aio
async def test_pending_state_clears_on_success_and_repeat_1_does_not_repeat_action(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        await _send("1")
        first_exec = await _send("1")
        assert "Client payment marked received" in first_exec.reply

        pending = await db.whatsapp_agent_disambiguation.find_one({"phone": AUTH_PHONE})
        assert pending is None  # cleared, not left dangling

        # A stray extra "1" now has NOTHING pending to resume/re-confirm —
        # it must NOT be silently reinterpreted as "run it again."
        repeat = await _send("1")
        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        assert row_a["pd_payment_in_received"] is True  # still exactly True, not toggled/duplicated
        assert "Client payment marked received" not in (repeat.reply or "") or repeat.handled is False
    finally:
        await _cleanup([pid_a, pid_b])


# ===========================================================================
# 11. Conversation isolation — two different chats never cross-resolve.
# ===========================================================================
@_aio
async def test_two_different_chats_never_cross_resolve(agents_ready):
    tag_a, (pid_a1, label_a1), (pid_a2, label_a2) = await _make_ambiguous_pair()
    tag_b, (pid_b1, label_b1), (pid_b2, label_b2) = await _make_ambiguous_pair()
    try:
        rA = await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag_a}", phone=AUTH_PHONE)
        assert label_a1 in rA.reply and label_a2 in rA.reply

        rB = await _send(f"Mark invoice raised for ZZZ_DISAMBIG_{tag_b}", phone=OTHER_AUTH_PHONE)
        assert label_b1 in rB.reply and label_b2 in rB.reply

        # Chat A picks "1" — must resolve against Chat A's OWN candidates
        # (label_a1), never Chat B's.
        pickA = await _send("1", phone=AUTH_PHONE)
        assert label_a1 in pickA.reply
        assert label_b1 not in pickA.reply and label_b2 not in pickA.reply

        pickB = await _send("2", phone=OTHER_AUTH_PHONE)
        assert label_b2 in pickB.reply
        assert label_a1 not in pickB.reply and label_a2 not in pickB.reply

        confirmA = await _send("1", phone=AUTH_PHONE)
        assert "Client payment marked received" in confirmA.reply
        confirmB = await _send("1", phone=OTHER_AUTH_PHONE)
        assert "Invoice raised" in confirmB.reply

        row_a1 = await db.projects.find_one({"id": pid_a1}, {"_id": 0})
        row_b2 = await db.projects.find_one({"id": pid_b2}, {"_id": 0})
        assert row_a1["pd_payment_in_received"] is True
        assert row_b2["pd_invoice_raised"] is True
    finally:
        await _cleanup([pid_a1, pid_a2, pid_b1, pid_b2], phones=[AUTH_PHONE, OTHER_AUTH_PHONE])


# ===========================================================================
# 12. A brand-new, valid Management command sent while a disambiguation is
# pending — documented, VERIFIED behaviour (checked directly against a raw
# DB read before writing this assertion, not assumed): dispatcher.py's own
# fresh-trigger-match path runs unconditionally BEFORE the "disambiguating"
# step is ever consulted, so a recognised new trigger starts an entirely
# NEW conversation for the new intent (here: management.mark_invoice_raised,
# step="confirming") — replacing, not merging with, the old one. The stale
# disambiguation.py pending doc for this phone is deleted as part of that
# same transition (pre-existing conversation.start_conversation hygiene,
# unmodified by this task), so nothing is left around to later be
# misresolved against an unrelated future reply. The original ambiguous
# command is simply abandoned — the user must resend it if they still want
# it — never silently executed, never cross-applied to the new command's
# fields.
# ===========================================================================
@_aio
async def test_new_command_while_pending_replaces_it_and_clears_old_pending_state(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    pid_c, label_c = await _make_project(uuid.uuid4().hex[:8], "Unrelated")
    try:
        await _send(f"Mark payment received for ZZZ_DISAMBIG_{tag}")
        pending_before = await db.whatsapp_agent_disambiguation.find_one({"phone": AUTH_PHONE})
        assert pending_before is not None

        new_cmd = await _send(f"Mark invoice raised for {label_c}")
        assert "Reply 1 to confirm" in new_cmd.reply
        assert label_c in new_cmd.reply

        pending_after = await db.whatsapp_agent_disambiguation.find_one({"phone": AUTH_PHONE})
        assert pending_after is None  # old pending state cleaned up, not orphaned

        confirm = await _send("1")
        assert "Invoice raised" in confirm.reply

        row_c = await db.projects.find_one({"id": pid_c}, {"_id": 0})
        row_a = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert row_c["pd_invoice_raised"] is True  # the NEW command ran
        assert not row_a.get("pd_payment_in_received")  # the OLD one never did
        assert not row_b.get("pd_payment_in_received")
    finally:
        await _cleanup([pid_a, pid_b, pid_c])


# ===========================================================================
# 13-15. Regressions: unambiguous commands, minor typos, and no-match
# queries all behave exactly as before this change.
# ===========================================================================
@_aio
async def test_unambiguous_command_unchanged(agents_ready):
    pid, label = await _make_project(uuid.uuid4().hex[:8], "Solo")
    try:
        r1 = await _send(f"Mark GST received for {label}")
        assert "Reply 1 to confirm" in r1.reply
        assert label in r1.reply
        r2 = await _send("1")
        assert "GST component marked received" in r2.reply
        row = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert row["pd_gst_component_received"] is True
    finally:
        await _cleanup([pid])


@_aio
async def test_minor_typo_still_auto_resolves_no_disambiguation(agents_ready):
    tag = uuid.uuid4().hex[:8]
    pid, label = await _make_project(tag, "Falcon")
    try:
        # One-character typo against an otherwise-unique label — must
        # still auto-resolve, never open a disambiguation for a typo.
        r1 = await _send(f"Mark invoice sent for ZZZ_DISAMBIG_{tag} Falcn")
        assert "Reply 1 to confirm" in r1.reply
        assert "found multiple" not in r1.reply.lower()
        r2 = await _send("1")
        assert "Invoice sent" in r2.reply
    finally:
        await _cleanup([pid])


@_aio
async def test_nonexistent_project_still_plain_no_match_error(agents_ready):
    r = await _send("Mark payment received for CompletelyUnrelatedNonexistentBrandXyz123")
    assert r.handled
    assert "found multiple" not in r.reply.lower()


# ===========================================================================
# 16-19. Per-intent verification — every intent wired into resumable
# disambiguation individually confirmed: ambiguous -> pick -> resumed
# confirmation -> correct executor runs exactly once.
# ===========================================================================
@_aio
async def test_mark_talent_status_lifecycle_kind_resumes(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        r1 = await _send(f"Mark ZZZ_DISAMBIG_{tag} as shoot scheduled")
        assert label_a in r1.reply and label_b in r1.reply
        r2 = await _send("2")
        assert label_b in r2.reply
        r3 = await _send("1")
        assert "shoot scheduled" in r3.reply.lower()
        row_b = await db.projects.find_one({"id": pid_b}, {"_id": 0})
        assert row_b.get("pd_production_status") == "shoot_scheduled"
    finally:
        await _cleanup([pid_a, pid_b])


@_aio
async def test_mark_talent_status_payment_kind_resumes_with_talent_resolved(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    tid, tname = await _make_locked_talent(pid_b, budget_total=75000)
    try:
        r1 = await _send(f"Mark {tname} payment cleared for ZZZ_DISAMBIG_{tag}")
        assert label_a in r1.reply and label_b in r1.reply
        r2 = await _send("2")
        # Resumed confirmation must show the REAL amount, proving
        # _resolve_talent_for_mark (not just the project) fully re-ran.
        assert "75,000" in r2.reply
        r3 = await _send("1")
        assert "payment marked cleared" in r3.reply.lower()
        row = await db.casting_pipeline.find_one({"project_id": pid_b, "talent_id": tid})
        assert row["pd_payment_status"] == "cleared"
    finally:
        await _cleanup([pid_a, pid_b], [tname])


@_aio
async def test_add_crew_kind_resumes(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    client_name = f"ZZZ_TEST_MGMT_DIS_Client_{uuid.uuid4().hex[:6]}"
    try:
        r1 = await _send(f"Add {client_name} as DOP for ZZZ_DISAMBIG_{tag}")
        assert label_a in r1.reply and label_b in r1.reply
        r2 = await _send("1")
        assert label_a in r2.reply
        assert "Reply 1 to confirm" in r2.reply
        r3 = await _send("1")
        assert "added as" in r3.reply.lower()
        row = await db.project_crew.find_one({"project_id": pid_a})
        assert row is not None
    finally:
        await _cleanup([pid_a, pid_b], client_names=[client_name])


@_aio
async def test_add_task_followup_redirect_kind_resumes(agents_ready):
    tag, (pid_a, label_a), (pid_b, label_b) = await _make_ambiguous_pair()
    try:
        r1 = await _send(f"Remind me to follow up with ZZZ_DISAMBIG_{tag} on 30 December")
        assert label_a in r1.reply and label_b in r1.reply
        r2 = await _send("1")
        assert label_a in r2.reply
        r3 = await _send("1")
        assert "next follow-up" in r3.reply.lower()
        row = await db.projects.find_one({"id": pid_a}, {"_id": 0})
        assert row.get("pd_next_follow_up_at")
    finally:
        await _cleanup([pid_a, pid_b])
