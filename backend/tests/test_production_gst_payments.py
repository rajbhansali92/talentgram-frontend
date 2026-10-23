"""Production Desk — GST, partial payments (instalments) and payment ageing.

Two layers:
  1. production_money (pure): GST breakdown, rounding, instalment split, due dates, ageing, status.
  2. The real FastAPI app on the local dev Mongo (same harness as test_production_desk_financials.py,
     whose fixtures/helpers are reused): invoices (tranches) with GST + instalments, talent GST invoices
     + instalments, idempotency, concurrency, corrections, project isolation, legacy records untouched,
     and the summary equalling the stored records after a fresh reload.
Throwaway `ZZZ_TEST_PDFIN_*` rows are cleaned by the reused fixture. Nothing is sent anywhere.
"""
import asyncio
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from datetime import date, timedelta

import pytest

import production_money as pm
from core import db
from routers import production_desk as pd
import pytest_asyncio

from test_production_desk_financials import (  # noqa: F401  (fixtures + helpers, reused unchanged)
    _desk, _locked, _project, _real_db_for_this_module, client,
)
from test_production_desk_v2 import _find_talent

_LOGIN_ROWS = {"endpoint": "login", "ip": "127.0.0.1", "email": "admin@example.com"}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def headers(client):
    """Same admin login as the sibling modules, but leaves the login limiter (5 per window, shared by every
    module that logs in) exactly as it found it — a module that logs in must not push the NEXT module's
    login over the limit when the whole suite runs back to back."""
    await db.rate_limits.delete_many(_LOGIN_ROWS)
    r = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "changeme123"})
    assert r.status_code == 200, r.text
    await db.rate_limits.delete_many(_LOGIN_ROWS)
    return {"Authorization": f"Bearer {r.json()['token']}"}


pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),   # shared module loop for the API half; the pure half is plain sync
    pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio' but it is not an async function"),
]
TODAY = date(2026, 10, 23)


# ═══════════════════════════ 1. pure rules ═══════════════════════════════════════════════════════════
def test_no_gst_recorded_is_unresolved_not_zero():
    b = pm.gst_breakdown(50000, None, None)
    assert b["state"] == "not_recorded" and b["total"] == 50000 and b["base"] is None and b["gst"] is None


def test_explicit_no_gst():
    b = pm.gst_breakdown(50000, "none", None)
    assert (b["state"], b["base"], b["gst"], b["total"]) == ("none", 50000, 0.0, 50000)


def test_gst_added_on_top_of_the_base():
    b = pm.gst_breakdown(100000, "exclusive", 18)
    assert (b["base"], b["gst"], b["total"], b["rate"]) == (100000, 18000, 118000, 18)


def test_recorded_total_that_already_includes_gst_is_not_taxed_twice():
    b = pm.gst_breakdown(118000, "inclusive", 18)
    assert (b["base"], b["gst"], b["total"]) == (100000, 18000, 118000)
    assert pm.gst_breakdown(118000, "inclusive", 18)["total"] == 118000     # still the recorded total


def test_taxable_treatment_without_a_rate_derives_nothing():
    b = pm.gst_breakdown(100000, "exclusive", None)
    assert b["state"] == "incomplete" and b["gst"] is None and b["base"] is None
    assert pm.gst_breakdown(100000, "exclusive", 150)["state"] == "incomplete"   # not a plausible percentage


@pytest.mark.parametrize("amount", [0.01, 1, 99.99, 999.99, 1234.56, 77777.77, 100000.5, 5000000.01])
@pytest.mark.parametrize("treatment,rate", [("exclusive", 18), ("exclusive", 12.5), ("exclusive", 5), ("inclusive", 18), ("inclusive", 12.5), ("inclusive", 5)])
def test_base_plus_gst_always_reconciles_to_the_total_to_the_paisa(amount, treatment, rate):
    b = pm.gst_breakdown(amount, treatment, rate)
    assert round(b["base"] + b["gst"], 2) == b["total"]
    assert b["total"] == (amount if treatment == "inclusive" else round(amount + b["gst"], 2))


def test_rounding_is_half_up_once():
    assert pm.money(0.125) == 0.13 and pm.money(2.675) == 2.68 and pm.money(1.005) == 1.01
    assert pm.gst_breakdown(999.99, "exclusive", 12.5)["gst"] == 125.0       # 124.99875 -> 125.00


@pytest.mark.parametrize("terms,days", [("Net 30", 30), ("net30", 30), ("30 days", 30), ("45 calendar days", 45), ("Payment within 15 days of invoice", 15),
                                        ("On receipt", None), ("50% advance, balance on delivery", None), ("", None), (None, None), ("Net 0", None), ("9999 days", None)])
