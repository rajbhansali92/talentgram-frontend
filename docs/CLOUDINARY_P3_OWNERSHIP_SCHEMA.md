# Cloudinary P3 — Media Ownership Model (schema design, confirmed against production)

**Status:** Design + dry-run. Additive and reversible. No physical Cloudinary asset is created, moved, re-uploaded, or deleted. No media URL changes. The migration writes exactly one new nested key per media item and nothing else.

**FOLDER ≠ OWNERSHIP.** Ownership is derived from authoritative application state (`category`, parent document, `scope`, `source_*` fields) — never from the Cloudinary folder string. Confirmed necessary: in production only 418 of 2,527 global-talent assets physically live under `talentgram/talents/…`; the rest sit in submission/application folders as copy-by-value.

---

## 1. Production media schema (measured, `railway run` read-only)

| | talents.media[] | submissions.media[] | applications.media[] |
|---|---|---|---|
| docs / items | 151 / 1,790 | 178 / 2,087 | 50 / 578 |
| always present | `id, category, url, public_id, resource_type, content_type, size, created_at` | same + `original_filename` | same + `original_filename, duration, poster_url` |
| ownership-ish today | `scope` (talent_portfolio/talent), `talent_id` (756), `source_submission_id`+`source_submission_media_id` (1034), `source_application_media_id` (373), `source_talent_media_id` (190) | `scope` (submission/admin_added/whatsapp_media_assignment), `submission_id` (1611), `project_id` (1611), `origin` (global/project/whatsapp), `profile_sync_status`, `source_talent_media_id` (465), `admin_added`/`admin_added_by` (45) | `scope` (application), `application_id` (560) |
| categories | western 777, portfolio 445, indian 435, video 133 | western 798, image 644, indian 425, intro_video 160, **take 54**, photos 5, intro 1 | western 206, image 172, indian 154, intro_video 46 |
| resource_type | image / video only | image / video only | image / video only |

**Findings that shape the design:**
- `scope` already half-encodes ownership but is inconsistent (`talent` vs `talent_portfolio`, 476 nulls) — **not trustworthy as the authoritative field**; used only as a hint.
- `origin` on submissions: `global` 465 (talent's profile media pulled in) vs `project` 1,337 (uploaded for this project). A useful hint but not decisive — a `project`-origin item can still be a global-category photo the talent added.
- All 54 `take*` items have a resolvable `project_id`, `submission_id`, `url`. **0 `take` items on applications** (no conflict).
- **0 media items with no resolvable talent id/email.**
- 508 `public_id`s appear with two "different" categories — every one is `{talents:portfolio, submissions:image}`, which the code already treats as identical (`build_prefill_media` maps `portfolio→image`, `sync_media_to_global_talent` maps `image→portfolio`). **Not a conflict — a label-drift the model normalizes.**
- 1,409 `public_id`s are referenced by >1 owner document — legitimate copy-by-value; flagged `is_shared_copy`.
- `photos` (5) and `intro` (1) are all `scope=whatsapp_media_assignment` — normalize to `image` / `intro_video`.

---

## 2. The `media[i].ownership` sub-document (the only thing P3 writes)

P3 adds **one nested key** — `ownership` — to each media item. It never touches any existing field. Existing code doesn't read `media[i].ownership`, so behaviour is unchanged.

```jsonc
media[i].ownership = {
  "owner_type":   "talent" | "project_submission",   // WHO may cause the Cloudinary asset to be destroyed
  "owner_id":     "<talent_id>" | "<submission_id>",
  "talent_id":    "<talent_id>",                       // always set (best-effort resolved)
  "project_id":   "<project_id>" | null,               // set for project_submission
  "submission_id":  "<id>" | null,
  "application_id": "<id>" | null,
  "media_type":   "image" | "video",                   // from resource_type
  "media_category_normalized": "portfolio|indian|western|intro_video|take",
  "cloudinary": {
    "public_id":     "<verbatim, unchanged>",
    "asset_id":      "<from Cloudinary inventory; null if not resolvable>",
    "resource_type": "image|video",
    "format":        "jpg|png|mp4|…" ,
    "bytes":         <int>
  },
  "is_shared_copy":   true | false,                    // public_id also on another owner doc, OR source_talent_media_id set
  "source_talent_media_id": "<id>" | null,             // mirrored from the item if present
  "owner_source":     "category:take" | "category:global" | "scope" | "origin" | "inference",
  "confidence":       "high" | "medium" | "low",
  "conflict":         null | "<reason>",               // non-null items are NOT auto-assigned; surfaced in the report
  "migrated_at":      "<ISO8601>",
  "migration_version":"p3-v1"
}
```

### Assignment rules

| Location | Rule | owner_type | owner_id | confidence |
|---|---|---|---|---|
| `talents.media[]` | every item | `talent` | `<talent doc id>` | high |
| `submissions.media[]`, category ∈ {take, take_1..3} | audition take | `project_submission` | `<submission id>` | high |
| `submissions.media[]`, other category | talent-owned (copy-by-value or pending sync) | `talent` | resolved `talent_id` | high if `talent_id`/`source_submission_id`/`talent_email`→talent resolvable, else medium |
| `applications.media[]` | every item (no takes exist) | `talent` | resolved `talent_id` | high/medium |
| category `photos` | normalize → `image` (global) | `talent` | resolved | medium |
| category `intro` | normalize → `intro_video` (global) | `talent` | resolved | medium |

**`talent_id` resolution order:** `talents` doc id (for talents.media) → item `talent_id` → parent doc `talent_id` → `resolve_canonical_talent(talent_email)` → `source_submission_id`→submission→talent → `null`.

**`conflict` is set (and owner_type left blank, item excluded from the write) when:**
- a `take*` item is on a non-submission document (0 in prod),
- `owner_type` would be `talent` but no `talent_id` resolves (0 in prod),
- category is unrecognised and not `photos`/`intro`,
- the same `public_id` carries two genuinely different normalized categories on different docs (0 in prod after normalization),
- the item is the 1 known Section-B audition-scheme entry the identity matcher mis-flagged — matcher fixed here so this resolves.

---

## 3. Reversibility

The migration `$set`s `media` (the whole array, per doc) with each item gaining exactly one new key `ownership`. Before writing each doc, its current `media` array is snapshotted to `db.p3_ownership_migration_backup` (`{collection, doc_id, media_before, migrated_at}`).

**Rollback:** `p3_rollback.py` — for every doc in the backup collection, restore `media` from `media_before` (exact), then drop the backup collection. Equivalent lightweight rollback: `$unset` `media.$[].ownership` across the three collections. Either fully reverts P3 with zero residue. No existing field is ever modified, so rollback cannot lose data.

---

## 4. What P3 does NOT do

- No Cloudinary API calls that write (only `resource()` reads to backfill `asset_id`/`format`, and those are cached from the Phase-0/P1 inventory — near-zero live calls).
- No `media.url` / `public_id` changes.
- No physical asset move, copy, re-upload, or delete.
- No deletion of any Mongo document or media item.
- No change to `scope`, `origin`, `profile_sync_status`, `source_*`, or any other existing field.
- No folder restructuring.
