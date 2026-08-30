# Cloudinary Re-architecture — P5 Completion Report

**Phase:** P5 — delivery-time transformation elimination
**Branch:** `p5/delivery-transformation-reduction`
**Status:** implemented + tested — **awaiting review/merge approval** (do not merge without it)
**Audit:** `docs/CLOUDINARY_P5_AUDIT.md` (RULE #1 inventory, A–F classification)

Companion to P4 (`0a92cb6`, upload-time). P4 stopped generating derivatives when media
is *ingested*; P5 stops generating them when media is *delivered* — the canonical
Cloudinary asset now goes straight to the browser / download / ZIP.

---

## 1. Exact transformations ELIMINATED

| Source | Before | After |
|---|---|---|
| **`IMAGE_URL()`** (`frontend/src/lib/api.js`) | injected `f_auto,q_auto/` as a segment into **every** Cloudinary image URL, **at full resolution**, on every rendering surface (12 components) → full-res AVIF/WebP re-encode per image | web-safe JPEG/PNG/WebP → **canonical asset, zero transform**. `f_auto` (format only, no `q_auto`) applied **only** to a HEIC/HEIF source |
| **`_get_video_download_url()`** (`backend/routers/links.py`) | stripped `f_*`/`sp_*`, appended **`f_mp4`**, forced `.mp4` ext → full video **re-transcode** on every download / ZIP entry | returns the **canonical asset URL** (existing stale transform segment stripped, none added, extension untouched) |
| **`getVideoDownloadUrl()`** (`ClientView.jsx`) | same client-side `f_mp4` rewrite for the Download button + WhatsApp file-share | same as above — canonical URL, no `f_mp4` |
| **`stream_video_url()`** (`backend/core.py`) | `c_limit,h_720,w_1280 / q_auto,vc_auto / f_mp4` — a universal **720p downscale + transcode** on first client view of any legacy Cloudinary video | returns the **untransformed canonical** delivery URL; deprecated; sole caller (`_public_media` legacy fallback) rerouted |
| **`media_url()` presets** (`backend/core.py`) | 5 presets: `roster` w_400, `thumb` w_200, `detail` w_1200, `full` w_1600, `poster` | **2 presets** (`roster`, `thumb`). `detail`/`full`/`poster` deleted — **zero callers** (verified by repo-wide search). Unknown preset → `roster` (small), never an uncapped transform |
| **`cloudinary_url_for()` image default** | `f_auto, q_auto, **dpr_auto**` | `f_auto, q_auto` — `dpr_auto` removed (needs client-hints, never configured; pure URL-token bloat) |
| **`fl_attachment:<name>`** download strings (`ClientView.jsx`) | 146 distinct billed strings — because the URL they modified **already carried** `IMAGE_URL`'s `f_auto,q_auto` (or `getVideoDownloadUrl`'s `f_mp4`) | `fl_attachment` now sits on a **canonical asset** (no other transform) → it is a pure delivery flag, **creates no derived asset, not billed**. Kept because it is the only non-`fetch()` way to force `Content-Disposition` inside the iOS-Safari user gesture (RULE #7) |

## 2. Exact transformations INTENTIONALLY RETAINED (complete allow-list)

| Transformation | Trigger | Derived asset | Cached | Frequency | Reason |
|---|---|---|---|---|---|
| `f_auto` (format only) on an image | source is HEIC/HEIF (url ext / `content_type` / `format` / `original_filename`) | 1 / such image | yes | rare (HEIC minority) | only Safari decodes HEIC; without this a non-Safari browser shows a broken image |
| `c_fill,w_400,f_auto,q_auto` (`media_url` `roster`) | talent roster / cover card thumbnail (persisted to `cover_thumbnail_url`) | 1 / talent cover | yes | 1 / talent | roster card needs a small predictable image; ~0.1 MP → AVIF surcharge negligible |
| `c_fill,w_200,f_auto,q_auto` (`media_url` `thumb`) | pipeline / mini thumbnail, persisted on image upload `/complete` | 1 / image | yes | 1 / uploaded image | pipeline card requirement |
| `c_fill,h_338,q_auto,w_600` + `.jpg` (`video_poster_url`) | video poster, first render then persisted to `media.poster_url` | 1 / video | yes | 1 / video | grid/list still frame; native `<video>` poster unreliable pre-load. Already ONE canonical string since P4 |
| `f_mp4` (`compat_video_delivery_url`) | `video_needs_compat_delivery()` — HEVC / non-web container **only** | 1 / compat video | yes | rare (iPhone "High Efficiency" .mov) | genuinely unplayable source across the supported browser matrix (P4 exception, unchanged) |

Everything else → **canonical asset, delivered directly, zero transformation.**

## 3. Before / after URL patterns

```
IMAGE (web-safe, e.g. portfolio JPEG)
  before : res.cloudinary.com/talentgram/image/upload/f_auto,q_auto/v17../talentgram/talents/<id>/<mid>.jpg
  after  : res.cloudinary.com/talentgram/image/upload/v17../talentgram/talents/<id>/<mid>.jpg

IMAGE (HEIC upload)
  before : res.cloudinary.com/talentgram/image/upload/f_auto,q_auto/v17../.../<mid>.heic
  after  : res.cloudinary.com/talentgram/image/upload/f_auto/v17../.../<mid>.heic

VIDEO download (legacy Cloudinary H.264 .mp4)
  before : res.cloudinary.com/talentgram/video/upload/f_mp4/v17../.../<mid>.mp4   (cold transcode)
  after  : res.cloudinary.com/talentgram/video/upload/v17../.../<mid>.mp4         (canonical)

VIDEO delivery (legacy record, no stored url)
  before : .../video/upload/c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4/<pid>       (720p transcode)
  after  : .../video/upload/<pid>                                                 (canonical)

ROSTER THUMBNAIL (unchanged — retained)
  before/after : .../image/upload/c_fill,w_400,f_auto,q_auto/<pid>   (dpr_auto dropped)

DOWNLOAD (single file, client review)
  before : .../image/upload/fl_attachment:Name/f_auto,q_auto/<pid>.jpg
  after  : .../image/upload/fl_attachment:Name/<pid>.jpg            (flag only, not billed)
```

## 4. AVIF reduction

The `extra_avif_mp_encoding` line (~10,996 units in the source data) is driven almost
entirely by **`IMAGE_URL`** encoding every rendered image to AVIF **at native resolution**
(the surcharge is per-megapixel). After P5:

- web-safe images (the overwhelming majority) → **no `f_auto`, no AVIF encode at all**.
- HEIC images → `f_auto` retained, but HEIC is a small fraction of the library, and the
  encode is a one-time cached derivative per asset (not per surface / per render).
- roster/thumb `f_auto` retained but at 200–400 px ≈ 0.04–0.16 MP → surcharge ~2 orders of
  magnitude smaller than a full-res encode.

**Expected: elimination of ~90%+ of `extra_avif_mp_encoding`**, bounded below only by the
genuine HEIC fraction of the catalogue. (Exact figure requires a post-deploy billing-cycle
read — P10.)

## 5. Download-transformation reduction

- **Video downloads / ZIP entries:** `f_mp4` re-transcode removed. A legacy Cloudinary
  H.264 MP4 (the common case — audition takes arrive from WhatsApp already H.264) now
  downloads the **stored bytes**, zero video-seconds billed. Only a P4 compat-flagged
  asset serves its (already-generated, cached) `f_mp4` URL.
- **`fl_attachment` strings:** the 146 distinct billed variants collapse to a
  non-billable delivery flag on the canonical asset.
- **Expected: elimination of the 61 "download strings forcing full video retranscoding"
  and the 146 `fl_attachment` transformation strings.**

## 6. Poster reduction

Already one canonical string per video since P4. P5 change: `_public_media` /
`enrich_talent` still fall back to `video_poster_url()` for legacy media with no persisted
`poster_url`, but that helper is deterministic → **1 cached derivative per video, not per
payload**. No further poster transforms introduced. (Optional future polish — persist the
computed poster on first fallback — deferred; it needs a write path and carries no billing
benefit since the derivative is already cached.)

## 7. Legacy-media transformation behaviour

- **No re-upload, no URL normalisation, no asset deletion.**
- Legacy records that already store a working delivery `url` are served that `url`
  verbatim (unchanged from before).
- Legacy records with **no** stored `url`: previously got a cold 720p transcode on first
  view; now get the canonical delivery URL (or the P4 compat URL if flagged non-web).
- Legacy stored URLs that carry a stale eager transform segment (e.g. an old
  `w_1280,h_720,…,f_mp4`) are **stripped back to the canonical asset** on the download
  path so the download doesn't pull a derived asset.

## 8. Client-review behaviour

Opening a client review link now causes, per RULE #10:

- **images:** direct canonical delivery, **no transformation** (HEIC excepted).
- **video playback:** the stored canonical delivery URL (unchanged) — or, for legacy
  no-url records, the canonical URL instead of a 720p transcode.
- **posters:** the persisted `poster_url`, or one canonical cached poster derivative.
- **no** per-open image resize, per-open AVIF encode, per-open video transcode, or
  multi-resolution generation.

Functionality preserved: playback, poster frames, portfolio lightbox, download button,
WhatsApp share, ZIP folder. Security preserved: `proxy_media` and folder-download keep
viewer-token auth + `visibility.download` gating; no private asset is exposed publicly to
dodge a transform.

## 9. ZIP behaviour

- **Images:** already packaged from the raw `m["url"]` — unchanged, no transform.
- **Videos:** `_resolve_video_download_url` no longer appends `f_mp4`. Cloudinary videos
  are streamed from the canonical asset; Stream videos use the MP4 rendition (unchanged);
  R2 objects are fetched directly (unchanged). Compat-flagged videos use their cached
  `f_mp4` URL.
- ZIP entry filenames (`Take_1.mp4`, `Introduction.mp4`) are set by the ZIP writer, not by
  a Cloudinary transform — unchanged.

## 10. Tests

| Suite | Result |
|---|---|
| `backend/tests/test_p5_delivery_transformations.py` (**new**, 20 tests) | **20 passed** |
| `frontend/src/lib/imageUrl.test.js` (**new**, 6 tests) | **6 passed** |
| `backend/tests/test_p4_no_eager_transformations.py` (48) | **48 passed** |
| `backend/tests/test_media_classification.py` (P3 Layer 1, 73) | **73 passed** |
| `backend/tests/test_storage_console_rebuild.py` (21) | **21 passed** |
| `backend/tests/test_client_payload_isolation.py` (6) | **6 passed** |
| `backend/tests/test_direct_uploads.py` (23) | **23 passed** |
| `backend/tests/test_talent_update_media_preservation.py` (1) | **1 passed** |
| `backend/tests/test_storage_health_cleanup_disabled.py` (2) | **2 passed** |
| frontend full suite (`vitest run`) | **315 passed** |

Pre-existing, **not P5-related**: `test_p3_media_ownership.py` (Layer 2) fails collection —
`mongomock_motor` is not installed in this environment (same on clean `main`). Several
`backend/tests/*` modules fail when *collected together* because each sets
`core.db = MagicMock()` at import — a documented pre-existing harness-ordering fragility
(identical failure set on clean `main`; every affected file passes in isolation).

P5 cost-regression guards (in the new suite) fail loudly if any of these return:
full-res `f_avif`, `dpr_auto`, universal `vc_auto`/`f_mp4` on video delivery, uncapped
`w_1200`/`w_1600` image transforms, download-time video transcoding, `stream_video_url`
re-acquiring a downscale chain.

## 11. Estimated transformation-credit reduction

Measured by *what the code now requests* (not a headline percentage):

| Cost line (source data) | P5 effect |
|---|---|
| `extra_avif_mp_encoding` ~10,996 units | **~90%+ removed** — full-res AVIF encode gone for all web-safe images; only genuine HEIC + tiny roster/thumb remain |
| download video re-transcode (61 strings, video-seconds billed) | **removed** — canonical asset served; only the rare P4 compat asset transcodes, once, cached |
| `fl_attachment` (146 distinct strings) | **removed as a billed line** — now a non-billable delivery flag |
| legacy `stream_video_url` 720p transcodes | **removed** — canonical delivery |
| 5 image presets | **collapsed to 2**; `dpr_auto` dropped from all |

Combined with P4's upload-side removals (est. −35–40 of the 70.94 transform credits), P5
targets the bulk of the **remaining** delivery-side spend — principally the ~11 AVIF
credits and the video-seconds attributable to downloads. A precise post-change credit
figure needs one full billing cycle on production (P10 verification).

## 12. Remaining UNKNOWN transformations

**None.** Every delivery transformation was traced to a definite flow and classified
(`docs/CLOUDINARY_P5_AUDIT.md` §1). `detail`/`full` presets were confirmed dead by
repo-wide caller search — removed, not preserved as UNKNOWN.

---

## Files changed

| File | Change |
|---|---|
| `backend/core.py` | `cloudinary_url_for` drop `dpr_auto`; `stream_video_url` → canonical (deprecated); `media_url` 5→2 presets; `_public_media` legacy video fallback → canonical / P4-compat |
| `backend/routers/links.py` | `_get_video_download_url` → canonical (no `f_mp4`, no ext force); `_resolve_video_download_url` → compat-aware; `proxy_media` filename ext tracks the real asset |
| `frontend/src/lib/api.js` | `IMAGE_URL` → canonical delivery; `f_auto` only for HEIC/HEIF |
| `frontend/src/pages-components/ClientView.jsx` | `getVideoDownloadUrl` → canonical (no `f_mp4`); `download()` `fl_attachment` comment (now a pure flag) |
| `backend/tests/test_p5_delivery_transformations.py` | **new** — 20 tests + cost-regression guards |
| `frontend/src/lib/imageUrl.test.js` | **new** — 6 tests |
| `docs/CLOUDINARY_P5_AUDIT.md`, `docs/CLOUDINARY_P5_COMPLETION.md` | **new** |

## Not touched (per P5 constraints)

Ownership model (P3) · Storage Console (P7) · deletion/cleanup (P6/P8/P9) · P4 upload
paths · `compat_video_delivery_url` · signed/authenticated delivery · no asset deleted,
re-uploaded, migrated, or regenerated.
