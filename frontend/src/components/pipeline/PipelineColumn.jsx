import React, { memo, useState } from "react";
import PipelineCard from "./PipelineCard";
import { EmptyLane } from "./PipelineEmptyState";
import {
    DEFAULT_ACCENT,
    EMPTY_STATE_COPY,
    STAGE_ACCENTS,
    getStageLabel,
} from "./constants";

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
    const emptyCopy = EMPTY_STATE_COPY[stage] || "Empty";

    const canSelectAll =
        bulkMode && !readOnly && items.length > 0 && typeof onSelectAll === "function";
    const allInColumnSelected =
        canSelectAll && items.every((i) => bulkIds.has(i.id));

    const isDroppable =
        dragSupported && !readOnly && Boolean(dragId) && typeof onCardDrop === "function";

    const [isDragOver, setIsDragOver] = useState(false);

    const handleDragOver = (e) => {
        if (!isDroppable) return;
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
                relative shrink-0 w-[268px] min-w-[268px] max-w-[268px]
                rounded-lg overflow-hidden
                bg-[#111]
                border transition-all duration-200
                ${
                    isDragOver
                        ? "border-white/20"
                        : "border-white/[0.06]"
                }
            `}
        >
            {/* Stage accent line — very subtle */}
            <div
                className={`absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r ${accent} pointer-events-none`}
                aria-hidden
            />

            {/* Sticky header */}
            <div
                className="
                    sticky top-0 z-10
                    px-3 py-2
                    bg-[#141414]
                    border-b border-white/[0.04]
                    flex items-center justify-between gap-2
                "
            >
                <div className="min-w-0 flex items-center gap-1.5">
                    <span className="text-[9px] tracking-wide uppercase text-white/65 font-medium truncate">
                        {getStageLabel(stage)}
                    </span>
                    {readOnly && (
                        <span className="text-[7px] tracking-wide uppercase text-amber-400/30">
                            ro
                        </span>
                    )}
                </div>
                <span
                    className="
                        text-[8px] font-mono text-white/35
                        px-1.5 py-0.5 rounded
                        bg-white/[0.03]
                        shrink-0
                    "
                    data-testid={`pipeline-column-count-${stage}`}
                >
                    {items.length}
                </span>
            </div>

            {/* Select all */}
            {canSelectAll && (
                <div className="px-3 py-1.5 border-b border-white/[0.02] bg-black/30">
                    <button
                        type="button"
                        onClick={() => onSelectAll(items)}
                        data-testid={`pipeline-select-all-${stage}`}
                        className="
                            w-full text-left flex items-center justify-between gap-2
                            text-[8px] tracking-wide uppercase
                            text-white/35 hover:text-white/60
                            transition-colors duration-200
                        "
                    >
                        <span>
                            {allInColumnSelected
                                ? "Deselect all"
                                : "Select all"}
                        </span>
                        <span className="font-mono text-white/15">
                            {items.length}
                        </span>
                    </button>
                </div>
            )}

            {/* Card stream */}
            <div
                className={`
                    px-2 py-2 space-y-2
                    overflow-y-auto tg-pipeline-scroll
                    ${
                        compact
                            ? "min-h-[180px] max-h-[260px]"
                            : "min-h-[260px] max-h-[52vh]"
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
