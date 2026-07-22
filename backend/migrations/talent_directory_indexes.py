import os
import sys
import asyncio
import logging
from pymongo import ASCENDING, DESCENDING

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, parse_height_to_inches

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("migrations")


async def backfill_height_inches():
    """One-time backfill: parse every existing talent's free-text `height`
    into the normalized `height_inches` field the new filter/sort engine
    range-queries. Idempotent — only touches docs missing the field, safe to
    re-run (e.g. after adding a talent via a path this migration predates).
    """
    cursor = db.talents.find(
        {"height": {"$exists": True, "$ne": None}, "height_inches": {"$exists": False}},
        {"_id": 1, "height": 1},
    )
    updated = 0
    unparsable = 0
    async for doc in cursor:
        inches = parse_height_to_inches(doc.get("height"))
        if inches is not None:
            await db.talents.update_one({"_id": doc["_id"]}, {"$set": {"height_inches": inches}})
            updated += 1
        else:
            unparsable += 1
    logger.info(f"height_inches backfill: {updated} updated, {unparsable} left unparsable (height text didn't match any known format)")


async def run_migrations():
    logger.info("Starting talent-directory filter engine migrations...")

    await backfill_height_inches()

    # New indexes for the structured filter/sort engine (GET /api/talents).
    # Individual (not compound) — Mongo can intersect single-field indexes
    # for combined filters, and individual indexes stay useful for the many
    # single-criterion queries (e.g. Quick Add's plain search) too.
    new_indexes = [
        [("location.city", ASCENDING)],
        [("location.country", ASCENDING)],
        [("gender", ASCENDING)],
        [("ethnicity", ASCENDING)],
        [("dob", ASCENDING)],
        [("height_inches", ASCENDING)],
        [("instagram_followers", ASCENDING)],
    ]
    for keys in new_indexes:
        try:
            await db.talents.create_index(keys)
            logger.info(f"Created index on talents.{keys}")
        except Exception as e:
            logger.warning(f"talents index {keys}: {e}")

    logger.info("=============================================")
    logger.info("Talent-directory index migration completed!")
    logger.info("=============================================")


if __name__ == "__main__":
    asyncio.run(run_migrations())
