import { memo, useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";

const STAGES = [
    "ask_to_test",
    "sent",
    "shortlisted",
    "locked",
    "not_interested",
    "not_available",
];

const STAGE_LABELS = {
    ask_to_test: "ASK TO TEST",
    sent: "SENT",
    shortlisted: "SHORTLISTED",
    locked: "LOCKED",
    not_interested: "NOT INTERESTED",
    not_available: "NOT AVAILABLE",
};

const NEXT_STAGE_FLOW = {
    ask_to_test: ["sent", "not_interested", "not_available"],
    sent: ["shortlisted", "not_interested", "not_available"],
    shortlisted: ["locked", "not_interested", "not_available"],
    locked: [],
    not_interested: [],
    not_available: [],
};

const getStageLabel = (stage) =>
    STAGE_LABELS[stage] || stage.replaceAll("_", " ").toUpperCase();

function ProjectPipeline({ projectId }) {
    const [data, setData] = useState([]);
    const [loading, setLoading] = useState(true);
    const [bulkIds, setBulkIds] = useState(new Set());
    const [bulkMode, setBulkMode] = useState(false);
    const [showBulkAdd, setShowBulkAdd] = useState(false);
    const [bulkTalentsInput, setBulkTalentsInput] = useState("");
    const [bulkAdding, setBulkAdding] = useState(false);
    const [error, setError] = useState(null);

    // Search state
    const [searchQuery, setSearchQuery] = useState("");
    const [searchResults, setSearchResults] = useState([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [selectedTalents, setSelectedTalents] = useState(new Set());

    // ✅ Stable identity. Used by Card.move + bulk handlers + post-mutation refresh.
    // Depends only on the primitive `projectId`, so re-creates exactly once per route.
    const fetchPipeline = useCallback(async () => {
        if (!projectId) return;
        try {
            setError(null);
            const res = await adminApi.get(`/pipeline/project/${projectId}`);
            setData(res.data?.data || []);
        } catch (e) {
            console.error("Failed to fetch pipeline:", e);
            setError("Failed to load pipeline data");
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    // ✅ Initial mount fetch with `alive` guard so we never call setState after
    // unmount (StrictMode double-invokes effects in dev — this makes that idempotent).
    useEffect(() => {
        if (!projectId) return;
        let alive = true;
        (async () => {
            try {
                setError(null);
                const res = await adminApi.get(`/pipeline/project/${projectId}`);
                if (!alive) return;
                setData(res.data?.data || []);
            } catch (e) {
                if (!alive) return;
                console.error("Failed to fetch pipeline:", e);
                setError("Failed to load pipeline data");
            } finally {
                if (alive) setLoading(false);
            }
        })();
        return () => {
            alive = false;
        };
    }, [projectId]);

    // ✅ Debounced search — primitive dep only, alive flag, inline (no stale closure).
    useEffect(() => {
        if (!searchQuery) {
            setSearchResults([]);
            return;
        }
        let alive = true;
        const timer = setTimeout(async () => {
            setSearchLoading(true);
            try {
                const res = await adminApi.get(
                    `/talents/search?q=${encodeURIComponent(searchQuery)}`,
                );
                if (alive) setSearchResults(res.data?.data || []);
            } catch (e) {
                console.error("Search failed", e);
                if (alive) setSearchResults([]);
            } finally {
                if (alive) setSearchLoading(false);
            }
        }, 300);
        return () => {
            alive = false;
            clearTimeout(timer);
        };
    }, [searchQuery]);

    // ✅ Functional setters — stable identity for memo'd children.
    const toggleTalentSelect = useCallback((id) => {
        setSelectedTalents((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const toggleBulkSelect = useCallback((id) => {
        setBulkIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const clearBulkSelection = useCallback(() => {
        setBulkIds(new Set());
        setBulkMode(false);
    }, []);

    const addSelectedToPipeline = async () => {
        if (selectedTalents.size === 0) return;
        try {
            await adminApi.post("/pipeline/add", {
                project_id: projectId,
                talent_ids: Array.from(selectedTalents),
            });
            setSelectedTalents(new Set());
            setSearchResults([]);
            setSearchQuery("");
            await fetchPipeline();
            toast.success(`Added ${selectedTalents.size} talent(s) to pipeline`);
        } catch (e) {
            console.error("Failed to add talents:", e);
            toast.error(e?.response?.data?.detail || "Failed to add talents to pipeline");
        }
    };

    const handleBulkAdd = async () => {
        const talentIds = bulkTalentsInput
            .split(/[\n,]/)
            .map((id) => id.trim())
            .filter((id) => id.length > 0 && id !== ",");

        if (talentIds.length === 0) {
            toast.error("Please enter at least one talent ID");
            return;
        }

        setBulkAdding(true);
        try {
            await adminApi.post("/pipeline/add", {
                project_id: projectId,
                talent_ids: talentIds,
            });
            setBulkTalentsInput("");
            setShowBulkAdd(false);
            await fetchPipeline();
            toast.success(`Successfully added ${talentIds.length} talent(s)`);
        } catch (e) {
            console.error("Bulk add failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to add talents");
        } finally {
            setBulkAdding(false);
        }
    };

    const handleBulkMove = async (targetStage) => {
        if (bulkIds.size === 0) return;
        const ok = window.confirm(
            `Move ${bulkIds.size} talent(s) to ${getStageLabel(targetStage)}?`,
        );
        if (!ok) return;
        try {
            await adminApi.patch("/pipeline/move", {
                ids: Array.from(bulkIds),
                stage: targetStage,
            });
            setBulkIds(new Set());
            setBulkMode(false);
            await fetchPipeline();
            toast.success(`Moved ${bulkIds.size} talent(s)`);
        } catch (e) {
            console.error("Bulk move failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to move talents");
        }
    };

    if (loading) {
        return (
            <div
                className="flex items-center justify-center h-64"
                data-testid="pipeline-loading"
            >
                <div className="text-white/60">Loading pipeline…</div>
            </div>
        );
    }

    if (error) {
        return (
            <div
                className="flex items-center justify-center h-64"
                data-testid="pipeline-error"
            >
                <div className="text-red-400">{error}</div>
            </div>
        );
    }

    return (
        <div className="p-4" data-testid="project-pipeline">
            {/* Header */}
            <div className="mb-4 flex justify-between items-center flex-wrap gap-2">
                <div>
                    <h2 className="text-white font-semibold">Casting Pipeline</h2>
                    <p className="text-white/40 text-xs mt-1 tg-mono">
                        Project ID: {projectId}
                    </p>
                </div>
                <div className="flex gap-2 flex-wrap">
                    {bulkMode ? (
                        <>
                            <button
                                onClick={clearBulkSelection}
                                data-testid="pipeline-bulk-cancel"
                                className="px-3 py-1 text-sm bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
                            >
                                Cancel ({bulkIds.size} selected)
                            </button>
                            {STAGES.slice(0, 4).map((stage) => (
                                <button
                                    key={stage}
                                    onClick={() => handleBulkMove(stage)}
                                    data-testid={`pipeline-bulk-move-${stage}`}
                                    className="px-3 py-1 text-sm bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 rounded transition-colors"
                                >
                                    Move to {getStageLabel(stage)}
                                </button>
                            ))}
                        </>
                    ) : (
                        <>
                            <button
                                onClick={() => setBulkMode(true)}
                                data-testid="pipeline-bulk-mode"
                                className="px-3 py-1 text-sm bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
                            >
                                Bulk Select
                            </button>
                            <button
                                onClick={() => setShowBulkAdd(true)}
                                data-testid="pipeline-bulk-add-open"
                                className="px-3 py-1 text-sm bg-green-500/20 hover:bg-green-500/30 text-green-300 rounded transition-colors"
                            >
                                + Bulk Add
                            </button>
                        </>
                    )}
                </div>
            </div>

            {/* Quick Add (search) */}
            <div className="mb-6">
                <div className="bg-black/40 border border-white/10 rounded-lg p-4">
                    <h3 className="text-white/80 text-sm font-medium mb-3">
                        Quick Add Talents
                    </h3>
                    <input
                        type="text"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
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
                                                onClick={addSelectedToPipeline}
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
                                                onToggle={toggleTalentSelect}
                                            />
                                        ))}
                                    </div>
                                </>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Bulk add modal */}
            {showBulkAdd && (
                <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50">
                    <div className="bg-gray-900 border border-white/20 rounded-lg p-6 w-full max-w-lg">
                        <h3 className="text-white text-lg mb-4">Bulk Add Talents</h3>
                        <p className="text-white/40 text-sm mb-3">
                            Enter talent IDs (one per line or comma-separated)
                        </p>
                        <textarea
                            value={bulkTalentsInput}
                            onChange={(e) => setBulkTalentsInput(e.target.value)}
                            placeholder={"tal_12345\ntal_67890\ntal_11111"}
                            data-testid="pipeline-bulk-input"
                            className="w-full h-40 bg-black/50 border border-white/20 rounded p-2 text-white mb-4 font-mono text-sm"
                            disabled={bulkAdding}
                        />
                        <div className="text-white/40 text-xs mb-4">
                            Supports UUIDs, custom IDs, or numeric IDs
                        </div>
                        <div className="flex gap-2 justify-end">
                            <button
                                onClick={() => setShowBulkAdd(false)}
                                disabled={bulkAdding}
                                className="px-4 py-2 bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={handleBulkAdd}
                                disabled={bulkAdding}
                                data-testid="pipeline-bulk-add-submit"
                                className="px-4 py-2 bg-green-500 hover:bg-green-600 text-white rounded transition-colors"
                            >
                                {bulkAdding ? "Adding…" : "Add Talents"}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Kanban */}
            <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-6 gap-4 overflow-x-auto">
                {STAGES.map((stage) => (
                    <Column
                        key={stage}
                        stage={stage}
                        items={data.filter((i) => i.stage === stage)}
                        refresh={fetchPipeline}
                        bulkMode={bulkMode}
                        bulkIds={bulkIds}
                        onToggleBulkSelect={toggleBulkSelect}
                    />
                ))}
            </div>
        </div>
    );
}

// ✅ Memoise. Parent (ProjectEdit) re-renders on every keystroke into its
// form fields; with primitive `projectId`, this subtree skips reconciliation
// until you navigate to a different project.
export default memo(ProjectPipeline);

/* --------------------------------------------------------------------- */
/* Subcomponents — all memoised so a single card move / search keystroke  */
/* doesn't re-render the entire kanban.                                   */
/* --------------------------------------------------------------------- */

const SearchResultRow = memo(function SearchResultRow({
    talent,
    selected,
    onToggle,
}) {
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
                <div className="flex-1 min-w-0">
                    <p className="text-white text-sm font-medium truncate">
                        {talent.name || "Unnamed Talent"}
                    </p>
                    {talent.email && (
                        <p className="text-white/40 text-xs truncate">
                            {talent.email}
                        </p>
                    )}
                    <p className="text-white/30 text-xs mt-0.5 tg-mono truncate">
                        ID: {talent.id}
                    </p>
                </div>
            </div>
        </div>
    );
});

const Column = memo(function Column({
    stage,
    items,
    refresh,
    bulkMode,
    bulkIds,
    onToggleBulkSelect,
}) {
    return (
        <div
            className="bg-black/40 border border-white/10 rounded-lg p-3 min-w-[200px]"
            data-testid={`pipeline-column-${stage}`}
        >
            <h3 className="text-xs font-semibold uppercase text-white/60 mb-3">
                {getStageLabel(stage)}
                <span className="ml-2 text-white/40">({items.length})</span>
            </h3>
            <div className="space-y-2 max-h-[calc(100vh-200px)] overflow-y-auto custom-scrollbar">
                {items.map((item) => (
                    <Card
                        key={item.id}
                        item={item}
                        refresh={refresh}
                        bulkMode={bulkMode}
                        isSelected={bulkIds.has(item.id)}
                        onToggleSelect={onToggleBulkSelect}
                    />
                ))}
                {items.length === 0 && (
                    <div className="text-white/20 text-xs text-center py-4">
                        No talents
                    </div>
                )}
            </div>
        </div>
    );
});

const Card = memo(function Card({
    item,
    refresh,
    bulkMode,
    isSelected,
    onToggleSelect,
}) {
    const [moving, setMoving] = useState(false);

    const move = async (stage) => {
        setMoving(true);
        try {
            await adminApi.patch("/pipeline/move", {
                ids: [item.id],
                stage,
            });
            await refresh();
        } catch (e) {
            console.error("Move failed:", e);
            toast.error(e?.response?.data?.detail || "Move failed");
        } finally {
            setMoving(false);
        }
    };

    const nextStages = NEXT_STAGE_FLOW[item.stage] || [];

    return (
        <div
            data-testid={`pipeline-card-${item.id}`}
            className={`bg-white/5 border rounded p-2 text-xs transition-all ${
                isSelected ? "border-blue-400 bg-blue-500/20" : "border-white/10"
            } ${moving ? "opacity-50" : ""}`}
        >
            {bulkMode ? (
                <div className="flex items-center gap-2">
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => onToggleSelect(item.id)}
                        className="w-4 h-4"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="font-mono text-white/90 truncate">
                            {item.talent_id}
                        </p>
                        {item.talent_name && (
                            <p className="text-white/60 truncate text-[10px] mt-0.5">
                                {item.talent_name}
                            </p>
                        )}
                    </div>
                </div>
            ) : (
                <>
                    <div className="mb-1">
                        <p className="font-mono text-white/90 font-medium truncate">
                            {item.talent_name || item.talent_id}
                        </p>
                        {item.talent_name && (
                            <p className="text-white/40 truncate text-[10px] mt-0.5 tg-mono">
                                ID: {item.talent_id}
                            </p>
                        )}
                    </div>

                    {item.email && (
                        <p className="text-white/40 truncate text-[10px]">
                            {item.email}
                        </p>
                    )}

                    {nextStages.length > 0 && (
                        <div className="flex gap-1 mt-2 flex-wrap">
                            {nextStages.map((stage) => (
                                <button
                                    key={stage}
                                    onClick={() => move(stage)}
                                    disabled={moving}
                                    data-testid={`pipeline-card-move-${item.id}-${stage}`}
                                    className="px-2 py-1 bg-white/10 hover:bg-white/20 rounded text-[10px] transition-colors"
                                >
                                    {getStageLabel(stage)}
                                </button>
                            ))}
                        </div>
                    )}

                    {item.stage === "locked" && (
                        <div className="mt-2 text-yellow-500/60 text-[10px]">
                            ✓ Finalized
                        </div>
                    )}

                    {(item.stage === "not_interested" ||
                        item.stage === "not_available") && (
                        <div className="mt-2 text-red-500/60 text-[10px]">
                            ✗ Rejected
                        </div>
                    )}
                </>
            )}
        </div>
    );
});
