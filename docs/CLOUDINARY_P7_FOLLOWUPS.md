# Cloudinary Re-architecture — P7 Follow-ups

Two items flagged in the P7 report, resolved before P8. No Cloudinary asset
deleted, moved, re-uploaded, or regenerated. No physical deletion.

---

## Follow-up 1 — `agents/modules/media_assignment.py` soft-delete audit

**Classification: MUST EXCLUDE SOFT-DELETED.**

Two submission queries were reviewed:

| Function | Query | Purpose | Decision |
|---|---|---|---|
| `resolve_authoritative_talent_for_upload` | `find({project_id, talent_id: $in})` | **write-path assist** — locates the submission a WhatsApp-delivered audition asset should be attached to | **exclude** — attaching fresh media to a soft-deleted submission is never correct (it would not render in client links and would be retention-purged). A soft-deleted submission must read as "not found" here. No historical-access case applies to a write path. |
| `_filter_stale_uploaded_assignments` (`...:614`) | `find_one({talent_id, project_id})` | cross-checks assignment-tracking rows against the submission's actual media | **exclude** — kept consistent with the resolver above; a soft-deleted submission's assignments are moot. |

Both now wrap their filter in `core.active_only(...)`. `test_media_assignment.py`
(52) + `test_media_send.py` (41) green.

---

## Follow-up 2 — the ownerless production assets

At the time of the P7 report, `GET /accounting` showed **33** distinct assets with
no P3 `media[i].ownership` sub-document. A fresh read-only investigation
(`scratchpad/p7f2_ownerless.py`, `railway run`) found **44** (more talent-media
was added between the two reads).

### Itemized classification

| Bucket | Count | Evidence |
|---|---|---|
| **likely global talent** | **44** | **every one lives directly in `talents.media[]`** — global by definition under the P3 rule (an item in `talents.media[]` is always `owner_type: "talent"`, regardless of category or folder). All 44 have a `talent_id` = the parent doc id. Categories: `western` / `indian` / `portfolio` / `video`. |
| likely project audition | 0 | — |
| legacy / unknown | 0 | — |
| insufficient evidence | 0 | — |

Cause: these were added **after** the P3 backfill (2026-08-30 timestamps) via
`talents.add_media`, `sync_media_to_global_talent`, and
`submission_complete_upload` — none of which set `ownership` at creation.

### Additive P3 enrichment (applied)

Because ownership is **provable from authoritative MongoDB references** for all 44
(they are `talents.media[]` items → talent-owned, high confidence — folder never
consulted), the existing, tested, reversible P3 migration was re-run:

```
railway run python3 backend/migrations/p3_media_ownership.py --apply
```

It is idempotent — only items lacking `ownership.migration_version == "p3-v1"`
are processed:

```
media_items_total ................ 4499
items_skipped_already_migrated ... 4455
items_assigned_owner_type ........ 44   → all GLOBAL_TALENT_MEDIA, confidence HIGH
items_conflict_left_unassigned ... 0
remaining_UNKNOWN_conflict ....... 0
documents_touched ................ 2
folder_vs_db_disagreements ....... 2   (folder says projects/applications; DB says talent — DB wins, by design)
```

Each touched document's `media` array was snapshotted to
`db.p3_ownership_migration_backup` before the write (exact rollback via
`--rollback`). Nothing was guessed: 0 conflicts, 0 items requiring inference.

**Post-enrichment: `GET /accounting` ownership → 0 unknown / 0 conflicting.**
Production now has an `ownership` sub-document on **all 4,499** media items.

### Write-path fixes (prevent the gap re-opening)

`ownership` is now set at creation, using a new pure wrapper
`migrations.media_ownership_rules.ownership_for_new_item()` (builds the trivial
single-item versions of the migration-scoped `pid_owner_count` / `pid_norm_cats`
args and calls the same `classify_item`):

| Write path | Change |
|---|---|
| `routers/talents.py` `add_media` | classifies the new `talents.media[]` item |
| `core.py` `sync_media_to_global_talent` | `ownership` (+ `lifecycle`) added to `MEDIA_COPY_EXCLUDE_FIELDS` so the SOURCE's ownership isn't carried into the Library mirror; the mirror is re-classified as talent-owned |
| `routers/submissions.py` `submission_complete_upload` | classifies the new `submissions.media[]` item (takes → project_submission, else talent) |

Remaining un-instrumented write paths (`attach_video_media`, multipart
`submission_upload`, `admin_add_media`, the application `/complete` handlers) are
lower-volume and every item they create is P3-migratable — re-running the P3
migration (idempotent) sweeps up anything they miss. Flagged as a minor
follow-up; not blocking P8.

### If ownership could NOT be proven

It couldn't fail here (all 44 were `talents.media[]`), but the rule stands: an
item `classify_item` cannot resolve returns `owner_type: null` + a `conflict`
string, is stored verbatim, and reads as **UNKNOWN → PROTECTED** everywhere
(`can_delete`, accounting, the P8 manifest). Never guessed, never folder-inferred.

---

## Files changed

| File | Change |
|---|---|
| `backend/migrations/media_ownership_rules.py` | `ownership_for_new_item()` write-path wrapper |
| `backend/routers/talents.py` | `add_media` sets `ownership` |
| `backend/routers/submissions.py` | `submission_complete_upload` sets `ownership` |
| `backend/core.py` | `sync_media_to_global_talent` re-classifies the mirror; `MEDIA_COPY_EXCLUDE_FIELDS` += `ownership`, `lifecycle` |
| `backend/agents/modules/media_assignment.py` | 2 submission queries → `active_only` |
| `backend/tests/test_media_classification.py` | +4 `ownership_for_new_item` tests (77 total) |
| `docs/CLOUDINARY_P7_FOLLOWUPS.md` | this report |

## Confirmations

- **ZERO Cloudinary writes.** The P3 migration makes no Cloudinary API call; the
  write-path changes only add a nested dict to a media item.
- **The only MongoDB writes** are the additive `media[i].ownership` sub-document
  (44 items, 2 docs) + its pre-write backup — exactly the P3 migration's
  documented, reversible behaviour.
- Physical-delete flag untouched (OFF). `pending_media_deletions` ledger untouched.
