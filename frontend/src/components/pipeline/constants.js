/* ---------------------------------------------------------------------
 * Pipeline constants — single source of truth for the kanban.
 *
 * MAIN_FLOW_STAGES     : the progression funnel (rendered first)
 * OUTCOME_STAGES       : terminal states (rendered after the funnel)
 * INDEPENDENT_STAGES   : `pitch` lives in its own section — not part of
 *                        the progression flow, sourcing-only
 * PIPELINE_STAGE_ORDER : flat array used by the kanban grid renderer
 *
 * Legacy `sent` is folded into `approved` at read-time so any stored
 * row with stage="sent" renders into the Approved column without a
 * data migration.
 * ------------------------------------------------------------------- */

export const MAIN_FLOW_STAGES = [
    "ask_to_test",
    "approved",
    "hold",
    "shortlisted",
    "already_tested",
    "locked",
];

export const OUTCOME_STAGES = ["rejected", "not_available", "not_interested"];

export const INDEPENDENT_STAGES = ["pitch"];

export const PIPELINE_STAGE_ORDER = [
    ...MAIN_FLOW_STAGES,
    ...OUTCOME_STAGES,
    ...INDEPENDENT_STAGES,
];

// Bulk-action toolbar exposes only the funnel destinations (no
// terminal/independent stages — those are still reachable via per-card
// buttons but rarely needed in bulk).
export const BULK_MOVE_TARGETS = ["ask_to_test", "approved", "shortlisted", "locked"];

export const LEGACY_STAGE_ALIASES = {
    sent: "approved",
};

export const normaliseStage = (raw) => {
    if (!raw) return raw;
    const s = String(raw).trim().toLowerCase();
    return LEGACY_STAGE_ALIASES[s] || s;
};

export const STAGE_LABELS = {
    ask_to_test: "ASK TO TEST",
    approved: "APPROVED",
    hold: "HOLD",
    shortlisted: "SHORTLISTED",
    already_tested: "ALREADY TESTED",
    locked: "LOCKED",
    rejected: "REJECTED",
    not_available: "NOT AVAILABLE",
    not_interested: "NOT INTERESTED",
    pitch: "PITCH",
    // Virtual read-only lane (PATCH 3C). Never stored in DB.
    follow_up: "FOLLOW-UP",
};

// Per-stage accent colours. Used as a thin top bar on the column header.
// Enhanced: Opacity tuned down for a more luxury, cinematic indicator.
export const STAGE_ACCENTS = {
    ask_to_test: "from-sky-400/40 to-sky-500/0",
    approved: "from-emerald-400/40 to-emerald-500/0",
    hold: "from-amber-400/40 to-amber-500/0",
    shortlisted: "from-violet-400/40 to-violet-500/0",
    already_tested: "from-fuchsia-400/40 to-fuchsia-500/0",
    locked: "from-yellow-300/40 to-yellow-500/0",
    rejected: "from-rose-400/25 to-rose-500/0",
    not_available: "from-zinc-400/20 to-zinc-500/0",
    not_interested: "from-zinc-400/20 to-zinc-500/0",
    pitch: "from-teal-400/40 to-teal-500/0",
    follow_up: "from-amber-400/50 to-amber-500/0",
};
export const DEFAULT_ACCENT = "from-white/10 to-white/0";

// Cinematic empty-state copy keyed by stage. Falls back to a generic line.
export const EMPTY_STATE_COPY = {
    ask_to_test: "Awaiting first invitations",
    approved: "No approvals yet",
    hold: "Nothing on hold",
    shortlisted: "Empty shortlist",
    already_tested: "No prior tests",
    locked: "Not finalised yet",
    rejected: "Cleanly clear",
    not_available: "Everyone's available",
    not_interested: "All in",
    pitch: "No pitches in flight",
    follow_up: "All caught up",
};

// Stable references used by the read-only follow-up lane.
export const EMPTY_BULK_SET = new Set();
export const NOOP = () => {};

/* ---------------------------------------------------------------------
 * Filter primitives (PATCH 4E)
 * ------------------------------------------------------------------- */
export const STATUS_FOCUS_OPTIONS = [
    { value: "all", label: "All" },
    { value: "follow_up", label: "Follow-up only" },
    { value: "pending", label: "Pending" },
    { value: "approved", label: "Approved" },
    { value: "shortlisted", label: "Shortlisted" },
    { value: "locked", label: "Locked" },
    { value: "rejected", label: "Rejected" },
];

export const TRISTATE_OPTIONS = [
    { value: "any", label: "Any" },
    { value: "yes", label: "Yes" },
    { value: "no", label: "No" },
];

export const DEFAULT_FILTERS = {
    search: "",
    statusFocus: "all",
    hasSubmission: "any",
    hasIg: "any",
};

/* ---------------------------------------------------------------------
 * Status tones (PATCH 4B)
 * Used by the Card footer for terminal/locked states. All tones stay
 * muted on purpose — luxury, not dashboard. 
 * ------------------------------------------------------------------- */
export const STATUS_TONES = {
    locked: {
        label: "Finalised",
        dot: "bg-yellow-200/60",
        text: "text-yellow-200/70",
        chip: "border-yellow-200/12 bg-yellow-200/[0.03]",
    },
    approved: {
        label: "Approved",
        dot: "bg-emerald-300/60",
        text: "text-emerald-300/70",
        chip: "border-emerald-300/12 bg-emerald-300/[0.03]",
    },
    hold: {
        label: "On hold",
        dot: "bg-amber-300/60",
        text: "text-amber-200/70",
        chip: "border-amber-300/12 bg-amber-300/[0.03]",
    },
    rejected: {
        label: "Rejected",
        dot: "bg-rose-300/50",
        text: "text-rose-300/60",
        chip: "border-rose-300/12 bg-rose-300/[0.03]",
    },
    not_available: {
        label: "Not available",
        dot: "bg-zinc-300/40",
        text: "text-zinc-300/55",
        chip: "border-zinc-300/12 bg-zinc-300/[0.03]",
    },
    not_interested: {
        label: "Not interested",
        dot: "bg-zinc-300/40",
        text: "text-zinc-300/55",
        chip: "border-zinc-300/12 bg-zinc-300/[0.03]",
    },
};

// Sugessted next-steps for card action buttons.
export const NEXT_STAGE_FLOW = {
    ask_to_test: ["approved", "not_interested", "not_available"],
    approved: ["shortlisted", "hold", "rejected"],
    hold: ["approved", "shortlisted", "rejected"],
    shortlisted: ["locked", "already_tested", "rejected"],
    already_tested: ["shortlisted", "locked", "rejected"],
    locked: [],
    rejected: [],
    not_available: [],
    not_interested: [],
    pitch: ["ask_to_test", "rejected"],
};

export const getStageLabel = (stage) =>
    STAGE_LABELS[stage] || stage.replaceAll("_", " ").toUpperCase();
