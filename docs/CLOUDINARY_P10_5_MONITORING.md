# P10.5 — Post-P5 Production Monitoring (OBSERVATION PHASE)

**Generated 2026-08-31.** This phase is **observation, not modification.** No Cloudinary
writes / deletes / uploads / transformation-generating requests. No MongoDB writes. No P9 Batch 4.
`MEDIA_LIFECYCLE_PHYSICAL_DELETE` stays OFF. P11 does not begin.

The Day-0 snapshot is `docs/p10_5_snapshots/day_20260831.json`. Future daily snapshots land in
the same folder as `day_YYYYMMDD.json`.

---

## A. Current post-P5 baseline (Day 0)

`cloudinary.api.usage()` captured **2026-08-31 08:22 UTC**. Cloudinary
`last_updated: 2026-08-30`, `date_requested: 2026-08-31T00:00:00Z` — **the data still lags ~1 day
and is essentially pre-P5** (P5 deployed 2026-08-30 14:09 UTC).

| Metric | Day 0 | vs P10 report (24 h earlier) |
|---|---|---|
| Plan | `Small PAYG`, **limit 60.00 credits/mo** | — |
| **Total credits** | **115.44 / 60 → 192.4%** | 115.84 (−0.40) |
| — transformation credits | **71.25** | 71.25 (flat) |
| — storage credits | **17.86** | 18.26 (**−0.40** — P9 deletions landing) |
| — bandwidth credits | 26.33 | 26.33 (flat) |
| Storage bytes | 19,175,407,932 (**17.86 GiB**) | 19,604,077,694 (**−428 MB** — P9 f_mp4 deletions now in `usage()`) |
| Original resources | 4,350 | 4,350 |
| **Derived resources** | **8,383** | 8,416 (**−33**) → `usage()` has now **fully caught up** on all 128 P9 deletions (8,511 P8.5 baseline − 128 = 8,383) |
| Objects | 12,733 | 12,766 |
| Transformation-unit breakdown | `transformation` 10,804 · `sd_video_second` 21,555 · `hd_video_second` 591 · `extra_avif_mp_encoding` 11,304 · `fourk_video_second` 1,165 · `frame` 1 | **all unchanged** |
| `transformations.usage` (rolling ~30 d) | 71,252 | 71,252 |

**Read the two movements correctly:**
- The **−0.40 storage credit / −428 MB / −33 derived** is the **P9 deletion work landing in the
  usage API** — a real, measured reduction, not a P4/P5 effect.
- The **transformation credit (71.25) and every transformation-unit line are UNCHANGED** — because
  those counters still describe pre-P5 activity. **This 71.25 must NOT be used to judge P4/P5.**

**Baseline for the monitoring series:** transformation credits **71.25**, transformation units
`transformation=10,804 / sd_video_second=21,555 / extra_avif_mp_encoding=11,304`, derived count
**8,383**, storage **17.86 credits**, total **115.44 credits**, Cloudinary `last_updated 2026-08-30`.

---

## B. Daily measurement mechanism (PROPOSED — not yet deployed)

Per the "present code before implementing" rule, this is the **proposal**. It is a standalone
read-only script; nothing is scheduled or committed until you approve.

### B.1 What it does — each run

1. `cloudinary.api.usage()` → record: `captured_at`, `cloudinary_last_updated`,
   `cloudinary_date_requested`, `plan`, `credits{usage,limit,used_percent}`, `storage.credits_usage`,
   `storage.usage` (bytes), `bandwidth.credits_usage`, `bandwidth.usage` (bytes),
   `transformations.credits_usage`, `transformations.usage` (rolling), `transformations.breakdown`
   (all 6 unit lines), `resources` (originals), `derived_resources`, `objects`, `requests`.
2. **New-derivative tracker** — list the *newest* N originals per resource type
   (`resources(direction=desc)`), plus every parent public_id in live Mongo `media[]`; for each,
   `resource(pid).derived[]`; keep every derived asset whose `created_at` ≥ **the previous
   snapshot's `captured_at`** (not since P5 — since *last measurement*). For each new one record:
   `parent_public_id`, `parent resource_type`, `transformation string`, `transformation family`,
   `bytes`, `created_at`, and the classification (see §C).
