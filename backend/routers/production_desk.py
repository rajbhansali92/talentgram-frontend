"""Production Desk — the post-lock operational workspace for a project.

Reuses, rather than duplicates, everything that already exists:

  • Locked talents        — read from `casting_pipeline` (stage == "locked"),
                             the EXISTING project-talent relationship. No
                             second "production talent" entity.
  • Talent budget/commission/payment status — stored as small, additive
                             `pd_*`-prefixed fields directly ON the existing
                             `casting_pipeline` documents (the row already
                             representing this exact talent-in-this-project
                             relationship), not a new collection.
  • Production budget/shooting days/checklist/production contact — small
                             additive `pd_*` fields directly on the existing
                             `projects` document, alongside the project's
                             already-existing commission_percent, shoot_dates,
                             medium_usage, etc. (displayed here, never
                             duplicated/re-entered).
  • Crew & kickback recipients — reference `db.clients` (routers/marketing.py's
                             existing CRM), by ObjectId. No new contacts table.
  • Documents (call sheet, agreement, invoice, reimbursement bills, ...) —
                             pushed onto the project's EXISTING
                             `materials[]` array via the exact same
                             `attach_project_material()` Cloudinary upload
                             path `POST /projects/{pid}/material` already
                             uses (category list widened by a few new
                             values — see core.MATERIAL_CATEGORIES).

Two genuinely new, minimal collections, because nothing in the existing
schema represents these concepts at all:

  • `project_kickbacks`       {id, project_id, amount, recipient_client_id,
                                recipient_name, notes, created_at, created_by}
  • `project_reimbursements`  {id, project_id, talent_id, expense_type,
                                amount, date, notes, status, material_id,
                                created_at, created_by}
  • `project_crew`            {id, project_id, client_id, role, status,
                                created_at} — a role-tagged junction between
                                a project and an EXISTING CRM client, not a
                                new contacts table.

No separate Finance/accounting module exists anywhere in this codebase
(re-verified for the Finance/Zoho connector pass — grepped the whole repo
again, including synonyms: billing, ledger, invoice, GST, TDS, bookkeeping).
The only near-miss is `routers/workflow.py`'s generic team to-do tracker,
which has a free-text "Finance" task *category* (checklist strings like
"Invoice Sent", "Payment Received") — that is a manual checklist app with
no amounts, no talent/project-typed linkage, and no calculations; it is
NOT a financial ledger and is deliberately left unconnected. This means
Production Desk's own `pd_*` fields + `project_kickbacks` /
`project_reimbursements` ARE the sole, authoritative store for this data —
there is no second copy anywhere to diverge from or reconcile against.

Two pre-existing, genuinely-project-level fields deliberately are NOT
wired into Production Desk's numbers: `project.talent_budget` and
`project.client_budget` (free-text `{label, value}` lines edited via
`BudgetLines` in Project Details). Those are pre-lock negotiation/ask
hints shown to talents on the submission form and to clients on the
public link — a different purpose and shape from Production Desk's
typed, per-locked-talent payable amount. They are surfaced here
READ-ONLY (see `client_budget_lines`/`talent_budget_lines` in the GET
response) purely so a manager doesn't have to tab-switch to see them —
never merged into the commission/payment math.

Zoho Books: no integration exists (confirmed — no OAuth/token/API-client/
organization-id/webhook code anywhere in the repo; the one incidental
"zoho" string hit is a coincidental base64 substring inside an unrelated
logo image file). Per the task's own Case-B instructions this pass does
NOT build one — `finance.zoho_status` below is a literal, static
"not_connected", never flipped to a fake "synced" state. The natural
future attachment points, if a Zoho sync is ever built, are the existing
stable ids already returned here: a locked talent's `pd_payment_status`
(→ Zoho vendor/talent payment), `project_kickbacks` rows (→ Zoho expense
or equivalent), `project_reimbursements` rows (→ Zoho expense), and
`pd_payment_in_received` (→ Zoho customer payment/invoice). None of that
mapping is implemented here — only documented as the boundary.

No generic activity/audit log exists (every audit collection in this repo
is feature-specific: storage_audit_log, profile_audits, scout_capture_audit,
otp_audit_logs, whatsapp_audit_log). The closest genuinely reusable,
generic mechanism is `notifications.fanout()` (the admin bell / Dashboard
"Recent Activity" feed) — reused below (not rebuilt) for the two highest-
signal financial state changes: a locked talent's payment marked cleared,
and a project's client payment (Payment In) marked received. No other
Production Desk write fires a notification, to avoid turning this into a
noisy audit trail the existing mechanism was never designed for.

No generic reminder-scheduling infrastructure exists — the only reminder
mechanism in the repo (`_compute_ongoing_pipeline_reminders` in
routers/whatsapp.py) is specifically a talent-submission-follow-up engine
tied to Casting Pipeline stages, not a general "notify me while X stays
pending" scheduler, so Production Desk's checklist/payment-pending items
cannot cleanly hook into it without building a new engine — which is out
of scope here and left as a disclosed limitation.

No WhatsApp-agent command wiring is included in this pass — connecting
"What's pending for X" style commands would mean extending the existing
multi-agent NLU/intent-routing architecture, which is a meaningfully
larger, dedicated piece of work, not a "very small change".
"""
from __future__ import annotations

import logging
import uuid
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from core import (
    COMMISSION_OPTIONS,
    _now,
    current_admin,
    current_team_or_admin,
    db,
)
# Reuse the EXISTING generic admin-notification fan-out (Dashboard "Recent
# Activity" feed) — not a new activity/audit system. See module docstring.
from notifications import fanout as notify_fanout

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["Production Desk"])

# Zoho Books integration does not exist anywhere in this codebase (see
# module docstring). This is a literal, static, honest state — never
# mutated by any code path — NOT a placeholder for a real sync.
ZOHO_STATUS = "not_connected"

# V2 polish (spec section 21) — the agency's own fixed billing details,
# appended verbatim to every talent invoice-request WhatsApp message.
# Static by design; not a per-project/per-client field anywhere in this
# schema, so it lives here rather than as new DB state.
BILLING_DETAILS_BLOCK = (
    "Billing Details:\n\n"
    "Company Name: Talentgram Agency LLP\n\n"
    "Address: 12, Ground Floor office no.8, Anand Bhuvan Building, "
    "Jagannath Sankarseth Marg, Mangalwadi, Girgaon, India, Maharashtra, "
    "Mumbai-400004\n\n"
    "Email ID: team@talentgramagency.com\n\n"
    "GSTIN No: 27AAVFT3898G1Z8\n\n"
    "PAN No: AAVFT3898G"
)

# Categories Production Desk can attach through the EXISTING project
# material pipeline (widens routers.projects.MATERIAL_CATEGORIES — see
# server.py startup, which merges this set in once, not a parallel list
# checked separately).
PRODUCTION_DESK_DOCUMENT_CATEGORIES = {
    "client_confirmation", "po", "agreement", "invoice", "call_sheet",
    "payment_proof", "reimbursement_bill", "gst_tds_document",
}

PAYMENT_STATUSES = {"pending", "cleared"}
REIMBURSEMENT_STATUSES = {"pending", "paid"}
CREW_ROLES = {
    "Director", "Producer", "DOP", "Photographer", "Stylist", "Makeup",
    "Hair", "Production Manager", "Line Producer", "Client", "Casting",
    "Editor", "Other",
}
# Post-lock operational lifecycle (Part 3, Production Checklist + Management
# Agent pass) — the existing project.status field (ongoing/hold/complete/
# locked, routers/projects.py) is a coarse, whole-project state used for
# Project List grouping; it has no granularity for "casting is locked but
# we haven't confirmed/shot/closed finance yet". Rather than overload that
# field or build a workflow engine, this is one small additive pd_* enum,
# purely informational — nothing in the backend gates on it.
PRODUCTION_STATUS_OPTIONS = ["not_started", "confirmed", "shoot_scheduled", "shoot_complete", "finance_closed"]

# Shoot status — deliberately a MANUALLY-SET status enum, not computed
# from a parsed date. Neither the project nor a locked talent has a
# structured shoot-date field anywhere in this schema (project.shoot_dates
# is free text like "24th - 30th August (ANY ONE DAY)" — not reliably
# parseable without guessing, which this codebase's own "never guess,
# always deterministic" convention rules out). "today" is therefore an
# explicit status a manager sets, the same way every other pd_* status
# field in this file already works — this is what TODAY's "shoots today"
# section reads, never a date computation.
SHOOT_STATUS_OPTIONS = ["not_scheduled", "scheduled", "today", "completed", "cancelled"]
TRIAL_STATUS_OPTIONS = ["not_scheduled", "scheduled", "completed"]
PAYMENT_FOLLOWUP_STATUSES = ["not_due", "due", "in_progress", "done"]

# ── Production Desk V2 (talent-level shooting/overtime/reimbursements) ─────
# Per-talent shoot-day records, readings/rehearsals, and payment tranches
# are all small, additive structures on the SAME existing rows/collections
# this module already owns — no new "talent scheduling" or "finance"
# system. See module docstring for the overall reuse principle.

# Reuses SHOOT_STATUS_OPTIONS for each individual day record's own status
# (a talent can be "today" on one date and "scheduled" on another).

# Agreement Signed — some projects genuinely have no agreement step, hence
# a real N/A rather than forcing every project into a false "Pending".
AGREEMENT_STATUS_OPTIONS = ["done", "pending", "n_a"]

# Payment Tranches — an operational billing-milestone tracker, deliberately
# NOT an accounting/invoicing engine (see module docstring's Zoho section
# for the same "honest, not-built" boundary). invoice_status mirrors the
# same raised/raised_and_sent vocabulary the project-level checklist uses,
# so one convention is used everywhere rather than two.
TRANCHE_INVOICE_STATUSES = ["pending", "raised", "raised_and_sent"]
TRANCHE_PAYMENT_STATUSES = ["pending", "received"]

READING_REHEARSAL_TYPES = {"reading", "rehearsal"}


# ── Business timezone: Asia/Kolkata (IST, UTC+05:30) ────────────────────────
# The server runs in UTC, so a bare date.today()/datetime.now() disagrees with
# the business calendar for 5.5 hours every day (00:00-05:30 IST is still "yesterday" in UTC).
# Every Production Desk "today" below goes through these two helpers instead —
# the same zone the reminder worker (services/production_reminder_worker._ist_today)
# already uses, so the desk, the reminders and the Management Agent agree.
IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_today() -> date:
    return ist_now().date()


def ist_day_bounds_utc() -> tuple:
    """[start, end) of the CURRENT IST calendar day, as ISO strings for comparing against stored *_at values.

    "Today" comes from the real clock in IST. Stored values are compared by the date they are WRITTEN
    with — the codebase's documented convention (services/production_reminder_worker.py): date-only
    fields are noon-UTC of the picked date, and times typed through WhatsApp keep the hour as typed
    but carry a UTC tag. Bucketing those by the real UTC instant of the IST day would push a 7 PM IST
    costume trial (stored 19:00Z) into tomorrow, so the bounds are the IST date's own 00:00-24:00 in the
    stored tagging, never a UTC conversion of IST midnight."""
    d = ist_today()
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


# Call / reporting times are IST wall-clock times — stored as plain "HH:MM" (24h), never as a
# UTC instant, so nothing can shift them across a day boundary. Legacy free-text values
# ("9 AM", "morning") already in the DB are left untouched on read; only NEW writes are validated.
_CLOCK_24H = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CLOCK_12H = re.compile(r"^(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AaPp])\.?[Mm]\.?$")


def parse_clock(raw: Optional[str]) -> Optional[int]:
    """Minutes since midnight for "09:30" / "9:30 AM" / "9 pm", else None (unparseable / empty)."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    m = _CLOCK_24H.match(t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _CLOCK_12H.match(t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return hour * 60 + int(m.group(2) or 0)
    return None


def normalize_clock(raw: Optional[str], field: str) -> Optional[str]:
    """Canonical "HH:MM" for a NEW write; None/"" clears the field; anything unparseable is a 400."""
    if raw is None or str(raw).strip() == "":
        return None
    minutes = parse_clock(raw)
    if minutes is None:
        raise HTTPException(400, f"{field} must be a time like 09:30 (IST)")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_reporting_before_call(reporting: Optional[str], call: Optional[str]) -> None:
    """Reporting time is when the talent must arrive; the call is when they are called onto set,
    so reporting may not be LATER than call. Only enforced when both parse (legacy text is exempt)."""
    r, c = parse_clock(reporting), parse_clock(call)
    if r is not None and c is not None and r > c:
        raise HTTPException(400, "Reporting time cannot be later than call time")


def build_maps_url(place_id: Optional[str], lat: Optional[float], lng: Optional[float], address: Optional[str]) -> Optional[str]:
    """A canonical Google Maps URL for a selected place (the Maps URLs API — no key needed to OPEN)."""
    from urllib.parse import quote_plus
    if lat is not None and lng is not None:
        url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    elif address:
        url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"
    else:
        return None
    if place_id:
        url += f"&query_place_id={quote_plus(place_id)}"
    return url


def _num(v) -> Optional[float]:
    """Best-effort float coercion — treats "", None, and non-numeric input
    as absent rather than raising, since every Production Desk numeric
    field is optional (a talent may simply not have a rate entered yet)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _commission_fraction(raw: Optional[str]) -> Optional[float]:
    """"15%" -> 0.15. Same COMMISSION_OPTIONS strings the rest of the app
    already stores on project.commission_percent — never re-parsed
    differently in two places."""
    if not raw:
        return None
    try:
        return float(str(raw).strip().rstrip("%")) / 100.0
    except ValueError:
        return None


