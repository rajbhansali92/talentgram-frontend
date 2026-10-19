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
import re
from typing import Any, Dict, List, Optional

from core import db
from personal_assistant import resolve as R
from personal_assistant import store as S
from personal_assistant import update_plan
from personal_assistant.parser import Segment, parse_message
from routers.casting_pipeline import PIPELINE_STAGE_ORDER, _normalise_stage

CONSEQUENTIAL = {"add", "send", "move", "share", "update"}
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
                                     else (e.get("prompt") or f'I couldn\'t find anyone matching "{e["phrase"]}". Check the spelling, or search with part of the name.'))})
    for i, e in enumerate(p.get("projects") or []):
        if e["state"] != "resolved":
            slots.append({"key": f"project:{i}", "kind": "project", "phrase": e["phrase"], "options": e.get("options") or [],
                          "prompt": (f'Which project did you mean by "{e["phrase"]}"?' if e["state"] == "ambiguous"
                                     else (e.get("prompt") or f'I couldn\'t find a project matching "{e["phrase"]}". Check the spelling, or search with part of the name.'))})
    rec = p.get("recipient")
    if task["kind"] == "share" and rec and rec.get("state") != "resolved":
        slots.append({"key": "recipient", "kind": "recipient", "phrase": rec.get("label") or "", "options": rec.get("options") or [],
                      "prompt": rec.get("prompt") or "Who should I send it to?"})
    if task["kind"] == "send" and p.get("template_state") != "resolved":
        slots.append({"key": "template", "kind": "template", "phrase": p.get("template_phrase") or "",
                      "options": p.get("template_options") or [],
                      "prompt": "Which template should I send?" if not p.get("template_phrase") else f'Which template did you mean by "{p["template_phrase"]}"?'})
    if task["kind"] == "update" and p.get("updates"):
        slots += update_plan.slots(task)
    if task["kind"] == "move" and not p.get("stage"):
        sug = p.get("stage_suggest") or []
        opts = sorted(({**o, "best": True} if o["id"] == (sug[0] if sug else None) and len(sug) == 1 else {**o, "sublabel": "Looks like what you said"} if o["id"] in sug else o for o in STAGE_OPTIONS),
                      key=lambda o: 0 if o["id"] in sug else 1)
        slots.append({"key": "stage", "kind": "stage", "phrase": p.get("stage_text") or "", "options": opts,
                      "prompt": f'Which pipeline stage did you mean by "{p["stage_text"]}"?' if p.get("stage_text") else "Which pipeline stage?"})
    return slots


def _all_resolved(entries: List[dict]) -> bool:
    return bool(entries) and all(e["state"] == "resolved" for e in entries)


def adopt_recipient_project(p: dict) -> None:
    """A recipient that IS a project's casting group ("…to Amazon Now") makes that project the task's project, so the preview, the
    group lookup and the send all read the one chosen project — never a name re-typed or re-guessed later."""
    rec = p.get("recipient") or {}
    if rec.get("state") == "resolved" and rec.get("kind") == "project_group" and rec.get("project_id"):
        p["projects"] = [{"phrase": rec.get("label") or "", "state": "resolved", "id": rec["project_id"], "label": rec.get("label") or ""}]


def _title(task: dict) -> str:
    p, k = task["params"], task["kind"]
    ts, ps = _names(p.get("talents") or []), _names(p.get("projects") or [])
    if k == "add":
        return (f"First: add {ts} to {ps}" if p.get("prereq_for") else f"Add {ts} to {ps}")
    if k == "send":
        return f"Send {p.get('template_name') or p.get('template_phrase') or 'a template'} to {ts}" + (f" for {ps}" if p.get("projects") else "")
    if k == "move":
        return f"Move {ts} to {stage_label(p.get('stage')) or '…'}" + (f" in {ps}" if p.get("projects") else "")
    if k == "update":
        return update_plan.title(task)
    if k == "share":
        rec = p.get("recipient") or {}
        to = (f"the {ps} WhatsApp group" if rec.get("kind") == "project_group" and p.get("projects") else
              rec.get("label") if rec.get("state") == "resolved" and rec.get("label") else "…")
        return f"Share {('media link' if p.get('lookup') == 'media_link' else 'profile')} of {ts} with {to}"
    if k == "list":
        return f"Talents in {stage_label(p.get('stage'))}" + (f" · {ps}" if p.get("projects") else "")
    return {"profile": f"Profile · {ts}", "media_link": f"Media link · {ts}", "media": f"Photos & video · {ts}", "history": f"History · {ts}",
            "projects": f"Projects of {ts}", "submissions": f"Submissions of {ts}", "tested": f"Projects tested · {ts}", "find": f"Find {ts}"}.get(p.get("lookup"), f"Look up {ts}")


