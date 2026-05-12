import React, {
    memo,
    useState,
    useEffect,
    useRef,
    useCallback,
} from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";
import TalentAvatar from "./TalentAvatar";
import {
    NEXT_STAGE_FLOW,
    STAGE_LABELS,
    STATUS_TONES,
    getStageLabel,
    normaliseStage,
    VISIBLE_ACTIONS_PER_CARD,
} from "./constants";

const PipelineCard = memo(function PipelineCard({
    item,
    refresh,
    bulkMode,
    isSelected,
    onToggleSelect,
    readOnly = false,
    dragSupported = false,
    isDragging = false,
    onDragStart,
    onDragEnd,
    compact = false,
}) {
    const [moving, setMoving] = useState(false);
    const [showMoreActions, setShowMoreActions] = useState(false);
    const overflowRef = useRef(null);
    const moreButtonRef = useRef(null);

    // PERFORMANCE FIX: Only attach global listeners when menu is open
    useEffect(() => {
        if (!showMoreActions) return;

        function handleClickOutside(e) {
            if (
                overflowRef.current &&
                !overflowRef.current.contains(e.target) &&
                moreButtonRef.current &&
                !moreButtonRef.current.contains(e.target)
            ) {
                setShowMoreActions(false);
            }
        }

        function handleEsc(e) {
            if (e.key === "Escape") {
                setShowMoreActions(false);
            }
        }

        // Add listeners only when menu is open
        document.addEventListener("mousedown", handleClickOutside);
        document.addEventListener("keydown", handleEsc);

        return () => {
            // Clean up listeners when menu closes
            document.removeEventListener("mousedown", handleClickOutside);
            document.removeEventListener("keydown", handleEsc);
        };
    }, [showMoreActions]); // Re-run when menu open state changes

    // Memoized move function to prevent unnecessary re-renders
    const move = useCallback(async (stage) => {
        setMoving(true);
        try {
            await adminApi.patch("/pipeline/move", {
                ids: [item.id],
                stage,
            });
            await refresh();
            // Close overflow menu after successful move (menu is already handled)
        } catch (e) {
            console.error("Move failed:", e);
            toast.error(e?.response?.data?.detail || "Move failed");
        } finally {
            setMoving(false);
        }
    }, [item.id, refresh]);

    // Memoized close menu function
    const closeMoreMenu = useCallback(() => {
        setShowMoreActions(false);
    }, []);

    // Memoized toggle menu function
    const toggleMoreMenu = useCallback(() => {
        setShowMoreActions(prev => !prev);
    }, []);

    const canonicalStage = normaliseStage(item.stage);
    const nextStages = NEXT_STAGE_FLOW[canonicalStage] || [];
    const statusTone = STATUS_TONES[canonicalStage];

    // Show only first N actions, rest in overflow
    const visibleActions = nextStages.slice(0, VISIBLE_ACTIONS_PER_CARD);
    const overflowActions = nextStages.slice(VISIBLE_ACTIONS_PER_CARD);

    const displayName = item.talent_name || item.talent_id || "Unknown";
    const displayEmail = item.talent_email || item.email || null;
    const displayPhone = item.talent_phone || null;
    const displayIg = item.instagram_handle || null;

    const draggable = dragSupported && !readOnly && !bulkMode;

    const handleDragStart = useCallback((e) => {
        if (!draggable) return;
        e.dataTransfer.setData("text/plain", item.id);
        e.dataTransfer.effectAllowed = "move";
        setTimeout(() => onDragStart && onDragStart(item.id), 0);
    }, [draggable, item.id, onDragStart]);

    const handleDragEnd = useCallback(() => {
        if (!draggable) return;
        if (onDragEnd) onDragEnd();
    }, [draggable, onDragEnd]);

    const handleKeyDown = useCallback((e) => {
        // Accessibility: Enter or Space triggers selection in bulk mode
        if (bulkMode && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onToggleSelect(item.id);
        }
    }, [bulkMode, item.id, onToggleSelect]);

    // Handle overflow action click - closes menu AFTER move completes
    const handleOverflowAction = useCallback(async (stage) => {
        // Close menu immediately for better UX
        setShowMoreActions(false);
        await move(stage);
    }, [move]);

    // Operational card styling - calm, stable, recruiter-focused
    const shellClass = [
        "group relative rounded-lg overflow-hidden",
        "transition-all duration-150",
        "bg-white",
        "shadow-[0_1px_2px_rgba(0,0,0,0.04)]",
        "border",
        "min-h-[108px]",
        isSelected
            ? "border-black/20 ring-1 ring-black/10"
            : "border-black/[0.08]",
        readOnly
            ? ""
            : "hover:border-black/[0.12]",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging
            ? "opacity-75 scale-[0.995]"
            : "",
        draggable ? "cursor-grab active:cursor-grabbing" : "",
    ].join(" ");

    if (bulkMode) {
        return (
            <div
                data-testid={`pipeline-card-${item.id}`}
                onClick={() => onToggleSelect(item.id)}
                onKeyDown={handleKeyDown}
                draggable={draggable}
                onDragStart={handleDragStart}
                onDragEnd={handleDragEnd}
                className="group relative rounded-lg overflow-hidden transition-all duration-150 bg-[#fafafa] border border-black/[0.08] min-h-[108px] px-3 py-2.5 cursor-pointer hover:border-black/[0.12]"
                role="checkbox"
                aria-checked={isSelected}
                tabIndex={0}
            >
                <div className="flex items-center gap-2.5">
                    <div className="relative flex-shrink-0">
                        <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => onToggleSelect(item.id)}
                            onClick={(e) => e.stopPropagation()}
                            className="
                                w-4 h-4 rounded-[3px]
                                border border-black/30 bg-white
                                checked:bg-black checked:border-black
                                transition-all duration-100
                                cursor-pointer
                                focus:outline-none focus:ring-1 focus:ring-black/20
                            "
                            aria-label={`Select ${displayName}`}
                        />
                    </div>
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="md"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="text-[13px] text-black/85 font-medium leading-[1.25] truncate">
                            {displayName}
                        </p>
                        {displayEmail && (
                            <p className="text-[9px] text-black/45 truncate mt-1">
                                {displayEmail}
                            </p>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div
            data-testid={`pipeline-card-${item.id}`}
            draggable={draggable}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            className={shellClass}
            aria-label={`Talent: ${displayName}`}
        >
            <div className="p-3 space-y-2">
                {/* Identity row */}
                <div className="flex items-start gap-2.5">
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="md"
                    />
                    <div className="flex-1 min-w-0">
                        <p
                            className="text-[13px] text-black/85 font-medium leading-[1.25] truncate"
                            title={displayName}
                        >
                            {displayName}
                        </p>
                        {displayIg && (
                            <p className="text-[8.5px] text-black/55 truncate mt-1">
                                @{displayIg}
                            </p>
                        )}
                        {!displayIg && item.talent_name && (
                            <p className="text-[8.5px] text-black/40 truncate font-mono mt-1">
                                {item.talent_id?.slice(0, 8)}…
                            </p>
                        )}
                    </div>

                    {/* Status chip - operational utility indicator */}
                    {statusTone && (
                        <span
                            className={`shrink-0 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border ${statusTone.chip}`}
                            title={statusTone.label}
                            role="status"
                            aria-label={`Status: ${statusTone.label}`}
                        >
                            <span
                                className={`w-1 h-1 rounded-full ${statusTone.dot}`}
                                aria-hidden="true"
                            />
                            <span
                                className={`text-[7.5px] tracking-wide uppercase ${statusTone.text}`}
                            >
                                {statusTone.label}
                            </span>
                        </span>
                    )}
                </div>

                {/* Metadata - clear operational hierarchy, minimal monospace */}
                {(displayEmail || displayPhone) && (
                    <div className="space-y-1" aria-label="Contact information">
                        {displayEmail && (
                            <p className="text-[9.5px] text-black/60 truncate" title={displayEmail}>
                                {displayEmail}
                            </p>
                        )}
                        {displayPhone && (
                            <p className="text-[9.5px] text-black/45 truncate" title={displayPhone}>
                                {displayPhone}
                            </p>
                        )}
                    </div>
                )}

                {/* Action controls - operational utility, not pill buttons */}
                {!readOnly && visibleActions.length > 0 && (
                    <div 
                        className="flex flex-wrap items-center gap-1 pt-2 border-t border-black/[0.05]"
                        role="group"
                        aria-label="Stage actions"
                    >
                        {visibleActions.map((stage) => (
                            <button
                                key={stage}
                                type="button"
                                onClick={() => move(stage)}
                                disabled={moving}
                                data-testid={`pipeline-card-move-${item.id}-${stage}`}
                                aria-label={`Move to ${getStageLabel(stage)}`}
                                className="
                                    px-2.5 py-1 rounded-md
                                    text-[9px] tracking-[0.08em] uppercase
                                    text-black/60 hover:text-black/85
                                    bg-black/[0.04] hover:bg-black/[0.07]
                                    border border-black/[0.05] hover:border-black/[0.10]
                                    transition-all duration-100
                                    disabled:opacity-40
                                "
                            >
                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                            </button>
                        ))}
                        {overflowActions.length > 0 && (
                            <div
                                className="relative"
                                ref={overflowRef}
                            >
                                <button
                                    ref={moreButtonRef}
                                    type="button"
                                    onClick={toggleMoreMenu}
                                    aria-label="More actions"
                                    aria-expanded={showMoreActions}
                                    aria-haspopup="true"
                                    className="
                                        flex items-center justify-center
                                        w-5 h-5 rounded-md
                                        text-[10px] font-mono
                                        text-black/45 hover:text-black/70
                                        hover:bg-black/[0.04]
                                        transition-colors duration-100
                                        focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/20
                                    "
                                >
                                    ⋯
                                </button>
                                {showMoreActions && (
                                    <div 
                                        className="absolute bottom-full right-0 mb-1.5 z-20 
                                            bg-white
                                            border border-black/[0.08] 
                                            shadow-[0_8px_24px_-16px_rgba(0,0,0,0.12)]
                                            rounded-md py-1.5 min-w-[110px]"
                                        role="menu"
                                        aria-label="More stage actions"
                                    >
                                        {overflowActions.map((stage) => (
                                            <button
                                                key={stage}
                                                type="button"
                                                onClick={() => handleOverflowAction(stage)}
                                                className="
                                                    w-full text-left px-3 py-1.5
                                                    text-[9px] tracking-[0.08em] uppercase
                                                    text-black/65 hover:text-black/90 hover:bg-black/[0.02]
                                                    transition-colors duration-100
                                                "
                                                role="menuitem"
                                                aria-label={`Move to ${getStageLabel(stage)}`}
                                            >
                                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
});

export default PipelineCard;
