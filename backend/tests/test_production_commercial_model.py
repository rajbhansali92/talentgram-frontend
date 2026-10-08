"""The definitive commercial model of the Production Desk (see _talent_card's docstring).

  WHAT WE TELL THE TALENT   rate + talent OT + talent reimbursements; commission = rate x % ONLY
  WHAT WE TELL PRODUCTION   quote + production OT + production reimbursements
  WHAT TALENTGRAM EARNS     commission + quote spread + OT spread + reimbursement spread

Production OT / reimbursement are independently editable and default to the talent's own amount, so
projects that never entered one keep their numbers. Real FastAPI app + real local dev Mongo; nothing
is sent; throwaway `zzz-test-pdcm-*` rows are removed afterwards.
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
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from core import _now, db
from real_db_isolation import install_real_db
from routers import production_desk as pd
from server import app
from test_production_desk_v2 import _add_to_pipeline, _find_talent, _make_project, _make_talent

pytestmark = pytest.mark.asyncio(loop_scope="module")

TAG = "ZZZ_PDCM"
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def headers(client):
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _project(**kw):
    pid = await _make_project(brand_name=f"{TAG}_{uuid.uuid4().hex[:5]}", commission_percent="15%", **kw)
    _projects.append(pid)
    return pid


async def _locked(client, headers, pid, name, *, rate, quote, ot=0, prod_ot=None, reimb=0, prod_reimb=None, commission=None):
    """A locked talent. Talent OT is the real one: shoot-day hours x the talent's per-day rate, sized to give exactly `ot`."""
    tid = await _make_talent(f"{TAG}_{name}_{uuid.uuid4().hex[:4]}", "+911234500000")
    _talents.append(tid)
    await _add_to_pipeline(pid, tid)
    body = {"budget_total": rate, "production_quote": quote}
    if commission is not None:
        body["commission_percent"] = commission
    if ot:
        body["budget_per_day"] = ot * 10          # 10 agreed hours, 1 extra hour => OT = per_day / 10 = `ot`
    if prod_ot is not None:
        body["production_overtime"] = prod_ot
    r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    if ot:
        r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": "2026-10-10", "agreed_hours": 10, "actual_hours": 11}, headers=headers)
        assert r.status_code == 200, r.text
    if reimb or prod_reimb is not None:
        data = {"talent_id": tid, "expense_type": "Travel", "amount": str(reimb), "date": "2026-10-10"}
        if prod_reimb is not None:
            data["production_amount"] = str(prod_reimb)
        r = await client.post(f"/api/projects/{pid}/production-desk/reimbursements", data=data, headers=headers)
        assert r.status_code == 200, r.text
    return tid


async def _desk(client, headers, pid):
    r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ═══ A. commission is on the agreed rate ONLY ══════════════════════════════

