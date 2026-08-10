"""Conversational Talent Search (Phase 1, 2026-08-10) — roster search by
gender/category/city/age/height inside the "Talentgram Casting Pipeline"
WhatsApp agent's read-only casting.query intent. Scoped tests for:
  - Pure NLU parsing (casting_pipeline_nlu.extract_talent_search_filters /
    extract_talent_search_refinement / extract_talent_search_pagination /
    classify_query's new talent_search / talent_search_page kinds).
  - End-to-end flows via handle_inbound_message: fresh search, the spec's
    own multi-turn refinement example, pagination (next/previous/all),
    the "never guess" vague-term and unsupported-criteria clarification
    loops, and zero-result / regression-safety cases.

Every other casting.query flavour (project list/detail, pipeline listing,
talent-centric project/stage questions, move/add/undo) is unaffected and
covered by test_casting_agent.py; this file only covers what's new.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import random
import re
import string
import uuid
from datetime import date

import pytest

from core import db, _now, parse_height_to_inches
from agents import modules as agent_modules
from agents import registry, session_context
from agents.dispatcher import handle_inbound_message
from agents.modules import casting_pipeline_nlu as nlu

agent_modules.register_all()

AGENT_ID = "casting-agent"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _phone() -> str:
    return "91" + str(uuid.uuid4().int)[:9]


async def _use_test_config(group_name: str):
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [group_name],
        "allowed_senders": [],
        "security_mode": "group_members",
        "active": True,
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, doc, upsert=True)
    return original


async def _restore_config(original):
    if original is None:
        await db[registry.CONFIG_COLLECTION].delete_one({"agent_id": AGENT_ID})
    else:
        original.pop("_id", None)
        await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, original, upsert=True)


def _unique_city() -> str:
    """A fabricated, globally-unique single-word city name — this is a
    shared dev DB with plenty of real "Mumbai"/"Delhi" talents already in
    it, so any test asserting an EXACT result set/count must scope its
    query to a location no pre-existing document can possibly have."""
    return ("zq" + "".join(random.choices(string.ascii_lowercase, k=8))).title()


def _dob_for_age(age: int) -> str:
    today = date.today()
    return today.replace(year=today.year - age).isoformat()


async def _seed_talent_full(
    name: str, *, gender=None, city=None, age=None, height=None,
    interested_in=None, instagram_handle=None,
) -> str:
    tid = f"test-ts-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "gender": gender,
        "location": [{"city": city, "country": "India"}] if city else [],
        "dob": _dob_for_age(age) if age is not None else None,
        "height": height,
        "height_inches": parse_height_to_inches(height) if height else None,
        "interested_in": interested_in or [],
        "instagram_handle": instagram_handle,
    })
    return tid


async def _seed_project(status: str = "ongoing", brand_name: str = None) -> str:
    pid = f"test-ts-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid,
        "brand_name": brand_name or f"Test Project {pid[-6:]}",
        "status": status,
        "slug": pid,
        "materials": [],
        "created_at": _now(),
    })
    return pid


async def _cleanup(phone: str, talent_ids=(), project_ids=()) -> None:
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_tasks.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})
    await db.whatsapp_agent_undo.delete_many({"agent_id": AGENT_ID, "phone": phone})


# ---------------------------------------------------------------------------
# Pure NLU parsing — no DB, no dispatcher.
# ---------------------------------------------------------------------------
async def test_extract_filters_gender_category_location():
    parsed = nlu.extract_talent_search_filters("Show female models from Mumbai")
    assert parsed["filters"] == {"gender": "female", "interested_in": ["Modeling"], "location": ["Mumbai"]}
    assert parsed["vague_terms"] == []
    assert parsed["unsupported"] == []


async def test_extract_filters_actors_age_between():
    parsed = nlu.extract_talent_search_filters("Show actors between 18 and 22")
    assert parsed["filters"] == {"interested_in": ["Acting"], "age_min": 18, "age_max": 22}


async def test_extract_filters_male_models_above_height():
    parsed = nlu.extract_talent_search_filters('Show male models above 5\'10"')
    assert parsed["filters"]["gender"] == "male"
    assert parsed["filters"]["interested_in"] == ["Modeling"]
    assert parsed["filters"]["height_min"] == pytest.approx(70.0)
    assert "height_max" not in parsed["filters"]


async def test_extract_filters_location_only():
    parsed = nlu.extract_talent_search_filters("Show talents from Delhi")
    assert parsed["filters"] == {"location": ["Delhi"]}


async def test_extract_filters_female_influencers():
    parsed = nlu.extract_talent_search_filters("Show female influencers")
    assert parsed["filters"] == {"gender": "female", "interested_in": ["Influencer Campaigns"]}


async def test_extract_filters_language_is_unsupported_not_guessed():
    parsed = nlu.extract_talent_search_filters("Show talents who speak Gujarati")
    assert "language" in parsed["unsupported"]
    # Never silently folded into filters as a location/category/etc.
    assert "location" not in parsed["filters"]


async def test_extract_filters_combined_availability_and_age_range():
    parsed = nlu.extract_talent_search_filters(
        "Show available female actors from Mumbai between 18 and 24"
    )
    assert "availability" in parsed["unsupported"]
    assert parsed["filters"]["gender"] == "female"
    assert parsed["filters"]["interested_in"] == ["Acting"]
    assert parsed["filters"]["location"] == ["Mumbai"]
    assert parsed["filters"]["age_min"] == 18
    assert parsed["filters"]["age_max"] == 24
    # No vague-term false positive just because "available" also matched.
    assert parsed["vague_terms"] == []


async def test_extract_filters_vague_height_no_number():
    parsed = nlu.extract_talent_search_filters("Show tall girls")
    assert "tall" in parsed["vague_terms"]
    assert "height_min" not in parsed["filters"]
    assert "height_max" not in parsed["filters"]
    assert parsed["filters"]["gender"] == "female"


async def test_extract_filters_vague_age_no_number():
    parsed = nlu.extract_talent_search_filters("Show young actors")
    assert "young" in parsed["vague_terms"]
    assert "age_min" not in parsed["filters"]
    assert "age_max" not in parsed["filters"]


async def test_extract_filters_absence_of_criteria_is_not_vague():
    # "Show female models from Mumbai" specifies no age/height at all —
    # per spec, that's simply unspecified, not ambiguous. Must NOT trigger
    # a clarification.
    parsed = nlu.extract_talent_search_filters("Show female models from Mumbai")
    assert parsed["vague_terms"] == []


async def test_extract_filters_explicit_relative_age_is_not_vague():
    # "older than 25" has a resolvable number — must be treated as a real
    # filter, not routed into the vague-term clarification path.
    parsed = nlu.extract_talent_search_filters("Show actors older than 25")
    assert parsed["filters"]["age_min"] == 25
    assert parsed["vague_terms"] == []


async def test_extract_refinement_only_location():
    assert nlu.extract_talent_search_refinement("Only Mumbai") == {"location": ["Mumbai"]}


async def test_extract_refinement_above_height():
    result = nlu.extract_talent_search_refinement('Above 5\'7"')
    assert result["height_min"] == pytest.approx(67.0)


async def test_extract_refinement_age_under():
    assert nlu.extract_talent_search_refinement("Age under 22") == {"age_max": 22}


async def test_extract_refinement_rejects_unrelated_chatter():
    assert nlu.extract_talent_search_refinement("ok thanks") is None
    assert nlu.extract_talent_search_refinement("Mumbai shoot confirmed for tomorrow") is None


async def test_extract_pagination_variants():
    assert nlu.extract_talent_search_pagination("Show next 20") == {"action": "next"}
    assert nlu.extract_talent_search_pagination("next") == {"action": "next"}
    assert nlu.extract_talent_search_pagination("Previous 20") == {"action": "previous"}
    assert nlu.extract_talent_search_pagination("prev") == {"action": "previous"}
    assert nlu.extract_talent_search_pagination("Show all") == {"action": "all"}
    assert nlu.extract_talent_search_pagination("all") == {"action": "all"}


async def test_extract_pagination_never_fires_mid_sentence():
    assert nlu.extract_talent_search_pagination("all good, see you then") is None
    assert nlu.extract_talent_search_pagination("let's do this next week") is None


async def test_classify_query_talent_search_kind():
    classification = nlu.classify_query("Show female models from Mumbai", [])
    assert classification.kind == "talent_search"
    assert classification.search_filters["gender"] == "female"


async def test_classify_query_pagination_kind():
    classification = nlu.classify_query("Show next 20", [])
    assert classification.kind == "talent_search_page"
    assert classification.search_page_action == "next"


async def test_classify_query_unrelated_text_still_unrecognized():
    classification = nlu.classify_query("Show me the weather", [])
    assert classification.kind == "unrecognized"


# ---------------------------------------------------------------------------
# End-to-end via handle_inbound_message
# ---------------------------------------------------------------------------
async def test_talent_search_basic_and_zero_results():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        t1 = await _seed_talent_full(
            f"Zz Ahana {uuid.uuid4().hex[:6]}", gender="female", city=city, age=22,
            height="5'7\"", interested_in=["Modeling"], instagram_handle="ahanapocha",
        )
        t2 = await _seed_talent_full(
            f"Zz Sneha {uuid.uuid4().hex[:6]}", gender="female", city=city, age=20,
            height="5'6\"", interested_in=["Modeling"],
        )
        t3 = await _seed_talent_full(
            f"Zz Rohan {uuid.uuid4().hex[:6]}", gender="male", city=city, age=24,
            interested_in=["Acting"],
        )
        talent_ids = [t1, t2, t3]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female models from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Found 2 talents." in r.reply
        assert "Zz Ahana" in r.reply
        assert "Zz Sneha" in r.reply
        assert "Zz Rohan" not in r.reply
        assert "Instagram:\n   https://instagram.com/ahanapocha" in r.reply
        assert "Gender: Female" in r.reply
        assert "Category: Modeling" in r.reply
        assert "Showing 2 of 2 results." in r.reply

        # Zero-result case: same city, but a criterion nothing there matches.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show female influencers from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert r2.reply.startswith("Found 0 talents matching that.")
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_refinement_chain_matches_spec_example():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        right_city = _unique_city()
        wrong_city = _unique_city()
        # Matches: female + Modeling + right_city + height>=5'7" + age<22.
        keep = await _seed_talent_full(
            f"Zz Keep {uuid.uuid4().hex[:6]}", gender="female", city=right_city, age=21,
            height="5'8\"", interested_in=["Modeling"],
        )
        # Wrong city — dropped by "Only <right_city>".
        wrong_city_id = await _seed_talent_full(
            f"Zz WrongCity {uuid.uuid4().hex[:6]}", gender="female", city=wrong_city, age=21,
            height="5'8\"", interested_in=["Modeling"],
        )
        # Right city, too short — dropped by "Above 5'7\"".
        too_short = await _seed_talent_full(
            f"Zz TooShort {uuid.uuid4().hex[:6]}", gender="female", city=right_city, age=21,
            height="5'4\"", interested_in=["Modeling"],
        )
        # Right city/height, too old — dropped by "Age under 22".
        too_old = await _seed_talent_full(
            f"Zz TooOld {uuid.uuid4().hex[:6]}", gender="female", city=right_city, age=25,
            height="5'8\"", interested_in=["Modeling"],
        )
        talent_ids = [keep, wrong_city_id, too_short, too_old]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show female models",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        assert session["talent_search"]["filters"] == {"gender": "female", "interested_in": ["Modeling"]}

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Only {right_city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zz WrongCity" not in r.reply
        session = await session_context.get_session(AGENT_ID, phone)
        assert session["talent_search"]["filters"]["location"] == [right_city]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text='Above 5\'7"',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zz TooShort" not in r.reply
        assert "Zz Keep" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Age under 22",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zz TooOld" not in r.reply
        assert "Zz Keep" in r.reply
        assert "Found 1 talents." in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_pagination_next_previous():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        prefix = uuid.uuid4().hex[:6]
        city = _unique_city()
        names = [f"Zz Page {prefix} {i:02d}" for i in range(1, 26)]  # 25 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(
                n, gender="female", city=city, interested_in=["Modeling"],
            ))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female models from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Found 25 talents." in r.reply
        assert "Showing 20 of 25 results." in r.reply
        for n in names[:20]:
            assert n in r.reply, n
        assert names[20] not in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show next 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert names[20] in r.reply
        assert names[0] not in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Previous 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert names[0] in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Previous 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "already at the first page" in r.reply.lower()

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show all",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        for n in names:
            assert n in r.reply, n
        assert "Showing all" in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_pagination_without_active_search():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show next 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "no active talent search" in r.reply.lower()
    finally:
        await _cleanup(phone)
        await _restore_config(original)


async def test_talent_search_vague_height_clarifies_then_resolves():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        tall = await _seed_talent_full(
            f"Zz Tall {uuid.uuid4().hex[:6]}", gender="female", city=city, height="5'9\"",
        )
        short = await _seed_talent_full(
            f"Zz Short {uuid.uuid4().hex[:6]}", gender="female", city=city, height="5'2\"",
        )
        talent_ids = [tall, short]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show tall girls from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "What minimum height should I use?"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="5'6\"",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Zz Tall" in r.reply
        assert "Zz Short" not in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_unsupported_language_clarifies_then_proceeds():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        t = await _seed_talent_full(f"Zz Gujarati {uuid.uuid4().hex[:6]}", city="Mumbai")
        talent_ids = [t]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show talents who speak Gujarati",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "can't filter by language" in r.reply.lower()

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="yes",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Found" in r.reply

        # Declining cancels rather than running a silently-wrong query.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show talents who speak Gujarati",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="no",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "cancelled" in r.reply.lower()
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_does_not_regress_other_query_kinds():
    """A talent search followed by an ordinary "Show ongoing projects"
    query must clear talent_search from session, so a stray refinement
    phrase typed afterwards isn't misread as still belonging to the
    search (see casting_pipeline.py's talent_search=None clears)."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        t = await _seed_talent_full(f"Zz Regress {uuid.uuid4().hex[:6]}", gender="female")
        talent_ids = [t]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show female models",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        assert session.get("talent_search") is not None

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        assert session.get("talent_search") is None

        # "Only Mumbai" now has nothing to refine — falls through as
        # unrelated chatter (untriggered, no active search), not a crash.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Only Mumbai",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_scoped_to_configured_group_only():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name="Some Other Random Group", sender_phone=phone,
            text="Show female models from Mumbai",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False
    finally:
        await _cleanup(phone)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# UX polish (2026-08-10): active-filters line, richer zero-results, a
# persistent ordinal->id result index (Phase 2 groundwork), and expanded
# gender/category vocabulary.
# ---------------------------------------------------------------------------
async def test_extract_filters_creator_synonym_maps_to_influencer_campaigns():
    parsed = nlu.extract_talent_search_filters("Show creators from Mumbai")
    assert parsed["filters"]["interested_in"] == ["Influencer Campaigns"]


async def test_extract_filters_gender_synonyms_all_map_correctly():
    for word, expected in [
        ("girls", "female"), ("women", "female"), ("female", "female"),
        ("boys", "male"), ("men", "male"), ("male", "male"),
    ]:
        parsed = nlu.extract_talent_search_filters(f"Show {word} talents")
        assert parsed["filters"]["gender"] == expected, word


async def test_extract_filters_category_synonyms_all_map_correctly():
    for word, expected in [
        ("actor", "Acting"), ("actress", "Acting"),
        ("model", "Modeling"), ("models", "Modeling"),
        ("creator", "Influencer Campaigns"), ("influencer", "Influencer Campaigns"),
    ]:
        parsed = nlu.extract_talent_search_filters(f"Show {word} talents")
        assert parsed["filters"]["interested_in"] == [expected], word


async def test_format_active_filters_matches_labeled_example():
    from agents.modules.casting_pipeline import _format_active_filters
    filters = {"gender": "female", "location": ["Mumbai"], "age_min": 18, "age_max": 22, "height_min": 67.0}
    assert _format_active_filters(filters) == 'Gender: Female, City: Mumbai, Age: 18–22, Height: 5\'7"+'


async def test_talent_search_shows_active_filters_line():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        t = await _seed_talent_full(
            f"Zz Filt {uuid.uuid4().hex[:6]}", gender="female", city=city, age=20,
            height="5'8\"", interested_in=["Modeling"],
        )
        talent_ids = [t]

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show female models from {city} between 18 and 22",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Filters: Gender: Female, Category: Modeling, City: {city}, Age: 18–22" in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_talent_search_zero_results_suggests_relaxing_filters():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        city = _unique_city()
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Show female models from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply.startswith("Found 0 talents matching that.")
        assert f"Filters: Gender: Female, Category: Modeling, City: {city}" in r.reply
        assert "Try removing one or more filters" in r.reply
        assert "Gender, Category, City" in r.reply
    finally:
        await _cleanup(phone)
        await _restore_config(original)


async def test_talent_search_result_index_persists_across_pagination():
    """Phase 2 groundwork: the ordinal shown next to each talent must be
    recorded in session.number_map (type="talent_search") with the real
    talent id, and the SAME talent must keep the SAME ordinal whether it
    was reached via the fresh search or via "Show next 20"."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        names = [f"Zz Idx {uuid.uuid4().hex[:6]} {i:02d}" for i in range(1, 23)]  # 22 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        number_map = session["number_map"]
        assert number_map["type"] == "talent_search"
        assert len(number_map["items"]) == 20
        assert number_map["items"][0]["ordinal"] == 1
        assert number_map["items"][0]["id"] == talent_ids[names.index(number_map["items"][0]["label"])]
        assert number_map["items"][19]["ordinal"] == 20

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show next 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        number_map = session["number_map"]
        assert number_map["type"] == "talent_search"
        # 22 total, page 2 has 2 items, continuing the ordinal from page 1
        # rather than resetting to 1 — same talent keeps the same number.
        assert len(number_map["items"]) == 2
        assert number_map["items"][0]["ordinal"] == 21
        assert number_map["items"][1]["ordinal"] == 22
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Post-Phase 1 UX polish (2026-08-10): only the talent line is numbered
# (metadata lines are indented, unnumbered), and Instagram renders as a
# clickable https://instagram.com/... URL instead of "@handle".
# ---------------------------------------------------------------------------
async def test_format_talent_card_only_name_line_is_numbered():
    from agents.modules.casting_pipeline import _format_talent_card

    talent = {
        "name": "Ahana Pocha", "age": 22, "height": "5'7\"",
        "location": [{"city": "Mumbai"}], "gender": "female",
        "interested_in": ["Modeling"], "instagram_handle": "ahanapocha",
    }
    card = _format_talent_card(3, talent)
    lines = card.splitlines()
    assert lines[0] == "3. Ahana Pocha"
    # No other line starts with a digit-dot ordinal — only metadata labels.
    for line in lines[1:]:
        assert not re.match(r"^\d+\.", line.strip()), line
    assert "   Age: 22" in card
    assert "   Height: 5'7\"" in card
    assert "   City: Mumbai" in card
    assert "   Gender: Female" in card
    assert "   Category: Modeling" in card
    assert "   Instagram:" in card
    assert "   https://instagram.com/ahanapocha" in card


async def test_format_talent_card_omits_missing_fields_cleanly():
    from agents.modules.casting_pipeline import _format_talent_card

    card = _format_talent_card(1, {"name": "No Data Talent"})
    assert card == "1. No Data Talent"


async def test_format_instagram_link_normalization_rules():
    from agents.modules.casting_pipeline import _format_instagram_link

    # Already a valid, canonical URL -> unchanged.
    assert _format_instagram_link("https://instagram.com/ahanapocha") == "https://instagram.com/ahanapocha"
    # URL variants (www, http, trailing slash) -> normalized to canonical form.
    assert _format_instagram_link("http://www.instagram.com/ahanapocha/") == "https://instagram.com/ahanapocha"
    assert _format_instagram_link("instagram.com/ahanapocha") == "https://instagram.com/ahanapocha"
    # "@username" -> strip "@", prepend the canonical URL.
    assert _format_instagram_link("@ahanapocha") == "https://instagram.com/ahanapocha"
    # bare username -> prepend the canonical URL.
    assert _format_instagram_link("ahanapocha") == "https://instagram.com/ahanapocha"
    # empty/missing -> omitted (None), never a broken/partial line.
    assert _format_instagram_link(None) is None
    assert _format_instagram_link("") is None
    assert _format_instagram_link("   ") is None
    # invalid characters -> omitted rather than rendered broken.
    assert _format_instagram_link("not a real handle!!") is None


async def test_talent_search_result_index_ordinals_unchanged_by_card_layout_change():
    """The persistent ordinal used for future selection (Phase 2) must be
    completely unaffected by the card-formatting change above — same
    skip+i+1 numbering, same number_map population."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Ord {prefix} {i:02d}" for i in range(1, 23)]  # 22 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1. " + names[0] in r.reply
        assert "20. " + names[19] in r.reply
        session = await session_context.get_session(AGENT_ID, phone)
        assert session["number_map"]["items"][0]["ordinal"] == 1
        assert session["number_map"]["items"][19]["ordinal"] == 20

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show next 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "21. " + names[20] in r.reply
        assert "22. " + names[21] in r.reply
        session = await session_context.get_session(AGENT_ID, phone)
        assert session["number_map"]["items"][0]["ordinal"] == 21
        assert session["number_map"]["items"][1]["ordinal"] == 22
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Phase 2 — Talent Selection & Add to Project (2026-08-10)
# ---------------------------------------------------------------------------
async def _seed_current_project(phone: str, project_id: str, label: str) -> None:
    await session_context.update_session(
        AGENT_ID, phone, current_project_id=project_id, current_project_label=label,
    )


