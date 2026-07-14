import React, { memo, useState, useCallback, useMemo } from "react";
import { ChevronDown, TrendingUp, Clock, AlertCircle, Maximize2, Minimize2 } from "lucide-react";
import PipelineCard from "./PipelineCard";
import { EmptyLane } from "./PipelineEmptyState";
import {
    DEFAULT_ACCENT,
    STAGE_ACCENTS,
    getStageLabel,
    PIPELINE_STAGE_ORDER,
    EMPTY_STATE_COPY,
} from "./constants";

// Pure utility functions - moved outside for testability
const isStale = (lastActivityTimestamp) => {
    const fiveDaysInMs = 5 * 24 * 60 * 60 * 1000;
    return Date.now() - lastActivityTimestamp > fiveDaysInMs;
};

const STAGE_HEADER_TINTS = {
    ask_to_test: "bg-slate-50/95",
    approved: "bg-emerald-50/95",
    hold: "bg-amber-50/95",
    shortlisted: "bg-violet-50/95",
    already_tested: "bg-fuchsia-50/95",
    locked: "bg-amber-50/95",
    rejected: "bg-rose-50/95",
    not_available: "bg-zinc-50/95",
    not_interested: "bg-zinc-50/95",
    pitch: "bg-teal-50/95",
    follow_up: "bg-amber-50/95",
};

const calculateStaleCount = (items) => {
    return items.filter(item => {
        const timestamp = item.lastActivityTimestamp || item.updatedAtTimestamp;
        return timestamp && isStale(timestamp);
    }).length;
};

const calculateAvgResponseAge = (items) => {
    const itemsWithTimestamp = items.filter(item => 
        item.lastActivityTimestamp || item.updatedAtTimestamp
    );
    
    if (itemsWithTimestamp.length === 0) return null;
    
    const totalDays = itemsWithTimestamp.reduce((sum, item) => {
        const timestamp = item.lastActivityTimestamp || item.updatedAtTimestamp;
        const daysSince = (Date.now() - timestamp) / (1000 * 60 * 60 * 24);
        return sum + Math.max(0, daysSince);
    }, 0);
    
    return (totalDays / itemsWithTimestamp.length).toFixed(1);
};

const calculateConversionRate = (stage, stageItemsMap) => {
    const currentIndex = PIPELINE_STAGE_ORDER.indexOf(stage);
    if (currentIndex <= 0) return null;
    
    const previousStage = PIPELINE_STAGE_ORDER[currentIndex - 1];
    const previousCount = stageItemsMap[previousStage]?.length || 0;
    const currentCount = stageItemsMap[stage]?.length || 0;
    
    if (previousCount === 0) return null;
    return ((currentCount / previousCount) * 100).toFixed(1);
};

// Separate component for focus mode - reduces main component complexity
const FocusedPipelineView = memo(function FocusedPipelineView({
    stage,
    items,
    refresh,
    bulkMode,
    bulkIds,
    onToggleBulkSelect,
    readOnly,
    compact,
    emptyCopy,
    onExitFocus,
}) {
    return (
        <div className="fixed inset-0 z-50 bg-white/98 backdrop-blur-sm flex items-center justify-center p-8">
            <div className="w-full max-w-2xl h-full max-h-[90vh] bg-white rounded-xl shadow-2xl border border-black/10 overflow-hidden flex flex-col">
                <div className="sticky top-0 z-20 bg-white border-b border-black/10 p-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <button
                            onClick={onExitFocus}
                            className="text-black/50 hover:text-black/80 transition-colors p-1 rounded"
                            aria-label="Exit focus mode"
                        >
                            <ChevronDown className="w-5 h-5 rotate-90" />
                        </button>
                        <span className="text-sm font-medium uppercase tracking-wider text-black/70">
                            {getStageLabel(stage)}
                        </span>
                        <span className="text-xs font-mono text-black/40">{items.length} items</span>
                    </div>
                </div>
                <div className="flex-1 overflow-y-auto p-4 space-y-3 bg-[#fafafa]">
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
                                dragSupported={false}
                                compact={compact}
                            />
                        ))
                    )}
                </div>
            </div>
        </div>
    );
});

