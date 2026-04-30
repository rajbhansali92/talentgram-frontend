"""In-app notifications for the admin team.

Triggered by:
  - new submission finalised
  - submission edit (form / media)
  - retake uploaded
  - admin decision change (approve / reject / hold)

Stored in `notifications` collection — one doc per (recipient, event).
Recipients = every active admin/team user. We DO NOT send email yet
(deferred per product owner). Frontend bell + dropdown reads from
/api/notifications and marks as read in bulk.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

NOTIFICATION_TYPES = {
    "submission_new",        # talent finalised a fresh submission
    "submission_updated",    # talent edited form/media after submitting
    "submission_retake",     # talent uploaded a new take after submitting
    "submission_decision",   # admin/team approved / rejected / put on hold
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _active_user_ids(db) -> List[str]:
    rows = await db.users.find(
        {"status": {"$in": ["active", None]}},
        {"_id": 0, "id": 1},
    ).to_list(1000)
    return [r["id"] for r in rows if r.get("id")]


async def fanout(
    db,
    *,
    type: str,
    title: str,
    body: str,
    payload: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
) -> int:
    """Insert one notification doc per active admin/team user.

    Best-effort — never raises into the caller's request handler. Returns
    the number of recipients reached.
    """
    if type not in NOTIFICATION_TYPES:
        logger.warning("notif fanout: unknown type=%s", type)
        return 0
    try:
        recipients = await _active_user_ids(db)
        if not recipients:
            return 0
        now = _now()
        docs = [
            {
                "id": str(uuid.uuid4()),
                "recipient_id": rid,
                "type": type,
                "title": title,
                "body": body,
                "payload": payload or {},
                "actor_id": actor_id,
                "read_at": None,
                "created_at": now,
            }
            for rid in recipients
            if rid != actor_id  # don't notify the actor of their own action
        ]
        if not docs:
            return 0
        await db.notifications.insert_many(docs)
        return len(docs)
    except Exception as e:
        logger.warning("notif fanout failed (%s): %s", type, e)
        return 0


async def ensure_indexes(db) -> None:
    try:
        await db.notifications.create_index([("recipient_id", 1), ("created_at", -1)])
        await db.notifications.create_index([("recipient_id", 1), ("read_at", 1)])
    except Exception as e:
        logger.warning("notifications index: %s", e)
