"""Production Desk money rules — GST, instalments and ageing, in ONE pure place.

Pure functions only (no database, no clock unless a `today` is passed in), so every figure the
desk shows can be reproduced and tested deterministically. Nothing here guesses:

  * GST is never assumed. A treatment must be recorded explicitly — "none", "exclusive" (GST is
    added on top of the recorded amount) or "inclusive" (the recorded amount already contains
    it). With no treatment, or a taxable treatment without a rate, the breakdown is reported as
    UNRESOLVED and no GST figure is produced. There is no default rate.
  * Rounding is half-up to 2 decimals, applied once per derived figure.
  * Historical records (no tax fields, no instalments) keep their exact old meaning: a tranche
    marked "received" counts its full amount, a talent marked "cleared" counts the full net payable.
  * Ageing needs a due date. A due date is only ever (1) the one recorded, (2) invoice date +
    the number of days written in the payment terms, or (3) the project's expected payment date —
    each labelled. With none of those the due date is "unavailable"; it is never invented.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

GST_TREATMENTS = ("none", "exclusive", "inclusive")
CENT = Decimal("0.01")
TOLERANCE = 0.005  # half a paisa: float noise must never flip a paid invoice to "partially paid"


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


def money(v: Any) -> float:
    """Round half-up to 2 decimals (the one rounding rule)."""
    return float(_d(v).quantize(CENT, rounding=ROUND_HALF_UP))


def num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_date(raw: Any) -> Optional[date]:
    """YYYY-MM-DD (or an ISO datetime) -> date; anything else -> None (never guessed)."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------- GST
def gst_breakdown(amount: Any, treatment: Optional[str], rate: Any) -> Dict[str, Any]:
    """Split a recorded amount into base / GST / total according to the RECORDED tax treatment.

    state: "not_recorded"  no treatment recorded (legacy)  -> total = amount, base/gst unavailable
           "none"          explicitly not taxable          -> base = total = amount, gst = 0
           "exclusive"     GST added on top of `amount`    -> base = amount, total = base + gst
           "inclusive"     `amount` already includes GST   -> total = amount, base = total - gst
           "incomplete"    taxable treatment without a valid rate -> nothing derived
    """
    amt = num(amount)
    out: Dict[str, Any] = {"state": "not_recorded", "treatment": treatment or None, "rate": None, "base": None, "gst": None, "total": None}
    if amt is None:
        return out
    if not treatment:
        out["total"] = money(amt)
        return out
    if treatment == "none":
        return {**out, "state": "none", "rate": 0.0, "base": money(amt), "gst": 0.0, "total": money(amt)}
    r = num(rate)
    if treatment not in GST_TREATMENTS or r is None or r < 0 or r > 100:
        return {**out, "state": "incomplete", "total": money(amt)}
    if treatment == "exclusive":
        base = money(amt)
        gst = money(_d(base) * _d(r) / Decimal(100))
        return {**out, "state": "exclusive", "rate": r, "base": base, "gst": gst, "total": money(_d(base) + _d(gst))}
    total = money(amt)  # inclusive
    base = money(_d(total) * Decimal(100) / (Decimal(100) + _d(r)))
    return {**out, "state": "inclusive", "rate": r, "base": base, "gst": money(_d(total) - _d(base)), "total": total}


# ------------------------------------------------------------------------------------ instalments
def active_payments(payments: Optional[List[dict]]) -> List[dict]:
    return [p for p in (payments or []) if p.get("status", "active") != "voided"]


def received_split(payments: Optional[List[dict]], gst: Optional[float], total: Optional[float]) -> Dict[str, Optional[float]]:
    """(received, GST part of received). GST part per instalment = the explicit `gst_amount` when one
    was recorded, otherwise pro-rata to the invoice's own GST/total mix; once the invoice is paid in
    full the GST part is exactly the invoice's GST (so rounding drift can never leave a residue)."""
    act = active_payments(payments)
    received = money(sum(_d(p.get("amount")) for p in act))
    if gst is None or total is None:
        return {"received": received, "received_gst": None}
    if total > 0 and received >= total - TOLERANCE:
        return {"received": received, "received_gst": money(gst)}
    part = Decimal("0")
    for p in act:
        explicit = num(p.get("gst_amount"))
        if explicit is not None:
            part += _d(explicit)
        elif total > 0:
            part += _d(p.get("amount")) * _d(gst) / _d(total)
    return {"received": received, "received_gst": money(min(max(part, Decimal("0")), _d(gst)))}