# ------------------------------------------------------------------------------------------ finalisation
async def finalize(task: dict, siblings: Optional[List[dict]] = None) -> dict:
    """Recompute slots / warnings / status of a task from its params. Idempotent; called after planning and after
    every clarification. Never executes anything."""
    p = task["params"]
    if task["kind"] == "share" and p.get("lookup") == "audition" and task["status"] not in S.TERMINAL:
        task["slots"], task["warnings"], task["summary"] = [], [], []
        task["title"] = _title(task)
        await _share_preview(task, [])                 # refuses at once, with the exact reason — no point asking who to send it to
        return task
    warnings: List[str] = []
    if task["kind"] == "update" and task["status"] not in S.TERMINAL and task["status"] not in S.ACTIVE and update_plan.all_unsupported(task):
        task["slots"], task["title"] = [], _title(task)
        task["status"], task["error"] = S.FAILED, update_plan.unsupported_message(task)       # nothing supported was asked for: do not ask "which talent?" first
        return task
    task["slots"] = compute_slots(task)
    task["title"] = _title(task)
    if task["status"] in S.TERMINAL or task["status"] in S.ACTIVE:
        return task
    if task["slots"]:
        task["status"] = S.NEEDS_CLARIFICATION
        return task

    if task["kind"] == "update":
        # every talent / value / file question is answered: read the CURRENT profile (read-only), check each value, then show exactly what will change
        await update_plan.prepare(task)
        task["slots"] = compute_slots(task)
        if task["slots"]:
            task["status"] = S.NEEDS_CLARIFICATION
            return task
        u = p["updates"]
        if not u["media"] and all(f.get("blocked") for f in u["fields"]):
            task["status"], task["error"] = S.FAILED, " ".join(f["blocked"] for f in u["fields"])
            return task
        task["warnings"] = [f"“{t}” can't be changed from here, so it will be left as it is." for t in u.get("unsupported") or []]
        task["summary"] = update_plan.describe(task)
        task["status"] = S.AWAITING_CONFIRMATION
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
        if task["kind"] == "send":
            await _plan_prerequisite(task, rows)             # missing memberships become a first step of the SAME plan
            if task["status"] == S.FAILED:
                return task
        else:
            in_proj = {r["talent_id"] for r in rows if r["project_id"] in pids}
            missing = [t for t in p["talents"] if t["id"] not in in_proj]
            if missing and len(missing) == len(tids):
                task["status"] = S.FAILED
                lead = "That talent isn't" if len(tids) == 1 else "None of these talents are"
                task["error"] = f"{lead} in the {_names(projects)} pipeline yet. Add them first."
                return task
            if missing:
                warnings.append(f"Not in {_names(projects)}: {_names(missing)} — will be skipped")

    if task["kind"] == "send":
        await _duplicate_check(task, warnings)
        await _send_destinations(task)
        if task["status"] == S.FAILED:
            return task
    if task["kind"] == "share":
        await _share_preview(task, warnings)
        if task["status"] == S.FAILED:
            return task
    task["warnings"] = warnings
    task["summary"] = summary_lines(task)
    if task["kind"] in CONSEQUENTIAL:
        task["status"] = S.AWAITING_CONFIRMATION
    else:
        task["status"] = "ready"
    return task


