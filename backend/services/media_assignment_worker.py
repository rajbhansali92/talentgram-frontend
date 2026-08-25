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
from typing import Any, Dict, List, Optional

from core import db
from agents import registry
from agents.modules import media_assignment
from agents.modules import media_send
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


def _project_advisory_note(
    project_label: str, mismatches: List[Dict[str, Any]], ambiguous: List[Dict[str, Any]],
) -> str:
    """Advisory-only (2026-08-25), never blocking — see validate_candidates'
    own comment on why project_mismatch/project_ambiguous marks are
    excluded from assignments but must not stop OTHER, correctly-resolved
    marks in the same scan from completing normally. Appended to whatever
    the primary UPLOAD report ends up being; returns "" (no-op) when there
    is nothing to flag, so callers can always unconditionally append it."""
    lines: List[str] = []
    for m in mismatches:
        lines.append(
            f"- \"{(m.get('mark_text') or '').strip()}\" confidently matches "
            f"{m.get('matched_project_label')!r}, not {project_label!r} — not uploaded here."
        )
    for a in ambiguous:
        candidates = ", ".join(p.get("label", "") for p in (a.get("ambiguous_projects") or []))
        lines.append(
            f"- \"{(a.get('mark_text') or '').strip()}\" could be for more than one project "
            f"({candidates}) — not uploaded here, nothing was guessed."
        )
    if not lines:
        return ""
    return "\n\nNote — not included above:\n" + "\n".join(lines)


