# Cloudinary P4 — Completion Report

**Scope:** forward-looking behaviour change for **new uploads** only. **Nothing existing was deleted, re-uploaded, migrated, or regenerated.** No cleanup-system change. Historical submissions untouched; legacy `poster_url` / `thumbnail_url` / derived assets / legacy URLs keep working.

Audit that preceded the code: `docs/CLOUDINARY_P4_AUDIT.md`.

---

## 1. Transformations eliminated (at upload / asset-generation time)

| Eliminated | Where it was | What it did |
|---|---|---|
| **Eager 720p H.264 video transcode** | 5 sign strings + `core.cloudinary_upload` video branch | `w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4` on **every** video upload — the direct cause of ~56K video transformations and the `fourk_video_second` 4K-input billing |
| **Eager JPG poster** | same 5 sign strings + `core.cloudinary_upload` | `w_600,h_338,c_fill,q_auto,f_jpg` on every video upload |
| **Incoming (stored-asset-mutating) transformation on takes** | `submission_sign_upload` | `transformation=w_1280,h_720,c_limit,q_auto,vc_auto` — transcoded the stored asset itself |
| **Eager `w_400` image thumbnail** | 3 sign strings + `core.cloudinary_upload` image branch | `w_400,c_fill,dpr_auto,f_auto,q_auto` on every image upload — generated and never referenced (the app renders a lazy `w_200`) |
| **Size-gated video transcode** | `core.cloudinary_upload` (`len(data) > 300 MB`) | `w_1280,h_720` incoming transform + `format=mp4` |
| **1080p re-upload chain** | `core.upload_and_track_asset` (`keep_original=False` video) | uploaded raw → eager 1080p mp4 + jpg → **re-uploaded both derivatives as new originals** (`…/audition_web`, `…/thumbnail`) → destroyed the raw: **3–4 Cloudinary assets per video**. Confirmed dead (0 live media reference it; unreachable endpoint). |
| **Eager transcode on the retired R2→Cloudinary path** | `submissions.py` / `applications.py` `video-complete` R2 branch | `eager_transformation=` string → `None` (path already dormant; provider = Stream) |
| **7 fragmented poster strings → 1** | `core.video_poster_url` | pinned to one canonical `c_fill,h_338,q_auto,w_600` + `.jpg` (dropped `dpr:auto`, fixed segment order). Recovers the public_id from a full URL, so bare-id and full-URL callers now produce the identical string. |
| **`video/webm` voice-note transcode** | `core.cloudinary_upload` (Chrome recorder blobs sent as `video/webm`) | incidentally removed — there is no video eager block any more |

## 2. Upload paths changed

| # | File · function | Route | Old | New |
|---|---|---|---|---|
| 1 | `submissions.py` · `submission_sign_upload` | `POST /public/submissions/{sid}/upload/sign` | signs `eager` (image `w_400`; video `mp4+jpg`) / `transformation` (take) | signs `{folder, public_id, timestamp}` only. `eager`/`transformation` still in the response, always `null`. |
| 2 | `submissions.py` · `video_signature` | `POST /public/submissions/{sid}/video-signature` | signs `eager` + `eager_async` | signs `{folder, public_id, overwrite, tags, context}` — no eager |
| 3 | `submissions.py` · `admin_add_media_sign` | `POST /projects/{pid}/submissions/{sid}/admin-media-v2/sign` | signs `eager` (image `w_400`; video `mp4+jpg`) | signs `{folder, public_id, timestamp}` only |
| 4 | `submissions.py` · `submission_complete_upload` | `…/upload/complete` | `url` = eager mp4 or `payload.url`; poster from eager jpg or lazy | `url` = uploaded original; **compat exception** (see §4); poster = one canonical lazy string, **persisted** |
| 5 | `submissions.py` · `attach_video_media` (used by `video-complete`) | `…/video-complete` | `url` = `secure_url`; poster = `video_poster_url` | + **compat exception** (reads `asset.video.codec` from the `cloudinary.api.resource` it already fetches); canonical poster |
| 6 | `submissions.py` · `admin_add_media_complete` | `…/admin-media-v2/complete` | parsed eager list for mp4/poster | `url` = original; compat exception; canonical poster |
| 7 | `submissions.py` · `submission_upload` (legacy multipart) | `POST /public/submissions/{sid}/upload` | via `upload_and_track_asset`, `keep_original` toggled the re-upload chain | single canonical upload; compat exception; canonical poster |
| 8 | `applications.py` · `sign_application_upload` | `POST /public/apply/{aid}/upload/sign` | signs `eager` `w_400` | signs `{folder, public_id, timestamp}` only |
| 9 | `applications.py` · `app_video_signature` | `POST /public/apply/{aid}/video-signature` | signs `eager` + `eager_async` | no eager |
| 10 | `applications.py` · `app_video_complete` | `…/video-complete` | `url` = `secure_url`; poster = `video_poster_url` | + compat exception; canonical poster |
| 11 | `talents.py` · `add_media` | `POST /talents/{tid}/media` | via `upload_and_track_asset` eager | single canonical upload; compat exception; canonical poster; lazy `w_400` roster thumb kept |
| 12 | `core.py` · `cloudinary_upload` | (shared helper — feedback, scout, auth `/upload`, project materials, WhatsApp media assignment, `admin_add_media`) | eager per type | **no eager, ever.** Returns `video_codec` for callers. |
| 13 | `core.py` · `upload_and_track_asset` | (shared) | re-upload chain for `keep_original=False` video | single `cloudinary_upload(keep_original=True)`. `keep_original` kept in the signature, no longer branches. |

