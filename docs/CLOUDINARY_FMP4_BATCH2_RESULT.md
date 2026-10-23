# f_mp4 Batch 2 — Execution Result (25 retired bare-`f_mp4` derived assets)

**Executed 2026-08-31** on your authorization of "ONE additional f_mp4 batch of EXACTLY 25
assets". **25 / 25 deleted. 0 blocked. 0 stale. 0 anomalies. No third f_mp4 batch. Flag OFF.**

Fresh manifest / hash / approval / batch — the f_mp4 canary approval was not reused.
Machine-readable: `docs/CLOUDINARY_FMP4_BATCH2_RESULT.json`.

| # | Item | Value |
|--:|---|---|
| **1** | manifest_id | `pm_5645ad40b835e64f3a29` |
| **2** | candidate_hash | `40922369f247787a5d657f4dad84c2a636d8a3252d8b8f3d34396776759b5936` (order-independent SHA-256 over exactly these 25 — new, distinct from the f_mp4 canary hash and every AVIF hash) |
| **3** | approval_id | `ap_821ac23afcddf44f3c22` (`approved_by = user-fmp4-batch2-authorization`, count 25, hash-pinned) |
| **4** | batch_id | `b_519efa200642013b9668` (`canary = false`, size 25, status `executed`) |

---

## 5. The exact 25 derived_ids

The 25 smallest still-eligible talent-owned bare-`f_mp4` `DELETE_CANDIDATE` (the 33-asset
remaining pool, minus the 10 canary). Every one: transformation **exactly bare `f_mp4`** ·
parent codec **H.264** (fetched live via `resource(media_metadata=True)`) ·
`video_needs_compat_delivery` = **False** · historical purpose = **retired `_get_video_download_url`
download transcode** · **not persisted** · `mongo_reference_count = 0` · P3 owner **talent**, no
conflict · manifest bytes **== live Cloudinary `derived.bytes`** (exact, all 25).

| # | derived_id | parent original | fmt/codec | bytes | live refs |
|--:|---|---|---|--:|:--:|
| 1 | `b47e8e918e732114e654c8a107996ac1` | `…/8779dfca…/auditions/f7b9ed14…_sharvari-kashid/…` | mp4 / h264 | 3 064 379 | 1 |
| 2 | `cf4130b7e84fd4c2c0bbc17576689365` | `…/f973d131…/auditions/unknown_talent_sniggy-chops/…` | mp4 / h264 | 3 401 104 | 2 |
| 3 | `9af755d6a4e4cb880df5bdc4c37806c0` | `…/3213d62c…/auditions/unknown_talent/…` | mov / h264 | 3 446 898 | 2 |
| 4 | `c4c81a2a654bb7ff2e6d70251da890ac` | `…/07d25735…/auditions/unknown_talent/…` | mp4 / h264 | 3 705 075 | 2 |
| 5 | `f50f5333febc649dc9e9a109689e7449` | `…/aafee923…/auditions/unknown_talent/…` | mp4 / h264 | 3 878 744 | 2 |
| 6 | `17fc735d3ca6716ea7550d5123e10725` | `…/3213d62c…/auditions/unknown_talent/…` | mov / h264 | 4 052 285 | 2 |
| 7 | `cc8db576e519f5c0b2348188ceac07d4` | `…/3213d62c…/auditions/unknown_talent/…` | mov / h264 | 4 605 314 | 2 |
| 8 | `5e7e978afbee64b80b5f78a3e8474915` | `…/07d25735…/auditions/unknown_talent/…` | mov / h264 | 4 784 155 | 3 |
| 9 | `6c53ab2b116398905a6f66855d6165ac` | `…/f973d131…/auditions/unknown_talent/…` | mp4 / h264 | 5 097 189 | 1 |
| 10 | `975ebd9ca2dc85936d2392c0ef310c61` | `…/fecd1329…/auditions/9ea93dbe…/…` | mp4 / h264 | 5 215 110 | 2 |
| 11 | `cd41e778f1a559bfa223f42fb5ff1a3d` | `…/f973d131…/auditions/unknown_talent/…` | mp4 / h264 | 5 406 695 | 1 |
| 12 | `8f44d38b6083bcd7f7a0a42ccdca5a96` | `…/f973d131…/auditions/unknown_talent/…` | mp4 / h264 | 6 134 078 | 1 |
| 13 | `dd17b8eb5ce871b181cfbfaeb7e03fd9` | `…/fecd1329…/auditions/unknown_talent/…` | mp4 / h264 | 6 654 465 | 1 |
| 14 | `3945703b4060ee8025878bf8717bc590` | `…/07d25735…/auditions/9fc1f243…/…` | mp4 / h264 | 7 244 696 | 1 |
| 15 | `c0f0d9277fc80b0f1c46448f2a309c67` | `…/07d25735…/auditions/unknown_talent/…` | mov / h264 | 7 594 871 | 1 |
| 16 | `8103c6f57ad5e6c85a282394ee903c5e` | `…/fecd1329…/auditions/unknown_talent/…` | mp4 / h264 | 8 100 315 | 1 |
| 17 | `a981026dec7254d5740d54989c7be5e6` | `…/f973d131…/auditions/unknown_talent/…` | mp4 / h264 | 8 308 655 | 1 |
| 18 | `e07ea51987af3ba6e455287cd77c46e7` | `…/07d25735…/auditions/unknown_talent/…` | mp4 / h264 | 9 811 410 | 2 |
| 19 | `2fd838b16610d8acb1e440b50fa005d5` | `…/f1f25731…/auditions/unknown_talent/…` | mov / h264 | 10 742 977 | 2 |
| 20 | `fbba1b5db2bb56980fa3a54b23637cbb` | `…/07d25735…/auditions/3527853d…/…` | mp4 / h264 | 11 277 421 | 1 |
| 21 | `cb30360ab928b81e40681503f8d2538b` | `…/07d25735…/auditions/unknown_talent/…` | mp4 / h264 | 11 310 518 | 1 |
| 22 | `3727771efb8ef56dd0b4577738cea184` | `…/f1f25731…/auditions/dfd73164…/…` | mov / h264 | 11 772 187 | 1 |
| 23 | `acf9b1719d6232224cabdba11edcea3b` | `…/ac346fe5…/auditions/2720f5e4…/…` | mp4 / h264 | 12 121 098 | 1 |
| 24 | `c576fe394fe368197c7bdbe6b93f308e` | `…/07d25735…/auditions/unknown_talent/…` | mov / h264 | 14 023 164 | 2 |
| 25 | `dffda2516e22d6ef689b6ca90738b141` | `…/fecd1329…/auditions/unknown_talent/…` | mp4 / h264 | 14 286 012 | 1 |

