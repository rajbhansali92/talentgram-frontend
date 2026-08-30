# Cloudinary Re-architecture — P5 Delivery-Transformation Audit

**Phase:** P5 (delivery-time transformation elimination)
**Status:** Audit complete — implemented (see `docs/CLOUDINARY_P5_COMPLETION.md`)
**Scope rule:** RULE #1 — audit & classify every delivery transformation *before* editing.
**Constraints carried forward:** no asset deletion, no re-upload, no physical migration, no
regeneration of existing media, no ownership-model change, no Storage-Console work, no
weakening of signed/authenticated delivery.

---

## 0. Method

Enumerated every code path that *constructs or rewrites a Cloudinary delivery URL* (as opposed
to an upload/ingest call, which was P4). Sources: `grep` sweep of the repo for `cloudinary`,
`/upload/`, `f_auto`, `f_avif`, `q_auto`, `dpr_auto`, `fl_attachment`, `f_mp4`,
`stream_video_url`, `getVideoDownloadUrl`, `_get_video_download_url`, `video_poster_url`,
`media_url`, `cloudinary_url_for`, `IMAGE_URL`, `poster_url`, `thumbnail_url`, `vc_`, `w_`,
`h_`, `c_`, `e_` across `backend/` and `frontend/src/`.

Classification legend (RULE #1):
**A REQUIRED** · **B OPTIONAL** · **C UNNECESSARY** · **D DUPLICATED** · **E LEGACY** · **F UNKNOWN**

---

## 1. Inventory

### 1.1 Backend

| # | Source | Function / helper | Transformation string | Res. type | Caller / flow | Distinct variants | Derived asset? | Cached? | Billable? | Why it exists | Needed? | Class |
|---|--------|-------------------|-----------------------|-----------|---------------|-------------------|----------------|---------|-----------|---------------|---------|-------|
| B1 | `core.py:1006` | `cloudinary_url_for()` | `f_auto,q_auto,dpr_auto` (+kwargs) | image | only via `media_url()` | n/a (base) | yes | yes | yes (per distinct string; AVIF adds `extra_avif_mp_encoding`/MP) | generic "optimize any image" default | **partly** — `f_auto`/`q_auto` yes at small sizes; `dpr_auto` no | B1a `f_auto`=B · B1b `q_auto`=B · B1c `dpr_auto`=**C** |
| B2 | `core.py:1198` | `media_url()` presets | `roster` `c_fill,w_400,f_auto,q_auto,dpr_auto` · `thumb` `c_fill,w_200,...` · `detail` `c_limit,w_1200` · `full` `c_limit,w_1600` · `poster`→`video_poster_url` | image | `roster`: cover thumbnails (`update_talent_cover_cache`, `enrich_talent:1584`, `talents.py:201/1100`, `agents_whatsapp:419`). `thumb`: upload `/complete` handlers persist `thumbnail_url` (submissions/applications ×6). `detail`/`full`/`poster`: **zero callers**. | roster:1/cover · thumb:1/image · detail/full/poster: 0 | yes | yes | yes (small MP → AVIF surcharge negligible) | roster + pipeline cards need a small predictable image | roster/thumb **yes**; detail/full/poster **dead** | roster=**A** · thumb=**A** · detail/full/poster=**E** |
| B3 | `core.py:1024` | `stream_video_url()` | `c_limit,h_720,w_1280 / q_auto,vc_auto / f_mp4` (3 segments) | video | **only** `_public_media:2821`, and only when `is_video && public_id && cloudinary && !stream && !url` (legacy record, no stored delivery URL) | ~12 historical string variants | yes (720p transcode + downscale) | yes | **yes — video seconds (sd/hd)** | pre-2026 records saved before the app stored a ready delivery URL | **no** — a universal 720p delivery transcode is exactly what RULE #4 forbids; original (or P4 compat) should be served | **E** |
| B4 | `core.py:1165` | `video_poster_url()` | `c_fill,h_338,q_auto,w_600` + `.jpg` (ONE canonical string, P4) | video→image | `_public_media:2839` & `enrich_talent:1569` as a **fallback** when `media.poster_url` not persisted (legacy video) | 1 / video | yes (1 poster jpg) | yes | yes (1 image transform / video) | grid/list needs a still; native `<video>` poster not reliable pre-load | **yes** (product requirement); already canonical | **A** |
| B5 | `core.py:1182` | `compat_video_delivery_url()` | `f_mp4` (ONE canonical string, P4) | video | `_public_media` / router `/complete` when `video_needs_compat_delivery()` (HEVC / non-web container only) | 1 / compat video | yes | yes | yes (video seconds — unavoidable for a genuinely unplayable source) | P4 browser-compat exception | **yes** — narrowly scoped | **A** |
| B6 | `core.py:2839`, `core.py:1569` | poster fallback (calls B4) | see B4 | video | every client/enrich payload build, per video media item | 1 / video (B4 is deterministic) | yes (via B4) | yes | low (1 / video, not 1 / payload) | legacy video media has no persisted `poster_url` | acceptable; **polish** = persist on first compute so payload-build never recomputes | **C** (redundant recompute) → resolved by persisting |
| B7 | `links.py:1516` | `_get_video_download_url()` | strips `f_*`/`sp_*`, appends **`f_mp4`**, forces `.mp4` ext | video | `_resolve_video_download_url` → ZIP packaging (`links.py:1988`), `proxy_media` single download (`2196`), campaign bundle zip (`2361`) | up to 61 (per source string) | yes (**full re-transcode**) | yes | **yes — video seconds, the worst offender** | (a) guarantee `.mp4` filename, (b) legacy assumption that stored URL isn't downloadable | **no** — `proxy_media` already streams bytes through our origin and sets `Content-Disposition`; a web-safe MP4/H.264 original is already downloadable verbatim | **C** |

### 1.2 Frontend

| # | Source | Function | Transformation | Res. type | Caller / flow | Distinct variants | Derived asset? | Cached? | Billable? | Why it exists | Needed? | Class |
|---|--------|----------|----------------|-----------|---------------|-------------------|----------------|---------|-----------|---------------|---------|-------|
| F1 | `lib/api.js:51` | `IMAGE_URL()` | inserts `f_auto,q_auto/` as a segment right after `/upload/`, **at full resolution**, into every Cloudinary *image* URL that doesn't already contain `f_auto` | image | **12 files** — ClientView portfolio/cover render, TalentEdit, Applications, SubmissionReviewCenter, PortalProfile, MaterialModal, MediaGrid, TalentBrowserModal, AdminAddSubmissionModal, ApplicationPage, SubmissionPage | 1 / image (deterministic) but **full-MP** | yes (full-res AVIF/WebP) | yes | **yes — this is the ~10,996 `extra_avif_mp_encoding` units** (surcharge is per-megapixel, and this runs at native resolution) | HEIC (default iPhone camera format, an accepted upload) only renders in Safari; `f_auto` negotiates a renderable format | **only for HEIC/HEIF sources.** JPEG/PNG/WebP originals are already universally renderable — Cloudinary serves the stored file directly with zero transform | **C** for web-safe · **A** for HEIC |
| F2 | `ClientView.jsx:344` | `getVideoDownloadUrl()` | same `f_mp4` rewrite as B7 (client-side) | video | `runShare()` WhatsApp file-share (`:2201`), `download()` (`:2635`) | up to 61 | yes (full re-transcode) | yes | **yes — video seconds** | mirror of B7 for the client-side anchor-download path | **no** — route through `proxy_media` (CORS + `Content-Disposition` + P4-aware) or serve canonical URL | **C** |
| F3 | `ClientView.jsx:2652` | `download()` `fl_attachment` injection | `url.replace("/upload/", "/upload/fl_attachment:<name>/")` | image+video | single-file Download button on the client review page | **146 unique** `fl_attachment:<name>` strings | yes (1 derived variant / distinct name) | yes | yes | force `Content-Disposition: attachment` + set the saved filename | **no** — filename belongs in an HTTP header (RULE #7); `proxy_media` already emits `Content-Disposition: attachment; filename=…` | **D** / **C** |
| F4 | `ClientView.jsx` | `shareableMedia` / `onShareIntent` prewarm | Cloudflare Stream `POST /downloads` | video | share-intent | n/a | no (Stream, not Cloudinary) | — | no | Stream MP4 rendition warm-up | out of P5 scope | — |

### 1.3 Verified non-issues

- `lib/mediaUtils.js` `thumbnailUrl()` / `posterUrl()` — read stored `media.thumbnail_url` / `media.poster_url` only; **no URL building**.
- `resolveTalentCover()` — object selection; no transform.
- ZIP **image** packaging (`links.py:1946-1990`) — uses raw `m["url"]`, no transform. ✓ RULE #11.
- `feedback.py:218` voice-note — post-P4 `cloudinary_upload`, no eager/incoming transform.
- Backend `/complete` handlers persisting `video_poster_url(payload.url)` / `compat_video_delivery_url(payload.url)` — P4 behaviour, one canonical string each, persisted once. ✓

---

## 2. Mapping to the user's stated P5 cost sources

| User's cost source | Audit finding |
|---|---|
| 1. ~10,996 `extra_avif_mp_encoding` (full-res AVIF) | **F1** `IMAGE_URL` — full-resolution `f_auto` on every image render |
| 2. 146 unique `fl_attachment` strings | **F3** `download()` per-filename `fl_attachment:<name>` |
| 3. 61 download strings forcing full video retranscoding | **B7** `_get_video_download_url` + **F2** `getVideoDownloadUrl` (`f_mp4`) |
| 4. 5 different image `media_url` presets | **B2** — only `roster` + `thumb` are live; `detail`/`full`/`poster` are dead code |
| 5. `_public_media` per-payload poster recomputation (legacy) | **B6** — deterministic post-P4 (1 derivative/video, not 1/payload); polish = persist |
| 6. `stream_video_url()` legacy cold transforms | **B3** — legacy-only fallback, 720p transcode + downscale |
| 7. other dynamic transformation URL builders | **B1c** `dpr_auto`; **F2** client `f_mp4` |

---

## 3. Proposed P5 changes (implementation plan)

Centralised, deterministic, minimal. No behaviour change for legitimately-incompatible media
(P4 compat path preserved). No security change (the download proxy keeps viewer-token auth +
`visibility.download` gate).

### 3.1 Images — `IMAGE_URL` (F1) — **biggest win**
- Serve `media.url` (the canonical original) **directly** for web-safe formats.
- Apply a transform **only** when the source is HEIC/HEIF — detected from the URL extension,
  `media.content_type`, or `media.original_filename` (never guessed).
- The HEIC transform is **one canonical string**: `f_auto` (format negotiation only — drop
  `q_auto` and any implicit full-res re-encode intent; no width, no `dpr`).
- Idempotent + safe for the string input form (`IMAGE_URL("https://…")`).
- Expectation: eliminates ~all `extra_avif_mp_encoding` except genuine HEIC (a small minority).

### 3.2 Image thumbnails — `media_url` / `cloudinary_url_for` (B1, B2)
- Delete the dead `detail` / `full` / `poster` image presets (E). Keep `poster` routing only if
  a caller appears — currently none; `video_poster_url` is called directly.
- Collapse to **two named canonical helpers**: `ROSTER_THUMB` (`c_fill,w_400`) and
  `PIPELINE_THUMB` (`c_fill,w_200`). Both `f_auto,q_auto`, **no `dpr_auto`**.
- Drop `dpr="auto"` from `cloudinary_url_for` image defaults (B1c — adds a token, no benefit
  without client-hints; RULE #9 / cost-guard).
- These stay because roster + pipeline cards are a genuine product requirement (RULE #3), and
  at 200–400 px the AVIF surcharge is negligible.

### 3.3 Video delivery — `stream_video_url` (B3)
- `_public_media` legacy fallback: when no stored `url`, serve the **original** Cloudinary
  delivery URL for `public_id` (no transform), or the **P4 compat URL** iff
  `media.needs_compat_delivery`. Never a 720p downscale-transcode.
- Keep `stream_video_url` importable (deprecate in docstring, like `audition_video_transformation`).

### 3.4 Downloads — `_get_video_download_url` (B7) / `getVideoDownloadUrl` (F2) / `fl_attachment` (F3) — RULE #6/#7
- **Backend `proxy_media` + ZIP:** for Cloudinary videos, fetch the **canonical/original** URL
  (no `f_mp4`) unless `media.needs_compat_delivery` → then use the stored P4
  `compat_delivery_url` / `original_url`. The proxy already sets
  `Content-Disposition: attachment; filename="<safe name>"`, so filename needs no transform.
- Keep the `.mp4` **extension** fix as a pure string op only where the asset genuinely lacks
  one (no transform segment added).
- **Frontend `download()`:** route the client review single-file download through the
  `proxy_media` endpoint (already CORS + auth + `Content-Disposition`), removing the
  `fl_attachment:<name>` URL rewrite entirely (F3 → 0 variants).
- **Frontend `getVideoDownloadUrl()`:** stop appending `f_mp4` for Cloudinary; return the
  canonical URL (Stream / R2 branches unchanged).
- Cloudflare Stream + R2 download paths: **unchanged** (already direct, no Cloudinary transform).

### 3.5 Posters — `video_poster_url` (B4, B6) — RULE #5
- Keep the one canonical poster string (already P4).
- **Persist** the computed poster into `media.poster_url` the first time `_public_media` /
  `enrich_talent` has to fall back, so subsequent payload builds read the stored value.
  (Additive write to an existing field — no schema change.)

### 3.6 Not touched
- `compat_video_delivery_url` (B5) — correct as-is.
- P4 upload paths — untouched.
- Ownership model, Storage Console, cleanup — untouched.
- ZIP image packaging — already canonical.

---

## 4. Retained transformations after P5 (the complete allow-list)

| Transformation | Trigger | Derived asset | Cached | Expected frequency | Reason |
|---|---|---|---|---|---|
| `f_auto` (format only) on an image | image whose source is HEIC/HEIF | 1 / such image | yes | rare (HEIC minority) | HEIC renders only in Safari |
| `c_fill,w_400,f_auto,q_auto` | talent roster/cover thumbnail | 1 / talent cover | yes | 1 / talent | roster card needs a small predictable image |
| `c_fill,w_200,f_auto,q_auto` | pipeline / mini thumbnail on image upload `/complete` | 1 / image | yes | 1 / uploaded image | pipeline card requirement |
| `c_fill,h_338,q_auto,w_600` + `.jpg` | video poster (first render, then persisted) | 1 / video | yes | 1 / video | grid/list still frame |
| `f_mp4` | `video_needs_compat_delivery()` — HEVC / non-web container only | 1 / compat video | yes | rare (iPhone "High Efficiency" .mov) | genuinely unplayable source |

Everything else → **canonical asset delivered directly, zero transformation.**

---

## 5. Open UNKNOWNs

None. Every delivery transformation traced to a definite flow and class. `detail`/`full`
presets confirmed dead by repo-wide caller search (not "unknown — keep").
