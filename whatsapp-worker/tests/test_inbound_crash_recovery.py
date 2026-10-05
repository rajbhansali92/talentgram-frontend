"""Regression tests for the 2026-09-30 crash-recovery fix.

Production incident: a WhatsApp worker process crash/restart that
interrupted a command AFTER the backend had already executed it and
returned a reply — but BEFORE the worker's own claim reached "completed"
— left that claim stuck "in_progress" forever. The existing 180s
stale-claim recovery (2026-09-08 fix) then treated it as unconditionally
safe to retry from scratch, re-dispatching the SAME message_id and
re-executing an already-answered command. Confirmed directly against
production: a whatsapp_agent_audit_log row proving "Show me Fanny Gandhi's
form for Carter" executed successfully, correlated with its
whatsapp_inbound_seen claim still sitting in_progress 15+ hours later.

The fix has two independent layers, both exercised here:
  1. Worker-side: `dispatch_started_at` (see _mark_dispatch_started) lets
     a recovered claim be classified as "genuinely never dispatched"
     (safe to treat as fresh — unchanged from the 2026-09-08 behavior) vs
     "dispatch was attempted, outcome unknown" (needs the safeguards
     below) — see _recovery_needs_idempotent_redispatch.
  2. Backend-side: agents/inbound_idempotency.py (tested independently in
     backend/tests/test_inbound_idempotency.py) guarantees a command's
     side effects run AT MOST ONCE per message_id, no matter how many
     times /inbound is called for it. This file simulates that boundary
     with a minimal in-memory stand-in (_FakeBackend) so the FULL
     worker-redispatch-meets-backend-idempotency round trip is provable
     from the worker's own test suite, without a live backend.

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_inbound_crash_recovery.py -q
"""
import asyncio
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest
from pymongo.errors import DuplicateKeyError

import inbound  # noqa: E402
import sender  # noqa: E402

pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────
# Shared fakes (same shape as test_inbound_concurrency.py's, kept local to
# this file so it stays self-contained and independently runnable).
# ─────────────────────────────────────────────────────────────────────────

class FakeCollection:
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
                return
        del self._docs[key]

    async def insert_one(self, doc):
        key = doc.get("message_id")
        if key is not None:
            if key in self._docs:
                raise DuplicateKeyError("E11000 duplicate key (fake)")
            self._docs[key] = dict(doc)
        self.inserted.append(doc)

    async def find_one_and_update(self, filt, update, **kwargs):
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
        self.page = object()
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
    """Claim-aware stand-in for _scan_group_for_new_messages: mirrors the
    REAL scan-phase gate (inbound.py) — the atomic _claim_message check,
    then the recovery-uncertainty classification — for each candidate, once.
    A plain pass-through fake would bypass the claim gate entirely and
    could not prove that a completed/duplicate claim is never redispatched."""
    remaining = {"left": True}

    async def fake_scan(page, group_name, participants_cache):
        if not remaining["left"]:
            return [], 0.0, 0.0
        remaining["left"] = False
        out = []
        for m in messages:
            if not await inbound._claim_message(m["message_id"]):
                continue
            claim_state = (
                "recovered_uncertain"
                if await inbound._recovery_needs_idempotent_redispatch(m["message_id"])
                else "fresh"
            )
            out.append({**m, "claim_state": claim_state})
        return out, 0.0, 0.0

    return fake_scan


def _crash_worker():
    """Simulates a full worker process restart: the in-memory caches
    (both are plain in-process sets/dicts) are gone; only whatever was
    already durably written to the (fake) Mongo collection survives."""
    inbound._seen_cache.clear()
    inbound._acked_cache.clear()


def _age_claim_past_recovery(fake_db, message_id, extra_sec=30):
    doc = fake_db[inbound.SEEN_COLLECTION]._docs[message_id]
    doc["claimed_at"] = inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC + extra_sec)


