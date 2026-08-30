# Cloudinary Re-architecture — P6: Media Lifecycle & Deletion Safety

**Phase:** P6 — one authoritative, ownership-aware media lifecycle / deletion system
**Branch:** `p6/media-lifecycle-deletion-safety`
**Status:** implemented + tested — **awaiting review/merge approval**
**Hard rule honoured:** ZERO production Cloudinary assets deleted · ZERO bulk cleanup executed

Builds on P3 (the `media[i].ownership` sub-document). Deletion is now decided from the
**MongoDB ownership + reference graph**, never from the Cloudinary folder string.

---

## 1. Lifecycle state model

One central service — **`backend/media_lifecycle.py`** — owns every deletion decision.
No delete path re-implements its own rules.

### States (stored additively, never destructive)

| Where | Field | States |
|---|---|---|
| `media[i].lifecycle.state` | on the media item | `active` (absent == active) · `pending_deletion` · `deleted` |
| `projects` / `submissions` | `lifecycle_state` + `deleted_at` + `deleted_by` | `active` · `deleted` (soft) |
| `talents` | `lifecycle_state` + `archived_at` | `active` · `archived` |
| `db.pending_media_deletions` (**new ledger**) | one row per asset | `pending_deletion` |

`media[i].lifecycle` (PENDING) carries: `marked_at`, `eligible_at`, `retention_days`,
`reason`, `marked_by`. The **ledger** carries the same plus the full owner snapshot
(`owner_type`, `owner_id`, `talent_id`, `project_id`, `submission_id`, `is_shared_copy`,
`conflict`, `url`, `resource_type`) — it is what P8/P9's controlled purge will read.

### Service API (`media_lifecycle`)

| Function | Purpose |
|---|---|
| `classify_owner(media)` | reads `media[i].ownership` (P3). Missing/null/conflict → UNKNOWN. Never reads the folder. |
| `get_dependencies(db, media, ctx)` | every active/historical reference: talents · submissions (live vs soft-deleted) · applications · client-review links · copy-by-value lineage |
| `can_delete(db, media, ctx, retention_days)` | the safety gate → `LifecycleDecision {deletable, state, reason, owner, dependencies, retention_days, eligible_at}` |
| `mark_pending_deletion(db, coll, parent_id, media_id, …)` | advance one item to PENDING. Idempotent — preserves the original clock. |
| `enqueue_pending_deletion(db, media, …)` | idempotent ledger row (used when the owning Mongo doc is itself being removed) |
| `record_owner_teardown(db, media_items, context_kind, context_id, …)` | project/submission delete: split global (skip) vs audition (enqueue) vs unknown (enqueue PROTECTED) vs still-referenced (skip) |
| `delete_if_safe(db, media, ctx, coll, parent_id, destroyer)` | gate → act: `already_deleted` / `no_asset` / `deleted` / `would_delete` / `marked_pending` / `protected` |
| `talent_hard_delete_blockers(db, talent_id)` | list of blocking dependencies; `[]` == safe |
| `resolve_retention_days` / `get_retention_days` | retention policy (below) |

---

## 2. Deletion safety rules (the gate, in order)

`can_delete` returns **not deletable** on *any* of:

1. `media[i].lifecycle.state == "deleted"` → already gone (prevents double-destroy).
2. no `public_id` and no `stream_uid` → nothing to physically delete (caller may drop the DB ref).
3. **ownership unknown or conflicting** (`ownership` missing, `owner_type` null, or `conflict` set) → PROTECT.
4. **any protecting reference** from `get_dependencies` — another talent library, a live
   (non-soft-deleted) submission, any application, an active client-review link, or a
   copy-by-value descendant.
5. **GLOBAL talent media** (`owner_type == "talent"`) — only ever deletable when
   `ctx.talent_hard_delete == owner.talent_id` (i.e. that exact talent is being
   legitimately hard-deleted) **and** rule 4 found nothing. Even then it enters the
   retention window rather than an instant purge (unless retention == 0).
6. **PROJECT audition media** (`owner_type == "project_submission"`) — deletable only when
   a PENDING marker exists whose `marked_at + retention_days` has elapsed **and** rule 4
   found nothing. `retention_days == -1` → never. `retention_days == 0` → immediate.
7. **fall-through → PROTECT** ("no rule authorises deletion").

