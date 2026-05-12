import React, { memo, useRef, useEffect } from "react";
import TalentAvatar from "./TalentAvatar";

const QuickAddTalents = memo(function QuickAddTalents({
    searchQuery,
    onSearchQueryChange,
    searchLoading,
    searchResults,
    selectedTalents,
    onToggleTalent,
    onAddSelected,
}) {
    const [isOpen, setIsOpen] = React.useState(false);
    const wrapperRef = useRef(null);

    useEffect(() => {
        function handleClickOutside(event) {
            if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
                setIsOpen(false);
            }
        }
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, []);

    const hasSelected = selectedTalents.size > 0;

    return (
        <div className="mb-5 relative" ref={wrapperRef}>
            <div className="flex items-center gap-2">
                <div className="relative flex-1">
                    <input
                        type="text"
                        value={searchQuery}
                        onChange={(e) => {
                            onSearchQueryChange(e.target.value);
                            setIsOpen(true);
                        }}
                        onFocus={() => setIsOpen(true)}
                        placeholder="Quick add — search by name or email..."
                        className="
                            w-full
                            bg-black/40 border border-white/[0.08]
                            rounded-md
                            pl-8 pr-3 py-1.5
                            text-[12px] text-white/75 placeholder-white/20
                            focus:outline-none focus:border-white/15
                            transition-colors duration-200
                        "
                    />
                    <svg
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-white/25"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                    >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    {searchLoading && (
                        <div className="absolute right-2.5 top-1/2 -translate-y-1/2">
                            <div className="w-3 h-3 border border-white/15 border-t-white/50 rounded-full animate-spin" />
                        </div>
                    )}
                </div>
                <button
                    onClick={onAddSelected}
                    disabled={!hasSelected}
                    className={`
                        shrink-0 px-3 py-1.5 rounded-md
                        text-[9px] tracking-wide uppercase font-medium
                        transition-all duration-200
                        ${hasSelected
                            ? "bg-white/10 text-white/80 hover:bg-white/15 border border-white/8"
                            : "bg-white/5 text-white/25 cursor-not-allowed"}
                    `}
                >
                    Add ({selectedTalents.size})
                </button>
            </div>

            {/* Search results dropdown */}
            {isOpen && searchQuery && (
                <div className="absolute z-20 mt-1 w-full bg-[#151515] border border-white/10 rounded-md shadow-lg overflow-hidden">
                    <div className="max-h-64 overflow-y-auto">
                        {searchResults.length === 0 && !searchLoading && (
                            <div className="px-3 py-6 text-center">
                                <p className="text-white/25 text-[10px]">
                                    No talents found
                                </p>
                            </div>
                        )}
                        {searchResults.map((talent) => {
                            const isSelected = selectedTalents.has(talent.id);
                            return (
                                <button
                                    key={talent.id}
                                    onClick={() => onToggleTalent(talent.id)}
                                    className={`
                                        w-full px-3 py-2 flex items-center gap-2.5
                                        hover:bg-white/5 transition-colors duration-150
                                        border-b border-white/[0.03] last:border-0
                                    `}
                                >
                                    <input
                                        type="checkbox"
                                        checked={isSelected}
                                        readOnly
                                        className="w-3.5 h-3.5 rounded border-white/20 bg-transparent"
                                    />
                                    <TalentAvatar
                                        src={talent.image_url}
                                        name={talent.name}
                                        size="sm"
                                    />
                                    <div className="flex-1 text-left">
                                        <p className="text-white/75 text-xs font-medium">
                                            {talent.name}
                                        </p>
                                        <p className="text-white/30 text-[10px] truncate">
                                            {talent.email}
                                        </p>
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                </div>
            )}
        </div>
    );
});

export default QuickAddTalents;
