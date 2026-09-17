# Simple Assistant — Gemini Live Validation & Operational Readiness Checkpoint

**Date:** 2026-09-15 (updated — real key now available)
**Type:** validation / operational-readiness checkpoint — **NOT** a new capability phase.

---

## 1. Executive verdict

```
LIVE VALIDATION PARTIALLY PASSED — SHIPPED DEFAULT MODEL IS DEAD;
ALTERNATE MODEL HIT A STRUCTURED-OUTPUT PARSING ISSUE + FREE-TIER QUOTA EXHAUSTION
```

**A genuine `GEMINI_API_KEY` was reached and genuinely exercised — 18 real Gemini API calls were made this session.** The credential, network path, SDK integration, and every safety/error-handling code path were proven live and correct. **However, the complete 9-case battery did not produce a single usable grounded draft with either model tested:**

- The **currently shipped default model**, `gemini-2.5-flash`, is **confirmed dead for this key**: every one of 9 real calls returned a real HTTP 404 — *"This model models/gemini-2.5-flash is no longer available to new users."*
- Google's own error message recommended `gemini-3.6-flash`. Testing that (via the **existing** `SA_AI_MODEL` env-var override — **no code was changed**) got real, timed responses for 6 of 9 cases, but the adapter's `json.loads()` call failed on all 6 (`"LLM returned non-JSON output"`) — a genuine structured-output-compatibility finding, not a network/credential problem. The remaining 3 of 9 failed with a real `429 Quota exceeded` from Google, because the account's free-tier quota was consumed by the calls immediately before them (9 + 6 = 15 calls already made against the same key/project in this session).

No credential, safety, or approval-path code failed. **This is a model-configuration and (likely) a token-budget/response-format finding, not an integration bug in the isolation/validator/approval layers**, all of which behaved exactly as designed under real failure conditions. No code was changed to investigate or fix this — that decision is left to the user (see §14/§16).

## 2. Provider / model

| | |
|---|---|
| Provider | Google Gemini, official `google-genai` SDK |
| **Currently shipped default model** (`ai_response._current_model()`'s default when `SA_AI_MODEL` is unset) | `gemini-2.5-flash` — **confirmed via a real API call to be rejected with HTTP 404** for this key ("no longer available to new users") |
| **Model tested as an env-var override only** (`SA_AI_MODEL=gemini-3.6-flash`, no code change) | `gemini-3.6-flash` — reachable, returned real (non-empty, ~3.7–5.3s) responses, but they did not parse as the requested JSON schema |
| Endpoint | Gemini Developer API (public), not Vertex AI |

**No code change was made to `ai_response.py`'s default model in this task.** This report documents the finding; a fix is a separate, explicit decision for the user (§16).

## 3. Deployment configuration (Railway)

| Check | Result |
|---|---|
| Backend service identified | **`talentgram-railway`** (project `pacific-art`, environment `production`, domain `https://api.talentgramagency.com`) — distinguished from `whatsapp-worker-2` (the Playwright worker) and `talentgram-frontend - whatsapp` (a separate service) by service name and by cross-checking against prior deployment-audit docs in this repo |
| Does the deployed backend read `GEMINI_API_KEY`? | Yes — `ai_gemini_client.is_configured()` reads exactly that variable name; confirmed present on `talentgram-railway`'s variable set (checked by name only, via a redacting shell pipeline that strips values before they reach any output — the value itself was never displayed, logged, or retrieved by this session) |
| Has the latest deployment completed since the variable was added? | The latest deployment on `talentgram-railway` is `019013ea…`, status **SUCCESS**, timestamp **2026-09-15 12:08:09 +05:30** — the current, active deployment, roughly 30 minutes before this check. (Direct proof that *this* container's live process has the variable was not attempted — no diagnostic endpoint exists for that; see the limitation noted below. The live self-check calls in §5, run via `railway run` against this same service/environment, independently prove the key is live and valid at the account level, which is the stronger and more direct proof this checkpoint actually needed.) |
| Does the deployed runtime have `google-genai` installed? | **Confirmed directly from that deployment's own build log**: `Successfully installed ... google-genai-1.71.0 ...` with no install errors. Not assumed — read from the actual Railway build log for deployment `019013ea`. |
| Can the deployed backend safely report "key configured" without exposing it? | **No such endpoint currently exists.** `simple_assistant/router.py` exposes only `/health` (`{"status": "ok"}` — no config info) and no route surfaces `ai_response.config_snapshot()`. This is reported honestly as a gap, not fabricated. No endpoint was added — that would be a code change outside this checkpoint's scope. |

## 4. Local vs. Railway execution — how the live calls were actually made

`SA_AI_SELFCHECK_LIVE=1 python3 -m simple_assistant.ai_selfcheck`, run bare, executes **locally**, using only the local process's own environment. The local environment has no `GEMINI_API_KEY` (confirmed again this session) — running it bare would have (correctly) reported `UNTESTED`, not a failure, exactly as instructed.

**Mechanism used instead — the existing, first-party `railway run` CLI feature** (already authenticated in this environment; nothing new was installed or configured): it attaches a Railway service's *current, live* variable set to a locally-executed subprocess. This is not a new endpoint, does not bypass authentication (it rides this session's own already-authenticated `railway` login), and does not touch the deployed container. To avoid pulling *all* of production's variables (real Mongo URL, real Cloudinary, real JWT secret, etc.) into a local test run unnecessarily, a small scratch-only wrapper script (never committed, lives only under this session's scratchpad) extracted **only** `GEMINI_API_KEY` from the Railway-injected environment, discarded everything else Railway supplied, and ran the **unmodified, existing** `simple_assistant.ai_selfcheck` module as a subprocess with that one real value plus the same synthetic/local values every Simple Assistant test file already uses (dummy Mongo URL, dummy JWT secret, etc.). The wrapper scrubs the key from any captured output before writing anything to disk and asserts the key string is absent from the saved report before finishing.

