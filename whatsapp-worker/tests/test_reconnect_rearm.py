"""Regression tests for the reconnect-reliability re-arm fix (2026-08-11).

Covers the bug where switching the authenticated WhatsApp account (logout +
QR rescan with a different account) left the inbound pipeline permanently
silent: _INVALID_GROUPS/_seen_cache/GroupParticipantsCache/KnownGroupsCache
are Python module globals that only ever got rebuilt by a full process
restart — the existing in-process reconnect path (worker.py's heartbeat-
failure branch) never touched them. Verifies the fix: a change in
WhatsAppSession.generation (the one signal that "an authentication just
happened", fired by session.py on every successful _authenticate()) makes
inbound.py rebuild every one of those caches, and a NOT_FOUND immediately
after that change is treated as "WhatsApp Web's chat list may still be
syncing" (grace period) rather than being marked permanently invalid — the
exact race that used to poison every configured group on an account switch.

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_reconnect_rearm.py -q
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest

import config  # noqa: E402
import inbound  # noqa: E402
import sender  # noqa: E402

pytestmark = pytest.mark.asyncio


class FakeCollection:
    def __init__(self):
        self.update_one_calls = []
        self.update_many_calls = []

    async def update_one(self, filt, update, **kwargs):
        self.update_one_calls.append((filt, update, kwargs))

    async def update_many(self, filt, update, **kwargs):
        self.update_many_calls.append((filt, update, kwargs))

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
    """Stands in for httpx.AsyncClient — KnownGroupsCache.get() only ever
    calls .get() on it."""
    def __init__(self, groups):
        self._groups = groups

    async def get(self, url, **kwargs):
        return FakeResponse(self._groups)


class FakeSession:
    def __init__(self, generation=1):
        self.generation = generation
        self.session_id = f"sess-{generation}"
        self.own_phone_number = None
        self.page = object()  # sentinel — never dereferenced; _open_group_chat is monkeypatched
        self.page_lock = asyncio.Lock()
        self.is_healthy = True


def _groups_cache(groups):
    """A real KnownGroupsCache backed by a fake HTTP client — exercises the
    actual refetch path (including the one _rearm_for_new_generation forces
    via .clear()) rather than poking private state directly."""
    return inbound.KnownGroupsCache(FakeHttp(groups))


def _reset_module_state():
    """Every test starts from a clean slate — these are the exact globals
    the bug this fix addresses left permanently poisoned in production."""
    inbound._INVALID_GROUPS.clear()
    inbound._PENDING_REVALIDATION.clear()
    inbound._seen_cache.clear()
    inbound._invalid_group_last_logged.clear()
    inbound._NOT_FOUND_STREAK.clear()
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


async def test_rearm_moves_invalid_groups_to_pending_and_clears_all_caches(_setup):
    inbound._INVALID_GROUPS.add("Stale Group")
    inbound._seen_cache.add("msg-1")
    groups_cache = _groups_cache(["Stale Group"])
    groups_cache._groups = ["Stale Group"]
    groups_cache._last_refresh = time.monotonic()
    participants_cache = inbound.GroupParticipantsCache()
    participants_cache._by_group["Stale Group"] = [{"phone": "9999"}]
    participants_cache._last_refresh["Stale Group"] = time.monotonic()
    session = FakeSession(generation=2)

    await inbound._rearm_for_new_generation(session, groups_cache, participants_cache)

    assert "Stale Group" not in inbound._INVALID_GROUPS
    assert "Stale Group" in inbound._PENDING_REVALIDATION
    assert inbound._seen_cache == set()
    assert groups_cache._groups == []
    assert groups_cache._last_refresh == 0.0
    assert participants_cache._by_group == {}
    assert participants_cache._last_refresh == {}


async def test_poll_once_detects_generation_change_and_rearms(_setup, monkeypatch):
    inbound._INVALID_GROUPS.add("Every Group")
    session = FakeSession(generation=5)
    groups_cache = _groups_cache(["Every Group"])
    participants_cache = inbound.GroupParticipantsCache()

    async def fake_open(page, group_name):
        return "OPENED"

    async def fake_scan(page, group_name, participants_cache):
        return [], 0.0, 0.0

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert inbound._last_seen_generation == 5
    assert "Every Group" not in inbound._INVALID_GROUPS, (
        "a group flagged invalid before the (re-)auth must be given a fresh "
        "chance the moment a new generation is observed — this is the "
        "actual fix for the reported bug"
    )


async def test_three_sequential_account_switches_each_rearm_correctly(_setup, monkeypatch):
    """Mirrors the exact manual smoke test: Account A -> Account B ->
    Account C, no restart in between. Each switch must independently give
    every group a fresh chance, not just the first one — proves the re-arm
    logic isn't a one-shot/idempotent-only mechanism."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Group X"])
    participants_cache = inbound.GroupParticipantsCache()

    async def fake_open_opened(page, group_name):
        return "OPENED"

    async def fake_scan(page, group_name, participants_cache):
        return [], 0.0, 0.0

    monkeypatch.setattr(sender, "_open_group_chat", fake_open_opened)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    seen_rearm_generations = []
    real_rearm = inbound._rearm_for_new_generation

    async def tracking_rearm(session, groups_cache, participants_cache):
        seen_rearm_generations.append(session.generation)
        await real_rearm(session, groups_cache, participants_cache)

    monkeypatch.setattr(inbound, "_rearm_for_new_generation", tracking_rearm)

    # Account A (generation 1, first-ever poll).
    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)
    # A poisons the group (simulating it going missing/unhealthy before B logs in).
    inbound._INVALID_GROUPS.add("Group X")

    # Account B (generation 2) — a fresh QR scan, no restart.
    session.generation = 2
    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert "Group X" not in inbound._INVALID_GROUPS, "must recover on switch to Account B"
    inbound._INVALID_GROUPS.add("Group X")  # poison again before the next switch

    # Account C (generation 3) — a SECOND fresh QR scan, still no restart.
    session.generation = 3
    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)
    assert "Group X" not in inbound._INVALID_GROUPS, "must recover on switch to Account C too"

    assert seen_rearm_generations == [1, 2, 3], (
        "every one of the three (re-)authentications must trigger its own "
        "rebuild — not just the first"
    )


