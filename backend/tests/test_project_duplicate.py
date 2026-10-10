"""Duplicate project — POST /api/projects/{pid}/duplicate.

Contract: configuration + Submission Requirements + audition material are copied (assets by reference),
with a fresh id/slug; NOTHING operational is copied; the original is never modified; admin-only; a double
click or retry never creates two projects; a failure never leaves a half-built project.
Real app + real local dev Mongo; throwaway `ZZZ_TEST_DUP` rows are removed afterwards."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import copy
import uuid

import httpx
import pymongo
import pytest
import pytest_asyncio

from core import db, _now, make_token, hash_password
from server import app

_aio = pytest.mark.asyncio(loop_scope="module")

# the ASGI test client never runs server.py's startup, so make sure the slug index the race guard relies on exists
_c = pymongo.MongoClient(os.environ["MONGO_URL"])
try:
    _c[os.environ["DB_NAME"]]["projects"].create_index("slug", unique=True, name="proj_slug_unique")
except Exception:
    pass
_c.close()

# every collection that holds records keyed by project_id (from the production audit)
OPERATIONAL = [
    "casting_pipeline", "submissions", "submission_drafts", "talent_project_calls", "talent_project_call_assignments",
    "workflow_tasks", "project_reimbursements", "project_crew", "feedback", "media_assignments", "asset_metadata",
    "mark_intents", "media_sends", "form_sends", "send_form_approvals", "whatsapp_batches", "casting_desk_sessions",
    "submission_actions", "storage_audit_log",
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def admin(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def team():
    uid = f"zzz-test-dup-team-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@example.com"
    await db.users.insert_one({"id": uid, "email": email, "name": "ZZZ_TEST_DUP Team", "password_hash": hash_password("Password@123"), "role": "team", "status": "active", "token_version": 0})
    yield {"Authorization": f"Bearer {make_token({'id': uid, 'email': email, 'role': 'team', 'tv': 0})}"}
    await db.users.delete_one({"id": uid})


QUESTIONS = [
    {"id": "q1", "question": "Do you ride a bike?", "type": "yes_no", "required": True},
    {"id": "q2", "question": "Which bike?", "type": "text", "required": False},
]
REQUIREMENTS = {
    "strictness": "soft",
    "fields": {"name": "required", "email": "required", "phone": "required", "dob": "hidden", "height": "optional"},
    "custom_questions": {"q1": "required"},
    "intro_video": "required", "audition_takes_visibility": "required", "min_audition_takes": 2,
    "portfolio": {"indian": 3, "western": 2, "image": 1},
    "skills": {"language": False, "performance": False, "sports": True, "action": False, "vehicle": True},
    "conditional_rules": [{"id": "r1", "trigger_question_id": "q1", "trigger_value": "Yes", "required_videos": [{"label": "Bike clip"}]}],
}


def _material(cat, name, ext):
    mid = str(uuid.uuid4())
    return {"id": mid, "category": cat, "url": f"https://res.cloudinary.com/talentgram/raw/upload/v1/talentgram/projects/SRC/materials/{mid}.{ext}",
            "public_id": f"talentgram/projects/SRC/materials/{mid}.{ext}", "resource_type": "raw", "content_type": "application/x", "original_filename": name,
            "size": 1234, "created_at": "2026-01-01T00:00:00+00:00", "scope": "project_material", "project_id": "SRC"}


class _Env:
    def __init__(self):
        self.pids, self.tids = [], []

    async def source(self, **over):
        pid = str(uuid.uuid4())
        doc = {
            "id": pid, "slug": f"zzz-test-dup-{uuid.uuid4().hex[:12]}", "brand_name": "ZZZ_TEST_DUP Baskin Robbins Film 3", "status": "complete",
            "brand_link": "https://brand.example", "character": "Arjun & Kiran", "shoot_dates": "16th October", "budget_per_day": "25k max",
            "commission_percent": "15%", "medium_usage": "digital - 3 to 6months", "director": "D. Rector", "production_house": "PH Films",
            "additional_details": "Bring ID.", "video_links": ["https://youtu.be/abc"], "competitive_brand_enabled": True,
            "custom_questions": copy.deepcopy(QUESTIONS), "talent_budget": [{"label": "Day rate", "value": "25000"}], "client_budget": [{"label": "Total", "value": "90000"}],
            "require_reapproval_on_edit": False, "hide_budget_from_talent": True, "submission_requirements": copy.deepcopy(REQUIREMENTS),
            "whatsapp_casting_group_name": "ZZZ_TEST_DUP Group",
            "materials": [_material("script", "Film 3 - BR.pdf", "pdf"), _material("audio", "brief.mp3", "mp3"), _material("video_file", "ref.mp4", "mp4"),
                          _material("image", "look.jpg", "jpg"), _material("invoice", "invoice.pdf", "pdf")],
            # Production Desk state — operational
            "pd_payment_terms": "50/50", "pd_invoice_raised": True, "pd_production_budget_total": 90000, "pd_shoot_date": "2026-10-16", "pd_shoot_location": "Studio",
            "created_at": "2026-01-01T00:00:00+00:00", "created_by": "someone", "updated_at": "2026-02-02T00:00:00+00:00",
        }
        for m in doc["materials"]:
            m["project_id"] = pid
        doc.update(over)
        await db.projects.insert_one(doc)
        self.pids.append(pid)
        return pid

    async def operational(self, pid):
        """Populate the original with operational records in every collection that is keyed by project_id."""
        tid = str(uuid.uuid4())
        await db.talents.insert_one({"id": tid, "name": "ZZZ_TEST_DUP Talent", "email": f"{tid}@example.com"})
        self.tids.append(tid)
        for coll in OPERATIONAL:
            await db[coll].insert_one({"id": str(uuid.uuid4()), "project_id": pid, "talent_id": tid, "stage": "locked", "created_at": _now(), "zzz_test_dup": True})
        await db.links.insert_one({"id": str(uuid.uuid4()), "slug": f"zzz-test-dup-link-{uuid.uuid4().hex[:8]}", "auto_project_id": pid, "talent_ids": [tid], "title": "ZZZ_TEST_DUP review", "zzz_test_dup": True})
        return tid

    async def op_counts(self, pid):
        return {c: await db[c].count_documents({"project_id": pid}) for c in OPERATIONAL} | {"links": await db.links.count_documents({"auto_project_id": pid})}

    async def cleanup(self):
        await db.projects.delete_many({"$or": [{"id": {"$in": self.pids}}, {"duplicated_from_project_id": {"$in": self.pids}}]})
        for c in OPERATIONAL:
            await db[c].delete_many({"zzz_test_dup": True})
        await db.links.delete_many({"zzz_test_dup": True})
        await db.talents.delete_many({"id": {"$in": self.tids}})


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    e = _Env()
    yield e
    await e.cleanup()


async def _dup(client, headers, pid, **body):
    return await client.post(f"/api/projects/{pid}/duplicate", json=body, headers=headers)


# ═══ what is copied ═══════════════════════════════════════════════════════════

@_aio
async def test_duplicate_copies_configuration_requirements_questions_and_rules(client, admin, env):
    pid = await env.source()
    r = await _dup(client, admin, pid, name="ZZZ_TEST_DUP Baskin Robbins Film 4")
    assert r.status_code == 200, r.text
    d = r.json()
    src = await db.projects.find_one({"id": pid}, {"_id": 0})
    assert d["id"] != pid and d["slug"] != src["slug"]
    assert d["brand_name"] == "ZZZ_TEST_DUP Baskin Robbins Film 4"
    for k in ("brand_link", "character", "shoot_dates", "budget_per_day", "commission_percent", "medium_usage", "director", "production_house",
              "additional_details", "video_links", "competitive_brand_enabled", "talent_budget", "client_budget", "require_reapproval_on_edit", "hide_budget_from_talent"):
        assert d[k] == src[k], k
    assert d["custom_questions"] == QUESTIONS
    assert d["submission_requirements"] == REQUIREMENTS                        # strictness, field rules, skills, conditional rules, portfolio...
    assert d["submission_requirements"]["conditional_rules"][0]["trigger_question_id"] == "q1"
    stored = await db.projects.find_one({"id": d["id"]}, {"_id": 0})
    assert stored["submission_requirements"] == REQUIREMENTS and stored["created_by"] != "someone"


@_aio
async def test_duplicate_is_a_fresh_ongoing_project_without_routing_or_production_state(client, admin, env):
    pid = await env.source(status="complete")
    d = (await _dup(client, admin, pid)).json()
    assert d["brand_name"] == "ZZZ_TEST_DUP Baskin Robbins Film 3 (Copy)"      # sensible default
    assert d["status"] == "ongoing"                                              # never inherits complete/hold/locked
    assert d["whatsapp_casting_group_name"] is None                              # one group = one project: not shared
    assert not [k for k in d if k.startswith("pd_")]                              # Production Desk state stays with the original
    assert "lifecycle_state" not in d and "deleted_at" not in d and "updated_at" not in d
    assert d["duplicated_from_project_id"] == pid


@_aio
async def test_public_submission_link_is_new_and_resolves_to_the_duplicate(client, admin, env):
    pid = await env.source()
    d = (await _dup(client, admin, pid, name="ZZZ_TEST_DUP Link Check")).json()
    assert d["slug"].startswith("zzz-test-dup-link-check-") and len(d["slug"].rsplit("-", 1)[1]) == 12
    pub = (await client.get(f"/api/public/projects/{d['slug']}")).json()
    assert pub["id"] == d["id"] and pub["brand_name"] == "ZZZ_TEST_DUP Link Check"
    assert pub["submission_requirements"]["strictness"] == "soft" and len(pub["custom_questions"]) == 2
    assert (await client.get(f"/api/public/projects/{(await db.projects.find_one({'id': pid}))['slug']}")).json()["id"] == pid      # the original's link is untouched


# ═══ audition material ═══════════════════════════════════════════════════════

@_aio
async def test_audition_material_is_shared_by_reference_and_production_documents_are_not(client, admin, env):
    pid = await env.source()
    d = (await _dup(client, admin, pid)).json()
    src = {m["category"]: m for m in (await db.projects.find_one({"id": pid}))["materials"]}
    got = {m["category"]: m for m in d["materials"]}
    assert set(got) == {"script", "audio", "video_file", "image"}                # the invoice is operational
    assert d["duplicate"]["materials_shared"] == 4 and d["duplicate"]["materials_excluded"] == 1
    for cat, m in got.items():
        o = src[cat]
        assert (m["url"], m["public_id"], m["resource_type"], m["content_type"], m["original_filename"], m["size"]) == (o["url"], o["public_id"], o["resource_type"], o["content_type"], o["original_filename"], o["size"])
        assert m["id"] != o["id"] and m["project_id"] == d["id"] and m["scope"] == "project_material"
        assert m["copied_from"] == {"project_id": pid, "material_id": o["id"]}


@_aio
async def test_removing_a_material_from_the_duplicate_never_destroys_the_shared_asset(client, admin, env, monkeypatch):
    import core
    import cloudinary.uploader
    calls = []
    monkeypatch.setattr(core, "cloudinary_destroy", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(cloudinary.uploader, "destroy", lambda *a, **k: calls.append(a) or {"result": "ok"})
    pid = await env.source()
    d = (await _dup(client, admin, pid)).json()
    victim = d["materials"][0]
    assert (await client.delete(f"/api/projects/{d['id']}/material/{victim['id']}", headers=admin)).status_code == 200
    assert calls == []                                                           # unlinked only — the original still has its file
    orig = await db.projects.find_one({"id": pid}, {"_id": 0})
    assert any(m["public_id"] == victim["public_id"] for m in orig["materials"]) and len(orig["materials"]) == 5


# ═══ nothing operational is copied; the original is untouched ═════════════════

@_aio
async def test_no_talents_pipeline_or_operational_records_are_copied_and_the_original_is_unchanged(client, admin, env):
    pid = await env.source()
    await env.operational(pid)
    before_doc = await db.projects.find_one({"id": pid}, {"_id": 0})
    before_counts = await env.op_counts(pid)
    assert all(v >= 1 for v in before_counts.values()), before_counts            # the original really has data everywhere
    d = (await _dup(client, admin, pid)).json()
    assert await env.op_counts(d["id"]) == {k: 0 for k in before_counts}         # not one record, in any collection
    assert await env.op_counts(pid) == before_counts                             # and the original's are all still there
    assert await db.projects.find_one({"id": pid}, {"_id": 0}) == before_doc    # the original document is byte-for-byte unchanged
    # Casting Pipeline regression: the duplicate starts with ZERO talents
    pipe = (await client.get(f"/api/projects/{d['id']}/pipeline", headers=admin)).json()
    assert pipe["data"] == []
    assert len((await client.get(f"/api/projects/{pid}/pipeline", headers=admin)).json()["data"]) >= 1
    assert await db.submissions.count_documents({"project_id": d["id"]}) == 0
    # editing the duplicate leaves the original alone
    r = await client.put(f"/api/projects/{d['id']}", json={"brand_name": "Renamed", "director": "Someone Else"}, headers=admin)
    assert r.status_code == 200
    assert await db.projects.find_one({"id": pid}, {"_id": 0}) == before_doc


# ═══ authorization + validation ═══════════════════════════════════════════════

@_aio
async def test_only_admins_can_duplicate(client, team, env):
    pid = await env.source()
    assert (await client.post(f"/api/projects/{pid}/duplicate", json={})).status_code in (401, 403)       # no token
    assert (await _dup(client, team, pid)).status_code == 403                                              # team role
    assert await db.projects.count_documents({"duplicated_from_project_id": pid}) == 0


@_aio
async def test_unknown_deleted_and_invalid_inputs_are_rejected_without_creating_anything(client, admin, env):
    pid = await env.source()
    assert (await _dup(client, admin, str(uuid.uuid4()))).status_code == 404
    gone = await env.source(lifecycle_state="deleted", status="deleted")
    assert (await _dup(client, admin, gone)).status_code == 404                                            # a deleted project cannot be duplicated
    for bad in ({"name": "   "}, {"name": "x" * 201}, {"name": "bad\x00name"}, {"name": "ok", "request_id": "short"}, {"name": "ok", "request_id": "has spaces and !!"}):
        assert (await _dup(client, admin, pid, **bad)).status_code == 400, bad
    assert await db.projects.count_documents({"duplicated_from_project_id": {"$in": [pid, gone]}}) == 0
    assert (await _dup(client, admin, pid, name="  Spaced    Out   Name ")).json()["brand_name"] == "Spaced Out Name"


@_aio
async def test_a_failure_leaves_no_partial_project_behind(client, admin, env, monkeypatch):
    pid = await env.source()

    async def boom(self, *a, **k):
        raise RuntimeError("db went away")
    from motor.motor_asyncio import AsyncIOMotorCollection
    real = AsyncIOMotorCollection.insert_one
    monkeypatch.setattr(AsyncIOMotorCollection, "insert_one", boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c2:
        r = await c2.post(f"/api/projects/{pid}/duplicate", json={"name": "ZZZ_TEST_DUP doomed"}, headers=admin)
    monkeypatch.setattr(AsyncIOMotorCollection, "insert_one", real)
    assert r.status_code >= 500
    assert await db.projects.count_documents({"brand_name": "ZZZ_TEST_DUP doomed"}) == 0
    assert await db.projects.count_documents({"duplicated_from_project_id": pid}) == 0


# ═══ double clicks / retries ═════════════════════════════════════════════════

@_aio
async def test_a_repeated_request_id_returns_the_same_project(client, admin, env):
    pid = await env.source()
    rid = uuid.uuid4().hex
    a = (await _dup(client, admin, pid, name="ZZZ_TEST_DUP Once", request_id=rid)).json()
    b = (await _dup(client, admin, pid, name="ZZZ_TEST_DUP Once", request_id=rid)).json()
    assert a["id"] == b["id"] and a["duplicate"]["already_created"] is False and b["duplicate"]["already_created"] is True
    assert await db.projects.count_documents({"duplicated_from_project_id": pid}) == 1
    c = (await _dup(client, admin, pid, name="ZZZ_TEST_DUP Once", request_id=uuid.uuid4().hex)).json()       # a deliberate second duplicate is allowed
    assert c["id"] != a["id"] and await db.projects.count_documents({"duplicated_from_project_id": pid}) == 2


@_aio
async def test_simultaneous_double_click_creates_exactly_one_project(client, admin, env):
    pid = await env.source()
    rid = uuid.uuid4().hex
    rs = await asyncio.gather(*[_dup(client, admin, pid, name="ZZZ_TEST_DUP Race", request_id=rid) for _ in range(6)])
    assert [r.status_code for r in rs] == [200] * 6
    assert len({r.json()["id"] for r in rs}) == 1
    assert await db.projects.count_documents({"duplicated_from_project_id": pid}) == 1