def _shoot_day_extra_hours_amount(per_day_rate: Optional[float], record: dict) -> float:
    """The core overtime formula (spec section 1):

        hourly_rate = per_day_rate / agreed_hours
        extra_hours = max(0, actual_hours - agreed_hours)
        extra_amount = hourly_rate * extra_hours

    `agreed_hours` is read PER RECORD (never a hardcoded 12), so a project
    on a 12-hour basis and one on a 14-hour basis compute correctly from
    the exact same function — the "basis" is just whatever the admin typed
    into that specific shoot day. Missing per_day_rate, agreed_hours, or
    actual_hours all mean "not computable yet" (0), not an error — a
    manager may add the date before the rate/hours are finalised."""
    agreed = _num(record.get("agreed_hours"))
    actual = _num(record.get("actual_hours"))
    if not per_day_rate or not agreed or agreed <= 0 or actual is None:
        return 0.0
    extra = actual - agreed
    if extra <= 0:
        return 0.0
    return round((per_day_rate / agreed) * extra, 2)


def _talent_extra_hours_total(per_day_rate: Optional[float], shoot_days: List[dict]) -> float:
    return round(sum(_shoot_day_extra_hours_amount(per_day_rate, d) for d in (shoot_days or [])), 2)


def _talent_extra_hours_count(shoot_days: List[dict]) -> float:
    """Raw overtime HOURS (not money) across every shoot day — used only for
    the WhatsApp invoice message's "Extra Hours: N hours" line (spec section
    18). Independent of _talent_extra_hours_total, which is the money value
    already used everywhere else; never stored, computed fresh each time."""
    total = 0.0
    for d in shoot_days or []:
        agreed = _num(d.get("agreed_hours"))
        actual = _num(d.get("actual_hours"))
        if agreed and actual is not None and actual > agreed:
            total += actual - agreed
    return round(total, 2)


async def _get_project_or_404(pid: str) -> dict:
    project = await db.projects.find_one({"id": pid}, {"_id": 0})
    if not project:
        raise HTTPException(404, "Project not found")
    return project


async def _get_locked_pipeline_row(pid: str, talent_id: str) -> dict:
    """The single definitional check for "is this a locked talent on this
    project" — every talent-scoped Production Desk write goes through
    this, so nothing here can ever create or edit budget/payment data for
    a talent who isn't actually locked in Casting Pipeline right now."""
    from routers.casting_pipeline import _normalise_stage

    row = await db.casting_pipeline.find_one({"project_id": pid, "talent_id": talent_id})
    if not row:
        raise HTTPException(404, "This talent is not on this project's pipeline")
    if (_normalise_stage(row.get("stage")) or row.get("stage")) != "locked":
        raise HTTPException(400, "This talent is not currently LOCKED on this project")
    return row


def _talent_card(t: dict, row: dict, project: dict, reimbursement_total: float = 0.0) -> dict:
    """One locked talent's Production Desk view — budget, commission,
    overtime, reimbursements, payment. Effective shooting days/commission
    fall back to the project-level value when the talent has no override,
    and an explicitly-set budget_total is NEVER recomputed from
    per-day × days (only used when total itself is absent).

    V2 (talent-level shooting/overtime): when `pd_shoot_days` (the
    structured per-day schedule) has any records, its length becomes the
    authoritative `shooting_days` — the old manual `pd_shooting_days`
    integer is only a fallback for talents that never adopted the new
    per-day schedule, preserving old records unchanged (spec section 33).

    Commission formula (spec section 22): commission applies to
    Base Fee + Extra Hours — reimbursements are a separate, non-
    commissionable pass-through, exactly as before (reimbursement_total is
    reported alongside but never folded into commissionable_amount)."""
    from routers.casting_pipeline import _talent_merge_fields

    merged = _talent_merge_fields(t)
    per_day = _num(row.get("pd_budget_per_day"))
    explicit_total = _num(row.get("pd_budget_total"))
    shoot_days = row.get("pd_shoot_days") or []
    readings_rehearsals = row.get("pd_readings_rehearsals") or []
    manual_shooting_days = row.get("pd_shooting_days")
    if shoot_days:
        shooting_days = len(shoot_days)
    elif manual_shooting_days not in (None, ""):
        shooting_days = int(manual_shooting_days)
    else:
        shooting_days = project.get("pd_shooting_days")
    commission_pct_raw = row.get("pd_commission_percent")
    commission_fraction = (
        _num(commission_pct_raw) / 100.0 if commission_pct_raw not in (None, "")
        else _commission_fraction(project.get("commission_percent"))
    )

    if explicit_total is not None:
        budget_total = explicit_total
    elif per_day is not None and shooting_days:
        budget_total = per_day * shooting_days
    else:
        budget_total = None

    extra_hours_total = _talent_extra_hours_total(per_day, shoot_days)
    commissionable_amount = (
        round((budget_total or 0) + extra_hours_total, 2)
        if (budget_total is not None or extra_hours_total)
        else None
    )
    commission_amount = (
        round(commissionable_amount * commission_fraction, 2)
        if commissionable_amount is not None and commission_fraction is not None
        else None
    )
    # Talent invoice amount (spec section 24): commissionable minus
    # commission, PLUS reimbursements added back uncommissioned.
    invoice_amount = (
        round((commissionable_amount - commission_amount) + reimbursement_total, 2)
        if commissionable_amount is not None and commission_amount is not None
        else None
    )

    # ── Production (client) side — INTERNAL ONLY ───────────────────────────
    # `pd_budget_*` above is the TALENT-facing agreed rate: commission, the talent's
    # invoice and "Ask to Raise Invoice" are all computed from it and never from the
    # quote below. `pd_production_quote` is what Talentgram quoted the production for
    # this talent; the difference is Talentgram's own margin and must never reach a
    # talent-facing surface (see _build_invoice_message, which reads none of these keys).
    # None (unset) is NOT zero: legacy rows have no quote and nothing is invented for them.
    production_quote = _num(row.get("pd_production_quote"))
    spread = (
        round(production_quote - budget_total, 2)
        if production_quote is not None and budget_total is not None
        else None
    )
    # Overtime and reimbursements are billed through to the production exactly as they are
    # owed to the talent (no margin added on them) — see _financial_rollup's docstring.
    production_billable = (
        round(production_quote + extra_hours_total + reimbursement_total, 2)
        if production_quote is not None
        else None
    )
    talentgram_earning = (
        round((commission_amount or 0.0) + (spread or 0.0), 2)
        if commission_amount is not None or spread is not None
        else None
    )

    return {
        "talent_id": t.get("id"),
        "name": merged["talent_name"],
        "image_url": merged["image_url"],
        "instagram_handle": merged["instagram_handle"],
        "phone": merged["talent_phone"],
        "budget_per_day": per_day,
        "budget_total": budget_total,
        "budget_total_is_explicit": explicit_total is not None,
        "shooting_days": shooting_days,
        "commission_percent": (commission_fraction * 100.0) if commission_fraction is not None else None,
        "commission_amount": commission_amount,
        "payment_status": row.get("pd_payment_status") or "pending",
        # V2 — per-day shooting schedule + overtime (spec sections 1-4).
        "shoot_days": shoot_days,
        "extra_hours_total": extra_hours_total,
        "commissionable_amount": commissionable_amount,
        "reimbursement_total": round(reimbursement_total, 2),
        "invoice_amount": invoice_amount,
        # Production/client side (internal only) — see the block above.
        "talent_agreed_rate": budget_total,
        "talent_net_payable": invoice_amount,
        "production_quote": production_quote,
        "spread": spread,
        "production_billable": production_billable,
        "talentgram_earning": talentgram_earning,
        # V2 — Readings & Rehearsals (spec section 8).
        "readings_rehearsals": readings_rehearsals,
        # Talent Preparation (Phase 2) — additive fields on the SAME
        # locked casting_pipeline row, no second talent/project record.
        # fitting_status/look_test_status stay fully alive here — the
        # Management Agent already has real, working NLU commands and
        # readiness checks against them (agents/modules/management_agent.py);
        # spec section 6 only asks to remove them from the Production Desk
        # UI, not to break that — see V2 frontend changes, not here.
        "costume_trial_at": row.get("pd_costume_trial_at"),
        "costume_trial_time": row.get("pd_costume_trial_time"),
        "costume_trial_location": row.get("pd_costume_trial_location"),
        "costume_trial_map_url": row.get("pd_costume_trial_map_url"),
        "fitting_status": row.get("pd_fitting_status") or "not_scheduled",
        "look_test_status": row.get("pd_look_test_status") or "not_scheduled",
        "grooming_requirements": row.get("pd_grooming_requirements"),
        "special_instructions": row.get("pd_special_instructions"),
        "shoot_status": row.get("pd_shoot_status") or "not_scheduled",
        # V2 polish — WhatsApp destination preference (spec section 22-25).
        # `whatsapp_group_name` is the SAME field the existing campaign
        # engine's own _resolve_destination() already uses as its source of
        # truth for "does this talent have a group" (routers/whatsapp.py) —
        # not a new mapping. See build_talent_invoice_message's docstring
        # for why the actual wa.me link still targets the phone number.
        "whatsapp_group_name": (t.get("whatsapp_group_name") or "").strip() or None,
    }


async def _locked_talent_cards(pid: str, project: dict, reimb_totals: Optional[Dict[str, float]] = None) -> List[dict]:
    rows = await db.casting_pipeline.find({"project_id": pid, "stage": "locked"}, {"_id": 0}).to_list(2000)
    if not rows:
        return []
    reimb_totals = reimb_totals or {}
    talent_ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    talents = await db.talents.find(
        {"id": {"$in": talent_ids}},
        {"_id": 0, "id": 1, "name": 1, "email": 1, "phone": 1, "instagram_handle": 1, "cover_media_id": 1, "media": 1, "whatsapp_group_name": 1},
    ).to_list(len(talent_ids))
    by_id = {t["id"]: t for t in talents}
    cards = []
    for row in rows:
        t = by_id.get(row.get("talent_id"))
        if not t:
            continue
        cards.append(_talent_card(t, row, project, reimb_totals.get(row.get("talent_id"), 0.0)))
    return cards


def _serialise_client_ref(client_id: Optional[str], name_cache: Dict[str, dict]) -> Optional[dict]:
    if not client_id:
        return None
    info = name_cache.get(client_id) or {}
    return {
        "client_id": client_id,
        "name": info.get("name", ""),
        "phone_number": info.get("phone_number"),
        # V2 polish (spec section 12) — the CRM "peek" needs a bit more than
        # name+phone; these ride along on the exact same lookup every
        # existing client-ref caller (kickback recipient, crew, payment
        # follow-up contact) already uses, so nothing fetches twice.
        "company_name": info.get("company_name"),
        "email": info.get("email"),
        "contact_type": info.get("contact_type"),
        "designation": info.get("designation"),
    }


async def _client_name_map(client_ids: List[str]) -> Dict[str, dict]:
    """Batch-resolve CRM client display name + phone — one query regardless
    of how many kickbacks/crew/payment-followup rows reference clients.
    Phone is needed by the WhatsApp one-tap actions (spec sections 17/23);
    exposed via the same map every existing client-ref caller already
    uses, not a second lookup."""
    from bson import ObjectId
    from bson.errors import InvalidId

    oids = []
    for cid in set(client_ids):
        if not cid:
            continue
        try:
            oids.append(ObjectId(cid))
        except (InvalidId, TypeError):
            continue
    if not oids:
        return {}
    docs = await db.clients.find(
        {"_id": {"$in": oids}},
        {"name": 1, "phone_number": 1, "company_name": 1, "email": 1, "contact_type": 1, "designation": 1},
    ).to_list(len(oids))
    return {
        str(d["_id"]): {
            "name": d.get("name", ""), "phone_number": d.get("phone_number"),
            "company_name": d.get("company_name"), "email": d.get("email"),
            "contact_type": d.get("contact_type"), "designation": d.get("designation"),
        }
        for d in docs
    }


_ACTIVE_TASK_STATUSES = ["pending", "in_progress"]


