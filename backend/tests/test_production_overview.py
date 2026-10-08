"""Global Production Desk — GET /api/production/overview.

The overview must be a LENS over the existing per-project desk, never a second implementation:
every figure here is asserted equal to what GET /projects/{pid}/production-desk returns for the same
project. Real FastAPI app + real local dev Mongo (same harness as test_production_desk_financials.py);
throwaway `zzz-test-ov-*` rows are removed afterwards; nothing is ever sent.
"""
import os

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from core import _now, db
from real_db_isolation import install_real_db
from routers import production_desk as pd
from server import app

_aio = pytest.mark.asyncio(loop_scope="module")
pytestmark = _aio

TAG = "ZZZ_OV"
_projects: list = []
_talents: list = []
_clients: list = []


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _real_db_for_this_module():
    real_db, restore = install_real_db(_MONGO_URL)
    yield
    try:
        from bson import ObjectId
        for coll in ("casting_pipeline", "project_reimbursements", "project_payment_tranches", "project_crew", "project_kickbacks", "workflow_tasks"):
            await real_db[coll].delete_many({"project_id": {"$in": _projects}})
        await real_db.projects.delete_many({"id": {"$in": _projects}})
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


# ── seed ───────────────────────────────────────────────────────────────────

async def _project(name, *, status="ongoing", locked=True, **extra):
    pid = f"zzz-test-ov-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": f"{TAG} {name}", "slug": pid, "status": status, "commission_percent": "15%",
        "materials": [], "created_at": _now(), "updated_at": _now(), **extra,
    })
    _projects.append(pid)
    return pid


async def _talent(pid, name, *, stage="locked", rate=None, quote=None, commission=None, client=None, headers=None):
    tid = f"zzz-test-ov-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": f"{TAG} {name}", "email": f"{tid}@example.com", "phone": "+911234500000", "tags": [], "media": []})
    _talents.append(tid)
    await db.casting_pipeline.insert_one({
        "id": f"zzz-test-ov-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid, "stage": stage,
        "created_at": _now(), "updated_at": _now(),
    })
    body = {}
    if rate is not None:
        body["budget_total"] = rate
    if quote is not None:
        body["production_quote"] = quote
    if commission is not None:
        body["commission_percent"] = commission
    if body and client is not None:
        r = await client.patch(f"/api/projects/{pid}/production-desk/talents/{tid}", json=body, headers=headers)
        assert r.status_code == 200, r.text
    return tid


async def _day(client, headers, pid, tid, date, **kw):
    r = await client.post(f"/api/projects/{pid}/production-desk/talents/{tid}/shoot-days", json={"date": date, **kw}, headers=headers)
    assert r.status_code == 200, r.text


def _iso(days):
    return (pd.ist_today() + timedelta(days=days)).isoformat()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(client, headers):
    """Alpha: shooting today + in 3 days, two talents with different rate/quote, a reimbursement, a client (company) contact.
       Beta: legacy (no quotes), client paid, shoot already past.   Gamma: complete (hidden by default).
       Delta: talent NOT locked (never listed).   Epsilon: soft-deleted (never listed)."""
    from bson import ObjectId
    w = {}
    # client contact with a company
    res = await db.clients.insert_one({"name": f"{TAG} Priya", "phone_number": "+919200000001", "company_name": f"{TAG} Films", "created_at": _now(), "last_contacted_date": _now(), "stage": "lead", "tags": []})
    cid = str(res.inserted_id); _clients.append(cid)

    w["alpha"] = await _project("Alpha", pd_production_contact_client_id=cid, pd_payment_terms="50/50")
    a1 = await _talent(w["alpha"], "A1", rate=50000, quote=60000, commission=15, client=client, headers=headers)
    a2 = await _talent(w["alpha"], "A2", rate=60000, quote=60000, commission=20, client=client, headers=headers)
    await _day(client, headers, w["alpha"], a1, _iso(0), reporting_time="08:00", call_time="09:00", location="Mehboob Studio", location_address="Bandra West, Mumbai", location_place_id="ChIJabcdefghij", location_lat=19.05, location_lng=72.83)
    await _day(client, headers, w["alpha"], a2, _iso(3), reporting_time="10:00", call_time="11:00", location="Film City")
    await client.post(f"/api/projects/{w['alpha']}/production-desk/reimbursements", data={"talent_id": a2, "expense_type": "Travel", "amount": "5000", "date": _iso(0)}, headers=headers)
    w["a1"], w["a2"] = a1, a2

    w["beta"] = await _project("Beta", production_house=f"{TAG} Studios", pd_production_budget_total=85000, pd_payment_in_received=True, pd_invoice_raised=True, pd_invoice_sent=True, pd_confirmation_mail_received=True, pd_gst_component_received=True)
    b1 = await _talent(w["beta"], "B1", rate=40000, client=client, headers=headers)
    await db.casting_pipeline.update_one({"project_id": w["beta"], "talent_id": b1}, {"$set": {"pd_payment_status": "cleared"}})
    await _day(client, headers, w["beta"], b1, _iso(-10), call_time="09:00", location="Old studio")

    w["gamma"] = await _project("Gamma", status="complete")
    await _talent(w["gamma"], "G1", rate=10000, quote=12000, client=client, headers=headers)

    w["delta"] = await _project("Delta")
    await _talent(w["delta"], "D1", stage="approved", rate=10000)

    w["eps"] = await _project("Epsilon", lifecycle_state="deleted")
    await _talent(w["eps"], "E1", rate=10000, quote=12000, client=client, headers=headers)
    return w


