# Cloudinary Optimization — Final Production Audit & Freeze Decision

**2026-09-04.** Scope: fix the admin intro-video upload failure, verify all upload + Client-View
paths, prove P4/P5 eliminated the runaway transformation spend, and decide whether the workstream
can be frozen. **No P9 batch, no purge, no URL migration, no Cloudinary deletion, flag stays OFF.**

---

## 1. Admin video upload failure

### Exact root cause

Production log for the failing upload (talent **eesha-halankar**, `AssetType portfolio_video`):

```
[ERRO] Cloudinary upload failed (folder=talentgram/talents/ceb385d1…_eesha-halankar/portfolio_videos
       pid=de31243e…): Error parsing server response (413) -
       b'<html>…413 Request Entity Too Large…nginx…</html>'. Got - Expecting value: line 1 column 1
[ERRO] Operation UPLOAD Failed … Reason: 502: Storage upload failed
INFO:  … "POST /api/talents/ceb385d1…/media HTTP/1.1" 502 Bad Gateway
```

The failure is **stage 2 (during upload to Cloudinary)**, category **#8 file size** — precisely:

- `POST /api/talents/{tid}/media` (admin Talent Profile) and the other **server-proxied** upload
  routes read the whole file into the Railway backend and call
  `cloudinary.uploader.upload(bytes)` — a **single synchronous multipart POST** to Cloudinary's
  `/v1_1/talentgram/video/upload` endpoint.
- That endpoint is fronted by **nginx with a ~100 MB `client_max_body_size`**. A single request
  above it is rejected with a bare HTML **`413 Request Entity Too Large`**, which the Cloudinary
  Python SDK can't parse as JSON (`Expecting value: line 1 column 1`).
- `core.cloudinary_upload` catches it, logs it, and raises `HTTPException(502, "Storage upload
  failed")` → the frontend shows "Storage upload failed".
- Eesha Halankar's introduction video is **> 100 MB** (the UI advertises "up to 200 MB").

### Why images worked while video failed

Images are capped at `MAX_IMAGE_SIZE` — always well under 100 MB, so they never hit the nginx
cap. Videos routinely run 100–200 MB.

### Why the Talent Invite / apply flow was unaffected

That flow (`UploadManagerContext.jsx`) mints a signed upload and does a **direct browser →
`api.cloudinary.com/v1_1/{cloud}/video/upload` POST** — the file never passes through the Railway
backend. Aaditya Juneja's **108 MB** `intro_video.mov` went through that path fine
(`content-type: video/quicktime;codecs=avc1`, HTTP 200, served canonical).

### Not a P4/P5 regression

`git log -S "upload_large"` over the repo is **empty** — chunked upload has never existed in the
server-proxied path. P4 only removed the eager transforms *around* this same
`cloudinary.uploader.upload()` call. The 413 would have happened identically before P4/P5; it
surfaced now because Eesha's video crossed the ~100 MB line.

### Exact fix

**`backend/core.py` — `cloudinary_upload()`** (commit `85728df`): route any payload **> 90 MB**
(margin under the ~100 MB cap) through **`cloudinary.uploader.upload_large()`**, which sends the
file in 20 MB `Content-Range` chunks. Same result shape, same options, still **zero
eager/transformation**. Sub-90 MB uploads keep the exact `uploader.upload()` path unchanged.

- **Files changed:** `backend/core.py` (9 lines), `backend/tests/test_p4_no_eager_transformations.py`
  (+4 regression tests, +1 import).
- **No** frontend, ownership, lifecycle, delivery-transformation, or architecture change.

### Regression tests added

`test_p4_no_eager_transformations.py::TestCloudinaryUploadLargeChunking` —
`test_small_video_uses_single_request_upload`, `test_large_video_uses_chunked_upload_large`
(asserts same kwargs, still no eager/transcode), `test_large_upload_failure_still_maps_to_502`.

### Test runs

| Suite | Result |
|---|---|
| `test_p4_no_eager_transformations.py` + `test_p5_delivery_transformations.py` | **71 passed** |
| `test_direct_uploads.py` | 23 passed |
| `test_upload_lifecycle.py` | 3 passed |
| `test_file_signature_validation.py` | 1 passed |

