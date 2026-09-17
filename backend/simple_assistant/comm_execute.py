"""Controlled external communication for Simple Assistant (Phase 4A).

confirm_and_send() executes an HMAC-signed CommunicationPlan:

  verify signature → only enabled action types → idempotency check
  → RE-RESOLVE destination + media from talent_id/project_id/submission_id
    against LIVE data (the plan's URLs/destination are never trusted blindly)
  → hand each item to the existing WhatsApp Engine job queue
    (simple_assistant.whatsapp_send → routers.whatsapp._create_batch_internal)
  → audit → structured per-item result (queued / failed)

The worker delivers asynchronously; comm_status() reconciles the real
per-job outcome the worker writes back.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from simple_assistant import audit, comm, links_adapter, whatsapp_send
from simple_assistant.commands import _envelope, _is_expired, _verified_pending, build_overview
from simple_assistant.comm_plan import (
    DEST_GROUP,
    DEST_NUMBER,
    SEND_MEDIA,
    SEND_PROFILE_LINK,
    SEND_PROJECT_INFO,
)
from simple_assistant.readonly_db import RDB
from simple_assistant.security import sign, verify

logger = logging.getLogger(__name__)

_SENDABLE = {SEND_MEDIA, SEND_PROFILE_LINK, SEND_PROJECT_INFO}


def _blocked(cid, msg, *, need_refresh=False):
    e = _envelope(conversation_id=cid, state="blocked", message=msg)
    e["need_refresh"] = need_refresh
    return e


def _stale(cid, msg, plan):
    e = _envelope(conversation_id=cid, state="stale", intent="communication", message=msg, comm=plan)
    e["need_refresh"] = True
    return e


async def confirm_and_send(*, conversation_id, context, user) -> dict:
    cid = conversation_id or (context or {}).get("conversation_id") or "sa_unknown"

    pending = _verified_pending(context)
    if pending is None:
        return _blocked(cid, "I couldn't verify that communication. Please ask me again so I can prepare a fresh plan.", need_refresh=True)
    if pending.get("kind") != "confirm_comm":
        return _blocked(cid, "There's no pending communication to confirm.")
    plan = pending.get("comm_plan") or {}
    plan_id = pending.get("plan_id")
    action_type = plan.get("action_type")

    if action_type not in _SENDABLE:
        return _blocked(cid, "I can prepare that, but this communication isn't enabled for sending yet.")

    talent = plan.get("talent") or {}
    project = plan.get("project") or {}
    if not talent.get("id"):
        return _blocked(cid, "That plan is incomplete — please ask me again.")

    # ---- SA-2: expiry (checked before idempotency, same relative position
    # confirm_ai_response already uses) ----
    if _is_expired(pending):
        return _blocked(cid, "That plan has expired. Please ask me again so I can prepare a fresh one.",
                        need_refresh=True)

    # ---- idempotency ----
    if plan_id:
        prior = await audit.find_completed(plan_id)
        if prior:
            return _replay(cid, plan, prior)

    # ---- live re-validation ----
    tdoc = await RDB.talents.find_one({"id": talent["id"]}, {"_id": 0, "id": 1, "name": 1})
    if not tdoc:
        return _blocked(cid, f'{talent.get("label")} no longer exists.', need_refresh=True)
    tlabel = tdoc.get("name") or talent["label"]

    # destination — DERIVED SERVER-SIDE, never from the client payload. The
    # signed plan says WHICH resolver to re-run (project casting group vs the
    # talent's own), never the value.
    if action_type == SEND_PROJECT_INFO:
        dest, dtype, reason = await comm._talent_own_destination(talent["id"])
        if not dest:
            return _blocked(cid, f"{tlabel} doesn't have a saved WhatsApp group or number.")
    else:
        if not project.get("id"):
            return _blocked(cid, "That plan is missing its project — please ask me again.")
        pdoc = await RDB.projects.find_one({"id": project["id"]}, {"_id": 0, "id": 1})
        if not pdoc:
            return _blocked(cid, f'The project "{project.get("label")}" no longer exists.', need_refresh=True)
        source = plan.get("destination_source") or "project_casting_group"
        if source == "talent_own":
            dest, dtype, reason = await comm._talent_own_destination(talent["id"])
            if not dest:
                return _blocked(cid, f"{tlabel} no longer has a saved WhatsApp group or number.", need_refresh=True)
        else:
            dest, gerr = await comm._casting_group(project["id"])
            dtype = DEST_GROUP
            if gerr:
                return _blocked(cid, f'{project.get("label")} has no WhatsApp casting group configured.')

    # STEP 13 — destination changed since the preview → refuse, ask again.
    if plan.get("destination") and dest != plan.get("destination"):
        return _stale(
            cid,
            "The send plan is no longer current.\n\n"
            f"The WhatsApp destination changed since the preview was prepared "
            f"(was {plan.get('destination')!r}, now {dest!r}).\n\n"
            "I've refreshed it — please review and confirm again.",
            plan,
        )

    # ---- build the send items, re-resolved from canonical data ----
    items: List[Dict[str, Any]] = []       # [{label, message, media_url}]

    if action_type == SEND_MEDIA:
        # STEP 12.11 — re-derive the submission from (project, authoritative
        # talent); the signed submission_id must still be THAT submission.
        sub2, _auth_tid2, _serr = await comm._resolve_submission(project["id"], [talent["id"]])
        if not sub2 or sub2.get("id") != plan.get("submission_id"):
            return _stale(
                cid,
                "The send plan is no longer current.\n\n"
                f"{tlabel}'s {project.get('label')} submission has changed since the preview.\n\n"
                "I've refreshed it — please review and confirm again.",
                plan,
            )
        sub = sub2
        media_by_id = {m.get("id"): m for m in (sub.get("media") or []) if isinstance(m, dict)}
        changed: List[str] = []
        for mref in plan.get("media") or []:
            live = media_by_id.get(mref.get("media_id"))
            if not live or not live.get("url"):
                changed.append(mref.get("label"))
                continue
            # content fingerprint — a replaced take keeps neither id nor public_id
            if mref.get("public_id") and live.get("public_id") and live.get("public_id") != mref.get("public_id"):
                changed.append(mref.get("label"))
                continue
            if mref.get("url") and live.get("url") != mref.get("url"):
                changed.append(mref.get("label"))
                continue
            items.append({"label": mref.get("label"), "media_url": live["url"],
                          "message": f"{tlabel} — {project.get('label')} · {mref.get('label')}"})
        if changed:
            # STEP 13 — any media changed → refuse the whole send, ask again.
            noun = "has" if len(changed) == 1 else "have"
            return _stale(
                cid,
                "The send plan is no longer current.\n\n"
                f"{', '.join(changed)} {noun} changed since the preview was prepared.\n\n"
                "I've refreshed the submission — please review and confirm again.",
                plan,
            )
        if not items:
            return _blocked(cid, "There's no media left to send.")

    elif action_type == SEND_PROFILE_LINK:
        link = await links_adapter.best_profile_link(talent["id"])
        if not link:
            return _blocked(cid, f"{tlabel} no longer has a generated profile link.", need_refresh=True)
        items.append({"label": "Talentgram profile", "media_url": None,
                      "message": f"{tlabel} — Talentgram profile\n{link['url']}"})

    else:  # SEND_PROJECT_INFO
        pfull = await RDB.projects.find_one({"id": project["id"]}, {"_id": 0}) or {}
        items.append({"label": "Project details", "media_url": None,
                      "message": comm._project_info_message(pfull, tlabel)})

    # ---- SA-2: recheck expiry immediately before the real send. Defense in
    # depth for the (currently synchronous, sub-second) gap between the entry
    # check above and the actual send — never a functional no-op, since
    # nothing above this point has queued anything yet. ----
    if _is_expired(pending):
        return _blocked(cid, "That plan expired while I was processing it. Please ask me again.",
                        need_refresh=True)

    # ---- hand each item to the existing WhatsApp Engine ----
    outcomes: List[Dict[str, Any]] = []
    batch_ids: List[str] = []
    for it in items:
        if it.get("error"):
            outcomes.append({"label": it["label"], "status": "failed", "reason": it["error"]})
            continue
        res = await whatsapp_send.queue_send(
            destination=dest, destination_type=("whatsapp_group" if dtype == DEST_GROUP else "whatsapp_number"),
            name=tlabel, message=it["message"], media_url=it["media_url"], admin=user,
        )
        if res.get("ok"):
            batch_ids.append(res["batch_id"])
            outcomes.append({"label": it["label"], "status": "queued", "batch_id": res["batch_id"]})
        else:
            outcomes.append({"label": it["label"], "status": "failed", "reason": res.get("reason") or "send failed"})

    n_ok = sum(1 for o in outcomes if o["status"] == "queued")
    n_total = len(outcomes)
    state = "queued" if n_ok == n_total else ("partial" if n_ok else "failed")

    await audit.record_comm(
        plan_id=plan_id or "-", conversation_id=cid, user=user, action_type=action_type,
        project_id=project.get("id"), project_label=project.get("label"),
        talent_id=talent["id"], talent_label=tlabel,
        destination=dest, destination_type=dtype,
        media_ids=[m.get("media_id") for m in (plan.get("media") or [])],
        outcomes=outcomes, batch_ids=batch_ids, executed=(n_ok > 0),
    )

    if state == "failed":
        msg = f"I couldn't send {tlabel}'s {items[0]['label'] if items else 'message'} to {plan.get('destination_label')}.\n"
        msg += "Reason: " + "; ".join(o.get("reason") or "send failed" for o in outcomes if o["status"] == "failed")
        msg += "\nNo successful send was recorded."
    elif state == "partial":
        lines = [f"{o['label']} — {'queued' if o['status']=='queued' else 'failed'}" for o in outcomes]
        msg = "\n".join(lines) + f"\nI couldn't complete the entire set — {n_ok} of {n_total} queued to WhatsApp."
    else:
        lines = [f"{o['label']} — queued" for o in outcomes]
        msg = "\n".join(lines) + f"\n{n_ok} item{'s' if n_ok != 1 else ''} queued to the WhatsApp Engine for {plan.get('destination_label')}."

    env = _envelope(conversation_id=cid, state=state, intent="communication", message=msg, comm=plan)
    env["result"] = {
        "action_type": action_type,
        "destination": dest, "destination_type": dtype,
        "destination_label": plan.get("destination_label"),
        "outcomes": outcomes, "counts": {"queued": n_ok, "failed": n_total - n_ok, "total": n_total},
        "status_token": sign({"plan_id": plan_id, "batch_ids": sorted(batch_ids)}) if batch_ids else None,
        "batch_ids": batch_ids,
    }
    env["overview"] = await build_overview(user)
    env["need_refresh"] = False
    return env


async def comm_status(*, plan_id: str, batch_ids: List[str], status_token: str) -> dict:
    """Reconcile the worker's real per-job outcome. `status_token` is the
    HMAC the confirm response issued — the client cannot ask about
    arbitrary batches."""
    if not verify({"plan_id": plan_id, "batch_ids": sorted(batch_ids)}, status_token):
        return {"error": "invalid token"}
    data = await whatsapp_send.send_status(batch_ids)
    by_batch: Dict[str, str] = {}
    for j in data["jobs"]:
        by_batch[j["batch_id"]] = j.get("status") or "pending"
    return {"jobs": data["jobs"], "by_batch": by_batch}


def _replay(cid, plan, prior_doc):
    sa = prior_doc.get("sa_action") or {}
    return _envelope(
        conversation_id=cid, state="queued", intent="communication",
        message="This communication was already sent. Nothing was sent again.",
        comm=plan,
    ) | {"result": {"action_type": sa.get("action_type"), "outcomes": sa.get("outcomes") or [],
                    "counts": {"queued": 0, "failed": 0, "total": 0}, "batch_ids": sa.get("batch_ids") or []},
         "overview": None, "need_refresh": False}
