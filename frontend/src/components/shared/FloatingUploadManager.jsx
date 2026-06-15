import React, { useState } from "react";
import { Upload, ChevronDown, X } from "lucide-react";

export default function FloatingUploadManager({ activeUploads = {}, onRetry, onDismiss }) {
    const items = Object.entries(activeUploads);
    const [collapsed, setCollapsed] = useState(false);

    if (items.length === 0) return null;

    const activeCount = items.filter(([_, u]) => u.status === "uploading" || u.status === "processing").length;
    const failedCount = items.filter(([_, u]) => u.status === "failed").length;

    return (
        <div className="fixed bottom-4 right-4 sm:bottom-6 sm:right-6 z-50 w-[calc(100vw-2rem)] max-w-xs sm:w-80 bg-white/90 backdrop-blur-md rounded-2xl shadow-2xl border border-[#eaeaea]/60 p-4 transition-all duration-300 animate-in slide-in-from-bottom-5">
            <div className="flex items-center justify-between border-b border-slate-100 pb-2 mb-3 cursor-pointer" onClick={() => setCollapsed(!collapsed)}>
                <div className="flex items-center gap-2">
                    <div className="relative">
                        <Upload className="w-4 h-4 text-[#0c2340] animate-pulse" />
                        {activeCount > 0 && (
                            <span className="absolute -top-1 -right-1 flex h-2 w-2">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#0c2340]/40 opacity-75"></span>
                                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#0c2340]"></span>
                            </span>
                        )}
                    </div>
                    <span className="font-semibold text-xs text-[#111111] font-mono tracking-wider uppercase">
                        Uploads ({items.length})
                    </span>
                </div>
                <div className="flex items-center gap-1.5">
                    {failedCount > 0 && (
                        <span className="text-[10px] font-mono font-bold text-rose-500 bg-rose-50 px-1.5 py-0.5 rounded-md border border-rose-100 animate-pulse">
                            {failedCount} Failed
                        </span>
                    )}
                    <button
                        type="button"
                        className="text-[#333333] hover:text-[#222222] p-1"
                    >
                        <ChevronDown className={`w-4 h-4 transform transition-transform duration-200 ${collapsed ? "rotate-180" : ""}`} />
                    </button>
                </div>
            </div>

            {!collapsed && (
                <div className="space-y-3 max-h-60 overflow-y-auto pr-1">
                    {items.map(([key, u]) => {
                        const cleanLabel = u.category === "intro_video" ? "Intro Video" : (u.category === "take" ? u.label : `${u.category === "image" ? "Portfolio" : u.category === "indian" ? "Indian" : "Western"}: ${u.fileName}`);
                        
                        return (
                            <div key={key} className="text-xs bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/80">
                                <div className="flex items-center justify-between mb-1.5">
                                    <span className="font-medium text-[#111111] truncate max-w-[160px]" title={cleanLabel}>
                                        {cleanLabel}
                                    </span>
                                    <div className="flex items-center gap-1">
                                        <span className={`font-mono text-[10px] font-semibold ${u.status === "failed" ? "text-rose-500" : u.status === "completed" ? "text-emerald-600" : "text-[#0c2340]"}`}>
                                            {u.status === "uploading" ? `${u.pct}%` : u.status === "processing" ? "Processing" : u.status === "completed" ? "Done" : "Failed"}
                                        </span>
                                        <button
                                            type="button"
                                            onClick={() => onDismiss(key)}
                                            className="text-[#333333] hover:text-[#222222] p-0.5"
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    </div>
                                </div>

                                {u.status === "failed" ? (
                                    <div className="flex items-center justify-between mt-1 gap-2">
                                        <span className="text-[10px] text-rose-500 truncate max-w-[150px] font-mono">{u.error || "Upload failed"}</span>
                                        <button
                                            type="button"
                                            onClick={() => onRetry(key)}
                                            className="text-[10px] font-semibold text-rose-600 hover:bg-rose-50 border border-rose-200 px-2 py-0.5 rounded-full bg-white active:scale-95 transition-all"
                                        >
                                            Retry
                                        </button>
                                    </div>
                                ) : (
                                    <div className="w-full bg-slate-100 rounded-full h-1 overflow-hidden">
                                        <div
                                            className={`h-full bg-[#0c2340] transition-all duration-300 ${u.status === "completed" ? "bg-emerald-500" : u.status === "processing" ? "bg-emerald-400 animate-pulse" : ""}`}
                                            style={{ width: `${u.pct}%` }}
                                        />
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
