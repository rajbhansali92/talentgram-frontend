import asyncio
import logging
from datetime import datetime, timezone, timedelta
from core import db
from services.import_service import execute_talent_import
from services.import_sessions import finalize_import_session

logger = logging.getLogger(__name__)
_worker_task = None
_last_refresh_time = None

async def _worker_loop():
    global _last_refresh_time
    logger.info("[Import Queue] Starting persistent worker loop...")
    while True:
        try:
            # 1. Recover stale processing jobs (stuck > 5 minutes)
            # Find and update status of stale processing tasks back to "queued"
            stale_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            stale_jobs = await db.import_sessions.update_many(
                {"status": "processing", "started_at": {"$lt": stale_time}},
                {"$set": {"status": "queued", "notes": "Re-queued automatically due to crash/timeout."}}
            )
            if stale_jobs.modified_count > 0:
                logger.warning(f"[Import Queue] Recovered and re-queued {stale_jobs.modified_count} stale import jobs.")

            # 2. Atomically claim a queued session
            # This is safe for concurrent autoscaled replicas
            session = await db.import_sessions.find_one_and_update(
                {"status": "queued"},
                {"$set": {
                    "status": "processing", 
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }},
                return_document=True
            )
            
            if session:
                session_id = session["_id"]
                filename = session.get("filename", "import.csv")
                logger.info(f"[Import Queue] Claimed job {session_id} for file {filename}")
                
                records = session.get("records", [])
                dup_actions = session.get("dup_actions", {})
                admin_id = session.get("admin_id", "admin")
                admin_email = session.get("admin_email", "admin@talentgram.com")
                
                start_time = datetime.now(timezone.utc)
                try:
                    res = await execute_talent_import(
                        import_id=session_id,
                        records=records,
                        dup_actions=dup_actions,
                        admin_id=admin_id
                    )
                    duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
                    
                    # Log to history (include file_checksum so /upload duplicate detection works)
                    history_doc = {
                        "id": session_id,
                        "user_email": admin_email,
                        "filename": filename,
                        "created_at": start_time.isoformat(),
                        "total_rows": len(records),
                        "imported": res["imported"],
                        "updated": res["updated"],
                        "skipped": res["skipped"],
                        "errors_count": res.get("failed", 0),
                        "duration_seconds": duration_sec,
                        "status": "completed",
                        "file_checksum": session.get("file_checksum")
                    }
                    await db.import_history.insert_one(history_doc)
                    await finalize_import_session(session_id, "completed", duration_sec)
                    
                    # 3. Debounced Search Index Refresh
                    # If other sessions are currently processing, skip refresh for now
                    active_processing = await db.import_sessions.count_documents({"status": "processing"})
                    if active_processing == 0:
                        now = datetime.now(timezone.utc)
                        # Debounce limit of 10 seconds since last refresh
                        if _last_refresh_time is None or (now - _last_refresh_time) > timedelta(seconds=10):
                            try:
                                from core import update_talent_cover_cache
                                await update_talent_cover_cache()
                                _last_refresh_time = now
                                logger.info(f"[Import Queue] Search cache successfully refreshed (debounced).")
                            except Exception as refresh_err:
                                logger.warning(f"Could not refresh search cache: {refresh_err}")
                        else:
                            logger.info(f"[Import Queue] Search cache refresh skipped (recently refreshed).")
                    else:
                        logger.info(f"[Import Queue] Search cache refresh deferred (other imports are still processing).")
                        
                except Exception as run_err:
                    logger.error(f"[Import Queue] Job failed: {run_err}", exc_info=True)
                    duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()
                    await finalize_import_session(session_id, "failed", duration_sec)
                    
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            logger.info("[Import Queue] Worker loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[Import Queue] Worker loop error: {e}", exc_info=True)
            await asyncio.sleep(5.0)

def start_import_worker():
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("[Import Queue] Spawned background worker task successfully.")
