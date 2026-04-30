"""Notifications API — admin/team only.

Endpoints:
  GET  /api/notifications          — list (own only); pagination + unread filter
  GET  /api/notifications/unread-count
  POST /api/notifications/{nid}/read
  POST /api/notifications/read-all
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core import _now, current_user, db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    unread_only: bool = False,
    page: int = 0,
    size: int = 30,
    user: dict = Depends(current_user),
):
    page = max(page, 0)
    size = max(1, min(size, 100))
    query = {"recipient_id": user["id"]}
    if unread_only:
        query["read_at"] = None
    total = await db.notifications.count_documents(query)
    items = (
        await db.notifications.find(query, {"_id": 0})
        .sort("created_at", -1)
        .skip(page * size)
        .limit(size)
        .to_list(size)
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "has_more": (page + 1) * size < total,
    }


@router.get("/unread-count")
async def unread_count(user: dict = Depends(current_user)):
    n = await db.notifications.count_documents(
        {"recipient_id": user["id"], "read_at": None}
    )
    return {"count": n}


@router.post("/{nid}/read")
async def mark_read(nid: str, user: dict = Depends(current_user)):
    res = await db.notifications.update_one(
        {"id": nid, "recipient_id": user["id"], "read_at": None},
        {"$set": {"read_at": _now()}},
    )
    if not res.matched_count:
        # Either already read or doesn't belong to this user — idempotent.
        return {"ok": True, "noop": True}
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(user: dict = Depends(current_user)):
    res = await db.notifications.update_many(
        {"recipient_id": user["id"], "read_at": None},
        {"$set": {"read_at": _now()}},
    )
    return {"ok": True, "marked": res.modified_count}
