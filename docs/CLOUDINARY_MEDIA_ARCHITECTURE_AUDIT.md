# Cloudinary / Media Architecture — Forensic Audit

**Status:** Audit only. No code written, modified, committed, or deployed. Awaiting architecture approval before any implementation.

**Method note (governs every claim):** each statement is tagged `[CONFIRMED]` (read directly in current `main` source, file:line cited), `[INFERENCE]` (derived by direct analogy to confirmed code), `[ASSUMPTION]` (a design choice this document proposes), `[MEASURED]` (a number you supplied from the Cloudinary console), `[ESTIMATE]` (a number this document derives from measured inputs + confirmed code behaviour — arithmetic shown), or `[UNKNOWN]` (not established — flagged, not guessed).

**Scope of the trace:** `backend/core.py`, `backend/providers.py`, `backend/routers/{submissions,applications,talents,projects,links,webhooks,portal,cloudinary_admin,feedback}.py`, `backend/services/media_assignment_worker.py`, `backend/scripts/{audit_media_storage,detect_orphans,cleanup_invalid_media}.py`, `frontend/src/lib/{api,mediaUtils,directVideoUpload,mediaShare}.js`, `frontend/src/context/UploadManagerContext.jsx`, `frontend/src/components/{HlsVideo,LazyVideoPlayer,MaterialModal,shared/MediaGrid}.jsx`, `frontend/src/pages-components/{ClientView,TalentEdit,StorageDashboard,SubmissionPage,SubmissionReviewCenter}.jsx`.

---

## SECTION 1 — EXECUTIVE SUMMARY

### Are we burning money on storage, bandwidth, or transformations?

**Transformations, by a clear margin — then bandwidth, then storage.** From the credit split you supplied `[MEASURED]`:

| Credit bucket | Credits | Share of 89.92 |
|---|---:|---:|
| **Transformations** | **51.32** | **57%** |
| Bandwidth | 21.03 | 23% |
| Storage | 17.56 | 20% |
| **Total** | **89.92 / 60 included** | **150% of plan** |

You are ~29.9 credits/month into paid overage, and transformations alone (51.32) would *by themselves* nearly blow the entire 60‑credit included allowance. Fix transformations and the account drops back under or near the included tier without touching a single stored byte.

### Why transformations are so high (70.82K/month, 56.65K of it video)

The application is configured as an **automatic transformation engine on every asset, at three separate stages**, exactly the pattern you suspected:

1. **Eager derivatives on every single upload.** Every image sign endpoint pins `eager = w_400,c_fill,dpr_auto,f_auto,q_auto` `[CONFIRMED: submissions.py:887, applications.py:832, submissions.py:3365]`. Every video sign endpoint pins `eager = w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4 | w_600,h_338,c_fill,q_auto,f_jpg` `[CONFIRMED: submissions.py:882, submissions.py:3363, applications.py:997]`. So **1 derived asset per image**, and **2 derived assets per video (a full 720p H.264 re-transcode + a poster JPG)** — generated whether or not anything ever requests them. `dpr_auto` in the eager string multiplies the image thumbnail across DPR 1/2/3 as they're first requested.

2. **A multi-step re-upload chain for some video paths.** `core.upload_and_track_asset()` for `keep_original=False` video uploads the original, requests an eager MP4 + JPG, then **re-uploads the eager MP4 as a second Cloudinary asset (`audition_web`)**, **re-uploads the JPG as a third asset (`thumbnail`)**, then destroys the temp original `[CONFIRMED: core.py:945–1009]`. That is up to 4 create operations and 2+ transformations for one logical video.

3. **On-the-fly delivery transforms that never match the eager string.** The frontend rewrites every Cloudinary image URL to insert `f_auto,q_auto` at *full resolution* — a different derivative from the `w_400` eager one `[CONFIRMED: frontend/src/lib/api.js:51–59]`. The backend has **five** distinct image presets (`roster` w_400, `thumb` w_200, `detail` w_1200, `full` w_1600, plus bare `f_auto,q_auto`) `[CONFIRMED: core.py:1214–1242]` and different call sites pick different ones for the same image (e.g. `add_media` stores a `roster`/w_400 thumb `[talents.py:1070]` but `admin_add_media_complete` stores a `thumb`/w_200 thumb `[submissions.py:3424]`). Each distinct transformation string Cloudinary sees for a public_id is a **new derived asset + a new billed transformation**.

4. **Video delivery / download re-transforms.** `stream_video_url()` builds a 3‑segment chained transform that Cloudinary treats as distinct from the single-segment eager string — the code comments confirm this caused *"a brand-new, never-before-requested on-demand transformation, cold, on the very first client view"* for every Cloudinary video `[CONFIRMED: core.py:2814–2835]`. The ZIP/download path strips `f_`/`sp_` and appends `f_mp4`, producing yet another transform string per download `[CONFIRMED: links.py:1516–1550, ClientView.jsx:344–390]`.

5. **Poster regeneration.** `_public_media()` computes `video_poster_url(public_id)` on **every** client payload build for **every** video, and `video_poster_url` uses `dpr:auto` `[CONFIRMED: core.py:2855, core.py:1201–1211]` — a poster derivative per video per DPR.

**The delivered-video resolution breakdown you supplied confirms the diagnosis.** 2,471s at 2160p + 178s at 1440p + 1,502s at 1080p `[MEASURED]` can only mean **untransformed original 4K/1080p masters are being delivered** (the app never *requests* anything above 720p), while 720p (28,921s) is the eager derivative and 480p (12,784s) is `q_auto` down-picking. So we are simultaneously (a) paying to generate 720p copies of everything and (b) still shipping the raw 4K masters on some paths.

### Can we get transformations close to zero — safely?

**Yes, to within ~90–95% of current volume**, without degrading the client experience, because almost none of these transformations are backed by a real product requirement:

- The business does not require universal 720p transcoding — an admin who downloads an audition take from WhatsApp is already handed a compressed, phone-recorded MP4.
- The business does not require an ABR ladder — there is no adaptive-streaming player in the stack for Cloudinary assets (`HlsVideo.jsx` only does HLS for Cloudflare Stream `.m3u8` `[CONFIRMED: HlsVideo.jsx:26]`).
- Images genuinely benefit from **one** `f_auto,q_auto` derivative (HEIC→web-safe, the one real requirement `[CONFIRMED: api.js:44–48]`) plus **one** small thumbnail. That is 2 transforms per image, cached forever, not 4–6.

The residual ~5–10% is the legitimate, cache-once work: one web-safe image derivative, one thumbnail, one poster frame per video. Everything above that line is removable.

---

## SECTION 2 — EXACT TRANSFORMATION SOURCES

Legend for "Necessary?": **A REQUIRED** · **B OPTIONAL** · **C UNNECESSARY** · **D DUPLICATED/BUG** · **E UNKNOWN — needs prod verification**

