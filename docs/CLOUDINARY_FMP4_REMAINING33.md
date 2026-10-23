# f_mp4 — Remaining 33 Analysis (8 talent-owned + 25 project-submission)

**READ-ONLY. Executed 2026-08-31.** Zero Cloudinary writes / deletes / transformations / uploads.
Zero MongoDB writes. **Nothing was deleted. No approval or batch was created. No execution.**

Machine-readable: `docs/CLOUDINARY_FMP4_REMAINING33.json`.

After the f_mp4 canary (10) + Batch 2 (25), **33 f_mp4 `DELETE_CANDIDATE` remain**: 8 talent-owned
(P9-eligible) + 25 `project_submission`-owned (P9 `RETENTION_BLOCKED`).

---

## A. The 8 talent-owned f_mp4 candidates

**All 8 pass every check.** Fresh per-asset revalidation — explicit 18-point + P9 Layer-1
(`revalidate_candidate`) — returned **`{"PASS": 8}`**.

| # | derived_id | bytes | parent fmt / codec | parent created | talent_id | live refs | 18-pt | P9 Layer-1 |
|--:|---|--:|---|---|---|:--:|:--:|:--:|
| 1 | `43cb35ba043fa3b695e548e2ecbb22f8` | 16 869 871 | mp4 / h264 | 2026-08-15 | `b6fd65be…` | 1 | PASS | PASS |
| 2 | `58b9fc6e1f17ff68b20c0c9b6d107080` | 18 001 880 | mov / h264 | 2026-08-18 | `fddaaede…` | 1 | PASS | PASS |
| 3 | `7c4cfbe0a50de34608fe978c1ebdab31` | 22 082 541 | mp4 / h264 | 2026-08-15 | `f91653a6…` | 1 | PASS | PASS |
| 4 | `1e50a8101c9aa42486476f14323467dc` | 22 120 672 | mov / h264 | 2026-08-18 | `6a2c086c…` | 2 | PASS | PASS |
| 5 | `824d30c9de9ddd12c42c894639cc6722` | 23 729 733 | mov / h264 | 2026-08-19 | `41e0c413…` | 3 | PASS | PASS |
| 6 | `7741c306abfcfc743dcc40e69b0c5e2a` | 24 937 206 | mp4 / h264 | 2026-08-13 | `e9fe1a0c…` | 1 | PASS | PASS |
| 7 | `078452c33ab95f551b340e2921ac0d6b` | 26 071 903 | mov / h264 | 2026-08-19 | `eea527f5…` | 2 | PASS | PASS |
| 8 | `dd4d98f1c56cc6217114a463293aaa82` | 88 817 141 | mov / h264 | 2026-08-19 | `1e342b65…` | 2 | PASS | PASS |

Total: **242,630,947 bytes (≈ 231.4 MB)**. Parent full paths in the JSON — all are
`talentgram/projects/…/auditions/…/submission_…/intro_video`; P3 ownership resolves to **talent**
(the media was promoted to the global talent profile — folder path is never consulted for
ownership).

### Safety evidence — identical `true` for all 8

| Check | Result (all 8) |
|---|---|
| transformation = exactly bare `f_mp4` | ✅ (live Cloudinary `derived.transformation == "f_mp4"`, format `mp4`) |
| historical purpose | retired `_get_video_download_url` download transcode (removed in P5) |
| parent codec = H.264 | ✅ (`resource(media_metadata=True).codec == "h264"`) |
| `video_needs_compat_delivery()` = FALSE | ✅ (unmodified production fn) |
| parent original exists | ✅ |
| parent active | ✅ (`parent_live_refs ≥ 1`, no soft-deleted-only) |
| P3 `owner_type` = talent | ✅ (no conflict, single owner_type) |
| talent doc exists / not archived / not deleted | ✅ (`lifecycle_state` is `None` for all 8) |
| no persisted derived URL | ✅ (exact URL, `(parent, f_mp4)` variant, `/f_mp4/` scan all negative) |
| Mongo derivative reference count = 0 | ✅ |
| no active review-link dependency | ✅ (talent_id ∉ link_talent_ids, submission_id ∉ link_submission_ids) |
| no application dependency | ✅ |
| no historical (source-lineage) dependency | ✅ |
| no ledger dependency | ✅ (not in `pending_media_deletions`) |
| no soft-deleted-container dependency | ✅ |
| retention satisfied | ✅ (global talent media — no audition retention hold) |
| exact Cloudinary identity | ✅ (derived id present on parent, xf + format match) |
| exact bytes | ✅ (`bytes_manifest == bytes_cloudinary` for all 8) |
| current application cannot require this derivative | ✅ (compat = FALSE; bare `f_mp4` no longer generated for a web-safe H.264 parent) |

### Proposed manifest (IN MEMORY ONLY — NOT EXECUTED)

