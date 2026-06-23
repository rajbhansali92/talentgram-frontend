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
                    |  (Emergent/Railway)|
                    +--------+----------+
                             |
              +--------------+--------------+
              |              |              |
    +---------v---+  +-------v-----+  +----v--------+
    | MongoDB     |  | Cloudinary  |  | Email       |
    | Atlas       |  | (media)     |  | (Resend/    |
    | (data)      |  |             |  |  SG/SES)    |
    +-------------+  +-------------+  +-------------+
                             |
                    +--------v----------+
                    | WhatsApp Worker   |
                    | (Railway/Docker)  |
                    | Playwright +      |
                    | Chromium          |
                    +-------------------+
```

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
- **Media sync**: On upload, portfolio media syncs to `db.talents` via `sync_media_to_global_talent()`. Audition takes do NOT sync.
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
- **Visibility**: Two layers -- link-level `visibility` (category toggles) and per-submission `field_visibility` (field toggles)
- **Tracking**: Opens, views, media views, and video watches are tracked via `/track` endpoint

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
| **WhatsApp Engine (6 collections)** | | |
| `whatsapp_templates` | Message templates | -- |
| `whatsapp_batches` | Batch send records | -- |
| `whatsapp_jobs` | Individual send jobs | -- |
| `whatsapp_sessions` | WhatsApp Web session state | -- |
| `whatsapp_config` | Engine configuration | -- |
| `whatsapp_audit_log` | WhatsApp audit trail | -- |
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
