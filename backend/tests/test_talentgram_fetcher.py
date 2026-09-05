"""Talentgram Fetcher — SHOW ME command (new, separate WhatsApp agent).
See agents/modules/talentgram_fetcher.py's module docstring for the
architecture (reused resolvers, ported Copy Form formatter, group
isolation via whatsapp_agent_config).
"""
import os
import sys
import uuid
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now, parse_height_to_inches, compute_age  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.dispatcher import handle_inbound_message  # noqa: E402
from agents.modules import talentgram_fetcher as fetcher  # noqa: E402

from tests.test_media_assignment import (  # noqa: E402
    _cleanup, _restore_config, _seed_project, _use_test_config,
)

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")

AGENT_ID = fetcher.AGENT_ID


def _dob_for_age(age: int) -> str:
    today = date.today()
    try:
        return today.replace(year=today.year - age).isoformat()
    except ValueError:
        # Feb 29 birthdays on a non-leap target year.
        return today.replace(year=today.year - age, day=28).isoformat()


async def _seed_talent_full(
    name: str, *, whatsapp_group_name: str = "", email: str = "", phone=None,
    age=None, height=None, location=None, gender=None, instagram_handle=None,
    work_links=None, skills=None,
) -> str:
    tid = f"test-fetcher-tal-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": phone, "whatsapp_group_name": whatsapp_group_name,
        "email": email or None, "normalized_email": (email or "").strip().lower() or None,
        "location": location or [],
        "gender": gender,
        "instagram_handle": instagram_handle,
        "work_links": work_links or [],
        "skills": skills or [],
    }
    if age is not None:
        doc["dob"] = _dob_for_age(age)
        # Real talent docs always have `age` stored alongside `dob`
        # (routers/talents.py computes it at write time) — _filter_talent_
        # for_client reads talent["age"] directly, it does not derive it
        # from dob itself, so a test seeder that only sets dob would
        # silently produce a profile with no Age line at all.
        doc["age"] = compute_age(doc["dob"])
    if height is not None:
        doc["height"] = height
        doc["height_inches"] = parse_height_to_inches(height)
    await db.talents.insert_one(doc)
    return tid


async def _seed_link(talent_id: str, *, is_public: bool = True, title: str = None, created_at: str = None) -> str:
    lid = f"test-fetcher-link-{uuid.uuid4().hex[:8]}"
    slug = f"test-slug-{uuid.uuid4().hex[:8]}"
    await db.links.insert_one({
        "id": lid, "slug": slug, "title": title or f"Test link {slug}", "brand_name": None,
        "talent_ids": [talent_id], "submission_ids": [],
        "is_public": is_public, "password": None, "notes": None,
        "created_at": created_at or _now(), "created_by": "test",
    })
    return slug


async def _cleanup_links(*, talent_ids=()):
    await db.links.delete_many({"talent_ids": {"$in": list(talent_ids)}})


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


async def _seed_project_with_requirements(brand_name: str, *, custom_questions=None, competitive_brand_required: bool = True):
    pid = await _seed_project(brand_name)
    update = {}
    if custom_questions is not None:
        update["custom_questions"] = custom_questions
    if competitive_brand_required:
        update["submission_requirements"] = {"fields": {"competitive_brand": "required"}}
    if update:
        await db.projects.update_one({"id": pid}, {"$set": update})
    return pid