async def _overview(http, headers, **params):
    r = await http.get("/api/production/overview", params={"q": TAG, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _names(body):
    return [it["brand_name"].replace(f"{TAG} ", "") for it in body["items"]]


async def _desk(client, headers, pid):
    r = await client.get(f"/api/projects/{pid}/production-desk", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ═══ what is listed ═══════════════════════════════════════════════════════

async def test_only_projects_with_a_locked_talent_are_listed_and_completed_ones_are_hidden_by_default(client, headers, world):
    body = await _overview(client, headers)
    names = _names(body)
    assert set(names) == {"Alpha", "Beta"}                                  # Gamma complete, Delta not locked, Epsilon deleted
    assert set(_names(await _overview(client, headers, status="all"))) == {"Alpha", "Beta", "Gamma"}
    assert _names(await _overview(client, headers, status="complete")) == ["Gamma"]
    assert _names(await _overview(client, headers, status="ongoing")).count("Alpha") == 1
    assert _names(await _overview(client, headers, status="hold")) == []
    for bad in ("Delta", "Epsilon"):
        assert bad not in _names(await _overview(client, headers, status="all"))


async def test_unauthenticated_access_is_refused(client, world):
    assert (await client.get("/api/production/overview")).status_code == 401


# ═══ the numbers ARE the project desk's numbers ══════════════════════════

_PARITY_KEYS = [
    "production_basis", "production_billable_total", "talent_agreed_total", "talent_payable_total",
    "commission_gross", "spread_total", "talentgram_earnings_total", "client_received_total",
    "client_outstanding_total", "talent_paid_total", "talent_pending_total", "payments_cleared", "payments_total",
    "production_overtime_total", "production_reimbursements_total",
]


async def test_every_figure_equals_the_project_page(client, headers, world):
    body = await _overview(client, headers, status="all")
    assert len(body["items"]) == 3
    for row in body["items"]:
        desk = await _desk(client, headers, row["project_id"])
        s = desk["summary"]
        assert row["locked_count"] == len(desk["locked_talents"]) == s["locked_count"]
        assert row["money"]["production_total"] == s["production_billable_total"]
        assert row["money"]["talent_agreed_total"] == s["talent_agreed_total"]
        assert row["money"]["talent_payable_total"] == s["talent_payable_total"]
        assert row["money"]["commission"] == s["commission_gross"]
        assert row["money"]["spread"] == s["spread_total"]
        assert row["money"]["earnings"] == s["talentgram_earnings_total"]
        assert row["money"]["overtime"] == s["production_overtime_total"]
        assert row["money"]["reimbursements"] == s["production_reimbursements_total"]
        assert row["money"]["production_basis"] == s["production_basis"]
        assert row["client_payment"]["received"] == s["client_received_total"]
        assert row["client_payment"]["outstanding"] == s["client_outstanding_total"]
        assert row["talent_payment"] == {"cleared": s["payments_cleared"], "total": s["payments_total"], "paid": s["talent_paid_total"], "pending": s["talent_pending_total"]}
        assert row["attention"] == desk["needs_attention"]                  # the ONE needs-attention definition
        assert row["attention_count"] == len(desk["needs_attention"])
        assert (row["money"]["production_quote"] is None) == (s["production_billable_total"] is None)


async def test_the_two_rate_model_shows_up_in_the_rows(client, headers, world):
    alpha = next(r for r in (await _overview(client, headers))["items"] if r["brand_name"].endswith("Alpha"))
    m = alpha["money"]
    # A1 50k/60k @15%, A2 60k/60k @20%  ->  commission 7,500 + 12,000 ; spread 10,000 ; earnings 29,500
    assert (m["talent_agreed_total"], m["commission"], m["spread"], m["earnings"]) == (110000, 19500, 10000, 29500)
    assert m["production_quote"] == 120000 and m["reimbursements"] == 5000 and m["production_total"] == 125000
    assert alpha["client"] == {"label": f"{TAG} Films", "contact_name": f"{TAG} Priya"}   # client = the saved contact's company
    assert alpha["client_payment"]["state"] == "pending" and alpha["client_payment"]["outstanding"] == 125000


async def test_legacy_project_budget_is_used_and_flagged_not_invented(client, headers, world):
    beta = next(r for r in (await _overview(client, headers))["items"] if r["brand_name"].endswith("Beta"))
    assert beta["money"]["production_basis"] == "project_budget" and beta["money"]["production_total"] == 85000
    assert beta["money"]["spread"] == 0
    assert beta["client_payment"]["state"] == "received" and beta["client"]["label"] == f"{TAG} Studios"   # falls back to production_house
    assert "Production quote missing for 1 talent" in beta["attention"]


# ═══ ordering, filters, search, pagination ═══════════════════════════════

async def test_default_order_is_operational_urgency(client, headers, world):
    body = await _overview(client, headers)
    assert _names(body)[0] == "Alpha"                                       # shooting today beats everything
    assert body["items"][0]["shoot"]["state"] == "today"
    assert _names(await _overview(client, headers, sort="name")) == ["Alpha", "Beta"]
    assert _names(await _overview(client, headers, sort="earnings"))[0] == "Alpha"
    assert _names(await _overview(client, headers, sort="outstanding"))[0] == "Alpha"
    assert _names(await _overview(client, headers, sort="shoot"))[0] == "Alpha"        # Beta's only shoot is in the past


async def test_filters(client, headers, world):
    assert _names(await _overview(client, headers, shoot_state="today")) == ["Alpha"]
    assert _names(await _overview(client, headers, shoot_state="completed")) == ["Beta"]
    assert _names(await _overview(client, headers, shoot_state="not_scheduled")) == []
    assert _names(await _overview(client, headers, shoot_date="today")) == ["Alpha"]
    assert _names(await _overview(client, headers, shoot_date="upcoming")) == ["Alpha"]
    assert _names(await _overview(client, headers, shoot_date="past")) == ["Beta"]
    assert _names(await _overview(client, headers, shoot_date="none")) == []
    assert _names(await _overview(client, headers, payment="client_pending")) == ["Alpha"]
    assert _names(await _overview(client, headers, payment="client_received")) == ["Beta"]
    assert _names(await _overview(client, headers, payment="client_partial")) == []
    assert _names(await _overview(client, headers, payment="talent_pending")) == ["Alpha"]
    assert _names(await _overview(client, headers, payment="talent_done")) == ["Beta"]
    assert _names(await _overview(client, headers, attention="needs")) == ["Alpha", "Beta"]
    assert _names(await _overview(client, headers, attention="clear")) == []
    assert _names(await _overview(client, headers, client=f"{TAG} films")) == ["Alpha"]
    assert _names(await _overview(client, headers, client=f"{TAG} STUDIOS")) == ["Beta"]
    assert _names(await _overview(client, headers, stage="not_started")) == ["Alpha", "Beta"]
    assert _names(await _overview(client, headers, stage="confirmed")) == []


async def test_search_matches_project_and_client_names(client, headers, world):
    r = await client.get("/api/production/overview", params={"q": f"{TAG} Alp"}, headers=headers)
    assert [i["brand_name"] for i in r.json()["items"]] == [f"{TAG} Alpha"]
    r = await client.get("/api/production/overview", params={"q": f"{TAG} Studios"}, headers=headers)
    assert [i["brand_name"] for i in r.json()["items"]] == [f"{TAG} Beta"]          # matched on the client label
    r = await client.get("/api/production/overview", params={"q": f"{TAG} priya"}, headers=headers)
    assert [i["brand_name"] for i in r.json()["items"]] == [f"{TAG} Alpha"]         # and on the contact's name


async def test_pagination_is_server_side(client, headers, world):
    p0 = await _overview(client, headers, size=1, page=0)
    p1 = await _overview(client, headers, size=1, page=1)
    p2 = await _overview(client, headers, size=1, page=2)
    assert [len(p["items"]) for p in (p0, p1, p2)] == [1, 1, 0]
    assert p0["total"] == p1["total"] == 2 and p0["pages"] == 2 and p0["has_more"] is True and p1["has_more"] is False
    assert _names(p0) + _names(p1) == ["Alpha", "Beta"]
    assert p0["summary"]["projects"] == 2                                          # the header covers the whole filtered set, not the page


async def test_invalid_parameters_are_rejected(client, headers, world):
    for params in ({"status": "cancelled"}, {"shoot_state": "x"}, {"shoot_date": "x"}, {"payment": "x"}, {"attention": "x"}, {"stage": "x"}, {"sort": "x"}, {"size": 0}, {"size": 101}, {"page": -1}):
        r = await client.get("/api/production/overview", params=params, headers=headers)
        assert r.status_code in (400, 422), params


# ═══ header summary, schedule, laziness, facets ══════════════════════════

async def test_header_summary_is_the_sum_of_the_filtered_rows(client, headers, world):
    body = await _overview(client, headers)
    rows = body["items"]
    s = body["summary"]
    assert s["projects"] == len(rows) == 2
    assert s["locked_talents"] == sum(r["locked_count"] for r in rows) == 3
    assert s["shooting_today"] == 1
    assert s["upcoming_shoots"] == 1                                         # Alpha's talent A2, three days from now
    assert s["client_outstanding"] == round(sum(r["client_payment"]["outstanding"] or 0 for r in rows), 2)
    assert s["talent_pending"] == round(sum(r["talent_payment"]["pending"] for r in rows), 2)
    assert s["earnings"] == round(sum(r["money"]["earnings"] for r in rows), 2)
    assert s["needs_attention"] == sum(1 for r in rows if r["attention_count"])
    # filtering re-scopes the header with the rows
    only_beta = await _overview(client, headers, q=f"{TAG} Beta")
    assert only_beta["summary"]["projects"] == 1 and only_beta["summary"]["shooting_today"] == 0


async def test_upcoming_shoots_come_from_the_talent_schedule(client, headers, world):
    body = await _overview(client, headers)
    ups = body["upcoming_shoots"]
    assert [(u["date"], u["location"]) for u in ups] == [(_iso(0), "Mehboob Studio"), (_iso(3), "Film City")]   # Beta's past day is not upcoming
    first = ups[0]
    assert first["brand_name"] == f"{TAG} Alpha" and first["talent_name"] == f"{TAG} A1"
    assert (first["reporting_time"], first["call_time"]) == ("08:00", "09:00")
    assert first["location_address"] == "Bandra West, Mumbai"
    assert first["map_url"] and first["map_url"].startswith("https://www.google.com/maps/")
    assert first["project_id"] == world["alpha"] and first["talent_id"] == world["a1"]


async def test_rows_are_lightweight_summaries_only(client, headers, world):
    row = (await _overview(client, headers))["items"][0]
    assert set(row) == {"project_id", "brand_name", "status", "stage", "client", "locked_count", "shoot", "money", "client_payment", "talent_payment", "attention", "attention_count"}
    blob = str(row)
    for heavy in ("locked_talents", "media", "tasks", "documents", "kickbacks", "image_url"):
        assert heavy not in blob


async def test_client_facet_lists_every_known_client_for_the_dropdown(client, headers, world):
    body = await _overview(client, headers, client=f"{TAG} Studios")
    assert f"{TAG} Films" in body["facets"]["clients"] and f"{TAG} Studios" in body["facets"]["clients"]   # not narrowed by the client filter itself


async def test_today_follows_the_indian_calendar_date(client, headers, world, monkeypatch):
    from zoneinfo import ZoneInfo
    # 00:30 IST on the date of Alpha's second shoot: that talent's day is TODAY in India (still yesterday in UTC)
    target_iso = _iso(3)                                                         # computed BEFORE the clock is faked
    target = datetime.fromisoformat(target_iso)
    monkeypatch.setattr(pd, "ist_now", lambda: datetime(target.year, target.month, target.day, 0, 30, tzinfo=ZoneInfo("Asia/Kolkata")))
    body = await _overview(client, headers)
    alpha = next(r for r in body["items"] if r["brand_name"].endswith("Alpha"))
    assert alpha["shoot"]["state"] == "today"
    assert [u["date"] for u in body["upcoming_shoots"]] == [target_iso]         # the earlier day is now past
