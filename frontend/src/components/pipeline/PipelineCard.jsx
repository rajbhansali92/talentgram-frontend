import React, {
    memo,
    useState,
    useEffect,
    useRef,
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

    // ISSUE 1 & 9: Click outside + ESC key to close overflow menu
    useEffect(() => {
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
            if (e.key === "Escape" && showMoreActions) {
                setShowMoreActions(false);
            }
        }

        document.addEventListener("mousedown", handleClickOutside);
        document.addEventListener("keydown", handleEsc);

        return () => {
            document.removeEventListener("mousedown", handleClickOutside);
            document.removeEventListener("keydown", handleEsc);
        };
    }, [showMoreActions]);

    const move = async (stage) => {
        setMoving(true);
        try {
            await adminApi.patch("/pipeline/move", {
                ids: [item.id],
                stage,
            });
            await refresh();
            // Close overflow menu after successful move
            setShowMoreActions(false);
        } catch (e) {
            console.error("Move failed:", e);
            toast.error(e?.response?.data?.detail || "Move failed");
        } finally {
            setMoving(false);
        }
    };

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

    const handleDragStart = (e) => {
        if (!draggable) return;
        e.dataTransfer.setData("text/plain", item.id);
        e.dataTransfer.effectAllowed = "move";
        setTimeout(() => onDragStart && onDragStart(item.id), 0);
    };

    const handleDragEnd = () => {
        if (!draggable) return;
        if (onDragEnd) onDragEnd();
    };

    const handleKeyDown = (e) => {
        // Accessibility: Enter or Space triggers selection in bulk mode
        if (bulkMode && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onToggleSelect(item.id);
        }
    };

    const shellClass = [
        "group relative rounded-md overflow-hidden",
        "transition-all duration-200",
        "bg-[#131313]",
        "border",
        "min-h-[112px]",
        isSelected
            ? "border-white/30 ring-1 ring-white/10"
            : "border-white/[0.05]",
        readOnly
            ? ""
            : "hover:border-white/10",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging
            ? "opacity-60 scale-[0.98]"
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
                className={`${shellClass} px-2 py-2 cursor-pointer`}
                role="checkbox"
                aria-checked={isSelected}
                tabIndex={0}
            >
                <div className="flex items-center gap-2">
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => onToggleSelect(item.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-3.5 h-3.5 rounded border-white/20 bg-transparent"
                        aria-label={`Select ${displayName}`}
                    />
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="sm"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="text-[11px] text-white/85 font-medium truncate">
                            {displayName}
                        </p>
                        {displayEmail && (
                            <p className="text-[8px] text-white/45 truncate font-mono mt-0.5">
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
            <div className="p-2.5 space-y-2">
                {/* Identity row */}
                <div className="flex items-start gap-2.5">
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="sm"
                    />
                    <div className="flex-1 min-w-0">
                        <p
                            className="text-[12px] text-white/85 font-medium truncate"
                            title={displayName}
                        >
                            {displayName}
                        </p>
                        {displayIg && (
                            <p className="text-[8px] text-white/55 truncate font-mono mt-0.5">
                                @{displayIg}
                            </p>
                        )}
                        {!displayIg && item.talent_name && (
                            <p className="text-[8px] text-white/35 truncate font-mono mt-0.5">
                                {item.talent_id?.slice(0, 8)}…
                            </p>
                        )}
                    </div>

                    {/* Status chip */}
                    {statusTone && (
                        <span
                            className={`shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm border ${statusTone.chip}`}
                            title={statusTone.label}
                            role="status"
                            aria-label={`Status: ${statusTone.label}`}
                        >
                            <span
                                className={`w-1 h-1 rounded-full ${statusTone.dot}`}
                                aria-hidden="true"
                            />
                            <span
                                className={`text-[7px] tracking-wide uppercase ${statusTone.text}`}
                            >
                                {statusTone.label}
                            </span>
                        </span>
                    )}
                </div>

                {/* Metadata - compact */}
                {(displayEmail || displayPhone) && (
                    <div className="space-y-0.5" aria-label="Contact information">
                        {displayEmail && (
                            <p className="text-[9px] text-white/55 truncate font-mono" title={displayEmail}>
                                {displayEmail}
                            </p>
                        )}
                        {displayPhone && (
                            <p className="text-[9px] text-white/55 truncate font-mono" title={displayPhone}>
                                {displayPhone}
                            </p>
                        )}
                    </div>
                )}

                {/* Action pills - max 2 visible */}
                {!readOnly && visibleActions.length > 0 && (
                    <div 
                        className="flex flex-wrap items-center gap-1 pt-1 border-t border-white/[0.03]"
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
                                    px-2 py-0.5 rounded-sm
                                    text-[8px] tracking-wide uppercase
                                    text-white/55 hover:text-white/80
                                    bg-white/[0.04] hover:bg-white/[0.07]
                                    border border-white/[0.03] hover:border-white/8
                                    transition-all duration-150
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
                                    onClick={() => setShowMoreActions(!showMoreActions)}
                                    aria-label="More actions"
                                    aria-expanded={showMoreActions}
                                    aria-haspopup="true"
                                    className="
                                        px-1.5 py-0.5 rounded-sm
                                        text-[8px] tracking-wide
                                        text-white/40 hover:text-white/60
                                        hover:bg-white/[0.04]
                                        transition-colors
                                    "
                                >
                                    ⋯
                                </button>
                                {showMoreActions && (
                                    <div 
                                        className="absolute bottom-full right-0 mb-1 z-20 bg-[#1a1a1a] border border-white/10 rounded-md shadow-lg py-1 min-w-[80px]"
                                        role="menu"
                                        aria-label="More stage actions"
                                    >
                                        {overflowActions.map((stage) => (
                                            <button
                                                key={stage}
                                                type="button"
                                                onClick={() => {
                                                    move(stage);
                                                    setShowMoreActions(false);
                                                }}
                                                className="
                                                    w-full text-left px-2 py-1
                                                    text-[8px] tracking-wide uppercase
                                                    text-white/60 hover:text-white/90 hover:bg-white/5
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
