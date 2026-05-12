import React, { memo } from "react";

export const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-10 flex flex-col items-center justify-center gap-3 text-center">
            <div className="w-8 h-px bg-gradient-to-r from-transparent via-white/[0.12] to-transparent" />
            <p className="text-[10px] tracking-[0.18em] uppercase text-white/22 italic">
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
                rounded-2xl
                bg-gradient-to-b from-white/[0.02] to-transparent
                backdrop-blur-sm
                shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]
                border border-white/[0.04]
            "
        >
            <div className="flex flex-col gap-2 max-w-lg">
                <p className="text-[10px] tracking-[0.18em] uppercase text-white/25">
                    No talents match
                </p>
                <p className="text-xs text-white/35 leading-relaxed">
                    Try widening the search, switching focus, or clearing active filters.
                </p>
            </div>
            <button
                type="button"
                onClick={onReset}
                data-testid="pipeline-filter-empty-reset"
                className="
                    mt-6 px-4 py-1.5 rounded-full
                    text-[10px] tracking-[0.18em] uppercase
                    text-white/80 hover:text-white
                    bg-white/[0.06] hover:bg-white/[0.1]
                    transition-all duration-200
                "
            >
                Clear filters
            </button>
        </div>
    );
});
