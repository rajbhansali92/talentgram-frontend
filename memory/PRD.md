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
- **2026-04-30 (v38d)** — **Branded favicon + OG/social-share image (UI ONLY).** Premium meta-asset polish so shared `/l/...` links no longer surface as default Emergent badges in WhatsApp/LinkedIn/iMessage previews.
  - **Favicons** in `/app/frontend/public/`:
    - `favicon.svg` — theme-aware Talentgram monogram (T flanked by brand slashes), painted via `prefers-color-scheme` media query so the favicon adapts to the user's browser chrome.
    - `favicon-16x16.png` / `favicon-32x32.png` / `favicon.ico` — multi-resolution PNG fallbacks generated from the brand asset (auto-cropped via alpha bbox).
    - `apple-touch-icon.png` (180×180) — iOS home-screen icon.
  - **OG image** `/og-image.png` (1200×630):
    - Solid `#080808` background with a soft elliptical glow for cinematic depth.
    - Centered white-ink Talentgram logo + hairline divider + `WE SCOUT · WE MANAGE` mono tagline + `INDIA | UAE` serif region.
    - Hairline corner ornaments + `PORTFOLIO · CASTING DECISION ENGINE` footer descriptor + small gold accent dot (matches in-app `#c9a961` highlight).
    - Generated via Python PIL from `talentgram-white.png` so it's pixel-aligned with the in-app logo.
  - **`index.html` head**:
    - Added `<link rel="icon">` chain (SVG → PNG → ICO) + `<link rel="apple-touch-icon">`.
    - Replaced default Emergent description with brand copy: "Talentgram — Casting decision platform. Curated portfolios, decisive presentations. India · UAE."
    - Theme-color now adaptive: `#050505` dark / `#fafaf7` light via `prefers-color-scheme`.
    - Full Open Graph tags (`og:type`/`site_name`/`title`/`description`/`image` + dimensions + alt) — required by WhatsApp/LinkedIn/iMessage scrapers.
    - Twitter Card tags (`twitter:card=summary_large_image` + title/description/image) for X/Twitter previews.
  - **Verified** — curl returns 200 + correct content-type for all 6 new assets (svg, ico, og-image.png, apple-touch, 16/32 png). HTML head emits all OG/Twitter meta. OG image visually confirmed (cinematic black with centered logo + tagline + corner ornaments + gold accent).
