import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import { IMAGE_URL, getViewerToken, saveViewerToken, PUBLIC_FRONTEND_URL, API } from "@/lib/api";
import LazyVideoPlayer from "@/components/LazyVideoPlayer";
import { thumbnailUrl, posterUrl, resolveTalentCover, displayInstagramHandle, instagramProfileUrl } from "@/lib/mediaUtils";
import Logo from "@/components/Logo";
import WorkLinksDisplay, { parseStoredWorkLink } from "@/components/WorkLinksDisplay";
import { api as axios } from "@/lib/api";
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
    { key: "ask_for_test", label: "Ask for Test", icon: ClipboardCheck },
    { key: "interested", label: "Audition Approved", icon: ThumbsUp },
    { key: "not_for_this", label: "Does Not Work For This Project", icon: XCircle },
    { key: "shortlist", label: "Shortlist", icon: Star },
    { key: "lock", label: "Lock", icon: Lock },
    { key: "not_sure", label: "Unsure", icon: HelpCircle },
];

const TABS = [
    { key: "pending_action", label: "Pending Action", icon: Clock },
    { key: "viewed", label: "Viewed", icon: Eye },
    { key: "ask_for_test", label: "Ask for Test", icon: ClipboardCheck },
    { key: "interested", label: "Audition Approved", icon: ThumbsUp },
    { key: "not_for_this", label: "Does Not Work For This Project", icon: XCircle },
    { key: "shortlist", label: "Shortlist", icon: Star },
    { key: "lock", label: "Lock", icon: Lock },
    { key: "not_sure", label: "Unsure", icon: HelpCircle },
];

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
                        const colorClass = lbl === "Available" ? "text-[#5A7D5A]" : lbl === "Not Available" ? "text-[#9E4A4A]" : "text-[#B89B5E]";
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
                                        <span className="font-semibold text-[#B89B5E] font-mono">{talent.budget.value}</span>
                                    </p>
                                    <p className="flex justify-between gap-4 mt-2">
                                        <span className="text-[#4A4A4A]">Status</span>
                                        <span className="inline-block px-2 py-0.5 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[#B89B5E]/8 text-[#B89B5E] font-medium">Counter-Offer</span>
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
    const queryParams = new URLSearchParams(window.location.search);
    const shareId = queryParams.get("share");

    const [shareData, setShareData] = useState(null);
    const [loadingShare, setLoadingShare] = useState(!!shareId);
    const [shareError, setShareError] = useState(null);
    const [sendingVoice, setSendingVoice] = useState(false);

    const [identified, setIdentified] = useState(!!getViewerToken(slug) || !!shareId);
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);

    const [savedReviewer, setSavedReviewer] = useState(() => {
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
    const [activeTab, setActiveTab] = useState("pending_action");
    const [showResumeBanner, setShowResumeBanner] = useState(false);

    // ── Stabilization refs ───────────────────────────────────────────────────
    /** Prevents double-submission on rapid taps on the identity gate. */
    const identifyInFlightRef = useRef(false);
    /** Deduplicates view_talent analytics — fires at most once per talent per session. */
    const trackedSeenRef = useRef(new Set());
    /** Deduplicates review_talent analytics — fires at most once per talent per session. */
    const trackedReviewedRef = useRef(new Set());
    /** Prevents stale state updates when loadData resolves after navigation away. */
    const loadDataMountedRef = useRef(true);

    // Depend specifically on data?.actions — not the whole data object — so this memo
    // does not recompute when unrelated fields (seen_ids, etc.) are updated.
    const viewerActions = useMemo(() => {
        const m = {};
        (data?.actions || []).forEach((a) => (m[a.talent_id] = a));
        return m;
    }, [data?.actions]);

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
                    project_budget: [],
                    project_shoot_dates: [],
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

    useEffect(() => {
        const prev = document.title;
        const brand = (data?.link?.brand_name || data?.link?.title || "").trim();
        document.title = brand ? `Talentgram | ${brand}` : "Talentgram | Portfolio";
        return () => {
            document.title = prev;
        };
    }, [data?.link?.brand_name, data?.link?.title]);

    const identify = async (e, optName, optEmail) => {
        if (e) e.preventDefault();
        const activeName = optName || name;
        const activeEmail = optEmail || email;
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

    const saveComment = useCallback(async (talentId) => {
        const text = commentDrafts[talentId];
        if (text === undefined) return;
        const existing = viewerActions[talentId];
        updateLocalAction(talentId, existing?.action || null, text);
        markReviewed(talentId);
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
        } catch {
            updateLocalAction(talentId, existing?.action, existing?.comment);
            toast.error("Failed to save");
        }
    }, [commentDrafts, viewerActions, slug, updateLocalAction, markReviewed]);

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
            await axios.post(
                `${API}/public/links/${slug}/feedback/voice`,
                formData,
                {
                    headers: {
                        "Content-Type": "multipart/form-data",
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            toast.success("Voice feedback sent for moderation");
            markReviewed(talentId);
        } catch (e) {
            toast.error("Failed to upload voice feedback");
            console.error(e);
        } finally {
            setSendingVoice(false);
        }
    }, [data, slug, markReviewed]);

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
                    <Loader2 className="w-8 h-8 animate-spin text-[#B89B5E]" />
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
                    projectBudget={[]}
                    projectShootDates={[]}
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

    const isShortlisted = (id) => viewerActions[id]?.action === "shortlist";
    const isNew = (id) => {
        if (!prevVisitAt) return false;
        const t = subjectAddedAt[id];
        if (!t) return false;
        return new Date(t).getTime() > new Date(prevVisitAt).getTime();
    };
    const buckets = {
        pending_action: talents.filter((t) => !reviewedIds.has(t.id) && !viewerActions[t.id]?.action),
        viewed: talents.filter((t) => reviewedIds.has(t.id) || !!viewerActions[t.id]?.action),
        ask_for_test: talents.filter((t) => viewerActions[t.id]?.action === "ask_for_test"),
        interested: talents.filter((t) => viewerActions[t.id]?.action === "interested"),
        not_for_this: talents.filter((t) => viewerActions[t.id]?.action === "not_for_this"),
        shortlist: talents.filter((t) => viewerActions[t.id]?.action === "shortlist"),
        lock: talents.filter((t) => viewerActions[t.id]?.action === "lock"),
        not_sure: talents.filter((t) => viewerActions[t.id]?.action === "not_sure"),
    };
    const filteredTalents = buckets[activeTab] || buckets.pending_action || talents;
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
                            {viewer.name} &bull; {seenCount} / {totalCount} reviewed
                        </p>
                    </div>
                    <div className="md:hidden mt-3 h-0.5 bg-black/[0.04] rounded-full overflow-hidden">
                        <div
                            className="h-full bg-[#B89B5E] transition-all duration-500"
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
                            <div className="bg-[#B89B5E]/5 border border-[#B89B5E]/15 rounded-xl p-4 flex items-center justify-between gap-4 backdrop-blur-sm shadow-[0_4px_12px_-6px_rgba(184,155,94,0.06)]">
                                <div className="flex items-center gap-3">
                                    <Clock className="w-4 h-4 text-[#B89B5E]" />
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


                <div
                    className="mb-5 hidden md:flex items-center gap-5"
                    data-testid="review-progress"
                >
                    <div className="flex-1 h-0.5 bg-black/[0.04] rounded-full overflow-hidden">
                        <div
                            className="h-full bg-[#B89B5E] transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar"
                        />
                    </div>
                </div>

                {(() => {
                    const visibleTabs = TABS.filter(t => t.key !== "ask_for_test" || link.requires_test === true);
                    return (
                        <div
                            className="mb-8 md:mb-12 -mx-6 md:mx-0 px-6 md:px-0 flex items-center gap-3 overflow-x-auto md:flex-wrap whitespace-nowrap border-b border-black/[0.04] pb-4"
                            style={{ scrollbarWidth: "none" }}
                            data-testid="client-view-tabs"
                        >
                            {visibleTabs.map((tab) => {
                                const count = buckets[tab.key]?.length || 0;
                                const active = activeTab === tab.key;
                                return (
                                    <button
                                        key={tab.key}
                                        type="button"
                                        onClick={() => setActiveTab(tab.key)}
                                        data-testid={`client-tab-${tab.key}`}
                                        className={`inline-flex items-center gap-2 px-4 md:px-4 py-2 rounded-full text-[11px] tracking-[0.08em] uppercase transition-colors duration-150 border shrink-0 active:scale-[0.97] ${
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
                        viewerActions[activeTalent.id]?.comment ??
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
                    sendingVoice={sendingVoice}
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
    console.log("CLIENT VIEW DATA (TalentDetail)", talent);
    const vis = link.visibility || {};
    const project = link || {};
    const visibleActions = ACTIONS.filter(a => a.key !== "ask_for_test" || project.requires_test === true);
    const mediaAll = talent.media || [];
    const portfolioOn = vis.portfolio !== false;
    const indianOn = portfolioOn && (vis.indian_images ?? true);
    const westernOn = portfolioOn && (vis.western_images ?? true);
    const portfolioGenericOn = portfolioOn;
    const portfolioImages = portfolioGenericOn
        ? mediaAll.filter((m) => m.category === "portfolio")
        : [];
    const indianImages = indianOn
        ? mediaAll.filter((m) => m.category === "indian")
        : [];
    const westernImages = westernOn
        ? mediaAll.filter((m) => m.category === "western")
        : [];
    const images = [...portfolioImages, ...indianImages, ...westernImages];
    const intro = mediaAll.find((m) => m.category === "video") || null;
    const takes = mediaAll.filter((m) => m.category === "take");
    const [idx, setIdx] = useState(0);
    const overlayRef = useRef(null);
    const [isModalOpen, setIsModalOpen] = useState(false);
    const [isDetailsExpanded, setIsDetailsExpanded] = useState(false);
    const [isDownloadingPackage, setIsDownloadingPackage] = useState(false);

    const handleCopyForm = () => {
        try {
            const lines = [];
            lines.push(`Name: ${privatizeName(talent.name)}`);
            if (talent.age) lines.push(`Age: ${talent.age}`);
            if (talent.height) lines.push(`Height: ${talent.height}`);
            if (talent.location) {
                const locStr = Array.isArray(talent.location)
                    ? talent.location.map(l => `${l.city}, ${l.country}`).join("; ")
                    : talent.location;
                lines.push(`Location: ${locStr}`);
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
        setIsDownloadingPackage(true);
        try {
            const token = getViewerToken(slug);
            const response = await axios.get(
                `${API}/public/links/${slug}/download/talent/${talent.id}`,
                {
                    params: token ? { token } : {},
                    headers: token ? { Authorization: `Bearer ${token}` } : {},
                    responseType: "blob"
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
            window.URL.revokeObjectURL(url);
        } catch (err) {
            console.error("Error downloading package:", err);
            if (err.response && err.response.data instanceof Blob) {
                const reader = new FileReader();
                reader.onload = function() {
                    alert(`Failed to download talent folder. Response: ${reader.result}`);
                };
                reader.readAsText(err.response.data);
            } else {
                const msg = err.response?.data?.detail || err.message || err;
                alert(`Failed to download talent folder. Error: ${JSON.stringify(msg)}`);
            }
        } finally {
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

    // Reset gallery image index and details accordion on talent navigation — prevents broken images when
    // navigating from a talent with many images to one with fewer (AUDIT: MED-01).
    useEffect(() => {
        setIdx(0);
        setIsDetailsExpanded(false);
    }, [talent.id]);

    useEffect(() => {
        if (!talent?.id || isReviewed) return;
        const timer = setTimeout(() => {
            onMarkReviewed(talent.id);
        }, 15000);
        return () => clearTimeout(timer);
    }, [talent?.id, isReviewed, onMarkReviewed]);

    const prev = useCallback(() => setIdx((i) => (i - 1 + images.length) % images.length), [images.length]);
    const next = useCallback(() => setIdx((i) => (i + 1) % images.length), [images.length]);

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


    const download = useCallback(async (m) => {
        await logDownload(talent.id, m.id);
        const rawUrl = IMAGE_URL(m);
        const isVideo = m.resource_type === "video" || m.category === "video" || m.category?.startsWith("take");
        const url = isVideo ? getVideoDownloadUrl(rawUrl) : rawUrl;
        
        const ext = isVideo ? "mp4" : (url.split(".").pop().split("?")[0] || "");
        let baseName = m.original_filename || `${privatizeName(talent.name)}_${m.category || "media"}`;
        if (baseName.includes(".")) {
            baseName = baseName.replace(/\.[^/.]+$/, "");
        }
        const filename = `${baseName}.${ext}`;
        
        try {
            // First try fetching the file directly to download via blob (respecting custom filename)
            const response = await fetch(url, { mode: "cors" });
            if (!response.ok) throw new Error("Network response was not ok");
            const blob = await response.blob();
            const blobUrl = URL.createObjectURL(blob);
            
            const a = document.createElement("a");
            a.href = blobUrl;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            
            // Clean up the object URL
            setTimeout(() => URL.revokeObjectURL(blobUrl), 100);
        } catch (err) {
            console.warn("CORS fetch failed, falling back to Cloudinary fl_attachment download:", err);
            // Fallback: Rewrite Cloudinary URL to force attachment headers
            let downloadUrl = url;
            if (url.includes("/upload/")) {
                const cleanName = baseName.replace(/[^a-zA-Z0-9_-]/g, "_");
                const flag = cleanName ? `fl_attachment:${cleanName}` : "fl_attachment";
                downloadUrl = url.replace("/upload/", `/upload/${flag}/`);
            }
            
            const a = document.createElement("a");
            a.href = downloadUrl;
            a.target = "_blank";
            document.body.appendChild(a);
            a.click();
            a.remove();
        }
    }, [logDownload, talent.id, talent.name]);

    return (
        <div
            ref={overlayRef}
            className={`fixed inset-0 z-50 bg-white overflow-hidden transition-opacity duration-300 ease-out ${isModalOpen ? "opacity-100" : "opacity-0"}`}
            data-testid="talent-detail-overlay"
        >
            <div className={`h-screen flex flex-col transition-transform duration-300 ease-out ${isModalOpen ? "scale-100" : "scale-95"}`}>

                {/* Unified Top Sticky Header (Desktop & Mobile) */}
                <div className="sticky top-0 z-50 bg-white border-b border-[#eaeaea] px-4 md:px-6 py-3 md:py-4 flex flex-wrap items-center justify-between shrink-0 shadow-sm">
                    <div className="min-w-0 flex-1 pr-4 flex items-center gap-3">
                        <h2 className="font-display text-base md:text-lg font-bold text-[#111111] truncate">
                            {privatizeName(talent.name)}
                        </h2>
                        <span className={`hidden md:inline-flex items-center text-[10px] px-2.5 py-1 rounded-full border font-mono uppercase tracking-wider ${
                            viewerAction?.action === "shortlist"
                                ? "bg-amber-50 text-amber-700 border-amber-200"
                                : viewerAction?.action === "interested"
                                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                : viewerAction?.action === "not_for_this"
                                ? "bg-rose-50 text-rose-700 border-rose-200"
                                : viewerAction?.action === "not_sure"
                                ? "bg-orange-50 text-orange-700 border-orange-200"
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
                                ? "bg-amber-50 text-amber-700 border-amber-200"
                                : viewerAction?.action === "interested"
                                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                : viewerAction?.action === "not_for_this"
                                ? "bg-rose-50 text-rose-700 border-rose-200"
                                : viewerAction?.action === "not_sure"
                                ? "bg-orange-50 text-orange-700 border-orange-200"
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
                                                value={Array.isArray(talent.location)
                                                    ? talent.location.map(l => `${l.city}, ${l.country}`).join("; ")
                                                    : talent.location} 
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
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Work</p>
                                            <WorkLinksDisplay links={talent.work_links} />
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
                                                        <span className="text-[8px] bg-amber-500 text-white px-1.5 py-0.5 rounded font-bold uppercase tracking-wider shrink-0">
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
                                                    />
                                                    {vis.download && (
                                                        <button
                                                            onClick={() => download(t)}
                                                            className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm z-10"
                                                            data-testid={`download-take-btn-${i}`}
                                                        >
                                                            <Download className="w-4 h-4 text-black" />
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
                                         />
                                         {vis.download && (
                                             <button
                                                 onClick={() => download(intro)}
                                                 className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm z-10"
                                                 data-testid="download-intro-btn"
                                             >
                                                 <Download className="w-4 h-4 text-black" />
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
                                            className="w-full h-full object-contain"
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
                                            className="absolute top-3 right-3 w-9 h-9 bg-white/90 border border-black/[0.06] hover:bg-white rounded-full flex items-center justify-center transition-colors duration-150 shadow-sm"
                                            data-testid="detail-download-btn"
                                        >
                                            <Download className="w-4 h-4" />
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
                                            onClick={() => setIdx(i)}
                                            className={`shrink-0 w-20 h-28 border-2 ${i === idx ? "border-[#B89B5E]" : "border-black/[0.04]"} rounded-xl overflow-hidden transition-colors duration-150`}
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
                                                className={`flex items-center gap-1.5 px-3 py-1.5 border rounded-full text-xs font-medium transition-colors duration-150 ${
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
                                                    <button key={a.key} onClick={() => setAction(talent.id, active ? null : a.key)} data-testid={`action-${a.key}-${talent.id}-mobile`} className={`flex items-center justify-center gap-2 px-4 py-2.5 border rounded-lg text-xs font-medium tracking-wide transition-all duration-150 ${active ? "border-black text-black bg-black/[0.04] font-semibold" : "border-[#eaeaea] hover:border-black/20 text-black/60 hover:text-black bg-transparent"}`}>
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
                                        <button onClick={saveComment} data-testid="detail-save-comment-btn-mobile" className="mt-3 text-xs px-4 py-2 border border-[#eaeaea] hover:border-black/25 rounded-full transition-colors duration-150 text-[#4A4A4A] hover:text-[#111111]">Save comment</button>
                                    </div>

                                    {talent.submission_id && talent.project_id && (
                                        <div className="pt-6 border-t border-black/[0.06]">
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Voice Note Feedback</p>
                                            <VoiceRecorder
                                                onSend={(blob) => saveVoiceNote(talent.id, blob)}
                                                sending={sendingVoice}
                                            />
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
                                    <InfoRow label="Location" value={talent.location} />
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
                                    <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Work</p>
                                    <WorkLinksDisplay links={talent.work_links} />
                                </div>
                            )}

                            {!isSharePreview && (
                                <div className="border-t border-black/[0.06] pt-6 mt-6">
                                    <div className="flex items-center justify-between mb-4">
                                        <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Your Decision</p>
                                        <button
                                            onClick={() => onMarkReviewed(talent.id)}
                                            disabled={isReviewed}
                                            className={`flex items-center gap-1.5 px-3 py-1.5 border rounded-full text-xs font-medium transition-colors duration-150 ${
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
                                                <button key={a.key} onClick={() => setAction(talent.id, active ? null : a.key)} data-testid={`action-${a.key}-${talent.id}`} className={`flex items-center justify-center gap-2 px-4 py-2.5 border rounded-lg text-xs font-medium tracking-wide transition-all duration-150 ${active ? "border-black text-black bg-black/[0.04] font-semibold" : "border-[#eaeaea] hover:border-black/20 text-black/60 hover:text-black bg-transparent"}`}>
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
                                        <button onClick={saveComment} data-testid="detail-save-comment-btn" className="mt-3 text-xs px-4 py-2 border border-[#eaeaea] hover:border-black/25 rounded-full transition-colors duration-150 text-[#4A4A4A] hover:text-[#111111]">Save comment</button>
                                    </div>

                                    {talent.submission_id && talent.project_id && (
                                        <div className="mt-6 pt-6 border-t border-black/[0.06]">
                                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Voice Note Feedback</p>
                                            <VoiceRecorder
                                                onSend={(blob) => saveVoiceNote(talent.id, blob)}
                                                sending={sendingVoice}
                                            />
                                        </div>
                                    )}
                                </div>
                            )}

                            {isSharePreview && (
                                <div className="mt-8 p-5 bg-white border border-black/[0.04] rounded-2xl flex flex-col items-center text-center">
                                    <span className="inline-block px-2.5 py-1 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[#B89B5E]/8 text-[#B89B5E] font-medium mb-3">
                                        Shared Audition Showcase
                                    </span>
                                    <p className="text-xs text-[#333333] max-w-xs leading-relaxed">
                                        This portfolio link was shared securely via WhatsApp and will expire in 48 hours.
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
const TalentCard = React.memo(function TalentCard({ talent, vis, action, seen, isNew, slug, setActiveTalent, markSeen }) {
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
        <button
            ref={ref}
            onClick={handleOpen}
            data-testid={`client-talent-${talent.id}`}
            data-seen={seen ? "true" : "false"}
            data-new={isNew ? "true" : "false"}
            className="group relative text-left transition-all duration-300 ease-out hover:-translate-y-0.5"
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
                    <div
                        className="font-display text-lg md:text-xl tracking-wide text-[#111111]"
                        data-testid={`client-card-name-${talent.id}`}
                    >
                        {privatizeName(talent.name)}
                    </div>
                    <div className="text-[11px] text-[#8A8A8A] font-mono tracking-[0.08em] mt-1">
                        {vis.location && talent.location 
                            ? (Array.isArray(talent.location) 
                                ? talent.location.map(l => `${l.city}, ${l.country}`).join("; ") 
                                : talent.location)
                            : ""}
                    </div>
                </div>

                <div className="absolute top-2 left-2 flex flex-col gap-1.5 items-start">
                    {isNew && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[#B89B5E] text-white text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
                            data-testid={`badge-new-${talent.id}`}
                        >
                            <Sparkles className="w-3 h-3" />
                            New
                        </span>
                    )}
                    {isShortlisted && (
                        <span
                            className="inline-flex items-center gap-1 px-2 py-1 bg-[#B89B5E] text-white text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
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
                </div>

                {seen && (
                    <span
                        className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 bg-white/95 border border-black/[0.06] text-[#8A8A8A] text-[10px] tracking-[0.08em] uppercase rounded-full shadow-sm"
                        data-testid={`badge-seen-${talent.id}`}
                    >
                        <Eye className="w-3 h-3" />
                        Seen
                    </span>
                )}
            </div>
        </button>
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
