"""Personal Assistant — database-aware name resolution (talents by spelling AND sound, projects, stages) and safe command validation
(2026-10-19 brief).

Same harness as test_personal_assistant.py (real FastAPI app + real local dev Mongo, throwaway Qzx rows). NOTHING is sent: a send/share
only creates `whatsapp_jobs` for a worker process that is not running in tests. The acceptance rule these tests pin down: a clear,
uniquely resolved name works smoothly; an uncertain or ambiguous one NEVER becomes a move / add / send against a guessed talent, project,
stage or destination.

  1  phonetic similarity (pure)         4  stages: exact synonyms vs resemblance      7  cache: ID + name only, refreshed, live-validated
  2  talents: exact / sounds-like / ...  5  speech noise: fillers and self-corrections  8  nothing is written before authorization
  3  projects: similar / completed       6  the brief's three-command example"""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import uuid

import pytest
import pytest_asyncio

from core import _now, db
from personal_assistant import phonetic, resolve as R
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, alice, anon, bob, cleanup, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

GROUP_X = "ZZZ NM GROUP X"
_MY_GROUPS = [GROUP_X]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def n(world):
    r = {}
    r["disha"] = await _talent("Qzx Disha Kumar")
    r["sahal"] = await _talent("Qzx Sahal Mansuri")
    r["sahil"] = await _talent("Qzx Sahil Mansuri")                                  # one letter from Sahal: both exist
    r["angela"] = await _talent("Qzx Angela Kumar")
    r["priya1"] = await _talent("Qzx Priya Nairr", age=24)
    r["priya2"] = await _talent("Qzx Priya Nairr", age=33, phone="911234500055")     # SAME name, different people
    r["dev1"] = await _talent("Qzx Devika Rao")
    r["dev2"] = await _talent("Qzx Divika Rao")                                      # sounds the same as the above
    r["toba"] = await _project("Qzx Tobamade Trend Film")
    r["seb_old"] = await _project("Qzx Sebamed Testimonial Film", status="complete")
    r["seb_new"] = await _project("Qzx Sebamed Testimonial Film Two")                 # contains the completed project's whole name
    r["only"] = await _project("Qzx Lonely Project")
    r["embassy"] = await _project("Qzx Embassy Onex", whatsapp_casting_group_name=GROUP_X)
    r["w1"] = world["w1"]
    return r


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh_state():
    R.invalidate_roster()
    await db.whatsapp_jobs.delete_many({"destination": {"$in": _MY_GROUPS}})
    yield
    R.invalidate_roster()
    await db.whatsapp_jobs.delete_many({"destination": {"$in": _MY_GROUPS}})
    await db.talents.update_many({"id": {"$regex": "^zzz-test-pa-t-"}}, {"$set": {"status": "ACTIVE"}})


async def _pipeline_rows():
    return await db.casting_pipeline.count_documents({"project_id": {"$regex": "^zzz-test-pa-p-"}})


async def _approve(anon, who, out, worker, expect=200):
    body = {"worker_id": worker, "ack_unverified": True, "fingerprints": {t["id"]: t["fingerprint"] for t in out["tasks"]}}
    r = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=who["headers"], json=body)
    assert r.status_code == expect, r.text
    return r.json()


# =============================================================== 1. phonetic similarity (pure)
def test_names_that_sound_alike_score_high_and_unrelated_ones_do_not():
    for a, b in (("Doha", "Disha"), ("Dhruv", "Druv"), ("Sahal", "Sahil"), ("Mansuri", "Mansoori"), ("Priya", "Preeya"), ("Zoya", "Soya"), ("Vidushi", "Viidushi")):
        assert phonetic.similarity(a, b) >= phonetic.PHON_MIN, (a, b)
    for a, b in (("Doha", "Mohan"), ("Angela", "Rivka"), ("Kumar", "Singh"), ("Tiwari", "Trivedi"), ("Ravi", "Rani")):
        assert phonetic.similarity(a, b) < phonetic.PHON_MIN, (a, b)


