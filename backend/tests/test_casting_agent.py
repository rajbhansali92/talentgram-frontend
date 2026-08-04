import os
# Local/test-only defaults, matching every other test file in this suite —
# pytest must never connect to production by default. Set TEST_MONGO_URL to
# override for a deliberate, explicit run against a different database.
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid

import pytest

from core import db, _now
from agents import modules as agent_modules
from agents import registry, session_context, undo_store
from agents.dispatcher import handle_inbound_message
from routers import casting_pipeline as pipeline_router

agent_modules.register_all()

AGENT_ID = "casting-agent"

# All tests in this file share one event loop: core.py's Motor client is a
# module-level singleton (matching every other test file's DB access
# pattern), and it binds to whichever event loop first uses it — pytest-
# asyncio's default per-function loop scope would hand each test a fresh
# loop the client is no longer valid on ("Event loop is closed" on the
# second test). Scoped to this module only; doesn't touch other test files.
pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------
def _phone() -> str:
    # Digits only: dispatcher._normalize_sender strips non-digit chars from
    # the inbound sender_phone before using it as the session/conversation
    # key, so a hex-letter phone here would make our direct session-seeding
    # helper (_seed_number_map_for_project) write a DIFFERENT key than what
    # handle_inbound_message reads back.
    return "91" + str(uuid.uuid4().int)[:9]


async def _use_test_config(group_name: str):
    """Point casting-agent's routing at a throwaway group name for this
    test, saving whatever config already existed so it can be restored —
    this is a shared local dev DB, not a disposable test database."""
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


async def _seed_project(status: str = "ongoing", brand_name: str = None) -> str:
    pid = f"test-cp-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid,
        "brand_name": brand_name or f"Test Project {pid[-6:]}",
        "status": status,
        "slug": pid,
        "materials": [],
        "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str) -> str:
    tid = f"test-cp-tal-{uuid.uuid4().hex[:8]}"
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


async def _cleanup(phone: str, project_ids=(), talent_ids=()) -> None:
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_undo.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})


async def _seed_number_map_for_project(phone: str, project_id: str, project_label: str) -> None:
    """Seeds session context as if the user had just run "Show ongoing
    projects" AND selected "Project 1" — sets both the projects number_map
    (so "Project 1"/"Project 99" resolution can still be exercised) and
    current_project_id/label directly (so tests that only care about
    pipeline/move mechanics don't need a separate round-trip through the
    project-selection step). Decouples tests from whatever else already
    exists as "ongoing" in this shared dev database."""
    await session_context.update_session(
        AGENT_ID, phone,
        current_project_id=project_id, current_project_label=project_label,
        number_map={"type": "projects", "items": [{"ordinal": 1, "id": project_id, "label": project_label}]},
    )


