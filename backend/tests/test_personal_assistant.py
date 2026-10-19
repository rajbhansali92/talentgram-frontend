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


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _isolate_each_test():
    """Every test starts and ends with no pipeline rows / jobs / batches on the throwaway talents and projects."""
    async def wipe():
        await db.casting_pipeline.delete_many({"project_id": {"$in": _created["projects"]}})
        await db.whatsapp_jobs.delete_many({"talent_id": {"$in": _created["talents"]}})
        await db.whatsapp_batches.delete_many({"project_id": {"$in": _created["projects"]}})
    await wipe()
    yield
    await wipe()


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
    assert (await anon.get(f"{BASE}/access", headers=alice["headers"])).json() == {"allowed": True}      # exactly the boolean — no role / config
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
    # A spelling correction is PROPOSED as a card (best match flagged), never silently chosen — one tap resolves it.
    assert t["status"] == "needs_clarification" and t["slots"][0]["options"][0]["id"] == world["rivka"] and t["slots"][0]["options"][0].get("best") is True
    t = (await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": t["slots"][0]["key"], "choice_id": world["rivka"]})).json()["task"]
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


async def test_audition_sharing_is_refused_at_planning_with_the_exact_reason(anon, alice, world):
    a = (await say(anon, alice, "Send Rivka Zenith's audition to the casting director."))["tasks"][0]
    assert a["status"] == "failed" and "audition" in a["error"].lower() and "WhatsApp casting-group workflow" in a["error"]
    await confirm(anon, alice, a["id"], worker=world["w1"], expect=200)                            # nothing to confirm: no job, no claim
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


# ======================================================================================== acceptance gaps
async def test_example_one_add_then_send_with_a_pronoun_is_one_dependent_pair(anon, alice, world):
    out = await say(anon, alice, "Add Rivka Zenith to Streaming Now and send her the Qzx Casting Call.")
    add, send = out["tasks"]
    assert (add["kind"], send["kind"]) == ("add", "send") and send["depends_on"] == [add["id"]]
    assert [e["id"] for e in send["params"]["talents"]] == [world["rivka"]] and [e["id"] for e in send["params"]["projects"]] == [world["amazon"]]
    assert send["params"]["template_id"] == world["cc"]
    r = await anon.post(f"{BASE}/tasks/{send['id']}/confirm", headers=alice["headers"], json={"worker_id": world["w1"]})
    assert r.status_code == 409 and await _jobs([world["rivka"]]) == []
    for t in out["tasks"]:
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def test_example_three_profile_and_a_pronoun_followup_both_run_and_point_at_the_same_talent(anon, alice, world):
    await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": world["old"], "talent_id": world["rivka"], "stage": "already_tested", "created_at": _now(), "updated_at": _now()})
    out = await say(anon, alice, "Show me Rivka Zenith's profile, and tell me which projects she has tested for.")
    first, second = out["tasks"]
    assert [first["status"], second["status"]] == ["completed", "completed"]                        # neither half was dropped
    assert second["result"]["kind"] == "tested" and second["result"]["cards"][0]["id"] == world["rivka"]
    assert [x["project"] for x in second["result"]["cards"][0]["tested_projects"]] == ["Qzx Fizzy Cola Legacy"]
    await db.casting_pipeline.delete_many({"talent_id": world["rivka"]})


async def test_example_four_these_other_talents_is_not_the_selection_on_screen(anon, alice, world):
    ctx = {"talent_ids": [world["rivka"]]}
    out = await say(anon, alice, "Move these talents to Follow-up, and add these other talents to Streaming Now.", ctx)
    move, add = out["tasks"]
    assert [e["id"] for e in move["params"]["talents"]] == [world["rivka"]]
    assert add["status"] == "needs_clarification" and add["params"]["talents"][0]["state"] == "none"         # asks who — never reuses the selection
    assert "other talents" in add["slots"][0]["prompt"]
    fixed = (await anon.post(f"{BASE}/tasks/{add['id']}/clarify", headers=alice["headers"], json={"slot": "talent:0", "text": "Nehara Zolt"})).json()["task"]
    assert fixed["status"] == "awaiting_confirmation" and [e["id"] for e in fixed["params"]["talents"]] == [world["nehara"]]
    for t in (move, fixed):
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def test_partial_surname_only_and_misspelt_names_resolve_but_nothing_runs_without_confirmation(anon, alice, world):
    for phrase in ("Rivka", "Zenith", "rivka zenith"):                                                 # exact / partial → understood at once
        t = (await say(anon, alice, f"Add {phrase} to Streaming Now"))["tasks"][0]
        assert t["status"] == "awaiting_confirmation" and t["params"]["talents"][0]["id"] == world["rivka"], phrase
        assert "Qzx Rivka Zenith" in t["title"]                                                      # the user sees who it understood
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
    typo = (await say(anon, alice, "Add Rivca Zenyth to Streaming Now"))["tasks"][0]                  # misspelt → a card to tap, not a guess
    assert typo["status"] == "needs_clarification" and typo["slots"][0]["options"][0]["id"] == world["rivka"]
    await anon.post(f"{BASE}/tasks/{typo['id']}/cancel", headers=alice["headers"])
    assert await _stage(world["amazon"], world["rivka"]) is None
    nick = (await say(anon, alice, "Add Riv to Streaming Now"))["tasks"][0]                          # a nickname is never silently trusted
    assert nick["status"] in ("needs_clarification", "awaiting_confirmation") and await _stage(world["amazon"], world["rivka"]) is None
    await anon.post(f"{BASE}/tasks/{nick['id']}/cancel", headers=alice["headers"])


async def test_two_ongoing_projects_with_similar_names_must_be_chosen_and_a_missing_project_asks(anon, alice, world):
    p1, p2 = await _project("Qzx Mango Soda One"), await _project("Qzx Mango Soda Two")
    t = (await say(anon, alice, "Add Rivka Zenith to Mango Soda"))["tasks"][0]
    assert t["status"] == "needs_clarification" and {o["id"] for o in t["slots"][0]["options"]} == {p1, p2}
    picked = (await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "project:0", "choice_id": p2})).json()["task"]
    assert picked["status"] == "awaiting_confirmation" and [e["id"] for e in picked["params"]["projects"]] == [p2]
    missing = (await say(anon, alice, "Add Rivka Zenith to Zqjxv Nonexistent"))["tasks"][0]
    assert missing["status"] == "needs_clarification" and missing["slots"][0]["kind"] == "project"
    none_given = (await say(anon, alice, "Add Rivka Zenith"))["tasks"]
    assert not none_given or none_given[0]["status"] in ("needs_clarification",)
    for x in (picked, missing):
        await anon.post(f"{BASE}/tasks/{x['id']}/cancel", headers=alice["headers"])


