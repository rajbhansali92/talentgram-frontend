# Simple Assistant — Pre-Commit Security & Architecture Audit

**Date:** 2026-09-17 (updated — SA-1 and SA-2 resolved)
**Type:** Inspection and reporting, then two scoped, minimal-diff fixes for the two Low findings this same audit identified. No flags enabled, no deployment, no commit, no push, no WhatsApp send, no production data mutation, no Phase 14, no new functionality, no change to any existing Talentgram workflow.
**Scope:** Complete pre-commit audit of the Simple Assistant module and every one of its integration points into existing Talentgram code, ahead of a first scoped commit — followed by resolving the two findings (SA-1, SA-2) the audit surfaced.

---

## Executive summary

**Commit-readiness decision:**

```
READY FOR SCOPED COMMIT
```

*(Updated from the original `READY AFTER SPECIFIC FIXES` — both fixes are now applied and verified; see "SA-1 / SA-2 resolution" below.)*

Simple Assistant is a well-isolated, additive module. Every mutation path this audit traced goes through an existing canonical Talentgram service function (never a new raw DB write), every send goes through the existing WhatsApp Engine job queue (never a new transport), and the two feature flags that gate the entire surface both default OFF — confirmed not just by reading the code but by an empirical test (`TestClient` request against the live-imported server, flag unset: every `/api/simple-assistant/*` route returned `404`). No Blocker or High-severity issue was found. The two Low findings (a stale docstring, and no explicit TTL on two plan kinds) have now been resolved — see below.

The one open item outside code quality is **Case I of the Gemini self-check battery (hidden budget + conflicting conversation history) has never completed a live provider call**, across four attempts on three separate checkpoints — always a real 429/503 from Google, never a validator or safety failure. `SA_AI_RESPONSE_ENABLED` stays `false`; this does not block a scoped commit of the code itself, but it does mean the AI-response feature specifically should not be enabled in production until Case I completes.

---

## SA-1 / SA-2 resolution (this update)

### SA-1 — RESOLVED

**File changed:** `backend/simple_assistant/__init__.py` (1 line).

The module docstring's claim that `SIMPLE_ASSISTANT_ENABLED` "default[s] on" was corrected to state it defaults **OFF**, matching `is_enabled()`'s actual code three lines below (which was always correct — only the prose was wrong):

```diff
-  - Gated behind ``SIMPLE_ASSISTANT_ENABLED`` (default on; set to
-    ``false``/``0``/``no`` to make every route 404, fully removing the
-    surface without a redeploy of anything else).
+  - Gated behind ``SIMPLE_ASSISTANT_ENABLED`` (default OFF; set to
+    ``true``/``1``/``yes``/``on`` to expose the surface — any other value,
+    including unset, 404s every route in the module).
```

No test was added for this: an existing, already-passing test (`tests/test_simple_assistant.py::test_feature_flag`) already asserts the exact behavior this docstring describes — unset → `False`, `"true"`/`"1"` → `True`, `"false"`/`"0"`/`""`/`"no"`/`"off"` → `False` — so the correct behavior was already locked in by a real test; only the comment was stale. Per the instruction not to introduce unnecessary test complexity, no new test was written to assert prose text itself.

### SA-2 — RESOLVED (TTL added, judged safe)

**Investigation (completed before any code change, as required):**