async def _plan_prerequisite(task: dict, rows: List[dict]) -> None:
    """A casting call can only go to talents who are IN the project's pipeline. When some (talent, project) pairs are missing,
    don't dead-end: record exactly which pairs are missing (`params.prereq`) so the add that fills them can be planned as step 1
    of the same plan. Only the requested talents and projects are ever involved. If a project can't take new talents (it isn't
    ongoing) say so plainly instead of substituting another one."""
    p = task["params"]
    present = {(r["project_id"], r["talent_id"]) for r in rows}
    tl = {t["id"]: t.get("label") or t.get("phrase") for t in p["talents"]}
    pl = {e["id"]: e.get("label") or e.get("phrase") for e in p["projects"] if e.get("id")}
    missing = [{"talent_id": tid, "talent": tl[tid], "project_id": pid, "project": pl[pid]} for tid in tl for pid in pl if (pid, tid) not in present]
    p.pop("prereq", None)
    if not missing:
        return
    mpids = list(dict.fromkeys(m["project_id"] for m in missing))
    docs = await db.projects.find({"id": {"$in": mpids}}, {"_id": 0, "id": 1, "brand_name": 1, "status": 1}).to_list(len(mpids))
    bad = [d for d in docs if d.get("status") != "ongoing"]
    if bad:
        who = ", ".join(dict.fromkeys(m["talent"] for m in missing if m["project_id"] in {b["id"] for b in bad}))
        names = ", ".join(f"{b.get('brand_name')} ({b.get('status')})" for b in bad)
        task["status"] = S.FAILED
        task["error"] = (f"{who} {'is' if ',' not in who else 'are'} not in the pipeline of {names}, and talents can only be added to ongoing projects, so "
                         "I can't add them there. Choose an ongoing project, or reopen that one first.")
        return
    p["prereq"] = {"talent_ids": list(dict.fromkeys(m["talent_id"] for m in missing)), "project_ids": mpids, "missing": missing,
                   "already": [{"talent": tl[tid], "project": pl[pid]} for tid in tl for pid in pl if (pid, tid) in present]}


async def build_prereq_task(owner_id: str, send: dict) -> dict:
    """The add that fills a send's missing pipeline entries, planned as an ordinary `add` task (so the existing add operation,
    its validation, idempotency and audit all apply) that the send depends on."""
    pre = send["params"]["prereq"]
    ts = S.now()
    talents = [dict(e) for e in send["params"]["talents"] if e.get("id") in pre["talent_ids"]]
    projects = [dict(e) for e in send["params"]["projects"] if e.get("id") in pre["project_ids"]]
    add = {"id": S.new_id(), "owner_id": owner_id, "command_id": send["command_id"], "seq": send.get("seq", 0), "kind": "add", "status": S.NEEDS_CLARIFICATION,
           "source_text": send.get("source_text") or "", "slots": [], "depends_on": [], "warnings": [], "summary": [], "result": None, "outcomes": [],
           "batch_ids": [], "error": None, "created_at": ts, "updated_at": ts,
           "params": {"talents": talents, "projects": projects, "prereq_for": send["id"], "missing": pre["missing"], "already": pre["already"]}}
    await finalize(add)
    return add


def link_prerequisite(send: dict, add: dict) -> None:
    send["depends_on"] = [add["id"]]
    send["params"]["strict_dependency"] = True                # the send waits for the add to FULLY succeed, not just partly
    send["params"]["prereq_task"] = add["id"]


async def attach_prerequisite(owner_id: str, send: dict) -> Optional[dict]:
    """For a task that already exists (planned earlier, completed by a clarification): create + persist the add step before it."""
    if not send["params"].get("prereq") or send.get("depends_on"):
        return None
    add = await build_prereq_task(owner_id, send)
    add["seq"] = send.get("seq", 0) - 0.5
    link_prerequisite(send, add)
    await S.insert_tasks([add])
    await finalize(send)
    return add


