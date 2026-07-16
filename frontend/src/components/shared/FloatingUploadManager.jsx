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

    return (
        <div
            data-testid="upload-activity-panel"
            // Floats clear of the page's sticky submit-CTA footer instead of
            // overlapping it. `--tg-sticky-cta-h` is the footer's live rendered
            // height (published by useStickyFooterHeightVar), so this stays
            // correct regardless of footer height, safe-area insets, or iOS
            // toolbar resize. Falls back to the original fixed gap on pages
            // with no sticky footer (the var is simply unset there).
            className="fixed bottom-[calc(var(--tg-sticky-cta-h,0px)+1rem)] right-4 sm:bottom-[calc(var(--tg-sticky-cta-h,0px)+1.5rem)] sm:right-6 z-50 w-[calc(100vw-2rem)] max-w-xs sm:w-80 bg-white/90 backdrop-blur-md rounded-2xl shadow-2xl border border-[#eaeaea]/60 p-4 transition-all duration-300 animate-in slide-in-from-bottom-5"
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
                    <ChevronDown className={`w-4 h-4 transform transition-transform duration-200 ${!expanded ? "rotate-180" : ""}`} />
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

            {expanded && items.length > 0 && (
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
                                    <button
                                        type="button"
                                        onClick={() => onDismiss(item.key)}
                                        className="text-[#333333] hover:text-[#222222] p-0.5"
                                    >
                                        <X className="w-3 h-3" />
                                    </button>
                                </div>
                            </div>

                            {item.status === "failed" ? (
                                <div className="flex items-center justify-between mt-1 gap-2">
                                    <span className="text-[10px] text-rose-500 truncate max-w-[150px] font-mono">{item.error || "Upload failed"}</span>
                                    <button
                                        type="button"
                                        onClick={() => onRetry(item.key)}
                                        className="text-[10px] font-semibold text-rose-600 hover:bg-rose-50 border border-rose-200 px-2 py-0.5 rounded-full bg-white active:scale-95 transition-all"
                                    >
                                        Retry
                                    </button>
                                </div>
                            ) : (
                                <div className="w-full bg-slate-100 rounded-full h-1 overflow-hidden">
                                    <div
                                        className={`h-full bg-[#0c2340] transition-all duration-300 ${item.status === "completed" ? "bg-emerald-500" : item.status === "processing" ? "bg-emerald-400 animate-pulse" : item.status === "compressing" ? "bg-blue-500 animate-pulse" : ""}`}
                                        style={{ width: `${item.pct}%` }}
                                    />
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