Total: **186,038,815 bytes (≈ 177.4 MB)**. Full parent paths, all 18 pre-check booleans, and the
P9 Layer-1 checks per asset are in the JSON.

## 6. Attempted count — **25**

## 7. Deleted count — **25**  (`skipped 0 · blocked 0 · stopped false · stop_reason null`)

## 8. Blocked count — **0**

Two independent revalidation passes, both green for all 25:
- **Explicit 18-point pre-execution check** (before any P9 call): 25 / 25 `ALL_PASS`.
- **P9 Layer-1** (`build_purge_manifest` + again per-asset inside `execute_batch`):
  `by_verdict: {"PASS": 25}`.

## 9. Stale count — **0**

## 10. Per-asset deletion result

25 × `cloudinary.api.delete_derived_resources([<single id>])` — **one id per call, 25 calls**
(verified: `delete_calls_made` is 25 single-element lists). Each returned
`{"deleted": {<id>: "deleted"}}` → `_response_ok = ok`. No partial / ambiguous / unknown.

## 11. Parent survival — **25 / 25**

Verified 3×: Layer 3's own post-delete check, the execution script's post-loop, and a standalone
independent pass (`parents_alive 25/25`).

## 12. MongoDB integrity

**No MongoDB document created, modified, or deleted.** All 25 parent originals still referenced
in `talents` / `submissions` / `applications` (`mongo_refs_intact 25/25`). Purge collections
gained only append-only rows (1 manifest, 1 approval, 1 batch, 25 audit).

## 13. Protected-group verification (full re-scan, not sampled)

| Bucket | Still present |
|---|---|
| **4 CURRENT_COMPATIBLE** (HEVC) | **4 / 4** |
| **6 UNKNOWN** | **6 / 6** |
| **35 REVIEW_LINKED_CANDIDATE** | **35 / 35** |

## 14. Previous 95 deletion verification

