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
            className="fixed z-40 left-1/2 -translate-x-1/2 bottom-5 w-[min(90vw,600px)]"
        >
            <div
                className="
                    flex items-center gap-2
                    px-3 py-1.5
                    rounded-full
                    bg-black/90
                    border border-white/12
                    shadow-lg
                "
            >
                <div
                    className="
                        shrink-0 flex items-center gap-1
                        px-2 py-0.5 rounded-full
                        bg-white/90 text-black
                        text-[9px] tracking-wide uppercase font-medium
                    "
                    data-testid="pipeline-bulk-bar-count"
                >
                    {count}
                </div>

                <div className="w-px h-4 bg-white/10" />

                <span className="text-[8px] tracking-wide uppercase text-white/30 shrink-0">
                    Move
                </span>

                <div className="flex-1 min-w-0 flex items-center gap-1 overflow-x-auto">
                    {BULK_MOVE_TARGETS.map((stage) => (
                        <button
                            key={stage}
                            type="button"
                            onClick={() => handleMove(stage)}
                            disabled={busy}
                            data-testid={`pipeline-bulk-move-${stage}`}
                            className="
                                shrink-0
                                px-2 py-0.5 rounded-full
                                text-[8px] tracking-wide uppercase
                                text-white/60 hover:text-white/80
                                bg-white/[0.04] hover:bg-white/[0.07]
                                border border-white/[0.05]
                                transition-all duration-150
                                disabled:opacity-40
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
                        text-white/35 hover:text-rose-300/50
                        hover:bg-rose-500/8
                        transition-all duration-200
                        text-base
                    "
                >
                    ×
                </button>
            </div>
        </div>
    );
});

export default BulkActionBar;
