"""Regression tests for the inbound page_lock self-deadlock fix (2026-09-03).

Reported production bug: "Share Instagram link of Anusha Sharma to Heena
Talentgram" produced a repeating "Got it — processing..." reply in the
casting-pipeline WhatsApp group every ~1 minute, with no final confirmation
ever arriving, even across multiple independent sends of the command.

Root cause (see inbound.py's poll_once and _post_inbound for the full
explanation left in code comments): routers/agents_whatsapp.py's /inbound
handler awaits handle_inbound_message() synchronously — SHARE Instagram's
live WhatsApp recipient search (casting_pipeline._search_whatsapp_live,
250ad8d) runs INLINE inside that one HTTP call, waiting up to
RECIPIENT_SEARCH_MAX_WAIT_SEC (20s) for the WhatsApp Worker's mark_scan_loop
to service a whatsapp_scan_requests doc. But inbound.py's poll_once held
session.page_lock for the ENTIRE duration of dispatching a message AND
awaiting the backend's response — and mark_scan_loop (spawned in the SAME
worker.py process, sharing the SAME session.page_lock) needs that identical
lock to actually run the search. Since poll_once would not release the lock
until the backend responded, and the backend could not respond until
mark_scan_loop ran — which it could never do without the lock — this was a
deterministic self-deadlock, broken only by the backend's own internal 20s
search timeout. _post_inbound's own httpx client timeout was ALSO exactly
20.0s, so it reliably lost that race and raised before the backend's
(post-timeout, CRM-fallback) response arrived — leaving the message NEVER
marked processed, so it was redetected and redispatched from scratch on
every subsequent poll cycle. That is the repeating "Got it — processing..."
with no resolution.

The fix: poll_once now holds page_lock only for genuinely page-touching
work (open+scan the chat, send the ack, send the final reply) — never
across the backend network wait. _post_inbound's own client-side timeout
was also raised (20.0s -> 35.0s) to give real headroom over the backend's
declared 20s worst case, as a second, independent line of defense.

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_inbound_concurrency.py -q
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest

import inbound  # noqa: E402
import sender  # noqa: E402

pytestmark = pytest.mark.asyncio


class FakeCollection:
    """Minimal Mongo-collection stand-in supporting exactly what
    _already_processed/_mark_processed/_update_worker_status/the
    no-phone dispatch-failure path need — find_one + update_one (with
    upsert semantics tracked in a plain dict), insert_one, and the
    find()/count_documents() shapes the reconnect tests already use."""

    def __init__(self):
        self._docs: dict = {}
        self.inserted = []

    async def find_one(self, filt, *a, **k):
        key = filt.get("message_id") or filt.get("id")
        return self._docs.get(key)

    async def update_one(self, filt, update, upsert=False, **kwargs):
        key = filt.get("message_id") or filt.get("id")
        if key is None:
            return
        doc = self._docs.setdefault(key, {})
        for k2, v in (update.get("$set") or {}).items():
            doc[k2] = v
        if upsert:
            for k2, v in (update.get("$setOnInsert") or {}).items():
                doc.setdefault(k2, v)

    async def update_many(self, filt, update, **kwargs):
        pass

    async def insert_one(self, doc):
        self.inserted.append(doc)

    def find(self, *a, **k):
        class _EmptyCursor:
            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                raise StopAsyncIteration
        return _EmptyCursor()

    async def count_documents(self, *a, **k):
        return 0


class FakeDB:
    def __init__(self):
        self.whatsapp_sessions = FakeCollection()
        self.whatsapp_dispatch_failures = FakeCollection()
        self._collections = {}

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())


class FakeResponse:
    def __init__(self, groups):
        self._groups = groups

    def raise_for_status(self):
        return None

    def json(self):
        return {"groups": self._groups}


class FakeHttp:
    def __init__(self, groups):
        self._groups = groups

    async def get(self, url, **kwargs):
        return FakeResponse(self._groups)


class FakeSession:
    def __init__(self, generation=1):
        self.generation = generation
        self.session_id = f"sess-{generation}"
        self.own_phone_number = None
        self.page = object()  # sentinel — never dereferenced; page-touching fns are monkeypatched
        self.page_lock = asyncio.Lock()
        self.is_healthy = True


def _groups_cache(groups):
    return inbound.KnownGroupsCache(FakeHttp(groups))


def _reset_module_state():
    inbound._INVALID_GROUPS.clear()
    inbound._PENDING_REVALIDATION.clear()
    inbound._seen_cache.clear()
    inbound._invalid_group_last_logged.clear()
    inbound._last_written_status.clear()
    inbound._last_seen_generation = None
    inbound._state_rebuilt_at = 0.0


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _reset_module_state()
    fake_db = FakeDB()
    monkeypatch.setattr(inbound, "get_db", lambda: fake_db)
    yield fake_db
    _reset_module_state()


async def _fake_open_opened(page, group_name):
    return "OPENED"


def _fake_scan_returning(messages):
    """Returns a _scan_group_for_new_messages stand-in that yields
    `messages` exactly once, then nothing — mirrors "the DOM has one new
    message this cycle", independent of any dedup bookkeeping (tests that
    care about dedup call the real _already_processed themselves)."""
    remaining = {"left": True}

    async def fake_scan(page, group_name, participants_cache):
        if not remaining["left"]:
            return [], 0.0, 0.0
        remaining["left"] = False
        return list(messages), 0.0, 0.0

    return fake_scan


# ─────────────────────────────────────────────────────────────────────────
# THE deadlock regression
# ─────────────────────────────────────────────────────────────────────────

async def test_page_lock_is_free_while_poll_once_awaits_the_backend(_setup, monkeypatch):
    """The core regression: a concurrent task (standing in for
    mark_scan_loop, which needs session.page_lock in the SAME process to
    service a worker-mediated backend round trip like SHARE Instagram's
    live WhatsApp recipient search) must be able to acquire
    session.page_lock WHILE poll_once is still awaiting _post_inbound's
    response. Before the fix, poll_once held the lock for that entire
    wait, so this contender could never acquire it until poll_once's own
    backend call finished — which, for a real resolve_recipient request,
    could never finish without the contender running first. Deadlock,
    broken only by the backend's own internal timeout."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()

    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([{
        "message_id": "wamid-deadlock-1", "text": "share instagram link of Anusha Sharma to Heena Talentgram",
        "sender_name": "Raj", "sender_phone": "919876543210", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }]))

    backend_call_started = asyncio.Event()
    backend_call_may_finish = asyncio.Event()

    async def slow_post_inbound(http, **kwargs):
        backend_call_started.set()
        await backend_call_may_finish.wait()
        return {"reply": None, "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", slow_post_inbound)
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 999.0)  # never fires the ack path — irrelevant here

    lock_acquired_while_backend_pending = asyncio.Event()

    async def contender():
        await backend_call_started.wait()
        # If poll_once still (incorrectly) held the lock here, this would
        # hang until backend_call_may_finish is set below — which never
        # happens until AFTER this coroutine acquires the lock, so a
        # regression would deadlock this test until pytest's own timeout,
        # not silently pass.
        async with session.page_lock:
            lock_acquired_while_backend_pending.set()
        backend_call_may_finish.set()

    contender_task = asyncio.create_task(contender())

    await asyncio.wait_for(
        inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                           participants_cache=participants_cache),
        timeout=5.0,
    )
    await asyncio.wait_for(contender_task, timeout=5.0)

    assert lock_acquired_while_backend_pending.is_set(), (
        "session.page_lock must be acquirable by a concurrent task while "
        "poll_once awaits the backend's response — otherwise a worker-"
        "mediated backend round trip started from within this same "
        "dispatch (e.g. SHARE Instagram's live WhatsApp recipient search) "
        "can never complete, reproducing the reported repeating "
        "'Got it — processing...' bug"
    )


