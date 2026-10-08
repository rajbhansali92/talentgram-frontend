"""Production Desk — the two-sided money model, the talent schedule, Google Maps places,
payment follow-up contacts, and IST day boundaries.

Real FastAPI app + real local dev Mongo (same harness as test_production_desk_v2.py, whose
helpers are reused); no worker runs, nothing is ever sent. Throwaway `zzz-test-pdfin-*` rows
are removed afterwards.

The principle every financial test pins:

    Talent rate  !=  Production quote
    Commission        = commission % x talent agreed rate ONLY        (never on OT, reimbursements or the quote)
    Spread            = production quote - talent rate (+ the OT and reimbursement spreads, see
                        test_production_commercial_model.py)
    Talentgram earning = commission + all spreads
    talent-facing math (invoice)   uses the talent rate only
    production-facing math (follow-up) uses the production quote only
"""
import os

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from core import _now, db
from real_db_isolation import install_real_db
from routers import production_desk as pd
from server import app
from test_production_desk_v2 import (  # noqa: F401  (reuse the established helpers)
    _add_to_pipeline, _cleanup, _find_talent, _make_crm_contact, _make_project, _make_talent,
)

_aio = pytest.mark.asyncio(loop_scope="module")
pytestmark = _aio

_projects: list = []
_talents: list = []
_clients: list = []


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _real_db_for_this_module():
    real_db, restore = install_real_db(_MONGO_URL)
    yield
    try:
        from bson import ObjectId
        for pid in _projects:
            for coll in ("projects", "casting_pipeline", "project_reimbursements", "project_payment_tranches", "project_crew", "project_kickbacks", "workflow_tasks"):
                await real_db[coll].delete_many({"id": pid} if coll == "projects" else {"project_id": pid})
        await real_db.talents.delete_many({"id": {"$in": _talents}})
        if _clients:
            await real_db.clients.delete_many({"_id": {"$in": [ObjectId(c) for c in _clients]}})
    finally:
        restore()


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


# ── helpers ────────────────────────────────────────────────────────────────

async def _project(**kw):
    pid = await _make_project(brand_name=f"ZZZ_TEST_PDFIN_{uuid.uuid4().hex[:5]}", commission_percent="15%", **kw)
    _projects.append(pid)
    return pid


async def _talent(name=None, phone="+911234500000"):
    tid = await _make_talent(name or f"ZZZ_TEST_PDFIN_T_{uuid.uuid4().hex[:5]}", phone)
    _talents.append(tid)
    return tid


async def _locked(client, headers, pid, *, rate, quote=None, commission=None, name=None, per_day=None):
    """A locked talent with an agreed rate (total) and optional production quote / own commission %."""
    tid = await _talent(name)
    await _add_to_pipeline(pid, tid)
    body = {"budget_total": rate}
    if per_day is not None:
        body = {"budget_per_day": per_day}
    if quote is not None:
        body["production_quote"] = quote
    if commission is not None:
        body["commission_percent"] = commission
    r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return tid


async def _desk(client, headers, pid):
    r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _crm(name, phone, **extra):
    res = await db.clients.insert_one({"name": name, "phone_number": phone, "created_at": _now(), "last_contacted_date": _now(), "stage": "lead", "tags": [], **extra})
    cid = str(res.inserted_id)
    _clients.append(cid)
    return cid


# ═══ 1. The money model ════════════════════════════════════════════════════

async def test_talent_rate_equal_to_quote_has_zero_spread_and_earns_only_commission(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)
    card = _find_talent(await _desk(client, headers, pid), tid)
    assert card["talent_agreed_rate"] == 60000 and card["production_quote"] == 60000
    assert card["spread"] == 0
    assert card["commission_amount"] == 9000                    # 15% of the 60,000 RATE
    assert card["talentgram_earning"] == 9000
    assert card["talent_net_payable"] == 51000


