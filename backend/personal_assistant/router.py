"""Personal Assistant HTTP surface — /api/personal-assistant/*

All routes require an authenticated team/admin user who is allowed to use the assistant (access.py). The owner
of every record is the JWT user; ids in the request body/path are only ever looked up WITHIN that owner's records.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agents.modules import casting_pipeline_nlu as nlu
from personal_assistant import executors as X
from personal_assistant import planner as P
from personal_assistant import resolve as R
from personal_assistant import store as S
from personal_assistant.access import assistant_user, is_allowed
from core import current_team_or_admin
from routers.casting_pipeline import PIPELINE_STAGE_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/personal-assistant", tags=["Personal Assistant"])

MAX_CONCURRENT = 3
NEEDS_WORKER = {"send", "share"}
HELP = ("I can look up a talent (“show Angela's profile / media link / submission history”), add talents to projects, "
        "send a casting call or any template, move talents between pipeline stages, list who is in a stage, and share a media link or profile.")


class ContextIn(BaseModel):
    project_id: Optional[str] = Field(default=None, max_length=64)
    talent_ids: List[str] = Field(default_factory=list, max_length=P.MAX_SELECTED)
    page: Optional[str] = Field(default=None, max_length=120)


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    client_request_id: str = Field(..., min_length=8, max_length=80)
    context: ContextIn = Field(default_factory=ContextIn)


class ClarifyIn(BaseModel):
    slot: str = Field(..., max_length=40)
    choice_id: Optional[str] = Field(default=None, max_length=80)
    text: Optional[str] = Field(default=None, max_length=300)


class ConfirmIn(BaseModel):
    worker_id: Optional[str] = Field(default=None, max_length=80)


class FollowUpIn(BaseModel):
    template_id: str = Field(..., max_length=80)


def _pub(t: Optional[dict]) -> Optional[dict]:
    return S.public(t)


def _reply(tasks: List[dict], unrecognised: List[str]) -> str:
    if not tasks:
        return "I couldn't turn that into an action. " + HELP
    need = sum(1 for t in tasks if t["status"] == S.NEEDS_CLARIFICATION)
    conf = sum(1 for t in tasks if t["status"] == S.AWAITING_CONFIRMATION)
    done = sum(1 for t in tasks if t["status"] == S.COMPLETED)
    failed = sum(1 for t in tasks if t["status"] == S.FAILED)
    bits = []
    if done:
        bits.append(f"{done} lookup{'s' if done != 1 else ''} done")
    if need:
        bits.append(f"{need} need{'s' if need == 1 else ''} your pick")
    if conf:
        bits.append(f"{conf} waiting for your OK")
    if failed:
        bits.append(f"{failed} can't be done as asked")
    text = "Here's what I understood: " + ", ".join(bits) + "." if bits else "Done."
    if unrecognised:
        text += " I didn't understand: “" + "”, “".join(unrecognised[:2]) + "”."
    return text


async def _own(user: dict, task_id: str) -> dict:
    t = await S.get_task(user["id"], task_id)
    if not t:
        raise HTTPException(404, "Task not found")      # same answer for "not yours" and "doesn't exist"
    return t


async def _auto_run_reads(user: dict, tasks: List[dict]) -> List[dict]:
    out = []
    for t in tasks:
        if t["status"] == "ready":
            t = await X.run_task(user, t, None)
        out.append(t)
    return out


# ----------------------------------------------------------------------------------------------------- access
@router.get("/access")
async def access(user: Dict[str, Any] = Depends(current_team_or_admin)):
    """Cheap capability probe for the shell (the icon is only shown when allowed). Not a security boundary."""
    return {"allowed": is_allowed(user), "role": user.get("role")}


@router.get("/workers")
async def workers(user: Dict[str, Any] = Depends(assistant_user)):
    return {"workers": await X.workers_snapshot()}


# ---------------------------------------------------------------------------------------------------- message
@router.post("/message")
async def message(body: MessageIn, user: Dict[str, Any] = Depends(assistant_user)):
    owner = user["id"]
    cmd, created = await S.claim_command(owner, body.text, body.client_request_id, body.context.model_dump())
    if not created:                                       # double click / retry → the original result, no new tasks
        tasks = await S.list_tasks(owner, 40, command_id=cmd["id"])
        tasks = [await X.refresh_task(owner, t) for t in sorted(tasks, key=lambda t: t["seq"])]
        return {"command_id": cmd["id"], "duplicate": True, "reply": cmd.get("reply"), "tasks": [_pub(t) for t in tasks], "unrecognised": []}
    await S.add_message(owner, "user", body.text, cmd["id"])
    tasks, unrecognised, _ = await P.plan_command(owner, cmd["id"], body.text, body.context.model_dump())
    await S.insert_tasks(tasks)
    tasks = await _auto_run_reads(user, tasks)
    reply = _reply(tasks, unrecognised)
    await S.finish_command(owner, cmd["id"], [t["id"] for t in tasks], reply)
    await S.add_message(owner, "assistant", reply, cmd["id"])
    return {"command_id": cmd["id"], "duplicate": False, "reply": reply, "tasks": [_pub(t) for t in tasks], "unrecognised": unrecognised}


# ------------------------------------------------------------------------------------------------- clarify
def _apply_choice(task: dict, slot: str, choice_id: str) -> bool:
    p = task["params"]
    kind, _, idx = slot.partition(":")
    if kind in ("talent", "project"):
        key = "talents" if kind == "talent" else "projects"
        i = int(idx) if idx.isdigit() else -1
        if not (0 <= i < len(p.get(key) or [])):
            return False
        e = p[key][i]
        opt = next((o for o in e.get("options") or [] if o["id"] == choice_id), None)
        if not opt:
            return False
        e.update({"state": "resolved", "id": opt["id"], "label": opt["label"]})
        e.pop("options", None)
        return True
    if kind == "template":
        opt = next((o for o in p.get("template_options") or [] if o["id"] == choice_id), None)
        if not opt:
            return False
        p.update({"template_id": opt["id"], "template_name": opt["label"], "template_state": "resolved"})
        p.pop("template_options", None)
        return True
    if kind == "stage":
        if choice_id not in PIPELINE_STAGE_ORDER:
            return False
        p["stage"] = choice_id
        return True
    return False


async def _apply_text(task: dict, slot: str, text: str) -> bool:
    p = task["params"]
    kind, _, idx = slot.partition(":")
    if kind == "talent":
        i = int(idx) if idx.isdigit() else -1
        if not (0 <= i < len(p.get("talents") or [])):
            return False
        p["talents"][i] = P._entry(text, await R.resolve_talent(text))
        return True
    if kind == "project":
        i = int(idx) if idx.isdigit() else -1
        if not (0 <= i < len(p.get("projects") or [])):
            return False
        res = await R.resolve_projects(text, prefer_active=task["kind"] in ("add", "send", "share"))
        entries = [{"phrase": r["phrase"], "state": r["state"], "id": r.get("id"), "label": r.get("label"), **({"options": r["options"]} if r.get("options") else {})} for r in res]
        p["projects"][i:i + 1] = entries or p["projects"][i:i + 1]
        return True
    if kind == "template":
        r = await R.resolve_template(text)
        p["template_phrase"], p["template_state"] = text, r["state"]
        if r["state"] == "resolved":
            p.update({"template_id": r["id"], "template_name": r["label"]})
            p.pop("template_options", None)
        else:
            p["template_options"] = r.get("options") or []
        return True
    if kind == "stage":
        m = nlu.match_stage_phrase(text, PIPELINE_STAGE_ORDER)
        p["stage"], p["stage_text"] = m.key, None if m.key else text
        return True
    return False


async def _propagate(owner: str, origin: dict) -> None:
    """A correction continues the SAME command: later tasks that borrowed this task's talents / projects follow it."""
    for t in await S.list_tasks(owner, 40, command_id=origin["command_id"]):
        if t["status"] in S.TERMINAL or t["status"] in S.ACTIVE or t["id"] == origin["id"]:
            continue
        changed = False
        if t["params"].get("talents_from") == origin["id"]:
            t["params"]["talents"] = [dict(e) for e in origin["params"]["talents"]]
            changed = True
        if t["params"].get("projects_from") == origin["id"]:
            t["params"]["projects"] = [dict(e) for e in origin["params"]["projects"]]
            changed = True
        if changed:
            await P.finalize(t)
            await S.update_task(owner, t["id"], {k: t[k] for k in ("params", "slots", "status", "title", "summary", "warnings", "error", "preview", "duplicate_risk") if k in t})


