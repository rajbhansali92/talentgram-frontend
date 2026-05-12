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

function PipelineBoard({ projectId }) {
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
            <div className="flex items-center justify-center h-64" data-testid="pipeline-loading">
                <div className="text-white/30 text-xs animate-pulse">
                    Loading pipeline...
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center justify-center h-64" data-testid="pipeline-error">
                <div className="text-rose-400/50 text-xs">{error}</div>
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

    return (
        <div className="p-4" data-testid="project-pipeline">
            <PipelineToolbar
                projectId={projectId}
                bulkMode={bulkMode}
                onToggleBulkMode={handleToggleBulkMode}
                onOpenBulkAdd={() => setShowBulkAdd(true)}
            />

            <QuickAddTalents
                searchQuery={searchQuery}
                onSearchQueryChange={setSearchQuery}
                searchLoading={searchLoading}
                searchResults={searchResults}
                selectedTalents={selectedTalents}
                onToggleTalent={toggleTalentSelect}
                onAddSelected={addSelectedToPipeline}
            />

            {showBulkAdd && (
                <BulkAddModal
                    value={bulkTalentsInput}
                    onChange={setBulkTalentsInput}
                    busy={bulkAdding}
                    onCancel={() => setShowBulkAdd(false)}
                    onSubmit={handleBulkAdd}
                />
            )}

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

            {hasZeroAfterFilter && (
                <FilterEmptyState onReset={clearAllFilters} />
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

            {!hasZeroAfterFilter && (
                <FollowUpLane
                    items={filteredData.filter((i) => i.is_follow_up === true)}
                    refresh={fetchPipeline}
                />
            )}

            {!hasZeroAfterFilter && !showOnlyFollowUp && (
                <BoardSection eyebrow="Archived" muted>
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
                <BoardSection eyebrow="Sourcing" divider>
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

            <BulkActionBar
                count={bulkIds.size}
                onClear={handleClearBulk}
                onMove={handleBulkMove}
            />
        </div>
    );
}

export default memo(PipelineBoard);
