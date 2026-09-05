"""Focused tests for Production Desk (backend/routers/production_desk.py).

Covers the minimum-scenario list from the feature spec (locked-talent
auto-pull from Casting Pipeline, budget/commission/kickback math, payment
status, reimbursements with/without a bill, crew via existing CRM, and the
edge cases around zero/one/many locked talents and zero/multiple kickbacks)
plus the two "don't break anything else" assertions: Casting Pipeline
stays the single source of truth (unlock removes a talent from PD) and a
CRM contact used for a kickback/crew is the SAME row, never duplicated.
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

# One shared event loop + one login for the whole file, not one per test —
# the local dev rate limiter allows only 5 logins/15min and this file has
# far more than 5 test functions.
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


async def _make_project(**overrides):
    pid = f"zzz-test-pd-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": "ZZZ_TEST_PD_Proj", "slug": pid,
        "status": "ongoing", "commission_percent": "15%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid


async def _make_talent(name="ZZZ_TEST_PD_Talent"):
    tid = f"zzz-test-pd-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "tags": [], "media": []})
    return tid


async def _add_to_pipeline(pid, tid, stage="ask_to_test"):
    row_id = f"zzz-test-pd-row-{uuid.uuid4().hex[:8]}"
    await db.casting_pipeline.insert_one({
        "id": row_id, "project_id": pid, "talent_id": tid, "stage": stage,
        "created_at": _now(), "updated_at": _now(),
    })
    return row_id


async def _cleanup(pid=None, talent_ids=None, client_ids=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.project_kickbacks.delete_many({"project_id": pid})
        await db.project_reimbursements.delete_many({"project_id": pid})
        await db.project_crew.delete_many({"project_id": pid})
    if talent_ids:
        await db.talents.delete_many({"id": {"$in": talent_ids}})
    if client_ids:
        from bson import ObjectId
        await db.clients.delete_many({"_id": {"$in": [ObjectId(c) for c in client_ids]}})


# ---------------------------------------------------------------------------
# A. Loads for a project + edge case: zero locked talents
# ---------------------------------------------------------------------------
@_aio
async def test_loads_with_zero_locked_talents(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["locked_talents"] == []
        assert body["summary"]["locked_count"] == 0
        assert body["kickbacks"] == [] and body["reimbursements"] == [] and body["crew"] == []
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# B. Locked talents appear automatically + C. non-locked excluded
# ---------------------------------------------------------------------------
@_aio
async def test_only_locked_talents_appear(client, headers):
    pid = await _make_project()
    t_locked = await _make_talent("ZZZ_TEST_PD_Locked")
    t_other = await _make_talent("ZZZ_TEST_PD_Shortlisted")
    await _add_to_pipeline(pid, t_locked, stage="locked")
    await _add_to_pipeline(pid, t_other, stage="shortlisted")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        ids = [t["talent_id"] for t in body["locked_talents"]]
        assert t_locked in ids
        assert t_other not in ids
        assert body["summary"]["locked_count"] == 1
    finally:
        await _cleanup(pid, [t_locked, t_other])


# ---------------------------------------------------------------------------
# D. Unlocking a talent removes it from Production Desk
# ---------------------------------------------------------------------------
@_aio
async def test_unlock_removes_from_production_desk(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert len(r.json()["locked_talents"]) == 1

        await db.casting_pipeline.update_one({"project_id": pid, "talent_id": tid}, {"$set": {"stage": "rejected"}})

        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["locked_talents"] == []
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# E. Per-day x shooting-days total budget calculation
# ---------------------------------------------------------------------------
@_aio
async def test_budget_total_computed_from_per_day_and_days(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shooting_days": 4}, headers=headers)
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 5000}, headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["budget_total"] == 20000
        assert card["budget_total_is_explicit"] is False
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_explicit_budget_total_is_never_overwritten(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shooting_days": 4}, headers=headers)
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 99999}, headers=headers)
        # Setting per_day afterwards must NOT recompute/overwrite the explicit total.
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 5000}, headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["budget_total"] == 99999
        assert card["budget_total_is_explicit"] is True
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_with_no_budget_entered(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["budget_total"] is None
        assert card["commission_amount"] is None
        assert card["payment_status"] == "pending"
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# F. Commission calculation (project-level commission_percent reused)
# ---------------------------------------------------------------------------
@_aio
async def test_commission_calculated_from_project_percent(client, headers):
    pid = await _make_project(commission_percent="20%")
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 10000}, headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["commission_percent"] == 20.0
        assert card["commission_amount"] == 2000.0
        assert r.json()["summary"]["commission_gross"] == 2000.0
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_missing_project_commission_percent_is_handled(client, headers):
    """Edge case: project has no commission_percent set at all."""
    pid = await _make_project(commission_percent=None)
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 10000}, headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["commission_percent"] is None
        assert card["commission_amount"] is None
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# G / H. Kickbacks — single, multiple, and zero
# ---------------------------------------------------------------------------
@_aio
async def test_kickback_reduces_net_commission(client, headers):
    pid = await _make_project(commission_percent="10%")
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    client_id = None
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 10000}, headers=headers)

        r = await client.post("/api/marketing/clients", json={"name": "ZZZ_TEST_PD_KB_Contact"}, headers=headers)
        client_id = r.json()["id"]

        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["summary"]["kickbacks_total"] == 0  # edge case: zero kickbacks

        r = await client.post(
            f"/api/projects/{pid}/production-desk/kickbacks",
            json={"amount": 300, "recipient_client_id": client_id, "recipient_name": "ZZZ_TEST_PD_KB_Contact"},
            headers=headers,
        )
        r = await client.post(
            f"/api/projects/{pid}/production-desk/kickbacks",
            json={"amount": 200, "recipient_client_id": client_id, "recipient_name": "ZZZ_TEST_PD_KB_Contact"},
            headers=headers,
        )
        body = r.json()
        assert len(body["kickbacks"]) == 2  # multiple kickbacks
        assert body["summary"]["kickbacks_total"] == 500
        assert body["summary"]["commission_gross"] == 1000
        assert body["summary"]["commission_net"] == 500
        assert body["kickbacks"][0]["recipient"]["name"] == "ZZZ_TEST_PD_KB_Contact"
    finally:
        await _cleanup(pid, [tid], [client_id] if client_id else None)


# ---------------------------------------------------------------------------
# I / J. Reimbursements — with and without a bill attachment
# ---------------------------------------------------------------------------
@_aio
async def test_reimbursement_without_bill(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.post(
            f"/api/projects/{pid}/production-desk/reimbursements",
            data={"talent_id": tid, "expense_type": "Travel", "amount": "500"},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["reimbursements"]) == 1
        assert body["reimbursements"][0]["material_id"] is None
        assert body["reimbursements"][0]["status"] == "pending"
        assert body["reimbursements"][0]["talent_name"] == "ZZZ_TEST_PD_Talent"
        assert "1 reimbursement bill missing" in " ".join(body["needs_attention"])
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_reimbursement_with_bill_uses_existing_material_pipeline(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        # Existing upload infra binary-signature-validates content (see
        # core.cloudinary_upload) — a real bill is a PDF/image, so use PDF
        # magic bytes rather than an arbitrary text blob.
        files = {"file": ("receipt.pdf", b"%PDF-1.4 fake-receipt-bytes", "application/pdf")}
        r = await client.post(
            f"/api/projects/{pid}/production-desk/reimbursements",
            data={"talent_id": tid, "expense_type": "Travel", "amount": "500"},
            files=files,
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        reimb = body["reimbursements"][0]
        assert reimb["material_id"] is not None

        # The bill must actually be a project material (existing infra), not
        # a new parallel file store.
        project = await db.projects.find_one({"id": pid})
        material_ids = [m["id"] for m in project.get("materials", [])]
        assert reimb["material_id"] in material_ids
        assert any(m["category"] == "reimbursement_bill" for m in project["materials"])
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_reimbursement_status_update_and_delete(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.post(
            f"/api/projects/{pid}/production-desk/reimbursements",
            data={"talent_id": tid, "expense_type": "Food", "amount": "200"},
            headers=headers,
        )
        rid = r.json()["reimbursements"][0]["id"]

        r = await client.patch(f"/api/projects/{pid}/production-desk/reimbursements/{rid}", json={"status": "paid"}, headers=headers)
        assert r.json()["reimbursements"][0]["status"] == "paid"

        r = await client.delete(f"/api/projects/{pid}/production-desk/reimbursements/{rid}", headers=headers)
        assert r.json()["reimbursements"] == []
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# K. Payment status updates — pending/cleared, multiple locked talents
# ---------------------------------------------------------------------------
@_aio
async def test_payment_status_summary_with_multiple_locked_talents(client, headers):
    pid = await _make_project()
    t1 = await _make_talent("ZZZ_TEST_PD_T1")
    t2 = await _make_talent("ZZZ_TEST_PD_T2")
    await _add_to_pipeline(pid, t1, stage="locked")
    await _add_to_pipeline(pid, t2, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["summary"]["payments_total"] == 2
        assert r.json()["summary"]["payments_cleared"] == 0

        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{t1}", json={"payment_status": "cleared"}, headers=headers)
        assert r.json()["summary"]["payments_cleared"] == 1

        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{t1}", json={"payment_status": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid, [t1, t2])


@_aio
async def test_cannot_edit_a_non_locked_talent(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="shortlisted")
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"payment_status": "cleared"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# L / M. Crew — existing CRM association, contact reused not duplicated
# ---------------------------------------------------------------------------
@_aio
async def test_crew_uses_existing_crm_contact_without_duplicating(client, headers):
    pid = await _make_project()
    r = await client.post("/api/marketing/clients", json={"name": "ZZZ_TEST_PD_Crew_Contact"}, headers=headers)
    client_id = r.json()["id"]
    before_count = await db.clients.count_documents({"name": "ZZZ_TEST_PD_Crew_Contact"})
    assert before_count == 1
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/crew", json={"client_id": client_id, "role": "Producer"}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body["crew"]) == 1
        assert body["crew"][0]["contact"]["name"] == "ZZZ_TEST_PD_Crew_Contact"

        # Adding the SAME contact to a second project must not create a
        # second CRM row — it's the same client_id referenced again.
        pid2 = await _make_project()
        try:
            r2 = await client.post(f"/api/projects/{pid2}/production-desk/crew", json={"client_id": client_id, "role": "Director"}, headers=headers)
            assert r2.status_code == 200
        finally:
            await _cleanup(pid2)

        after_count = await db.clients.count_documents({"name": "ZZZ_TEST_PD_Crew_Contact"})
        assert after_count == 1  # still exactly one CRM row
    finally:
        await _cleanup(pid, None, [client_id])


@_aio
async def test_add_crew_rejects_unknown_client_id(client, headers):
    pid = await _make_project()
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/crew", json={"client_id": "000000000000000000000000", "role": "Producer"}, headers=headers)
        assert r.status_code == 404
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# N. Existing project data is not overwritten by Production Desk patches
# ---------------------------------------------------------------------------
@_aio
async def test_project_level_patch_does_not_touch_unrelated_fields(client, headers):
    pid = await _make_project(commission_percent="25%", brand_name="ZZZ_TEST_PD_Untouched")
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shooting_days": 5}, headers=headers)
        project = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert project["commission_percent"] == "25%"
        assert project["brand_name"] == "ZZZ_TEST_PD_Untouched"
        assert project["pd_shooting_days"] == 5
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# Needs-attention: confirms deterministic (non-AI) checklist-driven banner
# ---------------------------------------------------------------------------
@_aio
async def test_needs_attention_reflects_checklist_state(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        attention = r.json()["needs_attention"]
        assert "Confirmation mail pending" in attention
        assert "Client payment pending" in attention
        assert "GST component pending" in attention

        await client.patch(
            f"/api/projects/{pid}/production-desk",
            json={"confirmation_mail_received": True, "payment_in_received": True, "gst_component_received": True},
            headers=headers,
        )
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        attention = r.json()["needs_attention"]
        assert "Confirmation mail pending" not in attention
        assert "Client payment pending" not in attention
        assert "GST component pending" not in attention
    finally:
        await _cleanup(pid)