async def _tasks_for_project(pid: str, talent_ids: List[str]) -> List[dict]:
    """Every workflow_tasks row tied to this project OR any of its locked
    talents — the EXACT SAME db.workflow_tasks collection the admin
    Workflow page and the Management Agent read/write. No second task
    store; a task created in any of the three places shows up in all of
    them immediately."""
    or_clauses: List[dict] = [{"project_id": pid}]
    if talent_ids:
        or_clauses.append({"talent_id": {"$in": talent_ids}})
    tasks = await db.workflow_tasks.find({"$or": or_clauses}, {"_id": 0}).sort("due_at", 1).to_list(500)
    return tasks


def _bucket_tasks(tasks: List[dict], today_start: str, today_end: str) -> Dict[str, List[dict]]:
    """Deterministic date-string bucketing (no date parsing needed — see
    module docstring: every due_at is an ISO 8601 UTC string, same shape
    as core._now(), so lexicographic comparison is chronological
    comparison)."""
    due_today, overdue, upcoming, pending = [], [], [], []
    for t in tasks:
        status = t.get("status") or "pending"
        due_at = t.get("due_at")
        if status in _ACTIVE_TASK_STATUSES:
            pending.append(t)
            if due_at:
                if due_at < today_start:
                    overdue.append(t)
                elif today_start <= due_at < today_end:
                    due_today.append(t)
                elif due_at >= today_end:
                    upcoming.append(t)
    return {"due_today": due_today, "overdue": overdue, "upcoming": upcoming, "pending": pending}


def _financial_rollup(
    locked: List[dict], project: dict, *, extra_hours_total: float, reimbursements_total: float,
    kickbacks_total: float, commission_gross: float, tranches: List[dict], tranches_received_total: float,
) -> Dict[str, Any]:
    """The ONE place the project-level money model is computed (the desk summary and the
    payment follow-up message both call this, so they can never disagree).

      Talent rate         what the talent was told (pd_budget_*)           — talent-facing
      Production quote    what the production is charged (pd_production_quote) — internal
      Commission          commission % x (talent rate + overtime)          — never on the quote
      Spread              production quote - talent rate (only where a quote exists)
      Talentgram earning  commission + spread
      Talent net payable  talent rate + overtime - commission + reimbursements
      Production billable production quote + overtime + reimbursements
      Client outstanding  production billable - amount received

    Overtime and reimbursements are passed through to the production at the same amount owed
    to the talent. Nothing here invents a number: a talent with no production quote contributes
    no spread, and a project whose quotes are only partly entered reports `partial` rather than a
    misleading total. Legacy projects that only ever had the single project-level production
    budget (`pd_production_budget_total`) keep using it (`project_budget` basis) until per-talent
    quotes are entered."""
    quotes = [c["production_quote"] for c in locked if c.get("production_quote") is not None]
    missing = len(locked) - len(quotes)
    legacy_total = _num(project.get("pd_production_budget_total"))
    if legacy_total is None:
        per_day = _num(project.get("pd_production_budget_per_day"))
        days = project.get("pd_shooting_days")
        legacy_total = per_day * days if per_day is not None and days else None

    if locked and missing == 0:
        basis, base = "per_talent", sum(quotes)
    elif quotes:
        basis, base = "partial", sum(quotes)
    elif legacy_total is not None:
        basis, base = "project_budget", legacy_total
    else:
        basis, base = "none", None

    billable = round(base + extra_hours_total + reimbursements_total, 2) if base is not None else None

    if tranches:
        received = float(tranches_received_total)
    else:
        received = 0.0
    # An explicit "client payment received" tick means the production has paid in full.
    if project.get("pd_payment_in_received") and billable is not None:
        received = max(received, billable)
    outstanding = (
        round(max(billable - received, 0.0), 2)
        if billable is not None and basis != "partial"
        else None
    )

    spreads = [c["spread"] for c in locked if c.get("spread") is not None]
    spread_total = round(sum(spreads), 2)
    nets = [c["talent_net_payable"] for c in locked if c.get("talent_net_payable") is not None]
    return {
        "production_basis": basis,
        "production_quote_total": round(sum(quotes), 2),
        "production_quotes_set": len(quotes),
        "production_quotes_missing": missing,
        "production_billable_total": billable,
        "production_overtime_total": round(extra_hours_total, 2),
        "production_reimbursements_total": round(reimbursements_total, 2),
        "talent_agreed_total": round(sum(c["talent_agreed_rate"] for c in locked if c.get("talent_agreed_rate") is not None), 2),
        "talent_payable_total": round(sum(nets), 2),
        "spread_total": spread_total,
        "talentgram_earnings_total": round(commission_gross + spread_total, 2),
        "talentgram_earnings_net_of_kickbacks": round(commission_gross + spread_total - kickbacks_total, 2),
        "client_received_total": round(received, 2),
        "client_outstanding_total": outstanding,
    }


