# Cloudinary P4 — Eager-Transformation Audit (before code changes)

**Scope:** forward-looking behavior change for **new uploads** only. No cleanup, no deletion, no re-upload of existing assets, no migration of physical files, no change to the cleanup system. Existing `poster_url` / `thumbnail_url` / derived assets / legacy URLs keep working; historical submissions untouched.

**Method:** every `cloudinary.uploader.upload`, `api_sign_request`, `cloudinary_upload`, `upload_and_track_asset`, and provider path traced from source + cross-checked against the frontend and against production data (`railway run`, read-only).

---

## 1. Production reality (measured)

- `VIDEO_PROVIDER=stream` **but** August 2026 video media: **317 on `res.cloudinary.com`, 16 on Cloudflare Stream.** The `VIDEO_PROVIDER` / Stream routing is only consulted by the **retired R2 pipeline**; the *active* video paths are direct-signed Cloudinary uploads that carry an eager `f_mp4` transcode string and bypass `VIDEO_PROVIDER` entirely. **This is the source of the 56.65K video transformations + the 4K transcoding** (`vc_auto` on a 4K source bills 4K input-seconds even to produce 720p).
- The **re-upload chain** (`core.py:945–1009`): **zero live media items** carry its signature (`…/audition_web`, `…/thumbnail`, `…/original_*`). Confirmed dead.
- The eager `w_400` image thumbnail: **not what the app renders.** `submission_complete_upload` / `admin_add_media` store `thumbnail_url = media_url(public_id, preset="thumb")` = a **lazy `w_200`** transform. The eager `w_400` from the sign string is generated and never referenced.

---

## 2. Every eager / incoming-transformation path

| # | File:line | Function → route | RT | Transformation | Eager/incoming | Derived asset created | Business purpose (stated) | Used by frontend? | Recommendation |
|---|---|---|---|---|---|---|---|---|---|
| **1** | `submissions.py:887` | `submission_sign_upload` → `POST /public/submissions/{sid}/upload/sign` | image | `eager = w_400,c_fill,dpr_auto,f_auto,q_auto` | eager, sync | 1 `w_400` image (×DPR) | "pre-warm roster thumbnail" | **No** — app renders lazy `w_200` (`preset=thumb`) + full-res `IMAGE_URL` | **Remove eager** |
| **2** | `submissions.py:882` | `submission_sign_upload` (intro_video) | video | `eager = w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4 \| w_600,h_338,c_fill,q_auto,f_jpg` | eager, sync | 720p H.264 transcode **+** JPG poster | "compressed serve" + poster | poster: yes; 720p mp4: served as `url` when present | **Remove mp4 eager. Poster → lazy canonical, persisted at `/complete`.** |
| **3** | `submissions.py:884` | `submission_sign_upload` (take) | video | `transformation = w_1280,h_720,c_limit,q_auto,vc_auto` (**incoming — mutates the stored asset**) + `eager = w_600,h_338,c_fill,q_auto,f_jpg` | incoming + eager | transcoded stored asset + JPG poster | compress take | poster: yes | **Remove incoming transformation. Poster → lazy canonical.** |
| **4** | `submissions.py:1600` | `video_signature` → `POST /public/submissions/{sid}/video-signature` | video | `eager = c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4 \| c_fill,h_338,w_600,q_auto/f_jpg`, `eager_async=true` | eager, async | 720p H.264 + JPG poster | compressed serve + poster | **Yes — primary talent video path** | **Remove mp4 eager. Poster → lazy canonical.** |
| **5** | `submissions.py:3363` | `admin_add_media_sign` → `POST /projects/{pid}/submissions/{sid}/admin-media-v2/sign` | video | `eager = w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4 \| w_600,h_338,c_fill,q_auto,f_jpg` | eager, sync | 720p H.264 + JPG poster | **browser compatibility** (admin sources `.mov` etc.) + poster | **Yes — primary admin audition path** | **Remove mp4 eager. Add explicit compat exception at `/complete` (format/codec-gated, lazy `f_mp4`). Poster → lazy canonical.** |
| **6** | `submissions.py:3365` | `admin_add_media_sign` (image) | image | `eager = w_400,c_fill,dpr_auto,f_auto,q_auto` | eager, sync | 1 `w_400` image | pre-warm thumb | **No** (lazy `w_200` stored) | **Remove eager** |
| **7** | `applications.py:832` | `sign_application_upload` → `POST /public/apply/{aid}/upload/sign` | image | `eager = w_400,c_fill,dpr_auto,f_auto,q_auto` | eager, sync | 1 `w_400` image | pre-warm thumb | **No** | **Remove eager** |
| **8** | `applications.py:997` | apply `video-signature` → `POST /public/apply/{aid}/video-signature` | video | `eager = c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4 \| c_fill,h_338,w_600,q_auto/f_jpg`, `eager_async=true` | eager, async | 720p H.264 + JPG poster | compressed serve + poster | **Yes — talent apply intro video** | **Remove mp4 eager. Poster → lazy canonical.** |
| **9** | `core.py:750–766` | `cloudinary_upload` (video, `keep_original=True`) | video | `eager = [{mp4 w_1280,h_720,c_limit,q_auto,vc_auto}, {jpg w_600,h_338,c_fill,dpr_auto,q_auto}]`, `eager_async=False` | eager, sync | 720p H.264 + JPG poster | compressed + poster | via `upload_and_track_asset` → `talents.py:1064` (admin global talent video); also `auth.py:301`, `agents_whatsapp.py:372`, `projects.py:209` | **Remove video eager. Poster → lazy canonical.** |
| **10** | `core.py:770–779` | `cloudinary_upload` (image) | image | `eager = [{w_400,c_fill,dpr_auto,fetch_format=auto,quality=auto}]`, `eager_async=False` | eager, sync | 1 `w_400` image | pre-warm thumb | via `add_media` (admin global talent image), `scout_capture`, `projects.materials`, `auth /upload` | **Remove eager** |
| **11** | `core.py:731–747` | `cloudinary_upload` (video, `keep_original=False` **and** `len(data) > 300 MB`) | video | incoming `transformation = [{w_1280,h_720,c_limit},{q_auto,vc_auto}]` + `format=mp4` + eager JPG poster | incoming + eager | transcoded stored asset + JPG poster | **size-based transcode** | `agents_whatsapp`, `admin_add_media` multipart | **Remove — the policy explicitly forbids size-based transcode.** (Also unreachable in practice: submission video cap is 200 MB.) |
| **12** | `core.py:945–1009` | `upload_and_track_asset` re-upload chain (`resource_type=="video" and not keep_original`) | video | temp upload → eager `[{mp4 w_1920,h_1080,c_limit,vc_h264,bit_rate=5m,q_auto},{jpg}]` → **re-upload the mp4 as `…/audition_web`** → **re-upload the jpg as `…/thumbnail`** → `cloudinary_destroy(temp)` | eager + 2 extra full uploads | **3–4 Cloudinary assets** for one logical video (temp destroyed; `audition_web`; `thumbnail`) | "store only the compressed derivative" | **No** — only reachable via the multipart `POST /public/submissions/{sid}/upload` endpoint, which the frontend never posts to (it uses `/sign`+`/complete` or the chunked video path). **0 live media reference `…/audition_web` or `…/thumbnail`.** | **Collapse to a single `cloudinary_upload(keep_original=True)`.** No existing reference can break (nothing references its output). |

