# Media Rules

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

## CRITICAL: Media Sync Rules

### What Syncs to Global Talent (`db.talents`)

| Source Category | Syncs To | Syncs? |
|---|---|---|
| `image` / `portfolio` | `portfolio` | YES |
| `indian` | `indian` | YES |
| `western` | `western` | YES |
| `video` / `intro_video` | `video` | YES (single slot, replaces) |
| `headshot` / `headshots` | `headshot` | YES |
| `additional_portfolio` | `additional_portfolio` | YES |
| **`take`** | -- | **NEVER** |
| **`take_1`** | -- | **NEVER** |
| **`take_2`** | -- | **NEVER** |
| **`take_3`** | -- | **NEVER** |

### CRITICAL RULE: Audition Takes Never Sync

**Audition take categories (`take`, `take_1`, `take_2`, `take_3`) must NEVER sync to the global talent record.**

These are project-specific audition recordings. They belong only to the submission. Syncing them to the global talent profile would:
- Contaminate the canonical talent portfolio with project-specific content
- Violate client confidentiality (auditions for one client visible to another)
- Bloat the talent media with ephemeral audition content

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