# ---------------------------------------------------------------------------
# Issue 3 regression — Competitive Brand and admin-defined project
# questions must never be silently dropped, including when blank.
# ---------------------------------------------------------------------------
async def test_form_includes_competitive_brand_when_answered():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_requirements(f"Mivi Phones {tag}")
    talent_id = await _seed_talent_full(f"Ameya Saawant {tag}")
    await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Ameya", "competitive_brand": "Samsung"})
    try:
        r = await _show_me(group, f"Show me Ameya Saawant {tag}'s form for Mivi Phones {tag}")
        assert r.handled, r
        assert "Competitive Brand - Samsung" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_includes_competitive_brand_blank_when_project_requires_it():
    # Real bug reproduction: Ameya Saawant / Mivi Phones — the project's
    # own submission_requirements.fields includes competitive_brand, but
    # her actual answer is "" — the field must still appear.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_requirements(f"Mivi Phones {tag}")
    talent_id = await _seed_talent_full(f"Ameya Saawant {tag}")
    await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Ameya", "competitive_brand": ""})
    try:
        r = await _show_me(group, f"Show me Ameya Saawant {tag}'s form for Mivi Phones {tag}")
        assert r.handled, r
        assert "Competitive Brand - [blank]" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_omits_competitive_brand_when_project_never_asks_for_it():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_requirements(f"NoCompBrand Project {tag}", competitive_brand_required=False)
    talent_id = await _seed_talent_full(f"Fixture Person {tag}")
    await _seed_submission_with_form(project_id, talent_id, original_form_data={"first_name": "Fixture"})
    try:
        r = await _show_me(group, f"Show me Fixture Person {tag}'s form for NoCompBrand Project {tag}")
        assert r.handled, r
        assert "Talentgram x NoCompBrand" in r.reply, r.reply
        assert "Competitive Brand" not in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_includes_multiple_custom_questions_answered_and_blank():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    questions = [
        {"id": "q1", "question": "Have you worked with a competing brand?"},
        {"id": "q2", "question": "Are you comfortable with a 2-day shoot?"},
    ]
    project_id = await _seed_project_with_requirements(f"Custom Q Project {tag}", custom_questions=questions, competitive_brand_required=False)
    talent_id = await _seed_talent_full(f"Fixture Person {tag}")
    await _seed_submission_with_form(
        project_id, talent_id,
        original_form_data={"first_name": "Fixture", "custom_answers": {"q1": "Samsung"}},
    )
    try:
        r = await _show_me(group, f"Show me Fixture Person {tag}'s form for Custom Q Project {tag}")
        assert r.handled, r
        assert "Have you worked with a competing brand? - Samsung" in r.reply, r.reply
        # q2 has no answer at all -> must STILL appear, blank.
        assert "Are you comfortable with a 2-day shoot? - [blank]" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_custom_question_ordering_matches_project_definition():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    questions = [
        {"id": "q1", "question": f"First question {tag}"},
        {"id": "q2", "question": f"Second question {tag}"},
        {"id": "q3", "question": f"Third question {tag}"},
    ]
    project_id = await _seed_project_with_requirements(f"Order Project {tag}", custom_questions=questions, competitive_brand_required=False)
    talent_id = await _seed_talent_full(f"Fixture Person {tag}")
    await _seed_submission_with_form(
        project_id, talent_id,
        original_form_data={"first_name": "Fixture", "custom_answers": {"q1": "A", "q2": "B", "q3": "C"}},
    )
    try:
        r = await _show_me(group, f"Show me Fixture Person {tag}'s form for Order Project {tag}")
        assert r.handled, r
        i1 = r.reply.index(f"First question {tag}")
        i2 = r.reply.index(f"Second question {tag}")
        i3 = r.reply.index(f"Third question {tag}")
        assert i1 < i2 < i3, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_new_future_custom_question_appears_automatically():
    # A project defined AFTER this code was written must still work with
    # no code changes — custom_questions is read straight off the
    # project document, never hardcoded.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    questions = [{"id": "future_q", "question": f"Brand new future question {tag}?"}]
    project_id = await _seed_project_with_requirements(f"Future Project {tag}", custom_questions=questions, competitive_brand_required=False)
    talent_id = await _seed_talent_full(f"Fixture Person {tag}")
    await _seed_submission_with_form(
        project_id, talent_id,
        original_form_data={"first_name": "Fixture", "custom_answers": {"future_q": "Yes"}},
    )
    try:
        r = await _show_me(group, f"Show me Fixture Person {tag}'s form for Future Project {tag}")
        assert r.handled, r
        assert f"Brand new future question {tag}? - Yes" in r.reply, r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_form_existing_copy_form_fields_unchanged():
    # Regression: the ordinary answered fields (Age/Height/Location/etc.)
    # must render exactly as before these fixes.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project_with_requirements(f"Regress Project {tag}", competitive_brand_required=False)
    talent_id = await _seed_talent_full(f"Fixture Person {tag}")
    await _seed_submission_with_form(
        project_id, talent_id,
        original_form_data={"first_name": "Fixture", "last_name": "Example", "age": 25, "height": "5'8\""},
        effective_age=25,
    )
    try:
        r = await _show_me(group, f"Show me Fixture Person {tag}'s form for Regress Project {tag}")
        assert r.handled, r
        assert "Fixture - E" in r.reply
        assert "Age - 25" in r.reply
        assert "Height - 5'8\"" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ===========================================================================
