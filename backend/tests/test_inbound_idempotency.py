"""Backend-side execution-boundary tests for agents/inbound_idempotency.py.

Production incident (2026-09-29/30) — see that module's docstring: a command
already executed by the backend must never execute again for the same
WhatsApp message_id, however many times /inbound is called for it.

Real-DB integration tests (local Mongo). Each test builds its OWN Motor
client inside its own event loop (no module-level client / shared loop), so
the file is independent of test ordering.
"""
import os
import uuid as _uuid
from datetime import timedelta

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

import core
import agents.inbound_idempotency as idem

pytestmark = pytest.mark.asyncio

_MTAG = "TEST_INBOUND_IDEM_"
# Captured at import: other test files mutate os.environ at runtime.
# Pinned to the local dev Mongo: other test files setdefault MONGO_URL to a
# fake host at collection time, which must not redirect these real-DB tests.
_MONGO_URL = os.environ.get("TEST_REAL_MONGO_URL", "mongodb://localhost:27017")
_DB_NAME = "talentgram"
_real_db = None


@pytest_asyncio.fixture(autouse=True)
async def _use_real_db(monkeypatch):
    global _real_db
    client = AsyncIOMotorClient(_MONGO_URL)
    _real_db = client[_DB_NAME]
    monkeypatch.setattr(core, "db", _real_db)
    monkeypatch.setattr(idem, "db", _real_db)
    # Mirrors agents/__init__.py's startup index creation (direct module
    # import never runs app startup). Idempotent no-op if already present.
    await _real_db.whatsapp_inbound_seen.create_index("message_id", unique=True)
    yield
    client.close()


async def _cleanup(message_ids):
    if message_ids:
        await _real_db.whatsapp_inbound_seen.delete_many({"message_id": {"$in": message_ids}})


# TEST 1 — Fresh message_id: first call must execute.
async def test_fresh_message_id_returns_execute():
    mid = f"{_MTAG}fresh_{_uuid.uuid4().hex[:8]}"
    try:
        state, stored = await idem.claim_or_get_result(mid)
        assert state == idem.EXECUTE
        assert stored is None
    finally:
        await _cleanup([mid])


# TEST 2 — THE INCIDENT: once a result is persisted for a message_id, a
# second claim_or_get_result call for the SAME message_id must NOT
# execute again — it must return the stored result verbatim.
async def test_persisted_result_is_returned_without_reexecution():
    mid = f"{_MTAG}exec_once_{_uuid.uuid4().hex[:8]}"
    try:
        state, _ = await idem.claim_or_get_result(mid)
        assert state == idem.EXECUTE

        real_result = {"handled": True, "reply": "Talentgram x Carter's - Form\n\nfanny - g", "operation_id": None}
        await idem.persist_result(mid, real_result)

        # Simulates the worker's stale-claim recovery redispatching the
        # SAME message_id — the exact production incident.
        state2, stored2 = await idem.claim_or_get_result(mid)
        assert state2 == idem.RETURN_STORED, "a message_id whose command already ran must never execute a second time"
        assert stored2 == real_result, "the replayed caller must get back the ORIGINAL reply, not a fresh one"

        # And a third, fourth call — however many times a flaky worker
        # keeps retrying — must all converge on the same stored result.
        for _ in range(3):
            state_n, stored_n = await idem.claim_or_get_result(mid)
            assert state_n == idem.RETURN_STORED
            assert stored_n == real_result
    finally:
        await _cleanup([mid])


# TEST 3 — Genuinely concurrent claim attempts for the SAME message_id
# (not yet stale): exactly one must win and execute; the rest must see
# STILL_EXECUTING, never EXECUTE.
async def test_concurrent_claims_exactly_one_executes():
    import asyncio
    mid = f"{_MTAG}race_{_uuid.uuid4().hex[:8]}"
    try:
        results = await asyncio.gather(*[idem.claim_or_get_result(mid) for _ in range(5)])
        states = [s for s, _ in results]
        assert states.count(idem.EXECUTE) == 1, f"exactly one concurrent claim must win, got states={states}"
        assert states.count(idem.STILL_EXECUTING) == 4
    finally:
        await _cleanup([mid])


