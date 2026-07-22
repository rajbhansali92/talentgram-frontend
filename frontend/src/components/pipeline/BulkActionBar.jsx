import React, { memo, useState, useRef, useEffect } from "react";
import { STAGE_LABELS, getStageLabel } from "./constants";
import { 
    ChevronDown, 
    Tag, 
    FileText, 
    MessageCircle, 
    Mail, 
    Download, 
    Archive, 
    Trash2, 
    X,
    FolderOpen,
    Check
} from "lucide-react";

/**
 * BulkActionBar
 *
 * Rich floating cinematic action bar anchored to the bottom-center of the viewport.
 * Disappears when selection is empty.
 */
const BulkActionBar = memo(function BulkActionBar({
    count,
    onClear,
    onMove,
    onLabel,
    onNote,
    onDelete,
    onExport,
    onWhatsApp,
    onEmail,
    onArchive,
    onReachedOut,
    showReachedOut,
}) {
    const visible = count > 0;
    const [busy, setBusy] = useState(false);
    const [showMoveDropdown, setShowMoveDropdown] = useState(false);
    const dropdownRef = useRef(null);

    // Click outside to close stage dropdown
    useEffect(() => {
        if (!showMoveDropdown) return;
        const handleClickOutside = (e) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
                setShowMoveDropdown(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, [showMoveDropdown]);

    const handleMove = async (stage) => {
        if (busy) return;
        setBusy(true);
        try {
            await onMove(stage);
            setShowMoveDropdown(false);
        } finally {
            setBusy(false);
        }
    };

    if (!visible) return null;

    const stagesToMove = [
        "ask_to_test",
        "approved",
        "hold",
        "shortlisted",
        "already_tested",
        "locked",
        "rejected",
        "not_available",
        "not_interested"
    ];

    return (
        <div
            data-testid="pipeline-bulk-bar"
            className="fixed z-40 left-1/2 -translate-x-1/2 bottom-5 w-[min(94vw,880px)] animate-in fade-in slide-in-from-bottom-4 duration-200"
        >
            <div
                className="
                    flex items-center gap-2 sm:gap-3
                    px-4 py-3 sm:px-5 sm:py-3
                    rounded-2xl
                    bg-black/90 backdrop-blur-2xl
                    border border-white/10
                    shadow-[0_24px_64px_-16px_rgba(0,0,0,0.85),inset_0_1px_0_0_rgba(255,255,255,0.05)]
                "
            >
                {/* Count indicator */}
                <div className="shrink-0 flex flex-col justify-center">
                    <span className="text-[14px] font-bold text-white tracking-tight tg-mono">
                        {count} Selected
                    </span>
                    <span className="text-[9px] text-white/40 tracking-wider uppercase font-semibold">
                        Casting Pool
                    </span>
                </div>

                <div className="w-px h-8 bg-white/15 mx-1" />

                {/* Actions group - scrollable on small viewports */}
                <div className="flex-1 flex items-center gap-2 overflow-x-auto scrollbar-none py-0.5">
                    {/* Reached Out — only ever shown when every selected talent
                        is currently in Ask To Test. Moves them all straight to
                        Follow-Up in one click (no dropdown, no confirmation). */}
                    {showReachedOut && (
                        <button
                            type="button"
                            onClick={onReachedOut}
                            disabled={busy}
                            data-testid="pipeline-bulk-reached-out"
                            className="
                                flex items-center gap-1.5 shrink-0
                                px-3 py-2 rounded-lg
                                text-[10.5px] tracking-wide uppercase font-semibold
                                text-white bg-emerald-500/15 hover:bg-emerald-500/25
                                border border-emerald-400/25 hover:border-emerald-400/40
                                transition-all duration-200 disabled:opacity-40
                            "
                        >
                            <Check className="w-3.5 h-3.5 text-emerald-400" />
                            <span>Reached Out</span>
                        </button>
                    )}

                    {/* Stage quick moves */}
                    <div className="relative" ref={dropdownRef}>
                        <button
                            type="button"
                            onClick={() => setShowMoveDropdown(prev => !prev)}
                            disabled={busy}
                            className="
                                flex items-center gap-1.5 shrink-0
                                px-3 py-2 rounded-lg
                                text-[10.5px] tracking-wide uppercase font-semibold
                                text-white bg-white/10 hover:bg-white/15
                                border border-white/10 hover:border-white/20
                                transition-all duration-200 disabled:opacity-40
                            "
                        >
                            <FolderOpen className="w-3.5 h-3.5" />
                            <span>Move Stage</span>
                            <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${showMoveDropdown ? "rotate-180" : ""}`} />
                        </button>
                        {showMoveDropdown && (
                            <div className="absolute bottom-full left-0 mb-2 z-50 bg-[#121212] border border-white/10 shadow-2xl rounded-xl py-1.5 min-w-[160px] flex flex-col gap-0.5">
                                <div className="px-3 py-1 text-[9px] font-bold text-white/45 tracking-wider uppercase border-b border-white/5 mb-1.5">
                                    Move {count} talents to
                                </div>
                                {stagesToMove.map((stage) => (
                                    <button
                                        key={stage}
                                        type="button"
                                        onClick={() => handleMove(stage)}
                                        className="w-full text-left px-3 py-2 text-[10.5px] text-white/75 hover:bg-white/10 hover:text-white transition-colors"
                                    >
                                        {STAGE_LABELS[stage] || getStageLabel(stage)}
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Labels */}
                    <button
                        type="button"
                        onClick={onLabel}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-white bg-white/5 hover:bg-white/10
                            border border-white/5 hover:border-white/10
                            transition-all duration-200
                        "
                    >
                        <Tag className="w-3.5 h-3.5 opacity-60" />
                        <span>Labels</span>
                    </button>

                    {/* Notes */}
                    <button
                        type="button"
                        onClick={onNote}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-white bg-white/5 hover:bg-white/10
                            border border-white/5 hover:border-white/10
                            transition-all duration-200
                        "
                    >
                        <FileText className="w-3.5 h-3.5 opacity-60" />
                        <span>Note</span>
                    </button>

                    {/* WhatsApp */}
                    <button
                        type="button"
                        onClick={onWhatsApp}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-white bg-white/5 hover:bg-[#25D366]/10 hover:text-[#25D366]
                            border border-white/5 hover:border-[#25D366]/20
                            transition-all duration-200
                        "
                    >
                        <MessageCircle className="w-3.5 h-3.5 text-[#25D366]" />
                        <span>WhatsApp</span>
                    </button>

                    {/* Email */}
                    <button
                        type="button"
                        onClick={onEmail}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-white bg-white/5 hover:bg-sky-500/10 hover:text-sky-400
                            border border-white/5 hover:border-sky-500/20
                            transition-all duration-200
                        "
                    >
                        <Mail className="w-3.5 h-3.5 text-sky-400" />
                        <span>Email</span>
                    </button>

                    {/* Export */}
                    <button
                        type="button"
                        onClick={onExport}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-white bg-white/5 hover:bg-white/10
                            border border-white/5 hover:border-white/10
                            transition-all duration-200
                        "
                    >
                        <Download className="w-3.5 h-3.5 opacity-60" />
                        <span>Export</span>
                    </button>

                    <div className="w-px h-5 bg-white/10 mx-1 shrink-0" />

                    {/* Archive */}
                    <button
                        type="button"
                        onClick={onArchive}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-amber-400 hover:text-amber-300 bg-amber-500/10 hover:bg-amber-500/15
                            border border-amber-500/20
                            transition-all duration-200
                        "
                    >
                        <Archive className="w-3.5 h-3.5" />
                        <span>Archive</span>
                    </button>

                    {/* Delete */}
                    <button
                        type="button"
                        onClick={onDelete}
                        disabled={busy}
                        className="
                            flex items-center gap-1.5 shrink-0
                            px-3 py-2 rounded-lg
                            text-[10.5px] tracking-wide uppercase font-semibold
                            text-rose-400 hover:text-rose-300 bg-rose-500/10 hover:bg-rose-500/15
                            border border-rose-500/20
                            transition-all duration-200
                        "
                    >
                        <Trash2 className="w-3.5 h-3.5" />
                        <span>Delete</span>
                    </button>
                </div>

                <div className="w-px h-8 bg-white/15 mx-1" />

                {/* Close selection */}
                <button
                    type="button"
                    onClick={onClear}
                    title="Clear selection (ESC)"
                    className="
                        shrink-0
                        w-8 h-8 rounded-lg
                        flex items-center justify-center
                        text-white/55 hover:text-white hover:bg-white/10
                        border border-white/10
                        transition-all duration-200
                    "
                >
                    <X className="w-4 h-4" />
                </button>
            </div>
        </div>
    );
});

export default BulkActionBar;
