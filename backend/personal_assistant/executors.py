"""Personal Assistant — executors. An EXPLICIT ALLOWLIST of existing operations, each called in-process with
the authenticated user as the actor, so the existing validation, permissions, queue and worker rules apply
unchanged:

  add    routers.casting_pipeline.bulk_add_talents_to_projects   (Global Talent's "Add to Project")
  move   routers.casting_pipeline.bulk_move_by_talent_ids        (Casting Pipeline bulk move)
  send   routers.whatsapp.send_casting_call                      (the casting-call / template send)
  share  routers.whatsapp._create_batch_internal                 (existing batch/job queue, "custom" template)
  lookup / list  read-only queries over talents / pipeline / submissions

Nothing here talks to WhatsApp directly: a send only ever creates jobs in the existing queue, and every
delivery state shown later is read back from those persisted jobs — "queued" is never reported as "sent".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from core import active_only, db
from personal_assistant import resolve as R
from personal_assistant import store as S
from personal_assistant.planner import stage_label

logger = logging.getLogger(__name__)


def _ids(entries: List[dict]) -> List[str]:
    return [e["id"] for e in entries if e.get("id")]


def _detail(exc: HTTPException) -> str:
    d = exc.detail
    return d if isinstance(d, str) else str(d)


# ------------------------------------------------------------------------------------------------- workers
async def worker_id_for_hint(hint: Optional[str]) -> Optional[str]:
    """"worker 2" (as typed) → the registered worker whose label matches. Only a hint — the id is validated
    (registered, sending enabled, connected) again at send time."""
    if not hint:
        return None
    wanted = hint.strip().lower()
    for w in await db.whatsapp_workers.find({}, {"_id": 0, "id": 1, "label": 1}).to_list(50):
        if (w.get("label") or "").strip().lower() == wanted:
            return w["id"]
    return None


async def workers_snapshot() -> List[dict]:
    """Live worker availability from the existing registry + session state (never hard-coded)."""
    out = []
    for w in await db.whatsapp_workers.find({}, {"_id": 0, "id": 1, "label": 1, "sending_enabled": 1}).to_list(50):
        sess = await db.whatsapp_sessions.find_one({"id": w["id"]}, {"_id": 0, "status": 1}) or {}
        ready = w.get("sending_enabled") is True and sess.get("status") == "authenticated"
        out.append({"id": w["id"], "label": w.get("label") or w["id"], "ready": ready,
                    "status": "Connected" if ready else ("Sending off" if w.get("sending_enabled") is not True else {"qr_pending": "Scan QR"}.get(sess.get("status"), "Disconnected"))})
    return out


# ------------------------------------------------------------------------------------------------- reads
async def _history(tid: str) -> dict:
    rows = await db.casting_pipeline.find({"talent_id": tid}, {"_id": 0, "project_id": 1, "stage": 1, "created_at": 1}).sort("created_at", -1).to_list(60)
    subs = await db.submissions.find({"talent_id": tid}, {"_id": 0, "project_id": 1, "status": 1, "created_at": 1}).sort("created_at", -1).to_list(60)
    pids = list({r["project_id"] for r in rows} | {s["project_id"] for s in subs if s.get("project_id")})
    names = {p["id"]: p for p in await db.projects.find({"id": {"$in": pids}}, {"_id": 0, "id": 1, "brand_name": 1, "status": 1}).to_list(len(pids) or 1)}
    from routers.casting_pipeline import _normalise_stage
    return {
        "projects": [{"project_id": r["project_id"], "project": (names.get(r["project_id"]) or {}).get("brand_name") or "(deleted project)",
                      "project_status": (names.get(r["project_id"]) or {}).get("status"), "stage": stage_label(_normalise_stage(r.get("stage"))),
                      "since": (r.get("created_at") or "")[:10]} for r in rows],
        "submissions": [{"project_id": s.get("project_id"), "project": (names.get(s.get("project_id")) or {}).get("brand_name") or "(deleted project)",
                         "status": s.get("status"), "date": (s.get("created_at") or "")[:10]} for s in subs],
    }


async def _profile_card(tid: str, label: str, with_media_link: bool = False) -> dict:
    from agents.modules.talentgram_fetcher import _find_talent_portfolio_link
    from routers.talent_media import resolve_media_link
    from routers.roster import video_watch_url, _thumb_url, _roster_images
    t = await db.talents.find_one({"id": tid}, {"_id": 0, "name": 1, "age": 1, "height": 1, "gender": 1, "location": 1, "instagram_handle": 1, "skills": 1,
                                                 "media": 1, "cover_media_id": 1, "cover_url": 1, "cover_thumbnail_url": 1, "status": 1}) or {}
    card = (await R.talent_cards([tid])).get(tid) or {"id": tid, "label": label, "sublabel": "", "thumb": None}
    imgs = _roster_images(t)
    photos = [m for cat in ("indian", "western", "portfolio") for m in imgs[cat]]
    video = next((m for m in t.get("media") or [] if m.get("category") == "video" and m.get("url") and m.get("client_visible") is not False and m.get("internal_only") is not True), None)
    # resolve_media_link mints the talent's opaque link on first use — a write — so only an EXPLICIT media-link /
    # photos request does that; a plain profile / history lookup never changes data.
    link = await resolve_media_link(tid) if with_media_link else None
    return {**card, "profile_path": f"/admin/talents/{tid}", "skills": (t.get("skills") or [])[:8],
            "photo_count": len(photos), "photos": [{"thumb": _thumb_url(m), "url": m["url"]} for m in photos[:6]],
            "video_url": video_watch_url(video) if video else None,
            "media_link": (link[1] if link else None), "media_link_available": bool(link and link[1]), "media_link_requested": with_media_link,
            "portfolio_url": await _find_talent_portfolio_link(tid, label)}


async def run_lookup(task: dict) -> dict:
    p = task["params"]
    kind = p.get("lookup")
    cards = []
    for e in p.get("talents") or []:
        tid, label = e["id"], e["label"]
        c = await _profile_card(tid, label, with_media_link=kind in ("media_link", "media"))
        if kind in ("history", "projects", "submissions", "tested"):
            h = await _history(tid)
            c["history"] = h
            tested = [x for x in h["projects"] if x["stage"] == "Already Tested"]
            c["tested_projects"] = tested
            if kind == "tested":
                # "Tested" is the pipeline stage Already Tested. A talent can have submitted for many projects without any being
                # marked that way, so an empty answer must not be silent: also list the projects she has submitted for (drafts
                # excluded; one row per project, newest first — _history is already newest first).
                subs: dict = {}
                for x in h["submissions"]:
                    if x.get("status") not in (None, "", "draft") and x.get("project_id") not in subs:
                        subs[x.get("project_id")] = {"project": x["project"], "status": x["status"], "date": x.get("date")}
                c["submitted_projects"] = list(subs.values())
        cards.append(c)
    return {"kind": kind, "cards": cards}


async def run_list(task: dict) -> dict:
    from routers.casting_pipeline import _normalise_stage
    p = task["params"]
    stage = p.get("stage")
    groups = []
    for e in p.get("projects") or []:
        rows = await db.casting_pipeline.find({"project_id": e["id"], "stage": {"$in": [stage, "sent"] if stage == "approved" else [stage]}}, {"_id": 0, "talent_id": 1}).to_list(500)
        cards = await R.talent_cards([r["talent_id"] for r in rows][:60])
        groups.append({"project_id": e["id"], "project": e["label"], "count": len(rows), "talents": list(cards.values())[:40]})
    return {"stage": stage_label(stage), "groups": groups}


# ---------------------------------------------------------------------------------------- consequential
async def _run_add(user: dict, task: dict) -> dict:
    """The existing bulk add, called ONE PROJECT AT A TIME so every project has its own outcome (a failure in one never hides
    the others, and a re-run only creates what is still missing — the add skips pairs already there). Nothing is reported as
    added unless the pipeline row is actually there afterwards."""
    from routers.casting_pipeline import BulkProjectAddIn, bulk_add_talents_to_projects
    p = task["params"]
    tids = _ids(p["talents"])
    outcomes: List[dict] = []
    for proj in p["projects"]:
        before = {r["talent_id"] for r in await db.casting_pipeline.find({"project_id": proj["id"], "talent_id": {"$in": tids}}, {"_id": 0, "talent_id": 1}).to_list(len(tids) + 5)}
        reason = None
        try:
            await bulk_add_talents_to_projects(BulkProjectAddIn(project_ids=[proj["id"]], talent_ids=tids), admin=user)
        except HTTPException as exc:
            reason = _detail(exc)
        after = {r["talent_id"] for r in await db.casting_pipeline.find({"project_id": proj["id"], "talent_id": {"$in": tids}}, {"_id": 0, "talent_id": 1}).to_list(len(tids) + 5)}
        for t in p["talents"]:
            st = "already_there" if t["id"] in before else ("added" if t["id"] in after else "failed")
            row = {"talent_id": t["id"], "name": t["label"], "project": proj["label"], "status": st}
            if st == "failed":
                row["reason"] = reason or "It wasn't added to the pipeline."
            outcomes.append(row)
    added = sum(1 for o in outcomes if o["status"] == "added")
    already = sum(1 for o in outcomes if o["status"] == "already_there")
    failed = [o for o in outcomes if o["status"] == "failed"]
    st = S.COMPLETED if not failed else (S.PARTIAL if (added or already) else S.FAILED)
    err = None
    if failed:
        bad = ", ".join(dict.fromkeys(f"{o['project']}" for o in failed))
        err = f"Couldn't add everyone to: {bad}" + (f" — {failed[0].get('reason')}" if failed[0].get("reason") else "") + ". What was added stays added; retry to finish the rest."
    cc = await db.whatsapp_templates.find_one({"slug": "casting_call"}, {"_id": 0, "id": 1, "name": 1})
    return {"status": st, "error": err, "outcomes": outcomes,
            "result": {"added": added, "already_there": already, "failed": len(failed), "projects": [e["label"] for e in p["projects"]],
                       "suggestions": ([{"type": "send_template", "label": f"Send the {cc['name']}", "template_id": cc["id"]}] if cc and not p.get("prereq_for") else [])}}


async def _run_move(user: dict, task: dict) -> dict:
    from routers.casting_pipeline import PIPELINE_STAGES, _normalise_stage, bulk_move_by_talent_ids
    p = task["params"]
    stage = _normalise_stage(p.get("stage"))
    if stage not in PIPELINE_STAGES:
        return {"status": S.FAILED, "error": f"“{p.get('stage')}” isn't a pipeline stage.", "result": None}
    moved = missing = 0
    outcomes: List[dict] = []
    for proj in p["projects"]:
        rows = await db.casting_pipeline.find({"project_id": proj["id"], "talent_id": {"$in": _ids(p["talents"])}}, {"_id": 0, "talent_id": 1}).to_list(5000)
        present = {r["talent_id"] for r in rows}
        for t in p["talents"]:
            if t["id"] in present:
                outcomes.append({"talent_id": t["id"], "name": t["label"], "project": proj["label"], "status": "moved"})
            else:
                missing += 1
                outcomes.append({"talent_id": t["id"], "name": t["label"], "project": proj["label"], "status": "not_in_project"})
        if present:
            res = await bulk_move_by_talent_ids(proj["id"], sorted(present), stage)
            moved += res.get("moved", 0) if res.get("moved") is not None else 0
    done = sum(1 for o in outcomes if o["status"] == "moved")
    st = S.COMPLETED if not missing else (S.PARTIAL if done else S.FAILED)
    return {"status": st, "outcomes": outcomes,
            "error": None if st != S.FAILED else "None of these talents are in that project's pipeline.",
            "result": {"stage": stage_label(stage), "moved": done, "not_in_project": missing}}


async def _run_send(user: dict, task: dict, worker_id: str) -> dict:
    from routers.whatsapp import CastingCallSendIn, send_casting_call
    p = task["params"]
    payload = CastingCallSendIn(talent_ids=_ids(p["talents"]), project_ids=_ids(p["projects"]), template_id=p["template_id"],
                                worker_id=worker_id, apply_pipeline_move=False)
    res = await send_casting_call(payload, admin=user)
    batches = res.get("batches") or []
    names = {t["id"]: t["label"] for t in p["talents"]}
    not_queued: List[dict] = []
    for b in batches:
        for s in b.get("skipped_recipients") or []:
            not_queued.append({"talent_id": s.get("talent_id"), "name": s.get("name") or names.get(s.get("talent_id")), "project": b.get("project_name"), "status": "not_queued", "reason": s.get("reason")})
        for tid in b.get("not_in_project") or []:
            not_queued.append({"talent_id": tid, "name": names.get(tid), "project": b.get("project_name"), "status": "not_in_project", "reason": "Not in this project's pipeline"})
    for e in res.get("errors") or []:
        not_queued.append({"talent_id": None, "name": None, "project": e.get("project_name") or e.get("project_id"), "status": "not_queued", "reason": str(e.get("error"))})
    return {"status": S.QUEUED, "error": None, "batch_ids": [b["batch_id"] for b in batches], "not_queued": not_queued,
            "result": {"queued": sum(b.get("queued", 0) for b in batches), "not_queued": len(not_queued), "worker_id": res.get("worker_id") or worker_id,
                       "projects": [b.get("project_name") for b in batches]}}


async def _run_share(user: dict, task: dict, worker_id: str) -> dict:
    from agents.modules.talentgram_fetcher import _render_media_link, _render_talent_profile
    from routers.whatsapp import BatchIn, ManualContact, SourceParams, _create_batch_internal
    from routers.whatsapp_workers import require_sendable_worker
    p = task["params"]
    rec = p.get("recipient") or {}
    if p.get("lookup") == "audition":
        return {"status": S.FAILED, "result": None, "error": "Sending an audition isn't available from the assistant yet."}
    custom = await db.whatsapp_templates.find_one({"slug": "custom"}, {"_id": 0, "id": 1})
    if not custom:
        return {"status": S.FAILED, "result": None, "error": "The WhatsApp “Custom Message” template isn't set up, so there is no safe way to send free text."}
    params: Optional[SourceParams] = None
    if rec.get("kind") == "number":
        params = SourceParams(contacts=[ManualContact(name="", phone=rec["phone"])])
    elif rec.get("kind") == "crm":
        from bson import ObjectId
        c = await db.clients.find_one({"_id": ObjectId(rec["id"]), "archived": {"$ne": True}, "deleted": {"$ne": True}}, {"name": 1, "phone_number": 1})
        if not c or not (c.get("phone_number") or "").strip():
            return {"status": S.FAILED, "result": None, "error": f"{rec.get('label') or 'That contact'} has no usable phone number in the CRM any more."}
        params = SourceParams(contacts=[ManualContact(name=c.get("name") or "", phone=c["phone_number"])])
    elif rec.get("kind") == "list":
        params = SourceParams(contact_list_ids=[rec["id"]])
    elif rec.get("kind") == "group":
        params = SourceParams(group_list_ids=[rec["id"]])
    elif rec.get("kind") == "project_group":
        proj = (p.get("projects") or [None])[0]
        doc = await db.projects.find_one({"id": (proj or {}).get("id")}, {"_id": 0, "brand_name": 1, "whatsapp_casting_group_name": 1}) if proj else None
        group = ((doc or {}).get("whatsapp_casting_group_name") or "").strip()
        if not group:
            return {"status": S.FAILED, "result": None, "error": f"{(doc or {}).get('brand_name') or 'That project'} has no WhatsApp casting group set, so there is nowhere to send to."}
        params = SourceParams(contacts=[ManualContact(name=doc.get("brand_name") or "", phone="", whatsapp_group_name=group)])
    if params is None:
        return {"status": S.FAILED, "result": None, "error": "I don't have a resolved recipient for this."}
    blocks, skipped = [], []
    for t in p["talents"]:
        text = await (_render_media_link(t["id"]) if p.get("lookup") == "media_link" else _render_talent_profile(t["id"], t["label"]))
        if "currently unavailable" in text or "No media information" in text:
            skipped.append({"talent_id": t["id"], "name": t["label"], "status": "not_queued", "reason": "Media link is switched off for this talent"})
        else:
            blocks.append(text)
    if not blocks:
        return {"status": S.FAILED, "result": None, "outcomes": skipped, "error": "Nothing shareable: " + "; ".join(f"{s['name']} — {s['reason']}" for s in skipped)}
    await require_sendable_worker(worker_id)
    out = await _create_batch_internal(BatchIn(source_type="SAVED_LISTS" if rec.get("kind") in ("list", "group") else "MANUAL", source_params=params, template_id=custom["id"],
                                               variable_data={"message": "\n\n---\n\n".join(blocks)}, worker_id=worker_id), user)
    return {"status": S.QUEUED, "error": None, "batch_ids": [out["batch"]["id"]], "not_queued": skipped,
            "result": {"queued": len(out["jobs"]), "not_queued": len(skipped), "worker_id": worker_id, "recipient": rec.get("label")}}


# ------------------------------------------------------------------------------------------------- driver
async def dependency_block(owner_id: str, task: dict) -> Optional[str]:
    for dep_id in task.get("depends_on") or []:
        dep = await S.get_task(owner_id, dep_id)
        if not dep:
            return "It depends on a task that no longer exists."
        if dep["status"] in (S.FAILED, S.CANCELLED):
            return f"It depends on “{dep['title']}”, which {dep['status']}."
        if dep["status"] not in (S.COMPLETED, S.PARTIAL, S.QUEUED):
            return f"Waiting for “{dep['title']}” first."
        if dep["status"] == S.PARTIAL and (task.get("params") or {}).get("strict_dependency"):
            return f"“{dep['title']}” only partly worked, so nothing has been sent. Retry that step; this one runs once it has fully succeeded."
    return None


async def run_task(user: dict, task: dict, worker_id: Optional[str]) -> dict:
    """Execute ONE confirmed task. Atomic claim (awaiting_confirmation → running): a second call — double click,
    retry, another tab — finds the claim taken and returns the current task instead of executing again."""
    owner = user["id"]
    kind = task["kind"]
    wdoc = await db.whatsapp_workers.find_one({"id": worker_id}, {"_id": 0, "label": 1}) if worker_id else None
    claimed = await S.update_task(owner, task["id"], {"status": S.RUNNING, "started_at": S.now(), "params.worker_id": worker_id,
                                                      "params.worker_label": (wdoc or {}).get("label") or worker_id}, only_status=[S.AWAITING_CONFIRMATION, "ready"])
    if claimed is None:
        return await S.get_task(owner, task["id"])
    try:
        stale = await stale_reason(claimed)
        if stale:
            out = {"status": S.FAILED, "error": stale, "result": None}
        elif kind == "add":
            out = await _run_add(user, claimed)
        elif kind == "move":
            out = await _run_move(user, claimed)
        elif kind == "send":
            out = await _run_send(user, claimed, worker_id)
        elif kind == "share":
            out = await _run_share(user, claimed, worker_id)
        elif kind == "lookup":
            out = {"status": S.COMPLETED, "result": await run_lookup(claimed), "error": None}
        elif kind == "list":
            out = {"status": S.COMPLETED, "result": await run_list(claimed), "error": None}
        else:
            out = {"status": S.FAILED, "error": "Unsupported action", "result": None}
    except HTTPException as exc:
        out = {"status": S.FAILED, "error": _detail(exc), "result": None}
    except Exception:  # noqa: BLE001
        logger.exception("assistant task %s (%s) crashed", task["id"], kind)
        out = {"status": S.FAILED, "result": None, "uncertain": kind in ("send", "share"),
               "error": "Something went wrong while running this" + (", so I can't tell whether anything was queued. Check WhatsApp Engine → campaign history before sending again." if kind in ("send", "share") else ". Nothing was reported as done.")}
    patch = {"status": out["status"], "error": out.get("error"), "result": out.get("result"), "completed_at": S.now() if out["status"] in S.TERMINAL else None}
    if out.get("uncertain"):
        patch["uncertain"] = True
    for k in ("outcomes", "batch_ids", "not_queued"):
        if out.get(k) is not None:
            patch[k] = out[k]
    updated = await S.update_task(owner, task["id"], patch)
    if kind in S_CONSEQUENTIAL:
        await S.audit(user, "execute", updated, updated["status"])
    return updated


S_CONSEQUENTIAL = {"add", "move", "send", "share"}


async def stale_reason(task: dict) -> Optional[str]:
    """Just before anything runs: is every talent / project the user picked still there? (A selection can go stale between
    the tap and the approval — a talent archived or merged, a project deleted.) Returns the reason, or None if fine."""
    p = task.get("params") or {}
    tids = _ids(p.get("talents") or [])
    if tids:
        docs = await db.talents.find({"id": {"$in": tids}}, {"_id": 0, "id": 1, "name": 1, "status": 1}).to_list(len(tids))
        by = {d["id"]: d for d in docs}
        gone = [t["label"] for t in p["talents"] if t.get("id") and (t["id"] not in by or str(by[t["id"]].get("status") or "").lower() in ("archived", "merged"))]
        if gone:
            return f"{', '.join(gone[:3])} is no longer available (archived, merged or removed) since you picked them, so I did not run this. Ask me again."
    pids = _ids(p.get("projects") or [])
    if pids:
        docs = await db.projects.find({"id": {"$in": pids}}, {"_id": 0, "id": 1, "status": 1}).to_list(len(pids))
        by = {d["id"]: d for d in docs}
        gone = [e["label"] for e in p["projects"] if e.get("id") and (e["id"] not in by or by[e["id"]].get("status") == "deleted")]
        if gone:
            return f"{', '.join(gone[:3])} no longer exists since you picked it, so I did not run this. Ask me again."
    return None


# ------------------------------------------------------------------------------ delivery status read-back
def _delivery(jobs: List[dict]) -> tuple[str, List[dict], str]:
    """Persisted job states → task status. 'sent' = the worker reports it handed the message to WhatsApp
    (verified when the worker confirmed it in the chat); the app has no recipient-device 'delivered' signal."""
    per = [{"talent_id": j.get("talent_id"), "name": j.get("talent_name"), "status": j.get("status"),
            "verified": j.get("verification_status") == "verified", "error": j.get("error_message")} for j in jobs]
    st = [j.get("status") for j in jobs]
    if not st:
        return S.QUEUED, per, "No jobs recorded"
    if all(s == "pending" for s in st):
        return S.QUEUED, per, f"{len(st)} waiting for the worker"
    if any(s in ("pending", "sending") for s in st):
        return S.RUNNING, per, f"{st.count('sent')} sent · {st.count('pending') + st.count('sending')} in progress"
    sent, failed, skipped = st.count("sent"), st.count("failed"), st.count("skipped")
    if sent and not failed and not skipped:
        return S.COMPLETED, per, f"{sent} sent" + (f" ({sum(1 for p in per if p['verified'])} verified)" if any(p["verified"] for p in per) else "")
    if sent:
        return S.PARTIAL, per, f"{sent} sent · {failed} failed · {skipped} skipped"
    if skipped and not failed:
        return S.CANCELLED, per, f"{skipped} cancelled before sending"
    return S.FAILED, per, f"{failed} failed"


STALE_RUNNING_SEC = 120


def _age_seconds(iso: Optional[str]) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return 0.0


async def refresh_task(owner_id: str, task: dict) -> dict:
    """For queued/running sends, read the real per-job states back from the existing queue and persist them.
    A task still "running" with nothing queued after two minutes was interrupted (e.g. the API restarted mid-run):
    it is marked failed AND flagged `uncertain` — we cannot know whether it got as far as queueing — so a send is
    never blindly retried (that could message people twice)."""
    if task["status"] == S.RUNNING and not task.get("batch_ids") and _age_seconds(task.get("started_at")) > STALE_RUNNING_SEC:
        cur = await S.update_task(owner_id, task["id"], {
            "status": S.FAILED, "uncertain": True, "completed_at": S.now(),
            "error": "This was interrupted while it was running (the server restarted), so I can't tell whether it finished."
                     + (" Check WhatsApp Engine → campaign history for this project before sending again." if task["kind"] in ("send", "share") else " It is safe to retry.")},
            only_status=[S.RUNNING])
        return cur or task
    if task["status"] not in S.ACTIVE or not task.get("batch_ids"):
        return task
    jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": task["batch_ids"]}}, {"_id": 0, "talent_id": 1, "talent_name": 1, "status": 1, "verification_status": 1, "error_message": 1}).to_list(2000)
    status, per, detail = _delivery(jobs)
    not_queued = task.get("not_queued") or []
    if status == S.COMPLETED and not_queued:
        status = S.PARTIAL
        detail += f" · {len(not_queued)} not queued"
    patch = {"status": status, "outcomes": per, "status_detail": detail, "completed_at": S.now() if status in S.TERMINAL else None}
    return (await S.update_task(owner_id, task["id"], patch)) or task


async def cancel_task(user: dict, task: dict) -> dict:
    """Cancel before it ran, or cancel a queued send while EVERY job is still pending (the existing batch-cancel
    marks pending/sending jobs skipped — unsafe once a job has started, so then we refuse)."""
    owner = user["id"]
    if task["status"] in (S.NEEDS_CLARIFICATION, S.AWAITING_CONFIRMATION, "ready"):
        cur = await S.update_task(owner, task["id"], {"status": S.CANCELLED, "completed_at": S.now()}, only_status=[S.NEEDS_CLARIFICATION, S.AWAITING_CONFIRMATION, "ready"])
        if cur:
            await S.audit(user, "cancel", cur, "cancelled")
        return cur or await S.get_task(owner, task["id"])
    if task["status"] == S.QUEUED and task.get("batch_ids"):
        from routers.whatsapp import BatchActionIn, batch_action
        jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": task["batch_ids"]}}, {"_id": 0, "status": 1}).to_list(2000)
        if jobs and all(j["status"] == "pending" for j in jobs):
            for bid in task["batch_ids"]:
                try:
                    await batch_action(bid, BatchActionIn(action="cancel"), admin=user)
                except HTTPException:
                    pass
            task = await refresh_task(owner, await S.get_task(owner, task["id"]))
            cur = await S.update_task(owner, task["id"], {"status": S.CANCELLED, "completed_at": S.now()}, only_status=[S.QUEUED, S.CANCELLED, S.RUNNING])
            await S.audit(user, "cancel", cur or task, "cancelled")
            return cur or task
        raise HTTPException(409, "Sending has already started, so it can't be cancelled safely.")
    raise HTTPException(409, "This task can't be cancelled now.")


async def retry_task(user: dict, task: dict) -> dict:
    owner = user["id"]
    if task["status"] not in (S.FAILED, S.PARTIAL):
        raise HTTPException(409, "Only failed or partially completed tasks can be retried.")
    if task.get("uncertain") and task["kind"] in ("send", "share"):
        raise HTTPException(409, "I can't tell whether this was queued before it was interrupted. Check WhatsApp Engine → campaign history for this project first; if nothing went out, ask me again.")
    if not task.get("batch_ids"):
        if task["kind"] in ("send", "share"):
            raise HTTPException(409, "Nothing was queued, so there is nothing to retry — confirm it again from a new request.")
        cur = await S.update_task(owner, task["id"], {"status": S.AWAITING_CONFIRMATION, "error": None, "uncertain": False}, only_status=[S.FAILED, S.PARTIAL])
        return cur or task
    # sends: re-queue ONLY the failed jobs through the existing retry (sent jobs are never touched → no duplicates)
    from routers.whatsapp import retry_job
    failed = await db.whatsapp_jobs.find({"batch_id": {"$in": task["batch_ids"]}, "status": "failed"}, {"_id": 0, "id": 1, "batch_id": 1}).to_list(500)
    if not failed:
        raise HTTPException(409, "There are no failed messages to retry.")
    for j in failed:
        await retry_job(j["batch_id"], j["id"], admin=user)
    cur = await S.update_task(owner, task["id"], {"status": S.QUEUED, "completed_at": None, "error": None}, only_status=[S.FAILED, S.PARTIAL])
    await S.audit(user, "retry", cur or task, "requeued_failed_jobs", {"jobs": len(failed)})
    return await refresh_task(owner, cur or task)