async def test_a_talent_who_exists_but_is_not_in_the_project_gets_the_add_planned_first_and_a_move_still_refuses(anon, alice, world):
    out = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith for Streaming Now"))["tasks"]
    add, send = out
    assert (add["kind"], send["kind"]) == ("add", "send") and send["depends_on"] == [add["id"]]      # step 1 is the add, step 2 the send
    assert add["status"] == send["status"] == "awaiting_confirmation" and add["title"].startswith("First: add")
    m = (await say(anon, alice, "Move Rivka Zenith to Approved in Streaming Now"))["tasks"][0]
    assert m["status"] == "failed" and "pipeline" in m["error"]                                       # a move has no add to plan: still a clear refusal
    assert await _jobs([world["rivka"]]) == [] and await _stage(world["amazon"], world["rivka"]) is None
    for t in (add, send):
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def test_two_simultaneous_confirms_of_one_send_create_exactly_one_batch_and_one_message(anon, alice, world):
    import asyncio
    await _add_rows(world["amazon"], world["rivka"])
    t = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]
    rs = await asyncio.gather(*[anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": world["w1"]}) for _ in range(5)])
    assert all(r.status_code == 200 for r in rs)
    assert len(await _jobs([world["rivka"]])) == 1
    assert await db.whatsapp_batches.count_documents({"project_id": world["amazon"], "template_id": world["fu"]}) == 1
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": world["rivka"]})
    await db.whatsapp_batches.delete_many({"project_id": world["amazon"]})


async def test_repeating_the_same_send_is_flagged_before_it_can_message_someone_twice(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"])
    first = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]
    assert not first.get("duplicate_risk")
    await confirm(anon, alice, first["id"], worker=world["w1"])
    again = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]            # a NEW request, same intent
    assert again["duplicate_risk"] is True and any("twice" in w for w in again["warnings"])
    await anon.post(f"{BASE}/tasks/{again['id']}/cancel", headers=alice["headers"])
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": world["rivka"]})
    await db.whatsapp_batches.delete_many({"project_id": world["amazon"]})


async def test_a_worker_going_offline_after_queueing_leaves_the_jobs_pending_and_the_task_queued(anon, alice, world):
    w = await _worker("ZZZ PA Flaky")
    await _add_rows(world["amazon"], world["rivka"])
    t = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]
    await confirm(anon, alice, t["id"], worker=w)
    await db.whatsapp_sessions.update_one({"id": w}, {"$set": {"status": "disconnected"}})          # redeploy / logout
    cur = (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]
    assert cur["status"] == "queued" and {j["status"] for j in await _jobs([world["rivka"]])} == {"pending"}
    assert "sent" not in cur["status_detail"]                                                         # never inflated into success
    await db.casting_pipeline.delete_many({"project_id": world["amazon"]})
    await db.whatsapp_jobs.delete_many({"talent_id": world["rivka"]})
    await db.whatsapp_batches.delete_many({"project_id": world["amazon"]})


