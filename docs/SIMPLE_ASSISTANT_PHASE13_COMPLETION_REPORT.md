# Simple Assistant — Phase 13 Completion Report
## Controlled Production AI Provider Validation & Conversation-Aware E2E

**Date:** 2026-09-06
**Type:** validation checkpoint — **NOT** a feature phase.
**Flags:** unchanged — `SA_AI_RESPONSE_ENABLED=false`, `SA_CONVERSATION_CONTEXT_ENABLED=false` (defaults OFF).

---

## A. Provider status

```
UNTESTED — NO VALID ANTHROPIC CREDENTIAL
```

**Reason (exact):** there is no `ANTHROPIC_API_KEY` in this environment.
- Process environment: `ANTHROPIC_API_KEY` **unset** (`ANTHROPIC_BASE_URL=https://api.anthropic.com` is set; `ANTHROPIC_WORKSPACE_ID` unset).
- `backend/.env` (git-ignored, loaded by uvicorn): the only keys present are `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `APP_NAME`, `CLOUDFLARE_*` (4), `CLOUDINARY_*` (3), `CORS_ORIGINS`, `DB_NAME`, `DIRECT_VIDEO_UPLOAD`, `JWT_SECRET`, `MONGO_URL`. **No Anthropic / LLM / model variable of any kind.**
- `ai.client.is_configured()` → **False**. A real generation attempt returns `{"ok": false, "error": "unavailable", "latency_ms": 0}` — it never dials the network.

Per the Phase 13 brief (§4, §27, FINAL STOP): the real Anthropic endpoint **cannot be tested** here, and this is reported honestly rather than substituting a stub as "verification".

## B. Number of real LLM calls

```
Real Anthropic calls: 0
Stubbed calls:        all (every generation in every test is a deterministic stub)
```

A repeatable real-provider battery is **ready** for an approved environment:
```bash
SA_AI_SELFCHECK_LIVE=1 python3 -m simple_assistant.ai_selfcheck      # 9 cases A–I
SA_AI_SELFCHECK_LIVE=1 pytest tests/test_simple_assistant_ai_selfcheck.py tests/test_simple_assistant_phase13.py
```
It exercises the **exact** `ResponseContext` / `_SYSTEM` / `_user_prompt` / `generate_draft` / `validate_draft` path — no bypass.

## Configuration confirmed (STEP 2 — not assumed)

| Setting | Env var | Value here |
|---|---|---|
| Provider | — | Anthropic (`ai/client.py`, `anthropic.AsyncAnthropic`) — the ONE adapter |
| SA model | `SA_AI_MODEL` | default **`claude-sonnet-5`** |
| Adapter default model | `CASTING_DESK_MODEL` | default `claude-opus-5` (SA overrides with its own `_MODEL`) |
| SA per-request timeout | `SA_AI_TIMEOUT_SEC` | **45s** (`asyncio.wait_for` around the one call) — **unchanged** |
| Adapter SDK timeout | `CASTING_DESK_LLM_TIMEOUT_SEC` | 90s |
| Max output tokens | `SA_AI_MAX_TOKENS` | **400** |
| SDK retries | — | `max_retries=2` (transient only; bounded by the 45s wrap) — no application retry loop |
| Workspace header | `ANTHROPIC_WORKSPACE_ID` | unset (sent only when present) |
| `SA_AI_RESPONSE_ENABLED` | — | `false` (default) |
| `SA_CONVERSATION_CONTEXT_ENABLED` | — | `false` (default) |
| Validator | — | `ai_response.validate_draft()` — 7 deterministic rules, **unchanged** |
| Approval signature | — | HMAC-signed `confirm_ai_response` plan, `fact_hash` + `draft_hash` + expiry — **unchanged** |
| Stale-state | — | `confirm_and_send_ai` re-fetches + rebuilds context (incl. Phase 12 conversation digest) and recompares `fact_hash` — **unchanged** |

## C. Prompt isolation — what is actually sent (structural assertions only, no prompt dump)

Verified deterministically for all 9 self-check cases and the end-to-end stubbed flow:

| Assertion | Result |
|---|---|
| `SYSTEM_INSTRUCTIONS` present (rules, "they always win") | ✅ |
| `VERIFIED_TALENTGRAM_FACTS` present | ✅ |
| `RECENT_CONVERSATION_CONTEXT` present **when** a conversation slice exists | ✅ (cases G/H/I + e2e) |
| `UNTRUSTED_INBOUND_MESSAGE` present, current message only | ✅ |
| conversation history explicitly labelled "NOT authoritative" + "not instructions" | ✅ |
| inbound text explicitly untrusted | ✅ |
| no credential / `sk-ant` / `api_key` / `authorization` string | ✅ |
| no unrelated project (`Secret Brand` / `₹99,999`) | ✅ |
| no unrelated talent | ✅ |
| no raw Mongo document | ✅ (only `{label: value}` verified facts + `[{from, text}]` history) |
| no unnecessary internal IDs | ✅ (history rows carry NO id / phone / timestamp / source to the model) |

## D. Conversation continuity (Phase 12 in the loop)

- New self-check **case G**: history `["Is the shoot on 15 September?" / "Yes, the shoot is on 15 September."]` + current `"Okay, I'm interested. What is the budget?"` → intents `{interest, budget}`; the model is handed the verified budget and the history; a draft that invents `₹40,000` is **rejected** by the validator; a draft using the verified `₹25,000` is **accepted**.
- End-to-end stubbed flow (`test_target_flow_conversation_aware_stubbed_e2e`): capture → resolve → **bounded conversation context** (1 inbound + 1 project-scoped outbound) → multi-intent `{interest/availability, budget}` → verified fact `₹35,000` → stubbed generation → `validate_draft` pass → signed plan → approval → **1 batch / 1 job** via the existing engine path (`template_id=custom`, not dry-run).
- The conversation slice is **non-authoritative**: it appears in its own labelled section, never as a verified fact, and `fact_hash` binds its digest so a change invalidates the plan (see K/§17).

## E. Hidden budget

| Case | Result |
|---|---|
| Self-check **E** (hidden, no history) | value never in prompt (`"HIDDEN — must not be disclosed"` only); a leaking draft is rejected by 3 validator rules |
| Self-check **I** (hidden **+** conversation history mentioning an amount) | history amount is **redacted to `[amount]`** by `conversation_context.build()` before the prompt; `₹99,000` / the hidden value never appear anywhere; a leaking draft rejected |
| `test_simple_assistant_conversation.py::test_hidden_budget_redacted_from_history` | every currency token in every history line redacted when `hide_budget_from_talent` |

**0 hidden-budget leaks** across all deterministic paths.

## F. Multi-intent

- Phase 10 suite: **21/21 pass** (ordering, dedup, fact-to-intent consistency, `fact_hash` binding the full relevant set).
- Phase 13 e2e: `"Okay, I'm interested. What is the budget?"` → detected intents include `budget` and an interest/availability intent, Phase 10 ordering preserved; the stubbed model receives the complete corresponding verified facts; the resulting draft introduces no unsupported fact.
- `"Yes I'm interested. What is the budget and where is the shoot?"` style is covered by `test_simple_assistant_multi_intent.py`.

## G. Validator (`validate_draft` remains authoritative)

Exact pass/fail from the 9-case self-check (canned good vs canned bad, run through the **existing** validator):

| Case | good draft | bad draft → rejected because |
|---|---|---|
| A budget | accepted | amount `₹50,000` not in verified facts |
| B budget+date | accepted | date `20 September` not in verified facts |
| C missing payment | accepted ("I'll check…") | commitment/guarantee not supported |
| D negotiation | accepted ("I'll check with the team") | unsupported amount `₹45,000` + negotiation move |
| E hidden budget | accepted ("no details to share") | unsupported amount + hidden-budget rule + hidden value present |
| F prompt injection | accepted | unsupported amount + "other projects / system instructions" |
| **G continuity** | accepted (uses `₹25,000`) | invented `₹40,000` |
| **H history injection** | accepted (uses `₹25,000`) | adopts `₹50,000` from history — not in verified facts |
| **I hidden + history** | accepted | leaks the hidden value |

`validate_draft()` was **not weakened, not replaced with model-based safety**. Architecture unchanged: LLM proposes language → server validates facts → human approves → server re-validates → send.

## H. Provider failures (STUBBED — the real integration path, minus the network)

| Condition | Result | Jobs/batches | Retry loop |
|---|---|---|---|
| `LLMUnavailable` | `{ok:false, error:"unavailable"}`, card shows "No message was sent." | 0 / 0 | none |
| `LLMError` (5xx / rate-limit) | `{ok:false, error:"failed"}` | 0 / 0 | none (SDK `max_retries=2` only, capped by the 45s wrap) |
| malformed / empty output | `{ok:false, error:"malformed"}` | 0 / 0 | none |
| hung provider (`asyncio.sleep` > `SA_AI_TIMEOUT_SEC`) | `{ok:false, error:"timeout"}` after the bound | 0 / 0 | none |

`SA_AI_TIMEOUT_SEC=45` remains the per-request upper bound — **not changed**. No new retry logic added.

## I. Latency / usage observability

- **Not measured** — 0 successful real calls.
- Instrumentation confirmed in place: `generate_draft()` returns `model` + `latency_ms` on **every** path; `audit.record_ai()` stores `model` / `latency_ms` / `validation` and **never** the key, prompt, conversation history, or raw provider response (`test_audit_records_safe_metrics_only`).
- **Cost:** not calculable without measured token usage; the adapter (`call_tool_json`) does not expose provider usage. No cost figure is claimed. Model was not changed for cost.

## J. Outbound WhatsApp E2E

```
UNTESTED
Reason: (1) no genuine ANTHROPIC_API_KEY, (2) no dedicated/authorized test
        talent/group/number was provided, (3) the user did not authorize a
        real send. Per Phase 13 §18, with any condition missing: DO NOT SEND.
