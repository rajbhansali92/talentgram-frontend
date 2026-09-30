# P9 Batch 2 — Execution Result (25 retired-AVIF derived assets)

**Executed 2026-08-30** on your authorization of "ONE additional controlled deletion batch of
EXACTLY 25 assets". **25 / 25 deleted. 0 blocked. 0 stale. 0 anomalies. No batch 3. Flag OFF.**

Machine-readable: `docs/CLOUDINARY_P9_BATCH2_RESULT.json`.

| Item | Value |
|---|---|
| **1. new manifest_id** | `pm_ec90d89f0a78b4728c9d` |
| **2. new approval_id** | `ap_93c07a8507f9d294a34a` (`approved_by = user-batch2-authorization`, count 25, hash-pinned) |
| **3. new batch_id** | `b_60dee7a7e922b94fedf0` (canary=false, size 25, status `executed`) |
| **4. new candidate_hash** | `e64c483daa236068df78da6009a4d25ee4b8c1c9b378b9d1ad19c5dc7187311a` (order-independent SHA-256 of exactly these 25 ids — distinct from the canary hash) |

This is a **fresh** manifest / approval / hash — the canary approval was not reused.

---

## 5. Exact 25 derived_ids proposed  · 6. exact 25 attempted

All 25 are P8.5 `DELETE_CANDIDATE`, transformation `f_avif,q_auto/jpg` (24) or `f_avif,q_auto/png` (1)
— retired, render-time-only, regenerable, never persisted. Fresh Layer-1 revalidation: **25 / 25
`PASS`**.

| # | derived_id | fmt | bytes | parent owner docs | parent live refs | persisted URL | verdict |
|--:|---|---|--:|--:|--:|---|---|
| 1 | `dd9a2a15b43e8bf9d400a716badc38ed` | jpg | 11 729 | 1 | 1 | NO | PASS |
| 2 | `5cf4a37e1dfccb831a8f840ddead9c20` | jpg | 11 971 | 2 | 2 | NO | PASS |
| 3 | `cde08bf66c2698ef82613f4934465f16` | jpg | 12 283 | 1 | 1 | NO | PASS |
| 4 | `2d3c802fba6950ac0bcb9166ac6b0e84` | jpg | 12 602 | 1 | 1 | NO | PASS |
| 5 | `cdef11607a86584d195f91ce415c852c` | jpg | 12 881 | 2 | 2 | NO | PASS |
| 6 | `519c5cba73811e5a90d55dd761f7135f` | jpg | 12 888 | 1 | 1 | NO | PASS |
| 7 | `ad41e148df7fd8c5b56ac8432efb08c2` | jpg | 12 969 | 3 | 3 | NO | PASS |
| 8 | `3e1874e40fe003659d5f916533be5737` | png | 13 002 | 2 | 2 | NO | PASS |
| 9 | `25fae2478fdfe00751834b521ae3cdb9` | jpg | 13 121 | 1 | 1 | NO | PASS |
| 10 | `8ddaaa0d680bd58f7e31ab71e5fa96a2` | jpg | 13 211 | 3 | 3 | NO | PASS |
| 11 | `c7f823c96417f397a2fd60762317d4c0` | jpg | 13 212 | 1 | 1 | NO | PASS |
| 12 | `9a52ac74987ebc12cf5ac5f0c2a4d2fa` | jpg | 13 333 | 2 | 2 | NO | PASS |
| 13 | `467a9d090508d489fe3ed31a4ea808f5` | jpg | 13 503 | 2 | 2 | NO | PASS |
| 14 | `dfc435bd2ec348f970d1b53152dbbc65` | jpg | 13 511 | 1 | 1 | NO | PASS |
| 15 | `9657044cf686b307df378b770652f79e` | jpg | 13 752 | 1 | 1 | NO | PASS |
| 16 | `ea50452e0e8057382e6d3eaa03c5c946` | jpg | 13 826 | 1 | 1 | NO | PASS |
| 17 | `703ec7d5657b2d2008e306acf423d68d` | jpg | 13 930 | 3 | 3 | NO | PASS |
| 18 | `618746522d4642107039eb9c82e9c503` | jpg | 13 930 | 2 | 2 | NO | PASS |
| 19 | `ff5ae6ca777e9da2ffc93217c0e0a7c3` | jpg | 14 644 | 2 | 2 | NO | PASS |
| 20 | `a930ff80765738b9514a85fe7f7bb011` | jpg | 14 702 | 2 | 2 | NO | PASS |
| 21 | `05320296ff98689b2c6d65aa55651109` | jpg | 14 780 | 1 | 1 | NO | PASS |
| 22 | `be7dc78275c956eb415c0267be330205` | jpg | 14 819 | 1 | 1 | NO | PASS |
| 23 | `30046141b032c60fff3f837aba4ec331` | jpg | 14 877 | 1 | 1 | NO | PASS |
| 24 | `4163d52182ef32475856aa4463ba03c6` | jpg | 14 999 | 3 | 3 | NO | PASS |
| 25 | `f1e04c377eef130497633afdc8470c6d` | jpg | 15 074 | 1 | 1 | NO | PASS |

