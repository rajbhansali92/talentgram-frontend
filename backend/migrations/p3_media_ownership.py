"""P3 — additive media-ownership model backfill (Cloudinary rearchitecture).

Adds exactly ONE nested key, ``media[i].ownership``, to every media item in
``db.talents.media[]`` / ``db.submissions.media[]`` / ``db.applications.media[]``.
NOTHING else is touched — no existing field is modified, no Cloudinary asset is
created / moved / re-uploaded / deleted, no ``url`` or ``public_id`` changes, no
document or media item is removed.

Design + rationale: docs/CLOUDINARY_P3_OWNERSHIP_SCHEMA.md

Safety:
  * --dry-run (default) does ZERO writes — prints the P3 report and writes a
    JSON report file, nothing else.
  * --apply snapshots each document's CURRENT ``media`` array into
    ``db.p3_ownership_migration_backup`` BEFORE writing it (exact rollback).
  * Idempotent / resumable: an item that already has
    ``ownership.migration_version == "p3-v1"`` is left untouched.
  * Items with a ``conflict`` are NEVER assigned an owner_type — reported and
    skipped. Ambiguity is preserved, never guessed.

Rollback:  python3 migrations/p3_media_ownership.py --rollback

Usage (target production via Railway):
  railway run -- python3 backend/migrations/p3_media_ownership.py --dry-run
  railway run -- python3 backend/migrations/p3_media_ownership.py --apply
  railway run -- python3 backend/migrations/p3_media_ownership.py --rollback
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, normalize_email, resolve_canonical_talent  # noqa: E402
# Classification logic lives in a side-effect-free module (no `core` import, no
# DB, no Cloudinary, no env) so it can be unit-tested in isolation.
from migrations.media_ownership_rules import (  # noqa: E402
    VERSION,
    TAKE_CATEGORIES,
    CATEGORY_NORMALIZE,
    CATEGORY_TO_NORMALIZED,
    classify_item,
    folder_disagrees,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("p3_media_ownership")

BACKUP_COLL = "p3_ownership_migration_backup"


async def _resolve_talent_id(coll: str, parent: dict, item: dict, cache: dict):
    """(talent_id, how). Never raises."""
    if coll == "talents":
        return parent.get("id"), "talents_doc"
    if item.get("talent_id"):
        return item["talent_id"], "item.talent_id"
    if parent.get("talent_id"):
        return parent["talent_id"], "parent.talent_id"
    email = parent.get("talent_email") or item.get("talent_email")
    if email:
        key = normalize_email(email)
        if key in cache:
            return cache[key], "talent_email"
        t = await resolve_canonical_talent(email=email)
        tid = t.get("id") if t else None
        cache[key] = tid
        if tid:
            return tid, "talent_email"
    src_sub = item.get("source_submission_id")
    if src_sub:
        s = await db.submissions.find_one({"id": src_sub}, {"_id": 0, "talent_id": 1, "talent_email": 1})
        if s and s.get("talent_id"):
            return s["talent_id"], "source_submission"
        if s and s.get("talent_email"):
            t = await resolve_canonical_talent(email=s["talent_email"])
            if t:
                return t["id"], "source_submission_email"
    return None, "unresolved"


async def do_migrate(apply: bool) -> dict:
    # pass 1 — public_id → owner count + normalized categories
    pid_owner_count: Counter = Counter()
    pid_norm_cats: dict = defaultdict(set)
    for coll in ("talents", "submissions", "applications"):
        async for d in db[coll].find({"media": {"$exists": True, "$ne": []}}, {"_id": 0, "media": 1}):
            for m in d.get("media") or []:
                pid = m.get("public_id")
                if pid:
                    pid_owner_count[pid] += 1
                    nc = CATEGORY_TO_NORMALIZED.get(m.get("category"))
                    if nc:
                        pid_norm_cats[pid].add(nc)

    stats = Counter()
    by_owner_type = Counter()
    by_confidence = Counter()
    conflicts, needs_inference, missing_refs, folder_db = [], [], [], []
    cache: dict = {}
    docs_touched = items_assigned = items_skipped = 0

    for coll in ("talents", "submissions", "applications"):
        async for parent in db[coll].find({"media": {"$exists": True, "$ne": []}}):
            media = parent.get("media") or []
            new_media, doc_changed = [], False
            for item in media:
                stats["items_total"] += 1
                existing = item.get("ownership")
                if isinstance(existing, dict) and existing.get("migration_version") == VERSION:
                    items_skipped += 1
                    new_media.append(item)
                    continue

                talent_id, how = await _resolve_talent_id(coll, parent, item, cache)
                ownership = classify_item(coll, parent, item, talent_id, how, pid_owner_count, pid_norm_cats)

                if ownership["conflict"]:
                    conflicts.append({"collection": coll, "doc_id": parent.get("id"), "media_id": item.get("id"),
                                      "category": item.get("category"), "public_id": item.get("public_id"),
                                      "reason": ownership["conflict"]})
                    stats["items_conflict"] += 1
                    if "talent_id" in ownership["conflict"]:
                        needs_inference.append({"collection": coll, "doc_id": parent.get("id"),
                                                "media_id": item.get("id"), "category": item.get("category")})
                else:
                    by_owner_type[ownership["owner_type"]] += 1
                    by_confidence[ownership["confidence"]] += 1
                    items_assigned += 1

                if not item.get("public_id") and not item.get("url"):
                    missing_refs.append({"collection": coll, "doc_id": parent.get("id"), "media_id": item.get("id")})
                d = folder_disagrees(ownership)
                if d:
                    folder_db.append({"collection": coll, "doc_id": parent.get("id"), "media_id": item.get("id"), **d})

                new_media.append({**item, "ownership": ownership})
                doc_changed = True

            if doc_changed:
                docs_touched += 1
                if apply:
                    await db[BACKUP_COLL].update_one(
                        {"collection": coll, "doc_id": parent.get("id")},
                        {"$setOnInsert": {"collection": coll, "doc_id": parent.get("id"),
                                          "media_before": media,
                                          "backed_up_at": datetime.now(timezone.utc).isoformat()}},
                        upsert=True,
                    )
                    await db[coll].update_one({"id": parent.get("id")}, {"$set": {"media": new_media}})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "APPLY" if apply else "DRY-RUN",
        "migration_version": VERSION,
        "totals": {
            "media_items_total": stats["items_total"],
            "items_assigned_owner_type": items_assigned,
            "items_conflict_left_unassigned": stats["items_conflict"],
            "items_skipped_already_migrated": items_skipped,
            "documents_touched": docs_touched,
        },
        "assignable_GLOBAL_TALENT_MEDIA": by_owner_type.get("talent", 0),
        "assignable_PROJECT_AUDITION_MEDIA": by_owner_type.get("project_submission", 0),
        "by_confidence": dict(by_confidence),
        "requiring_ownership_inference": len(needs_inference),
        "remaining_UNKNOWN_conflict": len(conflicts),
        "conflicting_references": len(conflicts),
        "missing_mongo_references": len(missing_refs),
        "folder_vs_db_ownership_disagreements": len(folder_db),
        "detail": {
            "conflicts": conflicts[:300],
            "needs_inference": needs_inference[:300],
            "missing_refs": missing_refs[:300],
            "folder_db_disagree_count": len(folder_db),
            "folder_db_disagree_sample": folder_db[:50],
        },
    }


async def do_rollback() -> None:
    n = await db[BACKUP_COLL].count_documents({})
    if n == 0:
        print("No P3 backup found — nothing to roll back.")
        return
    print(f"Rolling back {n} documents from {BACKUP_COLL} ...")
    restored = 0
    async for b in db[BACKUP_COLL].find({}):
        await db[b["collection"]].update_one({"id": b["doc_id"]}, {"$set": {"media": b["media_before"]}})
        restored += 1
    print(f"Restored media[] on {restored} documents. Dropping {BACKUP_COLL} ...")
    await db[BACKUP_COLL].drop()
    print("Rollback complete. P3 fully reverted.")


async def run(apply: bool, rollback: bool) -> None:
    if rollback:
        return await do_rollback()
    report = await do_migrate(apply)
    out = os.path.join(os.path.dirname(__file__), "reports",
                       f"p3_ownership_{'apply' if apply else 'dryrun'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n================ P3 OWNERSHIP MIGRATION — {report['mode']} ================")
    print(json.dumps({k: report[k] for k in (
        "totals", "assignable_GLOBAL_TALENT_MEDIA", "assignable_PROJECT_AUDITION_MEDIA", "by_confidence",
        "requiring_ownership_inference", "remaining_UNKNOWN_conflict", "conflicting_references",
        "missing_mongo_references", "folder_vs_db_ownership_disagreements",
    )}, indent=2))
    if report["detail"]["conflicts"]:
        print("\nCONFLICTS (left UNKNOWN, never guessed):")
        for c in report["detail"]["conflicts"][:25]:
            print(f"  - {c['collection']}/{c['doc_id']} media={c['media_id']} cat={c['category']}: {c['reason']}")
    print(f"\nfull report: {out}")
    if not apply:
        print("\nDRY-RUN — no writes. Re-run with --apply after review.")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(apply=bool(args.apply), rollback=bool(args.rollback)))


if __name__ == "__main__":
    main()
