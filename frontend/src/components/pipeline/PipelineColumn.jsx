import React, { memo, useState, useCallback } from "react";
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

    const handleDragOver = useCallback((e) => {
        if (!isDroppable) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
    }, [isDroppable]);

    const handleDragEnter = useCallback((e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(true);
    }, [isDroppable]);

    const handleDragLeave = useCallback((e) => {
        if (!isDroppable) return;
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setIsDragOver(false);
    }, [isDroppable]);

    const handleDrop = useCallback((e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(false);
        const droppedId = e.dataTransfer.getData("text/plain");
        if (droppedId) onCardDrop(stage, droppedId);
    }, [isDroppable, stage, onCardDrop]);

    // Accessibility: Announce column count to screen readers
    const columnLabel = `${getStageLabel(stage)} column with ${items.length} items`;

    return (
        <div
            data-testid={`pipeline-column-${stage}`}
            onDragOver={handleDragOver}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            role="region"
            aria-label={columnLabel}
            className={`
                relative shrink-0 w-[320px] min-w-[320px] max-w-[320px]
                rounded-lg overflow-visible
                bg-white
                shadow-[0_4px_14px_-10px_rgba(0,0,0,0.08)]
                border transition-all duration-200
                hover:border-black/[0.12]
                ${
                    isDragOver
                        ? `
                            bg-black/[0.02]
                            ring-1 ring-black/[0.08]
                            border-black/[0.12]
                          `
                        : "border-black/[0.08]"
                }
            `}
        >
            {/* Stage accent line — simple solid muted operational indicator */}
            <div
                className={`absolute inset-x-0 top-0 h-[2px] ${accent} pointer-events-none rounded-t-lg`}
                aria-hidden="true"
            />

            {/* Sticky header — operational clarity */}
            <div
                className="
                    sticky top-[52px] z-10
                    px-4 py-3
                    bg-white/96 backdrop-blur-sm
                    border-b border-black/[0.06]
                    rounded-t-lg
                    flex items-center justify-between gap-3
                "
            >
                <div className="min-w-0 flex items-center gap-2">
                    <span className="text-[11px] tracking-[0.15em] uppercase text-black/70 font-medium truncate">
                        {getStageLabel(stage)}
                    </span>
                    {readOnly && (
                        <span 
                            className="text-[8px] tracking-wide uppercase text-black/35 font-mono"
                            aria-label="Read only column"
                        >
                            view
                        </span>
                    )}
                </div>
                <span
                    className="
                        text-[11px] font-mono text-black/55
                        px-2 py-0.5 rounded-[4px]
                        bg-black/[0.04] border border-black/[0.06]
                        shrink-0
                    "
                    data-testid={`pipeline-column-count-${stage}`}
                    aria-label={`${items.length} items`}
                >
                    {items.length}
                </span>
            </div>

            {/* Select all — subtle operational area */}
            {canSelectAll && (
                <div className="px-4 py-2 border-b border-black/[0.04] bg-black/[0.015]">
                    <button
                        type="button"
                        onClick={() => onSelectAll(items)}
                        data-testid={`pipeline-select-all-${stage}`}
                        aria-label={allInColumnSelected ? "Deselect all items in column" : "Select all items in column"}
                        className="
                            w-full text-left flex items-center justify-between gap-2
                            text-[10px] tracking-wide uppercase
                            text-black/45 hover:text-black/70
                            transition-colors duration-200
                            focus:outline-none focus:ring-1 focus:ring-black/20 rounded
                        "
                    >
                        <span>
                            {allInColumnSelected
                                ? "Deselect all"
                                : "Select all"}
                        </span>
                        <span className="font-mono text-black/25" aria-hidden="true">
                            {items.length}
                        </span>
                    </button>
                </div>
            )}

            {/* Card stream - subtle operational lane background */}
            <div
                className={`
                    px-3 py-3 space-y-3
                    overflow-y-auto overflow-x-visible
                    bg-[#fafafa]
                    tg-pipeline-scroll
                    ${
                        compact
                            ? "min-h-[180px] max-h-[240px]"
                            : "min-h-[180px] max-h-[64vh]"
                    }
                `}
                role="list"
                aria-label={`Cards in ${getStageLabel(stage)} stage`}
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

            {/* Drop overlay — minimal operational feedback */}
            {isDragOver && (
                <div 
                    className="absolute inset-0 pointer-events-none bg-black/[0.01] rounded-lg transition-opacity duration-150"
                    aria-hidden="true"
                />
            )}
        </div>
    );
});

export default PipelineColumn;
