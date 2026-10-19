"""Personal Assistant — confirmation flow, media kit sharing to a project's casting group, and several different commands in
one message (2026-10-19 brief).

Same harness as test_personal_assistant.py (real FastAPI app + real local dev Mongo, throwaway Qzx rows). NOTHING is sent to
WhatsApp: a send/share only creates `whatsapp_jobs` for a worker process that is not running in tests, and delivery history used
for the "can this worker reach that group" checks is written by the tests themselves. Real delivery is NOT exercised here.

  1  identity vs approval: the picked card keeps its exact id, the action approval stays, cancel/retry never mutate or duplicate
  2  media kit: link reuse, contents, unavailable media, project group routing, ambiguity, worker/group holds, approval binding
  3  several commands at once: typed actions, entities that never leak, dependency order, partial failure/retry, cancel, invalidation"""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import uuid

import pytest
import pytest_asyncio

from core import _now, db
from personal_assistant.parser import parse_message
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _add_rows, _isolate_each_test, _jobs, _project, _stage, _talent, alice, anon, bob, cleanup, say, world,
    _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

GROUP_A = "ZZZ MK GROUP A"
GROUP_B = "ZZZ MK GROUP B"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def m(world):
    r = {}
    r["angela"] = await _talent("Qzx Angela Kumar", media=[
        {"id": "m1", "category": "indian", "url": "https://res.cloudinary.com/zzz/image/upload/v1/a1.jpg", "type": "image"},
        {"id": "m2", "category": "western", "url": "https://res.cloudinary.com/zzz/image/upload/v1/a2.jpg", "type": "image"},
        {"id": "m3", "category": "video", "url": "https://res.cloudinary.com/zzz/video/upload/v1/a3.mp4", "type": "video"}])
    r["jasmine"] = await _talent("Qzx Jasmine Singh")
    r["vikram"] = await _talent("Qzx Vikram Sharma")
    r["soumya"] = await _talent("Qzx Soumya Shukla")
    r["soumya2"] = await _talent("Qzx Soumya Shukla", phone="911234500077", age=31)
    r["bare"] = await _talent("Qzx Bare Bones")                                   # no photos / video
    r["amazon"] = await _project("Qzx Amazonia Now", whatsapp_casting_group_name=GROUP_A)
    r["embassy"] = await _project("Qzx Embassy Onex", whatsapp_casting_group_name=GROUP_B)
    r["nogroup"] = await _project("Qzx Silent Studio")
    r["twin1"] = await _project("Qzx Twinbrand Alpha", whatsapp_casting_group_name="ZZZ MK TWIN GROUP")
    r["twin2"] = await _project("Qzx Twinbrand Beta", whatsapp_casting_group_name="ZZZ MK TWIN GROUP")
    r["cc"] = world["cc"]
    r["w1"], r["w2"] = world["w1"], world["w2"]
    return r


async def _task(anon, who, tid):
    return (await anon.get(f"{BASE}/tasks/{tid}", headers=who["headers"])).json()["task"]


