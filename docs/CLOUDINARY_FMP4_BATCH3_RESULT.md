# f_mp4 Batch 3 — Execution Result (8 authorized talent-owned f_mp4 assets)

**Executed 2026-08-31** on your explicit authorization of exactly these 8 derived ids
(candidate_hash `b0740832807542535084d2c5afd4b2160a4d5b02adb5c2245e574788f2660727`).
**8 / 8 deleted. 0 blocked. 0 stale. 0 anomalies. No further batch. Flag OFF.**

Machine-readable: `docs/CLOUDINARY_FMP4_BATCH3_RESULT.json`.

> **Note — first attempt aborted safely.** An initial run of this batch **stopped at the
> pre-execution check with 0/8 passing and deleted nothing** (no manifest / approval / batch
> created, flag never enabled). The cause was **three bugs in the execution script's own
> verification wrapper**, not any problem with the assets: (1) the talent-doc lookup used a
> projection that returned `{}` when `lifecycle_state` was absent → false "talent not found";
> (2)–(3) two diagnostic strings were stored under check keys and treated as required booleans.
> The script's fail-closed design held — it substituted nothing and enabled nothing. Fixed, the
> re-run passed 8/8 (talent docs independently confirmed: all 8 exist, `status = SUBMITTED`, no
> `archived_at` / `is_deleted` / `lifecycle_state`).

| # | Item | Value |
|--:|---|---|
| **1** | execution manifest_id | `pm_727c8e218ba81ee8e42d` (freshly minted at execution time) |
| **2** | candidate_hash | `b0740832807542535084d2c5afd4b2160a4d5b02adb5c2245e574788f2660727` — execution hash **matches the approved hash exactly** |
| **3** | approval_id | `ap_e0c022e9e0c08dfeb5bc` (`approved_by = user-fmp4-talent8-authorization`, count 8, hash-pinned) |
| **4** | batch_id | `b_9ce0185df7a5ed571a5d` (`canary = false`, size 8, status `executed`) |

---

## 5. Exact 8 IDs attempted  ·  6. Exact 8 IDs deleted

All 8 = exactly the approved set, no substitutions. Total **242,630,947 bytes (≈ 231.4 MB)** —
matches the authorized expected total exactly.

| # | derived_id | talent | parent fmt/codec | bytes | live refs | parent audition |
|--:|---|---|---|--:|:--:|---|
| 1 | `43cb35ba043fa3b695e548e2ecbb22f8` | Srishti Rajput | mp4 / h264 | 16 869 871 | 1 | `…/unknown_talent_srishti-rajput/submission_31ad3a2a…/intro_video` |
| 2 | `58b9fc6e1f17ff68b20c0c9b6d107080` | Rajnandini Sharma | mov / h264 | 18 001 880 | 1 | `…/unknown_talent_rajnandini-sharma/submission_1656c52f…/intro_video` |
| 3 | `7c4cfbe0a50de34608fe978c1ebdab31` | Janavi Mahajan | mp4 / h264 | 22 082 541 | 1 | `…/unknown_talent_janavi-mahajan/submission_546c1e64…/intro_video` |
| 4 | `1e50a8101c9aa42486476f14323467dc` | Shrutika Gavhane | mov / h264 | 22 120 672 | 2 | `…/unknown_talent/submission_a80c0541…/intro_video` |
| 5 | `824d30c9de9ddd12c42c894639cc6722` | Paakhi Baranwal | mov / h264 | 23 729 733 | 3 | `…/unknown_talent_paakhi-baranwal/submission_0a072e9f…/intro_video` |
| 6 | `7741c306abfcfc743dcc40e69b0c5e2a` | Aman Desai | mp4 / h264 | 24 937 206 | 1 | `…/unknown_talent_aman-desai/submission_20c9483a…/intro_video` |
| 7 | `078452c33ab95f551b340e2921ac0d6b` | Ritika Ochani | mov / h264 | 26 071 903 | 2 | `…/unknown_talent_ritika-ochani/submission_0ac79754…/intro_video` |
| 8 | `dd4d98f1c56cc6217114a463293aaa82` | Harshita K Chundawat | mov / h264 | 88 817 141 | 2 | `…/1e342b65…_harshita-k-chundawat/submission_7ddffd83…/intro_video` |

## 7. Per-asset revalidation result

Two independent passes, **8 / 8 green** on both:
- **Mandatory 20-point final revalidation** (this script, before any P9 call): all 20 checks
  `true` for all 8 — id matches approved · classification `DELETE_CANDIDATE` · transformation
  exactly bare `f_mp4` · historical purpose = retired download transcode · parent codec `h264` ·
  `video_needs_compat_delivery()` = FALSE · parent exists + active · P3 `owner_type` = talent ·
  talent exists, not archived/deleted · no persisted URL · Mongo derivative ref = 0 · no
  review-link / application / lineage / ledger / soft-deleted-container dependency · retention
  satisfied · Cloudinary identity matches · bytes match the approved manifest.
- **P9 Layer-1** (`build_purge_manifest` + again per-asset inside `execute_batch`):
  `by_verdict: {"PASS": 8}`; execution `passed_candidate_hash` == approved hash.

## 8. Per-asset deletion result

8 × `cloudinary.api.delete_derived_resources([<single id>])` — **one id per call, 8 calls**
(verified: `delete_calls_made` is 8 single-element lists). Each returned
`{"deleted": {<id>: "deleted"}}` → `_response_ok = ok`. **No partial / ambiguous / unknown
response.**

## 9. Parent survival result

**8 / 8 parent originals survive** — verified in Layer 3's own post-delete check, the execution
script's post-loop, and a standalone independent pass (`parents_alive 8/8`).

## 10. MongoDB integrity

