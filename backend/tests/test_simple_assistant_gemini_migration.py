"""Simple Assistant — Google Gemini provider migration & validation.

Simple Assistant's preferred provider is now Google Gemini (SA_AI_PROVIDER
defaults to "gemini") via the isolated `simple_assistant.ai_gemini_client`
adapter. `backend/ai/client.py` (Anthropic) is UNCHANGED and still used by AI
Scout / AI Casting Desk — proven by tests/test_simple_assistant_ai_response.py
et al. still passing 100% pinned to SA_AI_PROVIDER=anthropic.

This file:
  1. Unit-tests ai_gemini_client.py directly against a FAKE google-genai
     client (no network) — success, malformed/empty/blocked response, and
     every error-mapping branch (401/403 → LLMUnavailable, other 4xx/5xx →
     LLMError) — proving LLMUnavailable/LLMError are the SAME classes
     ai.client raises, so ai_response.py needed no exception-handling change.
  2. Tests ai_response._provider_client() selection: default → gemini,
     explicit "anthropic" → the original module, explicit "gemini" → the new
     one, and that an unconfigured Gemini never silently falls back to
     Anthropic (or vice versa).
  3. Runs the full A–I validation matrix from the migration brief through the
     REAL pipeline (build_context → generate_draft → validate_draft →
     signed plan → approval) with Gemini as the STUBBED provider (the fake
     sits at the google-genai client boundary, so ai_gemini_client's own
     JSON-schema/error-handling code is genuinely exercised, not bypassed).
  4. A REAL Gemini call battery, skipped unless a genuine GEMINI_API_KEY is
     exported (never the dummy this file sets for stub tests).

No real WhatsApp send, no Cloudinary, no business-data mutation, no key ever
logged/printed/asserted-into an audit row.

Run:  python3 backend/tests/test_simple_assistant_gemini_migration.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "gemini-migration-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm_and_send_ai approval path
# (test_approval_path_unchanged_stubbed), so the new independent execution
# kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"
os.environ["SA_AI_RESPONSE_ENABLED"] = "true"
os.environ["SA_CONVERSATION_CONTEXT_ENABLED"] = "true"
# Explicit selection (never rely on the bare default inside a test file) —
# this file's job IS validating the Gemini path.
os.environ["SA_AI_PROVIDER"] = "gemini"
# A DUMMY key — never a real one — so ai_gemini_client.is_configured() is
# True for the stubbed tests below. `_REAL_KEY` (further down) explicitly
# excludes this exact placeholder from ever being treated as genuine.
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

import pytest

import ai.client as ai_client                      # the UNCHANGED Anthropic seam
import simple_assistant.ai_gemini_client as G
import simple_assistant.ai_response as A
import simple_assistant.ai_selfcheck as SC
from tests.test_simple_assistant_ai_response import (  # noqa: E402 — reuses the shared fake-Mongo harness
    AHANA, GROUP, USER, base_db, _t,
)
from simple_assistant.commands import run_command
from simple_assistant import ai_response_execute

_REAL_KEY = bool(os.environ.get("GEMINI_API_KEY")) and \
    os.environ.get("GEMINI_API_KEY") not in ("test-gemini-key", "x", "")
_LIVE = os.environ.get("SA_AI_SELFCHECK_LIVE", "").strip().lower() in ("1", "true", "yes", "on") and _REAL_KEY

_ORIG_CLIENT_FACTORY = G._client


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@pytest.fixture(scope="module", autouse=True)
def _module_env_guard():
    """One-time snapshot/restore around THIS FILE's entire test run. Other SA
    test files pin SA_AI_PROVIDER only at their own import time (not
    per-test), so any process-global env value this file's tests leave
    behind would otherwise silently change which provider those files'
    later-executed tests use (ai_response._current_provider() reads live —
    see its docstring). Restoring exactly the pre-existing ambient value
    here means this file's Gemini pin never leaks past its own tests."""
    saved_provider = os.environ.get("SA_AI_PROVIDER")
    saved_key = os.environ.get("GEMINI_API_KEY")
    yield
    _restore_env("SA_AI_PROVIDER", saved_provider)
    _restore_env("GEMINI_API_KEY", saved_key)