def test_payment_terms_only_yield_a_number_of_days_when_one_is_written(terms, days):
    assert pm.terms_days(terms) == days


def test_due_date_precedence_is_labelled_and_never_invented():
    assert pm.due_info("2026-11-01", "2026-10-01", "Net 30", "2026-12-25") == {"due_date": "2026-11-01", "due_basis": "due_date"}
    assert pm.due_info(None, "2026-10-01", "Net 30", "2026-12-25") == {"due_date": "2026-10-31", "due_basis": "payment_terms"}
    assert pm.due_info(None, None, "Net 30", "2026-12-25") == {"due_date": "2026-12-25", "due_basis": "expected_payment_date"}
    assert pm.due_info(None, "2026-10-01", "On receipt", None) == {"due_date": None, "due_basis": None}     # invoice date alone is NOT a due date
    assert pm.due_info("garbage", None, None, None) == {"due_date": None, "due_basis": None}


def test_ageing_needs_a_due_date_and_an_open_balance():
    assert pm.ageing("2026-10-13", 500, TODAY)["days_overdue"] == 10 and pm.ageing("2026-10-13", 500, TODAY)["overdue"]
    assert not pm.ageing("2026-10-13", 0, TODAY)["overdue"]                    # nothing outstanding -> never overdue
    assert not pm.ageing("2026-10-23", 500, TODAY)["overdue"]                  # due today is not overdue yet
    assert pm.ageing(None, 500, TODAY, "2026-10-01") == {"overdue": False, "days_overdue": 0, "days_since_invoice": 22}   # labelled fallback only
    assert pm.ageing(None, 500, TODAY) == {"overdue": False, "days_overdue": 0, "days_since_invoice": None}


def _inv(payments=None, amount=100000, treatment="exclusive", rate=18, due="2026-10-13", legacy=False):
    return pm.receivable_view(amount=amount, treatment=treatment, rate=rate, payments=payments, legacy_received=legacy,
                              due_date=due, invoice_date="2026-10-01", terms=None, expected_date=None, today=TODAY)


def test_half_now_half_later_is_partially_paid_then_paid_with_gst_split():
    half = [{"amount": 59000, "date": "2026-10-05"}]
    v = _inv(half)
    assert (v["invoice_total"], v["received"], v["outstanding"], v["state"]) == (118000, 59000, 59000, "partially_paid")
    assert v["received_gst"] == 9000 and v["outstanding_gst"] == 9000          # pro-rata: half the GST
    both = half + [{"amount": 59000, "date": "2026-10-20"}]
    v2 = _inv(both)
    assert (v2["state"], v2["outstanding"], v2["received_gst"], v2["outstanding_gst"]) == ("paid", 0, 18000, 0)
    assert (v2["first_payment_date"], v2["last_payment_date"], v2["instalments"]) == ("2026-10-05", "2026-10-20", 2)


def test_explicit_gst_portion_overrides_pro_rata_and_full_payment_leaves_no_rounding_residue():
    v = _inv([{"amount": 100000, "gst_amount": 0}, {"amount": 18000, "gst_amount": 18000}], due="2027-01-01")
    assert v["received_gst"] == 18000 and v["outstanding"] == 0                 # base first, GST component later
    v = _inv([{"amount": 100000, "gst_amount": 0}], due="2027-01-01")
    assert (v["received_gst"], v["outstanding_gst"], v["outstanding"]) == (0.0, 18000, 18000)
    thirds = [{"amount": 39333.33}, {"amount": 39333.33}, {"amount": 39333.34}]
    assert _inv(thirds)["received_gst"] == 18000                                 # paid in full -> exactly the invoice GST


def test_one_instalment_never_marks_a_larger_invoice_paid_and_voided_ones_do_not_count():
    assert _inv([{"amount": 10}])["state"] == "partially_paid"
    assert _inv([{"amount": 118000, "status": "voided"}])["received"] == 0 and _inv([{"amount": 118000, "status": "voided"}])["state"] == "unpaid"


def test_partially_paid_past_due_is_overdue_with_days():
    v = _inv([{"amount": 50000, "date": "2026-10-05"}])
    assert v["state"] == "partially_paid" and v["overdue"] and v["days_overdue"] == 10 and v["status"] == "Overdue"


