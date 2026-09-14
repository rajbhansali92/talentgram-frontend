"""Multi-Worker Phase 2 — REAL MongoDB integration test for the
whatsapp_agent_config uniqueness migration
(backend/migrations/whatsapp_agent_config_worker_index.py).

Every other test touching this migration (test_agent_routing_worker_scoped.py)
uses plain-Python fake collections and cannot exercise real MongoDB index
enforcement, drop_index semantics, or DuplicateKeyError — none of that is
meaningfully testable without a real mongod. This test is SKIPPED, clearly
and loudly, unless a real MongoDB is actually available via MONGO_TEST_URL —
it must never be reported as "passing" by silently no-op'ing.

Exercises the exact sequence a real production migration goes through:
  1. Create the OLD agent_id-only unique index (simulating pre-migration
     production — using a deliberately non-default index NAME, so this also
     proves the migration's name-independence, not just its happy path).
  2. Insert a Worker 1 config doc.
  3. Run the migration (apply()).
  4. Verify the old unique index is gone.
  5. Verify the new compound unique index exists.
  6. Insert the SAME agent_id for Worker 2 — must succeed.
  7. Insert a duplicate (agent_id, worker_id) pair — must be rejected.

Run against a real local MongoDB:
  mongod --dbpath /tmp/mongo-test-data --port 27099 --bind_ip 127.0.0.1 --fork --logpath /tmp/mongod.log
  MONGO_TEST_URL="mongodb://127.0.0.1:27099" python3 backend/tests/test_whatsapp_agent_config_worker_index_migration.py

Without MONGO_TEST_URL set, this prints a clear SKIPPED line and exits 0 —
it does NOT claim to have verified anything.
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MONGO_TEST_URL = os.environ.get("MONGO_TEST_URL")


async def _run() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from pymongo.errors import DuplicateKeyError
    from migrations import whatsapp_agent_config_worker_index as mig

    # Isolated per-run database name so this test never collides with a
    # concurrent run or leaves state behind in a shared test DB. Client is
    # created (and every operation awaited) inside this ONE coroutine/event
    # loop — Motor binds its internal connection machinery to the loop
    # active when the client is used, so mixing a real AsyncIOMotorClient
    # across multiple asyncio.run()/new_event_loop() calls (as the fake-DB
    # test files in this suite do) breaks it.
    test_db_name = f"wa_migration_test_{uuid.uuid4().hex[:8]}"
    client = AsyncIOMotorClient(MONGO_TEST_URL)
    db = client[test_db_name]
    coll = db[mig.COLLECTION]

    try:
        # 1. Create the OLD agent_id-only unique index — deliberately with a
        # NON-DEFAULT name, so a pass here proves key-pattern-based
        # detection, not just a lucky literal-name match.
        await coll.create_index([("agent_id", 1)], unique=True, name="legacy_agent_id_only_unique")
        print("1. created OLD single-field unique index (custom name) OK")

        # 2. Insert a Worker 1 config doc — deliberately WITHOUT worker_id,
        # matching real production exactly (confirmed via Atlas Data
        # Explorer, 2026-09-13: all 5 production documents have no
        # worker_id field at all). This exercises the Phase A backfill,
        # not just the index swap.
        await coll.insert_one({
            "agent_id": "casting-agent",
            "group_names": ["Talentgram Casting Pipeline"],
            "allowed_senders": [], "active": True,
        })
        print("2. inserted Worker 1 config doc (no worker_id field) OK")

        # 3. Run the migration for real.
        result = await mig.apply(db)
        assert result["already_compliant"] is False
        assert any("backfilled worker_id" in a for a in result["actions"]), result["actions"]
        assert any("legacy_agent_id_only_unique" in a for a in result["actions"])
        print("3. migration ran ->", result["actions"])

        # 4. Verify the old unique index is gone AND the doc was backfilled.
        after = await mig.inspect(db)
        assert after["obsolete_agent_id_unique_index"] is None, after
        assert after["documents"]["missing_worker_id"] == 0
        backfilled = await coll.find_one({"agent_id": "casting-agent"}, {"_id": 0, "worker_id": 1})
        assert backfilled["worker_id"] == "default", backfilled
        print("4. old unique index confirmed gone + doc backfilled to worker_id='default' OK")

        # 5. Verify the compound unique index exists.
        assert after["compound_index_present"] is True
        assert after["compound_index_actual"]["name"] == mig.COMPOUND_INDEX_NAME
        assert after["lookup_index_present"] is True
        print("5. compound unique index + lookup index confirmed present OK")

        # 6. Insert the SAME agent_id for Worker 2 — must succeed now.
        await coll.insert_one({
            "agent_id": "casting-agent", "worker_id": "wa-worker-2",
            "group_names": ["Worker 2 Casting"],
            "allowed_senders": [], "active": True,
        })
        print("6. Worker 2 config for the SAME agent_id inserted successfully OK")

        # 7. A duplicate (agent_id, worker_id) pair must still be rejected.
        try:
            await coll.insert_one({
                "agent_id": "casting-agent", "worker_id": "wa-worker-2",
                "group_names": ["dupe"], "allowed_senders": [], "active": True,
            })
            assert False, "expected DuplicateKeyError for a repeated (agent_id, worker_id) pair"
        except DuplicateKeyError:
            print("7. duplicate (agent_id, worker_id) correctly rejected OK")

        # Bonus: re-running apply() is a verified no-op (idempotency).
        result2 = await mig.apply(db)
        assert result2["already_compliant"] is True
        print("8. re-running the migration is a verified no-op OK")

        print("\nALL REAL-MONGODB MIGRATION INTEGRATION TESTS PASSED")
    finally:
        await client.drop_database(test_db_name)
        client.close()


def main():
    if not MONGO_TEST_URL:
        print(
            "SKIPPED: MONGO_TEST_URL is not set — this test requires a real MongoDB "
            "instance and does not run against fakes. No claim of integration "
            "coverage is made. See this file's docstring to run it for real."
        )
        return
    asyncio.run(_run())


if __name__ == "__main__":
    main()
