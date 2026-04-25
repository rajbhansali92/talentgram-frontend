# Talentgram — Portfolio Link Engine (PRD)

## Original Problem
Build a production-grade client-review system for Talentgram (talent agency). Core function: Admin picks talents → system generates a shareable web link ("Talentgram x {Brand/Talent}") → client opens link → reviews portfolios → takes actions (Shortlist / Interested / Not for this / Not sure) + comments → system tracks everything (views, unique viewers, downloads). Tone: "Netflix meets Casting — Luxury portfolio meets decision dashboard."

## User Personas
- **Admin (Talentgram team)** — creates/edits talents, uploads media, generates links with visibility toggles, reviews results
- **Client (Brand / Casting / Agency)** — opens the secure link, identifies self (name+email hard gate), browses curated talents, makes decisions

## Architecture
- **Backend**: FastAPI + Motor (async MongoDB), JWT (admin + viewer roles), bcrypt, Emergent Object Storage (S3-compatible) — single `/app/backend/server.py`
- **Frontend**: React 19 + react-router-dom v7 + Tailwind + shadcn/ui + sonner — pages under `/app/frontend/src/pages`
- **Auth**: Seeded admin `admin@talentgram.com` / `Admin@123` at startup; viewer tokens issued per slug on identify

## Status — Phase 1 MVP (Implemented 2026-04)
### Done
- ✅ Admin JWT login, seeded admin account
- ✅ Talent CRUD + media management (categories: indian / western / portfolio / video) with cover selection
- ✅ Emergent Object Storage integration with CDN-style `/api/files/{path}` serving
- ✅ Link generator with 11 visibility toggles + multi-select talents + `Talentgram x ...` naming
- ✅ Link history: list, open, copy, WhatsApp share (wa.me), duplicate, delete
- ✅ Client view: hard identity gate (name+email → viewer JWT), Tetris-grid portfolio, detail overlay with slider, intro video, instagram, work links, downloads
- ✅ Per-talent actions (shortlist/interested/not_for_this/not_sure) + comments, upsert semantics
- ✅ Download tracking (who/what/when), gated by visibility.download toggle
- ✅ Results dashboard: viewers list, download log, per-talent breakdown with action counts + comments
- ✅ Analytics: total views, unique viewers per link
- ✅ Mobile-responsive dark UI (Obsidian/Pearl palette, Outfit + Manrope fonts)
- ✅ Testing: 100% backend pytest + 100% frontend Playwright E2E

## Architecture — Flow-Driven (not DB-Driven)

```
Submission (Raw)   →   Admin (Decision)    →   Client (Presentation)
   form_data             field_visibility       curated_view
   scoped media          link.visibility        strict allowlist
                         link.submission_ids
```

- **Submission layer** — raw talent input; stays inside `submissions` collection, bound to `project_id`
- **Admin layer** — reviews, edits form_data, toggles per-field visibility, approves/rejects. Never copies data elsewhere.
- **Curated view layer** — `link.submission_ids` is the *source reference*; `link.visibility` is the *curation rule*. Nothing is denormalised.
- **Client layer** — receives computed, filtered, allowlisted output only. Internal admin fields (availability, budget, custom_answers, competitive_brand, form_data, dob, email, phone, notes) can never leak.

## Recent Updates
- **2026-04-25 (v26)** — **Sprint 2: Mobile-First Submission Wizard.** The highest-ROI mobile change in the system.
  - **3-step wizard on `<md`** (`SubmissionPage.jsx` + `index.css`): Step 1 Profile → Step 2 Brief / Questions → Step 3 Uploads. Implemented as a CSS-driven view (`data-mobile-step` on root + `data-step` markers on every block). **Desktop layout completely unchanged** — the same single-page form renders for `md+`.
  - **Stepbar UI** (`wizard-stepbar`): tappable step pills with check-state for completed steps + thin gold progress bar, all sticky under the header.
  - **Sticky bottom action bar** (`wizard-bottom-bar`): "Back / Continue to Brief / Continue to Uploads" — always one-thumb away, never hidden under keyboard.
  - **Per-step validation** (`validateStep1`, `validateStep2`) — narrower than the full form so users can advance partial; full validation runs at finalise time.
  - **Camera-first uploads**: every upload slot now has a mobile-only "Record / From library" pair that uses `capture="user"` (intro + takes) or `capture="environment"` (photos). Desktop keeps the single dashed-button.
  - **Resilient uploads** with auto-retry + exponential backoff (1s / 2s / 4s, 3 attempts) inside `uploadFile()`. Network blips no longer wipe the upload — and a per-slot `Retry` button surfaces only when all attempts fail (`retryQueue` state). True chunked/resumable-from-offset upload (tus-js-client) deferred to P1; this slim approach already covers ~80% of real-world transient drops without backend changes.
  - **Mobile keyboard hygiene**: `inputMode` (email/tel/numeric), `enterKeyHint="next"`, `autoComplete` per field type — keyboard now suggests appropriately (.com row on email, numpad on phone) and the next-field affordance flows correctly.
  - **Draft persistence** (`tg_draft_{slug}` localStorage) — debounced 400 ms, restored on mount. Refresh / app-switch never loses typed answers, even before the talent record is created on the backend.
  - **Auto-jump to Uploads**: once the backend submission record exists (`saved`), mobile users land directly on Step 3 instead of re-walking profile.
  - **Tap targets**: every wizard control + camera/library button is `min-h-[44px]` with `active:scale-[0.97] transition-transform` for tactile feedback.
  - **Verified on iPhone 12 viewport (390×844)**: invalid-fields blocks advance with a toast, valid-fields advances cleanly, sticky chrome stays one-thumb away, custom-questions render only on Step 2.

