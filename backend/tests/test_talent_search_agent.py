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
import string
import uuid
from datetime import date

import pytest

from core import db, parse_height_to_inches
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


async def _cleanup(phone: str, talent_ids=()) -> None:
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_tasks.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})


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
        assert "Instagram: @ahanapocha" in r.reply
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
