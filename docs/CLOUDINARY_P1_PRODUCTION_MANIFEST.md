# Cloudinary P1 — Production Cross-Join & Dry-Run Manifest

**Status:** Read-only verification complete. **Zero writes.** Nothing deleted, modified, migrated, regenerated, or re-uploaded. No cleanup executed. This is a dry-run manifest for your review. **Stopping here per your stop condition — no P3+ work until you approve.**

**Also done in this step (the one approved code change):** `POST /api/admin/cloudinary/health/cleanup` disabled → returns HTTP 410, performs no reads/deletes/mutations; the "One-Click Repair & Cleanup" button removed from the Storage Dashboard; test `backend/tests/test_health_cleanup_disabled.py` added and passing (2 tests). Details in §7.

---

## 1. How the production data was obtained (no secrets exposed)

The dev environment has the Railway CLI linked to **project `pacific-art` → service `talentgram-railway` → environment `production`** (`https://api.talentgramagency.com`, repo `rajbhansali92/talentgram-frontend`). The cross-join ran as:

```
railway run -- python3 p1_prod_manifest_v2.py
```

`railway run` injects the production env vars (`MONGO_URL`, `DB_NAME`, `CLOUDINARY_*`) into the subprocess — **the connection string is never printed, logged, or written to any file.**

**Read-only guarantees in the script:**
- The pymongo client is wrapped: every write method (`insert_*`, `update_*`, `delete_*`, `bulk_write`, `drop*`, `create_index*`, `find_one_and_*`, `rename`, …) **raises `RuntimeError` on call**. Only `find` / `aggregate` / `count_documents` / `list_collection_names` are used.
- `readPreference=secondaryPreferred` (reads off a replica).
- Cloudinary is contacted only for `api.usage()` (one read). The 4,324-original / 8,457-derived inventory is **reused from the Phase-0 read-only pull** (same account, verified still current: `usage()` now reports resources=4,324, derived=8,457).
- All output written to the local scratchpad only.

Evidence: `scratchpad/p1_out/prod_manifest.jsonl` (12,781 rows), `prod_manifest.csv`, `prod_summary.json`.

---

## 2. Production reality vs the dev-DB Phase-0 estimate

The Phase-0 orphan estimate (~14.6 GB) was against the **local dev MongoDB** (`mongodb://localhost:27017`), which holds mostly test churn and 4 test users. **Production is materially different** and the real orphan footprint is far smaller.

| | Dev DB (Phase 0) | **Production (P1)** |
|---|---:|---:|
| talents | 838 (test) | **1,264** |
| projects (live) | 864 (test churn) | **64** |
| submissions (live) | 1,017 | **274** |
| applications (live) | 150 | **82** |
| media items | 1,277 (sub-KB test files) | **4,455** (real content) |
| Cloudinary storage referenced | ~0.02 GB | **~13.6 GB of 19.5 GB** |
| Orphaned originals | ~3,560 / ~14.6 GB (contaminated) | **1,673 / 1.46 GB** |

**Cloudinary account (live now):** 4,324 originals · 8,457 derived · **19.55 GB storage** · **115.52 / 60 credits (193%)** · transformation breakdown unchanged (`sd_video_second` 21,472 + `extra_avif_mp_encoding` 10,996 + `transformation` 10,687 + `fourk_video_second` 1,165 + `hd_video_second` 591).

**Conclusion: the storage side is much healthier than feared. ~70% of stored bytes are legitimately referenced. The transformation bill (71 credits) is still the entire problem; storage cleanup reclaims only ~5 credits.**

---

## 3. EXACT PRODUCTION MANIFEST STATISTICS

### 3.1 Originals — 4,324 objects / 14.62 GB

| Classification | Count | Size | Meaning |
|---|---:|---:|---|
| **GLOBAL_TALENT_MEDIA** | **2,527** | **11.06 GB** | Referenced by a live talent (directly, or via a submission/application media item in a global category: portfolio/indian/western/intro_video/headshot). Talent-owned canonical. |
| **PROJECT_AUDITION_MEDIA** | **124** | **2.10 GB** | Referenced by a live submission (audition take) or project material; not talent-owned. |
| **ORPHAN** | **1,673** | **1.46 GB** | No identity reference (own media_id / `take_` id / exact public_id / exact delivery URL) in any live document. |
| UNKNOWN | 0 | — | — |
| *(external Cloudinary demo assets: `waves`, `cloudinary-logo-vector`, …)* | *13* | *0.008 GB* | *counted under ORPHAN* |

