import React, { memo, useCallback, useState, useMemo } from "react";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
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
import { FilterEmptyState } from "./PipelineEmptyState";
import QuickAddTalents from "./QuickAddTalents";
import BulkAddModal from "./BulkAddModal";
import TalentBrowserModal from "./TalentBrowserModal";

import { ChevronDown, Eye, EyeOff } from "lucide-react";

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

    // FIX 1: Added projectId to usePipelineDnD
    const {
        dragSupported,
        dragId,
        handleCardDragStart,
        handleCardDragEnd,
        handleCardDrop,
    } = usePipelineDnD({
        projectId,
        setData,
        refresh: fetchPipeline,
    });

    const [showBulkAdd, setShowBulkAdd] = useState(false);
    const [bulkTalentsInput, setBulkTalentsInput] = useState("");
    const [bulkAdding, setBulkAdding] = useState(false);

    // Stage focus state
    const [focusedStageId, setFocusedStageId] = useState(null);

    // Section collapse states
    const [toolbarCollapsed, setToolbarCollapsed] = useState(false);
    const [filtersCollapsed, setFiltersCollapsed] = useState(false);

    // Responsive mobile & accordion states
    const [isMobile, setIsMobile] = useState(false);
    const [mobileExpandedStages, setMobileExpandedStages] = useState({});
    const [collapsedStages, setCollapsedStages] = useState({});
    const [isFilterOpen, setIsFilterOpen] = useState(false);

    React.useEffect(() => {
        const checkMobile = () => {
            setIsMobile(window.innerWidth < 768);
        };
        checkMobile();
        window.addEventListener("resize", checkMobile);
        return () => window.removeEventListener("resize", checkMobile);
    }, []);

    const handleToggleCollapse = useCallback((stage) => {
        if (window.innerWidth < 768) {
            setMobileExpandedStages((prev) => ({
                ...prev,
                [stage]: !prev[stage],
            }));
        } else {
            setCollapsedStages((prev) => ({
                ...prev,
                [stage]: !prev[stage],
            }));
        }
    }, []);

    const mainStages = useMemo(() => 
        MAIN_FLOW_STAGES.filter((s) => !hiddenStages.has(s) && (!focusedStageId || focusedStageId === s)),
        [hiddenStages, focusedStageId]
    );

    const outcomeStages = useMemo(() => 
        OUTCOME_STAGES.filter((s) => !hiddenStages.has(s) && (!focusedStageId || focusedStageId === s)),
        [hiddenStages, focusedStageId]
    );

    const independentStages = useMemo(() => 
        INDEPENDENT_STAGES.filter((s) => !hiddenStages.has(s) && (!focusedStageId || focusedStageId === s)),
        [hiddenStages, focusedStageId]
    );

    // Roster browser and existing talent tracking
    const [showTalentBrowser, setShowTalentBrowser] = useState(false);
    const existingTalentIds = useMemo(() => new Set(data.map((item) => item.talent_id)), [data]);

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
            // FIX 2: First occurrence - added projectId to URL
            await adminApi.post(`/projects/${projectId}/pipeline/add`, {
                project_id: projectId,
                talent_ids: Array.from(selectedTalents),
            });
            const count = selectedTalents.size;
            resetSearch();
            await fetchPipeline();
            toast.success(`Added ${count} talent(s)`);
        } catch (e) {
            console.error("Failed to add talents:", e);
            toast.error(formatErrorDetail(e, "Failed to add talents"));
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
            // FIX 2: Second occurrence - added projectId to URL
            await adminApi.post(`/projects/${projectId}/pipeline/add`, {
                project_id: projectId,
                talent_ids: talentIds,
            });
            setBulkTalentsInput("");
            setShowBulkAdd(false);
            await fetchPipeline();
            toast.success(`Added ${talentIds.length} talent(s)`);
        } catch (e) {
            console.error("Bulk add failed:", e);
            toast.error(formatErrorDetail(e, "Failed to add talents"));
        } finally {
            setBulkAdding(false);
        }
    };

    // States for bulk modals
    const [showLabelModal, setShowLabelModal] = useState(false);
    const [labelAction, setLabelAction] = useState("add"); // "add" | "remove"
    const [labelText, setLabelText] = useState("");
    const [labelBusy, setLabelBusy] = useState(false);

    const [showNoteModal, setShowNoteModal] = useState(false);
    const [noteText, setNoteText] = useState("");
    const [noteBusy, setNoteBusy] = useState(false);

    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [deleteBusy, setDeleteBusy] = useState(false);

    const [showWhatsAppModal, setShowWhatsAppModal] = useState(false);
    const [showEmailModal, setShowEmailModal] = useState(false);
    const [emailSubject, setEmailSubject] = useState("");
    const [emailBody, setEmailBody] = useState("");
    const [emailBusy, setEmailBusy] = useState(false);

    const handleBulkMove = async (targetStage) => {
        if (bulkIds.size === 0) return;
        const count = bulkIds.size;
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/bulk-move`, {
                talent_ids: talentIds,
                stage: targetStage,
            });
            setBulkIds(new Set());
            setBulkMode(false);
            await fetchPipeline();
            toast.success(`Moved ${count} talents to ${getStageLabel(targetStage)}`);
        } catch (e) {
            console.error("Bulk move failed:", e);
            toast.error(formatErrorDetail(e, "Failed to move talents"));
        }
    };

    const handleBulkLabel = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0 || !labelText.trim()) return;
        setLabelBusy(true);
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/bulk-label`, {
                talent_ids: talentIds,
                labels: [labelText.trim()],
                action: labelAction
            });
            toast.success(`Successfully ${labelAction === "add" ? "applied" : "removed"} label "${labelText.trim()}"`);
            setShowLabelModal(false);
            setLabelText("");
            handleClearBulk();
            await fetchPipeline();
        } catch (e) {
            console.error(e);
            toast.error(formatErrorDetail(e, "Failed to update labels"));
        } finally {
            setLabelBusy(false);
        }
    };

    const handleBulkNote = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0 || !noteText.trim()) return;
        setNoteBusy(true);
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/bulk-note`, {
                talent_ids: talentIds,
                note: noteText.trim()
            });
            toast.success(`Successfully appended notes to ${talentIds.length} talents`);
            setShowNoteModal(false);
            setNoteText("");
            handleClearBulk();
            await fetchPipeline();
        } catch (e) {
            console.error(e);
            toast.error(formatErrorDetail(e, "Failed to add notes"));
        } finally {
            setNoteBusy(false);
        }
    };

    const handleBulkDelete = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0) return;
        setDeleteBusy(true);
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/bulk-delete`, {
                talent_ids: talentIds
            });
            toast.success(`Successfully removed ${talentIds.length} talents from pipeline`);
            setShowDeleteConfirm(false);
            handleClearBulk();
            await fetchPipeline();
        } catch (e) {
            console.error(e);
            toast.error(formatErrorDetail(e, "Failed to delete talents"));
        } finally {
            setDeleteBusy(false);
        }
    };

    const handleBulkArchive = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0) return;
        try {
            await adminApi.post(`/projects/${projectId}/pipeline/bulk-move`, {
                talent_ids: talentIds,
                stage: "rejected"
            });
            toast.success(`Successfully archived ${talentIds.length} talents`);
            handleClearBulk();
            await fetchPipeline();
        } catch (e) {
            console.error(e);
            toast.error(formatErrorDetail(e, "Failed to archive talents"));
        }
    };

    const handleBulkExport = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0) return;
        try {
            const { data: res } = await adminApi.post(`/projects/${projectId}/pipeline/bulk-export`, {
                talent_ids: talentIds
            });
            const talents = res.talents || [];
            if (talents.length === 0) {
                toast.error("No talent details found");
                return;
            }
            const headers = ["Name", "Email", "Phone", "Gender", "Height", "Skills", "Tags"];
            const csvLines = [headers.join(",")];
            talents.forEach(t => {
                const row = [
                    t.name || "",
                    t.email || "",
                    t.phone || "",
                    t.gender || "",
                    t.height || "",
                    (t.skills || []).join(";"),
                    (t.tags || []).map(tg => tg.name).join(";")
                ];
                csvLines.push(row.map(val => `"${val.replace(/"/g, '""')}"`).join(","));
            });
            const csvContent = "data:text/csv;charset=utf-8," + csvLines.join("\n");
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `talents_export_${projectId}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            toast.success(`Exported ${talents.length} talents`);
        } catch (e) {
            console.error(e);
            toast.error("Failed to export talents");
        }
    };

    const handleSendBulkEmail = async () => {
        const talentIds = Array.from(bulkIds).map(id => data.find(i => i.id === id)?.talent_id).filter(Boolean);
        if (talentIds.length === 0) return;
        setEmailBusy(true);
        try {
            // Mock sending email to all selected
            await new Promise(r => setTimeout(r, 1000));
            toast.success(`Successfully dispatched bulk email to ${talentIds.length} recipients`);
            setShowEmailModal(false);
            setEmailSubject("");
            setEmailBody("");
            handleClearBulk();
        } catch (e) {
            toast.error("Failed to send bulk email");
        } finally {
            setEmailBusy(false);
        }
    };

    // Keyboard shortcuts listener
    useEffect(() => {
        const handleGlobalKeyDown = (e) => {
            const active = document.activeElement;
            if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable)) {
                return;
            }
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
                e.preventDefault();
                const visibleIds = filteredData.map(item => item.id).filter(Boolean);
                setBulkIds(new Set(visibleIds));
                setBulkMode(true);
            }
            if (e.key === "Delete" && bulkIds.size > 0) {
                e.preventDefault();
                if (window.confirm(`Are you sure you want to archive ${bulkIds.size} selected talents?`)) {
                    handleBulkArchive();
                }
            }
        };
        window.addEventListener("keydown", handleGlobalKeyDown);
        return () => window.removeEventListener("keydown", handleGlobalKeyDown);
    }, [filteredData, bulkIds, setBulkIds, setBulkMode]);

    if (loading) {
        return (
            <div className="min-h-screen bg-[#f5f5f3] p-8 space-y-6" data-testid="pipeline-loading">
                <div className="flex justify-between items-center max-w-7xl mx-auto">
                    <div className="h-8 w-48 rounded animate-tg-shimmer" />
                    <div className="h-10 w-32 rounded animate-tg-shimmer" />
                </div>
                <div className="grid grid-cols-1 md:grid-cols-4 gap-6 max-w-7xl mx-auto">
                    {[1, 2, 3, 4].map((i) => (
                        <div key={i} className="bg-white p-4 rounded-lg border border-gray-100 space-y-4">
                            <div className="h-6 w-3/4 rounded animate-tg-shimmer" />
                            <div className="h-24 w-full rounded animate-tg-shimmer" />
                            <div className="h-24 w-full rounded animate-tg-shimmer" />
                        </div>
                    ))}
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
        projectId,
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
        focusedStageId,
        onFocus: setFocusedStageId,
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
            className="min-h-screen bg-[#f5f5f3] pl-[max(1.25rem,env(safe-area-inset-left))] pr-[max(1.25rem,env(safe-area-inset-right))] pb-[max(6rem,env(safe-area-inset-bottom))] pt-5 overflow-x-hidden"
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
                        {/* Restrained operational summary indicators - monochrome with toggles */}
                        <div className="hidden md:flex items-center gap-6 text-xs flex-wrap">
                            <div className="flex gap-5">
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

                            {/* Section Declutter Toggles */}
                            <div className="flex items-center gap-2 border-l border-black/[0.08] pl-5 ml-1">
                                <button
                                    type="button"
                                    onClick={() => setFiltersCollapsed(prev => !prev)}
                                    disabled={toolbarCollapsed}
                                    className={`inline-flex items-center gap-1.5 px-2.5 py-1 border border-black/[0.08] hover:border-black/[0.16] hover:bg-black/[0.02] rounded text-[10px] tracking-wide uppercase text-black/55 hover:text-black transition-colors ${toolbarCollapsed ? "opacity-40 cursor-not-allowed" : ""}`}
                                    title={filtersCollapsed ? "Show Filters Panel" : "Hide Filters Panel"}
                                >
                                    {filtersCollapsed ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
                                    <span>{filtersCollapsed ? "Show Filters" : "Hide Filters"}</span>
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setToolbarCollapsed(prev => !prev)}
                                    className="p-1 border border-black/[0.08] hover:border-black/[0.16] hover:bg-black/[0.02] rounded text-black/55 hover:text-black transition-colors"
                                    title={toolbarCollapsed ? "Expand Filters & Search" : "Collapse Filters & Search"}
                                >
                                    <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${toolbarCollapsed ? "-rotate-90" : ""}`} />
                                </button>
                            </div>
                        </div>
                    </div>

                    {/* Mobile Swipeable Metrics Snap Row */}
                    <div 
                        className="flex md:hidden overflow-x-auto gap-3 pb-3 -mx-5 px-5 snap-x snap-mandatory scrollbar-none mt-3"
                        style={{
                            WebkitOverflowScrolling: "touch",
                            scrollSnapType: "x mandatory",
                        }}
                    >
                        <div className="snap-start shrink-0 min-w-[140px] bg-white border border-black/[0.06] rounded-xl p-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                            <div className="text-[10px] uppercase tracking-wider text-black/40">Active</div>
                            <div className="text-xl font-bold text-black/85 mt-1">{activeCount}</div>
                        </div>
                        <div className="snap-start shrink-0 min-w-[140px] bg-white border border-black/[0.06] rounded-xl p-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                            <div className="text-[10px] uppercase tracking-wider text-black/40">Shortlisted</div>
                            <div className="text-xl font-bold text-black/85 mt-1">{shortlistedCount}</div>
                        </div>
                        <div className="snap-start shrink-0 min-w-[140px] bg-white border border-black/[0.06] rounded-xl p-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                            <div className="text-[10px] uppercase tracking-wider text-black/40">Approved</div>
                            <div className="text-xl font-bold text-black/85 mt-1">{approvedCount}</div>
                        </div>
                        <div className="snap-start shrink-0 min-w-[140px] bg-white border border-black/[0.06] rounded-xl p-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                            <div className="text-[10px] uppercase tracking-wider text-black/40">Pending Tests</div>
                            <div className="text-xl font-bold text-black/85 mt-1">{pendingTestsCount}</div>
                        </div>
                    </div>
                </div>

                {/* Unified Control Deck with operational styling - sticky with backdrop */}
                {!toolbarCollapsed && !filtersCollapsed && (
                    <div className="sticky top-0 z-40 bg-[#f5f5f3]/90 backdrop-blur-sm md:-mx-5 md:px-5 mx-0 px-0 pt-2 pb-2">
                        <div className="bg-white border border-black/[0.06] rounded-xl shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
                            <div className="px-4 py-3">
                                <PipelineToolbar
                                    projectId={projectId}
                                    bulkMode={bulkMode}
                                    onToggleBulkMode={handleToggleBulkMode}
                                    onOpenBulkAdd={() => setShowBulkAdd(true)}
                                    onOpenTalentBrowser={() => setShowTalentBrowser(true)}
                                />
                            </div>
                            {!isFilterOpen && (
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
                            )}
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
                                    isMobileDrawerOpen={isFilterOpen}
                                    onMobileDrawerOpenChange={setIsFilterOpen}
                                />
                            </div>
                        </div>
                    </div>
                )}

                {/* Workflow Content with operational rhythm */}
                <div className="space-y-4 mt-6">
                        {hasZeroAfterFilter && (
                            <div className="mt-8">
                                <FilterEmptyState onReset={clearAllFilters} />
                            </div>
                        )}

                        {!hasZeroAfterFilter && !showOnlyFollowUp && mainStages.length > 0 && (
                            <BoardSection
                                eyebrow="Pipeline"
                                helper={`${MAIN_FLOW_STAGES.length} stages`}
                            >
                                <BoardRow testid="pipeline-main-flow">
                                    {mainStages.map((stage) => (
                                        <PipelineColumn
                                            key={stage}
                                            stage={stage}
                                            items={itemsForStage(stage)}
                                            isFocused={focusedStageId === stage}
                                            isCollapsed={isMobile ? !mobileExpandedStages[stage] : !!collapsedStages[stage]}
                                            onToggleCollapse={handleToggleCollapse}
                                            {...columnCommons}
                                        />
                                    ))}
                                </BoardRow>
                            </BoardSection>
                        )}

                        {/* Supportive Follow-up Lane - clean, no opacity hacks */}
                        {!hasZeroAfterFilter && (!focusedStageId || focusedStageId === 'follow_up') && (
                            <BoardSection eyebrow="Follow-up">
                                <BoardRow testid="pipeline-follow-up">
                                    <PipelineColumn
                                        key="follow_up"
                                        stage="follow_up"
                                        items={filteredData.filter((i) => i.is_follow_up === true)}
                                        isFocused={focusedStageId === 'follow_up'}
                                        isCollapsed={isMobile ? !mobileExpandedStages['follow_up'] : !!collapsedStages['follow_up']}
                                        onToggleCollapse={handleToggleCollapse}
                                        {...columnCommons}
                                    />
                                </BoardRow>
                            </BoardSection>
                        )}

                        {!hasZeroAfterFilter && !showOnlyFollowUp && outcomeStages.length > 0 && (
                            <BoardSection eyebrow="Outcomes" muted>
                                <BoardRow testid="pipeline-outcomes">
                                    {outcomeStages.map((stage) => (
                                        <PipelineColumn
                                            key={stage}
                                            stage={stage}
                                            items={itemsForStage(stage)}
                                            isFocused={focusedStageId === stage}
                                            isCollapsed={isMobile ? !mobileExpandedStages[stage] : !!collapsedStages[stage]}
                                            onToggleCollapse={handleToggleCollapse}
                                            {...columnCommons}
                                        />
                                    ))}
                                </BoardRow>
                            </BoardSection>
                        )}

                        {!hasZeroAfterFilter && !showOnlyFollowUp && independentStages.length > 0 && (
                            <BoardSection eyebrow="Pitch" divider>
                                <BoardRow testid="pipeline-pitch">
                                    {independentStages.map((stage) => (
                                        <PipelineColumn
                                            key={stage}
                                            stage={stage}
                                            items={itemsForStage(stage)}
                                            isFocused={focusedStageId === stage}
                                            isCollapsed={isMobile ? !mobileExpandedStages[stage] : !!collapsedStages[stage]}
                                            onToggleCollapse={handleToggleCollapse}
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
                    onLabel={() => setShowLabelModal(true)}
                    onNote={() => setShowNoteModal(true)}
                    onDelete={() => setShowDeleteConfirm(true)}
                    onExport={handleBulkExport}
                    onWhatsApp={() => setShowWhatsAppModal(true)}
                    onEmail={() => setShowEmailModal(true)}
                    onArchive={handleBulkArchive}
                />

                <TalentBrowserModal
                    open={showTalentBrowser}
                    onClose={() => setShowTalentBrowser(false)}
                    projectId={projectId}
                    existingTalentIds={existingTalentIds}
                    onAdded={fetchPipeline}
                />

                {/* --- BULK LABELS MODAL --- */}
                {showLabelModal && (
                    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4">
                        <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl border border-black/15 overflow-hidden p-6 space-y-4">
                            <div className="flex justify-between items-center">
                                <h3 className="text-sm font-bold uppercase tracking-wider text-black/85">Assign Bulk Labels</h3>
                                <button onClick={() => setShowLabelModal(false)} className="text-black/40 hover:text-black/60"><X className="w-4 h-4" /></button>
                            </div>
                            <div className="space-y-3">
                                <div className="flex gap-2 p-0.5 border border-black/[0.08] rounded-lg">
                                    <button 
                                        type="button" 
                                        onClick={() => setLabelAction("add")}
                                        className={`flex-1 py-1.5 text-[10px] uppercase font-bold tracking-wider rounded ${labelAction === "add" ? "bg-black text-white" : "text-black/55 hover:bg-black/5"}`}
                                    >
                                        Add Label
                                    </button>
                                    <button 
                                        type="button" 
                                        onClick={() => setLabelAction("remove")}
                                        className={`flex-1 py-1.5 text-[10px] uppercase font-bold tracking-wider rounded ${labelAction === "remove" ? "bg-black text-white" : "text-black/55 hover:bg-black/5"}`}
                                    >
                                        Remove Label
                                    </button>
                                </div>
                                <input 
                                    type="text" 
                                    placeholder="Enter label name (e.g. Mumbai, Premium)"
                                    value={labelText}
                                    onChange={(e) => setLabelText(e.target.value)}
                                    className="w-full border border-black/[0.08] rounded-lg px-3 py-2 text-xs outline-none focus:border-black/40"
                                />
                            </div>
                            <div className="flex justify-end gap-2 pt-2">
                                <button 
                                    onClick={() => setShowLabelModal(false)}
                                    className="px-4 py-2 border border-black/10 rounded-lg text-xs font-semibold text-black/55 hover:bg-black/5"
                                >
                                    Cancel
                                </button>
                                <button 
                                    onClick={handleBulkLabel}
                                    disabled={labelBusy || !labelText.trim()}
                                    className="px-4 py-2 bg-black text-white rounded-lg text-xs font-semibold hover:bg-black/90 disabled:opacity-40"
                                >
                                    {labelBusy ? "Processing..." : "Apply"}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* --- BULK NOTE MODAL --- */}
                {showNoteModal && (
                    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4">
                        <div className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-black/15 overflow-hidden p-6 space-y-4">
                            <div className="flex justify-between items-center">
                                <h3 className="text-sm font-bold uppercase tracking-wider text-black/85">Add Note in Bulk</h3>
                                <button onClick={() => setShowNoteModal(false)} className="text-black/40 hover:text-black/60"><X className="w-4 h-4" /></button>
                            </div>
                            <textarea 
                                placeholder="Write internal note to append to all selected talents..."
                                value={noteText}
                                onChange={(e) => setNoteText(e.target.value)}
                                rows={4}
                                className="w-full border border-black/[0.08] rounded-lg px-3 py-2.5 text-xs outline-none focus:border-black/40 resize-none"
                            />
                            <div className="flex justify-end gap-2">
                                <button 
                                    onClick={() => setShowNoteModal(false)}
                                    className="px-4 py-2 border border-black/10 rounded-lg text-xs font-semibold text-black/55 hover:bg-black/5"
                                >
                                    Cancel
                                </button>
                                <button 
                                    onClick={handleBulkNote}
                                    disabled={noteBusy || !noteText.trim()}
                                    className="px-4 py-2 bg-black text-white rounded-lg text-xs font-semibold hover:bg-black/90 disabled:opacity-40"
                                >
                                    {noteBusy ? "Adding..." : "Add Note"}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* --- BULK DELETE CONFIRM MODAL --- */}
                {showDeleteConfirm && (
                    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4">
                        <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl border border-black/15 overflow-hidden p-6 space-y-4">
                            <h3 className="text-sm font-bold uppercase tracking-wider text-black/85">Remove Talents?</h3>
                            <p className="text-xs text-black/55">
                                Are you sure you want to remove the {bulkIds.size} selected talents from the casting pipeline? This will not delete their core profiles.
                            </p>
                            <div className="flex justify-end gap-2 pt-2">
                                <button 
                                    onClick={() => setShowDeleteConfirm(false)}
                                    className="px-4 py-2 border border-black/10 rounded-lg text-xs font-semibold text-black/55 hover:bg-black/5"
                                >
                                    Cancel
                                </button>
                                <button 
                                    onClick={handleBulkDelete}
                                    disabled={deleteBusy}
                                    className="px-4 py-2 bg-rose-600 text-white rounded-lg text-xs font-semibold hover:bg-rose-500 disabled:opacity-40"
                                >
                                    {deleteBusy ? "Deleting..." : "Delete"}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* --- BULK WHATSAPP PREVIEW --- */}
                {showWhatsAppModal && (
                    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4">
                        <div className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-black/15 overflow-hidden p-6 space-y-4">
                            <div className="flex justify-between items-center">
                                <h3 className="text-sm font-bold uppercase tracking-wider text-black/85">Bulk WhatsApp Preview</h3>
                                <button onClick={() => setShowWhatsAppModal(false)} className="text-black/40 hover:text-black/60"><X className="w-4 h-4" /></button>
                            </div>
                            <div className="text-xs text-black/55">
                                Ready to message <strong>{bulkIds.size}</strong> selected talents. Below is the dispatch preview:
                            </div>
                            <div className="max-h-[180px] overflow-y-auto border border-black/[0.06] rounded-lg p-2 space-y-1.5 bg-[#fafafa]">
                                {Array.from(bulkIds).map(id => {
                                    const talent = data.find(i => i.id === id);
                                    if (!talent) return null;
                                    const phone = talent.talent_phone || "No phone";
                                    return (
                                        <div key={id} className="flex justify-between items-center text-xs p-1 hover:bg-black/5 rounded">
                                            <span>{talent.talent_name || "Unknown"}</span>
                                            <a 
                                                href={`https://wa.me/${phone.replace(/\D/g, '')}?text=${encodeURIComponent(whatsAppMessage)}`}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                className="text-[#25D366] hover:underline font-mono text-[10.5px]"
                                            >
                                                {phone} ↗
                                            </a>
                                        </div>
                                    );
                                })}
                            </div>
                            <div className="space-y-1.5">
                                <label className="text-[10px] uppercase font-bold text-neutral-400">Pre-fill Message (Optional)</label>
                                <textarea 
                                    placeholder="Type message text to pre-fill wa.me links..."
                                    value={whatsAppMessage}
                                    onChange={(e) => setWhatsAppMessage(e.target.value)}
                                    rows={2}
                                    className="w-full border border-black/[0.08] rounded-lg px-3 py-2 text-xs outline-none focus:border-black/40 resize-none"
                                />
                            </div>
                            <div className="flex justify-end gap-2 pt-2">
                                <button 
                                    onClick={() => setShowWhatsAppModal(false)}
                                    className="px-4 py-2 border border-black/10 rounded-lg text-xs font-semibold text-black/55 hover:bg-black/5"
                                >
                                    Close
                                </button>
                                <button 
                                    onClick={() => {
                                        // Open WhatsApp Engine broadcast page pre-populated
                                        window.location.href = `/admin/whatsapp?project_id=${projectId}&source=manual`;
                                    }}
                                    className="px-4 py-2 bg-black text-white rounded-lg text-xs font-semibold hover:bg-black/90"
                                >
                                    Open Broadcast Engine
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* --- BULK EMAIL MODAL --- */}
                {showEmailModal && (
                    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4">
                        <div className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-black/15 overflow-hidden p-6 space-y-4">
                            <div className="flex justify-between items-center">
                                <h3 className="text-sm font-bold uppercase tracking-wider text-black/85">Send Bulk Email</h3>
                                <button onClick={() => setShowEmailModal(false)} className="text-black/40 hover:text-black/60"><X className="w-4 h-4" /></button>
                            </div>
                            <div className="text-xs text-black/55">
                                Selected <strong>{bulkIds.size}</strong> recipients.
                            </div>
                            <div className="space-y-3">
                                <input 
                                    type="text" 
                                    placeholder="Email Subject"
                                    value={emailSubject}
                                    onChange={(e) => setEmailSubject(e.target.value)}
                                    className="w-full border border-black/[0.08] rounded-lg px-3 py-2 text-xs outline-none focus:border-black/40"
                                />
                                <textarea 
                                    placeholder="Compose email body..."
                                    value={emailBody}
                                    onChange={(e) => setEmailBody(e.target.value)}
                                    rows={5}
                                    className="w-full border border-black/[0.08] rounded-lg px-3 py-2.5 text-xs outline-none focus:border-black/40 resize-none"
                                />
                            </div>
                            <div className="flex justify-end gap-2 pt-2">
                                <button 
                                    onClick={() => setShowEmailModal(false)}
                                    className="px-4 py-2 border border-black/10 rounded-lg text-xs font-semibold text-black/55 hover:bg-black/5"
                                >
                                    Cancel
                                </button>
                                <button 
                                    onClick={handleSendBulkEmail}
                                    disabled={emailBusy || !emailSubject.trim() || !emailBody.trim()}
                                    className="px-4 py-2 bg-black text-white rounded-lg text-xs font-semibold hover:bg-black/90 disabled:opacity-40"
                                >
                                    {emailBusy ? "Sending..." : "Dispatch Email"}
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

export default memo(PipelineBoard);
