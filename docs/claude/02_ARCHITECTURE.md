# Architecture

## System Overview

```
                    +-------------------+
                    |   Vercel (CDN)    |
                    | Next.js App Router|
                    | Edge Middleware   |
                    +--------+----------+
                             |
              +--------------+--------------+
              |              |              |
    apply.*   |    submit.*  |   review.*   |   links.*
    (talent   |    (audition |   (admin     |   (client
     apply)   |     submit)  |    SPA)      |    review)
              |              |              |
              +--------------+--------------+
                             |
                    +--------v----------+
                    |  FastAPI Backend   |
                    |  Railway           |
                    |  (talentgram-      |
                    |   railway)         |
                    +--------+----------+
                             |
       +---------+-----------+-----------+-----------+-----------+
       |         |           |           |           |           |
   +---v----+  +-v-------+  +v---------+ +v---------+ +v--------+ +v--------+
   |MongoDB |  |Cloudinary| |Cloudflare| |Cloudflare| |Email    | |WhatsApp |
   |Atlas   |  |(images / | |Stream    | |R2        | |(Resend/ | |Worker   |
   |(data)  |  |PDFs /    | |(HLS      | |(raw video| |SG/SES)  | |(Railway |
   |        |  |legacy vid)| |video)   | |uploads)  | |         | |Docker)  |
   +--------+  +----------+ +----------+ +----------+ +---------+ +---------+
```

Both the backend API and the WhatsApp worker are Railway services in the same project. See [05_DEPLOYMENT_RULES.md](05_DEPLOYMENT_RULES.md) for service names, roots, and start commands.

## Talent Invite / Application Flow

```
Talent receives invite link
        |
        v
apply.talentgramagency.com/apply
        |
        v
+------------------+
| Email Gate        |
| Enter email       |
+--------+---------+
         |
    +----+----+
    |         |
    v         v
 New       Existing
 Talent    Talent
    |         |
    v         v
 Unlock    OTP / Google
 Form      Verification
    |         |
    +----+----+
         |
         v
+------------------+
| Multi-Step Form   |
| 1. Identity       |
| 2. Profile Details |
| 3. Media Uploads  |
| 4. Review         |
+--------+---------+
         |
         v
+------------------+
| Draft Persistence |
| localStorage      |
| tg_application    |
| 30-day TTL        |
+--------+---------+
         |
         v
+------------------+
| Finalize          |
| - Validate reqs   |
| - Set submitted    |
| - Sync to talent   |
+--------+---------+
         |
         v
+------------------+
| Admin Review      |
| Approve / Reject  |
+--------+---------+
         |
    (if approved)
         v
+------------------+
| Merge to          |
| db.talents         |
| (canonical)       |
+------------------+
```

### Key Implementation Details

- **Entry point**: `frontend/src/pages-components/ApplicationPage.jsx`
- **Backend**: `POST /api/public/apply` (start/resume), `PUT /api/public/apply/{aid}` (save), `POST /api/public/apply/{aid}/finalize` (submit)
- **Draft storage**: localStorage key `tg_application` with 30-day TTL
- **Auth**: Submitter JWT (`role: "submitter", kind: "application"`) backed by persistent `access_token`
- **Reconciliation**: On GET, draft is hydrated from existing talent profile if fields are empty (`_reconcile_draft_from_talent`)

## Project Submission Flow

```
Talent receives project-specific link
        |
        v
submit.talentgramagency.com/submit/{slug}
        |
        v
+------------------+
| Project Info      |
| Brief, materials  |
+--------+---------+
         |
         v
+------------------+
| Identity Gate     |
| Email + OTP/OAuth |
+--------+---------+
         |
         v
+------------------+
| Submission Form   |
| - Availability    |
| - Budget response |
| - Audition takes  |
| - Portfolio media |
+--------+---------+
         |
         v
+------------------+
| Upload Manager    |
| - intro_video     |
| - take / take_1-3 |
| - indian/western  |
| - image           |
+--------+---------+
         |
         v
+------------------+
| Finalize          |
| - Validate media  |
| - Set submitted   |
| - Sync media to   |
|   global talent   |
+--------+---------+
         |
         v
+------------------+
| Admin Review      |
| via Review Center |
+------------------+
```

### Key Implementation Details