def test_the_first_sound_must_agree_and_very_short_words_never_match_by_sound():
    assert phonetic.similarity("Ravi", "Davi") == 0.0
    assert phonetic.similarity("Al", "El") == 0.0
    assert R.score_name("Qzx Doha Kumar", "Qzx Disha Kumar")[0] == "sounds_like"
    assert R.score_name("Qzx Angela Kumar", "Qzx Angela Kumar")[0] == "exact"
    assert R.score_name("Angela", "Angela Kumar")[0] == "partial"
    assert R.score_name("Kumar Disha", "Disha Kumar") is not None                      # word order is not part of a name's identity... but it still needs a tap
    assert R.score_name("Qzx Doha Singh", "Qzx Disha Kumar") is None                    # EVERY typed word must be accounted for


# ====================================================================== 2. talents
async def test_a_clear_unique_name_still_resolves_without_a_card(anon, alice, n):
    t = (await say(anon, alice, "Show me Qzx Angela Kumar's profile"))["tasks"][0]
    assert t["status"] == "completed" and t["params"]["talents"][0]["id"] == n["angela"]


async def test_an_exact_name_wins_over_a_similar_sounding_one(anon, alice, n):
    r = await R.resolve_talent("Qzx Sahal Mansuri")
    assert r["state"] == "resolved" and r["id"] == n["sahal"]                          # Sahil exists, but the typed name IS Sahal


async def test_a_mis_heard_name_becomes_cards_never_a_chosen_person_and_nothing_moves(anon, alice, n):
    await _add_rows(n["toba"], n["disha"], stage="ask_to_test")
    out = await say(anon, alice, "Move Qzx Doha Kumar to Approved in Qzx Tobamade Trend Film")
    t = out["tasks"][0]
    assert t["status"] == "needs_clarification" and t["params"]["talents"][0]["state"] == "ambiguous"
    opt = {o["id"]: o for o in t["slots"][0]["options"]}
    assert n["disha"] in opt and opt[n["disha"]]["match"] == "sounds_like" and not opt[n["disha"]].get("best")      # offered, never flagged as the answer
    assert await _stage(n["toba"], n["disha"]) == "ask_to_test"                                                   # nothing happened
    # an id that was not offered is refused
    bad = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": n["angela"]})
    assert bad.status_code == 400 and await _stage(n["toba"], n["disha"]) == "ask_to_test"
    # picking identifies the person; the move still waits for its own approval
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": n["disha"]})
    picked = ok.json()["task"]
    assert picked["status"] == "awaiting_confirmation" and picked["params"]["talents"][0]["id"] == n["disha"]
    assert await _stage(n["toba"], n["disha"]) == "ask_to_test"
    done = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": picked["fingerprint"]})
    assert done.status_code == 200 and await _stage(n["toba"], n["disha"]) == "approved"


async def test_two_talents_that_both_sound_right_are_both_offered_and_none_is_flagged(anon, alice, n):
    r = await R.resolve_talent("Qzx Davika Rao")
    assert r["state"] == "ambiguous"
    ids = {o["id"] for o in r["options"]}
    assert {n["dev1"], n["dev2"]} <= ids and not any(o.get("best") for o in r["options"])