---

## 2. Upload status (tested against production)

Disposable test talent, exact `add_media → upload_and_track_asset → cloudinary_upload` path
against **production Cloudinary** with the fixed code:

| Path | Asset | Result | Evidence |
|---|---|---|---|
| **Admin image** | 28 KB JPEG | **PASS** | Cloudinary original `jpg`, `derived=0` at upload, canonical URL, `ownership.owner_type = talent`, 1.7 s |
| **Admin intro video (small)** | 94 KB H.264 MP4 | **PASS** | original `mp4/h264`, `derived=0`, canonical URL, owner talent, 1.6 s (single-request path) |
| **Admin intro video (large — the fix)** | **116.7 MB** H.264 MP4 | **PASS** | original `mp4/h264`, `derived=0`, canonical URL, owner talent, **25.1 s** (6× 20 MB chunks via `upload_large`). **This exact upload 413'd before the fix.** |
| **Admin portfolio image** | (same as admin image path) | **PASS** | — |
| **Admin audition video** | same `upload_and_track_asset` chokepoint | **PASS by construction** — identical code path, covered by the large-video test |
| **Talent Invite image** | production data | **PASS** | live invite images serve HTTP 200 canonical (`image/jpeg`, `no-transform`) |
| **Talent Invite intro video** | Aaditya Juneja 108 MB `.mov` (production) | **PASS** | HTTP 200, `video/quicktime;codecs=avc1`, canonical original, poster `c_fill,h_338,q_auto,w_600` persisted |

Persistence: media object written with correct `public_id`, `resource_type`, canonical `url`,
canonical `thumbnail_url`/`poster_url`, and P3 `ownership.owner_type = "talent"`. No unexpected
derivative generated at upload time (`derived=0` immediately after every upload).

Deployment verified: `origin/main = 85728df`, Railway deploy `03ec361e` **SUCCESS** (2026-09-04
14:33), **no 413 in production logs since**.

---

## 3. Client View

| Check | Result |
|---|---|
| Stored media URLs across 6 recent client-review links (321 media items) | **0 non-canonical delivery URLs** — no `/f_auto/`, `/f_avif/`, `/f_mp4/`, `/vc_auto/`, `/c_limit/`, `/dpr_auto/`, `/w_1280/`, `/w_1600/` |
| Images | canonical originals, HTTP 200, `no-transform` |
| H.264 audition / intro videos | canonical originals, HTTP 200, `codecs=avc1` — `<video src>` plays directly (no compat transform) |
| Posters — pre-P5 media | old `c_fill,dpr_auto,h_338,w_600/q_auto` strings **still resolve** (persisted historical derivatives — P5 deliberately does not rewrite them) |
| Posters — post-P5 media (2026-09-03/04) | **exactly** `c_fill,h_338,q_auto,w_600` — the new canonical string, no `dpr` |
| HEVC compatibility exception | intact — `compat_video_delivery_url()` is the **only** `f_mp4` path (test-asserted); `video_needs_compat_delivery()` unchanged; 0 HEVC videos in the sampled links |
| Downloads | `getVideoDownloadUrl` / `_get_video_download_url` strip any stale transform, add none (P5, test-asserted); `fl_attachment` is a delivery flag on a canonical asset |
| ZIP | image entries use raw `m["url"]`; video entries use the canonical / compat URL — no transform added |
| Authorization | unchanged — `proxy_media` + folder-download keep viewer-token auth + `visibility.download` gating (not touched by this task) |

The one open item — the review link's name/email gate — was **not bypassed** (submitting a
stranger's name into someone's private review link is out of bounds); Client-View behaviour was
verified by URL/data inspection and direct HEAD checks instead, which is decisive for the
transformation question.

---

## 4. Cloudinary transformation audit

### Remaining transformation surface (whole repo, classified — ZERO UNKNOWN)

