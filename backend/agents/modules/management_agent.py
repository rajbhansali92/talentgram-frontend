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

import json
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
# Entity-extraction stopwords — ROOT-CAUSE fix for a whole class of bug,
# not a one-off "the" special case. Every function in this file that
# extracts a talent/project NAME CANDIDATE from free text runs it through
# `_is_plausible_name` before treating it as one. Without this guard, a
# regex like "<verb> (.+?) <field keyword>" will happily capture an
# ordinary function word sitting between the verb and the keyword — e.g.
# "Set the shoot date for Google AI..." naturally captures "the" as
# whatever sits between "set" and "shoot" once "set" is registered as a
# trigger (confirmed by reproducing it: _TALENT_TOPIC_RE's non-greedy
# `(.+?)` matches "the" as the shortest string before hitting the "shoot"
# topic keyword). The fix is structural — reject ANY candidate that is
# empty or consists ENTIRELY of stopwords/function-words — not a literal
# denylist entry for "the" alone (a different sentence would just as
# easily produce "for"/"to"/"is"/etc. as the captured span).
# ---------------------------------------------------------------------------
_ENTITY_STOPWORDS = {
    "the", "a", "an", "for", "to", "on", "at", "in", "is", "are", "was",
    "were", "be", "been", "set", "add", "mark", "make", "move", "show",
    "please", "tomorrow", "today", "yesterday", "and", "or", "with",
    "from", "of", "as", "her", "his", "their", "our", "it", "its", "this",
    "that", "these", "those", "who", "what", "when", "where", "why",
    "how", "we", "us", "you", "your", "i", "me", "my", "he", "she",
    "will", "would", "can", "could", "should", "shall", "next", "then",
    "another", "also", "has", "have", "had",
    # Time/date words that otherwise superficially pass the bare
    # "starts with a letter" name shape ("...3 PM and Rahul's..." would
    # misparse "PM" as a plausible name candidate without this).
    "am", "pm", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
}


def _is_plausible_name(candidate: str) -> bool:
    """True only if `candidate` contains at least one token that ISN'T a
    stopword/function-word — the gate every talent/project/CRM name
    extraction in this file runs a candidate through before using it."""
    candidate = (candidate or "").strip()
    if len(candidate) < 2:
        return False
    words = re.findall(r"[A-Za-z0-9']+", candidate.lower())
    if not words:
        return False
    return any(w not in _ENTITY_STOPWORDS for w in words)


# ---------------------------------------------------------------------------
# Project resolution — reuses the exact fuzzy resolver ADD/MOVE/SHOW ME use,
# plus session_context for "no project named this turn" continuity.
# ---------------------------------------------------------------------------
_FOR_PROJECT_RE = re.compile(r"\bfor\s+(.+?)\s*[\?\.!]*$", re.IGNORECASE)


def _extract_trailing_project(text: str) -> str:
    m = _FOR_PROJECT_RE.search(text or "")
    candidate = m.group(1).strip() if m else ""
    return candidate if _is_plausible_name(candidate) else ""


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