async def test_an_interrupted_run_is_failed_as_uncertain_and_a_send_is_never_blindly_retried(anon, alice, world):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    a = (await say(anon, alice, "Add Nehara Zolt to Streaming Now"))["tasks"][0]
    s = (await say(anon, alice, "Send the Qzx Follow Up to Nehara Zolt for Streaming Now. "))["tasks"]
    await db.assistant_tasks.update_one({"id": a["id"]}, {"$set": {"status": "running", "started_at": old}})
    got = (await anon.get(f"{BASE}/tasks/{a['id']}", headers=alice["headers"])).json()["task"]
    assert got["status"] == "failed" and got["uncertain"] is True and "interrupted" in got["error"]
    assert (await anon.post(f"{BASE}/tasks/{a['id']}/retry", headers=alice["headers"])).json()["task"]["status"] == "awaiting_confirmation"      # idempotent kinds may retry
    snd = (await say(anon, alice, "Share Nehara Zolt's media link with +91 98765 43211"))["tasks"][0]
    await db.assistant_tasks.update_one({"id": snd["id"]}, {"$set": {"status": "running", "started_at": old}})
    got2 = (await anon.get(f"{BASE}/tasks/{snd['id']}", headers=alice["headers"])).json()["task"]
    assert got2["status"] == "failed" and got2["uncertain"] is True and "campaign history" in got2["error"]
    r = await anon.post(f"{BASE}/tasks/{snd['id']}/retry", headers=alice["headers"])
    assert r.status_code == 409 and "campaign history" in r.json()["detail"]                          # blocked: could double-send
    fresh = (await say(anon, alice, "Add Nehara Zolt to Fizzy Cola"))["tasks"][0]
    await db.assistant_tasks.update_one({"id": fresh["id"]}, {"$set": {"status": "running", "started_at": _now()}})
    assert (await anon.get(f"{BASE}/tasks/{fresh['id']}", headers=alice["headers"])).json()["task"]["status"] == "running"   # a live run is left alone


# -------------------------------------------------------------------------------------------- recipients
async def _crm(name, phone, ctype="casting_director"):
    r = await db.clients.insert_one({"name": name, "phone_number": phone, "contact_type": ctype, "company_name": "ZZZ TEST PA Co"})
    _created.setdefault("clients", []).append(r.inserted_id)
    return str(r.inserted_id)


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def cleanup_clients():
    yield
    await db.clients.delete_many({"_id": {"$in": _created.get("clients", [])}})
    await db.whatsapp_templates.delete_many({"id": "zzz-test-pa-custom"})


async def test_share_to_a_named_crm_contact_previews_recipient_and_content_without_writing_then_sends_to_that_contact(anon, alice, world):
    await db.whatsapp_templates.update_one({"slug": "custom"}, {"$setOnInsert": {"id": "zzz-test-pa-custom", "name": "Custom Message", "slug": "custom", "body_text": "{{message}}", "variables": ["message"], "created_at": _now(), "updated_at": _now()}}, upsert=True)
    cid = await _crm("Qzx Casty Person", "+919800011122")
    await db.talents.update_one({"id": world["rivka"]}, {"$unset": {"media_download_token": ""}, "$set": {"media_download_enabled": True}})
    t = (await say(anon, alice, "Share Rivka Zenith's media link with Casty Person"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and t["params"]["recipient"]["id"] == cid
    text = "\n".join(t["summary"])
    assert "Qzx Casty Person" in text and "••••• 11122" in text and "9800011122" not in text         # recipient shown, number masked
    assert "Qzx Rivka Zenith" in text and "created when you confirm" in text                         # content shown
    assert "media_download_token" not in (await db.talents.find_one({"id": world["rivka"]}, {"_id": 0}))   # preview wrote nothing
    done = (await confirm(anon, alice, t["id"], worker=world["w2"]))["task"]
    job = (await db.whatsapp_jobs.find({"batch_id": done["batch_ids"][0]}).to_list(5))[0]
    assert job["destination"].endswith("9800011122") and "/talent-media/" in job["message_body"] and job["worker_id"] == world["w2"]
    assert done["params"]["worker_label"] == "ZZZ PA Worker 2"
    await db.whatsapp_jobs.delete_many({"batch_id": {"$in": done["batch_ids"]}})
    await db.whatsapp_batches.delete_many({"id": {"$in": done["batch_ids"]}})


async def test_a_role_is_never_guessed_it_lists_the_saved_contacts_of_that_type_to_pick_from(anon, alice, world):
    a, b = await _crm("Qzx Director A", "+919800011133"), await _crm("Qzx Director B", "+919800011144")
    t = (await say(anon, alice, "Share Rivka Zenith's profile with the casting director"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"][0]["kind"] == "recipient"
    opts = {o["id"]: o for o in t["slots"][0]["options"]}
    assert {f"crm:{a}", f"crm:{b}"} <= set(opts) and "Casting Director" in opts[f"crm:{a}"]["sublabel"] and "9800011133" not in str(opts)
    bad = await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "recipient", "choice_id": "crm:000000000000000000000000"})
    assert bad.status_code == 400                                                                      # only offered options are accepted
    ok = (await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "recipient", "choice_id": f"crm:{b}"})).json()["task"]
    assert ok["status"] == "awaiting_confirmation" and ok["params"]["recipient"]["id"] == b
    await anon.post(f"{BASE}/tasks/{ok['id']}/cancel", headers=alice["headers"])


