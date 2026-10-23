# Cloudinary — `f_mp4` Disambiguation (the 113 `UNKNOWN_DERIVED` bare-`f_mp4` derivatives)

**READ-ONLY analysis. Executed 2026-08-31.** Zero Cloudinary writes / deletes / transformations /
uploads. Zero MongoDB writes / updates / migrations. Nothing was deleted; nothing is authorized
for deletion by this document.

Machine-readable: `docs/CLOUDINARY_FMP4_DISAMBIGUATION.json` (all 113 rows, every field, the
per-candidate 13-point safety check).

---

## What these 113 assets are

All 113 carry the transformation **`f_mp4` and nothing else** — a bare fetch-format-to-MP4, no
`fl_attachment`, no `w_`/`h_`/`c_limit`/`vc_auto`. P8.5 could not split them from the transform
string alone (it is `compat-or-retired-download` — ambiguous) and parked all 113 as
`UNKNOWN_DERIVED` / **1.513 GB — the single largest unresolved byte bucket**.

A bare `/f_mp4/` derivative has exactly **two** possible origins in this codebase:

| Origin | Code | Status | Current? |
|---|---|---|---|
| **P4 browser-compat delivery** | `core.compat_video_delivery_url()` — fired only when `core.video_needs_compat_delivery(format, codec)` is `True` | **current, sanctioned** (the one P4 video exception) | generates a bare `f_mp4` today for HEVC / non-web-container parents |
| **Legacy download transcode** | `_get_video_download_url()` / `getVideoDownloadUrl()` — "stripped `f_*`, appended **`f_mp4`**, forced `.mp4`" on every Download button / ZIP entry | **retired in P5** (both now return the canonical URL, no `f_mp4`) | nothing in the current app generates it |

The retired 720p chain (`w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4`) is **multi-segment** and is a
different P8.5 family — **none** of these 113 are that chain. `OLD_720P` count in this bucket = **0**.

## The decisive test — current P4 compat, per parent

For every one of the 113 I fetched the parent original
(`cloudinary.api.resource(pid, resource_type="video", media_metadata=True)` — the plain call
returns an empty `video: {}`; the real codec is the top-level `codec` field, only present under
`media_metadata=True`) and ran the **exact production function** `core.video_needs_compat_delivery`
against the real container + codec. Not modified, not reimplemented.

| Parent codec | Count | `video_needs_compat_delivery` | Meaning |
|---|--:|:--:|---|
| `hevc` (H.265) | **4** | **True** | bare `f_mp4` **is** the current compat delivery derivative → keep |
| `h264` (AVC) | **109** | **False** | parent is web-safe now; app serves the canonical original; the bare `f_mp4` is a retired `_get_video_download_url` transcode |

Cloudinary's own `compatible` fourcc tag agrees: the 4 keepers are `hvc1`-family; the 109 others
are `avc1` / `mp42` / `qt`(H.264-in-QuickTime).

## Persisted-URL findings

**0 of 113** have their derived URL persisted anywhere. Checked against
`build_reference_index`'s `url_index` (every `url` / `poster_url` / `thumbnail_url` /
`original_url` / `compat_delivery_url` / `video_url` / `secure_url` in every live media doc),
its `persisted_derived_variants` `(parent, transformation)` set, and a direct scan for any live
`media.url` containing `/f_mp4/`. **Production currently has zero media items flagged
`needs_compat_delivery` and zero media URLs containing `/f_mp4/`** — these derivatives were
created by the download path (which never wrote the URL back to Mongo) and by pre-P4 compat
paths, and P5 stopped anything from persisting a bare `f_mp4`.

Because none is persisted, the **PERSISTED URL RULE** protects none of them, and the 4 HEVC
keepers are protected by the **CURRENT COMPATIBILITY RULE** (not the persisted rule) — the app
would lazily regenerate their compat URL on next view.

## MongoDB reference findings

`mongo_reference_count = 0` for all 113 (no doc stores the derived id or its exact URL). Parent
originals: **107 / 113** are actively referenced by a live doc; **6 / 113** have **no P3 owner
and no live Mongo reference at all** — their submission documents no longer exist
(`testing-bhansali` test data ×2, an orphan `admin_media` WhatsApp video ×1, a vanished
`angela-kumar` submission ×3).

