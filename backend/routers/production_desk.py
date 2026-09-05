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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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


def _talent_card(t: dict, row: dict, project: dict) -> dict:
    """One locked talent's Production Desk view — budget, commission,
    payment. Effective shooting days/commission fall back to the
    project-level value when the talent has no override, and an
    explicitly-set budget_total is NEVER recomputed from per-day × days
    (only used when total itself is absent)."""
    from routers.casting_pipeline import _talent_merge_fields

    merged = _talent_merge_fields(t)
    per_day = _num(row.get("pd_budget_per_day"))
    explicit_total = _num(row.get("pd_budget_total"))
    shooting_days = row.get("pd_shooting_days")
    shooting_days = int(shooting_days) if shooting_days not in (None, "") else project.get("pd_shooting_days")
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

    commission_amount = (
        round(budget_total * commission_fraction, 2)
        if budget_total is not None and commission_fraction is not None
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
        # Talent Preparation (Phase 2) — additive fields on the SAME
        # locked casting_pipeline row, no second talent/project record.
        "costume_trial_at": row.get("pd_costume_trial_at"),
        "costume_trial_location": row.get("pd_costume_trial_location"),
        "fitting_status": row.get("pd_fitting_status") or "not_scheduled",
        "look_test_status": row.get("pd_look_test_status") or "not_scheduled",
        "grooming_requirements": row.get("pd_grooming_requirements"),
        "special_instructions": row.get("pd_special_instructions"),
        "shoot_status": row.get("pd_shoot_status") or "not_scheduled",
    }


async def _locked_talent_cards(pid: str, project: dict) -> List[dict]:
    rows = await db.casting_pipeline.find({"project_id": pid, "stage": "locked"}, {"_id": 0}).to_list(2000)
    if not rows:
        return []
    talent_ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    talents = await db.talents.find(
        {"id": {"$in": talent_ids}},
        {"_id": 0, "id": 1, "name": 1, "email": 1, "phone": 1, "instagram_handle": 1, "cover_media_id": 1, "media": 1},
    ).to_list(len(talent_ids))
    by_id = {t["id"]: t for t in talents}
    cards = []
    for row in rows:
        t = by_id.get(row.get("talent_id"))
        if not t:
            continue
        cards.append(_talent_card(t, row, project))
    return cards


def _serialise_client_ref(client_id: Optional[str], name_cache: Dict[str, str]) -> Optional[dict]:
    if not client_id:
        return None
    return {"client_id": client_id, "name": name_cache.get(client_id, "")}


async def _client_name_map(client_ids: List[str]) -> Dict[str, str]:
    """Batch-resolve CRM client display names — one query regardless of how
    many kickbacks/crew rows reference clients."""
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
    docs = await db.clients.find({"_id": {"$in": oids}}, {"name": 1}).to_list(len(oids))
    return {str(d["_id"]): d.get("name", "") for d in docs}


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


