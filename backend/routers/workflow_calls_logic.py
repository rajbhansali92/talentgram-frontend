"""Pure (no I/O) rules for Workflow → Calls: follow-up derivation, call state, priorities, queue
ordering and project/pipeline grouping. Kept separate from the router so every rule is unit-testable
and there is exactly one definition of each.

Vocabulary
  pipeline stage     the talent's CASTING stage in ONE project (db.casting_pipeline). Never written here.
  call outcome       what happened on a phone call: `call_result` (answered / no_answer / busy /
                     switched_off / call_back) + optional `update_status` (sending / not_sending /
                     not_interested / other) + a note. Stored per call attempt, append-only.
  call state         derived, per (talent, project): pending | attempted | completed (see call_state()).
  priority           urgent | semi_urgent | normal — a property of the ASSIGNMENT, default normal.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

PRIORITIES = ("urgent", "semi_urgent", "normal")
DEFAULT_PRIORITY = "normal"
PRIORITY_RANK = {"urgent": 0, "semi_urgent": 1, "normal": 2}

# A call assigned within this window and not yet attempted is "newly assigned".
NEW_ASSIGNMENT_HOURS = 24

# Outcomes that mean the call did not reach the talent — the call still has to be made.
UNREACHED_RESULTS = ("no_answer", "busy", "switched_off", "call_back")

# Display order of pipeline lanes inside a project: the working lane first.
STAGE_DISPLAY_ORDER = [
    "follow_up", "ask_to_test", "already_tested", "approved", "shortlisted", "hold",
    "locked", "pitch", "not_available", "not_interested", "rejected",
]


def stage_sort_index(stage: Optional[str]) -> int:
    try:
        return STAGE_DISPLAY_ORDER.index(stage)
    except ValueError:
        return len(STAGE_DISPLAY_ORDER)


def derive_stage(raw_stage: Optional[str], has_submission: bool) -> Optional[str]:
    """The stage a talent shows in, with Follow-up resolved exactly as the Casting Pipeline board does
    (routers.casting_pipeline.list_pipeline): Follow-up is a derived lane — a stored `follow_up`
    ("Reached Out") OR an `ask_to_test` talent who has not submitted for this project. Every other stage
    is returned unchanged; nothing is invented and nothing is written back."""
    if raw_stage == "ask_to_test" and not has_submission:
        return "follow_up"
    return raw_stage


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def call_state(latest_call: Optional[Dict[str, Any]], assigned_at: Optional[str]) -> str:
    """pending   — nobody has attempted this call since it was assigned (or ever, if never assigned).
       attempted — attempted since assignment but the talent was not reached (no answer / busy / switched
                   off / call back): still has to be made.
       completed — the talent was reached (answered) since assignment.
    A call recorded BEFORE the current assignment does not complete the assignment."""
    if not latest_call:
        return "pending"
    called = parse_ts(latest_call.get("called_at"))
    assigned = parse_ts(assigned_at)
    if assigned and called and called < assigned:
        return "pending"
    return "completed" if latest_call.get("call_result") == "answered" else "attempted"


def is_new_assignment(state: str, assigned_to_id: Optional[str], assigned_at: Optional[str], now: datetime) -> bool:
    if state != "pending" or not assigned_to_id:
        return False
    at = parse_ts(assigned_at)
    return bool(at and now - at <= timedelta(hours=NEW_ASSIGNMENT_HOURS))


def clean_priority(value: Optional[str]) -> str:
    return value if value in PRIORITIES else DEFAULT_PRIORITY


def _ts_desc(value: Optional[str]) -> float:
    dt = parse_ts(value)
    return -dt.timestamp() if dt else 0.0          # newest first; missing sorts after any real time


def queue_key(row: Dict[str, Any], viewer_id: Optional[str], viewer_is_admin: bool) -> tuple:
    """ONE ordering, deterministic. Ascending key = earlier in the queue.

    Admin:  0 newly assigned & still pending · 1 urgent · 2 semi-urgent · 3 normal (assigned, still to
            call) · 4 unassigned, still to call · 5 completed (latest activity first)
    Team:   0 yours, newly assigned & pending · 1-3 yours by priority · 4 others' urgent · 5 other
            calls still to make · 6 completed
    Within a tier: priority, then newest assignment, then latest activity, then names (stable)."""
    state = row["call_state"]
    prio = PRIORITY_RANK[clean_priority(row.get("priority"))]
    assigned_at, activity = row.get("assigned_at"), row.get("last_activity_at")
    names = ((row.get("talent_name") or "").lower(), (row.get("project_name") or "").lower())
    mine = bool(viewer_id) and row.get("assigned_to_id") == viewer_id
    assigned = bool(row.get("assigned_to_id"))

    if state == "completed":
        tier = 5 if viewer_is_admin else 6
        return (tier, _ts_desc(activity), *names)
    if viewer_is_admin:
        if row.get("is_new"):
            tier = 0
        elif assigned:
            tier = 1 + prio
        else:
            tier = 4
    else:
        if mine and row.get("is_new"):
            tier = 0
        elif mine:
            tier = 1 + prio
        elif assigned and prio == 0:
            tier = 4
        else:
            tier = 5
    return (tier, prio, _ts_desc(assigned_at), _ts_desc(activity), *names)


def sort_rows(rows: List[Dict[str, Any]], viewer_id: Optional[str], viewer_is_admin: bool) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: queue_key(r, viewer_id, viewer_is_admin))


def counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """The numbers shown on a project / pipeline header and in the summary strip."""
    c = {"total": 0, "pending": 0, "attempted": 0, "completed": 0, "assigned": 0, "unassigned": 0, "urgent": 0, "new": 0}
    for r in rows:
        c["total"] += 1
        c[r["call_state"]] += 1
        c["assigned" if r.get("assigned_to_id") else "unassigned"] += 1
        if r["call_state"] != "completed" and clean_priority(r.get("priority")) == "urgent" and r.get("assigned_to_id"):
            c["urgent"] += 1
        if r.get("is_new"):
            c["new"] += 1
    return c


def group_by_project(sorted_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project → pipeline → rows. `sorted_rows` must already be in queue order: a project is placed by its
    best-ranked call (so the project holding the next call to make is first), a lane by its best call
    within the project (Follow-up as the tie-break order), and rows keep their queue order."""
    projects: Dict[str, Dict[str, Any]] = {}
    for idx, r in enumerate(sorted_rows):
        p = projects.setdefault(r["project_id"], {
            "project_id": r["project_id"], "project_name": r["project_name"], "first_index": idx, "lanes": {},
        })
        lane = p["lanes"].setdefault(r["pipeline_stage"], {"stage": r["pipeline_stage"], "first_index": idx, "rows": []})
        lane["rows"].append(r)
    out = []
    for p in sorted(projects.values(), key=lambda x: (x["first_index"], x["project_name"].lower())):
        lanes = sorted(p["lanes"].values(), key=lambda l: (stage_sort_index(l["stage"]), l["first_index"]))
        all_rows = [r for l in lanes for r in l["rows"]]
        nxt = next((r for r in all_rows if r["call_state"] != "completed"), None)
        out.append({
            "project_id": p["project_id"], "project_name": p["project_name"],
            "counts": counts(all_rows),
            "next_call": ({"talent_id": nxt["talent_id"], "talent_name": nxt["talent_name"], "pipeline_stage": nxt["pipeline_stage"]} if nxt else None),
            "pipelines": [{"stage": l["stage"], "counts": counts(l["rows"]), "rows": l["rows"]} for l in lanes],
        })
    return out