3. **fl_attachment instrumentation** — record `derived_resource_count` and the `transformation`
   unit line specifically, so a download-driven delta is visible (see §D).
4. Diff against the previous `day_*.json`: `Δ transformation_credits`, `Δ storage_credits`,
   `Δ bandwidth_credits`, `Δ total_credits`, `Δ derived_count`, `new_derived_count`,
   `new_derived_bytes`, `new_transformation_families`.
5. Write `docs/p10_5_snapshots/day_YYYYMMDD.json` and append a one-line row to
   `docs/p10_5_snapshots/SERIES.csv`.

### B.2 Read-only guarantees (asserted in the script)

- No `uploader.*`, no `destroy`, no `delete_*`, no eager/incoming transform, **no transformed-URL
  GET** (only Admin API `usage` / `resources` / `resource` / `transformations` reads).
- MongoDB: `find` / `count_documents` only.
- `MEDIA_LIFECYCLE_PHYSICAL_DELETE` is never read or set.

### B.3 How it runs — options for you to choose

| Option | What it is | Pros | Cons |
|---|---|---|---|
| **B3a (recommended)** | A scratch script run **manually once a day** via `railway run python3 …` (or by me on request) | zero repo change, zero infra, fully controlled | needs a person to remember |
| B3b | A committed `scripts/cloudinary_daily_usage.py` + a Railway cron (`0 9 * * *`) writing to a `cloudinary_usage_history` Mongo collection | automatic, durable history | **adds code + a MongoDB collection (a write)** — needs its own approval as a code change |
| B3c | A GitHub Action (`schedule:` cron) committing the daily JSON back to `docs/p10_5_snapshots/` | automatic, history in git, no DB write | adds a workflow file + a bot commit loop |

**Nothing in B3b/B3c is done without your explicit approval** — they involve committed code
and/or a new write path. The Day-0 snapshot already exists via B3a.

---

## C. Transformation-generation table — how each new derivative is classified

For every derived asset found with `created_at ≥ previous snapshot`:

| Field | How it's determined |
|---|---|
| `transformation` / `family` | from `derived[].transformation`, digits → `N` for the family |
| `parent_public_id`, `parent resource_type` | the `resource()` call's public_id + `resource_type` |
| **was REQUIRED?** | matches one of T1–T5 exactly (see below) → REQUIRED; anything else → **flag for review** |
| **generated by current P4/P5 code?** | the string is one the current helpers emit (T1–T5) **and** the parent was uploaded / its cover changed after P5 deploy |
| **legacy artifact?** | the string is a retired family (`f_avif` full-res, `dpr_auto`, `c_limit` presets, universal `vc_auto` 720p chain, bare `f_mp4` on a web-safe parent, `fl_attachment/f_*` combined) → legacy; should **not** appear as newly-created post-P5 |

**The six current families, tracked separately every day:**

| ID | Exact string | Purpose | REQUIRED? | Expected new-generation rate |
|---|---|---|---|---|
| **T1** | `c_fill,h_338,q_auto,w_600` (+ `.jpg`) | canonical video poster | **yes** | 1 per newly-uploaded video, once |
| **T2** | bare `f_mp4` | HEVC / non-web compat only | **yes** | 1 per non-web video upload (rare) |
| **T3** | `c_fill,f_auto,q_auto,w_400` | roster / cover thumbnail | **yes** | 1 per new cover image, once |
| **T4** | `c_fill,f_auto,q_auto,w_200` | pipeline thumbnail | **yes** | 1 per new image upload, once |
| **T5** | `f_auto` (format only) | HEIC format negotiation | **yes** | 1 per HEIC image first-render (rare) |
| **T6** | `fl_attachment:<name>` | download filename flag | **yes** (function) | 1 per distinct download filename — **billability under investigation, see §D** |

**Day 0:** across ~240 parents inspected, **0 derived assets created since the P5 deploy**
(consistent with the P10 finding of 0 across ~686 parents; only 1 original uploaded since deploy,
with no derivatives).

