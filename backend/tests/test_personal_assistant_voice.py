"""Personal Assistant — interpreting what a BROWSER speech engine actually produces (2026-10-19 brief).

Browser speech recognition returns one run of lower-case words with NO punctuation, sometimes a spoken correction, and names that only
sound like the stored ones. Layers kept apart here:   raw transcript → clause split → resolved records → the action that is approved.
Same harness as test_personal_assistant.py (real FastAPI app + real local dev Mongo, throwaway Qzx rows; nothing is sent).

SYNTHETIC mutations below are repeatable regression guards — they are NOT measurements of real-world voice accuracy."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import random

import pytest
import pytest_asyncio

from agents.modules import casting_pipeline_nlu as nlu
from core import db
from personal_assistant import resolve as R
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, alice, anon, bob, cleanup, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")
GROUP_V = "ZZZ VC GROUP V"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def v(world):
    r = {}
    r["sahal"] = await _talent("Qzx Sahal Mansuri")
    r["disha"] = await _talent("Qzx Disha Verma")
    r["vikram"] = await _talent("Qzx Vikram Sharma")
    r["sahil"] = await _talent("Qzx Sahilesh Mansuri")                               # a LONGER name that merely starts like Sahil
    r["sahi"] = await _talent("Qzx Sahil Mansurii")
    r["toba"] = await _project("Qzx Tobamade Trend Film")
    r["amazon"] = await _project("Qzx Amazon Now")
    r["emb1"] = await _project("Qzx Embassy One", whatsapp_casting_group_name=GROUP_V)
    r["film2"] = await _project("Qzx Quarry Film 2")
    r["seb"] = await _project("Qzx Sebamed Skin Campaign")
    r["w1"] = world["w1"]
    r["cc"] = world["cc"]
    return r


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh():
    R.invalidate_roster()
    await db.whatsapp_jobs.delete_many({"destination": GROUP_V})
    yield
    R.invalidate_roster()
    await db.whatsapp_jobs.delete_many({"destination": GROUP_V})
    await db.talents.update_many({"id": {"$regex": "^zzz-test-pa-t-"}}, {"$set": {"status": "ACTIVE"}})


# ======================================================= 1. run-on speech is split into separate commands
SPOKEN = ("add qzx sahal mansuri to qzx tobamade trend film move qzx disha verma to follow up in qzx amazon now "
          "send the qzx casting call for qzx embassy one to qzx vikram sharma")


def test_a_punctuation_free_transcript_becomes_three_commands_with_their_own_entities():
    segs = parse_message(SPOKEN)
    assert [s.intent for s in segs] == ["add", "move", "send"]
    assert segs[0].talents == ["qzx sahal mansuri"] and segs[0].project_text == "qzx tobamade trend film"
    assert segs[1].talents == ["qzx disha verma"] and segs[1].stage == "follow_up" and segs[1].project_text == "qzx amazon now"
    assert segs[2].talents == ["qzx vikram sharma"] and segs[2].template == "qzx casting call" and segs[2].project_text == "qzx embassy one"
    assert not any(s.talent_ref for s in segs[:2])                                     # nothing referred to anything else


def test_a_verb_that_is_part_of_a_name_or_a_half_clause_is_never_a_split_point():
    assert [s.intent for s in parse_message("add mark singh to show time film")] == ["add"]
    one = parse_message("add mark singh to show time film")[0]
    assert one.talents == ["mark singh"] and one.project_text == "show time film"
    assert [s.intent for s in parse_message("move mark to hold in amazon now")] == ["move"]
    assert [s.intent for s in parse_message("send the casting call to vikram and disha")] == ["send"]
    assert parse_message("send the casting call to vikram and disha")[0].talents == ["vikram", "disha"]


def test_show_the_kit_and_send_it_without_punctuation_still_pairs_the_two_clauses():
    segs = parse_message("show me the media link of qzx angela kumar send it to qzx embassy one")
    assert [s.intent for s in segs] == ["lookup", "share"] and segs[1].talent_ref == "previous" and segs[1].recipient["value"] == "qzx embassy one"


# ======================================================= 2. spoken corrections are never executed
@pytest.mark.parametrize("text", [
    "move qzx doha — no, qzx disha verma to approved in qzx amazon now",
    "move qzx doha, no, qzx disha verma to approved in qzx amazon now",
    "move qzx doha no qzx disha verma to approved in qzx amazon now",
    "never mind move qzx disha verma to approved in qzx amazon now",
    "move qzx disha verma to approved in qzx amazon now sorry I mean hold",
])
def test_a_spoken_correction_holds_the_clause_instead_of_acting_on_either_name(text):
    segs = parse_message(text)
    assert segs and all(s.intent == "unknown" and s.correction for s in segs), text


def test_a_correction_holds_only_the_clause_it_corrects():
    segs = parse_message("add qzx sahal mansuri to qzx tobamade trend film move qzx doha no qzx disha verma to approved in qzx amazon now")
    assert [s.intent for s in segs] == ["add", "unknown"] or [s.intent for s in segs] == ["unknown", "unknown"]
    assert not any(s.intent == "move" for s in segs)


async def test_a_corrected_command_creates_no_task_and_changes_nothing(anon, alice, v):
    out = await say(anon, alice, "move qzx doha no qzx disha verma to approved in qzx amazon now")
    assert out["tasks"] == [] and "correction" in out["unrecognised"][0]
    assert await db.casting_pipeline.count_documents({"project_id": v["amazon"]}) == 0


# ======================================================= 3. a typed word that is only the START of a name is a card
async def test_only_whole_words_resolve_directly_a_prefix_is_a_card(anon, alice, v):
    assert (await R.resolve_talent("Qzx Sahal Mansuri"))["id"] == v["sahal"]            # exact
    r = await R.resolve_talent("Qzx Sahal")                                              # whole words of exactly one name → still direct
    assert r["state"] == "resolved" and r["id"] == v["sahal"]
    r = await R.resolve_talent("Qzx Sahi Mansuri")                                       # "Sahi" is only the start of "Sahil..." / near "Sahal"
    assert r["state"] == "ambiguous" and not any(o.get("match") == "exact" for o in r["options"])
    assert R.score_name("Qzx Dish", "Qzx Disha Verma")[0] == "prefix"
    assert R.score_name("Qzx Disha", "Qzx Disha Verma")[0] == "partial"


def test_candidates_of_the_same_kind_are_ordered_by_how_closely_the_whole_name_matches():
    """Regression: `whole` (the string-similarity ratio) was once overwritten by a boolean, so the 0.1*ratio term collapsed and equally good
    word matches were ordered alphabetically instead of by overall closeness."""
    import difflib
    pillai, pellai = nlu.Candidate(id="1", label="Qzx Anaya Pillai"), nlu.Candidate(id="2", label="Qzx Anaya Pellai")
    ranked = R.rank_talents("Qzx Anaya Pilay", [pellai, pillai])                          # alphabetical order would put Pellai first
    assert [c.label for c, _k, _s in ranked] == ["Qzx Anaya Pillai", "Qzx Anaya Pellai"]
    assert ranked[0][1] == ranked[1][1]                                                      # same kind …
    assert ranked[0][2] > ranked[1][2]                                                       # … but a strictly higher score for the closer name
    q, l = nlu._normalize_name("Qzx Anaya Pilay"), nlu._normalize_name("Qzx Anaya Pillai")
    cover = sum(max(R._word_sim_ext(t, x)[0] for x in l.split()) for t in q.split()) / len(q.split())
    assert ranked[0][2] == round(cover + 0.1 * difflib.SequenceMatcher(None, q, l).ratio(), 4)   # the numeric ratio, not a boolean


async def test_cards_for_equally_plausible_sound_alikes_put_the_closer_name_first(anon, alice, v):
    a = await _talent("Qzx Anaya Pellai")
    b = await _talent("Qzx Anaya Pillai")
    R.invalidate_roster()
    r = await R.resolve_talent("Qzx Anaya Pilay")
    assert r["state"] == "ambiguous" and [o["id"] for o in r["options"][:2]] == [b, a]
    assert not any(o.get("best") for o in r["options"] if o.get("match") == "sounds_like")        # still only proposals, never chosen


# ======================================================= 4. projects: spoken numbers, sound-alikes, no-match
async def test_spoken_numbers_and_digits_name_the_same_project(anon, alice, v):
    for spoken in ("Qzx Quarry Film Two", "Qzx Quarry Film 2", "qzx quarry film two"):
        r = (await R.resolve_projects(spoken))[0]
        assert r["state"] == "resolved" and r["id"] == v["film2"], spoken
    assert (await R.resolve_projects("Qzx Embassy 1"))[0]["id"] == v["emb1"]             # "One" in the stored name is a number too
    assert (await R.resolve_projects("Qzx Embassy One"))[0]["id"] == v["emb1"]


async def test_a_sound_alike_project_is_a_card_never_chosen_and_gibberish_finds_nothing(anon, alice, v):
    r = (await R.resolve_projects("Qzx Cebamed Skin Campaign"))[0]
    assert r["state"] == "ambiguous" and [o["id"] for o in r["options"]] == [v["seb"]]
    assert (await R.resolve_projects("Qzx Zzyzx Qwerty Plonk"))[0]["state"] == "none"


async def test_a_sound_alike_project_never_supplies_a_whatsapp_group(anon, alice, v):
    t = (await say(anon, alice, "send qzx casting call for qzx embasy won to qzx vikram sharma"))["tasks"]
    assert await db.whatsapp_jobs.count_documents({"destination": GROUP_V}) == 0
    share = (await say(anon, alice, "send qzx sahal mansuri's media link to qzx embasy 1"))["tasks"][0]
    assert share["status"] == "needs_clarification" and not any(p.get("state") == "resolved" for p in share["params"].get("projects") or [])
    assert t is not None


# ======================================================= 5. raw words → resolved record → approved action
async def test_the_approval_shows_what_was_said_next_to_what_it_became(anon, alice, v):
    await _add_rows(v["amazon"], v["disha"], stage="ask_to_test")
    t = (await say(anon, alice, "move qzx disha verma to improved in qzx amazon now"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"][0]["options"][0]["id"] == "approved"
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "stage", "choice_id": "approved"})
    picked = r.json()["task"]
    assert picked["status"] == "awaiting_confirmation"
    assert "You said “improved” → Approved" in picked["summary"]
    assert await _stage(v["amazon"], v["disha"]) == "ask_to_test"                        # the words changed nothing; the approval will
    p2 = (await say(anon, alice, "move qzx disha to follow up in qzx amazon now"))["tasks"][0]
    assert "You said “qzx disha” → Qzx Disha Verma" in p2["summary"]                      # a partial name is shown next to the full record


# ======================================================= 6. the whole spoken example end to end
async def test_the_run_on_example_plans_three_independent_actions_and_never_runs_an_unresolved_one(anon, alice, v):
    out = await say(anon, alice, SPOKEN.replace("qzx disha verma", "qzx dish"))           # the middle name is only the START of a name
    by = {t["kind"]: t for t in out["tasks"] if t["kind"] != "add" or not t["params"].get("prereq_for")}
    assert by["add"]["params"]["talents"][0]["id"] == v["sahal"] and by["add"]["params"]["projects"][0]["id"] == v["toba"]
    assert by["move"]["status"] == "needs_clarification" and by["move"]["params"]["projects"][0]["id"] == v["amazon"]
    assert by["send"]["params"]["talents"][0]["id"] == v["vikram"]
    assert v["sahal"] not in [e.get("id") for e in by["move"]["params"]["talents"]] and v["vikram"] not in [e.get("id") for e in by["move"]["params"]["talents"]]
    body = {"worker_id": v["w1"], "ack_unverified": True, "fingerprints": {t["id"]: t["fingerprint"] for t in out["tasks"] if t["status"] == "awaiting_confirmation"}}
    res = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json=body)
    assert res.status_code == 200
    assert (await anon.get(f"{BASE}/tasks/{by['move']['id']}", headers=alice["headers"])).json()["task"]["status"] == "needs_clarification"
    assert await _stage(v["amazon"], v["disha"]) is None                                  # the unresolved person was never moved


# ======================================================= 7. repeatable synthetic evaluation (NOT real voice accuracy)
_FIRST = ["Aarav", "Ananya", "Arjun", "Disha", "Dhruv", "Ishaan", "Isha", "Kabir", "Kavya", "Meera", "Mihir", "Naina", "Neha", "Nikhil", "Pooja", "Priya",
          "Rahul", "Riya", "Rohan", "Sahil", "Sahal", "Sanya", "Shruti", "Siddharth", "Tanvi", "Varun", "Vidushi", "Vikram", "Yash", "Zoya"]
_LAST = ["Kumar", "Sharma", "Verma", "Mansuri", "Nair", "Iyer", "Shah", "Gupta", "Khan", "Rao", "Singh", "Mehta", "Joshi", "Patel", "Desai"]


def _synthetic_roster():
    rnd = random.Random(2024)
    names = sorted({f"{rnd.choice(_FIRST)} {rnd.choice(_LAST)}" for _ in range(900)})[:400]
    return names, [nlu.Candidate(id=str(i), label=n) for i, n in enumerate(names)]


def _decide(q, cands):
    ranked = R.rank_talents(q, cands)
    exact = [r for r in ranked if r[1] == "exact"]
    partial = [r for r in ranked if r[1] == "partial"]
    if len(exact) == 1:
        return "auto", exact[0][0].label, ranked
    if not exact and len(partial) == 1:
        return "auto", partial[0][0].label, ranked
    return ("cards" if ranked else "none"), None, ranked


def _mutate(word, rnd):
    w = list(word.lower())
    vow = [i for i, c in enumerate(w) if c in "aeiou"]
    for i in rnd.sample(vow, min(2, len(vow))):
        w[i] = rnd.choice([c for c in "aeiou" if c != w[i]])
    cons = [i for i, c in enumerate(w) if i > 0 and c not in "aeiou"]
    if cons and rnd.random() < .5:
        i = rnd.choice(cons)
        for a, b in (("k", "g"), ("t", "d"), ("p", "b"), ("f", "v"), ("s", "z"), ("m", "n")):
            if w[i] in (a, b):
                w[i] = b if w[i] == a else a
                break
    elif cons and w[cons[0]] in "hsr":
        del w[cons[0]]
    return "".join(w)


def test_synthetic_evaluation_no_uncertain_candidate_is_ever_chosen_automatically():
    names, cands = _synthetic_roster()
    rnd = random.Random(99)
    tot = found = auto_wrong = auto_any = 0
    for name in rnd.sample(names, 300):
        first, rest = name.split(" ", 1)
        q = f"{_mutate(first, rnd)} {rest}"
        if q.lower() == name.lower():
            continue
        tot += 1
        d, auto, ranked = _decide(q, cands)
        found += name in [c.label for c, _k, _s in ranked]
        auto_any += d == "auto"
        auto_wrong += d == "auto" and auto != name
    assert tot > 200
    assert auto_wrong == 0                                                               # THE target: an uncertain sound-alike is never chosen for the user
    assert auto_any == 0                                                                  # nor is a mutated name chosen automatically at all
    assert found / tot >= 0.6                                                             # recall floor for this synthetic set (reported separately from precision)


def test_synthetic_nonexistent_names_are_never_selected_and_mostly_find_nothing():
    names, cands = _synthetic_roster()
    existing = {n.lower() for n in names}
    rnd = random.Random(5)
    tot = cards = auto = 0
    for _ in range(400):
        q = f"{rnd.choice(_FIRST)} {rnd.choice(_LAST)}"
        if q.lower() in existing:
            continue
        tot += 1
        d, _a, _r = _decide(q, cands)
        cards += d == "cards"
        auto += d == "auto"
    assert tot > 30 and auto == 0