async def _apply_recipient(task: dict, choice_id: Optional[str], text: Optional[str]) -> bool:
    p = task["params"]
    rec = p.get("recipient") or {}
    if choice_id:
        if not any(o["id"] == choice_id for o in rec.get("options") or []):
            return False                                   # only what the server offered
        new = await R.recipient_from_option(choice_id)
    else:
        t = (text or "").strip()
        digits = re.sub(r"\D", "", t)
        new = await R.resolve_recipient({"kind": "number", "value": ("+" if t.startswith("+") else "") + digits} if len(digits) >= 8 and not re.search(r"[A-Za-z]", t)
                                        else {"kind": "named", "value": t})
        if new.get("state") != "resolved":
            p["recipient"] = new                           # still unresolved: show the new options / prompt
            return True
    if not new:
        return False
    p["recipient"] = new
    return True


@router.post("/tasks/{task_id}/clarify")
async def clarify(task_id: str, body: ClarifyIn, user: Dict[str, Any] = Depends(assistant_user)):
    owner = user["id"]
    task = await _own(user, task_id)
    if task["status"] != S.NEEDS_CLARIFICATION:
        raise HTTPException(409, "This task doesn't need clarification any more.")
    if not (body.choice_id or (body.text or "").strip()):
        raise HTTPException(400, "Pick one of the options or type a correction.")
    if body.slot == "recipient" and task["kind"] == "share":
        ok = await _apply_recipient(task, body.choice_id, body.text)
    else:
        ok = _apply_choice(task, body.slot, body.choice_id) if body.choice_id else await _apply_text(task, body.slot, body.text.strip())
    if not ok:
        raise HTTPException(400, "That isn't one of the options for this question.")
    task = await P.finalize(task)
    saved = await S.update_task(owner, task_id, {k: task[k] for k in ("params", "slots", "status", "title", "summary", "warnings", "error", "preview", "duplicate_risk") if k in task}, only_status=[S.NEEDS_CLARIFICATION])
    if saved is None:
        raise HTTPException(409, "This task was just changed — refresh and try again.")
    await _propagate(owner, saved)
    if saved["status"] == "ready":
        saved = await X.run_task(user, saved, None)
    return {"task": _pub(saved)}


