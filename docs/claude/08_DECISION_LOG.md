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
