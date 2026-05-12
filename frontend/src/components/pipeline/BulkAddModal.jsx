import React from "react";

function BulkAddModal({
    value,
    onChange,
    busy,
    onCancel,
    onSubmit,
}) {
    return (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50">
            <div className="bg-[#151515] border border-white/10 rounded-lg p-5 w-full max-w-md shadow-xl">
                <h3 className="text-white text-sm font-medium mb-1">
                    Bulk Import Talents
                </h3>
                <p className="text-white/30 text-[10px] mb-3">
                    Enter talent IDs (one per line or comma-separated)
                </p>
                <textarea
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder="tal_12345&#10;tal_67890&#10;tal_11111"
                    data-testid="pipeline-bulk-input"
                    className="
                        w-full h-32 
                        bg-black/50 border border-white/10 rounded-md 
                        p-2 text-white mb-3 font-mono text-xs
                        focus:outline-none focus:border-white/20
                        transition-colors duration-150
                        resize-none
                    "
                    disabled={busy}
                />
                <div className="text-white/20 text-[9px] mb-3">
                    Supports UUIDs, custom IDs, or numeric IDs
                </div>
                <div className="flex gap-2 justify-end">
                    <button
                        onClick={onCancel}
                        disabled={busy}
                        className="
                            px-3 py-1.5 
                            bg-white/5 hover:bg-white/10 
                            text-white/60 hover:text-white/80 
                            rounded text-xs
                            transition-colors duration-150
                        "
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onSubmit}
                        disabled={busy}
                        data-testid="pipeline-bulk-add-submit"
                        className="
                            px-3 py-1.5 
                            bg-white/10 hover:bg-white/15 
                            text-white/80 hover:text-white 
                            rounded text-xs font-medium
                            transition-colors duration-150
                        "
                    >
                        {busy ? "Adding..." : "Import"}
                    </button>
                </div>
            </div>
        </div>
    );
}

export default BulkAddModal;