async def test_ack_and_reply_sends_still_reacquire_the_lock(_setup, monkeypatch):
    """The lock must not simply be dropped forever — the two genuinely
    page-touching operations (the ack, the final reply) must still run
    under it, just not for the whole backend wait. Event-driven (not
    sleep/threshold timing) so it stays deterministic regardless of
    scheduling jitter from whatever ran before it in the same suite."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()

    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([{
        "message_id": "wamid-ack-1", "text": "hello", "sender_name": "Raj",
        "sender_phone": "919876543210", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }]))
    # timeout=0.0 deterministically takes the "backend not done yet" branch:
    # backend_task is freshly created and immediately blocks on an Event
    # that hasn't been set, so it can never be in `done` at the first
    # check, regardless of how fast/slow the surrounding suite is running.
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 0.0)

    backend_may_finish = asyncio.Event()

    async def slow_post_inbound(http, **kwargs):
        await backend_may_finish.wait()
        return {"reply": "Confirmation card", "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", slow_post_inbound)

    lock_held_during_send = []
    ack_sent = asyncio.Event()

    async def recording_send_reply(page, group_name, text):
        lock_held_during_send.append(session.page_lock.locked())
        if text == inbound.ACK_TEXT:
            ack_sent.set()
        return 0.0, {}, "sent"

    monkeypatch.setattr(inbound, "_send_reply", recording_send_reply)

    poll_task = asyncio.create_task(inbound.poll_once(
        session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache,
    ))
    await asyncio.wait_for(ack_sent.wait(), timeout=5.0)
    backend_may_finish.set()
    await asyncio.wait_for(poll_task, timeout=5.0)

    assert lock_held_during_send == [True, True], (
        "both the ack send and the final reply send must run with "
        "page_lock held — only the backend wait itself should run "
        f"unlocked, got {lock_held_during_send!r}"
    )


async def test_reply_send_skipped_gracefully_if_page_vanishes_mid_flight(_setup, monkeypatch):
    """Worker-restart-during-processing case (master prompt section 7):
    if session.page becomes None between releasing the lock (after scan)
    and re-acquiring it for the reply send — e.g. a reconnect happened
    while the backend call was in flight — the reply send must be
    skipped gracefully (logged, not attempted), never crash poll_once."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()

    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([{
        "message_id": "wamid-vanish-1", "text": "hello", "sender_name": "Raj",
        "sender_phone": "919876543210", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }]))

    async def post_inbound_that_wipes_the_page(http, **kwargs):
        session.page = None  # simulate a reconnect wiping the page mid-dispatch
        return {"reply": "Confirmation card", "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", post_inbound_that_wipes_the_page)

    send_calls = []

    async def recording_send_reply(page, group_name, text):
        send_calls.append(text)
        return 0.0, {}, "sent"

    monkeypatch.setattr(inbound, "_send_reply", recording_send_reply)

    # Must not raise.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert send_calls == [], "must never attempt to send with a None page"