| Item | Value |
|---|---|
| **proposed candidate_hash** | `b0740832807542535084d2c5afd4b2160a4d5b02adb5c2245e574788f2660727` — order-independent SHA-256 of exactly these 8 derived_ids. **This is the durable identifier to review/approve.** |
| **manifest_id** (advisory) | `pm_af033bc7cace7c6bcb87` — computed by `build_purge_manifest(persist=False)`; **not written to MongoDB**. A fresh manifest_id is minted at execution time (its hash includes a timestamp), exactly as for every prior batch; the candidate_hash is what stays fixed. |
| **approval_id** | **NOT CREATED** — created inside the authorized execution run, immediately before deletion, gated by `MEDIA_LIFECYCLE_PHYSICAL_DELETE`. |
| **batch_id** | **NOT CREATED** — same. |
| **P9 verdict** | `{"PASS": 8}` |
| **status** | **NOT EXECUTED.** No physical deletion. No approval. No batch. Flag OFF (never enabled in this task). |

---

## B. The 25 project-submission f_mp4 candidates (RETENTION_BLOCKED)

Total: **329,363,427 bytes (≈ 314.1 MB)**. `audition_retention_days` is **absent from
`db.app_config` → resolves to the 30-day default**.

| # | derived_id | bytes | project | submission | parent take, created |
|--:|---|--:|---|---|---|
| 1 | `193928b3f0c387a339f2ddf85e25721e` | 2 576 117 | `f1f25731…` | `38b85761…` | `take_baaa7339` · 2026-08-18 |
| 2 | `cc3ce1d95936e6ea8716f547e7b584bd` | 2 630 955 | `f1f25731…` | `38b85761…` | `take_9e11bee9` · 2026-08-18 |
| 3 | `0f4a210a4e8a69a95632a846472f4cc0` | 2 743 904 | `f1f25731…` | `0ac79754…` | `take_e1bca4d3` · 2026-08-19 |
| 4 | `28d3960e9684eb35701333b89effa5ca` | 3 089 693 | `f1f25731…` | `38b85761…` | `take_07374c5d` · 2026-08-18 |
| 5 | `f7e2b2473a7cd257132f8dbc337ddbe1` | 3 883 416 | `8779dfca…` | `2e18ba63…` | `take_23e86d50` · 2026-08-13 |
| 6 | `4d39703fe02b0bc9f0d4ff6457e7b860` | 5 197 311 | `f1f25731…` | `57e07ca6…` | `take_409cb195` · 2026-08-18 |
| 7 | `3ac0598aa0a1fd334bce5f7bf8ba0c89` | 5 457 493 | `8779dfca…` | `f3899bc6…` | `take_ee35466e` · 2026-08-13 |
| 8 | `b1f5a9c76ae0dda01be63e7c61bcdf5f` | 7 090 234 | `aafee923…` | `4b9efb59…` | `take_0c386c9c` · 2026-08-12 |
| 9 | `3fe1984270637ccfdd12ed196f14bc31` | 7 247 734 | `f973d131…` | `c72a5ee2…` | `take_41cce11e` · 2026-08-17 |
| 10 | `fea0513c7d2dfbe8ee158340b1f0d185` | 7 400 048 | `8779dfca…` | `32b21208…` | `take_76c58c97` · 2026-08-12 |
| 11 | `9a51c205317c68a3a9f4af2af0a9156d` | 8 392 872 | `8779dfca…` | `de79c988…` | `take_882defdd` · 2026-08-12 |
| 12 | `3f86f997ee9a96a0c440834f12bab51f` | 8 548 784 | `8779dfca…` | `32b21208…` | `take_d1a55149` · 2026-08-12 |
| 13 | `2133589a6f8efe842b7018c00eca056c` | 9 303 468 | `f973d131…` | `8aacaf50…` | `take_58cc068c` · 2026-08-17 |
| 14 | `243b541de805f10553231de7778ec1fb` | 9 424 242 | `f1f25731…` | `57e07ca6…` | `take_4fa11a42` · 2026-08-18 |
| 15 | `9aadcfa427aa2a784e29d6acba198a54` | 9 538 834 | `f973d131…` | `72f420f0…` | `take_62c805d1` · 2026-08-15 |
| 16 | `353e47d94698cbbb12b79c2dbaf6a146` | 12 043 468 | `f1f25731…` | `0ac79754…` | `take_be04d209` · 2026-08-19 |
| 17 | `59b68d444db6e333e86b162f3d667628` | 12 404 014 | `f973d131…` | `546c1e64…` | `take_d645e467` · 2026-08-15 |
| 18 | `7a2532386e3404d3a9c0268ff640378b` | 12 790 420 | `f973d131…` | `2db6629e…` | `take_34764abd` · 2026-08-15 |
| 19 | `4a4265cc22471584918824fb69bb2f7e` | 16 228 284 | `f1f25731…` | `5e5e736f…` | `take_725a929c` · 2026-08-18 |
| 20 | `e6a1b8928d2456c7c309d1385908f24f` | 16 914 652 | `f1f25731…` | `57e07ca6…` | `take_833ea59f` · 2026-08-18 |
| 21 | `97f7ab836f6aa82173af11dd072e89d7` | 20 618 413 | `f1f25731…` | `57e07ca6…` | `take_30eba247` · 2026-08-18 |
| 22 | `9189b68efc6c4a2a9617cb71882e58a2` | 22 308 852 | `f1f25731…` | `57e07ca6…` | `take_779a9f01` · 2026-08-18 |
| 23 | `4a0f34bf0aead81ba426a5d03779d481` | 25 001 150 | `f1f25731…` | `5e5e736f…` | `take_d492e2f2` · 2026-08-18 |
| 24 | `7ce1fb567801e9800e81ab699245696d` | 48 711 268 | `aaa3e712…` | `7ddffd83…` | `take_d1be1079` · 2026-08-19 |
| 25 | `6b66b275923f7f5078c576403cc3a21e` | 49 817 801 | `aaa3e712…` | `7ddffd83…` | `take_8158b3e3` · 2026-08-19 |

