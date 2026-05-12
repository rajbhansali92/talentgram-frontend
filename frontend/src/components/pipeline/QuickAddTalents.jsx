import React, { memo, useRef, useEffect, useState, useCallback } from "react";
import TalentAvatar from "./TalentAvatar";

/**
 * QuickAddTalents — Search and select talents to add to pipeline.
 * 
 * ISSUE 8 (Extended): Fixed dropdown stacking and clipping issues
 * Features:
 *   • Proper z-index stacking (dropdown above sticky elements)
 *   • Increased dropdown offset for better visual separation
   •   • Keyboard navigation support (Enter, Escape, Arrow keys)
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

    // Custom checkbox component
    const CustomCheckbox = ({ checked }) => (
        <div className={`
            w-4 h-4 rounded-full
            flex items-center justify-center
            transition-all duration-150
            ${checked 
                ? 'bg-white/70 ring-1 ring-white/20' 
                : 'bg-transparent ring-1 ring-white/20 hover:ring-white/35'
            }
        `}>
            {checked && (
                <div className="w-1.5 h-1.5 rounded-full bg-black" />
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
                            bg-black/40 border border-white/[0.08]
                            rounded-lg
                            pl-9 pr-9 py-2
                            text-[13px] text-white/85 placeholder-white/20
                            focus-visible:outline-none focus-visible:border-white/15 focus-visible:ring-1 focus-visible:ring-white/8
                            transition-all duration-200
                        "
                    />
                    <svg
                        className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/30 pointer-events-none"
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
                                text-white/40 hover:text-white/70
                                bg-white/[0.02] hover:bg-white/[0.08]
                                transition-all duration-200
                                text-base font-medium
                                focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/15
                            "
                        >
                            ×
                        </button>
                    )}
                    
                    {/* Loading spinner - positioned to avoid collision */}
                    {showSpinner && (
                        <div className="absolute right-3 top-1/2 -translate-y-1/2">
                            <div className="w-3.5 h-3.5 border border-white/15 border-t-white/50 rounded-full animate-spin" />
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
                        transition-all duration-200
                        focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/25
                        ${hasSelected
                            ? "bg-white/14 text-white/85 hover:bg-white/18 border border-white/10 cursor-pointer active:scale-[0.98]"
                            : "bg-white/5 text-white/35 cursor-not-allowed"}
                    `}
                >
                    Add ({selectedTalents.size})
                </button>
            </div>

            {/* Enhanced dropdown with robust positioning */}
            {isOpen && searchQuery && (
                <div 
                    id="quick-add-dropdown"
                    ref={dropdownRef}
                    role="listbox"
                    aria-label="Search results"
                    className="
                        absolute top-full left-0 mt-2 z-50
                        w-full sm:max-w-[420px]
                        bg-gradient-to-b from-[#181818] to-[#121212]
                        border border-white/[0.08] 
                        rounded-lg shadow-2xl shadow-black/50
                        overflow-hidden
                        transition-all duration-200 ease-out
                    "
                >
                    <div className="max-h-80 overflow-y-auto overscroll-contain tg-pipeline-scroll">
                        {/* Loading state */}
                        {searchLoading && searchResults.length === 0 && (
                            <div className="px-3 py-4 space-y-2.5">
                                {[1, 2, 3].map((i) => (
                                    <div key={i} className="flex items-center gap-2.5 animate-pulse">
                                        <div className="w-7 h-7 rounded-full bg-white/5" />
                                        <div className="flex-1">
                                            <div className="h-3 w-24 bg-white/5 rounded" />
                                            <div className="h-2 w-32 bg-white/3 rounded mt-1" />
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                        
                        {/* No results */}
                        {!searchLoading && searchResults.length === 0 && (
                            <div className="px-3 py-10 text-center">
                                <p className="text-white/30 text-[10px]">
                                    No matching talents
                                </p>
                                <p className="text-white/15 text-[9px] mt-1.5">
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
                                        w-full px-3 py-2.5 flex items-center gap-3
                                        transition-all duration-150 text-left
                                        border-b border-white/[0.03] last:border-0
                                        ${isFocused ? 'bg-white/8' : 'hover:bg-white/[0.06]'}
                                        ${isSelected ? 'bg-white/[0.02]' : ''}
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
                                        <p className="text-white/80 text-xs font-medium truncate">
                                            {talent.name}
                                        </p>
                                        <p className="text-white/35 text-[10px] truncate">
                                            {talent.email}
                                        </p>
                                    </div>
                                </button>
                            );
                        })}
                        
                        {/* More results indicator */}
                        {hasMoreResults && (
                            <div className="px-3 py-2 text-center border-t border-white/[0.04]">
                                <p className="text-white/25 text-[8px] tracking-wide uppercase">
                                    +{searchResults.length - maxResults} more results — refine search
                                </p>
                            </div>
                        )}
                        
                        {/* Keyboard hint */}
                        {searchResults.length > 0 && (
                            <div className="px-3 py-1.5 bg-white/[0.02] border-t border-white/[0.04]">
                                <p className="text-white/14 text-[8px] tracking-wide text-center">
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