- **Entry point**: `frontend/src/pages-components/SubmissionPage.jsx`
- **Backend**: `POST /api/public/projects/{slug}/submission` (start), `POST /api/public/submissions/{sid}/upload` (media), `POST /api/public/submissions/{sid}/finalize` (submit)
- **Draft storage**: localStorage keys `tg_submission_{slug}`, `tg_draft_{slug}`, `tg_atk_{slug}`
- **Landing page always shown**: A returning talent always begins on the project landing / email gate. Global cross-project sessions (`talentgram_portal_email`, `talentgram_google_*`) never auto-unlock a new project's gate. Only a per-slug JWT/ATK session or a per-slug Google marker (`tg_google_done_<slug>`) may skip it. Deep-links `?email=` pre-fill the landing field and send an OTP but keep the gate locked (established by commit `8cfb1b5`, 2026-07-02).
- **Media sync — ORIGINAL submissions only**: On upload while the submission is still a DRAFT, portfolio/intro-video media syncs to `db.talents` via `sync_media_to_global_talent()`. Audition takes never sync. **Any post-finalize workflow (resubmit / update / replace media / edit / admin-reopen → submit again) does NOT sync to the global talent profile.** The gate is a helper `has_been_submitted_once(sub)` in `backend/routers/submissions.py`, keyed on the monotonic `submitted_at` flag (never cleared by any edit flow) with a status fallback. Also enforced in the async Cloudinary webhook intro-video replace path (established by commit `8cfb1b5`).
- **Direct upload**: When `DIRECT_VIDEO_UPLOAD=true`, video uploads go direct browser-to-Cloudinary (Architecture C)

## Review Center Flow

```
Admin logs into review.talentgramagency.com
        |
        v
+------------------+
| Admin Dashboard   |
| /admin             |
+--------+---------+
         |
         v
+------------------+
| Projects List     |
| /admin/projects    |
+--------+---------+
         |
         v
+------------------+
| Submission Review |
| Center            |
| /admin/projects/  |
| {id}/submissions  |
+--------+---------+
         |
         v
+------+------+------+------+
| Tabs:                       |
| All | Pending | Approved    |
| Hold | Rejected | Updated   |
+------+------+------+------+
         |
         v
+------------------+
| Per-Submission    |
| - View portfolio  |
| - Watch takes     |
| - View details    |
| - Set decision    |
+--------+---------+
         |
         v
+------------------+
| Decision:         |
| pending           |
| approved          |
| hold              |
| rejected          |
| ask_to_test       |
| shortlisted       |
| does_not_work     |
+------------------+
```

### Key Implementation Details

- **Entry point**: `frontend/src/pages-components/SubmissionReviewCenter.jsx`
- **Backend**: `GET /api/projects/{pid}/submissions` (list), `POST /api/projects/{pid}/submissions/{sid}/decision` (decide)
- **Auth**: Admin JWT via `adminApi` axios instance
- **Talent portfolio**: Displayed alongside submission media; sourced from `db.talents` master record
- **Visibility persistence (immediate save)**: Media and field-visibility toggles PUT `/api/projects/{pid}/submissions/{sid}` the moment they change. Saves are serialized so overlapping toggles apply in order; `executeDecision` awaits any in-flight visibility save before recording the decision, so Hide → Approve cannot race. There is no separate "Save Overrides" gating (established by commit `eb8c057`, 2026-07-03).
- **Visibility model (Client / Hidden only)**: The three-state Client / Hidden / Internal model was collapsed to two states in commit `1b15075`. Legacy `internal_only: true` values are folded to `client_visible: false` on read and never persisted on write. A one-shot migration `backend/migrations/remove_internal_visibility.py` cleans the historical rows (see [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md)).
- **Live shaping (no frozen snapshots)**: `_submission_to_client_shape()` in `backend/core.py` always computes the client-facing shape from the current submission. The `client_package_snapshot` short-circuit was removed. Snapshot generation is deprecated but retained for one release (see D15 in [08_DECISION_LOG.md](08_DECISION_LOG.md)).
- **Google Drive folder button removed**: The "Open Google Drive folder" action and its backend endpoint `GET /api/submissions/{sid}/drive` were removed (Cloudinary-only). The env-gated backup pipeline in `backend/drive_backup.py` is untouched.

## Client Review Flow

