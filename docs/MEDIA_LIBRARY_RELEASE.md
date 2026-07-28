# Media Library — Release Notes, Deployment Checklist, Rollback Checklist

Status: release-candidate, prepared 2026-07-28. Covers the full Media Library initiative, five commits on `main`:

| Commit | Title |
|---|---|
| `d506543` | Phase 4.1 — canonical `build_prefill_media()` |
| `3348f63` | Phase 4.2 — Media Library Picker UI (origin badges) |
| `7df26fb` | Phase 4.3 — talent-owned Media Library Manager + reference-aware delete |
| `e97dd6b` | Phase 4.4 — production certification (provider metadata + systemic reference-aware delete) |
| `dc2ea3f` | Release blocker fix — `/apply` draft-media reconciliation additive-only |
| *(this prep)* | Release preparation — code cleanup, this document |

This document is Media-Library-specific. For the general Railway/Vercel mechanics (how to actually trigger a deploy, domain config, standard rollback commands, standard verification steps), see [docs/claude/05_DEPLOYMENT_RULES.md](claude/05_DEPLOYMENT_RULES.md) — nothing below duplicates that; it only calls out what's different or additional for this release. For the technical architecture, see [docs/claude/04_MEDIA_RULES.md](claude/04_MEDIA_RULES.md)'s "Media Library System" section. For the full incident/decision history, see [docs/claude/08_DECISION_LOG.md](claude/08_DECISION_LOG.md) D26/D27 and [docs/claude/07_OPEN_ISSUES.md](claude/07_OPEN_ISSUES.md) #15/#16.

---

## 1. Release Notes

### Features
- **Media Library Manager** (talent-facing): talents can view their reusable media by category (Indian/Western/Portfolio), delete an item, set a cover image, and view their introduction video — from Talent Profile → Media Library. No upload, reorder, or folders from this page (Version 1 scope, by design).
- **Canonical prefill**: every entry point that pre-fills a talent's media into a new submission (`/public/prefill`, project submission start, OTP/Google login) now goes through one shared builder (`build_prefill_media()`), instead of three independently-drifted implementations.
- **Origin badges**: submission media that came from the talent's reusable Library is visually distinguished ("Only in this project" badge, shown only for genuinely project-exclusive uploads) — presentation-layer only.

### Architecture Changes
- **Reference-aware deletion, applied system-wide**: any code path that can physically destroy a Cloudinary/Cloudflare Stream asset (Library delete, submission media delete/replace, application media delete, webhook-driven replace cleanup) now confirms via `core.is_media_asset_referenced()` that no other document (`talents`/`submissions`/`applications`) still needs that asset before destroying it, via a single shared function `core.safe_cleanup_media_storage()`. Previously only two of seven such call sites had this protection.
- **Provider-agnostic media copying**: every place that copies a media item between collections (mirror talent↔submission, mirror talent↔application, `/apply` draft hydration) now copies every field except a documented, centrally shared deny-list (`core.MEDIA_COPY_EXCLUDE_FIELDS`), instead of a hand-picked whitelist — so a future storage provider's own identifying fields survive automatically with no code change.
- **`/apply` draft-media reconciliation is additive-only**: fixes a data-loss bug where an ordinary page refresh could silently discard an applicant's own uploads. See D27.

