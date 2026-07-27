# Talent Dashboard — Architecture

Status: **Approved**, Phase 1 in progress.
This document, `TALENT_STATE_MODEL.md`, and `TALENT_MIGRATION_PLAN.md` are the canonical reference for this work. See `docs/claude/03_BUSINESS_RULES.md` for the pre-existing Global Talent Profile sync rules this architecture builds on — it is not superseded.

## Objective

One permanent Talent Dashboard, evolved from the existing Portal (`PortalApp.jsx` / `routers/portal.py`), not a new parallel system. A talent should never need to know which link they clicked, which flow they came from, or whether they're a first-timer — the Dashboard resolves the correct experience from **Account State + Engagement State + Current Context** (see `TALENT_STATE_MODEL.md`).

## What already exists and is reused as-is (not rebuilt)

| System | Location | Role in this architecture |
|---|---|---|
| Global Talent Profile | `core.py:1731` `TalentIn`/`TalentOut` | Single source of truth. Never owned by a project. |
| Field merge-policy classification | `core.py:2817-2843`, `docs/claude/03_BUSINESS_RULES.md` | Governs what a submission/application is allowed to write back to the master record. Unchanged. |
| Project Override system | `admin_edit_submission()`, `original_form_data`/`original_media` | Per-submission isolation. Unchanged. |
| Portal auth + session | `routers/portal.py`, `mint_portal_token`/`current_portal_talent` (`core.py:339-369`) | Base of the consolidated auth flow (Phase 1 item 3). |
| Requirement Engine | `lib/requirementEngine.js` | Drives per-project pending-items. Extended (not replaced) for Dashboard-wide Profile Health. |
| Readiness Engine | `lib/readinessStatus.js` | Upload/operational state. Reused as-is. |
| `useSubmissionExperienceModel` | `hooks/useSubmissionExperienceModel.js` | Aggregator. Reused; will also back the Smart Checklist. |
| `SubmissionReadinessPanel` | `components/shared/SubmissionReadinessPanel.jsx` | Presentational checklist. Reused, extended with deep-link targets per the Smart Checklist requirement. |
| Upload Manager | `context/UploadManagerContext.jsx` | Global upload engine. Reused; `PremiumUploadSlot` (currently locked inside `SubmissionPage.jsx`) is extracted into a shared widget, not rewritten. |
| Cloudinary / R2 pipeline | `cloudinary_upload`, presigned R2 flow | Unchanged. |
| `portalApi` client | `lib/api.js` | The Dashboard's API client. Unchanged. |

## Information Architecture (per approved Phase 1 spec — revised from the earlier draft)

```
Talent Dashboard (permanent shell — stays mounted, only content changes)
├── Dashboard        — overview, pending items across all engagements, Recent Profile Activity
├── Projects         — Invited / Active / Submitted / Shortlisted / Completed
│                       (Applications is NOT a separate nav item — an open
│                        application is just another card in this same list,
│                        distinguished by its Engagement State)
├── Profile           — ONE page, not split into Profile + Portfolio:
│                        Personal Information · Portfolio Media · Skills ·
│                        Social · Work Links
├── Settings
└── (reserved, not built) Notifications
```

This revises the Part-1 audit's original proposal (which had separate "My Profile" / "My Portfolio" / "Applications" nav items) per your explicit Phase 1 direction. Portfolio media becomes a section of the one Profile page, and Applications fold into Projects as an Engagement State rather than a parallel data source in the nav.

## Component tree (target shape)

```
PortalApp.jsx (existing react-router shell, extended not replaced)
└── DashboardLayout (new — modeled on AdminLayout.jsx's Outlet/NavLink pattern)
    ├── DashboardNav (Dashboard / Projects / Profile / Settings)
    ├── DashboardHome (new) — aggregates useSubmissionExperienceModel per engagement
    ├── ProjectsList (extends PortalHome.jsx's existing grouping logic)
    ├── ProjectDetail (new) — the actual replacement for "open the giant form"
    │     ├── SubmissionReadinessPanel (reused as-is)
    │     └── project-scoped fields only (Availability/Budget/Location/etc.,
    │           extracted from SubmissionPage.jsx, not re-implemented)
    ├── Profile (extends PortalProfile.jsx)
    │     ├── PersonalInformation section
    │     ├── Portfolio section (new — UploadSlot, extracted from
    │     │     SubmissionPage.jsx's PremiumUploadSlot)
    │     ├── Skills / Social / WorkLinks sections
    │     └── ProfileHealth indicator (extends requirementEngine.js)
    └── Settings (new, minimal in Phase 1)
```

## Data flow (unchanged from the approved audit — restated for reference)

```
Master Profile (db.talents) ──read──▶ Prefill Project/Profile screens
       ▲                                       │
       │ merge_talent_profile()                │ talent fills project-specific
       │ (AUTO_UPDATE fields only,              ▼
       │  first finalize only)            db.submissions / db.applications doc
       └───────────────────────────────────────┘
                                                 │
                                        admin_edit_submission()
                                        → original_form_data snapshot
                                        → db.talents NEVER touched
```

No change to this flow. The Dashboard is an orchestration layer on top of it — it does not own or duplicate talent/submission/media data.

## Entry points (unchanged URLs — see `TALENT_MIGRATION_PLAN.md` for the full map)

`submit.talentgramagency.com/{slug}` and `apply.talentgramagency.com` remain permanent. What changes is what runs behind them after authentication — both become thin gates into this Dashboard instead of standalone monoliths. Full detail, including the new standalone `/portal/login` entry point (Phase 1 item 2), is in the migration plan.

## Explicitly out of scope for this architecture

- `links.talentgramagency.com/{slug}` (`ClientView.jsx`) — brand-client facing, not a talent surface.
- `/admin/*`, `/signup`, `/forgot-password`, `/reset-password` — staff/admin identity, unrelated state model.
- Notifications — architecture reserved (see Current Context axis in `TALENT_STATE_MODEL.md`), not built in Phase 1.
