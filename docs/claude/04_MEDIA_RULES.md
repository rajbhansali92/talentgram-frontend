# Media Rules

## Video Delivery Architecture

Talentgram uses **multiple storage/delivery backends** for media, and the client shape is provider-aware:

| Content type | Storage | Delivery | Notes |
|---|---|---|---|
| Images (portfolio, headshots, indian, western) | Cloudinary | Cloudinary URL transforms | Fingerprinted magic-byte validation |
| Client-facing PDFs (talent packages) | Cloudinary | Cloudinary URL | Generated at request time |
| Audition videos (intro_video, takes) | Cloudflare R2 (raw upload) → Cloudflare Stream (transcode + HLS) | HLS `.m3u8` from `*.cloudflarestream.com` | See "Cloudflare Stream Pipeline" below |
| Legacy videos | Cloudinary | Cloudinary URL | Detected via `res.cloudinary.com` domain in stored URL |

`_public_media()` in `backend/core.py` inspects each media record: if `provider == "stream"`, `stream_uid` is set, the URL contains `cloudflarestream.com`, or it ends with `.m3u8`, the stored URL is passed through untouched. Only genuine Cloudinary videos get rewritten through the Cloudinary transform.

### Frontend HLS playback (`HlsVideo` component)

Located at `frontend/src/components/HlsVideo.jsx`. Chrome and Firefox do not have native HLS support, so `hls.js` is lazy-loaded and attached for `.m3u8` sources. Safari (macOS/iOS) uses its native HLS. `LazyVideoPlayer` picks `HlsVideo` for any URL containing `.m3u8`.

## Cloudinary Architecture

### Configuration
- **Cloud name**: `talentgram` (via `CLOUDINARY_CLOUD_NAME`)
- **SDK**: Python `cloudinary` library (upload, API, utils)
- **URL pattern**: `https://res.cloudinary.com/talentgram/...`

### Folder Structure

```
talentgram/
  talents/{talent_id}_{name_slug}/
    profile_images/           # Portfolio images
    intro_video/              # Talent intro video
    portfolio_videos/         # Additional portfolio videos
  projects/{project_id}/
    auditions/{talent_id}_{name_slug}/
      submission_{submission_id}/  # Audition media
  applications/{application_id}/  # Application media
  uploads/{admin_user_id}/        # Admin-uploaded files
```

All folders are validated to start with `"talentgram/"` via `_validate_folder()`.

## Upload Rules

### Binary Signature Validation
Every upload is validated by magic bytes before being sent to Cloudinary:
- **Images**: JPEG (`FF D8 FF`), PNG (`89 50 4E 47`), WebP (`52 49 46 46...57 45 42 50`), HEIC/HEIF (ftyp brands: heic, heix, hevc, hevx, mif1, msf1, heif, hefs)
- **Videos**: MP4 (`66 74 79 70` ftyp variants), MOV (QuickTime `qt  ` brand)
- **Documents**: PDF (`25 50 44 46`)
- **MIME cross-check**: Declared content-type must match detected signature type (first segment of MIME compared)
- Formats not in the above list (AVI, WebM, MKV, 3GP, BMP, TIFF, etc.) are implicitly rejected by failing signature validation

Note: The frontend accepts `.avi, .webm, .mkv, .3gp` file extensions in its file picker, but the backend will reject these if their magic bytes do not match the validated signatures above.

### Size Limits
| Type | Max Size |
|---|---|
| Submission video | 200 MB |
| Submission image | 20 MB |
| Project material video | 100 MB |
| Feedback audio | 25 MB |
| Feedback text | 4,000 chars |

### Count Limits
| Type | Min | Max |
|---|---|---|
| Submission takes | -- | 5 |
| Submission images (total) | 5 | 8 |
| Application images (total) | 5 | 8 |
| Images per category | -- | 10 |

### Video Duration Limit
- Max audition video: **300 seconds** (5 minutes)
- Enforced client-side for direct uploads

## Upload Categories

### Submission Upload Categories
`intro_video`, `take`, `take_1`, `take_2`, `take_3`, `image`, `indian`, `western`

### Application Upload Categories
`intro_video`, `image`, `indian`, `western`

