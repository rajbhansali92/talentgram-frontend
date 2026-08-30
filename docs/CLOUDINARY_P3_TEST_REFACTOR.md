# P3 Ownership Test Suite — Refactor Report

**Scope:** test refactoring + minimal testability refactoring only. **No** business logic, classification rules, `owner_type` values, migration behavior, deletion behavior, Cloudinary behavior, media schema, or P4/P5 behavior changed. Equivalence proven: **5,040 input combinations → byte-identical output** old vs new `classify_item` (with the timestamp stamp pinned).

PR [#4](https://github.com/rajbhansali92/talentgram-frontend/pull/4), commits `698c8f0` → `15dbce5`. All CI checks green.

---

## 1. Where `classify_item()` lives

**Now:** `backend/migrations/media_ownership_rules.py` — a new, pure, side-effect-free module.
**Was:** defined inline in `backend/migrations/p3_media_ownership.py` (which imports `core`).

`p3_media_ownership.py` now imports `classify_item`, `folder_disagrees`, `VERSION`, and the category constants from the pure module; its DB / talent-resolution / write / rollback code is unchanged.

## 2. Exact input/output contract

```python
classify_item(coll, parent, item, talent_id, how_tid,
              pid_owner_count, pid_norm_cats, *, now=None) -> dict
```

| Arg | Type | Meaning |
|---|---|---|
| `coll` | `"talents"` \| `"submissions"` \| `"applications"` | collection the `parent` doc lives in |
| `parent` | dict | parent document — reads `id`, `project_id` only |
| `item` | dict | the media item — reads `category`, `public_id`, `resource_type`, `content_type`, `submission_id`, `asset_id`, `format`, `size`/`bytes`, `source_talent_media_id` |
| `talent_id` | str \| None | the resolved talent id (resolved by the **caller**) |
| `how_tid` | str | how `talent_id` was resolved — only used to set `confidence` |
| `pid_owner_count` | `Counter` | `public_id` → number of media items referencing it (for `is_shared_copy`) |
| `pid_norm_cats` | dict | `public_id` → `set` of normalized categories seen (for the conflict check) |
| `now` | str \| None *(keyword-only)* | ISO-8601 to stamp `migrated_at`; default = live UTC. **Does not affect classification.** |

**Returns** the `ownership` sub-document (17 keys): `owner_type`, `owner_id`, `talent_id`, `project_id`, `submission_id`, `application_id`, `media_type`, `media_category_normalized`, `cloudinary{public_id, asset_id, resource_type, format, bytes}`, `is_shared_copy`, `source_talent_media_id`, `owner_source`, `confidence`, `conflict`, `migrated_at`, `migration_version`.

**`owner_type` ∈ `{"talent", "project_submission", None}`.** `owner_type is None` **iff** `conflict is not None` (UNKNOWN — the caller must not write an owner; the migration reports it).

**Conflict reasons (5):**
1. `"take-category item on {coll} (only submissions may own audition takes)"`
2. `"unrecognised category {cat!r}"`
3. `"public_id {pid} carries conflicting normalized categories [...]"`
4. `"take item on a submission with no project_id"`
5. `"talent-owned item with no resolvable talent_id"`

**Precedence (authoritative — the Cloudinary folder path is NEVER read):**
1. Forced-conflict pre-conditions, checked in order (`elif` chain):
   a. take-category on a non-`submissions` document → b. unrecognised category → c. same `public_id` carrying >1 normalized category.
2. Otherwise `category` **alone** picks `owner_type`:
   - `take*` → `project_submission` (`owner_id` = submission id); then if the submission has no `project_id` → conflict #4.
   - anything else → `talent` (`owner_id` = the resolved `talent_id`); then if no `talent_id` → conflict #5.
3. Reference availability (`talent_id` / `project_id`) can only **demote** to a conflict. It never flips `owner_type`, and ownership is **never inferred** from unrelated fields (`source_talent_media_id`, `submission_id`, folder path).

`confidence`: `"high"` normally; `"medium"` when `talent_id` was resolved indirectly (`how_tid ∈ {talent_email, source_submission, source_submission_email}`) **or** the category was normalized (`photos`/`intro`); `None` on conflict.

`folder_disagrees(ownership) -> dict | None`: **reports** (never enforces) a Cloudinary-folder vs DB-owner mismatch — proof that `classify_item` doesn't consult the folder.

## 3. What was changed to make it independently testable

**Yes, `classify_item` required refactoring** to be safely importable and pure:

| Problem (before) | Fix |
|---|---|
| Lived in `p3_media_ownership.py` → `from core import db, …` at module level → `import core` hard-reads **8 env vars** (`MONGO_URL`, `DB_NAME`, `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `CLOUDINARY_*`), instantiates an `AsyncIOMotorClient`, calls `cloudinary.config(...)`, and `load_dotenv(backend/.env)`. Old tests worked around it with `import core; core.db = None` + a dependency on `backend/.env` existing. | Extracted to `media_ownership_rules.py` — **stdlib only** (`datetime`, `collections`). Asserted in a test: importing it loads none of `core` / `cloudinary` / `motor` / `pymongo` / `dotenv`. The Layer 1 suite passes under `env -i` (empty environment). |
| **Non-deterministic**: `"migrated_at": datetime.now(...)` — two calls with identical input returned different dicts. | Added optional keyword-only `now=`. Default preserves the live-timestamp behavior exactly (the migration calls it without `now`); tests pass a fixed value. `migration_version` is a constant. |
| `owner_type` as bare string literals scattered through the function. | Named constants `OWNER_TYPE_TALENT` / `OWNER_TYPE_PROJECT_SUBMISSION` (identical string values). |

Already true, now **tested**: the function does not mutate any argument, does no DB / Cloudinary / filesystem / network / logging.

## 4. New pure unit test count

**73** — `backend/tests/test_media_classification.py` (Layer 1). Runs in ~0.14s. No DB, no Cloudinary, no migration, no `import core`, no mocking of any of them.

Coverage (task cases A–L + §5 precedence + extras):

| | Tests |
|---|---|
| A/C global talent media (on talents doc; carried on a submission) | 2 + `test_every_non_take_category_is_talent_owned` (parametrized ×18 categories) |
| B/D project audition take (+ every take category parametrized) | 2 + parametrized ×4 |
| E conflict — all 5 reasons + the `portfolio`/`image` non-conflict | 6 |
| F missing references — talent, project; **not inferred from `source_talent_media_id` / `submission_id`** | 4 |
| G abandoned draft → UNKNOWN (application + submission variants) | 2 |
| H FOLDER ≠ OWNERSHIP — both directions + "same inputs, 3 different folder strings → identical decision" + `folder_disagrees` | 5 |
| I shared global media stays talent-owned (`pid_owner_count`; `source_talent_media_id`; not-shared) | 3 |
| J audition take never becomes global even with `talent_id` present (+ parametrized ×4) | 2 |
| K input immutability (deepcopy assert on parent/item/counters; return not an alias) | 2 |
| L determinism (call twice → identical; only `migrated_at` varies without `now`) | 2 |
| §5 precedence boundaries (a>c, b>c, category-before-reference, global-missing-talent-never-project, confidence-by-resolution ×6) | 5 |
| category normalization (`photos`/`intro` → medium, still talent) | parametrized ×2 |
| output shape (`media_type`, `cloudinary` block mirror, `bytes` fallback, conflict still shaped) + residual branches | 6 |

## 5. Existing integration test count

**8** — `backend/tests/test_p3_media_ownership.py` (Layer 2), **rewritten**. Was 11 tests that hand-mocked the DB and only exercised `classify_item` (i.e. they were mislabeled unit tests). Now uses `mongomock-motor` (in-memory async Mongo, no network) and tests the **migration plumbing only**:

1. ownership written to every media item (+ report totals)
2. abandoned draft left UNKNOWN, not guessed (+ appears in the report's conflict list)
3. snapshot-before-write — backup collection holds the pre-migration array
4. idempotency — 2nd `--apply` run: 0 assigned, all skipped, 0 docs touched, backup not doubled
5. dry-run writes nothing (but still computes the full report)
6. rollback restores byte-for-byte + drops the backup collection
7. migration alters **no** existing field (before == after minus `ownership`)
8. take never becomes talent-owned end-to-end

Also kept (adjacent, unchanged): `test_talent_update_media_preservation.py` (1), `test_storage_health_cleanup_disabled.py` (2).

## 6. Complete test results

Run **individually** (the repo's convention — the suite cannot be collected as a whole on any checkout: `ModuleNotFoundError: core`, `REACT_APP_BACKEND_URL not set`):

| File | Result |
|---|---|
| `test_media_classification.py` **(new, Layer 1)** | **73 passed** |
| `test_p3_media_ownership.py` **(rewritten, Layer 2)** | **8 passed** |
| `test_talent_update_media_preservation.py` | 1 passed |
| `test_storage_health_cleanup_disabled.py` | 2 passed |
| `test_storage_console_rebuild.py` | 21 passed |
| `test_talents_tagging.py` | 34 passed |
| `test_media_assignment.py` | 52 passed |
| `test_upload_lifecycle.py` | 3 passed |
| `test_direct_uploads.py` | 23 passed |
| `test_p0_storage_hardening.py` | 3 passed, **1 failed** |
| `test_cloudinary_migration.py` | 1 passed, **1 failed + 7 errors** |

**Pre-existing failures (verified identical on `main` without this branch):**
- `test_p0_storage_hardening.py::test_get_storage_analytics_fallback` — a `MagicMock` used in an `await` in the archived-storage aggregate path. Unrelated to P3 (that file's other 3 tests pass).
- `test_cloudinary_migration.py` — a **live HTTP integration test** (`import requests`; `BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://…emergentagent.com")`). Needs a running backend + real admin login. Not runnable in this environment; `main` shows the same `1 failed, 1 passed, 7 errors`.

New/changed test files add **0** new failures.

## 7. Uncovered classification branches

No `coverage`/`pytest-cov` is installed in this environment, so this is by manual branch-to-test mapping (the 4 "residual branch coverage" tests in commit `15dbce5` were added specifically to close the gaps found in that pass):

- `classify_item`: every branch of the pre-conflict chain, the take/global split, both demote-to-conflict paths, the `owner_source` verbatim-vs-normalized ternary, the `confidence` high/medium/None assignment (including normalized-category + already-medium so it isn't double-demoted), the conflict-clears-owner block, `is_shared_copy` (via `source_talent_media_id`, via `pid_owner_count`, neither, and `public_id` missing), and every output-dict conditional (`project_id`, `submission_id`, `application_id`, `media_type`, `confidence`) is exercised by a named test.
- `folder_disagrees`: talent-in-project-folder, project-in-talents-folder, folder-matches (None), non-`talentgram` public_id (None), empty `cloudinary` block (None), single-segment public_id (None).

**Believed 100% branch coverage of both functions.** Recommend adding `pytest-cov` to the dev deps to make this measured rather than asserted — flagged, not done (out of scope: no new packages).

## 8. Classification ambiguity discovered

**None.** The rules are internally consistent and the precedence is unambiguous. One design note (not a bug):

- `classify_item` fully trusts the caller's `talent_id` + `how_tid`. Talent-id resolution lives in the migration's `_resolve_talent_id`, which uses `core.resolve_canonical_talent()` — the single canonical, dedup-safe email lookup every live entry point uses. So a wrong-talent assignment would be a resolver bug, not a classifier bug; `classify_item` correctly does not second-guess the resolution, and it correctly refuses (conflict #5) when the resolver returns `None`. This is the right separation of concerns and is exactly what keeps the 11 abandoned-draft records UNKNOWN.

No production rule was changed. Nothing to escalate.
