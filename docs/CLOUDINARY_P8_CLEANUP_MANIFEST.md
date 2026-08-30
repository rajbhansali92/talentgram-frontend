# Cloudinary Re-architecture — P8: Read-Only Cleanup Manifest Engine (DRY-RUN)

**Phase:** P8 — production-safe cleanup **analysis** engine
**Branch:** `p8/cleanup-manifest-engine`
**Status:** implemented + tested + run against production (read-only) — **awaiting review**
**Hard rules honoured:** ZERO Cloudinary writes · ZERO MongoDB writes · no `destroy` /
`delete_resources*` / `delete_folder` anywhere · physical-delete flag stays OFF · NO
one-click delete UI · P8 is analysis only — P9 (separate approval) will do controlled deletion.

---

## What it is

`backend/cloudinary_cleanup_manifest.py` answers **"what could we safely delete?"** and
emits a **per-asset manifest** — without deleting anything, and without being able to.

* imports **nothing** from `cloudinary` — holds no handle to any write API
* every DB call is `find` / `aggregate` / `count_documents` — **no MongoDB write**
* the Cloudinary inventory is **passed in** by the caller (the endpoint fetches it read-only
  and owns the cache); the engine makes **zero Cloudinary calls**
* `test_cloudinary_cleanup_manifest.py` walks the module AST to assert all of the above

Endpoint: `GET /api/admin/cloudinary/cleanup-manifest` (admin-gated, GET-only, read-only).
`?rescan=true` refreshes the object inventory (1 read-only listing); `?classification=` /
`?proposed_action=` filter; `?limit`/`?offset` page; `?include_rows=false` → summary only.

---

## Inputs cross-referenced (all read-only)

1. Cloudinary physical inventory (`cloudinary.api.resources` — **originals**; cached 24 h)
2. P3 `media[i].ownership` (owner_type / owner_id / project / submission / application)
3. Every `media[].public_id` **and** the full public_id recovered from `media[].url`
   (legacy sign endpoints stored a bare leaf id; the Cloudinary inventory keys on the
   full path — the manifest joins both)
4. `pending_media_deletions` ledger (P6)
5. `media[].lifecycle.state` + project/submission `lifecycle_state` / `deleted_at`
6. talent / project / submission / application records
7. historical (soft-deleted) submission references
8. **active** client-review links (`links.talent_ids` / `submission_ids`)
9. copy-by-value lineage (`source_*_media_id`)
10. derived-parent relationships (parsed from the delivery URL / a `derived_of` tag)
11. retention policy (`media_lifecycle.get_retention_days`; invalid → indefinite)
12. `asset_metadata` rows

**The Cloudinary folder path is NEVER consulted for ownership.** (Test 28.)

## Deletion-eligibility gate

`DELETE_ELIGIBLE` requires **ALL** of: KNOWN OWNERSHIP · NO PROTECTED REFERENCES ·
RETENTION EXPIRED · NO DEPENDENCIES · SAFE LIFECYCLE STATE · KNOWN ASSET RELATIONSHIP.
Any one unknown → **PROTECTED**. "Orphan" — unreferenced in MongoDB, parent doc deleted,
old folder, derived, months old — is **never** sufficient. A candidate is not marked
`SAFE_ORPHAN` until a repo-wide search for its identifiers (public_id / secure_url /
poster_url / thumbnail_url / original_url / asset_id, **including derived transformation
URLs** — P5 did not rewrite legacy stored URLs) finds nothing.

`DELETE_ELIGIBLE` ≠ delete now. It means *"could be safely deleted IF an admin approves
AND P9's fresh per-asset re-check passes."*

---

## Classifications & proposed actions

| Classification | Action | Meaning |
|---|---|---|
| `ACTIVE_GLOBAL_TALENT_MEDIA` | KEEP | referenced by a live (non-deleted) record; P3 owner_type = talent |
| `ACTIVE_PROJECT_AUDITION_MEDIA` | KEEP | referenced by a live submission; owner_type = project_submission |
| `ACTIVE_DERIVED_ASSET` | KEEP | referenced derived variant of a live parent |
| `PROTECTED_HISTORICAL` | PROTECT | active client link, or a persisted URL still contains this public_id, or global media whose only reference is soft-deleted |
| `PROTECTED_SHARED` | PROTECT | backing object on >1 owner document (copy-by-value) |
| `PROTECTED_UNKNOWN` | PROTECT | present, unreferenced, ownership unprovable — "orphan is never enough" |
| `PROTECTED_CONFLICT` | PROTECT | P3 `ownership.conflict` set |
| `PENDING_RETENTION` | WAIT_FOR_RETENTION | audition media, owner soft-deleted, retention window not elapsed (or indefinite, or no resolvable teardown timestamp) |
| `SAFE_ORPHAN` | DELETE_ELIGIBLE | known owner, all refs gone, retention expired, no persisted URL — safe **subject to P9's re-check** |
| `LEGACY_DERIVED_CANDIDATE` | REVIEW | old-transform derived asset, no persisted URL references it |
| `STALE_METADATA_ONLY` | REVIEW | in `asset_metadata` but no `media[]` item references it |

