# f_mp4 Canary — Execution Result (10 retired bare-`f_mp4` derived assets)

**Executed 2026-08-31** on your authorization of "f_mp4 CANARY — EXACTLY 10 ASSETS … the safest
f_mp4 DELETE_CANDIDATE assets". **10 / 10 deleted. 0 blocked. 0 stale. 0 anomalies. No second
f_mp4 batch. Flag OFF.**

This is the **first deletion from the `f_mp4` family** — a fresh canary for a new transformation
family, using a fresh manifest / approval / hash. Machine-readable:
`docs/CLOUDINARY_FMP4_CANARY_RESULT.json`.

The P9 engine was extended first ([fb6b78f](../commit/fb6b78f)) to parametrize the canary
transformation family (`canary_family="avif" | "f_mp4"`); the f_mp4 family admits **only**
exactly-bare `f_mp4` (rejects `w_`/`h_`/`c_limit`/`vc_auto`/`fl_attachment`/`dpr_` — the 720p and
download-with-filename families). 36 P9 tests pass (34 existing + 2 new).

| # | Item | Value |
|--:|---|---|
| **1** | manifest_id | `pm_ca2356226719a2d825fe` |
| **2** | candidate_hash | `79097bc3fbd2685c3e6cf8d2e84fe1067ecc5516cb6f073079ba7455df25ed65` (order-independent SHA-256 over exactly these 10 ids — new, distinct from every AVIF-batch hash) |
| **3** | approval_id | `ap_5c6f1a244614b55e5929` (`approved_by = user-fmp4-canary-authorization`, count 10, hash-pinned) |
| **4** | batch_id | `b_e40294922c32097b2296` (`canary = true`, `canary_family = "f_mp4"`, size 10, status `executed`) |

---

## 5–13. The exact 10 · parents · codec · P4 compat · historical purpose · persisted · Mongo refs · Layer-1 · deletion

Every one: transformation **exactly bare `f_mp4`** · parent codec **H.264** (fetched live via
`resource(media_metadata=True)`) · `core.video_needs_compat_delivery(fmt, codec)` = **False** ·
historical purpose = **retired `_get_video_download_url` download transcode** (removed in P5) ·
derived URL **not persisted** anywhere · `mongo_reference_count = 0` · P3 owner = **talent**, no
conflict · fresh P9 Layer-1 = **PASS** · manifest bytes **== live Cloudinary `derived.bytes`**
(exact, all 10).

| # | derived_id | parent original | p.fmt / codec | bytes | owner docs / live refs | Layer-1 | result |
|--:|---|---|---|--:|:--:|:--:|:--:|
| 1 | `2bb90e52863f0a6939b341afbe184384` | `…/unknown_talent_twinkle-dhaifule/submission_57e07ca6…/intro_video` | mov / h264 | 1 353 340 | 2 / 2 | PASS | deleted |
| 2 | `75b476c6574fae7106174c46d71eebf4` | `…/unknown_talent_ishani-bhola/submission_2e18ba63…/intro_video` | mov / h264 | 1 880 293 | 1 / 1 | PASS | deleted |
| 3 | `59cf4c8c203bdeec0f54d957d6656c0b` | `talentgram/applications/6f005ea2…/intro_video` | mp4 / h264 | 2 025 267 | 2 / 2 | PASS | deleted |
| 4 | `8cd6782abfc9b47c26a5f712ae367a0d` | `…/unknown_talent/submission_eaff552e…/intro_video` | mp4 / h264 | 2 450 511 | 1 / 1 | PASS | deleted |
| 5 | `e360e3e103911a4ffab016da266bed4d` | `…/unknown_talent_bishakha-thapa/submission_ee351d07…/intro_video` | mp4 / h264 | 2 829 095 | 2 / 2 | PASS | deleted |
| 6 | `03976a5df0a43dd1ed7f33af0b7a11a9` | `…/unknown_talent_manya-grover/submission_3877b22d…/intro_video` | mp4 / h264 | 2 840 444 | 1 / 1 | PASS | deleted |
| 7 | `cf32101eb125a9f49070f870bc8910c3` | `…/unknown_talent_drishita/submission_37c25a5b…/intro_video` | mp4 / h264 | 2 848 677 | 2 / 2 | PASS | deleted |
| 8 | `43139c6c57c6e72edc691745f5de59d2` | `…/85cd23f9…_padm-rautela/submission_4b9efb59…/intro_video` | mp4 / h264 | 2 916 326 | 1 / 1 | PASS | deleted |
| 9 | `58e808fadb97bdd2dcece4cefc6efa08` | `…/unknown_talent_hasnain-siddique/submission_856a598c…/intro_video` | mp4 / h264 | 2 978 572 | 2 / 2 | PASS | deleted |
| 10 | `9c4dc8503b1390ad18f6523e0ebde78e` | `…/unknown_talent_rishidha-katna/submission_f3899bc6…/intro_video` | mp4 / h264 | 3 054 770 | 1 / 1 | PASS | deleted |