EXPECTED_SELECTION_STATUS_SUFFIX = (
    "\n\nAvailable commands:\n\n"
    "• Show selected\n"
    "• Add selected to a project\n"
    "• Remove 3\n"
    "• Clear selection"
)


def _expected_selection_message(verb: str, affected: list, count: int) -> str:
    """Mirrors casting_pipeline._render_selection_action_result exactly —
    the checkmark block for whichever talents THIS action affected,
    followed by the compact status."""
    lines = []
    if affected:
        lines.append(f"✓ {verb}")
        lines.append("")
        lines.extend(f"• {name}" for name in affected)
        lines.append("")
    lines.append(f"Selected: {count} talent{'' if count == 1 else 's'}" + EXPECTED_SELECTION_STATUS_SUFFIX)
    return "\n".join(lines)


# --- Pure NLU: extract_selection_command / resolve_selection_spec --------
async def test_extract_selection_command_select_variants():
    assert nlu.extract_selection_command("Select 1") == {"action": "select", "spec": "1"}
    assert nlu.extract_selection_command("Select 1,3,5") == {"action": "select", "spec": "1,3,5"}
    assert nlu.extract_selection_command("Select 2-6") == {"action": "select", "spec": "2-6"}
    assert nlu.extract_selection_command("Select first 5") == {"action": "select", "spec": "first 5"}
    assert nlu.extract_selection_command("Select last 3") == {"action": "select", "spec": "last 3"}
    assert nlu.extract_selection_command("Select all") == {"action": "select", "spec": "all"}


