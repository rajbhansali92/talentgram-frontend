import React, { useState, useEffect } from "react";
import { SlidersHorizontal } from "lucide-react";
import { Sheet, SheetContent, SheetTrigger, SheetClose } from "@/components/ui/sheet";
import FilterPanel from "./FilterPanel";

/**
 * MobileFilterSheet — the compact filter button + bottom sheet for <768px
 * viewports. Built on the existing generic `ui/sheet.jsx` Radix primitive
 * (side="bottom") rather than hand-rolling another fixed/inset-0 overlay —
 * three near-identical hand-rolled versions of this already existed
 * (TalentBrowserModal's MobileFiltersSheet, PipelineFilters' drawer, the
 * talent preview drawer); this is the first one built on the shared
 * primitive instead of adding a fourth copy.
 *
 * Uses local draft state + explicit Apply, so filter requests don't fire on
 * every tap while the sheet is open — only when the talent commits.
 */
export default function MobileFilterSheet({ filters, setFilter, clearAllFilters, activeFilterCount, availableTags = [], availableLocations = [] }) {
    const [open, setOpen] = useState(false);
    const [draft, setDraft] = useState(filters);

    useEffect(() => {
        if (open) setDraft(filters);
    }, [open, filters]);

    const setDraftFilter = (key, value) => setDraft((d) => ({ ...d, [key]: value }));

    const apply = () => {
        Object.entries(draft).forEach(([key, value]) => {
            if (JSON.stringify(value) !== JSON.stringify(filters[key])) setFilter(key, value);
        });
        setOpen(false);
    };

    const reset = () => {
        clearAllFilters();
        setOpen(false);
    };

    return (
        <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger asChild>
                <button
                    type="button"
                    data-testid="mobile-filter-trigger"
                    className="relative flex items-center gap-2 px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg"
                >
                    <SlidersHorizontal className="w-4 h-4 text-[#333333]" />
                    Filters
                    {activeFilterCount > 0 && (
                        <span
                            data-testid="mobile-filter-count"
                            className="absolute -top-1.5 -right-1.5 min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full bg-[#0c2340] text-white text-[10px] font-semibold"
                        >
                            {activeFilterCount}
                        </span>
                    )}
                </button>
            </SheetTrigger>
            <SheetContent
                side="bottom"
                className="max-h-[85vh] rounded-t-2xl overflow-hidden flex flex-col p-0"
            >
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 shrink-0">
                    <h3 className="text-sm font-semibold text-[#111111] uppercase tracking-wider">Filters</h3>
                    <SheetClose asChild>
                        <button type="button" className="text-xs text-[#333333]">Close</button>
                    </SheetClose>
                </div>
                <div className="flex-1 overflow-y-auto px-5 py-4">
                    <FilterPanel filters={draft} setFilter={setDraftFilter} availableTags={availableTags} availableLocations={availableLocations} />
                </div>
                <div className="flex items-center gap-3 px-5 py-4 border-t border-gray-100 shrink-0 bg-white pb-[calc(1rem+env(safe-area-inset-bottom))]">
                    <button
                        type="button"
                        data-testid="mobile-filter-reset"
                        onClick={reset}
                        className="flex-1 py-3 text-sm font-medium text-[#333333] border border-gray-200 rounded-full"
                    >
                        Reset
                    </button>
                    <button
                        type="button"
                        data-testid="mobile-filter-apply"
                        onClick={apply}
                        className="flex-1 py-3 text-sm font-medium text-white bg-[#0c2340] rounded-full"
                    >
                        Apply
                    </button>
                </div>
            </SheetContent>
        </Sheet>
    );
}
