"""
WhatsApp Worker — Main Loop
Orchestrates the Playwright session, polls the MongoDB-backed job queue, claims jobs atomically,
executes sends with randomized delays, handles retries, and maintains heartbeats.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sys
from datetime import datetime, timezone, timedelta

from db import init_db, get_db
from session import WhatsAppSession
from sender import send_whatsapp_message
import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("whatsapp_worker")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def write_audit_log(
    event_type: str,
    actor: str = "worker",
    *,
    batch_id: str = None,
    job_id: str = None,
    talent_id: str = None,
    talent_name: str = None,
    destination: str = None,
    destination_type: str = None,
    message_preview: str = None,
    is_dry_run: bool = False,
    metadata: dict = None,
) -> None:
    db = get_db()
    doc = {
        "id": config.str_uuid(),
        "event_type": event_type,
        "batch_id": batch_id,
        "job_id": job_id,
        "talent_id": talent_id,
        "talent_name": talent_name,
        "destination": destination,
        "destination_type": destination_type,
        "message_preview": message_preview[:200] if message_preview else None,
        "is_dry_run": is_dry_run,
        "actor": actor,
        "metadata": metadata or {},
        "timestamp": _utcnow(),
    }
    await db.whatsapp_audit_log.insert_one(doc)


async def poll_and_process_jobs(session: WhatsAppSession) -> None:
    """Poll for a single pending job, claim it, send the message, update status."""
    db = get_db()
    
    # 1. Look for a batch in 'running' or 'pending' state
    # We only process jobs for active batches
    active_batches = await db.whatsapp_batches.find(
        {"status": {"$in": ["running", "pending"]}, "is_dry_run": False}
    ).to_list(100)
    
    if not active_batches:
        return
        
    batch_ids = [b["id"] for b in active_batches]
    batch_map = {b["id"]: b for b in active_batches}
    
    # 2. Query for a pending job in an active batch
    # Using find_one_and_update for atomic claim
    now_str = _utcnow()
    job = await db.whatsapp_jobs.find_one_and_update(
        {
            "batch_id": {"$in": batch_ids},
            "status": "pending",
            "is_dry_run": False,
        },
        {
            "$set": {
                "status": "sending",
                "worker_picked_at": now_str,
            }
        },
        sort=[("created_at", 1)],
        return_document=True,
    )
    
    if not job:
        # Check if any pending batch has actually finished all jobs
        for batch_id in batch_ids:
            pending_count = await db.whatsapp_jobs.count_documents(
                {"batch_id": batch_id, "status": "pending"}
            )
            sending_count = await db.whatsapp_jobs.count_documents(
                {"batch_id": batch_id, "status": "sending"}
            )
            if pending_count == 0 and sending_count == 0:
                # Update batch to completed
                total = await db.whatsapp_jobs.count_documents({"batch_id": batch_id})
                sent = await db.whatsapp_jobs.count_documents({"batch_id": batch_id, "status": "sent"})
                failed = await db.whatsapp_jobs.count_documents({"batch_id": batch_id, "status": "failed"})
                
                await db.whatsapp_batches.update_one(
                    {"id": batch_id},
                    {
                        "$set": {
                            "status": "completed",
                            "completed_at": now_str,
                            "sent_count": sent,
                            "failed_count": failed,
                        }
                    }
                )
                logger.info("batch: marked batch %s as completed (sent=%d, failed=%d)", batch_id, sent, failed)
        return

    batch_id = job["batch_id"]
    batch = batch_map.get(batch_id)
    
    # Ensure the batch status is updated to 'running' if it was 'pending'
    if batch and batch.get("status") == "pending":
        await db.whatsapp_batches.update_one(
            {"id": batch_id},
            {"$set": {"status": "running", "started_at": now_str}}
        )

    job_id = job["id"]
    logger.info("worker: claimed job %s for talent %s (%s)", job_id, job["talent_name"], job["destination"])
    
    min_delay = batch.get("min_delay_sec", 8) if batch else 8
    max_delay = batch.get("max_delay_sec", 15) if batch else 15
    delay = random.uniform(min_delay, max_delay)
    
    # Check circuit breaker limit
    config_docs = await db.whatsapp_config.find({}).to_list(50)
    config_dict = {d["key"]: d["value"] for d in config_docs}
    
    circuit_threshold = int(config_dict.get("circuit_breaker_threshold", 5))
    max_retries = int(config_dict.get("max_retries", 3))

    # Perform actual send
    success = False
    error_msg = None
    
    try:
        await send_whatsapp_message(
            page=session.page,
            destination_type=job["destination_type"],
            destination=job["destination"],
            message_body=job["message_body"],
            media_url=job.get("media_url"),
        )
        success = True
    except Exception as exc:
        error_msg = str(exc)
        logger.error("worker: failed to send job %s: %s", job_id, error_msg)

    finish_now = _utcnow()
    
    if success:
        # Update job to sent
        await db.whatsapp_jobs.update_one(
            {"id": job_id},
            {
                "$set": {
                    "status": "sent",
                    "sent_at": finish_now,
                    "attempt_count": job["attempt_count"] + 1,
                    "last_attempted_at": finish_now,
                }
            }
        )
        # Increment batch counters
        await db.whatsapp_batches.update_one(
            {"id": batch_id},
            {"$inc": {"sent_count": 1}}
        )
        # Log audit entry
        await write_audit_log(
            "message_sent",
            batch_id=batch_id,
            job_id=job_id,
            talent_id=job["talent_id"],
            talent_name=job["talent_name"],
            destination=job["destination"],
            destination_type=job["destination_type"],
            message_preview=job["message_body"],
        )
        
        # Apply randomized human-like delay
        logger.info("worker: sleeping for %.2f seconds before next job...", delay)
        await asyncio.sleep(delay)
        
    else:
        # Handle failure & retries
        attempt_count = job["attempt_count"] + 1
        is_retryable = attempt_count < max_retries
        
        new_status = "pending" if is_retryable else "failed"
        
        await db.whatsapp_jobs.update_one(
            {"id": job_id},
            {
                "$set": {
                    "status": new_status,
                    "error_message": error_msg,
                    "attempt_count": attempt_count,
                    "last_attempted_at": finish_now,
                    "worker_picked_at": None, # clear claim so it can be retried
                }
            }
        )
        
        if not is_retryable:
            await db.whatsapp_batches.update_one(
                {"id": batch_id},
                {"$inc": {"failed_count": 1}}
            )
            
        await write_audit_log(
            "message_failed",
            batch_id=batch_id,
            job_id=job_id,
            talent_id=job["talent_id"],
            talent_name=job["talent_name"],
            destination=job["destination"],
            destination_type=job["destination_type"],
            message_preview=job["message_body"],
            metadata={"error": error_msg, "attempt": attempt_count, "will_retry": is_retryable},
        )
        
        # Circuit Breaker check
        # Count consecutive failures in the last 10 jobs of this batch
        recent_jobs = await db.whatsapp_jobs.find(
            {"batch_id": batch_id},
            sort=[("last_attempted_at", -1)],
        ).limit(circuit_threshold).to_list(circuit_threshold)
        
        consecutive_failures = 0
        for r_job in recent_jobs:
            if r_job.get("status") == "failed" or (r_job.get("status") == "pending" and r_job.get("error_message")):
                consecutive_failures += 1
            else:
                break
                
        if consecutive_failures >= circuit_threshold:
            logger.warning("worker: CIRCUIT BREAKER TRIGGERED for batch %s. Pausing batch.", batch_id)
            await db.whatsapp_batches.update_one(
                {"id": batch_id},
                {"$set": {"status": "paused"}}
            )
            await write_audit_log(
                "batch_paused",
                batch_id=batch_id,
                metadata={"reason": f"Circuit breaker: {consecutive_failures} consecutive failures"},
            )
            
        # Standard safety delay even after failure
        await asyncio.sleep(5)


async def main() -> None:
    """Main execution lifecycle."""
    logger.info("worker: initializing database connection...")
    await init_db()
    
    # Add a helper function to config for UUID generation
    import uuid
    config.str_uuid = lambda: str(uuid.uuid4())

    session = WhatsAppSession()
    
    # Task to run heartbeat checking periodically
    async def heartbeat_loop():
        while True:
            await asyncio.sleep(config.HEARTBEAT_SEC)
            if session.is_healthy:
                await session.heartbeat()

    logger.info("worker: starting WhatsApp browser session...")
    try:
        await session.start()
    except Exception as e:
        logger.critical("worker: failed to start WhatsApp Web session: %s", e)
        await session.stop()
        sys.exit(1)

    # Start the heartbeat loop task in background
    heartbeat_task = asyncio.create_task(heartbeat_loop())

    logger.info("worker: entering job polling loop")
    try:
        while True:
            if not session.is_healthy:
                logger.error("worker: session is unhealthy, attempting restart...")
                await session.stop()
                await asyncio.sleep(10)
                await session.start()
                continue
                
            await poll_and_process_jobs(session)
            await asyncio.sleep(2.0) # check for jobs every 2 seconds
            
    except asyncio.CancelledError:
        logger.info("worker: shutting down...")
    finally:
        heartbeat_task.cancel()
        await session.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("worker: terminated by keyboard")
