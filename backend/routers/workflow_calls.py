"""Workflow → Calls — a fast, repetitive team follow-up log for calling
talents about ongoing projects (2026-09-22).

Scope (deliberately narrow — see the brief this was built from): this is
ADDITIVE only. It reads the EXISTING projects/casting_pipeline/talents/
users collections (never writes to any of them) and owns exactly two new
collections of its own:

  talent_project_calls              — append-only call history. Every
                                       "Save" creates ONE new row; nothing
                                       is ever overwritten or deleted here.
  talent_project_call_assignments   — current "who should call this
                                       Talent+Project" state, one doc per
                                       (talent_id, project_id), upserted on
                                       assignment. Deliberately separate
                                       from the history collection (the
                                       brief's own "assignment needs
                                       persistent state, store it
                                       separately" guidance) — assigning a
                                       row never creates a call record, and
                                       recording a call never changes who
                                       it's assigned to.

The "latest call" and "current pipeline stage" shown in the list are
ALWAYS derived live (an aggregation over talent_project_calls sorted by
called_at, and a direct read of db.casting_pipeline respectively) — never
duplicated/cached state that could drift from the source of truth. This
mirrors the brief's own explicit instruction and the same "read-only
join over existing collections" shape
routers.whatsapp._compute_ongoing_pipeline_reminders already uses for its
own "Ongoing Project Talents" list (same query shape: db.projects
{status: "ongoing"} -> db.casting_pipeline scoped to those project ids ->
db.talents) — not reused directly (that function's own eligibility rule
is narrower, follow_up-only), but the same proven pattern.

A call's `update_status` ("Sending"/"Not Sending"/"Not Interested") is
NEVER written back to db.casting_pipeline.stage — see the brief's own
explicit "Call Update vs Pipeline" requirement. Nothing in this file ever
calls agents/modules/casting_pipeline.py's pipeline-mutation functions or
touches the casting_pipeline collection with a write of any kind.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from core import db, current_team_or_admin, require_role, _now, active_only
from routers.casting_pipeline import _normalise_stage, PIPELINE_STAGE_ORDER
from .workflow_calls_schemas import CallIn, AssignIn, CALL_RESULTS, UPDATE_STATUSES
# Workflow -> WhatsApp notifications (2026-09-28 refinement pass, PART 4-8):
# reuses the EXACT same shared formatter/enqueue infrastructure
# routers.workflow already built for Tasks — same whatsapp_batches/
# whatsapp_jobs queue, same "Talentgram Workflow" group, no second
# notification system. Calls has no generic "update an existing call"
# endpoint (call records are append-only, assignment is separate — see
# this file's own module docstring), so only the two real mutation
# points below (assign, record) get a notification.
from .workflow import (
    _format_workflow_message,
    _fmt_event_ts,
    _resolve_user_names,
    enqueue_workflow_whatsapp_notification,
    WORKFLOW_APP_URL,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow/calls", tags=["workflow-calls"])

CALLS_COLLECTION = "talent_project_calls"
ASSIGNMENTS_COLLECTION = "talent_project_call_assignments"

CALL_RESULT_LABELS = {
    "answered": "Answered", "no_answer": "No Answer", "busy": "Busy",
    "switched_off": "Switched Off", "call_back": "Call Back",
}
UPDATE_STATUS_LABELS = {
    "sending": "Sending", "not_sending": "Not Sending", "not_interested": "Not Interested", "other": "Other",
}


def _call_link() -> str:
    """Calls are a different entity than a Workflow Task (own collection,
    own tab) — there is no per-call deep-link route to reuse (see this
    task's own audit), so the smallest safe link is the same Workflow app
    URL already used everywhere else, not a newly-invented URL structure."""
    return WORKFLOW_APP_URL


def _build_call_assigned_message(talent_name: str, project_name: Optional[str], assignee_name: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "📞 *CALL ASSIGNED*", subject_label="Talent", subject_name=talent_name, project_name=project_name,
        blocks=[[f"Assigned to: {assignee_name}", f"Assigned by: {actor_name}"], [f"Assigned: {_fmt_event_ts(event_ts)}"]],
        link=_call_link(), link_label="Open Call:",
    )


def _build_call_reassigned_message(talent_name: str, project_name: Optional[str], old_assignee_name: str, new_assignee_name: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "📞 *CALL REASSIGNED*", subject_label="Talent", subject_name=talent_name, project_name=project_name,
        blocks=[
            [f"Previously assigned to: {old_assignee_name}", f"Now assigned to: {new_assignee_name}", f"Updated by: {actor_name}"],
            [f"Updated: {_fmt_event_ts(event_ts)}"],
        ],
        link=_call_link(), link_label="Open Call:",
    )


def _build_call_recorded_message(talent_name: str, project_name: Optional[str], call_result: str, update_status: Optional[str], actor_name: str, event_ts: str) -> str:
    # "Recorded"/"Record" — the app's own existing terminology (the
    # button/modal are literally labelled "Record"), not "Completed" (PART
    # 7's own explicit "use the actual terminology" instruction).
    content = [f"Result: {CALL_RESULT_LABELS.get(call_result, call_result)}"]
    if update_status:
        content.append(f"Update: {UPDATE_STATUS_LABELS.get(update_status, update_status)}")
    return _format_workflow_message(
        "📞 *CALL RECORDED*", subject_label="Talent", subject_name=talent_name, project_name=project_name,
        blocks=[content, [f"Recorded by: {actor_name}"], [f"Recorded: {_fmt_event_ts(event_ts)}"]],
        link=_call_link(), link_label="Open Call:",
    )


def _user_label(u: Optional[Dict[str, Any]]) -> Optional[str]:
    if not u:
        return None
    return u.get("name") or u.get("email") or u.get("id")


async def _ongoing_project_map(project_ids: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """id -> {id, brand_name} for every ONGOING project (or the subset of
    `project_ids` that are still ongoing, if given) — the single place
    "ongoing" is defined for this feature, matching the application's
    existing project.status enum ("ongoing"/"hold"/"complete"/"locked")
    verbatim, never a second status system."""
    query: Dict[str, Any] = {"status": "ongoing"}
    if project_ids:
        query["id"] = {"$in": project_ids}
    docs = await db.projects.find(active_only(query), {"_id": 0, "id": 1, "brand_name": 1}).to_list(5000)
    return {p["id"]: p for p in docs}


async def _latest_calls_by_pair(pairs: List[tuple]) -> Dict[tuple, Dict[str, Any]]:
    """(talent_id, project_id) -> its most recent call row, in ONE
    aggregation (never loads full history into the app — see the brief's
    own performance requirement). Empty input -> empty result, no query."""
    if not pairs:
        return {}
    or_clauses = [{"talent_id": t, "project_id": p} for t, p in pairs]
    cursor = db[CALLS_COLLECTION].aggregate([
        {"$match": {"$or": or_clauses}},
        {"$sort": {"called_at": -1}},
        {"$group": {"_id": {"talent_id": "$talent_id", "project_id": "$project_id"}, "latest": {"$first": "$$ROOT"}}},
    ])
    out: Dict[tuple, Dict[str, Any]] = {}
    async for doc in cursor:
        row = doc["latest"]
        out[(row["talent_id"], row["project_id"])] = row
    return out


def _call_status_bucket(called_at: Optional[str]) -> str:
    """never | today | recent (<=7 days) | stale (>7 days) — the "Call
    Status" filter's own vocabulary (brief section 14's "Needs Follow Up"
    is treated as an alias for "stale" here, not a distinct signal
    requiring extra bookkeeping — no complicated reporting logic, per the
    brief's own explicit instruction)."""
    if not called_at:
        return "never"
    try:
        dt = datetime.fromisoformat(called_at)
    except (TypeError, ValueError):
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt.date() == now.date():
        return "today"
    if now - dt <= timedelta(days=7):
        return "recent"
    return "stale"


@router.get("")
async def list_calls(
    project_ids: Optional[str] = None,  # comma-separated
    talent_id: Optional[str] = None,
    assignment: Optional[str] = None,  # all | mine | unassigned | assigned
    assigned_to_id_filter: Optional[str] = Query(None, alias="assigned_to_id"),  # exact team member id, or "unassigned" — the "Assigned To" filter (2026-09-28)
    pipeline: Optional[str] = None,  # comma-separated stage keys
    call_status: Optional[str] = None,  # never | today | recent | stale
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_team_or_admin),
):
    """One row per (talent, project) currently in the pipeline of an
    ONGOING project — never a talent with no ongoing-project pipeline
    membership, never a completed/hold/locked project's rows (brief
    section 3/15, a hard requirement).

    Visibility (2026-09-28 — see this task's own audit/report): a "team"
    role user sees the SAME set of calls an admin's default view would —
    every ongoing-project pipeline row, not just rows assigned to them.
    This was a deliberate broadening (previously team members were hard-
    scoped server-side to their own assigned queue only, which made
    "record a call not assigned to you" unreachable through the UI at
    all, since that row was never even visible). Assignment itself
    (POST /assign, reassigning who a call belongs to) remains admin-only
    — this endpoint only ever reads, never writes, `assigned_to_id`.
    `assignment`/`assigned_to_id` are optional narrowing filters
    available to every caller now, not an admin-only capability — a team
    member can equally filter down to "My Calls" or a specific
    colleague's queue within the now-broader set they can see.
    """

    project_id_list = [p for p in (project_ids.split(",") if project_ids else []) if p]
    project_map = await _ongoing_project_map(project_id_list or None)
    if not project_map:
        return {"rows": []}

    pipeline_query: Dict[str, Any] = {"project_id": {"$in": list(project_map.keys())}}
    if talent_id:
        pipeline_query["talent_id"] = talent_id
    pipeline_rows = await db.casting_pipeline.find(
        active_only(pipeline_query), {"_id": 0, "project_id": 1, "talent_id": 1, "stage": 1},
    ).to_list(20000)
    if not pipeline_rows:
        return {"rows": []}

    stage_filter = set(s for s in (pipeline.split(",") if pipeline else []) if s)
    filtered_rows = []
    for r in pipeline_rows:
        stage = _normalise_stage(r.get("stage")) or r.get("stage")
        if stage_filter and stage not in stage_filter:
            continue
        filtered_rows.append((r["talent_id"], r["project_id"], stage))
    if not filtered_rows:
        return {"rows": []}

    talent_ids = sorted({t for t, _, _ in filtered_rows})
    talent_docs = await db.talents.find(
        active_only({"id": {"$in": talent_ids}}, exclude_archived=True), {"_id": 0, "id": 1, "name": 1, "phone": 1},
    ).to_list(len(talent_ids))
    talent_by_id = {t["id"]: t for t in talent_docs}

    pairs = [(t, p) for t, p, _ in filtered_rows]
    assignment_docs = await db[ASSIGNMENTS_COLLECTION].find(
        {"$or": [{"talent_id": t, "project_id": p} for t, p in pairs]}, {"_id": 0},
    ).to_list(len(pairs)) if pairs else []
    assignment_by_pair = {(a["talent_id"], a["project_id"]): a for a in assignment_docs}

    latest_call_by_pair = await _latest_calls_by_pair(pairs)

    assigned_user_ids = sorted({a["assigned_to_id"] for a in assignment_docs if a.get("assigned_to_id")})
    called_by_ids = sorted({c.get("called_by") for c in latest_call_by_pair.values() if c.get("called_by")})
    user_ids_needed = sorted(set(assigned_user_ids) | set(called_by_ids))
    users_by_id: Dict[str, Dict[str, Any]] = {}
    if user_ids_needed:
        user_docs = await db.users.find(
            {"id": {"$in": user_ids_needed}}, {"_id": 0, "id": 1, "name": 1, "email": 1},
        ).to_list(len(user_ids_needed))
        users_by_id = {u["id"]: u for u in user_docs}

    search_lower = (search or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for talent_id_, project_id_, stage in filtered_rows:
        talent = talent_by_id.get(talent_id_)
        if not talent:
            continue  # talent record missing/deleted — never fabricate a row for it
        assignment_doc = assignment_by_pair.get((talent_id_, project_id_))
        assigned_to_id = (assignment_doc or {}).get("assigned_to_id")

        if assignment and assignment != "all":
            if assignment == "mine" and assigned_to_id != user.get("id"):
                continue
            if assignment == "unassigned" and assigned_to_id:
                continue
            if assignment == "assigned" and not assigned_to_id:
                continue

        if assigned_to_id_filter:
            if assigned_to_id_filter == "unassigned":
                if assigned_to_id:
                    continue
            elif assigned_to_id != assigned_to_id_filter:
                continue

        latest_call = latest_call_by_pair.get((talent_id_, project_id_))
        bucket = _call_status_bucket((latest_call or {}).get("called_at"))
        if call_status and call_status != bucket:
            continue

        project = project_map[project_id_]
        if search_lower:
            haystack = f"{talent.get('name') or ''} {project.get('brand_name') or ''} {talent.get('phone') or ''}".lower()
            if search_lower not in haystack:
                continue

        rows.append({
            "talent_id": talent_id_,
            "talent_name": talent.get("name") or "Unnamed",
            # Primary number only — enough for an instant tel: Call button.
            # Alternate number/email are intentionally NOT included here;
            # the profile drawer fetches the full talent record on demand
            # (GET /talents/{id}, same endpoint TalentPreviewDrawer already
            # uses) instead of bloating every list row with fields most
            # rows will never need.
            "talent_phone": talent.get("phone") or None,
            "project_id": project_id_,
            "project_name": project.get("brand_name") or "Untitled Project",
            "pipeline_stage": stage,
            "assigned_to_id": assigned_to_id,
            "assigned_to_name": _user_label(users_by_id.get(assigned_to_id)) if assigned_to_id else None,
            "last_call_at": (latest_call or {}).get("called_at"),
            "last_call_result": (latest_call or {}).get("call_result"),
            "last_update_status": (latest_call or {}).get("update_status"),
            "last_update_text": (latest_call or {}).get("update_text"),
            "last_call_by_name": _user_label(users_by_id.get((latest_call or {}).get("called_by"))) if latest_call else None,
            "call_status_bucket": bucket,
        })

    rows.sort(key=lambda r: (r["last_call_at"] or ""), reverse=False)  # never-called first, oldest-called next
    return {"rows": rows, "pipeline_stages": PIPELINE_STAGE_ORDER}


@router.get("/{talent_id}/{project_id}/history")
async def call_history(
    talent_id: str, project_id: str, user: Dict[str, Any] = Depends(current_team_or_admin),
):
    """No ownership check — history visibility now matches list_calls'
    own broadened visibility (2026-09-28): any authenticated team/admin
    user can view history for any (talent, project) pair, the same set
    list_calls already shows them. Keeping an independent "assigned to
    you" 403 here after broadening the list would have made the History
    button silently fail for rows a team member can now see but isn't
    assigned to — the same reachability gap this whole change closes."""
    docs = await db[CALLS_COLLECTION].find(
        {"talent_id": talent_id, "project_id": project_id}, {"_id": 0},
    ).sort("called_at", -1).to_list(500)

    caller_ids = sorted({d.get("called_by") for d in docs if d.get("called_by")})
    users_by_id: Dict[str, Dict[str, Any]] = {}
    if caller_ids:
        user_docs = await db.users.find(
            {"id": {"$in": caller_ids}}, {"_id": 0, "id": 1, "name": 1, "email": 1},
        ).to_list(len(caller_ids))
        users_by_id = {u["id"]: u for u in user_docs}

    for d in docs:
        d["called_by_name"] = _user_label(users_by_id.get(d.get("called_by")))
    return {"history": docs}


@router.post("")
async def create_call(payload: CallIn, user: Dict[str, Any] = Depends(current_team_or_admin)):
    """Idempotent on `payload.id` — the frontend generates this ONCE per
    Save tap (and disables the button immediately, per the brief's own
    "save-lock" requirement) and never regenerates it on retry. A repeat
    POST with the SAME id (double-click before the disabled state took
    effect, or a network retry) hits the unique index on `id` and gets
    the ALREADY-created record back instead of a second history row —
    mirroring the exact "claim via unique-index insert, DuplicateKeyError
    -> return the existing doc" pattern already used elsewhere in this
    codebase (e.g. agents/modules/casting_command_interpreter.py's
    _claim, inbound_messages.capture_inbound)."""
    if payload.call_result not in CALL_RESULTS:
        raise HTTPException(400, f"call_result must be one of {CALL_RESULTS}")
    if payload.update_status is not None and payload.update_status not in UPDATE_STATUSES:
        raise HTTPException(400, f"update_status must be one of {UPDATE_STATUSES}")

    # No ownership check here (2026-09-28): a team member may record a
    # call for a pair NOT assigned to them, matching list_calls' own
    # broadened visibility — see this task's brief ("Allow a Team Member
    # to record/add a call entry even when the call is NOT assigned to
    # that team member"). This is deliberately the ONLY permission this
    # endpoint relaxes: it still never writes to ASSIGNMENTS_COLLECTION
    # (assignment/reassignment stays exclusively behind POST /assign,
    # require_role("admin"), untouched below), so recording a call can
    # never reassign who a pair belongs to.
    # Pipeline membership must be real and belong to a currently-ongoing
    # project — never let a call be logged against a stale/closed
    # combination (brief section 3, applied to writes too, not just the
    # list view).
    project = await db.projects.find_one(
        active_only({"id": payload.project_id, "status": "ongoing"}), {"_id": 0, "id": 1, "brand_name": 1},
    )
    if not project:
        raise HTTPException(400, "Project is not ongoing (or does not exist)")
    pipeline_row = await db.casting_pipeline.find_one(
        active_only({"talent_id": payload.talent_id, "project_id": payload.project_id}), {"_id": 0, "id": 1},
    )
    if not pipeline_row:
        raise HTTPException(400, "Talent is not in this project's pipeline")

    doc = {
        "id": payload.id,
        "talent_id": payload.talent_id,
        "project_id": payload.project_id,
        "called_by": user.get("id"),
        # Server-side timestamp (_now(), the SAME ISO-8601-UTC convention
        # every other *_at field in this codebase uses) — never trusts the
        # browser's own clock, per the brief's explicit requirement.
        "called_at": _now(),
        "call_result": payload.call_result,
        "update_status": payload.update_status,
        "update_text": (payload.update_text or "").strip() or None,
        "created_at": _now(),
    }
    try:
        await db[CALLS_COLLECTION].insert_one(doc)
    except DuplicateKeyError:
        # A retry of an ALREADY-recorded call — the notification for the
        # real mutation already fired the first time; replaying it here
        # would be exactly the duplicate-from-retry PART 13 forbids.
        existing = await db[CALLS_COLLECTION].find_one({"id": payload.id}, {"_id": 0})
        if existing:
            return existing
        raise
    doc.pop("_id", None)

    # WhatsApp group notification (PART 4/7) — fires only on the genuine
    # fresh insert above, never on the idempotent-replay path.
    talent = await db.talents.find_one({"id": payload.talent_id}, {"_id": 0, "name": 1})
    talent_name = (talent or {}).get("name") or "Unnamed Talent"
    actor_name = user.get("name") or user.get("email") or "Someone"
    enqueue_workflow_whatsapp_notification(
        _build_call_recorded_message(
            talent_name, project.get("brand_name"), payload.call_result, payload.update_status, actor_name, doc["called_at"],
        ),
        f"{payload.talent_id}:{payload.project_id}",
    )

    return doc


@router.post("/assign")
async def assign_calls(payload: AssignIn, user: Dict[str, Any] = Depends(require_role("admin"))):
    """Admin-only (brief section 12/25) — assignment is always at the
    (talent_id, project_id) level, never a whole talent across every
    project they're in (brief's own explicit "Important" callout)."""
    if not payload.pairs:
        raise HTTPException(400, "pairs must not be empty")
    if payload.assigned_to_id:
        assignee = await db.users.find_one({"id": payload.assigned_to_id}, {"_id": 0, "id": 1, "status": 1})
        if not assignee:
            raise HTTPException(404, "assigned_to_id does not match a real user")

    # Fetch the BEFORE state for every pair in one query — this is what
    # lets a real reassignment (old assignee != new) be told apart from a
    # first-time assignment (was unassigned) or a genuine no-op (same
    # assignee re-saved), so PART 13's "one logical change = one
    # notification" holds even across a bulk multi-pair assign.
    or_clauses = [{"talent_id": p.talent_id, "project_id": p.project_id} for p in payload.pairs]
    existing_docs = await db[ASSIGNMENTS_COLLECTION].find(
        {"$or": or_clauses}, {"_id": 0, "talent_id": 1, "project_id": 1, "assigned_to_id": 1},
    ).to_list(len(payload.pairs))
    prev_assignee_by_pair = {(d["talent_id"], d["project_id"]): d.get("assigned_to_id") for d in existing_docs}

    now = _now()
    updated = 0
    changed_pairs = []  # (talent_id, project_id, prev_assignee_id)
    for pair in payload.pairs:
        result = await db[ASSIGNMENTS_COLLECTION].update_one(
            {"talent_id": pair.talent_id, "project_id": pair.project_id},
            {
                "$set": {
                    "assigned_to_id": payload.assigned_to_id, "assigned_by": user.get("id"), "updated_at": now,
                },
                "$setOnInsert": {
                    "id": str(uuid.uuid4()), "talent_id": pair.talent_id, "project_id": pair.project_id,
                    "assigned_at": now,
                },
            },
            upsert=True,
        )
        if result.modified_count or result.upserted_id:
            updated += 1
        prev_assignee = prev_assignee_by_pair.get((pair.talent_id, pair.project_id))
        if payload.assigned_to_id and payload.assigned_to_id != prev_assignee:
            changed_pairs.append((pair.talent_id, pair.project_id, prev_assignee))

    # WhatsApp group notifications (PART 5/6) — one per pair whose
    # assignee genuinely changed to a real user; unassignment (setting
    # assigned_to_id back to None) has no template in this pass and is
    # deliberately left silent rather than inventing one.
    if changed_pairs:
        talent_ids = {t for t, _, _ in changed_pairs}
        project_ids = {p for _, p, _ in changed_pairs}
        talents = await db.talents.find({"id": {"$in": list(talent_ids)}}, {"_id": 0, "id": 1, "name": 1}).to_list(len(talent_ids))
        talent_names = {t["id"]: t.get("name") or "Unnamed Talent" for t in talents}
        projects = await db.projects.find({"id": {"$in": list(project_ids)}}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(len(project_ids))
        project_names = {p["id"]: p.get("brand_name") for p in projects}
        name_map = await _resolve_user_names({user.get("id"), payload.assigned_to_id} | {pa for _, _, pa in changed_pairs})
        actor_name = name_map.get(user.get("id"), user.get("name") or user.get("email") or "Someone")
        new_assignee_name = name_map.get(payload.assigned_to_id, "Unassigned")

        for talent_id, project_id, prev_assignee in changed_pairs:
            talent_name = talent_names.get(talent_id, "Unnamed Talent")
            project_name = project_names.get(project_id)
            if prev_assignee:
                old_assignee_name = name_map.get(prev_assignee, "Unassigned")
                message = _build_call_reassigned_message(talent_name, project_name, old_assignee_name, new_assignee_name, actor_name, now)
            else:
                message = _build_call_assigned_message(talent_name, project_name, new_assignee_name, actor_name, now)
            enqueue_workflow_whatsapp_notification(message, f"{talent_id}:{project_id}")

    return {"updated": updated}
