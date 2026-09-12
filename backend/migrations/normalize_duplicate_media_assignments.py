"""Normalize duplicate ACTIVE media_assignments rows for the same
talent_id + project_id + media_slot (media_role, take_number).

Production incident (2026-09-12, Ameya Saawant / "SINGLETON with shruti
hassan Intro has been marked twice, pointing to two different source
media messages"): record_assignment's own unique index is keyed on
source identity (talent_id, project_id, source_message_id,
source_thumbnail_hash), not slot identity — so before the accompanying
code fix (agents/modules/media_assignment.record_assignment now
supersedes a conflicting prior row on every NEW insert going forward),
two DIFFERENT marks for the SAME slot could sit side by side forever as
two independent "marked" (or "resolving"/"failed") rows, both looking
active. This script is the one-time cleanup for whatever duplicate
state already exists from BEFORE that fix, safe to run any number of
times.

Rule (matches record_assignment's own new supersede logic exactly):
for each (talent_id, project_id, media_role, take_number) with
media_role in ("take", "intro") — "photos" deliberately excluded, see
media_assignment.slot_key's own docstring: every photo is its own slot,
never collides — group all rows currently in a NON-terminal-non-
superseded state (marked/resolving/failed). If more than one DISTINCT
source (source_message_id, source_thumbnail_hash) is active for that
slot, keep the NEWEST (by created_at) as-is and mark every OTHER one
superseded. A row already assignment_status="uploaded" is NEVER touched
by this script — that media is already live on a submission; only a
future, explicit operation should touch it, never an automated cleanup.

Usage (from backend/):
    python -m migrations.normalize_duplicate_media_assignments --dry-run
    python -m migrations.normalize_duplicate_media_assignments --apply

Outputs a JSON report to migrations/reports/ either way — dry-run shows
exactly what WOULD change; apply performs the writes and reports what
DID change. Never deletes anything; every affected row is only ever
updated to assignment_status="superseded" with full audit fields
(superseded_at, superseded_by_assignment_id, replaced_reason,
updated_from_source_message_id) — the same fields and semantics the
accompanying code fix writes for future duplicates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db  # noqa: E402
from agents.modules.media_assignment import (  # noqa: E402
    ASSIGNMENTS_COLLECTION, ASSIGN_STATUS_UPLOADED, ASSIGN_STATUS_SUPERSEDED,
)

REPORT_DIR = Path(__file__).parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def find_duplicate_active_slots() -> List[Dict[str, Any]]:
    """Returns one entry per conflicting slot: {talent_id, project_id,
    media_role, take_number, keep (the row to keep, newest by
    created_at), supersede (every other active row for that slot)}."""
    rows = await db[ASSIGNMENTS_COLLECTION].find(
        {
            "media_role": {"$in": ["take", "intro"]},
            "assignment_status": {"$nin": [ASSIGN_STATUS_UPLOADED, ASSIGN_STATUS_SUPERSEDED]},
        },
        {"_id": 0},
    ).to_list(None)

    by_slot: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        slot = (r.get("talent_id"), r.get("project_id"), r.get("media_role"), r.get("take_number"))
        by_slot[slot].append(r)

    conflicts = []
    for slot, slot_rows in by_slot.items():
        distinct_sources = {(r.get("source_message_id"), r.get("source_thumbnail_hash")) for r in slot_rows}
        if len(distinct_sources) <= 1:
            continue  # same source repeated (or only one row) — not a conflict
        slot_rows.sort(key=lambda r: r.get("created_at") or _now(), reverse=True)
        keep = slot_rows[0]
        supersede = slot_rows[1:]
        conflicts.append({
            "talent_id": slot[0], "project_id": slot[1], "media_role": slot[2], "take_number": slot[3],
            "keep": keep, "supersede": supersede,
        })
    return conflicts


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


async def run(apply: bool) -> Dict[str, Any]:
    conflicts = await find_duplicate_active_slots()
    report: Dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "generated_at": _now().isoformat(),
        "conflicting_slots": len(conflicts),
        "details": [],
    }
    for c in conflicts:
        keep = c["keep"]
        entry = {
            "talent_id": c["talent_id"], "project_id": c["project_id"],
            "media_role": c["media_role"], "take_number": c["take_number"],
            "kept_assignment_id": keep.get("assignment_id"),
            "kept_source_message_id": keep.get("source_message_id"),
            "kept_created_at": keep.get("created_at"),
            "superseded": [
                {
                    "assignment_id": s.get("assignment_id"),
                    "source_message_id": s.get("source_message_id"),
                    "created_at": s.get("created_at"),
                }
                for s in c["supersede"]
            ],
        }
        report["details"].append(entry)
        if apply:
            for s in c["supersede"]:
                await db[ASSIGNMENTS_COLLECTION].update_one(
                    {"assignment_id": s["assignment_id"]},
                    {"$set": {
                        "assignment_status": ASSIGN_STATUS_SUPERSEDED,
                        "superseded_at": _now(),
                        "superseded_by_assignment_id": keep.get("assignment_id"),
                        "replaced_reason": "migration_normalize_duplicate_active_slot",
                        "updated_from_source_message_id": keep.get("source_message_id"),
                    }},
                )

    ts = _now().strftime("%Y%m%d_%H%M%S")
    suffix = "apply" if apply else "dryrun"
    out_path = REPORT_DIR / f"media_assignments_dedup_{suffix}_{ts}.json"
    out_path.write_text(json.dumps(_json_safe(report), indent=2))
    report["report_path"] = str(out_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    group.add_argument("--apply", action="store_true", help="Perform the supersede updates")
    args = parser.parse_args()

    report = asyncio.run(run(apply=args.apply))
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, indent=2))
    print(f"\nFull report written to: {report['report_path']}")
    if not args.apply and report["conflicting_slots"] > 0:
        print(f"\n{report['conflicting_slots']} conflicting slot(s) found. Re-run with --apply to normalize.")


if __name__ == "__main__":
    main()