**Physical deletion is additionally gated by the env flag
`MEDIA_LIFECYCLE_PHYSICAL_DELETE` (default OFF).** While OFF — the entire P6 rollout —
`delete_if_safe` on a "deletable" verdict returns `would_delete` and only advances state
to PENDING; it never calls Cloudinary. The **one exception** is a *failed-upload
rollback* (`ctx.just_uploaded_reject`): an asset created seconds earlier in the same
request that was never persisted to any owner doc — destroying it completes an aborted
transaction, not a deletion of existing media.

---

## 3. Retention behaviour

Config: `db.app_config` document `{"key": "audition_retention_days", "value": <int>}`.

| Configured value | Meaning | Resolution |
|---|---|---|
| *row absent* | — | **30** (documented default) |
| `0` | immediate | 0 |
| `30` | 30-day window | 30 |
| `90` | 90-day window | 90 |
| `-1` | indefinite — never auto-purged | -1 |
| **anything else** (`45`, `-5`, `"x"`, `""`, non-int) | invalid | **-1 (indefinite)** + logged warning — "safe" = err towards keeping the asset |

Project deletion → **soft-delete**, then the retention clock; the asset becomes
*eligible* (not deleted — P6 never deletes) only after it elapses **and** every reference
is gone. P8/P9 build the reaper that acts on eligibility.

---

## 4. Affected endpoints

| Endpoint | Before | After (P6) |
|---|---|---|
| `DELETE /api/projects/{pid}` | hard-deleted project + submissions; `delete_resources_by_prefix("talentgram/projects/{pid}/")` + `delete_folder` (missed `admin_media/`, `submissions/`; could nuke copy-by-value global media) | **soft-delete** project + submissions; `record_owner_teardown` → audition media to the ledger with retention; **global media untouched**; **0 Cloudinary calls** |
| `POST /api/projects/bulk-delete` | same folder-prefix mass-delete per id | same soft-delete + ledger; **0 Cloudinary calls** |
| `DELETE /api/talents/{tid}` | hard-deleted the Mongo doc (no Cloudinary, no dependency check) | **archive by default**; `?hard=true` → `talent_hard_delete_blockers` → **HTTP 409 + blocker list** if any; a permitted hard delete records the talent's now-unreferenced media in the ledger and **never cascade-destroys** |
| `DELETE …/submissions/{sid}` | hard-deleted the Mongo doc; no storage handling | **soft-delete** (historical record kept); `record_owner_teardown` → audition media to ledger; global untouched; **0 Cloudinary calls** |
| `DELETE …/submissions/{sid}/media/{media_id}` | `$pull` only (no ownership eval, no storage) | `$pull` + mirror-pull + `delete_if_safe` → PENDING only if safe; **never a blind destroy** |
| `DELETE /api/admin/cloudinary/talents/{talent_id}` (`delete_talent_assets`) | recomputed a folder slug + `delete_resources_by_prefix` on it (slug drifts from upload time) | `talent_hard_delete_blockers` gate → per-item ledger keyed on the **stored canonical `public_id`**; folder slug never recomputed; **0 destroys** |
| `delete_one_media_item` (used by `delete_talent_auditions` / `…/intro-video` / `…/images/delete`) | own `count_other_references` heuristic + immediate `cleanup_media_storage` + verify | delegates to `delete_if_safe`; always pulls the local ref; physical destroy only when gate-approved **and** flag on |
| `DELETE /api/admin/cloudinary/projects/{project_id}/auditions` | direct `cleanup_media_storage` per take | each take through `delete_if_safe` with retention; reports per-outcome counts |
| `core.safe_cleanup_media_storage` (choke point: talent Library delete, submission/application media delete+replace, webhook cleanup) | `is_media_asset_referenced` check → `cleanup_media_storage` | delegates the *decision* to `media_lifecycle.can_delete`; on a non-physical verdict records ledger intent for audition/deletable assets; global/shared/unknown left completely alone |
| `submissions.py` over-length audition reject | `cloudinary.uploader.destroy(...)` inline | `delete_if_safe(ctx.just_uploaded_reject=True)` — same immediate cleanup, now through the gate |

Frontend: `TalentEdit` delete toast now says **"Talent archived"** on the default path.

---

## 5. Legacy folder handling