async def test_talent_rate_below_quote_adds_the_spread_and_commission_stays_on_the_rate(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    card = _find_talent(await _desk(client, headers, pid), tid)
    assert card["spread"] == 10000
    assert card["commission_amount"] == 7500                    # 15% x 50,000 — NOT 15% x 60,000 (= 9,000)
    assert card["commission_amount"] != 9000
    assert card["talentgram_earning"] == 17500                  # 7,500 + 10,000
    assert card["talent_net_payable"] == 42500                  # 50,000 - 7,500
    assert card["production_billable"] == 60000


async def test_three_talents_are_independent_and_the_project_totals_add_up(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=50000, quote=60000)
    b = await _locked(client, headers, pid, rate=60000, quote=60000)
    c = await _locked(client, headers, pid, rate=40000, quote=60000)
    body = await _desk(client, headers, pid)
    ca, cb, cc = _find_talent(body, a), _find_talent(body, b), _find_talent(body, c)
    assert (ca["commission_amount"], ca["spread"], ca["talentgram_earning"]) == (7500, 10000, 17500)
    assert (cb["commission_amount"], cb["spread"], cb["talentgram_earning"]) == (9000, 0, 9000)
    assert (cc["commission_amount"], cc["spread"], cc["talentgram_earning"]) == (6000, 20000, 26000)
    s = body["summary"]
    assert s["production_basis"] == "per_talent"
    assert s["production_quote_total"] == 180000
    assert s["talent_agreed_total"] == 150000
    assert s["commission_gross"] == 22500
    assert s["spread_total"] == 30000
    assert s["talentgram_earnings_total"] == 52500
    assert s["talent_payable_total"] == 127500                  # 42,500 + 51,000 + 34,000
    assert s["production_billable_total"] == 180000


async def test_different_commission_percentages_per_talent(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=50000, quote=60000, commission=10)
    b = await _locked(client, headers, pid, rate=50000, quote=60000, commission=20)
    body = await _desk(client, headers, pid)
    assert _find_talent(body, a)["commission_amount"] == 5000 and _find_talent(body, a)["talentgram_earning"] == 15000
    assert _find_talent(body, b)["commission_amount"] == 10000 and _find_talent(body, b)["talentgram_earning"] == 20000
    assert body["summary"]["commission_gross"] == 15000


async def test_zero_commission_still_earns_the_spread(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=40000, quote=55000, commission=0)
    card = _find_talent(await _desk(client, headers, pid), tid)
    assert card["commission_amount"] == 0 and card["spread"] == 15000 and card["talentgram_earning"] == 15000


async def test_overtime_and_reimbursements_are_per_talent_and_never_double_counted(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=0, per_day=50000, quote=60000, name="ZZZ_PDFIN_A")
    b = await _locked(client, headers, pid, rate=50000, quote=60000, name="ZZZ_PDFIN_B")
    c = await _locked(client, headers, pid, rate=0, per_day=40000, quote=60000, name="ZZZ_PDFIN_C")
    # A: 1 shoot day, 10h basis, 12 actual -> OT = 50000/10*2 = 10,000
    await client.post(f"/api/projects/{pid}/production-desk/talents/{a}/shoot-days", json={"date": "2026-10-10", "agreed_hours": 10, "actual_hours": 12}, headers=headers)
    # C: 1 shoot day, 8h basis, 10 actual -> OT = 40000/8*2 = 10,000
    await client.post(f"/api/projects/{pid}/production-desk/talents/{c}/shoot-days", json={"date": "2026-10-10", "agreed_hours": 8, "actual_hours": 10}, headers=headers)
    # Reimbursements: B 5,000 ; A 2,000
    for tid, amt in ((b, 5000), (a, 2000)):
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": str(amt), "date": "2026-10-10"}, headers=headers)
        assert r.status_code == 200, r.text
    body = await _desk(client, headers, pid)
    ca, cb, cc = _find_talent(body, a), _find_talent(body, b), _find_talent(body, c)
    assert (ca["extra_hours_total"], ca["reimbursement_total"]) == (10000, 2000)
    assert (cb["extra_hours_total"], cb["reimbursement_total"]) == (0, 5000)
    assert (cc["extra_hours_total"], cc["reimbursement_total"]) == (10000, 0)
    # commission is on the agreed rate ONLY (never on OT, reimbursements or the quote)
    assert ca["commission_amount"] == round(50000 * 0.15, 2)
    assert cb["commission_amount"] == round(50000 * 0.15, 2)
    s = body["summary"]
    assert s["production_overtime_total"] == 20000 and s["production_reimbursements_total"] == 7000
    assert s["production_billable_total"] == 180000 + 20000 + 7000          # quotes + OT + reimbursements, once each
    assert s["talent_payable_total"] == round(sum(t["talent_net_payable"] for t in (ca, cb, cc)), 2)


async def test_legacy_project_without_quotes_keeps_its_project_budget_and_is_flagged_not_invented(client, headers):
    pid = await _project(pd_production_budget_total=85000, pd_shooting_days=1)
    tid = await _locked(client, headers, pid, rate=40000, name="ZZZ_PDFIN_Legacy")
    body = await _desk(client, headers, pid)
    card = _find_talent(body, tid)
    assert card["production_quote"] is None and card["spread"] is None and card["production_billable"] is None
    assert card["talentgram_earning"] == 6000                    # commission only — no invented spread
    s = body["summary"]
    assert s["production_basis"] == "project_budget" and s["production_billable_total"] == 85000
    assert s["spread_total"] == 0
    assert "Production quote missing for 1 talent" in body["needs_attention"]


async def test_partial_quotes_report_incomplete_instead_of_a_misleading_total(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000, quote=60000)
    await _locked(client, headers, pid, rate=50000)
    s = (await _desk(client, headers, pid))["summary"]
    assert s["production_basis"] == "partial" and s["production_quotes_missing"] == 1
    assert s["client_outstanding_total"] is None


async def test_quote_below_the_talent_rate_is_flagged(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=45000, name="ZZZ_PDFIN_Under")
    body = await _desk(client, headers, pid)
    assert _find_talent(body, tid)["spread"] == -5000
    assert any(x.startswith("Production quote below talent rate") for x in body["needs_attention"])


async def test_client_outstanding_uses_received_tranches_and_the_received_tick(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000, quote=60000)
    assert (await _desk(client, headers, pid))["summary"]["client_outstanding_total"] == 60000
    await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Advance", "amount": 20000, "payment_status": "received"}, headers=headers)
    s = (await _desk(client, headers, pid))["summary"]
    assert s["client_received_total"] == 20000 and s["client_outstanding_total"] == 40000
    r = await client.patch(f"/api/projects/{pid}/production-desk", json={"payment_in_received": True}, headers=headers)
    s = r.json()["summary"]
    assert s["client_outstanding_total"] == 0                    # an explicit "client payment received" means paid in full


async def test_production_quote_validation_and_clearing(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"production_quote": -1}, headers=headers)
    assert r.status_code == 422
    r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json={"production_quote": None}, headers=headers)
    assert _find_talent(r.json(), tid)["production_quote"] is None
    # the talent's own rate was never touched by any of this
    assert _find_talent(r.json(), tid)["talent_agreed_rate"] == 50000


