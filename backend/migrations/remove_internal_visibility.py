"""Issue #2/#3 — collapse the 3-state media visibility model to Client / Hidden.

The recruiter-facing visibility model used to be Client / Hidden / Internal,
stored on media as two flags: `client_visible` (bool) and `internal_only`
(bool). "Internal" and "Hidden" both mean "not visible to the client", so the
model is simplified to a single `client_visible` boolean.

This migration is idempotent and non-destructive to media itself. It only:
  • folds `internal_only: true`  -> `client_visible: false`
  • removes the now-deprecated `internal_only` field wherever it appears:
      - db.submissions.media[]
      - db.submissions.talent_media_visibility.<mid>{}
      - db.talents.media[]

No media is deleted; no client-visible media is hidden that wasn't already
hidden (internal was already excluded from every client surface).

Usage (from /app/backend):
    python -m migrations.remove_internal_visibility --dry-run   # preview
    python -m migrations.remove_internal_visibility             # apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402


def _fold_media_list(media: list) -> tuple[list, int]:
    """Return (new_media, changed_count). Folds internal_only into client_visible."""
    changed = 0
    out = []
    for m in media or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if "internal_only" in m:
            nm = dict(m)
            if nm.pop("internal_only", None) is True:
                nm["client_visible"] = False
            elif "client_visible" not in nm:
                nm["client_visible"] = True
            out.append(nm)
            changed += 1
        else:
            out.append(m)
    return out, changed


def _fold_visibility_map(vmap: dict) -> tuple[dict, int]:
    """Fold internal_only inside a talent_media_visibility override map."""
    changed = 0
    out = {}
    for mid, ov in (vmap or {}).items():
        if isinstance(ov, dict) and "internal_only" in ov:
            cv = False if ov.get("internal_only") is True else ov.get("client_visible", True)
            out[mid] = {"client_visible": cv}
            changed += 1
        else:
            out[mid] = ov
    return out, changed


async def migrate_submissions(dry_run: bool) -> tuple[int, int]:
    docs_changed = 0
    fields_changed = 0
    cursor = db.submissions.find(
        {"$or": [
            {"media.internal_only": {"$exists": True}},
            {"talent_media_visibility": {"$exists": True}},
        ]},
        {"_id": 1, "id": 1, "media": 1, "talent_media_visibility": 1},
    )
    async for sub in cursor:
        update = {}
        new_media, mc = _fold_media_list(sub.get("media") or [])
        if mc:
            update["media"] = new_media
            fields_changed += mc
        if sub.get("talent_media_visibility"):
            new_tmv, tc = _fold_visibility_map(sub["talent_media_visibility"])
            if tc:
                update["talent_media_visibility"] = new_tmv
                fields_changed += tc
        if update:
            docs_changed += 1
            if not dry_run:
                await db.submissions.update_one({"_id": sub["_id"]}, {"$set": update})
    return docs_changed, fields_changed


async def migrate_talents(dry_run: bool) -> tuple[int, int]:
    docs_changed = 0
    fields_changed = 0
    cursor = db.talents.find(
        {"media.internal_only": {"$exists": True}},
        {"_id": 1, "id": 1, "media": 1},
    )
    async for t in cursor:
        new_media, mc = _fold_media_list(t.get("media") or [])
        if mc:
            docs_changed += 1
            fields_changed += mc
            if not dry_run:
                await db.talents.update_one({"_id": t["_id"]}, {"$set": {"media": new_media}})
    return docs_changed, fields_changed


async def main(dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"[remove_internal_visibility] mode={mode}")
    sd, sf = await migrate_submissions(dry_run)
    td, tf = await migrate_talents(dry_run)
    print(f"  submissions: {sd} docs updated, {sf} media/override entries folded")
    print(f"  talents:     {td} docs updated, {tf} media entries folded")
    print("[remove_internal_visibility] done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
