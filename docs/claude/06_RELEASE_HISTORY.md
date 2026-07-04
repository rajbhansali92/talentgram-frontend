# Release History

## Timeline of Major Releases

This timeline covers repository history through July 4, 2026. The project has no tags or formal release branches; releases are tracked by merge commits and significant feature commits to `main`.

---

### 2026-07-04: Client Link Viewed-Tracking Fix

| Field | Value |
|---|---|
| Commit | `b1c902a` |
| Date | 2026-07-04 |
| Purpose | Stop auto-marking submissions viewed on landing / scroll |
| Impact | Removed the `TalentCard` IntersectionObserver and the 15s auto-review timer. `markSeen` now fires only on explicit open (card click, modal prev/next, Resume). Header counter switched from decision-based (`reviewedCount`) to opened-based (`seenCount = filter(seenIds.has)`). Server `/seen` uses `$addToSet` for first-view idempotency. Frontend-only; no new endpoints. |

### 2026-07-03: Immediate Visibility Persistence (Review Center)

| Field | Value |
|---|---|
| Commit | `eb8c057` |
| Date | 2026-07-03 |
| Purpose | Persist media / field-visibility toggles to Mongo immediately |
| Impact | Root cause of the earlier "hidden media still appears on the Client Review Link" report was persistence: the Hide toggles only mutated React state; the Client View preview filtered that local state (so it looked right), but the DB was never written until an explicit "Save Project Overrides" click. Toggles now PUT `/api/projects/{pid}/submissions/{sid}` on every change, serialized so overlapping toggles apply in order. `executeDecision` awaits any in-flight visibility save before the decision POST, eliminating any Hide → Approve race. |

### 2026-07-02: Single Live Client Pipeline + Client/Hidden Visibility + Review Centre UX

| Field | Value |
|---|---|
| Commit | `1b15075` |
| Date | 2026-07-02 |
| Purpose | Make Review Centre Client View and every client-facing renderer share one shaping/filter engine; simplify visibility to Client / Hidden; remove Google Drive folder button |
| Impact | Removed the `client_package_snapshot` render short-circuit in `_submission_to_client_shape` — every client surface (link, PDF, bundle, slideshow, individual media serve, Review Centre preview) now renders live from the same submission + visibility engine. Snapshot infra retained one release, deprecated. Media/field visibility collapsed from 3 states (Client/Hidden/Internal) to 2 (Client/Hidden); legacy `internal_only` is folded on read/write; migration `backend/migrations/remove_internal_visibility.py` cleans historical rows. Removed the "Open Google Drive folder" button and `GET /api/submissions/{sid}/drive` endpoint (Cloudinary-only). Languages / Special Abilities dropped from recruiter and client shapes (duplicate of Skills). Safari decision-bar disappearance fixed via `min-h-0` on the scroll flex child. Decision-change confirmation modal only when changing an already-registered decision. |

### 2026-07-02: Submission Landing Page Always Shown + Resubmissions Never Sync

| Field | Value |
|---|---|
| Commit | `8cfb1b5` |
| Date | 2026-07-02 |
| Purpose | Every project link opens on the landing/OTP page; every post-first-submit workflow is treated as a resubmission and never mutates the global talent profile |
| Impact | (Issue 1) Global cross-project sessions (`talentgram_portal_email`, `talentgram_google_*`) no longer auto-unlock a new project's gate. Only a per-slug JWT/ATK session or a per-slug `tg_google_done_<slug>` marker (written by `GoogleCallback`) may skip the landing. Deep-links `?email=` pre-fill the field and send OTP but keep the gate locked. (Issue 2) New helper `has_been_submitted_once(sub)`, keyed on the monotonic `submitted_at` flag with a status fallback, guards every write-to-global path: upload mirror + replace-removal, signed/complete upload, media delete, finalize field-merge, finalize media re-sync, and the async Cloudinary webhook intro-video replace. Test `test_phase3_sync.py` rewritten to the new contract. |

### 2026-06-23: Direct Cloudinary Video Upload (Architecture C)

| Field | Value |
|---|---|
| Commit | `dff9185` (merge), `f8f9ad4` (feature) |
| Date | 2026-06-23 |
| Purpose | Enable direct browser-to-Cloudinary video uploads, bypassing backend proxy |
| Impact | New upload path for audition videos. Feature-flagged OFF by default (`DIRECT_VIDEO_UPLOAD`). 20MB chunked uploads, 5-minute duration guard. Backend generates signed Cloudinary params; browser uploads directly. |