| # | Source | File / function | Transformation | Trigger (user flow) | Derived asset? | Est. usage / 30d | Class | Recommendation |
|---|---|---|---|---|---|---|---|---|
| 1 | Image eager on talent-facing submission upload | `submissions.py:887` `submission_sign_upload` | `eager=w_400,c_fill,dpr_auto,f_auto,q_auto` | Talent adds portfolio/look image on `/submit` | Yes — 1 (×DPR) | ~image uploads/mo; minor vs video | **B** | Keep ONE thumbnail, drop `dpr_auto` (pin `dpr_1.0` or omit), keep `f_auto,q_auto` |
| 2 | Image eager on `/apply` upload | `applications.py:832` `sign_application_upload` | same as #1 | Talent uploads look image during onboarding | Yes — 1 (×DPR) | low | **B** | same as #1 |
| 3 | Image eager on admin-added media | `submissions.py:3365` `admin_add_media_sign` | same as #1 | Admin adds an image to a submission in Review Center | Yes — 1 (×DPR) | low | **B** | same as #1 |
| 4 | Admin talent-library image eager | `core.py:767–779` `cloudinary_upload` (via `upload_and_track_asset`) | `eager=[w_400,c_fill,dpr_auto,fetch_format_auto,quality_auto]` sync | Admin uploads a global profile image on the Talent page | Yes — 1 (×DPR) | low | **B** | same as #1 |
| 5 | **Video eager: 720p H.264 re-transcode** | `core.py:749–766` `cloudinary_upload`; `submissions.py:882/3363`, `applications.py:997` sign strings | `eager` mp4 `w_1280,h_720,c_limit,q_auto,vc_auto` (+ jpg) | **Every** video upload (intro on `/submit`+`/apply`, every admin audition take) | Yes — 1 heavy video transform | **High — dominant cost.** ~1 per video upload + regen churn | **C** (for takes) / **B** (for intro if source is huge) | Stop transcoding by default. Deliver the uploaded MP4. Transcode only on an explicit, size/codec-gated condition |
| 6 | Video eager: poster JPG | same sign strings as #5; `core.py:758–765` | `eager` jpg `w_600,h_338,c_fill,q_auto` | Every video upload | Yes — 1 image transform | ~1 per video upload | **A** (one poster is legitimate) | Keep exactly one poster; store its URL; never recompute |
| 7 | Incoming `transformation` on talent audition takes | `submissions.py:884` | `transformation=w_1280,h_720,c_limit,q_auto,vc_auto` (mutates stored asset) | Talent uploads a `take` on `/submit` | Yes — replaces original | ~1 per take upload | **C** | Remove; store original bytes. (Also: business says takes are admin-only now — see §3) |
| 8 | Multi-step video re-upload chain | `core.py:945–1009` `upload_and_track_asset` (`keep_original=False`) | temp upload → eager mp4+jpg → **re-upload mp4** as `audition_web` → **re-upload jpg** as `thumbnail` → destroy temp | Any code path calling `upload_and_track_asset` with `keep_original=False` for video | Yes — 2 extra full assets + 2 transforms | E — depends how many prod uploads still hit this vs the direct-sign path | **D** | Collapse to a single upload; no re-upload of derivatives as new originals |
| 9 | Frontend full-res `f_auto,q_auto` rewrite | `frontend/src/lib/api.js:51–59` `IMAGE_URL` | inserts `f_auto,q_auto/` (no width) into every Cloudinary image URL | Every image render in ClientView, TalentEdit, MaterialModal, MediaGrid, PortalProfile | Yes — 1 per image per negotiated format | High on image side; ~1 per distinct image ever viewed (then cached) | **B** | Keep (HEIC correctness) but this should be the ONLY on-the-fly image transform |
| 10 | Backend 5-preset image URL builder | `core.py:1214–1242` `media_url` presets `roster/thumb/detail/full` | `w_400` / `w_200` / `w_1200` / `w_1600`, each `c_*,f_auto,q_auto` | Roster cards, pipeline cards, detail pages, lightbox, cover cache | Yes — up to 4 more derivatives per image | Medium — every preset ever hit = 1 transform | **C** (mostly) | Collapse to **one** `thumb` preset + rely on #9 for full size. Delete `roster/detail/full` |
| 11 | `stream_video_url()` legacy cold transform | `core.py:1138–1157`, called `core.py:2836` | 3-segment `w_1280,h_720,c_limit` / `q_auto,vc_auto` / `f_mp4` | Client view of a **legacy** Cloudinary video that has no stored `url` | Yes — new derived, cold | E — only legacy records; count unknown | **D** | Backfill `url` on legacy video records; delete this function after |
| 12 | Video download / ZIP transform rewrite | `links.py:1516–1550` `_get_video_download_url`; `ClientView.jsx:344–390` `getVideoDownloadUrl` | strips `f_`/`sp_`, appends `f_mp4`; forces `.mp4` extension | Client clicks Download, or "Download all as ZIP" | Yes — 1 per video per download (distinct string) | Medium — scales with client download activity | **C** | Serve the already-stored delivery URL for download; add only `fl_attachment` (a delivery flag, not a transform) |
| 13 | `video_poster_url` recompute on every payload | `core.py:2855` in `_public_media` (also `core.py:1585`, `talents.py:1071`, `webhooks.py:131/207`) | `w_600,h_338,c_fill,dpr_auto,q_auto` jpg | Every ClientView / roster / portal payload build for every video | Yes — poster per video per DPR (first hit) | Medium — bounded by #videos × DPRs | **D** | Compute once at upload, store `poster_url`, and **only** fall back to compute if missing |
| 14 | `sign_r2_media_if_needed` / cover cache | `core.py:1408`, `core.py:1600`, `talents.py:194` | `media_url(preset="roster")` = `w_400,c_fill,f_auto,q_auto` | Talent cover thumbnail denormalization | Yes — 1 per talent cover | ~1 per talent (cached in `cover_thumbnail_url`) | **B** | Fine as-is once #10 is collapsed to one preset that matches this |
| 15 | `reconcile_submission_videos` Admin API list | `submissions.py:1495–1519` | none (Admin API `resources()` call, not a transform) | Submission finalize | No | Admin API calls, not transforms — but counts toward rate limits | **B** | Leave; low volume |

### Where the 56.65K video transformations most plausibly originate `[ESTIMATE]`

The account holds ~4,311 originals `[MEASURED]`. 56.65K video transforms in 30 days against a mostly-static library means the number is **not** "one transcode per upload" — uploads/month are far lower than that. The multiplier is **derived-asset regeneration churn**:

- Cloudinary PAYG **auto-purges derived assets that go unaccessed**, then **re-bills the transformation** when they're next requested. With `q_auto`, `f_auto`, `dpr_auto`, and 4–6 distinct transformation strings per asset, each video/image has a *fan* of derivatives, each of which can be independently purged and regenerated.
- Every client-review link view of a legacy video that lacks a stored `url` triggers `stream_video_url()` cold (#11). Every download triggers #12. Every payload build recomputes posters (#13).
- Net effect: a single video can be re-charged for its 720p derivative and its poster **multiple times per month** as different clients view it, derivatives expire, and different transform strings are requested.

**This is verifiable in production** — see §10 for the exact Cloudinary Admin API queries (`derived` resource listing, `usage` history, transformation report) that will attribute the 56.65K precisely. Until then the attribution above is `[ESTIMATE]` / `[UNKNOWN]`, deliberately not asserted as fact.

---

## SECTION 3 — CURRENT MEDIA OWNERSHIP

### Collections & folder schemes `[CONFIRMED]`

| Concept | Mongo location | Cloudinary folder | Set by |
|---|---|---|---|
| Global talent media | `db.talents.media[]` | `talentgram/talents/{talent_id}_{slug}/{profile_images\|intro_video\|portfolio_videos}` | `upload_and_track_asset` (`core.py:885–895`), admin `POST /talents/{tid}/media` (`talents.py:1027`) |
| Talent-facing submission media | `db.submissions.media[]` | `talentgram/submissions/{sid}` | `submission_sign_upload` (`submissions.py:873`) |
| Admin-added submission media | `db.submissions.media[]` (`scope:"admin_added"`) | `talentgram/admin_media/{pid}/{sid}` | `admin_add_media_sign` (`submissions.py:3340`) |
| Per-submission audition folder (finalize reconcile / R2 path) | `db.submissions.media[]` | `talentgram/projects/{pid}/auditions/{talent_id}_{slug}/submission_{sid}` | `audition_submission_folder` (`core.py:1160`), `upload_and_track_asset` project branch (`core.py:888`) |
| Application media | `db.applications.media[]` | `talentgram/applications/{aid}` | `sign_application_upload` (`applications.py:829`) |
| Voice feedback | `db.feedback` + `db.asset_metadata` | Cloudinary (feedback router) | `routers/feedback.py` |
| Asset tracking rows | `db.asset_metadata` | — | `upload_and_track_asset`, webhook, various |

**There are three folder schemes for audition/project media** (`talentgram/submissions/…`, `talentgram/admin_media/…`, `talentgram/projects/…/auditions/…`) — this inconsistency is the direct cause of the deletion gaps in §4.

### What gets DUPLICATED vs REFERENCED

**Referenced (copy-by-value, same `public_id`, no new Cloudinary asset)** `[CONFIRMED]`:
- `build_prefill_media()` (`submissions.py:196`) turns `talent.media` into `prefill_media` carrying the **same `url`/`public_id`** — no re-upload.
- `submission_add_media_from_library` (`submissions.py:1139`) attaches a library item to a submission by value — same `public_id`.
- `sync_media_to_global_talent()` (`core.py:3437`) mirrors a submission's portfolio/intro item **into** `db.talents.media[]` by value — same `public_id`, new item `id`.
- Client review renders `submissions.media` live via `_submission_to_client_shape` — no copy.
- `is_media_asset_referenced()` (`core.py:1435`) + `safe_cleanup_media_storage()` (`core.py:1468`) exist specifically because the same `public_id` legitimately appears in `db.talents`, `db.submissions`, and `db.applications` at once.

**Duplicated (new Cloudinary asset created)** `[CONFIRMED]`:
- The video re-upload chain (`core.py:945–1009`) — `audition_web` and `thumbnail` are genuinely new uploaded resources.
- Legacy R2→Cloudinary transcode path (`providers.CloudinaryProvider`, `core.trigger_cloudinary_transcode`) — fetches R2 URL into a new Cloudinary asset. **Now dormant**: `get_video_provider()` defaults to `"stream"` `[CONFIRMED: providers.py:167]`.
- **No duplication when a talent is added to another project** — projects reference `talent.media` by value via prefill/from-library. This part of the current design is already correct.

### What gets synced to global vs stays project-scoped `[CONFIRMED: core.py:3393–3434]`

- **Synced to `db.talents.media`:** `image, portfolio, indian, western, video, intro_video, headshot(s), additional_portfolio` (+ Admin-Submission look categories).
- **Never synced (project-scoped forever):** `take, take_1, take_2, take_3` — `sync_media_to_global_talent` early-returns for any category not in `cat_mapping` `[CONFIRMED: core.py:3475–3477]`. Audition takes are structurally prevented from becoming global media. **This requirement is already met.**

### Submission snapshot behaviour `[CONFIRMED]`

- Replacing/deleting media on an **already-submitted** submission does **not** mutate the global profile — gated by `has_been_submitted_once(sub)` / `already_submitted` (`submissions.py:1106–1122`, `webhooks.py:166–182`).
- `sync_media_to_global_talent` only fires while the submission is still ORIGINAL.
- Client rendering is **always live** off `submissions.media` (the frozen `client_package_snapshot` is deprecated, `submissions.py:3507`). So a submission is a historical snapshot of *its own media array*, but the client view re-renders it live. Editing the talent's global profile does **not** rewrite historical `submissions.media[]` — those keep their own copied items. **This requirement is met**, with one nuance: because copies share `public_id`, physically deleting the underlying asset from one place *does* break the others unless the reference check runs (it does, on the talent-facing paths; it does **not** on two admin paths — §4).

---

## SECTION 4 — DELETION SAFETY AUDIT

### 1. Remove a talent from a project (delete one media item from a submission)

- **Talent-facing** `DELETE /public/submissions/{sid}/media/{mid}` (`submissions.py:1095`): `$pull` the item → `remove_synced_media_from_global_talent` **only if not already submitted** → `safe_cleanup_media_storage(target_media)` which runs `is_media_asset_referenced()` across `talents`+`submissions`+`applications` first `[CONFIRMED: submissions.py:1116–1130]`. **Safe.** Global asset survives because the talent's Library still references the same `public_id`.
- **Admin** `DELETE /projects/{pid}/submissions/{sid}/media/{media_id}` (`submissions.py:3470` `admin_remove_media_item`): **`$pull` only. No storage cleanup at all** `[CONFIRMED: submissions.py:3486]`. → **Orphans the Cloudinary asset** for admin-added audition takes (folder `talentgram/admin_media/…`). Never deletes global media (good), but leaves derived + original objects stranded.
- **Storage console** `DELETE /api/admin/cloudinary/projects/{pid}/talents/{tid}/auditions` etc. (`cloudinary_admin.py:851+`): calls `delete_one_media_item` which runs `count_other_references` (`cloudinary_admin.py:775`) before destroying — **reference-aware. Safe.**

### 2. Delete a project

`DELETE /projects/{pid}` (`projects.py:135`) and `/projects/bulk-delete` (`projects.py:94`):
```
db.projects.delete_one → db.submissions.delete_many({project_id}) → db.casting_pipeline.delete_many
→ db.asset_metadata.delete_many({project_id})
→ cloudinary.api.delete_resources_by_prefix("talentgram/projects/{pid}")
→ cloudinary.api.delete_folder("talentgram/projects/{pid}")
```
`[CONFIRMED: projects.py:141–159]`

- **Global talent media: NOT deleted.** It lives under `talentgram/talents/…`, outside the `talentgram/projects/{pid}` prefix. **Safe on that axis.**
- **BUG — orphans:** audition media uploaded through the two *other* folder schemes is **missed**:
  - `talentgram/submissions/{sid}` (talent-facing take uploads) — not under `projects/{pid}`.
  - `talentgram/admin_media/{pid}/{sid}` (admin audition takes — the current primary workflow) — **not under `projects/{pid}`** (`admin_media` ≠ `projects`).
  - Only assets under `talentgram/projects/{pid}/auditions/…` are actually purged. `[CONFIRMED by folder-string comparison: submissions.py:3340 vs projects.py:156]`
- **No reference check** on the prefix delete — but since global media is in a different prefix this is currently safe; it would become unsafe if any global asset were ever physically written under a project prefix (it is not, today).
- `db.submissions.delete_many` also removes the submission docs **without** calling `cleanup_media_storage` per media item — so any Stream video UIDs and R2 keys tied to those submissions are also orphaned (Stream/R2, not Cloudinary).

### 3. Delete a talent

`DELETE /api/admin/cloudinary/talents/{talent_id}` (`cloudinary_admin.py:1172` `delete_talent_assets`):
```
db.asset_metadata.delete_many({talent_id})
→ slug = re.sub(r'[^a-zA-Z0-9_]', '', talent_name.lower().replace(' ','_'))
→ cloudinary.api.delete_resources_by_prefix(f"talentgram/talents/{talent_id}_{slug}/")
→ cloudinary.api.delete_folder(...)
```
`[CONFIRMED: cloudinary_admin.py:1175–1182]`

- **BUG — slug mismatch:** this recomputes the folder slug with a *different* function (`re.sub(...lower().replace(' ','_'))`) than uploads use (`_slugify_deterministic`, `core.py:885`). If the two ever diverge (accented characters, punctuation, casing rules), `delete_resources_by_prefix` silently deletes **nothing** and every asset is orphaned. **Needs prod verification** `[E]`.
- **No reference check:** if a talent's global asset was copied-by-value into a *live* submission/application (it routinely is), destroying it here **breaks that submission's client link**. This is "delete the whole talent" so some breakage is expected, but there is no warning, no reference report, and no soft-delete.
- This endpoint does **not** delete the `db.talents` doc itself — it's an asset-only purge from the storage console. The talent row deletion path is elsewhere and does not cascade to Cloudinary at all `[UNKNOWN — not traced; flag for follow-up]`.

### 4. Talent replaces their introduction video

- On `/submit` / `/apply`: the sign endpoint `$pull`s the existing `intro_video` item first (`submissions.py:867–870`), new upload proceeds, and the webhook (`webhooks.py:151–182`) cleans up prior intro items via `safe_cleanup_media_storage` **and** only mirrors the removal to global if the submission is still ORIGINAL. **Safe & correct.**
- Admin `POST /talents/{tid}/media` with `category:"video"`: after the new upload succeeds, stale `video` items are removed via `delete_talent_media_item` → `safe_cleanup_media_storage` (reference-aware) `[CONFIRMED: talents.py:1080–1086]`. **Safe.**
- The old asset is physically destroyed **only if** no submission/application still references that `public_id`. A historical submission that copied the old intro keeps working. **Correct.**

### 5. Talent deletes an image

- `DELETE /talents/{tid}/media/{mid}` (admin) and `DELETE /portal/media/{mid}` (talent) both route to `core.delete_talent_media_item` (`core.py:1505`) → `$pull` → `safe_cleanup_media_storage` (reference-aware) → clear cover if it was the cover → `update_talent_cover_cache` `[CONFIRMED]`. **Safe.** Physical delete only when unreferenced anywhere.

### 6. Admin deletes an audition take

- Via **Review Center** `admin_remove_media_item` (`submissions.py:3470`): **`$pull` only — asset orphaned** (bug, item 1 above).
- Via **Storage Console** `delete_one_media_item` (`cloudinary_admin.py:802`): reference-checked destroy — safe, and correctly scoped (takes are project-only, never global).
- Because takes are structurally excluded from `sync_media_to_global_talent`, an audition-take delete can **never** touch global media regardless of path. **That requirement is met**; the only defect is orphaning on the Review Center path.

### Deletion audit summary

| Scenario | Global media safe? | Project media handled? | Orphan risk |
|---|---|---|---|
| Remove media from submission (talent path) | ✅ | ✅ reference-aware | none |
| Remove media from submission (admin Review Center) | ✅ | ❌ no cleanup | **orphan** |
| Delete project | ✅ | ⚠️ only `projects/{pid}/` prefix | **orphans `admin_media/` + `submissions/` + Stream/R2** |
| Delete talent (storage console) | ⚠️ breaks live refs, no warning | n/a | **orphan if slug mismatch** |
| Replace intro video (any path) | ✅ | ✅ | none |
| Delete talent image (any path) | ✅ | ✅ | none |
| Delete audition take (storage console) | ✅ | ✅ | none |
| Delete audition take (Review Center) | ✅ | ❌ no cleanup | **orphan** |

---

## SECTION 5 — TARGET ARCHITECTURE

### 5.1 Ownership model (authoritative)

```
Talent  (db.talents)
 └── GlobalMedia  =  db.talents.media[]          owner_type = "talent",  owner_id = talent_id
       ├── introduction_video   (category "video"/"intro_video", singleton)
       ├── portfolio_images[]   (category "portfolio"/"image")
       ├── indian_images[]      (category "indian")
       └── western_images[]     (category "western")
       Cloudinary folder:  talentgram/talents/{talent_id}/...          (drop the name slug — see 5.4)

Project  (db.projects)
 └── TalentSubmission  (db.submissions, one per talent per project)
       └── audition_media[]     =  items with category in {take, take_1..3}
             owner_type = "project_submission",  owner_id = submission_id,
             project_id = pid,  talent_id = tid
             Cloudinary folder:  talentgram/projects/{pid}/submissions/{sid}/...   (ONE scheme)

       └── referenced_global_media[]  =  copy-by-value items whose public_id also
             lives in db.talents.media[]  (prefill / from-library / admin select)
             owner_type = "talent"  (the PROJECT never owns these)
```

**Rules:**
1. **Global media exists once.** One Cloudinary asset per global item, under the talent folder. Never re-uploaded when a talent joins another project.
2. **Projects reference, never copy the asset.** The submission's `media[]` entry may be a value-copy of the *metadata* (url, public_id, dimensions) but carries `owner_type:"talent"` and `source_talent_media_id`. It never gets its own Cloudinary object.
3. **Audition media is owned by the submission.** `owner_type:"project_submission"`. One folder scheme. Structurally excluded from global sync (already true — keep the `cat_mapping` guard).
4. **Historical submissions are immutable snapshots of their own `media[]`.** Global profile edits only affect submissions created *after* the edit. No back-propagation.
5. **Every media item carries an explicit ownership header** (see 5.3) so every delete path can make a safe decision without guessing from folder strings.

### 5.2 Cloudinary's role (target)

| Cloudinary should be | Cloudinary should NOT be |
|---|---|
| Durable object store for originals | An auto-transcoder for every video |
| CDN / byte-range delivery for video | An ABR-ladder generator |
| **One** `f_auto,q_auto` negotiation per image (HEIC→web) | A 4–6-preset image resizer |
| **One** stored poster frame per video | A per-view / per-DPR poster regenerator |
| **One** stored small thumbnail per image | — |

Target transform budget per asset, generated once and stored, never recomputed:
- **Image:** 1 × `f_auto,q_auto` (full-res, on-the-fly, cached) + 1 × `w_400,f_auto,q_auto` thumbnail (eager). = 2, amortised to ~0/month after first view.
- **Video:** 1 × poster JPG (eager). Delivery = the stored MP4 as-is. = 1, amortised to ~0/month.
- **Video, only if `bytes > THRESHOLD` or codec ∉ {h264/h265/vp9}:** 1 × 1080p H.264 transcode. Gated, not default.

### 5.3 Media item schema (normalized — additive, backward-compatible)

```jsonc
{
  "id": "uuid",                        // unique per array entry (existing)
  "category": "portfolio|indian|western|video|take|take_1..3",  // existing
  "url": "https://res.cloudinary.com/...",   // canonical DELIVERY url (existing)
  "public_id": "talentgram/talents/{tid}/...",   // existing
  "asset_id": "cloudinary asset_id",   // existing, inconsistently populated → normalize
  "resource_type": "image|video",      // existing
  "format": "jpg|mp4|...",             // NEW-normalized (from upload response)
  "version": 1699999999,               // NEW-normalized (Cloudinary version int)
  "bytes": 123456,                     // existing as "size" → keep both during migration
  "width": 1080, "height": 1920, "duration": 12.4,   // existing where applicable

  // Ownership header (NEW — the core of deletion safety)
  "owner_type": "talent|project_submission",
  "owner_id": "talent_id | submission_id",
  "talent_id": "tid",                  // always set
  "project_id": "pid | null",          // set iff owner_type=project_submission
  "source_talent_media_id": "uuid|null",  // set iff this is a value-copy of a global item
  "provider": "cloudinary|stream",     // existing, normalize

  // Derived-asset pointers (NEW — so nothing is ever recomputed)
  "poster_url": "https://...jpg | null",   // videos only, stored once
  "thumb_url": "https://...w_400... | null" // images only, stored once
}
```

### 5.4 Folder scheme (single, canonical)

```
talentgram/talents/{talent_id}/{intro_video|portfolio|indian|western}/{media_id}
talentgram/projects/{project_id}/submissions/{submission_id}/{media_id}
```
Drop the `_{name_slug}` suffix entirely — it's a PII leak in the URL, it's the source of the delete-time slug-mismatch bug (§4.3), and it provides no addressing value (`talent_id` is already unique). Existing assets keep their current public_ids (migration §7 does not move them); only *new* uploads use the clean scheme, and every delete path keys off the stored `owner_type`/`public_id`, never a recomputed folder string.

### 5.5 Deletion decision function (single, shared)

```
def resolve_delete_action(media_item, context) -> Literal["unlink_only", "destroy"]:
    # context: which document is being edited (talent | submission | application | project)
    if media_item.owner_type == "talent" and context != "talent_hard_delete":
        return "unlink_only"                      # never destroy a global asset from a project op
    if await is_media_asset_referenced(public_id, stream_uid):
        return "unlink_only"                      # still used somewhere by value
    return "destroy"                              # safe: only this owner, no other refs
```
Every route (`admin_remove_media_item`, `delete_submission`, `delete_project`, `delete_talent_assets`, storage-console deletes) calls this — no route re-implements the check, no route deletes by folder prefix.

---

## SECTION 6 — CLOUDINARY OPTIMIZATION PLAN

Ordered by impact-per-risk. Each item is independently shippable.

### 6.1 Stop transcoding video by default *(largest single win — targets the 56.65K)*
- **Change:** remove the `mp4` entry from every eager sign string (`submissions.py:882/3363`, `applications.py:997`, `core.py:750–766`); remove the incoming `transformation` on takes (`submissions.py:884`). Keep the `jpg` poster eager.
- **Store & serve** `payload.url` (the uploaded original's `secure_url`) as `media.url`.
- **Gate transcoding** behind `if bytes > 200MB or source_codec not in SAFE_CODECS` — only then request a single 1080p H.264 eager. Most WhatsApp-sourced takes are already sub-50MB h264 and skip it entirely.
- **Risk:** a non-web-safe container (e.g. ProRes `.mov`) from an admin would fail to play. Mitigation: the codec/size gate above; plus a one-time `f_auto` delivery param on video URLs is cheap and handles most container issues without a stored transcode.
- **Expected:** video transformations drop ~85–95%. Delivered-video credit (bandwidth) also drops because you stop shipping 4K masters (see 6.4).

### 6.2 Collapse image transforms to exactly two
- Delete `media_url` presets `roster`, `detail`, `full`; keep one `thumb` (rename to `card`, `w_400,c_limit,f_auto,q_auto`, **no `dpr_auto`**).
- Keep `IMAGE_URL`'s single `f_auto,q_auto` full-res rewrite (`api.js`) — this becomes the only on-the-fly image transform.
- Audit call sites: every `media_url(..., preset="detail"/"full")` → use `IMAGE_URL`/raw `url`; every `preset="roster"` → the one `card` preset.
- Remove `dpr_auto` from the image eager string (`submissions.py:887` etc.) — pin nothing, let the browser downscale. This kills the ×2/×3 DPR fan-out.
- **Expected:** image transformations drop ~50–70%; the remainder is 1 thumbnail + 1 full-res per image, cached permanently.

### 6.3 Never recompute a derived URL that's already stored
- `_public_media` (`core.py:2855`): use `m.get("poster_url")` and only fall through to `video_poster_url()` when it's genuinely absent. Same for `enrich_talent` (`core.py:1585`).
- Backfill `poster_url` / `thumb_url` on all existing records (migration §7) so the fallback essentially never fires.
- Delete the `stream_video_url()` call at `core.py:2836` after legacy `url` backfill; delete the function.
- **Expected:** eliminates the per-view / per-payload poster regeneration churn (#13) and the legacy cold-transform (#11).

### 6.4 Serve originals for delivery *and* download
- Video delivery: `media.url` = stored original `secure_url`. No transform segment.
- Download/ZIP (`links.py:_get_video_download_url`, `ClientView.getVideoDownloadUrl`): return `media.url` with `fl_attachment:{filename}` appended — `fl_attachment` is a **delivery flag, not a transformation**, so it's free. Stop the `f_mp4` / strip-`f_` rewrites.
- Image download: same — `IMAGE_URL(m)` + `fl_attachment`, no width/crop.
- **Expected:** removes transform source #12; also cuts bandwidth by not regenerating full-size derivatives for download.

### 6.5 Collapse the video re-upload chain
- Rewrite `upload_and_track_asset` video branch (`core.py:945–1009`) to a **single** `cloudinary.uploader.upload` with an eager poster only; store `secure_url` directly. No `original_*` temp, no `audition_web` re-upload, no `thumbnail` re-upload, no `destroy`.
- **Expected:** removes 2 create-ops + 2 transforms per video that hits this path; also stops it from generating orphan-prone extra originals.

### 6.6 One-time derived-asset cleanup (after 6.1–6.4 are live and stable)
- Once the app stops *requesting* the wide transform fan, the stale derivatives stop being regenerated on their own. The remaining stale derived objects (from the old presets) can be pruned with Cloudinary's `delete_derived_resources` **scoped to specific transformation strings we know we retired** — never a blanket derived purge. Dry-run → report → batched delete → audit log (§7).
- **Do not** delete originals in this pass. Storage is only 20% of the bill and originals are the source of truth.

### 6.7 Bandwidth & storage (secondary)
- **Bandwidth:** biggest lever is 6.1+6.4 (stop shipping 4K). Secondary: add `q_auto:eco` to the image full-res delivery param; lazy-load client-review video (`LazyVideoPlayer` already does poster-first — verify it's used on every client surface).
- **Storage:** after 6.1, you stop storing a 720p copy of every video (saves ~1 derivative-size per video). After 6.5, you stop storing 2 extra copies per re-upload-chain video. Consider `keep_original=False` **only** for the gated large-transcode case in 6.1 (store the 1080p, drop the 4K) — but default is keep-original.

### Classification roll-up

| Class | Items | Action |
|---|---|---|
| **A REQUIRED** | #6 (one poster), #9 (one `f_auto` image), #14 (cover thumb) | Keep, store once |
| **B OPTIONAL** | #1–4 (image thumbnails — keep one, drop `dpr_auto`), #5 (video transcode — gate it), #15 | Keep gated/reduced |
| **C UNNECESSARY** | #5 (takes), #7, #10, #12 | Remove |
| **D DUPLICATED/BUG** | #8, #11, #13 | Remove / collapse |
| **E UNKNOWN** | attribution of the 56.65K; #8 prod frequency; #4.3 slug match | Verify in prod (§10) before final numbers |

---

## SECTION 7 — MIGRATION PLAN

**Principle: no re-upload of existing assets. Normalize metadata in place; retire transform strings; prune only provably-dead derivatives.**

### Phase M0 — Production verification (read-only, no changes)
1. `cloudinary.api.usage()` history + the **Transformation** report from the console → attribute the 56.65K (which transformation strings, which folders).
2. `cloudinary.api.resources(..., type="derived")` inventory → count derived objects per transformation string; confirm the 8,430 figure and its composition.
3. Count `db.submissions.media[]` / `db.applications.media[]` items still lacking a stored `url` (drives #11 legacy path).
4. Verify the `delete_talent_assets` slug function against 20 real talent folders (`_slugify_deterministic` vs the `re.sub` version).
5. Snapshot: total originals, derived, storage GB, 30-day credit split. This is the before-baseline for §10.

### Phase M1 — Additive schema normalization (no behaviour change)
- Migration script over `db.talents`, `db.submissions`, `db.applications`:
  - Backfill `owner_type`, `owner_id`, `project_id`, `source_talent_media_id` from existing `scope`/`talent_id`/`project_id`/`category` + folder-string inference (folder strings are reliable enough for a *read*, just not for a *delete*).
  - Backfill `poster_url` for every video item: prefer existing `poster_url`/`thumbnail_url`, else compute `video_poster_url(public_id)` **once** and store it.
  - Backfill `thumb_url` for every image item: `media_url(public_id, "card")` once, stored.
  - Backfill `format`/`version` from a batched `cloudinary.api.resources` lookup by public_id (Admin API, read-only, ~1 call/100 assets).
- Dry-run mode: writes a JSON report of every intended change, no DB writes. Same pattern as `backend/migrations/reports/talent_dedup_dryrun_*.json`.
- Idempotent, resumable, batched (500 docs/batch).

### Phase M2 — App changes behind the stored fields (Section 6.1–6.5)
- Ship 6.3 first (read stored `poster_url`/`thumb_url`) — pure win, zero risk, immediately cuts recompute churn.
- Ship 6.2 (image preset collapse) — verify every call site in one PR.
- Ship 6.1 + 6.4 + 6.5 (video) together behind a flag `MEDIA_TRANSCODE_MODE = "gated" | "always"`; default `"gated"` in staging, soak 1 week, then prod.
- Legacy `url` backfill for videos → then delete `stream_video_url` call + function (6.3 tail).

### Phase M3 — Deletion-path hardening (Section 5.5)
- Introduce `resolve_delete_action()` shared helper.
- Fix `admin_remove_media_item` and `delete_submission` to call `cleanup_media_storage` per item via the helper.
- Fix `delete_project` to enumerate `db.submissions.media[]` for the project and delete each asset via the helper (keying off `public_id`/`owner_type`, not folder prefix); keep the prefix delete as a secondary sweep.
- Fix `delete_talent_assets`: enumerate `db.talents.media[]` + all referencing submissions, warn on live references, delete via helper.
- New: on project delete, an explicit **retention policy** field (`project.audition_retention = "delete" | "keep"`, default `"delete"`) consulted by the helper.

### Phase M4 — Derived-asset prune (Section 6.6, one-time, gated on M2 stability)
- Only after M2 has soaked ≥2 weeks and the transformation-per-day metric has dropped and flattened.
- `Storage Health Scan` (Section 8) produces the candidate list: derived objects whose transformation string is in the *retired* set AND whose parent original is still referenced.
- Report → human approval → batched `delete_derived_resources` (200/batch) → audit log row per batch → 48h pause between batches to watch for client-link breakage.
- Reference protection: never delete a derived object whose exact URL appears in any `db.*.media[].url|poster_url|thumb_url`.
- Rollback: derived assets regenerate on next request from the original — so a wrongly-pruned derivative self-heals at the cost of one transform. Originals are never touched in this phase, so there is no destructive rollback scenario.

### Backward compatibility requirements (must hold throughout)
- Every existing client-review link (`/public/links/{slug}`) keeps rendering — `_submission_to_client_shape` reads `media[]` live; as long as `url` stays valid, links don't break.
- Existing `public_id`s are never renamed or moved.
- `media.size` stays populated (storage console reads it) — keep it alongside the new `bytes`.
- `thumbnail_url` / `poster_url` field names stay (frontend `mediaUtils.js` reads them).
- `/submit` and `/apply` URLs and payload shapes unchanged.

---

## SECTION 8 — IMPLEMENTATION PLAN (phased, safe)

| Phase | Deliverable | Risk | Depends on | Rollback |
|---|---|---|---|---|
| **P0** | M0 prod verification report (this doc's `[UNKNOWN]`s resolved) | none (read-only) | — | n/a |
| **P1** | M1 additive migration (dry-run → apply); `owner_type` + `poster_url`/`thumb_url` backfilled | low | P0 | re-run with corrected mapping; fields are additive |
| **P2** | 6.3 — read stored derived URLs, stop recompute; delete `_public_media` recompute branch | low | P1 | revert commit |
| **P3** | 6.2 — collapse image presets to one; drop `dpr_auto` | medium (many call sites) | P1 | revert commit; derived assets already exist |
| **P4** | 6.1 + 6.4 + 6.5 — video: stop default transcode, serve originals, collapse re-upload chain. Flag-gated. | medium (playback) | P1, staging soak | flip `MEDIA_TRANSCODE_MODE=always` |
| **P5** | Legacy video `url` backfill; delete `stream_video_url` | low | P4 | revert; re-add function |
| **P6** | M3 — deletion-path hardening + `resolve_delete_action` + retention policy | medium (destructive code) | P1 | feature-flag the new cleanup calls; default off for 1 release |
| **P7** | Storage console rebuild (Section 8 detail below) | low (read-mostly) | P1 | revert UI |
| **P8** | M4 — one-time derived prune (report → approve → batched) | **high (destructive)** | P4+P7 soaked ≥2wk | derived self-heal on next request |

### Storage Console rebuild (P7)

**Current state** `[CONFIRMED]`:
- `/summary` (`cloudinary_admin.py:244`): **live from Cloudinary** `cloudinary.api.usage()` — storage bytes, object count, bandwidth, `derived_resources`, credit usage/limit/percent. Accurate.
- `/analytics` category breakdown, per-project, per-talent totals (`cloudinary_admin.py:185–460`): **computed from Mongo** `submissions.media[].size` + `applications.media[].size`. **Does NOT read `db.talents.media[]`** → global-profile media uploaded directly (admin Talent page) is **invisible** in the category breakdown and talent-storage totals.
- `classify_media_item` buckets `take*` → "Audition Videos", `intro_video` → "Introduction Videos", `indian`/`western` → look buckets, else "Portfolio". `scope:"admin_added"` → "Admin Uploads" (overrides category).
- `/health` orphan scan: lists **all** Cloudinary physical resources, flags any `public_id` not in `{submissions.media, talents.media, asset_metadata, feedback-leaf}` — **omits `applications.media`** (the POST `/health/cleanup` was patched to include it, `cloudinary_admin.py:1067`, but the GET `/health` was not, `cloudinary_admin.py:942`). → **false-positive orphans**.
- `/health` "duplicate media": flags any `public_id` referenced by >1 doc — i.e. it flags **every intentional copy-by-value reuse** (global media used across projects) as a "duplicate" problem. **Misleading.**
- **`POST /health/cleanup` (`cloudinary_admin.py:1051`): one-click, no dry-run, no per-asset confirmation, no batching.** It `cloudinary.uploader.destroy()`s every "orphan", `$pull`s "broken" public_ids from **all** submissions and talents (`update_many({})`), and runs `cleanup_media_storage` on every "failed"/"purged" metadata row. This is the single most dangerous endpoint in the system and directly violates your "never mass-delete" rule.

**Rebuild:**
1. Category breakdown must aggregate **`db.talents.media[]` too**, and must split **`owner_type:"talent"` (Global Talent Media)** vs **`owner_type:"project_submission"` (Project Audition Media)** as top-level groupings, with ownership shown per row.
2. `/summary` cards: keep live Cloudinary numbers, label them "Cloudinary account (live)"; label Mongo-derived numbers "Talentgram references (estimated from upload size)". Never present a Mongo estimate as the Cloudinary truth.
3. Storage-by-category: reconcile Mongo sum vs Cloudinary `storage` — show the delta as "untracked / derived / orphaned (run Health Scan)".
4. **The UI must not imply a project delete removes global media.** Add an explicit line on the project-delete confirm: "Audition media for N submissions will be deleted. Global profile media for these talents is NOT affected." (Backed by the M3 helper actually doing that.)
5. **Health Scan = report only.** Include `applications.media` in the reference set. Reclassify "duplicate" as "shared (expected)" vs "true duplicate = same bytes, different public_id". Detect: orphaned Cloudinary/Stream/R2 objects, broken DB refs (url 404s), true duplicates, retired-transform derived assets, project media with no project, talent media with no talent, metadata inconsistencies (missing `format`/`version`/`owner_type`).
6. **Delete `POST /health/cleanup` in its current form.** Replace with: scan → downloadable report → per-category human approval → batched execution (≤200/batch) → audit-log row per batch → reference-protection re-check immediately before each destroy.

---

## SECTION 9 — TEST PLAN

Existing coverage `[CONFIRMED]`: `backend/tests/test_cloudinary_migration.py`, `test_media_assignment.py`, `test_media_optional.py`, `test_p0_storage_hardening.py`, `test_storage_console_rebuild.py`, `test_upload_lifecycle.py`, `test_direct_uploads.py`, `test_file_signature_validation.py`. Extend, don't fork.

| Area | Test | Assert |
|---|---|---|
| **Global media reuse** | Add talent to Project A then Project B via prefill/from-library | Same `public_id` in both submissions; **zero** new `cloudinary.uploader.upload` calls on the second add (mock, assert call count) |
| | Talent has intro + 3 portfolio; joins 5 projects | Cloudinary originals count unchanged; `db.talents.media` unchanged |
| **Project media isolation** | Upload `take_1` to Project A submission for Talent X | Item has `owner_type:"project_submission"`; **not** present in `db.talents.media` after finalize; not in Project B's prefill |
| | `sync_media_to_global_talent` called with a `take` category | No-op (early return); `db.talents` untouched |
| **Deletion safety** | `admin_remove_media_item` on an admin audition take | `resolve_delete_action` → destroy; Cloudinary `destroy` called once; asset_metadata row gone; **no** orphan |
| | `admin_remove_media_item` on a from-library (value-copy) item | → `unlink_only`; Cloudinary `destroy` **not** called; talent Library item intact |
| | `delete_project` with 3 submissions, mixed admin_media + submissions folders | Every audition asset across **all three** folder schemes destroyed; every talent's global media count unchanged |
| | `delete_project` respects `audition_retention:"keep"` | No asset destroyed; submissions removed |
| | `delete_talent_assets` when talent's intro is value-copied into a live submission | Warning surfaced; (policy TBD: block vs cascade); if cascade, submission `url` marked broken not silently dead |
| | Replace intro video on already-submitted submission | Global profile intro **unchanged**; old submission asset cleaned only if unreferenced |
| **Client review** | Generate link, load `/public/links/{slug}` for a submission with intro + 2 takes + 4 images | Response `url`s are stored delivery URLs; **no** `cloudinary.utils.cloudinary_url` call with a new transform string during the request (spy) |
| | Download-all ZIP for a talent | Each fetched URL == stored `media.url` + `fl_attachment` only; no `f_mp4`/`w_` rewrite |
| | Legacy video record with no stored `url` | Backfill migration populates `url`; client load does **not** call `stream_video_url` |
| **Talent profile update** | Admin uploads new portfolio image | Exactly 1 upload call, 1 eager thumbnail; `thumb_url` stored; no `detail`/`full` derivative generated |
| **No unnecessary transforms** | Upload a 30MB h264 MP4 audition take (gated mode) | **No** mp4 eager requested; only jpg poster eager; stored `url` == uploaded `secure_url` |
| | Upload a 400MB / ProRes video | 1080p h264 transcode **is** requested (gate triggered) |
| | Render same image in roster + detail + lightbox | All three resolve to ≤2 distinct Cloudinary URLs (thumb + full) |
| **Existing assets** | Run M1 migration on a fixture DB dump | Idempotent (2nd run = 0 changes); every media item gains `owner_type`; no `url`/`public_id` mutated |
| **Historical submission snapshots** | Edit talent global bio+media, then load a 6-month-old submission's client shape | Old submission renders its own copied media; new values not back-propagated |
| **Orphan detection** | Seed 1 true orphan + 1 shared-by-value asset + 1 broken ref | Scan reports orphan=1, shared classified as "expected" not "duplicate", broken=1; **cleanup endpoint makes zero deletions without explicit approval token** |
| **Storage console** | `/analytics` with media only in `db.talents.media` | Appears in "Global Talent Media" category with non-zero size |

---

## SECTION 10 — COST MODEL

### Measured baseline (your inputs, 30 days) `[MEASURED]`

| | Value |
|---|---|
| Plan | Cloudinary Small PAYG, $29/mo, 60 credits included |
| Credits consumed | ~89.92 / 60 (≈150%) |
| — Transformations | 51.32 credits (57%) |
| — Bandwidth | 21.03 credits (23%) |
| — Storage | 17.56 credits (20%) |
| Transformations (count) | 70.82K total — 56.65K video, 14.17K image |
| Storage | 18.07 GB · 4,311 originals · 8,430 derived · 12,741 objects |
| Bandwidth | 34.75 GB |
| Delivered video | 45,856 s (720p 28,921 · 480p 12,784 · 1080p 1,502 · 2160p 2,471 · 1440p 178) |

**Credit→unit ratios implied by your data** `[ESTIMATE — arithmetic from measured values]`:
- Storage: 17.56 cr ÷ 18.07 GB ≈ **0.97 cr/GB** (≈ Cloudinary's standard 1 credit = 1 GB stored).
- Transformations: 51.32 cr ÷ 70.82K ≈ **0.72 cr per 1,000 transforms** (≈ standard 1 credit = 1,000 transforms, with video transforms weighted slightly higher).
- Bandwidth: 21.03 cr ÷ 34.75 GB ≈ **0.61 cr/GB** — lower than the 1:1 standard; the difference is not explained by the data provided, so bandwidth projections below are conservative (held flat unless a change directly reduces bytes delivered). `[UNKNOWN — confirm via console]`

> The exact dollar value of each credit **beyond the 60 included** is not in the data you provided and is not hard-coded anywhere in the codebase (`cloudinary_admin.py:269–275` explicitly notes PAYG has no fixed GB cap and surfaces only raw credit numbers). Projections below are in **credits**; convert using your actual invoice's overage rate.

### Projection — reducing *unnecessary* transformations

"Unnecessary" here = classes **C** + **D** + the ungated portion of **B** from §2. It does **not** include the ~1 poster + ~1 image negotiation + ~1 thumbnail that stay. Based on the code, the removable share of the 51.32 transformation credits is high because video transcoding (the dominant driver) has no product requirement for takes and intros are usually already web-safe.

| Scenario | Transform credits | Δ transform | Bandwidth credits | Storage credits | **Total credits** | vs 60 included |
|---|---:|---:|---:|---:|---:|---:|
| **Today (measured)** | 51.32 | — | 21.03 | 17.56 | **89.92** | +29.92 over |
| **−50% unnecessary transforms** | ~27.7 | −23.6 | ~19.0¹ | ~17.3² | **~64.0** | +4.0 over |
| **−75%** | ~15.8 | −35.5 | ~17.5¹ | ~17.0² | **~52.3** | **−7.7 under** |
| **−90%** | ~8.7 | −42.6 | ~16.5¹ | ~16.7² | **~41.9** | −18.1 under |
| **−100% of *unnecessary*** (keeps required minimum ≈ 3.6 cr) | ~3.6 | −47.7 | ~16.0¹ | ~16.5² | **~36.1** | −23.9 under |

¹ Bandwidth also falls once video transcoding stops shipping generated 720p files *and* the 4K-master delivery paths are removed (§6.1/6.4). Modelled conservatively: −10% at −50%, up to −24% (removing the 2160p/1440p/1080p delivered-video segments ≈ 4,151 s of the 45,856 s, plus lower per-stream bytes) at −100%. `[ESTIMATE]`
² Storage falls slightly: you stop storing a 720p derivative per new video and stop the re-upload-chain's 2 extra copies. Existing derived objects shrink further after the §6.6 prune (not modelled here — that's an additional one-time ~1–3 GB reclaim against the 8,430 derived). `[ESTIMATE]`

### Bottom line

- **At −75% unnecessary transformations the account returns under the 60 included credits** (~52 cr) — i.e. **plausibly back to the $29 flat fee with zero overage**, with the entire existing UX intact.
- **At −90–100%** you have ~18–24 credits of headroom — room to scale to 1,000+ talents / 100+ projects before re-approaching the cap, since new-talent load adds mostly *storage* (cheap, ~1 cr/GB) and *one-time* transforms (poster + thumbnail), not recurring transform churn.
- The single highest-leverage change is **§6.1 (stop default video transcoding)** — it alone targets ~55K of the 70.8K transforms.

### Answers to the 27 audit questions (index)

1. Upload endpoints — `submissions.py:818` (`submission_sign_upload`), `submissions.py:933` (`submission_complete_upload`), `submissions.py:3313/3392` (admin-media-v2 sign/complete), `submissions.py` admin_add_media (multipart, pdf/raw), `applications.py:798/863` (apply sign/complete), `applications.py:990+` (apply video-signature), `talents.py:1027` (`add_media`), `feedback.py` (voice notes), legacy `core.cloudinary_upload`/`upload_and_track_asset`. 2. Upload config — `core.py:64–69` (`cloudinary.config`), `core.py:718–779` (`cloudinary_upload` kwargs), each sign endpoint's `params`. 3. Transforms at upload — the `eager`/`transformation` strings tabulated in §2 (#1–8). 4. Eager transforms — §2 #1–6, #8; `core.py:738/750/770`, all sign strings. 5. Incoming transformation params — `submissions.py:884` (takes), `core.py:732` (`keep_original=False` large video). 6. URL builders — `core.py:1120` `cloudinary_url_for`, `:1138` `stream_video_url`, `:1185` `video_poster_url`, `:1214` `media_url`, `api.js:51` `IMAGE_URL`. 7. Frontend transformed-URL creators — `api.js:IMAGE_URL`, `ClientView.jsx:344` `getVideoDownloadUrl`, `ClientView.jsx:2652` download flag, `mediaUtils.js` (passthrough only). 8. Backend transformed-URL creators — all of `core.py` builders above + `webhooks.py:131/207` + `links.py:1516`. 9. Thumbnail generation — image eager `w_400` (§2 #1–4); `media_url` presets; `video_poster_url`. 10. Video transcoding — §2 #5, #7, #8; `providers.CloudinaryProvider` (dormant). 11. Image resizing — `media_url` presets, image eager, `IMAGE_URL` (format only, no resize). 12. `q_auto`/`f_auto`/`w`/`h`/`c`/`quality` — every location in §2; `f_auto,q_auto` also `api.js:57`. 13. Single original → multiple derived — image eager + `IMAGE_URL` + 4 `media_url` presets (up to 6/image); video eager mp4+jpg + `stream_video_url` + `video_poster_url` + download rewrite; the re-upload chain. 14. Same asset in multiple resolutions — the 5 image presets; `dpr_auto` ×DPR; video 720p vs original 4K. 15. Client-review transforms — `_public_media` poster recompute (§2 #13), `stream_video_url` cold (#11), ZIP download rewrite (#12). 16. Repeated-request components — `ClientView` portfolio `IMAGE_URL(images[idx])`, cover cards `thumbnailUrl(cover)` (falls to full `url` — bandwidth), `MediaGrid`, `LazyVideoPlayer` (poster-first, good). 17. Background jobs generating derived — `media_assignment_worker.py`, `reconcile_submission_videos` (Admin API list, no transform), webhook eager callback. 18. Cleanup/deletion jobs — `core.cleanup_media_storage`, `safe_cleanup_media_storage`, `delete_talent_media_item`; `scripts/{cleanup_invalid_media,detect_orphans}.py`; `cloudinary_admin.py` health cleanup. 19. Cloudinary deletion API calls — `core.py:1111` (`cloudinary_destroy`), `core.py:1009/4436`, `projects.py:115/156` (`delete_resources_by_prefix`+`delete_folder`), `cloudinary_admin.py:1120/1179`, `cloudinary_admin.py:830` area. 20. How public_id/asset_id/secure_url/resource_type/format/version stored — `db.*.media[]`: `public_id`, `url`(=secure_url), `resource_type`, `size`; `asset_id` inconsistent (often == public_id, `core.py:1045`); **`format` and `version` are NOT reliably stored** — normalize in M1. `db.asset_metadata`: `public_id`, `asset_id`, `secure_url`, `asset_url`, `resource_type`, `folder_path`, `file_size`. 21. Canonical vs duplicated refs — canonical by value; §3. 22. Global media physically duplicated when attached to projects — **No** (prefill/from-library copy by reference). 23. Project media stored against talent globally — **No** for takes (excluded from `cat_mapping`); intro/portfolio uploaded on `/submit` **do** sync to global by design. 24. Deleting project/talent deletes global assets — project: **No** (different prefix), but orphans other-scheme audition media; talent-asset purge: can break live value-copies, no guard. 25. Deleting global talent deletes project-specific assets — talent-asset purge deletes by `talent_id` metadata + folder prefix; project takes live under `projects/`/`admin_media/` folders keyed by pid, so **not** deleted by the talent-folder prefix; `asset_metadata.delete_many({talent_id})` could remove tracking rows for shared items though. 26. Orphaned objects possible — **Yes**: Review Center media removal, `delete_submission`, `delete_project` (non-`projects/` folders), slug-mismatch talent delete, re-upload-chain failures. 27. Are the 8,430 derived still needed — **Mostly no**: they are the fan-out of retired/duplicate transform strings (§1, §6.6). Exact composition is `[UNKNOWN]` pending the M0 `type="derived"` inventory; prune only in P8 with reference protection.

---

## APPENDIX — the smallest safe permanent fix

If you want the minimum change that solves the bill permanently:

1. **Delete the `mp4` eager from all video sign strings + the takes `transformation`.** Serve the uploaded MP4. Keep the jpg poster eager. *(kills ~55K transforms)*
2. **Read stored `poster_url`/`thumb_url`; never recompute.** Backfill once. *(kills the monthly regeneration churn)*
3. **Collapse image `media_url` to one `w_400` preset; drop `dpr_auto`; keep `IMAGE_URL`'s single full-res `f_auto,q_auto`.** *(kills ~half the image transforms)*
4. **Add `owner_type` to media items; route every delete through one reference-aware helper; fix the 3 orphan-leaking delete paths.** *(stops new orphans; makes the console honest)*

Items 1–3 are ~1 week of work + a staging soak and get you under 60 credits. Item 4 is the durability piece. No re-upload of any existing asset. No mass deletion. Nothing about the client experience changes.