Net effect: the self-check genuinely used the Railway-configured `GEMINI_API_KEY` against the real Gemini API, while zero real production data (Mongo, Cloudinary, JWT) was touched.

## 5. Real Gemini self-check results

**Existing self-check module used as-is — not replaced, not simplified.** Both runs exercised the real `ai_response.build_context` → `generate_draft` → (attempted) `validate_draft` path for all 9 cases (A–I, per the migration/prior-checkpoint case set, covering: basic budget, multi-intent, missing-fact honesty, negotiation, hidden budget, prompt injection, conversation continuity, history-figure injection, hidden-budget+history combined).

### Run 1 — shipped default, `gemini-2.5-flash`

```
real calls attempted:  9
successful:            0
failed:                9  (all: HTTP 404 — "model no longer available to new users")
model:                 gemini-2.5-flash
latency range:         442 ms – 1,180 ms  (avg ≈ 566 ms)
timeout behavior:      not exercised (every call failed fast, well under the 45s SA_AI_TIMEOUT_SEC bound)
quota/rate-limit:      none seen this run
validator rejections:  0  (no draft text was ever produced to validate)
hidden-budget leaks:   0
history-figure adoption: 0
prompt-injection failures: 0  (no draft text was produced for case F to fail on)
complete battery passed: NO
```

### Run 2 — `SA_AI_MODEL=gemini-3.6-flash` override (env var only, no code change)

```
real calls attempted:  9
successful:            0
failed:                9  — 6 "LLM returned non-JSON output" (a real, non-empty response that failed JSON parsing),
                            3 "HTTP 429 — Quota exceeded" (free-tier quota, exhausted by the 15 prior real calls
                            already made this session against the same key/project)
model:                 gemini-3.6-flash
latency range:         447 ms – 5,336 ms  (avg ≈ 2,584 ms) — the 6 slow ones (3.7–5.3s) are real generation
                        round-trips; the 3 fast ones (447–551ms) are the quota rejection, returned before any
                        generation began
timeout behavior:      not triggered — every call resolved (success or error) well under 45s
quota/rate-limit:      YES — real 429 "Quota exceeded for metric: generativelanguage.googleapis...", surfaced
                        as a clean LLMError, no retry attempted
validator rejections:  0  (no case produced text that reached validate_draft — generation itself failed first
                        in every case)
hidden-budget leaks:   0  (nothing was ever generated to leak — this is NOT the same as "hidden-budget
                        protection verified live"; it was not meaningfully exercised this session)
history-figure adoption: 0  (same caveat — not meaningfully exercised)
prompt-injection failures: 0  (case F never produced parseable output to check)
complete battery passed: NO
```

**Honest characterization, per the instruction to never overstate a skip/stub as a pass:** the *safety* checks (hidden-budget leak count, history-figure-adoption count) read `0` in both runs, but that is because **no case ever produced usable draft text** to check — not because a real draft was generated and then proven safe. Do not read `0 leaks` as "hidden-budget protection was live-proven" this session; it was **not**, for lack of any successful generation to test it against. The deterministic *canned-draft* validator checks (the offline half of the self-check, which always runs regardless of live results) did pass in both runs (`prompt_isolation_ok: true`, `validator_ok: true`) — those are unchanged, pre-existing, offline guarantees, not new live evidence.

