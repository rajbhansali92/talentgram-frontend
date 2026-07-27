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

    // Ongoing statuses
    if (project.status === "draft") {
        return { color: "bg-black/25", text: "Draft / Continuation" };
    }
    if (project.status === "submitted" || project.status === "updated") {
        return { color: "bg-blue-500", text: "Awaiting Review" };
    }
    return { color: "bg-black/40", text: "Active" };
}
