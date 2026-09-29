"""Focused tests for Workflow Calls -> WhatsApp Talentgram Workflow group
notifications (routers/workflow_calls.py's assign_calls/create_call, wired
to the SAME shared formatter/enqueue infrastructure
routers/test_workflow_whatsapp_notifications.py already covers for Tasks —
same db.whatsapp_batches/db.whatsapp_jobs queue, same
recipient_kind="INTERNAL_GROUP", no second notification system).

No worker process runs in this suite, so a "pending" job here sends
nothing — same safety guarantee as the other WhatsApp-adjacent test files
in this repo.
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
    uid = f"zzz-test-cwa-team-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    name = "ZZZ_TEST_CWA Team Member"
    await db.users.insert_one({
        "id": uid, "email": email, "name": name,
        "password_hash": hash_password("Password@123"), "role": "team",
        "status": "active", "token_version": 0,
    })
    token = make_token({"id": uid, "email": email, "role": "team", "tv": 0})
    yield {"headers": {"Authorization": f"Bearer {token}"}, "id": uid, "name": name}
    await db.users.delete_one({"id": uid})


def _mk_project(*, status="ongoing", name="ZZZ_TEST_CWA Project"):
    pid = str(uuid.uuid4())
    return {"id": pid, "slug": f"zzz-test-cwa-{uuid.uuid4().hex[:10]}", "brand_name": name, "status": status}


def _mk_talent(*, name="ZZZ_TEST_CWA Talent"):
    tid = str(uuid.uuid4())
    return {"id": tid, "name": name, "phone": "", "alternate_contact_number": "", "email": f"zzz-test-cwa-{uuid.uuid4().hex[:8]}@example.com"}


class _Env:
    def __init__(self):
        self.project_ids = []
        self.talent_ids = []

    async def project(self, **kw):
        doc = _mk_project(**kw)
        await db.projects.insert_one(doc)
        self.project_ids.append(doc["id"])
        return doc

    async def talent(self, **kw):
        doc = _mk_talent(**kw)
        await db.talents.insert_one(doc)
        self.talent_ids.append(doc["id"])
        return doc

    async def pipeline(self, project_id, talent_id, stage="follow_up"):
        doc = {"id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id, "stage": stage, "created_at": _now(), "updated_at": _now()}
        await db.casting_pipeline.insert_one(doc)
        return doc

    async def cleanup(self):
        pairs = []
        if self.project_ids:
            await db.projects.delete_many({"id": {"$in": self.project_ids}})
            await db.casting_pipeline.delete_many({"project_id": {"$in": self.project_ids}})
            calls = await db.talent_project_calls.find({"project_id": {"$in": self.project_ids}}, {"_id": 0, "talent_id": 1, "project_id": 1}).to_list(500)
            assigns = await db.talent_project_call_assignments.find({"project_id": {"$in": self.project_ids}}, {"_id": 0, "talent_id": 1, "project_id": 1}).to_list(500)
            pairs = [(c["talent_id"], c["project_id"]) for c in calls] + [(a["talent_id"], a["project_id"]) for a in assigns]
            await db.talent_project_calls.delete_many({"project_id": {"$in": self.project_ids}})
            await db.talent_project_call_assignments.delete_many({"project_id": {"$in": self.project_ids}})
        if self.talent_ids:
            await db.talents.delete_many({"id": {"$in": self.talent_ids}})
        # Drain any never-asserted notification jobs this test created.
        for talent_id, project_id in set(pairs):
            await _jobs_for(talent_id, project_id)


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    e = _Env()
    yield e
    await e.cleanup()


async def _jobs_for(talent_id, project_id):
    """Waits for the fire-and-forget enqueue to land, then returns and
    DELETES every job seen so far for this (talent, project) pair — same
    drain pattern as test_workflow_whatsapp_notifications.py so a
    multi-step test doesn't see an earlier step's job leak into a later
    assertion."""
    await asyncio.sleep(0.2)
    source_id = f"{talent_id}:{project_id}"
    jobs = await db.whatsapp_jobs.find(
        {"source": "WORKFLOW_NOTIFICATION", "source_id": source_id}, {"_id": 0},
    ).sort("created_at", 1).to_list(50)
    ids = [j["id"] for j in jobs]
    batch_ids = [j["batch_id"] for j in jobs if j.get("batch_id")]
    if ids:
        await db.whatsapp_jobs.delete_many({"id": {"$in": ids}})
    if batch_ids:
        await db.whatsapp_batches.delete_many({"id": {"$in": batch_ids}})
    return jobs


# ---------------------------------------------------------------------------
# Call assignment
# ---------------------------------------------------------------------------
@_aio
async def test_call_assignment_generates_one_notification(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CWA Sawenchi")
    t = await env.talent(name="ZZZ_TEST_CWA Shivani Parihar")
    await env.pipeline(p["id"], t["id"])

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200

    jobs = await _jobs_for(t["id"], p["id"])
    assigned_jobs = [j for j in jobs if "CALL ASSIGNED" in j["message_body"]]
    assert len(assigned_jobs) == 1
    body = assigned_jobs[0]["message_body"]
    assert "Talent: ZZZ_TEST_CWA Shivani Parihar" in body
    assert "Project: ZZZ_TEST_CWA Sawenchi" in body
    assert f"Assigned to: {team['name']}" in body
    assert f"Assigned by: {admin['name']}" in body
    assert jobs[0]["recipient_kind"] == "INTERNAL_GROUP"
    assert jobs[0]["destination"] == "Talentgram Workflow"


# ---------------------------------------------------------------------------
# Call reassignment
# ---------------------------------------------------------------------------
@_aio
async def test_call_reassignment_generates_one_notification_not_two(client, admin, team, env):
    p = await env.project()
    t = await env.talent()
    await env.pipeline(p["id"], t["id"])

    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )
    await _jobs_for(t["id"], p["id"])  # drain the first CALL ASSIGNED

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200

    jobs = await _jobs_for(t["id"], p["id"])
    reassigned_jobs = [j for j in jobs if "CALL REASSIGNED" in j["message_body"]]
    assigned_jobs = [j for j in jobs if "CALL ASSIGNED" in j["message_body"] and "REASSIGNED" not in j["message_body"]]
    assert len(reassigned_jobs) == 1
    assert len(assigned_jobs) == 0  # no duplicate — reassignment does NOT also fire a plain "assigned" message
    body = reassigned_jobs[0]["message_body"]
    assert f"Previously assigned to: {admin['name']}" in body
    assert f"Now assigned to: {team['name']}" in body
    assert f"Updated by: {admin['name']}" in body


@_aio
async def test_reassigning_to_same_person_is_a_noop(client, admin, team, env):
    p = await env.project()
    t = await env.talent()
    await env.pipeline(p["id"], t["id"])

    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    await _jobs_for(t["id"], p["id"])

    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    jobs = await _jobs_for(t["id"], p["id"])
    assert len(jobs) == 0


# ---------------------------------------------------------------------------
# Call recorded
# ---------------------------------------------------------------------------
@_aio
async def test_call_recorded_generates_one_notification(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CWA Recorded Project")
    t = await env.talent(name="ZZZ_TEST_CWA Recorded Talent")
    await env.pipeline(p["id"], t["id"])

    call_id = str(uuid.uuid4())
    r = await client.post(
        "/api/workflow/calls",
        json={"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "answered", "update_status": "sending"},
        headers=admin["headers"],
    )
    assert r.status_code == 200

    jobs = await _jobs_for(t["id"], p["id"])
    recorded_jobs = [j for j in jobs if "CALL RECORDED" in j["message_body"]]
    assert len(recorded_jobs) == 1
    body = recorded_jobs[0]["message_body"]
    assert "Talent: ZZZ_TEST_CWA Recorded Talent" in body
    assert "Project: ZZZ_TEST_CWA Recorded Project" in body
    assert "Result: Answered" in body
    assert "Update: Sending" in body
    assert f"Recorded by: {admin['name']}" in body


@_aio
async def test_call_recorded_retry_does_not_duplicate_notification(client, admin, env):
    p = await env.project()
    t = await env.talent()
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())
    payload = {"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "no_answer"}

    r1 = await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])
    assert r1.status_code == 200
    r2 = await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])  # retry, same id
    assert r2.status_code == 200

    jobs = await _jobs_for(t["id"], p["id"])
    recorded_jobs = [j for j in jobs if "CALL RECORDED" in j["message_body"]]
    assert len(recorded_jobs) == 1  # the idempotent replay must NOT re-notify


# ---------------------------------------------------------------------------
# Project omission
# ---------------------------------------------------------------------------
def test_call_message_omits_project_when_absent():
    from routers.workflow_calls import _build_call_assigned_message, _build_call_recorded_message
    body = _build_call_assigned_message("Shivani Parihar", None, "Harshita", "Raj", _now())
    assert "Project:" not in body
    assert "—" not in body

    body2 = _build_call_recorded_message("Shivani Parihar", "", "answered", None, "Harshita", _now())
    assert "Project:" not in body2


# ---------------------------------------------------------------------------
# Team member (non-admin) can also trigger a CALL RECORDED notification
# ---------------------------------------------------------------------------
@_aio
async def test_team_member_recording_a_call_shows_correct_actor(client, admin, team, env):
    p = await env.project()
    t = await env.talent()
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())

    r = await client.post(
        "/api/workflow/calls",
        json={"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=team["headers"],
    )
    assert r.status_code == 200
    jobs = await _jobs_for(t["id"], p["id"])
    recorded_jobs = [j for j in jobs if "CALL RECORDED" in j["message_body"]]
    assert len(recorded_jobs) == 1
    assert f"Recorded by: {team['name']}" in recorded_jobs[0]["message_body"]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------
@_aio
async def test_call_mutation_succeeds_even_if_notification_enqueue_fails(client, admin, monkeypatch, env):
    import routers.workflow as workflow_module

    async def _boom(*a, **kw):
        raise RuntimeError("simulated Mongo outage")

    monkeypatch.setattr(workflow_module, "_enqueue_workflow_whatsapp_notification_task", _boom)

    p = await env.project()
    t = await env.talent()
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())

    r = await client.post(
        "/api/workflow/calls",
        json={"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=admin["headers"],
    )
    assert r.status_code == 200  # call recording itself must still succeed

    stored = await db.talent_project_calls.find_one({"id": call_id}, {"_id": 0, "call_result": 1})
    assert stored is not None and stored["call_result"] == "answered"


# ---------------------------------------------------------------------------
# Batch assignment (2026-09-29 fix) — one assignment ACTION (one POST
# /workflow/calls/assign request, regardless of how many pairs it
# contains) must produce exactly ONE WhatsApp notification job, not one
# per pair. Uses a broader drain (all pending WORKFLOW_NOTIFICATION jobs)
# since a batch notification's source_id is a synthetic "assign-batch:*"
# tag, not a single talent:project pair.
# ---------------------------------------------------------------------------
async def _drain_all_pending_jobs():
    await asyncio.sleep(0.2)
    jobs = await db.whatsapp_jobs.find(
        {"source": "WORKFLOW_NOTIFICATION"}, {"_id": 0},
    ).sort("created_at", 1).to_list(500)
    ids = [j["id"] for j in jobs]
    batch_ids = [j["batch_id"] for j in jobs if j.get("batch_id")]
    if ids:
        await db.whatsapp_jobs.delete_many({"id": {"$in": ids}})
    if batch_ids:
        await db.whatsapp_batches.delete_many({"id": {"$in": batch_ids}})
    return jobs


@_aio
async def test_ten_calls_one_assignment_produces_exactly_one_notification(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CWA Batch Project")
    talents = [await env.talent(name=f"ZZZ_TEST_CWA Batch Talent {i}") for i in range(10)]
    for t in talents:
        await env.pipeline(p["id"], t["id"])
    await _drain_all_pending_jobs()

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]} for t in talents], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 10

    # Call assignment RECORDS remain individual — 10 rows, one per pair.
    assignment_count = await db.talent_project_call_assignments.count_documents(
        {"project_id": p["id"], "talent_id": {"$in": [t["id"] for t in talents]}, "assigned_to_id": team["id"]},
    )
    assert assignment_count == 10

    jobs = await _drain_all_pending_jobs()
    assert len(jobs) == 1  # exactly ONE notification job for all 10
    body = jobs[0]["message_body"]
    assert "*CALLS ASSIGNED*" in body
    for t in talents:
        assert t["name"] in body
    assert body.count(p["brand_name"]) == 10  # every entry shows its project


@_aio
async def test_ten_calls_different_projects_one_assignment_one_notification(client, admin, team, env):
    projects = [await env.project(name=f"ZZZ_TEST_CWA Multi Project {i}") for i in range(3)]
    pairs = []
    talent_names = []
    for i in range(10):
        t = await env.talent(name=f"ZZZ_TEST_CWA Multi Talent {i}")
        p = projects[i % 3]
        await env.pipeline(p["id"], t["id"])
        pairs.append({"talent_id": t["id"], "project_id": p["id"]})
        talent_names.append((t["name"], p["brand_name"]))
    await _drain_all_pending_jobs()

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": pairs, "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200

    jobs = await _drain_all_pending_jobs()
    assert len(jobs) == 1  # exactly one notification, regardless of how many distinct projects
    body = jobs[0]["message_body"]
    for talent_name, project_name in talent_names:
        assert talent_name in body
        assert project_name in body


@_aio
async def test_two_separate_assignment_actions_produce_two_separate_notifications(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CWA Separate Actions")
    talents_a = [await env.talent(name=f"ZZZ_TEST_CWA SepA {i}") for i in range(3)]
    talents_b = [await env.talent(name=f"ZZZ_TEST_CWA SepB {i}") for i in range(4)]
    for t in talents_a + talents_b:
        await env.pipeline(p["id"], t["id"])
    await _drain_all_pending_jobs()

    # Action A: 3 calls.
    r1 = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]} for t in talents_a], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r1.status_code == 200
    jobs_a = await _drain_all_pending_jobs()
    assert len(jobs_a) == 1
    body_a = jobs_a[0]["message_body"]
    for t in talents_a:
        assert t["name"] in body_a
    for t in talents_b:
        assert t["name"] not in body_a  # action B's talents must NOT leak into action A's notification

    # Action B (a distinct later action): 4 calls.
    r2 = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]} for t in talents_b], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )
    assert r2.status_code == 200
    jobs_b = await _drain_all_pending_jobs()
    assert len(jobs_b) == 1  # a second, SEPARATE notification — not combined with action A
    body_b = jobs_b[0]["message_body"]
    for t in talents_b:
        assert t["name"] in body_b
    for t in talents_a:
        assert t["name"] not in body_b


@_aio
async def test_batch_assignment_omits_project_when_absent(client, admin, team, env):
    from routers.workflow_calls import _build_calls_assigned_batch_messages
    entries = [
        {"talent_name": "ZZZ_TEST_CWA No Project Talent 1", "project_name": None},
        {"talent_name": "ZZZ_TEST_CWA No Project Talent 2", "project_name": ""},
    ]
    msgs = _build_calls_assigned_batch_messages(entries, "Harshita", "Raj", _now())
    assert len(msgs) == 1
    assert "Project:" not in msgs[0]
    assert "—" not in msgs[0]


@_aio
async def test_batch_assignment_notification_failure_does_not_roll_back_assignments(client, admin, team, env, monkeypatch):
    import routers.workflow as workflow_module

    async def _boom(*a, **kw):
        raise RuntimeError("simulated Mongo outage")

    monkeypatch.setattr(workflow_module, "_enqueue_workflow_whatsapp_notification_task", _boom)

    p = await env.project()
    talents = [await env.talent(name=f"ZZZ_TEST_CWA Resilient {i}") for i in range(5)]
    for t in talents:
        await env.pipeline(p["id"], t["id"])

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]} for t in talents], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 5  # all 5 assignments succeed even though notification enqueue explodes

    assignment_count = await db.talent_project_call_assignments.count_documents(
        {"project_id": p["id"], "talent_id": {"$in": [t["id"] for t in talents]}, "assigned_to_id": team["id"]},
    )
    assert assignment_count == 5
