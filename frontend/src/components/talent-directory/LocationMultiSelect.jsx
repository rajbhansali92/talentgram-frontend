import React, { useEffect, useMemo, useRef, useState } from "react";
import { Search, X, ChevronDown } from "lucide-react";

/**
 * LocationMultiSelect — searchable multi-select dropdown for the Location
 * filter, shared by Global Talent and Browse Roster. Options come from
 * GET /talents/facets (distinct city/country values actually present in
 * the roster) — never free text, so every selected value is guaranteed to
 * match something. Multiple selections combine with OR/IN server-side
 * ("Mumbai" + "Delhi" -> either).
 */
export default function LocationMultiSelect({ value = [], onChange, options = [] }) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");
    const ref = useRef(null);

    useEffect(() => {
        if (!open) return;
        const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
        document.addEventListener("mousedown", onClick);
        return () => document.removeEventListener("mousedown", onClick);
    }, [open]);

    const filteredOptions = useMemo(() => {
        const q = query.trim().toLowerCase();
        const list = q ? options.filter((o) => o.toLowerCase().includes(q)) : options;
        return list.slice(0, 50);
    }, [options, query]);

    const toggle = (loc) => {
        onChange(value.includes(loc) ? value.filter((v) => v !== loc) : [...value, loc]);
    };

    return (
        <div className="relative" ref={ref}>
            <button
                type="button"
                data-testid="location-multiselect-trigger"
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center justify-between px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300"
            >
                <span className={value.length ? "text-[#111111]" : "text-gray-400"}>
                    {value.length === 0 ? "Any" : value.length === 1 ? value[0] : `${value.length} selected`}
                </span>
                <ChevronDown className="w-3.5 h-3.5 text-gray-400" />
            </button>

            {value.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                    {value.map((loc) => (
                        <span key={loc} className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 bg-gray-100 text-[#111111] rounded-full text-xs font-medium">
                            {loc}
                            <button type="button" onClick={() => toggle(loc)} className="hover:bg-gray-200 rounded-full p-0.5">
                                <X className="w-2.5 h-2.5" />
                            </button>
                        </span>
                    ))}
                </div>
            )}

            {open && (
                <div className="absolute left-0 right-0 mt-1 bg-white border border-gray-200 rounded-lg shadow-lg z-30 overflow-hidden">
                    <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-100">
                        <Search className="w-3.5 h-3.5 text-gray-400 shrink-0" />
                        <input
                            type="text"
                            autoFocus
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                            placeholder="Search cities, countries…"
                            data-testid="location-multiselect-search"
                            className="w-full text-sm outline-none"
                        />
                    </div>
                    <div className="max-h-56 overflow-y-auto py-1">
                        {filteredOptions.length === 0 ? (
                            <p className="px-3 py-2 text-xs text-gray-400">No matches</p>
                        ) : (
                            filteredOptions.map((loc) => (
                                <button
                                    key={loc}
                                    type="button"
                                    data-testid={`location-option-${loc}`}
                                    onClick={() => toggle(loc)}
                                    className={`w-full flex items-center justify-between px-3 py-1.5 text-sm text-left hover:bg-gray-50 ${
                                        value.includes(loc) ? "text-[#0c2340] font-medium" : "text-[#333333]"
                                    }`}
                                >
                                    {loc}
                                    {value.includes(loc) && <span className="text-[#0c2340]">✓</span>}
                                </button>
                            ))
                        )}
                    </div>
                    <div className="border-t border-gray-100 px-3 py-2 flex justify-end">
                        <button type="button" onClick={() => setOpen(false)} className="text-xs font-medium text-[#333333] hover:text-[#111111]">
                            Done
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