### 3.2 Derived — 8,457 objects / 4.93 GB

| Classification | Count | Size | Meaning |
|---|---:|---:|---|
| **ACTIVE_DERIVED_ASSET** | **2,396** | **0.77 GB** | Parent original referenced + transformation string is canonical (or not clearly retired). Mostly `w_400` thumbnails (1,947) and video posters (247). |
| **LEGACY_DERIVED_ASSET** | **3,427** | **3.56 GB** | Parent original still referenced, but transformation string is retired/fragmented/download. Dominated by full-res AVIF re-encodes (2,382) and fragmented video transcodes (239) / posters (207). **Regenerates on demand if ever re-requested** — safe to purge. |
| **ORPHAN** | **2,634** | **0.61 GB** | Parent original itself is orphaned. Image resizes (1,046), AVIF (762), thumbnails (496), video (321). |

### 3.3 ORPHAN originals — breakdown (1,673 / 1.46 GB)

| Cut | Count | Size |
|---|---:|---:|
| Parent document **deleted** (project/submission/application/talent removed, asset left behind) | 1,449 | 0.85 GB |
| Parent document **still alive** — **EXAMINE, do not bulk-delete** (§5) | 196 | 0.60 GB |
| Parent scheme n/a (Cloudinary demo assets) | 28 | 0.01 GB |
| *(of the above, referenced only by a stale `asset_metadata` row)* | *527* | *0.64 GB* |

By folder scheme: `projects/` 627 · `talents/` 410 · `applications/` 323 · `submissions/` 270 · `admin_media/` 15 · `scout_capture/` 15 · external 13.
By month created: 2026-07 → 508 · 2026-08 → 1,165 (recent accumulation — consistent with the deletion-path bugs identified in the code audit: `delete_project` / `delete_submission` / `admin_remove_media_item` perform no Cloudinary cleanup).
By resource type: image 1,349 · video 100 · raw 11 (parent-deleted subset).

### 3.4 Total cleanup-eligible footprint (production)

| Tranche | Objects | Size | Safety |
|---|---:|---:|---|
| ORPHAN originals, parent deleted | 1,449 | 0.85 GB | High — parent gone, no reference. Still: per-asset proof + retention window before destroy. |
| ORPHAN derived (parent orphaned) | 2,634 | 0.61 GB | High — parent itself orphaned. |
| LEGACY derived (retired transform strings, parent alive) | 3,427 | 3.56 GB | High — Cloudinary regenerates on demand; nothing breaks. |
| **Subtotal (P9 candidate)** | **~7,510** | **~5.0 GB (~5 storage credits)** | |
| ORPHAN originals, parent alive — **EXAMINE first** | 196 | 0.60 GB | Manual — likely replaced media; a few could be in-flight races. |
| Stale `asset_metadata` rows | 527 | (Mongo only) | Mongo cleanup, targeted by id — not a Cloudinary delete. |

---

## 4. The six classifications (definitions used)

1. **GLOBAL_TALENT_MEDIA** — asset identity appears in a live `db.talents` doc, or in a live submission/application media item whose category is global (`portfolio`, `indian`, `western`, `video`, `intro_video`, `headshot`, `additional_portfolio`, look-categories). Owner = talent. **Never deleted by a project/submission operation.**
2. **PROJECT_AUDITION_MEDIA** — identity in a live submission with a `take*` category, or a live project material, and **not** referenced by any talent doc. Owner = project/submission. Deletable only via project-retention purge.
3. **ACTIVE_DERIVED_ASSET** — derived; parent original is referenced; transformation string is canonical (`w_400,c_fill,dpr_auto,f_auto,q_auto`, `c_fill,h_338,w_600,q_auto/f_jpg`, `c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4`) or not clearly retired.
4. **LEGACY_DERIVED_ASSET** — derived; parent referenced; transformation string is retired (AVIF, `dpr_1.0`, a fragmented 720p/poster variant, any `fl_attachment:` download string).
5. **ORPHAN** — no identity reference anywhere in live docs (originals), or parent original is orphaned (derived).
6. **UNKNOWN** — referenced but ownership ambiguous (0 in production), plus 13 Cloudinary sample/demo assets outside the `talentgram/` namespace.

