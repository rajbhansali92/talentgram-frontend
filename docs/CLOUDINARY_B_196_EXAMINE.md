# Cloudinary — Section B: the 196 "EXAMINE" Orphans (parent doc alive)

**Status:** Investigation only. **Zero deletions, zero writes.** Read-only against production Mongo (via `railway run`) + Cloudinary `api.resource()` (read). This is the appendix requested before cleanup work.

**Scope:** the 196 originals the P1 manifest classified `ORPHAN` **and** `parent_doc_alive = True` — i.e. no identity reference anywhere in live documents, but the submission/application/talent whose id sits in the folder path still exists.

Full per-asset table: `scratchpad/b_out/examine_196_FINAL.csv` (196 rows, all fields below).

---

## 1. Result

| Classification | Count | Size | What it is |
|---|---:|---:|---|
| **SAFE_ORPHAN** | **180** | **0.556 GB** | Detached upload on a still-live parent — added then removed during editing. In no media array, referenced nowhere, no dependents. Owner is known (the parent submission/application in the folder path, corroborated by Cloudinary tags / `asset_metadata` where present). |
| **REPLACED_MEDIA** | **15** | **0.035 GB** | Superseded talent profile media — the talent re-uploaded and the old Cloudinary asset was orphaned by a replace flow that didn't clean up. **Not** the current canonical asset. |
| **LEGITIMATE_REFERENCE** | **1** | **0.005 GB** | Actually still in its parent's current media array — the P1 leaf-matcher missed it (it's an old audition-scheme path with a category-word leaf). Matcher bug, not an orphan. |
| **OWNERSHIP_METADATA_MISSING** | **0** | — | — |
| **UNKNOWN** | **0** | — | — |
| **Total** | **196** | **0.596 GB** | |

**No asset was classified UNKNOWN** — for every one, the folder path + Cloudinary tags + `asset_metadata` + parent-document state gave enough to place it. Nothing was guessed; where a category couldn't be resolved (168 of the 180 SAFE_ORPHAN), ownership was still unambiguous from the folder path and the classification does not depend on the category.

---

## 2. What these actually are (evidence)

### SAFE_ORPHAN — 180 (submissions 108, applications 49, admin_media 12, projects 11)

- **Parent status:** `submitted` 79, `updated`/`ongoing`/`locked`/`complete` ~64, `draft` 21, other — all **alive and healthy**, each with a populated current media array (e.g. `{intro_video:1, indian:4, western:8, image:10}`).
- **Timing:** 153 of 173 (with timestamps) were created **within the same window** as the parent's current media — i.e. uploaded in the same editing session, then removed. Only 20 predate the parent's earliest current asset.
- **These are images (177) + a few videos (17) + 2 raw** that a talent added to a submission/application and then deleted before finishing, on the flat folder scheme `talentgram/{submissions|applications}/{id}/{media_uuid}`. The media item was `$pull`ed from the array; the Cloudinary asset was left behind — exactly the gap the code audit flagged in `admin_remove_media_item` / the talent-facing replace paths.
- **Ownership:** unambiguous — the `{id}` in the folder is a live submission/application. Corroborated where tagged (`asset_kind=audition_video`, `category=take`) or tracked (`asset_metadata.asset_type=admin_upload` / `profile_image`).
- **Not global talent media** — none are referenced by any `db.talents` doc; they belong to the specific submission/application.

Example rows:
```
talentgram/applications/f022cbcf-…/a8f8c680-…   image/jpg  50 KB   parent=applications/f022cbcf (submitted)  2 derived
talentgram/applications/f8f21fae-…/decc6571-…   image/png  3.9 MB  parent=applications/f8f21fae (submitted)  2 derived
talentgram/submissions/4132cbf9-…/e42f8f71-…    image/jpg  2.0 MB  parent=submissions/4132cbf9 (updated)     2 derived
```

### REPLACED_MEDIA — 15 (all `talentgram/talents/{tid}/…`)

- **14** are `profile_images` for **2 talents**: `3c0154f4…` (zarah-philip) — **12 superseded headshots** (she re-uploaded her profile picture 12 times; each prior Cloudinary asset orphaned), and `7d1f02ee…` (jessica-…) — 2. Confirmed via `asset_metadata.asset_type=profile_image` + Cloudinary tag `asset_type=profile_image, talent_id=…`.
- **1** is a `portfolio_video` for `8025b311…` (kanchan-khatana) — a replaced portfolio clip.
- **These are not current canonical media** — the talents' live `db.talents.media[]` holds their current headshot; these are the old ones the "Replace" button left behind (audit finding: `POST /talents/{tid}/media` and the submission replace flow don't always reference-check the outgoing item before it's superseded).

### LEGITIMATE_REFERENCE — 1

`talentgram/projects/fecd1329-…/auditions/unknown_talent_isha-nandal/submission_aa65aeee-…/intro_video` — video/mp4, 5.2 MB. It **is** in submission `aa65aeee`'s current media array (category `intro_video`). The P1 identity matcher missed it because the public_id leaf is the literal word `intro_video` (old audition folder scheme), not a UUID — so it fell through to a substring check that didn't fire. **Fix the P1/P2 matcher** (handle category-word leaves via exact-full-public-id match against stored URLs); this is not an orphan.

---

## 3. Per-asset fields captured (in `examine_196_FINAL.csv`)

`classification` · `proposed_ownership` · `public_id` · `asset_id` (Cloudinary) · `resource_type` · `format` · `bytes` · `created_at` · `folder` · `folder_scheme` · `parent_kind` · `parent_id` · `parent_doc_status` · `resolved_category` · `category_source` (parent_media / asset_metadata / cloudinary_tag / folder_segment) · `timing_vs_parent_media` · `talent_id` · `project_id` · `submission_id` · `application_id` · `referenced_by_identity` · `references_outside_expected_parent` · `cloudinary_derived_count` · `proposed_future_action`

---

## 4. Proposed disposition (NO action taken)

| Class | Proposed future action | When |
|---|---|---|
| SAFE_ORPHAN (180) | Delete the Cloudinary original + its derived children. Per-asset reference re-check immediately before each destroy. Owner recorded in the audit log as the parent submission/application. | **P9 only**, after ≥30-day retention window and explicit approval of the P9 batch manifest. |
| REPLACED_MEDIA (15) | Same — delete superseded original + derived. Verify the talent's **current** `profile_image` / `portfolio_video` is untouched (different `public_id`) before each destroy. | **P9 only**, same gates. |
| LEGITIMATE_REFERENCE (1) | **KEEP.** Fix the P1/P2 identity matcher so category-word-leaf audition assets resolve. Backfill `owner_type` in P3. | P3 (matcher fix) |

**These 195 deletable assets add ~0.59 GB to the P9 candidate pool** (which was ~5 GB from the P1 manifest). They do not change the priority order — transformations remain the cost problem.

---

## 5. Feeds into later phases

- **P3:** the 1 LEGITIMATE_REFERENCE exposes a matcher gap — P3's ownership backfill must handle old audition-scheme paths (`…/submission_{sid}/{category_word}`) by matching the stored delivery URL exactly, not the leaf.
- **P6 (deletion hardening):** the 180 SAFE_ORPHAN + 15 REPLACED_MEDIA are direct evidence that the replace/remove paths (`admin_remove_media_item`, `POST /talents/{tid}/media` replace, submission media delete) must route through the shared reference-aware cleanup helper — otherwise this population regrows every week.
- **P8/P9:** these 196 enter the dry-run cleanup manifest as pre-classified rows; none are auto-deleted.
