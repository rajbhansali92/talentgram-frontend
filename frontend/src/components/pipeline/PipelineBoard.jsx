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

/**
 * PipelineBoard — orchestration root.
 *
 * Composes the data hook, filter hook, bulk-selection hook, DnD hook,
 * and talent-search hook, then wires them into the cinematic board
 * layout. The component itself is intentionally thin — every visual
 * concern lives in a dedicated component under /components/pipeline/.
 */
function PipelineBoard({ projectId }) {
    // ---- data ---------------------------------------------------------
    const { data, setData, loading, error, fetchPipeline } = usePipelineData(projectId);

    // ---- talent search (Quick Add) ------------------------------------
    const {
        searchQuery,
        setSearchQuery,
        searchResults,
        searchLoading,
        selectedTalents,
        toggleTalentSelect,
        resetSearch,
    } = useTalentSearch();

    // ---- filters ------------------------------------------------------
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

    // ---- bulk selection ----------------------------------------------
    const {
        bulkIds,
        setBulkIds,
        bulkMode,
        setBulkMode,
        toggleBulkSelect,
        selectAllInColumn,
    } = useBulkSelection();

    // ---- drag & drop -------------------------------------------------
    const {
        dragSupported,
        dragId,
        handleCardDragStart,
        handleCardDragEnd,
        handleCardDrop,
    } = usePipelineDnD({ setData, refresh: fetchPipeline });

    // ---- bulk-add modal ----------------------------------------------
    const [showBulkAdd, setShowBulkAdd] = useState(false);
    const [bulkTalentsInput, setBulkTalentsInput] = useState("");
    const [bulkAdding, setBulkAdding] = useState(false);

    // ---- handlers ----------------------------------------------------
    const handleToggleBulkMode = useCallback(() => {
        // Leaving bulk mode also clears the selection so it's a single
        // mental action.
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
            toast.success(`Added ${count} talent(s) to pipeline`);
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
        // The deliberate click on a floating pill is itself the confirm —
        // no `window.confirm` (it blocked the main thread and felt jarring).
        // Errors are toasted; failed moves keep the selection intact so the
        // user can retry or cancel cleanly.
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
                `Moved ${count} ${count === 1 ? "talent" : "talents"} to ${getStageLabel(targetStage)}`,
            );
        } catch (e) {
            console.error("Bulk move failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to move talents");
        }
    };

    // ---- early returns ------------------------------------------------
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

    // Shared column props — cuts the prop-drilling repetition across the
    // three rendered sections.
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

            {/* Sticky cinematic control bar (PATCH 4E) */}
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

            {/* Empty filter state — replaces board sections when active
                filters resolve to zero matches. */}
            {hasZeroAfterFilter && (
                <FilterEmptyState onReset={clearAllFilters} />
            )}

            {/* Main flow — hidden when showOnlyFollowUp or empty. */}
            {!hasZeroAfterFilter && !showOnlyFollowUp && (
                <BoardSection
                    eyebrow="Main flow"
                    helper={`${MAIN_FLOW_STAGES.length} stages · progression funnel`}
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

            {/* Follow-up — virtual read-only lane. Always shown unless the
                board was filtered to zero results. */}
            {!hasZeroAfterFilter && (
                <FollowUpLane
                    items={filteredData.filter((i) => i.is_follow_up === true)}
                    refresh={fetchPipeline}
                />
            )}

            {/* Outcomes — terminal states. Visually de-emphasised. */}
            {!hasZeroAfterFilter && !showOnlyFollowUp && (
                <BoardSection eyebrow="Outcomes" helper="Terminal states" muted>
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

            {/* Pitch — independent sourcing lane. */}
            {!hasZeroAfterFilter && !showOnlyFollowUp && (
                <BoardSection
                    eyebrow="Pitch"
                    helper="Sourcing · independent of funnel"
                    divider
                >
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

            {/* Floating cinematic bulk action bar (PATCH 4C). Mounted last
                so it sits on top of everything via z-index. */}
            <BulkActionBar
                count={bulkIds.size}
                onClear={handleClearBulk}
                onMove={handleBulkMove}
            />
        </div>
    );
}

// Memoise. Parent (ProjectEdit) re-renders on every keystroke into its
// form fields; with primitive `projectId`, this subtree skips
// reconciliation until you navigate to a different project.
export default memo(PipelineBoard);
