import { useCallback, useEffect, useRef, useState } from "react";
import { summarizeUploads, mapUploadManagerStatus, OPERATIONAL_STATES } from "@/lib/readinessStatus";

// THE UPLOAD ACTIVITY MODEL — the single presentation model for the Upload
// Activity Panel (components/shared/FloatingUploadManager.jsx), mirroring
// useSubmissionExperienceModel's role for the submission page: engine
// output + a small amount of local UI state go in, one fully-derived object
// comes out. FloatingUploadManager should be a PURE renderer of whatever
// this returns — it must never independently recompute a count, a label, an
// icon choice, or an expand/collapse decision.
//
// Every count here is sourced from `summarizeUploads()` (the Operational
// Engine, lib/readinessStatus.js) — nothing re-derives counts from raw
// `activeUploads` statuses. `completedCount` is the one value that can't
// come from `summarizeUploads` alone: the upload engine prunes a completed
// `activeUploads` entry 3s after success (unchanged, see
// UploadManagerContext.jsx), so a session-lifetime tally has to be tracked
// there and passed in — this hook only consumes it, it doesn't compute it.
//
// This hook changes NO upload/retry/transport behavior — it's purely a
// reshaping of already-existing engine output plus a small, self-contained
// bit of "when should this panel be open" UI state.

const SUCCESS_COLLAPSE_DELAY_MS = 2500;

const FRIENDLY_STATE_LABEL = {
    [OPERATIONAL_STATES.QUEUED]: "Queued",
    [OPERATIONAL_STATES.UPLOADING]: "Uploading…",
    [OPERATIONAL_STATES.RETRYING]: "Retrying…",
    [OPERATIONAL_STATES.WAITING]: "Waiting for connection…",
    [OPERATIONAL_STATES.PROCESSING]: "Processing…",
    [OPERATIONAL_STATES.COMPLETED]: "Completed",
    [OPERATIONAL_STATES.FAILED]: "Failed",
};

const STATE_TEXT_CLASS = {
    [OPERATIONAL_STATES.FAILED]: "text-rose-500",
    [OPERATIONAL_STATES.COMPLETED]: "text-emerald-600",
};

function cleanItemLabel(u) {
    if (u.category === "intro_video") return "Intro Video";
    if (u.category === "take") return u.label;
    const categoryLabel = u.category === "image" ? "Portfolio" : u.category === "indian" ? "Indian" : "Western";
    return `${categoryLabel}: ${u.fileName}`;
}

// One `activeUploads` entry -> everything its card needs to render, fully
// resolved. Prefers the engine's own rich, already-friendly live text (e.g.
// "Optimizing video… (10s remaining)") when it has one; otherwise falls back
// to the canonical wording map — never a raw engine status word.
function toItemViewModel([key, upload]) {
    const canonicalState = mapUploadManagerStatus(upload.status) || OPERATIONAL_STATES.UPLOADING;
    return {
        key,
        label: cleanItemLabel(upload),
        displayText: upload.statusText || FRIENDLY_STATE_LABEL[canonicalState],
        textClass: STATE_TEXT_CLASS[canonicalState] || "text-[#0c2340]",
        pct: upload.pct || 0,
        status: upload.status,
        error: upload.error,
    };
}

export function useUploadActivityModel({ activeUploads = {}, completedCount = 0 }) {
    const entries = Object.entries(activeUploads);

    // Reuse the Operational Engine's own aggregation — counts are never
    // re-derived from raw statuses anywhere in this hook.
    const uploadCounts = summarizeUploads(activeUploads);
    const uploadingCount = uploadCounts.inFlightTotal;
    const failedCount = uploadCounts.failedTotal;
    const hasActive = uploadingCount > 0;
    const hasFailed = failedCount > 0;

    const [expanded, setExpanded] = useState(false);
    const [justFinished, setJustFinished] = useState(false);
    const prevRef = useRef({ hasActive: false, hasFailed: false });

    // Stable "of N" denominator for the current batch. `entries.length`
    // alone would shrink mid-batch as completed entries get pruned (3s
    // after success), which would misread as the batch itself shrinking.
    // Tracks the high-water mark while active, resets once idle.
    const batchTotalRef = useRef(0);
    if (hasActive) {
        batchTotalRef.current = Math.max(batchTotalRef.current, entries.length);
    } else if (batchTotalRef.current !== 0) {
        batchTotalRef.current = 0;
    }
    const batchTotal = batchTotalRef.current;

    // Auto-expand / auto-collapse: expand immediately on a new failure or a
    // newly-started upload; collapse once everything is idle, briefly
    // showing a success summary first when the batch finished clean.
    useEffect(() => {
        const prev = prevRef.current;
        const startedNewUpload = hasActive && !prev.hasActive;
        const justFailed = hasFailed && !prev.hasFailed;

        if (startedNewUpload || justFailed) {
            setExpanded(true);
            setJustFinished(false);
        } else if (!hasActive && !hasFailed && prev.hasActive) {
            setJustFinished(true);
            setExpanded(true);
            prevRef.current = { hasActive, hasFailed };
            const t = setTimeout(() => {
                setJustFinished(false);
                setExpanded(false);
            }, SUCCESS_COLLAPSE_DELAY_MS);
            return () => clearTimeout(t);
        } else if (!hasActive && !hasFailed) {
            setExpanded(false);
        }

        prevRef.current = { hasActive, hasFailed };
    }, [hasActive, hasFailed]);

    const toggleExpanded = useCallback(() => setExpanded((v) => !v), []);

    // Nothing has happened this session yet — the panel shouldn't render at
    // all (Google Photos/Dropbox/iCloud Photos: no idle widget before the
    // first upload starts).
    const isVisible = entries.length > 0 || completedCount > 0;

    const overallProgress = entries.length === 0
        ? 100
        : Math.round(entries.reduce((sum, [, u]) => sum + (u.status === "completed" ? 100 : (u.pct || 0)), 0) / entries.length);

    const headline = hasActive
        ? `Uploading ${uploadingCount} of ${batchTotal}`
        : justFinished
            ? "Uploads Complete"
            : "Uploads";

    // The pure "does the panel need to be open right now" intent, decoupled
    // from `expanded` (the actual, user-toggleable render state the effect
    // above keeps in sync with this). Exposed directly so callers/tests
    // never have to re-derive it from hasActive/hasFailed/justFinished.
    const shouldExpand = hasActive || hasFailed || justFinished;
    const shouldCollapse = !shouldExpand;

    return {
        isVisible,
        items: entries.map(toItemViewModel),
        summary: { completedCount, uploadingCount, failedCount },
        completedCount,
        uploadingCount,
        failedCount,
        hasActive,
        hasFailed,
        overallProgress,
        headline,
        expanded,
        justFinished,
        shouldExpand,
        shouldCollapse,
        toggleExpanded,
    };
}
