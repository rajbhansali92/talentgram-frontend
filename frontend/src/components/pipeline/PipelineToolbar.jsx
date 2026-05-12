import React, { memo } from "react";

const PipelineToolbar = memo(function PipelineToolbar({
    projectId,
    bulkMode,
    onToggleBulkMode,
    onOpenBulkAdd,
}) {
    return (
        <div className="mb-5 flex justify-between items-end flex-wrap gap-3">
            <div>
                <h1 className="text-xl font-medium tracking-tight text-white/90">
                    Casting Pipeline
                </h1>
                <p className="text-white/25 text-[10px] mt-1 font-mono">
                    {projectId}
                </p>
            </div>
            <div className="flex gap-2">
                <button
                    type="button"
                    onClick={onToggleBulkMode}
                    data-testid="pipeline-bulk-mode"
                    aria-pressed={bulkMode}
                    className={`
                        px-3 py-1.5 text-[10px] tracking-wide uppercase
                        rounded-md transition-all duration-200
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
                    className="
                        px-3 py-1.5 text-[10px] tracking-wide uppercase
                        border border-white/10 bg-white/5
                        text-white/50 hover:text-white/80 hover:bg-white/8
                        rounded-md transition-all duration-200
                    "
                >
                    + Bulk Import
                </button>
            </div>
        </div>
    );
});

export default PipelineToolbar;
