"""
WhatsApp Worker — Main Loop
Orchestrates the Playwright session, polls the MongoDB-backed job queue, claims jobs atomically,
executes sends with randomized delays, handles retries, and maintains heartbeats.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import sys
from datetime import datetime, timezone, timedelta

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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


async def _write_timeline(job: dict, status: str) -> None:
    """Slice 4 — upsert one unified comm-timeline row (interactions collection)
    per job on terminal status, so the send appears on the talent / CRM profile.
    Keyed by job_id (idempotent). Derives the subject from the job's recipient."""
    db = get_db()
    kind = job.get("recipient_kind") or ("TALENT" if job.get("talent_id") else "")
    rid = job.get("recipient_id") or job.get("talent_id")
    if not kind or not rid:
        return
    doc = {
        "subject_type": kind,
        "subject_id": str(rid),
        "type": "whatsapp",
        "channel": "whatsapp",
        "direction": "out",
        "template_id": job.get("template_id"),
        "template_name": job.get("template_name"),
        "status": status,
        "batch_id": job.get("batch_id"),
        "job_id": job.get("id"),
        "destination": job.get("destination"),
        "destination_type": job.get("destination_type"),
        "preview": (job.get("message_body") or "")[:120],
        "created_at": _utcnow(),
    }
    if kind == "CRM_CLIENT":
        # Back-compat: existing CRM reads query by client_id (ObjectId).
        try:
            from bson import ObjectId
            doc["client_id"] = ObjectId(str(rid))
        except Exception:
            pass
    try:
        await db.interactions.update_one(
            {"job_id": job.get("id")}, {"$set": doc}, upsert=True
        )
    except Exception as exc:
        logger.warning("worker: timeline write failed for job %s: %s", job.get("id"), exc)


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
                unconfirmed = await db.whatsapp_jobs.count_documents(
                    {"batch_id": batch_id, "status": "sent_unverified"}
                )

                await db.whatsapp_batches.update_one(
                    {"id": batch_id},
                    {
                        "$set": {
                            "status": "completed",
                            "completed_at": now_str,
                            "sent_count": sent,          # VERIFIED only
                            "failed_count": failed,
                            "unconfirmed_count": unconfirmed,  # DELIVERY_UNCONFIRMED — NOT counted as verified
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

    # Perform actual send. send_whatsapp_message returns a DISTINCT outcome state
    # (TASK 4); only genuinely-unsent states are retried (TASK 3 — no duplicates).
    state = None
    error_msg = None

    try:
        state = await send_whatsapp_message(
            page=session.page,
            destination_type=job["destination_type"],
            destination=job["destination"],
            message_body=job["message_body"],
            media_url=job.get("media_url"),
        )
    except ValueError as exc:
        # Bad number / group not found — permanent, never retry.
        state = "INVALID_DESTINATION"
        error_msg = str(exc)
        logger.error("worker: invalid destination for job %s: %s", job_id, error_msg)
    except PlaywrightTimeoutError as exc:
        # A Playwright action timed out (e.g. a selector never resolved). These are
        # PRE-SEND failures — nothing was delivered — so classify as retryable
        # MESSAGE_NOT_SENT and NEVER as sent_unverified.
        state = "MESSAGE_NOT_SENT"
        error_msg = str(exc)
        logger.error("worker: pre-send Playwright timeout for job %s (retryable, not sent): %s",
                     job_id, error_msg)
    except Exception as exc:
        # Unknown error after we may have already typed/sent — treat conservatively
        # as sent-but-unverified so we DO NOT retry and risk a duplicate delivery.
        state = "MESSAGE_SENT_BUT_NOT_VERIFIED"
        error_msg = str(exc)
        logger.error("worker: unexpected send error for job %s (no retry, avoid dup): %s",
                     job_id, error_msg)

    logger.info("worker: job %s outcome state=%s", job_id, state)
    finish_now = _utcnow()

    if state == "MESSAGE_SENT_AND_VERIFIED":
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
        # Slice 4: unified comm timeline (talent / CRM profile).
        await _write_timeline(job, "sent")

        # Apply randomized human-like delay
        logger.info("worker: sleeping for %.2f seconds before next job...", delay)
        await asyncio.sleep(delay)
        
    else:
        # TASK 3/4: retry ONLY states we are confident were NOT delivered.
        # MESSAGE_SENT_BUT_NOT_VERIFIED and INVALID_DESTINATION are TERMINAL —
        # retrying a possibly-delivered message is exactly what duplicated Sahal.
        attempt_count = job["attempt_count"] + 1
        retryable_state = state in ("CHAT_NOT_OPENED", "MESSAGE_NOT_SENT")
        is_retryable = retryable_state and attempt_count < max_retries
        err = error_msg or state

        if is_retryable:
            new_status = "pending"            # safe to re-send (message never left)
        elif state == "MESSAGE_SENT_BUT_NOT_VERIFIED":
            new_status = "sent_unverified"    # TERMINAL — do NOT resend (no duplicate)
        else:
            new_status = "failed"             # INVALID_DESTINATION, or retries exhausted

        await db.whatsapp_jobs.update_one(
            {"id": job_id},
            {
                "$set": {
                    "status": new_status,
                    "outcome_state": state,
                    "error_message": err,
                    "attempt_count": attempt_count,
                    "last_attempted_at": finish_now,
                    "worker_picked_at": None,  # clear claim so a retryable job can be re-picked
                }
            }
        )

        if new_status == "failed":
            await db.whatsapp_batches.update_one(
                {"id": batch_id},
                {"$inc": {"failed_count": 1}}
            )
        elif new_status == "sent_unverified":
            # DELIVERY_UNCONFIRMED — counted separately, never as verified/sent.
            await db.whatsapp_batches.update_one(
                {"id": batch_id},
                {"$inc": {"unconfirmed_count": 1}}
            )

        await write_audit_log(
            "message_sent_unverified" if new_status == "sent_unverified" else "message_failed",
            batch_id=batch_id,
            job_id=job_id,
            talent_id=job["talent_id"],
            talent_name=job["talent_name"],
            destination=job["destination"],
            destination_type=job["destination_type"],
            message_preview=job["message_body"],
            metadata={"state": state, "error": err, "attempt": attempt_count, "will_retry": is_retryable},
        )
        # Slice 4: record terminal outcomes on the timeline (not retryable 'pending').
        if new_status in ("sent_unverified", "failed"):
            await _write_timeline(job, new_status)

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


async def _maybe_reset_session() -> None:
    """Honor an admin-requested session reset BEFORE the browser launches.

    An admin sets `reset_requested=True` on the singleton whatsapp_sessions
    doc (via POST /api/whatsapp/session/reset). On the next (re)start the
    worker wipes the persisted Chromium profile in config.SESSION_DIR so
    WhatsApp Web falls back to a fresh QR linking screen. Runs before
    session.start(), so it recovers even when a corrupt session crash-loops
    the worker. The mount point itself is preserved — only its contents are
    cleared — and the flag is cleared so the wipe happens exactly once.
    """
    db = get_db()
    doc = await db.whatsapp_sessions.find_one({"id": "default"})
    if not doc or not doc.get("reset_requested"):
        return

    logger.warning("worker: session reset requested — clearing %s", config.SESSION_DIR)
    try:
        if os.path.isdir(config.SESSION_DIR):
            before = os.listdir(config.SESSION_DIR)
            logger.info("worker: %d entries in session dir before wipe: %s",
                        len(before), before)
            for entry in before:
                path = os.path.join(config.SESSION_DIR, entry)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            # Verify the wipe actually emptied the user_data_dir.
            after = os.listdir(config.SESSION_DIR)
            if after:
                logger.error("worker: session dir NOT fully cleared — %d entries remain: %s",
                             len(after), after)
            else:
                logger.info("worker: session directory verified empty — a fresh QR will be generated")
        else:
            logger.info("worker: session dir %s does not exist yet — nothing to clear",
                        config.SESSION_DIR)
    except Exception as exc:
        logger.error("worker: failed to clear session dir: %s", exc)

    await db.whatsapp_sessions.update_one(
        {"id": "default"},
        {"$set": {"reset_requested": False, "status": "qr_pending", "error_message": None}},
    )


async def main() -> None:
    """Main execution lifecycle."""
    logger.info("worker: initializing database connection...")
    await init_db()

    # Honor a pending admin reset before touching the browser/session.
    await _maybe_reset_session()

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

    # Startup DOM health check — log which registry selectors resolve on the live
    # WhatsApp DOM so verification selector drift is visible immediately.
    try:
        import sender
        await sender.dom_health_check(session.page, "startup")
    except Exception as e:
        logger.warning("worker: startup DOM health check failed: %s", e)

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