### Payload-time poster recompute (not an *upload* eager — flagged, minimal P4 touch; full fix = P5)
`core._public_media` (`core.py:2855`), `core.enrich_talent` (`core.py:1585`), `webhooks.py:131/207` call `video_poster_url(public_id)` as a fallback when `poster_url` isn't stored — and `video_poster_url` uses `dpr:auto` (→ per-DPR posters) and a different string than the eager one (→ the 7 poster variants). **P4 poster policy fix:** (a) pin `video_poster_url()` to ONE canonical string (drop `dpr_auto`, fix segment order); (b) make every `/complete` + webhook **persist** `poster_url` with that one string, so the recompute fallback effectively never fires for new media. (The fallback code itself stays for legacy media — removing it is P5.)

### Dormant / already-correct (NOT touched by P4)
- `providers.CloudinaryProvider` eager (`providers.py:60–62`) — only when `VIDEO_PROVIDER=cloudinary`; prod is `stream`.
- `core.trigger_cloudinary_transcode` (`core.py:4463`) + `video-complete` R2 branch (`submissions.py:1672`) — the retired R2 pipeline; guarded so it can only match pre-retirement sessions.
- `feedback.py:218` voice notes — audio → `is_audio` skips eager. **Bug found:** a recorder blob sent as `video/webm`/`video/ogg` (Chrome does this) is *not* matched as audio → falls into the video eager branch → a 720p transcode + poster for a voice note. **P4: fix the audio detection to include audio-only `video/webm`/`video/ogg`.**

---

## 3. Re-upload chain (`core.py:945–1009`) — dependency analysis (as instructed)

