"""Personal Assistant — end to end through the real FastAPI app + real local dev Mongo (throwaway ZZZ_TEST_PA rows).

NOTHING is sent to WhatsApp: a "send" only creates `whatsapp_jobs` in the existing queue for a worker process that is
not running in tests; delivery states are then simulated by editing those job rows, exactly as the real worker would.
Covers: access control, privacy between users, resolution + ambiguity + typos, add/move/send/share through the existing
operations, dependency ordering, concurrency, idempotency, honest statuses, partial failure, cancel/retry, worker rules."""
import os

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL

import uuid

import httpx
import pytest
import pytest_asyncio

from core import _now, db, hash_password, make_token
from server import app
from test_casting_call_worker_select import _real_db_for_this_module, _worker  # noqa: F401  (throwaway workers + real-db isolation)

_aio = pytest.mark.asyncio(loop_scope="module")
pytestmark = _aio
BASE = "/api/personal-assistant"
TAG = "zzztestpa"
_created = {"talents": [], "projects": [], "users": []}


# ------------------------------------------------------------------------------------------------- fixtures
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def anon():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _user(role="admin", email=None):
    uid = f"zzz-test-pa-{role}-{uuid.uuid4().hex[:8]}"
    email = email or f"{uid}@example.com"
    await db.users.insert_one({"id": uid, "email": email, "name": "ZZZ_TEST_PA", "password_hash": hash_password("Password@123"), "role": role, "status": "active", "token_version": 0})
    _created["users"].append(uid)
    return {"id": uid, "email": email, "headers": {"Authorization": f"Bearer {make_token({'id': uid, 'email': email, 'role': role, 'tv': 0})}"}}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def alice():
    return await _user("admin")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def bob():
    return await _user("admin")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team():
    return await _user("team")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def cleanup():
    yield
    await db.talents.delete_many({"id": {"$in": _created["talents"]}})
    await db.projects.delete_many({"id": {"$in": _created["projects"]}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": _created["projects"]}})
    await db.whatsapp_jobs.delete_many({"talent_name": {"$regex": "^Qzx"}})
    await db.whatsapp_batches.delete_many({"project_id": {"$in": _created["projects"]}})
    owners = _created["users"]
    for coll in ("assistant_commands", "assistant_tasks", "assistant_messages"):
        await db[coll].delete_many({"owner_id": {"$in": owners}})
    await db.assistant_audit.delete_many({"user_id": {"$in": owners}})
    await db.users.delete_many({"id": {"$in": owners}})
    await db.whatsapp_templates.delete_many({"id": {"$regex": "^zzz-test-pa-"}})


async def _talent(name, phone="911234500000", **extra):
    tid = f"zzz-test-pa-t-{uuid.uuid4().hex[:8]}"
    doc = {"id": tid, "name": name, "email": f"{tid}@example.com", "phone": phone, "status": "ACTIVE", "age": 27, "height": "5'6\"",
           "location": [{"city": "Mumbai", "country": "India"}], "tags": [], "media": [], "instagram_handle": "qzx_" + tid[-4:], **extra}
    await db.talents.insert_one(dict(doc))
    _created["talents"].append(tid)
    return tid


async def _project(name, status="ongoing", **extra):
    pid = f"zzz-test-pa-p-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({"id": pid, "slug": pid, "brand_name": name, "status": status, "shoot_dates": "28th Oct", "budget_per_day": "50k",
                                  "created_at": _now(), "materials": [], **extra})
    _created["projects"].append(pid)
    return pid


async def _stage(pid, tid):
    r = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
    return r["stage"] if r else None


async def _template(name, body="Hi {{first_name}} — {{project_name}}", slug=None):
    tid = f"zzz-test-pa-tpl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_templates.insert_one({"id": tid, "name": name, "slug": slug or tid, "body_text": body, "variables": [], "media_type": "none", "is_custom": True,
                                            "created_at": _now(), "updated_at": _now()})
    return tid


