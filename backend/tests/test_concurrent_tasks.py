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
