import React, { memo } from "react";

export const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-8 flex flex-col items-center justify-center gap-2 text-center">
            <p className="text-[10px] tracking-[0.14em] uppercase text-black/35">
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
                my-6
                flex flex-col items-center justify-center text-center
                py-12 px-6
                rounded-lg
                bg-[#fafafa]
                border border-black/[0.06]
            "
        >
            <div className="flex flex-col gap-2 max-w-lg">
                <p className="text-[10px] tracking-[0.14em] uppercase text-black/40">
                    No matching talents found
                </p>
                <p className="text-xs text-black/45 leading-relaxed">
                    Adjust filters or clear the current search criteria.
                </p>
            </div>
            <button
                type="button"
                onClick={onReset}
                data-testid="pipeline-filter-empty-reset"
                className="
                    mt-5 px-4 py-1.5 rounded-full
                    text-[10px] tracking-[0.14em] uppercase
                    text-black/65 hover:text-black/85
                    bg-black/[0.04] hover:bg-black/[0.07]
                    transition-all duration-200
                "
            >
                Clear filters
            </button>
        </div>
    );
});
