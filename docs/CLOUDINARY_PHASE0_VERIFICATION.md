# Cloudinary Phase 0 — Read-Only Verification Report

**Status:** Verification only. Zero writes performed. No asset deleted, modified, migrated, regenerated, or re-uploaded. No cleanup executed. Awaiting approval before Phase 1.

**What was run (all read-only):**
- `cloudinary.api.usage()` — account credits/storage/bandwidth/transformation breakdown
- `cloudinary.api.transformations()` + `cloudinary.api.transformation(name)` for all 202 transformation strings — enumerated every derived asset grouped by transformation
- `cloudinary.api.resources(resource_type=image|video|raw, type=upload)` — full original inventory (4,324 objects) with bytes/format/dimensions/created_at
- MongoDB reads (dev/staging DB — see boundary note) across `talents, submissions, applications, projects, asset_metadata, feedback, links, casting_pipeline` for reference cross-join

**Evidence files (scratchpad, not committed):** `usage.json`, `transformations.json`, `derived_assets.json` (8,457 rows), `originals_full.json` (4,324 rows), `STEP*_*.json`.

---

## ⚠️ CRITICAL BOUNDARY — which numbers are authoritative

`backend/.env` is the **local dev environment** config (`# Talentgram backend — local dev environment`, `R2_BUCKET_NAME=talentgram-media-staging`, `users` collection = 4 test accounts: `admin@example.com`, `pwtest+…@example.com`). But its `CLOUDINARY_CLOUD_NAME=talentgram` is the **single real billed Cloudinary account** (`Small PAYG`, 115.34/60 credits).

**Finding P0-0: dev/staging and production share ONE Cloudinary account.** Test runs, CI, and QA submissions (`unknown_talent_qa-submitter`, `videocert-admin-test`, `matrix-test`, `fake/intro_1`, `WhatsApp_*` test files) write into the same account production uses, and count against the same bill.

| Data class | Source | Authoritative? |
|---|---|---|
| Credits, storage GB, bandwidth GB, transformation-type breakdown | `cloudinary.api.usage()` | ✅ **Yes — measured, real account** |
| 4,324 originals / 8,457 derived / 19.52 GB, folder structure, formats, dimensions, created dates | Cloudinary Admin API | ✅ **Yes — measured** |
| Transformation-string inventory & fragmentation | Cloudinary Admin API | ✅ **Yes — measured** |
| Which specific assets are referenced vs orphaned | Cross-join vs **dev/staging** MongoDB | ❌ **No — needs production `MONGO_URL`.** Dev-DB numbers below are a contaminated lower bound + a reusable method |

**To finish H/I/J precisely, one of:** (a) provide a read-only production `MONGO_URL` and I re-run `p0_step10_join.py` against it, or (b) you run that script against prod yourself. Everything else in this report stands on measured Cloudinary data.

---

## SECTION A — EXACT CLOUDINARY USAGE EXPLANATION

### Live account state (`usage()`, cycle ending 2026-08-29) `[MEASURED]`

| Metric | Value | Credits | Share |
|---|---|---:|---:|
| **Credits total** | **115.34 / 60.0 included** | — | **192%** |
| Transformations | 70,938 units | **70.94** | **61.5%** |
| Bandwidth | 37.39 GB | 26.22 | 22.7% |
| Storage | 19.52 GB | 18.18 | 15.8% |
| Objects | 12,768 | — | — |
| Requests | 28,097 | — | — |
| Resources (originals) | 4,320 | — | — |
| Derived resources | 8,448 | — | — |

**Answer to "storage, bandwidth, or transformations": TRANSFORMATIONS — 70.94 of 115.34 credits (61.5%).** Bandwidth is second (26.22). Storage is the *smallest* driver (18.18). The account is 55 credits into paid overage; transformations alone (70.94) exceed the entire 60-credit included tier.

### Transformation credit breakdown (`usage().transformations.breakdown`) `[MEASURED]`

| Unit type | Units | What it is |
|---|---:|---|
| `sd_video_second` | **21,472** | Seconds of SD (≤720p) video transcoding — the 720p H.264 eager derivatives |
| `extra_avif_mp_encoding` | **10,996** | AVIF-encoding surcharge (per megapixel) — full-res `f_auto`→AVIF on images |
| `transformation` | **10,687** | Base transform operations — image resizes, poster frames, crops |
| `fourk_video_second` | **1,165** | Seconds of **4K source** video transcoding (very expensive per second) |
| `hd_video_second` | 591 | Seconds of 1080p video transcoding |
| `frame` | 1 | — |

