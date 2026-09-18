"""Controlled media attachment for Simple Assistant (Phase 4B).

confirm_and_attach() executes an HMAC-signed SubmissionPlan:

  verify signature → only ATTACH → idempotency check
  → RE-RESOLVE talent (authoritative) / project / submission / media
    against LIVE data
  → verify each proposed library media id still exists + belongs to this talent
  → skip anything already attached (live check)
  → hand the valid remainder to the canonical submission service
      routers.submissions.attach_existing_talent_media_to_submission
  → verify the write → audit → per-item result + fresh overview

No direct DB write from this module (audit collection aside). No Cloudinary
upload, no WhatsApp job, no link creation.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from agents.modules.media_assignment import resolve_authoritative_talent_for_upload
from routers.submissions import (
    attach_existing_talent_media_to_submission,
    build_prefill_media,
    ingest_new_audition_media_to_submission,
)
from simple_assistant import EXECUTION_DISABLED_MESSAGE, audit, is_execution_enabled
from simple_assistant.commands import _envelope, _verified_pending, build_overview
from simple_assistant.readonly_db import RDB
from simple_assistant.submission_plan import ATTACH, INGEST

logger = logging.getLogger(__name__)


def _blocked(cid, msg, *, need_refresh=False):
    e = _envelope(conversation_id=cid, state="blocked", message=msg)
    e["need_refresh"] = need_refresh
    return e


async def confirm_and_attach(*, conversation_id, context, user) -> dict:
    cid = conversation_id or (context or {}).get("conversation_id") or "sa_unknown"

    pending = _verified_pending(context)
    if pending is None:
        return _blocked(cid, "I couldn't verify that submission plan. Please ask me again so I can prepare a fresh one.", need_refresh=True)
    if pending.get("kind") != "confirm_submission":
        return _blocked(cid, "There's no pending submission update to confirm.")

    # ---- SA-execution-gate: independent kill-switch, checked before ANY
    # further processing — idempotency, live re-resolution, and the
    # canonical attach call are all below this and never reached while
    # execution is disabled. Not recorded to the audit log, so a still-valid
    # plan can be confirmed again once enabled. ----
    if not is_execution_enabled():
        return _blocked(cid, EXECUTION_DISABLED_MESSAGE)

    plan = pending.get("sub_plan") or {}
    plan_id = pending.get("plan_id")

    if plan.get("action_type") != ATTACH:
        return _blocked(cid, "That plan is preview-only — there's nothing to attach.")

    talent = plan.get("talent") or {}
    project = plan.get("project") or {}
    sub_id = plan.get("submission_id")
    proposed = plan.get("proposed") or []
    if not (talent.get("id") and project.get("id") and sub_id and proposed):
        return _blocked(cid, "That plan is incomplete — please ask me again.")

    # ---- idempotency ----
    if plan_id:
        prior = await audit.find_completed(plan_id)
        if prior:
            return _replay(cid, plan, prior, await build_overview(user))

    # ---- live re-resolution ----
    proj_doc = await RDB.projects.find_one({"id": project["id"]}, {"_id": 0, "id": 1})
    if not proj_doc:
        return _blocked(cid, f'The project "{project.get("label")}" no longer exists.', need_refresh=True)

    # the submission the plan named MUST still be this (project, talent)'s own
    sub = await RDB.submissions.find_one({"id": sub_id, "project_id": project["id"]}, {"_id": 0})
    if not sub:
        return _blocked(cid, "That submission no longer exists.", need_refresh=True)

    # authoritative talent, re-derived from the project's own submission —
    # never trusted from the payload.
    auth = await resolve_authoritative_talent_for_upload(project["id"], [talent["id"]])
    auth_tid = auth.talent_id if auth.ok else (sub.get("talent_id") or talent["id"])
    auth_talent = await RDB.talents.find_one({"id": auth_tid}, {"_id": 0, "id": 1, "name": 1, "email": 1, "media": 1})
    if not auth_talent:
        return _blocked(cid, "The talent profile behind this submission is no longer available.", need_refresh=True)

    # ---- verify each proposed library id: exists + belongs to THIS talent ----
    library = await build_prefill_media(auth_talent, email=auth_talent.get("email"))
    lib_ids = {m.get("id") for m in library if m.get("id")}
    attached_src_now = {m.get("source_talent_media_id") for m in (sub.get("media") or []) if m.get("source_talent_media_id")}

    outcomes: List[Dict[str, Any]] = []
    to_attach: List[str] = []
    for pm in proposed:
        sid = pm.get("source_id")
        label = pm.get("label")
        if not sid or sid not in lib_ids:
            # tampered / stale / not this talent's media
            outcomes.append({"label": label, "status": "failed", "reason": "media not available on this talent's profile"})
            continue
        if sid in attached_src_now:
            outcomes.append({"label": label, "status": "already_attached"})
            continue
        to_attach.append(sid)
        outcomes.append({"label": label, "status": "_pending", "source_id": sid})

    if not to_attach:
        overview = await build_overview(user)
        stale = [o for o in outcomes if o["status"] == "already_attached"]
        if stale and len(stale) == len(outcomes):
            msg = ("This submission changed after I prepared the plan — "
                   + ", ".join(o["label"] for o in stale)
                   + (" is" if len(stale) == 1 else " are") + " already attached. I've refreshed the submission; nothing was attached again.")
            state = "stale"
        else:
            msg = "Nothing to attach — " + "; ".join(
                f"{o['label']}: {o.get('reason') or o['status']}" for o in outcomes
            ) + "."
            state = "blocked"
        await audit.record_submission(
            plan_id=plan_id or "-", conversation_id=cid, user=user,
            project_id=project["id"], project_label=project.get("label"),
            talent_id=auth_tid, talent_label=talent.get("label"),
            submission_id=sub_id, outcomes=outcomes, executed=False,
        )
        return _result(cid, state, msg, plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)

    # ---- canonical attach ----
    svc = await attach_existing_talent_media_to_submission(
        submission=sub,
        requested_source_media_ids=to_attach,
        admin=user,
        authoritative_talent_id=auth_tid,
    )

    attached_ids = {a["source_talent_media_id"] for a in svc.get("attached", [])}
    skipped_ids = set(svc.get("skipped_already_attached", []))
    notfound_ids = set(svc.get("not_found", [])) | set(svc.get("invalid_category", []))
    fresh_sub = svc.get("submission") or sub
    fresh_src = {m.get("source_talent_media_id") for m in (fresh_sub.get("media") or []) if m.get("source_talent_media_id")}

    for o in outcomes:
        if o["status"] != "_pending":
            continue
        sid = o["source_id"]
        if sid in attached_ids and sid in fresh_src:
            o.update(status="attached")
        elif sid in skipped_ids:
            o.update(status="already_attached")
        elif sid in notfound_ids:
            o.update(status="failed", reason="not found on this talent's profile")
        else:
            o.update(status="failed", reason=svc.get("error") or "the attach did not take effect")

    n_ok = sum(1 for o in outcomes if o["status"] == "attached")
    n_total = len(outcomes)
    state = "attached" if n_ok == n_total else ("partial" if n_ok else "failed")

    await audit.record_submission(
        plan_id=plan_id or "-", conversation_id=cid, user=user,
        project_id=project["id"], project_label=project.get("label"),
        talent_id=auth_tid, talent_label=talent.get("label"),
        submission_id=sub_id, outcomes=outcomes, executed=True,
    )

    overview = await build_overview(user)
    msg = _done_message(project.get("label"), outcomes, n_ok, n_total)
    return _result(cid, state, msg, plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)


# --------------------------------------------------------------------------
def _clean(outcomes):
    for o in outcomes:
        if o["status"] == "_pending":
            o["status"] = "failed"
            o.setdefault("reason", "not executed")
        o.pop("source_id", None)
    return outcomes


def _counts(outcomes):
    c: Dict[str, int] = {}
    for o in outcomes:
        c[o["status"]] = c.get(o["status"], 0) + 1
    return c


def _result(cid, state, message, plan, project, talent_id, talent_label, sub_id, outcomes, overview):
    _clean(outcomes)
    env = _envelope(conversation_id=cid, state=state, intent="submission", message=message)
    env["sub"] = plan
    env["result"] = {
        "action_type": (plan or {}).get("action_type") or "attach_media",
        "project": {"id": project.get("id"), "label": project.get("label")},
        "talent": {"id": talent_id, "label": talent_label},
        "submission_id": sub_id,
        "outcomes": outcomes,
        "counts": _counts(outcomes),
    }
    env["overview"] = overview
    env["need_refresh"] = False
    return env


def _done_message(project_label, outcomes, n_ok, n_total):
    head = "Submission updated." if n_ok == n_total and n_ok else (
        "Submission updated partially." if n_ok else "Nothing was attached."
    )
    lines = [head, project_label or ""]
    for o in outcomes:
        if o["status"] == "attached":
            lines.append(f"✓ {o['label']} — attached")
        elif o["status"] == "already_attached":
            lines.append(f"✓ {o['label']} — already attached")
        else:
            lines.append(f"✕ {o['label']} — failed" + (f" ({o['reason']})" if o.get("reason") else ""))
    lines.append(f"{n_ok} of {n_total} attached.")
    return "\n".join(x for x in lines if x)


def _replay(cid, plan, prior_doc, overview):
    sa = prior_doc.get("sa_action") or {}
    outcomes = sa.get("outcomes") or []
    replayed_state = "attached"
    msg = "This submission update was already applied. Nothing was changed again."
    if sa.get("action_type") == "upload_audition":
        msg = "This audition upload was already applied. The file was not uploaded again."
    return _result(
        cid, replayed_state, msg,
        plan, {"id": sa.get("project_id"), "label": sa.get("project_label")},
        sa.get("talent_id"), sa.get("talent_label"), sa.get("submission_id"), outcomes, overview,
    )


# ==========================================================================
# Phase 4C — ingest a genuinely NEW incoming audition file
# ==========================================================================
def _audit_file(up: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filename": up.get("filename"),
        "size": up.get("size"),
        "content_type": up.get("content_type"),
        "sha256": up.get("sha256"),
        "target_category": up.get("target_category"),
    }


async def confirm_and_ingest(*, conversation_id, context, user, file) -> dict:
    """Execute an HMAC-signed INGEST plan: the audition file's bytes arrive
    now (multipart), are checked against the SHA-256 pinned in the signed
    plan, then handed to the canonical
    ``routers.submissions.ingest_new_audition_media_to_submission``.

    No Cloudinary call from this module — the canonical service owns the
    upload. No WhatsApp job, no link creation, no pipeline mutation.
    """
    cid = conversation_id or (context or {}).get("conversation_id") or "sa_unknown"

    pending = _verified_pending(context)
    if pending is None:
        return _blocked(cid, "I couldn't verify that upload plan. Please ask me again so I can prepare a fresh one.", need_refresh=True)
    if pending.get("kind") != "confirm_upload":
        return _blocked(cid, "There's no pending audition upload to confirm.")

    # ---- SA-execution-gate: independent kill-switch, checked before ANY
    # further processing — in particular before the multipart file body is
    # ever read into memory, let alone reaching Cloudinary or MongoDB. Not
    # recorded to the audit log, so a still-valid plan can be confirmed
    # again once execution is enabled. ----
    if not is_execution_enabled():
        return _blocked(cid, EXECUTION_DISABLED_MESSAGE)

    plan = pending.get("sub_plan") or {}
    plan_id = pending.get("plan_id")
    if plan.get("action_type") != INGEST:
        return _blocked(cid, "That plan is preview-only — there's nothing to upload.")

    up = plan.get("upload") or {}
    talent = plan.get("talent") or {}
    project = plan.get("project") or {}
    sub_id = plan.get("submission_id")
    target_cat = up.get("target_category")
    if not (talent.get("id") and project.get("id") and sub_id and target_cat):
        return _blocked(cid, "That plan is incomplete — please ask me again.")

    # ---- the file must actually be here ----
    if file is None:
        return _blocked(cid, "No file arrived with that confirmation. Attach the audition video and try again.")
    data = await file.read()
    if not data:
        return _blocked(cid, "That file came through empty — nothing was uploaded.")

    # ---- idempotency ----
    if plan_id:
        prior = await audit.find_completed(plan_id)
        if prior:
            return _replay(cid, plan, prior, await build_overview(user))

    # ---- integrity: bytes must match the previewed file ----
    sha = hashlib.sha256(data).hexdigest()
    pinned = up.get("sha256")
    if pinned and pinned != sha:
        return _blocked(cid, "The file that arrived doesn't match the one you previewed. Nothing was uploaded.")

    # ---- live re-resolution ----
    proj_doc = await RDB.projects.find_one({"id": project["id"]}, {"_id": 0, "id": 1})
    if not proj_doc:
        return _blocked(cid, f'The project "{project.get("label")}" no longer exists.', need_refresh=True)
    sub = await RDB.submissions.find_one({"id": sub_id, "project_id": project["id"]}, {"_id": 0})
    if not sub:
        return _blocked(cid, "That submission no longer exists.", need_refresh=True)

    # authoritative talent, re-derived from the project's own submission
    auth = await resolve_authoritative_talent_for_upload(project["id"], [talent["id"]])
    auth_tid = auth.talent_id if auth.ok else (sub.get("talent_id") or talent["id"])

    target_label = up.get("target_label") or target_cat
    file_audit = _audit_file({**up, "sha256": sha})

    # ---- live duplicate check (something may have been uploaded since preview) ----
    for m in (sub.get("media") or []):
        if m.get("content_sha256") == sha:
            overview = await build_overview(user)
            outcomes = [{"label": target_label, "status": "already_uploaded"}]
            await audit.record_submission(
                plan_id=plan_id or "-", conversation_id=cid, user=user,
                project_id=project["id"], project_label=project.get("label"),
                talent_id=auth_tid, talent_label=talent.get("label"),
                submission_id=sub_id, outcomes=outcomes, executed=False,
                action_type="upload_audition", file_meta=file_audit,
            )
            return _result(cid, "stale",
                           "This exact file is already on the submission — nothing was uploaded again.",
                           plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)

    # ---- canonical ingest ----
    svc = await ingest_new_audition_media_to_submission(
        submission=sub,
        file_bytes=data,
        filename=up.get("filename"),
        content_type=up.get("content_type"),
        category=target_cat,
        label=target_label,
        admin=user,
        authoritative_talent_id=auth_tid,
        expected_sha256=sha,
    )

    fresh_sub = svc.get("submission") or sub
    fresh_media = fresh_sub.get("media") or []

    if svc.get("validation_error"):
        overview = await build_overview(user)
        outcomes = [{"label": target_label, "status": "failed", "reason": svc.get("message") or svc["validation_error"]}]
        await audit.record_submission(
            plan_id=plan_id or "-", conversation_id=cid, user=user,
            project_id=project["id"], project_label=project.get("label"),
            talent_id=auth_tid, talent_label=talent.get("label"),
            submission_id=sub_id, outcomes=outcomes, executed=False,
            action_type="upload_audition", file_meta=file_audit,
        )
        return _result(cid, "blocked", svc.get("message") or "That upload was rejected.",
                       plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)

    if svc.get("skipped_duplicate"):
        overview = await build_overview(user)
        outcomes = [{"label": target_label, "status": "already_uploaded"}]
        await audit.record_submission(
            plan_id=plan_id or "-", conversation_id=cid, user=user,
            project_id=project["id"], project_label=project.get("label"),
            talent_id=auth_tid, talent_label=talent.get("label"),
            submission_id=sub_id, outcomes=outcomes, executed=False,
            action_type="upload_audition", file_meta=file_audit,
        )
        return _result(cid, "stale",
                       "This exact file is already on the submission — nothing was uploaded again.",
                       plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)

    attached = svc.get("attached") or []
    ok = bool(attached) and any(
        m.get("id") == attached[0]["submission_media_id"] and m.get("content_sha256") == sha
        for m in fresh_media
    )
    if ok:
        outcomes = [{"label": attached[0]["label"], "status": "uploaded",
                     "submission_media_id": attached[0]["submission_media_id"]}]
        state = "attached"
        msg = _ingest_done_message(project.get("label"), attached[0]["label"], up.get("replaces"))
    else:
        outcomes = [{"label": target_label, "status": "failed", "reason": "the upload did not take effect"}]
        state = "failed"
        msg = "The upload didn't take effect — nothing was attached."

    await audit.record_submission(
        plan_id=plan_id or "-", conversation_id=cid, user=user,
        project_id=project["id"], project_label=project.get("label"),
        talent_id=auth_tid, talent_label=talent.get("label"),
        submission_id=sub_id, outcomes=outcomes, executed=(state == "attached"),
        action_type="upload_audition", file_meta=file_audit,
    )
    overview = await build_overview(user)
    return _result(cid, state, msg, plan, project, auth_tid, talent.get("label"), sub_id, outcomes, overview)


def _ingest_done_message(project_label, label, replaced) -> str:
    verb = "replaced" if replaced else "added"
    return "\n".join(x for x in [
        "Audition uploaded.", project_label or "",
        f"✓ {label} — {verb} on the submission", "1 of 1 uploaded.",
    ] if x)
