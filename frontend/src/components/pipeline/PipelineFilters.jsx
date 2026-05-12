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
                sticky top-3 z-30 mb-6
                rounded-lg
                bg-white
                border border-black/[0.08]
                shadow-[0_4px_18px_-14px_rgba(0,0,0,0.08)]
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
                            bg-[#f5f5f5] border border-black/[0.08]
                            rounded-lg
                            pl-9 pr-9 py-1.5
                            text-[13px] text-black/85 placeholder:text-black/35
                            focus-visible:outline-none focus-visible:border-black/[0.15] focus-visible:ring-1 focus-visible:ring-black/8
                            transition-all duration-200
                            hover:border-black/[0.12]
                        "
                    />
                    <svg
                        className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-black/30 pointer-events-none transition-colors duration-200"
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
                                text-black/45 hover:text-black/75
                                bg-black/[0.03] hover:bg-black/[0.06]
                                transition-all duration-200
                                text-base font-medium
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
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
                            showingCount ? "text-black/55" : "text-black/35"
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
                                text-[10px] tracking-wide uppercase font-medium
                                text-black/45 hover:text-black/75
                                hover:bg-black/[0.04]
                                transition-all duration-200
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
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
                bg-[#fafafa] border border-black/[0.06]
                rounded-md pl-2.5 pr-1 py-0.5
                hover:border-black/[0.10] transition-colors duration-200
            "
        >
            <span
                className={`text-[10px] font-medium tracking-wide uppercase text-black/40 shrink-0 ${
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
                                text-[10px] font-medium tracking-wide uppercase
                                transition-all duration-150
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
                                ${
                                    active
                                        ? "bg-black text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]"
                                        : "text-black/55 hover:text-black/85 hover:bg-black/[0.03]"
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