---

## Production manifest — `880d759cd800b882` (read-only, 2026-08-30)

### 1–2. Assets scanned & storage

| | |
|---|---|
| **Original objects scanned** | **4,348** (`cloudinary.api.resources`) |
| Manifest original bytes | **14.69 GB** |
| Derived objects | **8,511** reported by the usage API — **not individually enumerated** (`resources()` lists originals only; per-object derived classification needs one `resource(derived=True)` call per original → out of scope for a page-load manifest) |
| Cloudinary usage-API total storage | **19.63 GB** |
| Gap (usage − manifest originals) | **4.94 GB** ≈ the 8,511 derived assets |

### 3–10. Classification breakdown

| Classification | Objects | Bytes | Action |
|---|---:|---:|---|
| `PROTECTED_SHARED` | **1,413** | 7.39 GB | PROTECT |
| `PROTECTED_UNKNOWN` | **1,343** | 1.19 GB | PROTECT |
| `ACTIVE_GLOBAL_TALENT_MEDIA` | **694** | 2.57 GB | KEEP |
| `PROTECTED_HISTORICAL` | **463** | 1.92 GB | PROTECT |
| `STALE_METADATA_ONLY` | **396** | 0.51 GB | REVIEW |
| `ACTIVE_PROJECT_AUDITION_MEDIA` | **36** | 0.98 GB | KEEP |
| `PROTECTED_CONFLICT` | **3** | 0.14 GB | PROTECT |
| `LEGACY_DERIVED_CANDIDATE` | 0 | — | (derived not enumerated) |
| `PENDING_RETENTION` | **0** | — | WAIT |
| `SAFE_ORPHAN` | **0** | — | DELETE_ELIGIBLE |

**By proposed action:** PROTECT **3,222** · KEEP **730** · REVIEW **396** · **DELETE_ELIGIBLE 0**
**By confidence:** high 2,609 · low 1,343 (the unknown orphans) · medium 396 (stale metadata)

| # | P8 output item | Value |
|---|---|---|
| 3 | Active originals | **730** (694 global talent + 36 project audition) / 3.55 GB |
| 4 | Active derived | 0 in manifest · 8,511 per usage API (not enumerated) |
| 5 | Protected | **3,222** / 10.64 GB |
| 6 | Unknown | **1,343** / 1.19 GB — genuine orphans, ownership unprovable, PROTECTED |
| 7 | Pending retention | **0** — nothing has been soft-deleted in production since P6 shipped |
| 8 | Legacy candidates | **396** `STALE_METADATA_ONLY` (REVIEW) / 0.51 GB |
| 9 | **Delete-eligible candidates** | **0** |
| 10 | **Estimated reclaimable GB** | **0 GB right now.** The old "~7,510 objects / ~5 GB" pool is **superseded** — it counted derived assets, copy-by-value shares, and folder-path orphans as "deletable". Under P8's proven-ownership + retention gate, none qualify today. Deletable volume appears only after projects/submissions are soft-deleted and their retention windows elapse. |

### 11. Reconciliation

```
Cloudinary usage-API storage ............ 19.63 GB
  = manifest originals (14.69 GB) + ~8,511 derived assets (≈ 4.94 GB)

Manifest originals 14.69 GB
  = distinct referenced (13.00 GB)          [shared counted ONCE]
  + unreferenced / unknown originals (1.69 GB)   → PROTECTED_UNKNOWN + STALE_METADATA_ONLY
```

### 12. Top 100 largest candidates

All 100 are `STALE_METADATA_ONLY` (REVIEW) — the largest is a **101 MB** `audition_video`
`asset_metadata` row with no `media[]` reference. Full list: `?classification=STALE_METADATA_ONLY`
on the endpoint, or `top_100_largest_candidates` in the manifest payload.

### 13. Top families