Every asset: `owner_type = talent` (P3, folder never consulted) · `parent_live_refs == parent_owner_docs`
(no soft-deleted references) · `persisted_url_hit = false` · **manifest bytes == live Cloudinary
`derived.bytes`** (exact identity) · transformation retired. Total: **339,549 bytes**.

## 7. Exact number deleted

**25.** `attempted: 25 · deleted: 25 · skipped: 0 · blocked: 0 · stopped: false`.

## 8. Any blocked / stale assets

**None.** All 25 proposed passed the fresh revalidation; none were substituted.

## 9. Deletion result per asset

25 × `cloudinary.api.delete_derived_resources([<single id>])` → each `{"deleted": {<id>: "deleted"}}`
→ `ok`. **25 delete calls, each with exactly one id.** No partial / ambiguous response.

## 10. Parent-survival results

All 25 parent originals exist — verified in Layer 3's own post-delete check **and** a standalone
fresh pass (`parents alive: 25/25`).

## 11. MongoDB verification

All 25 parents' `media[]` references **unchanged** (present in talents/submissions/applications:
`25/25`). **No MongoDB document modified.** (The derivatives were never referenced in MongoDB.)

## 12. Unexpected deletion count

**0.** Exactly the 25 approved derived ids were removed; every parent survived; the previously
deleted canary 10 stayed deleted; nothing else changed.

## 13. Transformation count

**0.** `usage().transformations` = 71,687 before and after batch 2.

## 14. Upload count

**0.**

## 15. Storage / accounting change

| | storage bytes | derived objects | transformations |
|---|---:|---:|---:|
| before batch 2 (= post-canary) | 19,629,588,642 | 8,501 | 71,687 |
| after batch 2 (immediate) | 19,629,588,642 | 8,501 | 71,687 |

The **canary** is now reflected in `usage()` (8,511 → 8,501, storage −60,551 bytes ≈ the canary's
60,741). **Batch 2's 25 deletions are not yet reflected** — the usage API refreshes
asynchronously. Authoritative confirmation: per-parent `resource(derived=True)` shows **all 25
derived ids gone**, so derived objects **8,501 → 8,476** and storage **−339,549 bytes** once the
usage API catches up.

## 16. Audit-log count

`db.purge_audit_log`: **25 new immutable records** for batch `b_60dee7a7e922b94fedf0`
(`deletion_result = deleted`, `revalidation_result = PASS`, `actor = user-batch2-authorization`,
full asset + manifest + approval + batch fields). **No secrets in any record** (asserted).
Total audit records now **35** (10 canary + 25 batch 2).

## 17. Anomaly status

**No anomaly.** `stopped: false`, `stop_reason: null`. No identity mismatch, no unexpected
reference, no missing parent, no ownership change, no persisted URL discovered, no ambiguous
Cloudinary response, no post-delete verification failure.

## 18. Final physical-delete flag state

**OFF.** Enabled in the execution *process only* (Railway service env never touched), asserted
`True` during the run, `pop`-ped + asserted `False` in a `finally` block, re-verified unset by a
standalone pass.

---

## Purge-collection state

| Collection | Count | Notes |
|---|---:|---|
| `purge_manifests` | 3 | `pm_7c0dc1b2…` (disclosed dry-run artifact) · `pm_d9a40130…` (canary) · `pm_ec90d89f…` (batch 2) |
| `purge_approvals` | 2 | canary + batch 2 |
| `purge_batches` | 2 | `b_924a1600…` (canary, executed) · `b_60dee7a7…` (batch 2, executed) — **no batch 3** |
| `purge_audit_log` | 35 | 10 + 25 |

## Running total deleted (P9 so far)

| | derived objects | bytes |
|---|---:|---:|
| Canary | 10 | 60,741 |
| Batch 2 | 25 | 339,549 |
| **Total** | **35** | **400,290 (~0.39 MB)** |

All from the single `f_avif,q_auto/{jpg,png}` retired transformation family.

## STOP

Batch 2 is complete. **No batch 3 is authorized.** Frozen and untouched: the remaining
~4,970 `DELETE_CANDIDATE`, the **113 `f_mp4`**, the **2,654 `LEGACY_DERIVED`**, the
**468 `PROTECTED_HISTORICAL_DERIVED`**. Physical deletion is disabled again. Awaiting your
review of these results and your next instruction.