# ═══ 2. Talent invoice stays talent-facing ═════════════════════════════════

async def test_invoice_message_uses_the_talent_rate_never_the_production_quote(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000, name="ZZZ_PDFIN_Invoice")
    r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    text = body["message"]
    assert "Talent Fee: ₹50,000" in text
    assert "Commission @ 15% on Talent Fee: ₹7,500" in text
    assert "Invoice Amount to Talentgram: ₹42,500" in text
    assert body["breakdown"]["invoice_amount"] == 42500
    # nothing of the production side may reach a talent-facing surface
    assert "60,000" not in text and "10,000" not in text
    blob = json.dumps(body).lower()
    for forbidden in ("production_quote", "spread", "production_billable", "talentgram_earning", "17,500", "17500"):
        assert forbidden not in blob


async def test_invoice_includes_the_talents_own_overtime_and_reimbursement_only(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=0, per_day=50000, quote=90000, name="ZZZ_PDFIN_InvOT")
    await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-10", "agreed_hours": 10, "actual_hours": 12}, headers=headers)
    await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": "2500", "date": "2026-10-10"}, headers=headers)
    body = (await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)).json()
    # commission is 15% of the 50,000 rate only = 7,500 (the 10,000 OT is not commissionable);
    # invoice = 50,000 + 10,000 OT − 7,500 + 2,500 reimbursement
    assert body["breakdown"]["commissionable"] == 50000
    assert body["breakdown"]["commission_amount"] == 7500
    assert body["breakdown"]["invoice_amount"] == 55000
    assert "90,000" not in body["message"]                       # the quote is nowhere in it


# ═══ 3. Payment follow-up is production-facing ═════════════════════════════

async def _followup_project(client, headers):
    """quote 60,000 | talent rate 50,000 | OT 5,000 | reimbursement 2,000 | received 20,000"""
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=0, per_day=50000, quote=60000, name="ZZZ_PDFIN_FU")
    await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-10", "agreed_hours": 10, "actual_hours": 11}, headers=headers)
    await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data={"talent_id": tid, "expense_type": "Travel", "amount": "2000", "date": "2026-10-10"}, headers=headers)
    await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Advance", "amount": 20000, "payment_status": "received"}, headers=headers)
    return pid, tid


