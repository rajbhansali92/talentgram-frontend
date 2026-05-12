import React, { memo, useCallback, useState } from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";

import { usePipelineData } from "@/hooks/usePipelineData";
import { useTalentSearch } from "@/hooks/useTalentSearch";
import { usePipelineFilters } from "@/hooks/usePipelineFilters";
import { useBulkSelection } from "@/hooks/useBulkSelection";
import { usePipelineDnD } from "@/hooks/usePipelineDnD";

import PipelineToolbar from "./PipelineToolbar";
import PipelineFilters from "./PipelineFilters";
import PipelineColumn from "./PipelineColumn";
import { BoardSection, BoardRow } from "./PipelineBoardSection";
import BulkActionBar from "./BulkActionBar";
import FollowUpLane from "./FollowUpLane";
import { FilterEmptyState } from "./PipelineEmptyState";
import QuickAddTalents from "./QuickAddTalents";
import BulkAddModal from "./BulkAddModal";

import {
    INDEPENDENT_STAGES,
    MAIN_FLOW_STAGES,
    OUTCOME_STAGES,
    getStageLabel,
    normaliseStage,
} from "./constants";

function PipelineBoard({ projectId, projectName }) {
    const { data, setData, loading, error, fetchPipeline } = usePipelineData(projectId);
    const {
        searchQuery,
        setSearchQuery,
        searchResults,
        searchLoading,
        selectedTalents,
        toggleTalentSelect,
        resetSearch,
    } = useTalentSearch();

    const {
        search,
        setSearch,
        statusFocus,
        setStatusFocus,
        hasSubmission,
        setHasSubmission,
        hasIg,
        setHasIg,
        hiddenStages,
        clearAllFilters,
        filtersActive,
        filteredData,
        showOnlyFollowUp,
        hasZeroAfterFilter,
    } = usePipelineFilters(data);

    const {
        bulkIds,
        setBulkIds,
        bulkMode,
        setBulkMode,
        toggleBulkSelect,
        selectAllInColumn,
    } = useBulkSelection();

    const {
        dragSupported,
        dragId,
        handleCardDragStart,
        handleCardDragEnd,
        handleCardDrop,
    } = usePipelineDnD({ setData, refresh: fetchPipeline });

    const [showBulkAdd, setShowBulkAdd] = useState(false);
    const [bulkTalentsInput, setBulkTalentsInput] = useState("");
    const [bulkAdding, setBulkAdding] = useState(false);

    const handleToggleBulkMode = useCallback(() => {
        if (bulkMode) {
            setBulkMode(false);
            setBulkIds(new Set());
        } else {
            setBulkMode(true);
        }
    }, [bulkMode, setBulkMode, setBulkIds]);

    const handleClearBulk = useCallback(() => {
        setBulkIds(new Set());
        setBulkMode(false);
    }, [setBulkIds, setBulkMode]);

    const addSelectedToPipeline = async () => {
        if (selectedTalents.size === 0) return;
        try {
            await adminApi.post("/pipeline/add", {
                project_id: projectId,
                talent_ids: Array.from(selectedTalents),
            });
            const count = selectedTalents.size;
            resetSearch();
            await fetchPipeline();
            toast.success(`Added ${count} talent(s)`);
        } catch (e) {
            console.error("Failed to add talents:", e);
            toast.error(e?.response?.data?.detail || "Failed to add talents");
        }
    };

    const handleBulkAdd = async () => {
        const talentIds = bulkTalentsInput
            .split(/[\n,]/)
            .map((id) => id.trim())
            .filter((id) => id.length > 0 && id !== ",");

        if (talentIds.length === 0) {
            toast.error("Enter at least one talent ID");
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
            toast.success(`Added ${talentIds.length} talent(s)`);
        } catch (e) {
            console.error("Bulk add failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to add talents");
        } finally {
            setBulkAdding(false);
        }
    };

    const handleBulkMove = async (targetStage) => {
        if (bulkIds.size === 0) return;
        const count = bulkIds.size;
        try {
            await adminApi.patch("/pipeline/move", {
                ids: Array.from(bulkIds),
                stage: targetStage,
            });
            setBulkIds(new Set());
            setBulkMode(false);
            await fetchPipeline();
            toast.success(
                `Moved ${count} to ${getStageLabel(targetStage)}`,
            );
        } catch (e) {
            console.error("Bulk move failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to move talents");
        }
    };

    if (loading) {
        return (
            <div className="min-h-screen bg-[#f5f5f3] flex items-center justify-center" data-testid="pipeline-loading">
                <div className="text-black/45 text-sm tracking-wide">
                    Loading casting pipeline…
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div className="min-h-screen bg-[#f5f5f3] flex items-center justify-center" data-testid="pipeline-error">
                <div className="text-black/45 text-sm tracking-wide text-center">
                    <div>Failed to load casting pipeline</div>
                    <div className="text-xs mt-2 text-black/30">Please refresh or try again.</div>
                </div>
            </div>
        );
    }

    const columnCommons = {
        refresh: fetchPipeline,
        bulkMode,
        bulkIds,
        onToggleBulkSelect: toggleBulkSelect,
        onSelectAll: selectAllInColumn,
        dragSupported,
        dragId,
        onCardDragStart: handleCardDragStart,
        onCardDragEnd: handleCardDragEnd,
        onCardDrop: handleCardDrop,
    };

    const itemsForStage = (stage) =>
        filteredData.filter((i) => normaliseStage(i.stage) === stage);

    // Operational summary derived from data
    const activeCount = data.filter(i => !OUTCOME_STAGES.includes(normaliseStage(i.stage)) && normaliseStage(i.stage) !== 'archived').length;
    const shortlistedCount = data.filter(i => normaliseStage(i.stage) === 'shortlist').length;
    const approvedCount = data.filter(i => normaliseStage(i.stage) === 'approved').length;
    const pendingTestsCount = data.filter(i => normaliseStage(i.stage) === 'test_sent').length;

    // Display project name if provided, otherwise fallback to a cleaner placeholder
    const displayProjectName = projectName || (projectId && projectId.length > 8 ? `${projectId.slice(0, 8)}...` : projectId) || 'Active campaign';

    return (
        <div 
            className="min-h-screen bg-[#f5f5f3] px-5 py-5 pb-24"
            data-testid="project-pipeline"
        >
            <div className="max-w-[1680px] mx-auto">
                {/* Operational Header Hierarchy - refined typography */}
                <div className="mb-5">
                    <div className="flex items-baseline justify-between flex-wrap gap-3">
                        <div>
                            <h1 className="text-[22px] font-semibold text-black/85 tracking-[-0.01em]">Casting Pipeline</h1>
                            <div className="text-xs text-black/40 mt-0.5">Project · {displayProjectName}</div>
                        </div>
                        {/* Restrained operational summary indicators - monochrome */}
                        <div className="flex gap-5 text-xs">
                            <div className="flex items-center gap-1.5">
                                <span className="w-1.5 h-1.5 rounded-full bg-black/30"></span>
                                <span className="text-black/50">Active</span>
                                <span className="font-medium text-black/70 ml-1">{activeCount}</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                                <span className="w-1.5 h-1.5 rounded-full bg-black/30"></span>
                                <span className="text-black/50">Shortlisted</span>
                                <span className="font-medium text-black/70 ml-1">{shortlistedCount}</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                                <span className="w-1.5 h-1.5 rounded-full bg-black/30"></span>
                                <span className="text-black/50">Approved</span>
                                <span className="font-medium text-black/70 ml-1">{approvedCount}</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                                <span className="w-1.5 h-1.5 rounded-full bg-black/30"></span>
                                <span className="text-black/50">Pending Tests</span>
                                <span className="font-medium text-black/70 ml-1">{pendingTestsCount}</span>
                            </div>
                        </div>
                    </div>
                </div>

                {/* Unified Control Deck with operational styling - sticky with backdrop */}
                <div className="sticky top-0 z-40 bg-[#f5f5f3]/90 backdrop-blur-sm -mx-5 px-5 pt-2 pb-2">
                    <div className="bg-white border border-black/[0.06] rounded-xl shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
                        <div className="px-4 py-3">
                            <PipelineToolbar
                                projectId={projectId}
                                bulkMode={bulkMode}
                                onToggleBulkMode={handleToggleBulkMode}
                                onOpenBulkAdd={() => setShowBulkAdd(true)}
                            />
                        </div>
                        <div className="border-t border-black/[0.04] px-4 py-3">
                            <QuickAddTalents
                                searchQuery={searchQuery}
                                onSearchQueryChange={setSearchQuery}
                                searchLoading={searchLoading}
                                searchResults={searchResults}
                                selectedTalents={selectedTalents}
                                onToggleTalent={toggleTalentSelect}
                                onAddSelected={addSelectedToPipeline}
                            />
                        </div>
                        <div className="border-t border-black/[0.04] px-4 py-3">
                            <PipelineFilters
                                search={search}
                                onSearch={setSearch}
                                statusFocus={statusFocus}
                                onStatusFocus={setStatusFocus}
                                hasSubmission={hasSubmission}
                                onHasSubmission={setHasSubmission}
                                hasIg={hasIg}
                                onHasIg={setHasIg}
                                filtersActive={filtersActive}
                                onClearAll={clearAllFilters}
                                totalCount={data.length}
                                filteredCount={filteredData.length}
                            />
                        </div>
                    </div>
                </div>

                {/* Workflow Content with operational rhythm */}
                <div className="space-y-4 mt-6">
                    {hasZeroAfterFilter && (
                        <div className="mt-8">
                            <FilterEmptyState onReset={clearAllFilters} />
                        </div>
                    )}

                    {!hasZeroAfterFilter && !showOnlyFollowUp && (
                        <BoardSection
                            eyebrow="Pipeline"
                            helper={`${MAIN_FLOW_STAGES.length} stages`}
                        >
                            <BoardRow testid="pipeline-main-flow">
                                {MAIN_FLOW_STAGES.filter((s) => !hiddenStages.has(s)).map((stage) => (
                                    <PipelineColumn
                                        key={stage}
                                        stage={stage}
                                        items={itemsForStage(stage)}
                                        {...columnCommons}
                                    />
                                ))}
                            </BoardRow>
                        </BoardSection>
                    )}

                    {/* Supportive Follow-up Lane - clean, no opacity hacks */}
                    {!hasZeroAfterFilter && (
                        <div className="mt-2">
                            <FollowUpLane
                                items={filteredData.filter((i) => i.is_follow_up === true)}
                                refresh={fetchPipeline}
                            />
                        </div>
                    )}

                    {!hasZeroAfterFilter && !showOnlyFollowUp && (
                        <BoardSection eyebrow="Outcomes" muted>
                            <BoardRow testid="pipeline-outcomes">
                                {OUTCOME_STAGES.filter((s) => !hiddenStages.has(s)).map((stage) => (
                                    <PipelineColumn
                                        key={stage}
                                        stage={stage}
                                        items={itemsForStage(stage)}
                                        {...columnCommons}
                                    />
                                ))}
                            </BoardRow>
                        </BoardSection>
                    )}

                    {!hasZeroAfterFilter && !showOnlyFollowUp && (
                        <BoardSection eyebrow="Pitch" divider>
                            <BoardRow testid="pipeline-pitch">
                                {INDEPENDENT_STAGES.filter((s) => !hiddenStages.has(s)).map((stage) => (
                                    <PipelineColumn
                                        key={stage}
                                        stage={stage}
                                        items={itemsForStage(stage)}
                                        {...columnCommons}
                                    />
                                ))}
                            </BoardRow>
                        </BoardSection>
                    )}
                </div>

                <BulkActionBar
                    count={bulkIds.size}
                    onClear={handleClearBulk}
                    onMove={handleBulkMove}
                />
            </div>
        </div>
    );
}

export default memo(PipelineBoard);