async def test_same_name_talents_are_cards_and_the_pipeline_context_only_orders_and_proposes(anon, alice, n):
    await _add_rows(n["toba"], n["priya2"], stage="shortlisted")
    plain = await R.resolve_talent("Qzx Priya Nairr")
    assert plain["state"] == "ambiguous" and len(plain["options"]) == 2                  # never auto-resolved
    ctx = await R.resolve_talent("Qzx Priya Nairr", [n["toba"]])
    assert ctx["state"] == "ambiguous" and ctx["options"][0]["id"] == n["priya2"]
    assert ctx["options"][0]["in_project"] and ctx["options"][0].get("best") and "Qzx Tobamade Trend Film" in ctx["options"][0]["sublabel"]
    assert not ctx["options"][1].get("best")
    t = (await say(anon, alice, "Move Qzx Priya Nairr to Approved in Qzx Tobamade Trend Film"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"][0]["options"][0]["id"] == n["priya2"]
    assert await _stage(n["toba"], n["priya2"]) == "shortlisted"


async def test_unknown_names_find_nothing_and_ask(anon, alice, n):
    assert (await R.resolve_talent("Qzx Zzyzx Qwertyuiop"))["state"] == "none"
    t = (await say(anon, alice, "Move Qzx Zzyzx Qwertyuiop to Approved in Qzx Tobamade Trend Film"))["tasks"][0]
    assert t["status"] == "needs_clarification" and "couldn't find anyone" in t["slots"][0]["prompt"]
    assert await _pipeline_rows() == 0


async def test_a_talent_archived_after_the_index_was_built_is_not_offered(anon, alice, n):
    gone = await _talent("Qzx Gaurav Tandon")
    R.invalidate_roster()
    assert (await R.resolve_talent("Qzx Gorav Tandon"))["state"] == "ambiguous"        # builds the index with Gaurav in it
    await db.talents.update_one({"id": gone}, {"$set": {"status": "ARCHIVED"}})
    assert (await R.resolve_talent("Qzx Gorav Tandon"))["state"] == "none"             # the cache is stale; the live record decides


async def test_a_talent_added_after_the_index_was_built_is_still_found(anon, alice, n):
    R.invalidate_roster()
    await R.resolve_talent("Qzx Angela Kumar")                                          # index built now
    fresh = await _talent("Qzx Fresher Faroukh")
    r = await R.resolve_talent("Qzx Fresher Faroukh")
    assert r["state"] == "resolved" and r["id"] == fresh


async def test_the_talents_stored_name_is_never_altered_by_a_misheard_spelling(anon, alice, n):
    before = (await db.talents.find_one({"id": n["disha"]}, {"_id": 0, "name": 1}))["name"]
    await say(anon, alice, "Show me Qzx Doha Kumar's profile")
    assert (await db.talents.find_one({"id": n["disha"]}, {"_id": 0, "name": 1}))["name"] == before
    assert await db.talents.count_documents({"name": {"$regex": "^Qzx Doha"}}) == 0     # nothing was created either


# ====================================================================== 3. projects
async def test_a_completed_project_whose_name_sits_inside_a_live_ones_is_never_chosen_silently(anon, alice, n):
    res = await R.resolve_projects("Qzx Sebamed Testimonial Film")
    assert res[0]["state"] == "ambiguous"
    opts = {o["id"]: o for o in res[0]["options"]}
    assert set(opts) == {n["seb_old"], n["seb_new"]} and opts[n["seb_new"]].get("best") and not opts[n["seb_old"]].get("best")
    assert res[0]["options"][0]["id"] == n["seb_new"]                                    # live first
    assert (await R.resolve_projects("Qzx Sebamed Testimonial Film Two"))[0]["id"] == n["seb_new"]      # the full live name is unambiguous


async def test_a_lone_exact_project_still_resolves_and_speech_that_splits_a_word_still_finds_it(anon, alice, n):
    assert (await R.resolve_projects("Qzx Lonely Project"))[0] == {"phrase": "Qzx Lonely Project", "state": "resolved", "id": n["only"], "label": "Qzx Lonely Project"}
    r = (await R.resolve_projects("Qzx Toba Made Trend Film"))[0]                        # "Toba Made" heard instead of "Tobamade"
    assert r["state"] == "resolved" and r["id"] == n["toba"]


async def test_a_similar_project_name_never_picks_a_whatsapp_group(anon, alice, n):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media kit to Qzx Embassy Onx"))["tasks"][0]                  # misheard "Onex"
    assert t["status"] == "needs_clarification" and "Qzx Embassy Onex" in str(t["slots"][0]["options"])
    assert t["params"].get("projects") in ([], None) or not any(p.get("state") == "resolved" for p in t["params"]["projects"])
    assert await db.whatsapp_jobs.count_documents({"destination": GROUP_X}) == 0


# ====================================================================== 4. stages
def test_exact_stage_synonyms_apply_and_resemblances_are_only_suggested():
    for text in ("follow-up", "Follow up", "follow_up", "Followup"):
        assert parse_message(f"Move Qzx Disha to {text} in Qzx Lonely Project")[0].stage == "follow_up", text
    assert parse_message("Move Qzx Disha to Approved in Qzx Lonely Project")[0].stage == "approved"
    for text, sug in (("Aproved", "approved"), ("Improved", "approved"), ("lockd", "locked")):
        s = parse_message(f"Move Qzx Disha to {text} in Qzx Lonely Project")[0]
        assert s.stage is None and sug in s.stage_suggest and s.stage_text, text
    s = parse_message("Move Qzx Disha to Banana in Qzx Lonely Project")[0]
    assert s.stage is None and s.stage_text == "Banana"


async def test_a_resembling_stage_is_a_card_and_nothing_moves(anon, alice, n):
    await _add_rows(n["only"], n["disha"], stage="ask_to_test")
    t = (await say(anon, alice, "Move Qzx Disha Kumar to Improved in Qzx Lonely Project"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"][0]["kind"] == "stage"
    assert t["slots"][0]["options"][0]["id"] == "approved" and t["slots"][0]["options"][0].get("best")
    assert await _stage(n["only"], n["disha"]) == "ask_to_test"
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "stage", "text": "improvd"})
    assert r.json()["task"]["status"] == "needs_clarification"                           # a typed resemblance is still only a suggestion
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "stage", "choice_id": "approved"})
    assert ok.json()["task"]["status"] == "awaiting_confirmation" and await _stage(n["only"], n["disha"]) == "ask_to_test"


