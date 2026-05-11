import React, { memo, useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";

/* ---------------------------------------------------------------------
 * Stage registry — single source of truth for the kanban.
 *
 * `MAIN_FLOW_STAGES`     : the progression funnel (rendered first)
 * `OUTCOME_STAGES`       : terminal states (rendered after the funnel)
 * `INDEPENDENT_STAGES`   : `pitch` lives in its own section — not part of
 *                          the progression flow, sourcing-only
 * `PIPELINE_STAGE_ORDER` : flat array used by the kanban grid renderer
 *
 * Legacy `sent` is folded into `approved` at read-time so any stored
 * row with stage="sent" renders into the Approved column without a
 * data migration.
 * ------------------------------------------------------------------- */
const MAIN_FLOW_STAGES = [
    "ask_to_test",
    "approved",
    "hold",
    "shortlisted",
    "already_tested",
    "locked",
];

const OUTCOME_STAGES = ["rejected", "not_available", "not_interested"];

const INDEPENDENT_STAGES = ["pitch"];

const PIPELINE_STAGE_ORDER = [
    ...MAIN_FLOW_STAGES,
    ...OUTCOME_STAGES,
    ...INDEPENDENT_STAGES,
];

// Bulk-action toolbar exposes only the funnel destinations (no
// terminal/independent stages — those are still reachable via per-card
// buttons but rarely needed in bulk).
const BULK_MOVE_TARGETS = ["ask_to_test", "approved", "shortlisted", "locked"];

const LEGACY_STAGE_ALIASES = {
    sent: "approved",
};

const normaliseStage = (raw) => {
    if (!raw) return raw;
    const s = String(raw).trim().toLowerCase();
    return LEGACY_STAGE_ALIASES[s] || s;
};

const STAGE_LABELS = {
    ask_to_test: "ASK TO TEST",
    approved: "APPROVED",
    hold: "HOLD",
    shortlisted: "SHORTLISTED",
    already_tested: "ALREADY TESTED",
    locked: "LOCKED",
    rejected: "REJECTED",
    not_available: "NOT AVAILABLE",
    not_interested: "NOT INTERESTED",
    pitch: "PITCH",
    // Virtual read-only lane (PATCH 3C). Never stored in DB.
    follow_up: "FOLLOW-UP",
};

// Stable references used by the read-only follow-up lane. Defining them
// at module scope (not inside render) keeps the `memo` comparators on
// Column/Card from invalidating every render.
// Per-stage accent colours. Used as a thin top bar on the column header
// (cinematic stage indicator). Kept muted on purpose — no neon, no rainbow.
// Outcome lanes share a deep slate, follow-up gets a soft amber pulse so it
// reads as "attention needed" without screaming.
const STAGE_ACCENTS = {
    ask_to_test: "from-sky-300/60 to-sky-500/0",
    approved: "from-emerald-300/60 to-emerald-500/0",
    hold: "from-amber-300/60 to-amber-500/0",
    shortlisted: "from-violet-300/60 to-violet-500/0",
    already_tested: "from-fuchsia-300/60 to-fuchsia-500/0",
    locked: "from-yellow-200/70 to-yellow-500/0",
    rejected: "from-rose-300/40 to-rose-500/0",
    not_available: "from-zinc-300/30 to-zinc-500/0",
    not_interested: "from-zinc-300/30 to-zinc-500/0",
    pitch: "from-teal-300/60 to-teal-500/0",
    follow_up: "from-amber-300/70 to-amber-500/0",
};
const DEFAULT_ACCENT = "from-white/30 to-white/0";

// Cinematic empty-state copy keyed by stage. Falls back to a generic line.
const EMPTY_STATE_COPY = {
    ask_to_test: "Awaiting first invitations",
    approved: "No approvals yet",
    hold: "Nothing on hold",
    shortlisted: "Empty shortlist",
    already_tested: "No prior tests",
    locked: "Not finalised yet",
    rejected: "Cleanly clear",
    not_available: "Everyone's available",
    not_interested: "All in",
    pitch: "No pitches in flight",
    follow_up: "All caught up",
};

const EMPTY_BULK_SET = new Set();
const NOOP = () => {};

/* ---------------------------------------------------------------------
 * Status tones (PATCH 4B)
 * Used by the Card footer for terminal/locked states. All tones stay
 * muted on purpose — luxury, not dashboard. Borders are 8-12% opacity,
 * backgrounds 5-8%, text 60-70%.
 * ------------------------------------------------------------------- */
const STATUS_TONES = {
    locked: {
        // Elegant finalised state — soft gold, no neon.
        label: "Finalised",
        dot: "bg-yellow-200/80",
        text: "text-yellow-200/75",
        chip: "border-yellow-200/15 bg-yellow-200/[0.04]",
    },
    approved: {
        label: "Approved",
        dot: "bg-emerald-300/80",
        text: "text-emerald-300/75",
        chip: "border-emerald-300/15 bg-emerald-300/[0.04]",
    },
    hold: {
        label: "On hold",
        dot: "bg-amber-300/80",
        text: "text-amber-200/75",
        chip: "border-amber-300/15 bg-amber-300/[0.04]",
    },
    rejected: {
        label: "Rejected",
        dot: "bg-rose-300/70",
        text: "text-rose-300/70",
        chip: "border-rose-300/15 bg-rose-300/[0.04]",
    },
    not_available: {
        label: "Not available",
        dot: "bg-zinc-300/60",
        text: "text-zinc-300/65",
        chip: "border-zinc-300/15 bg-zinc-300/[0.04]",
    },
    not_interested: {
        label: "Not interested",
        dot: "bg-zinc-300/60",
        text: "text-zinc-300/65",
        chip: "border-zinc-300/15 bg-zinc-300/[0.04]",
    },
};

// Per-stage next-step suggestions for the card action buttons. `pitch`,
// terminal stages, and `locked` are intentionally empty — no automatic
// onward transitions, but admins can still bulk-move via the toolbar.
const NEXT_STAGE_FLOW = {
    ask_to_test: ["approved", "not_interested", "not_available"],
    approved: ["shortlisted", "hold", "rejected"],
    hold: ["approved", "shortlisted", "rejected"],
    shortlisted: ["locked", "already_tested", "rejected"],
    already_tested: ["shortlisted", "locked", "rejected"],
    locked: [],
    rejected: [],
    not_available: [],
    not_interested: [],
    pitch: ["ask_to_test", "rejected"],
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

    /* -----------------------------------------------------------------
     * Drag & Drop (PATCH 4D) — native HTML5, no library.
     *
     * Architecture:
     *   • `dragId` state — which pipeline row id is currently being
     *     dragged. Stored at the parent so every Column can render its
     *     own drag-over highlight without prop-drilling complex state.
     *   • `dragSupported` — gated by `matchMedia('(hover:hover) and
     *     (pointer:fine)')` so touch devices fall back to taps + buttons.
     *     This is the cleanest way to disable DnD on mobile while keeping
     *     the rest of the UX intact.
     *   • `handleCardDrop(targetStage, droppedId)` — optimistic update:
     *     mutate local `data` in-place (set new stage), call backend,
     *     refetch on failure to roll back cleanly.
     * --------------------------------------------------------------- */
    const [dragId, setDragId] = useState(null);

    const dragSupported =
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(hover: hover) and (pointer: fine)").matches;

    const handleCardDragStart = useCallback((id) => {
        setDragId(id);
    }, []);

    const handleCardDragEnd = useCallback(() => {
        setDragId(null);
    }, []);

    const handleCardDrop = useCallback(
        async (targetStage, droppedId) => {
            // Hard-clear drag state up front — independent of network result.
            setDragId(null);
            if (!droppedId || !targetStage) return;

            // Capture pre-move snapshot for clean rollback if backend fails.
            let snapshot = null;
            let toastUndo = null;
            setData((prev) => {
                snapshot = prev;
                const row = prev.find((r) => r.id === droppedId);
                if (!row) return prev;
                const current = normaliseStage(row.stage);
                if (current === targetStage) return prev; // no-op drops
                toastUndo = `Moving ${row.talent_name || "talent"} → ${getStageLabel(
                    targetStage,
                )}`;
                // Functional setter: clone the row with the new stage; leave
                // other rows untouched so memoised Cards skip re-render.
                return prev.map((r) =>
                    r.id === droppedId ? { ...r, stage: targetStage } : r,
                );
            });
            if (!toastUndo) return; // no-op drop (same stage or row not found)

            try {
                await adminApi.patch("/pipeline/move", {
                    ids: [droppedId],
                    stage: targetStage,
                });
                // Soft confirmation — single line, no spam.
                toast.success(
                    `Moved to ${getStageLabel(targetStage)}`,
                );
                // Refresh in background to pick up `is_follow_up`
                // recomputation + updated_at — no await so the drop feels
                // instant.
                fetchPipeline();
            } catch (e) {
                console.error("Drag move failed:", e);
                // Roll back to the pre-drop snapshot. Cheap because we
                // captured the exact reference before mutation.
                if (snapshot) setData(snapshot);
                toast.error(
                    e?.response?.data?.detail || "Move failed — reverted",
                );
            }
        },
        [fetchPipeline],
    );

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

    // Select-all-in-column — passed down to Column header. Toggles intelligently:
    // if every visible row in that column is already selected, deselect all
    // those rows; otherwise add them to the selection set. The function is
    // pure (no closure over column items) — Column passes its own items in.
    const selectAllInColumn = useCallback((items) => {
        // Filter defensively — Column already filters out readOnly lanes
        // before invoking, but a defensive check keeps the contract robust.
        const visibleIds = (items || []).map((i) => i.id).filter(Boolean);
        if (visibleIds.length === 0) return;
        // Enter bulk mode automatically — saves a click.
        setBulkMode(true);
        setBulkIds((prev) => {
            const next = new Set(prev);
            const allSelected = visibleIds.every((id) => next.has(id));
            if (allSelected) {
                // Toggle off: deselect just this column's visible rows.
                visibleIds.forEach((id) => next.delete(id));
            } else {
                visibleIds.forEach((id) => next.add(id));
            }
            return next;
        });
    }, []);

    // ESC clears selection — only attached when a selection exists, so the
    // global key listener doesn't fight other shortcuts on the page when
    // bulk mode is idle. Clicking outside the toolbar does NOT clear (per
    // spec: only ESC is a destructive shortcut).
    useEffect(() => {
        if (bulkIds.size === 0) return;
        const onKey = (e) => {
            if (e.key === "Escape") {
                setBulkIds(new Set());
                setBulkMode(false);
            }
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [bulkIds.size]);

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
            {/* Header — slimmed in Patch 4C. The bulk action surface
                lives in a floating bottom-center toolbar that only
                appears when there's a selection. Header now only carries
                the two persistent entry points: enter bulk-mode, open
                the bulk-add modal. */}
            <div className="mb-4 flex justify-between items-start flex-wrap gap-3">
                <div>
                    <h2 className="text-white font-semibold tracking-tight">
                        Casting Pipeline
                    </h2>
                    <p className="text-white/40 text-[11px] mt-1 tg-mono">
                        Project ID: {projectId}
                    </p>
                </div>
                <div className="flex gap-2 flex-wrap">
                    <button
                        type="button"
                        onClick={() => {
                            // Toggle: leaving bulk mode also clears the
                            // selection so it's a single mental action.
                            if (bulkMode) {
                                setBulkMode(false);
                                setBulkIds(new Set());
                            } else {
                                setBulkMode(true);
                            }
                        }}
                        data-testid="pipeline-bulk-mode"
                        aria-pressed={bulkMode}
                        className={`
                            px-3 py-1.5 text-[11px] tracking-[0.16em] uppercase
                            rounded-full border transition-all duration-200
                            ${
                                bulkMode
                                    ? "border-white/30 bg-white/[0.08] text-white/90"
                                    : "border-white/10 bg-white/[0.03] text-white/65 hover:text-white hover:border-white/20"
                            }
                        `}
                    >
                        {bulkMode ? "Exit Select" : "Bulk Select"}
                    </button>
                    <button
                        type="button"
                        onClick={() => setShowBulkAdd(true)}
                        data-testid="pipeline-bulk-add-open"
                        className="
                            px-3 py-1.5 text-[11px] tracking-[0.16em] uppercase
                            rounded-full border border-white/10 bg-white/[0.03]
                            text-white/65 hover:text-white hover:border-white/20
                            transition-all duration-200
                        "
                    >
                        + Bulk Add
                    </button>
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

            {/* ------------------------------------------------------------
                Cinematic Kanban — Main flow (progression funnel).
                Horizontal scroll on viewports narrower than the full row
                so a 6-stage funnel never compresses awkwardly. Mobile
                stacks via swipe-snap. Each column holds its own vertical
                scroll, so the board never grows taller than the viewport.
                ------------------------------------------------------------ */}
            <BoardSection
                eyebrow="Main flow"
                helper={`${MAIN_FLOW_STAGES.length} stages · progression funnel`}
            >
                <BoardRow testid="pipeline-main-flow">
                    {MAIN_FLOW_STAGES.map((stage) => (
                        <Column
                            key={stage}
                            stage={stage}
                            items={data.filter(
                                (i) => normaliseStage(i.stage) === stage,
                            )}
                            refresh={fetchPipeline}
                            bulkMode={bulkMode}
                            bulkIds={bulkIds}
                            onToggleBulkSelect={toggleBulkSelect}
                            onSelectAll={selectAllInColumn}
                            dragSupported={dragSupported}
                            dragId={dragId}
                            onCardDragStart={handleCardDragStart}
                            onCardDragEnd={handleCardDragEnd}
                            onCardDrop={handleCardDrop}
                        />
                    ))}
                </BoardRow>
            </BoardSection>

            {/* Follow-up — virtual read-only lane (PATCH 3C). Quietly
                separated under its own eyebrow with an amber accent. */}
            <BoardSection
                eyebrow="Follow-up"
                helper="Test pending · auto-cleared on submission"
                muted
            >
                <BoardRow testid="pipeline-follow-up">
                    <Column
                        stage="follow_up"
                        items={data.filter((i) => i.is_follow_up === true)}
                        refresh={fetchPipeline}
                        bulkMode={false}
                        bulkIds={EMPTY_BULK_SET}
                        onToggleBulkSelect={NOOP}
                        readOnly
                    />
                </BoardRow>
            </BoardSection>

            {/* Outcome stages — terminal states. Visually de-emphasised
                with a dimmer eyebrow and muted accent. */}
            <BoardSection eyebrow="Outcomes" helper="Terminal states" muted>
                <BoardRow testid="pipeline-outcomes">
                    {OUTCOME_STAGES.map((stage) => (
                        <Column
                            key={stage}
                            stage={stage}
                            items={data.filter(
                                (i) => normaliseStage(i.stage) === stage,
                            )}
                            refresh={fetchPipeline}
                            bulkMode={bulkMode}
                            bulkIds={bulkIds}
                            onToggleBulkSelect={toggleBulkSelect}
                            onSelectAll={selectAllInColumn}
                            dragSupported={dragSupported}
                            dragId={dragId}
                            onCardDragStart={handleCardDragStart}
                            onCardDragEnd={handleCardDragEnd}
                            onCardDrop={handleCardDrop}
                        />
                    ))}
                </BoardRow>
            </BoardSection>

            {/* Pitch — independent sourcing lane. Separated by a faint
                divider so the eye understands this is a different
                workflow, not the next funnel step. */}
            <BoardSection
                eyebrow="Pitch"
                helper="Sourcing · independent of funnel"
                divider
            >
                <BoardRow testid="pipeline-pitch">
                    {INDEPENDENT_STAGES.map((stage) => (
                        <Column
                            key={stage}
                            stage={stage}
                            items={data.filter(
                                (i) => normaliseStage(i.stage) === stage,
                            )}
                            refresh={fetchPipeline}
                            bulkMode={bulkMode}
                            bulkIds={bulkIds}
                            onToggleBulkSelect={toggleBulkSelect}
                            onSelectAll={selectAllInColumn}
                            dragSupported={dragSupported}
                            dragId={dragId}
                            onCardDragStart={handleCardDragStart}
                            onCardDragEnd={handleCardDragEnd}
                            onCardDrop={handleCardDrop}
                        />
                    ))}
                </BoardRow>
            </BoardSection>

            {/* Floating cinematic bulk action bar (PATCH 4C). Mounted last
                so it sits on top of everything via z-index. Renders only
                when there's a selection — slides up otherwise. */}
            <BulkActionBar
                count={bulkIds.size}
                onClear={() => {
                    setBulkIds(new Set());
                    setBulkMode(false);
                }}
                onMove={handleBulkMove}
            />
        </div>
    );
}

/* ---------------------------------------------------------------------
 * Cinematic board section helpers — pure layout, no state.
 *   BoardSection: eyebrow + helper + optional faint top divider.
 *   BoardRow:     horizontally scrolling flex strip + custom scrollbar.
 * ------------------------------------------------------------------- */
function BoardSection({ eyebrow, helper, children, muted = false, divider = false }) {
    return (
        <section
            className={`mt-10 ${divider ? "pt-10 border-t border-white/[0.05]" : ""}`}
        >
            <div className="flex items-baseline justify-between mb-4 px-1">
                <h3
                    className={`text-[10px] tracking-[0.28em] uppercase font-medium ${
                        muted ? "text-white/40" : "text-white/70"
                    }`}
                >
                    {eyebrow}
                </h3>
                {helper && (
                    <span className="text-[10px] tg-mono text-white/30 hidden sm:inline">
                        {helper}
                    </span>
                )}
            </div>
            {children}
        </section>
    );
}

function BoardRow({ children, testid }) {
    // Horizontal scroll mechanism — pure CSS, no library. Columns set their
    // own fixed widths; `flex-nowrap + overflow-x-auto` does the rest.
    // Snap points only on small viewports so swiping feels deliberate on
    // mobile; on desktop free-scroll feels more cinematic.
    return (
        <div
            data-testid={testid}
            className="
                flex gap-4 pb-3
                overflow-x-auto tg-pipeline-scroll
                flex-nowrap
                snap-x snap-mandatory md:snap-none
                -mx-1 px-1
            "
            style={{ scrollBehavior: "smooth" }}
        >
            {React.Children.map(children, (child, idx) => (
                <div key={idx} className="snap-start md:snap-none shrink-0">
                    {child}
                </div>
            ))}
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

const TalentAvatar = memo(function TalentAvatar({ src, name, size = "md" }) {
    // Premium cinematic avatar with an elegant initial-letter fallback.
    // Three sizes:
    //   sm — 24px  (legacy thumb, used in SearchResultRow)
    //   md — 44px  (default Card avatar)
    //   lg — 56px  (reserved for compact-mode hero rows)
    // Initial is computed once; fallback tile uses a soft radial gradient
    // so the empty state still reads "premium", not "broken image".
    const initial = (name || "?").trim().charAt(0).toUpperCase() || "?";
    const dims =
        size === "sm"
            ? "w-6 h-6 text-[10px] rounded"
            : size === "lg"
              ? "w-14 h-14 text-base rounded-xl"
              : "w-11 h-11 text-sm rounded-lg";

    if (src) {
        return (
            <img
                src={src}
                alt=""
                loading="lazy"
                className={`${dims} object-cover shrink-0 bg-white/5 ring-1 ring-white/10 shadow-[0_4px_12px_-4px_rgba(0,0,0,0.6)]`}
            />
        );
    }
    return (
        <div
            aria-hidden
            className={`${dims} shrink-0 flex items-center justify-center font-medium text-white/75
                bg-gradient-to-br from-white/[0.08] to-white/[0.02]
                ring-1 ring-white/10
                shadow-[0_4px_12px_-4px_rgba(0,0,0,0.6),inset_0_1px_0_0_rgba(255,255,255,0.05)]`}
        >
            {initial}
        </div>
    );
});

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

const Column = memo(function Column({
    stage,
    items,
    refresh,
    bulkMode,
    bulkIds,
    onToggleBulkSelect,
    onSelectAll,
    readOnly = false,
    dragSupported = false,
    dragId = null,
    onCardDragStart,
    onCardDragEnd,
    onCardDrop,
}) {
    // Cinematic column: a glass-panelled card with a thin stage-accent line
    // at the very top, a sticky header that survives vertical scroll, and a
    // calm empty state. No bright colours, no heavy borders.
    const accent = STAGE_ACCENTS[stage] || DEFAULT_ACCENT;
    const emptyCopy = EMPTY_STATE_COPY[stage] || "Nothing here yet";

    // Patch 4C — per-column "Select all" affordance. Only surfaces when
    // we're in bulk mode AND the lane is mutable (read-only lanes like
    // follow_up are explicitly excluded by the spec).
    const canSelectAll =
        bulkMode && !readOnly && items.length > 0 && typeof onSelectAll === "function";
    const allInColumnSelected =
        canSelectAll && items.every((i) => bulkIds.has(i.id));

    /* -----------------------------------------------------------------
     * Drag & Drop (PATCH 4D) — column = droppable target
     *
     * A column is "droppable" when:
     *   • drag is supported (pointer-fine device)
     *   • the lane is not read-only (follow-up is opt-out)
     *   • there's an active drag (`dragId` set in parent)
     *   • a drop callback is wired
     *
     * `isDragOver` is local — only this column re-renders during hover,
     * not the entire kanban. We compare incoming dragId against null to
     * decide whether to react to dragenter at all.
     * --------------------------------------------------------------- */
    const isDroppable =
        dragSupported && !readOnly && Boolean(dragId) && typeof onCardDrop === "function";

    const [isDragOver, setIsDragOver] = useState(false);

    const handleDragOver = (e) => {
        if (!isDroppable) return;
        // preventDefault is what tells the browser "yes, this is a drop
        // target". Without it the onDrop handler never fires.
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
    };

    const handleDragEnter = (e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(true);
    };

    const handleDragLeave = (e) => {
        if (!isDroppable) return;
        // Only clear when the pointer truly leaves the column shell —
        // dragenter/leave bubble through every child, so we guard with
        // currentTarget vs relatedTarget.
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setIsDragOver(false);
    };

    const handleDrop = (e) => {
        if (!isDroppable) return;
        e.preventDefault();
        setIsDragOver(false);
        const droppedId = e.dataTransfer.getData("text/plain");
        if (droppedId) onCardDrop(stage, droppedId);
    };

    return (
        <div
            data-testid={`pipeline-column-${stage}`}
            onDragOver={handleDragOver}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`
                relative shrink-0 w-[280px] md:w-[300px]
                rounded-xl overflow-hidden
                bg-gradient-to-b from-white/[0.04] to-white/[0.015]
                border transition-all duration-200
                backdrop-blur-xl
                ${
                    isDragOver
                        ? "border-white/30 ring-1 ring-white/10 shadow-[0_12px_36px_-12px_rgba(0,0,0,0.7),inset_0_0_0_1px_rgba(255,255,255,0.05)]"
                        : "border-white/[0.06] shadow-[0_8px_32px_-12px_rgba(0,0,0,0.6),inset_0_1px_0_0_rgba(255,255,255,0.04)]"
                }
            `}
        >
            {/* Stage accent — paper-thin gradient line that gives each lane
                a quiet sense of identity without colouring the whole card. */}
            <div
                className={`absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r ${accent} pointer-events-none`}
                aria-hidden
            />

            {/* Sticky header — survives vertical scroll inside the column.
                Slight backdrop-blur so cards passing under it stay legible. */}
            <div
                className="
                    sticky top-0 z-10
                    px-4 py-3
                    bg-black/40 backdrop-blur-md
                    border-b border-white/[0.05]
                    flex items-center justify-between gap-2
                "
            >
                <div className="min-w-0 flex items-center gap-2">
                    <span className="text-[10px] tracking-[0.22em] uppercase text-white/70 font-medium truncate">
                        {getStageLabel(stage)}
                    </span>
                    {readOnly && (
                        <span className="text-[9px] tracking-[0.18em] uppercase text-amber-200/60 tg-mono">
                            read-only
                        </span>
                    )}
                </div>
                <span
                    className="
                        text-[10px] tg-mono text-white/50
                        px-2 py-0.5 rounded-full
                        bg-white/[0.04] border border-white/[0.06]
                        shrink-0
                    "
                    data-testid={`pipeline-column-count-${stage}`}
                >
                    {items.length}
                </span>
            </div>

            {/* Per-column Select-all affordance (PATCH 4C). Only shown in
                bulk mode on mutable lanes. Toggles intelligently — if every
                row is already selected, it deselects this column instead. */}
            {canSelectAll && (
                <div className="px-4 py-2 border-b border-white/[0.04] bg-black/20">
                    <button
                        type="button"
                        onClick={() => onSelectAll(items)}
                        data-testid={`pipeline-select-all-${stage}`}
                        className="
                            w-full text-left flex items-center justify-between gap-2
                            text-[10px] tracking-[0.18em] uppercase
                            text-white/55 hover:text-white/90
                            transition-colors duration-200
                        "
                    >
                        <span>
                            {allInColumnSelected
                                ? "Deselect column"
                                : "Select all in column"}
                        </span>
                        <span className="tg-mono text-white/35">
                            {items.length}
                        </span>
                    </button>
                </div>
            )}

            {/* Card stream — independent vertical scroll. The fixed
                viewport height keeps the board cinematic and predictable. */}
            <div className="
                px-3 py-3 space-y-2
                max-h-[68vh] min-h-[180px]
                overflow-y-auto tg-pipeline-scroll
            ">
                {items.length === 0 ? (
                    <EmptyLane label={emptyCopy} />
                ) : (
                    items.map((item) => (
                        <Card
                            key={`${stage}-${item.id}`}
                            item={item}
                            refresh={refresh}
                            bulkMode={bulkMode && !readOnly}
                            isSelected={bulkIds.has(item.id)}
                            onToggleSelect={onToggleBulkSelect}
                            readOnly={readOnly}
                            dragSupported={dragSupported}
                            isDragging={dragId === item.id}
                            onDragStart={onCardDragStart}
                            onDragEnd={onCardDragEnd}
                        />
                    ))
                )}
            </div>
        </div>
    );
});

// Cinematic empty state — replaces the old dashed placeholder. Soft,
// quiet, no UI noise. Sits centred in the column so the eye rests rather
// than searches for the missing data.
const EmptyLane = memo(function EmptyLane({ label }) {
    return (
        <div className="py-10 flex flex-col items-center justify-center gap-2 text-center">
            <div className="w-8 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent" />
            <p className="text-[11px] tracking-wide text-white/30 italic">
                {label}
            </p>
        </div>
    );
});

const Card = memo(function Card({
    item,
    refresh,
    bulkMode,
    isSelected,
    onToggleSelect,
    readOnly = false,
    dragSupported = false,
    isDragging = false,
    onDragStart,
    onDragEnd,
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

    // Legacy `sent` rows render as `approved` so action buttons match the
    // column the card sits in, and terminal/locked rows expose no onward
    // transitions.
    const canonicalStage = normaliseStage(item.stage);
    const nextStages = NEXT_STAGE_FLOW[canonicalStage] || [];
    const statusTone = STATUS_TONES[canonicalStage];

    // Display fields with sensible fallbacks. `talent_email` is the new
    // hydrated field (Patch 2); `email` is the legacy pre-hydration alias.
    const displayName = item.talent_name || item.talent_id || "Unknown";
    const displayEmail = item.talent_email || item.email || null;
    const displayPhone = item.talent_phone || null;
    const displayIg = item.instagram_handle || null;

    /* -----------------------------------------------------------------
     * Drag & Drop (PATCH 4D) — card = draggable source
     *
     * Native HTML5 only. Disabled in three cases:
     *   • pointer-coarse device (touch) — `dragSupported` is false
     *   • read-only lane (follow-up) — drag is meaningless here
     *   • bulk mode is active — drag would conflict with multi-select
     * --------------------------------------------------------------- */
    const draggable = dragSupported && !readOnly && !bulkMode;

    const handleDragStart = (e) => {
        if (!draggable) return;
        // text/plain so any drop target — including outside the app — can
        // read the id without us having to guess MIME types.
        e.dataTransfer.setData("text/plain", item.id);
        e.dataTransfer.effectAllowed = "move";
        // Notify parent so all columns can react to the drag context.
        // Wrap in a microtask so the drag image is already snapshotted
        // before the visual state changes (otherwise the ghost image
        // shows the half-faded card).
        setTimeout(() => onDragStart && onDragStart(item.id), 0);
    };

    const handleDragEnd = () => {
        if (!draggable) return;
        if (onDragEnd) onDragEnd();
    };

    // Cinematic shell — glass card with luxury hover lift. Follow-up
    // (readOnly) cards stay quieter: no hover lift, dimmer surface.
    // During drag: slight scale-down + opacity dim + elevated shadow.
    const shellClass = [
        "group relative rounded-xl overflow-hidden",
        "border transition-all duration-300",
        "bg-gradient-to-b from-white/[0.05] to-white/[0.02]",
        "backdrop-blur-md",
        "shadow-[0_2px_8px_-2px_rgba(0,0,0,0.4),inset_0_1px_0_0_rgba(255,255,255,0.04)]",
        isSelected
            ? "border-white/40 ring-1 ring-white/20"
            : "border-white/[0.07]",
        readOnly
            ? "opacity-80"
            : "hover:border-white/15 hover:-translate-y-[1px] hover:shadow-[0_12px_32px_-12px_rgba(0,0,0,0.7),inset_0_1px_0_0_rgba(255,255,255,0.06)]",
        moving ? "opacity-40 pointer-events-none" : "",
        isDragging
            ? "opacity-60 scale-[0.97] ring-1 ring-white/15 shadow-[0_18px_48px_-12px_rgba(0,0,0,0.8)]"
            : "",
        draggable ? "cursor-grab active:cursor-grabbing" : "",
    ].join(" ");

    /* -----------------------------------------------------------------
     * BULK MODE — compact row, checkbox + small avatar + name + email.
     * No actions, no metadata. Designed for fast multi-select scanning.
     * --------------------------------------------------------------- */
    if (bulkMode) {
        return (
            <div
                data-testid={`pipeline-card-${item.id}`}
                onClick={() => onToggleSelect(item.id)}
                draggable={draggable}
                onDragStart={handleDragStart}
                onDragEnd={handleDragEnd}
                className={`${shellClass} px-3 py-2.5 cursor-pointer`}
            >
                <div className="flex items-center gap-2.5">
                    <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => onToggleSelect(item.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-4 h-4 accent-white/80 shrink-0"
                    />
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="sm"
                    />
                    <div className="flex-1 min-w-0">
                        <p className="text-[13px] text-white/90 font-medium truncate leading-tight">
                            {displayName}
                        </p>
                        {displayEmail && (
                            <p className="text-[10px] text-white/45 truncate tg-mono mt-0.5">
                                {displayEmail}
                            </p>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    /* -----------------------------------------------------------------
     * NORMAL MODE — premium cinematic card with three zones:
     *   Top    — avatar + name + instagram + optional status chip
     *   Middle — email + phone metadata rows
     *   Bottom — action pills (suppressed in readOnly mode)
     * --------------------------------------------------------------- */
    return (
        <div
            data-testid={`pipeline-card-${item.id}`}
            draggable={draggable}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
            className={shellClass}
        >
            {/* Subtle inner accent stripe that lights up on hover.
                Pure CSS, no JS animation. */}
            <div
                aria-hidden
                className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none"
            />

            <div className="p-3 space-y-2.5">
                {/* TOP — identity */}
                <div className="flex items-start gap-3">
                    <TalentAvatar
                        src={item.image_url}
                        name={displayName}
                        size="md"
                    />
                    <div className="flex-1 min-w-0">
                        <p
                            className="text-[13px] text-white/95 font-medium truncate leading-tight"
                            title={displayName}
                        >
                            {displayName}
                        </p>
                        {displayIg && (
                            <p className="text-[10px] text-white/45 truncate tg-mono mt-0.5">
                                {displayIg}
                            </p>
                        )}
                        {!displayIg && item.talent_name && (
                            <p
                                className="text-[10px] text-white/30 truncate tg-mono mt-0.5"
                                title={item.talent_id}
                            >
                                {item.talent_id?.slice(0, 8)}…
                            </p>
                        )}
                    </div>

                    {/* Status chip — only on terminal/locked lanes.
                        Mid-funnel stages (ask_to_test, shortlisted,
                        already_tested, pitch) intentionally stay
                        un-chipped to keep the eye on the next action. */}
                    {statusTone && (
                        <span
                            className={`shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border ${statusTone.chip}`}
                            title={statusTone.label}
                        >
                            <span
                                className={`w-1 h-1 rounded-full ${statusTone.dot}`}
                            />
                            <span
                                className={`text-[9px] tracking-[0.14em] uppercase ${statusTone.text}`}
                            >
                                {statusTone.label}
                            </span>
                        </span>
                    )}
                </div>

                {/* MIDDLE — metadata. Each row is a single line, truncate,
                    monospaced for that "casting CRM" feel. Hidden entirely
                    when there's no data → keeps the card compact. */}
                {(displayEmail || displayPhone) && (
                    <div className="space-y-0.5 pt-0.5">
                        {displayEmail && (
                            <p className="text-[10.5px] text-white/55 truncate tg-mono">
                                {displayEmail}
                            </p>
                        )}
                        {displayPhone && (
                            <p className="text-[10.5px] text-white/40 truncate tg-mono">
                                {displayPhone}
                            </p>
                        )}
                    </div>
                )}

                {/* BOTTOM — action pills. Suppressed for the readOnly
                    follow-up lane (Patch 3C) and for stages that have no
                    onward transitions (locked / terminal lanes). */}
                {!readOnly && nextStages.length > 0 && (
                    <div className="flex flex-wrap gap-1 pt-1.5 border-t border-white/[0.05]">
                        {nextStages.map((stage) => (
                            <button
                                key={stage}
                                type="button"
                                onClick={() => move(stage)}
                                disabled={moving}
                                data-testid={`pipeline-card-move-${item.id}-${stage}`}
                                title={`Move to ${getStageLabel(stage)}`}
                                className="
                                    px-2 py-1 rounded-full
                                    text-[9.5px] tracking-[0.12em] uppercase
                                    text-white/65 hover:text-white
                                    bg-white/[0.04] hover:bg-white/[0.08]
                                    border border-white/[0.06] hover:border-white/15
                                    transition-all duration-200
                                    hover:shadow-[0_0_0_3px_rgba(255,255,255,0.03)]
                                    disabled:opacity-40 disabled:cursor-not-allowed
                                "
                            >
                                {STAGE_LABELS[stage] || getStageLabel(stage)}
                            </button>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
});


/* ---------------------------------------------------------------------
 * BulkActionBar (PATCH 4C)
 *
 * Floating cinematic action bar anchored to the bottom-center of the
 * viewport. Surfaces only when `count > 0`. Slides up via CSS transform
 * + opacity (no animation library). ESC clears at the page level.
 *
 * Layout: [count badge] [Move to →] [pill, pill, pill, ...] [× clear]
 *
 * Pills are horizontally scrollable on mobile so any number of stages
 * fits on a single line without breaking the bar's visual rhythm.
 * ------------------------------------------------------------------- */
const BulkActionBar = memo(function BulkActionBar({ count, onClear, onMove }) {
    const visible = count > 0;

    // Local "in-flight" state prevents double-click duplicates. Parent's
    // loading state doesn't surface mid-mutation state for bulk ops.
    const [busy, setBusy] = useState(false);

    const handleMove = async (stage) => {
        if (busy) return;
        setBusy(true);
        try {
            await onMove(stage);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            aria-hidden={!visible}
            data-testid="pipeline-bulk-bar"
            className={`
                fixed z-40 left-1/2 -translate-x-1/2
                bottom-4 sm:bottom-6
                w-[min(94vw,720px)]
                transition-all duration-300 ease-out
                ${visible
                    ? "opacity-100 translate-y-0 pointer-events-auto"
                    : "opacity-0 translate-y-3 pointer-events-none"}
            `}
        >
            <div
                className="
                    flex items-center gap-2 sm:gap-3
                    px-3 py-2 sm:px-4 sm:py-2.5
                    rounded-full
                    bg-black/70 backdrop-blur-xl
                    border border-white/10
                    shadow-[0_18px_48px_-12px_rgba(0,0,0,0.7),inset_0_1px_0_0_rgba(255,255,255,0.05)]
                "
            >
                {/* Count badge — anchors the eye to the selection size */}
                <div
                    className="
                        shrink-0 flex items-center gap-1.5
                        px-2.5 py-1 rounded-full
                        bg-white text-black
                        text-[11px] tracking-[0.16em] uppercase font-medium
                    "
                    data-testid="pipeline-bulk-bar-count"
                >
                    <span className="tg-mono">{count}</span>
                    <span className="opacity-60">selected</span>
                </div>

                <div className="hidden sm:block w-px h-5 bg-white/10" />

                <span className="hidden sm:inline text-[10px] tracking-[0.18em] uppercase text-white/40 shrink-0">
                    Move to
                </span>

                <div
                    className="
                        flex-1 min-w-0 flex items-center gap-1.5
                        overflow-x-auto tg-pipeline-scroll
                        scroll-smooth
                    "
                >
                    {BULK_MOVE_TARGETS.map((stage) => (
                        <button
                            key={stage}
                            type="button"
                            onClick={() => handleMove(stage)}
                            disabled={busy}
                            data-testid={`pipeline-bulk-move-${stage}`}
                            title={`Move ${count} to ${getStageLabel(stage)}`}
                            className="
                                shrink-0
                                px-3 py-1.5 rounded-full
                                text-[10.5px] tracking-[0.12em] uppercase
                                text-white/75 hover:text-white
                                bg-white/[0.05] hover:bg-white/[0.10]
                                border border-white/[0.08] hover:border-white/20
                                transition-all duration-200
                                disabled:opacity-40 disabled:cursor-not-allowed
                                hover:shadow-[0_0_0_3px_rgba(255,255,255,0.04)]
                            "
                        >
                            {STAGE_LABELS[stage] || getStageLabel(stage)}
                        </button>
                    ))}
                </div>

                <button
                    type="button"
                    onClick={onClear}
                    data-testid="pipeline-bulk-bar-clear"
                    title="Clear selection · ESC"
                    className="
                        shrink-0
                        w-8 h-8 rounded-full
                        flex items-center justify-center
                        text-white/55 hover:text-rose-200
                        bg-white/[0.03] hover:bg-rose-300/10
                        border border-white/[0.08] hover:border-rose-300/20
                        transition-all duration-200
                        text-base leading-none
                    "
                >
                    ×
                </button>
            </div>

            <p className="text-center mt-2 text-[9px] tracking-[0.22em] uppercase text-white/25 hidden sm:block">
                Press ESC to clear
            </p>
        </div>
    );
});
