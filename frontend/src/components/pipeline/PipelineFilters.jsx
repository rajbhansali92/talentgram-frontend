import React, { memo } from "react";
import { STATUS_FOCUS_OPTIONS, TRISTATE_OPTIONS } from "./constants";

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
                sticky top-0 z-30 mb-5
                rounded-lg
                bg-[#111]
                border border-white/[0.06]
            "
        >
            <div className="flex flex-col lg:flex-row lg:items-center gap-2 px-3 py-2">
                {/* Search input */}
                <div className="relative flex-1 min-w-0">
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => onSearch(e.target.value)}
                        placeholder="Search by name, email, or phone..."
                        data-testid="pipeline-filter-search"
                        className="
                            w-full
                            bg-black/50 border border-white/[0.08]
                            rounded-md
                            pl-8 pr-3 py-1.5
                            text-[12px] text-white/80 placeholder-white/20
                            focus:outline-none focus:border-white/15
                            transition-colors duration-200
                        "
                    />
                    <svg
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-white/25"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    {search && (
                        <button
                            type="button"
                            onClick={() => onSearch("")}
                            aria-label="Clear search"
                            className="
                                absolute right-1.5 top-1/2 -translate-y-1/2
                                w-4 h-4 rounded-full
                                flex items-center justify-center
                                text-white/30 hover:text-white/60
                                bg-white/[0.03] hover:bg-white/[0.06]
                                transition-colors text-xs
                            "
                        >
                            ×
                        </button>
                    )}
                </div>

                {/* Filter pills */}
                <div className="flex items-center gap-1.5 overflow-x-auto lg:overflow-visible -mx-1 px-1">
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
                <div className="flex items-center gap-2 shrink-0">
                    <span
                        className={`text-[9px] font-mono ${
                            showingCount ? "text-white/50" : "text-white/20"
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
                                px-2 py-0.5 rounded
                                text-[9px] tracking-wide uppercase
                                text-white/35 hover:text-rose-300/50
                                hover:bg-rose-500/5
                                transition-all duration-200
                            "
                            title="Clear all filters"
                        >
                            Reset
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
});

function FilterSegmented({ label, value, onChange, options, testid, compact = false }) {
    return (
        <div
            data-testid={testid}
            className="
                shrink-0 flex items-center gap-1.5
                bg-white/[0.02] border border-white/[0.05]
                rounded-md pl-2 pr-0.5 py-0.5
            "
        >
            <span
                className={`text-[8px] tracking-wide uppercase text-white/25 shrink-0 ${
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
                                px-2 py-0.5 rounded
                                text-[8px] tracking-wide uppercase
                                transition-all duration-150
                                ${
                                    active
                                        ? "bg-white/80 text-black font-medium"
                                        : "text-white/45 hover:text-white/70 hover:bg-white/[0.03]"
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
