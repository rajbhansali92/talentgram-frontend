import React, { useState } from "react";
import { Check, ChevronDown, Loader2, AlertCircle, Circle, Clock, RotateCw, WifiOff, EyeOff } from "lucide-react";
import { REQUIREMENT_TIERS, OPERATIONAL_STATES } from "@/lib/readinessStatus";

// Pure presentation layer. Every item this component receives already carries
// its FINAL, fully-resolved state — this file makes no decisions about what's
// required, optional, hidden, or what the upload manager is doing. It never
// compares against a raw string; every status check goes through
// REQUIREMENT_TIERS / OPERATIONAL_STATES from lib/readinessStatus.js, the
// single canonical vocabulary for the whole submission experience. See
// SubmissionPage.jsx's `readinessModel` for where the two signals
// (validation + Upload Manager) are actually combined.
//
// Each item: { id, label, requirement: REQUIREMENT_TIERS.*,
//              operational?: OPERATIONAL_STATES.* }
// `operational` only matters when requirement === REQUIREMENT_TIERS.REQUIRED
// — optional/hidden items render as a plain badge (their upload lifecycle, if
// any, isn't submission-blocking, so it isn't worth the visual noise here).

const OPERATIONAL_META = {
    [OPERATIONAL_STATES.COMPLETED]: {
        icon: Check,
        iconWrapClass: "bg-emerald-500 text-white",
        textClass: "text-emerald-900",
        badgeText: null,
    },
    [OPERATIONAL_STATES.QUEUED]: {
        icon: Clock,
        iconWrapClass: "bg-[#333333] text-white",
        textClass: "text-[#333333]",
        badgeText: "Queued",
    },
    [OPERATIONAL_STATES.UPLOADING]: {
        icon: Loader2,
        iconWrapClass: "bg-[#0c2340] text-white",
        textClass: "text-[#0c2340]",
        badgeText: "Uploading…",
        spin: true,
    },
    [OPERATIONAL_STATES.RETRYING]: {
        icon: RotateCw,
        iconWrapClass: "bg-amber-500 text-white",
        textClass: "text-amber-700",
        badgeText: "Retrying…",
        spin: true,
    },
    [OPERATIONAL_STATES.WAITING]: {
        icon: WifiOff,
        iconWrapClass: "bg-amber-500 text-white",
        textClass: "text-amber-700",
        badgeText: "Waiting for internet…",
    },
    [OPERATIONAL_STATES.PROCESSING]: {
        icon: Loader2,
        iconWrapClass: "bg-[#0c2340]/70 text-white",
        textClass: "text-[#0c2340]",
        badgeText: "Processing…",
        spin: true,
    },
    [OPERATIONAL_STATES.FAILED]: {
        icon: AlertCircle,
        iconWrapClass: "bg-rose-500 text-white",
        textClass: "text-rose-700",
        badgeText: "Failed",
    },
    [OPERATIONAL_STATES.MISSING]: {
        icon: Circle,
        iconWrapClass: "bg-white border-2 border-[#d4d4d4] text-transparent",
        textClass: "text-[#333333]",
        badgeText: "Missing",
    },
};

const BADGE_TEXT_CLASS = {
    [OPERATIONAL_STATES.FAILED]: "text-rose-600",
    [OPERATIONAL_STATES.RETRYING]: "text-amber-600",
    [OPERATIONAL_STATES.WAITING]: "text-amber-600",
    [OPERATIONAL_STATES.MISSING]: "text-[#999]",
    [OPERATIONAL_STATES.QUEUED]: "text-[#666]",
};

function ReadinessRow({ item, onItemClick }) {
    // Optional/hidden items don't get the operational-state treatment — their
    // upload lifecycle (if any) never blocks Submit, so a single quiet badge
    // is more honest than a fabricated "Missing"/"Completed" reading.
    if (item.requirement !== REQUIREMENT_TIERS.REQUIRED) {
        return (
            <div
                data-testid={`readiness-item-${item.id}`}
                data-requirement={item.requirement}
                className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-2xl border bg-slate-50/30 border-slate-100 min-h-[44px] opacity-70"
            >
                <span className="shrink-0 w-5 h-5 rounded-full flex items-center justify-center bg-white border-2 border-[#eaeaea] text-[#999]">
                    {item.requirement === REQUIREMENT_TIERS.HIDDEN ? <EyeOff className="w-2.5 h-2.5" /> : <Circle className="w-2.5 h-2.5" />}
                </span>
                <span className="flex-1 min-w-0 text-[12px] font-medium tracking-tight truncate text-[#666]">
                    {item.label}
                </span>
                <span className="shrink-0 text-[10px] font-mono font-semibold uppercase tracking-wide text-[#999]">
                    {item.requirement === REQUIREMENT_TIERS.HIDDEN ? "Hidden" : "Optional"}
                </span>
            </div>
        );
    }

    const meta = OPERATIONAL_META[item.operational] || OPERATIONAL_META[OPERATIONAL_STATES.MISSING];
    const Icon = meta.icon;
    return (
        <button
            type="button"
            data-testid={`readiness-item-${item.id}`}
            data-requirement={item.requirement}
            data-operational={item.operational}
            onClick={() => onItemClick?.(item)}
            className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-2xl border transition-all duration-200 text-left bg-slate-50/50 border-slate-100 hover:bg-slate-100/70 active:scale-[0.99] min-h-[44px]"
        >
            <span className={`shrink-0 w-5 h-5 rounded-full flex items-center justify-center shadow-sm ${meta.iconWrapClass}`}>
                <Icon className={`w-3 h-3 stroke-[3] ${meta.spin ? "animate-spin" : ""}`} />
            </span>
            <span className={`flex-1 min-w-0 text-[12px] font-medium tracking-tight truncate ${meta.textClass}`}>
                {item.label}
            </span>
            {meta.badgeText && (
                <span className={`shrink-0 text-[10px] font-mono font-semibold uppercase tracking-wide ${BADGE_TEXT_CLASS[item.operational] || "text-[#0c2340]"}`}>
                    {meta.badgeText}
                </span>
            )}
        </button>
    );
}