def test_legacy_received_flag_means_the_recorded_amount_and_a_later_gst_is_not_assumed_received():
    plain = _inv(None, treatment=None, rate=None, legacy=True)
    assert (plain["received"], plain["outstanding"], plain["state"]) == (100000, 0, "paid")      # exactly the old meaning
    later = _inv(None, legacy=True)                                                                # GST 18% recorded afterwards, on top
    assert (later["invoice_total"], later["received"], later["outstanding"], later["outstanding_gst"]) == (118000, 100000, 18000, 18000)
    inc = _inv(None, amount=118000, treatment="inclusive", legacy=True)
    assert (inc["received"], inc["received_gst"], inc["outstanding"]) == (118000, 18000, 0)


def test_missing_dates_show_unavailable_instead_of_inventing_them():
    v = pm.receivable_view(amount=1000, treatment=None, rate=None, payments=None, legacy_received=False, due_date=None, invoice_date=None,
                           terms=None, expected_date=None, today=TODAY)
    assert v["due_date"] is None and v["due_basis"] is None and not v["overdue"] and v["days_since_invoice"] is None
    assert v["first_payment_date"] is None and v["state"] == "unpaid"


def test_overpayment_is_refused_not_hidden():
    assert pm.validate_new_payment(1000, 900, 100, None) is None
    assert "exceed" in pm.validate_new_payment(1000, 900, 100.01, None)
    assert pm.validate_new_payment(1000, 0, 0, None) and pm.validate_new_payment(None, 0, 10, None)
    assert pm.validate_new_payment(1000, 0, 100, 150) and pm.validate_new_payment(1000, 0, 100, -1)


def _ob(**kw):
    base = dict(net_payable=51000, treatment=None, rate=None, gst_invoice_number=None, gst_invoice_date=None, due_date=None,
                payments=None, legacy_cleared=False, today=TODAY)
    return pm.talent_obligation_view(**{**base, **kw})


def test_talent_with_a_valid_gst_invoice_adds_gst_to_what_is_payable():
    o = _ob(treatment="exclusive", rate=18, gst_invoice_number="TX-1", gst_invoice_date="2026-10-02")
    assert (o["gst_state"], o["base"], o["gst"], o["total_payable"], o["balance"]) == ("valid", 51000, 9180, 60180, 60180)
    assert o["gst_invoice_number"] == "TX-1" and o["gst_invoice_date"] == "2026-10-02"


def test_talent_gst_invoice_inclusive_is_not_added_twice():
    o = _ob(treatment="inclusive", rate=18, gst_invoice_number="TX-1")
    assert (o["total_payable"], o["gst"], o["base"]) == (51000, 7779.66, 43220.34)


def test_talent_without_a_gst_invoice_gets_no_gst_and_says_why():
    none = _ob()
    assert (none["gst_state"], none["gst"], none["total_payable"]) == ("no_gst", 0.0, 51000)
    missing = _ob(treatment="exclusive", rate=18)                     # taxable, but no invoice reference recorded
    assert (missing["gst_state"], missing["gst"], missing["total_payable"]) == ("invoice_missing", 0.0, 51000)
    norate = _ob(treatment="exclusive", gst_invoice_number="TX-9")
    assert (norate["gst_state"], norate["total_payable"]) == ("incomplete", 51000)


def test_talent_instalments_accumulate_and_the_balance_is_what_remains():
    o = _ob(treatment="exclusive", rate=18, gst_invoice_number="TX-1", payments=[{"amount": 20000, "date": "2026-10-03"}, {"amount": 20000, "date": "2026-10-10"}])
    assert (o["paid"], o["balance"], o["state"], o["instalments"]) == (40000, 20180, "partially_paid", 2)
    assert (o["first_payment_date"], o["last_payment_date"]) == ("2026-10-03", "2026-10-10")


def test_legacy_cleared_talent_counts_the_net_payable_and_a_later_gst_shows_as_the_balance():
    assert (_ob(legacy_cleared=True)["paid"], _ob(legacy_cleared=True)["state"]) == (51000, "paid")
    o = _ob(legacy_cleared=True, treatment="exclusive", rate=18, gst_invoice_number="TX-1")
    assert (o["paid"], o["balance"]) == (51000, 9180)
    assert _ob(net_payable=None) is None                              # no rate entered yet: nothing is invented


# ═══════════════════════════ 2. API — client invoices ═══════════════════════════════════════════════
def _url(pid, suffix=""):
    return f"/api/projects/{pid}/production-desk{suffix}"


async def _tranche(client, headers, pid, **body):
    body = {"name": "Invoice 1", "amount": 100000, **body}
    r = await client.post(_url(pid, "/tranches"), json=body, headers=headers)
    assert r.status_code == 200, r.text
    return (await _desk(client, headers, pid))["tranches"][-1]


