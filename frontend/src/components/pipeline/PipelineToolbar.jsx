import React, { memo, useState, useCallback } from "react";

/**
 * PipelineToolbar — Header section for the pipeline board.
 * 
 * ISSUE 8 FIX: Truncated raw UUID for cleaner display
 * Features:
 *   • Truncated project ID (first 8 chars + ellipsis)
 *   • Copy-to-clipboard functionality for full ID
 *   • Accessible tooltip with full ID
 *   • Responsive layout for mobile
 *   • Clear visual hierarchy
 *   • No runtime style injection (styles must be in global CSS)
 */

const PipelineToolbar = memo(function PipelineToolbar({
    projectId,
    projectName, // Optional: can be passed from parent for better UX
    bulkMode,
    onToggleBulkMode,
    onOpenBulkAdd,
}) {
    const [copyFeedback, setCopyFeedback] = useState(false);
    const [copyError, setCopyError] = useState(false);

    // Handle copy to clipboard with error handling
    const handleCopyId = useCallback(async () => {
        if (!projectId) return;
        
        try {
            await navigator.clipboard.writeText(projectId);
            setCopyFeedback(true);
            setCopyError(false);
            setTimeout(() => setCopyFeedback(false), 2000);
        } catch (err) {
            console.error("Failed to copy:", err);
            setCopyError(true);
            setTimeout(() => setCopyError(false), 2000);
            
            // Fallback for older browsers
            try {
                const textArea = document.createElement("textarea");
                textArea.value = projectId;
                document.body.appendChild(textArea);
                textArea.select();
                document.execCommand("copy");
                document.body.removeChild(textArea);
                setCopyFeedback(true);
                setCopyError(false);
            } catch (fallbackErr) {
                console.error("Fallback copy failed:", fallbackErr);
            }
        }
    }, [projectId]);

    // Format display ID: use projectName if available, else truncate UUID
    const displayProjectName = projectName || null;
    const truncatedId = projectId ? `${projectId.slice(0, 8)}...` : "";
    const fullId = projectId || "";

    return (
        <div className="mb-5 flex justify-between items-end flex-wrap gap-3">
            {/* Left section: Title and project info */}
            <div>
                <h1 className="text-xl font-medium tracking-tight text-white/90">
                    Casting Pipeline
                </h1>
                
                {/* Project metadata with copy capability */}
                <div className="flex items-center gap-2 mt-1">
                    {displayProjectName ? (
                        // If project name is available, show name + truncated ID
                        <>
                            <span className="text-white/40 text-[11px] font-medium">
                                {displayProjectName}
                            </span>
                            <span className="text-white/20 text-[9px] font-mono">
                                •
                            </span>
                            <button
                                type="button"
                                onClick={handleCopyId}
                                className="group flex items-center gap-1 text-white/25 hover:text-white/50 transition-colors duration-200 focus:outline-none focus:ring-1 focus:ring-white/20 rounded"
                                title={`Copy full ID: ${fullId}`}
                                aria-label="Copy project ID to clipboard"
                            >
                                <span className="text-[9px] font-mono">
                                    {truncatedId}
                                </span>
                                <svg
                                    className="w-2.5 h-2.5 opacity-0 group-hover:opacity-100 transition-opacity duration-200"
                                    fill="none"
                                    stroke="currentColor"
                                    viewBox="0 0 24 24"
                                    aria-hidden="true"
                                >
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                                </svg>
                            </button>
                            {copyFeedback && (
                                <span className="text-[8px] text-emerald-400/60 animate-fade-in" role="status">
                                    Copied!
                                </span>
                            )}
                            {copyError && (
                                <span className="text-[8px] text-rose-400/60 animate-fade-in" role="alert">
                                    Copy failed
                                </span>
                            )}
                        </>
                    ) : (
                        // Fallback: just show truncated ID with copy
                        <button
                            type="button"
                            onClick={handleCopyId}
                            className="group flex items-center gap-1 text-white/25 hover:text-white/50 transition-colors duration-200 focus:outline-none focus:ring-1 focus:ring-white/20 rounded"
                            title={`Copy full ID: ${fullId}`}
                            aria-label="Copy project ID to clipboard"
                        >
                            <span className="text-[9px] font-mono">
                                ID: {truncatedId}
                            </span>
                            <svg
                                className="w-2.5 h-2.5 opacity-0 group-hover:opacity-100 transition-opacity duration-200"
                                fill="none"
                                stroke="currentColor"
                                viewBox="0 0 24 24"
                                aria-hidden="true"
                            >
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                            </svg>
                            {copyFeedback && (
                                <span className="text-[8px] text-emerald-400/60 ml-1 animate-fade-in" role="status">
                                    Copied!
                                </span>
                            )}
                            {copyError && (
                                <span className="text-[8px] text-rose-400/60 ml-1 animate-fade-in" role="alert">
                                    Failed
                                </span>
                            )}
                        </button>
                    )}
                </div>
            </div>

            {/* Right section: Action buttons */}
            <div className="flex gap-2">
                <button
                    type="button"
                    onClick={onToggleBulkMode}
                    data-testid="pipeline-bulk-mode"
                    aria-pressed={bulkMode}
                    aria-label={bulkMode ? "Exit bulk selection mode" : "Enter bulk selection mode"}
                    className={`
                        px-3 py-1.5 text-[10px] tracking-wide uppercase
                        rounded-md transition-all duration-200
                        focus:outline-none focus:ring-1 focus:ring-white/20
                        ${
                            bulkMode
                                ? "bg-white/10 text-white border border-white/15"
                                : "bg-transparent text-white/50 border border-white/10 hover:bg-white/5 hover:text-white/70"
                        }
                    `}
                >
                    {bulkMode ? "Exit Select" : "Select Mode"}
                </button>
                
                <button
                    type="button"
                    onClick={onOpenBulkAdd}
                    data-testid="pipeline-bulk-add-open"
                    aria-label="Open bulk import modal"
                    className="
                        px-3 py-1.5 text-[10px] tracking-wide uppercase
                        border border-white/10 bg-white/5
                        text-white/50 hover:text-white/80 hover:bg-white/8
                        rounded-md transition-all duration-200
                        focus:outline-none focus:ring-1 focus:ring-white/20
                    "
                >
                    + Bulk Import
                </button>
            </div>
        </div>
    );
});

export default PipelineToolbar;