```
Client receives link
        |
        v
links.talentgramagency.com/l/{slug}
        |
        v
+------------------+
| Identify          |
| Name + Email      |
+--------+---------+
         |
         v
+------------------+
| Viewer Token      |
| (scoped to slug)  |
+--------+---------+
         |
         v
+------------------+
| Talent Grid       |
| Privacy: "First L."|
+--------+---------+
         |
         v
+------+------+------+------+------+
| Tabs:                              |
| All | Pending | Viewed | Ask Test  |
| Interested | Not For This          |
| Shortlist | Lock | Unsure          |
+------+------+------+------+------+
         |
         v
+------------------+
| Per-Talent Card   |
| - Portfolio media |
| - Intro video     |
| - Audition takes  |
| - Details (filtered|
|   by visibility)   |
+--------+---------+
         |
         v
+------------------+
| Client Actions    |
| - Interested      |
| - Not for this    |
| - Shortlist       |
| - Lock            |
| - Unsure          |
| - Ask for test    |
+--------+---------+
         |
         v
+------------------+
| Feedback          |
| - Text feedback   |
| - Voice recording |
+------------------+
```

### Key Implementation Details

- **Entry point**: `frontend/src/pages-components/ClientView.jsx`
- **Backend**: `GET /api/public/links/{slug}` (view), `POST /api/public/links/{slug}/action` (decide)
- **Auth**: Viewer JWT via `viewerApi` axios instance, stored as `tg_viewer_{slug}`
- **Privacy**: `privatizeName()` collapses full names to "First L."
- **Visibility**: Two layers -- link-level `visibility` (category toggles) and per-submission `field_visibility` (field toggles). The per-media state is now Client / Hidden only (Internal removed; see Review Center notes above).
- **Single live engine**: The Client Review Link, the Review Centre Client View preview, the download bundle, the client PDF, and the individual media serve all render through the same shaping/filter pipeline (`_submission_to_client_shape` + `_filter_talent_for_client`). Changes made in the Review Centre take effect on the client link immediately (established by commit `1b15075`).
- **Viewed = Opened**: A submission is marked viewed **only** when its detail is explicitly opened (card click → `handleOpen`, modal prev/next → `onNavigate`, or Resume banner). Landing on the page, scrolling, and rendering the card list do not count. The 15s auto-review timer and the `IntersectionObserver` were both removed. The header counter reads `seenCount = talents.filter(t => seenIds.has(t.id)).length`; `/seen` uses `$addToSet` for first-view idempotency (established by commit `b1c902a`, 2026-07-04).
- **Analytics tracking**: Opens (link-level), talent-open (`view_talent`), media views, and video watch durations are tracked via `POST /api/public/links/{slug}/track`. Talent-open events dedupe via `trackedSeenRef` per session.

## Global Talent Flow

```
+------------------+
| Sources of        |
| Talent Data       |
+--------+---------+
         |
    +----+----+----+
    |         |    |
    v         v    v
 Application  Submission  Admin
 (self-serve) (per-project) (manual)
    |         |    |
    +----+----+----+
         |
         v
+------------------+
| Merge to          |
| db.talents         |
| (CANONICAL)       |
+--------+---------+
         |
    +----+----+----+----+
    |    |    |    |    |
    v    v    v    v    v
  Pipeline  Links  Portal  Search  WhatsApp
  (Kanban)  (Client) (Self) (Admin) (Broadcast)
```

### Talent Profile Data Flow

```
+-------------------+     +-------------------+
| Application       |     | Submission         |
| form_data + media |     | form_data + media  |
+---------+---------+     +---------+----------+
          |                         |
          v                         v
   +------+------+          +------+------+
   | Finalize     |          | Finalize     |
   | F1 Rule:     |          | Media sync:  |
   | blanks never |          | portfolio    |
   | overwrite    |          | syncs, takes |
   +------+------+          | do NOT sync  |
          |                  +------+------+
          v                         |
   +------+------+                  |
   | Approval     |                 |
   | merge to     |                 v
   | db.talents   |          +------+------+
   +------+------+          | sync_media_  |
          |                  | to_global_   |
          |                  | talent()     |
          v                  +------+------+
   +------+------+                  |
   | db.talents   | <---------------+
   | (canonical)  |
   +------+------+
          |
          v
   Field classification:
   - AUTO_UPDATE: instagram, location, bio, skills, etc.
   - REVIEW: name, dob, gender, height, ethnicity
   - PRESERVE: notes, tags, internal_status, admin_flags
   - IGNORE: computed fields (age, media_count, etc.)
```

## Database Collections