async def _pay(client, headers, pid, tid, amount, expect=200, **extra):
    r = await client.post(_url(pid, f"/tranches/{tid}/payments"), json={"amount": amount, "request_id": uuid.uuid4().hex, **extra}, headers=headers)
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


async def test_invoice_without_gst_keeps_its_amount_and_reports_gst_as_not_recorded(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=50000)
    v = t["receivable"]
    assert (v["invoice_total"], v["tax"]["state"], v["tax"]["gst"], v["state"], v["outstanding"]) == (50000, "not_recorded", None, "unpaid", 50000)
    cr = (await _desk(client, headers, pid))["summary"]["client_receivables"]
    assert (cr["invoiced_total"], cr["tax_unresolved_count"], cr["invoiced_gst_total"], cr["outstanding_total"]) == (50000, 1, 0, 50000)


async def test_invoice_with_gst_added_and_with_gst_included_both_reconcile(client, headers):
    pid = await _project()
    ex = await _tranche(client, headers, pid, name="Exclusive", amount=100000, gst_treatment="exclusive", gst_rate=18)
    inc = await _tranche(client, headers, pid, name="Inclusive", amount=118000, gst_treatment="inclusive", gst_rate=18)
    assert (ex["receivable"]["tax"]["base"], ex["receivable"]["tax"]["gst"], ex["receivable"]["invoice_total"]) == (100000, 18000, 118000)
    assert (inc["receivable"]["tax"]["base"], inc["receivable"]["tax"]["gst"], inc["receivable"]["invoice_total"]) == (100000, 18000, 118000)
    cr = (await _desk(client, headers, pid))["summary"]["client_receivables"]
    assert (cr["invoiced_base_total"], cr["invoiced_gst_total"], cr["invoiced_total"], cr["gst_recorded_count"]) == (200000, 36000, 236000, 2)
    assert round(cr["invoiced_base_total"] + cr["invoiced_gst_total"], 2) == cr["invoiced_total"]


async def test_taxable_treatment_requires_a_rate_and_never_defaults_one(client, headers):
    pid = await _project()
    r = await client.post(_url(pid, "/tranches"), json={"name": "x", "amount": 1000, "gst_treatment": "exclusive"}, headers=headers)
    assert r.status_code == 400 and "rate" in r.text.lower()
    assert (await client.post(_url(pid, "/tranches"), json={"name": "x", "amount": 1000, "gst_treatment": "bogus"}, headers=headers)).status_code == 400
    assert (await client.post(_url(pid, "/tranches"), json={"name": "x", "amount": 1000, "gst_treatment": "none", "due_date": "13/10/2026"}, headers=headers)).status_code == 400


async def test_fifty_percent_now_and_the_rest_later_accumulates_and_reduces_the_balance(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=100000, gst_treatment="exclusive", gst_rate=18, due_date="2099-01-01")
    d = await _pay(client, headers, pid, t["id"], 59000, date="2026-10-05", reference="UTR-1")
    v = d["tranches"][0]["receivable"]
    assert (v["received"], v["outstanding"], v["state"], v["received_gst"], v["outstanding_gst"]) == (59000, 59000, "partially_paid", 9000, 9000)
    assert d["tranches"][0]["payment_status"] == "pending"                      # one instalment does not make it received
    d = await _pay(client, headers, pid, t["id"], 59000, date="2026-10-20", reference="UTR-2")
    tr = d["tranches"][0]
    assert (tr["receivable"]["state"], tr["receivable"]["outstanding"], tr["receivable"]["received_gst"]) == ("paid", 0, 18000)
    assert (tr["receivable"]["first_payment_date"], tr["receivable"]["last_payment_date"], tr["receivable"]["instalments"]) == ("2026-10-05", "2026-10-20", 2)
    assert tr["payment_status"] == "received" and tr["payment_date"] == "2026-10-20"
    assert [p["reference"] for p in tr["payments"]] == ["UTR-1", "UTR-2"]       # an auditable history, in order
    assert d["summary"]["client_received_total"] == 118000


async def test_partially_paid_invoice_past_its_due_date_is_overdue_and_flagged(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=100000, due_date="2020-01-01")
    d = await _pay(client, headers, pid, t["id"], 40000, date="2019-12-30")
    v = d["tranches"][0]["receivable"]
    assert v["state"] == "partially_paid" and v["overdue"] and v["status"] == "Overdue"
    assert v["days_overdue"] == (pd.ist_today() - date(2020, 1, 1)).days
    cr = d["summary"]["client_receivables"]
    assert (cr["overdue_count"], cr["overdue_total"], cr["max_days_overdue"]) == (1, 60000, v["days_overdue"])
    assert any(a.startswith("Client payment overdue") for a in d["needs_attention"])