Total: **25,177,295 bytes (≈ 24.0 MB)**. The 10 smallest of the 43 talent-owned f_mp4
`DELETE_CANDIDATE` (the 25 `project_submission`-owned ones are correctly held by P9's own
`project_submission → RETENTION_BLOCKED` rule and were never in scope).

### Two independent revalidation passes, both green for all 10

- **Explicit 18-point pre-execution check** (my script, before any P9 call): `parent_exists`,
  `derived_present`, `cld_public_id_match`, live `parent_codec == h264`,
  `video_needs_compat_delivery == False`, `P3_owner_type == talent`, no conflict, `parent_active`,
  `persisted == false`, `mongo_ref_count == 0`, `review_link_talent == false`,
  `review_link_submission == false`, `source_lineage == false`, `ledger == false`, not in a
  soft-deleted submission/project, `retention_ok`, transformation `f_mp4`, format `mp4`, bytes
  within 5 %. **10 / 10 ALL_PASS.**
- **P9 Layer-1** (`revalidate_candidate`, run in `build_purge_manifest` and again inside
  `execute_batch` immediately before each delete): **`by_verdict: {"PASS": 10}`**.

## 13. Deletion result per asset

10 × `cloudinary.api.delete_derived_resources([<single id>])` — **one id per call, 10 calls**
(verified: `delete_calls_made` is 10 single-element lists). Each returned
`{"deleted": {<id>: "deleted"}}` → `_response_ok = ok`. No partial / ambiguous / unknown.

## 14. Parent survival

**10 / 10 parent originals survive** — verified in Layer 3's own post-delete check, the
execution script's post-loop, and a **standalone independent** pass (`parents_alive 10/10`).

## 15. MongoDB integrity

**No MongoDB document created, modified, or deleted by this canary.** The 10 derived assets were
never referenced in MongoDB. Standalone check: all 10 parent originals are **still referenced**
in `talents` / `submissions` / `applications` (`parents still referenced in Mongo 10/10`). The
purge collections gained only append-only rows (1 manifest, 1 approval, 1 batch, 10 audit).

## 16. Unexpected deletion count

**0.** Exactly the 10 approved derived ids were removed. Every parent survived. Full re-scan of
the frozen buckets:

| Bucket | Still present on Cloudinary |
|---|---|
| **4 CURRENT_COMPATIBLE** (HEVC) | **4 / 4** |
| **6 UNKNOWN** | **6 / 6** |
| **35 REVIEW_LINKED_CANDIDATE** | **35 / 35** |

The previous 85 P9 AVIF deletions remain deleted (75 re-confirmed by audit cross-reference here;
all 85 confirmed in their own batch verifications). Nothing else changed.

## 17. Transformations generated

**0.** `usage().transformations.usage` = **71,252 before and after**. Deleting a derived asset
generates no transformation; none was regenerated in the window.

## 18. Uploads generated

**0.**

## 19. Storage bytes confirmed removed