| Collection | Purpose | Key Indexes |
|---|---|---|
| **Core** | | |
| `talents` | Canonical talent profiles | `email` (unique), `normalized_email` (unique) |
| `applications` | Self-service applications | `talent_email` (unique), `access_token` (unique+sparse) |
| `submissions` | Per-project audition submissions | `(project_id, talent_email)` (unique), `access_token` (unique+sparse) |
| `submission_drafts` | Temporary pre-submission drafts | -- |
| `projects` | Casting projects | -- |
| `links` | Client review links | `slug` (unique) |
| `casting_pipeline` | Kanban pipeline entries | `(project_id, talent_id)` (unique) |
| `users` | Admin/team users | `email` (unique) |
| `admins` | Legacy admin collection (migrated to `users`) | -- |
| `tags` | Talent tags | `normalized_name` (unique) |
| **Auth & Security** | | |
| `otp_codes` | Email OTP (TTL auto-expire) | -- |
| `otp_audit_logs` | OTP attempt rate-limiting audit | -- |
| `password_reset_tokens` | Reset tokens (SHA-256 hashed, TTL) | -- |
| **Client Link Tracking** | | |
| `link_views` | Link page open events | -- |
| `link_actions` | Client decisions on talents | -- |
| `link_events` | Granular event tracking (view, media, video) | -- |
| `link_shares` | Individual talent share tracking | -- |
| `link_downloads` | Download event tracking | -- |
| `client_states` | Per-viewer state (unique: link_id + viewer_email) | -- |
| **Media & Storage** | | |
| `asset_metadata` | Cloudinary upload tracking | -- |
| `storage_audit_log` | Upload/archive/restore/delete audit | -- |
| `profile_audits` | Talent field change history | -- |
| `profile_configs` | Onboarding requirement configurations | -- |
| **Feedback & Notifications** | | |
| `feedback` | Client feedback (moderated) | -- |
| `notifications` | In-app notifications | -- |
| **CRM** | | |
| `clients` | Marketing CRM client records | -- |
| `interactions` | CRM interaction notes | -- |
| **WhatsApp Engine (7 collections)** | | |
| `whatsapp_templates` | Message templates | -- |
| `whatsapp_batches` | Batch send records | -- |
| `whatsapp_jobs` | Individual send jobs | -- |
| `whatsapp_sessions` | WhatsApp Web session state | -- |
| `whatsapp_config` | Engine configuration | -- |
| `whatsapp_audit_log` | WhatsApp audit trail | -- |
| `whatsapp_dom_snapshots` | Dialog/DOM captures for unknown or undismissable dialogs | -- |
| **Workflow (3 collections)** | | |
| `workflow_tasks` | Internal tasks | -- |
| `workflow_scouts` | Scouting entries | -- |
| `workflow_notifications` | Workflow-specific notifications | -- |
| **Google Drive Backup** | | |
| `drive_oauth` | Drive OAuth credentials | -- |
| `drive_oauth_state` | OAuth state tokens | -- |
| `drive_upload_failures` | Failed backup records | -- |
| **Migrations** | | |
| `migration_reports` | Migration run reports | -- |

## Authentication Architecture

```
+------------------+     +------------------+     +------------------+
| Admin/Team       |     | Talent           |     | Client/Viewer    |
| Email + Password |     | Email OTP or     |     | Name + Email     |
| JWT (30 days)    |     | Google OAuth     |     | on link page     |
| role: admin/team |     |                  |     |                  |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         v                        v                        v
  +------+------+         +------+------+         +------+------+
  | Admin JWT   |         | Submitter   |         | Viewer JWT  |
  | {email,     |         | JWT {role,  |         | {role, slug,|
  |  role, id,  |         |  sid, kind/ |         |  email,name,|
  |  tv, exp}   |         |  slug}      |         |  viewer_id} |
  +------+------+         +------+------+         +------+------+
         |                        |                        |
         v                        v                        v
  +------+------+         +------+------+         +------+------+
  | tg_admin_   |         | tg_atk_{slug}|        | tg_viewer_  |
  | token       |         | + submitter  |         | {slug}      |
  | (localStorage)|       | JWT          |         | (localStorage)|
  +-------------+         +-------------+         +-------------+

  Portal Token (separate):
  +------+------+
  | Portal JWT  |
  | {role:portal |
  |  email}      |
  | 30-day exp   |
  +------+------+
         |
         v
  talentgram_portal_token (localStorage)
```
