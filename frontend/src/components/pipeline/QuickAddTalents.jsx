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

    // Handle click outside with proper refs
    useEffect(() => {
        function handleClickOutside(event) {
            if (
                wrapperRef.current && 
                !wrapperRef.current.contains(event.target) &&
                dropdownRef.current &&
                !dropdownRef.current.contains(event.target)
            ) {
                setIsOpen(false);
                setFocusedIndex(-1);
            }
        }
        
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    // Keyboard navigation
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (!isOpen) return;
            
            switch (e.key) {
                case 'ArrowDown':
                    e.preventDefault();
                    setFocusedIndex(prev => 
                        prev < searchResults.length - 1 ? prev + 1 : prev
                    );
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    setFocusedIndex(prev => prev > 0 ? prev - 1 : -1);
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
                    inputRef.current?.blur();
                    break;
                default:
                    break;
            }
        };
        
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, focusedIndex, searchResults, onToggleTalent]);

    const hasSelected = selectedTalents.size > 0;
    const limitedResults = searchResults.slice(0, maxResults);
    const hasMoreResults = searchResults.length > maxResults;

    // Auto-open dropdown when typing
    const handleInputChange = useCallback((e) => {
        onSearchQueryChange(e.target.value);
        setIsOpen(true);
        setFocusedIndex(-1);
    }, [onSearchQueryChange]);

    const handleAddSelected = useCallback(() => {
        if (hasSelected) {
            onAddSelected();
            setIsOpen(false);
            setFocusedIndex(-1);
        }
    }, [hasSelected, onAddSelected]);

    const handleSelectTalent = useCallback((id) => {
        onToggleTalent(id);
        // Keep dropdown open for multi-select
        inputRef.current?.focus();
    }, [onToggleTalent]);

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
                        placeholder="Quick add — search by name or email..."
                        aria-label="Search talents"
                        aria-expanded={isOpen}
                        aria-autocomplete="list"
                        aria-controls="quick-add-dropdown"
                        className="
                            w-full
                            bg-black/40 border border-white/[0.08]
                            rounded-md
                            pl-8 pr-8 py-1.5
                            text-[12px] text-white/75 placeholder-white/20
                            focus:outline-none focus:border-white/15 focus:ring-1 focus:ring-white/5
                            transition-all duration-200
                        "
                    />
                    <svg
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-white/25 pointer-events-none"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        aria-hidden="true"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    
                    {/* Clear button */}
                    {searchQuery && (
                        <button
                            type="button"
                            onClick={() => onSearchQueryChange("")}
                            aria-label="Clear search"
                            className="
                                absolute right-2 top-1/2 -translate-y-1/2
                                w-4 h-4 rounded-full
                                flex items-center justify-center
                                text-white/30 hover:text-white/60
                                bg-white/[0.03] hover:bg-white/[0.06]
                                transition-colors text-xs
                            "
                        >
                            ×
                        </button>
                    )}
                    
                    {/* Loading spinner */}
                    {searchLoading && (
                        <div className="absolute right-2.5 top-1/2 -translate-y-1/2">
                            <div className="w-3 h-3 border border-white/15 border-t-white/50 rounded-full animate-spin" />
                        </div>
                    )}
                </div>
                
                <button
                    onClick={handleAddSelected}
                    disabled={!hasSelected}
                    aria-label={`Add ${selectedTalents.size} selected talent(s)`}
                    className={`
                        shrink-0 px-3 py-1.5 rounded-md
                        text-[9px] tracking-wide uppercase font-medium
                        transition-all duration-200
                        focus:outline-none focus:ring-1 focus:ring-white/20
                        ${hasSelected
                            ? "bg-white/10 text-white/80 hover:bg-white/15 border border-white/8 cursor-pointer"
                            : "bg-white/5 text-white/25 cursor-not-allowed"}
                    `}
                >
                    Add ({selectedTalents.size})
                </button>
            </div>

            {/* Enhanced dropdown with proper stacking and spacing */}
            {isOpen && searchQuery && (
                <div 
                    id="quick-add-dropdown"
                    ref={dropdownRef}
                    role="listbox"
                    aria-label="Search results"
                    className="
                        absolute top-full left-0 right-0 z-50 mt-2
                        bg-[#151515] border border-white/10 
                        rounded-md shadow-2xl overflow-hidden
                        animate-dropdown-in
                    "
                >
                    <div className="max-h-80 overflow-y-auto tg-pipeline-scroll">
                        {/* Loading state */}
                        {searchLoading && searchResults.length === 0 && (
                            <div className="px-3 py-4 space-y-2">
                                {[1, 2, 3].map((i) => (
                                    <div key={i} className="flex items-center gap-2.5 animate-pulse">
                                        <div className="w-6 h-6 rounded-full bg-white/5" />
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
                            <div className="px-3 py-6 text-center">
                                <p className="text-white/25 text-[10px]">
                                    No talents found
                                </p>
                                <p className="text-white/15 text-[9px] mt-1">
                                    Try a different name or email
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
                                    className={`
                                        w-full px-3 py-2 flex items-center gap-2.5
                                        transition-all duration-150 text-left
                                        border-b border-white/[0.03] last:border-0
                                        ${isFocused ? 'bg-white/8' : 'hover:bg-white/5'}
                                        ${isSelected ? 'bg-white/[0.02]' : ''}
                                    `}
                                >
                                    <input
                                        type="checkbox"
                                        checked={isSelected}
                                        readOnly
                                        className="w-3.5 h-3.5 rounded border-white/20 bg-transparent pointer-events-none"
                                        tabIndex={-1}
                                        aria-hidden="true"
                                    />
                                    <TalentAvatar
                                        src={talent.image_url}
                                        name={talent.name}
                                        size="sm"
                                    />
                                    <div className="flex-1 min-w-0">
                                        <p className="text-white/75 text-xs font-medium truncate">
                                            {talent.name}
                                        </p>
                                        <p className="text-white/30 text-[10px] truncate">
                                            {talent.email}
                                        </p>
                                    </div>
                                </button>
                            );
                        })}
                        
                        {/* More results indicator */}
                        {hasMoreResults && (
                            <div className="px-3 py-2 text-center border-t border-white/[0.03]">
                                <p className="text-white/20 text-[8px] tracking-wide uppercase">
                                    +{searchResults.length - maxResults} more results — refine search
                                </p>
                            </div>
                        )}
                        
                        {/* Keyboard hint */}
                        {searchResults.length > 0 && (
                            <div className="px-3 py-1.5 bg-white/[0.02] border-t border-white/[0.03]">
                                <p className="text-white/15 text-[8px] tracking-wide text-center">
                                    ↑ ↓ to navigate · Enter to select · Esc to close
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
