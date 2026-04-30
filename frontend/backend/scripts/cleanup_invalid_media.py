"""Remove media entries that lack a Cloudinary `url`.

After the v37m migration, any media item without a non-empty `url` is
either (a) a legacy item Cloudinary refused (e.g. 1x1 placeholder JPEGs)
or (b) corrupt — neither is renderable by the frontend. This cleanup
purges them from every collection that holds media[] arrays AND clears
dangling cover_media_id references in parent documents.

Run from /app:

    cd /app/backend && python -m scripts.cleanup_invalid_media           # LIVE
    cd /app/backend && python -m scripts.cleanup_invalid_media --dry-run # preview only
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
sys.path.insert(0, str(ROOT_DIR))
from core import db  # noqa: E402

DRY_RUN = "--dry-run" in sys.argv


def _is_invalid(m: dict) -> bool:
    url = (m.get("url") or "").strip()
    return not url


COLLECTIONS = [
    ("talents", "media"),
    ("submissions", "media"),
    ("applications", "media"),
    ("projects", "materials"),
]


async def main() -> None:
    print(f"== Cleanup invalid media {'(DRY RUN)' if DRY_RUN else '(LIVE)'} ==")
    grand_total_scanned = grand_total_removed = grand_total_kept = 0
    docs_touched = covers_cleared = 0

    for coll, field in COLLECTIONS:
        scanned = removed = kept = 0
        async for doc in db[coll].find({field: {"$exists": True, "$ne": []}}, {"_id": 0}):
            arr = doc.get(field) or []
            scanned += len(arr)
            invalid_ids = [m.get("id") for m in arr if _is_invalid(m)]
            if not invalid_ids:
                kept += len(arr)
                continue
            valid = [m for m in arr if not _is_invalid(m)]
            kept += len(valid)
            removed += len(invalid_ids)
            update: dict = {field: valid}
            # Clear cover if it pointed to a now-removed media item.
            cover = doc.get("cover_media_id")
            if cover and cover in invalid_ids:
                update["cover_media_id"] = None
                covers_cleared += 1
            print(
                f"  [{coll}/{doc.get('id')}] removing {len(invalid_ids)} → keep {len(valid)}"
                + ("  (cover cleared)" if "cover_media_id" in update else "")
            )
            if not DRY_RUN:
                await db[coll].update_one({"id": doc.get("id")}, {"$set": update})
                docs_touched += 1
        print(f"== {coll}.{field}: scanned={scanned} removed={removed} kept={kept} ==")
        grand_total_scanned += scanned
        grand_total_removed += removed
        grand_total_kept += kept

    # Feedback: a single content_url field per doc, no array.
    fb_scanned = fb_removed = 0
    async for doc in db.feedback.find({}, {"_id": 0, "id": 1, "content_url": 1, "type": 1}):
        fb_scanned += 1
        if doc.get("type") == "voice" and not (doc.get("content_url") or "").strip():
            print(f"  [feedback/{doc.get('id')}] removing voice doc with no content_url")
            fb_removed += 1
            if not DRY_RUN:
                await db.feedback.delete_one({"id": doc.get("id")})
    print(f"== feedback: scanned={fb_scanned} removed={fb_removed} ==")

    print()
    print("================ SUMMARY ================")
    print(f"Mode:                {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"Media items scanned: {grand_total_scanned}")
    print(f"Media items removed: {grand_total_removed}")
    print(f"Media items kept:    {grand_total_kept}")
    print(f"Docs updated:        {docs_touched}")
    print(f"Covers cleared:      {covers_cleared}")
    print(f"Feedback removed:    {fb_removed}")
    print("=========================================")


if __name__ == "__main__":
    asyncio.run(main())