async def confirm(client, who, task_id, worker=None, expect=200, ack=True):
    """Confirm one task. `ack` = the user ticked "send anyway" for a group this worker has never been seen sending to."""
    body = {"worker_id": worker} if worker else {}
    if ack:
        body["ack_unverified"] = True
    r = await client.post(f"{BASE}/tasks/{task_id}/confirm", headers=who["headers"], json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def _approve(anon, who, out, worker=None, expect=200, fingerprints="auto", ack=True):
    fps = {t["id"]: t["fingerprint"] for t in out["tasks"]} if fingerprints == "auto" else fingerprints
    body = {"worker_id": worker} if worker else {}
    if ack:
        body["ack_unverified"] = True
    if fps is not None:
        body["fingerprints"] = fps
    r = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=who["headers"], json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def _group_jobs(group):
    return await db.whatsapp_jobs.find({"destination_type": "group", "destination": group}, {"_id": 0}).to_list(50)


async def _history_job(worker_id, group, status, error=None):
    jid = uuid.uuid4().hex
    await db.whatsapp_jobs.insert_one({"id": jid, "batch_id": "zzz-mk-hist", "worker_id": worker_id, "destination_type": "group", "destination": group, "status": status,
                                       "error_message": error, "talent_name": "Qzx History", "created_at": _now(), "is_dry_run": False, "message_body": "old"})
    return jid


_MY_GROUPS = [GROUP_A, GROUP_B, "ZZZ MK TWIN GROUP", "ZZZ MK MOVED GROUP", "ZZZ MK CHANGED", "ZZZ MK FIZZY GROUP"]


async def _wipe_mine():
    ids = [j["batch_id"] for j in await db.whatsapp_jobs.find({"destination": {"$in": _MY_GROUPS}}, {"batch_id": 1}).to_list(500)]
    await db.whatsapp_jobs.delete_many({"$or": [{"destination": {"$in": _MY_GROUPS}}, {"batch_id": "zzz-mk-hist"}]})
    await db.whatsapp_batches.delete_many({"id": {"$in": ids}})


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _wipe_history():
    await _wipe_mine()
    yield
    await _wipe_mine()
    await db.talents.update_many({"id": {"$regex": "^zzz-test-pa-t-"}}, {"$unset": {"media_download_enabled": ""}})


# ============================================================ 1. identity vs approval ============================================================
async def test_picking_a_candidate_keeps_that_exact_id_and_the_move_still_needs_one_approval(anon, alice, m):
    await _add_rows(m["amazon"], m["soumya"], m["soumya2"], stage="ask_to_test")
    t = (await say(anon, alice, "Move Soumya Shukla to Approved in Qzx Amazonia Now"))["tasks"][0]
    assert t["status"] == "needs_clarification"                                   # "Who do you mean?" — identity only
    assert {o["id"] for o in t["slots"][0]["options"]} == {m["soumya"], m["soumya2"]}
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": m["soumya2"]})
    picked = r.json()["task"]
    assert picked["id"] == t["id"] and picked["status"] == "awaiting_confirmation"      # SAME task: one card changes meaning, no new one appears
    assert picked["params"]["talents"][0]["id"] == m["soumya2"] and picked["params"]["talents"][0]["state"] == "resolved"
    assert await _stage(m["amazon"], m["soumya2"]) == "ask_to_test" and await _stage(m["amazon"], m["soumya"]) == "ask_to_test"   # picking mutated nothing
    done = (await confirm(anon, alice, t["id"]))["task"]
    assert done["status"] == "completed"
    assert await _stage(m["amazon"], m["soumya2"]) == "approved" and await _stage(m["amazon"], m["soumya"]) == "ask_to_test"    # only the picked person moved


async def test_cancel_after_the_pick_changes_nothing_and_a_second_confirm_never_repeats_the_move(anon, alice, m):
    await _add_rows(m["amazon"], m["soumya"], m["soumya2"], stage="ask_to_test")
    t = (await say(anon, alice, "Move Soumya Shukla to Approved in Qzx Amazonia Now"))["tasks"][0]
    await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": m["soumya"]})
    c = await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
    assert c.json()["task"]["status"] == "cancelled" and await _stage(m["amazon"], m["soumya"]) == "ask_to_test"
    await confirm(anon, alice, t["id"])                                            # cancelled: confirming it is a no-op, not a revival
    assert (await _task(anon, alice, t["id"]))["status"] == "cancelled" and await _stage(m["amazon"], m["soumya"]) == "ask_to_test"
    u = (await say(anon, alice, "Move Qzx Soumya Shukla to Approved in Qzx Amazonia Now", ))["tasks"][0]
    await anon.post(f"{BASE}/tasks/{u['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "choice_id": m["soumya"]})
    first = (await confirm(anon, alice, u["id"]))["task"]
    again = (await confirm(anon, alice, u["id"]))["task"]
    assert first["status"] == again["status"] == "completed" and first["completed_at"] == again["completed_at"]       # the second click returned the finished task


# ================================================================ 2. media kit ================================================================
async def test_media_kit_wording_parses_like_media_link():
    for text in ("Show me Angela Kumar's media link", "Show me Angela Kumar's media kit", "show angela kumar media kit"):
        s = parse_message(text)[0]
        assert (s.intent, s.lookup) == ("lookup", "media_link"), text
    s = parse_message("Send Angela Kumar's media kit to Embassy One")[0]
    assert (s.intent, s.lookup, s.talents) == ("share", "media_link", ["Angela Kumar"])
    assert s.recipient["value"] == "Embassy One"
    segs = parse_message("Show me Angela Kumar's media kit and send it to Embassy One")
    assert [x.intent for x in segs] == ["lookup", "share"] and segs[1].talent_ref == "previous" and segs[1].recipient["value"] == "Embassy One"
    assert parse_message("send it to Embassy One")[0].intent == "unknown"          # nothing to refer to → not guessed


async def test_media_kit_lookup_shows_the_one_existing_link_with_contents_and_never_a_storage_url(anon, alice, m):
    c = (await say(anon, alice, "Show me Qzx Angela Kumar's media kit"))["tasks"][0]["result"]["cards"][0]
    assert c["media_link_available"] and "/talent-media/" in c["media_link"]
    assert c["media_contents"] == {"indian": 1, "western": 1, "portfolio": 0, "video": True, "total": 3}
    assert "cloudinary" not in str(c["media_link"]) and c["media_link_reason"] is None
    again = (await say(anon, alice, "Show me Qzx Angela Kumar's media link"))["tasks"][0]["result"]["cards"][0]
    assert again["media_link"] == c["media_link"]                                    # the same link — not a second record
    assert await db.talents.count_documents({"id": m["angela"], "media_download_token": {"$exists": True}}) == 1


async def test_media_kit_lookup_is_honest_when_the_link_is_off_or_the_profile_has_no_media(anon, alice, m):
    off = await _talent("Qzx Offline Olivia", media_download_enabled=False)
    c = (await say(anon, alice, "Show me Qzx Offline Olivia's media kit"))["tasks"][0]["result"]["cards"][0]
    assert c["media_link"] is None and "switched off" in c["media_link_reason"]
    b = (await say(anon, alice, "Show me Qzx Bare Bones's media kit"))["tasks"][0]["result"]["cards"][0]
    assert b["media_link"] and b["media_contents"]["total"] == 0 and "no photos or video" in b["media_link_reason"]
    assert off


async def test_sending_a_media_link_to_a_project_previews_the_exact_group_and_sends_nothing_until_approved(anon, alice, m):
    out = await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now")
    t = out["tasks"][0]
    assert t["kind"] == "share" and t["status"] == "awaiting_confirmation"
    p = t["params"]
    assert p["talents"][0]["id"] == m["angela"] and p["projects"][0]["id"] == m["amazon"] and p["recipient"]["kind"] == "project_group"
    assert t["preview"]["group"] == GROUP_A
    blob = "\n".join(t["summary"])
    assert GROUP_A in blob and "Qzx Angela Kumar" in blob and "Qzx Amazonia Now" in blob and "media link" in blob
    assert await _group_jobs(GROUP_A) == []                                          # preview only — nothing queued
    done = (await confirm(anon, alice, t["id"], worker=m["w1"]))["task"]
    assert done["status"] == "queued" and done["params"]["worker_id"] == m["w1"]
    jobs = await _group_jobs(GROUP_A)
    assert len(jobs) == 1 and jobs[0]["worker_id"] == m["w1"] and "/talent-media/" in jobs[0]["message_body"]
    assert await _group_jobs(GROUP_B) == []                                          # nowhere else


async def test_media_kit_wording_to_a_project_resolves_the_same_way(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media kit to Qzx Embassy Onex"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and t["preview"]["group"] == GROUP_B and t["params"]["projects"][0]["id"] == m["embassy"]


async def test_show_the_kit_and_send_it_is_a_lookup_plus_a_separate_approved_share(anon, alice, m):
    out = await say(anon, alice, "Show me Qzx Angela Kumar's media kit and send it to Qzx Embassy Onex")
    look, share = out["tasks"]
    assert look["kind"] == "lookup" and look["status"] == "completed" and look["result"]["cards"][0]["media_link"]
    assert share["kind"] == "share" and share["status"] == "awaiting_confirmation" and share["params"]["talents"][0]["id"] == m["angela"]
    assert share["preview"]["group"] == GROUP_B and await _group_jobs(GROUP_B) == []


async def test_an_ambiguous_project_is_a_card_to_tap_never_a_guess(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Twinbrand"))["tasks"][0]
    assert t["status"] == "needs_clarification"
    slot = t["slots"][0]
    assert {o["id"] for o in slot["options"]} >= {f"project:{m['twin1']}", f"project:{m['twin2']}"}
    assert await _group_jobs("ZZZ MK TWIN GROUP") == []
    r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": slot["key"], "choice_id": f"project:{m['twin2']}"})
    picked = r.json()["task"]
    assert picked["status"] == "awaiting_confirmation" and picked["params"]["projects"][0]["id"] == m["twin2"]
    assert any("also the casting group of 1 other project" in w for w in picked["warnings"])       # shared chat is disclosed


async def test_a_project_without_a_saved_group_is_explained_and_nothing_is_invented(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Silent Studio"))["tasks"][0]
    assert t["status"] == "failed" and "no WhatsApp casting group" in t["error"]
    assert await db.whatsapp_jobs.count_documents({"talent_name": {"$regex": "^Qzx Silent"}}) == 0


async def test_a_worker_that_could_not_find_this_group_before_is_held_and_another_worker_still_works(anon, alice, m):
    await _history_job(m["w1"], GROUP_A, "failed", "WhatsApp group 'ZZZ MK GROUP A' not found in chat list")
    await _history_job(m["w2"], GROUP_A, "sent")
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    acc = t["preview"]["worker_access"]
    assert acc[m["w1"]]["state"] == "failed_before" and acc[m["w2"]]["state"] == "verified"
    assert any("sending is held" in line for line in t["summary"])
    r = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"], "fingerprint": t["fingerprint"], "ack_unverified": True})
    assert r.status_code == 409 and "could not find the group" in r.text       # a known failure is held even if the user ticks "send anyway"
    assert (await _task(anon, alice, t["id"]))["status"] == "awaiting_confirmation" and len(await _group_jobs(GROUP_A)) == 2      # only the two seeded history rows
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w2"], "fingerprint": t["fingerprint"]})
    assert ok.status_code == 200 and ok.json()["task"]["status"] == "queued"


async def test_a_group_nobody_has_reached_is_flagged_unverified_but_still_needs_the_users_approval(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and all(a["state"] == "unknown" for a in t["preview"]["worker_access"].values())
    assert any("can't confirm" in w for w in t["warnings"])


async def test_changing_the_groups_name_after_the_preview_invalidates_the_approval(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Embassy Onex"))["tasks"][0]
    await db.projects.update_one({"id": m["embassy"]}, {"$set": {"whatsapp_casting_group_name": "ZZZ MK MOVED GROUP"}})
    try:
        r = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"], "fingerprint": t["fingerprint"], "ack_unverified": True})
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["changed"] and d["tasks"][0]["preview"]["group"] == "ZZZ MK MOVED GROUP" and d["tasks"][0]["fingerprint"] != t["fingerprint"]
        assert await db.whatsapp_jobs.count_documents({"destination": {"$in": [GROUP_B, "ZZZ MK MOVED GROUP"]}}) == 0         # nothing went anywhere
        again = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"], "fingerprint": d["tasks"][0]["fingerprint"], "ack_unverified": True})
        assert again.status_code == 200 and (await _group_jobs("ZZZ MK MOVED GROUP"))[0]["worker_id"] == m["w1"]
    finally:
        await db.projects.update_one({"id": m["embassy"]}, {"$set": {"whatsapp_casting_group_name": GROUP_B}})


async def test_the_same_link_sent_to_the_same_group_twice_is_warned_about(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    await confirm(anon, alice, t["id"], worker=m["w1"])
    t2 = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    assert t2["duplicate_risk"] and any("twice" in w or "again would post" in w for w in t2["warnings"])
    assert t2["status"] == "awaiting_confirmation"                                    # a warning, never a silent block or a silent send


async def test_unavailable_media_is_never_sent(anon, alice, m):
    off = await _talent("Qzx Offline Olga", media_download_enabled=False)
    t = (await say(anon, alice, "Send Qzx Offline Olga's media link to Qzx Amazonia Now"))["tasks"][0]
    assert any("switched off" in w for w in t["warnings"])
    done = (await confirm(anon, alice, t["id"], worker=m["w1"]))["task"]
    assert done["status"] == "failed" and "Nothing shareable" in done["error"] and await _group_jobs(GROUP_A) == []
    assert off


async def test_sending_a_media_link_to_a_saved_contact_still_works_as_before(anon, alice, m):
    from bson import ObjectId
    res = await db.clients.insert_one({"name": "Qzx Contact Casey", "phone_number": "+919800000321", "type": "casting_director"})
    try:
        t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Contact Casey"))["tasks"][0]
        assert t["status"] == "awaiting_confirmation" and t["params"]["recipient"]["kind"] == "crm"
        assert "919800000321" not in str(t)                                           # the number is masked
    finally:
        await db.clients.delete_one({"_id": ObjectId(res.inserted_id)})


# ======================================================== 3. several commands in one message ========================================================
EXAMPLE = ("Send Qzx Angela Kumar's media link to Qzx Amazonia Now. Also, add Qzx Jasmine Singh to Qzx Amazonia Now and move her to Follow-up, "
           "and send the Qzx Casting Call for Qzx Embassy Onex to Qzx Vikram Sharma.")


async def test_the_brief_example_becomes_separate_typed_actions_that_keep_their_own_entities(anon, alice, m):
    out = await say(anon, alice, EXAMPLE)
    by = {}
    for t in out["tasks"]:
        by.setdefault(t["kind"], []).append(t)
    share, add, move = by["share"][0], by["add"][0], by["move"][0]
    send = by["send"][0]
    assert share["params"]["talents"][0]["id"] == m["angela"] and share["params"]["projects"][0]["id"] == m["amazon"] and share["preview"]["group"] == GROUP_A
    assert add["params"]["talents"][0]["id"] == m["jasmine"] and add["params"]["projects"][0]["id"] == m["amazon"]
    assert move["params"]["talents"][0]["id"] == m["jasmine"] and move["params"]["projects"][0]["id"] == m["amazon"] and move["params"]["stage"] == "follow_up"
    assert send["params"]["talents"][0]["id"] == m["vikram"] and [e["id"] for e in send["params"]["projects"]] == [m["embassy"]]
    assert len(send["params"]["talents"]) == 1 and share["params"]["talents"] != send["params"]["talents"]       # nobody leaks across commands
    assert move["depends_on"] == [add["id"]]                                                                       # the dependency is explicit
    assert not share["depends_on"]
    assert out["unrecognised"] == []


async def test_unsupported_clauses_are_surfaced_not_dropped_or_guessed(anon, alice, m):
    out = await say(anon, alice, "Move Qzx Jasmine Singh to Approved in Qzx Amazonia Now. Also book the studio for Friday.")
    assert [t["kind"] for t in out["tasks"]] == ["move"] and out["unrecognised"] and "book the studio" in out["unrecognised"][0]


async def test_a_pronoun_with_nothing_to_refer_to_is_not_guessed_from_another_command(anon, alice, m):
    out = await say(anon, alice, "Add Qzx Jasmine Singh to Qzx Amazonia Now. Send the Qzx Casting Call for Qzx Embassy Onex.")
    send = [t for t in out["tasks"] if t["kind"] == "send"]
    assert send and send[0]["status"] == "needs_clarification" and send[0]["params"]["talents"][0]["state"] != "resolved"     # does NOT inherit Jasmine


async def test_one_approval_runs_the_plan_in_dependency_order_and_reports_each_action_separately(anon, alice, m):
    out = await say(anon, alice, EXAMPLE)
    kinds = [t["kind"] for t in out["tasks"]]
    assert "share" in kinds and kinds.index("add") < kinds.index("move")
    res = await _approve(anon, alice, out, worker=m["w1"])
    got = {tid: r["task"] for tid, r in res["results"].items()}
    assert all("error" not in r for r in res["results"].values()), res
    by = {}
    for t in got.values():
        by.setdefault(t["kind"], []).append(t)
    assert all(t["status"] in ("completed", "queued") for t in got.values()), {t["kind"]: t["status"] for t in got.values()}
    assert await _stage(m["amazon"], m["jasmine"]) == "follow_up"                                  # add ran before the move
    assert len(await _group_jobs(GROUP_A)) == 1 and "/talent-media/" in (await _group_jobs(GROUP_A))[0]["message_body"]
    vik = await db.whatsapp_jobs.find({"talent_id": m["vikram"]}, {"_id": 0}).to_list(5)
    assert len(vik) == 1 and vik[0]["destination_type"] == "number"                                # to the talent — never to a group inferred from the talent's project
    assert await _group_jobs(GROUP_B) == []


async def test_an_ambiguous_action_never_executes_while_the_others_do(anon, alice, m):
    await _add_rows(m["amazon"], m["soumya"], m["soumya2"], stage="ask_to_test")
    out = await say(anon, alice, "Move Qzx Soumya Shukla to Approved in Qzx Amazonia Now. Also send Qzx Angela Kumar's media link to Qzx Amazonia Now.")
    mv = out["tasks"][0]
    assert mv["status"] == "needs_clarification"
    res = await _approve(anon, alice, out, worker=m["w1"])
    assert (await _task(anon, alice, mv["id"]))["status"] == "needs_clarification"
    assert await _stage(m["amazon"], m["soumya"]) == "ask_to_test" == await _stage(m["amazon"], m["soumya2"])
    assert len(await _group_jobs(GROUP_A)) == 1 and res["results"]


async def test_a_failed_add_blocks_what_depends_on_it_and_a_retry_resumes_only_the_unfinished_steps(anon, alice, m):
    gone = await _talent("Qzx Fading Faye")
    out = await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now. Add Qzx Fading Faye to Qzx Amazonia Now and move her to Approved.")
    await db.talents.update_one({"id": gone}, {"$set": {"status": "archived"}})                    # becomes unavailable after the plan was shown
    res = await _approve(anon, alice, out, worker=m["w1"])
    by = {r["task"]["kind"]: r for r in res["results"].values()}
    assert by["add"]["task"]["status"] == "failed"
    assert by["move"]["error"] and (await _task(anon, alice, by["move"]["task"]["id"]))["status"] == "awaiting_confirmation"      # blocked, not run
    assert await _stage(m["amazon"], gone) is None and len(await _group_jobs(GROUP_A)) == 1          # the independent share DID run
    await db.talents.update_one({"id": gone}, {"$set": {"status": "ACTIVE"}})
    r = await anon.post(f"{BASE}/tasks/{by['add']['task']['id']}/retry", headers=alice["headers"])
    assert r.json()["task"]["status"] == "awaiting_confirmation"
    fresh = await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"command_id": out["command_id"]})
    again = {"command_id": out["command_id"], "tasks": fresh.json()["tasks"]}
    await _approve(anon, alice, again, worker=m["w1"])
    assert await _stage(m["amazon"], gone) == "approved"
    assert len(await _group_jobs(GROUP_A)) == 1                                                    # the finished share was NOT sent again


async def test_cancelling_a_plan_before_approval_runs_nothing_even_if_approved_afterwards(anon, alice, m):
    out = await say(anon, alice, EXAMPLE)
    r = await anon.post(f"{BASE}/commands/{out['command_id']}/cancel", headers=alice["headers"])
    assert r.status_code == 200 and r.json()["cancelled"] >= 4 and r.json()["still_running"] == 0
    assert all(t["status"] == "cancelled" for t in r.json()["tasks"])
    res = await _approve(anon, alice, out, worker=m["w1"], fingerprints=None)
    assert res["results"] == {}
    assert await _stage(m["amazon"], m["jasmine"]) is None and await _group_jobs(GROUP_A) == [] and await db.whatsapp_jobs.count_documents({"talent_id": m["vikram"]}) == 0


async def test_a_material_change_after_review_invalidates_the_whole_approval_and_runs_nothing(anon, alice, m):
    out = await say(anon, alice, EXAMPLE)
    await db.projects.update_one({"id": m["amazon"]}, {"$set": {"whatsapp_casting_group_name": "ZZZ MK CHANGED"}})
    try:
        r = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"],
                            json={"worker_id": m["w1"], "ack_unverified": True, "fingerprints": {t["id"]: t["fingerprint"] for t in out["tasks"]}})
        assert r.status_code == 409 and r.json()["detail"]["changed"]
        assert await _stage(m["amazon"], m["jasmine"]) is None and await db.whatsapp_jobs.count_documents({"destination": {"$in": [GROUP_A, "ZZZ MK CHANGED"]}}) == 0
        assert await db.whatsapp_jobs.count_documents({"talent_id": m["vikram"]}) == 0             # not even the unaffected actions ran (all-or-nothing)
        revised = r.json()["detail"]["tasks"][0]
        assert revised["preview"]["group"] == "ZZZ MK CHANGED"
    finally:
        await db.projects.update_one({"id": m["amazon"]}, {"$set": {"whatsapp_casting_group_name": GROUP_A}})


async def test_an_approval_for_a_task_the_user_never_saw_is_refused(anon, alice, m):
    out = await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now")
    r = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json={"worker_id": m["w1"], "ack_unverified": True, "fingerprints": {}})
    assert r.status_code == 409 and await _group_jobs(GROUP_A) == []


async def test_other_users_cannot_cancel_or_approve_my_plan(anon, alice, bob, m):
    out = await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now")
    assert (await anon.post(f"{BASE}/commands/{out['command_id']}/cancel", headers=bob["headers"])).status_code == 404
    assert (await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=bob["headers"], json={})).status_code == 404
    assert (await anon.post(f"{BASE}/commands/{out['command_id']}/cancel")).status_code in (401, 403)


# ----------------------------------------------------- destination evidence: unverified groups need an explicit acknowledgement
async def test_an_unverified_group_is_held_until_the_user_explicitly_acknowledges_it(anon, alice, m):
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    r = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"], "fingerprint": t["fingerprint"]})
    assert r.status_code == 409 and r.json()["detail"]["needs_ack"] is True and "hasn't been confirmed" in r.json()["detail"]["message"]
    assert (await _task(anon, alice, t["id"]))["status"] == "awaiting_confirmation" and await _group_jobs(GROUP_A) == []         # nothing queued
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"], "fingerprint": t["fingerprint"], "ack_unverified": True})
    assert ok.status_code == 200 and ok.json()["task"]["status"] == "queued" and len(await _group_jobs(GROUP_A)) == 1


async def test_a_group_a_worker_has_really_sent_to_needs_no_acknowledgement(anon, alice, m):
    await _history_job(m["w2"], GROUP_A, "sent")
    t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w2"], "fingerprint": t["fingerprint"]})
    assert ok.status_code == 200 and ok.json()["task"]["status"] == "queued"
    other = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Qzx Amazonia Now"))["tasks"][0]                         # w1 has no history: still needs the tick
    r = await anon.post(f"{BASE}/tasks/{other['id']}/confirm", headers=alice["headers"], json={"worker_id": m["w1"]})
    assert r.status_code == 409 and r.json()["detail"]["needs_ack"]


async def test_a_plan_with_an_unverified_group_is_stopped_before_any_step_runs(anon, alice, m):
    out = await say(anon, alice, EXAMPLE)
    res = await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"],
                          json={"worker_id": m["w1"], "fingerprints": {t["id"]: t["fingerprint"] for t in out["tasks"]}})
    assert res.status_code == 409 and res.json()["detail"]["needs_ack"]
    assert await _stage(m["amazon"], m["jasmine"]) is None and await _group_jobs(GROUP_A) == [] and await db.whatsapp_jobs.count_documents({"talent_id": m["vikram"]}) == 0


async def test_similarly_named_and_completed_projects_are_cards_and_a_closed_one_is_flagged(anon, alice, m, world):
    await db.projects.update_many({"id": {"$in": [world["pepsi"], world["old"]]}}, {"$set": {"whatsapp_casting_group_name": "ZZZ MK FIZZY GROUP"}})
    try:
        t = (await say(anon, alice, "Send Qzx Angela Kumar's media link to Fizzy Cola"))["tasks"][0]
        assert t["status"] == "needs_clarification"
        labels = {o["label"]: o for o in t["slots"][0]["options"]}
        assert {"Qzx Fizzy Cola", "Qzx Fizzy Cola Legacy"} <= set(labels)                                                          # both offered, nothing chosen for the user
        assert "omplete" in labels["Qzx Fizzy Cola Legacy"]["sublabel"] and "ngoing" in labels["Qzx Fizzy Cola"]["sublabel"]
        r = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": t["slots"][0]["key"], "choice_id": labels["Qzx Fizzy Cola Legacy"]["id"]})
        picked = r.json()["task"]
        assert picked["params"]["projects"][0]["id"] == world["old"] and any("complete, not ongoing" in w for w in picked["warnings"])
    finally:
        await db.projects.update_many({"id": {"$in": [world["pepsi"], world["old"]]}}, {"$unset": {"whatsapp_casting_group_name": ""}})
