"""Personal Assistant — entity resolution vs action approval, database-backed suggestions, and the combined
"add to the pipeline, then send" workflow (2026-10-19 brief).

Same harness as test_personal_assistant.py (real FastAPI app + real local dev Mongo, throwaway Qzx rows, NOTHING is sent:
a send only creates `whatsapp_jobs` for a worker process that is not running in tests).

  1  tapping a talent card resolves IDENTITY only; the action keeps exactly ONE approval
  2  misspelt / partial / duplicate names → real cards, best match flagged, wrong people never chosen silently
  3  projects: same, plus closed projects explained and several projects kept apart
  4  a talent missing from a pipeline → one combined plan (add, then send), per-project outcomes, safe retry
  5  stale / expired / outdated selections can't run"""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException

import test_personal_assistant as T
from core import _now, db
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, alice, anon, bob, cleanup, confirm, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def w2(world):
    r = {}
    r["viid"] = await _talent("Qzx Viidushi Tiwari")
    r["vkaul"] = await _talent("Qzx Vidushi Kaul")
    r["nihar"] = await _talent("Qzx Niharika Tiwari")
    r["kripa"] = await _talent("Qzx Kripa Trivedi")
    r["pillai"] = await _talent("Qzx Anaya Pillai")
    r["pillay"] = await _talent("Qzx Anaya Pillay")
    r["dup1"] = await _talent("Qzx Dupli Cate", age=24)
    r["dup2"] = await _talent("Qzx Dupli Cate", age=33, phone="911234500009")
    r["test"] = await _project("Qzx Sebamed - Testimonial film")
    r["trend"] = await _project("Qzx Sebamed - Trend film", whatsapp_casting_group_name="ZZZ TEST PA GROUP 2")
    r["skin"] = await _project("Qzx Sebamed - Skintaal 1 and 2")
    r["closed"] = await _project("Qzx Sebamed - Closed film", status="complete")
    return r


async def _get(anon, who, task_id):
    return (await anon.get(f"{BASE}/tasks/{task_id}", headers=who["headers"])).json()["task"]


async def _pick(anon, who, task, slot, choice):
    r = await anon.post(f"{BASE}/tasks/{task['id']}/clarify", headers=who["headers"], json={"slot": slot, "choice_id": choice})
    assert r.status_code == 200, r.text
    return r.json()


async def _type(anon, who, task, slot, text):
    r = await anon.post(f"{BASE}/tasks/{task['id']}/clarify", headers=who["headers"], json={"slot": slot, "text": text})
    assert r.status_code == 200, r.text
    return r.json()["task"]


async def _cancel_all(anon, who, *tasks):
    for t in tasks:
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=who["headers"])


