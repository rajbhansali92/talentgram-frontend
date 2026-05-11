import React from "react";
import PipelineColumn from "./PipelineColumn";
import { BoardSection, BoardRow } from "./PipelineBoardSection";
import { EMPTY_BULK_SET, NOOP } from "./constants";

/**
 * FollowUpLane — virtual read-only lane (PATCH 3C).
 *
 * Renders the follow-up section using the standard PipelineColumn in
 * read-only mode. Bulk selection is hard-disabled (EMPTY_BULK_SET + NOOP)
 * because cards in this lane represent tests that haven't been submitted
 * yet, not actionable pipeline rows.
 *
 * The lane is `is_follow_up === true`, computed server-side per row.
 * It's NEVER persisted as a stage in the DB.
 */
function FollowUpLane({ items, refresh }) {
    return (
        <BoardSection
            eyebrow="Follow-up"
            helper="Test pending · auto-cleared on submission"
            muted
            className="mt-4" // Fixed spacing
        >
            {/* 
               CRITICAL FIX: Removed the <div className="overflow-x-hidden"> 
               that was causing column clipping and layout compression.
            */}
            <BoardRow testid="pipeline-follow-up">
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
            </BoardRow>
        </BoardSection>
    );
}

export default FollowUpLane;
