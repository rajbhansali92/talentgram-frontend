"""Slice 4 migration — backfill existing CRM interactions into the polymorphic
comm-timeline shape.

Existing `interactions` rows carry only `client_id` (ObjectId). This sets
`subject_type="CRM_CLIENT"` and `subject_id=str(client_id)` on those rows so the
unified `/whatsapp/timeline` returns them natively (the read endpoint is already
back-compatible via an OR on client_id, so this migration is cleanup, not a
hard dependency).

Idempotent: only touches rows missing `subject_type`.

Run:  MONGO_URL="mongodb+srv://..." python backend/migrations/whatsapp_timeline_polymorphic.py
"""
import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient


async def main() -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "talentgram")]

    cursor = db.interactions.find(
        {"subject_type": {"$exists": False}, "client_id": {"$exists": True}},
        {"_id": 1, "client_id": 1},
    )
    updated = 0
    async for row in cursor:
        await db.interactions.update_one(
            {"_id": row["_id"]},
            {"$set": {"subject_type": "CRM_CLIENT", "subject_id": str(row["client_id"])}},
        )
        updated += 1

    print(f"backfilled {updated} interaction(s) -> subject_type=CRM_CLIENT")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
