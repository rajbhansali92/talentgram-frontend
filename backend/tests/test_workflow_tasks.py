"""Focused tests for the Production Management Desk's extension of the
EXISTING workflow tasks (routers/workflow.py, db.workflow_tasks) — the
additive talent_id/due_at/priority fields, the new project/talent-scoped
GET /workflow/tasks/production query, and the pre-existing create_task
crash this pass found and fixed while extending the endpoint.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
import pytest
import pytest_asyncio
import httpx
from server import app
from core import db, _now

_aio = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def headers(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _cleanup_tasks(*task_ids):
    if task_ids:
        await db.workflow_tasks.delete_many({"id": {"$in": list(task_ids)}})


# ---------------------------------------------------------------------------
# The pre-existing bug this pass found and fixed: insert_one() mutates the
# dict in place (adds `_id`), and create_task returned that same dict
# without stripping it — every call crashed JSON serialization. Confirmed
# present on main before this file existed (git stash verification during
# the session). This is the regression guard.
# ---------------------------------------------------------------------------
@_aio
async def test_create_task_does_not_crash_and_returns_no_id(client, headers):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD basic task", "category": "general"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "_id" not in body
    assert body["title"] == "ZZZ_TEST_PMD basic task"
    assert body["status"] == "pending"
    try:
        pass
    finally:
        await _cleanup_tasks(body["id"])


# ---------------------------------------------------------------------------
# Backward compatibility: a task created with NO talent_id/due_at/priority
# (every pre-existing caller) must round-trip exactly as before.
# ---------------------------------------------------------------------------
@_aio
async def test_existing_task_shape_unaffected_by_new_fields(client, headers):
    r = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD legacy task", "category": "scouting"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body.get("talent_id") is None
    assert body.get("due_at") is None
    assert body.get("priority") is None
    assert body["category"] == "scouting"

    r = await client.get("/api/workflow/tasks", headers=headers)
    assert r.status_code == 200
    assert any(t["id"] == body["id"] for t in r.json())

    try:
        pass
    finally:
        await _cleanup_tasks(body["id"])


# ---------------------------------------------------------------------------
# New fields: talent_id, due_at, priority — set on create, updatable.
# ---------------------------------------------------------------------------
@_aio
async def test_task_with_talent_id_due_at_and_priority(client, headers):
    pid = f"zzz-test-wf-proj-{uuid.uuid4().hex[:8]}"
    tid = f"zzz-test-wf-tal-{uuid.uuid4().hex[:8]}"
    due = "2026-09-10T12:00:00+00:00"
    r = await client.post(
        "/api/workflow/tasks",
        json={"title": "ZZZ_TEST_PMD Get call sheet", "category": "project", "project_id": pid, "talent_id": tid, "due_at": due, "priority": "high"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid
    assert body["talent_id"] == tid
    assert body["due_at"] == due
    assert body["priority"] == "high"

    r2 = await client.put(f"/api/workflow/tasks/{body['id']}", json={"priority": "low", "due_at": "2026-09-11T12:00:00+00:00"}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["priority"] == "low"
    assert r2.json()["due_at"] == "2026-09-11T12:00:00+00:00"

    try:
        pass
    finally:
        await _cleanup_tasks(body["id"])


# ---------------------------------------------------------------------------
# GET /workflow/tasks/production — project-scoped, talent-scoped, scope
# filters. Deliberately NOT scoped to assignee/creator (an operational
# view, unlike GET /tasks).
# ---------------------------------------------------------------------------
@_aio
async def test_production_scoped_query_requires_project_or_talent(client, headers):
    r = await client.get("/api/workflow/tasks/production", headers=headers)
    assert r.status_code == 400


@_aio
async def test_production_scoped_query_by_project(client, headers):
    pid = f"zzz-test-wf-proj-{uuid.uuid4().hex[:8]}"
    r1 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD A", "project_id": pid}, headers=headers)
    r2 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD B", "project_id": f"other-{uuid.uuid4().hex[:6]}"}, headers=headers)
    try:
        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid}, headers=headers)
        assert r.status_code == 200
        titles = [t["title"] for t in r.json()]
        assert "ZZZ_TEST_PMD A" in titles
        assert "ZZZ_TEST_PMD B" not in titles
    finally:
        await _cleanup_tasks(r1.json()["id"], r2.json()["id"])


@_aio
async def test_production_scoped_query_by_talent(client, headers):
    tid = f"zzz-test-wf-tal-{uuid.uuid4().hex[:8]}"
    r1 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD talent task", "talent_id": tid}, headers=headers)
    try:
        r = await client.get("/api/workflow/tasks/production", params={"talent_id": tid}, headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["talent_id"] == tid
    finally:
        await _cleanup_tasks(r1.json()["id"])


@_aio
async def test_production_scoped_query_scope_filters(client, headers):
    from datetime import datetime, timedelta, timezone
    pid = f"zzz-test-wf-proj-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    today_due = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    future_due = (now + timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    past_due = (now - timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()

    r1 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD today", "project_id": pid, "due_at": today_due}, headers=headers)
    r2 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD future", "project_id": pid, "due_at": future_due}, headers=headers)
    r3 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD overdue", "project_id": pid, "due_at": past_due}, headers=headers)
    r4 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD done", "project_id": pid, "due_at": today_due}, headers=headers)
    await client.put(f"/api/workflow/tasks/{r4.json()['id']}", json={"status": "completed"}, headers=headers)

    try:
        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid, "scope": "today"}, headers=headers)
        titles = [t["title"] for t in r.json()]
        assert "ZZZ_TEST_PMD today" in titles
        assert "ZZZ_TEST_PMD future" not in titles
        assert "ZZZ_TEST_PMD overdue" not in titles
        assert "ZZZ_TEST_PMD done" not in titles  # completed excluded even though due today

        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid, "scope": "upcoming"}, headers=headers)
        titles = [t["title"] for t in r.json()]
        assert "ZZZ_TEST_PMD future" in titles
        assert "ZZZ_TEST_PMD today" not in titles

        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid, "scope": "pending"}, headers=headers)
        titles = [t["title"] for t in r.json()]
        assert "ZZZ_TEST_PMD today" in titles
        assert "ZZZ_TEST_PMD future" in titles
        assert "ZZZ_TEST_PMD overdue" in titles
        assert "ZZZ_TEST_PMD done" not in titles

        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid, "scope": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup_tasks(r1.json()["id"], r2.json()["id"], r3.json()["id"], r4.json()["id"])


@_aio
async def test_production_scoped_query_not_limited_to_own_tasks(client, headers):
    """Unlike GET /tasks (assignee/creator scoped for non-admins), the
    production-scoped view is a project-wide operational read — this
    admin-token test can't easily prove the non-admin branch, but
    confirms the query never applies an assignee/creator filter itself."""
    pid = f"zzz-test-wf-proj-{uuid.uuid4().hex[:8]}"
    r1 = await client.post("/api/workflow/tasks", json={"title": "ZZZ_TEST_PMD unassigned", "project_id": pid, "assignee_id": "someone-else"}, headers=headers)
    try:
        r = await client.get("/api/workflow/tasks/production", params={"project_id": pid}, headers=headers)
        assert any(t["title"] == "ZZZ_TEST_PMD unassigned" for t in r.json())
    finally:
        await _cleanup_tasks(r1.json()["id"])
