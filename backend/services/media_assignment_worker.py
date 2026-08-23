"""Media-Assignment backend orchestrator (Phase 1, 2026-08-22).

Drives the whatsapp_scan_requests state machine forward on the BACKEND
side — mirrors services/import_worker.py's exact shape (a persistent
asyncio poll loop, atomic find_one_and_update claims, started once from
server.py). The WhatsApp Worker (whatsapp-worker/mark_scan.py) only ever
does what one claimed request's `mode` says and reports back; every
decision — identity validation, project/role resolution, ambiguity/
resolution-failure detection, idempotency, and the final report text —
happens here, in agents/modules/media_assignment.py's pure functions.

State machine (see agents/modules/media_assignment.py for the full status
constants):
    pending_scan --(worker)--> scan_done | scan_failed
    --(this loop)--> pending_download | finished
    pending_download --(worker)--> download_done | download_failed
    --(this loop)--> finished

The final report is sent into the casting-agent's configured group via the
exact same create_batch() + custom-message-template path the WhatsApp
Campaign Agent already uses for every other outbound send — no new
outbound mechanism.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from core import db
from agents import registry
from agents.modules import media_assignment
from agents.modules.whatsapp_campaign_agent import _service_admin
from routers.whatsapp import BatchIn, ManualContact, SourceParams, create_batch

logger = logging.getLogger(__name__)

_worker_task = None
POLL_SEC = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _fetch_ongoing_projects_raw() -> List[Dict[str, str]]:
    """Self-contained (not imported from casting_pipeline.py's
    request_scope-cached version) — this loop runs outside any dispatch
    turn, so it deliberately doesn't touch that per-turn cache."""
    docs = await db.projects.find(
        {"status": "ongoing"}, {"_id": 0, "id": 1, "brand_name": 1}
    ).to_list(2000)
    return [{"id": d["id"], "label": d.get("brand_name") or "(untitled project)"} for d in docs]


async def _send_report(report_text: str) -> None:
    cfg = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": "casting-agent", "active": True})
    group_names = (cfg or {}).get("group_names") or []
    if not group_names:
        logger.warning("media_assignment_worker: no casting-agent group configured, cannot send report")
        return
    custom_template = await db.whatsapp_templates.find_one({"slug": "custom"}, {"_id": 0, "id": 1})
    if not custom_template:
        logger.warning("media_assignment_worker: no 'custom' template found, cannot send report")
        return
    admin = await _service_admin()
    batch_in = BatchIn(
        source_type="MANUAL",
        source_params=SourceParams(contacts=[
            ManualContact(name="Casting Pipeline", phone="", whatsapp_group_name=group_names[0])
        ]),
        template_id=custom_template["id"],
        variable_data={"message": report_text},
    )
    await create_batch(batch_in, admin=admin)


async def _finish(request_id: str, report_text: str) -> None:
    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": request_id},
        {"$set": {"status": media_assignment.STATUS_FINISHED, "report": report_text, "completed_at": _now()}},
    )
    await _send_report(report_text)


def _fmt_list(lines: List[str], ok: bool) -> str:
    mark = "✓" if ok else "✗"
    return "\n".join(f"{mark} {line}" for line in lines)


def _report_scan_failed(talent_label: str, project_label: str, error: str) -> str:
    return (
        f"UPLOAD FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"Could not inspect the WhatsApp group: {error}\n\nNo media was uploaded."
    )


def _report_ambiguous(talent_label: str, project_label: str, ambiguous: Dict[str, Any]) -> str:
    role = ambiguous["media_role"]
    take_number = ambiguous.get("take_number")
    slot_label = f"Take {take_number}" if role == "take" else role.capitalize()
    return (
        f"AMBIGUOUS MEDIA ASSIGNMENT\n\n"
        f"{project_label} {slot_label} has been marked twice, pointing to two different "
        f"source media messages.\n\nPlease specify which one should be used.\n\n"
        f"Talent: {talent_label}\nProject: {project_label}"
    )


def _report_unresolved(talent_label: str, project_label: str, unresolved: List[Dict[str, Any]]) -> str:
    items = "\n".join(
        f"- {project_label} {('Take ' + str(u.get('take_number'))) if u.get('media_role') == 'take' else (u.get('media_role') or '').capitalize()}"
        for u in unresolved
    )
    return (
        f"MEDIA RESOLUTION FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"The following were correctly marked for Gunwanti but the exact source media could not "
        f"be deterministically resolved:\n{items}\n\nNo upload was performed."
    )


