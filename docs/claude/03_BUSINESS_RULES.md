# Business Rules

## ADMIN IS SOURCE OF TRUTH

The `db.talents` collection is the canonical record for all talent data. Admin-managed data in this collection takes precedence over all other sources.

**Latest approved information wins.** When data flows from applications or submissions into the talent record, the most recent approved data overwrites older data -- with the following exceptions.

### Exceptions to "Latest Wins"

1. **Audition videos** (`take`, `take_1`, `take_2`, `take_3`): These are project-specific and NEVER sync to the global talent record. They exist only on the submission.

2. **Project-specific age**: Age displayed in a submission context may differ from the global talent age if the talent submitted different information for a specific project.

3. **Project-specific location**: Same as age -- a submission may carry a project-specific location that does not override the global record.

## Talent Sync Rules

### Field Classification

When merging data into `db.talents` (from applications or submissions), fields are classified:

#### AUTO_UPDATE Fields
These are always overwritten by the incoming non-empty value:
- `instagram_handle`
- `instagram_followers`
- `location`
- `bio`
- `skills`
- `work_links`
- `interested_in`
- `languages`
- `phone`
- `cover_media_id`
- `needs_location_review`

#### REVIEW Fields
These are only filled if the master talent record has no value. If both the master and incoming data have values and they conflict, the conflict is logged but the master is NOT overwritten:
- `name`
- `dob`
- `gender`
- `height`
- `ethnicity`

#### PRESERVE Fields
These are NEVER touched by any sync operation:
- `notes`
- `tags`
- `internal_status`
- `admin_flags`
- `commission_data`
- `client_feedback`
- `status`
- `created_by`
- `whatsapp_group_name`

#### APPEND Fields
- `media` -- media items are appended, not replaced (with deduplication)

#### IGNORE Fields
Computed or derived fields that are never synced:
- `id`, `email`, `normalized_email`, `created_at`, `source`
- `image_url`, `cover_thumbnail_url`, `cover_url`, `media_count`
- `first_submission_at`, `last_submission_at`, `total_submissions`
- `age`

## F1 Rule: Blank Values Never Overwrite

**Source**: `backend/routers/applications.py` (finalize endpoint)

When syncing application data to `db.talents`, blank/empty values are filtered out before writing:

```
update = {k: v for k, v in update.items() if v not in (None, "", [], {})}
```

This prevents a partially-filled application from wiping existing talent data. A talent who submits only their name and email will not erase their existing portfolio, location, or bio.

## Application Lifecycle

### Status Flow
```
draft --> submitted (via finalize)
submitted --> draft (via edit endpoint, or new application for same email)
```

### Decision Values
`pending` | `approved` | `rejected` | `hold` | `ask_to_test` | `shortlisted` | `does_not_work_for_this`

### Finalize Validation
Before an application can be finalized:
1. Profile requirements checked (name, location, IG handle/followers) per onboarding config
2. Portfolio requirements checked (portfolio/indian/western/video image counts) per config
3. Status set to `submitted` with timestamp
4. If talent email matches existing master record, data syncs to `db.talents`

### Approval Flow
When admin approves an application (decision = `approved`):
1. Application data converted to talent doc via `_application_to_talent()`
2. If email exists in `db.talents`: merge via `merge_talent_profile()`, replace media by category
3. If new email: insert new talent with `status: "SUBMITTED"`, `source.type: "self_onboard"`
4. Media deduplicated by `public_id`, `url`, `secure_url`, `asset_id`, `source_application_media_id`

## Submission Lifecycle

### Status Flow
```
draft --> submitted (via finalize) --> updated (if edited after submit)
```

### Re-approval Rule
Projects can set `require_reapproval_on_edit: true`. When enabled, any edit to a submitted submission resets the decision back to `pending`.

### Media Sync on Upload (ORIGINAL submissions only)
When a talent uploads portfolio media to a submission that has NEVER been finalized (draft state), sync fires:
- Portfolio images (`image`, `indian`, `western`) sync to `db.talents` via `sync_media_to_global_talent()`
- Intro video (`intro_video`) syncs to talent (single slot, replaces existing)
- **Audition takes (`take`, `take_1`, `take_2`, `take_3`) do NOT sync** -- they are project-specific
- **Any upload after the first finalize does NOT sync.** This includes resubmit, update, replace media, edit, and admin-reopen → submit again. The Global Talent Profile only accepts data from the first successful submission (and the separate Talent Invite / Profile Update flow).

The gate is the helper `has_been_submitted_once(sub)` in `backend/routers/submissions.py`, keyed on the monotonic `submitted_at` (never cleared) with a status fallback. It guards: submission upload, signed/complete upload, media delete, finalize field-merge, finalize media re-sync, and the async Cloudinary webhook intro-video replace path. See D17 in [08_DECISION_LOG.md](08_DECISION_LOG.md).

### Media Sync on Delete (ORIGINAL submissions only)
When submission media is deleted before first finalize, the mirrored copy on `db.talents` is also removed via `remove_synced_media_from_global_talent()`, matched by `source_submission_media_id` / `source_application_media_id`. After first finalize, deletes only remove the media from the submission — the global mirror is preserved.

## Casting Pipeline