# ---------------------------------------------------------------------------
# GET /projects/{pid}/production-desk — the consolidated view
# ---------------------------------------------------------------------------
@router.get("/{pid}/production-desk")
async def get_production_desk(pid: str, admin: dict = Depends(current_team_or_admin)):
    project = await _get_project_or_404(pid)

    # Reimbursements are fetched BEFORE the locked-talent cards (rather than
    # after, as before V2) so each talent's card can carry its own
    # reimbursement_total — same underlying db.project_reimbursements
    # collection/query, just reordered.
    reimbursements = await db.project_reimbursements.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(500)
    reimb_totals: Dict[str, float] = {}
    for r in reimbursements:
        tid = r.get("talent_id")
        if tid:
            reimb_totals[tid] = reimb_totals.get(tid, 0.0) + (_num(r.get("amount")) or 0.0)

    locked = await _locked_talent_cards(pid, project, reimb_totals)

    talent_budget_total = sum(c["budget_total"] for c in locked if c["budget_total"] is not None)
    extra_hours_total_all = sum(c["extra_hours_total"] for c in locked)
    commission_gross = sum(c["commission_amount"] for c in locked if c["commission_amount"] is not None)
    cleared = sum(1 for c in locked if c["payment_status"] == "cleared")
    pending_amount = sum(
        c["budget_total"] for c in locked
        if c["payment_status"] == "pending" and c["budget_total"] is not None
    )

    kickbacks = await db.project_kickbacks.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(500)
    kickbacks_total = sum(_num(k.get("amount")) or 0 for k in kickbacks)
    commission_net = commission_gross - kickbacks_total

    reimbursements_total_all = round(sum(_num(r.get("amount")) or 0.0 for r in reimbursements), 2)
    # Checklist reimbursement status (spec section 11) — computed, never
    # stored: N/A when the project genuinely has none, complete only once
    # every one of them is "paid", otherwise pending. Never a false
    # "Pending" when there's nothing to reimburse.
    if not reimbursements:
        reimbursement_checklist_status = "n_a"
    elif all(r.get("status") == "paid" for r in reimbursements):
        reimbursement_checklist_status = "complete"
    else:
        reimbursement_checklist_status = "pending"

    tranches = await db.project_payment_tranches.find({"project_id": pid}, {"_id": 0}).sort("created_at", 1).to_list(200)
    tranches_total = round(sum(_num(t.get("amount")) or 0.0 for t in tranches), 2)
    tranches_received_total = round(sum(_num(t.get("amount")) or 0.0 for t in tranches if t.get("payment_status") == "received"), 2)

    crew = await db.project_crew.find({"project_id": pid}, {"_id": 0}).sort("created_at", 1).to_list(200)

    name_map = await _client_name_map(
        [k.get("recipient_client_id") for k in kickbacks]
        + [c.get("client_id") for c in crew]
        + [project.get("pd_production_contact_client_id")]
    )
    for k in kickbacks:
        k["recipient"] = _serialise_client_ref(k.get("recipient_client_id"), name_map)
    for c in crew:
        c["contact"] = _serialise_client_ref(c.get("client_id"), name_map)

    reimbursement_talent_ids = [r.get("talent_id") for r in reimbursements if r.get("talent_id")]
    reimb_talent_names: Dict[str, str] = {}
    if reimbursement_talent_ids:
        rt_docs = await db.talents.find({"id": {"$in": reimbursement_talent_ids}}, {"_id": 0, "id": 1, "name": 1}).to_list(len(reimbursement_talent_ids))
        reimb_talent_names = {d["id"]: d.get("name", "") for d in rt_docs}
    for r in reimbursements:
        r["talent_name"] = reimb_talent_names.get(r.get("talent_id"), "")

    documents = [m for m in (project.get("materials") or []) if m.get("category") in PRODUCTION_DESK_DOCUMENT_CATEGORIES]

    # Production Management Desk (Phase 1+2) — tasks from the SAME
    # db.workflow_tasks collection the admin Workflow page + Management
    # Agent use, bucketed by due date. Costume trials use a real
    # pd_costume_trial_at datetime (so "today"/"upcoming" IS a genuine
    # date comparison); shoots use the manually-set pd_shoot_status
    # (see SHOOT_STATUS_OPTIONS' docstring for why — no parseable shoot
    # date exists anywhere in this schema).
    # The IST calendar day (not the UTC day) — see ist_day_bounds_utc.
    today_start, today_end = ist_day_bounds_utc()

    locked_talent_ids = [c["talent_id"] for c in locked]
    tasks = await _tasks_for_project(pid, locked_talent_ids)
    task_talent_ids = [t.get("talent_id") for t in tasks if t.get("talent_id")]
    task_talent_names: Dict[str, str] = {}
    if task_talent_ids:
        tt_docs = await db.talents.find({"id": {"$in": task_talent_ids}}, {"_id": 0, "id": 1, "name": 1}).to_list(len(task_talent_ids))
        task_talent_names = {d["id"]: d.get("name", "") for d in tt_docs}
    for t in tasks:
        t["talent_name"] = task_talent_names.get(t.get("talent_id"))
    task_buckets = _bucket_tasks(tasks, today_start, today_end)

    trials_today = [c for c in locked if c["costume_trial_at"] and today_start <= c["costume_trial_at"] < today_end]
    trials_upcoming = [c for c in locked if c["costume_trial_at"] and c["costume_trial_at"] >= today_end]
    shoots_today = [c for c in locked if c["shoot_status"] == "today"]
    shoots_upcoming = [c for c in locked if c["shoot_status"] == "scheduled"]
    project_shoot_today = project.get("pd_shoot_status") == "today"

    # V2 polish (spec sections 26-34) — Overview dashboard. Every item below
    # is DERIVED from data already computed above (locked talents' own
    # pd_shoot_days / pd_readings_rehearsals, reimbursements, tranches,
    # tasks) — no new collection, no second reminder engine, purely a
    # presentation-layer read of what already exists.
    today_date_str = ist_today().isoformat()
    shoot_days_today: List[dict] = []
    shoot_days_upcoming: List[dict] = []
    prep_events_today: List[dict] = []
    prep_events_upcoming: List[dict] = []
    for c in locked:
        for d in c.get("shoot_days") or []:
            ds = d.get("date")
            if not ds:
                continue
            entry = {
                "talent_id": c["talent_id"], "talent_name": c["name"], "date": ds,
                "call_time": d.get("call_time"), "location": d.get("location"),
                "status": d.get("shoot_status"),
            }
            if ds == today_date_str:
                shoot_days_today.append(entry)
            elif ds > today_date_str:
                shoot_days_upcoming.append(entry)
        for rr in c.get("readings_rehearsals") or []:
            ds = rr.get("date")
            if not ds:
                continue
            entry = {
                "talent_id": c["talent_id"], "talent_name": c["name"], "type": rr.get("type"),
                "date": ds, "time": rr.get("time"), "location": rr.get("location"),
            }
            if ds == today_date_str:
                prep_events_today.append(entry)
            elif ds > today_date_str:
                prep_events_upcoming.append(entry)
    shoot_days_upcoming.sort(key=lambda x: x["date"])
    prep_events_upcoming.sort(key=lambda x: x["date"])

    next_follow_up = project.get("pd_next_follow_up_at")
    followup_status = project.get("pd_payment_followup_status") or "not_due"
    payment_followup_due_today = bool(
        next_follow_up and followup_status != "done" and today_start <= next_follow_up < today_end
    )
    payment_followup_overdue = bool(
        next_follow_up and followup_status != "done" and next_follow_up < today_start
    )
    payment_followup_upcoming = bool(
        next_follow_up and followup_status != "done" and next_follow_up >= today_end
    )

    needs_attention: List[str] = []
    if not project.get("pd_confirmation_mail_received"):
        needs_attention.append("Confirmation mail pending")
    if not project.get("pd_invoice_raised"):
        needs_attention.append("Invoice not raised")
    elif not project.get("pd_invoice_sent"):
        # Only surface "not sent" once "raised" is already true — an
        # invoice that hasn't been raised yet obviously hasn't been sent
        # either; showing both would just be noise.
        needs_attention.append("Invoice not sent")
    if not project.get("pd_payment_in_received"):
        needs_attention.append("Client payment pending")
    if not project.get("pd_gst_component_received"):
        needs_attention.append("GST component pending")
    pending_talent_payments = len(locked) - cleared
    if pending_talent_payments > 0:
        needs_attention.append(f"{pending_talent_payments} talent payment{'s' if pending_talent_payments != 1 else ''} pending")
    if not any(m.get("category") == "call_sheet" for m in documents):
        needs_attention.append("Call sheet missing")
    reimbursements_pending = sum(1 for r in reimbursements if r.get("status") == "pending")
    if reimbursements_pending:
        needs_attention.append(f"{reimbursements_pending} reimbursement{'s' if reimbursements_pending != 1 else ''} pending")
    missing_bills = sum(1 for r in reimbursements if not r.get("material_id"))
    if missing_bills:
        needs_attention.append(f"{missing_bills} reimbursement bill{'s' if missing_bills != 1 else ''} missing")
    if task_buckets["overdue"]:
        n = len(task_buckets["overdue"])
        needs_attention.append(f"{n} task{'s' if n != 1 else ''} overdue")
    if payment_followup_overdue:
        needs_attention.append("Payment follow-up overdue")
    elif payment_followup_due_today:
        needs_attention.append("Payment follow-up due today")
    # The Talent Shooting Schedule is the source of truth for shoot dates/times/places, so the
    # project-level "Shoot Info" defaults no longer have to be filled in once every locked talent
    # has at least one fully specified day (date + call time + location).
    talent_schedules_complete = bool(locked) and all(
        any(d.get("date") and d.get("call_time") and d.get("location") for d in (c.get("shoot_days") or []))
        for c in locked
    )
    if locked and not (project.get("pd_shoot_location") or project.get("pd_call_time") or talent_schedules_complete):
        needs_attention.append("Shoot details incomplete")
    # Financial completeness — flagged, never guessed. A talent whose agreed rate is entered but
    # whose production quote is not has no spread/earning yet (legacy rows land here until completed).
    unquoted = [c["name"] for c in locked if c.get("production_quote") is None and c.get("talent_agreed_rate") is not None]
    if unquoted:
        needs_attention.append(f"Production quote missing for {len(unquoted)} talent{'s' if len(unquoted) != 1 else ''}")
    below_cost = [c["name"] for c in locked if c.get("spread") is not None and c["spread"] < 0]
    if below_cost:
        needs_attention.append(f"Production quote below talent rate: {', '.join(below_cost)}")
    # Only nags when the admin has explicitly marked it pending — an unset
    # (None) agreement_status never surfaces here, so old projects that
    # never touched this new field aren't retroactively flagged.
    if project.get("pd_agreement_status") == "pending":
        needs_attention.append("Agreement not signed")

    # V2 polish (spec section 32) — "recently completed", using each
    # record's own updated_at (already set by every status-changing PATCH
    # in this file / routers/workflow.py) as the recency signal. Capped and
    # sorted newest-first; nothing here is a new timestamp field.
    completed_reimbursements = sorted(
        (r for r in reimbursements if r.get("status") == "paid" and r.get("updated_at")),
        key=lambda r: r["updated_at"], reverse=True,
    )[:5]
    completed_tranches = sorted(
        (t for t in tranches if t.get("payment_status") == "received" and t.get("updated_at")),
        key=lambda t: t["updated_at"], reverse=True,
    )[:5]
    completed_tasks = sorted(
        (t for t in tasks if t.get("status") == "completed" and t.get("updated_at")),
        key=lambda t: t["updated_at"], reverse=True,
    )[:5]

    return {
        "project": {
            "id": project["id"],
            "brand_name": project.get("brand_name"),
            "status": project.get("status"),
            "commission_percent": project.get("commission_percent"),
            "shoot_dates": project.get("shoot_dates"),
            # Phase G — the structured, reminder-only date (see
            # ProductionDeskProjectPatch.shoot_date above). Independent of
            # shoot_dates; None is the normal case for a multi-day/range shoot.
            "pd_shoot_date": project.get("pd_shoot_date"),
            "medium_usage": project.get("medium_usage"),
            "director": project.get("director"),
            "production_house": project.get("production_house"),
            "additional_details": project.get("additional_details"),
            "competitive_brand_enabled": project.get("competitive_brand_enabled", False),
            # Production Desk's own additive fields — see module docstring.
            "pd_production_budget_per_day": _num(project.get("pd_production_budget_per_day")),
            "pd_production_budget_total": _num(project.get("pd_production_budget_total")),
            "pd_shooting_days": project.get("pd_shooting_days"),
            "pd_confirmation_mail_received": bool(project.get("pd_confirmation_mail_received")),
            "pd_invoice_raised": bool(project.get("pd_invoice_raised")),
            "pd_invoice_sent": bool(project.get("pd_invoice_sent")),
            "pd_payment_in_received": bool(project.get("pd_payment_in_received")),
            "pd_gst_component_received": bool(project.get("pd_gst_component_received")),
            # Post-lock operational stage — see PRODUCTION_STATUS_OPTIONS.
            # Purely informational; does NOT replace or gate the existing
            # project.status field (ongoing/hold/complete/locked), which
            # remains the overall project-list grouping shown elsewhere.
            "pd_production_status": project.get("pd_production_status") or "not_started",
            "pd_call_time": project.get("pd_call_time"),
            "pd_shoot_location": project.get("pd_shoot_location"),
            "pd_shoot_notes": project.get("pd_shoot_notes"),
            "pd_production_contact": _serialise_client_ref(project.get("pd_production_contact_client_id"), name_map),
            # Pre-existing Project Details fields, read-only here — see
            # module docstring for why these are deliberately NOT merged
            # into the budget/commission math below.
            "client_budget_lines": project.get("client_budget") or [],
            "talent_budget_lines": project.get("talent_budget") or [],
            # Shoot Management (Phase 2) — additive project-level fields.
            "pd_reporting_time": project.get("pd_reporting_time"),
            "pd_shoot_status": project.get("pd_shoot_status") or "not_scheduled",
            # Payment Follow-up Management (Phase 2) — operational
            # follow-up tracking ONLY, not a Finance/accounting record.
            # pd_payment_in_received (existing) stays the one boolean
            # "has it actually arrived" field; these are the working
            # notes a manager keeps while chasing it.
            "pd_payment_terms": project.get("pd_payment_terms"),
            "pd_expected_payment_date": project.get("pd_expected_payment_date"),
            "pd_last_follow_up_at": project.get("pd_last_follow_up_at"),
            "pd_next_follow_up_at": next_follow_up,
            "pd_payment_followup_status": followup_status,
            "pd_payment_followup_notes": project.get("pd_payment_followup_notes"),
            # V2 — Agreement Signed (spec section 14). None (unset) is
            # rendered by the frontend the same as "pending" for display,
            # but never auto-flagged in needs_attention (see above) so an
            # old project isn't retroactively nagged.
            "pd_agreement_status": project.get("pd_agreement_status"),
            # V2 — combined checklist convenience: true only when BOTH
            # underlying fields (kept alive for Management Agent's existing
            # invoice-status NLU/digest commands) are true.
            "pd_invoice_raised_and_sent": bool(project.get("pd_invoice_raised")) and bool(project.get("pd_invoice_sent")),
        },
        "finance": {
            # Honest, static Case-B state — see module docstring. Never
            # flipped to "synced" by any code path in this repo.
            "zoho_status": ZOHO_STATUS,
        },
        # What this deployment can do. The UI only offers Google Maps place search when the server
        # actually has a key, so an unconfigured environment never makes (or logs) a failing request.
        "capabilities": {"places_search": bool((os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip())},
        "locked_talents": locked,
        "summary": {
            "locked_count": len(locked),
            "shoot_days": project.get("pd_shooting_days"),
            "talent_budget_total": talent_budget_total,
            "extra_hours_total": round(extra_hours_total_all, 2),
            "reimbursements_total": reimbursements_total_all,
            "production_budget_total": _num(project.get("pd_production_budget_total")),
            # V2 (spec section 21) — auto-aggregated, never manually typed.
            # The pre-existing pd_production_budget_total (manual line
            # above) is left untouched/still editable; this is a SEPARATE,
            # additive, fully-derived total so nothing prior silently
            # changes meaning.
            "total_talent_and_overtime_and_reimbursements": round(talent_budget_total + extra_hours_total_all + reimbursements_total_all, 2),
            "commission_gross": round(commission_gross, 2),
            "kickbacks_total": round(kickbacks_total, 2),
            "commission_net": round(commission_net, 2),
            "payments_cleared": cleared,
            "payments_total": len(locked),
            "payments_pending_amount": round(pending_amount, 2),
            "tranches_total": tranches_total,
            "tranches_received_total": tranches_received_total,
            **_financial_rollup(
                locked, project, extra_hours_total=extra_hours_total_all, reimbursements_total=reimbursements_total_all,
                kickbacks_total=kickbacks_total, commission_gross=commission_gross,
                tranches=tranches, tranches_received_total=tranches_received_total,
            ),
        },
        "needs_attention": needs_attention,
        "kickbacks": kickbacks,
        "reimbursements": reimbursements,
        "reimbursement_checklist_status": reimbursement_checklist_status,
        "tranches": tranches,
        "crew": crew,
        "documents": documents,
        "tasks": {
            "all": tasks,
            "due_today": task_buckets["due_today"],
            "overdue": task_buckets["overdue"],
            "upcoming": task_buckets["upcoming"],
            "pending": task_buckets["pending"],
        },
        "today": {
            "tasks": task_buckets["due_today"],
            "trials": trials_today,
            "shoots": shoots_today,
            "shoot_days": shoot_days_today,
            "prep_events": prep_events_today,
            "project_shoot_today": project_shoot_today,
            "payment_followup_due": payment_followup_due_today,
        },
        "upcoming": {
            "tasks": task_buckets["upcoming"],
            "trials": trials_upcoming,
            "shoots": shoots_upcoming,
            "shoot_days": shoot_days_upcoming,
            "prep_events": prep_events_upcoming,
            "payment_followup": payment_followup_upcoming,
        },
        "completed": {
            "reimbursements": completed_reimbursements,
            "tranches": completed_tranches,
            "tasks": completed_tasks,
        },
    }


# ---------------------------------------------------------------------------
# PATCH /projects/{pid}/production-desk — project-level PD fields
# ---------------------------------------------------------------------------
class ProductionDeskProjectPatch(BaseModel):
    production_budget_per_day: Optional[float] = None
    production_budget_total: Optional[float] = None
    shooting_days: Optional[int] = None
    confirmation_mail_received: Optional[bool] = None
    invoice_raised: Optional[bool] = None
    invoice_sent: Optional[bool] = None
    payment_in_received: Optional[bool] = None
    gst_component_received: Optional[bool] = None
    call_time: Optional[str] = None
    shoot_location: Optional[str] = None
    shoot_notes: Optional[str] = None
    production_contact_client_id: Optional[str] = None
    production_status: Optional[str] = None
    # Shoot Management (Phase 2)
    reporting_time: Optional[str] = None
    shoot_status: Optional[str] = None
    # Reuses the PRE-EXISTING project.shoot_dates field (shown elsewhere —
    # submission forms, the public client link — as free text like
    # "24th - 30th August (ANY ONE DAY)") rather than adding a second,
    # pd_-prefixed shoot-date field. Was read-only in Production Desk
    # before Phase 3 (Management Agent NLU pass) added a write path here
    # (and a matching inline-edit in the UI) for read/write parity.
    shoot_dates: Optional[str] = None
    # Phase G (Production Reminders) — a genuinely NEW, small, additive
    # field: project.shoot_dates above is deliberately free text (see its
    # own comment — "24th - 30th August (ANY ONE DAY)" isn't reliably
    # parseable, and this codebase's convention is never to guess). The
    # reminder worker needs ONE unambiguous calendar date to compute
    # "day before" / "morning of" windows, so this is a separate,
    # explicitly-optional single ISO date (YYYY-MM-DD) that only powers
    # shoot reminder scheduling — it never overwrites, and is never
    # derived by guessing at, the free-text shoot_dates display field.
    # Unset (the common case for a multi-day/range shoot) simply means no
    # shoot reminder fires for this project — an honest degrade, not a
    # guess. See services/production_reminder_worker.py.
    shoot_date: Optional[str] = None
    # Payment Follow-up Management (Phase 2) — operational tracking only,
    # not a Finance record. See module docstring.
    payment_terms: Optional[str] = None
    expected_payment_date: Optional[str] = None
    last_follow_up_at: Optional[str] = None
    next_follow_up_at: Optional[str] = None
    payment_followup_status: Optional[str] = None
    payment_followup_notes: Optional[str] = None
    # V2 — Agreement Signed (spec section 14). Explicit three-way so a
    # project genuinely without an agreement step can say so honestly.
    agreement_status: Optional[str] = None
    # REMOVED (2026-10-07): the project-level structured date list duplicated each talent's own
    # shoot-day schedule, which is now the single source of truth for shoot dates. Still declared
    # so a stale client gets a clear 400 instead of a silently ignored write.
    shoot_dates_list: Optional[List[str]] = None
    # V2 — convenience alias so the frontend's single "Invoice Raised &
    # Sent" checklist toggle can write both underlying fields (still kept
    # alive for Management Agent — see module docstring) in one call
    # instead of two separate ones that could race/partially apply.
    invoice_raised_and_sent: Optional[bool] = None


@router.patch("/{pid}/production-desk")
async def update_production_desk_project(pid: str, payload: ProductionDeskProjectPatch, admin: dict = Depends(current_team_or_admin)):
    project = await _get_project_or_404(pid)
    if payload.production_status is not None and payload.production_status not in PRODUCTION_STATUS_OPTIONS:
        raise HTTPException(400, f"production_status must be one of {PRODUCTION_STATUS_OPTIONS}")
    if payload.shoot_status is not None and payload.shoot_status not in SHOOT_STATUS_OPTIONS:
        raise HTTPException(400, f"shoot_status must be one of {SHOOT_STATUS_OPTIONS}")
    if payload.payment_followup_status is not None and payload.payment_followup_status not in PAYMENT_FOLLOWUP_STATUSES:
        raise HTTPException(400, f"payment_followup_status must be one of {PAYMENT_FOLLOWUP_STATUSES}")
    if payload.agreement_status is not None and payload.agreement_status not in AGREEMENT_STATUS_OPTIONS:
        raise HTTPException(400, f"agreement_status must be one of {AGREEMENT_STATUS_OPTIONS}")
    if payload.shoot_date is not None and payload.shoot_date != "":
        try:
            date.fromisoformat(payload.shoot_date)
        except ValueError:
            raise HTTPException(400, "shoot_date must be an ISO date (YYYY-MM-DD)")
    if payload.shoot_dates_list is not None:
        raise HTTPException(400, "Project-level shoot dates were removed — schedule dates per talent in the Talent Shooting Schedule")
    field_map = {
        "production_budget_per_day": "pd_production_budget_per_day",
        "production_budget_total": "pd_production_budget_total",
        "shooting_days": "pd_shooting_days",
        "confirmation_mail_received": "pd_confirmation_mail_received",
        "invoice_raised": "pd_invoice_raised",
        "invoice_sent": "pd_invoice_sent",
        "payment_in_received": "pd_payment_in_received",
        "gst_component_received": "pd_gst_component_received",
        "call_time": "pd_call_time",
        "shoot_location": "pd_shoot_location",
        "shoot_notes": "pd_shoot_notes",
        "production_contact_client_id": "pd_production_contact_client_id",
        "production_status": "pd_production_status",
        "reporting_time": "pd_reporting_time",
        "shoot_status": "pd_shoot_status",
        "payment_terms": "pd_payment_terms",
        "expected_payment_date": "pd_expected_payment_date",
        "last_follow_up_at": "pd_last_follow_up_at",
        "next_follow_up_at": "pd_next_follow_up_at",
        "payment_followup_status": "pd_payment_followup_status",
        "payment_followup_notes": "pd_payment_followup_notes",
        # Identity mapping — the existing project.shoot_dates field, not a
        # new pd_* field. See ProductionDeskProjectPatch.shoot_dates above.
        "shoot_dates": "shoot_dates",
        "shoot_date": "pd_shoot_date",
        "agreement_status": "pd_agreement_status",
    }
    payload_dict = payload.model_dump(exclude_unset=True)
    # invoice_raised_and_sent is a write-only alias — it maps to BOTH
    # existing fields at once, never stored under its own key (the
    # computed pd_invoice_raised_and_sent read in get_production_desk
    # derives from them fresh every time, so there's nothing to desync).
    invoice_alias = payload_dict.pop("invoice_raised_and_sent", None)
    updates = {field_map[k]: v for k, v in payload_dict.items()}
    if invoice_alias is not None:
        updates["pd_invoice_raised"] = invoice_alias
        updates["pd_invoice_sent"] = invoice_alias
    # A shoot_date CHANGE invalidates any previously-sent shoot reminders —
    # see services/production_reminder_worker.py's idempotency design
    # (each reminder kind is only "sent for" a specific date value; clearing
    # these here means a rescheduled date is immediately eligible again,
    # and the OLD date's reminders can never re-fire since the worker only
    # ever compares against the CURRENT pd_shoot_date).
    if "pd_shoot_date" in updates:
        updates["pd_shoot_reminder_day_before_sent_for"] = None
        updates["pd_shoot_reminder_morning_of_sent_for"] = None
    if updates:
        updates["updated_at"] = _now()
        await db.projects.update_one({"id": pid}, {"$set": updates})

        # Notify the team via the EXISTING admin-notification fan-out —
        # only on the pending -> true TRANSITION, not on every save, and
        # only for these high-signal financial state changes.
        brand = project.get("brand_name") or "Project"
        if updates.get("pd_payment_in_received") is True and not project.get("pd_payment_in_received"):
            await notify_fanout(
                db, type="production_desk_payment_in_received",
                title=f"Client payment received — {brand}",
                body="Payment In marked received on Production Desk.",
                payload={"project_id": pid}, actor_id=admin.get("id"),
            )
        if updates.get("pd_invoice_sent") is True and not project.get("pd_invoice_sent"):
            await notify_fanout(
                db, type="production_desk_invoice_sent",
                title=f"Invoice sent — {brand}",
                body="Invoice marked sent on Production Desk.",
                payload={"project_id": pid}, actor_id=admin.get("id"),
            )
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# PATCH /projects/{pid}/production-desk/talents/{talent_id}
# ---------------------------------------------------------------------------
class TalentProductionPatch(BaseModel):
    budget_per_day: Optional[float] = None
    budget_total: Optional[float] = None
    # What the PRODUCTION is quoted for this talent (internal; the talent never sees it).
    # Independent of budget_total, which is the talent-facing agreed rate. null clears it.
    production_quote: Optional[float] = Field(None, ge=0)
    shooting_days: Optional[int] = None
    commission_percent: Optional[float] = None
    payment_status: Optional[str] = None
    # Talent Preparation (Phase 2)
    costume_trial_at: Optional[str] = None
    # V2 polish (spec section 2) — free-text time, matching the exact
    # pattern pd_call_time/pd_reporting_time already use (never real time
    # parsing anywhere in this schema).
    costume_trial_time: Optional[str] = None
    costume_trial_location: Optional[str] = None
    # V2 — lightest-possible "clickable location" (spec section 7): an
    # optional Google Maps URL alongside the existing plain-text location,
    # not a new maps integration. costume_trial_location's existing type
    # (a plain string) is untouched, so every old reader keeps working.
    costume_trial_map_url: Optional[str] = None
    fitting_status: Optional[str] = None
    look_test_status: Optional[str] = None
    grooming_requirements: Optional[str] = None
    special_instructions: Optional[str] = None
    shoot_status: Optional[str] = None


@router.patch("/{pid}/production-desk/talents/{talent_id}")
async def update_locked_talent_production(pid: str, talent_id: str, payload: TalentProductionPatch, admin: dict = Depends(current_team_or_admin)):
    row = await _get_locked_pipeline_row(pid, talent_id)
    if payload.payment_status is not None and payload.payment_status not in PAYMENT_STATUSES:
        raise HTTPException(400, f"payment_status must be one of {sorted(PAYMENT_STATUSES)}")
    if payload.fitting_status is not None and payload.fitting_status not in TRIAL_STATUS_OPTIONS:
        raise HTTPException(400, f"fitting_status must be one of {TRIAL_STATUS_OPTIONS}")
    if payload.look_test_status is not None and payload.look_test_status not in TRIAL_STATUS_OPTIONS:
        raise HTTPException(400, f"look_test_status must be one of {TRIAL_STATUS_OPTIONS}")
    if payload.shoot_status is not None and payload.shoot_status not in SHOOT_STATUS_OPTIONS:
        raise HTTPException(400, f"shoot_status must be one of {SHOOT_STATUS_OPTIONS}")

    field_map = {
        "budget_per_day": "pd_budget_per_day",
        "budget_total": "pd_budget_total",
        "production_quote": "pd_production_quote",
        "shooting_days": "pd_shooting_days",
        "commission_percent": "pd_commission_percent",
        "payment_status": "pd_payment_status",
        "costume_trial_at": "pd_costume_trial_at",
        "costume_trial_time": "pd_costume_trial_time",
        "costume_trial_location": "pd_costume_trial_location",
        "costume_trial_map_url": "pd_costume_trial_map_url",
        "fitting_status": "pd_fitting_status",
        "look_test_status": "pd_look_test_status",
        "grooming_requirements": "pd_grooming_requirements",
        "special_instructions": "pd_special_instructions",
        "shoot_status": "pd_shoot_status",
    }
    updates = {field_map[k]: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if updates:
        updates["updated_at"] = _now()
        await db.casting_pipeline.update_one({"project_id": pid, "talent_id": talent_id}, {"$set": updates})

        # Notify the team via the EXISTING admin-notification fan-out —
        # only on the pending -> cleared TRANSITION, not on every save.
        was_cleared = (row.get("pd_payment_status") or "pending") == "cleared"
        if updates.get("pd_payment_status") == "cleared" and not was_cleared:
            talent = await db.talents.find_one({"id": talent_id}, {"_id": 0, "name": 1})
            project = await db.projects.find_one({"id": pid}, {"_id": 0, "brand_name": 1})
            await notify_fanout(
                db,
                type="production_desk_payment_cleared",
                title=f"Talent payment cleared — {(talent or {}).get('name') or 'Talent'}",
                body=f"{(project or {}).get('brand_name') or 'Project'} — payment marked cleared on Production Desk.",
                payload={"project_id": pid, "talent_id": talent_id},
                actor_id=admin.get("id"),
            )
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# V2 — Per-talent shoot-day schedule (spec sections 1-4)
#
# Stored as a small array (`pd_shoot_days`) on the SAME locked
# casting_pipeline row every other pd_* talent field already lives on —
# not a new collection. Whole-array read/modify/write (fetch the row,
# mutate the list in Python, $set the array back) rather than positional
# Mongo array operators: a talent has at most a handful of shoot days, so
# this stays simple and easy to reason about, matching this module's own
# "don't over-engineer" convention elsewhere (e.g. kickbacks/reimbursements
# already use plain collections rather than aggregation pipelines).
# ---------------------------------------------------------------------------
class ShootDayIn(BaseModel):
    date: str = Field(..., description="ISO date YYYY-MM-DD (an IST calendar date)")
    call_time: Optional[str] = None
    reporting_time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    # A place picked through the Google Maps search (see /places/* below). `location` stays the
    # display name; these keep the real place so the schedule shows — and links to — the actual
    # selected location instead of a typed string.
    location_address: Optional[str] = None
    location_place_id: Optional[str] = None
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    notes: Optional[str] = None
    agreed_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    shoot_status: Optional[str] = None


class ShootDayUpdateIn(BaseModel):
    date: Optional[str] = None
    call_time: Optional[str] = None
    reporting_time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    # A place picked through the Google Maps search (see /places/* below). `location` stays the
    # display name; these keep the real place so the schedule shows — and links to — the actual
    # selected location instead of a typed string.
    location_address: Optional[str] = None
    location_place_id: Optional[str] = None
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    notes: Optional[str] = None
    agreed_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    shoot_status: Optional[str] = None


def _validate_shoot_day_date(raw: str):
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "date must be an ISO date (YYYY-MM-DD)")


async def _resync_talent_shooting_days(pid: str, talent_id: str, shoot_days: List[dict]):
    """Keeps pd_shooting_days (the plain count, used by the base-fee
    per-day × days calc) equal to len(pd_shoot_days) whenever the
    structured schedule is in use — see _talent_card's docstring."""
    await db.casting_pipeline.update_one(
        {"project_id": pid, "talent_id": talent_id},
        {"$set": {"pd_shoot_days": shoot_days, "pd_shooting_days": len(shoot_days), "updated_at": _now()}},
    )


@router.post("/{pid}/production-desk/talents/{talent_id}/shoot-days")
async def add_talent_shoot_day(pid: str, talent_id: str, payload: ShootDayIn, admin: dict = Depends(current_team_or_admin)):
    row = await _get_locked_pipeline_row(pid, talent_id)
    _validate_shoot_day_date(payload.date)
    if payload.shoot_status is not None and payload.shoot_status not in SHOOT_STATUS_OPTIONS:
        raise HTTPException(400, f"shoot_status must be one of {SHOOT_STATUS_OPTIONS}")
    call_time = normalize_clock(payload.call_time, "call_time")
    reporting_time = normalize_clock(payload.reporting_time, "reporting_time")
    validate_reporting_before_call(reporting_time, call_time)
    shoot_days = list(row.get("pd_shoot_days") or [])
    shoot_days.append({
        "id": str(uuid.uuid4()),
        "date": payload.date,
        "call_time": call_time,
        "reporting_time": reporting_time,
        "location": payload.location,
        "location_map_url": payload.location_map_url or build_maps_url(
            payload.location_place_id, payload.location_lat, payload.location_lng, payload.location_address,
        ),
        "location_address": payload.location_address,
        "location_place_id": payload.location_place_id,
        "location_lat": payload.location_lat,
        "location_lng": payload.location_lng,
        "notes": payload.notes,
        "agreed_hours": payload.agreed_hours,
        "actual_hours": payload.actual_hours,
        "shoot_status": payload.shoot_status or "scheduled",
    })
    shoot_days.sort(key=lambda d: d.get("date") or "")
    await _resync_talent_shooting_days(pid, talent_id, shoot_days)
    return await get_production_desk(pid, admin)


@router.patch("/{pid}/production-desk/talents/{talent_id}/shoot-days/{day_id}")
async def update_talent_shoot_day(pid: str, talent_id: str, day_id: str, payload: ShootDayUpdateIn, admin: dict = Depends(current_team_or_admin)):
    row = await _get_locked_pipeline_row(pid, talent_id)
    if payload.date is not None:
        _validate_shoot_day_date(payload.date)
    if payload.shoot_status is not None and payload.shoot_status not in SHOOT_STATUS_OPTIONS:
        raise HTTPException(400, f"shoot_status must be one of {SHOOT_STATUS_OPTIONS}")
    shoot_days = list(row.get("pd_shoot_days") or [])
    changes = payload.model_dump(exclude_unset=True)
    if "call_time" in changes:
        changes["call_time"] = normalize_clock(changes["call_time"], "call_time")
    if "reporting_time" in changes:
        changes["reporting_time"] = normalize_clock(changes["reporting_time"], "reporting_time")
    found = False
    for d in shoot_days:
        if d.get("id") == day_id:
            validate_reporting_before_call(
                changes.get("reporting_time", d.get("reporting_time")), changes.get("call_time", d.get("call_time")),
            )
            place_keys = {"location_address", "location_place_id", "location_lat", "location_lng"}
            # Typing a different location over a previously selected Google place must not leave
            # the OLD place's address/coordinates behind on the new text.
            if "location" in changes and changes["location"] != d.get("location") and not (place_keys & changes.keys()):
                for k in place_keys:
                    d[k] = None
                if "location_map_url" not in changes:
                    d["location_map_url"] = None
            d.update(changes)
            if d.get("location_place_id") or d.get("location_lat") is not None:
                d["location_map_url"] = d.get("location_map_url") or build_maps_url(
                    d.get("location_place_id"), d.get("location_lat"), d.get("location_lng"), d.get("location_address"),
                )
            found = True
            break
    if not found:
        raise HTTPException(404, "Shoot day not found")
    shoot_days.sort(key=lambda d: d.get("date") or "")
    await _resync_talent_shooting_days(pid, talent_id, shoot_days)
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/talents/{talent_id}/shoot-days/{day_id}")
async def delete_talent_shoot_day(pid: str, talent_id: str, day_id: str, admin: dict = Depends(current_team_or_admin)):
    row = await _get_locked_pipeline_row(pid, talent_id)
    shoot_days = [d for d in (row.get("pd_shoot_days") or []) if d.get("id") != day_id]
    if len(shoot_days) == len(row.get("pd_shoot_days") or []):
        raise HTTPException(404, "Shoot day not found")
    # Deliberately NOT resynced via _resync_talent_shooting_days when the
    # list becomes empty — that would silently blank out pd_shooting_days
    # for a talent falling back to the old manual count. Only keep the
    # count in sync while the structured schedule is still in use.
    if shoot_days:
        await _resync_talent_shooting_days(pid, talent_id, shoot_days)
    else:
        await db.casting_pipeline.update_one(
            {"project_id": pid, "talent_id": talent_id},
            {"$set": {"pd_shoot_days": [], "updated_at": _now()}},
        )
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# V2 — Readings & Rehearsals (spec section 8)
# Same whole-array-on-the-locked-row pattern as shoot-days above.
# ---------------------------------------------------------------------------
class ReadingRehearsalIn(BaseModel):
    type: str
    date: Optional[str] = None
    time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    notes: Optional[str] = None


class ReadingRehearsalUpdateIn(BaseModel):
    type: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{pid}/production-desk/talents/{talent_id}/readings-rehearsals")
async def add_reading_rehearsal(pid: str, talent_id: str, payload: ReadingRehearsalIn, admin: dict = Depends(current_team_or_admin)):
    if payload.type not in READING_REHEARSAL_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(READING_REHEARSAL_TYPES)}")
    row = await _get_locked_pipeline_row(pid, talent_id)
    entries = list(row.get("pd_readings_rehearsals") or [])
    entries.append({
        "id": str(uuid.uuid4()), "type": payload.type, "date": payload.date, "time": payload.time,
        "location": payload.location, "location_map_url": payload.location_map_url, "notes": payload.notes,
    })
    await db.casting_pipeline.update_one(
        {"project_id": pid, "talent_id": talent_id},
        {"$set": {"pd_readings_rehearsals": entries, "updated_at": _now()}},
    )
    return await get_production_desk(pid, admin)


