import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, X, Check, Image as ImageIcon, Instagram } from "lucide-react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";

/* ---------------------------------------------------------------------
 * TalentBrowserModal — recruiter-grade browser for adding talents to a
 * project's casting pipeline.
 *
 * Source of truth: GET /api/talents (the global roster). We do not
 * maintain a duplicate talent store, schema, or collection. Selection
 * is committed via the existing POST /pipeline/add endpoint, which
 * lands the selected talents in the ASK TO TEST stage.
 *
 * Performance notes for 1000+ talents:
 *   • Lazy fetch on first open (no network on mount if modal stays shut).
 *   • Single client-side filter pass via useMemo.
 *   • Progressive rendering: render `visibleCount` rows, increment with
 *     an IntersectionObserver sentinel near the grid bottom.
 *   • All cards are memoised; only the changed card re-renders on toggle.
 *   • `loading="lazy"` on every image.
 * ------------------------------------------------------------------- */

const PAGE_SIZE = 60;

const FOLLOWER_BUCKETS = [
    { value: 0, label: "Any" },
    { value: 1_000, label: "1k+" },
    { value: 10_000, label: "10k+" },
    { value: 100_000, label: "100k+" },
    { value: 1_000_000, label: "1M+" },
];

// Parse "10k", "1.2M", "1000", "1,000", "100K" → integer
const parseFollowers = (s) => {
    if (s === null || s === undefined) return 0;
    const str = String(s).trim();
    if (!str) return 0;
    const m = str.match(/^([\d.,]+)\s*([kKmM]?)/);
    if (!m) return 0;
    const n = parseFloat(m[1].replace(/,/g, ""));
    if (Number.isNaN(n)) return 0;
    const unit = (m[2] || "").toLowerCase();
    const mult = unit === "m" ? 1_000_000 : unit === "k" ? 1_000 : 1;
    return Math.round(n * mult);
};

// Pretty-print integer followers as "10K", "1.2M" for card display.
const formatFollowers = (n) => {
    if (!n || n < 1_000) return n ? String(n) : "";
    if (n >= 1_000_000) {
        const v = n / 1_000_000;
        return `${v >= 10 ? Math.round(v) : v.toFixed(1)}M`;
    }
    const v = n / 1_000;
    return `${v >= 10 ? Math.round(v) : v.toFixed(1)}K`;
};

// Pull a usable image URL out of the talent record.
const pickImage = (t) => {
    if (t.image_url) return t.image_url;
    const media = t.media || [];
    const cover = media.find((m) => m.id === t.cover_media_id);
    if (cover?.url) return cover.url;
    const img = media.find(
        (m) =>
            m.category !== "video" &&
            (m.content_type?.startsWith?.("image/") ||
                ["portfolio", "indian", "western", "image"].includes(m.category)),
    );
    return img?.url || null;
};

const FILTER_DEFAULTS = {
    search: "",
    gender: "any",
    ethnicity: "any",
    location: "any",
    ageMin: "",
    ageMax: "",
    height: "",
    minFollowers: 0,
};