# ---------------------------------------------------------------------------
# Project listing / project detail / pipeline listing
# ---------------------------------------------------------------------------
async def test_project_listing_and_pipeline_queries():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = None
    other_project_id = None
    talent_ids = []
    try:
        project_id = await _seed_project(status="ongoing", brand_name=f"Zz Unique Brand {uuid.uuid4().hex[:6]}")
        other_project_id = await _seed_project(status="complete", brand_name=f"Inactive Brand {uuid.uuid4().hex[:6]}")

        t1 = await _seed_talent("Angela Kumar")
        t2 = await _seed_talent("Neha Shah")
        t3 = await _seed_talent("Riya Patel")
        talent_ids = [t1, t2, t3]
        await _seed_pipeline_row(project_id, t1, "approved")
        await _seed_pipeline_row(project_id, t2, "approved")
        await _seed_pipeline_row(project_id, t3, "hold")

        # "Show ongoing projects" must include our ongoing project and
        # must NOT include the completed one.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zz Unique Brand" in r.reply
        assert "Inactive Brand" not in r.reply

        # Project detail: seed the number_map deterministically (see helper
        # docstring) rather than depending on our project's real ordinal
        # among whatever else is "ongoing" in this shared dev DB.
        await _seed_number_map_for_project(phone, project_id, "Zz Unique Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Project 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Approved (2)" in r.reply
        assert "Hold (1)" in r.reply
        # zero-count stage still shown
        assert "Locked (0)" in r.reply

        # Pipeline listing by name, using the session's current project set
        # by the "Project 1" turn above.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1. Angela Kumar" in r.reply
        assert "2. Neha Shah" in r.reply

        # Count-only query
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="How many talents in hold?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1 talent" in r.reply

        # Invalid project ordinal -> "Project doesn't exist." (re-seed the
        # projects number_map — "Show Approved" above switched it to a
        # talents map, same as it would in a real conversation).
        await _seed_number_map_for_project(phone, project_id, "Zz Unique Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Project 99",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply == "Project doesn't exist."

        # Unrecognized query
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show me the weather",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False or "understand" in (r.reply or "").lower()
    finally:
        ids = [pid for pid in (project_id, other_project_id) if pid]
        await _cleanup(phone, project_ids=ids, talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Move: single, bulk (list/range/mixed), everyone, confirmation approve
# ---------------------------------------------------------------------------
async def test_single_and_bulk_move_with_confirmation():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Move Test Brand {uuid.uuid4().hex[:6]}")
    names = [f"Talent {i}" for i in range(1, 11)]
    talent_ids = [await _seed_talent(n) for n in names]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        await _seed_number_map_for_project(phone, project_id, "Move Test Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        for i, n in enumerate(names, start=1):
            assert f"{i}. {n}" in r.reply

        # Single move by ordinal, with confirmation.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Talent 1" in r.reply
        assert "Approve" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Done." in r.reply
        assert "Moved 1 talent." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids[0]})
        assert doc["stage"] == "approved"

        # Bulk move via mixed selector "2,5,9-10" against the SAME
        # (stale) listing — ordinals still refer to the original 10, per
        # the fresh live re-query keyed on (project, stage).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 2,5,9-10 to Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        for n in ["Talent 2", "Talent 5", "Talent 9", "Talent 10"]:
            assert n in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "Moved 4 talents." in r.reply
        moved = await db.casting_pipeline.find(
            {"project_id": project_id, "talent_id": {"$in": [talent_ids[1], talent_ids[4], talent_ids[8], talent_ids[9]]}}
        ).to_list(10)
        assert all(d["stage"] == "rejected" for d in moved)

        # "Move everyone" over the remaining ask_to_test talents.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move everyone to Hold",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        remaining_ask_to_test = await db.casting_pipeline.count_documents(
            {"project_id": project_id, "stage": "ask_to_test"}
        )
        assert remaining_ask_to_test == 0

        # Success message includes project / source / destination / count /
        # a unique Operation ID, plus the existing UNDO footer.
        assert "Project: Move Test Brand" in r.reply
        assert "From: Ask To Test" in r.reply
        assert "To: Hold" in r.reply
        assert "Moved" in r.reply and "talent" in r.reply
        assert "Operation ID:" in r.reply
        assert "Reply UNDO within 5 minutes" in r.reply
        message_operation_id = next(
            line.split("Operation ID:")[1].strip()
            for line in r.reply.splitlines() if line.startswith("Operation ID:")
        )

        # Audit log for the approve turn captures project / stages / talents
        # / the SAME Operation ID shown in the message — full end-to-end
        # traceability from the WhatsApp reply back to the audit trail.
        audit_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "confirmation_action": "approve"},
            sort=[("timestamp", -1)],
        )
        assert audit_row is not None
        assert audit_row["parsed_fields"].get("project") == "Move Test Brand"
        assert audit_row["parsed_fields"].get("target_stage_label") == "Hold"
        assert audit_row["parsed_fields"].get("talents_moved")
        assert audit_row["parsed_fields"].get("operation_id") == message_operation_id

        # ...and the same Operation ID was stored in the undo record too.
        undo_doc = await db.whatsapp_agent_undo.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert undo_doc is not None
        assert undo_doc["operation"]["operation_id"] == message_operation_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Confirmation: edit + cancel
# ---------------------------------------------------------------------------
async def test_confirmation_edit_and_cancel():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Edit Cancel Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Sana Khan")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_number_map_for_project(phone, project_id, "Edit Cancel Brand")
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )

        # Cancel path
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Cancelled" in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "ask_to_test"  # untouched

        # Edit path: start a move, ask to edit, correct the stage, approve.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "change" in r.reply.lower()
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="To = Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Rejected" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "rejected"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
async def test_error_cases():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Error Case Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Only Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "approved")

        # No project context at all yet.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "pipeline open" in r.reply or "don't know which project" in r.reply

        await _seed_number_map_for_project(phone, project_id, "Error Case Brand")

        # Invalid pipeline name entirely.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Backup",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False or "Pipeline not found" in (r.reply or "") or "understand" in (r.reply or "").lower()

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Approved",
            sender_name="Raj", sender_is_group_member=True,
        )

        # Out-of-range ordinal.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 5 to Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "out of range" in r.reply

        # No matching talent by name.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Zzyzx Nomatch to Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "No matching talent" in r.reply

        # Already in target stage.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "already in Approved" in r.reply

        # Project-map used for a talent-ordinal move -> distinct error, not
        # a silent wrong resolution.
        await _seed_number_map_for_project(phone, project_id, "Error Case Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "pipeline open" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Access control: security_mode="group_members" fails closed
# ---------------------------------------------------------------------------
async def test_group_membership_gate():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Not A Member", sender_is_group_member=False,
        )
        assert r.handled is False

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Not Sure", sender_is_group_member=None,
        )
        assert r.handled is False  # can't verify => don't guess => denied

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is True
    finally:
        await _cleanup(phone)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Undo: successful single move, multiple-undo-attempts (one-shot)
