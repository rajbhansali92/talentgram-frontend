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
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest
from pymongo.errors import DuplicateKeyError

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

    async def delete_one(self, filt):
        key = filt.get("message_id")
        doc = self._docs.get(key)
        if doc is None:
            return
        for k2, v in filt.items():
            if doc.get(k2) != v:
                return  # filter didn't match (e.g. status changed underneath us) -- delete nothing
        del self._docs[key]

    async def insert_one(self, doc):
        # Simulates the real unique index on message_id (Production fix,
        # 2026-09-08 — the atomic-claim rework relies on this exact
        # DuplicateKeyError contract) — a doc with a message_id already
        # present in this collection raises, exactly like the real
        # unique index does; any other doc (e.g. whatsapp_dispatch_
        # failures, never keyed this way) is stored unconditionally.
        key = doc.get("message_id")
        if key is not None:
            if key in self._docs:
                raise DuplicateKeyError("E11000 duplicate key (fake)")
            self._docs[key] = dict(doc)
        self.inserted.append(doc)

    async def find_one_and_update(self, filt, update, **kwargs):
        """Minimal conditional-update stand-in for the bounded stale-claim
        recovery path: matches only when EVERY key in `filt` equals the
        stored doc's own value (mirrors Mongo's real match semantics,
        including the exact-claimed_at check that makes concurrent
        recovery attempts race safely) — returns the PRE-update doc on a
        match (Mongo's own default without return_document=AFTER), None
        otherwise. Never invents a match."""
        key = filt.get("message_id")
        doc = self._docs.get(key)
        if doc is None:
            return None
        for k2, v in filt.items():
            if doc.get(k2) != v:
                return None
        before = dict(doc)
        for k2, v in (update.get("$set") or {}).items():
            doc[k2] = v
        return before

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
    inbound._acked_cache.clear()
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
        return ("ok", {"reply": None, "operation_id": None, "handled": True})

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
        return ("ok", {"reply": "Confirmation card", "operation_id": None, "handled": True})

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
        return ("ok", {"reply": "Confirmation card", "operation_id": None, "handled": True})

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
    assert await inbound._claim_message("wamid-idem-1") is True
    assert await inbound._claim_message("wamid-idem-1") is False


async def test_different_message_id_is_not_deduped_by_another_ids_processed_state(_setup):
    await inbound._mark_processed("wamid-idem-A")
    assert await inbound._claim_message("wamid-idem-B") is True


