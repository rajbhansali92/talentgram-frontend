"""Focused tests for Workflow -> WhatsApp Talentgram Workflow group
notifications (routers/workflow.py's enqueue_workflow_whatsapp_notification
and its call sites in create_task/update_task).

Reuses the EXISTING WhatsApp send/queue infrastructure end to end: every
notification is a real insert into db.whatsapp_batches/db.whatsapp_jobs
(recipient_kind="INTERNAL_GROUP") — the same collections and shape the
existing internal-submission-notification feature already uses. No worker
process runs in this test suite (it is a wholly separate deployable), so
inserting a "pending" job here is inert and sends nothing — the same
"queue insert IS the full unit of work" safety guarantee used by
test_whatsapp_notifications.py.

Since the enqueue is intentionally fire-and-forget (asyncio.create_task,
never awaited by the request handler — see workflow.py's
enqueue_workflow_whatsapp_notification), each test briefly
`await asyncio.sleep(...)` after the HTTP call before asserting, matching
test_whatsapp_notifications.py's own established pattern for this exact
class of background task.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import uuid

import pytest
import pytest_asyncio
import httpx

from server import app
from core import db, _now, make_token, hash_password

_aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def admin(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    body = r.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "id": body["admin"]["id"], "name": body["admin"].get("name") or "Admin"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team(client):
    uid = f"zzz-test-wfwa-team-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    name = "ZZZ_TEST_WFWA Team Member"
    await db.users.insert_one({
        "id": uid, "email": email, "name": name,
        "password_hash": hash_password("Password@123"), "role": "team",
        "status": "active", "token_version": 0,
    })
    token = make_token({"id": uid, "email": email, "role": "team", "tv": 0})
    yield {"headers": {"Authorization": f"Bearer {token}"}, "id": uid, "name": name}
    await db.users.delete_one({"id": uid})


async def _cleanup(*task_ids):
    if not task_ids:
        return
    await db.workflow_tasks.delete_many({"id": {"$in": list(task_ids)}})
    jobs = await db.whatsapp_jobs.find(
        {"source": "WORKFLOW_NOTIFICATION", "source_id": {"$in": list(task_ids)}}, {"_id": 0, "batch_id": 1},
    ).to_list(500)
    await db.whatsapp_jobs.delete_many({"source": "WORKFLOW_NOTIFICATION", "source_id": {"$in": list(task_ids)}})
    batch_ids = [j["batch_id"] for j in jobs if j.get("batch_id")]
    if batch_ids:
        await db.whatsapp_batches.delete_many({"id": {"$in": batch_ids}})


async def _jobs_for(task_id):
    """Waits for the fire-and-forget background enqueue to land, then
    returns and DELETES every job seen so far for this task — each call
    reports only notifications fired since the previous call, so a
    multi-step test (e.g. complete then reopen) doesn't see an earlier
    step's job leak into a later assertion."""
    await asyncio.sleep(0.2)
    jobs = await db.whatsapp_jobs.find(
        {"source": "WORKFLOW_NOTIFICATION", "source_id": task_id}, {"_id": 0},
    ).sort("created_at", 1).to_list(50)
    ids = [j["id"] for j in jobs]
    batch_ids = [j["batch_id"] for j in jobs if j.get("batch_id")]
    if ids:
        await db.whatsapp_jobs.delete_many({"id": {"$in": ids}})
    if batch_ids:
        await db.whatsapp_batches.delete_many({"id": {"$in": batch_ids}})
    return jobs


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    created = []
    yield created
    await _cleanup(*created)


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------
@_aio
async def test_new_task_creates_exactly_one_notification(client, admin, team, env):
    r = await client.post(
        "/api/workflow/tasks",
        json={"title": "ZZZ_TEST_WFWA basic task", "category": "general", "assignee_id": team["id"], "due_at": "2026-09-29T17:00:00+00:00", "priority": "high"},
        headers=admin["headers"],
    )
    assert r.status_code == 200
    tid = r.json()["id"]
    env.append(tid)

    jobs = await _jobs_for(tid)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["recipient_kind"] == "INTERNAL_GROUP"
    assert job["destination_type"] == "group"
    assert job["destination"] == "Talentgram Workflow"  # default fallback, no config row set
    body = job["message_body"]
    assert "NEW TASK" in body
    assert "ZZZ_TEST_WFWA basic task" in body
    assert f"Assigned to: {team['name']}" in body
    assert f"Created by: {admin['name']}" in body
    assert "Due: 29 Sep, 5:00 PM" in body
    assert "Priority: High" in body
    assert f"?task={tid}" in body
    assert "https://review.talentgramagency.com/admin/workflow" in body


