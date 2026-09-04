# Cloudinary Re-architecture — P9: Controlled, Audited, Per-Asset Deletion

**Phase:** P9 — controlled purge system (derived assets only, first release)
**Branch:** `p9/controlled-purge` · PR #12
**Status:** implemented + tested + dry-run against production — **canary NOT executed; awaiting
your explicit approval of the first 10 assets**

**ABSOLUTE STOP HONOURED:** nothing deleted · no canary run · `MEDIA_LIFECYCLE_PHYSICAL_DELETE`
stays OFF · no bulk / prefix / folder delete anywhere · the 5,005 candidates, the 113 `f_mp4`,
the 468 persisted-legacy, and the 2,654 `LEGACY_DERIVED` are all untouched.

---

## Architecture — three layers, physically separated

`backend/cloudinary_controlled_purge.py`. Layers 1–2 are unit-tested with **zero Cloudinary
writes**; Layer 3 with an **injected fake deleter**.

### Layer 1 — `revalidate_candidate(db, candidate, resource_fetcher, ...)`

Re-proves, against **live** state, that one derived asset is safe to delete. Returns one of:
`PASS` · `PROTECTED` · `STALE_MANIFEST` · `RETENTION_BLOCKED` · `REFERENCE_BLOCKED` ·
`OWNERSHIP_BLOCKED` · `PARENT_BLOCKED` · `IDENTITY_MISMATCH` · `NOT_FOUND` · `UNKNOWN`.
Only `PASS` is deletable; everything else blocks.

| Check | Blocks on |
|---|---|
| **classification gate** | anything not `DELETE_CANDIDATE`; the P8.5 `_NEVER` set (`PROTECTED_*`, `ACTIVE_*`, `UNKNOWN_DERIVED`, `LEGACY_DERIVED`, `PENDING_RETENTION`, `STALE_METADATA_ONLY`) |
| **ownership** (P3 authoritative, **folder NEVER**) | no `media[]` reference to the parent · `ownership.conflict` set · `owner_type` unknown · owner_type changed vs manifest → `STALE_MANIFEST` |
| **parent active** | the parent original's only references are soft-deleted |
| **retention** | parent is live project-audition media (not eligible) |
| **repo-wide reference** | this exact derivative's URL persisted in any `media[]` field (`url`/`poster_url`/`thumbnail_url`/`original_url`/`compat_delivery_url`/`video_url`), matched **per-variant** as `(normalised_parent, transformation_family)` — robust to Cloudinary version numbers · copy-by-value lineage on the parent/derivative · parent in the `pending_media_deletions` ledger |
| **live Cloudinary identity** | parent original gone → `PARENT_BLOCKED` · derived id absent → `NOT_FOUND` (idempotent, nothing to do) · transformation / format differ from manifest → `IDENTITY_MISMATCH` · bytes changed > 5% → `STALE_MANIFEST` |

### Layer 2 — manifest / approval / batch

* `build_purge_manifest()` — runs Layer 1 over every candidate, freezes the result into an
  **immutable** `purge_manifests` doc with `manifest_id`, `passed_candidate_ids`,
  `passed_candidate_hash`, per-verdict tally, and a `canary_preview`.
* `approve_manifest(manifest_id, approved_by, candidate_ids)` — an explicit
  `purge_approvals` record. The approval **names an exact set**, pins it with
  `candidate_hash` (order-independent SHA-256), and can only name ids that **passed**
  revalidation in that manifest. **There is no `approve_all_future_candidates`** — an approval
  is one manifest, one immutable set.
* `create_batch(approval_id, size, canary)` — carves not-yet-processed candidates. **Canary
  batch is exactly 10.** Non-canary is capped at 50. The canary is drawn only from the safest
  family (retired sized/full-res AVIF, `PASS`, live+referenced parent, no persisted URL) —
  explicitly **never** `f_mp4` / `fl_attachment` / `vc_auto` / `fl_sprite` / any non-
  `DELETE_CANDIDATE` / any orphan-parent.

### Layer 3 — `execute_batch(db, batch_id, dry_run, resource_fetcher, derived_deleter, actor)`

Real deletion requires **ALL** of: `dry_run=False` · `MEDIA_LIFECYCLE_PHYSICAL_DELETE` on ·
the batch's approval exists, is unconsumed, and its `candidate_hash` still matches · the batch
ids are all in the manifest's passed set. Then, **per asset**:

1. **re-run Layer 1 live** (immediately before this asset's delete)
2. not `PASS` →
   * `NOT_FOUND` → skip (already gone)
   * anything else, and the manifest said `PASS` → the manifest **went stale** → **STOP the run**
   * otherwise → skip + audit
3. `PASS` + `dry_run` → audit "would_delete", write **nothing**
4. `PASS` + real → `derived_deleter([this_one_id])` — `cloudinary.api.delete_derived_resources`
   with a **single** derived id. **Never** a prefix, folder, transformation, or `destroy`.
   * deleter raises → stop
   * ambiguous / partial response → `DELETION_STATUS_UNKNOWN` → **stop, do not retry**
5. **post-delete verify**: re-fetch the parent → the derived id must be **gone** and the parent
   original must **still exist**. Derived still there → `delete_not_effective` → stop. Parent
   **disappeared** → `PurgeAnomaly` (hard halt).
6. immutable insert-only audit record (no secrets)

**The canary never auto-continues** — after 10, the run returns and the caller must review and
create the next batch.

### Anomaly stop

The run halts immediately on: a `PASS`-in-manifest asset now blocked · identity mismatch ·
a new reference · ownership change · parent gone · deleter exception · ambiguous response ·
derived still present after delete · parent disappeared · post-delete verify failure.

### `MEDIA_LIFECYCLE_PHYSICAL_DELETE`

Stays **OFF**. It is only ever **read** — no endpoint or code path sets it, and it alone is
**not** sufficient authorization (a matching, unconsumed, hash-verified approval is also
required). `POST /purge/execute` defaults `dry_run=true`.

---

## Endpoints (admin-gated, GET-only where read)

| Endpoint | Layer | Effect |
|---|---|---|
| `GET /api/admin/cloudinary/purge/manifest` | 2 | build a DRY-RUN purge manifest (Layer-1 revalidation over the `DELETE_CANDIDATE` derived assets) |
| `POST /api/admin/cloudinary/purge/approve` `{manifest_id, candidate_ids[]}` | 2 | immutable hash-pinned approval |
| `POST /api/admin/cloudinary/purge/batch` `{approval_id, canary, size}` | 2 | carve a size-capped batch |
| `POST /api/admin/cloudinary/purge/execute` `{batch_id, dry_run=true}` | 3 | run the batch — dry-run by default |

No "Delete All" / "Clean Everything" / "Delete Orphans" endpoint. No endpoint mutates the flag.

---

## Tests — `backend/tests/test_cloudinary_controlled_purge.py`

**33 tests covering the 36 required concerns** (some combined). All green.

`DELETE_CANDIDATE` can pass · `UNKNOWN` / `CONFLICT` / persisted-URL / historical-protected /
`f_mp4` / orphan-parent `LEGACY_DERIVED` **cannot** pass · ownership change / parent change /
identity change / bytes change → `STALE_MANIFEST` · retention not-expired / indefinite /
invalid → blocks · exact single-id deletion (deleter always receives `[one_id]`) ·
**no prefix / folder / `delete_resources` / `destroy` call anywhere (AST assert)** · batch
size enforced · approval required + manifest-specific + hash-pinned · no approval / flag off →
no Cloudinary deletion · dry-run → **zero Cloudinary writes, zero Mongo writes** · failed
deletion logged · ambiguous response stops (no retry) · unexpected count / parent-gone stops ·
successful delete verified · parent survives · Mongo references untouched · **the 468
persisted-legacy derivatives cannot be deleted** · **the 113 `f_mp4` cannot be deleted** ·
**orphan-parent legacy derivatives cannot be deleted** · canary exactly 10 · **stops after
canary, no auto-continue** · audit fields present · credentials never in the audit log ·
repeated execution cannot delete an already-executed batch.

Regression: P8 manifest (31) · P6 lifecycle (35) · P7 accounting (24) · P5 (20) · P4 (48) —
green per-file.

---

## Dry run against production (READ-ONLY)

*(Full ~5,005-candidate dry run in progress — this section is finalised once it completes.
Preliminary sample below.)*

**Sample of 400 AVIF `DELETE_CANDIDATE` derived assets:**

| | |
|---|---|
| candidates examined | 400 |
| **passed Layer-1 revalidation** | **400 / 400** (141.8 MB) |
| blocked / stale / protected | 0 |
| `MEDIA_LIFECYCLE_PHYSICAL_DELETE` | **OFF** |
| Cloudinary / MongoDB writes | **0 / 0** |

**Proposed first-10 canary (exact assets):** 10 `f_avif,q_auto/png` derivatives, 0.1–274 KB
each, every one with a live + referenced, talent-owned parent original and no persisted URL —
the safest possible category (retired render-time AVIF conversion, regenerable on demand).
The exact `derived_id`s are in the manifest's `canary_preview`.

---

## STOP

The system is built, tested, and dry-run. **Nothing has been deleted.** The canary has **not**
been executed. Physical deletion remains disabled.

**Next step is yours:** review the full dry-run report + the exact 10 canary `derived_id`s, then
explicitly approve those 10 assets. Only then will `POST /purge/execute {dry_run:false}` be run
(with the env flag enabled for that single scoped operation), followed by the 10-point canary
verification and another STOP for your review before batch 2.
