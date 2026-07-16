# Presentation Model Architecture

This document describes the layered pattern behind the Submission Experience
(`frontend/src/pages-components/SubmissionPage.jsx`) and the Upload Activity
Panel (`frontend/src/components/shared/FloatingUploadManager.jsx`). Both were
built the same way, and any future UI in this family (e.g. a Talent Invite
port of the Submission Experience Model — see [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md))
should follow the same layering rather than re-deriving state ad hoc.

## The rule

> - **Engines compute domain state.**
> - **Presentation Models aggregate engine outputs.**
> - **React components should remain pure renderers.**

Dependencies point one way only: **Engines → Presentation Models →
Components.** An engine never imports a presentation model or a component. A
presentation model never imports a component. A component never imports an
engine directly — it only ever consumes a presentation model's hook output.

```
        domain inputs (project config, form/submission state, activeUploads)
                                    |
                                    v
                    +-----------------------------+
                    |           ENGINES            |
                    |  (compute domain state only) |
                    +-----------------------------+
                                    |
                                    v
                    +-----------------------------+
                    |     PRESENTATION MODELS      |
                    | (aggregate engine outputs +  |
                    |  small, self-contained UI     |
                    |  state — expand/collapse,     |
                    |  timers)                       |
                    +-----------------------------+
                                    |
                                    v
                    +-----------------------------+
                    |         COMPONENTS           |
                    |     (pure renderers only)     |
                    +-----------------------------+
```

## The three engines (Submission Experience)

All three live in `frontend/src/lib/requirementEngine.js` and
`frontend/src/lib/readinessStatus.js`. Each owns exactly one concern and
never reaches into the other's inputs.

| Engine | Module | Input | Output | Never does |
|---|---|---|---|---|
| **Requirement Engine** | `lib/requirementEngine.js` — `computeRequirementItems()` | `project.submission_requirements` (admin config) + current `form`/`submission` state | Per-item `{ requirement: REQUIREMENT_TIERS.*, satisfied }` | Never inspects the Upload Manager or live upload state |
| **Operational Engine** | `lib/readinessStatus.js` — `mapUploadManagerStatus`, `findActiveUploadStatus`, `deriveOperationalStatus`, `summarizeUploads` | `UploadManagerContext`'s raw `activeUploads` map | Per-item and aggregate `OPERATIONAL_STATES.*` (missing/queued/uploading/retrying/waiting/processing/failed/completed) | Never touches requirement config |
| **Readiness Engine** | `lib/readinessStatus.js` — `summarizeReadiness`, `resolveBlockingReason`, `deriveSubmitCta`, `summarizeSections`, `summarizeOverallProgress` | The Requirement Engine's `requirement`/`satisfied` + the Operational Engine's `operational`, already combined per item | Submission-level (`readinessSummary`, `submitCta`), section-level (`sectionStatus`), and whole-checklist (`overallProgress`) summaries | Never computes a requirement tier or an operational status itself — it only combines values the other two engines already produced |

These three engines, plus their enums (`REQUIREMENT_TIERS`,
`OPERATIONAL_STATES`, `SUBMIT_BLOCKING_REASONS`, `CTA_ACTIONS`,
`SECTION_STATUS`), are the **only** vocabulary for readiness state anywhere
in the submission experience. No presentation model or component should ever
compare against a raw string literal — always the exported enum.

## The presentation models

A presentation model calls engines (in order, where one depends on another's
output), aggregates the results into one object, and hands that whole object
to its component(s). It owns **no business rule of its own** — the one
exception is small, self-contained UI-only state that isn't domain
knowledge (an expand/collapse flag, a "just finished" timer) — that's
legitimately presentation state, not a business decision, and it's what
keeps a presentation model from being a second engine in disguise.

| Presentation Model | Module | Aggregates | Also owns (UI-only state) | Feeds |
|---|---|---|---|---|
| **Submission Experience Model** | `hooks/useSubmissionExperienceModel.js` | Requirement Engine → Operational Engine → Readiness Engine, in that order, into `{ requirementItems, readinessModel, checklist, missingRequirements, readinessSummary, uploadSummary, uploadsInProgress, blockingReason, submitCta, sectionStatus, overallProgress, saveStatus }` | Nothing — it's a pure `useMemo` of its inputs | `SubmissionReadinessPanel`, the Submit CTA button, section-header badges, finalize-time validation scroll (all in `SubmissionPage.jsx`) |
| **Upload Activity Model** | `hooks/useUploadActivityModel.js` | The Operational Engine's `summarizeUploads()` (counts) + `mapUploadManagerStatus()` (per-item friendly state), plus a session-lifetime `completedCount` passed in from `UploadManagerContext` | `expanded`, `justFinished`, and the timers/effects that drive Phase 5's auto-expand/auto-collapse behavior, and the "of N" batch-size high-water mark | `FloatingUploadManager` |

Both presentation models are hooks (`use*`) precisely so a component can
call one and get back a single, already-resolved object — no component ever
reruns `summarizeReadiness()` or `summarizeUploads()` itself, and no two
components can ever disagree about what "ready" or "N Completed" means,
because there is exactly one place that computes it.

## The components (pure renderers)

A pure renderer receives an already-resolved model (or a slice of one) as
props/hook-output and only renders it. It performs no derivation: no
filtering, no counting, no status-to-label mapping, no expand/collapse
decision-making. If a component finds itself writing `item.status ===
"uploading" ? ... : ...` or filtering a list by a business condition, that
logic has leaked out of its engine/model and belongs back there.

| Component | Renders | Consumes |
|---|---|---|
| `SubmissionReadinessPanel.jsx` | The requirement checklist, per-item readiness rows, overall progress bar | `experience.checklist`, `experience.overallProgress` (Submission Experience Model) |
| Section-header `SectionStatusBadge` (in `SubmissionPage.jsx`) | Per-section rollup badge, click-to-navigate | `experience.sectionStatus` (Submission Experience Model) + the generic `revealAndJumpToRequirementItem` navigation helper (`lib/scrollHighlight.js`) |
| Submit CTA button (in `SubmissionPage.jsx`) | Button label/disabled/action | `experience.submitCta` (Submission Experience Model) |
| `FloatingUploadManager.jsx` | The Upload Activity Panel — summary line, headline, progress bar, expandable per-item list | `useUploadActivityModel(...)`'s full return value (Upload Activity Model) |

## Why this matters

This layering is what lets a UI surface grow (Phase 1 → Phase 5 of the
Submission Experience overhaul, see [06_RELEASE_HISTORY.md](06_RELEASE_HISTORY.md)
and [08_DECISION_LOG.md](08_DECISION_LOG.md)) without any single component
becoming the place where business rules, upload state, and rendering all
get tangled together. Every new derived value (a new badge, a new summary
line, a new navigation shortcut) has exactly one correct home:

- A new **business rule** (what counts as required/satisfied/blocking) →
  the Requirement or Readiness Engine.
- A new **upload-state translation** (a new raw status, a new aggregate
  count) → the Operational Engine.
- A new **combination** of already-computed engine outputs, or new
  self-contained UI timing/expand state → the relevant Presentation Model.
- A new **visual treatment** of an already-resolved value → the component,
  and only the component.