# ====================================================================== 5. speech noise
def test_hesitations_are_ignored_but_a_self_correction_is_never_acted_on():
    s = parse_message("Um, move Qzx Disha Kumar, uh, to Approved in Qzx Lonely Project")
    assert [x.intent for x in s] == ["move"] and s[0].talents == ["Qzx Disha Kumar"]
    c = parse_message("Move Qzx Doha Kumar, no wait, Qzx Disha Kumar to Approved in Qzx Lonely Project")
    assert [x.intent for x in c] == ["unknown"] and c[0].correction
    c = parse_message("Add Qzx Sahal Mansuri to Qzx Lonely Project. Move Qzx Doha Kumar to Approved in Qzx Lonely Project. Sorry, I mean Qzx Disha Kumar.")
    assert [x.intent for x in c] == ["add", "unknown", "unknown"]                        # the add before the corrected clause is untouched
    assert parse_message("Add Umang Shah to Qzx Lonely Project")[0].talents == ["Umang Shah"]      # "um" inside a real name is not noise


async def test_a_corrected_command_is_reported_and_creates_no_task(anon, alice, n):
    out = await say(anon, alice, "Move Qzx Doha Kumar to Approved in Qzx Lonely Project, no wait, Qzx Disha Kumar")
    assert out["tasks"] == [] and "correction" in out["unrecognised"][0]
    assert await _pipeline_rows() == 0


# ====================================================================== 6. the brief's example
EXAMPLE = ("Add Qzx Sahal Mansuri to Qzx Tobamade Trend Film, move Qzx Doha Kumar to Approved in Qzx Sebamed Testimonial Film Two, "
           "and send Qzx Angela Kumar's media kit to Qzx Embassy Onex.")