### 2026-06-22: Application Finalize Blank Wipe Fix

| Field | Value |
|---|---|
| Commit | `f4fab14` (merge), `d7c1657` (fix) |
| Date | 2026-06-22 |
| Purpose | Prevent application finalize from wiping master talent data with blank values |
| Impact | Critical data integrity fix. Implements F1 Rule: blank/empty values (`None`, `""`, `[]`, `{}`) are filtered out before syncing to `db.talents`. Prevents partially-filled applications from erasing existing talent data. |

### 2026-06-22: Portal Auth & Submitter Token Hardening

| Field | Value |
|---|---|
| Commit | `8b389a6` (merge), `b7b6d49`, `c49e7dd` |
| Date | 2026-06-22 |
| Purpose | Authenticate talent portal and harden submitter tokens |
| Impact | Security hardening. Portal token persistence made best-effort. Submitter tokens validated against DB-stored access_token. |

### 2026-06-22: Review Center Refactor

| Field | Value |
|---|---|
| Commit | `d391070`, `fe2163c` |
| Date | 2026-06-22 |
| Purpose | Make Review Center the sole entry point for submission review |
| Impact | Removed duplicate row-level Review button and modal. Review Center now displays talent portfolio media alongside submission media. |

### 2026-06-22: Upload Error Handling & Finalize Validation

| Field | Value |
|---|---|
| Commit | `438bb59`, `8dd1dc7` |
| Date | 2026-06-22 |
| Purpose | Fix orphan uploads blocking finalize and clean up failed asset_metadata |
| Impact | Aborted/failed uploads transition to `failed` status. Pending asset_metadata cleaned up on upload error. Prevents orphan uploads from blocking submission finalization. |

### 2026-06-19: WhatsApp Broadcast Engine

| Field | Value |
|---|---|
| Commit | `12e2530` |
| Date | 2026-06-19 |
| Purpose | Implement WhatsApp broadcast messaging system |
| Impact | New major feature. Admin UI page for creating message batches. Backend router for template/batch/job management. Standalone Railway worker (Playwright + Chromium) for browser automation. Circuit breaker, retry logic, audit trail. |

### 2026-06-19: Safari HEIC/HEIF/MOV Support

| Field | Value |
|---|---|
| Commit | `37c38c6` |
| Date | 2026-06-19 |
| Purpose | Support Apple media formats in uploads |
| Impact | Extended file signature verification to accept HEIC, HEIF, and QuickTime MOV files. Critical for iOS/Safari users. |

### 2026-06-18: Phase 1.7 -- Canonical Profile & Media Sync Refactor

| Field | Value |
|---|---|
| Commit | `e9489db` |
| Date | 2026-06-18 |
| Purpose | Establish `db.talents` as single source of truth with bidirectional media sync |
| Impact | Major architecture decision. `sync_media_to_global_talent()` and `remove_synced_media_from_global_talent()` implemented. Application media now syncs to talent on upload (except audition takes). Deduplication by public_id and source IDs. |

### 2026-06-17: Phase 1.6 -- Production Hardening

| Field | Value |
|---|---|
| Commit | `fde5125` |
| Date | 2026-06-17 |
| Purpose | Identity merge classification, media dedup, approval idempotency, Safari fallback, HEIC support, project deletion safety |
| Impact | Major stability release. Field classification system (AUTO_UPDATE, REVIEW, PRESERVE, IGNORE) formalized. Media deduplication by public_id. Project deletion cascades to Cloudinary cleanup. |

### 2026-06-17: Phase 1 -- Talent Identity Hardening & Data Integrity

| Field | Value |
|---|---|
| Commit | `6b39e31` |
| Date | 2026-06-17 |
| Purpose | Harden talent identity management and ensure data integrity |
| Impact | Foundation for canonical talent system. Email-based dedup, merge behavior defined, audit trails for profile changes. |

### 2026-06-16-17: Application Resume Flow Fixes

| Field | Value |
|---|---|
| Commits | `1f90ccd`, `6a07c6c`, `8e815ac`, `ec2f224`, `5c15bc2`, `a2f19fd` |
| Date | 2026-06-16 to 2026-06-17 |
| Purpose | Fix application resume flow -- draft hydration, token rotation, reconciliation |
| Impact | Lazy reconciliation from talent profile on GET. Case-insensitive email lookup. Token rotation in OAuth and OTP paths. Prevented autosave of finalized applications. |