Spread over **5 projects · 15 submissions**. Parents created 2026-08-12 → 2026-08-19, all
`take_*` audition takes.

### Per-asset status — identical for all 25

| Field | Value (all 25) |
|---|---|
| owner_type | `project_submission` |
| retention_days configured | **30** (`audition_retention_days` absent → default) |
| submission exists / soft-deleted | exists · **not** soft-deleted (`lifecycle_state = null`) |
| project exists / soft-deleted | exists · **not** soft-deleted (`lifecycle_state = null`) |
| media lifecycle state | `null` (not marked pending) |
| in `pending_media_deletions` ledger | **no** |
| persisted derived URL | **no** |
| other dependency | none found |
| retention expiry date | **n/a — clock not started** (needs a soft-delete first) |
| current retention status | **BLOCKED — live audition parent** |

### Exact reason each of the 25 is RETENTION_BLOCKED

`cloudinary_controlled_purge.revalidate_candidate` contains a **categorical** rule
(`backend/cloudinary_controlled_purge.py`, the `owner_type == "project_submission"` branch):

> a live audition parent → the parent is in use → derivative not deletable → `RETENTION_BLOCKED`

This is **not a timer that expires**. Every one of the 25 parent originals is a **live,
actively-referenced project-audition take** (submission + project both present and not
soft-deleted). While that is true, the derivative is never P9-eligible — regardless of the
`audition_retention_days` value.

The 30-day `audition_retention_days` window governs a **different** path: when an audition
submission is *soft-deleted*, `media_lifecycle.delete_if_safe` waits `retention_days` after the
teardown before the **parent media** can be physically purged (via the
`pending_media_deletions` ledger). It does not create an eligibility date for a *derivative of a
live audition*.

### Earliest possible eligibility

**None while the parent audition is live.** A derivative here could become P9-eligible only if
**all** of:

1. the parent submission (or its project) is soft-deleted, **and**
2. ≥ 30 days elapse from that soft-delete (`audition_retention_days`), **and**
3. every remaining Mongo reference to the parent is gone,

at which point `media_lifecycle` would tear down the **parent** — the derivative would follow it.
A P9 batch that deletes these *derivatives while keeping the live parent audition* would require
an explicit policy decision to relax the categorical `project_submission` block; that is not
proposed here.

---

## C. Zero-mutation confirmation

| | |
|---|---|
| Cloudinary writes | **0** |
| Cloudinary deletes | **0** |
| transformations generated | **0** (`usage().transformations` = 71,252 before & after) |
| uploads | **0** (`usage().derived` = 8,416 unchanged) |
| MongoDB writes | **0** — every DB call is `find` / `count_documents`; `build_purge_manifest` run with `persist=False` |

API calls (all read-only): `cloudinary.api.resource(media_metadata=True)` ×41 · `usage()` ×1 ·
Mongo reads ×88.

## Protected — untouched and unchanged

4 `CURRENT_COMPATIBLE` HEVC · 6 `UNKNOWN` · 35 `REVIEW_LINKED_CANDIDATE` · 2,654 `LEGACY_DERIVED`
· 468 `PROTECTED_HISTORICAL_DERIVED` · all remaining retired AVIF.

## STOP

Analysis only. No physical deletion. No approval or batch created. No f_mp4 batch executed.
`MEDIA_LIFECYCLE_PHYSICAL_DELETE` never enabled in this task. Awaiting your explicit approval of
the 8 talent-owned f_mp4 assets (candidate_hash
`b0740832807542535084d2c5afd4b2160a4d5b02adb5c2245e574788f2660727`) before any execution.