async def test_poll_once_never_redispatches_a_message_already_marked_processed(_setup, monkeypatch):
    """Simulates two independent poll cycles scanning a message that is
    STILL visible in WhatsApp Web's DOM (nothing hides a processed
    message there) — the real _claim_message primitive (not a stub)
    must ensure the second cycle dispatches nothing for it, exactly the
    'same inbound message ID encountered again -> do NOTHING' contract
    in the master prompt."""
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
        # Mirrors the real _scan_group_for_new_messages' own ATOMIC
        # CLAIM gate (Production fix, 2026-09-08) — a plain read-only
        # check here would not reproduce the real behavior at all.
        if not await inbound._claim_message(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatch_calls = []

    async def fake_post_inbound(http, **kwargs):
        dispatch_calls.append(kwargs["message_id"])
        return ("ok", {"reply": None, "operation_id": None, "handled": True})

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


async def test_same_message_observed_ten_times_still_one_ack_and_one_execution(_setup, monkeypatch):
    """Master prompt test #2: the same message observed 10 times (10
    separate poll cycles, all still finding it visible in the DOM) must
    still produce exactly one acknowledgement/execution — never once per
    cycle. Directly reproduces the Anushka incident's shape (6 repeated
    acks over ~10 minutes), just with more cycles."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw_message = {
        "message_id": "wamid-tenpoll-1", "text": "send anushka for lava", "sender_name": "Raj",
        "sender_phone": "919198765222", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatch_calls = []

    async def fake_post_inbound(http, **kwargs):
        dispatch_calls.append(kwargs["message_id"])
        return ("ok", {"reply": "SEND FORM PREVIEW", "operation_id": None, "handled": True})

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)

    ack_calls = []

    async def fake_send_reply(page, group_name, text):
        ack_calls.append(text)
        return 0.0, {}, "sent-1"

    monkeypatch.setattr(inbound, "_send_reply", fake_send_reply)

    for _ in range(10):
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                                 participants_cache=participants_cache)

    assert dispatch_calls == ["wamid-tenpoll-1"], (
        f"expected exactly one backend dispatch across 10 poll cycles, got {len(dispatch_calls)}"
    )
    assert ack_calls == ["SEND FORM PREVIEW"], (
        f"expected exactly one reply send across 10 poll cycles, got {ack_calls}"
    )


async def test_message_observed_while_execution_still_in_flight_is_ignored(_setup, monkeypatch):
    """Master prompt test #3: the same message observed by a SECOND poll
    cycle while the FIRST cycle's backend dispatch is still genuinely
    in-flight (not yet resolved) must be ignored entirely — no second
    claim, no second ack, no second dispatch. Models the exact Anushka
    shape: a slow backend call, with the scan loop revisiting the
    message on every subsequent poll before that call ever resolves."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw_message = {
        "message_id": "wamid-inflight-1", "text": "send anushka for lava", "sender_name": "Raj",
        "sender_phone": "919198765333", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    # A DIRECT test of the claim itself standing in for "still in flight":
    # the message is claimed once (as poll_once's own scan phase would),
    # and while that claim is outstanding (no completion/release yet —
    # exactly the state a slow, still-running backend call leaves it in),
    # further claim attempts for the SAME message_id must all fail.
    assert await inbound._claim_message(raw_message["message_id"]) is True
    for _ in range(5):
        assert await inbound._claim_message(raw_message["message_id"]) is False, (
            "a message whose claim is still in_progress must never be claimable again"
        )


async def test_two_concurrent_claims_exactly_one_wins(_setup):
    """Master prompt test #4: two 'workers' (here, two concurrent asyncio
    tasks racing the same atomic claim, standing in for two overlapping
    tasks or two worker processes sharing the same Mongo collection)
    attempting to claim the SAME message_id — exactly one must win."""
    results = await asyncio.gather(
        inbound._claim_message("wamid-race-1"),
        inbound._claim_message("wamid-race-1"),
        inbound._claim_message("wamid-race-1"),
    )
    assert sorted(results) == [False, False, True], (
        f"exactly one of N concurrent claims for the same message_id must win, got {results}"
    )


async def test_worker_restart_after_claim_does_not_blindly_reexecute(_setup, monkeypatch):
    """Master prompt test #5: a message is claimed, then the in-memory
    cache is wiped (simulating a worker process restart — _seen_cache is
    a plain in-process set, gone on restart) WHILE the claim is still
    genuinely fresh (not stale enough for recovery). The durable Mongo
    record must still reject a second claim — the fix must not rely
    solely on the in-memory set, exactly the master prompt's explicit
    'do NOT rely only on an in-memory Python set' requirement."""
    assert await inbound._claim_message("wamid-restart-1") is True
    inbound._seen_cache.clear()  # simulates a fresh process — nothing remembered in-memory
    assert await inbound._claim_message("wamid-restart-1") is False, (
        "a fresh in-memory cache must not cause a durable Mongo claim to be re-issued"
    )


async def test_legitimate_second_command_different_message_id_executes_normally(_setup, monkeypatch):
    """Master prompt test #6: identical TEXT sent at different times
    (genuinely different WhatsApp message_id each time) must remain two
    separate, independently-executable commands — deduplication is keyed
    on message identity, never on text."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    text = "send anushka dhaka for lava"
    first = {
        "message_id": "wamid-dup-text-1", "text": text, "sender_name": "Raj",
        "sender_phone": "919198765444", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }
    second = {**first, "message_id": "wamid-dup-text-2"}

    queue = [first, second]

    async def fake_scan(page, group_name, participants_cache):
        if not queue:
            return [], 0.0, 0.0
        msg = queue.pop(0)
        if not await inbound._claim_message(msg["message_id"]):
            return [], 0.0, 0.0
        return [dict(msg)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatch_calls = []

    async def fake_post_inbound(http, **kwargs):
        dispatch_calls.append(kwargs["message_id"])
        return ("ok", {"reply": None, "operation_id": None, "handled": True})

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    monkeypatch.setattr(inbound, "_send_reply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no reply expected")))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert dispatch_calls == ["wamid-dup-text-1", "wamid-dup-text-2"], (
        "two genuinely distinct WhatsApp messages with identical text must both execute"
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
        return ("ok", {"reply": None, "operation_id": None, "handled": True})

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    monkeypatch.setattr(inbound, "_send_reply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no reply expected")))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert dispatch_calls == ["wamid-new"], "a genuinely new message_id must be dispatched"


async def test_failed_backend_dispatch_is_retried_on_the_next_poll_cycle(_setup, monkeypatch):
    """Master prompt section 7 / test #12 ('a failed command can still be
    retried intentionally'): a transient backend failure (exception,
    timeout — _post_inbound returns None either way) must RELEASE the
    claim taken during the scan phase so the NEXT poll cycle retries it
    from scratch; once the backend actually succeeds, it must be
    dispatched exactly once more and then completed — never left
    retrying forever, never double-processed."""
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
        if not await inbound._claim_message(raw_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    attempts = {"n": 0}

    async def flaky_post_inbound(http, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ("unreachable", None)  # genuine connection failure -> claim released for retry
        return ("ok", {"reply": None, "operation_id": None, "handled": True})

    monkeypatch.setattr(inbound, "_post_inbound", flaky_post_inbound)

    # Cycle 1: backend fails -> claim must be RELEASED (not left dangling).
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 1
    assert await inbound._claim_message("wamid-retry-1") is True, (
        "a failed backend dispatch must release the claim so it can be "
        "claimed again on the next poll, not silently dropped or stuck forever"
    )
    # That direct claim call itself just claimed it again for this test's
    # own purposes — release it once more so poll_once's own retry below
    # (which does its own claim via fake_scan) starts from a clean slate.
    await inbound._release_claim("wamid-retry-1")

    # Cycle 2: backend succeeds -> dispatched exactly once more, then completed.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 2, "must retry exactly once on the next cycle, not loop within one cycle"
    assert await inbound._claim_message("wamid-retry-1") is False, "must be completed (claimed) after a successful dispatch"

    # Cycle 3: already processed -> must never be dispatched again.
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert attempts["n"] == 2, "must never be dispatched again once successfully processed"


async def test_long_running_send_remains_claimed_throughout_execution(_setup):
    """Master prompt test #8: a long-running SEND's claim must remain
    'in_progress' — and therefore un-claimable by anyone else — for the
    entire duration of its (bounded, but potentially many-seconds)
    synchronous dispatch, never expiring or becoming re-claimable merely
    because time has passed, as long as it stays within the recovery
    window."""
    assert await inbound._claim_message("wamid-longsend-1") is True
    # Simulate a genuinely long (but not yet recovery-eligible) dispatch
    # by backdating claimed_at short of the recovery threshold.
    db = inbound.get_db()
    doc = db[inbound.SEEN_COLLECTION]._docs["wamid-longsend-1"]
    doc["claimed_at"] = inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC - 5)
    assert await inbound._claim_message("wamid-longsend-1") is False, (
        "a claim still within the recovery window must never be reissued, "
        "no matter how long the legitimate operation is taking"
    )
    await inbound._complete_claim("wamid-longsend-1")


async def test_send_approval_message_is_independently_idempotent(_setup, monkeypatch):
    """Master prompt test #9: the SEND approval reply ('1') is itself a
    NEW inbound WhatsApp message with its own message_id — it must be
    claimed/deduped exactly like any other command, independent of the
    original SEND command's own message_id, so a repeated observation of
    the approval message never re-approves or re-dispatches media."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    approval_message = {
        "message_id": "wamid-approve-1", "text": "1", "sender_name": "Raj",
        "sender_phone": "919198765555", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(approval_message["message_id"]):
            return [], 0.0, 0.0
        return [dict(approval_message)], 0.0, 0.0

    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    approve_calls = []

    async def fake_post_inbound(http, **kwargs):
        approve_calls.append(kwargs["message_id"])
        return ("ok", {"reply": "✅ Approved — now sending...", "operation_id": None, "handled": True})

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)

    async def fake_send_reply(page, group_name, text):
        return 0.0, {}, "sent"

    monkeypatch.setattr(inbound, "_send_reply", fake_send_reply)

    for _ in range(3):
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                                 participants_cache=participants_cache)

    assert approve_calls == ["wamid-approve-1"], (
        f"the approval message must be dispatched (approved) exactly once, got {approve_calls}"
    )


