"""Multi-worker support — whatsapp_agent_config worker_id + uniqueness migration.

Before multi-worker support, `whatsapp_agent_config` had a single-field
UNIQUE index on `agent_id` alone: at most one config doc per agent_id, full
stop. That index rejects a second worker's config for an agent_id Worker 1
already has (E11000 duplicate key error), so the real key must become the
COMPOUND (agent_id, worker_id) pair.

Confirmed against production (2026-09-13, via Atlas Data Explorer): all 5
existing `whatsapp_agent_config` documents (crm-agent, casting-agent,
whatsapp-campaign-agent, talentgram-fetcher-agent, management-agent) have NO
`worker_id` field at all, and the obsolete unique index is genuinely named
`agent_id_1`. Given every production document lacks the field, this
migration explicitly BACKFILLS `worker_id: "default"` onto them as its
first phase, rather than relying on "a missing field is treated as default"
application-level semantics (registry.py's worker_match_filter) as the
only safety net — that fallback still exists and still works (see its own
docstring), but an explicit, visible value on every document is safer and
more auditable for a collection this small and this rarely written.

Three staged phases, in this exact order, every time:
  Phase A — BACKFILL: $set worker_id="default" on documents missing it.
            Pure document write, additive only (adds one field, touches
            nothing else), filtered by {"worker_id": {"$exists": False}} so
            it is idempotent and never overwrites an explicit value (even a
            non-"default" one, however that could have gotten there).
  Phase B — INDEX SWAP: drop the obsolete single-field unique index
            (identified by KEY PATTERN {"agent_id": 1} + unique=True — see
            _find_obsolete_index — never by an assumed literal name), then
            create the compound unique index (agent_id, worker_id) and the
            (worker_id, active) lookup index.
  Phase C — VERIFY: re-read both the document backfill coverage and the
            index catalog; raise RuntimeError (never report success) if
            anything doesn't match the expected end state.
Backfilling BEFORE the index swap means the compound unique index is always
built against fully-populated, explicit worker_id values — never relying on
MongoDB's "missing field indexes as null" behavior for the data that exists
today (it remains correct for that case too, just no longer required).

Safety:
  * --dry-run (default): READ-ONLY. Reports document worker_id coverage,
    the real index catalog, and what --apply would do. Writes/creates/
    drops NOTHING.
  * --apply: performs all three phases. Every action is logged and included
    in the returned/printed report. An unexpected database error (anything
    other than the one specific "index already gone" race — see
    _drop_obsolete_index's docstring) is RAISED, not swallowed — this
    script fails loudly and exits non-zero rather than reporting success on
    a partial completion.
  * Idempotent / safe to re-run, including after an interruption partway
    through: Phase A only touches documents still missing the field (a
    partially-completed backfill just picks up the rest on retry — no
    document is ever written twice, no data is lost). Phase B's drop/create
    calls are each individually safe to repeat (see _drop_obsolete_index
    and apply()'s pre-checks). If --apply is interrupted between Phase A
    and Phase B, documents are backfilled but the old index is still live —
    re-running --apply from the top is safe and simply continues into
    Phase B (Phase A becomes a no-op the second time). If interrupted
    between dropping the old index and creating the new compound one, see
    "Known window" below.
  * Known window: for the brief interval between Phase B's drop_index and
    its create_index call succeeding, `agent_id` uniqueness is briefly
    UNENFORCED at the database level. A concurrent write racing into that
    exact window (extremely unlikely for a collection this small and this
    infrequently written outside of admin edits and startup seeding) could
    in principle insert a second doc for one agent_id; if so, the
    subsequent create_index(unique=True) call would itself fail loudly
    (MongoDB refuses to build a unique index over data that violates it),
    surfacing as this script's normal FAILED/non-zero exit path — it would
    not silently succeed. Running this during a quiet period, and running
    the verification query in "Post-migration verification" below
    immediately after, closes this out completely.

Rollback:
  1. Re-create the dropped single-field unique index by its ORIGINAL name
     (recorded in this script's own printed/JSON report — capture it
     before running --apply):
       db.whatsapp_agent_config.createIndex({agent_id: 1}, {unique: true, name: "<original name>"})
     Safe at any time UNLESS a second worker's config for an agent_id
     Worker 1 already has was inserted in the meantime — check first:
       db.whatsapp_agent_config.aggregate([
         {$group: {_id: "$agent_id", n: {$sum: 1}}},
         {$match: {n: {$gt: 1}}},
       ])
  2. The backfilled `worker_id: "default"` field is harmless to leave in
     place even on a full rollback (the restored single-field index ignores
     it), so removing it is optional. To remove it anyway:
       db.whatsapp_agent_config.updateMany({}, {$unset: {worker_id: ""}})

Usage (mirrors this repo's existing migrations/p3_media_ownership.py convention):
  railway run -- python3 backend/migrations/whatsapp_agent_config_worker_index.py --dry-run
  railway run -- python3 backend/migrations/whatsapp_agent_config_worker_index.py --apply
  railway run -- python3 backend/migrations/whatsapp_agent_config_worker_index.py --apply --json-out report.json

Post-migration verification (read-only, run immediately after --apply):
  db.whatsapp_agent_config.countDocuments({worker_id: {$exists: false}})   // must be 0
  db.whatsapp_agent_config.getIndexes()                                    // agent_id_1 gone;
                                                                            // agent_id_worker_id_unique present
  db.whatsapp_agent_config.aggregate([
    {$group: {_id: {a: "$agent_id", w: "$worker_id"}, n: {$sum: 1}}},
    {$match: {n: {$gt: 1}}},
  ])                                                                       // must return nothing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("whatsapp_agent_config_worker_index")

COLLECTION = "whatsapp_agent_config"
DEFAULT_WORKER_ID = "default"
COMPOUND_INDEX_KEY = [("agent_id", 1), ("worker_id", 1)]
COMPOUND_INDEX_NAME = "agent_id_worker_id_unique"
LOOKUP_INDEX_KEY = [("worker_id", 1), ("active", 1)]
LOOKUP_INDEX_NAME = "worker_active_idx"

# The obsolete constraint's key pattern — matched structurally, never by name.
OBSOLETE_KEY_PATTERN = {"agent_id": 1}

# The specific, expected "already gone" race: this script's own
# find-then-drop already confirmed the index existed moments before the
# drop call, so IndexNotFound here means a CONCURRENT process (another
# invocation of this same migration, most likely) dropped it first —
# genuinely benign, and the only OperationFailure this script treats as
# non-fatal. Every other server error is re-raised.
MONGO_INDEX_NOT_FOUND_CODE = 27


async def _list_indexes(coll) -> list[dict]:
    return [idx async for idx in coll.list_indexes()]


def _find_obsolete_index(indexes: list[dict]) -> Optional[dict]:
    """The pre-worker_id unique index on {"agent_id": 1} alone, found by
    KEY PATTERN + uniqueness — never by an assumed literal name."""
    for idx in indexes:
        if idx.get("unique") and dict(idx.get("key", {})) == OBSOLETE_KEY_PATTERN:
            return idx
    return None


def _find_index_by_key(indexes: list[dict], key: list[tuple[str, int]]) -> Optional[dict]:
    key_dict = dict(key)
    for idx in indexes:
        if dict(idx.get("key", {})) == key_dict:
            return idx
    return None


async def _document_coverage(coll) -> dict:
    """READ-ONLY document-level findings: how many docs exist, how many
    already carry worker_id (broken down by value), how many are missing
    it, and whether any (agent_id, worker_id) pair is already duplicated
    (which would make the compound unique index impossible to create)."""
    total = await coll.count_documents({})
    missing = await coll.count_documents({"worker_id": {"$exists": False}})
    # MongoDB's {"$in": [None, ...]} ALSO matches a missing field (it treats
    # absence as null for equality/$in comparisons) — {"$exists": True} is
    # required here so this genuinely counts only "field present but
    # null/empty", never double-counting documents already in `missing`.
    null_or_empty = await coll.count_documents({"worker_id": {"$exists": True, "$in": [None, ""]}})
    by_value = await coll.aggregate([
        {"$match": {"worker_id": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$worker_id", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    duplicates = await coll.aggregate([
        {"$group": {"_id": {"agent_id": "$agent_id", "worker_id": "$worker_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]).to_list(length=None)
    return {
        "total_documents": total,
        "missing_worker_id": missing,
        "null_or_empty_worker_id": null_or_empty,
        "worker_id_value_counts": {d["_id"]: d["n"] for d in by_value},
        "duplicate_agent_worker_pairs": [
            {"agent_id": d["_id"]["agent_id"], "worker_id": d["_id"]["worker_id"], "count": d["n"]}
            for d in duplicates
        ],
    }


async def inspect(db) -> dict:
    """READ-ONLY. Never called from a code path that also writes — this is
    the function --dry-run runs, and the first step --apply runs before
    touching anything."""
    coll = db[COLLECTION]
    indexes = await _list_indexes(coll)
    obsolete = _find_obsolete_index(indexes)
    compound = _find_index_by_key(indexes, COMPOUND_INDEX_KEY)
    lookup = _find_index_by_key(indexes, LOOKUP_INDEX_KEY)
    return {
        "documents": await _document_coverage(coll),
        "all_indexes": [
            {"name": i.get("name"), "key": dict(i.get("key", {})), "unique": bool(i.get("unique"))}
            for i in indexes
        ],
        "obsolete_agent_id_unique_index": (
            {"name": obsolete["name"], "key": dict(obsolete["key"]), "unique": True} if obsolete else None
        ),
        "compound_index_present": bool(compound and compound.get("unique")),
        "compound_index_actual": (
            {"name": compound["name"], "key": dict(compound["key"]), "unique": bool(compound.get("unique"))}
            if compound else None
        ),
        "lookup_index_present": bool(lookup),
    }


async def _backfill_worker_id(coll, actions: list[str]) -> int:
    """Phase A. $set worker_id="default" on every document that doesn't
    already have the field — never touches a document that already has ANY
    worker_id value (including "default" itself), so this is a strict
    additive fill-in-the-gaps, not a blanket overwrite. Returns the number
    of documents modified."""
    result = await coll.update_many(
        {"worker_id": {"$exists": False}},
        {"$set": {"worker_id": DEFAULT_WORKER_ID}},
    )
    if result.modified_count:
        actions.append(
            f"backfilled worker_id={DEFAULT_WORKER_ID!r} onto {result.modified_count} "
            f"document(s) that had no worker_id field"
        )
        logger.info("backfilled worker_id onto %d document(s)", result.modified_count)
    else:
        actions.append("no documents needed worker_id backfill (already compliant)")
        logger.info("no documents needed worker_id backfill")
    return result.modified_count


async def _drop_obsolete_index(coll, obsolete: dict, actions: list[str]) -> None:
    name = obsolete["name"]
    try:
        await coll.drop_index(name)
        actions.append(f"dropped obsolete unique index {name!r} (key={dict(obsolete['key'])})")
        logger.info("dropped obsolete unique index %r", name)
    except OperationFailure as exc:
        if exc.code == MONGO_INDEX_NOT_FOUND_CODE:
            # Confirmed gone already (a concurrent run of this same
            # migration won the race) — benign, log and continue.
            actions.append(f"index {name!r} already absent (concurrent migration run) — treated as success")
            logger.warning("index %r vanished between inspect and drop (concurrent run?) — continuing", name)
        else:
            # Anything else (permissions, a genuine server-side problem) is
            # a real failure. Surface it — do NOT report success.
            logger.error("drop_index(%r) failed with an unexpected error: %s", name, exc)
            raise


async def apply(db) -> dict:
    coll = db[COLLECTION]
    before = await inspect(db)
    actions: list[str] = []

    fully_compliant = (
        before["documents"]["missing_worker_id"] == 0
        and not before["obsolete_agent_id_unique_index"]
        and before["compound_index_present"]
        and before["lookup_index_present"]
    )
    if fully_compliant:
        logger.info("already compliant — nothing to do")
        return {"already_compliant": True, "before": before, "actions": actions, "after": before}

    # Phase A — backfill BEFORE touching any index, so the compound unique
    # index (Phase B) is always built against fully-populated worker_id
    # values, never relying on "missing field indexes as null" for data
    # that exists today.
    await _backfill_worker_id(coll, actions)

    # Phase B — index swap.
    obsolete = before["obsolete_agent_id_unique_index"]
    if obsolete:
        # Re-fetch the raw index doc (inspect() returns a trimmed view) so
        # _drop_obsolete_index has the real key spec for its log line.
        raw = _find_obsolete_index(await _list_indexes(coll))
        if raw:
            await _drop_obsolete_index(coll, raw, actions)

    if not before["compound_index_present"]:
        await coll.create_index(COMPOUND_INDEX_KEY, unique=True, name=COMPOUND_INDEX_NAME)
        actions.append(f"created compound unique index {COMPOUND_INDEX_NAME!r} on {COMPOUND_INDEX_KEY}")
        logger.info("created compound unique index %r", COMPOUND_INDEX_NAME)

    if not before["lookup_index_present"]:
        await coll.create_index(LOOKUP_INDEX_KEY, name=LOOKUP_INDEX_NAME)
        actions.append(f"created lookup index {LOOKUP_INDEX_NAME!r} on {LOOKUP_INDEX_KEY}")
        logger.info("created lookup index %r", LOOKUP_INDEX_NAME)

    # Phase C — verify. Fail loudly if the end state isn't what it must be
    # — never report success on a partial/unverified completion.
    after = await inspect(db)
    if after["documents"]["missing_worker_id"] != 0:
        raise RuntimeError(
            f"MIGRATION VERIFICATION FAILED: {after['documents']['missing_worker_id']} "
            f"document(s) still missing worker_id after --apply"
        )
    if after["documents"]["duplicate_agent_worker_pairs"]:
        raise RuntimeError(
            f"MIGRATION VERIFICATION FAILED: duplicate (agent_id, worker_id) pairs exist: "
            f"{after['documents']['duplicate_agent_worker_pairs']}"
        )
    if after["obsolete_agent_id_unique_index"] is not None:
        raise RuntimeError(
            f"MIGRATION VERIFICATION FAILED: obsolete index "
            f"{after['obsolete_agent_id_unique_index']!r} is still present after --apply"
        )
    if not after["compound_index_present"]:
        raise RuntimeError("MIGRATION VERIFICATION FAILED: compound unique index is missing after --apply")
    if not after["lookup_index_present"]:
        raise RuntimeError("MIGRATION VERIFICATION FAILED: lookup index is missing after --apply")

    return {"already_compliant": False, "before": before, "actions": actions, "after": after}


def _dry_run_plan(findings: dict) -> dict:
    """Pure, side-effect-free — takes inspect()'s (read-only) findings and
    states explicitly what --apply WOULD do, without doing any of it. Exists
    so --dry-run's report doesn't require the reader to infer "would this
    field's current value trigger a write" themselves from the raw
    inspection data."""
    docs = findings["documents"]
    return {
        "would_backfill_count": docs["missing_worker_id"],
        "would_drop_index": findings["obsolete_agent_id_unique_index"],
        "would_create_compound_index": not findings["compound_index_present"],
        "would_create_lookup_index": not findings["lookup_index_present"],
        "already_fully_compliant": (
            docs["missing_worker_id"] == 0
            and findings["obsolete_agent_id_unique_index"] is None
            and findings["compound_index_present"]
            and findings["lookup_index_present"]
        ),
        "writes_performed": False,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only inspection (default).")
    mode.add_argument("--apply", action="store_true", help="Perform the migration.")
    parser.add_argument("--json-out", default=None, help="Also write the report to this JSON file.")
    args = parser.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    report: dict = {"ran_at": datetime.now(timezone.utc).isoformat(), "mode": "apply" if args.apply else "dry-run"}
    try:
        if args.apply:
            result = await apply(db)
        else:
            findings = await inspect(db)
            result = {"dry_run_findings": findings, "dry_run_plan": _dry_run_plan(findings)}
        report.update(result)
        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, indent=2, default=str))
        if args.json_out:
            with open(args.json_out, "w") as f:
                json.dump(report, f, indent=2, default=str)
        client.close()
        logger.error("MIGRATION FAILED — see report above. Exiting non-zero.")
        sys.exit(1)

    print(json.dumps(report, indent=2, default=str))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2, default=str)
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