async def _duplicate_check(task: dict, warnings: List[str]) -> None:
    """A second identical send (same template, same project, same talent) within a day is almost always a repeat.
    The existing queue does not dedupe across requests, so say so before confirming — never block, never hide."""
    from datetime import datetime, timedelta, timezone
    p = task["params"]
    pids = [e["id"] for e in p.get("projects") or [] if e.get("id")]
    tids = [e["id"] for e in p.get("talents") or [] if e.get("id")]
    if not (p.get("template_id") and pids and tids):
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    batches = await db.whatsapp_batches.find({"project_id": {"$in": pids}, "template_id": p["template_id"], "created_at": {"$gte": since}}, {"_id": 0, "id": 1}).to_list(200)
    if not batches:
        return
    jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": [b["id"] for b in batches]}, "talent_id": {"$in": tids}, "status": {"$in": ["pending", "sending", "sent"]}},
                                        {"_id": 0, "talent_name": 1, "status": 1}).to_list(500)
    if jobs:
        names = sorted({j.get("talent_name") or "?" for j in jobs})
        warnings.append(f"Already queued or sent in the last 24 h with this template for this project: {', '.join(names[:4])}{' +%d more' % (len(names) - 4) if len(names) > 4 else ''} — sending again would message them twice.")
        task["duplicate_risk"] = True


async def _send_destinations(task: dict) -> None:
    """Where each message would actually go — decided by the SENDER's own routing rule (routers.whatsapp._resolve_destination:
    the talent's WhatsApp group if set, else their phone), read-only, so the preview cannot disagree with the send. Numbers are
    masked like the share previews. If NOBODY has a usable destination — or it cannot be checked — the task fails here with the
    reason instead of offering a Confirm; talents without one are listed as skipped, exactly as the queue skips them."""
    p = task["params"]
    talents = [t for t in p.get("talents") or [] if t.get("state") == "resolved" and t.get("id")]
    if not talents:
        return
    try:
        from routers.whatsapp import _resolve_destination
        docs = await db.talents.find({"id": {"$in": [t["id"] for t in talents]}}, {"_id": 0, "id": 1, "phone": 1, "whatsapp_group_name": 1}).to_list(len(talents))
        by_id = {d["id"]: d for d in docs}
        items = []
        for t in talents:
            doc = by_id.get(t["id"])
            kind, dest, _ = _resolve_destination(doc) if doc else ("", None, None)
            if kind == "group":
                items.append({"name": t["label"], "kind": "group", "text": f"WhatsApp group “{dest}”"})
            elif kind == "number" and R.mask_phone(dest) != "no number on file":
                items.append({"name": t["label"], "kind": "number", "text": f"WhatsApp {R.mask_phone(dest)}"})
            else:
                items.append({"name": t["label"], "kind": "none", "text": "no WhatsApp number or group on file — will be skipped"})
    except Exception:
        task["status"] = S.FAILED
        task["error"] = "I couldn't check where these messages would be sent, so I won't offer to send them. Try again in a moment."
        return
    task["preview"] = {**(task.get("preview") or {}), "destinations": items}
    if all(i["kind"] == "none" for i in items):
        task["status"] = S.FAILED
        task["error"] = (("That talent has no" if len(items) == 1 else "None of these talents has a") + " WhatsApp number or group on file, so there is nowhere to send "
                         "this. Add one on the talent's profile first.")


