"""Production Reminder Worker (Phase G, 2026-09-07).

Deterministic (NO AI, NO LLM, NO new WhatsApp infrastructure) polling loop
that turns already-existing Production Desk / workflow_tasks data into
WhatsApp reminders, delivered through the EXISTING WhatsApp job queue
(db.whatsapp_batches + db.whatsapp_jobs, routers/whatsapp.py's
create_batch) and sent by the EXISTING WhatsApp Worker
(whatsapp-worker/worker.py) — no new agent, WhatsApp number, worker
process, or data store.

Architecture (mirrors services/media_assignment_worker.py's shape almost
exactly — see that file's own docstring for the established pattern this
follows):

    Production Desk / workflow_tasks   (source of truth, read-only here)
            |
            v
    this loop (_check_* functions, one per reminder kind, run every
               POLL_SEC seconds)
            |
            v
    create_batch()  — a MANUAL/group recipient + the "custom" WhatsApp
                       template (routers/whatsapp.py) — the SAME function
                       every admin-triggered send already goes through
            |
            v
    db.whatsapp_batches / db.whatsapp_jobs   (existing collections)
            |
            v
    the existing WhatsApp Worker (whatsapp-worker/worker.py) polls and
    sends, exactly as it does for every other job — including its
    existing retry/circuit-breaker/verification behaviour.

Inspection findings (Section 1 of the Phase G brief), briefly:
  - notifications.py is a PURE in-app admin-bell system — it has never
    sent a WhatsApp message and has no group-routing concept at all
    (its own docstring: "We DO NOT send email yet", and grep confirms
    zero WhatsApp coupling). The "existing WhatsApp job queue" in the
    brief's architecture diagram is whatsapp_batches/whatsapp_jobs,
    confirmed by reading whatsapp-worker/worker.py's
    poll_and_process_jobs: it only ever claims a "pending" job that
    belongs to a "pending"/"running" BATCH — a job with no batch is never
    sent. This module does not touch notifications.py.
  - whatsapp_jobs already supports destination_type="group" (used today
    for a talent's own whatsapp_group_name) — the exact same path is
    reused here for the Management Agent's group, via a MANUAL
    ManualContact(phone="", whatsapp_group_name=<group>).
  - The Management Agent's WhatsApp group is never hardcoded here — it's
    looked up from db.whatsapp_agent_config (agent_id="management-agent"),
    the SAME registry every other agent's routing already uses.
  - Production Desk has NO structured, reliably-parseable shoot date —
    project.shoot_dates is free text BY DELIBERATE DESIGN (see that
    field's own comment in routers/production_desk.py). A new, small,
    optional field, project.pd_shoot_date, was added specifically to give
    shoot reminders one unambiguous date to key off, without touching the
    free-text field's semantics. It's auto-derived by the Management
    Agent's NLU only when a SINGLE unambiguous day is parsed (never for a
    range/list), and is otherwise simply unset — an honest "no shoot
    reminder for this project" rather than a guess.

Idempotency (Section 5): no new "reminder" collection. Each reminder kind
is tracked by ONE small additive field on the record it already belongs
to (workflow_tasks / casting_pipeline / projects), storing the exact
source-date VALUE the reminder was last sent for — not just a boolean —
so changing that value (a rescheduled trial, a moved due date) makes the
record eligible again automatically, with no separate "did this change
under me" check needed. Claims are atomic (find_one_and_update filtered on
the still-current value), the same idiom whatsapp-worker/worker.py itself
already uses for job claims — so re-polling, or in principle two worker
instances, can never double-send. A rescheduled shoot's OLD reminders can
never fire again either, because the check always recomputes the timing
window from the CURRENT field value; there is no stored "reminder due at"
to go stale.

Timezone (Section 15): "today" is computed once per cycle from REAL
current time in Asia/Kolkata, the business timezone (see _ist_today()) —
the one place this module genuinely needs a timezone conversion, since it
is comparing against the actual current moment. Stored date/datetime
values (costume_trial_at, due_at, pd_shoot_date, pd_next_follow_up_at) are
compared using their date components exactly AS WRITTEN, with NO further
timezone conversion applied to them — every "_at" field already carries a
UTC tzinfo tag, but (see management_agent.py's own date parser) the
hour/day values are literally what the sender typed, not independently
shifted at write time. Converting them through UTC->IST math a second
time here would risk moving a date-only value across a day boundary the
sender never intended — exactly what Section 15 warns against. Task
due-at reminders are the one exception: "due" is evaluated as a genuine
absolute-instant comparison (due_at <= now), which needs no timezone
handling at all since both sides are real timestamps.

Known, disclosed, NOT fixed in this phase: management_agent.py's existing
_parse_due_date/_parse_absolute_datetime (Phase E/F, already shipped and
tested) compute "today"/"tomorrow" from `datetime.now(timezone.utc)`, not
IST — for roughly the UTC-afternoon/evening hours (when IST has already
rolled to the next calendar day), a bare "tomorrow" typed then could
resolve to a date one day earlier than a strict IST reading would give.
This is a pre-existing NLU-input-parsing characteristic, not something
this worker's own comparisons are affected by (they read whatever date
ended up stored, correctly, regardless of how it got there) — flagged for
visibility, deliberately left unchanged to avoid touching already-shipped,
already-tested Phase F code outside this phase's stated scope.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from core import db
from agents import registry
from agents.modules.whatsapp_campaign_agent import _service_admin
from routers.whatsapp import BatchIn, ManualContact, SourceParams, create_batch

logger = logging.getLogger(__name__)

_worker_task = None

# Conservative — reminders don't need sub-minute latency, and every check
# is a handful of cheap Mongo queries, not a WhatsApp round-trip (that
# part is the existing worker's job, on its own poll loop).
POLL_SEC = 90

IST = ZoneInfo("Asia/Kolkata")

# Reminder timing — centralized here so these can change later without
# touching any of the _check_* logic below (Section 4).
SHOOT_DAY_BEFORE_OFFSET = timedelta(days=1)

# Only projects in these statuses are reminder-eligible — the SAME set
# Production Desk / the Management Agent already use everywhere else
# (agents/modules/management_agent.py's PRODUCTION_DESK_RELEVANT_STATUSES).
_RELEVANT_PROJECT_STATUSES = ["ongoing", "locked"]

_ACTIVE_TASK_STATUSES = ["pending", "in_progress"]


# ---------------------------------------------------------------------------
# Small date/time helpers
# ---------------------------------------------------------------------------
def _ist_today() -> date:
    return datetime.now(IST).date()


def _date_of(iso_str: Optional[str]) -> Optional[date]:
    """Calendar date exactly AS WRITTEN — no timezone conversion. See the
    module docstring's Timezone section for why."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).date()
    except ValueError:
        try:
            return date.fromisoformat(str(iso_str)[:10])
        except ValueError:
            return None