# ---------------------------------------------------------------------------
async def test_undo_success_single_move_and_multiple_attempts():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Undo Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Undo Talent One")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_number_map_for_project(phone, project_id, "Undo Brand")
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "Operation ID:" in r.reply
        assert "Reply UNDO within 5 minutes" in r.reply
        move_operation_id = next(
            line.split("Operation ID:")[1].strip()
            for line in r.reply.splitlines() if line.startswith("Operation ID:")
        )
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "approved"

        # Successful undo restores the previous stage, and its own reply
        # names the SAME Operation ID it just reverted — the full chain
        # (move message -> audit log -> undo record -> undo message) is
        # traceable end-to-end via one shared id.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="UNDO",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Undo complete." in r.reply
        assert "Restored 1 talent." in r.reply
        assert f"Reverted Operation ID: {move_operation_id}" in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "ask_to_test"

        # Multiple undo attempts: the token is one-shot — a second,
        # immediate "UNDO" has nothing left to consume.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="UNDO",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply == "No recent operation available to undo."

        # The revert itself created its own audit entry, cross-referencing
        # the original move's Operation ID.
        revert_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "parsed_fields.reverted": True}
        )
        assert revert_row is not None
        assert revert_row["parsed_fields"]["restored_count"] == 1
        assert revert_row["confirmation_action"] == "auto"
        assert revert_row["parsed_fields"]["reverted_operation_id"] == move_operation_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Undo: after a bulk ("everyone") move — must restore every affected talent
# ---------------------------------------------------------------------------
async def test_undo_after_bulk_move():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Undo Bulk Brand {uuid.uuid4().hex[:6]}")
    names = [f"Undo Bulk Talent {i}" for i in range(1, 6)]
    talent_ids = [await _seed_talent(n) for n in names]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "ask_to_test")
        await _seed_number_map_for_project(phone, project_id, "Undo Bulk Brand")
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move everyone to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Moved 5 talents." in r.reply
        assert await db.casting_pipeline.count_documents(
            {"project_id": project_id, "stage": "approved"}
        ) == 5

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="undo",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Restored 5 talents." in r.reply
        assert await db.casting_pipeline.count_documents(
            {"project_id": project_id, "stage": "ask_to_test"}
        ) == 5
        assert await db.casting_pipeline.count_documents(
            {"project_id": project_id, "stage": "approved"}
        ) == 0
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Undo: window expiry and "nothing to undo"
# ---------------------------------------------------------------------------
async def test_undo_expiry_and_nothing_available():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()

    # Nothing to undo at all yet.
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="UNDO",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply == "No recent operation available to undo."
    finally:
        await _cleanup(phone)

    # An expired token reports expiry, not "nothing available", and never
    # touches the database.
    project_id = await _seed_project(brand_name=f"Undo Expiry Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Expiry Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "approved")
        await undo_store.store_undo(
            AGENT_ID, phone,
            {
                "operation_id": "test-op-expired",
                "project_id": project_id,
                "project_label": "Undo Expiry Brand",
                "new_stage": "approved",
                "previous_stage_by_id": {t1: "ask_to_test"},
                "approved_by": phone,
                "approved_by_name": "Raj",
                "timestamp": "irrelevant",
            },
            ttl_minutes=-1,  # already expired the moment it's written
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="UNDO",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply == "Undo period has expired."
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "approved"  # untouched
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Project summary — "Project N" / "Summary Project N" / "PN", including
# dynamic discovery of a brand-new stage with zero code changes.
# ---------------------------------------------------------------------------
async def test_project_summary_and_dynamic_stages():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Summary Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Summary Talent One")
    t2 = await _seed_talent("Summary Talent Two")
    talent_ids = [t1, t2]
    fake_stage = f"fake_stage_{uuid.uuid4().hex[:6]}"
    fake_stage_added = False
    try:
        await _seed_pipeline_row(project_id, t1, "approved")
        await _seed_pipeline_row(project_id, t2, "hold")

        for cmd in ("Project 1", "Summary Project 1", "P1"):
            # _handle_project_detail resets number_map after each call, so
            # re-seed it before every phrasing rather than assuming it
            # survives across the loop.
            await _seed_number_map_for_project(phone, project_id, "Summary Brand")
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=cmd,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, cmd
            assert "Total Talents: 2" in r.reply, cmd
            assert "Approved (1)" in r.reply, cmd
            assert "Hold (1)" in r.reply, cmd
            assert "Locked (0)" in r.reply, cmd  # zero-count stage still listed
            assert "Last Updated:" in r.reply, cmd

        # Dynamic stage discovery: add a brand-new stage to the ONE real
        # source of truth (routers/casting_pipeline.py's live list) — no
        # code change anywhere in the agent — and confirm the summary
        # picks it up immediately.
        pipeline_router.PIPELINE_STAGE_ORDER.append(fake_stage)
        pipeline_router.PIPELINE_STAGES.add(fake_stage)
        fake_stage_added = True
        t3 = await _seed_talent("Summary Talent Three")
        talent_ids.append(t3)
        await _seed_pipeline_row(project_id, t3, fake_stage)

        await _seed_number_map_for_project(phone, project_id, "Summary Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="P1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Total Talents: 3" in r.reply
        expected_label = fake_stage.replace("_", " ").title()
        assert f"{expected_label} (1)" in r.reply
    finally:
        if fake_stage_added:
            if fake_stage in pipeline_router.PIPELINE_STAGE_ORDER:
                pipeline_router.PIPELINE_STAGE_ORDER.remove(fake_stage)
            pipeline_router.PIPELINE_STAGES.discard(fake_stage)
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)
