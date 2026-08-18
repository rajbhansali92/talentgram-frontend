import React from "react";
import { Upload, ChevronDown, X, Check, AlertCircle } from "lucide-react";
import { useUploadActivityModel } from "@/hooks/useUploadActivityModel";

// Phase 5 — Upload Activity Panel. A PURE renderer of `useUploadActivityModel`
// (hooks/useUploadActivityModel.js): every count, label, icon choice, and
// expand/collapse decision is already resolved by the model before it gets
// here. This component contains no derivation of its own — it only reads
// the model and wires `onRetry`/`onDismiss` (plain pass-through handlers,
// not presentation state) to the per-item buttons.
export default function FloatingUploadManager({ activeUploads = {}, completedCount = 0, onRetry, onDismiss }) {
    const {
        isVisible,
        items,
        summary,
        hasActive,
        justFinished,
        overallProgress,
        headline,
        expanded,
        toggleExpanded,
    } = useUploadActivityModel({ activeUploads, completedCount });

    if (!isVisible) return null;

    // Shared positioning: floats clear of whichever sticky footer is
    // actually rendered instead of overlapping it. `--tg-sticky-cta-h`
    // (the Step 4 Submit footer) and `--tg-wizard-nav-h` (the wizard's
    // Steps 1-3 Back/Next footer, SubmissionPage.jsx) are each published by
    // useStickyFooterHeightVar and are mutually exclusive — only one is
    // ever visually mounted at a time, so the hidden one's height is 0 and
    // summing them always yields just the active footer's height. The
    // corner (right + z-index) is identical for both render states so
    // reopening/minimizing never jumps sideways; `bottom` differs per
    // state (see the minimized branch below) and is applied separately.
    const anchorCornerClass = "fixed right-4 sm:right-6 z-50";
    const expandedBottomClass =
        "bottom-[calc(var(--tg-sticky-cta-h,0px)+var(--tg-wizard-nav-h,0px)+1rem)] sm:bottom-[calc(var(--tg-sticky-cta-h,0px)+var(--tg-wizard-nav-h,0px)+1.5rem)]";

    // Upload Manager UI fix (2026-08): minimized must mean minimized — a
    // small, fixed-footprint corner control, never a shrunk copy of the
    // full panel. The previous "collapsed" state still rendered the full
    // header + progress bar + status line at ~90vw wide on mobile, which is
    // exactly the "obstructing the submission screen" this fixes. This
    // control never covers form fields, the Media section, or Back/Next —
    // it's an 11x11 circle, nothing else — and tapping it is the only way
    // back to the full panel.
    //
    // Its own clearance, NOT the shared `anchorPositionClass` above: that
    // formula only clears the footer's own measured height, not the
    // trailing bottom padding the submission content wrapper reserves
    // below it (`pb-28 sm:pb-10` in SubmissionPage.jsx) — a `position:
    // sticky` element can never render into that padding, so in practice
    // the footer sits noticeably higher than "wizard-nav-h + 1rem" above
    // the viewport edge. The full panel already accepted that same gap as
    // "close enough" (unchanged here, per "keep current functionality"),
    // but a precise 44px circle sitting inside that gap reads as directly
    // on top of Next, not just close to it — so it gets its own larger,
    // padding-matched clearance instead of inheriting the panel's.
    if (!expanded) {
        const minimizedBottomClass =
            "bottom-[calc(var(--tg-sticky-cta-h,0px)+var(--tg-wizard-nav-h,0px)+7rem)] sm:bottom-[calc(var(--tg-sticky-cta-h,0px)+var(--tg-wizard-nav-h,0px)+3rem)]";
        // Solid brand-navy fill (not the panel's white/blur treatment) —
        // at full-panel size a faint white-on-white edge is enough to read
        // as "a card," but shrunk to a 44px circle it nearly disappeared
        // against the page's own white background in testing. A solid fill
        // is the small addition needed to keep a genuinely minimized
        // control genuinely visible/tappable, not just present in the DOM.
        return (
            <button
                type="button"
                onClick={toggleExpanded}
                data-testid="upload-activity-minimized"
                aria-label={`${headline} — tap to show upload status`}
                className={`${anchorCornerClass} ${minimizedBottomClass} w-11 h-11 rounded-full bg-[#0c2340] shadow-lg flex items-center justify-center transition-all duration-300 animate-in slide-in-from-bottom-5 active:scale-95`}
            >
                <div className="relative">
                    {justFinished ? (
                        <Check className="w-4 h-4 text-emerald-300" />
                    ) : summary.failedCount > 0 ? (
                        <AlertCircle className="w-4 h-4 text-rose-300" />
                    ) : (
                        <Upload className={`w-4 h-4 text-white ${hasActive ? "animate-pulse" : ""}`} />
                    )}
                    {hasActive && (
                        <span className="absolute -top-1 -right-1 flex h-2 w-2">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white/50 opacity-75"></span>
                            <span className="relative inline-flex rounded-full h-2 w-2 bg-white"></span>
                        </span>
                    )}
                </div>
            </button>
        );
    }

    return (
        <div
            data-testid="upload-activity-panel"
            className={`${anchorCornerClass} ${expandedBottomClass} w-[calc(100vw-2rem)] max-w-xs sm:w-80 bg-white/90 backdrop-blur-md rounded-2xl shadow-2xl border border-[#eaeaea]/60 p-4 transition-all duration-300 animate-in slide-in-from-bottom-5`}
        >
            <div className="flex items-center justify-between cursor-pointer" onClick={toggleExpanded}>
                <div className="flex items-center gap-2 min-w-0">
                    <div className="relative shrink-0">
                        {justFinished ? (
                            <Check className="w-4 h-4 text-emerald-600" />
                        ) : summary.failedCount > 0 ? (
                            <AlertCircle className="w-4 h-4 text-rose-500" />
                        ) : (
                            <Upload className={`w-4 h-4 text-[#0c2340] ${hasActive ? "animate-pulse" : ""}`} />
                        )}
                        {hasActive && (
                            <span className="absolute -top-1 -right-1 flex h-2 w-2">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#0c2340]/40 opacity-75"></span>
                                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#0c2340]"></span>
                            </span>
                        )}
                    </div>
                    <span
                        data-testid="upload-activity-headline"
                        className="font-semibold text-xs text-[#111111] font-mono tracking-wider uppercase truncate"
                    >
                        {headline}
                    </span>
                </div>
                <button
                    type="button"
                    data-testid="upload-activity-toggle"
                    className="text-[#333333] hover:text-[#222222] p-1 shrink-0"
                >
                    <ChevronDown className="w-4 h-4 transform transition-transform duration-200" />
                </button>
            </div>

            {hasActive && (
                <div className="mt-3 h-1 w-full bg-slate-100 rounded-full overflow-hidden">
                    <div
                        className="h-full bg-[#0c2340] transition-all duration-300"
                        style={{ width: `${overallProgress}%` }}
                    />
                </div>
            )}

            {/* Always-visible summary (Phase 5 requirement #1) — collapsing
                the panel hides the per-item list below, never this. */}
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono">
                <span data-testid="upload-activity-stat-completed" className="font-semibold text-emerald-700">
                    {summary.completedCount} Completed
                </span>
                {summary.uploadingCount > 0 && (
                    <span data-testid="upload-activity-stat-uploading" className="font-semibold text-[#0c2340]">
                        {summary.uploadingCount} Uploading
                    </span>
                )}
                {summary.failedCount > 0 && (
                    <span data-testid="upload-activity-stat-failed" className="font-semibold text-rose-600">
                        {summary.failedCount} Failed
                    </span>
                )}
            </div>

            {items.length > 0 && (
                <div className="space-y-3 max-h-60 overflow-y-auto pr-1 mt-3 pt-3 border-t border-slate-100">
                    {items.map((item) => (
                        <div key={item.key} className="text-xs bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/80">
                            <div className="flex items-center justify-between mb-1.5">
                                <span className="font-medium text-[#111111] truncate max-w-[160px]" title={item.label}>
                                    {item.label}
                                </span>
                                <div className="flex items-center gap-1">
                                    <span className={`font-mono text-[10px] font-semibold ${item.textClass}`}>
                                        {item.displayText}
                                    </span>
                                    {(item.status === "completed" || item.status === "failed") && (
                                        <button
                                            type="button"
                                            onClick={() => onDismiss(item.key)}
                                            className="text-[#333333] hover:text-[#222222] p-0.5"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    )}
                                    {/* True cancel (item 10) — same onDismiss handler; while
                                        still active it aborts the in-flight upload instead of
                                        just hiding a terminal entry (see dismissUpload() in
                                        UploadManagerContext.jsx). */}
                                    {item.status !== "completed" && item.status !== "failed" && (
                                        <button
                                            type="button"
                                            onClick={() => onDismiss(item.key)}
                                            className="text-[#333333] hover:text-rose-600 p-0.5"
                                            title="Cancel upload"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    )}
                                </div>
                            </div>

                            {item.status === "failed" && (
                                <div className="flex items-center justify-between mt-1 gap-2">
                                    <span className="text-[10px] text-rose-500 truncate max-w-[150px] font-mono">Couldn't send this file.</span>
                                    <button
                                        type="button"
                                        onClick={() => onRetry(item.key)}
                                        className="text-[10px] font-semibold text-rose-600 hover:bg-rose-50 border border-rose-200 px-2 py-0.5 rounded-full bg-white active:scale-95 transition-all"
                                    >
                                        Tap to retry
                                    </button>
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
