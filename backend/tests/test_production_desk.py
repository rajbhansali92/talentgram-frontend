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
        await db.notifications.delete_many({"payload.project_id": pid})
        await db.workflow_tasks.delete_many({"project_id": pid})
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


# ===========================================================================
# Finance / Zoho connector pass (Production Desk stays the sole data owner —
# there is no separate Finance module in this codebase to "connect" to; see
# production_desk.py's module docstring for the full inspection findings).
# ===========================================================================

@_aio
async def test_finance_block_reports_honest_zoho_not_connected(client, headers):
    """Zoho Books integration does not exist anywhere in this codebase
    (Case B). The GET response must say so literally, never a fake
    "synced" state, and this must never change without a real integration
    being built."""
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["finance"] == {"zoho_status": "not_connected"}

        # Not mutable through any Production Desk endpoint — there is no
        # code path that ever sets it to anything else.
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shooting_days": 3}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["finance"]["zoho_status"] == "not_connected"
    finally:
        await _cleanup(pid)


@_aio
async def test_existing_client_and_talent_budget_lines_surfaced_read_only(client, headers):
    """project.client_budget / project.talent_budget (the pre-existing
    Project Details 'budget hint' fields) must be displayed, not
    duplicated or re-entered, and must NEVER feed the commission/payment
    math — only the pd_* structured fields do that."""
    pid = await _make_project(
        client_budget=[{"label": "Total Client Budget", "value": "5,52,000"}],
        talent_budget=[{"label": "Per Talent (indicative)", "value": "40,000 - 60,000"}],
    )
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        assert body["project"]["client_budget_lines"] == [{"label": "Total Client Budget", "value": "5,52,000"}]
        assert body["project"]["talent_budget_lines"] == [{"label": "Per Talent (indicative)", "value": "40,000 - 60,000"}]

        # The free-text hint values must NOT leak into the numeric
        # commission/payment calculation for the locked talent.
        card = body["locked_talents"][0]
        assert card["budget_total"] is None
        assert card["commission_amount"] is None
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_project_without_budget_lines_returns_empty_lists(client, headers):
    """Edge case: a project with no client_budget/talent_budget entered at
    all must not error and must not fabricate placeholder lines."""
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["project"]["client_budget_lines"] == []
        assert r.json()["project"]["talent_budget_lines"] == []
    finally:
        await _cleanup(pid)


@_aio
async def test_payment_cleared_notifies_via_existing_notification_fanout(client, headers):
    """Reuses notifications.fanout() (the existing Dashboard 'Recent
    Activity' admin notification mechanism) — no new activity/audit
    system. Fires exactly once per pending->cleared TRANSITION, not on
    every save."""
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        before = await db.notifications.count_documents({"type": "production_desk_payment_cleared", "payload.project_id": pid})
        assert before == 0

        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"payment_status": "cleared"}, headers=headers)
        assert r.status_code == 200
        after_first = await db.notifications.count_documents({"type": "production_desk_payment_cleared", "payload.project_id": pid})
        assert after_first > 0  # at least one active recipient notified

        # Re-saving "cleared" again (no-op transition) must NOT re-fire.
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"payment_status": "cleared"}, headers=headers)
        after_second = await db.notifications.count_documents({"type": "production_desk_payment_cleared", "payload.project_id": pid})
        assert after_second == after_first
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_payment_in_received_notifies_via_existing_notification_fanout(client, headers):
    pid = await _make_project()
    try:
        before = await db.notifications.count_documents({"type": "production_desk_payment_in_received", "payload.project_id": pid})
        assert before == 0

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"payment_in_received": True}, headers=headers)
        assert r.status_code == 200
        after = await db.notifications.count_documents({"type": "production_desk_payment_in_received", "payload.project_id": pid})
        assert after > 0

        # Flipping other unrelated fields must not re-fire.
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shooting_days": 2}, headers=headers)
        after2 = await db.notifications.count_documents({"type": "production_desk_payment_in_received", "payload.project_id": pid})
        assert after2 == after
    finally:
        await _cleanup(pid)


@_aio
async def test_no_conflicting_payment_status_source_exists(client, headers):
    """Production Desk's pd_payment_status is the ONLY place a locked
    talent's payment status is ever stored — verifies there is no second,
    independently-updatable record that could contradict it."""
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"payment_status": "cleared"}, headers=headers)

        # No separate "payments" or "finance" collection exists holding a
        # second copy of this status.
        collection_names = await db.list_collection_names()
        assert "payments" not in collection_names
        assert "finance" not in collection_names

        row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
        assert row["pd_payment_status"] == "cleared"

        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["locked_talents"][0]["payment_status"] == "cleared"
    finally:
        await _cleanup(pid, [tid])


# ===========================================================================
# Production Checklist + Management Agent pass — Invoice Raised/Sent,
# Production Status, and the resulting notification fanout.
# ===========================================================================