---

## D. Cloudinary billing interpretation — the A→E chain, especially T6

The user is right not to assume T6 is free. Here is exactly what is and is not proven.

### The five stages

| Stage | Meaning | Evidence for T1–T5 (real transforms) | Evidence for T6 (`fl_attachment` delivery flag) |
|---|---|---|---|
| **A. string exists in the account** | the transformation string appears in `cloudinary.api.transformations()` | ✅ all present (historical) | ✅ **9 bare `fl_attachment:<name>` strings are in the registry today** (`fg_image_dl`, `fg_take_dl`, `IMG_2901`, `WhatsApp_Image_…`, …) — plus 139 *combined* `fl_attachment:<name>/f_mp4` or `/f_auto,q_auto` |
| **B. URL is constructed** | code builds `.../upload/<xf>/<pid>` | ✅ free — `cloudinary.utils.cloudinary_url()` is local string formatting, no API call | ✅ same — free |
| **C. transformed asset is requested** | a client issues a GET for that URL | ✅ on first render of the media item | ✅ on a Download-button click / WhatsApp file-share |
| **D. derived resource is generated** | Cloudinary materializes and caches a distinct derived object | ✅ **confirmed** — `transformation(name).derived_count == 1` for the sampled T-style strings; each is a real pixel/container transform | **⚠️ NOT CONFIRMED for a *bare* flag.** The *combined* `fl_attachment:<name>/f_mp4` and `/f_auto,q_auto` strings **do** show `derived_count: 1` (they carry a real transform). Whether `fl_attachment:<name>` **alone**, on an otherwise-canonical asset, creates a distinct billable derived resource is **not established from the data available here.** |
| **E. billable transformation / credit consumed** | the generation in D counts against `transformations.credits_usage` | ✅ **once**, at generation; cached thereafter (subsequent deliveries = bandwidth only) | **⚠️ NOT PROVEN.** The P5 completion doc asserts `fl_attachment` on a canonical asset is "a pure delivery flag, creates no derived asset, not billed." That is **plausible** (Cloudinary documents `fl_` flags as delivery-response modifiers, and `fl_attachment` only sets `Content-Disposition`) **but it is an assertion, not a measurement.** |

### What would settle D→E for T6

1. **Cloudinary's plan-specific billing documentation** for delivery flags — not available to this
   analysis (no browsing). *Action: check the Cloudinary docs / account billing FAQ.*
2. **A controlled production before/after observation:** capture `derived_resources` and the
   `transformation` unit line, wait for a real Download-button click on a **web-safe** asset
   (which emits a bare `fl_attachment:<name>`), then re-capture after the usage API refreshes
   (~1–2 days). If `derived_resources` and `transformation` are unchanged → T6 bare is free
   (D=no). If they tick up by 1 → T6 bare bills (D=yes, E=yes). **The daily monitor is designed
   to catch exactly this.**

### Honest current position

- **T1–T5:** proven A→E. Each bills **exactly one transformation, once, at generation**, then is a
  cached object (storage only). This is the intended, minimal cost.
- **T6 bare `fl_attachment`:** proven A, B, C. **D and E are open.** The worst case is 1
  transformation per distinct download filename, once. Given download volume on a B2B casting
  tool is low and the filename set is bounded, even the worst case is a small line — but it
  should be **measured, not assumed.** Flagged for the 14-day window.

---

## E. 14-day / 30-day measurement plan

| Day | Action |
|---|---|
| **0 (2026-08-31)** | ✅ baseline captured (`day_20260831.json`) |
| **1–14** | one snapshot per day (B3a). Each: full `usage()`, new-derivative scan since previous snapshot, `Δ` vs previous, append to `SERIES.csv`. **No extrapolation before Day 7.** |
| **7** | first **provisional** read: 7-day average `Δ transformation_credits/day`, `Δ total_credits/day`; note Cloudinary `last_updated` (data will be ~Day 6). Explicitly label LOW confidence. |
| **14** | **decision-grade** read (see §F). Minimum bar for a plan decision. |
| **15–30** (preferred) | continue daily. A casting cycle (project open → submissions → client review → close) should fall inside this window — that is the real load test. |
| **30** | **high-confidence** read. Recompute §F with 30-day data. |

