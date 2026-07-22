import React, { useState, useRef, useEffect } from "react";
import { ArrowUpDown, Check } from "lucide-react";
import { SORT_OPTIONS } from "./constants";

/**
 * SortDropdown — shared sort control for Global Talent + Browse Roster.
 * No existing component to extend (the only precedent in the codebase is a
 * bare <select> in SubmissionReviewCenter.jsx) — built fresh here so both
 * surfaces get the exact same option set and behavior.
 */
export default function SortDropdown({ value, onChange }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);

    useEffect(() => {
        if (!open) return;
        const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
        const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
        document.addEventListener("mousedown", onClick);
        document.addEventListener("keydown", onKey);
        return () => {
            document.removeEventListener("mousedown", onClick);
            document.removeEventListener("keydown", onKey);
        };
    }, [open]);

    const current = SORT_OPTIONS.find((o) => o.value === value) || SORT_OPTIONS[0];

    return (
        <div className="relative" ref={ref}>
            <button
                type="button"
                data-testid="sort-dropdown-trigger"
                onClick={() => setOpen((v) => !v)}
                className="flex items-center gap-2 px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg hover:border-gray-300 transition-colors"
            >
                <ArrowUpDown className="w-3.5 h-3.5 text-[#333333]" />
                <span className="text-[#111111] font-medium">{current.label}</span>
            </button>
            {open && (
                <div
                    data-testid="sort-dropdown-menu"
                    className="absolute right-0 mt-1 w-64 max-h-80 overflow-y-auto bg-white border border-gray-200 rounded-lg shadow-lg z-30 py-1"
                >
                    {SORT_OPTIONS.map((opt) => (
                        <button
                            key={opt.value}
                            type="button"
                            data-testid={`sort-option-${opt.value}`}
                            onClick={() => { onChange(opt.value); setOpen(false); }}
                            className="w-full flex items-center justify-between px-3 py-2 text-sm text-left hover:bg-gray-50 transition-colors"
                        >
                            <span className={opt.value === value ? "text-[#111111] font-medium" : "text-[#333333]"}>{opt.label}</span>
                            {opt.value === value && <Check className="w-3.5 h-3.5 text-[#0c2340]" />}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
