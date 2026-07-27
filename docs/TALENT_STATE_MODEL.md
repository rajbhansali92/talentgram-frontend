# Talent State Model

Status: **Approved**. Every entry point (`/submit`, `/apply`, the new `/portal/login`) resolves through this model rather than maintaining separate logic paths. See `TALENT_DASHBOARD_ARCHITECTURE.md` for how this drives the Dashboard shell, and `TALENT_MIGRATION_PLAN.md` for how each entry point wires into it.

Three independent axes: **Account State** (who is this person, what can they access), **Engagement State** (where do they stand on *this* project or application — one instance per engagement), **Current Context** (what should be open right now). Conflating these into one linear list breaks the moment a talent has two projects at once in different states.

---

## 1. Account State

| State | Detection (existing fields — no new schema beyond the Option A session) | Dashboard access |
|---|---|---|
| `ANONYMOUS` | No session token | Auth gate only |
| `IDENTITY_VERIFIED` | OTP/Google ownership proven, **no `talents` row yet** — temporary session (Option A, below) | Narrow: only the profile/portfolio capture + the one project/application that initiated this session. Projects list hidden — nothing else exists yet for this identity. |
| `TALENT` | `talents` row exists, real `portal_token` issued | Full Dashboard: Projects, Profile, Settings — **regardless of profile completeness** |
| `SESSION_EXPIRED` | `current_portal_talent` 401 (existing, unchanged) | Bounced to auth gate. Not a lifecycle state — reachable from `IDENTITY_VERIFIED` or `TALENT` at any time. |

Two orthogonal **badges** on `TALENT` (cosmetic, never access gates):
- **Profile Health** — replaces "Profile Completeness" as a quality indicator, not a restriction. Extends `requirementEngine.js`'s pattern to a Dashboard-wide field set (missing media, missing work links, outdated intro video, missing skills). Computed, not stored.
- `isActiveTalent` — has ≥1 engagement with `decision == "approved"`. Label only.

### Option A — temporary identity session (approved)

Do **not** create a `talents` row on OTP/Google verification alone. Reuse the existing submitter-role JWT shape (already used for anonymous-but-verified submission/application access) rather than inventing a new token type: mint `{role: "submitter", kind: "identity", email}`, short-lived, when no `talents` row exists yet. Its presence = `IDENTITY_VERIFIED`.

The `talents` row is created only at first successful finalize (Requirement Engine confirms completeness) — exactly how `/submit` already behaves today via `merge_talent_profile()`. At that moment the temporary session is silently upgraded to a real `portal_token`; the talent never sees the swap.

**Why this is safe** (and not a repeat of the `submission_drafts` anti-pattern flagged in the architecture audit): finalize only fires once the Requirement Engine has validated completeness, so this never creates a stub row for someone who abandoned the form mid-way.

---

## 2. Engagement State

One instance per project submission *or* per open application. Already ~1:1 with existing `status`/`decision` fields (`core.py:1648-1649`) — this is a **presentation-layer synthesis function**, not a new backend concept.

| State | Detection |
|---|---|
| `INVITED` | `/submit` only, implicit — no `submissions` row exists yet for `(project_id, email)`. Not listed until first opened. |
| `DRAFT_STARTED` | `status == "draft"` AND `readinessModel.hasAnyProgress` |
| `READY_TO_SUBMIT` | `status == "draft"` AND `missingRequirements.length === 0` (Requirement Engine, already computes this exactly) |
| `SUBMITTED_UNDER_REVIEW` | `status ∈ {submitted, updated}` AND `decision ∈ {pending, ask_to_test}` |
| `ACTION_REQUIRED` | **Reserved, not fully wired in Phase 1.** No first-class "admin requests something" signal exists in the backend today — `SubmissionReviewCenter`'s decision-note field is internal-only (admin-facing, not synced to the talent). Phase 1 ships the state slot and UI treatment; wiring a real trigger (e.g. a talent-visible note, or `decision == "hold"` as an interim proxy) is explicitly deferred — do not invent a new backend field for this without a separate approval. |
| `SHORTLISTED` | `decision == "shortlisted"` |
| `ON_HOLD` | `decision == "hold"` |
| `APPROVED` | `decision == "approved"` |
| `NOT_SELECTED` | `decision ∈ {rejected, does_not_work_for_this}` |

**Not fully terminal**: an existing talent edit/resubmit can bounce `SHORTLISTED`/`ON_HOLD`/`APPROVED`/`NOT_SELECTED` back to `SUBMITTED_UNDER_REVIEW` — but only if `project.require_reapproval_on_edit` is true (existing flag, unchanged). Otherwise the decision stands and only `form_data`/`media` update. The Dashboard must read this per-project flag, not assume one behavior universally.

**No "withdraw" action exists in the backend today.** Noted as absent, not implemented speculatively.

### Apply-flow parity (approved)

`/apply` reuses the exact same Engagement State table — an open application is not a different shape, it's an engagement whose data source is `db.applications` instead of `db.submissions`. Per your approval, **Dashboard access is granted at first successful finalize, not at admin approval** — this requires moving `/apply`'s talent-creation trigger (`merge_talent_profile()`) from `set_application_decision(decision="approved")` to the finalize step, mirroring `/submit`'s already-correct timing. `SUBMITTED_UNDER_REVIEW` is what renders as "Application Under Review" in the UI.

---

## 3. Current Context

Determines what the Dashboard opens automatically on load — resolved once, by the auth gate, from how the talent arrived.

| Context | Trigger |
|---|---|
| `Dashboard` | Entered via the standalone `/portal/login` entry point, or no specific target in the incoming URL |
| `Project` | Entered via `submit.talentgramagency.com/{slug}` — opens directly to that project's `ProjectDetail` |
| `Profile` | `IDENTITY_VERIFIED` talent mid-onboarding, or explicit navigation |
| `Media` | Deep link into a specific Profile Portfolio section |
| `Notifications` | Reserved — architecture space only, not built in Phase 1 |
| `Settings` | Explicit navigation only |

Every screen this resolves to must itself be deep-linkable (`Dashboard → Project → Media → Introduction Video`, `Checklist item → exact upload field`) — this is what lets the Smart Checklist jump directly to a field instead of requiring manual search, and is a hard requirement for any future mobile client consuming the same URLs.

---

## How entry points resolve through this model

```
/submit/[slug]  ──┐
                   ├──▶ shared auth gate (consolidated Phase 1 item 3)
/apply          ──┤          │
                   │   resolveEntryState(accountState, engagementState, context)
/portal/login   ──┘          │
              ┌───────────────┼────────────────┐
       IDENTITY_VERIFIED   TALENT +          TALENT, no
       → guided Profile    engagement        specific target
       capture for the     in progress       → Dashboard home,
       one project/         → ProjectDetail    Context = Dashboard
       application that      checklist
       initiated this
       session, Context = Profile
```

`GoogleCallback.jsx` and the OTP verify handlers do not need to know about state — they only prove identity and hand control back to the gate, which is the single place this resolution runs for every entry point.
