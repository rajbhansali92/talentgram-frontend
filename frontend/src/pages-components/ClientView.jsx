'use client';

import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
import { useParams } from "next/navigation";
import { IMAGE_URL, getViewerToken, saveViewerToken, PUBLIC_FRONTEND_URL, API } from "@/lib/api";
import LazyVideoPlayer from "@/components/LazyVideoPlayer";
import { thumbnailUrl, posterUrl, resolveTalentCover, displayInstagramHandle, instagramProfileUrl } from "@/lib/mediaUtils";
import Logo from "@/components/Logo";
import WorkLinksDisplay, { parseStoredWorkLink } from "@/components/WorkLinksDisplay";
import { api as axios } from "@/lib/api";
import { formatTalentLocation } from "@/lib/sanitize";
import { toast } from "sonner";
import VoiceRecorder from "@/components/VoiceRecorder";
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
    Sparkles,
    Loader2,
    MessageSquare,
    Eye,
    Heart,
    Layers,
    Clock,
    Check,
    Share2,
    ArrowRight,
    ChevronDown,
    Lock,
    ClipboardCheck,
    Copy,
} from "lucide-react";

// API is imported from @/lib/api above — single source of truth across all pages.
// parseStoredWorkLink and WorkLinksDisplay are imported from @/components/WorkLinksDisplay.

/**
 * Client-facing privacy helper — collapses the talent's full name to
 * "First L." so casting clients never see the full last name.
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
    { key: "interested", label: "Audition Approved", icon: ThumbsUp },
    { key: "not_for_this", label: "Does Not Work For This Project", icon: XCircle },
    { key: "shortlist", label: "Shortlist", icon: Star },
    { key: "lock", label: "Lock", icon: Lock },
    { key: "not_sure", label: "Unsure", icon: HelpCircle },
    { key: "ask_for_test", label: "Ask for Test", icon: ClipboardCheck },
];

// Compact labels + icons for the mobile card status badge — keeps cards premium
// (e.g. "Does Not Work For This Project" → "Not for this"). Desktop cards keep
// the full ACTIONS labels.
const SHORT_ACTION_META = {
    interested: { label: "Approved", icon: ThumbsUp },
    not_for_this: { label: "Not for this", icon: XCircle },
    shortlist: { label: "Shortlisted", icon: Star },
    lock: { label: "Locked", icon: Lock },
    not_sure: { label: "Unsure", icon: HelpCircle },
    ask_for_test: { label: "Test", icon: ClipboardCheck },
};

const TABS = [
    { key: "all", label: "All Submissions", icon: Layers },
    { key: "pending_action", label: "Pending Action", icon: Clock },
    { key: "viewed", label: "Viewed", icon: Eye },
    { key: "ask_for_test", label: "Ask for Test", icon: ClipboardCheck },
    { key: "interested", label: "Audition Approved", icon: ThumbsUp },
    { key: "not_for_this", label: "Does Not Work For This Project", icon: XCircle },
    { key: "shortlist", label: "Shortlist", icon: Star },
    { key: "lock", label: "Lock", icon: Lock },
    { key: "not_sure", label: "Unsure", icon: HelpCircle },
];

// Essential filters shown by default on desktop; the rest collapse behind "More"
// to reduce first-impression cognitive load. All tabs remain in TABS and stay
// fully functional (and the mobile <select> still lists every tab).
const PRIMARY_TAB_KEYS = ["all", "pending_action", "shortlist", "interested"];

// Helper to parse FastAPI/Pydantic validation errors safely
function formatErrorMessage(error) {
    const detail = error?.response?.data?.detail;
    
    if (typeof detail === "string") {
        return detail;
    }
    
    if (Array.isArray(detail) && detail.length > 0) {
        return detail.map((err) => err.msg || err.message || "Validation error").join(", ");
    }
    
    if (typeof error?.response?.data?.message === "string") {
        return error.response.data.message;
    }
    
    if (typeof error?.message === "string") {
        return error.message;
    }
    
    return "Failed to continue. Please try again.";
}

function AvailabilityBudgetSection({ talent, projectShootDates, projectBudget, vis }) {
    const tProj = (talent.project_id && projectShootDates.find(p => p.project_id === talent.project_id)) || projectShootDates[0] || null;
    const tProjBudget = (talent.project_id && projectBudget.find(p => p.project_id === talent.project_id)) || projectBudget[0] || null;
    const showAvail = vis.availability !== false && ((talent.availability && talent.availability.status) || (tProj && tProj.shoot_dates));
    const showBudget = vis.budget && (talent.budget?.status || (tProjBudget && (tProjBudget.lines || []).length));
    
    if (!showAvail && !showBudget && !talent.competitive_brand) return null;
    
    return (
        <div className="mb-8 bg-white p-5 space-y-4 rounded-xl shadow-sm">
            {showAvail && (
                <div data-testid="client-availability">
                    <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-2">Availability</p>
                    {tProj?.shoot_dates && (
                        <p className="text-sm text-[#4A4A4A] mb-2" data-testid="client-shoot-dates">{tProj.shoot_dates}</p>
                    )}
                    {(() => {
                        const lbl = availabilityLabel(talent.availability);
                        if (!lbl) return null;
                        const colorClass = lbl === "Available" ? "text-[#5A7D5A]" : lbl === "Not Available" ? "text-[#9E4A4A]" : "text-[var(--tg-navy-primary)]";
                        return (
                            <div className="text-sm">
                                <p className="flex justify-end">
                                    <span className={`font-semibold font-mono ${colorClass}`} data-testid="client-availability-status">{lbl}</span>
                                </p>
                                {talent.availability?.note && (
                                    <p className="text-xs text-[#8A8A8A] text-right mt-1">{talent.availability.note}</p>
                                )}
                            </div>
                        );
                    })()}
                </div>
            )}
            {showBudget && (
                <div data-testid="client-budget">
                    <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-2">Budget</p>
                    {(() => {
                        const lines = (tProjBudget?.lines || []).filter(l => (l.label || "").trim() || (l.value || "").trim());
                        const budgetLine = lines.find(l => (l.label || "").toLowerCase().includes("budget")) || lines[0] || null;
                        const originalBudgetValue = budgetLine ? budgetLine.value : null;

                        if (talent.budget?.status === "custom" && (talent.budget?.value || "").trim()) {
                            return (
                                <div className="space-y-1.5 text-sm text-[#111111]" data-testid="budget-countered">
                                    {originalBudgetValue && (
                                        <p className="flex justify-between gap-4">
                                            <span className="text-[#4A4A4A]">Original Budget</span>
                                            <span className="text-[#5C5C5C] font-mono">{originalBudgetValue}</span>
                                        </p>
                                    )}
                                    <p className="flex justify-between gap-4 border-t border-black/[0.03] pt-1.5">
                                        <span className="text-[#4A4A4A] font-medium">Counter Budget</span>
                                        <span className="font-semibold text-[var(--tg-navy-primary)] font-mono">{talent.budget.value}</span>
                                    </p>
                                    <p className="flex justify-between gap-4 mt-2">
                                        <span className="text-[#4A4A4A]">Status</span>
                                        <span className="inline-block px-2 py-0.5 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[var(--tg-navy-badge-bg)] text-[var(--tg-navy-primary)] font-medium">Counter-Offer</span>
                                    </p>
                                    {lines.filter(l => l !== budgetLine).map((ln) => (
                                        <p key={`${ln.label}-${ln.value}`} className="flex justify-between gap-4 text-xs text-[#8A8A8A] mt-1">
                                            <span>{ln.label}</span>
                                            <span className="font-mono">{ln.value}</span>
                                        </p>
                                    ))}
                                </div>
                            );
                        }

                        if (talent.budget?.status === "accept") {
                            const offeredBudget = (() => {
                                const tLines = (tProjBudget?.talent_budget || []).filter(l => (l.label || "").trim() || (l.value || "").trim());
                                if (tLines.length > 0) {
                                    const tBudgetLine = tLines.find(l => (l.label || "").toLowerCase().includes("budget")) || tLines[0];
                                    if (tBudgetLine && tBudgetLine.value) {
                                        return tBudgetLine.value;
                                    }
                                }
                                if (tProjBudget?.budget_per_day) {
                                    const bpd = tProjBudget.budget_per_day.trim();
                                    if (bpd.toLowerCase().includes("/ day") || bpd.toLowerCase().includes("/day")) {
                                        return bpd;
                                    }
                                    return `${bpd} / day`;
                                }
                                return originalBudgetValue;
                            })();

                            return (
                                <div className="space-y-1.5 text-sm text-[#111111]" data-testid="budget-agreed">
                                    {offeredBudget && (
                                        <p className="flex justify-between gap-4">
                                            <span className="text-[#4A4A4A] font-medium">Agreed Budget</span>
                                            <span className="font-semibold text-[#5A7D5A] font-mono">{offeredBudget}</span>
                                        </p>
                                    )}
                                    {lines.filter(l => l !== budgetLine).map((ln) => (
                                        <p key={`${ln.label}-${ln.value}`} className="flex justify-between gap-4 text-xs text-[#8A8A8A] mt-1">
                                            <span>{ln.label}</span>
                                            <span className="font-mono">{ln.value}</span>
                                        </p>
                                    ))}
                                </div>
                            );
                        }

                        return (
                            <div className="space-y-1.5">
                                {lines.length > 0 ? (
                                    <ul className="text-sm text-[#111111] space-y-2">
                                        {lines.map((ln) => (
                                            <li key={`${ln.label}-${ln.value}`} className="flex justify-between gap-4">
                                                <span className="text-[#4A4A4A]">{ln.label}</span>
                                                <span className="font-medium font-mono">{ln.value}</span>
                                            </li>
                                        ))}
                                    </ul>
                                ) : null}
                                {(talent.budget?.status === "negotiable" || !talent.budget?.status) && (
                                    <p className="flex justify-between gap-4 text-sm text-[#111111] mt-2 border-t border-black/[0.03] pt-1.5">
                                        <span className="text-[#4A4A4A]">Status</span>
                                        <span className="inline-block px-2 py-0.5 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-black/4 text-[#8A8A8A] font-medium">Negotiable</span>
                                    </p>
                                )}
                            </div>
                        );
                    })()}
                </div>
            )}
            {talent.competitive_brand && (
                <div data-testid="client-competitive-brand">
                    <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-2">Competitive Brand</p>
                    <p className="text-sm text-[#111111]">{talent.competitive_brand}</p>
                </div>
            )}
        </div>
    );
}

/**
 * Stable session-ID helper hoisted outside the component so it is never
 * recreated per render. Includes a try/catch for Safari private mode where
 * sessionStorage access can throw a SecurityError.
 */
function getSessionId() {
    try {
        let sid = sessionStorage.getItem("client_session_id");
        if (!sid) {
            sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
            sessionStorage.setItem("client_session_id", sid);
        }
        return sid;
    } catch (e) {
        return "guest-session";
    }
}

function getVideoDownloadUrl(url) {
    if (!url) return url;
    // Cloudflare Stream: the stored URL is an HLS manifest (.../<uid>/manifest/video.m3u8),
    // which is not a downloadable file. Map it to the MP4 download URL. (MP4 downloads
    // must be enabled server-side; if not, this URL 404s and the caller surfaces an error.)
    if (url.includes("cloudflarestream.com")) {
        if (url.includes("/manifest/")) {
            return `${url.split("/manifest/")[0]}/downloads/default.mp4`;
        }
        return url;
    }
    // Non-Cloudinary (e.g. R2 signed object URLs) are already downloadable — return as-is.
    if (!url.includes("res.cloudinary.com") && !url.includes("/upload/")) {
        return url;
    }
    let cleanUrl = url;
    if (cleanUrl.includes("/upload/")) {
        const parts = cleanUrl.split("/upload/");
        const before = parts[0];
        let after = parts[1];
        const segments = after.split("/");
        const transformations = segments[0];
        if (transformations && !transformations.match(/^v\d+$/)) {
            let newTrans = transformations
                .split(",")
                .filter(t => !t.startsWith("f_") && !t.startsWith("sp_"))
                .join(",");
            newTrans = newTrans ? `${newTrans},f_mp4` : "f_mp4";
            segments[0] = newTrans;
            after = segments.join("/");
        } else {
            after = `f_mp4/${after}`;
        }
        cleanUrl = `${before}/upload/${after}`;
    }
    const mainPath = cleanUrl.split("?")[0].split("#")[0];
    const query = cleanUrl.substring(mainPath.length);
    const lastDotIdx = mainPath.lastIndexOf(".");
    if (lastDotIdx !== -1 && lastDotIdx > mainPath.lastIndexOf("/")) {
        const ext = mainPath.substring(lastDotIdx + 1);
        if (ext.toLowerCase() !== "mp4") {
            cleanUrl = mainPath.substring(0, lastDotIdx) + ".mp4" + query;
        }
    } else {
        cleanUrl = mainPath + ".mp4" + query;
    }
    return cleanUrl;
}