def _report_already_uploaded(talent_label: str, project_label: str, already: List[Dict[str, Any]]) -> str:
    lines = [
        media_assignment.role_label(a["media_role"], a.get("take_number"), project_label)
        for a in already
    ]
    return (
        f"ALREADY COMPLETED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"{_fmt_list(lines, True)}\n\nNo duplicate upload performed."
    )


def _report_upload_result(
    talent_label: str, project_label: str, uploaded_labels: List[str], failed_labels: List[str],
    already: List[Dict[str, Any]],
) -> str:
    already_labels = [
        media_assignment.role_label(a["media_role"], a.get("take_number"), project_label)
        for a in already
    ]
    all_ok_lines = already_labels + uploaded_labels
    body = "\n".join(
        [f"✓ {l}" for l in all_ok_lines] + [f"✗ {l}" for l in failed_labels]
    )
    if failed_labels:
        failed_str = ", ".join(failed_labels)
        return (
            f"UPLOAD FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n{body}\n\n"
            f"{failed_str} could not be uploaded.\n\nPipeline stage was NOT changed."
        )
    return (
        f"UPLOAD COMPLETE ✓\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"Uploaded:\n{body}\n\nDestination:\nTalentgram Submission Review\n\n"
        f"Status:\nAll media uploaded and verified successfully."
    )


async def _process_scan_done() -> bool:
    doc = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find_one_and_update(
        {"status": {"$in": [media_assignment.SCAN_STATUS_DONE, media_assignment.SCAN_STATUS_FAILED]}},
        {"$set": {"status": "orchestrating_scan", "updated_at": _now()}},
        sort=[("updated_at", 1)],
        return_document=True,
    )
    if not doc:
        return False

    talent_id, project_id = doc["talent_id"], doc["project_id"]
    talent_label, project_label = doc["talent_label"], doc["project_label"]
    group_name = doc["group_name"]

    if doc.get("scan_error"):
        await _finish(doc["id"], _report_scan_failed(talent_label, project_label, doc["scan_error"]))
        return True

    identity = await media_assignment.get_gunwanti_identity()
    if not identity or not identity.get("lid"):
        await _finish(
            doc["id"],
            f"UPLOAD FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
            "The Gunwanti agent identity is not configured (missing WhatsApp LID) — "
            "cannot validate @mentions. Contact an admin before retrying.",
        )
        return True

    projects = await _fetch_ongoing_projects_raw()
    outcome = media_assignment.validate_candidates(
        doc.get("candidates") or [],
        gunwanti_lid=identity["lid"],
        requested_project_id=project_id,
        requested_project_label=project_label,
        projects=projects,
        talent_id=talent_id,
    )

    if outcome.ambiguous:
        await _finish(doc["id"], _report_ambiguous(talent_label, project_label, outcome.ambiguous))
        return True
    if outcome.unresolved:
        await _finish(doc["id"], _report_unresolved(talent_label, project_label, outcome.unresolved))
        return True

    already = await media_assignment.already_uploaded(talent_id, project_id)
    already_slots = {(a["media_role"], a.get("take_number")) for a in already}

    for m in outcome.assignments:
        await media_assignment.record_assignment(
            talent_id=talent_id, project_id=project_id, normalized_project=project_label,
            group_name=group_name, group_id=doc.get("group_id"), mark=m, created_by="whatsapp-agent",
        )

    to_download = [m for m in outcome.assignments if (m["media_role"], m["take_number"]) not in already_slots]
    if not to_download:
        await _finish(doc["id"], _report_already_uploaded(talent_label, project_label, already))
        return True

    download_targets = [{
        "source_message_id": m["resolved_source_message_id"],
        "media_role": m["media_role"], "take_number": m["take_number"],
        "source_media_type": m.get("source_media_type"),
        "source_thumbnail_hash": m.get("quoted_thumbnail_hash"),
        "source_sender": m.get("source_sender"), "source_timestamp": m.get("source_timestamp"),
        "mark_reply_message_id": m.get("reply_message_id"), "mark_reply_text": m.get("mark_text"),
        "mark_target_contact_id": m.get("mention_lid"),
        "original_label": media_assignment.role_label(m["media_role"], m["take_number"], project_label),
        "talent_id": talent_id, "project_id": project_id,
    } for m in to_download]

    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": doc["id"]},
        {"$set": {
            "mode": "download",
            "status": media_assignment.DOWNLOAD_STATUS_PENDING,
            "download_targets": download_targets,
            "pending_report_context": {
                "talent_label": talent_label, "project_label": project_label,
                "already": already,
            },
            "updated_at": _now(),
        }},
    )
    return True