### Database / Index Changes
Six new **sparse**, **non-unique** indexes, created idempotently at backend startup (`core.py`'s existing `p0_indexes` list — no separate migration script):

| Collection | Field | Index name |
|---|---|---|
| `talents` | `media.public_id` | `talents_media_public_id` |
| `talents` | `media.stream_uid` | `talents_media_stream_uid` |
| `submissions` | `media.public_id` | `submissions_media_public_id` |
| `submissions` | `media.stream_uid` | `submissions_media_stream_uid` |
| `applications` | `media.public_id` | `applications_media_public_id` |
| `applications` | `media.stream_uid` | `applications_media_stream_uid` |

No data migration. No schema change to any document shape beyond what code already writes (no new required fields; `stream_uid`/`provider` were already optional fields on media items, now just reliably *preserved* when copied).

### Breaking Changes
**None expected.** Specifically verified:
- No API contract changes to any existing endpoint's request/response shape (two brand-new endpoints added: `DELETE /api/portal/media/{mid}`, `POST /api/portal/media/{mid}/cover` — additive).
- No new required environment variables. All Cloudinary/Cloudflare Stream/R2 config is reused as-is.
- Backward-compatible with data written before this release: `/apply` drafts hydrated by the pre-fix reconciliation logic self-heal on their first post-deploy load (no duplication, no data loss — see D27's live verification).
- Admin-facing endpoints (`DELETE /talents/{tid}/media/{mid}`, `POST /talents/{tid}/cover/{mid}`) are unchanged in request/response shape; only their internal implementation was refactored to shared helpers.

### Known, Deliberately Unfixed Limitation
Cloudflare Stream video replacement (the active production video provider) does not clean up the old asset on replace — a storage-cost leak, not a correctness or data-loss issue. Pre-existing, unaffected by this release either way. Tracked in [07_OPEN_ISSUES.md](claude/07_OPEN_ISSUES.md) #16 as a recommended fast-follow.

### Rollback Strategy
See section 3 (Rollback Checklist) below. Summary: this release is **safe to roll back at the code level with zero data cleanup required** — every change is additive or protective (nothing was deleted, no destructive migration ran). The one nuance is the six new indexes, which are themselves safe to leave in place indefinitely even after a code rollback (see 3.3).

---

## 2. Deployment Checklist

### 2.1 Pre-Deploy
- [ ] Confirm `git log --oneline -5` on the deploy branch shows the five commits listed above, most recent `dc2ea3f` (or later, if this prep's cleanup commit is included).
- [ ] Confirm no `.env`/secrets changes are needed — this release introduces zero new environment variables (verify against the "Required/Optional Environment Variables" tables in [05_DEPLOYMENT_RULES.md](claude/05_DEPLOYMENT_RULES.md); nothing new to add there).
- [ ] Run `backend/scripts/backup_db.py` per this repo's standing "backup before migrations" rule (05_DEPLOYMENT_RULES.md rule 6) — even though this release ships no destructive migration, the six new index builds do touch `talents`/`submissions`/`applications` at the storage-engine level.
- [ ] If `talents`/`submissions`/`applications` are large in production (check via MongoDB Atlas collection stats), confirm the Atlas cluster's MongoDB server version is ≥4.2 so the new index builds use the non-blocking build path by default. If on an older version, consider building the six indexes manually ahead of the deploy during a low-traffic window (`db.<coll>.createIndex(..., {background: true})`), rather than relying on the automatic startup path.

### 2.2 Railway (Backend)
- [ ] Deploy via the standard path (`git push` to `main`, or Railway dashboard redeploy) per [05_DEPLOYMENT_RULES.md](claude/05_DEPLOYMENT_RULES.md).
- [ ] Watch startup logs for the index-creation block. Expect either silence (indexes already exist / created cleanly) or, at worst, `logger.warning(f"{coll} index {keys}: {e}")` lines for the six new indexes — these are non-fatal by design (per this repo's "never let index creation abort application boot" policy) but should be investigated if seen, not ignored.
- [ ] Confirm `GET /health` returns `{"status": "ok", ...}` and `GET /api/` returns `{"app": "talentgram", "ok": true}`.
- [ ] Confirm the running commit hash matches `main` (`railway status --json`).
- [ ] No new Railway environment variables to set.

### 2.3 Vercel (Frontend)
- [ ] Deploy via the standard auto-deploy-on-push path per [05_DEPLOYMENT_RULES.md](claude/05_DEPLOYMENT_RULES.md) — **use the git-push path, not a bare `vercel --prod` CLI call**, per that doc's documented promotion gotcha.
- [ ] No new environment variables, no new subdomains, no `next.config.js`/middleware changes in this release.
- [ ] Confirm the deployment is actually promoted to the custom domains (`vercel inspect https://www.talentgramagency.com`), not just built.

### 2.4 MongoDB Indexes
- [ ] After backend deploy, verify the six new indexes exist:
  ```
  db.talents.getIndexes()       // expect talents_media_public_id, talents_media_stream_uid
  db.submissions.getIndexes()   // expect submissions_media_public_id, submissions_media_stream_uid
  db.applications.getIndexes()  // expect applications_media_public_id, applications_media_stream_uid
  ```
- [ ] Confirm no unexpected long-running index-build operations are still active (`db.currentOp()` filtered to index builds) more than a few minutes after boot, on a production-sized collection.

### 2.5 Cloudinary
- [ ] No configuration changes. Confirm existing `CLOUDINARY_CLOUD_NAME`/`CLOUDINARY_API_KEY`/`CLOUDINARY_API_SECRET` are unchanged in the Railway environment.
- [ ] Optional sanity check: `cloudinary.api.ping()` from a shell against production credentials (read-only, safe).

### 2.6 Cloudflare (Stream + R2)
- [ ] No configuration changes. Confirm existing `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_STREAM_API_TOKEN`, `CLOUDFLARE_STREAM_CUSTOMER_CODE`/`_SUBDOMAIN`, `CLOUDFLARE_STREAM_WEBHOOK_SECRET`, and R2 (`ENABLE_R2_MEDIA_PIPELINE`, `R2_*`) variables are unchanged.
- [ ] `VIDEO_PROVIDER` should remain unset or `"stream"` (the production default) — this release does not change provider selection.

### 2.7 Smoke Tests (post-deploy, in order)
Run against production (or the same checks against staging first, if one exists at deploy time):
1. **Existing talent, Media Library**: log in to Talent Portal as a talent with existing media → Profile → confirm Media Library renders items grouped by category, cover badge shown, no console errors.
2. **Delete**: delete one non-cover Library item → confirm it disappears, a toast confirms, and (if feasible to check) the Cloudinary asset is *not* destroyed if any other submission still references it.
3. **Set cover**: set a different item as cover → confirm the badge moves and persists across a page reload.
4. **New submission via Project Link**: start a new submission for a talent with existing Library media → confirm prefilled images appear with the "Only in this project" badge correctly absent from Library-sourced items.
5. **`/apply` via Invite Link**: start a new application → upload a photo → refresh the page → confirm the upload survives (this is the specific bug fixed by D27 — do not skip this check).
6. **Existing talent applying via `/apply`**: for an email that already has an existing talent record with Library media, start/resume an application, upload an extra photo beyond the Library's count, refresh twice → confirm both the hydrated Library items and the extra upload are present with no duplicates.
7. **Client Review Link**: open an existing, already-generated client review link for a finalized submission → confirm all media still renders (this proves submission-snapshot independence wasn't affected by anything in this release).
8. **Admin Talent Editor**: open a talent record in the admin dashboard, delete a media item and set a cover → confirm identical behavior to the talent-facing path (same shared backend implementation).

---

## 3. Rollback Checklist

### 3.1 If the deploy fails or a smoke test fails — what to revert
This release has no destructive migration and no schema changes that older code can't read, so **a straight code rollback is sufficient and safe** — there is nothing to separately "undo" in the data:

1. **Backend (Railway)**: redeploy the prior known-good commit (the one immediately before `d506543` if rolling back the entire initiative, or any intermediate commit if only the latest fix needs reverting) per [05_DEPLOYMENT_RULES.md](claude/05_DEPLOYMENT_RULES.md) section "Backend (Railway)". Confirm the running commit hash matches via `railway status --json`.
2. **Frontend (Vercel)**: promote the prior successful deployment from the Vercel dashboard (only `PortalProfile.jsx` changed on the frontend side across this whole initiative — a Vercel rollback alone is sufficient if the issue is frontend-only).
3. **No manual database cleanup is required** for a rollback:
   - The six new indexes are harmless to leave in place even after rolling back the code that benefits from them (see 3.3) — older code simply won't query by those fields, and the indexes cost nothing but a small amount of storage/write overhead.
   - No documents were deleted, migrated, or reshaped by this release. Older (pre-rollback) code reads the exact same document shapes it always did — new fields on media items (already-optional, like `provider`/`stream_uid`) are simply ignored by code that doesn't know about them.
   - `/apply` drafts that got additively merged by the new reconciliation logic are strictly a superset of what the old logic would have produced (nothing was removed) — rolling back to the old wholesale-replace logic would not "un-corrupt" anything, since nothing is corrupted; it would just reintroduce the original bug, which is why this fix should not be the one that gets rolled back in isolation without also reverting the whole initiative.

### 3.2 Partial rollback (only the `/apply` fix, keeping the rest)
If the `/apply` fix (`dc2ea3f`) specifically needs to be reverted while keeping Phases 4.1–4.4:
- `git revert dc2ea3f` cleanly reverts `applications.py`'s reconciliation logic back to the wholesale-replace behavior — this is safe to do in isolation (the fix touched only `applications.py`, no shared helper another commit depends on).
- Be aware this reintroduces the original data-loss bug (D26/D27) — only do this if the NEW code is causing an active incident and the OLD (buggy) behavior is judged less bad in the moment; this is not a recommended steady state.

### 3.3 The six new indexes — do they need rollback?
**No, they should stay** even under a full code rollback:
- They are pure read-optimization indexes with no `unique` constraint and no effect on write correctness — code that doesn't know about them behaves identically with or without them.
- Dropping them is possible (`db.<coll>.dropIndex("<name>")`) but not necessary. Only drop them if there's a specific, measured reason (e.g., write-amplification concern on a very high-write collection) — not as a default rollback step.

### 3.4 If a smoke test reveals actual data loss or corruption (not expected, but the procedure)
1. Stop further writes to the affected collection if possible (maintenance mode / pause the affected Railway service).
2. Restore from the pre-deploy backup taken in step 2.1 (`backend/scripts/backup_db.py`'s output, or the Atlas automated backup nearest the deploy time).
3. Roll back code per 3.1 before resuming traffic.
4. This scenario is not expected given the live-verification already performed (see section 4 of the Phase 4.4 audit and D27's regression report) — every fix in this initiative was specifically designed to make destructive scenarios *less* likely (reference-aware deletion, additive-only reconciliation), not more.

---

## 4. Sign-off

This document, plus the live-verification already recorded in [08_DECISION_LOG.md](claude/08_DECISION_LOG.md) (D26, D27) and the final regression pass in this same release-preparation session, constitute the release record for the Media Library initiative. See that regression pass's output for the last full pre-deploy verification run.
