"""TASK 5 — Group-send validation harness.

Enqueues a single live WhatsApp *group* job that the running worker picks up,
so you can watch the new chat-open verification + delivery verification + state
machine against a real group. This does NOT drive Playwright itself — it relies
on the already-authenticated worker session.

Usage (from a machine with MONGO_URL set to the same DB the worker uses):

    MONGO_URL="mongodb+srv://..." python scripts/test_group_send.py \
        --group "Talentgram QA Test"

Then watch the worker logs for:
    sender: CHAT OPEN VERIFICATION ... conversation_ready=True
    sender: OUTGOING DOM DUMP ...
    sender: verify — MATCHED selector=... timestamp=...
    sender: MESSAGE_SENT_AND_VERIFIED
    worker: job <id> outcome state=MESSAGE_SENT_AND_VERIFIED

Verify in the app: GET /api/whatsapp/batches/<batch_id>/jobs shows status=sent.
"""
import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="Talentgram QA Test", help="exact WhatsApp group name")
    ap.add_argument("--db", default=os.environ.get("MONGO_DB_NAME", "talentgram"))
    args = ap.parse_args()

    mongo_url = os.environ["MONGO_URL"]
    client = AsyncIOMotorClient(mongo_url)
    db = client[args.db]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"GROUP TEST {ts}"
    batch_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    now = _now()

    batch = {
        "id": batch_id,
        "project_id": "qa-test",
        "project_name": "QA Group Test",
        "template_id": "qa-test",
        "pipeline_stages": [],
        "variable_data": {},
        "media_url": None,
        "is_dry_run": False,
        "status": "pending",
        "min_delay_sec": 0,
        "max_delay_sec": 1,
        "total_jobs": 1,
        "sent_count": 0,
        "failed_count": 0,
        "skipped_recipients": [],
        "created_by": "qa-script",
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    job = {
        "id": job_id,
        "batch_id": batch_id,
        "talent_id": "qa-test",
        "talent_name": "QA Group",
        "destination_type": "group",
        "destination": args.group,
        "message_body": message,
        "media_url": None,
        "is_dry_run": False,
        "status": "pending",
        "attempt_count": 0,
        "last_attempted_at": None,
        "sent_at": None,
        "error_message": None,
        "worker_picked_at": None,
        "created_at": now,
    }

    await db.whatsapp_batches.insert_one(batch)
    await db.whatsapp_jobs.insert_one(job)
    print(f"Enqueued group test job.\n  batch_id={batch_id}\n  job_id={job_id}\n"
          f"  group={args.group!r}\n  message={message!r}\n"
          f"Watch worker logs, then check job status in whatsapp_jobs / the admin UI.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