## Parent status

All 113 parent originals **exist** in Cloudinary and are videos. All 113 derived assets are
still **present on their parent's `derived[]`** list, with live bytes **exactly matching** the
P8.5-recorded bytes (0 drift, all 113). None of the DELETE_CANDIDATE / REVIEW_LINKED parents is
in a soft-deleted submission or project.

---

## 1–3. Classification totals & bytes

| Classification | Count | Bytes | GB | Disposition |
|---|--:|--:|--:|---|
| **CURRENT_COMPATIBLE** | 4 | 142,597,140 | 0.143 | **PROTECT** — active P4 compat derivative (HEVC parent) |
| **UNKNOWN** | 6 | 62,523,874 | 0.063 | **PROTECT** — parent has no P3 owner / no live Mongo reference; evidence insufficient, do not guess |
| **REVIEW_LINKED_CANDIDATE** | 35 | 524,938,971 | 0.525 | **HOLD** — meets every delete proof except the parent talent is in an active client-review link |
| **DELETE_CANDIDATE** | 68 | 783,210,484 | 0.783 | analysis-only candidate, **subject to P9 fresh revalidation — NOT deleted** |
| **Total** | **113** | **1,513,270,469** | **1.513** | |

User-requested summary buckets:

| Bucket | Count | Bytes |
|---|--:|--:|
| CURRENT_COMPATIBLE | 4 | 142,597,140 |
| RETIRED_DOWNLOAD (historical purpose of all 109 H.264 rows) | 109 | 1,370,673,329 |
| OLD_720P | 0 | 0 |
| OTHER_LEGACY_COMPAT | 0 | 0 |
| PROTECTED_PERSISTED | 0 | 0 |
| UNKNOWN | 6 | 62,523,874 |
| DELETE_CANDIDATE (all 13 safety proofs pass) | 68 | 783,210,484 |

## 4. Current-compatibility determination

`video_needs_compat_delivery` = **YES for 4** (all `hevc`), **NO for 109** (all `h264`). The 4
YES are the only ones where the bare `f_mp4` is a live delivery dependency.

## 5. Historical-purpose determination

- **4** — current P4 browser-compat delivery derivative.
- **109** — retired `_get_video_download_url()` / `getVideoDownloadUrl()` download transcode
  (bare `f_mp4` on a web-safe H.264 `.mp4`/`.mov`); the P5 rewrite removed the only code that
  produced or consumed these.

## 6. Persisted-URL findings

**0 / 113 persisted.** See "Persisted-URL findings" above.

## 7. MongoDB-reference findings

**0 / 113** referenced by derived id or exact URL. Parent originals: 107 referenced, 6 orphaned.

## 8. Parent status

113 / 113 parents exist; 113 / 113 derivatives still on the parent; bytes match exactly; 0
parents in a soft-deleted container.

## 9. Proposed deletion candidates

**68**, total **783,210,484 bytes (≈ 0.78 GB)** — the `DELETE_CANDIDATE` rows only.
**Not** the 35 `REVIEW_LINKED_CANDIDATE`, **not** the 6 `UNKNOWN`, **not** the 4
`CURRENT_COMPATIBLE`. Every one of the 68:

- `current_P4_compat_decision == False` (parent H.264, web-safe — verified against the live
  production function)
- transformation is the retired bare `f_mp4`
- parent original exists, is active, P3 ownership known (43 `talent`, 25 `project_submission`;
  no conflicts, no ambiguity)
- derived URL not persisted anywhere; `mongo_reference_count == 0`
- parent talent/submission **not** in any active client-review link
- not in `source_lineage`, not in `pending_media_deletions`, not in a soft-deleted
  submission/project
- **all 13 CRITICAL SAFETY CHECK proofs pass** (see §10)

## 10. Per-candidate evidence — CRITICAL SAFETY CHECK

For each of the 68 `DELETE_CANDIDATE` the JSON records `critical_safety_check` with all 13
booleans `true`:

`current_P4_compat_not_required` · `not_persisted` · `no_protected_mongo_reference` ·
`parent_exists` · `parent_active` · `p3_ownership_known` · `no_client_review_dependency` ·
`no_application_dependency` · `no_historical_dependency` (source lineage) · `no_ledger_dependency`
· `not_in_soft_deleted_container` · `transformation_retired` · `app_cannot_require_this_exact_derivative`.

