import React, { memo } from "react";
import { STATUS_FOCUS_OPTIONS, TRISTATE_OPTIONS } from "./constants";

const PORTFOLIO_OPTIONS = [
    { value: "any", label: "All" },
    { value: "yes", label: "Attached" },
    { value: "no", label: "Missing" },
];

const IG_OPTIONS = [
    { value: "any", label: "All" },
    { value: "yes", label: "Connected" },
    { value: "no", label: "Unlinked" },
];

/**
 * PipelineFilters — visual filter toolbar component.
 * 
 * This is the UI component only. All filtering logic lives in:
 * frontend/src/hooks/usePipelineFilters.js
 * 
 * Features:
 *   • Sticky search input with clear button
 *   • Status focus segmented control (workflow state row)
 *   • Tristate filters for Portfolio and Instagram (data presence row)
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
            <div className="px-4 pt-3 pb-2.5 space-y-2.5">

                {/* Row 1: Search + Count/Clear inline */}
                <div className="flex items-center gap-3">
                    {/* Search input */}
                    <div className="relative flex-1">
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
                            className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-black/30 pointer-events-none"
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

                    {/* Count + Clear — always right of search */}
                    <div className="flex items-center gap-2.5 shrink-0">
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

                {/* Separator */}
                <div className="w-full h-px bg-black/[0.05]" aria-hidden="true" />

                {/* Row 2: Status filter group — workflow state */}
                <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">
                        Status
                    </span>
                    <div
                        data-testid="pipeline-filter-focus"
                        className="flex items-center gap-1 flex-wrap"
                    >
                        {STATUS_FOCUS_OPTIONS.map((opt) => {
                            const active = statusFocus === opt.value;
                            return (
                                <button
                                    key={opt.value}
                                    type="button"
                                    onClick={() => onStatusFocus(opt.value)}
                                    data-testid={`pipeline-filter-focus-${opt.value}`}
                                    aria-pressed={active}
                                    className={`
                                        shrink-0
                                        px-2.5 py-1 rounded-[5px]
                                        text-[10px] font-medium tracking-wide uppercase
                                        transition-all duration-150
                                        focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
                                        ${
                                            active
                                                ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]"
                                                : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"
                                        }
                                    `}
                                >
                                    {opt.label}
                                </button>
                            );
                        })}
                    </div>
                </div>

                {/* Row 3: Portfolio + Instagram — data presence filters */}
                <div className="flex items-center gap-4 flex-wrap">
                    {/* Portfolio Link */}
                    <div className="flex items-center gap-2">
                        <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">
                            Portfolio
                        </span>
                        <div
                            data-testid="pipeline-filter-submitted"
                            className="flex items-center gap-1"
                        >
                            {PORTFOLIO_OPTIONS.map((opt) => {
                                const active = hasSubmission === opt.value;
                                return (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => onHasSubmission(opt.value)}
                                        data-testid={`pipeline-filter-submitted-${opt.value}`}
                                        aria-pressed={active}
                                        className={`
                                            shrink-0
                                            px-2.5 py-1 rounded-[5px]
                                            text-[10px] font-medium tracking-wide uppercase
                                            transition-all duration-150
                                            focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
                                            ${
                                                active
                                                    ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]"
                                                    : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"
                                            }
                                        `}
                                    >
                                        {opt.label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Thin vertical separator between Portfolio and Instagram on wider viewports */}
                    <span className="hidden sm:block w-px h-3.5 bg-black/[0.08] shrink-0" aria-hidden="true" />

                    {/* Instagram */}
                    <div className="flex items-center gap-2">
                        <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">
                            Instagram
                        </span>
                        <div
                            data-testid="pipeline-filter-ig"
                            className="flex items-center gap-1"
                        >
                            {IG_OPTIONS.map((opt) => {
                                const active = hasIg === opt.value;
                                return (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => onHasIg(opt.value)}
                                        data-testid={`pipeline-filter-ig-${opt.value}`}
                                        aria-pressed={active}
                                        className={`
                                            shrink-0
                                            px-2.5 py-1 rounded-[5px]
                                            text-[10px] font-medium tracking-wide uppercase
                                            transition-all duration-150
                                            focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
                                            ${
                                                active
                                                    ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]"
                                                    : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"
                                            }
                                        `}
                                    >
                                        {opt.label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                </div>

            </div>
        </div>
    );
});

export default PipelineFilters;