All prior P9 deletions remain deleted — 85 re-confirmed by audit cross-reference here
(the AVIF canary's 10 use a different result-key and were confirmed in their own runs); every
matched audit record shows `deletion_result = deleted`.

## 15. Unexpected deletion count — **0**

Exactly the 25 approved derived ids removed; every parent survived; every protected bucket intact;
prior 95 still deleted.

## 16. Transformation count — **0**

`usage().transformations.usage` = **71,252 before and after**.

## 17. Upload count — **0**

## 18. Confirmed storage bytes removed

- **Expected:** 186,038,815 bytes.
- **Confirmed (authoritative):** 186,038,815 — all 25 derived resources gone from their parents'
  live `resource(derived=True)` listing (`derived_gone 25/25`), manifest bytes matched live
  Cloudinary bytes exactly for all 25.

## 19. `usage()` state

| | storage bytes | derived objects | transformations |
|---|---:|---:|---:|
| before f_mp4 Batch 2 | 19,604,077,694 | 8,416 | 71,252 |
| after f_mp4 Batch 2 (immediate) | 19,604,077,694 | 8,416 | 71,252 |

- **Freshness:** `usage()` has now **caught up through the f_mp4 canary** — `derived` is **8,416**
  = 8,511 (P8.5 baseline) − 95 (all P9 deletions through the f_mp4 canary). Storage is down
  ~25.55 GB→ (19.629 → 19.604 GB, −25.17 MB) reflecting Batch 3 + f_mp4 canary.
- **Batch 2's 25 not yet reflected** — the async refresh has not run since. Authoritative
  per-parent `resource(derived=True)`: **8,416 → 8,391 derived, −186,038,815 bytes** once
  `usage()` catches up.

## 20. Audit-log count

**25** new immutable records for batch `b_519efa200642013b9668` (`deletion_result = deleted`,
`revalidation_result = PASS`, `canary = false`, `classification = DELETE_CANDIDATE`,
`actor = user-fmp4-batch2-authorization`, `dry_run = false`, full asset + manifest + approval +
batch fields). **No secrets** (asserted). Total audit records now **120** (10 + 25 + 50 AVIF +
10 + 25 f_mp4).

## 21. Anomaly status — **none**

`stopped: false`, `stop_reason: null`. No identity mismatch, no unexpected reference, no missing
parent, no ownership change, no persisted-URL discovery, no ambiguous Cloudinary response, no
post-delete verification failure, no unexpected MongoDB change.

## 22. Final physical-delete flag state — **OFF**

Set in the execution process only (Railway service env never touched — pre-run reading `None`),
asserted `True` during the run, `pop`-ped + asserted `False` in a `finally` block, re-verified
unset by the standalone pass.

## 23. Confirmation no further batch ran

**Confirmed.** `db.purge_batches` contains **five** batches — `b_924a1600…` (AVIF canary),
`b_60dee7a7…` (AVIF B2), `b_5e239bb6…` (AVIF B3), `b_e4029492…` (f_mp4 canary),
`b_519efa20…` (this f_mp4 Batch 2) — all `executed`. `create_batch` called once this run; a
non-canary batch never auto-continues. No third f_mp4 batch created or executed.

---

## Purge-collection state

| Collection | Count |
|---|---:|
| `purge_manifests` | 6 |
| `purge_approvals` | 5 |
| `purge_batches` | 5 (all `executed`) |
| `purge_audit_log` | 120 |

## P9 running total (all families)

| Family | Batches | Derived deleted | Bytes |
|---|---|--:|--:|
| retired AVIF | canary 10 + 25 + 50 | 85 | 1,316,213 |
| retired bare `f_mp4` | canary 10 + 25 | 35 | 211,216,110 |
| **Total** | **5** | **120** | **212,532,323 (~202.7 MB)** |

## Frozen — untouched by this batch

- **8 talent-owned `f_mp4` `DELETE_CANDIDATE`** not in this batch + **25 `project_submission`-owned**
  (held by P9's own `project_submission → RETENTION_BLOCKED` rule) = **33 remaining f_mp4
  candidates**
- **4 `CURRENT_COMPATIBLE`** HEVC (verified 4/4 present)
- **6 `UNKNOWN`** (verified 6/6 present)
- **35 `REVIEW_LINKED_CANDIDATE`** (verified 35/35 present)
- 2,654 `LEGACY_DERIVED`, 468 `PROTECTED_HISTORICAL_DERIVED`, all remaining retired AVIF

## STOP

f_mp4 Batch 2 is complete. **No third f_mp4 batch is authorized.** Physical deletion is disabled
again (`MEDIA_LIFECYCLE_PHYSICAL_DELETE` unset). No automatic progression. Awaiting your review.
