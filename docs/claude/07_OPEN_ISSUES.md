# Open Issues

## Known Issues

### 0. Railway Auto-Deploy From GitHub — Status Uncertain
**Status**: Possibly reconnected (as of 2026-07-04); needs verification on next push
**Impact**: High (if still disconnected)
**Evidence**: Earlier (2026-07-02–03), three pushes (`8cfb1b5`, `1b15075`, `eb8c057`) did NOT auto-deploy — both services were stuck on `840adbe`. However, the 2026-07-04 push of commits `914b1fa`/`1a9d556` appeared to trigger an automatic backend deploy (both services updated without a manual `railway redeploy`). This may indicate Railway's GitHub App reconnected, or a manual redeploy happened to coincide.
**Risk**: If still disconnected, backend pushes silently fail to reach production. If reconnected, the issue is resolved.
**Verification needed**:
1. Push a trivial commit (docs-only or whitespace) and observe whether a new Railway deployment starts within ~30 seconds without a manual redeploy.
2. If it does NOT auto-deploy, reconnect the source in Railway (dashboard → each service → Settings → Source → Disconnect → Reconnect Repo `rajbhansali92/talentgram-frontend`, branch `main`, root `/backend` or `/whatsapp-worker`).
3. Re-authorize Railway's GitHub App if needed (Settings → Applications → Installed GitHub Apps → Railway → Configure → grant access to `talentgram-frontend`).
**Interim workaround** (documented in `05_DEPLOYMENT_RULES.md`):
```
railway redeploy --service talentgram-railway --from-source --yes
railway redeploy --service "talentgram-frontend - whatsapp" --from-source --yes
```

### 1. No CI/CD Pipeline
**Status**: Active gap
**Impact**: High
**Evidence**: No `.github/workflows/` directory exists. All deploys are manual git push triggers.
**Risk**: No automated tests run before deploy. Regressions can reach production unchecked.
**Recommendation**: Add GitHub Actions for lint, type-check, and test on PR.

### 2. No Staging Environment
**Status**: Active gap
**Impact**: High
**Evidence**: Only production subdomains exist. Preview deploys use the production backend.
**Risk**: Frontend preview deploys hit the production database. Backend changes deploy directly to production.
**Recommendation**: Create a staging backend with a separate database for preview deploys.

### 3. Single-File Backend Server
**Status**: Technical debt
**Impact**: Medium
**Evidence**: `backend/server.py` is the main entry point. While routers are split into `backend/routers/`, the core business logic is still concentrated.
**Risk**: As the codebase grows, this becomes harder to maintain and test. Core functions like `merge_talent_profile()`, `sync_media_to_global_talent()` live in a single module.
**Recommendation**: Extract core business logic into `backend/app/core/` modules (partially started).

### 4. Mixed Location Formats
**Status**: Partially resolved
**Impact**: Medium
**Evidence**: Commits `e514844`, `693130a`, `ab67ca7`, `bd99e09` all fix location format handling. Location can be string, array, or object depending on when the data was created.
**Risk**: Defensive formatting (`formatLocation()`) is needed everywhere locations are displayed. New code that assumes a single format will crash.
**Recommendation**: Run a migration to normalize all locations to the structured array format. Remove defensive formatting after migration completes.

### 5. Theme Toggle is a No-Op
**Status**: By design (but confusing)
**Impact**: Low
**Evidence**: `useTheme()` hook permanently locks to light mode. Toggle exists in UI but does nothing.
**Risk**: Users may report it as a bug.
**Recommendation**: Either remove the toggle or implement dark mode.

### 6. Legacy Take Categories
**Status**: Technical debt
**Impact**: Low
**Evidence**: Both `take` (with label) and `take_1`/`take_2`/`take_3` (legacy numbered) categories exist. `LEGACY_TAKE_CATEGORIES = {take_1, take_2, take_3}` is explicitly defined.
**Risk**: Code must handle both naming conventions. New code might use the wrong convention.
**Recommendation**: Migrate legacy takes to the `take` + label format. Remove `take_1`/`take_2`/`take_3` support.

### 7. Direct Video Upload Feature-Flagged Off
**Status**: Implemented but inactive
**Impact**: Medium
**Evidence**: Architecture C (direct browser-to-Cloudinary upload) is complete (`f8f9ad4`) but `DIRECT_VIDEO_UPLOAD` defaults to `false`.
**Risk**: Feature untested in production. May have edge cases when enabled.
**Recommendation**: Enable in production after testing. Will significantly reduce backend bandwidth and Railway costs for video uploads.

### 8. Google OAuth Hardcoded Fallback
**Status**: Technical debt
**Impact**: Low
**Evidence**: `next.config.js` and frontend code has hardcoded fallback Google Client ID (`339414275037-...`).
**Risk**: If env var is missing, falls back to a specific client ID that may not match the deployment.
**Recommendation**: Remove hardcoded fallback. Fail explicitly if env var is missing.

### 9. Dual Environment Variable Naming
**Status**: Technical debt
**Impact**: Low
**Evidence**: Both `REACT_APP_*` (CRA legacy) and `NEXT_PUBLIC_*` (Next.js) prefixes are used for the same variables. `next.config.js` maps both.
**Risk**: Confusion about which prefix to use. Both work, but only `NEXT_PUBLIC_*` is the correct Next.js convention.
**Recommendation**: Standardize on `NEXT_PUBLIC_*` and remove `REACT_APP_*` references.