* **`classify_owner` never derives ownership from a folder.** It reads `media[i].ownership`
  (P3). A legacy item with no P3 sub-document is UNKNOWN → PROTECTED (the one narrow
  exception: `category ∈ {take, take_1..3}` with no sub-document is treated as
  project-audition, matching P3's own rule — verified by test 17).
* **Legacy folder schemes stay discoverable.** `get_dependencies` matches on the stored
  `public_id`/`stream_uid` verbatim, so an asset under `admin_media/…`, `submissions/…`,
  `talentgram/projects/{pid}/…`, or any historical scheme is still found by the reference
  scan (test 16).
* **Canonical `public_id` is preferred over any recomputed slug** everywhere
  (`delete_talent_assets` no longer builds `{talent_id}_{slug}`; test 17).
* The two folder-prefix mass-deletes (`projects.py`, `cloudinary_admin.delete_talent_assets`)
  are **removed**, not kept as fallback — they are unsafe by construction (folder ≠ ownership).

---

## 6. Dependency checks

`get_dependencies(db, media, ctx)` returns a typed list; `ctx.exclude_collection` /
`exclude_parent_id` drop the one record the caller is removing the media from.

| Reference kind | Protects? |
|---|---|
| another talent's global library | yes |
| a live (not soft-deleted) submission | yes |
| a soft-deleted submission | no — its retention clock governs |
| any application | yes |
| an **active** client-review link surfacing the talent/submission | yes |
| an inactive/expired link | no |
| a copy-by-value descendant (`source_*_media_id`) | yes |

`talent_hard_delete_blockers(db, talent_id)` blocks a hard delete on **any** of:
historical/active project submissions · applications on that email · client-review links ·
casting-pipeline entries · global media still owned. Each blocker is returned with a
count and a human-readable `detail`; a failed probe is itself reported as a blocker
(errs towards ARCHIVE). Empty list == safe.

---

## 7. Tests

**`backend/tests/test_media_lifecycle.py` — 35 passed.** In-memory async Mongo fake
(`mongomock_motor` is absent in this environment). Covers the required matrix:

| # | Test | Result |
|---|---|---|
| 1 | global media survives project removal (copy-by-value in a torn-down submission) | ✅ |
| 2 | global media survives project deletion | ✅ |
| 3 | shared global media stays protected (2nd talent) | ✅ |
| 4 | project audition → PENDING_DELETION | ✅ |
| 5 | retention prevents early deletion | ✅ |
| 6 | retention expiry + no refs → deletable | ✅ |
| 7 | active reference prevents deletion | ✅ |
| 8 | historical protected reference (2nd live submission) prevents deletion | ✅ |
| 9 | unknown ownership prevents deletion | ✅ |
| 10 | conflicting ownership prevents deletion | ✅ |
| 11 | folder path cannot override DB ownership | ✅ |
| 12 | `admin_remove_media_item` uses the lifecycle service | ✅ |
| 13 | `delete_submission` uses the lifecycle service | ✅ |
| 14 | `delete_project` uses the service, no folder-prefix delete | ✅ |
| 15 | `delete_talent` does not blindly destroy global media | ✅ |
| 16 | legacy folder structures remain discoverable | ✅ |
| 17 | canonical `public_id` preferred over recomputed folder slug | ✅ |
| 18 | idempotent `mark_pending_deletion` (clock preserved) | ✅ |
| 19 | repeated deletion request cannot destroy twice | ✅ |
| 20 | Cloudinary destroy never called when the gate fails | ✅ |
| + | retention-value safety table (`45→-1`, `"x"→-1`, absent→30, …) | ✅ |
| + | physical-delete flag OFF → only marks PENDING | ✅ |
| + | failed-upload rollback destroys even with the flag OFF | ✅ |
| + | `record_owner_teardown` splits global vs audition vs no-asset | ✅ |
| + | `talent_hard_delete_blockers` lists dependencies | ✅ |

**Regression sweep — no new failures introduced by P6:**

| Suite | Result |
|---|---|
| `test_media_lifecycle.py` (35) | ✅ |
| `test_storage_console_rebuild.py` (21 — `TestDeleteOneMediaItem` rewritten to the P6 contract) | ✅ |
| `test_p5_delivery_transformations.py` (20) | ✅ |
| `test_p4_no_eager_transformations.py` (48) | ✅ |
| `test_media_classification.py` — P3 Layer 1 (73) | ✅ |
| `test_direct_uploads.py` (23) | ✅ |
| `test_media_assignment.py` (52) | ✅ |
| `test_upload_lifecycle.py` (3) | ✅ |
| `test_talent_merge.py` (11) | ✅ |
| `test_talent_update_media_preservation.py` (1) | ✅ |
| `test_client_payload_isolation.py` (6) | ✅ |
| `test_media_send.py` (41) | ✅ |
| `test_talent_folder_pdf.py` (6) | ✅ |
| combined `p4+p5+p6+p3L1+storage` run | **197 passed** |

Pre-existing failures unrelated to P6 (identical on `main` — live-server integration
tests, `mongomock_motor` absence, R2 env config): `test_p3_media_ownership.py` (Layer 2
collection), `test_media_optional.py`, `test_cloudinary_migration.py`,
`test_client_intelligence.py`, `test_p0_storage_hardening.py::test_get_storage_analytics_fallback`,
`test_casting_review.py::test_newly_surfaced_fields_gating` (`KeyError: 'languages'` — a
client-shape field this phase never touched). Every one of these either fails identically
on clean `main` or cannot be collected there at all.

---

## 8. Confirmation — ZERO production Cloudinary assets deleted

* `media_lifecycle` performs **no Cloudinary API call** unless
  `MEDIA_LIFECYCLE_PHYSICAL_DELETE` is set (it is **not** set anywhere; default OFF).
* Both folder-prefix mass-deletes (`delete_resources_by_prefix` / `delete_folder`) are
  **removed** from `projects.py` and `cloudinary_admin.delete_talent_assets`.
* `core.cleanup_media_storage` (the low-level Stream/R2/Cloudinary destroyer) is now only
  reachable from `safe_cleanup_media_storage` behind the OFF flag, from `delete_if_safe`'s
  injected `destroyer` behind the OFF flag, and from the failed-upload-rollback path
  (brand-new never-persisted asset).
* No migration was run. No asset was moved, copied, re-uploaded, or regenerated.
* Existing UNKNOWN-ownership assets remain PROTECTED (P3 behaviour unchanged).

## 9. Confirmation — ZERO bulk cleanup executed

* No "delete all orphaned assets" capability was added.
* No one-click destructive endpoint was added or re-enabled (`POST …/health/cleanup`
  stays 410 from P0.5).
* `bulk_delete_projects` still exists but now only **soft-deletes** and writes ledger
  rows — it issues no Cloudinary calls.
* The `pending_media_deletions` ledger is **inert in P6** — nothing reads or acts on it.
  P8 builds the dry-run manifest over it; P9 the controlled, per-batch, approved purge.

---

## Files changed

| File | Change |
|---|---|
| `backend/media_lifecycle.py` | **new** — the entire lifecycle/deletion-safety service |
| `backend/tests/test_media_lifecycle.py` | **new** — 35 tests + in-memory Mongo fake |
| `backend/core.py` | `safe_cleanup_media_storage` delegates the decision to `media_lifecycle` |
| `backend/routers/projects.py` | `delete_project` + `bulk_delete_projects` → soft-delete + ledger, folder-prefix mass-delete removed |
| `backend/routers/talents.py` | `delete_talent` → archive default + `?hard=true` blocker gate, no cascade destroy |
| `backend/routers/submissions.py` | `delete_submission` soft-delete + teardown; `admin_remove_media_item` → `delete_if_safe`; over-length reject through the gate |
| `backend/routers/cloudinary_admin.py` | `delete_talent_assets`, `delete_one_media_item`, `delete_project_audition_videos` → lifecycle; folder-prefix slug delete removed |
| `backend/tests/test_storage_console_rebuild.py` | `TestDeleteOneMediaItem` rewritten to the P6 delegation contract |
| `frontend/src/pages-components/TalentEdit.jsx` | delete toast reflects "archived" |
| `docs/CLOUDINARY_P6_LIFECYCLE.md` | **new** — this report |

## Not touched (per P6 constraints)

Ownership model (P3) · Storage Console (P7) · dry-run cleanup (P8) · controlled purge
(P9) · billing verification (P10) · P4/P5 upload & delivery paths · no asset deleted,
moved, re-uploaded, or regenerated · no folder-inferred ownership.
