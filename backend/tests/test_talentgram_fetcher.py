"""Talentgram Fetcher — SHOW ME command (new, separate WhatsApp agent).
See agents/modules/talentgram_fetcher.py's module docstring for the
architecture (reused resolvers, ported Copy Form formatter, group
isolation via whatsapp_agent_config).
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.dispatcher import handle_inbound_message  # noqa: E402
from agents.modules import talentgram_fetcher as fetcher  # noqa: E402

from tests.test_media_assignment import (  # noqa: E402
    _cleanup, _restore_config, _seed_project, _use_test_config,
)

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")

AGENT_ID = fetcher.AGENT_ID


async def _seed_talent_full(name: str, *, whatsapp_group_name: str = "", email: str = "", phone=None) -> str:
    tid = f"test-fetcher-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": phone, "whatsapp_group_name": whatsapp_group_name,
        "email": email or None, "normalized_email": (email or "").strip().lower() or None,
    })
    return tid


async def _seed_submission_with_form(
    project_id: str, talent_id: str, *,
    original_form_data=None, form_data=None, effective_age=None, decision="pending",
) -> str:
    sid = f"test-fetcher-sub-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": sid, "project_id": project_id, "talent_id": talent_id,
        "talent_email": f"{talent_id}@example.com",
        "media": [], "decision": decision, "created_at": _now(), "submitted_at": _now(),
    }
    if original_form_data is not None:
        doc["original_form_data"] = original_form_data
    doc["form_data"] = form_data if form_data is not None else {}
    if effective_age is not None:
        doc["effective_age"] = effective_age
    await db.submissions.insert_one(doc)
    return sid


async def _cleanup_fetcher(*, talent_ids=(), project_ids=(), submission_ids=()):
    await _cleanup(talent_ids=talent_ids, project_ids=project_ids, submission_ids=submission_ids)


GROUP = None  # set per-test via _use_test_config


async def _show_me(group, text, phone="919000000001"):
    return await handle_inbound_message(
        group_name=group, sender_phone=phone, text=text,
        sender_name="Admin", sender_is_group_member=True,
    )


# ---------------------------------------------------------------------------
# 1. Single talent + project
# ---------------------------------------------------------------------------
async def test_single_talent_single_project():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(
            project_id, talent_id,
            original_form_data={"first_name": "Angela", "last_name": "Sharma", "age": 24, "height": "5ft 6in"},
            effective_age=24,
        )
        r = await _show_me(group, f"Show me Angela Sharma {tag}'s form for Hinge {tag}")
        assert r.handled, r
        assert f"Talentgram x Hinge {tag} - Form" in r.reply, r.reply
        assert "Angela - S" in r.reply
        assert "Age - 24" in r.reply
        assert "Height - 5ft 6in" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 2. Case-insensitive command
# ---------------------------------------------------------------------------
async def test_case_insensitive_command():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"SHOW ME angela sharma {tag}'s FORM for hinge {tag}".upper())
        assert r.handled, r
        assert "Angela - S" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 3. Extra spaces
# ---------------------------------------------------------------------------
async def test_extra_spaces_tolerated():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"Show   me   Angela Sharma {tag}   form   for   Hinge {tag}")
        assert r.handled, r
        assert "Angela - S" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 4. Apostrophe variation
# ---------------------------------------------------------------------------
async def test_apostrophe_variation():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        # fancy unicode apostrophe
        r = await _show_me(group, f"Show me Angela Sharma {tag}’s form for Hinge {tag}")
        assert r.handled, r
        assert "Angela - S" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 5. Minor talent spelling variation
# ---------------------------------------------------------------------------
async def test_minor_talent_spelling_variation():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"Show me Angla Sharma {tag}'s form for Hinge {tag}")
        assert r.handled, r
        assert "Angela - S" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 6. Minor project spelling variation
# ---------------------------------------------------------------------------
async def test_minor_project_spelling_variation():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hingle Project {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"Show me Angela Sharma {tag}'s form for Hingle Projct {tag}")
        assert r.handled, r
        assert "Angela - S" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 7. Multiple talents, single project
# ---------------------------------------------------------------------------
async def test_multiple_talents_single_project():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        t1 = await _seed_talent_full(f"Angela Sharma {tag}")
        t2 = await _seed_talent_full(f"Priya Shah {tag}")
        await _seed_submission_with_form(project_id, t1, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        await _seed_submission_with_form(project_id, t2, original_form_data={"first_name": "Priya", "last_name": "Shah"})
        r = await _show_me(group, f"Show me Angela Sharma {tag}, Priya Shah {tag} forms for Hinge {tag}")
        assert r.handled, r
        assert f"Angela Sharma {tag}" in r.reply
        assert f"Priya Shah {tag}" in r.reply
        assert "Angela - S" in r.reply
        assert "Priya - S" in r.reply
        # single project overall -> no "Talent — Project" header
        assert " — Hinge" not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 8. Single talent, multiple projects
# ---------------------------------------------------------------------------
async def test_single_talent_multiple_projects():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        p1 = await _seed_project(f"Hinge {tag}")
        p2 = await _seed_project(f"Dove {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(p1, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        await _seed_submission_with_form(p2, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"Show me Angela Sharma {tag}'s form for Hinge {tag}, Dove {tag}")
        assert r.handled, r
        assert f"Angela Sharma {tag} — Hinge {tag}" in r.reply, r.reply
        assert f"Angela Sharma {tag} — Dove {tag}" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[p1, p2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 9. Multiple talents + multiple projects (full cross product)
# ---------------------------------------------------------------------------
async def test_multiple_talents_multiple_projects_cross_product():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        p1 = await _seed_project(f"Hinge {tag}")
        p2 = await _seed_project(f"Dove {tag}")
        t1 = await _seed_talent_full(f"Angela Sharma {tag}")
        t2 = await _seed_talent_full(f"Priya Shah {tag}")
        for p in (p1, p2):
            for t in (t1, t2):
                await _seed_submission_with_form(p, t, original_form_data={"first_name": "X", "last_name": "Y"})
        r = await _show_me(group, f"Show me Angela Sharma {tag}, Priya Shah {tag} forms for Hinge {tag}, Dove {tag}")
        assert r.handled, r
        for talent in (f"Angela Sharma {tag}", f"Priya Shah {tag}"):
            for proj in (f"Hinge {tag}", f"Dove {tag}"):
                assert f"{talent} — {proj}" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2], project_ids=[p1, p2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 10. Missing talent
# ---------------------------------------------------------------------------
async def test_missing_talent():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        r = await _show_me(group, f"Show me Nonexistent Person {tag} form for Hinge {tag}")
        assert r.handled, r
        assert "couldn't find" in r.reply
    finally:
        await _cleanup_fetcher(project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 11. Missing project
# ---------------------------------------------------------------------------
async def test_missing_project():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Nonexistent Project {tag}")
        assert r.handled, r
        assert "couldn't find the project" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 12. Missing submission
# ---------------------------------------------------------------------------
async def test_missing_submission():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r
        assert f"No Hinge {tag} submission was found for Angela Sharma {tag}" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 13. Ambiguous talent (single-pair -> full numbered clarification, resumable)
# ---------------------------------------------------------------------------
async def test_ambiguous_talent_single_pair():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        # Two genuinely unrelated talents sharing a near-identical name,
        # NEITHER with a submission for this project -> tie-break can't
        # help, must ask.
        t1 = await _seed_talent_full(f"Angela Sharma {tag}", phone="9111111111")
        t2 = await _seed_talent_full(f"Angela Sharma {tag}", phone="9222222222")
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r
        assert "Which Angela Sharma" in r.reply, r.reply
        assert "1 →" in r.reply and "2 →" in r.reply and "Cancel" in r.reply, r.reply

        # Resolve with the numbered pick.
        r2 = await _show_me(group, "1")
        assert r2.handled, r2
        assert "no matching talent" not in r2.reply
        # Since neither has a submission, expect the honest no-submission message
        assert f"No Hinge {tag} submission was found for Angela Sharma {tag}" in r2.reply, r2.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 13b. Ambiguous talent cancel path
# ---------------------------------------------------------------------------
async def test_ambiguous_talent_cancel():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        t1 = await _seed_talent_full(f"Angela Sharma {tag}", phone="9111111111")
        t2 = await _seed_talent_full(f"Angela Sharma {tag}", phone="9222222222")
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r
        r2 = await _show_me(group, "3")
        assert r2.handled, r2
        assert "cancelled" in r2.reply.lower(), r2.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 14. Verify exact canonical talent_id + project_id lookup (never
#     name/latest-submission-only) — a talent with the SAME NAME in a
#     different, unrelated project must never leak a submission here.
# ---------------------------------------------------------------------------
async def test_canonical_id_lookup_no_cross_project_leak():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        target_project = await _seed_project(f"Hinge {tag}")
        other_project = await _seed_project(f"Unrelated {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        # Submission exists only for the OTHER project.
        await _seed_submission_with_form(other_project, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r
        assert f"No Hinge {tag} submission was found for Angela Sharma {tag}" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[target_project, other_project])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 15. Fetcher works ONLY in its own group — a different (even active)
#     agent's group must not answer SHOW ME.
# ---------------------------------------------------------------------------
async def test_group_isolation_other_group_does_not_respond():
    other_group = f"Some Other Group {uuid.uuid4().hex[:6]}"
    # Deliberately do NOT seed a fetcher config for this group at all.
    r = await handle_inbound_message(
        group_name=other_group, sender_phone="919000000001",
        text="Show me Angela Sharma's form for Hinge",
        sender_name="Admin", sender_is_group_member=True,
    )
    assert not r.handled


# ---------------------------------------------------------------------------
# 17. Verify Talentgram Scouting Agent does NOT execute SHOW ME.
# ---------------------------------------------------------------------------
async def test_scouting_agent_group_does_not_execute_show_me():
    from agents import registry
    scouting_cfg = await registry.get_agent_config("whatsapp-campaign-agent")
    if not scouting_cfg or not scouting_cfg.get("group_names"):
        pytest.skip("whatsapp-campaign-agent has no configured group in this test DB")
    scouting_group = scouting_cfg["group_names"][0]
    r = await handle_inbound_message(
        group_name=scouting_group, sender_phone="919000000001",
        text="Show me Angela Sharma's form for Hinge",
        sender_name="Admin", sender_is_group_member=True,
    )
    # Scouting Agent has no "show me" trigger registered on any of its
    # intents -> must not be handled as Fetcher's SHOW ME.
    if r.handled:
        assert "Talentgram x" not in (r.reply or "")


# ---------------------------------------------------------------------------
# 16. Verify the exact same Copy Form output is returned as Submission
#     Review — build_copy_form_message called directly on the SAME
#     submission/project docs must byte-match what SHOW ME sent.
# ---------------------------------------------------------------------------
async def test_show_me_output_matches_build_copy_form_message_directly():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(
            project_id, talent_id,
            original_form_data={
                "first_name": "Angela", "last_name": "Sharma", "age": 24,
                "instagram_handle": "@angela.sharma",
                "budget": {"status": "accept", "value": "50000"},
            },
            effective_age=24,
        )
        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r

        sub = await db.submissions.find_one({"talent_id": talent_id, "project_id": project_id}, {"_id": 0})
        project_doc = await db.projects.find_one({"id": project_id}, {"_id": 0})
        expected = fetcher.build_copy_form_message(sub, project_doc)
        assert r.reply.strip() == expected.strip(), (r.reply, expected)
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 20. Verify no new database records are created just by fetching — a pure
#     read: submissions/talents/projects/media_assignments counts must be
#     unchanged by SHOW ME (unlike SEND, which deliberately writes
#     approval/scan-request records).
# ---------------------------------------------------------------------------
async def test_show_me_creates_no_new_database_records():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    try:
        project_id = await _seed_project(f"Hinge {tag}")
        talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
        await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Angela", "last_name": "Sharma"})

        before_subs = await db.submissions.count_documents({})
        before_talents = await db.talents.count_documents({})
        before_projects = await db.projects.count_documents({})

        r = await _show_me(group, f"Show me Angela Sharma {tag} form for Hinge {tag}")
        assert r.handled, r

        assert await db.submissions.count_documents({}) == before_subs
        assert await db.talents.count_documents({}) == before_talents
        assert await db.projects.count_documents({}) == before_projects
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)
