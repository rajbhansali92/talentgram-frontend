import React, { useCallback, useEffect, useState, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import { IMAGE_URL, getViewerToken, saveViewerToken } from "@/lib/api";
import LazyVideoPlayer from "@/components/LazyVideoPlayer";
import { thumbnailUrl, posterUrl, resolveTalentCover } from "@/lib/mediaUtils";
import Logo from "@/components/Logo";
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
} from "lucide-react";

// Safety fallback to prevent catastrophic failures when env var is missing
const API =
    process.env.REACT_APP_BACKEND_URL
        ? `${process.env.REACT_APP_BACKEND_URL}/api`
        : "http://localhost:8000/api";

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

// Refined actions with desaturated, editorial gold accent
const ACTIONS = [
    { key: "shortlist", label: "Shortlist", icon: Star, color: "#B89B5E" },
    { key: "interested", label: "Interested", icon: ThumbsUp, color: "#5A7D5A" },
    { key: "not_for_this", label: "Not for this", icon: XCircle, color: "#9E4A4A" },
    { key: "not_sure", label: "Not sure", icon: HelpCircle, color: "#6B7280" },
];

const TABS = [
    { key: "pending_action", label: "Pending Action", icon: Clock },
    { key: "new", label: "New", icon: Sparkles },
    { key: "viewed", label: "Viewed", icon: Eye },
    { key: "shortlisted", label: "Shortlisted", icon: Heart },
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
        <div className="mb-8 bg-[#FCFBF8] p-5 space-y-4 rounded-xl shadow-sm">
            {showAvail && (
                <div data-testid="client-availability">
                    <p className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-2">Availability</p>
                    {tProj?.shoot_dates && (
                        <p className="text-sm text-[#4A4A4A] mb-2" data-testid="client-shoot-dates">{tProj.shoot_dates}</p>
                    )}
                    {(() => {
                        const lbl = availabilityLabel(talent.availability);
                        if (!lbl) return null;
                        const tone = lbl === "Available" ? "bg-[#5A7D5A]/8 text-[#5A7D5A]" : lbl === "Not Available" ? "bg-[#9E4A4A]/8 text-[#9E4A4A]" : "bg-[#B89B5E]/8 text-[#B89B5E]";
                        return (
                            <p className="text-sm">
                                <span className="text-[#8A8A8A] mr-2 text-[10px] font-mono tracking-[0.08em] uppercase">Status</span>
                                <span className={`inline-block px-2 py-0.5 mr-2 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full ${tone}`} data-testid="client-availability-status">{lbl}</span>
                                {talent.availability?.note && <span className="text-[#4A4A4A]">{talent.availability.note}</span>}
                            </p>
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
                            return (
                                <div className="space-y-1.5 text-sm text-[#111111]" data-testid="budget-agreed">
                                    {originalBudgetValue && (
                                        <p className="flex justify-between gap-4">
                                            <span className="text-[#4A4A4A]">Original Budget</span>
                                            <span className="text-[#5C5C5C] font-mono">{originalBudgetValue}</span>
                                        </p>
                                    )}
                                    <p className="flex justify-between gap-4 border-t border-black/[0.03] pt-1.5">
                                        <span className="text-[#4A4A4A]">Status</span>
                                        <span className="inline-block px-2 py-0.5 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[#5A7D5A]/8 text-[#5A7D5A] font-medium">Agreed</span>
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

export default function ClientView() {
    const { slug } = useParams();
    const getSessionId = () => {
        let sid = sessionStorage.getItem("client_session_id");
        if (!sid) {
            sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
            sessionStorage.setItem("client_session_id", sid);
        }
        return sid;
    };
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

    const [data, setData] = useState(null);
    const [activeTalent, setActiveTalent] = useState(null);
    const [commentDrafts, setCommentDrafts] = useState({});
    const [seenIds, setSeenIds] = useState(new Set());
    const [reviewedIds, setReviewedIds] = useState(new Set());
    const [activeTab, setActiveTab] = useState("pending_action");
    const [showResumeBanner, setShowResumeBanner] = useState(false);

    const viewerActions = useMemo(() => {
        const m = {};
        (data?.actions || []).forEach((a) => (m[a.talent_id] = a));
        return m;
    }, [data]);

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
            setData(data);
            setSeenIds(new Set(data?.client_state?.seen_talent_ids || []));
            setReviewedIds(new Set(data?.client_state?.reviewed_talent_ids || []));
            axios.post(`${API}/public/links/${slug}/track`, {
                event_type: "open",
                session_id: getSessionId(),
            }).catch(() => {});
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

    const markReviewed = useCallback(
        async (talentId) => {
            if (!talentId) return;
            setReviewedIds((prev) => {
                if (prev.has(talentId)) return prev;
                const n = new Set(prev);
                n.add(talentId);
                return n;
            });
            axios.post(`${API}/public/links/${slug}/track`, {
                event_type: "review_talent",
                session_id: getSessionId(),
                talent_id: talentId,
            }).catch(() => {});
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

    const identify = async (e) => {
        e.preventDefault();
        setLoading(true);
        try {
            const response = await axios.post(
                `${API}/public/links/${slug}/identify`,
                {
                    name: name,
                    email: email,
                },
            );
            if (response.data.token) {
                saveViewerToken(slug, response.data.token);
                setIdentified(true);
                toast.success(
                    `Welcome, ${(name || "Guest").split(" ")[0]}`
                );
            } else {
                throw new Error("No token received");
            }
        } catch (e) {
            const errorMessage = formatErrorMessage(e);
            console.error("IDENTIFY ERROR:", errorMessage);
            toast.error(errorMessage);
        } finally {
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
            
            const shareUrl = `${window.location.origin}/l/${slug}?share=${data.share_id}`;
            
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
            setSeenIds((prev) => {
                if (prev.has(talentId)) return prev;
                const n = new Set(prev);
                n.add(talentId);
                return n;
            });
            axios.post(`${API}/public/links/${slug}/track`, {
                event_type: "view_talent",
                session_id: getSessionId(),
                talent_id: talentId,
            }).catch(() => {});
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
                <div className="min-h-screen bg-[#FCFBF8] flex items-center justify-center">
                    <Loader2 className="w-8 h-8 animate-spin text-[#B89B5E]" />
                </div>
            );
        }
        if (shareError) {
            return (
                <div className="min-h-screen bg-[#FCFBF8] flex flex-col items-center justify-center p-6 text-center">
                    <XCircle className="w-12 h-12 text-[#9E4A4A] mb-4" />
                    <h2 className="text-xl font-display text-[#111111] mb-2 font-semibold">Shared Preview Expired</h2>
                    <p className="text-sm text-gray-500 max-w-sm leading-relaxed">{shareError}</p>
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
                                    className="mt-2 w-full bg-transparent border-b border-black/[0.06] focus:border-black/25 outline-none py-2 text-sm text-[#111111] placeholder:text-black/25 transition-colors duration-150"
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
                                    className="mt-2 w-full bg-transparent border-b border-black/[0.06] focus:border-black/25 outline-none py-2 text-sm text-[#111111] placeholder:text-black/25 transition-colors duration-150"
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
        new: talents.filter((t) => isNew(t.id)),
        viewed: talents.filter((t) => reviewedIds.has(t.id)),
        shortlisted: talents.filter((t) => isShortlisted(t.id)),
    };
    const filteredTalents = buckets[activeTab] || buckets.pending_action || talents;
    const reviewedCount = talents.filter((t) => reviewedIds.has(t.id) || !!viewerActions[t.id]?.action).length;
    const seenCount = reviewedCount;
    const totalCount = talents.length;
    const reviewedPct = totalCount === 0 ? 0 : Math.round((reviewedCount / totalCount) * 100);

    return (
        <div className="min-h-screen bg-white text-[#111111]" data-testid="client-view-page">
            <header className="sticky top-0 z-30 bg-white/95 border-b border-black/[0.04]">
                <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-4 md:py-6">
                    <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0 flex-1">
                            <p className="eyebrow hidden md:block tracking-[0.12em] text-[#8A8A8A]">Curated Review</p>
                            <h1 className="font-display text-base md:text-xl tracking-wide mt-0 md:mt-1 truncate text-[#111111]">
                                {link.title}
                            </h1>
                            <p className="text-[10px] md:hidden text-[#8A8A8A] font-mono tracking-[0.08em] mt-0.5 truncate">
                                {viewer.name} · {seenCount}/{totalCount} reviewed
                            </p>
                        </div>
                        <div className="hidden md:block text-right">
                            <p className="text-xs text-[#8A8A8A]">Viewing as</p>
                            <p className="text-sm font-medium text-[#111111]">{viewer.name}</p>
                        </div>
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

            <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-6 md:py-16">
                {showResumeBanner && (() => {
                    const lastId = localStorage.getItem(`tg_last_viewed_${slug}`);
                    const lastTalent = talents.find(t => t.id === lastId);
                    if (!lastTalent) return null;
                    return (
                        <div className="mb-8 animate-fade-in" data-testid="resume-review-banner">
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
                <div className="hidden md:flex mb-10 items-center justify-between flex-wrap gap-4">
                    <div>
                        <p className="eyebrow tracking-[0.12em] mb-2 text-[#8A8A8A]">{talents.length} Talents</p>
                        <h2 className="font-display text-3xl md:text-4xl tracking-wide text-[#111111]">
                            Pick your winners.
                        </h2>
                    </div>
                    <p className="text-xs text-[#8A8A8A] max-w-sm leading-relaxed">
                        Tap any card to view the full portfolio. Actions and
                        comments are saved instantly.
                    </p>
                </div>

                <div
                    className="mb-8 hidden md:flex items-center gap-5"
                    data-testid="review-progress"
                >
                    <div className="flex-1 h-0.5 bg-black/[0.04] rounded-full overflow-hidden">
                        <div
                            className="h-full bg-[#B89B5E] transition-all duration-500"
                            style={{ width: `${reviewedPct}%` }}
                            data-testid="review-progress-bar"
                        />
                    </div>
                    <div
                        className="text-[11px] font-mono text-[#8A8A8A] tracking-[0.08em] shrink-0"
                        data-testid="review-progress-label"
                    >
                        {seenCount} of {totalCount} reviewed
                    </div>
                </div>

                <div
                    className="mb-8 md:mb-12 -mx-6 md:mx-0 px-6 md:px-0 flex items-center gap-3 overflow-x-auto md:flex-wrap whitespace-nowrap border-b border-black/[0.04] pb-4"
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

                {projectBudget.length > 0 && (
                    <section
                        className="mb-12 bg-[#FCFBF8] p-6 md:p-8 rounded-2xl shadow-[0_2px_8px_-4px_rgba(0,0,0,0.02)]"
                        data-testid="project-budget-block"
                    >
                        <p className="eyebrow tracking-[0.12em] mb-5 text-[#5C5C5C]">Project Budget</p>
                        <div className="grid gap-8 md:grid-cols-2">
                            {projectBudget.map((grp, gi) => (
                                <div
                                    key={grp.project_id || `grp-${gi}`}
                                    className="space-y-3"
                                    data-testid={`project-budget-group-${gi}`}
                                >
                                    {grp.brand_name && projectBudget.length > 1 && (
                                        <div className="text-[10px] tracking-[0.08em] uppercase text-[#8A8A8A] mb-1">
                                            {grp.brand_name}
                                        </div>
                                    )}
                                    {(grp.lines || []).map((row, ri) => (
                                        <div
                                            key={`${row.label || ""}-${ri}`}
                                            className="flex items-center justify-between gap-4 border-b border-black/[0.03] pb-2 text-sm"
                                            data-testid={`project-budget-line-${gi}-${ri}`}
                                        >
                                            <span className="text-[#5C5C5C]">
                                                {row.label || "—"}
                                            </span>
                                            <span className="font-mono text-[#111111]">
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
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-5 md:gap-7"
                    data-testid="client-talents-grid"
                >
                    {filteredTalents.length === 0 ? (
                        <div
                            className="col-span-full text-center py-20 text-[#8A8A8A] text-sm"
                            data-testid="client-tab-empty"
                        >
                            {activeTab === "new" && "Nothing new since your last visit."}
                            {activeTab === "shortlisted" &&
                                "No shortlists yet — open a card and click Shortlist to add one."}
                            {activeTab === "viewed" && "You haven't reviewed any talents yet."}
                            {activeTab === "pending_action" &&
                                "You've reviewed everyone — nice work."}
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
                                onOpen={() => {
                                    setActiveTalent(t);
                                    markSeen(t.id);
                                    try {
                                        localStorage.setItem(`tg_last_viewed_${slug}`, t.id);
                                    } catch (e) { console.error(e); }
                                }}
                                onSeen={() => markSeen(t.id)}
                            />
                        ))
                    )}
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
    const vis = link.visibility || {};
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
    const [busyAction, setBusyAction] = useState(null);
    const overlayRef = useRef(null);
    const [isModalOpen, setIsModalOpen] = useState(false);

    useEffect(() => {
        if (!talent?.id || isReviewed) return;
        const timer = setTimeout(() => {
            onMarkReviewed(talent.id);
        }, 15000);
        return () => clearTimeout(timer);
    }, [talent?.id, isReviewed, onMarkReviewed]);

    const prev = useCallback(() => setIdx((i) => (i - 1 + images.length) % images.length), [images.length]);
    const next = useCallback(() => setIdx((i) => (i + 1) % images.length), [images.length]);

    useEffect(() => {
        document.body.style.overflow = "hidden";
        setIsModalOpen(true);
        
        const handleKeyDown = (e) => {
            if (e.key === "Escape" && !isSharePreview) {
                onClose();
            } else if (e.key === "ArrowLeft") {
                prev();
            } else if (e.key === "ArrowRight") {
                next();
            } else if (e.key === "1" && !isSharePreview) {
                setAction(talent.id, viewerAction?.action === "shortlist" ? null : "shortlist");
            } else if (e.key === "2" && !isSharePreview) {
                setAction(talent.id, viewerAction?.action === "not_sure" ? null : "not_sure");
            } else if (e.key === "3" && !isSharePreview) {
                setAction(talent.id, viewerAction?.action === "not_for_this" ? null : "not_for_this");
            }
        };
        document.addEventListener("keydown", handleKeyDown);
        
        return () => {
            document.body.style.overflow = "";
            document.removeEventListener("keydown", handleKeyDown);
        };
    }, [onClose, prev, next, setAction, talent.id, viewerAction?.action, isSharePreview]);

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
            if (ax > 60 && ax > ay * 1.4) {
                if (movedX < 0) goNextTalent();
                else goPrevTalent();
                return;
            }
            if (movedY > 110 && ay > ax * 1.4 && startY < 200 && !isSharePreview) {
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
    }, [goNextTalent, goPrevTalent, onClose, isSharePreview]);

    const quickAction = useCallback(async (key) => {
        if (busyAction) return;
        setBusyAction(key);
        if (typeof navigator !== "undefined" && navigator.vibrate) {
            try { navigator.vibrate?.(10); } catch (e) { console.error(e); }
        }
        try {
            await setAction(talent.id, key);
            setTimeout(() => {
                if (hasNextTalent) goNextTalent();
                setBusyAction(null);
            }, 350);
        } catch {
            setBusyAction(null);
        }
    }, [busyAction, setAction, talent.id, hasNextTalent, goNextTalent]);

    const download = useCallback(async (m) => {
        await logDownload(talent.id, m.id);
        const url = IMAGE_URL(m);
        const a = document.createElement("a");
        a.href = url;
        a.download = m.original_filename || "file";
        a.target = "_blank";
        document.body.appendChild(a);
        a.click();
        a.remove();
    }, [logDownload, talent.id]);

    return (
        <div
            ref={overlayRef}
            className={`fixed inset-0 z-50 bg-white overflow-hidden transition-opacity duration-300 ease-out ${isModalOpen ? "opacity-100" : "opacity-0"}`}
            data-testid="talent-detail-overlay"
        >
            <div className={`h-screen flex flex-col transition-transform duration-300 ease-out ${isModalOpen ? "scale-100" : "scale-95"}`}>
                <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
                    {/* Left Column - Image */}
                    <div className="w-full md:w-[58%] lg:w-[60%] bg-[#FCFBF8] overflow-y-auto">
                        <div className="p-4 md:p-8">
                            {vis.takes !== false && takes.length > 0 && (
                                <div className="mb-10">
                                    <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Audition Takes</p>
                                    <div className="grid grid-cols-2 gap-4">
                                        {takes.map((t, i) => (
                                            <div key={t.id} data-testid={`client-take-${i}`}>
                                                <p className="text-[11px] text-[#8A8A8A] mb-2 font-mono tracking-[0.08em] truncate flex items-center justify-between">
                                                    <span className="flex items-center gap-1.5 min-w-0">
                                                        <span className="truncate">{t.label || `Take ${i + 1}`}</span>
                                                        {!isSharePreview && (
                                                            <button
                                                                onClick={() => onShare(talent.id, t.id)}
                                                                className="p-1 hover:bg-black/5 rounded text-gray-400 hover:text-gray-700 transition-colors shrink-0"
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
                                                <LazyVideoPlayer
                                                    src={t.url}
                                                    poster={posterUrl(t)}
                                                    label={t.label || `Take ${i + 1}`}
                                                    className="w-full"
                                                    mediaId={t.id}
                                                    slug={slug}
                                                />
                                            </div>
                                        ))}
                                    </div>
                                </div>
                             )}
 
                             {vis.intro_video && intro && (
                                 <div className="mb-10">
                                     <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Introduction</p>
                                     <LazyVideoPlayer
                                         src={intro.url}
                                         poster={posterUrl(intro)}
                                         label="Introduction Video"
                                         className="w-full"
                                         mediaId={intro.id}
                                         slug={slug}
                                     />
                                 </div>
                             )}

                            {images.length > 0 && (
                                <p className="eyebrow tracking-[0.12em] mb-4 text-[#4A4A4A]">Portfolio</p>
                            )}
                            {images.length > 0 ? (
                                <div className="relative bg-white rounded-xl overflow-hidden shadow-sm">
                                    <div className="aspect-[3/4] md:max-h-[78vh]">
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
                                <div className="aspect-[3/4] md:max-h-[78vh] bg-[#FCFBF8] rounded-xl flex items-center justify-center text-[#8A8A8A] shadow-sm">
                                    No portfolio
                                </div>
                            )}

                            {images.length > 1 && (
                                <div className="mt-7 flex gap-3 overflow-x-auto pb-3" data-stop-swipe="1">
                                    {images.map((m, i) => (
                                        <button
                                            key={m.id}
                                            onClick={() => setIdx(i)}
                                            className={`shrink-0 w-20 h-24 border-2 ${i === idx ? "border-[#B89B5E]" : "border-black/[0.04]"} rounded-xl overflow-hidden transition-colors duration-150`}
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
                        </div>
                    </div>

                    {/* Right Column - Details (scrollable with soft shadow) */}
                    <div className="w-full md:w-[42%] lg:w-[40%] bg-white overflow-y-auto shadow-[-10px_0_30px_-20px_rgba(0,0,0,0.08)]">
                        <div className="p-6 md:p-8">
                            <div className="hidden md:flex absolute top-5 right-5 z-50 gap-2">
                                {!isSharePreview && (
                                    <button
                                        onClick={() => onShare(talent.id)}
                                        className="w-11 h-11 border border-black/[0.06] hover:border-black/20 rounded-full flex items-center justify-center bg-white/90 transition-colors duration-150 shadow-sm"
                                        title="Share Portfolio"
                                        data-testid="detail-share-btn"
                                    >
                                        <Share2 className="w-4 h-4 text-[#8A8A8A]" />
                                    </button>
                                )}
                                {!isSharePreview && (
                                    <button
                                        onClick={onClose}
                                        className="w-11 h-11 border border-black/[0.06] hover:border-black/20 rounded-full flex items-center justify-center bg-white/90 transition-colors duration-150 shadow-sm"
                                        data-testid="detail-close-btn"
                                    >
                                        <X className="w-4 h-4" />
                                    </button>
                                )}
                            </div>

                            <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Talent</p>
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
                            </div>

                            <AvailabilityBudgetSection 
                                talent={talent}
                                projectShootDates={projectShootDates}
                                projectBudget={projectBudget}
                                vis={vis}
                            />

                            {(talent.custom_answers || []).length > 0 && (
                                <div className="mb-8 bg-[#FCFBF8] p-5 space-y-3 rounded-xl shadow-sm">
                                    <p className="eyebrow tracking-[0.12em] text-[#4A4A4A]">Additional Details</p>
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
                                    <a href={`https://instagram.com/${talent.instagram_handle.replace("@", "")}`} target="_blank" rel="noopener noreferrer" data-testid="client-instagram-link" className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.06] hover:border-black/20 rounded-full text-xs transition-colors duration-150 text-[#111111] bg-white/50">
                                        <Instagram className="w-3.5 h-3.5" /> {talent.instagram_handle}
                                    </a>
                                )}
                            </div>

                            {vis.work_links && (talent.work_links || []).length > 0 && (
                                <div className="mb-8">
                                    <p className="eyebrow tracking-[0.12em] mb-3 text-[#4A4A4A]">Work</p>
                                    <div className="space-y-2">
                                        {talent.work_links.map((w) => (
                                            <a key={w} href={w} target="_blank" rel="noreferrer" className="flex items-center gap-2 text-sm text-[#4A4A4A] hover:text-[#111111] font-mono truncate transition-colors duration-150">
                                                <ExternalLink className="w-3 h-3 shrink-0" />
                                                <span className="truncate">{w}</span>
                                            </a>
                                        ))}
                                    </div>
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
                                                    : "border-black/[0.08] hover:border-black/20 text-[#4A4A4A]"
                                            }`}
                                            data-testid="mark-reviewed-btn"
                                        >
                                            <Check className="w-3.5 h-3.5" />
                                            {isReviewed ? "Reviewed" : "Mark Reviewed"}
                                        </button>
                                    </div>
                                    <div className="grid grid-cols-2 gap-2 mb-6">
                                        {ACTIONS.map((a) => {
                                            const active = viewerAction?.action === a.key;
                                            return (
                                                <button key={a.key} onClick={() => setAction(talent.id, active ? null : a.key)} data-testid={`action-${a.key}-${talent.id}`} className={`flex items-center gap-2 px-4 py-3 border rounded-xl text-sm transition-colors duration-150 ${active ? "bg-[#1A1A1A] text-white border-[#1A1A1A]" : "border-black/[0.08] hover:border-black/20 text-[#111111]"}`}>
                                                    <a.icon className="w-4 h-4" style={{ color: active ? "#fff" : a.color }} />
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
                                        <textarea value={commentDraft} onChange={(e) => setCommentDraft(e.target.value)} rows={3} placeholder="Share any notes about this talent..." data-testid="detail-comment-input" className="w-full bg-transparent border border-black/[0.08] focus:border-black/25 rounded-xl p-3 text-sm outline-none transition-colors duration-150 text-[#111111] placeholder:text-black/30" />
                                        <button onClick={saveComment} data-testid="detail-save-comment-btn" className="mt-3 text-xs px-4 py-2 border border-black/[0.08] hover:border-black/25 rounded-full transition-colors duration-150 text-[#4A4A4A] hover:text-[#111111]">Save comment</button>
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
                                <div className="mt-8 p-5 bg-[#FCFBF8] border border-black/[0.04] rounded-2xl flex flex-col items-center text-center">
                                    <span className="inline-block px-2.5 py-1 text-[10px] font-mono tracking-[0.08em] uppercase rounded-full bg-[#B89B5E]/8 text-[#B89B5E] font-medium mb-3">
                                        Shared Audition Showcase
                                    </span>
                                    <p className="text-xs text-gray-500 max-w-xs leading-relaxed">
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

            {/* Mobile bottom bar */}
            {!isSharePreview && (
                <div className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-white/95 border-t border-black/[0.04] px-4 py-3" data-testid="detail-bottom-bar" style={{ paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom))" }}>
                    <div className="grid grid-cols-3 gap-2">
                        <button type="button" onClick={() => quickAction("shortlist")} disabled={Boolean(busyAction)} data-testid="quick-shortlist-btn" className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-xl border text-[11px] tracking-[0.08em] uppercase active:scale-[0.97] transition-colors duration-150 ${viewerAction?.action === "shortlist" ? "bg-[#B89B5E] text-white border-[#B89B5E]" : "border-black/[0.08] text-[#111111] hover:border-black/20"}`}>
                            {busyAction === "shortlist" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Star className={`w-4 h-4 ${viewerAction?.action === "shortlist" ? "fill-current" : ""}`} />}
                            Shortlist
                        </button>
                        <button type="button" onClick={() => quickAction("not_sure")} disabled={Boolean(busyAction)} data-testid="quick-hold-btn" className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-xl border text-[11px] tracking-[0.08em] uppercase active:scale-[0.97] transition-colors duration-150 ${viewerAction?.action === "not_sure" ? "bg-black/5 text-black border-black/20" : "border-black/[0.08] text-[#4A4A4A] hover:border-black/20"}`}>
                            {busyAction === "not_sure" ? <Loader2 className="w-4 h-4 animate-spin" /> : <HelpCircle className="w-4 h-4" />}
                            Hold
                        </button>
                        <button type="button" onClick={() => quickAction("not_for_this")} disabled={Boolean(busyAction)} data-testid="quick-reject-btn" className={`min-h-[52px] flex flex-col items-center justify-center gap-0.5 rounded-xl border text-[11px] tracking-[0.08em] uppercase active:scale-[0.97] transition-colors duration-150 ${viewerAction?.action === "not_for_this" ? "bg-[#9E4A4A] text-white border-[#9E4A4A]" : "border-black/[0.08] text-[#4A4A4A] hover:border-[#9E4A4A]/50 hover:text-[#9E4A4A]"}`}>
                            {busyAction === "not_for_this" ? <Loader2 className="w-4 h-4 animate-spin" /> : <XCircle className="w-4 h-4" />}
                            Reject
                        </button>
                    </div>
                    {list.length > 1 && (
                        <div className="flex items-center justify-between mt-2 text-[10px] font-mono tracking-[0.08em] text-[#8A8A8A]">
                            <button type="button" onClick={goPrevTalent} disabled={!hasPrevTalent} data-testid="quick-prev-btn" className="px-2 py-1 disabled:opacity-30 active:scale-[0.95] transition-transform" aria-label="Previous talent">← swipe right · prev</button>
                            <span>{currentTalentIdx + 1} of {list.length}</span>
                            <button type="button" onClick={goNextTalent} disabled={!hasNextTalent} data-testid="quick-next-btn" className="px-2 py-1 disabled:opacity-30 active:scale-[0.95] transition-transform" aria-label="Next talent">next · swipe left →</button>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

function TalentCard({ talent, vis, action, seen, isNew, onOpen, onSeen }) {
    const ref = useRef(null);
    const timerRef = useRef(null);

    const cover = resolveTalentCover(talent);
    const isShortlisted = action === "shortlist";

    useEffect(() => {
        if (seen || !ref.current) return;
        const node = ref.current;
        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
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
            className="group relative text-left transition-all duration-300 ease-out hover:-translate-y-0.5"
        >
            <div className="aspect-[3/4] bg-white overflow-hidden rounded-2xl group-hover:shadow-[0_8px_30px_rgb(0,0,0,0.04)] transition-all duration-300 relative shadow-sm">
                {cover ? (
                    <img
                        src={thumbnailUrl(cover)}
                        alt={privatizeName(talent.name)}
                        loading="lazy"
                        className="w-full h-full object-cover group-hover:scale-105 transition-all duration-500 ease-out"
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
                        {vis.location && talent.location ? talent.location : ""}
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
}

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