**What each daily row records** (`SERIES.csv` columns): `date, cloudinary_last_updated,
storage_credits, bandwidth_credits, transformation_credits, total_credits, derived_count,
new_derived_count, new_derived_bytes, new_transformation_families, Δ_transformation_credits,
Δ_total_credits`.

**Watch specifically for:**
- Any new derived asset whose family is **not** T1–T5 → investigate immediately (a regression).
- `extra_avif_mp_encoding` **rising** post-P5 → the AVIF removal didn't take (it should be flat
  or falling as the rolling window sheds pre-P5 days).
- `sd_video_second` / `fourk_video_second` **rising** → a video transcode path is still active
  (should be flat — canonical delivery).
- `derived_count` rising faster than uploads → on-demand generation of something unexpected.

---

## F. Current-plan viability methodology

**Do not decide from < 14 days.** At Day 14 (and again Day 30) compute:

| # | Quantity | Formula |
|--:|---|---|
| 1 | **Actual transformation consumption** | `transformation_credits` at end − at start of the *fully-post-P5* window (i.e. once Cloudinary `last_updated` ≥ 2026-08-31) |
| 2 | **Projected 30-day transformation credits** | `(1) / observed_days × 30`, with a ± band from daily variance |
| 3 | **Actual total-credit consumption** | same for `credits.usage` |
| 4 | **Projected 30-day total credits** | `(3) / observed_days × 30` |
| 5 | **Remaining headroom** | `60 − (4)` (credits); and separately `transformation_budget − (2)` where `transformation_budget ≈ 60 − projected_storage − projected_bandwidth` |
| 6 | **Sustainable on current plan?** | YES if `(4) ≤ ~50` (≥ 15% margin under 60) **and** `(2)` fits the transformation budget with margin; MARGINAL if `50 < (4) ≤ 60`; NO if `(4) > 60` |
| 7 | **Confidence** | LOW (< 14 d or < 1 casting cycle) · MEDIUM (14–30 d, ≥ 1 cycle) · HIGH (30 d, ≥ 1 full cycle, low variance) |

**Plan / price reconciliation (do NOT assume $29 ≡ 60 credits):**
- Cloudinary reports `plan: "Small PAYG"` with a **60-credit monthly limit**. "Small PAYG" is a
  legacy pay-as-you-go tier.
- Whether the **$29/month** the business wants to keep *is* this plan, or a different current tier
  with a different included allowance, **cannot be determined from the API** — `usage()` exposes
  the credit limit, not the dollar price or the plan's list name.
- **Action (you or billing owner):** open the Cloudinary console → Account → Billing / Plan, and
  record: (a) the exact plan name, (b) monthly base price, (c) included monthly credits, (d)
  overage rate per credit, (e) whether transformations/storage/bandwidth have separate sub-caps.
  Feed those into §F. Until then the analysis is in **credits**, targeting **≤ ~50 of 60**.
- **Do NOT recommend the $99 plan** unless the measured post-P5 projection (2)/(4) exceeds the
  current plan's allowance with MEDIUM+ confidence.

---

## G. Exact conditions required before P11 (stored-URL migration for the 468)

**All** must hold:

1. **≥ 14 days (30 preferred) of fully-post-P5 usage data** captured, spanning ≥ 1 casting cycle.
2. **Demonstrated post-P5 cost stability** — daily `Δ transformation_credits` flat or falling; no
   day with a new non-T1–T5 derivative family.
3. **`extra_avif_mp_encoding` confirmed flat/falling** in the rolling window (proves the AVIF
   removal held).
4. **T6 billability resolved** (§D) — bare `fl_attachment` proven free, or its cost measured and
   accepted.
5. **Delivery architecture re-confirmed correct** — the P10 transformation table (6 families, 0
   UNKNOWN) still matches `main`; cost-regression tests still green.
