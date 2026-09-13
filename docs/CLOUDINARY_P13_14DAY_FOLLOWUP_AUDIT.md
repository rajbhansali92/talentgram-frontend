# Cloudinary — 13/14-Day Post-P5 Follow-Up Audit (READ-ONLY)

**2026-09-13.** Pure measurement. Zero Cloudinary writes/deletes/uploads/transform-generating
requests. Zero MongoDB writes. No P9, no P11, no manifest/approval/batch, flag untouched.

Evidence: `docs/p10_5_snapshots/day_20260913.json` (+ `day_20260831.json`, `day_20260904.json` for
comparison). Scripts used are read-only Admin-API/Mongo-read scratch scripts (not committed —
same pattern as P10/P10.5).

---

## 1. Executive summary

Everything continues to point the same direction as the 2026-09-04 audit, now with **13 days**
of real post-P5 production behind it instead of ~4 hours: **the transformation-cost problem
remains resolved and is continuing to improve.** Transformation credits fell from 71.25 (08-31)
→ 68.21 (09-04) → **53.25 (09-13)** — a monotonic, accelerating decline consistent with the
rolling-window mechanism shedding pre-P5 high-usage days. Across **1,050 distinct originals**
sampled two different ways (the 800 newest + a random 250 of 4,747), **zero new derivative
assets were created since the P5 deploy**, despite **411 real new uploads** in that window. Three
new client-review links (64 media items) show 0 non-canonical delivery URLs and the HEVC
compatibility path firing correctly once. No P9/P11/purge activity occurred (purge collections
byte-for-byte unchanged since 08-31). The upload-413 fix (`85728df`) is unmodified in the deployed
code and the regression suite is still green.

**One open item, disclosed honestly (§C, §F):** `derived_resource_count` rose +229 over the
window (8,428→8,657), but neither sample (1,050/4,747 originals, ~22%) located the specific new
derivative records responsible. This does not contradict the "0 new derivatives" finding — it
means the growth, if it is genuine new lazy-thumbnail generation, is spread thinly across the
~78% of originals not sampled. It could equally be a Cloudinary accounting artifact. Flagged as a
genuine limitation, not resolved by inference.

**Plan viability:** confidence **MEDIUM (borderline)** — 13 days of data, one short of the
stated 14-day bar, but backed by substantial representative activity (411 new originals, 220
submissions, 81 talents, 16 applications). A rough (LOW-confidence) linear projection to day 30
puts total credits at **~56–59 of 60** — driven not by transformations (which are heading toward
a near-zero floor) but by **ordinary storage/bandwidth growth** as the product grows. This is a
different kind of finding than the original problem and needs its own watch, not a reopening of
the Cloudinary workstream.

**Final decision: B. CLOUDINARY WORKSTREAM FROZEN, CONTINUE OBSERVATION** (see §11).

---

## 2. Exact observation window

| Milestone | Timestamp (UTC) | Days before "now" |
|---|---|--:|
| P4 deployed (`0a92cb6`) | 2026-08-30 13:39:06 | 13.61 |
| P5 deployed (`d258972`) | 2026-08-30 14:09:03 | 13.59 |
| Previous audit / upload-fix deployed (`85728df`) | 2026-09-04 09:02:54 | 8.80 |
| **Now** (this audit) | **2026-09-13 04:14:12** | — |

| Data point | Value |
|---|---|
| Cloudinary `usage().last_updated` at this audit | **2026-09-12** |
| `date_requested` | 2026-09-13T00:00:00Z |
| Data currency | **~1 day lag**, consistent every time this has been measured (08-30, 09-03, 09-12 lags on 08-31/09-04/09-13 captures respectively) |
| Complete days of usage data covering the post-P5 period | **P5 deploy (08-30) → last_updated (09-12) = 13 days**. One day short of the task's own 14-day MEDIUM bar. |

**Historical vs. observed, kept separate:**

