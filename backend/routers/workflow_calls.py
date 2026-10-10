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

Per-project call state is ALWAYS derived live — latest call, pipeline stage (and the derived Follow-up
lane), call state (pending / attempted / completed), "new" — never cached state that could drift. The
rules live in workflow_calls_logic.py. Nothing in this file ever writes to db.casting_pipeline, and a
call's `update_status` ("Sending"/"Not Sending"/"Not Interested") is NEVER written back to a pipeline
stage: a call outcome and a project's casting decision are different things.

Shared call outcome (2026-10): a call is one phone conversation with one talent, so recording it ALSO
appends a clearly marked entry (`synced_from_call_id` / `synced_from_project_id`, id derived from the
source call + project so a retry is a no-op) to the same canonical talent's OTHER ongoing-project call
histories. Only call history is shared; stage, approval, availability and assignment stay per project.

Assignment docs hold the current owner, `assigned_at` (when THAT assignee took it), `priority`
(urgent / semi_urgent / normal, default normal) and an append-only `assignment_history`.

Talent identity is canonical: a talent absorbed into another by a merge keeps old ids in some
collections, so every read/write resolves ids through `_Identity` (canonical id + absorbed aliases).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from core import db, current_team_or_admin, require_role, _now, active_only
from routers.casting_pipeline import _normalise_stage, PIPELINE_STAGE_ORDER
from .workflow_calls_schemas import CallIn, AssignIn, PriorityIn, CALL_RESULTS, UPDATE_STATUSES
from . import workflow_calls_logic as L
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
        link=_call_link(), link_label="Open Calls:",
    )


def _build_call_reassigned_message(talent_name: str, project_name: Optional[str], old_assignee_name: str, new_assignee_name: str, actor_name: str, event_ts: str) -> str:
    return _format_workflow_message(
        "📞 *CALL REASSIGNED*", subject_label="Talent", subject_name=talent_name, project_name=project_name,
        blocks=[
            [f"Previously assigned to: {old_assignee_name}", f"Now assigned to: {new_assignee_name}", f"Updated by: {actor_name}"],
            [f"Updated: {_fmt_event_ts(event_ts)}"],
        ],
        link=_call_link(), link_label="Open Calls:",
    )


# WhatsApp's practical message-length ceiling is ~65,536 characters — a
# batch notification only ever gets split if the rendered text would
# genuinely exceed a safe margin under that, never on pair-count alone
# (brief's own explicit "do NOT split ordinary batches unnecessarily").
_BATCH_MESSAGE_MAX_CHARS = 60000


def _render_calls_assigned_batch(entries: list, assignee_name: str, actor_name: str, event_ts: str, part: Optional[tuple] = None) -> str:
    header = "📞 *CALLS ASSIGNED*"
    if part:
        header = f"📞 *CALLS ASSIGNED — {part[0]}/{part[1]}*"
    lines = [
        header,
        f"Assigned to: {assignee_name}",
        f"Assigned by: {actor_name}",
        f"Assigned: {_fmt_event_ts(event_ts)}",
        "",
    ]
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. {entry['talent_name']}")
        if entry.get("project_name"):
            lines.append(f"Project: {entry['project_name']}")
    lines.append("")
    lines.append("Open Calls:")
    lines.append(_call_link())
    return "\n".join(lines)