**Plain English:** ~55% of transformation cost is **video transcoding seconds** (23,228 s across SD/HD/4K). ~25% is **AVIF image encoding** at full resolution. ~20% is base image ops. The bill is dominated by (1) transcoding every uploaded video to 720p even when the source is already web-playable, including transcoding 4K masters, and (2) re-encoding every full-resolution photo to AVIF on delivery.

---

## SECTION B — EXACT TRANSFORMATION SOURCES (traced to code)

### B.1 Derived-object breakdown by purpose `[MEASURED — all 8,457 derived enumerated]`

| Purpose | Distinct transform strings | Derived objects | Storage |
|---|---:|---:|---:|
| Image thumbnail `w_400` | **4** | 3,938 | 0.107 GB |
| Image full-res **AVIF** re-encode | **10** | 3,144 | 0.882 GB |
| Video **720p transcode** | **16** (12 with output) | 579 | 3.109 GB |
| Video **poster** JPG | **7** | 617 | 0.012 GB |
| **Download** (`fl_attachment:…`) — video re-transcode | **61** | 65 | 0.757 GB |
| Download — image variant | 47 | 42 | 0.029 GB |
| Download — image AVIF | 38 | 41 | 0.018 GB |
| Image `w_200` thumbnail | 1 | 2 | ~0 |
| Other / named / misc | 18 | 29 | 0.017 GB |
| **TOTAL** | **202** | **8,457** | **4.93 GB** |

### B.2 Transformation-string FRAGMENTATION — same output, many cache buckets `[MEASURED]`

**Answer to "is the same original transformed into multiple variants": YES, structurally.** Cloudinary caches per *exact* transformation string. Different code paths emit semantically identical but syntactically different strings, so the same logical derivative is billed and stored 4–16 times:

**720p video transcode — 12 distinct strings for ONE intended output:**
```
206×  c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4
125×  c_limit,h_720,w_1280/q_auto,vc_auto
113×  f_mp4                                        ← bare remux, near-original size (1.5 GB!)
 70×  c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4/   ← trailing slash
 44×  c_limit,h_720,w_1280,f_mp4/q_auto,vc_auto    ← comma order
 17×  w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4    ← different comma order
  3×  c_limit,h_720,w_1280/q_auto,vc_auto/mp4
  +   q_auto:good,vc_auto,f_mp4  /  f_mp4,vc_h265,…  /  f_mp4,vc_h264,…   (abandoned experiments)
```
Code sources: `core.stream_video_url()` (3-segment chain), `core.cloudinary_upload` eager (`core.py:750`), the sign strings (`submissions.py:882`, `submissions.py:3363`, `applications.py:997`), `links._get_video_download_url()` + `ClientView.getVideoDownloadUrl()` (strip `f_`, append `f_mp4`), `core.upload_and_track_asset` re-upload chain (`core.py:956`).

**Video poster — 7 strings:** `c_fill,dpr_1.0,h_338,w_600/q_auto/jpg` (265×), `c_fill,h_338,w_600,q_auto/f_jpg` (206×), `c_fill,dpr_auto,h_338,w_600/q_auto/jpg` (128×), `w_600,h_338,c_fill,q_auto,f_jpg` (18×)… Sources: `core.video_poster_url()` (`core.py:1201`), `webhooks.py:131/207`, `submissions.py` eager strings, the frontend `poster_url` passthrough.

**`w_400` thumbnail — 4 strings:** `w_400,c_fill,dpr_auto,f_auto,q_auto` (2,443×) vs `c_fill,dpr_auto,f_auto,q_auto,w_400` (1,468×) vs `c_fill,dpr_1.0,f_avif,q_auto,w_400` (273×)… Sources: eager sign strings vs `core.media_url(preset="roster")` (`core.py:1232`) vs `t_media_lib_thumb` named transform.

**Download — 146 strings, each unique & un-cacheable:** `fl_attachment:{filename}` embeds the human filename (`fl_attachment:Rtwik_N_video/f_mp4`, `fl_attachment:WhatsApp_Video_2026-08-19_at_19_30_11/w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4`, `fl_attachment:0O0A8953/f_avif,q_auto/jpg`). Every distinct filename = a fresh derived asset + a fresh transformation. 61 of these force a full video re-transcode (`f_mp4`). One even double-transcodes: `fl_attachment:Doha_K_take/c_limit,h_720,w_1280,f_mp4/q_auto,vc_auto/f_mp4`.

### B.3 Fan-out per original `[MEASURED]`