async def test_follow_up_message_is_production_side_quote_plus_ot_plus_reimbursements_minus_received(client, headers):
    pid, _tid = await _followup_project(client, headers)
    cid = await _crm("ZZZ_PDFIN_Producer", "+919900000001")
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": cid}, headers=headers)
    r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    text = body["message"]
    assert "Production amount: ₹60,000" in text
    assert "Overtime: ₹5,000" in text
    assert "Reimbursements: ₹2,000" in text
    assert "Total due: ₹67,000" in text
    assert "Received: ₹20,000" in text
    assert "Outstanding: ₹47,000" in text
    assert body["breakdown"]["outstanding"] == 47000 and body["warnings"] == []
    # the talent's own rate / Talentgram's margin never appear in a production-facing message
    assert "50,000" not in text and "10,000" not in text and "spread" not in text.lower()
    # same numbers the desk summary shows
    s = (await _desk(client, headers, pid))["summary"]
    assert s["production_billable_total"] == body["breakdown"]["total_due"] and s["client_outstanding_total"] == body["breakdown"]["outstanding"]


async def test_follow_up_without_a_quote_has_no_amounts_and_says_why(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000)
    cid = await _crm("ZZZ_PDFIN_NoQuote", "+919900000002")
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": cid}, headers=headers)
    body = (await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)).json()
    assert "Outstanding" not in body["message"] and "Total due" not in body["message"]
    assert body["warnings"] and "No production quote" in body["warnings"][0]


async def test_follow_up_with_partial_quotes_omits_amounts_and_warns(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000, quote=60000)
    await _locked(client, headers, pid, rate=50000)
    cid = await _crm("ZZZ_PDFIN_Partial", "+919900000003")
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": cid}, headers=headers)
    body = (await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)).json()
    assert "Outstanding" not in body["message"]
    assert "missing for 1 talent" in body["warnings"][0]


async def test_follow_up_contacts_are_the_projects_own_crm_contacts_only(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000, quote=60000)
    producer = await _crm("ZZZ_PDFIN_Producer", "+919900000011", company_name="ZZZ Films Pvt Ltd")
    accounts = await _crm("ZZZ_PDFIN_Accounts", "+919900000012", company_name="ZZZ Films Pvt Ltd", designation="Accounts")
    stranger = await _crm("ZZZ_PDFIN_Stranger", "+919900000013", company_name="Unrelated Co")
    nophone = await _crm("ZZZ_PDFIN_NoPhone", "", company_name="ZZZ Films Pvt Ltd")
    await client.post(f"/api/projects/{pid}/production-desk/crew", json={"client_id": producer, "role": "Producer"}, headers=headers)
    r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-contacts", headers=headers)
    assert r.status_code == 200
    by_id = {c["client_id"]: c for c in r.json()["contacts"]}
    assert producer in by_id and by_id[producer]["source"] == "crew" and by_id[producer]["role"] == "Producer"
    assert accounts in by_id and by_id[accounts]["source"] == "same_company"
    assert by_id[nophone]["has_phone"] is False
    assert stranger not in by_id                                       # unrelated CRM contact is never offered


async def test_follow_up_goes_to_the_selected_contact_and_nobody_else(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, rate=50000, quote=60000)
    producer = await _crm("ZZZ_PDFIN_Producer", "+919900000021", company_name="ZZZ Co")
    accounts = await _crm("ZZZ_PDFIN_Accounts", "+919900000022", company_name="ZZZ Co")
    stranger = await _crm("ZZZ_PDFIN_Stranger", "+919900000023", company_name="Other Co")
    nophone = await _crm("ZZZ_PDFIN_NoPhone", "", company_name="ZZZ Co")
    await client.post(f"/api/projects/{pid}/production-desk/crew", json={"client_id": producer, "role": "Producer"}, headers=headers)
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": producer}, headers=headers)
    url = f"/api/projects/{pid}/production-desk/payment-followup-message"

    # default = the saved concerned person
    d = (await client.get(url, headers=headers)).json()
    assert d["contact_id"] == producer and d["phone"] == "+919900000021" and d["contact_name"] == "ZZZ_PDFIN_Producer"
    # explicit selection of a same-company contact -> THEIR number, never the default's
    s = (await client.get(url, params={"contact_id": accounts}, headers=headers)).json()
    assert s["contact_id"] == accounts and s["phone"] == "+919900000022" and s["message"].startswith("Hi ZZZ_PDFIN_Accounts,")
    # an unrelated CRM contact, an unknown id and a contact with no phone are all refused
    assert (await client.get(url, params={"contact_id": stranger}, headers=headers)).status_code == 400
    assert (await client.get(url, params={"contact_id": "0" * 24}, headers=headers)).status_code == 400
    assert (await client.get(url, params={"contact_id": "not-an-id"}, headers=headers)).status_code == 400
    assert (await client.get(url, params={"contact_id": nophone}, headers=headers)).status_code == 400


