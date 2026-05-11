import React, { memo, useState } from "react";
import PipelineCard from "./PipelineCard";
import { EmptyLane } from "./PipelineEmptyState";
import {
    DEFAULT_ACCENT,
    EMPTY_STATE_COPY,
    STAGE_ACCENTS,
    getStageLabel,
} from "./constants";

/**
 * PipelineColumn — single stage column.
 *
 * Cinematic glass-panelled card with:
 *   • thin stage-accent line at the very top
 *   • sticky header that survives vertical scroll
 *   • optional "Select all in column" affordance (PATCH 4C)
 *   • calm empty state
 *   • native HTML5 drop target (PATCH 4D)
 *
 * A column is "droppable" when:
 *   • drag is supported (pointer-fine device)
 *   • the lane is not read-only (follow-up is opt-out)
 *   • there's an active drag (`dragId` set in parent)
 *   • a drop callback is wired
 *
 * `isDragOver` is local — only this column re-renders during hover.
 */
const PipelineColumn = memo(function PipelineColumn({
    stage,
    items,
    refresh,
    bulkMode,
    bulkIds,
    onToggleBulkSelect,
    onSelectAll,
    readOnly = false,
    dragSupported = false,
    dragId = null,
    onCardDragStart,
    onCardDragEnd,
    onCardDrop,
    compact = false,
}) {
    const accent = STAGE_ACCENTS[stage] || DEFAULT_ACCENT;
    const emptyCopy = EMPTY_STATE_COPY[stage] || "Nothing here yet";

    // Per-column "Select all" affordance. Only surfaces when we're in
    // bulk mode AND the lane is mutable (read-only lanes like follow_up
    // are explicitly excluded by the spec).
    const canSelectAll =
        bulkMode && !readOnly && items.length > 0 && typeof onSelectAll === "function";
    const allInColumnSelected =
        canSelectAll && items.every((i) => bulkIds.has(i.id));

    const isDroppable =
        dragSupported && !readOnly && Boolean(dragId) && typeof onCardDrop === "function";

    const [isDragOver, setIsDragOver] = useState(false);

    const handleDragOver = (e) => {
        if (!isDroppable) return;
        // preventDefault is what tells the browser "yes, this is a drop
        // target". Without it the onDrop handler never fires.
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
    };

    const handleDragEnter = (e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(true);
    };

    const handleDragLeave = (e) => {
        if (!isDroppable) return;
        // Only clear when the pointer truly leaves the column shell —
        // dragenter/leave bubble through every child, so we guard with
        // currentTarget vs relatedTarget.
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setIsDragOver(false);
    };

    const handleDrop = (e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(false);
        const droppedId = e.dataTransfer.getData("text/plain");
        if (droppedId) onCardDrop(stage, droppedId);
    };

    return (
        <div
            data-testid={`pipeline-column-${stage}`}
            onDragOver={handleDragOver}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`
                relative shrink-0 w-[280px] min-w-[280px] max-w-[280px]
                rounded-xl overflow-hidden
                bg-[#131313]
                border transition-all duration-200
                backdrop-blur-sm
                ${
                    isDragOver
                        ? "border-white/25 ring-1 ring-white/8 shadow-[0_12px_28px_-12px_rgba(0,0,0,0.5)]"
                        : "border-white/[0.08] shadow-[0_4px_16px_-8px_rgba(0,0,0,0.4)]"
                }
            `}
        >
            {/* Stage accent — paper-thin gradient line that gives each lane
                a quiet sense of identity without colouring the whole card. */}
            <div
                className={`absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r ${accent} pointer-events-none`}
                aria-hidden
            />

            {/* Sticky header — survives vertical scroll inside the column.
                Slight backdrop-blur so cards passing under it stay legible. */}
            <div
                className="
                    sticky top-0 z-10
                    px-4 py-2.5
                    bg-[#1b1b1b]/95
                    border-b border-white/[0.06]
                    flex items-center justify-between gap-2
                "
            >
                <div className="min-w-0 flex items-center gap-2">
                    <span className="text-[11px] tracking-[0.22em] uppercase text-white/75 font-medium truncate">
                        {getStageLabel(stage)}
                    </span>
                    {readOnly && (
                        <span className="text-[9px] tracking-[0.18em] uppercase text-amber-200/50 tg-mono">
                            read-only
                        </span>
                    )}
                </div>
                <span
                    className="
                        text-[10px] tg-mono text-white/50
                        px-2 py-0.5 rounded-full
                        bg-white/[0.04] border border-white/[0.05]
                        shrink-0
                    "
                    data-testid={`pipeline-column-count-${stage}`}
                >
                    {items.length}
                </span>
            </div>

            {/* Per-column Select-all affordance (PATCH 4C). */}
            {canSelectAll && (
                <div className="px-4 py-1.5 border-b border-white/[0.03] bg-black/15">
                    <button
                        type="button"
                        onClick={() => onSelectAll(items)}
                        data-testid={`pipeline-select-all-${stage}`}
                        className="
                            w-full text-left flex items-center justify-between gap-2
                            text-[10px] tracking-[0.18em] uppercase
                            text-white/50 hover:text-white/80
                            transition-colors duration-200
                        "
                    >
                        <span>
                            {allInColumnSelected
                                ? "Deselect column"
                                : "Select all in column"}
                        </span>
                        <span className="tg-mono text-white/30">
                            {items.length}
                        </span>
                    </button>
                </div>
            )}

            {/* Card stream — independent vertical scroll. The fixed
                viewport height keeps the board cinematic and predictable. */}
            <div
                className={`
                    px-3 py-3 space-y-2
                    overflow-y-auto tg-pipeline-scroll
                    ${
                        compact
                            ? "min-h-[200px] max-h-[280px]"
                            : "min-h-[240px] max-h-[52vh]"
                    }
                `}
            >
                {items.length === 0 ? (
                    <EmptyLane label={emptyCopy} />
                ) : (
                    items.map((item) => (
                        <PipelineCard
                            key={`${stage}-${item.id}`}
                            item={item}
                            refresh={refresh}
                            bulkMode={bulkMode && !readOnly}
                            isSelected={bulkIds.has(item.id)}
                            onToggleSelect={onToggleBulkSelect}
                            readOnly={readOnly}
                            dragSupported={dragSupported}
                            isDragging={dragId === item.id}
                            onDragStart={onCardDragStart}
                            onDragEnd={onCardDragEnd}
                            compact={compact}
                        />
                    ))
                )}
            </div>
        </div>
    );
});

export default PipelineColumn;