@router.patch("/{pid}/production-desk/talents/{talent_id}/readings-rehearsals/{entry_id}")
async def update_reading_rehearsal(pid: str, talent_id: str, entry_id: str, payload: ReadingRehearsalUpdateIn, admin: dict = Depends(current_team_or_admin)):
    if payload.type is not None and payload.type not in READING_REHEARSAL_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(READING_REHEARSAL_TYPES)}")
    row = await _get_locked_pipeline_row(pid, talent_id)
    entries = list(row.get("pd_readings_rehearsals") or [])
    changes = payload.model_dump(exclude_unset=True)
    found = False
    for e in entries:
        if e.get("id") == entry_id:
            e.update(changes)
            found = True
            break
    if not found:
        raise HTTPException(404, "Entry not found")
    await db.casting_pipeline.update_one(
        {"project_id": pid, "talent_id": talent_id},
        {"$set": {"pd_readings_rehearsals": entries, "updated_at": _now()}},
    )
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/talents/{talent_id}/readings-rehearsals/{entry_id}")
async def delete_reading_rehearsal(pid: str, talent_id: str, entry_id: str, admin: dict = Depends(current_team_or_admin)):
    row = await _get_locked_pipeline_row(pid, talent_id)
    entries = [e for e in (row.get("pd_readings_rehearsals") or []) if e.get("id") != entry_id]
    if len(entries) == len(row.get("pd_readings_rehearsals") or []):
        raise HTTPException(404, "Entry not found")
    await db.casting_pipeline.update_one(
        {"project_id": pid, "talent_id": talent_id},
        {"$set": {"pd_readings_rehearsals": entries, "updated_at": _now()}},
    )
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# Kickbacks
# ---------------------------------------------------------------------------
class KickbackIn(BaseModel):
    amount: float = Field(..., gt=0)
    recipient_client_id: Optional[str] = None
    recipient_name: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{pid}/production-desk/kickbacks")