class _FakeBackend:
    """Minimal in-memory stand-in for agents/inbound_idempotency.py's
    real, Mongo-backed claim_or_get_result/persist_result (independently
    tested against real Mongo in
    backend/tests/test_inbound_idempotency.py). Lets this worker-side
    suite prove the FULL round trip invariant end to end — "the command's
    side effects run at most once" — without a live backend process."""

    def __init__(self):
        self.execution_count: dict[str, int] = {}
        self._results: dict[str, dict] = {}

    async def dispatch(self, message_id: str, *, reply: str, operation_id=None):
        """Stands in for the worker's real _post_inbound + the backend's
        real /inbound handler combined: returns ("ok", body) exactly like
        _post_inbound does, but ONLY actually executes (increments
        execution_count) the first time this message_id is seen — every
        subsequent call, however many times it's redispatched, returns
        the SAME stored body without re-executing."""
        if message_id in self._results:
            return "ok", self._results[message_id]
        self.execution_count[message_id] = self.execution_count.get(message_id, 0) + 1
        body = {"handled": True, "reply": reply, "operation_id": operation_id}
        self._results[message_id] = body
        return "ok", body


def _recording_send_reply(sent: list):
    """Returns an async _send_reply stand-in that records (text, is_retry)
    and reports a successful send — used everywhere this file only cares
    about WHAT was sent and with what is_retry flag, not the real DOM."""
    async def fake(page, group_name, text, is_retry=False):
        sent.append((text, is_retry))
        return 0.0, {}, "wamid-reply-1"
    return fake


def _raw_msg(message_id, text="send anushka dhaka for lava", **overrides):
    base = {
        "message_id": message_id, "text": text, "sender_name": "Raj",
        "sender_phone": "919876543210", "sender_is_group_member": True,
        "raw_pre_plain_text": None, "media_type": None, "reply_context": None,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────
# Window 1 — claim created, crash before dispatch even starts, restart.
# ─────────────────────────────────────────────────────────────────────────

async def test_window1_crash_before_dispatch_executes_exactly_once(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w1-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))

    # 1. Claim, then crash BEFORE ever calling _post_inbound.
    claimed = await inbound._claim_message(mid)
    assert claimed is True
    _crash_worker()

    # 2. Restart: the claim is still fresh (not yet stale) immediately
    # after a crash — a real crash-then-immediate-restart wouldn't yet be
    # past the 180s recovery window. Age it to simulate real elapsed time.
    _age_claim_past_recovery(fake_db, mid)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count.get(mid, 0) == 1, "the command must execute exactly once"
    assert len(sent) == 1, "the reply must be sent exactly once"
    assert sent[0] == ("Done.", False), "a claim never dispatched before the crash must be treated as fresh (is_retry=False)"


# ─────────────────────────────────────────────────────────────────────────
# Window 2 — dispatch_started persisted, crash before the backend ever
# receives the request, restart: the command must NOT be lost.
# ─────────────────────────────────────────────────────────────────────────

async def test_window2_crash_before_backend_receives_request_command_not_lost(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w2-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))

    # 1. Claim, mark dispatch started (worker reached this point)...
    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    # ...then crash BEFORE the backend call is ever actually made (no
    # entry in backend.execution_count at all for this message_id).
    _crash_worker()
    assert mid not in backend.execution_count

    # 2. Restart, recover.
    _age_claim_past_recovery(fake_db, mid)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count.get(mid, 0) == 1, "the command must NOT be lost — it must eventually execute"
    assert len(sent) == 1, "its reply must eventually be sent"


# ─────────────────────────────────────────────────────────────────────────
# Window 3 — THE PRODUCTION INCIDENT: backend receives + executes the
# command, worker crashes before _complete_claim, restart.
# ─────────────────────────────────────────────────────────────────────────

