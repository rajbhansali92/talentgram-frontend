"""Phase 9 — approve & send a human-reviewed AI WhatsApp response.

confirm_and_send_ai() runs ONLY on an explicit "Approve & Send":

  verify signature → not expired → idempotency
  → re-fetch inbound message + project
  → re-resolve talent + project + destination (Phase 5 rule, never client)
  → rebuild the verified ResponseContext
  → verify the relevant facts did NOT change (fact_hash)
  → re-validate the FINAL text (edited or not) deterministically
  → hand ONE message to the existing WhatsApp Engine
      (simple_assistant.whatsapp_send.queue_send → routers.whatsapp._create_batch_internal)
  → audit (approve_ai_response, executed=True) + the comm send

No new transport, no worker change, no template change, no media, no
pipeline / submission / Cloudinary effect. Text only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from simple_assistant import ai_response, audit, comm, whatsapp_send
from simple_assistant.comm_plan import DEST_GROUP
from simple_assistant.commands import _envelope, _verified_pending, build_overview
from simple_assistant.readonly_db import RDB
from simple_assistant.security import sign

logger = logging.getLogger(__name__)


def _blocked(cid, msg, *, need_refresh=False):
    e = _envelope(conversation_id=cid, state="blocked", intent="communication", message=msg)
    e["need_refresh"] = need_refresh
    return e


def _stale(cid, msg):
    e = _envelope(conversation_id=cid, state="stale", intent="communication", message=msg)
    e["need_refresh"] = True
    return e


async def confirm_and_send_ai(*, conversation_id, context, user, final_text: Optional[str] = None) -> dict:
    cid = conversation_id or (context or {}).get("conversation_id") or "sa_unknown"

    pending = _verified_pending(context)
    if pending is None:
        return _blocked(cid, "I couldn't verify that response. Please ask me to draft a fresh one.", need_refresh=True)
    if pending.get("kind") != "confirm_ai_response":
        return _blocked(cid, "There's no pending AI response to approve.")

    plan_id = pending.get("plan_id")
    inbound_id = pending.get("inbound_id")
    talent_id = pending.get("talent_id")
    project_id = pending.get("project_id")
    if not (plan_id and inbound_id and talent_id):
        return _blocked(cid, "That response plan is incomplete — please ask me again.")

    # ---- expiry ----
    exp = pending.get("expires_at")
    try:
        if exp and datetime.now(timezone.utc) > datetime.fromisoformat(exp):
            return _blocked(cid, "That draft has expired. Ask me to draft a fresh reply.", need_refresh=True)
    except Exception:
        pass

    # ---- idempotency ----
    prior = await audit.find_completed(plan_id)
    if prior:
        return _replay(cid, prior)

    # ---- re-fetch the source message + project (server-authoritative) ----
    msg = await RDB.whatsapp_inbound_messages.find_one({"id": inbound_id}, {"_id": 0})
    if not msg:
        return _blocked(cid, "That inbound message is no longer available.", need_refresh=True)
    tdoc = await RDB.talents.find_one({"id": talent_id}, {"_id": 0, "id": 1, "name": 1})
    if not tdoc:
        return _blocked(cid, "That talent record is no longer available.", need_refresh=True)
    tlabel = tdoc.get("name") or pending.get("talent_label") or "there"

    proj_doc = None
    if project_id:
        proj_doc = await RDB.projects.find_one({"id": project_id}, {"_id": 0})
        if not proj_doc:
            return _stale(cid, "The project for this reply no longer exists. Please review the response again.")

    # ---- rebuild the verified context, same way it was drafted ----
    from simple_assistant import conversation_context as sa_convo
    from simple_assistant import inbound_intelligence as sa_intel
    analysis = sa_intel.analyze(msg, proj_doc)
    # Phase 12 — re-derive the bounded conversation slice at approval time.
    # If new messages have landed since the draft, its digest changes and the
    # fact_hash check below invalidates the stale plan.
    convo = await sa_convo.build(
        talent_id=talent_id, project_id=project_id,
        current_inbound_id=inbound_id, project=proj_doc,
    )
    ctx = ai_response.build_context(analysis=analysis, project=proj_doc, talent_label=tlabel,
                                   conversation_context=convo)

    # ---- STEP 17: the facts (and recent conversation) behind this reply must not have changed ----
    if ai_response.fact_hash(ctx) != pending.get("fact_hash"):
        return _stale(
            cid,
            "The conversation or project information changed since this response was "
            "drafted. Please review the response again.",
        )

    # ---- the FINAL text: edited (client) or the original draft (signed) ----
    text = (final_text or "").strip() or (pending.get("draft_text") or "").strip()
    if not text:
        return _blocked(cid, "There's no response text to send.")

    ok, reasons = ai_response.validate_draft(text, ctx)
    if not ok:
        return _blocked(
            cid,
            "I can't approve that reply:\n- " + "\n- ".join(reasons)
            + "\nNo message was sent. Please edit it and try again.",
        )

    # ---- STEP 20: destination is RE-DERIVED, never from the client ----
    dest, dtype, reason = await comm._talent_own_destination(talent_id)
    if not dest:
        return _blocked(
            cid, f"{tlabel} has no saved WhatsApp group or number — I can't send this reply.",
            need_refresh=True,
        )

    # ---- hand ONE message to the EXISTING WhatsApp Engine ----
    res = await whatsapp_send.queue_send(
        destination=dest,
        destination_type=("whatsapp_group" if dtype == DEST_GROUP else "whatsapp_number"),
        name=tlabel, message=text, media_url=None, admin=user,
    )

    d_hash = ai_response.draft_hash(text)
    if not res.get("ok"):
        await audit.record_ai(
            action_type="approve_ai_response", executed=False, conversation_id=cid, user=user,
            plan_id=plan_id, inbound_id=inbound_id, talent_id=talent_id, talent_label=tlabel,
            project_id=project_id, project_label=(proj_doc or {}).get("brand_name"),
            draft_hash=d_hash, fact_hash=pending.get("fact_hash"), destination=dest,
            batch_id=None, result="send_failed: " + (res.get("reason") or "unknown"),
        )
        return _envelope(
            conversation_id=cid, state="failed", intent="communication",
            message="I couldn't queue the reply with the WhatsApp Engine.\nReason: "
                    + (res.get("reason") or "send failed") + "\nNo message was sent.",
        )

    batch_id = res["batch_id"]
    await audit.record_ai(
        action_type="approve_ai_response", executed=True, conversation_id=cid, user=user,
        plan_id=plan_id, inbound_id=inbound_id, talent_id=talent_id, talent_label=tlabel,
        project_id=project_id, project_label=(proj_doc or {}).get("brand_name"),
        draft_hash=d_hash, fact_hash=pending.get("fact_hash"),
        destination=dest, destination_type=dtype, batch_id=batch_id,
        result="queued", edited=bool(final_text and final_text.strip() and
                                     final_text.strip() != (pending.get("draft_text") or "").strip()),
    )

    overview = await build_overview(user)
    env = _envelope(
        conversation_id=cid, state="queued", intent="communication",
        message=(f'Reply to {tlabel} queued to WhatsApp:\n\n"{text}"\n\n1 item queued.'),
    )
    env["result"] = {
        "action_type": "approve_ai_response",
        "destination": dest, "destination_type": dtype,
        "outcomes": [{"label": "AI reply", "status": "queued", "batch_id": batch_id}],
        "counts": {"queued": 1, "failed": 0, "total": 1},
        "status_token": sign({"plan_id": plan_id, "batch_ids": [batch_id]}),
        "batch_ids": [batch_id],
    }
    env["overview"] = overview
    env["need_refresh"] = False
    return env


def _replay(cid, prior_doc):
    sa = prior_doc.get("sa_action") or {}
    return _envelope(
        conversation_id=cid, state="queued", intent="communication",
        message="This reply was already approved and queued. Nothing was sent again.",
    ) | {"result": {"action_type": "approve_ai_response",
                    "outcomes": [{"label": "AI reply", "status": "queued", "batch_id": sa.get("batch_id")}],
                    "counts": {"queued": 0, "failed": 0, "total": 0},
                    "batch_ids": [sa.get("batch_id")] if sa.get("batch_id") else []},
         "overview": None, "need_refresh": False}
