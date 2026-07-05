# Decision Log

Major architectural and business decisions discovered in repository evidence. Each entry includes the decision, rationale, implementation location, and the commit or code that establishes it.

---

## D1: Admin is Source of Truth

**Decision**: `db.talents` is the canonical record for all talent data. Admin-managed data takes precedence.

**Rationale**: Talent data flows in from multiple sources (self-service applications, project submissions, admin edits). Without a single canonical source, data conflicts would be unresolvable. The admin-managed talent record serves as the merge target.

**Implementation**: `backend/core.py` -- `merge_talent_profile()` function with field classification (AUTO_UPDATE, REVIEW, PRESERVE). REVIEW fields (name, dob, gender, height, ethnicity) are only filled if the master record is empty; conflicts are logged but master is never overwritten.

**Established by**: Commit `6b39e31` (2026-06-17) "Phase 1 Talent Identity Hardening & Data Integrity" and commit `fde5125` (2026-06-17) "Phase 1.6 Production Hardening" which formalized the field classification system.

---

## D2: Audition Takes Never Sync to Global Talent

**Decision**: Audition take categories (`take`, `take_1`, `take_2`, `take_3`) must never sync from submissions to the global talent record in `db.talents`.

**Rationale**: Audition takes are project-specific recordings. Syncing them to the global talent profile would contaminate the canonical portfolio with project-specific content and violate client confidentiality (auditions for one client visible to another).

**Implementation**: `backend/core.py` -- `sync_media_to_global_talent()` uses `cat_mapping` which excludes all take categories. Only `image/portfolio`, `indian`, `western`, `video/intro_video`, `headshot`, and `additional_portfolio` categories are in the mapping.

**Established by**: Commit `e9489db` (2026-06-18) "Phase 1.7 Canonical Profile and Media Sync refactor".

---

## D3: F1 Rule -- Blank Values Never Overwrite Master

**Decision**: When syncing application data to `db.talents` during finalize, blank/empty values (`None`, `""`, `[]`, `{}`) are filtered out before writing.

**Rationale**: Prevents a partially-filled application from wiping existing talent data. A talent who submits only their name and email should not erase their existing portfolio, location, or bio.

**Implementation**: `backend/routers/applications.py:900`:
```python
update = {k: v for k, v in update.items() if v not in (None, "", [], {})}
```

**Established by**: Commit `d7c1657` (2026-06-22) "fix(talent-sync): prevent application finalize from wiping master with blanks".

---

## D4: Direct Cloudinary Upload (Architecture C)

**Decision**: Implement direct browser-to-Cloudinary video uploads, bypassing the backend proxy entirely for video content.

**Rationale**: Video files are large (up to 200MB). Proxying through the FastAPI backend consumes Railway bandwidth and memory, increases upload time, and risks timeouts. Direct upload sends bytes straight to Cloudinary while the backend only handles signing and confirmation.

**Implementation**:
- Backend: `POST /public/submissions/{sid}/video-signature` generates signed Cloudinary params; `POST /public/submissions/{sid}/video-complete` confirms upload
- Frontend: `frontend/src/lib/directVideoUpload.js` handles 20MB chunked upload to Cloudinary
- Feature-flagged: `DIRECT_VIDEO_UPLOAD` (backend) / `NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD` (frontend), defaults to `false`

**Established by**: Commit `f8f9ad4` (2026-06-23) "feat(audition-video): direct browser->Cloudinary upload (Architecture C, flag-off)".

**Status**: Implemented but disabled in production. Flag defaults to `false`.

---

## D5: Subdomain-Based Multi-Tenant Routing

**Decision**: Use separate subdomains for each application surface (`apply.`, `submit.`, `review.`, `links.`) with Next.js Edge Middleware rewriting requests to internal route groups.

**Rationale**: Clean separation of concerns between public talent-facing pages, admin dashboard, and client review links. Each subdomain can have its own SEO metadata, security context, and CORS rules.

**Implementation**: `frontend/src/middleware.ts` inspects the hostname and rewrites to internal Next.js route groups: `(apply)`, `(submit)`, `(review)`, `(links)`.

**Established by**: Commit `b4b6e9f` (2026-06-14) "feat: migrate to Next.js App Router and implement Edge Middleware subdomain routing". Followed by ~15 iterative fix commits over 6 hours to resolve blank pages, middleware conflicts, and hydration issues.

