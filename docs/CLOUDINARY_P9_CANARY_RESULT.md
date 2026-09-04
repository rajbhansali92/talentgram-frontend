# P9 Canary — Execution Result

**Executed 2026-08-30** on your explicit approval of exactly these 10 derived-asset ids.
**10 / 10 deleted. 0 failures. 0 anomalies. No second batch. Flag returned to OFF.**

Machine-readable: `docs/CLOUDINARY_P9_CANARY_RESULT.json`.

| Identity | Value |
|---|---|
| approved `candidate_hash` | `b9abc586ff6fb894f989d045d98e846941fc2681570737e90028be052396db94` |
| fresh manifest (re-revalidated at execute time) | `pm_d9a40130a1937e262dbe` — `passed_candidate_hash` **matched** the approved hash exactly |
| approval | `ap_5a7d79e2365b48adc8f1` (`approved_by = user-explicit-approval`, count 10, hash-pinned) |
| canary batch | `b_924a1600328b3c5e5852` (canary=true, size 10, status `executed`) |

---

## 1. Exact 10 assets attempted

`4e0b91628ad040df8f03b35be20d40a9` · `90e2c0d506f9c3204805c540a2b906d7` ·
`dcb3a4b118d6ea2d24dc152bfd9bd743` · `36a77e5f8dd27f5e628133fb56afff65` ·
`73d1d947e0df9b419a43b492a9a265a1` · `87b0e6770c6e9815b756624c8f19a185` ·
`439200ab322c41e518aee866828ff021` · `b1a29f34d6901420dae9cfa783e6f286` ·
`ee24054723beeb7b14da77221c9ccdd8` · `20ff5acda320d634c2d66ad523e63ddd`

(exactly the 10 you approved — no substitutions)

## 2. Exact 10 assets successfully deleted

**All 10.** Each re-passed a fresh Layer-1 revalidation (`PASS`) immediately before its own
delete, then was removed via `cloudinary.api.delete_derived_resources([<single id>])`.

| # | derived_id | bytes | transformation | pre-delete revalidation | delete result |
|--:|---|--:|---|---|---|
| 1 | `4e0b91628ad040df8f03b35be20d40a9` | 91 | `f_avif,q_auto/jpg` | PASS | deleted |
| 2 | `90e2c0d506f9c3204805c540a2b906d7` | 91 | `f_avif,q_auto/jpg` | PASS | deleted |
| 3 | `dcb3a4b118d6ea2d24dc152bfd9bd743` | 91 | `f_avif,q_auto/jpg` | PASS | deleted |
| 4 | `36a77e5f8dd27f5e628133fb56afff65` | 95 | `f_avif,q_auto/png` | PASS | deleted |
| 5 | `73d1d947e0df9b419a43b492a9a265a1` | 6 294 | `f_avif,q_auto/jpg` | PASS | deleted |
| 6 | `87b0e6770c6e9815b756624c8f19a185` | 10 449 | `f_avif,q_auto/jpg` | PASS | deleted |
| 7 | `439200ab322c41e518aee866828ff021` | 10 514 | `f_avif,q_auto/jpg` | PASS | deleted |
| 8 | `b1a29f34d6901420dae9cfa783e6f286` | 10 621 | `f_avif,q_auto/jpg` | PASS | deleted |
| 9 | `ee24054723beeb7b14da77221c9ccdd8` | 11 062 | `f_avif,q_auto/jpg` | PASS | deleted |
| 10 | `20ff5acda320d634c2d66ad523e63ddd` | 11 243 | `f_avif,q_auto/jpg` | PASS | deleted |

Total: **60 741 bytes** across 10 derived resources.

## 3. Any asset that failed revalidation

**None.** `attempted: 10 · deleted: 10 · skipped: 0 · blocked: 0 · stopped: false`.

## 4. Deletion response for each

