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

/* ---------------------------------------------------------------------
 * ISSUE 7 FIX: Clearer empty state copy
 * Replaced abstract text with more descriptive, actionable copy
 * ------------------------------------------------------------------- */
export const EMPTY_STATE_COPY = {
    // Main flow stages — clear action-oriented copy
    ask_to_test: "No pending invitations",
    approved: "Awaiting approvals",
    hold: "On hold",  // Fixed: was "Paused" - clearer
    shortlisted: "No shortlisted talents",
    already_tested: "No completed tests",
    locked: "No finalised placements",
    
    // Outcome stages — clear terminal states
    rejected: "No rejected talents",
    not_available: "All talents available",
    not_interested: "Open to opportunities",  // Fixed: was "Open" - clearer
    
    // Independent stages
    pitch: "No active pitches",  // Fixed: was "No active" - more descriptive
    
    // Follow-up
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

// Only show 2 primary actions per card — others in overflow menu
export const VISIBLE_ACTIONS_PER_CARD = 2;

/* ---------------------------------------------------------------------
 * Next stage flow definitions
 * Controls which transition buttons appear on each card
 * ------------------------------------------------------------------- */
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

/* ---------------------------------------------------------------------
 * Helper: Get human-readable stage label
 * ------------------------------------------------------------------- */
export const getStageLabel = (stage) =>
    STAGE_LABELS[stage] || stage.replaceAll("_", " ").toUpperCase();

/* ---------------------------------------------------------------------
 * Additional helpers for better type safety and DX
 * ------------------------------------------------------------------- */

/**
 * Check if a stage is a terminal state (outcome)
 */
export const isTerminalStage = (stage) => {
    const normalised = normaliseStage(stage);
    return OUTCOME_STAGES.includes(normalised);
};

/**
 * Check if a stage is in the main flow
 */
export const isMainFlowStage = (stage) => {
    const normalised = normaliseStage(stage);
    return MAIN_FLOW_STAGES.includes(normalised);
};

/**
 * Get all available stages for a given current stage
 */
export const getAvailableTransitions = (currentStage) => {
    const normalised = normaliseStage(currentStage);
    return NEXT_STAGE_FLOW[normalised] || [];
};

/**
 * Get stage color for UI indicators (simplified mapping)
 */
export const getStageColor = (stage) => {
    const normalised = normaliseStage(stage);
    const colorMap = {
        ask_to_test: "slate",
        approved: "emerald",
        hold: "amber",
        shortlisted: "violet",
        already_tested: "fuchsia",
        locked: "amber",
        rejected: "rose",
        not_available: "zinc",
        not_interested: "zinc",
        pitch: "teal",
        follow_up: "amber",
    };
    return colorMap[normalised] || "white";
};

/**
 * Check if a stage can accept drag-drop operations
 */
export const isDroppableStage = (stage) => {
    const nonDroppable = [...OUTCOME_STAGES, "locked", "pitch"];
    return !nonDroppable.includes(normaliseStage(stage));
};

/**
 * Get placeholder text for empty state based on context
 */
export const getEmptyStateMessage = (stage, hasFilters = false) => {
    if (hasFilters) {
        return "No matching talents";
    }
    return EMPTY_STATE_COPY[stage] || "Empty";
};