async def test_second_poll_at_same_generation_does_not_rearm_again(_setup, monkeypatch):
    session = FakeSession(generation=3)
    groups_cache = _groups_cache(["G"])
    participants_cache = inbound.GroupParticipantsCache()

    async def fake_open(page, group_name):
        return "OPENED"

    async def fake_scan(page, group_name, participants_cache):
        return [], 0.0, 0.0

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    monkeypatch.setattr(inbound, "_scan_group_for_new_messages", fake_scan)

    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)
    rebuilt_after_first = inbound._state_rebuilt_at
    await asyncio.sleep(0.01)
    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert inbound._state_rebuilt_at == rebuilt_after_first, (
        "polling again at the SAME generation must not re-trigger the "
        "rebuild — only an actual generation change should"
    )


async def test_not_found_within_grace_period_does_not_poison(_setup, monkeypatch):
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Real Group"])
    participants_cache = inbound.GroupParticipantsCache()

    async def fake_open_not_found(page, group_name):
        return "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open_not_found)

    # generation=1 was never seen before -> this poll cycle re-arms, which
    # sets _state_rebuilt_at to "now" -> the rest of this test runs inside
    # the grace window.
    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert "Real Group" not in inbound._INVALID_GROUPS, (
        "a NOT_FOUND immediately after a (re-)auth must not permanently "
        "poison the group — this is the exact race an account switch used "
        "to trigger (WhatsApp Web's chat list still syncing)"
    )


async def test_not_found_after_grace_period_still_marks_invalid(_setup, monkeypatch):
    """A group that is ACTUALLY missing is still flagged once the grace window has elapsed AND the misses are
    sustained (config.GROUP_MISSING_CONFIRM_*). Before 2026-10-22 the first post-grace miss flagged it."""
    session = FakeSession(generation=1)
    groups_cache = _groups_cache(["Genuinely Missing Group"])
    participants_cache = inbound.GroupParticipantsCache()

    async def fake_open_not_found(page, group_name):
        return "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open_not_found)

    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)
    # Simulate the grace window having fully elapsed, and a sustained run of misses.
    inbound._state_rebuilt_at = time.monotonic() - config.REAUTH_GRACE_SEC - 1
    _age_streak("Genuinely Missing Group", seconds=config.GROUP_MISSING_CONFIRM_SEC + 1,
                misses=config.GROUP_MISSING_CONFIRM_MISSES - 1)

    await inbound.poll_once(session, http=None, groups_cache=groups_cache,
                             participants_cache=participants_cache)

    assert "Genuinely Missing Group" in inbound._INVALID_GROUPS, (
        "a group that's ACTUALLY missing (not just mid-sync) must still be "
        "marked invalid once the grace window has genuinely elapsed and the misses are sustained — this "
        "existing protective behavior (added to stop an infinite-retry log "
        "storm) must not regress"
    )