### Legacy Take Categories
`take_1`, `take_2`, `take_3` (legacy naming; current system uses `take` with labels)

### Portfolio Image Categories
`image`, `indian`, `western`

### Direct Video Categories (Architecture C)
`intro_video`, `take`, `take_1`, `take_2`, `take_3`

### Material Categories (Project briefs)
`script`, `image`, `audio`, `video_file`

## Video Processing

### Eager Transformations
When a video is uploaded to Cloudinary:
1. **720p MP4** derivative generated (`c_limit, w_1280, h_720, vc_auto, q_auto, f_mp4`) -- codec is `auto` (Cloudinary selects optimal codec), not hardcoded H.264
2. **Poster frame** generated (`c_fill, w_600, h_338, f_jpg, q_auto, dpr_auto`)
3. For videos > 300MB with `keep_original=False`: incoming transformation discards the original, storing only the 720p derivative

### Image Transformations
- **Eager thumbnail**: `w=400, c=fill, f_auto, q_auto, dpr_auto`

### URL Generation Presets (`media_url()`)
| Preset | Transformation | Usage |
|---|---|---|
| `roster` | w=400, c=fill | Roster cards |
| `thumb` | w=200, c=fill | Pipeline mini-thumbnails |
| `detail` | w=1200, c=limit | Detail page view |
| `full` | w=1600, c=limit | Lightbox / full view |
| `poster` | 600x338, first frame | Video poster |

## Upload Paths

### Path A: Backend Proxy (Default)
```
Browser --> multipart POST --> FastAPI --> Cloudinary
                                 |
                            (validates file,
                             uploads to Cloudinary,
                             stores metadata)
```
- Frontend sends `multipart/form-data` to backend endpoint
- Backend validates binary signature, uploads to Cloudinary
- Metadata stored in `asset_metadata` collection

### Path B: Direct Browser-to-Cloudinary (Architecture C)
```
Browser --> POST /video-signature --> FastAPI (returns signed params)
Browser --> chunked upload (20MB chunks) --> Cloudinary
Browser --> POST /video-complete --> FastAPI (confirms, stores metadata)
```
- Feature-flagged: `DIRECT_VIDEO_UPLOAD=true` / `NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD=true`
- Only for video slots on submission endpoints
- Backend never sees video bytes
- Incoming transformation: `c_limit,h_720,w_1280/q_auto,vc_auto` (720p, auto codec)

### Upload UX
- `FloatingUploadManager` component renders fixed-position overlay at bottom-right
- Shows upload progress per file
- State machine: `uploading -> processing -> completed/failed`
- Retry: 3 attempts with exponential backoff
- Dismiss controls per upload
- **Never covers the sticky submit CTA**: the overlay's `bottom` offset is computed from the current page's sticky submit-CTA footer's live rendered height (via `useStickyFooterHeightVar` + a `--tg-sticky-cta-h` CSS custom property), so it always floats clear of the button instead of overlapping it — adapts automatically to footer height, safe-area insets, and iOS toolbar reflows (see D24 in [08_DECISION_LOG.md](08_DECISION_LOG.md)). On pages without a sticky footer, it falls back to its original fixed bottom-right position.
- **Client-side video compression** (`lib/videoCompress.js`, ffmpeg.wasm): concurrency gating, the shared FFmpeg singleton's lifecycle, idle-timeout recycling, versioned `/ffmpeg/v1` asset caching, and telemetry hooks are documented in full in [10_FFMPEG_LIFECYCLE.md](10_FFMPEG_LIFECYCLE.md).

## CRITICAL: Media Sync Rules

### What Syncs to Global Talent (`db.talents`)

Sync only fires when **both** conditions hold:
1. The category is in the mapping below.
2. The submission has never been finalized (`has_been_submitted_once(sub) == False`). Any upload/delete after the first finalize is treated as a resubmission and skips sync entirely (see D17 in [08_DECISION_LOG.md](08_DECISION_LOG.md)).

