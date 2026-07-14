import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from core import db

async def create_import_session(
    filename: str, 
    total_rows: int,
    records: List[Dict[str, Any]] = None,
    dup_actions: Dict[str, Any] = None,
    admin_id: str = None,
    admin_email: str = None
) -> str:
    session_id = str(uuid.uuid4())
    session_doc = {
        "_id": session_id,
        "filename": filename,
        "status": "queued",
        "total_rows": total_rows,
        "processed_rows": 0,
        "successful_rows": 0,
        "failed_rows": [],
        "snapshots": [], # Stores {"talent_id": ..., "before": ..., "after": ...}
        "records": records or [],
        "dup_actions": dup_actions or {},
        "admin_id": admin_id,
        "admin_email": admin_email,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "duration_seconds": 0.0
    }
    await db.import_sessions.insert_one(session_doc)
    return session_id


async def update_import_progress(
    session_id: str, 
    processed_count: int, 
    success_count: int,
    failed_rows: List[Dict[str, Any]],
    status: str = "importing"
):
    await db.import_sessions.update_one(
        {"_id": session_id},
        {
            "$set": {
                "processed_rows": processed_count,
                "successful_rows": success_count,
                "status": status
            },
            "$push": {
                "failed_rows": {"$each": failed_rows}
            }
        }
    )

async def record_import_snapshot(session_id: str, talent_id: str, before: Optional[Dict[str, Any]], after: Dict[str, Any]):
    # Strip MongoDB ObjectId to avoid serialization issues
    if before and "_id" in before:
        before = before.copy()
        before.pop("_id", None)
    if after and "_id" in after:
        after = after.copy()
        after.pop("_id", None)
        
    await db.import_sessions.update_one(
        {"_id": session_id},
        {
            "$push": {
                "snapshots": {
                    "talent_id": talent_id,
                    "before": before,
                    "after": after
                }
            }
        }
    )

async def finalize_import_session(session_id: str, status: str, duration: float):
    await db.import_sessions.update_one(
        {"_id": session_id},
        {
            "$set": {
                "status": status,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": duration
            }
        }
    )