## 3. Old vs new behaviour

```
BEFORE (per new upload)
  image  →  original  +  eager w_400 (×DPR)  [+ lazy w_200 thumbnail_url, + on-render f_auto/AVIF]
  video  →  original  +  eager 720p H.264 transcode (4K-input billed)  +  eager JPG poster
           (audition_video via the dead multipart path: 3–4 assets)

AFTER (P4)
  image  →  original ONLY.  thumbnail_url = one lazy canonical w_200 string. HEIC→web still
            handled by the frontend's lazy IMAGE_URL delivery transform (unchanged).
  video  →  original ONLY, served as `url`.  poster_url / thumbnail_url = one lazy canonical
            poster string, PERSISTED on the media item at /complete.
  video, non-web container/codec (e.g. .avi, ProRes) → `url` points at ONE canonical lazy
            f_mp4 delivery string; `original_url` + `needs_compat_delivery: true` recorded.
```

Every media-item field that existed still exists. New fields are additive: `original_url` and `needs_compat_delivery` (only on the rare compat asset), `video_codec` in the internal `cloudinary_upload` return.

## 4. The one intentionally-retained transform — the browser-compat exception

`core.video_needs_compat_delivery(fmt, codec)` returns `True` only for:
- `codec ∈ {prores, dnxhd, mjpeg, wmv1/2/3, vc1, mpeg4, msmpeg4*, rv30/40, theora, flv1, cinepak, svq3}`, or
- `format ∈ {avi, wmv, flv, mkv, mpeg, mpg, ogv, rm, rmvb, asf, vob, divx}`.

Unknown format **and** unknown codec → `False` (serve the original — a genuinely unplayable asset is recoverable; transcoding-by-default is the cost problem we removed). `.mp4` / `.webm` / `.mov` with h264/hevc/vp9 → served as-is.

When it fires, `core.compat_video_delivery_url(public_id)` produces exactly **one** string: `…/video/upload/f_mp4/<public_id>.mp4` — a lazy delivery transform generated by Cloudinary on first playback, not an eager, not stored-asset-mutating, one canonical string per asset. The uploaded original is kept and recorded as `original_url`.

The frontend forwards Cloudinary's detected `format` + `video.codec` (`UploadManagerContext.jsx`, `directVideoUpload.js`); the server-mediated paths read them from the `cloudinary.api.resource` / upload response they already have.

## 5. Tests

**New — `backend/tests/test_p4_no_eager_transformations.py` (29 tests):**
- pure helpers: `video_needs_compat_delivery` truth table (13 cases), `video_poster_url` is one canonical string / bare-id == full-URL / rejects non-Cloudinary, `compat_video_delivery_url` forces `f_mp4` exactly once, `audition_video_transformation()` has no callers.
- `cloudinary_upload` (mocked `cloudinary.uploader.upload`, kwargs inspected): image / video / large-video upload **request no `eager`, no `transformation`, no `format`**; return surfaces `video_codec`.
- `upload_and_track_asset` video with `keep_original=False`: **exactly one** `cloudinary.uploader.upload` call, no `…/audition_web`, no `cloudinary_destroy`.
- **cost-safety guards** on the sign endpoints (submission image, submission video, admin-media-v2, application image): the signed params and the whole response contain **none** of `w_1280 h_720 w_1920 h_1080 vc_auto f_mp4 w_400 w_600 h_338 f_avif dpr_auto eager` — these fail loudly if any eager is reintroduced.
- `/complete`: video stores the original + a canonical persisted poster and is **not** transcoded; the `.avi` compat case produces one `f_mp4` delivery + `original_url` + `needs_compat_delivery`; image stores the original with no `w_400`/`f_avif`/`dpr_auto` baked in.

