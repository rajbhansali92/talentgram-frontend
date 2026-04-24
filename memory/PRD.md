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
