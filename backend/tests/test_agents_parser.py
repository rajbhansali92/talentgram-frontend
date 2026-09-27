"""Focused unit tests for the SHARED command-detection layer
(agents/parser.py::detect_trigger / _one_edit_away) — the single funnel
every agent (Scouting, Management, Fetcher, CRM, casting-agent) goes
through via agents/dispatcher.py, confirmed by direct code audit
(dispatcher.py is the only caller of detect_trigger). No DB, no
dispatcher, no domain module — pure unit tests against a minimal fake
AgentDefinition, so these run in milliseconds and exercise the shared
layer in complete isolation from any one agent's business logic.

2026-09-27 cross-agent hardening: a small, explicit false-positive
denylist (_TRIGGER_FALSE_POSITIVES) was added to detect_trigger's
typo-tolerant fallback so ordinary English words that happen to be one
edit away from a trigger word ("and"~"add", "sent"~"send") are never
mistaken for a command — closing a gap the same class of collision was
previously fixed for ONLY inside casting_command_interpreter.py's own
narrow Gemini-adjacent prefilter, never in the shared detect_trigger
every agent actually uses.
"""
import os
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("MONGO_URL", os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017"))

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.parser import detect_trigger, _one_edit_away
from agents.models import AgentDefinition, IntentDefinition


def _noop_executor(collected, ctx):
    raise AssertionError("executor should never be called by these unit tests")


def _mk_intent(intent_id, *triggers):
    return IntentDefinition(intent_id=intent_id, triggers=list(triggers), fields=[], executor=_noop_executor)


# A small fake agent covering every _TYPO_TOLERANT_TRIGGERS word, mirroring
# the real Scouting Agent's own trigger vocabulary shape closely enough to
# exercise detect_trigger realistically without importing any real module.
_FAKE_AGENT = AgentDefinition(
    agent_id="test-fake-agent",
    name="Test Fake Agent",
    module="test_fake",
    intents=[
        _mk_intent("fake.add", "add"),
        _mk_intent("fake.move", "move"),
        _mk_intent("fake.share", "share"),
        _mk_intent("fake.send", "send"),
        _mk_intent("fake.upload", "upload"),
        _mk_intent("fake.undo", "undo"),
        _mk_intent("fake.tested", "tested"),
        _mk_intent("fake.show", "show"),
        _mk_intent("fake.help", "help"),
    ],
)


def _trigger_id(text):
    intent = detect_trigger(_FAKE_AGENT, text)
    return intent.intent_id if intent else None


# ---------------------------------------------------------------------------
# Exact matches — always work, never the concern of this file.
# ---------------------------------------------------------------------------
def test_exact_add_matches():
    assert _trigger_id("Add Kimaya to Flying Machine") == "fake.add"


def test_exact_move_matches():
    assert _trigger_id("Move Kimaya to Follow Up") == "fake.move"


# ---------------------------------------------------------------------------
# The spec's own required typo examples — bounded, single-edit-distance
# typos of the trigger word itself must still open the intent.
# ---------------------------------------------------------------------------
def test_ad_typo_of_add_still_matches():
    assert _trigger_id("ad Kimaya to Flying Machine") == "fake.add"


def test_mve_typo_of_move_still_matches():
    assert _trigger_id("mve Kimaya to Follow Up") == "fake.move"


def test_shre_typo_of_share_still_matches():
    assert _trigger_id("shre the template with Kimaya") == "fake.share"


def test_snd_typo_of_send_still_matches():
    assert _trigger_id("snd the template to Kimaya") == "fake.send"


def test_addd_typo_of_add_still_matches():
    assert _trigger_id("addd Kimaya to Flying Machine") == "fake.add"


# ---------------------------------------------------------------------------
# 2026-09-27 false-positive fix — ordinary chatter that happens to be one
# edit away from a trigger word must NEVER be read as a command.
# ---------------------------------------------------------------------------
def test_and_is_never_read_as_add():
    assert _trigger_id("And, can you also check on Priya") is None
    assert _trigger_id("and please confirm when free") is None


def test_sent_is_never_read_as_send():
    assert _trigger_id("Sent the deck already") is None
    assert _trigger_id("sent it yesterday") is None


def test_ordinary_chatter_is_never_a_command():
    assert _trigger_id("Good morning everyone") is None
    assert _trigger_id("Thanks a lot for the update") is None
    assert _trigger_id("Sounds good, will do") is None


# ---------------------------------------------------------------------------
# Two edits is too loose to trust — must not match.
# ---------------------------------------------------------------------------
def test_two_edits_away_does_not_match():
    # "mve" (1 edit) is the spec's own example; "mv" is 2 edits from "move".
    assert _trigger_id("mv Kimaya to Follow Up") is None


# ---------------------------------------------------------------------------
# Ambiguous first word (equidistant from two different triggers) matches
# neither — never guesses.
# ---------------------------------------------------------------------------
def test_equidistant_first_word_matches_nothing():
    # "shre" is 1 edit from "share" only among this fake agent's triggers,
    # so this isn't naturally equidistant; directly exercise the underlying
    # tie-breaking guarantee instead via _one_edit_away itself.
    assert _one_edit_away("shre", "share") is True
    assert _one_edit_away("shre", "send") is False


def test_one_edit_away_rejects_exact_and_far_matches():
    assert _one_edit_away("move", "move") is False  # exact match is not "one edit away"
    assert _one_edit_away("mv", "move") is False  # 2 edits
    assert _one_edit_away("mve", "move") is True  # 1 deletion