---

## 5. Caveats & the 196 "EXAMINE" cases

**Classifier heuristics (be aware before acting):**
- **Folder scheme ≠ ownership.** Only 418 of 2,527 global-talent assets physically live under `talentgram/talents/…`; the rest sit in submission/project/application folders (copy-by-value where the physical asset stayed put and the talent doc references its URL). The new model's `owner_type` header (P3) is what makes ownership authoritative — folder strings cannot.
- Old audition-scheme assets with a **category-word leaf** (`…/submission_{sid}/intro_video`) have no unique id in their path; they're matched only by exact-full-public-id substring in a delivery URL. A handful may be mis-bucketed GLOBAL vs PROJECT_AUDITION — immaterial for deletion safety (both = KEEP), matters only for the P3 ownership backfill.
- 64 `projects.materials` references bucket as PROJECT_AUDITION_MEDIA — project brief attachments, technically a third category. KEEP either way.

**The 196 ORPHAN-originals-with-a-live-parent (0.60 GB) — these need eyes, not a batch job:**
- Distribution: submissions 108 · applications 49 · talents 15 · projects 12 · admin_media 12. 195 of 196 have a real-id leaf. Recent (135 in Aug 2026).
- Almost certainly **replaced media**: a talent uploaded a new intro video / photo and the replace path left the old Cloudinary asset behind without cleanup (matches the audit finding that `POST /talents/{tid}/media` and the submission replace flow don't always reference-check the outgoing item). Examples: `talentgram/talents/…_zarah-philip/profile_images/…`, `talentgram/talents/…_kanchan-khatana/portfolio_videos/…`.
- A small number could be **in-flight races** (webhook/DB write pending at scan time).
- **Action:** enumerate all 196 in the P8 dry-run engine with the specific parent doc + current media array, and clear them one batch at a time with a re-check immediately before each destroy. Never include them in a bulk pass.

---

## 6. REVISED ARCHITECTURE PROPOSAL (updated with production reality)

The shape is unchanged from Phase 0. Production data sharpens three points:

### 6.1 `owner_type` header is non-negotiable and is the whole game
Folder scheme is proven unreliable (§5). Every `media[]` item gets, in the P3 additive migration:
```
owner_type:  "talent" | "project_submission"
owner_id:    talent_id | submission_id
project_id:  <id> | null
talent_id:   <id>            (always set)
source_talent_media_id: <id> | null     (set iff this is a value-copy of a global item)
```
Backfill logic: category ∈ global-set → `owner_type:"talent"`; category ∈ `take*` → `owner_type:"project_submission"`; verified against the manifest's classification per asset.

### 6.2 Storage cleanup is small and secondary — do transformations first
Only ~5 GB / ~5 credits is reclaimable. The 71 transformation credits are 100% of the overage. **Phase order stays P4 (kill eager transcode + AVIF) → P5 (collapse URL fragmentation) before P9 (storage prune).** Do not front-load the delete work.

### 6.3 Deletion model (your decisions 9 & 10) — confirmed feasible against the data
- **Project soft-delete + `audition_retention_days` (default 30) + reaper:** the 64 live projects reference 124 audition originals (2.1 GB). On purge, the reaper enumerates those submissions' `owner_type:"project_submission"` items and destroys only the unreferenced ones. Global media (`owner_type:"talent"`) in the same folders is structurally skipped.
- **Talent hard-delete blocked on dependencies:** 1,264 live talents; the pre-flight checks (active projects, submissions, applications, client links, global media, other refs) are all cheap indexed lookups. Archive (`archived_at`) is the default path.

### 6.4 Storage console (your item 11) — the fix
- `/summary` (live `usage()`) stays — it's the only accurate part.
- Category/project/talent breakdowns: rebuild to aggregate `talents.media[]` **and** `submissions/applications.media[]`, keyed on `owner_type`, presented as two columns: **"Cloudinary actual" (from Admin API)** vs **"Talentgram references" (from Mongo)**, with the delta labelled "unreferenced / derived / orphaned — run Health Scan".
- Health Scan: report-only, full reference set (incl. `applications.media`, `projects.materials`, `links`, `casting_pipeline`), classification per the six buckets above, **no execute button**.

### 6.5 Transformation policy (your items 3–5) — confirmed, no change
Video: no eager transcode, serve originals, codec-gated exception only. Images: one `f_auto` delivery (WEBP, drop AVIF) + one stored `w_400` thumbnail, no `dpr_auto`. Downloads: `fl_attachment` as a delivery flag, no `f_mp4`/`f_avif` rewrite. Canonical URL helpers replace the 12/7/4/146 fragmented strings. Enable Cloudinary strict transformations with a ~5-string whitelist once the code emits only those.

**Projected transformation reduction unchanged:** −75% (≈ back under 60 credits) from the two high-confidence changes alone (stop video transcode ≈ −30 cr, drop AVIF ≈ −15 cr); −90%+ with the recompute/fragmentation fixes.

---

## 7. The health/cleanup safety change (done)

**Backend** `backend/routers/cloudinary_admin.py`: `POST /health/cleanup` now immediately `raise HTTPException(410, …)` — reads nothing, deletes nothing, mutates nothing. Route kept registered (`deprecated=True`) so a stale cached frontend gets a clear 410, not a 404. All the old destroy/`update_many({})` logic removed.

**Frontend** `frontend/src/pages-components/StorageDashboard.jsx`: the "One-Click Repair & Cleanup" button and its `handleOneClickCleanup` handler removed. The read-only "Storage Health Scan" (GET `/health`) button stays. Unused `Sparkles` import removed.

**Test** `backend/tests/test_health_cleanup_disabled.py` (2 tests, passing):
1. `run_storage_cleanup` raises `HTTPException(410)` and — with `cloudinary.uploader.destroy`, `cloudinary.api.delete_resources`, `cleanup_media_storage`, R2 client, `assert_providers_healthy`, the physical-list helpers, and `log_storage_action` all patched — **none are called**.
2. The route stays registered as `deprecated`.

*(Unrelated pre-existing failure noted: `test_p0_storage_hardening.py::test_get_storage_analytics_fallback` fails on `main` before and after this change — a mock issue in the archived-storage aggregate path, not caused here.)*

---

## 8. Manifest file & schema

`scratchpad/p1_out/prod_manifest.jsonl` — 12,781 rows (4,324 originals + 8,457 derived). Also `prod_manifest.csv` for spreadsheet review.

Per-row fields (originals): `public_id, asset_id, resource_type, type, format, bytes, width, height, created_at, folder, folder_scheme, leaf, leaf_is_real_id, referenced, reference_count, reference_sources[], reference_categories[], owning_talent_id, owning_project_id, submission_ids[], application_ids[], submission_in_path, parent_doc_alive, in_asset_metadata_only, classification, proposed_action`.

Per-row fields (derived): `public_id (parent), derived_id, format, bytes, width, height, transformation, transform_category, folder_scheme, parent_original_referenced, is_download_string, is_retired_string, is_canonical_string, classification, proposed_action`.

**No `proposed_action` is "delete now" — every deletion candidate is "CANDIDATE FOR DELETION after retention window; prove per-asset before destroy".**

---

## 9. STOP — awaiting your approval

Per your stop condition, both required steps are done:
1. ✅ Destructive `health/cleanup` endpoint + button disabled, with a test.
2. ✅ Read-only production cross-join complete; dry-run manifest produced.

**No P3 (ownership model), P4 (transformation removal), P5 (URL consolidation), or any migration/cleanup will begin until you approve this report.**

Open questions for you:
- **The 527 stale `asset_metadata` rows** — approve a targeted, Mongo-only cleanup (delete the specific rows by `id`, no Cloudinary calls) as part of P3? Or leave them for P8?
- **The 196 EXAMINE orphans** — want the individual list (public_id + parent doc + current media array) as a separate appendix now, or fold it into P8?
- Confirm phase order P3 → P4 → P5 → P6 → P7 → P8 → P9, and that I proceed to **P3 (additive `owner_type` schema migration, dry-run first)** on your go.
