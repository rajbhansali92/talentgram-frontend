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
from typing import Optional

import pytest

from core import db, _now
from agents import modules as agent_modules
from agents import registry, request_scope, session_context, undo_store
from agents.dispatcher import handle_inbound_message
from routers import casting_pipeline as pipeline_router

# Compound Actions (2026-08-27) — reusing the SAME proven SEND-gate seeding
# helpers test_media_send.py itself reuses from test_media_assignment.py
# (test-only cross-file coupling, already established precedent), aliased
# to avoid colliding with this file's own differently-shaped _seed_project/
# _seed_talent/_cleanup.
from tests.test_media_assignment import (  # noqa: E402
    GUNWANTI_LID,
    _seed_project as _seed_send_project,
    _seed_submission as _seed_send_submission,
    _seed_talent as _seed_send_talent,
)
from agents.modules import media_assignment as _ma

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


async def _seed_talent(name: str, phone: str = "", whatsapp_group_name: str = "") -> str:
    tid = f"test-cp-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": phone or None, "whatsapp_group_name": whatsapp_group_name,
    })
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


# ---------------------------------------------------------------------------
# Combined Casting Pipeline + WhatsApp Automation (2026-08-19) helpers —
# mirror tests/test_whatsapp_campaign_agent.py's own _seed_project (with
# render-verifiable shoot_dates/budget), _seed_template, _cleanup_batch.
# ---------------------------------------------------------------------------
async def _seed_project_with_details(brand_name: str, *, shoot_dates: str, budget: str) -> str:
    pid = f"test-cp-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "shoot_dates": shoot_dates, "budget_per_day": budget,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_template(name: str, body_text: Optional[str] = None) -> str:
    tpl_id = f"test-cp-tpl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_templates.insert_one({
        "id": tpl_id, "name": name, "slug": name.lower().replace(" ", "_") + uuid.uuid4().hex[:4],
        "body_text": body_text or (
            "Hi {{talent_name}}, about {{project_name}} — Dates {{shoot_dates}} "
            "Budget {{budget}} Link {{submission_link}}"
        ),
        "variables": [], "media_type": "none", "media_url": None, "media_cloudinary_id": None,
        "is_custom": False, "created_by": "test", "created_at": _now(), "updated_at": _now(),
    })
    return tpl_id


async def _cleanup_batch(batch_id: str) -> None:
    await db.whatsapp_batches.delete_one({"id": batch_id})
    await db.whatsapp_jobs.delete_many({"batch_id": batch_id})


async def _cleanup_jobs_for_talents(talent_ids) -> None:
    jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": list(talent_ids)}}, {"_id": 0, "batch_id": 1}).to_list(200)
    for j in jobs:
        await _cleanup_batch(j["batch_id"])


async def _cleanup(phone: str, project_ids=(), talent_ids=()) -> None:
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_undo.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})
    # Concurrent Task Engine (2026-08-05) — casting-agent creates a task doc
    # alongside every fresh-trigger conversation (supports_concurrent_tasks),
    # so every test in this file leaves one behind unless it's cleaned up
    # here too. Left unfixed, this accumulates in the shared dev DB across
    # runs — CP-YYYYMMDD-XXXX only has a 4-hex-char/day suffix (65536 slots),
    # and a few hundred leaked docs from repeated regression runs is enough
    # to make operation_id collisions non-negligible.
    await db.whatsapp_agent_tasks.delete_many({"agent_id": AGENT_ID, "phone": phone})


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
        # Guided Context-Aware Responses (2026-08-28) — CANCELLED is now
        # all-caps with a short explanation (see agents/confirmation.py's
        # CANCELLED_MESSAGE).
        assert "CANCELLED" in r.reply
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

        # "Overlap Brand <suffix>" doesn't exactly match either project,
        # but every one of its tokens is present in BOTH candidates' own
        # tokens (order-independent token-subset match — see
        # resolve_project_by_name's tier 4) — a real, precise match tier,
        # just ambiguous between the two, so it's a proper numbered
        # disambiguation list rather than a vaguer fuzzy "did you mean".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Overlap Talent to Approved in Overlap Brand {suffix}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple projects." in r.reply
        assert "Reply with the number." in r.reply
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
        assert "CANCELLED" in r.reply
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
async def test_project_fuzzy_typo_auto_resolves():
    """2026-08-05 latency/matching sprint: project fuzzy matching now auto-
    resolves an unambiguous close typo, mirroring talent matching's own
    autocorrect-cutoff + ambiguity-margin logic exactly (previously project
    matching NEVER auto-resolved on fuzzy, always asking "did you mean" —
    that old behavior is intentionally gone). Confirmation is still
    required before anything is actually moved — this only removes the
    extra "did you mean" round trip, not the approval step."""
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
        # Auto-resolved straight to the move confirmation card — no "did
        # you mean" round trip for an unambiguous typo.
        assert "Project" in r.reply and "Toyota Glanza" in r.reply
        assert "You are about to move" in r.reply
        assert "• Suggestion Talent" in r.reply
        # Still not moved — confirmation is a separate, required step.
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "hold"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_project_fuzzy_ambiguous_still_asks():
    """Two projects close enough to each other that neither clears the
    ambiguity margin over the other must still ask — auto-resolve only
    fires for a single, clearly-best fuzzy match (same safety bar as
    talent matching)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_a = await _seed_project(brand_name=f"Toyota Glanza {tag}")
    project_b = await _seed_project(brand_name=f"Toyota Glanzo {tag}")
    t1 = await _seed_talent(f"AmbigProjTalent {tag}")
    talent_ids = [t1]
    try:
        await _seed_pipeline_row(project_a, t1, "hold")
        await _seed_pipeline_row(project_b, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move AmbigProjTalent {tag} to Approved in Toyota Glanz {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Neither project was silently picked.
        assert (await db.casting_pipeline.find_one({"project_id": project_a, "talent_id": t1}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": project_b, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=talent_ids)
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


# ---------------------------------------------------------------------------
# Conversational Engine Sprint — conversation-state and contextual
# reasoning coverage (clarification by name/ordinal, token-fuzzy project
# matching, honorific/partial talent matching, long multi-turn context
# retention, voice confidence gate, pagination, correction-after-ambiguity,
# interrupted conversations, undo after a multi-turn move, pronoun
# resolution, query-level clarification continuation).
# ---------------------------------------------------------------------------
async def test_clarification_by_project_name():
    """An ambiguous project list continues when the reply is the literal
    project NAME, not just a number — same pending operation, no restart."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    p1 = await _seed_project(brand_name=f"Bajaj Apache {uuid.uuid4().hex[:6]}")
    p2 = await _seed_project(brand_name=f"Bajaj Pulsar {uuid.uuid4().hex[:6]}")
    label1 = (await db.projects.find_one({"id": p1}))["brand_name"]
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"NameContinuation {uuid.uuid4().hex[:6]}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(p1, t, "hold")
        await _seed_pipeline_row(p2, t, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {(await db.talents.find_one({'id': t}))['name']} to Approved in Bajaj",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label2,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label2}" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": p1, "talent_id": t}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


async def test_clarification_by_partial_project_name():
    """Same as above, but resolved by a PARTIAL, reordered project name
    ("Pulsar Guy" for "Bajaj Pulsar - Main Guy") — token-subset matching."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"Bajaj Apache - Main Guy {tag}")
    p2 = await _seed_project(brand_name=f"Bajaj Pulsar - Main Guy {tag}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"PartialNameTalent {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(p1, t, "hold")
        await _seed_pipeline_row(p2, t, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {(await db.talents.find_one({'id': t}))['name']} to Approved in Bajaj Main Guy {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Pulsar Guy {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label2}" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="go ahead",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


async def test_clarification_by_ordinal_word():
    """"The third one" / "first" resolve a numbered disambiguation list
    exactly like a bare number would."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"Ordinal Alpha {tag}")
    p2 = await _seed_project(brand_name=f"Ordinal Beta {tag}")
    p3 = await _seed_project(brand_name=f"Ordinal Gamma {tag}")
    label3 = (await db.projects.find_one({"id": p3}))["brand_name"]
    t = await _seed_talent(f"OrdinalTalent {tag}")
    talent_ids = [t]
    try:
        for pid in (p1, p2, p3):
            await _seed_pipeline_row(pid, t, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {(await db.talents.find_one({'id': t}))['name']} to Approved in Ordinal {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply
        # Alphabetical: Alpha < Beta < Gamma -> Gamma is #3 ("the third one").
        assert r.reply.index(f"Ordinal Alpha {tag}") < r.reply.index(f"Ordinal Beta {tag}") < r.reply.index(label3)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="the third one",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label3}" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1", sender_name="Raj", sender_is_group_member=True,
        )
        # "1" now answers the real move confirmation (Approve), not the
        # earlier disambiguation — proves the pending operation moved on.
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p3, "talent_id": t}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[p1, p2, p3], talent_ids=talent_ids)
        await _restore_config(original)


async def test_fuzzy_project_matching_token_variations():
    """Every phrasing from the spec resolves to the same project when it's
    the only token-plausible candidate: full name minus hyphen, reordered,
    partial, and a single distinctive token."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    target = await _seed_project(brand_name=f"Bajaj Pulsar - Main Guy {tag}")
    other = await _seed_project(brand_name=f"Toyota Glanza {tag}")
    t = await _seed_talent(f"FuzzyProjectTalent {tag}")
    talent_ids = [t]
    queries = [
        f"Bajaj Pulsar Main Guy {tag}",
        f"Pulsar Main Guy {tag}",
        f"Main Guy {tag}",
        f"Bajaj Main Guy {tag}",
        f"Pulsar {tag}",
        f"Pulsar Guy {tag}",
    ]
    try:
        for q in queries:
            await _seed_pipeline_row(target, t, "hold")
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {(await db.talents.find_one({'id': t}))['name']} to Approved in {q}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Project" in r.reply and f"Bajaj Pulsar - Main Guy {tag}" in r.reply, (q, r.reply)
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="yes",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Done." in r.reply, (q, r.reply)
            await db.casting_pipeline.delete_many({"project_id": target, "talent_id": t})
    finally:
        await _cleanup(phone, project_ids=[target, other], talent_ids=talent_ids)
        await _restore_config(original)


async def test_project_fuzzy_matching_tolerates_campaign_filler_word():
    """"campaign" was added to the project filler-word set for the
    WhatsApp Campaign Agent's benefit (casting_pipeline_nlu.py's
    _PROJECT_FILLER_WORDS, shared verbatim with casting-agent's own project
    matching) — "Toyota Glanza Campaign" must still resolve to a project
    literally named "Toyota Glanza", exactly like "Toyota Glanza Film"/
    "Toyota Glanza Project" already did before this change. A project
    whose REAL name happens to contain "Campaign" must still resolve by
    its full name too — filler-word stripping is symmetric (applied to
    both the query and every candidate label), so it can never make an
    exact, unambiguous name unmatchable."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"Toyota Glanza {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"CampaignFillerTalent {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(project_id, t, "hold")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move CampaignFillerTalent {tag} to Approved in Toyota Glanza Campaign {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Project" in r.reply and label in r.reply, r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_fuzzy_talent_matching_honorific_and_partial():
    """"Mr Prajal" / "Tushir" / "Prajal Kumar" all resolve to the one
    "Prajal Tushir" — honorific stripping + existing best-token fuzzy."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Honorific Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent("Prajal Tushir")
    talent_ids = [t]
    try:
        for query in ("Mr Prajal", "Tushir", "Prajal Kumar"):
            await _seed_pipeline_row(project_id, t, "hold")
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Move {query} to Approved in {label}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "• Prajal Tushir" in r.reply, (query, r.reply)
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Done." in r.reply, (query, r.reply)
            await db.casting_pipeline.delete_many({"project_id": project_id, "talent_id": t})
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_long_multi_turn_conversation_context_retention():
    """The exact worked scenario: Project N -> Show Approved -> Move 7 to
    Locked -> Yes -> Show again -> Move 2 and 5 -> Approved -> Yes — 10+
    messages, never repeating the project or pipeline name."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"Multiturn Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    names = [f"MT{n:02d} {tag}" for n in range(1, 9)]  # 8 talents, sorts by name
    talent_ids = []
    for name in names:
        tid = await _seed_talent(name)
        talent_ids.append(tid)
        await _seed_pipeline_row(project_id, tid, "approved")
    try:
        # 1. "Show ongoing projects" -> find our project's live ordinal.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        ordinal = next(
            int(line.split(".", 1)[0]) for line in r.reply.splitlines() if line.strip().endswith(label)
        )

        # 2. "Project N"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Project {ordinal}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert label in r.reply

        # 3. "Show Approved" — no project repeated.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label}" in r.reply
        assert names[6] in r.reply  # MT07 is ordinal 7 (alphabetical)

        # 4. "Move 7 to Locked" — no project/pipeline repeated.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 7 to Locked",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {names[6]}" in r.reply

        # 5. "Yes"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids[6]}))["stage"] == "locked"

        # 6. "Show again" — replays the Approved pipeline, now missing #7.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show again",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label}" in r.reply
        assert "Pipeline\nApproved" in r.reply
        assert "Total Talents: 7" in r.reply

        # 7. "Move 2 and 5" — no project/pipeline repeated, no stage yet.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 2 and 5",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "pipeline" in r.reply.lower()  # asked which stage

        # 8. "Locked" answers the pending stage question (not "Approved" —
        # ordinals 2 and 5 came from the APPROVED listing itself, so moving
        # them back to Approved would correctly be a no-op "already there";
        # a different destination keeps the scenario semantically live).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Locked",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {names[1]}" in r.reply and f"• {names[4]}" in r.reply

        # 9. "Yes"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids[1]}))["stage"] == "locked"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_ids[4]}))["stage"] == "locked"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_voice_low_confidence_confirmation_flow():
    """A low-confidence transcript is held behind "I heard: ... Is that
    correct?" — "yes" feeds the ORIGINAL transcript through the normal
    pipeline; "no" cancels cleanly; an unrecognized reply re-asks."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Voice Conf Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"VoiceConfTalent {uuid.uuid4().hex[:6]}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(project_id, t, "hold")
        name = (await db.talents.find_one({"id": t}))["name"]
        transcript = f"Move {name} to Approved in {label}"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=transcript,
            sender_name="Raj", sender_is_group_member=True, transcript_confidence=0.3,
        )
        assert r.handled
        assert "I heard:" in r.reply and transcript in r.reply and "Is that correct?" in r.reply

        # Unrecognized reply re-asks rather than silently failing.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="what",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Is that correct?" in r.reply

        # "Yes" feeds the transcript through the normal pipeline.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {name}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t}))["stage"] == "approved"

        # A second low-confidence transcript, this time rejected with "no".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move {name} to Hold",
            sender_name="Raj", sender_is_group_member=True, transcript_confidence=0.1,
        )
        assert "Is that correct?" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="no",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "type your message" in r.reply.lower()
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_voice_note_without_transcript_replies_gracefully():
    """No STT engine is wired up yet — a voice note with no transcript at
    all gets a clear reply instead of being silently dropped."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="",
            sender_name="Raj", sender_is_group_member=True, media_type="voice_note",
        )
        assert r.handled
        assert "can't listen to voice notes yet" in r.reply.lower()
    finally:
        await _cleanup(phone)
        await _restore_config(original)


