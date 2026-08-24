"""SEND workflow (2026-08-24) — an INDEPENDENT consumer of the same marked
WhatsApp source media the UPLOAD workflow (media_assignment.py) already
resolves. UPLOAD's path is WhatsApp media -> Cloudinary -> submission.
SEND's path is WhatsApp media -> the Casting Pipeline WhatsApp group
directly. Neither depends on the other's output:

    MARKED WHATSAPP MEDIA
            |
      +-----+-----+
      |           |
   UPLOAD       SEND
      |           |
 submission   casting group

SEND deliberately does NOT read submission.media[], media_assignments, or
any "uploaded" status anywhere — its own idempotency lives entirely in
this module's `media_sends` collection, keyed on the source WhatsApp
media's own identity (never on anything UPLOAD produced).

Talent identity is still resolved the SAME way UPLOAD does — this module
imports (never duplicates) media_assignment.resolve_authoritative_talent_for_upload,
media_assignment.validate_candidates, and the source-resolution machinery
in whatsapp-worker/mark_scan.py, which both workflows share unchanged.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from core import db
from agents.modules.media_assignment import (
    MAX_SCAN_MESSAGES,
    SCAN_REQUESTS_COLLECTION,
    SCAN_STATUS_PENDING,
    _now,
)

MEDIA_SENDS_COLLECTION = "media_sends"

SEND_STATUS_MARKED = "marked"
SEND_STATUS_SENT = "sent"
SEND_STATUS_FAILED = "failed"


async def ensure_indexes() -> None:
    """Called once at backend startup alongside media_assignment.ensure_indexes
    (see server.py) — safe to call repeatedly. The unique key mirrors
    media_assignments' own (talent_id, project_id, source_message_id,
    source_thumbnail_hash) exactly — including source_thumbnail_hash so
    two different tiles of the same album never collide — plus
    destination_group, since the SAME source media could in principle be
    sent to more than one destination and each is its own independent
    send."""
    await db[MEDIA_SENDS_COLLECTION].create_index(
        [
            ("talent_id", 1), ("project_id", 1), ("source_message_id", 1),
            ("source_thumbnail_hash", 1), ("destination_group", 1),
        ],
        unique=True, name="uniq_talent_project_source_message_hash_destination",
    )


async def create_send_scan_request(
    *, talent_id: str, talent_label: str, project_id: str, project_label: str,
    group_name: str, destination_group: str,
) -> str:
    """Same shape/lifecycle as media_assignment.create_scan_request — mode
    stays "scan" (the worker's scan logic is 100% shared/unchanged between
    UPLOAD and SEND); `workflow: "send"` is the ONLY marker the backend
    orchestrator (services/media_assignment_worker.py) needs to branch
    into SEND-specific post-scan handling instead of UPLOAD's."""
    req_id = str(uuid.uuid4())
    await db[SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id,
        "mode": "scan",
        "workflow": "send",
        "status": SCAN_STATUS_PENDING,
        "group_name": group_name,
        "destination_group": destination_group,
        "talent_id": talent_id,
        "talent_label": talent_label,
        "project_id": project_id,
        "project_label": project_label,
        "max_messages": MAX_SCAN_MESSAGES,
        "candidates": None,
        "send_targets": None,
        "download_results": None,
        "report": None,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
    })
    return req_id


async def already_sent(talent_id: str, project_id: str, destination_group: str) -> List[Dict[str, Any]]:
    return await db[MEDIA_SENDS_COLLECTION].find(
        {
            "talent_id": talent_id, "project_id": project_id,
            "destination_group": destination_group, "send_status": SEND_STATUS_SENT,
        },
        {"_id": 0},
    ).to_list(200)


async def record_send(
    *, talent_id: str, project_id: str, destination_group: str,
    group_name: str, group_id: Optional[str], mark: Dict[str, Any], created_by: str,
) -> Dict[str, Any]:
    """Upserts a `marked`-status row keyed on the unique index — safe to
    call repeatedly for the same source media without creating
    duplicates. Mirrors media_assignment.record_assignment exactly,
    scoped to this collection instead."""
    doc = {
        "send_id": str(uuid.uuid4()),
        "talent_id": talent_id,
        "project_id": project_id,
        "destination_group": destination_group,
        "source_group_id": group_id,
        "source_group_name": group_name,
        "source_message_id": mark.get("resolved_source_message_id"),
        "source_media_type": mark.get("source_media_type"),
        "source_thumbnail_hash": mark.get("quoted_thumbnail_hash"),
        "source_sender": mark.get("source_sender"),
        "source_timestamp": mark.get("source_timestamp"),
        "album_tile_index": mark.get("album_tile_index"),
        "mark_reply_message_id": mark.get("reply_message_id"),
        "mark_reply_text": mark.get("mark_text"),
        "mark_target_contact_id": mark.get("mention_lid"),
        "media_role": mark.get("media_role"),
        "take_number": mark.get("take_number"),
        "send_status": SEND_STATUS_MARKED,
        "created_at": _now(),
        "created_by": created_by,
    }
    try:
        await db[MEDIA_SENDS_COLLECTION].insert_one(doc)
        return doc
    except Exception:
        existing = await db[MEDIA_SENDS_COLLECTION].find_one(
            {
                "talent_id": talent_id, "project_id": project_id,
                "destination_group": destination_group,
                "source_message_id": doc["source_message_id"],
                "source_thumbnail_hash": doc["source_thumbnail_hash"],
            },
            {"_id": 0},
        )
        return existing or doc


async def mark_send_status(
    talent_id: str, project_id: str, source_message_id: str, source_thumbnail_hash: str,
    destination_group: str, status: str, **extra,
) -> None:
    """source_thumbnail_hash is required, not optional — same reasoning as
    media_assignment.mark_assignment_status: for an album, source_message_id
    alone matches every tile sharing that album."""
    await db[MEDIA_SENDS_COLLECTION].update_one(
        {
            "talent_id": talent_id, "project_id": project_id,
            "source_message_id": source_message_id, "source_thumbnail_hash": source_thumbnail_hash,
            "destination_group": destination_group,
        },
        {"$set": {"send_status": status, **extra}},
    )