async def test_follow_up_still_requires_a_contact_when_none_is_chosen_or_saved(client, headers):
    pid = await _project()
    r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)
    assert r.status_code == 400


# ═══ 4. Talent shooting schedule ═══════════════════════════════════════════

async def test_each_talent_keeps_its_own_dates_times_and_locations(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=50000, quote=60000, name="ZZZ_PDFIN_SA")
    b = await _locked(client, headers, pid, rate=60000, quote=60000, name="ZZZ_PDFIN_SB")
    ra = await client.post(f"/api/projects/{pid}/production-desk/talents/{a}/shoot-days", json={
        "date": "2026-10-08", "reporting_time": "08:00", "call_time": "09:00", "location": "Studio A", "notes": "Bring wardrobe"}, headers=headers)
    rb = await client.post(f"/api/projects/{pid}/production-desk/talents/{b}/shoot-days", json={
        "date": "2026-10-09", "reporting_time": "10:00", "call_time": "11:00", "location": "Studio B"}, headers=headers)
    assert ra.status_code == 200 and rb.status_code == 200
    body = await _desk(client, headers, pid)                       # reload from the server
    da = _find_talent(body, a)["shoot_days"][0]
    db_ = _find_talent(body, b)["shoot_days"][0]
    assert (da["date"], da["reporting_time"], da["call_time"], da["location"], da["notes"]) == ("2026-10-08", "08:00", "09:00", "Studio A", "Bring wardrobe")
    assert (db_["date"], db_["reporting_time"], db_["call_time"], db_["location"]) == ("2026-10-09", "10:00", "11:00", "Studio B")