async def add_kickback(pid: str, payload: KickbackIn, admin: dict = Depends(current_team_or_admin)):
    await _get_project_or_404(pid)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": pid,
        "amount": payload.amount,
        "recipient_client_id": payload.recipient_client_id,
        "recipient_name": payload.recipient_name,
        "notes": payload.notes,
        "created_at": _now(),
        "created_by": admin.get("email"),
    }
    await db.project_kickbacks.insert_one(doc)
    return await get_production_desk(pid, admin)


class KickbackUpdateIn(BaseModel):
    # Phase H — completes the kickback CRUD surface (create+delete already
    # existed; "Set Rahul's kickback to 5000" needs an update path, which
    # simply didn't exist before). Same collection, same shape — not a
    # second kickback model.
    amount: Optional[float] = Field(None, gt=0)
    recipient_name: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/{pid}/production-desk/kickbacks/{kickback_id}")
async def update_kickback(pid: str, kickback_id: str, payload: KickbackUpdateIn, admin: dict = Depends(current_team_or_admin)):
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if updates:
        updates["updated_at"] = _now()
        res = await db.project_kickbacks.update_one({"id": kickback_id, "project_id": pid}, {"$set": updates})
        if not res.matched_count:
            raise HTTPException(404, "Kickback not found")
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/kickbacks/{kickback_id}")
async def delete_kickback(pid: str, kickback_id: str, admin: dict = Depends(current_admin)):
    res = await db.project_kickbacks.delete_one({"id": kickback_id, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Kickback not found")
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# Reimbursements — bill attachment reuses attach_project_material()
# ---------------------------------------------------------------------------
@router.post("/{pid}/production-desk/reimbursements")
async def add_reimbursement(
    pid: str,
    talent_id: str = Form(...),
    expense_type: str = Form(...),
    amount: float = Form(...),
    date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    admin: dict = Depends(current_team_or_admin),
):
    await _get_project_or_404(pid)
    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0, "id": 1})
    if not talent:
        raise HTTPException(404, "Talent not found")

    material_id = None
    if file is not None:
        from routers.projects import attach_project_material

        data = await file.read()
        updated_project = await attach_project_material(pid, "reimbursement_bill", data, file.filename, file.content_type)
        material_id = updated_project["materials"][-1]["id"]

    doc = {
        "id": str(uuid.uuid4()),
        "project_id": pid,
        "talent_id": talent_id,
        "expense_type": expense_type,
        "amount": amount,
        "date": date,
        "notes": notes,
        "status": "pending",
        "material_id": material_id,
        "created_at": _now(),
        "created_by": admin.get("email"),
    }
    await db.project_reimbursements.insert_one(doc)
    return await get_production_desk(pid, admin)


class ReimbursementStatusPatch(BaseModel):
    status: str


