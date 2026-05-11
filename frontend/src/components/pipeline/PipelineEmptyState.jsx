import React, { memo } from "react";

/**
 * PipelineEmptyState — two flavours:
 *   EmptyLane: column-level placeholder (no rows in this stage)
 *   FilterEmptyState: board-level placeholder when active filters resolve
 *                     to zero matches.
 *
 * Both are intentionally quiet — no big icons, no bright CTAs, no noise.
 * Enhanced with softer layering and cinematic calm.
 */

export const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-8 flex flex-col items-center justify-center gap-2 text-center">
            {/* Soft decorative divider — opacity reduced for luxury feel */}
            <div className="w-8 h-px bg-gradient-to-r from-transparent via-white/12 to-transparent" />
            <p className="text-[11px] tracking-wide text-white/22 italic">
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
                mt-8 mb-8
                flex flex-col items-center justify-center text-center
                py-20 px-6
                rounded-2xl
                bg-gradient-to-b from-white/[0.02] to-transparent
                border border-white/[0.04]
                backdrop-blur-sm
            "
        >
            <div className="w-12 h-px bg-gradient-to-r from-transparent via-white/15 to-transparent mb-4" />
            <p className="text-[11px] tracking-[0.22em] uppercase text-white/35 mb-2">
                No talents match
            </p>
            <p className="text-sm text-white/50 max-w-md leading-relaxed">
                Try widening the search, switching focus to{" "}
                <span className="text-white/75">All</span>, or clearing the active filters.
            </p>
            <button
                type="button"
                onClick={onReset}
                data-testid="pipeline-filter-empty-reset"
                className="
                    mt-6 px-4 py-2 rounded-full
                    text-[10px] tracking-[0.18em] uppercase font-medium
                    text-black bg-white/92 hover:bg-white
                    transition-colors duration-200
                "
            >
                Clear filters
            </button>
        </div>
    );
});
