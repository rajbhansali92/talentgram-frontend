"""Bulk project status — POST /api/projects/bulk-status.

Contract: sets ONLY `status` on the selected projects (ongoing / hold / complete / locked); every other field is
byte-identical afterwards; admin and team may use it (same as PUT /projects/{pid}), nobody else; unknown / soft-deleted
ids are reported as failed and never touched or revived; projects already in the status are reported as unchanged;
invalid status / empty / oversized requests are rejected without writing anything.
Real app + real local dev Mongo; throwaway `ZZZ_TEST_BS` rows are removed afterwards."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
import uuid

import httpx
import pytest
import pytest_asyncio

from core import db, _now, make_token, hash_password
from server import app

_aio = pytest.mark.asyncio(loop_scope="module")
pytestmark = _aio

TAG = "ZZZ_TEST_BS"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def admin(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _user(role):
    uid = f"zzz-test-bs-{role}-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    await db.users.insert_one({"id": uid, "email": email, "name": f"{TAG} {role}", "password_hash": hash_password("Password@123"),
                               "role": role, "status": "active", "token_version": 0})
    return uid, {"Authorization": f"Bearer {make_token({'id': uid, 'email': email, 'role': role, 'tv': 0})}"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team():
    uid, h = await _user("team")
    yield h
    await db.users.delete_one({"id": uid})


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def viewer():
    uid, h = await _user("viewer")
    yield h
    await db.users.delete_one({"id": uid})


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def cleanup():
    yield
    await db.projects.delete_many({"brand_name": {"$regex": f"^{TAG}"}})


async def _project(status="ongoing", **extra):
    pid = str(uuid.uuid4())
    doc = {"id": pid, "slug": f"zzz-test-bs-{pid[:8]}", "brand_name": f"{TAG} {pid[:6]}", "status": status, "character": "Mother",
           "shoot_dates": "28th oct", "budget_per_day": "50k", "commission_percent": "20%",
           "talent_budget": [{"label": " raw ", "amount": "  100 "}], "custom_questions": [{"id": "q1", "question": "?"}],
           "materials": [{"id": "m1"}], "created_at": _now(), "pd_shoot_date": "2026-10-28", "whatsapp_casting_group_name": "G"}
    doc.update(extra)
    await db.projects.insert_one(dict(doc))
    return doc


async def _row(pid):
    return await db.projects.find_one({"id": pid}, {"_id": 0})


async def test_sets_status_on_every_selected_project_across_statuses_and_changes_nothing_else(client, admin):
    a, b, c, d = await _project("ongoing"), await _project("hold"), await _project("complete"), await _project("locked")
    bystander = await _project("ongoing")
    before = {p["id"]: copy.deepcopy(await _row(p["id"])) for p in (a, b, c, d, bystander)}
    for target in ("complete", "hold", "locked", "ongoing"):
        r = await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [a["id"], b["id"], c["id"], d["id"]], "status": target})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == target and out["failed"] == []
        assert sorted(out["updated"] + out["unchanged"]) == sorted([a["id"], b["id"], c["id"], d["id"]])
        for p in (a, b, c, d):
            assert (await _row(p["id"]))["status"] == target
    # only `status` ever changed; an unselected project was never touched
    for p in (a, b, c, d):
        now = await _row(p["id"]); was = before[p["id"]]
        assert {k: v for k, v in now.items() if k != "status"} == {k: v for k, v in was.items() if k != "status"}
    assert await _row(bystander["id"]) == before[bystander["id"]]


async def test_reports_unchanged_and_changed_separately(client, admin):
    a, b = await _project("complete"), await _project("ongoing")
    r = (await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [a["id"], b["id"]], "status": "complete"})).json()
    assert r["updated"] == [b["id"]] and r["unchanged"] == [a["id"]] and r["failed"] == []


async def test_unknown_and_deleted_projects_are_failed_never_revived(client, admin):
    ok = await _project("ongoing")
    gone = await _project("deleted", lifecycle_state="deleted", deleted_at=_now())
    before_gone = await _row(gone["id"])
    r = await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [ok["id"], gone["id"], "does-not-exist"], "status": "hold"})
    assert r.status_code == 200
    out = r.json()
    assert out["updated"] == [ok["id"]]
    assert sorted(f["id"] for f in out["failed"]) == sorted([gone["id"], "does-not-exist"])
    assert (await _row(ok["id"]))["status"] == "hold"
    assert await _row(gone["id"]) == before_gone                                   # still deleted, untouched


async def test_invalid_input_is_rejected_without_writing(client, admin):
    p = await _project("ongoing")
    for body in ({"ids": [p["id"]], "status": "archived"}, {"ids": [p["id"]], "status": "deleted"}, {"ids": [p["id"]], "status": ""},
                 {"ids": [], "status": "hold"}, {"ids": [""], "status": "hold"}):
        assert (await client.post("/api/projects/bulk-status", headers=admin, json=body)).status_code == 400, body
    assert (await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [p["id"]]})).status_code == 422   # status required
    assert (await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [f"x{i}" for i in range(501)], "status": "hold"})).status_code == 400
    assert (await _row(p["id"]))["status"] == "ongoing"


async def test_duplicates_in_ids_are_collapsed(client, admin):
    p = await _project("ongoing")
    out = (await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [p["id"], p["id"], p["id"]], "status": "locked"})).json()
    assert out["updated"] == [p["id"]]


async def test_permissions_match_the_single_project_update(client, admin, team, viewer):
    p = await _project("ongoing")
    # same dependency as PUT /projects/{pid}: team may, anonymous / non-staff may not
    assert (await client.post("/api/projects/bulk-status", json={"ids": [p["id"]], "status": "hold"})).status_code in (401, 403)
    assert (await client.post("/api/projects/bulk-status", headers=viewer, json={"ids": [p["id"]], "status": "hold"})).status_code in (401, 403)
    assert (await _row(p["id"]))["status"] == "ongoing"
    r = await client.post("/api/projects/bulk-status", headers=team, json={"ids": [p["id"]], "status": "hold"})
    assert r.status_code == 200 and (await _row(p["id"]))["status"] == "hold"


async def test_status_changes_are_visible_in_the_projects_list_and_the_single_project_read(client, admin):
    p = await _project("ongoing")
    await client.post("/api/projects/bulk-status", headers=admin, json={"ids": [p["id"]], "status": "complete"})
    listed = {x["id"]: x for x in (await client.get("/api/projects", headers=admin)).json()}
    assert listed[p["id"]]["status"] == "complete"
    assert (await client.get(f"/api/projects/{p['id']}", headers=admin)).json()["status"] == "complete"
