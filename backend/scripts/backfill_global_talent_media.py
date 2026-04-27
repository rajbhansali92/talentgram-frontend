"""Idempotent one-time backfill — mirror submission image media into the
global talent record (db.talents[].media[]).

Run once after deploying v37i. Safe to re-run; uses
`source_submission_media_id` to deduplicate.

Usage:
    cd /app/backend && python scripts/backfill_global_talent_media.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SYNC_CATEGORIES = {"image", "indian", "western"}


async def main() -> None:
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    scanned = mirrored = already = no_email = no_talent = 0

    async for sub in db.submissions.find(
        {"media.category": {"$in": list(SYNC_CATEGORIES)}}
    ):
        scanned += 1
        email = (sub.get("talent_email") or "").lower().strip()
        if not email:
            no_email += 1
            continue
        if not await db.talents.find_one({"email": email}, {"id": 1}):
            no_talent += 1
            continue
        for m in sub.get("media") or []:
            if m.get("category") not in SYNC_CATEGORIES:
                continue
            source_id = m.get("id")
            if not source_id:
                continue
            mirror = {
                "id": str(uuid.uuid4()),
                "category": m.get("category"),
                "storage_path": m.get("storage_path"),
                "mime": m.get("mime"),
                "content_type": m.get("content_type"),
                "size": m.get("size"),
                "created_at": m.get("created_at")
                or datetime.now(timezone.utc).isoformat(),
                "scope": "talent",
                "source_submission_id": sub.get("id"),
                "source_submission_media_id": source_id,
            }
            if m.get("resized_storage_path"):
                mirror["resized_storage_path"] = m["resized_storage_path"]
                mirror["resized_size"] = m.get("resized_size")

            res = await db.talents.update_one(
                {
                    "email": email,
                    "media.source_submission_media_id": {"$ne": source_id},
                },
                {"$push": {"media": mirror}},
            )
            if res.modified_count:
                mirrored += 1
            else:
                already += 1

    print("Backfill complete:")
    print(f"  submissions scanned       : {scanned}")
    print(f"  media items mirrored      : {mirrored}")
    print(f"  already mirrored (skipped): {already}")
    print(f"  skipped (no talent_email) : {no_email}")
    print(f"  skipped (no talent record): {no_talent}")


if __name__ == "__main__":
    asyncio.run(main())