@_aio
async def test_invoice_raised_and_sent_read_and_update(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["project"]["pd_invoice_raised"] is False
        assert r.json()["project"]["pd_invoice_sent"] is False

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_raised": True}, headers=headers)
        assert r.json()["project"]["pd_invoice_raised"] is True
        assert r.json()["project"]["pd_invoice_sent"] is False
        assert "Invoice not sent" in r.json()["needs_attention"]
        assert "Invoice not raised" not in r.json()["needs_attention"]

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_sent": True}, headers=headers)
        assert r.json()["project"]["pd_invoice_sent"] is True
        assert "Invoice not sent" not in r.json()["needs_attention"]
    finally:
        await _cleanup(pid)


@_aio
async def test_invoice_not_raised_is_the_only_invoice_attention_item(client, headers):
    """Before 'raised' is true, showing 'not sent' too would be redundant
    noise — only the earlier lifecycle step should surface."""
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        attention = r.json()["needs_attention"]
        assert "Invoice not raised" in attention
        assert "Invoice not sent" not in attention
    finally:
        await _cleanup(pid)


@_aio
async def test_existing_payment_in_and_gst_checklist_still_work(client, headers):
    """Regression: adding invoice_raised/invoice_sent must not disturb the
    pre-existing Payment In / GST checklist items."""
    pid = await _make_project()
    try:
        r = await client.patch(
            f"/api/projects/{pid}/production-desk",
            json={"payment_in_received": True, "gst_component_received": True},
            headers=headers,
        )
        body = r.json()
        assert body["project"]["pd_payment_in_received"] is True
        assert body["project"]["pd_gst_component_received"] is True
        assert "Client payment pending" not in body["needs_attention"]
        assert "GST component pending" not in body["needs_attention"]
    finally:
        await _cleanup(pid)


@_aio
async def test_invoice_sent_notifies_via_existing_fanout_transition_only(client, headers):
    pid = await _make_project()
    try:
        before = await db.notifications.count_documents({"type": "production_desk_invoice_sent", "payload.project_id": pid})
        assert before == 0

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_sent": True}, headers=headers)
        assert r.status_code == 200
        after = await db.notifications.count_documents({"type": "production_desk_invoice_sent", "payload.project_id": pid})
        assert after > 0

        # Redundant re-save must not re-fire.
        await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_sent": True}, headers=headers)
        after2 = await db.notifications.count_documents({"type": "production_desk_invoice_sent", "payload.project_id": pid})
        assert after2 == after
    finally:
        await _cleanup(pid)


@_aio
async def test_production_status_read_write_and_validation(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["project"]["pd_production_status"] == "not_started"

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"production_status": "shoot_scheduled"}, headers=headers)
        assert r.json()["project"]["pd_production_status"] == "shoot_scheduled"

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"production_status": "not_a_real_status"}, headers=headers)
        assert r.status_code == 400

        # Existing project.status (ongoing/hold/complete/locked) must be
        # completely untouched by pd_production_status writes.
        project = await db.projects.find_one({"id": pid}, {"_id": 0})
        assert project["status"] == "ongoing"
    finally:
        await _cleanup(pid)


@_aio
async def test_no_duplicate_financial_records_from_checklist_changes(client, headers):
    """Flipping every checklist toggle must never create a second document
    anywhere — everything lands as fields on the ONE existing project doc."""
    pid = await _make_project()
    try:
        before_project_count = await db.projects.count_documents({"id": pid})
        await client.patch(
            f"/api/projects/{pid}/production-desk",
            json={"invoice_raised": True, "invoice_sent": True, "payment_in_received": True, "gst_component_received": True, "production_status": "finance_closed"},
            headers=headers,
        )
        after_project_count = await db.projects.count_documents({"id": pid})
        assert before_project_count == after_project_count == 1
        collection_names = await db.list_collection_names()
        assert "invoices" not in collection_names
    finally:
        await _cleanup(pid)


# ===========================================================================
# Production Management Desk Phase 1+2 — Shoot Management, Talent
# Preparation, Payment Follow-up, and the shared workflow_tasks display.
# ===========================================================================

