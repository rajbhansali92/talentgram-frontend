import React from "react";

function BulkAddModal({
    value,
    onChange,
    busy,
    onCancel,
    onSubmit,
}) {
    return (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50">
            <div className="
                relative
                bg-gradient-to-b from-[#171717] to-[#101010]
                backdrop-blur-xl
                border border-white/[0.06]
                rounded-2xl
                p-6
                w-full max-w-lg
                shadow-[0_20px_60px_-24px_rgba(0,0,0,0.75)]
                shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]
            ">
                {/* Atmospheric top glow */}
                <div className="absolute top-0 left-0 right-0 h-16 pointer-events-none bg-gradient-to-b from-white/[0.02] to-transparent rounded-2xl" />
                
                <div className="relative space-y-4">
                    <div>
                        <h3 className="text-[13px] tracking-[0.08em] uppercase font-medium text-white">
                            Bulk Import Talents
                        </h3>
                        <p className="text-white/40 text-[10px] tracking-wide leading-relaxed mt-1">
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
                            bg-black/35
                            border border-white/[0.06]
                            rounded-xl
                            p-3
                            text-white text-[12px] leading-relaxed
                            placeholder:text-white/18
                            font-mono
                            focus:outline-none
                            focus:border-white/[0.14]
                            focus:bg-black/45
                            transition-all duration-150
                            resize-none
                        "
                        disabled={busy}
                    />

                    <div className="text-white/30 text-[10px] tracking-wide">
                        Supports UUIDs, custom IDs, or numeric IDs
                    </div>

                    <div className="flex gap-2 justify-end">
                        <button
                            onClick={onCancel}
                            disabled={busy}
                            className="
                                px-4 py-2
                                bg-white/[0.04] hover:bg-white/[0.07]
                                text-white/60 hover:text-white/80
                                rounded-full
                                text-[10px] tracking-[0.16em] uppercase
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
                                px-4 py-2
                                bg-white/[0.12] hover:bg-white/[0.16]
                                text-white/90 hover:text-white
                                rounded-full
                                text-[10px] tracking-[0.16em] uppercase font-medium
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
