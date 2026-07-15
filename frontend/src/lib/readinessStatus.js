// THE OPERATIONAL ENGINE + THE READINESS ENGINE.
//
// This file holds two of the three engines behind the Submission Experience
// Model (see hooks/useSubmissionExperienceModel.js for the aggregator, and
// lib/requirementEngine.js for the third — the Requirement Engine, which
// decides what's required/optional/hidden from project config):
//
//   OPERATIONAL ENGINE  — translates the Upload Manager's raw activeUploads
//                         state into OPERATIONAL_STATES
//                         (mapUploadManagerStatus, findActiveUploadStatus,
//                         deriveOperationalStatus, summarizeUploads).
//
//   READINESS ENGINE    — combines Requirement Engine output (requirement +
//                         satisfied) with Operational Engine output
//                         (operational) into submission-level and
//                         section-level summaries (summarizeReadiness,
//                         resolveBlockingReason, deriveSubmitCta,
//                         summarizeSections, summarizeOverallProgress).
//
// Two independent axes feed both engines, deliberately kept separate so they
// can never drift out of sync with each other:
//
//   REQUIREMENT_TIERS  — "is this required, optional, or hidden?"
//                         Decided ENTIRELY by the validation/config engine
//                         (project.submission_requirements). Never touches
//                         upload state.
//
//   OPERATIONAL_STATES — "what is the live state of this item right now?"
//                         Decided ENTIRELY by form data + the Upload Manager
//                         (UploadManagerContext's activeUploads). Never
//                         touches requirement config.
//
// These, plus SUBMIT_BLOCKING_REASONS, CTA_ACTIONS, and SECTION_STATUS
// below, are the ONLY enums for readiness state anywhere in the submission
// experience. No component, page, or lib should compare against a raw
// "required"/"missing"/etc. string literal — always reference the exported
// enum so a typo becomes a ReferenceError instead of a silent mismatch.
//
// Nothing in this module, in requirementEngine.js, in
// useSubmissionExperienceModel.js, or in any UI component computes business
// rules redundantly — each engine owns its own concern, the hook only
// aggregates their outputs, and every UI component (SubmissionReadinessPanel,
// the Submit button, the footer, the upload manager, section headers) only
// ever renders what the aggregated model already decided.

export const REQUIREMENT_TIERS = Object.freeze({
    REQUIRED: "required",
    OPTIONAL: "optional",
    HIDDEN: "hidden",
});

export const OPERATIONAL_STATES = Object.freeze({
    MISSING: "missing",
    QUEUED: "queued",
    UPLOADING: "uploading",
    RETRYING: "retrying",
    WAITING: "waiting",
    PROCESSING: "processing",
    FAILED: "failed",
    COMPLETED: "completed",
});

// A THIRD, distinct enum: not a per-item state (that's OPERATIONAL_STATES),
// but the single reason `summarizeReadiness` gives for why Submit is blocked
// across all required items right now. Kept separate from OPERATIONAL_STATES
// on purpose — "waiting" here summarizes the whole IN_FLIGHT_OPERATIONAL_
// STATES set (queued/uploading/retrying/waiting/processing all collapse to
// one reason), it isn't one specific per-item state.
export const SUBMIT_BLOCKING_REASONS = Object.freeze({
    MISSING: "missing",
    WAITING: "waiting",
    FAILED: "failed",
});

// Priority order so `deriveOperationalStatus` can pick the "most urgent"
// status when multiple upload-manager entries match one requirement item
// (e.g. 3 portfolio images where one failed and one is still uploading — the
// failure wins).
const OPERATIONAL_PRIORITY = {
    [OPERATIONAL_STATES.FAILED]: 7,
    [OPERATIONAL_STATES.RETRYING]: 6,
    [OPERATIONAL_STATES.WAITING]: 5,
    [OPERATIONAL_STATES.UPLOADING]: 4,
    [OPERATIONAL_STATES.QUEUED]: 3,
    [OPERATIONAL_STATES.PROCESSING]: 2,
    [OPERATIONAL_STATES.COMPLETED]: 1,
    [OPERATIONAL_STATES.MISSING]: 0,
};

// UploadManagerContext's raw `activeUploads[slotKey].status` values today are
// "compressing" | "uploading" | "processing" | "completed" | "failed" — that
// vocabulary belongs to the upload engine itself (pre-dates this feature) and
// is intentionally left as-is; this table is the ONE place it gets translated
// into our canonical OPERATIONAL_STATES. "queued"/"retrying"/"waiting" don't
// exist as distinct engine states yet (Phase 6 bounded queue, Phase 5/6
// surfaced retries, Phase 7 offline-aware retry); when those phases add them,
// only this table needs a new line — nothing downstream (this file, the
// page, the panel) needs to change again.
const RAW_STATUS_TO_OPERATIONAL = {
    compressing: OPERATIONAL_STATES.UPLOADING,
    uploading: OPERATIONAL_STATES.UPLOADING,
    processing: OPERATIONAL_STATES.PROCESSING,
    completed: OPERATIONAL_STATES.COMPLETED,
    failed: OPERATIONAL_STATES.FAILED,
    queued: OPERATIONAL_STATES.QUEUED,
    retrying: OPERATIONAL_STATES.RETRYING,
    waiting: OPERATIONAL_STATES.WAITING,
};