def _fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d %b") if d else "—"


def _fmt_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    txt = dt.strftime("%I:%M %p").lstrip("0")
    return txt or "12:00 AM"


def _join_names(names: List[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return "The"
    return " & ".join(names) + "'s"


# ---------------------------------------------------------------------------
# Outbound send — reuses the EXISTING WhatsApp job queue + worker (Section
# 7/8). No new outbound mechanism, no hardcoded group.
# ---------------------------------------------------------------------------
async def _management_agent_group() -> Optional[str]:
    cfg = await db[registry.CONFIG_COLLECTION].find_one(
        {"agent_id": "management-agent", "active": True}
    )
    names = (cfg or {}).get("group_names") or []
    return names[0] if names else None


async def _send_reminder(message_text: str) -> bool:
    """Enqueues one job via the existing create_batch() path — the exact
    function every admin-triggered send in the app already goes through.
    Returns True once the batch/job docs are created (the existing
    WhatsApp Worker owns actually delivering it from here)."""
    group = await _management_agent_group()
    if not group:
        logger.warning("production_reminder_worker: no active management-agent group configured, cannot send")
        return False
    custom_template = await db.whatsapp_templates.find_one({"slug": "custom"}, {"_id": 0, "id": 1})
    if not custom_template:
        logger.warning("production_reminder_worker: no 'custom' WhatsApp template found, cannot send")
        return False
    try:
        admin = await _service_admin()
        batch_in = BatchIn(
            source_type="MANUAL",
            source_params=SourceParams(contacts=[
                ManualContact(name="Production Reminder", phone="", whatsapp_group_name=group)
            ]),
            template_id=custom_template["id"],
            variable_data={"message": message_text},
        )
        await create_batch(batch_in, admin=admin)
        return True
    except Exception:
        logger.exception("production_reminder_worker: failed to enqueue reminder")
        return False


async def _relevant_project_ids() -> Set[str]:
    docs = await db.projects.find(
        {"status": {"$in": _RELEVANT_PROJECT_STATUSES}}, {"_id": 0, "id": 1}
    ).to_list(10000)
    return {d["id"] for d in docs}


async def _locked_talent_names(project_id: str) -> List[str]:
    rows = await db.casting_pipeline.find(
        {"project_id": project_id, "stage": "locked"}, {"_id": 0, "talent_id": 1}
    ).to_list(200)
    ids = [r["talent_id"] for r in rows if r.get("talent_id")]
    if not ids:
        return []
    docs = await db.talents.find({"id": {"$in": ids}}, {"_id": 0, "name": 1}).to_list(len(ids))
    return [d.get("name") or "Talent" for d in docs]


# ---------------------------------------------------------------------------
# 1. Tasks — Section 3/4: remind once the task becomes due.
# ---------------------------------------------------------------------------
async def _check_tasks() -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    candidates = await db.workflow_tasks.find({
        "due_at": {"$ne": None, "$lte": now_iso},
        "status": {"$in": _ACTIVE_TASK_STATUSES},
    }, {"_id": 0}).to_list(500)

    sent = 0
    for t in candidates:
        due_at = t.get("due_at")
        if t.get("reminder_sent_for_due_at") == due_at:
            continue
        claimed = await db.workflow_tasks.find_one_and_update(
            {
                "id": t["id"],
                "status": {"$in": _ACTIVE_TASK_STATUSES},
                "due_at": due_at,
                "reminder_sent_for_due_at": {"$ne": due_at},
            },
            {"$set": {"reminder_sent_for_due_at": due_at}},
        )
        if not claimed:
            continue  # lost the race, or status/due_at changed since the query above

        project_label = t.get("project_name") or ""
        prefix = f"{project_label} — " if project_label else ""
        message = f"Task Due\n{prefix}{t.get('title') or 'Task'}.\nDue today."
        if await _send_reminder(message):
            sent += 1
        else:
            await db.workflow_tasks.update_one(
                {"id": t["id"]}, {"$set": {"reminder_sent_for_due_at": None}}
            )
    return sent


# ---------------------------------------------------------------------------
# 2. Costume trials — Section 3/4: one reminder on the day of the trial.
# casting_pipeline is the per-(project,talent) locked record — see
# routers/production_desk.py's update_locked_talent_production.
# ---------------------------------------------------------------------------
async def _check_costume_trials(relevant_project_ids: Set[str]) -> int:
    today = _ist_today()
    rows = await db.casting_pipeline.find({
        "stage": "locked",
        "pd_costume_trial_at": {"$ne": None},
        "pd_fitting_status": {"$ne": "completed"},
    }, {"_id": 0}).to_list(2000)

    sent = 0
    for row in rows:
        if row.get("project_id") not in relevant_project_ids:
            continue
        trial_at = row.get("pd_costume_trial_at")
        if _date_of(trial_at) != today:
            continue
        if row.get("pd_costume_trial_reminder_sent_for") == trial_at:
            continue
        claimed = await db.casting_pipeline.find_one_and_update(
            {
                "project_id": row["project_id"], "talent_id": row["talent_id"],
                "pd_costume_trial_at": trial_at,
                "pd_fitting_status": {"$ne": "completed"},
                "pd_costume_trial_reminder_sent_for": {"$ne": trial_at},
            },
            {"$set": {"pd_costume_trial_reminder_sent_for": trial_at}},
        )
        if not claimed:
            continue

        project = await db.projects.find_one({"id": row["project_id"]}, {"_id": 0, "brand_name": 1})
        talent = await db.talents.find_one({"id": row["talent_id"]}, {"_id": 0, "name": 1})
        proj_label = (project or {}).get("brand_name") or "(untitled project)"
        talent_name = (talent or {}).get("name") or "Talent"
        time_txt = _fmt_time(trial_at)
        when = f" at {time_txt}" if time_txt else ""
        lines = ["Costume Trial Reminder", f"{proj_label} — {talent_name}'s costume trial is today{when}."]
        if row.get("pd_costume_trial_location"):
            lines.append(f"Location: {row['pd_costume_trial_location']}")
        if await _send_reminder("\n".join(lines)):
            sent += 1
        else:
            await db.casting_pipeline.update_one(
                {"project_id": row["project_id"], "talent_id": row["talent_id"]},
                {"$set": {"pd_costume_trial_reminder_sent_for": None}},
            )
    return sent


# ---------------------------------------------------------------------------
# 3. Shoot — Section 3/4: one reminder the day before, one the morning of.
# Project-level (call time/location/date are project fields); one combined
# message names every currently-locked talent rather than one message per
# talent, keeping this project-scoped like every other shoot field.
# ---------------------------------------------------------------------------
async def _check_shoots(relevant_project_ids: Set[str]) -> int:
    today = _ist_today()
    projects = await db.projects.find({
        "id": {"$in": list(relevant_project_ids)},
        "pd_shoot_date": {"$ne": None},
        "pd_shoot_status": {"$nin": ["cancelled", "completed"]},
    }, {"_id": 0}).to_list(5000)

    sent = 0
    for p in projects:
        shoot_date = _date_of(p.get("pd_shoot_date"))
        if not shoot_date:
            continue
        if shoot_date - SHOOT_DAY_BEFORE_OFFSET == today:
            kind, when_txt = "day_before", "tomorrow"
        elif shoot_date == today:
            kind, when_txt = "morning_of", "today"
        else:
            continue

        sent_field = f"pd_shoot_reminder_{kind}_sent_for"
        shoot_date_raw = p.get("pd_shoot_date")
        if p.get(sent_field) == shoot_date_raw:
            continue
        claimed = await db.projects.find_one_and_update(
            {
                "id": p["id"], "pd_shoot_date": shoot_date_raw,
                "pd_shoot_status": {"$nin": ["cancelled", "completed"]},
                sent_field: {"$ne": shoot_date_raw},
            },
            {"$set": {sent_field: shoot_date_raw}},
        )
        if not claimed:
            continue

        who = _join_names(await _locked_talent_names(p["id"]))
        proj_label = p.get("brand_name") or "(untitled project)"
        lines = ["Shoot Reminder", f"{proj_label} — {who} shoot is {when_txt}.", f"Date: {_fmt_date(shoot_date)}"]
        if p.get("pd_call_time"):
            lines.append(f"Call time: {p['pd_call_time']}")
        if p.get("pd_reporting_time"):
            lines.append(f"Reporting time: {p['pd_reporting_time']}")
        if p.get("pd_shoot_location"):
            lines.append(f"Location: {p['pd_shoot_location']}")
        if await _send_reminder("\n".join(lines)):
            sent += 1
        else:
            await db.projects.update_one({"id": p["id"]}, {"$set": {sent_field: None}})
    return sent


# ---------------------------------------------------------------------------
# 4. Payment follow-up — Section 3/4: one reminder on the configured date.
# Operational only — see production_desk.py's own module docstring on why
# this is deliberately NOT a Finance record.
# ---------------------------------------------------------------------------
async def _check_payment_followups(relevant_project_ids: Set[str]) -> int:
    today = _ist_today()
    projects = await db.projects.find({
        "id": {"$in": list(relevant_project_ids)},
        "pd_next_follow_up_at": {"$ne": None},
        "pd_payment_followup_status": {"$ne": "done"},
    }, {"_id": 0}).to_list(5000)

    sent = 0
    for p in projects:
        follow_at = p.get("pd_next_follow_up_at")
        if _date_of(follow_at) != today:
            continue
        if p.get("pd_payment_followup_reminder_sent_for") == follow_at:
            continue
        claimed = await db.projects.find_one_and_update(
            {
                "id": p["id"], "pd_next_follow_up_at": follow_at,
                "pd_payment_followup_status": {"$ne": "done"},
                "pd_payment_followup_reminder_sent_for": {"$ne": follow_at},
            },
            {"$set": {"pd_payment_followup_reminder_sent_for": follow_at}},
        )
        if not claimed:
            continue

        proj_label = p.get("brand_name") or "(untitled project)"
        message = f"Payment Follow-up\n{proj_label} — client payment follow-up is due today."
        if await _send_reminder(message):
            sent += 1
        else:
            await db.projects.update_one(
                {"id": p["id"]}, {"$set": {"pd_payment_followup_reminder_sent_for": None}}
            )
    return sent


# ---------------------------------------------------------------------------
# Poll loop — mirrors services/media_assignment_worker.py's exact shape.
# ---------------------------------------------------------------------------
async def run_reminder_cycle() -> int:
    """One full pass over every reminder kind. Exposed (not just internal
    to the loop) so tests can call it directly/repeatedly without waiting
    on POLL_SEC, and so it can be invoked ad hoc if ever needed."""
    relevant_ids = await _relevant_project_ids()
    total = 0
    total += await _check_tasks()
    total += await _check_costume_trials(relevant_ids)
    total += await _check_shoots(relevant_ids)
    total += await _check_payment_followups(relevant_ids)
    return total


async def _worker_loop() -> None:
    logger.info("production_reminder_worker: starting persistent reminder loop...")
    while True:
        try:
            n = await run_reminder_cycle()
            if n:
                logger.info("production_reminder_worker: sent %d reminder(s) this cycle", n)
        except Exception:
            logger.exception("production_reminder_worker: unexpected error in reminder cycle")
        await asyncio.sleep(POLL_SEC)


def start_production_reminder_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker_loop())