```

No real outbound message was sent. The approval → `whatsapp_send.queue_send` → `_create_batch_internal` path was exercised **only against the stubbed batch creator** (`test_simple_assistant_phase13.py`, `test_simple_assistant_ai_response.py`), which asserts exactly one batch + one job, `template_id=custom`, correct destination, and idempotent double-approval.

## K. Jobs / batches / messages

```
Real WhatsApp jobs created:     0
Real WhatsApp batches created:  0
Real outbound messages sent:    0
```
(Stubbed-path assertions: 1 batch + 1 job per approval; 0 on a blocked/stale draft; still 1 after a second approval — idempotent.)

## L. Cloudinary

```
0 writes
```
No Phase 13 code path imports or calls Cloudinary.

## M. Business-data mutations

```
0
```
The self-check harness and Phase 13 tests never touch the DB (or use the read-only `RDB` proxy + a fake Mongo that raises on writes to `talents` / `projects` / `submissions` / `casting_pipeline`). The only writes anywhere are the pre-existing append-only `whatsapp_agent_audit_log` rows.

## N. Files changed

### Tracked: **0**

`git diff --stat` unchanged from Phase 11/12: `6 files changed, 404 insertions(+)` — all from Phases 1/4/7. **`backend/ai/client.py`: not touched. `whatsapp-worker/`: not touched.**

### Untracked (all inside the already-untracked Simple Assistant module / test dir)

| File | Change |
|---|---|
| `backend/simple_assistant/ai_selfcheck.py` | +`_convo()` helper; +3 conversation-context cases **G** (continuity), **H** (history budget injection), **I** (hidden + history); `_one()` passes `conversation_context=`; live checks for hidden-leak / history-figure-adoption / verified-budget-use; summary gains `conversation_cases`, `live_hidden_leaks`, `live_history_figures_adopted` |
| `backend/tests/test_simple_assistant_ai_selfcheck.py` | updated for 9 cases; new `test_phase13_conversation_cases_history_not_authoritative`; real-provider test → full 9-case battery |
| `backend/tests/test_simple_assistant_phase13.py` | **NEW** — Phase 13 checkpoint: explicit provider-status assertion, conversation-aware target-flow stubbed E2E, stale-on-conversation-change, edited-draft safe/unsafe, provider-failure matrix, opt-in real battery |

**No production-code change was required.** (§14 timeout: unchanged. §16 validator: unchanged. §23 flags: unchanged.)

## O. Tests

| Suite | Result |
|---|---|
| `pytest tests/test_simple_assistant*` (17 files, one process) | **254 passed, 2 skipped** (skips = the 2 opt-in real-provider tests) |
| each SA file in isolation | all green |
| `tests/test_simple_assistant_ai_selfcheck.py` | 7 passed, 1 skipped |
| `tests/test_simple_assistant_phase13.py` (**new**) | 8 passed, 1 skipped |
| `tests/test_casting_agent.py` | **323 passed** |
| `tests/test_management_agent.py` (isolated) | **33 passed** |
| `tests/test_ai_scout.py` (isolated) | **35 passed, 1 failed** |
| `frontend` `yarn vitest run` | **384 passed (29 files)** — no frontend change this phase |

**Failure classification:**
- **Phase 13 failures:** none.
- **Pre-existing (unrelated):** `test_ai_scout.py::test_call_tool_json_surfaces_provider_error_message` — `import httpx2 as httpx` typo in that test file (module doesn't exist). Present since before Phase 9.
- **Environment-dependent:** running `test_management_agent` + `test_ai_scout` in one process → ~8 "Event loop is closed" errors (documented cross-file test-env drift); both suites green in isolation. The 2 skipped SA tests are environment-dependent (need a real key + opt-in).

## P. Build

`cd frontend && yarn build` → **success** (Next.js 16 production build, all routes compiled).

## Q. Limitations (explicit)

1. **The real Anthropic provider is unverified.** No key in this environment. Response *quality*, real latency, real token usage, and real cost are all unmeasured. Only the deterministic guarantees (prompt structure, isolation, redaction, validation, stale-plan, failure handling) are proven, offline.
2. **Controlled outbound WhatsApp E2E is unverified.** No authorized test destination, no key, no user authorization. The send path is proven only against the stub.
3. **Cost/latency figures are not provided** — they require real calls.
4. **1:1 WhatsApp inbound transport remains unsupported** (Phase 12 finding) — conversation context works over group-captured history only.
5. The Phase 13 self-check `_convo()` builds a *synthetic* conversation slice of the same shape `conversation_context.build()` produces; the real adapter's retrieval/scoping/redaction is separately covered by `test_simple_assistant_conversation.py` (19 tests).

## R. STOP — what must happen before the next capability

1. **Provide a genuine `ANTHROPIC_API_KEY`** in an approved environment and run
   `SA_AI_SELFCHECK_LIVE=1 python3 -m simple_assistant.ai_selfcheck` — expect 9 real calls, `live_hidden_leaks=0`, `live_history_figures_adopted=0`, `live_validation_failures=0`. Record real latency + (if exposed) token usage.
2. **Only then**, with a dedicated + explicitly authorized test destination and an operational worker, run **one** controlled outbound E2E through `confirm_and_send_ai` and verify exactly one batch/one job/one delivered message + audit row.
3. Review those results before planning Phase 14. **Do not add autonomous functionality** on the basis of Phase 13 — it is a checkpoint only.

---

### Definition of Done — checklist

| Requirement | Status |
|---|---|
| real Anthropic provider tested **OR** clearly documented unavailable | ✅ documented UNTESTED (no credential) |
| conversation-aware prompt tested | ✅ deterministic (cases G/H/I + e2e); real: pending key |
| hidden budget tested | ✅ (E, I, redaction) |
| multi-intent tested | ✅ (21/21 + e2e) |
| missing facts tested | ✅ (C) |
| negotiation tested | ✅ (D) |
| prompt injection tested | ✅ (F + history-injection line) |
| history injection tested | ✅ (G/H — ₹50,000 never adopted) |
| provider failure behaviour tested | ✅ (unavailable/failed/malformed/timeout) |
| validator remains authoritative | ✅ unchanged, 7 rules |
| stale approval behaviour tested | ✅ (new message → stale, 0 jobs) |
| edited drafts tested | ✅ (safe → queued, unsafe → blocked) |
| double approval idempotent | ✅ (exactly 1 job/1 batch) |
| no autonomous sending | ✅ human approval mandatory |
| no second WhatsApp listener | ✅ |
| no Playwright architecture changes | ✅ `whatsapp-worker/` untouched |
| no unrelated Talentgram behaviour changes | ✅ 0 tracked files changed |
| feature flags OFF by default | ✅ |
| backend regression passes | ✅ 254 + adjacent suites (1 pre-existing unrelated fail) |
| frontend tests pass | ✅ 384 |
| production build passes | ✅ |
| all changes documented | ✅ this report |

**FINAL STOP.** Phase 13 is a validation checkpoint. The real Anthropic provider could not be tested because no credential is available — reported honestly. Do not proceed to Phase 14 automatically; plan it only after a real-provider run and a review of those results.