# TEST 4 — A claim left "executing" past the stale-recovery timeout (a
# genuinely rare backend-process crash mid-command, not the worker crash
# this fix primarily targets) is eligible for exactly one recovery
# attempt — documented, narrower residual gap.
async def test_stale_executing_claim_is_recovered_after_timeout():
    mid = f"{_MTAG}stale_backend_{_uuid.uuid4().hex[:8]}"
    try:
        state, _ = await idem.claim_or_get_result(mid)
        assert state == idem.EXECUTE

        # Simulate this backend process having died mid-command: push
        # backend_dispatch_started_at back past the stale threshold.
        await _real_db.whatsapp_inbound_seen.update_one(
            {"message_id": mid},
            {"$set": {"backend_dispatch_started_at": idem._now() - timedelta(seconds=idem._STALE_EXECUTING_TIMEOUT_SEC + 5)}},
        )
        state2, _ = await idem.claim_or_get_result(mid)
        assert state2 == idem.EXECUTE, "a genuinely stale backend-side claim must be recoverable exactly once"
    finally:
        await _cleanup([mid])


# TEST 5 — A claim "executing" and NOT yet stale must never be silently
# re-executed just because a caller happens to check again quickly.
async def test_not_yet_stale_executing_claim_is_not_reexecuted():
    mid = f"{_MTAG}not_stale_{_uuid.uuid4().hex[:8]}"
    try:
        state, _ = await idem.claim_or_get_result(mid)
        assert state == idem.EXECUTE
        state2, stored2 = await idem.claim_or_get_result(mid)
        assert state2 == idem.STILL_EXECUTING
        assert stored2 is None
    finally:
        await _cleanup([mid])


# TEST 6 — persist_result is idempotent/safe to call once; the stored
# shape round-trips exactly (including a None reply, the common
# handled=False/no-reply turn).
async def test_persist_result_round_trips_a_no_reply_turn():
    mid = f"{_MTAG}noreply_{_uuid.uuid4().hex[:8]}"
    try:
        state, _ = await idem.claim_or_get_result(mid)
        assert state == idem.EXECUTE
        await idem.persist_result(mid, {"handled": False, "reply": None, "operation_id": None})
        state2, stored2 = await idem.claim_or_get_result(mid)
        assert state2 == idem.RETURN_STORED
        assert stored2 == {"handled": False, "reply": None, "operation_id": None}
    finally:
        await _cleanup([mid])


# TEST 7 — END TO END through the real /inbound handler: the same
# message_id POSTed repeatedly (a worker's stale-claim recovery
# redispatching an already-answered command) executes the agent command
# exactly once and returns the identical reply every time.
async def test_inbound_endpoint_executes_command_once_per_message_id(monkeypatch):
    import routers.agents_whatsapp as raw
    from agents.models import DispatchResult

    monkeypatch.setattr(raw, "db", _real_db)
    mid = f"{_MTAG}endpoint_{_uuid.uuid4().hex[:8]}"
    executions = []

    async def fake_handle(**kwargs):
        executions.append(kwargs["message_id"])
        return DispatchResult(handled=True, reply="Talentgram x Carter's - Form\n\nfanny - g", operation_id=None)

    monkeypatch.setattr(raw, "handle_inbound_message", fake_handle)
    # Sending-permission gate: treat the worker as sending-enabled.
    await _real_db.whatsapp_workers.update_one(
        {"id": "default"}, {"$set": {"sending_enabled": True}}, upsert=True,
    )
    payload = raw.InboundMessageIn(
        group_name="Talentgram Fetcher Agent", sender_phone="919876543210",
        text="Show me Fanny Gandhi's form for Carter", message_id=mid,
    )
    try:
        first = await raw.inbound_message(payload, x_internal_secret=raw.INBOUND_SECRET or None)
        replays = [await raw.inbound_message(payload, x_internal_secret=raw.INBOUND_SECRET or None) for _ in range(3)]

        assert len(executions) == 1, f"command must execute once per message_id, executed {len(executions)}x"
        assert first["reply"] == "Talentgram x Carter's - Form\n\nfanny - g"
        for r in replays:
            assert r == first, "replays must return the original stored result, not a new one"
    finally:
        await _cleanup([mid])