async def test_three_commands_resolve_independently_and_an_ambiguous_one_never_runs(anon, alice, n):
    await _add_rows(n["seb_new"], n["disha"], stage="ask_to_test")
    out = await say(anon, alice, EXAMPLE)
    by = {t["kind"]: t for t in out["tasks"]}
    assert set(by) == {"add", "move", "share"}
    assert by["add"]["params"]["talents"][0]["id"] == n["sahal"] and by["add"]["params"]["projects"][0]["id"] == n["toba"]
    assert by["move"]["status"] == "needs_clarification" and by["move"]["params"]["talents"][0]["state"] == "ambiguous"       # Doha → cards
    assert by["move"]["params"]["projects"][0]["id"] == n["seb_new"] and by["move"]["params"]["stage"] == "approved"
    assert by["share"]["params"]["talents"][0]["id"] == n["angela"] and by["share"]["preview"]["group"] == GROUP_X
    ids = lambda t: [e.get("id") for e in t["params"]["talents"]]
    assert n["sahal"] not in ids(by["move"]) and n["sahal"] not in ids(by["share"]) and n["angela"] not in ids(by["add"])    # nothing leaks across clauses
    assert await _stage(n["seb_new"], n["disha"]) == "ask_to_test" and await _stage(n["toba"], n["sahal"]) is None            # nothing ran at planning time
    # approving the plan runs the resolved actions only; the ambiguous move is left waiting for its pick
    await _approve(anon, alice, out, n["w1"])
    assert await _stage(n["toba"], n["sahal"]) is not None                               # the add (fully resolved) ran
    assert await _stage(n["seb_new"], n["disha"]) == "ask_to_test"                      # the unresolved identity was never moved
    now = (await anon.get(f"{BASE}/tasks/{by['move']['id']}", headers=alice["headers"])).json()["task"]
    assert now["status"] == "needs_clarification"
    assert len(await db.whatsapp_jobs.find({"destination": GROUP_X, "destination_type": "group"}).to_list(10)) == 1


async def test_add_then_move_of_the_same_new_talent_keeps_its_order_and_a_failed_add_blocks_the_move(anon, alice, n):
    out = await say(anon, alice, "Add Qzx Sahal Mansuri to Qzx Lonely Project and move him to Approved")
    kinds = [t["kind"] for t in out["tasks"]]
    assert kinds == ["add", "move"] and out["tasks"][1]["depends_on"] == [out["tasks"][0]["id"]]
    await db.talents.update_one({"id": n["sahal"]}, {"$set": {"status": "ARCHIVED"}})          # becomes unavailable after the plan was shown
    res = await _approve(anon, alice, out, n["w1"])
    by = {r["task"]["kind"]: r for r in res["results"].values()}
    assert by["add"]["task"]["status"] == "failed" and by["move"].get("error")
    assert await _stage(n["only"], n["sahal"]) is None


# ====================================================================== 7. the name index
async def test_the_name_index_holds_ids_and_names_only_and_is_built_once_per_minute(anon, alice, n):
    R.invalidate_roster()
    await R.resolve_talent("Qzx Angela Kumar")
    at = R._roster["at"]
    for q in ("Qzx Doha Kumar", "Qzx Sahil Mansuri", "Qzx Davika Rao", "Qzx Priya Nairr"):
        await R.resolve_talent(q)
    assert R._roster["at"] == at                                                         # one load served every phrase
    sample = R._roster["names"][0]
    assert {k for k, v in vars(sample).items() if v is not None} == {"id", "label"}          # nothing but the id and the name is held
    blob = repr(R._roster["names"][:200]) + repr(list(R._roster["index"])[:200])
    assert "@" not in blob and "9112345" not in blob                                     # no e-mail, no phone number


async def test_resolving_names_requires_an_authenticated_allowed_user(anon, alice, n):
    assert (await anon.post(f"{BASE}/message", json={"text": "Move Qzx Doha Kumar to Approved in Qzx Lonely Project", "client_request_id": "x" * 10})).status_code in (401, 403)


# ====================================================================== 8. nothing is written before authorization
async def test_resolving_and_picking_write_nothing_and_a_picked_send_still_needs_approval(anon, alice, n):
    t = (await say(anon, alice, "Send Qzx Doha Kumar's media link to Qzx Embassy Onex"))["tasks"][0]
    assert t["status"] == "needs_clarification"
    await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": n["disha"]})
    assert await db.whatsapp_jobs.count_documents({"destination": GROUP_X}) == 0 and await _pipeline_rows() == 0
    again = (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]
    assert again["status"] == "awaiting_confirmation"
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
    assert await db.whatsapp_jobs.count_documents({"destination": GROUP_X}) == 0