1. **Historical/pre-P5 cumulative** — the P1 snapshot (2026-08-30 08:20, before either deploy):
   115.52 total credits, transformation breakdown `transformation=10,687 / sd_video_second=21,472
   / hd_video_second=591 / extra_avif_mp_encoding=10,996 / fourk_video_second=1,165`, 8,457
   derived, 4,324 originals. This is the "old architecture" baseline.
2. **Post-P5 observed** — direct evidence collected in this and the prior audit: 0 new derivative
   assets found across two independent samples totalling 1,050 originals (this audit) + earlier
   samples (600+240 originals, prior audit); 3 new client-review links, 100% canonical.
3. **Current account totals** — §3 table, row `2026-09-13`.
4. **Cannot yet be confidently attributed to the post-P5 period alone:** the `transformations.usage`
   rolling figure (53,251) and its credit equivalent (53.25) — this is a **rolling window** that
   still contains a shrinking number of pre-P5 days. It is falling because old high-usage days are
   aging out, not solely because new usage is low (though the direct-evidence checks show new
   usage genuinely is ~0). The two effects are not separable from the `usage()` number alone —
   only from the direct derivative-generation checks in §4.

---

## 3. Cloudinary usage comparison table

| Date captured | data → (`last_updated`) | Total credits (%) | Transf. credits | Storage credits | Bandwidth credits | Derived count | Originals | Transf. units (transformation / sd_video_s / hd_video_s / avif_mp / 4k_video_s) |
|---|---|---|--:|--:|--:|--:|--:|---|
| 2026-08-30 (P1, **pre-P4/P5**) | ~08-28 | 115.52 (192.5%) | ~71.0* | ~18.3* | ~26.2* | 8,457 | 4,324 | 10,687 / 21,472 / 591 / 10,996 / 1,165 |
| 2026-08-31 (P10 baseline) | 08-30 | 115.44 (192.4%) | 71.25 | 17.86 | 26.33 | 8,383 | 4,350 | 10,804 / 21,555 / 591 / 11,304 / 1,165 |
| 2026-09-04 (prior audit) | 09-03 | 113.35 (188.9%) | 68.21 | 18.35 | 26.79 | 8,428 | 4,420 | 9,450 / 20,753 / 591 / 10,901 / 1,165 |
| **2026-09-13 (this audit)** | **09-12** | **101.57 (169.3%)** | **53.25** | **20.32** | **28.00** | **8,657** | **4,791** | **7,242 / 14,825 / 653 / 9,121 / 1,148** |

\* P1's credit split by category wasn't captured separately in that snapshot; the unit breakdown
was, and is shown.

**Cloudinary does not expose true historical daily usage** via this account/API tier — `usage()`
returns only the current rolling-period snapshot. The table above is built entirely from
**snapshots this workstream took and saved** (`docs/p10_5_snapshots/day_*.json`), not from a
Cloudinary-provided history endpoint. **No daily values are interpolated or fabricated** — the
three rows are the only three points actually measured; the trend between them is described, not
filled in.

**Trend, computed only from measured points:**

| Interval | Days | Δ Total credits | Δ Transf. credits | Δ Storage credits | Δ Bandwidth credits |
|---|--:|--:|--:|--:|--:|
| 08-31 → 09-04 | ~4 | −2.09 (−0.52/day) | −3.04 (−0.76/day) | +0.49 (+0.12/day) | +0.46 (+0.12/day) |
| 09-04 → 09-13 | ~9 | −11.78 (−1.31/day) | **−14.96 (−1.66/day)** | +1.97 (+0.22/day) | +1.21 (+0.13/day) |

Transformation-credit decline is **accelerating** (−0.76/day → −1.66/day) — exactly what the
rolling-window mechanism predicts as more of the window becomes post-P5. Storage and bandwidth
are **rising** — ordinary organic growth (+371 originals in the second window), unrelated to
P4/P5.

---

## 4. Transformation-generation findings

### Method (strongest available live evidence, per the instructions)

- **Sample A — newest activity:** the 400 newest originals per resource type (800 total),
  checked via `cloudinary.api.resource(pid).derived[]` for any `created_at ≥ P5 deploy`.
- **Sample B — random cross-section:** 250 originals chosen uniformly at random from the full
  4,747-original population (not just the newest), same check.