# SHOW ME PROFILE
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Single talent with generated portfolio link.
# ---------------------------------------------------------------------------
async def test_profile_single_talent_with_link():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
    slug = await _seed_link(talent_id)
    try:
        r = await _show_me(group, f"Show me the profile of Angela Sharma {tag}")
        assert r.handled, r
        assert f"Talentgram X Angela Sharma {tag}" in r.reply
        assert "Click to view the portfolio:" in r.reply
        assert f"https://links.talentgramagency.com/l/{slug}" in r.reply
    finally:
        await _cleanup_links(talent_ids=[talent_id])
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 2. Single talent without generated portfolio link (written fallback).
# ---------------------------------------------------------------------------
async def test_profile_single_talent_without_link():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(
        f"Priya Shah {tag}", age=23, height="5'6\"",
        location=[{"city": "Mumbai", "country": "India"}],
        instagram_handle="priya.shah", work_links=["Reel || https://vimeo.com/priya"],
    )
    try:
        r = await _show_me(group, f"Show me the profile of Priya Shah {tag}")
        assert r.handled, r
        assert f"Talentgram X Priya Shah {tag}" in r.reply
        assert "Click to view the portfolio" not in r.reply
        assert "Age: 23" in r.reply
        assert "Height: 5'6\"" in r.reply
        assert "Location: Mumbai, India" in r.reply
        assert "Instagram: https://www.instagram.com/priya.shah/" in r.reply
        assert "Work Links:" in r.reply
        assert "Reel: https://vimeo.com/priya" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 3. Multiple talents, all with links.