def _report_batch_failed(talent_label: str, project_label: str, batch_failures: List[Dict[str, Any]]) -> str:
    items = "\n".join(
        f"- {(b.get('mark_text') or '').strip()} ({b.get('batch_resolution_error') or 'could not resolve album tiles'})"
        for b in batch_failures
    )
    return (
        f"BATCH RESOLUTION FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"The following batch mark(s) could not be deterministically resolved to the album's "
        f"tiles:\n{items}\n\nNo upload was performed. Re-check the mark and album, then retry."
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


def _report_no_marks_found(talent_label: str, project_label: str) -> str:
    """Completion-invariant fix (2026-08-25 — real production incident):
    _report_already_uploaded used to fire whenever there was nothing left
    to download, which is ALSO true when the scan found zero marks for
    the requested project in the first place — a completely different
    situation from "everything is genuinely already uploaded", but the
    two were never distinguished, so an admin got told ALREADY COMPLETED
    for a submission that had zero media at all. This is the honest,
    distinct report for that case — never claims completion of anything."""
    return (
        f"NO MARKED MEDIA FOUND\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"No marks in the WhatsApp group resolved to this project. Nothing was uploaded.\n\n"
        f"Check that the mark text references this project clearly, then retry."
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


def _report_already_sent(talent_label: str, project_label: str, destination_group: str, already: List[Dict[str, Any]]) -> str:
    lines = [
        media_assignment.role_label(a["media_role"], a.get("take_number"), project_label)
        for a in already
    ]
    return (
        f"ALREADY SENT\n\nTalent: {talent_label}\nProject: {project_label}\n"
        f"Destination: {destination_group}\n\n{_fmt_list(lines, True)}\n\nNo duplicate send performed."
    )


def _report_send_result(
    talent_label: str, project_label: str, destination_group: str,
    sent_labels: List[str], failed_items: List[Dict[str, str]], already: List[Dict[str, Any]],
    *, form_status_line: Optional[str] = None,
) -> str:
    already_labels = [
        media_assignment.role_label(a["media_role"], a.get("take_number"), project_label)
        for a in already
    ]
    total = len(already_labels) + len(sent_labels) + len(failed_items)
    body_lines = ([form_status_line] if form_status_line else []) + [f"✓ {l}" for l in already_labels + sent_labels]
    body_lines += [f"✗ {i['label']} — {i['error']}" for i in failed_items]
    body = "\n".join(body_lines)
    form_failed = bool(form_status_line and form_status_line.startswith("✗"))
    header = "SEND COMPLETE ✓" if not (failed_items or form_failed) else "SEND PARTIAL"
    return (
        f"{header}\n\nTalent: {talent_label}\nProject: {project_label}\n"
        f"Destination: {destination_group}\n\n"
        f"{len(already_labels) + len(sent_labels)}/{total} media sent"
        + (f", {len(failed_items)} failed" if failed_items else "")
        + f"\n\n{body}\n\nPipeline stage was NOT changed."
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

    if doc.get("scan_probe"):
        # TEMPORARY (2026-08-23) — diagnostic-only scan (no real
        # talent/project), used to inspect raw DOM shapes (e.g. a
        # whole-album reply's quoted-block) without ever validating
        # candidates against a fake talent/project or sending a report —
        # mirrors mode=="download_probe"'s short-circuit in
        # _process_download_done. Marks finished directly; the candidates/
        # debug fields the worker already wrote stay on the doc for
        # inspection.
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {"status": media_assignment.STATUS_FINISHED, "completed_at": _now()}},
        )
        return True

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

    if outcome.batch_failures:
        await _finish(doc["id"], _report_batch_failed(talent_label, project_label, outcome.batch_failures))
        return True
    # project_mismatch/project_ambiguous (2026-08-25) are advisory, never
    # blocking — see validate_candidates' own comment. UPLOAD's report
    # below appends a note for any such mark; SEND's own report (further
    # down, workflow=="send") is untouched and does not read these fields
    # at all — this task is UPLOAD-only.
    upload_advisory = _project_advisory_note(project_label, outcome.project_mismatch, outcome.project_ambiguous)
    if outcome.ambiguous:
        await _finish(doc["id"], _report_ambiguous(talent_label, project_label, outcome.ambiguous))
        return True
    if outcome.unresolved:
        await _finish(doc["id"], _report_unresolved(talent_label, project_label, outcome.unresolved))
        return True

    if doc.get("workflow") == "send":
        # SEND (2026-08-24) — independent of UPLOAD from this point on:
        # own idempotency collection (media_send.already_sent, never
        # media_assignment.already_uploaded), own target list
        # (send_targets, never download_targets/Cloudinary), same shared
        # candidate validation above.
        destination_group = doc["destination_group"]
        already = await media_send.already_sent(talent_id, project_id, destination_group)
        already_slots = {
            media_assignment.slot_key(a["media_role"], a.get("take_number"), a.get("source_message_id"), a.get("source_thumbnail_hash"))
            for a in already
        }
        for m in outcome.assignments:
            await media_send.record_send(
                talent_id=talent_id, project_id=project_id, destination_group=destination_group,
                group_name=group_name, group_id=doc.get("group_id"), mark=m, created_by="whatsapp-agent",
            )
        to_send = [
            m for m in outcome.assignments
            if media_assignment.slot_key(m["media_role"], m["take_number"], m.get("resolved_source_message_id"), m.get("quoted_thumbnail_hash")) not in already_slots
        ]
        # A pending form_message must still reach the worker even when
        # every media item is already sent — SEND's own spec requires the
        # form to go out independent of media state, never silently
        # dropped because there was nothing new to forward.
        if not to_send and not doc.get("form_message"):
            await _finish(doc["id"], _report_already_sent(talent_label, project_label, destination_group, already))
            return True
        send_targets = [{
            "source_message_id": m["resolved_source_message_id"],
            "media_role": m["media_role"], "take_number": m["take_number"],
            "source_media_type": m.get("source_media_type"),
            "source_thumbnail_hash": m.get("quoted_thumbnail_hash"),
            "album_tile_index": m.get("album_tile_index"),
            "mark_reply_message_id": m.get("reply_message_id"), "mark_reply_text": m.get("mark_text"),
            "mark_target_contact_id": m.get("mention_lid"),
            "destination_group": destination_group,
            # role_label (not submission_label) — a SEND caption needs
            # the project name, since it lands in a shared casting
            # group alongside other talents/projects; submission_label
            # is for the app's own submission page, where the project is
            # already implicit.
            "caption": f"{talent_label} — {media_assignment.role_label(m['media_role'], m['take_number'], project_label)}",
            "talent_id": talent_id, "project_id": project_id,
        } for m in to_send]
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {
                "mode": "send",
                "status": media_assignment.DOWNLOAD_STATUS_PENDING,
                "send_targets": send_targets,
                # form_message rides through unchanged from create_send_scan_request
                # (already None if this exact submission version was already
                # sent) — the worker sends it first, before any media forward.
                "form_message": doc.get("form_message"),
                "pending_report_context": {
                    "talent_label": talent_label, "project_label": project_label,
                    "destination_group": destination_group, "already": already,
                    "submission_id": doc.get("submission_id"), "content_hash": doc.get("content_hash"),
                    "form_message_included": bool(doc.get("form_message")),
                },
                "updated_at": _now(),
            }},
        )
        return True

    already = await media_assignment.already_uploaded(talent_id, project_id)
    already_slots = {
        media_assignment.slot_key(a["media_role"], a.get("take_number"), a.get("source_message_id"), a.get("source_thumbnail_hash"))
        for a in already
    }

    for m in outcome.assignments:
        await media_assignment.record_assignment(
            talent_id=talent_id, project_id=project_id, normalized_project=project_label,
            group_name=group_name, group_id=doc.get("group_id"), mark=m, created_by="whatsapp-agent",
        )

    to_download = [
        m for m in outcome.assignments
        if media_assignment.slot_key(m["media_role"], m["take_number"], m.get("resolved_source_message_id"), m.get("quoted_thumbnail_hash")) not in already_slots
    ]
    if not to_download:
        if not outcome.assignments and not already:
            # Completion-invariant fix — "nothing left to download" is
            # ALSO true when the scan found zero marks for this project
            # at all (never confuse "nothing new because it's done" with
            # "nothing new because nothing was ever found"). See
            # _report_no_marks_found's docstring for the real incident
            # this reproduces exactly (Sharvari Kashid / Tapti AI App
            # (Ananya), 2026-08-25): two real marks existed in the
            # WhatsApp group, but their project text didn't resolve to
            # THIS project, so outcome.assignments came back empty and
            # the old code reported ALREADY COMPLETED with an empty item
            # list — while the submission had zero media.
            await _finish(doc["id"], _report_no_marks_found(talent_label, project_label) + upload_advisory)
            return True
        await _finish(doc["id"], _report_already_uploaded(talent_label, project_label, already) + upload_advisory)
        return True

    download_targets = [{
        "source_message_id": m["resolved_source_message_id"],
        "media_role": m["media_role"], "take_number": m["take_number"],
        "source_media_type": m.get("source_media_type"),
        "source_thumbnail_hash": m.get("quoted_thumbnail_hash"),
        "source_sender": m.get("source_sender"), "source_timestamp": m.get("source_timestamp"),
        "mark_reply_message_id": m.get("reply_message_id"), "mark_reply_text": m.get("mark_text"),
        "mark_target_contact_id": m.get("mention_lid"),
        # submission_label (not role_label) — the submission's own media
        # label should read "Take 1"/"Introduction", not "Google Take 1";
        # role_label's project-name prefix is for cross-project WhatsApp
        # chat reports only (see _report_upload_result below).
        "original_label": media_assignment.submission_label(m["media_role"], m["take_number"]),
        # album_tile_index (may be None for a non-album source) is what
        # tells the worker to use the proven tile-viewer download path
        # instead of the plain single-message path.
        "album_tile_index": m.get("album_tile_index"),
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
                "already": already, "upload_advisory": upload_advisory,
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

    if doc.get("mode") == "send":
        # SEND completion (2026-08-24) — independent of UPLOAD's own
        # completion logic below. There is no per-item server-side write
        # during processing (unlike /media-upload, which marks each item
        # "uploaded" synchronously) — the worker's mark_scan.py._run_send
        # builds `download_results` in the SAME order as `send_targets`
        # (one result per target, always, even on early per-item
        # failure), so positional pairing is exact, never inferred.
        ctx = doc.get("pending_report_context") or {}
        talent_label, project_label = ctx.get("talent_label", ""), ctx.get("project_label", "")
        destination_group = ctx.get("destination_group") or doc.get("destination_group")
        talent_id, project_id = doc["talent_id"], doc["project_id"]
        send_targets = doc.get("send_targets") or []
        results = doc.get("download_results") or []

        # Form-send outcome (2026-08-25) — completely independent of the
        # media results below; a media item failing never marks the form
        # failed, and vice versa. `form_message_included` distinguishes
        # "we sent it and it failed" from "already sent earlier, nothing
        # attempted this run" (content_hash unchanged -> no re-send).
        form_status_line: Optional[str] = None
        if ctx.get("form_message_included"):
            form_result = doc.get("form_send_result") or {}
            form_ok = bool(form_result.get("ok"))
            status = media_send.SEND_STATUS_SENT if form_ok else media_send.SEND_STATUS_FAILED
            extra = {"sent_at": _now()} if form_ok else {"error": form_result.get("error") or "no result reported"}
            if ctx.get("submission_id") and ctx.get("content_hash"):
                await media_send.mark_form_send_status(
                    talent_id, project_id, destination_group, ctx["content_hash"], status, **extra,
                )
            form_status_line = "✓ Submission details" if form_ok else f"✗ Submission details — {extra.get('error')}"
        elif ctx.get("content_hash"):
            form_status_line = "✓ Submission details (already sent)"

        sent_labels: List[str] = []
        failed_items: List[Dict[str, str]] = []
        for i, target in enumerate(send_targets):
            label = f"{talent_label} — {media_assignment.role_label(target['media_role'], target.get('take_number'), project_label)}"
            result = results[i] if i < len(results) else None
            ok = bool(result and result.get("ok"))
            status = media_send.SEND_STATUS_SENT if ok else media_send.SEND_STATUS_FAILED
            extra = {"sent_at": _now()} if ok else {"error": (result or {}).get("error") or "no result reported"}
            await media_send.mark_send_status(
                talent_id, project_id, target["source_message_id"], target.get("source_thumbnail_hash"),
                destination_group, status, **extra,
            )
            if ok:
                sent_labels.append(label)
            else:
                failed_items.append({"label": label, "error": (result or {}).get("error") or "no result reported"})

        report = _report_send_result(
            talent_label, project_label, destination_group, sent_labels, failed_items, ctx.get("already") or [],
            form_status_line=form_status_line,
        )
        await _finish(doc["id"], report)
        return True

    ctx = doc.get("pending_report_context") or {}
    talent_label, project_label = ctx.get("talent_label", ""), ctx.get("project_label", "")
    talent_id, project_id = doc["talent_id"], doc["project_id"]

    fresh_uploaded = await media_assignment.already_uploaded(talent_id, project_id)
    uploaded_slots = {
        media_assignment.slot_key(a["media_role"], a.get("take_number"), a.get("source_message_id"), a.get("source_thumbnail_hash"))
        for a in fresh_uploaded
    }

    uploaded_labels, failed_labels = [], []
    for target in doc.get("download_targets") or []:
        slot = media_assignment.slot_key(
            target["media_role"], target["take_number"], target.get("source_message_id"), target.get("source_thumbnail_hash"),
        )
        label = target["original_label"]
        # Was this specific slot newly uploaded (not already-uploaded
        # before this run, which is reported separately)?
        already_before = any(
            media_assignment.slot_key(a["media_role"], a.get("take_number"), a.get("source_message_id"), a.get("source_thumbnail_hash")) == slot
            for a in (ctx.get("already") or [])
        )
        if slot in uploaded_slots and not already_before:
            uploaded_labels.append(label)
        elif not already_before:
            failed_labels.append(label)

    report = _report_upload_result(talent_label, project_label, uploaded_labels, failed_labels, ctx.get("already") or [])
    await _finish(doc["id"], report + (ctx.get("upload_advisory") or ""))
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
#
# 2026-08-25: UPLOAD's own hardened video-retrieval path
# (mark_scan.py's PER_VIDEO_DOWNLOAD_TIMEOUT=500s, up to
# MAX_DOWNLOAD_READINESS_ROUNDS close/reopen rounds) legitimately needs up
# to `60 + 500*n_video_targets` seconds for a "download" request — a real
# single-video UPLOAD E2E was reaped here at 360s while still genuinely
# working (elapsed ~400s, well within its own worker-side budget). Sized
# for a realistic worst-case batch (4 videos: 3 takes + intro) with
# headroom, not an arbitrary bump — still comfortably bounded, never
# "forever".
STUCK_CLAIM_TIMEOUT_S = 2100
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