function readinessSummary(items) {
    const remaining = items.filter((i) => i.requirement === REQUIREMENT_TIERS.REQUIRED && i.operational !== OPERATIONAL_STATES.COMPLETED);
    if (remaining.length === 0) return { label: "Ready to Submit", allDone: true };
    return { label: `${remaining.length} Item${remaining.length === 1 ? "" : "s"} Remaining`, allDone: false };
}

/**
 * Persistent, config-driven submission readiness checklist. Renders whatever
 * `items` it's given — it never decides what's required, optional, or hidden,
 * and never inspects the Upload Manager directly; that all happens in the
 * host page's requirement + operational-status computation (single source of
 * truth, so this component can't drift from the real validation rules or the
 * real upload state).
 */
export default function SubmissionReadinessPanel({
    title = "Submission Progress",
    items = [],
    onItemClick,
    mode = "full",
    saveStatus,
    progress,
    testId = "submission-readiness-panel",
}) {
    const [stickyExpanded, setStickyExpanded] = useState(false);
    if (items.length === 0) return null;
    const summary = readinessSummary(items);

    if (mode === "sticky") {
        return (
            <div
                data-testid="sticky-mobile-readiness"
                className="sm:hidden fixed inset-x-0 bottom-0 z-40 border-t border-[#eaeaea] bg-white/95 backdrop-blur-md shadow-[0_-2px_12px_rgba(15,23,42,0.06)]"
                style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
                role="group"
                aria-label="Submission readiness"
            >
                {progress && progress.totalCount > 0 && (
                    <div className="h-[3px] w-full bg-slate-100" data-testid="sticky-readiness-progress">
                        <div
                            className={`h-full transition-all duration-300 ${summary.allDone ? "bg-emerald-500" : "bg-[#0c2340]"}`}
                            style={{ width: `${progress.percent}%` }}
                        />
                    </div>
                )}
                <button
                    type="button"
                    onClick={() => setStickyExpanded((v) => !v)}
                    className="w-full px-4 py-2.5 flex items-center gap-3 min-h-[44px]"
                >
                    <span
                        className={`text-[11px] font-mono font-semibold tracking-tight shrink-0 ${summary.allDone ? "text-emerald-700" : "text-[#0c2340]"}`}
                        aria-live="polite"
                        data-testid="sticky-readiness-summary"
                    >
                        {summary.allDone ? "✓ Ready to Submit" : summary.label}
                    </span>
                    <ChevronDown className={`w-4 h-4 text-[#999] ml-auto shrink-0 transition-transform duration-200 ${stickyExpanded ? "rotate-180" : ""}`} />
                </button>
                {stickyExpanded && (
                    <div className="px-3 pb-3 space-y-1.5 max-h-[45vh] overflow-y-auto">
                        {items.map((item) => (
                            <ReadinessRow
                                key={item.id}
                                item={item}
                                onItemClick={(it) => {
                                    setStickyExpanded(false);
                                    onItemClick?.(it);
                                }}
                            />
                        ))}
                    </div>
                )}
            </div>
        );
    }

    return (
        <section
            className="mb-10 bg-white rounded-3xl p-6 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.03)]"
            data-testid={testId}
        >
            <div className="flex items-center justify-between mb-3 gap-3">
                <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-black font-semibold">{title}</p>
                <p
                    className={`uppercase tracking-[0.18em] text-[10px] font-mono font-semibold shrink-0 ${summary.allDone ? "text-emerald-600" : "text-[#333333]"}`}
                    data-testid="submission-readiness-summary"
                    aria-live="polite"
                >
                    {summary.label}
                </p>
            </div>
            {progress && progress.totalCount > 0 && (
                <div className="mb-4" data-testid="submission-readiness-progress">
                    <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                        <div
                            className={`h-full rounded-full transition-all duration-300 ${summary.allDone ? "bg-emerald-500" : "bg-[#0c2340]"}`}
                            style={{ width: `${progress.percent}%` }}
                        />
                    </div>
                    <p className="mt-1.5 text-[10px] font-mono text-[#999] tracking-wide">
                        {progress.completedCount} of {progress.totalCount} required items complete ({progress.percent}%)
                    </p>
                </div>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5" role="list">
                {items.map((item) => (
                    <ReadinessRow key={item.id} item={item} onItemClick={onItemClick} />
                ))}
            </div>
            {saveStatus !== undefined && (
                <div className="mt-4 pt-3 border-t border-[#eaeaea]/60 flex items-start justify-between gap-3">
                    <p className="text-[11px] leading-relaxed text-[#333333] font-mono">
                        Your progress is automatically saved.<br className="hidden sm:block" />
                        {" "}You can continue later using the same email.
                    </p>
                    <span
                        data-testid="autosave-indicator"
                        aria-live="polite"
                        className="shrink-0 inline-flex items-center gap-1.5 text-[11px] font-mono font-semibold tracking-tight"
                    >
                        {saveStatus === "saving" ? (
                            <>
                                <span className="w-1.5 h-1.5 rounded-full bg-[#0c2340]/50 animate-pulse" />
                                <span className="text-[#333333]">Saving…</span>
                            </>
                        ) : saveStatus === "saved" ? (
                            <>
                                <Check className="w-3 h-3 stroke-[3] text-emerald-500" />
                                <span className="text-emerald-700">Saved</span>
                            </>
                        ) : null}
                    </span>
                </div>
            )}
        </section>
    );
}