async def _remember_task(ctx: ExecContext, task_id: str, title: str) -> None:
    """Phase H / P0-H — "it"/"that task" resolution. Reuses the SAME
    session_context store _remember_project already writes to (domain-
    agnostic — see that module's docstring), just two more fields on the
    same document. No second context store."""
    if not task_id:
        return
    await session_context.update_session(
        AGENT_ID, ctx.sender_phone, last_task_id=task_id, last_task_title=title or "",
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
    return name if _is_plausible_name(name) else ""


_TALENT_TOPIC_RE = re.compile(
    r"^(?:when is|when's|show|what's|whats|is|mark)\s+(.+?)\s+(costume trial|trial|fitting|look test|shoot(?:ing)?|payment|reimbursement|ready)\b",
    re.IGNORECASE,
)
# Phase I — "What is pending for Shivi?" (the spec's own example phrasing
# for the talent-readiness summary): name comes AFTER the topic word here,
# the reverse order of _TALENT_TOPIC_RE above. Deliberately NOT wired into
# _extract_talent_and_topic itself — "pending for X" is ALSO the existing,
# pre-established project-digest phrasing ("What's pending for Google AI?"),
# and _extract_talent_and_topic is checked BEFORE project resolution in
# _status_query_executor, so unconditionally treating "pending for X" as a
# talent name here shadowed that older command (found live in regression
# testing). Instead this is only consulted from the EXISTING "project
# resolution already, definitively failed -> try X as a bare talent name"
# fallback below, matching that fallback's own established guardrail.
_TALENT_PENDING_FOR_RE = re.compile(
    r"^(?:what(?:'s| is)\s+)?pending for\s+(.+?)\??$",
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
    if not _is_plausible_name(name):
        return "", None
    topic_raw = m.group(2).lower()
    if "trial" in topic_raw or "fitting" in topic_raw or "look test" in topic_raw:
        topic = "trial"
    elif "shoot" in topic_raw:
        topic = "shoot"
    elif "payment" in topic_raw:
        topic = "payment"
    elif "reimbursement" in topic_raw:
        topic = "reimbursement"
    elif "ready" in topic_raw:
        topic = "readiness"
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


_TIME_OF_DAY_WORD_RE = re.compile(r"\s*\b(?:morning|afternoon|evening|night)\b", re.IGNORECASE)


def _strip_date_phrase(text: str) -> Tuple[str, Optional[str]]:
    """Finds the LAST date word in the text (matching how these commands
    are phrased — "...to get the call sheet tomorrow", "...on Monday"),
    parses it, and returns the text with that phrase (plus a leading "on"
    if present, and a trailing time-of-day word if present — "tomorrow
    morning" is one date phrase, not "tomorrow" + a dangling "morning" in
    the title) removed. (remaining_text, due_at_or_None)."""
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
    tail = text[m.end():]
    tod_m = _TIME_OF_DAY_WORD_RE.match(tail)
    if tod_m:
        tail = tail[tod_m.end():]
    remaining = (text[:start] + tail).strip().rstrip(".").strip()
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
    # "tomorrow" maps here too — "What's happening tomorrow for Google AI?"
    # (project-scoped) renders the same Upcoming section the project-less
    # global digest shows for "tomorrow" (everything after today, not
    # filtered to exactly tomorrow's date — an honest, if slightly
    # broader, answer rather than nothing).
    "upcoming": ("upcoming", "what's next", "whats next", "tomorrow"),
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


async def _global_needs_attention() -> str:
    """Phase H / P0-I, expanded in Phase I — "What needs attention?" /
    "What's overdue?" / "Which talents aren't ready?" / "Anything
    urgent?" — a READ/QUERY layer only, aggregating the SAME structured
    signals _global_digest and the reminder worker already read directly
    (no N+1 per-project fan-out, no new store — see _global_digest's own
    docstring for why direct signal queries, not project iteration, is
    this file's established pattern for a cross-project view). Reuses
    services/production_reminder_worker.py's own date helpers and
    _shoot_pending_items — one place computes "what's pending on this
    shoot", shared by the reminder worker, this query, and the daily
    briefing, not three separate implementations."""
    from services import production_reminder_worker as reminder_worker

    now_iso = datetime.now(timezone.utc).isoformat()
    today = reminder_worker._ist_today()
    tomorrow = today + timedelta(days=1)

    relevant_ids = {p["id"] for p in await db.projects.find(
        {"status": {"$in": PRODUCTION_DESK_RELEVANT_STATUSES}}, {"_id": 0, "id": 1},
    ).to_list(10000)}
    rid_list = list(relevant_ids)

    active_tasks = await db.workflow_tasks.find(
        {"due_at": {"$ne": None}, "status": {"$in": _ACTIVE_STATUSES}}, {"_id": 0},
    ).to_list(1000)
    overdue_tasks = [t for t in active_tasks if t["due_at"] < now_iso]
    due_today_tasks = [t for t in active_tasks if reminder_worker._date_of(t["due_at"]) == today and t["due_at"] >= now_iso]
    high_priority_tasks = [t for t in active_tasks if (t.get("priority") or "").lower() == "high"]

    overdue_followups = await db.projects.find(
        {"pd_next_follow_up_at": {"$lt": now_iso, "$ne": None}, "pd_payment_followup_status": {"$ne": "done"},
         "status": {"$in": PRODUCTION_DESK_RELEVANT_STATUSES}},
        {"_id": 0, "id": 1, "brand_name": 1},
    ).to_list(200)
    # "Not ready": locked, but fitting/look-test still open, or no
    # costume trial on file at all yet.
    not_ready_rows = await db.casting_pipeline.find(
        {"stage": "locked", "$or": [
            {"pd_fitting_status": {"$in": [None, "not_scheduled", "scheduled"]}},
            {"pd_costume_trial_at": None},
        ]},
        {"_id": 0},
    ).to_list(1000)
    pending_payment_rows = await db.casting_pipeline.find(
        {"stage": "locked", "pd_payment_status": {"$in": [None, "pending"]}}, {"_id": 0},
    ).to_list(1000)
    not_ready_rows = [r for r in not_ready_rows if r.get("project_id") in relevant_ids]
    pending_payment_rows = [r for r in pending_payment_rows if r.get("project_id") in relevant_ids]

    checklist_gap_projects = await db.projects.find(
        {"id": {"$in": rid_list}, "$or": [
            {"pd_confirmation_mail_received": {"$ne": True}},
            {"pd_invoice_raised": {"$ne": True}},
            {"pd_invoice_sent": {"$ne": True}},
            {"pd_gst_component_received": {"$ne": True}},
        ]},
        {"_id": 0},
    ).to_list(len(rid_list)) if rid_list else []

    all_projects = await db.projects.find({"id": {"$in": rid_list}}, {"_id": 0}).to_list(len(rid_list)) if rid_list else []
    shoots_today = [p for p in all_projects if reminder_worker._date_of(p.get("pd_shoot_date")) == today]
    shoots_tomorrow = [p for p in all_projects if reminder_worker._date_of(p.get("pd_shoot_date")) == tomorrow]

    project_ids = {t.get("project_id") for t in overdue_tasks + due_today_tasks + high_priority_tasks}
    project_ids |= {p["id"] for p in overdue_followups + checklist_gap_projects + shoots_today + shoots_tomorrow}
    project_ids |= {r.get("project_id") for r in not_ready_rows + pending_payment_rows}
    project_ids.discard(None)
    projects = await db.projects.find({"id": {"$in": list(project_ids)}}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(len(project_ids)) if project_ids else []
    label_by_id = {p["id"]: p.get("brand_name") or "(untitled project)" for p in projects}

    talent_ids = list({r["talent_id"] for r in not_ready_rows + pending_payment_rows if r.get("talent_id")})
    talent_docs = await db.talents.find({"id": {"$in": talent_ids}}, {"_id": 0, "id": 1, "name": 1}).to_list(len(talent_ids)) if talent_ids else []
    talent_name_by_id = {t["id"]: t.get("name") or "Talent" for t in talent_docs}

    sections: Dict[str, List[str]] = {}

    def _add(project_id: Optional[str], line: str) -> None:
        if not project_id:
            return
        sections.setdefault(project_id, []).append(line)

    for p in shoots_today:
        _add(p["id"], "  • Shoot today")
    for p in shoots_tomorrow:
        _add(p["id"], "  • Shoot tomorrow")
    for t in overdue_tasks:
        _add(t.get("project_id"), f"  • Overdue task: {t.get('title')}")
    for t in due_today_tasks:
        _add(t.get("project_id"), f"  • Task due today: {t.get('title')}")
    for t in high_priority_tasks:
        _add(t.get("project_id"), f"  • High priority task: {t.get('title')}")
    for p in overdue_followups:
        _add(p["id"], "  • Payment follow-up overdue")
    for p in checklist_gap_projects:
        gaps = [label for flag, label in (
            ("pd_confirmation_mail_received", "confirmation mail pending"),
            ("pd_invoice_raised", "invoice not raised"),
            ("pd_invoice_sent", "invoice not sent"),
            ("pd_gst_component_received", "GST component pending"),
        ) if not p.get(flag)]
        if gaps:
            _add(p["id"], f"  • Checklist: {', '.join(gaps)}")
    for r in pending_payment_rows:
        _add(r.get("project_id"), f"  • Payment pending — {talent_name_by_id.get(r.get('talent_id'), 'Talent')}")
    for r in not_ready_rows:
        _add(r.get("project_id"), f"  • Not ready — {talent_name_by_id.get(r.get('talent_id'), 'Talent')} (costume trial/fitting pending)")

    if not sections:
        return "✓ Nothing needs attention right now."
    lines = ["⚠ NEEDS ATTENTION"]
    for project_id, items in sections.items():
        lines.append("")
        lines.append(label_by_id.get(project_id, project_id))
        lines.extend(items)
    return "\n".join(lines)


_NEEDS_ATTENTION_RE = re.compile(
    r"needs? attention|which projects? need|which talents?.*(?:not ready|aren.t ready)|who isn.t ready|"
    r"who is not ready|talents? who aren.t ready|not ready for|production tasks?.*overdue|^what.s overdue|^what is overdue|"
    r"projects? with pending payments?|anything urgent|urgent\??$|"
    r"today.s production issues|what.s pending today|what is pending today|follow up on\??$|what.*follow up on",
    re.IGNORECASE,
)


def _talent_readiness_issues(talent: dict) -> List[str]:
    """P1 — derived purely from EXISTING Production Desk fields, no new
    readiness collection/status of its own. "Confirmation" reuses the
    EXISTING shoot_status enum's own "scheduled"/"today"/"completed"
    values as the confirmed signal (Phase F's own "confirmed for the
    shoot" -> shoot_status="scheduled" mapping) — not a new field."""
    issues = []
    if (talent.get("shoot_status") or "not_scheduled") == "not_scheduled":
        issues.append("confirmation pending")
    if not talent.get("costume_trial_at"):
        issues.append("costume trial not scheduled")
    if (talent.get("fitting_status") or "not_scheduled") != "completed":
        issues.append(f"fitting {(talent.get('fitting_status') or 'not_scheduled').replace('_', ' ')}")
    if (talent.get("look_test_status") or "not_scheduled") != "completed":
        issues.append(f"look test {(talent.get('look_test_status') or 'not_scheduled').replace('_', ' ')}")
    return issues


def _render_talent_reply(
    talent: dict, project_label: str, topic: Optional[str],
    call_time: Optional[str] = None, reporting_time: Optional[str] = None,
) -> str:
    lines = [f"👤 {talent['name']} — {project_label}", ""]
    if topic == "readiness":
        issues = _talent_readiness_issues(talent)
        confirmed = (talent.get("shoot_status") or "not_scheduled") != "not_scheduled"
        lines.append(f"Confirmation: {'Done' if confirmed else 'Pending'}")
        lines.append(f"Costume Trial: {'Done' if talent.get('costume_trial_at') else 'Pending'}")
        lines.append(f"Fitting: {'Done' if talent.get('fitting_status') == 'completed' else 'Pending'}")
        lines.append(f"Look Test: {'Done' if talent.get('look_test_status') == 'completed' else 'Pending'}")
        if reporting_time:
            lines.append(f"Reporting: {reporting_time}")
        if call_time:
            lines.append(f"Call: {call_time}")
        lines.append(f"Status: {'READY' if not issues else 'NOT READY'}")
    elif topic == "trial":
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
    r"(?:happening|payment follow-?ups?(?:\s+are|\s+due)?|due|reminders?)\s*.*?\b(today|tomorrow)\b",
    re.IGNORECASE,
)


_SHOOTS_QUERY_RE = re.compile(r"\bshoot(?:s|ing)?\b", re.IGNORECASE)
_SHOOTS_TIMEFRAME_RE = re.compile(r"\b(today|tomorrow|this week|coming up|upcoming)\b", re.IGNORECASE)
# "What is happening this week?" (spec's own example phrasing) never says
# "shoot" — treat it as an alternate entry point into the same renderer.
# Deliberately restricted to "this week"/"coming up"/"upcoming" ONLY, never
# "today"/"tomorrow" — "What's happening today/tomorrow?" is the PRE-
# EXISTING _GLOBAL_DIGEST_RE trigger (Phase F/G), and this function is
# checked earlier in _status_query_executor, so an unrestricted "happening"
# match here would silently shadow that older, broader digest command
# (found live in regression testing: it started answering a plain
# alphabetical-position digest test with an empty shoots-only reply).
_SHOOTS_HAPPENING_RE = re.compile(r"\bhappening\b.*\b(this week|coming up|upcoming)\b", re.IGNORECASE)


async def _render_shoots_query(raw: str) -> Optional[str]:
    """Phase I — "What shoots are today/tomorrow?" / "Show me tomorrow's
    shoots." / "Who is shooting tomorrow?" / "What is happening this
    week?" — a global (cross-project) query. A project- OR talent-
    scoped "shoot" question ("What's the Google AI shoot date?", "When
    is Shivi shooting?") already works via the EXISTING "shoot" focus
    keyword / _extract_talent_and_topic and is deliberately left to that
    path — this function steps aside the moment EITHER a project or a
    talent name is mentioned (found live in regression testing: "When is
    Shivi shooting?" was being wrongly swallowed here before this
    check)."""
    has_shoot_word = bool(_SHOOTS_QUERY_RE.search(raw or ""))
    has_happening_word = bool(_SHOOTS_HAPPENING_RE.search(raw or ""))
    if not (has_shoot_word or has_happening_word):
        return None
    if _extract_trailing_project(raw):
        return None
    talent_hint, _topic = _extract_talent_and_topic(raw)
    if talent_hint:
        return None
    from services import production_reminder_worker as reminder_worker

    today = reminder_worker._ist_today()
    tf_m = _SHOOTS_TIMEFRAME_RE.search(raw)
    timeframe = (tf_m.group(1).lower() if tf_m else "coming up")
    if timeframe == "today":
        start, end, label = today, today, "TODAY"
    elif timeframe == "tomorrow":
        d = today + timedelta(days=1)
        start, end, label = d, d, "TOMORROW"
    elif timeframe == "this week":
        start, end, label = today, today + timedelta(days=6), "THIS WEEK"
    else:
        start, end, label = today, today + timedelta(days=13), "UPCOMING"

    relevant_ids = {p["id"] for p in await db.projects.find(
        {"status": {"$in": PRODUCTION_DESK_RELEVANT_STATUSES}}, {"_id": 0, "id": 1},
    ).to_list(10000)}
    rid_list = list(relevant_ids)
    projects = await db.projects.find(
        {"id": {"$in": rid_list}, "pd_shoot_date": {"$ne": None}}, {"_id": 0},
    ).to_list(len(rid_list)) if rid_list else []
    matches = [p for p in projects if p.get("pd_shoot_date") and start <= reminder_worker._date_of(p["pd_shoot_date"]) <= end]

    if not matches:
        return f"📋 SHOOTS — {label}\n\nNothing scheduled."
    matches.sort(key=lambda p: reminder_worker._date_of(p["pd_shoot_date"]))
    lines = [f"📋 SHOOTS — {label}"]
    for p in matches:
        d = reminder_worker._date_of(p["pd_shoot_date"])
        talent_count = await db.casting_pipeline.count_documents({"project_id": p["id"], "stage": "locked"})
        pending = await reminder_worker._shoot_pending_items(p)
        status = "✓ Ready" if not pending else f"⚠ {len(pending)} pending"
        lines.append("")
        lines.append(f"{p.get('brand_name') or '(untitled project)'} — {reminder_worker._fmt_date(d)}")
        if p.get("pd_shoot_location"):
            lines.append(f"  Location: {p['pd_shoot_location']}")
        if p.get("pd_reporting_time"):
            lines.append(f"  Reporting: {p['pd_reporting_time']}")
        if p.get("pd_call_time"):
            lines.append(f"  Call: {p['pd_call_time']}")
        lines.append(f"  Talents: {talent_count}  ·  {status}")
    return "\n".join(lines)


_TASK_ASSIGNED_RE = re.compile(r"assigned to\s+(.+?)\s*\??$", re.IGNORECASE)
_MY_PENDING_TASKS_RE = re.compile(r"my pending tasks|show.*pending tasks", re.IGNORECASE)


async def _render_tasks_query(raw: str) -> Optional[str]:
    """Phase H / P0-H — global (cross-project) task queries: "Show my
    pending tasks." / "What tasks are assigned to Rahul?" Project-scoped
    task queries ("Show Google AI tasks.") already work via the existing
    "tasks" focus keyword below — this only covers the two shapes that
    genuinely have no project to resolve."""
    m = _TASK_ASSIGNED_RE.search(raw)
    if m:
        hint = _trim_trailing_stopwords(m.group(1).strip())
        user = await db.users.find_one({"name": {"$regex": re.escape(hint), "$options": "i"}}, {"_id": 0, "id": 1, "name": 1})
        if not user:
            return f'No team member matching "{hint}" found.'
        tasks = await db.workflow_tasks.find(
            {"assignee_id": user["id"], "status": {"$in": _ACTIVE_STATUSES}}, {"_id": 0},
        ).sort("due_at", 1).to_list(100)
        if not tasks:
            return f"No pending tasks assigned to {user['name']}."
        lines = [f"TASKS — {user['name']}"]
        for t in tasks:
            proj = f" ({t['project_name']})" if t.get("project_name") else ""
            lines.append(f"  • {t['title']}{proj} — due {_format_due(t.get('due_at'))} — {t['status'].upper()}")
        return "\n".join(lines)

    if _MY_PENDING_TASKS_RE.search(raw):
        tasks = await db.workflow_tasks.find(
            {"status": {"$in": _ACTIVE_STATUSES}}, {"_id": 0},
        ).sort("due_at", 1).to_list(200)
        if not tasks:
            return "No pending tasks."
        lines = ["PENDING TASKS"]
        for t in tasks:
            proj = f" ({t['project_name']})" if t.get("project_name") else ""
            lines.append(f"  • {t['title']}{proj} — due {_format_due(t.get('due_at'))}")
        return "\n".join(lines)
    return None


async def _status_query_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    raw = collected.get("raw_text", "")

    # "What shoots are today/tomorrow?" / "Who is shooting tomorrow?" —
    # checked first since it's the most specific keyword ("shoot") of
    # the global queries below.
    shoots_reply = await _render_shoots_query(raw)
    if shoots_reply:
        return ExecResult(ok=True, message=shoots_reply)

    # "Show my pending tasks." / "What tasks are assigned to Rahul?" —
    # checked before the needs-attention/project branches since these
    # never name a project either.
    tasks_reply = await _render_tasks_query(raw)
    if tasks_reply:
        return ExecResult(ok=True, message=tasks_reply)

    # "What needs attention?" / "What's overdue?" / "Which talents aren't
    # ready?" — a global, cross-project query; checked before the
    # project-scoped branches below since these never name a project.
    if _NEEDS_ATTENTION_RE.search(raw) and not _extract_trailing_project(raw):
        return ExecResult(ok=True, message=await _global_needs_attention())

    # "What is the kickback for Rahul?" — checked first since "kickback"
    # is a specific, unambiguous keyword that can't collide with any
    # other query shape below.
    kb_m = _KICKBACK_QUERY_RE.search(raw)
    if kb_m:
        hint = _trim_trailing_stopwords(kb_m.group(1).strip())
        if _is_plausible_name(hint):
            resolution = await _resolve_project("", ctx)
            if resolution.project:
                row = await _find_kickback_row(resolution.project["id"], hint)
                if row:
                    return ExecResult(ok=True, message=f"{row.get('recipient_name') or hint}'s kickback on {resolution.project['label']}: {_format_inr(pd._num(row.get('amount')) or 0)}.")
                return ExecResult(ok=True, message=f'No kickback on file for "{hint}" on {resolution.project["label"]}.')

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
            call_time = reporting_time = None
            if topic == "readiness":
                proj_doc = await db.projects.find_one({"id": project["id"]}, {"_id": 0, "pd_call_time": 1, "pd_reporting_time": 1})
                call_time = (proj_doc or {}).get("pd_call_time")
                reporting_time = (proj_doc or {}).get("pd_reporting_time")
            return ExecResult(ok=True, message=_render_talent_reply(talent, project["label"], topic, call_time, reporting_time))
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
                # "What is pending for Shivi?" — Phase I's own example
                # phrasing for the talent-readiness summary; project
                # resolution has already, definitively failed above, so
                # "pending for X" now safely means "X is a talent".
                pf_topic = "readiness" if _TALENT_PENDING_FOR_RE.match(raw.strip()) else None
                call_time = reporting_time = None
                if pf_topic == "readiness":
                    proj_doc = await db.projects.find_one({"id": project["id"]}, {"_id": 0, "pd_call_time": 1, "pd_reporting_time": 1})
                    call_time = (proj_doc or {}).get("pd_call_time")
                    reporting_time = (proj_doc or {}).get("pd_reporting_time")
                return ExecResult(ok=True, message=_render_talent_reply(talent, project["label"], pf_topic, call_time, reporting_time))
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
            ct = body["project"].get("pd_call_time") if topic == "readiness" else None
            rt = body["project"].get("pd_reporting_time") if topic == "readiness" else None
            return ExecResult(ok=True, message=_render_talent_reply(match, project["label"], topic, ct, rt))
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
        # Phase G (Production Reminders) — "what's due"/"what reminders"
        # are the natural phrasing for reading the same due-today/
        # due-tomorrow digest the reminder worker itself acts on.
        "what's due", "whats due", "what is due", "what reminders", "reminders",
        # Phase H — checklist-focused phrasing (same "pending" render the
        # existing "what's pending" trigger already produces).
        "which checklist", "checklist items", "which projects need attention",
        "which talents", "who isn't ready", "who is not ready",
        "what needs attention", "needs attention", "what's overdue", "whats overdue", "what is overdue",
        "show me projects", "show me all talents",
        "my pending tasks", "show my pending", "what tasks are assigned", "what tasks",
        # Phase I — proactive-ops query phrasing (Section "Success Criteria").
        # Note: several of these are short, terminal phrases ("Anything
        # urgent?") that a trailing "?" with no following space keeps
        # detect_trigger's own prefix-match from ever catching (see the
        # Phase H "Mark it complete." bug write-up) — they're ALSO
        # covered by _NEEDS_ATTENTION_RE inside resolve_bare_reply below,
        # so registering the trigger here is a belt-and-suspenders extra,
        # not the only path; a genuinely bare "urgent" is deliberately
        # NOT a trigger (too broad on its own).
        "anything urgent", "show me today's production issues",
        "what do i need to follow up on", "what's happening this week", "whats happening this week",
        "what is happening this week", "who is shooting", "who's shooting", "what shoots",
        "show me tomorrow's shoots", "show me today's shoots",
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
_MARK_LIFECYCLE_RE = re.compile(r"^mark\s+(.+?)\s+as\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_LIFECYCLE_AS_MAP = {
    "confirmed": "confirmed",
    "shoot scheduled": "shoot_scheduled",
    "shoot complete": "shoot_complete", "shoot completed": "shoot_complete",
    "finance closed": "finance_closed",
    "not started": "not_started",
}


def _talent_status_extract_fields(text: str) -> Dict[str, str]:
    # "Mark Google AI as shoot scheduled." is a PROJECT lifecycle
    # statement, not a talent-status one — checked first, since
    # _TALENT_TOPIC_RE's bare "mark X shoot..." shape would otherwise
    # misparse "Google AI as" as a talent name (the "shoot" in "shoot
    # scheduled" looks like the existing shoot-topic word to that
    # regex). Only redirected when the "as ..." phrase actually maps to
    # a known lifecycle value — an unrecognised "mark X as Y" still
    # falls through to the normal talent-status handling below.
    lifecycle_m = _MARK_LIFECYCLE_RE.match(text or "")
    if lifecycle_m:
        status = _LIFECYCLE_AS_MAP.get(lifecycle_m.group(2).strip().lower())
        project_hint = _trim_trailing_stopwords(lifecycle_m.group(1).strip())
        if status and _is_plausible_name(project_hint):
            return {"_kind": "lifecycle", "project": project_hint, "lifecycle_value": status}

    talent, topic = _extract_talent_and_topic(text)
    if topic == "trial":
        return {"project": _extract_trailing_project(text), "talent": talent, "_kind": "trial"}
    if topic == "reimbursement":
        return {"project": _extract_trailing_project(text), "talent": talent, "_kind": "reimbursement_paid"}

    # "Mark it complete." / "Mark that task complete." — the bare "mark"
    # trigger (this intent's own, shortest, fallback trigger) always
    # wins the dispatch for ANY "mark ..." opener, so TASK_MANAGE_INTENT's
    # own "mark it complete"/"mark it done" triggers can never be reached
    # when the sentence ends right there with no space after (a trailing
    # "." fails detect_trigger's own startswith(trigger+" ") check — see
    # its own docstring). Checked ONLY after the established topic
    # detection above finds nothing — "Mark Shivi's costume trial
    # completed." must keep resolving to _kind="trial" exactly as before,
    # never redirected here. Regex-shape-only (extract_fields must stay
    # sync/DB-free); try_auto_execute verifies a real task actually
    # exists before committing to this kind, falling back to the normal
    # payment-clear handling below otherwise — never a guess.
    task_m = _TASK_COMPLETE_RE.match(text or "") if text and text.strip().lower().startswith("mark") else None
    if task_m:
        return {"_kind": "task_action", "task_hint": task_m.group(1)}

    # default to payment for bare "mark X payment cleared"
    return {"project": _extract_trailing_project(text), "talent": talent, "_kind": "payment"}


async def _resolve_talent_for_mark(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    """Shared resolution for both kinds — populates _resolved_* on
    success, returns an ExecResult to short-circuit on failure."""
    talent_q = collected.get("talent", "")
    if not talent_q or not _is_plausible_name(talent_q):
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
    if collected.get("_kind") == "lifecycle":
        resolution = await _resolve_project(collected.get("project", ""), ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message=resolution.error)
        await _remember_project(ctx, resolution.project)
        collected["_resolved_project_id"] = resolution.project["id"]
        collected["_resolved_project_label"] = resolution.project["label"]
        return None

    if collected.get("_kind") == "task_action":
        hint = collected.get("task_hint", "")
        task, err = await _resolve_task_reference(hint, ctx)
        if task:
            collected["_task_id"] = task["id"]
            collected["_task_title"] = task.get("title") or ""
            return None
        # "it"/"that task"/"the task" with nothing in session context is
        # NEVER a plausible talent name — surface the real "which task?"
        # error directly rather than letting a pronoun fall through into
        # a talent-name search (found live in production testing:
        # "Mark it complete." with no task in context was silently
        # re-interpreted as a search for a talent literally named "it",
        # which matched unrelated real talents via a bare substring
        # regex — a genuine entity-resolution gap this closes).
        if _clean_task_hint(hint) in _TASK_PRONOUNS:
            return ExecResult(ok=False, message=err or 'Which task do you mean?')
        # Otherwise: a genuine title-hint miss — this was never a task
        # command at all (e.g. an unrecognised "mark X complete" about
        # something else); fall through to the ordinary payment-clear
        # handling below, exactly as an unrecognised "mark X ..." always
        # has, rather than surfacing a confusing "no task found" error.
        collected["_kind"] = "payment"
        collected["talent"] = hint

    err = await _resolve_talent_for_mark(collected, ctx)
    if err:
        return err
    if collected.get("_kind") == "reimbursement_paid":
        pid, tid = collected["_resolved_project_id"], collected["_resolved_talent_id"]
        rows = await db.project_reimbursements.find(
            {"project_id": pid, "talent_id": tid, "status": "pending"}, {"_id": 0}
        ).sort("created_at", -1).to_list(1)
        if not rows:
            name = collected.get("_resolved_talent_name")
            return ExecResult(ok=False, message=f"No pending reimbursement found for {name} on {collected['_resolved_project_label']}.")
        collected["_reimbursement_id"] = rows[0]["id"]
        collected["_reimbursement_amount"] = str(rows[0].get("amount") or 0)
        collected["_reimbursement_type"] = rows[0].get("expense_type") or "expense"
    return None


async def _talent_status_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get("_kind") == "lifecycle":
        status_label = collected.get("lifecycle_value", "").replace("_", " ")
        return f"Mark {collected.get('_resolved_project_label')} as {status_label}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    if collected.get("_kind") == "task_action":
        return f'Mark task "{collected.get("_task_title")}" complete?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'
    name = collected.get("_resolved_talent_name") or collected.get("talent")
    if collected.get("_kind") == "trial":
        return f"Mark {name}'s costume trial as completed?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    if collected.get("_kind") == "reimbursement_paid":
        amount_txt = _format_inr(float(collected.get("_reimbursement_amount") or 0))
        reason = collected.get("_reimbursement_type", "expense")
        return f"Mark {name}'s {reason} reimbursement ({amount_txt}) as paid?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    amount = collected.get("_resolved_amount")
    amount_txt = f" of {_format_inr(amount)}" if amount else ""
    return f"Mark {name}'s payment{amount_txt} as cleared?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."


async def _talent_status_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    if collected.get("_kind") == "lifecycle":
        pid = collected.get("_resolved_project_id")
        status = collected.get("lifecycle_value")
        try:
            await pd.update_production_desk_project(pid, pd.ProductionDeskProjectPatch(production_status=status), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f"✓ {collected.get('_resolved_project_label')} marked {status.replace('_', ' ')}.")

    if collected.get("_kind") == "task_action":
        from routers import workflow as workflow_router
        tid_task = collected.get("_task_id")
        title = collected.get("_task_title", "")
        await _remember_task(ctx, tid_task, title)
        try:
            await workflow_router.update_task(tid_task, workflow_router.TaskUpdateIn(status="completed"), {"id": _AGENT_ADMIN["id"], "role": "admin"})
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f'✓ Task "{title}" marked complete.')

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

    if collected.get("_kind") == "reimbursement_paid":
        rid = collected.get("_reimbursement_id")
        try:
            await pd.update_reimbursement_status(pid, rid, pd.ReimbursementStatusPatch(status="paid"), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f"✓ {name}'s reimbursement marked paid.")

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
        # Not required at the generic-engine level any more (Phase H): the
        # "lifecycle" kind (project-level, no talent at all) needs this
        # NOT to block before try_auto_execute runs — _resolve_talent_for_mark
        # already returns its own "Which talent?" error for the other kinds
        # when talent is genuinely missing, so no coverage is lost.
        FieldSpec(key="talent", label="Talent", question="Which talent?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="project", label="Project", question="Which project?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="lifecycle_value", label="Status", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="task_hint", label="Task", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
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
    talent_candidate = talent_m.group(1).strip() if talent_m else ""
    return {
        "amount": str(amount) if amount is not None else "",
        "reason": reason_m.group(1).strip() if reason_m else "expense",
        "talent": talent_candidate if _is_plausible_name(talent_candidate) else "",
    }


# "Shivi's travel reimbursement is 2500." — bare (no "add"), so only
# reachable via resolve_bare_reply; feeds ADD_INTENT's SAME reimbursement
# branch as "Add ₹2,500 travel reimbursement for Shivi." (identical
# collected shape, different entry point).
_REIMBURSEMENT_POSSESSIVE_RE = re.compile(
    r"(.+?)'s\s+([a-zA-Z ]+?)\s+reimbursement\s+is\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE
)


def _bare_reimbursement_fields(text: str) -> Optional[Dict[str, str]]:
    m = _REIMBURSEMENT_POSSESSIVE_RE.search(text or "")
    if not m:
        return None
    talent = _trim_leading_stopwords(m.group(1).strip())
    if not _is_plausible_name(talent):
        return None
    return {"_kind": "reimbursement", "amount": m.group(3), "reason": m.group(2).strip() or "expense", "talent": talent}


# "Change Amit's role to line producer." / "Remove Amit from the project
# crew." — neither "change" nor "remove" is a registered trigger word
# anywhere else, so both only ever reach resolve_bare_reply.
_CREW_ROLE_CHANGE_RE = re.compile(r"change\s+(.+?)'s\s+role\s+to\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_CREW_REMOVE_RE = re.compile(r"remove\s+(.+?)\s+from\s+(?:the\s+)?(?:project\s+)?crew\b", re.IGNORECASE)


def _bare_crew_command_fields(text: str) -> Optional[Dict[str, str]]:
    m = _CREW_ROLE_CHANGE_RE.search(text or "")
    if m:
        name = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(name):
            return {"_kind": "crew_update", "name": name, "role": m.group(2).strip()}
    m = _CREW_REMOVE_RE.search(text or "")
    if m:
        name = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(name):
            return {"_kind": "crew_remove", "name": name}
    return None


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


async def _find_crew_row(pid: str, name_hint: str) -> Optional[dict]:
    """Crew rows only store client_id — resolve each candidate's CRM name
    to match against, same as the read side's own crew rendering."""
    rows = await db.project_crew.find({"project_id": pid}, {"_id": 0}).to_list(200)
    if not rows:
        return None
    from bson import ObjectId
    from bson.errors import InvalidId
    hint_low = (name_hint or "").strip().lower()
    for row in rows:
        try:
            client = await db.clients.find_one({"_id": ObjectId(row["client_id"])}, {"_id": 0, "name": 1})
        except (InvalidId, TypeError):
            client = None
        name = ((client or {}).get("name") or "").strip().lower()
        if name and (name == hint_low or hint_low in name or name in hint_low):
            row["_client_name"] = client["name"]
            return row
    return None


_ADD_KICKBACK_PCT_RE = re.compile(r"^\s*add\s+(?:a\s+)?(\d+(?:\.\d+)?)\s*%\s*kickback\s+for\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_ADD_KICKBACK_AMT_RE = re.compile(r"^\s*add\s+(?:a\s+)?(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*kickback\s+for\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)

# Bare kickback phrasings — no fixed "add"/"set" opener, or opened by
# "set"/"remove"/"what" which already belong to OTHER intents' triggers.
# One shared parser feeds both SMART_UPDATE_INTENT's "set..." path and
# resolve_bare_reply's fallback path, so "Set Rahul's kickback to 5000.",
# "Rahul has a ₹5,000 kickback.", "Rahul's kickback is 5000.", and
# "Remove Rahul's kickback." are all recognised the same way.
_KICKBACK_REMOVE_RE = re.compile(r"remove\s+(.+?)'s\s+kickback\b", re.IGNORECASE)
_KICKBACK_POSSESSIVE_RE = re.compile(r"(.+?)'s\s+kickback\s+(?:is|to|=)\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_KICKBACK_HAS_RE = re.compile(r"^\s*(.+?)\s+has\s+a\s+(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*kickback\b", re.IGNORECASE)
# "X has a 10% kickback." — the percent-bearing sibling of _KICKBACK_HAS_RE
# above (that one's [\d,]+ capture has nothing to skip a literal "%" with,
# so "has a 10% kickback" never matched it).
_KICKBACK_HAS_PCT_RE = re.compile(r"^\s*(.+?)\s+has\s+a\s+(\d+(?:\.\d+)?)\s*%\s*kickback\b", re.IGNORECASE)
_KICKBACK_PCT_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*kickback\s+for\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_KICKBACK_QUERY_RE = re.compile(r"kickback\s+for\s+(.+?)\s*\??$", re.IGNORECASE)


def _kickback_command_from_text(text: str) -> Optional[Dict[str, str]]:
    """Returns {"kb_action", "kb_recipient", "kb_amount"?/"kb_percent"?}
    or None — used by both SMART_UPDATE_INTENT (for "Set X's kickback to
    Y") and resolve_bare_reply (for "X has a kickback"/"Remove X's
    kickback", which open with no fixed trigger word at all)."""
    text = text or ""
    if "kickback" not in text.lower():
        return None
    m = _KICKBACK_REMOVE_RE.search(text)
    if m:
        hint = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(hint):
            return {"kb_action": "remove", "kb_recipient": hint}
    m = _KICKBACK_POSSESSIVE_RE.search(text)
    if m:
        hint = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(hint):
            return {"kb_action": "set", "kb_recipient": hint, "kb_amount": m.group(2)}
    m = _KICKBACK_HAS_PCT_RE.search(text)
    if m:
        hint = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(hint):
            return {"kb_action": "set", "kb_recipient": hint, "kb_percent": m.group(2)}
    m = _KICKBACK_HAS_RE.search(text)
    if m:
        hint = _trim_leading_stopwords(m.group(1).strip())
        if _is_plausible_name(hint):
            return {"kb_action": "set", "kb_recipient": hint, "kb_amount": m.group(2)}
    m = _KICKBACK_PCT_BARE_RE.search(text)
    if m and _is_plausible_name(m.group(2)):
        return {"kb_action": "set", "kb_recipient": _trim_trailing_stopwords(m.group(2).strip()), "kb_percent": m.group(1)}
    return None


def _add_extract_fields(text: str) -> Dict[str, str]:
    crew_m = _ADD_CREW_RE.match(text or "")
    if crew_m and _is_plausible_name(crew_m.group(1)):
        return {
            "_kind": "crew",
            "name": crew_m.group(1).strip(),
            "role": crew_m.group(2).strip(),
            "project": (crew_m.group(3) or "").strip(),
        }
    # "Add a 10% kickback for Rahul." / "Add a ₹5,000 kickback for Rahul."
    # — checked before the generic reimbursement fallback since both
    # start with bare "add" and share no other structure.
    pct_m = _ADD_KICKBACK_PCT_RE.match(text or "")
    if pct_m and _is_plausible_name(pct_m.group(2)):
        return {"_kind": "kickback", "kb_action": "set", "kb_recipient": pct_m.group(2).strip(), "kb_percent": pct_m.group(1)}
    amt_m = _ADD_KICKBACK_AMT_RE.match(text or "")
    if amt_m and _is_plausible_name(amt_m.group(2)):
        return {"_kind": "kickback", "kb_action": "set", "kb_recipient": amt_m.group(2).strip(), "kb_amount": amt_m.group(1)}
    fields = _reimbursement_extract_fields(text)
    fields["_kind"] = "reimbursement"
    return fields


# ===========================================================================
# Kickbacks (Phase H / P0-C) — completes the previously-deferred NLU
# coverage for the EXISTING db.project_kickbacks model (amount,
# recipient_client_id, recipient_name, notes — see
# routers/production_desk.py's KickbackIn/KickbackUpdateIn). No new
# kickback model. Percent-based commands ("Add a 10% kickback for Rahul")
# are a deterministic ARITHMETIC convenience over the talent's own known
# budget_total — the existing model has no percent field, so a % command
# is converted to a flat amount at write time, exactly like every other
# amount this schema already stores; never guessed when no budget is on
# file (an honest error asking for a flat amount instead).
# ===========================================================================
async def _resolve_kickback_recipient(pid: str, hint: str) -> Tuple[str, Optional[str], Optional[float]]:
    """(display_name, recipient_client_id_or_None, talent_budget_total_or_None).
    Prefers an already-locked talent's real name/budget (the common case —
    a kickback paid to the talent themselves); falls back to the SAME
    CRM lookup-or-create the crew flow already uses — never a duplicate
    identity for a non-talent recipient (an agent/manager)."""
    match, project_hit, _others = await _find_talent_across_projects(hint)
    if match and project_hit and project_hit["id"] == pid:
        return match["name"], None, match.get("budget_total")
    client = await _resolve_or_create_crm_contact(hint)
    return client["name"], client["id"], None


async def _resolve_kickback_write(hint: str, action: str, kb_amount_raw: str, kb_percent_raw: str, ctx: ExecContext) -> Tuple[Optional[dict], Optional[str]]:
    """Shared by ADD_INTENT's "add ... kickback" branch and
    SMART_UPDATE_INTENT's "set"/bare-phrasing branch — one resolution
    path, two entry points (see _apply_kickback_write for the equivalent
    on the write side). Returns (plan_dict, error_message).

    Kickback commands rarely name a project explicitly ("Rahul has a
    ₹5,000 kickback") — the recipient themselves usually IS the answer:
    if they're a locked talent, their OWN project is used directly,
    never requiring the user to separately establish session context
    first. Only falls back to session context when the recipient isn't
    a locked talent anywhere (a crew/CRM-only kickback recipient)."""
    if not hint:
        return None, 'Who is this kickback for? e.g. "Add a ₹5,000 kickback for Rahul."'

    match, project_hit, _others = await _find_talent_across_projects(hint)
    if match and project_hit:
        project = project_hit
    else:
        resolution = await _resolve_project("", ctx)
        if resolution.ambiguous:
            return None, _ambiguous_project_message(resolution.ambiguous)
        if resolution.error:
            return None, "Which project is this kickback for?"
        project = resolution.project

    await _remember_project(ctx, project)
    plan: Dict[str, Any] = {"project_id": project["id"], "project_label": project["label"]}

    if action == "remove":
        existing = await _find_kickback_row(project["id"], hint)
        if not existing:
            return None, f'No kickback found for "{hint}" on {project["label"]}.'
        plan.update({"action": "remove", "name": existing.get("recipient_name") or hint,
                     "existing_id": existing["id"], "amount": existing.get("amount") or 0})
        return plan, None

    if match and project_hit and project_hit["id"] == project["id"]:
        name, client_id, budget_total = match["name"], None, match.get("budget_total")
    else:
        name, client_id, budget_total = await _resolve_kickback_recipient(project["id"], hint)

    amount: Optional[float] = None
    if kb_amount_raw:
        amount = _extract_amount(kb_amount_raw)
    elif kb_percent_raw:
        pct = float(kb_percent_raw)
        if budget_total is None:
            return None, f"I don't have a budget on file for {name} to calculate {pct}% from — please give a flat amount instead, e.g. \"Set {name}'s kickback to 5000.\""
        amount = round(budget_total * pct / 100.0, 2)
    if amount is None or amount <= 0:
        return None, "Please give a kickback amount, e.g. ₹5,000 or a percentage."

    existing_id = (await _find_kickback_row(project["id"], name) or {}).get("id", "")
    plan.update({"action": "set", "name": name, "client_id": client_id or "", "existing_id": existing_id, "amount": amount})
    return plan, None


async def _find_kickback_row(pid: str, recipient_hint: str) -> Optional[dict]:
    rows = await db.project_kickbacks.find({"project_id": pid}, {"_id": 0}).sort("created_at", -1).to_list(500)
    hint_low = (recipient_hint or "").strip().lower()
    for row in rows:
        name = (row.get("recipient_name") or "").strip().lower()
        if name and (name == hint_low or hint_low in name or name in hint_low):
            return row
    return None


async def _apply_kickback_write(
    action: str, project_id: str, name: str,
    client_id: str = "", existing_id: str = "", amount: float = 0.0,
) -> ExecResult:
    """Shared by ADD_INTENT's "add ... kickback" branch (the "add" trigger
    always routes there — see its own comment) and SMART_UPDATE_INTENT's
    "set"/bare-phrasing branch. One write path, two entry points."""
    try:
        if action == "remove":
            await pd.delete_kickback(project_id, existing_id, _AGENT_ADMIN)
            return ExecResult(ok=True, message=f"✓ Removed {name}'s kickback.")
        if existing_id:
            await pd.update_kickback(project_id, existing_id, pd.KickbackUpdateIn(amount=amount), _AGENT_ADMIN)
        else:
            await pd.add_kickback(
                project_id,
                pd.KickbackIn(amount=amount, recipient_client_id=client_id or None, recipient_name=name),
                _AGENT_ADMIN,
            )
        return ExecResult(ok=True, message=f"✓ Kickback for {name}: {_format_inr(float(amount))}.")
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't save the kickback: {e.detail}")


async def _add_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if collected.get("_kind") == "kickback":
        plan, err = await _resolve_kickback_write(
            collected.get("kb_recipient", ""), collected.get("kb_action") or "set",
            collected.get("kb_amount", ""), collected.get("kb_percent", ""), ctx,
        )
        if err:
            return ExecResult(ok=False, message=err)
        collected["_resolved_project_id"] = plan["project_id"]
        collected["_resolved_project_label"] = plan["project_label"]
        collected["_kb_name"] = plan["name"]
        collected["_kb_client_id"] = plan.get("client_id", "")
        collected["_kb_existing_id"] = plan.get("existing_id", "")
        collected["_kb_amount"] = str(plan["amount"])
        return None

    if collected.get("_kind") in ("crew_update", "crew_remove"):
        name = collected.get("name", "")
        if not name:
            return ExecResult(ok=False, message="Which crew member?")
        resolution = await _resolve_project("", ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message="Which project's crew?")
        project = resolution.project
        row = await _find_crew_row(project["id"], name)
        if not row:
            return ExecResult(ok=False, message=f'No crew member matching "{name}" found on {project["label"]}.')
        await _remember_project(ctx, project)
        collected["_resolved_project_id"] = project["id"]
        collected["_resolved_project_label"] = project["label"]
        collected["_crew_id"] = row["id"]
        collected["_crew_name"] = row.get("_client_name") or name
        collected["_crew_old_role"] = row.get("role", "")
        return None

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

        # Pre-existing gap, found while testing Phase H's crew-update flow:
        # the crew ADD path never remembered the project, unlike every
        # other write in this file — meaning "Change Amit's role..." right
        # after adding him had nothing to resolve against. Fixed here,
        # generically (not special-cased to crew_update).
        await _remember_project(ctx, resolution.project)
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
    if collected.get("_kind") == "kickback":
        name = collected.get("_kb_name", "")
        amount_txt = _format_inr(float(collected.get("_kb_amount") or 0))
        project_label = collected.get("_resolved_project_label", "")
        if collected.get("kb_action") == "remove":
            return f"Remove {name}'s kickback ({amount_txt}) on {project_label}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
        verb = "Update" if collected.get("_kb_existing_id") else "Add"
        return f"{verb} kickback for {name} on {project_label}: {amount_txt}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."

    if collected.get("_kind") == "crew_update":
        role = _match_crew_role(collected.get("role", ""))
        return f"Change {collected.get('_crew_name')}'s role from {collected.get('_crew_old_role') or '—'} to {role} on {collected.get('_resolved_project_label')}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."

    if collected.get("_kind") == "crew_remove":
        return f"Remove {collected.get('_crew_name')} ({collected.get('_crew_old_role') or 'crew'}) from {collected.get('_resolved_project_label')}'s crew?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."

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
    if collected.get("_kind") == "kickback":
        pid = collected.get("_resolved_project_id")
        if not pid:
            return ExecResult(ok=False, message="Couldn't resolve the project — please resend the command.")
        return await _apply_kickback_write(
            collected.get("kb_action") or "set", pid, collected.get("_kb_name", ""),
            client_id=collected.get("_kb_client_id", ""), existing_id=collected.get("_kb_existing_id", ""),
            amount=float(collected.get("_kb_amount") or 0),
        )

    if collected.get("_kind") == "crew_update":
        pid = collected.get("_resolved_project_id")
        role = _match_crew_role(collected.get("role", ""))
        try:
            await pd.update_crew(pid, collected.get("_crew_id"), pd.CrewUpdateIn(role=role), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update crew member: {e.detail}")
        return ExecResult(ok=True, message=f"✓ {collected.get('_crew_name')} is now {role}.")

    if collected.get("_kind") == "crew_remove":
        pid = collected.get("_resolved_project_id")
        try:
            await pd.delete_crew(pid, collected.get("_crew_id"), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't remove crew member: {e.detail}")
        return ExecResult(ok=True, message=f"✓ Removed {collected.get('_crew_name')} from the crew.")

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
        FieldSpec(key="kb_action", label="Kickback action", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="kb_recipient", label="Kickback recipient", question="Who is this kickback for?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="kb_amount", label="Kickback amount", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="kb_percent", label="Kickback percent", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
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
# "Add a task for Rahul to send the call sheet tomorrow." / "Create a task
# for Shivi to confirm costume trial by 6 PM." — the project/talent-FIRST
# word order the Phase H spec's own examples use, distinct from
# _ADD_TASK_TRIGGER_RE's project/talent-LAST shape ("...to Y for X").
_ADD_TASK_FOR_TRIGGER_RE = re.compile(r"^(?:add|create)\s+a\s+task\s+for\s+(.+?)\s+to\s+(.+)$", re.IGNORECASE)


def _add_task_extract_fields(text: str) -> Dict[str, str]:
    # "Remind me to follow up with Google AI on 30 August." — ADD_TASK's
    # own "remind me" trigger unconditionally wins over
    # SMART_UPDATE_INTENT's parsing for ANY "remind me..." opener (a
    # trigger match always beats resolve_bare_reply — see agents/models.py's
    # own docstring on that hook), so a genuine payment-follow-up
    # statement would otherwise get silently misfiled as a generic task
    # instead of setting project.pd_next_follow_up_at. Checked first,
    # BEFORE the normal title/due-date extraction below, and redirected
    # into its own small kind — reusing the EXACT SAME field/resolution
    # the "follow up with X on Y" phrase already uses elsewhere in this
    # file (_FOLLOW_UP_WITH_RE), never a second implementation.
    fu_m = _FOLLOW_UP_WITH_RE.search(text or "")
    if fu_m:
        project_hint = _trim_trailing_stopwords(fu_m.group(1).strip())
        if _is_plausible_name(project_hint):
            return {
                "_kind": "followup_redirect",
                "title": "(payment follow-up)",  # placeholder — never actually used
                "project_hint": project_hint,
                "_followup_date_raw": fu_m.group(2).strip(),
            }

    # Project/talent-FIRST word order — "Add a task for Rahul to send the
    # call sheet tomorrow." Checked before the project-LAST shape below
    # since both share the "add/create a task" opener.
    for_first_m = _ADD_TASK_FOR_TRIGGER_RE.match(text or "")
    if for_first_m:
        hint_candidate = _trim_trailing_stopwords(for_first_m.group(1).strip())
        body = for_first_m.group(2)
        remaining, due_at = _strip_date_phrase(body)
        return {
            "title": remaining.strip().rstrip("."),
            "due_at": due_at or "",
            "project_hint": hint_candidate if _is_plausible_name(hint_candidate) else "",
        }

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
        candidate = tail_m.group(1).strip().rstrip(".")
        project_hint = candidate if _is_plausible_name(candidate) else ""

    return {
        "title": remaining.strip().rstrip("."),
        "due_at": due_at or "",
        "project_hint": project_hint,
    }


async def _add_task_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    if collected.get("_kind") == "followup_redirect":
        resolution = await _resolve_project(collected.get("project_hint", ""), ctx)
        if resolution.ambiguous:
            return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
        if resolution.error:
            return ExecResult(ok=False, message=resolution.error)
        dt = _parse_absolute_datetime(collected.get("_followup_date_raw", ""))
        if not dt:
            return ExecResult(ok=False, message=f"Couldn't understand the follow-up date \"{collected.get('_followup_date_raw')}\".")
        await _remember_project(ctx, resolution.project)
        collected["_resolved_project_id"] = resolution.project["id"]
        collected["_resolved_project_label"] = resolution.project["label"]
        collected["_followup_date"] = dt
        return None

    title = collected.get("title", "").strip()
    if not title:
        return ExecResult(ok=False, message='What should the task be? e.g. "Add a task to get the call sheet tomorrow."')

    project = None
    talent_id = ""
    project_hint = collected.get("project_hint", "")
    if project_hint:
        # Try the hint as a locked TALENT first — "Add a task for Shivi
        # to confirm costume trial by 6 PM." names a talent, not a
        # project; her own project is used directly, matching the same
        # talent-first resolution pattern kickbacks/reimbursements
        # already use.
        match, project_hit, _others = await _find_talent_across_projects(project_hint)
        if match and project_hit:
            project = project_hit
            talent_id = match["talent_id"]
        else:
            resolution = await _resolve_project(project_hint, ctx)
            if resolution.ambiguous:
                return ExecResult(ok=False, message=_ambiguous_project_message(resolution.ambiguous))
            if resolution.project:
                project = resolution.project
            # A resolution error here is NOT fatal — "follow up" itself may
            # just be a task with no resolvable project mention; fall
            # through to the session fallback below rather than failing
            # the whole command.
    if not project:
        session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
        last_id = (session or {}).get("last_project_id")
        if last_id:
            project = {"id": last_id, "label": (session or {}).get("last_project_label") or ""}

    collected["_resolved_project_id"] = project["id"] if project else ""
    collected["_resolved_project_label"] = project["label"] if project else ""
    collected["_resolved_talent_id"] = talent_id
    return None


async def _add_task_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get("_kind") == "followup_redirect":
        return f"Set next follow-up for {collected.get('_resolved_project_label')} to {_format_due(collected.get('_followup_date'))}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
    title = collected.get("title")
    due_at = collected.get("due_at")
    project_label = collected.get("_resolved_project_label") or ""
    due_txt = f" (due {_format_due(due_at)})" if due_at else ""
    proj_txt = f" for {project_label}" if project_label else ""
    return f'Add task "{title}"{proj_txt}{due_txt}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'


async def _add_task_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    from routers import workflow as workflow_router

    if collected.get("_kind") == "followup_redirect":
        pid = collected.get("_resolved_project_id")
        try:
            await pd.update_production_desk_project(pid, pd.ProductionDeskProjectPatch(next_follow_up_at=collected.get("_followup_date")), _AGENT_ADMIN)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f"✓ {collected.get('_resolved_project_label')}: Next follow-up → {_format_due(collected.get('_followup_date'))}")

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
        talent_id=collected.get("_resolved_talent_id") or None,
        due_at=due_at,
        priority="normal",
    )
    try:
        created = await workflow_router.create_task(payload, synthetic_user)
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't add task: {e.detail}")
    await _remember_task(ctx, created.get("id", ""), title)
    due_txt = f" (due {_format_due(due_at)})" if due_at else ""
    return ExecResult(ok=True, message=f"✓ Task added: {title}{due_txt}")


ADD_TASK_INTENT = IntentDefinition(
    intent_id="management.add_task",
    triggers=["add a task", "create a task", "remind me"],
    fields=[
        FieldSpec(key="title", label="Task", question="What should the task be?", validate=lambda v: ValidationResult(ok=True, value=v) if v else ValidationResult(ok=False, error="Please describe the task.")),
        FieldSpec(key="due_at", label="Due", question="When is it due?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="project_hint", label="Project", question="Which project, if any?", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="_kind", label="Kind", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
        FieldSpec(key="_followup_date_raw", label="Follow-up date", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False),
    ],
    extract_fields=_add_task_extract_fields,
    try_auto_execute=_add_task_try_auto_execute,
    build_confirmation=_add_task_build_confirmation,
    executor=_add_task_executor,
)


# ===========================================================================
# CONVERSATIONAL TASK MANAGEMENT (Phase H / P0-H) — update/complete/reopen
# an EXISTING workflow_tasks row (creation stays ADD_TASK_INTENT's job).
# Writes through routers.workflow.update_task DIRECTLY — the SAME
# db.workflow_tasks collection/API the admin Workflow page and Production
# Desk's own task queries already use. No new task model.
#
# "It"/"that task" resolution reuses session_context (see _remember_task)
# — the SAME store _remember_project already writes to, not a second
# context mechanism. A non-pronoun hint ("the call sheet task") is
# resolved by a case-insensitive title match, narrowed to the session's
# last project when one is set.
# ===========================================================================
_TASK_PRONOUNS = {"it", "that", "that task", "the task", "this task"}


def _clean_task_hint(raw: str) -> str:
    raw = (raw or "").strip()
    low = raw.lower()
    if low in _TASK_PRONOUNS:
        return low
    raw = re.sub(r"^(?:the|that|this)\s+", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"\s+task$", "", raw, flags=re.IGNORECASE).strip()
    return raw


async def _resolve_task_reference(hint: str, ctx: ExecContext) -> Tuple[Optional[dict], Optional[str]]:
    hint = _clean_task_hint(hint)
    session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
    if not hint or hint in _TASK_PRONOUNS:
        tid = (session or {}).get("last_task_id")
        if not tid:
            return None, 'Which task do you mean? Try naming it, e.g. "the call sheet task".'
        task = await db.workflow_tasks.find_one({"id": tid}, {"_id": 0})
        if not task:
            return None, "That task no longer exists."
        return task, None

    candidates = await db.workflow_tasks.find(
        {"title": {"$regex": re.escape(hint), "$options": "i"}}, {"_id": 0},
    ).sort("created_at", -1).to_list(50)
    if not candidates:
        return None, None  # genuinely no match — caller decides how to treat this (see callers)
    pid = (session or {}).get("last_project_id")
    if pid:
        scoped = [t for t in candidates if t.get("project_id") == pid]
        if scoped:
            candidates = scoped
    return candidates[0], None  # most recent match — a deterministic tie-break, never a silent guess across unrelated projects since session already narrows scope


_TASK_REOPEN_RE = re.compile(r"^reopen\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_TASK_COMPLETE_RE = re.compile(r"^(?:mark\s+)?(.+?)\s+(?:is\s+)?(?:as\s+)?(?:complete|completed|done)\s*[\.\?!]*$", re.IGNORECASE)
_TASK_MAKE_PRIORITY_RE = re.compile(r"^make\s+(.+?)\s+(high|low|normal|urgent)\s*priority\s*[\.\?!]*$", re.IGNORECASE)
_TASK_MOVE_DUE_RE = re.compile(r"^move\s+(.+?)\s+to\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)
_TASK_RETITLE_RE = re.compile(r"^change\s+(.+?)\s+to\s+(.+?)\s*[\.\?!]*$", re.IGNORECASE)


def _classify_task_command(text: str) -> Optional[dict]:
    text = (text or "").strip()
    m = _TASK_REOPEN_RE.match(text)
    if m:
        return {"action": "reopen", "hint": m.group(1)}
    m = _TASK_MAKE_PRIORITY_RE.match(text)
    if m:
        priority = m.group(2).lower()
        return {"action": "update", "hint": m.group(1), "priority": "high" if priority == "urgent" else priority}
    m = _TASK_MOVE_DUE_RE.match(text)
    if m:
        due_at = _parse_absolute_datetime(m.group(2))
        if due_at:
            return {"action": "update", "hint": m.group(1), "due_at": due_at}
    m = _TASK_RETITLE_RE.match(text)
    if m:
        return {"action": "update", "hint": m.group(1), "new_title": m.group(2).strip()}
    m = _TASK_COMPLETE_RE.match(text)
    if m:
        return {"action": "complete", "hint": m.group(1)}
    return None


async def _task_manage_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    cmd = _classify_task_command(collected.get("raw_text", ""))
    if not cmd:
        return ExecResult(ok=False, message='Which task, and what should change? e.g. "Make that task high priority." or "Mark that task complete."')
    task, err = await _resolve_task_reference(cmd["hint"], ctx)
    if err:
        return ExecResult(ok=False, message=err)
    if not task:
        return ExecResult(ok=False, message=f"No task matching \"{_clean_task_hint(cmd['hint'])}\" found.")
    collected["_task_id"] = task["id"]
    collected["_task_title"] = task.get("title") or ""
    collected["_action"] = cmd["action"]
    if cmd["action"] == "update":
        collected["_new_priority"] = cmd.get("priority", "")
        collected["_new_due_at"] = cmd.get("due_at", "")
        collected["_new_title"] = cmd.get("new_title", "")
    return None


async def _task_manage_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    title = collected.get("_task_title", "")
    action = collected.get("_action")
    if action == "complete":
        return f'Mark task "{title}" complete?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'
    if action == "reopen":
        return f'Reopen task "{title}"?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'
    parts = []
    if collected.get("_new_priority"):
        parts.append(f"priority → {collected['_new_priority']}")
    if collected.get("_new_due_at"):
        parts.append(f"due → {_format_due(collected['_new_due_at'])}")
    if collected.get("_new_title"):
        parts.append(f"title → {collected['_new_title']}")
    change_txt = ", ".join(parts) or "no recognisable change"
    return f'Update task "{title}": {change_txt}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel.'


async def _task_manage_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    from routers import workflow as workflow_router

    tid = collected.get("_task_id")
    title = collected.get("_task_title", "")
    action = collected.get("_action")
    synthetic_user = {"id": _AGENT_ADMIN["id"], "role": "admin"}
    await _remember_task(ctx, tid, title)

    if action == "complete":
        try:
            await workflow_router.update_task(tid, workflow_router.TaskUpdateIn(status="completed"), synthetic_user)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f'✓ Task "{title}" marked complete.')

    if action == "reopen":
        try:
            await workflow_router.update_task(tid, workflow_router.TaskUpdateIn(status="pending"), synthetic_user)
        except HTTPException as e:
            return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
        return ExecResult(ok=True, message=f'✓ Task "{title}" reopened.')

    patch_kwargs = {}
    if collected.get("_new_priority"):
        patch_kwargs["priority"] = collected["_new_priority"]
    if collected.get("_new_due_at"):
        patch_kwargs["due_at"] = collected["_new_due_at"]
    if collected.get("_new_title"):
        patch_kwargs["title"] = collected["_new_title"]
    if not patch_kwargs:
        return ExecResult(ok=False, message="Nothing recognisable to change.")
    try:
        await workflow_router.update_task(tid, workflow_router.TaskUpdateIn(**patch_kwargs), synthetic_user)
    except HTTPException as e:
        return ExecResult(ok=False, message=f"Couldn't update: {e.detail}")
    display_title = collected.get("_new_title") or title
    _labels = {"priority": "priority", "due_at": "due"}
    _fmt = lambda k, v: _format_due(v) if k == "due_at" else v
    changes = ", ".join(f"{_labels.get(k, k)} → {_fmt(k, v)}" for k, v in patch_kwargs.items() if k != "title")
    changes_txt = f" ({changes})" if changes else ""
    return ExecResult(ok=True, message=f'✓ Task "{display_title}" updated{changes_txt}.')


TASK_MANAGE_INTENT = IntentDefinition(
    intent_id="management.task_manage",
    triggers=[
        "make that task", "make it", "move it", "move that task", "move the task",
        "update the task", "update that task", "change the task", "change that task",
        "mark that task", "mark it complete", "mark it done", "complete that task", "complete it",
        "reopen that task", "reopen it", "reopen the task", "reopen",
    ],
    fields=[FieldSpec(key="raw_text", label="Command", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False)],
    extract_fields=lambda text: {"raw_text": text},
    try_auto_execute=_task_manage_try_auto_execute,
    build_confirmation=_task_manage_build_confirmation,
    executor=_task_manage_executor,
)


# ===========================================================================
# SMART MULTI-ACTION UPDATE — natural, non-rigid coverage of every writable
# Production Desk field, one OR MANY in a single free-text message
# ("Google AI shoot is 26 and 27 August, call time 7:30 AM, reporting 7 AM,
# location Mumbai. Shivi's costume trial is 25 August at 3 PM in Andheri.
# Add a task to get the call sheet tomorrow and another to follow up
# payment on Monday."). Reached via MANAGEMENT_AGENT.resolve_bare_reply —
# the platform's EXISTING "no trigger matched this turn, give the agent
# one more chance against session context" hook (agents/models.py) — not
# a new dispatch mechanism. Still plain deterministic regex/keyword
# matching throughout; no AI/LLM anywhere in this file.
#
# ROOT-CAUSE FIX this section exists to protect (see _is_plausible_name
# above): every subject (project/talent) candidate this parser extracts
# is run through that same stopword gate before being treated as an
# entity — "Set the shoot date for Google AI..." must never resolve "the"
# as a talent, because "the" fails the gate structurally, not because of
# a denylist entry naming that one word.
# ===========================================================================

# --- Date-range protection + clause splitting --------------------------------
_DATE_AND_RE = re.compile(
    r"\d{1,2}(?:st|nd|rd|th)?(\s*(?:-|to|and)\s*)\d{1,2}(?:st|nd|rd|th)?(\s+(?:of\s+)?[A-Za-z]+)?",
    re.IGNORECASE,
)


def _protect_date_ranges(text: str) -> str:
    """"26 and 27 August" must survive clause-splitting as ONE date value,
    not be mistaken for two separate clauses joined by "and"."""
    return _DATE_AND_RE.sub(lambda m: m.group(0).replace(" and ", " \x00AND\x00 ").replace(" to ", " \x00TO\x00 "), text or "")


# "Shivi and Rahul's costume trials..." — the clause splitter's own
# capital-letter heuristic (a talent/project name starting a genuinely
# NEW clause usually IS capitalized) would otherwise mistake "and Rahul's"
# for a fresh sentence, same failure mode as _protect_date_ranges guards
# against for dates — a two-name LIST sharing one trailing possessive is
# the other structural shape "and" needs protecting inside.
_NAME_AND_POSSESSIVE_RE = re.compile(r"\b([A-Za-z][\w'.]*)\s+and\s+([A-Za-z][\w'.]*)'s\b")


def _protect_name_lists(text: str) -> str:
    def _sub(m: "re.Match") -> str:
        # Guard against a false positive like "...3 PM and Rahul's..." —
        # "PM" superficially matches the bare capitalized-word shape too;
        # only protect when BOTH sides are plausible names, the same
        # gate used everywhere else an entity candidate is accepted.
        if _is_plausible_name(m.group(1)) and _is_plausible_name(m.group(2)):
            return f"{m.group(1)} \x00AND\x00 {m.group(2)}'s"
        return m.group(0)
    return _NAME_AND_POSSESSIVE_RE.sub(_sub, text or "")


def _unprotect(text: str) -> str:
    return text.replace("\x00AND\x00", "and").replace("\x00TO\x00", "to")


_CLAUSE_SPLIT_RE = re.compile(
    r"\.\s+|;\s*|,\s+and\s+|,\s*(?=[a-z])|\s+and\s+(?=[A-Z])"
    # Phase H fix: a comma followed by a CAPITALIZED word is only safe to
    # split on when that word is immediately its own clause subject —
    # "...call time 7:30 AM, Shivi's fitting is complete" must split
    # before "Shivi", but "location is Mumbai, India" must not split
    # before "India". The generic, structural signal: the capitalized
    # word is directly followed by "'s"/" is "/" has " (a real subject
    # starting a new statement), not just any capitalized word (which
    # could be a value continuation).
    r"|,\s*(?=[A-Z][\w'.]*(?:'s\b|\s+is\b|\s+has\b))"
)


def _split_into_clauses(text: str) -> List[str]:
    protected = _protect_name_lists(_protect_date_ranges(text or ""))
    parts = _CLAUSE_SPLIT_RE.split(protected)
    return [_unprotect(p).strip().rstrip(".") for p in parts if _unprotect(p).strip()]


# --- Date/time parsing for the few fields that need a REAL datetime
# (costume_trial_at, expected_payment_date, next_follow_up_at) — every
# other date-ish field (project.shoot_dates, pd_call_time,
# pd_reporting_time) is stored as free text already and is captured
# verbatim, per "preserve existing Production Desk semantics instead of
# inventing a new date format". -----------------------------------------
_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DATE_DM_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_DATE_MD_RE = re.compile(r"\b(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
# Stricter than _TIME_RE — requires an explicit am/pm or a colon, so a
# bare number ("2 weeks", "2 days") is never mistaken for a clock time.
_EXPLICIT_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)


def _parse_time_of_day(text: str) -> Optional[Tuple[int, int]]:
    for m in _TIME_RE.finditer(text or ""):
        hour = int(m.group(1))
        if hour > 24:
            continue
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if hour == 0 and not ampm and minute == 0:
            continue  # bare "0" isn't a time mention
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23:
            continue
        if not ampm and hour == 24:
            continue
        return hour % 24, minute
    return None


def _parse_absolute_datetime(text: str) -> Optional[str]:
    """"25 August at 3 PM" / "tomorrow" / "on Monday" / "30 August" ->
    ISO datetime string. Returns None (never guesses) when nothing
    recognisable is present."""
    text = text or ""
    word_m = _DATE_WORD_RE.search(text)
    if word_m:
        base = _parse_due_date(word_m.group(1))
        if base:
            t = _parse_time_of_day(text[word_m.end():])
            if t:
                dt = datetime.fromisoformat(base).replace(hour=t[0], minute=t[1])
                return dt.isoformat()
            return base

    now = datetime.now(timezone.utc)
    m = _DATE_DM_RE.search(text)
    day_group, month_group = (1, 2) if m else (None, None)
    if not m:
        m = _DATE_MD_RE.search(text)
        day_group, month_group = (2, 1) if m else (None, None)
    if not m:
        # No recognisable date word/number-month pattern anywhere — but a
        # BARE time alone ("3 PM", "trial is 3 PM") is a common, natural
        # way to state "today at that time" (the spec's own literal
        # example — "Shivi's trial is 3 PM and Rahul's is 5 PM" — never
        # mentions a date at all). Generic fallback, not a costume-trial
        # special case: any date-value field goes through this same
        # function. Gated on an EXPLICIT time marker (am/pm or a colon)
        # so a bare number in an unrelated phrase ("in 2 weeks", "2
        # days") is never misread as a clock time.
        if _EXPLICIT_TIME_RE.search(text):
            t = _parse_time_of_day(text)
            if t:
                return now.replace(hour=t[0], minute=t[1], second=0, microsecond=0).isoformat()
        return None
    try:
        day = int(m.group(day_group))
        month = _MONTH_NAMES[m.group(month_group).lower()]
        candidate = datetime(now.year, month, day, 12, 0, 0, tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None
    if candidate.date() < now.date():
        candidate = candidate.replace(year=now.year + 1)
    t = _parse_time_of_day(text[m.end():]) or _parse_time_of_day(text[:m.start()])
    if t:
        candidate = candidate.replace(hour=t[0], minute=t[1])
    return candidate.isoformat()


# --- Field keyword tables. Ordered most-specific-first WITHIN each list
# (checked as a whole regex alternation is NOT used — see
# _find_field_matches, which tries every phrase of every field and
# resolves overlaps by position+length, so relative ORDER across
# different fields' lists doesn't matter, only phrase specificity). -----
# Each phrase is (text, forced_value). forced_value is None for a phrase
# that INTRODUCES a value still to come afterward ("call time" -> value
# follows); it is a literal string for a phrase that IS the value itself
# ("fitting complete" doesn't need anything typed after it — the phrase
# already says what fitting_status should become). Matched by
# _find_field_matches/_slice_field_values below; not a special case, a
# property of the phrase table any field can use.
_PROJECT_FIELD_PHRASES: List[Tuple[str, List[Tuple[str, Optional[str]]]]] = [
    ("shoot_dates", [("shooting dates are", None), ("shoot dates are", None), ("shooting dates", None), ("shoot dates", None), ("shooting date", None), ("shoot date", None), ("shoot is", None)]),
    ("reporting_time", [("reporting time", None), ("reporting", None)]),
    ("call_time", [("call time", None)]),
    ("shoot_location", [("location is", None), ("location", None)]),
    ("production_contact_client_id", [("production contact is", None), ("production contact", None)]),
    ("shoot_status", [("shoot scheduled", "scheduled"), ("confirmed for the shoot", "scheduled"), ("shoot status is", None), ("shoot status", None)]),
    ("shoot_notes", [("add note that", None), ("add note", None), ("notes are", None), ("note is", None), ("notes:", None), ("note:", None)]),
    ("payment_terms", [("payment terms are", None), ("payment terms", None)]),
    ("expected_payment_date", [("expected payment date is", None), ("expected payment date", None), ("payment is expected on", None), ("payment expected on", None), ("expected on", None)]),
    # "follow up WITH X on Y" is handled by _FOLLOW_UP_WITH_RE (a
    # dedicated structural pattern, checked before generic field
    # matching) — "with" isn't listed as PART of this field's own
    # keyword phrases, because it's what introduces the SUBJECT, not
    # the value; if it were folded into the keyword span, subject
    # extraction would have nothing left to find.
    ("next_follow_up_at", [("next follow up on", None), ("next follow-up on", None), ("follow up on", None), ("follow-up on", None), ("next follow up date", None), ("needs follow up", None), ("needs follow-up", None)]),
    ("payment_followup_notes", [("follow up note that", None), ("follow-up note that", None), ("follow up note", None), ("follow-up note", None)]),
    ("payment_followup_status", [("payment follow up done", "done"), ("payment follow-up done", "done"), ("follow up done", "done"), ("follow-up done", "done")]),
    ("production_budget_per_day", [("production budget per day is", None), ("production budget per day", None), ("production budget/day", None)]),
    ("production_budget_total", [("production budget is", None), ("production budget", None)]),
    ("shooting_days", [("shooting days", None), ("shoot days", None)]),
    ("confirmation_mail_received", [("confirmation mail received", "true"), ("confirmation mail", "true")]),
    ("invoice_raised", [("invoice raised", "true"), ("invoice has been raised", "true")]),
    ("invoice_sent", [("invoice sent", "true"), ("invoice has been sent", "true")]),
    ("payment_in_received", [("client payment received", "true"), ("client payment", "true"), ("payment received from", "true")]),
    ("gst_component_received", [("gst component received", "true"), ("gst payment received", "true"), ("gst received", "true"), ("gst component", "true")]),
    # Project lifecycle (Phase H / P0-G) — production_status is a purely
    # informational pd_* enum (routers/production_desk.py's
    # PRODUCTION_STATUS_OPTIONS); nothing in the backend gates a
    # transition, so there is no business-rule check to preserve here
    # beyond the existing enum-membership validation
    # update_production_desk_project already does.
    # "shoot scheduled" (bare) stays exclusively shoot_status's own
    # phrase, unchanged — only the longer, more specific "as shoot
    # scheduled" belongs to production_status, so the two never compete
    # for the same span (the longer phrase always wins an overlap).
    ("production_status", [
        ("is confirmed", "confirmed"), ("as confirmed", "confirmed"),
        ("as shoot scheduled", "shoot_scheduled"),
        ("shoot is complete", "shoot_complete"), ("shoot complete", "shoot_complete"),
        ("to finance closed", "finance_closed"), ("finance closed", "finance_closed"),
        ("production status is", None), ("production status", None),
    ]),
]

_TALENT_FIELD_PHRASES: List[Tuple[str, List[Tuple[str, Optional[str]]]]] = [
    ("costume_trial_at", [("costume trial is", None), ("costume trial", None), ("costume fitting is", None), ("costume fitting", None), ("trial is", None), ("trial", None)]),
    ("fitting_status", [("fitting is complete", "completed"), ("fitting complete", "completed"), ("fitting is done", "completed"), ("fitting done", "completed"), ("fitting", None)]),
    ("look_test_status", [("look test is done", "completed"), ("look test done", "completed"), ("look test is complete", "completed"), ("look test complete", "completed"), ("look test", None)]),
    ("grooming_requirements", [("grooming instructions are", None), ("grooming instructions", None), ("grooming requirements are", None), ("grooming requirements", None), ("grooming is", None), ("grooming", None)]),
    ("special_instructions", [("special instructions are", None), ("special instructions", None)]),
    ("shoot_status", [("confirmed for the shoot", "scheduled"), ("shoot status is", None), ("shoot status", None)]),
    ("budget_per_day", [("per day", None), ("budget per day is", None), ("budget per day", None), ("budget/day", None)]),
    ("shooting_days", [("shoot days", None), ("shooting days", None)]),
    ("budget_total", [("budget is", None), ("budget", None)]),
    ("commission_percent", [("commission is", None), ("commission", None)]),
]

_STATUS_ENUM_MAP = {
    "shoot_status": {
        "today": "today", "confirmed": "scheduled", "scheduled": "scheduled",
        "complete": "completed", "completed": "completed", "done": "completed",
        "cancelled": "cancelled", "canceled": "cancelled", "not scheduled": "not_scheduled",
    },
    "fitting_status": {
        "complete": "completed", "completed": "completed", "done": "completed",
        "scheduled": "scheduled", "not scheduled": "not_scheduled",
    },
    "look_test_status": {
        "complete": "completed", "completed": "completed", "done": "completed",
        "scheduled": "scheduled", "not scheduled": "not_scheduled",
    },
    "payment_followup_status": {
        "done": "done", "complete": "done", "completed": "done",
        "due": "due", "in progress": "in_progress", "not due": "not_due",
    },
}


def _find_field_matches(clause: str, field_defs: List[Tuple[str, List[Tuple[str, Optional[str]]]]]) -> List[Tuple[int, int, str, Optional[str]]]:
    """Every (phrase, field) pair is tried independently; overlapping
    matches are resolved by picking the LEFTMOST, and among ties at the
    same start position the LONGEST phrase — avoids needing one fragile
    hand-ordered mega-regex alternation. Returns (start, end, field_key,
    forced_value)."""
    candidates = []
    for field_key, phrases in field_defs:
        for phrase, forced_value in phrases:
            # Trailing "s?" tolerates simple plural/verb-agreement drift
            # ("costume trial" also matching "costume trials", generic
            # per the "singular/plural variations" requirement — not a
            # per-word synonym dictionary, just one optional trailing
            # letter on whatever phrase already matched.
            for m in re.finditer(r"\b" + phrase + r"s?\b", clause, re.IGNORECASE):
                candidates.append((m.start(), m.end(), field_key, forced_value))
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    resolved: List[Tuple[int, int, str, Optional[str]]] = []
    last_end = -1
    for start, end, key, forced_value in candidates:
        if start >= last_end:
            resolved.append((start, end, key, forced_value))
            last_end = end
    return resolved


# Word-boundaried on BOTH sides of each word-alternative — without \b,
# "to" as a bare substring alternative matches the first two letters of
# "tomorrow", turning it into "morrow" (found live during testing).
_LEADING_CONNECTOR_RE = re.compile(r"^(?:\b(?:is|are|to|as|that)\b|[=:,])\s*", re.IGNORECASE)
_TRAILING_CONJUNCTION_RE = re.compile(r"[\s,]*(?:,|&|\band\b)\s*$", re.IGNORECASE)


def _clean_extracted_text(text: str) -> str:
    """Generic post-extraction cleanup applied to EVERY captured span
    (task title, field value, subject-name candidate) — not a fix for
    one sentence. Collapses repeated whitespace and strips a dangling
    trailing conjunction/comma a clause or task-opener boundary can
    leave behind (e.g. "...tomorrow AND" when the next item's own
    opener match starts at "another", not at the "and" joining the
    two)."""
    text = re.sub(r"\s{2,}", " ", (text or "")).strip()
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_CONJUNCTION_RE.sub("", text).strip()
    return text.strip(" .,")


def _trim_trailing_stopwords(candidate: str) -> str:
    """"Google AI to" -> "Google AI" — a multi-word "for X" capture can
    run on into the next clause's connector word ("to"/"is"/"on"/...);
    trim trailing stopword tokens rather than constraining the capture
    regex itself, so a real (if unusual) trailing word is never silently
    unreachable."""
    words = (candidate or "").split()
    while words and re.sub(r"[^a-z0-9']", "", words[-1].lower()) in _ENTITY_STOPWORDS:
        words.pop()
    return " ".join(words)


def _trim_leading_stopwords(candidate: str) -> str:
    """"Set Rahul" -> "Rahul" — the mirror-image of
    _trim_trailing_stopwords, for a capture that ran BACKWARD into a
    preceding trigger/verb word (e.g. a possessive regex with `re.search`
    matching from the earliest possible position, "Set Rahul's kickback"
    -> group(1)="Set Rahul")."""
    words = (candidate or "").split()
    while words and re.sub(r"[^a-z0-9']", "", words[0].lower()) in _ENTITY_STOPWORDS:
        words.pop(0)
    return " ".join(words)


def _slice_field_values(clause: str, matches: List[Tuple[int, int, str, Optional[str]]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for i, (start, end, key, forced_value) in enumerate(matches):
        if forced_value is not None:
            # The matched phrase itself already IS the value ("fitting
            # complete", "invoice sent") — nothing to slice from trailing
            # text, and there may be nothing meaningful there anyway.
            result.setdefault(key, forced_value)
            continue
        value_end = matches[i + 1][0] if i + 1 < len(matches) else len(clause)
        raw = clause[end:value_end].strip()
        raw = _LEADING_CONNECTOR_RE.sub("", raw).strip()
        raw = _clean_extracted_text(raw)
        if raw:
            result.setdefault(key, raw)
    return result


def _normalize_status_value(field_key: str, raw: str) -> str:
    mapping = _STATUS_ENUM_MAP.get(field_key, {})
    low = raw.strip().lower()
    return mapping.get(low, low.replace(" ", "_"))


def _extract_subject_from_clause(clause: str, first_field_pos: Optional[int]) -> Tuple[str, str]:
    """Returns (subject_candidate, clause_with_subject_removed). Tries, in
    order: an explicit "for X" mention anywhere in the clause; then the
    leading text before the first recognised field keyword (stripping a
    leading imperative verb and a trailing possessive 's). Every
    candidate is gated by _is_plausible_name before being returned —
    this is the exact mechanism that keeps "Set the shoot date..." from
    ever extracting "the". Shared by both project- and talent-scoped
    clauses — the extraction shape is identical either way."""
    for_m = re.search(r"\bfor\s+([A-Za-z][\w'.]*(?:\s+[A-Za-z][\w'.]*){0,4})", clause, re.IGNORECASE)
    if for_m:
        candidate = _trim_trailing_stopwords(for_m.group(1).strip().rstrip(",. "))
        if _is_plausible_name(candidate):
            remaining = clause[:for_m.start()] + " " + clause[for_m.end():]
            return candidate, remaining.strip()

    if first_field_pos and first_field_pos > 0:
        candidate = clause[:first_field_pos].strip()
        candidate = re.sub(r"^(?:mark|set|make|move|add note that|add note)\s+", "", candidate, flags=re.IGNORECASE).strip()
        if candidate.endswith("'s"):
            candidate = candidate[:-2].strip()
        elif candidate.endswith("s'"):
            candidate = candidate[:-1].strip()
        candidate = _trim_trailing_stopwords(candidate)
        if _is_plausible_name(candidate):
            return candidate, clause[first_field_pos:]

    return "", clause


def _strip_possessive(name: str) -> str:
    if name.endswith("'s"):
        return name[:-2].strip()
    if name.endswith("s'"):
        return name[:-1].strip()
    return name


def _split_multi_names(candidate: str) -> List[str]:
    """"Shivi and Rahul" -> ["Shivi", "Rahul"] — only splits on a bare
    " and " (never inside a date range, which never reaches this
    function — it only ever receives an already-isolated name span).
    Each part also has a trailing possessive stripped, since a caller's
    capture regex can end up including it ("Rahul's" whole, when the
    optional "'s" group it expected to consume it separately already had
    nothing left to match)."""
    parts = [_strip_possessive(p.strip()) for p in re.split(r"\s+and\s+", candidate) if p.strip()]
    candidate = _strip_possessive(candidate.strip())
    return [p for p in parts if _is_plausible_name(p)] or ([candidate] if _is_plausible_name(candidate) else [])


_TRAILING_LOCATION_RE = re.compile(r"\bin\s+([A-Za-z][\w\s]*)$", re.IGNORECASE)


def _split_trial_value(raw: str) -> Tuple[str, str]:
    """"25 August at 3 PM in Andheri" -> ("25 August at 3 PM", "Andheri").
    Location is introduced by a trailing "in X" (time uses "at", so the
    two don't collide)."""
    loc_m = _TRAILING_LOCATION_RE.search(raw)
    if loc_m:
        return raw[:loc_m.start()].strip(), loc_m.group(1).strip()
    return raw, ""


_NUMERIC_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _extract_numeric(raw: str) -> Optional[float]:
    m = _NUMERIC_RE.search(raw or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# --- Talent-scope disambiguation. Most talent-field keywords are UNIQUE
# to talents (a project has no "costume trial"/"fitting"/"look test"/
# "grooming"/"special instructions" concept at all) — a clause containing
# any of these is unambiguously talent-scoped regardless of HOW the
# subject is phrased (possessive, "mark X", or a bare leading name).
# Only a handful of field KEYS exist at both levels (shoot_status,
# shooting_days, and the rate-per-day shape below) — those default to
# talent scope only when the clause also carries a possessive "'s" or a
# "mark" opener; otherwise project (the more common bare phrasing).
_TALENT_UNIQUE_FIELDS = {
    "costume_trial_at", "fitting_status", "look_test_status",
    "grooming_requirements", "special_instructions",
}
_TALENT_SIGNAL_RE = re.compile(r"'s\b|^\s*mark\b", re.IGNORECASE)

# Structural "<Name> is <number> per day (for <N> days)?" shape — a value-
# THEN-unit sentence order the generic keyword->value slicer (built for
# keyword-THEN-value phrasing) can't parse directly. Generalises the
# WHOLE pattern shape, not any one example's numbers.
_TALENT_RATE_RE = re.compile(
    r"^([A-Za-z][\w'.]*)\s+is\s+([\d,]+(?:\.\d+)?)\s*(?:per\s*day|/\s*day|a\s*day)\b(?:\s+for\s+(\d+)\s*days?)?",
    re.IGNORECASE,
)
_PROJECT_RATE_RE = re.compile(
    r"^(.*?)\bproduction\s+budget\s+is\s+([\d,]+(?:\.\d+)?)\s*(?:per\s*day|/\s*day|a\s*day)\b(?:\s+for\s+(\d+)\s*days?)?",
    re.IGNORECASE,
)

# "Follow up WITH <project> on <date>" — "with" introduces the subject,
# so it can't also be part of the field keyword (see next_follow_up_at's
# phrase list above); handled as its own structural pattern instead.
_FOLLOW_UP_WITH_RE = re.compile(r"\bfollow[\s-]?up\s+with\s+(.+?)\s+on\s+(.+)$", re.IGNORECASE)

# "Rahul's is 5 PM" — bare subject + "is" + value, no field keyword of
# its own (elided — see the carry-over comment in _parse_smart_message).
_BARE_SUBJECT_IS_RE = re.compile(r"^([A-Za-z][\w'.]*)(?:'s)?\s+is\s+(.+)$", re.IGNORECASE)


def _classify_and_parse_clause(clause: str) -> Optional[dict]:
    """One clause -> {"scope": "project"|"talent", "subjects": [...],
    "fields": {field_key: raw_value}} or None if nothing recognisable."""
    fu_m = _FOLLOW_UP_WITH_RE.search(clause)
    if fu_m:
        subject = _trim_trailing_stopwords(fu_m.group(1).strip())
        if _is_plausible_name(subject):
            return {"scope": "project", "subjects": [subject], "fields": {"next_follow_up_at": fu_m.group(2).strip()}}

    rate_m = _TALENT_RATE_RE.match(clause)
    if rate_m and _is_plausible_name(rate_m.group(1)):
        names = _split_multi_names(rate_m.group(1))
        if names:
            fields = {"budget_per_day": rate_m.group(2)}
            if rate_m.group(3):
                fields["shooting_days"] = rate_m.group(3)
            return {"scope": "talent", "subjects": names, "fields": fields}

    prate_m = _PROJECT_RATE_RE.match(clause)
    if prate_m:
        subject = _trim_trailing_stopwords(prate_m.group(1).strip())
        subject = subject if _is_plausible_name(subject) else ""
        fields = {"production_budget_per_day": prate_m.group(2)}
        if prate_m.group(3):
            fields["shooting_days"] = prate_m.group(3)
        return {"scope": "project", "subjects": [subject] if subject else [], "fields": fields}

    talent_matches = _find_field_matches(clause, _TALENT_FIELD_PHRASES)
    project_matches = _find_field_matches(clause, _PROJECT_FIELD_PHRASES)
    has_talent_unique = any(k in _TALENT_UNIQUE_FIELDS for _, _, k, _fv in talent_matches)
    has_talent_signal = bool(_TALENT_SIGNAL_RE.search(clause))

    if talent_matches and (has_talent_unique or (has_talent_signal and not project_matches)):
        subject, remainder = _extract_subject_from_clause(clause, talent_matches[0][0])
        names = _split_multi_names(subject) if subject else []
        matches2 = _find_field_matches(remainder, _TALENT_FIELD_PHRASES)
        values = _slice_field_values(remainder, matches2)
        if values and names:
            return {"scope": "talent", "subjects": names, "fields": values}

    if project_matches:
        subject, remainder = _extract_subject_from_clause(clause, project_matches[0][0])
        matches2 = _find_field_matches(remainder, _PROJECT_FIELD_PHRASES)
        values = _slice_field_values(remainder, matches2)
        if values:
            return {"scope": "project", "subjects": [subject] if subject else [], "fields": values}
    return None


# --- Task extraction — multiple "task ... to ..." mentions in one message
_TASK_OPENER_RE = re.compile(
    r"\b(?:add\s+(?:a\s+|another\s+)?tasks?|another\s+task|tasks?)\s+(?:to|for)\b"
    # Phase G — "Remind me about/to X <date>" also creates a task, EXCEPT
    # when it's really "remind me to follow up with X on Y" (a project
    # payment-follow-up date, handled separately by _FOLLOW_UP_WITH_RE —
    # the negative lookahead keeps that phrasing out of task-land).
    r"|\bremind me (?:about|to)(?!\s+follow[\s-]?up)\b",
    re.IGNORECASE,
)


def _extract_one_task(body: str) -> Optional[Dict[str, str]]:
    body = body.strip().rstrip(",. ").lstrip(",. ")
    if not body:
        return None
    hint = ""
    for_to_m = re.match(r"^([A-Za-z][\w'.]*(?:\s+[A-Za-z][\w'.]*){0,3})\s+to\s+(.+)$", body, re.IGNORECASE)
    if for_to_m and _is_plausible_name(for_to_m.group(1)):
        hint = for_to_m.group(1).strip()
        body = for_to_m.group(2).strip()
    remaining, due_at = _strip_date_phrase(body)
    # _clean_extracted_text (generic — used for every captured span, not
    # just task titles) collapses the doubled space left where a date
    # word was removed, and strips a dangling trailing conjunction a
    # boundary can leave behind.
    title = _clean_extracted_text(remaining) or _clean_extracted_text(body)
    return {"title": title, "due_at": due_at or "", "hint": hint} if title else None


def _extract_tasks_from_text(text: str) -> List[Dict[str, str]]:
    openers = list(_TASK_OPENER_RE.finditer(text or ""))
    tasks = []
    for i, m in enumerate(openers):
        end = openers[i + 1].start() if i + 1 < len(openers) else len(text)
        body = text[m.end():end].strip()
        if not body:
            continue
        # One opener ("add TASKS to ...") can introduce a comma/and-
        # separated LIST of distinct tasks ("get the call sheet
        # tomorrow, confirm Shivi's costume trial Monday, and follow up
        # payment Tuesday") — reuses the SAME clause-splitting machinery
        # (date-range and name-list protection included) rather than a
        # second, bespoke list parser.
        for item in _split_into_clauses(body):
            task = _extract_one_task(item)
            if task:
                tasks.append(task)
    return tasks


def _parse_smart_message(text: str) -> dict:
    """Top-level entry: splits a free-text message into project field
    updates, talent field updates (grouped per subject, subject carried
    forward across clauses that don't repeat it — "Google AI shoot is
    26 and 27 August, call time 7:30 AM..." only names Google AI once),
    and task creations. Deterministic regex/keyword matching throughout."""
    text = text or ""
    m0 = _TASK_OPENER_RE.search(text)
    field_text = text[:m0.start()] if m0 else text
    tasks_raw = _extract_tasks_from_text(text[m0.start():]) if m0 else []

    clauses = _split_into_clauses(field_text)
    project_updates: Dict[str, Dict[str, str]] = {}
    talent_updates: List[Tuple[Tuple[str, ...], Dict[str, str]]] = []
    current_project = ""
    current_talents: Tuple[str, ...] = ()
    last_talent_field: Optional[str] = None

    for clause in clauses:
        parsed = _classify_and_parse_clause(clause)
        if not parsed:
            # Elliptical follow-on clause — "Shivi's trial is 3 PM and
            # RAHUL'S IS 5 PM" elides the field name the second time
            # ("trial" isn't repeated); reuse whichever single field the
            # immediately preceding talent clause set, generic carry-
            # over rather than a rule about "trial" specifically.
            bare_m = _BARE_SUBJECT_IS_RE.match(clause)
            if bare_m and last_talent_field and _is_plausible_name(bare_m.group(1)):
                names = _split_multi_names(bare_m.group(1))
                value = _clean_extracted_text(bare_m.group(2))
                if names and value:
                    current_talents = tuple(names)
                    talent_updates.append((current_talents, {last_talent_field: value}))
            continue
        if parsed["scope"] == "talent":
            if parsed["subjects"]:
                current_talents = tuple(parsed["subjects"])
            if current_talents:
                talent_updates.append((current_talents, parsed["fields"]))
            last_talent_field = next(iter(parsed["fields"])) if len(parsed["fields"]) == 1 else None
        else:
            if parsed["subjects"] and parsed["subjects"][0]:
                current_project = parsed["subjects"][0]
            # Store even when current_project is still "" (no subject
            # named anywhere in THIS message yet, e.g. a bare "Set the
            # call time to 7:30 AM." context follow-up) — _resolve_project
            # ("", ctx) already falls back to session_context's
            # last-discussed project; dropping the update here instead
            # would silently lose a legitimate contextual command.
            project_updates.setdefault(current_project, {}).update(parsed["fields"])

    return {"project_updates": project_updates, "talent_updates": talent_updates, "tasks": tasks_raw}


# --- Value normalization + resolution — turns the parser's RAW string
# plan into a fully-resolved plan (real project/talent ids, real CRM
# contact ids, typed values matching production_desk.py's own Pydantic
# models) ready to confirm and apply. -----------------------------------
_PROJECT_FIELD_NORMALIZERS: Dict[str, Any] = {
    "shoot_status": lambda v: _normalize_status_value("shoot_status", v),
    "payment_followup_status": lambda v: _normalize_status_value("payment_followup_status", v),
    "production_budget_per_day": _extract_numeric,
    "production_budget_total": _extract_numeric,
    "shooting_days": lambda v: (int(n) if (n := _extract_numeric(v)) is not None else None),
    "expected_payment_date": _parse_absolute_datetime,
    "next_follow_up_at": _parse_absolute_datetime,
    "confirmation_mail_received": lambda v: True,
    "invoice_raised": lambda v: True,
    "invoice_sent": lambda v: True,
    "payment_in_received": lambda v: True,
    "gst_component_received": lambda v: True,
}
_TALENT_FIELD_NORMALIZERS: Dict[str, Any] = {
    "fitting_status": lambda v: _normalize_status_value("fitting_status", v),
    "look_test_status": lambda v: _normalize_status_value("look_test_status", v),
    "shoot_status": lambda v: _normalize_status_value("shoot_status", v),
    "budget_per_day": _extract_numeric,
    "budget_total": _extract_numeric,
    "commission_percent": _extract_numeric,
    "shooting_days": lambda v: (int(n) if (n := _extract_numeric(v)) is not None else None),
}
_PROJECT_FIELD_LABELS = {
    "shoot_dates": "Shoot dates", "call_time": "Call time", "reporting_time": "Reporting time",
    "shoot_location": "Location", "shoot_status": "Shoot status", "shoot_notes": "Notes",
    "payment_terms": "Payment terms", "expected_payment_date": "Expected payment date",
    "next_follow_up_at": "Next follow-up", "payment_followup_notes": "Follow-up notes",
    "payment_followup_status": "Payment follow-up status",
    "production_budget_per_day": "Production budget/day", "production_budget_total": "Production budget",
    "shooting_days": "Shooting days", "confirmation_mail_received": "Confirmation mail received",
    "invoice_raised": "Invoice raised", "invoice_sent": "Invoice sent",
    "payment_in_received": "Client payment received", "gst_component_received": "GST component received",
    "production_contact_client_id": "Production contact",
    "production_status": "Production status", "shoot_date": "Shoot date (reminders)",
}
_TALENT_FIELD_LABELS = {
    "costume_trial_at": "Costume trial", "costume_trial_location": "Trial location",
    "fitting_status": "Fitting", "look_test_status": "Look test",
    "grooming_requirements": "Grooming", "special_instructions": "Special instructions",
    "shoot_status": "Shoot status", "budget_per_day": "Budget/day", "budget_total": "Budget total",
    "shooting_days": "Shoot days", "commission_percent": "Commission %",
}
_DATE_VALUE_FIELDS = {"expected_payment_date", "next_follow_up_at", "costume_trial_at"}


async def _resolve_or_create_crm_contact(name: str) -> dict:
    """Same lookup-or-create the crm-agent's own executor and ADD_INTENT's
    crew flow use — an existing contact is reused, never duplicated."""
    existing = await db.clients.find_one(
        {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "deleted": {"$ne": True}}
    )
    if existing:
        return {"id": str(existing["_id"]), "name": existing["name"]}
    doc = await insert_client_doc(name=name, contact_type=None, source=f"whatsapp_agent:{AGENT_ID}")
    return {"id": doc["id"], "name": doc["name"]}


def _display_value(key: str, value: Any) -> str:
    if value is True:
        return "Yes"
    if key in _DATE_VALUE_FIELDS:
        return _format_due(value)
    if key == "production_status":
        return str(value).replace("_", " ")
    return str(value)


async def _resolve_smart_plan(text: str, ctx: ExecContext) -> dict:
    """Turns _parse_smart_message's raw string plan into a fully-resolved
    one: real project/talent ids (via the SAME fuzzy resolvers every
    other intent in this file uses — never a second lookup system),
    typed/normalised values, and a human-readable display line per
    change. Non-fatal per-item failures (an unresolvable project/talent
    name) are collected in "errors" rather than aborting the whole
    message — a message with 5 valid changes and 1 typo still applies
    the 5."""
    raw_plan = _parse_smart_message(text)
    resolved: Dict[str, Any] = {"projects": [], "talents": [], "tasks": [], "errors": []}

    for label_hint, fields in raw_plan["project_updates"].items():
        resolution = await _resolve_project(label_hint, ctx)
        if resolution.ambiguous:
            resolved["errors"].append(_ambiguous_project_message(resolution.ambiguous))
            continue
        if resolution.error:
            resolved["errors"].append(resolution.error)
            continue
        project = resolution.project
        applied_fields: Dict[str, Any] = {}
        display: List[str] = []
        for key, raw_value in fields.items():
            if key == "production_contact_client_id":
                client = await _resolve_or_create_crm_contact(raw_value)
                applied_fields[key] = client["id"]
                display.append(f"Production contact → {client['name']}")
                continue
            norm = _PROJECT_FIELD_NORMALIZERS.get(key)
            value = norm(raw_value) if norm else raw_value
            if value is None:
                continue
            applied_fields[key] = value
            display.append(f"{_PROJECT_FIELD_LABELS.get(key, key)} → {_display_value(key, value)}")
            if key == "shoot_dates":
                # Phase G — alongside the free-text shoot_dates write
                # (unchanged), ALSO derive the structured, reminder-only
                # shoot_date when the value is a single, unambiguous day
                # ("shoot is tomorrow", "shoot is 26 August") — never for
                # a range/list ("26 and 27 August"), which stays
                # deliberately un-derived rather than guessed at.
                if not _DATE_AND_RE.search(raw_value):
                    dt = _parse_absolute_datetime(raw_value)
                    if dt:
                        applied_fields["shoot_date"] = dt[:10]
                        display.append(f"Shoot date (reminders) → {dt[:10]}")
        if applied_fields:
            resolved["projects"].append({
                "project_id": project["id"], "label": project["label"],
                "fields": applied_fields, "display": display,
            })

    for names, fields in raw_plan["talent_updates"]:
        for name in names:
            match, project_hit, others = await _find_talent_across_projects(name)
            if not match:
                if others:
                    resolved["errors"].append(f'Found "{name}" locked on more than one project: {", ".join(others)}.')
                else:
                    resolved["errors"].append(f'Couldn\'t find a locked talent matching "{name}".')
                continue
            applied_fields = {}
            display = []
            for key, raw_value in dict(fields).items():
                if key == "costume_trial_at":
                    date_part, location = _split_trial_value(raw_value)
                    dt = _parse_absolute_datetime(date_part)
                    if dt:
                        applied_fields["costume_trial_at"] = dt
                        display.append(f"Costume trial → {_format_due(dt)}")
                    if location:
                        applied_fields["costume_trial_location"] = location
                        display.append(f"Trial location → {location}")
                    continue
                norm = _TALENT_FIELD_NORMALIZERS.get(key)
                value = norm(raw_value) if norm else raw_value
                if value is None:
                    continue
                applied_fields[key] = value
                display.append(f"{_TALENT_FIELD_LABELS.get(key, key)} → {_display_value(key, value)}")
            if applied_fields:
                resolved["talents"].append({
                    "project_id": project_hit["id"], "project_label": project_hit["label"],
                    "talent_id": match["talent_id"], "label": match["name"],
                    "fields": applied_fields, "display": display,
                })

    for t in raw_plan["tasks"]:
        project = None
        task_talent_id = None
        task_talent_label = ""
        if t["hint"]:
            match, project_hit, _others = await _find_talent_across_projects(t["hint"])
            if match:
                project = project_hit
                task_talent_id = match["talent_id"]
                task_talent_label = match["name"]
            else:
                hint_resolution = await _resolve_project(t["hint"], ctx)
                project = hint_resolution.project
        if not project and resolved["projects"]:
            # Prefer a project already mentioned EARLIER IN THIS SAME
            # MESSAGE over stale session history — "Google AI shoot is
            # ... Add a task to get the call sheet tomorrow" must tie
            # the task to Google AI without needing a repeated "for
            # Google AI" hint, and without waiting for a future turn's
            # session-remember to catch up.
            last_proj = resolved["projects"][-1]
            project = {"id": last_proj["project_id"], "label": last_proj["label"]}
        if not project and resolved["talents"]:
            last_tal = resolved["talents"][-1]
            project = {"id": last_tal["project_id"], "label": last_tal["project_label"]}
        if not project:
            session = await session_context.get_session(AGENT_ID, ctx.sender_phone)
            last_id = (session or {}).get("last_project_id")
            if last_id:
                project = {"id": last_id, "label": (session or {}).get("last_project_label") or ""}
        due_txt = f" — due {_format_due(t['due_at'])}" if t["due_at"] else ""
        who_txt = f" ({task_talent_label})" if task_talent_label else ""
        resolved["tasks"].append({
            "title": t["title"], "due_at": t["due_at"] or None,
            "project_id": project["id"] if project else None,
            "project_label": project["label"] if project else "",
            "talent_id": task_talent_id,
            "display": f"{t['title']}{who_txt}{due_txt}",
        })

    return resolved


async def _smart_update_try_auto_execute(collected: dict, ctx: ExecContext) -> Optional[ExecResult]:
    raw = collected.get("raw_text", "")

    # Kickback commands ("Set Rahul's kickback to 5000.") are checked
    # first and handled as their own small plan — see
    # _kickback_command_from_text's docstring for why this can't just be
    # folded into the project/talent/task plan below. Standalone-message
    # only in this pass (not composed with other field updates in the
    # same multi-action message) — a disclosed V1 scope limit.
    kb_cmd = _kickback_command_from_text(raw)
    if kb_cmd:
        plan, err = await _resolve_kickback_write(
            kb_cmd["kb_recipient"], kb_cmd["kb_action"], kb_cmd.get("kb_amount", ""), kb_cmd.get("kb_percent", ""), ctx,
        )
        if err:
            return ExecResult(ok=False, message=err)
        collected["_kb_plan"] = json.dumps(plan)
        return None

    plan = await _resolve_smart_plan(raw, ctx)
    total_items = len(plan["projects"]) + len(plan["talents"]) + len(plan["tasks"])
    if total_items == 0:
        if plan["errors"]:
            return ExecResult(ok=False, message=plan["errors"][0])
        # Only reachable via an EXPLICIT trigger match ("Set ..." with
        # nothing parseable after it) — resolve_bare_reply never hands
        # this intent a message unless the parser already found
        # something, so a genuinely unrelated message never reaches
        # here at all (it's silently ignored upstream, same as before).
        return ExecResult(ok=False, message="I couldn't tell what to update from that — try naming the field and project, e.g. \"Set call time for Google AI to 7:30 AM.\"")
    # Remember whichever project was touched, for a natural context
    # follow-up next turn ("Set the call time to 7:30." after asking
    # about Google AI).
    if plan["projects"]:
        await _remember_project(ctx, {"id": plan["projects"][0]["project_id"], "label": plan["projects"][0]["label"]})
    elif plan["talents"]:
        await _remember_project(ctx, {"id": plan["talents"][0]["project_id"], "label": plan["talents"][0]["project_label"]})
    collected["_plan"] = json.dumps(plan)
    return None  # proceed to the normal confirm/edit/cancel card


async def _smart_update_build_confirmation(collected: dict, ctx: ExecContext) -> str:
    if collected.get("_kb_plan"):
        kb = json.loads(collected["_kb_plan"])
        amount_txt = _format_inr(float(kb.get("amount") or 0))
        if kb["action"] == "remove":
            return f"Remove {kb['name']}'s kickback ({amount_txt}) on {kb['project_label']}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."
        verb = "Update" if kb.get("existing_id") else "Add"
        return f"{verb} kickback for {kb['name']} on {kb['project_label']}: {amount_txt}?\n\nReply 1 to confirm, 2 to edit, 3 to cancel."

    plan = json.loads(collected.get("_plan") or "{}")
    lines: List[str] = []
    n = 0
    for p in plan.get("projects", []):
        lines.append(f"{p['label']}:")
        for d in p["display"]:
            n += 1
            lines.append(f"  {n}. {d}")
    for t in plan.get("talents", []):
        lines.append(f"{t['label']} ({t['project_label']}):")
        for d in t["display"]:
            n += 1
            lines.append(f"  {n}. {d}")
    if plan.get("tasks"):
        lines.append("Tasks:")
        for t in plan["tasks"]:
            n += 1
            proj_txt = f" for {t['project_label']}" if t.get("project_label") else ""
            lines.append(f"  {n}. {t['display']}{proj_txt}")

    header = f"I found {n} change{'s' if n != 1 else ''}:\n\n"
    body = "\n".join(lines)
    errs = plan.get("errors") or []
    err_txt = ("\n\n⚠ Couldn't understand:\n" + "\n".join(f"  • {e}" for e in errs)) if errs else ""
    return header + body + err_txt + "\n\nReply 1 to confirm, 2 to edit, 3 to cancel."


async def _smart_update_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    from routers import workflow as workflow_router

    if collected.get("_kb_plan"):
        kb = json.loads(collected["_kb_plan"])
        return await _apply_kickback_write(
            kb["action"], kb["project_id"], kb["name"],
            client_id=kb.get("client_id", ""), existing_id=kb.get("existing_id", ""), amount=float(kb.get("amount") or 0),
        )

    plan = json.loads(collected.get("_plan") or "{}")
    applied: List[str] = []
    failed: List[str] = []

    for p in plan.get("projects", []):
        try:
            await pd.update_production_desk_project(p["project_id"], pd.ProductionDeskProjectPatch(**p["fields"]), _AGENT_ADMIN)
            applied.append(f"✓ {p['label']}: {', '.join(p['display'])}")
        except (HTTPException, ValueError) as e:
            failed.append(f"{p['label']}: {getattr(e, 'detail', e)}")

    for t in plan.get("talents", []):
        try:
            await pd.update_locked_talent_production(t["project_id"], t["talent_id"], pd.TalentProductionPatch(**t["fields"]), _AGENT_ADMIN)
            applied.append(f"✓ {t['label']}: {', '.join(t['display'])}")
        except (HTTPException, ValueError) as e:
            failed.append(f"{t['label']}: {getattr(e, 'detail', e)}")

    for tk in plan.get("tasks", []):
        try:
            payload = workflow_router.TaskIn(
                title=tk["title"],
                category="project" if tk.get("project_id") else "general",
                project_id=tk.get("project_id"),
                project_name=tk.get("project_label") or "",
                talent_id=tk.get("talent_id"),
                due_at=tk.get("due_at"),
                priority="normal",
            )
            created = await workflow_router.create_task(payload, {"id": _AGENT_ADMIN["id"], "role": "admin"})
            await _remember_task(ctx, created.get("id", ""), tk["title"])
            applied.append(f"✓ Task: {tk['title']}")
        except Exception as e:  # noqa: BLE001 — best-effort per-item, never abort the batch
            failed.append(f"Task '{tk['title']}': {e}")

    lines: List[str] = []
    if applied:
        lines.extend(applied)
    if failed:
        lines.append("⚠ Some items failed:")
        lines.extend(f"  • {f}" for f in failed)
    if not lines:
        lines = ["Nothing was applied."]
    return ExecResult(ok=bool(applied), message="\n".join(lines))


SMART_UPDATE_INTENT = IntentDefinition(
    intent_id="management.smart_update",
    # A handful of explicit imperative openers ("Set the shoot date...")
    # for messages that DO start with a fixed word; every other shape
    # ("Google AI shoot is...", "shivi costume trial tomorrow...",
    # "invoice sent for google ai") is reached via resolve_bare_reply
    # below (the platform's own "no trigger matched, give the agent one
    # more try" hook — not a second dispatch mechanism).
    triggers=["set", "update", "please update", "please set", "can you set", "can you update", "need to set", "just note that", "note that", "save this", "put this in the project"],
    fields=[FieldSpec(key="raw_text", label="Update", question="", validate=lambda v: ValidationResult(ok=True, value=v), required=False)],
    extract_fields=lambda text: {"raw_text": text},
    try_auto_execute=_smart_update_try_auto_execute,
    build_confirmation=_smart_update_build_confirmation,
    executor=_smart_update_executor,
)


async def _resolve_bare_reply(text: str, ctx: ExecContext) -> Optional[Tuple[IntentDefinition, Dict[str, str]]]:
    """The platform's existing "no trigger matched this turn, no active
    conversation — give the agent one last chance" hook (agents/models.py
    docstring). Runs the SAME smart parser a triggered "set..." message
    would use; only claims the message when it actually found something
    recognisable, so unrelated chatter in the group still falls through
    to being silently ignored exactly as before."""
    plan = _parse_smart_message(text)
    if plan["project_updates"] or plan["talent_updates"] or plan["tasks"]:
        return SMART_UPDATE_INTENT, {"raw_text": text}
    # "Rahul has a ₹5,000 kickback." / "Rahul's kickback is 5000." /
    # "Remove Rahul's kickback." — none of these open with a fixed
    # trigger word, so they only ever reach here. Routes into
    # SMART_UPDATE_INTENT's own kickback branch (see
    # _smart_update_try_auto_execute) — the SAME resolution/confirm/
    # execute path "Set Rahul's kickback to 5000." already uses.
    if _kickback_command_from_text(text):
        return SMART_UPDATE_INTENT, {"raw_text": text}
    # "Shivi's travel reimbursement is 2500." — no fixed trigger word.
    bare_reimb = _bare_reimbursement_fields(text)
    if bare_reimb:
        return ADD_INTENT, bare_reimb
    bare_crew = _bare_crew_command_fields(text)
    if bare_crew:
        return ADD_INTENT, bare_crew
    # "Is Shivi ready for Google AI?" / "Who isn't ready for tomorrow?" —
    # a READ query with no registered trigger of its own (a bare "is" is
    # deliberately NOT a trigger — far too broad). Routes into
    # STATUS_QUERY_INTENT's existing (auto_confirm) executor, which
    # already knows how to read talent+topic="readiness" or the global
    # needs-attention aggregation.
    _, topic = _extract_talent_and_topic(text)
    if topic == "readiness" or _NEEDS_ATTENTION_RE.search(text):
        return STATUS_QUERY_INTENT, {"raw_text": text}
    # "What shoots are coming up?" / "Who is shooting tomorrow?" — same
    # trailing-punctuation safety net as the needs-attention/readiness
    # checks just above (only claimed when _render_shoots_query would
    # actually have something to say — a bare "shoot" substring alone,
    # with no timeframe/project, is too weak a signal to claim here).
    if (
        (_SHOOTS_QUERY_RE.search(text) or _SHOOTS_HAPPENING_RE.search(text))
        and _SHOOTS_TIMEFRAME_RE.search(text)
        and not _extract_trailing_project(text)
    ):
        return STATUS_QUERY_INTENT, {"raw_text": text}
    # "The call sheet task is done." — bare, no fixed trigger. Checked
    # LAST and only claimed once a REAL task is actually found matching
    # the hint (never a guess) — this keeps "Shivi's fitting is
    # complete" and every other "X is complete/done" sentence the
    # earlier checks already own from ever reaching here (they're
    # claimed above, before this point) or, on a genuine miss, simply
    # falling through to unhandled/ignored exactly as before.
    cmd = _classify_task_command(text)
    if cmd and cmd["action"] in ("complete", "reopen"):
        task, _err = await _resolve_task_reference(cmd["hint"], ctx)
        if task:
            return TASK_MANAGE_INTENT, {"raw_text": text}
    return None


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
        TASK_MANAGE_INTENT,
        SMART_UPDATE_INTENT,
    ],
    # The natural-language multi-field/multi-action engine (Part 15-20 of
    # the NLU pass) — most such messages don't start with any fixed
    # trigger word ("Google AI shoot is...", "invoice sent for google
    # ai"), so they're only ever reached here, not via detect_trigger.
    resolve_bare_reply=_resolve_bare_reply,
    help_text=HELP_TEXT,
)


def register() -> None:
    register_agent(MANAGEMENT_AGENT)