@_aio
async def test_new_task_notification_includes_project(client, admin, env):
    r = await client.post(
        "/api/workflow/tasks",
        json={"title": "ZZZ_TEST_WFWA project task", "category": "project", "project_id": "proj-1", "project_name": "Snapdragon Film 3"},
        headers=admin["headers"],
    )
    tid = r.json()["id"]
    env.append(tid)
    jobs = await _jobs_for(tid)
    assert "Project: Snapdragon Film 3" in jobs[0]["message_body"]


@_aio
async def test_new_task_event_timestamp_present(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA ts task"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    jobs = await _jobs_for(tid)
    assert "Created:" in jobs[0]["message_body"]


# ---------------------------------------------------------------------------
# Checklist creation
# ---------------------------------------------------------------------------
@_aio
async def test_new_checklist_item_generates_one_notification(client, admin, team, env):
    r = await client.post(
        "/api/workflow/tasks",
        json={"title": "ZZZ_TEST_WFWA Send call sheet", "assignee_id": team["id"]},
        headers=admin["headers"],
    )
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)  # drain the NEW TASK notification first

    r2 = await client.put(
        f"/api/workflow/tasks/{tid}",
        json={"subtasks": [{"id": "s1", "text": "Send final call sheet to production", "completed": False}]},
        headers=admin["headers"],
    )
    assert r2.status_code == 200

    jobs = await _jobs_for(tid)
    checklist_jobs = [j for j in jobs if "NEW CHECKLIST ITEM" in j["message_body"]]
    assert len(checklist_jobs) == 1
    body = checklist_jobs[0]["message_body"]
    assert "ZZZ_TEST_WFWA Send call sheet" in body
    assert "Send final call sheet to production" in body
    assert f"Added by: {admin['name']}" in body
    assert f"Assigned to: {team['name']}" in body
    assert f"?task={tid}" in body


