import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { formatTalentLocation } from "@/lib/sanitize";
import { instagramProfileUrl } from "@/lib/mediaUtils";
import { Search, Plus, Check, User, LayoutGrid, List, Tag, Instagram, SlidersHorizontal } from "lucide-react";
import { toast } from "sonner";
import BulkSelectBar from "@/components/BulkSelectBar";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import TagPopover from "@/components/TagPopover";
import BulkTagDialog from "@/components/BulkTagDialog";
import AddToProjectModal from "@/components/AddToProjectModal";
import { talentPreviewCache } from "@/lib/talentPreviewCache";
import { useTalentDirectory } from "@/hooks/useTalentDirectory";
import FilterPanel from "@/components/talent-directory/FilterPanel";
import MobileFilterSheet from "@/components/talent-directory/MobileFilterSheet";
import SortDropdown from "@/components/talent-directory/SortDropdown";
import FilterChips from "@/components/talent-directory/FilterChips";

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

    const getGradientClass = (str) => {
        if (!str) return "from-slate-50 to-slate-100 text-slate-400";
        const sum = str.split("").reduce((acc, char) => acc + char.charCodeAt(0), 0);
        const gradients = [
            "from-[#f8fafc] via-[#f1f5f9] to-[#e2e8f0] text-slate-600 border-slate-200/40",
            "from-[#f0fdfa] via-[#f0fdfa] to-[#ccfbf1] text-teal-800 border-teal-200/30",
            "from-[#faf5ff] via-[#f3e8ff] to-[#e9d5ff] text-purple-800 border-purple-200/30",
            "from-[#eff6ff] via-[#dbeafe] to-[#bfdbfe] text-blue-800 border-blue-200/30",
            "from-[#fff5f5] via-[#fed7d7] to-[#feb2b2] text-red-800 border-red-200/30",
        ];
        return gradients[sum % gradients.length];
    };

    const gradientClass = getGradientClass(letters);

    return (
        <div className={`w-full h-full flex flex-col items-center justify-center bg-gradient-to-b ${gradientClass} border-b transition-all duration-300 relative overflow-hidden`}>
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(255,255,255,0.2)_0%,transparent_75%)] pointer-events-none" />
            {letters ? (
                <span className="text-[28px] font-bold tracking-tight select-none font-display drop-shadow-[0_1px_1px_rgba(0,0,0,0.03)]">
                    {letters}
                </span>
            ) : (
                <User className="w-10 h-10 opacity-40" strokeWidth={1.5} />
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
    onTagClick,
}) {
    const handleToggle = useCallback(
        (e) => {
            e.stopPropagation();
            onToggle(t.id);
        },
        [t.id, onToggle]
    );

    const handleTagClick = useCallback(
        (e) => {
            e.stopPropagation();
            e.preventDefault();
            onTagClick(t);
        },
        [t, onTagClick]
    );

    const igUrl = instagramProfileUrl(t.instagram_handle);

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
                <div className="flex items-center justify-between gap-1.5 mt-0.5">
                <div className="text-[11px] text-neutral-500 truncate">
                    {[formatTalentLocation(t.location), t.category].filter(Boolean).join(" · ") || "\u00a0"}
                </div>
                {igUrl && (
                    <a
                        href={igUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Open ${t.name || "talent"}'s Instagram profile`}
                        data-testid={`talent-instagram-btn-mobile-${t.id}`}
                        className="md:hidden shrink-0 flex items-center justify-center w-10 h-10 -my-2 -mr-1 text-black/50 active:text-black active:scale-95 transition-all"
                    >
                        <Instagram className="w-4 h-4" />
                    </a>
                )}
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

            {/* Tag Assignment Trigger */}
            <button
                type="button"
                onClick={handleTagClick}
                aria-label="Manage tags"
                data-testid={`talent-tag-btn-${t.id}`}
                className={[
                    "absolute top-2 right-2 z-10 w-11 h-11 md:w-8 md:h-8 rounded-full bg-white/90 border border-black/15 shadow-sm flex items-center justify-center text-black/60 hover:text-black hover:bg-white active:scale-95 transition-all",
                    checked
                        ? "opacity-100"
                        : "opacity-100 md:opacity-0 md:group-hover:opacity-100",
                ].join(" ")}
            >
                <Tag className="w-4.5 h-4.5 md:w-3.5 md:h-3.5" />
            </button>

            {/* Instagram shortcut — external tab only; Instagram blocks embedding.
                Desktop only: on mobile this floating icon sat over the image
                and could cover the talent's face, so mobile gets its own
                shortcut inside the info row instead (see cardContent below). */}
            {igUrl && (
                <a
                    href={igUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`Open ${t.name || "talent"}'s Instagram profile`}
                    title="Open Instagram"
                    data-testid={`talent-instagram-btn-${t.id}`}
                    className={[
                        "hidden md:flex absolute top-2 right-11 z-10 w-8 h-8 rounded-full bg-white/90 border border-black/15 shadow-sm items-center justify-center text-black/60 hover:text-black hover:bg-white active:scale-95 transition-all",
                        checked ? "opacity-100" : "opacity-0 group-hover:opacity-100",
                    ].join(" ")}
                >
                    <Instagram className="w-3.5 h-3.5" />
                </a>
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
// Single roster row — memoized to avoid re-renders on parent state changes
// ---------------------------------------------------------------------------
const TalentListRow = React.memo(function TalentListRow({
    t,
    checked,
    isSelectionMode,
    canBulkDelete,
    onToggle,
    onTagClick,
}) {
    const handleToggle = useCallback(
        (e) => {
            e.stopPropagation();
            onToggle(t.id);
        },
        [t.id, onToggle]
    );

    const handleTagClick = useCallback(
        (e) => {
            e.stopPropagation();
            e.preventDefault();
            onTagClick(t);
        },
        [t, onTagClick]
    );

    // Filter media arrays safely
    const mediaList = t.media || [];
    const imageCount = t.media_count !== undefined 
        ? t.media_count 
        : mediaList.filter(m => m.category !== "video").length;
    const videoCount = mediaList.filter(m => m.category === "video" || m.content_type?.startsWith("video/")).length;

    const formattedDate = useMemo(() => {
        const dateStr = t.updated_at || t.created_at;
        if (!dateStr) return "—";
        try {
            const d = new Date(dateStr);
            if (isNaN(d.getTime())) return "—";
            return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
        } catch {
            return "—";
        }
    }, [t.updated_at, t.created_at]);

    const handleWhatsApp = useCallback((e) => {
        e.stopPropagation();
        e.preventDefault();
        if (t.phone) {
            const cleaned = t.phone.replace(/\D/g, '');
            const url = `https://wa.me/${cleaned}`;
            window.open(url, '_blank', 'noopener,noreferrer');
        } else {
            toast.error("No phone number available");
        }
    }, [t.phone]);

    const igUrl = instagramProfileUrl(t.instagram_handle);

    return (
        <div
            data-testid={`talent-row-${t.id}`}
            onClick={isSelectionMode ? handleToggle : undefined}
            className={[
                "group relative rounded-xl border bg-white p-3.5 transition-all duration-300 ease-out flex flex-wrap items-center justify-between gap-4",
                isSelectionMode ? "cursor-pointer" : "",
                checked
                    ? "border-black/45 ring-1 ring-black/25 shadow-sm"
                    : "border-black/[0.07] hover:border-black/[0.18] hover:shadow-[0_4px_20px_rgb(0,0,0,0.02)]",
            ].join(" ")}
        >
            {/* Left Section: Checkbox + Photo + Identity info.
                min-w-[180px] (not min-w-0) so this flex-1 block always
                registers real width in the wrap calculation — flex-basis:0
                from flex-1 alone reads as "needs no space", so rows with a
                small shrink-0 actions block (e.g. no tags) never trigger a
                wrap and the name collapses to 0 instead of dropping the
                actions to their own line. */}
            <div className="flex items-center gap-3.5 min-w-[180px] flex-1">
                {/* Selection checkbox */}
                {canBulkDelete && (
                    <button
                        type="button"
                        onClick={handleToggle}
                        aria-label={checked ? "Deselect talent" : "Select talent"}
                        data-testid={`talent-check-${t.id}`}
                        className={[
                            "w-5 h-5 rounded-md border flex items-center justify-center shrink-0",
                            "transition-all duration-150",
                            checked
                                ? "bg-black border-black text-white opacity-100"
                                : "bg-white border-black/20 text-transparent",
                            isSelectionMode
                                ? "opacity-100"
                                : "opacity-0 group-hover:opacity-100 group-hover:border-black/35",
                        ].join(" ")}
                    >
                        {checked && <Check className="w-3 h-3" strokeWidth={2.5} />}
                    </button>
                )}

                {/* Profile Photo */}
                <div className="w-11 h-11 rounded-lg overflow-hidden bg-black/[0.04] shrink-0 relative border border-black/[0.04]">
                    <TalentThumb src={t.image_url} alt={t.name} />
                </div>

                {/* Info block */}
                <div className="min-w-0 flex-1 grid grid-cols-1 md:grid-cols-12 gap-2 md:gap-4 items-center">
                    {/* Name & Tags */}
                    <div className="md:col-span-4 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                            {!isSelectionMode ? (
                                <Link
                                    to={`/admin/talents/${t.id}`}
                                    className="font-semibold text-sm leading-snug tracking-tight text-neutral-800 hover:text-black hover:underline truncate block"
                                >
                                    {t.name || "—"}
                                </Link>
                            ) : (
                                <span className="font-semibold text-sm leading-snug tracking-tight text-neutral-800 truncate block">
                                    {t.name || "—"}
                                </span>
                            )}
                            
                            {/* Tags display in row */}
                            {(t.tags || []).length > 0 && (
                                <div className="flex items-center gap-1 flex-wrap">
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
                        {/* Mobile metadata summary */}
                        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-neutral-400 mt-0.5 md:hidden">
                            {t.gender && <span className="capitalize">{t.gender}</span>}
                            {t.age && <span>· {t.age} yrs</span>}
                            {t.height && <span>· {t.height}</span>}
                            {formatTalentLocation(t.location) && <span className="truncate">· {formatTalentLocation(t.location)}</span>}
                        </div>
                    </div>

                    {/* Desktop/Tablet Columns */}
                    {/* Demographics */}
                    <div className="hidden md:flex md:col-span-3 text-[12px] text-neutral-500 flex-wrap items-center gap-x-2 gap-y-0.5">
                        {t.gender && <span className="capitalize font-medium text-neutral-700">{t.gender}</span>}
                        {t.age && <span>· {t.age} yrs</span>}
                        {t.height && <span>· {t.height}</span>}
                    </div>

                    {/* Location */}
                    <div className="hidden md:block md:col-span-2 text-[12px] text-neutral-500 truncate" title={formatTalentLocation(t.location)}>
                        {formatTalentLocation(t.location) || "—"}
                    </div>

                    {/* Media Counts */}
                    <div className="hidden md:flex md:col-span-1.5 items-center gap-3 text-[12px] text-neutral-500 shrink-0 font-medium">
                        <span className="flex items-center gap-1">
                            📷 {imageCount}
                        </span>
                        <span className="flex items-center gap-1">
                            🎥 {videoCount}
                        </span>
                    </div>

                    {/* Last Updated */}
                    <div className="hidden md:block md:col-span-1.5 text-[11px] text-neutral-400 tracking-tight font-mono shrink-0">
                        {formattedDate}
                    </div>
                </div>
            </div>

            {/* Right Section: Media icons for mobile + Actions */}
            <div className="flex items-center gap-2 shrink-0">
                {/* Mobile media counts */}
                <div className="flex md:hidden items-center gap-2 text-[10px] text-neutral-400 mr-2 shrink-0 font-medium">
                    <span>📷 {imageCount}</span>
                    <span>🎥 {videoCount}</span>
                </div>

                {/* Actions */}
                <div className="flex items-center gap-1">
                    {!isSelectionMode && (
                        <>
                            <button
                                type="button"
                                onClick={handleTagClick}
                                aria-label="Manage tags"
                                data-testid={`talent-tag-btn-${t.id}`}
                                className="inline-flex items-center justify-center border border-black/[0.08] hover:border-black/30 hover:bg-black/[0.02] bg-white text-black text-[11px] font-medium w-11 h-11 md:w-auto md:px-2.5 md:py-1.5 rounded-lg transition-colors select-none min-h-[44px] shrink-0"
                                title="Manage Tags"
                            >
                                <Tag className="w-4.5 h-4.5 md:w-3.5 md:h-3.5 md:mr-1" />
                                <span className="hidden md:inline">Tags</span>
                            </button>
                            {igUrl && (
                                <a
                                    href={igUrl}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    aria-label={`Open ${t.name || "talent"}'s Instagram profile`}
                                    title="Open Instagram"
                                    data-testid={`talent-instagram-btn-${t.id}`}
                                    className="inline-flex items-center justify-center border border-black/[0.08] hover:border-black/30 hover:bg-black/[0.02] bg-white text-black/60 hover:text-black w-11 h-11 md:w-8 md:h-8 rounded-lg transition-colors select-none min-h-[44px] md:min-h-0 shrink-0"
                                >
                                    <Instagram className="w-4.5 h-4.5 md:w-3.5 md:h-3.5" />
                                </a>
                            )}
                            <Link
                                to={`/admin/talents/${t.id}`}
                                className="inline-flex items-center justify-center border border-black/[0.08] hover:border-black/30 bg-white text-black text-[11px] font-medium px-2.5 py-1.5 rounded-lg transition-colors select-none min-h-[44px] shrink-0"
                                title="View Talent Profile"
                            >
                                View
                            </Link>
                            <Link
                                to={`/admin/talents/${t.id}`}
                                className="inline-flex items-center justify-center border border-black/[0.08] hover:border-black/30 bg-white text-black text-[11px] font-medium px-2.5 py-1.5 rounded-lg transition-colors select-none min-h-[44px] shrink-0"
                                title="Edit Talent Profile"
                            >
                                Edit
                            </Link>
                            {t.phone && (
                                <button
                                    type="button"
                                    onClick={handleWhatsApp}
                                    className="inline-flex items-center justify-center border border-emerald-500/20 hover:border-emerald-500 bg-emerald-50 text-emerald-700 text-[11px] font-semibold px-2.5 py-1.5 rounded-lg transition-colors select-none min-h-[44px] shrink-0"
                                    title="WhatsApp Connection"
                                >
                                    WhatsApp
                                </button>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    );
});

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function TalentList() {
    const {
        search: q,
        setSearch: setQ,
        filters,
        setFilter,
        removeFilter,
        clearAllFilters,
        activeFilterCount,
        filtersActive,
        sortBy,
        setSortBy,
        page,
        setPage,
        pageSize,
        total: totalItems,
        pages: totalPages,
        talents: fetchedTalents,
        loading,
        refetch,
    } = useTalentDirectory({ pageSize: 40 });
    // Local mirror of fetched talents — kept in sync via the effect below,
    // but still a plain useState so the existing optimistic tag-update
    // helpers (handleSaveTagsOptimistic etc.) can keep mutating it in place
    // exactly as before, without waiting on a full server round-trip.
    const [talents, setTalents] = useState([]);
    useEffect(() => { setTalents(fetchedTalents); }, [fetchedTalents]);

    const [availableTags, setAvailableTags] = useState([]);
    useEffect(() => {
        adminApi.get("/tags").then(({ data }) => setAvailableTags(data?.tags || [])).catch(() => {});
    }, []);

    const [availableLocations, setAvailableLocations] = useState([]);
    useEffect(() => {
        adminApi.get("/talents/facets").then(({ data }) => setAvailableLocations(data?.locations || [])).catch(() => {});
    }, []);

    const [showDesktopFilters, setShowDesktopFilters] = useState(false);

    const [selected, setSelected] = useState(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const canBulkDelete = isAdmin();

    const searchInputRef = useRef(null);

    // Keyboard shortcut to focus search input
    useEffect(() => {
        const handleKeyDown = (e) => {
            const isTyping = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
            if (
                ((e.metaKey || e.ctrlKey) && e.key?.toLowerCase() === "k") ||
                (e.key === "/" && !isTyping)
            ) {
                e.preventDefault();
                searchInputRef.current?.focus();
                searchInputRef.current?.select();
            }
        };
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, []);

    // Inline tagging state
    const [tagPopoverTalent, setTagPopoverTalent] = useState(null);
    const [bulkTagAction, setBulkTagAction] = useState(null); // 'assign' | 'remove' | null
    const [showAddToProject, setShowAddToProject] = useState(false);

    const [viewMode, setViewMode] = useState(() => {
        try {
            return localStorage.getItem("tg_talents_view") || "grid";
        } catch {
            return "grid";
        }
    });

    useEffect(() => {
        try {
            localStorage.setItem("tg_talents_view", viewMode);
        } catch (e) {
            console.error(e);
        }
    }, [viewMode]);

    // Data fetching (search/filter/sort/pagination) is entirely owned by
    // useTalentDirectory now — see the hook call above. Re-fetch when
    // navigating back from a talent's detail page (e.g. after an edit).
    useEffect(() => {
        const onVisible = () => {
            if (document.visibilityState === "visible") refetch();
        };
        document.addEventListener("visibilitychange", onVisible);
        return () => document.removeEventListener("visibilitychange", onVisible);
    }, [refetch]);

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

    // ── Optimistic state updates ─────────────────────────────────────────────
    const handleSaveTagsOptimistic = useCallback((talentId, updatedTags) => {
        setTalents(prev => prev.map(t => t.id === talentId ? { ...t, tags: updatedTags } : t));
    }, []);

    const handleBulkSaveTagsOptimistic = useCallback((tag, actionType) => {
        const ids = Array.from(selected);
        setTalents(prev => prev.map(t => {
            if (ids.includes(t.id)) {
                const existing = t.tags || [];
                let nextTags = [...existing];
                if (actionType === "assign") {
                    if (!existing.some(tg => tg.id === tag.id)) {
                        nextTags.push({ id: tag.id, name: tag.name });
                    }
                } else {
                    nextTags = nextTags.filter(tg => tg.id !== tag.id);
                }
                return { ...t, tags: nextTags };
            }
            return t;
        }));
        clear();
    }, [selected, clear]);

    // ── Bulk delete ──────────────────────────────────────────────────────────
    const bulkDelete = useCallback(async () => {
        const ids = Array.from(selected);
        try {
            const res = await adminApi.post("/talents/bulk-delete", { ids });
            ids.forEach(id => talentPreviewCache.invalidateTalent(id));
            toast.success(
                `Deleted ${res.data.deleted} talent${res.data.deleted === 1 ? "" : "s"}${
                    res.data.missing ? ` (${res.data.missing} already gone)` : ""
                }`
            );
            clear();
            setConfirmOpen(false);
            refetch();
        } catch (err) {
            toast.error(
                err?.response?.data?.detail ?? err?.message ?? "Bulk delete failed"
            );
            throw err;
        }
    }, [selected, clear, refetch]);

    // ── Export ───────────────────────────────────────────────────────────────
    const handleExport = useCallback(() => {
        const selectedTalents = talents.filter(t => selected.has(t.id));
        if (selectedTalents.length === 0) return;
        
        // Define CSV Headers
        const headers = ["Name", "Email", "Phone", "Location", "Gender", "Age", "Height", "Instagram Handle", "Followers", "Tags"];
        const rows = selectedTalents.map(t => [
            t.name || "",
            t.email || "",
            t.phone || "",
            formatTalentLocation(t.location) || "",
            t.gender || "",
            t.age || "",
            t.height || "",
            t.instagram_handle || "",
            t.instagram_followers || "",
            (t.tags || []).map(tg => tg.name).join("; ")
        ]);
        
        // Build CSV Content
        const csvContent = "data:text/csv;charset=utf-8," 
            + [headers.join(","), ...rows.map(e => e.map(val => `"${String(val).replace(/"/g, '""')}"`).join(","))].join("\n");
        
        const encodedUri = encodeURI(csvContent);
        const link = document.createElement("a");
        link.setAttribute("href", encodedUri);
        link.setAttribute("download", `talents_export_${new Date().toISOString().split('T')[0]}.csv`);
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        
        toast.success(`Exported ${selectedTalents.length} talents successfully.`);
        clear();
    }, [talents, selected, clear]);

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
                        <p className="text-[11px] text-neutral-500 mt-2 tracking-wide uppercase font-semibold">
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

            {/* Search & View Toggle Container */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
                <div className="relative max-w-md w-full">
                    <Search className="absolute left-0 top-3 w-4 h-4 text-black/30 pointer-events-none" />
                    <input
                        ref={searchInputRef}
                        type="text"
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                        placeholder="Search name, Instagram, phone, email, tag, or talent ID…"
                        data-testid="talent-search-input"
                        autoComplete="off"
                        spellCheck={false}
                        className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-3 pl-7 pr-12 text-sm text-black/85 placeholder:text-black/28 transition-colors duration-150"
                    />
                    <div className="absolute right-0 top-3 flex items-center gap-0.5 pointer-events-none select-none px-1.5 py-0.5 rounded bg-black/[0.04] border border-black/[0.06] text-[9px] font-medium text-black/40 tracking-wide">
                        <span>⌘</span>
                        <span>K</span>
                    </div>
                </div>
                {/* View Toggle Buttons */}
                <div className="flex items-center bg-black/[0.03] border border-black/[0.06] rounded-lg p-0.5 shrink-0 self-start sm:self-auto" data-testid="view-toggle">
                    <button
                        type="button"
                        onClick={() => setViewMode("grid")}
                        className={`flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium rounded-md transition-all min-h-[44px] min-w-[44px] justify-center select-none ${
                            viewMode === "grid"
                                ? "bg-white text-black shadow-sm border border-black/[0.06]"
                                : "text-black/55 hover:text-black/80"
                        }`}
                        title="Grid View"
                    >
                        <LayoutGrid size={13} />
                        <span>Grid</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => setViewMode("list")}
                        className={`flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium rounded-md transition-all min-h-[44px] min-w-[44px] justify-center select-none ${
                            viewMode === "list"
                                ? "bg-white text-black shadow-sm border border-black/[0.06]"
                                : "text-black/55 hover:text-black/80"
                        }`}
                        title="List View"
                    >
                        <List size={13} />
                        <span>List</span>
                    </button>
                </div>
            </div>

            {/* Filter / Sort toolbar — desktop gets an inline toggleable panel
                (mirrors Browse Roster's "Show/Hide Filters"), mobile gets the
                compact button + bottom sheet. Both drive the same
                useTalentDirectory filter state, so results always match. */}
            <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        data-testid="desktop-filter-toggle"
                        onClick={() => setShowDesktopFilters((v) => !v)}
                        className={`hidden md:flex items-center gap-2 px-3 py-2 text-sm border rounded-lg transition-colors ${
                            showDesktopFilters ? "border-black/30 bg-black/[0.02]" : "border-gray-200 bg-white hover:border-gray-300"
                        }`}
                    >
                        <SlidersHorizontal className="w-4 h-4 text-[#333333]" />
                        Filters
                        {activeFilterCount > 0 && (
                            <span className="min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full bg-[#0c2340] text-white text-[10px] font-semibold">
                                {activeFilterCount}
                            </span>
                        )}
                    </button>
                    <div className="md:hidden">
                        <MobileFilterSheet
                            filters={filters}
                            setFilter={setFilter}
                            clearAllFilters={clearAllFilters}
                            activeFilterCount={activeFilterCount}
                            availableTags={availableTags}
                            availableLocations={availableLocations}
                        />
                    </div>
                </div>
                <SortDropdown value={sortBy} onChange={setSortBy} />
            </div>

            {showDesktopFilters && (
                <div className="hidden md:block border border-gray-200 bg-gray-50/50 rounded-xl p-5 mb-4">
                    <FilterPanel filters={filters} setFilter={setFilter} availableTags={availableTags} availableLocations={availableLocations} />
                </div>
            )}

            {filtersActive && (
                <div className="mb-6">
                    <FilterChips
                        filters={filters}
                        setFilter={setFilter}
                        removeFilter={removeFilter}
                        clearAllFilters={clearAllFilters}
                        activeFilterCount={activeFilterCount}
                        availableTags={availableTags}
                    />
                </div>
            )}

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
                        {q
                            ? `No results for "${q}"`
                            : filtersActive
                                ? "No talents match these filters"
                                : "No talents yet"}
                    </p>
                    {filtersActive && !q && (
                        <button
                            type="button"
                            onClick={clearAllFilters}
                            className="text-xs font-medium text-[#333333] hover:text-[#111111] underline underline-offset-2"
                        >
                            Clear all filters
                        </button>
                    )}
                    {!q && !filtersActive && (
                        <Link
                            to="/admin/talents/new"
                            className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/85 transition-colors duration-150 mt-4"
                        >
                            Add your first talent
                        </Link>
                    )}
                </div>
            ) : viewMode === "list" ? (
                <div
                    className="flex flex-col gap-3 pb-20 animate-fade-in"
                    data-testid="talents-list"
                >
                    {talents.map((t) => (
                        <TalentListRow
                            key={t.id}
                            t={t}
                            checked={selected.has(t.id)}
                            isSelectionMode={isSelectionMode}
                            canBulkDelete={canBulkDelete}
                            onToggle={toggle}
                            onTagClick={setTagPopoverTalent}
                        />
                    ))}
                </div>
            ) : (
                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-5 pb-20 animate-fade-in"
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
                            onTagClick={setTagPopoverTalent}
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
                    grandTotal={totalItems}
                    allSelected={selected.size === talents.length}
                    onSelectAll={selectAll}
                    onClear={clear}
                    onDelete={() => setConfirmOpen(true)}
                    onAssignTags={() => setBulkTagAction("assign")}
                    onRemoveTags={() => setBulkTagAction("remove")}
                    onExport={handleExport}
                    onAddToProject={() => setShowAddToProject(true)}
                    labelSingular="talent"
                    labelPlural="talents"
                    testid="talents-bulk-bar"
                />
            )}

            {showAddToProject && (
                <AddToProjectModal
                    open={showAddToProject}
                    talentIds={Array.from(selected)}
                    onClose={() => setShowAddToProject(false)}
                    onSuccess={clear}
                />
            )}

            {/* Individual Inline Tag Popover */}
            {tagPopoverTalent && (
                <TagPopover
                    talent={tagPopoverTalent}
                    onSave={handleSaveTagsOptimistic}
                    onClose={() => setTagPopoverTalent(null)}
                />
            )}

            {/* Bulk Tag Dialog */}
            {bulkTagAction && (
                <BulkTagDialog
                    selectedCount={selected.size}
                    selectedIds={Array.from(selected)}
                    actionType={bulkTagAction}
                    onSave={handleBulkSaveTagsOptimistic}
                    onClose={() => setBulkTagAction(null)}
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