def _build_calls_assigned_batch_messages(entries: list, assignee_name: str, actor_name: str, event_ts: str) -> list:
    """ONE message per assignment action (the batch boundary is the whole
    assign_calls request, not any per-pair loop) — see this function's own
    call site. Only splits into numbered parts if the single rendered
    message would genuinely exceed WhatsApp's length ceiling; an ordinary
    batch (even a large one) stays one message."""
    single = _render_calls_assigned_batch(entries, assignee_name, actor_name, event_ts)
    if len(single) <= _BATCH_MESSAGE_MAX_CHARS or len(entries) <= 1:
        return [single]

    # Estimate a safe chunk size from the single-message average entry
    # cost, then split into that many equal-ish parts and re-render each
    # with its own part-N-of-M header.
    overhead = len(single) - sum(len(f"{i}. {e['talent_name']}\n" + (f"Project: {e['project_name']}\n" if e.get("project_name") else "")) for i, e in enumerate(entries, 1))
    avg_entry_len = max(1, (len(single) - overhead) // len(entries))
    entries_per_chunk = max(1, _BATCH_MESSAGE_MAX_CHARS // avg_entry_len)
    chunks = [entries[i:i + entries_per_chunk] for i in range(0, len(entries), entries_per_chunk)]
    total = len(chunks)
    return [_render_calls_assigned_batch(chunk, assignee_name, actor_name, event_ts, part=(idx, total)) for idx, chunk in enumerate(chunks, 1)]


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


class _Identity:
    """Canonical talent identity. A merged ("absorbed") talent keeps its old id in some collections (the
    merge executor repoints pipeline rows but not the call tables), so every read resolves an id to its
    surviving canonical talent and treats the canonical id plus every id absorbed into it as one person."""

    def __init__(self, parent: Dict[str, str]):
        self._canon: Dict[str, str] = {}
        for a in parent:
            t, hops = a, 0
            while t in parent and hops < 8:
                t, hops = parent[t], hops + 1
            self._canon[a] = t
        self._aliases: Dict[str, set] = {}
        for a, c in self._canon.items():
            self._aliases.setdefault(c, {c}).add(a)

    def canon(self, talent_id: str) -> str:
        return self._canon.get(talent_id, talent_id)

    def alias_ids(self, canon_id: str) -> set:
        return set(self._aliases.get(canon_id, {canon_id}))

    def all_ids(self, canon_ids: Iterable[str]) -> List[str]:
        out: set = set()
        for c in canon_ids:
            out |= self.alias_ids(c)
        return sorted(out)


async def _identity() -> _Identity:
    docs = await db.talents.find({"status": "MERGED", "merged_into": {"$ne": None}}, {"_id": 0, "id": 1, "merged_into": 1}).to_list(5000)
    return _Identity({d["id"]: d["merged_into"] for d in docs})


def _pick_later(a: Optional[Dict[str, Any]], b: Dict[str, Any], key: str) -> Dict[str, Any]:
    return b if a is None or (b.get(key) or "") > (a.get(key) or "") else a


async def _latest_calls_by_pair(talent_ids: List[str], project_ids: List[str], identity: _Identity) -> Dict[tuple, Dict[str, Any]]:
    """(canonical talent, project) -> {latest: its most recent call row, count: attempts}, in ONE aggregation
    over the (talent ids x project ids) the page needs — never the full history, never one query per pair."""
    if not talent_ids or not project_ids:
        return {}
    cursor = db[CALLS_COLLECTION].aggregate([
        {"$match": {"talent_id": {"$in": talent_ids}, "project_id": {"$in": project_ids}}},
        {"$sort": {"called_at": -1}},
        {"$group": {"_id": {"talent_id": "$talent_id", "project_id": "$project_id"}, "latest": {"$first": "$$ROOT"}, "count": {"$sum": 1}}},
    ])
    out: Dict[tuple, Dict[str, Any]] = {}
    async for doc in cursor:
        row = doc["latest"]
        key = (identity.canon(row["talent_id"]), row["project_id"])
        prev = out.get(key)
        merged = {"latest": _pick_later(prev["latest"] if prev else None, row, "called_at"), "count": doc["count"] + (prev["count"] if prev else 0)}
        out[key] = merged
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


async def _load_call_rows(identity: _Identity, talent_scope: Optional[List[str]] = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Every (canonical talent, ongoing project) call row, fully derived, in a fixed handful of queries
    regardless of how many rows there are (projects, pipeline, submissions, talents, assignments, latest
    calls, users). `talent_scope` (canonical ids) narrows the load to one person for the call-detail view."""
    now = now or datetime.now(timezone.utc)
    project_map = await _ongoing_project_map()
    if not project_map:
        return {"rows": [], "project_map": {}}
    query: Dict[str, Any] = {"project_id": {"$in": list(project_map.keys())}}
    if talent_scope:
        query["talent_id"] = {"$in": identity.all_ids(talent_scope)}
    pipeline_rows = await db.casting_pipeline.find(
        active_only(query), {"_id": 0, "project_id": 1, "talent_id": 1, "stage": 1, "created_at": 1},
    ).sort("created_at", 1).to_list(20000)
    if not pipeline_rows:
        return {"rows": [], "project_map": project_map}

    # one row per (canonical talent, project) — a merged duplicate must not show a person twice
    pairs: Dict[tuple, Dict[str, Any]] = {}
    for r in pipeline_rows:
        key = (identity.canon(r["talent_id"]), r["project_id"])
        if key not in pairs:
            pairs[key] = r
    canon_ids = sorted({t for t, _ in pairs})
    all_ids = identity.all_ids(canon_ids)
    project_ids = sorted({p for _, p in pairs})

    subs, talent_docs, assignment_docs, call_info = await asyncio.gather(
        db.submissions.find(active_only({"project_id": {"$in": project_ids}, "talent_id": {"$in": all_ids}}), {"_id": 0, "project_id": 1, "talent_id": 1}).to_list(None),
        db.talents.find(active_only({"id": {"$in": canon_ids}}, exclude_archived=True), {"_id": 0, "id": 1, "name": 1, "phone": 1}).to_list(len(canon_ids)),
        db[ASSIGNMENTS_COLLECTION].find({"talent_id": {"$in": all_ids}, "project_id": {"$in": project_ids}}, {"_id": 0, "assignment_history": 0}).to_list(None),
        _latest_calls_by_pair(all_ids, project_ids, identity),
    )
    submitted = {(p, identity.canon(t)) for s in subs for p, t in [(s.get("project_id"), s.get("talent_id"))] if p and t}
    talent_by_id = {t["id"]: t for t in talent_docs}
    assignment_by_pair: Dict[tuple, Dict[str, Any]] = {}
    for a in assignment_docs:
        key = (identity.canon(a["talent_id"]), a["project_id"])
        assignment_by_pair[key] = _pick_later(assignment_by_pair.get(key), a, "updated_at")

    user_ids = {a.get("assigned_to_id") for a in assignment_by_pair.values()} | {a.get("assigned_by") for a in assignment_by_pair.values()}
    user_ids |= {c["latest"].get("called_by") for c in call_info.values()}
    user_ids.discard(None)
    users_by_id: Dict[str, Dict[str, Any]] = {}
    if user_ids:
        users_by_id = {u["id"]: u for u in await db.users.find({"id": {"$in": sorted(user_ids)}}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(len(user_ids))}

    per_talent: Dict[str, int] = {}
    for t, _ in pairs:
        per_talent[t] = per_talent.get(t, 0) + 1

    rows: List[Dict[str, Any]] = []
    for (canon_id, project_id), pr in pairs.items():
        talent = talent_by_id.get(canon_id)
        if not talent:
            continue  # talent record missing/deleted — never fabricate a row for it
        raw_stage = _normalise_stage(pr.get("stage")) or pr.get("stage")
        stage = L.derive_stage(raw_stage, (project_id, canon_id) in submitted)
        asg = assignment_by_pair.get((canon_id, project_id)) or {}
        assigned_to_id = asg.get("assigned_to_id")
        assigned_at = asg.get("assigned_at") if assigned_to_id else None
        info = call_info.get((canon_id, project_id))
        latest = (info or {}).get("latest")
        state = L.call_state(latest, assigned_at)
        project = project_map[project_id]
        activity = max([x for x in ((latest or {}).get("called_at"), assigned_at) if x], default=None)
        src_pid = (latest or {}).get("synced_from_project_id")
        rows.append({
            "talent_id": canon_id,
            "talent_name": talent.get("name") or "Unnamed",
            "talent_phone": talent.get("phone") or None,
            "project_id": project_id,
            "project_name": project.get("brand_name") or "Untitled Project",
            "pipeline_stage": stage,
            "raw_stage": raw_stage,
            "is_follow_up": stage == "follow_up",
            "assigned_to_id": assigned_to_id,
            "assigned_to_name": _user_label(users_by_id.get(assigned_to_id)) if assigned_to_id else None,
            "assigned_by_name": _user_label(users_by_id.get(asg.get("assigned_by"))) if assigned_to_id else None,
            "assigned_at": assigned_at,
            "priority": L.clean_priority(asg.get("priority")),
            "call_state": state,
            "is_new": L.is_new_assignment(state, assigned_to_id, assigned_at, now),
            "call_count": (info or {}).get("count", 0),
            "last_call_at": (latest or {}).get("called_at"),
            "last_call_result": (latest or {}).get("call_result"),
            "last_update_status": (latest or {}).get("update_status"),
            "last_update_text": (latest or {}).get("update_text"),
            "last_call_by_name": _user_label(users_by_id.get((latest or {}).get("called_by"))) if latest else None,
            "last_call_synced": bool((latest or {}).get("synced_from_call_id")),
            "last_call_source_project": (project_map.get(src_pid) or {}).get("brand_name") if src_pid else None,
            "last_activity_at": activity,
            "call_status_bucket": _call_status_bucket((latest or {}).get("called_at")),
            "other_projects_count": per_talent.get(canon_id, 1) - 1,
        })
    return {"rows": rows, "project_map": project_map}


def _apply_filters(rows: List[Dict[str, Any]], *, viewer_id: Optional[str], project_ids: List[str], talent_id: Optional[str], assignment: Optional[str],
                   assigned_to_id: Optional[str], stages: set, call_status: Optional[str], call_state: Optional[str],
                   priority: Optional[str], search: Optional[str], identity: _Identity) -> List[Dict[str, Any]]:
    wanted_talent = identity.canon(talent_id) if talent_id else None
    needle = (search or "").strip().lower()
    out = []
    for r in rows:
        if project_ids and r["project_id"] not in project_ids:
            continue
        if wanted_talent and r["talent_id"] != wanted_talent:
            continue
        if stages and r["pipeline_stage"] not in stages and r["raw_stage"] not in stages:
            continue
        a = r["assigned_to_id"]
        if assignment == "mine" and a != viewer_id:
            continue
        if assignment == "unassigned" and a:
            continue
        if assignment == "assigned" and not a:
            continue
        if assigned_to_id:
            if assigned_to_id == "unassigned":
                if a:
                    continue
            elif a != assigned_to_id:
                continue
        if call_status and call_status != r["call_status_bucket"]:
            continue
        if call_state and call_state != "all" and call_state != r["call_state"]:
            continue
        if priority and priority != r["priority"]:
            continue
        if needle and needle not in f"{r['talent_name']} {r['project_name']} {r['talent_phone'] or ''}".lower():
            continue
        out.append(r)
    return out


@router.get("")
async def list_calls(
    project_ids: Optional[str] = None,  # comma-separated
    talent_id: Optional[str] = None,
    assignment: Optional[str] = None,  # all | mine | unassigned | assigned
    assigned_to_id_filter: Optional[str] = Query(None, alias="assigned_to_id"),  # exact team member id, or "unassigned"
    pipeline: Optional[str] = None,  # comma-separated stage keys (follow_up included)
    call_status: Optional[str] = None,  # never | today | recent | stale
    call_state: Optional[str] = None,  # pending | attempted | completed | all
    priority: Optional[str] = None,  # urgent | semi_urgent | normal
    search: Optional[str] = None,
    include_facets: bool = True,  # the filter dropdowns' option lists; the UI asks once, not on every filter change
    view: str = Query("list", pattern="^(list|grouped)$"),
    page: int = Query(0, ge=0),
    size: Optional[int] = Query(None, ge=1, le=200),  # list: rows per page · grouped: projects per page · omitted: everything
    user: Dict[str, Any] = Depends(current_team_or_admin),
):
    """One row per (canonical talent, project) in the pipeline of an ONGOING project — never a project
    that is not ongoing (a hard requirement).

    Everything is derived live, never cached (stage from casting_pipeline, Follow-up by the Casting
    Pipeline board's own rule, call state from the latest call versus the assignment). Rows come back in
    ONE queue order (workflow_calls_logic.queue_key) for the viewer: admins and team members see the same
    set of calls (assignment itself stays admin-only), ordered for their own role.

      view=list     flat rows, optionally paginated (page/size)
      view=grouped  project → pipeline → rows, paginated by PROJECT (page/size)

    `summary`, `facets` and `next_up` always describe the whole filtered set, not just the page."""
    identity = await _identity()
    loaded = await _load_call_rows(identity)
    all_rows = loaded["rows"]
    viewer_id, viewer_is_admin = user.get("id"), user.get("role") == "admin"
    pid_list = [p for p in (project_ids.split(",") if project_ids else []) if p]
    stages = {s for s in (pipeline.split(",") if pipeline else []) if s}

    facets = None
    if include_facets:
        facets = {
            "projects": sorted({(r["project_id"], r["project_name"]) for r in all_rows}, key=lambda x: x[1].lower()),
            "talents": sorted({(r["talent_id"], r["talent_name"]) for r in all_rows}, key=lambda x: x[1].lower()),
            "assignees": sorted({(r["assigned_to_id"], r["assigned_to_name"] or r["assigned_to_id"]) for r in all_rows if r["assigned_to_id"]}, key=lambda x: x[1].lower()),
        }
        facets = {k: [{"id": i, "label": l} for i, l in v] for k, v in facets.items()}

    rows = _apply_filters(
        all_rows, viewer_id=viewer_id, project_ids=pid_list, talent_id=talent_id, assignment=assignment,
        assigned_to_id=assigned_to_id_filter, stages=stages, call_status=call_status, call_state=call_state,
        priority=priority, search=search, identity=identity,
    )
    rows = L.sort_rows(rows, viewer_id, viewer_is_admin)
    out: Dict[str, Any] = {
        "pipeline_stages": L.STAGE_DISPLAY_ORDER, "summary": L.counts(rows),
        "next_up": [r for r in rows if r["call_state"] != "completed"][:5],
        "viewer": {"id": viewer_id, "is_admin": viewer_is_admin},
    }
    if facets is not None:
        out["facets"] = facets
    if view == "grouped":
        groups = L.group_by_project(rows)
        if size:
            total = len(groups)
            groups = groups[page * size:(page + 1) * size]
            out.update({"total_projects": total, "page": page, "size": size, "has_more": (page + 1) * size < total})
        out["projects"] = groups
    else:
        total = len(rows)
        if size:
            rows = rows[page * size:(page + 1) * size]
            out.update({"page": page, "size": size, "has_more": (page + 1) * size < total})
        out["total"] = total
        out["rows"] = rows
    return out


@router.get("/{talent_id}/{project_id}/context")
async def call_context(talent_id: str, project_id: str, user: Dict[str, Any] = Depends(current_team_or_admin)):
    """What the caller needs before dialling: this talent's call row and every OTHER ongoing project the
    same canonical person is in (stage, call state, owner, latest outcome) — so nobody makes a redundant
    call. One batched load for the one person; never a query per project."""
    identity = await _identity()
    canon_id = identity.canon(talent_id)
    loaded = await _load_call_rows(identity, [canon_id])
    rows = loaded["rows"]
    current = next((r for r in rows if r["project_id"] == project_id), None)
    if not current:
        raise HTTPException(404, "This talent is not in that ongoing project's pipeline")
    others = sorted((r for r in rows if r["project_id"] != project_id), key=lambda r: r["project_name"].lower())
    return {"talent_id": canon_id, "current": current, "other_projects": others}


@router.get("/{talent_id}/{project_id}/history")
async def call_history(
    talent_id: str, project_id: str, user: Dict[str, Any] = Depends(current_team_or_admin),
):
    """No ownership check — history visibility matches list_calls' own visibility: any authenticated
    team/admin user can view history for any (talent, project) pair they can see in the list. Includes
    entries recorded under an id later absorbed into this talent by a merge, and flags entries that were
    synced here from another project's call (`synced_from_project_name`) so the audit trail is explicit."""
    identity = await _identity()
    ids = sorted(identity.alias_ids(identity.canon(talent_id)))
    docs = await db[CALLS_COLLECTION].find(
        {"talent_id": {"$in": ids}, "project_id": project_id}, {"_id": 0},
    ).sort("called_at", -1).to_list(500)

    caller_ids = sorted({d.get("called_by") for d in docs if d.get("called_by")})
    users_by_id: Dict[str, Dict[str, Any]] = {}
    if caller_ids:
        user_docs = await db.users.find(
            {"id": {"$in": caller_ids}}, {"_id": 0, "id": 1, "name": 1, "email": 1},
        ).to_list(len(caller_ids))
        users_by_id = {u["id"]: u for u in user_docs}
    src_ids = sorted({d["synced_from_project_id"] for d in docs if d.get("synced_from_project_id")})
    src_names: Dict[str, str] = {}
    if src_ids:
        src_names = {p["id"]: p.get("brand_name") for p in await db.projects.find({"id": {"$in": src_ids}}, {"_id": 0, "id": 1, "brand_name": 1}).to_list(len(src_ids))}

    for d in docs:
        d["called_by_name"] = _user_label(users_by_id.get(d.get("called_by")))
        d["synced_from_project_name"] = src_names.get(d.get("synced_from_project_id"))
    return {"history": docs}


async def _sync_call_to_other_projects(call: Dict[str, Any], identity: _Identity) -> List[Dict[str, Any]]:
    """Record the same call outcome against the talent's OTHER ongoing-project calls.

    Only the call-history side is touched: each target gets one APPEND-ONLY entry marked
    `synced_from_call_id` / `synced_from_project_id`. Pipeline stages, approval/rejection, availability and
    assignments are never read-modified-written here — they are project decisions. Idempotent: the entry's
    id is derived from the source call id and the target project, so the unique index on `id` makes a retry
    (or a replay after a partial failure) a no-op that still returns the full list of affected projects."""
    canon_id = identity.canon(call["talent_id"])
    project_map = await _ongoing_project_map()
    target_ids = [p for p in project_map if p != call["project_id"]]
    if not target_ids:
        return []
    rows = await db.casting_pipeline.find(
        active_only({"talent_id": {"$in": sorted(identity.alias_ids(canon_id))}, "project_id": {"$in": target_ids}}),
        {"_id": 0, "project_id": 1},
    ).to_list(500)
    affected = []
    for pid in sorted({r["project_id"] for r in rows}):
        entry = {
            "id": f"{call['id']}:{pid}", "talent_id": canon_id, "project_id": pid,
            "called_by": call.get("called_by"), "called_at": call["called_at"],
            "call_result": call["call_result"], "update_status": call.get("update_status"), "update_text": call.get("update_text"),
            "created_at": _now(),
            "synced_from_call_id": call["id"], "synced_from_project_id": call["project_id"],
        }
        try:
            await db[CALLS_COLLECTION].insert_one(entry)
        except DuplicateKeyError:
            pass  # already synced by an earlier attempt
        affected.append({"project_id": pid, "project_name": project_map[pid].get("brand_name") or "Untitled Project"})
    return affected


@router.post("")
async def create_call(payload: CallIn, user: Dict[str, Any] = Depends(current_team_or_admin)):
    """Idempotent on `payload.id` — the frontend generates this ONCE per Save tap (and disables the button
    immediately) and never regenerates it on retry. A repeat POST with the SAME id hits the unique index on
    `id` and gets the ALREADY-created record back instead of a second history row (the same
    "claim via unique-index insert, DuplicateKeyError -> return the existing doc" pattern used elsewhere,
    e.g. agents/modules/casting_command_interpreter.py's _claim).

    The call is also recorded against the talent's other ongoing-project calls (see
    _sync_call_to_other_projects) unless `sync_other_projects` is false; the response lists the projects
    that were updated. No ownership check: a team member may record a call that is not assigned to them —
    and it never writes to ASSIGNMENTS_COLLECTION, so recording can never reassign anything. A call can
    only be logged against a talent who is really in an ONGOING project's pipeline."""
    if payload.call_result not in CALL_RESULTS:
        raise HTTPException(400, f"call_result must be one of {CALL_RESULTS}")
    if payload.update_status is not None and payload.update_status not in UPDATE_STATUSES:
        raise HTTPException(400, f"update_status must be one of {UPDATE_STATUSES}")

    identity = await _identity()
    canon_id = identity.canon(payload.talent_id)
    project = await db.projects.find_one(
        active_only({"id": payload.project_id, "status": "ongoing"}), {"_id": 0, "id": 1, "brand_name": 1},
    )
    if not project:
        raise HTTPException(400, "Project is not ongoing (or does not exist)")
    pipeline_row = await db.casting_pipeline.find_one(
        active_only({"talent_id": {"$in": sorted(identity.alias_ids(canon_id))}, "project_id": payload.project_id}), {"_id": 0, "id": 1},
    )
    if not pipeline_row:
        raise HTTPException(400, "Talent is not in this project's pipeline")

    doc = {
        "id": payload.id,
        "talent_id": canon_id,
        "project_id": payload.project_id,
        "called_by": user.get("id"),
        # Server-side timestamp (_now(), ISO-8601 UTC like every other *_at field) — never the browser's clock.
        "called_at": _now(),
        "call_result": payload.call_result,
        "update_status": payload.update_status,
        "update_text": (payload.update_text or "").strip() or None,
        "sync_other_projects": bool(payload.sync_other_projects),
        "created_at": _now(),
    }
    fresh = True
    try:
        await db[CALLS_COLLECTION].insert_one(doc)
    except DuplicateKeyError:
        existing = await db[CALLS_COLLECTION].find_one({"id": payload.id}, {"_id": 0})
        if not existing:
            raise
        doc, fresh = existing, False
    doc.pop("_id", None)

    # A replay finishes any sync a failed first attempt left half-done (calls recorded before this
    # feature carry no flag and are never retro-synced).
    synced: List[Dict[str, Any]] = []
    if doc.get("sync_other_projects", False):
        synced = await _sync_call_to_other_projects(doc, identity)

    if fresh:
        # WhatsApp group notification — only on the genuine fresh insert, never on an idempotent replay.
        talent = await db.talents.find_one({"id": canon_id}, {"_id": 0, "name": 1})
        talent_name = (talent or {}).get("name") or "Unnamed Talent"
        actor_name = user.get("name") or user.get("email") or "Someone"
        enqueue_workflow_whatsapp_notification(
            _build_call_recorded_message(
                talent_name, project.get("brand_name"), payload.call_result, payload.update_status, actor_name, doc["called_at"],
            ),
            f"{canon_id}:{payload.project_id}",
        )
    return {**doc, "synced_projects": synced}


def _history_entry(kind: str, actor_id: Optional[str], now: str, **fields: Any) -> Dict[str, Any]:
    return {"type": kind, "at": now, "by": actor_id, **fields}


@router.post("/assign")
async def assign_calls(payload: AssignIn, user: Dict[str, Any] = Depends(require_role("admin"))):
    """Admin-only — assignment is always at the (talent_id, project_id) level, never a whole talent across
    every project they're in.

    The assignment doc is the current state: assignee, `assigned_at` (when THIS assignee took it — reset on
    a real reassignment, left alone on a same-assignee re-save), `priority` (default normal for a new
    assignment), and an append-only `assignment_history` so a reassignment never destroys what came before.
    A legacy doc being reassigned first records its previous known assignment, with its real original
    timestamp — nothing is back-filled or invented."""
    if not payload.pairs:
        raise HTTPException(400, "pairs must not be empty")
    if payload.priority is not None and payload.priority not in L.PRIORITIES:
        raise HTTPException(400, f"priority must be one of {L.PRIORITIES}")
    if payload.assigned_to_id:
        assignee = await db.users.find_one({"id": payload.assigned_to_id}, {"_id": 0, "id": 1, "status": 1})
        if not assignee:
            raise HTTPException(404, "assigned_to_id does not match a real user")

    identity = await _identity()
    canon_pairs = [(identity.canon(p.talent_id), p.project_id) for p in payload.pairs]
    existing_docs = await db[ASSIGNMENTS_COLLECTION].find(
        {"talent_id": {"$in": identity.all_ids(t for t, _ in canon_pairs)}, "project_id": {"$in": sorted({p for _, p in canon_pairs})}}, {"_id": 0},
    ).to_list(None)
    existing_by_pair: Dict[tuple, Dict[str, Any]] = {}
    for d in existing_docs:
        key = (identity.canon(d["talent_id"]), d["project_id"])
        existing_by_pair[key] = _pick_later(existing_by_pair.get(key), d, "updated_at")

    now = _now()
    updated = 0
    changed_pairs = []  # (talent_id, project_id, prev_assignee_id)
    for canon_id, project_id in canon_pairs:
        prev = existing_by_pair.get((canon_id, project_id))
        prev_assignee = (prev or {}).get("assigned_to_id")
        new_assignee = payload.assigned_to_id
        priority = payload.priority or (prev or {}).get("priority") or L.DEFAULT_PRIORITY
        assignee_changed = new_assignee != prev_assignee
        priority_changed = bool(prev) and priority != L.clean_priority((prev or {}).get("priority")) and not assignee_changed

        if not prev:
            doc = {
                "id": str(uuid.uuid4()), "talent_id": canon_id, "project_id": project_id,
                "assigned_to_id": new_assignee, "assigned_by": user.get("id"),
                "assigned_at": now if new_assignee else None, "priority": priority, "updated_at": now,
                "assignment_history": [_history_entry("assigned" if new_assignee else "unassigned", user.get("id"), now, assigned_to_id=new_assignee, priority=priority)],
            }
            try:
                await db[ASSIGNMENTS_COLLECTION].insert_one(doc)
                updated += 1
                if new_assignee:
                    changed_pairs.append((canon_id, project_id, None))
                continue
            except DuplicateKeyError:
                prev = await db[ASSIGNMENTS_COLLECTION].find_one({"talent_id": canon_id, "project_id": project_id}, {"_id": 0}) or {}
                prev_assignee = prev.get("assigned_to_id")
                assignee_changed = new_assignee != prev_assignee

        if assignee_changed:
            history: List[Dict[str, Any]] = []
            if not prev.get("assignment_history") and prev_assignee:
                # legacy doc: keep what we genuinely know about the previous assignment, with its real timestamp
                history.append(_history_entry("assigned", prev.get("assigned_by"), prev.get("assigned_at"), assigned_to_id=prev_assignee, legacy=True, ended_at=now))
            history.append(_history_entry("assigned" if new_assignee else "unassigned", user.get("id"), now, assigned_to_id=new_assignee, previous_assigned_to_id=prev_assignee, priority=priority))
            res = await db[ASSIGNMENTS_COLLECTION].update_one(
                {"id": prev["id"], "assigned_to_id": prev_assignee},   # optimistic: a concurrent change loses cleanly
                {"$set": {"assigned_to_id": new_assignee, "assigned_by": user.get("id"), "assigned_at": now if new_assignee else None,
                          "priority": priority, "updated_at": now},
                 "$push": {"assignment_history": {"$each": history}}},
            )
            if res.modified_count:
                updated += 1
                if new_assignee:
                    changed_pairs.append((canon_id, project_id, prev_assignee))
        elif priority_changed:
            res = await db[ASSIGNMENTS_COLLECTION].update_one(
                {"id": prev["id"]},
                {"$set": {"priority": priority, "updated_at": now},
                 "$push": {"assignment_history": _history_entry("priority_changed", user.get("id"), now, priority_from=L.clean_priority(prev.get("priority")), priority_to=priority)}},
            )
            updated += 1 if res.modified_count else 0

    # WhatsApp group notifications — one per assignment ACTION, only for pairs whose assignee genuinely
    # changed to a real user; unassignment has no template and stays silent.
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
        if len(changed_pairs) == 1:
            talent_id, project_id, prev_assignee = changed_pairs[0]
            talent_name = talent_names.get(talent_id, "Unnamed Talent")
            project_name = project_names.get(project_id)
            if prev_assignee:
                old_assignee_name = name_map.get(prev_assignee, "Unassigned")
                message = _build_call_reassigned_message(talent_name, project_name, old_assignee_name, new_assignee_name, actor_name, now)
            else:
                message = _build_call_assigned_message(talent_name, project_name, new_assignee_name, actor_name, now)
            enqueue_workflow_whatsapp_notification(message, f"{talent_id}:{project_id}")
        else:
            entries = [
                {"talent_name": talent_names.get(t, "Unnamed Talent"), "project_name": project_names.get(p)}
                for t, p, _ in changed_pairs
            ]
            batch_source_id = f"assign-batch:{uuid.uuid4()}"
            for message in _build_calls_assigned_batch_messages(entries, new_assignee_name, actor_name, now):
                enqueue_workflow_whatsapp_notification(message, batch_source_id)

    return {"updated": updated}


@router.patch("/priority")
async def set_priority(payload: PriorityIn, user: Dict[str, Any] = Depends(current_team_or_admin)):
    """Change priority (urgent / semi_urgent / normal). Admins may set it on any call; a team member only on
    calls assigned to THEMSELVES (they cannot reach assignment itself). Persisted on the assignment doc, so
    it survives refreshes and shows identically in the project view, pipeline view and call details."""
    if payload.priority not in L.PRIORITIES:
        raise HTTPException(400, f"priority must be one of {L.PRIORITIES}")
    if not payload.pairs:
        raise HTTPException(400, "pairs must not be empty")
    is_admin = user.get("role") == "admin"
    identity = await _identity()
    canon_pairs = [(identity.canon(p.talent_id), p.project_id) for p in payload.pairs]
    docs = await db[ASSIGNMENTS_COLLECTION].find(
        {"talent_id": {"$in": identity.all_ids(t for t, _ in canon_pairs)}, "project_id": {"$in": sorted({p for _, p in canon_pairs})}}, {"_id": 0},
    ).to_list(None)
    by_pair: Dict[tuple, Dict[str, Any]] = {}
    for d in docs:
        key = (identity.canon(d["talent_id"]), d["project_id"])
        by_pair[key] = _pick_later(by_pair.get(key), d, "updated_at")

    now = _now()
    updated, skipped = 0, []
    for canon_id, project_id in canon_pairs:
        doc = by_pair.get((canon_id, project_id))
        if not is_admin and (not doc or doc.get("assigned_to_id") != user.get("id")):
            skipped.append({"talent_id": canon_id, "project_id": project_id, "reason": "only the assignee can change this priority"})
            continue
        if doc is None:
            new_doc = {
                "id": str(uuid.uuid4()), "talent_id": canon_id, "project_id": project_id, "assigned_to_id": None, "assigned_by": None,
                "assigned_at": None, "priority": payload.priority, "updated_at": now,
                "assignment_history": [_history_entry("priority_changed", user.get("id"), now, priority_from=L.DEFAULT_PRIORITY, priority_to=payload.priority)],
            }
            try:
                await db[ASSIGNMENTS_COLLECTION].insert_one(new_doc)
                updated += 1
                continue
            except DuplicateKeyError:
                doc = await db[ASSIGNMENTS_COLLECTION].find_one({"talent_id": canon_id, "project_id": project_id}, {"_id": 0})
        if L.clean_priority(doc.get("priority")) == payload.priority:
            continue
        res = await db[ASSIGNMENTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {"priority": payload.priority, "updated_at": now},
             "$push": {"assignment_history": _history_entry("priority_changed", user.get("id"), now, priority_from=L.clean_priority(doc.get("priority")), priority_to=payload.priority)}},
        )
        updated += 1 if res.modified_count else 0
    return {"updated": updated, "skipped": skipped}