const PipelineColumn = memo(function PipelineColumn({
    stage,
    projectId,
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
    isCollapsed = false,
    onToggleCollapse,
    isFocused = false,
    onFocus,
    stageMetrics = {},
    stageItemsMap = {},
    // New props for virtualization (future)
    virtualizerRef = null,
    // Responsive width system
    columnWidth = 300,
}) {
    const accent = STAGE_ACCENTS[stage] || DEFAULT_ACCENT;
    const emptyCopy = EMPTY_STATE_COPY?.[stage] || "Empty";

    // ============================================
    // ALL HOOKS MUST BE CALLED BEFORE ANY CONDITIONAL RETURNS
    // Order: useState, useMemo, useCallback
    // ============================================

    // 1. useState hooks
    const [isDragOver, setIsDragOver] = useState(false);
    const [visibleLimit, setVisibleLimit] = useState(20);
    const sentinelRef = React.useRef(null);

    React.useEffect(() => {
        setVisibleLimit(20);
    }, [items.length]);

    React.useEffect(() => {
        if (items.length <= visibleLimit) return;
        const sentinel = sentinelRef.current;
        if (!sentinel) return;

        const observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting) {
                setVisibleLimit((prev) => Math.min(prev + 20, items.length));
            }
        }, { root: sentinel.parentElement, rootMargin: "150px" });

        observer.observe(sentinel);
        return () => observer.disconnect();
    }, [items.length, visibleLimit]);

    // 2. useMemo hooks
    const displayMetrics = useMemo(() => {
        // Use external metrics if provided (backend-calculated)
        if (stageMetrics.count !== undefined) {
            return {
                count: stageMetrics.count,
                stale: stageMetrics.stale ?? 0,
                avgResponse: stageMetrics.avgResponse ?? null,
                conversion: stageMetrics.conversion ?? null,
            };
        }
        
        // Otherwise compute locally (still memoized)
        const staleCount = calculateStaleCount(items);
        const avgResponseAge = calculateAvgResponseAge(items);
        const conversionRate = calculateConversionRate(stage, stageItemsMap);
        
        return {
            count: items.length,
            stale: staleCount,
            avgResponse: avgResponseAge,
            conversion: conversionRate,
        };
    }, [items, stage, stageItemsMap, stageMetrics]);

    const widthClasses = useMemo(() => {
        if (isFocused) return "w-full min-w-0 flex-1";
        if (columnWidth === 300) return "w-full md:w-[300px] md:min-w-[300px] md:max-w-[300px]";
        if (columnWidth === 350) return "w-full md:w-[350px] md:min-w-[350px] md:max-w-[350px]";
        if (columnWidth === 400) return "w-full md:w-[400px] md:min-w-[400px] md:max-w-[400px]";
        return "w-full md:w-[300px] md:min-w-[300px] md:max-w-[300px]";
    }, [columnWidth, isFocused]);

    // 3. Derived variables (non-hook calculations - MUST be before useCallback that depends on them)
    const canSelectAll =
        bulkMode && !readOnly && items.length > 0 && typeof onSelectAll === "function";
    const allInColumnSelected =
        canSelectAll && items.every((i) => bulkIds.has(i.id));

    const isDroppable =
        dragSupported && !readOnly && Boolean(dragId) && typeof onCardDrop === "function";

    const columnLabel = `${getStageLabel(stage)} column with ${displayMetrics.count} items${displayMetrics.stale ? `, ${displayMetrics.stale} stale candidates` : ""}`;

    // 4. useCallback hooks (depend on isDroppable, isCollapsed, etc.)
    const handleToggleSelect = useCallback((id, shiftKey) => {
        onToggleBulkSelect?.(id, shiftKey, items);
    }, [onToggleBulkSelect, items]);

    const handleDragStart = useCallback((id) => {
        onCardDragStart?.(id);
    }, [onCardDragStart]);

    const handleDragEnd = useCallback(() => {
        onCardDragEnd?.();
    }, [onCardDragEnd]);

    const handleDragOver = useCallback((e) => {
        if (!isDroppable || isCollapsed) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
    }, [isDroppable, isCollapsed]);

    const handleDragEnter = useCallback((e) => {
        if (!isDroppable || isCollapsed) return;
        e.preventDefault();
        setIsDragOver(true);
    }, [isDroppable, isCollapsed]);

    const handleDragLeave = useCallback((e) => {
        if (!isDroppable) return;
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setIsDragOver(false);
    }, [isDroppable]);

    const handleDrop = useCallback((e) => {
        if (!isDroppable || isCollapsed) return;
        e.preventDefault();
        setIsDragOver(false);
        const droppedId = e.dataTransfer.getData("text/plain");
        if (droppedId) onCardDrop(stage, droppedId);
    }, [isDroppable, isCollapsed, stage, onCardDrop]);

    // ============================================
    // CONDITIONAL RETURNS (allowed AFTER all hooks)
    // ============================================
    

    // ============================================
    // FINAL JSX RENDER
    // ============================================
    
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
                relative shrink-0 ${widthClasses}
                rounded-lg overflow-visible
                bg-white
                shadow-[0_1px_2px_rgba(0,0,0,0.04)]
                border border-black/[0.08] transition-all duration-200
                ${isCollapsed ? "h-auto" : "h-auto md:h-full"}
                ${isDragOver ? "ring-1 ring-black/[0.06] border-black/[0.10] bg-black/[0.01]" : ""}
            `}
        >
            {/* Stage accent line */}
            <div
                className={`absolute inset-x-0 top-0 h-[2px] ${accent} pointer-events-none rounded-t-lg`}
                aria-hidden="true"
            />

            {/* Sticky header with intelligence metrics (with premium soft header tint) */}
            <div 
                onClick={(e) => onToggleCollapse?.(stage)}
                className={`sticky top-0 z-20 px-4 md:px-5 py-2.5 md:py-3.5 border-b border-black/[0.06] rounded-t-lg transition-all duration-150 ${STAGE_HEADER_TINTS[stage] || "bg-white"} backdrop-blur-md shadow-sm h-[60px] md:h-[68px] flex flex-col justify-center cursor-pointer select-none`}
            >
                {/* Primary header row */}
                <div className="flex items-center justify-between gap-3 mb-2">
                    <div className="min-w-0 flex items-center gap-3">
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                onToggleCollapse?.(stage);
                            }}
                            className="text-black/40 hover:text-black/70 transition-colors p-1"
                            aria-label={isCollapsed ? "Expand column" : "Collapse column"}
                        >
                            <ChevronDown 
                                className={`w-3.5 h-3.5 transform transition-transform duration-150 ${isCollapsed ? "-rotate-90" : ""}`}
                            />
                        </button>
                        <span className="text-[11.5px] tracking-[0.08em] uppercase text-black/70 font-semibold truncate">
                            {getStageLabel(stage)}
                        </span>
                        {readOnly && (
                            <span 
                                className="text-[8px] tracking-wide uppercase text-black/35 font-mono ml-1"
                                aria-label="Read only column"
                            >
                                view
                            </span>
                        )}
                    </div>
                    <div className="flex items-center gap-2">
                        {/* Focus mode button */}
                        {typeof onFocus === "function" && (
                            <button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    onFocus(isFocused ? null : stage);
                                }}
                                className="text-black/35 hover:text-black/60 transition-colors p-0.5"
                                aria-label={isFocused ? `Exit focus on ${getStageLabel(stage)} column` : `Focus on ${getStageLabel(stage)} column`}
                                title={isFocused ? "Exit focus mode" : "Focus mode"}
                            >
                                {isFocused ? <Minimize2 className="w-3 h-3" /> : <Maximize2 className="w-3 h-3" />}
                            </button>
                        )}
                        <span
                            className="text-[11px] font-mono text-black/55 px-2 py-0.5 rounded bg-black/[0.04] border border-black/[0.06] shrink-0"
                            data-testid={`pipeline-column-count-${stage}`}
                            aria-label={`${displayMetrics.count} items`}
                        >
                            {displayMetrics.count}
                        </span>
                    </div>
                </div>

                {/* Intelligence metrics row */}
                <div className="flex flex-wrap items-center gap-2.5 text-[9px] font-mono text-black/45">
                    {displayMetrics.conversion && (
                        <span className="flex items-center gap-1" title="Conversion from previous stage">
                            <TrendingUp className="w-2.5 h-2.5 text-black/40" />
                            <span>{displayMetrics.conversion}%</span>
                        </span>
                    )}
                    {displayMetrics.avgResponse && (
                        <span className="flex items-center gap-1" title="Average response time (days)">
                            <Clock className="w-2.5 h-2.5 text-black/40" />
                            <span>{displayMetrics.avgResponse}d</span>
                        </span>
                    )}
                    {displayMetrics.stale > 0 && (
                        <span className="inline-flex items-center gap-1 text-amber-700 bg-amber-50 border border-amber-200/50 px-1.5 py-0.5 rounded-md font-medium" title="Candidates with no activity for 5+ days">
                            <AlertCircle className="w-2.5 h-2.5 text-amber-600" />
                            <span>{displayMetrics.stale} stale</span>
                        </span>
                    )}
                </div>
            </div>

            {/* Collapsible content */}
            {!isCollapsed && (
                <>
                    {/* Select all section */}
                    {canSelectAll && (
                        <div className="px-4 py-2 border-b border-black/[0.04] bg-black/[0.015]">
                            <button
                                type="button"
                                onClick={() => onSelectAll(items)}
                                data-testid={`pipeline-select-all-${stage}`}
                                aria-label={allInColumnSelected ? "Deselect all items in column" : "Select all items in column"}
                                className="w-full text-left flex items-center justify-between gap-2 text-[10px] tracking-wide uppercase text-black/45 hover:text-black/70 transition-colors duration-100 focus:outline-none focus:ring-1 focus:ring-black/20 rounded"
                            >
                                <span>
                                    {allInColumnSelected ? "Deselect all" : "Select all"}
                                </span>
                                <span className="font-mono text-black/25" aria-hidden="true">
                                    {displayMetrics.count}
                                </span>
                            </button>
                        </div>
                    )}

                    {/* Card stream - using dynamic height to avoid viewport issues */}
                    <div
                        className={`
                            px-2.5 pt-3.5 pb-2.5 space-y-4
                            overflow-y-auto overflow-x-visible
                            bg-[#fafafa]
                            tg-pipeline-scroll
                            ${items.length === 0 ? "min-h-[110px]" : (compact ? "min-h-[220px]" : "min-h-[220px]")}
                        `}
                        style={{
                            maxHeight: isFocused ? "min(75vh, 1000px)" : (compact ? "240px" : "min(64vh, 800px)"), // Cap at 800px for Safari
                        }}
                        role="list"
                        aria-label={`Cards in ${getStageLabel(stage)} stage`}
                    >
                        {items.length === 0 ? (
                            <EmptyLane 
                                label={emptyCopy}
                                stage={stage}
                                suggestions={[
                                    "Source candidates via LinkedIn",
                                    `Set ${getStageLabel(stage)} automation rules`,
                                    "Review candidate pool health"
                                ]}
                            />
                        ) : (
                            <>
                                {items.slice(0, visibleLimit).map((item) => (
                                    <PipelineCard
                                        key={`${stage}-${item.id}`}
                                        projectId={projectId}
                                        item={item}
                                        refresh={refresh}
                                        bulkMode={bulkMode && !readOnly}
                                        isSelected={bulkIds.has(item.id)}
                                        onToggleSelect={handleToggleSelect}
                                        readOnly={readOnly}
                                        dragSupported={dragSupported}
                                        isDragging={dragId === item.id}
                                        onDragStart={handleDragStart}
                                        onDragEnd={handleDragEnd}
                                        compact={compact}
                                    />
                                ))}
                                {items.length > visibleLimit && (
                                    <div
                                        ref={sentinelRef}
                                        className="h-8 flex items-center justify-center text-[10px] text-black/35 font-mono select-none"
                                    >
                                        Loading more candidates...
                                    </div>
                                )}
                            </>
                        )}
                    </div>
                </>
            )}

            {/* Collapsed summary view */}
            {isCollapsed && items.length > 0 && (
                <div className="px-4 py-2 border-t border-black/[0.04] bg-black/[0.01] text-[10px] text-black/40 font-mono flex justify-between">
                    <span>{displayMetrics.count} candidates</span>
                    {displayMetrics.stale > 0 && (
                        <span className="text-amber-600">{displayMetrics.stale} stale</span>
                    )}
                </div>
            )}

            {/* Drop overlay */}
            {isDragOver && (
                <div 
                    className="absolute inset-0 pointer-events-none bg-black/[0.005] rounded-lg transition-opacity duration-100"
                    aria-hidden="true"
                />
            )}
        </div>
    );
});

export default PipelineColumn;
