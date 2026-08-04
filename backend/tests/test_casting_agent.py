import os
# Local/test-only defaults, matching every other test file in this suite —
# pytest must never connect to production by default. Set TEST_MONGO_URL to
# override for a deliberate, explicit run against a different database.
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import re
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


_DUPLICATE_ORDINAL_RE = re.compile(r"^\d+\.\s*\d+\.\s")


def _assert_no_duplicate_numbering(text: str) -> None:
    """Regression guard: a numbered line must never look like "15. 15.
    Sarah" — the ordinal prefix is applied exactly once, at render time,
    never baked into a stored/re-fetched label."""
    for line in text.splitlines():
        assert not _DUPLICATE_ORDINAL_RE.match(line), f"duplicate ordinal numbering in line: {line!r}"


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
    # Zero-padded so alphabetical display order == creation order == the
    # numeric intent of the test — "Talent 1".."Talent 10" would otherwise
    # alphabetically sort as 1, 10, 2, 3, ... (lexicographic), which is
    # exactly the kind of case the new alphabetical-sort behavior needs to
    # be tested against, not accidentally sidestepped by.
    names = [f"Talent {i:02d}" for i in range(1, 11)]
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
        assert "Project\nMove Test Brand" in r.reply
        assert "Pipeline\nAsk To Test" in r.reply
        assert "Total Talents: 10" in r.reply
        for i, n in enumerate(names, start=1):
            assert f"{i}. {n}" in r.reply
        _assert_no_duplicate_numbering(r.reply)

        # Single move by ordinal, with confirmation.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Project\nMove Test Brand" in r.reply
        assert "Pipeline\nAsk To Test" in r.reply
        assert "• Talent 01" in r.reply
        assert "Approve" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Done." in r.reply
        assert "Project\nMove Test Brand" in r.reply
        assert "Moved 1 talent." in r.reply
        assert "• Talent 01" in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids[0]})
        assert doc["stage"] == "approved"

        # Bulk move via mixed selector "2,5,9-10" against the SAME
        # (stale) listing — ordinals still refer to the original 10, per
        # the stored (sorted-at-display-time) number_map, not a fresh
        # re-query that could silently renumber after talent #1 left.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 2,5,9-10 to Rejected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        for n in ["Talent 02", "Talent 05", "Talent 09", "Talent 10"]:
            assert f"• {n}" in r.reply
        _assert_no_duplicate_numbering(r.reply)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "Moved 4 talents." in r.reply
        for n in ["Talent 02", "Talent 05", "Talent 09", "Talent 10"]:
            assert f"• {n}" in r.reply
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

        # Success message includes the project name, the bulleted list of
        # moved names, a unique Operation ID, and the existing UNDO footer.
        assert "Project\nMove Test Brand" in r.reply
        assert "Moved" in r.reply and "talent" in r.reply
        for n in ["Talent 03", "Talent 04", "Talent 06", "Talent 07", "Talent 08"]:
            assert f"• {n}" in r.reply
        assert "Operation ID:" in r.reply
        assert "Reply UNDO within 5 minutes" in r.reply
        _assert_no_duplicate_numbering(r.reply)
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
        assert "Project\nUndo Brand" in r.reply
        assert "Restored 1 talent." in r.reply
        assert "• Undo Talent One" in r.reply
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
        for n in names:
            assert f"• {n}" in r.reply
        _assert_no_duplicate_numbering(r.reply)
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


