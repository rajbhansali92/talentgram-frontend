import React, { memo } from "react";
import { STATUS_FOCUS_OPTIONS, TRISTATE_OPTIONS } from "./constants";

/**
 * PipelineFilters — visual filter toolbar component.
 * 
 * This is the UI component only. All filtering logic lives in:
 * frontend/src/hooks/usePipelineFilters.js
 * 
 * Features:
 *   • Sticky search input with clear button
 *   • Status focus segmented control
 *   • Tristate filters for Portfolio and Instagram
 *   • Filter count display
 *   • Reset button for clearing all filters
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

    const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
            onSearch('');
        }
    };

    return (
        <div
            data-testid="pipeline-control-bar"
            className="
                sticky top-2 z-30 mb-6
                rounded-xl
                bg-gradient-to-br from-[#141415] via-[#111112] to-[#0a0a0b]
                border border-white/[0.04]
                backdrop-blur-md
                overflow-visible
            "
        >
            <div className="flex flex-col xl:flex-row flex-wrap xl:flex-nowrap xl:items-center gap-3 px-4 py-3">
                {/* Search input */}
                <div className="relative flex-1 min-w-[180px] xl:min-w-0">
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => onSearch(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder="Search by name, email, phone, or ID..."
                        data-testid="pipeline-filter-search"
                        className="
                            w-full
                            bg-black/40 border border-white/[0.06]
                            rounded-lg
                            pl-9 pr-9 py-1.5
                            text-[13px] text-white/85 placeholder-white/25
                            focus-visible:outline-none focus-visible:border-white/15 focus-visible:ring-1 focus-visible:ring-white/8
                            transition-all duration-200
                            hover:border-white/10
                        "
                    />
                    <svg
                        className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/30 pointer-events-none transition-colors duration-200"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        aria-hidden="true"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    
                    {/* Clear button */}
                    {search && (
                        <button
                            type="button"
                            onClick={() => onSearch("")}
                            aria-label="Clear search"
                            className="
                                absolute right-2 top-1/2 -translate-y-1/2
                                min-w-[22px] min-h-[22px] w-[22px] h-[22px] rounded
                                flex items-center justify-center
                                text-white/40 hover:text-white/70
                                bg-white/[0.02] hover:bg-white/[0.06]
                                transition-all duration-200
                                text-base font-medium
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/15
                            "
                        >
                            ×
                        </button>
                    )}
                </div>

                {/* Filter pills */}
                <div className="flex items-center gap-2 overflow-x-auto overflow-y-visible xl:overflow-visible px-0.5 pb-1 xl:pb-0 scrollbar-none">
                    <FilterSegmented
                        label="Status"
                        value={statusFocus}
                        onChange={onStatusFocus}
                        options={STATUS_FOCUS_OPTIONS}
                        testid="pipeline-filter-focus"
                    />
                    <FilterSegmented
                        label="Portfolio"
                        value={hasSubmission}
                        onChange={onHasSubmission}
                        options={TRISTATE_OPTIONS}
                        testid="pipeline-filter-submitted"
                        compact
                    />
                    <FilterSegmented
                        label="IG"
                        value={hasIg}
                        onChange={onHasIg}
                        options={TRISTATE_OPTIONS}
                        testid="pipeline-filter-ig"
                        compact
                    />
                </div>

                {/* Count + clear */}
                <div className="flex items-center gap-3 shrink-0">
                    <span
                        className={`text-[10px] font-mono tracking-wide ${
                            showingCount ? "text-white/55" : "text-white/25"
                        }`}
                        data-testid="pipeline-filter-count"
                    >
                        {showingCount
                            ? `${filteredCount.toLocaleString()}/${totalCount.toLocaleString()}`
                            : `${totalCount.toLocaleString()} total`}
                    </span>
                    {filtersActive && (
                        <button
                            type="button"
                            onClick={onClearAll}
                            data-testid="pipeline-filter-clear"
                            className="
                                px-2.5 py-0.5 rounded
                                text-[9px] tracking-wide uppercase font-medium
                                text-white/40 hover:text-rose-300/60
                                hover:bg-rose-500/8
                                transition-all duration-200
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-rose-500/25
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
 * FilterSegmented — reusable segmented control component
 */
function FilterSegmented({ label, value, onChange, options, testid, compact = false }) {
    return (
        <div
            data-testid={testid}
            className="
                shrink-0 flex items-center gap-2
                bg-white/[0.015] border border-white/[0.04]
                rounded-md pl-2.5 pr-1 py-0.5
                hover:border-white/[0.07] transition-colors duration-200
            "
        >
            <span
                className={`text-[9px] font-medium tracking-wide uppercase text-white/25 shrink-0 ${
                    compact ? "hidden xl:inline" : ""
                }`}
            >
                {label}
            </span>
            <div className="flex items-center gap-1">
                {options.map((opt) => {
                    const active = value === opt.value;
                    return (
                        <button
                            key={opt.value}
                            type="button"
                            onClick={() => onChange(opt.value)}
                            data-testid={`${testid}-${opt.value}`}
                            aria-pressed={active}
                            className={`
                                shrink-0
                                px-2.5 py-0.5 rounded-[5px]
                                text-[9px] font-medium tracking-wide uppercase
                                transition-all duration-150
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/15
                                ${
                                    active
                                        ? "bg-white text-black shadow-[0_1px_2px_rgba(0,0,0,0.1)] ring-1 ring-white/15"
                                        : "text-white/45 hover:text-white/75 hover:bg-white/[0.03]"
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