async def test_known_groups_cache_clear_forces_refetch():
    cache = _groups_cache(["A", "B"])
    cache._groups = ["A", "B"]
    cache._last_refresh = time.monotonic()
    cache.clear()
    assert cache._groups == []
    assert cache._last_refresh == 0.0


async def test_group_participants_cache_clear_resets_all_groups():
    cache = inbound.GroupParticipantsCache()
    cache._by_group = {"G1": [1], "G2": [2]}
    cache._last_refresh = {"G1": 1.0, "G2": 2.0}
    cache.clear()
    assert cache._by_group == {}
    assert cache._last_refresh == {}


def _age_streak(group, seconds, misses):
    """Pretend `group`'s current NOT_FOUND streak began `seconds` ago and already has `misses` misses."""
    inbound._NOT_FOUND_STREAK[group] = (time.monotonic() - seconds, misses)


async def _poll(session, groups_cache, participants_cache):
    await inbound.poll_once(session, http=None, groups_cache=groups_cache, participants_cache=participants_cache)


def _past_grace():
    inbound._state_rebuilt_at = time.monotonic() - config.REAUTH_GRACE_SEC - 1


def _agent_config_writes(fake_db):
    coll = fake_db[inbound.AGENT_CONFIG_COLLECTION]
    return coll.update_many_calls


# ---- 2026-10-22: a group that is temporarily missing past the grace window is retried, not flagged -------------------
async def test_delayed_chat_list_sync_is_retried_then_group_is_found_and_never_flagged(_setup, monkeypatch):
    """The default-worker restart: 'Talentgram CRM' / 'Talentgram Casting Pipeline' returned stale/empty search rows
    (NOT_FOUND) well after the grace window, then would have opened. They must not be flagged in the meantime."""
    fake_db = _setup
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Late Group"])

    async def fake_open(page, group_name):
        return "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)

    await _poll(session, groups_cache, participants)                 # re-arm + first miss (inside grace)
    _past_grace()
    for _ in range(config.GROUP_MISSING_CONFIRM_MISSES + 1):         # many misses past grace, but a SHORT streak
        _age_streak("Late Group", seconds=10, misses=inbound._NOT_FOUND_STREAK["Late Group"][1])
        await _poll(session, groups_cache, participants)
        assert "Late Group" not in inbound._INVALID_GROUPS
    assert _agent_config_writes(fake_db) == [], "no INVALID_CONFIGURATION write while it is still being retried"
    assert "Late Group" in inbound._NOT_FOUND_STREAK


async def test_a_successful_open_clears_the_streak(_setup, monkeypatch):
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Late Group"])
    calls = {"n": 0}

    async def fake_open(page, group_name):
        calls["n"] += 1
        return "OPENED" if calls["n"] > 3 else "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)

    await _poll(session, groups_cache, participants)
    _past_grace()
    for _ in range(2):
        await _poll(session, groups_cache, participants)
    assert inbound._NOT_FOUND_STREAK["Late Group"][1] == 3
    await _poll(session, groups_cache, participants)                # 4th probe opens (the scan body logs, not raises, on the stub page)
    assert "Late Group" not in inbound._NOT_FOUND_STREAK, "an OPENED probe must reset the miss streak"
    assert "Late Group" not in inbound._INVALID_GROUPS