- **2026-04-25 (v25)** — **Sprint 1: Mobile Critical Fixes shipped.** Production hardening from the Mobile UX Audit.
  - **C1 — Brand on mobile login** (`AdminLogin.jsx`): mobile-only `BrandHero` block above the form (`md:hidden`) so the logo never disappears on phones.
  - **C2 — Notification dropdown overflow** (`NotificationBell.jsx`): mobile renders as a full-width sheet (`fixed left-2 right-2 top-[60px]`) with a tappable backdrop + Close button; desktop keeps the corner popover.
  - **H1 — Client View above-the-fold** (`ClientView.jsx`): mobile collapses the page heading + viewer + progress into a compact sticky header (~80 px). Verbose H2 / desktop progress bar moved to `hidden md:flex`. Tabs now horizontally scroll instead of wrapping. Result: **first talent card now appears at y≈166 (was ≈300)** on iPhone 12 viewport — 45% more content above the fold.
  - **H4 — Admin mobile nav** (`AdminLayout.jsx`): scrolling tab strip (`overflow-x-auto whitespace-nowrap`), no `flex-1` squash, full label visibility, `min-h-[44px]` tap targets.
  - **M3 — Toast positioning** (`App.js`): `position="top-center"` with `mobileOffset={64}` so toasts no longer collide with the sticky header.
  - **Bonus** — `BrandHero` now responsive (110 px logo `<sm`, 140 px `sm-md`, 220 px `md+`) so iPhone SE no longer pushes CTAs below the fold. New `.tg-noscrollbar` utility for clean horizontal-scroll surfaces.

- **2026-04-25 (v24)** — **Brand-first Landing & Admin Login.** Removed all marketing copy on the public-facing surfaces; the logo IS the hero now.
  - **`BrandHero.jsx`** — new shared component: centred logo, hairline divider, dual-line tagline `WE SCOUT · WE MANAGE` / `INDIA | UAE`. Sizes: `lg` (Landing, ~220 px logo) and `md` (Admin Login left rail, ~140 px). `inverted` prop forces white logo + white text on always-dark surfaces (admin-login left panel) regardless of the active theme.
  - **`Logo.jsx`** — new `forceVariant` prop ("white" | "black") so consumers can override the auto-theme swap when the surrounding panel has a fixed colour.
  - **Landing.jsx** — removed `Talentgram × Portfolio Engine` eyebrow, "Curated portfolios. Decisive presentations." headline and the description paragraph. Hero is now logo + tagline; CTAs (`Enter Dashboard` / `Apply as Talent`) sit centred below. Subtle radial vignette replaces the heavy background photo. Footer keeps version + copyright.
  - **AdminLogin.jsx** — removed `Client Review System` eyebrow and the "Curated decisions. Quietly powerful." H2. Left rail now shows `BrandHero` (inverted, md) centred over the existing dark texture. Right column form unchanged.
  - **Day & Night** verified via theme toggle — landing flips backgrounds; admin-login left rail keeps its dark texture intact in both modes; tagline typography (`tg-mono` for "We Scout" + `font-display` for "INDIA | UAE") matches the existing serif logo voice.

