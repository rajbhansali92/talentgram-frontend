import React from "react";
import PipelineColumn from "./PipelineColumn";
import { EMPTY_BULK_SET, NOOP } from "./constants";

function FollowUpLane({ items, refresh }) {
    if (items.length === 0) return null;

    return (
        <div className="mt-4 px-2 sm:px-4">
            <div className="
                relative
                rounded-2xl
                bg-gradient-to-b from-white/[0.015] to-transparent
                border border-white/[0.04]
                shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]
                overflow-hidden
            ">
                {/* Section header */}
                <div className="px-4 pt-3 pb-2.5">
                    <div className="flex items-center gap-3">
                        <div className="flex items-center gap-2">
                            <span className="text-[10px] tracking-[0.18em] uppercase text-white/40 font-medium">
                                Follow-up
                            </span>
                            <span className="text-[9px] font-mono text-white/35">
                                {items.length}
                            </span>
                        </div>
                        <div className="h-3 w-px bg-white/[0.06]" aria-hidden="true" />
                        <span className="text-[8px] text-white/30 tracking-wide">
                            Pending test submissions
                        </span>
                    </div>
                </div>

                {/* Column container - width matched to main pipeline */}
                <div className="pb-3 px-4">
                    <div className="w-[340px]">
                        <PipelineColumn
                            stage="follow_up"
                            items={items}
                            refresh={refresh}
                            bulkMode={false}
                            bulkIds={EMPTY_BULK_SET}
                            onToggleBulkSelect={NOOP}
                            readOnly
                            compact
                        />
                    </div>
                </div>

                {/* Atmospheric inner glow */}
                <div 
                    className="absolute inset-0 pointer-events-none bg-gradient-to-t from-white/[0.005] via-transparent to-transparent"
                    aria-hidden="true"
                />
            </div>
        </div>
    );
}

export default FollowUpLane;