async def _process_download_done() -> bool:
    doc = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find_one_and_update(
        {"status": {"$in": [media_assignment.DOWNLOAD_STATUS_DONE, media_assignment.DOWNLOAD_STATUS_FAILED]}},
        {"$set": {"status": "orchestrating_download", "updated_at": _now()}},
        sort=[("updated_at", 1)],
        return_document=True,
    )
    if not doc:
        return False

    if doc.get("mode") == "download_probe":
        # TEMPORARY (2026-08-23) — diagnostic-only mode for the download-
        # mechanism investigation (right-click "Download"/"Download all").
        # No real talent/project upload happened and none should be
        # reported — mark finished directly, no _send_report(), so probing
        # never sends anything into the real casting-agent WhatsApp group.
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {"status": media_assignment.STATUS_FINISHED, "completed_at": _now()}},
        )
        return True

    ctx = doc.get("pending_report_context") or {}
    talent_label, project_label = ctx.get("talent_label", ""), ctx.get("project_label", "")
    talent_id, project_id = doc["talent_id"], doc["project_id"]

    fresh_uploaded = await media_assignment.already_uploaded(talent_id, project_id)
    uploaded_slots = {(a["media_role"], a.get("take_number")) for a in fresh_uploaded}

    uploaded_labels, failed_labels = [], []
    for target in doc.get("download_targets") or []:
        slot = (target["media_role"], target["take_number"])
        label = target["original_label"]
        # Was this specific slot newly uploaded (not already-uploaded
        # before this run, which is reported separately)?
        already_before = any(
            (a["media_role"], a.get("take_number")) == slot for a in (ctx.get("already") or [])
        )
        if slot in uploaded_slots and not already_before:
            uploaded_labels.append(label)
        elif not already_before:
            failed_labels.append(label)

    report = _report_upload_result(talent_label, project_label, uploaded_labels, failed_labels, ctx.get("already") or [])
    await _finish(doc["id"], report)
    return True


# A worker-side asyncio.wait_for around a claimed request only protects
# against an in-process hang — it cannot help if the worker PROCESS itself
# dies mid-request (crash, OOM, Railway's own restart policy) and comes
# back up with no memory of the claim. Real evidence of exactly this
# (2026-08-23): a download_probe claim sat in "processing" for 2h43m while
# the worker process was otherwise healthy and actively servicing other
# WhatsApp traffic the whole time — the claim was simply orphaned. This
# reaper is the backend-side backstop: anything still "processing" well
# past any plausible worker-side bound gets forced to a terminal failed
# state so it can never block a queue or wait forever.
STUCK_CLAIM_TIMEOUT_S = 360
_REAP_INTERVAL_S = 60.0
_last_reap = 0.0


async def _reap_stuck_claims() -> None:
    global _last_reap
    now_ts = asyncio.get_event_loop().time()
    if now_ts - _last_reap < _REAP_INTERVAL_S:
        return
    _last_reap = now_ts
    cutoff = _now().timestamp() - STUCK_CLAIM_TIMEOUT_S
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    stuck = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find(
        {"status": "processing", "claimed_at": {"$lt": cutoff_dt}}
    ).to_list(50)
    for doc in stuck:
        is_scan = doc.get("mode") == "scan"
        next_status = media_assignment.SCAN_STATUS_FAILED if is_scan else media_assignment.DOWNLOAD_STATUS_FAILED
        error_field = "scan_error" if is_scan else "download_error"
        error_msg = f"worker claim orphaned — stuck in 'processing' for over {STUCK_CLAIM_TIMEOUT_S}s, reaped by backend orchestrator"
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"], "status": "processing"},
            {"$set": {"status": next_status, error_field: error_msg, "updated_at": _now()}},
        )
        logger.warning("media_assignment_worker: reaped orphaned claim %r (mode=%r)", doc["id"], doc.get("mode"))


async def _worker_loop() -> None:
    logger.info("media_assignment_worker: starting persistent orchestrator loop...")
    while True:
        try:
            await _reap_stuck_claims()
            did_work = await _process_scan_done()
            did_work = await _process_download_done() or did_work
        except Exception:
            logger.exception("media_assignment_worker: unexpected error in orchestrator cycle")
            did_work = False
        await asyncio.sleep(POLL_SEC if not did_work else 0.2)


def start_media_assignment_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker_loop())
