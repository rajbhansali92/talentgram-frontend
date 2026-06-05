import React, { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { adminApi, isAdmin, PUBLIC_FRONTEND_URL } from "@/lib/api";
import { toast } from "sonner";
import {
    ArrowLeft,
    ChevronLeft,
    ChevronRight,
    Check,
    XCircle,
    PauseCircle,
    Clock,
    Eye,
    EyeOff,
    Shield,
    Star,
    Lock,
    ExternalLink,
    Loader2,
    Video,
    Film,
    Image as ImageIcon,
    FileText,
    Download,
    Cloud,
    RefreshCw,
    User,
    Phone,
    Mail
} from "lucide-react";
import { AVAILABILITY_OPTIONS, BUDGET_OPTIONS } from "@/lib/talentSchema";

function formatRelativeTime(ts) {
    if (!ts) return "—";
    try {
        const d = new Date(ts);
        const now = new Date();
        const diffMs = now - d;
        const diffMins = Math.floor(diffMs / 60000);
        if (diffMins < 1) return "Just now";
        if (diffMins < 60) return `${diffMins}m ago`;
        const diffHours = Math.floor(diffMins / 60);
        if (diffHours < 24) return `${diffHours}h ago`;
        const diffDays = Math.floor(diffHours / 24);
        if (diffDays === 1) return "Yesterday";
        if (diffDays < 7) return `${diffDays}d ago`;
        return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    } catch {
        return "—";
    }
}

function PremiumVideoPlayer({ src, poster, isPrimary, label }) {
    const videoRef = useRef(null);
    const [isPlaying, setIsPlaying] = useState(false);

    const togglePlay = () => {
        if (!videoRef.current) return;
        if (isPlaying) {
            videoRef.current.pause();
            setIsPlaying(false);
        } else {
            videoRef.current.play().then(() => {
                setIsPlaying(true);
            }).catch(() => {});
        }
    };

    return (
        <div className="relative border border-neutral-200 bg-black rounded-lg overflow-hidden aspect-video group shadow-sm">
            <video
                ref={videoRef}
                src={src}
                poster={poster}
                preload="metadata"
                className="w-full h-full object-cover cursor-pointer"
                onEnded={() => setIsPlaying(false)}
                onClick={togglePlay}
            />
            {!isPlaying && (
                <button
                    onClick={togglePlay}
                    className="absolute inset-0 flex items-center justify-center bg-black/20 hover:bg-black/35 transition-colors group-hover:scale-105 duration-200"
                >
                    <div className="w-12 h-12 rounded-full bg-white/95 shadow-md flex items-center justify-center text-black">
                        <Video className="w-6 h-6 text-black" />
                    </div>
                </button>
            )}
            <div className="absolute bottom-2.5 left-2.5 right-2.5 flex items-center justify-between pointer-events-none">
                <span className="text-[9px] bg-black/60 backdrop-blur-sm text-white px-2 py-0.5 rounded font-mono uppercase tracking-wider">
                    {label}
                </span>
                {isPrimary && (
                    <span className="text-[9px] bg-amber-500 backdrop-blur-sm text-white px-2 py-0.5 rounded font-bold uppercase tracking-wider flex items-center gap-0.5">
                        <Star className="w-2.5 h-2.5 fill-white text-white" /> Primary Take
                    </span>
                )}
            </div>
        </div>
    );
}

function getCompleteness(s, project) {
    const hasIntro = s.media?.some(m => m.category === "intro_video" || m.category === "video");
    const takesCount = s.media?.filter(m => ["take", "take_1", "take_2", "take_3"].includes(m.category)).length || 0;
    const imagesCount = s.media?.filter(m => ["image", "indian", "western"].includes(m.category)).length || 0;
    
    const customAnswers = s.form_data?.custom_answers || {};
    const totalQuestions = project?.custom_questions?.length || 0;
    const answeredCount = Object.values(customAnswers).filter(val => val !== undefined && val !== null && val !== "").length;
    const hasAllQuestions = totalQuestions === 0 || answeredCount >= totalQuestions;

    const missing = [];
    if (!hasIntro) missing.push("Intro Video");
    if (imagesCount === 0) missing.push("Images");
    if (!hasAllQuestions) missing.push("Questions");

    if (missing.length === 0) {
        return { status: "Complete", color: "bg-green-50 text-green-700 border-green-200" };
    }
    if (missing.length === 1) {
        return { status: `Missing ${missing[0]}`, color: "bg-amber-50 text-amber-700 border-amber-200" };
    }
    return { status: "Incomplete Submission", color: "bg-red-50 text-red-700 border-red-200" };
}

export default function SubmissionReviewCenter() {
    const { id } = useParams();
    const navigate = useNavigate();
    const isAdminRole = isAdmin();

    const [project, setProject] = useState(null);
    const [submissions, setSubmissions] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [detail, setDetail] = useState(null);
    
    // UI states
    const [loadingProject, setLoadingProject] = useState(true);
    const [loadingSubmissions, setLoadingSubmissions] = useState(true);
    const [loadingDetail, setLoadingDetail] = useState(false);
    const [filter, setFilter] = useState("all");
    const [searchQuery, setSearchQuery] = useState("");
    const [isMobileDetailOpen, setIsMobileDetailOpen] = useState(false);
    const [isEndOfList, setIsEndOfList] = useState(false);
    
    // Curation / Decision states
    const [decisionNote, setDecisionNote] = useState("");
    const [form, setForm] = useState({});
    const [fv, setFv] = useState({});
    const [mediaList, setMediaList] = useState([]);
    const [isPreviewMode, setIsPreviewMode] = useState(false);
    const [saving, setSaving] = useState(false);

    // Normalize utility
    const normalize = (fd) => ({
        ...fd,
        availability:
            typeof fd?.availability === "object" && fd.availability !== null
                ? fd.availability
                : { status: "", note: fd?.availability || "" },
        budget:
            typeof fd?.budget === "object" && fd.budget !== null
                ? fd.budget
                : { status: "", value: fd?.budget || "" },
    });

    // 1. Load project data
    useEffect(() => {
        const fetchProject = async () => {
            try {
                const { data } = await adminApi.get(`/projects/${id}`);
                setProject(data);
            } catch (e) {
                toast.error("Failed to load project details");
            } finally {
                setLoadingProject(false);
            }
        };
        fetchProject();
    }, [id]);

    // 2. Load submission lists
    const loadSubmissions = useCallback(async () => {
        setLoadingSubmissions(true);
        try {
            const { data } = await adminApi.get(`/projects/${id}/submissions`);
            setSubmissions(data);
            setSelectedId(prev => {
                if (!prev && data.length > 0) {
                    return data[0].id;
                }
                return prev;
            });
        } catch (e) {
            toast.error("Failed to load submissions");
        } finally {
            setLoadingSubmissions(false);
        }
    }, [id]);

    useEffect(() => {
        loadSubmissions();
    }, [loadSubmissions]);

    // 3. Load selected submission details
    useEffect(() => {
        if (!selectedId) {
            setDetail(null);
            return;
        }
        const fetchDetail = async () => {
            setLoadingDetail(true);
            try {
                const { data } = await adminApi.get(`/projects/${id}/submissions/${selectedId}`);
                setDetail(data);
                setForm(normalize(data?.form_data));
                setFv(data?.field_visibility || {});
                setMediaList(data?.media || []);
                setDecisionNote(data?.decision_note || "");
            } catch (e) {
                toast.error("Failed to load submission details");
            } finally {
                setLoadingDetail(false);
            }
        };
        fetchDetail();
    }, [selectedId, id]);

    // Action handlers
    const handleDecision = async (decision) => {
        if (!selectedId) return;
        setSaving(true);
        try {
            await adminApi.post(`/projects/${id}/submissions/${selectedId}/decision`, {
                decision,
                note: decisionNote,
            });
            
            const toastMessages = {
                approved: "Approved and moved to next submission",
                hold: "Held and moved to next submission",
                rejected: "Rejected and moved to next submission",
            };
            toast.success(toastMessages[decision] || `${decision} registered`);
            
            // Reload submission lists and update statuses locally
            const updatedList = submissions.map(s => s.id === selectedId ? { ...s, decision } : s);
            setSubmissions(updatedList);

            // Move to next submission automatically if exists, otherwise show completion view
            if (currentIndex === filteredSubmissions.length - 1) {
                setIsEndOfList(true);
            } else {
                const nextIndex = currentIndex >= filteredSubmissions.length - 1 ? 0 : currentIndex + 1;
                setSelectedId(filteredSubmissions[nextIndex].id);
                setIsEndOfList(false);
            }
        } catch (e) {
            toast.error("Failed to register decision");
        } finally {
            setSaving(false);
        }
    };

    const handleSaveCuration = async () => {
        if (!selectedId) return;
        setSaving(true);
        try {
            await adminApi.put(`/projects/${id}/submissions/${selectedId}`, {
                form_data: form,
                field_visibility: fv,
                media: mediaList,
            });
            toast.success("Curation settings saved");
            
            // Reload detailed snapshot
            const { data } = await adminApi.get(`/projects/${id}/submissions/${selectedId}`);
            setDetail(data);
            setForm(normalize(data?.form_data));
            setFv(data?.field_visibility || {});
            setMediaList(data?.media || []);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save curation settings");
        } finally {
            setSaving(false);
        }
    };

    const openInDrive = async () => {
        if (!selectedId) return;
        try {
            const { data } = await adminApi.get(`/submissions/${selectedId}/drive`);
            window.open(data.url, "_blank", "noopener,noreferrer");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Google Drive not configured");
        }
    };

    const regenerateSnapshot = async () => {
        if (!selectedId) return;
        setSaving(true);
        try {
            await adminApi.post(`/projects/${id}/submissions/${selectedId}/snapshot`);
            toast.success("Client snapshot frozen successfully");
        } catch (e) {
            toast.error("Snapshot generation failed");
        } finally {
            setSaving(false);
        }
    };

    // Filtered lists
    const filteredSubmissions = submissions.filter((s) => {
        // Status tabs
        if (filter !== "all") {
            if (filter === "updated" && s.status !== "updated") return false;
            if (filter !== "updated" && (s.decision || "pending") !== filter) return false;
        }
        // Search query
        if (searchQuery.trim()) {
            const q = searchQuery.toLowerCase();
            const nameMatch = (s.talent_name || "").toLowerCase().includes(q);
            const emailMatch = (s.talent_email || "").toLowerCase().includes(q);
            return nameMatch || emailMatch;
        }
        return true;
    });

    // Navigation indexes
    const currentIndex = filteredSubmissions.findIndex(s => s.id === selectedId);
    
    const handlePrev = () => {
        if (filteredSubmissions.length <= 1) return;
        const newIndex = currentIndex <= 0 ? filteredSubmissions.length - 1 : currentIndex - 1;
        setSelectedId(filteredSubmissions[newIndex].id);
        setIsEndOfList(false);
    };

    const handleNext = () => {
        if (filteredSubmissions.length <= 1) return;
        const newIndex = currentIndex >= filteredSubmissions.length - 1 ? 0 : currentIndex + 1;
        setSelectedId(filteredSubmissions[newIndex].id);
        setIsEndOfList(false);
    };

    const handleSelectRow = (sid) => {
        setSelectedId(sid);
        setIsEndOfList(false);
        setIsMobileDetailOpen(true);
    };

    // Media grouping helper
    const getCuratedMedia = (categoryGroup) => {
        return mediaList.filter((m) => {
            const matchesGroup = categoryGroup === "video"
                ? m.category === "intro_video" || m.category === "video"
                : categoryGroup === "takes"
                ? ["take", "take_1", "take_2", "take_3"].includes(m.category)
                : m.category === categoryGroup;
            
            if (!matchesGroup) return false;
            if (isPreviewMode) {
                return m.client_visible !== false && !m.internal_only;
            }
            return true;
        });
    };

    const introVideo = getCuratedMedia("video")[0];
    const takes = getCuratedMedia("takes");
    const portfolioImages = getCuratedMedia("image");
    const indianImages = getCuratedMedia("indian");
    const westernImages = getCuratedMedia("western");

    const FIELDS = [
        { key: "first_name", label: "First Name" },
        { key: "last_name", label: "Last Name" },
        { key: "age", label: "Age", type: "number" },
        { key: "height", label: "Height" },
        { key: "location", label: "Location" },
        { key: "competitive_brand", label: "Competitive Brand" },
    ];

    // Status styling maps
    const borderColors = {
        approved: "border-l-4 border-l-green-500",
        rejected: "border-l-4 border-l-red-500",
        hold: "border-l-4 border-l-purple-500",
        updated: "border-l-4 border-l-blue-500",
        pending: "border-l-4 border-l-amber-500",
    };

    const statusBadges = {
        approved: "bg-green-50 text-green-700 border-green-200",
        rejected: "bg-red-50 text-red-700 border-red-200",
        hold: "bg-purple-50 text-purple-700 border-purple-200",
        updated: "bg-blue-50 text-blue-700 border-blue-200",
        pending: "bg-amber-50 text-amber-700 border-amber-200",
    };

    return (
        <div className="flex flex-col h-screen bg-[#fafaf9] text-neutral-800 overflow-hidden font-sans">
            {/* Top Bar Navigation */}
            <header className="px-6 py-4 bg-white border-b border-black/[0.08] flex items-center justify-between shrink-0 shadow-sm">
                <div className="flex items-center gap-4">
                    <button
                        onClick={() => navigate(`/admin/projects/${id}`)}
                        className="p-2 border border-black/[0.08] hover:border-black/[0.16] hover:bg-black/[0.02] rounded-full text-black/60 hover:text-black transition-colors"
                        title="Back to Project Edit"
                    >
                        <ArrowLeft className="w-4 h-4" />
                    </button>
                    <div>
                        <span className="text-[10px] uppercase tracking-wider text-black/40 font-semibold font-mono">Submission Review Center</span>
                        <h1 className="text-xl font-display font-semibold tracking-tight text-black/90">
                            {project?.brand_name || "Loading Project..."}
                        </h1>
                    </div>
                </div>

                {/* Progress Indicators */}
                <div className="flex items-center gap-3">
                    {submissions.length > 0 && (
                        <div className="px-4 py-1.5 bg-black text-white text-xs font-mono font-semibold uppercase tracking-wider rounded-sm shadow-sm animate-pulse-subtle">
                            Reviewing {currentIndex + 1} of {filteredSubmissions.length}
                        </div>
                    )}
                </div>
            </header>

            {/* Main Content Area */}
            <div className="flex flex-1 overflow-hidden relative">
                
                {/* ── LEFT PANEL (SUBMISSION LIST) ── */}
                <aside className={`w-full md:w-[350px] lg:w-[400px] border-r border-black/[0.08] bg-white flex flex-col shrink-0 overflow-hidden transition-all duration-300 ${isMobileDetailOpen ? "hidden md:flex" : "flex"}`}>
                    {/* Search & Filter Bar */}
                    <div className="p-4 border-b border-black/[0.06] space-y-3 bg-[#fafaf9]">
                        <input
                            type="search"
                            placeholder="Search talent name or email..."
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            className="w-full text-xs px-3 py-2 border border-black/[0.08] focus:border-black/40 rounded-lg outline-none bg-white transition-all text-black/85"
                        />
                        <div className="flex gap-1 overflow-x-auto pb-1 scrollbar-thin">
                            {["all", "pending", "approved", "hold", "rejected", "updated"].map((tab) => (
                                <button
                                    key={tab}
                                    type="button"
                                    onClick={() => setFilter(tab)}
                                    className={`text-[10px] font-mono tracking-wider uppercase px-2.5 py-1 rounded-sm border transition-colors shrink-0 ${filter === tab ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20]"}`}
                                >
                                    {tab}
                                </button>
                            ))}
                        </div>
                    </div>

                    {/* Submissions List */}
                    <div className="flex-1 overflow-y-auto divide-y divide-black/[0.06]">
                        {loadingSubmissions ? (
                            <div className="h-40 flex flex-col items-center justify-center text-black/40 gap-2">
                                <Loader2 className="w-6 h-6 animate-spin text-black/60" />
                                <span className="text-xs tracking-wider uppercase font-mono">Loading list...</span>
                            </div>
                        ) : filteredSubmissions.length === 0 ? (
                            <div className="p-8 text-center text-black/35 text-xs">
                                No submissions match filters.
                            </div>
                        ) : (
                             filteredSubmissions.map((s, idx) => {
                                 const isSelected = s.id === selectedId;
                                 const isUpdated = s.status === "updated";
                                 const statusKey = isUpdated ? "updated" : (s.decision || "pending");
                                 const cardBorder = borderColors[statusKey] || "";
                                 
                                 const comp = getCompleteness(s, project);

                                 const hasIntro = s.media?.some(m => m.category === "intro_video" || m.category === "video");
                                 const takesCount = s.media?.filter(m => ["take", "take_1", "take_2", "take_3"].includes(m.category)).length || 0;
                                 const imagesCount = s.media?.filter(m => ["image", "indian", "western"].includes(m.category)).length || 0;

                                 const customAnswers = s.form_data?.custom_answers || {};
                                 const totalQuestions = project?.custom_questions?.length || 0;
                                 const answeredCount = Object.values(customAnswers).filter(val => val !== undefined && val !== null && val !== "").length;

                                 const isRecent = (() => {
                                     const ts = s.submitted_at || s.created_at;
                                     if (!ts) return false;
                                     const diffMs = new Date() - new Date(ts);
                                     return diffMs < 24 * 60 * 60 * 1000;
                                 })();

                                 return (
                                     <div
                                         key={s.id}
                                         onClick={() => handleSelectRow(s.id)}
                                         className={`px-4 py-3.5 cursor-pointer transition-colors flex flex-col gap-2 ${cardBorder} ${isSelected ? "bg-black/[0.02] border-r-2 border-r-black" : "bg-white hover:bg-black/[0.01]"}`}
                                     >
                                         <div className="flex items-center justify-between gap-2">
                                             <span className="font-display font-semibold text-sm text-black/95 truncate">
                                                 {s.talent_name}
                                             </span>
                                             <span className={`text-[8px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 ${comp.color}`}>
                                                 {comp.status}
                                             </span>
                                         </div>
                                         <div className="text-[10px] text-black/45 font-mono truncate">
                                             {s.talent_email}
                                         </div>
                                         <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
                                             {hasIntro ? (
                                                 <span className="inline-flex items-center text-[9px] bg-neutral-100 text-neutral-700 px-1.5 py-0.5 rounded font-mono">
                                                     🎥 Intro Video
                                                 </span>
                                             ) : (
                                                 <span className="inline-flex items-center text-[9px] bg-neutral-50 text-neutral-400 px-1.5 py-0.5 rounded font-mono border border-dashed border-neutral-200">
                                                     🎥 No Video
                                                 </span>
                                             )}

                                             <span className={`inline-flex items-center text-[9px] px-1.5 py-0.5 rounded font-mono ${takesCount > 0 ? "bg-neutral-100 text-neutral-700" : "bg-neutral-50 text-neutral-400 border border-dashed border-neutral-200"}`}>
                                                 🎬 Audition Takes ({takesCount})
                                             </span>

                                             <span className={`inline-flex items-center text-[9px] px-1.5 py-0.5 rounded font-mono ${imagesCount > 0 ? "bg-neutral-100 text-neutral-700" : "bg-neutral-50 text-neutral-400 border border-dashed border-neutral-200"}`}>
                                                 📷 Images ({imagesCount})
                                             </span>

                                             {totalQuestions > 0 && (
                                                 <span className={`inline-flex items-center text-[9px] px-1.5 py-0.5 rounded font-mono ${answeredCount >= totalQuestions ? "bg-neutral-100 text-neutral-700" : "bg-amber-50 text-amber-600 border border-amber-200"}`}>
                                                     📄 Qs ({answeredCount}/{totalQuestions})
                                                 </span>
                                             )}
                                         </div>
                                         <div className="text-[9px] text-black/55 flex items-center justify-between mt-1">
                                             <span>Age: {s.effective_age !== undefined && s.effective_age !== null ? `${s.effective_age} yrs` : "—"}</span>
                                             <div className="flex items-center gap-1">
                                                 {isRecent && (
                                                     <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" title="Recently updated" />
                                                 )}
                                                 <span className={isRecent ? "text-blue-600 font-semibold" : "text-black/45"}>
                                                     🕒 {isRecent ? "Recently Updated" : "Last Updated"}: {formatRelativeTime(s.submitted_at || s.created_at)}
                                                 </span>
                                             </div>
                                         </div>
                                     </div>
                                 );
                             })
                        )}
                    </div>
                </aside>

                {/* ── RIGHT PANEL (CURATED REVIEW PANEL) ── */}
                <main className={`flex-1 flex flex-col bg-white overflow-hidden transition-all duration-300 ${!isMobileDetailOpen ? "hidden md:flex" : "flex"}`}>
                    
                    {/* Detail Panel Sub-header */}
                    <div className="px-6 py-3 border-b border-black/[0.08] bg-[#fafaf9] flex items-center justify-between gap-4 shrink-0">
                        {/* Mobile Back control */}
                        <button
                            onClick={() => setIsMobileDetailOpen(false)}
                            className="flex items-center gap-1.5 text-xs text-black/60 hover:text-black font-semibold md:hidden"
                        >
                            <ChevronLeft className="w-4 h-4" />
                            <span>List</span>
                        </button>

                        {/* Navigation controls */}
                        <div className="flex items-center gap-1">
                            <button
                                onClick={handlePrev}
                                disabled={filteredSubmissions.length <= 1}
                                className="p-1.5 border border-black/[0.08] hover:border-black/[0.20] disabled:opacity-40 disabled:hover:border-black/[0.08] rounded-md transition-all bg-white shadow-sm"
                                title="Previous Submission"
                            >
                                <ChevronLeft className="w-4 h-4" />
                            </button>
                            <button
                                onClick={handleNext}
                                disabled={filteredSubmissions.length <= 1}
                                className="p-1.5 border border-black/[0.08] hover:border-black/[0.20] disabled:opacity-40 disabled:hover:border-black/[0.08] rounded-md transition-all bg-white shadow-sm"
                                title="Next Submission"
                            >
                                <ChevronRight className="w-4 h-4" />
                            </button>
                        </div>

                        {/* Mode selectors */}
                        <div className="flex bg-black/[0.04] p-0.5 rounded-full border border-black/[0.02]">
                            <button
                                type="button"
                                onClick={() => setIsPreviewMode(false)}
                                className={`px-3 py-1 rounded-full text-[10px] font-mono uppercase tracking-wider transition-all duration-200 ${!isPreviewMode ? "bg-white text-black shadow-sm font-semibold" : "text-black/55 hover:text-black"}`}
                            >
                                Recruiter view
                            </button>
                            <button
                                type="button"
                                onClick={() => setIsPreviewMode(true)}
                                className={`px-3 py-1 rounded-full text-[10px] font-mono uppercase tracking-wider transition-all duration-200 ${isPreviewMode ? "bg-amber-500 text-white shadow-sm font-semibold" : "text-black/55 hover:text-black"}`}
                            >
                                Client view
                            </button>
                        </div>
                    </div>

                    {isEndOfList ? (
                        <div className="flex-1 flex flex-col items-center justify-center p-6 bg-white text-center">
                            <div className="max-w-md w-full border border-black/[0.08] bg-[#fafaf9] rounded-xl p-8 shadow-sm space-y-6">
                                <div className="w-16 h-16 bg-green-50 rounded-full flex items-center justify-center mx-auto text-green-600 border border-green-200 animate-bounce">
                                    <Check className="w-8 h-8" />
                                </div>
                                <div className="space-y-2">
                                    <h2 className="text-xl font-display font-semibold text-black/90">All submissions reviewed</h2>
                                    <p className="text-sm text-black/45">You have reached the end of the submission list.</p>
                                </div>
                                <div className="flex flex-col gap-2 pt-2">
                                    <button
                                        onClick={() => {
                                            setIsEndOfList(false);
                                            setIsMobileDetailOpen(false);
                                        }}
                                        className="w-full py-2.5 bg-black hover:bg-black/90 text-white rounded-md text-xs font-semibold shadow-sm transition-all"
                                    >
                                        Return to list
                                    </button>
                                    <button
                                        onClick={() => {
                                            const approvedList = submissions.filter(s => s.decision === "approved");
                                            if (approvedList.length > 0) {
                                                setFilter("approved");
                                                setSelectedId(approvedList[0].id);
                                                setIsEndOfList(false);
                                                setIsMobileDetailOpen(true);
                                            } else {
                                                toast.error("No approved submissions found");
                                            }
                                        }}
                                        className="w-full py-2.5 border border-black/[0.08] hover:border-black/20 text-black/80 rounded-md text-xs font-semibold shadow-sm transition-all bg-white"
                                    >
                                        Review Approved
                                    </button>
                                    <button
                                        onClick={() => {
                                            const pendingList = submissions.filter(s => (s.decision || "pending") === "pending");
                                            if (pendingList.length > 0) {
                                                setFilter("pending");
                                                setSelectedId(pendingList[0].id);
                                                setIsEndOfList(false);
                                                setIsMobileDetailOpen(true);
                                            } else {
                                                toast.error("No pending submissions found");
                                            }
                                        }}
                                        className="w-full py-2.5 border border-black/[0.08] hover:border-black/20 text-black/80 rounded-md text-xs font-semibold shadow-sm transition-all bg-white"
                                    >
                                        Review Pending
                                    </button>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <>
                            {/* Scrollable details view */}
                            <div className="flex-1 overflow-y-auto p-6 space-y-8">
                        {loadingDetail ? (
                            <div className="h-64 flex flex-col items-center justify-center text-black/40 gap-3">
                                <Loader2 className="w-8 h-8 animate-spin text-black/80" />
                                <span className="text-xs uppercase tracking-widest font-mono">Loading curation details...</span>
                            </div>
                        ) : !detail ? (
                            <div className="h-64 flex flex-col items-center justify-center text-black/35 text-sm">
                                Select a submission to begin reviewing.
                            </div>
                        ) : (
                            <div className="max-w-4xl mx-auto space-y-8">
                                
                                {/* Talent Headline */}
                                <div className="border-b border-black/[0.06] pb-5">
                                    <div className="flex items-center gap-3 flex-wrap">
                                        <h2 className="text-2xl md:text-3xl font-display font-semibold text-black/90">
                                            {detail.talent_name}
                                        </h2>
                                        <span className={`inline-flex items-center text-[9px] font-mono tracking-widest uppercase px-2.5 py-0.5 rounded border ${statusBadges[detail.decision || "pending"]}`}>
                                            {detail.decision || "pending"}
                                        </span>
                                    </div>
                                    <div className="mt-1.5 text-xs text-black/45 font-mono flex items-center gap-3 flex-wrap">
                                        <span className="flex items-center gap-1"><Mail className="w-3.5 h-3.5" /> {detail.talent_email}</span>
                                        {detail.talent_phone && <span className="flex items-center gap-1"><Phone className="w-3.5 h-3.5" /> {detail.talent_phone}</span>}
                                    </div>
                                </div>

                                {/* Curators External Integration actions */}
                                {!isPreviewMode && (
                                    <div className="flex flex-wrap gap-2 pt-1">
                                        <button
                                            type="button"
                                            onClick={openInDrive}
                                            className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider px-3.5 py-2 border border-black/[0.08] hover:border-black/40 rounded-sm bg-white text-black/70 hover:text-black transition-all shadow-sm"
                                        >
                                            <Cloud className="w-3.5 h-3.5" />
                                            Open Google Drive folder
                                        </button>
                                        {detail.decision === "approved" && (
                                            <button
                                                type="button"
                                                onClick={regenerateSnapshot}
                                                className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider px-3.5 py-2 border border-green-600/20 hover:border-green-600/40 rounded-sm bg-green-50 text-green-700 hover:text-green-800 transition-all font-semibold shadow-sm"
                                            >
                                                <RefreshCw className="w-3.5 h-3.5 animate-spin-hover" />
                                                Regenerate Client Package
                                            </button>
                                        )}
                                    </div>
                                )}

                                {/* Profile detail blocks */}
                                <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm space-y-6">
                                    <p className="eyebrow mb-2">Talent Profile Details</p>
                                    
                                    {isPreviewMode ? (
                                        <div className="grid grid-cols-2 md:grid-cols-3 gap-6 py-2">
                                            {FIELDS.filter(f => fv[f.key] !== false).map(f => (
                                                <div key={f.key} className="min-w-0">
                                                    <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">{f.label}</p>
                                                    <p className="text-sm font-medium text-black/85">{form[f.key] || "—"}</p>
                                                </div>
                                            ))}
                                        </div>
                                    ) : (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
                                            {FIELDS.map((f) => (
                                                <div key={f.key} className="flex items-start gap-3 bg-[#fafaf9] p-3 rounded-lg border border-black/[0.03]">
                                                    <div className="flex-1 min-w-0">
                                                        <label className="text-[10px] text-black/45 tracking-widest uppercase">
                                                            {f.label}
                                                        </label>
                                                        <input
                                                            type={f.type || "text"}
                                                            value={form[f.key] ?? ""}
                                                            onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                                                            className="mt-1 w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 font-medium"
                                                        />
                                                    </div>
                                                    <button
                                                        type="button"
                                                        onClick={() => setFv({ ...fv, [f.key]: !fv[f.key] })}
                                                        title={fv[f.key] ? "Visible to client" : "Hidden from client"}
                                                        className={`mt-4 w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv[f.key] ? "bg-black" : "bg-black/15"}`}
                                                    >
                                                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv[f.key] ? "translate-x-4 bg-white" : "bg-black"}`} />
                                                    </button>
                                                </div>
                                            ))}

                                            {/* Structured Availability */}
                                            <div className="md:col-span-2 border-t border-black/[0.08] pt-4 mt-2 space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <label className="text-[10px] text-black/45 tracking-widest uppercase">Availability</label>
                                                    <button
                                                        type="button"
                                                        onClick={() => setFv({ ...fv, availability: fv.availability === false })}
                                                        className={`w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv.availability !== false ? "bg-black" : "bg-black/15"}`}
                                                    >
                                                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.availability !== false ? "translate-x-4 bg-white" : "bg-black"}`} />
                                                    </button>
                                                </div>
                                                <div className="flex items-center gap-3">
                                                    <select
                                                        value={form.availability?.status || ""}
                                                        onChange={(e) => setForm({
                                                            ...form,
                                                            availability: { ...form.availability, status: e.target.value }
                                                        })}
                                                        className="bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1.5 text-sm text-black/85 font-medium"
                                                    >
                                                        <option value="">—</option>
                                                        {AVAILABILITY_OPTIONS.map((opt) => (
                                                            <option key={opt.key} value={opt.key}>{opt.label}</option>
                                                        ))}
                                                    </select>
                                                    <input
                                                        type="text"
                                                        value={form.availability?.note || ""}
                                                        onChange={(e) => setForm({
                                                            ...form,
                                                            availability: { ...form.availability, note: e.target.value }
                                                        })}
                                                        placeholder="Note / reason"
                                                        className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 placeholder:text-black/30 font-medium"
                                                    />
                                                </div>
                                            </div>

                                            {/* Structured Budget */}
                                            <div className="md:col-span-2 space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <label className="text-[10px] text-black/45 tracking-widest uppercase">Budget</label>
                                                    <button
                                                        type="button"
                                                        onClick={() => setFv({ ...fv, budget: !fv.budget })}
                                                        className={`w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv.budget ? "bg-black" : "bg-black/15"}`}
                                                    >
                                                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.budget ? "translate-x-4 bg-white" : "bg-black"}`} />
                                                    </button>
                                                </div>
                                                <div className="flex items-center gap-3">
                                                    <select
                                                        value={form.budget?.status || ""}
                                                        onChange={(e) => setForm({
                                                            ...form,
                                                            budget: { ...form.budget, status: e.target.value }
                                                        })}
                                                        className="bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1.5 text-sm text-black/85 font-medium"
                                                    >
                                                        <option value="">—</option>
                                                        {BUDGET_OPTIONS.map((opt) => (
                                                            <option key={opt.key} value={opt.key}>{opt.label}</option>
                                                        ))}
                                                    </select>
                                                    <input
                                                        type="text"
                                                        value={form.budget?.value || ""}
                                                        onChange={(e) => setForm({
                                                            ...form,
                                                            budget: { ...form.budget, value: e.target.value }
                                                        })}
                                                        placeholder="Expected budget (if custom)"
                                                        className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 placeholder:text-black/30 font-medium"
                                                    />
                                                </div>
                                            </div>
                                        </div>
                                    )}

                                    {/* Client Mode Availability / Budget Display */}
                                    {isPreviewMode && (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 border-t border-black/[0.08] pt-4">
                                            {fv.availability !== false && form.availability?.status && (
                                                <div>
                                                    <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">Availability</p>
                                                    <p className="text-sm font-medium text-black/85">
                                                        {form.availability?.status === "available" ? "🟢 Available" : "🔴 Unavailable"} 
                                                        {form.availability?.note ? ` — ${form.availability.note}` : ""}
                                                    </p>
                                                </div>
                                            )}
                                            {fv.budget && form.budget?.status && (
                                                <div>
                                                    <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">Budget</p>
                                                    <p className="text-sm font-medium text-black/85">
                                                        {form.budget?.status === "accept" ? "🟢 Accepts Day Rate" : "🔴 Expected Day Rate"} 
                                                        {form.budget?.value ? ` — ${form.budget.value}` : ""}
                                                    </p>
                                                </div>
                                            )}
                                        </div>
                                    )}

                                    {/* Custom Question Answers */}
                                    {Array.isArray(project?.custom_questions) && project.custom_questions.length > 0 && (
                                        <div className="border-t border-black/[0.08] pt-5 mt-4 space-y-4">
                                            <p className="eyebrow text-black/75">Application answers</p>
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                                {project.custom_questions.map((q) => (
                                                    <div key={q.id} className="text-sm">
                                                        <div className="text-black/45 text-[10px] mb-1 uppercase tracking-wider font-semibold">{q.question}</div>
                                                        <div className="text-black/85 font-medium">
                                                            {(form.custom_answers || {})[q.id] || "—"}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}

                                    {/* Action save curations */}
                                    {!isPreviewMode && (
                                        <div className="flex justify-end pt-2">
                                            <button
                                                type="button"
                                                onClick={handleSaveCuration}
                                                disabled={saving}
                                                className="inline-flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-md text-xs font-semibold hover:bg-black/90 transition-colors shadow-sm"
                                            >
                                                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                                                Save Curations
                                            </button>
                                        </div>
                                    )}
                                </section>

                                {/* ── MEDIA WORKSPACE ── */}

                                {/* Section 1: Intro Video */}
                                {(!isPreviewMode || introVideo) && (
                                    <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm">
                                        <p className="eyebrow mb-4 border-b border-black/[0.05] pb-3">Introduction Video</p>
                                        {introVideo ? (
                                            <div className="max-w-2xl">
                                                <PremiumVideoPlayer
                                                    src={introVideo.url}
                                                    poster={introVideo.poster_url}
                                                    label="Intro Tape"
                                                />
                                            </div>
                                        ) : (
                                            <div className="border border-dashed border-black/[0.08] bg-[#fafaf9] aspect-video flex items-center justify-center text-black/45 text-xs font-mono rounded-lg">
                                                Not submitted
                                            </div>
                                        )}
                                    </section>
                                )}

                                {/* Section 2: Audition Takes */}
                                {(!isPreviewMode || takes.length > 0) && (
                                    <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm">
                                        <p className="eyebrow mb-4 border-b border-black/[0.05] pb-3">Audition Takes ({takes.length})</p>
                                        {takes.length === 0 ? (
                                            <div className="border border-dashed border-black/[0.08] bg-[#fafaf9] h-36 flex items-center justify-center text-black/45 text-xs font-mono rounded-lg">
                                                No takes submitted
                                            </div>
                                        ) : (
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                                {takes.map((t, idx) => (
                                                    <div key={t.id} className="relative group bg-[#fafaf9] p-3 border border-black/[0.06] rounded-xl">
                                                        <div className="mb-2.5 flex items-center justify-between">
                                                            <span className="text-[10px] font-bold font-mono uppercase tracking-wider text-black/60">
                                                                {t.label || `Take ${idx + 1}`}
                                                            </span>
                                                            <div className="flex gap-1">
                                                                {t.client_visible === false && (
                                                                    <span className="text-[8px] bg-red-100 text-red-700 px-1.5 py-0.5 rounded font-mono uppercase tracking-wider">Hidden</span>
                                                                )}
                                                                {t.internal_only && (
                                                                    <span className="text-[8px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-mono uppercase tracking-wider">Internal</span>
                                                                )}
                                                                {t.primary_take && (
                                                                    <span className="text-[8px] bg-amber-500 text-white px-1.5 py-0.5 rounded font-bold uppercase tracking-wider">Primary</span>
                                                                )}
                                                            </div>
                                                        </div>
                                                        <PremiumVideoPlayer
                                                            src={t.url}
                                                            poster={t.poster_url}
                                                            isPrimary={t.primary_take}
                                                            label={t.label || `Take ${idx + 1}`}
                                                        />
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </section>
                                )}

                                {/* Section 3: Indian Look Images */}
                                {(!isPreviewMode || indianImages.length > 0) && (
                                    <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm">
                                        <p className="eyebrow mb-4 border-b border-black/[0.05] pb-3">Indian Look Images ({indianImages.length})</p>
                                        {indianImages.length === 0 ? (
                                            <div className="border border-dashed border-black/[0.08] bg-[#fafaf9] h-28 flex items-center justify-center text-black/45 text-xs font-mono rounded-lg">
                                                No Indian look images
                                            </div>
                                        ) : (
                                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                                                {indianImages.map((m) => (
                                                    <div key={m.id} className="relative aspect-square overflow-hidden border border-black/[0.06] rounded-lg bg-[#fafaf9]">
                                                        <img src={m.url} alt="" className="w-full h-full object-cover" />
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </section>
                                )}

                                {/* Section 4: Western Look Images */}
                                {(!isPreviewMode || westernImages.length > 0) && (
                                    <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm">
                                        <p className="eyebrow mb-4 border-b border-black/[0.05] pb-3">Western Look Images ({westernImages.length})</p>
                                        {westernImages.length === 0 ? (
                                            <div className="border border-dashed border-black/[0.08] bg-[#fafaf9] h-28 flex items-center justify-center text-black/45 text-xs font-mono rounded-lg">
                                                No Western look images
                                            </div>
                                        ) : (
                                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                                                {westernImages.map((m) => (
                                                    <div key={m.id} className="relative aspect-square overflow-hidden border border-black/[0.06] rounded-lg bg-[#fafaf9]">
                                                        <img src={m.url} alt="" className="w-full h-full object-cover" />
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </section>
                                )}
                            </div>
                        )}
                    </div>

                    {/* Sticky Decision Footer */}
                    {detail && !isPreviewMode && (
                        <footer className="px-6 py-4 bg-white border-t border-black/[0.08] shrink-0 flex flex-col gap-3 shadow-lg z-20">
                            <div className="w-full">
                                <input
                                    type="text"
                                    value={decisionNote}
                                    onChange={(e) => setDecisionNote(e.target.value)}
                                    placeholder="Add a decision note or internal comment..."
                                    className="w-full text-xs px-3.5 py-2 border border-black/[0.08] focus:border-black/40 rounded-lg outline-none bg-black/[0.02] focus:bg-white transition-all text-black/85"
                                />
                            </div>
                            <div className="flex items-center justify-between gap-4 flex-wrap">
                                <span className="text-[10px] uppercase font-mono tracking-wider text-black/45">Review decision actions:</span>
                                <div className="flex gap-2">
                                    <button
                                        onClick={() => handleDecision("approved")}
                                        disabled={saving}
                                        className="inline-flex items-center gap-1.5 px-4 py-2.5 bg-green-600 hover:bg-green-700 text-white rounded-md text-xs font-bold transition-all shadow-sm"
                                    >
                                        <Check className="w-4 h-4" /> Approve
                                    </button>
                                    <button
                                        onClick={() => handleDecision("hold")}
                                        disabled={saving}
                                        className="inline-flex items-center gap-1.5 px-4 py-2.5 border border-purple-500 text-purple-600 hover:bg-purple-50 rounded-md text-xs font-bold transition-all bg-white shadow-sm"
                                    >
                                        <PauseCircle className="w-4 h-4" /> Hold
                                    </button>
                                    <button
                                        onClick={() => handleDecision("rejected")}
                                        disabled={saving}
                                        className="inline-flex items-center gap-1.5 px-4 py-2.5 border border-red-500 text-red-600 hover:bg-red-50 rounded-md text-xs font-bold transition-all bg-white shadow-sm"
                                    >
                                        <XCircle className="w-4 h-4" /> Reject
                                    </button>
                                </div>
                            </div>
                        </footer>
                    )}
                        </>
                    )}
                </main>
            </div>
        </div>
    );
}