### 2026-06-16: Onboarding Requirements Engine

| Field | Value |
|---|---|
| Commit | `ccc4ad6` |
| Date | 2026-06-16 |
| Purpose | Configurable profile and portfolio requirements for onboarding |
| Impact | Admin can set fields as required/optional. Custom per-profile configs. Finalize validates against config before submission. |

### 2026-06-16: Upload Manager Consolidation

| Field | Value |
|---|---|
| Commit | `d6c8aac` |
| Date | 2026-06-16 |
| Purpose | Consolidate invite and submission upload flows |
| Impact | `SharedUploadManager` and `FloatingUploadManager` created as reusable components. Upload progress overlay, retry logic, state machine. |

### 2026-06-16: Mobile Responsive Audit

| Field | Value |
|---|---|
| Commits | `587e659`, `c270fb7`, `6044e5c` |
| Date | 2026-06-16 |
| Purpose | Full mobile/iOS Safari compatibility |
| Impact | DVH viewport units, 16px inputs (prevent iOS zoom), safe-area padding, overflow-x guards. Always-visible delete buttons on touch devices. iOS keyboard handling. |

### 2026-06-15: Talent Workflow Hardening

| Field | Value |
|---|---|
| Commits | `2c64e43`, `f303782` |
| Date | 2026-06-15 |
| Purpose | Prevent auto-creating talents on login, handle duplicates, enforce status lifecycle |
| Impact | DuplicateKeyError handling during application conversion. Cloudinary status verified before finalization. ARCHIVED status filtering. Draft expiration (30 days). Drafts excluded from search/bulk/listing. |

### 2026-06-15: Storage Admin Dashboard

| Field | Value |
|---|---|
| Commit | `e42673e` |
| Date | 2026-06-15 |
| Purpose | Database-first Cloudinary storage management |
| Impact | New admin page for storage analytics. Cost controls. Archive/restore/delete operations. `asset_metadata` and `storage_audit_log` collections. |

### 2026-06-14: Security Remediation

| Field | Value |
|---|---|
| Commit | `15b4a92` |
| Date | 2026-06-14 |
| Purpose | OWASP security hardening |
| Impact | Secured public prefill route. Strict JWT_SECRET enforcement. Google token verification with JWK validation (replacing simple decode). File magic-byte validation. Rate limiting on auth routes. |

### 2026-06-14: Next.js App Router Migration

| Field | Value |
|---|---|
| Commits | `b4b6e9f` through `132bf8f` (series of ~15 commits) |
| Date | 2026-06-14 |
| Purpose | Migrate from CRA/react-router to Next.js App Router with Edge Middleware |
| Impact | Major architectural migration. Subdomain routing via Edge Middleware. SSR support. Dynamic OG image generation. Several iterative fixes for blank pages, middleware conflicts, and hydration issues. |

### 2026-06-13: Client Review Analytics & Intelligence

| Field | Value |
|---|---|
| Commits | `b4e8fee`, `cda7aed`, `f14d8f5` |
| Date | 2026-06-13 |
| Purpose | Complete client link analytics system |
| Impact | Event tracking (open, view, media, video). Download logging. Analytics intelligence dashboard for admins. |

### 2026-06-12: Structured Locations & OSM Autocomplete

| Field | Value |
|---|---|
| Commit | `e514844` |
| Date | 2026-06-12 |
| Purpose | Replace free-text locations with structured location arrays |
| Impact | OpenStreetMap autocomplete selector. Multi-city filtering. Location stored as structured arrays. Defensive handling for mixed formats (string vs array vs object). |

### 2026-06-12: Submission Review Center V2

| Field | Value |
|---|---|
| Commits | `670133d`, `b517faa`, `a5a1cf1` |
| Date | 2026-06-12 |
| Purpose | Major Review Center rebuild |
| Impact | Admin override layer. Editable Q&A. Project-specific media manager. Visibility controls. Media preloading. Keyboard shortcuts. Data integrity and visibility fixes. |

### 2026-06-12: Client Review Enhancements

| Field | Value |
|---|---|
| Commits | `7fbcadd`, `5945d2f`, `cea66e2`, `8039d72` |
| Date | 2026-06-12 |
| Purpose | Bulk actions, theme standardization, visibility sync, review history |
| Impact | Bulk client actions. Separate individual share vs project review center visibility sources. Client review history with backward-compatible migration. |