async def test_full_talent_list_no_pagination():
    """Pagination was tried and explicitly reverted — a pipeline listing
    (even a large one) always returns the complete, alphabetically sorted,
    stably numbered list in ONE message. No "Showing X-Y of Z", no
    "Next"/"Previous" framing, no truncation."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"NoPagination Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    names = [f"PG{n:03d} {tag}" for n in range(1, 46)]  # 45 talents
    talent_ids = []
    for name in names:
        tid = await _seed_talent(name)
        talent_ids.append(tid)
        await _seed_pipeline_row(project_id, tid, "hold")
    try:
        await _seed_number_map_for_project(phone, project_id, label)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Hold",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Total Talents: 45" in r.reply
        for name in names:
            assert name in r.reply, f"{name} missing from single-message listing"
        assert "45. " in r.reply  # last ordinal present in the same message
        assert "Showing" not in r.reply
        assert "reply Next" not in r.reply.lower()

        # Ordinal 45 (the very last one) is directly usable — no separate
        # page to fetch first.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move 45 to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {names[44]}" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_correction_after_ambiguity_keeps_pending_operation():
    """An unmatched free-text reply to an ambiguous list doesn't discard
    the pending move — a follow-up correct reply still completes it."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"Correction Alpha {tag}")
    p2 = await _seed_project(brand_name=f"Correction Beta {tag}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"CorrectionTalent {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(p1, t, "hold")
        await _seed_pipeline_row(p2, t, "hold")
        name = (await db.talents.find_one({"id": t}))["name"]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in Correction {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        # A reply that matches NEITHER option — re-prompted, nothing lost.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Zzzargled Nonsense",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Still no move has happened yet.
        assert (await db.casting_pipeline.find_one({"project_id": p1, "talent_id": t}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "hold"

        # The correct reply still resolves the SAME pending move.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label2,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label2}" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


async def test_interrupted_conversation_fresh_trigger_replaces_pending():
    """A fresh MOVE command mid-clarification fully replaces the pending
    one — the original talent is left untouched."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"Interrupt Alpha {tag}")
    p2 = await _seed_project(brand_name=f"Interrupt Beta {tag}")
    other_project = await _seed_project(brand_name=f"Interrupt Other {tag}")
    other_label = (await db.projects.find_one({"id": other_project}))["brand_name"]
    t1 = await _seed_talent(f"InterruptedTalent {tag}")
    t2 = await _seed_talent(f"FreshTalent {tag}")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(p1, t1, "hold")
        await _seed_pipeline_row(p2, t1, "hold")
        await _seed_pipeline_row(other_project, t2, "hold")
        name1 = (await db.talents.find_one({"id": t1}))["name"]
        name2 = (await db.talents.find_one({"id": t2}))["name"]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name1} to Approved in Interrupt {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        # A completely different, unrelated fresh MOVE command.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name2} to Locked in {other_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {name2}" in r.reply
        assert "I found multiple projects." not in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": other_project, "talent_id": t2}))["stage"] == "locked"
        # The original, interrupted move never happened.
        assert (await db.casting_pipeline.find_one({"project_id": p1, "talent_id": t1}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t1}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[p1, p2, other_project], talent_ids=talent_ids)
        await _restore_config(original)


async def test_undo_after_multiturn_disambiguated_move():
    """Undo works correctly for a move that only completed after a
    multi-turn clarification."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"UndoMT Alpha {tag}")
    p2 = await _seed_project(brand_name=f"UndoMT Beta {tag}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"UndoMTTalent {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(p1, t, "hold")
        await _seed_pipeline_row(p2, t, "hold")
        name = (await db.talents.find_one({"id": t}))["name"]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {name} to Approved in UndoMT {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label2}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "approved"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="undo",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Undo complete." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t}))["stage"] == "hold"
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


async def test_pronoun_reference_across_commands():
    """"Move Sarah to Hold" then "Approve her" — the pronoun refers to
    whoever was just discussed, no name repeated."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"Pronoun Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"PronounSarah {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(project_id, t, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move PronounSarah {tag} to Hold in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "• PronounSarah" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t}))["stage"] == "hold"

        # Pronoun follow-up — no name, no project repeated.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Approve her",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "• PronounSarah" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t}))["stage"] == "approved"

        # Bare verb command (no name, no stored ambiguity pending), still
        # referring to the same last-discussed talent.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Lock",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "• PronounSarah" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t}))["stage"] == "locked"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_query_ambiguous_project_continuation():
    """A QUERY (read-only) hitting an ambiguous project also stays alive
    for a stateful reply — auto_confirm intents get the same continuation
    MOVE already had."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"QueryAmbig Alpha {tag}")
    p2 = await _seed_project(brand_name=f"QueryAmbig Beta {tag}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"QueryAmbigTalent {tag}")
    talent_ids = [t]
    try:
        await _seed_pipeline_row(p2, t, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show Hold for QueryAmbig {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Which project did you mean?" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label2,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Project\n{label2}" in r.reply
        assert "Pipeline\nHold" in r.reply
        assert f"QueryAmbigTalent {tag}" in r.reply
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Feedback Sprint — bare-number project selection, Add-to-pipeline (Ask To
# Test), full-list rendering already covered by
# test_full_talent_list_no_pagination above.
# ---------------------------------------------------------------------------
async def test_bare_number_selects_project():
    """A bare number right after "Show ongoing projects" opens that
    project directly — the session already has the mapping, no need to
    repeat "Project N"."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"BareNumber Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        ordinal = next(
            int(line.split(".", 1)[0]) for line in r.reply.splitlines() if line.strip().endswith(label)
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=str(ordinal),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Project" in r.reply and label in r.reply
        assert "Total Talents:" in r.reply

        # Out of range still errors clearly rather than silently doing
        # nothing or crashing.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="99999",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "doesn't exist" in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[project_id])
        await _restore_config(original)


async def test_bare_number_ignored_without_projects_list():
    """A bare number with no "projects" number_map in session (e.g. never
    listed projects, or the last number_map was a talent list) is
    unrelated chatter, not silently misinterpreted."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"BareNumberTalents Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"BareNumberTalent {uuid.uuid4().hex[:6]}")
    try:
        await _seed_pipeline_row(project_id, t, "hold")
        await _seed_number_map_for_project(phone, project_id, label)

        # Show a TALENT list — number_map.type becomes "talents", not
        # "projects".
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Hold",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Not interpreted as "Project 1" — no active conversation, no
        # trigger, wrong map type, so it's silently ignored (unrelated
        # chatter), same as any other unrecognized bare message.
        assert not r.handled
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_add_single_talent_creates_ask_to_test_entry():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"AddSingle Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"AddSingleTalent {uuid.uuid4().hex[:6]}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add {(await db.talents.find_one({'id': t}))['name']} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert "Pipeline:" in r.reply and "Ask To Test" in r.reply
        assert "1 → Approve" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "Added 1 talent to Ask To Test." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "ask_to_test"

        # Adding again reports it's already there instead of duplicating.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add {(await db.talents.find_one({'id': t}))['name']} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "already in the" in r.reply and "pipeline" in r.reply
        assert "No changes were made." in r.reply
        count = await db.casting_pipeline.count_documents({"project_id": project_id, "talent_id": t})
        assert count == 1
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_add_multi_talent_commas_and_newlines():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AddMulti Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent(f"AddMultiOne {tag}")
    t2 = await _seed_talent(f"AddMultiTwo {tag}")
    t3 = await _seed_talent(f"AddMultiThree {tag}")
    talent_ids = [t1, t2, t3]
    try:
        # Comma + "and" form, single line.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add AddMultiOne {tag}, AddMultiTwo {tag} and AddMultiThree {tag} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        for tid in talent_ids:
            name = (await db.talents.find_one({"id": tid}))["name"]
            assert name in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Added 3 talents to Ask To Test." in r.reply
        for tid in talent_ids:
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc is not None and doc["stage"] == "ask_to_test"

        # Clean up for the newline-form half of this test.
        await db.casting_pipeline.delete_many({"project_id": project_id})

        # One-name-per-line form.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add\nAddMultiOne {tag}\nAddMultiTwo {tag}\nAddMultiThree {tag}\nto\n{label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="go ahead",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Added 3 talents to Ask To Test." in r.reply
        for tid in talent_ids:
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc is not None and doc["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_add_fuzzy_typo_names():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"AddFuzzy Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent("Prajal Tushir")
    try:
        for query in ("Prajel", "Prjal", "Prajal Kumar"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Add {query} to {label}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Prajal Tushir" in r.reply, (query, r.reply)
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Done." in r.reply, (query, r.reply)
            await db.casting_pipeline.delete_many({"project_id": project_id, "talent_id": t})
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_add_ambiguous_talent_continuation():
    """Multiple similar-named talents -> numbered clarification -> a bare
    reply resolves it and the ADD continues without repeating the command."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AddAmbigTalent Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    p1 = await _seed_talent(f"Prajal Shah {tag}")
    p2 = await _seed_talent(f"Prajal Mehta {tag}")
    talent_ids = [p1, p2]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add Prajal {tag} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple matching talents." in r.reply
        assert f"Prajal Shah {tag}" in r.reply and f"Prajal Mehta {tag}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert f"Prajal Mehta {tag}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p2})) is not None
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p1})) is None
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_add_ambiguous_project_continuation():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(brand_name=f"AddAmbigProj Alpha {tag}")
    p2 = await _seed_project(brand_name=f"AddAmbigProj Beta {tag}")
    label2 = (await db.projects.find_one({"id": p2}))["brand_name"]
    t = await _seed_talent(f"AddAmbigProjTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add {(await db.talents.find_one({'id': t}))['name']} to AddAmbigProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label2,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert label2 in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": p2, "talent_id": t})) is not None
        assert (await db.casting_pipeline.find_one({"project_id": p1, "talent_id": t})) is None
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=[t])
        await _restore_config(original)


async def test_add_talent_not_found():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"AddNotFound Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add Zzzargled Nonexistent Person to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "No matching talent found." in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id])
        await _restore_config(original)


async def test_add_requires_project_when_omitted():
    """Add never defaults the destination project from session context —
    it's always asked for explicitly if omitted, since it's a real write."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"AddNoProject Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"AddNoProjectTalent {uuid.uuid4().hex[:6]}")
    try:
        await _seed_number_map_for_project(phone, project_id, label)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add {(await db.talents.find_one({'id': t}))['name']}",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Guided Context-Aware Responses (2026-08-28) — the missing-project
        # question now says what's needed + an example + reassurance,
        # rather than the old bare "Which project should I add them to?".
        assert "add needs a project" in r.reply.lower()

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert label in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_add_does_not_affect_move_workflow():
    """Regression guard: the "add" trigger and casting.add intent coexist
    cleanly with an in-progress MOVE conversation — a fresh "add" mid-move
    replaces the pending move (same "fresh trigger always restarts" rule
    as any other trigger), it doesn't corrupt or silently merge with it."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AddMoveCoexist Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    move_talent = await _seed_talent(f"MoveCoexistTalent {tag}")
    add_talent = await _seed_talent(f"AddCoexistTalent {tag}")
    talent_ids = [move_talent, add_talent]
    try:
        await _seed_pipeline_row(project_id, move_talent, "hold")

        move_name = (await db.talents.find_one({"id": move_talent}))["name"]
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {move_name} to Approved in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"• {move_name}" in r.reply

        add_name = (await db.talents.find_one({"id": add_talent}))["name"]
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add {add_name} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert add_name in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        assert "Added 1 talent to Ask To Test." in r.reply

        # The original move never happened — cleanly replaced, not merged.
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": move_talent}))["stage"] == "hold"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": add_talent}))["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# "and confirm" — skips the approval card when resolution is unambiguous;
# still asks when ambiguous, then auto-continues (no second approval) once
# the ambiguity is resolved.
# ---------------------------------------------------------------------------
async def test_and_confirm_skips_confirmation_for_move():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AndConfirmMove Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"AndConfirmTalent {tag}")
    try:
        await _seed_pipeline_row(project_id, t, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move AndConfirmTalent {tag} to Approved in {label} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Executed immediately — no "Reply: 1 -> Approve" confirmation card.
        assert "Reply:" not in r.reply
        assert "Done." in r.reply
        assert "Moved 1 talent." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc["stage"] == "approved"

        # No pending conversation left behind either.
        conv = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv is None
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_and_confirm_skips_confirmation_for_add():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AndConfirmAdd Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"AndConfirmAddTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add AndConfirmAddTalent {tag} to {label} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        assert "Done." in r.reply
        assert "Added 1 talent to Ask To Test." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_and_confirm_ambiguous_still_asks_then_auto_continues():
    """An "and confirm" whose talent is ambiguous must still stop and ask
    — but once the user picks one, it executes immediately with no second
    approval step (the whole point of "and confirm" surviving the
    disambiguation continuation)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"AndConfirmAmbig Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    a = await _seed_talent(f"Prajal Alpha {tag}")
    b = await _seed_talent(f"Prajal Beta {tag}")
    talent_ids = [a, b]
    try:
        await _seed_pipeline_row(project_id, a, "ask_to_test")
        await _seed_pipeline_row(project_id, b, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Prajal {tag} to Approved in {label} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple matching talents." in r.reply
        assert f"Prajal Alpha {tag}" in r.reply and f"Prajal Beta {tag}" in r.reply
        # Nobody touched yet.
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": a}))["stage"] == "ask_to_test"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": b}))["stage"] == "ask_to_test"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Resolves straight to execution — no second confirmation card.
        assert "Reply:" not in r.reply
        assert "Done." in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": b}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": a}))["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Multi-action commands — chained ("and move to"), multi-project cross-
# product, independent newline-separated operations, partial-failure
# summary.
# ---------------------------------------------------------------------------
async def test_multi_action_chained_add_then_move():
    """"Add X to Y and move to Approved" — one combined confirmation card,
    then a single approval executes BOTH steps; the second step's implicit
    talent resolves via the pronoun/last-talent continuation."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"ChainedAddMove Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"ChainedTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add ChainedTalent {tag} to {label} and move to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan:" in r.reply
        assert f"ChainedTalent {tag}" in r.reply
        assert "Approved" in r.reply
        # Not executed yet.
        assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})) is None

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 2
        assert "✗" not in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_multi_project_single_move_command():
    """"Move X to Approved in A and B" — same talent moved in BOTH
    projects from one command, via the 1-step-plan cross-product path."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_a = await _seed_project(brand_name=f"MultiProjA Brand {tag}")
    project_b = await _seed_project(brand_name=f"MultiProjB Brand {tag}")
    label_a = (await db.projects.find_one({"id": project_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": project_b}))["brand_name"]
    t = await _seed_talent(f"MultiProjTalent {tag}")
    try:
        await _seed_pipeline_row(project_a, t, "ask_to_test")
        await _seed_pipeline_row(project_b, t, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move MultiProjTalent {tag} to Approved in {label_a} and {label_b}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan:" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 2
        assert (await db.casting_pipeline.find_one({"project_id": project_a, "talent_id": t}))["stage"] == "approved"
        assert (await db.casting_pipeline.find_one({"project_id": project_b, "talent_id": t}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=[t])
        await _restore_config(original)


async def test_cross_product_multi_talent_multi_project_add():
    """"Add T1 and T2 to A and B" — cross-product-expands to all 4
    (talent x project) additions from one command."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_a = await _seed_project(brand_name=f"CrossProjA Brand {tag}")
    project_b = await _seed_project(brand_name=f"CrossProjB Brand {tag}")
    label_a = (await db.projects.find_one({"id": project_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": project_b}))["brand_name"]
    t1 = await _seed_talent(f"CrossTalentOne {tag}")
    t2 = await _seed_talent(f"CrossTalentTwo {tag}")
    talent_ids = [t1, t2]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add CrossTalentOne {tag} and CrossTalentTwo {tag} to {label_a} and {label_b}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan:" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 4
        for pid in (project_a, project_b):
            for tid in talent_ids:
                doc = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
                assert doc is not None and doc["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=talent_ids)
        await _restore_config(original)


async def test_independent_multi_move_partial_failure_summary():
    """Two independent, newline-separated move commands in one message —
    one against a real project, one against a project that doesn't exist —
    both run; the failing one is reported, not allowed to abort the rest."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"IndepMove Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t1 = await _seed_talent(f"IndepOne {tag}")
    t2 = await _seed_talent(f"IndepTwo {tag}")
    talent_ids = [t1, t2]
    missing_project = f"NoSuchProject {tag}"
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        text = (
            f"Move IndepOne {tag}, IndepTwo {tag} to Approved in {label}\n\n"
            f"Move Somebody Else to Hold in {missing_project}"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan:" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply.startswith("Completed")
        assert "✓" in r.reply and "✗" in r.reply
        assert "2 talents moved" in r.reply
        assert missing_project in r.reply
        for tid in talent_ids:
            assert (await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid}))["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Bulk multi-mapping (talent-group -> project-group segments) + trailing
# actions applying to the whole resolved set, and multi-talent pending-
# project queries.
# ---------------------------------------------------------------------------
async def test_bulk_multi_segment_add_fan_out_follow_up_and_confirm():
    """The full mega-example: several independent talent-group ->
    project-group mappings in ONE Add command, followed by a trailing
    "move to Follow Up" with no name/project of its own — must apply to
    every pair the command just created, not just the last one touched.
    "and confirm" only bypasses the approval card; it is NOT a second
    stage (see casting_pipeline.py's _resolve_one_plan_segment)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"FanOutA Brand {tag}")
    proj_b = await _seed_project(brand_name=f"FanOutB Brand {tag}")
    proj_c = await _seed_project(brand_name=f"FanOutC Brand {tag}")
    proj_d = await _seed_project(brand_name=f"FanOutD Brand {tag}")
    label_a = (await db.projects.find_one({"id": proj_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    label_c = (await db.projects.find_one({"id": proj_c}))["brand_name"]
    label_d = (await db.projects.find_one({"id": proj_d}))["brand_name"]
    ta = await _seed_talent(f"FanOutTalentA {tag}")
    tb = await _seed_talent(f"FanOutTalentB {tag}")
    tc = await _seed_talent(f"FanOutTalentC {tag}")
    td = await _seed_talent(f"FanOutTalentD {tag}")
    te = await _seed_talent(f"FanOutTalentE {tag}")
    all_talent_ids = [ta, tb, tc, td, te]
    all_project_ids = [proj_a, proj_b, proj_c, proj_d]
    try:
        text = (
            f"Add FanOutTalentA {tag} and FanOutTalentB {tag} to {label_a}, "
            f"FanOutTalentC {tag} and FanOutTalentD {tag} to {label_b}, "
            f"FanOutTalentE {tag} to {label_c} and {label_d}, "
            f"and move to follow up and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        assert "Completed" in r.reply
        assert "✗" not in r.reply

        for tid, pid in [
            (ta, proj_a), (tb, proj_a), (tc, proj_b), (td, proj_b), (te, proj_c), (te, proj_d),
        ]:
            doc = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
            assert doc is not None and doc["stage"] == "follow_up", (tid, pid, doc)
    finally:
        await _cleanup(phone, project_ids=all_project_ids, talent_ids=all_talent_ids)
        await _restore_config(original)


async def test_bulk_multi_segment_add_without_chaining_requires_approval():
    """"Add A to X, B to Y" (no trailing action, no "confirm") — each
    talent -> project mapping is its own segment; the plan preview lists
    both, and approving executes both as separate additions in their own
    projects (not a garbled cross-product)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"SegNoChainA Brand {tag}")
    proj_b = await _seed_project(brand_name=f"SegNoChainB Brand {tag}")
    label_a = (await db.projects.find_one({"id": proj_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    ta = await _seed_talent(f"SegNoChainTalentA {tag}")
    tb = await _seed_talent(f"SegNoChainTalentB {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add SegNoChainTalentA {tag} to {label_a}, SegNoChainTalentB {tag} to {label_b}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan:" in r.reply
        assert f"SegNoChainTalentA {tag}" in r.reply
        assert f"SegNoChainTalentB {tag}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 2
        assert (await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": ta}))["stage"] == "ask_to_test"
        assert (await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": tb}))["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_bulk_multi_segment_add_ambiguous_talent_in_one_segment_isolated():
    """An ambiguous name in ONE segment of a multi-mapping Add reports
    that segment's error (rather than silently guessing) without touching
    the other, unambiguous segments — the same partial-failure isolation
    the plan engine already guarantees for independent multi-step
    commands."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"AmbigSegA Brand {tag}")
    proj_b = await _seed_project(brand_name=f"AmbigSegB Brand {tag}")
    label_a = (await db.projects.find_one({"id": proj_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    shared_name = f"AmbigSegTwin {tag}"
    t1 = await _seed_talent(shared_name)
    t2 = await _seed_talent(shared_name)
    tb = await _seed_talent(f"AmbigSegTalentB {tag}")
    try:
        text = f"Add {shared_name} to {label_a}, AmbigSegTalentB {tag} to {label_b}, and confirm"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✓" in r.reply
        assert "✗" in r.reply
        assert (await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": tb})) is not None
        assert (await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": t1})) is None
        assert (await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": t2})) is None
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[t1, t2, tb])
        await _restore_config(original)


async def test_multi_talent_pending_projects_query_grouped():
    """"Show pending projects for A, B and C" — resolves all three names
    independently via the existing fuzzy matcher (parse_talent_selector's
    name_queries split), and groups the response by talent, one talent
    with no active pipeline included alongside the ones that do."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(status="ongoing", brand_name=f"MultiQA Brand {tag}")
    proj_b = await _seed_project(status="ongoing", brand_name=f"MultiQB Brand {tag}")
    ta = await _seed_talent(f"MultiQTalentA {tag}")
    tb = await _seed_talent(f"MultiQTalentB {tag}")
    tc = await _seed_talent(f"MultiQTalentC {tag}")
    try:
        await _seed_pipeline_row(proj_a, ta, "ask_to_test")
        await _seed_pipeline_row(proj_b, tb, "follow_up")
        # tc has no active pipeline row at all.

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show pending projects for MultiQTalentA {tag}, MultiQTalentB {tag} and MultiQTalentC {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"MultiQTalentA {tag}" in r.reply
        assert f"MultiQTalentB {tag}" in r.reply
        assert f"MultiQTalentC {tag}" in r.reply
        assert "MultiQA Brand" in r.reply
        assert "MultiQB Brand" in r.reply
        assert "is currently not part of any active casting pipeline" in r.reply
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb, tc])
        await _restore_config(original)


async def test_multi_talent_pending_projects_query_with_of_and_fuzzy_typo():
    """Matches the original spec's "of" phrasing too, and tolerates a
    minor spelling typo in one of the names via the existing fuzzy
    matcher — reused unchanged, not a new matching system."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tag_b = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(status="ongoing", brand_name=f"FuzzyQA Brand {tag}")
    ta = await _seed_talent(f"Ahana Pocha {tag}")
    tb = await _seed_talent(f"FuzzyQTalentB {tag_b}")
    try:
        await _seed_pipeline_row(proj_a, ta, "ask_to_test")

        # "Ahna" (missing an 'a') should still fuzzy-resolve to "Ahana Pocha".
        # A DIFFERENT random tag on the second name — sharing one token with
        # the first name's tag would itself create a spurious fuzzy match
        # (this matcher scores best-TOKEN, not whole-string, similarity).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show pending projects of Ahna Pocha {tag}, FuzzyQTalentB {tag_b}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Ahana Pocha {tag}" in r.reply
        assert "FuzzyQA Brand" in r.reply
        assert f"FuzzyQTalentB {tag_b}" in r.reply
    finally:
        await _cleanup(phone, project_ids=[proj_a], talent_ids=[ta, tb])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Compact line/slash-delimited formats.
# ---------------------------------------------------------------------------
async def test_slash_delimited_single_add_command():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"SlashAdd Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"SlashAddTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add / SlashAddTalent {tag} / {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply
        assert f"SlashAddTalent {tag}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_line_based_positional_move_command():
    """"Move\\nNAME\\nSTAGE\\nPROJECT" — no connector words, pure line
    position determines talent / stage / project."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"PositionalMove Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"PositionalTalent {tag}")
    try:
        await _seed_pipeline_row(project_id, t, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move\nPositionalTalent {tag}\nApproved\n{label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"PositionalTalent {tag}" in r.reply
        assert "Approve" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_mega_example_line_based_add_move_confirm():
    """The combined mega-example: compact line-based Add + chained Move +
    a trailing "Confirm" line — one message, no taps, talent ends up
    directly in Approved (never just sitting in Ask To Test)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"MegaLine Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"MegaLineTalent {tag}")
    try:
        text = f"Add\nMegaLineTalent {tag}\n{label}\nMove\nApproved\nConfirm"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        assert "You are about to run this plan:" not in r.reply
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 2
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


async def test_mega_example_slash_delimited_add_move_confirm():
    """Same mega-example, slash-delimited instead of newline-delimited —
    normalize_compact_text must make the two forms behave identically."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"MegaSlash Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    t = await _seed_talent(f"MegaSlashTalent {tag}")
    try:
        text = f"add / MegaSlashTalent {tag} / {label} / move / approved / confirm"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        assert "Completed" in r.reply
        assert r.reply.count("✓") == 2
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t})
        assert doc is not None and doc["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Performance profiling / timing output + per-turn cache.
# ---------------------------------------------------------------------------
async def test_dispatch_timing_stage_breakdown_logged(caplog):
    """dispatch_timing's log line carries a per-stage breakdown (mongo/
    fuzzy/auth/...), not just one coarse total — the whole point of the
    latency-instrumentation work."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"TimingBrand {uuid.uuid4().hex[:6]}")
    try:
        await _seed_number_map_for_project(phone, project_id, "TimingBrand")
        import logging
        with caplog.at_level(logging.INFO, logger="agents.dispatcher"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="Show Ask To Test",
                sender_name="Raj", sender_is_group_member=True,
            )
        assert r.handled
        timing_records = [rec for rec in caplog.records if "dispatch_timing" in rec.getMessage()]
        assert timing_records
        msg = timing_records[-1].getMessage()
        assert "dispatch_ms=" in msg
        assert "stages=" in msg
        assert "project_lookup" in msg or "talent_lookup" in msg
    finally:
        await _cleanup(phone, project_ids=[project_id])
        await _restore_config(original)


async def test_request_scope_session_cache_avoids_duplicate_reads():
    """Two get_session calls within the SAME turn (request_scope.reset()
    called once) must hit Mongo only once — the per-turn memo cache."""
    phone = _phone()

    class _CountingCollection:
        def __init__(self, real):
            self._real = real
            self.find_one_calls = 0

        async def find_one(self, *a, **kw):
            self.find_one_calls += 1
            return await self._real.find_one(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _FakeDB:
        def __init__(self, real, name, fake):
            self._real = real
            self._name = name
            self._fake = fake

        def __getitem__(self, name):
            if name == self._name:
                return self._fake
            return self._real[name]

    real_collection = db[session_context.COLLECTION]
    counting = _CountingCollection(real_collection)
    fake_db = _FakeDB(db, session_context.COLLECTION, counting)

    import agents.session_context as sc_module
    old_db = sc_module.db
    sc_module.db = fake_db
    try:
        request_scope.reset()
        s1 = await session_context.get_session(AGENT_ID, phone)
        s2 = await session_context.get_session(AGENT_ID, phone)
        assert s1 == s2
        assert counting.find_one_calls == 1

        # A fresh turn (reset() again) is a genuinely new scope — it must
        # be allowed to hit Mongo again, proving the cache is per-turn
        # only, never stale across turns.
        request_scope.reset()
        await session_context.get_session(AGENT_ID, phone)
        assert counting.find_one_calls == 2
    finally:
        sc_module.db = old_db


# ---------------------------------------------------------------------------
# Conversational Casting Insights (2026-08-09) — read-only talent-centric
# and stage-specific query intents. Every test below only ever sends
# QUERY-shaped messages ("Show", "Is", "Which", "What", "Has", "Did") —
# never move/add/undo — so this section also stands as the sprint's own
# "no database writes occur for any query" evidence: _assert_pipeline_row_count_unchanged
# wraps casting_pipeline row counts around each scenario.
# ---------------------------------------------------------------------------
async def _pipeline_row_count(project_ids) -> int:
    return await db.casting_pipeline.count_documents({"project_id": {"$in": list(project_ids)}})


async def test_talent_query_active_projects_across_multiple() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Ahana {uuid.uuid4().hex[:6]}"
    p1 = p2 = p3 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=f"Zzq Toyota {uuid.uuid4().hex[:6]}")
        p2 = await _seed_project(status="ongoing", brand_name=f"Zzq Dove {uuid.uuid4().hex[:6]}")
        p3 = await _seed_project(status="complete", brand_name=f"Zzq Inactive {uuid.uuid4().hex[:6]}")
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "follow_up")
        await _seed_pipeline_row(p2, talent_id, "approved")
        await _seed_pipeline_row(p3, talent_id, "hold")  # inactive project — must be excluded

        before = await _pipeline_row_count([p1, p2, p3])
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show {name}'s ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert name in r.reply
        assert "Follow Up" in r.reply
        assert "Approved" in r.reply
        assert "Total Active Projects: 2" in r.reply
        assert "Zzq Inactive" not in r.reply
        assert await _pipeline_row_count([p1, p2, p3]) == before  # read-only

        # A second phrasing of the same capability.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"What projects is {name} part of?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Total Active Projects: 2" in r2.reply
    finally:
        await _cleanup(phone, project_ids=[p for p in (p1, p2, p3) if p], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_talent_query_no_active_projects() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Lonely {uuid.uuid4().hex[:6]}"
    talent_id = await _seed_talent(name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Which casting projects involve {name}?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "not part of any active casting pipeline" in r.reply
        assert name in r.reply
    finally:
        await _cleanup(phone, project_ids=[], talent_ids=[talent_id])
        await _restore_config(original)


async def test_talent_query_unknown_talent() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show active projects of Zzq Nonexistent {uuid.uuid4().hex[:8]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "No matching talent" in r.reply
    finally:
        await _restore_config(original)


async def test_talent_query_ambiguous_talent_disambiguates() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    shared_name = f"Zzq Twin {uuid.uuid4().hex[:6]}"
    p1 = p2 = t1 = t2 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=f"Zzq Twin Proj A {uuid.uuid4().hex[:6]}")
        p2 = await _seed_project(status="ongoing", brand_name=f"Zzq Twin Proj B {uuid.uuid4().hex[:6]}")
        t1 = await _seed_talent(shared_name)
        t2 = await _seed_talent(shared_name)
        await _seed_pipeline_row(p1, t1, "approved")
        await _seed_pipeline_row(p2, t2, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show active projects of {shared_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "multiple matching talents" in r.reply.lower()

        # Reply with a number — resumes the ORIGINAL talent-projects query
        # for whichever of the two was picked, exactly like an ambiguous
        # project/talent pick already does for casting.move.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Total Active Projects: 1" in r2.reply
        assert ("Zzq Twin Proj A" in r2.reply) or ("Zzq Twin Proj B" in r2.reply)
    finally:
        await _cleanup(phone, project_ids=[p for p in (p1, p2) if p], talent_ids=[t for t in (t1, t2) if t])
        await _restore_config(original)


async def test_talent_stage_boolean_yes_no_and_wrong_project() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Bool {uuid.uuid4().hex[:6]}"
    p1 = p2 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=f"Zzq Bool Toyota {uuid.uuid4().hex[:6]}")
        p2 = await _seed_project(status="ongoing", brand_name=f"Zzq Bool Dove {uuid.uuid4().hex[:6]}")
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "ask_to_test")

        before = await _pipeline_row_count([p1, p2])

        # Correct stage, correct project -> Yes.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Is {name} testing for Zzq Bool Toyota?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled

        # Wrong stage, same project -> No, with the real current stage shown.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Has {name} been approved for Zzq Bool Toyota?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.reply.startswith("No.")
        assert "Ask To Test" in r2.reply

        # Right stage phrasing, project the talent ISN'T part of -> No, not part of it.
        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Is {name} testing for Zzq Bool Dove?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r3.reply.startswith("No.")
        assert "not part of" in r3.reply

        assert await _pipeline_row_count([p1, p2]) == before  # read-only throughout
    finally:
        await _cleanup(phone, project_ids=[p for p in (p1, p2) if p], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_talent_stage_boolean_matches() -> None:
    """Isolated from the fuzzy-suffix noise above: a clean Yes. case."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Yes {uuid.uuid4().hex[:6]}"
    project_label = f"Zzq Yes Toyota Glanza {uuid.uuid4().hex[:6]}"
    p1 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "follow_up")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Is {name} in Follow Up for {project_label}?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply.startswith("Yes.")
        assert project_label in r.reply
        assert "Follow Up" in r.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_talent_stage_filtered_list_without_project() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Person {uuid.uuid4().hex[:6]}"
    p1 = p2 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=f"Zzq Filter Toyota {uuid.uuid4().hex[:6]}")
        p2 = await _seed_project(status="ongoing", brand_name=f"Zzq Filter Dove {uuid.uuid4().hex[:6]}")
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "ask_to_test")
        await _seed_pipeline_row(p2, talent_id, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Which projects is {name} testing for?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zzq Filter Toyota" in r.reply
        assert "Zzq Filter Dove" not in r.reply
        assert "Total: 1" in r.reply
    finally:
        await _cleanup(phone, project_ids=[p for p in (p1, p2) if p], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_talent_stage_unresolvable_stage_word_is_graceful() -> None:
    """"Selected" has no SINGLE canonical stage mapping anywhere in this
    system (casting.move never implies one either — see
    IMPLIED_STAGE_BY_VERB's docstring) — asking about it must never
    silently guess. UX polish sprint (2026-08-09): rather than a dead-end
    "I don't understand," this now surfaces the real Approved/Locked
    ambiguity through the SAME existing stage-ambiguous clarification
    flow every other genuine near-tie already uses (see
    match_stage_phrase_for_query in casting_pipeline_nlu.py) — replacing
    the previous sprint's flat fallback message, an intentional,
    requested behavior change for this exact case."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Selected {uuid.uuid4().hex[:6]}"
    project_label = f"Zzq Selected Proj {uuid.uuid4().hex[:6]}"
    p1 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Did {name} get selected for {project_label}?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which pipeline did you mean?" in r.reply
        assert "Approved" in r.reply and "Locked" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.reply.startswith("Yes.")
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_pipeline_query_positional_project_and_stage() -> None:
    """"Show <Project> <Stage>" / "List <Project> <Stage>" — the
    connector-less phrasing with no "for"/"of" at all. (A message that
    leads with the PROJECT name and no trigger word at all, e.g. a bare
    "Toyota Follow Up list", can never be routed at all — every intent in
    this platform requires the first word(s) to be a recognized trigger;
    see agents/parser.detect_trigger. "Show"/"List" first is the reachable
    form of this phrasing.)"""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq Positional {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Positional Person")
        await _seed_pipeline_row(p1, t1, "follow_up")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show {project_label} Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1. Positional Person" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"List {project_label} Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "1. Positional Person" in r2.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[t1] if t1 else [])
        await _restore_config(original)


async def test_pipeline_query_who_is_testing_natural_language() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq NL {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("NL Person")
        await _seed_pipeline_row(p1, t1, "ask_to_test")
        await _seed_number_map_for_project(phone, p1, project_label)

        for text in ("Who is testing?", "Testing list", "Show testing pipeline"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
            assert "1. NL Person" in r.reply, f"failed for {text!r}: {r.reply!r}"
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[t1] if t1 else [])
        await _restore_config(original)


async def test_pipeline_query_missing_project_offers_choices() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    label_a = f"Zzq Missing A {uuid.uuid4().hex[:6]}"
    label_b = f"Zzq Missing B {uuid.uuid4().hex[:6]}"
    pa = pb = ta = None
    try:
        pa = await _seed_project(status="ongoing", brand_name=label_a)
        pb = await _seed_project(status="ongoing", brand_name=label_b)
        ta = await _seed_talent("Missing Case Person")
        await _seed_pipeline_row(pa, ta, "follow_up")

        # Brand new conversation — no session context, no project named.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show Follow Up pipeline",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which project" in r.reply
        assert label_a in r.reply and label_b in r.reply

        # Resolve by naming the project exactly — resumes the original
        # "Follow Up" pipeline query via the shared disambiguation engine.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label_a,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "1. Missing Case Person" in r2.reply
    finally:
        await _cleanup(phone, project_ids=[p for p in (pa, pb) if p], talent_ids=[ta] if ta else [])
        await _restore_config(original)


async def test_pipeline_query_empty_pipeline() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq Empty Case {uuid.uuid4().hex[:6]}"
    p1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show {project_label} Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "No talents in this pipeline" in r.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[])
        await _restore_config(original)


async def test_query_intents_never_write_to_casting_pipeline() -> None:
    """Explicit audit assertion for the sprint's own "read-only" gate: a
    representative sweep of every new query shape, before/after row-count
    diff on the exact projects touched."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Audit {uuid.uuid4().hex[:6]}"
    project_label = f"Zzq Audit Proj {uuid.uuid4().hex[:6]}"
    p1 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "hold")

        before = await _pipeline_row_count([p1])
        messages = [
            f"Show active projects of {name}",
            f"Is {name} in Hold for {project_label}?",
            f"Has {name} been approved for {project_label}?",
            f"Which projects is {name} testing for?",
            f"Show {project_label} Hold",
        ]
        for text in messages:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
        after = await _pipeline_row_count([p1])
        assert before == after
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_existing_move_add_undo_unaffected_by_query_changes() -> None:
    """Narrow regression guard specific to this sprint's touch points
    (_resolve_project_ref's newly-added "offer every ongoing project"
    branch, and QUERY_TRIGGERS' new leading words) — proves a MOVE still
    behaves exactly as before even with a brand new conversation (no
    project context), and that a bare "Is"/"Has" word never gets routed
    into casting.move by mistake."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq Move Unaffected {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Move Unaffected Person")
        await _seed_pipeline_row(p1, t1, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Move Unaffected Person to Approved in {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "about to move" in r.reply.lower() or "approve" in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[t1] if t1 else [])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# UX Polish sprint (2026-08-09) — "Selected" query support + verb-less
# pipeline queries. Both scoped entirely to casting.query; global trigger
# routing (agents/parser.detect_trigger, QUERY_TRIGGERS/MOVE_TRIGGERS) is
# untouched by this sprint.
# ---------------------------------------------------------------------------
async def test_selected_talent_boolean_ambiguous_then_resolves() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq Sel {uuid.uuid4().hex[:6]}"
    project_label = f"Zzq Sel Toyota {uuid.uuid4().hex[:6]}"
    p1 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Was {name} selected for {project_label}?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which pipeline did you mean?" in r.reply
        assert "Approved" in r.reply and "Locked" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.reply.startswith("Yes.")
        assert project_label in r2.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_selected_talent_no_project_ambiguous_then_resolves() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    name = f"Zzq SelNP {uuid.uuid4().hex[:6]}"
    project_label = f"Zzq SelNP Toyota {uuid.uuid4().hex[:6]}"
    p1 = talent_id = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        talent_id = await _seed_talent(name)
        await _seed_pipeline_row(p1, talent_id, "locked")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Did {name} get selected?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which pipeline did you mean?" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Locked",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert project_label in r2.reply
        assert "Locked" in r2.reply
        assert "Total: 1" in r2.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[talent_id] if talent_id else [])
        await _restore_config(original)


async def test_selected_pipeline_listing_who_got_selected() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq SelList Toyota {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Selected List Person")
        await _seed_pipeline_row(p1, t1, "locked")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Who got selected for {project_label}?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which pipeline did you mean?" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",  # Approved, Locked -> pick "Locked"
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "1. Selected List Person" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show selected candidates for {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Which pipeline did you mean?" in r3.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[t1] if t1 else [])
        await _restore_config(original)


async def test_selected_does_not_change_move_stage_validation() -> None:
    """Regression guard: casting.move's own "which pipeline?" question,
    answered with "Selected", must behave byte-for-byte as before this
    sprint — match_stage_phrase (unmodified) still treats it as an
    unrelated weak fuzzy hint, NOT the new Approved/Locked ambiguity
    (that's only wired into match_stage_phrase_for_query, never into the
    shared function extract_move_fields/_validate_target_stage call)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_label = f"Zzq MoveSel {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Move Selected Person")
        await _seed_pipeline_row(p1, t1, "hold")
        await _seed_number_map_for_project(phone, p1, project_label)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Move Move Selected Person to Selected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Never the new query-only Approved/Locked ambiguity card.
        assert "Approved" not in r.reply or "Locked" not in r.reply
        assert "Which pipeline did you mean?" not in r.reply
    finally:
        await _cleanup(phone, project_ids=[p1] if p1 else [], talent_ids=[t1] if t1 else [])
        await _restore_config(original)


async def test_verbless_pipeline_queries_match_show_prefixed_equivalents() -> None:
    """"Toyota Follow Up" (no "Show") must behave exactly like
    "Show Toyota Follow Up" — one seeded project/talent, exercised through
    every stage phrase from the sprint's own example list."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    project_label = f"Zzq Verbless {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    phones: list = []
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Verbless Person")
        await _seed_pipeline_row(p1, t1, "follow_up")

        cases = [
            ("Follow Up", "1. Verbless Person"),
            ("Testing", "No talents in this pipeline"),
            ("Rejected", "No talents in this pipeline"),
            ("Ask To Test", "No talents in this pipeline"),
            ("Shortlisted", "No talents in this pipeline"),
            ("Hold", "No talents in this pipeline"),
        ]
        for stage_text, expect_snippet in cases:
            bare_phone = _phone()
            shown_phone = _phone()
            phones.extend([bare_phone, shown_phone])

            bare = await handle_inbound_message(
                group_name=group, sender_phone=bare_phone, text=f"{project_label} {stage_text}",
                sender_name="Raj", sender_is_group_member=True,
            )
            shown = await handle_inbound_message(
                group_name=group, sender_phone=shown_phone, text=f"Show {project_label} {stage_text}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert bare.handled, f"bare form unhandled for {stage_text!r}"
            assert expect_snippet in bare.reply, f"bare form wrong reply for {stage_text!r}: {bare.reply!r}"
            assert bare.reply == shown.reply, f"parity mismatch for {stage_text!r}"
    finally:
        for phone in phones:
            await _cleanup(phone, project_ids=[], talent_ids=[])
        await db.projects.delete_many({"id": p1} if p1 else {"id": "__none__"})
        await db.talents.delete_many({"id": t1} if t1 else {"id": "__none__"})
        await db.casting_pipeline.delete_many({"project_id": p1} if p1 else {"project_id": "__none__"})
        await _restore_config(original)


async def test_verbless_query_unknown_stage_word_falls_through_unhandled() -> None:
    """"Toyota Callback" has no existing stage synonym anywhere in this
    system (same as "Show Toyota Callback" would) — parity means it must
    ALSO fail gracefully rather than guessing a stage, not that it must
    magically start working."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    project_label = f"Zzq Callback {uuid.uuid4().hex[:6]}"
    p1 = None
    bare_phone = _phone()
    shown_phone = _phone()
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)

        bare = await handle_inbound_message(
            group_name=group, sender_phone=bare_phone, text=f"{project_label} Callback",
            sender_name="Raj", sender_is_group_member=True,
        )
        shown = await handle_inbound_message(
            group_name=group, sender_phone=shown_phone, text=f"Show {project_label} Callback",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert bare.handled is False
        assert shown.handled is False or "understand" in (shown.reply or "").lower()
    finally:
        await _cleanup(bare_phone, project_ids=[p1] if p1 else [], talent_ids=[])
        await _cleanup(shown_phone, project_ids=[], talent_ids=[])
        await _restore_config(original)


async def test_verbless_query_ignores_ordinary_group_chatter() -> None:
    """The DB-backed verification gate (resolve_project_by_name against
    the REAL live project list) must decline messages that merely contain
    a stage-ish word inside ordinary conversation — never auto-reply to
    unrelated group chatter. Uses phrasing that also avoids every existing
    MOVE/ADD/UNDO/QUERY trigger word entirely, so this is a clean test of
    the new gate specifically, not pre-existing trigger behavior."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phones = []
    try:
        for text in (
            "sarah seems on hold right now honestly",
            "everyone please note the shoot is rescheduled",
            "that shortlist looks great honestly",
            "lunch is ready guys",
        ):
            phone = _phone()
            phones.append(phone)
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled is False, f"unexpectedly handled: {text!r} -> {r.reply!r}"
    finally:
        for phone in phones:
            await _cleanup(phone, project_ids=[], talent_ids=[])
        await _restore_config(original)


async def test_verbless_and_selected_queries_never_write_to_casting_pipeline() -> None:
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    project_label = f"Zzq PolishAudit {uuid.uuid4().hex[:6]}"
    p1 = t1 = None
    phones = []
    try:
        p1 = await _seed_project(status="ongoing", brand_name=project_label)
        t1 = await _seed_talent("Polish Audit Person")
        await _seed_pipeline_row(p1, t1, "approved")

        before = await _pipeline_row_count([p1])
        for text in (
            f"{project_label} Follow Up",
            f"{project_label} Selected",
            f"Was Polish Audit Person selected for {project_label}?",
            f"Who got selected for {project_label}?",
        ):
            phone = _phone()
            phones.append(phone)
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
        after = await _pipeline_row_count([p1])
        assert before == after
    finally:
        for phone in phones:
            await _cleanup(phone, project_ids=[], talent_ids=[])
        await db.projects.delete_many({"id": p1} if p1 else {"id": "__none__"})
        await db.talents.delete_many({"id": t1} if t1 else {"id": "__none__"})
        await db.casting_pipeline.delete_many({"project_id": p1} if p1 else {"project_id": "__none__"})
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Simplified Command Language (2026-08-17) — hyphen-delimited grammar.
# Parser unit tests first (pure functions, no DB), then end-to-end coverage
# through handle_inbound_message for every shape the spec calls out.
# ---------------------------------------------------------------------------
from agents.modules import casting_pipeline_nlu as _snlu  # noqa: E402
from routers.casting_pipeline import PIPELINE_STAGE_ORDER as _SIMPLE_STAGE_ORDER  # noqa: E402


async def test_simple_parse_add_basic():
    parsed = _snlu.parse_simple_add_move_command("Add - Riya Patel - Toyota Campaign - Follow Up")
    assert parsed == {
        "action": "add", "talent_part": "Riya Patel",
        "project_part": "Toyota Campaign", "pipeline_part": "Follow Up",
    }
    assert _snlu.translate_simple_command_to_natural_language(parsed) == (
        "Add Riya Patel to Toyota Campaign and move to Follow Up"
    )


async def test_simple_parse_move_basic():
    parsed = _snlu.parse_simple_add_move_command("Move - Riya Patel - Toyota Campaign - Ask To Test to Approved")
    assert parsed["action"] == "move"
    assert _snlu.translate_simple_command_to_natural_language(parsed) == (
        "Move Riya Patel to Approved in Toyota Campaign"
    )


async def test_simple_parse_move_arrow_target():
    parsed = _snlu.parse_simple_add_move_command("Move - Riya - Toyota - Ask To Test → Approved")
    assert _snlu.translate_simple_command_to_natural_language(parsed) == "Move Riya to Approved in Toyota"


async def test_simple_parse_combined_action_case_insensitive():
    for word in ("add,move", "Add, Move", "ADD,  MOVE", "move,add", "Move, Add"):
        parsed = _snlu.parse_simple_add_move_command(f"{word} - A - B - C")
        assert parsed is not None, word
        assert parsed["action"] == "add_move"
        # Combined action chains identically to bare "add" — Pipeline is
        # always meaningful (see translate_simple_command_to_natural_language).
        assert _snlu.translate_simple_command_to_natural_language(parsed) == "Add A to B and move to C"


async def test_simple_parse_bare_add_also_chains_into_move():
    parsed = _snlu.parse_simple_add_move_command("Add - A - B - C")
    assert _snlu.translate_simple_command_to_natural_language(parsed) == "Add A to B and move to C"


async def test_simple_parse_rejects_natural_language_and_unmatched_text():
    assert _snlu.parse_simple_add_move_command("Add Riya to Toyota and move to Follow Up") is None
    assert _snlu.parse_simple_add_move_command("Show ongoing projects") is None
    assert _snlu.parse_simple_add_move_command("") is None


async def test_simple_parse_missing_hyphen_recovery_via_stage_boundary():
    parsed = _snlu.parse_simple_add_move_command(
        "Add - Riya Patel - Toyota Campaign Follow Up", stage_order=list(_SIMPLE_STAGE_ORDER),
    )
    assert parsed is not None
    assert parsed["project_part"] == "Toyota Campaign"
    assert parsed["pipeline_part"] == "Follow Up"


async def test_simple_parse_missing_hyphen_recovery_requires_stage_order():
    assert _snlu.parse_simple_add_move_command("Add - Riya Patel - Toyota Campaign Follow Up") is None


async def test_simple_parse_bare_2field_add_with_no_recognizable_stage_is_a_plain_add():
    """Command Specification V1 Phase 3C (2026-08-27) — a 2-field "Add -
    Talent - Project" with nothing stage-like in the trailing text is no
    longer declined outright: for a pure "add" action (never bare "move",
    which still requires an explicit stage — see the sibling test below),
    this is now recognized as a plain "just add them, no explicit stage"
    command, matching the already-proven space-separated "Add Talent to
    Project" natural form's own default-stage behavior. No pipeline_part
    at all in the result — this is the signal translate_simple_command_
    to_natural_language uses to emit a plain "Add X to Y" sentence."""
    parsed = _snlu.parse_simple_add_move_command(
        "Add - Riya Patel - Some Ambiguous Trailing Words", stage_order=list(_SIMPLE_STAGE_ORDER),
    )
    assert parsed is not None
    assert parsed["action"] == "add"
    assert parsed["talent_part"] == "Riya Patel"
    assert parsed["project_part"] == "Some Ambiguous Trailing Words"
    assert "pipeline_part" not in parsed


async def test_simple_parse_bare_2field_move_still_declines_no_stage_to_guess():
    """The same fix must NOT extend to a bare "move" action — MOVE always
    needs an explicit target stage to mean anything, so a 2-field "Move -
    Talent - X" with no recognizable stage in X still declines cleanly
    (falls through to natural-language parsing, unchanged)."""
    assert _snlu.parse_simple_add_move_command(
        "Move - Riya Patel - Some Ambiguous Trailing Words", stage_order=list(_SIMPLE_STAGE_ORDER),
    ) is None


async def test_simple_translate_commands_in_text_preserves_blank_lines_and_confirm():
    text = (
        "Add - Talent A - Project A - Follow Up\n"
        "\n"
        "Move - Talent B - Project B - Approved\n"
        "and confirm"
    )
    out = _snlu.translate_simple_commands_in_text(text, list(_SIMPLE_STAGE_ORDER))
    lines = out.split("\n")
    assert lines[0] == "Add Talent A to Project A and move to Follow Up"
    assert lines[1] == ""
    assert lines[2] == "Move Talent B to Approved in Project B"
    assert lines[3] == "and confirm"


async def test_simple_translate_commands_no_blank_line_still_splits_on_new_trigger():
    # No blank line between the two commands — split_actions_grouped keys
    # off the trigger word the translation left behind, not blank lines.
    # The first command's own translated "and move to Follow Up" chain is
    # further split into its own sub-chunk (same group 0) by the existing
    # and-chaining rule — that's independent of, and correct alongside,
    # the group-boundary behavior this test targets.
    text = "Add - Talent A - Project A - Follow Up\nMove - Talent B - Project B - Approved"
    translated = _snlu.translate_simple_commands_in_text(text, list(_SIMPLE_STAGE_ORDER))
    grouped = _snlu.split_actions_grouped(translated)
    groups = [g for g, _c in grouped]
    assert groups == [0, 0, 1]
    assert grouped[-1][1] == "Move Talent B to Approved in Project B"


async def test_simple_parse_query_pending_test():
    qi = _snlu.parse_simple_query_command("pending test - Riya Patel")
    assert qi is not None and qi.kind == "pending_tests" and qi.talent_query == "Riya Patel"


async def test_simple_parse_query_testing_check_with_and_without_question_mark():
    for word in ("testing?", "testing"):
        qi = _snlu.parse_simple_query_command(f"{word} - Riya Patel - Toyota Campaign")
        assert qi is not None and qi.kind == "testing_check", word
        assert qi.talent_query == "Riya Patel" and qi.project_name_query == "Toyota Campaign"


async def test_simple_parse_query_show_multi():
    qi = _snlu.parse_simple_query_command("show - Project A,Project B - Follow Up,Approved")
    assert qi is not None and qi.kind == "pipeline_multi"
    assert qi.project_name_query == "Project A,Project B"
    assert qi.stage_key_multi == "Follow Up,Approved"


async def test_simple_parse_query_ignores_non_matching_text():
    assert _snlu.parse_simple_query_command("Show ongoing projects") is None
    assert _snlu.parse_simple_query_command("pending test") is None
    assert _snlu.parse_simple_query_command("pending test -") is None


async def test_simple_add_single_command_requires_confirmation_then_writes():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"SimpleAdd Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"SimpleAdd Talent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add - SimpleAdd Talent {tag} - {label} - Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to" in r.reply
        assert f"SimpleAdd Talent {tag}" in r.reply

        # Bare "Add" always chains into "and move to <stage>" (Pipeline is
        # never optional — see translate_simple_command_to_natural_language),
        # so this becomes a 2-step plan (add, then move) rather than a
        # single "Done." confirmation.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r.reply
        assert "✗" not in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_simple_move_stage_to_stage_and_confirm():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"SimpleMove Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"SimpleMove Talent {tag}")
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move - SimpleMove Talent {tag} - {label} - Ask To Test to Approved and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_simple_combined_add_move_action_and_confirm():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"SimpleCombined Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"SimpleCombined Talent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add,Move - SimpleCombined Talent {tag} - {label} - Shortlisted and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "shortlisted"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_simple_multi_command_blank_line_separated_with_single_trailing_confirm():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"MultiCmdA Brand {tag}")
    proj_b = await _seed_project(brand_name=f"MultiCmdB Brand {tag}")
    label_a = (await db.projects.find_one({"id": proj_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    ta = await _seed_talent(f"MultiCmdTalentA {tag}")
    tb = await _seed_talent(f"MultiCmdTalentB {tag}")
    try:
        await _seed_pipeline_row(proj_b, tb, "ask_to_test")
        text = (
            f"Add - MultiCmdTalentA {tag} - {label_a} - Follow Up\n"
            "\n"
            f"Move - MultiCmdTalentB {tag} - {label_b} - Shortlisted\n"
            "and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        doc_a = await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": ta})
        doc_b = await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": tb})
        assert doc_a is not None and doc_a["stage"] == "follow_up"
        assert doc_b is not None and doc_b["stage"] == "shortlisted"
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_simple_multi_command_no_blank_line_still_splits_correctly():
    """The parser must not depend exclusively on blank lines — a new
    action keyword on its own line is enough of a boundary."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"NoBlankA Brand {tag}")
    proj_b = await _seed_project(brand_name=f"NoBlankB Brand {tag}")
    label_a = (await db.projects.find_one({"id": proj_a}))["brand_name"]
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    ta = await _seed_talent(f"NoBlankTalentA {tag}")
    tb = await _seed_talent(f"NoBlankTalentB {tag}")
    try:
        text = (
            f"Add - NoBlankTalentA {tag} - {label_a} - Follow Up\n"
            f"Add - NoBlankTalentB {tag} - {label_b} - Shortlisted\n"
            "and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        doc_a = await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": ta})
        doc_b = await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": tb})
        assert doc_a is not None and doc_a["stage"] == "follow_up"
        assert doc_b is not None and doc_b["stage"] == "shortlisted"
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_simple_add_missing_hyphen_recovery_end_to_end():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"HyphenRecover Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"HyphenRecover Talent {tag}")
    try:
        # Only 2 hyphens (one forgotten before the trailing stage phrase).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add - HyphenRecover Talent {tag} - {label} Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_simple_move_multi_pick_ambiguity_resume():
    """Ambiguous talent name inside a hyphen Move -> numbered clarification
    -> a multi-pick reply ("1 and 3") resolves BOTH and the move continues
    for both without the user repeating the command. Uses Move rather than
    Add: bare "Add" always chains into "and move to <stage>" (Pipeline is
    never optional), which turns it into a 2-step PLAN — and a plan step's
    ambiguity is reported inline in the plan card rather than through the
    interactive single-op disambiguation resume this multi-pick fallback
    lives in (a pre-existing platform limitation, mirrored in the WhatsApp
    campaign agent's own plan engine). A pure Move never chains, so it
    stays on the single-op path where multi-pick resume applies."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"MultiPickAmbig Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    p1 = await _seed_talent(f"Prajal Shah {tag}")
    p2 = await _seed_talent(f"Prajal Mehta {tag}")
    p3 = await _seed_talent(f"Prajal Verma {tag}")
    for tid in (p1, p2, p3):
        await _seed_pipeline_row(project_id, tid, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move - Prajal {tag} - {label} - Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple matching talents." in r.reply
        assert f"Prajal Shah {tag}" in r.reply
        assert f"Prajal Mehta {tag}" in r.reply
        assert f"Prajal Verma {tag}" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1 and 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to" in r.reply
        assert f"Prajal Shah {tag}" in r.reply
        assert f"Prajal Verma {tag}" in r.reply
        assert f"Prajal Mehta {tag}" not in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r.reply
        d1 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p1})
        d2 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p2})
        d3 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p3})
        assert d1 is not None and d1["stage"] == "approved"
        assert d2 is not None and d2["stage"] == "ask_to_test"
        assert d3 is not None and d3["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[p1, p2, p3])
        await _restore_config(original)


async def test_simple_pending_test_query_single_and_multi_talent():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(status="ongoing", brand_name=f"PendingTestA Brand {tag}")
    proj_b = await _seed_project(status="ongoing", brand_name=f"PendingTestB Brand {tag}")
    ta = await _seed_talent(f"PendingTestTalentA {tag}")
    tb = await _seed_talent(f"PendingTestTalentB {tag}")
    try:
        await _seed_pipeline_row(proj_a, ta, "ask_to_test")
        await _seed_pipeline_row(proj_b, ta, "ask_to_test")
        await _seed_pipeline_row(proj_a, tb, "approved")  # no pending test

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"pending test - PendingTestTalentA {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 pending tests" in r.reply
        assert "PendingTestA Brand" in r.reply
        assert "PendingTestB Brand" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"pending test - PendingTestTalentA {tag},PendingTestTalentB {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 pending tests" in r.reply
        assert "No pending tests" in r.reply
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_simple_testing_check_multi_talent_multi_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(status="ongoing", brand_name=f"TestingCheckA Brand {tag}")
    proj_b = await _seed_project(status="ongoing", brand_name=f"TestingCheckB Brand {tag}")
    ta = await _seed_talent(f"TestingCheckTalentA {tag}")
    tb = await _seed_talent(f"TestingCheckTalentB {tag}")
    try:
        await _seed_pipeline_row(proj_a, ta, "already_tested")
        await _seed_pipeline_row(proj_b, ta, "ask_to_test")
        await _seed_pipeline_row(proj_a, tb, "shortlisted")
        # tb has no row at all in proj_b -> "Not tested"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"testing? - TestingCheckTalentA {tag},TestingCheckTalentB {tag} - "
                f"TestingCheckA {tag},TestingCheckB {tag}"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"TestingCheckTalentA {tag}" in r.reply
        assert f"TestingCheckTalentB {tag}" in r.reply
        assert f"TestingCheckA Brand {tag} — Tested" in r.reply
        assert f"TestingCheckB Brand {tag} — Test pending" in r.reply
        assert f"TestingCheckA Brand {tag} — Tested — Shortlisted" in r.reply
        assert f"TestingCheckB Brand {tag} — Not tested" in r.reply
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_simple_show_multi_project_multi_stage():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(status="ongoing", brand_name=f"ShowMultiA Brand {tag}")
    proj_b = await _seed_project(status="ongoing", brand_name=f"ShowMultiB Brand {tag}")
    ta = await _seed_talent(f"ShowMultiTalentA {tag}")
    tb = await _seed_talent(f"ShowMultiTalentB {tag}")
    try:
        await _seed_pipeline_row(proj_a, ta, "follow_up")
        await _seed_pipeline_row(proj_b, tb, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"show - ShowMultiA {tag},ShowMultiB {tag} - Follow Up,Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"ShowMultiA Brand {tag} — Follow Up" in r.reply
        assert f"1. ShowMultiTalentA {tag}" in r.reply
        assert f"ShowMultiB Brand {tag} — Approved" in r.reply
        assert f"1. ShowMultiTalentB {tag}" in r.reply
        # No cross-contamination: A's Approved lane and B's Follow Up lane
        # are both empty and must be reported as such, not omitted.
        assert f"ShowMultiA Brand {tag} — Approved\n(none)" in r.reply
        assert f"ShowMultiB Brand {tag} — Follow Up\n(none)" in r.reply
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Combined Casting Pipeline + WhatsApp Automation (2026-08-19) — "add,move,
# send"/"add,send"/"move,send". Parser unit tests first (pure functions, no
# DB), then end-to-end coverage: pipeline write -> WhatsApp send ordering,
# per-project rendering, group isolation, failure safety, fuzzy matching,
# ambiguity resume, and global confirm.
# ---------------------------------------------------------------------------
from unittest.mock import patch  # noqa: E402


async def test_ams_parse_action_words():
    assert _snlu._parse_combined_action_words("add,move,send") == (True, True, True)
    assert _snlu._parse_combined_action_words("add,send") == (True, False, True)
    assert _snlu._parse_combined_action_words("move,send") == (False, True, True)
    assert _snlu._parse_combined_action_words("add,move") == (True, True, False)
    assert _snlu._parse_combined_action_words("add") == (True, False, False)
    assert _snlu._parse_combined_action_words("move") == (False, True, False)
    # A bare "send" (no add/move) is out of scope for this grammar — stays
    # the WhatsApp Campaign Agent's own bare-send grammar.
    assert _snlu._parse_combined_action_words("send") is None
    assert _snlu._parse_combined_action_words("add,add") is None  # duplicate word
    assert _snlu._parse_combined_action_words("addmove") is None
    assert _snlu._parse_combined_action_words("") is None


async def test_ams_parse_combined_command_five_fields():
    parsed = _snlu.parse_simple_add_move_command(
        "add,move,send - Talent A,Talent B - Casting Call - Project A,Project B - Follow Up"
    )
    assert parsed == {
        "action": "add_move", "talent_part": "Talent A,Talent B",
        "template_part": "Casting Call",
        "project_part": "Project A,Project B", "pipeline_part": "Follow Up",
    }


async def test_ams_parse_move_send_action():
    parsed = _snlu.parse_simple_add_move_command(
        "move,send - Talent A,Talent B - Follow Up Template - Project A - Approved"
    )
    assert parsed["action"] == "move"
    assert parsed["template_part"] == "Follow Up Template"


async def test_ams_parse_add_send_action():
    parsed = _snlu.parse_simple_add_move_command("add,send - A - Template - Project - Follow Up")
    # "add" alone (no explicit "move") ALSO chains into add+move — the
    # Pipeline field is always meaningful (see translate_simple_command_
    # to_natural_language) — so "add" and "add_move" translate identically.
    assert parsed["action"] == "add"
    assert parsed["template_part"] == "Template"
    assert _snlu.translate_simple_command_to_natural_language(parsed) == (
        "Add A to Project and move to Follow Up" + _snlu.SEND_TEMPLATE_MARKER + "Template"
    )


async def test_ams_parse_requires_exactly_five_fields():
    # 4 fields (no template) never matches the send-inclusive grammar —
    # falls through to None (natural language), never guesses which field
    # is missing.
    assert _snlu.parse_simple_add_move_command("add,move,send - A - Project A - Follow Up") is None


async def test_ams_translate_embeds_marker():
    parsed = _snlu.parse_simple_add_move_command(
        "add,move,send - A - Casting Call - Project A - Follow Up"
    )
    translated = _snlu.translate_simple_command_to_natural_language(parsed)
    assert translated == (
        "Add A to Project A and move to Follow Up" + _snlu.SEND_TEMPLATE_MARKER + "Casting Call"
    )

    move_parsed = _snlu.parse_simple_add_move_command(
        "move,send - A - Follow Up Template - Project A - Approved"
    )
    move_translated = _snlu.translate_simple_command_to_natural_language(move_parsed)
    assert move_translated == (
        "Move A to Approved in Project A" + _snlu.SEND_TEMPLATE_MARKER + "Follow Up Template"
    )


async def test_ams_marker_survives_and_confirm_suffix():
    text = "add,move,send - A - Casting Call - Project A - Follow Up and confirm"
    translated = _snlu.translate_simple_commands_in_text(text, list(pipeline_router.PIPELINE_STAGE_ORDER))
    stripped, auto_confirm = _snlu.strip_and_confirm(translated)
    assert auto_confirm
    assert stripped == (
        "Add A to Project A and move to Follow Up" + _snlu.SEND_TEMPLATE_MARKER + "Casting Call"
    )


async def test_ams_marker_attaches_to_move_subchunk_of_add_move_chain():
    # The marker ends up on the "move to X" half of an add+move chain
    # (see translate_simple_command_to_natural_language) — both halves
    # share the SAME group index, which is all _strip_send_template_
    # markers actually needs.
    text = "Add A to Project A and move to Follow Up" + _snlu.SEND_TEMPLATE_MARKER + "Casting Call"
    grouped = _snlu.split_actions_grouped(text)
    assert [g for g, _c in grouped] == [0, 0]
    assert grouped[0][1] == "Add A to Project A"
    assert grouped[1][1] == "move to Follow Up" + _snlu.SEND_TEMPLATE_MARKER + "Casting Call"

    from agents.modules.casting_pipeline import _strip_send_template_markers
    cleaned, group_send_template = _strip_send_template_markers(grouped)
    assert cleaned == [(0, "Add A to Project A"), (0, "move to Follow Up")]
    assert group_send_template == {0: "Casting Call"}


async def test_ams_basic_single_talent_renders_all_variables():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(
        f"AMSBasicTpl {tag}",
        "Hi {{talent_name}} ({{full_name}}/{{phone}}), {{project_name}} — Dates {{shoot_dates}} "
        "Budget {{budget}} Link {{submission_link}}",
    )
    project_id = await _seed_project_with_details(
        f"AMSBasicProj {tag}", shoot_dates="1-2 Jan 2028", budget="Rs 11,111/day",
    )
    talent_id = await _seed_talent(f"AMSBasicTalent {tag}", phone="917000600001")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - AMSBasicTalent {tag} - AMSBasicTpl {tag} - AMSBasicProj {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        assert "1 WhatsApp message queued." in r.reply

        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
        body = jobs[0]["message_body"]
        assert "{{" not in body and "}}" not in body
        assert f"AMSBasicTalent {tag}" in body
        assert f"AMSBasicProj {tag}" in body
        assert "1-2 Jan 2028" in body
        assert "Rs 11,111/day" in body
        assert f"https://submit.talentgramagency.com/submit/{project_id}" in body
        assert "917000600001" in body
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_multiple_talents_one_project():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSMultiTalentTpl {tag}")
    project_id = await _seed_project_with_details(
        f"AMSMultiTalentProj {tag}", shoot_dates="5 Dec 2026", budget="Rs 10,000/day",
    )
    t1 = await _seed_talent(f"AMSMTA {tag}", phone="917000600010")
    t2 = await _seed_talent(f"AMSMTB {tag}", phone="917000600011")
    t3 = await _seed_talent(f"AMSMTC {tag}", phone="917000600012")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"add,move,send - AMSMTA {tag},AMSMTB {tag},AMSMTC {tag} - AMSMultiTalentTpl {tag} - "
                f"AMSMultiTalentProj {tag} - Follow Up and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        assert "3 WhatsApp messages queued." in r.reply

        for tid in (t1, t2, t3):
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc is not None and doc["stage"] == "follow_up"

        jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2, t3]}}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2, t3}
        for j in jobs:
            assert "{{" not in j["message_body"]
    finally:
        await _cleanup_jobs_for_talents([t1, t2, t3])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2, t3])
        await _restore_config(original)


async def test_ams_one_talent_multiple_projects_own_context_no_cross_contamination():
    """"add,move,send - Prachi - Casting Call - Amazon,Twamev - Follow Up"
    must generate TWO messages, each with THAT project's own values —
    never one project's context reused for the other."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSMultiProjTpl {tag}")
    proj_a = await _seed_project_with_details(
        f"AMSAmazon {tag}", shoot_dates="1-3 Oct 2026", budget="Rs 20,000/day",
    )
    proj_b = await _seed_project_with_details(
        f"AMSTwamev {tag}", shoot_dates="10-12 Nov 2026", budget="Rs 35,000/day",
    )
    talent_id = await _seed_talent(f"AMSPrachi {tag}", phone="917000600020")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"add,move,send - AMSPrachi {tag} - AMSMultiProjTpl {tag} - "
                f"AMSAmazon {tag},AMSTwamev {tag} - Follow Up and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        assert "2 WhatsApp messages queued." in r.reply

        doc_a = await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": talent_id})
        doc_b = await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": talent_id})
        assert doc_a is not None and doc_a["stage"] == "follow_up"
        assert doc_b is not None and doc_b["stage"] == "follow_up"

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 2
        bodies = [j["message_body"] for j in jobs]
        amazon_body = next(b for b in bodies if f"AMSAmazon {tag}" in b)
        twamev_body = next(b for b in bodies if f"AMSTwamev {tag}" in b)
        assert "1-3 Oct 2026" in amazon_body and "Rs 20,000/day" in amazon_body
        assert "10-12 Nov 2026" not in amazon_body and "Rs 35,000/day" not in amazon_body
        assert "10-12 Nov 2026" in twamev_body and "Rs 35,000/day" in twamev_body
        assert "1-3 Oct 2026" not in twamev_body and "Rs 20,000/day" not in twamev_body
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_multiple_talents_multiple_projects_matrix():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSMatrixTpl {tag}")
    proj_a = await _seed_project_with_details(f"AMSMatrixA {tag}", shoot_dates="1 Jan", budget="Rs 100")
    proj_b = await _seed_project_with_details(f"AMSMatrixB {tag}", shoot_dates="2 Feb", budget="Rs 200")
    t1 = await _seed_talent(f"AMSMatrixT1 {tag}", phone="917000600030")
    t2 = await _seed_talent(f"AMSMatrixT2 {tag}", phone="917000600031")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"add,move,send - AMSMatrixT1 {tag},AMSMatrixT2 {tag} - AMSMatrixTpl {tag} - "
                f"AMSMatrixA {tag},AMSMatrixB {tag} - Follow Up and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        assert "4 WhatsApp messages queued." in r.reply

        for pid in (proj_a, proj_b):
            for tid in (t1, t2):
                doc = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
                assert doc is not None and doc["stage"] == "follow_up", (pid, tid)

        jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2]}}).to_list(10)
        assert len(jobs) == 4
        for j in jobs:
            assert "{{" not in j["message_body"]
    finally:
        await _cleanup_jobs_for_talents([t1, t2])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_ams_multiple_commands_group_isolation():
    """Two independent add,move,send commands in one message — each keeps
    its OWN template/talents/project, and the touched_pairs/send
    accumulator must never leak from one command into the next (the exact
    bug already found and fixed once in this grammar)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl1_id = await _seed_template(f"AMSGroupCasting {tag}")
    tpl2_id = await _seed_template(f"AMSGroupFollow {tag}")
    proj_a = await _seed_project_with_details(f"AMSGroupAmazon {tag}", shoot_dates="1 Jan", budget="Rs 1")
    proj_b = await _seed_project_with_details(f"AMSGroupNike {tag}", shoot_dates="2 Feb", budget="Rs 2")
    ta = await _seed_talent(f"AMSGroupTA {tag}", phone="917000600040")
    tb = await _seed_talent(f"AMSGroupTB {tag}", phone="917000600041")
    tc = await _seed_talent(f"AMSGroupTC {tag}", phone="917000600042")
    td = await _seed_talent(f"AMSGroupTD {tag}", phone="917000600043")
    try:
        text = (
            f"add,move,send - AMSGroupTA {tag},AMSGroupTB {tag} - AMSGroupCasting {tag} - "
            f"AMSGroupAmazon {tag} - Follow Up\n"
            "\n"
            f"add,move,send - AMSGroupTC {tag},AMSGroupTD {tag} - AMSGroupFollow {tag} - "
            f"AMSGroupNike {tag} - Shortlisted\n"
            "and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply

        for tid in (ta, tb):
            doc = await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": tid})
            assert doc is not None and doc["stage"] == "follow_up"
        for tid in (tc, td):
            doc = await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": tid})
            assert doc is not None and doc["stage"] == "shortlisted"
        # No cross-contamination: A/B were never added to Nike, C/D were
        # never added to Amazon.
        assert await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": ta}) is None
        assert await db.casting_pipeline.find_one({"project_id": proj_a, "talent_id": tc}) is None

        jobs_ab = await db.whatsapp_jobs.find({"talent_id": {"$in": [ta, tb]}}).to_list(10)
        jobs_cd = await db.whatsapp_jobs.find({"talent_id": {"$in": [tc, td]}}).to_list(10)
        assert len(jobs_ab) == 2 and all(j["template_id"] == tpl1_id for j in jobs_ab)
        assert len(jobs_cd) == 2 and all(j["template_id"] == tpl2_id for j in jobs_cd)
    finally:
        await _cleanup_jobs_for_talents([ta, tb, tc, td])
        await db.whatsapp_templates.delete_many({"id": {"$in": [tpl1_id, tpl2_id]}})
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[ta, tb, tc, td])
        await _restore_config(original)


async def test_ams_pipeline_update_happens_before_whatsapp_send():
    """Mocks both the pipeline write and the WhatsApp batch creation to
    record call order — the move write must complete before create_batch
    is ever called, never the reverse."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSOrderTpl {tag}")
    project_id = await _seed_project_with_details(f"AMSOrderProj {tag}", shoot_dates="1 Jan", budget="Rs 1")
    talent_id = await _seed_talent(f"AMSOrderTalent {tag}", phone="917000600050")
    call_order = []
    import agents.modules.casting_pipeline as cp_module

    real_bulk_move = cp_module.bulk_move_by_talent_ids
    real_create_batch = cp_module.create_batch

    async def _tracked_bulk_move(*args, **kwargs):
        call_order.append("pipeline_move")
        return await real_bulk_move(*args, **kwargs)

    async def _tracked_create_batch(*args, **kwargs):
        call_order.append("whatsapp_create_batch")
        return await real_create_batch(*args, **kwargs)

    try:
        with patch.object(cp_module, "bulk_move_by_talent_ids", _tracked_bulk_move), \
             patch.object(cp_module, "create_batch", _tracked_create_batch):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"add,move,send - AMSOrderTalent {tag} - AMSOrderTpl {tag} - AMSOrderProj {tag} - Follow Up and confirm",
                sender_name="Raj", sender_is_group_member=True,
            )
        assert r.handled
        assert call_order == ["pipeline_move", "whatsapp_create_batch"]
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_pipeline_failure_blocks_whatsapp_send():
    """If bulk_move_by_talent_ids raises, no WhatsApp job may be created
    for that talent — the pipeline update must succeed before any send is
    attempted."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSFailTpl {tag}")
    project_id = await _seed_project_with_details(f"AMSFailProj {tag}", shoot_dates="1 Jan", budget="Rs 1")
    talent_id = await _seed_talent(f"AMSFailTalent {tag}", phone="917000600060")
    import agents.modules.casting_pipeline as cp_module

    async def _raising_bulk_move(*args, **kwargs):
        raise RuntimeError("simulated pipeline write failure")

    try:
        with patch.object(cp_module, "bulk_move_by_talent_ids", _raising_bulk_move):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"add,move,send - AMSFailTalent {tag} - AMSFailTpl {tag} - AMSFailProj {tag} - Follow Up and confirm",
                sender_name="Raj", sender_is_group_member=True,
            )
        assert r.handled
        assert "✗" in r.reply
        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert jobs == []
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_global_confirm_no_intermediate_prompts():
    """"and confirm" must execute the WHOLE combined operation (pipeline +
    send) without ever asking for a separate confirmation of either half."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSConfirmTpl {tag}")
    project_id = await _seed_project_with_details(f"AMSConfirmProj {tag}", shoot_dates="1 Jan", budget="Rs 1")
    talent_id = await _seed_talent(f"AMSConfirmTalent {tag}", phone="917000600070")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - AMSConfirmTalent {tag} - AMSConfirmTpl {tag} - AMSConfirmProj {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Reply:" not in r.reply
        assert "1 → Approve" not in r.reply
        assert "Completed" in r.reply
        assert "1 WhatsApp message queued." in r.reply
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_without_confirm_shows_pending_send_in_preview_then_sends_on_approval():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSPreviewTpl {tag}")
    project_id = await _seed_project_with_details(f"AMSPreviewProj {tag}", shoot_dates="1 Jan", budget="Rs 1")
    talent_id = await _seed_talent(f"AMSPreviewTalent {tag}", phone="917000600080")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - AMSPreviewTalent {tag} - AMSPreviewTpl {tag} - AMSPreviewProj {tag} - Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to run this plan:" in r.reply
        assert f"AMSPreviewTpl {tag}" in r.reply
        assert "Reply:" in r.reply
        # Nothing sent yet.
        assert await db.whatsapp_jobs.find_one({"talent_id": talent_id}) is None

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r2.reply
        assert "1 WhatsApp message queued." in r2.reply
        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_fuzzy_talent_and_project_typo_tolerance():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSFuzzyTpl {tag}")
    project_id = await _seed_project_with_details(
        f"Tira Suhana Film {tag}", shoot_dates="1 Jan", budget="Rs 1",
    )
    talent_id = await _seed_talent(f"Ayushi Thakur {tag}", phone="917000600090")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"add,move,send - Ayushii Thakur {tag} - AMSFuzzyTpl {tag} - "
                f"Tira Suhana Project {tag} - Follow Up and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_ams_ambiguous_talent_shows_full_list_never_sends_prematurely():
    """"add,move,send" always chains into a 2-step (add+move) PLAN — an
    ambiguous talent inside a plan step is reported INLINE in the plan
    preview text (never as an interactive numbered pending_disambiguation
    round), a PRE-EXISTING limitation of the plan engine shared by every
    other "Add"-chaining combined command (documented, not introduced by
    this feature — see the module's own "resend that one command alone to
    disambiguate it" convention). What this DOES guarantee, and what this
    test verifies: the FULL candidate list is always shown (never
    truncated), and — critically for THIS feature specifically — an
    unresolved ambiguity must never let a WhatsApp message go out."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    tpl_id = await _seed_template(f"AMSAmbigTpl {tag}")
    project_id = await _seed_project_with_details(f"AMSAmbigProj {tag}", shoot_dates="1 Jan", budget="Rs 1")
    p1 = await _seed_talent(f"Ayushi Thakur {tag}", phone="917000600100")
    p2 = await _seed_talent(f"Ayushi Singh {tag}", phone="917000600101")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - Ayushi {tag} - AMSAmbigTpl {tag} - AMSAmbigProj {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        assert f"Ayushi Thakur {tag}" in r.reply
        assert f"Ayushi Singh {tag}" in r.reply
        assert "WhatsApp message" not in r.reply
        assert await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p1}) is None
        assert await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p2}) is None
        assert await db.whatsapp_jobs.find_one({"talent_id": {"$in": [p1, p2]}}) is None

        # Resending with the exact, disambiguated name succeeds cleanly.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - Ayushi Thakur {tag} - AMSAmbigTpl {tag} - AMSAmbigProj {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Completed" in r2.reply
        assert "1 WhatsApp message queued." in r2.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p1})
        assert doc is not None and doc["stage"] == "follow_up"
        assert await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": p2}) is None
        jobs = await db.whatsapp_jobs.find({"talent_id": p1}).to_list(10)
        assert len(jobs) == 1
    finally:
        await _cleanup_jobs_for_talents([p1, p2])
        await db.whatsapp_templates.delete_one({"id": tpl_id})
        await _cleanup(phone, project_ids=[project_id], talent_ids=[p1, p2])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Enhancements (2026-08-20):
#   1. Whole-Stage Move — "move - Project - StageFrom to StageTo" moves
#      EVERY talent currently in StageFrom to StageTo, no talent named.
#   2. Whitespace-tolerant hyphen grammar — every simplified command also
#      parses with no spaces around its "-" separators.
# ---------------------------------------------------------------------------
async def test_stage_move_parser_unit():
    STAGES = list(pipeline_router.PIPELINE_STAGE_ORDER)
    parsed = _snlu.parse_simple_stage_move_command(
        "move - Bajaj Almond Oil - ask to test to follow up", STAGES,
    )
    assert parsed == {"project_part": "Bajaj Almond Oil", "from_stage": "ask_to_test", "to_stage": "follow_up"}

    # No spaces at all.
    parsed2 = _snlu.parse_simple_stage_move_command(
        "move-Bajaj Almond Oil-ask to test to follow up", STAGES,
    )
    assert parsed2 == parsed

    # Must NEVER shadow the named-talent 4-field grammar.
    assert _snlu.parse_simple_stage_move_command(
        "Move - Riya Patel - Toyota Campaign - Follow Up", STAGES,
    ) is None
    # Not a bare "move" -> None.
    assert _snlu.parse_simple_stage_move_command(
        "add,move - Project A - Follow Up to Approved", STAGES,
    ) is None
    # No "to" connector between two stages -> None.
    assert _snlu.parse_simple_stage_move_command("move - Project A - Follow Up", STAGES) is None


async def test_stage_move_basic_confirmation_then_approve():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"StageMoveProj {tag}")
    t1 = await _seed_talent(f"StageMoveT1 {tag}")
    t2 = await _seed_talent(f"StageMoveT2 {tag}")
    t3 = await _seed_talent(f"StageMoveT3 {tag}")
    distractor = await _seed_talent(f"StageMoveDistractor {tag}")
    try:
        for tid in (t1, t2, t3):
            await _seed_pipeline_row(project_id, tid, "ask_to_test")
        await _seed_pipeline_row(project_id, distractor, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageMoveProj {tag} - ask to test to follow up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to move" in r.reply
        assert "Ask To Test" in r.reply
        assert "Follow Up" in r.reply
        for tid, label in ((t1, f"StageMoveT1 {tag}"), (t2, f"StageMoveT2 {tag}"), (t3, f"StageMoveT3 {tag}")):
            assert label in r.reply
        # Nothing written yet.
        for tid in (t1, t2, t3):
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc["stage"] == "ask_to_test"

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r2.reply
        for tid in (t1, t2, t3):
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc["stage"] == "follow_up"
        # Distractor (a different stage) is completely untouched.
        doc_d = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": distractor})
        assert doc_d["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2, t3, distractor])
        await _restore_config(original)


async def test_stage_move_exclude_names_then_approve():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"StageExclProj {tag}")
    t1 = await _seed_talent(f"StageExclT1 {tag}")
    t2 = await _seed_talent(f"StageExclT2 {tag}")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageExclProj {tag} - ask to test to follow up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"StageExclT1 {tag}" in r.reply and f"StageExclT2 {tag}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"exclude StageExclT1 {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"StageExclT1 {tag}" not in r2.reply
        assert f"StageExclT2 {tag}" in r2.reply
        assert "Reply:" in r2.reply  # still on the confirmation card, not left the step

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r3.reply
        doc1 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        doc2 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t2})
        assert doc1["stage"] == "ask_to_test"  # excluded — never moved
        assert doc2["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_stage_move_and_confirm_executes_immediately_full_set():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"StageConfirmProj {tag}")
    t1 = await _seed_talent(f"StageConfirmT1 {tag}")
    t2 = await _seed_talent(f"StageConfirmT2 {tag}")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageConfirmProj {tag} - ask to test to follow up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Reply:" not in r.reply
        assert "Done." in r.reply
        for tid in (t1, t2):
            doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": tid})
            assert doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_stage_move_cancel_makes_no_changes():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"StageCancelProj {tag}")
    t1 = await _seed_talent(f"StageCancelT1 {tag}")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageCancelProj {tag} - ask to test to follow up",
            sender_name="Raj", sender_is_group_member=True,
        )
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "ask_to_test"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1])
        await _restore_config(original)


async def test_stage_move_no_one_in_from_stage():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"StageEmptyProj {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageEmptyProj {tag} - ask to test to follow up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "no one" in r.reply.lower() or "not sure" in r.reply.lower() or r.reply
        assert "Reply:" not in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[])
        await _restore_config(original)


async def test_stage_move_ambiguous_project_disambiguates_and_resumes():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    proj_a = await _seed_project(brand_name=f"StageAmbig Alpha {tag}")
    proj_b = await _seed_project(brand_name=f"StageAmbig Beta {tag}")
    label_b = (await db.projects.find_one({"id": proj_b}))["brand_name"]
    t1 = await _seed_talent(f"StageAmbigT1 {tag}")
    try:
        await _seed_pipeline_row(proj_b, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move - StageAmbig {tag} - ask to test to follow up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=label_b,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to move" in r2.reply
        assert f"StageAmbigT1 {tag}" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Done." in r3.reply
        doc = await db.casting_pipeline.find_one({"project_id": proj_b, "talent_id": t1})
        assert doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[proj_a, proj_b], talent_ids=[t1])
        await _restore_config(original)


async def test_stage_move_never_shadows_named_talent_move():
    """Regression guard: the existing "Move - Talent - Project - Pipeline"
    grammar (and its stage-to-stage pipeline field) must keep working
    completely unchanged."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"NamedMoveProj {tag}")
    t1 = await _seed_talent(f"NamedMoveT1 {tag}")
    t2 = await _seed_talent(f"NamedMoveT2 {tag}")  # must stay untouched
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move - NamedMoveT1 {tag} - NamedMoveProj {tag} - Ask To Test to Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        doc1 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        doc2 = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t2})
        assert doc1["stage"] == "follow_up"
        assert doc2["stage"] == "ask_to_test"  # never named -> never moved
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Whitespace-tolerant hyphen grammar (2026-08-20)
# ---------------------------------------------------------------------------
async def test_whitespace_tolerant_detect_trigger_unit():
    from agents import parser as agent_parser
    from agents.modules.casting_pipeline import MOVE_INTENT, ADD_INTENT

    class _FakeAgent:
        intents = [MOVE_INTENT, ADD_INTENT]

    assert agent_parser.detect_trigger(_FakeAgent(), "move-Project-Stage") is MOVE_INTENT
    assert agent_parser.detect_trigger(_FakeAgent(), "move - Project - Stage") is MOVE_INTENT
    assert agent_parser.detect_trigger(_FakeAgent(), "add-Talent-Project-Stage") is ADD_INTENT


async def test_whitespace_tolerant_add_move_no_spaces_end_to_end():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"NoSpaceAddProj {tag}")
    talent_id = await _seed_talent(f"NoSpaceAddTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add-NoSpaceAddTalent {tag}-NoSpaceAddProj {tag}-Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "✗" not in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_whitespace_tolerant_missing_hyphen_recovery_still_works():
    """Regression guard: the EXISTING (spaced) missing-hyphen recovery
    must keep working unchanged now that the action-word matching was
    restructured into a prefix-first approach."""
    parsed = _snlu.parse_simple_add_move_command(
        "Add - Riya Patel - Toyota Campaign Follow Up",
        stage_order=list(pipeline_router.PIPELINE_STAGE_ORDER),
    )
    assert parsed is not None
    assert parsed["project_part"] == "Toyota Campaign"
    assert parsed["pipeline_part"] == "Follow Up"


async def test_whitespace_tolerant_hyphenated_name_still_resolves_with_spaced_separators():
    """A project name that itself contains a no-space hyphen ("Co-Star
    Casting") must still resolve correctly as long as the command's OWN
    field separators use the documented " - " form: the strict, spaced
    split (always tried FIRST) already finds the right field count
    immediately in that case, so the no-space-tolerant fallback (which
    could otherwise mis-split "Co-Star" into two fields) is never even
    reached. (A project name that itself uses SPACED " - " internally,
    e.g. "Tira - Suhana's Film", is a separate, pre-existing ambiguity —
    unrelated to this fix — since the field-count itself becomes genuinely
    unclear regardless of any whitespace tolerance.)"""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"Co-Star Casting {tag}")
    talent_id = await _seed_talent(f"HyphenNameTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add - HyphenNameTalent {tag} - Co-Star Casting {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_whitespace_tolerant_query_commands_no_spaces():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(status="ongoing", brand_name=f"NoSpaceQueryProj {tag}")
    talent_id = await _seed_talent(f"NoSpaceQueryTalent {tag}")
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"pending test-NoSpaceQueryTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1 pending test" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"testing?-NoSpaceQueryTalent {tag}-NoSpaceQueryProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert f"NoSpaceQueryTalent {tag}" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"show-NoSpaceQueryProj {tag}-Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r3.handled
        assert f"NoSpaceQueryTalent {tag}" in r3.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_whitespace_tolerant_stage_move_end_to_end():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"NoSpaceStageProj {tag}")
    t1 = await _seed_talent(f"NoSpaceStageT1 {tag}")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"move-NoSpaceStageProj {tag}-ask to test to follow up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Done." in r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert doc["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Production incident (2026-08-25) — casting-agent's whatsapp_agent_config
# doc had group_names=[] in production (created_at == updated_at, never
# touched since creation), which is silently valid per resolve_agent_for_group
# ("no groups configured" == "matches nothing"), so a real message sent into
# the actual "Talentgram Casting Pipeline" group produced NO reply at all —
# not a parser/dispatcher/executor bug, the message never even reached
# routing. seed_agent_config() never overwrites an existing doc (by design,
# to protect admin edits), so once a config drifts to an empty group list it
# stays broken across every future restart until someone notices and fixes
# the data directly. This test reproduces the exact silent-drop behavior
# against a disposable config (never touches the real casting-agent doc),
# so a future change to make empty-group_names behave differently is a
# deliberate, visible decision, not an accidental regression.
# ---------------------------------------------------------------------------
async def test_empty_group_names_silently_drops_every_message():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [],  # the exact production incident's broken state
        "allowed_senders": [],
        "security_mode": "group_members",
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, doc, upsert=True)
    phone = _phone()
    try:
        resolved = await registry.resolve_agent_for_group(group)
        assert resolved is None, "an agent with group_names=[] must never resolve for ANY group name"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Show Ask To Test",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False, r
        assert not r.reply, r  # completely silent — no reply text at all
    finally:
        await _restore_config(original)


async def test_agent_config_health_check_flags_empty_group_names():
    """The systemic gap the production incident exposed: an active agent
    with an empty group list is functionally dead but was never flagged
    anywhere — the admin only finds out by a real command going silently
    unanswered. registry.find_agents_with_empty_group_names() is the
    lightweight, read-only health check ensure_agents_ready() now logs a
    warning from on every backend startup, so this state is visible in
    logs immediately instead of persisting unnoticed indefinitely."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [],
        "allowed_senders": [],
        "security_mode": "group_members",
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, doc, upsert=True)
    try:
        broken = await registry.find_agents_with_empty_group_names()
        assert AGENT_ID in broken, broken
    finally:
        await _restore_config(original)
        broken_after = await registry.find_agents_with_empty_group_names()
        assert AGENT_ID not in broken_after, broken_after


# ---------------------------------------------------------------------------
# Command Enhancement (2026-08-27) — light regression coverage for the
# specific new behaviors added this pass. Deliberately NOT re-testing
# everything the 150+ tests above already cover (bulk ADD/MOVE via the
# hyphen grammar, undo, ambiguity itself, SEND approval-gating) — those are
# unchanged and still exercised by the existing suites (this file, plus
# tests/test_media_send.py for SEND).
# ---------------------------------------------------------------------------
async def test_comma_whitespace_normalization_matches_no_whitespace():
    """"Talent A, Talent B ,Talent C" must resolve identically to
    "Talent A,Talent B,Talent C" — both end up adding the exact same two
    talents, regardless of stray spaces around the commas."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    pid = await _seed_project(brand_name=f"WhitespaceProj {tag}")
    label = (await db.projects.find_one({"id": pid}))["brand_name"]
    ta = await _seed_talent(f"WsTalentA {tag}")
    tb = await _seed_talent(f"WsTalentB {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"Add WsTalentA {tag} ,  WsTalentB {tag} to {label} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        rows = await db.casting_pipeline.find({"project_id": pid}).to_list(10)
        assert {row["talent_id"] for row in rows} == {ta, tb}, r.reply
    finally:
        await _restore_config(original)
        await db.projects.delete_one({"id": pid})
        await db.talents.delete_many({"id": {"$in": [ta, tb]}})
        await db.casting_pipeline.delete_many({"project_id": pid})


async def test_which_projects_is_talent_in_natural_variant():
    """New natural-language variant for casting.query's talent_projects
    kind: "Which projects is X in?" (bare trailing "in", no "part of"/
    "working on" phrasing) — must resolve, not fall through to "I didn't
    understand that"."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    pid = await _seed_project(brand_name=f"WhichProj {tag}")
    label = (await db.projects.find_one({"id": pid}))["brand_name"]
    tid = await _seed_talent(f"WhichTalent {tag}")
    await _seed_pipeline_row(pid, tid, "shortlisted")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"Which projects is WhichTalent {tag} in",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "didn't understand" not in r.reply.lower(), r.reply
        assert label in r.reply, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"What projects does WhichTalent {tag} have",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "didn't understand" not in r2.reply.lower(), r2.reply
        assert label in r2.reply, r2.reply
    finally:
        await _restore_config(original)
        await db.projects.delete_one({"id": pid})
        await db.talents.delete_one({"id": tid})
        await db.casting_pipeline.delete_many({"project_id": pid})


async def test_natural_language_add_and_move_verb_first_bulk():
    """"Add and move Talent A, Talent B to Project A, Project B to Stage
    and confirm" — the compound-VERB-LEADING natural phrasing (distinct
    from the already-working "Add X to Y and move to Z") — must add AND
    move every talent x project combination."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    pa = await _seed_project(brand_name=f"NLProjA {tag}")
    pb = await _seed_project(brand_name=f"NLProjB {tag}")
    la = (await db.projects.find_one({"id": pa}))["brand_name"]
    lb = (await db.projects.find_one({"id": pb}))["brand_name"]
    ta = await _seed_talent(f"NLTalentA {tag}")
    tb = await _seed_talent(f"NLTalentB {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"Add and move NLTalentA {tag}, NLTalentB {tag} to {la}, {lb} to Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        rows = await db.casting_pipeline.find({"project_id": {"$in": [pa, pb]}}).to_list(20)
        assert len(rows) == 4, rows
        assert all(row["stage"] == "follow_up" for row in rows), rows
    finally:
        await _restore_config(original)
        await db.projects.delete_many({"id": {"$in": [pa, pb]}})
        await db.talents.delete_many({"id": {"$in": [ta, tb]}})
        await db.casting_pipeline.delete_many({"project_id": {"$in": [pa, pb]}})


async def test_typo_tolerant_but_genuine_ambiguity_still_asks():
    """A minor typo against a UNIQUE name still resolves; two genuinely
    similar names still stop and ask — existing ambiguity safeguards are
    not weakened by anything added this pass."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    pid = await _seed_project(brand_name=f"TypoProj {tag}")
    label = (await db.projects.find_one({"id": pid}))["brand_name"]
    unique = await _seed_talent(f"Zolara Kapoor {tag}")
    dup_a = await _seed_talent(f"Rahul Sharma {tag}")
    dup_b = await _seed_talent(f"Rahul Sharm {tag}")
    try:
        # Minor one-letter typo still surfaces the intended talent as a
        # candidate (fuzzy matching isn't disabled) — whether it resolves
        # outright or appears in a "did you mean" list alongside unrelated
        # names is this system's own pre-existing, unmodified matching
        # behavior; this only guards that the typo is TOLERATED, not
        # rejected outright as "no match at all".
        r = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"show projects of Zolara Kapor {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Zolara Kapoor {tag}" in r.reply, r.reply

        # Genuinely similar names still stop and ask, never guess.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=_phone(),
            text=f"show projects of Rahul Sha {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "multiple matching talents" in r2.reply.lower(), r2.reply
    finally:
        await _restore_config(original)
        await db.projects.delete_one({"id": pid})
        await db.talents.delete_many({"id": {"$in": [unique, dup_a, dup_b]}})


async def test_clarification_reply_resumes_original_move_command():
    """A bare clarification reply ("1") resumes and completes the
    ORIGINAL pending move — the admin never repeats the command."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    pid = await _seed_project(brand_name=f"ResumeProj {tag}")
    label = (await db.projects.find_one({"id": pid}))["brand_name"]
    dup_a = await _seed_talent(f"Rahul Sharma {tag}")
    dup_b = await _seed_talent(f"Rahul Sharm {tag}")
    await _seed_pipeline_row(pid, dup_a, "ask_to_test")
    await _seed_pipeline_row(pid, dup_b, "ask_to_test")
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Rahul Sha to Approved in {label} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "multiple matching talents" in r.reply.lower(), r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Moved 1 talent" in r2.reply, r2.reply
        row_a = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": dup_a})
        assert row_a["stage"] == "approved", row_a
    finally:
        await _restore_config(original)
        await db.projects.delete_one({"id": pid})
        await db.talents.delete_many({"id": {"$in": [dup_a, dup_b]}})
        await db.casting_pipeline.delete_many({"project_id": pid})


def test_help_text_mentions_send_and_new_query_forms():
    """HELP must mention SEND (previously entirely absent) and the new
    show-projects/has-tested natural-language forms, without dropping any
    existing entry (Add/Move/Add,Move/Undo/testing?/pending test)."""
    from agents.modules.casting_pipeline import HELP_TEXT
    for existing in ("Add - ", "Move - ", "Add,Move - ", "testing?", "pending test", "and confirm"):
        assert existing in HELP_TEXT, f"missing pre-existing HELP entry: {existing!r}"
    for new in ("send - ", "show projects of", "has Ayushi tested", "which projects is"):
        assert new in HELP_TEXT, f"missing new HELP entry: {new!r}"


def test_whatsapp_campaign_agent_uses_group_members_security_mode():
    """Command Enhancement requirement #1: every number currently in (or
    later added to) the WhatsApp Agent group may issue commands — the
    seed default must no longer be a single-number allowlist."""
    import inspect
    import agents
    src = inspect.getsource(agents.ensure_agents_ready)
    # The whatsapp-campaign-agent seed call must now pass group_members —
    # a crude but effective regression guard against silently reverting
    # to the old single-number allowlist default.
    idx = src.index('"whatsapp-campaign-agent"')
    call_src = src[idx:idx + 800]
    assert 'security_mode="group_members"' in call_src, call_src


# ---------------------------------------------------------------------------
# Compound-plan UNDO (2026-08-27, Command Specification V1 Phase 2) — the
# confirmed P1 gap: an "Add,Move" (or "Add,Move,Send") plan step never
# created an undo record, unlike a standalone MOVE. Fixed by having
# _execute_plan's own move sub-step call the EXACT SAME undo_store.
# store_undo used by _move_executor — no second undo system, no change to
# plain MOVE/UNDO's own behavior (already covered by the many existing
# undo tests above, all still passing unchanged).
# ---------------------------------------------------------------------------
async def test_compound_add_move_creates_undo_record_and_undo_reverts_it():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"CompoundUndo Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"CompoundUndo Talent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add,Move - CompoundUndo Talent {tag} - {label} - Shortlisted and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "shortlisted", doc
        assert "Reply UNDO" in r.reply, r.reply

        pending = await undo_store.get_undo(AGENT_ID, phone)
        assert pending is not None, "Add,Move must now record an undo entry, same as a standalone MOVE"
        assert pending["operation"]["project_id"] == project_id
        assert pending["operation"]["previous_stage_by_id"].get(talent_id) == "ask_to_test"

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="undo",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Undo complete" in r2.reply, r2.reply
        doc_after = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        # Reverts the STAGE (back to what ADD itself set moments earlier) —
        # never removes the talent from the pipeline entirely; that would be
        # a materially different operation this fix deliberately does not
        # attempt (see the module comment on the store_undo call site).
        assert doc_after["stage"] == "ask_to_test", doc_after
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_compound_add_move_noop_step_never_creates_misleading_undo():
    """A move sub-step that's already at its target stage (skipped, no
    write) must never create/overwrite an undo record — only a step that
    actually wrote something may."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"NoopUndo Brand {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"NoopUndo Talent {tag}")
    try:
        before = await undo_store.get_undo(AGENT_ID, phone)
        assert before is None
        # ADD defaults to "Ask To Test" — moving to that same stage right
        # after is a genuine no-op for the move sub-step.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add,Move - NoopUndo Talent {tag} - {label} - Ask To Test and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        after = await undo_store.get_undo(AGENT_ID, phone)
        assert after is None, "a no-op move step must never fabricate an undo record"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Natural-language bulk HAS TESTED (2026-08-27, Command Specification V1
# Phase 3A) — "has A,B tested for X" now fans out via the existing
# _handle_testing_check, exactly like the structured "testing? - A,B - X"
# form already did; the single-item natural form (test_talent_stage_query*
# elsewhere in this file) is completely unaffected.
# ---------------------------------------------------------------------------
async def test_natural_language_has_tested_bulk_talents():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"BulkQProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    ta = await _seed_talent(f"Priyanka Bulkq {tag}")
    tb = await _seed_talent(f"Rohan Bulkq {tag}")
    await _seed_pipeline_row(project_id, ta, "shortlisted")
    # tb has no pipeline row at all -> never tested
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"has Priyanka Bulkq {tag},Rohan Bulkq {tag} tested for {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"Priyanka Bulkq {tag}" in r.reply and "Shortlisted" in r.reply, r.reply
        assert f"Rohan Bulkq {tag}" in r.reply and "Not tested" in r.reply, r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[ta, tb])
        await _restore_config(original)


async def test_bare_hyphen_no_space_add_end_to_end():
    """The exact originally-reported failure (Command Specification V1
    Phase 3C): "Add-Talent-Project" with no spaces around either hyphen
    and no pipeline segment must add the talent at the default stage,
    not mis-split into a confusing "which project?" prompt."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"BareHyphenProj{tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"BareHyphenTalent{tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add-BareHyphenTalent{tag}-{label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Which project" not in r.reply, r.reply
        assert "You are about to add" in r.reply, r.reply
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "ask_to_test", (r2.reply, doc)
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_canonical_tested_bare_word_trigger_single_and_bulk():
    """Command Simplification (2026-08-27): "TESTED <talent> FOR <project>"
    as its OWN leading word (no "has"/"is" lead-in) must open casting.query
    and answer with the real current stage — both single and bulk."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"TestedProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    ta = await _seed_talent(f"Kavya Cq {tag}")
    tb = await _seed_talent(f"Meera Cq {tag}")
    await _seed_pipeline_row(project_id, ta, "shortlisted")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Tested Kavya Cq {tag} for {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Shortlisted" in r.reply, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Tested Kavya Cq {tag},Meera Cq {tag} for {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Kavya Cq {tag}" in r2.reply and "Shortlisted" in r2.reply, r2.reply
        assert f"Meera Cq {tag}" in r2.reply and "Not tested" in r2.reply, r2.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[ta, tb])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Command Simplification + SHARE (2026-08-27) — casting.share.
# ---------------------------------------------------------------------------
async def test_share_casting_call_single_project_single_talent():
    """"share casting call for X to Y" resolves the SAME real, seeded
    "Casting Call" template (routers/whatsapp.py's default-template seed)
    the existing Add,Move,Send compound command already uses — never a
    second/duplicate template."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"ShareProj {tag}", shoot_dates="1 Jan 2028", budget="Rs 5,000/day",
    )
    talent_id = await _seed_talent(f"ShareTalent {tag}", phone="917000700001")
    # A "Casting Call" send renders {{project_name}}/{{shoot_dates}}/
    # {{budget}} via create_batch's PROJECT-source recipient resolution
    # (routers/whatsapp.py's resolve_recipients_engine), which requires an
    # existing casting_pipeline row for this project — SHARE reuses that
    # exact rendering path unmodified, so the recipient must already be
    # somewhere in this project's pipeline, same as a real "invite this
    # already-shortlisted talent to confirm" use.
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting call for ShareProj {tag} to ShareTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SHARE PREVIEW" in r.reply, r.reply
        assert "Casting Call" in r.reply, r.reply
        assert f"ShareProj {tag}" in r.reply, r.reply
        assert f"ShareTalent {tag}" in r.reply, r.reply
        assert "NOT SENT" not in r.reply  # not part of this preview's wording, just confirming no false claim
        assert "1 message" in r.reply, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "Shared." in r2.reply, r2.reply
        assert "1 WhatsApp message queued." in r2.reply, r2.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
        body = jobs[0]["message_body"]
        assert "{{" not in body
        assert f"ShareProj {tag}" in body
        assert "1 Jan 2028" in body
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_share_multi_project_multi_talent_cross_product():
    """"share casting calls for P1,P2 to T1,T2" fans out as a real
    cross-product: each project sends to BOTH talents (4 messages total),
    each with THAT project's own rendered values."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project_with_details(f"ShareCPA {tag}", shoot_dates="2 Feb 2028", budget="Rs 1,000/day")
    p2 = await _seed_project_with_details(f"ShareCPB {tag}", shoot_dates="3 Mar 2028", budget="Rs 2,000/day")
    t1 = await _seed_talent(f"ShareCPT1 {tag}", phone="917000700010")
    t2 = await _seed_talent(f"ShareCPT2 {tag}", phone="917000700011")
    # See test_share_casting_call_single_project_single_talent's comment —
    # both talents need a pipeline row in BOTH projects for the cross
    # product to actually send all 4 messages.
    for pid in (p1, p2):
        for tid in (t1, t2):
            await _seed_pipeline_row(pid, tid, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting calls for ShareCPA {tag},ShareCPB {tag} to ShareCPT1 {tag},ShareCPT2 {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "4 WhatsApp messages queued." in r.reply, r.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2]}}).to_list(10)
        assert len(jobs) == 4
        by_project = {}
        for j in jobs:
            by_project.setdefault(j["source_id"], []).append(j)
        assert set(by_project.keys()) == {p1, p2}
        for j in by_project[p1]:
            assert "2 Feb 2028" in j["message_body"] and "Rs 1,000/day" in j["message_body"]
        for j in by_project[p2]:
            assert "3 Mar 2028" in j["message_body"] and "Rs 2,000/day" in j["message_body"]
    finally:
        await _cleanup_jobs_for_talents([t1, t2])
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_share_to_pipeline_targets_everyone_currently_in_it():
    """"share casting call for X to pipeline" reuses create_batch's own
    project-wide recipient resolution — no separate/duplicate "everyone in
    this pipeline" lookup — so it must reach every talent CURRENTLY in
    that project's pipeline, not just talents named in the command."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"SharePipeProj {tag}", shoot_dates="4 Apr 2028", budget="Rs 3,000/day",
    )
    t1 = await _seed_talent(f"SharePipeT1 {tag}", phone="917000700020")
    t2 = await _seed_talent(f"SharePipeT2 {tag}", phone="917000700021")
    await _seed_pipeline_row(project_id, t1, "ask_to_test")
    await _seed_pipeline_row(project_id, t2, "shortlisted")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting call for SharePipeProj {tag} to pipeline and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "queued" in r.reply, r.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2]}}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
    finally:
        await _cleanup_jobs_for_talents([t1, t2])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_share_bare_template_word_is_ambiguous_never_guessed():
    """A bare "share template for X to Y" (no specific template named) must
    ask which template rather than silently picking one — this repo's real
    template registry has several (Casting Call, Follow Up, Shortlisted,
    ...), so this is a genuine, live ambiguity, not a contrived one."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"ShareAmbigProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"ShareAmbigTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share template for {label} to ShareAmbigTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Which template should I share?" in r.reply, r.reply
        jobs = await db.whatsapp_jobs.find({}).to_list(1)
        # Not asserting jobs == [] globally (shared dev DB) — the real
        # assertion is that OUR talent never received anything.
        mine = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert mine == []
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_share_hyphen_form_and_missing_recipient_asks():
    """"share - Project - Talent" structured form works, and a message
    missing the recipient entirely asks instead of guessing/erroring
    unhelpfully."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"ShareHyphenProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"ShareHyphenTalent {tag}", phone="917000700030")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share - {label} - ShareHyphenTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SHARE PREVIEW" in r.reply, r.reply
        assert f"ShareHyphenTalent {tag}" in r.reply, r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Compound Actions (2026-08-27) — ADD, MOVE, SHARE, SEND combinable in one