---

## D6: React Router Inside Next.js for Admin and Portal SPAs

**Decision**: The admin dashboard (`/admin`) and talent portal (`/portal`) use React Router inside Next.js App Router catch-all routes, rather than native Next.js file-based routing.

**Rationale**: The admin dashboard and portal are single-page applications with complex client-side navigation, modals, and state management. Converting all admin routes to Next.js file-based routing would require significant refactoring of the existing SPA architecture (originally built with CRA/react-router).

**Implementation**: `frontend/src/app/(review)/admin/[[...catchall]]/page.jsx` renders `AdminApp.jsx` which uses React Router. Similarly, `frontend/src/app/portal/[[...catchall]]/page.jsx` renders `PortalApp.jsx`.

**Trade-off**: These SPAs lose SSR/SSG benefits. All admin and portal pages are client-rendered.

**Established by**: Part of the Next.js migration in commit `b4b6e9f` (2026-06-14).

---

## D7: Email Delivery Cascade

**Decision**: OTP emails use a three-tier cascade: Resend > SendGrid > AWS SES, with automatic fallback.

**Rationale**: Email delivery is critical for talent authentication (OTP-based login). No single provider guarantees 100% deliverability. The cascade ensures emails are sent even if the primary provider is down.

**Implementation**: `backend/core.py` -- email sending function tries Resend first, falls back to SendGrid, then to AWS SES. In development, falls back to a mock that logs the OTP.

**Established by**: Commit `068158e` (2026-06-12) "feat: upgrade OTP email system to branded HTML".

---

## D8: Token Version Invalidation

**Decision**: Every password change bumps `token_version` on the user record, instantly invalidating all prior JWTs.

