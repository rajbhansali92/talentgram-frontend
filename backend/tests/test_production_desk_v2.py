"""Focused tests for Production Desk V2 (talent-level shooting, overtime,
reimbursements, payment tranches, checklist restructure, WhatsApp one-tap
message builders).

Reuses test_production_desk.py's exact fixtures/conventions (in-process
ASGI client, module-scoped login, _make_project/_make_talent/_add_to_pipeline
helpers) — not a second test harness.
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


async def _make_project(**overrides):
    pid = f"zzz-test-pdv2-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": "ZZZ_TEST_PDV2_Proj", "slug": pid,
        "status": "ongoing", "commission_percent": "20%", "materials": [],
        "created_at": _now(), "updated_at": _now(),
    }
    doc.update(overrides)
    await db.projects.insert_one(doc)
    return pid


async def _make_talent(name="ZZZ_TEST_PDV2_Talent", phone="+911234500000"):
    tid = f"zzz-test-pdv2-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "phone": phone, "tags": [], "media": []})
    return tid


async def _add_to_pipeline(pid, tid, stage="locked"):
    row_id = f"zzz-test-pdv2-row-{uuid.uuid4().hex[:8]}"
    await db.casting_pipeline.insert_one({
        "id": row_id, "project_id": pid, "talent_id": tid, "stage": stage,
        "created_at": _now(), "updated_at": _now(),
    })
    return row_id


async def _make_crm_contact(name="ZZZ_TEST_PDV2_Contact", phone="+919988877766"):
    res = await db.clients.insert_one({"name": name, "phone_number": phone, "created_at": _now(), "last_contacted_date": _now(), "stage": "lead", "tags": []})
    return str(res.inserted_id)


async def _cleanup(pid=None, talent_ids=None, client_ids=None):
    if pid:
        await db.projects.delete_one({"id": pid})
        await db.casting_pipeline.delete_many({"project_id": pid})
        await db.project_reimbursements.delete_many({"project_id": pid})
        await db.project_payment_tranches.delete_many({"project_id": pid})
        await db.notifications.delete_many({"payload.project_id": pid})
    if talent_ids:
        await db.talents.delete_many({"id": {"$in": talent_ids}})
    if client_ids:
        from bson import ObjectId
        await db.clients.delete_many({"_id": {"$in": [ObjectId(c) for c in client_ids]}})


def _find_talent(body, tid):
    return next(t for t in body["locked_talents"] if t["talent_id"] == tid)


# ---------------------------------------------------------------------------
# Overtime / extra hours
# ---------------------------------------------------------------------------
@_aio
async def test_overtime_12_hour_basis(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 50000}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 12, "actual_hours": 14}, headers=headers)
        card = _find_talent(r.json(), tid)
        # hourly = 50000/12, extra = 2h -> 8333.33
        assert card["extra_hours_total"] == pytest.approx(8333.33, abs=0.01)
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_overtime_14_hour_basis_adjusts_hourly_rate(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 50000}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 14, "actual_hours": 15}, headers=headers)
        card = _find_talent(r.json(), tid)
        # hourly = 50000/14, extra = 1h
        assert card["extra_hours_total"] == pytest.approx(3571.43, abs=0.01)
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_zero_overtime_when_actual_equals_agreed(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 50000}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 12, "actual_hours": 12}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["extra_hours_total"] == 0
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_zero_overtime_when_actual_less_than_agreed(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 50000}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 12, "actual_hours": 10}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["extra_hours_total"] == 0
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_multiple_overtime_days_sum(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 50000}, headers=headers)
        await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-20", "agreed_hours": 12, "actual_hours": 14}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 12, "actual_hours": 13}, headers=headers)
        card = _find_talent(r.json(), tid)
        # day1: 8333.33, day2: 4166.67 -> 12500.00
        assert card["extra_hours_total"] == pytest.approx(12500.00, abs=0.02)
        assert card["shooting_days"] == 2
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_different_talents_different_rates_and_overtime(client, headers):
    pid = await _make_project()
    t1 = await _make_talent("ZZZ_TEST_PDV2_A")
    t2 = await _make_talent("ZZZ_TEST_PDV2_B")
    await _add_to_pipeline(pid, t1)
    await _add_to_pipeline(pid, t2)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{t1}", json={"budget_per_day": 50000}, headers=headers)
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{t2}", json={"budget_per_day": 30000}, headers=headers)
        await client.post(f"/api/projects/{pid}/production-desk/talents/{t1}/shoot-days", json={"date": "2026-09-20", "agreed_hours": 12, "actual_hours": 14}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{t2}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 14, "actual_hours": 15}, headers=headers)
        body = r.json()
        c1 = _find_talent(body, t1)
        c2 = _find_talent(body, t2)
        assert c1["extra_hours_total"] == pytest.approx(8333.33, abs=0.01)
        assert c2["extra_hours_total"] == pytest.approx(2142.86, abs=0.01)
        assert body["summary"]["extra_hours_total"] == pytest.approx(c1["extra_hours_total"] + c2["extra_hours_total"], abs=0.02)
    finally:
        await _cleanup(pid, [t1, t2])


@_aio
async def test_commission_includes_overtime(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 100000, "shooting_days": 1, "commission_percent": 20}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 12, "actual_hours": 14}, headers=headers)
        card = _find_talent(r.json(), tid)
        # budget_total=100000 (shoot_days array len=1 -> shooting_days=1),
        # extra = 100000/12*2 = 16666.67, commissionable = 116666.67
        assert card["budget_total"] == 100000
        assert card["extra_hours_total"] == pytest.approx(16666.67, abs=0.01)
        assert card["commissionable_amount"] == pytest.approx(116666.67, abs=0.01)
        assert card["commission_amount"] == pytest.approx(23333.33, abs=0.02)
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# Talent scheduling — independent dates/hours/locations per talent
# ---------------------------------------------------------------------------
@_aio
async def test_project_dates_auto_populate_talent_schedule(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shoot_dates_list": ["2026-09-20", "2026-09-21", "2026-09-22"]}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days/use-project-dates", headers=headers)
        card = _find_talent(r.json(), tid)
        assert [d["date"] for d in card["shoot_days"]] == ["2026-09-20", "2026-09-21", "2026-09-22"]
        assert card["shooting_days"] == 3
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_can_have_fewer_dates_than_project(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"shoot_dates_list": ["2026-09-20", "2026-09-21", "2026-09-22"]}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21"}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["shooting_days"] == 1
        # Project's own dates untouched by editing the talent.
        proj = r.json()["project"]
        assert proj["pd_shoot_dates_list"] == ["2026-09-20", "2026-09-21", "2026-09-22"]
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_multiple_talents_independent_dates_call_times_locations(client, headers):
    pid = await _make_project()
    t1 = await _make_talent("ZZZ_TEST_PDV2_A")
    t2 = await _make_talent("ZZZ_TEST_PDV2_B")
    await _add_to_pipeline(pid, t1)
    await _add_to_pipeline(pid, t2)
    try:
        await client.post(f"/api/projects/{pid}/production-desk/talents/{t1}/shoot-days", json={"date": "2026-09-20", "call_time": "7:00 AM", "location": "Studio A"}, headers=headers)
        await client.post(f"/api/projects/{pid}/production-desk/talents/{t1}/shoot-days", json={"date": "2026-09-21", "call_time": "7:00 AM", "location": "Studio A"}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{t2}/shoot-days", json={"date": "2026-09-21", "call_time": "9:00 AM", "location": "Studio B"}, headers=headers)
        body = r.json()
        c1 = _find_talent(body, t1)
        c2 = _find_talent(body, t2)
        assert [d["date"] for d in c1["shoot_days"]] == ["2026-09-20", "2026-09-21"]
        assert [d["date"] for d in c2["shoot_days"]] == ["2026-09-21"]
        assert c1["shoot_days"][0]["location"] == "Studio A"
        assert c2["shoot_days"][0]["location"] == "Studio B"
        assert c2["shoot_days"][0]["call_time"] == "9:00 AM"
    finally:
        await _cleanup(pid, [t1, t2])


@_aio
async def test_independent_shoot_status_per_day(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "shoot_status": "today"}, headers=headers)
        card = _find_talent(r.json(), tid)
        day_id = card["shoot_days"][0]["id"]
        r2 = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days/{day_id}", json={"shoot_status": "completed"}, headers=headers)
        card2 = _find_talent(r2.json(), tid)
        assert card2["shoot_days"][0]["shoot_status"] == "completed"
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_delete_shoot_day(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21"}, headers=headers)
        day_id = _find_talent(r.json(), tid)["shoot_days"][0]["id"]
        r2 = await client.delete(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days/{day_id}", headers=headers)
        assert _find_talent(r2.json(), tid)["shoot_days"] == []
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# Readings & Rehearsals
# ---------------------------------------------------------------------------
@_aio
async def test_readings_rehearsals_crud(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals", json={"type": "reading", "date": "2026-09-20", "time": "4 PM", "location": "Studio X"}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert len(card["readings_rehearsals"]) == 1
        entry_id = card["readings_rehearsals"][0]["id"]

        r2 = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals", json={"type": "rehearsal", "date": "2026-09-22", "location": "Andheri"}, headers=headers)
        assert len(_find_talent(r2.json(), tid)["readings_rehearsals"]) == 2

        r3 = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals/{entry_id}", json={"notes": "Bring script"}, headers=headers)
        updated = next(e for e in _find_talent(r3.json(), tid)["readings_rehearsals"] if e["id"] == entry_id)
        assert updated["notes"] == "Bring script"

        r4 = await client.delete(f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals/{entry_id}", headers=headers)
        assert len(_find_talent(r4.json(), tid)["readings_rehearsals"]) == 1
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_readings_rehearsals_invalid_type_rejected(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals", json={"type": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# Reimbursements — per-talent surfacing + commission exclusion + checklist
# ---------------------------------------------------------------------------
@_aio
async def test_talent_reimbursement_total_excluded_from_commission(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 100000, "shooting_days": 1, "commission_percent": 20}, headers=headers)
        fd = {"talent_id": tid, "expense_type": "Travel", "amount": "2500"}
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data=fd, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["reimbursement_total"] == 2500
        # Commission stays on the fee alone — 20% of 100000 = 20000, NOT 20% of 102500.
        assert card["commission_amount"] == pytest.approx(20000, abs=0.02)
        assert card["commissionable_amount"] == 100000
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_multiple_reimbursements_sum_per_talent(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": "1000"}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Food", "amount": "500"}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["reimbursement_total"] == 1500
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_reimbursement_checklist_na_when_none(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["reimbursement_checklist_status"] == "n_a"
    finally:
        await _cleanup(pid)


@_aio
async def test_reimbursement_checklist_pending_then_complete(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": "1000"}, headers=headers)
        assert r.json()["reimbursement_checklist_status"] == "pending"
        reimb_id = r.json()["reimbursements"][0]["id"]
        r2 = await client.patch(f"/api/projects/{pid}/production-desk/reimbursements/{reimb_id}", json={"status": "paid"}, headers=headers)
        assert r2.json()["reimbursement_checklist_status"] == "complete"
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_project_reimbursement_aggregation(client, headers):
    pid = await _make_project()
    t1 = await _make_talent("ZZZ_TEST_PDV2_A")
    t2 = await _make_talent("ZZZ_TEST_PDV2_B")
    await _add_to_pipeline(pid, t1)
    await _add_to_pipeline(pid, t2)
    try:
        await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": t1, "expense_type": "Travel", "amount": "1000"}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": t2, "expense_type": "Food", "amount": "500"}, headers=headers)
        assert r.json()["summary"]["reimbursements_total"] == 1500
    finally:
        await _cleanup(pid, [t1, t2])


# ---------------------------------------------------------------------------
# Checklist restructure
# ---------------------------------------------------------------------------
@_aio
async def test_agreement_signed_and_na(client, headers):
    pid = await _make_project()
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"agreement_status": "done"}, headers=headers)
        assert r.json()["project"]["pd_agreement_status"] == "done"
        r2 = await client.patch(f"/api/projects/{pid}/production-desk", json={"agreement_status": "n_a"}, headers=headers)
        assert r2.json()["project"]["pd_agreement_status"] == "n_a"
        assert "Agreement not signed" not in r2.json()["needs_attention"]
        r3 = await client.patch(f"/api/projects/{pid}/production-desk", json={"agreement_status": "pending"}, headers=headers)
        assert "Agreement not signed" in r3.json()["needs_attention"]
    finally:
        await _cleanup(pid)


@_aio
async def test_agreement_status_rejects_invalid_value(client, headers):
    pid = await _make_project()
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"agreement_status": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid)


@_aio
async def test_invoice_raised_and_sent_combined_toggle(client, headers):
    pid = await _make_project()
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_raised_and_sent": True}, headers=headers)
        body = r.json()["project"]
        assert body["pd_invoice_raised"] is True
        assert body["pd_invoice_sent"] is True
        assert r.json()["project"]["pd_invoice_raised_and_sent"] is True
        r2 = await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_raised_and_sent": False}, headers=headers)
        assert r2.json()["project"]["pd_invoice_raised"] is False
        assert r2.json()["project"]["pd_invoice_sent"] is False
    finally:
        await _cleanup(pid)


@_aio
async def test_invoice_combined_status_false_when_only_one_leg_true(client, headers):
    """Backward compatibility: an OLD project/record that only ever had
    invoice_raised=True (never invoice_sent) must show the new combined
    view as NOT complete — never silently upgraded."""
    pid = await _make_project()
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={"invoice_raised": True}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        assert r.json()["project"]["pd_invoice_raised"] is True
        assert r.json()["project"]["pd_invoice_sent"] is False
        assert r.json()["project"]["pd_invoice_raised_and_sent"] is False
    finally:
        await _cleanup(pid)


@_aio
async def test_client_payment_gst_checklist_unaffected(client, headers):
    pid = await _make_project()
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk", json={"payment_in_received": True, "gst_component_received": True}, headers=headers)
        body = r.json()["project"]
        assert body["pd_payment_in_received"] is True
        assert body["pd_gst_component_received"] is True
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# Payment Tranches
# ---------------------------------------------------------------------------
@_aio
async def test_create_and_list_tranche(client, headers):
    pid = await _make_project()
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Signing", "amount": 200000, "trigger": "On signing"}, headers=headers)
        assert r.status_code == 200
        tranches = r.json()["tranches"]
        assert len(tranches) == 1
        assert tranches[0]["name"] == "Signing"
        assert tranches[0]["invoice_status"] == "pending"
        assert tranches[0]["payment_status"] == "pending"
    finally:
        await _cleanup(pid)


@_aio
async def test_tranche_invoice_and_payment_status_updates(client, headers):
    pid = await _make_project()
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Shoot Commencement", "amount": 300000}, headers=headers)
        tid = r.json()["tranches"][0]["id"]
        r2 = await client.patch(f"/api/projects/{pid}/production-desk/tranches/{tid}", json={"invoice_status": "raised_and_sent", "payment_status": "received"}, headers=headers)
        t = r2.json()["tranches"][0]
        assert t["invoice_status"] == "raised_and_sent"
        assert t["payment_status"] == "received"
    finally:
        await _cleanup(pid)


@_aio
async def test_multiple_tranches_totals(client, headers):
    pid = await _make_project()
    try:
        await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "T1", "amount": 200000}, headers=headers)
        r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "T2", "amount": 300000}, headers=headers)
        tranches = r.json()["tranches"]
        t1_id = next(t["id"] for t in tranches if t["name"] == "T1")
        r2 = await client.patch(f"/api/projects/{pid}/production-desk/tranches/{t1_id}", json={"payment_status": "received"}, headers=headers)
        summary = r2.json()["summary"]
        assert summary["tranches_total"] == 500000
        assert summary["tranches_received_total"] == 200000
    finally:
        await _cleanup(pid)


@_aio
async def test_tranche_invalid_status_rejected(client, headers):
    pid = await _make_project()
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "T1", "amount": 1000, "invoice_status": "bogus"}, headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid)


@_aio
async def test_delete_tranche(client, headers):
    pid = await _make_project()
    try:
        r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "T1", "amount": 1000}, headers=headers)
        tid = r.json()["tranches"][0]["id"]
        r2 = await client.delete(f"/api/projects/{pid}/production-desk/tranches/{tid}", headers=headers)
        assert r2.json()["tranches"] == []
    finally:
        await _cleanup(pid)


# ---------------------------------------------------------------------------
# CRM / WhatsApp — payload correctness (never actually sends anything)
# ---------------------------------------------------------------------------
@_aio
async def test_payment_followup_message_payload(client, headers):
    pid = await _make_project(brand_name="ZZZ_TEST_PDV2_WA_Proj")
    tid = await _make_talent("ZZZ_TEST_PDV2_WA_Talent")
    cid = await _make_crm_contact("ZZZ_TEST_PDV2_Amit", "+919900011122")
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk", json={
            "production_contact_client_id": cid, "payment_terms": "Within 15 days", "expected_payment_date": "2026-09-25T12:00:00.000Z",
        }, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["phone"] == "+919900011122"
        assert "ZZZ_TEST_PDV2_WA_Proj" in body["message"]
        assert "ZZZ_TEST_PDV2_WA_Talent" in body["message"]
        assert "Within 15 days" in body["message"]
        assert "2026-09-25" in body["message"]
    finally:
        await _cleanup(pid, [tid], [cid])


@_aio
async def test_payment_followup_message_requires_contact(client, headers):
    pid = await _make_project()
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid)


@_aio
async def test_talent_invoice_message_calculation_matches_spec_example(client, headers):
    """Reproduces the master prompt's own section 24 worked example
    exactly: fee 1,00,000 / commission 20% / extra hours 10,000 /
    reimbursement 2,500 -> invoice amount 90,500."""
    pid = await _make_project(commission_percent="20%")
    tid = await _make_talent("ZZZ_TEST_PDV2_Invoice_Talent", "+911112223334")
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 100000}, headers=headers)
        # Extra hours = 10000: 50000/10*2 -> use per_day 100000/2=50000? Simpler:
        # directly craft a shoot day giving exactly 10000 extra: per_day=100000
        # is the talent's own rate field, but budget_total is explicit here,
        # so extra hours are computed independently from budget_per_day.
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_per_day": 100000}, headers=headers)
        await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-09-21", "agreed_hours": 10, "actual_hours": 11}, headers=headers)
        await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": "2500"}, headers=headers)

        r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
        assert r.status_code == 200
        breakdown = r.json()["breakdown"]
        assert breakdown["talent_fee"] == 100000
        assert breakdown["extra_hours"] == 10000
        assert breakdown["commissionable"] == 110000
        assert breakdown["commission_amount"] == 22000
        assert breakdown["reimbursements"] == 2500
        assert breakdown["invoice_amount"] == 90500
        assert breakdown["extra_hours_count"] == 1
        message = r.json()["message"]
        assert "90,500" in message
        assert "Extra Hours: 1 hour — ₹10,000" in message
        assert "Commissionable Amount: ₹1,10,000" in message
        assert "Commission @ 20%: ₹22,000" in message
        assert "Reimbursements: ₹2,500" in message
        assert "Billing Details:" in message
        assert "Talentgram Agency LLP" in message
        assert "GSTIN No: 27AAVFT3898G1Z8" in message
        # V2 polish (spec section 19) — the user explicitly does not want
        # this sentence in the outgoing message anymore.
        assert "not subject to commission" not in message
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_invoice_message_no_overtime_no_reimbursement_omits_lines(client, headers):
    """Spec sections 17/20: flat fee, no extra hours, no reimbursement ->
    message must NOT mention Extra Hours, Commissionable Amount, or
    Reimbursements at all, and the math is the simple base case."""
    pid = await _make_project(commission_percent="15%")
    tid = await _make_talent("ZZZ_TEST_PDV2_Flat_Talent", "+911112223335")
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 50000}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["breakdown"]["invoice_amount"] == 42500
        message = body["message"]
        assert "Extra Hours" not in message
        assert "Commissionable Amount" not in message
        assert "Reimbursements" not in message
        assert "Commission @ 15%: ₹7,500" in message
        assert "Invoice Amount to Talentgram: ₹42,500" in message
        assert "Billing Details:" in message
        assert body["destination_type"] == "phone"
        assert body["whatsapp_group_name"] is None
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_invoice_message_prefers_whatsapp_group_when_set(client, headers):
    """Spec sections 22-24: whatsapp_group_name (the SAME field the
    existing campaign engine already reads) is reported as the preferred
    destination, but the phone number is still returned as the actual
    wa.me target — a group can't be opened via a plain link."""
    pid = await _make_project(commission_percent="15%")
    tid = await _make_talent("ZZZ_TEST_PDV2_Group_Talent", "+911112223336")
    await db.talents.update_one({"id": tid}, {"$set": {"whatsapp_group_name": "ZZZ_TEST_Group_Chat"}})
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 50000}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["destination_type"] == "group"
        assert body["whatsapp_group_name"] == "ZZZ_TEST_Group_Chat"
        assert body["phone"] == "+911112223336"
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_talent_invoice_message_requires_phone(client, headers):
    pid = await _make_project()
    tid = await _make_talent(phone=None)
    await _add_to_pipeline(pid, tid)
    try:
        await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"budget_total": 10000, "commission_percent": 10}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
        assert r.status_code == 400
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# Regression — talent-level fields don't break base flows
# ---------------------------------------------------------------------------
@_aio
async def test_existing_production_desk_shape_unaffected_by_v2(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        card = _find_talent(r.json(), tid)
        # Old fields still present and correctly defaulted with no V2 data.
        assert card["fitting_status"] == "not_scheduled"
        assert card["look_test_status"] == "not_scheduled"
        assert card["shoot_status"] == "not_scheduled"
        assert card["shoot_days"] == []
        assert card["readings_rehearsals"] == []
        assert card["reimbursement_total"] == 0
    finally:
        await _cleanup(pid, [tid])


# ---------------------------------------------------------------------------
# V2 final polish — costume_trial_time, Overview (today/upcoming/completed)
# ---------------------------------------------------------------------------
@_aio
async def test_costume_trial_time_round_trips(client, headers):
    pid = await _make_project()
    tid = await _make_talent()
    await _add_to_pipeline(pid, tid)
    try:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"costume_trial_time": "4:00 PM"}, headers=headers)
        card = _find_talent(r.json(), tid)
        assert card["costume_trial_time"] == "4:00 PM"
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_today_and_upcoming_surface_structured_shoot_days_and_prep_events(client, headers):
    from datetime import date, timedelta

    pid = await _make_project()
    tid = await _make_talent("ZZZ_TEST_PDV2_Today_Talent")
    await _add_to_pipeline(pid, tid)
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    try:
        await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": today_str, "location": "Mumbai"}, headers=headers)
        await client.post(
            f"/api/projects/{pid}/production-desk/talents/{tid}/readings-rehearsals",
            json={"type": "reading", "date": tomorrow_str},
            headers=headers,
        )
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        body = r.json()
        assert any(d["talent_id"] == tid and d["date"] == today_str for d in body["today"]["shoot_days"])
        assert any(e["talent_id"] == tid and e["date"] == tomorrow_str for e in body["upcoming"]["prep_events"])
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_completed_bucket_shows_paid_reimbursement_and_received_tranche(client, headers):
    pid = await _make_project()
    tid = await _make_talent("ZZZ_TEST_PDV2_Completed_Talent")
    await _add_to_pipeline(pid, tid)
    try:
        reimb_resp = await client.post(
            f"/api/projects/{pid}/production-desk/reimbursements",
            data={"talent_id": tid, "expense_type": "Travel", "amount": "1000"},
            headers=headers,
        )
        reimb_id = reimb_resp.json()["reimbursements"][0]["id"]
        await client.patch(f"/api/projects/{pid}/production-desk/reimbursements/{reimb_id}", json={"status": "paid"}, headers=headers)

        tranche_resp = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Advance", "amount": 5000}, headers=headers)
        tranche_id = tranche_resp.json()["tranches"][0]["id"]
        r = await client.patch(f"/api/projects/{pid}/production-desk/tranches/{tranche_id}", json={"payment_status": "received"}, headers=headers)

        body = r.json()
        assert any(x["id"] == reimb_id for x in body["completed"]["reimbursements"])
        assert any(x["id"] == tranche_id for x in body["completed"]["tranches"])
    finally:
        await _cleanup(pid, [tid])


@_aio
async def test_crew_client_ref_includes_crm_peek_fields(client, headers):
    pid = await _make_project()
    client_resp = await client.post(
        "/api/marketing/clients",
        json={"name": "ZZZ_TEST_PDV2_Crew_Contact", "phone_number": "+911234567890", "company_name": "ZZZ_TEST Co", "email": "crew@example.com", "contact_type": "production"},
        headers=headers,
    )
    cid = client_resp.json()["id"]
    try:
        await client.post(f"/api/projects/{pid}/production-desk/crew", json={"client_id": cid, "role": "Producer"}, headers=headers)
        r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
        crew_contact = r.json()["crew"][0]["contact"]
        assert crew_contact["company_name"] == "ZZZ_TEST Co"
        assert crew_contact["email"] == "crew@example.com"
        assert crew_contact["contact_type"] == "production"
    finally:
        await db.project_crew.delete_many({"project_id": pid})
        await _cleanup(pid, client_ids=[cid])
