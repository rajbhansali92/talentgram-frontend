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
                            {BULK_MOVE_TARGETS.map((stage) => (
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
                        />
                    ))}
                </BoardRow>
            </BoardSection>
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

const TalentAvatar = memo(function TalentAvatar({ src, name }) {
    // 24px square thumbnail next to the talent name. Falls back to the
    // first letter on a coloured tile when no image_url is available
    // (talents without a cover photo, or pre-hydration loading state).
    const initial = (name || "?").trim().charAt(0).toUpperCase() || "?";
    if (src) {
        return (
            <img
                src={src}
                alt=""
                loading="lazy"
                className="w-6 h-6 rounded object-cover shrink-0 bg-white/5"
            />
        );
    }
    return (
        <div
            aria-hidden
            className="w-6 h-6 rounded shrink-0 bg-white/10 flex items-center justify-center text-[10px] text-white/70 font-medium"
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
                <TalentAvatar src={talent.image_url} name={talent.name} />
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
    readOnly = false,
}) {
    // Cinematic column: a glass-panelled card with a thin stage-accent line
    // at the very top, a sticky header that survives vertical scroll, and a
    // calm empty state. No bright colours, no heavy borders.
    const accent = STAGE_ACCENTS[stage] || DEFAULT_ACCENT;
    const emptyCopy = EMPTY_STATE_COPY[stage] || "Nothing here yet";

    return (
        <div
            data-testid={`pipeline-column-${stage}`}
            className="
                relative shrink-0 w-[280px] md:w-[300px]
                rounded-xl overflow-hidden
                bg-gradient-to-b from-white/[0.04] to-white/[0.015]
                border border-white/[0.06]
                backdrop-blur-xl
                shadow-[0_8px_32px_-12px_rgba(0,0,0,0.6),inset_0_1px_0_0_rgba(255,255,255,0.04)]
            "
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

    // Treat legacy `sent` rows as `approved` so the action buttons match
    // the column the card is rendered in, and terminal/locked rows expose
    // no onward transitions.
    const canonicalStage = normaliseStage(item.stage);
    const nextStages = NEXT_STAGE_FLOW[canonicalStage] || [];

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
                    <TalentAvatar
                        src={item.image_url}
                        name={item.talent_name || item.talent_id}
                    />
                    <div className="flex-1 min-w-0">
                        <p className="font-mono text-white/90 truncate">
                            {item.talent_name || item.talent_id}
                        </p>
                        {item.talent_email && (
                            <p className="text-white/60 truncate text-[10px] mt-0.5">
                                {item.talent_email}
                            </p>
                        )}
                    </div>
                </div>
            ) : (
                <>
                    <div className="flex items-start gap-2 mb-1">
                        <TalentAvatar
                            src={item.image_url}
                            name={item.talent_name || item.talent_id}
                        />
                        <div className="flex-1 min-w-0">
                            <p className="font-mono text-white/90 font-medium truncate">
                                {item.talent_name || item.talent_id}
                            </p>
                            {item.talent_name && (
                                <p className="text-white/40 truncate text-[10px] mt-0.5 tg-mono">
                                    ID: {item.talent_id}
                                </p>
                            )}
                        </div>
                    </div>

                    {(item.talent_email || item.email) && (
                        <p className="text-white/40 truncate text-[10px]">
                            {item.talent_email || item.email}
                        </p>
                    )}
                    {item.instagram_handle && (
                        <p className="text-white/40 truncate text-[10px] tg-mono">
                            {item.instagram_handle}
                        </p>
                    )}

                    {/* Onward-stage action buttons. Suppressed in readOnly
                        mode (e.g. the virtual `follow_up` lane — PATCH 3C). */}
                    {!readOnly && nextStages.length > 0 && (
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

                    {canonicalStage === "locked" && (
                        <div className="mt-2 text-yellow-500/60 text-[10px]">
                            ✓ Finalized
                        </div>
                    )}

                    {(canonicalStage === "not_interested" ||
                        canonicalStage === "not_available" ||
                        canonicalStage === "rejected") && (
                        <div className="mt-2 text-red-500/60 text-[10px]">
                            ✗ {getStageLabel(canonicalStage)}
                        </div>
                    )}
                </>
            )}
        </div>
    );
});
