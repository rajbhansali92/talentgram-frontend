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

from core import db, SubmissionDecisionIn
from agents import registry
from agents.modules import media_assignment
from agents.modules import media_send
from agents.modules.whatsapp_campaign_agent import _service_admin
from routers.submissions import set_decision
from routers.whatsapp import BatchIn, ManualContact, SourceParams, create_batch

logger = logging.getLogger(__name__)

_worker_task = None
# 2026-08-27 (SEND speed investigation): reduced from 2.0s. This interval
# only governs how long the orchestrator can sit idle before re-checking
# for scan_done/download_done work — it never touches WhatsApp at all
# (pure Mongo poll), so tightening it is risk-free for both UPLOAD and
# SEND; it directly shrinks the "form/scan finished but nothing noticed
# yet" gap a real production SEND showed taking several real seconds on
# the scan_result -> send_dispatched transition. The loop already
# fast-cycles to 0.2s right after any poll that found work — this constant
# only matters for the FIRST check after a period of true idleness.
POLL_SEC = 0.5


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
    # Production fix (routing hardening) — real bug: this hardcoded
    # "casting-agent" lookup predates the Talentgram Scouting Agent
    # consolidation, which moved every real UPLOAD/SEND/ADD/MOVE command
    # onto whatsapp-campaign-agent's own group ("Talentgram Scouting
    # Agent"); casting-agent's group ("Talentgram Casting Pipeline") has
    # been redirect-only ever since (see casting_pipeline.py's
    # CASTING_REDIRECT_INTENT) and can no longer legitimately originate a
    # scan_request at all. Every async SEND/UPLOAD completion report was
    # still being posted into the now-inactive Casting Pipeline group
    # regardless of which group the command actually came from — this is
    # the actual routing fix, not a message-hiding workaround.
    cfg = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": "whatsapp-campaign-agent", "active": True})
    group_names = (cfg or {}).get("group_names") or []
    if not group_names:
        logger.warning("media_assignment_worker: no whatsapp-campaign-agent group configured, cannot send report")
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


_UNRESOLVED_STATE_PHRASE = {
    # From mark_scan._resolve_single_media_via_jump's `failure_state`
    # (2026-09-11 — Zeeshan Ali). Distinguishes "the marked message is
    # genuinely gone / the jump landed elsewhere" (re-mark) from "the
    # marked media just didn't finish rendering this time" (retry).
    "not_located": "the marked WhatsApp message could not be re-opened",
    "wrong_message": "the marked WhatsApp message could not be re-opened",
    "media_not_rendered": "the marked media did not finish loading in WhatsApp Web in time",
}