- **2026-04-30 (v38c)** — **Theme system upgrade — premium contrast & dual-logo polish (UI ONLY).** Tightens the dark/light parity to a locked colour spec without touching any business logic.
  - **Locked colour tokens** (CSS vars in `index.css`):
    - Light: primary `#111111` · secondary `#555555` · border `rgba(0,0,0,0.10)` strong `0.25`
    - Dark: primary `#FFFFFF` · secondary `#BBBBBB` · border `rgba(255,255,255,0.10)` strong `0.25`
    - Shadcn `--muted-foreground` synced: light `0 0% 33%` (#555), dark `0 0% 73%` (#BBB).
  - **Dark-mode secondary floor**: `text-white/40`, `/50`, `/60` clamped to `#9a9a9a` / `#b0b0b0` / `#bbbbbb` so faded labels never drop below WCAG AA on dark surfaces.
  - **Light-mode text mapping**: `text-white/85` (was unmapped → invisible on light bg, breaking the Landing "Apply as Talent" button) now resolves to `rgba(17,17,17,0.92)`. All `text-white/X` variants from `/95` through `/20` now map to graduated `#111`-based opacities.
  - **Logo system** (`Logo.jsx`):
    - Sizes bumped: `sm` 24, `md` 40, `lg` 64, `xl` 110, `2xl` 150 (height; width auto via 1.99:1 aspect).
    - Subtle drop-shadow added (`0 1px 2px rgba(opposite,0.55)`) so the logo never blends into similar-luminance surfaces.
    - Theme detection unchanged — `useTheme().isLight` already swaps `talentgram-black.png` ↔ `talentgram-white.png`.
  - **BrandHero** (`BrandHero.jsx`): hero sizes bumped — md=110/150 (mobile/desktop height), lg=140/200. Width on desktop hero reaches ~280–400px.
  - **Eyebrow weight** raised to 600 for legibility on small uppercase labels.
  - **Verified live** at 1280×800 + 390×844 across Landing, AdminLogin, Dashboard, Talents, /apply — both modes:
    - Logo correctly swaps ink (white in dark, black in light).
    - Drop-shadow halo visible on solid surfaces, invisible on contrasted ones (intentional).
    - `Apply as Talent` outline CTA on Landing now READABLE in light mode (was washed-out before).
    - Sidebar logo, page headings, secondary text, primary buttons, outlined buttons — all pass visual contrast in both modes.
- **2026-04-30 (v38b)** — **Pagination Phase 2 (Links + Project Submissions).** Wired the same backward-compatible pagination + infinite-scroll pattern to the remaining high-volume admin lists.
  - **Backend**: `/api/links` and `/api/projects/{pid}/submissions` now accept `?limit=` (alias for `size=`) like the talents/applications endpoints. New `GET /api/projects/{pid}/submissions/stats` returns `{all, pending, approved, hold, rejected, updated}` counts via cheap `count_documents` calls — powers the filter-chip badges without loading the full submission set.
  - **Frontend `LinkHistory.jsx`**: refactored to `useInfiniteList` + `useInfiniteScroll`. Loads 30/page with auto-load + manual "Load more (N remaining)" button + "Showing X of Y" header. `duplicate`/`delete`/`bulk-delete` callbacks rewired through hook's `reload()`.
  - **Frontend `ProjectEdit.jsx`**: submissions section now server-filtered. Filter chip change → re-fetches scoped page from backend (no more client-side filter scan). Counts come from `/submissions/stats` (always full-DB-accurate, not just-current-page-accurate). Action handlers (`setDecision`, `deleteSubmission`, `onChanged` from review modal) `Promise.all` the list+stats reload.
  - **`ForwardToLinkModal` data load**: opening "Create Client Link from Approved" now fetches all approved submissions in one shot (`?decision=approved&limit=200`) into a dedicated `forwardModalSubmissions` state, decoupled from the paginated list. Prevents the modal from missing approved submissions that aren't on the current visible page.
  - **Verified**: curl smoke — `/links?limit=5` → `{total:6, pages:2, has_more:true}`; `/projects/{pid}/submissions/stats` → `{all:3, pending:2, approved:1, ...}`; live UI confirms chips show server counts + filter switch only shows matching rows.
- **2026-04-30 (v38)** — **Pagination — Phase 1 (TalentList + Applications).** Backward-compatible pagination on the two highest-volume admin lists.
  - **Backend (`core.py`)**: `_paginated()` now returns BOTH legacy keys (`items`, `size`, `has_more`) and canonical keys (`data`, `pages`, `limit`) so old & new consumers coexist. `_paginate_params()` accepts either `?size=` (legacy) or `?limit=` (new); when both, `limit` wins. Page index stays 0-indexed.
  - **`/api/talents`** + **`/api/applications`**: when `?page=` or `?limit=` is supplied, returns paginated dict; otherwise returns legacy raw array (Dashboard / LinkGenerator unaffected).
  - **`/api/applications/stats`** (NEW, admin/team): single endpoint returning `{all, pending, approved, rejected, drafts}` counts via `count_documents`. Powers filter-chip badges without loading the full list.
  - **Frontend hook** `useInfiniteList` + `useInfiniteScroll` in `/app/frontend/src/hooks/useInfiniteList.js` — handles paginated AND legacy array responses, race-condition-safe via request-id ref, debounced reset on `deps` change.
  - **`TalentList.jsx`**: now loads 30 at a time with debounced search + IntersectionObserver auto-load + manual `Load more (N remaining)` button + "Showing X of Y" header. Bulk-delete reload routed through hook's `reload()`.
  - **`Applications.jsx`**: identical 30/page UX. Filter chips show server-side counts (`Pending 0 · Approved 7 · Rejected 2 · Drafts 53`). Smoke verified end-to-end: drafts filter shows 30 cards → click Load More → 53 cards → button disappears, summary updates.
  - **Auth Persistence (P0) — re-verified.** Live test on preview Atlas DB: changed `Admin@123 → PersistTest@456` → restarted backend via supervisor → new pw STILL works post-restart, env `Admin@123` STILL rejected → restored to Admin@123. Fix from v37d (`seed_admin` no longer rewrites `password_hash` for existing rows) is locked in.
- **2026-04-27 (v37j)** — **Project Review 3-section render + granular link visibility (UI ONLY).** Three changes, no backend touch:
  1. **`ProjectEdit.jsx` Review modal** — Submission media is now split into Indian/Western/Portfolio sections (`data-testid=review-{indian|western|portfolio}-images-section`), each gated by `length > 0` so empty buckets are completely hidden. The submission-row badge counter sums all three image categories (was `image`-only).
  2. **`LinkGenerator.jsx` Visibility Controls** — Added 2 NEW toggles: **Indian Look Images** (`vis-toggle-indian_images`) and **Western Look Images** (`vis-toggle-western_images`), defaulting ON. The existing **Portfolio Images** toggle remains as the master gate (when OFF, all three image buckets are hidden in ClientView).
  3. **`ClientView.jsx` granular gating** — Reads `vis.indian_images` and `vis.western_images` (with `?? true` backward-compat fallback for older links) and filters the per-talent media buckets client-side. `portfolio=false` acts as a master gate that hides all three.
  Backend round-trips `link.visibility` as a free-form `Dict[str, bool]` (already supported), so no schema or API change. Verified: 10/10 backend pytest in iteration_15, ProjectEdit Review 3-section render confirmed for Shivani Yadav (4 portfolio + 3 indian + 1 western), LinkGenerator UI shows 3 toggles in correct order, indian_images=false PUT through admin API persists and reflects in public-link payload (visual gating logic is pure JS in ClientView L600-L614). Canonical fixture (`talentgram-x-comfort-9339a4`) restored to all-true at the end.
- **2026-04-27 (v37i)** — **Submission ↔ Global Talent media sync.** Closes a long-standing gap where talents who uploaded `image`/`indian`/`western` images during a project submission saw them in the Client View (after v37h) but NOT on their Global Talent Profile (`/admin/talents/:id`) — because submission media lived only on `db.submissions[].media[]` and was never copied into `db.talents[].media[]`. Phase 0 spec deliberately kept the two scopes separate; v37i adds a one-way mirror with idempotency. Implementation: `core.py` exposes `sync_media_to_global_talent()` and `remove_synced_media_from_global_talent()` keyed on a stable `source_submission_media_id`. Wired into (a) `submissions.upload_submission_media` (forward sync on every new image upload), (b) `submissions.submission_delete_media` (reverse sync on remove), and (c) `submission_finalize` (retroactive sweep of pre-finalize uploads — closes the testing-agent-flagged design gap so a draft talent's pre-finalize images surface on the global profile as soon as their record is created). Categories `intro_video` and `take` are NEVER mirrored (audition-scope only). Backfill script at `/app/backend/scripts/backfill_global_talent_media.py` mirrored 28 existing items across 4 talents on first run; idempotent re-run mirrors 0. Verified by 14/14 backend pytest + live curl smoke confirming a pre-finalize indian upload appears on the global profile post-finalize. Frontend untouched (`TalentEdit.jsx` already renders 3 sections; data simply now exists in the API response).
- **2026-04-27 (v37h)** — **Phase 3 — Indian/Western/Portfolio media + per-category caps + work_links surface.** Root bug: `_submission_to_client_shape` in `core.py` only mapped `cat=='image'` → `'portfolio'` and silently dropped `indian` / `western` from the client view payload. Now all three are passed through with their category preserved; `ClientView.jsx` concatenates them into the portfolio carousel (`portfolio → indian → western`). Per-category cap raised from a combined 8 to **10 per category** (`MAX_IMAGES_PER_CATEGORY = 10`) — independently enforced in `submissions.upload`, `applications.upload`, `SubmissionPage.uploadImages`, `ApplicationPage.upload`. `drive_backup` extended to handle `indian` (folder `indian_look`, file `indian_N.jpg`) and `western` (folder `western_look`, file `western_N.jpg`). `work_links` already round-trips correctly via `form_data` → `talent.work_links`. Verified by 14/14 pytest + ClientView smoke on `/l/talentgram-x-comfort-9339a4` (carousel went from 1/4 to 1/8 once indian/western surfaced).
- **2026-04-27 (v37b)** — **Email-first validation timing fix.** The Continue / Save buttons on `/apply` and `/submit` were validating `first_name` BEFORE `email` in their order-of-checks, so a user who hadn't completed the email step (or who had only typed an email) saw a misleading "First name is required" toast. Pure validation-order fix in the frontend — no schema, no API, no UI redesign.
  - **/submit `validateForm` and `validateStep1`**: now check `email` FIRST. If email is empty → "Email is required". If `emailGateUnlocked` is `false` → "Please complete the email step first". Otherwise full validation continues in the original order. The Continue button on the mobile wizard bottom bar was already hidden behind `{emailGateUnlocked && ...}`, so this is also defense-in-depth.
  - **/apply `startApplication`**: reordered checks — email first, then `emailGateUnlocked` guard, then first_name, then last_name. The Continue button was already gated on `disabled={!emailGateUnlocked}`.
  - **Behavior verified live** at 390x844 mobile viewport on `/submit/{slug}`:
    - Initial load → Continue button HIDDEN, no validation can fire ✅
    - Empty email blur → Continue still hidden ✅
    - Unknown email blur → Continue appears → click without first_name → correctly shows "First name is required" (correct post-email-step error) ✅
    - Known email + "Use this" → first_name auto-prefilled → Continue passes through ✅

- **2026-04-27 (v37)** — **Phase 1 cleanup: budget consolidation + project metadata visibility on /submit.** Pure rendering / mapping work — no backend, schema, or API changes.
  - **Removed from project creation UI:** the two `<BudgetLines>` editors on `ProjectEdit` (talent-facing + client-facing budget). Wrapped the section in `{false &&}` so the existing data on already-created projects is preserved untouched in the DB but no longer surfaces or can be edited. Re-enable by restoring the conditional.
  - **Removed from link generator UI:** the `Client Budget Override` section. Also wrapped in `{false &&}` for the same legacy-data preservation; existing override values on existing links are inert from the UI's perspective but DB-untouched.
  - **Removed from visibility toggles:** the `Budget Form` row in `LinkGenerator`'s `FIELDS` array. The single `Budget` toggle now controls all client-facing budget visibility. Existing links keep their `visibility.budget_form` value in the DB but it's no longer surfaced anywhere.
  - **Single budget source going forward:** the talent's submitted budget on each submission. Admin can edit it on the submission row in `ProjectEdit` (existing flow). Client view's `client-budget` block already prefers `talent.budget.value` (counter-offer) and falls back gracefully to "Agreed" pill when the talent accepted — no changes needed to the rendering logic.
  - **/submit project metadata:** Director + Production House are now rendered alongside Character / Shoot Dates / Commission / Medium-Usage / Additional Details. Both fields already existed in `projects.director` / `projects.production_house` — the talent-facing audition page just wasn't surfacing them. `<Info>` auto-hides empty values, so older projects without those fields stay clean.
  - **Note:** `/apply` is project-independent (open self-onboarding), so Director/Production House aren't applicable there. Surfacing was added only to `/submit/{slug}` per actual context.
  - Verified live: 1 screenshot of `/submit/pantaloons-with-a-celebrity-11ccd4` shows DIRECTOR=misha ghose + PRODUCTION HOUSE=Amok Films + no Budget/Day. 1 screenshot of `/admin/projects/{id}` confirms 0 BudgetLines editors. 1 screenshot of `/admin/links/new` confirms no Override section + no Budget Form toggle. Lint clean across all 3 files.

- **2026-04-26 (v36)** — **Notification panel layout fix.** The dropdown was being cropped on desktop because:
  1. Outer wrapper used `max-h-[calc(100vh-80px)]` (≈90vh) — looser than the 80vh spec.
  2. Inner scroll region had `max-h-[420px]` — capped the list and made the panel look stunted on tall viewports.
  3. Position was `md:right-0` against a bell wrapper that sits at the right edge of the **left** sidebar — the dropdown's left edge was rendering at `x=-129` on a 1280px viewport, **off the left side of the screen**.
  - Fix (CSS-only, no JS / state / API changes):
    - Outer wrapper: `max-h-[80vh]` (per spec) + retain `overflow-hidden` for clean rounded edges.
    - Inner scroll region: drop the `max-h-[420px]` cap; let `flex-1 overflow-y-auto overscroll-contain min-h-0` claim the remainder of the 80vh wrapper.
    - Position: `md:right-auto md:left-0 md:top-full md:mt-2` — dropdown now drops down + extends INTO the main content area instead of off-screen left.
    - Mobile sheet (`fixed left-2 right-2 top-[60px]`) untouched.
  - Verified live at 1280x800: panel x=199, y=68, w=360, h=640 → **fully within viewport, exactly 80vh tall**. Scroll region overflow-y: `auto`. Header + footer never clip; list scrolls cleanly.

- **2026-04-26 (v35)** — **Video streaming fix: real `206 Partial Content` + Range support.** The upstream Emergent Object Store ignored `Range` headers and always returned `200 OK` with the full body, while we falsely advertised `Accept-Ranges: bytes`. That broke Safari/iOS playback and made every seek re-download the entire file. **Fixed via server-side range slicing in `core.py:stream_object()`** — no storage-layer or schema changes.
  - When the client sends a `Range` header and upstream returns `200`, we now parse the range (`bytes=START-END`, `bytes=START-`, `bytes=-N`), stream the upstream body chunk-by-chunk, yield only the requested slice, and respond with `206 Partial Content` + correct `Content-Range: bytes <start>-<end>/<total>` + slice-length `Content-Length`.
  - When upstream actually honors the Range (returns 206), we forward unchanged.
  - Out-of-range requests return `416 Range Not Satisfiable` per RFC, with a `Content-Range: bytes */<size>` header.
  - `Cache-Control` is now **force-set** (not setdefault) to `public, max-age=86400`. Verified via direct localhost curl that backend emits the override; in this preview env Cloudflare's edge clobbers it on egress to `no-store, no-cache, must-revalidate`, but in production deploys our value lands.
  - `Accept-Ranges: bytes` is now truthful — we DO support seeking via slicing in every code path.
  - Memory cost bounded by the 256 KB upstream chunk size — no full-file buffering.

- **2026-04-26 (v34)** — **Client View polish: budget value, availability dates, name privacy, Instagram bug fix.** All 4 fixes scoped strictly to client-facing rendering — no schema changes, no API contract changes, no submission write-path changes. Endpoints / Pydantic models / DB indexes are byte-for-byte identical.
  - **Budget display fix.** When the talent picked "Counter-offer" the value now renders (e.g. `"1.5L & 50k"` with a `Counter-offer` chip) instead of just "Custom — —". When the talent accepted and the project has published budget lines, those lines render as a list. When neither is available, the section is hidden gracefully.
  - **Availability display fix.** A new `project_shoot_dates` sibling array (sourced from existing `projects.shoot_dates` field — no schema change) is exposed on `GET /api/public/links/{slug}` and rendered above the talent's response (`AVAILABILITY` label → shoot-dates line → `STATUS` pill: Available / Not Available / Conditional). Conditional fires automatically when the talent left a note on a yes/no answer.
  - **Name privacy (CRITICAL).** New `privatizeName()` helper collapses every client-facing name to `First L` (e.g. `Ayushi Thakur → "Ayushi T"`). Applied to: card label, detail-pane header, image alt-text, talent-grid row label. Single-name talents pass through unchanged; extra whitespace is trimmed.
  - **Instagram bug fix.** Root cause: `_submission_to_client_shape` in `core.py` hardcoded `instagram_handle: None` / `instagram_followers: None` / `work_links: []`, silently dropping all three even when the admin had toggled visibility ON. Fix: surface the existing `form_data.instagram_*` / `form_data.work_links` values, gated by the submission-level `field_visibility` (which already defaults to `True` for those keys). Result: Instagram handle now renders, is clickable, opens in a new tab with `rel="noopener noreferrer"`, and Followers chip shows the value. Hidden gracefully when toggle off OR value missing.
  - Verified live on `/l/talentgram-x-pantaloons-with-a-celebrity-746149`: cards show "Ayushi T" / "Shiwani B"; detail pane shows shoot-dates "2nd May - Trials on 30th April/1st may" + AVAILABLE chip + AGREED budget + clickable @ayushinidhi.

- **2026-04-26 (v33)** — **Dropdown / Enum Centralization (Phase 2.1).** Every dropdown option, decision-status list, and enumeration in the UI now consumes from the single `/app/frontend/src/lib/talentSchema.js` module. Zero hardcoded enum drift remains in any user-facing form.
  - **New centralized exports:** `AVAILABILITY_OPTIONS` (yes/no), `BUDGET_OPTIONS` (accept/custom), `SUBMISSION_DECISIONS` (pending/approved/hold/rejected) + `SUBMISSION_DECISION_KEYS`, `SUBMISSION_FILTER_TABS` (adds `all` + `updated` synthetic tabs), `FEEDBACK_TYPES` (voice/text), `FEEDBACK_STATUSES` (pending/approved/rejected), `MATERIAL_CATEGORIES` (image/video_file/audio).
  - **Refactored consumers:**
    - `SubmissionPage.jsx` → AVAILABILITY_OPTIONS for the Yes/No availability gate (replaced inline `[{key:"yes",...}]`).
    - `ProjectEdit.jsx` → AVAILABILITY_OPTIONS, BUDGET_OPTIONS in the admin review-side selects + SUBMISSION_FILTER_TABS for the per-project filter chips.
    - `AdminFeedback.jsx` → FEEDBACK_STATUSES (synthesized with `all` for the filter tabs).
  - **Pre-existing centralized exports (Phase 2):** `HEIGHT_OPTIONS`, `GENDER_OPTIONS`, `ETHNICITY_OPTIONS`, `FOLLOWER_TIERS`, `MEDIA_CATEGORIES`, `PORTFOLIO_LOOK_CATEGORIES`, `calcAge`, `genderLabel`, `ethnicityLabel`.
  - **Verified live:** AdminFeedback shows 4 chips (Pending/Approved/Rejected/All), ProjectEdit shows 6 chips (All/Pending/Approved/Hold/Rejected/Updated), no console errors. Lint clean across all touched files.
  - Note: `Applications.jsx` STATUS_FILTERS and `ClientView.jsx` TABS keep their own bespoke rows because they each carry view-specific metadata (`query` payloads, `icon` components) and bespoke keys (`drafts`, `seen`, `shortlisted`, `new`) that aren't part of the canonical decision/status enums. These are admin-side filter UIs, not talent-input forms.

- **2026-04-26 (v32)** — **Email-First Conditional Rendering on `/apply` and `/submit`.** Pure conditional-rendering + state work — no backend, schema, prefill API, or validation changes.
  - Both surfaces now show ONLY the email field on first paint. Every other input + the wizard stepbar + the wizard bottom action bar are hidden behind a new `emailGateUnlocked` boolean.
  - **On email blur:** the existing `/api/public/prefill` is called.
    - **Match → "We found your profile" card** appears (`apply-prefill-card` on /apply, `prefill-suggestion-card` on /submit) with two CTAs: `Use this` / `Edit manually`. The rest of the form stays hidden until the user picks one.
    - **No match / network error / 429 → silent unlock** so the user is never blocked.
  - **`Use this`** fills only EMPTY fields (never overwrites typed values, never touches media), shows toast `Welcome back, {first_name}`, and unlocks the form. **`Edit manually`** dismisses the card and unlocks the form.
  - **Re-arm logic:** if the user edits an email after a tried lookup, the gate re-locks so a corrected typo retriggers prefill.
  - **Existing draft resume:** if /submit has a `saved` draft (already-started submission), the gate auto-unlocks so resumed sessions skip straight to where the talent left off.
  - Smoke-tested all 4 behaviours on both pages: initial-only-email ✅, unknown-email auto-unlock ✅, known-email card ✅, Use-this prefill+unlock ✅.

- **2026-04-26 (v31)** — **Phase 2: Schema Unification across the 3 talent-facing surfaces.**
  - **Single source of truth** at `/app/frontend/src/lib/talentSchema.js`: exports `HEIGHT_OPTIONS`, `GENDER_OPTIONS` (4: female/male/non_binary/prefer_not_say), `ETHNICITY_OPTIONS` (9: indian/south_asian/east_asian/caucasian/african/middle_eastern/latino/mixed/other), `FOLLOWER_TIERS` (25 buckets across 4 groups: Early/Mid/High/Premium), `MEDIA_CATEGORIES`, `calcAge`. Admin TalentEdit, `/apply`, and `/submit` now ALL import from this module — no more inline drift.
  - **Backend schema additions (`core.py`)**: `phone: Optional[str]` added to `TalentIn` (was previously collected by 2 of 3 forms but silently dropped on merge — fixed). `SUBMISSION_UPLOAD_CATEGORIES` and `APPLICATION_UPLOAD_CATEGORIES` extended to accept `indian` and `western` look images. New `PORTFOLIO_IMAGE_CATEGORIES = {image, indian, western}` drives a unified MAX(8)/MIN(5) image gate at upload + finalize.
  - **Backend prefill (`/api/public/prefill`)**: response now includes `gender`, `ethnicity`, `bio`, `work_links` (still strictly excludes media/source/notes/created_by).
  - **Backend application flow (`routers/applications.py`)**: `_application_to_talent()` now writes `phone`, `ethnicity`, `gender`, `work_links` into the talent record. Approval merge branch fills `phone` if empty.
  - **Backend submission finalize (`routers/submissions.py`)**: Q5 — "fill empty only" sync. When a finalize lands an existing-talent match, blank fields on the talent are filled from the latest submission, but admin's hand-edits are NEVER overwritten. Media is also never merged here (per Phase 0 spec).
  - **Admin TalentEdit**: now exposes `email` + `phone` inputs (regression — Admin couldn't directly edit either before). Gender pills are 4 canonical (was 2: Male/Female only). Ethnicity is now a `Select` dropdown with the 9 canonical options (was a free-text input).
  - **`/apply`**: added `Ethnicity` dropdown, `Work Links` repeater, `Indian Look` and `Western Look` optional image groups (`apply-look-group-indian`, `apply-look-group-western`). Pulls `FOLLOWER_TIERS` from the shared module so the bucket list matches Admin exactly.
  - **`/submit/{slug}`**: added a new `unified-identity-block` with gender pills, ethnicity dropdown, instagram_handle, instagram_followers, bio, work_links. Step 3 uploads gain `Indian Look` + `Western Look` `<PortfolioGroup>` sections above the generic Portfolio Images grid (per Q3). The unified MAX/MIN image counts now include all 3 categories.
  - **Frontend tests:** Admin/Apply/Submit smoke-tested at 1280×900 — all expected testids rendered, schema parity confirmed (4 genders + 9 ethnicities + 25 follower tiers).
  - **Backend tests:** new `tests/test_phase2_unification.py` with 7 cases — phone round-trip, dup-email merge fills empty only, prefill exposes unified fields without leaking media/source, indian+western upload categories accepted, MAX(8) cap enforced across all 3 portfolio categories combined, finalize fill-empty-only sync, application-approval writes phone/work_links/ethnicity/gender. **7/7 PASS.**

- **2026-04-25 (v30)** — **Phase 1 verified + Admin Onboarding-Link card shipped.**
  - **Phase 1 (Talent Identity Auto-fill) verification (iteration_9.json)**: 100% backend pytest (6/6 in `test_phase1_prefill.py`) + 100% frontend critical path. Email is the FIRST input under Talent Details, suggestion card 'We found your profile.' surfaces with `Use this` / `Edit manually` CTAs, applyPrefill never overwrites typed fields, no media is ever pre-populated, unknown emails are silent no-ops, and the rate limiter (20/min/IP) returns 429 on excess. The earlier `JSONDecodeError` was a transient supervisor-reload race — endpoint is stable on Atlas.
  - **Admin Onboarding-Link card** (`Dashboard.jsx` → new `OnboardingLinkCard` component): surfaces the public `/apply` URL right under Drive Backup with Copy + WhatsApp share buttons. Card carries `data-testid="onboarding-link-card"` (+ `onboarding-link-url`, `onboarding-copy-btn`, `onboarding-whatsapp-btn`). Replaces the old "team has to remember the URL" friction. Inline link to `/admin/applications` for the review queue.

- **2026-04-25 (v29)** — **Phase 1 implementation: Talent Identity Utilization.**
  - **Email-first form**: `SubmissionPage.jsx` now anchors the entire Talent Details form on the Email field (data-testid `form-email`). Email is rendered as the first input in Step 1 of the mobile wizard *and* in the desktop layout. On blur the form posts the email to `/api/public/prefill`.
  - **Confirmation-prompt UX (no silent overwrites)**: when `prefill` returns a hit, an inline `prefill-suggestion-card` appears with "We found your profile. Use saved details?" and two buttons — `prefill-use-btn` ("Use this") and `prefill-dismiss-btn` ("Edit manually"). `applyPrefill` only fills fields where `f.<field>` is empty — pre-typed values are never overwritten. Toast: "Welcome back, {first_name}". Auto-fills `first_name`, `last_name`, `phone`, `age`, `dob`, `height`, `location`, `instagram_handle`, `instagram_followers`. **Never** auto-fills media (intro, takes, images), bio, gender, ethnicity, or work_links.
  - **Backend**: `GET /api/public/prefill?email=...` projects ONLY the safe allowlist from talents (no media/bio/gender/work_links). Empty `{}` for unknown / malformed emails. Sliding-window rate limiter (20 reqs / 60 s / IP) returns 429 on excess. Tests: `test_phase1_prefill.py` 6/6 pass.

- **2026-04-25 (v28)** — **Phase 0: Unified Email-Identity Hardening.** Strict no-new-features production cleanup before Phase 1.
  - **Migration script** (`/app/backend/migrations/phase0_dedup.py`): groups talents by lowercased email, keeps the oldest per cluster, merges non-empty fields + media (without overwriting), then deletes duplicates. Same logic on `applications` (one per email, prefer submitted-over-draft + newest) and `submissions` (unique per `(project_id, talent_email)`). Generates a CSV report at `migrations/reports/phase0_<ts>.csv` for every merge. Idempotent — safe to re-run.
  - **`source` field standardised** to `{type, talent_email, reference_id}` across all 68 existing talents. Any future write that supplies a string is silently re-shaped via the standardise helper.
  - **DB-level unique indexes**:
    - `talents.email` (unique, partial filter so legacy email-less rows don't violate)
    - `applications.talent_email` (unique)
    - `submissions (project_id, talent_email)` (unique compound)
    - All created idempotently in `core.ensure_indexes` so they survive future boots.
  - **Merge logic unified**: submission finalize now uses the same broad `{$or: [{email}, {source.talent_email}]}` lookup as `/apply` approval. Both paths handle `DuplicateKeyError` races by re-fetching the winner.
  - **Admin "Add Talent" now upserts** by email — fills empty fields on the existing record, never inserts a duplicate. Returns the canonical talent (whether merged or new).
  - **Admin "Edit Talent" rejects email collisions** with a 409 instead of silently breaking the unique index.
  - **Application + submission start endpoints** now respond `resumed: true` with the existing record's id when the unique index fires (race-safe).
  - **Prefill endpoint rate-limited**: `/api/public/prefill` capped at 20 lookups / 60 s / IP via a sliding-window in-process counter (replace with Redis when we run multi-replica). Returns 429 on excess.
  - **Tests**: 7 new pytests (`tests/test_phase0_identity.py`) — all pass. Full backend regression 39/39 on Atlas.

- **2026-04-25 (v27)** — **Sprint 3: Client Decision Experience.** Mobile-first review polish — every decision now reachable by one thumb.
  - **Sticky bottom action bar** on mobile (`<md`) inside `TalentDetail`: `quick-shortlist-btn` (gold) · `quick-hold-btn` (white outline) · `quick-reject-btn` (red), each 52 px tall with `active:scale-[0.97]` + safe-area padding for iPhone notches. Hidden on desktop where the existing in-card action grid is already thumb-reachable.
  - **Auto-advance after action**: tap any quick-action → backend save → 350 ms transition → next talent in the **filtered** list (respects current Pending/Shortlisted/etc. tab). Last talent in list closes the overlay. Includes a per-button spinner during the transition.
  - **Swipe gestures**: native touchstart/touchmove/touchend on the overlay container — left = next talent, right = prev, swipe-down (≥ 110 px from top) = close. 60 px threshold + horizontal-vs-vertical disambiguation prevents accidental triggers during scroll. Horizontally-scrollable children (e.g. take-thumbs strip) opt out via `data-stop-swipe="1"`.
  - **Talent counter pill** (top-left of overlay on mobile): "1 / N" — always visible.
  - **Manual prev/next breadcrumb** at the bottom of the action bar with hint text "← swipe right · prev | N of M | next · swipe left →" so users discover the gesture if they don't try it first.
  - **Haptic feedback**: `navigator.vibrate(10)` on supported devices (Android Chrome) when an action fires.
  - **State persistence verified**: shortlists from a previous session re-paint the gold pill on the action bar via the existing M5 `client_states` backend.
  - **Tap targets**: all 3 quick-action buttons + close button + nav arrows ≥ 44 px.
  - **Verified on iPhone 12 viewport (390 × 844)**: created a 3-talent fixture, opened the first card → tapped Shortlist → auto-advanced to the second card → tapped Next → advanced to the third card. Heading + counter pill update correctly throughout.

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
  - `backend/.env` switched to MongoDB Atlas (connection string in `.env`, not committed) with `DB_NAME=talentgram`.
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

- **2026-04-27 (v37d)** — **Admin password persistence fix.** Removed the env-driven `password_hash` rewrite from `seed_admin()` in `core.py`. Previously, every backend restart re-ran `verify_password(ADMIN_PASSWORD, …)` and silently overwrote the admin's hash back to env value, reverting any UI-driven password change. Seed is now strictly insert-if-missing on `password_hash`; role/status repair preserved. Verified: password changed via `/api/auth/change-password` → backend restart → new password still works, old env password rejected. To rotate admin password, use the in-app change/forgot-password flow (env `ADMIN_PASSWORD` only seeds first-boot).
- **2026-04-27 (v37c)** — **Media optionality (balanced).** `/submit` (audition submission) is now FULLY form-only — no minimum images, no required intro video, no required take. Talents pressed for time can ship and add media via Refine later. `/apply` (onboarding application) keeps a single hard requirement: at least 1 portfolio/headshot image (recognisable photo for admin review); intro video and additional images are optional/recommended. Backend: `submission_finalize` strips intro/take/MIN_IMAGES 400s; `finalize_application` reduces validation to `img_count < 1`. Frontend: SubmissionPage disabled-button hint trimmed to form fields; ApplicationPage labels now read "Introduction Video (optional)" and "Profile / Headshot Image *" with sub-hint "Upload at least 1 clear profile/headshot image (required). Add more (up to 8)…". Verified by 8/8 backend pytest + source-grepped UI labels (iteration_12.json).
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

## v37m — Cloudinary Migration (Apr 28, 2026) ✅ COMPLETE
- Storage layer fully migrated from Emergent Object Storage to Cloudinary.
- **Backend**: `cloudinary_upload` / `cloudinary_destroy` helpers in `core.py`; all routers (`talents`, `submissions`, `applications`, `projects`, `feedback`) write to Cloudinary. Legacy `init_storage`/`put_object`/`get_object`/`stream_object` removed. `/api/files/*` proxy removed.
- **Frontend**: `FILE_URL` helper deleted from `lib/api.js`; all 13+ components/pages refactored to render `media.url` directly. `IMAGE_URL(media)` retained as a thin pass-through (`media.url`) for backward call sites (`ClientView.jsx`).
- **Data backfill**: `scripts/migrate_emergent_to_cloudinary.py` migrated 152/252 legacy media records (additive — `storage_path` preserved). 100 failures are 1×1 placeholder JPEGs (537 bytes, test data) which Cloudinary rejects as invalid; documented as expected.
- **Validation**: 9/9 backend tests passing (`tests/test_cloudinary_migration.py`); admin smoke + client view + submission page all render Cloudinary thumbnails.

## v37n — Submission UX Premium Redesign (Apr 30, 2026) ✅ COMPLETE
- **Backend**:
  - New `_resolve_cover_url()` helper resolves: `cover_media_id` → portfolio/indian/western image → first usable `url`. Used by `enrich_talent`, `_submission_to_client_shape`, `_filter_talent_for_client`, `_with_image_url`.
  - Every talent / submission / application response now exposes a top-level `image_url` (Cloudinary URL or `null`, never `"undefined"`).
  - `/api/public/prefill` enriched with `image_url` and DOB-derived age fallback for the "Is this you?" confirmation card. 20/min IP rate-limited.
  - `MediaItem` model dropped optional legacy `storage_path`; `_public_media` now returns clean Cloudinary fields only. DB cleanup script stripped 396 legacy fields across 37 docs.
  - Removed hardcoded Cloudinary credentials from `server.py` (now solely env-based via `core.py`). Fixed malformed `.env` (separated `GOOGLE_DRIVE_SA_KEY_JSON` newline + dedupe).
- **Frontend**:
  - New `COVER_URL(subject)` helper in `lib/api.js` — single source of truth for image resolution. Used in TalentList, Applications, LinkGenerator, ClientView, ForwardToLinkModal.
  - Talent grid bug fixed: `<Inner>` was receiving stale `anyImg` prop after migration → switched to `coverUrl={COVER_URL(t)}`.
  - **Submission page redesign** (gold-themed Netflix-luxury aesthetic):
    - "Is this you?" prefill card now shows Cloudinary thumbnail + name + age + location + height
    - Age handling: when DOB present, shows read-only auto-calculated age + optional override input ("Override age for this submission") with helper text. Override stays in `form_data.age_override` (never overwrites global talent profile)
    - Budget block: prominent gold card with `Client Budget: ₹X / day`, two large CTAs ("Accept this budget" / "Propose your own"), smooth grid-rows reveal animation, secondary commission line
    - Availability block: gold card with `Shoot Dates`, two large CTAs ("Yes, available" / "Not available"), smooth reveal of details textarea
    - Important Questions section: gold-bordered card with Lightbulb icon + "RECOMMENDED" tag + helper text "*These help us shortlist you better. Not mandatory.*"
    - Work Links: chips with X buttons, paste/Enter to add, comma/whitespace split for multi-paste, backspace-on-empty removes last chip, helper text "Customize links for this project only"
  - Premium polish CSS layer (`index.css`):
    - Gold accent classes (`bg-[#c9a961]/[0.06-0.07]`, `border-[#c9a961]/40-50`) properly themed for both modes
    - Placeholder contrast bumped to `white/55+` (dark) / `black/45+` (light) for AA compliance
    - `tg-card` utility (subtle 1px highlight in dark, soft glow in light)
    - `tg-divider` utility for clean section separation
    - `tg-help` utility for accessible helper text
- **Validation**: testing_agent_v3_fork iteration 17 → **10/10 backend pytest pass + 100% frontend Playwright** (admin grid, prefill + autofill, age override, budget redesign, availability redesign, work-link chips, theme toggle preserves gold accents in light mode).

## Test Credentials
See `/app/memory/test_credentials.md`.