async def test_due_date_falls_back_to_payment_terms_then_expected_date_and_is_labelled(client, headers):
    pid = await _project()
    await client.patch(_url(pid), json={"payment_terms": "Net 30"}, headers=headers)
    t = await _tranche(client, headers, pid, amount=1000, invoice_date="2020-01-01")
    v = (await _desk(client, headers, pid))["tranches"][0]["receivable"]
    assert (v["due_date"], v["due_basis"], v["overdue"]) == ("2020-01-31", "payment_terms", True)
    pid2 = await _project()
    t2 = await _tranche(client, headers, pid2, amount=1000, invoice_date="2020-01-01")       # no terms, no due date, no expected date
    v2 = (await _desk(client, headers, pid2))["tranches"][0]["receivable"]
    assert v2["due_date"] is None and v2["due_basis"] is None and not v2["overdue"] and v2["days_since_invoice"] is not None


async def test_overpayment_and_bad_gst_portions_are_rejected(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000)
    await _pay(client, headers, pid, t["id"], 700)
    r = await _pay(client, headers, pid, t["id"], 300.01, expect=400)
    assert "exceed" in r.text
    assert (await _pay(client, headers, pid, t["id"], 100, expect=400, gst_amount=10)).status_code == 400     # no GST on this invoice
    await _pay(client, headers, pid, t["id"], 300)                                                              # exactly the balance is fine
    assert (await _desk(client, headers, pid))["tranches"][0]["receivable"]["state"] == "paid"


async def test_a_repeated_submission_with_the_same_request_id_records_one_payment(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000)
    body = {"amount": 250, "request_id": "dup-request-0001"}
    for _ in range(3):
        assert (await client.post(_url(pid, f"/tranches/{t['id']}/payments"), json=body, headers=headers)).status_code == 200
    assert len((await _desk(client, headers, pid))["tranches"][0]["payments"]) == 1


async def test_simultaneous_double_click_and_competing_payments_never_double_record_or_overpay(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000)
    same = await asyncio.gather(*[client.post(_url(pid, f"/tranches/{t['id']}/payments"), json={"amount": 400, "request_id": "same-click-0001"}, headers=headers) for _ in range(6)])
    assert all(r.status_code == 200 for r in same)
    assert [p["amount"] for p in (await _desk(client, headers, pid))["tranches"][0]["payments"]] == [400]
    # three DIFFERENT payments of 400 against the 600 still due: exactly one fits
    rs = await asyncio.gather(*[client.post(_url(pid, f"/tranches/{t['id']}/payments"), json={"amount": 400, "request_id": f"diff-click-000{i}"}, headers=headers) for i in range(3)])
    assert sorted(r.status_code for r in rs).count(200) == 1
    tr = (await _desk(client, headers, pid))["tranches"][0]
    assert tr["receivable"]["received"] == 800 and tr["receivable"]["overpaid"] == 0


async def test_editing_and_voiding_a_payment_recalculates_everything_without_duplicating(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000)
    d = await _pay(client, headers, pid, t["id"], 400, date="2026-10-01")
    first = d["tranches"][0]["payments"][0]["id"]
    d = await _pay(client, headers, pid, t["id"], 600, date="2026-10-02")
    assert d["tranches"][0]["payment_status"] == "received"
    r = await client.patch(_url(pid, f"/tranches/{t['id']}/payments/{first}"), json={"amount": 300, "reference": "corrected"}, headers=headers)
    assert r.status_code == 200
    tr = r.json()["tranches"][0]
    assert tr["receivable"]["received"] == 900 and tr["receivable"]["state"] == "partially_paid" and tr["payment_status"] == "pending"
    assert len(tr["payments"]) == 2 and tr["payments"][0]["revisions"][0]["previous"]["amount"] == 400      # history of the correction
    assert (await client.patch(_url(pid, f"/tranches/{t['id']}/payments/{first}"), json={"amount": 500.01}, headers=headers)).status_code == 400   # would overpay
    r = await client.delete(_url(pid, f"/tranches/{t['id']}/payments/{first}"), headers=headers)
    tr = r.json()["tranches"][0]
    assert tr["receivable"]["received"] == 600 and len(tr["payments"]) == 2 and tr["payments"][0]["status"] == "voided"   # kept in history, not counted
    assert (await client.delete(_url(pid, f"/tranches/{t['id']}/payments/{first}"), headers=headers)).status_code == 404   # cannot void twice