def _report_unresolved(talent_label: str, project_label: str, unresolved: List[Dict[str, Any]]) -> str:
    def _line(u: Dict[str, Any]) -> str:
        role = ("Take " + str(u.get("take_number"))) if u.get("media_role") == "take" else (u.get("media_role") or "").capitalize()
        phrase = _UNRESOLVED_STATE_PHRASE.get(u.get("resolution_failure_state") or "", "the marked WhatsApp message could not be re-opened")
        return f"- {project_label} {role} — {phrase}"

    items = "\n".join(_line(u) for u in unresolved)
    transient = any((u.get("resolution_failure_state") == "media_not_rendered") for u in unresolved)
    tail = (
        "No upload was performed. This is usually a temporary WhatsApp Web loading delay — retry UPLOAD; "
        "if it keeps failing, re-send the MARK reply on the same media."
        if transient else
        "No upload was performed. Re-send the MARK reply on the same media, then retry UPLOAD."
    )
    return (
        f"MEDIA RESOLUTION FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
        f"These were correctly MARKed in {talent_label}'s WhatsApp group, but their exact original "
        f"media could not be re-verified:\n{items}\n\n{tail}"
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


_UPLOAD_STATE_PHRASE = {
    # From mark_scan.py's machine `state` tags (2026-09-11 — Mahim Suhalka:
    # Take AND Introduction both reported the SAME catch-all "WhatsApp Web
    # could not open the media", which was true for one of them (the
    # video viewer never opened) but imprecise for the other (the viewer
    # opened and fully buffered — acquisition itself is what failed).
    # Each machine state now gets its own sentence, per the master
    # prompt's own worked examples.
    "SOURCE_NOT_FOUND": "exact source could not be found from the marked WhatsApp message",
    "SOURCE_NOT_HYDRATED": "exact source was found, but its media did not finish loading in time",
    "MEDIA_HASH_MISMATCH": "was located, but the media no longer matches the mark",
    "MEDIA_TILE_NOT_FOUND": "exact source was found, but its media tile could not be located",
    "MEDIA_NOT_READY": "exact source was found, but its video never became ready to open",
    "MEDIA_OPEN_FAILED": "exact source was found, but the video could not be opened",
    "DOWNLOAD_NOT_STARTED": "exact video was opened, but WhatsApp did not provide the media for download",
    "DOWNLOAD_TIMEOUT": "took too long to retrieve from WhatsApp Web",
    # 2026-09-11 (second Mahim Suhalka follow-up, byte-validation audit):
    # a browser download event firing, or a blob: fetch returning a
    # non-empty response, was never actual proof of a genuine, complete
    # video — this is what the new real content validation
    # (mark_scan._validate_acquired_media_bytes) catches, and it is
    # reported honestly as its OWN state rather than a false success.
    "INVALID_MEDIA_BYTES": "the exact media opened, but WhatsApp Web returned invalid or incomplete media data — the upload was not attempted",
    "UPLOAD_FAILED": "was retrieved, but the upload to Talentgram failed",
}


def _humanize_upload_error(raw_error: str) -> str:
    """UPLOAD per-item failure (2026-09-11 — Sahal Mansuri / then Mahim
    Suhalka: a single catch-all sentence gave no state, and later
    conflated two genuinely different failures under the same wording).
    mark_scan.py now tags its own errors with a machine `[STATE]` prefix
    (see _UPLOAD_STATE_PHRASE); this is checked FIRST. The substring
    matching below remains as a fallback for untagged errors (the photo/
    album acquisition paths, which this incident did not touch, and any
    older persisted record) — never inventing a reason, never exposing
    internals."""
    raw_error = raw_error or ""
    if raw_error.startswith("["):
        end = raw_error.find("]")
        if end > 0:
            state = raw_error[1:end]
            phrase = _UPLOAD_STATE_PHRASE.get(state)
            if phrase:
                return f"{phrase} — please run UPLOAD again."
    low = raw_error.lower()
    if "no longer found in window" in low or "message_not_found" in low or "message not found" in low:
        return "could not be found from the exact marked WhatsApp message — please re-mark it and run UPLOAD again."
    if "hash_mismatch" in low or "no_tiles_found" in low or "hash_read_failed" in low:
        return "was located, but the media no longer matches the mark — please re-mark it and run UPLOAD again."
    if "timed out after" in low:
        return "took too long to retrieve from WhatsApp Web — please run UPLOAD again."
    if "zero bytes" in low:
        return "was retrieved from WhatsApp Web but came back empty — please run UPLOAD again."
    if "download_not_available" in low or "download" in low or "no <video>" in low or "click failed" in low or "tile" in low:
        return "was found, but WhatsApp Web could not open the media to retrieve it — please run UPLOAD again."
    if "/media-upload" in low or "handoff" in low or "cloudinary" in low or "http " in low:
        return "was retrieved, but the upload to Talentgram failed — please run UPLOAD again."
    if "group not open" in low:
        return "could not be retrieved because the WhatsApp group could not be opened — please run UPLOAD again."
    return "could not be uploaded due to a WhatsApp Web issue — please run UPLOAD again."


def _report_upload_result(
    talent_label: str, project_label: str, uploaded_labels: List[str],
    failed_items: List[Dict[str, str]], already: List[Dict[str, Any]],
) -> str:
    already_labels = [
        media_assignment.role_label(a["media_role"], a.get("take_number"), project_label)
        for a in already
    ]
    all_ok_lines = already_labels + uploaded_labels
    failed_lines = [f"✗ {fi['label']} — {_humanize_upload_error(fi.get('error') or '')}" for fi in failed_items]
    body = "\n".join([f"✓ {l}" for l in all_ok_lines] + failed_lines)
    if failed_items:
        failed_str = ", ".join(fi["label"] for fi in failed_items)
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
        media_assignment.simple_role_label(a["media_role"], a.get("take_number"))
        for a in already
    ]
    return (
        f"ALREADY SENT\n\nTalent: {talent_label}\nProject: {project_label}\n"
        f"Destination: {destination_group}\n\n{_fmt_list(lines, True)}\n\nNo duplicate send performed."
    )