Any single failed proof downgrades the row to `UNKNOWN` (or `REVIEW_LINKED_CANDIDATE` when the
**only** failure is the review-link proof). 35 rows downgraded to `REVIEW_LINKED_CANDIDATE`
on that basis; 0 rows failed any other proof; the 6 `UNKNOWN` failed `parent_active` +
`p3_ownership_known`.

The `REVIEW_LINKED_CANDIDATE` bucket: these 35 are H.264 / web-safe / not-persisted / owned
retired-download transcodes whose parent talent has ≥1 active review link. All 32 production
links are talent-scoped (`submission_ids` empty on every link), and post-P5 the review UI
(`ClientView.jsx`) serves playback, poster and Download all from **canonical** URLs — so there
is no *functional* dependency on a bare `f_mp4`. They are held separately purely because the
task's safety list names "no client review dependency" as a required proof and the parent is
link-adjacent. They are **not** proposed for deletion here.

## 11. API calls performed

All read-only:

| Call | Count |
|---|--:|
| `cloudinary.api.resource(pid, resource_type="video", media_metadata=True)` | 113 (+ ~10 during codec-probe) |
| `cloudinary.api.usage()` | 2 |
| `build_reference_index` (Mongo `find` over talents/submissions/applications/projects/links/asset_metadata) | 2 |
| `build_ledger_index` (Mongo `find` over `pending_media_deletions`) | 2 |
| Mongo `find` scans (talents 152 / submissions 178 / applications 51 cursors) + targeted lookups | ~390 |

No `transformations()` / `transformation()` calls were needed — P8.5 already enumerated the
derived rows; this phase only needed each parent's codec.

## 12–16. Zero-mutation confirmation

- **12. ZERO Cloudinary writes** — no `uploader.*`, no `destroy`, no `delete_resources*`, no
  `delete_derived*`, no eager/incoming transformation, no transformed-URL fetch.
- **13. ZERO Cloudinary deletes.**
- **14. ZERO transformations generated** — `usage().transformations` = 71,250 before and after.
- **15. ZERO uploads** — `usage()` derived count 8,476 unchanged; storage bytes unchanged.
- **16. ZERO MongoDB writes** — every DB call is `find` / `count_documents`. No
  `update_*` / `insert_*` / `delete_*` / `$set` anywhere in the analysis scripts.

---

## Full 113-row classification

Sorted by classification (keepers first), then descending derived bytes. `p.` = parent, `d.` =
derived. `persisted` is `no` for every row. Full per-row fields (codec_tag, profile,
pix_format, Cloudinary `compatible`, owner ids, project/submission/application ids, the 13
safety booleans) are in the JSON.

