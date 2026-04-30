import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import { viewerApi, COVER_URL, IMAGE_URL, VIDEO_URL, VIDEO_POSTER_URL, getViewerToken, saveViewerToken } from "@/lib/api";
import ThemeToggle from "@/components/ThemeToggle";
import Logo from "@/components/Logo";
import FeedbackComposer from "@/components/FeedbackComposer";
import axios from "axios";
import { toast } from "sonner";
import {
    Instagram,
    ExternalLink,
    Star,
    ThumbsUp,
    XCircle,
    HelpCircle,
    ChevronLeft,
    ChevronRight,
    X,
    Download,
    Play,
    Sparkles,
    Loader2,
    MessageSquare,
    Eye,
    Heart,
    Layers,
    Clock,
} from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

/**
 * Client-facing privacy helper — collapses the talent's full name to
 * "First L." so casting clients never see the full last name.
 *
 *   "Ayushi Thakur"     → "Ayushi T"
 *   "  Riya  Singh   "  → "Riya S"
 *   "Madonna"           → "Madonna"   (single name passes through)
 *   ""                  → "Unnamed"
 */
function privatizeName(raw) {
    const s = (raw || "").trim();
    if (!s) return "Unnamed";
    const parts = s.split(/\s+/);
    if (parts.length === 1) return parts[0];
    const first = parts[0];
    const lastInitial = parts[parts.length - 1].charAt(0).toUpperCase();
    return `${first} ${lastInitial}`;
}

/**
 * Map the talent's availability response to one of three labels the client
 * sees. A note on a "yes" or "no" response upgrades the label to
 * "Conditional" because the talent has qualified the answer.
 */
function availabilityLabel(av) {
    if (!av || !av.status) return null;
    const status = av.status;
    const note = (av.note || "").trim();
    if (status === "yes") return note ? "Conditional" : "Available";
    if (status === "no") return note ? "Conditional" : "Not Available";
    return null;
}

const ACTIONS = [
    { key: "shortlist", label: "Shortlist", icon: Star, color: "#FFCC00" },
    { key: "interested", label: "Interested", icon: ThumbsUp, color: "#34C759" },
    { key: "not_for_this", label: "Not for this", icon: XCircle, color: "#FF3B30" },
    { key: "not_sure", label: "Not sure", icon: HelpCircle, color: "#9CA3AF" },
];

// Tabs for the Client Viewing Intelligence System.
// `All` is default; `New` filters subjects added after viewer's previous visit.
const TABS = [
    { key: "all", label: "All", icon: Layers },
    { key: "pending", label: "Pending", icon: Clock },
    { key: "seen", label: "Seen", icon: Eye },
    { key: "shortlisted", label: "Shortlisted", icon: Heart },
    { key: "new", label: "New", icon: Sparkles },
];

