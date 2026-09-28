"""Focused tests for Workflow -> Calls (routers/workflow_calls.py).

Covers: ongoing-project-only scoping, one row per (talent, project) pair,
live (never-synced) pipeline stage, call-status buckets, filters/search,
idempotent call creation, invalid-input rejection, non-ongoing/not-in-
pipeline rejection, full never-overwritten call history, admin-only
assignment at the pair level (never global to a talent), and strict
server-side team-role scoping (never trusts the frontend).

Same in-process ASGI convention as tests/test_workflow_tasks.py (admin via
real login) combined with tests/test_rate_limiting_revocation.py's direct
db.users.insert_one + make_token() for a synthetic team-role user (no real
team credentials needed).
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import httpx

from server import app
from core import db, _now, make_token, hash_password

_aio = pytest.mark.asyncio(loop_scope="module")

# Normally created once by server.py's own startup (asyncio.gather'd
# _workflow_calls_indexes()) — but the ASGITransport test client never
# fires the FastAPI lifespan startup event, so the unique index this
# suite's idempotency test depends on wouldn't otherwise exist. Same
# established pattern as tests/test_mark_intent.py's own identical note.
import pymongo as _pymongo  # noqa: E402

_sync_client = _pymongo.MongoClient(os.environ["MONGO_URL"])
_sync_client[os.environ["DB_NAME"]]["talent_project_calls"].create_index("id", unique=True, name="uniq_call_id")
_sync_client[os.environ["DB_NAME"]]["talent_project_call_assignments"].create_index(
    [("talent_id", 1), ("project_id", 1)], unique=True, name="uniq_talent_project",
)
_sync_client.close()


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
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "id": body["admin"]["id"]}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team(client):
    uid = f"zzz-test-calls-team-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    await db.users.insert_one({
        "id": uid, "email": email, "name": "ZZZ_TEST_CALLS Team Member",
        "password_hash": hash_password("Password@123"), "role": "team",
        "status": "active", "token_version": 0,
    })
    token = make_token({"id": uid, "email": email, "role": "team", "tv": 0})
    yield {"headers": {"Authorization": f"Bearer {token}"}, "id": uid}
    await db.users.delete_one({"id": uid})


def _mk_project(*, status="ongoing", name="ZZZ_TEST_CALLS Project"):
    pid = str(uuid.uuid4())
    return {"id": pid, "slug": f"zzz-test-calls-{uuid.uuid4().hex[:10]}", "brand_name": name, "status": status}


def _mk_talent(*, name="ZZZ_TEST_CALLS Talent", phone="", alt=""):
    tid = str(uuid.uuid4())
    return {
        "id": tid, "name": name, "phone": phone, "alternate_contact_number": alt,
        "email": f"zzz-test-calls-{uuid.uuid4().hex[:8]}@example.com",
    }


def _mk_pipeline_row(project_id, talent_id, stage="ask_to_test"):
    return {
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "stage": stage, "created_at": _now(), "updated_at": _now(),
    }


class _Env:
    """Scratch bucket for one test's created docs + cleanup."""

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

    async def pipeline(self, project_id, talent_id, stage="ask_to_test"):
        doc = _mk_pipeline_row(project_id, talent_id, stage)
        await db.casting_pipeline.insert_one(doc)
        return doc

    async def cleanup(self):
        if self.project_ids:
            await db.projects.delete_many({"id": {"$in": self.project_ids}})
            await db.casting_pipeline.delete_many({"project_id": {"$in": self.project_ids}})
            await db.talent_project_calls.delete_many({"project_id": {"$in": self.project_ids}})
            await db.talent_project_call_assignments.delete_many({"project_id": {"$in": self.project_ids}})
        if self.talent_ids:
            await db.talents.delete_many({"id": {"$in": self.talent_ids}})


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    e = _Env()
    yield e
    await e.cleanup()


def _row_for(rows, talent_id, project_id):
    return next((r for r in rows if r["talent_id"] == talent_id and r["project_id"] == project_id), None)


