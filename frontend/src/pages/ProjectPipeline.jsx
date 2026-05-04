import { useEffect, useState } from "react";
import axios from "axios";

const STAGES = [
  "ask_to_test",
  "sent",
  "shortlisted",
  "locked",
  "not_interested",
  "not_available",
];

const getStageLabel = (stage) => {
  const labels = {
    ask_to_test: "ASK TO TEST",
    sent: "SENT",
    shortlisted: "SHORTLISTED",
    locked: "LOCKED",
    not_interested: "NOT INTERESTED",
    not_available: "NOT AVAILABLE",
  };
  return labels[stage] || stage.replaceAll("_", " ").toUpperCase();
};

// ✅ FIXED: Accept projectId as prop instead of using useParams
export default function ProjectPipeline({ projectId }) {
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(true);
  const [bulkIds, setBulkIds] = useState(new Set());
  const [bulkMode, setBulkMode] = useState(false);
  const [showBulkAdd, setShowBulkAdd] = useState(false);
  const [bulkTalentsInput, setBulkTalentsInput] = useState("");
  const [bulkAdding, setBulkAdding] = useState(false);
  const [error, setError] = useState(null);
  
  // Search State
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [selectedTalents, setSelectedTalents] = useState(new Set());

  const fetchPipeline = async () => {
    if (!projectId) return;
    
    try {
      setError(null);
      const res = await axios.get(`/pipeline/project/${projectId}`);
      setData(res.data.data || []);
    } catch (error) {
      console.error("Failed to fetch pipeline:", error);
      setError("Failed to load pipeline data");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (projectId) {
      fetchPipeline();
    }
  }, [projectId]);

  // SEARCH FUNCTION with /api prefix
  const searchTalents = async (q) => {
    if (!q) {
      setSearchResults([]);
      return;
    }

    setSearchLoading(true);
    try {
      const res = await axios.get(`/api/talents/search?q=${encodeURIComponent(q)}`);
      setSearchResults(res.data.data || []);
    } catch (e) {
      console.error("Search failed", e);
      setSearchResults([]);
    } finally {
      setSearchLoading(false);
    }
  };

  // Debounce search to avoid too many requests
  useEffect(() => {
    const timer = setTimeout(() => {
      if (searchQuery) {
        searchTalents(searchQuery);
      } else {
        setSearchResults([]);
      }
    }, 300);

    return () => clearTimeout(timer);
  }, [searchQuery]);

  // SELECT TOGGLE
  const toggleTalentSelect = (id) => {
    const newSet = new Set(selectedTalents);
    if (newSet.has(id)) newSet.delete(id);
    else newSet.add(id);
    setSelectedTalents(newSet);
  };

  // ADD TO PIPELINE
  const addSelectedToPipeline = async () => {
    if (selectedTalents.size === 0) return;

    try {
      await axios.post("/pipeline/add", {
        project_id: projectId,
        talent_ids: Array.from(selectedTalents),
      });

      setSelectedTalents(new Set());
      setSearchResults([]);
      setSearchQuery("");

      fetchPipeline();
      alert(`Added ${selectedTalents.size} talent(s) to pipeline`);
    } catch (error) {
      console.error("Failed to add talents:", error);
      alert("Failed to add talents to pipeline");
    }
  };

  const handleBulkAdd = async () => {
    const talentIds = bulkTalentsInput
      .split(/[\n,]/)
      .map(id => id.trim())
      .filter(id => id.length > 0 && id !== ',');

    if (talentIds.length === 0) {
      alert("Please enter at least one talent ID");
      return;
    }

    setBulkAdding(true);
    try {
      await axios.post("/pipeline/add", {
        project_id: projectId,
        talent_ids: talentIds,
      });
      
      setBulkTalentsInput("");
      setShowBulkAdd(false);
      await fetchPipeline();
      alert(`Successfully added ${talentIds.length} talent(s)`);
    } catch (error) {
      console.error("Bulk add failed:", error);
      const message = error.response?.data?.message || "Failed to add talents";
      alert(message);
    } finally {
      setBulkAdding(false);
    }
  };

  const handleBulkMove = async (targetStage) => {
    if (bulkIds.size === 0) return;

    const confirmed = confirm(
      `Move ${bulkIds.size} talent(s) to ${getStageLabel(targetStage)}?`
    );
    if (!confirmed) return;

    try {
      await axios.patch("/pipeline/move", {
        ids: Array.from(bulkIds),
        stage: targetStage,
      });
      
      setBulkIds(new Set());
      setBulkMode(false);
      await fetchPipeline();
    } catch (error) {
      console.error("Bulk move failed:", error);
      alert("Failed to move talents");
    }
  };

  const toggleBulkSelect = (id) => {
    const newSet = new Set(bulkIds);
    if (newSet.has(id)) {
      newSet.delete(id);
    } else {
      newSet.add(id);
    }
    setBulkIds(newSet);
  };

  const clearBulkSelection = () => {
    setBulkIds(new Set());
    setBulkMode(false);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-white/60">Loading pipeline...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-red-400">{error}</div>
      </div>
    );
  }

  return (
    <div className="p-4">
      {/* Header with actions */}
      <div className="mb-4 flex justify-between items-center">
        <div>
          <h2 className="text-white font-semibold">Casting Pipeline</h2>
          <p className="text-white/40 text-sm mt-1">
            Project ID: {projectId}
          </p>
        </div>
        <div className="flex gap-2">
          {bulkMode ? (
            <>
              <button
                onClick={clearBulkSelection}
                className="px-3 py-1 text-sm bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
              >
                Cancel ({bulkIds.size} selected)
              </button>
              {STAGES.slice(0, 4).map((stage) => (
                <button
                  key={stage}
                  onClick={() => handleBulkMove(stage)}
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
                className="px-3 py-1 text-sm bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
              >
                Bulk Select
              </button>
              <button
                onClick={() => setShowBulkAdd(true)}
                className="px-3 py-1 text-sm bg-green-500/20 hover:bg-green-500/30 text-green-300 rounded transition-colors"
              >
                + Bulk Add
              </button>
            </>
          )}
        </div>
      </div>

      {/* SEARCH UI SECTION - Above Kanban */}
      <div className="mb-6">
        <div className="bg-black/40 border border-white/10 rounded-lg p-4">
          <h3 className="text-white/80 text-sm font-medium mb-3">
            🔍 Quick Add Talents
          </h3>
          
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by name or email..."
            className="w-full bg-black/50 border border-white/20 rounded px-3 py-2 text-white placeholder-white/40 focus:outline-none focus:border-white/40"
          />

          {/* Search Results */}
          {searchQuery && (
            <div className="mt-4">
              {searchLoading && (
                <div className="text-white/60 text-sm text-center py-4">
                  Searching...
                </div>
              )}
              
              {!searchLoading && searchResults.length === 0 && searchQuery && (
                <div className="text-white/40 text-sm text-center py-4">
                  No talents found matching "{searchQuery}"
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
                        className="text-xs bg-green-500/20 hover:bg-green-500/30 text-green-300 px-2 py-1 rounded"
                      >
                        Add {selectedTalents.size}
                      </button>
                    )}
                  </div>
                  
                  <div className="space-y-2 max-h-64 overflow-y-auto">
                    {searchResults.map((talent) => (
                      <div
                        key={talent.id}
                        className={`bg-white/5 border rounded p-2 cursor-pointer transition-all ${
                          selectedTalents.has(talent.id)
                            ? "border-blue-400 bg-blue-500/20"
                            : "border-white/10 hover:bg-white/10"
                        }`}
                        onClick={() => toggleTalentSelect(talent.id)}
                      >
                        <div className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            checked={selectedTalents.has(talent.id)}
                            onChange={() => {}}
                            onClick={(e) => e.stopPropagation()}
                            className="w-4 h-4"
                          />
                          <div className="flex-1">
                            <p className="text-white text-sm font-medium">
                              {talent.name || "Unnamed Talent"}
                            </p>
                            {talent.email && (
                              <p className="text-white/40 text-xs">{talent.email}</p>
                            )}
                            <p className="text-white/30 text-xs mt-0.5">
                              ID: {talent.id}
                            </p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Bulk Add Modal */}
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
              placeholder="Example:&#10;tal_12345&#10;tal_67890&#10;tal_11111"
              className="w-full h-40 bg-black/50 border border-white/20 rounded p-2 text-white mb-4 font-mono text-sm"
              disabled={bulkAdding}
            />
            <div className="text-white/40 text-xs mb-4">
              💡 Supports UUIDs, custom IDs, or numeric IDs
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setShowBulkAdd(false)}
                className="px-4 py-2 bg-white/10 hover:bg-white/20 text-white rounded transition-colors"
                disabled={bulkAdding}
              >
                Cancel
              </button>
              <button
                onClick={handleBulkAdd}
                className="px-4 py-2 bg-green-500 hover:bg-green-600 text-white rounded transition-colors"
                disabled={bulkAdding}
              >
                {bulkAdding ? "Adding..." : "Add Talents"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kanban Board */}
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

// Column Component
function Column({ stage, items, refresh, bulkMode, bulkIds, onToggleBulkSelect }) {
  return (
    <div className="bg-black/40 border border-white/10 rounded-lg p-3 min-w-[200px]">
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
            onToggleSelect={() => onToggleBulkSelect(item.id)}
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
}

// Card Component
function Card({ item, refresh, bulkMode, isSelected, onToggleSelect }) {
  const [moving, setMoving] = useState(false);

  const move = async (stage) => {
    setMoving(true);
    try {
      await axios.patch("/pipeline/move", {
        ids: [item.id],
        stage: stage,
      });
      refresh();
    } catch (error) {
      console.error("Move failed:", error);
    } finally {
      setMoving(false);
    }
  };

  const getNextStages = () => {
    const flow = {
      ask_to_test: ["sent", "not_interested", "not_available"],
      sent: ["shortlisted", "not_interested", "not_available"],
      shortlisted: ["locked", "not_interested", "not_available"],
      locked: [],
      not_interested: [],
      not_available: [],
    };
    return flow[item.stage] || [];
  };

  const nextStages = getNextStages();

  return (
    <div
      className={`bg-white/5 border rounded p-2 text-xs transition-all ${
        isSelected ? "border-blue-400 bg-blue-500/20" : "border-white/10"
      } ${moving ? "opacity-50" : ""}`}
    >
      {bulkMode ? (
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={isSelected}
            onChange={onToggleSelect}
            className="w-4 h-4"
          />
          <div className="flex-1">
            <p className="font-mono text-white/90">{item.talent_id}</p>
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
            <p className="font-mono text-white/90 font-medium">
              {item.talent_name || item.talent_id}
            </p>
            {item.talent_name && (
              <p className="text-white/40 truncate text-[10px] mt-0.5">
                ID: {item.talent_id}
              </p>
            )}
          </div>
          
          {item.email && (
            <p className="text-white/40 truncate text-[10px]">{item.email}</p>
          )}

          {nextStages.length > 0 && (
            <div className="flex gap-1 mt-2 flex-wrap">
              {nextStages.map((stage) => (
                <button
                  key={stage}
                  onClick={() => move(stage)}
                  disabled={moving}
                  className="px-2 py-1 bg-white/10 hover:bg-white/20 rounded text-[10px] transition-colors"
                >
                  {getStageLabel(stage)}
                </button>
              ))}
            </div>
          )}

          {item.stage === "locked" && (
            <div className="mt-2 text-yellow-500/60 text-[10px] flex items-center gap-1">
              <span>✓</span> Finalized
            </div>
          )}

          {(item.stage === "not_interested" || item.stage === "not_available") && (
            <div className="mt-2 text-red-500/60 text-[10px] flex items-center gap-1">
              <span>✗</span> Rejected
            </div>
          )}
        </>
      )}
    </div>
  );
}