| Source Category | Syncs To | Syncs on ORIGINAL? |
|---|---|---|
| `image` / `portfolio` | `portfolio` | YES |
| `indian` | `indian` | YES |
| `western` | `western` | YES |
| `video` / `intro_video` | `video` | YES (single slot, replaces) |
| `headshot` / `headshots` | `headshot` | YES |
| `additional_portfolio` | `additional_portfolio` | YES |
| **`take`** | -- | **NEVER (any state)** |
| **`take_1`** | -- | **NEVER (any state)** |
| **`take_2`** | -- | **NEVER (any state)** |
| **`take_3`** | -- | **NEVER (any state)** |

### CRITICAL RULE 1: Audition Takes Never Sync

**Audition take categories (`take`, `take_1`, `take_2`, `take_3`) must NEVER sync to the global talent record.** They are project-specific and would violate client confidentiality if mirrored globally.

### CRITICAL RULE 2: Resubmissions Never Sync

**A submission that has already been finalized once cannot mutate the Global Talent Profile.** Every post-first-submit workflow -- resubmit, update, replace media, edit existing submission, admin-reopen → talent submits again -- is a project-specific correction and must not propagate to `db.talents`. Only the FIRST original submission (and the separate Talent Invite / Profile Update flow) may update the master profile.

Enforcement is centralized via `has_been_submitted_once(sub)` in `backend/routers/submissions.py` and applied at every write-to-global path: upload mirror, upload replace-removal, signed/complete upload, media delete, finalize field-merge, finalize media re-sync, and the async Cloudinary webhook intro-video replace path. The `submitted_at` flag is monotonic (never cleared by any edit flow) which makes the check robust across current and future edit workflows.

### Sync Implementation: `sync_media_to_global_talent()`

**Source**: `backend/core.py` (function at line ~2337)

Called on every submission/application media upload for syncable categories.

Deduplication by:
- `public_id`
- `url`
- `source_submission_media_id`
- `source_application_media_id`

For video category: `$pull` existing video before pushing new one (single-slot behavior).

### Reverse Sync: `remove_synced_media_from_global_talent()`

Called when submission/application media is deleted. Removes the mirrored copy from `db.talents` by matching `source_submission_media_id` or `source_application_media_id`.

## Media Library System (Phase 4)

Talents own a reusable **Global Media Library** (`db.talents.media[]`) on top of the sync rules above. This section is the architecture reference for that system — media ownership, why submissions are immutable snapshots even though they share storage with the Library, and the reference-aware deletion model that makes that safe. Built across four increments: 4.1 canonical prefill, 4.2 picker UI (origin badges), 4.3 the talent-owned Media Library Manager + reference-aware delete, 4.4 this production-certification pass (provider metadata + systemic delete-safety fixes). See D26 in [08_DECISION_LOG.md](08_DECISION_LOG.md) for the full incident writeup.

### Media ownership: three collections, one physical asset

`db.talents.media[]`, `db.submissions.media[]`, and `db.applications.media[]` each independently **own** media entries by value — a JSON dict with `url`/`public_id`/`resource_type`/etc. There is no foreign-key relationship between them; when media moves between collections, it is **copied**, never referenced. Two directions of copy exist:

1. **Mirror** (submission/application → talent), `sync_media_to_global_talent()` in `core.py`. Fires on every original (never-yet-finalized) submission upload and at finalize, for syncable categories only (see the sync table above). Copies the SOURCE item's fields into a new dict with a fresh `id`, `scope: "talent"`, and `source_submission_media_id`/`source_application_media_id` linkage back to where it came from.
2. **Prefill** (talent → new submission/application), `build_prefill_media()` in `submissions.py` (submissions) and the `should_hydrate_media` block in `applications.py`'s `_reconcile_draft_from_talent()` (applications). Copies a Library item's fields into a brand-new submission/application at start time, tagged `origin: "global"` (vs `"project"` for a genuinely fresh upload — see Phase 4.2's `ProjectOnlyBadge`).

**The critical fact this whole section exists to document**: both copy directions are *shallow* — the new dict gets the exact same `public_id` (Cloudinary) or `stream_uid` (Cloudflare Stream) as the source. **The physical storage object is never duplicated.** A talent's Library photo and the submission it was prefilled into (or mirrored from) are, at the storage layer, literally the same file. This is intentional (avoids doubling storage cost on every reuse) but means every delete/replace operation on ANY of the three collections must ask "does anyone else still need this file" before touching storage — see Reference-Aware Deletion below.

### Submission snapshots

A submission's `media[]` is a **point-in-time snapshot**, not a live view of the talent's current Library. Editing/deleting from the Library after a submission is finalized never changes what that submission shows — to the talent, to admin review, or on a Client Review Link (`_submission_to_client_shape()` renders `submissions.media` directly, live-computed on every view but always from THAT submission's own array, never the talent's current Library state). This snapshot guarantee is what makes historical submissions, shortlists, and client-facing links trustworthy over time even as a talent curates their Library.

The guarantee is enforced by two independent rules, not one:
- **Sync only ever fires pre-finalize** (`has_been_submitted_once(sub) == False` — Issue 2 / rule in the sync table above). A resubmission/edit never touches `db.talents`.
- **Reference-aware deletion** (below) means even a *destructive* action elsewhere (deleting the Library original, or removing a reused photo from a different submission) cannot destroy the storage object a finalized submission's snapshot still points at.

### Reference-aware deletion

Because storage is shared by value (see above), no delete path may unconditionally destroy a Cloudinary/Stream asset — it might still be needed by another collection's snapshot. Two functions in `core.py` centralize this, and **every** media-delete/replace call site in the backend goes through them:

- **`is_media_asset_referenced(public_id, stream_uid)`** — queries `db.talents`, `db.submissions`, and `db.applications` (indexed on `media.public_id`/`media.stream_uid`, see the Performance note below) for any remaining document that still points at this asset. Returns `True` if any do.
- **`safe_cleanup_media_storage(media, scope, parent_id, operation_id=None)`** — the ONLY function that should ever pair "check references" with "physically destroy." Calls `is_media_asset_referenced()`; only if nothing else references the asset does it delegate to `cleanup_media_storage()` (the actual multi-provider destroy: Cloudflare Stream by `stream_uid`, Cloudinary by `public_id`, R2 raw upload, `asset_metadata` tracking row).

Call sites using `safe_cleanup_media_storage()` (not the raw `cleanup_media_storage()`): `delete_talent_media_item()` (Library delete, both the admin and talent-owned routes — see below), the submission single-slot video-replace path, the talent-owned submission media-delete endpoint (`DELETE /public/submissions/{sid}/media/{mid}`), the Cloudinary webhook's intro-video-replace cleanup, the application media-delete endpoint, and `sync_media_to_global_talent()`'s own Library-side video single-slot replace. **Do not duplicate the reference-check + cleanup pairing at a call site — always go through `safe_cleanup_media_storage()`.** The DB `$pull` that removes the collection's own reference always happens *before* the reference check, so a document never counts itself as a remaining reference to its own now-deleted entry.

Exempt by design (not at risk, don't route through the wrapper): audition takes and voice-note feedback (`cloudinary_admin.py`'s project-level bulk-delete tools) are categorically never synced to `db.talents` (see "Audition Takes Never Sync" above) or shared with any other collection, so they're always safe to destroy unconditionally. The admin `run_storage_cleanup` health-cleanup tool builds its own independent, broader reference set (across `asset_metadata`/`submissions`/`talents`/`applications`/`feedback`) as part of its own orphan-detection logic — it is intentionally a separate mechanism, not a `safe_cleanup_media_storage()` caller.

### The Media Library Manager (talent-facing)

`routers/portal.py`: `DELETE /api/portal/media/{mid}` and `POST /api/portal/media/{mid}/cover`, authorized via `current_portal_talent` (the acting talent's id comes from the session token, never a URL parameter — a talent physically cannot address another talent's media). Both routes are thin wrappers around the exact same `core.delete_talent_media_item()` / `core.set_talent_cover_media()` the admin Talent Editor's `DELETE /talents/{tid}/media/{mid}` / `POST /talents/{tid}/cover/{mid}` routes call — one implementation, two authorization boundaries. Frontend: `frontend/src/pages-components/PortalProfile.jsx`'s "Media Library" section (view by category, delete, set cover, view intro video only — no upload/reorder/folders from this page, per the Version 1 scope decision).

### Canonical prefill: `build_prefill_media()`

The single source of truth (submissions.py) for turning a talent's Library media into the `prefill_media` a new submission starts with — replaces three previously independent, slightly-divergent implementations (`/public/prefill`, `start_submission`, `routers/auth.py`'s `_get_talent_profile_response`). Portfolio images come straight from `talent.media`, resource-type-checked so a miscategorized item can't leak into the wrong bucket. Intro video uses a 3-tier fallback: the talent's own Library → their most recent submission with a video → their most recent `/apply` application with a video. **Not yet consolidated**: `applications.py`'s own `_reconcile_draft_from_talent()` hydrate-media logic performs the equivalent talent→application copy independently, with its own category map (`_TALENT_TO_APP_CATEGORY`) — a fourth, still-separate implementation of the same "copy Library media by value" pattern. It received the same provider-metadata fix (below) but was not merged into `build_prefill_media()` in this pass (would be a genuine consolidation/redesign, not a certification-scope fix).