async def _share_preview(task: dict, warnings: List[str]) -> None:
    """What will actually be sent and to whom — computed WITHOUT writing anything (no link is minted here)."""
    p = task["params"]
    rec = p.get("recipient") or {}
    prev: Dict[str, Any] = {"items": []}
    if p.get("lookup") == "audition":
        task["status"] = S.FAILED
        task["error"] = ("Sending an audition isn't something I can do yet. The existing audition SEND runs inside the WhatsApp casting-group workflow "
                         "(it works from the marked WhatsApp media with its own approval and duplicate protection) and has no function the assistant can safely call. "
                         "I can share the talent's profile or media link instead.")
        return
    if rec.get("kind") == "project_group":
        pid = ((p.get("projects") or [{}])[0] or {}).get("id")
        doc = await db.projects.find_one({"id": pid}, {"_id": 0, "whatsapp_casting_group_name": 1}) if pid else None
        group = ((doc or {}).get("whatsapp_casting_group_name") or "").strip()
        if not pid:
            task["status"], task["error"] = S.FAILED, "Tell me which project's WhatsApp group (e.g. “…with the group for Amazon Now”)."
            return
        if not group:
            task["status"], task["error"] = S.FAILED, f"{p['projects'][0]['label']} has no WhatsApp casting group set, so there is nowhere to send to."
            return
        prev["group"] = group
        pdoc = await db.projects.find_one({"id": pid}, {"_id": 0, "status": 1})
        status = (pdoc or {}).get("status") or "ongoing"
        if status != "ongoing":
            warnings.append(f"{p['projects'][0]['label']} is {status}, not ongoing — check this is the group you want.")
        col = await R.group_collisions(group, pid)
        if col["others"]:
            warnings.append(f"“{group}” is also the casting group of {col['others']} other project{'s' if col['others'] != 1 else ''}"
                            f"{' (' + str(col['ongoing']) + ' ongoing)' if col['ongoing'] else ''} — the message lands in that one shared chat.")
        access = await R.group_evidence(group)
        prev["worker_access"] = access
        if access and not any(a["state"] == "verified" for a in access.values()):
            warnings.append("No WhatsApp worker has sent to this group before, so I can't confirm the one you pick is a member. "
                            "If it isn't, nothing is delivered and the failure shows in the result.")
    if rec.get("kind") in ("crm",) and not rec.get("phone_present"):
        task["status"], task["error"] = S.FAILED, f"{rec.get('label')} has no phone number saved in the CRM."
        return
    for t in p.get("talents") or []:
        if t.get("state") != "resolved":
            continue
        if p.get("lookup") == "media_link":
            pk = await R.peek_media_link(t["id"])
            text = {"ready": pk.get("url"), "will_create": "their media download link (created when you confirm)", "off": "media link is switched off — will be skipped",
                    "missing": "talent not found — will be skipped"}[pk["state"]]
            if pk["state"] in ("off", "missing"):
                warnings.append(f"{t['label']}: {text}")
            elif rec.get("kind") == "project_group" and pk["state"] == "ready" and pk.get("url") and await _recent_group_share(prev.get("group"), pk["url"]):
                warnings.append(f"{t['label']}'s media link was already sent or queued to this group in the last 24 h — sending again would post it twice.")
                task["duplicate_risk"] = True
        else:
            text = "profile / portfolio link, as the Fetcher sends it"
        st = "profile"
        if p.get("lookup") == "media_link":
            st = "link" if pk["state"] in ("ready", "will_create") else pk["state"]       # a link counts as "a link"; its token is irrelevant
        prev["items"].append({"name": t["label"], "text": text, "state": st})
    task["preview"] = prev


async def _recent_group_share(group: Optional[str], url: str) -> bool:
    from datetime import datetime, timedelta, timezone
    if not (group and url):
        return False
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return bool(await db.whatsapp_jobs.find_one({"destination_type": "group", "destination": group, "created_at": {"$gte": since}, "is_dry_run": {"$ne": True},
                                                 "status": {"$in": ["pending", "sending", "sent", "sent_unverified"]}, "message_body": {"$regex": re.escape(url)}}, {"_id": 1}))


def _heard_lines(p: dict) -> List[str]:
    """Raw words → what they were resolved to, wherever the two differ ("You said “doha kumar” → Disha Kumar"), so the approval shows both
    the user's own words and the record that will really be used."""
    from agents.modules import casting_pipeline_nlu as nlu
    out: List[str] = []
    for key, norm in (("talents", nlu._normalize_name), ("projects", nlu._normalize_project_label)):
        for e in p.get(key) or []:
            if e.get("state") == "resolved" and e.get("phrase") and e.get("label") and norm(e["phrase"]) != norm(e["label"]):
                out.append(f"You said “{e['phrase']}” → {e['label']}")
    if p.get("stage") and p.get("stage_text"):
        out.append(f"You said “{p['stage_text']}” → {stage_label(p['stage'])}")
    return out[:6]


