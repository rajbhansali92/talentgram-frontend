"""POST /api/whatsapp/casting-call/send — admin-selected template
(2026-09-27). Previously this endpoint always resolved the built-in
`casting_call` slug; the Global Talent -> Add to Project -> "Send Casting
Call?" flow now lets the admin pick any existing WhatsApp template first
(frontend: AddToProjectModal.jsx's new template-picker step), and this
endpoint sends whichever one was chosen. No new template store, no new
send path — reuses the existing `whatsapp_templates` collection and the
same _create_batch_internal/_render_message pipeline every other WhatsApp
send in this app already goes through.

Dispatches through the real FastAPI app (httpx.ASGITransport) against the
real local dev DB — no mocks — so a pass here is proof the endpoint, the
Pydantic model, and the render pipeline actually work together, not just
that some helper function returns the right dict. is_dry_run is never used
here on purpose: the point is to verify a REAL job actually gets queued
with the chosen template's rendered body, which only happens on a real
(non-dry-run) batch — this never talks to WhatsApp itself, only queues a
`whatsapp_jobs` doc for the (separate, offline-in-tests) worker to pick up.
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
        r = await c.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
        assert r.status_code == 200
        token = r.json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


async def _make_project(**overrides):
    pid = f"zzz-test-cc-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": f"ZZZ_TEST_CC_{uuid.uuid4().hex[:6]}", "slug": pid,
        "status": "ongoing", "shoot_dates": "TBD", "budget": "TBD",
        "submission_link": "", "audition_material_link": "",
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid, doc["brand_name"]


async def _make_talent_in_pipeline(pid, stage="ask_to_test", name=None):
    name = name or f"ZZZ_TEST_CC_Talent_{uuid.uuid4().hex[:6]}"
    tid = f"zzz-test-cc-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "email": f"{tid}@example.com", "phone": "911234500000",
        "tags": [], "media": [],
    })
    row_id = f"zzz-test-cc-row-{uuid.uuid4().hex[:8]}"
    await db.casting_pipeline.insert_one({
        "id": row_id, "project_id": pid, "talent_id": tid, "stage": stage,
        "created_at": _now(), "updated_at": _now(),
    })
    return tid, name


async def _make_custom_template(body_text, variables=None):
    tid = f"zzz-test-cc-tmpl-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": tid, "name": f"ZZZ_TEST_CC_Template_{uuid.uuid4().hex[:6]}",
        "slug": f"zzz_test_cc_{uuid.uuid4().hex[:6]}", "body_text": body_text,
        "variables": variables or [], "media_type": "none", "media_url": None,
        "media_cloudinary_id": None, "is_custom": True,
        "created_at": _now(), "updated_at": _now(),
    }
    await db.whatsapp_templates.insert_one(doc)
    return tid, doc


async def _cleanup(pid=None, talent_ids=None, template_id=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.whatsapp_batches.delete_many({"project_id": pid})
    if talent_ids:
        await db.talents.delete_many({"id": {"$in": talent_ids}})
        await db.whatsapp_jobs.delete_many({"talent_id": {"$in": talent_ids}})
    if template_id:
        await db.whatsapp_templates.delete_one({"id": template_id})


@_aio
async def test_template_id_required_no_silent_fallback(client):
    pid, label = await _make_project()
    tid, _ = await _make_talent_in_pipeline(pid)
    try:
        r = await client.post("/api/whatsapp/casting-call/send", json={"talent_ids": [tid], "project_ids": [pid]})
        assert r.status_code == 400
        assert "template" in r.json()["detail"].lower()
        # Nothing was queued.
        assert await db.whatsapp_jobs.count_documents({"talent_id": tid}) == 0
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_nonexistent_template_id_rejected(client):
    pid, label = await _make_project()
    tid, _ = await _make_talent_in_pipeline(pid)
    try:
        r = await client.post(
            "/api/whatsapp/casting-call/send",
            json={"talent_ids": [tid], "project_ids": [pid], "template_id": "does-not-exist"},
        )
        assert r.status_code == 404
        assert await db.whatsapp_jobs.count_documents({"talent_id": tid}) == 0
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_selected_non_default_template_is_actually_used_and_rendered(client):
    """Proves the existing render pipeline is reused unmodified for a
    template OTHER than the old hardcoded `casting_call` slug — the whole
    point of this feature."""
    pid, label = await _make_project(budget_per_day="₹50,000")
    tid, tname = await _make_talent_in_pipeline(pid)
    tmpl_id, tmpl_doc = await _make_custom_template(
        "Hi {{talent_name}}, budget for {{project_name}} is {{budget}}.",
        variables=["talent_name", "project_name", "budget"],
    )
    try:
        r = await client.post(
            "/api/whatsapp/casting-call/send",
            json={"talent_ids": [tid], "project_ids": [pid], "template_id": tmpl_id},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["success"] is True
        assert not body["errors"]

        job = await db.whatsapp_jobs.find_one({"talent_id": tid})
        assert job is not None
        assert job["template_id"] == tmpl_id
        assert job["message_body"] == f"Hi {tname}, budget for {label} is ₹50,000."
    finally:
        await _cleanup(pid, [tid], tmpl_id)


@_aio
async def test_explicit_casting_call_template_id_still_works_no_regression(client):
    """The old default behaviour (the built-in casting_call template) still
    works exactly as before — just via an explicit template_id now instead
    of a hardcoded slug lookup."""
    pid, label = await _make_project()
    tid, tname = await _make_talent_in_pipeline(pid)
    casting_call_template = await db.whatsapp_templates.find_one({"slug": "casting_call"}, {"_id": 0})
    assert casting_call_template is not None, "seeded casting_call template must exist"
    try:
        r = await client.post(
            "/api/whatsapp/casting-call/send",
            json={"talent_ids": [tid], "project_ids": [pid], "template_id": casting_call_template["id"]},
        )
        assert r.status_code == 201
        job = await db.whatsapp_jobs.find_one({"talent_id": tid})
        assert job["template_id"] == casting_call_template["id"]
        assert tname in job["message_body"]
        assert label in job["message_body"]
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_multiple_projects_still_creates_one_batch_per_project(client):
    """Existing multi-project grouping (one batch per project) is preserved
    unchanged — only the template resolution changed."""
    pid_a, label_a = await _make_project()
    pid_b, label_b = await _make_project()
    tid, tname = await _make_talent_in_pipeline(pid_a)
    tid_b, tname_b = await _make_talent_in_pipeline(pid_b)
    tmpl_id, _ = await _make_custom_template("Hi {{talent_name}} — {{project_name}}.")
    try:
        r = await client.post(
            "/api/whatsapp/casting-call/send",
            json={"talent_ids": [tid, tid_b], "project_ids": [pid_a, pid_b], "template_id": tmpl_id},
        )
        assert r.status_code == 201
        body = r.json()
        assert len(body["batches"]) == 2
        assert {b["project_id"] for b in body["batches"]} == {pid_a, pid_b}

        job_a = await db.whatsapp_jobs.find_one({"talent_id": tid})
        job_b = await db.whatsapp_jobs.find_one({"talent_id": tid_b})
        assert job_a["message_body"] == f"Hi {tname} — {label_a}."
        assert job_b["message_body"] == f"Hi {tname_b} — {label_b}."
    finally:
        await _cleanup(pid_a, [tid], tmpl_id)
        await _cleanup(pid_b, [tid_b])


@_aio
async def test_multiple_talents_single_project_all_use_selected_template(client):
    pid, label = await _make_project()
    tid1, tname1 = await _make_talent_in_pipeline(pid)
    tid2, tname2 = await _make_talent_in_pipeline(pid)
    tmpl_id, _ = await _make_custom_template("Hey {{talent_name}}, re: {{project_name}}.")
    try:
        r = await client.post(
            "/api/whatsapp/casting-call/send",
            json={"talent_ids": [tid1, tid2], "project_ids": [pid], "template_id": tmpl_id},
        )
        assert r.status_code == 201
        job1 = await db.whatsapp_jobs.find_one({"talent_id": tid1})
        job2 = await db.whatsapp_jobs.find_one({"talent_id": tid2})
        assert job1["template_id"] == tmpl_id and job1["message_body"] == f"Hey {tname1}, re: {label}."
        assert job2["template_id"] == tmpl_id and job2["message_body"] == f"Hey {tname2}, re: {label}."
    finally:
        await _cleanup(pid, [tid1, tid2], tmpl_id)