async def test_window3_crash_after_backend_execution_no_second_execution(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w3-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    # 1. Claim, mark dispatch started, and let the backend ACTUALLY
    # execute (simulating the proven incident: the backend ran the
    # command and produced a reply) — then crash before this worker
    # process does anything else (no _complete_claim, no reply sent).
    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    outcome, body = await backend.dispatch(mid, reply="Talentgram x Carter's - Form\n\nfanny - g")
    assert outcome == "ok"
    assert backend.execution_count[mid] == 1
    _crash_worker()

    # 2. Restart, recover, redispatch through the (now-idempotent)
    # backend boundary.
    _age_claim_past_recovery(fake_db, mid)

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Talentgram x Carter's - Form\n\nfanny - g")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid, text="Show me Fanny Gandhi's form for Carter")]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count[mid] == 1, "backend command execution count must stay 1 — no second Fletcher/casting execution"
    assert len(sent) == 1
    assert sent[0][1] is True, "a recovered, dispatch-uncertain redispatch must send its reply with is_retry=True (dedupe-checked)"


# ─────────────────────────────────────────────────────────────────────────
# Window 4 — backend execution confirmed, reply not yet sent, crash,
# restart: no re-execution, and the (never-actually-sent) reply is
# delivered exactly once.
# ─────────────────────────────────────────────────────────────────────────

async def test_window4_reply_not_yet_sent_delivered_exactly_once_no_reexecution(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w4-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()
    reply_text = "Done."

    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    await backend.dispatch(mid, reply=reply_text)  # backend executed; reply never sent
    _crash_worker()
    _age_claim_past_recovery(fake_db, mid)

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply=reply_text)

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)

    # DOM dedupe check simulator: nothing has actually been sent yet, so
    # _already_delivered-style detection correctly finds nothing and the
    # send proceeds for real.
    dom_sent_messages: list[str] = []

    async def fake_send_whatsapp_message(page, destination_type, destination, message_body, fast=False, is_retry=False, **kw):
        if is_retry and message_body in dom_sent_messages:
            return {"state": "MESSAGE_SENT_AND_VERIFIED", "evidence": {}, "timing": {}, "sent_message_id": "prior-bubble"}
        dom_sent_messages.append(message_body)
        return {"state": "MESSAGE_SENT_AND_VERIFIED", "evidence": {}, "timing": {}, "sent_message_id": "new-bubble"}

    monkeypatch.setattr(sender, "send_whatsapp_message", fake_send_whatsapp_message)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count[mid] == 1, "no re-execution"
    assert dom_sent_messages == [reply_text], "the never-actually-sent reply must be delivered exactly once"


# ─────────────────────────────────────────────────────────────────────────
# Window 5 — reply successfully sent, worker crashes before the final
# completion marker, restart: no duplicate command execution, and — given
# the existing is_retry/_already_delivered DOM check — no duplicate
# reply either. Documents the one narrow residual limitation (below).
# ─────────────────────────────────────────────────────────────────────────

async def test_window5_reply_already_sent_no_duplicate_reply_via_dom_dedupe(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w5-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()
    reply_text = "Talentgram x Santoor (Main Girl) - Form\n\nAngela - K"

    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    await backend.dispatch(mid, reply=reply_text)
    # The reply WAS actually sent by the prior (crashed) attempt — model
    # this as already present in the chat, exactly like sender.py's real
    # _already_delivered would see it on a genuine WhatsApp DOM.
    dom_sent_messages = [reply_text]
    _crash_worker()
    _age_claim_past_recovery(fake_db, mid)

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply=reply_text)

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)

    async def fake_send_whatsapp_message(page, destination_type, destination, message_body, fast=False, is_retry=False, **kw):
        if is_retry and message_body in dom_sent_messages:
            # _already_delivered short-circuit: reports success WITHOUT
            # typing/sending anything new.
            return {"state": "MESSAGE_SENT_AND_VERIFIED", "evidence": {}, "timing": {}, "sent_message_id": "prior-bubble"}
        dom_sent_messages.append(message_body)
        return {"state": "MESSAGE_SENT_AND_VERIFIED", "evidence": {}, "timing": {}, "sent_message_id": "new-bubble"}

    monkeypatch.setattr(sender, "send_whatsapp_message", fake_send_whatsapp_message)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count[mid] == 1, "no re-execution"
    assert dom_sent_messages == [reply_text], (
        "no duplicate reply — the DOM dedupe check (is_retry -> _already_delivered) correctly finds the "
        "reply already present and does not send it again. LIMITATION (documented, not hidden): this "
        "protection depends on the prior reply being visible in the scanned DOM window at the moment of "
        "recovery; if WhatsApp's own client hasn't rendered/synced it yet, or it has scrolled out of the "
        "short lookback the check scans, a duplicate reply is still possible. This is a narrower residual "
        "gap than the incident this fix closes (which had NO protection at all), not a full guarantee."
    )