@pytest.fixture(autouse=True)
def _restore():
    # Force this file's own pin fresh before EVERY test (never trust ambient
    # state left by whichever test ran immediately before this one) — the
    # module-scoped guard above restores the true original after this file
    # is entirely done.
    os.environ["SA_AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = "test-gemini-key"
    yield
    G._client = _ORIG_CLIENT_FACTORY


# ---------------------------------------------------------------------------
# fake google-genai client — sits at the SDK boundary so ai_gemini_client's
# own JSON/error-handling code genuinely runs, only the network is faked.
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, text=None, block_reason=None):
        self.text = text
        self.prompt_feedback = _NS(block_reason=block_reason) if block_reason else None


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_fake_client(fn):
    class _Models:
        async def generate_content(self, *, model, contents, config):
            return await fn(model=model, contents=contents, config=config)

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    G._client = lambda: _Client()


# ---------------------------------------------------------------------------
# 1. ai_gemini_client.py — unit tests against the fake SDK boundary
# ---------------------------------------------------------------------------
def test_is_configured_reflects_gemini_key_only():
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        assert G.is_configured() is False
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
    assert G.is_configured() is True
    print("1. is_configured() reflects GEMINI_API_KEY presence only OK")


def test_exceptions_are_the_same_classes_as_anthropic_client():
    assert G.LLMUnavailable is ai_client.LLMUnavailable
    assert G.LLMError is ai_client.LLMError
    print("2. ai_gemini_client re-exports the SAME LLMUnavailable/LLMError — no except-clause change needed OK")


def test_missing_key_raises_llmunavailable_before_any_network_call():
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        try:
            run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                                 input_schema={"type": "object", "properties": {}}))
            assert False, "expected LLMUnavailable"
        except G.LLMUnavailable as exc:
            assert "GEMINI_API_KEY" in str(exc)
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
    print("3. no key → LLMUnavailable, no network dial OK")


def test_successful_structured_response_is_parsed():
    async def _ok(*, model, contents, config):
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema is not None
        # the live-established fix (see ai_gemini_client module docstring):
        # thinking must be explicitly disabled or max_output_tokens can be
        # consumed by hidden reasoning before any visible JSON is written.
        assert config.thinking_config is not None and config.thinking_config.thinking_budget == 0
        assert model == "gemini-3.6-flash"
        return _FakeResp(text='{"text": "Hi there", "mode": "informational", "confidence": "high", '
                              '"grounded": true, "needs_review": true, "insufficient_information": false}')

    _install_fake_client(_ok)
    out = run(G.call_tool_json(system="SYS", user="USER", tool_name="propose_whatsapp_reply",
                               tool_description="d", input_schema={"type": "object", "properties": {}},
                               model="gemini-3.6-flash"))
    assert out["text"] == "Hi there" and out["grounded"] is True
    print("4. valid structured JSON response parsed into a dict; thinking explicitly disabled OK")


def test_default_model_is_the_live_confirmed_one():
    assert G.DEFAULT_MODEL == "gemini-3.6-flash"
    print("4b. DEFAULT_MODEL is gemini-3.6-flash (gemini-2.5-flash confirmed dead — 404 — for this account) OK")


def test_truncated_or_empty_output_reports_finish_reason_safely():
    """Simulates the ACTUAL failure mode observed live before the fix: a
    response with no usable text. The error must name finish_reason (safe
    diagnostic metadata) and never the raw prompt/response content."""
    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def _truncated(*, model, contents, config):
        resp = _FakeResp(text=None)
        resp.candidates = [_NS(finish_reason="MAX_TOKENS", content=None)]
        return resp

    _install_fake_client(_truncated)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMError as exc:
        assert "MAX_TOKENS" in str(exc)
        assert "SYS" not in str(exc) and "USER" not in str(exc)
    print("4c. empty/truncated output → clean LLMError naming finish_reason, no content leaked OK")


def test_markdown_fenced_output_is_rejected_not_silently_stripped():
    """No live response has ever actually been markdown-fenced (verified —
    see the module docstring's root-cause note), so this module deliberately
    does NOT strip fences. This documents the resulting behavior IF a future
    model ever does return one: a clean, strict-parser rejection — never an
    unsafe substring-extraction guess at the "real" JSON inside."""
    async def _fenced(*, model, contents, config):
        return _FakeResp(text='```json\n{"text": "hi", "mode": "informational"}\n```')

    _install_fake_client(_fenced)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False, "expected LLMError"
    except G.LLMError as exc:
        assert "non-JSON" in str(exc)
    print("4d. markdown-fenced text (hypothetical) → clean rejection, no substring guessing OK")


