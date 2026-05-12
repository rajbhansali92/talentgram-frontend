import React from "react";
import PipelineColumn from "./PipelineColumn";
import { EMPTY_BULK_SET, NOOP } from "./constants";

function FollowUpLane({ items, refresh }) {
    if (items.length === 0) return null;

    return (
        <div className="mt-6 pt-4 border-t border-white/[0.03]">
            <div className="flex items-center gap-2 mb-2 px-1">
                <span className="text-[8px] tracking-wide uppercase text-white/30">
                    Follow-up
                </span>
                <span className="text-[7px] font-mono text-white/20">
                    {items.length}
                </span>
            </div>
            <div className="overflow-x-auto tg-pipeline-scroll">
                <div className="w-[268px]">
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
        </div>
    );
}

export default FollowUpLane;