async def test_abandoned_in_progress_claim_is_recovered_after_timeout(_setup):
    """Master prompt's recovery requirement: a claim genuinely abandoned
    (worker crashed mid-dispatch, claimed_at far older than
    _CLAIM_RECOVERY_TIMEOUT_SEC, in-memory cache gone) IS eligible for a
    bounded recovery — the system must not create a permanent deadlock."""
    assert await inbound._claim_message("wamid-abandoned-1") is True
    inbound._seen_cache.clear()  # the crashed process's memory is gone
    db = inbound.get_db()
    doc = db[inbound.SEEN_COLLECTION]._docs["wamid-abandoned-1"]
    doc["claimed_at"] = inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC + 30)
    assert await inbound._claim_message("wamid-abandoned-1") is True, (
        "a genuinely stale in_progress claim (older than the recovery timeout) must be recoverable"
    )


async def test_healthy_claim_not_stale_is_never_recovered(_setup):
    """The other half of the recovery contract: a claim that is
    in_progress but NOT yet stale enough must NEVER be 'recovered' —
    this is the master prompt's explicit warning ('a healthy long-running
    command must NOT be started again merely because it has been running
    for several minutes')."""
    assert await inbound._claim_message("wamid-healthy-1") is True
    inbound._seen_cache.clear()
    db = inbound.get_db()
    doc = db[inbound.SEEN_COLLECTION]._docs["wamid-healthy-1"]
    doc["claimed_at"] = inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC - 30)
    assert await inbound._claim_message("wamid-healthy-1") is False, (
        "a claim well within the recovery window must never be recovered/reissued"
    )