**Rationale**: When an admin changes their password (or an admin resets another user's password), all existing sessions should be invalidated immediately. The JWT `tv` (token version) claim is checked against the stored `token_version` on every authenticated request.

**Implementation**: `backend/routers/password.py` increments `token_version` on password change. `backend/core.py` `current_user` dependency rejects JWTs where `tv < stored_version`.

**Established by**: Part of the auth system, hardened in commit `15b4a92` (2026-06-14) "Security Remediation".

---

## D9: Privacy-First Client Review Links

**Decision**: Client review links privatize talent names to "First L." format, hide budget by default, and disable downloads by default.

**Rationale**: Talent identity is sensitive. Clients should evaluate talent based on portfolio and audition, not name. Budget information is only shared when explicitly enabled by admin. Downloads are disabled to prevent unauthorized distribution.

**Implementation**:
- `frontend/src/pages-components/ClientView.jsx` -- `privatizeName()` function
- `backend/core.py` -- `DEFAULT_VISIBILITY` sets `budget: False`, `download: False`
- Two-layer visibility: link-level category toggles + per-submission field toggles
- `CLIENT_ALLOWED_FIELDS` strict allowlist as final gate

**Established by**: Part of the link system architecture, with visibility controls refined in commits `7fbcadd`, `cea66e2` (2026-06-12).

---

## D10: Binary File Signature Validation

**Decision**: Validate uploaded files by magic bytes, not just file extension or MIME type.

**Rationale**: File extensions and MIME types can be spoofed. Magic byte validation ensures the file content actually matches the declared type, preventing upload of malicious files disguised as images or videos.

**Implementation**: `backend/core.py` -- `cloudinary_upload()` reads the first bytes of every file and validates against known signatures for JPEG, PNG, WebP, HEIC/HEIF, MP4, MOV, and PDF. Additionally cross-checks declared content-type against detected type.

**Established by**: Commit `15b4a92` (2026-06-14) "Security Remediation: implement file magic-byte validation".

---

## D11: No CI/CD Pipeline

**Decision**: Ship directly from feature branches merged to main, with no automated test pipeline.

**Rationale**: Speed of iteration. The project is in rapid development phase (100 commits in 12 days) with a small team. The overhead of CI/CD setup was deprioritized in favor of shipping features.

**Trade-off**: No automated test runs before deploy. Regressions can reach production unchecked. This is documented as a known issue in [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md).

**Status**: Active gap. Should be addressed as the project stabilizes.

---

## D12: MongoDB as Primary Database

**Decision**: Use MongoDB Atlas as the sole database, with Motor (async driver) for all data access.

**Rationale**: Document-oriented storage fits the talent profile model well -- profiles have varying fields, nested media arrays, and flexible schemas. The async Motor driver integrates naturally with FastAPI's async architecture.

**Implementation**: `backend/app/core/core.py` -- Motor client with connection pool (maxPoolSize=50). Single database `talentgram` with 37+ collections.

**Trade-off**: No relational integrity enforcement. Deduplication and consistency must be handled in application code (e.g., unique indexes, merge functions, audit trails).

---

## D13: Opaque Access Tokens as JWT Fallback

**Decision**: In addition to short-lived JWTs, store a persistent opaque `access_token` (256-bit `secrets.token_urlsafe(32)`) on submission and application records.

**Rationale**: Talent users may return days or weeks later to resume a submission. JWTs expire (3-7 days), but the opaque token persists in the database. When the JWT expires, the system can fall back to matching the opaque token stored in localStorage (`tg_atk_{slug}`).

**Implementation**: `backend/routers/submissions.py` and `backend/routers/applications.py` -- token stored on the record, rotated on auth events, matched verbatim against DB.

**Established by**: Refined in commits `5c15bc2`, `ec2f224` (2026-06-16) "fix(resume-flow): rotate and persist resume tokens".

---

## D14: Theme Locked to Light Mode

**Decision**: The `useTheme()` hook permanently returns light mode. The theme toggle is a visual no-op.

**Rationale**: Described in code as "ATS operational design guidelines" -- the agency's branding and design system is built for light mode only.

**Implementation**: `frontend/src/hooks/useTheme.js` -- always returns `"light"`.

**Trade-off**: Toggle exists in UI but does nothing, which may confuse users. Documented as a known issue.

---

## D15: Always-Live Client Rendering (No Frozen Snapshots)

**Decision**: The Client Review Link, Review Centre Client View, download bundle, client PDF, and individual media serve endpoint all render live from the same shaping/filtering pipeline (`_submission_to_client_shape` + `_filter_talent_for_client`). No frozen `client_package_snapshot` is used for rendering.

**Rationale**: A frozen snapshot was silently keeping recruiter visibility edits from reaching the Client Review Link (the link short-circuited to the snapshot captured at approval time). A single always-live engine eliminates the divergence class entirely and makes the Review Centre Client View a true live preview of the client link.

**Implementation**: `backend/core.py:_submission_to_client_shape()` -- the `if sub.get("client_package_snapshot"): return snap` short-circuit was removed. The auto-write on approve and the PUT `regenerate_snapshot` branch were also removed. `generate_submission_snapshot()` and `POST /api/projects/{pid}/submissions/{sid}/snapshot` are retained for one release (dormant) to protect any lingering external caller.

**Established by**: Commit `1b15075` (2026-07-02) "feat(review-center): single live client pipeline, Client/Hidden visibility, UX fixes".

---

## D16: Client / Hidden Two-State Visibility Model

**Decision**: Media and field visibility is Client / Hidden only. The third state ("Internal") was removed.

**Rationale**: "Hidden" and "Internal" both mean "the client cannot see this" -- the only relevant distinction from a recruiter's perspective. The third state added UI complexity without carrying operationally distinct semantics.

**Implementation**:
- Frontend: `MediaVisControls` in `frontend/src/pages-components/SubmissionReviewCenter.jsx` renders two buttons. Any legacy `internal_only: true` displayed as "Hidden".
- Backend write: PUT handler folds `internal_only: true` into `client_visible: false` and strips the deprecated flag before persisting.
- Backend read: `_submission_to_client_shape` and `_filter_talent_for_client` still exclude both `client_visible: false` and `internal_only: true` for safety.
- Migration: `backend/migrations/remove_internal_visibility.py` (idempotent) cleans historical documents.

**Established by**: Commit `1b15075` (2026-07-02).

---

## D17: Resubmission Never Overwrites Global Talent Profile

**Decision**: A submission that has already been finalized once cannot mutate `db.talents`. Every post-first-submit workflow -- resubmit, update, replace media, edit, admin-reopen → submit again -- is treated as a project-specific correction and skips the sync.

**Rationale**: A talent may edit a submitted audition weeks later to swap or hide media for one client. That project-specific correction must not become the canonical portfolio -- otherwise a future project auto-loads the wrong (project-A-scoped) media instead of the latest true master profile. Only the FIRST successful submission (and the separate Talent Invite / Profile Update flow) should update the master.

**Implementation**: New helper `has_been_submitted_once(sub)` in `backend/routers/submissions.py`, keyed on the monotonic `submitted_at` (never cleared by any current or foreseeable edit flow) with a `status in {submitted, updated}` fallback. Guards every write-to-global path: upload mirror (submissions.py:801), upload replace-removal, signed/complete upload (1088), media delete (1165), finalize field-merge and media re-sync (1967), and the async Cloudinary webhook intro-video replace path (webhooks.py).

**Established by**: Commit `8cfb1b5` (2026-07-02) "fix(submissions): always start at landing page; never sync resubmissions to global profile".

---

## D18: Immediate Visibility Persistence (No Debounce)

**Decision**: Media and field-visibility toggles in the Review Centre persist to Mongo the instant they change, with no debounce. Saves are serialized so overlapping toggles apply in order, and the decision POST awaits any in-flight visibility save.

**Rationale**: The Review Centre Client View filters local React state; the Client Review Link reads Mongo. If the toggles only mutate React state (as they did before this decision), the preview looks right but the DB was never written -- and Approve publishes stale visibility to the client. Debouncing would only mask the race. Immediate persistence + explicit await in `executeDecision` proves the DB is up to date before any decision publishes.

**Implementation**: `frontend/src/pages-components/SubmissionReviewCenter.jsx` -- a single effect over `mediaList`, `talentPortfolioMedia`, `fv` diffs against the last-persisted baseline via `curationSig()` and fires the PUT immediately. `savePromiseRef` serializes overlapping saves. `executeDecision` awaits `savePromiseRef.current` (and flushes any not-yet-captured change) before the decision POST.

**Established by**: Commit `eb8c057` (2026-07-03) "fix(review-center): persist media/field visibility immediately (no debounce)".

---

## D19: Viewed = Explicitly Opened

**Decision**: A submission is marked viewed only when the client explicitly opens its detail (card click, modal prev/next navigation, or Resume banner). Rendering the card list, scrolling, prefetching, and landing on the page do not count as viewed. No timer counts as viewed.

**Rationale**: Auto-marking on visibility (via IntersectionObserver) inflated analytics -- a client who landed and left after 30 seconds looked like they had actively reviewed the talent. The header "N / M viewed" is a decision-support signal for the recruiter; it must reflect actual engagement.

**Implementation**: `frontend/src/pages-components/ClientView.jsx` -- removed the `TalentCard` IntersectionObserver and the 15s auto-review timer inside the detail view. `markSeen` fires only from `handleOpen` (card click), `onNavigate` (modal keyboard/swipe/click), and the Resume button. Server `/seen` uses `$addToSet` so the first-viewed state is idempotent. Header counter reads `seenCount = talents.filter(t => seenIds.has(t.id)).length`.

**Established by**: Commit `b1c902a` (2026-07-04) "fix(client-link): only mark a submission viewed when its detail is opened".

---

## D20: Every Project Link Always Starts On The Landing Page

**Decision**: Every project submission link opens on the landing / OTP page. No global cross-project session can auto-unlock a new project's gate.

**Rationale**: On iPhone Safari, a returning talent who previously authenticated on Project A was skipping the landing on Project B -- landing on a bare form with a silently-sent OTP hidden behind it, producing the "stuck, nothing loads" report. Only a signal proving authentication happened for THIS project should skip the gate.

**Implementation**: `frontend/src/pages-components/SubmissionPage.jsx` -- the email-gate initial state and mount effect were rewritten. Only two signals unlock the gate: a per-slug JWT/ATK submission session (already handled by resume effects), or a per-slug `tg_google_done_<slug>` marker written by `GoogleCallback.jsx`. Global `talentgram_portal_email` / `talentgram_google_*` do not bypass. Deep-link `?email=` pre-fills the landing field and sends the OTP but keeps the gate LOCKED.

**Established by**: Commit `8cfb1b5` (2026-07-02) — bundled with D17.

---

## D21: Recognition-Based Modal Handling (WhatsApp Worker)

**Decision**: WhatsApp Web dialogs are handled by a recognition-based framework that only dismisses dialogs matching a known-benign registry. Unrecognized dialogs are never touched — they are captured, logged, and the send fails gracefully (retryable).

**Rationale**: WhatsApp Web periodically shows `aria-modal="true"` announcement dialogs that intercept all pointer events on the page. Playwright's actionability checks correctly refuse to click through overlays, causing every job to time out. Force-clicking or blindly clicking any button could inadvertently confirm dangerous dialogs (logout, delete chat, block contact). A recognition-based approach ensures only known-safe dialogs are dismissed and new/unknown dialogs are surfaced for human review before any action.

**Implementation**:
- `whatsapp-worker/modals.py`: `dismiss_blocking_dialogs(page, context)` is called at login, pre-open, search, and pre-send.
- Registry: `KNOWN_DIALOG_PATTERNS` (casefolded regex list); currently matches "what's new on whatsapp", "turn on notifications", "whatsapp is now faster".
- Dismissal priority: Escape → `button[aria-label="Close"]` / `[role="button"][aria-label="Close"]` → exact-label whitelist (`Continue`, `Got it`, `OK`, `Not now`, `Done`, `Dismiss`). Never `force=True`, never clicks an unrecognized button.
- Unknown dialogs: screenshot + dialog HTML + title/body stored to `whatsapp_dom_snapshots` collection, logged as `UNKNOWN_DIALOG`, function returns `False` (caller fails gracefully, job retries).
- Every encounter emits a structured `DIALOG_EVENT` JSON log line: `{context, title, body, method, success, outcome}`.

**Safety rules**: Never force-click. Never click an unlisted button. Never dismiss an unrecognized dialog. Structured logging on every encounter — nothing is silent.

**Established by**: Commit `914b1fa` (2026-07-04) "fix(whatsapp): dismiss blocking WhatsApp Web dialogs before every interaction".

---

## D22: Backend Runs On Railway (Not Emergent)

**Decision**: The FastAPI backend is deployed to Railway (`talentgram-railway` service, root `/backend`, `uvicorn server:app`). The `.emergent/emergent.yml` file that suggests an Emergent base image is historical (dated 2026-05-16) and does not drive the live deploy.

**Rationale**: Consolidating the backend API onto the same Railway account that already hosted the WhatsApp worker simplified operations (one dashboard, one CLI, one plan). Railway's Nixpacks builder handles the FastAPI app without a Dockerfile.

**Implementation**: Railway service `talentgram-railway` (service ID `b3242fe8-67b3-4d5d-9fce-a875d05b58ce`) in project `pacific-art`. Source: `rajbhansali92/talentgram-frontend`, branch `main`, root `/backend`. Public URL: `https://talentgram-app-production.up.railway.app`. Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`. Health: `GET /health`.

**Trade-off**: See open issue #0 -- GitHub → Railway auto-deploy status is uncertain (may have reconnected as of 2026-07-04; verify on next push).

---

## D23: Talent Invite Thank-You Screen Has No Edit Path

**Decision**: The "Edit Profile" button is removed from the Talent Invite finalized/Thank-You screen. Once finalize succeeds, the flow is terminal; there is no in-app path back into editing that draft.

**Rationale**: Per D17, only two workflows may ever write to the Global Talent Profile — the Talent Invite flow and a talent's first project submission (pre-finalize). Once Talent Invite finalize succeeds, that write is complete; there's no data-integrity reason to route the user back into editing immediately, and doing so encouraged unnecessary re-edits. A recruiter who wants a talent to update their profile can send a new invite link.

**Implementation**: `frontend/src/pages-components/ApplicationPage.jsx` — removed the button (previously called `enableEditing()`, which POSTed `/public/apply/{aid}/edit` to reset the application to `draft` status) and the now-orphaned handler. The backend `/edit` endpoint is untouched (still reachable, just unreferenced by this UI path). `isEditMode` state and the "Profile Updated"/"Saved" text variants remain — they're still reachable via the separate returning-talent resume paths (hydrating a `status: "submitted"` draft), which is unrelated to this button.

**Established by**: Commit `ac0ce31` (2026-07-05) "fix(application): remove Edit Profile button from Talent Invite Thank You screen".

---

## D24: Prevent Upload-Manager/CTA Overlap By Repositioning, Not Z-Index

**Decision**: When the floating upload-progress overlay (`FloatingUploadManager`) and a flow's sticky submit-CTA footer would otherwise occupy the same screen region, the overlay is repositioned to float clear of the footer's actual rendered height — not simply painted on top or below it via a z-index change.

**Rationale**: `FloatingUploadManager` (`fixed`, `z-50`, bottom-anchored) and each flow's sticky CTA footer (`sticky bottom-0`) were built independently and collide on narrow (mobile) viewports whenever an upload is active, visually covering the submit button. A z-index change only decides which element wins the overlap — it doesn't stop them from occupying the same space, and would just move the same bug onto whichever element loses. The fix must also not hardcode a pixel offset, since the footer's height varies by device safe-area inset, conditional warning text, and iOS toolbar-driven reflows.

**Implementation**: New hook `frontend/src/hooks/useStickyFooterHeightVar.js` observes the sticky footer element via `ResizeObserver` and publishes its live rendered height to a CSS custom property (`--tg-sticky-cta-h`) on `<html>` — consistent with this codebase's existing `--tg-*` design-token convention (see `index.css`). `FloatingUploadManager`'s `bottom` offset became `calc(var(--tg-sticky-cta-h,0px) + 1rem)` (`+1.5rem` on `sm:`) instead of the static `bottom-4`/`bottom-6`, so it always floats above the CTA regardless of the CTA's actual height. Falls back to the original fixed gap on any page without a sticky footer (the variable is simply unset there — no regression). Applies to both `ApplicationPage.jsx` and `SubmissionPage.jsx`, since both share the same `UploadManagerContext`/`FloatingUploadManager` singleton.

**Established by**: Commit `e2a2cd2` (2026-07-05) "fix(upload): prevent floating upload manager from covering the sticky submit CTA".

---

## D25: Talent Invite Draft Ownership By Resolved Identity (Email-Scoped Storage)

**Decision**: The Talent Invite local draft cache is namespaced by the talent's normalized email — not a single global slot. On mount, the *intended identity* is resolved first (explicit `?email=` invite → verified portal session → Google session → otherwise the newest local draft, only when none of those exist), and only that identity's slot is ever read. A stale draft belonging to a different email can never override the invite/session context the talent is actually in. The backend document remains authoritative; local storage is a convenience cache, never an overriding session.

**Rationale**: Reported symptom: refreshing a Talent Invite link could restore an older draft created under a different email, because the single global key `tg_application` was restored unconditionally on mount with no identity check — a direct precedence inversion (stale local cache > invite context > backend truth). `SubmissionPage` already avoided this class of bug by namespacing its draft/session keys per project `slug` (see D20) and never letting a global cross-project session unlock a new project's gate; this decision applies the same principle to Talent Invite using the identity that's actually available there (email, since there's no project slug) instead of retrofitting an unrelated namespace.

**Implementation**:
- Ownership model: the application id (`aid`) remains the backend record identifier and the JWT remains the credential — both live *inside* the per-email value, never in the key, exactly mirroring how `SubmissionPage` keeps `id`/token inside its per-slug value.
- Key scheme extracted to `frontend/src/lib/applyDraft.js` (single source of truth): `normEmail()` matches backend `core.normalize_email()` exactly (`strip().lower()`, no plus/dot folding); `emailDigest()` is a deterministic FNV-1a → 8-hex digest (key hygiene, not a security boundary — the value still holds PII under the existing 30-day TTL); `appDraftKey(email)` = `tg_application_<digest>`.
- Mount precedence (`ApplicationPage.jsx`): identity resolution order is `?email=` invite → verified portal session (`talentgram_portal_token` + `talentgram_portal_email`) → Google session (returns earlier in the same effect) → newest local draft across all per-email slots, consulted *only* when none of the above exist. The resolved identity's slot is the only one ever read; a legacy `tg_application` slot is migrated into a per-email slot on first load **only if its stored email matches the resolved identity** (TTL/`savedAt` preserved via a verbatim copy), otherwise left untouched and TTL-bounded.
- `GoogleCallback.jsx`'s apply-resume branch writes `appDraftKey(data.email)` directly (imports the same shared key scheme) rather than the legacy key — storage destination only; the OAuth exchange, `portal_token` persistence, and redirect are unchanged. No live code path writes the legacy key anymore, so the migration only needs to survive a deprecation window for drafts already in users' browsers before it can be removed (tracked in [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md)).
- Project Submission's per-`slug` storage (`tg_submission_{slug}`, `tg_draft_{slug}`, `tg_atk_{slug}`) is untouched.

**Established by**: Commit `c80a3c9` (2026-07-05) "fix(apply): namespace Talent Invite draft cache by identity, not one global slot".
