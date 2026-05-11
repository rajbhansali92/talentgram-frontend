import React, { memo } from "react";

/**
 * PipelineEmptyState — two flavours:
 *   EmptyLane: column-level placeholder (no rows in this stage)
 *   FilterEmptyState: board-level placeholder when active filters resolve
 *                     to zero matches.
 *
 * Both are intentionally quiet — no big icons, no bright CTAs, no noise.
 */

export const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-10 flex flex-col items-center justify-center gap-2 text-center">
            <div className="w-8 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent" />
            <p className="text-[11px] tracking-wide text-white/30 italic">
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
                mt-12 mb-8
                flex flex-col items-center justify-center text-center
                py-20 px-6
                rounded-2xl
                bg-gradient-to-b from-white/[0.02] to-transparent
                border border-white/[0.05]
                backdrop-blur-md
            "
        >
            <div className="w-12 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent mb-4" />
            <p className="text-[11px] tracking-[0.22em] uppercase text-white/40 mb-2">
                No talents match
            </p>
            <p className="text-sm text-white/55 max-w-md leading-relaxed">
                Try widening the search, switching focus to{" "}
                <span className="text-white/80">All</span>, or clearing the active filters.
            </p>
            <button
                type="button"
                onClick={onReset}
                data-testid="pipeline-filter-empty-reset"
                className="
                    mt-6 px-4 py-2 rounded-full
                    text-[10px] tracking-[0.18em] uppercase font-medium
                    text-black bg-white/95 hover:bg-white
                    transition-colors duration-200
                "
            >
                Clear filters
            </button>
        </div>
    );
});
