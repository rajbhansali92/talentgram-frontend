import React from "react";

/**
 * BulkAddModal — paste-IDs modal for bulk-adding talents to the pipeline.
 * Stateless; the parent owns the textarea value and the busy flag.
 */
function BulkAddModal({
    value,
    onChange,
    busy,
    onCancel,
    onSubmit,
}) {
    return (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50">
            <div className="bg-gray-900 border border-white/20 rounded-lg p-6 w-full max-w-lg">
                <h3 className="text-white text-lg mb-4">Bulk Add Talents</h3>
                <p className="text-white/40 text-sm mb-3">
                    Enter talent IDs (one per line or comma-separated)
                </p>
                <textarea
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder={"tal_12345\ntal_67890\ntal_11111"}
                    data-testid="pipeline-bulk-input"
                    className="w-full h-40 bg-black/50 border border-white/20 rounded p-2 text-white mb-4 font-mono text-sm"
                    disabled={busy}
                />
                <div className="text-white/40 text-xs mb-4">
                    Supports UUIDs, custom IDs, or numeric IDs
                </div>
                <div className="flex gap-2 justify-end">
                    <button
                        onClick={onCancel}
                        disabled={busy}
                        className="px-4 py-2 bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onSubmit}
                        disabled={busy}
                        data-testid="pipeline-bulk-add-submit"
                        className="px-4 py-2 bg-green-500 hover:bg-green-600 text-white rounded transition-colors"
                    >
                        {busy ? "Adding…" : "Add Talents"}
                    </button>
                </div>
            </div>
        </div>
    );
}

export default BulkAddModal;
