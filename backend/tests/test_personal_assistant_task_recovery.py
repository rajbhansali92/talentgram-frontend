"""Personal Assistant — clearing the chat never makes an outstanding task unrecoverable (2026-10-20).

Real FastAPI app + real local dev Mongo, throwaway ZZZ rows. NOTHING is sent to WhatsApp. Proves: "Clear chat" deletes conversation messages only;
the persisted task (plan, status, approval fingerprint) is untouched and can still be approved / cancelled; open work is listable by status
independently of the newest-N window; listing/clearing never executes or creates anything; isolation and authentication are unchanged."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import uuid

import httpx
import pytest
import pytest_asyncio

from core import _now, db, hash_password, make_token
from server import app
from test_casting_call_worker_select import _real_db_for_this_module  # noqa: F401  (real-db isolation, same as the other assistant modules)

pytestmark = pytest.mark.asyncio(loop_scope="module")
BASE = "/api/personal-assistant"
_created = {"talents": [], "projects": [], "users": [], "tasks": []}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def anon():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _user(role="admin"):
    uid = f"zzz-test-pa-rec-{role}-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    await db.users.insert_one({"id": uid, "email": email, "name": "ZZZ_TEST_PA_REC", "password_hash": hash_password("Password@123"), "role": role, "status": "active", "token_version": 0})
    _created["users"].append(uid)
    return {"id": uid, "email": email, "headers": {"Authorization": f"Bearer {make_token({'id': uid, 'email': email, 'role': role, 'tv': 0})}"}}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def alice():
    return await _user("admin")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def bob():
    return await _user("admin")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def cleanup():
    yield
    await db.talents.delete_many({"id": {"$in": _created["talents"]}})
    await db.projects.delete_many({"id": {"$in": _created["projects"]}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": _created["projects"]}})
    owners = _created["users"]
    for coll in ("assistant_commands", "assistant_tasks", "assistant_messages"):
        await db[coll].delete_many({"owner_id": {"$in": owners}})
    await db.assistant_audit.delete_many({"user_id": {"$in": owners}})
    await db.users.delete_many({"id": {"$in": owners}})


async def _talent(name):
    tid = f"zzz-test-pa-rec-t-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "phone": "911234500000", "status": "ACTIVE", "age": 27, "tags": [], "media": []})
    _created["talents"].append(tid)
    return tid


async def _project(name):
    pid = f"zzz-test-pa-rec-p-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({"id": pid, "slug": pid, "brand_name": name, "status": "ongoing", "created_at": _now(), "materials": []})
    _created["projects"].append(pid)
    return pid


async def _say(client, who, text):
    r = await client.post(f"{BASE}/message", headers=who["headers"], json={"text": text, "client_request_id": uuid.uuid4().hex, "context": {}})
    assert r.status_code == 200, r.text
    return r.json()


async def _counts():
    return {"jobs": await db.whatsapp_jobs.count_documents({}), "batches": await db.whatsapp_batches.count_documents({}),
            "pipeline": await db.casting_pipeline.count_documents({}), "tasks": await db.assistant_tasks.count_documents({}),
            "commands": await db.assistant_commands.count_documents({})}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world():
    w = {"rivka": await _talent("Qzr Rivka Zenith"), "proj": await _project("Qzr Streaming Now")}
    await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": w["proj"], "talent_id": w["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()})
    return w


async def _stage(world):
    r = await db.casting_pipeline.find_one({"project_id": world["proj"], "talent_id": world["rivka"]})
    return r["stage"]


async def _pending_move(anon, who, world, talent="Rivka Zenith", project="Streaming Now"):
    out = await _say(anon, who, f"Move {talent} to Approved in {project}")
    t = out["tasks"][0]
    assert t["status"] == "awaiting_confirmation", t
    return out, t


# ------------------------------------------------------------------------------------------------ clear chat vs tasks
async def test_clear_chat_deletes_messages_only_and_the_persisted_task_is_untouched(anon, alice, world):
    out, t = await _pending_move(anon, alice, world)
    before_task = await db.assistant_tasks.find_one({"owner_id": alice["id"], "id": t["id"]}, {"_id": 0})
    before = await _counts()
    assert len((await anon.get(f"{BASE}/history", headers=alice["headers"])).json()["messages"]) >= 2
    cleared = await anon.post(f"{BASE}/history/clear", headers=alice["headers"])
    assert cleared.status_code == 200 and cleared.json()["deleted"] >= 2
    assert (await anon.get(f"{BASE}/history", headers=alice["headers"])).json()["messages"] == []          # the conversation is gone …
    after = await _counts()
    assert after["tasks"] == before["tasks"] and after["commands"] == before["commands"]                    # … tasks and their commands are not
    assert after["jobs"] == before["jobs"] and after["batches"] == before["batches"] and after["pipeline"] == before["pipeline"]    # nothing created, nothing executed
    kept = await db.assistant_tasks.find_one({"owner_id": alice["id"], "id": t["id"]}, {"_id": 0})
    assert kept == before_task                                                                               # byte-for-byte: status, plan, params, fingerprint, summary
    assert kept["status"] == "awaiting_confirmation"
    assert (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]["fingerprint"] == t["fingerprint"]      # the approval fingerprint is derived from the persisted task, so it survives
    assert await _stage(world) == "ask_to_test"                                                              # and it has NOT run
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def test_after_clearing_the_chat_the_task_is_listed_fetched_and_can_still_be_approved(anon, alice, world):
    out, t = await _pending_move(anon, alice, world)
    await anon.post(f"{BASE}/history/clear", headers=alice["headers"])
    listed = (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation,needs_clarification"})).json()["tasks"]
    assert t["id"] in [x["id"] for x in listed]
    got = (await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])).json()["task"]
    assert got["status"] == "awaiting_confirmation" and got["title"] and got["summary"] and got["source_text"]      # enough to review it with no chat at all
    assert await _stage(world) == "ask_to_test"                                                              # opening it ran nothing
    stale = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": "not-what-was-shown"})
    assert stale.status_code == 409 and await _stage(world) == "ask_to_test"                                 # a stale approval cannot execute it
    ok = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": got["fingerprint"]})
    assert ok.status_code == 200 and ok.json()["task"]["status"] == "completed"
    assert await _stage(world) == "approved"
    again = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": got["fingerprint"]})
    assert again.status_code == 200 and again.json()["task"]["status"] == "completed"                         # a second approval is a no-op …
    assert await _stage(world) == "approved" and len(await db.casting_pipeline.find({"project_id": world["proj"]}).to_list(10)) == 1   # … nothing runs twice
    await db.casting_pipeline.update_one({"project_id": world["proj"], "talent_id": world["rivka"]}, {"$set": {"stage": "ask_to_test"}})


async def test_cancelling_after_the_chat_was_cleared_persists_a_terminal_state_and_blocks_execution(anon, alice, world):
    out, t = await _pending_move(anon, alice, world)
    await anon.post(f"{BASE}/history/clear", headers=alice["headers"])
    c = await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
    assert c.status_code == 200 and c.json()["task"]["status"] == "cancelled"
    assert (await db.assistant_tasks.find_one({"owner_id": alice["id"], "id": t["id"]}))["status"] == "cancelled"
    after = await anon.post(f"{BASE}/tasks/{t['id']}/confirm", headers=alice["headers"], json={"fingerprint": t["fingerprint"]})
    assert after.status_code == 200 and after.json()["task"]["status"] == "cancelled"                         # confirming a finished task is a no-op …
    assert await _stage(world) == "ask_to_test"                                                              # … a cancelled task is never executed
    open_ids = [x["id"] for x in (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation"})).json()["tasks"]]
    assert t["id"] not in open_ids                                                                           # and it leaves the 'needs you' set


async def test_a_task_needing_clarification_stays_listed_with_its_question_after_clearing(anon, alice, world):
    out = await _say(anon, alice, "Move Rivka Zenith to Narnia in Streaming Now")
    t = out["tasks"][0]
    assert t["status"] == "needs_clarification" and t["slots"]
    await anon.post(f"{BASE}/history/clear", headers=alice["headers"])
    listed = {x["id"]: x for x in (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "needs_clarification"})).json()["tasks"]}
    assert listed[t["id"]]["slots"] and listed[t["id"]]["source_text"]
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


# ----------------------------------------------------------------------------------------------- listing by status
async def test_open_tasks_are_found_by_status_even_when_more_than_the_newest_window_exist(anon, alice):
    old_cmd = uuid.uuid4().hex
    base = {"owner_id": alice["id"], "kind": "move", "slots": [], "depends_on": [], "warnings": [], "summary": ["x"], "result": None, "outcomes": [], "batch_ids": [], "error": None, "params": {}}
    old = {**base, "id": f"zzz-old-{uuid.uuid4().hex[:6]}", "command_id": old_cmd, "seq": 0, "status": "awaiting_confirmation", "title": "Old open task", "source_text": "old", "created_at": "2020-01-01T00:00:00+00:00", "updated_at": "2020-01-01T00:00:00+00:00"}
    newer = [{**base, "id": f"zzz-new-{i}-{uuid.uuid4().hex[:4]}", "command_id": uuid.uuid4().hex, "seq": 0, "status": "completed", "title": f"Done {i}", "source_text": "n",
              "created_at": f"2026-10-19T10:{i // 60:02d}:{i % 60:02d}+00:00", "updated_at": _now()} for i in range(70)]
    await db.assistant_tasks.insert_many([dict(old), *[dict(n) for n in newer]])
    plain = (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"limit": 60})).json()["tasks"]
    assert old["id"] not in [t["id"] for t in plain]                                                         # the newest-N window really does miss it …
    by_status = (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation,needs_clarification", "limit": 200})).json()["tasks"]
    assert old["id"] in [t["id"] for t in by_status]                                                         # … and the status filter really finds it
    assert all(t["status"] in ("awaiting_confirmation", "needs_clarification") for t in by_status)
    ids = [t["id"] for t in by_status]
    await db.assistant_tasks.delete_many({"owner_id": alice["id"], "id": {"$regex": "^zzz-(old|new)-"}})
    assert ids


async def test_status_filter_validation_isolation_and_authentication(anon, alice, bob):
    _, t = await _pending_move(anon, alice, await world_for_isolation(), talent="Isolde Veyra", project="Veyra Pictures")
    assert (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "bogus"})).status_code == 422
    mine = (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation"})).json()["tasks"]
    theirs = (await anon.get(f"{BASE}/tasks", headers=bob["headers"], params={"status": "awaiting_confirmation"})).json()["tasks"]
    assert t["id"] in [x["id"] for x in mine] and t["id"] not in [x["id"] for x in theirs]                  # another user never sees it
    assert (await anon.post(f"{BASE}/history/clear", headers=bob["headers"])).status_code == 200            # Bob clearing HIS chat cannot touch Alice's tasks
    assert t["id"] in [x["id"] for x in (await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation"})).json()["tasks"]]
    for hdr in ({}, {"Authorization": "Bearer not.a.token"}):
        assert (await anon.get(f"{BASE}/tasks", headers=hdr, params={"status": "awaiting_confirmation"})).status_code in (401, 403)
        assert (await anon.post(f"{BASE}/history/clear", headers=hdr)).status_code in (401, 403)
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])


async def world_for_isolation():
    w = {"rivka": await _talent("Qzr Isolde Veyra"), "proj": await _project("Qzr Veyra Pictures")}
    await db.casting_pipeline.insert_one({"id": uuid.uuid4().hex, "project_id": w["proj"], "talent_id": w["rivka"], "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()})
    return w


async def test_listing_and_clearing_never_create_jobs_batches_or_move_anyone(anon, alice, world):
    out, t = await _pending_move(anon, alice, world)
    before = await _counts()
    for _ in range(3):
        await anon.get(f"{BASE}/tasks", headers=alice["headers"])
        await anon.get(f"{BASE}/tasks", headers=alice["headers"], params={"status": "awaiting_confirmation,needs_clarification"})
        await anon.get(f"{BASE}/tasks/{t['id']}", headers=alice["headers"])
        await anon.get(f"{BASE}/history", headers=alice["headers"])
    await anon.post(f"{BASE}/history/clear", headers=alice["headers"])
    assert await _counts() == before
    assert await _stage(world) == "ask_to_test"
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=alice["headers"])