# ---------------------------------------------------------------------------
async def test_profile_multiple_talents_with_links():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent_full(f"Angela Sharma {tag}")
    t2 = await _seed_talent_full(f"Priya Shah {tag}")
    s1 = await _seed_link(t1)
    s2 = await _seed_link(t2)
    try:
        r = await _show_me(group, f"Show me the profile of Angela Sharma {tag}, Priya Shah {tag}")
        assert r.handled, r
        assert s1 in r.reply and s2 in r.reply
        assert "---" in r.reply
    finally:
        await _cleanup_links(talent_ids=[t1, t2])
        await _cleanup_fetcher(talent_ids=[t1, t2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 4. Multiple talents mixed: some with links, some without.
# ---------------------------------------------------------------------------
async def test_profile_multiple_talents_mixed():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent_full(f"Angela Sharma {tag}")
    t2 = await _seed_talent_full(f"Riya Mehta {tag}", age=21, height="5'5\"")
    s1 = await _seed_link(t1)
    try:
        r = await _show_me(group, f"Show me the profiles of Angela Sharma {tag}, Riya Mehta {tag}")
        assert r.handled, r
        assert s1 in r.reply
        assert f"Talentgram X Riya Mehta {tag}" in r.reply
        assert "Age: 21" in r.reply
    finally:
        await _cleanup_links(talent_ids=[t1, t2])
        await _cleanup_fetcher(talent_ids=[t1, t2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 5. Minor talent spelling error.
# ---------------------------------------------------------------------------
async def test_profile_minor_spelling_error():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
    try:
        r = await _show_me(group, f"Show me the profile of Angla Sharma {tag}")
        assert r.handled, r
        assert f"Talentgram X Angela Sharma {tag}" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 6. Extra spaces.
# ---------------------------------------------------------------------------
async def test_profile_extra_spaces():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
    try:
        r = await _show_me(group, f"Show   me   the   profile   of   Angela Sharma {tag}")
        assert r.handled, r
        assert f"Talentgram X Angela Sharma {tag}" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 7. Case variation.
# ---------------------------------------------------------------------------
async def test_profile_case_variation():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(f"Angela Sharma {tag}")
    try:
        r = await _show_me(group, f"SHOW ME THE PROFILE OF angela sharma {tag}".upper())
        assert r.handled, r
        assert f"Talentgram X Angela Sharma {tag}" in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 8. Ambiguous talent (single-talent profile request -> resumable clarification).
# ---------------------------------------------------------------------------
async def test_profile_ambiguous_talent():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent_full(f"Priya Shah {tag}", phone="9111111111")
    t2 = await _seed_talent_full(f"Priya Shah {tag}", phone="9222222222")
    try:
        r = await _show_me(group, f"Show me the profile of Priya Shah {tag}")
        assert r.handled, r
        assert "Which Priya Shah" in r.reply
        assert "1 →" in r.reply and "Cancel" in r.reply

        r2 = await _show_me(group, "1")
        assert r2.handled, r2
        assert f"Talentgram X Priya Shah {tag}" in r2.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 9. Verify no internal/admin fields leak into fallback.
# ---------------------------------------------------------------------------
async def test_profile_fallback_leaks_no_internal_fields():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(f"Angela Sharma {tag}", age=24, height="5'6\"")
    # Budget is admin-only by default visibility — must never appear.
    await db.talents.update_one({"id": talent_id}, {"$set": {
        "budget": {"status": "accept", "value": "50000"},
        "notes": "internal admin note — never client facing",
        "competitive_brand": "SecretBrand",
    }})
    try:
        r = await _show_me(group, f"Show me the profile of Angela Sharma {tag}")
        assert r.handled, r
        assert talent_id not in r.reply
        assert "50000" not in r.reply
        assert "internal admin note" not in r.reply
        assert "SecretBrand" not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 10. Verify existing public profile fields are formatted correctly.
# ---------------------------------------------------------------------------
async def test_profile_fields_formatted_correctly():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    talent_id = await _seed_talent_full(
        f"Riya Mehta {tag}", age=22, height="5'7\"",
        location=[{"city": "Delhi", "country": "India"}],
        instagram_handle="@riya.mehta", skills=["Dancing", "Acting"],
    )
    try:
        r = await _show_me(group, f"Show me the profile of Riya Mehta {tag}")
        assert r.handled, r
        assert "Age: 22" in r.reply
        assert "Height: 5'7\"" in r.reply
        assert "Location: Delhi, India" in r.reply
        assert "Instagram: https://www.instagram.com/riya.mehta/" in r.reply
        # Skills has no DEFAULT_VISIBILITY entry (core.py) -> gated off by
        # default in the real public-profile whitelist this reuses; a
        # link-level visibility override could enable it, but the no-link
        # fallback has no such override to consult, so it correctly never
        # appears here.
        assert "Skills:" not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# Issue 1 regression — PROFILE must pick the individual single-talent link,
# never a multi-talent/project one, and among several valid single-talent
# links must prefer the one titled after the talent's OWN name over a
# brand-titled one (real bug: Angela Kumar's "Talentgram x Kay Beauty" link
# was picked over "Talentgram x Angela" purely because it was newer).
# ---------------------------------------------------------------------------
async def test_profile_prefers_individual_link_over_multi_talent_link():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    name = f"Angela Kumar {tag}"
    t1 = await _seed_talent_full(name)
    t2 = await _seed_talent_full(f"Other Talent {tag}")
    individual_slug = await _seed_link(t1, title=f"Talentgram x {name}")
    lid = f"test-fetcher-link-{uuid.uuid4().hex[:8]}"
    multi_slug = f"test-slug-{uuid.uuid4().hex[:8]}"
    # A multi-talent/project portfolio link created AFTER the individual
    # one (so "most recent" alone would wrongly prefer it).
    await db.links.insert_one({
        "id": lid, "slug": multi_slug, "title": "Talentgram x Collective", "brand_name": None,
        "talent_ids": [t1, t2], "submission_ids": [],
        "is_public": True, "password": None, "notes": None,
        "created_at": _now(), "created_by": "test",
    })
    try:
        r = await _show_me(group, f"Show me the profile of {name}")
        assert r.handled, r
        assert individual_slug in r.reply
        assert multi_slug not in r.reply
    finally:
        await _cleanup_links(talent_ids=[t1, t2])
        await _cleanup_fetcher(talent_ids=[t1, t2])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_profile_prefers_name_titled_link_over_newer_brand_titled_link():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    name = f"Angela Kumar {tag}"
    talent_id = await _seed_talent_full(name)
    # The name-titled link is created FIRST (older) — a naive "most
    # recent" tie-break would wrongly pick the brand-titled one below.
    individual_slug = await _seed_link(talent_id, title=f"Talentgram x {name}")
    brand_slug = await _seed_link(talent_id, title="Talentgram x Kay Beauty")
    try:
        r = await _show_me(group, f"Show me the profile of {name}")
        assert r.handled, r
        assert individual_slug in r.reply, r.reply
        assert brand_slug not in r.reply
    finally:
        await _cleanup_links(talent_ids=[talent_id])
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_profile_falls_back_to_most_recent_when_no_name_titled_link_exists():
    # When NONE of the talent's single-talent links are titled after their
    # own name (every one is a brand-specific share), the existing
    # "most recent wins" tie-break is the only signal available and stays
    # in place — never a hard failure just because no name-titled link
    # exists.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    name = f"Angela Kumar {tag}"
    talent_id = await _seed_talent_full(name)
    await _seed_link(talent_id, title="Talentgram x Nykaa")
    newer_slug = await _seed_link(talent_id, title="Talentgram x Kay Beauty")
    try:
        r = await _show_me(group, f"Show me the profile of {name}")
        assert r.handled, r
        assert newer_slug in r.reply
    finally:
        await _cleanup_links(talent_ids=[talent_id])
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_profile_lookup_has_no_project_influence():
    # PROFILE never takes a project into account at all — confirmed by
    # construction (the resolver signature has no project parameter), and
    # empirically here: a talent with submissions on several DIFFERENT
    # projects still resolves to the SAME individual link regardless.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    name = f"Angela Kumar {tag}"
    talent_id = await _seed_talent_full(name)
    p1 = await _seed_project(f"Project A {tag}")
    p2 = await _seed_project(f"Project B {tag}")
    await _seed_submission_with_form(p1, talent_id, original_form_data={"first_name": "Angela"})
    await _seed_submission_with_form(p2, talent_id, original_form_data={"first_name": "Angela"})
    individual_slug = await _seed_link(talent_id, title=f"Talentgram x {name}")
    try:
        r = await _show_me(group, f"Show me the profile of {name}")
        assert r.handled, r
        assert individual_slug in r.reply
    finally:
        await _cleanup_links(talent_ids=[talent_id])
        await _cleanup_fetcher(talent_ids=[talent_id], project_ids=[p1, p2])
        await _restore_config(original, agent_id=AGENT_ID)


def test_link_exact_talent_ids_condition_not_contains():
    # Verifies the query documents the exact condition — a regex sanity
    # check on the actual query construction in the source, so a future
    # edit that silently loosens "==" back to "in"/"$in" fails this test.
    import inspect
    src = inspect.getsource(fetcher._find_talent_portfolio_link)
    assert '"talent_ids": [talent_id]' in src
    assert "$in" not in src


# ===========================================================================
# SHOW ME FILTERED TALENTS
# ===========================================================================

async def _seed_filterable_talent(tag, **kwargs):
    kwargs.setdefault("location", [{"city": "Mumbai", "country": "India"}])
    return await _seed_talent_full(f"Filter Talent {tag} {uuid.uuid4().hex[:4]}", **kwargs)


# ---------------------------------------------------------------------------
# 11. Gender filter.
# ---------------------------------------------------------------------------
async def test_filter_gender_only():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_filterable_talent(tag, gender="female", age=22)
    t2 = await _seed_filterable_talent(tag, gender="male", age=22)
    try:
        r = await _show_me(group, "Show me all female talents")
        assert r.handled, r
        doc1 = await db.talents.find_one({"id": t1})
        doc2 = await db.talents.find_one({"id": t2})
        assert doc1["name"] in r.reply
        assert doc2["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t1, t2])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 12. Age range.
# ---------------------------------------------------------------------------
async def test_filter_age_range():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t_in = await _seed_filterable_talent(tag, age=20)
    t_out = await _seed_filterable_talent(tag, age=40)
    try:
        r = await _show_me(group, "Show me all talents between 18 and 25")
        assert r.handled, r
        doc_in = await db.talents.find_one({"id": t_in})
        doc_out = await db.talents.find_one({"id": t_out})
        assert doc_in["name"] in r.reply
        assert doc_out["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_in, t_out])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 13. Height range.
# ---------------------------------------------------------------------------
async def test_filter_height_range():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    # Height alone is too common a filter in the real shared dev DB to
    # isolate cleanly (hundreds of pre-existing talents can fall in any
    # given range, and the 20-result page cap can push a real match off
    # the rendered page) — combine with a unique, unrealistic city so the
    # result set is scoped to just these two seeded talents.
    unique_city = f"Zanzibaria{tag}"
    t_in = await _seed_talent_full(f"Filter Talent {tag} In", height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    t_out = await _seed_talent_full(f"Filter Talent {tag} Out", height="6'2\"", location=[{"city": unique_city, "country": "India"}])
    try:
        r = await _show_me(group, f"Show me all talents between 5'4 and 5'8 in {unique_city}")
        assert r.handled, r
        doc_in = await db.talents.find_one({"id": t_in})
        doc_out = await db.talents.find_one({"id": t_out})
        assert doc_in["name"] in r.reply
        assert doc_out["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_in, t_out])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 14. Location.
# ---------------------------------------------------------------------------
async def test_filter_location_only():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_in = await _seed_talent_full(f"Filter Talent {tag} A", location=[{"city": unique_city, "country": "India"}])
    t_out = await _seed_talent_full(f"Filter Talent {tag} B", location=[{"city": "Delhi", "country": "India"}])
    try:
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        doc_in = await db.talents.find_one({"id": t_in})
        doc_out = await db.talents.find_one({"id": t_out})
        assert doc_in["name"] in r.reply
        assert doc_out["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_in, t_out])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 15-18. Combined filters.
# ---------------------------------------------------------------------------
async def test_filter_gender_and_age():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t_match = await _seed_filterable_talent(tag, gender="female", age=22)
    t_wrong_gender = await _seed_filterable_talent(tag, gender="male", age=22)
    t_wrong_age = await _seed_filterable_talent(tag, gender="female", age=40)
    try:
        r = await _show_me(group, "Show me female talents between 18 and 25")
        assert r.handled, r
        names = {(await db.talents.find_one({"id": tid}))["name"] for tid in (t_match, t_wrong_gender, t_wrong_age)}
        match_name = (await db.talents.find_one({"id": t_match}))["name"]
        assert match_name in r.reply
        assert (await db.talents.find_one({"id": t_wrong_gender}))["name"] not in r.reply
        assert (await db.talents.find_one({"id": t_wrong_age}))["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match, t_wrong_gender, t_wrong_age])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_filter_gender_and_height():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    t_match = await _seed_filterable_talent(tag, gender="female", height="5'6\"")
    t_wrong = await _seed_filterable_talent(tag, gender="male", height="5'6\"")
    try:
        r = await _show_me(group, "Show me female talents height 5'4 to 5'8")
        assert r.handled, r
        assert (await db.talents.find_one({"id": t_match}))["name"] in r.reply
        assert (await db.talents.find_one({"id": t_wrong}))["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match, t_wrong])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_filter_gender_and_location():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", gender="female", location=[{"city": unique_city, "country": "India"}])
    t_wrong = await _seed_talent_full(f"Filter Talent {tag} B", gender="male", location=[{"city": unique_city, "country": "India"}])
    try:
        r = await _show_me(group, f"Show me female talents in {unique_city}")
        assert r.handled, r
        assert (await db.talents.find_one({"id": t_match}))["name"] in r.reply
        assert (await db.talents.find_one({"id": t_wrong}))["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match, t_wrong])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_filter_age_height_location():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", age=22, height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    t_wrong_age = await _seed_talent_full(f"Filter Talent {tag} B", age=40, height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    try:
        r = await _show_me(group, f"Show me talents age 18 to 25, height 5'4 to 5'8, {unique_city}")
        assert r.handled, r
        assert (await db.talents.find_one({"id": t_match}))["name"] in r.reply
        assert (await db.talents.find_one({"id": t_wrong_age}))["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match, t_wrong_age])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 19. All four filters together.
# ---------------------------------------------------------------------------
async def test_filter_all_four_together():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", gender="female", age=22, height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    t_wrong = await _seed_talent_full(f"Filter Talent {tag} B", gender="male", age=22, height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    try:
        r = await _show_me(group, f"Show me all female talents between 18 and 25, height 5'4 to 5'8, {unique_city}")
        assert r.handled, r
        assert (await db.talents.find_one({"id": t_match}))["name"] in r.reply
        assert (await db.talents.find_one({"id": t_wrong}))["name"] not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match, t_wrong])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 20. Criteria supplied in different orders -> identical results.
# ---------------------------------------------------------------------------
async def test_filter_order_independence():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", gender="female", age=22, height="5'6\"", location=[{"city": unique_city, "country": "India"}])
    try:
        r1 = await _show_me(group, f"Show me all female talents between 18 and 25, height 5'4 to 5'8, {unique_city}")
        r2 = await _show_me(group, f"Show me talents height 5'4 to 5'8, female, {unique_city}, age 18 to 25")
        assert r1.handled and r2.handled
        assert r1.reply == r2.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 21. Minor spelling error in location.
# ---------------------------------------------------------------------------
async def test_filter_location_minor_spelling_error():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", location=[{"city": unique_city, "country": "India"}])
    typo_city = unique_city[:-1] + "e" + unique_city[-1]  # one inserted char
    try:
        r = await _show_me(group, f"Show me all talents in {typo_city}")
        assert r.handled, r
        assert (await db.talents.find_one({"id": t_match}))["name"] in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[t_match])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 22. Zero matches.
# ---------------------------------------------------------------------------
async def test_filter_zero_matches():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    try:
        r = await _show_me(group, "Show me all female talents between 5 and 6, height 7'0 to 7'2, Nonexistentcityxyz")
        assert r.handled, r
        assert "No talents matched these criteria." in r.reply
        assert "broadening" in r.reply
    finally:
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 23. Multiple matching talents -> result count stated.
# ---------------------------------------------------------------------------
async def test_filter_multiple_matches_count_stated():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    ids = [
        await _seed_talent_full(f"Filter Talent {tag} {i}", gender="female", location=[{"city": unique_city, "country": "India"}])
        for i in range(3)
    ]
    try:
        r = await _show_me(group, f"Show me all female talents in {unique_city}")
        assert r.handled, r
        assert "Found 3 talents matching your criteria." in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=ids)
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 24. Verify returned records are canonical talent records (real DB ids).
# ---------------------------------------------------------------------------
async def test_filter_returns_canonical_talent_records():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", location=[{"city": unique_city, "country": "India"}])
    try:
        query = fetcher._build_talent_query(
            q=None, status=None, gender=None, ethnicity=None, location=[unique_city],
            age_min=None, age_max=None, height_min=None, height_max=None,
            followers_min=None, interested_in=[], interested_in_mode="any",
            skills=[], skills_mode="any", tags=[], tags_mode="any",
        )
        docs = await db.talents.find(query, {"_id": 0, "id": 1}).to_list(20)
        ids = {d["id"] for d in docs}
        assert t_match in ids
    finally:
        await _cleanup_fetcher(talent_ids=[t_match])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 25. Portfolio link resolution uses the existing generated-link system.
# ---------------------------------------------------------------------------
async def test_filter_result_never_includes_portfolio_link():
    # Production fix (Issue 2): filter results are ALWAYS the concise
    # Name/Age/Height/Location/Instagram card, even when the talent has a
    # generated portfolio link — a portfolio link is only ever shown for
    # an explicit SHOW ME PROFILE request, never for filtered talent
    # options.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", location=[{"city": unique_city, "country": "India"}])
    await _seed_link(t_match)
    try:
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        assert "https://links.talentgramagency.com" not in r.reply
        assert "Click to view the portfolio" not in r.reply
    finally:
        await _cleanup_links(talent_ids=[t_match])
        await _cleanup_fetcher(talent_ids=[t_match])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# 26. Verify no new profile links are created by Fetcher.
# ---------------------------------------------------------------------------
async def test_filter_creates_no_new_links():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    t_match = await _seed_talent_full(f"Filter Talent {tag} A", location=[{"city": unique_city, "country": "India"}])
    try:
        before = await db.links.count_documents({})
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        after = await db.links.count_documents({})
        assert after == before
    finally:
        await _cleanup_fetcher(talent_ids=[t_match])
        await _restore_config(original, agent_id=AGENT_ID)


# ---------------------------------------------------------------------------
# Issue 2 regression — filter output must be concise (Name/Age/Height/
# Location/Instagram ONLY, no Work Links/Skills/portfolio link) and must
# always be ONE message, never an artificial 15/20-result page.
# ---------------------------------------------------------------------------
async def test_filter_output_contains_only_required_fields():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    talent_id = await _seed_talent_full(
        f"Filter Talent {tag}", age=24, height="5'5\"",
        location=[{"city": unique_city, "country": "India"}],
        instagram_handle="filter.talent", work_links=["Reel || https://vimeo.com/x"],
        skills=["Dancing"],
    )
    try:
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        assert "Age: 24" in r.reply
        assert "Height: 5'5\"" in r.reply
        assert f"Location: {unique_city}, India" in r.reply
        assert "Instagram: https://www.instagram.com/filter.talent/" in r.reply
        assert "Work Links" not in r.reply
        assert "Skills" not in r.reply
        assert "Click to view the portfolio" not in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=[talent_id])
        await _restore_config(original, agent_id=AGENT_ID)


async def test_filter_result_always_single_message_not_capped_at_arbitrary_page_size():
    # Real bug: the previous implementation capped at 20 results with a
    # "showing N of M" footer even when everything would have fit in one
    # WhatsApp message easily. 30 seeded talents (well under the real
    # ~65K-char WhatsApp limit even with full cards) must ALL appear in
    # one response, uncapped.
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    ids = [
        await _seed_talent_full(f"Filter Talent {tag} {i:02d}", location=[{"city": unique_city, "country": "India"}])
        for i in range(30)
    ]
    try:
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        assert "Found 30 talents matching your criteria." in r.reply
        assert "Showing" not in r.reply  # no "showing N of M" partial-page footer
        for tid in ids:
            doc = await db.talents.find_one({"id": tid})
            assert doc["name"] in r.reply
    finally:
        await _cleanup_fetcher(talent_ids=ids)
        await _restore_config(original, agent_id=AGENT_ID)


async def test_filter_too_large_result_set_asks_to_narrow_without_partial_list():
    group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id=AGENT_ID)
    tag = uuid.uuid4().hex[:6]
    unique_city = f"Zanzibaria{tag}"
    ids = [
        await _seed_talent_full(f"Filter Talent {tag} {i:04d}", location=[{"city": unique_city, "country": "India"}])
        for i in range(50)
    ]
    original_limit = fetcher._WHATSAPP_SAFE_MESSAGE_CHAR_LIMIT
    fetcher._WHATSAPP_SAFE_MESSAGE_CHAR_LIMIT = 500  # force the too-large branch deterministically
    try:
        r = await _show_me(group, f"Show me all talents in {unique_city}")
        assert r.handled, r
        assert "Found 50 talents." in r.reply
        assert "narrow your criteria" in r.reply.lower()
        # Never a partial list mixed into the same message.
        doc0 = await db.talents.find_one({"id": ids[0]})
        assert doc0["name"] not in r.reply
    finally:
        fetcher._WHATSAPP_SAFE_MESSAGE_CHAR_LIMIT = original_limit
        await _cleanup_fetcher(talent_ids=ids)
        await _restore_config(original, agent_id=AGENT_ID)


# ===========================================================================
# REGRESSION
# ===========================================================================

# 27 (existing SHOW ME FORM tests) and 28 (Scouting Agent tests) are covered
# by re-running test_talentgram_fetcher.py's own FORM tests (unchanged
# above) and test_casting_agent.py / test_whatsapp_campaign_agent.py
# standalone — see the deployment report, not duplicated here.

# ---------------------------------------------------------------------------
# 29. SHOW ME PROFILE sent in Scouting Agent does not execute.
# ---------------------------------------------------------------------------
async def test_profile_not_executed_in_scouting_agent():
    from agents import registry
    scouting_cfg = await registry.get_agent_config("whatsapp-campaign-agent")
    if not scouting_cfg or not scouting_cfg.get("group_names"):
        pytest.skip("whatsapp-campaign-agent has no configured group in this test DB")
    scouting_group = scouting_cfg["group_names"][0]
    r = await handle_inbound_message(
        group_name=scouting_group, sender_phone="919000000001",
        text="Show me the profile of Angela Sharma",
        sender_name="Admin", sender_is_group_member=True,
    )
    if r.handled:
        assert "Talentgram X" not in (r.reply or "")


# ---------------------------------------------------------------------------
# 30. Filtered SHOW ME sent in Scouting Agent does not execute.
# ---------------------------------------------------------------------------
async def test_filtered_not_executed_in_scouting_agent():
    from agents import registry
    scouting_cfg = await registry.get_agent_config("whatsapp-campaign-agent")
    if not scouting_cfg or not scouting_cfg.get("group_names"):
        pytest.skip("whatsapp-campaign-agent has no configured group in this test DB")
    scouting_group = scouting_cfg["group_names"][0]
    r = await handle_inbound_message(
        group_name=scouting_group, sender_phone="919000000001",
        text="Show me all female talents between 18 and 25 in Mumbai",
        sender_name="Admin", sender_is_group_member=True,
    )
    if r.handled:
        assert "Found" not in (r.reply or "") or "matching your criteria" not in (r.reply or "")


# ---------------------------------------------------------------------------
# 31. Fetcher commands execute only in Talentgram Fetcher Agent (both new
#     commands, unregistered group).
# ---------------------------------------------------------------------------
async def test_profile_and_filter_do_not_execute_in_unregistered_group():
    other_group = f"Some Other Group {uuid.uuid4().hex[:6]}"
    r1 = await handle_inbound_message(
        group_name=other_group, sender_phone="919000000001",
        text="Show me the profile of Angela Sharma",
        sender_name="Admin", sender_is_group_member=True,
    )
    assert not r1.handled
    r2 = await handle_inbound_message(
        group_name=other_group, sender_phone="919000000001",
        text="Show me all female talents between 18 and 25 in Mumbai",
        sender_name="Admin", sender_is_group_member=True,
    )
    assert not r2.handled


# 32 (agent's own outgoing messages not reprocessed) is enforced entirely
# by the shared transport (whatsapp-worker/inbound.py's sender.
# _is_outgoing_msg filter), unrelated to anything added in this module —
# see test_talentgram_fetcher.py's existing note on this point for SHOW ME
# FORM; nothing new to test here.