# ---------------------------------------------------------------------------
# LIST — scoping to ongoing projects only, one row per pair
# ---------------------------------------------------------------------------
@_aio
async def test_list_excludes_non_ongoing_projects(client, admin, env):
    p_on = await env.project(status="ongoing", name="ZZZ_TEST_CALLS OnGoing")
    p_hold = await env.project(status="hold", name="ZZZ_TEST_CALLS Hold")
    t = await env.talent(name="ZZZ_TEST_CALLS Solo")
    await env.pipeline(p_on["id"], t["id"])
    await env.pipeline(p_hold["id"], t["id"])

    r = await client.get("/api/workflow/calls", headers=admin["headers"])
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert _row_for(rows, t["id"], p_on["id"]) is not None
    assert _row_for(rows, t["id"], p_hold["id"]) is None


@_aio
async def test_list_talent_in_multiple_ongoing_projects_gets_separate_rows(client, admin, env):
    p1 = await env.project(name="ZZZ_TEST_CALLS Multi A")
    p2 = await env.project(name="ZZZ_TEST_CALLS Multi B")
    t = await env.talent(name="ZZZ_TEST_CALLS Multi Talent")
    await env.pipeline(p1["id"], t["id"])
    await env.pipeline(p2["id"], t["id"])

    r = await client.get("/api/workflow/calls", headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t["id"], p1["id"]) is not None
    assert _row_for(rows, t["id"], p2["id"]) is not None