# ─────────────────────────────────────────────────────────────────────────
# Window 6 — normal fresh command, exactly once.
# ─────────────────────────────────────────────────────────────────────────

async def test_window6_normal_fresh_command_exactly_once(_setup, monkeypatch):
    mid = "wamid-w6-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count.get(mid, 0) == 1
    assert sent == [("Done.", False)], "a genuinely fresh command must never be treated as a recovered/uncertain one"


# ─────────────────────────────────────────────────────────────────────────
# Window 7 — same message_id encountered twice in one scan cycle's
# results: exactly one logical execution.
# ─────────────────────────────────────────────────────────────────────────

async def test_window7_duplicate_message_id_same_scan_one_execution(_setup, monkeypatch):
    mid = "wamid-w7-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))

    # The same message_id appears twice in the raw DOM scan; the claim-aware
    # scan gate (mirroring the real one) must only let one through.
    monkeypatch.setattr(
        inbound, "_scan_group_for_new_messages",
        _fake_scan_returning([_raw_msg(mid), _raw_msg(mid)]),
    )
    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count.get(mid, 0) == 1
    assert len(sent) == 1


# ─────────────────────────────────────────────────────────────────────────
# Window 8 — worker restart after a fully completed command: zero output.
# ─────────────────────────────────────────────────────────────────────────

async def test_window8_restart_after_completed_command_zero_output(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-w8-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    backend = _FakeBackend()

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))

    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    await backend.dispatch(mid, reply="Done.")
    await inbound._complete_claim(mid)  # genuinely finished, unlike Windows 3-5

    _crash_worker()
    fake_db[inbound.SEEN_COLLECTION]._docs[mid]["claimed_at"] = (
        inbound._now() - timedelta(seconds=inbound._CLAIM_RECOVERY_TIMEOUT_SEC * 100)
    )
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count.get(mid, 0) == 1, "a completed claim must never be recovered/redispatched, regardless of age"
    assert sent == [], "zero output — no ack, no reply, nothing"


# ─────────────────────────────────────────────────────────────────────────
# Item 9 — the exact production Fletcher case, reproduced end to end.
# ─────────────────────────────────────────────────────────────────────────

