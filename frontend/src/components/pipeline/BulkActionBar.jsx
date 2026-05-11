import React, { memo, useState } from "react";
import { BULK_MOVE_TARGETS, STAGE_LABELS, getStageLabel } from "./constants";

/**
 * BulkActionBar (PATCH 4C)
 *
 * Floating cinematic action bar anchored to the bottom-center of the
 * viewport. Surfaces only when `count > 0`. Slides up via CSS transform
 * + opacity (no animation library). ESC clears at the page level.
 *
 * Layout: [count badge] [Move to →] [pill, pill, pill, ...] [× clear]
 *
 * Pills are horizontally scrollable on mobile so any number of stages
 * fits on a single line without breaking the bar's visual rhythm.
 */
const BulkActionBar = memo(function BulkActionBar({ count, onClear, onMove }) {
    const visible = count > 0;

    // Local "in-flight" state prevents double-click duplicates. Parent's
    // loading state doesn't surface mid-mutation state for bulk ops.
    const [busy, setBusy] = useState(false);

    const handleMove = async (stage) => {
        if (busy) return;
        setBusy(true);
        try {
            await onMove(stage);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            aria-hidden={!visible}
            data-testid="pipeline-bulk-bar"
            className={`
                fixed z-40 left-1/2 -translate-x-1/2
                bottom-4 sm:bottom-6
                w-[min(94vw,720px)]
                transition-all duration-300 ease-out
                ${visible
                    ? "opacity-100 translate-y-0 pointer-events-auto"
                    : "opacity-0 translate-y-3 pointer-events-none"}
            `}
        >
            <div
                className="
                    flex items-center gap-2 sm:gap-3
                    px-3 py-2 sm:px-4 sm:py-2.5
                    rounded-full
                    bg-black/70 backdrop-blur-xl
                    border border-white/10
                    shadow-[0_18px_48px_-12px_rgba(0,0,0,0.7),inset_0_1px_0_0_rgba(255,255,255,0.05)]
                "
            >
                {/* Count badge — anchors the eye to the selection size */}
                <div
                    className="
                        shrink-0 flex items-center gap-1.5
                        px-2.5 py-1 rounded-full
                        bg-white text-black
                        text-[11px] tracking-[0.16em] uppercase font-medium
                    "
                    data-testid="pipeline-bulk-bar-count"
                >
                    <span className="tg-mono">{count}</span>
                    <span className="opacity-60">selected</span>
                </div>

                <div className="hidden sm:block w-px h-5 bg-white/10" />

                <span className="hidden sm:inline text-[10px] tracking-[0.18em] uppercase text-white/40 shrink-0">
                    Move to
                </span>

                <div
                    className="
                        flex-1 min-w-0 flex items-center gap-1.5
                        overflow-x-auto tg-pipeline-scroll
                        scroll-smooth
                    "
                >
                    {BULK_MOVE_TARGETS.map((stage) => (
                        <button
                            key={stage}
                            type="button"
                            onClick={() => handleMove(stage)}
                            disabled={busy}
                            data-testid={`pipeline-bulk-move-${stage}`}
                            title={`Move ${count} to ${getStageLabel(stage)}`}
                            className="
                                shrink-0
                                px-3 py-1.5 rounded-full
                                text-[10.5px] tracking-[0.12em] uppercase
                                text-white/75 hover:text-white
                                bg-white/[0.05] hover:bg-white/[0.10]
                                border border-white/[0.08] hover:border-white/20
                                transition-all duration-200
                                disabled:opacity-40 disabled:cursor-not-allowed
                                hover:shadow-[0_0_0_3px_rgba(255,255,255,0.04)]
                            "
                        >
                            {STAGE_LABELS[stage] || getStageLabel(stage)}
                        </button>
                    ))}
                </div>

                <button
                    type="button"
                    onClick={onClear}
                    data-testid="pipeline-bulk-bar-clear"
                    title="Clear selection · ESC"
                    className="
                        shrink-0
                        w-8 h-8 rounded-full
                        flex items-center justify-center
                        text-white/55 hover:text-rose-200
                        bg-white/[0.03] hover:bg-rose-300/10
                        border border-white/[0.08] hover:border-rose-300/20
                        transition-all duration-200
                        text-base leading-none
                    "
                >
                    ×
                </button>
            </div>

            <p className="text-center mt-2 text-[9px] tracking-[0.22em] uppercase text-white/25 hidden sm:block">
                Press ESC to clear
            </p>
        </div>
    );
});

export default BulkActionBar;
