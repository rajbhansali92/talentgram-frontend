import React, { useState, useEffect, useCallback, useRef, useMemo } from "react";
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
    Mail,
    Upload,
    Trash2,
    Plus
} from "lucide-react";
import { AVAILABILITY_OPTIONS, BUDGET_OPTIONS } from "@/lib/talentSchema";
import LocationSelector from "@/components/LocationSelector";

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

    useEffect(() => {
        setIsPlaying(false);
        const video = videoRef.current;
        return () => {
            if (video) {
                try {
                    video.pause();
                    video.removeAttribute("src");
                    video.load();
                } catch (e) {
                    // ignore
                }
            }
        };
    }, [src]);

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
    const [debouncedSearchQuery, setDebouncedSearchQuery] = useState("");
    
    useEffect(() => {
        const handler = setTimeout(() => {
            setDebouncedSearchQuery(searchQuery);
        }, 200);
        return () => clearTimeout(handler);
    }, [searchQuery]);
    const [isMobileDetailOpen, setIsMobileDetailOpen] = useState(false);
    const [isEndOfList, setIsEndOfList] = useState(false);
    const [savedProgressId, setSavedProgressId] = useState(null);
    const [showResumePrompt, setShowResumePrompt] = useState(false);
    const [visibleCount, setVisibleCount] = useState(50);
    
    // Advanced Filters & Sorting states
    const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
    const [hasIntroFilter, setHasIntroFilter] = useState(false);
    const [hasTakesFilter, setHasTakesFilter] = useState(false);
    const [hasImagesFilter, setHasImagesFilter] = useState(false);
    const [completenessFilter, setCompletenessFilter] = useState("all");
    const [recentlyUpdatedFilter, setRecentlyUpdatedFilter] = useState(false);
    const [sortBy, setSortBy] = useState("newest");
    
    // Curation / Decision states
    const [decisionNote, setDecisionNote] = useState("");
    const [form, setForm] = useState({});
    const [fv, setFv] = useState({});
    const [mediaList, setMediaList] = useState([]);
    const [isPreviewMode, setIsPreviewMode] = useState(false);
    const [saving, setSaving] = useState(false);

    // Admin media upload state
    const adminMediaInputRef = useRef(null);
    const [adminMediaUploading, setAdminMediaUploading] = useState(false);
    const [adminMediaCategory, setAdminMediaCategory] = useState("image");

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
            
            // Check if there is saved progress
            const savedId = localStorage.getItem(`review_center_progress_${id}`);
            const hasSaved = savedId && data.some(s => s.id === savedId);
            
            if (hasSaved) {
                setSavedProgressId(savedId);
                setShowResumePrompt(true);
            } else {
                setSelectedId(prev => {
                    if (!prev && data.length > 0) {
                        return data[0].id;
                    }
                    return prev;
                });
            }
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

    // Auto-save progress when selectedId changes
    useEffect(() => {
        if (selectedId && id) {
            localStorage.setItem(`review_center_progress_${id}`, selectedId);
        }
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

    const handleAdminMediaUpload = async (file) => {
        if (!file || !selectedId) return;
        setAdminMediaUploading(true);
        try {
            const formData = new FormData();
            formData.append("file", file);
            formData.append("category", adminMediaCategory);
            const { data } = await adminApi.post(
                `/projects/${id}/submissions/${selectedId}/admin-media`,
                formData,
                { headers: { "Content-Type": "multipart/form-data" } }
            );
            setDetail(data);
            setMediaList(data?.media || []);
            toast.success("Media uploaded successfully");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to upload media");
        } finally {
            setAdminMediaUploading(false);
        }
    };

    const handleRemoveMedia = async (mediaId) => {
        if (!selectedId || !window.confirm("Remove this media item?")) return;
        setSaving(true);
        try {
            const { data } = await adminApi.delete(
                `/projects/${id}/submissions/${selectedId}/media/${mediaId}`
            );
            setDetail(data);
            setMediaList(data?.media || []);
            toast.success("Media removed");
        } catch (e) {
            toast.error("Failed to remove media");
        } finally {
            setSaving(false);
        }
    };

    // Reset visible count when filters or sorting change
    useEffect(() => {
        setVisibleCount(50);
    }, [filter, debouncedSearchQuery, hasIntroFilter, hasTakesFilter, hasImagesFilter, completenessFilter, recentlyUpdatedFilter, sortBy, submissions]);

    // Filtered lists
    const filteredSubmissions = useMemo(() => {
        return submissions
            .filter((s) => {
                // Status tabs
                if (filter !== "all") {
                    if (filter === "updated" && s.status !== "updated") return false;
                    if (filter !== "updated" && (s.decision || "pending") !== filter) return false;
                }
                // Search query
                if (debouncedSearchQuery.trim()) {
                    const q = debouncedSearchQuery.toLowerCase();
                    const nameMatch = (s.talent_name || "").toLowerCase().includes(q);
                    const emailMatch = (s.talent_email || "").toLowerCase().includes(q);
                    if (!nameMatch && !emailMatch) return false;
                }

                // Has Intro Video
                if (hasIntroFilter) {
                    const hasIntro = s.media?.some(m => m.category === "intro_video" || m.category === "video");
                    if (!hasIntro) return false;
                }

                // Has Audition Takes
                if (hasTakesFilter) {
                    const takesCount = s.media?.filter(m => ["take", "take_1", "take_2", "take_3"].includes(m.category)).length || 0;
                    if (takesCount === 0) return false;
                }

                // Has Images
                if (hasImagesFilter) {
                    const imagesCount = s.media?.filter(m => ["image", "indian", "western"].includes(m.category)).length || 0;
                    if (imagesCount === 0) return false;
                }

                // Completeness: 'all', 'complete', 'incomplete'
                if (completenessFilter !== "all") {
                    const comp = getCompleteness(s, project);
                    if (completenessFilter === "complete" && comp.status !== "Complete") return false;
                    if (completenessFilter === "incomplete" && comp.status === "Complete") return false;
                }

                // Recently Updated (updated in last 24 hours)
                if (recentlyUpdatedFilter) {
                    const ts = s.submitted_at || s.created_at;
                    if (!ts) return false;
                    const diffMs = new Date() - new Date(ts);
                    const isRecent = diffMs < 24 * 60 * 60 * 1000;
                    if (!isRecent) return false;
                }

                return true;
            })
            .sort((a, b) => {
                if (sortBy === "newest") {
                    return new Date(b.created_at || 0) - new Date(a.created_at || 0);
                }
                if (sortBy === "oldest") {
                    return new Date(a.created_at || 0) - new Date(b.created_at || 0);
                }
                if (sortBy === "recently_updated") {
                    const tA = a.submitted_at || a.created_at || 0;
                    const tB = b.submitted_at || b.created_at || 0;
                    return new Date(tB) - new Date(tA);
                }
                if (sortBy === "most_complete") {
                    const aMedia = a.media?.length || 0;
                    const bMedia = b.media?.length || 0;
                    return bMedia - aMedia;
                }
                if (sortBy === "age") {
                    const ageA = a.effective_age || 0;
                    const ageB = b.effective_age || 0;
                    return ageA - ageB;
                }
                if (sortBy === "location") {
                    const getLocStr = (s) => {
                        const loc = s.form_data?.location;
                        if (Array.isArray(loc)) {
                            return loc[0]?.city || "";
                        }
                        return loc || "";
                    };
                    const locA = getLocStr(a).toLowerCase();
                    const locB = getLocStr(b).toLowerCase();
                    return locA.localeCompare(locB);
                }
                return 0;
            });
    }, [submissions, filter, debouncedSearchQuery, hasIntroFilter, hasTakesFilter, hasImagesFilter, completenessFilter, recentlyUpdatedFilter, sortBy, project]);

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

    const PERSONAL_FIELDS = [
        { key: "first_name", label: "First Name" },
        { key: "last_name", label: "Last Name" },
        { key: "age", label: "Age", type: "number" },
        { key: "height", label: "Height" },
        { key: "location", label: "Location" },
        { key: "gender", label: "Gender" },
        { key: "ethnicity", label: "Ethnicity" },
        { key: "instagram_handle", label: "Instagram Handle" },
        { key: "instagram_followers", label: "Instagram Followers" },
    ];

    const PROFESSIONAL_FIELDS = [
        { key: "languages", label: "Languages" },
        { key: "skills", label: "Skills" },
        { key: "special_abilities", label: "Special Abilities" },
        { key: "competitive_brand", label: "Competitive Brand" },
    ];

    // Combined for preview-mode grid and any backward-compat references
    const FIELDS = [...PERSONAL_FIELDS, ...PROFESSIONAL_FIELDS];

    /** Renders a single editable field row with visibility toggle + override indicator. */
    const renderFieldRow = (f) => {
        const isArrayVal = Array.isArray(form[f.key]);
        const displayVal = isArrayVal ? form[f.key].join(", ") : (form[f.key] ?? "");
        // Show override indicator when original_form_data exists and value has been changed
        const origVal = detail?.original_form_data?.[f.key];
        const hasOverride = origVal !== undefined && JSON.stringify(origVal) !== JSON.stringify(form[f.key]);
        return (
            <div key={f.key} className={`flex items-start gap-3 p-3 rounded-lg border ${hasOverride ? "border-amber-200 bg-amber-50/40" : "bg-[#fafaf9] border-black/[0.03]"}`}>
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                        <label className="text-[10px] text-black/45 tracking-widest uppercase">{f.label}</label>
                        {hasOverride && (
                            <span className="text-[8px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-mono uppercase tracking-wider">Override</span>
                        )}
                    </div>
                    {f.key === "location" ? (
                        <div className="mt-1">
                            <LocationSelector
                                value={Array.isArray(form.location) ? form.location : []}
                                onChange={(arr) => setForm({ ...form, location: arr })}
                                testid="form-location"
                            />
                        </div>
                    ) : (
                        <input
                            type={f.type || "text"}
                            value={displayVal}
                            onChange={(e) => {
                                const val = e.target.value;
                                if (isArrayVal) {
                                    setForm({ ...form, [f.key]: val.split(",").map(s => s.trim()).filter(Boolean) });
                                } else {
                                    setForm({ ...form, [f.key]: val });
                                }
                            }}
                            className="mt-1 w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 font-medium"
                        />
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => setFv({ ...fv, [f.key]: fv[f.key] === false ? undefined : false })}
                    title={fv[f.key] !== false ? "Visible to client" : "Hidden from client"}
                    className={`mt-4 w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv[f.key] !== false ? "bg-black" : "bg-black/15"}`}
                >
                    <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv[f.key] !== false ? "translate-x-4 bg-white" : "bg-black"}`} />
                </button>
            </div>
        );
    };

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
                            {isEndOfList
                                ? "All Submissions Reviewed"
                                : filteredSubmissions.length === 0
                                ? "No matching submissions"
                                : currentIndex !== -1
                                ? `Reviewing ${currentIndex + 1} of ${filteredSubmissions.length}`
                                : `Submissions (${filteredSubmissions.length})`
                            }
                        </div>
                    )}
                </div>
            </header>

            {/* Main Content Area */}
            <div className="flex flex-1 overflow-hidden relative">
                
                {/* ── LEFT PANEL (SUBMISSION LIST) ── */}
                <aside className={`w-full md:w-[35%] border-r border-black/[0.08] bg-white flex flex-col shrink-0 overflow-hidden transition-all duration-300 ${isMobileDetailOpen ? "hidden md:flex" : "flex"}`}>
                    {/* Search & Filter Bar */}
                    <div className="p-4 border-b border-black/[0.06] space-y-3 bg-[#fafaf9]">
                        <div className="flex items-center gap-2">
                            <input
                                type="search"
                                placeholder="Search name or email..."
                                value={searchQuery}
                                onChange={(e) => setSearchQuery(e.target.value)}
                                className="flex-1 text-xs px-3 py-2 border border-black/[0.08] focus:border-black/40 rounded-lg outline-none bg-white transition-all text-black/85"
                            />
                            <button
                                type="button"
                                onClick={() => setShowAdvancedFilters(!showAdvancedFilters)}
                                className={`px-2.5 py-2 border rounded-lg text-xs font-semibold transition-all shrink-0 ${showAdvancedFilters ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.16] bg-white"}`}
                                title="Toggle Advanced Filters"
                            >
                                ⚙️ Filters
                            </button>
                        </div>

                        {showAdvancedFilters && (
                            <div className="border border-black/[0.08] bg-white rounded-lg p-3 space-y-3.5 animate-in slide-in-from-top-2 duration-200">
                                {/* Sort Dropdown */}
                                <div className="space-y-1">
                                    <label className="text-[9px] uppercase font-mono tracking-wider text-black/45 block">Sort By</label>
                                    <select
                                        value={sortBy}
                                        onChange={(e) => setSortBy(e.target.value)}
                                        className="w-full text-xs px-2.5 py-1.5 border border-black/[0.08] rounded-md outline-none bg-[#fafaf9] text-black/80"
                                    >
                                        <option value="newest">Newest First</option>
                                        <option value="oldest">Oldest First</option>
                                        <option value="recently_updated">Recently Updated First</option>
                                        <option value="most_complete">Most Complete First</option>
                                        <option value="age">Age (Youngest First)</option>
                                        <option value="location">Location (A-Z)</option>
                                    </select>
                                </div>

                                {/* Completeness Select */}
                                <div className="space-y-1">
                                    <label className="text-[9px] uppercase font-mono tracking-wider text-black/45 block">Completeness</label>
                                    <select
                                        value={completenessFilter}
                                        onChange={(e) => setCompletenessFilter(e.target.value)}
                                        className="w-full text-xs px-2.5 py-1.5 border border-black/[0.08] rounded-md outline-none bg-[#fafaf9] text-black/80"
                                    >
                                        <option value="all">All Completeness States</option>
                                        <option value="complete">Complete Only</option>
                                        <option value="incomplete">Incomplete Only</option>
                                    </select>
                                </div>

                                {/* Boolean Toggles */}
                                <div className="grid grid-cols-2 gap-2 pt-1.5 border-t border-black/[0.04]">
                                    <label className="flex items-center gap-1.5 text-xs text-black/70 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={hasIntroFilter}
                                            onChange={(e) => setHasIntroFilter(e.target.checked)}
                                            className="rounded border-black/[0.15] text-black focus:ring-black w-3.5 h-3.5"
                                        />
                                        <span>Has Intro Video</span>
                                    </label>
                                    <label className="flex items-center gap-1.5 text-xs text-black/70 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={hasTakesFilter}
                                            onChange={(e) => setHasTakesFilter(e.target.checked)}
                                            className="rounded border-black/[0.15] text-black focus:ring-black w-3.5 h-3.5"
                                        />
                                        <span>Has Takes</span>
                                    </label>
                                    <label className="flex items-center gap-1.5 text-xs text-black/70 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={hasImagesFilter}
                                            onChange={(e) => setHasImagesFilter(e.target.checked)}
                                            className="rounded border-black/[0.15] text-black focus:ring-black w-3.5 h-3.5"
                                        />
                                        <span>Has Images</span>
                                    </label>
                                    <label className="flex items-center gap-1.5 text-xs text-black/70 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={recentlyUpdatedFilter}
                                            onChange={(e) => setRecentlyUpdatedFilter(e.target.checked)}
                                            className="rounded border-black/[0.15] text-black focus:ring-black w-3.5 h-3.5"
                                        />
                                        <span>Recently Updated</span>
                                    </label>
                                </div>

                                {/* Reset button if any filter is active */}
                                {(hasIntroFilter || hasTakesFilter || hasImagesFilter || completenessFilter !== "all" || recentlyUpdatedFilter || sortBy !== "newest") && (
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setHasIntroFilter(false);
                                            setHasTakesFilter(false);
                                            setHasImagesFilter(false);
                                            setCompletenessFilter("all");
                                            setRecentlyUpdatedFilter(false);
                                            setSortBy("newest");
                                        }}
                                        className="w-full text-center text-[10px] uppercase tracking-wider font-mono text-red-500 hover:text-red-600 pt-1"
                                    >
                                        Clear Advanced Filters
                                    </button>
                                )}
                            </div>
                        )}
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
                            <>
                                 {filteredSubmissions.slice(0, visibleCount).map((s, idx) => {
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
                                             <div className="flex items-center justify-between gap-2 flex-wrap">
                                                 <span className="font-display font-semibold text-sm text-black/95 truncate">
                                                     {s.talent_name}
                                                 </span>
                                                 <div className="flex gap-1 shrink-0">
                                                     {/* Primary: Decision status */}
                                                     <span className={`text-[8px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border ${statusBadges[s.decision || "pending"]}`}>
                                                         {s.decision || "pending"}
                                                     </span>
                                                     {/* Secondary: Completeness */}
                                                     <span className={`text-[8px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border ${comp.color}`}>
                                                         {comp.status}
                                                     </span>
                                                 </div>
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
                                 })}
                                 {filteredSubmissions.length > visibleCount && (
                                     <div className="p-4 text-center">
                                         <button
                                             type="button"
                                             onClick={() => setVisibleCount((prev) => prev + 50)}
                                             className="px-4 py-2 border border-black/[0.08] hover:border-black/35 rounded-lg text-xs font-semibold shadow-sm transition-all bg-white select-none active:scale-[0.98]"
                                         >
                                             Load More (+50)
                                         </button>
                                     </div>
                                 )}
                            </>
                        )}
                    </div>
                </aside>

                {/* ── RIGHT PANEL (CURATED REVIEW PANEL) ── */}
                <main className={`flex-1 flex flex-col bg-white overflow-hidden transition-all duration-300 ${!isMobileDetailOpen ? "hidden md:flex" : "flex"} relative`}>
                    {showResumePrompt && (
                        <div className="absolute inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-6 z-50">
                            <div className="max-w-sm w-full bg-white border border-black/[0.08] rounded-xl p-6 shadow-xl space-y-6 text-center animate-in fade-in zoom-in-95 duration-200">
                                <div className="w-12 h-12 bg-amber-50 rounded-full flex items-center justify-center mx-auto text-amber-600 border border-amber-200">
                                    <Clock className="w-6 h-6 animate-pulse" />
                                </div>
                                <div className="space-y-1">
                                    <h3 className="text-md font-display font-semibold text-black/95">Resume Review?</h3>
                                    <p className="text-xs text-black/55">You have a saved review progress from your last session.</p>
                                </div>
                                <div className="flex flex-col gap-2">
                                    <button
                                        onClick={() => {
                                            setSelectedId(savedProgressId);
                                            setShowResumePrompt(false);
                                            setIsEndOfList(false);
                                            setIsMobileDetailOpen(true);
                                            toast.info("Resumed review progress");
                                        }}
                                        className="w-full py-2 bg-black hover:bg-black/90 text-white rounded-md text-xs font-semibold shadow-sm transition-all"
                                    >
                                        Resume Review
                                    </button>
                                    <button
                                        onClick={() => {
                                            if (submissions.length > 0) {
                                                setSelectedId(submissions[0].id);
                                                localStorage.setItem(`review_center_progress_${id}`, submissions[0].id);
                                            }
                                            setShowResumePrompt(false);
                                            setIsEndOfList(false);
                                            toast.info("Started from beginning");
                                        }}
                                        className="w-full py-2 border border-black/[0.08] hover:border-black/20 text-black/80 rounded-md text-xs font-semibold shadow-sm transition-all bg-white"
                                    >
                                        Start From Beginning
                                    </button>
                                </div>
                            </div>
                        </div>
                    )}
                    
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

                                {/* ── SECTION A: Personal Information ── */}
                                <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm space-y-4">
                                    <div className="flex items-start justify-between border-b border-black/[0.05] pb-3 gap-4">
                                        <p className="eyebrow">Personal Information</p>
                                        {!isPreviewMode && (
                                            <span className="text-[9px] text-black/35 font-mono text-right shrink-0">Project-specific overrides · Master profile unchanged</span>
                                        )}
                                    </div>
                                    {isPreviewMode ? (
                                        <div className="grid grid-cols-2 md:grid-cols-3 gap-6 py-2">
                                            {PERSONAL_FIELDS.filter(f => fv[f.key] !== false).map(f => {
                                                let val = form[f.key];
                                                if (f.key === "location" && Array.isArray(val)) {
                                                    val = val.map(l => `${l.city}, ${l.country}`).join("; ");
                                                } else if (Array.isArray(val)) {
                                                    val = val.join(", ");
                                                }
                                                return (
                                                    <div key={f.key} className="min-w-0">
                                                        <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">{f.label}</p>
                                                        <p className="text-sm font-medium text-black/85">{val || "—"}</p>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    ) : (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3">
                                            {PERSONAL_FIELDS.map((f) => renderFieldRow(f))}
                                        </div>
                                    )}
                                </section>

                                {/* ── SECTION B: Professional Information ── */}
                                <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm space-y-4">
                                    <p className="eyebrow border-b border-black/[0.05] pb-3">Professional Information</p>
                                    {isPreviewMode ? (
                                        <div className="grid grid-cols-2 md:grid-cols-3 gap-6 py-2">
                                            {PROFESSIONAL_FIELDS.filter(f => fv[f.key] !== false).map(f => {
                                                let val = form[f.key];
                                                if (Array.isArray(val)) val = val.join(", ");
                                                return (
                                                    <div key={f.key} className="min-w-0">
                                                        <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">{f.label}</p>
                                                        <p className="text-sm font-medium text-black/85">{val || "—"}</p>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    ) : (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3">
                                            {PROFESSIONAL_FIELDS.map((f) => renderFieldRow(f))}
                                        </div>
                                    )}
                                </section>

                                {/* ── SECTION C: Project Information ── */}
                                <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm space-y-6">
                                    <p className="eyebrow border-b border-black/[0.05] pb-3">Project Information</p>

                                    {/* Availability + Budget — preview vs edit */}
                                    {isPreviewMode ? (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                            {fv.availability !== false && form.availability?.status && (
                                                <div>
                                                    <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">Availability</p>
                                                    <p className="text-sm font-medium text-black/85">
                                                        {form.availability?.status === "yes" ? "🟢 Available" : "🔴 Unavailable"}
                                                        {form.availability?.note ? ` — ${form.availability.note}` : ""}
                                                    </p>
                                                </div>
                                            )}
                                            {fv.budget !== false && form.budget?.status && (
                                                <div>
                                                    <p className="text-[10px] text-black/45 tracking-widest uppercase mb-1">Budget</p>
                                                    <p className="text-sm font-medium text-black/85">
                                                        {form.budget?.status === "accept" ? "🟢 Accepts Day Rate" : "🔴 Expected Day Rate"}
                                                        {form.budget?.value ? ` — ${form.budget.value}` : ""}
                                                    </p>
                                                </div>
                                            )}
                                        </div>
                                    ) : (
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3">
                                            {/* Structured Availability */}
                                            <div className="bg-[#fafaf9] p-3 rounded-lg border border-black/[0.03] space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <label className="text-[10px] text-black/45 tracking-widest uppercase">Availability</label>
                                                    <button
                                                        type="button"
                                                        onClick={() => setFv({ ...fv, availability: fv.availability === false ? undefined : false })}
                                                        className={`w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv.availability !== false ? "bg-black" : "bg-black/15"}`}
                                                    >
                                                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.availability !== false ? "translate-x-4 bg-white" : "bg-black"}`} />
                                                    </button>
                                                </div>
                                                <div className="flex items-center gap-3">
                                                    <select
                                                        value={form.availability?.status || ""}
                                                        onChange={(e) => setForm({ ...form, availability: { ...form.availability, status: e.target.value } })}
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
                                                        onChange={(e) => setForm({ ...form, availability: { ...form.availability, note: e.target.value } })}
                                                        placeholder="Note / reason"
                                                        className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 placeholder:text-black/30 font-medium"
                                                    />
                                                </div>
                                            </div>

                                            {/* Structured Budget */}
                                            <div className="bg-[#fafaf9] p-3 rounded-lg border border-black/[0.03] space-y-2">
                                                <div className="flex items-center justify-between">
                                                    <label className="text-[10px] text-black/45 tracking-widest uppercase">Budget</label>
                                                    <button
                                                        type="button"
                                                        onClick={() => setFv({ ...fv, budget: fv.budget === false ? undefined : false })}
                                                        className={`w-9 h-5 rounded-full relative transition-colors shrink-0 ${fv.budget !== false ? "bg-black" : "bg-black/15"}`}
                                                    >
                                                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.budget !== false ? "translate-x-4 bg-white" : "bg-black"}`} />
                                                    </button>
                                                </div>
                                                <div className="flex items-center gap-3">
                                                    <select
                                                        value={form.budget?.status || ""}
                                                        onChange={(e) => setForm({ ...form, budget: { ...form.budget, status: e.target.value } })}
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
                                                        onChange={(e) => setForm({ ...form, budget: { ...form.budget, value: e.target.value } })}
                                                        placeholder="Expected budget (if custom)"
                                                        className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 placeholder:text-black/30 font-medium"
                                                    />
                                                </div>
                                            </div>
                                        </div>
                                    )}

                                    {/* Custom Question Answers — editable with override indicator */}
                                    {Array.isArray(project?.custom_questions) && project.custom_questions.length > 0 && (
                                        <div className="border-t border-black/[0.08] pt-5 space-y-4">
                                            <p className="eyebrow text-black/75">Application Answers</p>
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                                {project.custom_questions.map((q) => {
                                                    const origAns = detail?.original_form_data?.custom_answers?.[q.id];
                                                    const curAns = (form.custom_answers || {})[q.id] || "";
                                                    const ansOverridden = origAns !== undefined && origAns !== curAns;
                                                    return (
                                                        <div key={q.id} className={`text-sm p-3 rounded-lg border ${ansOverridden ? "border-amber-200 bg-amber-50/40" : "bg-[#fafaf9] border-black/[0.03]"}`}>
                                                            <div className="flex items-center gap-2 mb-1.5">
                                                                <div className="text-black/45 text-[10px] uppercase tracking-wider font-semibold">{q.question}</div>
                                                                {ansOverridden && (
                                                                    <span className="text-[8px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-mono uppercase tracking-wider shrink-0">Override</span>
                                                                )}
                                                            </div>
                                                            {isPreviewMode ? (
                                                                <div className="text-black/85 font-medium">{curAns || "—"}</div>
                                                            ) : (
                                                                <textarea
                                                                    value={curAns}
                                                                    onChange={(e) => setForm({
                                                                        ...form,
                                                                        custom_answers: { ...(form.custom_answers || {}), [q.id]: e.target.value }
                                                                    })}
                                                                    rows={2}
                                                                    placeholder="Enter answer..."
                                                                    className="w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-1 text-sm text-black/85 font-medium resize-none placeholder:text-black/30"
                                                                />
                                                            )}
                                                            {!isPreviewMode && origAns !== undefined && (
                                                                <div className="mt-2 text-[10px] text-black/35 font-mono">Original: {origAns || "—"}</div>
                                                            )}
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                    )}

                                    {/* Save button */}
                                    {!isPreviewMode && (
                                        <div className="flex justify-end pt-2">
                                            <button
                                                type="button"
                                                onClick={handleSaveCuration}
                                                disabled={saving}
                                                className="inline-flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-md text-xs font-semibold hover:bg-black/90 transition-colors shadow-sm"
                                            >
                                                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                                                Save Project Overrides
                                            </button>
                                        </div>
                                    )}
                                </section>

                                {/* ── MEDIA WORKSPACE ── */}

                                {/* Section 0: Admin-Added Media (Project-Specific) */}
                                {!isPreviewMode && (
                                    <section className="border border-black/[0.08] bg-white rounded-xl p-5 md:p-6 shadow-sm">
                                        <div className="flex items-start justify-between border-b border-black/[0.05] pb-3 mb-4 gap-4">
                                            <p className="eyebrow">Admin-Added Media</p>
                                            <span className="text-[9px] text-black/35 font-mono text-right shrink-0">Project-specific · Master profile unchanged</span>
                                        </div>

                                        {/* Admin-added media grid */}
                                        {mediaList.filter(m => m.admin_added).length > 0 && (
                                            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3 mb-5">
                                                {mediaList.filter(m => m.admin_added).map((m) => (
                                                    <div key={m.id} className="relative group border border-black/[0.06] rounded-lg bg-[#fafaf9] overflow-hidden">
                                                        {m.resource_type === "video" ? (
                                                            <div className="aspect-video flex items-center justify-center bg-black/5">
                                                                <Video className="w-8 h-8 text-black/25" />
                                                            </div>
                                                        ) : m.content_type === "application/pdf" ? (
                                                            <div className="aspect-video flex items-center justify-center bg-black/5">
                                                                <FileText className="w-8 h-8 text-black/25" />
                                                            </div>
                                                        ) : (
                                                            <img src={m.thumbnail_url || m.url} alt="" loading="lazy" className="w-full aspect-video object-cover" />
                                                        )}
                                                        <div className="p-2">
                                                            <p className="text-[9px] text-black/50 font-mono uppercase truncate">{m.label || m.category}</p>
                                                        </div>
                                                        <button
                                                            type="button"
                                                            onClick={() => handleRemoveMedia(m.id)}
                                                            disabled={saving}
                                                            className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity p-1 bg-red-500 text-white rounded shadow"
                                                            title="Remove"
                                                        >
                                                            <Trash2 className="w-3 h-3" />
                                                        </button>
                                                    </div>
                                                ))}
                                            </div>
                                        )}

                                        {mediaList.filter(m => m.admin_added).length === 0 && (
                                            <div className="border border-dashed border-black/[0.08] bg-[#fafaf9] rounded-lg p-5 text-center text-xs text-black/40 font-mono mb-4">
                                                No admin-added media yet
                                            </div>
                                        )}

                                        {/* Upload controls */}
                                        <div className="flex flex-wrap items-center gap-3">
                                            <select
                                                value={adminMediaCategory}
                                                onChange={(e) => setAdminMediaCategory(e.target.value)}
                                                className="text-xs border border-black/[0.10] rounded-md px-2 py-1.5 bg-white outline-none text-black/70"
                                            >
                                                <option value="intro_video">Intro Video</option>
                                                <option value="take">Audition Take</option>
                                                <option value="image">Image</option>
                                                <option value="indian">Indian Look</option>
                                                <option value="western">Western Look</option>
                                                <option value="pdf">PDF</option>
                                            </select>
                                            <input
                                                ref={adminMediaInputRef}
                                                type="file"
                                                className="hidden"
                                                accept="video/*,image/*,application/pdf"
                                                onChange={(e) => {
                                                    const file = e.target.files?.[0];
                                                    if (file) {
                                                        handleAdminMediaUpload(file);
                                                        e.target.value = "";
                                                    }
                                                }}
                                            />
                                            <button
                                                type="button"
                                                onClick={() => adminMediaInputRef.current?.click()}
                                                disabled={adminMediaUploading}
                                                className="inline-flex items-center gap-1.5 px-3.5 py-2 border border-black/[0.12] hover:border-black/30 rounded-md text-xs font-semibold bg-white text-black/70 hover:text-black transition-all shadow-sm"
                                            >
                                                {adminMediaUploading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />}
                                                {adminMediaUploading ? "Uploading..." : "Upload File"}
                                            </button>
                                        </div>
                                    </section>
                                )}

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
                                                        <img src={m.url} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
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
                                                        <img src={m.url} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
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
                        <footer className="px-6 py-5 bg-white border-t-2 border-black/[0.08] shrink-0 flex flex-col gap-4 shadow-[0_-10px_40px_-15px_rgba(0,0,0,0.1)] z-20">
                            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-5">
                                <div className="flex-1">
                                    <label className="text-[10px] uppercase font-mono tracking-widest text-black/60 font-semibold mb-1.5 block">Review Decision Note</label>
                                    <input
                                        type="text"
                                        value={decisionNote}
                                        onChange={(e) => setDecisionNote(e.target.value)}
                                        placeholder="Add an internal comment or reason..."
                                        className="w-full text-sm px-4 py-3 border border-black/[0.12] focus:border-black/50 rounded-xl outline-none bg-[#fafaf9] focus:bg-white transition-all text-black/90 shadow-sm"
                                    />
                                </div>
                                <div className="flex items-center gap-3 shrink-0 mt-2 lg:mt-0">
                                    <button
                                        onClick={() => handleDecision("rejected")}
                                        disabled={saving}
                                        className="flex-1 lg:flex-none inline-flex items-center justify-center gap-2 px-5 py-3 border border-rose-200 text-rose-700 hover:bg-rose-50 rounded-xl text-sm font-bold transition-all bg-white shadow-sm"
                                    >
                                        <XCircle className="w-4 h-4" /> Reject
                                    </button>
                                    <button
                                        onClick={() => handleDecision("hold")}
                                        disabled={saving}
                                        className="flex-1 lg:flex-none inline-flex items-center justify-center gap-2 px-5 py-3 border border-amber-200 text-amber-700 hover:bg-amber-50 rounded-xl text-sm font-bold transition-all bg-white shadow-sm"
                                    >
                                        <PauseCircle className="w-4 h-4" /> Hold
                                    </button>
                                    <button
                                        onClick={() => handleDecision("approved")}
                                        disabled={saving}
                                        className="flex-1 lg:flex-none inline-flex items-center justify-center gap-2 px-8 py-3 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-sm font-bold transition-all shadow-md hover:shadow-lg"
                                    >
                                        <Check className="w-4 h-4" /> Approve
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