@_aio
async def test_list_never_called_row_shape(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS NeverCalled")
    t = await env.talent(name="ZZZ_TEST_CALLS Fresh", phone="+911111100001")
    await env.pipeline(p["id"], t["id"], stage="approved")

    r = await client.get("/api/workflow/calls", headers=admin["headers"])
    row = _row_for(r.json()["rows"], t["id"], p["id"])
    assert row is not None
    assert row["last_call_at"] is None
    assert row["call_status_bucket"] == "never"
    assert row["assigned_to_id"] is None
    assert row["assigned_to_name"] is None
    assert row["pipeline_stage"] == "approved"
    assert row["talent_phone"] == "+911111100001"  # needed for the row's own tel: Call button


# ---------------------------------------------------------------------------
# LIST — filters + search
# ---------------------------------------------------------------------------
@_aio
async def test_list_filter_by_project_ids(client, admin, env):
    p1 = await env.project(name="ZZZ_TEST_CALLS FilterA")
    p2 = await env.project(name="ZZZ_TEST_CALLS FilterB")
    t = await env.talent(name="ZZZ_TEST_CALLS FilterTalent")
    await env.pipeline(p1["id"], t["id"])
    await env.pipeline(p2["id"], t["id"])

    r = await client.get("/api/workflow/calls", params={"project_ids": p1["id"]}, headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t["id"], p1["id"]) is not None
    assert _row_for(rows, t["id"], p2["id"]) is None


@_aio
async def test_list_filter_by_talent_id(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS TalentFilterProj")
    t1 = await env.talent(name="ZZZ_TEST_CALLS TA")
    t2 = await env.talent(name="ZZZ_TEST_CALLS TB")
    await env.pipeline(p["id"], t1["id"])
    await env.pipeline(p["id"], t2["id"])

    r = await client.get("/api/workflow/calls", params={"talent_id": t1["id"]}, headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t1["id"], p["id"]) is not None
    assert _row_for(rows, t2["id"], p["id"]) is None


@_aio
async def test_list_filter_by_pipeline_stage(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS StageFilterProj")
    t1 = await env.talent(name="ZZZ_TEST_CALLS StageA")
    t2 = await env.talent(name="ZZZ_TEST_CALLS StageB")
    await env.pipeline(p["id"], t1["id"], stage="shortlisted")
    await env.pipeline(p["id"], t2["id"], stage="rejected")

    r = await client.get("/api/workflow/calls", params={"pipeline": "shortlisted"}, headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t1["id"], p["id"]) is not None
    assert _row_for(rows, t2["id"], p["id"]) is None


@_aio
async def test_list_search_matches_talent_project_and_phone(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS Nykaa Search")
    t = await env.talent(name="ZZZ_TEST_CALLS Searchable Talent", phone="+919999912345")
    await env.pipeline(p["id"], t["id"])

    for term in ("Searchable", "Nykaa Search", "912345"):
        r = await client.get("/api/workflow/calls", params={"search": term}, headers=admin["headers"])
        rows = r.json()["rows"]
        assert _row_for(rows, t["id"], p["id"]) is not None, f"search term {term!r} should match"

    r = await client.get("/api/workflow/calls", params={"search": "zzz-no-such-thing-xyz"}, headers=admin["headers"])
    assert _row_for(r.json()["rows"], t["id"], p["id"]) is None


# ---------------------------------------------------------------------------
# POST /calls — create, idempotency, validation, pipeline-membership guard
# ---------------------------------------------------------------------------
@_aio
async def test_create_call_happy_path_and_appears_as_latest(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS HappyPath")
    t = await env.talent(name="ZZZ_TEST_CALLS Happy Talent")
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())

    r = await client.post(
        "/api/workflow/calls",
        json={"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "answered", "update_status": "sending"},
        headers=admin["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == call_id
    assert body["call_result"] == "answered"
    assert body["update_status"] == "sending"
    assert body["called_by"] == admin["id"]
    assert body["called_at"]  # server-assigned, non-empty

    r2 = await client.get("/api/workflow/calls", headers=admin["headers"])
    row = _row_for(r2.json()["rows"], t["id"], p["id"])
    assert row["last_call_result"] == "answered"
    assert row["last_update_status"] == "sending"
    assert row["call_status_bucket"] == "today"


@_aio
async def test_create_call_idempotent_on_client_id(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS Idempotent")
    t = await env.talent(name="ZZZ_TEST_CALLS Idempotent Talent")
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())
    payload = {"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "no_answer"}

    r1 = await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])
    r2 = await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])
    assert r1.status_code == 200 and r2.status_code == 200

    # The DB is the source of truth for "did this create a second row" —
    # exactly one row for this id, no matter how many times Save is retried.
    count = await db.talent_project_calls.count_documents({"id": call_id})
    assert count == 1
    stored = await db.talent_project_calls.find_one({"id": call_id}, {"_id": 0, "called_at": 1})
    # Both responses must reflect that SAME single stored row's timestamp —
    # never a second, independently-generated one.
    assert r1.json()["called_at"] == stored["called_at"]
    assert r2.json()["called_at"] == stored["called_at"]


@_aio
async def test_create_call_rejects_invalid_call_result(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS BadResult")
    t = await env.talent(name="ZZZ_TEST_CALLS BadResult Talent")
    await env.pipeline(p["id"], t["id"])
    r = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "not-a-real-result"},
        headers=admin["headers"],
    )
    assert r.status_code == 400


@_aio
async def test_create_call_rejects_invalid_update_status(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS BadUpdate")
    t = await env.talent(name="ZZZ_TEST_CALLS BadUpdate Talent")
    await env.pipeline(p["id"], t["id"])
    r = await client.post(
        "/api/workflow/calls",
        json={
            "id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"],
            "call_result": "answered", "update_status": "not-a-real-status",
        },
        headers=admin["headers"],
    )
    assert r.status_code == 400


@_aio
async def test_create_call_rejects_non_ongoing_project(client, admin, env):
    p = await env.project(status="complete", name="ZZZ_TEST_CALLS Closed")
    t = await env.talent(name="ZZZ_TEST_CALLS Closed Talent")
    await env.pipeline(p["id"], t["id"])
    r = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=admin["headers"],
    )
    assert r.status_code == 400


@_aio
async def test_create_call_rejects_talent_not_in_pipeline(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS NoPipeline")
    t = await env.talent(name="ZZZ_TEST_CALLS NotInPipeline")
    # deliberately no env.pipeline() call
    r = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=admin["headers"],
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# History — full, newest-first, never overwritten
# ---------------------------------------------------------------------------
@_aio
async def test_call_history_is_full_and_newest_first(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS History")
    t = await env.talent(name="ZZZ_TEST_CALLS History Talent")
    await env.pipeline(p["id"], t["id"])

    ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, cid in enumerate(ids):
        await client.post(
            "/api/workflow/calls",
            json={"id": cid, "talent_id": t["id"], "project_id": p["id"], "call_result": "no_answer", "update_text": f"attempt {i}"},
            headers=admin["headers"],
        )

    r = await client.get(f"/api/workflow/calls/{t['id']}/{p['id']}/history", headers=admin["headers"])
    assert r.status_code == 200
    history = r.json()["history"]
    assert len(history) == 3
    assert [h["id"] for h in history] == list(reversed(ids))  # newest first == last inserted first
    assert {h["update_text"] for h in history} == {"attempt 0", "attempt 1", "attempt 2"}


# ---------------------------------------------------------------------------
# Assign — admin-only, pair-level (never global to a talent)
# ---------------------------------------------------------------------------
@_aio
async def test_assign_is_admin_only(client, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS AssignAuth")
    t = await env.talent(name="ZZZ_TEST_CALLS AssignAuth Talent")
    await env.pipeline(p["id"], t["id"])
    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=team["headers"],
    )
    assert r.status_code == 403


@_aio
async def test_assign_is_scoped_to_pair_not_whole_talent(client, admin, team, env):
    p1 = await env.project(name="ZZZ_TEST_CALLS PairScopeA")
    p2 = await env.project(name="ZZZ_TEST_CALLS PairScopeB")
    t = await env.talent(name="ZZZ_TEST_CALLS PairScope Talent")
    await env.pipeline(p1["id"], t["id"])
    await env.pipeline(p2["id"], t["id"])

    r = await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p1["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    r2 = await client.get("/api/workflow/calls", headers=admin["headers"])
    rows = r2.json()["rows"]
    assert _row_for(rows, t["id"], p1["id"])["assigned_to_id"] == team["id"]
    assert _row_for(rows, t["id"], p2["id"])["assigned_to_id"] is None


# ---------------------------------------------------------------------------
# Team-role visibility — broadened 2026-09-28: a team member sees the SAME
# rows an admin's default view would (no more hard "only my assigned rows"
# scoping), and can record a call / view history for ANY visible pair, not
# just ones assigned to them. Assignment itself (POST /assign) stays
# admin-only and untouched — see test_assign_is_admin_only above.
# ---------------------------------------------------------------------------
@_aio
async def test_team_member_sees_assigned_and_unassigned_and_other_assigned_rows(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS ScopeProj")
    t_mine = await env.talent(name="ZZZ_TEST_CALLS ScopeMine")
    t_other_assignee = await env.talent(name="ZZZ_TEST_CALLS ScopeOtherAssignee")
    t_unassigned = await env.talent(name="ZZZ_TEST_CALLS ScopeUnassigned")
    await env.pipeline(p["id"], t_mine["id"])
    await env.pipeline(p["id"], t_other_assignee["id"])
    await env.pipeline(p["id"], t_unassigned["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_mine["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_other_assignee["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )
    # t_unassigned stays unassigned.

    r = await client.get("/api/workflow/calls", headers=team["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t_mine["id"], p["id"]) is not None
    assert _row_for(rows, t_other_assignee["id"], p["id"]) is not None  # broadened: visible even though assigned to someone else
    assert _row_for(rows, t_unassigned["id"], p["id"]) is not None  # broadened: visible even though unassigned


@_aio
async def test_team_member_assignment_mine_filter_still_narrows_within_broadened_set(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS ScopeMineFilter")
    t_mine = await env.talent(name="ZZZ_TEST_CALLS ScopeMineFilterMine")
    t_other = await env.talent(name="ZZZ_TEST_CALLS ScopeMineFilterOther")
    await env.pipeline(p["id"], t_mine["id"])
    await env.pipeline(p["id"], t_other["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_mine["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )

    r = await client.get("/api/workflow/calls", params={"assignment": "mine"}, headers=team["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t_mine["id"], p["id"]) is not None
    assert _row_for(rows, t_other["id"], p["id"]) is None  # `assignment=mine` narrows even for a team member now


@_aio
async def test_team_member_can_create_call_and_view_history_on_unassigned_pair(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS ScopeWrite")
    t = await env.talent(name="ZZZ_TEST_CALLS ScopeWrite Talent")
    await env.pipeline(p["id"], t["id"])
    # never assigned to `team`

    r = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=team["headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["called_by"] == team["id"]

    r2 = await client.get(f"/api/workflow/calls/{t['id']}/{p['id']}/history", headers=team["headers"])
    assert r2.status_code == 200
    assert len(r2.json()["history"]) == 1


@_aio
async def test_team_member_can_record_call_on_pair_assigned_to_someone_else(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS ScopeOtherRecord")
    t = await env.talent(name="ZZZ_TEST_CALLS ScopeOtherRecord Talent")
    await env.pipeline(p["id"], t["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )

    r = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=team["headers"],
    )
    assert r.status_code == 200, r.text


@_aio
async def test_recording_a_call_never_changes_assigned_to(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS NoReassign")
    t = await env.talent(name="ZZZ_TEST_CALLS NoReassign Talent")
    await env.pipeline(p["id"], t["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )

    await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=team["headers"],
    )

    assignment_doc = await db.talent_project_call_assignments.find_one(
        {"talent_id": t["id"], "project_id": p["id"]}, {"_id": 0, "assigned_to_id": 1},
    )
    assert assignment_doc["assigned_to_id"] == admin["id"]  # untouched by a Record from a different user


@_aio
async def test_recording_a_call_creates_exactly_one_entry(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS OneEntry")
    t = await env.talent(name="ZZZ_TEST_CALLS OneEntry Talent")
    await env.pipeline(p["id"], t["id"])
    call_id = str(uuid.uuid4())

    r = await client.post(
        "/api/workflow/calls",
        json={"id": call_id, "talent_id": t["id"], "project_id": p["id"], "call_result": "answered", "update_text": "note"},
        headers=team["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["talent_id"] == t["id"] and body["project_id"] == p["id"] and body["update_text"] == "note"

    count = await db.talent_project_calls.count_documents({"talent_id": t["id"], "project_id": p["id"]})
    assert count == 1


@_aio
async def test_admin_visibility_and_record_unchanged_by_broadening(client, admin, env):
    """Admin behaviour must stay exactly as before — same rows, same
    ability to record/assign/view history for anything."""
    p = await env.project(name="ZZZ_TEST_CALLS AdminUnchanged")
    t = await env.talent(name="ZZZ_TEST_CALLS AdminUnchanged Talent")
    await env.pipeline(p["id"], t["id"])

    r = await client.get("/api/workflow/calls", headers=admin["headers"])
    assert _row_for(r.json()["rows"], t["id"], p["id"]) is not None

    r2 = await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered"},
        headers=admin["headers"],
    )
    assert r2.status_code == 200

    r3 = await client.get(f"/api/workflow/calls/{t['id']}/{p['id']}/history", headers=admin["headers"])
    assert r3.status_code == 200 and len(r3.json()["history"]) == 1


# ---------------------------------------------------------------------------
# "Assigned To" filter — new `assigned_to_id` query param (2026-09-28)
# ---------------------------------------------------------------------------
@_aio
async def test_filter_by_assigned_to_id_exact_user(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS AssignedToFilter")
    t_team = await env.talent(name="ZZZ_TEST_CALLS AssignedToFilterTeam")
    t_admin = await env.talent(name="ZZZ_TEST_CALLS AssignedToFilterAdmin")
    await env.pipeline(p["id"], t_team["id"])
    await env.pipeline(p["id"], t_admin["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_team["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_admin["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )

    r = await client.get("/api/workflow/calls", params={"assigned_to_id": team["id"]}, headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t_team["id"], p["id"]) is not None
    assert _row_for(rows, t_admin["id"], p["id"]) is None


@_aio
async def test_filter_by_assigned_to_id_unassigned(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS AssignedToUnassignedFilter")
    t_assigned = await env.talent(name="ZZZ_TEST_CALLS AssignedToUnassignedFilterAssigned")
    t_unassigned = await env.talent(name="ZZZ_TEST_CALLS AssignedToUnassignedFilterUnassigned")
    await env.pipeline(p["id"], t_assigned["id"])
    await env.pipeline(p["id"], t_unassigned["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_assigned["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )

    r = await client.get("/api/workflow/calls", params={"assigned_to_id": "unassigned"}, headers=admin["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t_unassigned["id"], p["id"]) is not None
    assert _row_for(rows, t_assigned["id"], p["id"]) is None


@_aio
async def test_filter_by_assigned_to_id_combines_with_project_and_pipeline_filters(client, admin, team, env):
    p1 = await env.project(name="ZZZ_TEST_CALLS AssignedToComboA")
    p2 = await env.project(name="ZZZ_TEST_CALLS AssignedToComboB")
    t1 = await env.talent(name="ZZZ_TEST_CALLS AssignedToComboT1")
    t2 = await env.talent(name="ZZZ_TEST_CALLS AssignedToComboT2")
    await env.pipeline(p1["id"], t1["id"], stage="shortlisted")
    await env.pipeline(p2["id"], t2["id"], stage="shortlisted")
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t1["id"], "project_id": p1["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t2["id"], "project_id": p2["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )

    r = await client.get(
        "/api/workflow/calls",
        params={"assigned_to_id": team["id"], "project_ids": p1["id"], "pipeline": "shortlisted"},
        headers=admin["headers"],
    )
    rows = r.json()["rows"]
    assert _row_for(rows, t1["id"], p1["id"]) is not None
    assert _row_for(rows, t2["id"], p2["id"]) is None  # excluded by project_ids, despite same assignee


@_aio
async def test_filter_by_assigned_to_id_available_to_team_member_too(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CALLS AssignedToTeamCaller")
    t_mine = await env.talent(name="ZZZ_TEST_CALLS AssignedToTeamCallerMine")
    t_other = await env.talent(name="ZZZ_TEST_CALLS AssignedToTeamCallerOther")
    await env.pipeline(p["id"], t_mine["id"])
    await env.pipeline(p["id"], t_other["id"])
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_mine["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]},
        headers=admin["headers"],
    )
    await client.post(
        "/api/workflow/calls/assign",
        json={"pairs": [{"talent_id": t_other["id"], "project_id": p["id"]}], "assigned_to_id": admin["id"]},
        headers=admin["headers"],
    )

    r = await client.get("/api/workflow/calls", params={"assigned_to_id": team["id"]}, headers=team["headers"])
    rows = r.json()["rows"]
    assert _row_for(rows, t_mine["id"], p["id"]) is not None
    assert _row_for(rows, t_other["id"], p["id"]) is None


# ---------------------------------------------------------------------------
# Decoupling — pipeline stage stays LIVE (never synced), and a call's
# update_status NEVER writes back to casting_pipeline.stage.
# ---------------------------------------------------------------------------
@_aio
async def test_pipeline_stage_shown_is_always_live(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS LiveStage")
    t = await env.talent(name="ZZZ_TEST_CALLS LiveStage Talent")
    await env.pipeline(p["id"], t["id"], stage="ask_to_test")

    await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered", "update_status": "sending"},
        headers=admin["headers"],
    )
    await db.casting_pipeline.update_one({"talent_id": t["id"], "project_id": p["id"]}, {"$set": {"stage": "shortlisted"}})

    r = await client.get("/api/workflow/calls", headers=admin["headers"])
    row = _row_for(r.json()["rows"], t["id"], p["id"])
    assert row["pipeline_stage"] == "shortlisted"  # reflects the direct pipeline mutation, not a stale snapshot


@_aio
async def test_call_update_status_never_mutates_pipeline_stage(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CALLS NoSync")
    t = await env.talent(name="ZZZ_TEST_CALLS NoSync Talent")
    await env.pipeline(p["id"], t["id"], stage="ask_to_test")

    await client.post(
        "/api/workflow/calls",
        json={"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "call_result": "answered", "update_status": "not_interested"},
        headers=admin["headers"],
    )

    pipeline_doc = await db.casting_pipeline.find_one({"talent_id": t["id"], "project_id": p["id"]}, {"_id": 0, "stage": 1})
    assert pipeline_doc["stage"] == "ask_to_test"  # untouched despite a "Not Interested" call update
