import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { Search, Plus, Check, User } from "lucide-react";
import { toast } from "sonner";
import BulkSelectBar from "@/components/BulkSelectBar";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";

// ---------------------------------------------------------------------------
// Skeleton card — prevents layout shift during load
// ---------------------------------------------------------------------------
function SkeletonCard() {
    return (
        <div className="rounded-xl border border-black/[0.06] bg-white overflow-hidden animate-pulse">
            <div className="aspect-[3/4] bg-black/[0.05]" />
            <div className="p-4 space-y-2">
                <div className="h-3.5 bg-black/[0.07] rounded w-3/4" />
                <div className="h-2.5 bg-black/[0.04] rounded w-1/2" />
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Initials avatar — stable placeholder when no cover image is set
// ---------------------------------------------------------------------------
function Initials({ name }) {
    const letters = (name || "")
        .split(" ")
        .filter(Boolean)
        .slice(0, 2)
        .map((w) => w[0].toUpperCase())
        .join("");
    return (
        <div className="w-full h-full flex items-center justify-center bg-black/[0.04]">
            {letters ? (
                <span className="text-2xl font-medium text-black/25 select-none tracking-wide">
                    {letters}
                </span>
            ) : (
                <User className="w-8 h-8 text-black/20" strokeWidth={1} />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Lazy image with blur-up reveal — no layout shift
// ---------------------------------------------------------------------------
function TalentThumb({ src, alt }) {
    const [loaded, setLoaded] = useState(false);
    const [errored, setErrored] = useState(false);

    // Reset state when src changes (e.g. after cover change)
    const prevSrc = useRef(src);
    if (prevSrc.current !== src) {
        prevSrc.current = src;
        if (loaded) setLoaded(false);
        if (errored) setErrored(false);
    }

    if (!src || errored) {
        return <Initials name={alt} />;
    }

    return (
        <>
            {/* Placeholder shown while image loads */}
            {!loaded && <Initials name={alt} />}
            <img
                src={src}
                alt={alt}
                loading="lazy"
                decoding="async"
                onLoad={() => setLoaded(true)}
                onError={() => setErrored(true)}
                className={[
                    "absolute inset-0 w-full h-full object-cover transition-all duration-500 ease-out group-hover:scale-105",
                    loaded ? "opacity-100" : "opacity-0",
                ].join(" ")}
            />
        </>
    );
}

// ---------------------------------------------------------------------------
// Single roster card — memoized to avoid re-renders on parent state changes
// ---------------------------------------------------------------------------
const TalentCard = React.memo(function TalentCard({
    t,
    checked,
    isSelectionMode,
    canBulkDelete,
    onToggle,
}) {
    const handleToggle = useCallback(
        (e) => {
            e.stopPropagation();
            onToggle(t.id);
        },
        [t.id, onToggle]
    );

    const cardContent = (
        <>
            {/* Thumbnail — aspect-ratio container prevents layout shift */}
            <div className="aspect-[3/4] bg-black/[0.04] rounded-t-xl overflow-hidden relative">
                <TalentThumb src={t.image_url} alt={t.name} />
                {/* Asset count badge */}
                {t.media_count > 0 && (
                    <div className="absolute bottom-2 right-2 bg-black/60 text-white text-[10px] font-medium px-1.5 py-0.5 rounded-md backdrop-blur-sm">
                        {t.media_count}
                    </div>
                )}
            </div>

            {/* Card metadata */}
            <div className="p-3">
                <div className="font-semibold text-[13.5px] leading-snug tracking-tight text-neutral-800 truncate">
                    {t.name || "—"}
                </div>
                <div className="text-[11px] text-neutral-400 mt-0.5 truncate">
                    {[t.location, t.category].filter(Boolean).join(" · ") || "\u00a0"}
                </div>
                {/* Compact tag pills — max 2 visible + overflow badge */}
                {(t.tags || []).length > 0 && (
                    <div className="flex items-center gap-1 mt-1.5 flex-wrap">
                        {(t.tags || []).slice(0, 2).map((tag) => (
                            <span
                                key={tag.id}
                                className="px-1.5 py-0.5 rounded text-[9px] tracking-[0.05em] bg-black/[0.05] text-black/50 border border-black/[0.06] truncate max-w-[72px]"
                                title={tag.name}
                            >
                                {tag.name}
                            </span>
                        ))}
                        {(t.tags || []).length > 2 && (
                            <span className="px-1.5 py-0.5 rounded text-[9px] bg-black/[0.03] text-black/35 border border-black/[0.05]">
                                +{(t.tags || []).length - 2}
                            </span>
                        )}
                    </div>
                )}
            </div>
        </>
    );


    return (
        <div
            key={t.id}
            data-testid={`talent-card-${t.id}`}
            className={[
                "group relative rounded-xl border bg-white transition-all duration-300 ease-out",
                checked
                    ? "border-black/45 ring-1 ring-black/25 shadow-sm"
                    : "border-black/[0.07] hover:border-black/[0.18] hover:shadow-[0_8px_30px_rgb(0,0,0,0.04)] hover:-translate-y-0.5",
            ].join(" ")}
        >
            {/* Selection checkbox */}
            {canBulkDelete && (
                <button
                    type="button"
                    onClick={handleToggle}
                    aria-label={checked ? "Deselect talent" : "Select talent"}
                    data-testid={`talent-check-${t.id}`}
                    className={[
                        "absolute top-2.5 left-2.5 z-10 w-5 h-5 rounded-md border flex items-center justify-center",
                        "transition-all duration-150",
                        checked
                            ? "bg-black border-black text-white opacity-100"
                            : "bg-white/90 border-black/20 text-transparent",
                        isSelectionMode
                            ? "opacity-100"
                            : "opacity-0 group-hover:opacity-100 group-hover:border-black/35",
                    ].join(" ")}
                >
                    {checked && <Check className="w-3 h-3" strokeWidth={2.5} />}
                </button>
            )}

            {/* Card body — link or button depending on selection mode */}
            {!isSelectionMode ? (
                <Link
                    to={`/admin/talents/${t.id}`}
                    className="block focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black/30 rounded-xl"
                >
                    {cardContent}
                </Link>
            ) : (
                <button
                    type="button"
                    onClick={() => onToggle(t.id)}
                    className="block w-full text-left"
                >
                    {cardContent}
                </button>
            )}
        </div>
    );
});

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function TalentList() {
    const [talents, setTalents] = useState([]);
    const [q, setQ] = useState("");
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [totalItems, setTotalItems] = useState(0);
    const canBulkDelete = isAdmin();

    const pageSize = 40;

    // ── Data fetching ────────────────────────────────────────────────────────
    const load = useCallback(async (qq = "", pageNum = 1) => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/talents", {
                params: {
                    ...(qq ? { q: qq } : {}),
                    page: pageNum - 1,
                    size: pageSize,
                },
            });
            if (Array.isArray(data)) {
                setTalents(data);
                setTotalPages(1);
                setTotalItems(data.length);
            } else {
                setTalents(data?.items ?? []);
                setTotalPages(data?.pages ?? 1);
                setTotalItems(data?.total ?? 0);
            }
        } finally {
            setLoading(false);
        }
    }, []);

    const qRef = useRef(q);
    useEffect(() => {
        qRef.current = q;
    }, [q]);

    // Initial load
    useEffect(() => {
        load(qRef.current, page);
    }, [page, load]);

    // Debounced search — 250 ms (resets to page 1)
    useEffect(() => {
        const t = setTimeout(() => {
            setPage(1);
            load(q, 1);
        }, 250);
        return () => clearTimeout(t);
    }, [q, load]);

    // Re-fetch when navigating back from detail page.
    useEffect(() => {
        const onVisible = () => {
            if (document.visibilityState === "visible") {
                load(qRef.current, page);
            }
        };
        document.addEventListener("visibilitychange", onVisible);
        return () => document.removeEventListener("visibilitychange", onVisible);
    }, [load, page]);

    // ── Selection ────────────────────────────────────────────────────────────
    const toggle = useCallback((id) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const clear = useCallback(() => setSelected(new Set()), []);
    const selectAll = useCallback(
        () => setSelected(new Set(talents.map((t) => t.id))),
        [talents]
    );

    const isSelectionMode = selected.size > 0;

    // ── Bulk delete ──────────────────────────────────────────────────────────
    const bulkDelete = useCallback(async () => {
        const ids = Array.from(selected);
        try {
            const res = await adminApi.post("/talents/bulk-delete", { ids });
            toast.success(
                `Deleted ${res.data.deleted} talent${res.data.deleted === 1 ? "" : "s"}${
                    res.data.missing ? ` (${res.data.missing} already gone)` : ""
                }`
            );
            clear();
            setConfirmOpen(false);
            load(q);
        } catch (err) {
            toast.error(
                err?.response?.data?.detail ?? err?.message ?? "Bulk delete failed"
            );
            throw err;
        }
    }, [selected, clear, load, q]);

    // ── Stats bar ────────────────────────────────────────────────────────────
    const totalAssets = useMemo(
        () => talents.reduce((sum, t) => sum + (t.media_count || 0), 0),
        [talents]
    );

    // ── Render ───────────────────────────────────────────────────────────────
    return (
        <div className="p-6 md:p-10 max-w-7xl mx-auto" data-testid="talent-list-page">

            {/* Header */}
            <div className="flex items-end justify-between mb-8 flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Roster</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        Talents
                    </h1>
                    {!loading && totalItems > 0 && (
                        <p className="text-[11px] text-black/35 mt-2 tracking-wide uppercase">
                            {totalItems} talent{totalItems !== 1 ? "s" : ""}
                            {totalPages > 1 && ` · Page ${page} of ${totalPages}`}
                            {totalAssets > 0 && ` · ${totalAssets} assets on page`}
                        </p>
                    )}
                </div>
                <Link
                    to="/admin/talents/new"
                    data-testid="new-talent-btn"
                    className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/85 active:bg-black transition-colors duration-150"
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} />
                    Add Talent
                </Link>
            </div>

            {/* Search */}
            <div className="mb-8 relative max-w-md">
                <Search className="absolute left-0 top-3 w-4 h-4 text-black/30 pointer-events-none" />
                <input
                    type="text"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Search by name…"
                    data-testid="talent-search-input"
                    autoComplete="off"
                    spellCheck={false}
                    className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-3 pl-7 text-sm text-black/85 placeholder:text-black/28 transition-colors duration-150"
                />
            </div>

            {/* Grid */}
            {loading ? (
                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-5 pb-20"
                    aria-label="Loading talents"
                >
                    {Array.from({ length: 8 }).map((_, i) => (
                        <SkeletonCard key={i} />
                    ))}
                </div>
            ) : talents.length === 0 ? (
                <div className="border border-black/[0.08] bg-white rounded-xl p-14 text-center">
                    <User className="w-10 h-10 text-black/18 mx-auto mb-4" strokeWidth={1} />
                    <p className="text-black/55 mb-2 font-medium">
                        {q ? `No results for "${q}"` : "No talents yet"}
                    </p>
                    {!q && (
                        <Link
                            to="/admin/talents/new"
                            className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/85 transition-colors duration-150 mt-4"
                        >
                            Add your first talent
                        </Link>
                    )}
                </div>
            ) : (
                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-5 pb-20"
                    data-testid="talents-grid"
                >
                    {talents.map((t) => (
                        <TalentCard
                            key={t.id}
                            t={t}
                            checked={selected.has(t.id)}
                            isSelectionMode={isSelectionMode}
                            canBulkDelete={canBulkDelete}
                            onToggle={toggle}
                        />
                    ))}
                </div>
            )}

            {/* Pagination Controls */}
            {!loading && totalPages > 1 && (
                <div className="flex items-center justify-center gap-4 mt-2 mb-12 animate-fadeIn" data-testid="talents-pagination">
                    <button
                        type="button"
                        disabled={page === 1}
                        onClick={() => {
                            setPage((p) => Math.max(1, p - 1));
                            window.scrollTo({ top: 0, behavior: "smooth" });
                        }}
                        className="px-4 py-2 border border-black/[0.08] bg-white hover:border-black/30 rounded-lg text-xs font-medium disabled:opacity-40 disabled:hover:border-black/[0.08] transition-colors select-none active:scale-[0.98]"
                    >
                        Previous
                    </button>
                    <span className="text-xs font-mono text-black/55">
                        Page {page} of {totalPages}
                    </span>
                    <button
                        type="button"
                        disabled={page === totalPages}
                        onClick={() => {
                            setPage((p) => Math.min(totalPages, p + 1));
                            window.scrollTo({ top: 0, behavior: "smooth" });
                        }}
                        className="px-4 py-2 border border-black/[0.08] bg-white hover:border-black/30 rounded-lg text-xs font-medium disabled:opacity-40 disabled:hover:border-black/[0.08] transition-colors select-none active:scale-[0.98]"
                    >
                        Next
                    </button>
                </div>
            )}

            {/* Bulk action bar */}
            {canBulkDelete && (
                <BulkSelectBar
                    count={selected.size}
                    total={talents.length}
                    allSelected={selected.size === talents.length}
                    onSelectAll={selectAll}
                    onClear={clear}
                    onDelete={() => setConfirmOpen(true)}
                    labelSingular="talent"
                    labelPlural="talents"
                    testid="talents-bulk-bar"
                />
            )}

            <ConfirmDeleteDialog
                open={confirmOpen}
                title={`Delete ${selected.size} talent${selected.size === 1 ? "" : "s"}?`}
                description="This permanently removes the selected talent records and all their portfolio media. Submissions attached to these talents remain (they live on the project). This cannot be undone."
                confirmLabel={`Delete ${selected.size}`}
                typeToConfirm="DELETE"
                onCancel={() => setConfirmOpen(false)}
                onConfirm={bulkDelete}
                testid="talents-bulk-confirm"
            />
        </div>
    );
}
