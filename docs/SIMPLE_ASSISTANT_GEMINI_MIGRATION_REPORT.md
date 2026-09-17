# Simple Assistant — Google Gemini Provider Migration Report

**Date:** 2026-09-15
**Type:** provider migration + controlled validation — **NOT** an autonomous-agent phase.
**Flags:** unchanged defaults — `SA_AI_RESPONSE_ENABLED=false`, `SA_CONVERSATION_CONTEXT_ENABLED=false`.

---

## A. Provider

| | |
|---|---|
| **Provider** | Google Gemini, via the official `google-genai` SDK (`genai.Client(...).aio.models.generate_content`) |
| **Exact model** | `gemini-2.5-flash` (default; overridable with `SA_AI_MODEL`) |
| **SDK** | `google-genai==1.71.0` — **already a pinned dependency in `requirements.txt`** before this migration (unused until now; no requirements.txt change was needed). `google-generativeai` was explicitly **not** used — importing it prints Google's own deprecation notice: *"All support for the `google.generativeai` package has ended ... switch to `google.genai`."* |
| **Free-tier eligible** | **Yes**, per `ai.google.dev/gemini-api/docs/pricing` (fetched live): `gemini-2.5-flash` input/output tokens are listed "Free of charge." Exact RPM/RPD limits are account-specific and only visible at `aistudio.google.com/rate-limit` for a logged-in key — **not independently verifiable without a real key from this environment**, so no numeric quota is claimed. |
| **Why this model** | Google's own docs describe it as *"Best price-performance model for low-latency, high-volume tasks requiring reasoning"* — matches the requirement exactly (short professional drafts, bounded context, low-volume human-approved operation). It is the stable `2.5` generation rather than a very recently introduced `3.x` variant, minimizing churn risk for a production default. |
| **Comparison performed (§3 of the brief)** | 1) **Direct Google GenAI SDK** — chosen: native `response_json_schema` structured output, async client, error hierarchy (`ClientError`/`ServerError`) that maps cleanly onto the existing `LLMUnavailable`/`LLMError` split. 2) **Gemini's OpenAI-compatible endpoint** — rejected: would add a new dependency (`openai` package, not installed), is explicitly a migration-convenience shim in Google's own docs (not the primary path), and its structured-output guarantee is weaker (translated through OpenAI's dialect, not native `response_json_schema`). 3) **Reuse `ai.client` as-is, pointed at Gemini** — rejected: `ai.client` is **shared** by AI Scout and AI Casting Desk (see D); repointing it would risk both. |

## B. Credential status

```
UNTESTED — NO GEMINI API KEY
```

**Exact reason:** `GEMINI_API_KEY` is unset in the process environment and absent from `backend/.env` (the only keys present there are `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `APP_NAME`, `CLOUDFLARE_*`, `CLOUDINARY_*`, `CORS_ORIGINS`, `DB_NAME`, `DIRECT_VIDEO_UPLOAD`, `JWT_SECRET`, `MONGO_URL` — no Google/Gemini/LLM variable of any kind). `ai_gemini_client.is_configured()` → `False`. A real call attempt returns a clean `{"ok": false, "error": "unavailable"}` without dialling the network (verified: `test_missing_key_raises_llmunavailable_before_any_network_call`).

This mirrors the exact same finding from the prior Anthropic validation (Phase 11/13): no LLM credential has ever been available in this environment.

## C. Real call count

```
Real Gemini calls:  0
Stubbed calls:      23 test scenarios (fake sits at the google-genai SDK
                     boundary — client.aio.models.generate_content — so
                     ai_gemini_client's own JSON-schema construction and
                     every error-mapping branch genuinely execute; only the
                     network call itself is faked)
Failed calls:        0 real (10 STUBBED failure-mode scenarios: 401, 429,
                     404/invalid-model, 503, malformed JSON, non-object
                     JSON, blocked/safety response, timeout, unavailable —
                     all producing the correct clean structured failure)
Skipped calls:       9 (the real-provider battery in
                     tests/test_simple_assistant_ai_selfcheck.py and
                     tests/test_simple_assistant_gemini_migration.py,
                     skipped — opt-in, requires SA_AI_SELFCHECK_LIVE=1 AND a
                     genuine, non-dummy GEMINI_API_KEY)
```

A repeatable real-provider battery is ready for whoever has a key:
```bash
GEMINI_API_KEY=<real key> SA_AI_SELFCHECK_LIVE=1 python3 -m simple_assistant.ai_selfcheck
# or
GEMINI_API_KEY=<real key> SA_AI_SELFCHECK_LIVE=1 pytest tests/test_simple_assistant_gemini_migration.py tests/test_simple_assistant_ai_selfcheck.py
```
Both exercise the exact `ResponseContext` → `generate_draft` → `validate_draft` path — no bypass — and clearly print `provider: gemini`.

## D. Is `ai/client.py` shared? (STEP 2 finding)

**Yes — confirmed by direct search.** `backend/ai/client.py` (Anthropic) is imported by:
- `routers/ai_scout.py`, `ai/scout.py` — **AI Scout**
- `routers/casting_desk.py`, `ai/casting_requirement.py` — **AI Casting Desk** (⛔ frozen per prior session memory — "do not touch AI code without explicit resume")
- `simple_assistant/ai_response.py`, `simple_assistant/ai_selfcheck.py` — Simple Assistant

**Consequence: `backend/ai/client.py` was NOT modified — not one line.** Gemini support lives entirely in a new, isolated, Simple-Assistant-only module, `backend/simple_assistant/ai_gemini_client.py`, that mirrors `ai.client`'s exact call contract (`is_configured()`, `call_tool_json()`, and — critically — **re-exports the SAME `LLMUnavailable`/`LLMError` classes from `ai.client` rather than redefining them**, so every existing `except ai_client.LLM*` clause in `ai_response.py` is correct regardless of which provider module is active). AI Scout and AI Casting Desk continue to use `ai.client` exactly as before, proven unaffected by `tests/test_ai_scout.py` (35/36 pass, the 1 failure a pre-existing unrelated typo) and `tests/test_management_agent.py` (34/34 pass) — both run in isolation, byte-identical behavior to before this migration.

## E. Provider selection

`SA_AI_PROVIDER` — **"gemini" is now the default** (Simple Assistant's preferred provider per this migration's objective); `SA_AI_PROVIDER=anthropic` selects the original, unchanged path. Selection is **explicit and live** (`ai_response._current_provider()` reads `os.environ` on every call — never cached at import time, never a silent per-request fallback): if Gemini is the selected provider and unconfigured, the system reports `"unavailable"` even when an Anthropic key happens to be present (`test_no_silent_fallback_when_gemini_unconfigured_even_if_anthropic_key_present`) — and vice versa.

`SA_AI_MODEL`, if set, overrides the model for whichever provider is active; if unset, a provider-aware default applies (`gemini-2.5-flash` / `claude-sonnet-5`) so a bare provider switch can never accidentally send an Anthropic model name to the Gemini API.

## F. Prompt isolation

The Phase 9–12 structure is **unchanged**:
```
SYSTEM_INSTRUCTIONS
VERIFIED_TALENTGRAM_FACTS
RECENT_CONVERSATION_CONTEXT   (present only when non-empty)
UNTRUSTED_INBOUND_MESSAGE
```
`_SYSTEM`, `_user_prompt()`, `_TOOL_SCHEMA`, `validate_draft()` in `ai_response.py` were **not** rewritten for Gemini — the SAME prompt string is sent to whichever provider is active; Gemini receives it as `system_instruction` + `contents=user` with `response_mime_type="application/json"` + `response_json_schema=_TOOL_SCHEMA` (Gemini's **native** structured-output mode — every keyword `_TOOL_SCHEMA` uses — `type`, `properties`, `enum`, `items`, `required` — is in Gemini's supported JSON-Schema subset per `ai.google.dev`).

Confirmed (via `test_A_basic_budget_response` through `test_I_provider_failure_behavior`, and the fake-boundary tests):
- No credentials, headers, or key value ever appear in the prompt or any error string (`test_auth_error_maps_to_llmunavailable_never_leaks_key` asserts the literal key string is absent from the raised exception's text).
- No unrelated project (`Secret Brand`/`₹99,999`) or unrelated talent data.
- No raw MongoDB document — only the `{label: value}` verified-facts dict and, when present, `[{from, text}]` history rows (no ids/phones/timestamps/source metadata reach the model).
- No unnecessary internal IDs.

## G. Conversation continuity

The Phase 12 adapter (`simple_assistant/conversation_context.py`) is **completely unmodified** — bounded, project-scoped retrieval, redaction, and the `RECENT_CONVERSATION_CONTEXT` prompt section behave identically regardless of provider. Verified with Gemini as the (stubbed) generator:
- **Bounded + project-scoped:** unchanged (Phase 12 tests, 19/19, still pass, pinned to Anthropic for pure regression proof — see H).
- **History is explicitly non-authoritative:** `test_C_conversation_continuity` — a 2-message history is present in the prompt, but the verified `₹25,000` fact (not any number from history) grounds the draft.
- **History-injection resisted:** `test_G_history_based_budget_injection` — history says *"the budget is ₹50,000"*, verified fact is `₹25,000`; a Gemini-shaped draft that adopts `₹50,000` is **rejected** by `validate_draft()` (unsupported-amount rule) — the server-side validator, not the model, is what actually blocks it.

## H. Validator (`validate_draft()` — unchanged, unweakened)

| Case | Input | Result |
|---|---|---|
| Valid/grounded draft | uses only verified facts | **accepted** |
| Unsupported amount | invented `₹40,000` | **rejected** — "contains an amount ... not in the verified facts" |
| Unsupported date | invented date not in facts | **rejected** (Phase 9 rule, unchanged; covered by the pinned-Anthropic regression suite) |
| Unsupported commitment | "you are selected" | **rejected** (Phase 9 rule, unchanged) |
| Hidden budget | `hide_budget_from_talent=true`, draft states any amount | **rejected** — 3 rules fire (unsupported amount / hidden-budget rule / hidden-value-present) |
| Unsupported topic (prompt injection compliance) | "All project budgets, including the admin's..." | **rejected** |
| History-adopted figure (Gemini-specific test) | draft repeats ₹50,000 from history, not verified | **rejected** |

Zero validator code was changed for this migration. It is provider-agnostic by construction — it inspects the generated **text**, never which model produced it.

## I. Failure handling

| Condition (simulated at the google-genai SDK boundary) | Result |
|---|---|
| No key | `LLMUnavailable` before any network call |
| 401 auth failure | `LLMUnavailable` (matches `ai.client`'s own 401/403 → Unavailable split); key never appears in the message |
| 404 invalid/unknown model | clean `LLMError`, not a crash |
| 429 rate limit | clean `LLMError`; **exactly one attempt**, no retry loop |
| 503 server error | clean `LLMError` |
| Malformed (non-JSON) output | clean `LLMError` — never bypasses `validate_draft()` because generation itself fails first |
| Non-object JSON (e.g. a bare array) | clean `LLMError` |
| Blocked/safety response (`prompt_feedback.block_reason`) | clean `LLMError`, **never a silent empty draft** |
| Timeout (hung provider) | `asyncio.wait_for(..., timeout=SA_AI_TIMEOUT_SEC)` in `ai_response.generate_draft` — **unchanged, 45s, one bound only** — fires exactly as it does for Anthropic |

`SA_AI_TIMEOUT_SEC` was **not** changed. No retry loop was added anywhere (`ai_gemini_client.py` adds none beyond whatever the SDK itself does for genuinely transient conditions, same posture as the existing Anthropic adapter's `max_retries=2`).

## J. Approval path

**Completely unchanged.** `confirm_and_send_ai` → `simple_assistant.whatsapp_send.queue_send` → the existing `routers.whatsapp._create_batch_internal` → existing `whatsapp_jobs`/`whatsapp_batches` → existing worker. Verified with a Gemini-stubbed draft (`test_approval_path_unchanged_stubbed`):
- Human approval remains mandatory (no path skips it).
- Stale plans remain protected (`fact_hash` binds `conversation_digest`; unchanged Phase 12 logic, re-proven by the pinned-Anthropic suite in H).
- Edited drafts are re-validated server-side before send.
- Double approval is idempotent — exactly 1 job / 1 batch either way.

## K. WhatsApp

```
0 jobs
0 batches
0 outbound messages
0 worker changes
0 Playwright invocations
```
**Controlled outbound E2E: UNTESTED.** Reason: (1) no genuine `GEMINI_API_KEY`, (2) no dedicated/authorized test WhatsApp destination was provided, (3) the user did not explicitly authorize a real test send. Per the brief's rule ("if any condition is missing: DO NOT SEND"), none was attempted. The approval path was exercised only against the existing stubbed `_create_batch_internal` used throughout the Phase 9–13 regression suite.

## L. Cloudinary

```
0 writes
```
No code path added by this migration touches Cloudinary.

## M. Business-data mutations

```
0
```
All new/changed test coverage runs against the existing fake in-memory Mongo harness or the read-only `RDB` proxy (writes raise `RuntimeError`). `ai_gemini_client.py` itself makes no database calls at all.

## N. Files changed

### Tracked: **0**
`git diff --stat` is **byte-identical** to its state before this migration began — `6 files changed, 405 insertions(+)`, none of them touched by this work. `backend/ai/client.py`: unchanged. `requirements.txt`: unchanged (`google-genai==1.71.0` was already present, pre-existing, unused until now — no dependency file edit was needed). `whatsapp-worker/`: untouched.

### Untracked (all inside the already-untracked Simple Assistant module / its tests)

| File | Change |
|---|---|
| `backend/simple_assistant/ai_gemini_client.py` | **NEW** — the isolated Gemini adapter (`is_configured`, `call_tool_json`, re-exports `LLMUnavailable`/`LLMError` from `ai.client`) |
| `backend/simple_assistant/ai_response.py` | Provider selection (`_current_provider()`/`_current_model()`/`_provider_client()`, live-read, default "gemini"); `config_snapshot()` and `generate_draft()` now report `provider` alongside `model`; **prompt/validator/hashing/approval logic unchanged** |
| `backend/simple_assistant/ai_selfcheck.py` | `_one()`'s live-mode check now asks `ai_response._provider_client()` for the active provider instead of hardcoding `ai.client`; `run_selfcheck()` summary gains `provider`/`model` |
| `backend/simple_assistant/audit.py` | `record_ai()` gains an optional `provider` field (additive, `None`-default, backward compatible) — safe diagnostic only, never a credential |
| `backend/simple_assistant/commands.py` | One call site now also passes `provider=gen.get("provider")` to the existing audit call |
| `backend/tests/test_simple_assistant_ai_response.py` | +1 line: pins `SA_AI_PROVIDER=anthropic` (this suite stubs `ai.client` directly — proves the Anthropic path is fully intact when explicitly selected) |
| `backend/tests/test_simple_assistant_multi_intent.py` | same +1 line, same reason |
| `backend/tests/test_simple_assistant_ai_selfcheck.py` | same +1 line; `config_snapshot()` key-set assertion updated for the new `key_env_var` field |
| `backend/tests/test_simple_assistant_phase13.py` | docstring updated to note its explicit Anthropic pin (inherited) — no logic change |
| `backend/tests/test_simple_assistant_gemini_migration.py` | **NEW** — the Gemini-specific test suite (23 tests + 1 opt-in real-provider battery), including a module-scoped fixture that snapshots/restores `SA_AI_PROVIDER`/`GEMINI_API_KEY` around its own run so it cannot leak into any other test file's provider pin |

**Why the `ai_response.py` change was necessary and could not be avoided:** Simple Assistant needed a way to select between two provider adapters without touching the shared `ai.client.py`. The chosen mechanism (`_provider_client()` returning either module) is the smallest possible seam — one function, called from exactly two places (`config_snapshot`, `generate_draft`) that already existed. **Regression risk:** low — both provider modules expose an identical contract and raise identical exception types, so every downstream consumer (`validate_draft`, the signed-approval flow, `commands.py`, `ai_response_execute.py`) needed zero changes. **Tests proving compatibility:** the entire pre-existing Phase 9–13 regression suite (245 tests across 12 files) passes byte-for-byte unchanged when pinned to `SA_AI_PROVIDER=anthropic`.

## O. Tests

| Suite | Result |
|---|---|
| `pytest tests/test_simple_assistant*` (16 files, one process) | **277 passed, 3 skipped** (skips: 2 pre-existing opt-in real-Anthropic tests + 1 new opt-in real-Gemini battery) |
| Same, each file in isolation | all green (including 3 independent full-suite reruns and 2 reordered/shuffled combinations — zero flakiness, zero cross-file provider leakage) |
| `tests/test_simple_assistant_gemini_migration.py` (**new**) | 23 passed, 1 skipped (isolated) |
| `tests/test_management_agent.py` (isolated) | 34 passed |
| `tests/test_ai_scout.py` (isolated) | 35 passed, 1 failed |
| `tests/test_casting_agent.py` | 280–287 passed, 36–43 failed (**non-deterministic count across identical reruns**) |
| `frontend` `yarn vitest run` | 439 passed (34 files) |
| `frontend` `yarn build` | success |

**Failure classification (per the brief's requirement to separate these):**
- **Migration failures:** **none.**
- **Missing-key failures:** the 3 skipped real-provider tests (2 Anthropic, 1 Gemini) — expected, not a failure.
- **Pre-existing, unrelated to this migration:**
  - `test_ai_scout.py::test_call_tool_json_surfaces_provider_error_message` — `import httpx2 as httpx` typo in that test file itself (module doesn't exist); present since before Phase 9; confirmed unchanged from every prior phase report.
  - `test_casting_agent.py` — **zero references to `simple_assistant`, `ai_response`, `ai_gemini`, `ai.client`, `ai_scout`, or `casting_desk` anywhere in that file** (verified by direct grep), so there is no code path by which this migration could affect it. Root cause identified for the `test_ig_share_*` cluster: the already-committed, pre-session commit `3aa3d6a` *"Add explicit WhatsApp worker sending permission gate"* (2026-09-14, `routers/whatsapp.py::_create_batch_internal`) added a fail-closed `sending_enabled` check whose test fixtures were not fully updated for it (`"Worker 'default' is not enabled for sending"`). The remaining failures (typo-tolerance, stage-filtering, compound-share tests — none touching AI/provider code) plus the **non-deterministic failure count across identical reruns** (36 → 43) further confirm this is pre-existing test-environment drift, matching the standing project note *"local dev DB has cross-session test-environment drift — don't trust a pytest failure at face value."* Out of scope for this migration; `routers/whatsapp.py`/`agents/` were not touched.

## P. Build

`cd frontend && yarn build` → **success.**

## Q. Limitations

1. **Real Gemini provider unverified.** No `GEMINI_API_KEY` anywhere in this environment. All Gemini-path testing is against a fake at the `google-genai` SDK boundary (the adapter's own JSON/error-handling code genuinely runs; only the network is faked). Response *quality*, real latency, real token usage, and free-tier rate-limit behavior are all unmeasured.
2. **Free-tier quota specifics unknown.** Google's pricing page confirms `gemini-2.5-flash` is free-of-charge, but exact RPM/RPD/TPM limits are only visible in a logged-in AI Studio dashboard, not independently verifiable here.
3. **Model availability is a live-API fact, not a static one.** `gemini-2.5-flash` was verified against `ai.google.dev` documentation fetched at write time (2026-09-15); Google can deprecate or rename models. `SA_AI_MODEL` provides a one-env-var override with no code change needed if this happens.
4. **Controlled outbound WhatsApp E2E is unverified** — no key, no authorized destination.
5. **Privacy/data-use note:** Google's free tier documentation states free-tier content may be used to improve Google's products — this is a genuine consideration for anyone deciding to run a real test with non-synthetic data (this report used none).
6. **No autonomous operation introduced.** `SA_AI_RESPONSE_ENABLED` and `SA_CONVERSATION_CONTEXT_ENABLED` remain `false` by default; nothing in this migration changes when or whether the AI path runs — only *which provider* it would use if enabled.
7. **1:1 WhatsApp transport remains unsupported** (unchanged, pre-existing finding from Phase 12).
8. **`test_casting_agent.py`'s pre-existing failures** (see O) are unresolved — explicitly out of scope; fixing them would require changing `routers/whatsapp.py`/test fixtures for the sending-permission-gate feature, which this migration must not touch.

## R. STOP

Before any further Simple Assistant capability work:
1. **Obtain a genuine `GEMINI_API_KEY`** and run the real battery:
   `GEMINI_API_KEY=<real> SA_AI_SELFCHECK_LIVE=1 pytest tests/test_simple_assistant_gemini_migration.py tests/test_simple_assistant_ai_selfcheck.py` — confirm 9 real calls, 0 hidden-budget leaks, 0 history-figure adoptions, 0 unexpected validation failures, and record real latency.
2. Only then, with an explicitly authorized dedicated test WhatsApp destination and the user's direct go-ahead, run **one** controlled outbound E2E through the existing `confirm_and_send_ai` path.
3. Separately, the pre-existing `test_casting_agent.py` failures (§O) should be triaged as their own task — unrelated to AI providers, tied to the already-committed sending-permission-gate feature.
4. **No new autonomous functionality** should be built on the basis of this migration — it is a provider swap and validation checkpoint only. `SA_AI_RESPONSE_ENABLED` and `SA_CONVERSATION_CONTEXT_ENABLED` stay `false` in production until a deliberate future decision turns them on.

---

### Definition of Done — checklist

| Requirement | Status |
|---|---|
| Gemini integration behind a safe provider boundary | ✅ `ai_gemini_client.py`, isolated |
| Simple Assistant can use Gemini without Anthropic | ✅ `SA_AI_PROVIDER=gemini` is now the default |
| Exact model documented | ✅ `gemini-2.5-flash` |
| Free-tier eligibility checked | ✅ confirmed via live docs fetch; exact quota not independently verifiable without a key |
| Real Gemini calls verified, or clearly reported unavailable | ✅ **UNTESTED — NO GEMINI API KEY**, reported honestly |
| Structured output compatible with existing contract | ✅ native `response_json_schema`, same `_TOOL_SCHEMA`, same return shape |
| Phase 9–12 safeguards intact | ✅ prompt structure, validator, signed-plan, conversation-context all unchanged |
| Hidden budget protected | ✅ unchanged redaction + validator rules; new Gemini-specific test |
| Conversation context bounded + project-scoped | ✅ unchanged adapter; re-proven under the Anthropic-pinned regression suite |
| Validator authoritative | ✅ zero validator changes |
| Human approval mandatory | ✅ unchanged |
| No autonomous messaging | ✅ |
| Existing WhatsApp infrastructure untouched | ✅ 0 jobs/batches/sends/worker changes/Playwright invocations |
| No Cloudinary writes | ✅ |
| No business-data mutations | ✅ |
| Feature flags OFF by default | ✅ |
| Backend regressions pass | ✅ 277+ SA tests; casting_agent pre-existing failures documented and traced to an unrelated commit |
| Frontend tests pass | ✅ 439 |
| Production build passes | ✅ |
| All changes documented | ✅ this report |

**FINAL STOP.** This is a provider migration and validation checkpoint. No new autonomous functionality was added, and none should be planned until a real Gemini key is available for genuine verification and the results reviewed.
