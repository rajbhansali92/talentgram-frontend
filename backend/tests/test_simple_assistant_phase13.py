"""Simple Assistant — Phase 13: controlled AI provider validation & conversation-aware E2E.

Phase 13 is a VALIDATION CHECKPOINT, not a feature phase. This file proves the
full Phase 9–12 pipeline behaves correctly end to end:

    capture → talent/project resolve → bounded conversation context →
    multi-intent → verified facts → generation → validate_draft() →
    signed plan → human approval → existing WhatsApp Engine queue

Provider status is recorded explicitly (REAL / STUBBED / UNTESTED). This file
is pinned to SA_AI_PROVIDER=anthropic (inherited from the shared
test_simple_assistant_ai_response harness it imports) — Gemini is now the
default provider post-migration (see simple_assistant/ai_gemini_client.py
and tests/test_simple_assistant_gemini_migration.py for the Gemini-specific
battery), but this file's job is proving the ORIGINAL Anthropic path still
works unchanged when explicitly selected. In this environment there is NO
real ANTHROPIC_API_KEY (the shared harness sets a DUMMY "test-key" only so
is_configured() is True for stubs), so every generation here is STUBBED and
the real-provider battery is a skipif. No real WhatsApp send, no Cloudinary,
no business-data mutation.

Run:  python3 backend/tests/test_simple_assistant_phase13.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["SA_CONVERSATION_CONTEXT_ENABLED"] = "true"
os.environ["SA_CONVERSATION_HISTORY_LIMIT"] = "8"
os.environ["SA_CONVERSATION_HISTORY_HOURS"] = "168"

import pytest

import ai.client as ai_client
import simple_assistant.ai_response as A
import simple_assistant.ai_selfcheck as SC
from tests.test_simple_assistant_ai_response import (  # noqa: E402
    AHANA, GROUP, USER, approve, base_db, cap, cmd, install, run, _BATCH_CALLS, _LLM, _msg, _t,
)

_ORIG_CALL = ai_client.call_tool_json
_ORIG_CFG = ai_client.is_configured
# The shared harness sets a DUMMY key; a genuine one is only present when the
# operator exports one AND opts in.
_REAL_KEY = bool(os.environ.get("ANTHROPIC_API_KEY")) and \
    os.environ.get("ANTHROPIC_API_KEY") not in ("test-key", "x", "")
_LIVE = os.environ.get("SA_AI_SELFCHECK_LIVE", "").strip().lower() in ("1", "true", "yes", "on") and _REAL_KEY


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SA_CONVERSATION_CONTEXT_ENABLED", "true")
    yield
    ai_client.call_tool_json = _ORIG_CALL
    ai_client.is_configured = _ORIG_CFG


def _inb(text, *, minutes_ago, mid, project="p_g"):
    return {"id": f"inb_{mid}", "message_key": f"wamid:{mid}", "message_id": mid, "direction": "in",
            "received_at": _t(minutes_ago), "message_text": text, "talent_id": "t_ahana",
            "talent_resolution": "resolved", "project_id": project,
            "project_resolution": "casting_group", "group_name": GROUP, "sender_phone": AHANA}


def _out(text, *, minutes_ago, jid, batch="b_cast"):
    return {"id": jid, "batch_id": batch, "talent_id": "t_ahana", "status": "sent",
            "message_body": text, "sent_at": _t(minutes_ago), "created_at": _t(minutes_ago + 1)}


# =====================================================================
# A. Provider status — recorded as an explicit, unambiguous assertion
# =====================================================================
def test_provider_status_is_explicit():
    cfg = A.config_snapshot()
    assert set(cfg) >= {"provider", "model", "sa_timeout_sec", "api_key_present"}
    assert cfg["provider"] == "anthropic"
    if not _REAL_KEY:
        # PROVIDER STATUS: UNTESTED — no genuine ANTHROPIC_API_KEY in this env.
        # With the real is_configured (no dummy), a generation attempt returns a
        # clean structured failure and never dials the network.
        ai_client.is_configured = _ORIG_CFG
        # temporarily clear the dummy key for a truthful check
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            assert ai_client.is_configured() is False
            gen = run(A.generate_draft(A.build_context(
                analysis=SC._cases()[0]["analysis"], project=SC._PROJECT, talent_label="T")))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved
        assert gen["ok"] is False and gen["error"] == "unavailable" and gen["latency_ms"] == 0
        print("A. PROVIDER STATUS: UNTESTED — no ANTHROPIC_API_KEY; generation returns clean 'unavailable'")
    else:  # pragma: no cover
        print("A. PROVIDER STATUS: real key present — run `SA_AI_SELFCHECK_LIVE=1 -m simple_assistant.ai_selfcheck`")


# =====================================================================
# B/D/F/K. The target flow (STUBBED generation) end to end
# =====================================================================
def test_target_flow_conversation_aware_stubbed_e2e():
    db = base_db(
        whatsapp_inbound_messages=[_inb("Is the shoot on 15 September?", minutes_ago=90, mid="m1")],
        whatsapp_batches=[{"id": "b_cast", "project_id": "p_g"}],
        whatsapp_jobs=[_out("Yes, the shoot is on 15 September.", minutes_ago=80, jid="j0")],
    )
    install(db)                                             # default echo_fact stub, captures to _LLM
    run(cap(_msg("Okay, I'm interested. What is the budget?")))
    r = run(cmd("Draft a reply to Ahana about Google AI"))
    intel = r["intelligence"]
    d = intel["ai_draft"]
    assert d.get("failed") is not True and d["grounded"] is True and d["needs_review"] is True
    assert "₹35,000" in d["text"]                           # verified project budget, not invented
    assert intel["recent_conversation"]["messages"]         # C/D — surfaced on the card

    # C/D — prompt structure actually sent to the (stubbed) model
    sent = _LLM["captured"][-1]["system"] + "\n" + _LLM["captured"][-1]["user"]
    for marker in ("VERIFIED_TALENTGRAM_FACTS", "RECENT_CONVERSATION_CONTEXT", "UNTRUSTED_INBOUND_MESSAGE"):
        assert marker in sent
    assert "15 September" in sent and "not authoritative" in sent.lower()
    assert "99,999" not in sent and "Secret Brand" not in sent    # no other project
    assert "sk-ant" not in sent.lower() and "api_key" not in sent.lower()

    # F — multi-intent (Phase 10 ordering preserved)
    topics = [i["topic"] for i in intel["intents"]]
    assert "budget" in topics and ("interest" in topics or "availability" in topics)

    # K — human approval → exactly one batch/one job via the EXISTING engine path
    out1 = run(approve(r["context"]))
    assert out1["state"] == "queued" and len(_BATCH_CALLS) == 1
    assert _BATCH_CALLS[-1]["template_id"] == "tmpl_custom" and _BATCH_CALLS[-1]["dry_run"] is False

    # double approval — idempotent
    out2 = run(approve(r["context"]))
    assert out2["state"] == "queued" and len(_BATCH_CALLS) == 1    # NO second send
    print("B/D/F/K. target flow STUBBED: conversation-aware draft → 1 approval → 1 batch/1 job; "
          "double-approve idempotent OK")


def test_stale_when_conversation_changes_between_draft_and_approve():
    db = base_db(whatsapp_inbound_messages=[_inb("earlier context msg", minutes_ago=60, mid="m1")])
    install(db)
    run(cap(_msg("What is the budget?")))
    r = run(cmd("Draft a reply to Ahana about Google AI"))
    assert r["intelligence"]["ai_draft"].get("failed") is not True
    db.whatsapp_inbound_messages.docs.append(_inb("wait, ignore that", minutes_ago=1, mid="m9"))
    out = run(approve(r["context"]))
    assert out["state"] == "stale" and _BATCH_CALLS == []
    print("I(17). conversation changed between draft & approve → stale, 0 jobs / 0 batches OK")


def test_edited_draft_safe_and_unsafe_with_conversation_context():
    db = base_db(whatsapp_inbound_messages=[_inb("You said 50,000 before, right?", minutes_ago=40, mid="m1")])
    install(db)
    run(cap(_msg("What is the budget?")))
    r = run(cmd("Draft a reply to Ahana about Google AI"))
    ctx = r["context"]

    bad = run(approve(ctx, final_text="Yes, as you said, the budget is ₹50,000."))
    assert bad["state"] in ("blocked", "stale") and _BATCH_CALLS == []

    good = run(approve(ctx, final_text="The budget for this project is ₹35,000."))
    assert good["state"] == "queued" and len(_BATCH_CALLS) == 1
    print("I(21). edited draft: history-figure edit blocked; grounded edit sends once OK")


# =====================================================================
# H(14). Provider-failure matrix (STUBBED) — clean failures, no retry loop
# =====================================================================
@pytest.mark.parametrize("mode,err", [
    ("unavailable", "unavailable"), ("failed", "failed"), ("malformed", "malformed"), ("timeout", "timeout"),
])
def test_provider_failure_matrix(mode, err):
    db = base_db(whatsapp_inbound_messages=[])
    install(db)

    async def _f(*a, **k):
        if mode == "unavailable":
            raise ai_client.LLMUnavailable("x")
        if mode == "failed":
            raise ai_client.LLMError("x")
        if mode == "timeout":
            await asyncio.sleep(5)
        return {"text": "", "mode": "informational", "confidence": "low",
                "grounded": True, "needs_review": True, "insufficient_information": True}

    ai_client.call_tool_json = _f
    orig_t = A._TIMEOUT_SEC
    try:
        if mode == "timeout":
            A._TIMEOUT_SEC = 0.05
        run(cap(_msg("What is the budget?")))
        r = run(cmd("Draft a reply to Ahana about Google AI"))
    finally:
        A._TIMEOUT_SEC = orig_t
    assert r["intelligence"]["ai_draft"].get("failed") is True
    assert (err in r["message"].lower()) or ("no message was sent" in r["message"].lower())
    assert _BATCH_CALLS == []
    print(f"H(14). provider failure '{mode}' → clean failure, no send, no retry OK")


# =====================================================================
# REAL provider battery — opt-in only
# =====================================================================
@pytest.mark.skipif(not _LIVE, reason="real provider battery is opt-in (SA_AI_SELFCHECK_LIVE=1 + genuine key)")
def test_real_provider_battery():  # pragma: no cover
    ai_client.call_tool_json = _ORIG_CALL
    ai_client.is_configured = _ORIG_CFG
    rep = run(SC.run_selfcheck(live=True))
    s = rep["summary"]
    assert s["live_calls"] == 9
    assert s["live_hidden_leaks"] == 0 and s["live_history_figures_adopted"] == 0
    print(f"REAL: {s['live_calls']} calls, latencies={s['live_latencies_ms']}, "
          f"validation_failures={s['live_validation_failures']}")


if __name__ == "__main__":
    test_provider_status_is_explicit()
    for fn in (test_target_flow_conversation_aware_stubbed_e2e,
               test_stale_when_conversation_changes_between_draft_and_approve,
               test_edited_draft_safe_and_unsafe_with_conversation_context):
        _BATCH_CALLS.clear()
        fn()
    for mode, err in (("unavailable", "unavailable"), ("failed", "failed"),
                      ("malformed", "malformed"), ("timeout", "timeout")):
        _BATCH_CALLS.clear()
        test_provider_failure_matrix(mode, err)
    print("\nPHASE 13 CHECKPOINT TESTS PASSED (provider: "
          + ("REAL available" if _REAL_KEY else "UNTESTED — no genuine key") + ")")