| Question | Answer |
|---|---|
| Why does it exist? | To store *only* a compressed 1080p H.264 derivative and discard the heavy original — the `keep_original=False` strategy. It uploads the raw bytes, asks Cloudinary for an eager mp4+jpg, then **re-uploads those derivative URLs back into Cloudinary as brand-new originals** (`audition_web`, `thumbnail`) and destroys the temp. |
| How many Cloudinary assets does one upload create? | Up to **4**: `…/original_{id}` (temp, destroyed at the end), the eager mp4 + eager jpg on the temp (derived), `…/audition_web` (re-uploaded mp4 as a new original), `…/thumbnail` (re-uploaded jpg as a new original). Net persisted: 2 originals + their own derived fan-out. |
| Is the original retained? | **No** — `cloudinary_destroy(f"{folder}/original_{media_id}")` at line 1009. |
| Is the transformed copy referenced? | The function *returns* `audition_web`'s `secure_url` and `thumbnail`'s `secure_url`. But **no live media item in production has a `public_id` ending in `/audition_web` or `/thumbnail`** (checked all of `talents/submissions/applications .media[]`). |
| Is any copy necessary? | No. It's only reachable through the multipart `POST /public/submissions/{sid}/upload` endpoint with `asset_type="audition_video"`. The frontend never posts to that endpoint (it uses `/sign`+`/complete` for images and the chunked `video-signature`/`video-complete` for video). |
| Can it be eliminated without changing existing asset references? | **Yes.** Nothing references its output, so replacing it with a single `keep_original=True` upload changes no live URL. |

**P4 action:** in `upload_and_track_asset`, treat `resource_type=="video"` identically to the `else` branch — one `cloudinary_upload(keep_original=True)`, storing the original. Keep the `keep_original` parameter in the signature (callers pass it) but stop branching on it for video.

---

## 4. P4 target behavior (new uploads)

```
UPLOAD → ONE CANONICAL CLOUDINARY ASSET (the original) → DB record → NO automatic derivatives
```

| Asset | Before | After (P4) |
|---|---|---|
| Image (talent global, submission, application, admin, scout, project material) | original + eager `w_400` | **original only.** `thumbnail_url` stays a **lazy** `media_url(preset="thumb")` string (one canonical, generated on first request — the deliberate, single thumbnail the policy allows). HEIC→web handled by the existing lazy `IMAGE_URL` delivery transform. |
| Video (talent intro, talent take, apply intro, admin audition, admin global) | original + eager 720p H.264 mp4 (+ 4K input transcode) + eager JPG poster | **original only, served as `url`.** No transcode, no 4K processing, no ABR, no size-gate. |
| Video — **explicit compatibility exception** | (implicit — everything transcoded) | at `/complete`, if the uploaded `format`/codec is non-web (`format ∈ {avi,wmv,flv,mkv,mpeg,mpg,ogv,rm,…}` **or** `codec ∈ {hevc,h265,hvc1,hev1,prores,dnxhd,mjpeg,wmv1/2/3,mpeg4,…}`) → `url` = one canonical **lazy** `f_mp4` delivery string. Flag `needs_compat_delivery: true` for observability. Gated on a detected condition, one string on first playback — not an eager, not the default. **(Merge-check correction: HEVC/H.265 added — not decodable on Firefox ESR / non-hardware Chrome. See `docs/CLOUDINARY_P4_COMPLETION.md` §4 for the full matrix.)** |
| Video poster | 7 fragmented eager/lazy strings, recomputed per payload | **one canonical string** (`c_fill,h_338,w_600,q_auto/f_jpg`, no dpr), computed once and **persisted** in `poster_url`/`thumbnail_url` at `/complete` + webhook. Lazy (generated on first grid render). Kept because `LazyVideoPlayer` / `MediaGrid` genuinely need it for the poster-first UX (which is itself a bandwidth win — no video bytes until click). |
| Re-upload chain | 3–4 assets/video | one `keep_original=True` upload |

## 5. Compatibility / DB compatibility

- No existing field removed. `poster_url`, `thumbnail_url`, `eager` (payload), `provider`, `stream_uid`, `original_url` all stay.
- Legacy media keeps its stored URLs; historical submissions render unchanged (they read `media[].url` live).
- New media: `url` = original; `thumbnail_url`/`poster_url` = one canonical lazy string each.
- `submission_complete_upload` / `admin_add_media_complete` / `sign_application_*` still return the same response shape (frontend needs no change beyond passing `format`/`video_codec` from Cloudinary's upload response, which is additive).

## 6. What P4 does NOT touch

- The 8,457 derived assets, 3,427 legacy derived, 2,634 orphan derived, 1,673 orphan originals — all remain P8/P9.
- Delivery-time fragmentation (`IMAGE_URL` full-res AVIF, `_get_video_download_url` `f_mp4` rewrites, the 146 `fl_attachment` strings, `stream_video_url` legacy cold transforms, the `_public_media` recompute *fallback* for legacy media) — all **P5**.
- The `MediaItem` model round-trip / deletion hardening — **P6**.