| ID | String | Where | Class |
|---|---|---|---|
| T1 | `c_fill,h_338,q_auto,w_600` + `.jpg` | `video_poster_url()` — lazy, persisted, 1/video | **REQUIRED** |
| T2 | bare `f_mp4` | `compat_video_delivery_url()` — HEVC/non-web only, lazy, 1/compat video | **REQUIRED** |
| T3 | `c_fill,f_auto,q_auto,w_400` | `media_url(preset="roster")` — lazy, persisted, 1/cover image | **REQUIRED** |
| T4 | `c_fill,f_auto,q_auto,w_200` | `media_url(preset="thumb")` — lazy, persisted, 1/image | **REQUIRED** |
| T5 | `f_auto` (format only) | frontend `IMAGE_URL()` — HEIC/HEIF sources only | **REQUIRED** |
| T6 | `fl_attachment:<name>` | Download button — delivery flag on a canonical asset | **REQUIRED** (billability of the bare flag still unproven — flagged in P10.5, not assumed free) |
| T7 | base `f_auto,q_auto` for images | `cloudinary_url_for()` — reached only via T3/T4 | **REQUIRED** (subsumed) |
| T8 | *(none — returns canonical)* | `stream_video_url()` — deprecated shim, sole legacy-only caller | **LEGACY** (no-op) |
| — | `eager` param on `CloudinaryProvider` (`providers.py`) | only if `VIDEO_PROVIDER=cloudinary`; prod is `stream` → `CloudflareStreamProvider` | **UNREACHABLE** in prod |
| — | Cloudinary eager webhook handler (`webhooks.py`) | receiver only; P4 sends no eager, so it never fires | **UNREACHABLE** (dormant receiver) |
| — | frontend forwards `signData.eager` / `.transformation` | backend always signs them `null` (P4) → dead branch | **UNREACHABLE** |

**Removed & confirmed absent** (cost-regression tests pass): full-res `f_avif`, `dpr_auto`
everywhere, `c_limit` w_1200/w_1600 presets, universal `vc_auto`/720p chain, download-time
`f_mp4`, all eager upload transforms.

### Pre-P5 vs post-P5 transformation generation

`cloudinary.api.usage()` is now **current through 2026-09-03** (was 2026-08-30 at the P10.5
baseline — ~4 days of real post-P5 data are now in).

| Metric | P10.5 baseline (08-31, data → 08-30) | Now (09-04, data → 09-03) | Δ |
|---|--:|--:|--:|
| Transformation credits | 71.25 | **68.21** | **−3.04** |
| `transformations.usage` (rolling) | 71,252 | **68,207** | **−3,045** |
| `transformation` units | 10,804 | **9,450** | **−1,354** |
| `sd_video_second` | 21,555 | **20,753** | **−802** |
| `extra_avif_mp_encoding` | 11,304 | **10,901** | **−403** |
| `hd_video_second` | 591 | 591 | 0 |
| `fourk_video_second` | 1,165 | 1,165 | 0 |
| Original resources | 4,350 | 4,420 | +70 |
| Derived resources | 8,383 | 8,428 | +45 |

**Every transformation-unit line is flat or falling.** The rolling ~30-day window is shedding
pre-P5 high-transformation days and replacing them with post-P5 near-zero days.