# ---------------------------------------------------------------------------
# Task completion
# ---------------------------------------------------------------------------
@_aio
async def test_completing_task_generates_one_notification(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA complete me"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    r2 = await client.put(f"/api/workflow/tasks/{tid}", json={"status": "completed"}, headers=admin["headers"])
    assert r2.status_code == 200

    jobs = await _jobs_for(tid)
    completed_jobs = [j for j in jobs if "TASK COMPLETED" in j["message_body"]]
    assert len(completed_jobs) == 1
    body = completed_jobs[0]["message_body"]
    assert f"Completed by: {admin['name']}" in body
    assert "Completed:" in body


# ---------------------------------------------------------------------------
# Checklist completion
# ---------------------------------------------------------------------------
@_aio
async def test_completing_checklist_item_generates_one_notification(client, admin, env):
    r = await client.post(
        "/api/workflow/tasks",
        json={"title": "ZZZ_TEST_WFWA checklist complete", "subtasks": [{"id": "s1", "text": "Do the thing", "completed": False}]},
        headers=admin["headers"],
    )
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    r2 = await client.put(
        f"/api/workflow/tasks/{tid}",
        json={"subtasks": [{"id": "s1", "text": "Do the thing", "completed": True}]},
        headers=admin["headers"],
    )
    assert r2.status_code == 200

    jobs = await _jobs_for(tid)
    checklist_jobs = [j for j in jobs if "CHECKLIST COMPLETED" in j["message_body"]]
    assert len(checklist_jobs) == 1
    body = checklist_jobs[0]["message_body"]
    assert "Do the thing" in body
    assert f"Completed by: {admin['name']}" in body


# ---------------------------------------------------------------------------
# Task update
# ---------------------------------------------------------------------------
@_aio
async def test_assignment_only_change_sends_task_assigned(client, admin, team, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA assign only"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    r2 = await client.put(f"/api/workflow/tasks/{tid}", json={"assignee_id": team["id"]}, headers=admin["headers"])
    assert r2.status_code == 200

    jobs = await _jobs_for(tid)
    assigned_jobs = [j for j in jobs if "TASK ASSIGNED" in j["message_body"]]
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assert len(assigned_jobs) == 1
    assert len(updated_jobs) == 0  # no duplicate — assignment-only change is NOT also a generic update
    body = assigned_jobs[0]["message_body"]
    assert f"Assigned to: {team['name']}" in body
    assert "Previously: Unassigned" in body
    assert f"Assigned by: {admin['name']}" in body


@_aio
async def test_due_date_change_generates_notification(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA due change", "due_at": "2026-09-29T00:00:00+00:00"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    r2 = await client.put(f"/api/workflow/tasks/{tid}", json={"due_at": "2026-09-30T00:00:00+00:00"}, headers=admin["headers"])
    assert r2.status_code == 200

    jobs = await _jobs_for(tid)
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assert len(updated_jobs) == 1
    assert "Due date:" in updated_jobs[0]["message_body"]


@_aio
async def test_priority_change_generates_notification(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA priority change", "priority": "normal"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    r2 = await client.put(f"/api/workflow/tasks/{tid}", json={"priority": "high"}, headers=admin["headers"])
    jobs = await _jobs_for(tid)
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assert len(updated_jobs) == 1
    assert "Priority: Normal → High" in updated_jobs[0]["message_body"]


@_aio
async def test_title_change_generates_notification(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA old title"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    await client.put(f"/api/workflow/tasks/{tid}", json={"title": "ZZZ_TEST_WFWA new title"}, headers=admin["headers"])
    jobs = await _jobs_for(tid)
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assert len(updated_jobs) == 1
    assert "Title: ZZZ_TEST_WFWA old title → ZZZ_TEST_WFWA new title" in updated_jobs[0]["message_body"]


@_aio
async def test_status_reopen_generates_notification_not_completed(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA reopen"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await client.put(f"/api/workflow/tasks/{tid}", json={"status": "completed"}, headers=admin["headers"])
    await _jobs_for(tid)

    await client.put(f"/api/workflow/tasks/{tid}", json={"status": "pending"}, headers=admin["headers"])
    jobs = await _jobs_for(tid)
    completed_jobs = [j for j in jobs if "TASK COMPLETED" in j["message_body"]]
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assert len(completed_jobs) == 0  # reopening is not a completion event
    assert len(updated_jobs) == 1
    assert "Status: Completed → Pending" in updated_jobs[0]["message_body"]


@_aio
async def test_multiple_simultaneous_changes_grouped_into_one_notification(client, admin, team, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA multi change", "priority": "normal", "due_at": "2026-09-29T00:00:00+00:00"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    await client.put(
        f"/api/workflow/tasks/{tid}",
        json={"assignee_id": team["id"], "due_at": "2026-09-30T00:00:00+00:00", "priority": "high"},
        headers=admin["headers"],
    )
    jobs = await _jobs_for(tid)
    updated_jobs = [j for j in jobs if "TASK UPDATED" in j["message_body"]]
    assigned_jobs = [j for j in jobs if "TASK ASSIGNED" in j["message_body"]]
    assert len(updated_jobs) == 1  # ONE consolidated notification
    assert len(assigned_jobs) == 0  # assignment folded into the update, not a separate message
    body = updated_jobs[0]["message_body"]
    assert f"Assigned to: Unassigned → {team['name']}" in body
    assert "Due date:" in body
    assert "Priority: Normal → High" in body


# ---------------------------------------------------------------------------
# No-op changes
# ---------------------------------------------------------------------------
@_aio
async def test_saving_unchanged_task_sends_no_notification(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA noop", "priority": "high"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    await _jobs_for(tid)

    await client.put(f"/api/workflow/tasks/{tid}", json={"title": "ZZZ_TEST_WFWA noop", "priority": "high"}, headers=admin["headers"])
    jobs = await _jobs_for(tid)
    assert len(jobs) == 0


# ---------------------------------------------------------------------------
# Duplicate protection
# ---------------------------------------------------------------------------
@_aio
async def test_one_mutation_produces_exactly_one_job_row_in_db(client, admin, env):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA one mutation"}, headers=admin["headers"])
    tid = r.json()["id"]
    env.append(tid)
    jobs = await _jobs_for(tid)
    assert len(jobs) == 1
    batch_ids = {j["batch_id"] for j in jobs}
    assert len(batch_ids) == 1  # one batch too — no double-enqueue


# ---------------------------------------------------------------------------
# Failure handling — WhatsApp/notification errors never fail the mutation
# ---------------------------------------------------------------------------
@_aio
async def test_task_mutation_succeeds_even_if_notification_enqueue_fails(client, admin, monkeypatch, env):
    import routers.workflow as workflow_module

    async def _boom(*a, **kw):
        raise RuntimeError("simulated Mongo outage")

    monkeypatch.setattr(workflow_module, "_enqueue_workflow_whatsapp_notification_task", _boom)

    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA resilient"}, headers=admin["headers"])
    assert r.status_code == 200  # task creation itself must still succeed
    tid = r.json()["id"]
    env.append(tid)

    stored = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0, "title": 1})
    assert stored is not None
    assert stored["title"] == "ZZZ_TEST_WFWA resilient"


# ---------------------------------------------------------------------------
# Group configuration
# ---------------------------------------------------------------------------
@_aio
async def test_custom_group_name_config_is_respected(client, admin, env):
    await db.whatsapp_config.update_one(
        {"key": "workflow_notification_group_name"},
        {"$set": {"key": "workflow_notification_group_name", "value": "ZZZ_TEST_WFWA Custom Group"}},
        upsert=True,
    )
    try:
        r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_WFWA custom group"}, headers=admin["headers"])
        tid = r.json()["id"]
        env.append(tid)
        jobs = await _jobs_for(tid)
        assert jobs[0]["destination"] == "ZZZ_TEST_WFWA Custom Group"
    finally:
        await db.whatsapp_config.delete_one({"key": "workflow_notification_group_name"})