@_aio
async def test_talent_preparation_fields_read_write(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        card = r.json()["locked_talents"][0]
        assert card["fitting_status"] == "not_scheduled"
        assert card["look_test_status"] == "not_scheduled"
        assert card["shoot_status"] == "not_scheduled"
        assert card["costume_trial_at"] is None

        due = "2026-09-10T12:00:00+00:00"
        r = await client.patch(
            f"/api/projects/{pid}/production-desk/talents/{tid}",
            json={"costume_trial_at": due, "costume_trial_location": "Studio A", "fitting_status": "scheduled", "look_test_status": "completed", "shoot_status": "today", "grooming_requirements": "Clean shave", "special_instructions": "Bring own shoes"},
            headers=headers,
        )
        card = r.json()["locked_talents"][0]
        assert card["costume_trial_at"] == due
        assert card["costume_trial_location"] == "Studio A"
        assert card["fitting_status"] == "scheduled"
        assert card["look_test_status"] == "completed"
        assert card["shoot_status"] == "today"
        assert card["grooming_requirements"] == "Clean shave"
        assert card["special_instructions"] == "Bring own shoes"
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_preparation_status_validation(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"fitting_status": "not_a_real_status"}, headers=headers)
        assert r.status_code == 400
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"shoot_status": "not_a_real_status"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_shoot_management_and_payment_followup_project_fields(client, headers):
    pid = await _make_project()
    try:
        due = "2026-09-10T12:00:00+00:00"
        r = await client.patch(
            f"/api/projects/{pid}/production-desk",
            json={
                "reporting_time": "7:00 AM", "shoot_status": "scheduled",
                "payment_terms": "50% advance, 50% on delivery",
                "expected_payment_date": due, "next_follow_up_at": due,
                "payment_followup_status": "due", "payment_followup_notes": "Called client",
            },
            headers=headers,
        )
        assert r.status_code == 200
        proj = r.json()["project"]
        assert proj["pd_reporting_time"] == "7:00 AM"
        assert proj["pd_shoot_status"] == "scheduled"
        assert proj["pd_payment_terms"] == "50% advance, 50% on delivery"
        assert proj["pd_expected_payment_date"] == due
        assert proj["pd_next_follow_up_at"] == due
        assert proj["pd_payment_followup_status"] == "due"
        assert proj["pd_payment_followup_notes"] == "Called client"

        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"shoot_status": "bogus"}, headers=headers)
        assert r.status_code == 400
        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"payment_followup_status": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid)


@_aio
async def test_needs_attention_includes_payment_followup_and_shoot_details(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert "Shoot details incomplete" in r.json()["needs_attention"]

        await client.patch(f"/api/projects/{pid}/production-desk", json={"call_time": "8am"}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert "Shoot details incomplete" not in r.json()["needs_attention"]

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        await client.patch(f"/api/projects/{pid}/production-desk", json={"next_follow_up_at": today, "payment_followup_status": "due"}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert "Payment follow-up due today" in r.json()["needs_attention"]

        await client.patch(f"/api/projects/{pid}/production-desk", json={"payment_followup_status": "done"}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert "Payment follow-up due today" not in r.json()["needs_attention"]
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_production_desk_displays_shared_workflow_tasks(client, headers):
    """UI-created-vs-agent-created is the SAME record: Production Desk's
    GET response reads directly from db.workflow_tasks — a task inserted
    there any way shows up here immediately."""
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid, stage="locked")
    try:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        today_due = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        future_due = (now + timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()

        r1 = await client.post("/api/workflow/tasks", json={"title": "Get call sheet", "category": "project", "project_id": pid, "due_at": today_due, "priority": "high"}, headers=headers)
        r2 = await client.post("/api/workflow/tasks", json={"title": "Confirm costume trial", "category": "project", "project_id": pid, "talent_id": tid, "due_at": future_due}, headers=headers)

        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        assert len(body["tasks"]["all"]) == 2
        titles = {t["title"] for t in body["tasks"]["all"]}
        assert titles == {"Get call sheet", "Confirm costume trial"}
        assert any(t["title"] == "Confirm costume trial" and t["talent_name"] for t in body["tasks"]["all"])
        assert len(body["today"]["tasks"]) == 1
        assert body["today"]["tasks"][0]["title"] == "Get call sheet"
        assert len(body["upcoming"]["tasks"]) == 1
        assert body["upcoming"]["tasks"][0]["title"] == "Confirm costume trial"

        # Completing a task (e.g. via the Management Agent / Workflow page)
        # must be immediately reflected here — no separate sync step.
        await client.put(f"/api/workflow/tasks/{r1.json()['id']}", json={"status": "completed"}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        assert len(body["tasks"]["pending"]) == 1
        assert body["tasks"]["pending"][0]["title"] == "Confirm costume trial"
        assert "1 task overdue" not in body["needs_attention"]  # not overdue, just completed+one future
    finally:
        await _cleanup(pid, [tid])
        await db.workflow_tasks.delete_many({"project_id": pid})


@_aio
async def test_overdue_task_surfaces_in_needs_attention(client, headers):
    pid = await _make_project()
    try:
        from datetime import datetime, timedelta, timezone
        past_due = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        r1 = await client.post("/api/workflow/tasks", json={"title": "Overdue task", "category": "project", "project_id": pid, "due_at": past_due}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        assert len(body["tasks"]["overdue"]) == 1
        assert "1 task overdue" in body["needs_attention"]
    finally:
        await _cleanup(pid)