Each `cloudinary.api.delete_derived_resources([id])` returned `{"deleted": {<id>: "deleted"}}`
→ `_response_ok` = `ok`. 10 delete calls, **each with exactly one id** (verified). No
`partial`, no ambiguous response, no `DELETION_STATUS_UNKNOWN`.

## 5. Post-delete existence verification

For every one of the 10, a **fresh** `cloudinary.api.resource(parent, derived=True)` shows the
`derived_id` is **gone** (`approved-derived still present: []` for all 10 parents).

## 6. Parent-survival verification

All 10 parent originals still exist (`parent_exists = True` for every one), fetched fresh both
in Layer 3's own post-delete check and again in the standalone verification pass.

## 7. MongoDB reference verification

All 10 parents' `media[]` references are **unchanged** — still present in
`talents`/`submissions`/`applications`. **No MongoDB document was modified.** (The derived
assets were never referenced in MongoDB; only their parents were, and those refs are intact.)

## 8. Unexpected deletion count

**0.** Exactly the 10 approved derived ids were deleted; every parent original survived; no
other Cloudinary resource was touched.

## 9. Transformations generated

**0.** `usage().transformations` = 71 687 before and immediately after. Deleting a derived
asset generates no transformation, and none was regenerated in the window.

## 10. Upload count

**0.**

## 11. Storage / accounting change

`cloudinary.api.usage()` immediately after: `storage 19 629 649 193 · derived 8 511 ·
transformations 71 687` — **unchanged from before**, because Cloudinary's usage API is
refreshed asynchronously (periodically, not real-time). The **authoritative** confirmation is
the per-parent `resource(derived=True)` check: **8 511 → 8 501 derived objects, −60 741 bytes**.
The usage API will reflect this on its next refresh cycle.

## 12. Audit-log confirmation

`db.purge_audit_log` — **10 immutable records** for batch `b_924a1600328b3c5e5852`, one per
asset: `timestamp`, `actor = user-explicit-approval`, `manifest_id`, `approval_id`, `batch_id`,
`canary = true`, `public_id`, `parent_public_id`, `derived_id`, `resource_type`, `format`,
`bytes`, `transformation`, `classification`, `revalidation_result = PASS`,
`deletion_result = deleted`, `cloudinary_response_summary`. **No credentials / secrets in any
record** (asserted).

## 13. Final `MEDIA_LIFECYCLE_PHYSICAL_DELETE` state

**OFF.** The flag was set in the execution *process only* (the Railway service env was never
touched), asserted `True` during the run, and `pop`-ped + asserted `False` in a `finally`
block before exit. A standalone verification pass afterwards confirms it is unset →
`_physical_delete_enabled() == False`.

## 14. Confirmation that no second batch ran

**Confirmed.** `db.purge_batches` contains exactly **one** batch (`b_924a1600328b3c5e5852`,
`canary=true`, `status=executed`). `create_batch` was called once. `execute_batch` never
loops past its batch's own 10 candidates, and a canary batch never auto-continues. The
approval `ap_5a7d79e2365b48adc8f1` remains with `consumed_batch_ids = [b_924a1600328b3c5e5852]`
and its remaining candidate pool is empty.

---

## Purge-collection state after the canary

| Collection | Count |
|---|---:|
| `purge_manifests` | 2 (`pm_7c0dc1b2008103a2a4c3` — disclosed earlier dry-run artifact · `pm_d9a40130a1937e262dbe` — this canary) |
| `purge_approvals` | 1 |
| `purge_batches` | 1 (`executed`) |
| `purge_audit_log` | 10 |

## STOP

The 10-asset canary is complete. **No further deletion is authorized.** Nothing else has been
touched — not the remaining ~4,995 `DELETE_CANDIDATE`, the 1,968 dpr thumbnails, the 146
`fl_attachment`, the 179+44+22 old 720p chains, the 113 `f_mp4`, the 2,654 `LEGACY_DERIVED`,
or the 468 `PROTECTED_HISTORICAL_DERIVED`. Physical deletion is disabled again.

Awaiting your review of these canary results and your next instruction.