def payment_dates(payments: Optional[List[dict]]) -> Dict[str, Optional[str]]:
    dates = sorted(d.isoformat() for d in (parse_date(p.get("date")) for p in active_payments(payments)) if d)
    return {"first_payment_date": dates[0] if dates else None, "last_payment_date": dates[-1] if dates else None}


def payment_state(total: Optional[float], received: float) -> str:
    """unpaid | partially_paid | paid  (never "paid" from a single instalment unless it covers the total)."""
    if total is None:
        return "unknown"
    if received <= TOLERANCE:
        return "unpaid"
    return "paid" if received >= total - TOLERANCE else "partially_paid"


# ----------------------------------------------------------------------------------------- ageing
_TERMS_RE = re.compile(r"(?:net\s*(\d{1,3}))|(?:(\d{1,3})\s*(?:calendar\s*|working\s*|business\s*)?days?)", re.I)


def terms_days(terms: Optional[str]) -> Optional[int]:
    """Days written in free-text payment terms ("Net 30", "30 days"). None when there is no number —
    words like "on receipt" are not interpreted."""
    if not terms or not isinstance(terms, str):
        return None
    m = _TERMS_RE.search(terms)
    if not m:
        return None
    n = int(m.group(1) or m.group(2))
    return n if 0 < n <= 365 else None


def due_info(explicit_due: Any, invoice_date: Any, terms: Optional[str], expected_date: Any) -> Dict[str, Optional[str]]:
    due = parse_date(explicit_due)
    if due:
        return {"due_date": due.isoformat(), "due_basis": "due_date"}
    inv, days = parse_date(invoice_date), terms_days(terms)
    if inv and days:
        return {"due_date": (inv + timedelta(days=days)).isoformat(), "due_basis": "payment_terms"}
    exp = parse_date(expected_date)
    if exp:
        return {"due_date": exp.isoformat(), "due_basis": "expected_payment_date"}
    return {"due_date": None, "due_basis": None}


def ageing(due_iso: Optional[str], outstanding: Optional[float], today: date, invoice_date: Any = None) -> Dict[str, Any]:
    due = parse_date(due_iso)
    open_amount = outstanding is not None and outstanding > TOLERANCE
    overdue_days = (today - due).days if due and open_amount and today > due else 0
    inv = parse_date(invoice_date)
    return {
        "overdue": overdue_days > 0,
        "days_overdue": overdue_days,
        # labelled fallback when there is no due date: how long since the invoice date (never "overdue")
        "days_since_invoice": (today - inv).days if inv and open_amount and today >= inv else None,
    }


def status_label(state: str, overdue: bool) -> str:
    base = {"unpaid": "Unpaid", "partially_paid": "Partially Paid", "paid": "Paid"}.get(state, "Unknown")
    return "Overdue" if overdue and state != "paid" else base


# ------------------------------------------------------------------------- one obligation, one view
def receivable_view(
    *, amount: Any, treatment: Optional[str], rate: Any, payments: Optional[List[dict]], legacy_received: bool,
    due_date: Any, invoice_date: Any, terms: Optional[str], expected_date: Any, today: date,
) -> Dict[str, Any]:
    """A client invoice (tranche). `legacy_received` = the old binary "received" flag on a row that has no
    instalments: it means the whole recorded amount was received, exactly as before."""
    tax = gst_breakdown(amount, treatment, rate)
    total = tax["total"]
    if payments:
        split = received_split(payments, tax["gst"], total)
        received, received_gst = split["received"], split["received_gst"]
    elif legacy_received and total is not None:
        # The old binary flag meant "the recorded amount was received". If GST is recorded later on top of it
        # the GST is NOT assumed received — it shows as outstanding until a payment is recorded.
        received = min(total, money(amount))
        received_gst = tax["gst"] if (tax["gst"] is not None and received >= total - TOLERANCE) else (0.0 if tax["gst"] is not None else None)
    else:
        received, received_gst = 0.0, (0.0 if tax["gst"] is not None else None)
    outstanding = money(max(_d(total) - _d(received), Decimal("0"))) if total is not None else None
    state = payment_state(total, received)
    due = due_info(due_date, invoice_date, terms, expected_date)
    age = ageing(due["due_date"], outstanding, today, invoice_date)
    gst_out = None if tax["gst"] is None else money(max(_d(tax["gst"]) - _d(received_gst), Decimal("0")))
    return {
        "tax": tax,
        "invoice_total": total,
        "received": received,
        "received_gst": received_gst,
        "outstanding": outstanding,
        "outstanding_gst": gst_out,
        "overpaid": money(_d(received) - _d(total)) if total is not None and received > total + TOLERANCE else 0.0,
        "state": state,
        "status": status_label(state, age["overdue"]),
        "instalments": len(active_payments(payments)),
        **payment_dates(payments),
        **due,
        **age,
    }