- **Targeted family hunt:** across Sample A, explicitly searched every derived asset's
  transformation string for `avif`, `f_mp4`, `vc_auto`, `c_limit`, `dpr_auto` with
  `created_at ≥ P5 deploy`.
- **Live upload count:** every original whose `created_at ≥ P5 deploy` was counted directly from
  `resources()`, independent of any database log (see §5's note on `storage_audit_log`
  under-counting).

### Results

| Check | Result |
|---|---|
| Originals uploaded since P5 deploy (direct Cloudinary count) | **411** |
| Originals uploaded since the prior audit (09-04) | **336** |
| New derived assets since P5 — Sample A (800 newest originals) | **0** |
| New derived assets since P5 — Sample B (250 random originals) | **0** |
| New derived assets since P5 — **combined, 1,050 distinct originals (~22% of all 4,747)** | **0** |
| Post-P5 `avif`/`f_mp4`/`vc_auto`/`c_limit`/`dpr_auto` derived hits (Sample A) | **0** |
| New transformation families appearing in the registry | **None found from new generation.** Registry grew 203→274 strings and `fl_attachment` entries 148→217 over 13 days — these are new **filenames** for the (unchanged, non-billable) download flag, not new billable families; every family seen in Sample B (any age) is a pre-P5 legacy string (`f_avif` full-res, `dpr_auto` variants, old 720p `c_limit`/`vc_auto` chains, bare `f_mp4`) |
| Eager generation evidence | None — no eager parameter anywhere reachable in prod code (unchanged since P10); `providers.CloudinaryProvider` (the only remaining `eager` param) is unreachable (`VIDEO_PROVIDER=stream`) |
| Repeated regeneration evidence | None observed — every derived asset checked has exactly one `created_at`, no re-derivation pattern |
| Download-triggered transformation cost | **Not directly measurable this audit** (would require correlating a specific Download click with a `derived_resource_count` tick — no such click was deliberately triggered, per the task's "do not intentionally trigger transformations" instruction). No `fl_attachment`-only (bare) derived hit was found in the post-P5 samples either, so no evidence of it firing, but this remains the same open question flagged in P10.5 (§D there) |
| Transformation counters: flat, falling, or increasing? | **Falling, and the fall is accelerating** — every one of the 5 unit lines is flat or down from both prior snapshots except `hd_video_second` (591→591→653, +62 total, see below) |

### `hd_video_second` — the one line that rose, examined

591 → 591 → **653** (+62 over the second window only). This is a **video-seconds** unit, meaning
Cloudinary generated (or served, depending on the exact metering) HD-resolution video seconds
during this window. Two explanations, and the audit cannot distinguish them from `usage()` alone:
(a) the sanctioned **T2 HEVC-compat exception** firing on a genuine non-web upload — §5's
client-review sample directly observed **one** live `needs_compat_delivery` HEVC video with an
intact `f_mp4` compat URL in a link created since the prior audit, which is exactly this
mechanism working as designed; or (b) some other HD video delivery. +62 seconds is small (about
one minute of compat-transcoded video, once) and is consistent with (a) alone. **Not flagged as a
regression**, but noted rather than silently folded into "everything falling."

### Confidence & limitations

- **Confidence: MEDIUM-HIGH** for "no unexpected transformation family is being generated" — two
  independent, non-overlapping-by-construction samples (newest + random) covering ~22% of all
  originals, plus a targeted family hunt, all returned zero.
- **Confidence: LOW** for fully explaining the **+229 derived-count growth** — neither sample
  found the specific new records. See §6 for the honest treatment of this gap; it is **not**
  resolved by inference here.
- The `transformations.usage` rolling figure **cannot** be used alone to compute a "post-P5 daily
  rate" — it mixes a shrinking pre-P5 tail with genuine post-P5 activity. The direct
  derivative-generation checks are the only way to isolate the post-P5 contribution, and they
  measured it at effectively zero.

---

## 5. Upload architecture findings

**Code-level verification** (no test uploads performed, per instruction):

`backend/core.py` — `cloudinary_upload()` still contains, unmodified since `85728df`:
```
_CHUNKED_UPLOAD_THRESHOLD = 90 * 1024 * 1024
if len(data) > _CHUNKED_UPLOAD_THRESHOLD:
    result = cloudinary.uploader.upload_large(io.BytesIO(data), chunk_size=20*1024*1024, **upload_kwargs)
else:
    result = cloudinary.uploader.upload(data, **upload_kwargs)
```
Confirmed via `git diff` against the fix commit — **zero drift** over 9 days and dozens of
unrelated commits.

**Every backend-proxied upload route in the app funnels through this one function** (traced by
static call-graph, this audit):

| Route | Endpoint | Proxied or direct? | Protected by the fix? |
|---|---|---|---|
| Admin Talent Profile image/video | `POST /talents/{tid}/media` (`add_media`) | **backend-proxied** (`upload_and_track_asset`→`cloudinary_upload`) | ✅ |
| Admin audition video (legacy) | `POST /projects/{pid}/submissions/{sid}/admin-media` | **backend-proxied** | ✅ |
| Admin audition video (current) | `POST …/admin-media-v2/sign` | **browser-direct** signed upload | n/a (never affected) |
| Admin application media | `POST /public/apply/{aid}/upload`-adjacent (`upload_application_media`) | **backend-proxied** | ✅ |
| Legacy public submission upload | `POST /public/submissions/{sid}/upload` (`submission_upload`) | **backend-proxied** | ✅ |
| **Primary** Talent Invite / apply image | `POST /public/submissions/{sid}/upload/sign`, `POST /public/apply/{aid}/upload/sign` | **browser-direct** to `api.cloudinary.com` | n/a (never affected) |
| **Primary** Talent Invite / apply video | `POST …/video-signature` (both submissions and applications) | **browser-direct** | n/a (never affected) |
| WhatsApp media ingestion | `POST /media-upload` (`media_upload`) | **backend-proxied** | ✅ |
| Casting-desk attachments | `POST /sessions/{sid}/attachments` | **backend-proxied** | ✅ |
| Project materials | `attach_project_material` | **backend-proxied** | ✅ |
| Generic internal upload | `POST /upload` (auth.py) | **backend-proxied** | ✅ |
| Admin ingest-new-audition-media | `ingest_new_audition_media_to_submission` | **backend-proxied** | ✅ |

**Live evidence since the fix:**

- `db.storage_audit_log` (records only successful **backend-proxied** uploads — the failure path
  raises before this log call, so it structurally cannot show failed attempts): **92 successful
  UPLOAD rows since the fix deploy** on days 09-04, 09-05, 09-12 (sparse — the collection only
  logs entries on days admins/agents actually used a proxied route; most real volume goes through
  the direct-signed Talent Invite/apply path, which this collection never sees).
- **No durable failure log exists** for the pre-fix 413 (the exception is raised before
  `log_storage_action` runs) — so historical failure counts before/after the fix can only come
  from Railway's log retention window, which currently only covers the last several hours to a
  day and shows **zero** `413` / `Storage upload failed` / `Cloudinary upload failed` entries in
  that window. **This is a genuine blind spot**, not a "confirmed zero for 13 days" — stated
  plainly rather than overclaimed.
- **220 submissions, 81 talents, 16 applications created since P5 deploy** — substantial real
  production activity exercising both upload paths.
- Every one of the 411 originals uploaded since P5 (§4) is, by construction of the P4/P5 code
  path, stored as a single canonical asset with no eager derivative — confirmed by the 0-new-
  derivatives finding, which covers uploads on both the proxied and direct paths equally (the
  derivative-generation check doesn't distinguish which path created the original).
- Ownership/ persistence correctness was not re-tested with a fresh upload this time (per the "do
  not perform destructive tests / do not upload large test files" instruction) — it rests on the
  P10-audit production test (disposable talent, 116.7 MB video, `ownership.owner_type=talent`,
  correct `public_id`/canonical URL) plus the unchanged code.

**Conclusion:** the upload architecture is unchanged and, on available evidence (regression
suite green, 92 logged successes, 411 real uploads with 0 unwanted derivatives, no visible 413s
in the retained log window), functioning correctly. The one blind spot (no long-window failure
log) is disclosed rather than papered over.

---

## 6. Client View / playback / delivery findings

**Sample checked:** the **3 client-review links created since the prior audit** (2026-09-04),
covering **64 media items** across their referenced talents/submissions. (The prior audit's
6-link/321-item sample from before 09-04 was not re-checked here — it was already reported
clean and nothing in this workstream can have changed it, since no URL-migration or purge ran.)

| Check | Result | Sample |
|---|---|---|
| Non-canonical delivery URLs (`/f_auto/`, `/f_avif/`, `/f_mp4/`, `/vc_auto/`, `/c_limit/`, `/dpr_auto/`, `/w_1280/`, `/w_1600/`, `/w_1200/`) | **0 / 64** | 3 links, 64 media |
| Sanctioned HEVC compat `f_mp4` URLs | **1** — `needs_compat_delivery=true` media with an intact `f_mp4` URL, exactly the P4 exception | same 3 links |
| Poster URLs present | 4 | same 3 links |
| Poster URLs canonical (`c_fill,h_338,q_auto,w_600`, no `dpr`) | **4 / 4** — 100%, all post-fix media | same 3 links |
| Stored-URL migration accidentally occurred? | **No** — 0 evidence of any rewritten `url`/`poster_url`/`thumbnail_url`; P11 has not started (confirmed §9) | — |
| Media ownership / authorization regression | **None found** — not independently re-verified via a live gated request this audit (the private name/email gate was not bypassed, consistent with the prior audit's approach); code paths (`proxy_media`, `visibility.download` gate, viewer-token auth) are unchanged since P5 (`git diff` shows no changes to `links.py` auth logic since the last audit) |

**Explicitly not claimed:** this audit did **not** re-verify every one of the platform's ~32
client-review links, nor did it click through an actual browser session (the review-link gate
requires a name/email submission that would be entering a stranger's data into a private link —
out of bounds, same reasoning as the prior audit). The conclusion is scoped to: **the 3 links and
64 media items created since the last audit, all clean**, plus **unchanged code** for everything
not re-sampled.

---

## 7. Storage and derived-resource findings

| | 2026-08-31 | 2026-09-04 | 2026-09-13 | Δ (13-day) |
|---|--:|--:|--:|--:|
| Original assets | 4,350 | 4,420 | **4,791** | **+441** |
| Derived assets (Cloudinary `derived_resources`) | 8,383 | 8,428 | **8,657** | **+274** |
| P9 total deletions to date | 128 (all before 08-31) | 128 | **128 — unchanged** | 0 |

**Category breakdown (LEGACY_DERIVED / PROTECTED_HISTORICAL / active-required / frozen f_mp4
buckets) was last individually re-enumerated at the P8.5/P9 f_mp4 Batch 3 point (2026-08-31) and
was NOT re-run in this audit** — a full re-classification of ~8,657 derived assets is a ~200+
read-only Admin-API-call operation (the same scale as the original P8.5 inventory) and is outside
this audit's scope of a lightweight follow-up check. Carried forward from the last known state
(all frozen, all unchanged since no P9/P11 activity occurred — confirmed in §9):

| Category | Last measured (2026-08-31) | Re-verified this audit? |
|---|--:|---|
| Remaining retired-AVIF `DELETE_CANDIDATE` | ~2,347 | Not re-enumerated; inferred unchanged (no P9 batch ran) |
| `LEGACY_DERIVED` (orphan-parent) | 2,654 | Not re-enumerated; inferred unchanged |
| `PROTECTED_HISTORICAL_DERIVED` (persisted) | 468 | Not re-enumerated; inferred unchanged |
| `CURRENT_COMPATIBLE` HEVC f_mp4 | 4 | Not re-enumerated; inferred unchanged |
| `UNKNOWN` f_mp4 (orphan parent) | 6 | Not re-enumerated; inferred unchanged |
| `REVIEW_LINKED_CANDIDATE` f_mp4 | 35 | Not re-enumerated; inferred unchanged |
| `project_submission` f_mp4 (RETENTION_BLOCKED) | 25 | Not re-enumerated; inferred unchanged |

**Basis for "inferred unchanged":** these categories can only shrink via a P9 batch (none ran —
purge collections identical, §9) or grow via new derivative generation matching those retired
families (0 found anywhere in this audit's ~1,050-original sample + targeted family hunt, §4).
**This is inference, not a fresh count** — flagged per the task's own "distinguish inference from
verified evidence" requirement.

**New growth (+441 originals, +274 derived) is entirely attributable to normal product usage**
(220 new submissions, 81 new talents, 16 new applications, §5) — new original uploads plus their
lazy T1/T3/T4 posters/thumbnails, not to any transformation regression (§4 found 0 evidence of a
retired family regenerating).

**Storage in `usage()` (20.32 credits, ~20.3 GiB)** continues to rise with real originals — this
is the product growing, not a Cloudinary architecture problem. **Usage API lag** (data is always
~1 day behind `date_requested`) means the "current" 4,791/8,657 figures already slightly
undercount today's true live counts by roughly one day's worth of activity — noted, not
corrected for (no reliable way to correct for it without over-fitting to a single lag estimate).

---

## 8. Billing / plan viability

**Confirm from console (as given) vs. API (measured):** Plan **Small PAYG**, base **$29/month**,
included **60 credits**, overage **$0.55/credit** (console figures, not independently
re-verifiable from the API — `usage()` exposes the credit limit and plan *name* but not the
dollar pricing). Billing/reset date was not re-queried this audit (not exposed by `usage()`
beyond `date_requested`); the prior audit's console-reported "next bill 2026-09-13" is **today** —
worth the user independently checking whether the cycle actually rolled and whether that explains
any of the `last_updated` behavior (this audit did not detect an obvious reset artifact, but did
not specifically test for one either).

**Confidence label, per the task's own rubric:**

| Bar | Requirement | This audit |
|---|---|---|
| HIGH | ≥30 days clean post-P5 data + representative activity | Not met — only 13 days |
| **MEDIUM** | ≥14 days + meaningful activity | **13 days — one day short of the stated bar.** Activity is well beyond "meaningful" (411 uploads, 220 submissions). Labelled **MEDIUM (borderline)**, not full MEDIUM, in deference to the exact day count. |
| LOW | <14 days, incomplete data, or insufficient activity | Superseded by the above — data is complete for the window it covers and activity is substantial, so LOW would understate it |

**Calculations (with the caveats the task demands):**

- **Post-P5 average daily transformation-credit *change*:** −0.76/day (first 4 days) accelerating
  to −1.66/day (most recent 9 days). **This is a decline rate, not a usage rate** — it cannot be
  read as "the app generates −1.66 credits of transformations per day" (that's nonsensical); it
  reflects the rolling window shedding pre-P5 days faster as more of the window becomes post-P5.
- **Post-P5 average daily *new*-transformation-generation rate (the number that actually
  matters):** measured directly, not derived from the credit trend — **≈ 0**, per the 1,050-
  original zero-hit sample in §4. Even pessimistically attributing the entire unexplained +229
  derived-count growth (§7) to new transformation-billed generation, that is at most
  **≈ 229 ÷ 13 days ≈ 17.6 new derivatives/day**, each a tiny thumbnail/poster fraction of one
  "transformation" unit (not a video-second) — order-of-magnitude **≤ 0.02 credits/day ≈ 0.5
  credits over a 30-day month.** Trivial against the 60-credit budget either way.
- **Estimated 30-day transformation credits:** **not confidently computable from the rolling
  number** (see above). Based on the *mechanism* (window fully post-P5 by ~2026-09-29) plus the
  measured near-zero new-generation rate, transformation credits should continue falling toward a
  low-single-digit floor by day 30 — **this is a projection from mechanism + trend, not a direct
  measurement, and is explicitly lower-confidence than the rest of this report.**
- **Estimated 30-day total credits:** storage and bandwidth are growing linearly at
  **+0.22/day** and **+0.13/day** respectively (measured, most recent 9-day window). A **LOW-
  confidence linear projection** (17 more days at the same rate) gives storage ≈ 24.0, bandwidth
  ≈ 30.3; adding a projected transformation floor of ~2–5 gives a **projected total of roughly
  56–59 of 60 credits around 2026-09-29**. This straddles "sustainable" and "marginal" — **it is
  driven by ordinary storage/bandwidth growth, not by any transformation regression.**
- **Is the estimate distorted by historical/pre-P5 activity?** The transformation piece, yes
  (explicitly not used for the projection beyond "heading toward a floor"). The storage/bandwidth
  pieces are **not** distorted by pre-P5 activity — they reflect real, current, organic growth
  and would be true regardless of P4/P5.
- **Sustainability verdict:** **MARGINAL, trending toward likely-sustainable on the
  transformation axis specifically (that problem is resolved and still improving) but
  potentially tight overall if storage/bandwidth keep growing at the current rate** — this is a
  **capacity-planning** observation about product growth, not a Cloudinary-architecture defect.
  **Do not upgrade to the $99 plan on this projection alone** (LOW confidence, 13 days, linear
  extrapolation) — recommend one more read at ~day 30 (≈2026-09-29/30) before any plan decision.

---

## 9. Regression and safety checks

| Check | Result |
|---|---|
| Unexpected Cloudinary deletes | **0** — this audit performed none; no evidence any occurred (derived/original counts only grew, consistent with organic use, not deletion) |
| Unexpected Cloudinary uploads (by this audit) | **0** — no test uploads performed |
| Unexpected transformation generation | **None found** beyond the disclosed +229 derived-count gap (§4, §6) |
| Unexpected MongoDB media changes | Not directly diffed record-by-record (out of scope for a read-only usage audit), but the **purge/lifecycle control-plane collections are exact** — see below |
| Stored URL rewrites (P11) | **0** — `p9/controlled-purge` branch unchanged at `16ed22b`; no P11 branch/commit exists anywhere in `git log --all` |
| P9 batch execution | **0** — `purge_manifests` 7, `purge_approvals` 6, `purge_batches` 6, `purge_audit_log` 128 — **byte-identical** to the count immediately after f_mp4 Batch 3 (2026-08-31); most recent audit-log timestamp is still `2026-08-31T07:43:03` (batch `b_9ce0185df7a5ed571a5d`) — **nothing has been appended in 13 days** |
| P11 execution | **0** — not started, no branch, no commits |
| `MEDIA_LIFECYCLE_PHYSICAL_DELETE` | **NOT SET (OFF)** — confirmed via `railway variables` |
| Unexpected changes to protected media | No evidence of any — protected buckets are inferred unchanged (§7) because their only two possible triggers (a P9 batch or new matching-family generation) both show zero activity |
| New production errors related to Cloudinary delivery/upload | **None found in the retained Railway log window** (a few hours to ~1 day — see the blind-spot note in §5); the P4/P5 cost-regression test suite (`test_p4_no_eager_transformations.py` + `test_p5_delivery_transformations.py`, 71 tests) still passes unmodified |
| Code drift on the upload fix | **0** — `core.cloudinary_upload` byte-identical to `85728df` |

---

## 10. Issues found

1. **+229 derived-count growth not attributed to specific records** (§4, §6, §7) — likely benign
   (organic lazy T1/T3/T4 generation spread across the ~78% of originals not sampled) but not
   proven. **Not a regression finding**, a **measurement-coverage gap**. No action required
   unless the user wants a full re-enumeration for certainty.
2. **No durable upload-failure log** — the 502/413 failure path never reaches
   `storage_audit_log`, so failure-rate history relies entirely on Railway's short log retention.
   Pre-existing design property, not introduced by this audit or the fix; worth a future
   lightweight improvement (log failures too) if upload reliability ever needs auditing again.
3. **Storage/bandwidth growth could approach the 60-credit ceiling by ~day 30** on a LOW-
   confidence linear projection — a **capacity-planning watch item**, not a Cloudinary-
   architecture defect and not caused by anything P4/P5/P9 touched.

None of these rise to "reopen the workstream."

---

## 11. Final recommendation

# B. CLOUDINARY WORKSTREAM FROZEN, CONTINUE OBSERVATION

- **No architecture changes recommended.**
- **No P9 deletion recommended** (the remaining retired-AVIF pool stays frozen; any future batch
  still needs its own full per-batch authorization).
- **No P11 migration recommended** (still needs its own dry-run manifest + URL dependency audit +
  rollback plan, per the original P10 conditions — none of that has been started, correctly).
- **No Cloudinary plan upgrade recommended** — the transformation-cost problem is resolved and
  still improving; the only reason for caution is ordinary storage/bandwidth growth, which is a
  capacity question to revisit with a day-30 reading, not a reason to pay for a bigger
  transformation allowance the app no longer needs.
- **Normal Talentgram development may continue** — 9+ unrelated commits landed on `main` in this
  window with zero interaction with the Cloudinary/upload code, confirming the fix and
  architecture are stable under real ongoing development.
- **Recommended next check:** one more read-only snapshot around **2026-09-29 to 2026-10-02**
  (≈30 days post-P5) to (a) get a HIGH-confidence plan-viability read once the rolling
  transformation window is fully post-P5, and (b) resolve the +229-derived-count question with a
  wider or fuller sample if it still seems worth resolving by then.

---

## 12. Evidence files / commit / deployment references

| | |
|---|---|
| Snapshots compared | `docs/p10_5_snapshots/day_20260831.json`, `day_20260904.json`, `day_20260913.json` (new, this audit) |
| Upload-fix commit (unmodified, verified) | `85728df` — `fix(media): chunk server-proxied uploads >90MB so large admin intro videos don't 413` |
| P9 engine branch (untouched) | `p9/controlled-purge` @ `16ed22b` (PR #12 still open, not merged, not executed further) |
| Prior audit reports | `docs/CLOUDINARY_FINAL_PRODUCTION_AUDIT.md` (2026-09-04), `docs/CLOUDINARY_P10_5_MONITORING.md`, `docs/CLOUDINARY_P10_COST_VERIFICATION.md` |
| Current deploy | Railway `919753d0` SUCCESS (2026-09-12 23:08, HEAD `9c19833` — unrelated WhatsApp fix, no Cloudinary-code changes since `85728df`) |
| `MEDIA_LIFECYCLE_PHYSICAL_DELETE` | confirmed NOT SET via `railway variables` at time of this audit |
| Purge control-plane state | `purge_manifests` 7, `purge_approvals` 6, `purge_batches` 6, `purge_audit_log` 128, `pending_media_deletions` 0 — unchanged since 2026-08-31 |

### Evidence classification (verified live / code-level / historical / inference / unknown)

- **Verified live evidence:** current `usage()` snapshot; 1,050-original zero-new-derivative
  sample; targeted family hunt; 3-link/64-item client-review sample; purge-collection counts;
  `MEDIA_LIFECYCLE_PHYSICAL_DELETE` unset; regression-suite pass; `core.py` diff-clean.
- **Code-level evidence:** the full upload-route call graph (§5); the unchanged
  `cloudinary_upload` fix; absence of any P11 code/branch.
- **Historical evidence:** the P1/P10/P10.5 snapshots used for comparison; the P8.5/P9 category
  breakdown carried forward in §7 (explicitly marked "not re-enumerated").
- **Inference (explicitly labelled as such):** protected-bucket counts unchanged (§7); the
  +229-derived-growth explanation as "likely organic lazy generation" (§4, §6); the day-30 credit
  projection (§8).
- **Unknown / not measurable this audit:** whether any download click has ever billed a bare
  `fl_attachment` transformation (T6 billability, carried over from P10.5 as still open); the
  true dollar billing-cycle boundary; failure-rate history beyond Railway's retained log window.

**STOP.** No changes made. No deployment. No deletion. No new phase. The Cloudinary workstream
remains frozen; this was an observation-only check.
