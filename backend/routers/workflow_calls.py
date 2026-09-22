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

from fastapi import APIRouter, Depends, HTTPException
from pymongo.errors import DuplicateKeyError

from core import db, current_team_or_admin, require_role, _now, active_only
from routers.casting_pipeline import _normalise_stage, PIPELINE_STAGE_ORDER
from .workflow_calls_schemas import CallIn, AssignIn, CALL_RESULTS, UPDATE_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow/calls", tags=["workflow-calls"])

CALLS_COLLECTION = "talent_project_calls"
ASSIGNMENTS_COLLECTION = "talent_project_call_assignments"


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
    assignment: Optional[str] = None,  # all | mine | unassigned | assigned (admin only — see below)
    pipeline: Optional[str] = None,  # comma-separated stage keys
    call_status: Optional[str] = None,  # never | today | recent | stale
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(current_team_or_admin),
):
    """One row per (talent, project) currently in the pipeline of an
    ONGOING project — never a talent with no ongoing-project pipeline
    membership, never a completed/hold/locked project's rows (brief
    section 3/15, a hard requirement).

    Authorization (never trust the frontend — same principle
    core.require_role's own docstring states): a "team" role user's
    results are ALWAYS restricted server-side to rows assigned to them,
    regardless of what `assignment` was requested — assignment is an
    admin-only action (see POST /assign), so a team member only ever
    works their own assigned queue (brief section 13/25). An "admin" may
    use `assignment` freely.
    """
    is_admin = user.get("role") == "admin"

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

        if not is_admin:
            # Hard server-side scope — see this endpoint's own docstring.
            if assigned_to_id != user.get("id"):
                continue
        elif assignment and assignment != "all":
            if assignment == "mine" and assigned_to_id != user.get("id"):
                continue
            if assignment == "unassigned" and assigned_to_id:
                continue
            if assignment == "assigned" and not assigned_to_id:
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
    if user.get("role") != "admin":
        assignment_doc = await db[ASSIGNMENTS_COLLECTION].find_one(
            {"talent_id": talent_id, "project_id": project_id}, {"_id": 0, "assigned_to_id": 1},
        )
        if not assignment_doc or assignment_doc.get("assigned_to_id") != user.get("id"):
            raise HTTPException(403, "Not assigned to you")

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

    if user.get("role") != "admin":
        assignment_doc = await db[ASSIGNMENTS_COLLECTION].find_one(
            {"talent_id": payload.talent_id, "project_id": payload.project_id}, {"_id": 0, "assigned_to_id": 1},
        )
        if not assignment_doc or assignment_doc.get("assigned_to_id") != user.get("id"):
            raise HTTPException(403, "Not assigned to you")

    # Pipeline membership must be real and belong to a currently-ongoing
    # project — never let a call be logged against a stale/closed
    # combination (brief section 3, applied to writes too, not just the
    # list view).
    project = await db.projects.find_one(
        active_only({"id": payload.project_id, "status": "ongoing"}), {"_id": 0, "id": 1},
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
        existing = await db[CALLS_COLLECTION].find_one({"id": payload.id}, {"_id": 0})
        if existing:
            return existing
        raise
    doc.pop("_id", None)
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

    now = _now()
    updated = 0
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
    return {"updated": updated}