async def test_extract_selection_command_remove_variants():
    assert nlu.extract_selection_command("Remove 3") == {"action": "remove", "spec": "3"}
    assert nlu.extract_selection_command("Remove 2,5") == {"action": "remove", "spec": "2,5"}
    assert nlu.extract_selection_command("Remove all") == {"action": "remove", "spec": "all"}
    assert nlu.extract_selection_command("Unselect 4") == {"action": "remove", "spec": "4"}
    assert nlu.extract_selection_command("Deselect 8") == {"action": "remove", "spec": "8"}


async def test_extract_selection_command_clear():
    assert nlu.extract_selection_command("Clear selection") == {"action": "clear"}
    assert nlu.extract_selection_command("Clear") == {"action": "clear"}


async def test_extract_selection_command_rejects_names_and_real_moves():
    # A talent NAME, or a real "select ... to <stage>" move — never a
    # Phase-2 selection command. This is the safety net the MOVE-trigger
    # interception in _extract_move_fields depends on.
    assert nlu.extract_selection_command("Select Priya") is None
    assert nlu.extract_selection_command("Select Priya to Approved") is None
    assert nlu.extract_selection_command("Select 1 to Approved") is None
    assert nlu.extract_selection_command("Show ongoing projects") is None


async def test_resolve_selection_spec_variants():
    ordinals = list(range(1, 21))  # a 20-result page, like Phase 1's default
    assert nlu.resolve_selection_spec("1", ordinals) == ([1], None)
    assert nlu.resolve_selection_spec("1,3,5", ordinals) == ([1, 3, 5], None)
    assert nlu.resolve_selection_spec("2-6", ordinals) == ([2, 3, 4, 5, 6], None)
    assert nlu.resolve_selection_spec("first 5", ordinals) == ([1, 2, 3, 4, 5], None)
    assert nlu.resolve_selection_spec("last 3", ordinals) == ([18, 19, 20], None)
    assert nlu.resolve_selection_spec("all", ordinals) == (ordinals, None)