async def test_commission_is_the_agreed_rate_times_percent_and_nothing_else(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Rate", rate=50000, quote=60000, ot=5000, prod_ot=7000, reimb=2000, prod_reimb=3000)
    c = _find_talent(await _desk(client, headers, pid), tid)
    assert c["commission_percent"] == 15 and c["commission_amount"] == 7500          # 15% x 50,000
    assert c["commissionable_amount"] == 50000                                      # B. OT is paid but never commissionable
    assert c["extra_hours_total"] == 5000


# ═══ the single-talent worked example ═════════════════════════════════════

async def test_worked_example_talentgram_earns_20500(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Ex", rate=50000, quote=60000, ot=5000, prod_ot=7000, reimb=2000, prod_reimb=3000)
    body = await _desk(client, headers, pid)
    c = _find_talent(body, tid)
    assert (c["talent_agreed_rate"], c["production_quote"]) == (50000, 60000)       # C. quote differs from rate
    assert c["commission_amount"] == 7500
    assert c["spread"] == c["quote_spread"] == 10000                                 # H. quote spread
    assert (c["extra_hours_total"], c["production_overtime"], c["ot_spread"]) == (5000, 7000, 2000)          # D / I
    assert (c["reimbursement_total"], c["production_reimbursement_total"], c["reimbursement_spread"]) == (2000, 3000, 1000)   # E / J
    assert c["talentgram_earning"] == 7500 + 10000 + 2000 + 1000 == 20500            # K
    assert c["talent_net_payable"] == 50000 + 5000 - 7500 + 2000 == 49500             # talent side only
    assert c["talent_gross_payable"] == 57000
    assert c["production_billable"] == 60000 + 7000 + 3000 == 70000                   # production side only
    s = body["summary"]
    assert (s["commission_gross"], s["quote_spread_total"], s["ot_spread_total"], s["reimbursement_spread_total"], s["talentgram_earnings_total"]) == (7500, 10000, 2000, 1000, 20500)
    assert s["production_billable_total"] == 70000 and s["production_overtime_total"] == 7000 and s["production_reimbursements_total"] == 3000


# ═══ F. nothing entered separately -> production side equals the talent side ═

async def test_existing_projects_without_production_specific_values_keep_their_numbers(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Legacy", rate=50000, quote=60000, ot=5000, reimb=2000)       # no production OT / reimbursement entered
    body = await _desk(client, headers, pid)
    c = _find_talent(body, tid)
    assert c["production_overtime"] == c["extra_hours_total"] == 5000 and c["production_overtime_is_explicit"] is False
    assert c["production_reimbursement_total"] == c["reimbursement_total"] == 2000
    assert (c["ot_spread"], c["reimbursement_spread"]) == (0, 0)
    assert c["production_billable"] == 60000 + 5000 + 2000                             # exactly what it was before
    assert c["talentgram_earning"] == 7500 + 10000                                     # commission + quote spread
    s = body["summary"]
    assert s["production_billable_total"] == 67000 and s["talentgram_earnings_total"] == 17500
    assert (s["ot_spread_total"], s["reimbursement_spread_total"]) == (0, 0)


async def test_production_ot_and_reimbursement_are_independently_editable_and_revertible(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Edit", rate=50000, quote=60000, ot=5000, reimb=2000)
    rid = (await _desk(client, headers, pid))["reimbursements"][0]["id"]
    url = f"/api/projects/{pid}/production-desk"
    # production OT: set, then null clears it (follows the talent's OT again)
    r = await client.patch(f"{url}/talents/{tid}", json={"production_overtime": 9000}, headers=headers)
    assert _find_talent(r.json(), tid)["production_overtime"] == 9000 and _find_talent(r.json(), tid)["ot_spread"] == 4000
    r = await client.patch(f"{url}/talents/{tid}", json={"production_overtime": None}, headers=headers)
    assert _find_talent(r.json(), tid)["production_overtime"] == 5000 and _find_talent(r.json(), tid)["ot_spread"] == 0
    # production reimbursement: its own amount per reimbursement; the talent's amount is never touched
    r = await client.patch(f"{url}/reimbursements/{rid}", json={"production_amount": 2600}, headers=headers)
    assert r.status_code == 200
    c = _find_talent(r.json(), tid)
    assert (c["reimbursement_total"], c["production_reimbursement_total"], c["reimbursement_spread"]) == (2000, 2600, 600)
    assert r.json()["reimbursements"][0]["amount"] == 2000
    r = await client.patch(f"{url}/reimbursements/{rid}", json={"production_amount": None}, headers=headers)
    assert _find_talent(r.json(), tid)["production_reimbursement_total"] == 2000
    # status updates still work and do not disturb the production amount
    await client.patch(f"{url}/reimbursements/{rid}", json={"production_amount": 2200}, headers=headers)
    r = await client.patch(f"{url}/reimbursements/{rid}", json={"status": "paid"}, headers=headers)
    assert r.status_code == 200 and _find_talent(r.json(), tid)["production_reimbursement_total"] == 2200
    # validation
    assert (await client.patch(f"{url}/reimbursements/{rid}", json={}, headers=headers)).status_code == 400
    assert (await client.patch(f"{url}/reimbursements/{rid}", json={"status": "nope"}, headers=headers)).status_code == 400
    assert (await client.patch(f"{url}/talents/{tid}", json={"production_overtime": -1}, headers=headers)).status_code == 422
    assert (await client.patch(f"{url}/reimbursements/{rid}", json={"production_amount": -5}, headers=headers)).status_code == 422


# ═══ G. several talents, each calculated on its own, totals reconcile ══════

async def test_three_talents_calculate_independently_and_the_totals_reconcile(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, "A", rate=50000, quote=60000, ot=5000, prod_ot=7000, reimb=2000, prod_reimb=3000)
    b = await _locked(client, headers, pid, "B", rate=60000, quote=60000)
    c = await _locked(client, headers, pid, "C", rate=40000, quote=60000, ot=3000, prod_ot=5000, reimb=1000, prod_reimb=2000)
    body = await _desk(client, headers, pid)
    ca, cb, cc = (_find_talent(body, t) for t in (a, b, c))
    # A
    assert (ca["commission_amount"], ca["spread"], ca["ot_spread"], ca["reimbursement_spread"], ca["talentgram_earning"]) == (7500, 10000, 2000, 1000, 20500)
    assert (ca["talent_net_payable"], ca["production_billable"]) == (49500, 70000)
    # B: nothing separate -> only commission (15% x 60,000)
    assert (cb["commission_amount"], cb["spread"], cb["ot_spread"], cb["reimbursement_spread"], cb["talentgram_earning"]) == (9000, 0, 0, 0, 9000)
    assert (cb["talent_net_payable"], cb["production_billable"]) == (51000, 60000)
    # C: 15% x 40,000 = 6,000
    assert (cc["commission_amount"], cc["spread"], cc["ot_spread"], cc["reimbursement_spread"], cc["talentgram_earning"]) == (6000, 20000, 2000, 1000, 29000)
    assert (cc["talent_net_payable"], cc["production_billable"]) == (38000, 67000)
    s = body["summary"]
    assert s["commission_gross"] == 22500 and s["quote_spread_total"] == 30000
    assert (s["ot_spread_total"], s["reimbursement_spread_total"]) == (4000, 2000)
    assert s["talentgram_earnings_total"] == 58500 == ca["talentgram_earning"] + cb["talentgram_earning"] + cc["talentgram_earning"]
    assert s["production_billable_total"] == 197000 == ca["production_billable"] + cb["production_billable"] + cc["production_billable"]
    assert s["talent_payable_total"] == 138500
    # what production pays = what the talents get + Talentgram's commission + every spread
    assert s["production_billable_total"] == s["talent_payable_total"] + s["commission_gross"] + s["quote_spread_total"] + s["ot_spread_total"] + s["reimbursement_spread_total"]
    # the global Production Desk shows the same figures
    r = await client.get("/api/production/overview", params={"q": TAG, "status": "all", "size": 100}, headers=headers)
    row = next(i for i in r.json()["items"] if i["project_id"] == pid)
    m = row["money"]
    assert (m["commission"], m["spread"], m["ot_spread"], m["reimbursement_spread"], m["earnings"]) == (22500, 30000, 4000, 2000, 58500)
    assert (m["production_total"], m["talent_payable_total"], m["overtime"], m["reimbursements"]) == (197000, 138500, 12000, 5000)
    assert (m["talent_overtime"], m["talent_reimbursements"]) == (8000, 3000)


# ═══ L. the talent-facing invoice never exposes a production number ════════

async def test_talent_invoice_exposes_nothing_from_the_production_side(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Inv", rate=40000, quote=60000, ot=3000, prod_ot=5000, reimb=1000, prod_reimb=2000, commission=15)
    r = await client.get(f"/api/projects/{pid}/production-desk/talents/{tid}/invoice-message", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    text = body["message"]
    assert "Talent Fee: ₹40,000" in text
    assert "Extra Hours: 1 hour — ₹3,000" in text                       # the TALENT's overtime
    assert "Commission @ 15% on Talent Fee: ₹6,000" in text             # 15% x 40,000 — not on the 3,000 OT
    assert "Reimbursements: ₹1,000" in text
    assert "Invoice Amount to Talentgram: ₹38,000" in text              # 40,000 + 3,000 − 6,000 + 1,000
    for forbidden in ("60,000", "67,000", "20,000", "29,000", "5,000", "2,000", "Commissionable"):
        assert forbidden not in text, forbidden
    blob = json.dumps(body).lower()
    for forbidden in ("production", "spread", "earning", "quote", "billable"):
        assert forbidden not in blob, forbidden
    assert body["breakdown"] == {
        "talent_fee": 40000, "extra_hours": 3000, "extra_hours_count": 1, "commissionable": 40000,
        "commission_percent": 15, "commission_amount": 6000, "reimbursements": 1000, "invoice_amount": 38000,
    }


# ═══ M. the payment follow-up is production-side only ══════════════════════

async def test_payment_follow_up_uses_the_production_numbers(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, "FU", rate=45000, quote=60000, ot=5000, prod_ot=7000, reimb=2000, prod_reimb=3000)
    r = await client.post(f"/api/projects/{pid}/production-desk/tranches", json={"name": "Advance", "amount": 20000, "payment_status": "received"}, headers=headers)
    assert r.status_code == 200, r.text
    res = await db.clients.insert_one({"name": f"{TAG} Producer", "phone_number": "+919900000077", "created_at": _now(), "last_contacted_date": _now(), "stage": "lead", "tags": []})
    cid = str(res.inserted_id); _clients.append(cid)
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": cid}, headers=headers)
    r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json(); text = body["message"]
    assert "Production amount: ₹60,000" in text
    assert "Overtime: ₹7,000" in text and "Reimbursements: ₹3,000" in text
    assert "Total due: ₹70,000" in text and "Received: ₹20,000" in text
    assert "Outstanding: ₹50,000" in text                               # 60,000 + 7,000 + 3,000 − 20,000
    assert body["breakdown"]["outstanding"] == 50000
    for forbidden in ("45,000", "Overtime: ₹5,000", "Reimbursements: ₹2,000", "spread", "commission"):
        assert forbidden.lower() not in text.lower(), forbidden
    s = (await _desk(client, headers, pid))["summary"]
    assert s["client_outstanding_total"] == 50000 == body["breakdown"]["outstanding"]


# ═══ N. the CRM contact picked is the one the message is prepared for ═════

async def test_follow_up_is_prepared_for_the_contact_that_was_chosen(client, headers):
    pid = await _project()
    await _locked(client, headers, pid, "Pick", rate=40000, quote=50000)
    ids = []
    for nm, ph in (("Producer", "+919900000081"), ("Accounts", "+919900000082")):
        res = await db.clients.insert_one({"name": f"{TAG} {nm}", "phone_number": ph, "company_name": f"{TAG} Films", "created_at": _now(), "last_contacted_date": _now(), "stage": "lead", "tags": []})
        ids.append(str(res.inserted_id)); _clients.append(ids[-1])
    await client.patch(f"/api/projects/{pid}/production-desk", json={"production_contact_client_id": ids[0]}, headers=headers)
    cands = (await client.get(f"/api/projects/{pid}/production-desk/payment-followup-contacts", headers=headers)).json()["contacts"]
    assert {c["client_id"] for c in cands} == set(ids)                 # a project can have several production contacts
    for want, phone in zip(ids, ("+919900000081", "+919900000082")):
        r = await client.get(f"/api/projects/{pid}/production-desk/payment-followup-message", params={"contact_id": want}, headers=headers)
        assert r.status_code == 200 and r.json()["contact_id"] == want and r.json()["phone"] == phone
        assert f"Hi {TAG}" in r.json()["message"]


# ═══ Q. IST boundaries: 00:00, 00:30 and 23:30 ═════════════════════════════

@pytest.mark.parametrize("utc_hm, ist_date", [
    ((18, 29), "2026-10-07"),      # 23:59 IST on the 7th
    ((18, 0), "2026-10-07"),       # 23:30 IST
    ((18, 30), "2026-10-08"),      # 00:00 IST — the new day starts exactly here
    ((19, 0), "2026-10-08"),       # 00:30 IST
])
def test_today_flips_exactly_at_midnight_ist(monkeypatch, utc_hm, ist_date):
    fixed = datetime(2026, 10, 7, utc_hm[0], utc_hm[1], tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(pd, "ist_now", lambda: fixed)
    assert pd.ist_today().isoformat() == ist_date
    start, end = pd.ist_day_bounds_utc()
    assert start.startswith(ist_date) and end[:10] > ist_date


# ═══ P. maps: a free-text location is searchable; a selected place keeps its exact URL ═════

async def test_a_selected_place_keeps_its_exact_address_place_id_coordinates_and_url(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, "Maps", rate=40000, quote=50000)
    day = {"date": "2026-10-10", "location": "Mehboob Studio", "location_address": "Mehboob Studio, Hill Rd, Bandra West, Mumbai 400050",
           "location_place_id": "ChIJexactplace01", "location_lat": 19.0522, "location_lng": 72.8277}
    r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json=day, headers=headers)
    d = _find_talent(r.json(), tid)["shoot_days"][0]
    assert d["location_address"] == day["location_address"] and d["location_place_id"] == "ChIJexactplace01"
    assert (d["location_lat"], d["location_lng"]) == (19.0522, 72.8277)
    assert d["location_map_url"] == "https://www.google.com/maps/search/?api=1&query=19.0522,72.8277&query_place_id=ChIJexactplace01"
    # persisted: a fresh read returns the same
    d2 = _find_talent(await _desk(client, headers, pid), tid)["shoot_days"][0]
    assert d2["location_map_url"] == d["location_map_url"] and d2["location_address"] == day["location_address"]
