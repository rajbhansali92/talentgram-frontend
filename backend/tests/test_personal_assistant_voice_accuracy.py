"""Personal Assistant — voice command ACCURACY from existing data (2026-10-20 brief).

Layers kept apart so a failure says WHERE it is:  browser transcript → clause parse (action + phrases) → data-aware resolution → planned task.
  1. parser      pure: what was asked (action, name phrases, project phrase, stage), including mis-heard action words and word order
  2. resolvers   pure: name / project ranking against in-memory copies of the real record SHAPES (names masked where they are not the brief's examples)
  3. end to end  real FastAPI app + local dev Mongo, throwaway Qzx rows, NOTHING sent: transcript → planned task → clarification → approval
All transcripts here are SYNTHETIC realistic variations. They guard behaviour; they are not measurements of real-world voice accuracy."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import uuid

import pytest
import pytest_asyncio

from core import db
from personal_assistant import phonetic
from personal_assistant import resolve as R
from personal_assistant import update_items as U
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, alice, anon, bob, team, cleanup, confirm, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


def one(msg):
    segs = parse_message(msg)
    assert len(segs) == 1, [(s.text, s.intent) for s in segs]
    return segs[0]


# ====================================================================================== 1. parser (pure)
@pytest.mark.parametrize("msg,intent,talents,project,stage,lookup", [
    ("Share Shivi Rajput's profile.", "share", ["Shivi Rajput"], None, None, "profile"),
    ("Share Shivi's media link.", "share", ["Shivi"], None, None, "media_link"),
    ("Move Shivi Rajput to Already Tested in MK Jules film 1.", "move", ["Shivi Rajput"], "MK Jules film 1", "already_tested", None),
    ("Move Shivi to Approved in MK Jules film one.", "move", ["Shivi"], "MK Jules film one", "approved", None),
    # natural phrasing: fillers, no possessive (speech engines drop the 's), object first, polite forms
    ("um share Shivi Rajput's profile", "share", ["Shivi Rajput"], None, None, "profile"),
    ("okay so share shivi rajput profile", "share", ["shivi rajput"], None, None, "profile"),
    ("share profile Shivi Rajput", "share", ["Shivi Rajput"], None, None, "profile"),
    ("share shivi rajput media link", "share", ["shivi rajput"], None, None, "media_link"),
    ("Can you share Shivi Rajput's profile", "share", ["Shivi Rajput"], None, None, "profile"),
    ("I'd like to send Shivi Rajput the casting call for MK Jules film 1", "send", ["Shivi Rajput"], "MK Jules film 1", None, None),
    ("please move shivi rajput to approved in mk jules film one", "move", ["shivi rajput"], "mk jules film one", "approved", None),
    ("put Shivi Rajput in approved in MK Jules film 1", "move", ["Shivi Rajput"], "MK Jules film 1", "approved", None),
    # the project said first
    ("In MK Jules film 1 move Shivi Rajput to approved", "move", ["Shivi Rajput"], "MK Jules film 1", "approved", None),
])
def test_example_commands_and_natural_phrasings_reach_the_right_action(msg, intent, talents, project, stage, lookup):
    s = one(msg)
    assert (s.intent, s.talents, s.project_text, s.stage, s.lookup) == (intent, talents, project, stage, lookup)
    assert s.verb_heard is None                                                 # nothing was repaired: no question needed


@pytest.mark.parametrize("msg,intent", [
    ("moves Shivi Rajput to approved in MK Jules film 1", "move"), ("moved Shivi Rajput to approved in MK Jules film 1", "move"),
    ("sent the casting call to MK Jules film 1", "send"), ("sending the casting call for MK Jules film 1", "send"),
    ("updated Shivi Rajput's height to 5 feet 7", "update"), ("up date Shivi Rajput's height to five feet seven", "update"),
    ("set Shivi Rajput's height to five feet seven", "update"), ("change Shivi Rajput's height to 5'7", "update"), ("edit Shivi Rajput's age to 24", "update"),
    ("update Shivi Rajput height five seven", "update"), ("update Shivi Rajput's introduction video", "update"),
])
def test_inflections_synonyms_and_spoken_numbers_are_read_as_the_command_they_are(msg, intent):
    s = one(msg)
    assert s.intent == intent and s.verb_heard is None


def test_change_without_a_profile_item_is_still_a_move_and_status_is_not_an_update():
    s = one("change Shivi Rajput to approved in MK Jules film 1")
    assert s.intent == "move" and s.stage == "approved"
    assert one("change Shivi Rajput's status to approved").intent != "update"         # 'status' is not a profile item the update command supports


@pytest.mark.parametrize("msg,meant", [
    ("shear Shivi Rajput's profile", "share"), ("shore Shivi Rajput's profile", "share"), ("chair Shivi Rajput's profile", "share"), ("sher Shivi's media link", "share"),
    ("moove Shivi to Approved in MK Jules film 1", "move"), ("mood Shivi Rajput to Already Tested in MK Jules film 1", "move"),
    ("sand the casting call to MK Jules film 1", "send"), ("cent casting call to MK Jules film 1", "send"),
])
def test_a_soundalike_action_word_becomes_a_hypothesis_that_must_be_asked_about(msg, meant):
    s = one(msg)
    assert s.verb_heard and s.verb_heard["meant"] == meant and s.verb_heard["said"] == msg.split()[0].lower()
    assert s.intent in {"share": {"share"}, "move": {"move"}, "send": {"send", "share"}}[meant]


@pytest.mark.parametrize("msg", [
    "the weather is nice today", "shake the table", "mood board for MK Jules film 1", "Shivi Rajput to approved in MK Jules film 1", "hello there",
    "what time is it", "sandwich for Shivi", "movie night",
])
def test_unrelated_phrases_never_become_a_command(msg):
    assert all(s.intent == "unknown" for s in parse_message(msg))


@pytest.mark.parametrize("msg", [
    "made Shivi Rajput to approved in MK Jules film 1", "mode Shivi Rajput to approved in MK Jules film 1", "mute Shivi Rajput to approved in MK Jules film 1",
    "bend the casting call to MK Jules film 1", "sandy the casting call to MK Jules film 1", "lend Shivi Rajput's profile",
])
def test_only_words_that_really_sound_like_a_command_word_are_treated_as_one(msg):
    for s in parse_message(msg):
        assert s.verb_heard is None and s.intent in ("unknown", "lookup"), msg               # no repaired verb; at most a read-only lookup of a name that matches nobody


def test_a_leading_phrase_that_is_not_a_project_is_not_rewritten_into_one():
    s = one("In Mumbai move Shivi Rajput to approved in MK Jules film 1")
    assert s.intent == "unknown" or (s.project_text or "").lower() == "mk jules film 1"       # never "MK Jules film 1 in Mumbai"


def test_a_correction_inside_an_utterance_is_still_never_acted_on():
    assert all(s.intent == "unknown" and s.correction for s in parse_message("move Shivi Rajput, no wait, Shivi Bhatt to approved in MK Jules film 1"))


def test_multi_command_utterances_split_into_one_action_each():
    segs = parse_message("Share Shivi's profile and move her to Already Tested in MK Jules film 1.")
    assert [s.intent for s in segs] == ["share", "move"] and segs[1].talent_ref == "previous" and segs[1].stage == "already_tested"
    segs = parse_message("share shivi's profile move her to already tested in mk jules film 1")        # no punctuation at all
    assert [s.intent for s in segs] == ["share", "move"]


def test_spoken_numbers_become_values_deterministically_and_nothing_else_is_guessed():
    assert U.parse_height("five feet seven")[0] == "5'7\"" and U.parse_height("five seven")[0] == "5'7\"" and U.parse_height("six feet")[0] == "6'0\""
    assert U.parse_age("twenty four") == 24 and U.parse_age("thirty") == 30
    for bad in ("one seventy", "tall", "nine feet two"):
        with pytest.raises(U.BadValue):
            U.parse_height(bad)


# ====================================================================================== 2. resolvers (pure)
# Masked copies of the real roster's SHAPES: the brief's own example (Shivi Rajput) plus look-alike siblings with invented surnames.
ROSTER = [("t1", "Shivi Rajput"), ("t2", "Shivi Bhatt"), ("t3", "Shivang Rajpaul"), ("t4", "Shivani Joshua"), ("t5", "Priya Singh"), ("t6", "Priya Sharma"), ("t7", "Shivi Rajputra")]


def rank(q):
    from agents.modules import casting_pipeline_nlu as nlu
    return [(c.label, k) for c, k, _s in R.rank_talents(q, [nlu.Candidate(id=i, label=n) for i, n in ROSTER])]


def test_phonetics_cover_the_substitutions_speech_engines_really_make():
    assert phonetic.similarity("chivi", "shivi") >= phonetic.PHON_MIN and phonetic.similarity("sivi", "shivi") >= phonetic.PHON_MIN
    assert phonetic.similarity("jules", "jewels") >= phonetic.PHON_MIN and phonetic.similarity("rajpoot", "rajput") == 1.0
    assert phonetic.similarity("shivi", "kivi") == 0.0                          # a different first sound is still a different name


@pytest.mark.parametrize("said", ["Shivy Rajput", "Chivi Rajput", "Sheevi Rajput", "Shivi Rajpoot", "Shiwi Rajput", "Shivee Rajput", "Chivy Rajpoot"])
def test_an_imperfect_transcription_ranks_the_intended_talent_first_but_is_never_exact(said):
    ranked = rank(said)
    assert ranked and ranked[0][0] == "Shivi Rajput" and ranked[0][1] != "exact"      # a card to tap, never silently chosen


def test_spacing_only_differences_are_exact_and_similar_names_stay_apart():
    assert rank("Shivi Rajput")[0] == ("Shivi Rajput", "exact")
    assert rank("shivi raj put")[0] == ("Shivi Rajput", "exact")                       # same letters, only the spacing differs
    first = rank("Shivi")
    assert {n for n, _k in first} >= {"Shivi Rajput", "Shivi Bhatt"} and {k for n, k in first if n in ("Shivi Rajput", "Shivi Bhatt")} == {"partial"}
    assert "Shivani Joshua" not in [n for n, _k in rank("Shivi Rajput")]                # a different person is never offered for a full name


def test_a_surname_alone_or_a_first_name_alone_is_never_resolved_to_one_of_several():
    for q in ("Shivi", "Priya"):
        kinds = [(n, k) for n, k in rank(q)]
        assert len([1 for _n, k in kinds if k == "partial"]) >= 2             # several fit: the resolver will ask


PROJECTS = [{"id": f"p{i}", "label": l, "status": st, "character": ""} for i, (l, st) in enumerate([
    ("MK Jewels - film 1", "ongoing"), ("MK Jewels - film 2", "ongoing"), ("MK Jewels - Mother", "ongoing"), ("MK Jewels - Dadi", "ongoing"),
    ("Skoda Slavia Film 1", "complete"), ("Secret Temptation Film 1", "complete"), ("Macho Sporto - film 2 ", "complete")])]


async def proj(q):
    return await R.resolve_projects(q, projects=PROJECTS)


@pytest.mark.parametrize("said", ["MK Jewels film 1", "MK Jewels - film 1", "mk jewels film one", "M K Jewels Film One", "MK Jewel film 1", "Mkjewels film 1", "em kay jewels film 1"])
async def test_a_project_said_correctly_in_any_number_or_spacing_form_resolves(said):
    (r,) = await proj(said)
    assert r["state"] == "resolved" and r["label"] == "MK Jewels - film 1"


@pytest.mark.parametrize("said", ["MK Jules film 1", "MK Jules Film One", "MK Jules film one", "M K Jules film 1", "Mk jules film1", "em kay jules film 1"])
async def test_a_soundalike_project_is_proposed_as_one_flagged_card_never_chosen(said):
    (r,) = await proj(said)
    assert r["state"] == "ambiguous"
    assert [o["label"] for o in r["options"]][0] == "MK Jewels - film 1" and r["options"][0].get("best") is True
    assert "MK Jewels - film 2" not in [o["label"] for o in r["options"]]          # film 1 and film 2 are never conflated


async def test_a_soundalike_with_several_candidates_proposes_none_of_them():
    (r,) = await proj("MK Jules")                                                  # "Jewels" heard differently, and no film / number to say which
    assert r["state"] == "ambiguous" and len(r["options"]) >= 3 and not any(o.get("best") for o in r["options"])


async def test_similar_projects_stay_separate_and_a_bare_brand_asks():
    (a,) = await proj("MK Jewels film 2")
    assert a["state"] == "resolved" and a["label"] == "MK Jewels - film 2"
    (b,) = await proj("MK Jewels")
    assert b["state"] == "ambiguous" and len(b["options"]) >= 4 and not any(o.get("best") for o in b["options"])
    (c,) = await proj("MK Jewels film")
    assert c["state"] == "ambiguous" and {o["label"] for o in c["options"]} >= {"MK Jewels - film 1", "MK Jewels - film 2"}
    (d,) = await proj("Quantum Zebra Spaceship")
    assert d["state"] == "none"                                                   # never fabricated


def test_spoken_initials_are_joined_only_as_a_run():
    assert R.join_spoken_initials("M K Jules film 1") == "MK Jules film 1" and R.join_spoken_initials("M. K. Jules") == "MK Jules"
    assert R.join_spoken_initials("em kay jewels film 1") == "MK jewels film 1"
    assert R.join_spoken_initials("see you in the film") == "see you in the film"          # everyday words are never read as letters
    assert R.join_spoken_initials("MK Jules film 1") == "MK Jules film 1"


# ====================================================================================== 3. end to end
GROUP = "ZZZ VA GROUP"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def va(world):
    r = {}
    r["shivi"] = await _talent("Qzx Shivi Rajput")
    r["shivi2"] = await _talent("Qzx Shivi Bhatt")                              # same first name, different person
    r["shivang"] = await _talent("Qzx Shivang Rajpaul")
    r["sher"] = await _talent("Qzx Sher Quillan")                              # a real name that BEGINS like the verb "share"
    r["mom"] = await _talent("Qzx Mother")                                      # a talent whose whole name is also a word in a project name
    r["film1"] = await _project("Qzx MK Jewels - film 1", whatsapp_casting_group_name=GROUP)
    r["film2"] = await _project("Qzx MK Jewels - film 2")
    r["mother"] = await _project("Qzx MK Jewels - Mother")
    r["old"] = await _project("Qzx Skoda Slavia Film 1", status="complete")
    r["cc"] = world["cc"]
    r["w1"] = world["w1"]
    return r


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh():
    R.invalidate_roster()
    yield
    R.invalidate_roster()


def slot_kinds(t):
    return [s["kind"] for s in t["slots"]]


def talent_state(t, i=0):
    return t["params"]["talents"][i]


async def clarify(anon, who, task, slot, choice=None, text=None, expect=200):
    body = {"slot": slot, **({"choice_id": choice} if choice else {}), **({"text": text} if text else {})}
    r = await anon.post(f"{BASE}/tasks/{task['id']}/clarify", headers=who["headers"], json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def test_brief_examples_end_to_end_create_the_right_tasks_and_nothing_runs(anon, alice, va):
    await _add_rows(va["film1"], va["shivi"])
    before_jobs = len(await _jobs([va["shivi"]]))
    s = (await say(anon, alice, "Share Shivi Rajput's profile."))["tasks"][0]
    assert s["kind"] == "share" and talent_state(s)["state"] == "resolved" and talent_state(s)["id"] == va["shivi"] and slot_kinds(s) == ["recipient"]
    m = (await say(anon, alice, "Move Shivi Rajput to Already Tested in Qzx MK Jewels film 1"))["tasks"][0]
    assert m["kind"] == "move" and m["status"] == "awaiting_confirmation" and m["params"]["stage"] == "already_tested"
    assert m["params"]["projects"][0]["id"] == va["film1"] and talent_state(m)["id"] == va["shivi"]
    assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"               # nothing ran: it waits for the approval
    u = (await say(anon, alice, "Update Shivi Rajput's height to five feet seven."))["tasks"][0]
    assert u["kind"] == "update" and u["status"] == "awaiting_confirmation" and u["params"]["updates"]["fields"][0]["value"] == "5'7\""
    assert len(await _jobs([va["shivi"]])) == before_jobs


@pytest.mark.parametrize("said", ["Shivy Rajput", "Chivi Rajput", "Sheevi Rajput", "Shivi Rajpoot", "Shivi Raj put", "Shivi Rajput ji", "Chivy Rajpoot", "Shivi Rajputt"])
async def test_a_misheard_talent_becomes_a_card_for_the_intended_person_not_a_silent_choice(anon, alice, va, said):
    t = (await say(anon, alice, f"Share {said}'s profile."))["tasks"][0]
    e = talent_state(t)
    if e["state"] == "resolved":
        assert e["id"] == va["shivi"]                                           # only possible for a spacing / honorific-only difference
    else:
        assert e["state"] == "ambiguous" and e["options"][0]["id"] == va["shivi"]
        assert t["status"] == "needs_clarification" and "talent:0" in [s["key"] for s in t["slots"]]


async def test_a_first_name_shared_by_two_talents_asks_and_only_the_server_offered_ids_are_accepted(anon, alice, va):
    t = (await say(anon, alice, "Share Shivi's media link."))["tasks"][0]
    e = talent_state(t)
    assert e["state"] == "ambiguous" and {o["id"] for o in e["options"]} >= {va["shivi"], va["shivi2"]}
    await clarify(anon, alice, t, "talent:0", choice=va["shivang"], expect=400)               # an id the server did not offer is refused
    ok = (await clarify(anon, alice, t, "talent:0", choice=va["shivi"]))["task"]
    assert talent_state(ok)["state"] == "resolved" and talent_state(ok)["id"] == va["shivi"]


async def test_a_misheard_project_is_one_flagged_card_and_film_1_is_never_swapped_for_film_2(anon, alice, va):
    await _add_rows(va["film1"], va["shivi"])
    for said in ("Qzx MK Jules film 1", "Qzx MK Jules Film One", "Qzx M K Jules film one"):
        m = (await say(anon, alice, f"Move Shivi Rajput to Approved in {said}"))["tasks"][0]
        pe = m["params"]["projects"][0]
        assert pe["state"] == "ambiguous" and pe["options"][0]["id"] == va["film1"] and pe["options"][0].get("best") is True
        assert va["film2"] not in [o["id"] for o in pe["options"]] and m["status"] == "needs_clarification"
        assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"
    done = (await clarify(anon, alice, m, "project:0", choice=va["film1"]))["task"]
    assert done["status"] == "awaiting_confirmation"
    assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"                                 # still not run: approval is separate from the pick
    ran = (await confirm(anon, alice, done["id"]))["task"]
    assert ran["status"] == "completed" and await _stage(va["film1"], va["shivi"]) == "approved"


async def test_a_soundalike_action_word_asks_did_you_mean_and_nothing_runs_until_confirmed_and_approved(anon, alice, va):
    await _add_rows(va["film1"], va["shivi"])
    t = (await say(anon, alice, "moove Shivi Rajput to Approved in Qzx MK Jewels film 1"))["tasks"][0]
    assert t["kind"] == "move" and t["status"] == "needs_clarification" and slot_kinds(t)[0] == "action"
    assert "move" in t["slots"][0]["prompt"] and [o["id"] for o in t["slots"][0]["options"]] == ["yes"]
    assert (await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={})).json()["task"]["status"] == "needs_clarification"   # cannot be approved yet
    assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"
    await clarify(anon, alice, t, "action", text="move it", expect=400)                         # free text is not accepted for the action question
    await clarify(anon, alice, t, "action", choice="no", expect=400)
    asked = (await clarify(anon, alice, t, "action", choice="yes"))["task"]
    assert asked["status"] == "awaiting_confirmation" and asked["params"]["verb_confirmed"] is True
    assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"
    assert (await confirm(anon, alice, asked["id"]))["task"]["status"] == "completed" and await _stage(va["film1"], va["shivi"]) == "approved"


async def test_chair_and_shear_become_share_after_a_question_and_the_share_still_needs_a_recipient(anon, alice, va):
    for said in ("chair", "shear"):
        t = (await say(anon, alice, f"{said} Shivi Rajput's profile"))["tasks"][0]
        assert t["kind"] == "share" and slot_kinds(t)[0] == "action" and talent_state(t)["id"] == va["shivi"]
        after = (await clarify(anon, alice, t, "action", choice="yes"))["task"]
        assert after["status"] == "needs_clarification" and slot_kinds(after) == ["recipient"]      # the share's own question is still there


async def test_unrelated_phrases_with_a_soundalike_word_are_not_turned_into_commands(anon, alice, va):
    for msg in ("sand paper for Shivi", "mood board for Qzx MK Jewels film 1", "shake the table", "cent per day for tomorrow"):
        out = await say(anon, alice, msg)
        assert out["tasks"] == [] and out["unrecognised"]


async def test_a_real_name_that_looks_like_a_verb_is_read_as_the_person(anon, alice, va):
    t = (await say(anon, alice, "Sher Quillan's profile"))["tasks"][0]
    assert t["kind"] == "lookup" and "action" not in slot_kinds(t) and talent_state(t)["id"] == va["sher"]
    assert t["status"] == "completed"                                                         # a read-only lookup, as typed


async def test_send_to_a_project_name_is_read_as_the_project_and_the_talents_come_from_the_selection(anon, alice, va):
    ctx = {"talent_ids": [va["shivi"]]}
    r = await anon.post(f"{BASE}/message", headers=alice["headers"], json={"text": "Send the casting call to Qzx MK Jewels film 1", "client_request_id": uuid.uuid4().hex, "context": ctx})
    tasks = r.json()["tasks"]
    t = next(x for x in tasks if x["kind"] == "send")                                        # (a missing pipeline entry is planned as a first "add" step)
    assert t["params"]["projects"][0]["id"] == va["film1"] and [e["id"] for e in t["params"]["talents"]] == [va["shivi"]]
    assert not (await _jobs([va["shivi"]]))                                                   # planned only
    for x in tasks:
        await anon.post(f"{BASE}/tasks/{x['id']}/cancel", headers=alice["headers"])
    # a name that is BOTH a talent and (part of) a project is read as the talent
    tasks3 = (await say(anon, alice, "Send the casting call to Mother"))["tasks"]
    t3 = next(x for x in tasks3 if x["kind"] == "send")
    assert [e["id"] for e in t3["params"]["talents"]] == [va["mom"]] and not [e for e in t3["params"]["projects"] if e.get("id")]
    for x in tasks3:
        await anon.post(f"{BASE}/tasks/{x['id']}/cancel", headers=alice["headers"])
    # a name that IS a talent is still read as a talent
    tasks2 = (await say(anon, alice, "Send the casting call to Shivi Rajput for Qzx MK Jewels film 1"))["tasks"]
    t2 = next(x for x in tasks2 if x["kind"] == "send")
    assert [e["id"] for e in t2["params"]["talents"]] == [va["shivi"]]
    for x in tasks2:
        await anon.post(f"{BASE}/tasks/{x['id']}/cancel", headers=alice["headers"])


async def test_update_synonyms_and_spoken_values_plan_the_same_update(anon, alice, va):
    for msg in ("Set Shivi Rajput's height to five feet seven", "update Shivi Rajput height five seven", "change Shivi Rajput's height to 5'7", "updated Shivi Rajput's height to 5 feet 7"):
        t = (await say(anon, alice, msg))["tasks"][0]
        assert t["kind"] == "update" and t["status"] == "awaiting_confirmation" and t["params"]["updates"]["fields"][0]["value"] == "5'7\"", msg
    before = await db.talents.find_one({"id": va["shivi"]}, {"_id": 0, "height": 1})
    assert before["height"] == "5'6\""                                                         # planning never writes


async def test_multi_command_share_then_move_keeps_the_dependency_and_runs_nothing(anon, alice, va):
    await _add_rows(va["film1"], va["shivi"])
    out = await say(anon, alice, "Share Shivi Rajput's profile and move her to Already Tested in Qzx MK Jewels film 1.")
    a, b = out["tasks"]
    assert (a["kind"], b["kind"]) == ("share", "move") and talent_state(b)["id"] == va["shivi"]
    assert a["status"] == "needs_clarification" and await _stage(va["film1"], va["shivi"]) == "ask_to_test"


async def test_when_part_of_a_multi_command_cannot_be_resolved_nothing_is_run(anon, alice, va):
    await _add_rows(va["film1"], va["shivi"])
    out = await say(anon, alice, "Share Qzv Nonexistentperson's profile and move her to Already Tested in Qzx MK Jewels film 1.")
    a, b = out["tasks"]
    assert talent_state(a)["state"] == "none" and a["status"] == "needs_clarification"
    assert b["status"] == "needs_clarification" and talent_state(b)["state"] == "none"       # "her" inherits the unresolved person: no subset is run
    assert await _stage(va["film1"], va["shivi"]) == "ask_to_test"


async def test_unknown_entities_are_never_fabricated(anon, alice, va):
    t = (await say(anon, alice, "Move Qzv Nobodyhere to Approved in Qzx Quantum Zebra Spaceship"))["tasks"][0]
    assert talent_state(t)["state"] == "none" and t["params"]["projects"][0]["state"] == "none" and t["status"] == "needs_clarification"
    assert not talent_state(t).get("id") and not t["params"]["projects"][0].get("id")


async def test_a_closed_project_is_not_silently_substituted_for_a_live_one(anon, alice, va):
    t = (await say(anon, alice, "Add Shivi Rajput to Qzx Skoda Slavia Film 1"))["tasks"][0]          # the closed one, by its exact name
    assert t["status"] == "failed" and "ongoing" in t["error"]                                         # the existing rule still refuses it, loudly


async def test_authorization_and_isolation_are_unchanged(anon, alice, bob, team, va):
    assert (await anon.post(f"{BASE}/message", headers=team["headers"], json={"text": "moove Shivi Rajput to approved", "client_request_id": uuid.uuid4().hex})).status_code == 403
    await _add_rows(va["film1"], va["shivi"])
    t = (await say(anon, alice, "moove Shivi Rajput to Approved in Qzx MK Jewels film 1"))["tasks"][0]
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=bob["headers"], json={"slot": "action", "choice_id": "yes"})
    assert r.status_code in (403, 404) and (await db.assistant_tasks.find_one({"id": t["id"]}))["params"].get("verb_confirmed") is None
    assert (await anon.post(f"{BASE}/message", json={"text": "share Shivi", "client_request_id": uuid.uuid4().hex})).status_code in (401, 403)


async def test_a_run_on_spoken_clause_with_no_punctuation_still_plans_each_action(anon, alice, va):
    out = await say(anon, alice, "share shivi rajput profile move shivi rajput to approved in qzx mk jewels film 1")
    assert [t["kind"] for t in out["tasks"]] == ["share", "move"]