| # | derived_id | parent (short) | p.fmt | p.codec | p.dims | d.fmt | d.bytes | xf | created_at | P3 owner | persisted | Mongo refs | P4 compat now | historical purpose | classification | confidence |
|--:|---|---|---|---|---|---|--:|---|---|---|:--:|--:|:--:|---|---|---|
| 1 | `717315117ce76cf1d5a6ba2f020fec80` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mp4 | hevc | 720x1280 | mp4 | 119,384,347 | `f_mp4` | 2026-08-19 | project_submission | no | 0 | YES | compat delivery | **CURRENT_COMPATIBLE** | high |
| 2 | `d701ab2f15e83b78a66a30f14b4be737` | talents/af28ac19-6f98-48ff-9cf6-99013f1f6a47_saloni-rana/portfolio | mov | hevc | 1976x3264 | mp4 | 12,907,855 | `f_mp4` | 2026-08-07 | talent | no | 0 | YES | compat delivery | **CURRENT_COMPATIBLE** | high |
| 3 | `64304ab6172cd7deed19a414fe9b33da` | projects/9ad5ca4a-f1f2-4cf7-a056-242a89266ad3/auditions/d7d7cd97-d | mov | hevc | 1920x1080 | mp4 | 5,510,849 | `f_mp4` | 2026-08-20 | project_submission | no | 0 | YES | compat delivery | **CURRENT_COMPATIBLE** | high |
| 4 | `efcd7841ec4cc97b31f6d0d932f61945` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | hevc | 1614x1014 | mp4 | 4,794,089 | `f_mp4` | 2026-08-18 | talent | no | 0 | YES | compat delivery | **CURRENT_COMPATIBLE** | high |
| 5 | `e333adcd6640feeca3bff0d0fe4a6869` | projects/2af9dc91-0ead-4166-aade-1f79981c69b1/auditions/0f87bc97-2 | mp4 | h264 | 1280x720 | mp4 | 19,619,719 | `f_mp4` | 2026-08-10 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 6 | `b769bf3cf0f64b9b057c2864368301a4` | projects/2af9dc91-0ead-4166-aade-1f79981c69b1/auditions/0f87bc97-2 | mp4 | h264 | 1280x720 | mp4 | 15,506,107 | `f_mp4` | 2026-08-10 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 7 | `ed922d62409785f100a3bc2628fd0841` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 8,735,641 | `f_mp4` | 2026-08-14 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 8 | `bd22b5c08efb95b1e81342a331f2b7fa` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 7,663,693 | `f_mp4` | 2026-08-14 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 9 | `e5793cf9e5f6362e63fe7622ba43a557` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 6,100,887 | `f_mp4` | 2026-08-14 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 10 | `8054a03b67cbc511dcff7d7ac85df207` | admin_media/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/85a9152a-20ca-4a4 | mp4 | h264 | 1280x720 | mp4 | 4,897,827 | `f_mp4` | 2026-08-10 | — | no | 0 | no | retired download transcode | **UNKNOWN** | low |
| 11 | `1193f58b918e9943142c568f7b932b75` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/6362dcb0-b | mov | h264 | 1596x2646 | mp4 | 102,194,279 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 12 | `e48c4b78131e3cdd868717e100099807` | projects/3213d62c-57ef-4504-ab1e-722602191533/auditions/unknown_ta | mov | h264 | 3840x2160 | mp4 | 88,510,383 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 13 | `20d32a8cfa4583a8ec51a70cbe420e3c` | projects/3213d62c-57ef-4504-ab1e-722602191533/auditions/a4d7b36d-7 | mov | h264 | 3840x2160 | mp4 | 43,219,436 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 14 | `3031c54aa5c199faf7b51ef99922a7f8` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/01b5b576-0 | mp4 | h264 | 1920x1080 | mp4 | 37,734,965 | `f_mp4` | 2026-08-20 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 15 | `d5f5bb0b9a1bdc8edd5f379b0cebc6d4` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 26,030,925 | `f_mp4` | 2026-08-17 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 16 | `0301e469cff38af13087b45c7e0e51e7` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 22,136,123 | `f_mp4` | 2026-08-16 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 17 | `831e2a695ee22e40c65bd867cb093801` | projects/aafee923-5ba7-48f2-8a99-3da91bd54abb/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 19,236,253 | `f_mp4` | 2026-08-12 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 18 | `87f9e12134c80b7d4c9a636c3ee0ad56` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 3840x2160 | mp4 | 15,732,726 | `f_mp4` | 2026-08-20 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 19 | `36fe628c338af37274494d4853d30142` | projects/36ac74dd-b682-403e-93c7-b65e2d8ab08b/auditions/8e605c26-e | mov | h264 | 3840x2160 | mp4 | 15,732,726 | `f_mp4` | 2026-08-21 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 20 | `0dfad9119eac7b7e667695517e856479` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/21bee031-f | mov | h264 | 1080x1920 | mp4 | 13,142,372 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 21 | `67ca8b4646c137fb8b17dbedfda32b7a` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 10,033,395 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 22 | `a72953e57e983ac23b2b61f25ec178d5` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/21bee031-f | mp4 | h264 | 1280x720 | mp4 | 9,017,778 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 23 | `bbd37a7943e9bbaf52be7d159bdaaae1` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 8,735,641 | `f_mp4` | 2026-08-14 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 24 | `87343d75c6810a1a3f6818bc6cd81da2` | projects/36ac74dd-b682-403e-93c7-b65e2d8ab08b/auditions/unknown_ta | mov | h264 | 3840x2160 | mp4 | 8,721,177 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 25 | `eb90e774409c9abd4790aa60ede4b77a` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 1440x1920 | mp4 | 8,269,287 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 26 | `564250c9bfa50e762bac0fb2cabcdbb8` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/3a467c34-8 | mp4 | h264 | 1280x720 | mp4 | 7,834,097 | `f_mp4` | 2026-08-15 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 27 | `1c0da5581c3bdfc303ec46c9f4d74e0a` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 7,663,693 | `f_mp4` | 2026-08-14 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 28 | `9520c3c1deff8a4139b2a95d520d5686` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/6362dcb0-b | mp4 | h264 | 1280x720 | mp4 | 7,652,713 | `f_mp4` | 2026-08-13 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 29 | `55d470843e265b91378de89a7e30fde6` | projects/afde9a67-8dec-470e-83f2-9a764fd7a910/auditions/21bee031-f | mov | h264 | 1920x1080 | mp4 | 7,565,203 | `f_mp4` | 2026-08-20 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 30 | `917b2715280e8d3362101620a8e20ed4` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 6,336,757 | `f_mp4` | 2026-08-16 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 31 | `7aeb99cb89da9374d32bf23a32aa838a` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 6,319,607 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 32 | `79c93765264536cc9f5417d6fbc8f7c5` | projects/38e3ce02-03a6-4b71-9a9c-13f0fb165c56/auditions/a4d7b36d-7 | mp4 | h264 | 1024x576 | mp4 | 6,100,887 | `f_mp4` | 2026-08-14 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 33 | `550cb10c3e52e25d8884734ceafcba2c` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 5,927,549 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 34 | `7f13621fa84c3955813c4ae38b57498a` | applications/09b5f1ba-8a96-4482-b2ba-c62d79beb8d2/intro_video | mp4 | h264 | 1280x720 | mp4 | 5,755,648 | `f_mp4` | 2026-08-16 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 35 | `436aa12997fa10531fa8661010b344c0` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/21bee031-f | mov | h264 | 1280x720 | mp4 | 5,659,380 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 36 | `c0a540c0552d2e443359458c55285c7b` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 480x800 | mp4 | 5,605,129 | `f_mp4` | 2026-08-20 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 37 | `1bc797e1f827c3f49af9c1d959a94695` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 1920x1080 | mp4 | 5,152,388 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 38 | `22f50bd8b8e3fdfe9c3901ce8c6fdf82` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 480x704 | mp4 | 3,724,106 | `f_mp4` | 2026-08-20 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 39 | `071964d0d77dd11763a3827f7abd3a94` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/42969e7c-2 | mov | h264 | 1920x1080 | mp4 | 3,582,614 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 40 | `141e3c141868aa53740858568552f255` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 832x464 | mp4 | 2,960,572 | `f_mp4` | 2026-08-20 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 41 | `25135d95e6130efa84a0c2f2b188a0c3` | applications/8d2ffa1f-7514-4c8d-ae0a-b35623de9067/intro_video | mov | h264 | 1280x720 | mp4 | 2,658,187 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 42 | `9e53bc69d0ca5e33d36e3a999e7277da` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/21bee031-f | mov | h264 | 720x1280 | mp4 | 2,033,360 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 43 | `5fc10b2c3ba0ce828f030e33316c81fe` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/88de33bb-b | mov | h264 | 832x464 | mp4 | 1,606,104 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 44 | `8143a7732d5a45bcf41025dca565ae39` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 1024x576 | mp4 | 1,418,416 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 45 | `dd92a26b9f1eed8dc34752592c43c606` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/21bee031-f | mp4 | h264 | 480x848 | mp4 | 935,095 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **REVIEW_LINKED_CANDIDATE** | medium-low |
| 46 | `dd4d98f1c56cc6217114a463293aaa82` | projects/aaa3e712-b288-41e3-935b-831d6d1b6bac/auditions/1e342b65-e | mov | h264 | 3840x2160 | mp4 | 88,817,141 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 47 | `6b66b275923f7f5078c576403cc3a21e` | projects/aaa3e712-b288-41e3-935b-831d6d1b6bac/auditions/1e342b65-e | mp4 | h264 | 1280x720 | mp4 | 49,817,801 | `f_mp4` | 2026-08-19 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 48 | `7ce1fb567801e9800e81ab699245696d` | projects/aaa3e712-b288-41e3-935b-831d6d1b6bac/auditions/1e342b65-e | mp4 | h264 | 1280x720 | mp4 | 48,711,268 | `f_mp4` | 2026-08-19 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 49 | `078452c33ab95f551b340e2921ac0d6b` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 2160x3840 | mp4 | 26,071,903 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 50 | `4a0f34bf0aead81ba426a5d03779d481` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/dfd73164-e | mov | h264 | 2160x3840 | mp4 | 25,001,150 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 51 | `7741c306abfcfc743dcc40e69b0c5e2a` | projects/aafee923-5ba7-48f2-8a99-3da91bd54abb/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 24,937,206 | `f_mp4` | 2026-08-13 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 52 | `824d30c9de9ddd12c42c894639cc6722` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mov | h264 | 3840x2160 | mp4 | 23,729,733 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 53 | `9189b68efc6c4a2a9617cb71882e58a2` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mp4 | h264 | 720x1280 | mp4 | 22,308,852 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 54 | `1e50a8101c9aa42486476f14323467dc` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 3840x2160 | mp4 | 22,120,672 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 55 | `7c4cfbe0a50de34608fe978c1ebdab31` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 22,082,541 | `f_mp4` | 2026-08-15 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 56 | `97f7ab836f6aa82173af11dd072e89d7` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mp4 | h264 | 720x1280 | mp4 | 20,618,413 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 57 | `58b9fc6e1f17ff68b20c0c9b6d107080` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 1920x1080 | mp4 | 18,001,880 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 58 | `e6a1b8928d2456c7c309d1385908f24f` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mp4 | h264 | 720x1280 | mp4 | 16,914,652 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 59 | `43cb35ba043fa3b695e548e2ecbb22f8` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 16,869,871 | `f_mp4` | 2026-08-15 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 60 | `4a4265cc22471584918824fb69bb2f7e` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/dfd73164-e | mov | h264 | 2158x3840 | mp4 | 16,228,284 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 61 | `dffda2516e22d6ef689b6ca90738b141` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 14,286,012 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 62 | `c576fe394fe368197c7bdbe6b93f308e` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 2160x3840 | mp4 | 14,023,164 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 63 | `7a2532386e3404d3a9c0268ff640378b` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/cce54e82-c | mp4 | h264 | 1282x720 | mp4 | 12,790,420 | `f_mp4` | 2026-08-15 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 64 | `59b68d444db6e333e86b162f3d667628` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 12,404,014 | `f_mp4` | 2026-08-15 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 65 | `acf9b1719d6232224cabdba11edcea3b` | projects/ac346fe5-4050-4360-bc11-e812ddfe6743/auditions/2720f5e4-e | mp4 | h264 | 1280x720 | mp4 | 12,121,098 | `f_mp4` | 2026-08-12 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 66 | `353e47d94698cbbb12b79c2dbaf6a146` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 1920x1080 | mp4 | 12,043,468 | `f_mp4` | 2026-08-19 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 67 | `3727771efb8ef56dd0b4577738cea184` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/dfd73164-e | mov | h264 | 3840x2158 | mp4 | 11,772,187 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 68 | `cb30360ab928b81e40681503f8d2538b` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 1044x720 | mp4 | 11,310,518 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 69 | `fbba1b5db2bb56980fa3a54b23637cbb` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/3527853d-a | mp4 | h264 | 1280x720 | mp4 | 11,277,421 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 70 | `2fd838b16610d8acb1e440b50fa005d5` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 2982x2160 | mp4 | 10,742,977 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 71 | `e07ea51987af3ba6e455287cd77c46e7` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 1096x720 | mp4 | 9,811,410 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 72 | `9aadcfa427aa2a784e29d6acba198a54` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 464x832 | mp4 | 9,538,834 | `f_mp4` | 2026-08-15 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 73 | `243b541de805f10553231de7778ec1fb` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/5370f927-3 | mov | h264 | 2160x3840 | mp4 | 9,424,242 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 74 | `2133589a6f8efe842b7018c00eca056c` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1282x720 | mp4 | 9,303,468 | `f_mp4` | 2026-08-17 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 75 | `3f86f997ee9a96a0c440834f12bab51f` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/cce54e82-c | mp4 | h264 | 1280x720 | mp4 | 8,548,784 | `f_mp4` | 2026-08-12 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 76 | `9a51c205317c68a3a9f4af2af0a9156d` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/f7b9ed14-a | mp4 | h264 | 1280x720 | mp4 | 8,392,872 | `f_mp4` | 2026-08-12 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 77 | `a981026dec7254d5740d54989c7be5e6` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1440x1080 | mp4 | 8,308,655 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 78 | `8103c6f57ad5e6c85a282394ee903c5e` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 1008x720 | mp4 | 8,100,315 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 79 | `c0f0d9277fc80b0f1c46448f2a309c67` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 1920x1080 | mp4 | 7,594,871 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 80 | `fea0513c7d2dfbe8ee158340b1f0d185` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/cce54e82-c | mp4 | h264 | 1280x720 | mp4 | 7,400,048 | `f_mp4` | 2026-08-12 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 81 | `3fe1984270637ccfdd12ed196f14bc31` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 960x720 | mp4 | 7,247,734 | `f_mp4` | 2026-08-17 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 82 | `3945703b4060ee8025878bf8717bc590` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/9fc1f243-a | mp4 | h264 | 832x464 | mp4 | 7,244,696 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 83 | `b1f5a9c76ae0dda01be63e7c61bcdf5f` | projects/aafee923-5ba7-48f2-8a99-3da91bd54abb/auditions/85cd23f9-9 | mp4 | h264 | 1024x576 | mp4 | 7,090,234 | `f_mp4` | 2026-08-12 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 84 | `dd17b8eb5ce871b181cfbfaeb7e03fd9` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 720x1280 | mp4 | 6,654,465 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 85 | `8f44d38b6083bcd7f7a0a42ccdca5a96` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 6,134,078 | `f_mp4` | 2026-08-16 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 86 | `3ac0598aa0a1fd334bce5f7bf8ba0c89` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 5,457,493 | `f_mp4` | 2026-08-13 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 87 | `cd41e778f1a559bfa223f42fb5ff1a3d` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 5,406,695 | `f_mp4` | 2026-08-16 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 88 | `975ebd9ca2dc85936d2392c0ef310c61` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/9ea93dbe-1 | mp4 | h264 | 1280x720 | mp4 | 5,215,110 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 89 | `4d39703fe02b0bc9f0d4ff6457e7b860` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 2160x3840 | mp4 | 5,197,311 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 90 | `6c53ab2b116398905a6f66855d6165ac` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 816x704 | mp4 | 5,097,189 | `f_mp4` | 2026-08-15 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 91 | `5e7e978afbee64b80b5f78a3e8474915` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mov | h264 | 1920x1080 | mp4 | 4,784,155 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 92 | `cc8db576e519f5c0b2348188ceac07d4` | projects/3213d62c-57ef-4504-ab1e-722602191533/auditions/unknown_ta | mov | h264 | 1280x720 | mp4 | 4,605,314 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 93 | `17fc735d3ca6716ea7550d5123e10725` | projects/3213d62c-57ef-4504-ab1e-722602191533/auditions/unknown_ta | mov | h264 | 1008x1580 | mp4 | 4,052,285 | `f_mp4` | 2026-08-19 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 94 | `f7e2b2473a7cd257132f8dbc337ddbe1` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/unknown_ta | mp4 | h264 | 834x720 | mp4 | 3,883,416 | `f_mp4` | 2026-08-13 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 95 | `f50f5333febc649dc9e9a109689e7449` | projects/aafee923-5ba7-48f2-8a99-3da91bd54abb/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 3,878,744 | `f_mp4` | 2026-08-13 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 96 | `c4c81a2a654bb7ff2e6d70251da890ac` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 3,705,075 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 97 | `9af755d6a4e4cb880df5bdc4c37806c0` | projects/3213d62c-57ef-4504-ab1e-722602191533/auditions/unknown_ta | mov | h264 | 1280x720 | mp4 | 3,446,898 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 98 | `cf4130b7e84fd4c2c0bbc17576689365` | projects/f973d131-406f-46f9-b951-4c12ff392e94/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 3,401,104 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 99 | `28d3960e9684eb35701333b89effa5ca` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/6ba1b1a9-3 | mp4 | h264 | 720x1280 | mp4 | 3,089,693 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 100 | `b47e8e918e732114e654c8a107996ac1` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/f7b9ed14-a | mp4 | h264 | 1280x720 | mp4 | 3,064,379 | `f_mp4` | 2026-08-12 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 101 | `9c4dc8503b1390ad18f6523e0ebde78e` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 3,054,770 | `f_mp4` | 2026-08-13 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 102 | `58e808fadb97bdd2dcece4cefc6efa08` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 2,978,572 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 103 | `43139c6c57c6e72edc691745f5de59d2` | projects/aafee923-5ba7-48f2-8a99-3da91bd54abb/auditions/85cd23f9-9 | mp4 | h264 | 848x480 | mp4 | 2,916,326 | `f_mp4` | 2026-08-12 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 104 | `cf32101eb125a9f49070f870bc8910c3` | projects/07d25735-40b7-4c71-bb64-44374fa96aa3/auditions/unknown_ta | mp4 | h264 | 848x480 | mp4 | 2,848,677 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 105 | `03976a5df0a43dd1ed7f33af0b7a11a9` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 2,840,444 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 106 | `e360e3e103911a4ffab016da266bed4d` | projects/fecd1329-1b24-4997-8f8f-b805e1a9bdfd/auditions/unknown_ta | mp4 | h264 | 1280x720 | mp4 | 2,829,095 | `f_mp4` | 2026-08-10 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 107 | `0f4a210a4e8a69a95632a846472f4cc0` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 1080x1920 | mp4 | 2,743,904 | `f_mp4` | 2026-08-19 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 108 | `cc3ce1d95936e6ea8716f547e7b584bd` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/6ba1b1a9-3 | mov | h264 | 720x1280 | mp4 | 2,630,955 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 109 | `193928b3f0c387a339f2ddf85e25721e` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/6ba1b1a9-3 | mov | h264 | 882x1920 | mp4 | 2,576,117 | `f_mp4` | 2026-08-18 | project_submission | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 110 | `8cd6782abfc9b47c26a5f712ae367a0d` | projects/cb32a229-9e7a-42e4-8f58-ac71e8447ec6/auditions/unknown_ta | mp4 | h264 | 720x1280 | mp4 | 2,450,511 | `f_mp4` | 2026-08-17 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 111 | `59cf4c8c203bdeec0f54d957d6656c0b` | applications/6f005ea2-c46f-4813-b9a8-37a74b4b1e1c/intro_video | mp4 | h264 | 1174x720 | mp4 | 2,025,267 | `f_mp4` | 2026-08-15 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 112 | `75b476c6574fae7106174c46d71eebf4` | projects/8779dfca-886a-43a0-8993-34555713383b/auditions/unknown_ta | mov | h264 | 1280x720 | mp4 | 1,880,293 | `f_mp4` | 2026-08-13 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |
| 113 | `2bb90e52863f0a6939b341afbe184384` | projects/f1f25731-5d71-4b53-8213-f40dc39bbd01/auditions/unknown_ta | mov | h264 | 1024x576 | mp4 | 1,353,340 | `f_mp4` | 2026-08-18 | talent | no | 0 | no | retired download transcode | **DELETE_CANDIDATE** | medium |

---

## Proposed next step (for your review — no action taken)

If you later authorise a P9 batch against this bucket, the natural first slice is a
**small canary from the 68 `DELETE_CANDIDATE`** (e.g. the 10 smallest, all H.264, all
13 proofs green), executed through the exact same P9 machinery (fresh per-asset Layer-1
revalidation, one `delete_derived_resources([single_id])` per asset, immutable audit,
anomaly-stop, flag returns OFF). The 35 `REVIEW_LINKED_CANDIDATE` and 6 `UNKNOWN` stay frozen
pending an explicit decision on the review-link proof and an orphan-original review respectively.

## STOP

Analysis only. No `f_mp4` asset deleted. No deletion approval created. No P9 batch executed.
The frozen buckets from the P9 batches (remaining retired AVIF, 2,654 `LEGACY_DERIVED`, 468
`PROTECTED_HISTORICAL_DERIVED`) are untouched. Awaiting your review of this 113-row evidence.
