"""Backfill `height_inches` for existing talents from their free-text `height`.

Safe by construction:
  - Only ever SETS height_inches. Never touches `height` (the original
    display string) and never overwrites an existing height_inches value.
  - Idempotent / resumable: the query that selects candidates
    ({height: exists, height_inches: missing}) naturally shrinks to zero as
    records get updated, so re-running (including after an interruption) is
    always safe and just picks up wherever it left off.
  - --dry-run (default) does zero writes — it only reports what WOULD
    happen, including every string that fails to parse, so failures can be
    reviewed before any write happens.

Usage:
  python3 migrations/backfill_height_inches.py --dry-run   # read-only report
  python3 migrations/backfill_height_inches.py --apply      # actually writes

Uses the same MONGO_URL/DB_NAME as the running app (core.py) — run this via
`railway run python3 migrations/backfill_height_inches.py ...` to target
production, or with a local .env for local Mongo.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, parse_height_to_inches

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("backfill_height_inches")


async def run(apply: bool) -> None:
    total = await db.talents.count_documents({})
    already_populated = await db.talents.count_documents({"height_inches": {"$exists": True}})

    # Candidates: has a real (non-null, non-empty) height string, doesn't yet
    # have height_inches. NOTE: a dict literal can only hold one "$ne" key —
    # {"$ne": None, "$ne": ""} silently collapses to just the second one in
    # Python, which is NOT the same query. Use $nin for both in one op.
    height_has_value = {"$exists": True, "$nin": [None, ""]}
    cursor = db.talents.find(
        {"height": height_has_value, "height_inches": {"$exists": False}},
        {"_id": 1, "id": 1, "name": 1, "height": 1},
    )

    processed = 0
    updated = 0
    failed = []  # (id, name, height) that didn't parse
    skipped_no_height = await db.talents.count_documents(
        {"$and": [{"$or": [{"height": {"$exists": False}}, {"height": None}, {"height": ""}]},
                   {"height_inches": {"$exists": False}}]}
    )

    async for doc in cursor:
        processed += 1
        inches = parse_height_to_inches(doc.get("height"))
        if inches is None:
            failed.append((doc.get("id"), doc.get("name"), doc.get("height")))
            continue
        if apply:
            await db.talents.update_one(
                {"_id": doc["_id"], "height_inches": {"$exists": False}},  # re-check at write time — resumable/safe under concurrent runs
                {"$set": {"height_inches": inches}},
            )
        updated += 1

    mode = "APPLY" if apply else "DRY-RUN"
    logger.info("=" * 60)
    logger.info(f"height_inches backfill [{mode}]")
    logger.info("=" * 60)
    logger.info(f"Total talents in collection:          {total}")
    logger.info(f"Already had height_inches (untouched): {already_populated}")
    logger.info(f"Candidates processed (height, no height_inches): {processed}")
    logger.info(f"Updated (parsed successfully):         {updated}")
    logger.info(f"Skipped (no height value at all):      {skipped_no_height}")
    logger.info(f"Failed (height present, unparsable):   {len(failed)}")
    if failed:
        logger.info("-" * 60)
        logger.info("Unparsable height values (NOT modified, left as-is):")
        for tid, name, height in failed:
            logger.info(f"  id={tid} name={name!r} height={height!r}")
    logger.info("=" * 60)
    if not apply:
        logger.info("DRY RUN — no writes performed. Re-run with --apply to write.")
    else:
        final_populated = await db.talents.count_documents({"height_inches": {"$exists": True}})
        logger.info(f"height_inches now populated on {final_populated}/{total} talents.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    group.add_argument("--apply", action="store_true", help="Actually perform the backfill")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))