Derived-transformation families are not enumerated (see #4). `STALE_METADATA_ONLY` by
folder-kind: **talents 385 · projects 6 · admin_media 4 · applications 1** — almost all are
`portfolio_video` / `profile_image` metadata rows whose media item was later removed.

### 14. Every reason an asset is protected

| Reason | Count |
|---|---|
| Backing object shared across >1 owner document (copy-by-value) | 1,413 |
| Present, zero MongoDB references, ownership unprovable ("orphan ≠ delete") | 1,343 |
| Surfaced by an ACTIVE client-review link | *(subset of 463)* |
| A persisted URL (media.url / poster_url / thumbnail_url / original_url) still contains this public_id | *(subset of 463)* |
| Global talent media whose only reference is a soft-deleted document | *(subset of 463)* |
| P3 `ownership.conflict` set | 3 |
| Retention window not elapsed / indefinite / no resolvable teardown timestamp | 0 (none soft-deleted yet) |

---

## The 196 "EXAMINE" assets (Section B) — re-evaluated under P3/P6/P8

All **196** are in the manifest. Section B classified 180 as `SAFE_ORPHAN` **using the
Cloudinary folder path** to infer ownership. **P8 does not do that.** Re-evaluated with
proven-ownership-only:

| Section B verdict (folder-based) | P8 verdict (ownership-proven) |
|---|---|
| 180 SAFE_ORPHAN + 15 REPLACED_MEDIA + 1 LEGITIMATE_REFERENCE | **176 `PROTECTED_UNKNOWN` (PROTECT) + 20 `STALE_METADATA_ONLY` (REVIEW)** |
| — | **0 `DELETE_ELIGIBLE`** |

This is deliberately more conservative and directly demonstrates the rule: an asset whose
only ownership signal is its folder path is **PROTECTED**, not deletable. The Section B
"SAFE_ORPHAN" label is **not** carried forward into any deletion decision.

---

## 15. Manifest location

* **Live:** `GET /api/admin/cloudinary/cleanup-manifest` (admin-gated, read-only, cached
  inventory). Returns the full envelope + rows (paged) + `top_100_largest_candidates`.
* Snapshot fields: `manifest_id`, `generated_at`, `source_cloudinary_inventory_time`,
  `source_mongo_snapshot_time`, `dry_run: true`, `read_only: true`,
  `not_authoritative_note` (P9 must re-validate).
* Manifest row schema (per the spec): `asset_id, public_id, resource_type, type, format,
  bytes, created_at, folder, owner_type, owner_id, talent_id, project_id, submission_id,
  application_id, lifecycle_state, classification, proposed_action, reason,
  reference_count, references[], retention_policy, eligible_at, retention_remaining_seconds,
  derived_parent, derived_transformation, confidence`.

## 16. Tests

`backend/tests/test_cloudinary_cleanup_manifest.py` — **31 passed** (in-memory async Mongo
fake). Covers the required 1–30 matrix:

active global/audition → KEEP · shared/unknown/conflict → PROTECT · active submission /
historical / active link → PROTECT · soft-deleted project within/after retention → WAIT /
DELETE_ELIGIBLE · stale metadata → REVIEW · legacy derived with persisted URL → PROTECT ·
legacy derived no refs → REVIEW · replaced media only eligible when proven · derived with
live / deleted / unknown parent classified correctly (deleted parent ≠ auto-delete) ·
shared reference → PROTECT · **manifest deterministic + idempotent (same `manifest_id`)** ·
**no Cloudinary delete call possible (AST)** · **no MongoDB write (AST)** · no frontend
destructive action · stale manifest flagged non-authoritative · **every DELETE_ELIGIBLE
row has a >40-char explanation, never "orphaned → delete"** · retention math correct ·
invalid retention → indefinite (never eligible) · **folder path never determines ownership**
· P3 owner_type determines ownership · **the 33/44 ownerless assets (now resolved) + the
196 EXAMINE remain PROTECTED unless ownership is proven** · reconciliation no double-count.

Regression per-file green: `test_storage_accounting` (24) · `test_media_lifecycle` (35) ·
`test_media_classification` (77) · `test_storage_console_rebuild` (21) · P4 (48) · P5 (20) ·
`test_direct_uploads` (23) · `test_media_assignment` (52) · `test_client_payload_isolation` (6).

## 17. Confirmation — ZERO Cloudinary writes

The engine imports nothing from `cloudinary`. The endpoint's only Cloudinary calls are
`cloudinary.api.resources` (list) and `cloudinary.api.usage` (read) — both wrapped in
`run_in_threadpool`, both read-only. `manifest["integrity"]["cloudinary_writes"] == 0`.
AST test asserts no `destroy` / `delete_resources` / `delete_resources_by_prefix` /
`delete_folder` call exists in the module.

## 18. Confirmation — ZERO MongoDB writes

Every DB call in the engine is `find` / `aggregate` / `count_documents`.
`manifest["integrity"]["mongodb_writes"] == 0`. AST test asserts no `update_*` / `insert_*`
/ `delete_*` / `replace_one` / `bulk_write` / `drop` call exists in the module. (The
inventory-list cache write lives in the **router**, not the engine, so the engine stays
provably write-free — and even that is an operational cache, keyed `full_object_inventory`,
never a data mutation.)

---

## Files changed

| File | Change |
|---|---|
| `backend/cloudinary_cleanup_manifest.py` | **new** — the read-only analysis engine |
| `backend/tests/test_cloudinary_cleanup_manifest.py` | **new** — 31 tests + AST safety asserts |
| `backend/routers/cloudinary_admin.py` | `GET /cleanup-manifest` + `_get_object_inventory` (router-owned cache) |
| `docs/CLOUDINARY_P8_CLEANUP_MANIFEST.md` | this report |

## Not touched / not built (P8 boundary)

No "Delete All" / "Clean Cloudinary" / "Delete Orphans" / "Repair Storage" button. No
POST endpoint. No physical deletion. No re-validation loop, batching, or deletion logging —
**that is P9** and requires separate explicit approval after this manifest is reviewed.