export function mapUploadManagerStatus(rawStatus) {
    return RAW_STATUS_TO_OPERATIONAL[rawStatus] || null;
}

// The subset of OPERATIONAL_STATES that mean "actively moving, will resolve
// on its own" — as opposed to FAILED (stalled, needs the talent to act) or
// MISSING/COMPLETED (terminal, nothing pending). Centralized here so every
// consumer (the Submit-button disable check, the footer messaging) reads the
// same definition instead of each re-deriving its own list.
export const IN_FLIGHT_OPERATIONAL_STATES = Object.freeze([
    OPERATIONAL_STATES.QUEUED,
    OPERATIONAL_STATES.UPLOADING,
    OPERATIONAL_STATES.RETRYING,
    OPERATIONAL_STATES.WAITING,
    OPERATIONAL_STATES.PROCESSING,
]);

/**
 * Scans the Upload Manager's `activeUploads` map for any slot belonging to
 * `prefix` (exact match, or `prefix:` namespace — see UploadManagerContext's
 * `slotKey = category:label`) and returns the highest-priority OPERATIONAL_
 * STATES value found, or null if nothing in the upload manager currently
 * concerns this item.
 */
export function findActiveUploadStatus(activeUploads, prefix) {
    if (!prefix || !activeUploads) return null;
    let best = null;
    let bestRank = -1;
    Object.entries(activeUploads).forEach(([slotKey, upload]) => {
        if (slotKey !== prefix && !slotKey.startsWith(prefix)) return;
        const mapped = mapUploadManagerStatus(upload.status);
        if (!mapped) return;
        const rank = OPERATIONAL_PRIORITY[mapped] ?? -1;
        if (rank > bestRank) {
            bestRank = rank;
            best = mapped;
        }
    });
    return best;
}

/**
 * Combines a requirement item's validation-engine `satisfied` verdict with
 * live Upload Manager state into ONE OPERATIONAL_STATES value. This is the
 * only place the two signals meet — everything upstream (getRequirementItems)
 * and downstream (SubmissionReadinessPanel) only ever deals with one axis at
 * a time.
 *
 * - `satisfied` (validation engine): does the current form/media data meet
 *   the configured requirement (e.g. count >= min)? Pure business rule, no
 *   upload-manager awareness.
 * - `media` (validation engine): optional `{ prefix }` telling us which
 *   Upload Manager slot(s), if any, correspond to this item.
 * - `activeUploads` (Upload Manager): the live queue, read-only.
 */
export function deriveOperationalStatus({ satisfied, media, activeUploads }) {
    if (satisfied) return OPERATIONAL_STATES.COMPLETED;
    const liveStatus = media ? findActiveUploadStatus(activeUploads, media.prefix) : null;
    return liveStatus || OPERATIONAL_STATES.MISSING;
}

/**
 * The single source of truth for "why can't this talent submit yet" — turns
 * a list of already-combined REQUIRED items (requirement + operational, see
 * `deriveOperationalStatus`) into one clear SUBMIT_BLOCKING_REASONS value
 * instead of leaving every consumer to independently guess which of missing /
 * waiting-on-an-upload / upload-failed applies. Callers (the footer, a future
 * toast, etc.) should render off `blockingReason` — they should never
 * re-inspect `activeUploads` or re-derive this priority themselves.
 *
 * Priority: FAILED > WAITING > MISSING. A failed upload doesn't resolve on
 * its own and needs the talent to act, so it's surfaced ahead of "still
 * uploading" (which will clear itself) and ahead of a plain "missing"
 * reading, which would otherwise misrepresent a failed upload as if the
 * talent had never attempted it at all.
 */
export function summarizeReadiness(items) {
    const failed = items.filter((item) => item.operational === OPERATIONAL_STATES.FAILED);
    const inFlight = items.filter((item) => IN_FLIGHT_OPERATIONAL_STATES.includes(item.operational));
    const missing = items.filter((item) => item.operational === OPERATIONAL_STATES.MISSING);
    const ready = failed.length === 0 && inFlight.length === 0 && missing.length === 0;

    let blockingReason = null;
    if (!ready) {
        blockingReason = failed.length > 0
            ? SUBMIT_BLOCKING_REASONS.FAILED
            : inFlight.length > 0
                ? SUBMIT_BLOCKING_REASONS.WAITING
                : SUBMIT_BLOCKING_REASONS.MISSING;
    }

    return { ready, blockingReason, failed, inFlight, missing };
}