def _humanize_media_send_error(raw_error: str) -> str:
    """Production fix (Requirement #14) — the worker's own per-item error
    string is a genuinely useful, detailed diagnostic (a raw Playwright
    exception, e.g. "tile click failed after 3 attempts:
    Locator.scroll_into_view_if_needed: Timeout 5000ms exceeded") — exactly
    right for Railway logs, never appropriate as the primary WhatsApp
    user-facing text. This maps the worker's own small, closed set of
    failure-reason PREFIXES (see mark_scan.py's
    _open_media_and_get_forward_button/_send_one_target_native_forward,
    the only producers of this string) to one plain-English sentence each
    — never inventing a new reason, never hiding that something failed.
    The raw string is still logged verbatim by the caller before this
    runs, so nothing diagnostic is lost, only kept out of the chat."""
    logger.info("media_assignment_worker: raw SEND item error: %s", raw_error)
    low = (raw_error or "").lower()
    # Native-forward failure states (2026-09-11 — Sahal Mansuri / Mahindra
    # Thar: the previous catch-all "send failed" bucket reported "Send
    # control could not be confirmed" even when the real failure was the
    # video never becoming forward-ready or the compose box never
    # appearing). mark_scan.py now tags the exact stalled state.
    if "video forward control not ready" in low or "forward control not ready" in low:
        return "could not be sent because WhatsApp Web could not make the video's Forward control ready in time. Please try SEND again."
    if "tile click failed" in low or "forward not ready" in low or "no <video> mounted" in low or "no clickable" in low or "no longer found in window" in low or "message not found in current window" in low:
        return "could not be sent because WhatsApp Web could not reopen the marked media. Please re-mark the media and try SEND again."
    if "destination selection failed" in low:
        return "could not be sent because the destination group could not be selected in WhatsApp Web. Please try SEND again."
    if "[forward_dialog_ready]" in low or "forward dialog never appeared" in low:
        return "could not be sent because WhatsApp Web's Forward dialog did not open. Please try SEND again."
    if "[caption_state]" in low or "compose box" in low:
        return "could not be sent because WhatsApp Web's forward caption box did not appear. Please try SEND again."
    if "[send_control_ready]" in low or "no real send control" in low:
        return "could not be sent because WhatsApp Web's Send control could not be confirmed. Please try SEND again."
    if "send failed" in low or "forward click failed" in low:
        return "could not be sent because WhatsApp Web's Send control could not be confirmed. Please try SEND again."
    if "send unverified" in low:
        return "was forwarded but WhatsApp Web could not confirm it arrived in the destination group. Please check the group and try SEND again if it is missing."
    if "source group not open" in low or "source message not open" in low:
        return "could not be sent because the source WhatsApp chat could not be opened. Please try SEND again."
    if "timed out after" in low:
        return "could not be sent because WhatsApp Web did not respond in time. Please try SEND again."
    return "could not be sent due to a WhatsApp Web issue. Please try SEND again."