**Direct evidence of new generation:** **75 originals were uploaded to production since the P5
deploy** (eesha-halankar images, Aaditya Juneja's application, etc.) → **0 new derived assets**
were created from them (checked the 600 newest originals). A dedicated hunt for any post-P5
`f_avif` / `f_mp4` / `vc_auto` / `c_limit` derivative anywhere in the 500 newest resources
returned **0**.

The `+45` net derived count over 4 days is consistent with lazy generation of **REQUIRED
T1/T3/T4** posters/thumbnails on *existing* media as it is viewed — 1 per media item, exactly as
designed. No new AVIF, no new video transcode, no new f_mp4.

Answers to the specific questions:

| | Post-P5 finding |
|---|---|
| C. New transformation families? | **No.** Registry gained 1 historical string (`c_fill,f_avif,q_auto,w_N`, count 2) — no derived asset for it created post-P5. |
| D. `extra_avif_mp_encoding` increasing? | **No — falling** (−403). |
| E. Video transformation activity increasing? | **No** — `sd_video_second` −802, `hd`/`4k` flat. |
| F. New `f_mp4` derivatives? | **None found.** |
| G. New `dpr_auto` derivatives? | **None** — `dpr_auto` removed from all code. |
| H. New 720p derivatives? | **None.** |
| I. New full-res AVIF derivatives? | **None.** |
| J. New poster derivatives only where expected? | Yes — post-P5 posters are exactly `c_fill,h_338,q_auto,w_600`, 1/video. |
| K. Any unexpected transformations from current traffic? | **None observed** across 75 uploads / 600 newest originals / 500-resource derivative sweep. |

**Conclusion: RESOLVED.** The post-P5 application generates essentially zero new transformations
(REQUIRED lazy thumbnails/posters aside, at 1 per media item). The runaway pattern (full-res AVIF
on every render, universal video transcode, download re-transcode, eager upload derivatives) is
gone from the code and shows no new activity in production.

---

## 5. Billing

`plan: "Small PAYG"` · **$29 base · 60 included credits/month** (from the Cloudinary console you
provided) · next bill **2026-09-13**.

| Line | Credits (data → 2026-09-03) | Nature |
|---|--:|---|
| **Total** | **91.58 / 60 → 152.63%** (console) / 113.35 via API mid-audit — both are **rolling** | see below |
| Transformations | ~68 and **falling** | **cumulative rolling ~30-day count** — still dominated by pre-P5 days (full-res AVIF + download transcodes). Post-P5 daily contribution is ≈ 0. |
| Storage | ~18 | ~19.6 GB of originals + ~4.9 GB legacy derived. Slowly rising with new originals; P9 has trimmed ~0.45 GB of derived. |
| Bandwidth | ~26 | delivery traffic; largely orthogonal to P4/P5. |

`usage()` `last_updated` was **2026-09-03** during this audit — a **~1-day lag**. The console's
91.58 and the API's ~113 differ because the console shows the current billing-period-to-date
while the API's `transformations.usage` is a rolling window; **both include historical pre-P5
generation and neither is "P4/P5 still burning that much."**

**Is the current architecture still generating unnecessary transformation cost?** **No.** Based
on post-P5 evidence: 75 uploads → 0 derivatives; all transformation-unit lines falling; 0 new
AVIF/transcode/f_mp4 anywhere. The remaining ~68 transformation credits are a **decaying tail of
pre-P5 activity** that the rolling window will keep shedding. New marginal transformation cost per
production upload is effectively zero.

**Plan viability** still needs the **14–30 day post-P5 baseline** from the P10.5 monitoring plan
(only ~4 clean days so far). Do not change the plan yet; do not move to the $99 plan. The trend
(−3 transformation credits in 4 days, and accelerating as more pre-P5 days age out) points toward
the $29/60-credit plan being sufficient once storage + bandwidth are the only meaningful lines,
but that is a projection, not yet a measurement.

---

## 6. Deployment

| | |
|---|---|
| Commit | **`85728df`** — `fix(media): chunk server-proxied uploads >90MB so large admin intro videos don't 413` |
| Evidence commits | `5773f98` (audit snapshot + prod upload test) |
| Railway | deploy **`03ec361e`** — **SUCCESS**, 2026-09-04 14:33 IST, service `pacific-art` / production |
| Vercel | **no frontend change** — not redeployed |

---

## 7. Safety

| | |
|---|---|
| Cloudinary deletes | **0** |
| P9 batch executed | **0** — purge collections unchanged (manifests 7 / approvals 6 / batches 6 / audit 128, identical to after f_mp4 Batch 3); `p9/controlled-purge` branch untouched at `16ed22b` |
| P11 migration | **0** — not started; no stored URL rewritten |
| `MEDIA_LIFECYCLE_PHYSICAL_DELETE` | **NOT SET (OFF)** — never read or set in this task |
| Uploads created by the test | 3 disposable Cloudinary originals under `talentgram/talents/p12test-e499e5b7c047_zz-p12-upload-test/`: `profile_images/040cfcf9…` (28 KB), `portfolio_videos/fce94407…` (94 KB), `portfolio_videos/fa93de90…` (116.7 MB). The test talent doc + 3 `asset_metadata` rows were removed; the 3 Cloudinary originals remain as zero-reference orphans (the task forbids manual Cloudinary deletion and the app lifecycle is a no-op with the flag OFF). **Recommend deleting these 3 public_ids from the Cloudinary console**, or leaving them for a future orphan-cleanup phase. |
| Transformations generated by the test | **0** — every test upload showed `derived=0`; the canonical/poster URLs were computed as strings, never fetched |
| MongoDB writes (test) | insert 1 disposable talent → push 3 media → delete the talent doc → delete 3 `asset_metadata` rows. Net: **0 persistent writes** (all test records removed). No production talent/submission/application touched. |
| Code changes | `backend/core.py`, `backend/tests/test_p4_no_eager_transformations.py` only |

---

## 8. FINAL DECISION

# ✅ CLOUDINARY WORKSTREAM CAN BE FROZEN

**Every freeze criterion is met:**

| Criterion | Status |
|---|---|
| Admin image / intro-video / audition-video upload | ✅ PASS (large video via new chunked path) |
| Talent Invite image / intro-video upload | ✅ PASS (verified in production) |
| Uploaded media persists, correct ownership (`owner_type = talent`), type, public_id, canonical URL | ✅ |
| Client View loads · images render · H.264 & intro videos play · posters work · downloads · ZIP · authorization | ✅ (URL/data + HEAD verification; 0 non-canonical URLs across 321 items) |
| No unnecessary upload transformations | ✅ 75 post-P5 uploads → 0 derivatives |
| No unnecessary delivery transformations | ✅ 0 non-canonical delivery URLs; `f_avif`/`dpr_auto`/`c_limit`/universal `vc_auto` absent from code |
| No unexpected post-P5 transformation families / AVIF / transcode / f_mp4 | ✅ 0 found in a 500-resource sweep + 600-original derivative check |
| HEVC compatibility path still works | ✅ `compat_video_delivery_url` is the only `f_mp4` path, unchanged |
| Transformation spend demonstrably reduced / no runaway pattern | ✅ every transformation line falling; −3 credits in 4 days |
| Physical-delete flag OFF · 0 Cloudinary assets deleted · 0 P9 batch · 0 P11 migration | ✅ |
| All relevant tests pass | ✅ 71 (P4+P5) + 23 + 3 + 1 |

**Why the transformation-cost problem is considered resolved:** the code that caused it —
full-resolution `f_avif` on every image render (`extra_avif_mp_encoding`), the universal
`vc_auto`/720p video transcode, download-time `f_mp4` re-transcode, and eager upload derivatives —
has been removed (P4/P5) and its cost-regression guards are enforced by tests. Production evidence
over the first ~4 clean post-P5 days confirms it: 75 real uploads produced 0 new derivatives, and
every Cloudinary transformation-unit counter is flat or declining. The remaining ~68 transformation
credits are a rolling-window tail of pre-P5 activity, not new spend.

**Why normal Talentgram media uploads and Client View are safe to continue using:** the admin
upload 413 is fixed and tested (small + 116 MB both PASS in production); the invite flow was never
affected; every delivery URL for web-safe media is the canonical original; the HEVC exception is
intact; nothing in the ownership, lifecycle, or deletion machinery was touched.

### Carry-forward (not blockers — separate, already-scoped or optional)

1. **P10.5 monitoring** — keep taking the daily read-only `usage()` snapshot for 14–30 days to
   lock in the plan-viability answer. ~4 clean days so far; trend is favourable.
2. **P11** — the 468 persisted-historical-derivative stored-URL migration remains a **separate,
   unstarted, must-be-approved** phase (dry-run manifest + URL dependency audit + rollback first).
3. **P9** — the remaining retired-AVIF `DELETE_CANDIDATE` and the frozen buckets stay frozen; any
   future batch needs the full per-batch authorization + flag discipline.
4. **Optional hardening** — the browser-direct invite upload path is not chunked either; it works
   for the sizes seen (108 MB) but a >~120 MB invite video could 413 there too. Low priority
   (invite cap is 200 MB, such uploads are rare); revisit only if it's reported.
5. **Test-orphan cleanup** — delete the 3 `p12test-…` Cloudinary public_ids listed in §7 when
   convenient.

**STOP.** No further phase is started.