1. **Where plan creation timestamps are generated:** Nowhere, for `confirm_plan`/`confirm_comm`, prior to this change. Only `confirm_ai_response` computed a timestamp-derived field (`expires_at = now + 30min`, in `commands.py`'s AI-draft-building code, ~line 1770) — and only that one deadline value, not a separate `created_at`.
2. **What is included in the signed payload:** The single shared helper `commands._ctx()` builds and HMAC-signs the entire `pending` dict as one unit (`security.sign(pending)`, over a canonical, sorted-keys JSON serialization) — every key present, including `plan_id`, is automatically covered. `_ctx()` is the **only** place that produces a signed `context`; every one of the module's construction sites for `confirm_plan` (3) and `confirm_comm` (1, shared by all comm action types) already routes through it.
3. **How expiry is currently enforced (for `confirm_ai_response`, the only precedent):** A single check at the very top of `ai_response_execute.confirm_and_send_ai()`, right after the kind/structural-completeness checks and before idempotency: parse `pending["expires_at"]`, compare to `datetime.now(timezone.utc)`, block if past. No second check later in that same function.
4. **Whether any legitimate existing flow depends on plans outliving 30 minutes:** No test in the existing suite (`test_simple_assistant_execute.py`, `test_simple_assistant_comm.py`, or anywhere else) constructs a plan and waits/simulates elapsed time before confirming — every existing test previews and confirms within the same synchronous call, so real elapsed time is always ~0. No test's *expected outcome* depends on a plan still being valid after any meaningful delay. Running the full suite after implementing the change (below) confirms this empirically: zero existing tests broke.
5. **Effect on clarification/resume, media uploads, communication status, idempotency:**
   - **Clarification/resume flows** (`clarify_project`, `clarify_talent`, `clarify_comm_*`, `clarify_sub_*`, `comm_media_partial`) are a different set of `kind`s, not in `_CONFIRMABLE_KINDS` and not in the new `_TTL_KINDS` — unaffected. A resumed clarification that eventually produces a `confirm_plan`/`confirm_comm` pending gets a **fresh** `_ctx()` call at that moment, so its TTL starts from the moment of that fresh preview, never inherited from the original ask.
   - **`confirm_submission`/`confirm_upload`** (media attach/ingest) were explicitly out of scope for this change per the instruction, and were deliberately left with no `expires_at` — confirmed both by code (only `confirm_plan`/`confirm_comm` are in the new `_TTL_KINDS` set) and by a new test (`test_confirm_submission_and_ai_response_kinds_unaffected_by_confirm_plan_ttl`) that asserts a `confirm_submission` pending built through `_ctx()` still carries no `expires_at`.
   - **Communication status** (`comm_status`/`status_token`) is a **separate** HMAC signature, issued in the confirm **result** (`{"plan_id", "batch_ids"}`, signed independently), used afterward to poll delivery status — it is not part of the `pending` context this change touches, and is not time-bounded by this change (unaffected, unchanged).
   - **Idempotency** (`audit.find_completed(plan_id)`) is orthogonal: `plan_id` generation is untouched, and the ordering (expiry checked before idempotency, mirroring `confirm_ai_response`'s exact precedent) means a plan that already executed successfully is still correctly identified — expiry only ever fires for a plan that has *not yet* been confirmed.
6. **Whether expiry must be checked both before and during execution:** Judged yes, as defense-in-depth: both `confirm_and_execute()` and `confirm_and_send()` have `await` points (live DB re-resolution) between the entry check and the actual mutating/sending call. A second, cheap recheck was added immediately before each function's real mutation/send — in the currently-synchronous, sub-second execution path this is not expected to ever fire differently from the entry check, but it costs nothing and closes the theoretical gap.

**Conclusion: safe to add**, matching `confirm_ai_response`'s already-accepted precedent, with zero interaction found across clarification, upload/attach, status-polling, or idempotency.

**Implementation (minimal, backward-compatible):**

| File | Change |
|---|---|
| `backend/simple_assistant/commands.py` | Added `_TTL_KINDS = ("confirm_plan", "confirm_comm")` and `_PLAN_TTL_MINUTES = 30`. `_ctx()` now stamps `expires_at` (30 min from creation) into the `pending` dict for these two kinds only, before signing — reusing the exact same `sign()` call, no new signing system. Added one new helper, `_is_expired(pending)`, mirroring `ai_response_execute.py`'s existing try/except-on-`fromisoformat` pattern; a missing/unparseable `expires_at` is treated as "does not expire," so `confirm_ai_response` (which sets its own, unchanged) and `confirm_submission`/`confirm_upload` (which get none) are both correctly unaffected. |
| `backend/simple_assistant/execute.py` | Imports `_is_expired`. Added one check right after the existing structural-validation step and before idempotency (mirroring `confirm_ai_response`'s relative ordering exactly) — an expired `confirm_plan` is blocked with `need_refresh=True`, same shape as every other "please ask me again" rejection in this file. Added a second, defense-in-depth recheck immediately before the three canonical mutating calls (`bulk_move_by_talent_ids`/`add_talents_to_pipeline`/`bulk_delete_pipeline`) — nothing has mutated by that point, so it can still cleanly refuse. |
| `backend/simple_assistant/comm_execute.py` | Same pattern: imports `_is_expired`, one check after the existing structural-completeness check and before idempotency, one defense-in-depth recheck immediately before the send loop that calls `whatsapp_send.queue_send()` — nothing has been queued by that point. |
| `backend/tests/test_simple_assistant_execute.py` | +3 tests (below). |
| `backend/tests/test_simple_assistant_comm.py` | +2 tests (below). |

No change to `security.py` (signing/verification logic untouched), no change to `ai_response_execute.py` (confirm_ai_response's own expiry is untouched — still its own inline 30-minute stamp, still checked exactly as before), no change to `submission_execute.py`/`submission.py`/`comm.py`/any other file, no change to any client-facing response shape beyond the new (and previously impossible) "expired" message appearing only when a plan genuinely has expired.

**New tests added (5, all offline, no live provider/DB):**

- `test_fresh_plan_carries_a_30min_expiry_and_still_executes` — a freshly-built `confirm_plan` context carries `expires_at` ≈ now+30min, and a normal (non-expired) confirm still executes exactly as before.
- `test_expired_plan_refuses_even_with_a_valid_signature` — builds a genuinely expired **and validly re-signed** context (not merely tampered — the signature is recomputed with `security.sign()` after backdating `expires_at`, specifically to exercise the new expiry check itself rather than the already-separately-tested signature-mismatch path) → `blocked`, `need_refresh=True`, nothing mutated, and not falsely recorded in the audit log as executed.
- `test_confirm_submission_and_ai_response_kinds_unaffected_by_confirm_plan_ttl` — a `confirm_submission` pending built through the same `_ctx()` still carries no `expires_at`.
- `test_fresh_comm_plan_carries_a_30min_expiry_and_still_sends` — same proof as the first, for `confirm_comm`.
- `test_expired_comm_plan_refuses_even_with_a_valid_signature` — same genuinely-expired-and-validly-signed proof, for `confirm_comm`; confirms no *real* (non-dry-run) WhatsApp batch call occurs.

---

## 1. Repository state

**Branch:** `main`
**HEAD:** `3aa3d6a` — "Add explicit WhatsApp worker sending permission gate" (unrelated to Simple Assistant)

**`git status` (full, current):**

```
Changes not staged for commit:
	modified:   backend/core.py
	modified:   backend/routers/agents_whatsapp.py
	modified:   backend/routers/submissions.py
	modified:   backend/server.py
	modified:   frontend/src/components/AdminApp.jsx
	modified:   frontend/src/components/AdminLayout.jsx

Untracked files: (66 total — see breakdown below)
```

### 1.1 Tracked, modified files (6) — all Simple Assistant integration points

| File | Diff size | Nature |
|---|---|---|
| `backend/core.py` | +18 | Purely additive — 6 new Mongo index specs appended to the existing `p0_indexes` list, for the Phase 7 `whatsapp_inbound_messages` collection. No existing line changed. |
| `backend/routers/agents_whatsapp.py` | +23 | Purely additive — one new `import inbound_messages`, and a `try/except`-wrapped, non-fatal call to `inbound_messages.capture_inbound(...)` inserted into the existing `/api/agents/whatsapp/inbound` handler, before the existing `handle_inbound_message(...)` call. The existing call and its return value are untouched. |
| `backend/routers/submissions.py` | +352 | Purely additive — two new canonical service **functions** (`attach_existing_talent_media_to_submission`, `ingest_new_audition_media_to_submission`) appended before an existing route. No existing route or function body is modified. Neither function is reachable by any HTTP route in this file — both are called only from `backend/simple_assistant/submission_execute.py` (confirmed by grep, §3). |
| `backend/server.py` | +5 | Purely additive — one import (`from simple_assistant import router as simple_assistant_router`) and one `app.include_router(...)` call, in the same style as every other router registration in the file. |
| `frontend/src/components/AdminApp.jsx` | +2 | Purely additive — one import, one `<Route>` entry. |
| `frontend/src/components/AdminLayout.jsx` | +5 | Purely additive — one import, one conditionally-rendered nav item (`...(SIMPLE_ASSISTANT_ENABLED ? [...] : [])`). |

**Total: 405 insertions, 0 deletions, across 6 files.** Every diff was read in full for this audit (not just `--stat`); none contains a modification to a pre-existing line.

### 1.2 Untracked files relevant to Simple Assistant (56)

**Backend (42):**
- `backend/simple_assistant/` — 25 `.py` files (the module itself; full list in §2)
- `backend/tests/test_simple_assistant*.py` — 17 files

**Frontend (14):**
- `frontend/src/components/simple-assistant/` — 8 files (`AssistantConsole.jsx` + 5 sibling components/styles + 1 test file)
- `frontend/src/hooks/useAssistantCommand.js`, `useAssistantOverview.js`, `useAssistantOverview.test.js`
- `frontend/src/lib/simpleAssistant.js`
- `frontend/src/pages-components/SimpleAssistant.jsx`, `SimpleAssistant.test.jsx`

**Docs, SA-related (7 existing + this new one = 8):**
`SIMPLE_ASSISTANT_GEMINI_COMPATIBILITY_REPORT.md`, `SIMPLE_ASSISTANT_GEMINI_FINAL_VALIDATION_REPORT.md`, `SIMPLE_ASSISTANT_GEMINI_LIVE_VALIDATION_REPORT.md`, `SIMPLE_ASSISTANT_GEMINI_MIGRATION_REPORT.md`, `SIMPLE_ASSISTANT_PHASE11_COMPLETION_REPORT.md`, `SIMPLE_ASSISTANT_PHASE12_COMPLETION_REPORT.md`, `SIMPLE_ASSISTANT_PHASE13_COMPLETION_REPORT.md`, and this file.

### 1.3 Unrelated untracked files present in the working tree — **must NOT be part of a Simple Assistant commit**

These pre-date and are unconnected to this workstream (confirmed against project memory — several belong to explicitly **frozen** or **audit-only, no-plan-approved** workstreams):

- `backend/migrations/reports/*.json`, `*.txt` (8 files) — one-off dry-run/apply output artifacts from the unrelated "P3 ownership" and "talent dedup" migrations (2026-08-17, 2026-08-30). Scratch output, not source.
- `docs/ADR_CANONICAL_PROFILE_OWNERSHIP.md`, `CANONICAL_ARCHITECTURE_REDESIGN.md`, `CANONICAL_DATA_DICTIONARY.md`, `CLOUDINARY_P3_COMPLETION_REPORT.md`, `CONSISTENCY_REVIEW_ADR_CDD.md`, `IMPLEMENTATION_ROADMAP_V1.md`, `MANAGEMENT_AGENT_COMMAND_COVERAGE.md`, `PHASE4_INDEPENDENT_PRODUCTION_AUDIT.md`, `REVERIFICATION_ISSUE1_ISSUE2.md`, `TALENT_DASHBOARD_MIGRATION_HANDOFF.md`, `cloudinary_b_196_examine.csv` — all belong to separate, unrelated workstreams (canonical-architecture planning, the frozen Cloudinary rearchitecture, the audit-only Talent Profile Architecture review). None reference or depend on Simple Assistant.

**These must be left untouched by this commit** — neither added nor deleted. They are pre-existing working-tree state, not something this audit or a Simple Assistant commit should decide the fate of.

### 1.4 Commit history

```
git log --oneline -- backend/simple_assistant/     →  0 commits (confirmed again this audit)
git log --oneline -- backend/inbound_messages.py    →  1 commit (already tracked, already clean — Phase 7, unrelated to this commit)
```

`backend/simple_assistant/` has never been committed, in any form, at any point in this project's history. It has never been deployed to Railway (no deploy log or build has ever included it, since Railway builds from git).

### 1.5 `.gitignore` risk

Checked explicitly with `git check-ignore -v` against every file in `backend/simple_assistant/` and every Simple Assistant frontend file: **zero matches**. `.gitignore` contains no `simple_assistant` pattern and nothing else in it (verified by reading every `.env*`/`node_modules`/`__pycache__` rule) would accidentally catch any Simple Assistant source file. Nothing will be silently excluded from a future `git add`.

### 1.6 Dependency / configuration completeness

- **Backend:** `google-genai==1.71.0` is already pinned in `backend/requirements.txt` (line 49) — no `requirements.txt` change needed. `backend/runtime.txt` (`python-3.11`) is unrelated to this module and unchanged. No Dockerfile/`railway.json` exists at `/backend` — Railway's Nixpacks builder installs directly from `requirements.txt` (confirmed in the prior Gemini migration checkpoint via `railway deployment list` build logs), so no separate packaging step is needed for the new backend files once committed.
- **Frontend:** every external package Simple Assistant's frontend files import (`react`, `react-router-dom`, `lucide-react`, `@testing-library/react`, `vitest`) is already a `package.json` dependency — confirmed by extracting every non-relative import across all 14 frontend SA files. No `package.json` change needed.
- **Import-path sanity check:** `AdminApp.jsx` imports `SimpleAssistant` from `@/pages/SimpleAssistant`, and the actual file lives at `frontend/src/pages-components/SimpleAssistant.jsx` — this is **not** a broken import: the project's `jsconfig.json` maps `"@/pages/*": ["src/pages-components/*"]` (confirmed; every other `@/pages/*` import in `AdminApp.jsx` follows the identical convention). The production build (§8) compiles this cleanly, confirming the alias resolves correctly.

---

## 2. Architecture map

```
User types in the Assistant Console (frontend/src/components/simple-assistant/AssistantConsole.jsx)
  → POST /api/simple-assistant/command   (backend/simple_assistant/router.py)
      → _require_enabled()  — 404 if SIMPLE_ASSISTANT_ENABLED is not truthy
      → current_team_or_admin  — the SAME JWT dependency every other admin route uses
      → commands.run_command()            — regex/deterministic NLU, intent detection
          → readonly_db.RDB                — a structural read-only proxy (§4); resolves
                                              project/talent/pipeline/submission state
          → build a PREVIEW only            — nothing is mutated, nothing is sent
          → security.sign(pending)          — HMAC-SHA256 the proposed plan
      ← {message, plan/comm/sub preview, context: {pending, sig}}   — the "context" blob is
        opaque and round-trips through the client; the browser can read but not forge it

User reviews the preview, clicks "Confirm"
  → POST /api/simple-assistant/command/confirm   (same router, same auth + flag gate)
      → security.verify(pending, sig)      — reject a tampered/forged/expired-signature context
      → dispatch by pending["kind"]:
          confirm_plan          → execute.py           → routers.casting_pipeline.{add_talents_to_pipeline,
                                                           bulk_move_by_talent_ids, bulk_delete_pipeline}
          confirm_comm           → comm_execute.py       → whatsapp_send.py → routers.whatsapp._create_batch_internal
                                                            (the EXISTING WhatsApp Engine job queue)
          confirm_submission     → submission_execute.py → routers.submissions.attach_existing_talent_media_to_submission
          confirm_upload          (via /command/upload)  → routers.submissions.ingest_new_audition_media_to_submission
                                                            → core.upload_and_track_asset (existing Cloudinary pipeline)
          confirm_ai_response    → ai_response_execute.py → whatsapp_send.py → routers.whatsapp._create_batch_internal
      → every path: live re-resolve entity IDs/destination/media from the DATABASE
        (never from the client payload) → stale/idempotent check → run the ONE canonical
        existing function that performs the real mutation/send → post-verify → audit.record*()
  ← {state: executed/queued/attached/stale/blocked/failed, result, fresh overview}
```

**Separately, read-only:**
```
Existing WhatsApp worker → POST /api/agents/whatsapp/inbound (backend/routers/agents_whatsapp.py, UNCHANGED endpoint)
  → [NEW, additive, non-fatal] inbound_messages.capture_inbound(...)   — writes ONE new collection,
    whatsapp_inbound_messages, gated by SA_INBOUND_CAPTURE_ENABLED; swallows its own exceptions
  → [UNCHANGED] handle_inbound_message(...)                            — the existing dispatcher, untouched
```

**Where Simple Assistant can read, write, send, upload, or mutate data** (every path found in this audit):

| Capability | Mechanism | Gate |
|---|---|---|
| Read project/talent/pipeline/submission state | `readonly_db.RDB` — structural read-only proxy | `SIMPLE_ASSISTANT_ENABLED` (route 404) |
| Write to `whatsapp_inbound_messages` (new collection, Phase 7) | `inbound_messages.capture_inbound()` | `SA_INBOUND_CAPTURE_ENABLED` (no-op when off) |
| Move/add/remove a talent in a project's pipeline | `routers.casting_pipeline.{add_talents_to_pipeline, bulk_move_by_talent_ids, bulk_delete_pipeline}` (existing functions, called unchanged) | flag + signed plan + human confirm |
| Send a WhatsApp message (comm plan or AI reply) | `routers.whatsapp._create_batch_internal` via `whatsapp_send.queue_send()` (existing engine, existing worker) | flag + signed plan + human confirm; AI path additionally requires `SA_AI_RESPONSE_ENABLED` |
| Attach existing talent-library media to a submission | `routers.submissions.attach_existing_talent_media_to_submission` (new function, reachable only from SA) | flag + signed plan + human confirm |
| Upload a NEW audition video (Cloudinary write) | `routers.submissions.ingest_new_audition_media_to_submission` → `core.upload_and_track_asset` (existing Cloudinary pipeline) | flag + signed plan + SHA-256-pinned + human confirm |
| Call an LLM (Gemini or Anthropic) | `ai_response.generate_draft()` → `ai_gemini_client` / `ai.client` | `SA_AI_RESPONSE_ENABLED` (currently `false`) |
| Write an audit row | `simple_assistant.audit.record*()` — a NEW collection, additive only | always on when the module runs; never disables an action |

No other write/send/upload/mutate path exists anywhere in the 25-file backend module (verified by grep for `insert_one`/`update_one`/`delete_one`/`$push`/`$pull`/`upload_and_track_asset`/`_create_batch_internal` across every file — every hit traces to one of the rows above, all inside the `*_execute.py` confirm-time modules or `audit.py`).

---

## 3. Isolation and zero-regression boundaries

| File/area inspected | Modified? | Reused how | Behavior changed? |
|---|---|---|---|
| `backend/ai/client.py` (shared Anthropic seam, AI Scout + Casting Desk) | **No** — `git diff` empty | Imported by `ai_response.py` only when `SA_AI_PROVIDER=anthropic`; `ai_gemini_client.py` re-exports (not redefines) its `LLMError`/`LLMUnavailable` | No |
| AI Scout (`routers/ai_scout.py`, `ai/scout.py`) | **No** | Not imported by Simple Assistant at all | No — 35/36 tests pass, 1 pre-existing unrelated failure (§8) |
| Casting Desk (`routers/casting_desk.py`, `ai/casting_requirement.py`) | **No** | Not imported by Simple Assistant at all | No — 15/15 pass |
| Existing WhatsApp Engine (`routers/whatsapp.py`) | **No** | `_create_batch_internal`, `BatchIn`, `ManualContact`, `SourceParams` imported and called exactly as the existing "Send Casting Call" flow calls them | No — same function, same validation, same job creation |
| WhatsApp worker (`whatsapp-worker/`) | **No** | Not touched at all — jobs land in the same `whatsapp_jobs` collection the worker already polls | No new listener, no new session, no new Playwright process |
| Existing WhatsApp agent routes (`routers/agents_whatsapp.py`) | **Yes, additive only** (§1.1) | One new import + one `try/except`-wrapped call inserted before the existing dispatch call | The existing call, its arguments, and its return value are byte-identical; a Simple Assistant capture failure is swallowed and logged, never surfaces to the caller |
| Existing submission routes (`routers/submissions.py`) | **Yes, additive only** (§1.1) | Two new functions added; zero existing route or function body touched | No — confirmed via full diff read (§1.1) |
| Existing pipeline mutation services (`routers/casting_pipeline.py`) | **No** | `add_talents_to_pipeline`, `bulk_move_by_talent_ids`, `bulk_delete_pipeline`, `PIPELINE_STAGES`, `_normalise_stage` imported and called unmodified | No |
| Existing Cloudinary upload paths (`core.upload_and_track_asset`) | **No** | Called unmodified, through the new `ingest_new_audition_media_to_submission` service, exactly as the talent-facing upload path calls it | No |
| Existing generated-link functionality | **No** | `links_adapter.py` reads via `RDB`/existing link-lookup helpers only, to find a talent's best existing profile link for `SEND_PROFILE_LINK` — never creates a new link | No — Simple Assistant cannot create a generated link |
| Existing admin navigation/routes (`AdminApp.jsx`, `AdminLayout.jsx`) | **Yes, additive only** (§1.1) | One new route, one new conditionally-rendered nav item | No existing route/nav item changed |
| Existing frontend components used outside Simple Assistant | **No** | Simple Assistant's frontend files live entirely under their own directories (`components/simple-assistant/`, `pages-components/SimpleAssistant.jsx`, `hooks/useAssistant*`, `lib/simpleAssistant.js`) and are not imported by any non-SA component | No |

**Monkey patches / global state / startup side effects / route conflicts:** none found. Grepped the entire module for `on_event`, `asyncio.create_task`, `BackgroundTasks`, bare `@app.` decorators, `Thread(`, `asyncio.ensure_future` — zero matches. The module registers exactly one `APIRouter` at import time (the same pattern every other router in `server.py` uses) and performs no other import-time action. Confirmed empirically: importing `server` end-to-end with the flag unset (§8) produces no errors, no extra log lines beyond the normal boot sequence, and no route conflicts (`/api/simple-assistant/*` is a namespace no other router touches).

**Result: additive integration through narrow adapters, not modification of existing workflows** — matches the architectural requirement.

---

## 4. Security audit

| # | Area | Finding |
|---|---|---|
| 1 | Authentication/authorization | Every route requires `current_team_or_admin` — the identical JWT dependency every other admin-plane route uses. No new auth mechanism, no bypass. |
| 2 | Team/tenant isolation | **N/A, not a gap.** Talentgram has no team/tenant concept anywhere in the codebase (confirmed: no `team_id`/`tenant_id` field or filter exists in `core.py` or any router) — it is a single-agency internal tool where every authenticated admin/team user already shares the same data. Simple Assistant introduces no new exposure here; it reuses the same single authorization boundary as the rest of the app. |
| 3 | Server-side resolution of talent/project/submission/destination/media | Confirmed in every `*_execute.py` module: talent (`resolve_authoritative_talent_for_upload`), project, submission, and destination (`comm._talent_own_destination` / `comm._casting_group`, live DB lookups on `whatsapp_group_name`/`phone`/`whatsapp_casting_group_name`) are ALL re-derived from the database at confirm time. The signed plan carries only which resolver to re-run and a previous value to compare for staleness — never a trusted value. |
| 4 | HMAC/signature validation | `security.py` — HMAC-SHA256, key domain-separated from `JWT_SECRET` (`"simple-assistant/context/v1\x00" + JWT_SECRET`), constant-time compare (`hmac.compare_digest`). A missing/malformed/tampered signature makes `_verified_pending()` return `None`, treated identically to "no plan at all." |
| 5 | Expiry handling | **Updated (SA-2, resolved):** `confirm_ai_response`, `confirm_plan`, and `confirm_comm` plans now ALL carry an explicit `expires_at` (30 min), checked before idempotency at entry and rechecked once more immediately before the real mutation/send in `confirm_plan`/`confirm_comm` (defense in depth for the live-DB-resolution gap; `confirm_ai_response`'s own single entry check is unchanged). `confirm_submission`/`confirm_upload` remain without one (explicitly out of scope, confirmed by test), continuing to rely on live-state staleness + idempotency alone — a documented, not accidental, choice. |
| 6 | Stale-plan detection | `execute.py` re-checks each talent's live pipeline stage against the plan's assumed `current_value` before moving it; `comm_execute.py` re-checks the live destination and live submission-media fingerprint (public_id/url) against the plan's snapshot and refuses the whole send on any mismatch; `submission_execute.py` re-checks each proposed library-media id is still on the talent's live library and not already attached; `ai_response_execute.py` recomputes `fact_hash` (verified facts + forbidden set + conversation digest) and refuses on any mismatch. All four independent mechanisms, all live-DB-backed. |
| 7 | Idempotency / replay protection | Every `*_execute.py` module checks `audit.find_completed(plan_id)` before doing anything, and replays the recorded outcome (no re-execution) if the plan already ran. `plan_id` is server-generated (`sap_{uuid4().hex}`) at preview time and bound into the HMAC-signed payload — a client cannot supply or predict one for a plan it didn't get from the server. |
| 8 | Double-approval behavior | Covered by idempotency (item 7) — a second `/command/confirm` with the same signed plan returns "already applied / already sent / already attached," never repeats the mutation or the send. |
| 9 | Client-supplied ID/URL tampering | Every ID (talent, project, submission, media) is re-verified to belong to the resolved talent/project (e.g. `submission_execute.py`'s library-id membership check against a freshly rebuilt `build_prefill_media()` set) before use — a tampered id is rejected as "not found," not silently trusted. |
| 10 | Destination tampering | Confirmed at the code level (§ table above, `comm.py:311-331`) — destination is a **live DB value**, computed fresh from `talent_id`/`project_id`; the plan's own `destination` field is used only as a staleness comparator, never as the send target. |
| 11 | Media URL tampering | `comm_execute.py`'s `SEND_MEDIA` path re-fetches the submission's live media list and compares `public_id`/`url` byte-for-byte against the plan's snapshot; any mismatch refuses the entire send as stale. `submission_execute.py` similarly re-verifies every proposed source-media id against the talent's live library. |
| 12 | Prompt injection | `ai_response.py`'s system prompt explicitly separates `SYSTEM_INSTRUCTIONS` / `VERIFIED_TALENTGRAM_FACTS` / `RECENT_CONVERSATION_CONTEXT` / `UNTRUSTED_INBOUND_MESSAGE`, instructs the model to treat the inbound message and conversation history as data never instructions, and to refuse any embedded attempt to change its behavior. This is backstopped by the **deterministic, non-LLM** `validate_draft()` — every draft is checked with regex-based unsupported-amount/date/commitment/topic detectors regardless of what the model claims. Live-proven resistant (case F, real Gemini call: injected instruction ignored, `validate_draft()` passed). |
| 13 | Hidden-budget leakage | Two independent layers: (a) `ai_response.build_context()` never places the raw hidden value in `verified_facts`, and the user prompt substitutes `"HIDDEN — must not be disclosed"`; (b) `validate_draft()` regex-scans for any money pattern when `"budget" in forbidden_facts` and separately checks the literal hidden value against every number in the draft. Live-proven: `live_hidden_leak: False` on case E's real successful call. Case I (hidden budget + conflicting history) has never completed a live call to prove this combined scenario end-to-end — flagged in §5/§9, not a code defect. |
| 14 | Conversation-history leakage | `conversation_context.py` — bounded by count (`SA_CONVERSATION_HISTORY_LIMIT`, capped at 20) and time window (`SA_CONVERSATION_HISTORY_HOURS`, capped at 720h); strictly project-scoped (`no_resolved_project` → empty context, never an unscoped history dump); every historical line has amount tokens redacted via the same `_MONEY_RE` pattern used by the validator, before it can reach the prompt, whenever the project has `hide_budget_from_talent` set. `render_for_prompt()` strips everything except `{from, text}` — no ids, no phone numbers, no internal metadata ever reach the model. |
| 15 | Cross-talent/cross-project leakage | `readonly_db` queries throughout are scoped by the resolved `talent_id`/`project_id`; `conversation_context.build()` explicitly refuses to return any history when the project cannot be resolved (§ item 14); `attach_existing_talent_media_to_submission`'s candidate pool is `build_prefill_media(talent)` for the ONE resolved talent — a requested id belonging to a different talent is rejected as "not found." |
| 16 | Sensitive data in logs/audit records | `audit.py` stores only ids, hashes (`draft_hash`, `fact_hash`), safe metrics (model name, provider name, latency, validation verdict) — the docstring on `record_ai()` states explicitly "NEVER the prompt, provider key, or raw model output," confirmed by reading every field written. `security.py`/`ai_gemini_client.py` never log the JWT secret or the provider API key (confirmed across all Gemini live-validation checkpoints — every credential-scrubbing check performed on saved reports found zero matches, every time). |
| 17 | API-key handling | `GEMINI_API_KEY`/`ANTHROPIC_API_KEY` are read only from `os.environ` at call time; never written to a file, never included in `config_snapshot()`'s output (only `api_key_present: bool` and `key_env_var: str` are exposed), never logged. |
| 18 | Error-message leakage | Provider errors are truncated to 300 chars and surfaced only as generic "the AI provider is not configured/failed/timed out" reasons to the caller — no stack trace, no internal path, no key fragment. |
| 19 | Race conditions between preview and approval | Handled by the combination of idempotency (item 7) + live re-verification (item 6) + atomic Mongo guards on every write (`{"media.content_sha256": {"$ne": sha}}`, `{"media.source_talent_media_id": {"$ne": id}}`) — a concurrent double-fire of the same confirm is rejected at the database level (`matched_count == 0` → treated as "already done"), not just at the application level. |

**No Blocker or High findings.** Two Low findings (SA-1, SA-2) are detailed in the findings table (§9-adjacent, listed at the end of this report) — neither is exploitable as-is; both are recommended fixes before committing for clarity/consistency, not because current behavior is unsafe.

---

## 5. AI/Gemini audit

| Property | Verified state |
|---|---|
| Provider-selection mechanism | `SA_AI_PROVIDER` env var, read live on every call via `_current_provider()` (never cached at import time — deliberately, to avoid cross-test-file leakage); default `"gemini"` |
| Model default | `gemini-3.6-flash` (`_DEFAULT_MODEL_BY_PROVIDER["gemini"]` in `ai_response.py`, and `DEFAULT_MODEL` in `ai_gemini_client.py` — both re-confirmed identical this audit) |
| Gemini thinking configuration | `thinking_config=types.ThinkingConfig(thinking_budget=0)` — confirmed present in `ai_gemini_client.py`'s `GenerateContentConfig`, unchanged since the 2026-09-15 compatibility fix |
| Strict JSON/schema handling | `response_mime_type="application/json"` + `response_json_schema=input_schema` on the request; `json.loads()` (strict, no markdown-fence stripping — none has ever been observed live) on the response; a non-dict or unparseable result raises `LLMError` |
| Timeout behavior | `asyncio.wait_for(..., timeout=_TIMEOUT_SEC)` in `ai_response.generate_draft()` — `SA_AI_TIMEOUT_SEC=45`, independent of any SDK-internal timeout, applies to either provider identically |
| Provider failure handling | `LLMUnavailable`/`LLMError`/`asyncio.TimeoutError`/generic `Exception` are each caught distinctly and returned as a structured `{"ok": False, "error": ..., "reason": ...}` — never raised uncaught, never silently retried |
| No unintended fallback to another provider | Confirmed — `_provider_client()` selects exactly one module per call based on `SA_AI_PROVIDER`; there is no code path that tries a second provider on the first one's failure |
| No automatic retries that could consume quota | Confirmed by code (`generate_draft()`'s own docstring: "One controlled generation attempt... no retry loop here") and by every live-validation checkpoint to date — no call has ever been automatically retried; every 429/503 has been reported and left alone |
| No API-key logging | Confirmed (§4 item 17) |
| No prompt/content logging | Confirmed (§4 item 16) |
| `validate_draft()` remains authoritative | Confirmed — every generation path (self-check, real `ai_response_execute.py`) runs the SAME deterministic `validate_draft()` before a draft can be approved or (re-)validates the final text again at send time |
| Human approval remains mandatory | `needs_review: True` is hard-coded on every draft (`generate_draft()`); nothing reaches WhatsApp without an explicit `/command/confirm` with `kind == "confirm_ai_response"`, itself gated by the same flag + auth as every other action |
| AI cannot directly send WhatsApp messages | Confirmed — `ai_response.generate_draft()` returns text only; sending is a separate, later, human-triggered call into `ai_response_execute.confirm_and_send_ai()`, which itself re-validates before calling `whatsapp_send.queue_send()` |
| AI cannot directly mutate submissions, pipeline state, or business data | Confirmed — the AI-response code path (`ai_response.py`, `ai_gemini_client.py`) has no import of, or call into, any DB write function, `routers.casting_pipeline.*`, or `routers.submissions.*` |
| AI receives only the intended scoped context | Confirmed — `build_context()` builds a plain dict from server-resolved facts; the model is never given a DB handle, a tool, or a dereferenceable id |
| Conversation history is explicitly non-authoritative | Confirmed both in the system prompt text (§4 item 12) and structurally — `fact_hash()` binds the conversation digest so a changed history invalidates a pending plan, but the validator itself never trusts history over `verified_facts` |
| Hidden budget remains excluded/redacted | Confirmed (§4 item 13) |
| Edited drafts are revalidated | Confirmed — `ai_response_execute.py` runs `validate_draft()` against `final_text` (the edited text if the human changed it) before it can be approved, never against the original unedited draft |
| Approval rebuilds current facts and checks stale state | Confirmed — `confirm_and_send_ai()` re-runs `sa_intel.analyze()`, `sa_convo.build()`, and `ai_response.build_context()` from scratch at approval time and compares the resulting `fact_hash` against the one signed at draft time |

### Case I live-validation limitation — documented explicitly, not minimized

**The system is NOT fully live-verified.** 8 of 9 self-check cases (A–H) have live evidence from a real Gemini call with the current compatibility fix, all with correct safety outcomes (0 hidden-budget leaks, 0 history-figure adoptions, injection correctly resisted, continuity context correctly treated as non-authoritative). **Case I — hidden budget combined with a conflicting conversation-history figure, the single highest-risk combined scenario in the suite — has been attempted four times across three separate checkpoints (2026-09-15; 2026-09-17 ×3) and has never once completed a live generation.** Every attempt failed with a real, external provider condition (quota exhaustion, then two consecutive `503 high demand` responses) — never a validator, grounding, or safety failure, since the provider never returned a response to evaluate. Current classification, carried forward unchanged from the last validation checkpoint: `PARTIALLY VERIFIED — 8 OF 9 CASES LIVE-CONFIRMED (A–H); CASE I NOT VERIFIED — PROVIDER LIMITATION`. This audit does not treat the system as fully live-verified, and recommends `SA_AI_RESPONSE_ENABLED` stay `false` until Case I completes.

---

## 6. Production side-effect audit

| Path | Read-only | Preview-only | Explicitly approved mutation/send | Disabled/unreachable |
|---|---|---|---|---|
| Create WhatsApp jobs | | | ✅ only via `whatsapp_send.queue_send()` at confirm time | 404 unless `SIMPLE_ASSISTANT_ENABLED` |
| Create WhatsApp batches | | | ✅ (same as above — one batch per queued send) | 404 unless flag on |
| Send WhatsApp messages | | | ✅ only after signed-plan verify + live re-resolve + human confirm | 404 unless flag on |
| Upload to Cloudinary | | | ✅ only via `confirm_and_ingest()` → `ingest_new_audition_media_to_submission` → `core.upload_and_track_asset`, SHA-256-pinned | 404 unless flag on |
| Modify submissions | | | ✅ only via `confirm_and_attach()`/`confirm_and_ingest()` | 404 unless flag on |
| Modify talent records | ✅ (never written — no talent-record write path exists anywhere in the module) | | | — |
| Modify project records | ✅ (never written — no project-record write path exists anywhere in the module) | | | — |
| Modify pipeline status | | | ✅ only via `execute.py`'s three canonical pipeline functions | 404 unless flag on |
| Create generated links | ✅ (`links_adapter.py` only reads existing links; no link-creation call exists) | | | — |
| Write audit records | | | ✅ (always, whenever the module runs an action — this is intentional, an audit trail, not a mutation of business data) | — |
| Trigger background workers | ✅ — Simple Assistant queues a job into the same collection the existing worker already polls; it does not itself launch, ping, or control any worker process | | | — |
| Call Gemini | | ✅ (`/command` preview path never calls the LLM; only `ai_response_execute.py`'s confirm path does, and only when the draft itself was already generated during preview) | | Gated by `SA_AI_RESPONSE_ENABLED=false` (currently off) |
| Change existing Talentgram behavior | ✅ — confirmed empirically (§8): zero behavior difference in any existing route/function when the flag is off, which is the current production state | | | — |

**Every mutating/sending row requires: `SIMPLE_ASSISTANT_ENABLED=true` AND a valid HMAC-signed plan AND an explicit human `/command/confirm`.** With the flag at its current production value (`unset` → `false`), the entire mutating/sending column is unreachable — the whole module is `GET`/`POST` 404 end to end.

---

## 7. Feature-flag and deployment audit

| Check | Result |
|---|---|
| Master flag (`SIMPLE_ASSISTANT_ENABLED`) defaults disabled | ✅ `is_enabled()` in `simple_assistant/__init__.py` defaults `"false"` — confirmed by reading the code AND by an empirical `TestClient` request (§8): every route 404s with the flag unset |
| AI response flag (`SA_AI_RESPONSE_ENABLED`) defaults disabled | ✅ `ai_response.is_enabled()` defaults `"false"`; also confirmed unset in Railway production via `railway variables -s talentgram-railway -e production` (checked repeatedly across the Gemini validation checkpoints, most recently this same day) |
| AI generation cannot occur when the flag is disabled | The self-check harness (`ai_selfcheck.py`) calls `ai_response.generate_draft()` directly for controlled testing purposes and does not itself gate on `SA_AI_RESPONSE_ENABLED` — this is by design (it is a validation-only tool, never reachable from any HTTP route). The one HTTP-reachable AI path, `POST /command` → `commands.py`'s AI-draft branch, is only exercised when Phase 8/9's inbound-intelligence pipeline is active, which itself sits behind `SIMPLE_ASSISTANT_ENABLED` (route-level 404) — and `ai_response.is_enabled()` is additionally checked before a generation attempt is made in that live path. |
| Outbound sending cannot occur without explicit approval | ✅ confirmed throughout §4/§5/§6 — every send path requires a signed plan + `/command/confirm` |
| No startup process automatically launches a worker or listener | ✅ confirmed by grep (§3) — zero `on_event`/background-task/thread patterns anywhere in the module |
| No second WhatsApp listener/session was introduced | ✅ confirmed — `whatsapp_send.py`'s own docstring and this audit's code read both confirm every send goes through the existing `_create_batch_internal`/`whatsapp_jobs`/existing-worker path; inbound capture (§2) hooks the EXISTING inbound webhook endpoint, adding a write, not a new listener |
| Deployment packaging includes all required untracked files | Once committed, yes — Railway's Nixpacks builder picks up the entire repo tree; there is no separate include/exclude list to configure for Python source files. Vercel similarly builds from the full `frontend/` tree. |
| Required Python dependencies declared correctly | ✅ `google-genai==1.71.0` already in `requirements.txt` (§1.6) |
| No local-only path or scratch wrapper required in production | ✅ confirmed — every scratch wrapper script used during live Gemini validation (`railway run ... python3 <scratch-only wrapper>`) lives outside the repository (in the session's scratchpad directory) and exists solely to inject a real credential into a LOCAL test process; production itself reads `GEMINI_API_KEY`/`ANTHROPIC_API_KEY` directly from its own Railway environment, no wrapper involved |
| Railway and Vercel environment expectations documented | `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY` if `SA_AI_PROVIDER=anthropic`) must be set on the `talentgram-railway` service for the AI-response feature to function once enabled; `SIMPLE_ASSISTANT_ENABLED`/`SA_INBOUND_CAPTURE_ENABLED`/`SA_INBOUND_INTELLIGENCE_ENABLED`/`SA_AI_RESPONSE_ENABLED`/`SA_CONVERSATION_CONTEXT_ENABLED` are all optional, all default OFF; `NEXT_PUBLIC_SIMPLE_ASSISTANT_ENABLED` must be set on the Vercel frontend project to reveal the nav entry (the route itself additionally self-redirects when this is unset, §1.6). None of these need to be set for the code to be safely committed and deployed in its current, fully-dormant state. |

---

## 8. Test and build verification

### Original audit pass

| Suite | Result |
|---|---|
| Simple Assistant backend tests (`pytest tests/test_simple_assistant*.py`, 18 files, one process) | **290 passed, 3 skipped** (skips are the opt-in real-provider tests, intentionally not run without `SA_AI_SELFCHECK_LIVE=1`) |
| AI Scout (`test_ai_scout.py`, isolated) | 35 passed, **1 pre-existing, unrelated failure** — `test_call_tool_json_surfaces_provider_error_message` fails on `ModuleNotFoundError: No module named 'httpx2'`, a typo in that test file's own import, nothing to do with Simple Assistant |
| Casting Desk (`test_casting_desk.py`, isolated) | 15 passed |
| Management Agent (`test_management_agent.py`, isolated) | 34 passed |
| Casting Agent / WhatsApp Scouting Agent (`test_casting_agent.py`, isolated) | 287 passed, **36 pre-existing, unrelated failures** — every one inspected traces to the literal error `"Worker 'default' is not enabled for sending. An admin must explicitly enable sending for this worker first."`, i.e. the existing WhatsApp-worker sending-permission gate added in the current `HEAD` commit `3aa3d6a` ("Add explicit WhatsApp worker sending permission gate", unrelated to and predating any Simple Assistant work) |
| Frontend (`vitest run`, full suite) | **439 passed** (34 test files) |
| Production frontend build (`npm run build`) | **Succeeded** — compiled cleanly, all 16 app routes generated, confirms the `@/pages/SimpleAssistant` alias resolves correctly (§1.6) |
| Empirical flag-off check | Imported `server.py` end-to-end with `SIMPLE_ASSISTANT_ENABLED` unset (the current production default) and issued live `TestClient` requests: `GET /api/simple-assistant/overview` → `404`, `GET /api/simple-assistant/health` → `404`. No import error, no startup error, no unexpected log output. |

**No new failures anywhere.** Both sets of pre-existing failures (AI Scout's `httpx2` typo, Casting Agent's sending-permission-gate assertions) were individually inspected this audit and their exact error text confirms they are unconnected to Simple Assistant, not merely assumed unrelated by pattern-matching against memory.

### Re-verification after SA-1 / SA-2 (this update)

| Suite | Result |
|---|---|
| Simple Assistant backend tests (full, `pytest tests/test_simple_assistant*.py`) | **295 passed, 3 skipped** — up from 290 by exactly the 5 new SA-2 tests; zero regressions |
| `test_simple_assistant.py::test_feature_flag` (SA-1's own coverage) | passed |
| `test_simple_assistant_execute.py` (confirm_plan / confirmation / execution) | **15 passed** (was 12; +3 new SA-2 tests) |
| `test_simple_assistant_comm.py` (confirm_comm / communication execution) | **25 passed** (was 23; +2 new SA-2 tests) |
| `test_simple_assistant_ai_response.py` (confirm_ai_response approval flow) | passed, unchanged — including `test_unsigned_and_tampered_and_expired_rejected` and `test_idempotent_double_approval`, confirming `confirm_ai_response`'s own expiry/idempotency behavior is byte-for-byte unaffected |
| `test_simple_assistant_submission.py`, `test_simple_assistant_send.py` (confirm_submission / confirm_upload) | passed, unchanged — confirming the explicitly-out-of-scope kinds gained no new behavior |
| AI Scout (isolated) | 35 passed, same 1 pre-existing unrelated failure |
| Casting Desk (isolated) | 15 passed |
| Management Agent (isolated) | 34 passed |
| Frontend / build | Not re-run — SA-1 and SA-2 touched only Python files under `backend/simple_assistant/` and `backend/tests/`; no frontend file was changed, so the frontend suite and build are unaffected by this update |

**Invariants explicitly re-confirmed by name** (per this task's verification checklist): `test_expired_plan_refuses_even_with_a_valid_signature` and `test_expired_comm_plan_refuses_even_with_a_valid_signature` (expired plans cannot execute — tested against a *genuinely* expired, validly re-signed context, not just a tampered one) · `test_unsigned_or_missing_context_rejected`, `test_arbitrary_client_action_cannot_be_injected`, `test_unsigned_plan_rejected`, `test_tampered_*` (×5 across execute/comm/submission/send) (invalid signatures remain rejected) · `test_stale_single_plan_refuses` (stale plans remain rejected) · `test_replay_same_plan_does_not_double_apply`, `test_replay_does_not_send_twice`, `test_idempotent_double_approval`, `test_replay_no_duplicate`, `test_replay_creates_no_second_job_or_batch` (duplicate approvals remain idempotent, across all five confirm kinds).

**Side-effect confirmation for this update's test run:** every test in the re-run suite (295 SA tests) uses an in-memory `FakeDB`/`FakeColl` and a stubbed `routers.whatsapp._create_batch_internal` (`_BATCH_CALLS`, an in-process list, never a real HTTP/WhatsApp call) — the same pattern every prior checkpoint in this workstream has used. `test_expired_comm_plan_refuses_even_with_a_valid_signature` explicitly asserts the count of *real* (non-dry-run) batch calls is unchanged by the expired confirm attempt. No real Mongo, no real Cloudinary, no real WhatsApp, no real Gemini/Anthropic call was made anywhere in this verification pass — confirmed by the test infrastructure itself, not merely assumed. `git status` (checked again after these edits) shows the same 6 pre-existing tracked-file diffs as the original audit and no new commits — the SA-1/SA-2 changes are confined entirely to files that were already untracked.

---

## 9. Findings (all severities)

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| SA-1 | **Low** | `backend/simple_assistant/__init__.py`'s module docstring stated the flag "default[s] on," contradicting the actual code default of off. | **FIXED.** One-line docstring correction; no behavior change; existing `test_feature_flag` (already passing) already covered the real behavior. |
| SA-2 | **Low** | `confirm_plan`/`confirm_comm` carried no explicit time-based expiry, unlike `confirm_ai_response`'s 30-minute `expires_at`. | **FIXED.** Explicit 30-minute `expires_at` added to both, via the single shared `_ctx()` signing helper; checked at entry and rechecked immediately before the real mutation/send; 5 new tests added and passing; zero regression across the full suite. `confirm_submission`/`confirm_upload` deliberately left unchanged (out of scope), confirmed by a dedicated test. |
| SA-3 | **Informational** | Case I of the Gemini self-check battery has no live evidence after four attempts (§5). Not a code or safety defect — the provider has never returned a response to validate. | **Unchanged, carried forward.** Do not enable `SA_AI_RESPONSE_ENABLED` in production until Case I completes with its expected safety outcome; retry it in isolation (`SA_AI_SELFCHECK_CASES=I`) when there's reason to expect better provider availability. |
| SA-4 | **Informational** | Several untracked files in the working tree (`backend/migrations/reports/*`, multiple `docs/*.md`) belong to other, unrelated workstreams (§1.3). | **Unchanged, carried forward.** Exclude explicitly from this commit's `git add`; do not delete them (they may be someone else's in-progress work). |

No Blocker, High, or Medium findings, before or after this update.

---

## Commit-readiness decision (detail)

```
READY FOR SCOPED COMMIT
```

Both Low findings are now resolved and verified. This module is ready for a scoped commit in its current, fully-flag-dormant state.

### Exact files to include

**Tracked (already modified, include as-is):**
```
backend/core.py
backend/routers/agents_whatsapp.py
backend/routers/submissions.py
backend/server.py
frontend/src/components/AdminApp.jsx
frontend/src/components/AdminLayout.jsx
```

**Untracked, to `git add` (56 files):**
```
backend/simple_assistant/                          (25 .py files — SA-1/SA-2 fixes already applied)
backend/tests/test_simple_assistant*.py             (17 files)
frontend/src/components/simple-assistant/           (8 files)
frontend/src/hooks/useAssistantCommand.js
frontend/src/hooks/useAssistantOverview.js
frontend/src/hooks/useAssistantOverview.test.js
frontend/src/lib/simpleAssistant.js
frontend/src/pages-components/SimpleAssistant.jsx
frontend/src/pages-components/SimpleAssistant.test.jsx
docs/SIMPLE_ASSISTANT_GEMINI_COMPATIBILITY_REPORT.md
docs/SIMPLE_ASSISTANT_GEMINI_FINAL_VALIDATION_REPORT.md
docs/SIMPLE_ASSISTANT_GEMINI_LIVE_VALIDATION_REPORT.md
docs/SIMPLE_ASSISTANT_GEMINI_MIGRATION_REPORT.md
docs/SIMPLE_ASSISTANT_PHASE11_COMPLETION_REPORT.md
docs/SIMPLE_ASSISTANT_PHASE12_COMPLETION_REPORT.md
docs/SIMPLE_ASSISTANT_PHASE13_COMPLETION_REPORT.md
docs/SIMPLE_ASSISTANT_PRE_COMMIT_SECURITY_AUDIT.md   (this file)
```

`backend/simple_assistant/__pycache__/` should NOT be added (bytecode cache — confirm it's covered by `__pycache__/` in `.gitignore`, which it is, §1.5).

### Exact files to exclude

```
backend/migrations/reports/*.json
backend/migrations/reports/*.txt
docs/ADR_CANONICAL_PROFILE_OWNERSHIP.md
docs/CANONICAL_ARCHITECTURE_REDESIGN.md
docs/CANONICAL_DATA_DICTIONARY.md
docs/CLOUDINARY_P3_COMPLETION_REPORT.md
docs/CONSISTENCY_REVIEW_ADR_CDD.md
docs/IMPLEMENTATION_ROADMAP_V1.md
docs/MANAGEMENT_AGENT_COMMAND_COVERAGE.md
docs/PHASE4_INDEPENDENT_PRODUCTION_AUDIT.md
docs/REVERIFICATION_ISSUE1_ISSUE2.md
docs/TALENT_DASHBOARD_MIGRATION_HANDOFF.md
docs/cloudinary_b_196_examine.csv
```
(all unrelated pre-existing working-tree state, §1.3 — leave untouched, do not delete)

### Required dependency/configuration changes

None. `google-genai` is already pinned; no frontend package is new; no `.gitignore` change is needed.

### Unrelated diffs that must remain untouched

None of the 6 tracked-modified files contain any unrelated diff — every line in all 6 is Simple Assistant integration (§1.1, full diffs read). No other tracked file should be touched by this commit.

### Proposed commit message

```
feat: add Simple Assistant module (flag-gated, off by default)

Adds an isolated, additive JARVIS-style inbound/command assistant for
the admin console: deterministic command parsing, a read-only
situational overview, HMAC-signed preview→confirm plans for pipeline
moves, WhatsApp sends, submission media attach/ingest, and a
human-approved AI (Gemini, with Anthropic fallback) WhatsApp reply
draft — always through existing canonical services (casting_pipeline,
whatsapp._create_batch_internal, submissions upload/attach,
upload_and_track_asset), never a new write/send path.

Gated end-to-end by SIMPLE_ASSISTANT_ENABLED /
NEXT_PUBLIC_SIMPLE_ASSISTANT_ENABLED / SA_AI_RESPONSE_ENABLED, all
default off — every /api/simple-assistant/* route 404s and the nav
entry is hidden until explicitly enabled. Zero regression confirmed
against the full existing test suite.
```

### Deployment checklist

- [x] Apply the SA-1 docstring fix in `backend/simple_assistant/__init__.py` — done, verified
- [x] Apply the SA-2 explicit-expiry fix to `confirm_plan`/`confirm_comm` — done, verified
- [ ] `git add` exactly the file list above
- [ ] Confirm `SIMPLE_ASSISTANT_ENABLED`, `SA_INBOUND_CAPTURE_ENABLED`, `SA_INBOUND_INTELLIGENCE_ENABLED`, `SA_AI_RESPONSE_ENABLED`, `SA_CONVERSATION_CONTEXT_ENABLED`, `NEXT_PUBLIC_SIMPLE_ASSISTANT_ENABLED` remain unset (or explicitly `false`) in both Railway and Vercel before/at deploy time
- [ ] Deploy as normal — no flag change, no new environment variable is required for this commit to deploy safely dormant
- [ ] After deploy, spot-check `GET /api/simple-assistant/health` on production returns `404` (confirms the flag state matches intent)
- [ ] Do not enable any Simple Assistant flag in production without a separate, explicit go-ahead
- [ ] Do not enable `SA_AI_RESPONSE_ENABLED` until Case I (SA-3) completes with its expected safety outcome

---

**STOP.** This update resolved the two Low findings (SA-1, SA-2) the original audit surfaced — a one-line docstring correction, and an explicit 30-minute expiry added to `confirm_plan`/`confirm_comm` via the single shared signing helper, with 5 new tests and a full zero-regression re-run (295/3 skip). No new functionality was added, no existing Talentgram workflow, AI Scout, Casting Desk, WhatsApp worker, or shared AI infrastructure file was touched, no existing security check was weakened, `SA_AI_RESPONSE_ENABLED` remains `false`, no flag was enabled, nothing was deployed, no WhatsApp message was sent, and no production data was touched. Phase 14 and any commit/push/deploy/flag-enable action remain for a separate, explicit follow-up.
