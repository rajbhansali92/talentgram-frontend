"""Concurrent Task Engine (2026-08-05) — tests for whatsapp_agent_tasks and
dispatcher.py's reply-to-message routing branch. All logic-level: no live
WhatsApp needed, since handle_inbound_message accepts replied_to_message_id
directly and the worker's job is only to capture/report a real WhatsApp
message id into that same parameter (see whatsapp-worker/inbound.py's
_post_task_sent / sender.py's sent_message_id capture, tested separately via
the fake-page suite in whatsapp-worker/tests/test_group_routing.py).

Matches the sprint's explicit test matrix: several concurrent tasks
approved in random order via reply-to-message with no cross-contamination,
multiple users proceeding independently, and a regression guard proving a
bare digit with no reply-to-message still resolves via the pre-existing
conversation.py single-slot path untouched.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import random
import string
import uuid

import pytest

from core import db, _now
from agents import modules as agent_modules
from agents import registry, tasks
from agents.dispatcher import handle_inbound_message

agent_modules.register_all()

AGENT_ID = "casting-agent"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _phone() -> str:
    return "91" + str(uuid.uuid4().int)[:9]


def _rand_suffix(n: int = 6) -> str:
    # Letters only, deliberately — the NL move parser's name extraction can
    # truncate a name at a bare digit (it also has to recognize ordinal/
    # bulk-selector syntax like "Move 2,5,9-10"), so a digit-bearing
    # uniquifying suffix can silently shorten a seeded talent's name and
    # make otherwise-distinct talents collide on a shared prefix. Not
    # relevant to what this file is testing (task isolation), so sidestep
    # it entirely rather than depending on that unrelated extraction detail.
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


async def _use_test_config(group_name: str):
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [group_name],
        "allowed_senders": [],
        "security_mode": "group_members",
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, doc, upsert=True)
    return original


async def _restore_config(original):
    if original is None:
        await db[registry.CONFIG_COLLECTION].delete_one({"agent_id": AGENT_ID})
    else:
        original.pop("_id", None)
        await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, original, upsert=True)


async def _seed_project(brand_name: str = None) -> str:
    pid = f"test-cte-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid,
        "brand_name": brand_name or f"Test Project {pid[-6:]}",
        "status": "ongoing",
        "slug": pid,
        "materials": [],
        "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str) -> str:
    tid = f"test-cte-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "tags": [], "notes": ""})
    return tid


async def _seed_pipeline_row(project_id: str, talent_id: str, stage: str) -> None:
    await db.casting_pipeline.insert_one({
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "talent_id": talent_id,
        "stage": stage,
        "created_at": _now(),
        "updated_at": _now(),
    })


async def _cleanup(phones, project_ids=(), talent_ids=()) -> None:
    if isinstance(phones, str):
        phones = [phones]
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    for phone in phones:
        await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
        await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
        await db.whatsapp_agent_undo.delete_many({"agent_id": AGENT_ID, "phone": phone})
        await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})
        await db[tasks.COLLECTION].delete_many({"agent_id": AGENT_ID, "phone": phone})


async def _stage(project_id: str, talent_id: str) -> str:
    doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
    return doc["stage"]


# ---------------------------------------------------------------------------
# One task, reply-to-message resolves it (and only it) — the highest
# priority feature per the sprint spec.
# ---------------------------------------------------------------------------
async def test_single_task_reply_to_message_resolves_and_clears_only_that_task():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Concurrent Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Solo Talent {_rand_suffix()}")
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {(await db.talents.find_one({'id': talent_id}))['name']} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.operation_id is not None
        op_id = r.operation_id

        task = await tasks.get_task(AGENT_ID, op_id)
        assert task is not None
        assert task["status"] == tasks.STATUS_CONFIRMING

        # Worker reports back the WhatsApp message id the confirmation card
        # actually got — this is what makes the task reply-addressable.
        await tasks.set_confirmation_message_id(AGENT_ID, op_id, "wa-msg-solo-1")

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
            replied_to_message_id="wa-msg-solo-1",
        )
        assert r2.handled
        assert "Done." in r2.reply
        assert await _stage(project_id, talent_id) == "approved"

        # Terminal — this one operation is gone, no trace left behind.
        assert await tasks.get_task(AGENT_ID, op_id) is None

        audit_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "confirmation_action": "task_reply"},
        )
        assert audit_row is not None
        assert audit_row["conversation_id"] == op_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Several concurrent tasks for the SAME user, approved in random (non-
# creation) order via reply-to-message — no cross-contamination.
# ---------------------------------------------------------------------------
async def test_same_user_multiple_concurrent_tasks_random_order_no_cross_contamination():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Multi Op Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    # Deliberately no shared prefix/tokens between names (unlike "Talent 1"/
    # "Talent 2") — this file is testing task isolation, not the fuzzy
    # resolver's ambiguity handling, so each name must resolve unambiguously
    # on its own.
    words = ["Zephyr", "Quokka", "Marmoset", "Ibex"]
    names = [f"{w} {_rand_suffix()}" for w in words]
    talent_ids = [await _seed_talent(n) for n in names]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        # Four independent operations opened back-to-back, none confirmed —
        # each is its own task, self-contained (explicit talent name +
        # explicit project name each time), so none depend on the others or
        # on any stored ordinal/number_map state.
        op_ids = []
        for name in names:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {name} to Approved in {label}.",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
            assert r.operation_id is not None
            op_ids.append(r.operation_id)

        assert len(set(op_ids)) == 4, "each fresh trigger must mint its own distinct operation_id"

        pending_count = await db[tasks.COLLECTION].count_documents({"agent_id": AGENT_ID, "phone": phone})
        assert pending_count == 4, "all 4 tasks must coexist — a fresh trigger must never replace an earlier task"

        message_ids = [f"wa-msg-multi-{i}" for i in range(4)]
        for op_id, msg_id in zip(op_ids, message_ids):
            await tasks.set_confirmation_message_id(AGENT_ID, op_id, msg_id)

        # Random (specifically non-sequential) approval order: 2, 0, 3, 1.
        approval_order = [2, 0, 3, 1]
        for idx in approval_order:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Raj", sender_is_group_member=True,
                replied_to_message_id=message_ids[idx],
            )
            assert r.handled
            assert "Done." in r.reply
            assert f"• {names[idx]}" in r.reply

            # Only the just-approved talent changed — everyone still
            # waiting stays exactly as seeded.
            for j in range(4):
                expected = "approved" if j in {approval_order[k] for k in range(approval_order.index(idx) + 1)} else "ask_to_test"
                assert await _stage(project_id, talent_ids[j]) == expected, (
                    f"talent index {j} has wrong stage after approving index {idx}"
                )

            # The just-resolved task is gone; every still-pending task is
            # completely untouched (auto cleanup removes ONLY that one).
            assert await tasks.get_task(AGENT_ID, op_ids[idx]) is None
            still_pending = [
                op_ids[j] for j in range(4)
                if j not in {approval_order[k] for k in range(approval_order.index(idx) + 1)}
            ]
            for op_id in still_pending:
                remaining_task = await tasks.get_task(AGENT_ID, op_id)
                assert remaining_task is not None
                assert remaining_task["status"] == tasks.STATUS_CONFIRMING

        assert all([await _stage(project_id, tid) == "approved" for tid in talent_ids])
        assert await db[tasks.COLLECTION].count_documents({"agent_id": AGENT_ID, "phone": phone}) == 0
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Multiple DIFFERENT users in the same group, proceeding independently —
# nobody waits, and one user's reply can never resolve another's task.
# ---------------------------------------------------------------------------
async def test_multiple_users_independent_tasks_full_isolation():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone_a = _phone()
    phone_b = _phone()
    phone_c = _phone()
    project_id = await _seed_project(brand_name=f"Multi User Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name_a, name_b, name_c = (f"User{u} Talent {_rand_suffix()}" for u in "ABC")
    tid_a, tid_b, tid_c = [await _seed_talent(n) for n in (name_a, name_b, name_c)]
    talent_ids = [tid_a, tid_b, tid_c]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        results = {}
        for phone, name in ((phone_a, name_a), (phone_b, name_b), (phone_c, name_c)):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {name} to Approved in {label}.",
                sender_name=f"Sender {name}", sender_is_group_member=True,
            )
            assert r.handled and r.operation_id is not None
            results[phone] = r.operation_id

        for phone, op_id in results.items():
            await tasks.set_confirmation_message_id(AGENT_ID, op_id, f"wa-msg-{op_id}")

        # A different user's reply-to-message id must never resolve someone
        # else's task, even though get_task_by_confirmation_message_id is
        # keyed only by agent_id (not phone) — the message id itself is the
        # isolation boundary since each user's card gets its own id.
        wrong_msg_id = f"wa-msg-{results[phone_a]}"
        r_wrong = await handle_inbound_message(
            group_name=group, sender_phone=phone_b, text="1",
            sender_name="Sender B", sender_is_group_member=True,
            replied_to_message_id=wrong_msg_id,
        )
        # phone_b replying to phone_a's card still resolves phone_a's task
        # (message-id lookup has no phone-scoping by design — the WhatsApp
        # message id itself is unique per card) — so this must move A's
        # talent, not B's, and must NOT touch phone_b's own pending task.
        assert r_wrong.handled
        assert await _stage(project_id, tid_a) == "approved"
        assert await _stage(project_id, tid_b) == "ask_to_test"
        assert await _stage(project_id, tid_c) == "ask_to_test"
        assert await tasks.get_task(AGENT_ID, results[phone_a]) is None
        task_b = await tasks.get_task(AGENT_ID, results[phone_b])
        assert task_b is not None and task_b["status"] == tasks.STATUS_CONFIRMING

        # phone_b now approves their own (still-pending) task normally.
        r_b = await handle_inbound_message(
            group_name=group, sender_phone=phone_b, text="1",
            sender_name="Sender B", sender_is_group_member=True,
            replied_to_message_id=f"wa-msg-{results[phone_b]}",
        )
        assert r_b.handled and "Done." in r_b.reply
        assert await _stage(project_id, tid_b) == "approved"
        assert await _stage(project_id, tid_c) == "ask_to_test"

        # phone_c cancels via reply-to-message instead of approving.
        r_c = await handle_inbound_message(
            group_name=group, sender_phone=phone_c, text="cancel",
            sender_name="Sender C", sender_is_group_member=True,
            replied_to_message_id=f"wa-msg-{results[phone_c]}",
        )
        assert r_c.handled
        assert await tasks.get_task(AGENT_ID, results[phone_c]) is None
        assert await _stage(project_id, tid_c) == "ask_to_test"
    finally:
        await _cleanup([phone_a, phone_b, phone_c], project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Explicit operation-id typed in the message (priority tier 2) — no
# reply-to-message needed, still resolves only that one task.
# ---------------------------------------------------------------------------
async def test_explicit_operation_id_prefix_resolves_task_without_reply_context():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Explicit Op Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name = f"Explicit Talent {uuid.uuid4().hex[:6]}"
    talent_id = await _seed_talent(name)
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        op_id = r.operation_id
        assert op_id is not None

        # No confirmation_message_id was ever set — this must still resolve
        # purely from the typed operation id, no reply-to-message involved.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"{op_id} 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Done." in r2.reply
        assert await _stage(project_id, talent_id) == "approved"
        assert await tasks.get_task(AGENT_ID, op_id) is None
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Regression guard: a plain digit with NO reply-to-message and no explicit
# operation id must keep using the pre-existing conversation.py single-slot
# path, completely unaffected by this feature — "nothing should regress".
# ---------------------------------------------------------------------------
async def test_bare_digit_without_reply_context_still_uses_legacy_conversation_path():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Legacy Path Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name = f"Legacy Talent {uuid.uuid4().hex[:6]}"
    talent_id = await _seed_talent(name)
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        op_id = r.operation_id
        assert op_id is not None  # a task WAS also created alongside conversation.py's slot

        conv = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv is not None and conv["step"] == "confirming"

        # Bare "1", no replied_to_message_id — every existing/CRM caller's
        # exact shape. Must resolve via conversation.py, not the task.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Done." in r2.reply
        assert await _stage(project_id, talent_id) == "approved"

        # conversation.py's own slot is cleared, exactly as before this
        # feature existed.
        assert await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone}) is None

        # The task created alongside it is a SEPARATE, parallel record that
        # this legacy path never touches — it is left orphaned until the
        # TTL index reaps it. This is expected/documented behaviour (the
        # additive design's one accepted tradeoff), not a bug: a client
        # that never reports reply-to-message context simply never gets
        # task-based routing, and legacy behaviour is fully preserved.
        orphaned_task = await tasks.get_task(AGENT_ID, op_id)
        assert orphaned_task is not None
        assert orphaned_task["status"] == tasks.STATUS_CONFIRMING
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Expired task: a reply to a confirmation that has since expired must fall
# through cleanly (fail closed) rather than resurrecting a stale operation.
# ---------------------------------------------------------------------------
async def test_expired_task_reply_does_not_resolve_and_is_cleared():
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Expired Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name = f"Expired Talent {uuid.uuid4().hex[:6]}"
    talent_id = await _seed_talent(name)
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        op_id = r.operation_id
        assert op_id is not None
        await tasks.set_confirmation_message_id(AGENT_ID, op_id, "wa-msg-expired-1")

        # Force it into the past — is_expired checks task['expires_at']
        # against now().
        from datetime import datetime, timedelta, timezone
        await db[tasks.COLLECTION].update_one(
            {"agent_id": AGENT_ID, "operation_id": op_id},
            {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}},
        )

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
            replied_to_message_id="wa-msg-expired-1",
        )
        # No match -> falls through. conversation.py's own slot is still
        # "confirming" from the original fresh trigger, so the bare "1"
        # (post-strip) resolves there instead — still handled, still
        # completes the move, just via the fallback tier, not the task.
        assert r2.handled
        assert await tasks.get_task(AGENT_ID, op_id) is None  # expired task was cleared on the lookup miss
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ===========================================================================
# Step 2B — real WhatsApp reply routing via quoted-card-TEXT correlation.
# Real production DOM samples never showed a readable message id on the
# quoted block, so this is the tier that actually resolves a live WhatsApp
# reply — replied_to_message_id (Tier 1) stays exercised above for when a
# stronger identifier IS available; these tests exercise the practical path.
# ===========================================================================
async def test_reply_resolves_via_quoted_text_with_no_message_id():
    """The primary Step 2B path: a reply carries the confirmation card's
    quoted TEXT but no message id at all (matching real production DOM
    evidence) — must still resolve to exactly that task."""
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Quoted Text Brand {_rand_suffix()}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name = f"Quoted Text Talent {_rand_suffix()}"
    talent_id = await _seed_talent(name)
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        op_id = r.operation_id
        assert op_id is not None
        task = await tasks.get_task(AGENT_ID, op_id)
        assert task["last_message_text"] == r.reply

        # No set_confirmation_message_id call at all — this task has NO
        # confirmation_message_id, only its quoted text. The worker also
        # never reports a quotedMessageId in this scenario (real evidence).
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
            replied_quoted_text=r.reply,
        )
        assert r2.handled
        assert "Done." in r2.reply
        assert await _stage(project_id, talent_id) == "approved"
        assert await tasks.get_task(AGENT_ID, op_id) is None

        audit_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "confirmation_action": "task_reply"},
        )
        assert audit_row is not None and audit_row["conversation_id"] == op_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_reply_to_clarification_resolves_via_quoted_text():
    """Same routing must work for a CLARIFICATION card (e.g. "I found
    multiple matching talents... Reply with the number"), not just a
    confirmation card — the spec explicitly calls this out."""
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    p1 = await _seed_project(brand_name=f"Clarify P1 {_rand_suffix()}")
    p2 = await _seed_project(brand_name=f"Clarify P2 {_rand_suffix()}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    tag = _rand_suffix()
    sarah1 = await _seed_talent(f"ClaritySarah Anjuli {tag}")
    sarah2 = await _seed_talent(f"ClaritySarah Kapoor {tag}")
    talent_ids = [sarah1, sarah2]
    try:
        await _seed_pipeline_row(p1, sarah1, "ask_to_test")
        await _seed_pipeline_row(p2, sarah2, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move ClaritySarah {tag} to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        op_id = r.operation_id
        assert op_id is not None
        task = await tasks.get_task(AGENT_ID, op_id)
        # Whichever internal status this particular disambiguation card
        # left the task in (confirming vs. clarifying is an implementation
        # detail of how casting_pipeline renders an ambiguous match), the
        # thing this test actually cares about is that last_message_text
        # was kept in sync with whatever card text was actually sent, so
        # quoted-text correlation works for it either way.
        assert task["last_message_text"] == r.reply

        # Reply quoting the clarification card's exact text, with no
        # message id and no reply-to at all in the legacy sense — this is
        # what Step 2B is actually responsible for: routing this reply back
        # to THIS operation_id, and only this one. What a given reply text
        # then DOES with that operation (proceed vs. ask for an edit) is
        # existing, separately-tested casting_pipeline confirmation-reply
        # grammar, not something Step 2B changes.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
            replied_quoted_text=r.reply,
        )
        assert r2.handled
        assert r2.operation_id == op_id, "the reply must resolve to the SAME operation the card belonged to"

        # Nothing executed prematurely, and the task is still alive (now
        # tracking whatever new card THIS turn sent) — a correctly-routed
        # clarification reply that doesn't (yet) resolve the ambiguity must
        # neither touch the pipeline nor drop the operation.
        assert await _stage(p1, sarah1) == "ask_to_test"
        assert await _stage(p2, sarah2) == "hold"
        task_after = await tasks.get_task(AGENT_ID, op_id)
        assert task_after is not None
        assert task_after["last_message_text"] == r2.reply
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


async def test_quoted_text_ambiguous_match_fails_closed():
    """Direct unit coverage of tasks.find_task_by_quoted_text's safety
    property: if (hypothetically) two pending tasks for the same phone
    carry identical last_message_text, the match must be treated as
    ambiguous and return None — never guess which one a reply meant."""
    phone = _phone()
    try:
        t1 = await tasks.create_task(
            agent_id=AGENT_ID, phone=phone, group_name="g", sender_name="Raj",
            intent_id="casting.move", original_text="x",
        )
        t2 = await tasks.create_task(
            agent_id=AGENT_ID, phone=phone, group_name="g", sender_name="Raj",
            intent_id="casting.move", original_text="y",
        )
        same_text = "Project\nSame Card Text\n\nReply:\n1 -> Approve"
        await tasks.update_task(AGENT_ID, t1["operation_id"], last_message_text=same_text)
        await tasks.update_task(AGENT_ID, t2["operation_id"], last_message_text=same_text)

        match = await tasks.find_task_by_quoted_text(AGENT_ID, phone, same_text)
        assert match is None

        # A DIFFERENT phone's identically-worded task must never be
        # considered at all (scoping check, not just the ambiguity check).
        other_phone = _phone()
        t3 = await tasks.create_task(
            agent_id=AGENT_ID, phone=other_phone, group_name="g", sender_name="Someone",
            intent_id="casting.move", original_text="z",
        )
        await tasks.update_task(AGENT_ID, t3["operation_id"], last_message_text="Unique unambiguous text")
        match2 = await tasks.find_task_by_quoted_text(AGENT_ID, other_phone, "Unique unambiguous text")
        assert match2 is not None and match2["operation_id"] == t3["operation_id"]
    finally:
        await db[tasks.COLLECTION].delete_many({"agent_id": AGENT_ID, "phone": {"$in": [phone, other_phone]}})


async def test_four_concurrent_tasks_quoted_text_random_order_no_cross_contamination():
    """Step 2B's version of the sprint's headline scenario: four tasks,
    none ever get a confirmation_message_id (matching real production
    behaviour today), approved in random order purely via quoted-text
    reply — no cross-contamination."""
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"QText Multi Brand {_rand_suffix()}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    words = ["Falcon", "Otter", "Heron", "Lynx"]
    names = [f"{w} {_rand_suffix()}" for w in words]
    talent_ids = [await _seed_talent(n) for n in names]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        card_texts = []
        op_ids = []
        for name in names:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {name} to Approved in {label}.",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled and r.operation_id is not None
            op_ids.append(r.operation_id)
            card_texts.append(r.reply)

        assert len(set(op_ids)) == 4
        assert len(set(card_texts)) == 4, "each card's text must be distinguishable from the others"

        approval_order = [3, 1, 0, 2]
        for pos, idx in enumerate(approval_order):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Raj", sender_is_group_member=True,
                replied_quoted_text=card_texts[idx],
            )
            assert r.handled
            assert "Done." in r.reply
            assert f"• {names[idx]}" in r.reply

            done_so_far = {approval_order[k] for k in range(pos + 1)}
            for j in range(4):
                expected = "approved" if j in done_so_far else "ask_to_test"
                assert await _stage(project_id, talent_ids[j]) == expected

            assert await tasks.get_task(AGENT_ID, op_ids[idx]) is None
            for j in range(4):
                if j not in done_so_far:
                    still = await tasks.get_task(AGENT_ID, op_ids[j])
                    assert still is not None and still["status"] == tasks.STATUS_CONFIRMING

        assert all([await _stage(project_id, tid) == "approved" for tid in talent_ids])
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_multi_user_quoted_text_isolation():
    """Three different users, three tasks, each resolved purely by quoted
    text — one user's reply must never be able to resolve another's task
    even by accident, since find_task_by_quoted_text is scoped by phone."""
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone_a, phone_b, phone_c = _phone(), _phone(), _phone()
    project_id = await _seed_project(brand_name=f"QText User Brand {_rand_suffix()}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name_a, name_b, name_c = (f"QU{u} {_rand_suffix()}" for u in "ABC")
    tid_a, tid_b, tid_c = [await _seed_talent(n) for n in (name_a, name_b, name_c)]
    talent_ids = [tid_a, tid_b, tid_c]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        # phone_a opens a task; phone_b has NOTHING pending yet anywhere
        # (no task, no legacy conversation) — this isolates the quoted-text
        # scoping check from the legacy bare-digit fallback, which would
        # otherwise mask a real leak: if phone_b already had its own
        # pending move, a bare "1" could legitimately approve THAT via the
        # tier-4 fallback regardless of what quoted text was attached,
        # making the test pass for the wrong reason.
        r_a = await handle_inbound_message(
            group_name=group, sender_phone=phone_a,
            text=f"Move {name_a} to Approved in {label}.",
            sender_name="Sender A", sender_is_group_member=True,
        )
        assert r_a.handled and r_a.operation_id is not None
        card_a = r_a.reply

        # phone_b, with nothing of its own pending, replies "1" quoting
        # phone_a's card text. Must NOT resolve to phone_a's task (wrong
        # phone scope) and, having nothing else pending, must be entirely
        # unhandled.
        r_wrong = await handle_inbound_message(
            group_name=group, sender_phone=phone_b, text="1",
            sender_name="Sender B", sender_is_group_member=True,
            replied_quoted_text=card_a,
        )
        assert not r_wrong.handled
        assert await _stage(project_id, tid_a) == "ask_to_test"

        # Now phone_b and phone_c each open their own task too.
        cards = {phone_a: card_a}
        for phone, name in ((phone_b, name_b), (phone_c, name_c)):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {name} to Approved in {label}.",
                sender_name=f"Sender {name}", sender_is_group_member=True,
            )
            assert r.handled and r.operation_id is not None
            cards[phone] = r.reply

        # Each user now approves via THEIR OWN card's quoted text.
        for phone, tid in ((phone_a, tid_a), (phone_b, tid_b), (phone_c, tid_c)):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Sender", sender_is_group_member=True,
                replied_quoted_text=cards[phone],
            )
            assert r.handled and "Done." in r.reply
            assert await _stage(project_id, tid) == "approved"
    finally:
        await _cleanup([phone_a, phone_b, phone_c], project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_undo_after_quoted_text_reply_approval():
    """Approve one task via quoted-text reply, then UNDO it — correct
    operation restored, correct audit log, and a SECOND still-pending task
    for the same phone is completely untouched throughout."""
    group = f"Test Concurrent {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Undo QText Brand {_rand_suffix()}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    name1 = f"UndoQText One {_rand_suffix()}"
    name2 = f"UndoQText Two {_rand_suffix()}"
    t1 = await _seed_talent(name1)
    t2 = await _seed_talent(name2)
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        r1 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name1} to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        op1 = r1.operation_id
        assert op1 is not None

        # A second, independent task stays pending throughout.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name2} to Hold in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        op2 = r2.operation_id
        assert op2 is not None and op2 != op1

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
            replied_quoted_text=r1.reply,
        )
        assert "Done." in r3.reply
        assert await _stage(project_id, t1) == "approved"
        message_operation_id = next(
            line.split("Operation ID:")[1].strip()
            for line in r3.reply.splitlines() if line.startswith("Operation ID:")
        )

        # op2 (the second task) is completely untouched by op1's reply.
        task2 = await tasks.get_task(AGENT_ID, op2)
        assert task2 is not None and task2["status"] == tasks.STATUS_CONFIRMING
        assert await _stage(project_id, t2) == "ask_to_test"

        r4 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="UNDO",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r4.handled
        assert "Undo complete." in r4.reply
        assert f"Reverted Operation ID: {message_operation_id}" in r4.reply
        assert await _stage(project_id, t1) == "ask_to_test"

        undo_audit = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "parsed_fields.reverted": True}
        )
        assert undo_audit is not None
        assert undo_audit["parsed_fields"]["reverted_operation_id"] == message_operation_id

        # op2 is STILL untouched after the undo.
        task2_after = await tasks.get_task(AGENT_ID, op2)
        assert task2_after is not None and task2_after["status"] == tasks.STATUS_CONFIRMING
        assert await _stage(project_id, t2) == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)
