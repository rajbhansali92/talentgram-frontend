import React, { memo, useState } from "react";
import { createPortal } from "react-dom";
import { STATUS_FOCUS_OPTIONS, TRISTATE_OPTIONS } from "./constants";

const PORTFOLIO_OPTIONS = [
    { value: "any", label: "All" },
    { value: "yes", label: "Attached" },
    { value: "no", label: "Missing" },
];

const IG_OPTIONS = [
    { value: "any", label: "All" },
    { value: "yes", label: "Connected" },
    { value: "no", label: "Unlinked" },
];

/**
 * PipelineFilters — visual filter toolbar component.
 * 
 * This is the UI component only. All filtering logic lives in:
 * frontend/src/hooks/usePipelineFilters.js
 * 
 * Features:
 *   • Sticky search input with clear button
 *   • Status focus segmented control (workflow state row)
 *   • Tristate filters for Portfolio and Instagram (data presence row)
 *   • Filter count display
 *   • Reset button for clearing all filters
 */

const PipelineFilters = memo(function PipelineFilters({
    search,
    onSearch,
    statusFocus,
    onStatusFocus,
    hasSubmission,
    onHasSubmission,
    hasIg,
    onHasIg,
    filtersActive,
    onClearAll,
    totalCount,
    filteredCount,
    isMobileDrawerOpen = false,
    onMobileDrawerOpenChange,
}) {
    const showingCount = filteredCount !== totalCount;

    const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
            onSearch('');
        }
    };

    const activeFilterCount = [
        statusFocus && statusFocus !== "all" && statusFocus !== "",
        hasSubmission && hasSubmission !== "any",
        hasIg && hasIg !== "any",
    ].filter(Boolean).length;

    React.useEffect(() => {
        if (!isMobileDrawerOpen) return;
        const originalStyle = window.getComputedStyle(document.body).overflow;
        document.body.style.overflow = "hidden";
        
        const handleGlobalKeyDown = (e) => {
            if (e.key === 'Escape') {
                onMobileDrawerOpenChange?.(false);
            }
        };
        window.addEventListener('keydown', handleGlobalKeyDown);
        return () => {
            window.removeEventListener('keydown', handleGlobalKeyDown);
            document.body.style.overflow = originalStyle;
        };
    }, [isMobileDrawerOpen, onMobileDrawerOpenChange]);

    return (
        <div data-testid="pipeline-control-bar" className="sticky top-3 z-30 mb-6">
            {/* Desktop View */}
            <div className="hidden md:block rounded-lg bg-white border border-black/[0.08] shadow-[0_4px_18px_-14px_rgba(0,0,0,0.08)] overflow-visible">
                <div className="px-4 pt-3 pb-2.5 space-y-2.5">
                    {/* Row 1: Search + Count/Clear inline */}
                    <div className="flex items-center gap-3">
                        <div className="relative flex-1">
                            <input
                                type="text"
                                value={search}
                                onChange={(e) => onSearch(e.target.value)}
                                onKeyDown={handleKeyDown}
                                placeholder="Search by name, email, phone, or ID..."
                                data-testid="pipeline-filter-search"
                                className="w-full bg-[#f5f5f5] border border-black/[0.08] rounded-lg pl-9 pr-9 py-1.5 text-[13px] text-black/85 placeholder:text-black/35 focus-visible:outline-none focus-visible:border-black/[0.15] focus-visible:ring-1 focus-visible:ring-black/8 transition-all duration-200 hover:border-black/[0.12]"
                            />
                            <svg
                                className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-black/30 pointer-events-none"
                                fill="none"
                                stroke="currentColor"
                                viewBox="0 0 24 24"
                                aria-hidden="true"
                            >
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                            </svg>
                            {search && (
                                <button
                                    type="button"
                                    onClick={() => onSearch("")}
                                    aria-label="Clear search"
                                    className="absolute right-2 top-1/2 -translate-y-1/2 min-w-[22px] min-h-[22px] w-[22px] h-[22px] rounded flex items-center justify-center text-black/45 hover:text-black/75 bg-black/[0.03] hover:bg-black/[0.06] transition-all duration-200 text-base font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15"
                                >
                                    ×
                                </button>
                            )}
                        </div>
                        <div className="flex items-center gap-2.5 shrink-0">
                            <span className={`text-[10px] font-mono tracking-wide ${showingCount ? "text-black/55" : "text-black/35"}`} data-testid="pipeline-filter-count">
                                {showingCount ? `${filteredCount.toLocaleString()}/${totalCount.toLocaleString()}` : `${totalCount.toLocaleString()} total`}
                            </span>
                            {filtersActive && (
                                <button
                                    type="button"
                                    onClick={onClearAll}
                                    data-testid="pipeline-filter-clear"
                                    className="px-2.5 py-0.5 rounded text-[10px] tracking-wide uppercase font-medium text-black/45 hover:text-black/75 hover:bg-black/[0.04] transition-all duration-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15"
                                    title="Clear all filters"
                                >
                                    Clear
                                </button>
                            )}
                        </div>
                    </div>
                    <div className="w-full h-px bg-black/[0.05]" aria-hidden="true" />
                    {/* Row 2: Status filter group */}
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">Status</span>
                        <div data-testid="pipeline-filter-focus" className="flex items-center gap-1 flex-wrap">
                            {STATUS_FOCUS_OPTIONS.map((opt) => {
                                const active = statusFocus === opt.value;
                                return (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => onStatusFocus(opt.value)}
                                        data-testid={`pipeline-filter-focus-${opt.value}`}
                                        aria-pressed={active}
                                        className={`shrink-0 px-2.5 py-1 rounded-[5px] text-[10px] font-medium tracking-wide uppercase transition-all duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15 ${active ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]" : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"}`}
                                    >
                                        {opt.label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                    {/* Row 3: Portfolio + Instagram */}
                    <div className="flex items-center gap-4 flex-wrap">
                        <div className="flex items-center gap-2">
                            <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">Portfolio</span>
                            <div data-testid="pipeline-filter-submitted" className="flex items-center gap-1">
                                {PORTFOLIO_OPTIONS.map((opt) => {
                                    const active = hasSubmission === opt.value;
                                    return (
                                        <button
                                            key={opt.value}
                                            type="button"
                                            onClick={() => onHasSubmission(opt.value)}
                                            data-testid={`pipeline-filter-submitted-${opt.value}`}
                                            aria-pressed={active}
                                            className={`shrink-0 px-2.5 py-1 rounded-[5px] text-[10px] font-medium tracking-wide uppercase transition-all duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15 ${active ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]" : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"}`}
                                        >
                                            {opt.label}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                        <span className="hidden sm:block w-px h-3.5 bg-black/[0.08] shrink-0" aria-hidden="true" />
                        <div className="flex items-center gap-2">
                            <span className="text-[9px] font-medium tracking-widest uppercase text-black/30 shrink-0 w-16">Instagram</span>
                            <div data-testid="pipeline-filter-ig" className="flex items-center gap-1">
                                {IG_OPTIONS.map((opt) => {
                                    const active = hasIg === opt.value;
                                    return (
                                        <button
                                            key={opt.value}
                                            type="button"
                                            onClick={() => onHasIg(opt.value)}
                                            data-testid={`pipeline-filter-ig-${opt.value}`}
                                            aria-pressed={active}
                                            className={`shrink-0 px-2.5 py-1 rounded-[5px] text-[10px] font-medium tracking-wide uppercase transition-all duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15 ${active ? "bg-neutral-900 text-white shadow-[0_1px_2px_rgba(0,0,0,0.05)]" : "text-black/55 bg-white border border-black/[0.06] hover:text-black/85 hover:bg-black/[0.02] hover:border-black/[0.08]"}`}
                                        >
                                            {opt.label}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            {/* Mobile View - Drawer Trigger and search input */}
            <div className="block md:hidden flex gap-2 w-full">
                <div className="relative flex-1">
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => onSearch(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder="Search candidates..."
                        data-testid="pipeline-filter-search-mobile"
                        className="w-full bg-white border border-black/[0.08] rounded-xl pl-9 pr-9 py-2.5 text-[14px] text-black/85 placeholder:text-black/35 focus-visible:outline-none focus-visible:border-black/[0.15] shadow-[0_1px_2px_rgba(0,0,0,0.02)] min-h-[44px]"
                    />
                    <svg
                        className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-black/30 pointer-events-none"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        aria-hidden="true"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    {search && (
                        <button
                            type="button"
                            onClick={() => onSearch("")}
                            aria-label="Clear search"
                            className="absolute right-3 top-1/2 -translate-y-1/2 min-w-[24px] min-h-[24px] w-[24px] h-[24px] rounded flex items-center justify-center text-black/45 hover:text-black/75 bg-black/[0.03]"
                        >
                            ×
                        </button>
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => onMobileDrawerOpenChange?.(!isMobileDrawerOpen)}
                    className="px-4 py-2 border border-black/[0.08] rounded-xl bg-white flex items-center gap-2 text-[13px] font-semibold text-[#111111] min-h-[44px] active:scale-[0.98] transition-all shadow-[0_1px_2px_rgba(0,0,0,0.02)]"
                >
                    <svg className="w-4 h-4 text-black/55" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
                    </svg>
                    <span>Filters</span>
                    {activeFilterCount > 0 && (
                        <span className="bg-neutral-900 text-white rounded-full text-[9px] w-5 h-5 flex items-center justify-center font-mono font-bold shadow-sm">
                            {activeFilterCount}
                        </span>
                    )}
                </button>
            </div>

            {/* Mobile Filter Drawer Overlay bottom sheet */}
            {isMobileDrawerOpen && createPortal(
                <div className="fixed inset-0 z-[100] bg-black/25 backdrop-blur-xs md:hidden flex items-end">
                    <div className="absolute inset-0" onClick={() => onMobileDrawerOpenChange?.(false)} />
                    <div className="relative w-full bg-white rounded-t-3xl pl-[max(1.25rem,env(safe-area-inset-left))] pr-[max(1.25rem,env(safe-area-inset-right))] pb-[max(1.25rem,env(safe-area-inset-bottom))] pt-5 shadow-2xl animate-in slide-in-from-bottom duration-200 z-10 max-h-[85vh] flex flex-col overflow-hidden">
                        <div className="w-12 h-1 bg-slate-200 rounded-full mx-auto mb-4 flex-shrink-0" />
                        
                        <div className="flex justify-between items-center mb-5 pb-3 border-b border-black/[0.05] flex-shrink-0">
                            <div className="flex items-center gap-2">
                                <button
                                    type="button"
                                    onClick={() => onMobileDrawerOpenChange?.(false)}
                                    className="p-1.5 rounded-full text-[#333333] hover:text-[#222222] focus:outline-none min-w-[36px] min-h-[36px] flex items-center justify-center bg-black/[0.02] hover:bg-black/[0.05]"
                                    aria-label="Close filters"
                                >
                                    <svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                    </svg>
                                </button>
                                <div>
                                    <h3 className="font-bold text-sm text-[#111111] tracking-wide uppercase font-mono">Filters</h3>
                                    <span className="text-[10px] text-[#333333] font-mono">
                                        {showingCount ? `${filteredCount.toLocaleString()}/${totalCount.toLocaleString()} matched` : `${totalCount.toLocaleString()} total`}
                                    </span>
                                </div>
                            </div>
                            <div className="flex items-center gap-3">
                                {filtersActive && (
                                    <button
                                        type="button"
                                        onClick={onClearAll}
                                        className="text-xs text-rose-500 font-semibold uppercase tracking-wider min-h-[36px]"
                                    >
                                        Clear All
                                    </button>
                                )}
                                <button
                                    type="button"
                                    onClick={() => onMobileDrawerOpenChange?.(false)}
                                    className="px-4 py-2 bg-neutral-900 text-white rounded-xl text-xs font-semibold active:scale-[0.97] transition-all min-h-[38px] flex items-center justify-center"
                                >
                                    Done
                                </button>
                            </div>
                        </div>

                        <div className="flex-1 overflow-y-auto overscroll-contain tg-pipeline-scroll space-y-6 pb-6 pr-1">
                            {/* Status Section */}
                            <div className="space-y-2">
                                <span className="text-[9px] font-semibold tracking-widest uppercase text-black/40 font-mono block">Status</span>
                                <div className="flex flex-wrap gap-1.5">
                                    {STATUS_FOCUS_OPTIONS.map((opt) => {
                                        const active = statusFocus === opt.value;
                                        return (
                                            <button
                                                key={opt.value}
                                                type="button"
                                                onClick={() => onStatusFocus(opt.value)}
                                                className={`px-3 py-2 rounded-xl text-[11px] font-semibold uppercase tracking-wide transition-all min-h-[38px] ${active ? "bg-neutral-900 text-white shadow-sm" : "bg-slate-50 border border-slate-200 text-[#222222] active:bg-slate-100"}`}
                                            >
                                                {opt.label}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>

                            <div className="w-full h-px bg-black/[0.05]" />

                            {/* Portfolio Section */}
                            <div className="space-y-2">
                                <span className="text-[9px] font-semibold tracking-widest uppercase text-black/40 font-mono block">Portfolio</span>
                                <div className="flex flex-wrap gap-1.5">
                                    {PORTFOLIO_OPTIONS.map((opt) => {
                                        const active = hasSubmission === opt.value;
                                        return (
                                            <button
                                                key={opt.value}
                                                type="button"
                                                onClick={() => onHasSubmission(opt.value)}
                                                className={`px-3 py-2 rounded-xl text-[11px] font-semibold uppercase tracking-wide transition-all min-h-[38px] ${active ? "bg-neutral-900 text-white shadow-sm" : "bg-slate-50 border border-slate-200 text-[#222222] active:bg-slate-100"}`}
                                            >
                                                {opt.label}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>

                            <div className="w-full h-px bg-black/[0.05]" />

                            {/* Instagram Section */}
                            <div className="space-y-2">
                                <span className="text-[9px] font-semibold tracking-widest uppercase text-black/40 font-mono block">Instagram</span>
                                <div className="flex flex-wrap gap-1.5">
                                    {IG_OPTIONS.map((opt) => {
                                        const active = hasIg === opt.value;
                                        return (
                                            <button
                                                key={opt.value}
                                                type="button"
                                                onClick={() => onHasIg(opt.value)}
                                                className={`px-3 py-2 rounded-xl text-[11px] font-semibold uppercase tracking-wide transition-all min-h-[38px] ${active ? "bg-neutral-900 text-white shadow-sm" : "bg-slate-50 border border-slate-200 text-[#222222] active:bg-slate-100"}`}
                                            >
                                                {opt.label}
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>,
                document.body
            )}
        </div>
    );
});

export default PipelineFilters;