def talent_obligation_view(
    *, net_payable: Optional[float], treatment: Optional[str], rate: Any, gst_invoice_number: Optional[str],
    gst_invoice_date: Any, due_date: Any, payments: Optional[List[dict]], legacy_cleared: bool, today: date,
) -> Optional[Dict[str, Any]]:
    """What Talentgram owes ONE talent. GST is added only when a taxable treatment, a rate AND a GST invoice
    reference are all recorded ("valid GST invoice"); otherwise the payable is the net payable and the GST
    state says why. Talent GST is independent of commission and of the client's GST — nothing here nets
    them off or assumes input-tax credit."""
    if net_payable is None:
        return None
    taxable = treatment in ("exclusive", "inclusive")
    ref = (gst_invoice_number or "").strip()
    if taxable:
        tax = gst_breakdown(net_payable, treatment, rate)
        valid = tax["state"] in ("exclusive", "inclusive") and bool(ref)
        if not valid:
            gst_state = "invoice_missing" if tax["state"] in ("exclusive", "inclusive") else "incomplete"
            tax = {"state": "none", "treatment": treatment, "rate": None, "base": money(net_payable), "gst": 0.0, "total": money(net_payable)}
        else:
            gst_state = "valid"
    else:
        gst_state = "no_gst"
        tax = {"state": "none", "treatment": treatment or None, "rate": None, "base": money(net_payable), "gst": 0.0, "total": money(net_payable)}
    total = tax["total"]
    if payments:
        paid = received_split(payments, tax["gst"], total)["received"]
    elif legacy_cleared:
        paid = money(net_payable)  # the old "cleared" flag meant the net payable was paid
    else:
        paid = 0.0
    balance = money(max(_d(total) - _d(paid), Decimal("0")))
    state = payment_state(total, paid)
    age = ageing(parse_date(due_date).isoformat() if parse_date(due_date) else None, balance, today, gst_invoice_date)
    return {
        "gst_state": gst_state,
        "treatment": treatment or None,
        "rate": tax["rate"],
        "gst_invoice_number": ref or None,
        "gst_invoice_date": parse_date(gst_invoice_date).isoformat() if parse_date(gst_invoice_date) else None,
        "net_payable": money(net_payable),
        "base": tax["base"],
        "gst": tax["gst"],
        "total_payable": total,
        "paid": paid,
        "balance": balance,
        "state": state,
        "status": status_label(state, age["overdue"]),
        "instalments": len(active_payments(payments)),
        **payment_dates(payments),
        "due_date": parse_date(due_date).isoformat() if parse_date(due_date) else None,
        "due_basis": "due_date" if parse_date(due_date) else None,
        **age,
    }


def validate_new_payment(total_due: Optional[float], already_received: float, amount: float, gst_amount: Optional[float]) -> Optional[str]:
    """None when the instalment is acceptable, otherwise the reason. Overpayment is never silently accepted."""
    if amount is None or amount <= 0:
        return "Payment amount must be greater than zero."
    if gst_amount is not None and (gst_amount < 0 or gst_amount > amount + TOLERANCE):
        return "The GST portion must be between 0 and the payment amount."
    if total_due is None:
        return "This obligation has no amount yet, so a payment cannot be recorded against it."
    if already_received + amount > total_due + TOLERANCE:
        return (f"This payment would exceed the amount due: ₹{money(max(_d(total_due) - _d(already_received), Decimal('0'))):,.2f} remains. "
                "Correct the invoice amount first if the total changed.")
    return None