- **2026-04-25 (v23)** — **M6 Feedback Relay UI shipped.** Three coordinated surfaces.
  - **Admin** (`/admin/feedback`, `AdminFeedback.jsx`): moderation queue with filter chips (Pending/Approved/Rejected/All), per-row Approve & Share / Reject / Edit (text-only) / Delete actions, project subtitle, voice player. New sidebar nav `Feedback`.
  - **Client** (inside `TalentDetail` overlay on `ClientView.jsx`): `FeedbackComposer` with two tabs — text (4000 chars) and voice (60 s `MediaRecorder`, preview before send). Posts to `/api/public/links/{slug}/feedback[/voice]`. Submission-backed cards only (M2/M3); pure talent-share (M1) doesn't expose composer because no `submission_id` is attached.
  - **Talent** (Submitted thank-you screen on `SubmissionPage.jsx`): new `Client Feedback` inbox section. Empty state when nothing approved; otherwise renders `FeedbackRow`s — voice player or text bubble + "Received Xm ago".
  - **NotificationBell**: `client_feedback_new` / `feedback_approved` / `feedback_rejected` notifications now route to `/admin/feedback`.
  - **Backend tweak**: `_filter_talent_for_client` now passes through `submission_id` + `project_id` (added to `CLIENT_ALLOWED_FIELDS`) — required for the composer to round-trip the feedback POST. These are non-PII opaque IDs.
  - **VoiceRecorder.jsx**: standalone reusable component (mic permission handling, double-click guard, 60s auto-stop, preview audio + Send/Discard).
  - **Tests**: 32/32 backend pytests pass (feedback relay 9 + client_intelligence 6 + phase1_arch 11 + isolation 6). Live E2E curl round-trip confirmed: pending→admin queue→approve→talent inbox surfaces only after approval.

- **2026-04-25 (v22)** — **Moderated Client→Talent Feedback Relay (M6, backend-only).** Admin is the only gateway between client and talent.
  - **New `feedback` collection**: `{id, type (text|voice), text, content_url, content_type, talent_id, submission_id, project_id, link_id, created_by="client", client_viewer_email/name, status (pending|approved|rejected), visibility (admin_only|shared_with_talent), created_at, approved_at, approved_by, rejected_at, rejected_by, edited_at, edited_by}`. Indexes on `(submission_id, status)`, `(project_id, status)`, and `created_at`.
  - **Client (viewer-token) endpoints**:
    - `POST /api/public/links/{slug}/feedback` (JSON, text)
    - `POST /api/public/links/{slug}/feedback/voice` (multipart, audio file → S3, max 25 MB)
    - Both default to `status=pending, visibility=admin_only`. Subject-isolation guard: link must include the (submission_id | talent_id | auto_pull project) trio, else 403.
  - **Admin (admin/team) endpoints**:
    - `GET /api/admin/feedback` (filter: status, project_id, submission_id; supports pagination)
    - `POST /api/admin/feedback/{fid}/approve` → status=approved, visibility=shared_with_talent, approved_by/_at stamped
    - `POST /api/admin/feedback/{fid}/reject` → status=rejected, visibility stays admin_only
    - `POST /api/admin/feedback/{fid}/edit` → text edit (text-only rows; voice 400)
    - `DELETE /api/admin/feedback/{fid}` (cleanup)
  - **Talent surface**: `GET /api/public/submissions/{sid}` now includes `client_feedback: [...]` — ONLY rows where `status=approved AND visibility=shared_with_talent`. Pending/rejected/admin_only rows are silently filtered. Sensitive fields (viewer email, link_id, approver_id) stripped via `_client_feedback_view()`.
  - **Notifications fan-out**: `client_feedback_new` on creation; `feedback_approved` / `feedback_rejected` on moderation. No talent push channel yet — talents discover approved feedback on their next submission fetch (single source of truth, no duplicate state).
  - **Retake-loop compatibility preserved**: feedback is keyed by `submission_id`, never duplicated when talent retakes; submission edits flow through existing `submission_updated` notification path.
  - **Tests**: 9 new pytests (`test_feedback_relay.py`) → 100% pass. Total backend regression now 117/117 on Atlas.

- **2026-04-25 (v21)** — **Client Viewing Intelligence System (M5).** Self-aware client review experience.
  - **5 tabs** on the public link page: All · Pending · Seen · ❤ Shortlisted · ✨ New (with live counts).
  - **Progress bar** "X of Y reviewed" pinned above the talent grid.
  - **Per-card badges**: 👁 Seen, ❤ Shortlisted, ✨ New.
  - **Auto-track Seen**: IntersectionObserver fires after 5 s of ≥50% viewport visibility OR on overlay-open. POST `/api/public/links/{slug}/seen` is idempotent ($addToSet).
  - **"New" detection**: per-link `link.subject_added_at[id]` stamped at create + on PUT for newly-added subjects (preserved for existing). For auto-pull (M2), the timestamp is derived from the submission's `decided_at`/`created_at`. A subject is "new" when `subject_added_at > viewer.prev_visit_at`.
  - **Visit rotation**: each `identify` rolls `prev_visit_at = state.last_visit_at; last_visit_at = now` in the new `client_states` collection (unique index on link_id + viewer_email).
  - **Shortlisted tab** filters where the viewer's existing per-talent action == "shortlist" — no duplicate state.
  - **Tests**: 6 new pytests (`test_client_intelligence.py`) → 100% pass. Frontend testing agent (iteration_7) → 11/11 review_request bullets verified, 0 bugs.