async def test_status_cannot_be_forced_and_the_total_cannot_drop_below_what_was_received(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000)
    await _pay(client, headers, pid, t["id"], 600)
    assert (await client.patch(_url(pid, f"/tranches/{t['id']}"), json={"payment_status": "received"}, headers=headers)).status_code == 400
    r = await client.patch(_url(pid, f"/tranches/{t['id']}"), json={"amount": 500}, headers=headers)
    assert r.status_code == 400 and "below" in r.text
    r = await client.patch(_url(pid, f"/tranches/{t['id']}"), json={"gst_treatment": "exclusive", "gst_rate": 12.5, "due_date": "2027-01-01"}, headers=headers)
    assert r.status_code == 200
    v = r.json()["tranches"][0]["receivable"]
    assert (v["invoice_total"], v["tax"]["gst"], v["received"], v["outstanding"]) == (1125, 125, 600, 525)


async def test_historical_binary_tranche_and_talent_keep_their_numbers_and_reading_changes_nothing(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)                      # net payable 51,000 at 15%
    legacy = await _tranche(client, headers, pid, amount=40000, payment_status="received", payment_date="2026-09-01")
    r = await client.patch(_url(pid, f"/talents/{tid}"), json={"payment_status": "cleared"}, headers=headers)
    assert r.status_code == 200
    stored_t = await db.project_payment_tranches.find_one({"id": legacy["id"]})
    stored_row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
    d1, d2 = await _desk(client, headers, pid), await _desk(client, headers, pid)
    assert d1["summary"] == d2["summary"]
    s = d1["summary"]
    assert (s["client_received_total"], s["talent_paid_total"], s["talent_pending_total"], s["payments_cleared"]) == (40000, 51000, 0, 1)
    assert d1["tranches"][0]["receivable"]["received"] == 40000 and d1["tranches"][0]["receivable"]["state"] == "paid"
    assert await db.project_payment_tranches.find_one({"id": legacy["id"]}) == stored_t            # GET wrote nothing
    assert await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid}) == stored_row


async def test_a_legacy_received_invoice_takes_a_new_payment_without_inventing_a_date(client, headers):
    pid = await _project()
    t = await _tranche(client, headers, pid, amount=1000, payment_status="received")             # no payment date recorded
    await client.patch(_url(pid, f"/tranches/{t['id']}"), json={"gst_treatment": "exclusive", "gst_rate": 18}, headers=headers)
    v = (await _desk(client, headers, pid))["tranches"][0]["receivable"]
    assert (v["invoice_total"], v["received"], v["outstanding"], v["outstanding_gst"]) == (1180, 1000, 180, 180)    # GST not assumed received
    d = await _pay(client, headers, pid, t["id"], 180, date="2026-10-22")
    tr = d["tranches"][0]
    assert tr["receivable"]["state"] == "paid" and len(tr["payments"]) == 2
    assert tr["payments"][0]["legacy"] and tr["payments"][0]["date"] is None and tr["payments"][0]["amount"] == 1000


# ═══════════════════════════ 3. API — talents ═══════════════════════════════════════════════════════
async def test_talent_gst_invoice_instalments_and_the_remaining_balance(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)
    r = await client.patch(_url(pid, f"/talents/{tid}"), json={"gst_treatment": "exclusive", "gst_rate": 18, "gst_invoice_number": "TX-77",
                                                                "gst_invoice_date": "2026-10-02", "payment_due_date": "2099-01-01"}, headers=headers)
    assert r.status_code == 200
    c = _find_talent(r.json(), tid)
    p = c["payable"]
    assert (c["talent_net_payable"], p["gst"], p["total_payable"], p["gst_state"], p["balance"]) == (51000, 9180, 60180, "valid", 60180)
    assert (p["gst_invoice_number"], p["gst_invoice_date"]) == ("TX-77", "2026-10-02")
    assert c["commission_amount"] == 9000                                              # talent GST never touches commission
    async def pay(amount, **kw):
        rr = await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": amount, "request_id": uuid.uuid4().hex, **kw}, headers=headers)
        assert rr.status_code == 200, rr.text
        return rr.json()
    d = await pay(20000, date="2026-10-03")
    d = await pay(20000, date="2026-10-10")
    c = _find_talent(d, tid)
    assert (c["payable"]["paid"], c["payable"]["balance"], c["payable"]["state"], c["payment_status"]) == (40000, 20180, "partially_paid", "pending")
    assert d["summary"]["talent_paid_total"] == 40000 and d["summary"]["talent_pending_total"] == 20180
    assert d["summary"]["talent_payables"]["gst_total"] == 9180 and d["summary"]["payments_cleared"] == 0
    d = await pay(20180, gst_amount=9180)
    c = _find_talent(d, tid)
    assert (c["payable"]["state"], c["payable"]["balance"], c["payment_status"]) == ("paid", 0, "cleared")
    assert d["summary"]["payments_cleared"] == 1 and d["summary"]["talent_pending_total"] == 0
    assert (await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 1, "request_id": uuid.uuid4().hex}, headers=headers)).status_code == 400   # fully paid