# ─────────────────────────────────────────────────────────────────────────
# Idempotency contract (master prompt sections 2 & 6)
# ─────────────────────────────────────────────────────────────────────────

async def test_same_message_id_is_recognized_as_already_processed(_setup):
    assert await inbound._already_processed("wamid-idem-1") is False
    await inbound._mark_processed("wamid-idem-1")
    assert await inbound._already_processed("wamid-idem-1") is True


async def test_different_message_id_is_not_deduped_by_another_ids_processed_state(_setup):
    await inbound._mark_processed("wamid-idem-A")
    assert await inbound._already_processed("wamid-idem-B") is False


async def test_poll_once_never_redispatches_a_message_already_marked_processed(_setup, monkeypatch):
    """Simulates two independent poll cycles scanning a message that is
    STILL visible in WhatsApp Web's DOM (nothing hides a processed
    message there) — the real _already_processed/_mark_processed
    primitives (not a stub) must ensure the second cycle dispatches
    nothing for it, exactly the 'same inbound message ID encountered
    again -> do NOTHING' contract in the master prompt."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw_message = {
        "message_id": "wamid-fixed-1", "text": "hello", "sender_name": "Alice",
        "sender_phone": "919198765000", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        # Mirrors the real _scan_group_for_new_messages' own dedup gate.
        if await inbound._already_processed(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatch_calls = []

    async def fake_post_inbound(http, **kwargs):
        dispatch_calls.append(kwargs["message_id"])
        return {"reply": None, "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)

    send_calls = []

    async def fake_send_reply(page, group_name, text):
        send_calls.append(text)
        return 0.0, {}, "sent-1"

    monkeypatch.setattr(inbound, "_send_reply", fake_send_reply)

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert dispatch_calls == ["wamid-fixed-1"]

    # A second, independent poll cycle.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert dispatch_calls == ["wamid-fixed-1"], (
        "the same message_id must never be dispatched to the backend a "
        "second time once it has been marked processed"
    )


async def test_genuinely_new_message_with_new_id_still_processes_normally(_setup, monkeypatch):
    """The other half of the contract: a NEW WhatsApp message (new
    message_id), even with identical text to one already processed, must
    be treated as a new command, not swallowed by the dedup check."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    await inbound._mark_processed("wamid-old")

    async def fake_scan(page, group_name, participants_cache):
        return [{
            "message_id": "wamid-new", "text": "hello", "sender_name": "Alice",
            "sender_phone": "919198765000", "sender_is_group_member": True,
            "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
        }], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatch_calls = []

    async def fake_post_inbound(http, **kwargs):
        dispatch_calls.append(kwargs["message_id"])
        return {"reply": None, "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    monkeypatch.setattr(inbound, "_send_reply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no reply expected")))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert dispatch_calls == ["wamid-new"], "a genuinely new message_id must be dispatched"


async def test_failed_backend_dispatch_is_retried_on_the_next_poll_cycle(_setup, monkeypatch):
    """Master prompt section 7 ('backend retry' / 'worker retry'): a
    transient backend failure (exception, timeout — _post_inbound
    returns None either way) must leave the message unmarked so the
    NEXT poll cycle retries it from scratch; once the backend actually
    succeeds, it must be dispatched exactly once more and then marked
    processed — never left retrying forever, never double-processed."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw_message = {
        "message_id": "wamid-retry-1", "text": "hello", "sender_name": "Alice",
        "sender_phone": "919198765111", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if await inbound._already_processed(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    attempts = {"n": 0}

    async def flaky_post_inbound(http, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None  # transient failure (exception/timeout), as _post_inbound itself returns on error
        return {"reply": None, "operation_id": None, "handled": True}

    monkeypatch.setattr(inbound, "_post_inbound", flaky_post_inbound)

    # Cycle 1: backend fails -> must NOT be marked processed.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 1
    assert await inbound._already_processed("wamid-retry-1") is False, (
        "a failed backend dispatch must leave the message unmarked so it "
        "is retried, not silently dropped"
    )

    # Cycle 2: backend succeeds -> dispatched exactly once more, then marked.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 2, "must retry exactly once on the next cycle, not loop within one cycle"
    assert await inbound._already_processed("wamid-retry-1") is True

    # Cycle 3: already processed -> must never be dispatched again.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 2, "must never be dispatched again once successfully processed"


# ─────────────────────────────────────────────────────────────────────────
# Timeout headroom (secondary, defense-in-depth fix)
# ─────────────────────────────────────────────────────────────────────────

def test_inbound_dispatch_timeout_has_real_headroom_over_backend_worst_case():
    """The backend's own declared worst case for a single /inbound call
    is SHARE Instagram's live recipient search
    (RECIPIENT_SEARCH_MAX_WAIT_SEC, default 20s) plus a small amount of
    fallback/DB work. This client-side timeout must sit meaningfully
    above that, or a legitimately-slow-but-successful backend response
    gets killed here — a second, independent way to reproduce
    'message never marked processed, redispatched from scratch'."""
    assert inbound._INBOUND_DISPATCH_TIMEOUT_SEC >= 30.0
    assert inbound._INBOUND_DISPATCH_TIMEOUT_SEC > 20.0 + 10.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
