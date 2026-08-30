# Cloudinary Re-architecture — P7: Storage Accounting + Soft-Delete Query Audit

**Phase:** P7 — corrected storage accounting model + repo-wide soft-delete query audit
**Branch:** `p7/storage-accounting-softdelete-audit`
**Status:** implemented + tested — **awaiting review/merge approval**
**Hard rules honoured:** ZERO Cloudinary assets deleted · ZERO physical cleanup · physical-delete
flag stays OFF · no bulk deletion · read-only scans only

---

## 1. Corrected storage-accounting model

New service **`backend/storage_accounting.py`** + endpoint **`GET /api/admin/cloudinary/accounting`**.
It keeps the two authorities strictly separate and always labelled:

| Bucket | Source of truth | What it reports |
|---|---|---|
| **A. Cloudinary actual storage** | `cloudinary.api.usage()` (cached 5 min) | bytes, object count, `original_objects`, `bandwidth`, `credits` |
| **B. Application media references** | MongoDB `media[].size` sums | **"APPLICATION REFERENCE SIZE — NOT Cloudinary storage"**; deduped by `public_id` (shared copy-by-value counted once) + a raw figure that keeps the duplicates |
| **C. Global talent media** | P3 `media[i].ownership.owner_type == "talent"` | distinct assets + reference bytes |
| **D. Project audition media** | P3 `owner_type == "project_submission"` | distinct assets + reference bytes |
| **E. Derived assets** | `usage().derived_resources` | count (Cloudinary's usage API does not break out derived *bytes*) |
| **F. Orphaned assets** | the last cached `GET /health` full scan only | unreferenced-object count + bytes; `never_run` until a scan happens |
| **G. Unknown / unresolved** | P3 `ownership.conflict` set, or no `ownership` sub-document | distinct assets — always **PROTECTED** |
| **Lifecycle** | `media[].lifecycle.state` + `pending_media_deletions` ledger | active / pending_deletion / deleted / protected / eligible-for-cleanup |
| **Cost** | `usage()` | storage / bandwidth / transformation credits + `used_percent` |
| **Reconciliation** | A − B | the honest gap, explained (derived + orphans + null-size legacy) — never a fabricated per-object split |

**Design rules enforced (tests pin them):**
- A MongoDB `size` sum is never returned under a "Cloudinary storage" key.
- `talents.media[]` **is** counted (the old model's biggest omission), alongside submissions + applications.
- Shared global media is counted once — `distinct_backing_assets`, not `Σ media[]`.
- Ownership is read from the P3 sub-document, **never** inferred from the Cloudinary folder path (test 18: a `public_id` under a project folder whose P3 ownership says "talent" is classified as global).
- Every figure carries a freshness stamp; nothing claims real-time precision it doesn't have.
- The endpoint is **read-only** — it cannot trigger a full inventory scan or any deletion (test 13, 20).

---

## 2. Exact current vs corrected numbers (production, read-only)

| Metric | Old Storage Console | P7 accounting model | Truth source |
|---|---|---|---|
| "Storage" headline | ~**16.0 GB** (submissions + applications `media[].size` only; `talents.media[]` omitted) — the top card sometimes degraded to a near-zero value when `/summary` was unavailable | see split below | — |
| **A. Cloudinary actual storage** | not shown as a distinct authoritative figure | **19.63 GB** | `cloudinary.api.usage()` |
| Cloudinary objects | shown | **12,858** (4,348 originals + **8,510 derived**) | usage API |
| Transformations (period) | shown as credits only | **71,626** | usage API |
| **Credits** | shown | **116.15 / 60 = 193.6 %** of plan | usage API |
| **B. Application reference size (deduped)** | conflated with "storage" | **14.84 GB** across **2,649** distinct backing assets (**1,432** shared copy-by-value) | MongoDB `media[].size` |
| B raw (double-counts shared copies) | — | 25.46 GB | MongoDB |
| **C. Global talent media** | not classified | **2,562 assets / 12.73 GB** ref | P3 ownership |
| **D. Project audition media** | not classified | **54 assets / 1.86 GB** ref | P3 ownership |
| **G. Unknown / conflicting** | not classified | **33 assets / 0.25 GB** — protected | P3 ownership |
| **Lifecycle** | not shown | 2,616 active · 0 pending · 0 deleted · 33 protected · ledger empty | `media[].lifecycle` + ledger |
| **Reconciliation gap (A − B)** | invisible | **≈ 4.79 GB** = derived assets + orphaned originals + null-size legacy refs | computed |
| **F. Orphans** | a full 12k-object scan on **every page load** (~19 s) | only on explicit "Re-Scan"; result cached; surfaced with a `last_scan_at` age | `GET /health` |

The user-reported "~0.02 GB" corresponds to a degraded state of the old top card (empty `/summary`
response → `storage_bytes: 0`); the category aggregation itself reported ~16 GB. Either way it was
wrong: it never counted `talents.media[]` and never distinguished actual Cloudinary storage from a
reference-size estimate.

---

## 3. Cloudinary API calls used

| Call | Where | Frequency | Cost |
|---|---|---|---|
| `cloudinary.api.usage()` | `GET /accounting`, `GET /summary`, `GET /analytics` | **≤ 1 per 5 min** (server-side cache in `db.storage_metrics_cache`) | 1 Admin API request; **no transformation** |
| `cloudinary.api.resources(...)` paginated | `GET /health` **only** | manual "Re-Scan" only | Admin API list requests (~26 pages for 12.8k objects); **no transformation** |

**No `cloudinary.uploader.*`, no `destroy`, no `delete_resources*`, no delivery/transformation URL is
built for the dashboard.** Storage administration uses the Admin/Usage APIs + MongoDB metadata only.

---

## 4. Caching strategy

`db.storage_metrics_cache` — one row per metric, `{key, value, fetched_at}`:

| Key | TTL | Refreshed by |
|---|---|---|
| `cloudinary_usage` | **300 s** | `get_cloudinary_usage()` on a cache miss (or `?refresh=true`); falls back to the **stale cached value** if the live call fails, flagged `stale: true` |
| `full_inventory_scan` | **86,400 s** | written only by `GET /health` (manual Re-Scan); `get_cached_scan()` reports `never_run` / `fresh` / `stale` |

`GET /accounting` returns a `freshness` block: `cloudinary_usage.age_seconds` / `.stale`,
`full_inventory_scan.last_scan_at` / `.status`, `mongodb_aggregation.computed_at` (always live —
the Mongo aggregation is ~0.3 s and runs every call). The frontend renders each figure's age
("updated 2m ago", "scan: never — run a Health Scan").

Repeated dashboard loads within 5 minutes issue **zero** new Cloudinary calls (test 15).

---

## 5. Soft-delete query audit

P6 changed project + submission deletion from `delete_one` to a **soft-delete**
(`lifecycle_state = "deleted"` + `deleted_at`); talent deletion defaults to **archive**
(`lifecycle_state = "archived"`). Pre-P6 those rows vanished; now they persist. Every consumer of
`projects` / `submissions` / `talents` was reviewed and classified.

**Canonical markers:** `lifecycle_state == "deleted"` (projects, submissions) ·
`lifecycle_state == "archived"` (talents). `{"$ne": "deleted"}` matches every pre-P6 row
(field absent) and every `"active"` row.

**Helper:** `core.active_only(query, *, include_deleted=False, exclude_archived=False)` +
`core.NOT_DELETED`. `include_deleted=True` returns the query untouched (admin/historical/accounting).

### Classification

| # | Consumer | Class | Action |
|---|---|---|---|
| 1 | `projects.list_projects` (`GET /api/projects`) | **MUST EXCLUDE** | `active_only` + `?include_deleted=true` opt-in |
| 2 | `submissions.list_submissions` (`GET /projects/{pid}/submissions` — Review Center) | **MUST EXCLUDE** | `active_only` |
| 3 | `submissions.list_all_approved_submissions` (Link picker) | **MUST EXCLUDE** | `active_only` |
| 4 | `submissions.update_talent_submission_metrics` (talent card counts) | **MUST EXCLUDE** | `**NOT_DELETED` |
| 5 | `casting_pipeline` follow-up-lane submission scan | **MUST EXCLUDE** | `active_only` |
| 6 | `links.py` — 14 client-review submission fetches (auto-pull `decision:approved` + curated `id:$in`) across `get_public_link`, link-detail, share, results, bundle-zip, media-proxy | **MUST EXCLUDE** (client-facing render *is* an operational query) | `**_NOT_DELETED` merged into every one |
| 7 | `talents.py` roster list (`GET /api/talents`) | **MUST EXCLUDE** | `active_only(exclude_archived=True)` when no explicit `?status` |
| 8 | `whatsapp.list_projects_for_wa` + `whatsapp` project search picker | **MUST EXCLUDE** | `active_only` |
| — | `cloudinary_admin` storage `/projects`, `/analytics`, `aggregate_project_talent_totals`, `get_storage_health` | **MUST INCLUDE HISTORICAL** (storage accounting — a soft-deleted asset still occupies Cloudinary bytes until P8/P9 purge) | unchanged; deleted projects shown labelled `"Deleted Project"` |
| — | `storage_accounting.aggregate_ownership` | **MUST INCLUDE HISTORICAL** | includes everything, tags `lifecycle_state` per asset |
| — | `media_lifecycle.get_dependencies` | **INCLUDE — but distinguishes** | a live submission protects; a soft-deleted one does not (its retention clock governs) — deliberate |
| — | `pending_media_deletions` ledger reads | **INCLUDE HISTORICAL** | P8/P9 only; inert in P6/P7 |
| — | `projects.list_projects?include_deleted=true` · `talents?status=archived` | **ADMIN-ONLY MAY INCLUDE** | explicit opt-in params |
| — | `find_one({"id": sid, "project_id": pid})` in ~30 submission action endpoints (decision, add-media, snapshot, …) | **UNKNOWN → reviewed, no change** | acting on a specific record by exact id; a soft-deleted record still resolves for admin inspect/repair. A future guard could 409 on soft-deleted; not required for P7. Documented. |
| — | `auth.py` talent-portal submission lookup by `(project_id, talent_email)` | **UNKNOWN → reviewed, no change** | if the project is soft-deleted the talent has no active workflow there; edge case, low risk |
| — | `server.py:410` one-time startup metrics backfill | **UNKNOWN → reviewed, no change** | one-shot job; the live path (`update_talent_submission_metrics`, #4) is fixed |
| — | `agents/modules/media_assignment.py` submission fetch | **UNKNOWN → follow-up** | WhatsApp media-assignment agent; flagged for a dedicated pass |

### Queries changed (files)

`backend/core.py` (helper) · `backend/routers/projects.py` (`list_projects`) ·
`backend/routers/submissions.py` (`list_submissions`, `list_all_approved_submissions`,
`update_talent_submission_metrics`) · `backend/routers/casting_pipeline.py` (follow-up lane) ·
`backend/routers/links.py` (14 client-review fetches, `_NOT_DELETED` import) ·
`backend/routers/talents.py` (roster list) · `backend/routers/whatsapp.py` (2 project pickers).

---

## 6. Historical-query exceptions (explicit)

| Access | Mechanism |
|---|---|
| Admin: see soft-deleted projects | `GET /api/projects?include_deleted=true` |
| Admin: see archived talents | `GET /api/talents?status=archived` |
| Storage accounting: all assets regardless of lifecycle | `GET /api/admin/cloudinary/accounting` (labels each asset's `lifecycle_state`); `.../analytics`, `.../projects`, `.../health` |
| Retention / purge candidates | `db.pending_media_deletions` ledger (P8/P9) |
| Audit of a specific record | `find_one({"id": ...})` by exact id still resolves |

A soft-deleted submission **stops rendering in client review links** (decision: client link
rendering is an operational query; the historical record persists in the DB for audit/billing).
An **archived talent still renders in a link that explicitly references them** (explicit
curation wins — only the *roster* filters archived).

---

## 7. Security audit (Objective 6)

| Check | Result |
|---|---|
| Admin authorization on every storage endpoint | ✅ all 24 `cloudinary_admin` routes carry `Depends(require_role("admin"))` — including the new `/accounting` |
| Cloudinary credentials never reach the frontend | ✅ `_shape_usage` whitelists fields; `usage()` / `resources()` responses contain no secret; test 14 asserts `api_key`/`api_secret`/`cloudinary://` and the literal secret are absent from the payload |
| Cloudinary Admin API calls backend-only | ✅ all in `run_in_threadpool`-wrapped sync helpers server-side |
| Raw Cloudinary responses not over-exposed | ✅ `/accounting` returns a shaped model; `/health` returns public_ids + public delivery URLs only (fine for admin) |
| Storage scans read-only | ✅ `/accounting` and `/health` contain no `destroy`/`delete_resources`/`uploader` call |
| No endpoint triggers physical deletion | ✅ verified by grep; `/accounting` is a 2-line read wrapper |
| No frontend action can enable `MEDIA_LIFECYCLE_PHYSICAL_DELETE` | ✅ the flag is **only ever read** (`os.environ.get`) in `media_lifecycle.py` / `core.py`; no endpoint or code path sets it; only test code uses `monkeypatch.setenv` |

---

## 8. Performance considerations

- **No full inventory scan on page render.** `/accounting` = 1 cached `usage()` call (≤ 1 per 5 min)
  + a single-pass MongoDB aggregation over `media[]` (~0.3 s on production data). The 12.8k-object
  listing runs **only** on an explicit "Re-Scan" (`GET /health`), and its result is cached for 24 h
  so `/accounting` can surface orphan counts + scan age without re-listing.
- The MongoDB aggregation unwinds `media[]` across 3 collections (~4,500 items) — cheap, indexed on
  `_id`, streamed to a list once.
- Stale-cache fallback: if `usage()` fails, the last good value is served flagged `stale: true`
  rather than zeros or an error (test).
- Freshness is always displayed, so an admin never mistakes a 4-minute-old figure or a week-old
  scan for live data.

---

## 9. Storage Health Scan improvements (Objective 8) — still read-only

`GET /health` now:
- includes **`applications.media[]`** in the referenced set (was missing — a real bug),
- classifies every Cloudinary physical object: `referenced` / `orphan` / `derived_variant`
  (a delivery URL still carrying a transformation segment is a derived variant, not a standalone
  orphan original) — with counts **and bytes** per class,
- rolls up the **referenced** assets by P3 ownership (`global_talent_media` / `project_audition_media`
  / `unknown`) and by `lifecycle` (`active` / `pending_deletion` / `deleted`),
- reports the `pending_media_deletions` ledger count,
- renames "duplicate" → `shared_public_id_count` (it is legitimate copy-by-value per P3, not a bug;
  old keys kept as back-compat aliases),
- stamps `scanned_at` and `read_only: true`,
- **caches a compact summary** for `/accounting`.

It still **produces a report and deletes nothing.**

---

## 10. Tests

**`backend/tests/test_storage_accounting.py` — 24 passed.** In-memory async Mongo fake (aggregate +
find + upsert). Covers the required 1–20 matrix:

| # | Assertion | ✅ |
|---|---|---|
| 1 | Cloudinary bytes shown as Cloudinary storage | ✅ |
| 2 | MongoDB size **not** labelled Cloudinary storage | ✅ |
| 3–5 | `talents` + `submissions` + `applications` media all counted | ✅ |
| 6 | derived assets distinguishable (`derived_objects` vs `original_objects`) | ✅ |
| 7–8 | orphan / unknown distinguishable | ✅ |
| 9 | global talent media not classified as project media | ✅ |
| 10 | project audition media not classified as global | ✅ |
| 11 | soft-deleted project excluded from `list_projects` (uses `active_only` + `include_deleted`) | ✅ |
| 12 | historical query can `include_deleted` | ✅ |
| 13 | accounting is read-only (doc counts unchanged) | ✅ |
| 14 | no credentials in the payload | ✅ |
| 15 | repeated loads within TTL → 1 Cloudinary call; refetch after TTL | ✅ |
| 16 | freshness (age / ttl / stale / scan status) exposed | ✅ |
| 17 | shared global media not double-counted | ✅ |
| 18 | classification uses P3 ownership, not the folder path | ✅ |
| 19 | pending deletion visible in lifecycle + ledger, not deleted | ✅ |
| 20 | reconciliation gap explained, no fake precision | ✅ |
| + | retention/usage stale-cache fallback; `active_only` variants; client-link query filter; `list_projects` wiring | ✅ |

**Regression sweep (per-file, isolation) — no new failures:**
`test_media_lifecycle` (35) · `test_storage_console_rebuild` (21) · `test_media_classification` (73) ·
`test_p4_no_eager_transformations` (48) · `test_p5_delivery_transformations` (20) ·
`test_direct_uploads` (23) · `test_media_assignment` (52) · `test_upload_lifecycle` (3) ·
`test_talent_merge` (11) · `test_talents_roster_scale` (2) · `test_talents_tagging` (34) ·
`test_client_payload_isolation` (6) · `test_casting_agent` (209) · `test_whatsapp_campaign_agent` (189) ·
`test_talent_folder_pdf` (6). Frontend `vitest` — **315 passed**.

Pre-existing failures identical on `main` (unrelated to P7): `test_talent_search_agent` ×2 (MOVE-trigger
NLU), `test_casting_review::test_newly_surfaced_fields_gating` (`KeyError: 'languages'`),
`test_p3_media_ownership` Layer 2 (`mongomock_motor` absent), live-server integration suites.

---

## 11. Confirmation — ZERO asset deletion

- `storage_accounting.py` makes **no Cloudinary write call** of any kind.
- `GET /accounting` and `GET /health` contain no `destroy` / `delete_resources*` / `uploader.*`.
- No existing deletion endpoint was made *more* destructive; the health scan gained classification
  detail only.
- `MEDIA_LIFECYCLE_PHYSICAL_DELETE` remains unset in production (verified read-only) and is
  untouched by P7.

## 12. Confirmation — ZERO physical cleanup

- No bulk deletion, orphan deletion, derived-asset deletion, `asset_metadata` cleanup, or physical
  migration was added or run.
- `POST /health/cleanup` stays disabled (410, from P0.5).
- The `pending_media_deletions` ledger is still **inert** — P7 only *reads* it for the accounting
  lifecycle rollup. P8 builds the dry-run manifest over it; P9 the controlled purge.

---

## Files changed

| File | Change |
|---|---|
| `backend/storage_accounting.py` | **new** — the corrected A–G accounting model + caching |
| `backend/tests/test_storage_accounting.py` | **new** — 24 tests + in-memory Mongo fake |
| `backend/routers/cloudinary_admin.py` | new `GET /accounting`; `/health` gains applications + classification + ownership/lifecycle rollup + scan cache |
| `backend/core.py` | `NOT_DELETED`, `active_only()` soft-delete helper |
| `backend/routers/projects.py` | `list_projects` excludes soft-deleted (+ `?include_deleted`) |
| `backend/routers/submissions.py` | `list_submissions`, `list_all_approved_submissions`, `update_talent_submission_metrics` |
| `backend/routers/casting_pipeline.py` | follow-up-lane submission scan |
| `backend/routers/links.py` | 14 client-review submission fetches exclude soft-deleted |
| `backend/routers/talents.py` | roster list excludes archived/deleted |
| `backend/routers/whatsapp.py` | 2 project pickers exclude soft-deleted |
| `frontend/src/pages-components/StorageDashboard.jsx` | new "Storage Accounting" panel (A–G + freshness), reads `/accounting` |
| `docs/CLOUDINARY_P7_STORAGE_ACCOUNTING.md` | **new** — this report |

## Not touched (per P7 constraints)

Physical deletion (stays OFF) · P8 dry-run cleanup · P9 controlled purge · the `pending_media_deletions`
ledger (read-only here) · P3 ownership model · P4/P5 upload & delivery · no asset deleted, moved,
re-uploaded, or regenerated.
