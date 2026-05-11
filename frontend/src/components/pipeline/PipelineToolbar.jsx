import React, { memo } from "react";

/**
 * PipelineToolbar — slim header row with the two persistent entry points
 * for bulk workflows. Renders the page title + project ID on the left
 * and the "Bulk Select" toggle + "+ Bulk Add" CTA on the right.
 *
 * The heavy bulk-action surface lives in the floating BulkActionBar
 * (PATCH 4C) — this toolbar only carries persistent entry points.
 */
const PipelineToolbar = memo(function PipelineToolbar({
    projectId,
    bulkMode,
    onToggleBulkMode,
    onOpenBulkAdd,
}) {
    return (
        <div className="mb-4 flex justify-between items-start flex-wrap gap-3">
            <div>
                <h2 className="text-white font-semibold tracking-tight">
                    Casting Pipeline
                </h2>
                <p className="text-white/40 text-[11px] mt-1 tg-mono">
                    Project ID: {projectId}
                </p>
            </div>
            <div className="flex gap-2 flex-wrap">
                <button
                    type="button"
                    onClick={onToggleBulkMode}
                    data-testid="pipeline-bulk-mode"
                    aria-pressed={bulkMode}
                    className={`
                        px-3 py-1.5 text-[11px] tracking-[0.16em] uppercase
                        rounded-full border transition-all duration-200
                        ${
                            bulkMode
                                ? "border-white/30 bg-white/[0.08] text-white/90"
                                : "border-white/10 bg-white/[0.03] text-white/65 hover:text-white hover:border-white/20"
                        }
                    `}
                >
                    {bulkMode ? "Exit Select" : "Bulk Select"}
                </button>
                <button
                    type="button"
                    onClick={onOpenBulkAdd}
                    data-testid="pipeline-bulk-add-open"
                    className="
                        px-3 py-1.5 text-[11px] tracking-[0.16em] uppercase
                        rounded-full border border-white/10 bg-white/[0.03]
                        text-white/65 hover:text-white hover:border-white/20
                        transition-all duration-200
                    "
                >
                    + Bulk Add
                </button>
            </div>
        </div>
    );
});

export default PipelineToolbar;