6. **A separate, approved P11 plan document** containing:
   - a **URL dependency audit** — every one of the 468 stored URLs, which `media[]` field it sits
     in (`url` / `poster_url` / `thumbnail_url` / `original_url` / `compat_delivery_url` / …),
     which collection + doc, and whether that doc is live;
   - the **computed canonical replacement** for each (`media_url()` / `video_poster_url()` /
     canonical original / `compat_video_delivery_url()`), with a read-only HEAD verification that
     the replacement resolves;
   - a **dry-run manifest** (no writes) listing every proposed field update;
   - a **rollback strategy** — old value preserved in `original_url_pre_migration`, a reverse
     manifest, and a documented "restore" procedure;
   - explicit statement that P11 is a **MongoDB-write phase** (distinct from P9/P10) requiring its
     own execution authorization.
7. **Only after** the 468 media items no longer reference the old derivative may a P9 pass delete
   those derivatives — subject to per-asset revalidation.

P11 does not start until every item above is satisfied and separately approved.

---

## H. Exact conditions required before ANY future P9 deletion batch

**All** must hold:

1. **An explicit per-batch authorization** from you naming the family, the exact count, and — for
   a specific asset set — the **candidate_hash** and the exact derived_ids.
2. **A fresh read-only analysis** of the proposed set (the P8.5/P9 pattern): P8.5 classification,
   fresh P9 Layer-1 `revalidate_candidate` verdict, per-asset safety proofs, presented **before**
   execution.
3. **`MEDIA_LIFECYCLE_PHYSICAL_DELETE` OFF** before the run; enabled **process-local only** during
   the authorized execution; `pop`-ped + asserted OFF in `finally`; independently re-verified OFF
   after.
4. **New immutable manifest / approval / batch** — never reuse a prior one. Execution
   `candidate_hash` must equal the approved hash exactly.
5. **Per-asset final revalidation immediately before each delete** (the 18–20 point check); any
   single failure → **STOP the batch, no substitution.**
6. **Deletion only via** `cloudinary.api.delete_derived_resources([single_exact_id])`, one id per
   call. Never prefix / folder / `uploader.destroy` / wildcard.
7. **Anomaly → STOP** (identity mismatch, unexpected reference, missing/absent parent, ambiguous
   Cloudinary response, unexpected Mongo change) — no auto-retry.
8. **Post-batch verification** — derived gone, parent survives, Mongo unchanged, protected buckets
   intact (full re-scan), previous deletions still deleted, 0 transformations, 0 uploads, audit
   records written, flag OFF.
9. **The target family must not be frozen** — the currently-frozen set stays frozen absent a
   specific new authorization: 2,654 `LEGACY_DERIVED`, 468 `PROTECTED_HISTORICAL_DERIVED`, 4 HEVC
   `CURRENT_COMPATIBLE`, 6 `UNKNOWN`, 35 `REVIEW_LINKED_CANDIDATE`, 25 `project_submission`
   RETENTION_BLOCKED, all originals. The remaining ~2,347 retired-AVIF `DELETE_CANDIDATE` are the
   only pre-cleared family, and even those need a per-batch authorization.

---

## Frozen — unchanged by this phase

2,654 `LEGACY_DERIVED` · 468 `PROTECTED_HISTORICAL_DERIVED` · 4 HEVC `CURRENT_COMPATIBLE` · 6
`UNKNOWN` f_mp4 · 35 `REVIEW_LINKED_CANDIDATE` · 25 `project_submission` RETENTION_BLOCKED ·
~2,347 remaining retired-AVIF `DELETE_CANDIDATE` · all 4,350 originals.
`MEDIA_LIFECYCLE_PHYSICAL_DELETE`: **OFF**.

## Confirmation — READ-ONLY

Cloudinary writes **0** · deletes **0** · transformations generated **0** · uploads **0** ·
MongoDB writes **0**. Day-0 used: `usage()` ×1, `transformations()` ×1, `transformation(name)` ×12,
`resources()` ×2, `resource()` ×240 — all read-only.

**STOP.** No monitoring job is scheduled or committed until you pick a B.3 option. No P11. No P9
Batch 4. No mutation of any kind.