# ======================================================================================= 1. identity vs approval
async def test_an_exact_single_talent_move_has_exactly_one_approval_and_never_runs_twice(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["pillai"])
    t = (await say(anon, alice, "Move Qzx Anaya Pillai to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and t["slots"] == []                    # nothing to pick: straight to the one approval
    assert await _stage(w2["trend"], w2["pillai"]) == "ask_to_test"                        # nothing happened before approval
    done = (await confirm(anon, alice, t["id"]))["task"]
    assert done["status"] == "completed" and await _stage(w2["trend"], w2["pillai"]) == "hold"
    again = (await confirm(anon, alice, t["id"]))["task"]                                  # a second approval is a no-op, not a second run
    assert again["status"] == "completed"


async def test_a_misspelt_talent_is_a_card_to_tap_and_the_tap_resolves_identity_without_a_second_question(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["viid"], w2["vkaul"], w2["nihar"])
    t = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    assert t["status"] == "needs_clarification" and [s["key"] for s in t["slots"]] == ["talent:0"]
    opts = t["slots"][0]["options"]
    assert opts[0]["id"] == w2["viid"] and opts[0].get("best") is True                      # the right person, proposed first…
    assert {o["id"] for o in opts} == {w2["viid"]}                                          # …and the lookalikes (Kaul, Niharika Tiwari) are NOT offered
    out = await _pick(anon, alice, t, "talent:0", w2["viid"])
    picked = out["task"]
    # identity is resolved; the project and destination stage from the ORIGINAL command are intact; ONE approval is left
    assert picked["status"] == "awaiting_confirmation" and picked["slots"] == []
    assert picked["params"]["talents"][0]["id"] == w2["viid"] and picked["params"]["talents"][0]["state"] == "resolved"
    assert [p["id"] for p in picked["params"]["projects"]] == [w2["trend"]] and picked["params"]["stage"] == "hold"
    assert any("Qzx Viidushi Tiwari" in l for l in picked["summary"]) and any("Hold" in l and "Qzx Sebamed - Trend film" in l for l in picked["summary"])
    assert await _stage(w2["trend"], w2["viid"]) == "ask_to_test"                           # the tap alone moved nothing
    done = (await confirm(anon, alice, picked["id"]))["task"]
    assert done["status"] == "completed" and await _stage(w2["trend"], w2["viid"]) == "hold"
    assert await _stage(w2["trend"], w2["vkaul"]) == "ask_to_test" and await _stage(w2["trend"], w2["nihar"]) == "ask_to_test"   # nobody else touched


async def test_canceling_the_final_approval_runs_nothing(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["viid"])
    t = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    picked = (await _pick(anon, alice, t, "talent:0", w2["viid"]))["task"]
    c = (await anon.post(f"{BASE}/tasks/{picked['id']}/cancel", headers=alice["headers"])).json()["task"]
    assert c["status"] == "cancelled" and await _stage(w2["trend"], w2["viid"]) == "ask_to_test"
    await confirm(anon, alice, picked["id"])                                               # approving a cancelled task does nothing
    assert await _stage(w2["trend"], w2["viid"]) == "ask_to_test"


async def test_several_talents_and_projects_stay_correctly_paired_while_each_question_is_answered_in_any_order(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["viid"], w2["kripa"])
    await _add_rows(w2["test"], w2["viid"], w2["kripa"])
    t = (await say(anon, alice, "Move Qzx Vidushi Tiwari and Qzx Kripa Trivedii to Hold in Qzx Sebamed Trendd film and Qzx Sebamed Testimonial film"))["tasks"][0]
    keys = {s["key"] for s in t["slots"]}
    assert keys == {"talent:0", "talent:1", "project:0"}, keys                           # only the unresolved ones are asked; the exact project is already settled
    assert [p["id"] for p in t["params"]["projects"]][1] == w2["test"]
    t = (await _pick(anon, alice, t, "talent:1", w2["kripa"]))["task"]                    # answer the SECOND talent first
    assert {s["key"] for s in t["slots"]} == {"talent:0", "project:0"} and t["status"] == "needs_clarification"
    assert t["params"]["talents"][0]["state"] != "resolved"                                # …which did not resolve the first one
    t = (await _pick(anon, alice, t, "project:0", w2["trend"]))["task"]
    t = (await _pick(anon, alice, t, "talent:0", w2["viid"]))["task"]
    assert t["status"] == "awaiting_confirmation"
    assert [e["id"] for e in t["params"]["talents"]] == [w2["viid"], w2["kripa"]]
    assert [e["id"] for e in t["params"]["projects"]] == [w2["trend"], w2["test"]]
    done = (await confirm(anon, alice, t["id"]))["task"]
    assert done["status"] == "completed"
    for pid in (w2["trend"], w2["test"]):
        for tid in (w2["viid"], w2["kripa"]):
            assert await _stage(pid, tid) == "hold"


async def test_two_pending_commands_never_share_or_overwrite_each_others_selection(anon, alice, world, w2):
    a = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    b = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Approved in Qzx Sebamed - Testimonial film"))["tasks"][0]
    await _add_rows(w2["trend"], w2["viid"])
    await _add_rows(w2["test"], w2["viid"])
    a2 = (await _pick(anon, alice, a, "talent:0", w2["viid"]))["task"]
    b_now = await _get(anon, alice, b["id"])
    assert b_now["status"] == "needs_clarification" and b_now["params"]["talents"][0]["state"] != "resolved"     # B did not inherit A's pick
    b2 = (await _pick(anon, alice, b_now, "talent:0", w2["viid"]))["task"]
    assert a2["params"]["stage"] == "hold" and b2["params"]["stage"] == "approved"
    assert [p["id"] for p in a2["params"]["projects"]] == [w2["trend"]] and [p["id"] for p in b2["params"]["projects"]] == [w2["test"]]
    await _cancel_all(anon, alice, a2, b2)


# ============================================================================================ 2. talent matching
async def test_exact_case_spacing_and_partial_names_resolve_without_a_pick_but_misspellings_never_do(anon, alice, world, w2):
    for phrase in ("Qzx Kripa Trivedi", "qzx   kripa  trivedi", "QZX KRIPA TRIVEDI", "Kripa Trivedi Qzx"):
        t = (await say(anon, alice, f"Show me {phrase}'s profile"))["tasks"][0]
        assert t["status"] == "completed" and t["result"]["cards"][0]["id"] == w2["kripa"], phrase
    for phrase in ("Qzx Kripa Trivedii", "Qzx Kriipa Trivedi", "Qzx Kipa Trivedi", "Qzx Krpa Trivdi"):            # extra / doubled / missing characters
        t = (await say(anon, alice, f"Show me {phrase}'s profile"))["tasks"][0]
        assert t["status"] == "needs_clarification", phrase
        assert t["slots"][0]["options"][0]["id"] == w2["kripa"], phrase
    part = (await say(anon, alice, "Show me Qzx Kripa's profile"))["tasks"][0]                              # a partial name that one person fits
    assert part["status"] == "completed" and part["result"]["cards"][0]["id"] == w2["kripa"]


async def test_confusable_names_are_never_collapsed_to_a_guess(anon, alice, world, w2):
    t = (await say(anon, alice, "Show me Qzx Anaya Pillae's profile"))["tasks"][0]
    opts = t["slots"][0]["options"]
    assert t["status"] == "needs_clarification" and {o["id"] for o in opts} == {w2["pillai"], w2["pillay"]}
    assert not any(o.get("best") for o in opts)                                         # two equally plausible people: nobody is pre-highlighted
    d = (await say(anon, alice, "Show me Qzx Dupli Cate's profile"))["tasks"][0]
    assert d["status"] == "needs_clarification" and {o["id"] for o in d["slots"][0]["options"]} == {w2["dup1"], w2["dup2"]}
    assert all(o["sublabel"] for o in d["slots"][0]["options"])                          # age / city etc. to tell duplicates apart
    for task in (t, d):
        await _cancel_all(anon, alice, task)


async def test_no_match_asks_again_and_a_typed_correction_returns_visible_cards_not_a_silent_guess(anon, alice, world, w2):
    t = (await say(anon, alice, "Show me Zqjxv Wplm's profile"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"][0]["options"] == [] and "spelling" in t["slots"][0]["prompt"]
    fixed = await _type(anon, alice, t, t["slots"][0]["key"], "Qzx Kripa Trivedii")                  # still misspelt → cards, not a resolve
    assert fixed["status"] == "needs_clarification" and fixed["slots"][0]["options"][0]["id"] == w2["kripa"] and fixed["slots"][0]["options"][0].get("best") is True
    done = (await _pick(anon, alice, fixed, fixed["slots"][0]["key"], w2["kripa"]))["task"]
    assert done["status"] == "completed" and done["result"]["cards"][0]["id"] == w2["kripa"]


async def test_the_browser_cannot_pick_something_the_server_did_not_offer(anon, alice, world, w2):
    t = (await say(anon, alice, "Show me Qzx Kripa Trivedii's profile"))["tasks"][0]
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": w2["vkaul"]})
    assert r.status_code == 400
    await _cancel_all(anon, alice, t)


async def test_voice_phrasing_with_the_talent_at_the_end_is_understood(anon, alice, world, w2):
    out = (await say(anon, alice, "send the Qzx Casting Call for Qzx Sebamed Testimonial film and Qzx Sebamed Trend film with Qzx Kriipa Trivedi"))["tasks"]
    send = out[-1]
    assert send["kind"] == "send" and send["params"]["template_id"] == world["cc"]
    assert [p["id"] for p in send["params"]["projects"]] == [w2["test"], w2["trend"]]
    assert send["params"]["talents"][0]["phrase"].lower() == "qzx kriipa trivedi" and send["status"] == "needs_clarification"
    assert send["slots"][0]["options"][0]["id"] == w2["kripa"]
    await _cancel_all(anon, alice, *out)


# ============================================================================================ 3. project matching
async def test_projects_exact_hyphen_case_and_precise_partial_resolve_but_spelling_corrections_are_cards(anon, alice, world, w2):
    for phrase in ("Qzx Sebamed - Trend film", "qzx sebamed trend film", "QZX SEBAMED - TREND FILM"):
        t = (await say(anon, alice, f"Add Qzx Kripa Trivedi to {phrase}"))["tasks"][0]
        assert t["status"] == "awaiting_confirmation" and [p["id"] for p in t["params"]["projects"]] == [w2["trend"]], phrase
        await _cancel_all(anon, alice, t)
    fuzzy = (await say(anon, alice, "Add Qzx Kripa Trivedi to Qzx Sibamed Trendd film"))["tasks"][0]            # a spelling correction on a project
    assert fuzzy["status"] == "needs_clarification" and fuzzy["slots"][0]["kind"] == "project"
    o = fuzzy["slots"][0]["options"]
    assert o[0]["id"] == w2["trend"] and o[0].get("best") is True and o[0]["status"] == "ongoing" and "Ongoing" in o[0]["sublabel"]
    done = (await _pick(anon, alice, fuzzy, "project:0", w2["trend"]))["task"]
    assert done["status"] == "awaiting_confirmation" and done["params"]["projects"][0]["id"] == w2["trend"]
    await _cancel_all(anon, alice, done)


async def test_similarly_named_projects_must_be_chosen_and_no_match_is_explained(anon, alice, world, w2):
    t = (await say(anon, alice, "Show everyone in Ask to Test for Qzx Sebamed"))["tasks"][0]
    opts = t["slots"][0]["options"]
    assert t["status"] == "needs_clarification" and {o["id"] for o in opts} >= {w2["test"], w2["trend"], w2["skin"], w2["closed"]}
    assert [o["status"] for o in opts][:3] == ["ongoing"] * 3 and opts[-1]["status"] != "ongoing"        # ongoing first, never silently preferred
    none = (await say(anon, alice, "Add Qzx Kripa Trivedi to Zqxw Nonexistent Film"))["tasks"][0]
    assert none["status"] == "needs_clarification" and none["slots"][0]["options"] == [] and "couldn't find a project" in none["slots"][0]["prompt"]
    await _cancel_all(anon, alice, t, none)


async def test_a_closed_project_is_explained_not_silently_replaced(anon, alice, world, w2):
    t = (await say(anon, alice, "Add Qzx Kripa Trivedi to Qzx Sebamed - Closed film"))["tasks"][0]
    assert t["status"] == "failed" and "Closed film is complete" in t["error"] and "ongoing" in t["error"]
    assert await _stage(w2["closed"], w2["kripa"]) is None


# ===================================================================== 4. missing pipeline membership → one plan
async def _plan(anon, alice, text):
    out = (await say(anon, alice, text))["tasks"]
    return out


async def test_talent_already_in_every_requested_pipeline_needs_no_extra_step(anon, alice, world, w2):
    await _add_rows(w2["test"], w2["kripa"])
    await _add_rows(w2["trend"], w2["kripa"])
    out = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Testimonial film and Qzx Sebamed - Trend film")
    assert [t["kind"] for t in out] == ["send"] and out[0]["depends_on"] == [] and out[0]["status"] == "awaiting_confirmation"
    await _cancel_all(anon, alice, *out)


async def test_missing_from_only_one_pipeline_adds_only_that_one(anon, alice, world, w2):
    await _add_rows(w2["test"], w2["kripa"])
    add, send = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Testimonial film and Qzx Sebamed - Trend film")
    assert [e["id"] for e in add["params"]["projects"]] == [w2["trend"]]                              # NOT the one she is already in, NOT any other project
    assert [m["project"] for m in add["params"]["missing"]] == ["Qzx Sebamed - Trend film"] and add["params"]["already"][0]["project"] == "Qzx Sebamed - Testimonial film"
    assert send["depends_on"] == [add["id"]] and send["params"]["strict_dependency"] is True
    assert any("already there" in l for l in add["summary"]) and any("Step 2 of 2" in l for l in send["summary"])
    await _cancel_all(anon, alice, add, send)
    assert await _stage(w2["skin"], w2["kripa"]) is None and await _stage(w2["trend"], w2["kripa"]) is None


async def test_one_approval_of_the_plan_adds_first_then_queues_the_send_and_a_repeat_creates_no_duplicates(anon, alice, world, w2):
    out = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Testimonial film and Qzx Sebamed - Trend film")
    add, send = out
    assert add["status"] == send["status"] == "awaiting_confirmation"
    cmd = add["command_id"]
    # send alone cannot run first: it waits for its prerequisite
    r = await anon.post(f"{BASE}/tasks/{send['id']}/confirm", headers=alice["headers"], json={"worker_id": world["w1"]})
    assert r.status_code == 409 and "Waiting for" in r.text and await _jobs([w2["kripa"]]) == []
    rs = await asyncio.gather(*[anon.post(f"{BASE}/commands/{cmd}/confirm-all", headers=alice["headers"], json={"worker_id": world["w1"]}) for _ in range(3)])
    assert all(r.status_code == 200 for r in rs)
    for pid in (w2["test"], w2["trend"]):
        assert await db.casting_pipeline.count_documents({"project_id": pid, "talent_id": w2["kripa"]}) == 1      # exactly one entry, however many approvals
    assert len(await _jobs([w2["kripa"]])) == 2                                                          # one message per project, once
    assert await db.whatsapp_batches.count_documents({"project_id": {"$in": [w2["test"], w2["trend"]]}, "template_id": world["cc"]}) == 2
    done_add = await _get(anon, alice, add["id"])
    assert done_add["status"] == "completed" and {o["status"] for o in done_add["outcomes"]} == {"added"}
    assert (await _get(anon, alice, send["id"]))["status"] in ("queued", "running", "completed")


async def test_a_partial_failure_shows_per_project_outcomes_blocks_the_send_and_a_retry_finishes_only_what_is_missing(anon, alice, world, w2, monkeypatch):
    import routers.casting_pipeline as cp
    real = cp.bulk_add_talents_to_projects

    async def flaky(payload, admin=None):
        if w2["trend"] in payload.project_ids:
            raise HTTPException(500, "simulated failure adding to Trend film")
        return await real(payload, admin=admin)

    monkeypatch.setattr(cp, "bulk_add_talents_to_projects", flaky)
    add, send = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Testimonial film and Qzx Sebamed - Trend film")
    res = (await anon.post(f"{BASE}/commands/{add['command_id']}/confirm-all", headers=alice["headers"], json={"worker_id": world["w1"]})).json()["results"]
    a = await _get(anon, alice, add["id"])
    assert a["status"] == "partial"                                                                       # NOT reported as success
    by = {o["project"]: o["status"] for o in a["outcomes"]}
    assert by == {"Qzx Sebamed - Testimonial film": "added", "Qzx Sebamed - Trend film": "failed"}
    assert "Trend film" in a["error"] and "retry" in a["error"].lower()
    assert "only partly worked" in res[send["id"]]["error"]                                               # the send was blocked, with the reason
    assert (await _get(anon, alice, send["id"]))["status"] == "awaiting_confirmation" and await _jobs([w2["kripa"]]) == []
    assert await db.casting_pipeline.count_documents({"project_id": w2["test"], "talent_id": w2["kripa"]}) == 1
    # recover: the completed project stays done, only the failed one is re-attempted
    monkeypatch.setattr(cp, "bulk_add_talents_to_projects", real)
    r = await anon.post(f"{BASE}/tasks/{add['id']}/retry", headers=alice["headers"])
    assert r.status_code == 200 and r.json()["task"]["status"] == "awaiting_confirmation"
    fixed = (await confirm(anon, alice, add["id"]))["task"]
    assert fixed["status"] == "completed" and {o["project"]: o["status"] for o in fixed["outcomes"]} == {"Qzx Sebamed - Testimonial film": "already_there", "Qzx Sebamed - Trend film": "added"}
    for pid in (w2["test"], w2["trend"]):
        assert await db.casting_pipeline.count_documents({"project_id": pid, "talent_id": w2["kripa"]}) == 1
    sent = (await confirm(anon, alice, send["id"], worker=world["w1"]))["task"]
    assert sent["status"] in ("queued", "running", "completed") and len(await _jobs([w2["kripa"]])) == 2


async def test_cancelling_the_plan_creates_nothing(anon, alice, world, w2):
    add, send = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Trend film")
    await _cancel_all(anon, alice, add, send)
    assert (await _get(anon, alice, add["id"]))["status"] == "cancelled" and (await _get(anon, alice, send["id"]))["status"] == "cancelled"
    await confirm(anon, alice, send["id"], worker=world["w1"])                                            # approving cancelled work does nothing
    await confirm(anon, alice, add["id"])
    assert await _stage(w2["trend"], w2["kripa"]) is None and await _jobs([w2["kripa"]]) == []


async def test_a_pick_in_the_middle_of_a_send_still_gets_its_add_step_planned(anon, alice, world, w2):
    t = (await say(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedii for Qzx Sebamed - Trend film"))["tasks"][0]
    assert t["status"] == "needs_clarification"
    out = await _pick(anon, alice, t, "talent:0", w2["kripa"])
    send, added = out["task"], out["added_tasks"][0]
    assert send["status"] == "awaiting_confirmation" and send["depends_on"] == [added["id"]]
    assert added["kind"] == "add" and [e["id"] for e in added["params"]["projects"]] == [w2["trend"]] and added["status"] == "awaiting_confirmation"
    listed = (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"command_id": send["command_id"]})).json()["tasks"]
    assert {x["kind"] for x in listed} == {"add", "send"}
    await _cancel_all(anon, alice, added, send)


async def test_a_talent_missing_from_a_closed_project_gets_a_plain_explanation_and_no_add(anon, alice, world, w2):
    out = await _plan(anon, alice, "Send the Qzx Casting Call to Qzx Kripa Trivedi for Qzx Sebamed - Closed film")
    assert [t["kind"] for t in out] == ["send"] and out[0]["status"] == "failed"
    assert "Closed film (complete)" in out[0]["error"] and "ongoing" in out[0]["error"]
    assert await _stage(w2["closed"], w2["kripa"]) is None and await _jobs([w2["kripa"]]) == []


async def test_a_message_that_already_says_add_then_send_does_not_plan_a_second_add(anon, alice, world, w2):
    out = (await say(anon, alice, "Add Qzx Kripa Trivedi to Qzx Sebamed - Trend film and send her the Qzx Casting Call"))["tasks"]
    assert [t["kind"] for t in out] == ["add", "send"] and out[1]["depends_on"] == [out[0]["id"]]
    assert not out[1]["params"].get("prereq")
    await _cancel_all(anon, alice, *out)


# =========================================================================== 5. stale / expired / outdated selections
async def test_a_request_that_waited_too_long_is_expired_and_cannot_be_clarified_or_confirmed(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["viid"])
    t = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    await db.assistant_tasks.update_one({"id": t["id"]}, {"$set": {"updated_at": old}})
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": w2["viid"]})
    assert r.status_code == 409 and "expired" in r.text
    assert (await _get(anon, alice, t["id"]))["status"] == "cancelled" and await _stage(w2["trend"], w2["viid"]) == "ask_to_test"
    await _add_rows(w2["trend"], w2["pillai"])
    t2 = (await say(anon, alice, "Move Qzx Anaya Pillai to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    assert t2["status"] == "awaiting_confirmation"
    await db.assistant_tasks.update_one({"id": t2["id"]}, {"$set": {"updated_at": old}})
    r = await anon.post(f"{BASE}/tasks/{t2['id']}/confirm", headers=alice["headers"], json={})
    assert r.status_code == 409 and "expired" in r.text and await _stage(w2["trend"], w2["pillai"]) == "ask_to_test"


async def test_a_talent_archived_between_the_tap_and_the_approval_is_not_acted_on(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["viid"])
    t = (await say(anon, alice, "Move Qzx Vidushi Tiwari to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    picked = (await _pick(anon, alice, t, "talent:0", w2["viid"]))["task"]
    await db.talents.update_one({"id": w2["viid"]}, {"$set": {"status": "ARCHIVED"}})
    try:
        done = (await confirm(anon, alice, picked["id"]))["task"]
    finally:
        await db.talents.update_one({"id": w2["viid"]}, {"$set": {"status": "ACTIVE"}})
    assert done["status"] == "failed" and "no longer available" in done["error"] and await _stage(w2["trend"], w2["viid"]) == "ask_to_test"


async def test_reopening_the_panel_does_not_rerun_a_completed_action(anon, alice, world, w2):
    await _add_rows(w2["trend"], w2["pillai"])
    t = (await say(anon, alice, "Move Qzx Anaya Pillai to Hold in Qzx Sebamed - Trend film"))["tasks"][0]
    await confirm(anon, alice, t["id"])
    await db.casting_pipeline.update_one({"project_id": w2["trend"], "talent_id": w2["pillai"]}, {"$set": {"stage": "approved"}})   # someone moves her on
    for _ in range(3):                                                                                     # panel reopened / tasks refreshed / double click
        await anon.get(f"{BASE}/tasks", headers=alice["headers"])
        await confirm(anon, alice, t["id"])
    assert await _stage(w2["trend"], w2["pillai"]) == "approved"                                          # the old Confirm did not run again