async def say(client, who, text, ctx=None, rid=None):
    r = await client.post(f"{BASE}/message", headers=who["headers"], json={"text": text, "client_request_id": rid or uuid.uuid4().hex, "context": ctx or {}})
    assert r.status_code == 200, r.text
    return r.json()


async def confirm(client, who, task_id, worker=None, expect=200):
    r = await client.post(f"{BASE}/tasks/{task_id}/confirm", headers=who["headers"], json={"worker_id": worker} if worker else {})
    assert r.status_code == expect, r.text
    return r.json()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(anon):
    """Distinctive names so nothing in the shared dev DB can collide with them."""
    w = {}
    w["quorra"] = await _talent("Qzx Quorra Tessier")
    w["quorra2"] = await _talent("Qzx Quorra Tessier", phone="911234500001", age=31)       # same-name duplicate record
    w["rivka"] = await _talent("Qzx Rivka Zenith")
    w["nehara"] = await _talent("Qzx Nehara Zolt")
    w["nophone"] = await _talent("Qzx Nolan Nophone", phone="")
    w["amazon"] = await _project("Qzx Streaming Now", whatsapp_casting_group_name="ZZZ TEST PA GROUP")
    w["pepsi"] = await _project("Qzx Fizzy Cola")
    w["old"] = await _project("Qzx Fizzy Cola Legacy", status="complete")
    w["cc"] = await _template("Qzx Casting Call", slug=f"zzz_test_pa_cc_{uuid.uuid4().hex[:6]}")
    w["fu"] = await _template("Qzx Follow Up")
    w["w1"] = await _worker("ZZZ PA Worker 1")
    w["w2"] = await _worker("ZZZ PA Worker 2")
    w["wdown"] = await _worker("ZZZ PA Worker Down", status="disconnected")
    return w


# ---------------------------------------------------------------------------------------------------- access
async def test_access_rules_are_enforced_on_the_backend(anon, alice, team, monkeypatch):
    assert (await anon.get(f"{BASE}/access")).status_code in (401, 403)
    assert (await anon.post(f"{BASE}/message", json={"text": "hi there", "client_request_id": "x" * 10})).status_code in (401, 403)
    assert (await anon.get(f"{BASE}/access", headers=alice["headers"])).json() == {"allowed": True, "role": "admin"}
    assert (await anon.get(f"{BASE}/access", headers=team["headers"])).json()["allowed"] is False
    for method, path in (("get", "/tasks"), ("get", "/history"), ("get", "/workers")):
        assert (await getattr(anon, method)(BASE + path, headers=team["headers"])).status_code == 403
    assert (await anon.post(f"{BASE}/message", headers=team["headers"], json={"text": "show me qzx", "client_request_id": uuid.uuid4().hex})).status_code == 403
    # enabling another user needs only configuration, and does not share anything with the others
    monkeypatch.setenv("ASSISTANT_ALLOWED_USERS", team["email"].upper())
    assert (await anon.get(f"{BASE}/access", headers=team["headers"])).json()["allowed"] is True
    assert (await anon.get(f"{BASE}/tasks", headers=team["headers"])).json()["tasks"] == []
    # exclusive mode: only the listed account — admins included — may use it
    monkeypatch.setenv("ASSISTANT_RESTRICT_TO_ALLOWLIST", "true")
    assert (await anon.get(f"{BASE}/access", headers=alice["headers"])).json()["allowed"] is False
    assert (await anon.get(f"{BASE}/tasks", headers=alice["headers"])).status_code == 403
    assert (await anon.get(f"{BASE}/access", headers=team["headers"])).json()["allowed"] is True
    monkeypatch.delenv("ASSISTANT_RESTRICT_TO_ALLOWLIST")
    monkeypatch.setenv("PERSONAL_ASSISTANT_DISABLED", "true")
    assert (await anon.get(f"{BASE}/access", headers=alice["headers"])).json()["allowed"] is False
    assert (await anon.get(f"{BASE}/tasks", headers=alice["headers"])).status_code == 403


