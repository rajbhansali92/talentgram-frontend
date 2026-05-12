import React, { memo } from "react";

export const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-6 flex flex-col items-center justify-center gap-2 text-center">
            <p className="text-[9px] tracking-wide uppercase text-white/15">
                {label}
            </p>
        </div>
    );
});

export const FilterEmptyState = memo(function FilterEmptyState({ onReset }) {
    return (
        <div
            data-testid="pipeline-filter-empty"
            className="
                my-8
                flex flex-col items-center justify-center text-center
                py-16 px-6
                rounded-lg
                bg-white/[0.01] border border-white/[0.04]
            "
        >
            <p className="text-[9px] tracking-wide uppercase text-white/25 mb-2">
                No results
            </p>
            <p className="text-xs text-white/35 max-w-md">
                Adjust filters or search query
            </p>
            <button
                type="button"
                onClick={onReset}
                data-testid="pipeline-filter-empty-reset"
                className="
                    mt-5 px-4 py-1.5 rounded-md
                    text-[9px] tracking-wide uppercase
                    text-white bg-white/8 hover:bg-white/12
                    transition-colors duration-200
                "
            >
                Clear filters
            </button>
        </div>
    );
});
