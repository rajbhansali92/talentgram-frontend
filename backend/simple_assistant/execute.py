"""Controlled execution of an approved Simple Assistant ActionPlan (Phase 3).

Pipeline:  verified context  →  structural validation  →  live entity check
           →  per-talent stale check  →  reuse existing pipeline mutation
           →  post-verify  →  audit  →  structured result + fresh overview

There is NO direct MongoDB write in this module. Every mutation goes
through the *exact* function the existing casting-pipeline REST endpoints
call:

    add    → routers.casting_pipeline.add_talents_to_pipeline   (idempotent)
    mark   → routers.casting_pipeline.bulk_move_by_talent_ids
    remove → routers.casting_pipeline.bulk_delete_pipeline       (the endpoint fn)

The client cannot inject an action: the plan it "confirms" is the
HMAC-signed one the server itself produced (security.verify), and even a
valid signature is re-checked against canonical stage vocabulary and live
DB state before anything runs.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from routers.casting_pipeline import (
    PIPELINE_STAGES,
    PipelineBulkDeleteIn,
    _normalise_stage,
    add_talents_to_pipeline,
    bulk_delete_pipeline,
    bulk_move_by_talent_ids,
)
from simple_assistant import audit
from simple_assistant.commands import (
    EXECUTABLE_INTENTS,
    STAGE_LABELS,
    _envelope,
    _is_expired,
    _verified_pending,
    build_overview,
)
from simple_assistant.readonly_db import RDB

logger = logging.getLogger(__name__)

_ALLOWED_OPS = {"update", "add", "remove"}
_ADD_STAGE = "ask_to_test"


def _blocked(cid: str, message: str, *, need_refresh: bool = False) -> dict:
    env = _envelope(conversation_id=cid, state="blocked", message=message)
    env["need_refresh"] = need_refresh
    return env


async def confirm_and_execute(
    *, conversation_id: Optional[str], context: Optional[dict], user: dict
) -> dict:
    cid = conversation_id or (context or {}).get("conversation_id") or "sa_unknown"

    # ---- 1. authenticity: the plan must be the server's own signed one ----
    pending = _verified_pending(context)
    if pending is None:
        return _blocked(
            cid,
            "I couldn't verify that action. Please ask me again so I can prepare a fresh plan.",
            need_refresh=True,
        )
    if pending.get("kind") != "confirm_plan":
        return _blocked(cid, "There's no pending action to confirm.")
    plan = pending.get("plan") or {}
    plan_id = pending.get("plan_id")
    intent = plan.get("intent")

    # ---- 2. only the explicitly-enabled action types execute ----
    if intent not in EXECUTABLE_INTENTS:
        return _blocked(
            cid, "I can prepare that, but this action isn't enabled for execution yet."
        )

    project = plan.get("project") or {}
    project_id = project.get("id")
    changes: List[dict] = plan.get("changes") or []
    if not project_id or not changes:
        return _blocked(cid, "That plan is incomplete — please ask me again.")

    # ---- 3. structural validation of every proposed change ----
    for c in changes:
        if c.get("op") not in _ALLOWED_OPS:
            return _blocked(cid, "That plan contains an action I can't run — please ask me again.")
        if not c.get("entity_id"):
            return _blocked(cid, "That plan is missing a talent id — please ask me again.")
        if c["op"] in ("update", "add"):
            pv = _normalise_stage(c.get("proposed_value"))
            if pv not in PIPELINE_STAGES:
                return _blocked(cid, "That plan targets a stage I don't recognise — please ask me again.")
    if intent == "mark_talent_status":
        targets = {_normalise_stage(c.get("proposed_value")) for c in changes}
        if targets - {"not_available", "not_interested"} or len(targets) != 1:
            return _blocked(cid, "That plan targets an unexpected stage — please ask me again.")

    # ---- SA-2: expiry (checked before idempotency, same relative position
    # confirm_ai_response already uses) ----
    if _is_expired(pending):
        return _blocked(cid, "That plan has expired. Please ask me again so I can prepare a fresh one.",
                        need_refresh=True)

    # ---- 4. idempotency: same plan already executed? ----
    if plan_id:
        prior = await audit.find_completed(plan_id)
        if prior:
            return _replay(cid, plan, prior, await build_overview(user))

    # ---- 5. live entity + stale check ----
    proj_doc = await RDB.projects.find_one({"id": project_id}, {"_id": 0, "id": 1, "brand_name": 1})
    if not proj_doc:
        return _blocked(cid, f'The project "{project.get("label")}" no longer exists.', need_refresh=True)
    project_label = proj_doc.get("brand_name") or project.get("label")

    talent_ids = [c["entity_id"] for c in changes]
    tdocs = await RDB.talents.find(
        {"id": {"$in": talent_ids}}, {"_id": 0, "id": 1, "name": 1}
    ).to_list(len(talent_ids))
    name_by_id = {d["id"]: d.get("name") or "Unknown" for d in tdocs}

    rows = await RDB.casting_pipeline.find(
        {"project_id": project_id, "talent_id": {"$in": talent_ids}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(5000)
    live_stage = {r["talent_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in rows}
    in_pipeline = set(live_stage)

    outcomes: List[Dict[str, Any]] = []
    move_ids: List[str] = []
    add_ids: List[str] = []
    remove_ids: List[str] = []
    target_stage = _normalise_stage(changes[0].get("proposed_value")) if intent == "mark_talent_status" else None

    for c in changes:
        tid = c["entity_id"]
        label = c.get("entity_label") or name_by_id.get(tid) or tid
        if tid not in name_by_id:
            outcomes.append(_o(tid, label, "failed", reason="talent record not found"))
            continue
        op = c["op"]
        if op == "update":
            cur = live_stage.get(tid)
            want_from = _normalise_stage(c.get("current_value"))
            proposed = _normalise_stage(c.get("proposed_value"))
            if tid not in in_pipeline:
                outcomes.append(_o(tid, label, "failed", reason="no longer in this project's pipeline"))
            elif cur == proposed:
                outcomes.append(_o(tid, label, "unchanged", frm=cur, to=cur,
                                   reason=f"already {STAGE_LABELS.get(cur, cur)}"))
            elif cur != want_from:
                outcomes.append(_o(
                    tid, label, "stale", frm=cur,
                    reason=f"status changed to {STAGE_LABELS.get(cur, cur)} after I prepared the plan",
                ))
            else:
                move_ids.append(tid)
                outcomes.append(_o(tid, label, "_pending_move", frm=cur))
        elif op == "add":
            if tid in in_pipeline:
                outcomes.append(_o(tid, label, "unchanged",
                                   reason=f"already in the pipeline ({STAGE_LABELS.get(live_stage[tid], live_stage[tid])})"))
            else:
                add_ids.append(tid)
                outcomes.append(_o(tid, label, "_pending_add"))
        elif op == "remove":
            if tid not in in_pipeline:
                outcomes.append(_o(tid, label, "unchanged", reason="not in the pipeline"))
            else:
                remove_ids.append(tid)
                outcomes.append(_o(tid, label, "_pending_remove", frm=live_stage.get(tid)))

    actionable = move_ids + add_ids + remove_ids
    if not actionable:
        overview = await build_overview(user)
        # everything was stale/unchanged/failed — nothing ran.
        stale = [o for o in outcomes if o["status"] == "stale"]
        if stale:
            msg = (
                f"This action is no longer current. "
                + "; ".join(f"{o['talent_label']}'s {o['reason']}" for o in stale)
                + ". Ask me again and I'll prepare a fresh plan."
            )
            state = "stale"
        else:
            msg = "Nothing to do — " + "; ".join(
                f"{o['talent_label']}: {o['reason']}" for o in outcomes
            ) + "."
            state = "executed"
        await audit.record(
            plan_id=plan_id or "-", conversation_id=cid, user=user, action_type=intent,
            project_id=project_id, project_label=project_label, target_stage=target_stage,
            outcomes=outcomes, executed=False,
        )
        return _result(cid, state, msg, plan, intent, project_id, project_label, target_stage, outcomes, overview)

    # ---- SA-2: recheck expiry immediately before the real mutation. Defense
    # in depth for the (currently synchronous, sub-second) gap between the
    # entry check above and the actual write — never a functional no-op,
    # since nothing above this point has mutated anything yet. ----
    if _is_expired(pending):
        overview = await build_overview(user)
        return _result(cid, "blocked", "That plan expired while I was processing it. Please ask me again.",
                       plan, intent, project_id, project_label, target_stage, outcomes, overview)

    # ---- 6. execute the actionable subset via the canonical functions ----
    exec_error: Optional[str] = None
    try:
        if move_ids:
            await bulk_move_by_talent_ids(project_id, move_ids, target_stage)
        if add_ids:
            await add_talents_to_pipeline(project_id, add_ids, _ADD_STAGE)
        if remove_ids:
            await bulk_delete_pipeline(project_id, PipelineBulkDeleteIn(talent_ids=remove_ids), user)
    except Exception as exc:  # never fabricate success
        logger.exception("simple_assistant execution failed mid-flight")
        exec_error = str(exc)

    # ---- 7. post-verify against fresh DB state ----
    rows2 = await RDB.casting_pipeline.find(
        {"project_id": project_id, "talent_id": {"$in": actionable}},
        {"_id": 0, "talent_id": 1, "stage": 1},
    ).to_list(5000)
    after_stage = {r["talent_id"]: (_normalise_stage(r.get("stage")) or r.get("stage")) for r in rows2}

    for o in outcomes:
        tid = o["talent_id"]
        if o["status"] == "_pending_move":
            if after_stage.get(tid) == target_stage:
                o.update(status="success", to=target_stage)
            else:
                o.update(status="failed", to=after_stage.get(tid),
                         reason=exec_error or "the change did not take effect")
        elif o["status"] == "_pending_add":
            if tid in after_stage:
                o.update(status="success", to=_ADD_STAGE)
            else:
                o.update(status="failed", reason=exec_error or "the talent was not added")
        elif o["status"] == "_pending_remove":
            if tid not in after_stage:
                o.update(status="success", to="removed")
            else:
                o.update(status="failed", to=after_stage.get(tid),
                         reason=exec_error or "the talent was not removed")

    await audit.record(
        plan_id=plan_id or "-", conversation_id=cid, user=user, action_type=intent,
        project_id=project_id, project_label=project_label, target_stage=target_stage,
        outcomes=outcomes, executed=True, error=exec_error,
    )

    overview = await build_overview(user)
    n_ok = sum(1 for o in outcomes if o["status"] == "success")
    n_total = len(outcomes)
    msg = _done_message(intent, project_label, target_stage, outcomes, n_ok, n_total)
    return _result(cid, "executed", msg, plan, intent, project_id, project_label,
                   target_stage, outcomes, overview)


# --------------------------------------------------------------------------
def _o(tid, label, status, *, frm=None, to=None, reason=None):
    return {"talent_id": tid, "talent_label": label, "status": status,
            "from": frm, "to": to, "reason": reason}


def _clean_outcomes(outcomes):
    # collapse leftover _pending_* (shouldn't happen) into failed
    for o in outcomes:
        if o["status"].startswith("_pending"):
            o["status"] = "failed"
            o.setdefault("reason", "not executed")
    return outcomes


def _counts(outcomes):
    c: Dict[str, int] = {}
    for o in outcomes:
        c[o["status"]] = c.get(o["status"], 0) + 1
    return c


def _result(cid, state, message, plan, intent, project_id, project_label, target_stage, outcomes, overview):
    _clean_outcomes(outcomes)
    env = _envelope(
        conversation_id=cid, state=state, intent=intent, message=message, plan=plan,
    )
    env["result"] = {
        "intent": intent,
        "project": {"id": project_id, "label": project_label},
        "target_stage": target_stage,
        "target_stage_label": STAGE_LABELS.get(target_stage, target_stage) if target_stage else None,
        "outcomes": [
            {**o, "from_label": STAGE_LABELS.get(o["from"], o["from"]) if o["from"] else None,
             "to_label": STAGE_LABELS.get(o["to"], o["to"]) if o["to"] else None}
            for o in outcomes
        ],
        "counts": _counts(outcomes),
    }
    env["overview"] = overview
    env["need_refresh"] = False
    return env


def _done_message(intent, project_label, target_stage, outcomes, n_ok, n_total):
    head = "Done." if n_ok == n_total else (
        f"I couldn't complete every change." if n_ok else "No changes were made."
    )
    lines = [head, project_label]
    for o in outcomes:
        if o["status"] == "success":
            lines.append(f"{o['talent_label']} — {STAGE_LABELS.get(o['to'], o['to'])}")
        elif o["status"] == "unchanged":
            lines.append(f"{o['talent_label']} — unchanged ({o['reason']})")
        elif o["status"] == "stale":
            lines.append(f"{o['talent_label']} — unchanged; {o['reason']}")
        else:
            lines.append(f"{o['talent_label']} — failed; {o['reason']}")
    lines.append(f"{n_ok} of {n_total} change{'s' if n_total != 1 else ''} completed successfully.")
    return "\n".join(lines)


def _replay(cid, plan, prior_doc, overview):
    sa = prior_doc.get("sa_action") or {}
    outcomes = [
        {"talent_id": t.get("talent_id"), "talent_label": t.get("talent_label"),
         "status": t.get("outcome"), "from": t.get("previous_state"), "to": t.get("new_state"),
         "reason": t.get("reason")}
        for t in (sa.get("talents") or [])
    ]
    return _result(
        cid, "executed",
        "That change was already applied — here's the outcome again. Nothing was done twice.",
        plan, sa.get("action_type"), sa.get("project_id"), sa.get("project_label"),
        sa.get("target_stage"), outcomes, overview,
    )