def summary_lines(task: dict) -> List[str]:
    p, k = task["params"], task["kind"]
    lines: List[str] = []
    if k == "update":
        return update_plan.describe(task)
    n = len(p.get("talents") or [])
    if k == "add":
        lines = [f"{n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}", f"Into: {_names(p['projects'], 6)}"]
        if p.get("prereq_for"):
            lines = ["Step 1 of 2 — only the missing pipeline entries are created:"] + [f"  • {m['talent']} → {m['project']} (not in the pipeline yet)" for m in (p.get("missing") or [])[:8]] \
                + [f"  • {a['talent']} → {a['project']} (already there)" for a in (p.get("already") or [])[:4]]
    elif k == "send":
        dests = (task.get("preview") or {}).get("destinations") or []
        pre = p.get("prereq") or {}
        lines = ([f"Step 2 of 2 — runs only after step 1 succeeds. Not in the pipeline yet: {', '.join(dict.fromkeys(m['talent'] for m in pre.get('missing') or []))}"] if pre else [])
        lines += [f"Template: {p.get('template_name')}", f"To {n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}"]
        lines += [f"  • {d['name']} → {d['text']}" for d in dests[:8]] + ([f"  • …and {len(dests) - 8} more"] if len(dests) > 8 else [])
        lines += [f"Project: {_names(p['projects'], 6)}", "Sent through the existing WhatsApp queue — pick the worker below."]
    elif k == "move":
        lines = [f"{n} talent{'s' if n != 1 else ''}: {_names(p['talents'], 6)}", f"To {stage_label(p.get('stage'))} in {_names(p['projects'], 6)}"]
    elif k == "share":
        rec = p.get("recipient") or {}
        dest = rec.get("display") or rec.get("label") or "?"
        if rec.get("kind") == "project_group" and (p.get("projects") or [None])[0]:
            dest = f"WhatsApp group “{task.get('preview', {}).get('group') or '…'}” of {p['projects'][0]['label']}"
        lines = [f"Recipient: {dest}", f"Content: {'media link' if p.get('lookup') == 'media_link' else 'profile'} of {n} talent{'s' if n != 1 else ''}:"]
        for it in (task.get("preview") or {}).get("items", []):
            lines.append(f"  • {it['name']} — {it['text']}")
        acc = (task.get("preview") or {}).get("worker_access") or {}
        for a in acc.values():
            note = {"verified": f"has sent to this group before{' (' + a['when'] + ')' if a.get('when') else ''}", "failed_before": f"could NOT find this group last time{' (' + a['when'] + ')' if a.get('when') else ''} — sending is held for it",
                    "unknown": "not verified for this group yet"}[a["state"]]
            lines.append(f"Worker {a['label']}: {note}")
        lines.append("Sent through the existing WhatsApp queue — pick the worker below.")
    return lines + _heard_lines(p) + list(task.get("warnings") or [])


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
        res = await R.resolve_projects(seg.project_text, prefer_active=kind in ("add", "send", "share"), projects=projects)
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