async def test_completed_claim_is_never_recovered_regardless_of_age(_setup):
    """A 'completed' claim (the command genuinely finished) must never be
    recovered/reprocessed even if it happens to look 'old' — only
    'in_progress' claims are ever recovery-eligible."""
    assert await inbound._claim_message("wamid-done-1") is True
    await inbound._complete_claim("wamid-done-1")
    inbound._seen_cache.clear()
    db = inbound.get_db()
    doc = db[inbound.SEEN_COLLECTION]._docs["wamid-done-1"]
    doc["claimed_at"] = inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC * 10)
    assert await inbound._claim_message("wamid-done-1") is False, (
        "a completed claim must never be reprocessed, regardless of how old it is"
    )


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


# ─────────────────────────────────────────────────────────────────────────
# Agent self-messages (master prompt test #7)
# ─────────────────────────────────────────────────────────────────────────

async def test_agent_own_acknowledgement_never_reingested_as_a_command(_setup):
    """The real _scan_group_for_new_messages marks an OUTGOING message
    (direction is True — our own "Got it — processing...", a form
    preview, a completion/error message, a HELP response, an ambiguity
    prompt) processed via this exact _mark_processed call, without ever
    adding it to new_messages (see the `if direction is True:` branch) —
    this proves that once marked this way, it is durably claimed and can
    never subsequently be claimed again, i.e. can never re-enter the
    dispatch path as if it were a new inbound command, regardless of how
    many more times the same outgoing bubble is observed in later scans."""
    own_message_id = "wamid-own-ack-1"
    await inbound._mark_processed(own_message_id)  # the exact call the direction=True branch makes
    assert await inbound._claim_message(own_message_id) is False, (
        "an agent's own outgoing message, once marked via the direction=True "
        "path, must never be claimable again as if it were a fresh inbound command"
    )


# ─────────────────────────────────────────────────────────────────────────
# 2026-09-11 — "Send Zeeshan Ali for Mahindra Thar" acked "Got it —
# processing..." FOUR times (~9:09 / 9:11 / 9:13 / 9:15). Root cause: the
# backend's synchronous SEND-confirmation preview scan exceeded the
# worker's _INBOUND_DISPATCH_TIMEOUT_SEC; _post_inbound returned None; the
# caller RELEASED the claim (deleting it) and re-dispatched — with no ack
# idempotency, every retry re-acked and re-triggered the same slow scan.
# Fixes: (a) _post_inbound discriminates timeout (backend HAS it — do not
# re-dispatch) from unreachable (never got it — safe silent retry); (b) a
# durable, claim-release-surviving ack-once marker.
# ─────────────────────────────────────────────────────────────────────────