// What a Submit button click should actually do — a second enum (distinct
// from OPERATIONAL_STATES/SUBMIT_BLOCKING_REASONS) because it describes an
// ACTION, not a state.
export const CTA_ACTIONS = Object.freeze({
    SUBMIT: "submit",
    SCROLL_TO_MISSING: "scroll_to_missing",
});

/**
 * The single source of truth for the submission CTA (the Submit button's
 * label, whether it's disabled, and what clicking it should do). The button
 * component should be a PURE renderer of this — it should never independently
 * decide its own label or disabled state from `finalizing`/`uploadsInProgress`
 * or by re-reading `readinessSummary` itself.
 *
 * Inputs are the three signals that together determine "can this actually be
 * submitted right now": the readiness model's `summarizeReadiness()` output
 * (required items only), `uploadsInProgress` (ANY upload in flight, required
 * or optional — the backend refuses to finalize while anything is pending
 * regardless of category), and `finalizing` (a finalize request is already
 * in flight).
 *
 * Returns:
 * - `ready`          — true only when a Submit click would actually succeed
 * - `buttonLabel`     — what the button should say
 * - `buttonAction`    — CTA_ACTIONS.SUBMIT | CTA_ACTIONS.SCROLL_TO_MISSING
 * - `disabled`        — true only when clicking would do nothing useful
 *                        (already submitting, or blocked with no actionable
 *                        item to send the talent to)
 * - `firstMissingRequirement` / `scrollTarget` — the same item, exposed
 *   under both names: the most actionable outstanding requirement item
 *   (failed upload first — needs a retry — then a genuinely missing item,
 *   then, as a last resort, a required item that's still mid-upload), or
 *   null when there's nothing left to point at.
 *
 * `readyLabel`/`notReadyLabel`/`submittingLabel` let each flow keep its own
 * product copy (e.g. "Submit Audition" here vs. "Submit Application" on the
 * Talent Invite flow) without forking the underlying CTA logic.
 */
export function deriveSubmitCta({
    readinessSummary,
    uploadsInProgress,
    finalizing,
    readyLabel = "Submit Application",
    notReadyLabel = "Complete Remaining Items",
    submittingLabel = "Submitting…",
}) {
    if (finalizing) {
        return {
            ready: false,
            buttonLabel: submittingLabel,
            buttonAction: CTA_ACTIONS.SUBMIT,
            disabled: true,
            firstMissingRequirement: null,
            scrollTarget: null,
        };
    }

    const ready = readinessSummary.ready && !uploadsInProgress;
    if (ready) {
        return {
            ready: true,
            buttonLabel: readyLabel,
            buttonAction: CTA_ACTIONS.SUBMIT,
            disabled: false,
            firstMissingRequirement: null,
            scrollTarget: null,
        };
    }

    const priorityItem =
        readinessSummary.failed[0] ||
        readinessSummary.missing[0] ||
        readinessSummary.inFlight[0] ||
        null;

    return {
        ready: false,
        buttonLabel: notReadyLabel,
        buttonAction: CTA_ACTIONS.SCROLL_TO_MISSING,
        // Only truly nothing to do when every required item is satisfied and
        // the sole remaining blocker is an unrelated optional upload still in
        // flight — scrolling would have no target, so disable rather than
        // perform a no-op click.
        disabled: !priorityItem,
        firstMissingRequirement: priorityItem,
        scrollTarget: priorityItem,
    };
}

/**
 * Resolves the ONE reason to show the talent for why Submit isn't available
 * right now — used by footer/validation messaging. Takes `uploadsInProgress`
 * (ANY upload, required or optional) into account as well as
 * `readinessSummary` (required items only), because the backend blocks
 * finalize on ANY pending upload regardless of category — so "waiting" can
 * be true even when every required item individually reads MISSING/COMPLETE
 * in `readinessSummary`. Returns a SUBMIT_BLOCKING_REASONS value, or null
 * when nothing is blocking submission. This is the single place that
 * priority decision is made — no UI component should re-derive it.
 */
export function resolveBlockingReason({ readinessSummary, uploadsInProgress }) {
    if (readinessSummary.blockingReason === SUBMIT_BLOCKING_REASONS.FAILED) {
        return SUBMIT_BLOCKING_REASONS.FAILED;
    }
    if (uploadsInProgress || readinessSummary.blockingReason === SUBMIT_BLOCKING_REASONS.WAITING) {
        return SUBMIT_BLOCKING_REASONS.WAITING;
    }
    if (readinessSummary.blockingReason === SUBMIT_BLOCKING_REASONS.MISSING) {
        return SUBMIT_BLOCKING_REASONS.MISSING;
    }
    return null;
}

