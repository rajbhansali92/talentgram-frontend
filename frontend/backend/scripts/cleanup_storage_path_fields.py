"""Strip legacy `storage_path` and `resized_storage_path` fields from all
media items in the DB. Idempotent — safe to re-run.

Run from /app:

    cd /app/backend && python -m scripts.cleanup_storage_path_fields
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
sys.path.insert(0, str(ROOT_DIR))
from core import db  # noqa: E402

DRY_RUN = "--dry-run" in sys.argv


COLLECTIONS = [
    ("talents", "media"),
    ("submissions", "media"),
    ("applications", "media"),
    ("projects", "materials"),
]

LEGACY_KEYS = ("storage_path", "resized_storage_path", "resized_size")


async def main() -> None:
    print(f"== Strip legacy storage fields {'(DRY RUN)' if DRY_RUN else '(LIVE)'} ==")
    total_docs = total_items = total_stripped = 0
    for coll, field in COLLECTIONS:
        coll_docs = coll_items = coll_stripped = 0
        async for doc in db[coll].find({field: {"$exists": True, "$ne": []}}, {"_id": 0}):
            arr = doc.get(field) or []
            coll_items += len(arr)
            new_arr = []
            mutated = False
            for m in arr:
                stripped_count = sum(1 for k in LEGACY_KEYS if k in m)
                if stripped_count:
                    mutated = True
                    coll_stripped += stripped_count
                    new_arr.append({k: v for k, v in m.items() if k not in LEGACY_KEYS})
                else:
                    new_arr.append(m)
            if mutated:
                coll_docs += 1
                if not DRY_RUN:
                    await db[coll].update_one({"id": doc.get("id")}, {"$set": {field: new_arr}})
        print(f"  {coll}.{field}: docs touched={coll_docs}, items={coll_items}, fields stripped={coll_stripped}")
        total_docs += coll_docs
        total_items += coll_items
        total_stripped += coll_stripped

    # Drive failure queue may also carry storage_path
    drive_stripped = 0
    async for doc in db.drive_upload_failures.find({"storage_path": {"$exists": True}}, {"_id": 0, "media_id": 1}):
        drive_stripped += 1
        if not DRY_RUN:
            await db.drive_upload_failures.update_one(
                {"media_id": doc.get("media_id")},
                {"$unset": {"storage_path": ""}},
            )
    print(f"  drive_upload_failures: storage_path unset on {drive_stripped} docs")

    print()
    print("================ SUMMARY ================")
    print(f"Mode:                 {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"Total media items:    {total_items}")
    print(f"Docs touched:         {total_docs}")
    print(f"Legacy fields stripped: {total_stripped}")
    print("=========================================")


if __name__ == "__main__":
    asyncio.run(main())
