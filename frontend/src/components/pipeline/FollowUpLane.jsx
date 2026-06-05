import React from "react";
import PipelineColumn from "./PipelineColumn";
import PipelineCard from "./PipelineCard";
import { EMPTY_BULK_SET, NOOP } from "./constants";
import { Maximize2, Minimize2, ArrowLeft } from "lucide-react";

function FollowUpLane({
    projectId,
    items = [],
    refresh,
    focusedStageId,
    onFocus,
    isExpanded = false,
    onToggleExpand = NOOP,
    dragSupported = false,
    dragId = null,
    onCardDragStart = NOOP,
    onCardDragEnd = NOOP,
    onCardDrop = NOOP,
}) {
    const isFocused = focusedStageId === "follow_up";
    const [visibleLimit, setVisibleLimit] = React.useState(24);
    const sentinelRef = React.useRef(null);

    React.useEffect(() => {
        setVisibleLimit(24);
    }, [items.length, isExpanded]);

    React.useEffect(() => {
        if (!isExpanded || items.length <= visibleLimit) return;
        const sentinel = sentinelRef.current;
        if (!sentinel) return;

        const observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting) {
                setVisibleLimit((prev) => Math.min(prev + 24, items.length));
            }
        }, { rootMargin: "150px" });

        observer.observe(sentinel);
        return () => observer.disconnect();
    }, [items.length, visibleLimit, isExpanded]);

    if (isExpanded) {
        return (
            <div className="mt-4 px-2 sm:px-4" data-testid="follow-up-expanded-workspace">
                <div className="
                    relative
                    rounded-2xl
                    bg-white
                    border border-black/[0.08]
                    shadow-[0_4px_20px_-4px_rgba(0,0,0,0.05)]
                    overflow-hidden
                    transition-all duration-200
                ">
                    {/* Header with back button, title, and minimize button */}
                    <div className="px-4 sm:px-6 py-4 border-b border-black/[0.06] bg-slate-50/50 backdrop-blur-md flex flex-wrap items-center justify-between gap-3">
                        <div className="flex items-center gap-3">
                            <button
                                onClick={onToggleExpand}
                                className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 border border-black/[0.08] hover:border-black/[0.16] hover:bg-black/[0.02] rounded-lg text-xs font-medium text-black/60 hover:text-black transition-colors"
                                title="Back to Pipeline"
                            >
                                <ArrowLeft className="w-4 h-4" />
                                <span>Back to Pipeline</span>
                            </button>
                            <div className="h-4 w-px bg-black/[0.08]" />
                            <div className="flex items-center gap-2">
                                <span className="text-xs tracking-[0.18em] uppercase text-black/50 font-semibold">
                                    Follow-up Workspace
                                </span>
                                <span className="text-xs font-mono text-black/45 px-2 py-0.5 rounded bg-black/[0.04]">
                                    {items.length} candidates
                                </span>
                            </div>
                        </div>
                        <button
                            onClick={onToggleExpand}
                            className="p-2 border border-black/[0.08] hover:border-black/[0.16] hover:bg-black/[0.02] rounded-lg text-black/55 hover:text-black transition-colors"
                            title="Minimize View"
                        >
                            <Minimize2 className="w-4 h-4" />
                        </button>
                    </div>

                    {/* Expanded Grid Area */}
                    <div className="p-4 sm:p-6 bg-[#fafafa] min-h-[400px]">
                        {items.length === 0 ? (
                            <div className="flex flex-col items-center justify-center py-20 text-center">
                                <p className="text-sm text-black/40 font-medium">All caught up</p>
                                <p className="text-xs text-black/30 mt-1">No pending invitations or follow-ups at the moment.</p>
                            </div>
                        ) : (
                            <>
                                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                                    {items.slice(0, visibleLimit).map((item) => (
                                        <PipelineCard
                                            key={`follow-up-expanded-${item.id}`}
                                            projectId={projectId}
                                            item={item}
                                            refresh={refresh}
                                            bulkMode={false}
                                            isSelected={false}
                                            onToggleSelect={NOOP}
                                            readOnly={false}
                                            dragSupported={dragSupported}
                                            isDragging={dragId === item.id}
                                            onDragStart={onCardDragStart}
                                            onDragEnd={onCardDragEnd}
                                        />
                                    ))}
                                </div>
                                {items.length > visibleLimit && (
                                    <div
                                        ref={sentinelRef}
                                        className="h-10 flex items-center justify-center text-[10px] text-black/35 font-mono select-none mt-4"
                                    >
                                        Loading more candidates...
                                    </div>
                                )}
                            </>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="mt-4 px-2 sm:px-4">
            <div className="
                relative
                rounded-2xl
                bg-white
                border border-black/[0.08]
                shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]
                overflow-hidden
            ">
                {/* Section header */}
                <div className="px-4 pt-3 pb-2.5 border-b border-black/[0.04] bg-slate-50/30 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className="flex items-center gap-2">
                            <span className="text-[10px] tracking-[0.18em] uppercase text-black/60 font-semibold">
                                Follow-up
                            </span>
                            <span className="text-[10px] font-mono text-black/55 px-1.5 py-0.5 rounded bg-black/[0.04]">
                                {items.length}
                            </span>
                        </div>
                        <div className="h-3 w-px bg-black/[0.06]" aria-hidden="true" />
                        <span className="text-[9px] text-black/40 tracking-wide font-medium">
                            Pending test submissions
                        </span>
                    </div>
                    
                    {/* Expand/Maximize button in the header */}
                    <button
                        onClick={onToggleExpand}
                        className="p-1 border border-black/[0.06] hover:border-black/[0.12] hover:bg-black/[0.02] rounded text-black/45 hover:text-black transition-colors"
                        title="Expand Follow-up Workspace"
                    >
                        <Maximize2 className="w-3.5 h-3.5" />
                    </button>
                </div>

                {/* Column container - width matched to main pipeline */}
                <div className="pb-3 px-4 pt-3 bg-[#fafafa]">
                    <div className={isFocused ? "w-full" : "w-[340px]"}>
                        <PipelineColumn
                            stage="follow_up"
                            projectId={projectId}
                            items={items}
                            refresh={refresh}
                            bulkMode={false}
                            bulkIds={EMPTY_BULK_SET}
                            onToggleBulkSelect={NOOP}
                            readOnly={false}
                            compact
                            isFocused={isFocused}
                            onFocus={onFocus}
                            dragSupported={dragSupported}
                            dragId={dragId}
                            onCardDragStart={onCardDragStart}
                            onCardDragEnd={onCardDragEnd}
                            onCardDrop={onCardDrop}
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