def _report_send_result(
    talent_label: str, project_label: str, destination_group: str,
    sent_labels: List[str], failed_items: List[Dict[str, str]], already: List[Dict[str, Any]],
    *, form_status_line: Optional[str] = None, marker_status_line: Optional[str] = None,
) -> str:
    """SEND ATTENTION REQUIRED (Production fix, 2026-09-09 — SEND
    self-healing/reliability master prompt, item 14) — a genuinely
    FAILED media item, at this point, has already exhausted
    mark_scan.py's own bounded automatic recovery (MAX_SEND_ITEM_ATTEMPTS
    full attempts per item, each with fresh source reacquisition-by-
    identity and real destination-chat delivery verification — see
    _send_one_target_native_forward's own docstring); this report is the
    ONE-TIME final word on it, never a mid-recovery status (recovery is
    fully synchronous inside the worker's single per-item call, so there
    is nothing "in progress" left to narrate by the time any report is
    posted at all — satisfying the master prompt's own "one processing
    acknowledgement only, never a status message per retry cycle"
    requirement trivially, since no retry-cycle message is ever sent).

    Distinct from a form-only or marker-only failure (send_targets could
    be empty, or every media item could have succeeded) — those keep the
    existing "SEND PARTIAL" header and wording exactly as before; only a
    real, exhausted, actionable MEDIA failure gets the stronger header,
    the explicit "no duplicate media were sent" reassurance (true by
    construction — media_send's own idempotency, unchanged, means a
    later RETRY re-dispatch — literally just re-running the same SEND
    command — naturally excludes every already-SENT item via
    prepare_send_targets' existing already_sent() filtering; nothing new
    was built for this), and the RETRY hint."""
    already_labels = [
        media_assignment.simple_role_label(a["media_role"], a.get("take_number"))
        for a in already
    ]
    total = len(already_labels) + len(sent_labels) + len(failed_items)
    body_lines = ([form_status_line] if form_status_line else []) + [f"✓ {l}" for l in already_labels + sent_labels]
    body_lines += [f"✗ {i['label']} {_humanize_media_send_error(i['error'])}" for i in failed_items]
    if marker_status_line:
        body_lines.append(marker_status_line)
    body = "\n".join(body_lines)
    form_failed = bool(form_status_line and form_status_line.startswith("✗"))
    marker_failed = bool(marker_status_line and marker_status_line.startswith("✗"))
    if not (failed_items or form_failed or marker_failed):
        header = "SEND COMPLETE ✓"
        footer = "Pipeline stage was NOT changed."
    elif failed_items:
        header = "SEND ATTENTION REQUIRED"
        footer = (
            "Pipeline stage was NOT changed.\n\n"
            "Automatic recovery exhausted.\n\n"
            "No duplicate media were sent.\n\n"
            "Reply RETRY to attempt only the failed media."
        )
    else:
        header = "SEND PARTIAL"
        footer = "Pipeline stage was NOT changed."
    return (
        f"{header}\n\nTalent: {talent_label}\nProject: {project_label}\n"
        f"Destination: {destination_group}\n\n"
        f"{len(already_labels) + len(sent_labels)}/{total} media sent"
        + (f", {len(failed_items)} failed" if failed_items else "")
        + f"\n\n{body}\n\n{footer}"
    )


SCAN_STATUS_AWAITING_SIBLINGS = "scan_awaiting_siblings"