async def test_previously_flagged_group_that_is_now_found_is_cleared(_setup, monkeypatch):
    """Eventual discovery of a DB-flagged (pending-revalidation) group after some misses auto-recovers it."""
    fake_db = _setup
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Casting Group"])
    inbound._PENDING_REVALIDATION.add("Casting Group")
    calls = {"n": 0}

    async def fake_open(page, group_name):
        calls["n"] += 1
        return "NOT_FOUND" if calls["n"] <= 3 else "OPENED"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)

    await _poll(session, groups_cache, participants)                # miss 1 (inside grace)
    _past_grace()
    for _ in range(2):                                              # misses 2-3, past grace: still pending, still unflagged
        await _poll(session, groups_cache, participants)
    assert "Casting Group" in inbound._PENDING_REVALIDATION and "Casting Group" not in inbound._INVALID_GROUPS
    assert _agent_config_writes(fake_db) == []
    await _poll(session, groups_cache, participants)                # 4th probe opens
    unsets = [c for c in _agent_config_writes(fake_db) if "$unset" in c[1]]
    assert unsets, "found again -> the DB flag must be cleared (INVALID_CONFIGURATION recovery)"
    assert "Casting Group" not in inbound._PENDING_REVALIDATION


async def test_genuinely_missing_group_needs_BOTH_enough_misses_and_enough_time(_setup, monkeypatch):
    fake_db = _setup
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Gone Group"])

    async def fake_open(page, group_name):
        return "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    await _poll(session, groups_cache, participants)
    _past_grace()
    n, t = config.GROUP_MISSING_CONFIRM_MISSES, config.GROUP_MISSING_CONFIRM_SEC

    _age_streak("Gone Group", seconds=t + 60, misses=n - 2)         # long enough, but this probe is only miss n-1
    await _poll(session, groups_cache, participants)
    assert "Gone Group" not in inbound._INVALID_GROUPS, "too few misses"

    _age_streak("Gone Group", seconds=t - 60, misses=n + 5)         # many misses, but not for long enough
    await _poll(session, groups_cache, participants)
    assert "Gone Group" not in inbound._INVALID_GROUPS, "not sustained for long enough"
    assert _agent_config_writes(fake_db) == []

    _age_streak("Gone Group", seconds=t + 1, misses=n - 1)          # both satisfied on this probe
    await _poll(session, groups_cache, participants)
    assert "Gone Group" in inbound._INVALID_GROUPS
    flagged = [c for c in _agent_config_writes(fake_db) if c[1].get("$set", {}).get("config_status") == inbound.INVALID_CONFIGURATION]
    assert len(flagged) == 1 and flagged[0][0] == {"group_names": "Gone Group"}
    assert "Gone Group" not in inbound._NOT_FOUND_STREAK


async def test_streaks_are_per_group_one_missing_group_does_not_affect_another(_setup, monkeypatch):
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Missing", "Fine"])

    async def fake_open(page, group_name):
        return "NOT_FOUND" if group_name == "Missing" else "OPENED"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    await _poll(session, groups_cache, participants)
    _past_grace()
    _age_streak("Missing", seconds=config.GROUP_MISSING_CONFIRM_SEC + 1, misses=config.GROUP_MISSING_CONFIRM_MISSES)
    await _poll(session, groups_cache, participants)
    assert "Missing" in inbound._INVALID_GROUPS and "Fine" not in inbound._INVALID_GROUPS
    assert "Fine" not in inbound._NOT_FOUND_STREAK


async def test_retryable_search_failure_neither_counts_nor_flags(_setup, monkeypatch):
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Flaky"])

    async def fake_open(page, group_name):
        return "SEARCH_FAILED"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    await _poll(session, groups_cache, participants)
    _past_grace()
    for _ in range(config.GROUP_MISSING_CONFIRM_MISSES + 2):
        await _poll(session, groups_cache, participants)
    assert "Flaky" not in inbound._INVALID_GROUPS and "Flaky" not in inbound._NOT_FOUND_STREAK


async def test_reauthentication_resets_miss_streaks(_setup, monkeypatch):
    session, participants = FakeSession(generation=1), inbound.GroupParticipantsCache()
    groups_cache = _groups_cache(["Late Group"])

    async def fake_open(page, group_name):
        return "NOT_FOUND"

    monkeypatch.setattr(sender, "_open_group_chat", fake_open)
    await _poll(session, groups_cache, participants)
    _past_grace()
    _age_streak("Late Group", seconds=config.GROUP_MISSING_CONFIRM_SEC - 5, misses=config.GROUP_MISSING_CONFIRM_MISSES - 1)
    session.generation = 2                                          # the account re-authenticated
    await _poll(session, groups_cache, participants)
    assert "Late Group" not in inbound._INVALID_GROUPS
    assert inbound._NOT_FOUND_STREAK["Late Group"][1] == 1, "streak restarted from this probe, not carried over"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