async def test_talent_without_a_gst_invoice_is_paid_the_net_amount_and_the_missing_invoice_is_flagged(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)
    r = await client.patch(_url(pid, f"/talents/{tid}"), json={"gst_treatment": "exclusive", "gst_rate": 18}, headers=headers)   # taxable, no invoice reference
    c = _find_talent(r.json(), tid)
    assert (c["payable"]["gst_state"], c["payable"]["gst"], c["payable"]["total_payable"]) == ("invoice_missing", 0, 51000)
    assert r.json()["summary"]["talent_payables"]["gst_invoice_missing_count"] == 1
    assert (await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 100, "gst_amount": 10}, headers=headers)).status_code == 400
    d = (await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 51000, "request_id": "talent-full-0001"}, headers=headers)).json()
    assert _find_talent(d, tid)["payment_status"] == "cleared"


async def test_multiple_talents_each_keep_their_own_paid_and_pending_amounts(client, headers):
    pid = await _project()
    a = await _locked(client, headers, pid, rate=60000, quote=60000)
    b = await _locked(client, headers, pid, rate=20000, quote=20000)
    await client.post(_url(pid, f"/talents/{a}/payments"), json={"amount": 10000, "request_id": "talent-a-0001"}, headers=headers)
    d = await _desk(client, headers, pid)
    assert (_find_talent(d, a)["payable"]["balance"], _find_talent(d, b)["payable"]["balance"]) == (41000, 17000)
    assert (d["summary"]["talent_paid_total"], d["summary"]["talent_pending_total"]) == (10000, 58000)


async def test_talent_payment_guards_duplicates_status_and_gst_changes(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)
    body = {"amount": 5000, "request_id": "talent-dup-0001"}
    rs = await asyncio.gather(*[client.post(_url(pid, f"/talents/{tid}/payments"), json=body, headers=headers) for _ in range(5)])
    assert all(r.status_code == 200 for r in rs)
    c = _find_talent(await _desk(client, headers, pid), tid)
    assert len(c["payments"]) == 1 and c["payable"]["paid"] == 5000
    assert (await client.patch(_url(pid, f"/talents/{tid}"), json={"payment_status": "cleared"}, headers=headers)).status_code == 400
    pay_id = c["payments"][0]["id"]
    assert (await client.patch(_url(pid, f"/talents/{tid}/payments/{pay_id}"), json={"amount": 99999}, headers=headers)).status_code == 400
    r = await client.patch(_url(pid, f"/talents/{tid}/payments/{pay_id}"), json={"amount": 6000, "reference": "fixed"}, headers=headers)
    assert _find_talent(r.json(), tid)["payable"]["paid"] == 6000
    r = await client.delete(_url(pid, f"/talents/{tid}/payments/{pay_id}"), headers=headers)
    c = _find_talent(r.json(), tid)
    assert c["payable"]["paid"] == 0 and c["payments"][0]["status"] == "voided"
    await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 51000, "request_id": "talent-full-0002"}, headers=headers)
    assert (await client.patch(_url(pid, f"/talents/{tid}"), json={"gst_treatment": "none"}, headers=headers)).status_code == 200      # same total: fine
    legacy_pid = await _project()
    legacy_tid = await _locked(client, headers, legacy_pid, rate=60000, quote=60000)
    await client.patch(_url(legacy_pid, f"/talents/{legacy_tid}"), json={"payment_status": "cleared"}, headers=headers)
    r = await client.post(_url(legacy_pid, f"/talents/{legacy_tid}/payments"), json={"amount": 1, "request_id": "legacy-over-0001"}, headers=headers)
    assert r.status_code == 400                                                                      # the old "cleared" already covered the whole net