async def test_fletcher_show_me_replay_reproduction(_setup, monkeypatch):
    """Direct reproduction of the proven production incident: 'Show me
    Fanny Gandhi's form for Carter' executes once (backend-confirmed via
    the real whatsapp_agent_audit_log correlation found during
    investigation), the worker crashes before completing its own claim,
    and a later recovery must not cause a second Fletcher execution, a
    second 'Got it — processing...' ack, or a second outbound Form
    reply."""
    fake_db = _setup
    mid = "wamid-fanny-carter-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Talentgram Fetcher Agent"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    form_reply = (
        "Talentgram x Carter's - Form\n\nfanny - g\nAge - 26\nHeight - 5'6\"\n"
        "Current Location - Mumbai, India; Delhi, India; Chandigarh, India\n"
        "Availability - Available\nCompetitive Brand - [blank]\n"
        "Instagram link - https://www.instagram.com/fannygandhi/"
    )
    backend = _FakeBackend()

    # First attempt: backend genuinely executes Fletcher's SHOW ME and
    # produces the real form reply — then the worker dies before
    # _complete_claim (the proven incident).
    claimed = await inbound._claim_message(mid)
    assert claimed is True
    await inbound._mark_dispatch_started(mid)
    await backend.dispatch(mid, reply=form_reply)
    assert backend.execution_count[mid] == 1
    _crash_worker()
    _age_claim_past_recovery(fake_db, mid)

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply=form_reply)

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    acks_and_replies: list[tuple[str, bool]] = []

    async def fake_send_whatsapp_message(page, destination_type, destination, message_body, fast=False, is_retry=False, **kw):
        acks_and_replies.append((message_body, is_retry))
        return {"state": "MESSAGE_SENT_AND_VERIFIED", "evidence": {}, "timing": {}, "sent_message_id": f"wamid-out-{len(acks_and_replies)}"}

    monkeypatch.setattr(sender, "send_whatsapp_message", fake_send_whatsapp_message)
    monkeypatch.setattr(
        inbound, "_scan_group_for_new_messages",
        _fake_scan_returning([_raw_msg(mid, text="Show me Fanny Gandhi's form for Carter",
                                        sender_name="Divyani")]),
    )

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    assert backend.execution_count[mid] == 1, "Fletcher execution count: must be 1"
    form_sends = [m for m, _ in acks_and_replies if m == form_reply]
    assert len(form_sends) == 1, "Form reply generation/send count: must be 1"
    ack_sends = [m for m, _ in acks_and_replies if m == inbound.ACK_TEXT]
    assert len(ack_sends) == 0, "no fresh 'Got it — processing...' caused by recovery (backend responded well within ACK_THRESHOLD_SEC)"


# ─────────────────────────────────────────────────────────────────────────
# Item 6 — "Got it — processing..." must not be re-sent by recovery, even
# when the recovered redispatch is itself slow enough to cross the ack
# threshold. The ack has its own durable per-message_id marker
# (ACK_COLLECTION, unique index), independent of claim state.
# ─────────────────────────────────────────────────────────────────────────

async def test_recovery_never_resends_the_processing_ack(_setup, monkeypatch):
    fake_db = _setup
    mid = "wamid-ack-recovery-1"
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    monkeypatch.setattr(inbound, "ACK_THRESHOLD_SEC", 0.01)

    # Prior attempt: claimed, dispatched, and already sent the ack, then died.
    assert await inbound._claim_message(mid) is True
    await inbound._mark_dispatch_started(mid)
    assert await inbound._claim_ack(mid) is True
    _crash_worker()
    _age_claim_past_recovery(fake_db, mid)

    backend = _FakeBackend()

    async def slow_post_inbound(http, **kwargs):
        await asyncio.sleep(0.1)  # crosses ACK_THRESHOLD_SEC
        return await backend.dispatch(kwargs["message_id"], reply="Done.")

    monkeypatch.setattr(inbound, "_post_inbound", slow_post_inbound)
    sent = []
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)

    texts = [t for t, _ in sent]
    assert inbound.ACK_TEXT not in texts, "a recovered message must never get a second 'Got it — processing...'"
    assert texts == ["Done."]


# ─────────────────────────────────────────────────────────────────────────
# ROOT CAUSE regression (2026-10-05): the 48h dedupe-record TTL expiring
# while the message is still inside the 15-message scan tail.
# ─────────────────────────────────────────────────────────────────────────

def _ttl_sweep(fake_db, now):
    """Stand-in for Mongo's TTL monitor on SEEN_COLLECTION.created_at."""
    coll = fake_db[inbound.SEEN_COLLECTION]
    cutoff = now - timedelta(seconds=inbound.config.INBOUND_DEDUP_TTL_SEC)
    for mid in [m for m, d in coll._docs.items()
                if inbound._as_aware_utc(d.get("created_at")) < cutoff]:
        del coll._docs[mid]


