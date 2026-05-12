import React, { memo, useRef, useEffect, useState, useCallback } from "react";
import TalentAvatar from "./TalentAvatar";

/**
 * QuickAddTalents — Search and select talents to add to pipeline.
 * 
 * ISSUE 8 (Extended): Fixed dropdown stacking and clipping issues
 * Features:
 *   • Proper z-index stacking (dropdown above sticky elements)
 *   • Increased dropdown offset for better visual separation
 *   • Keyboard navigation support (Enter, Escape, Arrow keys)
 *   • Focus management and accessibility
 *   • Loading states with skeleton option
 *   • No DOM mutation — uses proper CSS containment
 */

const QuickAddTalents = memo(function QuickAddTalents({
    searchQuery,
    onSearchQueryChange,
    searchLoading,
    searchResults,
    selectedTalents,
    onToggleTalent,
    onAddSelected,
    maxResults = 10, // Configurable max results
}) {
    const [isOpen, setIsOpen] = useState(false);
    const [focusedIndex, setFocusedIndex] = useState(-1);
    const wrapperRef = useRef(null);
    const inputRef = useRef(null);
    const dropdownRef = useRef(null);

    // Handle click outside with simplified logic
    useEffect(() => {
        function handleClickOutside(event) {
            if (
                wrapperRef.current && 
                !wrapperRef.current.contains(event.target)
            ) {
                setIsOpen(false);
                setFocusedIndex(-1);
            }
        }
        
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    // Scroll focused item into view
    const scrollFocusedIntoView = useCallback((index) => {
        if (index < 0) return;
        
        setTimeout(() => {
            const focusedElement = document.querySelector(`[data-focused-index="${index}"]`);
            if (focusedElement) {
                focusedElement.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
        }, 0);
    }, []);

    // Keyboard navigation (scoped to input element)
    const handleKeyDown = useCallback((e) => {
        if (!isOpen) return;
        
        switch (e.key) {
            case 'ArrowDown':
                e.preventDefault();
                setFocusedIndex(prev => {
                    const next = prev < searchResults.length - 1 ? prev + 1 : prev;
                    scrollFocusedIntoView(next);
                    return next;
                });
                break;
            case 'ArrowUp':
                e.preventDefault();
                setFocusedIndex(prev => {
                    const next = prev > 0 ? prev - 1 : -1;
                    scrollFocusedIntoView(next);
                    return next;
                });
                break;
            case 'Enter':
                e.preventDefault();
                if (focusedIndex >= 0 && focusedIndex < searchResults.length) {
                    const talent = searchResults[focusedIndex];
                    onToggleTalent(talent.id);
                    setFocusedIndex(-1);
                } else if (searchResults.length === 1) {
                    // Auto-select if only one result
                    onToggleTalent(searchResults[0].id);
                }
                break;
            case 'Escape':
                e.preventDefault();
                setIsOpen(false);
                setFocusedIndex(-1);
                // Don't blur input — keep focus for rapid workflow
                break;
            default:
                break;
        }
    }, [isOpen, focusedIndex, searchResults, onToggleTalent, scrollFocusedIntoView]);

    const hasSelected = selectedTalents.size > 0;
    const limitedResults = searchResults.slice(0, maxResults);
    const hasMoreResults = searchResults.length > maxResults;

    // Auto-open dropdown when typing
    const handleInputChange = useCallback((e) => {
        onSearchQueryChange(e.target.value);
        setIsOpen(true);
        setFocusedIndex(-1);
    }, [onSearchQueryChange]);

    const handleClearSearch = useCallback(() => {
        onSearchQueryChange("");
        setIsOpen(false);
        setFocusedIndex(-1);
        inputRef.current?.focus();
    }, [onSearchQueryChange]);

    const handleAddSelected = useCallback(() => {
        if (hasSelected) {
            onAddSelected();
            setIsOpen(false);
            setFocusedIndex(-1);
            // Refocus input for rapid sequential adds
            setTimeout(() => {
                inputRef.current?.focus();
            }, 0);
        }
    }, [hasSelected, onAddSelected]);

    const handleSelectTalent = useCallback((id) => {
        onToggleTalent(id);
        // Keep dropdown open for multi-select
        inputRef.current?.focus();
    }, [onToggleTalent]);

    // Custom checkbox component - operational ATS style
    const CustomCheckbox = ({ checked }) => (
        <div className={`
            w-4 h-4 rounded-[3px]
            flex items-center justify-center
            transition-all duration-150
            ${checked 
                ? 'bg-black border-black' 
                : 'border border-black/[0.18] bg-white'
            }
        `}>
            {checked && (
                <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
                </svg>
            )}
        </div>
    );

    // Determine if spinner should show (avoid collision with clear button)
    const showSpinner = searchLoading && !searchQuery;

    return (
        <div className="mb-5 relative z-40" ref={wrapperRef}>
            <div className="flex items-center gap-2">
                <div className="relative flex-1">
                    <input
                        ref={inputRef}
                        type="text"
                        value={searchQuery}
                        onChange={handleInputChange}
                        onFocus={() => setIsOpen(true)}
                        onKeyDown={handleKeyDown}
                        placeholder="Quick add — search by name or email..."
                        aria-label="Search talents"
                        aria-expanded={isOpen}
                        aria-autocomplete="list"
                        aria-controls="quick-add-dropdown"
                        className="
                            w-full
                            bg-[#f5f5f5] border border-black/[0.08]
                            rounded-lg
                            pl-9 pr-9 py-2
                            text-[13px] text-black/85 placeholder:text-black/35
                            focus-visible:outline-none focus-visible:border-black/[0.15] focus-visible:ring-1 focus-visible:ring-black/8
                            transition-all duration-150
                            hover:border-black/[0.12]
                        "
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
                    
                    {/* Clear button with larger touch target */}
                    {searchQuery && !showSpinner && (
                        <button
                            type="button"
                            onClick={handleClearSearch}
                            aria-label="Clear search"
                            className="
                                absolute right-2 top-1/2 -translate-y-1/2
                                min-w-[28px] min-h-[28px] w-7 h-7 rounded
                                flex items-center justify-center
                                text-black/45 hover:text-black/75
                                bg-black/[0.03] hover:bg-black/[0.06]
                                transition-all duration-150
                                text-base font-medium
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/15
                            "
                        >
                            ×
                        </button>
                    )}
                    
                    {/* Loading spinner - positioned to avoid collision */}
                    {showSpinner && (
                        <div className="absolute right-3 top-1/2 -translate-y-1/2">
                            <div className="w-3.5 h-3.5 border border-black/15 border-t-black/50 rounded-full animate-spin" />
                        </div>
                    )}
                </div>
                
                <button
                    onClick={handleAddSelected}
                    disabled={!hasSelected}
                    aria-label={`Add ${selectedTalents.size} selected talent(s)`}
                    className={`
                        shrink-0 min-h-[36px] px-3.5 py-1.5 rounded-md
                        text-[10px] tracking-wide uppercase font-semibold
                        transition-all duration-150
                        focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-black/25
                        ${hasSelected
                            ? "bg-black text-white hover:bg-black/90 cursor-pointer active:scale-[0.98]"
                            : "bg-black/[0.05] text-black/30 cursor-not-allowed"}
                    `}
                >
                    Add ({selectedTalents.size})
                </button>
            </div>

            {/* Enhanced dropdown with robust positioning - operational ATS style */}
            {isOpen && searchQuery && (
                <div 
                    id="quick-add-dropdown"
                    ref={dropdownRef}
                    role="listbox"
                    aria-label="Search results"
                    className="
                        absolute top-full left-0 mt-2 z-50
                        w-full sm:max-w-[420px]
                        bg-white
                        border border-black/[0.08] 
                        rounded-lg shadow-[0_8px_24px_-16px_rgba(0,0,0,0.12)]
                        overflow-hidden
                        transition-all duration-150 ease-out
                    "
                >
                    <div className="max-h-80 overflow-y-auto overscroll-contain tg-pipeline-scroll">
                        {/* Loading state */}
                        {searchLoading && searchResults.length === 0 && (
                            <div className="px-3 py-4 space-y-2.5">
                                {[1, 2, 3].map((i) => (
                                    <div key={i} className="flex items-center gap-2.5 animate-pulse">
                                        <div className="w-7 h-7 rounded-full bg-black/[0.06]" />
                                        <div className="flex-1">
                                            <div className="h-3 w-24 bg-black/[0.06] rounded" />
                                            <div className="h-2 w-32 bg-black/[0.03] rounded mt-1" />
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                        
                        {/* No results */}
                        {!searchLoading && searchResults.length === 0 && (
                            <div className="px-3 py-10 text-center">
                                <p className="text-black/45 text-[10px]">
                                    No matching talents found
                                </p>
                                <p className="text-black/30 text-[9px] mt-1.5">
                                    Try refining the search
                                </p>
                            </div>
                        )}
                        
                        {/* Results list */}
                        {limitedResults.map((talent, index) => {
                            const isSelected = selectedTalents.has(talent.id);
                            const isFocused = focusedIndex === index;
                            
                            return (
                                <button
                                    key={talent.id}
                                    onClick={() => handleSelectTalent(talent.id)}
                                    onMouseEnter={() => setFocusedIndex(index)}
                                    onMouseLeave={() => setFocusedIndex(-1)}
                                    role="option"
                                    aria-selected={isSelected}
                                    data-focused-index={index}
                                    className={`
                                        w-full px-3 py-2 flex items-center gap-2.5
                                        transition-all duration-150 text-left
                                        border-b border-black/[0.04] last:border-0
                                        ${isFocused ? 'bg-black/[0.04]' : 'hover:bg-black/[0.03]'}
                                        ${isSelected ? 'bg-black/[0.02]' : ''}
                                        active:scale-[0.995]
                                    `}
                                >
                                    <CustomCheckbox checked={isSelected} />
                                    <TalentAvatar
                                        src={talent.image_url}
                                        name={talent.name}
                                        size="sm"
                                    />
                                    <div className="flex-1 min-w-0">
                                        <p className="text-black/85 text-xs font-medium truncate">
                                            {talent.name}
                                        </p>
                                        <p className="text-black/45 text-[10px] truncate">
                                            {talent.email}
                                        </p>
                                    </div>
                                </button>
                            );
                        })}
                        
                        {/* More results indicator */}
                        {hasMoreResults && (
                            <div className="px-3 py-2 text-center border-t border-black/[0.05] bg-[#fafafa]">
                                <p className="text-black/35 text-[8px] tracking-wide uppercase">
                                    +{searchResults.length - maxResults} more results — refine search
                                </p>
                            </div>
                        )}
                        
                        {/* Keyboard hint */}
                        {searchResults.length > 0 && (
                            <div className="px-3 py-1.5 bg-[#fafafa] border-t border-black/[0.05]">
                                <p className="text-black/25 text-[8px] tracking-wide text-center">
                                    ↑ ↓ navigate · Enter select · Esc close
                                </p>
                            </div>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
});

export default QuickAddTalents;