# ----------------------------------------------------------------------------------------------- confirm
async def _resolve_worker(task: dict, requested: Optional[str]) -> Optional[str]:
    return (requested or "").strip() or await X.worker_id_for_hint(task["params"].get("worker_hint"))


async def _confirm_one(user: dict, task: dict, requested_worker: Optional[str]) -> dict:
    owner = user["id"]
    if task["status"] != S.AWAITING_CONFIRMATION:
        return task                                       # already confirmed / moved: never run twice
    block = await X.dependency_block(owner, task)
    if block:
        raise HTTPException(409, block)
    worker = None
    if task["kind"] in NEEDS_WORKER:
        worker = await _resolve_worker(task, requested_worker)
        if not worker:
            raise HTTPException(400, "Choose which WhatsApp worker to send from.")
        from routers.whatsapp_workers import require_sendable_worker
        await require_sendable_worker(worker)             # 404 / 403 / 409 — an explicit choice is never swapped for another worker
    return await X.run_task(user, task, worker)


@router.post("/tasks/{task_id}/confirm")
async def confirm(task_id: str, body: ConfirmIn = ConfirmIn(), user: Dict[str, Any] = Depends(assistant_user)):
    task = await _own(user, task_id)
    done = await _confirm_one(user, task, body.worker_id)
    return {"task": _pub(await X.refresh_task(user["id"], done))}


