import React from "react";

function BulkAddModal({
    value,
    onChange,
    busy,
    onCancel,
    onSubmit,
}) {
    return (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
            <div className="
                relative
                bg-white
                border border-black/[0.08]
                rounded-xl
                p-6
                w-full max-w-lg
                shadow-[0_24px_60px_-32px_rgba(0,0,0,0.18)]
            ">
                <div className="relative space-y-4">
                    <div>
                        <h3 className="text-[13px] tracking-[0.12em] uppercase font-medium text-black/85">
                            Bulk Import Talents
                        </h3>
                        <p className="text-black/45 text-[10px] tracking-wide leading-relaxed mt-1">
                            Enter talent IDs (one per line or comma-separated)
                        </p>
                    </div>

                    <textarea
                        value={value}
                        onChange={(e) => onChange(e.target.value)}
                        placeholder="tal_12345&#10;tal_67890&#10;tal_11111"
                        data-testid="pipeline-bulk-input"
                        className="
                            w-full h-32 
                            bg-[#f5f5f5]
                            border border-black/[0.08]
                            rounded-lg
                            p-3
                            text-black/85 text-[12px] leading-relaxed
                            placeholder:text-black/30
                            font-mono
                            focus:outline-none
                            focus:border-black/[0.14]
                            focus:bg-white
                            focus:ring-1 focus:ring-black/[0.06]
                            transition-all duration-150
                            resize-none
                        "
                        disabled={busy}
                    />

                    <div className="text-black/40 text-[10px] tracking-wide">
                        Supports UUIDs, custom IDs, or numeric IDs
                    </div>

                    <div className="flex gap-2 justify-end">
                        <button
                            onClick={onCancel}
                            disabled={busy}
                            className="
                                px-4 py-2 rounded-full
                                bg-black/[0.04] hover:bg-black/[0.07]
                                text-black/60 hover:text-black/85
                                text-[10px] tracking-[0.12em] uppercase
                                transition-all duration-150
                                disabled:opacity-40 disabled:cursor-not-allowed
                            "
                        >
                            Cancel
                        </button>
                        <button
                            onClick={onSubmit}
                            disabled={busy}
                            data-testid="pipeline-bulk-add-submit"
                            className="
                                px-4 py-2 rounded-full
                                bg-black text-white
                                hover:bg-black/90
                                text-[10px] tracking-[0.12em] uppercase font-medium
                                transition-all duration-150
                                disabled:opacity-40 disabled:cursor-not-allowed
                            "
                        >
                            {busy ? "Importing" : "Import"}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default BulkAddModal;
