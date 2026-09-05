"""Talentgram Management Agent — a NEW WhatsApp agent group, using the SAME
existing WhatsApp number / worker / multi-agent routing infrastructure as
Talentgram Scouting Agent, Talentgram Casting Pipeline, and Talentgram
Fetcher Agent. Group routing needs no new transport code at all — it is
entirely data-driven via `whatsapp_agent_config` (see agents/registry.py's
resolve_agent_for_group and agents/__init__.py's seed_agent_config call for
AGENT_ID below); the WhatsApp worker discovers this new group automatically
via the existing generic /known-groups endpoint the moment that config doc
exists. No new WhatsApp number, no new Playwright worker, no new engine.

Scope: an operational read/write interface over data that ALREADY lives in
Casting Pipeline + Production Desk (backend/routers/production_desk.py,
commit 934cd06 + the Finance connector pass). Every query and every write
below calls production_desk's own functions DIRECTLY (plain Python calls,
not HTTP) — the exact same code path Production Desk's own UI uses. This
means:
  - A payment/checklist/invoice status this agent reports is the SAME
    record Production Desk shows — never a second, independently-tracked
    copy that could drift.
  - A write this agent performs (mark cleared, add a reimbursement, add
    crew) goes through production_desk.py's existing validation (e.g. a
    payment can only be marked cleared for an ACTUALLY locked talent) and
    ALREADY fires the existing notification fan-out
    (notifications.production_desk_payment_cleared /
    production_desk_payment_in_received) for free — nothing new to wire.
  - No WhatsApp-specific duplicate project/finance/talent storage exists
    anywhere in this file.

Reused, not duplicated:
  - Project name resolution: casting_pipeline_nlu.resolve_project_by_name —
    the EXACT same fuzzy/typo-tolerant matching ADD/MOVE/SHARE/SHOW ME
    already use. The CANDIDATE LIST fed into it is intentionally NOT
    casting_pipeline._fetch_ongoing_projects() (see
    PRODUCTION_DESK_RELEVANT_STATUSES below for why).
  - Conversational continuity ("What's our commission?" with no project
    named): session_context (whatsapp_agent_sessions) — the same
    domain-agnostic per-(agent, phone) state store other agents use for
    multi-turn flows, here holding only {last_project_id, last_project_label}.
  - Confirmation/edit/cancel flow for financial mutations: the platform's
    own generic engine (FieldSpec + IntentDefinition.auto_confirm=False),
    not a bespoke yes/no prompt — same mechanism crm-agent/casting-agent
    already use for every other write action on this platform.
  - CRM contact lookup/creation for "Add Rahul as DOP": routers.marketing.
    insert_client_doc — the exact function crm-agent's own executor calls;
    an existing contact with a matching name is reused, never duplicated.

Production Management Desk (Phase 1+2, 2026-09): tasks/reminders created
or completed through this agent go through routers.workflow.create_task /
update_task DIRECTLY (the exact functions the admin Workflow page and
Production Desk's own task queries use) — reading/writing the SAME
db.workflow_tasks collection, never a WhatsApp-only copy. "Remind me to
follow up with X on Monday" creates a due-dated workflow_tasks row a
manager can see and complete from Production Desk or the Workflow page;
it is NOT an autonomous push notification — no autonomous time-based
reminder-firing exists yet (deliberately deferred to a later pass, per
spec, until this foundation is verified) — see production_desk.py's own
docstring for the architecture that would add it (a small polling loop
mirroring services/media_assignment_worker.py, none of which exists yet).

Deliberately NOT implemented in this pass: "Add kickback" as a WhatsApp
command (recipient resolution via free text adds real ambiguity a v1
shouldn't guess at — Kickbacks stay Production-Desk-UI-only for now);
Zoho Books anything (does not exist — see production_desk.py's own
docstring); autonomous time-based WhatsApp push reminders (see above).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from core import db
from routers import production_desk as pd
from routers.marketing import insert_client_doc

from agents import session_context
from agents.models import AgentDefinition, ExecContext, ExecResult, FieldSpec, IntentDefinition, ValidationResult
from agents.registry import register_agent
from agents.modules import casting_pipeline_nlu as nlu

logger = logging.getLogger(__name__)

AGENT_ID = "management-agent"

# Project candidates for THIS agent's name resolution — deliberately NOT
# casting_pipeline._fetch_ongoing_projects() (status == "ongoing" only),
# which is scoped to Casting Pipeline's own "still needs casting work"
# concern. Real production data has projects whose casting is fully
# locked (project.status == "locked", a human flips this once casting is
# done) that STILL have live Production Desk data — that is in fact the
# primary case Production Desk (and therefore this agent) exists for.
# Excludes "hold" (paused, nothing active yet) and "complete" (already
# closed out). Fixes a real production incident (2026-09-05): "GOOGLE AI"
# has project.status == "locked" — _fetch_ongoing_projects() could never
# return it, so "What's pending for Google AI?" fell through to fuzzy-
# matching against an unrelated candidate pool. Still reuses
# casting_pipeline_nlu.resolve_project_by_name for the actual name
# matching — this is only a differently-scoped candidate list, not a
# second name-resolution system.
PRODUCTION_DESK_RELEVANT_STATUSES = ["ongoing", "locked"]


async def _fetch_production_desk_projects() -> List[Dict[str, str]]:
    cursor = db.projects.find(
        {"status": {"$in": PRODUCTION_DESK_RELEVANT_STATUSES}}, {"_id": 0, "id": 1, "brand_name": 1}
    ).sort("brand_name", 1)
    docs = await cursor.to_list(2000)
    return [{"id": d["id"], "label": d.get("brand_name") or "(untitled project)"} for d in docs]

# Synthetic "admin" identity passed to production_desk.py's functions,
# which only ever read admin.get("id")/admin.get("email") off it (for
# created_by/actor_id bookkeeping) — no real FastAPI auth session exists
# for a WhatsApp turn, same reasoning crm-agent's insert_client_doc(source=...)
# call already uses instead of a real admin session.
_AGENT_ADMIN = {"id": "whatsapp:management-agent", "email": "management-agent@whatsapp.talentgram"}


# ---------------------------------------------------------------------------
# Project resolution — reuses the exact fuzzy resolver ADD/MOVE/SHOW ME use,
# plus session_context for "no project named this turn" continuity.
# ---------------------------------------------------------------------------
_FOR_PROJECT_RE = re.compile(r"\bfor\s+(.+?)\s*[\?\.!]*$", re.IGNORECASE)


def _extract_trailing_project(text: str) -> str:
    m = _FOR_PROJECT_RE.search(text or "")
    return m.group(1).strip() if m else ""


@dataclass
class ProjectResolution:
    project: Optional[Dict[str, str]] = None  # {"id", "label"}
    ambiguous: Optional[List[Dict[str, str]]] = None
    error: Optional[str] = None


async def _resolve_project(query: str, ctx: ExecContext) -> ProjectResolution:
    projects = await _fetch_production_desk_projects()
    q = (query or "").strip()
    if not q:
        session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
        last_id = (session or {}).get("last_project_id")
        if last_id:
            proj = next((p for p in projects if p["id"] == last_id), None)
            if proj:
                return ProjectResolution(project=proj)
        return ProjectResolution(
            error='Which project? Please include the project name, e.g. "...for Google AI".'
        )
    match = nlu.resolve_project_by_name(q, projects)
    if match.project:
        return ProjectResolution(project=match.project)
    if match.ambiguous:
        return ProjectResolution(ambiguous=match.ambiguous)
    # PRODUCTION BUG (fixed) — ProjectNameMatch has a FOURTH outcome this
    # wrapper originally missed entirely: `.suggestions` (Tier 5's "no
    # confident/tied match, but here are close fuzzy candidates below the
    # auto-resolve bar" — see casting_pipeline_nlu.resolve_project_by_name's
    # own docstring on the field, and casting_pipeline.py's existing
    # callers, which all handle it explicitly). When neither `.project`
    # nor `.ambiguous` nor `.suggestions` matched (a genuine "not found"),
    # `.error` is also not guaranteed non-empty by the dataclass itself —
    # every existing caller defensively falls back to a literal message
    # rather than trusting it's set; do the same here. Before this fix,
    # a message like "What's pending for Google AI?" where "Google AI"
    # didn't closely match any real ongoing project label produced a
    # ProjectResolution with project=None, ambiguous=None, error=None —
    # silently passing both `if` checks in every caller and crashing on
    # `project["id"]` several lines later (production incident, live
    # WhatsApp group, 2026-09-05).
    if match.suggestions:
        return ProjectResolution(ambiguous=match.suggestions)
    return ProjectResolution(error=match.error or f'I couldn\'t find a project matching "{q}".')


async def _remember_project(ctx: ExecContext, project: Optional[Dict[str, str]]) -> None:
    # Defense in depth — every real call site already only reaches here
    # after confirming `project` is a resolved {"id","label"} dict (see
    # the ProjectResolution.suggestions fix above, which was the actual
    # production crash site). A None here would be a genuinely new bug
    # elsewhere; skip silently rather than crash the whole turn over a
    # session-continuity nicety that isn't the primary result being sent.
    if not project or not project.get("id"):
        return
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone,
        last_project_id=project["id"], last_project_label=project.get("label"),
    )


def _ambiguous_project_message(candidates: List[Dict[str, str]]) -> str:
    lines = ["Which project do you mean?", ""]
    lines += [f"{i}. {c['label']}" for i, c in enumerate(candidates, start=1)]
    lines.append("\nPlease resend your command with the exact project name.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Talent resolution WITHIN a project's locked talents (never the full
# roster — Management Agent only ever talks about LOCKED talents, per the
# Casting-Pipeline-is-the-source-of-truth rule Production Desk itself
# follows).
# ---------------------------------------------------------------------------
def _extract_talent_before_payment(text: str) -> str:
    m = re.match(r"^\s*\S+\s+(.+?)\s+payment\b", text or "", re.IGNORECASE)
    if not m:
        return ""
    name = m.group(1).strip().strip('"\'')
    if name.lower().endswith("'s"):
        name = name[:-2].strip()
    return name


_TALENT_TOPIC_RE = re.compile(
    r"^(?:when is|when's|show|what's|whats|is|mark)\s+(.+?)\s+(costume trial|trial|fitting|look test|shoot(?:ing)?|payment)\b",
    re.IGNORECASE,
)


def _extract_talent_and_topic(text: str) -> Tuple[str, Optional[str]]:
    """"When is Shivi's costume trial?" -> ("Shivi", "trial"). "When is Shivi
    shooting?" -> ("Shivi", "shoot"). "Show Shivi's payment" -> ("Shivi",
    "payment"). "Mark Shivi's costume trial completed" -> ("Shivi", "trial")
    (also matched by the "mark" trigger — see MARK_TALENT_STATUS_INTENT)."""
    m = _TALENT_TOPIC_RE.match((text or "").strip())
    if not m:
        return "", None
    name = m.group(1).strip().strip('"\'')
    if name.lower().endswith("'s"):
        name = name[:-2].strip()
    topic_raw = m.group(2).lower()
    if "trial" in topic_raw or "fitting" in topic_raw or "look test" in topic_raw:
        topic = "trial"
    elif "shoot" in topic_raw:
        topic = "shoot"
    elif "payment" in topic_raw:
        topic = "payment"
    else:
        topic = None
    return name, topic


def _match_talent_by_name(name_query: str, locked_talents: List[dict]) -> Tuple[Optional[dict], List[dict]]:
    """Case-insensitive substring match against a project's already-small
    locked-talents list — not the fuzzy full-roster matcher other agents
    use for resolving among thousands of talents, deliberately simpler
    since this list is a handful of names at most."""
    q = (name_query or "").strip().lower()
    if not q:
        return None, []
    exact = [t for t in locked_talents if (t.get("name") or "").strip().lower() == q]
    if len(exact) == 1:
        return exact[0], []
    substr = [t for t in locked_talents if q in (t.get("name") or "").strip().lower()]
    if len(substr) == 1:
        return substr[0], []
    if substr:
        return None, substr
    return None, []


async def _find_talent_across_projects(name_query: str) -> Tuple[Optional[dict], Optional[dict], List[str]]:
    """No project named — search LOCKED talents (any ongoing/locked
    project — see PRODUCTION_DESK_RELEVANT_STATUSES) for a name match.
    Talent-name-first (not project-iteration-first): a
    case-insensitive regex against db.talents.name, then filtered down to
    rows actually LOCKED on an ongoing project — bounded by how many
    talents match the name, not by how many ongoing projects exist (an
    earlier version capped at the first 30 projects sorted by brand_name,
    which silently could never find a name locked on a project sorting
    past that cutoff — fixed by searching talents, not projects, first).
    Returns (talent_card, project_dict, other_project_labels_if_ambiguous)."""
    q = (name_query or "").strip()
    if not q:
        return None, None, []
    talent_docs = await db.talents.find(
        {"name": {"$regex": re.escape(q), "$options": "i"}},
        {"_id": 0, "id": 1, "name": 1, "email": 1, "phone": 1, "instagram_handle": 1, "cover_media_id": 1, "media": 1},
    ).to_list(50)
    if not talent_docs:
        return None, None, []
    talent_ids = [t["id"] for t in talent_docs]
    rows = await db.casting_pipeline.find(
        {"talent_id": {"$in": talent_ids}, "stage": "locked"}, {"_id": 0}
    ).to_list(200)
    if not rows:
        return None, None, []
    project_ids = list({r["project_id"] for r in rows})
    projects = await db.projects.find(
        {"id": {"$in": project_ids}, "status": {"$in": PRODUCTION_DESK_RELEVANT_STATUSES}}, {"_id": 0}
    ).to_list(len(project_ids))
    proj_by_id = {p["id"]: p for p in projects}
    talent_by_id = {t["id"]: t for t in talent_docs}

    found: List[Tuple[dict, dict]] = []
    for row in rows:
        project = proj_by_id.get(row["project_id"])
        talent = talent_by_id.get(row["talent_id"])
        if not project or not talent:
            continue
        card = pd._talent_card(talent, row, project)
        found.append((card, {"id": project["id"], "label": project.get("brand_name") or "(untitled project)"}))

    if len(found) == 1:
        return found[0][0], found[0][1], []
    if len(found) > 1:
        return None, None, [p["label"] for _, p in found]
    return None, None, []


# ---------------------------------------------------------------------------
# Money extraction — "₹5,000" / "Rs 5000" / "5000" (bare number as last
# resort only — an explicit currency marker is preferred whenever present).
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"\b([\d,]{3,}(?:\.\d+)?)\b")


def _extract_amount(text: str) -> Optional[float]:
    m = _AMOUNT_RE.search(text or "")
    if not m:
        m = _BARE_NUMBER_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _format_inr(val) -> str:
    if val is None:
        return "—"
    try:
        return f"₹{val:,.0f}"
    except (TypeError, ValueError):
        return f"₹{val}"


def _format_due(due_at: Optional[str]) -> str:
    if not due_at:
        return "—"
    try:
        dt = datetime.fromisoformat(due_at)
        return dt.strftime("%d %b")
    except ValueError:
        return due_at


# ---------------------------------------------------------------------------
# Deterministic (non-AI) relative-date parsing — "today"/"tomorrow"/a
# weekday name only. No natural-language date library, no guessing beyond
# these explicit words; anything else (an explicit calendar date like
# "30 August") is simply not recognised and the field is left for the
# user to fill in via the normal missing-field prompt.
# ---------------------------------------------------------------------------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DATE_WORD_RE = re.compile(
    r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
)


def _parse_due_date(word: str) -> Optional[str]:
    w = (word or "").strip().lower()
    now = datetime.now(timezone.utc)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if w == "today":
        return noon.isoformat()
    if w == "tomorrow":
        return (noon + timedelta(days=1)).isoformat()
    if w in _WEEKDAYS:
        target_idx = _WEEKDAYS.index(w)
        days_ahead = (target_idx - now.weekday()) % 7
        days_ahead = days_ahead or 7  # "on Monday" said on a Monday means NEXT Monday, not today
        return (noon + timedelta(days=days_ahead)).isoformat()
    return None


def _strip_date_phrase(text: str) -> Tuple[str, Optional[str]]:
    """Finds the LAST date word in the text (matching how these commands
    are phrased — "...to get the call sheet tomorrow", "...on Monday"),
    parses it, and returns the text with that phrase (plus a leading "on"
    if present) removed. (remaining_text, due_at_or_None)."""
    matches = list(_DATE_WORD_RE.finditer(text or ""))
    if not matches:
        return (text or "").strip(), None
    m = matches[-1]
    due_at = _parse_due_date(m.group(1))
    start = m.start()
    # Absorb a preceding " on " into the stripped span too.
    prefix = text[:start]
    on_m = re.search(r"\bon\s*$", prefix, re.IGNORECASE)
    if on_m:
        start = on_m.start()
    remaining = (text[:start] + text[m.end():]).strip().rstrip(".").strip()
    return remaining, due_at


# ===========================================================================
# READ — one flexible status/finance query intent covering every example
# query the spec lists, response SHAPED by which keywords the message
# contains (deterministic keyword matching — no AI/LLM anywhere here).
# ===========================================================================
_FOCUS_KEYWORDS = {
    "locked_talents": ("locked talent",),
    "budget": ("talent budget", "production budget", "budget"),
    "commission": ("commission", "kickback"),
    "reimbursement": ("reimbursement",),
    "contact": ("production contact", "contact"),
    "shoot": ("shoot date", "shoot day", "shoot details", "call time", "location"),
    "invoice": ("invoice",),
    "payment_in": ("client paid", "client payment", "payment in"),
    "crew": ("crew",),
    "requirements": ("requirement", "usage", "deliverable"),
    "documents": ("document",),
    # Production Management Desk (Phase 1+2)
    "today": ("happening today", "today"),
    "upcoming": ("upcoming", "what's next", "whats next"),
    "tasks": ("task", "todo", "to-do"),
    "payment_followup": ("payment follow-up", "payment followup", "follow-up", "followup"),
}


def _detect_focus(text: str) -> List[str]:
    t = (text or "").lower()
    hits = [key for key, phrases in _FOCUS_KEYWORDS.items() if any(p in t for p in phrases)]
    return hits


def _render_status_reply(project_label: str, body: dict, focus: List[str]) -> str:
    lines = [f"📋 {project_label}"]
    p, summary, locked, na = body["project"], body["summary"], body["locked_talents"], body["needs_attention"]

    def _overview():
        lines.append("")
        lines.append(f"Locked talents: {summary['locked_count']}  ·  Shoot days: {summary.get('shoot_days') or '—'}")
        lines.append(f"Payments: {summary['payments_cleared']}/{summary['payments_total']} cleared")
        if na:
            lines.append("")
            lines.append("⚠ Pending:")
            # NOTE: must be .extend(), not `lines += [...]` — the latter is
            # an assignment to `lines`, which makes Python treat `lines` as
            # local to this nested function for its ENTIRE body (including
            # the .append() calls above), raising UnboundLocalError.
            lines.extend(f"  • {item}" for item in na)
        else:
            lines.append("✓ Nothing pending.")

    if not focus:
        _overview()
    if "locked_talents" in focus or not focus:
        lines.append("")
        lines.append("LOCKED TALENTS")
        if not locked:
            lines.append("  (none)")
        for t in locked:
            lines.append(f"  • {t['name'] or 'Untitled'} — {_format_inr(t['budget_total'])} — {t['payment_status'].upper()}")
    if "budget" in focus:
        lines.append("")
        lines.append(f"Talent budget: {_format_inr(summary['talent_budget_total'])}")
        lines.append(f"Production budget: {_format_inr(summary['production_budget_total'])}")
    if "commission" in focus:
        lines.append("")
        lines.append(f"Commission %: {p.get('commission_percent') or '—'}")
        lines.append(f"Gross commission: {_format_inr(summary['commission_gross'])}")
        lines.append(f"Kickbacks: {_format_inr(summary['kickbacks_total'])}")
        lines.append(f"Net commission: {_format_inr(summary['commission_net'])}")
    if "reimbursement" in focus:
        lines.append("")
        lines.append("REIMBURSEMENTS")
        if not body["reimbursements"]:
            lines.append("  (none)")
        for r in body["reimbursements"]:
            lines.append(f"  • {r.get('talent_name') or '—'} — {_format_inr(r.get('amount'))} — {r.get('expense_type')} — {r.get('status').upper()}")
    if "contact" in focus:
        lines.append("")
        contact = p.get("pd_production_contact")
        lines.append(f"Production contact: {contact['name'] if contact else '—'}")
    if "shoot" in focus:
        lines.append("")
        lines.append(f"Shoot dates: {p.get('shoot_dates') or '—'}")
        lines.append(f"Location: {p.get('pd_shoot_location') or '—'}")
        lines.append(f"Call time: {p.get('pd_call_time') or '—'}")
    if "invoice" in focus:
        lines.append("")
        lines.append(f"Invoice raised: {'✓ Yes' if p.get('pd_invoice_raised') else '⚠ No'}")
        lines.append(f"Invoice sent: {'✓ Yes' if p.get('pd_invoice_sent') else '⚠ No'}")
    if "payment_in" in focus:
        lines.append("")
        lines.append(f"Client payment: {'✓ Received' if p.get('pd_payment_in_received') else '⚠ Pending'}")
    if "crew" in focus:
        lines.append("")
        lines.append("CREW")
        if not body["crew"]:
            lines.append("  (none)")
        for c in body["crew"]:
            lines.append(f"  • {(c.get('contact') or {}).get('name') or '—'} — {c.get('role')}")
    if "requirements" in focus:
        lines.append("")
        lines.append(f"Medium/Usage: {p.get('medium_usage') or '—'}")
        lines.append(f"Director: {p.get('director') or '—'}")
        lines.append(f"Production house: {p.get('production_house') or '—'}")
    if "documents" in focus:
        lines.append("")
        lines.append(f"Documents on file: {len(body['documents'])}")
    if "today" in focus:
        today = body["today"]
        lines.append("")
        lines.append("TODAY")
        if today["project_shoot_today"]:
            lines.append("  • Shoot is TODAY")
        for c in today["shoots"]:
            lines.append(f"  • Shoot today — {c['name']}")
        for c in today["trials"]:
            lines.append(f"  • Costume trial today — {c['name']} ({c.get('costume_trial_location') or 'location TBC'})")
        for t in today["tasks"]:
            who = f" ({t['talent_name']})" if t.get("talent_name") else ""
            lines.append(f"  • Task due today: {t['title']}{who}")
        if today["payment_followup_due"]:
            lines.append("  • Payment follow-up due today")
        if not any([today["project_shoot_today"], today["shoots"], today["trials"], today["tasks"], today["payment_followup_due"]]):
            lines.append("  Nothing scheduled today.")
    if "upcoming" in focus:
        up = body["upcoming"]
        lines.append("")
        lines.append("UPCOMING")
        for c in up["trials"]:
            lines.append(f"  • Costume trial — {c['name']} ({_format_due(c.get('costume_trial_at'))})")
        for c in up["shoots"]:
            lines.append(f"  • Shoot scheduled — {c['name']}")
        for t in up["tasks"]:
            who = f" ({t['talent_name']})" if t.get("talent_name") else ""
            lines.append(f"  • {t['title']}{who} — due {_format_due(t.get('due_at'))}")
        if up["payment_followup"]:
            lines.append(f"  • Payment follow-up — {_format_due(p.get('pd_next_follow_up_at'))}")
        if not any([up["trials"], up["shoots"], up["tasks"], up["payment_followup"]]):
            lines.append("  Nothing upcoming.")
    if "tasks" in focus:
        lines.append("")
        lines.append("TASKS")
        pending = body["tasks"]["pending"]
        if not pending:
            lines.append("  (none)")
        for t in pending:
            who = f" ({t['talent_name']})" if t.get("talent_name") else ""
            pr = f" [{t['priority']}]" if t.get("priority") else ""
            lines.append(f"  • {t['title']}{who}{pr} — due {_format_due(t.get('due_at'))} — {t['status'].upper()}")
    if "payment_followup" in focus:
        lines.append("")
        lines.append("PAYMENT FOLLOW-UP")
        lines.append(f"  Terms: {p.get('pd_payment_terms') or '—'}")
        lines.append(f"  Expected: {_format_due(p.get('pd_expected_payment_date'))}")
        lines.append(f"  Next follow-up: {_format_due(p.get('pd_next_follow_up_at'))}")
        lines.append(f"  Status: {(p.get('pd_payment_followup_status') or 'not_due').upper()}")

    return "\n".join(lines)


_ACTIVE_STATUSES = ["pending", "in_progress"]


async def _global_digest(day_offset: int) -> str:
    """"What's happening today/tomorrow?" — no project named. Queries the
    due SIGNALS directly (workflow_tasks by due_at, casting_pipeline by
    costume_trial_at/shoot_status, projects by next_follow_up_at) rather
    than iterating every ongoing/locked project and checking each one —
    a real production DB can have hundreds of ongoing projects, and an
    "iterate the first N projects" approach silently misses anything past
    that cutoff (found live during this pass: a project sorting late
    alphabetically was invisible to an earlier, capped-scan version of
    this function). Querying the signal collections directly is bounded
    by how much is ACTUALLY due, not by how many projects exist."""
    now = datetime.now(timezone.utc)
    target_start = (now + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_end = target_start + timedelta(days=1)
    ts, te = target_start.isoformat(), target_end.isoformat()

    tasks = await db.workflow_tasks.find(
        {"due_at": {"$gte": ts, "$lt": te}, "status": {"$in": _ACTIVE_STATUSES}, "project_id": {"$ne": None}},
        {"_id": 0},
    ).to_list(500)
    trial_rows = await db.casting_pipeline.find(
        {"stage": "locked", "pd_costume_trial_at": {"$gte": ts, "$lt": te}}, {"_id": 0},
    ).to_list(500)
    shoot_rows = []
    project_shoot_today: List[dict] = []
    if day_offset == 0:
        shoot_rows = await db.casting_pipeline.find(
            {"stage": "locked", "pd_shoot_status": "today"}, {"_id": 0},
        ).to_list(500)
        project_shoot_today = await db.projects.find(
            {"pd_shoot_status": "today"}, {"_id": 0, "id": 1, "brand_name": 1},
        ).to_list(200)
    followup_projects = await db.projects.find(
        {"pd_next_follow_up_at": {"$gte": ts, "$lt": te}, "pd_payment_followup_status": {"$ne": "done"}},
        {"_id": 0, "id": 1, "brand_name": 1},
    ).to_list(200)

    project_ids = {t.get("project_id") for t in tasks} | {r.get("project_id") for r in trial_rows + shoot_rows}
    project_ids |= {p["id"] for p in followup_projects + project_shoot_today}
    project_ids.discard(None)
    projects = await db.projects.find({"id": {"$in": list(project_ids)}}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(len(project_ids)) if project_ids else []
    label_by_id = {p["id"]: p.get("brand_name") or "(untitled project)" for p in projects}

    talent_ids = list({r["talent_id"] for r in trial_rows + shoot_rows if r.get("talent_id")})
    talent_docs = await db.talents.find({"id": {"$in": talent_ids}}, {"_id": 0, "id": 1, "name": 1}).to_list(len(talent_ids)) if talent_ids else []
    talent_name_by_id = {t["id"]: t.get("name") or "Talent" for t in talent_docs}

    sections: Dict[str, List[str]] = {}

    def _add(project_id: Optional[str], line: str) -> None:
        if not project_id:
            return
        sections.setdefault(project_id, []).append(line)

    for p in project_shoot_today:
        _add(p["id"], "  • Shoot today")
    for r in shoot_rows:
        _add(r.get("project_id"), f"  • Shoot today — {talent_name_by_id.get(r.get('talent_id'), 'Talent')}")
    for r in trial_rows:
        _add(r.get("project_id"), f"  • Costume trial — {talent_name_by_id.get(r.get('talent_id'), 'Talent')}")
    for t in tasks:
        _add(t.get("project_id"), f"  • Task: {t.get('title')}")
    for p in followup_projects:
        _add(p["id"], "  • Payment follow-up due")

    label = "TODAY" if day_offset == 0 else "TOMORROW"
    lines = [f"📋 {label}"]
    if not sections:
        lines.append("")
        lines.append("Nothing scheduled.")
        return "\n".join(lines)
    for project_id, items in sections.items():
        lines.append("")
        lines.append(label_by_id.get(project_id, project_id))
        lines.extend(items)
    return "\n".join(lines)


def _render_talent_reply(talent: dict, project_label: str, topic: Optional[str]) -> str:
    lines = [f"👤 {talent['name']} — {project_label}", ""]
    if topic == "trial":
        lines.append(f"Costume trial: {_format_due(talent.get('costume_trial_at'))}")
        lines.append(f"Location: {talent.get('costume_trial_location') or '—'}")
        lines.append(f"Fitting status: {talent['fitting_status'].upper()}")
        lines.append(f"Look test status: {talent['look_test_status'].upper()}")
    elif topic == "shoot":
        lines.append(f"Shoot status: {talent['shoot_status'].upper()}")
    elif topic == "payment":
        lines.append(f"Budget: {_format_inr(talent['budget_total'])}")
        lines.append(f"Payment status: {talent['payment_status'].upper()}")
    else:
        lines.append(f"Budget: {_format_inr(talent['budget_total'])}  ·  Payment: {talent['payment_status'].upper()}")
        lines.append(f"Costume trial: {_format_due(talent.get('costume_trial_at'))}  ·  Fitting: {talent['fitting_status'].upper()}")
        lines.append(f"Shoot status: {talent['shoot_status'].upper()}")
    return "\n".join(lines)


_GLOBAL_DIGEST_RE = re.compile(
    r"(?:happening|payment follow-?ups?(?:\s+are|\s+due)?|due)\s*.*?\b(today|tomorrow)\b",
    re.IGNORECASE,
)


async def _status_query_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw = collected.get("raw_text", "")

    # "What's happening today/tomorrow?" / "What payment follow-ups are
    # due today?" with NO project named — a global, cross-project digest
    # (see _global_digest). "...for Google AI" is a different, project-
    # scoped case handled below via the normal focus detection ("today"
    # is one of _FOCUS_KEYWORDS), since a project WAS named there.
    global_m = _GLOBAL_DIGEST_RE.search(raw)
    if global_m and not _extract_trailing_project(raw):
        return ExecResult(ok=True, message=await _global_digest(0 if global_m.group(1).lower() == "today" else 1))

    # Talent-scoped query with no project named ("Show Shivi's payment",
    # "When is Shivi's costume trial?", "When is Shivi shooting?").
    talent_q, topic = _extract_talent_and_topic(raw)
    project_q = _extract_trailing_project(raw)
    if talent_q and not project_q:
        talent, project, others = await _find_talent_across_projects(talent_q)
        if talent and project:
            await _remember_project(ctx, project)
            return ExecResult(ok=True, message=_render_talent_reply(talent, project["label"], topic))
        if others:
            return ExecResult(ok=False, message=f'Found "{talent_q}" locked on more than one project: {", ".join(others)}. Please specify which one.')
        return ExecResult(ok=False, message=f'Couldn\'t find a locked talent matching "{talent_q}" on any ongoing project.')

    resolution = await _resolve_project(project_q, ctx)
    if resolution.ambiguous:
        return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
    if resolution.error:
        # "What's pending for Shivi?" — "Shivi" isn't a project; before
        # giving up, try it as a bare talent name (never guessed silently
        # — only tried as a genuine fallback once project resolution has
        # already, definitively, failed).
        if project_q:
            talent, project, others = await _find_talent_across_projects(project_q)
            if talent and project:
                await _remember_project(ctx, project)
                return ExecResult(ok=True, message=_render_talent_reply(talent, project["label"], None))
            if others:
                return ExecResult(ok=False, message=f'Found "{project_q}" locked on more than one project: {", ".join(others)}. Please specify which one.')
        return ExecResult(ok=False, message=resolution.error)

    project = resolution.project
    await _remember_project(ctx, project)
    body = await pd.get_production_desk(project["id"], _AGENT_ADMIN)

    # If a talent name was given WITH a project ("Show Shivi's payment for
    # Google AI"), narrow to just that talent instead of the full digest.
    if talent_q:
        match, others = _match_talent_by_name(talent_q, body["locked_talents"])
        if match:
            return ExecResult(ok=True, message=_render_talent_reply(match, project["label"], topic))
        if others:
            names = ", ".join(t["name"] for t in others)
            return ExecResult(ok=False, message=f'Multiple locked talents match "{talent_q}" on {project["label"]}: {names}.')
        return ExecResult(ok=False, message=f'No locked talent matching "{talent_q}" on {project["label"]}.')

    focus = _detect_focus(raw)
    return ExecResult(ok=True, message=_render_status_reply(project["label"], body, focus))


STATUS_QUERY_INTENT = IntentDefinition(
    intent_id="management.status_query",
    triggers=[
        "what's pending", "whats pending", "what is pending", "show pending",
        "show locked", "show reimbursements", "show reimbursement",
        "show crew", "show documents", "show", "what's the", "whats the",
        "what is the", "what's our", "whats our", "who is", "who's",
        "has the", "has invoice", "status", "pending for",
        "when is", "when's", "what's happening", "whats happening",
        "what payment", "payment follow-up", "payment followups", "payment follow-ups",
    ],
    fields=[FieldSpec(key="raw_text", label="Query", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False)],
    # Deliberately trivial extract_fields: the whole raw message IS the
    # field — all real parsing happens in the executor (project/talent/
    # focus extraction), matching the generic engine's contract that
    # extract_fields is sync + DB-free while everything DB-dependent
    # belongs in the (async) executor.
    extract_fields=lambda text: {"raw_text": text},
    executor=_status_query_executor,
    auto_confirm=True,
)


# ===========================================================================
# WRITE — checklist toggles (invoice raised/sent, client payment, GST).
# Confirmation-gated (auto_confirm=False, the platform's generic engine) —
# not silent, per the spec's own "confirmation for sensitive actions" rule.
# ===========================================================================
def _project_field_spec() -> FieldSpec:
    return FieldSpec(
        key="project", label="Project", question="Which project?",
        validate=lambda v: ValidationResult(ok=True, value=v) if (v or "").strip() else ValidationResult(ok=False, error="Please name the project."),
    )


def _checklist_extract_fields(text: str) -> Dict[str, str]:
    return {"project": _extract_trailing_project(text)}


def _make_checklist_try_auto_execute(field_name: str):
    async def _hook(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
        resolution = await _resolve_project(collected.get("project", ""), ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message=resolution.error)
        collected["_resolved_project_id"] = resolution.project["id"]
        collected["_resolved_project_label"] = resolution.project["label"]
        return None  # proceed to normal confirmation
    return _hook


def _make_checklist_build_confirmation(question: str):
    async def _confirm(collected: dict, ctx: ExecContext) -> str:
        label = collected.get("_resolved_project_label") or collected.get("project")
        return f"{question} {label}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    return _confirm


def _make_checklist_executor(field_name: str, success_label: str):
    async def _exec(collected: dict, ctx: ExecContext) -> ExecResult:
        pid = collected.get("_resolved_project_id")
        if not pid:
            resolution = await _resolve_project(collected.get("project", ""), ctx)
            if not resolution.project:
                return ExecResult(ok=False, message=resolution.error or "Couldn't resolve the project.")
            pid = resolution.project["id"]
        try:
            body = await pd.update_production_desk_project(
                pid, pd.ProductionDeskProjectPatch(**{field_name: True}), _AGENT_ADMIN
            )
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        label = collected.get("_resolved_project_label") or body["project"]["brand_name"]
        return ExecResult(ok=True, message=f"✓ {success_label} — {label}.")
    return _exec


MARK_INVOICE_RAISED_INTENT = IntentDefinition(
    intent_id="management.mark_invoice_raised",
    triggers=["mark invoice raised", "invoice raised"],
    fields=[_project_field_spec()],
    extract_fields=_checklist_extract_fields,
    try_auto_execute=_make_checklist_try_auto_execute("invoice_raised"),
    build_confirmation=_make_checklist_build_confirmation("Mark invoice raised for"),
    executor=_make_checklist_executor("invoice_raised", "Invoice raised"),
)

MARK_INVOICE_SENT_INTENT = IntentDefinition(
    intent_id="management.mark_invoice_sent",
    triggers=["mark invoice sent", "invoice sent"],
    fields=[_project_field_spec()],
    extract_fields=_checklist_extract_fields,
    try_auto_execute=_make_checklist_try_auto_execute("invoice_sent"),
    build_confirmation=_make_checklist_build_confirmation("Mark invoice sent for"),
    executor=_make_checklist_executor("invoice_sent", "Invoice sent"),
)

MARK_PAYMENT_IN_INTENT = IntentDefinition(
    intent_id="management.mark_payment_in",
    triggers=["mark client payment received", "mark client payment", "mark payment received", "mark payment in"],
    fields=[_project_field_spec()],
    extract_fields=_checklist_extract_fields,
    try_auto_execute=_make_checklist_try_auto_execute("payment_in_received"),
    build_confirmation=_make_checklist_build_confirmation("Mark client payment received for"),
    executor=_make_checklist_executor("payment_in_received", "Client payment marked received"),
)

MARK_GST_RECEIVED_INTENT = IntentDefinition(
    intent_id="management.mark_gst_received",
    triggers=["mark gst received", "mark gst component received", "mark gst"],
    fields=[_project_field_spec()],
    extract_fields=_checklist_extract_fields,
    try_auto_execute=_make_checklist_try_auto_execute("gst_component_received"),
    build_confirmation=_make_checklist_build_confirmation("Mark GST component received for"),
    executor=_make_checklist_executor("gst_component_received", "GST component marked received"),
)


# ===========================================================================
# WRITE — "mark <talent> ..." status updates. Two kinds share the bare
# "mark" trigger (see _extract_talent_and_topic — the SAME topic detector
# the read side uses), so — same reasoning as ADD_INTENT's kind branching
# — they live behind ONE intent rather than two silently racing on an
# identical trigger word:
#   - payment cleared: confirmation shows the REAL amount, per the spec's
#     own example ("Mark Shivi payment of ₹1,50,000 as cleared?").
#   - costume trial completed: sets pd_fitting_status="completed" on the
#     SAME locked casting_pipeline row Production Desk's Talent
#     Preparation section reads — no second "trial" record.
# ===========================================================================
def _talent_status_extract_fields(text: str) -> Dict[str, str]:
    talent, topic = _extract_talent_and_topic(text)
    kind = "trial" if topic == "trial" else "payment"  # default to payment for bare "mark X payment cleared"
    return {"project": _extract_trailing_project(text), "talent": talent, "_kind": kind}


async def _resolve_talent_for_mark(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    """Shared resolution for both kinds — populates _resolved_* on
    success, returns an ExecResult to short-circuit on failure."""
    talent_q = collected.get("talent", "")
    if not talent_q:
        return ExecResult(ok=False, message='Which talent? e.g. "Mark Shivi payment cleared" or "Mark Shivi\'s costume trial completed".')

    project_q = collected.get("project", "")
    if project_q:
        resolution = await _resolve_project(project_q, ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message=resolution.error)
        project = resolution.project
        body = await pd.get_production_desk(project["id"], _AGENT_ADMIN)
        match, others = _match_talent_by_name(talent_q, body["locked_talents"])
        if not match and not others:
            return ExecResult(ok=False, message=f'No locked talent matching "{talent_q}" on {project["label"]}.')
        if others:
            return ExecResult(ok=False, message=f'Multiple locked talents match "{talent_q}": {", ".join(t["name"] for t in others)}.')
    else:
        match, project_hit, other_labels = await _find_talent_across_projects(talent_q)
        if not match:
            if other_labels:
                return ExecResult(ok=False, message=f'Found "{talent_q}" locked on more than one project: {", ".join(other_labels)}. Please specify which one.')
            return ExecResult(ok=False, message=f'Couldn\'t find a locked talent matching "{talent_q}" on any ongoing project.')
        project = project_hit

    await _remember_project(ctx, project)
    collected["_resolved_project_id"] = project["id"]
    collected["_resolved_project_label"] = project["label"]
    collected["_resolved_talent_id"] = match["talent_id"]
    collected["_resolved_talent_name"] = match["name"]
    collected["_resolved_amount"] = match.get("budget_total")
    return None


async def _talent_status_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    return await _resolve_talent_for_mark(collected, ctx)


async def _talent_status_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    name = collected.get("_resolved_talent_name") or collected.get("talent")
    if collected.get("_kind") == "trial":
        return f"Mark {name}'s costume trial as completed?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    amount = collected.get("_resolved_amount")
    amount_txt = f" of {_format_inr(amount)}" if amount else ""
    return f"Mark {name}'s payment{amount_txt} as cleared?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."


async def _talent_status_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    pid = collected.get("_resolved_project_id")
    tid = collected.get("_resolved_talent_id")
    if not pid or not tid:
        return ExecResult(ok=False, message="Couldn't resolve the talent/project — please resend the command.")
    name = collected.get("_resolved_talent_name") or tid
    if collected.get("_kind") == "trial":
        try:
            await pd.update_locked_talent_production(
                pid, tid, pd.TalentProductionPatch(fitting_status="completed"), _AGENT_ADMIN
            )
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f"✓ {name}'s costume trial marked completed.")

    try:
        await pd.update_locked_talent_production(
            pid, tid, pd.TalentProductionPatch(payment_status="cleared"), _AGENT_ADMIN
        )
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
    return ExecResult(ok=True, message=f"✓ {name}'s payment marked cleared.")


MARK_TALENT_STATUS_INTENT = IntentDefinition(
    intent_id="management.mark_talent_status",
    triggers=["mark"],  # shortest — only wins when no longer "mark X" trigger above matches first
    fields=[
        FieldSpec(key="_kind", label="Kind", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="talent", label="Talent", question="Which talent?", validate=lambda v: ValidationResult(ok=True, value=v) if v else ValidationResult(ok=False, error="Please name the talent.")),
        FieldSpec(key="project", label="Project", question="Which project?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
    ],
    extract_fields=_talent_status_extract_fields,
    try_auto_execute=_talent_status_try_auto_execute,
    build_confirmation=_talent_status_build_confirmation,
    executor=_talent_status_executor,
)


# ===========================================================================
# WRITE — add reimbursement. "Add ₹5,000 travel reimbursement for Shivi."
# ===========================================================================
_REIMBURSEMENT_TYPE_RE = re.compile(r"reimbursement[:\s]*", re.IGNORECASE)
_REIMBURSEMENT_REASON_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d+)?\s+([a-zA-Z ]+?)\s+reimbursement", re.IGNORECASE
)
# Same broad "everything after the last 'for'" shape as _FOR_PROJECT_RE —
# a real name can contain a hyphen/apostrophe/etc., so this deliberately
# does NOT restrict to a letters-only character class.
_REIMBURSEMENT_TALENT_RE = re.compile(r"\bfor\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)


def _reimbursement_extract_fields(text: str) -> Dict[str, str]:
    amount = _extract_amount(text)
    reason_m = _REIMBURSEMENT_REASON_RE.search(text or "")
    talent_m = _REIMBURSEMENT_TALENT_RE.search(text or "")
    return {
        "amount": str(amount) if amount is not None else "",
        "reason": reason_m.group(1).strip() if reason_m else "expense",
        "talent": talent_m.group(1).strip() if talent_m else "",
    }


def _validate_amount(raw: str) -> ValidationResult:
    try:
        val = float((raw or "").replace(",", ""))
    except (TypeError, ValueError):
        return ValidationResult(ok=False, error="Please give an amount, e.g. ₹5,000.")
    if val <= 0:
        return ValidationResult(ok=False, error="Amount must be greater than zero.")
    return ValidationResult(ok=True, value=str(val))


# "Add" is shared by two different actions ("Add ₹5,000 travel
# reimbursement for Shivi" vs "Add Rahul as DOP") — the platform's
# trigger-matching picks ONE intent per trigger phrase (longest-match,
# first-registered-wins on a tie), so both live behind a SINGLE intent
# here with a small, deterministic (" as " present => crew) content
# check deciding which sub-flow runs — not two intents silently racing
# on an identical trigger word.
_ADD_CREW_RE = re.compile(r"^\s*add\s+(.+?)\s+as\s+(.+?)(?:\s+for\s+(.+?))?\s*[\.\?!]*$", re.IGNORECASE)


def _match_crew_role(raw: str) -> str:
    raw = (raw or "").strip()
    for role in pd.CREW_ROLES:
        if role.lower() == raw.lower():
            return role
    return raw.title() if raw else "Other"


def _add_extract_fields(text: str) -> Dict[str, str]:
    crew_m = _ADD_CREW_RE.match(text or "")
    if crew_m:
        return {
            "_kind": "crew",
            "name": crew_m.group(1).strip(),
            "role": crew_m.group(2).strip(),
            "project": (crew_m.group(3) or "").strip(),
        }
    fields = _reimbursement_extract_fields(text)
    fields["_kind"] = "reimbursement"
    return fields


async def _add_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if collected.get("_kind") == "crew":
        name = collected.get("name", "")
        if not name:
            return ExecResult(ok=False, message='Who should I add? e.g. "Add Rahul as DOP".')

        resolution = await _resolve_project(collected.get("project", ""), ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message=resolution.error)

        existing = await db.clients.find_one(
            {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "deleted": {"$ne": True}}
        )
        if existing:
            collected["_client_id"] = str(existing["_id"])
            collected["_client_name"] = existing["name"]
            collected["_client_is_new"] = "0"
        else:
            collected["_client_id"] = ""
            collected["_client_name"] = name
            collected["_client_is_new"] = "1"

        collected["_resolved_project_id"] = resolution.project["id"]
        collected["_resolved_project_label"] = resolution.project["label"]
        return None

    # kind == reimbursement
    talent_q = collected.get("talent", "")
    amount_raw = collected.get("amount", "")
    if not talent_q:
        return ExecResult(ok=False, message='Who is this reimbursement for? e.g. "Add ₹5,000 travel reimbursement for Shivi."')
    amount_check = _validate_amount(amount_raw)
    if not amount_check.ok:
        return ExecResult(ok=False, message=amount_check.error)
    collected["amount"] = amount_check.value

    match, project_hit, other_labels = await _find_talent_across_projects(talent_q)
    if not match:
        if other_labels:
            return ExecResult(ok=False, message=f'Found "{talent_q}" locked on more than one project: {", ".join(other_labels)}. Please specify which one.')
        return ExecResult(ok=False, message=f'Couldn\'t find a locked talent matching "{talent_q}" on any ongoing project.')

    await _remember_project(ctx, project_hit)
    collected["_resolved_project_id"] = project_hit["id"]
    collected["_resolved_project_label"] = project_hit["label"]
    collected["_resolved_talent_id"] = match["talent_id"]
    collected["_resolved_talent_name"] = match["name"]
    return None


async def _add_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get("_kind") == "crew":
        role = _match_crew_role(collected.get("role", ""))
        name = collected.get("_client_name") or collected.get("name")
        project_label = collected.get("_resolved_project_label", "")
        new_note = " (new CRM contact)" if collected.get("_client_is_new") == "1" else ""
        return f"Add {name} as {role} on {project_label}{new_note}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."

    name = collected.get("_resolved_talent_name") or collected.get("talent")
    amount = collected.get("amount")
    reason = collected.get("reason") or "expense"
    amount_txt = _format_inr(float(amount)) if amount else "—"
    return f"Add {amount_txt} {reason} reimbursement for {name}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."


async def _add_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    if collected.get("_kind") == "crew":
        pid = collected.get("_resolved_project_id")
        if not pid:
            return ExecResult(ok=False, message="Couldn't resolve the project — please resend the command.")
        client_id = collected.get("_client_id")
        if not client_id:
            doc = await insert_client_doc(
                name=collected.get("_client_name") or collected.get("name"),
                contact_type=None,
                source=f"whatsapp_agent:{AGENT_ID}",
            )
            client_id = doc["id"]
        role = _match_crew_role(collected.get("role", ""))
        try:
            await pd.add_crew(pid, pd.CrewIn(client_id=client_id, role=role), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't add crew member: {e.detail}")
        name = collected.get("_client_name") or collected.get("name")
        return ExecResult(ok=True, message=f"✓ {name} added as {role}.")

    pid = collected.get("_resolved_project_id")
    tid = collected.get("_resolved_talent_id")
    if not pid or not tid:
        return ExecResult(ok=False, message="Couldn't resolve the talent/project — please resend the command.")
    try:
        await pd.add_reimbursement(
            pid,
            talent_id=tid,
            expense_type=collected.get("reason") or "expense",
            amount=float(collected.get("amount") or 0),
            date=None,
            notes=f"Added via {AGENT_ID} WhatsApp command.",
            file=None,
            admin=_AGENT_ADMIN,
        )
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't add reimbursement: {e.detail}")
    name = collected.get("_resolved_talent_name") or tid
    return ExecResult(ok=True, message=f"✓ Reimbursement added for {name}.")


ADD_INTENT = IntentDefinition(
    intent_id="management.add",
    triggers=["add"],
    # All fields optional at the generic-engine level — which ones
    # actually matter depends on _kind (crew vs reimbursement), decided
    # in extract_fields; try_auto_execute does the real "is this actually
    # complete" check and returns a helpful error for whichever kind is
    # missing something, rather than the generic engine prompting for
    # fields that don't even apply to the other kind.
    fields=[
        # Set by extract_fields, never asked for — must be a declared
        # FieldSpec purely so the generic engine's initial extraction loop
        # (which only copies keys present in `fields`) actually carries
        # it into `collected` for try_auto_execute/build_confirmation/
        # executor to branch on.
        FieldSpec(key="_kind", label="Kind", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="project", label="Project", question="Which project?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="name", label="Name", question="Who should I add?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="role", label="Role", question="What role?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="talent", label="Talent", question="Who is this for?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="amount", label="Amount", question="What's the amount?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="reason", label="Reason", question="What's it for?", validate=lambda v: ValidationResult(ok=True, value=v or "expense"), required=False),
    ],
    extract_fields=_add_extract_fields,
    try_auto_execute=_add_try_auto_execute,
    build_confirmation=_add_build_confirmation,
    executor=_add_executor,
)


# ===========================================================================
# WRITE — task/reminder creation ("Add a task to get the call sheet
# tomorrow.", "Remind me to follow up with Google AI on Monday."). Writes
# through routers.workflow.create_task DIRECTLY — the SAME db.workflow_tasks
# collection the admin Workflow page and Production Desk's own task
# queries use. This is a passive, due-dated record a manager can see and
# complete from either surface; NOT an autonomous push notification (see
# module docstring — that's a deliberately deferred later pass).
# ===========================================================================
_ADD_TASK_TRIGGER_RE = re.compile(r"^add a task to\s+(.+)$", re.IGNORECASE)
_REMIND_TRIGGER_RE = re.compile(r"^remind me to\s+(.+)$", re.IGNORECASE)
_WITH_PROJECT_RE = re.compile(r"\bwith\s+(.+)$", re.IGNORECASE)
_FOR_PROJECT_TAIL_RE = re.compile(r"\bfor\s+(.+)$", re.IGNORECASE)


def _add_task_extract_fields(text: str) -> Dict[str, str]:
    m = _ADD_TASK_TRIGGER_RE.match(text or "") or _REMIND_TRIGGER_RE.match(text or "")
    if not m:
        return {}
    body = m.group(1)
    remaining, due_at = _strip_date_phrase(body)

    project_hint = ""
    with_m = _WITH_PROJECT_RE.search(remaining)
    for_m = _FOR_PROJECT_TAIL_RE.search(remaining)
    # Whichever of "with X" / "for X" appears — "Remind me to follow up
    # WITH Google AI on Monday" vs a hypothetical "...FOR Google AI" —
    # the project name is everything after that word; the title keeps
    # the full original phrase (readable in the task list either way).
    tail_m = with_m or for_m
    if tail_m:
        project_hint = tail_m.group(1).strip().rstrip(".")

    return {
        "title": remaining.strip().rstrip("."),
        "due_at": due_at or "",
        "project_hint": project_hint,
    }


async def _add_task_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    title = collected.get("title", "").strip()
    if not title:
        return ExecResult(ok=False, message='What should the task be? e.g. "Add a task to get the call sheet tomorrow."')

    project = None
    project_hint = collected.get("project_hint", "")
    if project_hint:
        resolution = await _resolve_project(project_hint, ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.project:
            project = resolution.project
        # A resolution error here is NOT fatal — "follow up" itself may
        # just be a task with no resolvable project mention; fall through
        # to the session fallback below rather than failing the whole command.
    if not project:
        session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
        last_id = (session or {}).get("last_project_id")
        if last_id:
            project = {"id": last_id, "label": (session or {}).get("last_project_label") or ""}

    collected["_resolved_project_id"] = project["id"] if project else ""
    collected["_resolved_project_label"] = project["label"] if project else ""
    return None


async def _add_task_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    title = collected.get("title")
    due_at = collected.get("due_at")
    project_label = collected.get("_resolved_project_label") or ""
    due_txt = f" (due {_format_due(due_at)})" if due_at else ""
    proj_txt = f" for {project_label}" if project_label else ""
    return f'Add task "{title}"{proj_txt}{due_txt}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'


async def _add_task_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    from routers import workflow as workflow_router

    title = collected.get("title")
    due_at = collected.get("due_at") or None
    pid = collected.get("_resolved_project_id") or None
    project_label = collected.get("_resolved_project_label") or ""
    synthetic_user = {"id": _AGENT_ADMIN["id"], "role": "admin"}
    payload = workflow_router.TaskIn(
        title=title,
        category="project" if pid else "general",
        project_id=pid,
        project_name=project_label,
        due_at=due_at,
        priority="normal",
    )
    try:
        await workflow_router.create_task(payload, synthetic_user)
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't add task: {e.detail}")
    due_txt = f" (due {_format_due(due_at)})" if due_at else ""
    return ExecResult(ok=True, message=f"✓ Task added: {title}{due_txt}")


ADD_TASK_INTENT = IntentDefinition(
    intent_id="management.add_task",
    triggers=["add a task", "remind me"],
    fields=[
        FieldSpec(key="title", label="Task", question="What should the task be?", validate=lambda v: ValidationResult(ok=True, value=v) if v else ValidationResult(ok=False, error="Please describe the task.")),
        FieldSpec(key="due_at", label="Due", question="When is it due?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="project_hint", label="Project", question="Which project, if any?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
    ],
    extract_fields=_add_task_extract_fields,
    try_auto_execute=_add_task_try_auto_execute,
    build_confirmation=_add_task_build_confirmation,
    executor=_add_task_executor,
)


HELP_TEXT = (
    "TALENTGRAM MANAGEMENT AGENT\n"
    "QUICK MANUAL\n\n"
    "READ (no confirmation needed):\n"
    '• "What\'s pending for Google AI?"\n'
    '• "Show locked talents for Google AI."\n'
    '• "What\'s the talent budget for Google AI?"\n'
    '• "What\'s our commission?" (uses the last project you asked about)\n'
    '• "Show Shivi\'s payment."\n'
    '• "Show reimbursements for Google AI."\n'
    '• "What\'s happening today?" / "...tomorrow?" (across all projects)\n'
    '• "What\'s happening today for Google AI?"\n'
    '• "What\'s pending for Shivi?"\n'
    '• "When is Shivi\'s costume trial?"\n'
    '• "When is Shivi shooting?"\n'
    '• "What payment follow-ups are due today?"\n\n'
    "ACTIONS (I'll ask you to confirm before doing anything):\n"
    '• "Mark invoice raised for Google AI."\n'
    '• "Mark invoice sent for Google AI."\n'
    '• "Mark client payment received for Google AI."\n'
    '• "Mark GST received for Google AI."\n'
    '• "Mark Shivi payment cleared."\n'
    '• "Mark Shivi\'s costume trial completed."\n'
    '• "Add ₹5,000 travel reimbursement for Shivi."\n'
    '• "Add Rahul as DOP." (for the project we were just discussing)\n'
    '• "Add a task to get the call sheet tomorrow."\n'
    '• "Remind me to follow up with Google AI on Monday." (creates a due-dated task — not an automatic WhatsApp reminder yet)\n\n'
    "Everything here reads and writes the SAME data Production Desk shows — "
    "nothing here is a separate copy."
)


MANAGEMENT_AGENT = AgentDefinition(
    agent_id=AGENT_ID,
    name="Talentgram Management Agent",
    module="production_desk",
    # Order matters: detect_trigger picks the LONGEST matching trigger
    # across every intent below, so more specific "mark invoice raised" /
    # "mark invoice sent" / etc. intents are declared before the bare
    # "mark" fallback (talent payment) — listing order itself doesn't
    # affect matching (longest-wins is explicit in parser.detect_trigger),
    # this ordering is purely for readability.
    intents=[
        STATUS_QUERY_INTENT,
        MARK_INVOICE_RAISED_INTENT,
        MARK_INVOICE_SENT_INTENT,
        MARK_PAYMENT_IN_INTENT,
        MARK_GST_RECEIVED_INTENT,
        MARK_TALENT_STATUS_INTENT,
        ADD_INTENT,
        ADD_TASK_INTENT,
    ],
    help_text=HELP_TEXT,
)


def register() -> None:
    register_agent(MANAGEMENT_AGENT)