4,263 of 4,324 originals have ≥1 derived. Distribution: 1→985, **2→2,690**, 3→396, 4→87, 5→76, 6→27, 7→2. Video originals fan out to 6–7 (transcode variants + poster variants + download). **Strict transformations mode is OFF** (`allowed_for_strict=True` on only 2 of 202 strings) — nothing prevents the fan-out or the unbounded `fl_attachment` strings.

### B.4 Repeated client-payload generation `[CONFIRMED from code + fragmentation evidence]`

**Answer to "does repeated client payload generation create transformations": YES.**
- `core._public_media()` recomputes `video_poster_url(public_id)` on **every** client/roster/portal payload build for every video (`core.py:2855`) — and `video_poster_url` uses `dpr:auto`, so a poster derivative per video per DPR. The 3 poster string-variants with different `dpr` values (265× / 128× / 18×) are the fingerprint of this.
- `core.stream_video_url()` was historically recomputed on every client view for every Cloudinary video (`core.py:2814` comment: *"the client link was ALWAYS forcing a brand-new, never-before-requested on-demand transformation, cold, on the very first client view"*). Partially fixed 2026-08 (only fires now when no `url` stored) — legacy records still hit it.
- Frontend `IMAGE_URL()` (`api.js:51`) rewrites every image URL to full-res `f_auto,q_auto` on every render → the 3,144 AVIF re-encodes.

---

## SECTION C — EXACT ORPHAN / DUPLICATE / STORAGE ATTRIBUTION

### C.1 Storage attribution `[MEASURED — Cloudinary]`

| | Objects | Size |
|---|---:|---:|
| Image originals | 3,933 | 5.15 GB |
| Video originals | 380 | 9.46 GB |
| Raw originals | 11 | 0.01 GB |
| **All originals** | **4,324** | **14.62 GB** |
| Derived assets | 8,457 | 4.93 GB |
| **Total (matches `usage().storage` 19.52 GB)** | **12,781** | **19.55 GB** |

**Largest consumers `[MEASURED]`:** all top-7 storage folders are `talentgram/projects/{pid}/…` (5.9 GB combined) — and **all 7 of those project IDs do not exist** in the dev DB (0 submissions each). Video storage is concentrated: 23 videos > 100 MB = 4.18 GB (44% of video storage), mostly `.mov` (QuickTime) admin/desktop uploads and application intro videos kept as un-transcoded masters alongside their 720p derivatives.

### C.2 Reference / orphan counts

**Method note:** run against dev/staging Mongo. Contaminated by test churn; a lower bound on the *reference* side and therefore an upper bound on orphans that includes test artifacts. Rerun against prod for final numbers.

| | Originals | Size |
|---|---:|---:|
| Referenced by a live dev-DB doc | 665 | 0.02 GB |
| In `asset_metadata` only (no live doc) | ~47–90 | ~0 |
| **Not referenced anywhere (dev DB)** | **~3,565–3,659** | **~14.6 GB** |

Orphans by parent-document state (dev DB) `[dev-DB derived]`:

| Parent state | Objects | Size |
|---|---:|---:|
| Parent submission **deleted** | ~1,880 | ~10.6 GB |
| Parent application **deleted** | ~790 | ~2.7 GB |
| Parent talent **deleted** | ~815 | ~0.8 GB |
| Parent project **deleted** | ~134 | ~0.45 GB |
| Parent still alive (current-code bug) | **~3** | ~0 |
| Not classifiable (Cloudinary demo assets: `waves`, `paper`, `cloudinary-logo-vector`) | 28 | ~0 |

Orphans by folder scheme: `projects/` 315 / 7.3 GB · `submissions/` 1,656 / 3.5 GB · `applications/` 796 / 2.7 GB · `talents/` 819 / 0.8 GB · `admin_media/` 45 / 0.16 GB.

Orphans by month created: **2026-06: 4 · 2026-07: ~960 · 2026-08: ~2,690.** Almost the entire orphan population was created in the last ~8 weeks.

Derived assets (dev DB cross-join): ~840 referenced, ~7,520 orphaned (~4.9 GB).

**Interpretation:** essentially 100% of orphans trace to a **deleted parent document**, not to a bug in current code that leaves assets behind while the parent lives. This is consistent with the code audit: `delete_project`, `delete_submission` (`submissions.py:3491`), and `admin_remove_media_item` (`submissions.py:3470`) perform no Cloudinary cleanup, and `delete_project`'s prefix delete only covers `talentgram/projects/{pid}/`, missing `admin_media/` and `submissions/` schemes.

### C.3 Duplicates