# --------------------------------------------------------------------------------------------------- lookups
async def test_profile_lookup_returns_real_data_without_private_contact_details(anon, alice, world):
    out = await say(anon, alice, "Show me Rivka Zenith's profile.")
    t = out["tasks"][0]
    assert t["kind"] == "lookup" and t["status"] == "completed"
    card = t["result"]["cards"][0]
    assert card["id"] == world["rivka"] and card["label"] == "Qzx Rivka Zenith" and card["profile_path"] == f"/admin/talents/{world['rivka']}"
    blob = str(t)
    assert "911234500000" not in blob and "@example.com" not in blob                       # no phone / e-mail in assistant output
    assert card["media_link"] is None and "media_download_token" not in (await db.talents.find_one({"id": world["rivka"]}, {"_id": 0}) or {})   # a plain profile view never writes (no link minted)


async def test_media_link_lookup_respects_the_existing_toggle(anon, alice, world):
    ok = (await say(anon, alice, "Show me Rivka Zenith's media link."))["tasks"][0]["result"]["cards"][0]
    assert ok["media_link_available"] is True and "/talent-media/" in ok["media_link"]
    await db.talents.update_one({"id": world["nehara"]}, {"$set": {"media_download_enabled": False}})
    off = (await say(anon, alice, "Show me Nehara Zolt's media link."))["tasks"][0]["result"]["cards"][0]
    assert off["media_link_available"] is False and off["media_link"] is None