async def test_unknown_recipient_asks_and_a_typed_number_resolves_it(anon, alice, world):
    t = (await say(anon, alice, "Share Rivka Zenith's media link with Zqjxv Nobody"))["tasks"][0]
    assert t["status"] == "needs_clarification" and "couldn't find" in t["slots"][0]["prompt"].lower()
    fixed = (await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": "recipient", "text": "+91 98765 43210"})).json()["task"]
    assert fixed["status"] == "awaiting_confirmation" and "••••• 43210" in "\n".join(fixed["summary"])
    none = (await say(anon, alice, "Share Rivka Zenith's media link"))["tasks"][0]
    assert none["status"] == "needs_clarification" and "Who should I send it to" in none["slots"][0]["prompt"]
    for x in (fixed, none):
        await anon.post(f"{BASE}/tasks/{x['id']}/cancel", headers=alice["headers"])


async def test_a_project_group_share_shows_the_group_and_a_project_without_one_is_refused(anon, alice, world):
    t = (await say(anon, alice, "Share Rivka Zenith's media link with the WhatsApp group for Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation" and "ZZZ TEST PA GROUP" in "\n".join(t["summary"])
    nogroup = (await say(anon, alice, "Share Rivka Zenith's media link with the WhatsApp group for Fizzy Cola"))["tasks"][0]
    assert nogroup["status"] == "failed" and "no WhatsApp casting group" in nogroup["error"]
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


# -------------------------------------------------------------------------------------------- privacy depth
async def test_every_assistant_endpoint_scopes_by_the_authenticated_user_not_by_anything_in_the_request(anon, alice, bob, world):
    mine = await say(anon, alice, "Add Rivka Zenith to Streaming Now")
    task_id, cmd_id = mine["tasks"][0]["id"], mine["command_id"]
    # listing with someone else's command id / extra owner fields in the body returns nothing and acts on nothing
    assert (await anon.get(f"{BASE}/tasks", headers=bob["headers"], params={"command_id": cmd_id, "owner_id": alice["id"]})).json()["tasks"] == []
    spoof = await anon.post(f"{BASE}/message", headers=bob["headers"], json={"text": "Find Rivka Zenith", "client_request_id": uuid.uuid4().hex, "owner_id": alice["id"], "user_id": alice["id"]})
    assert spoof.status_code == 200
    assert (await anon.get(f"{BASE}/tasks", headers=alice["headers"])).json()["tasks"][0]["id"] != spoof.json()["tasks"][0]["id"]      # recorded under bob, not alice
    assert not any(t["id"] == spoof.json()["tasks"][0]["id"] for t in (await anon.get(f"{BASE}/tasks", headers=alice["headers"])).json()["tasks"])
    assert (await anon.post(f"{BASE}/history/clear", headers=bob["headers"])).json()["deleted"] >= 1
    assert len((await anon.get(f"{BASE}/history", headers=alice["headers"])).json()["messages"]) >= 2      # alice's history untouched by bob's clear
    for method, path in (("get", f"/tasks/{task_id}"), ("post", f"/tasks/{task_id}/confirm"), ("post", f"/tasks/{task_id}/cancel"), ("post", f"/tasks/{task_id}/retry"),
                         ("post", f"/commands/{cmd_id}/confirm-all")):
        r = await getattr(anon, method)(BASE + path, headers=bob["headers"], **({"json": {}} if method == "post" else {}))
        assert r.status_code == 404 and "Rivka" not in r.text and alice["id"] not in r.text, path           # leaks nothing, not even existence
    assert await _stage(world["amazon"], world["rivka"]) is None
    await anon.post(f"{BASE}/tasks/{task_id}/cancel", headers=alice["headers"])


async def test_tasks_and_history_survive_a_new_session(anon, alice, world):
    out = await say(anon, alice, "Show me Nehara Zolt's profile")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as fresh_client:    # "a new browser"
        tasks = (await fresh_client.get(f"{BASE}/tasks", headers=alice["headers"])).json()["tasks"]
        assert any(t["id"] == out["tasks"][0]["id"] and t["status"] == "completed" for t in tasks)
        assert any(m["command_id"] == out["command_id"] for m in (await fresh_client.get(f"{BASE}/history", headers=alice["headers"])).json()["messages"])


async def test_media_link_preview_finds_a_bare_talent_that_has_none_of_the_optional_fields(world):
    """Real talents often have no status / media_download_* fields: the projection is then `{}` (falsy) and the preview
    used to say "talent not found — will be skipped" for someone who exists."""
    from personal_assistant import resolve as R
    tid = f"zzz-test-pa-bare-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": "Qzx Bare Talent"})
    _created["talents"].append(tid)
    assert (await R.peek_media_link(tid))["state"] == "will_create"
    assert (await R.peek_media_link("zzz-test-pa-does-not-exist"))["state"] == "missing"
    await db.talents.update_one({"id": tid}, {"$set": {"media_download_enabled": False}})
    assert (await R.peek_media_link(tid))["state"] == "off"


# ------------------------------------------------------------------------------- production-acceptance safety checks
async def _footprint(world):
    """Everything a confirmed action could change, for the throwaway fixtures."""
    projects = [world["amazon"], world["pepsi"]]
    talents = [world["rivka"], world["nehara"]]
    return {
        "pipeline": sorted([(r["project_id"], r["talent_id"], r["stage"]) async for r in db.casting_pipeline.find({"project_id": {"$in": projects}}, {"_id": 0})]),
        "jobs": await db.whatsapp_jobs.count_documents({"talent_id": {"$in": talents}}),
        "batches": await db.whatsapp_batches.count_documents({"project_id": {"$in": projects}}),
        "tokens": await db.talents.count_documents({"id": {"$in": talents}, "media_download_token": {"$exists": True}}),
    }


async def test_planning_every_kind_of_task_writes_nothing_until_it_is_confirmed(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"])
    before = await _footprint(world)
    res = await say(anon, alice, "Add Nehara Zolt to Fizzy Cola. Move Rivka Zenith in Streaming Now to Approved. "
                                 "Send the Qzx Casting Call to Rivka Zenith for Streaming Now. Share Rivka Zenith's media link with +91 98765 43211")
    kinds = {t["kind"] for t in res["tasks"]}
    assert {"add", "move", "send", "share"} <= kinds, kinds                         # all four consequential kinds were planned…
    assert all(t["status"] in ("awaiting_confirmation", "needs_clarification") for t in res["tasks"])
    assert await _footprint(world) == before                                         # …and not one row, job, batch or media token was written
    for t in res["tasks"]:                                                           # a refresh / poll / preview read changes nothing either
        await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])
    await anon.get(f"{BASE}/tasks", headers=alice["headers"])
    assert await _footprint(world) == before


