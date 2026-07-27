/**
 * Extracted from PortalProfile.jsx's parseStoredLink (Phase 3 item 2).
 * PortalProfile.jsx/ApplicationPage.jsx/SubmissionPage.jsx each keep their
 * own copy (edit-flow specific, out of scope to touch here) — this is the
 * same parsing made available to Project Detail's read-only display,
 * rather than re-implementing it a fourth time.
 */
export function parseStoredLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        const url = stored.slice(idx + 4).trim().replace(/[.,;:!?)\]>]+$/, "");
        return { label: stored.slice(0, idx).trim(), url };
    }
    const url = (stored || "").replace(/[.,;:!?)\]>]+$/, "");
    return { label: "", url };
}
