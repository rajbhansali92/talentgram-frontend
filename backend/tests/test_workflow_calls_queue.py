"""Workflow → Calls: derived Follow-up, project/pipeline grouping, other-project context, shared call-outcome
sync, assignment timestamps + history, priorities, queue ordering, permissions, filters/pagination.

Pure rules (routers/workflow_calls_logic.py) are tested without a DB; the API behaviour runs against the real
app + local dev Mongo with `ZZZ_TEST_CQ` throwaway rows (same harness as test_workflow_calls.py)."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from core import db, _now, make_token, hash_password
from routers import workflow_calls_logic as L
from server import app
from test_workflow_calls import _Env, _row_for  # noqa: F401  (same helpers/index bootstrap as the base suite)

_aio = pytest.mark.asyncio(loop_scope="module")


# ═══ pure rules ═══════════════════════════════════════════════════════════════

def test_follow_up_is_the_pipeline_boards_derived_lane():
    assert L.derive_stage("follow_up", True) == "follow_up"            # stored "Reached Out"
    assert L.derive_stage("ask_to_test", False) == "follow_up"         # asked to test, no submission yet
    assert L.derive_stage("ask_to_test", True) == "ask_to_test"        # submitted -> not a follow-up
    for st in ("approved", "already_tested", "hold", "rejected"):
        assert L.derive_stage(st, False) == st                         # nothing else is ever rewritten


def _call(result, at):
    return {"call_result": result, "called_at": at}


def test_call_state_pending_attempted_completed():
    t0 = "2026-10-08T10:00:00+00:00"
    assert L.call_state(None, None) == "pending"
    assert L.call_state(_call("answered", "2026-10-08T11:00:00+00:00"), t0) == "completed"
    for r in ("no_answer", "busy", "switched_off", "call_back"):
        assert L.call_state(_call(r, "2026-10-08T11:00:00+00:00"), t0) == "attempted"      # reached nobody: still to call
    # a call made BEFORE the current assignment does not complete it
    assert L.call_state(_call("answered", "2026-10-08T09:00:00+00:00"), t0) == "pending"
    assert L.call_state(_call("answered", "2026-10-08T09:00:00+00:00"), None) == "completed"   # never assigned: any answer counts


def test_new_assignment_window():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    iso = lambda h: (now - timedelta(hours=h)).isoformat()
    assert L.is_new_assignment("pending", "u1", iso(2), now) is True
    assert L.is_new_assignment("pending", "u1", iso(30), now) is False             # older than 24h
    assert L.is_new_assignment("attempted", "u1", iso(2), now) is False            # already attempted
    assert L.is_new_assignment("completed", "u1", iso(2), now) is False
    assert L.is_new_assignment("pending", None, iso(2), now) is False              # nobody owns it


def _row(name, *, state="pending", prio="normal", to=None, at=None, activity=None, new=False, project="P", stage="follow_up"):
    return {"talent_id": name, "talent_name": name, "project_id": project, "project_name": project, "pipeline_stage": stage,
            "call_state": state, "priority": prio, "assigned_to_id": to, "assigned_at": at, "last_activity_at": activity or at, "is_new": new}


def _order(rows, viewer, admin):
    return [r["talent_name"] for r in L.sort_rows(rows, viewer, admin)]


def test_admin_queue_order():
    rows = [
        _row("done_old", state="completed", to="u1", at="2026-10-01T00:00:00+00:00", activity="2026-10-02T00:00:00+00:00"),
        _row("done_new", state="completed", to="u1", at="2026-10-01T00:00:00+00:00", activity="2026-10-08T00:00:00+00:00"),
        _row("unassigned"),
        _row("normal", to="u1", at="2026-10-05T00:00:00+00:00"),
        _row("semi", prio="semi_urgent", to="u1", at="2026-10-05T00:00:00+00:00"),
        _row("urgent_old", prio="urgent", to="u1", at="2026-10-04T00:00:00+00:00"),
        _row("urgent_recent", prio="urgent", to="u2", at="2026-10-06T00:00:00+00:00"),
        _row("new_normal", to="u2", at="2026-10-09T10:00:00+00:00", new=True),
        _row("attempted_urgent", state="attempted", prio="urgent", to="u1", at="2026-10-03T00:00:00+00:00"),
    ]
    assert _order(rows, "admin1", True) == [
        "new_normal",                                    # 1. newly assigned, still pending
        "urgent_recent", "urgent_old", "attempted_urgent",   # 2. urgent — newest assignment first
        "semi", "normal",                                # 3/4. semi-urgent then normal
        "unassigned",                                    # nobody owns it yet
        "done_new", "done_old",                          # 6. completed — latest activity first, always last
    ]


def test_team_member_queue_order_puts_my_new_calls_first_and_completed_last():
    me = "u1"
    rows = [
        _row("others_normal", to="u2", at="2026-10-05T00:00:00+00:00"),
        _row("others_urgent", prio="urgent", to="u2", at="2026-10-05T00:00:00+00:00"),
        _row("mine_normal", to=me, at="2026-10-05T00:00:00+00:00"),
        _row("mine_urgent", prio="urgent", to=me, at="2026-10-04T00:00:00+00:00"),
        _row("mine_new", to=me, at="2026-10-09T10:00:00+00:00", new=True),
        _row("unassigned"),
        _row("mine_done", state="completed", to=me, at="2026-10-01T00:00:00+00:00", activity="2026-10-09T00:00:00+00:00"),
    ]
    assert _order(rows, me, False) == ["mine_new", "mine_urgent", "mine_normal", "others_urgent", "others_normal", "unassigned", "mine_done"]


def test_ordering_is_deterministic_for_exact_ties():
    a, b = _row("Anna", to="u1", at="2026-10-05T00:00:00+00:00"), _row("Zoya", to="u1", at="2026-10-05T00:00:00+00:00")
    assert _order([b, a], "x", True) == _order([a, b], "x", True) == ["Anna", "Zoya"]


def test_grouping_is_project_then_pipeline_with_follow_up_first_and_counts():
    rows = L.sort_rows([
        _row("a", project="Tyaani", stage="approved", to="u1", at="2026-10-05T00:00:00+00:00"),
        _row("b", project="Tyaani", stage="follow_up", prio="urgent", to="u1", at="2026-10-06T00:00:00+00:00"),
        _row("c", project="Tyaani", stage="already_tested", state="completed", to="u1", at="2026-10-01T00:00:00+00:00"),
        _row("d", project="Zed", stage="follow_up"),
    ], "adm", True)
    groups = L.group_by_project(rows)
    assert [g["project_name"] for g in groups] == ["Tyaani", "Zed"]                    # the project holding the next call first
    ty = groups[0]
    assert [p["stage"] for p in ty["pipelines"]] == ["follow_up", "already_tested", "approved"]
    assert ty["counts"] == {"total": 3, "pending": 2, "attempted": 0, "completed": 1, "assigned": 3, "unassigned": 0, "urgent": 1, "new": 0}
    assert ty["next_call"]["talent_name"] == "b"
    assert groups[1]["pipelines"][0]["counts"]["unassigned"] == 1


# ═══ API ══════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def admin(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    body = r.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "id": body["admin"]["id"]}


async def _mk_team(name):
    uid = f"zzz-test-cq-team-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    await db.users.insert_one({"id": uid, "email": email, "name": name, "password_hash": hash_password("Password@123"), "role": "team", "status": "active", "token_version": 0})
    return {"id": uid, "headers": {"Authorization": f"Bearer {make_token({'id': uid, 'email': email, 'role': 'team', 'tv': 0})}"}}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team(client):
    t = await _mk_team("ZZZ_TEST_CQ Team A")
    yield t
    await db.users.delete_one({"id": t["id"]})


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team2(client):
    t = await _mk_team("ZZZ_TEST_CQ Team B")
    yield t
    await db.users.delete_one({"id": t["id"]})


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    e = _Env()
    e.submissions = []
    yield e
    await db.submissions.delete_many({"id": {"$in": e.submissions}})
    await db.talents.delete_many({"id": {"$in": getattr(e, "extra_talents", [])}})
    await e.cleanup()


async def _submit(env, project_id, talent_id):
    sid = str(uuid.uuid4())
    await db.submissions.insert_one({"id": sid, "project_id": project_id, "talent_id": talent_id, "created_at": _now()})
    env.submissions.append(sid)


async def _list(client, headers, project_ids, **params):
    p = {"project_ids": ",".join(project_ids), **params}
    r = await client.get("/api/workflow/calls", params=p, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _record(client, headers, talent_id, project_id, result="answered", **kw):
    r = await client.post("/api/workflow/calls", json={"id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id, "call_result": result, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _assign(client, headers, pairs, user_id, **kw):
    r = await client.post("/api/workflow/calls/assign", json={"pairs": [{"talent_id": t, "project_id": p} for t, p in pairs], "assigned_to_id": user_id, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Follow-up ──────────────────────────────────────────────────────────────

@_aio
async def test_follow_up_is_visible_derived_from_the_pipeline_boards_own_rule(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CQ Follow")
    stored, untested, submitted, approved = [await env.talent(name=f"ZZZ_TEST_CQ F{i}") for i in range(4)]
    await env.pipeline(p["id"], stored["id"], "follow_up")
    await env.pipeline(p["id"], untested["id"], "ask_to_test")           # no submission -> Follow-up
    await env.pipeline(p["id"], submitted["id"], "ask_to_test")
    await _submit(env, p["id"], submitted["id"])                          # submitted -> stays Ask to test
    await env.pipeline(p["id"], approved["id"], "approved")
    rows = (await _list(client, admin["headers"], [p["id"]]))["rows"]
    by = {r["talent_id"]: r for r in rows}
    assert by[stored["id"]]["pipeline_stage"] == "follow_up" and by[stored["id"]]["raw_stage"] == "follow_up"
    assert by[untested["id"]]["pipeline_stage"] == "follow_up" and by[untested["id"]]["raw_stage"] == "ask_to_test"
    assert by[untested["id"]]["is_follow_up"] is True
    assert by[submitted["id"]]["pipeline_stage"] == "ask_to_test" and by[approved["id"]]["pipeline_stage"] == "approved"
    # the Pipeline filter can select Follow-up, and it returns the stored AND the derived rows
    got = {r["talent_id"] for r in (await _list(client, admin["headers"], [p["id"]], pipeline="follow_up"))["rows"]}
    assert got == {stored["id"], untested["id"]}
    # nothing was written back to the casting pipeline
    assert (await db.casting_pipeline.find_one({"project_id": p["id"], "talent_id": untested["id"]}))["stage"] == "ask_to_test"


# ── grouping, pagination, summary ──────────────────────────────────────────

@_aio
async def test_grouped_view_is_project_then_pipeline_and_paginates_by_project(client, admin, env):
    projects = [await env.project(name=f"ZZZ_TEST_CQ Group {i}") for i in range(3)]
    for i, p in enumerate(projects):
        for j, stage in enumerate(("follow_up", "approved", "already_tested")[: i + 1]):
            t = await env.talent(name=f"ZZZ_TEST_CQ G{i}{j}")
            await env.pipeline(p["id"], t["id"], stage)
    ids = [p["id"] for p in projects]
    body = await _list(client, admin["headers"], ids, view="grouped")
    assert len(body["projects"]) == 3 and body["summary"]["total"] == 6
    sizes = {g["project_name"]: [(l["stage"], l["counts"]["total"]) for l in g["pipelines"]] for g in body["projects"]}
    assert sizes["ZZZ_TEST_CQ Group 2"] == [("follow_up", 1), ("already_tested", 1), ("approved", 1)]       # Follow-up first
    page0 = await _list(client, admin["headers"], ids, view="grouped", size=2, page=0)
    page1 = await _list(client, admin["headers"], ids, view="grouped", size=2, page=1)
    assert [len(page0["projects"]), len(page1["projects"])] == [2, 1] and page0["has_more"] and not page1["has_more"]
    assert page0["total_projects"] == 3
    assert page0["summary"]["total"] == 6                                       # the summary is the whole filtered set, not the page
    # flat view paginates by row
    flat = await _list(client, admin["headers"], ids, size=4, page=1)
    assert len(flat["rows"]) == 2 and flat["total"] == 6 and not flat["has_more"]


# ── other projects for the same canonical talent ──────────────────────────

@_aio
async def test_context_lists_the_talents_other_ongoing_projects_with_state_and_owner(client, admin, team, env):
    p1, p2, p3, done = [await env.project(name=f"ZZZ_TEST_CQ Ctx {i}", status=("ongoing" if i < 3 else "complete")) for i in range(4)]
    t = await env.talent(name="ZZZ_TEST_CQ Multi")
    for p, st in ((p1, "follow_up"), (p2, "approved"), (p3, "already_tested"), (done, "approved")):
        await env.pipeline(p["id"], t["id"], st)
    await _assign(client, admin["headers"], [(t["id"], p2["id"])], team["id"], priority="urgent")
    await _record(client, admin["headers"], t["id"], p3["id"], "no_answer", sync_other_projects=False)
    ctx = (await client.get(f"/api/workflow/calls/{t['id']}/{p1['id']}/context", headers=team["headers"])).json()
    assert ctx["current"]["project_id"] == p1["id"]
    others = {o["project_id"]: o for o in ctx["other_projects"]}
    assert set(others) == {p2["id"], p3["id"]}                                   # the completed project is not "ongoing"
    assert (others[p2["id"]]["pipeline_stage"], others[p2["id"]]["assigned_to_id"], others[p2["id"]]["priority"], others[p2["id"]]["call_state"]) == ("approved", team["id"], "urgent", "pending")
    assert (others[p3["id"]]["call_state"], others[p3["id"]]["last_call_result"]) == ("attempted", "no_answer")
    listed = _row_for((await _list(client, admin["headers"], [p1["id"], p2["id"], p3["id"]]))["rows"], t["id"], p1["id"])
    assert listed["other_projects_count"] == 2                                   # counted over ALL ongoing projects, not just the filtered ones
    assert (await client.get(f"/api/workflow/calls/{t['id']}/{done['id']}/context", headers=admin["headers"])).status_code == 404


@_aio
async def test_merged_duplicate_talent_resolves_to_one_person(client, admin, env):
    p1, p2 = await env.project(name="ZZZ_TEST_CQ Merge A"), await env.project(name="ZZZ_TEST_CQ Merge B")
    canon = await env.talent(name="ZZZ_TEST_CQ Canonical", phone="+911234500123")
    ghost_id = str(uuid.uuid4())
    await db.talents.insert_one({"id": ghost_id, "name": "ZZZ_TEST_CQ Canonical (dup)", "status": "MERGED", "merged_into": canon["id"], "email": f"{ghost_id}@example.com"})
    env.extra_talents = [ghost_id]
    await env.pipeline(p1["id"], canon["id"], "approved")
    await env.pipeline(p2["id"], ghost_id, "follow_up")                 # a pipeline row still pointing at the absorbed id
    await db.talent_project_calls.insert_one({"id": str(uuid.uuid4()), "talent_id": ghost_id, "project_id": p2["id"], "called_by": admin["id"], "called_at": _now(), "call_result": "answered", "created_at": _now()})
    rows = (await _list(client, admin["headers"], [p1["id"], p2["id"]]))["rows"]
    assert sorted(r["talent_id"] for r in rows) == [canon["id"], canon["id"]]          # never two people, never the absorbed id
    b = _row_for(rows, canon["id"], p2["id"])
    assert b["call_state"] == "completed" and b["last_call_result"] == "answered"       # the absorbed id's history is not lost
    ctx = (await client.get(f"/api/workflow/calls/{ghost_id}/{p1['id']}/context", headers=admin["headers"])).json()
    assert {o["project_id"] for o in ctx["other_projects"]} == {p2["id"]}              # asking by the absorbed id resolves the same person
    # recording for the canonical id works against a row stored under the absorbed id
    await _record(client, admin["headers"], canon["id"], p2["id"], "no_answer", sync_other_projects=False)


# ── shared call outcome ───────────────────────────────────────────────────

@_aio
async def test_recording_a_call_syncs_the_outcome_to_other_projects_but_never_pipeline_stages(client, admin, env):
    p1, p2, p3 = [await env.project(name=f"ZZZ_TEST_CQ Sync {i}") for i in range(3)]
    t = await env.talent(name="ZZZ_TEST_CQ Synced")
    stages = {p1["id"]: "follow_up", p2["id"]: "approved", p3["id"]: "ask_to_test"}
    for p, st in ((p1, "follow_up"), (p2, "approved"), (p3, "ask_to_test")):
        await env.pipeline(p["id"], t["id"], st)
    call = await _record(client, admin["headers"], t["id"], p1["id"], "answered", update_status="not_interested", update_text="busy shooting")
    assert {s["project_id"] for s in call["synced_projects"]} == {p2["id"], p3["id"]}
    assert {s["project_name"] for s in call["synced_projects"]} == {"ZZZ_TEST_CQ Sync 1", "ZZZ_TEST_CQ Sync 2"}
    # pipeline stages are project decisions: untouched everywhere, even for "not interested"
    for pid, st in stages.items():
        assert (await db.casting_pipeline.find_one({"project_id": pid, "talent_id": t["id"]}))["stage"] == st
    rows = {r["project_id"]: r for r in (await _list(client, admin["headers"], list(stages)))["rows"]}
    for pid in (p2["id"], p3["id"]):
        assert rows[pid]["last_call_result"] == "answered" and rows[pid]["last_update_status"] == "not_interested"
        assert rows[pid]["last_call_synced"] is True and rows[pid]["last_call_source_project"] == "ZZZ_TEST_CQ Sync 0"
        assert rows[pid]["call_state"] == "completed"
    assert rows[p1["id"]]["last_call_synced"] is False
    # the audit trail: each project's history shows the entry, flagged where it came from
    h2 = (await client.get(f"/api/workflow/calls/{t['id']}/{p2['id']}/history", headers=admin["headers"])).json()["history"]
    assert len(h2) == 1 and h2[0]["synced_from_project_name"] == "ZZZ_TEST_CQ Sync 0" and h2[0]["synced_from_call_id"] == call["id"]


@_aio
async def test_outcome_submission_is_idempotent_no_duplicate_history_anywhere(client, admin, env):
    p1, p2 = await env.project(name="ZZZ_TEST_CQ Idem 0"), await env.project(name="ZZZ_TEST_CQ Idem 1")
    t = await env.talent(name="ZZZ_TEST_CQ Idem")
    await env.pipeline(p1["id"], t["id"], "approved"); await env.pipeline(p2["id"], t["id"], "approved")
    payload = {"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p1["id"], "call_result": "no_answer"}
    first = (await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])).json()
    again = (await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])).json()
    again2 = (await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])).json()
    assert first["id"] == again["id"] == again2["id"] and again["synced_projects"] == first["synced_projects"]
    assert await db.talent_project_calls.count_documents({"talent_id": t["id"], "project_id": p1["id"]}) == 1
    assert await db.talent_project_calls.count_documents({"talent_id": t["id"], "project_id": p2["id"]}) == 1


@_aio
async def test_a_retry_completes_a_half_finished_sync_without_duplicating(client, admin, env):
    p1, p2 = await env.project(name="ZZZ_TEST_CQ Half 0"), await env.project(name="ZZZ_TEST_CQ Half 1")
    t = await env.talent(name="ZZZ_TEST_CQ Half")
    await env.pipeline(p1["id"], t["id"], "approved"); await env.pipeline(p2["id"], t["id"], "approved")
    payload = {"id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p1["id"], "call_result": "call_back"}
    await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])
    await db.talent_project_calls.delete_many({"project_id": p2["id"]})            # simulate the sync failing after the primary insert
    r = (await client.post("/api/workflow/calls", json=payload, headers=admin["headers"])).json()
    assert [s["project_id"] for s in r["synced_projects"]] == [p2["id"]]
    assert await db.talent_project_calls.count_documents({"project_id": p2["id"]}) == 1
    assert await db.talent_project_calls.count_documents({"project_id": p1["id"]}) == 1


@_aio
async def test_sync_can_be_turned_off_and_never_touches_non_ongoing_projects(client, admin, env):
    p1, p2, closed = await env.project(name="ZZZ_TEST_CQ Off 0"), await env.project(name="ZZZ_TEST_CQ Off 1"), await env.project(name="ZZZ_TEST_CQ Off closed", status="complete")
    t = await env.talent(name="ZZZ_TEST_CQ Off")
    for p in (p1, p2, closed):
        await env.pipeline(p["id"], t["id"], "approved")
    only = await _record(client, admin["headers"], t["id"], p1["id"], "no_answer", sync_other_projects=False)
    assert only["synced_projects"] == [] and await db.talent_project_calls.count_documents({"talent_id": t["id"], "project_id": p2["id"]}) == 0
    both = await _record(client, admin["headers"], t["id"], p1["id"], "busy")
    assert [s["project_id"] for s in both["synced_projects"]] == [p2["id"]]
    assert await db.talent_project_calls.count_documents({"project_id": closed["id"]}) == 0


# ── assignment: timestamps, history, priority ─────────────────────────────

@_aio
async def test_assignment_records_when_and_by_whom_and_defaults_to_normal(client, admin, team, env):
    p = await env.project(name="ZZZ_TEST_CQ Assign")
    t = await env.talent(name="ZZZ_TEST_CQ Assigned")
    await env.pipeline(p["id"], t["id"], "follow_up")
    before = datetime.now(timezone.utc)
    await _assign(client, admin["headers"], [(t["id"], p["id"])], team["id"])
    row = _row_for((await _list(client, admin["headers"], [p["id"]]))["rows"], t["id"], p["id"])
    assert row["assigned_to_id"] == team["id"] and row["priority"] == "normal"
    assert before <= datetime.fromisoformat(row["assigned_at"]) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert row["is_new"] is True and row["call_state"] == "pending" and row["assigned_by_name"]


@_aio
async def test_reassignment_resets_assigned_at_and_keeps_the_history(client, admin, team, team2, env):
    p = await env.project(name="ZZZ_TEST_CQ Reassign")
    t = await env.talent(name="ZZZ_TEST_CQ Reassigned")
    await env.pipeline(p["id"], t["id"], "approved")
    await _assign(client, admin["headers"], [(t["id"], p["id"])], team["id"], priority="urgent")
    first = await db.talent_project_call_assignments.find_one({"talent_id": t["id"], "project_id": p["id"]})
    await db.talent_project_call_assignments.update_one({"id": first["id"]}, {"$set": {"assigned_at": "2026-10-01T00:00:00+00:00"}})
    await _assign(client, admin["headers"], [(t["id"], p["id"])], team2["id"])         # priority kept, assignee + timestamp change
    doc = await db.talent_project_call_assignments.find_one({"id": first["id"]})
    assert doc["assigned_to_id"] == team2["id"] and doc["priority"] == "urgent"
    assert doc["assigned_at"] > "2026-10-02"
    kinds = [(h["type"], h.get("assigned_to_id")) for h in doc["assignment_history"]]
    assert kinds == [("assigned", team["id"]), ("assigned", team2["id"])]               # the previous assignment is not destroyed
    # a same-assignee re-save is a no-op: no new history, timestamp untouched
    stamp = doc["assigned_at"]
    assert (await _assign(client, admin["headers"], [(t["id"], p["id"])], team2["id"]))["updated"] == 0
    doc2 = await db.talent_project_call_assignments.find_one({"id": first["id"]})
    assert doc2["assigned_at"] == stamp and len(doc2["assignment_history"]) == 2
    await _assign(client, admin["headers"], [(t["id"], p["id"])], None)
    doc3 = await db.talent_project_call_assignments.find_one({"id": first["id"]})
    assert doc3["assigned_to_id"] is None and doc3["assigned_at"] is None and doc3["assignment_history"][-1]["type"] == "unassigned"


@_aio
async def test_a_legacy_assignment_keeps_its_real_timestamp_in_history_and_nothing_is_fabricated(client, admin, team, team2, env):
    p = await env.project(name="ZZZ_TEST_CQ Legacy")
    t = await env.talent(name="ZZZ_TEST_CQ Legacy T")
    await env.pipeline(p["id"], t["id"], "approved")
    await db.talent_project_call_assignments.insert_one({                          # the pre-priority shape: no priority, no history
        "id": str(uuid.uuid4()), "talent_id": t["id"], "project_id": p["id"], "assigned_to_id": team["id"],
        "assigned_by": admin["id"], "assigned_at": "2026-09-20T08:00:00+00:00", "updated_at": "2026-09-20T08:00:00+00:00"})
    row = _row_for((await _list(client, admin["headers"], [p["id"]]))["rows"], t["id"], p["id"])
    assert row["priority"] == "normal" and row["assigned_at"] == "2026-09-20T08:00:00+00:00" and row["is_new"] is False
    await _assign(client, admin["headers"], [(t["id"], p["id"])], team2["id"])
    doc = await db.talent_project_call_assignments.find_one({"talent_id": t["id"], "project_id": p["id"]})
    legacy = doc["assignment_history"][0]
    assert (legacy["assigned_to_id"], legacy["at"], legacy["legacy"]) == (team["id"], "2026-09-20T08:00:00+00:00", True)


@_aio
async def test_priorities_are_validated_persisted_and_permissioned(client, admin, team, team2, env):
    p = await env.project(name="ZZZ_TEST_CQ Prio")
    mine, theirs, free = [await env.talent(name=f"ZZZ_TEST_CQ Prio{i}") for i in range(3)]
    for t in (mine, theirs, free):
        await env.pipeline(p["id"], t["id"], "follow_up")
    await _assign(client, admin["headers"], [(mine["id"], p["id"])], team["id"])
    await _assign(client, admin["headers"], [(theirs["id"], p["id"])], team2["id"])
    bad = await client.post("/api/workflow/calls/assign", json={"pairs": [{"talent_id": mine["id"], "project_id": p["id"]}], "assigned_to_id": team["id"], "priority": "asap"}, headers=admin["headers"])
    assert bad.status_code == 400
    assert (await client.patch("/api/workflow/calls/priority", json={"pairs": [{"talent_id": mine["id"], "project_id": p["id"]}], "priority": "asap"}, headers=admin["headers"])).status_code == 400
    # a team member changes the priority of a call assigned to THEM
    r = await client.patch("/api/workflow/calls/priority", json={"pairs": [{"talent_id": mine["id"], "project_id": p["id"]}], "priority": "semi_urgent"}, headers=team["headers"])
    assert r.json() == {"updated": 1, "skipped": []}
    # ...but not someone else's, nor an unassigned one
    r = await client.patch("/api/workflow/calls/priority", json={"pairs": [{"talent_id": theirs["id"], "project_id": p["id"]}, {"talent_id": free["id"], "project_id": p["id"]}], "priority": "urgent"}, headers=team["headers"])
    assert r.json()["updated"] == 0 and len(r.json()["skipped"]) == 2
    # an admin can set any, including an unassigned call
    r = await client.patch("/api/workflow/calls/priority", json={"pairs": [{"talent_id": theirs["id"], "project_id": p["id"]}, {"talent_id": free["id"], "project_id": p["id"]}], "priority": "urgent"}, headers=admin["headers"])
    assert r.json() == {"updated": 2, "skipped": []}
    rows = {r["talent_id"]: r for r in (await _list(client, admin["headers"], [p["id"]]))["rows"]}      # persisted: read back fresh
    assert (rows[mine["id"]]["priority"], rows[theirs["id"]]["priority"], rows[free["id"]]["priority"]) == ("semi_urgent", "urgent", "urgent")
    assert rows[free["id"]]["assigned_to_id"] is None                          # setting a priority never assigns
    hist = (await db.talent_project_call_assignments.find_one({"talent_id": mine["id"], "project_id": p["id"]}))["assignment_history"]
    assert hist[-1] == {**hist[-1], "type": "priority_changed", "priority_from": "normal", "priority_to": "semi_urgent"}


@_aio
async def test_team_members_cannot_assign(client, team, env):
    p = await env.project(name="ZZZ_TEST_CQ NoAssign")
    t = await env.talent(name="ZZZ_TEST_CQ NoAssign T")
    await env.pipeline(p["id"], t["id"], "approved")
    r = await client.post("/api/workflow/calls/assign", json={"pairs": [{"talent_id": t["id"], "project_id": p["id"]}], "assigned_to_id": team["id"]}, headers=team["headers"])
    assert r.status_code == 403


# ── queue ordering through the API ────────────────────────────────────────

async def _queue_world(client, admin, team, team2, env):
    p = await env.project(name="ZZZ_TEST_CQ Queue")
    names = ["new_mine", "urgent_mine", "normal_mine", "urgent_other", "unassigned", "done_mine"]
    T = {n: await env.talent(name=f"ZZZ_TEST_CQ Q_{n}") for n in names}
    for t in T.values():
        await env.pipeline(p["id"], t["id"], "follow_up")
    P = p["id"]
    await _assign(client, admin["headers"], [(T["normal_mine"]["id"], P)], team["id"])
    await _assign(client, admin["headers"], [(T["urgent_mine"]["id"], P)], team["id"], priority="urgent")
    await _assign(client, admin["headers"], [(T["urgent_other"]["id"], P)], team2["id"], priority="urgent")
    await _assign(client, admin["headers"], [(T["done_mine"]["id"], P)], team["id"])
    # age the "old" assignments past the 24h "new" window; only new_mine stays new
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db.talent_project_call_assignments.update_many({"project_id": P, "talent_id": {"$in": [T[n]["id"] for n in ("normal_mine", "urgent_mine", "urgent_other", "done_mine")]}}, {"$set": {"assigned_at": old}})
    await _assign(client, admin["headers"], [(T["new_mine"]["id"], P)], team["id"])
    await _record(client, admin["headers"], T["done_mine"]["id"], P, "answered")
    return P, T


@_aio
async def test_admin_and_team_queues_are_ordered_for_their_role_with_completed_last(client, admin, team, team2, env):
    P, T = await _queue_world(client, admin, team, team2, env)
    nm = lambda body: [r["talent_name"].replace("ZZZ_TEST_CQ Q_", "") for r in body["rows"]]
    assert nm(await _list(client, admin["headers"], [P])) == ["new_mine", "urgent_mine", "urgent_other", "normal_mine", "unassigned", "done_mine"]
    assert nm(await _list(client, team["headers"], [P])) == ["new_mine", "urgent_mine", "normal_mine", "urgent_other", "unassigned", "done_mine"]
    body = await _list(client, team["headers"], [P])
    assert [r["call_state"] for r in body["rows"]][-1] == "completed" and body["rows"][-1]["talent_name"].endswith("done_mine")
    assert [r["talent_name"].replace("ZZZ_TEST_CQ Q_", "") for r in body["next_up"]][:2] == ["new_mine", "urgent_mine"]
    assert body["summary"] == {"total": 6, "pending": 5, "attempted": 0, "completed": 1, "assigned": 5, "unassigned": 1, "urgent": 2, "new": 1}


@_aio
async def test_filters_combine_priority_state_assignment_and_search(client, admin, team, team2, env):
    P, T = await _queue_world(client, admin, team, team2, env)
    ids = lambda body: sorted(r["talent_name"].replace("ZZZ_TEST_CQ Q_", "") for r in body["rows"])
    assert ids(await _list(client, admin["headers"], [P], priority="urgent")) == ["urgent_mine", "urgent_other"]
    assert ids(await _list(client, admin["headers"], [P], call_state="completed")) == ["done_mine"]
    assert ids(await _list(client, admin["headers"], [P], call_state="pending", assignment="unassigned")) == ["unassigned"]
    assert ids(await _list(client, team["headers"], [P], assignment="mine", priority="urgent")) == ["urgent_mine"]
    assert ids(await _list(client, admin["headers"], [P], search="q_normal", call_state="pending", pipeline="follow_up")) == ["normal_mine"]
    assert ids(await _list(client, admin["headers"], [P], assigned_to_id=team2["id"], priority="urgent")) == ["urgent_other"]
    assert ids(await _list(client, admin["headers"], [P], priority="normal", call_state="completed")) == ["done_mine"]
    assert (await _list(client, admin["headers"], [P], priority="urgent", call_state="completed"))["rows"] == []


# ── performance ───────────────────────────────────────────────────────────

@_aio
async def test_list_runs_a_fixed_number_of_queries_regardless_of_row_count(client, admin, team, env, monkeypatch):
    from motor.motor_asyncio import AsyncIOMotorCollection
    p1, p2 = await env.project(name="ZZZ_TEST_CQ Perf 0"), await env.project(name="ZZZ_TEST_CQ Perf 1")
    for i in range(24):
        t = await env.talent(name=f"ZZZ_TEST_CQ Perf T{i}")
        await env.pipeline(p1["id"], t["id"], "follow_up"); await env.pipeline(p2["id"], t["id"], "approved")
        if i % 3 == 0:
            await _assign(client, admin["headers"], [(t["id"], p1["id"])], team["id"])
    calls = {"n": 0}
    for name in ("find", "aggregate", "find_one", "count_documents"):
        orig = getattr(AsyncIOMotorCollection, name)
        def wrap(self, *a, _orig=orig, **k):
            calls["n"] += 1
            return _orig(self, *a, **k)
        monkeypatch.setattr(AsyncIOMotorCollection, name, wrap)
    body = await _list(client, team["headers"], [p1["id"], p2["id"]], view="grouped", size=5)
    assert body["summary"]["total"] == 48
    assert calls["n"] <= 12, f"{calls['n']} queries for 48 rows — must not grow with rows (no N+1)"
    n_before = calls["n"]
    ctx = (await client.get(f"/api/workflow/calls/{(await db.casting_pipeline.find_one({'project_id': p1['id']}))['talent_id']}/{p1['id']}/context", headers=team["headers"])).json()
    assert len(ctx["other_projects"]) == 1 and calls["n"] - n_before <= 12


@_aio
async def test_facets_are_optional_so_filter_changes_stay_light(client, admin, env):
    p = await env.project(name="ZZZ_TEST_CQ Facets")
    t = await env.talent(name="ZZZ_TEST_CQ Facet T")
    await env.pipeline(p["id"], t["id"], "follow_up")
    with_facets = await _list(client, admin["headers"], [p["id"]])
    assert {f["label"] for f in with_facets["facets"]["projects"]} >= {"ZZZ_TEST_CQ Facets"}
    without = await _list(client, admin["headers"], [p["id"]], include_facets="false")
    assert "facets" not in without and without["summary"]["total"] == 1