/* ------------------------------------------------------------------ */
/* Modal root                                                          */
/* ------------------------------------------------------------------ */
function TalentBrowserModal({ open, onClose, projectId, existingTalentIds, onAdded }) {
    const [talents, setTalents] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [selected, setSelected] = useState(new Set());
    const [submitting, setSubmitting] = useState(false);
    const [filters, setFilters] = useState(FILTER_DEFAULTS);
    const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

    const gridRef = useRef(null);
    const sentinelRef = useRef(null);

    // Fetch talents lazily — first time the modal opens. We never refetch
    // while the modal is open; opening again will reuse the cache unless
    // the user explicitly closes/reopens. For "freshness on every open",
    // we reset the cache in the close handler below.
    useEffect(() => {
        if (!open || talents.length > 0 || loading) return;
        let alive = true;
        (async () => {
            setLoading(true);
            setError(null);
            try {
                const res = await adminApi.get("/talents");
                if (!alive) return;
                // /talents returns an array directly (no pagination
                // wrapper) when no page/limit param is sent.
                const list = Array.isArray(res.data) ? res.data : res.data?.data || [];
                setTalents(list);
            } catch (e) {
                if (!alive) return;
                console.error("Failed to load talents:", e);
                setError("Failed to load talent roster");
            } finally {
                if (alive) setLoading(false);
            }
        })();
        return () => {
            alive = false;
        };
    }, [open, talents.length, loading]);

    // Reset selection + visible window every time the modal opens.
    useEffect(() => {
        if (open) {
            setSelected(new Set());
            setVisibleCount(PAGE_SIZE);
        }
    }, [open]);

    // ESC to close.
    useEffect(() => {
        if (!open) return;
        const onKey = (e) => {
            if (e.key === "Escape") onClose();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [open, onClose]);

    // Body scroll lock while open.
    useEffect(() => {
        if (!open) return;
        const prev = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => {
            document.body.style.overflow = prev;
        };
    }, [open]);

    // Distinct dropdown options derived from loaded roster.
    const { genders, ethnicities, locations } = useMemo(() => {
        const g = new Set();
        const e = new Set();
        const l = new Set();
        for (const t of talents) {
            if (t.gender) g.add(String(t.gender).trim());
            if (t.ethnicity) e.add(String(t.ethnicity).trim());
            if (t.location) l.add(String(t.location).trim());
        }
        const sort = (s) => Array.from(s).filter(Boolean).sort((a, b) => a.localeCompare(b));
        return { genders: sort(g), ethnicities: sort(e), locations: sort(l) };
    }, [talents]);

    // Apply filters — single pass, memoised on inputs.
    const filtered = useMemo(() => {
        const {
            search,
            gender,
            ethnicity,
            location,
            ageMin,
            ageMax,
            height,
            minFollowers,
        } = filters;
        const q = search.trim().toLowerCase();
        const ageMinN = ageMin === "" ? null : Number(ageMin);
        const ageMaxN = ageMax === "" ? null : Number(ageMax);
        const heightQ = height.trim().toLowerCase();

        return talents.filter((t) => {
            if (q) {
                const hay = [t.name, t.email, t.instagram_handle, t.location]
                    .filter(Boolean)
                    .join(" ")
                    .toLowerCase();
                if (!hay.includes(q)) return false;
            }
            if (gender !== "any" && t.gender !== gender) return false;
            if (ethnicity !== "any" && t.ethnicity !== ethnicity) return false;
            if (location !== "any" && t.location !== location) return false;
            if (ageMinN !== null && !Number.isNaN(ageMinN)) {
                if (!t.age || t.age < ageMinN) return false;
            }
            if (ageMaxN !== null && !Number.isNaN(ageMaxN)) {
                if (!t.age || t.age > ageMaxN) return false;
            }
            if (heightQ) {
                if (!t.height || !String(t.height).toLowerCase().includes(heightQ)) return false;
            }
            if (minFollowers > 0) {
                const n = parseFollowers(t.instagram_followers);
                if (n < minFollowers) return false;
            }
            return true;
        });
    }, [talents, filters]);

    // Reset visible window when filters change — otherwise the user could
    // scroll a full filtered list into view from a stale offset.
    useEffect(() => {
        setVisibleCount(PAGE_SIZE);
    }, [filters]);

    // Infinite-scroll sentinel — bumps visibleCount when it intersects.
    useEffect(() => {
        if (!open) return;
        const node = sentinelRef.current;
        if (!node) return;
        const observer = new IntersectionObserver(
            (entries) => {
                if (entries.some((e) => e.isIntersecting)) {
                    setVisibleCount((c) =>
                        c >= filtered.length ? c : c + PAGE_SIZE,
                    );
                }
            },
            { root: gridRef.current, rootMargin: "200px" },
        );
        observer.observe(node);
        return () => observer.disconnect();
    }, [open, filtered.length]);

    const filtersActive = useMemo(() => {
        return (
            filters.search.trim() !== "" ||
            filters.gender !== "any" ||
            filters.ethnicity !== "any" ||
            filters.location !== "any" ||
            filters.ageMin !== "" ||
            filters.ageMax !== "" ||
            filters.height.trim() !== "" ||
            filters.minFollowers > 0
        );
    }, [filters]);

    const setFilter = useCallback((key, value) => {
        setFilters((prev) => ({ ...prev, [key]: value }));
    }, []);

    const resetFilters = useCallback(() => {
        setFilters(FILTER_DEFAULTS);
    }, []);

    const toggleSelect = useCallback(
        (id, alreadyInPipeline) => {
            if (alreadyInPipeline) return;
            setSelected((prev) => {
                const next = new Set(prev);
                if (next.has(id)) next.delete(id);
                else next.add(id);
                return next;
            });
        },
        [],
    );

    const handleSubmit = async () => {
        if (selected.size === 0 || submitting) return;
        setSubmitting(true);
        const count = selected.size;
        try {
            await adminApi.post("/pipeline/add", {
                project_id: projectId,
                talent_ids: Array.from(selected),
            });
            toast.success(
                `Added ${count} ${count === 1 ? "talent" : "talents"} to Ask To Test`,
            );
            if (onAdded) await onAdded();
            // Reset and close.
            setSelected(new Set());
            onClose();
        } catch (e) {
            console.error("Add to pipeline failed:", e);
            toast.error(e?.response?.data?.detail || "Failed to add talents");
        } finally {
            setSubmitting(false);
        }
    };

    if (!open) return null;

    const visibleRows = filtered.slice(0, visibleCount);
    const hasMore = visibleCount < filtered.length;

    return (
        <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="talent-browser-title"
            data-testid="talent-browser-modal"
            className="fixed inset-0 z-50 flex items-stretch sm:items-center justify-center bg-black/80 backdrop-blur-sm p-0 sm:p-6"
            onMouseDown={(e) => {
                // Click on backdrop closes; click inside the panel does not.
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div
                className="
                    relative w-full sm:max-w-6xl h-full sm:h-[90vh]
                    flex flex-col
                    bg-[#0a0a0a] sm:rounded-2xl
                    border border-white/[0.08]
                    shadow-[0_24px_80px_-20px_rgba(0,0,0,0.8)]
                    overflow-hidden
                "
            >
                {/* Header */}
                <div className="flex items-start justify-between gap-4 px-5 sm:px-7 py-4 border-b border-white/[0.06] bg-black/40">
                    <div className="min-w-0">
                        <p className="eyebrow text-[10px] tracking-[0.22em] uppercase text-white/40 mb-1">
                            Roster
                        </p>
                        <h2
                            id="talent-browser-title"
                            className="font-display text-xl sm:text-2xl tracking-tight text-white truncate"
                        >
                            Add Talents
                        </h2>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        data-testid="talent-browser-close"
                        aria-label="Close"
                        className="shrink-0 w-9 h-9 rounded-full flex items-center justify-center border border-white/10 bg-white/[0.03] text-white/70 hover:text-white hover:border-white/30 transition-colors"
                    >
                        <X className="w-4 h-4" strokeWidth={1.6} />
                    </button>
                </div>

                {/* Filter bar — sticky */}
                <FilterBar
                    filters={filters}
                    setFilter={setFilter}
                    resetFilters={resetFilters}
                    filtersActive={filtersActive}
                    totalCount={talents.length}
                    filteredCount={filtered.length}
                    genders={genders}
                    ethnicities={ethnicities}
                    locations={locations}
                />

                {/* Grid */}
                <div
                    ref={gridRef}
                    className="flex-1 overflow-y-auto px-4 sm:px-6 py-5 tg-pipeline-scroll"
                    data-testid="talent-browser-grid"
                >
                    {loading && talents.length === 0 ? (
                        <div className="text-center py-24 text-white/50 text-sm">
                            Loading roster…
                        </div>
                    ) : error ? (
                        <div className="text-center py-24 text-rose-300/80 text-sm">
                            {error}
                        </div>
                    ) : filtered.length === 0 ? (
                        <EmptyResults onReset={resetFilters} hasFilters={filtersActive} />
                    ) : (
                        <>
                            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3 sm:gap-4">
                                {visibleRows.map((t) => {
                                    const already = existingTalentIds.has(t.id);
                                    return (
                                        <TalentCard
                                            key={t.id}
                                            talent={t}
                                            selected={selected.has(t.id)}
                                            alreadyInPipeline={already}
                                            onToggle={toggleSelect}
                                        />
                                    );
                                })}
                            </div>

                            {hasMore && (
                                <div
                                    ref={sentinelRef}
                                    className="py-8 text-center text-[11px] tracking-[0.2em] uppercase text-white/30"
                                >
                                    Loading more…
                                </div>
                            )}
                        </>
                    )}
                </div>

                {/* Footer */}
                <div className="flex flex-wrap items-center justify-between gap-3 px-5 sm:px-7 py-3.5 border-t border-white/[0.06] bg-black/40">
                    <div className="text-[11px] tracking-[0.18em] uppercase text-white/55 tg-mono">
                        <span data-testid="talent-browser-selected-count">
                            {selected.size}
                        </span>{" "}
                        selected
                        <span className="opacity-50 mx-1.5">·</span>
                        <span className="opacity-60">
                            {filtered.length} of {talents.length} talents
                        </span>
                    </div>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={onClose}
                            data-testid="talent-browser-cancel"
                            className="px-4 py-2 rounded-full text-[11px] tracking-[0.16em] uppercase border border-white/10 bg-white/[0.03] text-white/70 hover:text-white hover:border-white/25 transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={handleSubmit}
                            disabled={selected.size === 0 || submitting}
                            data-testid="talent-browser-add-selected"
                            className="
                                px-4 py-2 rounded-full text-[11px] tracking-[0.16em] uppercase font-medium
                                bg-white text-black hover:bg-white/90
                                disabled:bg-white/15 disabled:text-white/40 disabled:cursor-not-allowed
                                transition-colors
                            "
                        >
                            {submitting
                                ? "Adding…"
                                : selected.size === 0
                                  ? "Add to Ask To Test"
                                  : `Add ${selected.size} to Ask To Test`}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default TalentBrowserModal;

/* ------------------------------------------------------------------ */
/* Filter bar                                                          */
/* ------------------------------------------------------------------ */
const FilterBar = memo(function FilterBar({
    filters,
    setFilter,
    resetFilters,
    filtersActive,
    totalCount,
    filteredCount,
    genders,
    ethnicities,
    locations,
}) {
    return (
        <div className="border-b border-white/[0.06] bg-black/30 backdrop-blur-md">
            <div className="px-4 sm:px-6 py-3 flex flex-col gap-2.5">
                {/* Row 1 — search + reset */}
                <div className="flex items-center gap-2">
                    <div className="relative flex-1 min-w-0">
                        <Search
                            className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/35"
                            strokeWidth={1.6}
                        />
                        <input
                            type="text"
                            value={filters.search}
                            onChange={(e) => setFilter("search", e.target.value)}
                            placeholder="Search by name, email, instagram…"
                            data-testid="talent-browser-search"
                            className="
                                w-full bg-black/40 border border-white/[0.08]
                                rounded-full pl-9 pr-3 py-1.5
                                text-[12.5px] text-white/90 placeholder-white/30
                                focus:outline-none focus:border-white/30 focus:bg-black/60
                                transition-colors
                            "
                        />
                    </div>
                    {filtersActive && (
                        <button
                            type="button"
                            onClick={resetFilters}
                            data-testid="talent-browser-reset-filters"
                            className="shrink-0 px-3 py-1.5 rounded-full text-[10px] tracking-[0.18em] uppercase text-white/55 hover:text-rose-200 bg-white/[0.03] hover:bg-rose-300/10 border border-white/[0.08] hover:border-rose-300/20 transition-colors"
                        >
                            Reset
                        </button>
                    )}
                    <span className="hidden sm:inline shrink-0 text-[10px] tg-mono text-white/40">
                        {filteredCount}/{totalCount}
                    </span>
                </div>

                {/* Row 2 — filter pills (scrollable on mobile) */}
                <div className="flex items-center gap-2 overflow-x-auto tg-pipeline-scroll lg:flex-wrap -mx-1 px-1">
                    <SelectPill
                        label="Gender"
                        value={filters.gender}
                        onChange={(v) => setFilter("gender", v)}
                        options={[
                            { value: "any", label: "Any" },
                            ...genders.map((g) => ({ value: g, label: g })),
                        ]}
                        testid="talent-browser-filter-gender"
                    />
                    <SelectPill
                        label="Ethnicity"
                        value={filters.ethnicity}
                        onChange={(v) => setFilter("ethnicity", v)}
                        options={[
                            { value: "any", label: "Any" },
                            ...ethnicities.map((g) => ({ value: g, label: g })),
                        ]}
                        testid="talent-browser-filter-ethnicity"
                    />
                    <SelectPill
                        label="Location"
                        value={filters.location}
                        onChange={(v) => setFilter("location", v)}
                        options={[
                            { value: "any", label: "Any" },
                            ...locations.map((g) => ({ value: g, label: g })),
                        ]}
                        testid="talent-browser-filter-location"
                    />
                    <NumberRangePill
                        label="Age"
                        min={filters.ageMin}
                        max={filters.ageMax}
                        onMin={(v) => setFilter("ageMin", v)}
                        onMax={(v) => setFilter("ageMax", v)}
                        testid="talent-browser-filter-age"
                    />
                    <TextPill
                        label="Height"
                        placeholder={`e.g. 5'10"`}
                        value={filters.height}
                        onChange={(v) => setFilter("height", v)}
                        testid="talent-browser-filter-height"
                    />
                    <SelectPill
                        label="IG followers"
                        value={String(filters.minFollowers)}
                        onChange={(v) => setFilter("minFollowers", Number(v))}
                        options={FOLLOWER_BUCKETS.map((b) => ({
                            value: String(b.value),
                            label: b.label,
                        }))}
                        testid="talent-browser-filter-followers"
                    />
                </div>
            </div>
        </div>
    );
});

function SelectPill({ label, value, onChange, options, testid }) {
    return (
        <label
            data-testid={testid}
            className="shrink-0 flex items-center gap-2 bg-white/[0.03] border border-white/[0.06] rounded-full pl-3 pr-1.5 py-1"
        >
            <span className="text-[9px] tracking-[0.18em] uppercase text-white/40">
                {label}
            </span>
            <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="bg-transparent text-[11px] text-white/85 focus:outline-none cursor-pointer pr-1"
            >
                {options.map((opt) => (
                    <option
                        key={opt.value}
                        value={opt.value}
                        className="bg-black text-white"
                    >
                        {opt.label}
                    </option>
                ))}
            </select>
        </label>
    );
}

function NumberRangePill({ label, min, max, onMin, onMax, testid }) {
    return (
        <div
            data-testid={testid}
            className="shrink-0 flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] rounded-full pl-3 pr-2 py-1"
        >
            <span className="text-[9px] tracking-[0.18em] uppercase text-white/40">
                {label}
            </span>
            <input
                type="number"
                inputMode="numeric"
                value={min}
                onChange={(e) => onMin(e.target.value)}
                placeholder="min"
                className="w-12 bg-transparent text-[11px] text-white/85 placeholder-white/25 focus:outline-none"
            />
            <span className="text-white/30 text-[10px]">–</span>
            <input
                type="number"
                inputMode="numeric"
                value={max}
                onChange={(e) => onMax(e.target.value)}
                placeholder="max"
                className="w-12 bg-transparent text-[11px] text-white/85 placeholder-white/25 focus:outline-none"
            />
        </div>
    );
}

function TextPill({ label, value, onChange, placeholder, testid }) {
    return (
        <div
            data-testid={testid}
            className="shrink-0 flex items-center gap-2 bg-white/[0.03] border border-white/[0.06] rounded-full pl-3 pr-2 py-1"
        >
            <span className="text-[9px] tracking-[0.18em] uppercase text-white/40">
                {label}
            </span>
            <input
                type="text"
                value={value}
                onChange={(e) => onChange(e.target.value)}
                placeholder={placeholder}
                className="w-24 bg-transparent text-[11px] text-white/85 placeholder-white/25 focus:outline-none"
            />
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Talent card                                                         */
/* ------------------------------------------------------------------ */
const TalentCard = memo(function TalentCard({
    talent,
    selected,
    alreadyInPipeline,
    onToggle,
}) {
    const img = pickImage(talent);
    const followers = parseFollowers(talent.instagram_followers);
    const followersLabel = formatFollowers(followers);

    const handleClick = () => onToggle(talent.id, alreadyInPipeline);

    const borderClass = alreadyInPipeline
        ? "border-white/[0.05]"
        : selected
          ? "border-white/80 ring-1 ring-white/30"
          : "border-white/[0.08] hover:border-white/25";

    return (
        <button
            type="button"
            onClick={handleClick}
            disabled={alreadyInPipeline}
            aria-pressed={selected}
            data-testid={`talent-browser-card-${talent.id}`}
            className={`
                relative text-left
                rounded-lg overflow-hidden
                bg-white/[0.02]
                border transition-all duration-200
                ${borderClass}
                ${alreadyInPipeline ? "opacity-55 cursor-not-allowed" : "cursor-pointer hover:-translate-y-[1px]"}
            `}
        >
            {/* Selection / status badge */}
            {alreadyInPipeline ? (
                <span
                    data-testid={`talent-browser-already-${talent.id}`}
                    className="absolute top-2 left-2 z-10 px-2 py-0.5 rounded-full bg-black/70 border border-white/15 text-[8.5px] tracking-[0.16em] uppercase text-white/70 backdrop-blur-sm"
                >
                    In pipeline
                </span>
            ) : (
                <span
                    className={`
                        absolute top-2 left-2 z-10 w-6 h-6 rounded-full
                        flex items-center justify-center transition-all
                        ${
                            selected
                                ? "bg-white border-white text-black"
                                : "bg-black/60 border border-white/30 text-transparent"
                        }
                        border
                    `}
                    aria-hidden
                >
                    {selected && <Check className="w-3.5 h-3.5" strokeWidth={2} />}
                </span>
            )}

            {/* Image */}
            <div className="aspect-[3/4] bg-[#0c0c0c] overflow-hidden">
                {img ? (
                    <img
                        src={img}
                        alt={talent.name || ""}
                        loading="lazy"
                        decoding="async"
                        className="w-full h-full object-cover"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-white/20">
                        <ImageIcon className="w-7 h-7" strokeWidth={1} />
                    </div>
                )}
            </div>

            {/* Meta */}
            <div className="p-2.5 space-y-1">
                <p className="text-[12.5px] text-white/95 font-medium truncate leading-tight">
                    {talent.name || "Unnamed Talent"}
                </p>
                <div className="text-[10px] text-white/45 tg-mono flex items-center gap-1.5 flex-wrap leading-tight">
                    {talent.age && <span>{talent.age}</span>}
                    {talent.age && talent.height && <Dot />}
                    {talent.height && <span>{talent.height}</span>}
                    {talent.location && (talent.age || talent.height) && <Dot />}
                    {talent.location && (
                        <span className="truncate">{talent.location}</span>
                    )}
                </div>
                {(talent.instagram_handle || followersLabel) && (
                    <div className="text-[10px] text-white/40 tg-mono flex items-center gap-1.5 truncate pt-0.5">
                        <Instagram
                            className="w-2.5 h-2.5 shrink-0 text-white/35"
                            strokeWidth={1.6}
                        />
                        {talent.instagram_handle && (
                            <span className="truncate">{talent.instagram_handle}</span>
                        )}
                        {followersLabel && (
                            <>
                                {talent.instagram_handle && <Dot />}
                                <span className="shrink-0">{followersLabel}</span>
                            </>
                        )}
                    </div>
                )}
            </div>
        </button>
    );
});

const Dot = () => <span className="text-white/20">·</span>;

/* ------------------------------------------------------------------ */
/* Empty results                                                       */
/* ------------------------------------------------------------------ */
function EmptyResults({ onReset, hasFilters }) {
    return (
        <div className="flex flex-col items-center justify-center text-center py-24">
            <div className="w-12 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent mb-4" />
            <p className="text-[11px] tracking-[0.22em] uppercase text-white/45 mb-2">
                {hasFilters ? "No matches" : "No talents available"}
            </p>
            <p className="text-sm text-white/55 max-w-sm leading-relaxed">
                {hasFilters
                    ? "Try widening the filters or clearing the search."
                    : "Add talents from the roster page first."}
            </p>
            {hasFilters && (
                <button
                    type="button"
                    onClick={onReset}
                    className="mt-5 px-4 py-2 rounded-full text-[10px] tracking-[0.18em] uppercase font-medium text-black bg-white/95 hover:bg-white transition-colors"
                >
                    Clear filters
                </button>
            )}
        </div>
    );
}