# instruction (Master Prompt: "make SHARE behave like the other pipeline
# actions"). Focused regression coverage only (8 tests, per spec) — the
# underlying SHARE/SEND mechanics themselves are already covered above and
# in test_media_send.py; these tests are specifically about the COMBINING.
# ---------------------------------------------------------------------------

async def test_1_standalone_share_still_works_after_compound_actions():
    """Test 1 — standalone SHARE, completely unaffected by the compound-
    plan wiring added in this pass."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"StandaloneShareProj {tag}", shoot_dates="6 Jun 2028", budget="Rs 6,000/day",
    )
    talent_id = await _seed_talent(f"StandaloneShareTalent {tag}", phone="917000900001")
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting call for StandaloneShareProj {tag} to StandaloneShareTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SHARE PREVIEW" in r.reply, r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_2_compound_add_move_share():
    """Test 2 — ADD, MOVE, SHARE chained in one instruction, comma-
    separated, with SHARE's recipient/project both left implicit
    ("her"/nothing named) and correctly inherited from what ADD+MOVE just
    touched."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"CompAMSProj {tag}", shoot_dates="1 Jul 2028", budget="Rs 7,000/day",
    )
    talent_id = await _seed_talent(f"CompAMSTalent {tag}", phone="917000900010")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompAMSTalent {tag} to CompAMSProj {tag}, "
                "Move her to shortlisted, Share the casting call with her and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Completed" in r.reply, r.reply
        assert "Share Casting Call" in r.reply, r.reply
        assert "1 WhatsApp message queued." in r.reply, r.reply

        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "shortlisted", (r.reply, doc)

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
        assert "CompAMSProj" in jobs[0]["message_body"]
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_3_compound_add_move_share_send():
    """Test 3 — the full ADD, MOVE, SHARE, SEND chain in one instruction.
    Verifies ADD/MOVE/SHARE all completed AND that SEND's own form
    preview is shown as part of the SAME reply — see Test 8 for the
    dedicated approval-gate-integrity checks on this exact flow."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    email = f"amsend.{tag}@example.com"
    project_id = await _seed_send_project(f"CompAMSSProj {tag}", whatsapp_casting_group_name="Talentgram Casting Test")
    talent_id = await _seed_send_talent(
        f"CompAMSSTalent {tag}", whatsapp_group_name=f"CompAMSSTalent {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_send_submission(project_id, talent_id, email, decision="approved")
    await db[_ma.IDENTITY_COLLECTION].update_one(
        {}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True,
    )
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompAMSSTalent {tag} to CompAMSSProj {tag}, "
                "Move her to shortlisted, Share the casting call with her, "
                f"Send her for CompAMSSProj {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Completed" in r.reply, r.reply
        assert "Share Casting Call" in r.reply, r.reply
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        assert "1 → Approve" in r.reply, r.reply

        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "shortlisted", (r.reply, doc)
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await db.submissions.delete_many({"id": submission_id})
        await db[_ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original)


async def test_4_compound_share_multiple_talents():
    """Test 4 — SHARE with multiple talents inside a compound command."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"CompMultiTalProj {tag}", shoot_dates="2 Aug 2028", budget="Rs 8,000/day",
    )
    t1 = await _seed_talent(f"CompMultiTalA {tag}", phone="917000900020")
    t2 = await _seed_talent(f"CompMultiTalB {tag}", phone="917000900021")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompMultiTalA {tag}, CompMultiTalB {tag} to CompMultiTalProj {tag}, "
                "Share the casting call with both and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Completed" in r.reply, r.reply
        assert "2 WhatsApp messages queued." in r.reply, r.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2]}}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
    finally:
        await _cleanup_jobs_for_talents([t1, t2])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_5_compound_share_multiple_projects_explicit_override():
    """Test 5 — SHARE with an EXPLICIT project override that differs from
    the ADD/MOVE project (Part 4: an explicit SHARE target always wins
    over inheritance)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    added_project_id = await _seed_project_with_details(
        f"CompAddedProj {tag}", shoot_dates="3 Sep 2028", budget="Rs 9,000/day",
    )
    other_project_id = await _seed_project_with_details(
        f"CompOtherProj {tag}", shoot_dates="4 Oct 2028", budget="Rs 10,000/day",
    )
    talent_id = await _seed_talent(f"CompOverrideTalent {tag}", phone="917000900030")
    await _seed_pipeline_row(other_project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompOverrideTalent {tag} to CompAddedProj {tag}, "
                f"Share the casting call for CompOtherProj {tag} with CompOverrideTalent {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Completed" in r.reply, r.reply
        assert f"CompOtherProj {tag}" in r.reply, r.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["source_id"] == other_project_id
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[added_project_id, other_project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_6_compound_share_ambiguous_project_asks_not_guesses():
    """Test 6 — an ambiguous SHARE project inside a compound plan is
    reported inline (✗ + the "which one?" question), exactly like an
    ambiguous ADD/MOVE step already is — never silently guessed, never a
    send to the wrong project."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    alpha_label = f"CompAmbig {tag} Alpha"
    beta_label = f"CompAmbig {tag} Beta"
    p1 = await _seed_project_with_details(alpha_label, shoot_dates="1 Nov 2028", budget="Rs 1/day")
    p2 = await _seed_project_with_details(beta_label, shoot_dates="2 Nov 2028", budget="Rs 2/day")
    talent_id = await _seed_talent(f"CompAmbigTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompAmbigTalent {tag} to {alpha_label}, "
                f"Share the casting call for CompAmbig {tag} with CompAmbigTalent {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Completed" in r.reply, r.reply
        assert "found multiple projects" in r.reply.lower(), r.reply

        jobs = await db.whatsapp_jobs.find({"talent_id": talent_id}).to_list(10)
        assert jobs == []
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=[talent_id])
        await _restore_config(original)