async def _run_visible_message_for(hours, fake_db, monkeypatch, mid="wamid-ttl-1", step_h=3):
    """A command is executed once, then simply STAYS visible in the chat
    tail for `hours` while the worker restarts every `step_h` hours and
    Mongo's TTL monitor runs. Returns the backend + everything sent."""
    from datetime import datetime, timezone
    clock = {"t": datetime(2026, 9, 27, 13, 24, tzinfo=timezone.utc)}
    monkeypatch.setattr(inbound, "_now", lambda: clock["t"])
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Talentgram Fetcher Agent"])
    participants_cache = inbound.GroupParticipantsCache()
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)
    backend = _FakeBackend()
    sent = []

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="Talentgram x Carter's - Form\n\nfanny - g")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))

    for _ in range(int(hours / step_h) + 1):
        _crash_worker()          # worker restart: in-memory caches gone
        inbound._last_keepalive.clear()
        inbound._last_seen_generation = None
        monkeypatch.setattr(
            inbound, "_scan_group_for_new_messages",
            _fake_scan_returning([_raw_msg(mid, text="Show me Fanny Gandhi's form for Carter")]),
        )
        await inbound.poll_once(session, http=object(), groups_cache=groups_cache, participants_cache=participants_cache)
        _ttl_sweep(fake_db, clock["t"])
        clock["t"] = clock["t"] + timedelta(hours=step_h)
    return backend, sent


async def test_visible_message_never_replays_when_its_48h_ttl_would_expire(_setup, monkeypatch):
    backend, sent = await _run_visible_message_for(7 * 24, _setup, monkeypatch)
    assert backend.execution_count["wamid-ttl-1"] == 1, "a message that merely stays visible must execute exactly once, ever"
    assert len(sent) == 1, "and its form must be sent exactly once, ever (no 48h replay, no re-ack)"


async def test_control_without_keepalive_reproduces_the_48h_replay(_setup, monkeypatch):
    """Control: disable the keep-alive (== pre-fix behaviour) and the SAME
    scenario replays the command, reproducing the production incident
    (a repeat every ~48h)."""
    monkeypatch.setattr(inbound, "_KEEPALIVE_AFTER_SEC", 1e12)
    backend, sent = await _run_visible_message_for(7 * 24, _setup, monkeypatch)
    # Backend stand-in is itself idempotent, so count worker-side replays.
    assert len(sent) >= 3, f"pre-fix behaviour must replay roughly every 48h over 7 days, sent={len(sent)}"


