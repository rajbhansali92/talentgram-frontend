"""Phase 0 — Dedup + Standardise + Index hardening.

Run ONCE before adding DB-level unique indexes.

Usage (from /app/backend):
    python -m migrations.phase0_dedup --dry-run        # preview
    python -m migrations.phase0_dedup                  # apply
    python -m migrations.phase0_dedup --skip-csv       # apply without report

Outputs:
    /app/backend/migrations/reports/phase0_<timestamp>.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow running as a script from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_email(e: Any) -> Optional[str]:
    if not e or not isinstance(e, str):
        return None
    e = e.strip().lower()
    return e or None


# ---------------------------------------------------------------------------
# 1. Talents — group by email, merge into oldest, delete the rest.
# ---------------------------------------------------------------------------
async def dedup_talents(dry_run: bool, rows: List[List[str]]) -> Tuple[int, int]:
    """Returns (duplicates_before, duplicates_after)."""
    cursor = db.talents.find({}, {"_id": 0})
    by_email: Dict[str, List[dict]] = {}
    no_email: List[dict] = []
    async for t in cursor:
        e = _norm_email(t.get("email"))
        if e:
            by_email.setdefault(e, []).append(t)
        else:
            no_email.append(t)

    # Also bucket by "source.talent_email" for any talent whose top-level
    # email is empty but the source carries the email (legacy / merged rows).
    leftover: List[dict] = []
    for t in no_email:
        src = t.get("source") or {}
        e = _norm_email(src.get("talent_email") if isinstance(src, dict) else None)
        if e:
            by_email.setdefault(e, []).append(t)
        else:
            leftover.append(t)

    dup_before = sum(1 for v in by_email.values() if len(v) > 1)

    for email, group in by_email.items():
        if len(group) < 2:
            continue
        # Sort: oldest first (created_at asc) — keep the oldest, merge others into it.
        group.sort(key=lambda x: x.get("created_at") or "")
        keeper = group[0]
        kid = keeper["id"]
        merged_media = list(keeper.get("media") or [])
        merged_media_ids = {m["id"] for m in merged_media if m.get("id")}
        update: Dict[str, Any] = {}
        # Keep the lower-cased email canonical
        if keeper.get("email") != email:
            update["email"] = email
        for dup in group[1:]:
            # 1. Merge media — only when it doesn't already exist.
            for m in (dup.get("media") or []):
                if m.get("id") and m["id"] not in merged_media_ids:
                    merged_media.append(m)
                    merged_media_ids.add(m["id"])
            # 2. Fill empty fields ONLY (never overwrite existing data).
            for key in (
                "name", "phone", "age", "dob", "height", "location",
                "ethnicity", "gender", "instagram_handle", "instagram_followers",
                "bio", "cover_media_id",
            ):
                if not keeper.get(key) and dup.get(key) and not update.get(key):
                    update[key] = dup[key]
            # 3. Merge work_links uniquely
            if dup.get("work_links"):
                cur = update.get("work_links") or list(keeper.get("work_links") or [])
                for w in dup["work_links"]:
                    if w not in cur:
                        cur.append(w)
                update["work_links"] = cur

        if merged_media != keeper.get("media"):
            update["media"] = merged_media

        merged_ids = [d["id"] for d in group[1:]]
        rows.append([
            "talents", email, ",".join(merged_ids), kid, _now_iso(),
            f"merged {len(merged_ids)} duplicates"
        ])
        if not dry_run:
            if update:
                await db.talents.update_one({"id": kid}, {"$set": update})
            await db.talents.delete_many({"id": {"$in": merged_ids}})

    return dup_before, 0


# ---------------------------------------------------------------------------
# 2. Standardise `source` shape on every talent.
#    Old: source = "audition_submission"   (plain string)
#    New: source = {"type": "audition_submission", "talent_email": "...", "reference_id": null}
# ---------------------------------------------------------------------------
async def standardise_source(dry_run: bool, rows: List[List[str]]) -> int:
    fixed = 0
    cursor = db.talents.find({}, {"_id": 0, "id": 1, "email": 1, "source": 1})
    async for t in cursor:
        cur = t.get("source")
        if isinstance(cur, dict) and cur.get("type"):
            continue  # already standardised
        new_src: Dict[str, Any] = {
            "type": "admin",
            "talent_email": _norm_email(t.get("email")),
            "reference_id": None,
        }
        if isinstance(cur, str):
            # legacy string form
            new_src["type"] = cur
        elif isinstance(cur, dict):
            # legacy partial shape
            new_src["type"] = cur.get("type") or "admin"
            new_src["reference_id"] = (
                cur.get("application_id")
                or cur.get("submission_id")
                or cur.get("reference_id")
            )
            new_src["talent_email"] = (
                _norm_email(cur.get("talent_email")) or new_src["talent_email"]
            )
        rows.append([
            "talents.source", new_src["talent_email"] or "", t["id"], t["id"], _now_iso(),
            f"normalised source -> {new_src['type']}",
        ])
        fixed += 1
        if not dry_run:
            await db.talents.update_one(
                {"id": t["id"]}, {"$set": {"source": new_src}}
            )
    return fixed


# ---------------------------------------------------------------------------
# 3. Applications — one per email. If multiple, keep the most-progressed
#    (submitted > draft) AND newest among that bucket.
# ---------------------------------------------------------------------------
async def dedup_applications(dry_run: bool, rows: List[List[str]]) -> int:
    cursor = db.applications.find({}, {"_id": 0})
    by_email: Dict[str, List[dict]] = {}
    async for a in cursor:
        e = _norm_email(a.get("talent_email"))
        if not e:
            continue
        by_email.setdefault(e, []).append(a)
    removed = 0
    for email, group in by_email.items():
        if len(group) < 2:
            continue

        def _rank(a):
            # submitted > draft, then newest first
            status_rank = 1 if a.get("status") == "submitted" else 0
            return (status_rank, a.get("created_at") or "")

        group.sort(key=_rank, reverse=True)
        keeper = group[0]
        merged_ids = [d["id"] for d in group[1:]]
        rows.append([
            "applications", email, ",".join(merged_ids), keeper["id"], _now_iso(),
            f"deleted {len(merged_ids)} older drafts",
        ])
        removed += len(merged_ids)
        if not dry_run:
            await db.applications.delete_many({"id": {"$in": merged_ids}})
        # Lower-case the email on the keeper so the unique index sticks
        if not dry_run and keeper.get("talent_email") != email:
            await db.applications.update_one(
                {"id": keeper["id"]}, {"$set": {"talent_email": email}}
            )
    return removed


# ---------------------------------------------------------------------------
# 4. Submissions — unique (project_id, talent_email). Same rule.
# ---------------------------------------------------------------------------
async def dedup_submissions(dry_run: bool, rows: List[List[str]]) -> int:
    cursor = db.submissions.find({}, {"_id": 0})
    by_pair: Dict[Tuple[str, str], List[dict]] = {}
    async for s in cursor:
        e = _norm_email(s.get("talent_email"))
        pid = s.get("project_id")
        if not e or not pid:
            continue
        by_pair.setdefault((pid, e), []).append(s)
    removed = 0
    for (pid, email), group in by_pair.items():
        if len(group) < 2:
            continue

        def _rank(s):
            sr = {"updated": 3, "submitted": 2, "draft": 1}.get(s.get("status"), 0)
            return (sr, s.get("submitted_at") or s.get("created_at") or "")

        group.sort(key=_rank, reverse=True)
        keeper = group[0]
        merged_ids = [d["id"] for d in group[1:]]
        rows.append([
            "submissions", f"{pid}|{email}", ",".join(merged_ids), keeper["id"], _now_iso(),
            f"deleted {len(merged_ids)} older",
        ])
        removed += len(merged_ids)
        if not dry_run:
            await db.submissions.delete_many({"id": {"$in": merged_ids}})
        if not dry_run and keeper.get("talent_email") != email:
            await db.submissions.update_one(
                {"id": keeper["id"]}, {"$set": {"talent_email": email}}
            )
    return removed


# ---------------------------------------------------------------------------
# 5. Indexes — only after dedup succeeds.
# ---------------------------------------------------------------------------
async def add_unique_indexes(dry_run: bool, rows: List[List[str]]) -> int:
    plan = [
        ("talents", "talents_email_unique", [("email", 1)],
         {"unique": True, "partialFilterExpression": {"email": {"$type": "string"}}}),
        ("applications", "applications_email_unique", [("talent_email", 1)],
         {"unique": True}),
        ("submissions", "submissions_project_email_unique",
         [("project_id", 1), ("talent_email", 1)], {"unique": True}),
    ]
    applied = 0
    for coll, name, keys, opts in plan:
        rows.append([
            f"index:{coll}", name, "", "", _now_iso(),
            f"{'PLAN' if dry_run else 'APPLIED'} {keys} unique"
        ])
        if not dry_run:
            try:
                # Drop conflicting non-unique index if it exists
                if coll == "talents":
                    try:
                        await db.talents.drop_index("email_1")
                    except Exception:
                        pass
                await db[coll].create_index(keys, name=name, **opts)
                applied += 1
            except Exception as e:
                rows.append([
                    f"index:{coll}", name, "", "", _now_iso(),
                    f"FAILED: {e}"
                ])
                raise
    return applied


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview only — no writes")
    ap.add_argument("--skip-csv", action="store_true", help="Skip CSV report")
    ap.add_argument("--skip-indexes", action="store_true",
                    help="Skip index creation (run only dedup + standardise)")
    args = ap.parse_args()

    rows: List[List[str]] = [[
        "scope", "key", "merged_ids", "kept_id", "timestamp", "note"
    ]]
    print(f"[phase0] {'DRY RUN' if args.dry_run else 'APPLY'} — starting")

    dup_before, _ = await dedup_talents(args.dry_run, rows)
    print(f"[phase0] talents: duplicate-clusters processed = {dup_before}")

    fixed_src = await standardise_source(args.dry_run, rows)
    print(f"[phase0] talents.source standardised = {fixed_src}")

    rem_apps = await dedup_applications(args.dry_run, rows)
    print(f"[phase0] applications removed = {rem_apps}")

    rem_subs = await dedup_submissions(args.dry_run, rows)
    print(f"[phase0] submissions removed = {rem_subs}")

    if not args.skip_indexes:
        applied = await add_unique_indexes(args.dry_run, rows)
        print(f"[phase0] indexes applied = {applied}")

    if not args.skip_csv:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"phase0_{ts}.csv"
        with open(report_path, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        print(f"[phase0] report -> {report_path}")

    # Verification — count residual duplicates
    print("[phase0] verifying...")
    pipeline = [
        {"$match": {"email": {"$type": "string"}}},
        {"$group": {"_id": "$email", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    talent_dupes = await db.talents.aggregate(pipeline).to_list(1000)
    print(f"[phase0] residual talents email duplicates = {len(talent_dupes)}")

    pipeline_apps = [
        {"$group": {"_id": "$talent_email", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    app_dupes = await db.applications.aggregate(pipeline_apps).to_list(1000)
    print(f"[phase0] residual applications email duplicates = {len(app_dupes)}")

    pipeline_subs = [
        {"$group": {"_id": {"p": "$project_id", "e": "$talent_email"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    sub_dupes = await db.submissions.aggregate(pipeline_subs).to_list(1000)
    print(f"[phase0] residual submissions duplicates = {len(sub_dupes)}")

    print("[phase0] done")


if __name__ == "__main__":
    asyncio.run(main())