async def test_slow_send_command_acked_exactly_once_and_not_redispatched(_setup, monkeypatch):
    """The exact Zeeshan Ali shape: the backend receives the command but
    its synchronous work runs past the dispatch timeout. Across many poll
    cycles: exactly ONE 'Got it — processing...' and exactly ONE dispatch
    — never four."""
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 0.0)
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Talentgram Scouting Agent"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw = {
        "message_id": "wamid-zeeshan-1", "text": "Send Zeeshan Ali for Mahindra Thar",
        "sender_name": "Raj", "sender_phone": "919198765900", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(raw["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw)], 0.0, 0.0
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    dispatches = []

    async def slow_then_timeout(http, **kwargs):
        dispatches.append(kwargs["message_id"])
        await asyncio.sleep(0.01)  # never completes before ACK_THRESHOLD (0.0)
        return ("timeout", None)   # backend received it, still working / already done
    monkeypatch.setattr(inbound, "_post_inbound", slow_then_timeout)

    acks = []

    async def fake_send_reply(page, group_name, text):
        acks.append(text)
        return 0.0, {}, "sent"
    monkeypatch.setattr(inbound, "_send_reply", fake_send_reply)

    for _ in range(6):
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                                participants_cache=participants_cache)

    assert dispatches == ["wamid-zeeshan-1"], f"one dispatch only, got {dispatches}"
    assert acks == [inbound.ACK_TEXT], f"exactly one processing-ack, got {acks}"
    assert await inbound._claim_message("wamid-zeeshan-1") is False, "a timed-out dispatch terminal-completes the claim"


async def test_ack_once_survives_a_claim_release_and_redispatch(_setup, monkeypatch):
    """A GENUINE connection failure legitimately releases the claim and
    the message is retried — but the durable ack-once marker means the
    'Got it — processing...' is still sent only ONCE across the retries."""
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 0.0)
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    raw = {
        "message_id": "wamid-ackonce-1", "text": "Send X for Y", "sender_name": "Raj",
        "sender_phone": "919198765901", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(raw["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw)], 0.0, 0.0
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    n = {"i": 0}

    async def flaky(http, **kwargs):
        n["i"] += 1
        await asyncio.sleep(0.01)
        if n["i"] <= 2:
            return ("unreachable", None)  # genuine outage -> claim released, retried
        return ("ok", {"reply": "SEND FORM PREVIEW", "operation_id": None, "handled": True})
    monkeypatch.setattr(inbound, "_post_inbound", flaky)

    acks = []

    async def fake_send_reply(page, group_name, text):
        acks.append(text)
        return 0.0, {}, "sent"
    monkeypatch.setattr(inbound, "_send_reply", fake_send_reply)

    for _ in range(4):
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                                participants_cache=participants_cache)

    assert n["i"] == 3, f"retried on each unreachable, then succeeded — {n['i']} dispatches"
    assert acks.count(inbound.ACK_TEXT) == 1, f"processing-ack sent exactly once despite retries, got {acks}"
    assert "SEND FORM PREVIEW" in acks, "the real reply still lands once the backend recovers"


async def test_claim_ack_is_atomic_exactly_one_of_concurrent_callers_wins(_setup):
    """Concurrent delivery of the same message_id: exactly one caller gets
    to send the ack (durable unique-index gate, same guarantee the claim
    itself has)."""
    inbound._acked_cache.clear()
    results = await asyncio.gather(
        inbound._claim_ack("wamid-ackrace-1"),
        inbound._claim_ack("wamid-ackrace-1"),
        inbound._claim_ack("wamid-ackrace-1"),
        inbound._claim_ack("wamid-ackrace-1"),
    )
    assert sorted(results) == [False, False, False, True], results


async def test_timeout_outcome_never_releases_the_claim(_setup, monkeypatch):
    """A ('timeout', None) from _post_inbound must terminal-complete the
    claim, NOT release it — the backend has the request; re-dispatch is
    pure harm (redundant work + duplicate ack)."""
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 0.0)
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    raw = {
        "message_id": "wamid-timeout-noretry-1", "text": "Send A for B", "sender_name": "Raj",
        "sender_phone": "919198765902", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }

    async def fake_scan(page, group_name, participants_cache):
        if not await inbound._claim_message(raw["message_id"]):
            return [], 0.0, 0.0
        return [dict(raw)], 0.0, 0.0
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    calls = {"n": 0}

    async def timing_out(http, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return ("timeout", None)
    monkeypatch.setattr(inbound, "_post_inbound", timing_out)
    monkeypatch.setattr(inbound, "_send_reply", lambda *a, **k: _noop_reply())

    for _ in range(5):
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache,
                                participants_cache=participants_cache)
    assert calls["n"] == 1, f"timed-out dispatch must not be re-dispatched, got {calls['n']}"


async def _noop_reply():
    return 0.0, {}, "sent"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
