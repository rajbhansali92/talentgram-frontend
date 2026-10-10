import React, { memo, useCallback, useEffect, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, GripVertical, Search, SlidersHorizontal, X } from "lucide-react";
import { buildParams, useTalentDirectory } from "@/hooks/useTalentDirectory";
import FilterPanel from "@/components/talent-directory/FilterPanel";
import FilterChips from "@/components/talent-directory/FilterChips";
import MobileFilterSheet from "@/components/talent-directory/MobileFilterSheet";
import SortDropdown from "@/components/talent-directory/SortDropdown";
import { mergeSelection, moveItem, removeIds, shortTalentName } from "@/lib/rosterSelection";

// Key of the (module-level, session-scoped) browsing snapshot that lets the Talents step come back with
// the same filters / page / results after a trip to the Images step. RosterBuilder clears it on mount.
export const ROSTER_DIRECTORY_KEY = "roster-builder-talents";
const PAGE_SIZE = 24;

const toEntry = (t) => ({ id: t.id, name: t.name, thumb: t.cover_thumbnail_url || t.image_url || null });

/** One result card. Memoised: ticking a card re-renders only that card, not the 24 around it. */
const TalentCard = memo(function TalentCard({ t, on, onToggle }) {
    const src = t.cover_thumbnail_url || t.image_url;
    return (
        <button type="button" onClick={() => onToggle(t)} aria-pressed={on} data-testid={`roster-talent-${t.id}`}
            className={`relative text-left rounded-xl overflow-hidden border bg-white transition-colors ${on ? "border-black ring-1 ring-black" : "border-black/[0.08] hover:border-black/30"}`}>
            <div className="aspect-[3/4] bg-[#f3f2ef]">
                {src && <img src={src} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />}
            </div>
            <div className="p-2.5">
                <div className="text-sm font-medium text-black/85 truncate">{t.name}</div>
                <div className="text-[11px] text-black/45 truncate">{[t.age && `${t.age}y`, t.height].filter(Boolean).join(" · ") || "—"}</div>
            </div>
            <span className={`absolute top-2 right-2 w-6 h-6 rounded-full flex items-center justify-center border ${on ? "bg-black text-white border-black" : "bg-white/90 border-black/20"}`}>
                {on && <Check className="w-3.5 h-3.5" />}
            </span>
        </button>
    );
});

const SelectedRow = memo(function SelectedRow({ t, i, last, onRemove, onMove, onDragStart, onDrop }) {
    return (
        <li draggable onDragStart={() => onDragStart(i)} onDragOver={(e) => e.preventDefault()} onDrop={() => onDrop(i)}
            className="flex items-center gap-2 border border-black/[0.08] rounded-lg p-2 bg-white" data-testid={`roster-selected-${t.id}`}>
            <GripVertical className="w-4 h-4 text-black/30 cursor-grab shrink-0" aria-hidden />
            <span className="text-[11px] font-mono text-black/40 w-6">{String(i + 1).padStart(2, "0")}</span>
            <div className="w-9 h-11 rounded bg-[#f3f2ef] overflow-hidden shrink-0">
                {t.thumb && <img src={t.thumb} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />}
            </div>
            <div className="min-w-0 flex-1">
                <div className="text-sm text-black/85 truncate">{t.name}</div>
                <div className="text-[11px] text-black/45">as {shortTalentName(t.name)}</div>
            </div>
            <div className="flex flex-col">
                <button type="button" aria-label={`Move ${t.name} up`} disabled={i === 0} onClick={() => onMove(i, -1)} className="p-1 text-black/50 hover:text-black disabled:opacity-25"><ChevronUp className="w-4 h-4" /></button>
                <button type="button" aria-label={`Move ${t.name} down`} disabled={last} onClick={() => onMove(i, 1)} className="p-1 text-black/50 hover:text-black disabled:opacity-25"><ChevronDown className="w-4 h-4" /></button>
            </div>
            <button type="button" aria-label={`Remove ${t.name}`} onClick={() => onRemove(t.id)} className="p-1.5 text-black/40 hover:text-red-600"><X className="w-4 h-4" /></button>
        </li>
    );
});