# ---------------------------------------------------------------------------
# P0: an explicit project reference (by name) must always override stored
# conversational context — never silently fall back to it.
# ---------------------------------------------------------------------------
async def test_explicit_project_overrides_context():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    toyota_id = await _seed_project(brand_name=f"Toyota Glanza {uuid.uuid4().hex[:6]}")
    google_id = await _seed_project(brand_name=f"Google - Film 1 & 3 {uuid.uuid4().hex[:6]}")
    toyota_label = (await db.projects.find_one({"id": toyota_id}))["brand_name"]
    google_label = (await db.projects.find_one({"id": google_id}))["brand_name"]
    t1 = await _seed_talent("Follow Up Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(google_id, t1, "follow_up")

        # Stored/active context is Toyota — an explicit project reference
        # in the very next message must win, not be silently ignored.
        await _seed_number_map_for_project(phone, toyota_id, toyota_label)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show Follow Up pipeline for {google_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{google_label}" in r.reply
        assert toyota_label not in r.reply
        assert "Follow Up Talent" in r.reply

        # The active project context is now updated to the one explicitly
        # named — subsequent commands without a name use THIS project.
        session = await db.whatsapp_agent_sessions.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert session["current_project_id"] == google_id

        # An explicit reference that doesn't resolve to anything is a
        # clear error, never a silent fall-through to stale context.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Show Approved pipeline for Nonexistent Studio XYZ",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "couldn't find a project" in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[toyota_id, google_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Pipeline list: alphabetical (case-insensitive) sort + Project/Pipeline/
# Total Talents header.
# ---------------------------------------------------------------------------
async def test_pipeline_list_alphabetical_sort_and_header():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Sort Test Brand {uuid.uuid4().hex[:6]}")
    # Deliberately non-alphabetical creation order.
    talent_ids = []
    for n in ["Zara", "amit", "Mona"]:  # mixed case, to prove case-insensitivity too
        tid = await _seed_talent(n)
        talent_ids.append(tid)
        await _seed_pipeline_row(project_id, tid, "hold")
    try:
        await _seed_number_map_for_project(phone, project_id, "Sort Test Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Hold",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Project\nSort Test Brand" in r.reply
        assert "Pipeline\nHold" in r.reply
        assert "Total Talents: 3" in r.reply
        assert "━━━━━━━━━━━━━━" in r.reply
        assert "1. amit" in r.reply
        assert "2. Mona" in r.reply
        assert "3. Zara" in r.reply
        assert r.reply.index("1. amit") < r.reply.index("2. Mona") < r.reply.index("3. Zara")
        _assert_no_duplicate_numbering(r.reply)
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Selection numbering: the stored map (and therefore "Move N") must match
# the DISPLAYED (sorted) order, never raw Mongo insertion order.
# ---------------------------------------------------------------------------
async def test_move_by_displayed_number_after_sort():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Number Map Brand {uuid.uuid4().hex[:6]}")
    zara_id = await _seed_talent("Zara")
    amit_id = await _seed_talent("Amit")
    mona_id = await _seed_talent("Mona")
    talent_ids = [zara_id, amit_id, mona_id]  # insertion order: Zara, Amit, Mona
    for tid in talent_ids:
        await _seed_pipeline_row(project_id, tid, "hold")
    try:
        await _seed_number_map_for_project(phone, project_id, "Number Map Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Hold",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "1. Amit" in r.reply and "2. Mona" in r.reply and "3. Zara" in r.reply

        session = await db.whatsapp_agent_sessions.find_one({"agent_id": AGENT_ID, "phone": phone})
        items = session["number_map"]["items"]
        assert [it["label"] for it in items] == ["Amit", "Mona", "Zara"]
        assert [it["id"] for it in items] == [amit_id, mona_id, zara_id]

        # "Move 1" must move Amit — the alphabetically-first, DISPLAYED
        # first talent — never Zara, who happened to be inserted first.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 1 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "• Amit" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply

        amit_doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": amit_id})
        zara_doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": zara_id})
        assert amit_doc["stage"] == "approved"
        assert zara_doc["stage"] == "hold"  # untouched — proves ordinal 1 was Amit, not Zara
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Duplicate-numbering regression: a list long enough to cross the
# double-digit boundary must never render "15. 15. Name".
# ---------------------------------------------------------------------------
async def test_duplicate_numbering_regression_large_list():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Dup Numbering Brand {uuid.uuid4().hex[:6]}")
    names = [f"Talent {chr(65 + i)}{i:02d}" for i in range(20)]  # 20 distinct, orderable names
    talent_ids = [await _seed_talent(n) for n in names]
    try:
        for tid in talent_ids:
            await _seed_pipeline_row(project_id, tid, "shortlisted")
        await _seed_number_map_for_project(phone, project_id, "Dup Numbering Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Shortlisted",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Total Talents: 20" in r.reply
        assert "15. " in r.reply  # crosses into double-digit ordinals
        _assert_no_duplicate_numbering(r.reply)
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Production safety: the displayed number->talent mapping must stay frozen
# for the lifetime of a displayed list — a talent entering/leaving the
# pipeline between "Show X" and "Move N" must NOT change who ordinal N
# refers to. Move resolution must use the STORED session mapping, never a
# fresh live re-query/re-sort.
# ---------------------------------------------------------------------------
async def test_move_by_number_is_stable_against_concurrent_pipeline_changes():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Stability Brand {uuid.uuid4().hex[:6]}")

    # 16 talents, "Talent A".."Talent P" — alphabetically and unambiguously
    # ordered, so ordinal 15 is deterministically "Talent O".
    letters = [chr(ord("A") + i) for i in range(16)]
    names = [f"Talent {c}" for c in letters]
    talent_ids = {n: await _seed_talent(n) for n in names}
    for tid in talent_ids.values():
        await _seed_pipeline_row(project_id, tid, "follow_up")

    try:
        await _seed_number_map_for_project(phone, project_id, "Stability Brand")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "15. Talent O" in r.reply

        # Confirm the persisted mapping in whatsapp_agent_sessions really
        # does hold display_index -> talent_id, keyed to this exact list.
        session = await db.whatsapp_agent_sessions.find_one({"agent_id": AGENT_ID, "phone": phone})
        items = session["number_map"]["items"]
        assert items[14]["ordinal"] == 15
        assert items[14]["label"] == "Talent O"
        assert items[14]["id"] == talent_ids["Talent O"]

        # Simulate another talent entering the SAME pipeline after the list
        # was shown but before the move is approved — sorts as "Talent AA",
        # which lands between "Talent A" and "Talent B", shifting every
        # subsequent alphabetical position by one. If numbering were
        # recomputed live, displayed #15 would now resolve to "Talent N".
        disruptor_id = await _seed_talent("Talent AA")
        await _seed_pipeline_row(project_id, disruptor_id, "follow_up")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 15 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "• Talent O" in r.reply  # confirmation still names the ORIGINAL #15
        assert "Talent N" not in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "• Talent O" in r.reply

        o_doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids["Talent O"]})
        n_doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids["Talent N"]})
        disruptor_doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": disruptor_id})
        assert o_doc["stage"] == "approved"           # the ORIGINALLY-displayed #15 moved
        assert n_doc["stage"] == "follow_up"          # untouched — proves no live re-sort happened
        assert disruptor_doc["stage"] == "follow_up"  # the new arrival was never touched either

        talent_ids["Talent AA"] = disruptor_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=list(talent_ids.values()))
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Natural-language moves — full sentence, zero prior "Show ongoing
# projects" / "Project N" / "Show <Pipeline>" required.
# ---------------------------------------------------------------------------
async def test_natural_language_move_standalone_name_stage_and_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Toyota Glanza {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Aahana Pocha")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        # No prior turn at all — this is the whole point of this sprint.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Aahana Pocha to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label}" in r.reply
        assert "• Aahana Pocha" in r.reply
        assert "Approved" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "approved"

        # The project named in the sentence becomes the active context for
        # whatever comes next.
        session = await db.whatsapp_agent_sessions.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert session["current_project_id"] == project_id
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_natural_language_move_multi_name_and_implied_stage_verb():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Google Film 1 {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    aahana_id = await _seed_talent("Aahana")
    sneha_id = await _seed_talent("Sneha")
    sarah_id = await _seed_talent("Sarah")
    talent_ids = [aahana_id, sneha_id, sarah_id]
    try:
        for tid in (aahana_id, sneha_id, sarah_id):
            await _seed_pipeline_row(project_id, tid, "ask_to_test")

        # Two names in one sentence, joined with "and".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Aahana and Sneha to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "• Aahana" in r.reply and "• Sneha" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Moved 2 talents." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": aahana_id}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sneha_id}))["stage"] == "approved"

        # Implied-stage verb ("Approve") + explicit project, no "to".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Approve Sarah in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "• Sarah" in r.reply
        assert "Approved" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_id}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_natural_language_move_put_into_for():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Pantaloons {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Arya")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Put Arya into Approved for {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label}" in r.reply
        assert "• Arya" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_natural_language_move_ambiguous_talent_disambiguates():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Ambiguous Talent Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    sarah_a = await _seed_talent("Sarah Ahuja")
    sarah_b = await _seed_talent("Sarah Bhatt")
    talent_ids = [sarah_a, sarah_b]
    try:
        await _seed_pipeline_row(project_id, sarah_a, "hold")
        await _seed_pipeline_row(project_id, sarah_b, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Sarah to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # A disambiguation list, not a guess — neither record was touched.
        assert "I found multiple matching talents." in r.reply
        assert "Reply with the number." in r.reply
        assert "Sarah Ahuja" in r.reply and "Sarah Bhatt" in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_a}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_b}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_natural_language_move_ambiguous_and_unresolvable_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    suffix = uuid.uuid4().hex[:6]
    project_a = await _seed_project(brand_name=f"Overlap Brand Alpha {suffix}")
    project_b = await _seed_project(brand_name=f"Overlap Brand Beta {suffix}")
    t1 = await _seed_talent("Overlap Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_a, t1, "hold")
        await _seed_pipeline_row(project_b, t1, "hold")

        # "Overlap Brand <suffix>" doesn't exactly or substring-match
        # either project (it's a substring of neither and vice versa) —
        # it's close to BOTH via fuzzy, which is always a suggestion to
        # confirm/retype, never a silent or numbered pick (project fuzzy
        # matches never auto-resolve or claim definitive candidacy).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Overlap Talent to Approved in Overlap Brand {suffix}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Did you mean" in r.reply
        assert f"Overlap Brand Alpha {suffix}" in r.reply
        assert f"Overlap Brand Beta {suffix}" in r.reply

        # A project name that matches nothing at all is a clear error, not
        # a silent fall-back to whatever's in stored context.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Move Overlap Talent to Approved in Totally Nonexistent Studio ZzzQx.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't find a project" in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# V2: Global talent resolution — project omitted entirely, no stored
# context either. Never guesses: unique match auto-resolves, multiple
# matches disambiguate grouped by project, no match is a clear "not found
# in any active project".
# ---------------------------------------------------------------------------
async def test_global_talent_resolution_unique_match():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Global Unique Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Aahana Pocha")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")

        # No project named, no prior turn at all.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Aahana Pocha to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label}" in r.reply
        assert "• Aahana Pocha" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_global_talent_resolution_ambiguous_grouped_by_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    suffix = uuid.uuid4().hex[:6]
    project_a = await _seed_project(brand_name=f"Global Ambig Alpha {suffix}")
    project_b = await _seed_project(brand_name=f"Global Ambig Beta {suffix}")
    label_a = (await db.projects.find_one({"id": project_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": project_b}))["brand_name"]
    t_a = await _seed_talent("Aahana Pocha")
    t_b = await _seed_talent("Aahana Pocha")  # same name, different project
    talent_ids = [t_a, t_b]
    try:
        await _seed_pipeline_row(project_a, t_a, "hold")
        await _seed_pipeline_row(project_b, t_b, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Aahana Pocha to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        assert "Reply with the number." in r.reply
        assert label_a in r.reply and label_b in r.reply
        # Grouped, numbered listing — never a guess.
        assert r.reply.count("Aahana Pocha") == 2
        # Neither record was touched.
        assert (await db.casting_pipeline.find_one({"project_id": project_a, "talent_id": t_a}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": project_b, "talent_id": t_b}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=talent_ids)
        await _restore_config(original)


async def test_global_talent_resolution_not_found_and_excludes_inactive_projects():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    # A talent that exists only in a NON-ongoing project must not surface
    # via the global (active-projects-only) search.
    inactive_project = await _seed_project(status="hold", brand_name=f"Inactive Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Nowhere Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(inactive_project, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Nowhere Talent to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "wasn't found in any active project" in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": inactive_project, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[inactive_project], talent_ids=talent_ids)
        await _restore_config(original)


async def test_multi_name_move_without_project_requires_explicit_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Multi No Project Brand {uuid.uuid4().hex[:6]}")
    t1 = await _seed_talent("Aahana")
    t2 = await _seed_talent("Sneha")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")
        await _seed_pipeline_row(project_id, t2, "hold")

        # Multiple names, no project named, no stored context — must ask
        # rather than risk writing to the wrong project.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Aahana and Sneha to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "need to know the project" in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t2}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# V2: Stage-first commands ("Hold Sarah", "Lock Rahul") end to end.
# ---------------------------------------------------------------------------
async def test_stage_first_commands():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Stage First Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    sarah_id = await _seed_talent("Sarah")
    rahul_id = await _seed_talent("Rahul")
    talent_ids = [sarah_id, rahul_id]
    try:
        await _seed_pipeline_row(project_id, sarah_id, "ask_to_test")
        await _seed_pipeline_row(project_id, rahul_id, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Hold Sarah in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Hold" in r.reply and "• Sarah" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_id}))["stage"] == "hold"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Lock Rahul in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Locked" in r.reply and "• Rahul" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": rahul_id}))["stage"] == "locked"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# V2: Conversational confirmation synonyms — "Go ahead" / "Proceed" /
# "Do it" approve; "Stop" / "No" cancel — exercised end to end, not just
# at the parser level.
# ---------------------------------------------------------------------------
async def test_conversational_confirmation_synonyms():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Conversational Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Go Ahead Talent")
    t2 = await _seed_talent("Stop Talent")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")
        await _seed_pipeline_row(project_id, t2, "hold")

        # "Proceed" approves, just like "1".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move Go Ahead Talent to Approved in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Proceed",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"

        # "Stop" cancels, just like "3" / "cancel".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move Stop Talent to Approved in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Stop",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Cancelled" in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t2}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# V1 Final — Production Polish & Intelligent Error Handling
# ---------------------------------------------------------------------------

# 1. Voice-style transcript, end to end (leading filler + trailing filler,
# no punctuation, lowercase) — behaves identically to clean typed text.
async def test_voice_style_transcript_end_to_end():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Voice Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Aahana Pocha")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"hey move aahana pocha to approved in {label} please",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "• Aahana Pocha" in r.reply
        assert "Approved" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"

        # The audit log's raw_message stays VERBATIM — voice cleanup is
        # for interpretation only, never for what's recorded as received.
        opening_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "raw_message": {"$regex": "^hey move"}}
        )
        assert opening_row is not None
        assert opening_row["raw_message"].startswith("hey move aahana pocha")
        assert opening_row["raw_message"].endswith("please")
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# 2. Fuzzy talent names — every example from the spec resolves when
# unambiguous, without asking for clarification.
async def test_fuzzy_talent_names_resolve_when_unambiguous():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Fuzzy Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    aahana_id = await _seed_talent("Aahana Pocha")
    sneha_id = await _seed_talent("Sneha")
    shivangi_id = await _seed_talent("Shivangi Negi")
    talent_ids = [aahana_id, sneha_id, shivangi_id]
    try:
        # Aahana/Shivangi start in "hold" (moved TO Approved below); Sneha
        # starts elsewhere so "Move Sneh to Hold" is a real move rather
        # than an "already in that stage" no-op.
        await _seed_pipeline_row(project_id, aahana_id, "hold")
        await _seed_pipeline_row(project_id, sneha_id, "ask_to_test")
        await _seed_pipeline_row(project_id, shivangi_id, "hold")

        cases = [
            ("Move Ahana to Approved", "Aahana Pocha"),
            ("Move Aahana to Approved", "Aahana Pocha"),
            ("Move Ahna Pocha to Approved", "Aahana Pocha"),
            ("Move Aahana Poocha to Approved", "Aahana Pocha"),
            ("Move Sneh to Hold", "Sneha"),
            ("Move Shivangi Negi to Approved", "Shivangi Negi"),
        ]
        for command, expected_name in cases:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=f"{command} in {label}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, command
            assert f"• {expected_name}" in r.reply, f"{command} -> {r.reply}"
            # Cancel so the next case starts from a clean slate without
            # needing to reset stages between cases.
            await handle_inbound_message(
                group_name=group, sender_phone=phone, text="cancel",
                sender_name="Raj", sender_is_group_member=True,
            )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# 3. Talent already in destination stage — clear, two-line message, no
# write performed.
async def test_talent_already_in_destination_stage_message():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Already Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Aahana Pocha")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Aahana Pocha to Approved in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Aahana Pocha is already in Approved.\n\nNo changes were made."
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# 4. Project fuzzy suggestion — a typo'd project name is suggested, never
# silently auto-corrected.
async def test_project_fuzzy_suggestion_not_auto_resolved():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name="Toyota Glanza")
    t1 = await _seed_talent("Suggestion Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Move Suggestion Talent to Approved in Toyota Glnza",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I couldn't find a project matching:" in r.reply
        assert "Toyota Glnza" in r.reply
        assert "Did you mean:" in r.reply
        assert "• Toyota Glanza" in r.reply
        # Never silently moved — the typo was only ever suggested.
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# 5. Pipeline suggestion — an unknown stage name lists every available
# pipeline, dynamically, never hardcoded.
async def test_pipeline_suggestion_lists_available():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Pipeline Suggest Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Pipeline Talent")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Pipeline Talent to Zzzargled in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert 'I couldn\'t find a pipeline named "Zzzargled".' in r.reply
        assert "Available pipelines:" in r.reply
        for stage in pipeline_router.PIPELINE_STAGE_ORDER:
            assert f"• {stage.replace('_', ' ').title()}" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# 6. Clarification context continuation — project ambiguity, then a bare
# number continues the SAME move without repeating the command.
async def test_clarification_context_continuation_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    toyota1 = await _seed_project(brand_name="Toyota Glanza")
    toyota2 = await _seed_project(brand_name="Toyota Urban Cruiser")
    t1 = await _seed_talent("Continuation Talent")
    t2 = await _seed_talent("Continuation Talent")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(toyota1, t1, "hold")
        await _seed_pipeline_row(toyota2, t2, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Move Continuation Talent to Approved in Toyota",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple projects." in r.reply
        assert "Toyota Glanza" in r.reply and "Toyota Urban Cruiser" in r.reply
        assert "Reply with the number." in r.reply
        # Options are numbered in the same order _fetch_ongoing_projects
        # returns them (alphabetical by brand_name) — "Glanza" < "Urban
        # Cruiser", so "Toyota Urban Cruiser" is #2. Assert that ordering
        # directly rather than re-deriving it from the rendered text.
        assert r.reply.index("1.") < r.reply.index("Toyota Glanza") < r.reply.index("2.") < r.reply.index("Toyota Urban Cruiser")
        ordinal = 2

        # Continue with JUST the number — no command repeated.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=str(ordinal),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Project\nToyota Urban Cruiser" in r.reply
        assert "• Continuation Talent" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="go ahead",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": toyota2, "talent_id": t2}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": toyota1, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[toyota1, toyota2], talent_ids=talent_ids)
        await _restore_config(original)


# 7. Ambiguous talent continuation — same idea, for a cross-project
# talent-name ambiguity.
async def test_ambiguous_talent_continuation():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    p1 = await _seed_project(brand_name=f"Sarah P1 {uuid.uuid4().hex[:6]}")
    p2 = await _seed_project(brand_name=f"Sarah P2 {uuid.uuid4().hex[:6]}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    sarah1 = await _seed_talent("Sarah Anjuli")
    sarah2 = await _seed_talent("Sarah Kapoor")
    talent_ids = [sarah1, sarah2]
    try:
        await _seed_pipeline_row(p1, sarah1, "ask_to_test")
        await _seed_pipeline_row(p2, sarah2, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Sarah to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        assert "Sarah Anjuli" in r.reply and "Sarah Kapoor" in r.reply
        # Options are numbered in candidate-fetch order (created_at
        # ascending); Sarah Anjuli's pipeline row was seeded first, so
        # Sarah Kapoor is #2. Assert that ordering directly.
        assert r.reply.index("1.") < r.reply.index("Sarah Anjuli") < r.reply.index("2.") < r.reply.index("Sarah Kapoor")
        ordinal = 2

        # Continue with just the number.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=str(ordinal),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label2}" in r.reply
        assert "• Sarah Kapoor" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="proceed",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": sarah2}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": p1, "talent_id": sarah1}))["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


# 8. Voice transcript with filler words mid-sentence ("um", "can you") —
# distinct from leading/trailing filler, and a repeated-word stutter.
async def test_voice_transcript_with_filler_words():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Filler Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent("Sarah")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"um can you move move sarah to hold in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "• Sarah" in r.reply
        assert "Hold" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="do it",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)
