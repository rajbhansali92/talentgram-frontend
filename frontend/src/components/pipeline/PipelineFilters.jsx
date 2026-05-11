import React, { memo } from "react";
import { STATUS_FOCUS_OPTIONS, TRISTATE_OPTIONS } from "./constants";

/**
 * PipelineFilters (PATCH 4E)
 *
 * Sticky cinematic search + filter surface. Sits directly under the
 * page header and stays put while the board scrolls beneath it.
 * Glassmorphism + compact pills — same visual system as the rest of
 * the kanban.
 *
 * Layout (desktop):
 *   [search input — flex-grow] [status focus segmented] [submission?] [ig?] [clear]
 * Layout (mobile):
 *   [search input — full row]
 *   [horizontal scrolling filter pills]
 *
 * Memoised so search keystrokes don't re-render the entire board context.
 */
const PipelineFilters = memo(function PipelineFilters({
    search,
    onSearch,
    statusFocus,
    onStatusFocus,
    hasSubmission,
    onHasSubmission,
    hasIg,
    onHasIg,
    filtersActive,
    onClearAll,
    totalCount,
    filteredCount,
}) {
    const showingCount = filteredCount !== totalCount;

    return (
        <div
            data-testid="pipeline-control-bar"
            className="
                sticky top-2 z-30
                mt-3 mb-4
                rounded-xl
                bg-gradient-to-b from-black/50 to-black/30
                backdrop-blur-md
                border border-white/[0.06]
                shadow-[0_8px_32px_-12px_rgba(0,0,0,0.6),inset_0_1px_0_0_rgba(255,255,255,0.04)]
            "
        >
            <div className="flex flex-col lg:flex-row lg:items-center gap-3 lg:gap-4 px-3 py-2 lg:px-4 lg:py-2">
                {/* Search input — anchors the bar on desktop, full-width on mobile */}
                <div className="relative flex-1 min-w-0">
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => onSearch(e.target.value)}
                        placeholder="Search talents — name, email, phone, instagram…"
                        data-testid="pipeline-filter-search"
                        className="
                            w-full
                            bg-black/40 border border-white/[0.08]
                            rounded-full
                            pl-9 pr-3 py-1.5
                            text-[12.5px] text-white/90 placeholder-white/30
                            tg-mono
                            focus:outline-none focus:border-white/30 focus:bg-black/60
                            transition-colors duration-200
                        "
                    />
                    <span
                        aria-hidden
                        className="absolute left-3 top-1/2 -translate-y-1/2 text-white/35 text-[11px] tg-mono pointer-events-none"
                    >
                        ⌕
                    </span>
                    {search && (
                        <button
                            type="button"
                            onClick={() => onSearch("")}
                            aria-label="Clear search"
                            className="
                                absolute right-1.5 top-1/2 -translate-y-1/2
                                w-6 h-6 rounded-full
                                flex items-center justify-center
                                text-white/40 hover:text-white/80
                                bg-white/[0.04] hover:bg-white/[0.08]
                                transition-colors
                            "
                        >
                            ×
                        </button>
                    )}
                </div>

                {/* Filter pills — horizontally scrollable on small viewports */}
                <div className="flex items-center gap-2 overflow-x-auto tg-pipeline-scroll lg:overflow-visible -mx-1 px-1">
                    <FilterSegmented
                        label="Focus"
                        value={statusFocus}
                        onChange={onStatusFocus}
                        options={STATUS_FOCUS_OPTIONS}
                        testid="pipeline-filter-focus"
                    />
                    <FilterSegmented
                        label="Submitted"
                        value={hasSubmission}
                        onChange={onHasSubmission}
                        options={TRISTATE_OPTIONS}
                        testid="pipeline-filter-submitted"
                        compact
                    />
                    <FilterSegmented
                        label="Has IG"
                        value={hasIg}
                        onChange={onHasIg}
                        options={TRISTATE_OPTIONS}
                        testid="pipeline-filter-ig"
                        compact
                    />
                </div>

                {/* Count + clear */}
                <div className="flex items-center gap-2 shrink-0">
                    <span
                        className={`text-[10px] tg-mono ${
                            showingCount ? "text-white/70" : "text-white/35"
                        }`}
                        data-testid="pipeline-filter-count"
                    >
                        {showingCount
                            ? `${filteredCount}/${totalCount}`
                            : `${totalCount} total`}
                    </span>
                    {filtersActive && (
                        <button
                            type="button"
                            onClick={onClearAll}
                            data-testid="pipeline-filter-clear"
                            className="
                                px-2.5 py-1 rounded-full
                                text-[10px] tracking-[0.18em] uppercase
                                text-white/55 hover:text-rose-200
                                bg-white/[0.03] hover:bg-rose-300/10
                                border border-white/[0.08] hover:border-rose-300/20
                                transition-all duration-200
                            "
                            title="Clear all filters"
                        >
                            Clear
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
});

/**
 * FilterSegmented — small segmented control. Compact mode shrinks the
 * label and hides it on narrow viewports. Pure presentational.
 */
function FilterSegmented({ label, value, onChange, options, testid, compact = false }) {
    return (
        <div
            data-testid={testid}
            className="
                shrink-0 flex items-center gap-1.5
                bg-white/[0.03] border border-white/[0.06]
                rounded-full pl-2.5 pr-1 py-1
            "
        >
            <span
                className={`text-[9px] tracking-[0.18em] uppercase text-white/40 shrink-0 ${
                    compact ? "hidden xl:inline" : ""
                }`}
            >
                {label}
            </span>
            <div className="flex items-center gap-0.5">
                {options.map((opt) => {
                    const active = value === opt.value;
                    return (
                        <button
                            key={opt.value}
                            type="button"
                            onClick={() => onChange(opt.value)}
                            data-testid={`${testid}-${opt.value}`}
                            className={`
                                shrink-0
                                px-2.5 py-0.5 rounded-full
                                text-[10px] tracking-[0.1em] uppercase
                                transition-all duration-200
                                ${
                                    active
                                        ? "bg-white/90 text-black font-medium"
                                        : "text-white/55 hover:text-white/90 hover:bg-white/[0.04]"
                                }
                            `}
                        >
                            {opt.label}
                        </button>
                    );
                })}
            </div>
        </div>
    );
}

export default PipelineFilters;