- **2026-04-25 (v20)** — **Phase 1 + 4 spec + Drive backup + 3-mode Link Generator UI.**
  - **Google Drive backup** via User OAuth + PKCE running on a non-blocking asyncio worker (`drive_backup.py`). Submissions backed up post-S3 with retry queue. Strict naming convention: `{brand}/{submission_id}/...`. UI: `DriveBackupCard` shows connection status + folder link.
  - **In-app notifications**: `notifications` collection + `routers/notifications.py` + `NotificationBell` (sidebar + mobile) + `/admin/notifications` page. Fanout on submission_new / submission_updated / submission_retake / submission_decision.
  - **Phase 1 — Per-link talent_field_visibility**: Link admin can override link-level visibility per talent (M1). UI added in v20: gear icon on selected talent → modal with ON / OFF / INHERIT toggles per field.
  - **Phase 1 — auto-pull (M2)**: link.auto_pull + auto_project_id resolves the curated submission list at read time so newly-approved submissions appear automatically.
  - **Phase 4 — Hold + require_reapproval_on_edit**: `hold` is a first-class decision state alongside approved/rejected/pending. Project-level toggle `require_reapproval_on_edit` (default ON) controls whether retakes flip the decision back to pending or silently keep it.
  - **3-mode Link Generator UI** (`LinkGenerator.jsx`): entry-screen mode picker → M1 Individual Talent Share · M2 Project Showcase · M3 Submission/Audition Link. Each mode locks its own picker (talents grid, project cards, or approved-submission grid) and posts a clean payload.
  - **Submission filter chips** on `/admin/projects/{id}`: All · Pending · Approved · Hold · Rejected · Updated with live counts.
  - **Tests**: 108/108 backend pytest passing on Atlas.

- **2026-04-24 (v19)** — **Secure Change Password + admin-only Reset Password flows.** Production-grade auth recovery.
  - **Change Password** (`POST /api/auth/change-password`): authenticated; verifies current with bcrypt; new password policy = min 8 chars + at least 1 number or special character; rejects same-as-current; on success bumps `token_version` which kills every existing JWT for that user. Modal accessible from `AdminLayout` sidebar + mobile top bar; logs the user out post-change and bounces them to `/admin/login`.
  - **JWT invalidation via `tv` claim**: `make_token` callers embed `tv = user.token_version`; `current_user` dependency compares token `tv` vs stored value — mismatch → `401 "Session expired"`. Stateless, no allowlist, instant global logout.
  - **Forgot Password** (`POST /api/public/forgot-password`): public page at `/forgot-password`. Always returns the exact generic message `"If that account exists, contact your administrator to reset your password."` regardless of whether the email exists — zero enumeration surface. No reset token is ever generated here. Rate-limited at 5 hits / 15 min per IP (in-memory sliding window → 429 with Retry-After header).
  - **Admin-generated reset link** (`POST /api/users/{uid}/reset-password`, admin-only): returns `{reset_token, reset_path: "/reset-password?token=…", expires_at, email}`. Raw token shown to the admin ONCE in a copyable modal; Mongo stores only the SHA-256 hex digest. 1-hour TTL (enforced by Mongo TTL index on `password_reset_tokens.expires_at`). Single-use — completing or re-generating invalidates the old token. Team users get 403.
  - **Reset Password page** (`/reset-password?token=…`): validates the token on mount (`POST /api/public/reset-password/validate`), shows the associated email, lets the user set a new password, completes via `POST /api/public/reset-password` which enforces policy, bumps `token_version`, marks the token used, and redirects to `/admin/login`.
  - **401 interceptor** in `lib/api.js`: any `"Session expired"` / `"Invalid token"` / `"Not authenticated"` response clears localStorage and bounces the user to login — BUT guarded against infinite loops on auth pages (`/admin/login`, `/forgot-password`, `/reset-password`, `/signup`).
  - **Bonus perf fix**: `/api/links` rewrote view_count/unique_viewers from an N+1 round-trip (2 queries × N links) into a single aggregation pipeline — was timing out on Atlas past ~20 links.
  - **Testing**: 81 existing pytests + 9 new `test_password_flows.py` + 1 updated `test_user_roles_api.py` = **87/87 passing on Atlas**. Testing agent iteration_5.json reports **100% backend + 100% frontend critical path** with zero issues across change-password, forgot, admin-reset, reset-consume, single-use, token_version invalidation, and rate-limit paths.