**No MongoDB document created, modified, or deleted by this batch.** All 8 parent originals still
referenced in `talents` / `submissions` / `applications` (`mongo_refs_intact 8/8`). No `media[]`
reference altered. Purge collections gained only append-only rows (1 manifest, 1 approval, 1
batch, 8 audit).

## 11. Protected-group verification (full re-scan)

| Bucket | Still present |
|---|---|
| **4 CURRENT_COMPATIBLE** (HEVC) | **4 / 4** |
| **6 UNKNOWN** | **6 / 6** |
| **35 REVIEW_LINKED_CANDIDATE** | **35 / 35** |
| **25 project_submission RETENTION_BLOCKED** | **25 / 25** |

Previous 120 P9 deletions remain deleted (110 re-confirmed by audit cross-reference; the AVIF
canary's 10 use a different result-key and were confirmed in their own run).

## 12. Unexpected deletion count — **0**

Exactly the 8 approved derived ids removed; every parent survived; every protected bucket intact.

## 13. Transformation count — **0**

`usage().transformations.usage` = **71,252 before and after**.

## 14. Upload count — **0**

## 15. Exact bytes confirmed removed

- **Expected:** 242,630,947 bytes.
- **Confirmed (authoritative):** 242,630,947 — all 8 derived resources gone from their parents'
  live `resource(derived=True)` listing (`derived_gone 8/8`); manifest bytes matched live
  Cloudinary bytes exactly for all 8 (largest: `dd4d98f1…` at 88,817,141 B).

## 16. `usage()` before / after

| | storage bytes | derived objects | transformations |
|---|---:|---:|---:|
| before f_mp4 Batch 3 | 19,604,077,694 | 8,416 | 71,252 |
| after f_mp4 Batch 3 (immediate) | 19,604,077,694 | 8,416 | 71,252 |

**Not treated as a failure** — Cloudinary's usage API refreshes asynchronously. The current
`derived` (8,416) already reflects everything through the **f_mp4 canary** (8,511 P8.5 baseline −
95). Still pending in `usage()`: f_mp4 Batch 2 (−25) + this Batch 3 (−8). Authoritative per-parent
verification: **8,416 → 8,383 derived, −(186,038,815 + 242,630,947) bytes** once `usage()`
catches up.

## 17. Direct derived verification

Per-parent `cloudinary.api.resource(parent, resource_type="video")` for each of the 8:
`derived_id` absent from `derived[]` (**8/8 gone**), `public_id` present (**8/8 parent alive**).

## 18. Audit-log count

**8** new immutable records for batch `b_9ce0185df7a5ed571a5d` (`deletion_result = deleted`,
`revalidation_result = PASS`, `canary = false`, `classification = DELETE_CANDIDATE`,
`actor = user-fmp4-talent8-authorization`, `dry_run = false`,
`cloudinary_response_summary = {"deleted": {<id>: "deleted"}}`, full asset + manifest + approval +
batch fields). **No secrets** (asserted). Total audit records now **128**
(10 + 25 + 50 AVIF + 10 + 25 + 8 f_mp4).

## 19. Anomaly status — **none**

`stopped: false`, `stop_reason: null`. No identity mismatch, no unexpected reference, no missing
parent, no ownership change, no persisted-URL discovery, no ambiguous Cloudinary response, no
post-delete verification failure, no unexpected MongoDB change. (The earlier aborted attempt was
a script-verification bug, not an asset anomaly — it deleted nothing.)

## 20. Final physical-delete flag state — **OFF**

Asserted `False` **before** execution. Set to `true` **in the execution process only** (Railway
service env never touched — pre-run reading `None`). Asserted `True` during the run. `pop`-ped +
asserted `False` in a `finally` block. Re-verified unset (`enabled? False`) by the standalone
pass.

## 21. Confirmation that no further batch ran

**Confirmed.** `db.purge_batches` contains **six** batches — `b_924a1600…` (AVIF canary),
`b_60dee7a7…` (AVIF B2), `b_5e239bb6…` (AVIF B3), `b_e4029492…` (f_mp4 canary),
`b_519efa20…` (f_mp4 B2), `b_9ce0185d…` (this f_mp4 B3) — all `executed`. `create_batch` was
called once this run; a non-canary batch never auto-continues. No further batch created or
executed.

---

## Purge-collection state

| Collection | Count |
|---|---:|
| `purge_manifests` | 7 |
| `purge_approvals` | 6 |
| `purge_batches` | 6 (all `executed`) |
| `purge_audit_log` | 128 |

## P9 running total (all families)

| Family | Batches | Derived deleted | Bytes |
|---|---|--:|--:|
| retired AVIF | 10 + 25 + 50 | 85 | 1,316,213 |
| retired bare `f_mp4` | canary 10 + 25 + 8 | 43 | 453,847,057 |
| **Total** | **6** | **128** | **455,163,270 (~434.1 MB)** |

The f_mp4 retired-download family is now **fully cleared** — all 43 talent-owned
`DELETE_CANDIDATE` deleted (10 + 25 + 8).

## Frozen — untouched by this batch

- **25 `project_submission` f_mp4** (RETENTION_BLOCKED — live audition parents; verified 25/25 present)
- **4 `CURRENT_COMPATIBLE`** HEVC (verified 4/4)
- **6 `UNKNOWN`** (verified 6/6)
- **35 `REVIEW_LINKED_CANDIDATE`** (verified 35/35)
- 2,654 `LEGACY_DERIVED`, 468 `PROTECTED_HISTORICAL_DERIVED`, all remaining retired AVIF

## STOP

f_mp4 Batch 3 is complete. **No further deletion is authorized.** Physical deletion is disabled
again (`MEDIA_LIFECYCLE_PHYSICAL_DELETE` unset). No automatic progression. Awaiting your review.