export default function ClientView() {
    const { slug } = useParams();
    const [shareId, setShareId] = useState(null);
    const [loadingShare, setLoadingShare] = useState(false);

    useEffect(() => {
        if (typeof window !== "undefined") {
            const queryParams = new URLSearchParams(window.location.search);
            const sId = queryParams.get("share");
            setShareId(sId);
            setLoadingShare(!!sId);
        }
    }, []);

    const [shareData, setShareData] = useState(null);
    const [shareError, setShareError] = useState(null);
    const [sendingVoice, setSendingVoice] = useState(false);

    const [identified, setIdentified] = useState(false);
    useEffect(() => {
        if (typeof window !== "undefined") {
            setIdentified(!!getViewerToken(slug) || !!shareId);
        }
    }, [slug, shareId]);

    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);

    const [savedReviewer, setSavedReviewer] = useState(() => {
        if (typeof window === "undefined") return null;
        try {
            const saved = localStorage.getItem(`client_view_${slug}`);
            return saved ? JSON.parse(saved) : null;
        } catch (e) {
            return null;
        }
    });
    const [showWelcomeBack, setShowWelcomeBack] = useState(!!savedReviewer);

    const [data, setData] = useState(null);
    const [activeTalent, setActiveTalent] = useState(null);
    const [commentDrafts, setCommentDrafts] = useState({});
    const [seenIds, setSeenIds] = useState(new Set());
    const [reviewedIds, setReviewedIds] = useState(new Set());
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [activeTab, setActiveTab] = useState("all");
    const [showMoreTabs, setShowMoreTabs] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [showResumeBanner, setShowResumeBanner] = useState(false);
    const [showHint, setShowHint] = useState(false);

    // ── Stabilization refs ───────────────────────────────────────────────────
    /** Prevents double-submission on rapid taps on the identity gate. */
    const identifyInFlightRef = useRef(false);
    /** Deduplicates view_talent analytics — fires at most once per talent per session. */
    const trackedSeenRef = useRef(new Set());
    /** Deduplicates review_talent analytics — fires at most once per talent per session. */
    const trackedReviewedRef = useRef(new Set());
    /** Prevents stale state updates when loadData resolves after navigation away. */
    const loadDataMountedRef = useRef(true);
    /** Prevents duplicate comment posts if Save is tapped twice. */
    const commentSavingRef = useRef(new Set());

    // Depend specifically on data?.actions — not the whole data object — so this memo
    // does not recompute when unrelated fields (seen_ids, etc.) are updated.
    const viewerActions = useMemo(() => {
        const m = {};
        (data?.actions || []).forEach((a) => (m[a.talent_id] = a));
        return m;
    }, [data?.actions]);

    const buckets = useMemo(() => {
        const talents = data?.talents || [];
        return {
            all: talents,
            pending_action: talents.filter((t) => !reviewedIds.has(t.id) && !viewerActions[t.id]?.action),
            viewed: talents.filter((t) => reviewedIds.has(t.id) || !!viewerActions[t.id]?.action),
            ask_for_test: talents.filter((t) => viewerActions[t.id]?.action === "ask_for_test"),
            interested: talents.filter((t) => viewerActions[t.id]?.action === "interested"),
            not_for_this: talents.filter((t) => viewerActions[t.id]?.action === "not_for_this"),
            shortlist: talents.filter((t) => viewerActions[t.id]?.action === "shortlist"),
            lock: talents.filter((t) => viewerActions[t.id]?.action === "lock"),
            not_sure: talents.filter((t) => viewerActions[t.id]?.action === "not_sure"),
        };
    }, [data?.talents, reviewedIds, viewerActions]);

    const filteredTalents = useMemo(() => {
        const base = buckets[activeTab] || buckets.all || [];
        if (!searchQuery.trim()) return base;
        const query = searchQuery.toLowerCase().trim();
        return base.filter((t) => {
            const name = (t.name || "").toLowerCase();
            const insta = (t.instagram_handle || "").toLowerCase();
            const id = (t.id || "").toLowerCase();
            const locText = formatTalentLocation(t.location);
            const location = locText.toLowerCase();

            return name.includes(query) ||
                   insta.includes(query) ||
                   id.includes(query) ||
                   location.includes(query);
        });
    }, [buckets, activeTab, searchQuery]);

    useEffect(() => {
        if (!shareId) return;
        const fetchShare = async () => {
            try {
                setLoadingShare(true);
                const { data } = await axios.get(`${API}/public/shares/${shareId}`);
                setShareData(data);
                setActiveTalent(data.talent);
                setData({
                    link: data.link,
                    talents: [data.talent],
                    actions: [],
                    project_budget: data.project_budget || [],
                    project_shoot_dates: data.project_shoot_dates || [],
                    viewer: { email: "share@talentgram", name: "Shared Preview" },
                });
            } catch (e) {
                setShareError(e?.response?.data?.detail || "Failed to load shared preview. It may have expired.");
            } finally {
                setLoadingShare(false);
            }
        };
        fetchShare();
    }, [shareId]);

    const updateLocalAction = useCallback((talentId, action, comment) => {
        setData(prev => {
            if (!prev) return prev;
            const actions = prev.actions || [];
            const existingActionIndex = actions.findIndex(a => a.talent_id === talentId);
            let newActions;
            if (existingActionIndex >= 0) {
                newActions = [...actions];
                if (action === null) {
                    newActions.splice(existingActionIndex, 1);
                } else {
                    newActions[existingActionIndex] = {
                        ...newActions[existingActionIndex],
                        action,
                        comment: comment !== undefined ? comment : newActions[existingActionIndex].comment
                    };
                }
            } else if (action !== null) {
                newActions = [...actions, { talent_id: talentId, action, comment: comment || "" }];
            } else {
                newActions = actions;
            }
            return { ...prev, actions: newActions };
        });
    }, []);

    const loadData = useCallback(async () => {
        try {
            const { data } = await axios.get(`${API}/public/links/${slug}`, {
                headers: {
                    Authorization: `Bearer ${getViewerToken(slug)}`,
                },
            });
            // Guard: skip state updates if component unmounted before response arrived
            if (!loadDataMountedRef.current) return;
            setData(data);
            setSeenIds(new Set(data?.client_state?.seen_talent_ids || []));
            setReviewedIds(new Set(data?.client_state?.reviewed_talent_ids || []));
            axios.post(`${API}/public/links/${slug}/track`, {
                event_type: "open",
                session_id: getSessionId(),
            }).catch(() => {});
        } catch (e) {
            if (!loadDataMountedRef.current) return;
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

    // Mark component as unmounted so in-flight loadData callbacks safely abort
    useEffect(() => {
        loadDataMountedRef.current = true;
        return () => { loadDataMountedRef.current = false; };
    }, []);

    // Keep the open detail view in sync with the latest data so a saved comment
    // or voice note appears immediately (optimistic update + loadData refetch)
    // without needing a page refresh. Same-id object → no remount; only updates
    // when the underlying talent object actually changed (e.g. comments/voice).
    useEffect(() => {
        setActiveTalent((prev) => {
            if (!prev) return prev;
            const fresh = (data?.talents || []).find((t) => t.id === prev.id);
            return fresh && fresh !== prev ? fresh : prev;
        });
    }, [data]);

    const markReviewed = useCallback(
        async (talentId) => {
            if (!talentId) return;
            setReviewedIds((prev) => {
                if (prev.has(talentId)) return prev;
                const n = new Set(prev);
                n.add(talentId);
                return n;
            });
            // Analytics deduplication: fire review_talent track at most once per session per talent.
            // Prevents duplicate events from the 15s auto-review timer + manual markReviewed calls.
            if (!trackedReviewedRef.current.has(talentId)) {
                trackedReviewedRef.current.add(talentId);
                axios.post(`${API}/public/links/${slug}/track`, {
                    event_type: "review_talent",
                    session_id: getSessionId(),
                    talent_id: talentId,
                }).catch(() => {});
            }
            try {
                await axios.post(
                    `${API}/public/links/${slug}/reviewed`,
                    { talent_id: talentId },
                    {
                        headers: {
                            Authorization: `Bearer ${getViewerToken(slug)}`,
                        },
                    },
                );
            } catch (e) {
                console.error(e);
            }
        },
        [slug],
    );

    useEffect(() => {
        if (!data || !data.talents || data.talents.length === 0) return;
        try {
            const lastId = localStorage.getItem(`tg_last_viewed_${slug}`);
            if (lastId) {
                const found = data.talents.find(t => t.id === lastId);
                const isReviewed = reviewedIds.has(lastId) || !!viewerActions[lastId]?.action;
                const resumed = !sessionStorage.getItem(`tg_session_active_${slug}`);
                if (found && !isReviewed && resumed) {
                    setShowResumeBanner(true);
                    sessionStorage.setItem(`tg_session_active_${slug}`, "true");
                }
            } else {
                sessionStorage.setItem(`tg_session_active_${slug}`, "true");
            }
        } catch (e) { console.error(e); }
    }, [data, slug, reviewedIds, viewerActions]);

    // One-time, dismissible orientation hint for first-time clients. Gated by a
    // per-slug localStorage flag; never shown in share-preview mode. Safari
    // private-mode safe (storage access wrapped in try/catch).
    useEffect(() => {
        if (shareId) return;
        if (!data || !data.talents || data.talents.length === 0) return;
        try {
            if (!localStorage.getItem(`tg_hint_seen_${slug}`)) {
                setShowHint(true);
            }
        } catch (e) { /* storage disabled — skip hint */ }
    }, [data, slug, shareId]);

    const dismissHint = useCallback(() => {
        setShowHint(false);
        try { localStorage.setItem(`tg_hint_seen_${slug}`, "1"); } catch (e) { /* storage disabled */ }
    }, [slug]);

    useEffect(() => {
        const prev = document.title;
        const brand = (data?.link?.brand_name || data?.link?.title || "").trim();
        document.title = brand ? `Talentgram | ${brand}` : "Talentgram | Portfolio";
        return () => {
            document.title = prev;
        };
    }, [data?.link?.brand_name, data?.link?.title]);

    const getBrowserAndDevice = () => {
        const ua = navigator.userAgent;
        let browser = "Unknown";
        let device = "Desktop";

        if (ua.includes("Firefox")) browser = "Firefox";
        else if (ua.includes("SamsungBrowser")) browser = "Samsung Browser";
        else if (ua.includes("Opera") || ua.includes("OPR")) browser = "Opera";
        else if (ua.includes("Trident")) browser = "Internet Explorer";
        else if (ua.includes("Edge") || ua.includes("Edg")) browser = "Edge";
        else if (ua.includes("Chrome")) browser = "Chrome";
        else if (ua.includes("Safari")) browser = "Safari";

        if (/Android/i.test(ua)) {
            device = "Android";
        } else if (/iPhone/i.test(ua)) {
            device = "iPhone";
        } else if (/iPad/i.test(ua)) {
            device = "iPad";
        } else if (/Mobile/i.test(ua)) {
            device = "Mobile";
        }
        return { browser, device };
    };

    const identify = async (e, optName, optEmail) => {
        if (e) e.preventDefault();
        const activeName = optName || name;
        const activeEmail = optEmail || email;
        const { browser, device } = getBrowserAndDevice();
        // Synchronous in-flight guard: prevents double-submission on rapid taps
        // (state-based `loading` flag is async and doesn't block a second call immediately).
        if (identifyInFlightRef.current) return;
        identifyInFlightRef.current = true;
        setLoading(true);
        try {
            const response = await axios.post(
                `${API}/public/links/${slug}/identify`,
                {
                    name: activeName,
                    email: activeEmail,
                    browser,
                    device
                },
            );
            if (response.data.token) {
                saveViewerToken(slug, response.data.token);
                
                // Decode token to extract viewer_id (reviewId)
                let reviewId = "";
                try {
                    const payloadPart = response.data.token.split('.')[1];
                    const decoded = JSON.parse(atob(payloadPart.replace(/-/g, '+').replace(/_/g, '/')));
                    reviewId = decoded.viewer_id || "";
                } catch (err) {
                    console.error("Token decoding error:", err);
                }

                localStorage.setItem(`client_view_${slug}`, JSON.stringify({
                    name: activeName,
                    email: activeEmail,
                    reviewId: reviewId
                }));

                setIdentified(true);
                toast.success(
                    `Welcome, ${(activeName || "Guest").split(" ")[0]}`
                );
            } else {
                throw new Error("No token received");
            }
        } catch (e) {
            const errorMessage = formatErrorMessage(e);
            console.error("IDENTIFY ERROR:", errorMessage);
            toast.error(errorMessage);
        } finally {
            identifyInFlightRef.current = false;
            setLoading(false);
        }
    };

    const setAction = useCallback(async (talentId, action) => {
        updateLocalAction(talentId, action);
        markReviewed(talentId);
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
        } catch {
            updateLocalAction(talentId, viewerActions[talentId]?.action);
            toast.error("Action failed");
        }
    }, [slug, updateLocalAction, viewerActions, markReviewed]);

    const setBulkAction = useCallback(async (talentIds, action) => {
        // Optimistic local state update
        setData(prev => {
            if (!prev) return prev;
            const actions = prev.actions || [];
            let newActions = [...actions];
            talentIds.forEach(talentId => {
                const idx = newActions.findIndex(a => a.talent_id === talentId);
                if (idx >= 0) {
                    if (action === null) {
                        newActions.splice(idx, 1);
                    } else {
                        newActions[idx] = { ...newActions[idx], action };
                    }
                } else if (action !== null) {
                    newActions.push({ talent_id: talentId, action, comment: "" });
                }
            });
            return { ...prev, actions: newActions };
        });

        setReviewedIds(prev => {
            const n = new Set(prev);
            talentIds.forEach(id => n.add(id));
            return n;
        });

        try {
            await axios.post(
                `${API}/public/links/${slug}/bulk-action`,
                { talent_ids: talentIds, action },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            toast.success(`Bulk action successfully updated for ${talentIds.length} talent(s)`);
            setSelectedIds(new Set()); // Clear selection
        } catch (e) {
            console.error(e);
            toast.error("Bulk action failed");
            // Reload original state from backend
            loadData();
        }
    }, [slug, loadData]);

    const saveComment = useCallback(async (talentId) => {
        const text = commentDrafts[talentId];
        if (text === undefined || !text.trim()) return;
        if (commentSavingRef.current.has(talentId)) return; // guard double-tap race
        commentSavingRef.current.add(talentId);
        const existing = viewerActions[talentId];
        updateLocalAction(talentId, existing?.action || null, text);
        markReviewed(talentId);
        
        // Optimistic UI update
        const now = new Date().toISOString();
        const newComment = {
            author: data?.viewer?.name || data?.viewer?.email || "Viewer",
            role: (data?.viewer?.role === "admin" || data?.viewer?.role === "team") ? "Admin" : "Viewer",
            timestamp: now,
            decision_status: existing?.action || null,
            content: text.trim()
        };
        setData(prev => {
            if (!prev) return prev;
            const newTalents = prev.talents.map(t => {
                if (t.id === talentId) {
                    return {
                        ...t,
                        comments: [newComment, ...(t.comments || [])]
                    };
                }
                return t;
            });
            return { ...prev, talents: newTalents };
        });

        // Clear input immediately
        setCommentDrafts(prev => ({
            ...prev,
            [talentId]: ""
        }));

        try {
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
            loadData();
        } catch {
            updateLocalAction(talentId, existing?.action, existing?.comment);
            toast.error("Failed to save");
            loadData();
        } finally {
            commentSavingRef.current.delete(talentId);
        }
    }, [commentDrafts, viewerActions, slug, updateLocalAction, markReviewed, data, loadData]);

    const logDownload = useCallback(async (talentId, mediaId) => {
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
    }, [slug]);

    const handleShare = useCallback(async (talentId, mediaId = null) => {
        try {
            const { data } = await axios.post(
                `${API}/public/links/${slug}/share`,
                {
                    talent_id: talentId,
                    media_id: mediaId,
                },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            
            const shareUrl = `${PUBLIC_FRONTEND_URL}/l/${slug}?share=${data.share_id}`;
            
            if (navigator.share) {
                try {
                    await navigator.share({
                        title: `Talentgram | Shared Audition`,
                        text: `Check out this audition showcase on Talentgram!`,
                        url: shareUrl,
                    });
                    toast.success("Shared successfully");
                } catch (e) {
                    if (e.name !== "AbortError") {
                        await navigator.clipboard.writeText(shareUrl);
                        toast.success("Link copied to clipboard");
                    }
                }
            } else {
                await navigator.clipboard.writeText(shareUrl);
                toast.success("Link copied to clipboard");
            }
        } catch (e) {
            toast.error("Failed to generate share link");
            console.error(e);
        }
    }, [slug]);

    const saveVoiceNote = useCallback(async (talentId, blob) => {
        const talents = data?.talents || [];
        const t = talents.find(x => x.id === talentId);
        if (!t || !t.submission_id || !t.project_id) {
            toast.error("Voice feedback is not supported for this card");
            return;
        }

        setSendingVoice(true);
        const formData = new FormData();
        formData.append("talent_id", t.id);
        formData.append("submission_id", t.submission_id);
        formData.append("project_id", t.project_id);
        formData.append("file", blob, "voice_feedback.webm");

        try {
            const response = await axios.post(
                `${API}/public/links/${slug}/feedback/voice`,
                formData,
                {
                    headers: {
                        "Content-Type": "multipart/form-data",
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            const returnedDoc = response.data;
            const now = new Date().toISOString();
            const newVoice = {
                author: data?.viewer?.name || data?.viewer?.email || "Viewer",
                role: (data?.viewer?.role === "admin" || data?.viewer?.role === "team") ? "Admin" : "Viewer",
                timestamp: now,
                content: returnedDoc.content_url
            };
            setData(prev => {
                if (!prev) return prev;
                const newTalents = prev.talents.map(t => {
                    if (t.id === talentId) {
                        return {
                            ...t,
                            voice_notes: [newVoice, ...(t.voice_notes || [])]
                        };
                    }
                    return t;
                });
                return { ...prev, talents: newTalents };
            });
            toast.success("Voice feedback uploaded successfully");
            markReviewed(talentId);
            loadData();
        } catch (e) {
            toast.error("Failed to upload voice feedback");
            console.error(e);
            loadData();
        } finally {
            setSendingVoice(false);
        }
    }, [data, slug, markReviewed, loadData]);

    const markSeen = useCallback(
        async (talentId) => {
            if (!talentId) return;
            // Analytics deduplication: fire view_talent track at most once per session per talent.
            // Prevents double-firing from concurrent IntersectionObserver + onOpen calls.
            if (!trackedSeenRef.current.has(talentId)) {
                trackedSeenRef.current.add(talentId);
                axios.post(`${API}/public/links/${slug}/track`, {
                    event_type: "view_talent",
                    session_id: getSessionId(),
                    talent_id: talentId,
                }).catch(() => {});
            }
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
            }
        },
        [slug],
    );

    if (shareId) {
        if (loadingShare) {
            return (
                <div className="min-h-screen bg-white flex items-center justify-center">
                    <Loader2 className="w-8 h-8 animate-spin text-[var(--tg-navy-primary)]" />
                </div>
            );
        }
        if (shareError) {
            return (
                <div className="min-h-screen bg-white flex flex-col items-center justify-center p-6 text-center">
                    <XCircle className="w-12 h-12 text-[#9E4A4A] mb-4" />
                    <h2 className="text-xl font-display text-[#111111] mb-2 font-semibold">Shared Preview Expired</h2>
                    <p className="text-sm text-[#333333] max-w-sm leading-relaxed">{shareError}</p>
                </div>
            );
        }
        if (activeTalent) {
            return (
                <TalentDetail
                    talent={activeTalent}
                    talents={[activeTalent]}
                    link={data?.link || {}}
                    slug={slug}
                    projectBudget={data?.project_budget || []}
                    projectShootDates={data?.project_shoot_dates || []}
                    viewerAction={null}
                    viewerActions={{}}
                    reviewedIds={new Set()}
                    isReviewed={true}
                    onMarkReviewed={() => {}}
                    onClose={() => {}}
                    onNavigate={() => {}}
                    setAction={() => {}}
                    commentDraft=""
                    setCommentDraft={() => {}}
                    saveComment={() => {}}
                    logDownload={() => {}}
                    onShare={() => {}}
                    saveVoiceNote={() => {}}
                    sendingVoice={false}
                    isSharePreview={true}
                />
            );
        }
    }

    if (!identified) {
        return (
            <div className="min-h-screen bg-white flex items-center justify-center p-6 text-black/85">
                <div className="w-full max-w-md flex flex-col items-center">
                    {/* Logo top-centered */}
                    <div className="mb-10 text-center">
                        <Logo size={120} className="mx-auto" forceVariant="black" />
                    </div>

                    <div className="w-full border border-black/[0.06] rounded-2xl p-8 md:p-10 bg-white">
                        {showWelcomeBack && savedReviewer ? (
                            <div className="text-center">
                                <p className="eyebrow mb-2 tracking-[0.12em] text-[#5C5C5C] uppercase text-[11px]">Welcome Back</p>
                                <h1 className="font-display text-2xl tracking-wide mb-6 text-[#111111]">
                                    Is this you?
                                </h1>
                                
                                <div className="mb-8 p-4 bg-black/[0.02] rounded-xl text-left border border-black/[0.04]">
                                    <div className="mb-3">
                                        <span className="text-[10px] text-[#8A8A8A] tracking-[0.08em] uppercase block">
                                            Name
                                        </span>
                                        <span className="text-base font-medium text-[#111111]">{savedReviewer.name}</span>
                                    </div>
                                    <div>
                                        <span className="text-[10px] text-[#8A8A8A] tracking-[0.08em] uppercase block">
                                            Email
                                        </span>
                                        <span className="text-base font-medium text-[#111111]">{savedReviewer.email}</span>
                                    </div>
                                </div>
                                
                                <button
                                    onClick={() => identify(null, savedReviewer.name, savedReviewer.email)}
                                    disabled={loading}
                                    data-testid="identity-continue-btn"
                                    className="w-full bg-[#1A1A1A] text-white py-3.5 rounded-xl text-sm font-medium hover:bg-[#111111] transition-colors duration-150 inline-flex items-center justify-center gap-2 tracking-[0.04em] mb-4"
                                >
                                    {loading && (
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                    )}
                                    Continue Review
                                </button>
                                
                                <button
                                    onClick={() => {
                                        localStorage.removeItem(`client_view_${slug}`);
                                        setSavedReviewer(null);
                                        setShowWelcomeBack(false);
                                        setName("");
                                        setEmail("");
                                    }}
                                    disabled={loading}
                                    data-testid="identity-not-you-btn"
                                    className="w-full bg-transparent text-[#8A8A8A] hover:text-[#111111] py-2 text-xs font-medium transition-colors duration-150 tracking-[0.04em]"
                                >
                                    Not You?
                                </button>
                            </div>
                        ) : (
                            <form
                                onSubmit={identify}
                                data-testid="identity-gate-form"
                            >
                                <p className="eyebrow mb-2 tracking-[0.12em] text-[#5C5C5C]">Curated Portfolio</p>
                                <h1 className="font-display text-2xl tracking-wide mb-4 text-[#111111]">
                                    A private review awaits you.
                                </h1>
                                <p className="text-[#8A8A8A] text-sm mb-8 leading-relaxed">
                                    Please share your name and email to continue. This
                                    helps us follow up on your selections.
                                </p>
                                <label className="block mb-5">
                                    <span className="text-[11px] text-[#8A8A8A] tracking-[0.08em] uppercase">
                                        Your Name
                                    </span>
                                    <input
                                        value={name}
                                        onChange={(e) => setName(e.target.value)}
                                        required
                                        data-testid="identity-name-input"
                                        className="mt-2 w-full bg-transparent border-b border-black/[0.06] focus:border-black/25 outline-none py-2 text-base text-[#111111] placeholder:text-black/25 transition-colors duration-150"
                                    />
                                </label>
                                <label className="block mb-8">
                                    <span className="text-[11px] text-[#8A8A8A] tracking-[0.08em] uppercase">
                                        Email
                                    </span>
                                    <input
                                        type="email"
                                        value={email}
                                        onChange={(e) => setEmail(e.target.value)}
                                        required
                                        data-testid="identity-email-input"
                                        className="mt-2 w-full bg-transparent border-b border-black/[0.06] focus:border-black/25 outline-none py-2 text-base text-[#111111] placeholder:text-black/25 transition-colors duration-150"
                                    />
                                </label>
                                <button
                                    type="submit"
                                    disabled={loading}
                                    data-testid="identity-submit-btn"
                                    className="w-full bg-[#1A1A1A] text-white py-3.5 rounded-xl text-sm font-medium hover:bg-[#111111] transition-colors duration-150 inline-flex items-center justify-center gap-2 tracking-[0.04em]"
                                >
                                    {loading && (
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                    )}
                                    Enter Review
                                </button>
                            </form>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    if (!data) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-white">
                <div className="animate-pulse space-y-4 w-full max-w-md px-6">
                    <div className="h-4 bg-black/[0.04] rounded w-3/4 mx-auto"></div>
                    <div className="h-32 bg-black/[0.04] rounded-xl"></div>
                    <div className="space-y-2">
                        <div className="h-3 bg-black/[0.04] rounded"></div>
                        <div className="h-3 bg-black/[0.04] rounded w-5/6"></div>
                    </div>
                </div>
            </div>
        );
    }

    const { link, talents, viewer } = data;
    const vis = link.visibility || {};
    const projectBudget = data.project_budget || [];
    const projectShootDates = data.project_shoot_dates || [];
    const subjectAddedAt = data.subject_added_at || {};
    const prevVisitAt = data?.client_state?.prev_visit_at || null;

    const isNew = (id) => {
        if (!prevVisitAt) return false;
        // A talent the client has already seen/reviewed is never "new", regardless
        // of when it was added relative to the (token-lifetime) visit baseline.
        if (seenIds.has(id) || reviewedIds.has(id)) return false;
        const t = subjectAddedAt[id];
        if (!t) return false;
        return new Date(t).getTime() > new Date(prevVisitAt).getTime();
    };

    const reviewedCount = talents.filter((t) => reviewedIds.has(t.id) || !!viewerActions[t.id]?.action).length;
    const seenCount = reviewedCount;
    const totalCount = talents.length;
    const reviewedPct = totalCount === 0 ? 0 : Math.round((reviewedCount / totalCount) * 100);

    return (
        <div className="h-[100dvh] flex flex-col bg-white text-[#111111] overflow-hidden" data-testid="client-view-page">
            <header className="shrink-0 bg-white/95 border-b border-black/[0.04]">
                <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-3 md:py-4">
                    <div className="flex flex-col items-center justify-center text-center gap-1.5">
                        <Logo size={36} className="hidden md:block mx-auto mb-2.5" />
                        <Logo size={28} className="md:hidden mx-auto mb-2" />
                        <h1 className="font-display text-base md:text-xl tracking-wide text-[#111111] max-w-2xl truncate">
                            {link.title}
                        </h1>
                        <p className="text-xs text-[#8A8A8A] mt-1 font-sans">
                            {viewer.name} &bull; {seenCount} / {totalCount} viewed
                        </p>
                    </div>
                    <div className="md:hidden mt-3 h-0.5 bg-black/[0.04] rounded-full overflow-hidden">
                        <div
                            className="h-full bg-[var(--tg-navy-primary)] transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar-mobile"
                        />
                    </div>
                </div>
            </header>

            <div className={`flex-1 min-h-0 ${activeTalent ? "overflow-hidden" : "overflow-y-auto"}`}>
                <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-5 md:py-8">
                {showResumeBanner && (() => {
                    // Guard: localStorage.getItem can throw in Safari private browsing
                    let lastId = null;
                    try { lastId = localStorage.getItem(`tg_last_viewed_${slug}`); } catch (e) { /* storage disabled */ }
                    const lastTalent = lastId ? talents.find(t => t.id === lastId) : null;
                    if (!lastTalent) return null;
                    return (
                        <div className="mb-6 animate-fade-in" data-testid="resume-review-banner">
                            <div className="bg-[var(--tg-navy-light)] border border-[var(--tg-navy-border)] rounded-xl p-4 flex items-center justify-between gap-4 backdrop-blur-sm shadow-[0_4px_12px_-6px_rgba(30,41,59,0.06)]">
                                <div className="flex items-center gap-3">
                                    <Clock className="w-4 h-4 text-[var(--tg-navy-primary)]" />
                                    <span className="text-sm text-[#111111] font-medium">
                                        Continue reviewing <span className="underline font-semibold">{privatizeName(lastTalent.name)}</span> where you left off?
                                    </span>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setActiveTalent(lastTalent);
                                            markSeen(lastTalent.id);
                                            setShowResumeBanner(false);
                                        }}
                                        className="px-3.5 py-1.5 bg-[#1A1A1A] hover:bg-[#111111] text-white text-xs font-semibold rounded-full transition-colors active:scale-[0.97]"
                                    >
                                        Resume
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setShowResumeBanner(false)}
                                        className="p-1 hover:bg-black/5 rounded-full text-[#8A8A8A] hover:text-[#111111]"
                                        aria-label="Dismiss banner"
                                    >
                                        <X className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        </div>
                    );
                })()}

                {showHint && (
                    <div className="mb-6 animate-fade-in" data-testid="first-time-hint">
                        <div className="bg-black/[0.02] border border-black/[0.06] rounded-xl px-4 py-3 flex items-start justify-between gap-4">
                            <div className="flex items-start gap-3 min-w-0">
                                <Sparkles className="w-4 h-4 text-[var(--tg-navy-primary)] shrink-0 mt-0.5" />
                                <p className="text-xs md:text-sm text-[#4A4A4A] leading-relaxed">
                                    Review each talent, choose the ones that fit — your selections are saved automatically.
                                </p>
                            </div>
                            <button
                                type="button"
                                onClick={dismissHint}
                                className="flex items-center justify-center w-11 h-11 -m-2 text-[#8A8A8A] hover:text-[#111111] shrink-0"
                                aria-label="Dismiss hint"
                                data-testid="first-time-hint-dismiss"
                            >
                                <X className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                )}

                <div
                    className="mb-5 hidden md:flex items-center gap-5"
                    data-testid="review-progress"
                >
                    <div className="flex-1 h-0.5 bg-black/[0.04] rounded-full overflow-hidden">
                        <div
                            className="h-full bg-[var(--tg-navy-primary)] transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar"
                        />
                    </div>
                </div>

                {(() => {
                    const visibleTabs = TABS;
                    // Desktop progressive disclosure: primary tabs always shown;
                    // secondary tabs revealed via "More" (or auto-shown if the
                    // active tab lives in the secondary group).
                    const primaryTabs = TABS.filter((t) => PRIMARY_TAB_KEYS.includes(t.key));
                    const secondaryTabs = TABS.filter((t) => !PRIMARY_TAB_KEYS.includes(t.key));
                    const activeInSecondary = secondaryTabs.some((t) => t.key === activeTab);
                    const showSecondary = showMoreTabs || activeInSecondary;
                    const desktopTabs = showSecondary ? [...primaryTabs, ...secondaryTabs] : primaryTabs;
                    return (
                        <div
                            className="mb-8 md:mb-12 -mx-6 md:mx-0 px-6 md:px-0 border-b border-black/[0.04] pb-6"
                            data-testid="client-view-tabs-container"
                        >
                            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                                {/* Desktop tab row */}
                                <div className="hidden md:flex items-center gap-3 overflow-x-auto whitespace-nowrap" style={{ scrollbarWidth: "none" }} data-testid="client-view-tabs">
                                    {desktopTabs.map((tab) => {
                                        const count = buckets[tab.key]?.length || 0;
                                        const active = activeTab === tab.key;
                                        return (
                                            <button
                                                key={tab.key}
                                                type="button"
                                                onClick={() => setActiveTab(tab.key)}
                                                data-testid={`client-tab-${tab.key}`}
                                                className={`inline-flex items-center gap-2 px-4 py-2 rounded-full text-[11px] tracking-[0.08em] uppercase transition-colors duration-150 border shrink-0 active:scale-[0.97] ${
                                                    active
                                                        ? "bg-[#1A1A1A] text-white border-[#1A1A1A]"
                                                        : "border-black/[0.06] text-[#5C5C5C] hover:text-[#111111] hover:border-black/15"
                                                }`}
                                            >
                                                <tab.icon className="w-3.5 h-3.5" />
                                                {tab.label}
                                                <span
                                                    className={`font-mono text-[10px] ${active ? "text-white/60" : "text-[#8A8A8A]"}`}
                                                >
                                                    {count}
                                                </span>
                                            </button>
                                        );
                                    })}
                                    {secondaryTabs.length > 0 && !activeInSecondary && (
                                        <button
                                            type="button"
                                            onClick={() => setShowMoreTabs((v) => !v)}
                                            data-testid="client-tabs-more-toggle"
                                            aria-expanded={showSecondary}
                                            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-full text-[11px] tracking-[0.08em] uppercase transition-colors duration-150 border shrink-0 active:scale-[0.97] border-black/[0.06] text-[#5C5C5C] hover:text-[#111111] hover:border-black/15"
                                        >
                                            {showMoreTabs ? "Less" : "More"}
                                            <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-150 ${showMoreTabs ? "rotate-180" : ""}`} />
                                        </button>
                                    )}
                                </div>

                                {/* Mobile dropdown selector */}
                                <div className="flex md:hidden flex-col gap-1.5 w-full" data-testid="client-view-tabs-mobile">
                                    <label htmlFor="mobile-filter-select" className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] font-semibold">
                                        Filter Submissions
                                    </label>
                                    <div className="relative">
                                        <select
                                            id="mobile-filter-select"
                                            data-testid="mobile-filter-select"
                                            value={activeTab}
                                            onChange={(e) => setActiveTab(e.target.value)}
                                            className="w-full appearance-none bg-white border border-black/10 rounded-xl px-4 py-3 pr-10 text-xs font-semibold tracking-wider uppercase text-[#111111] focus:outline-none focus:border-black transition-colors"
                                        >
                                            {visibleTabs.map((tab) => {
                                                const count = buckets[tab.key]?.length || 0;
                                                return (
                                                    <option key={tab.key} value={tab.key}>
                                                        {tab.label} ({count})
                                                    </option>
                                                );
                                            })}
                                        </select>
                                        <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center pr-4 text-[#8A8A8A]">
                                            <ChevronDown className="w-4 h-4" />
                                        </div>
                                    </div>
                                </div>

                                {/* Bulk Select — labelled and separated from the filters above
                                    so it doesn't read as another filter control. */}
                                <div className="flex flex-col gap-1.5">
                                    <span className="md:hidden text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] font-semibold">Bulk Select</span>
                                    <div className="flex items-center gap-2 flex-wrap text-xs">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const visibleIds = filteredTalents.map(t => t.id);
                                                setSelectedIds(new Set(visibleIds));
                                                toast.success(`Selected ${visibleIds.length} shown talent(s)`);
                                            }}
                                            data-testid="select-all-visible"
                                            className="px-3 py-1.5 border border-black/10 hover:border-black/25 text-[#4A4A4A] hover:text-[#111111] rounded-md transition-colors font-medium bg-white"
                                        >
                                            Select shown ({filteredTalents.length})
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => {
                                                const allIds = talents.map(t => t.id);
                                                setSelectedIds(new Set(allIds));
                                                toast.success(`Selected all ${allIds.length} talent(s)`);
                                            }}
                                            data-testid="select-all-filtered"
                                            className="px-3 py-1.5 border border-black/10 hover:border-black/25 text-[#4A4A4A] hover:text-[#111111] rounded-md transition-colors font-medium bg-white"
                                        >
                                            Select all ({talents.length})
                                        </button>
                                        {selectedIds.size > 0 && (
                                            <button
                                                type="button"
                                                onClick={() => {
                                                    setSelectedIds(new Set());
                                                    toast.success("Selection cleared");
                                                }}
                                                data-testid="clear-selection"
                                                className="px-3 py-1.5 text-rose-600 hover:text-rose-700 hover:bg-rose-50 rounded-md transition-colors font-semibold"
                                            >
                                                Clear Selection
                                            </button>
                                        )}
                                    </div>
                                </div>
                            </div>

                            {/* Search bar row */}
                            <div className="mt-4 max-w-md" data-testid="search-container">
                                <div className="relative">
                                    <input
                                        type="text"
                                        placeholder="Search by name, instagram, location..."
                                        value={searchQuery}
                                        onChange={(e) => setSearchQuery(e.target.value)}
                                        data-testid="search-input"
                                        className="w-full bg-[#fdfdfd] border border-black/[0.06] hover:border-black/15 focus:border-black/30 rounded-full py-2.5 pl-5 pr-10 text-xs text-[#111111] placeholder-[#8A8A8A] focus:outline-none transition-all duration-150 shadow-sm"
                                    />
                                    {searchQuery && (
                                        <button
                                            type="button"
                                            onClick={() => setSearchQuery("")}
                                            className="absolute inset-y-0 right-0 flex items-center pr-4 text-[#8A8A8A] hover:text-[#111111] transition-colors"
                                            aria-label="Clear search"
                                        >
                                            <X className="w-3.5 h-3.5" />
                                        </button>
                                    )}
                                </div>
                            </div>
                        </div>
                    );
                })()}


                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-5 md:gap-7"
                    data-testid="client-talents-grid"
                >
                    {filteredTalents.length === 0 ? (
                        <div
                            className="col-span-full text-center py-20 text-[#8A8A8A] text-sm"
                            data-testid="client-tab-empty"
                        >
                            {activeTab === "pending_action" && "You've reviewed everyone — nice work."}
                            {activeTab === "viewed" && "No viewed talents."}
                            {activeTab === "ask_for_test" && "No test requests."}
                            {activeTab === "interested" && "No approved auditions."}
                            {activeTab === "not_for_this" && "No rejected talents."}
                            {activeTab === "shortlist" && "No shortlists."}
                            {activeTab === "lock" && "No locked talents."}
                            {activeTab === "not_sure" && "No unsure talents."}
                        </div>
                    ) : (
                        filteredTalents.map((t) => (
                            <TalentCard
                                key={t.id}
                                talent={t}
                                vis={vis}
                                action={viewerActions[t.id]?.action}
                                seen={seenIds.has(t.id)}
                                isNew={isNew(t.id)}
                                slug={slug}
                                setActiveTalent={setActiveTalent}
                                markSeen={markSeen}
                                selected={selectedIds.has(t.id)}
                                onSelect={(id, checked) => {
                                    setSelectedIds(prev => {
                                        const n = new Set(prev);
                                        if (checked) n.add(id);
                                        else n.delete(id);
                                        return n;
                                    });
                                }}
                            />
                        ))
                    )}
                </div>
            </div>
            </div>

            {activeTalent && (
                <TalentDetail
                    talent={activeTalent}
                    talents={filteredTalents}
                    link={link}
                    slug={slug}
                    projectBudget={projectBudget}
                    projectShootDates={projectShootDates}
                    viewerAction={viewerActions[activeTalent.id]}
                    viewerActions={viewerActions}
                    reviewedIds={reviewedIds}
                    isReviewed={reviewedIds.has(activeTalent.id) || !!viewerActions[activeTalent.id]?.action}
                    onMarkReviewed={markReviewed}
                    onClose={() => setActiveTalent(null)}
                    onNavigate={(t) => {
                        setActiveTalent(t);
                        markSeen(t.id);
                        try {
                            localStorage.setItem(`tg_last_viewed_${slug}`, t.id);
                        } catch (e) { console.error(e); }
                    }}
                    setAction={setAction}
                    commentDraft={
                        commentDrafts[activeTalent.id] ??
                        ""
                    }
                    setCommentDraft={(text) =>
                        setCommentDrafts(prev => ({
                            ...prev,
                            [activeTalent.id]: text,
                        }))
                    }
                    saveComment={() => saveComment(activeTalent.id)}
                    logDownload={logDownload}
                    onShare={handleShare}
                    saveVoiceNote={saveVoiceNote}
                />
            )}

            {/* Bulk Actions bar — mobile: full-width, 3-col action grid (no clipping).
                Desktop: centered horizontal row (unchanged). */}
            {selectedIds.size > 0 && (
                <div
                    data-testid="bulk-action-bar"
                    className="fixed z-40 animate-fade-in bg-[var(--tg-navy-primary)] text-white border border-[var(--tg-navy-border)] shadow-2xl rounded-2xl
                               left-2 right-2 bottom-[calc(1rem+env(safe-area-inset-bottom))] px-4 py-3
                               md:left-1/2 md:right-auto md:-translate-x-1/2 md:bottom-[calc(1.5rem+env(safe-area-inset-bottom))] md:px-6 md:py-4 md:max-w-3xl"
                >
                    <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 md:gap-6">
                        {/* Header: count + label; cancel inline on mobile */}
                        <div className="flex items-center justify-between md:justify-start gap-2.5 shrink-0">
                            <div className="flex items-center gap-2.5">
                                <div className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center text-xs font-mono font-semibold">
                                    {selectedIds.size}
                                </div>
                                <span className="text-sm font-medium tracking-wide">Bulk actions</span>
                            </div>
                            <button
                                onClick={() => setSelectedIds(new Set())}
                                className="md:hidden flex items-center justify-center w-9 h-9 -mr-1 hover:bg-white/10 rounded-full transition-colors"
                                aria-label="Cancel selection"
                            >
                                <X className="w-4 h-4 text-white/70" />
                            </button>
                        </div>

                        <div className="hidden md:block h-6 w-px bg-white/10" />

                        <div className="grid grid-cols-3 gap-2 md:flex md:flex-wrap md:items-center">
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "interested")}
                                data-testid="bulk-action-interested"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-white/5 hover:bg-white/10 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-white/5"
                            >
                                <ThumbsUp className="w-3.5 h-3.5 shrink-0" />
                                <span className="hidden md:inline">Audition Approved</span>
                                <span className="md:hidden">Approve</span>
                            </button>
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "shortlist")}
                                data-testid="bulk-action-shortlist"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-white/5 hover:bg-white/10 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-white/5"
                            >
                                <Star className="w-3.5 h-3.5 shrink-0" />
                                Shortlist
                            </button>
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "not_for_this")}
                                data-testid="bulk-action-not_for_this"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-rose-950/40 hover:bg-rose-900/60 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-rose-500/20 text-rose-200"
                            >
                                <XCircle className="w-3.5 h-3.5 shrink-0" />
                                <span className="hidden md:inline">Does Not Work</span>
                                <span className="md:hidden">Reject</span>
                            </button>
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "lock")}
                                data-testid="bulk-action-lock"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-white/5 hover:bg-white/10 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-white/5"
                            >
                                <Lock className="w-3.5 h-3.5 shrink-0" />
                                Lock
                            </button>
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "not_sure")}
                                data-testid="bulk-action-not_sure"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-white/5 hover:bg-white/10 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-white/5"
                            >
                                <HelpCircle className="w-3.5 h-3.5 shrink-0" />
                                Unsure
                            </button>
                            <button
                                onClick={() => setBulkAction(Array.from(selectedIds), "ask_for_test")}
                                data-testid="bulk-action-ask_for_test"
                                className="inline-flex items-center justify-center gap-1.5 px-2 md:px-3 py-2 min-h-[44px] bg-white/5 hover:bg-white/10 rounded-lg text-[11px] md:text-xs font-semibold tracking-wide transition-colors active:scale-95 border border-white/5"
                            >
                                <ClipboardCheck className="w-3.5 h-3.5 shrink-0" />
                                <span className="hidden md:inline">Ask for Test</span>
                                <span className="md:hidden">Test</span>
                            </button>
                        </div>

                        <div className="hidden md:block h-6 w-px bg-white/10" />

                        <button
                            onClick={() => setSelectedIds(new Set())}
                            className="hidden md:flex items-center justify-center w-11 h-11 hover:bg-white/10 rounded-full transition-colors shrink-0"
                            aria-label="Cancel selection"
                        >
                            <X className="w-4 h-4 text-white/60 hover:text-white" />
                        </button>
                    </div>
                </div>
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
    viewerActions,
    reviewedIds,
    isReviewed,
    onMarkReviewed,
    onClose,
    onNavigate,
    setAction,
    commentDraft,
    setCommentDraft,
    saveComment,
    logDownload,
    onShare,
    saveVoiceNote,
    sendingVoice,
    isSharePreview = false,
}) {
    const renderCommentsHistory = () => {
        if (!talent.comments || talent.comments.length === 0) return null;
        return (
            <div className="mt-4 space-y-3" data-testid="threaded-comments-container">
                <p className="text-[10px] font-mono tracking-wider uppercase text-black/40">Review History ({talent.comments.length})</p>
                <div className="space-y-3 max-h-[250px] overflow-y-auto pr-1 custom-scrollbar">
                    {talent.comments.map((c, idx) => (
                        <div key={idx} className="p-3 bg-black/[0.02] border border-black/[0.04] rounded-xl hover:bg-black/[0.03] transition-all duration-150" data-testid={`comment-item-${idx}`}>
                            <div className="flex items-start justify-between gap-2 mb-1.5">
                                <div className="flex items-center gap-1.5 flex-wrap">
                                    <span className="text-xs font-semibold text-black/80">{c.author}</span>
                                    <span className={`px-1.5 py-0.5 text-[9px] font-medium rounded-full uppercase tracking-wider ${
                                        c.role?.toLowerCase() === 'admin' 
                                            ? 'bg-amber-100 text-amber-800 border border-amber-200' 
                                            : 'bg-gray-100 text-gray-600 border border-gray-200'
                                    }`}>
                                        {c.role}
                                    </span>
                                    {c.decision_status && (
                                        <span className="text-[10px] text-black/50 bg-black/[0.04] px-1.5 py-0.5 rounded-md font-medium capitalize">
                                            {c.decision_status.replace(/_/g, ' ')}
                                        </span>
                                    )}
                                </div>
                                <span className="text-[10px] text-black/40 font-mono">
                                    {new Date(c.timestamp).toLocaleDateString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}
                                </span>
                            </div>
                            <p className="text-xs text-black/70 leading-relaxed break-words whitespace-pre-wrap">{c.content || c.comment}</p>
                        </div>
                    ))}
                </div>
            </div>
        );
    };

    const renderVoiceHistory = () => {
        if (!talent.voice_notes || talent.voice_notes.length === 0) return null;
        return (
            <div className="mt-4 space-y-3" data-testid="voice-notes-container">
                <p className="text-[10px] font-mono tracking-wider uppercase text-black/40">Voice Reviews ({talent.voice_notes.length})</p>
                <div className="space-y-3 max-h-[250px] overflow-y-auto pr-1 custom-scrollbar">
                    {talent.voice_notes.map((vn, idx) => (
                        <div key={idx} className="p-3 bg-black/[0.02] border border-black/[0.04] rounded-xl hover:bg-black/[0.03] transition-all duration-150" data-testid={`voice-note-item-${idx}`}>
                            <div className="flex items-start justify-between gap-2 mb-2">
                                <div className="flex items-center gap-1.5 flex-wrap">
                                    <span className="text-xs font-semibold text-black/80">{vn.author}</span>
                                    <span className={`px-1.5 py-0.5 text-[9px] font-medium rounded-full uppercase tracking-wider ${
                                        vn.role?.toLowerCase() === 'admin' 
                                            ? 'bg-amber-100 text-amber-800 border border-amber-200' 
                                            : 'bg-gray-100 text-gray-600 border border-gray-200'
                                    }`}>
                                        {vn.role}
                                    </span>
                                </div>
                                <span className="text-[10px] text-black/40 font-mono">
                                    {new Date(vn.timestamp).toLocaleDateString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}
                                </span>
                            </div>
                            <audio src={vn.content} controls className="w-full h-8 mt-1 accent-black" />
                        </div>
                    ))}
                </div>
            </div>
        );
    };
    const vis = link.visibility || {};
    const project = link || {};
    const visibleActions = ACTIONS;
    const mediaAll = useMemo(() => talent.media || [], [talent.media]);
    const portfolioOn = vis.portfolio !== false;
    const indianOn = portfolioOn && (vis.indian_images ?? true);
    const westernOn = portfolioOn && (vis.western_images ?? true);
    const portfolioGenericOn = portfolioOn;
    const images = useMemo(() => {
        const pImgs = portfolioGenericOn ? mediaAll.filter((m) => m.category === "portfolio") : [];
        const iImgs = indianOn ? mediaAll.filter((m) => m.category === "indian") : [];
        const wImgs = westernOn ? mediaAll.filter((m) => m.category === "western") : [];
        return [...pImgs, ...iImgs, ...wImgs];
    }, [mediaAll, portfolioGenericOn, indianOn, westernOn]);
    const intro = mediaAll.find((m) => m.category === "video") || null;
    const takes = mediaAll.filter((m) => m.category === "take");
    const [idx, setIdx] = useState(0);
    const overlayRef = useRef(null);
    const [isModalOpen, setIsModalOpen] = useState(false);
    const [isDetailsExpanded, setIsDetailsExpanded] = useState(true);
    const [isDownloadingPackage, setIsDownloadingPackage] = useState(false);
    // C3: in-flight guards prevent duplicate downloads from rapid taps.
    const downloadPackageInFlightRef = useRef(false);
    const downloadingRef = useRef(new Set());
    const [downloadingIds, setDownloadingIds] = useState(() => new Set());

    const handleCopyForm = () => {
        try {
            const lines = [];
            lines.push(`Name: ${privatizeName(talent.name)}`);
            if (talent.age) lines.push(`Age: ${talent.age}`);
            if (talent.height) lines.push(`Height: ${talent.height}`);
            if (talent.location) {
                const locStr = formatTalentLocation(talent.location);
                if (locStr) lines.push(`Location: ${locStr}`);
            }
            if (talent.ethnicity) lines.push(`Ethnicity: ${talent.ethnicity}`);
            
            // Availability status
            const availLabel = availabilityLabel(talent.availability);
            if (availLabel) {
                let val = availLabel;
                if (talent.availability?.note) {
                    val += ` — ${talent.availability.note}`;
                }
                lines.push(`Availability: ${val}`);
            }

            // Budget status
            if (talent.budget?.status) {
                const bstatus = talent.budget.status;
                if (bstatus === "custom" && talent.budget.value) {
                    lines.push(`Budget: Counter Budget: ${talent.budget.value}`);
                } else if (bstatus === "accept") {
                    // Try to get original budget
                    const tProjBudget = (talent.project_id && projectBudget.find(p => p.project_id === talent.project_id)) || projectBudget[0] || null;
                    const offeredBudget = (() => {
                        const tLines = (tProjBudget?.talent_budget || []).filter(l => (l.label || "").trim() || (l.value || "").trim());
                        if (tLines.length > 0) {
                            const tBudgetLine = tLines.find(l => (l.label || "").toLowerCase().includes("budget")) || tLines[0];
                            if (tBudgetLine && tBudgetLine.value) {
                                return tBudgetLine.value;
                            }
                        }
                        if (tProjBudget?.budget_per_day) {
                            const bpd = tProjBudget.budget_per_day.trim();
                            if (bpd.toLowerCase().includes("/ day") || bpd.toLowerCase().includes("/day")) {
                                return bpd;
                            }
                            return `${bpd} / day`;
                        }
                        return null;
                    })();
                    lines.push(`Budget: Agreed Budget (${offeredBudget || "Project Budget"})`);
                } else {
                    lines.push(`Budget: ${bstatus.charAt(0).toUpperCase() + bstatus.slice(1)}`);
                }
            }

            if (talent.competitive_brand) {
                lines.push(`Competitive Brand: ${talent.competitive_brand}`);
            }

            if (talent.instagram_handle) {
                lines.push(`Instagram: @${talent.instagram_handle.replace("@", "")}`);
                if (talent.instagram_followers) {
                    lines.push(`Instagram Followers: ${talent.instagram_followers}`);
                }
            }

            if ((talent.custom_answers || []).length > 0) {
                lines.push("");
                lines.push("Additional Details:");
                talent.custom_answers.forEach((qa) => {
                    lines.push(`- ${qa.question}: ${qa.answer}`);
                });
            }

            if ((talent.work_links || []).length > 0) {
                lines.push("");
                lines.push("Work Links:");
                talent.work_links.forEach((w) => {
                    const { label, url } = parseStoredWorkLink(w);
                    lines.push(label ? `- ${label}: ${url}` : `- ${url}`);
                });
            }

            const textToCopy = lines.join("\n");
            navigator.clipboard.writeText(textToCopy);
            toast.success("Talent details form copied to clipboard!");
        } catch (err) {
            console.error("Failed to copy form:", err);
            toast.error("Failed to copy form. Please try again.");
        }
    };

    const handleDownloadPackage = async () => {
        if (downloadPackageInFlightRef.current) return; // C3: block duplicate clicks
        downloadPackageInFlightRef.current = true;
        setIsDownloadingPackage(true);
        try {
            const token = getViewerToken(slug);
            const response = await axios.get(
                `${API}/public/links/${slug}/download/talent/${talent.id}`,
                {
                    params: token ? { token } : {},
                    headers: token ? { Authorization: `Bearer ${token}` } : {},
                    responseType: "blob",
                    timeout: 120000, // C5: 2-minute ceiling for server-side ZIP assembly
                }
            );

            const blob = new Blob([response.data], { type: "application/zip" });
            const url = window.URL.createObjectURL(blob);
            const linkElement = document.createElement("a");
            linkElement.href = url;

            const cleanName = (talent.name || "Talent").trim().replace(/\./g, "").replace(/\s+/g, "_");
            linkElement.setAttribute("download", `${cleanName}_Package.zip`);
            document.body.appendChild(linkElement);
            linkElement.click();
            linkElement.remove();
            // Delay revoke so the browser can start the download (esp. iOS Safari).
            setTimeout(() => window.URL.revokeObjectURL(url), 1000);
            toast.success("Talent folder downloaded");
        } catch (err) {
            console.error("Error downloading package:", err);
            // C4: friendly, non-technical feedback via the app's toast system.
            let message = "Couldn't prepare the talent folder. Please try again in a moment.";
            if (err.code === "ECONNABORTED") {
                message = "The download timed out. Please check your connection and try again.";
            } else if (err.response?.data instanceof Blob) {
                try {
                    const detail = JSON.parse(await err.response.data.text())?.detail;
                    if (typeof detail === "string" && detail.length < 200) message = detail;
                } catch (_) { /* keep the generic message */ }
            } else if (typeof err.response?.data?.detail === "string") {
                message = err.response.data.detail;
            }
            toast.error(message);
        } finally {
            downloadPackageInFlightRef.current = false;
            setIsDownloadingPackage(false);
        }
    };
    const list = useMemo(() => (
        Array.isArray(talents) ? talents : []
    ), [talents]);

    const nextUnreviewed = useMemo(() => {
        const idx = list.findIndex((t) => t.id === talent.id);
        if (idx === -1) return null;
        for (let i = idx + 1; i < list.length; i++) {
            const t = list[i];
            const isRev = (reviewedIds && reviewedIds.has(t.id)) || (viewerActions && !!viewerActions[t.id]?.action);
            if (!isRev) return t;
        }
        for (let i = 0; i < idx; i++) {
            const t = list[i];
            const isRev = (reviewedIds && reviewedIds.has(t.id)) || (viewerActions && !!viewerActions[t.id]?.action);
            if (!isRev) return t;
        }
        return null;
    }, [talent.id, list, reviewedIds, viewerActions]);

    const currentTalentIdx = list.findIndex((t) => t.id === talent.id);
    const hasPrevTalent = currentTalentIdx > 0;
    const hasNextTalent = currentTalentIdx >= 0 && currentTalentIdx < list.length - 1;

    const goPrevTalent = useCallback(() => {
        if (hasPrevTalent && onNavigate) {
            onNavigate(list[currentTalentIdx - 1]);
        }
    }, [hasPrevTalent, onNavigate, list, currentTalentIdx]);

    const goNextTalent = useCallback(() => {
        if (hasNextTalent && onNavigate) {
            onNavigate(list[currentTalentIdx + 1]);
        } else {
            onClose();
        }
    }, [hasNextTalent, onNavigate, list, currentTalentIdx, onClose]);

    /**
     * Mirrors viewerAction in a ref so the keyboard handler can read the latest
     * action without being listed as a dependency (which caused handler re-registration
     * and a brief keyboard responsiveness gap on every action button tap).
     */
    const viewerActionRef = useRef(viewerAction);

    useEffect(() => {
        viewerActionRef.current = viewerAction;
    }, [viewerAction]);

    const trackedMediaRefs = useRef(new Set());

    const trackMediaView = useCallback((mediaId) => {
        if (!mediaId || trackedMediaRefs.current.has(mediaId)) return;
        trackedMediaRefs.current.add(mediaId);
        
        let sid = sessionStorage.getItem("client_session_id") || "guest-session";
        axios.post(
            `${API}/public/links/${slug}/track`,
            {
                event_type: "view_media",
                session_id: sid,
                media_id: mediaId,
                talent_id: talent.id
            }
        ).catch(() => {});
    }, [slug, talent.id]);

    useEffect(() => {
        setIdx(0);
        setIsDetailsExpanded(true); // Issue 2: details expanded by default on every talent open
        trackedMediaRefs.current.clear();
    }, [talent.id]);

    useEffect(() => {
        if (!talent?.id || isReviewed) return;
        const timer = setTimeout(() => {
            onMarkReviewed(talent.id);
        }, 15000);
        return () => clearTimeout(timer);
    }, [talent?.id, isReviewed, onMarkReviewed]);

    const prev = useCallback(() => {
        setIdx((i) => {
            const nextIdx = (i - 1 + images.length) % images.length;
            if (images[nextIdx]) trackMediaView(images[nextIdx].id);
            return nextIdx;
        });
    }, [images, trackMediaView]);

    const next = useCallback(() => {
        setIdx((i) => {
            const nextIdx = (i + 1) % images.length;
            if (images[nextIdx]) trackMediaView(images[nextIdx].id);
            return nextIdx;
        });
    }, [images, trackMediaView]);

    // Effect 1: Body scroll lock — empty deps so it only runs on mount/unmount.
    // Previously combined with the keyboard effect whose deps included viewerAction?.action,
    // causing scroll to flicker on every action button tap (mobile jank).
    useEffect(() => {
        document.body.style.overflow = "hidden";
        setIsModalOpen(true);
        return () => {
            document.body.style.overflow = "";
        };
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    // Effect 2: Keyboard shortcuts — uses viewerActionRef instead of viewerAction
    // so this handler is NOT re-registered on every action change (eliminates the
    // momentary keyboard dead-zone after each Shortlist/Reject press).
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (e.key === "Escape" && onClose) {
                onClose();
                return;
            }
            if (isSharePreview) return;
            const current = viewerActionRef.current;
            if (e.key === "s" || e.key === "S") {
                setAction(talent.id, current?.action === "shortlist" ? null : "shortlist");
            } else if (e.key === "r" || e.key === "R") {
                setAction(talent.id, current?.action === "not_for_this" ? null : "not_for_this");
            } else if (e.key === "h" || e.key === "H") {
                setAction(talent.id, current?.action === "not_sure" ? null : "not_sure");
            } else if (e.key === "i" || e.key === "I") {
                setAction(talent.id, current?.action === "interested" ? null : "interested");
            } else if (e.key === "ArrowLeft" && goPrevTalent) {
                goPrevTalent();
            } else if (e.key === "ArrowRight" && goNextTalent) {
                goNextTalent();
            }
        };
        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [talent.id, setAction, onClose, goPrevTalent, goNextTalent, isSharePreview]);

    // Touch swipe gesture handlers for gallery navigation (mobile jank fix - AUDIT: HIGH-01)
    useEffect(() => {
        let startX = 0;
        let startY = 0;
        const node = overlayRef.current;
        if (!node) return;

        const onTouchStart = (e) => {
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
        };

        const onTouchMove = (e) => {
            // No-op to satisfy Chrome/Safari passive event checks
        };

        const onTouchEnd = (e) => {
            // Don't trigger swipe on touchable sub-components like carousels/sliders (AUDIT: HIGH-01)
            if (e.target.closest("[data-stop-swipe]")) return;
            const diffX = e.changedTouches[0].clientX - startX;
            const diffY = e.changedTouches[0].clientY - startY;

            if (Math.abs(diffX) > 60 && Math.abs(diffY) < 40) {
                if (diffX > 0 && goPrevTalent) {
                    goPrevTalent();
                } else if (diffX < 0 && goNextTalent) {
                    goNextTalent();
                }
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
    }, [goNextTalent, goPrevTalent]);


    const download = useCallback((m) => {
        if (!m || !m.id) return;
        if (downloadingRef.current.has(m.id)) return; // guard rapid double-taps
        try {
            const rawUrl = IMAGE_URL(m);
            const isVideo = m.resource_type === "video" || m.category === "video" || m.category?.startsWith("take");
            const url = isVideo ? getVideoDownloadUrl(rawUrl) : rawUrl;
            if (!url) {
                toast.error("This file isn't available to download.");
                return;
            }

            let baseName = m.original_filename || `${privatizeName(talent.name)}_${m.category || "media"}`;
            if (baseName.includes(".")) {
                baseName = baseName.replace(/\.[^/.]+$/, "");
            }

            // Build a directly-downloadable URL. For Cloudinary, use fl_attachment so
            // the server returns Content-Disposition: attachment (sets the filename
            // too). This is reliable and REPEATABLE on iOS Safari/Chrome because the
            // anchor click stays inside the user gesture — no fetch()/blob() (which
            // breaks the gesture and fails on the 2nd download in Safari).
            let downloadUrl = url;
            if (url.includes("/upload/")) {
                const cleanName = baseName.replace(/[^a-zA-Z0-9_-]/g, "_");
                const flag = cleanName ? `fl_attachment:${cleanName}` : "fl_attachment";
                downloadUrl = url.replace("/upload/", `/upload/${flag}/`);
            }

            const a = document.createElement("a");
            a.href = downloadUrl;
            a.download = baseName; // honored same-origin / Cloudinary; ignored cross-origin (server name used)
            a.rel = "noopener";
            document.body.appendChild(a);
            a.click();
            a.remove();

            // Fire-and-forget analytics — must NOT precede/await the click (would
            // break the Safari user-gesture and block the download).
            logDownload(talent.id, m.id);
        } catch (err) {
            console.error("Download error:", err);
            toast.error("Couldn't download this file. Please try again.");
            return;
        }

        // Brief visual guard (native download runs in the browser's download
        // manager; there's no async completion to await).
        downloadingRef.current.add(m.id);
        setDownloadingIds((prev) => new Set(prev).add(m.id));
        setTimeout(() => {
            downloadingRef.current.delete(m.id);
            setDownloadingIds((prev) => {
                const n = new Set(prev);
                n.delete(m.id);
                return n;
            });
        }, 1500);
    }, [logDownload, talent.id, talent.name]);

    return (
        <div
            ref={overlayRef}
            className={`fixed inset-0 z-50 bg-white overflow-hidden transition-opacity duration-300 ease-out ${isModalOpen ? "opacity-100" : "opacity-0"}`}
            data-testid="talent-detail-overlay"
        >
            <div className={`h-[100dvh] flex flex-col transition-transform duration-300 ease-out ${isModalOpen ? "scale-100" : "scale-95"}`}>

                {/* Unified Top Sticky Header (Desktop & Mobile) */}
                <div className="sticky top-0 z-50 bg-white border-b border-[#eaeaea] px-4 md:px-6 py-3 md:py-4 flex flex-wrap items-center justify-between shrink-0 shadow-sm">
                    <div className="min-w-0 flex-1 pr-4 flex items-center gap-3">
                        <h2 className="font-display text-base md:text-lg font-bold text-[#111111] truncate">
                            {privatizeName(talent.name)}
                        </h2>
                        <span className={`hidden md:inline-flex items-center text-[10px] px-2.5 py-1 rounded-full border font-mono uppercase tracking-wider ${
                            viewerAction?.action === "shortlist"
                                ? "bg-[var(--tg-navy-light)] text-[var(--tg-navy-primary)] border-[var(--tg-navy-border)]"
                                : viewerAction?.action === "interested"
                                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                : viewerAction?.action === "not_for_this"
                                ? "bg-rose-50 text-rose-700 border-rose-200"
                                : viewerAction?.action === "not_sure"
                                ? "bg-slate-100 text-[#4A4A4A] border-slate-200"
                                : "bg-slate-50 text-[#333333] border-[#eaeaea]"
                        }`}>
                            {{
                                shortlist: "Shortlist",
                                interested: "Interested",
                                not_for_this: "Reject",
                                not_sure: "Hold",
                            }[viewerAction?.action] || "Pending"}
                        </span>
                    </div>
                    
                    <div className="flex items-center gap-2 md:gap-3 shrink-0">
                        {/* Mobile action indicator */}
                        <span className={`md:hidden inline-flex items-center text-[9px] px-2 py-0.5 rounded-full border font-mono uppercase tracking-wider ${
                            viewerAction?.action === "shortlist"
                                ? "bg-[var(--tg-navy-light)] text-[var(--tg-navy-primary)] border-[var(--tg-navy-border)]"
                                : viewerAction?.action === "interested"
                                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                : viewerAction?.action === "not_for_this"
                                ? "bg-rose-50 text-rose-700 border-rose-200"
                                : viewerAction?.action === "not_sure"
                                ? "bg-slate-100 text-[#4A4A4A] border-slate-200"
                                : "bg-slate-50 text-[#333333] border-[#eaeaea]"
                        }`}>
                            {{
                                shortlist: "Shortlist",
                                interested: "Interested",
                                not_for_this: "Reject",
                                not_sure: "Hold",
                            }[viewerAction?.action] || "Pending"}
                        </span>

                        {vis.download && !isSharePreview && (
                            <button
                                onClick={handleDownloadPackage}
                                disabled={isDownloadingPackage}
                                className="hidden md:flex h-9 md:h-10 px-3 md:px-4 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full items-center gap-2 transition-colors duration-150 shadow-sm text-xs font-semibold text-[#111111] disabled:opacity-50 disabled:cursor-not-allowed"
                                title="Download Talent Folder"
                                data-testid="header-download-package-btn"
                            >
                                {isDownloadingPackage ? (
                                    <>
                                        <Loader2 className="w-3.5 h-3.5 animate-spin text-[#333333]" />
                                        <span className="hidden lg:inline">Preparing...</span>
                                    </>
                                ) : (
                                    <>
                                        <Download className="w-3.5 h-3.5 text-[#333333]" />
                                        <span className="hidden lg:inline">Download Folder</span>
                                    </>
                                )}
                            </button>
                        )}
                        <button
                            onClick={handleCopyForm}
                            className="hidden md:flex h-9 md:h-10 px-3 md:px-4 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full items-center gap-2 transition-colors duration-150 shadow-sm text-xs font-semibold text-[#111111]"
                            data-testid="header-copy-form-btn"
                        >
                            <Copy className="w-3.5 h-3.5 text-[#333333]" />
                            <span className="hidden lg:inline">Copy Form</span>
                        </button>
                        {!isSharePreview && (
                            <button
                                onClick={() => onShare(talent.id)}
                                className="hidden md:flex w-9 h-9 md:w-10 md:h-10 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full items-center justify-center transition-colors duration-150 shadow-sm"
                                title="Share Portfolio"
                                data-testid="header-share-btn"
                            >
                                <Share2 className="w-4 h-4 text-[#333333]" />
                            </button>
                        )}
                        {!isSharePreview && (
                            <button
                                onClick={onClose}
                                className="w-8 h-8 md:w-10 md:h-10 bg-slate-100 hover:bg-slate-200 rounded-full flex items-center justify-center transition-colors duration-150 ml-1 md:ml-0"
                                data-testid="header-close-btn"
                            >
                                <X className="w-4 h-4 md:w-5 md:h-5 text-[#111111]" />
                            </button>
                        )}
                    </div>
                </div>

                <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
                    {/* Left Column - Image */}
                    {/* min-h-0: required for iOS Safari — flex children without min-h-0 fail to scroll */}
                    <div className="w-full md:w-[58%] lg:w-[60%] bg-white overflow-y-auto min-h-0 pb-10">
                        {/* Mobile Download Talent Folder Button */}
                        {vis.download && (
                            <div className="md:hidden px-4 py-3 bg-white border-b border-black/[0.04]">
                                <button
                                    onClick={handleDownloadPackage}
                                    disabled={isDownloadingPackage}
                                    className="w-full h-11 bg-[#1A1A1A] hover:bg-[#111111] disabled:bg-black/40 disabled:cursor-not-allowed text-white rounded-xl flex items-center justify-center gap-2 text-xs font-semibold tracking-wider transition-all duration-150 shadow-sm"
                                    data-testid="detail-download-package-btn-mobile"
                                >
                                    {isDownloadingPackage ? (
                                        <>
                                            <Loader2 className="w-4 h-4 animate-spin text-white" />
                                            <span>Preparing Talent Folder...</span>
                                        </>
                                    ) : (
                                        <>
                                            <Download className="w-4 h-4 text-white" />
                                            <span>Download Talent Folder</span>
                                        </>
                                    )}
                                </button>
                            </div>
                        )}
                        {/* Mobile Details Accordion */}
                        <div className="md:hidden border-b border-black/[0.04] bg-white">
                            <div className="flex items-center justify-between px-4 py-2 border-b border-black/[0.02]">
                                <button
                                    type="button"
                                    onClick={() => setIsDetailsExpanded(!isDetailsExpanded)}
                                    className="flex-1 text-left py-2.5 text-sm font-medium text-black/70 flex items-center justify-between"
                                >
                                    <span>Talent Details Form</span>
                                    <ChevronDown className={`w-4 h-4 text-black/40 transition-transform duration-200 ${isDetailsExpanded ? "rotate-180" : ""}`} />
                                </button>
                                <button
                                    onClick={handleCopyForm}
                                    className="ml-3 text-[11px] px-2.5 py-1.5 border border-[#eaeaea] hover:border-black/20 rounded-md transition-colors text-[#111111] bg-white font-medium shadow-sm flex items-center justify-center shrink-0"
                                    data-testid="copy-form-btn-mobile"
                                >
                                    Copy Form
                                </button>
                            </div>

                            {isDetailsExpanded && (
                                <div className="px-4 pb-6 pt-2 bg-white space-y-6 text-sm">
                                    <div className="grid grid-cols-2 gap-y-5">
                                        {vis.age && talent.age && (
                                            <InfoRow label="Age" value={talent.age} />
                                        )}
                                        {vis.height && talent.height && (
                                            <InfoRow label="Height" value={talent.height} />
                                        )}
                                        {vis.location && talent.location && (
                                            <InfoRow 
                                                label="Location" 
                                                value={formatTalentLocation(talent.location)} 
                                            />
                                        )}
                                        {vis.ethnicity && talent.ethnicity && (
                                            <InfoRow label="Ethnicity" value={talent.ethnicity} />
                                        )}
                                        {vis.instagram_followers && talent.instagram_followers && (
                                            <InfoRow label="Followers" value={talent.instagram_followers} />
                                        )}
                                        {talent.gender && (
                                            <InfoRow label="Gender" value={talent.gender} />
                                        )}
                                        {talent.languages && (Array.isArray(talent.languages) ? talent.languages.length > 0 : talent.languages) && (
                                            <InfoRow label="Languages" value={Array.isArray(talent.languages) ? talent.languages.join(", ") : talent.languages} />
                                        )}
                                        {talent.skills && (Array.isArray(talent.skills) ? talent.skills.length > 0 : talent.skills) && (
                                            <InfoRow label="Skills" value={Array.isArray(talent.skills) ? talent.skills.join(", ") : talent.skills} />
                                        )}
                                        {talent.special_abilities && (
                                            <InfoRow label="Special Abilities" value={talent.special_abilities} />
                                        )}
                                    </div>
                                    
                                    <AvailabilityBudgetSection 
                                        talent={talent}
                                        projectShootDates={projectShootDates}
                                        projectBudget={projectBudget}
                                        vis={vis}
                                    />

                                    {(talent.custom_answers || []).length > 0 && (
                                        <div className="bg-white p-5 space-y-3 rounded-xl border border-black/[0.04] shadow-sm">
                                            <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">CUSTOM QUESTIONS</p>
                                            {talent.custom_answers.map((qa, i) => (
                                                <div key={`${qa.question}-${i}`} data-testid={`custom-qa-mobile-${i}`}>
                                                    <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-1">{qa.question}</p>
                                                    <p className="text-sm text-[#111111] whitespace-pre-wrap leading-relaxed">{qa.answer}</p>
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    <div className="flex gap-3 flex-wrap">
                                        {vis.instagram && talent.instagram_handle && (
                                            <a href={instagramProfileUrl(talent.instagram_handle)} target="_blank" rel="noopener noreferrer" data-testid="client-instagram-link-mobile" className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.06] hover:border-black/20 rounded-full text-xs transition-colors duration-150 text-[#111111] bg-white/50">
                                                <Instagram className="w-3.5 h-3.5" /> {displayInstagramHandle(talent.instagram_handle)}
                                            </a>
                                        )}
                                    </div>

                                    {vis.work_links && (talent.work_links || []).length > 0 && (
                                        <div>
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Work Links</p>
                                            <WorkLinksDisplay links={talent.work_links} variant="list" />
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                        <div className="p-4 md:p-8">
                            {vis.takes !== false && takes.length > 0 && (
                                <div className="mb-10">
                                    <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Audition Takes</p>
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                        {takes.map((t, i) => (
                                            <div key={t.id} data-testid={`client-take-${i}`}>
                                                <p className="text-[11px] text-[#8A8A8A] mb-2 font-mono tracking-[0.08em] truncate flex items-center justify-between">
                                                    <span className="flex items-center gap-1.5 min-w-0">
                                                        <span className="truncate">{t.label || `Take ${i + 1}`}</span>
                                                        {!isSharePreview && (
                                                            <button
                                                                onClick={() => onShare(talent.id, t.id)}
                                                                className="p-1 hover:bg-black/5 rounded text-[#333333] hover:text-[#111111] transition-colors shrink-0"
                                                                title="Share this take only"
                                                            >
                                                                <Share2 className="w-3 h-3" />
                                                            </button>
                                                        )}
                                                    </span>
                                                    {t.primary_take && (
                                                        <span className="text-[8px] bg-[var(--tg-navy-primary)] text-white px-1.5 py-0.5 rounded font-bold uppercase tracking-wider shrink-0">
                                                            ★ Primary
                                                        </span>
                                                    )}
                                                </p>
                                                <div className="relative">
                                                    <LazyVideoPlayer
                                                        src={t.url}
                                                        poster={posterUrl(t)}
                                                        label={t.label || `Take ${i + 1}`}
                                                        className="w-full"
                                                        mediaId={t.id}
                                                        slug={slug}
                                                        talentId={talent.id}
                                                    />
                                                    {vis.download && (
                                                        <button
                                                            onClick={() => download(t)}
                                                            disabled={downloadingIds.has(t.id)}
                                                            className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm z-10 disabled:opacity-50 disabled:cursor-not-allowed"
                                                            data-testid={`download-take-btn-${i}`}
                                                        >
                                                            {downloadingIds.has(t.id) ? <Loader2 className="w-4 h-4 text-black animate-spin" /> : <Download className="w-4 h-4 text-black" />}
                                                        </button>
                                                    )}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                             )}
 
                             {vis.intro_video && intro && (
                                 <div className="mb-10">
                                     <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Introduction</p>
                                     <div className="relative">
                                         <LazyVideoPlayer
                                             src={intro.url}
                                             poster={posterUrl(intro)}
                                             label="Introduction Video"
                                             className="w-full"
                                             mediaId={intro.id}
                                             slug={slug}
                                             talentId={talent.id}
                                         />
                                         {vis.download && (
                                             <button
                                                 onClick={() => download(intro)}
                                                 disabled={downloadingIds.has(intro.id)}
                                                 className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm z-10 disabled:opacity-50 disabled:cursor-not-allowed"
                                                 data-testid="download-intro-btn"
                                             >
                                                 {downloadingIds.has(intro.id) ? <Loader2 className="w-4 h-4 text-black animate-spin" /> : <Download className="w-4 h-4 text-black" />}
                                             </button>
                                         )}
                                     </div>
                                 </div>
                             )}

                            {images.length > 0 && (
                                <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Portfolio</p>
                            )}
                            {images.length > 0 ? (
                                <div className="relative bg-white rounded-xl overflow-hidden shadow-sm">
                                    <div className="aspect-[3/4] md:max-h-[78vh] mx-auto">
                                        <img
                                            src={IMAGE_URL(images[idx])}
                                            alt={privatizeName(talent.name)}
                                            className="w-full h-full object-contain cursor-pointer"
                                            onClick={() => trackMediaView(images[idx].id)}
                                        />
                                    </div>
                                    {images.length > 1 && (
                                        <>
                                            <button
                                                onClick={prev}
                                                className="absolute left-3 top-1/2 -translate-y-1/2 w-10 h-10 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm"
                                            >
                                                <ChevronLeft className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={next}
                                                className="absolute right-3 top-1/2 -translate-y-1/2 w-10 h-10 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm"
                                            >
                                                <ChevronRight className="w-4 h-4" />
                                            </button>
                                            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 px-3 py-1.5 bg-white/90 border border-black/[0.04] text-[10px] font-mono tracking-[0.08em] rounded-full text-[#5C5C5C] shadow-sm">
                                                {idx + 1} / {images.length}
                                            </div>
                                        </>
                                    )}
                                    {vis.download && (
                                        <button
                                            onClick={() => download(images[idx])}
                                            disabled={downloadingIds.has(images[idx]?.id)}
                                            className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
                                            data-testid="detail-download-btn"
                                        >
                                            {downloadingIds.has(images[idx]?.id) ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                                        </button>
                                    )}
                                </div>
                            ) : (
                                <div className="aspect-[3/4] md:max-h-[78vh] bg-white rounded-xl flex items-center justify-center text-[#8A8A8A] shadow-sm">
                                    No portfolio
                                </div>
                            )}

                            {images.length > 1 && (
                                <div className="mt-7 flex gap-3 overflow-x-auto pb-3" data-stop-swipe="1">
                                    {images.map((m, i) => (
                                        <button
                                            key={m.id}
                                            onClick={() => {
                                                setIdx(i);
                                                trackMediaView(m.id);
                                            }}
                                            className={`shrink-0 w-20 h-28 border-2 ${i === idx ? "border-[var(--tg-navy-primary)]" : "border-black/[0.04]"} rounded-xl overflow-hidden transition-colors duration-150`}
                                        >
                                            <img
                                                src={thumbnailUrl(m)}
                                                alt=""
                                                className="w-full h-full object-cover"
                                            />
                                        </button>
                                    ))}
                                </div>
                            )}

                            {/* Mobile-only Decisions, Comments and Feedback */}
                            {!isSharePreview && (
                                <div className="md:hidden border-t border-black/[0.06] pt-6 mt-6 space-y-6">
                                    <div>
                                        <div className="flex items-center justify-between mb-4">
                                            <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Your Decision</p>
                                            <button
                                                onClick={() => onMarkReviewed(talent.id)}
                                                disabled={isReviewed}
                                                className={`flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] border rounded-full text-xs font-medium transition-colors duration-150 ${
                                                    isReviewed
                                                        ? "bg-[#E6F4EA] text-[#137333] border-[#E6F4EA]"
                                                        : "border-[#eaeaea] hover:border-black/20 text-[#4A4A4A]"
                                                }`}
                                                data-testid="mark-reviewed-btn-mobile"
                                            >
                                                <Check className="w-3.5 h-3.5" />
                                                {isReviewed ? "Reviewed" : "Mark Reviewed"}
                                            </button>
                                        </div>
                                        <div className="grid grid-cols-2 gap-2 mb-6">
                                            {visibleActions.map((a) => {
                                                const active = viewerAction?.action === a.key;
                                                return (
                                                    <button key={a.key} onClick={() => setAction(talent.id, active ? null : a.key)} data-testid={`action-${a.key}-${talent.id}-mobile`} className={`flex items-center justify-center gap-2 px-4 py-2.5 min-h-[44px] border rounded-lg text-xs font-medium tracking-wide transition-all duration-150 ${active ? "border-black text-black bg-black/[0.04] font-semibold" : "border-[#eaeaea] hover:border-black/20 text-black/60 hover:text-black bg-transparent"}`}>
                                                        <a.icon className={`w-3.5 h-3.5 ${active ? "opacity-90" : "opacity-40"}`} />
                                                        {a.label}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    </div>

                                    <div>
                                        <div className="flex items-center gap-2 mb-2">
                                            <MessageSquare className="w-3.5 h-3.5 text-[#8A8A8A]" />
                                            <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Comment</p>
                                        </div>
                                        <textarea value={commentDraft} onChange={(e) => setCommentDraft(e.target.value)} rows={3} placeholder="Share any notes about this talent..." data-testid="detail-comment-input-mobile" className="w-full bg-transparent border border-[#eaeaea] focus:border-black/25 rounded-xl p-3 text-sm outline-none transition-colors duration-150 text-[#111111] placeholder:text-black/30" />
                                        <button onClick={saveComment} data-testid="detail-save-comment-btn-mobile" className="mt-3 inline-flex items-center min-h-[44px] text-xs px-4 py-2 border border-[#eaeaea] hover:border-black/25 rounded-full transition-colors duration-150 text-[#4A4A4A] hover:text-[#111111]">Save comment</button>
                                        {renderCommentsHistory()}
                                    </div>

                                    {talent.submission_id && talent.project_id && (
                                        <div className="pt-6 border-t border-black/[0.06]">
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Voice Note Feedback</p>
                                            <VoiceRecorder
                                                onSend={(blob) => saveVoiceNote(talent.id, blob)}
                                                sending={sendingVoice}
                                            />
                                            {renderVoiceHistory()}
                                        </div>
                                    )}

                                    {nextUnreviewed && (
                                        <div className="pt-6 border-t border-black/[0.06]">
                                            <button
                                                onClick={() => onNavigate(nextUnreviewed)}
                                                className="w-full flex items-center justify-center gap-2 px-4 py-3.5 bg-[#1A1A1A] hover:bg-[#2A2A2A] text-white rounded-xl text-sm font-medium transition-colors duration-150 shadow-sm"
                                                data-testid="next-unreviewed-btn-mobile"
                                            >
                                                Next Unreviewed Talent →
                                            </button>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>

                    {/* Right Column - Details (scrollable with soft shadow) */}
                    {/* min-h-0: required for iOS Safari scroll fix in flex context */}
                    <div className="hidden md:block w-full md:w-[42%] lg:w-[40%] bg-white overflow-y-visible md:overflow-y-auto shadow-[-10px_0_30px_-20px_rgba(0,0,0,0.08)] min-h-0">
                        {/* pb-[130px] gives clearance above the fixed mobile bottom action bar + home indicator */}
                        <div className="p-6 md:p-8 pt-20 md:pt-20 pb-[130px] md:pb-8">


                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Talent Details Form</p>
                            <h2 className="font-display text-3xl md:text-4xl tracking-wide mb-6 text-[#111111]">
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
                                    <InfoRow 
                                        label="Location" 
                                        value={formatTalentLocation(talent.location)} 
                                    />
                                )}
                                {vis.ethnicity && talent.ethnicity && (
                                    <InfoRow label="Ethnicity" value={talent.ethnicity} />
                                )}
                                {vis.instagram_followers && talent.instagram_followers && (
                                    <InfoRow label="Followers" value={talent.instagram_followers} />
                                )}
                                {talent.gender && (
                                    <InfoRow label="Gender" value={talent.gender} />
                                )}
                                {talent.languages && (Array.isArray(talent.languages) ? talent.languages.length > 0 : talent.languages) && (
                                    <InfoRow label="Languages" value={Array.isArray(talent.languages) ? talent.languages.join(", ") : talent.languages} />
                                )}
                                {talent.skills && (Array.isArray(talent.skills) ? talent.skills.length > 0 : talent.skills) && (
                                    <InfoRow label="Skills" value={Array.isArray(talent.skills) ? talent.skills.join(", ") : talent.skills} />
                                )}
                                {talent.special_abilities && (
                                    <InfoRow label="Special Abilities" value={talent.special_abilities} />
                                )}
                            </div>

                            <AvailabilityBudgetSection 
                                talent={talent}
                                projectShootDates={projectShootDates}
                                projectBudget={projectBudget}
                                vis={vis}
                            />

                            {(talent.custom_answers || []).length > 0 && (
                                <div className="mb-8 bg-white p-5 space-y-3 rounded-xl shadow-sm">
                                    <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">CUSTOM QUESTIONS</p>
                                    {talent.custom_answers.map((qa, i) => (
                                        <div key={`${qa.question}-${i}`} data-testid={`custom-qa-${i}`}>
                                            <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-1">{qa.question}</p>
                                            <p className="text-sm text-[#111111] whitespace-pre-wrap leading-relaxed">{qa.answer}</p>
                                        </div>
                                    ))}
                                </div>
                            )}

                            <div className="flex gap-3 mb-8 flex-wrap">
                                {vis.instagram && talent.instagram_handle && (
                                    <a href={instagramProfileUrl(talent.instagram_handle)} target="_blank" rel="noopener noreferrer" data-testid="client-instagram-link" className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.06] hover:border-black/20 rounded-full text-xs transition-colors duration-150 text-[#111111] bg-white/50">
                                        <Instagram className="w-3.5 h-3.5" /> {displayInstagramHandle(talent.instagram_handle)}
                                    </a>
                                )}
                            </div>

                            {vis.work_links && (talent.work_links || []).length > 0 && (
                                <div className="mb-8">
                                    <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Work Links</p>
                                    <WorkLinksDisplay links={talent.work_links} variant="list" />
                                </div>
                            )}

                            {!isSharePreview && (
                                <div className="border-t border-black/[0.06] pt-6 mt-6">
                                    <div className="flex items-center justify-between mb-4">
                                        <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Your Decision</p>
                                        <button
                                            onClick={() => onMarkReviewed(talent.id)}
                                            disabled={isReviewed}
                                            className={`flex items-center gap-1.5 px-3 py-1.5 min-h-[44px] border rounded-full text-xs font-medium transition-colors duration-150 ${
                                                isReviewed
                                                    ? "bg-[#E6F4EA] text-[#137333] border-[#E6F4EA]"
                                                    : "border-[#eaeaea] hover:border-black/20 text-[#4A4A4A]"
                                            }`}
                                            data-testid="mark-reviewed-btn"
                                        >
                                            <Check className="w-3.5 h-3.5" />
                                            {isReviewed ? "Reviewed" : "Mark Reviewed"}
                                        </button>
                                    </div>
                                    <div className="grid grid-cols-2 gap-2 mb-6">
                                        {visibleActions.map((a) => {
                                            const active = viewerAction?.action === a.key;
                                            return (
                                                <button key={a.key} onClick={() => setAction(talent.id, active ? null : a.key)} data-testid={`action-${a.key}-${talent.id}`} className={`flex items-center justify-center gap-2 px-4 py-2.5 min-h-[44px] border rounded-lg text-xs font-medium tracking-wide transition-all duration-150 ${active ? "border-black text-black bg-black/[0.04] font-semibold" : "border-[#eaeaea] hover:border-black/20 text-black/60 hover:text-black bg-transparent"}`}>
                                                    <a.icon className={`w-3.5 h-3.5 ${active ? "opacity-90" : "opacity-40"}`} />
                                                    {a.label}
                                                </button>
                                            );
                                        })}
                                    </div>

                                    <div>
                                        <div className="flex items-center gap-2 mb-2">
                                            <MessageSquare className="w-3.5 h-3.5 text-[#8A8A8A]" />
                                            <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Comment</p>
                                        </div>
                                        <textarea value={commentDraft} onChange={(e) => setCommentDraft(e.target.value)} rows={3} placeholder="Share any notes about this talent..." data-testid="detail-comment-input" className="w-full bg-transparent border border-[#eaeaea] focus:border-black/25 rounded-xl p-3 text-sm outline-none transition-colors duration-150 text-[#111111] placeholder:text-black/30" />
                                        <button onClick={saveComment} data-testid="detail-save-comment-btn" className="mt-3 inline-flex items-center min-h-[44px] text-xs px-4 py-2 border border-[#eaeaea] hover:border-black/25 rounded-full transition-colors duration-150 text-[#4A4A4A] hover:text-[#111111]">Save comment</button>
                                        {renderCommentsHistory()}
                                    </div>

                                    {talent.submission_id && talent.project_id && (
                                        <div className="mt-6 pt-6 border-t border-black/[0.06]">
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Voice Note Feedback</p>
                                            <VoiceRecorder
                                                onSend={(blob) => saveVoiceNote(talent.id, blob)}
                                                sending={sendingVoice}
                                            />
                                            {renderVoiceHistory()}
                                        </div>
                                    )}
                                </div>
                            )}

                            {isSharePreview && (
                                <div className="mt-8 p-5 bg-white border border-black/[0.04] rounded-2xl flex flex-col items-center text-center">
                                    <span className="inline-block px-2.5 py-1 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[var(--tg-navy-badge-bg)] text-[var(--tg-navy-primary)] font-medium mb-3">
                                        Shared Audition Showcase
                                    </span>
                                    <p className="text-xs text-[#333333] max-w-xs leading-relaxed">
                                        This portfolio link was shared securely and stays active for as long as the review link is live.
                                    </p>
                                </div>
                            )}

                            {nextUnreviewed && !isSharePreview && (
                                <div className="mt-8 pt-6 border-t border-black/[0.06]">
                                    <button
                                        onClick={() => onNavigate(nextUnreviewed)}
                                        className="w-full flex items-center justify-center gap-2 px-4 py-3.5 bg-[#1A1A1A] hover:bg-[#2A2A2A] text-white rounded-xl text-sm font-medium transition-colors duration-150 shadow-sm"
                                        data-testid="next-unreviewed-btn"
                                    >
                                        Next Unreviewed Talent →
                                    </button>
                                </div>
                            )}       </div>
                        </div>
                    </div>
                </div>

        </div>
    );
}

/**
 * TalentCard — wrapped in React.memo so it only re-renders when its own props change.
 *
 * Key stabilization changes:
 * - Accepts `slug`, `setActiveTalent`, `markSeen` as stable props instead of inline
 *   arrow functions. This prevents 100+ new function objects per parent render.
 * - `handleOpen` is computed inside the card via useCallback with stable deps.
 * - IntersectionObserver deps are now [seen, talent.id, markSeen] — all stable —
 *   eliminating the observer disconnect/reconnect churn on every parent re-render.
 * - `transition-all` on cover image replaced with `transition-transform` (cheaper).
 */
const TalentCard = React.memo(function TalentCard({ talent, vis, action, seen, isNew, slug, setActiveTalent, markSeen, selected, onSelect }) {
    const ref = useRef(null);
    const timerRef = useRef(null);

    const cover = resolveTalentCover(talent);
    const isShortlisted = action === "shortlist";

    // Stable open handler — computed inside the card so the parent doesn't need
    // to create a new inline arrow function per render per card.
    const handleOpen = useCallback(() => {
        setActiveTalent(talent);
        markSeen(talent.id);
        try {
            localStorage.setItem(`tg_last_viewed_${slug}`, talent.id);
        } catch (e) { console.error(e); }
    }, [talent, slug, setActiveTalent, markSeen]);

    // Stabilized deps: [seen, talent.id, markSeen] are all stable references.
    // Previously [seen, onSeen] where onSeen was a new inline arrow per render,
    // causing all 100 observers to disconnect and reconnect on every state change.
    useEffect(() => {
        if (seen || !ref.current) return;
        const node = ref.current;
        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
                        if (!timerRef.current) {
                            timerRef.current = setTimeout(() => {
                                markSeen(talent.id);
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
    }, [seen, talent.id, markSeen]);

    return (
        <div className="relative group/card">
            {/* Absolute Checkbox overlay positioned at top-right or top-left, let's put it at top-left to avoid seen badge overlapping */}
            <div className="absolute top-1 right-1 z-30">
                {/* 44px hit area (label) wraps a ~20px visual box for reliable touch selection */}
                <label
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`Select ${privatizeName(talent.name)}`}
                    className="flex items-center justify-center w-11 h-11 cursor-pointer"
                >
                    <input
                        type="checkbox"
                        checked={selected}
                        onChange={(e) => {
                            e.stopPropagation();
                            onSelect(talent.id, e.target.checked);
                        }}
                        data-testid={`select-checkbox-${talent.id}`}
                        className="w-5 h-5 rounded border-black/20 text-[var(--tg-navy-primary)] focus:ring-[var(--tg-navy-primary)] cursor-pointer accent-[var(--tg-navy-primary)] shadow-sm"
                    />
                </label>
            </div>
            <button
                ref={ref}
                onClick={handleOpen}
                data-testid={`client-talent-${talent.id}`}
                data-seen={seen ? "true" : "false"}
                data-new={isNew ? "true" : "false"}
                className="w-full text-left transition-all duration-300 ease-out hover:-translate-y-0.5"
            >
            <div className="aspect-[3/4] bg-white overflow-hidden rounded-2xl group-hover:shadow-[0_8px_30px_rgb(0,0,0,0.04)] transition-all duration-300 relative shadow-sm">
                {cover ? (
                    <img
                        src={thumbnailUrl(cover)}
                        alt={privatizeName(talent.name)}
                        loading="lazy"
                        className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500 ease-out"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-[#8A8A8A]">
                        <Sparkles className="w-8 h-8" />
                    </div>
                )}
                {seen && (
                    <div className="absolute inset-0 bg-white/40 pointer-events-none" />
                )}

                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-white via-white/90 to-transparent p-4">
                    {/* Mobile-only (<md): compact badges sit in the bottom gradient above
                        the name — never over the face. Desktop/tablet use the top-left stack. */}
                    {(isNew || isShortlisted || (action && action !== "shortlist") || seen) && (
                        <div className="flex md:hidden flex-wrap items-center gap-1 mb-1.5">
                            {isNew && (
                                <span
                                    className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-[var(--tg-navy-primary)] text-white text-[9px] tracking-[0.06em] uppercase rounded-full shadow-sm"
                                    data-testid={`badge-new-mobile-${talent.id}`}
                                >
                                    <Sparkles className="w-2.5 h-2.5" /> New
                                </span>
                            )}
                            {isShortlisted && (
                                <span
                                    className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-[var(--tg-navy-primary)] text-white text-[9px] tracking-[0.06em] uppercase rounded-full shadow-sm"
                                    data-testid={`badge-shortlisted-mobile-${talent.id}`}
                                >
                                    <Heart className="w-2.5 h-2.5 fill-current" /> Shortlisted
                                </span>
                            )}
                            {action && action !== "shortlist" && (() => {
                                const meta = SHORT_ACTION_META[action];
                                const ShortIcon = meta?.icon;
                                return (
                                    <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-white/95 text-[#111111] text-[9px] tracking-[0.06em] uppercase rounded-full border border-black/[0.06] shadow-sm">
                                        {ShortIcon && <ShortIcon className="w-2.5 h-2.5" />}
                                        {meta?.label || ACTIONS.find((a) => a.key === action)?.label}
                                    </span>
                                );
                            })()}
                            {seen && (
                                <span
                                    className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-white/95 border border-black/[0.06] text-[#8A8A8A] text-[9px] tracking-[0.06em] uppercase rounded-full shadow-sm"
                                    data-testid={`badge-seen-mobile-${talent.id}`}
                                >
                                    <Eye className="w-2.5 h-2.5" /> Viewed
                                </span>
                            )}
                        </div>
                    )}
                    <div
                        className="font-display text-lg md:text-xl tracking-wide text-[#111111]"
                        data-testid={`client-card-name-${talent.id}`}
                    >
                        {privatizeName(talent.name)}
                    </div>
                    <div className="text-[11px] text-[#8A8A8A] font-mono tracking-[0.08em] mt-1">
                        {vis.location && talent.location ? formatTalentLocation(talent.location) : ""}
                    </div>
                </div>

                {/* Desktop/tablet (md+): unchanged top-left badge stack. Hidden on mobile,
                    where badges move into the bottom gradient so the face stays clear. */}
                <div className="absolute top-2 left-2 hidden md:flex md:flex-col gap-1.5 items-start">
                    {isNew && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--tg-navy-primary)] text-white text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
                            data-testid={`badge-new-${talent.id}`}
                        >
                            <Sparkles className="w-3 h-3" />
                            New
                        </span>
                    )}
                    {isShortlisted && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--tg-navy-primary)] text-white text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
                            data-testid={`badge-shortlisted-${talent.id}`}
                        >
                            <Heart className="w-3 h-3 fill-current" />
                            Shortlisted
                        </span>
                    )}
                    {action && action !== "shortlist" && (
                        <span className="inline-flex items-center gap-1 px-2 py-1 bg-white/95 text-[#111111] text-[10px] tracking-[0.08em] uppercase rounded-full border border-black/[0.06] shadow-sm">
                            {ACTIONS.find((a) => a.key === action)?.label}
                        </span>
                    )}
                    {seen && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-white/95 border border-black/[0.06] text-[#8A8A8A] text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
                            data-testid={`badge-seen-${talent.id}`}
                        >
                            <Eye className="w-3 h-3" />
                            Viewed
                        </span>
                    )}
                </div>
            </div>
            </button>
        </div>
    );
});

function InfoRow({ label, value }) {
    return (
        <div>
            <div className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-1">
                {label}
            </div>
            <div className="text-sm font-medium text-[#111111]">{value}</div>
        </div>
    );
}