async def _talent_entries(seg: Segment, prev: Optional[dict], context: dict, project_ids: Optional[List[str]] = None) -> tuple[List[dict], Optional[str]]:
    if seg.talent_ref == "ask":
        return [{"phrase": "", "state": "none", "prompt": "Which talents do you mean by “these other talents”? Type their names — I won't reuse the selection on screen for that."}], None
    if seg.talents:
        names = seg.talents[:MAX_NAMES]
        res = await asyncio.gather(*[R.resolve_talent(n, project_ids) for n in names])
        return [_entry(n, r) for n, r in zip(names, res)], None
    if seg.talent_ref == "previous" and prev:
        return copy.deepcopy(prev["params"].get("talents") or []), prev["id"]
    selected = await _validated_selection((context or {}).get("talent_ids") or [])
    if selected:
        return selected, None
    # Another clause's talents are NEVER inherited just because they came first: only an explicit pronoun ("send THEM…") — handled above —
    # points at the previous action. A clause that names nobody and has no on-screen selection asks, instead of acting on someone else.
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
        if seg.intent == "update":
            new = await _plan_update(owner_id, command_id, seg, prev, context, len(tasks))
            if not new:
                unrecognised.append(seg.text)
            tasks.extend(new)
            prev = new[-1] if new else prev
            continue
        if seg.intent == "unknown":
            unrecognised.append(seg.text + (" — it contains a correction, so I did not act on it; please say it once, with the name you want" if seg.correction else ""))
            continue
        kind = seg.intent
        t = _base_task(owner_id, command_id, len(tasks), kind, seg)
        p = t["params"]
        if seg.worker_hint:
            p["worker_hint"] = seg.worker_hint
        if kind in ("add", "send", "move", "list") or (kind == "share" and ((seg.recipient or {}).get("kind") == "project_group")):
            # projects first: the project named in THIS clause is context for telling similar talent names apart (cards only — it never chooses)
            p["projects"], pfrom = await _project_entries(seg, kind, prev, ctx_project, projects)
            if pfrom:
                p["projects_from"] = pfrom
        if kind in ("add", "send", "move", "share", "lookup"):
            ctx_pids = [e["id"] for e in p.get("projects") or [] if e.get("state") == "resolved" and e.get("id")]
            p["talents"], origin = await _talent_entries(seg, prev, context, ctx_pids)
            if origin:
                p["talents_from"] = origin
                t["depends_on"] = [origin] if (prev and prev["kind"] in ("add",) and kind in ("send", "move")) else []
            if seg.stage_filter:
                p["stage_filter"] = seg.stage_filter
        if kind == "send":
            r = await R.resolve_template(seg.template, templates)
            p["template_phrase"], p["template_state"] = seg.template or "", r["state"]
            if r["state"] == "resolved":
                p["template_id"], p["template_name"] = r["id"], r["label"]
            else:
                p["template_options"] = r.get("options") or []
        if kind == "move" or kind == "list":
            p["stage"], p["stage_text"] = seg.stage, seg.stage_text
            if seg.stage_suggest:
                p["stage_suggest"] = list(seg.stage_suggest)
        if kind in ("lookup", "share"):
            p["lookup"] = seg.lookup
        if kind == "share":
            p["recipient"] = await R.resolve_recipient(seg.recipient or {"kind": "unspecified"})
            adopt_recipient_project(p)
        # a send / move for talents that an earlier add in THIS message is putting into the same project waits for it
        if kind in ("send", "move") and not t["depends_on"]:
            for earlier in tasks:
                if earlier["kind"] == "add" and _overlap(earlier, t):
                    t["depends_on"] = [earlier["id"]]
        if kind == "add" and t["params"].get("talents") == [] :
            t["params"]["talents"] = [{"phrase": "", "state": "none", "prompt": "Who should I add?"}]
        await finalize(t)
        if t["kind"] == "send" and t["status"] == S.AWAITING_CONFIRMATION and p.get("prereq") and not t["depends_on"]:
            add_t = await build_prereq_task(owner_id, t)
            add_t["seq"] = len(tasks)
            tasks.append(add_t)
            t["seq"] = len(tasks)
            link_prerequisite(t, add_t)
            await finalize(t)                                  # recompute the summary now that step 1 exists
        tasks.append(t)
        prev = t
    return tasks, unrecognised, []


async def _plan_update(owner_id: str, command_id: str, seg: Segment, prev: Optional[dict], context: dict, seq0: int) -> List[dict]:
    """`update …` → ONE task per talent (each with only its own items), so a file or value can never attach to another talent and one
    talent's failure cannot touch another's. Names resolve exactly as everywhere else (cards for anything uncertain)."""
    out: List[dict] = []
    for g in seg.update_groups:
        entries: List[dict] = []
        if g.get("ref") == "previous":
            entries = [dict(e) for e in (prev or {}).get("params", {}).get("talents", [])]
            if not entries:
                entries = [{"phrase": "", "state": "none", "prompt": "Whose profile should I update? Say the name."}]
        else:
            names = g["names"][:MAX_NAMES]
            res = await asyncio.gather(*[R.resolve_talent(n) for n in names])
            entries = [_entry(n, r) for n, r in zip(names, res)]
        for e in entries:
            t = _base_task(owner_id, command_id, seq0 + len(out), "update", seg)
            t["params"]["talents"] = [e]
            t["params"]["updates"] = update_plan.new_params([dict(i) for i in g["items"]])
            await finalize(t)
            out.append(t)
    return out


def _overlap(add_task: dict, other: dict) -> bool:
    a = {(e.get("label") or e.get("phrase") or "").lower() for e in add_task["params"].get("talents", [])}
    b = {(e.get("label") or e.get("phrase") or "").lower() for e in other["params"].get("talents", [])}
    pa = {e.get("id") or (e.get("phrase") or "").lower() for e in add_task["params"].get("projects", [])}
    pb = {e.get("id") or (e.get("phrase") or "").lower() for e in other["params"].get("projects", [])}
    return bool(a & b) and bool(pa & pb)
