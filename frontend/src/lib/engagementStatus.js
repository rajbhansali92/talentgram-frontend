/**
 * Extracted verbatim from ProjectCard.jsx's getStatusDetails() (Phase 2
 * item 4 — Project Detail, see docs/TALENT_DASHBOARD_ARCHITECTURE.md).
 * Same status/decision → label/color mapping, now shared between
 * ProjectCard and ProjectDetail instead of living only inside ProjectCard.
 * No new logic — this is the same thing ProjectCard already computed.
 */
export function getEngagementStatusDetails(project, theme) {
    if (theme === "shortlisted") {
        return { color: "bg-amber-500", text: "Shortlisted" };
    }
    if (theme === "completed") {
        return { color: "bg-green-600", text: "Completed" };
    }

    // Ongoing statuses — `project.status` here is the engagement card's
    // `status` field, a verbatim copy of the submission's own `status`
    // (see routers/portal.py's `/portal/projects`: `"status": sub.get("status", "draft")`).
    // retest/selected/rejected/approved were real values this function
    // already received but had no branch for, silently falling through to
    // the generic "Active" case below — same status system, same values
    // SubmissionPage.jsx's own status switch already names; this just
    // completes coverage here too.
    if (project.status === "draft") {
        return { color: "bg-black/25", text: "Draft / Continuation" };
    }
    if (project.status === "submitted" || project.status === "updated") {
        return { color: "bg-blue-500", text: "Awaiting Review" };
    }
    if (project.status === "retest") {
        return { color: "bg-rose-500", text: "Retest Requested" };
    }
    if (project.status === "selected") {
        return { color: "bg-emerald-600", text: "Selected" };
    }
    if (project.status === "approved") {
        return { color: "bg-emerald-600", text: "Approved" };
    }
    if (project.status === "rejected") {
        return { color: "bg-slate-400", text: "Closed" };
    }
    return { color: "bg-black/40", text: "Active" };
}

/**
 * Dashboard UX audit (Phase 3 item 7) — with several ongoing engagements at
 * once, a `retest` (the one status that genuinely needs the talent to act)
 * could sit buried among drafts/awaiting-review/closed cards with no
 * visual priority, ordered only by whatever the backend happened to return.
 * This is a pure client-side reorder of cards the caller already fetched —
 * same `status` field getEngagementStatusDetails() already reads, no new
 * data, no new meaning assigned to any status. `Array.prototype.sort` is
 * stable (ES2019+), so within each urgency tier the caller's existing
 * order — e.g. already sorted by recency — is preserved.
 */
export function sortByUrgency(projects) {
    return [...projects].sort((a, b) => {
        const aUrgent = a.status === "retest" ? 0 : 1;
        const bUrgent = b.status === "retest" ? 0 : 1;
        return aUrgent - bUrgent;
    });
}
