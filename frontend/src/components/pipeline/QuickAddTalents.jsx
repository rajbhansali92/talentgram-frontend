import React, { memo } from "react";
import TalentAvatar from "./TalentAvatar";

/**
 * QuickAddTalents — search box + result list with multi-select.
 *
 * Behaviour matches the original "Quick Add Talents" panel from
 * ProjectPipeline.jsx exactly. Search debouncing happens in the
 * `useTalentSearch` hook upstream — this component is purely visual.
 */
const QuickAddTalents = memo(function QuickAddTalents({
    searchQuery,
    onSearchQueryChange,
    searchLoading,
    searchResults,
    selectedTalents,
    onToggleTalent,
    onAddSelected,
}) {
    return (
        <div className="mb-6">
            <div className="bg-black/40 border border-white/10 rounded-lg p-4">
                <h3 className="text-white/80 text-sm font-medium mb-3">
                    Quick Add Talents
                </h3>
                <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => onSearchQueryChange(e.target.value)}
                    placeholder="Search by name or email…"
                    data-testid="pipeline-search-input"
                    className="w-full bg-black/50 border border-white/20 rounded px-3 py-2 text-white placeholder-white/40 focus:outline-none focus:border-white/40"
                />

                {searchQuery && (
                    <div className="mt-4">
                        {searchLoading && (
                            <div className="text-white/60 text-sm text-center py-4">
                                Searching…
                            </div>
                        )}

                        {!searchLoading && searchResults.length === 0 && (
                            <div className="text-white/40 text-sm text-center py-4">
                                No talents found matching &quot;{searchQuery}&quot;
                            </div>
                        )}

                        {!searchLoading && searchResults.length > 0 && (
                            <>
                                <div className="flex justify-between items-center mb-2">
                                    <span className="text-white/60 text-xs">
                                        {searchResults.length} result(s)
                                    </span>
                                    {selectedTalents.size > 0 && (
                                        <button
                                            onClick={onAddSelected}
                                            data-testid="pipeline-add-selected"
                                            className="text-xs bg-green-500/20 hover:bg-green-500/30 text-green-300 px-2 py-1 rounded"
                                        >
                                            Add {selectedTalents.size}
                                        </button>
                                    )}
                                </div>

                                <div className="space-y-2 max-h-64 overflow-y-auto">
                                    {searchResults.map((talent) => (
                                        <SearchResultRow
                                            key={talent.id}
                                            talent={talent}
                                            selected={selectedTalents.has(talent.id)}
                                            onToggle={onToggleTalent}
                                        />
                                    ))}
                                </div>
                            </>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
});

const SearchResultRow = memo(function SearchResultRow({ talent, selected, onToggle }) {
    return (
        <div
            onClick={() => onToggle(talent.id)}
            data-testid={`pipeline-search-row-${talent.id}`}
            className={`bg-white/5 border rounded p-2 cursor-pointer transition-all ${
                selected
                    ? "border-blue-400 bg-blue-500/20"
                    : "border-white/10 hover:bg-white/10"
            }`}
        >
            <div className="flex items-center gap-2">
                <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => onToggle(talent.id)}
                    onClick={(e) => e.stopPropagation()}
                    className="w-4 h-4"
                />
                <TalentAvatar src={talent.image_url} name={talent.name} size="sm" />
                <div className="flex-1 min-w-0">
                    <p className="text-white text-sm font-medium truncate">
                        {talent.name || "Unnamed Talent"}
                    </p>
                    {talent.email && (
                        <p className="text-white/40 text-xs truncate">
                            {talent.email}
                        </p>
                    )}
                    {talent.instagram_handle && (
                        <p className="text-white/30 text-xs truncate tg-mono">
                            {talent.instagram_handle}
                        </p>
                    )}
                </div>
            </div>
        </div>
    );
});

export default QuickAddTalents;
