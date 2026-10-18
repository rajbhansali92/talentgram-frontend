"""Personal Assistant — turns parsed segments into persisted TASKS (structured action objects).

A task never contains free text to execute — only resolved entity ids that the executors validate again
against the database. Anything unresolved becomes a "slot" with tappable options (server-generated); a
clarification can only pick from those options or re-run the same resolver on corrected text, so the browser
can never inject an arbitrary id. Consequential kinds wait in `awaiting_confirmation`; read-only kinds
(lookup, list) run immediately once their entities are resolved.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Optional

from core import db
from personal_assistant import resolve as R
from personal_assistant import store as S
from personal_assistant.parser import Segment, parse_message
from routers.casting_pipeline import PIPELINE_STAGE_ORDER, _normalise_stage

CONSEQUENTIAL = {"add", "send", "move", "share"}
READ_ONLY = {"lookup", "list"}
MAX_SELECTED = 200
MAX_SEGMENTS = 10          # actions per message
MAX_NAMES = 25             # typed names per action
STAGE_OPTIONS = [{"id": s, "label": s.replace("_", " ").title(), "sublabel": "", "thumb": None} for s in PIPELINE_STAGE_ORDER]


def stage_label(key: Optional[str]) -> str:
    return (key or "").replace("_", " ").title()


def _names(entries: List[dict], limit: int = 3) -> str:
    labels = [e.get("label") or e.get("phrase") or "?" for e in entries]
    if not labels:
        return "no one yet"
    return ", ".join(labels[:limit]) + (f" +{len(labels) - limit} more" if len(labels) > limit else "")


def _entry(phrase: str, res: dict) -> dict:
    e = {"phrase": phrase, "state": res["state"], "id": res.get("id"), "label": res.get("label")}
    if res.get("options"):
        e["options"] = res["options"]
    return e


# --------------------------------------------------------------------------------------------- slot logic
def compute_slots(task: dict) -> List[dict]:
    p = task["params"]
    slots: List[dict] = []
    for i, e in enumerate(p.get("talents") or []):
        if e["state"] != "resolved":
            slots.append({"key": f"talent:{i}", "kind": "talent", "phrase": e["phrase"], "options": e.get("options") or [],
                          "prompt": (f'Which "{e["phrase"]}" did you mean?' if e["state"] == "ambiguous"
                                     else (e.get("prompt") or f'I couldn\'t find a talent matching "{e["phrase"]}". Type the name again.'))})
    for i, e in enumerate(p.get("projects") or []):
        if e["state"] != "resolved":
            slots.append({"key": f"project:{i}", "kind": "project", "phrase": e["phrase"], "options": e.get("options") or [],
                          "prompt": (f'Which project did you mean by "{e["phrase"]}"?' if e["state"] == "ambiguous"
                                     else (e.get("prompt") or f'I couldn\'t find a project matching "{e["phrase"]}".'))})
    if task["kind"] == "send" and p.get("template_state") != "resolved":
        slots.append({"key": "template", "kind": "template", "phrase": p.get("template_phrase") or "",
                      "options": p.get("template_options") or [],
                      "prompt": "Which template should I send?" if not p.get("template_phrase") else f'Which template did you mean by "{p["template_phrase"]}"?'})
    if task["kind"] == "move" and not p.get("stage"):
        slots.append({"key": "stage", "kind": "stage", "phrase": p.get("stage_text") or "", "options": STAGE_OPTIONS,
                      "prompt": "Which pipeline stage?"})
    return slots


def _all_resolved(entries: List[dict]) -> bool:
    return bool(entries) and all(e["state"] == "resolved" for e in entries)


def _title(task: dict) -> str:
    p, k = task["params"], task["kind"]
    ts, ps = _names(p.get("talents") or []), _names(p.get("projects") or [])
    if k == "add":
        return f"Add {ts} to {ps}"
    if k == "send":
        return f"Send {p.get('template_name') or p.get('template_phrase') or 'a template'} to {ts}" + (f" for {ps}" if p.get("projects") else "")
    if k == "move":
        return f"Move {ts} to {stage_label(p.get('stage')) or '…'}" + (f" in {ps}" if p.get("projects") else "")
    if k == "share":
        rec = p.get("recipient") or {}
        to = {"number": "a WhatsApp number", "project_group": f"the {ps} group" if p.get("projects") else "the project group",
              "casting_director": "the casting director"}.get(rec.get("kind"), "…")
        return f"Share {('media link' if p.get('lookup') == 'media_link' else 'profile')} of {ts} with {to}"
    if k == "list":
        return f"Talents in {stage_label(p.get('stage'))}" + (f" · {ps}" if p.get("projects") else "")
    return {"profile": f"Profile · {ts}", "media_link": f"Media link · {ts}", "media": f"Photos & video · {ts}", "history": f"History · {ts}",
            "projects": f"Projects of {ts}", "submissions": f"Submissions of {ts}", "tested": f"Has {ts} tested?", "find": f"Find {ts}"}.get(p.get("lookup"), f"Look up {ts}")


# ------------------------------------------------------------------------------------------ finalisation
async def finalize(task: dict, siblings: Optional[List[dict]] = None) -> dict:
    """Recompute slots / warnings / status of a task from its params. Idempotent; called after planning and after
    every clarification. Never executes anything."""
    p = task["params"]
    task["slots"] = compute_slots(task)
    warnings: List[str] = []
    task["title"] = _title(task)
    if task["status"] in S.TERMINAL or task["status"] in S.ACTIVE:
        return task
    if task["slots"]:
        task["status"] = S.NEEDS_CLARIFICATION
        return task

    projects = p.get("projects") or []
    pids = [e["id"] for e in projects if e.get("id")]
    # selected-talents-in-stage filter ("the selected talents in Ask to Test")
    if p.get("stage_filter") and len(pids) == 1 and p.get("talents") and not p.get("stage_filter_applied"):
        rows = await db.casting_pipeline.find({"project_id": pids[0], "talent_id": {"$in": [t["id"] for t in p["talents"]]}}, {"_id": 0, "talent_id": 1, "stage": 1}).to_list(500)
        keep = {r["talent_id"] for r in rows if _normalise_stage(r.get("stage")) == p["stage_filter"]}
        dropped = len(p["talents"]) - len([t for t in p["talents"] if t["id"] in keep])
        p["talents"] = [t for t in p["talents"] if t["id"] in keep]
        p["stage_filter_applied"] = True
        if dropped:
            warnings.append(f"{dropped} selected talent{'s' if dropped != 1 else ''} not in {stage_label(p['stage_filter'])} — left out")
        if not p["talents"]:
            task["status"], task["error"] = S.FAILED, f"None of the selected talents are in {stage_label(p['stage_filter'])} for this project."
            task["title"] = _title(task)
            return task

    # project must accept new talents (the existing add endpoint only takes ongoing projects)
    if task["kind"] == "add" and pids:
        docs = await db.projects.find({"id": {"$in": pids}}, {"_id": 0, "id": 1, "brand_name": 1, "status": 1}).to_list(len(pids))
        bad = [d for d in docs if d.get("status") != "ongoing"]
        if bad:
            task["status"] = S.FAILED
            task["error"] = ("Talents can only be added to ongoing projects — " + ", ".join(f"{b['brand_name']} is {b.get('status')}" for b in bad) + ".")
            return task

    # membership precheck for sends / moves that do not depend on an add in the same command
    if task["kind"] in ("send", "move") and len(pids) >= 1 and _all_resolved(p.get("talents") or []) and not task.get("depends_on"):
        tids = [t["id"] for t in p["talents"]]
        rows = await db.casting_pipeline.find({"project_id": {"$in": pids}, "talent_id": {"$in": tids}}, {"_id": 0, "project_id": 1, "talent_id": 1}).to_list(5000)
        in_proj = {r["talent_id"] for r in rows if r["project_id"] in pids}
        missing = [t for t in p["talents"] if t["id"] not in in_proj]
        if missing and len(missing) == len(tids):
            task["status"] = S.FAILED
            task["error"] = f"{'That talent is' if len(tids) == 1 else 'None of these talents are'} in the {_names(projects)} pipeline yet. Add them first."
            return task
        if missing:
            warnings.append(f"Not in {_names(projects)}: {_names(missing)} — will be skipped")

    task["warnings"] = warnings
    task["summary"] = summary_lines(task)
    if task["kind"] in CONSEQUENTIAL:
        task["status"] = S.AWAITING_CONFIRMATION
    else:
        task["status"] = "ready"
    return task


def summary_lines(task: dict) -> List[str]:
    p, k = task["params"], task["kind"]
    lines: List[str] = []
    n = len(p.get("talents") or [])
    if k == "add":
        lines = [f"{n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}", f"Into: {_names(p['projects'], 6)}"]
    elif k == "send":
        lines = [f"Template: {p.get('template_name')}", f"To {n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}", f"Project: {_names(p['projects'], 6)}",
                 "Sent through the existing WhatsApp queue — pick the worker below."]
    elif k == "move":
        lines = [f"{n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}", f"To {stage_label(p.get('stage'))} in {_names(p['projects'], 6)}"]
    elif k == "share":
        rec = p.get("recipient") or {}
        lines = [f"Content: {'media link' if p.get('lookup') == 'media_link' else 'profile'} of {_names(p['talents'], 6)}",
                 f"Recipient: {rec.get('display') or rec.get('kind')}", "Sent through the existing WhatsApp queue — pick the worker below."]
    return lines + list(task.get("warnings") or [])


# ------------------------------------------------------------------------------------------------ planning
def _base_task(owner_id: str, command_id: str, seq: int, kind: str, seg: Segment) -> dict:
    ts = S.now()
    return {"id": S.new_id(), "owner_id": owner_id, "command_id": command_id, "seq": seq, "kind": kind, "status": S.NEEDS_CLARIFICATION,
            "source_text": seg.text, "params": {"talents": [], "projects": []}, "slots": [], "depends_on": [], "warnings": [], "summary": [],
            "result": None, "outcomes": [], "batch_ids": [], "error": None, "created_at": ts, "updated_at": ts}


async def _validated_selection(ids: List[str]) -> List[dict]:
    ids = [i for i in dict.fromkeys(i for i in (ids or []) if isinstance(i, str) and i)][:MAX_SELECTED]
    cards = await R.talent_cards(ids)
    return [{"phrase": cards[i]["label"], "state": "resolved", "id": i, "label": cards[i]["label"]} for i in ids if i in cards]


async def _project_entries(seg: Segment, kind: str, prev: Optional[dict], ctx_project: Optional[dict], projects: List[dict]) -> tuple[List[dict], Optional[str]]:
    """→ (entries, inherited_from_task_id). Inherited = copied from the previous task ("send THEM the casting call")."""
    if seg.project_text:
        res = await R.resolve_projects(seg.project_text, prefer_active=kind in ("add", "send"), projects=projects)
        return [{"phrase": r["phrase"], "state": r["state"], "id": r.get("id"), "label": r.get("label"), **({"options": r["options"]} if r.get("options") else {})} for r in res], None
    inherit = seg.talent_ref == "previous" and prev and prev["params"].get("projects")
    if seg.project_ref == "current" or (not seg.project_text and kind != "add"):
        if inherit:
            return copy.deepcopy(prev["params"]["projects"]), prev["id"]
        if ctx_project:
            return [{"phrase": ctx_project["label"], "state": "resolved", "id": ctx_project["id"], "label": ctx_project["label"]}], None
    if inherit:
        return copy.deepcopy(prev["params"]["projects"]), prev["id"]
    options = await R.recent_project_options()
    return [{"phrase": "", "state": "none", "options": options, "prompt": "Which project?"}], None


async def _talent_entries(seg: Segment, prev: Optional[dict], context: dict) -> tuple[List[dict], Optional[str]]:
    if seg.talents:
        names = seg.talents[:MAX_NAMES]
        res = await asyncio.gather(*[R.resolve_talent(n) for n in names])
        return [_entry(n, r) for n, r in zip(names, res)], None
    if seg.talent_ref == "previous" and prev:
        return copy.deepcopy(prev["params"].get("talents") or []), prev["id"]
    selected = await _validated_selection((context or {}).get("talent_ids") or [])
    if selected:
        return selected, None
    if prev and prev["params"].get("talents") and seg.talent_ref in ("selected", None):
        return copy.deepcopy(prev["params"]["talents"]), prev["id"]
    return [{"phrase": "", "state": "none", "prompt": "No talents are selected. Type the names you want (e.g. “Angela and Riya”)."}], None


async def plan_command(owner_id: str, command_id: str, message: str, context: dict) -> tuple[List[dict], List[str], List[str]]:
    """→ (tasks, unrecognised_segments, offers). Tasks are NOT yet persisted."""
    context = context or {}
    segments = parse_message(message)[:MAX_SEGMENTS]
    projects = await R.load_projects()
    templates = await R.load_templates()
    ctx_project = None
    if context.get("project_id"):
        ctx_project = next((p for p in projects if p["id"] == context["project_id"]), None)
    tasks: List[dict] = []
    unrecognised: List[str] = []
    prev: Optional[dict] = None
    for seg in segments:
        if seg.intent == "offer_send":
            if prev and prev["kind"] == "add":
                prev["params"]["offer_send"] = True
            continue
        if seg.intent == "unknown":
            unrecognised.append(seg.text)
            continue
        kind = seg.intent
        t = _base_task(owner_id, command_id, len(tasks), kind, seg)
        p = t["params"]
        if seg.worker_hint:
            p["worker_hint"] = seg.worker_hint
        if kind in ("add", "send", "move", "share", "lookup"):
            p["talents"], origin = await _talent_entries(seg, prev, context)
            if origin:
                p["talents_from"] = origin
                t["depends_on"] = [origin] if (prev and prev["kind"] in ("add",) and kind in ("send", "move")) else []
            if seg.stage_filter:
                p["stage_filter"] = seg.stage_filter
        if kind in ("add", "send", "move", "list") or (kind == "share" and (seg.recipient or {}).get("kind") == "project_group"):
            p["projects"], pfrom = await _project_entries(seg, kind, prev, ctx_project, projects)
            if pfrom:
                p["projects_from"] = pfrom
        if kind == "send":
            r = await R.resolve_template(seg.template, templates)
            p["template_phrase"], p["template_state"] = seg.template or "", r["state"]
            if r["state"] == "resolved":
                p["template_id"], p["template_name"] = r["id"], r["label"]
            else:
                p["template_options"] = r.get("options") or []
        if kind == "move" or kind == "list":
            p["stage"], p["stage_text"] = seg.stage, seg.stage_text
        if kind in ("lookup", "share"):
            p["lookup"] = seg.lookup
        if kind == "share":
            p["recipient"] = seg.recipient or {"kind": "unspecified"}
        # a send / move for talents that an earlier add in THIS message is putting into the same project waits for it
        if kind in ("send", "move") and not t["depends_on"]:
            for earlier in tasks:
                if earlier["kind"] == "add" and _overlap(earlier, t):
                    t["depends_on"] = [earlier["id"]]
        if kind == "add" and t["params"].get("talents") == [] :
            t["params"]["talents"] = [{"phrase": "", "state": "none", "prompt": "Who should I add?"}]
        await finalize(t)
        tasks.append(t)
        prev = t
    return tasks, unrecognised, []


def _overlap(add_task: dict, other: dict) -> bool:
    a = {(e.get("label") or e.get("phrase") or "").lower() for e in add_task["params"].get("talents", [])}
    b = {(e.get("label") or e.get("phrase") or "").lower() for e in other["params"].get("talents", [])}
    pa = {e.get("id") or (e.get("phrase") or "").lower() for e in add_task["params"].get("projects", [])}
    pb = {e.get("id") or (e.get("phrase") or "").lower() for e in other["params"].get("projects", [])}
    return bool(a & b) and bool(pa & pb)