### 10. WhatsApp Worker Session Fragility
**Status**: Partially mitigated (as of 2026-07-04)
**Impact**: Medium
**Evidence**: WhatsApp Web session stored on Railway persistent volume. Requires QR code re-scan when session expires.
**Risk**: Session can break on Railway redeploy, container restart, or WhatsApp server-side invalidation. No auto-recovery. Feature-announcement dialogs (`aria-modal`) used to silently block all sends until the container was restarted.
**Mitigation added**: The modal handling framework (`whatsapp-worker/modals.py`, commit `914b1fa`) now auto-dismisses known-benign dialogs at login, pre-open, search, and pre-send. Unknown dialogs are captured and logged without interaction, causing a graceful retryable failure instead of a 30s timeout. This eliminates the most common cause of "session appears healthy but all sends time out."
**Remaining risk**: QR expiry and WhatsApp server-side session invalidation still require manual re-scan. No alerting pipeline exists.
**Recommendation**: Monitor session health via admin UI. Consider alerting when session drops or when `UNKNOWN_DIALOG` events appear in logs.

### 11. `internal_only` Cleanup Migration Is Manual
**Status**: Pending run
**Impact**: Low (read paths are already tolerant)
**Evidence**: `backend/migrations/remove_internal_visibility.py` was added in commit `1b15075`. It folds legacy `internal_only: true` into `client_visible: false` across `db.submissions.media[]`, `db.submissions.talent_media_visibility.*`, and `db.talents.media[]`. It is idempotent and non-destructive, but not wired into the startup migrations.
**Risk**: Data older than 2026-07-02 still carries the deprecated flag until the migration is run. All read paths already normalize on read, so there is no correctness bug — it's a cleanliness / future-simplicity concern.
**Recommendation**: Run once against production:
```
python -m migrations.remove_internal_visibility --dry-run
python -m migrations.remove_internal_visibility
```

### 12. Snapshot Infrastructure Dormant For One Release
**Status**: Deprecated, scheduled for removal
**Impact**: Low
**Evidence**: `client_package_snapshot` is no longer used for rendering. `_submission_to_client_shape()` always computes live. `generate_submission_snapshot()` in `backend/core.py` and `POST /api/projects/{pid}/submissions/{sid}/snapshot` are retained for one release as a safety net for any lingering external caller (or a cached frontend during rolling deploy). Nothing in the current app calls them.
**Risk**: A stale `client_package_snapshot` may still be present on old approved submissions in Mongo, but is ignored by the shape function.
**Recommendation**: Remove `generate_submission_snapshot()`, the `/snapshot` endpoint, and the `client_package_snapshot`/`client_package_snapshots` fields (via `$unset` migration) in the next release cycle. See D15 in [08_DECISION_LOG.md](08_DECISION_LOG.md).

## Technical Debt

### Frontend

1. **React Router inside Next.js**: Admin SPA (`/admin`) and Portal (`/portal`) use React Router inside Next.js App Router catch-all routes. This works but prevents SSR/SSG benefits for those pages.

2. **localStorage as primary state**: Application drafts, auth tokens, and submission state all persist in localStorage. No server-side session management. Lost if user clears browser data.

3. **No TypeScript**: Frontend is JavaScript-only (`.jsx`, `.js`). No type checking beyond ESLint.

4. **Hardcoded API URL**: Backend URL is an environment variable but effectively hardcoded to the Railway production URL.

5. **No error boundary**: No global React error boundary for graceful crash recovery.

### Backend

1. **No automated tests in CI**: Test files exist (`backend/tests/`, root `tests/`) but no CI pipeline runs them.

2. **Migration scripts are manual**: Scripts in `backend/scripts/` and `backend/migrations/` must be run manually. Only startup migrations are automatic.

3. **Email delivery cascade**: Resend > SendGrid > SES fallback works but makes debugging delivery issues complex.

4. **No database backup automation**: `backup_db.py` exists but must be run manually. No scheduled backups.

5. **Rate limiting is per-endpoint**: No global rate limiting middleware. Each endpoint implements its own limits.

## Future Improvements

### Architecture

1. **Separate backend from monorepo**: The backend could be its own repository with independent CI/CD.

2. **Add WebSocket for real-time updates**: Pipeline drag-and-drop, upload progress, and notification delivery would benefit from real-time communication.

3. **Implement CDN caching**: Cloudinary URLs could be served through a CDN with proper cache headers.

4. **Add search indexing**: Full-text search on talents currently queries MongoDB directly. A search index (Atlas Search or Elasticsearch) would improve performance.

### Features Not Yet Implemented

1. **Google Drive backup automation**: OAuth integration exists (`drive_admin.py`) but backup scheduling is manual.

2. **Talent portal media upload**: Portal profile editing exists but media upload from the portal is limited.

3. **Multi-language support**: No i18n infrastructure. All strings are hardcoded in English.

4. **Notification preferences**: Notifications exist but users cannot configure which notifications they receive.

5. **Audit log UI**: Profile audits and storage audit logs are collected but no admin UI to browse them (beyond storage dashboard).

## Architecture Decisions Not Yet Implemented

### 1. Full TypeScript Migration
The frontend uses JavaScript. TypeScript would catch type errors at build time, especially around the complex media/talent sync logic.

### 2. API Versioning
All endpoints are unversioned (`/api/...`). No strategy for backward-compatible API changes.

### 3. Background Job Queue
WhatsApp worker uses MongoDB polling. Other background tasks (media processing, email sending) are synchronous. A proper job queue (Redis + worker) would improve reliability.

### 4. Multi-Tenant Support
The platform currently serves a single agency. The architecture could support multi-tenancy but it's not implemented.
