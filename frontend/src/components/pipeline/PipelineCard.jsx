import React, { memo, useState } from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";
import TalentAvatar from "./TalentAvatar";
import {
    NEXT_STAGE_FLOW,
    STAGE_LABELS,
    STATUS_TONES,
    getStageLabel,
    normaliseStage,
} from "./constants";

/**
 * PipelineCard — single talent card.
 *
 * Renders two modes:
 *   • BULK MODE — compact row, checkbox + small avatar + name + email.
 *     No actions, no metadata. Designed for fast multi-select scanning.
 *   • NORMAL MODE — premium cinematic card with three zones:
 *       Top    — avatar + name + instagram + optional status chip
 *       Middle — email + phone metadata rows
 *       Bottom — action pills (suppressed in readOnly mode)
 *
 * Drag & Drop (PATCH 4D) — card = draggable source. Disabled when:
 *   • pointer-coarse device (touch) — `dragSupported` is false
 *   • read-only lane (follow-up) — drag is meaningless here
 *   • bulk mode is active — drag would conflict with multi-select
 */
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

    const move = async (stage) => {
        setMoving(true);
        try {
            await adminApi.patch("/pipeline/move", {
                ids: [item.id],
                stage,
            });
            await refresh();
        } catch (e) {
            console.error("Move failed:", e);
            toast.error(e?.response?.data?.detail || "Move failed");
        } finally {
            setMoving(false);
        }
    };

    // Legacy `sent` rows render as `approved` so action buttons match the
    // column the card sits in, and terminal/locked rows expose no onward
    // transitions.
    const canonicalStage = normaliseStage(item.stage);
    const nextStages = NEXT_STAGE_FLOW[canonicalStage] || [];
    const statusTone = STATUS_TONES[canonicalStage];

    // Display fields with sensible fallbacks. `talent_email` is the new
    // hydrated field (Patch 2); `email` is the legacy pre-hydration alias.
    const displayName = item.talent_name || item.talent_id || "Unknown";
    const displayEmail = item.talent_email || item.email || null;
    const displayPhone = item.talent_phone || null;
    const displayIg = item.instagram_handle || null;

    const draggable = dragSupported && !readOnly && !bulkMode;

    const handleDragStart = (e) => {
        if (!draggable) return;
        // text/plain so any drop target — including outside the app — can
        // read the id without us having to guess MIME types.
        e.dataTransfer.setData("text/plain", item.id);
        e.dataTransfer.effectAllowed = "move";
        // Notify parent so all columns can react to the drag context.
        // Wrap in a microtask so the drag image is already snapshotted
        // before the visual state changes (otherwise the ghost image
        // shows the half-faded card).
        setTimeout(() => onDragStart && onDragStart(item.id), 0);
    };

    const handleDragEnd = () => {
        if (!draggable) return;
        if (onDragEnd) onDragEnd();
    };

    // Cinematic shell — layered charcoal card with soft elevation.
    // Follow-up (readOnly) cards stay quieter: no hover lift, dimmer surface.
    // During drag: slight scale-down + opacity dim + elevated shadow.
    const shellClass = [
        "group relative rounded-xl overflow-hidden",
        "transition-all duration-300",
        "bg-[#131313]",
        "shadow-[0_4px_18px_rgba(0,0,0,0.22)]",
        "border border-white/[0.05]", // Refined border
        compact ? "min-h-[96px]" : "min-h-[132px]", // Density fix
        isSelected
            ? "border-white/40 ring-1 ring-white/20"
            : "border-white/[0.05]",
        readOnly
            ? ""
            : "hover:border-white/12 hover:-translate-y-[1px] hover:shadow-[0_8px_24px_-8px_rgba(0,0,0,0.4)]",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging
            ? "opacity-60 scale-[0.97] ring-1 ring-white/15 shadow-[0_12px_28px_-10px_rgba(0,0,0,0.5)]"
            : "",
        draggable ? "cursor-grab active:cursor-grabbing" : "",
    ].join(" ");

    if (bulkMode) {
        return (
            <div
                data-testid={`pipeline-card-${item.id}`}
                onClick={() => onToggleSelect(item.id)}
                draggable={draggable}
                onDragStart={handleDragStart}
                onDragEnd={handleDragEnd}
                className={`${shellClass} px-3 py-2 cursor-pointer`}
            >
                <div className="flex items-center gap-2.5">
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => onToggleSelect(item.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-4 h-4 accent-white/80 shrink-0"
                    />
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="sm"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="text-[13px] text-white/88 font-medium truncate leading-tight">
                            {displayName}
                        </p>
                        {displayEmail && (
                            <p className="text-[10px] text-white/60 truncate tg-mono mt-0.5">
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
        >
            {/* Subtle inner accent stripe that lights up on hover. */}
            <div
                aria-hidden
                className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/8 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none"
            />

            <div className="p-2.5 space-y-2">
                {/* TOP — identity */}
                <div className="flex items-start gap-3">
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="md"
                    />
                    <div className="flex-1 min-w-0">
                        <p
                            className="text-[13px] text-white/88 font-medium truncate leading-tight"
                            title={displayName}
                        >
                            {displayName}
                        </p>
                        {displayIg && (
                            <p className="text-[10px] text-white/72 truncate tg-mono mt-0.5">
                                {displayIg}
                            </p>
                        )}
                        {!displayIg && item.talent_name && (
                            <p
                                className="text-[10px] text-white/60 truncate tg-mono mt-0.5"
                                title={item.talent_id}
                            >
                                {item.talent_id?.slice(0, 8)}…
                            </p>
                        )}
                    </div>

                    {/* Status chip — only on terminal/locked lanes. */}
                    {statusTone && (
                        <span
                            className={`shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border ${statusTone.chip}`}
                            title={statusTone.label}
                        >
                            <span
                                className={`w-1 h-1 rounded-full ${statusTone.dot}`}
                            />
                            <span
                                className={`text-[9px] tracking-[0.14em] uppercase ${statusTone.text}`}
                            >
                                {statusTone.label}
                            </span>
                        </span>
                    )}
                </div>

                {/* MIDDLE — metadata. */}
                {(displayEmail || displayPhone) && (
                    <div className="space-y-0.5 pt-0.5">
                        {displayEmail && (
                            <p className="text-[10.5px] text-white/72 truncate tg-mono">
                                {displayEmail}
                            </p>
                        )}
                        {displayPhone && (
                            <p className="text-[10.5px] text-white/72 truncate tg-mono">
                                {displayPhone}
                            </p>
                        )}
                    </div>
                )}

                {/* BOTTOM — action pills. */}
                {!readOnly && nextStages.length > 0 && (
                    <div className="flex flex-wrap gap-1 pt-1.5 border-t border-white/[0.04]">
                        {nextStages.map((stage) => (
                            <button
                                key={stage}
                                type="button"
                                onClick={() => move(stage)}
                                disabled={moving}
                                data-testid={`pipeline-card-move-${item.id}-${stage}`}
                                title={`Move to ${getStageLabel(stage)}`}
                                className="
                                    px-2 py-1 rounded-full
                                    text-[9.5px] tracking-[0.12em] uppercase
                                    text-white/75 hover:text-white
                                    bg-white/[0.06] hover:bg-white/[0.12]
                                    border border-white/[0.05] hover:border-white/12
                                    transition-all duration-200
                                    hover:shadow-[0_0_0_3px_rgba(255,255,255,0.02)]
                                    disabled:opacity-40 disabled:cursor-not-allowed
                                "
                            >
                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                            </button>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
});

export default PipelineCard;