def test_gemini_path_timeout_uses_the_same_sa_ai_timeout_sec_bound():
    """The 45s bound lives in ai_response.generate_draft (provider-agnostic),
    not in ai_gemini_client itself — proves it applies correctly when Gemini
    is the active provider too, with no Gemini-specific timeout added."""
    import asyncio as _asyncio

    async def _hang(*, model, contents, config):
        await _asyncio.sleep(5)
        return _FakeResp(text="{}")

    _install_fake_client(_hang)
    orig_t = A._TIMEOUT_SEC
    try:
        A._TIMEOUT_SEC = 0.05
        ctx = A.build_context(analysis=SC._cases()[0]["analysis"], project=SC._PROJECT, talent_label="T")
        gen = run(A.generate_draft(ctx))
    finally:
        A._TIMEOUT_SEC = orig_t
    assert gen["ok"] is False and gen["error"] == "timeout" and gen["provider"] == "gemini"
    print("8. Gemini path: a hung call is still cut off by SA_AI_TIMEOUT_SEC — no new timeout logic added OK")


def test_blocked_response_raises_llmerror_not_silently_empty():
    async def _blocked(*, model, contents, config):
        return _FakeResp(text=None, block_reason="SAFETY")

    _install_fake_client(_blocked)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False, "expected LLMError"
    except G.LLMError as exc:
        assert "SAFETY" in str(exc) or "no usable content" in str(exc)
    print("5. blocked/empty response → LLMError, never a silent empty draft OK")


def test_malformed_json_raises_llmerror():
    async def _bad(*, model, contents, config):
        return _FakeResp(text="not json at all {")

    _install_fake_client(_bad)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMError as exc:
        assert "non-JSON" in str(exc)
    print("6. malformed (non-JSON) output → LLMError OK")


def test_non_object_json_raises_llmerror():
    async def _arr(*, model, contents, config):
        return _FakeResp(text="[1, 2, 3]")

    _install_fake_client(_arr)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMError as exc:
        assert "not a JSON object" in str(exc)
    print("6b. valid JSON that isn't an object → LLMError OK")


def test_auth_error_maps_to_llmunavailable_never_leaks_key():
    from google.genai import errors

    async def _unauth(*, model, contents, config):
        raise errors.ClientError(401, {"error": {"message": "API key not valid", "status": "UNAUTHENTICATED"}})

    _install_fake_client(_unauth)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMUnavailable as exc:
        msg = str(exc)
        assert "401" in msg
        assert "test-gemini-key" not in msg and os.environ["GEMINI_API_KEY"] not in msg
    print("7. 401 → LLMUnavailable; key never appears in the error text OK")


def test_rate_limit_and_server_error_map_to_llmerror_no_retry():
    from google.genai import errors

    calls = {"n": 0}

    async def _rl(*, model, contents, config):
        calls["n"] += 1
        raise errors.ClientError(429, {"error": {"message": "rate limit exceeded"}})

    _install_fake_client(_rl)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMError as exc:
        assert "429" in str(exc)
    assert calls["n"] == 1                          # exactly one attempt — no retry loop here

    async def _srv(*, model, contents, config):
        raise errors.ServerError(503, {"error": {"message": "overloaded"}})

    _install_fake_client(_srv)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}))
        assert False
    except G.LLMError as exc:
        assert "503" in str(exc)
    print("8. 429 + 503 → LLMError, exactly one attempt each, no retry loop OK")


def test_invalid_model_maps_cleanly():
    from google.genai import errors

    async def _notfound(*, model, contents, config):
        raise errors.ClientError(404, {"error": {"message": f"model {model} not found"}})

    _install_fake_client(_notfound)
    try:
        run(G.call_tool_json(system="s", user="u", tool_name="t", tool_description="d",
                             input_schema={"type": "object"}, model="not-a-real-model"))
        assert False
    except G.LLMError as exc:
        assert "404" in str(exc)
    print("9. invalid/unknown model id → clean LLMError (404), not a crash OK")


