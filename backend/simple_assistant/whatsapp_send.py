"""Thin WhatsApp send adapter for Simple Assistant (Phase 4A).

Contains NO WhatsApp business logic. It receives an already-validated,
server-resolved destination + message/media and hands the operation to the
EXISTING WhatsApp Engine job queue:

    routers.whatsapp._create_batch_internal(BatchIn(source_type="MANUAL", ...))

— the same function POST /api/whatsapp/batches, the "Send Casting Call"
flow, and the WhatsApp Scouting Agent's SEND/SHARE all call. That path
resolves the recipient (routers.whatsapp._resolve_destination), renders the
message, creates `whatsapp_jobs`, and the existing Playwright worker
(whatsapp-worker/) picks them up, downloads any `media_url`, attaches it,
sends with the configured pacing/retries, and writes back per-job status.

No new template, no new queue, no new transport, no re-upload of media
(the worker fetches the existing hosted URL directly).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core import db
from routers.whatsapp import (
    BatchIn,
    ManualContact,
    SourceParams,
    _create_batch_internal,
)

logger = logging.getLogger(__name__)

# The seeded pass-through template ("body_text": "{{message}}", is_custom
# True) — lets us send an exact message without inventing a new template.
_CUSTOM_TEMPLATE_SLUG = "custom"


async def _custom_template_id() -> Optional[str]:
    doc = await db.whatsapp_templates.find_one({"slug": _CUSTOM_TEMPLATE_SLUG}, {"_id": 0, "id": 1})
    return (doc or {}).get("id")


async def preview_destination(*, destination: str, destination_type: str, name: str) -> Dict[str, Any]:
    """Dry-run recipient resolution through the real engine — proves the
    destination is sendable with ZERO side effects (is_dry_run creates no
    jobs). Returns {"ok": bool, "reason": str|None}."""
    tid = await _custom_template_id()
    if not tid:
        return {"ok": False, "reason": "WhatsApp 'Custom Message' template is not configured."}
    contact = _contact(destination, destination_type, name)
    try:
        res = await _create_batch_internal(
            BatchIn(
                source_type="MANUAL",
                source_params=SourceParams(contacts=[contact]),
                template_id=tid,
                variable_data={"message": "preview"},
                is_dry_run=True,
            ),
            _service_admin(),
        )
    except Exception as exc:  # HTTPException("No sendable recipients ...") etc.
        return {"ok": False, "reason": getattr(exc, "detail", str(exc))}
    return {"ok": bool(res.get("jobs")), "reason": None if res.get("jobs") else "Destination did not resolve."}


async def queue_send(
    *,
    destination: str,
    destination_type: str,
    name: str,
    message: str,
    media_url: Optional[str],
    admin: Dict[str, Any],
) -> Dict[str, Any]:
    """Enqueue ONE WhatsApp message (optionally with one media file) to one
    resolved destination via the existing engine. Returns
    {"ok", "batch_id", "job_ids", "reason"}. Does NOT wait for delivery —
    the worker processes the job asynchronously; use send_status() to
    reconcile the real outcome."""
    tid = await _custom_template_id()
    if not tid:
        return {"ok": False, "reason": "WhatsApp 'Custom Message' template is not configured."}
    try:
        res = await _create_batch_internal(
            BatchIn(
                source_type="MANUAL",
                source_params=SourceParams(contacts=[_contact(destination, destination_type, name)]),
                template_id=tid,
                variable_data={"message": message},
                media_url=media_url or None,
                is_dry_run=False,
            ),
            admin,
        )
    except Exception as exc:
        logger.warning("simple_assistant whatsapp queue_send failed: %s", exc)
        return {"ok": False, "reason": getattr(exc, "detail", str(exc))}
    jobs = res.get("jobs") or []
    if not jobs:
        return {"ok": False, "reason": "No sendable recipient — the destination did not resolve."}
    return {
        "ok": True,
        "batch_id": res["batch"]["id"],
        "job_ids": [j["id"] for j in jobs],
        "reason": None,
    }


async def send_status(batch_ids: List[str]) -> Dict[str, Any]:
    """Reconcile the real per-job outcome the worker wrote back. Statuses
    are the engine's own: pending / sending / sent / verified / failed."""
    if not batch_ids:
        return {"jobs": []}
    jobs = await db.whatsapp_jobs.find(
        {"batch_id": {"$in": batch_ids}},
        {"_id": 0, "id": 1, "batch_id": 1, "status": 1, "error_message": 1, "sent_at": 1},
    ).to_list(500)
    return {"jobs": jobs}


def _contact(destination: str, destination_type: str, name: str) -> ManualContact:
    if destination_type == "whatsapp_group":
        # ManualContact.phone is a required str; "" + a group name routes to
        # the group via routers.whatsapp._resolve_destination.
        return ManualContact(name=name or "", phone="", whatsapp_group_name=destination)
    return ManualContact(name=name or "", phone=destination)


def _service_admin() -> Dict[str, Any]:
    # Only used for the dry-run preview (sender/system template vars). The
    # real send uses the actual authenticated admin.
    return {"id": "simple-assistant", "name": "Simple Assistant", "email": "", "role": "team"}