@router.patch("/{pid}/production-desk/reimbursements/{reimbursement_id}")
async def update_reimbursement_status(pid: str, reimbursement_id: str, payload: ReimbursementStatusPatch, admin: dict = Depends(current_team_or_admin)):
    if payload.status not in REIMBURSEMENT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(REIMBURSEMENT_STATUSES)}")
    res = await db.project_reimbursements.update_one(
        {"id": reimbursement_id, "project_id": pid},
        {"$set": {"status": payload.status, "updated_at": _now()}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Reimbursement not found")
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/reimbursements/{reimbursement_id}")
async def delete_reimbursement(pid: str, reimbursement_id: str, admin: dict = Depends(current_admin)):
    res = await db.project_reimbursements.delete_one({"id": reimbursement_id, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Reimbursement not found")
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# Crew — role-tagged reference to an existing CRM client
# ---------------------------------------------------------------------------
class CrewIn(BaseModel):
    client_id: str
    role: str
    status: Optional[str] = "confirmed"


@router.post("/{pid}/production-desk/crew")
async def add_crew(pid: str, payload: CrewIn, admin: dict = Depends(current_team_or_admin)):
    await _get_project_or_404(pid)
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        oid = ObjectId(payload.client_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid client_id")
    client = await db.clients.find_one({"_id": oid}, {"_id": 1})
    if not client:
        raise HTTPException(404, "CRM contact not found")

    doc = {
        "id": str(uuid.uuid4()),
        "project_id": pid,
        "client_id": payload.client_id,
        "role": payload.role,
        "status": payload.status or "confirmed",
        "created_at": _now(),
    }
    await db.project_crew.insert_one(doc)
    return await get_production_desk(pid, admin)


class CrewUpdateIn(BaseModel):
    # Phase H — "Change Amit's role to line producer" needs an update
    # path; create+delete already existed. Same collection/shape.
    role: Optional[str] = None
    status: Optional[str] = None


@router.patch("/{pid}/production-desk/crew/{crew_id}")
async def update_crew(pid: str, crew_id: str, payload: CrewUpdateIn, admin: dict = Depends(current_team_or_admin)):
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if updates:
        updates["updated_at"] = _now()
        res = await db.project_crew.update_one({"id": crew_id, "project_id": pid}, {"$set": updates})
        if not res.matched_count:
            raise HTTPException(404, "Crew member not found")
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/crew/{crew_id}")
async def delete_crew(pid: str, crew_id: str, admin: dict = Depends(current_admin)):
    res = await db.project_crew.delete_one({"id": crew_id, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Crew member not found")
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# V2 — Known Locations (spec section 2: lightweight location search/select).
#
# No Google Places / Maps API integration exists anywhere in this codebase
# and none is added here — that would require new external credentials and
# billing this task cannot introduce. Instead: a cheap, read-only, capped
# aggregation of location names + map URLs the studio has ALREADY typed
# in, across every project's shoot location, every locked talent's costume
# trial location, and every shoot-day / reading-rehearsal location. This
# turns the plain-text location fields into a genuine search/select-from-
# real-places-we've-used combobox (see ProductionDesk.jsx's LocationPicker)
# without inventing geocoding or a maps platform.
# ---------------------------------------------------------------------------
@router.get("/{pid}/production-desk/known-locations")
async def get_known_locations(pid: str, admin: dict = Depends(current_team_or_admin)):
    await _get_project_or_404(pid)
    locations: Dict[str, Optional[str]] = {}

    def _note(name, map_url=None):
        name = (name or "").strip()
        if not name:
            return
        if name not in locations or (map_url and not locations.get(name)):
            locations[name] = map_url or locations.get(name)

    async for proj in db.projects.find({}, {"pd_shoot_location": 1}).limit(1000):
        _note(proj.get("pd_shoot_location"))

    async for row in db.casting_pipeline.find(
        {"stage": "locked"},
        {"pd_costume_trial_location": 1, "pd_costume_trial_map_url": 1, "pd_shoot_days": 1, "pd_readings_rehearsals": 1},
    ).limit(1000):
        _note(row.get("pd_costume_trial_location"), row.get("pd_costume_trial_map_url"))
        for d in (row.get("pd_shoot_days") or []):
            _note(d.get("location"), d.get("location_map_url"))
        for r in (row.get("pd_readings_rehearsals") or []):
            _note(r.get("location"), r.get("location_map_url"))

    result = sorted(
        [{"name": k, "map_url": v} for k, v in locations.items()],
        key=lambda x: x["name"].lower(),
    )[:100]
    return {"locations": result}


# ---------------------------------------------------------------------------
# Google Maps place search/select (server-side proxy)
#
# The Maps API key lives ONLY in the server environment (GOOGLE_MAPS_API_KEY); the browser
# never sees it — it talks to these two authenticated endpoints, which call Google's Places API
# (New) and return just the fields a shoot location needs. With no key configured they answer
# 503 and the picker silently falls back to previously-used locations + free text, exactly as
# before. Nothing is persisted here: the chosen place is saved on the shoot day by the normal
# shoot-day endpoints (location, address, place id, lat/lng, Maps URL).
# ---------------------------------------------------------------------------
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{10,300}$")
_PLACES_BASE = "https://places.googleapis.com/v1"


def _maps_key() -> str:
    key = (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        raise HTTPException(503, "Google Maps search is not configured")
    return key


@router.get("/{pid}/production-desk/places/search")
async def search_places(
    pid: str, q: str = Query(..., min_length=3, max_length=200), session: Optional[str] = Query(None, max_length=100),
    admin: dict = Depends(current_team_or_admin),
):
    await _get_project_or_404(pid)
    key = _maps_key()
    body: Dict[str, Any] = {"input": q.strip(), "regionCode": "IN"}
    if session:
        body["sessionToken"] = session
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.post(f"{_PLACES_BASE}/places:autocomplete", json=body, headers={"X-Goog-Api-Key": key})
    except httpx.HTTPError:
        raise HTTPException(502, "Google Maps search is unavailable right now")
    if resp.status_code != 200:
        logger.warning("places autocomplete failed status=%s", resp.status_code)
        raise HTTPException(502, "Google Maps search is unavailable right now")
    results = []
    for sug in (resp.json().get("suggestions") or [])[:8]:
        pred = sug.get("placePrediction") or {}
        pid_ = pred.get("placeId")
        if not pid_:
            continue
        fmt = pred.get("structuredFormat") or {}
        results.append({
            "place_id": pid_,
            "name": (fmt.get("mainText") or {}).get("text") or (pred.get("text") or {}).get("text") or "",
            "address": (fmt.get("secondaryText") or {}).get("text") or "",
        })
    return {"results": results}


@router.get("/{pid}/production-desk/places/{place_id}")
async def get_place(
    pid: str, place_id: str, session: Optional[str] = Query(None, max_length=100),
    admin: dict = Depends(current_team_or_admin),
):
    await _get_project_or_404(pid)
    if not _PLACE_ID_RE.match(place_id):
        raise HTTPException(400, "Invalid place id")
    key = _maps_key()
    params = {"sessionToken": session} if session else None
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": "id,displayName,formattedAddress,location,googleMapsUri"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.get(f"{_PLACES_BASE}/places/{place_id}", params=params, headers=headers)
    except httpx.HTTPError:
        raise HTTPException(502, "Google Maps search is unavailable right now")
    if resp.status_code == 404:
        raise HTTPException(404, "Place not found")
    if resp.status_code != 200:
        logger.warning("places details failed status=%s", resp.status_code)
        raise HTTPException(502, "Google Maps search is unavailable right now")
    d = resp.json()
    loc = d.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    address = d.get("formattedAddress") or ""
    return {
        "place_id": d.get("id") or place_id,
        "name": (d.get("displayName") or {}).get("text") or address,
        "address": address,
        "lat": lat,
        "lng": lng,
        "maps_url": d.get("googleMapsUri") or build_maps_url(place_id, lat, lng, address),
    }


# ---------------------------------------------------------------------------
# V2 — Payment Tranches / Billing Milestones (spec sections 19-20)
#
# A genuinely new, small collection — nothing existing models "a project
# gets paid in stages". Deliberately an operational billing TRACKER, not
# an accounting/invoicing engine: no ledger, no journal entries, no GST
# reconciliation (see module docstring's Zoho section for the same
# "honest, not-built" boundary this follows).
# ---------------------------------------------------------------------------
class TrancheIn(BaseModel):
    name: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    trigger: Optional[str] = None
    invoice_status: Optional[str] = "pending"
    invoice_date: Optional[str] = None
    invoice_number: Optional[str] = None
    payment_status: Optional[str] = "pending"
    payment_date: Optional[str] = None
    notes: Optional[str] = None


class TrancheUpdateIn(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = Field(None, gt=0)
    trigger: Optional[str] = None
    invoice_status: Optional[str] = None
    invoice_date: Optional[str] = None
    invoice_number: Optional[str] = None
    payment_status: Optional[str] = None
    payment_date: Optional[str] = None
    notes: Optional[str] = None


def _validate_tranche_statuses(invoice_status, payment_status):
    if invoice_status is not None and invoice_status not in TRANCHE_INVOICE_STATUSES:
        raise HTTPException(400, f"invoice_status must be one of {TRANCHE_INVOICE_STATUSES}")
    if payment_status is not None and payment_status not in TRANCHE_PAYMENT_STATUSES:
        raise HTTPException(400, f"payment_status must be one of {TRANCHE_PAYMENT_STATUSES}")


@router.post("/{pid}/production-desk/tranches")
async def add_tranche(pid: str, payload: TrancheIn, admin: dict = Depends(current_team_or_admin)):
    await _get_project_or_404(pid)
    _validate_tranche_statuses(payload.invoice_status, payload.payment_status)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": pid,
        "name": payload.name,
        "amount": payload.amount,
        "trigger": payload.trigger,
        "invoice_status": payload.invoice_status or "pending",
        "invoice_date": payload.invoice_date,
        "invoice_number": payload.invoice_number,
        "payment_status": payload.payment_status or "pending",
        "payment_date": payload.payment_date,
        "notes": payload.notes,
        "created_at": _now(),
        "created_by": admin.get("email"),
    }
    await db.project_payment_tranches.insert_one(doc)
    return await get_production_desk(pid, admin)


@router.patch("/{pid}/production-desk/tranches/{tranche_id}")
async def update_tranche(pid: str, tranche_id: str, payload: TrancheUpdateIn, admin: dict = Depends(current_team_or_admin)):
    _validate_tranche_statuses(payload.invoice_status, payload.payment_status)
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if updates:
        updates["updated_at"] = _now()
        res = await db.project_payment_tranches.update_one({"id": tranche_id, "project_id": pid}, {"$set": updates})
        if not res.matched_count:
            raise HTTPException(404, "Tranche not found")
    return await get_production_desk(pid, admin)


@router.delete("/{pid}/production-desk/tranches/{tranche_id}")
async def delete_tranche(pid: str, tranche_id: str, admin: dict = Depends(current_admin)):
    res = await db.project_payment_tranches.delete_one({"id": tranche_id, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Tranche not found")
    return await get_production_desk(pid, admin)


# ---------------------------------------------------------------------------
# V2 — WhatsApp one-tap message builders (spec sections 17/18/23/24/25/26)
#
# These do NOT send anything — see module docstring's WhatsApp-agent-wiring
# note and the spec's own "do not create a new WhatsApp sender/worker".
# Both return {phone, message}; the frontend opens the EXACT SAME
# wa.me/<phone>?text=<message> deep link MarketingHub.jsx's own
# handleShare() already uses for one-off admin-triggered messages — a
# manual, admin-reviewed send (the admin still taps Send inside WhatsApp
# themselves), not an automated one. The financial arithmetic lives here
# (Python), once, reusing _talent_card's own already-computed numbers —
# the frontend never re-derives an amount, only renders what this returns.
# ---------------------------------------------------------------------------
def _fmt_inr(amount: Optional[float]) -> str:
    if amount is None:
        return "0"
    n = int(round(amount))
    s = str(abs(n))
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups) + "," + last3
    return ("-" if n < 0 else "") + s


async def _followup_contact_candidates(pid: str, project: dict) -> List[dict]:
    """CRM contacts a payment follow-up may legitimately go to — the EXISTING `clients`
    collection, no second contact list. Source order (and why):

      1. the project's saved Payment Follow-up contact (`pd_production_contact_client_id`)
      2. the project's crew (project_crew rows — role-tagged CRM links: Producer, Production
         Manager, Client, ...) — the only project<->contact association the CRM models
      3. other CRM contacts at the SAME company as any of those (the CRM's own company link,
         `company_id`, falling back to the identical `company_name`) — e.g. the accounts
         person at the production house who is not on the crew list.

    Anyone outside this set is never offered and is rejected by the message endpoint, so a
    follow-up cannot be addressed to an arbitrary contact by passing an id."""
    from bson import ObjectId
    from bson.errors import InvalidId

    sources: Dict[str, dict] = {}

    def _oid(cid: Optional[str]):
        try:
            return ObjectId(cid) if cid else None
        except (InvalidId, TypeError):
            return None

    primary = project.get("pd_production_contact_client_id")
    crew_rows = await db.project_crew.find({"project_id": pid}, {"_id": 0}).sort("created_at", 1).to_list(200)
    ordered_ids: List[tuple] = []
    if primary:
        ordered_ids.append((primary, "production_contact", None))
    for c in crew_rows:
        if c.get("client_id"):
            ordered_ids.append((c["client_id"], "crew", c.get("role")))

    oids = [o for o in (_oid(cid) for cid, _, _ in ordered_ids) if o]
    docs = await db.clients.find({"_id": {"$in": oids}}).to_list(len(oids) or 1) if oids else []
    by_id = {str(d["_id"]): d for d in docs}
    crew_role_by_id = {c["client_id"]: c.get("role") for c in crew_rows if c.get("client_id")}
    for cid, source, role in ordered_ids:
        d = by_id.get(cid)
        if d and cid not in sources:
            # the saved concerned person keeps its source, but still shows its crew role when it has one
            sources[cid] = {"doc": d, "source": source, "role": role or crew_role_by_id.get(cid)}

    company_ids = {str(d.get("company_id")) for d in docs if d.get("company_id")}
    company_names = {(d.get("company_name") or "").strip().lower() for d in docs if (d.get("company_name") or "").strip()}
    if company_ids or company_names:
        ors: List[dict] = []
        if company_ids:
            ors.append({"company_id": {"$in": list(company_ids)}})
        if company_names:
            ors.append({"company_name": {"$in": [d.get("company_name") for d in docs if (d.get("company_name") or "").strip()]}})
        async for d in db.clients.find({"$or": ors}).limit(60):
            cid = str(d["_id"])
            if cid not in sources:
                sources[cid] = {"doc": d, "source": "same_company", "role": None}

    out = []
    for cid, info in sources.items():
        d = info["doc"]
        out.append({
            "client_id": cid,
            "name": d.get("name") or "",
            "phone_number": d.get("phone_number"),
            "has_phone": bool((d.get("phone_number") or "").strip()),
            "company_name": d.get("company_name"),
            "designation": d.get("designation"),
            "contact_type": d.get("contact_type"),
            "role": info["role"],
            "source": info["source"],
            "is_default": cid == primary,
        })
    return out


@router.get("/{pid}/production-desk/payment-followup-contacts")
async def get_payment_followup_contacts(pid: str, admin: dict = Depends(current_team_or_admin)):
    project = await _get_project_or_404(pid)
    return {"contacts": await _followup_contact_candidates(pid, project)}


async def _financial_context(pid: str, project: dict) -> tuple:
    """(locked cards, rollup) for the message builders — the same numbers GET
    /production-desk puts in `summary`, computed from the same helpers."""
    reimbursements = await db.project_reimbursements.find({"project_id": pid}, {"_id": 0}).to_list(500)
    reimb_totals: Dict[str, float] = {}
    for r in reimbursements:
        tid = r.get("talent_id")
        if tid:
            reimb_totals[tid] = reimb_totals.get(tid, 0.0) + (_num(r.get("amount")) or 0.0)
    locked = await _locked_talent_cards(pid, project, reimb_totals)
    kickbacks = await db.project_kickbacks.find({"project_id": pid}, {"_id": 0}).to_list(500)
    tranches = await db.project_payment_tranches.find({"project_id": pid}, {"_id": 0}).to_list(200)
    rollup = _financial_rollup(
        locked, project,
        extra_hours_total=sum(c["extra_hours_total"] for c in locked),
        reimbursements_total=round(sum(_num(r.get("amount")) or 0.0 for r in reimbursements), 2),
        kickbacks_total=sum(_num(k.get("amount")) or 0 for k in kickbacks),
        commission_gross=sum(c["commission_amount"] for c in locked if c["commission_amount"] is not None),
        tranches=tranches,
        tranches_received_total=round(sum(_num(t.get("amount")) or 0.0 for t in tranches if t.get("payment_status") == "received"), 2),
    )
    return locked, rollup


@router.get("/{pid}/production-desk/payment-followup-message")
async def build_payment_followup_message(
    pid: str, contact_id: Optional[str] = Query(None), admin: dict = Depends(current_team_or_admin),
):
    """The follow-up goes to the PRODUCTION, so every amount here is production-side:
    production quote + overtime + reimbursements - amount already received. The talent's agreed
    rate (and Talentgram's spread) never appear. Nothing is sent from here — the admin reviews
    this exact text in the UI and then opens WhatsApp themselves."""
    project = await _get_project_or_404(pid)
    candidates = await _followup_contact_candidates(pid, project)
    if contact_id:
        chosen = next((c for c in candidates if c["client_id"] == contact_id), None)
        if not chosen:
            raise HTTPException(400, "That CRM contact is not associated with this project")
    else:
        default_id = project.get("pd_production_contact_client_id")
        if not default_id:
            raise HTTPException(400, "Set a Payment Follow-up concerned person first")
        chosen = next((c for c in candidates if c["client_id"] == default_id), None)
        if not chosen:
            raise HTTPException(400, "Set a Payment Follow-up concerned person first")
    if not (chosen.get("phone_number") or "").strip():
        raise HTTPException(400, "This CRM contact has no phone number on file")

    locked, money = await _financial_context(pid, project)
    talent_names = ", ".join(c["name"] for c in locked if c.get("name")) or "the locked talent(s)"
    brand = project.get("brand_name") or "the project"
    terms = project.get("pd_payment_terms")
    expected = project.get("pd_expected_payment_date")
    shoot_dates = sorted({d.get("date") for c in locked for d in (c.get("shoot_days") or []) if d.get("date")})

    lines = [
        f"Hi {chosen.get('name') or ''},".strip(),
        "",
        f"Just a gentle reminder regarding the payment for the {brand} project.",
        "",
        f"Project: {brand}",
        f"Talent(s): {talent_names}",
    ]
    if shoot_dates:
        lines.append("Shoot date(s): " + ", ".join(date.fromisoformat(d).strftime("%d %b %Y") for d in shoot_dates))
    elif project.get("shoot_dates"):
        lines.append(f"Shoot date(s): {project['shoot_dates']}")

    warnings: List[str] = []
    breakdown: Dict[str, Any] = {}
    billable = money["production_billable_total"]
    if billable is None:
        warnings.append("No production quote entered — the message has no amounts.")
    elif money["production_basis"] == "partial":
        warnings.append(
            f"Production quote is missing for {money['production_quotes_missing']} talent(s) — amounts omitted until it is entered."
        )
    else:
        base = billable - money["production_overtime_total"] - money["production_reimbursements_total"]
        lines += ["", f"Production amount: ₹{_fmt_inr(base)}"]
        if money["production_overtime_total"]:
            lines.append(f"Overtime: ₹{_fmt_inr(money['production_overtime_total'])}")
        if money["production_reimbursements_total"]:
            lines.append(f"Reimbursements: ₹{_fmt_inr(money['production_reimbursements_total'])}")
        lines.append(f"Total due: ₹{_fmt_inr(billable)}")
        if money["client_received_total"]:
            lines.append(f"Received: ₹{_fmt_inr(money['client_received_total'])}")
        lines.append(f"Outstanding: ₹{_fmt_inr(money['client_outstanding_total'])}")
        breakdown = {
            "production_amount": base, "overtime": money["production_overtime_total"],
            "reimbursements": money["production_reimbursements_total"], "total_due": billable,
            "received": money["client_received_total"], "outstanding": money["client_outstanding_total"],
            "basis": money["production_basis"],
        }
    if terms:
        lines.append(f"Payment terms: {terms}")
    if expected:
        expected_display = expected[:10] if isinstance(expected, str) else expected
        lines.append(f"Expected payment date: {expected_display}")
    lines += ["", "Kindly arrange the payment at your earliest convenience.", "", "Thank you."]
    return {
        "phone": chosen["phone_number"], "contact_id": chosen["client_id"], "contact_name": chosen.get("name"),
        "message": "\n".join(lines), "breakdown": breakdown, "warnings": warnings,
    }


async def _load_talent_invoice_card(pid: str, talent_id: str) -> tuple[dict, dict, dict]:
    """Shared by the GET preview and the POST group-send endpoint below —
    one place that resolves project/talent/card, never two."""
    project = await _get_project_or_404(pid)
    row = await _get_locked_pipeline_row(pid, talent_id)
    talent = await db.talents.find_one({"id": talent_id}, {"_id": 0, "id": 1, "name": 1, "email": 1, "phone": 1, "instagram_handle": 1, "cover_media_id": 1, "media": 1, "whatsapp_group_name": 1})
    if not talent:
        raise HTTPException(404, "Talent not found")
    reimbursements = await db.project_reimbursements.find({"project_id": pid, "talent_id": talent_id}, {"_id": 0}).to_list(200)
    reimbursement_total = sum(_num(r.get("amount")) or 0.0 for r in reimbursements)
    card = _talent_card(talent, row, project, reimbursement_total)
    return project, talent, card


def _build_invoice_message(project: dict, card: dict) -> Dict[str, Any]:
    """Pure message/breakdown construction — no I/O, no HTTP concerns, so
    the GET preview and the POST group-send action can never drift apart."""
    if card["commissionable_amount"] is None or card["commission_amount"] is None:
        raise HTTPException(400, "Set this talent's budget/day (or total) and commission first")

    brand = project.get("brand_name") or "the project"
    first_name = (card["name"] or "").split(" ")[0] or "there"
    has_extra_hours = bool(card["extra_hours_total"])
    has_reimbursement = bool(card["reimbursement_total"])
    extra_hours_count = _talent_extra_hours_count(card["shoot_days"]) if has_extra_hours else 0

    lines = [
        f"Hi {first_name},",
        "",
        f"Please raise and send your invoice for the {brand} project.",
        "",
        f"Talent Fee: ₹{_fmt_inr(card['budget_total'])}",
    ]
    commission_pct = card["commission_percent"]
    commission_pct_label = f"{commission_pct:g}%" if commission_pct is not None else ""
    if has_extra_hours:
        # V2 polish (spec section 18) — only mention overtime/commissionable
        # breakdown when overtime actually happened; a flat fee with no
        # extra hours never mentions "Commissionable Amount" at all (spec
        # section 17), since it would just equal the Talent Fee.
        hours_label = f"{extra_hours_count:g} hour{'s' if extra_hours_count != 1 else ''}"
        lines.append(f"Extra Hours: {hours_label} — ₹{_fmt_inr(card['extra_hours_total'])}")
        lines.append(f"Commissionable Amount: ₹{_fmt_inr(card['commissionable_amount'])}")
        lines.append(f"Commission @ {commission_pct_label}: ₹{_fmt_inr(card['commission_amount'])}")
    else:
        lines.append(f"Commission @ {commission_pct_label}: ₹{_fmt_inr(card['commission_amount'])}")
    if has_reimbursement:
        lines.append(f"Reimbursements: ₹{_fmt_inr(card['reimbursement_total'])}")
    lines += [
        "",
        f"Invoice Amount to Talentgram: ₹{_fmt_inr(card['invoice_amount'])}",
        "",
        "Please raise the invoice accordingly and share it with us.",
        "",
        BILLING_DETAILS_BLOCK,
        "",
        "Thanks,",
        "Talentgram Agency",
    ]
    message = "\n".join(lines)

    # V2 polish (spec sections 22-25) — WhatsApp destination preference.
    # whatsapp_group_name (the SAME field the existing campaign engine's own
    # _resolve_destination() already treats as authoritative) wins when
    # non-empty after stripping — exactly _resolve_destination's own rule,
    # so "missing/null/empty/whitespace-only" group all correctly fall back
    # to phone here too, with no separate validity notion invented.
    group_name = (card.get("whatsapp_group_name") or "").strip() or None
    destination_type = "group" if group_name else "phone"

    return {
        "phone": card["phone"], "talent_name": card["name"], "message": message,
        "whatsapp_group_name": group_name,
        "destination_type": destination_type,
        "breakdown": {
            "talent_fee": card["budget_total"],
            "extra_hours": card["extra_hours_total"],
            "extra_hours_count": extra_hours_count,
            "commissionable": card["commissionable_amount"],
            "commission_percent": commission_pct,
            "commission_amount": card["commission_amount"],
            "reimbursements": card["reimbursement_total"],
            "invoice_amount": card["invoice_amount"],
        },
    }


@router.get("/{pid}/production-desk/talents/{talent_id}/invoice-message")
async def build_talent_invoice_message(pid: str, talent_id: str, admin: dict = Depends(current_team_or_admin)):
    project, talent, card = await _load_talent_invoice_card(pid, talent_id)
    result = _build_invoice_message(project, card)
    # Phone is only REQUIRED when it's actually the destination that will be
    # used — a talent with a WhatsApp group but no phone on file must still
    # be able to send to that group (spec: "no phone + valid group -> group
    # is used").
    if result["destination_type"] == "phone" and card["phone"] is None:
        raise HTTPException(400, "This talent has no phone number on file")
    return result


@router.post("/{pid}/production-desk/talents/{talent_id}/invoice-message/send-to-group")
async def send_talent_invoice_to_group(pid: str, talent_id: str, admin: dict = Depends(current_team_or_admin)):
    """Queues the exact same invoice message to the talent's WhatsApp GROUP
    through the EXISTING WhatsApp Engine (the same _create_batch_internal /
    whatsapp_jobs / worker pipeline POST /api/whatsapp/batches, "Send
    Casting Call", and the Simple Assistant WhatsApp adapter all already
    use — see simple_assistant/whatsapp_send.py's own docstring for the
    identical pattern reused here, no new template/queue/worker/number).

    A phone number can NEVER open a WhatsApp group via a link — wa.me only
    opens an individual chat — so this is the only way the destination can
    actually BE the group, not just display its name. Only reachable when a
    group is genuinely on file; the phone case stays on the existing
    wa.me-with-manual-review flow, untouched. The frontend requires an
    explicit admin confirmation click (showing this exact message and
    destination) before calling this endpoint — see ProductionDesk.jsx's
    askTalentToRaiseInvoice — so a financial message is never queued
    without the admin having reviewed it first. The worker's own
    sending_enabled gate (added after a prior misuse incident) still
    applies unchanged; this endpoint cannot bypass it.
    """
    from routers.whatsapp import BatchIn, ManualContact, SourceParams, _create_batch_internal

    project, talent, card = await _load_talent_invoice_card(pid, talent_id)
    result = _build_invoice_message(project, card)
    group_name = result["whatsapp_group_name"]
    if not group_name:
        raise HTTPException(400, "This talent has no WhatsApp group on file — use the phone number instead")

    template_doc = await db.whatsapp_templates.find_one({"slug": "custom"}, {"_id": 0, "id": 1})
    if not template_doc:
        raise HTTPException(503, "WhatsApp 'Custom Message' template is not configured")

    try:
        res = await _create_batch_internal(
            BatchIn(
                source_type="MANUAL",
                source_params=SourceParams(contacts=[
                    ManualContact(name=card["name"] or "", phone="", whatsapp_group_name=group_name),
                ]),
                template_id=template_doc["id"],
                variable_data={"message": result["message"]},
                is_dry_run=False,
            ),
            admin,
        )
    except HTTPException:
        raise
    jobs = res.get("jobs") or []
    if not jobs:
        raise HTTPException(400, "The WhatsApp group did not resolve to a sendable destination")
    return {
        "ok": True,
        "batch_id": res["batch"]["id"],
        "job_ids": [j["id"] for j in jobs],
        "whatsapp_group_name": group_name,
        "talent_name": card["name"],
    }