async def test_times_are_stored_as_canonical_ist_hh_mm(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-08", "call_time": "9:30 AM", "reporting_time": "8 am"}, headers=headers)
    day = _find_talent(r.json(), tid)["shoot_days"][0]
    assert (day["call_time"], day["reporting_time"]) == ("09:30", "08:00")


async def test_reporting_later_than_call_is_rejected_on_add_and_edit(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    base = f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days"
    bad = await client.post(base, json={"date": "2026-10-08", "call_time": "09:00", "reporting_time": "10:00"}, headers=headers)
    assert bad.status_code == 400 and "later than call" in bad.json()["detail"]
    ok = await client.post(base, json={"date": "2026-10-08", "call_time": "09:00", "reporting_time": "09:00"}, headers=headers)   # equal is fine
    assert ok.status_code == 200
    day_id = _find_talent(ok.json(), tid)["shoot_days"][0]["id"]
    bad_edit = await client.patch(f"{base}/{day_id}", json={"reporting_time": "09:30"}, headers=headers)
    assert bad_edit.status_code == 400
    unparsable = await client.patch(f"{base}/{day_id}", json={"call_time": "sometime"}, headers=headers)
    assert unparsable.status_code == 400
    # nothing was changed by the rejected edits
    day = _find_talent(await _desk(client, headers, pid), tid)["shoot_days"][0]
    assert (day["call_time"], day["reporting_time"]) == ("09:00", "09:00")


async def test_legacy_free_text_times_survive_unrelated_edits(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    day_id = str(uuid.uuid4())
    await db.casting_pipeline.update_one({"project_id": pid, "talent_id": tid}, {"$set": {"pd_shoot_days": [
        {"id": day_id, "date": "2026-10-08", "call_time": "8 AM sharp", "reporting_time": "7:15", "location": "Old studio", "shoot_status": "scheduled"}]}})
    base = f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days/{day_id}"
    r = await client.patch(base, json={"notes": "just a note"}, headers=headers)
    assert r.status_code == 200
    day = _find_talent(r.json(), tid)["shoot_days"][0]
    assert day["call_time"] == "8 AM sharp" and day["reporting_time"] == "7:15"    # untouched
    # an unparseable legacy call time cannot trigger the reporting<=call check
    assert (await client.patch(base, json={"reporting_time": "23:00"}, headers=headers)).status_code == 200


async def test_a_selected_google_place_is_stored_and_a_typed_location_clears_it(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    base = f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days"
    r = await client.post(base, json={
        "date": "2026-10-08", "location": "Film City", "location_address": "Goregaon East, Mumbai, Maharashtra",
        "location_place_id": "ChIJplace12345", "location_lat": 19.16, "location_lng": 72.88}, headers=headers)
    assert r.status_code == 200
    day = _find_talent(r.json(), tid)["shoot_days"][0]
    assert (day["location_address"], day["location_place_id"], day["location_lat"], day["location_lng"]) == ("Goregaon East, Mumbai, Maharashtra", "ChIJplace12345", 19.16, 72.88)
    assert day["location_map_url"] == "https://www.google.com/maps/search/?api=1&query=19.16,72.88&query_place_id=ChIJplace12345"
    # a different typed location must not keep the old place's address/coordinates/link
    r2 = await client.patch(f"{base}/{day['id']}", json={"location": "Plain typed studio"}, headers=headers)
    day2 = _find_talent(r2.json(), tid)["shoot_days"][0]
    assert day2["location"] == "Plain typed studio"
    assert day2["location_address"] is None and day2["location_place_id"] is None and day2["location_lat"] is None and day2["location_map_url"] is None
    # coordinates are range-checked
    bad = await client.post(base, json={"date": "2026-10-09", "location_lat": 123.0}, headers=headers)
    assert bad.status_code == 422


async def test_shoot_days_can_be_edited_and_removed_and_the_count_follows(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=0, per_day=10000, quote=15000)
    base = f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days"
    for d in ("2026-10-08", "2026-10-09"):
        r = await client.post(base, json={"date": d}, headers=headers)
    card = _find_talent(r.json(), tid)
    assert card["shooting_days"] == 2 and card["talent_agreed_rate"] == 20000
    first = card["shoot_days"][0]["id"]
    r = await client.patch(f"{base}/{first}", json={"location": "Moved"}, headers=headers)
    assert _find_talent(r.json(), tid)["shoot_days"][0]["location"] == "Moved"
    r = await client.delete(f"{base}/{first}", headers=headers)
    card = _find_talent(r.json(), tid)
    assert card["shooting_days"] == 1 and len(card["shoot_days"]) == 1


async def test_the_project_level_shoot_date_list_is_gone_but_its_old_data_is_left_alone(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000)
    await db.projects.update_one({"id": pid}, {"$set": {"pd_shoot_dates_list": ["2026-09-20"]}})   # pre-existing data
    r = await client.patch(f"/api/projects/{pid}/production-desk", json={"shoot_dates_list": ["2026-10-01"]}, headers=headers)
    assert r.status_code == 400 and "per talent" in r.json()["detail"]
    r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days/use-project-dates", headers=headers)
    assert r.status_code in (404, 405)
    body = await _desk(client, headers, pid)
    assert "pd_shoot_dates_list" not in body["project"]
    # other project edits still work, and the old stored field is never rewritten or deleted
    await client.patch(f"/api/projects/{pid}/production-desk", json={"shoot_notes": "x"}, headers=headers)
    assert (await db.projects.find_one({"id": pid}, {"_id": 0, "pd_shoot_dates_list": 1}))["pd_shoot_dates_list"] == ["2026-09-20"]


async def test_shoot_details_are_complete_once_every_locked_talent_has_a_full_schedule_day(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=50000, quote=60000, name="ZZZ_PDFIN_Done")
    assert "Shoot details incomplete" in (await _desk(client, headers, pid))["needs_attention"]
    await client.post(f"/api/projects/{pid}/production-desk/talents/{a}/shoot-days", json={"date": "2026-10-08", "call_time": "09:00", "location": "Studio A"}, headers=headers)
    assert "Shoot details incomplete" not in (await _desk(client, headers, pid))["needs_attention"]


# ═══ 5. Google Maps place search (server-side proxy) ═══════════════════════

class _FakeGoogle:
    """Replaces httpx.AsyncClient inside the router with a MockTransport so no network is used and the
    request Google would receive (including its API key header) can be inspected."""
    def __init__(self, monkeypatch, handler):
        self.requests = []
        real = httpx.AsyncClient

        def wrapped(handler_):
            def handle(request):
                self.requests.append(request)
                return handler_(request)
            return handle
        monkeypatch.setattr(pd.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(wrapped(handler)), **{k: v for k, v in kw.items() if k != "transport"}))


async def test_place_search_is_unavailable_without_a_server_key(client, headers, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    pid = await _project()
    r = await client.get(f"/api/projects/{pid}/production-desk/places/search", params={"q": "Film City"}, headers=headers)
    assert r.status_code == 503
    r = await client.get(f"/api/projects/{pid}/production-desk/places/ChIJplace12345", headers=headers)
    assert r.status_code == 503


async def test_place_search_and_details_use_the_server_key_and_never_return_it(client, headers, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "SECRET-KEY-123")

    def handler(request):
        assert request.headers["x-goog-api-key"] == "SECRET-KEY-123"
        if request.url.path.endswith("places:autocomplete"):
            return httpx.Response(200, json={"suggestions": [{"placePrediction": {
                "placeId": "ChIJplace12345", "text": {"text": "Film City, Goregaon East"},
                "structuredFormat": {"mainText": {"text": "Film City"}, "secondaryText": {"text": "Goregaon East, Mumbai"}}}}]})
        return httpx.Response(200, json={"id": "ChIJplace12345", "displayName": {"text": "Film City"},
                                         "formattedAddress": "Goregaon East, Mumbai, Maharashtra",
                                         "location": {"latitude": 19.16, "longitude": 72.88}, "googleMapsUri": "https://maps.google.com/?cid=99"})
    fake = _FakeGoogle(monkeypatch, handler)
    pid = await _project()
    s = await client.get(f"/api/projects/{pid}/production-desk/places/search", params={"q": "Film City", "session": "abc"}, headers=headers)
    assert s.status_code == 200
    assert s.json()["results"] == [{"place_id": "ChIJplace12345", "name": "Film City", "address": "Goregaon East, Mumbai"}]
    d = await client.get(f"/api/projects/{pid}/production-desk/places/ChIJplace12345", headers=headers)
    assert d.status_code == 200
    assert d.json() == {"place_id": "ChIJplace12345", "name": "Film City", "address": "Goregaon East, Mumbai, Maharashtra",
                        "lat": 19.16, "lng": 72.88, "maps_url": "https://maps.google.com/?cid=99"}
    assert "SECRET-KEY-123" not in s.text and "SECRET-KEY-123" not in d.text
    assert json.loads(fake.requests[0].content)["regionCode"] == "IN"


async def test_the_desk_reports_whether_place_search_is_configured(client, headers, monkeypatch):
    pid = await _project()
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert (await _desk(client, headers, pid))["capabilities"] == {"places_search": False}
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "some-key")
    body = await _desk(client, headers, pid)
    assert body["capabilities"] == {"places_search": True}
    assert "some-key" not in json.dumps(body)                       # only the boolean ever leaves the server


async def test_place_endpoints_validate_input_and_survive_google_errors(client, headers, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _FakeGoogle(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    pid = await _project()
    assert (await client.get(f"/api/projects/{pid}/production-desk/places/search", params={"q": "ab"}, headers=headers)).status_code == 422     # too short
    assert (await client.get(f"/api/projects/{pid}/production-desk/places/search", params={"q": "Film City"}, headers=headers)).status_code == 502
    assert (await client.get(f"/api/projects/{pid}/production-desk/places/ChIJplace12345", headers=headers)).status_code == 502
    assert (await client.get(f"/api/projects/{pid}/production-desk/places/bad id!", headers=headers)).status_code in (400, 404)
    assert (await client.get("/api/projects/does-not-exist/production-desk/places/search", params={"q": "Film City"}, headers=headers)).status_code == 404


# ═══ 6. IST ═══════════════════════════════════════════════════════════════

def test_clock_parsing_and_normalising():
    assert pd.parse_clock("09:30") == 570 and pd.parse_clock("9:30 AM") == 570 and pd.parse_clock("9 pm") == 1260
    assert pd.parse_clock("12 AM") == 0 and pd.parse_clock("12:15 pm") == 735 and pd.parse_clock("23:59") == 1439
    for bad in ("24:00", "morning", "", None, "9.30"):
        assert pd.parse_clock(bad) is None
    assert pd.normalize_clock("9 pm", "call_time") == "21:00" and pd.normalize_clock("  ", "call_time") is None


def test_ist_today_and_day_bounds_follow_the_indian_calendar_date(monkeypatch):
    from zoneinfo import ZoneInfo
    # 2026-10-07 19:00 UTC == 2026-10-08 00:30 IST -> "today" is Oct 8 (the UTC date would say Oct 7)
    monkeypatch.setattr(pd, "ist_now", lambda: datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")))
    assert pd.ist_today().isoformat() == "2026-10-08"
    # bounds are that IST DATE's own 00:00-24:00 in the stored-value tagging (not a UTC conversion of IST midnight)
    assert pd.ist_day_bounds_utc() == ("2026-10-08T00:00:00+00:00", "2026-10-09T00:00:00+00:00")
    # and at 18:00 UTC (23:30 IST) it is still Oct 7 in IST
    monkeypatch.setattr(pd, "ist_now", lambda: datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")))
    assert pd.ist_today().isoformat() == "2026-10-07"
    assert pd.ist_day_bounds_utc() == ("2026-10-07T00:00:00+00:00", "2026-10-08T00:00:00+00:00")


def test_workflow_today_bounds_are_ist_too(monkeypatch):
    from routers import workflow

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed
    monkeypatch.setattr(workflow, "datetime", _Fixed)
    # 19:00 UTC is already Oct 8 in India
    assert workflow._today_bounds_utc() == ("2026-10-08T00:00:00+00:00", "2026-10-09T00:00:00+00:00")


def test_management_agent_resolves_relative_dates_against_the_ist_calendar(monkeypatch):
    from agents.modules import management_agent as ma

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)     # 00:30 IST Thu 8 Oct
            return fixed.astimezone(tz) if tz else fixed
    monkeypatch.setattr(ma, "datetime", _Fixed)
    assert ma._parse_due_date("today") == "2026-10-08T12:00:00+00:00"       # UTC would have said Oct 7
    assert ma._parse_due_date("tomorrow") == "2026-10-09T12:00:00+00:00"
    assert ma._parse_due_date("monday") == "2026-10-12T12:00:00+00:00"       # next Monday after Thu 8 Oct
    # a bare time keeps the hour as typed, on the IST date
    assert ma._parse_absolute_datetime("trial at 7 PM") == "2026-10-08T19:00:00+00:00"


async def test_desk_today_and_tasks_follow_the_ist_day_not_the_utc_day(client, headers, monkeypatch):
    """At 00:30 IST on Oct 8 (still Oct 7 in UTC) a shoot dated Oct 8 is TODAY and a task due that morning is
    due today — with a UTC cut-over both would have been wrongly 'upcoming'. Stored values are bucketed by the
    date they are written with, so an evening-hour trial stays on its own day."""
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(pd, "ist_now", lambda: datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")))
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=50000, quote=60000, name="ZZZ_PDFIN_Today")
    await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-08", "call_time": "09:00", "location": "Studio"}, headers=headers)
    await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-09"}, headers=headers)
    await db.workflow_tasks.insert_one({"id": f"zzz-test-pdfin-task-{uuid.uuid4().hex[:6]}", "project_id": pid, "title": "ZZZ due this morning IST", "status": "pending",
                                        "due_at": "2026-10-08T01:00:00+00:00", "created_at": _now(), "updated_at": _now()})
    await db.workflow_tasks.insert_one({"id": f"zzz-test-pdfin-task-{uuid.uuid4().hex[:6]}", "project_id": pid, "title": "ZZZ due last night IST", "status": "pending",
                                        "due_at": "2026-10-07T17:00:00+00:00", "created_at": _now(), "updated_at": _now()})
    body = await _desk(client, headers, pid)
    # a 7 PM trial typed as "7 PM" (stored 19:00 with a UTC tag) is still TODAY's trial, not tomorrow's
    await db.casting_pipeline.update_one({"project_id": pid, "talent_id": tid}, {"$set": {"pd_costume_trial_at": "2026-10-08T19:00:00+00:00"}})
    body = await _desk(client, headers, pid)
    assert [c["talent_id"] for c in body["today"]["trials"]] == [tid]
    assert [d["date"] for d in body["today"]["shoot_days"]] == ["2026-10-08"]
    assert [d["date"] for d in body["upcoming"]["shoot_days"]] == ["2026-10-09"]
    assert [t["title"] for t in body["today"]["tasks"]] == ["ZZZ due this morning IST"]
    assert [t["title"] for t in body["tasks"]["overdue"]] == ["ZZZ due last night IST"]