- **True duplicates (same bytes, different `public_id`):** not separately confirmed without a content-hash pass — deferred (needs `cloudinary.api.resources` with `image_metadata`/`phash`, a heavier read). `[UNKNOWN — flag for a follow-up read-only pass if you want it]`
- **"Duplicate" as the current `/health` scan defines it (same `public_id` in >1 Mongo doc):** 362 leaves in the dev DB. **These are legitimate copy-by-value reuse** (`build_prefill_media` / `from-library` / `sync_media_to_global_talent`), not duplicate uploads. The scan mislabels intentional reuse as a defect.
- **Semantic duplication via transform fragmentation:** the 4–16 strings per logical output in §B.2 — this is the real "duplication" and it's a code problem, fully fixable.

### C.4 Shared / reused (legitimate)

362 assets in the dev DB are referenced by >1 owner document (a talent's global item + one or more submissions, same `public_id`). This is the intended model and must be preserved — any deletion path must be reference-aware (`core.is_media_asset_referenced` already exists for this; two admin delete paths bypass it).

---

## SECTION D — determinations against your A–L checklist

| | Question | Determination |
|---|---|---|
| **A** | What are the 8,430 derived objects | 8,457 enumerated. §B.1: 3,938 image `w_400` thumbnails · 3,144 full-res image AVIF re-encodes · 617 video posters · 579 video 720p transcodes · 148 download (`fl_attachment`) derivatives · 33 misc. `[MEASURED]` |
| **B** | Breakdown by folder / rtype / format / transformation / dims / date / size / public_id / asset_id | Captured in `derived_assets.json` (public_id, format, bytes, width, height, transformation, category per row) + `originals_full.json` (created_at, folder, version). Per-derived `created_at` and `asset_id` are **not returned** by `transformation(name)` — `[UNKNOWN from this API path; obtainable per-asset via `resource()` if required]` |
| **C** | How many are transcodes / posters / thumbnails / image transforms / delivery variants / legacy / unknown | video transcodes **579** (+65 download) · video posters **617** · image thumbnails **3,940** · image AVIF/format transforms **3,144** · download delivery variants **148** · unknown/misc **~30**. "Legacy" not separable by field — proxy: 3,003 of 3,144 AVIF derivatives have an orphaned parent. `[MEASURED]` |
| **D** | Trace 56.65K video transformations to code | Now measured as **23,228 video-seconds** (`sd_video_second` 21,472 + `hd` 591 + `fourk` 1,165) + the `transformation`/`avif` buckets. Code paths: eager `mp4` in `submission_sign_upload` / `admin_add_media_sign` / apply video-signature; `core.cloudinary_upload` eager (`core.py:750`); `core.upload_and_track_asset` re-upload chain (`core.py:956–1009`); `core.stream_video_url` recompute (`core.py:2836`); `links._get_video_download_url` + `ClientView.getVideoDownloadUrl` (`f_mp4` rewrite); `core.trigger_cloudinary_transcode` → `providers.CloudinaryProvider` (dormant — provider defaults to `stream`). `fourk_video_second: 1,165` proves 4K masters are being transcoded (admin `keep_original=True` path keeps the 4K and still generates the 720p). `[MEASURED + CONFIRMED]` |
| **E** | Which transformation strings generate the most usage | By derived count: `f_avif,q_auto/jpg` (2,675) · `w_400,c_fill,dpr_auto,f_auto,q_auto` (2,443) · `c_fill,dpr_auto,f_auto,q_auto,w_400` (1,468) · video `c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4` (206) · bare `f_mp4` (113, **1.5 GB**). Cloudinary Admin API does **not** expose per-string lifetime invocation counts — only `used: bool` + current derived list; exact per-string transformation *counts* need the console's Reports export. `[MEASURED for derived count; UNKNOWN for lifetime invocations]` |
| **F** | Same original → multiple variants? | **YES.** §B.2 — 12 strings for one 720p output, 7 for one poster, 4 for one thumbnail, 146 unique download strings. Avg 2 derived/original, up to 7. `[MEASURED]` |
| **G** | Repeated client payload generation creating transformations? | **YES.** §B.4 — `_public_media` recomputes posters per payload; legacy `stream_video_url` cold-transform per view; `IMAGE_URL` full-res AVIF per render. The `dpr_1.0` vs `dpr_auto` vs no-dpr poster string split is the fingerprint. `[CONFIRMED code + MEASURED fragmentation]` |
| **H** | How many of 8,457 derived are still referenced | dev DB: ~840. **Prod-DB rerun required for the real number.** `[dev-DB derived / needs prod]` |
| **I** | How many genuinely orphaned | dev DB: ~7,520 derived (~4.9 GB) + ~3,560 originals (~14.6 GB). **Prod-DB rerun required.** `[dev-DB derived / needs prod]` |
| **J** | How many legitimate shared/reused | dev DB: 362 originals referenced by >1 owner doc — legitimate copy-by-value. `[dev-DB derived / needs prod]` |
| **K** | Largest storage consumers | §C.1 — video originals 9.46 GB (63% of originals); top-7 folders all deleted projects (5.9 GB); 23 videos >100 MB = 4.18 GB. `[MEASURED]` |
| **L** | Is Cloudinary billing consistent with the app storage console | **NO — off by ~1000×.** `cloudinary_admin.compute_category_breakdown()` sums `submissions.media[].size` + `applications.media[].size` only → dev DB total **0.02 GB** vs Cloudinary **19.52 GB**. It never reads `talents.media[]`, never counts derived, never counts orphans, and `media[].size` is largely unpopulated for the direct-upload video path. The `/summary` card (live `usage()`) IS accurate; the category/project/talent breakdowns are not. `[MEASURED + CONFIRMED code]` |

---

## SECTION E — EXISTING DESTRUCTIVE CLEANUP: `POST /api/admin/cloudinary/health/cleanup`

### How it works today `[CONFIRMED — `cloudinary_admin.py:1051–1170`, `StorageDashboard.jsx:97–107,308–316`]`

1. **UI trigger:** a button labeled **"One-Click Repair & Cleanup"** in the admin Storage Dashboard. `onClick={handleOneClickCleanup}` → immediate `adminApi.post('/admin/cloudinary/health/cleanup')`. **No confirmation dialog.** Enabled whenever the last scan found `orphaned_count > 0 || broken_count > 0 || unused_count > 0`.
2. **Auth:** `require_role("admin")` — full admin only (not team). One admin, one click.
3. **What it does, with no dry-run, no batching, no per-asset review:**
   - **A. Orphans:** lists every physical Cloudinary resource (`resources()` image+video+raw), and for any `public_id` **not** in `{submissions.media, talents.media, asset_metadata, feedback-leaf}` → `cloudinary.uploader.destroy(pid, invalidate=True)`. **The GET `/health` reference set OMITS `applications.media`** (only the POST path was patched — `cloudinary_admin.py:1067` vs the GET at `:942`), so any asset referenced *only* by an application is flagged and destroyed.
   - **B. Broken refs:** for every `asset_metadata` row whose `public_id` isn't a live physical resource → `db.asset_metadata.delete_one` **+ `db.submissions.update_many({}, {"$pull": {"media": {"public_id": pid}}})` + `db.talents.update_many({}, {"$pull": …})`** — mutates historical submission/talent media arrays across the whole collection.
   - **C. Unused:** every `asset_metadata` row with `status/upload_status == "failed"` or `project_status == "purged"` → `cleanup_media_storage(...)` (real Stream/R2/Cloudinary delete).
4. **Guard that exists:** `assert_providers_healthy()` (503s if Cloudinary ping fails). That's the only safety.

### Why this is a P0 in the current environment

Given the measured account state, a scan **right now** would report on the order of **3,000+ "orphaned" Cloudinary originals + 7,000+ "orphaned" derived + broken refs**, and one click would call `destroy()` on all of them and `$pull` from every matching submission/talent. Because dev/staging shares this account, and because the `/health` reference set is built from **whichever MongoDB the backend is pointed at**, running this from a backend connected to the dev DB would treat **production-referenced assets as orphans**. The `applications.media` omission alone means live client-review assets are in scope.

### Protections required before this endpoint may exist in any form

1. **Immediate (P0, before Phase 1):** disable the path. Either remove the `handleOneClickCleanup` button + return `410 Gone` from the endpoint, or gate it behind an env flag defaulting off. *(This is a code change — flagged for your approval as the one exception to "no code changes"; say the word and it's a 2-line frontend + 3-line backend diff, no asset touched.)*
2. Reference set must union **all** of: `talents.media`, `submissions.media`, `applications.media`, `projects.materials`, `feedback`, `links` snapshots, `casting_pipeline`, plus every derived-URL field (`poster_url`, `thumbnail_url`). Keyed on unique tokens (UUID / `take_`/`adm_` ids), never on category-name leaves (`intro_video`, `portfolio_video`).
3. Must run against a **known, asserted database** (assert `DB_NAME`/env == production, or refuse).
4. **Report-only by default.** Output a downloadable manifest: per asset — `public_id`, `asset_id`, bytes, created_at, folder scheme, inferred owner, parent-doc alive?, reference count, "what created it" (from folder + `asset_metadata`), classification.
5. **No collection-wide `update_many({})`.** Broken-ref cleanup must target specific `(collection, doc_id, media_id)` tuples from the manifest.
6. **Batched execution** (≤200/batch), **explicit typed confirmation per category**, audit-log row per batch, and a re-check of `is_media_asset_referenced` immediately before each `destroy`.
7. **Retention window:** an asset must be orphaned for ≥ N days (config, default 30) before it's eligible — matches your project-deletion policy.
8. Age/size caps per run; a hard ceiling (e.g. refuse if manifest > 500 assets or > 5 GB without a second confirmation).

---

## SECTION F — RECOMMENDED FINAL ARCHITECTURE

Unchanged in shape from the pre-Phase-0 audit, now confirmed by measured data and adjusted to your four decisions.

### F.1 Ownership model

```
Talent (db.talents)
 └── GlobalMedia = db.talents.media[]        owner_type="talent", owner_id=talent_id
       intro_video · portfolio_images[] · indian_images[] · western_images[]
       Cloudinary: talentgram/talents/{talent_id}/{category}/{media_id}   (drop the name slug)

Project (db.projects)  — soft-deletable, retention-governed
 └── TalentSubmission (db.submissions)
       audition_media[] = category ∈ {take, take_1..3}
         owner_type="project_submission", owner_id=submission_id, project_id, talent_id
         Cloudinary: talentgram/projects/{project_id}/submissions/{submission_id}/{media_id}
       referenced_global_media[] = value-copies whose public_id ∈ db.talents.media[]
         owner_type stays "talent"; project never owns the Cloudinary object
```

Rules: global media exists once; projects reference (never re-upload); audition media is submission-owned and never syncs to global (`cat_mapping` guard already enforces this — keep it); historical submissions are immutable snapshots of their own `media[]`; every item carries an explicit ownership header.

### F.2 Media item schema (additive, backward-compatible)

Add to each `media[]` entry: `owner_type`, `owner_id`, `project_id|null`, `source_talent_media_id|null`, `format`, `version`, `poster_url` (videos, stored once), `thumb_url` (images, stored once). Keep `size` alongside a new `bytes`. Keep `public_id`, `url`, `resource_type`, `provider`, `stream_uid`.

### F.3 Cloudinary's role

Durable store + CDN delivery. **One** `f_auto,q_auto` image negotiation (capped, see policy). **One** stored thumbnail per image. **One** stored poster per video. Deliver video originals as-is. Enable **strict transformations mode** with a whitelist of the ~5 canonical strings so the fan-out (§B.2) and unbounded `fl_attachment` strings become structurally impossible.

### F.4 Deletion decision function (single, shared)

```
resolve_delete_action(media_item, context) -> "unlink_only" | "destroy":
  if media_item.owner_type == "talent" and context != "talent_hard_delete":
      return "unlink_only"                       # never destroy a global asset from a project op
  if is_media_asset_referenced(public_id, stream_uid):
      return "unlink_only"                       # still used by value somewhere
  return "destroy"
```
Every route calls this. No route deletes by folder prefix. No route re-implements the check.

---

## SECTION G — DELETION MODEL (your decisions 2 & 3)

### G.1 Project deletion → soft-delete + retention (Decision 2)

```
project.deleted_at            = <timestamp>          # soft-delete, project hidden everywhere
project.audition_retention_days = 30                 # config: 0=immediate, 30, 90, -1=forever
```
Flow:
1. Delete action sets `deleted_at`, removes the project + its submissions from all active UIs, **destroys nothing**.
2. A scheduled reaper (daily) finds projects where `deleted_at + audition_retention_days < now`.
3. For each, iterate the (retained) submissions' `audition_media[]`; for each item run `resolve_delete_action(item, "project_purge")`:
   - `owner_type == "project_submission"` **and** not referenced anywhere → `destroy` (Cloudinary + Stream + R2 + `asset_metadata` + the media entry), one audit-log row.
   - `owner_type == "talent"` (a value-copied global item) → `unlink_only`.
4. Global talent media is **structurally out of scope** — different folder, different `owner_type`, never enumerated by this path.
5. `audition_retention_days == -1` → step 2 never fires; assets retained until an explicit purge.
6. Reversible during the window: clearing `deleted_at` restores the project intact.

### G.2 Talent hard-delete → blocked when dependencies exist (Decision 3)

Normal operation = **archive** (`talent.archived_at`), which hides the talent but keeps every asset and reference.

Hard-delete pre-flight check — **refuse with an itemized reason if ANY of:**
- active (non-deleted) projects the talent has a submission in
- historical submissions (any status)
- application records
- client review links referencing the talent
- global media items (`db.talents.media[]` non-empty)
- any other collection referencing `talent_id` (`casting_pipeline`, `media_sends`, `whatsapp_*`, `link_actions`)

Only when **all** are clear: hard-delete proceeds, and even then destroys only Cloudinary assets whose `owner_type == "talent"` **and** `is_media_asset_referenced` is false. Never cascades to project audition media (different owner). Response on block: `409` with the list of blocking dependencies and their counts, plus "Archive instead?" affordance.

---

## SECTION H — TRANSFORMATION POLICY (your decision 1)

**Video — transcoding OFF by default:**
- Admin uploads → store the canonical original (`keep_original=True`), serve the original's `secure_url` as `media.url`.
- **No eager `mp4` derivative. No `w_400/720/480/1080` variants. No ABR ladder. No incoming `transformation` on takes.**
- A transcoded variant is created **only** when a specific, recorded trigger fires:
  - source container/codec is not browser-safe (e.g. ProRes/DNxHD, some `.mov`, `.avi`, `.mkv`) — detected from the upload response's `format`/`video.codec`, not from file size;
  - OR an explicit admin/product action requests a specific rendition.
- When triggered: **one** derivative, single canonical transformation string, stored on the item, never recomputed.
- Delivery URLs carry no transformation segment. A single `f_auto` *delivery* param (not a stored transform) is acceptable for container negotiation if needed.
- 4K transcoding (`fourk_video_second: 1,165`) stops entirely under this policy unless the codec gate trips.

**Video posters — only if the UI genuinely needs them:**
- Audit whether any client/portal surface actually renders a poster the browser can't derive from the video itself. Where `<video>` with a mid-frame preview suffices, **no poster is generated**.
- Where a poster is required (e.g. a grid card that must not load video bytes): generate **once** at upload, store `poster_url`, and **never** recompute in `_public_media`/`enrich_talent`/webhook. One canonical string.

**Images:**
- Keep **one** on-the-fly `f_auto,q_auto` full-res delivery (HEIC→web correctness — the one real requirement). Consider capping `f_auto` to WEBP (drop AVIF) or adding a `w_1600` ceiling to kill the `extra_avif_mp_encoding: 10,996` at full 4000px resolution.
- Keep **one** stored `w_400` thumbnail, single canonical string, **no `dpr_auto`**.
- Delete `media_url` presets `roster`/`detail`/`full`; collapse to the one `card` (w_400) preset + raw `url` for full size.

**Downloads:** serve `media.url` + `fl_attachment:{filename}` as a **delivery flag** (free, not a transform). Stop the `f_mp4` / strip-`f_` rewrites in `links.py` and `ClientView.jsx`.

**Enforcement:** enable Cloudinary **strict transformations** with a whitelist of the ~5 canonical strings once the code emits only those. Any un-whitelisted string then 400s instead of silently minting a derived asset.

---

## SECTION I — MIGRATION PLAN

| Phase | Action | Writes? | Gate |
|---|---|---|---|
| **P0 (this report)** | Read-only verification | none | ✅ done — awaiting approval |
| **P0.1** | Neutralize `health/cleanup` (button + endpoint) | code only, **no assets** | your explicit OK |
| **P0.2** | Rerun `p0_step10_join.py` against **production** `MONGO_URL` → final H/I/J numbers + orphan manifest | none (read-only prod) | prod DB access |
| **P1** | Additive schema migration: backfill `owner_type`/`owner_id`/`project_id`/`source_talent_media_id`, `poster_url`/`thumb_url` (compute once), `format`/`version`. Dry-run → JSON report → apply. Idempotent, batched. | Mongo only, additive | P0.2 + approval |
| **P2** | Ship §B.4 fixes: read stored `poster_url`/`thumb_url`, stop recompute in `_public_media`/`enrich_talent`/webhook. Delete legacy `stream_video_url` call after `url` backfill. | code | P1 |
| **P3** | Image preset collapse (§H): one `card` preset, drop `dpr_auto`, cap `f_auto` resolution / drop AVIF. Audit every `media_url` call site. | code | P1 |
| **P4** | Video policy (§H): stop default transcode, serve originals, collapse the re-upload chain, codec-gated transcode only. Flag `MEDIA_TRANSCODE_MODE=off\|codec_gated\|always`, default `codec_gated`. Staging soak 1 week. | code | P1, staging soak |
| **P5** | Deletion model (§G): `resolve_delete_action`, project soft-delete + `audition_retention_days` + reaper, talent hard-delete pre-flight. Fix `admin_remove_media_item` / `delete_submission` / `delete_project` to route through it. | code | P1 |
| **P6** | Storage console rebuild (§L fix): aggregate `talents.media` too; split Global vs Project audition; label live-Cloudinary vs Mongo-estimate; reconciliation delta; Health Scan = report-only with full reference set. | code | P1 |
| **P7** | Enable Cloudinary strict transformations + whitelist. | Cloudinary setting | P2–P4 soaked |
| **P8** | One-time orphan prune: from the P0.2 manifest, ≥30-day-orphaned, parent-deleted, not referenced, retired-transform. Report → per-category approval → ≤200/batch → audit log → 48h pause between batches → reference re-check before each destroy. Originals never touched in the same pass as a code change; derived self-heal on next request. | **destructive — Cloudinary** | everything above soaked ≥2 weeks + explicit approval |

**Existing assets:** never re-uploaded, never renamed. `public_id`s stay. Only metadata is normalized in place.

---

## SECTION J — PROJECTED REDUCTION IN TRANSFORMATION USAGE

Baseline: **70.94 transformation credits / cycle** (`sd_video_second` 21,472 + `avif` 10,996 + `transformation` 10,687 + `fourk` 1,165 + `hd` 591 units).

| Change | Targets | Est. transform-credit reduction | Confidence |
|---|---|---:|---|
| **§H video: stop default transcode** (codec-gated only) | `sd_video_second` 21,472 + `hd` 591 + `fourk` 1,165 | **−22K units (~−30 credits, ~−43%)** | High — no product requirement; most WhatsApp takes are already h264 |
| **§H images: drop AVIF / cap `f_auto` resolution** | `extra_avif_mp_encoding` 10,996 | **−11K units (~−15 credits, ~−21%)** | High — WEBP is universally supported; visual delta negligible |
| **§B.4: stop poster/`stream_video_url` recompute + fix fragmentation** | portion of `transformation` 10,687 + regeneration churn | **−4–6 credits** | Medium — depends on how much of the 10,687 is recompute vs first-gen |
| **§H downloads: `fl_attachment` as delivery flag, no `f_mp4`** | 146 unique download strings, 61 forcing re-transcode | **−1–3 credits + eliminates unbounded growth** | Medium — current absolute cost modest, but it scales with client activity |
| **§F.7 strict transformations whitelist** | prevents *future* fan-out | caps regrowth at ~0 | High |

**Scenario projection (transformation credits/cycle):**

| Reduction target | Transform credits | Total account credits (bw 26 + storage ~17) | vs 60 included |
|---|---:|---:|---:|
| Today | 70.94 | 115.34 | +55 over |
| **−50%** | ~35 | ~78 | +18 over |
| **−75%** | ~18 | ~61 | **≈ at cap** |
| **−90%** | ~7 | ~50 | **−10 under** |
| **−100% of *unnecessary*** (keeps ~3–4 cr: one poster where required, one image negotiation, one thumbnail) | ~3.6 | ~47 | **−13 under** |

**−75% is reached by the two high-confidence changes alone** (stop video transcode + drop AVIF ≈ −45 credits → transform credits ~26, hmm) — more precisely: video −30 + AVIF −15 = −45 → ~26 transform credits ≈ −63%. Adding the recompute/fragmentation fixes gets to −75%+. At that point the account is back **at or under the 60-credit included tier** with the entire Talentgram UX unchanged, and storage cleanup (P8, one-time ~14 GB / ~13 credits reclaim on the dev-DB estimate, prod TBD) plus the ban on dev writes to the prod account provide the headroom to scale to 1,000+ talents / 100+ projects.

**Storage & bandwidth (secondary, not the ask but move together):**
- Bandwidth (26.22 cr): stops shipping generated 720p files + stops 2160p/1440p/1080p master delivery on the paths that currently do → est. −15–25%.
- Storage (18.18 cr): P8 orphan prune is the lever (dev-DB estimate ~14.6 GB originals + ~4.9 GB derived orphaned; **prod number pending P0.2**). Stopping the 720p-per-video derivative and the re-upload chain slows growth.

---

## IMMEDIATE NEXT STEPS (need your approval)

1. **Approve P0.1** — neutralize `POST /admin/cloudinary/health/cleanup` + its button. This is the one code change I'm asking to make now, for safety. No assets touched.
2. **Provide read-only production `MONGO_URL`** (or run `scratchpad/p0_step10_join.py` against prod yourself) so H/I/J and the orphan manifest are real, not dev-DB estimates.
3. **Confirm** the transformation policy (§H), deletion model (§G), and phased plan (§I) — then I start P1 (additive, non-destructive schema backfill) and nothing else until you approve each subsequent phase.

**Stopping here. No Phase 1 work will begin without your approval.**