const btn = "px-3 min-h-[40px] text-xs font-medium border border-black/[0.12] rounded-lg text-black/70 hover:bg-black/[0.03] bg-white disabled:opacity-40 disabled:hover:bg-white";

/* ------------------------------ Step 2: talents ----------------------------- */
export default function TalentStep({ selected, setSelected, selectedSet, maxTalents, availableTags, availableLocations }) {
    const {
        search, setSearch, filters, setFilter, removeFilter, clearAllFilters, activeFilterCount, filtersActive,
        sortBy, setSortBy, page, setPage, total, pages, talents, loading, error, refetch,
    } = useTalentDirectory({ pageSize: PAGE_SIZE, persistKey: ROSTER_DIRECTORY_KEY });

    const [showFilters, setShowFilters] = useState(false);
    const [confirmClear, setConfirmClear] = useState(false);
    const [bulkBusy, setBulkBusy] = useState(false);
    const dragIndex = useRef(null);
    // Latest-value mirrors so the card / row callbacks below keep one identity for the whole session.
    const selectedRef = useRef(selected);
    selectedRef.current = selected;

    const atLimit = selected.length >= maxTalents;

    const toggle = useCallback((t) => {
        if (selectedRef.current.some((x) => x.id === t.id)) {
            setSelected((prev) => prev.filter((x) => x.id !== t.id));
        } else if (selectedRef.current.length >= maxTalents) {
            toast.error(`A roster can include at most ${maxTalents} talents`);
        } else {
            setSelected((prev) => (prev.some((x) => x.id === t.id) ? prev : [...prev, toEntry(t)]));
        }
    }, [setSelected, maxTalents]);

    const remove = useCallback((id) => setSelected((prev) => prev.filter((t) => t.id !== id)), [setSelected]);
    const move = useCallback((i, d) => setSelected((prev) => moveItem(prev, i, i + d)), [setSelected]);
    const onDragStart = useCallback((i) => { dragIndex.current = i; }, []);
    const onDrop = useCallback((i) => {
        if (dragIndex.current !== null) setSelected((p) => moveItem(p, dragIndex.current, i));
        dragIndex.current = null;
    }, [setSelected]);

    // Bulk add is ATOMIC: either every talent of the chosen scope is added or none is, with the reason shown.
    const addBulk = useCallback((incoming, what) => {
        const { list, added, overflow } = mergeSelection(selectedRef.current, incoming.map(toEntry), maxTalents);
        if (overflow > 0) {
            toast.error(`${what} would put the roster ${overflow} over the ${maxTalents}-talent limit — nothing was added. Narrow the filters or remove some first.`);
            return;
        }
        if (added === 0) { toast.info("They are all selected already"); return; }
        setSelected(list);
        toast.success(`Added ${added} talent${added === 1 ? "" : "s"}`);
    }, [setSelected, maxTalents]);

    const selectPage = () => addBulk(talents, "Selecting this page");

    // Select EVERY talent that matches the current search + filters (not only the visible page). Fetched in
    // one explicit request in the same server-side order; only offered when the match count is within the limit.
    const selectAllMatching = async () => {
        setBulkBusy(true);
        try {
            const params = buildParams(filters, sortBy, 0, 200);
            const { data } = await adminApi.get("/talents", { params });
            const items = data.data || data.items || [];
            if ((data.total || items.length) > items.length) {
                toast.error("Too many talents match to select them all at once — narrow the filters first.");
                return;
            }
            addBulk(items, `Selecting all ${items.length} matching talents`);
        } catch {
            toast.error("Couldn't load the matching talents");
        } finally {
            setBulkBusy(false);
        }
    };

    const deselectPage = () => {
        const next = removeIds(selectedRef.current, new Set(talents.map((t) => t.id)));
        if (next !== selectedRef.current) setSelected(next);
    };

    const clearSelection = () => { setSelected([]); setConfirmClear(false); };

    // Clear Filters keeps the selection (selection lives in the wizard, not in the directory query).
    const pageAllSelected = talents.length > 0 && talents.every((t) => selectedSet.has(t.id));
    const canSelectAllMatching = total > 0 && total <= maxTalents;

    // The page can become empty after filtering past the last page — step back to a valid one.
    useEffect(() => { if (!loading && pages > 0 && page > pages) setPage(pages); }, [loading, pages, page, setPage]);

    return (
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-6" data-testid="roster-step-talents">
            <section className="min-w-0">
                <div className="flex flex-col sm:flex-row gap-3 mb-3">
                    <div className="relative flex-1">
                        <Search className="w-4 h-4 text-black/35 absolute left-3.5 top-1/2 -translate-y-1/2" />
                        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search talents by name, city, skill…"
                            data-testid="roster-search" aria-label="Search talents"
                            className="w-full border border-black/[0.12] rounded-lg pl-10 pr-4 min-h-[44px] text-sm focus:outline-none focus:border-black/50 bg-white" />
                    </div>
                    <div className="flex items-center gap-2">
                        <button type="button" data-testid="roster-filter-toggle" onClick={() => setShowFilters((v) => !v)} aria-expanded={showFilters}
                            className={`hidden md:flex items-center gap-2 px-3 min-h-[44px] text-sm border rounded-lg transition-colors bg-white ${showFilters ? "border-black/30" : "border-black/[0.12] hover:border-black/30"}`}>
                            <SlidersHorizontal className="w-4 h-4" /> Filters
                            {activeFilterCount > 0 && <span className="min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full bg-[#0c2340] text-white text-[10px] font-semibold">{activeFilterCount}</span>}
                        </button>
                        <div className="md:hidden">
                            <MobileFilterSheet filters={filters} setFilter={setFilter} clearAllFilters={clearAllFilters}
                                activeFilterCount={activeFilterCount} availableTags={availableTags} availableLocations={availableLocations} />
                        </div>
                        <SortDropdown value={sortBy} onChange={setSortBy} />
                    </div>
                </div>

                {showFilters && (
                    <div className="hidden md:block border border-gray-200 bg-gray-50/50 rounded-xl p-5 mb-3" data-testid="roster-filter-panel">
                        <FilterPanel filters={filters} setFilter={setFilter} availableTags={availableTags} availableLocations={availableLocations} />
                    </div>
                )}

                {filtersActive && (
                    <div className="mb-3">
                        <FilterChips filters={filters} setFilter={setFilter} removeFilter={removeFilter} clearAllFilters={clearAllFilters}
                            activeFilterCount={activeFilterCount} availableTags={availableTags} />
                    </div>
                )}

                {/* Result count + the explicit select scopes */}
                <div className="flex flex-wrap items-center gap-2 mb-4" data-testid="roster-bulk">
                    <span className="text-xs text-black/55 mr-auto" data-testid="roster-result-count" aria-live="polite">
                        {loading && !talents.length ? "Loading…" : `${total}${filtersActive ? " matching" : ""} talent${total === 1 ? "" : "s"}`}
                        {" · "}<span data-testid="roster-selected-of">{selected.length} of {maxTalents} selected</span>
                    </span>
                    <button type="button" className={btn} data-testid="roster-select-page" onClick={selectPage} disabled={!talents.length || pageAllSelected || bulkBusy}>
                        Select this page ({talents.length})
                    </button>
                    <button type="button" className={btn} data-testid="roster-select-all-matching" onClick={selectAllMatching}
                        disabled={!canSelectAllMatching || bulkBusy}
                        title={canSelectAllMatching || total === 0 ? undefined : `More than ${maxTalents} talents match — narrow the filters to select them all at once`}>
                        {bulkBusy ? "Selecting…" : `Select all ${total} ${filtersActive ? "matching" : "talents"}`}
                    </button>
                    <button type="button" className={btn} data-testid="roster-deselect-page" onClick={deselectPage}
                        disabled={!talents.some((t) => selectedSet.has(t.id))}>
                        Deselect this page
                    </button>
                </div>

                {error && !loading ? (
                    <div className="text-sm text-black/55 py-10 text-center" data-testid="roster-talents-error">
                        Couldn&apos;t load talents. <button type="button" onClick={refetch} className="underline">Try again</button>
                    </div>
                ) : (
                    <div className={`grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3 transition-opacity ${loading ? "opacity-60" : ""}`} data-testid="roster-talent-grid" aria-busy={loading}>
                        {talents.map((t) => <TalentCard key={t.id} t={t} on={selectedSet.has(t.id)} onToggle={toggle} />)}
                    </div>
                )}
                {!loading && !error && talents.length === 0 && (
                    <div className="text-sm text-black/45 py-10 text-center" data-testid="roster-no-results">
                        No talents found.
                        {filtersActive && <> <button type="button" onClick={clearAllFilters} className="underline ml-1" data-testid="roster-clear-filters">Clear filters</button> <span className="text-black/35">(your {selected.length} selected talents are kept)</span></>}
                    </div>
                )}

                {pages > 1 && (
                    <nav className="flex items-center justify-center gap-3 mt-6" aria-label="Talent pages" data-testid="roster-pager">
                        <button type="button" className={btn} onClick={() => setPage(page - 1)} disabled={page <= 1 || loading} aria-label="Previous page"><ChevronLeft className="w-4 h-4" /></button>
                        <span className="text-xs text-black/55" data-testid="roster-page-label">Page {page} of {pages}</span>
                        <button type="button" className={btn} onClick={() => setPage(page + 1)} disabled={page >= pages || loading} aria-label="Next page"><ChevronRight className="w-4 h-4" /></button>
                    </nav>
                )}
            </section>

            <aside className="min-w-0 lg:sticky lg:top-6 self-start bg-white border border-black/[0.08] rounded-xl p-4" data-testid="roster-selected">
                <div className="flex items-baseline justify-between mb-3">
                    <h2 className="font-display text-base text-black/85">Selected Talents</h2>
                    <span className={`text-xs ${atLimit ? "text-red-600" : "text-black/50"}`} data-testid="roster-selected-count">{selected.length}</span>
                </div>
                {selected.length === 0 ? (
                    <p className="text-xs text-black/45 py-4">Pick talents on the left. Drag to reorder — this is the order they appear in the roster. Your picks stay selected while you search and filter.</p>
                ) : (
                    <>
                        <div className="flex items-center justify-between mb-3 text-[11px] text-black/45">
                            <span>{atLimit ? `Limit of ${maxTalents} reached` : `${maxTalents - selected.length} more can be added`}</span>
                            {confirmClear ? (
                                <span className="flex items-center gap-2">
                                    <button type="button" data-testid="roster-clear-confirm" onClick={clearSelection} className="text-red-600 font-medium">Remove all {selected.length}</button>
                                    <button type="button" onClick={() => setConfirmClear(false)} className="underline">Cancel</button>
                                </span>
                            ) : (
                                <button type="button" data-testid="roster-clear-selection" onClick={() => setConfirmClear(true)} className="underline hover:text-black/80">Clear selection</button>
                            )}
                        </div>
                        <ol className="space-y-2 max-h-[60vh] overflow-auto pr-1">
                            {selected.map((t, i) => (
                                <SelectedRow key={t.id} t={t} i={i} last={i === selected.length - 1}
                                    onRemove={remove} onMove={move} onDragStart={onDragStart} onDrop={onDrop} />
                            ))}
                        </ol>
                    </>
                )}
            </aside>
        </div>
    );
}
