"""Regression tests for the reconnect-diagnostics fields (2026-08-11).

Covers _update_worker_status: writes onto the SAME whatsapp_sessions
singleton doc session.py already owns (no new collection), and only
issues a DB write when a field's value actually changed — so the 2s
inbound poll cycle doesn't turn into a write-every-cycle for fields like
listener_status/dispatcher_status that rarely change, while still always
writing genuinely new values (like the per-message last_incoming_at/
last_processed_at/last_reply_at timestamps).

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_worker_status_diagnostics.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest

import inbound  # noqa: E402

pytestmark = pytest.mark.asyncio


class FakeCollection:
    def __init__(self):
        self.update_one_calls = []

    async def update_one(self, filt, update, **kwargs):
        self.update_one_calls.append((filt, update, kwargs))


class FakeDB:
    def __init__(self):
        self.whatsapp_sessions = FakeCollection()

    def __getitem__(self, name):
        raise AssertionError(f"_update_worker_status must only touch whatsapp_sessions, not {name!r}")


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    inbound._last_written_status.clear()
    fake_db = FakeDB()
    monkeypatch.setattr(inbound, "get_db", lambda: fake_db)
    yield fake_db
    inbound._last_written_status.clear()


async def test_writes_onto_the_existing_whatsapp_sessions_singleton(_setup):
    await inbound._update_worker_status(listener_status="active")

    assert len(_setup.whatsapp_sessions.update_one_calls) == 1
    filt, update, kwargs = _setup.whatsapp_sessions.update_one_calls[0]
    assert filt == {"id": "default"}
    assert update == {"$set": {"listener_status": "active"}}
    assert kwargs.get("upsert") is True


async def test_repeating_the_same_value_does_not_write_again(_setup):
    await inbound._update_worker_status(dispatcher_status="ready")
    await inbound._update_worker_status(dispatcher_status="ready")

    assert len(_setup.whatsapp_sessions.update_one_calls) == 1, (
        "an unchanged status must not turn into a write on every 2s poll "
        "cycle"
    )


async def test_an_actual_change_writes_again(_setup):
    await inbound._update_worker_status(dispatcher_status="ready")
    await inbound._update_worker_status(dispatcher_status="unreachable")

    assert len(_setup.whatsapp_sessions.update_one_calls) == 2
    _, second_update, _ = _setup.whatsapp_sessions.update_one_calls[1]
    assert second_update == {"$set": {"dispatcher_status": "unreachable"}}


async def test_only_the_changed_fields_are_included_in_a_mixed_call(_setup):
    await inbound._update_worker_status(listener_status="active", dispatcher_status="ready")
    await inbound._update_worker_status(listener_status="active", dispatcher_status="unreachable")

    _, second_update, _ = _setup.whatsapp_sessions.update_one_calls[1]
    assert second_update == {"$set": {"dispatcher_status": "unreachable"}}, (
        "listener_status didn't change between the two calls, so it must "
        "not appear in the second write"
    )


async def test_a_fresh_timestamp_value_always_writes(_setup):
    """last_incoming_at/last_processed_at/last_reply_at are always a NEW
    timestamp string on every real event, so the change-detection dedup
    must never suppress them in practice — verified here with two
    different values."""
    await inbound._update_worker_status(last_incoming_at="2026-08-11T18:08:00+00:00")
    await inbound._update_worker_status(last_incoming_at="2026-08-11T18:09:00+00:00")

    assert len(_setup.whatsapp_sessions.update_one_calls) == 2


async def test_db_write_failure_does_not_raise(_setup, monkeypatch):
    async def _raise(*a, **k):
        raise RuntimeError("mongo unavailable")
    monkeypatch.setattr(_setup.whatsapp_sessions, "update_one", _raise)

    # Must not propagate — a diagnostics write failing must never take down
    # the actual message-processing pipeline it's reporting on.
    await inbound._update_worker_status(listener_status="active")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