### Stages (ordered)
```
ask_to_test -> approved -> hold -> shortlisted -> already_tested -> locked -> rejected -> not_available -> not_interested -> pitch
```

- Default stage for new entries: `ask_to_test`
- Legacy alias: `sent` maps to `approved`
- One card per (project, talent) -- unique constraint enforced

### Pipeline Operations
- Talents are added to a project pipeline, placed in a stage
- Drag-and-drop moves between stages (optimistic UI with undo)
- Bulk move supported
- Pipeline entries hydrate talent data from `db.talents` for display

## Client Link Rules

### Visibility Control (Two Layers + Per-Media)

**Layer 1 -- Link-level visibility**: Controls which categories of information are shown across all talents in the link:
```
portfolio: true/false
intro_video: true/false
takes: true/false
instagram: true/false
instagram_followers: true/false
age: true/false
height: true/false
location: true/false
ethnicity: true/false
availability: true/false
budget: false (default)
work_links: true/false
budget_form: false (default)
download: false (default)
```

**Layer 2 -- Per-submission field_visibility**: Controls which fields are visible for each individual submission within the link.

**Per-media**: Each media item carries `client_visible: true|false`. Two states only -- Client / Hidden. The Client state means "show to the client"; Hidden means "recruiter/admin still sees it, client never does". Legacy `internal_only: true` values are folded to `client_visible: false` on read and on write; the deprecated flag is never persisted (see D16 in [08_DECISION_LOG.md](08_DECISION_LOG.md)).

**Final gate**: A `CLIENT_ALLOWED_FIELDS` strict allowlist enforces what fields can ever reach the client, regardless of visibility settings. `languages` and `special_abilities` were removed from the client shape (they duplicate `skills`).

**Single live engine**: The Client Review Link, the Review Centre Client View, the download bundle, the client PDF, and the individual media serve endpoint all render through the same shaping/filter pipeline. Snapshots are no longer used for rendering — snapshot generation is deprecated but retained for one release.

### Privacy Rules
- Talent names are privatized to "First L." via `privatizeName()`
- Budget is hidden by default (`budget: false`)
- Download is disabled by default (`download: false`)
- Link can be deactivated instantly via `is_public: false`

### Client Actions
- `interested`
- `not_for_this`
- `shortlist`
- `lock`
- `not_sure`
- `null` (reset / no action)

### Tracking
All client interactions are tracked:
- `link_views`: page opens
- `link_actions`: decisions on talents
- `client_states`: per-viewer state (unique: link_id + viewer_email). Stores `seen_talent_ids` (opened) and `reviewed_talent_ids` (decided).
- Events: `open`, `view_talent`, `view_media`, `watch_video`

### Viewed = Opened (not decided)
A submission is marked viewed only when its detail is explicitly opened by the client -- card click, modal prev/next navigation, or the Resume banner. Landing on the page, scrolling, rendering the card list, and prefetching do not count as viewed. The `IntersectionObserver` and the 15s auto-review timer were removed. `POST /public/links/{slug}/seen` uses `$addToSet` so each submission's first-viewed state is recorded exactly once. The header counter reads `seenCount = talents.filter(t => seenIds.has(t.id)).length` (see D19 in [08_DECISION_LOG.md](08_DECISION_LOG.md)).

## Onboarding Configuration

### Default Requirements
Fields can be `"required"` or `"optional"`:
- Profile: `name`, `location`, `instagram_handle`, `instagram_followers`
- Portfolio: `portfolio`, `indian`, `western`, `video`

### Custom Configs
- Custom per-profile onboarding configs stored in `profile_configs` collection
- Falls back to global config, then to `DEFAULT_ONBOARDING_CONFIG`
- Admin can manage via `/api/admin/onboarding-config` and `/api/admin/profile-configs`

## User Management

### Roles
- `admin`: Full access
- `team`: Restricted (no user management, no destructive ops, no storage admin)

### Statuses
- `active`: Can log in
- `invited`: Has invite token, hasn't completed signup
- `disabled`: Blocked from login

### Invite Flow
1. Admin creates invite via `POST /api/users/invite`
2. Invite token generated with expiry (status 410 if expired)
3. Invited user validates token via `POST /api/public/signup/validate`
4. Completes signup via `POST /api/public/signup/complete`

### Password Policy
- Minimum 8 characters
- At least one digit or special character
- Password changes bump `token_version`, invalidating all prior JWTs

## Valid Business Constants

### Interest Categories
`Acting`, `Modeling`, `Print Campaigns`, `TV Commercials`, `Digital Ads`, `Instagram Collaborations`, `Influencer Campaigns`, `Social Media Collaborations`, `Fashion Campaigns`, `Brand Shoots`, `Music Videos`, `OTT / Film Projects`, `Event Appearances`, `Hosting / Anchoring`

### Commission Options
`10%`, `15%`, `20%`, `25%`, `30%`

### Project Statuses
`ongoing`, `hold`, `complete`, `locked`

## Rate Limits

| Endpoint | Limit |
|---|---|
| OTP send | 5/hour/email, 5/hour/IP |
| OTP verify | 5 attempts per code |
| Google OAuth | 15/minute/IP |
| Forgot password | 5/15min/IP |