async def test_history_and_tested_lookups_use_real_pipeline_rows(anon, alice, world):
    await db.casting_pipeline.insert_many([
        {"id": uuid.uuid4().hex, "project_id": world["old"], "talent_id": world["rivka"], "stage": "already_tested", "created_at": _now(), "updated_at": _now()},
        {"id": uuid.uuid4().hex, "project_id": world["pepsi"], "talent_id": world["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()}])
    c = (await say(anon, alice, "Which projects has Rivka Zenith been added to?"))["tasks"][0]["result"]["cards"][0]
    assert {p["project"] for p in c["history"]["projects"]} == {"Qzx Fizzy Cola Legacy", "Qzx Fizzy Cola"}
    t = (await say(anon, alice, "Has Rivka Zenith tested for any previous projects?"))["tasks"][0]["result"]["cards"][0]
    assert [x["project"] for x in t["tested_projects"]] == ["Qzx Fizzy Cola Legacy"]
    await db.casting_pipeline.delete_many({"talent_id": world["rivka"]})


# ------------------------------------------------------------------------------------------- ambiguity / typos
async def test_same_name_records_ask_which_one_with_photo_card_details_and_continue_after_the_pick(anon, alice, world):
    out = await say(anon, alice, "Show me Quorra Tessier's profile.")
    t = out["tasks"][0]
    assert t["status"] == "needs_clarification"
    slot = t["slots"][0]
    assert slot["kind"] == "talent" and {o["id"] for o in slot["options"]} == {world["quorra"], world["quorra2"]}
    assert all(o["label"] == "Qzx Quorra Tessier" and o["sublabel"] for o in slot["options"])        # identifying details to tell them apart
    bad = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": slot["key"], "choice_id": world["rivka"]})
    assert bad.status_code == 400                                                                    # only the offered options are accepted
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": slot["key"], "choice_id": world["quorra2"]})
    task = ok.json()["task"]
    assert task["status"] == "completed" and task["result"]["cards"][0]["id"] == world["quorra2"]    # resolved by ID, then ran


async def test_minor_spelling_mistakes_still_find_the_right_talent_and_nonsense_asks_again(anon, alice, world):
    t = (await say(anon, alice, "Show me Rivka Zenyth's profile"))["tasks"][0]
    assert t["status"] == "completed" and t["result"]["cards"][0]["id"] == world["rivka"]
    n = (await say(anon, alice, "Show me Zqjxv Wplm's profile"))["tasks"][0]
    assert n["status"] == "needs_clarification" and n["slots"][0]["options"] == []
    fixed = (await anon.post(f"{BASE}/tasks/{n['id']}/clarify", headers=alice["headers"], json={"slot": n["slots"][0]["key"], "text": "Rivka Zenith"})).json()["task"]
    assert fixed["status"] == "completed" and fixed["result"]["cards"][0]["id"] == world["rivka"]   # a correction continues the same task


async def test_project_names_resolve_across_several_words_and_ambiguity_is_offered(anon, alice, world):
    t = (await say(anon, alice, "Add Rivka Zenith to Streaming Now and Fizzy Cola"))["tasks"][0]
    assert [e["id"] for e in t["params"]["projects"]] == [world["amazon"], world["pepsi"]]         # "Fizzy Cola" prefers the ONGOING one
    assert t["status"] == "awaiting_confirmation"
    amb = (await say(anon, alice, "Show everyone in Ask to Test for Qzx Fizzy"))["tasks"][0]
    opts = amb["slots"][0]["options"]
    assert amb["status"] == "needs_clarification" and {o["id"] for o in opts} == {world["pepsi"], world["old"]}   # a read may match any status → ask
    picked = (await anon.post(f"{BASE}/tasks/{amb['id']}/clarify", headers=alice["headers"], json={"slot": "project:0", "choice_id": world["old"]})).json()["task"]
    assert picked["status"] == "completed" and picked["result"]["groups"][0]["project_id"] == world["old"]


# ---------------------------------------------------------------------------------------------- add / dedupe
async def test_add_multiple_talents_to_multiple_projects_waits_for_confirmation_then_writes_once(anon, alice, world):
    t = (await say(anon, alice, "Add Rivka Zenith and Nehara Zolt to Streaming Now and Fizzy Cola"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and await _stage(world["amazon"], world["rivka"]) is None       # nothing happens before OK
    done = (await confirm(anon, alice, t["id"]))["task"]
    assert done["status"] == "completed" and done["result"]["added"] == 4
    for p in (world["amazon"], world["pepsi"]):
        for tid in (world["rivka"], world["nehara"]):
            assert await _stage(p, tid) == "ask_to_test"
    again = (await confirm(anon, alice, t["id"]))["task"]                                           # double click / retry
    assert again["status"] == "completed"
    assert await db.casting_pipeline.count_documents({"project_id": world["amazon"], "talent_id": world["rivka"]}) == 1
    assert done["result"]["suggestions"][0]["label"].startswith("Send the")                        # offers the next step from real state
    await db.casting_pipeline.delete_many({"project_id": {"$in": [world["amazon"], world["pepsi"]]}})


async def test_adding_to_a_project_that_is_not_ongoing_fails_up_front_with_the_reason(anon, alice, world):
    t = (await say(anon, alice, "Add Rivka Zenith to Fizzy Cola Legacy"))["tasks"][0]
    assert t["status"] == "failed" and "complete" in t["error"].lower() and await _stage(world["old"], world["rivka"]) is None


async def test_selected_talents_and_current_project_come_from_context_and_are_validated(anon, alice, world):
    ctx = {"project_id": world["amazon"], "talent_ids": [world["rivka"], world["nehara"], "does-not-exist"]}
    t = (await say(anon, alice, "Add these selected talents to the current project.", ctx))["tasks"][0]
    assert [e["id"] for e in t["params"]["talents"]] == [world["rivka"], world["nehara"]]          # unknown id dropped, not trusted
    assert [e["id"] for e in t["params"]["projects"]] == [world["amazon"]]
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


# ------------------------------------------------------------------------------------------------- pipeline
async def test_move_uses_the_existing_stage_rules_and_reports_talents_not_in_the_project(anon, alice, world):
    await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": world["amazon"], "talent_id": world["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()})
    t = (await say(anon, alice, "Move Rivka Zenith and Nehara Zolt to Shortlisted in Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and t["params"]["stage"] == "shortlisted"
    assert any("Nehara" in w for w in t["warnings"])                                                # not in the pipeline → said before confirming
    done = (await confirm(anon, alice, t["id"]))["task"]
    assert done["status"] == "partial" and await _stage(world["amazon"], world["rivka"]) == "shortlisted"
    assert {o["name"]: o["status"] for o in done["outcomes"]} == {"Qzx Rivka Zenith": "moved", "Qzx Nehara Zolt": "not_in_project"}
    r = (await say(anon, alice, "Mark Rivka Zenith as reached out in Streaming Now"))["tasks"][0]
    assert (await confirm(anon, alice, r["id"]))["task"]["status"] == "completed" and await _stage(world["amazon"], world["rivka"]) == "follow_up"
    lst = (await say(anon, alice, "Show me everyone in Follow-up in Streaming Now"))["tasks"][0]["result"]
    assert lst["groups"][0]["count"] == 1 and lst["groups"][0]["talents"][0]["id"] == world["rivka"]
    bogus = (await say(anon, alice, "Move Rivka Zenith to Narnia in Streaming Now"))["tasks"][0]
    assert bogus["status"] == "needs_clarification" and bogus["slots"][0]["kind"] == "stage"       # never invents a stage
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})


# ---------------------------------------------------------------------------------------------------- sends
async def _add_rows(pid, *tids, stage="ask_to_test"):
    for tid in tids:
        await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": pid, "talent_id": tid, "stage": stage, "created_at": _now(), "updated_at": _now()})


async def _jobs(tids):
    return await db.whatsapp_jobs.find({"talent_id": {"$in": tids}}, {"_id": 0}).to_list(100)


async def test_send_needs_an_explicit_connected_worker_and_queues_only_the_selected_talents_for_this_project(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"], world["nehara"], world["nophone"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith, Nolan Nophone for Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and t["params"]["template_id"] == world["cc"]
    await confirm(anon, alice, t["id"], expect=400)                                                 # no worker chosen → never guessed
    await confirm(anon, alice, t["id"], worker=world["wdown"], expect=409)                          # disconnected → refused, not rerouted
    assert (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]["status"] == "awaiting_confirmation"
    done = (await confirm(anon, alice, t["id"], worker=world["w2"]))["task"]
    assert done["status"] == "queued" and done["params"]["worker_id"] == world["w2"] and len(done["batch_ids"]) == 1
    jobs = await _jobs([world["rivka"], world["nehara"], world["nophone"]])
    assert [j["talent_id"] for j in jobs] == [world["rivka"]]                                       # nehara not selected, nophone not sendable
    assert jobs[0]["worker_id"] == world["w2"] and "Qzx Streaming Now" in jobs[0]["message_body"]    # project-specific content
    assert done["not_queued"][0]["name"] == "Qzx Nolan Nophone" and done["not_queued"][0]["reason"]  # partial queue explained by name
    assert await _stage(world["amazon"], world["rivka"]) == "ask_to_test"                           # sending never moves a stage
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": world["rivka"]})


async def test_delivery_status_is_read_back_from_the_persisted_jobs_never_assumed(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"], world["nehara"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith and Nehara Zolt for Streaming Now"))["tasks"][0]
    done = (await confirm(anon, alice, t["id"], worker=world["w1"]))["task"]
    fetch = lambda: anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])
    assert (await fetch()).json()["task"]["status"] == "queued"                                      # jobs are pending: NOT "sent"
    jobs = await _jobs([world["rivka"], world["nehara"]])
    await db.whatsapp_jobs.update_one({"id": jobs[0]["id"]}, {"$set": {"status": "sent", "verification_status": "verified"}})
    mid = (await fetch()).json()["task"]
    assert mid["status"] == "running" and "1 sent" in mid["status_detail"]
    await db.whatsapp_jobs.update_one({"id": jobs[1]["id"]}, {"$set": {"status": "failed", "error_message": "number not on WhatsApp"}})
    end = (await fetch()).json()["task"]
    assert end["status"] == "partial" and {o["status"] for o in end["outcomes"]} == {"sent", "failed"}
    # retry re-queues ONLY the failed job (the sent one is never touched → no duplicate delivery)
    r = (await anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"])).json()["task"]
    jobs2 = {j["id"]: j for j in await _jobs([world["rivka"], world["nehara"]])}
    assert jobs2[jobs[0]["id"]]["status"] == "sent" and jobs2[jobs[1]["id"]]["status"] == "pending" and r["status"] in ("queued", "running")
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": {"$in": [world["rivka"], world["nehara"]]}})


async def test_cancel_works_only_while_every_message_is_still_pending(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"], world["nehara"])
    a = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]
    await confirm(anon, alice, a["id"], worker=world["w1"])
    c = (await anon.post(f"{BASE}/tasks/{a['id']}/cancel", headers=alice["headers"])).json()["task"]
    assert c["status"] == "cancelled" and {j["status"] for j in await _jobs([world["rivka"]])} == {"skipped"}
    b = (await say(anon, alice, "Send the Qzx Follow Up to Nehara Zolt for Streaming Now"))["tasks"][0]
    await confirm(anon, alice, b["id"], worker=world["w1"])
    j = (await _jobs([world["nehara"]]))[0]
    await db.whatsapp_jobs.update_one({"id": j["id"]}, {"$set": {"status": "sending"}})
    assert (await anon.post(f"{BASE}/tasks/{b['id']}/cancel", headers=alice["headers"])).status_code == 409   # already started: refused
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": {"$in": [world["rivka"], world["nehara"]]}})


# ------------------------------------------------------------------------- multi-command / dependencies
async def test_multi_command_message_becomes_ordered_tasks_and_a_dependent_send_waits_for_its_add(anon, alice, world):
    out = await say(anon, alice, "Add Rivka Zenith to Streaming Now and send them the Qzx Casting Call. Also add Nehara Zolt to Fizzy Cola and send her the Qzx Follow Up template.")
    kinds = [t["kind"] for t in out["tasks"]]
    assert kinds == ["add", "send", "add", "send"]
    add1, send1, add2, send2 = out["tasks"]
    assert send1["depends_on"] == [add1["id"]] and send2["depends_on"] == [add2["id"]]
    assert [e["id"] for e in send1["params"]["talents"]] == [world["rivka"]] and [e["id"] for e in send1["params"]["projects"]] == [world["amazon"]]
    r = await anon.post(f"{BASE}/tasks/{send1['id']}/confirm", headers=alice["headers"], json={"worker_id": world["w1"]})
    assert r.status_code == 409 and "Waiting for" in r.json()["detail"]                              # the send cannot jump ahead of the add
    assert await _jobs([world["rivka"]]) == []
    res = (await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json={"worker_id": world["w1"]})).json()["results"]
    assert res[add1["id"]]["task"]["status"] == "completed" and res[add2["id"]]["task"]["status"] == "completed"
    assert res[send1["id"]]["task"]["status"] == "queued" and res[send2["id"]]["task"]["status"] == "queued"
    assert await _stage(world["amazon"], world["rivka"]) == "ask_to_test" and await _stage(world["pepsi"], world["nehara"]) == "ask_to_test"
    assert len(await _jobs([world["rivka"]])) == 1 and len(await _jobs([world["nehara"]])) == 1
    await db.casting_pipeline.delete_many({"project_id": {"$in": [world["amazon"], world["pepsi"]]}})
    await db.whatsapp_jobs.delete_many({"talent_id": {"$in": [world["rivka"], world["nehara"]]}})


async def test_a_failed_dependency_blocks_its_dependent_instead_of_sending(anon, alice, world):
    out = await say(anon, alice, "Add Rivka Zenith to Streaming Now and send them the Qzx Casting Call")
    add, send = out["tasks"]
    await db.projects.update_one({"id": world["amazon"]}, {"$set": {"status": "hold"}})              # the project stops accepting talents before OK
    try:
        res = (await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json={"worker_id": world["w1"]})).json()["results"]
    finally:
        await db.projects.update_one({"id": world["amazon"]}, {"$set": {"status": "ongoing"}})
    assert res[add["id"]]["task"]["status"] == "failed"
    assert "failed" in res[send["id"]]["error"] and await _jobs([world["rivka"]]) == []


async def test_independent_tasks_confirm_together(anon, alice, world):
    out = await say(anon, alice, "Add Rivka Zenith to Streaming Now. Add Nehara Zolt to Fizzy Cola.")
    assert [t["depends_on"] for t in out["tasks"]] == [[], []]
    res = (await anon.post(f"{BASE}/commands/{out['command_id']}/confirm-all", headers=alice["headers"], json={})).json()["results"]
    assert {r["task"]["status"] for r in res.values()} == {"completed"}
    await db.casting_pipeline.delete_many({"project_id": {"$in": [world["amazon"], world["pepsi"]]}})


async def test_the_same_client_request_never_plans_twice(anon, alice, world):
    rid = uuid.uuid4().hex
    a = await say(anon, alice, "Add Rivka Zenith to Streaming Now", rid=rid)
    b = await say(anon, alice, "Add Rivka Zenith to Streaming Now", rid=rid)
    assert b["duplicate"] is True and [t["id"] for t in a["tasks"]] == [t["id"] for t in b["tasks"]]
    assert await db.assistant_tasks.count_documents({"command_id": a["command_id"]}) == 1


# ----------------------------------------------------------------------------------------------- share
async def test_share_media_link_to_a_number_goes_through_the_existing_queue_after_confirmation(anon, alice, world):
    await db.whatsapp_templates.update_one({"slug": "custom"}, {"$setOnInsert": {"id": "zzz-test-pa-custom", "name": "Custom Message", "slug": "custom", "body_text": "{{message}}", "variables": ["message"], "created_at": _now(), "updated_at": _now()}}, upsert=True)
    t = (await say(anon, alice, "Share Rivka Zenith's media link with this WhatsApp number +91 98765 43210"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and any("Recipient" in line for line in t["summary"])      # recipient + content shown before sending
    assert await _jobs([world["rivka"]]) == [] and await db.whatsapp_jobs.count_documents({"destination": {"$regex": "919876543210$"}}) == 0
    done = (await confirm(anon, alice, t["id"], worker=world["w1"]))["task"]
    assert done["status"] == "queued"
    job = (await db.whatsapp_jobs.find({"batch_id": done["batch_ids"][0]}).to_list(5))[0]
    assert job["destination"].endswith("919876543210") and "/talent-media/" in job["message_body"] and job["worker_id"] == world["w1"]
    await db.whatsapp_jobs.delete_many({"batch_id": {"$in": done["batch_ids"]}})
    await db.whatsapp_batches.delete_many({"id": {"$in": done["batch_ids"]}})


async def test_unsupported_sharing_is_reported_not_faked(anon, alice, world):
    a = (await say(anon, alice, "Send Rivka Zenith's audition to the casting director."))["tasks"][0]
    f = await confirm(anon, alice, a["id"], worker=world["w1"])
    assert f["task"]["status"] == "failed" and "audition" in f["task"]["error"].lower()
    g = (await say(anon, alice, "Share Rivka Zenith's media link to the casting director"))["tasks"][0]
    assert (await confirm(anon, alice, g["id"], worker=world["w1"]))["task"]["status"] == "failed"
    assert await _jobs([world["rivka"]]) == []


# ---------------------------------------------------------------------------------------- privacy / ownership
async def test_users_cannot_see_confirm_cancel_clarify_retry_or_list_each_others_tasks_or_history(anon, alice, bob, world):
    mine = (await say(anon, alice, "Add Rivka Zenith to Streaming Now"))["tasks"][0]
    amb = (await say(anon, alice, "Show me Quorra Tessier's profile"))["tasks"][0]
    for verb, path, body in (("get", f"/tasks/{mine['id']}", None), ("post", f"/tasks/{mine['id']}/confirm", {}), ("post", f"/tasks/{mine['id']}/cancel", None),
                             ("post", f"/tasks/{mine['id']}/retry", None), ("post", f"/tasks/{mine['id']}/follow-up", {"template_id": world["cc"]}),
                             ("post", f"/tasks/{amb['id']}/clarify", {"slot": "talent:0", "choice_id": world["quorra"]})):
        r = await (anon.get(BASE + path, headers=bob["headers"]) if verb == "get" else anon.post(BASE + path, headers=bob["headers"], json=body or {}))
        assert r.status_code == 404, (path, r.text)                                                   # indistinguishable from "doesn't exist"
    assert (await anon.get(f"{BASE}/tasks", headers=bob["headers"])).json()["tasks"] == []
    assert (await anon.get(f"{BASE}/history", headers=bob["headers"])).json()["messages"] == []
    assert len((await anon.get(f"{BASE}/history", headers=alice["headers"])).json()["messages"]) >= 2
    assert (await anon.post(f"{BASE}/commands/{mine['command_id']}/confirm-all", headers=bob["headers"], json={})).status_code == 404
    assert await _stage(world["amazon"], world["rivka"]) is None                                       # nothing happened
    # a client-supplied owner is ignored: the body has no owner field, and the same request id is private to each user
    rid = uuid.uuid4().hex
    a, b = await say(anon, alice, "Find Rivka Zenith", rid=rid), await say(anon, bob, "Find Rivka Zenith", rid=rid)
    assert a["command_id"] != b["command_id"] and b["duplicate"] is False
    await anon.post(f"{BASE}/tasks/{mine['id']}/cancel", headers=alice["headers"])


async def test_audit_records_ids_and_outcome_but_no_message_text(anon, alice, world):
    t = (await say(anon, alice, "Add Rivka Zenith to Streaming Now"))["tasks"][0]
    await confirm(anon, alice, t["id"])
    rec = await db.assistant_audit.find_one({"task_id": t["id"]})
    assert rec["user_id"] == alice["id"] and rec["talent_ids"] == [world["rivka"]] and rec["project_ids"] == [world["amazon"]] and rec["result"] == "completed"
    assert "Rivka" not in str(rec) and "message" not in rec
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})


async def test_unrecognised_text_gets_help_not_an_action(anon, alice):
    out = await say(anon, alice, "tell me a joke about elephants")
    assert out["tasks"] == [] and "casting call" in out["reply"].lower()


async def test_the_workers_endpoint_reports_live_state_not_a_hardcoded_one(anon, alice, world):
    ws = {w["id"]: w for w in (await anon.get(f"{BASE}/workers", headers=alice["headers"])).json()["workers"]}
    assert ws[world["w1"]]["ready"] is True and ws[world["wdown"]]["ready"] is False and ws[world["wdown"]]["status"] == "Disconnected"
    await db.whatsapp_sessions.update_one({"id": world["w1"]}, {"$set": {"status": "qr_pending"}})
    assert {w["id"]: w for w in (await anon.get(f"{BASE}/workers", headers=alice["headers"])).json()["workers"]}[world["w1"]]["status"] == "Scan QR"
    await db.whatsapp_sessions.update_one({"id": world["w1"]}, {"$set": {"status": "authenticated"}})