**Updated — `backend/tests/test_direct_uploads.py`:** `test_video_signature_asynchronous_transformations` → `test_video_signature_requests_no_transformation`; `test_app_video_signature_success` → `test_app_video_signature_requests_no_transformation` (both now assert absence of eager — cost-safety direction).

**Full regression sweep (run per-file, the repo convention):**

| Suite | Result |
|---|---|
| `test_p4_no_eager_transformations` | **29 passed** |
| `test_direct_uploads` | 23 passed |
| `test_upload_lifecycle` | 3 passed |
| `test_media_assignment` | 52 passed |
| `test_scout_capture` | 27 passed |
| `test_media_classification` (P3) | 73 passed |
| `test_p3_media_ownership` (P3) | 8 passed |
| `test_talent_update_media_preservation` | 1 passed |
| `test_storage_health_cleanup_disabled` | 2 passed |
| `test_storage_console_rebuild` | 21 passed |
| `test_talents_tagging` | 34 passed · `test_talent_folder_pdf` | 6 passed · `test_file_signature_validation` | 1 passed |
| `test_cloudinary_migration` | 1 failed + 7 errors — **pre-existing** (live-HTTP integration test, "Admin login 404"; identical on `main`) |
| `test_feedback_relay` | 9 errors — **pre-existing** (`ConnectionRefusedError`; identical on `main`) |

**P4 introduces zero new test failures.** Server imports cleanly (292 routes).

## 6. Compatibility considerations

- **DB:** no field removed. `poster_url`, `thumbnail_url`, `eager` (payload), `provider`, `stream_uid`, `original_url` all remain. New media: `url` = original; `poster_url`/`thumbnail_url` = one canonical lazy string; `original_url`/`needs_compat_delivery` only on compat assets.
- **Legacy media:** keeps its stored URLs and derived assets. Historical submissions render unchanged (they read `media[].url` live).
- **Frontend response shape:** unchanged — sign endpoints still return `eager`/`transformation` keys (now `null`). Frontend change is a 2-line additive forward of `format`/`video_codec`.
- **`_public_media` / `enrich_talent` poster recompute fallback:** left in place for legacy media; it now emits the canonical string too. Removing the fallback entirely is **P5**.

## 7. Remaining transformation paths (NOT P4 — deferred to P5)

- Delivery-time full-resolution AVIF (`IMAGE_URL` frontend rewrite → `extra_avif_mp_encoding` ~11K units).
- `_get_video_download_url` / `ClientView.getVideoDownloadUrl` `f_mp4` rewrites + the 146 `fl_attachment:` download strings + `stream_video_url` legacy cold transforms.
- The 5 image `media_url` presets (`roster`/`thumb`/`detail`/`full`) — collapse to one.
- The `_public_media` per-payload poster recompute *fallback* for legacy media.

## 8. Estimated transformation reduction

Baseline: **70.94 transformation credits / cycle** (`sd_video_second` 21,472 + `hd` 591 + `fourk` 1,165 + `transformation` 10,687 + `extra_avif_mp_encoding` 10,996 units).

| Removed by P4 (for new uploads, going forward) | Est. credit impact |
|---|---:|
| Eager 720p transcode on every video (incl. 4K input) — `sd_video_second` + `hd` + `fourk` | **≈ −23K units → ≈ −30 credits** once the current video corpus stops being re-transcoded on new uploads |
| Eager JPG poster on every video (part of `transformation`) | ≈ −1–2 credits |
| Eager `w_400` on every image (part of `transformation`) + its AVIF fan-out | ≈ −3–5 credits |
| Re-upload chain (dead, but removes future 4-asset fan-out risk) | structural |

**P4 alone targets ~35–40 of the 70.94 transformation credits** (~50%), entirely from processing the product never needed. It does **not** touch the delivery-time AVIF (~15 credits) or the download-string fragmentation — those are P5. Combined P4+P5 is the path back under the 60-credit included tier (per the Phase-0 cost model).

Storage growth also slows: new videos no longer store a 720p copy alongside the original; new images no longer store a `w_400` eager.

## 9. What P4 did NOT do

No deletion of the 8,457 derived / 3,427 legacy derived / 2,634 orphan derived / 1,673 orphan originals (P8/P9). No physical re-upload or folder migration. No change to the cleanup system, deletion logic, ownership model, or P5 delivery paths.