async def test_7_existing_add_move_send_template_blast_unchanged():
    """Test 7 — the PRE-EXISTING "Add,Move,Send - Talent - Casting Call -
    Project - Stage" structured compound (the WhatsApp-template-blast tail,
    unrelated to casting.send) must remain completely unaffected by the
    new ADD/MOVE/SHARE/SEND word-based chunking."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"AMSRegressProj {tag}", shoot_dates="5 Dec 2028", budget="Rs 5,000/day",
    )
    talent_id = await _seed_talent(f"AMSRegressTalent {tag}", phone="917000900040")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"add,move,send - AMSRegressTalent {tag} - Casting Call - AMSRegressProj {tag} - Follow Up and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "✗" not in r.reply, r.reply
        assert "1 WhatsApp message queued." in r.reply, r.reply

        doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
        assert doc is not None and doc["stage"] == "follow_up"
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_8_send_approval_gate_intact_inside_compound_command():
    """Test 8 — SEND's approval gate is genuinely preserved when SEND is
    part of a compound command: nothing is sent merely because the plan
    was approved/auto-confirmed, the SEND form is a real, LIVE
    confirmation (a subsequent "3" cancels it cleanly), and no WhatsApp
    job/media-send record is ever created without a SEPARATE, explicit
    approval of that form."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    email = f"gatecheck.{tag}@example.com"
    project_id = await _seed_send_project(f"CompGateProj {tag}", whatsapp_casting_group_name="Talentgram Casting Test")
    talent_id = await _seed_send_talent(
        f"CompGateTalent {tag}", whatsapp_group_name=f"CompGateTalent {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_send_submission(project_id, talent_id, email, decision="approved")
    await db[_ma.IDENTITY_COLLECTION].update_one(
        {}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True,
    )
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add CompGateTalent {tag} to CompGateProj {tag}, "
                f"Send her for CompGateProj {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        # Nothing has actually been sent/scanned yet — approving the PLAN
        # (via "and confirm") must never be mistaken for approving SEND.
        assert await db[_ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id}) is None

        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert cancel.handled, cancel.reply
        # Guided Context-Aware Responses (2026-08-28) — SEND gets its own
        # cancel wording ("SEND CANCELLED... The form approval was
        # discarded"), distinct from the generic CANCELLED_MESSAGE.
        assert "SEND CANCELLED" in cancel.reply, cancel.reply
        assert await db[_ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id}) is None
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await db.submissions.delete_many({"id": submission_id})
        await db[_ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Guided, Context-Aware Agent Responses (2026-08-28) — focused coverage for
# the new contextual EDIT prompts (Parts 1-5 of the test list) plus a
# dedicated CANCEL check. Missing-project/ambiguous-talent/ambiguous-project/
# invalid-command/success/partial-failure (Parts 6-12) are exercised by the
# existing regression suite above, unchanged in substance — only 3 field
# questions and the shared CANCELLED_MESSAGE gained wording improvements,
# both already covered by updated assertions in this same file.
# ---------------------------------------------------------------------------

async def test_guided_1_add_edit_response_is_contextual():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"GuidedAddProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"GuidedAddTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add GuidedAddTalent {tag} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to add" in r.reply, r.reply

        edit = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "EDITING ADD" in edit.reply, edit.reply
        assert f"GuidedAddTalent {tag}" in edit.reply, edit.reply
        assert label in edit.reply, edit.reply
        assert "Nothing will be executed until you confirm" in edit.reply, edit.reply
        # Must NOT be the old generic, domain-blind prompt.
        assert "Role = Casting Director" not in edit.reply, edit.reply

        # A fresh trigger re-opens a "confirming" card (a bare "3" only
        # cancels FROM "confirming" — see test_confirmation_edit_and_cancel
        # for that same, pre-existing state-machine behaviour, unchanged).
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add GuidedAddTalent {tag} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "CANCELLED" in cancel.reply, cancel.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_guided_2_move_edit_response_is_contextual():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(brand_name=f"GuidedMoveProj {tag}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"GuidedMoveTalent {tag}")
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move GuidedMoveTalent {tag} to Follow Up in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to move" in r.reply, r.reply

        edit = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "EDITING MOVE" in edit.reply, edit.reply
        assert f"GuidedMoveTalent {tag}" in edit.reply, edit.reply
        assert "Follow Up" in edit.reply, edit.reply
        assert "Nothing will be executed until you confirm" in edit.reply, edit.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move GuidedMoveTalent {tag} to Follow Up in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "CANCELLED" in cancel.reply, cancel.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_guided_3_share_edit_response_shows_resolved_template():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"GuidedShareProj {tag}", shoot_dates="1 Jan 2029", budget="Rs 1/day",
    )
    talent_id = await _seed_talent(f"GuidedShareTalent {tag}")
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting call for GuidedShareProj {tag} to GuidedShareTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "SHARE PREVIEW" in r.reply, r.reply

        edit = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "EDITING SHARE" in edit.reply, edit.reply
        assert f"GuidedShareTalent {tag}" in edit.reply, edit.reply
        # The RESOLVED template name, not just the "casting call" hint text.
        assert "Casting Call" in edit.reply, edit.reply
        assert "Nothing will be sent until you confirm" in edit.reply, edit.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share casting call for GuidedShareProj {tag} to GuidedShareTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "CANCELLED" in cancel.reply, cancel.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_guided_4_compound_plan_edit_response_lists_all_steps():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_details(
        f"GuidedPlanProj {tag}", shoot_dates="2 Feb 2029", budget="Rs 2/day",
    )
    talent_id = await _seed_talent(f"GuidedPlanTalent {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add GuidedPlanTalent {tag} to GuidedPlanProj {tag}, "
                "Move her to Shortlisted, Share the casting call with her"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "You are about to run this plan" in r.reply, r.reply

        edit = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "EDITING YOUR PLAN" in edit.reply, edit.reply
        assert "Current plan:" in edit.reply, edit.reply
        # All three steps described, not just the first.
        assert "Add" in edit.reply and f"GuidedPlanTalent {tag}" in edit.reply, edit.reply
        assert "Move" in edit.reply and "Shortlisted" in edit.reply, edit.reply
        assert "Share" in edit.reply, edit.reply
        assert "Nothing will execute until you confirm" in edit.reply, edit.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Add GuidedPlanTalent {tag} to GuidedPlanProj {tag}, "
                "Move her to Shortlisted, Share the casting call with her"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "CANCELLED" in cancel.reply, cancel.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_guided_5_send_edit_response_lists_form_fields_not_db_fields():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    email = f"guidededit.{tag}@example.com"
    project_id = await _seed_send_project(f"GuidedSendProj {tag}", whatsapp_casting_group_name="Talentgram Casting Test")
    talent_id = await _seed_send_talent(
        f"GuidedSendTalent {tag}", whatsapp_group_name=f"GuidedSendTalent {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_send_submission(project_id, talent_id, email, decision="approved")
    await db[_ma.IDENTITY_COLLECTION].update_one(
        {}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True,
    )
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - GuidedSendTalent {tag} - GuidedSendProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "SEND FORM PREVIEW" in r.reply, r.reply

        edit = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "EDITING SEND FORM" in edit.reply, edit.reply
        for field in ("Age", "Height", "Current Location", "Availability", "Budget"):
            assert field in edit.reply, edit.reply
        # Never the raw/internal DB fields Client View shows.
        for forbidden in ("Gender", "Ethnicity", "Followers", "Skills"):
            assert forbidden not in edit.reply, edit.reply
        assert "Nothing will be sent until you approve the updated form" in edit.reply, edit.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - GuidedSendTalent {tag} - GuidedSendProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "SEND CANCELLED" in cancel.reply, cancel.reply
        assert "form approval was discarded" in cancel.reply.lower(), cancel.reply
    finally:
        await _cleanup_jobs_for_talents([talent_id])
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await db.submissions.delete_many({"id": submission_id})
        await db[_ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original)