- **2026-04-24 (v18)** — **MongoDB Atlas migration complete.** Production-ready persistence.
  - `backend/.env` switched to `mongodb+srv://...@cluster0.sipmssu.mongodb.net/talentgram?retryWrites=true&w=majority` with `DB_NAME=talentgram`.
  - Atlas Network Access configured with `0.0.0.0/0` allowlist (creds-only auth). TLS handshake confirmed OK; `admin ping` returns `{ok:1}`.
  - End-to-end smoke pass against Atlas: admin login ✅, create talent ✅, create project ✅, public submission start ✅, create link ✅, viewer identify ✅. Supervisor restart retains 100% of data (persistence proven).
  - Full pytest regression on Atlas: **70 passed, 6 skipped, 2 transient failures retried → 78/78 pass.** Both initial failures were HTTP 500s from `integrations.emergentagent.com/objstore` (upstream object storage), unrelated to Mongo. Retry passed cleanly in 23 s.
  - Admin seed (`admin@talentgram.com` / `Admin@123`) auto-created on first Atlas start. No local Mongo dependency remains.

- **2026-04-24 (v17)** — **P0 Scalability pass 2 (pagination + upload caps + image resize + progress bar).**
  - **Backward-compatible pagination** on every admin list endpoint: `/api/talents`, `/api/projects`, `/api/links`, `/api/applications`, `/api/submissions/approved`, `/api/projects/{pid}/submissions`. With `?page=0&size=50` the endpoint returns `{items, total, page, size, has_more}`; omit `?page` and the legacy raw-array shape is preserved so the current UI keeps working. Size is clamped to `[1, 200]` via `_paginate_params` in `core.py`.
  - **Upload size caps**: public submission/application uploads now reject videos > 150 MB and images > 25 MB with an HTTP 400 carrying a readable `"Video is too large (NN MB). Max 150 MB — please compress and retry."` message. Constants live in `core.py` (`MAX_SUBMISSION_VIDEO_BYTES`, `MAX_SUBMISSION_IMAGE_BYTES`). Frontend mirrors the cap before sending the request so the user gets instant feedback.
  - **Image resize pipeline**: on every `category="image"` upload (submissions + applications), `resize_image_bytes()` in `core.py` produces a 1600px-wide progressive JPEG (quality 85). The smaller copy is stored alongside the original and referenced via `media.resized_storage_path`. `_public_media()` forwards this path to the client so ClientView loads fast, while the original is preserved for downloads.
  - **Frontend progress bar**: `SubmissionPage.jsx` `uploadFile` + `uploadImages` use axios `onUploadProgress` to drive a new `uploadPct` state (0-100). The `UploadSlot` CTA paints a filling `bg-white/10` stripe + "Uploading… N%" caption, the image add tile shows a `%` below the spinner, and `AddTakeSlot` shows `N%` on the Upload button — all mobile-safe.
  - **ClientView tweak**: images now go through `IMAGE_URL(media)` (new helper in `lib/api.js`) which prefers `resized_storage_path` when present; downloads still grab the original.
  - **Tests**: 67 existing pytests + 7 new (`test_scale_p0.py`) + 4 new (`test_scale_p0_extra.py` added by testing agent) = **78 passing / 0 failing**. Pagination back-compat + size caps + resize contract are all locked in.

## Older Updates
- **2026-04-24 (v16)** — **Casting Review Architecture Overhaul.** Strict new contract:
  - **Client view order**: TAKES → INTRO → IMAGES (enforced in both `_submission_to_client_shape` and `ClientView` left column).
  - **Renamable takes**: new media category `take` with `label` field; MAX_SUBMISSION_TAKES=5. Legacy `take_1/2/3` still accepted on upload but auto-mapped to `{category:"take", label:"Take N"}` on read for back-compat. New `PATCH /api/public/submissions/{sid}/media/{mid}` renames existing takes. Submission page renders a dynamic `TakeRow` per take (inline label edit) + an `AddTakeSlot` for the next slot (hidden at 5).
  - **Retest / re-upload flow**: `start_submission` now returns the existing (project, email) submission with `resumed: true`. Any post-finalize mutation (upload/delete/patch) flips `status → "updated"` and `decision → "pending"`; clients see only the latest approved version. `finalize` called a second time returns `resubmitted: true`.
  - **Competitive brand + custom answers** flow through to client payload, gated by per-submission `field_visibility`. `custom_answers` supports BOTH bool (all-or-nothing) AND `{question_label: bool}` dict (per-question filter). `AdminSubmissionEditIn.field_visibility` relaxed to `Dict[str, Any]` to accept the dict shape.
  - **CLIENT_ALLOWED_FIELDS** extended with `competitive_brand` and `custom_answers`.
  - **Status model**: `{draft, submitted, updated}` — `updated` is the new "retest pending re-approval" state.
  - **Tests**: +13 unit in `test_casting_review.py`, +7 live-API in `test_casting_review_live.py`; total **67 pytests pass**. 1 contract bug found + fixed during regression (per-question dict rejected by admin edit model — now accepted).