// ---------------------------------------------------------------------------
// OPERATIONAL ENGINE — upload-manager-wide summary (for the Upload Manager
// panel / a future "Uploading 2 of 8" widget). Counts ALL activeUploads, not
// just ones tied to a requirement item — an optional photo mid-upload is
// still part of "what's going on with uploads right now."
// ---------------------------------------------------------------------------
export function summarizeUploads(activeUploads) {
    const counts = {
        [OPERATIONAL_STATES.QUEUED]: 0,
        [OPERATIONAL_STATES.UPLOADING]: 0,
        [OPERATIONAL_STATES.RETRYING]: 0,
        [OPERATIONAL_STATES.WAITING]: 0,
        [OPERATIONAL_STATES.PROCESSING]: 0,
        [OPERATIONAL_STATES.FAILED]: 0,
        [OPERATIONAL_STATES.COMPLETED]: 0,
    };
    const uploads = activeUploads ? Object.values(activeUploads) : [];
    uploads.forEach((upload) => {
        const mapped = mapUploadManagerStatus(upload.status);
        if (mapped && counts[mapped] !== undefined) counts[mapped] += 1;
    });
    const inFlightTotal = IN_FLIGHT_OPERATIONAL_STATES.reduce((sum, state) => sum + counts[state], 0);
    return {
        counts,
        total: uploads.length,
        inFlightTotal,
        failedTotal: counts[OPERATIONAL_STATES.FAILED],
        completedTotal: counts[OPERATIONAL_STATES.COMPLETED],
    };
}

// A per-SECTION rollup status — distinct from OPERATIONAL_STATES (per item)
// and SUBMIT_BLOCKING_REASONS (whole-submission). ATTENTION takes priority
// over IN_PROGRESS/INCOMPLETE the same way FAILED takes priority in
// summarizeReadiness: a failed upload needs the talent to act, so it should
// never be visually indistinguishable from "just not done yet."
export const SECTION_STATUS = Object.freeze({
    COMPLETE: "complete",
    IN_PROGRESS: "in_progress",
    ATTENTION: "attention",
    INCOMPLETE: "incomplete",
    OPTIONAL: "optional",
});

/**
 * Groups the FULL readiness model (required + optional + hidden, each with
 * `operational` already computed) by `section` and rolls each group up into
 * one SECTION_STATUS value — e.g. "Portfolio: 4/5 uploaded", "Work Links:
 * Optional". For future Section Header UI; nothing consumes this yet, but it
 * exists here (Readiness Engine), not in a component, so that UI stays a
 * pure renderer when it does.
 */
export function summarizeSections(readinessModel) {
    const bySection = new Map();
    readinessModel.forEach((item) => {
        const key = item.section || "other";
        if (!bySection.has(key)) bySection.set(key, { required: [], optional: [], hidden: [] });
        const bucket = bySection.get(key);
        if (item.requirement === REQUIREMENT_TIERS.REQUIRED) bucket.required.push(item);
        else if (item.requirement === REQUIREMENT_TIERS.HIDDEN) bucket.hidden.push(item);
        else bucket.optional.push(item);
    });

    return Array.from(bySection.entries()).map(([section, bucket]) => {
        const requiredTotal = bucket.required.length;
        const requiredCompleted = bucket.required.filter((item) => item.operational === OPERATIONAL_STATES.COMPLETED).length;
        const hasFailed = bucket.required.some((item) => item.operational === OPERATIONAL_STATES.FAILED);
        const hasInFlight = bucket.required.some((item) => IN_FLIGHT_OPERATIONAL_STATES.includes(item.operational));

        let status;
        if (requiredTotal === 0) status = SECTION_STATUS.OPTIONAL;
        else if (hasFailed) status = SECTION_STATUS.ATTENTION;
        else if (requiredCompleted === requiredTotal) status = SECTION_STATUS.COMPLETE;
        else if (hasInFlight) status = SECTION_STATUS.IN_PROGRESS;
        else status = SECTION_STATUS.INCOMPLETE;

        return {
            section,
            status,
            requiredTotal,
            requiredCompleted,
            optionalTotal: bucket.optional.length,
            hiddenTotal: bucket.hidden.length,
        };
    });
}

/**
 * Simple whole-submission progress — "N of M required items complete" plus a
 * percentage, for a future progress bar/summary. Deliberately counts only
 * REQUIRED items (the `checklist`, not the full `readinessModel`): progress
 * toward submission is about what's blocking Submit, not about optional
 * extras.
 */
export function summarizeOverallProgress(checklist) {
    const totalCount = checklist.length;
    const completedCount = checklist.filter((item) => item.operational === OPERATIONAL_STATES.COMPLETED).length;
    const percent = totalCount === 0 ? 100 : Math.round((completedCount / totalCount) * 100);
    return { completedCount, totalCount, percent };
}