async def _finish_multi_source_scan_sibling(doc: Dict[str, Any]) -> None:
    """One of N sibling scan requests (one per configured WhatsApp
    source) for a single mixed-source SEND — see create_send_scan_
    request's own multi_scan_group_id docstring for the full design.
    Marks THIS sibling individually finished-and-waiting, then checks
    whether every sibling has now reached that same state; if not,
    simply returns (the next poll-loop pass over whichever sibling
    finishes next will re-check). Only the call that observes the LAST
    sibling reaching that state proceeds to merge every sibling's raw
    candidates (each tagged with its OWN source_type/group_name — never
    a single shared source assumed), validate once, and either report a
    failure or dispatch the real mode="send" execution — reusing
    media_send.prepare_send_targets and the exact same report/dispatch
    shapes the single-source path already uses. This poll loop is a
    single sequential asyncio loop, never concurrent with itself, so
    this "check if I'm last" pattern needs no additional locking."""
    group_id = doc["multi_scan_group_id"]
    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": doc["id"]},
        {"$set": {"status": SCAN_STATUS_AWAITING_SIBLINGS, "updated_at": _now()}},
    )
    siblings = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find(
        {"multi_scan_group_id": group_id},
    ).sort("created_at", 1).to_list(20)
    total_sources = doc.get("total_sources") or len(siblings)
    ready = [s for s in siblings if s.get("status") == SCAN_STATUS_AWAITING_SIBLINGS]
    if len(siblings) < total_sources or len(ready) < total_sources:
        return  # not every sibling has individually finished scanning yet

    primary = siblings[0]
    primary_id = primary["id"]
    others = [s["id"] for s in siblings[1:]]

    talent_id, project_id = primary["talent_id"], primary["project_id"]
    talent_label, project_label = primary["talent_label"], primary["project_label"]
    destination_group = primary["destination_group"]

    identity = await media_assignment.get_gunwanti_identity()
    if not identity or not identity.get("lid"):
        await _finish(
            primary_id,
            f"SEND FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
            "The Gunwanti agent identity is not configured (missing WhatsApp LID) — "
            "cannot validate @mentions. Contact an admin before retrying.",
        )
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return

    merged: List[Dict[str, Any]] = []
    hard_errors: List[str] = []
    for s in ready:
        if s.get("scan_error"):
            hard_errors.append(s["scan_error"])
            continue
        raw = s.get("candidates") or []
        merged.extend({**c, "source_type": s.get("source_type") or "group", "source_group_name": s.get("group_name")} for c in raw)

    if hard_errors and not merged:
        await _finish(
            primary_id,
            f"SEND FAILED\n\nTalent: {talent_label}\nProject: {project_label}\n\n"
            f"Could not inspect WhatsApp: {'; '.join(hard_errors)}\n\nNothing has been sent.",
        )
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return

    projects = await _fetch_ongoing_projects_raw()
    outcome = media_assignment.validate_candidates(
        merged, gunwanti_lid=identity["lid"],
        requested_project_id=project_id, requested_project_label=project_label,
        projects=projects, talent_id=talent_id,
    )

    if outcome.batch_failures:
        await _finish(primary_id, _report_batch_failed(talent_label, project_label, outcome.batch_failures))
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return
    if outcome.ambiguous:
        await _finish(primary_id, _report_ambiguous(talent_label, project_label, outcome.ambiguous))
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return
    if outcome.unresolved:
        await _finish(primary_id, _report_unresolved(talent_label, project_label, outcome.unresolved))
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return

    send_targets, form_insert_index, send_marker_on_success, already = await media_send.prepare_send_targets(
        talent_id=talent_id, project_id=project_id, destination_group=destination_group,
        assignments=outcome.assignments,
        default_source_type=primary.get("source_type") or "group", default_group_name=primary.get("group_name"),
    )
    if not send_targets and not primary.get("form_message") and not send_marker_on_success:
        await _finish(primary_id, _report_already_sent(talent_label, project_label, destination_group, already))
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})
        return

    await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
        {"id": primary_id},
        {"$set": {
            "mode": "send",
            "status": media_assignment.DOWNLOAD_STATUS_PENDING,
            "send_dispatched_at": _now(),
            "send_targets": send_targets,
            "form_insert_index": form_insert_index,
            "send_marker_on_success": send_marker_on_success,
            "form_message": primary.get("form_message"),
            "pending_report_context": {
                "talent_label": talent_label, "project_label": project_label,
                "destination_group": destination_group, "already": already,
                "submission_id": primary.get("submission_id"), "content_hash": primary.get("content_hash"),
                "form_message_included": bool(primary.get("form_message")),
                "marker_attempted": send_marker_on_success,
            },
            "updated_at": _now(),
        }},
    )
    await db[media_assignment.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": others}})


async def _process_scan_done() -> bool:
    # mode != "resolve_recipient" (2026-09-03 — real production race found
    # via live verification): this collection is also used by
    # casting_pipeline.py's SHARE Instagram recipient resolver
    # (_search_whatsapp_live), which polls the SAME status values
    # (scan_done/scan_failed) directly via find_one, with no claim. Without
    # this exclusion, THIS loop's find_one_and_update wins the race often
    # enough in practice to matter — it has no "talent_id"/"project_id"/etc.
    # for a resolve_recipient doc, throws inside the try/except below, and
    # leaves the doc stranded in "orchestrating_scan" forever (no reaper
    # covers that status), while the resolver's own poller waits out its
    # full timeout and silently falls back to the CRM/talent tier. Confirmed
    # live: "Rising Sun x Talentgram Agency" landed in "orchestrating_scan"
    # and never resolved until this exclusion was added.
    doc = await db[media_assignment.SCAN_REQUESTS_COLLECTION].find_one_and_update(
        {
            "status": {"$in": [media_assignment.SCAN_STATUS_DONE, media_assignment.SCAN_STATUS_FAILED]},
            "mode": {"$ne": "resolve_recipient"},
        },
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

    if doc.get("skip_validation"):
        if doc.get("multi_scan_group_id"):
            # Mixed-source SEND EXECUTION dispatch (Production fix,
            # 2026-09-08) — this is one of N sibling scan requests (one
            # per configured source) created together by
            # casting_pipeline._send_one_pair. validate_candidates must
            # run EXACTLY ONCE, on every sibling's candidates merged
            # together — never once per sibling in isolation (a role
            # marked in both sources would never be caught as real
            # ambiguity otherwise). This poll loop is a single sequential
            # asyncio loop (never concurrent with itself), so "check
            # whether every sibling has individually finished, and if so,
            # become the merger" is safe without extra locking — each
            # call to this function fully completes before the next one
            # starts.
            await _finish_multi_source_scan_sibling(doc)
            return True
        # Non-multi-source skip_validation request (the synchronous
        # single-source preview scan, casting_pipeline.
        # _scan_raw_candidates_for_source) — see media_send.
        # create_send_scan_request's own skip_validation docstring.
        # Finishes directly with whatever the worker wrote back (raw
        # `candidates` on success, `scan_error` on failure) untouched —
        # never runs validate_candidates here; the caller
        # (casting_pipeline._scan_and_validate_multi_source) merges
        # every source's own raw scan and validates once itself.
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

    if doc.get("preview_only"):
        # SEND confirmation preview (Production fix, 2026-09-03) — a
        # READ-ONLY discovery pass the SEND confirmation card triggers
        # BEFORE the admin approves anything, so they see WHICH marked
        # media will actually be forwarded (never "3 media files" — see
        # casting_pipeline.py's _preview_send_marks / _build_send_
        # confirmation). Deliberately bypasses _finish (which posts a
        # report into the WhatsApp casting group) — a preview must never
        # send anything anywhere. Also never touches media_assignments/
        # media_sends (those record REAL marks tied to an actual upload/
        # send; a preview re-triggered on every confirmation-card
        # refresh must stay a pure read with zero side effects) and
        # never proceeds to mode="download"/"send" — this is the SAME
        # terminal-short-circuit shape doc.get("scan_probe") already
        # uses above, for the identical reason (finish directly, no
        # further transition).
        preview_result = {
            "ok": not (outcome.batch_failures or outcome.ambiguous or outcome.unresolved),
            "assignments": outcome.assignments,
            "ambiguous": outcome.ambiguous,
            "unresolved": outcome.unresolved,
            "batch_failures": outcome.batch_failures,
        }
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {
                "status": media_assignment.STATUS_FINISHED,
                "preview_result": preview_result, "completed_at": _now(),
            }},
        )
        return True

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
        # prepare_send_targets (Production fix — mixed-source SEND,
        # 2026-09-08) — the record_send/already-sent/build_send_targets/
        # marker-gate bookkeeping extracted into ONE shared function
        # (agents.modules.media_send) so BOTH this single-source worker-
        # driven path and the synchronous multi-source approval path
        # (casting_pipeline._send_one_pair, where assignments may span
        # BOTH a talent's group and their individual chat) share
        # identical logic. This request's own candidates were all scanned
        # from ONE source (group_name/source_type at the top of this
        # doc), so every assignment here falls through to the default_*
        # values — behavior is byte-for-byte identical to the inline
        # block this replaced. `send_marker_on_success` (returned here)
        # closes the gap to full completion exactly when every remaining
        # media item and the form (if not already sent) are about to be
        # attempted this run — nothing marked for this talent/project is
        # ever left outside send_targets ∪ already, so succeeding at all
        # of send_targets + form is equivalent to succeeding at
        # everything.
        send_targets, form_insert_index, send_marker_on_success, already = await media_send.prepare_send_targets(
            talent_id=talent_id, project_id=project_id, destination_group=destination_group,
            assignments=outcome.assignments,
            default_source_type=doc.get("source_type") or "group", default_group_name=group_name,
        )

        # A pending form_message must still reach the worker even when
        # every media item is already sent — SEND's own spec requires the
        # form to go out independent of media state, never silently
        # dropped because there was nothing new to forward. Likewise, the
        # ☑️ marker alone (nothing left to forward, form already sent)
        # still needs one more worker pass if it hasn't gone out yet.
        if not send_targets and not doc.get("form_message") and not send_marker_on_success:
            await _finish(doc["id"], _report_already_sent(talent_label, project_label, destination_group, already))
            return True
        await db[media_assignment.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": {
                "mode": "send",
                "status": media_assignment.DOWNLOAD_STATUS_PENDING,
                "send_dispatched_at": _now(),  # phase-timing instrumentation, diagnostic only
                "send_targets": send_targets,
                "form_insert_index": form_insert_index,
                "send_marker_on_success": send_marker_on_success,
                # form_message rides through unchanged from create_send_scan_request
                # (already None if this exact submission version was already
                # sent) — the worker sends it at form_insert_index, between
                # Intro and Pictures.
                "form_message": doc.get("form_message"),
                "pending_report_context": {
                    "talent_label": talent_label, "project_label": project_label,
                    "destination_group": destination_group, "already": already,
                    "submission_id": doc.get("submission_id"), "content_hash": doc.get("content_hash"),
                    "form_message_included": bool(doc.get("form_message")),
                    "marker_attempted": send_marker_on_success,
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
            form_status_line = "✓ Submission details" if form_ok else f"✗ Submission details {_humanize_media_send_error(extra.get('error') or '')}"
        elif ctx.get("content_hash"):
            form_status_line = "✓ Submission details (already sent)"

        sent_labels: List[str] = []
        failed_items: List[Dict[str, str]] = []
        for i, target in enumerate(send_targets):
            # simple_role_label (Production fix — issue 7's own example
            # shows "✓ Audition Take" / "✓ Introduction Take", not
            # "✓ {talent} — {project} Take 1"; Talent/Project already have
            # their own header lines above in the completion report).
            label = media_assignment.simple_role_label(target["media_role"], target.get("take_number"))
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

        # Completion marker (Phase 5/7, 2026-08-26) — independent row from
        # media_sends/form_sends; recorded only when the worker actually
        # attempted it (marker_attempted) AND it genuinely succeeded. A
        # withheld or failed marker means the overall operation is still
        # incomplete — the approval snapshot stays "approved" (not
        # "completed"), so the next attempt naturally resumes and retries
        # the marker rather than silently treating this as done.
        marker_status_line: Optional[str] = None
        if ctx.get("marker_attempted"):
            marker_result = doc.get("marker_result") or {}
            marker_ok = bool(marker_result.get("ok"))
            if marker_ok:
                await media_send.record_marker_sent(talent_id, project_id, destination_group, created_by="whatsapp-agent")
                await media_send.complete_send_approval(talent_id, project_id, destination_group)
                # Post-SEND approval transition (2026-08-27) — ONLY reached
                # when every required item (takes/intro/form/pictures) has
                # already succeeded this run AND the ☑️ marker itself just
                # succeeded; a withheld/failed marker means this line is
                # never reached, so a partial SEND never approves anything.
                # Deliberately reuses the REAL production approval
                # mechanism (routers.submissions.set_decision) rather than
                # a raw decision write — that function already owns
                # decided_at/status_history/pipeline-sync/notification,
                # and set_decision is itself idempotent (a no-op when the
                # submission is already "approved" and linked to a
                # talent), so calling it again on a later, unrelated SEND
                # for the same talent/project never double-fires a
                # transition or duplicate notification.
                submission_id = ctx.get("submission_id")
                if submission_id:
                    try:
                        admin = await _service_admin()
                        await set_decision(
                            project_id, submission_id,
                            SubmissionDecisionIn(decision="approved", note="Auto-approved after successful SEND completion"),
                            admin,
                        )
                    except Exception:
                        logger.exception(
                            "media_assignment_worker: SEND completed but the post-SEND approval "
                            "transition failed for submission %r (project %r) — media/form/marker "
                            "were still sent successfully; this does not roll them back.",
                            submission_id, project_id,
                        )
                marker_status_line = "✓ ☑️ (complete)"
            else:
                marker_status_line = f"✗ ☑️ {_humanize_media_send_error(marker_result.get('error') or 'not sent')}"

        # Talent acknowledgement (Production feature) — attempted by the
        # worker only when this run's own media+form all succeeded (see
        # mark_scan.py's _run_send gating), so an ack_result present here
        # is never a false "shared" claim. Deliberately does NOT affect
        # SEND's own COMPLETE/PARTIAL header or media/form counts — the
        # casting group already received everything correctly regardless
        # of whether the courtesy message to the talent's own group landed;
        # only logged so a silent, persistent ack failure is still visible
        # somewhere, never silently swallowed.
        ack_result = doc.get("ack_result")
        if ack_result is not None and not ack_result.get("ok"):
            logger.warning(
                "media_assignment_worker: SEND completed for talent=%r project=%r but the talent "
                "acknowledgement failed to send: %s — casting-group delivery is unaffected.",
                talent_label, project_label, ack_result.get("error"),
            )

        report = _report_send_result(
            talent_label, project_label, destination_group, sent_labels, failed_items, ctx.get("already") or [],
            form_status_line=form_status_line, marker_status_line=marker_status_line,
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

    # Per-item worker error strings, positionally paired with
    # download_targets (2026-09-11 — the previous report just said "could
    # not be uploaded" with no state; the worker always reported WHY).
    dl_results = doc.get("download_results") or []
    uploaded_labels, failed_items = [], []
    for i, target in enumerate(doc.get("download_targets") or []):
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
            item_result = dl_results[i] if i < len(dl_results) else None
            failed_items.append({"label": label, "error": (item_result or {}).get("error") or "no result reported"})

    report = _report_upload_result(talent_label, project_label, uploaded_labels, failed_items, ctx.get("already") or [])
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
        # "resolve_recipient" (2026-09-03) is also a scan-phase mode — its
        # claim starts life as SCAN_STATUS_PENDING and, if orphaned, must be
        # reaped to SCAN_STATUS_FAILED/scan_error the same as "scan", never
        # mistaken for a download-phase claim.
        is_scan = doc.get("mode") in ("scan", "resolve_recipient")
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