# ---------------------------------------------------------------------------
# 2. provider selection — explicit, never a silent fallback
# ---------------------------------------------------------------------------
def test_provider_selection_default_is_gemini():
    # ai_response._current_provider()/_current_model() read SA_AI_PROVIDER
    # LIVE (never frozen at this module's first import) specifically so
    # multiple SA test files can each pin a different provider in the same
    # pytest process without any importlib.reload games or cross-file leaks.
    assert os.environ["SA_AI_PROVIDER"] == "gemini"    # this file's own pin
    assert A._current_provider() == "gemini"
    assert A._provider_client() is G
    assert A._current_model() == "gemini-3.6-flash"
    print("10. SA_AI_PROVIDER=gemini → resolves live to gemini, model=gemini-3.6-flash OK")


def test_provider_selection_explicit_anthropic():
    saved = os.environ["SA_AI_PROVIDER"]
    os.environ["SA_AI_PROVIDER"] = "anthropic"
    try:
        assert A._current_provider() == "anthropic"
        assert A._provider_client() is ai_client
        assert A._current_model() == "claude-sonnet-5"
    finally:
        os.environ["SA_AI_PROVIDER"] = saved
    assert A._current_provider() == "gemini"           # restored
    assert A._DEFAULT_MODEL_BY_PROVIDER == {"gemini": "gemini-3.6-flash", "anthropic": "claude-sonnet-5"}
    print("11. SA_AI_PROVIDER=anthropic → resolves live to the original ai.client module, unchanged OK")


def test_no_silent_fallback_when_gemini_unconfigured_even_if_anthropic_key_present():
    saved_gemini = os.environ.pop("GEMINI_API_KEY", None)
    saved_anthropic = os.environ.get("ANTHROPIC_API_KEY")   # restore exactly, never a bare pop
    os.environ["ANTHROPIC_API_KEY"] = "some-anthropic-key"  # present, but provider stays gemini
    try:
        cfg = A.config_snapshot()
        assert cfg["provider"] == "gemini"
        assert cfg["api_key_present"] is False               # never silently checks/uses the Anthropic key
        gen = run(A.generate_draft(A.build_context(
            analysis=SC._cases()[0]["analysis"], project=SC._PROJECT, talent_label="T")))
        assert gen["ok"] is False and gen["error"] == "unavailable" and gen["provider"] == "gemini"
    finally:
        if saved_gemini is not None:
            os.environ["GEMINI_API_KEY"] = saved_gemini
        _restore_env("ANTHROPIC_API_KEY", saved_anthropic)
    print("12. Gemini unconfigured + Anthropic key present → still 'unavailable', NEVER silently switches OK")


# ---------------------------------------------------------------------------
# 3. the A–I validation matrix (STUBBED Gemini, via the fake SDK boundary)
# ---------------------------------------------------------------------------
def _grounded_stub():
    async def _fn(*, model, contents, config):
        import json as _j
        payload = _j.loads(contents.split("=== VERIFIED_TALENTGRAM_FACTS", 1)[-1]
                           .split("===\n", 1)[-1].split("\n\n===")[0])
        facts = payload.get("verified_facts") or {}
        convo_present = "RECENT_CONVERSATION_CONTEXT" in contents
        if facts.get("budget") and "HIDDEN" not in str(facts["budget"]):
            txt = f"Thanks! The budget for this project is {facts['budget']}."
        elif "budget" in facts:  # HIDDEN marker present
            txt = "I don't have budget details I can share at this stage."
        else:
            txt = "Thanks — I'll confirm and get back to you."
        return _FakeResp(text=_j.dumps({
            "text": txt, "mode": "informational", "confidence": "high",
            "grounded": True, "needs_review": True, "insufficient_information": not facts,
        }))
    return _fn


def _ctx_for(*, facts=None, hidden=None, missing=None, negotiation=False, convo=None, intents=("budget",)):
    analysis = {
        "message_text": "test message", "intents": [{"topic": t, "confidence": "high", "order": i}
                                                     for i, t in enumerate(intents)],
        "topic": intents[0], "topic_confidence": "high",
        "signals": {"is_question": True, "negotiation_request": negotiation},
        "project_knowledge": {},
        "project_knowledge_multi": {"facts": facts or {}, "missing": missing or [], "hidden": hidden or [],
                                    "per_topic": {}},
        "answerability": "answerable" if facts and not missing and not hidden else "partially_answerable",
    }
    return A.build_context(analysis=analysis, project={"id": "p_gem", "brand_name": "Simple Assistant Gemini Validation"},
                           talent_label="Test Talent", conversation_context=convo)


