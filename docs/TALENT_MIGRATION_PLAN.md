# Talent Dashboard — Migration Plan

Status: **Approved**. Strangler-fig migration — old flows keep working throughout, nothing about the backend submission/application model changes as a precondition, so there is no data migration risk to sequence around.

## Entry-point map (verified live against the running app — see architecture-audit history for evidence)

| Mechanism | Generates | URL — **frozen, never changes** | Currently lands on | After Phase 1+ |
|---|---|---|---|---|
| Project Submission Link | `ProjectEdit.jsx:315` | `submit.talentgramagency.com/{slug}` | `/submit/[slug]` → `SubmissionPage.jsx` monolith | Same URL → auth gate → Dashboard, `Context=Project` |
| WhatsApp `{{submission_link}}` | `whatsapp.py:775` | same as above | same | same |
| Talent Invite Link | `Dashboard.jsx:378` | `apply.talentgramagency.com` | `/apply` → `ApplicationPage.jsx` monolith | Same URL → auth gate → Dashboard, `Context=Profile` (first-time) or `Dashboard` (returning) |
| Google OAuth callback | shared | `/google-callback?state=...` | redirects back to whichever of the above initiated it | **Unchanged** — no new redirect branches needed; `/submit`/`/apply` themselves now act on the `portal_token` they already receive |
| OTP email | `auth.py:339` | no link, code only | N/A | N/A |
| Legacy portal URL | nothing distributes this | `talentgramagency.com/portal/{slug}` | `/portal/:slug` → `PortalGateway.jsx` | **Retired as a public entry point** (Phase 1 item 2 replaces it with a real standalone entry — see below) |

**Confirmed out of scope, no changes**: `links.talentgramagency.com/{slug}` (`ClientView.jsx`, brand-client facing), `/admin/*`, `/signup`, `/forgot-password`, `/reset-password` (staff/admin identity).

## Phase 1 (this pass)

Priority order, per approval — each item gets its own live smoke test before moving to the next, per the mandatory dev workflow:

1. **Location schema fix** — `PortalProfileUpdateIn.location: Optional[str]` (`routers/portal.py`) vs. master `TalentIn.location: List[LocationItem]` (`core.py:1731`). Fix with backward compatibility (accept either shape on read, always persist the structured shape), plus an audit of any `db.talents` rows already corrupted by a real `PUT /portal/profile` call in production.
2. **Standalone Dashboard login entry point** — a real `/portal/login` (or equivalent) page that does not require a project slug in the URL, fixing the confirmed gap where a returning talent has no way into their Dashboard without a live project link in hand.
3. **Consolidated authentication** — one shared OTP/Google implementation (extracted from the three near-identical copies in `SubmissionPage.jsx`, `ApplicationPage.jsx`, `PortalGateway.jsx`), used by `/submit`, `/apply`, and the new `/portal/login`.
4. **Revive and extend the existing Portal** — `PortalApp.jsx`/`PortalHome.jsx`/`PortalProfile.jsx` become the base of `DashboardLayout`/`ProjectsList`/`Profile`, not replaced.
5. Extension over replacement, applied everywhere — no new component is created where an existing one already covers ~80% of the need (`PortalProfile.jsx`, `SubmissionReadinessPanel.jsx`, `PremiumUploadSlot`, etc.).
6. Every URL in the table above keeps working, unchanged, throughout.

**Backend change required for the state model** (documented in `TALENT_STATE_MODEL.md`): move `/apply`'s `merge_talent_profile()`/`talents`-row-creation trigger from `set_application_decision(decision="approved")` to the application's first finalize — matching `/submit`'s already-correct timing. This is what makes "immediate Dashboard access, Application Under Review until approved" possible without inventing a new mechanism.

## Later phases (not started, sequenced after Phase 1 review)

- **Phase 2** — `ProjectDetail` screen (the actual replacement for opening the giant form), wired to `SubmissionReadinessPanel` + the extracted project-scoped fields.
- **Phase 3** — Profile page's Portfolio section (extracted `UploadSlot`), Profile Health indicator.
- **Phase 4** — Cutover: `/submit/[slug]` and `/apply` stop rendering their monoliths directly and become pure auth-gate-then-Dashboard-hand-off for all traffic, not just pilot projects. Old monolith code removed only after full parity is confirmed live.
- **Phase 5** — Notifications (architecture reserved in the state model's Current Context axis; not designed in detail until explicitly scheduled).

## Risks carried into Phase 1

- **`getCompleteness()` in `SubmissionReviewCenter.jsx:338`** vs. the Requirement/Readiness engine — two independent "is this complete" calculators. Must not let Profile Health become a third. Reconcile before Phase 3, not blocking Phase 1.
- **Auth consolidation is the highest-stakes single change in Phase 1** — it touches the one surface that, if broken, locks out every talent on every entry point simultaneously. Gets its own isolated smoke test before anything else in item 3 proceeds, per the mandatory workflow's "minimum changes, test before moving on" rule.
- **Two requirement-config systems** (`Project.submission_requirements` vs `profile_configs`) remain unreconciled — out of scope for Phase 1, flagged again so it isn't forgotten once `ProjectDetail` (Phase 2) needs to render both shapes.