async def test_resolve_selection_spec_invalid_ordinal_rejects_whole_spec():
    ordinals = list(range(1, 21))
    result, err = nlu.resolve_selection_spec("99", ordinals)
    assert result is None
    assert err == "Talent 99 is not part of the current search."
    # A MIX of valid + invalid also rejects the whole spec — never partial.
    result, err = nlu.resolve_selection_spec("1,3,99", ordinals)
    assert result is None
    assert "99" in err


# --- End-to-end: select / remove / clear / show selected -----------------
async def test_selection_select_single_multiple_range_all():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Sel {prefix} {i:02d}" for i in range(1, 6)]  # 5 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Selected", [names[0]], 1)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 2,3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Selected", [names[1], names[2]], 3)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 4-5",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Selected", [names[3], names[4]], 5)

        session = await session_context.get_session(AGENT_ID, phone)
        basket_ids = {it["id"] for it in session["selection_basket"]["items"]}
        assert basket_ids == set(talent_ids)

        # "Select all" when everything is already selected — stays at 5,
        # no duplicates, and no checkmark block (nothing newly affected).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select all",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Selected", [], 5)
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_remove_and_clear():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Rem {prefix} {i:02d}" for i in range(1, 4)]  # 3 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1,2,3",
            sender_name="Raj", sender_is_group_member=True,
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Remove 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Removed", [names[1]], 2)
        session = await session_context.get_session(AGENT_ID, phone)
        basket_ids = {it["id"] for it in session["selection_basket"]["items"]}
        assert talent_ids[1] not in basket_ids  # ordinal 2 (0-indexed: 1) removed
        assert basket_ids == {talent_ids[0], talent_ids[2]}

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Clear selection",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Selection cleared."
        session = await session_context.get_session(AGENT_ID, phone)
        assert not session.get("selection_basket")
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_unselect_deselect_synonyms_and_remove_all():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Syn {prefix} {i:02d}" for i in range(1, 3)]
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1,2",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Unselect 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Removed", [names[0]], 1)

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Deselect all",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Selection cleared."
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_invalid_ordinal_rejected():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        talent_ids.append(await _seed_talent_full(f"Zz OneOnly {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 99",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Talent 99 is not part of the current search."
        session = await session_context.get_session(AGENT_ID, phone)
        assert not (session.get("selection_basket") or {}).get("items")
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_without_active_search_rejected():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "no active talent search" in r.reply.lower()
    finally:
        await _cleanup(phone)
        await _restore_config(original)


async def test_show_selected_lists_basket_with_fresh_display_numbering():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Show {prefix} {i:02d}" for i in range(1, 3)]
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1,2",
            sender_name="Raj", sender_is_group_member=True,
        )

        for phrase in ("Show selected", "Show my selection", "Show selected talents", "Current selection"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=phrase,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, phrase
            assert r.reply == f"Currently selected (2)\n\n1. {names[0]}\n2. {names[1]}", phrase
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_show_selected_empty_basket_falls_back_to_existing_ambiguous_stage_query():
    """The critical collision-avoidance case: with NO active selection,
    "Show selected" must behave EXACTLY as it did before Phase 2 —
    resolving as the pre-existing ambiguous Approved/Locked pipeline-stage
    query — not the new "empty basket" message."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show selected",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Which pipeline did you mean?" in r.reply
        assert "Approved" in r.reply and "Locked" in r.reply
    finally:
        await _cleanup(phone)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# UX polish round 2 (2026-08-10): natural bare selection-viewing commands
# (no "Show" prefix required), a direct "Add N to Project" shortcut, and
# richer select/remove confirmations showing only the affected talents.
# ---------------------------------------------------------------------------
async def test_bare_natural_selection_phrases_show_basket_without_show_prefix():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Bare {prefix} {i:02d}" for i in range(1, 3)]
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1,2",
            sender_name="Raj", sender_is_group_member=True,
        )

        expected = f"Currently selected (2)\n\n1. {names[0]}\n2. {names[1]}"
        for phrase in ("Selected", "Selection", "My selection", "Current selection",
                       "Selected talents", "Who have I selected"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=phrase,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, phrase
            assert r.reply == expected, phrase
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_bare_selection_and_selected_do_not_collide_with_real_move_trigger():
    """Empirically confirmed via agents.parser.detect_trigger before
    implementing: "select"/"selected"/"selection" are different tokens
    under detect_trigger's exact-word matching, so adding "selected"/
    "selection"/"my selection" as new casting.query triggers cannot steal
    "Select Priya"/"Select Priya to Approved" from casting.move. This test
    is the end-to-end proof."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        name = f"Zz Collide {uuid.uuid4().hex[:6]}"
        talent_ids.append(await _seed_talent_full(name))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Select {name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Which pipeline should they move to?"
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_direct_add_shortcut_by_ordinals_reuses_existing_add_flow():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand = f"Zz DirectAdd Brand {uuid.uuid4().hex[:6]}"
        pid = await _seed_project(brand_name=brand)
        project_ids = [pid]
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Direct {prefix} {i:02d}" for i in range(1, 6)]  # 5 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add 1,3 to {brand}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to add" in r.reply
        assert names[0] in r.reply and names[2] in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Added 2 talent" in r.reply
        # No "Selection cleared." — nothing real was ever selected via
        # Select/Show selected, so there's nothing to announce clearing.
        assert "Selection cleared." not in r.reply

        rows = await db.casting_pipeline.find({"project_id": pid}).to_list(10)
        assert {r2["talent_id"] for r2 in rows} == {talent_ids[0], talent_ids[2]}
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_direct_add_shortcut_first_and_last_n():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand1 = f"Zz FirstN Brand {uuid.uuid4().hex[:6]}"
        brand2 = f"Zz LastN Brand {uuid.uuid4().hex[:6]}"
        pid1 = await _seed_project(brand_name=brand1)
        pid2 = await _seed_project(brand_name=brand2)
        project_ids = [pid1, pid2]
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz FL {prefix} {i:02d}" for i in range(1, 6)]  # 5 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add first 2 to {brand1}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to add" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Added 2 talent" in r.reply
        rows = await db.casting_pipeline.find({"project_id": pid1}).to_list(10)
        assert {r2["talent_id"] for r2 in rows} == {talent_ids[0], talent_ids[1]}

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add last 2 to {brand2}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to add" in r.reply
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Added 2 talent" in r.reply
        rows = await db.casting_pipeline.find({"project_id": pid2}).to_list(10)
        assert {r2["talent_id"] for r2 in rows} == {talent_ids[3], talent_ids[4]}
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_direct_add_shortcut_never_touches_a_real_selection_basket():
    """The critical safety property: "Add N to Project" must not read,
    overwrite, or clear the user's REAL selection_basket — it's a
    self-contained, one-off action resolved fresh against the current
    number_map, never touching session.selection_basket at all."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand = f"Zz NoClobber Brand {uuid.uuid4().hex[:6]}"
        pid = await _seed_project(brand_name=brand)
        project_ids = [pid]
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Clobber {prefix} {i:02d}" for i in range(1, 4)]  # 3 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Build a REAL selection (talent #1) — this must survive untouched.
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        session = await session_context.get_session(AGENT_ID, phone)
        assert {it["id"] for it in session["selection_basket"]["items"]} == {talent_ids[0]}

        # Direct shortcut on a DIFFERENT talent (#2), unrelated to the real selection.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add 2 to {brand}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Added 1 talent" in r.reply

        rows = await db.casting_pipeline.find({"project_id": pid}).to_list(10)
        assert {r2["talent_id"] for r2 in rows} == {talent_ids[1]}

        # The REAL selection basket is completely untouched.
        session = await session_context.get_session(AGENT_ID, phone)
        assert {it["id"] for it in session["selection_basket"]["items"]} == {talent_ids[0]}
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_selection_survives_pagination_and_refinement():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        prefix = uuid.uuid4().hex[:6]
        names = [f"Zz Surv {prefix} {i:02d}" for i in range(1, 23)]  # 22 talents
        for n in names:
            talent_ids.append(await _seed_talent_full(n, gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Select talent #1 (page 1) AND talent #21 (only reachable after paging).
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show next 20",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 21",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == _expected_selection_message("Selected", [names[20]], 2)

        # Refine — a NEW search entirely (still both, since filters don't
        # exclude anyone) — the basket must be untouched by the refine.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Only {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        session = await session_context.get_session(AGENT_ID, phone)
        basket_ids = {it["id"] for it in session["selection_basket"]["items"]}
        assert basket_ids == {talent_ids[0], talent_ids[20]}
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_survives_refinement_even_when_talent_drops_out_of_results():
    """PART 9's explicit case: a refinement that removes a previously
    selected talent from the CURRENT results must NOT silently drop them
    from the basket — the basket represents user intent."""
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        older = await _seed_talent_full(f"Zz Older {uuid.uuid4().hex[:6]}", gender="female", city=city, age=30)
        talent_ids = [older]

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Refine to exclude the very talent we just selected.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Age under 25",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply.startswith("Found 0 talents matching that.")
        session = await session_context.get_session(AGENT_ID, phone)
        basket_ids = {it["id"] for it in session["selection_basket"]["items"]}
        assert basket_ids == {older}
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_selection_basket_resets_on_unrelated_workflow():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        talent_ids.append(await _seed_talent_full(f"Zz Reset {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        session = await session_context.get_session(AGENT_ID, phone)
        assert (session.get("selection_basket") or {}).get("items")

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        session = await session_context.get_session(AGENT_ID, phone)
        assert session.get("selection_basket") is None
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


# --- End-to-end: Add / Attach selected to a project -----------------------
async def test_add_selected_to_project_unambiguous():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand = f"Zz AddSel Brand {uuid.uuid4().hex[:6]}"
        pid = await _seed_project(brand_name=brand)
        project_ids = [pid]
        for i in range(2):
            talent_ids.append(await _seed_talent_full(f"Zz AddSel {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select all",
            sender_name="Raj", sender_is_group_member=True,
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add selected to {brand}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to add" in r.reply
        assert "Reply:" in r.reply and "1 → Approve" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Added 2 talent" in r.reply
        assert "Selection cleared." in r.reply

        rows = await db.casting_pipeline.find({"project_id": pid}).to_list(10)
        assert {r["talent_id"] for r in rows} == set(talent_ids)

        session = await session_context.get_session(AGENT_ID, phone)
        assert session.get("selection_basket") is None
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_attach_selected_to_project_synonym():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand = f"Zz AttachSel Brand {uuid.uuid4().hex[:6]}"
        pid = await _seed_project(brand_name=brand)
        project_ids = [pid]
        talent_ids.append(await _seed_talent_full(f"Zz Attach {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Attach selected to {brand}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to add" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Added 1 talent" in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_add_selected_to_ambiguous_project_reuses_existing_clarification():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        city = _unique_city()
        brand = f"Zz Dup Brand {uuid.uuid4().hex[:6]}"
        pid1 = await _seed_project(brand_name=brand)
        pid2 = await _seed_project(brand_name=brand)  # exact duplicate name -> ambiguous
        project_ids = [pid1, pid2]
        talent_ids.append(await _seed_talent_full(f"Zz DupSel {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add selected to {brand}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Confirms the basket path reaches the SAME existing ambiguity
        # machinery casting.add's name-based path already uses (the
        # disambiguation options carry {label, value=label} — resolving
        # between two IDENTICALLY-named projects by re-submitting the same
        # label is a pre-existing limitation of that machinery, not
        # something Phase 2 changes; verifying the ambiguity is correctly
        # SURFACED is what matters here).
        assert "I found multiple projects." in r.reply
        assert brand in r.reply
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_add_selected_to_nonexistent_project_never_guesses():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        city = _unique_city()
        talent_ids.append(await _seed_talent_full(f"Zz NoProj {uuid.uuid4().hex[:6]}", gender="female", city=city))

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Show female talents from {city}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Select 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Add selected to Zzznonexistent{uuid.uuid4().hex[:8]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't find a project" in r.reply.lower()
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)


async def test_add_selected_with_empty_basket_rejected():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_ids = []
    try:
        pid = await _seed_project(brand_name=f"Zz EmptySel Brand {uuid.uuid4().hex[:6]}")
        project_ids = [pid]
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add selected to {pid}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "No talents are currently selected.\n\nSearch and select talents first."
    finally:
        await _cleanup(phone, project_ids=project_ids)
        await _restore_config(original)


# --- Regression: "select" as a real casting.move trigger is unaffected ---
async def test_select_with_explicit_stage_still_works_as_real_move():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids, project_ids = [], []
    try:
        pid = await _seed_project(brand_name=f"Zz RealSelectMove {uuid.uuid4().hex[:6]}")
        project_ids = [pid]
        name = f"Zz RealMoveTarget {uuid.uuid4().hex[:6]}"
        tid = await _seed_talent_full(name)
        talent_ids = [tid]
        await db.casting_pipeline.insert_one({
            "id": str(uuid.uuid4()), "project_id": pid, "talent_id": tid,
            "stage": "ask_to_test", "created_at": _now(), "updated_at": _now(),
        })
        await _seed_current_project(phone, pid, "Zz RealSelectMove")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Select {name} to Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "You are about to move" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Moved 1 talent" in r.reply
        row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
        assert row["stage"] == "approved"
    finally:
        await _cleanup(phone, talent_ids=talent_ids, project_ids=project_ids)
        await _restore_config(original)


async def test_bare_select_name_still_asks_for_stage():
    group = f"Test Talent Search {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    talent_ids = []
    try:
        name = f"Zz BareSelect {uuid.uuid4().hex[:6]}"
        talent_ids.append(await _seed_talent_full(name))

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Select {name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Which pipeline should they move to?"
    finally:
        await _cleanup(phone, talent_ids=talent_ids)
        await _restore_config(original)