async def test_keepalive_is_throttled_not_a_write_per_poll(_setup, monkeypatch):
    from datetime import datetime, timezone
    clock = {"t": datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(inbound, "_now", lambda: clock["t"])
    assert await inbound._claim_message("wamid-throttle-1") is True
    writes = []
    coll = _setup[inbound.SEEN_COLLECTION]
    orig = coll.update_one

    async def counting(filt, update, **kw):
        if "created_at" in (update.get("$set") or {}):
            writes.append(clock["t"])
        return await orig(filt, update, **kw)

    coll.update_one = counting
    for _ in range(500):           # 500 polls inside the throttle window
        await inbound._claim_message("wamid-throttle-1")
        clock["t"] += timedelta(seconds=2)
    assert writes == [], "no keep-alive writes within the 6h throttle window"
    clock["t"] += timedelta(hours=7)
    await inbound._claim_message("wamid-throttle-1")
    await inbound._claim_message("wamid-throttle-1")
    assert len(writes) == 1, "exactly one renewal once the window has elapsed"


# ─────────────────────────────────────────────────────────────────────────
# Legacy stuck claims (pre-fix, no dispatch_started_at) are never replayed.
# ─────────────────────────────────────────────────────────────────────────

async def test_legacy_stale_in_progress_claim_is_quarantined_never_replayed(_setup, monkeypatch):
    from datetime import datetime, timezone
    mid = "conv-msg-LEGACY1"
    old = datetime(2026, 10, 4, 5, 21, tzinfo=timezone.utc)   # before _LEGACY_CLAIM_CUTOFF
    _setup[inbound.SEEN_COLLECTION]._docs[mid] = {
        "message_id": mid, "status": "in_progress", "claimed_at": old, "created_at": old, "worker_id": "default",
    }
    session = FakeSession(generation=1)
    backend = _FakeBackend()
    sent = []
    monkeypatch.setattr(sender, "_open_group_chat", _fake_open_opened)

    async def fake_post_inbound(http, **kwargs):
        return await backend.dispatch(kwargs["message_id"], reply="REPLAYED")

    monkeypatch.setattr(inbound, "_post_inbound", fake_post_inbound)
    monkeypatch.setattr(inbound, "_send_reply", _recording_send_reply(sent))
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", _fake_scan_returning([_raw_msg(mid)]))

    await inbound.poll_once(session, http=object(), groups_cache=_groups_cache(["G"]),
                            participants_cache=inbound.GroupParticipantsCache())

    assert backend.execution_count.get(mid, 0) == 0, "a legacy stale claim must never be re-executed"
    assert sent == []
    doc = _setup[inbound.SEEN_COLLECTION]._docs[mid]
    assert doc["status"] == "completed" and doc["quarantined_legacy"] is True


async def test_post_fix_stale_claim_that_never_dispatched_is_still_recovered(_setup, monkeypatch):
    """Quarantine is only for pre-fix claims — a post-fix claim that
    genuinely never dispatched (worker crashed before the dispatch loop)
    must still be recovered: a legitimate command is never lost."""
    mid = "wamid-postfix-1"
    assert await inbound._claim_message(mid) is True
    _crash_worker()
    _age_claim_past_recovery(_setup, mid)
    assert await inbound._claim_message(mid) is True


# ─────────────────────────────────────────────────────────────────────────
# Stale-message guard: _message_age_sec (second layer behind keep-alive).
# ─────────────────────────────────────────────────────────────────────────

def _utc(y, mo, d, h=0, mi=0):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_age_of_the_exact_production_replay_messages():
    # "Show me Fanny Gandhi's form for Carter" was sent 9/27 ~13:24 and replayed 9/29 13:26.
    age = inbound._message_age_sec("[1:24 pm, 27/9/2026] Divyani: ", _utc(2026, 9, 29, 13, 26))
    assert 47.9 * 3600 < age < 48.2 * 3600
    assert age > inbound._MAX_NEW_COMMAND_AGE_SEC, "a 48h-old message must be skipped"


def test_fresh_messages_are_never_stale_including_across_timezone_skew():
    now = _utc(2026, 10, 5, 10, 0)
    assert inbound._message_age_sec("[9:55 am, 5/10/2026] X: ", now) < 600
    # browser rendering ~14h ahead/behind of this process clock still << 36h
    assert abs(inbound._message_age_sec("[11:55 pm, 4/10/2026] X: ", now)) < 14 * 3600
    assert inbound._message_age_sec("[11:55 pm, 4/10/2026] X: ", now) < inbound._MAX_NEW_COMMAND_AGE_SEC


def test_ambiguous_day_month_takes_the_most_recent_reading():
    # 5/10 is 5 Oct or 10 May — only call a message old if it is old under
    # BOTH readings; a fresh message is never mislabelled stale by locale.
    now = _utc(2026, 10, 5, 10, 0)
    for text in ("[9:00 am, 5/10/2026] X: ", "[9:00 am, 10/5/2026] X: "):
        age = inbound._message_age_sec(text, now)
        assert age is not None and age < inbound._MAX_NEW_COMMAND_AGE_SEC, text
    # old under BOTH readings (3 Apr / 4 Mar) -> stale
    assert inbound._message_age_sec("[9:00 am, 3/4/2026] X: ", now) > inbound._MAX_NEW_COMMAND_AGE_SEC


def test_24h_clock_and_unparseable_inputs():
    now = _utc(2026, 10, 5, 10, 0)
    assert inbound._message_age_sec("[13:24, 27/09/2026] X: ", now) > 36 * 3600
    for bad in (None, "", "Name: ", "[garbage] X", "[25:99, 1/1/2026] X", "[13:00 pm, 1/1/2026] X"):
        assert inbound._message_age_sec(bad, now) is None, bad