async def test_cancelling_a_task_touches_only_its_own_pending_jobs(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"], world["nehara"])
    mine = (await say(anon, alice, "Send the Qzx Follow Up to Rivka Zenith for Streaming Now"))["tasks"][0]
    other = (await say(anon, alice, "Send the Qzx Casting Call to Nehara Zolt for Streaming Now"))["tasks"][0]
    await confirm(anon, alice, mine["id"], worker=world["w1"])
    await confirm(anon, alice, other["id"], worker=world["w2"])
    await db.whatsapp_jobs.insert_one({"id": "zzz-test-pa-foreign-job", "batch_id": "zzz-test-pa-foreign-batch", "talent_id": world["nehara"], "talent_name": "Qzx Nehara Zolt",
                                       "status": "pending", "worker_id": world["w1"], "message_body": "someone else's campaign"})       # a campaign this assistant never made
    assert (await anon.post(f"{BASE}/tasks/{mine['id']}/cancel", headers=alice["headers"])).json()["task"]["status"] == "cancelled"
    by_talent = {j["talent_id"]: j["status"] for j in await _jobs([world["rivka"], world["nehara"]]) if j["id"] != "zzz-test-pa-foreign-job"}
    assert by_talent[world["rivka"]] == "skipped" and by_talent[world["nehara"]] == "pending"          # the other task's job is untouched
    assert (await db.whatsapp_jobs.find_one({"id": "zzz-test-pa-foreign-job"}))["status"] == "pending"   # and so is a campaign from outside the assistant
    assert (await anon.get(f"{BASE}/tasks/{other['id']}", headers=alice["headers"])).json()["task"]["status"] == "queued"


async def test_double_tapping_retry_requeues_each_failed_message_once_and_never_errors_or_resends_a_sent_one(anon, alice, world):
    import asyncio
    await _add_rows(world["amazon"], world["rivka"], world["nehara"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith and Nehara Zolt for Streaming Now"))["tasks"][0]
    await confirm(anon, alice, t["id"], worker=world["w1"])
    jobs = await _jobs([world["rivka"], world["nehara"]])
    await db.whatsapp_jobs.update_one({"id": jobs[0]["id"]}, {"$set": {"status": "sent", "verification_status": "verified"}})
    await db.whatsapp_jobs.update_one({"id": jobs[1]["id"]}, {"$set": {"status": "failed", "error_message": "number not on WhatsApp"}})
    assert (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]["status"] == "partial"
    rs = await asyncio.gather(*[anon.post(f"{BASE}/tasks/{t['id']}/retry", headers=alice["headers"]) for _ in range(4)])
    assert all(r.status_code in (200, 409) for r in rs), [(r.status_code, r.text[:120]) for r in rs]   # never a 400/500 from the underlying job retry
    assert any(r.status_code == 200 for r in rs)
    after = {j["id"]: j for j in await _jobs([world["rivka"], world["nehara"]])}
    assert len(after) == 2 and after[jobs[0]["id"]]["status"] == "sent"                              # the delivered one is never re-queued
    assert after[jobs[1]["id"]]["status"] == "pending"                                                # the failed one is re-queued exactly once


async def test_the_missing_pipeline_step_names_who_and_where_for_one_and_for_several_talents(anon, alice, world):
    one = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith for Streaming Now"))["tasks"]
    assert any("Qzx Rivka Zenith → Qzx Streaming Now (not in the pipeline yet)" in l for l in one[0]["summary"])
    two = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith and Nehara Zolt for Streaming Now"))["tasks"]
    assert sum("(not in the pipeline yet)" in l for l in two[0]["summary"]) == 2
    assert await _jobs([world["rivka"], world["nehara"]]) == []                                              # nothing queued, nothing added yet
    for t in one + two:
        await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def test_which_projects_has_a_named_talent_tested_for_returns_that_talents_history(anon, alice, world):
    await db.casting_pipeline.insert_many([
        {"id": uuid.uuid4().hex, "project_id": world["old"], "talent_id": world["rivka"], "stage": "already_tested", "created_at": _now(), "updated_at": _now()},
        {"id": uuid.uuid4().hex, "project_id": world["pepsi"], "talent_id": world["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()}])
    for phrase in ("Which projects has Rivka Zenith tested for", "WHICH PROJECTS HAS RIVKA ZENITH TESTED FOR?"):
        t = (await say(anon, alice, phrase))["tasks"][0]
        assert t["status"] == "completed", t
        card = t["result"]["cards"][0]
        assert t["result"]["kind"] == "tested" and card["id"] == world["rivka"]
        assert [x["project"] for x in card["tested_projects"]] == ["Qzx Fizzy Cola Legacy"]       # only the Already-Tested one
        assert {p["project"] for p in card["history"]["projects"]} == {"Qzx Fizzy Cola Legacy", "Qzx Fizzy Cola"}


async def test_which_projects_has_a_name_shared_by_two_talents_tested_for_asks_which_and_never_picks(anon, alice, world):
    t = (await say(anon, alice, "Which projects has Quorra Tessier tested for?"))["tasks"][0]
    assert t["status"] == "needs_clarification" and t.get("result") in (None, {})                 # nothing was looked up yet
    slot = t["slots"][0]
    assert slot["kind"] == "talent" and {o["id"] for o in slot["options"]} == {world["quorra"], world["quorra2"]}
    ok = (await anon.post(f"{BASE}/tasks/{t['id']}/clarify", headers=alice["headers"], json={"slot": slot["key"], "choice_id": world["quorra2"]})).json()["task"]
    assert ok["status"] == "completed" and ok["result"]["kind"] == "tested" and ok["result"]["cards"][0]["id"] == world["quorra2"]


async def test_a_pronoun_question_still_uses_the_talent_of_the_previous_step(anon, alice, world):
    out = await say(anon, alice, "Show me Rivka Zenith's profile, and tell me which projects she has tested for.")
    second = out["tasks"][1]
    assert second["status"] == "completed" and second["result"]["kind"] == "tested" and second["result"]["cards"][0]["id"] == world["rivka"]


# --------------------------------------------------------------------- send preview shows WHERE each message goes
async def _preview_text(t):
    return "\n".join(t["summary"]) + "\n" + str(t.get("preview"))


async def test_send_preview_shows_a_masked_number_never_the_full_one_and_matches_the_queued_destination(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith for Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation"
    assert "  • Qzx Rivka Zenith → WhatsApp 91 ••••• 00000" in t["summary"]
    assert "911234500000" not in await _preview_text(t)                                          # the full number is never shown
    assert await _jobs([world["rivka"]]) == []                                                   # previewing queued nothing
    await confirm(anon, alice, t["id"], worker=world["w1"])
    job = (await _jobs([world["rivka"]]))[0]
    assert str(job.get("destination") or job.get("phone") or "").endswith("00000")              # what the preview promised is what was queued


async def test_send_preview_shows_the_whatsapp_group_when_the_talent_routes_to_a_group(anon, alice, world):
    gid = await _talent("Qzx Grouped Gita", phone="911234599999", whatsapp_group_name="ZZZ TEST PA TALENT GROUP")
    await _add_rows(world["amazon"], gid)
    t = (await say(anon, alice, "Send the Qzx Casting Call to Grouped Gita for Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation"
    assert "  • Qzx Grouped Gita → WhatsApp group “ZZZ TEST PA TALENT GROUP”" in t["summary"]
    assert "99999" not in await _preview_text(t)                                                 # group wins, exactly as the sender decides — phone not shown
    await confirm(anon, alice, t["id"], worker=world["w1"])
    job = (await _jobs([gid]))[0]
    assert "ZZZ TEST PA TALENT GROUP" in str(job.get("destination") or job.get("whatsapp_group_name") or job)


async def test_send_preview_lists_a_talent_without_a_destination_as_skipped_and_still_allows_the_others(anon, alice, world):
    await _add_rows(world["amazon"], world["rivka"], world["nophone"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith and Nolan Nophone for Streaming Now"))["tasks"][0]
    assert t["status"] == "awaiting_confirmation"                                                # partial: the queue skips one, so do we
    assert "  • Qzx Nolan Nophone → no WhatsApp number or group on file — will be skipped" in t["summary"]
    assert "  • Qzx Rivka Zenith → WhatsApp 91 ••••• 00000" in t["summary"]


async def test_send_with_no_usable_destination_is_refused_at_planning_with_no_confirm(anon, alice, world):
    await _add_rows(world["amazon"], world["nophone"])
    t = (await say(anon, alice, "Send the Qzx Casting Call to Nolan Nophone for Streaming Now"))["tasks"][0]
    assert t["status"] == "failed" and t["error"].startswith("That talent has no WhatsApp number or group on file")
    r = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"worker_id": world["w1"]})
    assert r.status_code == 200 and r.json()["task"]["status"] == "failed"                       # the existing idempotent confirm hands back the unchanged task…
    assert await _jobs([world["nophone"]]) == [] and await db.whatsapp_batches.count_documents({"project_id": world["amazon"]}) == 0   # …and nothing was queued


async def test_if_the_destination_cannot_be_checked_the_send_is_not_offered(anon, alice, world, monkeypatch):
    await _add_rows(world["amazon"], world["rivka"])
    import routers.whatsapp as W
    def boom(_talent):
        raise RuntimeError("routing unavailable")
    monkeypatch.setattr(W, "_resolve_destination", boom)
    t = (await say(anon, alice, "Send the Qzx Casting Call to Rivka Zenith for Streaming Now"))["tasks"][0]
    assert t["status"] == "failed" and "won't offer to send" in t["error"]                       # fail closed — never guess a destination
    assert await _jobs([world["rivka"]]) == []


# ------------------------------------------------------------ "which projects has X tested for" answers with projects, not a profile
async def _submission(tid, pid, status):
    sid = f"zzz-test-pa-sub-{uuid.uuid4().hex[:8]}"
    await db.submissions.insert_one({"id": sid, "talent_id": tid, "project_id": pid, "status": status, "created_at": _now(), "talent_email": f"{sid}@example.com"})   # unique index on (project, email)
    return sid


async def test_which_project_has_x_tested_for_returns_a_titled_project_answer_with_tested_and_submitted_projects(anon, alice, world):
    await db.casting_pipeline.insert_many([
        {"id": uuid.uuid4().hex, "project_id": world["old"], "talent_id": world["rivka"], "stage": "already_tested", "created_at": _now(), "updated_at": _now()},
        {"id": uuid.uuid4().hex, "project_id": world["pepsi"], "talent_id": world["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()}])
    subs = []
    try:
        subs += [await _submission(world["rivka"], world["pepsi"], "submitted"), await _submission(world["rivka"], world["amazon"], "draft")]
        for phrase in ("Which project has Rivka Zenith tested for", "Which projects has Rivka Zenith tested for?", "What project has Rivka Zenith tested for?",
                       "What projects has Rivka Zenith tested for?", "Has Rivka Zenith tested?"):
            t = (await say(anon, alice, phrase))["tasks"][0]
            assert t["status"] == "completed" and t["title"] == "Projects tested · Qzx Rivka Zenith", (phrase, t["title"])     # not "Has … tested?" for a which-question
            assert t["result"]["kind"] == "tested"
            card = t["result"]["cards"][0]
            assert [x["project"] for x in card["tested_projects"]] == ["Qzx Fizzy Cola Legacy"]
            assert [x["project"] for x in card["submitted_projects"]] == ["Qzx Fizzy Cola"]            # the draft for Streaming Now is not a submission
    finally:
        await db.submissions.delete_many({"id": {"$in": subs}})


async def test_a_talent_with_submissions_but_no_project_marked_already_tested_gets_an_explicit_empty_answer_plus_her_submissions(anon, alice, world):
    """The production case: many submissions, none of the pipeline rows in Already Tested — the answer must say so, not go blank."""
    await db.casting_pipeline.insert_many([
        {"id": uuid.uuid4().hex, "project_id": world["amazon"], "talent_id": world["nehara"], "stage": "approved", "created_at": _now(), "updated_at": _now()},
        {"id": uuid.uuid4().hex, "project_id": world["pepsi"], "talent_id": world["nehara"], "stage": "follow_up", "created_at": _now(), "updated_at": _now()}])
    subs = []
    try:
        subs += [await _submission(world["nehara"], world["amazon"], "submitted"), await _submission(world["nehara"], world["amazon"], "updated"),
                 await _submission(world["nehara"], world["pepsi"], "submitted")]
        card = (await say(anon, alice, "Which project has Nehara Zolt tested for"))["tasks"][0]["result"]["cards"][0]
        assert card["tested_projects"] == []
        assert sorted(x["project"] for x in card["submitted_projects"]) == ["Qzx Fizzy Cola", "Qzx Streaming Now"]    # one row per project (two Amazon submissions → one)
        assert {p["project"] for p in card["history"]["projects"]} == {"Qzx Fizzy Cola", "Qzx Streaming Now"}
    finally:
        await db.submissions.delete_many({"id": {"$in": subs}})


async def test_which_project_questions_resolve_hyphenated_and_punctuated_names_and_still_ask_when_ambiguous(anon, alice, world):
    anne = await _talent("Qzx Anne-Marie O'Neil")
    t = (await say(anon, alice, "Which projects has Qzx Anne-Marie O'Neil tested for?"))["tasks"][0]
    assert t["status"] == "completed" and t["result"]["cards"][0]["id"] == anne and t["title"] == "Projects tested · Qzx Anne-Marie O'Neil"
    amb = (await say(anon, alice, "Which project has Quorra Tessier tested for"))["tasks"][0]
    assert amb["status"] == "needs_clarification" and not amb.get("result")                                       # two talents share the name: nothing picked for her
    assert {o["id"] for o in amb["slots"][0]["options"]} == {world["quorra"], world["quorra2"]}


async def test_the_projects_a_talent_is_in_and_the_projects_she_tested_for_are_different_answers(anon, alice, world):
    await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": world["old"], "talent_id": world["rivka"], "stage": "already_tested", "created_at": _now(), "updated_at": _now()})
    been = (await say(anon, alice, "Which projects has Rivka Zenith been added to?"))["tasks"][0]
    tested = (await say(anon, alice, "Which projects has Rivka Zenith tested for?"))["tasks"][0]
    assert been["title"] == "Projects of Qzx Rivka Zenith" and tested["title"] == "Projects tested · Qzx Rivka Zenith"
    assert "submitted_projects" not in been["result"]["cards"][0] and "submitted_projects" in tested["result"]["cards"][0]