# ---------------------------------------------------------------------------
# GET /projects/{pid}/production-desk — the consolidated view
# ---------------------------------------------------------------------------
@router.get("/{pid}/production-desk")
async def get_production_desk(pid: str, admin: dict = Depends(current_team_or_admin)):
    project = await _get_project_or_404(pid)

    locked = await _locked_talent_cards(pid, project)

    talent_budget_total = sum(c["budget_total"] for c in locked if c["budget_total"] is not None)
    commission_gross = sum(c["commission_amount"] for c in locked if c["commission_amount"] is not None)
    cleared = sum(1 for c in locked if c["payment_status"] == "cleared")
    pending_amount = sum(
        c["budget_total"] for c in locked
        if c["payment_status"] == "pending" and c["budget_total"] is not None
    )

    kickbacks = await db.project_kickbacks.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(500)
    kickbacks_total = sum(_num(k.get("amount")) or 0 for k in kickbacks)
    commission_net = commission_gross - kickbacks_total

    reimbursements = await db.project_reimbursements.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(500)
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
    from routers.workflow import _today_bounds_utc
    today_start, today_end = _today_bounds_utc()

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
    if locked and not project.get("pd_shoot_location") and not project.get("pd_call_time"):
        needs_attention.append("Shoot details incomplete")

    return {
        "project": {
            "id": project["id"],
            "brand_name": project.get("brand_name"),
            "status": project.get("status"),
            "commission_percent": project.get("commission_percent"),
            "shoot_dates": project.get("shoot_dates"),
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
        },
        "finance": {
            # Honest, static Case-B state — see module docstring. Never
            # flipped to "synced" by any code path in this repo.
            "zoho_status": ZOHO_STATUS,
        },
        "locked_talents": locked,
        "summary": {
            "locked_count": len(locked),
            "shoot_days": project.get("pd_shooting_days"),
            "talent_budget_total": talent_budget_total,
            "production_budget_total": _num(project.get("pd_production_budget_total")),
            "commission_gross": round(commission_gross, 2),
            "kickbacks_total": round(kickbacks_total, 2),
            "commission_net": round(commission_net, 2),
            "payments_cleared": cleared,
            "payments_total": len(locked),
            "payments_pending_amount": round(pending_amount, 2),
        },
        "needs_attention": needs_attention,
        "kickbacks": kickbacks,
        "reimbursements": reimbursements,
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
            "project_shoot_today": project_shoot_today,
            "payment_followup_due": payment_followup_due_today,
        },
        "upcoming": {
            "tasks": task_buckets["upcoming"],
            "trials": trials_upcoming,
            "shoots": shoots_upcoming,
            "payment_followup": payment_followup_upcoming,
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
    # Payment Follow-up Management (Phase 2) — operational tracking only,
    # not a Finance record. See module docstring.
    payment_terms: Optional[str] = None
    expected_payment_date: Optional[str] = None
    last_follow_up_at: Optional[str] = None
    next_follow_up_at: Optional[str] = None
    payment_followup_status: Optional[str] = None
    payment_followup_notes: Optional[str] = None


@router.patch("/{pid}/production-desk")
async def update_production_desk_project(pid: str, payload: ProductionDeskProjectPatch, admin: dict = Depends(current_team_or_admin)):
    project = await _get_project_or_404(pid)
    if payload.production_status is not None and payload.production_status not in PRODUCTION_STATUS_OPTIONS:
        raise HTTPException(400, f"production_status must be one of {PRODUCTION_STATUS_OPTIONS}")
    if payload.shoot_status is not None and payload.shoot_status not in SHOOT_STATUS_OPTIONS:
        raise HTTPException(400, f"shoot_status must be one of {SHOOT_STATUS_OPTIONS}")
    if payload.payment_followup_status is not None and payload.payment_followup_status not in PAYMENT_FOLLOWUP_STATUSES:
        raise HTTPException(400, f"payment_followup_status must be one of {PAYMENT_FOLLOWUP_STATUSES}")
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
    }
    updates = {field_map[k]: v for k, v in payload.model_dump(exclude_unset=True).items()}
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
    shooting_days: Optional[int] = None
    commission_percent: Optional[float] = None
    payment_status: Optional[str] = None
    # Talent Preparation (Phase 2)
    costume_trial_at: Optional[str] = None
    costume_trial_location: Optional[str] = None
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
        "shooting_days": "pd_shooting_days",
        "commission_percent": "pd_commission_percent",
        "payment_status": "pd_payment_status",
        "costume_trial_at": "pd_costume_trial_at",
        "costume_trial_location": "pd_costume_trial_location",
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


@router.delete("/{pid}/production-desk/crew/{crew_id}")
async def delete_crew(pid: str, crew_id: str, admin: dict = Depends(current_admin)):
    res = await db.project_crew.delete_one({"id": crew_id, "project_id": pid})
    if not res.deleted_count:
        raise HTTPException(404, "Crew member not found")
    return await get_production_desk(pid, admin)