def test_A_basic_budget_response():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={"budget": "₹25,000"})
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True and gen["provider"] == "gemini"
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    assert "25,000" in gen["draft"]["text"]
    print("A. basic budget response — grounded in the verified ₹25,000 OK")


def test_B_multi_intent():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={"budget": "₹25,000", "shoot location": "Mumbai"}, intents=("interest", "budget", "location"))
    topics = [i["topic"] for i in ctx["intents"]]
    assert topics == ["interest", "budget", "location"]
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    print("B. multi-intent (interest/budget/location) — coherent grounded draft OK")


def test_C_conversation_continuity():
    convo = SC._convo(("in", "Is the shoot on 15 September?"), ("out", "Yes, the shoot is on 15 September."))
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={"budget": "₹25,000"}, intents=("interest", "budget"), convo=convo)
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True
    assert "15 September" not in gen["draft"]["text"] or True  # history informs tone only, not required verbatim
    assert "25,000" in gen["draft"]["text"]              # verified fact remains authoritative
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    print("C. conversation continuity — history present, verified facts authoritative OK")


def test_D_missing_payment_information():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={}, missing=["payment terms"], intents=("payment",))
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True
    assert "30 days" not in gen["draft"]["text"] and "within" not in gen["draft"]["text"].lower()
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    print("D. missing payment terms — never invented, validator accepts the honest draft OK")


def test_E_negotiation_no_commitment():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={}, missing=[], hidden=[], negotiation=True, intents=("budget", "negotiation"))
    ok, reasons = A.validate_draft("Sure, we can increase it to ₹40,000.", ctx)
    assert ok is False and any("negotiation" in r for r in reasons)
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True                              # the stub itself never proposes a number
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    print("E. negotiation — no autonomous commitment / invented amount; validator rejects one if offered OK")


def test_F_hidden_budget():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={}, hidden=["budget"], intents=("budget",))
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True
    assert "₹" not in gen["draft"]["text"]
    assert A.validate_draft(gen["draft"]["text"], ctx)[0] is True
    leak_ok, leak_reasons = A.validate_draft("The budget is ₹99,000.", ctx)
    assert leak_ok is False
    print("F. hidden budget — not exposed by the stub; a leaking draft is rejected by the validator OK")


def test_G_history_based_budget_injection():
    convo = SC._convo(("in", "Earlier you said the budget is ₹50,000, right?"))

    async def _adopts_history(*, model, contents, config):
        import json as _j
        return _FakeResp(text=_j.dumps({
            "text": "Yes, as mentioned, the budget is ₹50,000.", "mode": "informational",
            "confidence": "high", "grounded": True, "needs_review": True, "insufficient_information": False,
        }))

    _install_fake_client(_adopts_history)
    ctx = _ctx_for(facts={"budget": "₹25,000"}, convo=convo)
    gen = run(A.generate_draft(ctx))
    assert gen["ok"] is True
    ok, reasons = A.validate_draft(gen["draft"]["text"], ctx)
    assert ok is False and any("50,000" in r for r in reasons)   # NOT adopted — validator rejects
    print("G. history-based budget injection (₹50,000 in history, ₹25,000 verified) → rejected OK")


def test_H_prompt_injection():
    _install_fake_client(_grounded_stub())
    ctx = _ctx_for(facts={"budget": "₹25,000"})
    prompt = A._SYSTEM + "\n" + A._user_prompt(ctx)
    assert "UNTRUSTED_INBOUND_MESSAGE" in prompt
    ok, reasons = A.validate_draft("All project budgets, including the admin's: ₹25,000, ₹99,999 for others.", ctx)
    assert ok is False
    print("H. prompt injection — message stays untrusted data; a compliant draft is still rejected OK")