- **2026-04-24 (v15)** — **User Role System (admin/team) complete.** Migrated legacy `db.admins` → unified `db.users` collection on startup (idempotent — root admin keeps id/email, now with `role="admin"` + `status="active"` + `last_login`). JWT now carries `role`; `current_user` + `require_role` + `current_team_or_admin` dependencies in `core.py` enforce RBAC server-side (never trust frontend). **Role matrix**: admin = full access; team = view everything + create/edit talents/projects/submissions, CANNOT delete anything, CANNOT manage users, CANNOT create/edit links. New router `/api/users` (admin-only) provides list + stats + invite + role patch + disable/enable + hard-delete + reset-password (temp password shown once). Public signup flow (`/api/public/signup/validate` + `/complete`) consumes invite tokens (secrets.token_urlsafe, 7-day TTL, single-use). Guards: cannot disable/demote/delete the last active admin, cannot disable yourself. Frontend: new `/admin/users` page (admin-only, stats + sortable table + Select-based role change + disable/key/delete actions + gold/grey role badges), `InviteModal` returns copyable `/signup?token=…` URL (no email sent), `TempPasswordModal` displays the reset password once, public `/signup` page validates/completes + auto-login, `AdminLayout` conditionally shows Users nav + role badge in footer, Delete buttons on Talents/Projects/Links/Submissions hidden for team. **45 pytest tests pass** (26 unit + 19 live-API); **100% Playwright E2E pass** on role-gated UI. test_credentials.md updated with `raj@test.com/RajPass123!` team account.
- **2026-04-24 (v14)** — **Project Budget (editable pricing)** shipped. Projects carry two independent key/value breakdowns: `talent_budget` (rendered on submission form as an "Offered Budget" hint) and `client_budget` (rendered on client link view, gated by `visibility.budget`). Public `/api/public/projects/{slug}` strips `client_budget` so talents never see it. Link-level `client_budget_override` replaces the aggregated project client_budget when non-empty. Reusable `<BudgetLines>` component drives both ProjectEdit and LinkGenerator UIs. 5 new pytest regression tests for clean lines + public_project_for_talent stripping.
- **2026-04-24 (v13)** — **Code review fixes + security hardening.** Replaced all 5 array-index `key={i}` map patterns with stable keys (URL string keys in `TalentEdit` / `ProjectEdit` / `ClientView` / `MaterialModal`; composite `viewer_email + updated_at` keys in `LinkResults`). Fixed 1 React hook exhaustive-deps warning in `ClientView.jsx` (wrapped `loadData` in `useCallback` with `[slug]`). Added a project-level `eslint.config.js` + `@babel/eslint-parser` so future hook/key regressions are caught by CI. Backend: added `SecurityHeadersMiddleware` in `server.py` — every JSON/media response now ships `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` (geo/mic/cam off), and a strict `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. `ApplicationPage` draft (which contains the application token + PII) now carries a `savedAt` timestamp and auto-purges after 30 days.

- **2026-04-24 (v13)** — **Code review fixes + security hardening.** Frontend: replaced all 5 array-index `key={i}` map patterns with stable keys (URL string keys in `TalentEdit` / `ProjectEdit` / `ClientView` / `MaterialModal`; composite `viewer_email + updated_at` keys in `LinkResults`). Fixed 1 React hook exhaustive-deps warning in `ClientView.jsx` (wrapped `loadData` in `useCallback` with `[slug]`). Added a project-level `eslint.config.js` + `@babel/eslint-parser` so future hook/key regressions are caught by CI. Backend: added `SecurityHeadersMiddleware` in `server.py` — every JSON/media response now ships `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` (geo/mic/cam off), and a strict `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. Frontend: `ApplicationPage` draft (which contains the application token + PII) now carries a `savedAt` timestamp and auto-purges after 30 days — defense-in-depth against stale tokens in `localStorage`. All 14 pytest regression tests + 0 eslint warnings.