### Provider metadata integrity

Every copy operation above (mirror, prefill, application-hydrate) must preserve every field the source item carries, not a hand-picked subset — because lifecycle operations on the COPY (delete, reference-checking) need the exact same identifiers (`public_id`, `stream_uid`, `provider`, and whatever a future storage provider adds) the ORIGINAL had. Before the Phase 4.4 fix, `sync_media_to_global_talent()`'s mirror used a fixed whitelist that silently dropped `provider`/`stream_uid`/`thumbnail_url`/`poster_url`/`duration` — meaning a Cloudflare Stream intro video's Library copy had no way to ever be identified as a Stream asset again, so `is_media_asset_referenced()` and storage cleanup silently no-op'd for it forever (a real orphan-leak, confirmed live). The fix: mirror/hydrate now copy every field on the source item **except** an explicit deny-list of fields that describe the source document's ownership/location/processing-state (`id`, `scope`, `submission_id`, `origin`, `status`, `client_visible`, etc. — see the code comment in `sync_media_to_global_talent()` for the full, current list and rationale per field). A deny-list means a brand new provider-specific field added in the future is preserved automatically — no code change needed here when the next storage provider is added.

### Performance: indexes for reference-checking

`is_media_asset_referenced()` runs on every media delete/replace across the whole system (see the call-site list above) — at "thousands of submissions, years of accumulated media" scale this must be an indexed lookup, not a collection scan. Sparse indexes on `media.public_id` and `media.stream_uid` exist on `talents`, `submissions`, and `applications` (`core.py`'s `p0_indexes`, created idempotently at startup).

## Asset Metadata Tracking

### `db.asset_metadata` Collection
Every upload is tracked with:
- `upload_status`: `pending` -> `completed` | `failed`
- Links to talent_id, submission_id, application_id
- Cloudinary public_id, resource_type, format

### `db.storage_audit_log` Collection
Records all storage operations:
- `UPLOAD`: New asset uploaded
- `ARCHIVE`: Asset archived (not deleted)
- `RESTORE`: Archived asset restored
- `DELETE`: Asset permanently deleted

## Cloudinary Admin Operations

| Operation | Endpoint | Description |
|---|---|---|
| Analytics | `GET /api/admin/cloudinary/analytics` | Storage usage stats |
| Project breakdown | `GET /api/admin/cloudinary/projects` | Per-project storage |
| Archive project | `POST /api/admin/cloudinary/projects/{pid}/archive` | Archive assets |
| Restore project | `POST /api/admin/cloudinary/projects/{pid}/restore` | Restore archived |
| Delete project assets | `DELETE /api/admin/cloudinary/projects/{pid}` | Permanent delete |
| Delete talent assets | `DELETE /api/admin/cloudinary/talents/{tid}` | Permanent delete |

## Media Deduplication

### On Application Approval
When merging application media into talent:
- Dedup by `public_id`, `url`, `secure_url`, `asset_id`, `source_application_media_id`
- Existing media in the same category is replaced (not appended)

### Startup Migration
`run_media_duplicate_cleanup_migration` runs at backend startup:
- Deduplicates talent media by `public_id`
- Keeps the oldest copy (by insertion order)