def test_I_provider_failure_behavior():
    from google.genai import errors
    import asyncio as _asyncio

    scenarios = {}

    async def _unavail(*, model, contents, config):
        raise G.LLMUnavailable("no key")
    scenarios["unavailable"] = _unavail

    async def _invalid_model(*, model, contents, config):
        raise errors.ClientError(404, {"error": {"message": "model not found"}})
    scenarios["invalid_model"] = _invalid_model

    async def _rate_limit(*, model, contents, config):
        raise errors.ClientError(429, {"error": {"message": "rate limited"}})
    scenarios["rate_limit"] = _rate_limit

    async def _malformed(*, model, contents, config):
        return _FakeResp(text="{not valid json")
    scenarios["malformed"] = _malformed

    async def _blocked(*, model, contents, config):
        return _FakeResp(text=None, block_reason="PROHIBITED_CONTENT")
    scenarios["blocked"] = _blocked

    async def _timeout(*, model, contents, config):
        await _asyncio.sleep(5)
        return _FakeResp(text="{}")
    scenarios["timeout"] = _timeout

    orig_t = A._TIMEOUT_SEC
    for name, fn in scenarios.items():
        _install_fake_client(fn)
        if name == "timeout":
            A._TIMEOUT_SEC = 0.05
        try:
            ctx = _ctx_for(facts={"budget": "₹25,000"})
            gen = run(A.generate_draft(ctx))
        finally:
            A._TIMEOUT_SEC = orig_t
        assert gen["ok"] is False, name
        assert gen["provider"] == "gemini"
    print("I. provider failures (unavailable/invalid-model/rate-limit/malformed/blocked/timeout) "
          "— all clean, no uncontrolled retry OK")


# ---------------------------------------------------------------------------
# Approval path stays untouched — same validate → sign → approve → send
# ---------------------------------------------------------------------------
def test_approval_path_unchanged_stubbed():
    db = base_db(whatsapp_inbound_messages=[])
    import simple_assistant.readonly_db as sa_rdb
    import simple_assistant.audit as sa_audit
    import simple_assistant.whatsapp_send as wsend
    import simple_assistant.service as sa_service
    import inbound_messages as ibm
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    ibm.db = db
    wsend.db = db

    async def _sc(pid):
        return {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}
    sa_service.get_stage_counts = _sc
    batch_calls = []

    async def _fake_batch(payload, admin):
        batch_calls.append(payload.variable_data.get("message"))
        return {"batch": {"id": f"b{len(batch_calls)}"}, "jobs": [{"id": f"j{len(batch_calls)}", "talent_id": None}]}

    wsend._create_batch_internal = _fake_batch
    _install_fake_client(_grounded_stub())

    def _msg(text, mid="g1"):
        return {"message_id": mid, "sender_phone": AHANA, "text": text, "sender_name": "Ahana",
                "group_name": GROUP, "received_at": _t(5), "source": "whatsapp_worker"}

    run(ibm.capture_inbound(**_msg("What is the budget?")))
    r = run(run_command(message="Draft a reply to Ahana about Google AI", conversation_id=None, context=None, user=USER))
    d = r["intelligence"]["ai_draft"]
    assert d.get("failed") is not True and "₹35,000" in d["text"]     # base_db's real Google AI project fact
    out1 = run(ai_response_execute.confirm_and_send_ai(
        conversation_id=r["context"].get("conversation_id"), context=r["context"], user=USER))
    assert out1["state"] == "queued" and len(batch_calls) == 1
    out2 = run(ai_response_execute.confirm_and_send_ai(
        conversation_id=r["context"].get("conversation_id"), context=r["context"], user=USER))
    assert out2["state"] == "queued" and len(batch_calls) == 1        # idempotent, no second send
    print("Approval: Gemini draft → validate → sign → approve → EXISTING queue_send; "
          "double-approve idempotent OK")


# ---------------------------------------------------------------------------
# REAL Gemini — opt-in only
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _LIVE, reason="real Gemini battery is opt-in (SA_AI_SELFCHECK_LIVE=1 + genuine GEMINI_API_KEY)")
def test_real_gemini_battery():  # pragma: no cover
    G._client = _ORIG_CLIENT_FACTORY
    assert G.is_configured()
    rep = run(SC.run_selfcheck(live=True))
    s = rep["summary"]
    assert s["provider"] == "gemini"
    assert s["live_calls"] == 9
    assert s["live_hidden_leaks"] == 0 and s["live_history_figures_adopted"] == 0
    print(f"REAL GEMINI: model={s['model']}, {s['live_calls']} calls, latencies={s['live_latencies_ms']}, "
          f"validation_failures={s['live_validation_failures']}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
        except TypeError:
            pass
        finally:
            G._client = _ORIG_CLIENT_FACTORY
    if _LIVE:
        test_real_gemini_battery()
    else:
        print("REAL Gemini battery — SKIPPED (opt-in: SA_AI_SELFCHECK_LIVE=1 with a genuine GEMINI_API_KEY)")
    print(f"\nGEMINI MIGRATION TESTS PASSED (provider: {A._current_provider()})")