async def test_a_talent_marked_cleared_before_then_getting_gst_has_exactly_the_gst_outstanding(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=60000)
    await client.patch(_url(pid, f"/talents/{tid}"), json={"payment_status": "cleared"}, headers=headers)
    r = await client.patch(_url(pid, f"/talents/{tid}"), json={"gst_treatment": "exclusive", "gst_rate": 18, "gst_invoice_number": "TX-1"}, headers=headers)
    c = _find_talent(r.json(), tid)
    assert (c["payable"]["paid"], c["payable"]["balance"], c["payment_status"]) == (51000, 9180, "pending")
    d = (await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 9180, "gst_amount": 9180, "request_id": "gst-top-up-0001"}, headers=headers)).json()
    c = _find_talent(d, tid)
    assert (c["payable"]["paid"], c["payable"]["balance"], c["payment_status"]) == (60180, 0, "cleared")
    assert [p["amount"] for p in c["payments"]] == [51000, 9180] and c["payments"][0]["legacy"] and c["payments"][0]["date"] is None


# ═══════════════════════════ 4. isolation, reload, overview ═════════════════════════════════════════
async def test_two_projects_stay_financially_isolated(client, headers):
    p1, p2 = await _project(), await _project()
    t1 = await _tranche(client, headers, p1, amount=1000, gst_treatment="exclusive", gst_rate=18)
    t2 = await _tranche(client, headers, p2, amount=5000)
    await _pay(client, headers, p1, t1["id"], 500)
    d1, d2 = await _desk(client, headers, p1), await _desk(client, headers, p2)
    assert d1["summary"]["client_receivables"]["received_total"] == 500 and d2["summary"]["client_receivables"]["received_total"] == 0
    assert d2["tranches"][0]["payments"] == [] if "payments" in d2["tranches"][0] else True
    r = await client.post(_url(p2, f"/tranches/{t1['id']}/payments"), json={"amount": 1, "request_id": uuid.uuid4().hex}, headers=headers)
    assert r.status_code == 404                                                       # another project's invoice is unreachable
    tid = await _locked(client, headers, p1, rate=1000, quote=1000)
    r = await client.post(_url(p2, f"/talents/{tid}/payments"), json={"amount": 1, "request_id": uuid.uuid4().hex}, headers=headers)
    assert r.status_code in (400, 404)


async def test_summary_after_a_fresh_reload_equals_the_stored_records_and_the_global_overview(client, headers):
    pid = await _project()
    tid = await _locked(client, headers, pid, rate=60000, quote=100000)
    t = await _tranche(client, headers, pid, name="A", amount=60000, gst_treatment="exclusive", gst_rate=18, due_date="2099-01-01")
    t2 = await _tranche(client, headers, pid, name="B", amount=40000)
    await _pay(client, headers, pid, t["id"], 35400, date="2026-10-01")
    await _pay(client, headers, pid, t2["id"], 10000, date="2026-10-02")
    await client.post(_url(pid, f"/talents/{tid}/payments"), json={"amount": 12345.67, "request_id": "reload-check-001"}, headers=headers)
    fresh = await _desk(client, headers, pid)
    stored = await db.project_payment_tranches.find({"project_id": pid}).to_list(10)
    stored_received = round(sum(sum(p["amount"] for p in s.get("payments", []) if p.get("status", "active") != "voided") for s in stored), 2)
    assert fresh["summary"]["tranches_received_total"] == stored_received == 45400
    cr = fresh["summary"]["client_receivables"]
    assert cr["received_total"] == 45400 and cr["received_gst_total"] == 5400                  # 35,400 of a 70,800 invoice -> half of 10,800
    assert cr["invoiced_total"] == 70800 + 40000 and cr["outstanding_total"] == round(cr["invoiced_total"] - cr["received_total"], 2)
    row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid})
    assert fresh["summary"]["talent_paid_total"] == sum(p["amount"] for p in row["pd_talent_payments"]) == 12345.67
    # base billing outstanding is reduced only by the non-GST part that was received
    assert fresh["summary"]["client_outstanding_total"] == round(100000 - (45400 - 5400), 2)
    ov = (await client.get("/api/production/overview", params={"q": fresh["project"]["brand_name"]}, headers=headers)).json()
    row_ov = next(r for r in ov["items"] if r["project_id"] == pid) if "items" in ov else next(r for r in ov["projects"] if r["project_id"] == pid)
    assert row_ov["client_payment"]["received"] == fresh["summary"]["client_received_total"]
    assert row_ov["client_payment"]["outstanding"] == fresh["summary"]["client_outstanding_total"]
    again = await _desk(client, headers, pid)
    assert again["summary"] == fresh["summary"]