@router.post("/commands/{command_id}/confirm-all")
async def confirm_all(command_id: str, body: ConfirmIn = ConfirmIn(), user: Dict[str, Any] = Depends(assistant_user)):
    """Confirm every waiting task of a command. Independent tasks run concurrently (bounded); a task that depends
    on another waits for it and is skipped — with the reason — if the earlier one did not succeed."""
    owner = user["id"]
    tasks = sorted(await S.list_tasks(owner, 60, command_id=command_id), key=lambda t: t["seq"])
    if not tasks:
        raise HTTPException(404, "Command not found")
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results: Dict[str, dict] = {}

    async def one(t: dict) -> None:
        async with sem:
            try:
                results[t["id"]] = {"task": _pub(await X.refresh_task(owner, await _confirm_one(user, t, body.worker_id)))}
            except HTTPException as exc:
                results[t["id"]] = {"task": _pub(t), "error": exc.detail if isinstance(exc.detail, str) else str(exc.detail)}

    waiting = [t for t in tasks if t["status"] == S.AWAITING_CONFIRMATION]
    first = [t for t in waiting if not t.get("depends_on")]
    later = [t for t in waiting if t.get("depends_on")]
    await asyncio.gather(*[one(t) for t in first])
    for t in later:                                       # dependents in order, each re-checked against its dependency's REAL result
        fresh = await S.get_task(owner, t["id"])
        await one(fresh)
    return {"results": results}


# ------------------------------------------------------------------------------------- cancel / retry / follow-up
@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, user: Dict[str, Any] = Depends(assistant_user)):
    task = await _own(user, task_id)
    return {"task": _pub(await X.cancel_task(user, task))}


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: str, user: Dict[str, Any] = Depends(assistant_user)):
    task = await _own(user, task_id)
    return {"task": _pub(await X.retry_task(user, task))}


@router.post("/tasks/{task_id}/follow-up")
async def follow_up(task_id: str, body: FollowUpIn, user: Dict[str, Any] = Depends(assistant_user)):
    """After an "add" completes: queue a send of a template to the SAME talents/projects (ids carried over, so
    nothing is re-resolved by name). It still waits for confirmation and a worker choice."""
    owner = user["id"]
    src = await _own(user, task_id)
    if src["kind"] != "add" or src["status"] not in (S.COMPLETED, S.PARTIAL):
        raise HTTPException(409, "A follow-up send is only offered after talents were added.")
    tpl = next((t for t in await R.load_templates() if t["id"] == body.template_id and t.get("slug") != "custom"), None)
    if not tpl:
        raise HTTPException(404, "Template not found")
    ts = S.now()
    t = {"id": S.new_id(), "owner_id": owner, "command_id": src["command_id"], "seq": 99, "kind": "send", "status": S.NEEDS_CLARIFICATION,
         "source_text": f"Send {tpl['name']} (follow-up)", "slots": [], "depends_on": [], "warnings": [], "summary": [], "result": None, "outcomes": [],
         "batch_ids": [], "error": None, "created_at": ts, "updated_at": ts,
         "params": {"talents": [dict(e) for e in src["params"]["talents"]], "projects": [dict(e) for e in src["params"]["projects"]],
                    "template_id": tpl["id"], "template_name": tpl["name"], "template_state": "resolved", "template_phrase": tpl["name"]}}
    await P.finalize(t)
    await S.insert_tasks([t])
    return {"task": _pub(t)}


# ---------------------------------------------------------------------------------------------- tasks / history
@router.get("/tasks")
async def tasks_list(limit: int = 30, command_id: Optional[str] = None, user: Dict[str, Any] = Depends(assistant_user)):
    owner = user["id"]
    rows = await S.list_tasks(owner, max(1, min(limit, 60)), command_id=command_id)
    rows = [await X.refresh_task(owner, t) for t in rows]
    return {"tasks": [_pub(t) for t in rows]}


@router.get("/tasks/{task_id}")
async def task_get(task_id: str, user: Dict[str, Any] = Depends(assistant_user)):
    return {"task": _pub(await X.refresh_task(user["id"], await _own(user, task_id)))}


@router.get("/history")
async def history(user: Dict[str, Any] = Depends(assistant_user)):
    return {"messages": await S.history(user["id"])}


@router.post("/history/clear")
async def history_clear(user: Dict[str, Any] = Depends(assistant_user)):
    return {"deleted": await S.clear_history(user["id"])}