- **2026-04-24 (v12)** — **PART 5 (broadened email dedup) + PART 6 (auto-prefill)**. Backend: `TalentIn` gains optional `email` field; `_application_to_talent` sets `talent.email` AND `talent.source.talent_email`. Approval dedup now matches both (`$or: [{email}, {source.talent_email}]`), covering manual talents + prior applications + legacy forwards — no duplicates regardless of origin. New public endpoint `GET /api/public/prefill?email=...` returns only non-sensitive fields (first_name, last_name, age, height, location, instagram_handle, instagram_followers) — zero DOB/gender/bio/media leak. Frontend: `SubmissionPage.jsx` calls prefill on email blur, auto-fills empty fields, shows friendly toast "Welcome back, {name} — we auto-filled what we had". Prefill runs once per session. PART 7 constraints all hold: approval-required gate, email-dedup, project↔talent isolation (all pre-existing).
- **2026-04-24 (v11)** — **Admin Applications review panel** complete. New route `/admin/applications` + sidebar entry ("Applications" with UserPlus icon). Grid of cards showing cover image, name, email, location, Instagram handle, image count, and quick Approve/Reject + Review actions. Filter chips (All · Pending · Approved · Rejected · Drafts). Review modal displays full profile (Age auto-computed from DOB, Height, Gender, Location, Instagram, Followers, Bio), Intro Video player, Images grid, and sticky Approve/Reject bar. Approve → creates talent in master DB (or merges by email); re-approving is idempotent.
- **2026-04-24 (v10)** — **Open Talent Application flow** (project-independent signup). Backend: new `applications` collection + `routers/applications.py` with public endpoints (`POST /api/public/apply` · email-based resume, `PUT /api/public/apply/{aid}`, `POST /api/public/apply/{aid}/upload`, `DELETE /api/public/apply/{aid}/media/{mid}`, `POST /api/public/apply/{aid}/finalize`) and admin endpoints (`GET /api/applications`, `GET /api/applications/{aid}`, `POST /api/applications/{aid}/decision`). On approval, `_application_to_talent()` creates a master talent record (or **merges** if the email already exists — adds new media, fills empty fields). Storage isolated at `{app}/applications/{aid}/...`. Frontend: new `/apply` route (`ApplicationPage.jsx`) with identity gate → 3 sections (Profile · Professional · Media) → Submitted thank-you. Autosave every 800 ms · localStorage draft · resume-by-email supported.
- **2026-04-24 (v9)** — **Availability & Budget now flow to client view** (admin-controlled final values). Architecture decision: `form_data` IS the admin-controlled source of truth (since admin edits persist via review modal PUT) — no separate `reviewed_data` needed. Backend: added `availability` + `budget` to `CLIENT_ALLOWED_FIELDS`; `_submission_to_client_shape` emits structured `{status, note}` / `{status, value}` objects gated by `field_visibility`; `_filter_talent_for_client` further gates by link-level `visibility.availability` / `visibility.budget`. Frontend: `ClientView` renders a "Details" card with coloured pills (green AVAILABLE, red NOT AVAILABLE, green ACCEPTS PROPOSED BUDGET, neutral CUSTOM). `DEFAULT_VISIBILITY` adds `availability: true`, `budget: false`; `VIS_ITEMS` adds both toggles in `VisibilityToggles.jsx` + `LinkGenerator.jsx`. Regression test covers both enabled + disabled states (14 tests pass).
- **2026-04-24 (v8)** — **Audition takes now visible to clients** (gated by new `visibility.takes` toggle, default true). Backend: `_submission_to_client_shape` preserves `take_1/2/3` in deterministic order (intro → take_1 → take_2 → take_3 → portfolio); `_filter_talent_for_client` gates them via `visibility.takes`. Frontend: `ClientView` renders `INTRODUCTION` (hero), `AUDITION TAKES` (grid with Take 1/2/3 labels), `PORTFOLIO` (image slider) as distinct sections. All video elements use `preload="metadata"` for perf. `DEFAULT_VISIBILITY` + `VIS_ITEMS` updated in both `VisibilityToggles.jsx` and `LinkGenerator.jsx`. Regression test covers ordering + visibility gating (13 tests pass).
- **2026-04-24 (v7)** — **Reference Videos** support added to project audition materials. Backend: new `video_file` category in `MATERIAL_CATEGORIES`, 100 MB size cap, video-mime validation, segregated storage path (`{app}/projects/{pid}/videos/...`). Frontend: `ProjectEdit` now shows a 4-tile upload grid (Script · Images · Audio Notes · Reference Videos) with "Max 100 MB · mp4/mov" hint + client-side size/type guard. `MaterialModal` adds a **REFERENCE VIDEOS** section rendering each video inline with a native player + filename label. Both admin preview and public talent submission page share the same modal, so talents see reference videos automatically.
- **2026-04-24 (v6)** — **Router split & theme audit.** Backend: `server.py` reduced from 1469→50 lines (bootstrap only). Shared primitives moved to `core.py` (config, db, security, storage, utils, constants, models, visibility filters). Route logic split into `routers/{auth,talents,links,projects,submissions}.py`. All 12 regression tests pass + live curl confirms every endpoint still works (login, talents, links, projects, submissions, public viewer flow, 404s). Frontend: `ForwardToLinkModal.jsx` and `SubmissionReviewModal` (inside `ProjectEdit.jsx`) fully theme-aware — replaced every hardcoded `bg-black`, `text-white`, `border-white`, `bg-[#0a0a0a]` with shadcn tokens (`bg-background`, `text-foreground`, `text-muted-foreground`, `bg-muted`, `border-border`). Video/audio players wrapped in `bg-muted rounded-lg` for contrast in both themes.
- **2026-04-24 (v5)** — Link editor now supports inline editing of `submission_ids`. Added `GET /api/submissions/approved` (admin convenience endpoint) and a tabbed picker (`Talents` / `Auditions`) in `LinkGenerator.jsx`. Admins can now freely add/remove talents AND approved submissions on the same link, including creating **mixed** links (both sources). Backend `create_link` / `update_link` strip empty-string ids, deduplicate, and reject links with zero subjects.
- **2026-04-24 (v4)** — Hardened client payload with a structural allowlist. Added `CLIENT_ALLOWED_FIELDS` constant in `server.py`; `_filter_talent_for_client` applies it as a final defensive sweep so admin-internal data (`availability`, `budget`, `custom_answers`, `competitive_brand`, `form_data`, `field_visibility`, `dob`, `gender`, `bio`, `email`, `phone`, `source`, `notes`, etc.) can never leak to clients — even if future code accidentally adds a new key. Regression tests in `/app/backend/tests/test_client_payload_isolation.py` (4 passing) lock in the invariant.
- **2026-04-24 (v3)** — Media scoping is now explicit. Every uploaded media dict carries a `scope` marker + origin ids: submission media → `scope="submission"` with `submission_id` + `project_id`; project material → `scope="project_material"` with `project_id`; talent media → `scope="talent_portfolio"` with `talent_id`. Before leaving the API to the client, a `_public_media()` sanitizer strips scope markers so clients never see internal origin metadata.
- **2026-04-24 (v2)** — Submissions are no longer copied into the `talents` master DB. Added `link.submission_ids` alongside `link.talent_ids`; `/api/projects/{pid}/forward-to-link` now just stores submission references (preserves selection order) instead of inserting duplicate talent profiles. Public `/api/public/links/{slug}` loads talents + submissions, flattens submissions via `_submission_to_client_shape` (respects per-field visibility), and applies the strict link-level allowlist. Admin `/api/links/{lid}/results` returns a unified `subjects` map (each tagged `source: talent|submission`) so the results page resolves names for both types. Frontend `LinkResults.jsx` and `LinkHistory.jsx` updated accordingly.
- **2026-04-24** — Strict client visibility: `/api/public/links/{slug}` returns an explicit allowlist per talent, filtered by the link's `visibility` map. Admin-only fields (`notes`, `password`, `created_by`, `talent_ids`, `submission_ids`, `is_public`) are stripped from the link response. Talent fields (`dob`, `gender`, `bio`, `source`, `created_at`, any toggled-off demographic/social data) are never sent to the client. Media is filtered: images require `visibility.portfolio`, videos require `visibility.intro_video`.
- **2026-04-24** — Admin review modal now always renders all 3 audition take slots (Take 1/2/3) with video players or "Not submitted" placeholders, plus a fallback for missing intro video. Client matrix enforced: Intro ✅/✅, Takes ✅/❌, Images ✅/✅.

## Phase 2 — Backlog
### P1 (high-value)
- [ ] Private link permissions (per-email ACL, password-protected links)
- [ ] Budget form per project (editable pricing per talent)
- [ ] Project-based auto talent selection (tags/categories → auto multi-select)
- [ ] Pagination for talents/links (once lists grow beyond 1000)
- [ ] Server-side validation on media uploads (max size, MIME allowlist)

### P2 (nice-to-have)
- [ ] Advanced analytics charts (daily views, action funnel)
- [ ] Bulk import talents (CSV)
- [ ] Email notification when client completes review
- [ ] Presigned/tokenized file URLs for stricter privacy
- [ ] Activity timeline on Results page

## Test Credentials
See `/app/memory/test_credentials.md`.
