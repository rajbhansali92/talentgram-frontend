"""Simple Assistant — Phase 11/13 controlled AI validation / hardening.

Exercises the self-check harness (simple_assistant.ai_selfcheck) with NO
provider call, proving for every canonical case (A–F Phase 11, G–I the
Phase 13 conversation-context cases):
  * the model would receive only the server-built verified context
  * the hidden budget value never enters the prompt
  * no unrelated-project data / injection payload enters the prompt
  * RECENT_CONVERSATION_CONTEXT is present and explicitly non-authoritative;
    a figure that lives only in history is still rejected by the validator
  * the EXISTING validate_draft() accepts a grounded draft and rejects an
    invented / negotiation / hidden-leak / cross-project / history-adopted one
Plus the SA-specific generation timeout, the observability audit fields,
and a real-provider test that SKIPS when no key is configured.

Run:  python3 backend/tests/test_simple_assistant_ai_selfcheck.py
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase11-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
os.environ["SA_AI_RESPONSE_ENABLED"] = "true"
# This suite monkeypatches ai.client directly, so it pins the provider to
# Anthropic explicitly (Gemini is now the SA_AI_PROVIDER default post-
# migration — see simple_assistant/ai_gemini_client.py).
os.environ["SA_AI_PROVIDER"] = "anthropic"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import ai.client as ai_client
import simple_assistant.ai_response as A
import simple_assistant.ai_selfcheck as SC


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


_ORIG = ai_client.call_tool_json
_ORIG_CFG = ai_client.is_configured


@pytest.fixture(autouse=True)
def _restore():
    yield
    ai_client.call_tool_json = _ORIG
    ai_client.is_configured = _ORIG_CFG


def _force_configured():
    ai_client.is_configured = lambda: True


# ---- config snapshot -------------------------------------------
def test_config_snapshot_is_credential_free():
    cfg = A.config_snapshot()
    assert cfg["provider"] == "anthropic"          # pinned by this suite (SA_AI_PROVIDER above)
    assert cfg["model"] == os.environ.get("SA_AI_MODEL") or "claude-sonnet-5"
    assert cfg["key_env_var"] == "ANTHROPIC_API_KEY"
    assert cfg["max_tokens"] == 400
    assert isinstance(cfg["sa_timeout_sec"], float) and cfg["sa_timeout_sec"] > 0
    assert set(cfg) == {"provider", "model", "max_tokens", "sa_timeout_sec", "client_timeout_sec",
                        "api_key_present", "key_env_var", "workspace_id_present", "flag_enabled"}
    # no key / header / secret value anywhere
    blob = str(cfg).lower()
    for bad in ("sk-ant", "authorization", "bearer", "x-api-key"):
        assert bad not in blob
    print("1. config snapshot — model/timeouts exposed, zero credentials OK")


# ---- offline self-check (all 9 cases) --------------------------
def test_selfcheck_all_cases_isolated_and_validated():
    rep = run(SC.run_selfcheck(live=False))
    s = rep["summary"]
    assert s["cases"] == 9
    assert s["conversation_cases"] == 3
    assert s["prompt_isolation_ok"] is True
    assert s["validator_ok"] is True
    assert s["live_calls"] == 0
    by = {c["case"]: c for c in rep["cases"]}
    # A/B — verified facts only
    assert by["A"]["verified_facts"] == {"budget": "₹35,000"}
    assert by["B"]["verified_facts"] == {"budget": "₹35,000", "shoot date": "15 September 2026"}
    # C — nothing verified, honest "check"
    assert by["C"]["verified_facts"] == {} and by["C"]["canned_bad_rejected"] is True
    # D — negotiation: no amount in verified facts, counteroffer rejected
    assert "budget" not in by["D"]["verified_facts"]
    # E — hidden: forbidden, leak rejected
    assert by["E"]["forbidden_facts"] == ["budget"]
    # F — injection: cross-project dump rejected
    assert by["F"]["canned_bad_rejected"] is True
    # G/H/I — Phase 13 conversation-context cases
    assert by["G"]["has_conversation"] and by["G"]["verified_facts"] == {"budget": "₹25,000"}
    assert by["G"]["canned_bad_rejected"] is True          # invented ₹40,000 rejected
    assert by["H"]["verified_facts"] == {"budget": "₹25,000"}
    assert by["H"]["canned_bad_rejected"] is True          # ₹50,000 from history NOT adopted
    assert by["I"]["forbidden_facts"] == ["budget"] and by["I"]["canned_bad_rejected"] is True
    print("2. self-check A–I — prompt isolation + validator verdicts all correct OK")


def test_hidden_value_and_cross_project_never_in_any_prompt():
    for case in SC._cases():
        ctx = A.build_context(analysis=case["analysis"], project=SC._PROJECT,
                              talent_label="Test Talent",
                              conversation_context=case.get("convo"))
        prompt = SC._prompt_of(ctx)
        assert "99,000" not in prompt                      # the hidden value, never
        assert "Other Project" not in prompt               # no other project
        assert "UNTRUSTED_INBOUND_MESSAGE" in prompt        # message always isolated
        assert "VERIFIED_TALENTGRAM_FACTS" in prompt
        if case.get("convo"):
            assert "RECENT_CONVERSATION_CONTEXT" in prompt
            assert "not authoritative" in prompt.lower()
    print("3. across every case: hidden value + other-project data absent; history non-authoritative OK")


def test_phase13_conversation_cases_history_not_authoritative():
    """G/H/I — the model gets the conversation but the deterministic validator
    still rejects a draft that adopts a figure from history / a hidden budget."""
    cases = {c["id"]: c for c in SC._cases()}
    for cid in ("G", "H", "I"):
        case = cases[cid]
        ctx = A.build_context(analysis=case["analysis"], project=SC._PROJECT,
                              talent_label="Test Talent", conversation_context=case["convo"])
        # the current message is still the only thing in UNTRUSTED_INBOUND_MESSAGE
        prompt = SC._prompt_of(ctx)
        assert case["analysis"]["message_text"] in prompt
        # H: ₹50,000 is in the prompt (as history) but a draft using it is rejected
        if cid == "H":
            assert "₹50,000" in prompt
            assert A.validate_draft("The budget is ₹50,000.", ctx)[0] is False
            assert A.validate_draft("The budget for this project is ₹25,000.", ctx)[0] is True
        # I: hidden — no amount at all may appear in the reply
        if cid == "I":
            assert "99,000" not in prompt and "[amount]" in prompt
            assert A.validate_draft(f"The budget is {SC._HIDDEN_VALUE}.", ctx)[0] is False
    print("3b. G/H/I — conversation is context; validator stays authoritative OK")


# ---- validation-only case selection (SA_AI_SELFCHECK_CASES) ---
# Strictly a self-check-only filter added to close the G/H/I live-testing
# gap left by full-battery quota exhaustion. Does not touch ai_response.py /
# ai_gemini_client.py / ai/client.py / any production route or flag.
def test_select_cases_default_is_unchanged_all_nine_in_order():
    all_cases = SC._cases()
    selected = SC._select_cases(None)
    assert selected == all_cases
    assert [c["id"] for c in selected] == list("ABCDEFGHI")
    print("8. _select_cases(None) — identical to _cases(), all 9, original order OK")


def test_select_cases_subset_preserves_canonical_order():
    # requested out of order — result must still come back A..I order
    selected = SC._select_cases(["I", "G", "H"])
    assert [c["id"] for c in selected] == ["G", "H", "I"]
    print("9. _select_cases(subset) — canonical A-I order regardless of request order OK")


def test_select_cases_is_case_and_whitespace_tolerant():
    selected = SC._select_cases(["g", " h ", "I"])
    assert [c["id"] for c in selected] == ["G", "H", "I"]
    print("10. _select_cases — case-insensitive + whitespace-tolerant OK")


def test_select_cases_rejects_unknown_id():
    with pytest.raises(ValueError, match="Unknown self-check case id"):
        SC._select_cases(["Z"])
    with pytest.raises(ValueError, match="Unknown self-check case id"):
        SC._select_cases(["G", "Q"])
    print("11. _select_cases — unknown id raises ValueError naming the bad id OK")


def test_select_cases_rejects_empty_requested_set():
    with pytest.raises(ValueError):
        SC._select_cases([""])
    with pytest.raises(ValueError):
        SC._select_cases([])
    print("12. _select_cases — empty/blank id list raises ValueError instead of silently running everything OK")


def test_cases_from_env_parses_or_returns_none():
    orig = os.environ.pop("SA_AI_SELFCHECK_CASES", None)
    try:
        assert SC._cases_from_env() is None
        os.environ["SA_AI_SELFCHECK_CASES"] = ""
        assert SC._cases_from_env() is None
        os.environ["SA_AI_SELFCHECK_CASES"] = "G,H,I"
        assert SC._cases_from_env() == ["G", "H", "I"]
        os.environ["SA_AI_SELFCHECK_CASES"] = " g , h ,i "
        assert SC._cases_from_env() == ["g", "h", "i"]
    finally:
        if orig is None:
            os.environ.pop("SA_AI_SELFCHECK_CASES", None)
        else:
            os.environ["SA_AI_SELFCHECK_CASES"] = orig
    print("13. _cases_from_env — unset/empty -> None, else comma-split list OK")


def test_run_selfcheck_default_behavior_is_byte_identical_shape():
    rep = run(SC.run_selfcheck(live=False))
    assert rep["cases_selected"] == list("ABCDEFGHI")
    assert rep["summary"]["cases"] == 9
    print("14. run_selfcheck() default — cases_selected == all 9, summary unchanged OK")


def test_run_selfcheck_with_case_ids_runs_only_those():
    rep = run(SC.run_selfcheck(live=False, case_ids=["G", "H", "I"]))
    assert rep["cases_selected"] == ["G", "H", "I"]
    assert rep["summary"]["cases"] == 3
    assert [c["case"] for c in rep["cases"]] == ["G", "H", "I"]
    # the underlying per-case assertions (grounding / hidden-leak / history-not-
    # adopted) must still hold exactly as they do in the full run
    by = {c["case"]: c for c in rep["cases"]}
    assert by["G"]["has_conversation"] and by["G"]["canned_bad_rejected"] is True
    assert by["H"]["canned_bad_rejected"] is True
    assert by["I"]["forbidden_facts"] == ["budget"] and by["I"]["canned_bad_rejected"] is True
    print("15. run_selfcheck(case_ids=[G,H,I]) — only those 3 run, same safety checks pass OK")


def test_run_selfcheck_unknown_case_id_raises():
    with pytest.raises(ValueError):
        run(SC.run_selfcheck(live=False, case_ids=["Z"]))
    print("16. run_selfcheck(case_ids=[unknown]) — raises, no partial run OK")


# ---- SA generation timeout (Phase 11 hardening) ---------------
def test_generation_has_a_hard_timeout():
    async def _hang(*a, **k):
        await asyncio.sleep(10)
        return {"text": "too late"}

    _force_configured()
    ai_client.call_tool_json = _hang
    orig = A._TIMEOUT_SEC
    try:
        A._TIMEOUT_SEC = 0.05
        ctx = A.build_context(analysis=SC._cases()[0]["analysis"], project=SC._PROJECT, talent_label="T")
        gen = run(A.generate_draft(ctx))
        assert gen["ok"] is False and gen["error"] == "timeout"
        assert "did not respond within" in gen["reason"]
        assert isinstance(gen["latency_ms"], int)
    finally:
        A._TIMEOUT_SEC = orig
    print("4. a hung provider call → clean 'timeout' failure, no hang, no retry OK")


def test_failure_modes_are_clean():
    _force_configured()
    ctx = A.build_context(analysis=SC._cases()[0]["analysis"], project=SC._PROJECT, talent_label="T")

    async def _unavail(*a, **k):
        raise ai_client.LLMUnavailable("no key")

    async def _err(*a, **k):
        raise ai_client.LLMError("provider 500")

    async def _malformed(*a, **k):
        return {"text": "", "mode": "informational", "confidence": "low",
                "grounded": True, "needs_review": True, "insufficient_information": True}

    for fn, err in ((_unavail, "unavailable"), (_err, "failed"), (_malformed, "malformed")):
        ai_client.call_tool_json = fn
        gen = run(A.generate_draft(ctx))
        assert gen["ok"] is False and gen["error"] == err
        assert "model" in gen and "latency_ms" in gen
    print("5. unavailable / failed / malformed → structured failure with model+latency OK")


# ---- observability audit fields ------------------------------
def test_audit_records_safe_metrics_only():
    import simple_assistant.audit as sa_audit

    captured = []

    class _Coll:
        async def insert_one(s, d):
            captured.append(d)

    class _DB:
        def __getitem__(s, n):
            return _Coll()

    sa_audit.db = _DB()
    run(sa_audit.record_ai(
        action_type="generate_ai_response", executed=False, conversation_id="c", user={"id": "u"},
        plan_id=None, inbound_id="inb", model="claude-sonnet-5", latency_ms=812,
        validation="passed", draft_hash="dh", fact_hash="fh", result="draft_ok",
    ))
    sa = captured[0]["sa_action"]
    assert sa["model"] == "claude-sonnet-5" and sa["latency_ms"] == 812 and sa["validation"] == "passed"
    assert sa["executed"] is False
    blob = str(captured[0]).lower()
    for bad in ("sk-ant", "authorization", "api_key", "prompt", "system_instructions"):
        assert bad not in blob
    print("6. audit row carries model/latency/validation — no prompt, no key OK")


# ---- REAL provider — opt-in ONLY -----------------------------
# Never auto-runs on key presence: a sibling suite sets a fake ANTHROPIC_API_KEY
# for its stubs, and a real outbound call must be a deliberate, approved act.
# Enable with:  SA_AI_SELFCHECK_LIVE=1 pytest tests/test_simple_assistant_ai_selfcheck.py
_LIVE = os.environ.get("SA_AI_SELFCHECK_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


@pytest.mark.skipif(not _LIVE,
                    reason="real provider test is opt-in (set SA_AI_SELFCHECK_LIVE=1 in an approved env with a genuine key)")
def test_real_provider_full_battery():  # pragma: no cover - needs a live key + opt-in
    assert ai_client.is_configured(), "SA_AI_SELFCHECK_LIVE set but no ANTHROPIC_API_KEY"
    ai_client.call_tool_json = _ORIG
    ai_client.is_configured = _ORIG_CFG
    rep = run(SC.run_selfcheck(live=True))
    s = rep["summary"]
    assert s["live_calls"] == 9
    # security invariants that must hold regardless of model wording
    assert s["live_hidden_leaks"] == 0                 # E, I — hidden budget never leaked
    assert s["live_history_figures_adopted"] == 0      # H — ₹50,000 from history never adopted
    by = {c["case"]: c for c in rep["cases"]}
    if by["A"].get("live_ok"):
        assert "35,000" in by["A"]["live_text"] and by["A"]["live_validates"] is True
    if by["G"].get("live_ok") and by["G"].get("live_used_verified_budget"):
        assert by["G"]["live_validates"] is True       # continuity draft is grounded
    print(f"7. REAL provider — {s['live_calls']} calls, 0 hidden leaks, 0 history figures adopted, "
          f"latencies_ms={s['live_latencies_ms']}")


if __name__ == "__main__":
    for fn in [
        test_config_snapshot_is_credential_free, test_selfcheck_all_cases_isolated_and_validated,
        test_hidden_value_and_cross_project_never_in_any_prompt,
        test_phase13_conversation_cases_history_not_authoritative,
        test_select_cases_default_is_unchanged_all_nine_in_order,
        test_select_cases_subset_preserves_canonical_order,
        test_select_cases_is_case_and_whitespace_tolerant,
        test_select_cases_rejects_unknown_id,
        test_select_cases_rejects_empty_requested_set,
        test_cases_from_env_parses_or_returns_none,
        test_run_selfcheck_default_behavior_is_byte_identical_shape,
        test_run_selfcheck_with_case_ids_runs_only_those,
        test_run_selfcheck_unknown_case_id_raises,
        test_generation_has_a_hard_timeout,
        test_failure_modes_are_clean, test_audit_records_safe_metrics_only,
    ]:
        fn()
        ai_client.call_tool_json = _ORIG
        ai_client.is_configured = _ORIG_CFG
    if _LIVE:
        test_real_provider_full_battery()
    else:
        print("7. REAL provider — SKIPPED (opt-in: SA_AI_SELFCHECK_LIVE=1 with a genuine key)")
    print("\nALL SIMPLE ASSISTANT AI-SELFCHECK (PHASE 11/13) TESTS PASSED")