## 6. Safety results

| Invariant | Result |
|---|---|
| `SA_AI_RESPONSE_ENABLED` stayed `false` | ✅ — never set to `true` anywhere in this checkpoint; the self-check bypasses that flag by design (it is a direct diagnostic tool, not the approval-gated command path) |
| `SA_CONVERSATION_CONTEXT_ENABLED` stayed `false` | ✅ — untouched |
| No WhatsApp messages sent | ✅ — the self-check never calls `whatsapp_send`/`_create_batch_internal` |
| No WhatsApp jobs/batches created | ✅ |
| WhatsApp worker not invoked | ✅ |
| No Cloudinary writes | ✅ — no code path in the self-check touches Cloudinary |
| No talent/project/submission/pipeline mutations | ✅ — the self-check builds synthetic in-memory contexts only; it never touches the database (confirmed: none of the wrapper's environment included a real, working Mongo connection — a dummy `mongodb://x` was used throughout, and the self-check code path never queries any collection regardless) |
| No key printed/logged/stored | ✅ — verified by direct inspection: the saved diagnostic reports contain the string `GEMINI_API_KEY` only as a field **name** (`"key_env_var": "GEMINI_API_KEY"`), never as a value; no `AIza…`-shaped token appears anywhere in any saved file or this report |
| No retry loop added | ✅ — every failure (404 / 429 / non-JSON) was a single attempt; nothing in this checkpoint added retry logic |
| `backend/ai/client.py` unmodified | ✅ |
| AI Scout / Casting Desk unmodified | ✅ |
| WhatsApp worker / shared WhatsApp infra unmodified | ✅ |

## 7. Provider isolation

Unchanged from the prior checkpoint's findings — re-confirmed this session:
- `backend/ai/client.py` (Anthropic, shared with AI Scout + Casting Desk) — untouched; `ANTHROPIC_API_KEY`/`ANTHROPIC_WORKSPACE_ID` remain independently configured on Railway (checked by name only) and are unaffected by the Gemini key or by this checkpoint.
- No `SA_AI_PROVIDER`/`SA_AI_MODEL`/other `SA_*` override exists on the Railway `talentgram-railway` service at all (checked by name only) — meaning **production, right now, runs on every Simple Assistant default**: provider = `gemini` (code default), model = `gemini-2.5-flash` (code default — the one just proven dead), `SA_AI_RESPONSE_ENABLED=false` (code default, so none of this is live in production traffic today regardless).
- `tests/test_ai_scout.py` (isolated): 35 passed, 1 pre-existing unrelated failure.
- `tests/test_casting_desk.py` (isolated): 15 passed.
- `tests/test_management_agent.py` (isolated): 34 passed.

## 8. Test results (post-live-validation regression)

| Suite | Result |
|---|---|
| `pytest tests/test_simple_assistant*` (16 files, one process) | **277 passed, 3 skipped** — unchanged from every prior checkpoint |
| `tests/test_ai_scout.py` (isolated) | 35 passed, 1 pre-existing unrelated failure |
| `tests/test_casting_desk.py` (isolated) | 15 passed |
| `tests/test_management_agent.py` (isolated) | 34 passed |
| `frontend` `yarn vitest run` | 439 passed (34 files) |
| `frontend` `yarn build` | success |

No regression was introduced by this checkpoint (no code was changed). Results are byte-identical to the prior checkpoint's regression numbers.

## 9. Pre-existing unrelated failures

Unchanged, not touched this task:
1. `tests/test_ai_scout.py::test_call_tool_json_surfaces_provider_error_message` — `import httpx2` typo inside that test file itself.
2. The previously-documented "Event loop is closed" cross-file pytest-asyncio contamination between `test_ai_scout.py`/`test_casting_desk.py` when run together.

## 10. Confirmations

```
0 outbound WhatsApp messages sent
0 WhatsApp jobs created
0 WhatsApp batches created
0 Cloudinary writes
0 business-data mutations (talent / project / submission / pipeline)
0 production feature flags enabled (SA_AI_RESPONSE_ENABLED and
  SA_CONVERSATION_CONTEXT_ENABLED remain false)
```

## 11. Real call summary (across this entire checkpoint)

```
Total real Gemini API calls made:  18
Successful (usable grounded draft): 0
Failed — model deprecated (404):    9   (gemini-2.5-flash, the shipped default)
Failed — non-JSON output:           6   (gemini-3.6-flash, override-only test)
Failed — quota exceeded (429):      3   (gemini-3.6-flash, override-only test)
Failed — timeout:                   0
Failed — auth/unavailable:          0   (the key itself authenticated successfully every time —
                                          every failure was model-availability, format, or quota,
                                          never a credential rejection)
```

## 12. Limitations

1. **No model tested this session produced a single usable grounded draft.** The shipped default is dead (404); the suggested replacement has a real structured-output parsing incompatibility that was observed but not root-caused (doing so would mean inspecting/logging raw model output and/or changing the adapter's request shape — both out of this checkpoint's scope).
2. **Free-tier quota for this key/project is now partially or fully consumed for the current window** (a real 429 was returned after 15 calls). Exact remaining quota is unknown; Google's own dashboard (`ai.dev/rate-limit`) is the source of truth, not independently queryable from here.
3. **No endpoint exists to remotely confirm the deployed container's live process environment** without adding one — the live self-check calls (run via `railway run`, not against the running deployed process) are the strongest available proof instead, and they do conclusively prove the key is valid and reachable at the account level.
4. The non-JSON-output finding for `gemini-3.6-flash` could stem from several distinct causes (markdown-fenced output despite `response_mime_type=json`, `max_output_tokens=400` being consumed by internal "thinking" tokens before any visible output, or a genuine schema-compatibility gap) — this was **observed, not diagnosed**, to avoid spending additional quota or making an unauthorized code change during a validation-only task.
5. This checkpoint intentionally did not attempt to determine "the correct model" beyond testing the one Google's own error message named — a deeper model survey was not performed.

## 13. Recommendation

**A separate authorized outbound WhatsApp E2E test must NOT be considered yet.** The live self-check did not pass — no model tested produced a working grounded draft. Sending a real WhatsApp message on top of a non-functional generation path would be unsafe and was correctly out of scope for this task regardless.

## 14. What actually needs to happen next (for the user to decide — no action taken)

1. **Update the shipped default model.** `gemini-2.5-flash` is confirmed dead for this key. This is a one-line change to `_DEFAULT_MODEL_BY_PROVIDER["gemini"]` in `backend/simple_assistant/ai_response.py` (and the corresponding default in `ai_gemini_client.py`) — genuinely small, but it **is** a code change, and this checkpoint's scope was validation only, so it was not made. A `SA_AI_MODEL` Railway variable override is also possible without any code change at all, once a working model is confirmed.
2. **Resolve the non-JSON-output finding before trusting `gemini-3.6-flash` (or whichever model is chosen next).** Recommend a small, separate, explicitly-authorized diagnostic pass: capture one raw (synthetic-data-only) response to see whether it's markdown-fenced, truncated, or genuinely malformed, then decide the fix (e.g., strip code fences before `json.loads`, raise `max_output_tokens`, or disable "thinking" tokens via `GenerateContentConfig.thinking_config` if that turns out to be the cause).
3. **Wait for quota to reset** (or confirm the account's actual free-tier limits at `ai.dev/rate-limit`) before running another full 9-case battery, to avoid repeatedly hitting 429 mid-battery.
4. Only after (1) and (2) produce at least one real, successfully-validated grounded draft should this checkpoint be re-run to reach `LIVE VALIDATION PASSED`.

---

### Definition-of-done checklist (this checkpoint)

| Requirement | Status |
|---|---|
| Deployment configuration confirmed (service, variable presence, deploy recency, dependency) | ✅ |
| Distinguished local vs. Railway execution | ✅ — used the existing `railway run` mechanism, no invented endpoint, no auth bypass |
| Real Gemini self-check run, existing mechanism, unmodified | ✅ — 18 real calls, 2 full battery runs |
| Sanitized results recorded | ✅ — key never printed/logged/stored anywhere |
| Safety boundaries preserved | ✅ — flags unchanged, no WhatsApp/Cloudinary/mutations |
| Regression run | ✅ — 277 SA + 35 AI Scout + 15 Casting Desk + 34 Management Agent + 439 frontend, all matching prior baselines |
| Report updated with accurate, non-inflated verdict | ✅ — this document |
| No outbound WhatsApp E2E performed | ✅ |
| No unrelated pre-existing failures "fixed" | ✅ — none touched |

**STOP.** Live credential and safety-path validation is genuinely complete and positive. Functional generation is **not yet working** with any model tested — this is a real, actionable, but out-of-this-checkpoint's-scope finding, reported honestly rather than concealed or worked around.