export default function ClientView() {
    const { slug } = useParams();
    const [identified, setIdentified] = useState(!!getViewerToken(slug));
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);

    const [data, setData] = useState(null);
    const [activeTalent, setActiveTalent] = useState(null);
    const [commentDrafts, setCommentDrafts] = useState({});
    // Client viewing-intelligence state. `seenIds` is local-augmented from
    // server `client_state.seen_talent_ids`; `activeTab` toggles between
    // All / Pending / Seen / Shortlisted / New buckets.
    const [seenIds, setSeenIds] = useState(new Set());
    const [activeTab, setActiveTab] = useState("all");

    const loadData = useCallback(async () => {
        try {
            const { data } = await axios.get(`${API}/public/links/${slug}`, {
                headers: {
                    Authorization: `Bearer ${getViewerToken(slug)}`,
                },
            });
            setData(data);
            setSeenIds(new Set(data?.client_state?.seen_talent_ids || []));
        } catch (e) {
            if (e?.response?.status === 401) {
                setIdentified(false);
            } else {
                toast.error("Failed to load portfolio");
            }
        }
    }, [slug]);

    useEffect(() => {
        if (identified) loadData();
    }, [identified, loadData]);

    // Branded page title — replaces the raw slug-based title users used to
    // see in the browser tab. Shape: "Talentgram | <Project Name>" using
    // brand_name when set, else the link title.
    useEffect(() => {
        const prev = document.title;
        const brand = (data?.link?.brand_name || data?.link?.title || "").trim();
        document.title = brand ? `Talentgram | ${brand}` : "Talentgram | Portfolio";
        return () => {
            document.title = prev;
        };
    }, [data?.link?.brand_name, data?.link?.title]);

    const identify = async (e) => {
        e.preventDefault();
        setLoading(true);
        try {
            const { data } = await axios.post(
                `${API}/public/links/${slug}/identify`,
                { name, email },
            );
            saveViewerToken(slug, data.token);
            setIdentified(true);
            toast.success(`Welcome, ${name.split(" ")[0]}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to continue");
        } finally {
            setLoading(false);
        }
    };

    const viewerActions = useMemo(() => {
        const m = {};
        (data?.actions || []).forEach((a) => (m[a.talent_id] = a));
        return m;
    }, [data]);

    const setAction = async (talentId, action) => {
        try {
            await axios.post(
                `${API}/public/links/${slug}/action`,
                { talent_id: talentId, action },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            await loadData();
        } catch {
            toast.error("Action failed");
        }
    };

    const saveComment = async (talentId) => {
        const text = commentDrafts[talentId];
        if (text === undefined) return;
        try {
            const existing = viewerActions[talentId];
            await axios.post(
                `${API}/public/links/${slug}/action`,
                {
                    talent_id: talentId,
                    action: existing?.action || null,
                    comment: text,
                },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            toast.success("Comment saved");
            await loadData();
        } catch {
            toast.error("Failed to save");
        }
    };

    const logDownload = async (talentId, mediaId) => {
        try {
            await axios.post(
                `${API}/public/links/${slug}/download-log`,
                { talent_id: talentId, media_id: mediaId },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
        } catch (e) { console.error(e); }
    };

    // Mark a subject as "seen" — fires from IntersectionObserver (5s in
    // viewport) AND from opening the detail overlay. Optimistic locally,
    // best-effort on server (silent failure is fine).
    const markSeen = useCallback(
        async (talentId) => {
            if (!talentId) return;
            setSeenIds((prev) => {
                if (prev.has(talentId)) return prev;
                const n = new Set(prev);
                n.add(talentId);
                return n;
            });
            try {
                await axios.post(
                    `${API}/public/links/${slug}/seen`,
                    { talent_id: talentId },
                    {
                        headers: {
                            Authorization: `Bearer ${getViewerToken(slug)}`,
                        },
                    },
                );
            } catch (e) {
                console.error(e);
                // Silent — local state still tracks visually for this session
            }
        },
        [slug],
    );

    // ---------------- Identity Gate ----------------
    if (!identified) {
        return (
            <div className="min-h-screen bg-[#050505] relative">
                <div className="absolute top-5 right-5 z-20">
                    <ThemeToggle />
                </div>
                <div
                    className="absolute inset-0 opacity-30"
                    style={{
                        backgroundImage:
                            "url('https://images.pexels.com/photos/15128321/pexels-photo-15128321.jpeg')",
                        backgroundSize: "cover",
                        backgroundPosition: "center",
                    }}
                />
                <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-black/80 to-black" />
                <div className="relative z-10 min-h-screen flex items-center justify-center px-6">
                    <form
                        onSubmit={identify}
                        className="w-full max-w-md bg-black/60 backdrop-blur-xl border border-white/10 p-8 md:p-10 tg-fade-up"
                        data-testid="identity-gate-form"
                    >
                        <div className="flex justify-center mb-8">
                            <Logo size="md" />
                        </div>
                        <p className="eyebrow mb-3">Curated Portfolio</p>
                        <h1 className="font-display text-3xl tracking-tight mb-3">
                            A private review awaits you.
                        </h1>
                        <p className="text-white/50 text-sm mb-8">
                            Please share your name and email to continue. This
                            helps us follow up on your selections.
                        </p>
                        <label className="block mb-4">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Your Name
                            </span>
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                required
                                data-testid="identity-name-input"
                                className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>
                        <label className="block mb-8">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Email
                            </span>
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                required
                                data-testid="identity-email-input"
                                className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>
                        <button
                            type="submit"
                            disabled={loading}
                            data-testid="identity-submit-btn"
                            className="w-full bg-white text-black py-3.5 rounded-sm text-sm font-medium hover:opacity-90 inline-flex items-center justify-center gap-2"
                        >
                            {loading && (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            )}
                            Enter Review
                        </button>
                    </form>
                </div>
            </div>
        );
    }

    if (!data) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] text-white/40">
                <Loader2 className="w-6 h-6 animate-spin" />
            </div>
        );
    }

    const { link, talents, viewer } = data;
    const vis = link.visibility || {};
    const projectBudget = data.project_budget || [];
    const projectShootDates = data.project_shoot_dates || [];
    const subjectAddedAt = data.subject_added_at || {};
    const prevVisitAt = data?.client_state?.prev_visit_at || null;

    // Compute per-tab membership ONCE so chips show live counts.
    const isShortlisted = (id) => viewerActions[id]?.action === "shortlist";
    const isNew = (id) => {
        if (!prevVisitAt) return false; // first-ever visit → nothing is "new"
        const t = subjectAddedAt[id];
        if (!t) return false;
        return new Date(t).getTime() > new Date(prevVisitAt).getTime();
    };
    const buckets = {
        all: talents,
        pending: talents.filter((t) => !seenIds.has(t.id)),
        seen: talents.filter((t) => seenIds.has(t.id)),
        shortlisted: talents.filter((t) => isShortlisted(t.id)),
        new: talents.filter((t) => isNew(t.id)),
    };
    const filteredTalents = buckets[activeTab] || talents;
    const seenCount = buckets.seen.length;
    const totalCount = talents.length;
    const reviewedPct = totalCount === 0 ? 0 : Math.round((seenCount / totalCount) * 100);

    return (
        <div className="min-h-screen bg-[#050505] text-white" data-testid="client-view-page">
            {/* Header — on mobile we collapse the page heading + progress
                bar into the sticky chrome to maximise above-the-fold cards.
                Desktop keeps the verbose layout below. */}
            <header className="sticky top-0 z-30 bg-black/85 backdrop-blur-xl border-b border-white/10">
                <div className="max-w-[1600px] mx-auto px-4 md:px-12 py-3 md:py-5">
                    <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0 flex-1">
                            <p className="eyebrow hidden md:block">Curated Review</p>
                            <h1 className="font-display text-base md:text-2xl tracking-tight mt-0 md:mt-1 truncate">
                                {link.title}
                            </h1>
                            <p className="text-[10px] md:hidden text-white/50 tg-mono mt-0.5 truncate">
                                {viewer.name} · {seenCount}/{totalCount} reviewed
                            </p>
                        </div>
                        <div className="hidden md:block text-right">
                            <p className="text-xs text-white/50">Viewing as</p>
                            <p className="text-sm font-medium">{viewer.name}</p>
                        </div>
                        <ThemeToggle />
                    </div>
                    {/* Mobile-only sticky progress bar — replaces the verbose
                        below-fold card so cards are visible immediately. */}
                    <div className="md:hidden mt-2 h-1 bg-white/10 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-white transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar-mobile"
                        />
                    </div>
                </div>
            </header>

            {/* Grid */}
            <div className="max-w-[1600px] mx-auto px-4 md:px-12 py-5 md:py-16">
                {/* Verbose page heading — desktop only; mobile has it in the header */}
                <div className="hidden md:flex mb-6 items-center justify-between flex-wrap gap-3">
                    <div>
                        <p className="eyebrow mb-2">{talents.length} Talents</p>
                        <h2 className="font-display text-3xl md:text-5xl tracking-tight">
                            Pick your winners.
                        </h2>
                    </div>
                    <p className="text-xs text-white/40 max-w-sm">
                        Tap any card to view the full portfolio. Actions and
                        comments are saved instantly.
                    </p>
                </div>

                {/* Desktop progress bar (mobile uses sticky variant in header) */}
                <div
                    className="mb-6 hidden md:flex items-center gap-4"
                    data-testid="review-progress"
                >
                    <div className="flex-1 h-1.5 bg-white/10 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-white transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar"
                        />
                    </div>
                    <div
                        className="text-[11px] tg-mono text-white/60 shrink-0"
                        data-testid="review-progress-label"
                    >
                        {seenCount} of {totalCount} reviewed
                    </div>
                </div>

                {/* Tabs — Client Viewing Intelligence. Mobile = horizontal scroll
                    so tabs never wrap to a second row eating vertical space. */}
                <div
                    className="mb-5 md:mb-8 -mx-4 md:mx-0 px-4 md:px-0 flex items-center gap-2 overflow-x-auto md:flex-wrap whitespace-nowrap border-b border-white/10 pb-3"
                    style={{ scrollbarWidth: "none" }}
                    data-testid="client-view-tabs"
                >
                    {TABS.map((tab) => {
                        const count = buckets[tab.key].length;
                        const active = activeTab === tab.key;
                        return (
                            <button
                                key={tab.key}
                                type="button"
                                onClick={() => setActiveTab(tab.key)}
                                data-testid={`client-tab-${tab.key}`}
                                className={`inline-flex items-center gap-1.5 px-3 md:px-3.5 py-2 rounded-sm text-[11px] tracking-widest uppercase transition-all border shrink-0 active:scale-[0.97] ${
                                    active
                                        ? "bg-white text-black border-white"
                                        : "border-white/15 text-white/60 hover:text-white hover:border-white/40"
                                }`}
                            >
                                <tab.icon className="w-3.5 h-3.5" />
                                {tab.label}
                                <span
                                    className={`tg-mono text-[10px] ${active ? "text-black/60" : "text-white/40"}`}
                                >
                                    {count}
                                </span>
                            </button>
                        );
                    })}
                </div>

                {projectBudget.length > 0 && (
                    <section
                        className="mb-10 border border-white/10 bg-white/[0.02] p-5 md:p-6"
                        data-testid="project-budget-block"
                    >
                        <p className="eyebrow mb-4">Project Budget</p>
                        <div className="grid gap-6 md:grid-cols-2">
                            {projectBudget.map((grp, gi) => (
                                <div
                                    key={grp.project_id || `grp-${gi}`}
                                    className="space-y-2"
                                    data-testid={`project-budget-group-${gi}`}
                                >
                                    {grp.brand_name && projectBudget.length > 1 && (
                                        <div className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                                            {grp.brand_name}
                                        </div>
                                    )}
                                    {(grp.lines || []).map((row, ri) => (
                                        <div
                                            key={`${row.label || ""}-${ri}`}
                                            className="flex items-center justify-between gap-3 border-b border-white/5 pb-2 text-sm"
                                            data-testid={`project-budget-line-${gi}-${ri}`}
                                        >
                                            <span className="text-white/70">
                                                {row.label || "—"}
                                            </span>
                                            <span className="tg-mono text-white/95">
                                                {row.value || "—"}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            ))}
                        </div>
                    </section>
                )}

                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6"
                    data-testid="client-talents-grid"
                >
                    {filteredTalents.length === 0 ? (
                        <div
                            className="col-span-full text-center py-16 text-white/40 text-sm"
                            data-testid="client-tab-empty"
                        >
                            {activeTab === "new" && "Nothing new since your last visit."}
                            {activeTab === "shortlisted" &&
                                "No shortlists yet — open a card and tap Shortlist to add one."}
                            {activeTab === "seen" && "You haven't reviewed any talents yet."}
                            {activeTab === "pending" &&
                                "You've reviewed everyone — nice work."}
                            {activeTab === "all" && "No talents on this link."}
                        </div>
                    ) : (
                        filteredTalents.map((t, i) => (
                            <TalentCard
                                key={t.id}
                                talent={t}
                                index={i}
                                vis={vis}
                                action={viewerActions[t.id]?.action}
                                seen={seenIds.has(t.id)}
                                isNew={isNew(t.id)}
                                onOpen={() => {
                                    setActiveTalent(t);
                                    markSeen(t.id);
                                }}
                                onSeen={() => markSeen(t.id)}
                            />
                        ))
                    )}
                </div>
            </div>

            {/* Detail Overlay */}
            {activeTalent && (
                <TalentDetail
                    talent={activeTalent}
                    talents={filteredTalents}
                    link={link}
                    slug={slug}
                    projectBudget={projectBudget}
                    projectShootDates={projectShootDates}
                    viewerAction={viewerActions[activeTalent.id]}
                    onClose={() => setActiveTalent(null)}
                    onNavigate={(t) => {
                        setActiveTalent(t);
                        markSeen(t.id);
                    }}
                    setAction={setAction}
                    commentDraft={
                        commentDrafts[activeTalent.id] ??
                        viewerActions[activeTalent.id]?.comment ??
                        ""
                    }
                    setCommentDraft={(text) =>
                        setCommentDrafts({
                            ...commentDrafts,
                            [activeTalent.id]: text,
                        })
                    }
                    saveComment={() => saveComment(activeTalent.id)}
                    logDownload={logDownload}
                />
            )}
        </div>
    );
}

function TalentDetail({
    talent,
    talents,
    link,
    slug,
    projectBudget = [],
    projectShootDates = [],
    viewerAction,
    onClose,
    onNavigate,
    setAction,
    commentDraft,
    setCommentDraft,
    saveComment,
    logDownload,
}) {
    const vis = link.visibility || {};
    // Split media by explicit category. Backend normalises all takes (new +
    // legacy) to category="take" + label, preserves intro as "video" and
    // images as "portfolio" / "indian" / "western". Order is also enforced
    // backend-side, but we pick buckets here for independent section
    // rendering.
    const mediaAll = talent.media || [];
    // Phase 3 v37j — granular per-look visibility. The umbrella `portfolio`
    // toggle is the master gate (when OFF, no look images render at all).
    // When `portfolio` is ON, each look bucket is gated by its own toggle:
    //   - portfolio (generic)  → vis.portfolio_images (default ON when key absent)
    //   - indian               → vis.indian_images   (default ON when key absent)
    //   - western              → vis.western_images  (default ON when key absent)
    // `?? true` keeps backward-compat for older links that don't have the
    // new keys yet.
    const portfolioOn = vis.portfolio !== false;
    const indianOn = portfolioOn && (vis.indian_images ?? true);
    const westernOn = portfolioOn && (vis.western_images ?? true);
    const portfolioGenericOn = portfolioOn; // existing behaviour kept identical
    const portfolioImages = portfolioGenericOn
        ? mediaAll.filter((m) => m.category === "portfolio")
        : [];
    const indianImages = indianOn
        ? mediaAll.filter((m) => m.category === "indian")
        : [];
    const westernImages = westernOn
        ? mediaAll.filter((m) => m.category === "western")
        : [];
    // Combined view used by the lightbox carousel (preserves order: portfolio → indian → western).
    const images = [...portfolioImages, ...indianImages, ...westernImages];
    const intro = mediaAll.find((m) => m.category === "video") || null;
    const takes = mediaAll.filter((m) => m.category === "take");
    const [idx, setIdx] = useState(0);
    const [busyAction, setBusyAction] = useState(null);
    const overlayRef = useRef(null);

    const prev = () => setIdx((i) => (i - 1 + images.length) % images.length);
    const next = () => setIdx((i) => (i + 1) % images.length);

    // Talent navigation — driven by the parent's filtered list so swipe
    // respects the current tab (Pending / Shortlisted / etc.).
    const list = Array.isArray(talents) ? talents : [];
    const currentTalentIdx = list.findIndex((t) => t.id === talent.id);
    const hasPrevTalent = currentTalentIdx > 0;
    const hasNextTalent =
        currentTalentIdx >= 0 && currentTalentIdx < list.length - 1;
    const goPrevTalent = () => {
        if (hasPrevTalent && onNavigate) onNavigate(list[currentTalentIdx - 1]);
    };
    const goNextTalent = () => {
        if (hasNextTalent && onNavigate) {
            onNavigate(list[currentTalentIdx + 1]);
        } else {
            onClose();
        }
    };

    // Swipe gesture handler — distinguishes horizontal (talent navigation)
    // from vertical (close on swipe-down) intent. 60 px threshold avoids
    // accidental triggers during scroll.
    useEffect(() => {
        const node = overlayRef.current;
        if (!node) return;
        let startX = 0;
        let startY = 0;
        let movedX = 0;
        let movedY = 0;
        let active = false;
        const onTouchStart = (e) => {
            if (e.touches.length !== 1) return;
            // Skip if the touch starts on a horizontally-scrollable child
            // (e.g. take video carousel) so internal scrolling works.
            const el = e.target.closest('[data-stop-swipe="1"]');
            if (el) return;
            active = true;
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            movedX = 0;
            movedY = 0;
        };
        const onTouchMove = (e) => {
            if (!active) return;
            movedX = e.touches[0].clientX - startX;
            movedY = e.touches[0].clientY - startY;
        };
        const onTouchEnd = () => {
            if (!active) return;
            active = false;
            const ax = Math.abs(movedX);
            const ay = Math.abs(movedY);
            // Horizontal swipe wins iff distinctly horizontal AND ≥ 60 px.
            if (ax > 60 && ax > ay * 1.4) {
                if (movedX < 0) goNextTalent();
                else goPrevTalent();
                return;
            }
            // Downward swipe-to-close (must be near top of overlay).
            if (movedY > 110 && ay > ax * 1.4 && startY < 200) {
                onClose();
            }
        };
        node.addEventListener("touchstart", onTouchStart, { passive: true });
        node.addEventListener("touchmove", onTouchMove, { passive: true });
        node.addEventListener("touchend", onTouchEnd, { passive: true });
        return () => {
            node.removeEventListener("touchstart", onTouchStart);
            node.removeEventListener("touchmove", onTouchMove);
            node.removeEventListener("touchend", onTouchEnd);
        };
        // `goNextTalent` / `goPrevTalent` / `onClose` / `onNavigate` are
        // intentionally omitted from deps. They're closures recreated on
        // every render, but our re-registration triggers (`talent.id`,
        // `list.length`, `currentTalentIdx`) cover every case where the
        // closures' meaningful inputs change. Including them would
        // tear-down + re-attach the touch listeners on every parent
        // render, causing dropped gestures mid-swipe.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [talent.id, list.length, currentTalentIdx]);

    // Quick-decision: shortlist / reject / hold. Auto-advances to the next
    // talent after the action so casting reviews fly through the list.
    const quickAction = async (key) => {
        if (busyAction) return;
        setBusyAction(key);
        // Subtle haptic on supported devices (Android Chrome).
        try { navigator.vibrate?.(10); } catch (e) { console.error(e); }
        try {
            await setAction(talent.id, key);
            // Tiny delay so the action confirmation toast registers + the
            // overlay's transition feels natural.
            setTimeout(() => {
                if (hasNextTalent) goNextTalent();
                setBusyAction(null);
            }, 350);
        } catch {
            setBusyAction(null);
        }
    };

    const download = async (m) => {
        await logDownload(talent.id, m.id);
        const url = m.url;
        const a = document.createElement("a");
        a.href = url;
        a.download = m.original_filename || "file";
        a.target = "_blank";
        a.click();
    };

    return (
        <div
            ref={overlayRef}
            className="fixed inset-0 z-50 bg-black/95 backdrop-blur-2xl overflow-y-auto pb-[88px] md:pb-0"
            data-testid="talent-detail-overlay"
        >
            {/* Talent navigation pills (mobile only — desktop has arrow keys
                via image carousel; swipe is the primary mobile pattern). */}
            <div className="md:hidden fixed top-3 left-3 z-50 flex items-center gap-1 text-[10px] tg-mono">
                {currentTalentIdx >= 0 && (
                    <span className="px-2 py-1 bg-black/60 border border-white/15 rounded-sm text-white/70">
                        {currentTalentIdx + 1} / {list.length}
                    </span>
                )}
            </div>
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-50 w-11 h-11 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50 active:scale-95 transition-transform"
                data-testid="detail-close-btn"
            >
                <X className="w-4 h-4" />
            </button>

            <div className="max-w-[1400px] mx-auto px-6 md:px-12 py-12">
                <div className="grid lg:grid-cols-5 gap-8 lg:gap-12">
                    {/* Left column — strict display order: TAKES → INTRO → IMAGES */}
                    <div className="lg:col-span-3">
                        {/* AUDITION TAKES — FIRST PRIORITY per product spec */}
                        {vis.takes !== false && takes.length > 0 && (
                            <div
                                className="mb-8"
                                data-testid="client-takes-section"
                            >
                                <p className="eyebrow mb-3">Audition Takes</p>
                                <div className="grid md:grid-cols-2 gap-4">
                                    {takes.map((t, i) => (
                                        <div
                                            key={t.id}
                                            data-testid={`client-take-${i}`}
                                        >
                                            <p className="text-[11px] text-white/60 mb-2 tg-mono truncate">
                                                {t.label || `Take ${i + 1}`}
                                            </p>
                                            <video
                                                src={VIDEO_URL(t)}
                                                poster={VIDEO_POSTER_URL(t)}
                                                controls
                                                preload="metadata"
                                                className="w-full border border-white/10 bg-black rounded-sm"
                                            />
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* INTRODUCTION VIDEO */}
                        {vis.intro_video && intro && (
                            <div className="mb-8">
                                <p className="eyebrow mb-3">Introduction</p>
                                <video
                                    src={VIDEO_URL(intro)}
                                    poster={VIDEO_POSTER_URL(intro)}
                                    controls
                                    preload="metadata"
                                    className="w-full border border-white/10 bg-black rounded-sm"
                                    data-testid="client-intro-video"
                                />
                            </div>
                        )}

                        {/* PORTFOLIO IMAGES (slider) */}
                        {images.length > 0 && (
                            <p className="eyebrow mb-3">Portfolio</p>
                        )}
                        {images.length > 0 ? (
                            <div className="relative bg-[#0a0a0a] aspect-[3/4] border border-white/10 overflow-hidden">
                                <img
                                    src={IMAGE_URL(images[idx])}
                                    alt={privatizeName(talent.name)}
                                    className="w-full h-full object-contain"
                                />
                                {images.length > 1 && (
                                    <>
                                        <button
                                            onClick={prev}
                                            className="absolute left-2 top-1/2 -translate-y-1/2 w-10 h-10 bg-black/50 border border-white/20 hover:bg-black flex items-center justify-center"
                                        >
                                            <ChevronLeft className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={next}
                                            className="absolute right-2 top-1/2 -translate-y-1/2 w-10 h-10 bg-black/50 border border-white/20 hover:bg-black flex items-center justify-center"
                                        >
                                            <ChevronRight className="w-4 h-4" />
                                        </button>
                                        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 px-3 py-1 bg-black/60 border border-white/15 text-[10px] tg-mono">
                                            {idx + 1} / {images.length}
                                        </div>
                                    </>
                                )}
                                {vis.download && (
                                    <button
                                        onClick={() => download(images[idx])}
                                        className="absolute top-2 right-2 w-9 h-9 bg-black/60 border border-white/20 hover:bg-white hover:text-black flex items-center justify-center"
                                        data-testid="detail-download-btn"
                                    >
                                        <Download className="w-4 h-4" />
                                    </button>
                                )}
                            </div>
                        ) : (
                            <div className="aspect-[3/4] bg-[#0a0a0a] border border-white/10 flex items-center justify-center text-white/30">
                                No portfolio
                            </div>
                        )}

                        {/* Thumbs */}
                        {images.length > 1 && (
                            <div
                                className="mt-3 flex gap-2 overflow-x-auto tg-scroll pb-2"
                                data-stop-swipe="1"
                            >
                                {images.map((m, i) => (
                                    <button
                                        key={m.id}
                                        onClick={() => setIdx(i)}
                                        className={`shrink-0 w-16 h-20 border ${i === idx ? "border-white" : "border-white/10"}`}
                                    >
                                        <img
                                            src={IMAGE_URL(m)}
                                            alt=""
                                            className="w-full h-full object-cover"
                                        />
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Info */}
                    <div className="lg:col-span-2">
                        <p className="eyebrow mb-3">Talent</p>
                        <h2
                            className="font-display text-4xl md:text-5xl tracking-tight mb-6"
                            data-testid="client-talent-name"
                        >
                            {privatizeName(talent.name)}
                        </h2>

                        <div className="grid grid-cols-2 gap-y-5 mb-8 text-sm">
                            {vis.age && talent.age && (
                                <InfoRow label="Age" value={talent.age} />
                            )}
                            {vis.height && talent.height && (
                                <InfoRow label="Height" value={talent.height} />
                            )}
                            {vis.location && talent.location && (
                                <InfoRow label="Location" value={talent.location} />
                            )}
                            {vis.ethnicity && talent.ethnicity && (
                                <InfoRow label="Ethnicity" value={talent.ethnicity} />
                            )}
                            {vis.instagram_followers &&
                                talent.instagram_followers && (
                                    <InfoRow
                                        label="Followers"
                                        value={talent.instagram_followers}
                                    />
                                )}
                        </div>

                        {/* Availability & Budget & Competitive Brand — admin-controlled final values */}
                        {(() => {
                            // Resolve the project this talent submitted to (if any) so we can
                            // surface its shoot_dates + budget value alongside the talent's response.
                            const tProj =
                                (talent.project_id &&
                                    projectShootDates.find(
                                        (p) => p.project_id === talent.project_id,
                                    )) ||
                                projectShootDates[0] ||
                                null;
                            const tProjBudget =
                                (talent.project_id &&
                                    projectBudget.find(
                                        (p) => p.project_id === talent.project_id,
                                    )) ||
                                projectBudget[0] ||
                                null;
                            const showAvail =
                                vis.availability !== false &&
                                ((talent.availability && talent.availability.status) ||
                                    (tProj && tProj.shoot_dates));
                            // Budget visibility now only requires either the talent's
                            // response OR the project's published budget — we render
                            // "Budget: <value>" using whichever side is present.
                            const showBudget =
                                vis.budget &&
                                (talent.budget?.status ||
                                    (tProjBudget && (tProjBudget.lines || []).length));
                            if (
                                !showAvail &&
                                !showBudget &&
                                !talent.competitive_brand
                            )
                                return null;
                            return (
                                <div
                                    className="mb-8 border border-white/10 p-4 space-y-4"
                                    data-testid="client-details-section"
                                >
                                    {showAvail && (
                                        <div data-testid="client-availability">
                                            <p className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                                                Availability
                                            </p>
                                            {tProj?.shoot_dates && (
                                                <p
                                                    className="text-sm text-white/90 mb-1"
                                                    data-testid="client-shoot-dates"
                                                >
                                                    {tProj.shoot_dates}
                                                </p>
                                            )}
                                            {(() => {
                                                const lbl = availabilityLabel(
                                                    talent.availability,
                                                );
                                                if (!lbl) return null;
                                                const tone =
                                                    lbl === "Available"
                                                        ? "bg-[#34C759]/15 text-[#34C759]"
                                                        : lbl === "Not Available"
                                                          ? "bg-[#FF3B30]/15 text-[#FF3B30]"
                                                          : "bg-[#c9a961]/15 text-[#c9a961]";
                                                return (
                                                    <p className="text-sm">
                                                        <span className="text-white/40 mr-2 text-[10px] tg-mono uppercase">
                                                            Status
                                                        </span>
                                                        <span
                                                            className={`inline-block px-2 py-0.5 mr-2 text-[10px] tg-mono uppercase rounded-sm ${tone}`}
                                                            data-testid="client-availability-status"
                                                        >
                                                            {lbl}
                                                        </span>
                                                        {talent.availability
                                                            ?.note && (
                                                            <span className="text-white/70">
                                                                {
                                                                    talent.availability
                                                                        .note
                                                                }
                                                            </span>
                                                        )}
                                                    </p>
                                                );
                                            })()}
                                        </div>
                                    )}
                                    {showBudget && (
                                        <div data-testid="client-budget">
                                            <p className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                                                Budget
                                            </p>
                                            {(() => {
                                                // 1) Custom counter — show the talent's typed amount.
                                                if (
                                                    talent.budget?.status === "custom" &&
                                                    (talent.budget?.value || "").trim()
                                                ) {
                                                    return (
                                                        <p className="text-sm text-white/90">
                                                            {talent.budget.value}
                                                            <span className="ml-2 inline-block px-2 py-0.5 text-[10px] tg-mono uppercase rounded-sm bg-white/10 text-white/60">
                                                                Counter-offer
                                                            </span>
                                                        </p>
                                                    );
                                                }
                                                // 2) Talent agreed → show the project's published budget lines.
                                                const lines =
                                                    (tProjBudget?.lines || []).filter(
                                                        (l) =>
                                                            (l.label || "").trim() ||
                                                            (l.value || "").trim(),
                                                    );
                                                if (
                                                    talent.budget?.status === "accept" &&
                                                    lines.length
                                                ) {
                                                    return (
                                                        <ul className="text-sm text-white/90 space-y-0.5">
                                                            {lines.map((ln, i) => (
                                                                <li
                                                                    key={`${ln.label}-${ln.value}`}
                                                                    className="flex justify-between gap-4"
                                                                    data-testid={`client-budget-line-${i}`}
                                                                >
                                                                    <span className="text-white/70">
                                                                        {ln.label}
                                                                    </span>
                                                                    <span>{ln.value}</span>
                                                                </li>
                                                            ))}
                                                        </ul>
                                                    );
                                                }
                                                // 3) Talent agreed but no published lines → show
                                                //    the brand's headline budget if available.
                                                if (
                                                    talent.budget?.status === "accept" &&
                                                    !lines.length
                                                ) {
                                                    return (
                                                        <p className="text-sm">
                                                            <span className="inline-block px-2 py-0.5 text-[10px] tg-mono uppercase rounded-sm bg-[#34C759]/15 text-[#34C759]">
                                                                Agreed
                                                            </span>
                                                        </p>
                                                    );
                                                }
                                                // 4) No talent response, project lines only.
                                                if (lines.length) {
                                                    return (
                                                        <ul className="text-sm text-white/90 space-y-0.5">
                                                            {lines.map((ln) => (
                                                                <li
                                                                    key={`${ln.label}-${ln.value}`}
                                                                    className="flex justify-between gap-4"
                                                                >
                                                                    <span className="text-white/70">
                                                                        {ln.label}
                                                                    </span>
                                                                    <span>{ln.value}</span>
                                                                </li>
                                                            ))}
                                                        </ul>
                                                    );
                                                }
                                                return null;
                                            })()}
                                        </div>
                                    )}
                                    {talent.competitive_brand && (
                                        <div data-testid="client-competitive-brand">
                                            <p className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                                                Competitive Brand
                                            </p>
                                            <p className="text-sm text-white/90">
                                                {talent.competitive_brand}
                                            </p>
                                        </div>
                                    )}
                                </div>
                            );
                        })()}

                        {/* Custom questions & answers — admin-filtered per-question */}
                        {(talent.custom_answers || []).length > 0 && (
                            <div
                                className="mb-8 border border-white/10 p-4 space-y-3"
                                data-testid="client-custom-answers"
                            >
                                <p className="eyebrow">Additional Details</p>
                                {talent.custom_answers.map((qa, i) => (
                                    <div
                                        key={`${qa.question}-${i}`}
                                        data-testid={`custom-qa-${i}`}
                                    >
                                        <p className="text-[10px] tracking-widest uppercase text-white/40 mb-0.5">
                                            {qa.question}
                                        </p>
                                        <p className="text-sm text-white/90 whitespace-pre-wrap">
                                            {qa.answer}
                                        </p>
                                    </div>
                                ))}
                            </div>
                        )}

                        <div className="flex gap-3 mb-8 flex-wrap">
                            {vis.instagram && talent.instagram_handle && (
                                <a
                                    href={`https://instagram.com/${talent.instagram_handle.replace("@", "")}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    data-testid="client-instagram-link"
                                    className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/20 hover:border-white rounded-sm text-xs"
                                >
                                    <Instagram className="w-3.5 h-3.5 text-current" />{" "}
                                    {talent.instagram_handle}
                                </a>
                            )}
                        </div>

                        {vis.work_links &&
                            (talent.work_links || []).length > 0 && (
                                <div className="mb-8">
                                    <p className="eyebrow mb-3">Work</p>
                                    <div className="space-y-2">
                                        {talent.work_links.map((w) => (
                                            <a
                                                key={w}
                                                href={w}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="flex items-center gap-2 text-sm text-white/70 hover:text-white tg-mono truncate"
                                            >
                                                <ExternalLink className="w-3 h-3 shrink-0" />
                                                <span className="truncate">
                                                    {w}
                                                </span>
                                            </a>
                                        ))}
                                    </div>
                                </div>
                            )}

                        {/* Actions */}
                        <div className="border-t border-white/10 pt-6 mt-6">
                            <p className="eyebrow mb-4">Your Decision</p>
                            <div className="grid grid-cols-2 gap-2 mb-6">
                                {ACTIONS.map((a) => {
                                    const active = viewerAction?.action === a.key;
                                    return (
                                        <button
                                            key={a.key}
                                            onClick={() =>
                                                setAction(
                                                    talent.id,
                                                    active ? null : a.key,
                                                )
                                            }
                                            data-testid={`action-${a.key}-${talent.id}`}
                                            className={`flex items-center gap-2 px-4 py-3 border rounded-sm text-sm transition-all ${active ? "bg-white text-black border-white" : "border-white/15 hover:border-white/40"}`}
                                        >
                                            <a.icon
                                                className="w-4 h-4"
                                                style={{
                                                    color: active
                                                        ? "#000"
                                                        : a.color,
                                                }}
                                            />
                                            {a.label}
                                        </button>
                                    );
                                })}
                            </div>

                            <div>
                                <div className="flex items-center gap-2 mb-2">
                                    <MessageSquare className="w-3.5 h-3.5 text-white/60" />
                                    <p className="eyebrow">Comment</p>
                                </div>
                                <textarea
                                    value={commentDraft}
                                    onChange={(e) =>
                                        setCommentDraft(e.target.value)
                                    }
                                    rows={3}
                                    placeholder="Share any notes about this talent..."
                                    data-testid="detail-comment-input"
                                    className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none"
                                />
                                <button
                                    onClick={saveComment}
                                    data-testid="detail-save-comment-btn"
                                    className="mt-3 text-xs px-4 py-2 border border-white/20 hover:border-white rounded-sm"
                                >
                                    Save comment
                                </button>
                            </div>

                            {/* Moderated feedback relay — only available for
                                submission-backed cards (the submission_id +
                                project_id come from `_submission_to_client_shape`). */}
                            {talent.submission_id && talent.project_id && (
                                <FeedbackComposer
                                    slug={slug}
                                    token={getViewerToken(slug)}
                                    talent={talent}
                                    submission={{
                                        id: talent.submission_id,
                                        project_id: talent.project_id,
                                    }}
                                />
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {/* Mobile-only sticky bottom action bar — Shortlist / Hold / Reject.
                Auto-advances to the next talent after the action so casting
                reviews fly through the list one-thumb. Hidden on desktop where
                the in-card action grid is already thumb-reachable. */}
            <div
                className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-black/90 backdrop-blur-xl border-t border-white/10 px-3 py-3"
                data-testid="detail-bottom-bar"
                style={{ paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom))" }}
            >
                <div className="grid grid-cols-3 gap-2">
                    <button
                        type="button"
                        onClick={() => quickAction("shortlist")}
                        disabled={Boolean(busyAction)}
                        data-testid="quick-shortlist-btn"
                        className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-sm border text-[11px] tracking-widest uppercase active:scale-[0.97] transition-transform ${viewerAction?.action === "shortlist" ? "bg-[#FFCC00] text-black border-[#FFCC00]" : "border-white/20 text-white/85 hover:border-white"}`}
                    >
                        {busyAction === "shortlist" ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Star className={`w-4 h-4 ${viewerAction?.action === "shortlist" ? "fill-current" : ""}`} />
                        )}
                        Shortlist
                    </button>
                    <button
                        type="button"
                        onClick={() => quickAction("not_sure")}
                        disabled={Boolean(busyAction)}
                        data-testid="quick-hold-btn"
                        className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-sm border text-[11px] tracking-widest uppercase active:scale-[0.97] transition-transform ${viewerAction?.action === "not_sure" ? "bg-white/10 text-white border-white/60" : "border-white/20 text-white/70 hover:border-white"}`}
                    >
                        {busyAction === "not_sure" ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <HelpCircle className="w-4 h-4" />
                        )}
                        Hold
                    </button>
                    <button
                        type="button"
                        onClick={() => quickAction("not_for_this")}
                        disabled={Boolean(busyAction)}
                        data-testid="quick-reject-btn"
                        className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-sm border text-[11px] tracking-widest uppercase active:scale-[0.97] transition-transform ${viewerAction?.action === "not_for_this" ? "bg-[#FF3B30] text-white border-[#FF3B30]" : "border-white/20 text-white/70 hover:border-[#FF3B30] hover:text-[#FF3B30]"}`}
                    >
                        {busyAction === "not_for_this" ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <XCircle className="w-4 h-4" />
                        )}
                        Reject
                    </button>
                </div>
                {/* Tiny breadcrumb — clarifies "you're on N of M" + nav arrows. */}
                {list.length > 1 && (
                    <div className="flex items-center justify-between mt-2 text-[10px] tg-mono text-white/40">
                        <button
                            type="button"
                            onClick={goPrevTalent}
                            disabled={!hasPrevTalent}
                            data-testid="quick-prev-btn"
                            className="px-2 py-1 disabled:opacity-30 active:scale-[0.95]"
                            aria-label="Previous talent"
                        >
                            ← swipe right · prev
                        </button>
                        <span>{currentTalentIdx + 1} of {list.length}</span>
                        <button
                            type="button"
                            onClick={goNextTalent}
                            disabled={!hasNextTalent}
                            data-testid="quick-next-btn"
                            className="px-2 py-1 disabled:opacity-30 active:scale-[0.95]"
                            aria-label="Next talent"
                        >
                            next · swipe left →
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

function TalentCard({ talent, index, vis, action, seen, isNew, onOpen, onSeen }) {
    const ref = useRef(null);
    const timerRef = useRef(null);

    const coverUrl = COVER_URL(talent);
    const isShortlisted = action === "shortlist";

    useEffect(() => {
        if (seen || !ref.current) return;
        const node = ref.current;
        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
                        // 5s of >= 50% visibility → mark seen.
                        if (!timerRef.current) {
                            timerRef.current = setTimeout(() => {
                                onSeen();
                                timerRef.current = null;
                            }, 5000);
                        }
                    } else if (timerRef.current) {
                        clearTimeout(timerRef.current);
                        timerRef.current = null;
                    }
                });
            },
            { threshold: [0, 0.5, 1] },
        );
        observer.observe(node);
        return () => {
            if (timerRef.current) clearTimeout(timerRef.current);
            observer.disconnect();
        };
    }, [seen, onSeen]);

    return (
        <button
            ref={ref}
            onClick={onOpen}
            data-testid={`client-talent-${talent.id}`}
            data-seen={seen ? "true" : "false"}
            data-new={isNew ? "true" : "false"}
            style={{ animationDelay: `${index * 40}ms` }}
            className="group relative text-left tg-fade-up"
        >
            <div className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden border border-white/10 group-hover:border-white/30 transition-all relative">
                {coverUrl ? (
                    <img
                        src={coverUrl}
                        alt={privatizeName(talent.name)}
                        loading="lazy"
                        onError={(e) => { e.currentTarget.style.display = "none"; }}
                        className="w-full h-full object-cover group-hover:scale-[1.03] transition-all duration-700"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-white/20">
                        <Sparkles className="w-8 h-8" />
                    </div>
                )}
                {/* Seen overlay — subtle desaturation cue */}
                {seen && (
                    <div className="absolute inset-0 bg-black/35 pointer-events-none" />
                )}

                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black via-black/40 to-transparent p-4">
                    <div
                        className="font-display text-lg md:text-xl tracking-tight"
                        data-testid={`client-card-name-${talent.id}`}
                    >
                        {privatizeName(talent.name)}
                    </div>
                    <div className="text-[11px] text-white/50 tg-mono mt-1">
                        {vis.location && talent.location ? talent.location : ""}
                    </div>
                </div>

                {/* Status pills row (top-left) */}
                <div className="absolute top-2 left-2 flex flex-col gap-1.5 items-start">
                    {isNew && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[#c9a961] text-black text-[10px] tracking-widest uppercase rounded-sm"
                            data-testid={`badge-new-${talent.id}`}
                        >
                            <Sparkles className="w-3 h-3" />
                            New
                        </span>
                    )}
                    {isShortlisted && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[#FF3366] text-white text-[10px] tracking-widest uppercase rounded-sm"
                            data-testid={`badge-shortlisted-${talent.id}`}
                        >
                            <Heart className="w-3 h-3 fill-current" />
                            Shortlisted
                        </span>
                    )}
                    {action && action !== "shortlist" && (
                        <span className="inline-flex items-center gap-1 px-2 py-1 bg-white text-black text-[10px] tracking-widest uppercase rounded-sm">
                            {ACTIONS.find((a) => a.key === action)?.label}
                        </span>
                    )}
                </div>

                {/* Seen indicator (top-right) */}
                {seen && (
                    <span
                        className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 bg-black/70 border border-white/20 text-white/80 text-[10px] tracking-widest uppercase rounded-sm"
                        data-testid={`badge-seen-${talent.id}`}
                    >
                        <Eye className="w-3 h-3" />
                        Seen
                    </span>
                )}
            </div>
        </button>
    );
}

function InfoRow({ label, value }) {
    return (
        <div>
            <div className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                {label}
            </div>
            <div className="text-sm font-medium">{value}</div>
        </div>
    );
}
