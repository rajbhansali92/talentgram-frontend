import React, { memo, useState } from "react";
import { BULK_MOVE_TARGETS, STAGE_LABELS, getStageLabel } from "./constants";

const BulkActionBar = memo(function BulkActionBar({ count, onClear, onMove }) {
    const visible = count > 0;
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

    if (!visible) return null;

    return (
        <div
            data-testid="pipeline-bulk-bar"
            className="fixed z-40 left-1/2 -translate-x-1/2 bottom-[max(1.5rem,env(safe-area-inset-bottom))] w-[min(90vw,600px)]"
        >
            <div
                className="
                    flex items-center gap-2
                    px-3 py-2
                    rounded-full
                    bg-gradient-to-b from-[#171717]/95 to-[#101010]/92
                    backdrop-blur-xl
                    border border-white/[0.06]
                    shadow-[0_10px_35px_-18px_rgba(0,0,0,0.7)]
                    shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]
                    ring-1 ring-white/[0.03]
                "
            >
                <div
                    className="
                        shrink-0 flex items-center gap-1
                        px-2 py-0.5 rounded-full
                        bg-white/[0.12]
                        border border-white/[0.08]
                        text-white/90
                        text-[10px] tracking-[0.18em] uppercase font-medium
                    "
                    data-testid="pipeline-bulk-bar-count"
                >
                    {count}
                </div>

                <div className="w-px h-4 bg-white/[0.05]" />

                <span className="text-[9px] tracking-[0.18em] uppercase text-white/30 shrink-0">
                    Move
                </span>

                <div className="flex-1 min-w-0 flex items-center gap-1.5 overflow-x-auto">
                    {BULK_MOVE_TARGETS.map((stage) => (
                        <button
                            key={stage}
                            type="button"
                            onClick={() => handleMove(stage)}
                            disabled={busy}
                            data-testid={`pipeline-bulk-move-${stage}`}
                            className="
                                shrink-0
                                px-2.5 py-1 rounded-full
                                text-[9px] tracking-[0.12em] uppercase
                                text-white/60 hover:text-white/80
                                bg-white/[0.05] hover:bg-white/[0.08]
                                border border-white/[0.05] hover:border-white/[0.1]
                                transition-all duration-150
                                disabled:opacity-40 disabled:cursor-not-allowed
                            "
                        >
                            {STAGE_LABELS[stage]?.split(" ")[0] || getStageLabel(stage).split(" ")[0]}
                        </button>
                    ))}
                </div>

                <button
                    type="button"
                    onClick={onClear}
                    data-testid="pipeline-bulk-bar-clear"
                    className="
                        shrink-0
                        w-6 h-6 rounded-full
                        flex items-center justify-center
                        text-white/35 hover:text-white/70
                        hover:bg-white/[0.05]
                        transition-all duration-200
                        text-base leading-none
                    "
                >
                    ×
                </button>
            </div>
        </div>
    );
});

export default BulkActionBar;