- **Expected:** 25,177,295 bytes.
- **Confirmed (authoritative):** 25,177,295 — all 10 derived resources gone from their parents'
  live `resource(derived=True)` listing (`derived_gone 10/10`), and manifest bytes matched live
  Cloudinary bytes exactly for all 10.
- **`usage()`:** `storage` / `derived` (8,476) / `transformations` (71,252) **unchanged** in the
  immediate post-run snapshot. Per your instruction this is **not treated as a failure** —
  Cloudinary's usage API refreshes asynchronously. It will reflect this canary (and Batch 3's
  50) on a later refresh cycle; the per-parent verification is the source of truth.

## 20. Audit-log count

`db.purge_audit_log`: **10 new immutable records** for batch `b_e40294922c32097b2296`
(`deletion_result = deleted`, `revalidation_result = PASS`, `canary = true`,
`classification = DELETE_CANDIDATE`, `actor = user-fmp4-canary-authorization`, `dry_run = false`,
full `public_id` / `parent_public_id` / `derived_id` / `resource_type` / `format` / `bytes` /
`transformation` / `ownership` / `reference_count` / `retention` / `cloudinary_response_summary`
/ `timestamp`). **No secrets** in any record (asserted). Total audit records now **95**
(10 + 25 + 50 AVIF + 10 f_mp4).

## 21. Final physical-delete flag state

**OFF.** `MEDIA_LIFECYCLE_PHYSICAL_DELETE` was set **in the execution process only** (Railway
service env never touched — pre-run reading `None`), asserted `True` during the run, `pop`-ped +
asserted `False` in a `finally` block, and re-verified unset (`enabled? False`) by the standalone
pass.

## 22. Confirmation that no second batch ran

**Confirmed.** `db.purge_batches` contains **four** batches — `b_924a1600…` (AVIF canary),
`b_60dee7a7…` (AVIF Batch 2), `b_5e239bb6…` (AVIF Batch 3), `b_e4029492…` (this f_mp4 canary) —
all `executed`. `create_batch` was called once this run; a canary batch never auto-continues.
No second f_mp4 batch was created or executed.

---

## Anomaly status

**No anomaly.** `stopped: false`, `stop_reason: null`. No identity mismatch, no unexpected
reference, no missing parent, no ownership change, no persisted-URL discovery, no ambiguous
Cloudinary response, no post-delete verification failure, no unexpected MongoDB change.

## Purge-collection state after the f_mp4 canary

| Collection | Count |
|---|---:|
| `purge_manifests` | 5 |
| `purge_approvals` | 4 |
| `purge_batches` | 4 (all `executed`) |
| `purge_audit_log` | 95 |

## P9 running total (all families)

| Family | Batches | Derived deleted | Bytes |
|---|---|--:|--:|
| retired AVIF (`f_avif,q_auto`) | canary 10 + 25 + 50 | 85 | 1,316,213 |
| retired bare `f_mp4` (download transcode) | canary 10 | 10 | 25,177,295 |
| **Total** | **4** | **95** | **26,493,508 (~25.3 MB)** |

## Frozen — untouched by this canary

- **4 `CURRENT_COMPATIBLE`** HEVC compat derivatives (142.6 MB) — verified 4/4 present
- **6 `UNKNOWN`** orphaned-parent f_mp4 (62.5 MB) — verified 6/6 present
- **35 `REVIEW_LINKED_CANDIDATE`** (524.9 MB) — verified 35/35 present
- the remaining **58 `DELETE_CANDIDATE`** f_mp4 (43 talent-owned − 10 + 25 project_submission)
- 2,654 `LEGACY_DERIVED`, 468 `PROTECTED_HISTORICAL_DERIVED`, all remaining retired AVIF

## STOP

The f_mp4 canary is complete. **No second f_mp4 batch is authorized.** Physical deletion is
disabled again (`MEDIA_LIFECYCLE_PHYSICAL_DELETE` unset). No automatic progression. Awaiting your
review of these canary results before any further deletion.
