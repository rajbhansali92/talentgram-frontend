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

// Bulk-action toolbar exposes only the funnel destinations
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
    follow_up: "FOLLOW-UP",
};

// Refined stage accents — very subtle, almost invisible
export const STAGE_ACCENTS = {
    ask_to_test: "from-slate-400/20 to-transparent",
    approved: "from-emerald-400/20 to-transparent",
    hold: "from-amber-400/20 to-transparent",
    shortlisted: "from-violet-400/20 to-transparent",
    already_tested: "from-fuchsia-400/15 to-transparent",
    locked: "from-amber-300/15 to-transparent",
    rejected: "from-rose-400/12 to-transparent",
    not_available: "from-zinc-500/10 to-transparent",
    not_interested: "from-zinc-500/10 to-transparent",
    pitch: "from-teal-400/20 to-transparent",
    follow_up: "from-amber-400/15 to-transparent",
};
export const DEFAULT_ACCENT = "from-white/5 to-transparent";

// Minimal empty-state copy
export const EMPTY_STATE_COPY = {
    ask_to_test: "No pending",
    approved: "Awaiting approval",
    hold: "Paused",
    shortlisted: "Curated",
    already_tested: "Completed",
    locked: "Finalised",
    rejected: "Archived",
    not_available: "Available",
    not_interested: "Open",
    pitch: "No active",
    follow_up: "All caught up",
};

export const EMPTY_BULK_SET = new Set();
export const NOOP = () => {};

/* ---------------------------------------------------------------------
 * Filter primitives
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
 * Status tones — refined, minimal
 * ------------------------------------------------------------------- */
export const STATUS_TONES = {
    locked: {
        label: "Final",
        dot: "bg-amber-300/40",
        text: "text-amber-300/50",
        chip: "border-amber-400/8 bg-amber-400/[0.01]",
    },
    approved: {
        label: "Approved",
        dot: "bg-emerald-400/40",
        text: "text-emerald-400/50",
        chip: "border-emerald-400/8 bg-emerald-400/[0.01]",
    },
    hold: {
        label: "Hold",
        dot: "bg-amber-400/35",
        text: "text-amber-300/45",
        chip: "border-amber-400/8 bg-amber-400/[0.01]",
    },
    rejected: {
        label: "Rejected",
        dot: "bg-rose-400/30",
        text: "text-rose-400/40",
        chip: "border-rose-400/6 bg-rose-400/[0.005]",
    },
    not_available: {
        label: "Unavailable",
        dot: "bg-zinc-500/30",
        text: "text-zinc-400/40",
        chip: "border-zinc-500/6 bg-zinc-500/[0.005]",
    },
    not_interested: {
        label: "Declined",
        dot: "bg-zinc-500/30",
        text: "text-zinc-400/40",
        chip: "border-zinc-500/6 bg-zinc-500/[0.005]",
    },
};

// Only show 2 primary actions per card — others require click
export const VISIBLE_ACTIONS_PER_CARD = 2;

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
