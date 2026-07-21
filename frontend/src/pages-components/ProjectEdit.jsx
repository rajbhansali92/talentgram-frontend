import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link, useSearchParams } from "react-router-dom";
import { adminApi, isAdmin, getSubdomainUrl } from "@/lib/api";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import { toast } from "sonner";
import WhatsAppShareButton from "@/components/WhatsAppShareButton";
import { generateSubmissionMessage } from "@/lib/whatsappShare";
import MaterialModal from "@/components/MaterialModal";
import BudgetLines from "@/components/BudgetLines";
import ProjectPipeline from "@/pages-components/ProjectPipeline";
import {
    AVAILABILITY_OPTIONS,
    BUDGET_OPTIONS,
    SUBMISSION_FILTER_TABS,
} from "@/lib/talentSchema";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    ArrowLeft,
    ChevronDown,
    Upload,
    Trash2,
    Loader2,
    X,
    FileText,
    Image as ImageIcon,
    Music,
    PlayCircle,
    Film,
    FolderOpen,
    Plus,
    Copy,
    MessageCircle,
    Check,
    XCircle,
    Clock,
    ExternalLink,
    PauseCircle,
    Lock,
    Phone,
    MessageSquare,
    User,
} from "lucide-react";

const COMMISSION_OPTIONS = ["10%", "15%", "20%", "25%", "30%"];

const empty = {
    brand_name: "",
    brand_link: "",
    character: "",
    shoot_dates: "",
    budget_per_day: "",
    commission_percent: "",
    medium_usage: "",
    director: "",
    production_house: "",
    additional_details: "",
    video_links: [],
    competitive_brand_enabled: false,
    custom_questions: [],
    talent_budget: [],
    client_budget: [],
    require_reapproval_on_edit: true,
    hide_budget_from_talent: false,
    status: "ongoing",
};

// ISSUE 2 & 3: More robust file validation using startsWith()
const MAX_FILE_SIZE = {
    script: 10 * 1024 * 1024, // 10MB
    image: 20 * 1024 * 1024,  // 20MB
    audio: 30 * 1024 * 1024,  // 30MB
    video_file: 100 * 1024 * 1024, // 100MB
};

function TextField({ label, value, onChange, type = "text", disabled = false, ...rest }) {
    // View mode (disabled): present the value as a read-only record — full text
    // wraps naturally (no single-line truncation), and it reads as content
    // rather than a greyed-out input. Edit mode is unchanged.
    const Wrapper = disabled ? "div" : "label";
    return (
        <Wrapper className="block">
            <span className="text-[11px] text-black/45 tracking-widest uppercase">
                {label}
            </span>
            {disabled ? (
                <div className="mt-2 py-2.5 text-sm text-black/85 whitespace-pre-wrap break-words">
                    {value ? value : <span className="text-black/30">—</span>}
                </div>
            ) : (
                <input
                    type={type}
                    value={value || ""}
                    onChange={(e) => onChange(e.target.value)}
                    className="mt-2 w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2.5 text-sm text-black/85 placeholder:text-black/30"
                    {...rest}
                />
            )}
        </Wrapper>
    );
}

const PROJECT_TABS = [
    { id: "details", label: "Project Details" },
    { id: "submissions", label: "Submission Review" },
    { id: "pipeline", label: "Casting Pipeline" },
];
const LAST_PROJECT_TAB_KEY = "tg_last_project_tab";

export default function ProjectEdit() {
    const { id } = useParams();
    const nav = useNavigate();
    const isEdit = Boolean(id);
    const isAdminRole = isAdmin();
    const isMounted = useRef(true);

    // Tabbed workspace: URL ?tab= wins (deep-linking), then the
    // last-used tab remembered in sessionStorage (so an admin who lives in
    // Pipeline lands back there), then "details" as the default. Inactive
    // tabs stay mounted and are only hidden via CSS (never unmounted) so
    // their internal state/scroll survives switching; each tab's own
    // scroll offset is saved/restored on switch since the whole page still
    // shares one document-level scroll.
    const [searchParams, setSearchParams] = useSearchParams();
    const [activeTab, setActiveTabState] = useState(() => {
        const urlTab = searchParams.get("tab");
        if (PROJECT_TABS.some((t) => t.id === urlTab)) return urlTab;
        try {
            const stored = sessionStorage.getItem(LAST_PROJECT_TAB_KEY);
            if (PROJECT_TABS.some((t) => t.id === stored)) return stored;
        } catch {
            // sessionStorage unavailable — fall through to default
        }
        return "details";
    });
    const [visitedTabs, setVisitedTabs] = useState(() => new Set([activeTab]));
    const scrollPositionsRef = useRef({});

    const selectTab = useCallback((tabId) => {
        setActiveTabState((prev) => {
            if (prev === tabId) return prev;
            scrollPositionsRef.current[prev] = window.scrollY;
            return tabId;
        });
        setVisitedTabs((prev) => (prev.has(tabId) ? prev : new Set(prev).add(tabId)));
        try {
            sessionStorage.setItem(LAST_PROJECT_TAB_KEY, tabId);
        } catch {
            // sessionStorage unavailable — tab memory just won't persist
        }
        setSearchParams(
            (prev) => {
                const next = new URLSearchParams(prev);
                next.set("tab", tabId);
                return next;
            },
            { replace: true },
        );
    }, [setSearchParams]);

    // Keep the URL's ?tab= in sync with the resolved initial tab (covers the
    // case where it came from sessionStorage rather than the URL itself),
    // and restore each tab's saved scroll offset after switching.
    useEffect(() => {
        if (searchParams.get("tab") !== activeTab) {
            setSearchParams(
                (prev) => {
                    const next = new URLSearchParams(prev);
                    next.set("tab", activeTab);
                    return next;
                },
                { replace: true },
            );
        }
        const saved = scrollPositionsRef.current[activeTab];
        const raf = requestAnimationFrame(() => {
            window.scrollTo(0, saved || 0);
        });
        return () => cancelAnimationFrame(raf);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTab]);

    const [project, setProject] = useState(empty);
    const [saving, setSaving] = useState(false);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(null); // category string
    const [videoInput, setVideoInput] = useState("");
    const [showMaterialModal, setShowMaterialModal] = useState(false);
    const [submissions, setSubmissions] = useState([]);
    const [submissionFilter, setSubmissionFilter] = useState("all");
    const [deleteSubmissionId, setDeleteSubmissionId] = useState(null);
    const [deleteMaterialId, setDeleteMaterialId] = useState(null);
    const [collapsedSections, setCollapsedSections] = useState({
        projectDetails: false,
        additionalDetails: false,
        auditionMaterial: false,
        formConfig: false,
        submissionRequirements: false,
        submissionLink: false,
        submissions: false,
    });
    const scriptRef = useRef();
    const imageRef = useRef();
    const audioRef = useRef();
    const videoFileRef = useRef();

    // ─── View / Edit mode ─────────────────────────────────────────────────────
    const [isEditing, setIsEditing] = useState(!isEdit);
    const [originalProject, setOriginalProject] = useState(empty);
    const isDirty = isEditing && JSON.stringify(project) !== JSON.stringify(originalProject);

    const updateProject = useCallback((patch) => {
        setProject(prev => ({
            ...prev,
            ...patch
        }));
    }, []);

    const loadSubmissions = useCallback(async (pid) => {
        try {
            const { data } = await adminApi.get(`/projects/${pid}/submissions`);
            if (isMounted.current) setSubmissions(data);
        } catch (e) { console.error(e); }
    }, []);

    useEffect(() => {
        isMounted.current = true;
        return () => { isMounted.current = false; };
    }, []);

    useEffect(() => {
        if (!isEdit) {
            setLoading(false);
            return;
        }
        (async () => {
            try {
                const { data } = await adminApi.get(`/projects/${id}`);
                if (isMounted.current) {
                    const loaded = { ...empty, ...data };
                    setProject(loaded);
                    setOriginalProject(loaded);
                    setLoading(false);
                    loadSubmissions(id);
                }
            } catch {
                if (isMounted.current) {
                    toast.error("Failed to load project");
                    setLoading(false);
                }
            }
        })();
    }, [id, isEdit, loadSubmissions]);

    const setDecision = async (sid, decision, note = "") => {
        await adminApi.post(`/projects/${id}/submissions/${sid}/decision`, {
            decision,
            note,
        });
        toast.success(`Marked as ${decision}`);
        loadSubmissions(id);
    };

    const confirmDeleteSubmission = (sid) => {
        setDeleteSubmissionId(sid);
    };

    const handleDeleteSubmission = async () => {
        if (!deleteSubmissionId) return;
        await adminApi.delete(`/projects/${id}/submissions/${deleteSubmissionId}`);
        loadSubmissions(id);
        setDeleteSubmissionId(null);
    };

    const submissionUrl = project?.slug
        ? `${getSubdomainUrl("submit")}/${project.slug}`
        : "";

    const copySubmitLink = () => {
        if (!submissionUrl) {
            toast.error("No submission link available");
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(submissionUrl).then(() => {
                toast.success("Submission link copied");
            }).catch(() => {
                fallbackCopy(submissionUrl);
            });
        } else {
            fallbackCopy(submissionUrl);
        }
    };

    const fallbackCopy = (text) => {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand("copy");
            toast.success("Submission link copied");
        } catch (err) {
            toast.error("Failed to copy link");
        }
        document.body.removeChild(textarea);
    };

    const shareWhatsApp = () => {
        window.open(generateSubmissionMessage(project.brand_name, submissionUrl), "_blank");
    };

    const handleCancel = () => {
        setProject(originalProject);
        setIsEditing(false);
    };

    const save = async () => {
        if (!project.brand_name.trim()) {
            toast.error("Brand / Project name is required");
            return;
        }
        setSaving(true);
        try {
            const payload = {
                ...project,
                video_links: (project.video_links || []).filter(Boolean),
            };
            if (isEdit) {
                await adminApi.put(`/projects/${id}`, payload);
                toast.success("Saved");
                setOriginalProject(payload);
                setIsEditing(false);
            } else {
                const { data } = await adminApi.post(`/projects`, payload);
                toast.success("Project created");
                nav(`/admin/projects/${data.id}`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally {
            setSaving(false);
        }
    };

    const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

    const deleteProject = async () => {
        if (!isEdit) return;
        try {
            const res = await adminApi.delete(`/projects/${id}`);
            console.info("[delete project]", id, res?.data);
            toast.success(
                `Project deleted${res?.data?.cascaded_submissions ? ` (+${res.data.cascaded_submissions} submissions removed)` : ""}`,
            );
            setConfirmDeleteOpen(false);
            nav("/admin/projects");
        } catch (err) {
            console.error("[delete project] failed", err?.response?.data || err);
            toast.error(
                err?.response?.data?.detail ||
                    err?.message ||
                    "Delete failed — check console for details",
            );
            throw err;
        }
    };

    // ─── Navigation guards (dirty state) ──────────────────────────────────────
    useEffect(() => {
        const handleBeforeUnload = (e) => {
            if (!isDirty) return;
            e.preventDefault();
            e.returnValue = "";
        };
        window.addEventListener("beforeunload", handleBeforeUnload);
        return () => window.removeEventListener("beforeunload", handleBeforeUnload);
    }, [isDirty]);

    useEffect(() => {
        if (!isDirty) return;
        const handlePopState = () => {
            const leave = window.confirm("You have unsaved changes. Leave without saving?");
            if (leave) {
                window.removeEventListener("popstate", handlePopState);
                nav(-1);
            } else {
                window.history.pushState(null, "", window.location.href);
            }
        };
        window.history.pushState(null, "", window.location.href);
        window.addEventListener("popstate", handlePopState);
        return () => window.removeEventListener("popstate", handlePopState);
    }, [isDirty, nav]);

    useEffect(() => {
        if (!isDirty) return;
        const handleClick = (e) => {
            const anchor = e.target.closest("a[href]");
            if (!anchor) return;
            const href = anchor.getAttribute("href");
            if (!href || href.startsWith("#") || href.startsWith("http") || anchor.target === "_blank") return;
            e.preventDefault();
            e.stopPropagation();
            const leave = window.confirm("You have unsaved changes. Leave without saving?");
            if (leave) nav(href);
        };
        document.addEventListener("click", handleClick, true);
        return () => document.removeEventListener("click", handleClick, true);
    }, [isDirty, nav]);

    // ISSUE 2: More robust file validation using startsWith() for broad compatibility
    const validateFile = (file, category) => {
        const maxSize = MAX_FILE_SIZE[category];
        
        // Check file size
        if (file.size > maxSize) {
            toast.error(`${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB — max ${maxSize / 1024 / 1024} MB`);
            return false;
        }
        
        // Type validation based on category
        if (category === "script") {
            if (!file.type === "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
                toast.error(`${file.name} must be a PDF file`);
                return false;
            }
        } else if (category === "image") {
            // Allow any image type broadly
            if (!file.type.startsWith("image/")) {
                toast.error(`${file.name} must be an image file`);
                return false;
            }
        } else if (category === "audio") {
            if (!file.type.startsWith("audio/")) {
                toast.error(`${file.name} must be an audio file`);
                return false;
            }
        } else if (category === "video_file") {
            if (!file.type.startsWith("video/")) {
                toast.error(`${file.name} must be a video file`);
                return false;
            }
        }
        
        return true;
    };

    const uploadMaterial = async (files, category) => {
        if (!isEdit) {
            toast.error("Save the project before uploading materials");
            return;
        }
        
        // Validate all files first
        for (const f of files) {
            if (!validateFile(f, category)) {
                return;
            }
        }
        
        setUploading(category);
        try {
            for (const file of files) {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                const { data } = await adminApi.post(
                    `/projects/${id}/material`,
                    fd,
                    { headers: { "Content-Type": "multipart/form-data" } },
                );
                if (isMounted.current) {
                    setProject({ ...empty, ...data });
                }
            }
            toast.success(`${files.length} uploaded`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const confirmRemoveMaterial = (mid) => {
        setDeleteMaterialId(mid);
    };

    const handleRemoveMaterial = async () => {
        if (!deleteMaterialId) return;
        await adminApi.delete(`/projects/${id}/material/${deleteMaterialId}`);
        const { data } = await adminApi.get(`/projects/${id}`);
        if (isMounted.current) {
            setProject({ ...empty, ...data });
        }
        setDeleteMaterialId(null);
    };

    const addVideoLink = () => {
        const v = videoInput.trim();
        if (!v) return;
        updateProject({
            video_links: [...(project.video_links || []), v],
        });
        setVideoInput("");
    };

    const [cqInput, setCqInput] = useState("");
    const addCustomQuestion = () => {
        const q = cqInput.trim();
        if (!q) return;
        setProject(prev => ({
            ...prev,
            custom_questions: [
                ...(prev.custom_questions || []),
                { id: crypto.randomUUID(), question: q, type: "text" },
            ]
        }));
        setCqInput("");
    };

    const materialsCount = (project.materials || []).length + (project.video_links || []).length;

    // ISSUE 4: Centralized filtered submissions to avoid duplicate filter logic
    const filteredSubmissions = submissions.filter((s) => {
        if (submissionFilter === "all") return true;
        if (submissionFilter === "updated") return s.status === "updated";
        return (s.decision || "pending") === submissionFilter;
    });

    const approvedCount = submissions.filter((s) => s.decision === "approved").length;
    const rejectedCount = submissions.filter((s) => s.decision === "rejected").length;
    const pendingCount = submissions.filter((s) => !s.decision || s.decision === "pending").length;
    const updatedCount = submissions.filter((s) => s.status === "updated").length;

    // Loading skeleton
    if (loading) {
        return (
            <div className="p-6 md:p-10 max-w-7xl mx-auto">
                <div className="animate-pulse">
                    <div className="h-4 w-32 bg-black/10 rounded mb-6"></div>
                    <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                        <div>
                            <div className="h-3 w-24 bg-black/10 rounded mb-3"></div>
                            <div className="h-12 w-64 bg-black/10 rounded"></div>
                        </div>
                        <div className="h-10 w-32 bg-black/10 rounded"></div>
                    </div>
                    <div className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mb-6">
                        <div className="h-4 w-32 bg-black/10 rounded mb-6"></div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
                            {[...Array(10)].map((_, i) => (
                                <div key={i}>
                                    <div className="h-3 w-20 bg-black/10 rounded mb-2"></div>
                                    <div className="h-10 w-full bg-black/5 rounded"></div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto text-black/85"
            data-testid="project-edit-page"
        >
            <Link
                to="/admin/projects"
                className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6"
            >
                <ArrowLeft className="w-3 h-3" /> Back to projects
            </Link>

            <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                <div>
                    <p className="eyebrow mb-3">
                        {isEdit
                            ? isEditing ? "Editing Project" : "Project"
                            : "New Project"}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        {project.brand_name || "Untitled"}
                    </h1>
                </div>
                <div className="flex gap-2 flex-wrap">
                    {isEdit && (
                        <>
                            <button
                                onClick={() => setShowMaterialModal(true)}
                                data-testid="view-audition-material-btn"
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#eaeaea] hover:border-black/[0.20] rounded-sm text-xs transition-colors"
                            >
                                <FolderOpen className="w-3.5 h-3.5" /> View Audition
                                Material ({materialsCount})
                            </button>
                            {isAdminRole && (
                                <button
                                    onClick={() => setConfirmDeleteOpen(true)}
                                    className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#eaeaea] text-black/60 hover:text-red-600 hover:border-red-600/40 rounded-sm text-xs"
                                    data-testid="delete-project-btn"
                                >
                                    <Trash2 className="w-3 h-3" /> Delete
                                </button>
                            )}
                        </>
                    )}
                    {/* Status — interactive dropdown in edit mode; a static,
                        unambiguously read-only chip in view mode (no dropdown
                        affordance, so it never looks clickable when it isn't). */}
                    {isEditing ? (
                        <Select
                            value={project.status || "ongoing"}
                            onValueChange={(v) => isEditing && updateProject({ status: v })}
                        >
                            <SelectTrigger
                                data-testid="project-status-select-trigger"
                                className="bg-transparent border border-[#eaeaea] hover:border-black/[0.20] rounded-sm text-xs h-9 px-3 w-[120px] focus:ring-0 shadow-none text-black/70 font-medium"
                            >
                                <SelectValue placeholder="Status" />
                            </SelectTrigger>
                            <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                <SelectItem value="ongoing">Ongoing</SelectItem>
                                <SelectItem value="hold">Hold</SelectItem>
                                <SelectItem value="complete">Complete</SelectItem>
                                <SelectItem value="locked">Locked</SelectItem>
                            </SelectContent>
                        </Select>
                    ) : (
                        <span
                            data-testid="project-status-readonly"
                            className="inline-flex items-center rounded-sm border border-[#eaeaea] text-xs h-9 px-3 text-black/70 font-medium capitalize"
                        >
                            {project.status || "ongoing"}
                        </span>
                    )}
                    {/* View mode: Edit button */}
                    {isEdit && !isEditing && (
                        <button
                            onClick={() => setIsEditing(true)}
                            data-testid="edit-project-btn"
                            className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-sm text-xs font-medium hover:bg-black/90 transition-colors"
                        >
                            Edit
                        </button>
                    )}
                    {/* Edit mode: Cancel + Save */}
                    {isEditing && isEdit && (
                        <button
                            onClick={handleCancel}
                            data-testid="cancel-project-btn"
                            className="inline-flex items-center gap-2 px-5 py-2.5 border border-black/[0.12] rounded-sm text-xs font-medium text-black/60 hover:text-black transition-colors"
                        >
                            Cancel
                        </button>
                    )}
                    {(!isEdit || isEditing) && (
                        <button
                            onClick={save}
                            disabled={saving}
                            data-testid="save-project-btn"
                            className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-sm text-xs font-medium hover:bg-black/90 transition-colors disabled:opacity-60"
                        >
                            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
                            {isEdit ? "Save Changes" : "Create project"}
                        </button>
                    )}
                </div>
            </div>

            {isEdit && (
                <div
                    className="sticky top-14 md:top-0 z-30 bg-white border-b border-[#eaeaea] mb-6"
                    data-testid="project-tab-bar"
                >
                    <div className="flex gap-1 overflow-x-auto tg-noscrollbar">
                        {PROJECT_TABS.map((tab) => (
                            <button
                                key={tab.id}
                                type="button"
                                onClick={() => selectTab(tab.id)}
                                data-testid={`project-tab-${tab.id}`}
                                aria-current={activeTab === tab.id ? "true" : undefined}
                                className={`shrink-0 px-4 md:px-5 py-3.5 text-xs md:text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
                                    activeTab === tab.id
                                        ? "border-black text-black"
                                        : "border-transparent text-black/45 hover:text-black/70"
                                }`}
                            >
                                {tab.label}
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* Project details */}
            <div style={{ display: !isEdit || activeTab === "details" ? "block" : "none" }} data-testid="project-tab-panel-details">
            <section className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mb-6">
                <div className="flex items-center justify-between mb-6">
                    <p className="eyebrow">Project Details</p>
                    <button
                        type="button"
                        onClick={() => setCollapsedSections(prev => ({ ...prev, projectDetails: !prev.projectDetails }))}
                        className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors"
                        aria-label={collapsedSections.projectDetails ? "Expand project details" : "Collapse project details"}
                    >
                        <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.projectDetails ? "-rotate-90" : ""}`} />
                    </button>
                </div>
                {!collapsedSections.projectDetails && (
                    <>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
                            <TextField
                                label="Project / Brand Name"
                                value={project.brand_name}
                                onChange={(v) => updateProject({ brand_name: v })}
                                disabled={!isEditing}
                                data-testid="project-brand-input"
                            />
                            <TextField
                                label="Project / Brand Link"
                                value={project.brand_link}
                                onChange={(v) => updateProject({ brand_link: v })}
                                disabled={!isEditing}
                                placeholder="https://..."
                            />
                            <TextField
                                label="Character"
                                value={project.character}
                                onChange={(v) => updateProject({ character: v })}
                                disabled={!isEditing}
                                placeholder="e.g. Young Mother, 28-35"
                            />
                            <TextField
                                label="Shoot Dates"
                                value={project.shoot_dates}
                                onChange={(v) => updateProject({ shoot_dates: v })}
                                disabled={!isEditing}
                                placeholder="e.g. 15–18 March 2026"
                            />
                            <TextField
                                label="Budget per Day"
                                value={project.budget_per_day}
                                onChange={(v) => updateProject({ budget_per_day: v })}
                                disabled={!isEditing}
                                placeholder="e.g. ₹50,000"
                            />
                            <div>
                                <span className="text-[11px] text-black/45 tracking-widest uppercase">
                                    Commission %
                                </span>
                                <div className="mt-2">
                                    {!isEditing ? (
                                        <div className="py-2.5 text-sm text-black/85">
                                            {project.commission_percent ? project.commission_percent : <span className="text-black/30">—</span>}
                                        </div>
                                    ) : (
                                        <Select
                                            value={project.commission_percent || ""}
                                            onValueChange={(v) => isEditing && updateProject({ commission_percent: v })}
                                            disabled={!isEditing}
                                        >
                                            <SelectTrigger
                                                data-testid="commission-select-trigger"
                                                disabled={!isEditing}
                                                className="bg-transparent border-0 border-b border-black/[0.10] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5 disabled:opacity-60 disabled:cursor-not-allowed"
                                            >
                                                <SelectValue placeholder="Select commission" />
                                            </SelectTrigger>
                                            <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                {COMMISSION_OPTIONS.map((c) => (
                                                    <SelectItem
                                                        key={c}
                                                        value={c}
                                                        className="focus:bg-black/5 focus:text-black"
                                                        data-testid={`commission-option-${c}`}
                                                    >
                                                        {c}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    )}
                                </div>
                            </div>
                            <TextField
                                label="Medium / Usage"
                                value={project.medium_usage}
                                onChange={(v) => updateProject({ medium_usage: v })}
                                disabled={!isEditing}
                                placeholder="e.g. TVC · Digital · Print — 1yr"
                            />
                            <TextField
                                label="Director"
                                value={project.director}
                                onChange={(v) => updateProject({ director: v })}
                                disabled={!isEditing}
                            />
                            <TextField
                                label="Production House"
                                value={project.production_house}
                                onChange={(v) => updateProject({ production_house: v })}
                                disabled={!isEditing}
                            />
                        </div>
                        <div className="mt-6 border-t border-[#eaeaea] pt-6">
                            <div className="flex items-center justify-between mb-4">
                                <span className="text-[11px] text-black/45 tracking-widest uppercase font-semibold">
                                    Additional Details
                                </span>
                                <button
                                    type="button"
                                    onClick={() => setCollapsedSections(prev => ({ ...prev, additionalDetails: !prev.additionalDetails }))}
                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded text-black/55 hover:text-black transition-colors"
                                    aria-label={collapsedSections.additionalDetails ? "Expand additional details" : "Collapse additional details"}
                                >
                                    <ChevronDown className={`w-3 h-3 transform transition-transform duration-200 ${collapsedSections.additionalDetails ? "-rotate-90" : ""}`} />
                                </button>
                            </div>
                            {!collapsedSections.additionalDetails && (
                                isEditing ? (
                                    <textarea
                                        value={project.additional_details || ""}
                                        onChange={(e) => updateProject({ additional_details: e.target.value })}
                                        rows={3}
                                        className="mt-2 w-full bg-transparent border border-[#eaeaea] focus:border-black/40 outline-none p-4 text-sm text-black/85 rounded-xl"
                                    />
                                ) : (
                                    <div className="mt-2 text-sm text-black/85 whitespace-pre-wrap break-words leading-relaxed">
                                        {project.additional_details ? project.additional_details : <span className="text-black/30">—</span>}
                                    </div>
                                )
                            )}
                        </div>
                    </>
                )}
            </section>

            {/* Audition Material uploads */}
            {isEdit && (
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8"
                    data-testid="audition-material-section"
                >
                    <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
                        <div>
                            <p className="eyebrow">Audition Material</p>
                            <p className="text-xs text-black/45 mt-1">
                                Attach script (PDF), reference images, audio
                                notes, and video links
                            </p>
                        </div>
                        <button
                            type="button"
                            onClick={() => setCollapsedSections(prev => ({ ...prev, auditionMaterial: !prev.auditionMaterial }))}
                            className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors shrink-0"
                            aria-label={collapsedSections.auditionMaterial ? "Expand audition material" : "Collapse audition material"}
                        >
                            <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.auditionMaterial ? "-rotate-90" : ""}`} />
                        </button>
                    </div>

                    {!collapsedSections.auditionMaterial && (
                        <>
                            {isEditing ? (
                                <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
                                    <UploadTile
                                        title="Script (PDF)"
                                        icon={FileText}
                                        accept="application/pdf,.pdf"
                                        onPick={(files) => uploadMaterial(files, "script")}
                                        inputRef={scriptRef}
                                        uploading={uploading === "script"}
                                        testid="upload-script"
                                    />
                                    <UploadTile
                                        title="Images"
                                        icon={ImageIcon}
                                        accept="image/*"
                                        multiple
                                        onPick={(files) => uploadMaterial(files, "image")}
                                        inputRef={imageRef}
                                        uploading={uploading === "image"}
                                        testid="upload-image"
                                    />
                                    <UploadTile
                                        title="Audio Notes"
                                        icon={Music}
                                        accept="audio/*"
                                        multiple
                                        onPick={(files) => uploadMaterial(files, "audio")}
                                        inputRef={audioRef}
                                        uploading={uploading === "audio"}
                                        testid="upload-audio"
                                    />
                                    <UploadTile
                                        title="Reference Videos"
                                        icon={Film}
                                        accept="video/*"
                                        multiple
                                        onPick={(files) => uploadMaterial(files, "video_file")}
                                        inputRef={videoFileRef}
                                        uploading={uploading === "video_file"}
                                        testid="upload-video-file"
                                        hint="Max 100 MB · mp4/mov/webm"
                                    />
                                </div>
                            ) : (
                                <p className="text-xs text-black/40 italic mb-8">Enable Edit to upload new materials.</p>
                            )}

                            {/* Video links list */}
                            <div>
                                <div className="flex items-center gap-2 mb-3">
                                    <PlayCircle className="w-3.5 h-3.5 text-black/60" />
                                    <p className="eyebrow">Video Links</p>
                                </div>
                                <div className="space-y-2 mb-3">
                                    {(project.video_links || []).map((v, i) => (
                                        <div
                                            key={v}
                                            className="flex items-center gap-2 border-b border-black/[0.06] pb-2"
                                        >
                                            <a
                                                href={v}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="flex-1 text-sm text-black/75 tg-mono truncate hover:text-black"
                                            >
                                                {v}
                                            </a>
                                            {isEditing && (
                                                <button
                                                    type="button"
                                                    onClick={() =>
                                                        updateProject({
                                                            video_links: project.video_links.filter(
                                                                (_, j) => j !== i,
                                                            ),
                                                        })
                                                    }
                                                    className="text-black/40 hover:text-red-600 transition-colors"
                                                >
                                                    <X className="w-3.5 h-3.5" />
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                                {isEditing && (
                                    <div className="flex gap-2">
                                        <input
                                            type="url"
                                            value={videoInput}
                                            onChange={(e) => setVideoInput(e.target.value)}
                                            placeholder="https://youtube.com/..."
                                            className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                                        />
                                        <button
                                            type="button"
                                            onClick={addVideoLink}
                                            className="text-xs px-3 py-2 border border-black/[0.10] hover:border-black/[0.20] rounded-sm inline-flex items-center gap-1 text-black/70 hover:text-black transition-colors"
                                        >
                                            <Plus className="w-3 h-3" /> Add link
                                        </button>
                                    </div>
                                )}
                                <p className="text-[10px] text-black/35 mt-3 tg-mono">
                                    Save project after adding video links
                                </p>
                            </div>
                        </>
                    )}
                </section>
            )}
            {!isEdit && (
                <p className="text-xs text-black/45 italic">
                    Save this project first to upload audition materials.
                </p>
            )}

            {/* Budget Configuration — Phase 1 cleanup (v37):
                Project-level budget editors (talent-facing + client-facing) are
                fully hidden. The single source of truth is the budget the
                talent submits in their audition (admin can edit it on the
                submission row). Existing project-level budget lines persist
                untouched in the DB but are no longer presented to anyone.
                Re-enable by restoring this <section> if the workflow ever
                changes. */}
            {false && isEdit && (
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mt-6"
                    data-testid="budget-config-section"
                >
                    <p className="eyebrow mb-2">Project Budget</p>
                    <p className="text-xs text-black/45 mb-6">
                        Keep the talent-facing and client-facing breakdowns separate.
                        Talents see only the talent budget; clients see only the client
                        budget (gated by the link's Budget visibility toggle).
                    </p>

                    <div className="mb-8">
                        <p className="text-sm text-black/80 mb-1">
                            Talent-Facing Budget
                        </p>
                        <p className="text-xs text-black/45 mb-3">
                            Hint shown on the audition submission form so talents
                            understand the offer before they quote.
                        </p>
                        <BudgetLines
                            lines={project.talent_budget || []}
                            onChange={(lines) => updateProject({ talent_budget: lines })}
                            testidPrefix="talent-budget"
                        />
                    </div>

                    <div className="border-t border-[#eaeaea] pt-6">
                        <p className="text-sm text-black/80 mb-1">
                            Client-Facing Budget
                        </p>
                        <p className="text-xs text-black/45 mb-3">
                            Shown to clients on the shared link view. Individual
                            links can still override this via the Link Generator.
                        </p>
                        <BudgetLines
                            lines={project.client_budget || []}
                            onChange={(lines) => updateProject({ client_budget: lines })}
                            testidPrefix="client-budget"
                        />
                    </div>

                    <p className="text-[10px] text-black/35 mt-6 tg-mono">
                        Save project to apply changes
                    </p>
                </section>
            )}

            {/* Submission Form Configuration */}
            {isEdit && (
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mt-6"
                    data-testid="form-config-section"
                >
                    <div className="flex items-center justify-between mb-6">
                        <p className="eyebrow">Submission Form Configuration</p>
                        <button
                            type="button"
                            onClick={() => setCollapsedSections(prev => ({ ...prev, formConfig: !prev.formConfig }))}
                            className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors shrink-0"
                            aria-label={collapsedSections.formConfig ? "Expand form config" : "Collapse form config"}
                        >
                            <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.formConfig ? "-rotate-90" : ""}`} />
                        </button>
                    </div>

                    {!collapsedSections.formConfig && (
                        <>
                            <label className="flex items-center justify-between cursor-pointer mb-6">
                                <div>
                                    <div className="text-sm text-black/80">
                                        Re-approval required on edit
                                    </div>
                                    <div className="text-[11px] text-black/45 tg-mono mt-0.5">
                                        When ON, any retake or form edit after a decision moves the submission back to Pending. Turn OFF to silently keep the existing decision.
                                    </div>
                                </div>
                                <input
                                    type="checkbox"
                                    checked={project.require_reapproval_on_edit !== false}
                                    onChange={(e) => isEditing && updateProject({ require_reapproval_on_edit: e.target.checked })}
                                    data-testid="require-reapproval-toggle"
                                    disabled={!isEditing}
                                    className="w-5 h-5 accent-black disabled:opacity-60"
                                />
                            </label>
                            <label className="flex items-center justify-between cursor-pointer mb-6">
                                <div>
                                    <div className="text-sm text-black/80">
                                        Hide Budget From Talent
                                    </div>
                                    <div className="text-[11px] text-black/45 tg-mono mt-0.5">
                                        When ON, the Budget Per Day field is hidden from the public audition submission page. Default is OFF (budget visible).
                                    </div>
                                </div>
                                <input
                                    type="checkbox"
                                    checked={project.hide_budget_from_talent === true}
                                    onChange={(e) => isEditing && updateProject({ hide_budget_from_talent: e.target.checked })}
                                    data-testid="hide-budget-toggle"
                                    disabled={!isEditing}
                                    className="w-5 h-5 accent-black disabled:opacity-60"
                                />
                            </label>
                            <label className="flex items-center justify-between cursor-pointer mb-6">
                                <div>
                                    <div className="text-sm text-black/80">
                                        Ask "Competitive Brand" field
                                    </div>
                                    <div className="text-xs text-black/45 mt-1">
                                        When enabled, talents must declare any brand
                                        conflicts
                                    </div>
                                </div>
                                <button
                                    type="button"
                                    disabled={!isEditing}
                                    onClick={() =>
                                        isEditing && updateProject({
                                            competitive_brand_enabled: !project.competitive_brand_enabled,
                                        })
                                    }
                                    data-testid="toggle-competitive-brand"
                                    className={`w-10 h-5 rounded-full relative transition-colors shrink-0 disabled:opacity-60 disabled:cursor-not-allowed ${project.competitive_brand_enabled ? "bg-black" : "bg-black/15"}`}
                                >
                                    <span
                                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${project.competitive_brand_enabled ? "translate-x-5 bg-white" : "bg-black"}`}
                                    />
                                </button>
                            </label>

                            <div className="border-t border-[#eaeaea] pt-6">
                                <p className="text-sm text-black/80 mb-1">
                                    Custom Questions
                                </p>
                                <p className="text-xs text-black/45 mb-4">
                                    Ask project-specific questions. Shown on the talent
                                    submission form.
                                </p>
                                <div className="space-y-2 mb-3">
                                    {(project.custom_questions || []).map((q, i) => (
                                        <div
                                            key={q.id}
                                            className="flex items-center gap-2 border-b border-black/[0.06] pb-2"
                                            data-testid={`cq-row-${i}`}
                                        >
                                            <span className="text-sm text-black/75 flex-1 truncate">
                                                {q.question}
                                            </span>
                                            {isEditing && (
                                                <button
                                                    type="button"
                                                    onClick={() =>
                                                        updateProject({
                                                            custom_questions: project.custom_questions.filter(
                                                                (_, j) => j !== i,
                                                            ),
                                                        })
                                                    }
                                                    className="text-black/40 hover:text-red-600 transition-colors"
                                                >
                                                    <X className="w-3.5 h-3.5" />
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                                {isEditing && (
                                    <div className="flex gap-2">
                                        <input
                                            value={cqInput}
                                            onChange={(e) => setCqInput(e.target.value)}
                                            placeholder="e.g. Can you ride a bike?"
                                            data-testid="cq-input"
                                            className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                                        />
                                        <button
                                            type="button"
                                            onClick={addCustomQuestion}
                                            data-testid="cq-add-btn"
                                            className="text-xs px-3 py-2 border border-black/[0.10] hover:border-black/[0.20] rounded-sm inline-flex items-center gap-1 text-black/70 hover:text-black transition-colors"
                                        >
                                            <Plus className="w-3 h-3" /> Add
                                        </button>
                                    </div>
                                )}
                                <p className="text-[10px] text-black/35 mt-3 tg-mono">
                                    Save project to apply changes
                                </p>
                            </div>
                        </>
                    )}
                </section>
            )}

            {/* Submission Requirements Section */}
            {isEdit && (
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mt-6"
                    data-testid="submission-requirements-section"
                >
                    <div className="flex items-center justify-between mb-6">
                        <div>
                            <p className="eyebrow">Submission Requirements Engine</p>
                            <p className="text-xs text-black/45 mt-1">
                                Enforce profile fields, portfolio counts, specific skills, and conditional video tasks.
                            </p>
                        </div>
                        <button
                            type="button"
                            onClick={() => setCollapsedSections(prev => ({ ...prev, submissionRequirements: !prev.submissionRequirements }))}
                            className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors shrink-0"
                            aria-label={collapsedSections.submissionRequirements ? "Expand requirements" : "Collapse requirements"}
                        >
                            <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.submissionRequirements ? "-rotate-90" : ""}`} />
                        </button>
                    </div>

                    {!collapsedSections.submissionRequirements && (() => {
                        const defaultRequirements = {
                            strictness: "strict",
                            fields: {
                                name: "required",
                                email: "required",
                                phone: "optional",
                                dob: "optional",
                                age: "optional",
                                height: "optional",
                                location: "optional",
                                gender: "optional",
                                ethnicity: "optional",
                                instagram_handle: "optional",
                                instagram_followers: "optional",
                                bio: "optional",
                                competitive_brand: "optional",
                                availability: "optional",
                                budget_expectation: "optional",
                                work_links: "optional",
                            },
                            custom_questions: {},
                            intro_video: "optional",
                            audition_takes_visibility: "optional",
                            min_audition_takes: 0,
                            portfolio_image_visibility: "optional",
                            portfolio_indian_visibility: "optional",
                            portfolio_western_visibility: "optional",
                            portfolio: {
                                indian: 0,
                                western: 0,
                                image: 0,
                            },
                            work_links_visibility: "optional",
                            min_work_links: 0,
                            skills: {
                                Dance: false,
                                Music: false,
                                "Sports & Fitness": false,
                                "Action & Stunts": false,
                                "Vehicle Skills": false,
                                Performance: false,
                                "Special Skills": false,
                                Languages: false,
                             },
                            interested_in: "optional",
                            conditional_rules: [],
                        };

                        const reqs = {
                            ...defaultRequirements,
                            ...(project.submission_requirements || {}),
                            audition_takes_visibility: (project.submission_requirements || {}).audition_takes_visibility || (((project.submission_requirements || {}).min_audition_takes || 0) > 0 ? "required" : "optional"),
                            work_links_visibility: (project.submission_requirements || {}).work_links_visibility || (((project.submission_requirements || {}).min_work_links || 0) > 0 ? "required" : "optional"),
                            portfolio_image_visibility: (project.submission_requirements || {}).portfolio_image_visibility || ((((project.submission_requirements || {}).portfolio || {}).image || 0) > 0 ? "required" : "optional"),
                            portfolio_indian_visibility: (project.submission_requirements || {}).portfolio_indian_visibility || ((((project.submission_requirements || {}).portfolio || {}).indian || 0) > 0 ? "required" : "optional"),
                            portfolio_western_visibility: (project.submission_requirements || {}).portfolio_western_visibility || ((((project.submission_requirements || {}).portfolio || {}).western || 0) > 0 ? "required" : "optional"),
                            fields: {
                                ...defaultRequirements.fields,
                                ...((project.submission_requirements || {}).fields || {}),
                            },
                            portfolio: {
                                ...defaultRequirements.portfolio,
                                ...((project.submission_requirements || {}).portfolio || {}),
                            },
                            skills: {
                                ...defaultRequirements.skills,
                                ...((project.submission_requirements || {}).skills || {}),
                            },
                            custom_questions: {
                                ...((project.submission_requirements || {}).custom_questions || {}),
                            },
                            conditional_rules: [
                                ...((project.submission_requirements || {}).conditional_rules || []),
                            ],
                        };

                        const updateReqs = (patch) => {
                            updateProject({
                                submission_requirements: {
                                    ...reqs,
                                    ...patch,
                                }
                            });
                        };

                        const updateField = (field, value) => {
                            updateReqs({
                                fields: {
                                    ...reqs.fields,
                                    [field]: value,
                                }
                            });
                        };

                        const updatePortfolio = (cat, value) => {
                            updateReqs({
                                portfolio: {
                                    ...reqs.portfolio,
                                    [cat]: parseInt(value) || 0,
                                }
                            });
                        };

                        const updateSkill = (cat, checked) => {
                            updateReqs({
                                skills: {
                                    ...reqs.skills,
                                    [cat]: checked,
                                }
                            });
                        };

                        const updateCustomQuestionReq = (qid, value) => {
                            updateReqs({
                                custom_questions: {
                                    ...reqs.custom_questions,
                                    [qid]: value,
                                }
                            });
                        };

                        return (
                            <div className="space-y-8" data-testid="requirements-engine-container">
                                {/* Strictness & Global settings */}
                                <div className="grid md:grid-cols-2 gap-6 pb-6 border-b border-[#eaeaea]">
                                    <div>
                                        <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2">
                                            Strictness Mode
                                        </label>
                                        <Select
                                            value={reqs.strictness}
                                            onValueChange={(v) => isEditing && updateReqs({ strictness: v })}
                                            disabled={!isEditing}
                                        >
                                            <SelectTrigger className="w-full bg-transparent border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                <SelectItem value="strict">Strict (Blocks submission on validation fail)</SelectItem>
                                                <SelectItem value="standard">Standard (Warnings only, non-blocking)</SelectItem>
                                                <SelectItem value="flexible">Flexible (Validation disabled)</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div>
                                        <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2">
                                            Interested In Field
                                        </label>
                                        <Select
                                            value={reqs.interested_in}
                                            onValueChange={(v) => isEditing && updateReqs({ interested_in: v })}
                                            disabled={!isEditing}
                                        >
                                            <SelectTrigger className="w-full bg-transparent border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                <SelectItem value="required">Required (At least 1 selected)</SelectItem>
                                                <SelectItem value="optional">Optional</SelectItem>
                                                <SelectItem value="hidden">Hidden</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                </div>

                                {/* Standard Profile Fields Configuration */}
                                <div>
                                    <h3 className="text-sm font-semibold text-black/85 mb-4">Profile Fields Configuration</h3>
                                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-4">
                                        {Object.keys(defaultRequirements.fields).map((f) => {
                                            const val = reqs.fields[f] || "optional";
                                            return (
                                                <div key={f} className="flex items-center justify-between border-b border-black/[0.04] pb-2 text-xs">
                                                    <span className="font-mono text-black/75 capitalize">
                                                        {f.replace("_", " ")}
                                                    </span>
                                                    <div className="flex gap-2">
                                                        {["required", "optional", "hidden"].map((opt) => (
                                                            <button
                                                                key={opt}
                                                                type="button"
                                                                disabled={!isEditing}
                                                                onClick={() => updateField(f, opt)}
                                                                className={`px-2 py-1 rounded text-[10px] uppercase font-mono tracking-wider transition-colors ${
                                                                    val === opt
                                                                        ? "bg-black text-white"
                                                                        : "bg-black/[0.04] hover:bg-black/[0.08] text-black/60"
                                                                }`}
                                                            >
                                                                {opt}
                                                            </button>
                                                        ))}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>

                                {/* Custom Questions Validation Configuration */}
                                {(project.custom_questions || []).length > 0 && (
                                    <div className="border-t border-[#eaeaea] pt-6">
                                        <h3 className="text-sm font-semibold text-black/85 mb-4">Custom Questions Validation</h3>
                                        <div className="space-y-3">
                                            {(project.custom_questions || []).map((q) => {
                                                const val = reqs.custom_questions[q.id] || "optional";
                                                return (
                                                    <div key={q.id} className="flex items-center justify-between border-b border-black/[0.04] pb-2 text-xs">
                                                        <span className="text-black/75 truncate flex-1 pr-4">
                                                            {q.question}
                                                        </span>
                                                        <div className="flex gap-2">
                                                            {["required", "optional"].map((opt) => (
                                                                <button
                                                                    key={opt}
                                                                    type="button"
                                                                    disabled={!isEditing}
                                                                    onClick={() => updateCustomQuestionReq(q.id, opt)}
                                                                    className={`px-2 py-1 rounded text-[10px] uppercase font-mono tracking-wider transition-colors ${
                                                                        val === opt
                                                                            ? "bg-black text-white"
                                                                            : "bg-black/[0.04] hover:bg-black/[0.08] text-black/60"
                                                                    }`}
                                                                >
                                                                    {opt}
                                                                </button>
                                                            ))}
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    </div>
                                )}

                                 {/* Media & Portfolio Limits */}
                                 <div className="border-t border-[#eaeaea] pt-6">
                                     <h3 className="text-sm font-semibold text-black/85 mb-4">Media & Portfolio Requirements</h3>
                                     <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
                                         {/* Introduction Video */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Introduction Video
                                                 </label>
                                                 <Select
                                                     value={reqs.intro_video}
                                                     onValueChange={(v) => isEditing && updateReqs({ intro_video: v })}
                                                     disabled={!isEditing}
                                                 >
                                                     <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                         <SelectValue />
                                                     </SelectTrigger>
                                                     <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                         <SelectItem value="required">Required</SelectItem>
                                                         <SelectItem value="optional">Optional</SelectItem>
                                                         <SelectItem value="hidden">Hidden</SelectItem>
                                                     </SelectContent>
                                                 </Select>
                                             </div>
                                             <div className="mt-4">
                                                 <span className="text-[10px] text-black/40">Minimum upload: 1 (Fixed)</span>
                                             </div>
                                         </div>

                                         {/* Audition Takes */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Audition Takes
                                                 </label>
                                                 <div className="space-y-3">
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Visibility</span>
                                                         <Select
                                                             value={reqs.audition_takes_visibility}
                                                             onValueChange={(v) => {
                                                                 if (!isEditing) return;
                                                                 const patch = { audition_takes_visibility: v };
                                                                 if (v === "hidden") {
                                                                     patch.min_audition_takes = 0;
                                                                 }
                                                                 updateReqs(patch);
                                                             }}
                                                             disabled={!isEditing}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 <SelectItem value="required">Required</SelectItem>
                                                                 <SelectItem value="optional">Optional</SelectItem>
                                                                 <SelectItem value="hidden">Hidden</SelectItem>
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Min Count</span>
                                                         <Select
                                                             value={String(reqs.min_audition_takes || 0)}
                                                             onValueChange={(v) => isEditing && updateReqs({ min_audition_takes: parseInt(v) })}
                                                             disabled={!isEditing || reqs.audition_takes_visibility === "hidden"}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 {[0, 1, 2, 3, 4, 5].map((n) => (
                                                                     <SelectItem key={n} value={String(n)}>{n} Take(s)</SelectItem>
                                                                 ))}
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                 </div>
                                             </div>
                                         </div>

                                         {/* Work Links */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Work Links
                                                 </label>
                                                 <div className="space-y-3">
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Visibility</span>
                                                         <Select
                                                             value={reqs.work_links_visibility}
                                                             onValueChange={(v) => {
                                                                 if (!isEditing) return;
                                                                 const patch = { work_links_visibility: v };
                                                                 if (v === "hidden") {
                                                                     patch.min_work_links = 0;
                                                                 }
                                                                 updateReqs(patch);
                                                             }}
                                                             disabled={!isEditing}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 <SelectItem value="required">Required</SelectItem>
                                                                 <SelectItem value="optional">Optional</SelectItem>
                                                                 <SelectItem value="hidden">Hidden</SelectItem>
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Min Count</span>
                                                         <Select
                                                             value={String(reqs.min_work_links || 0)}
                                                             onValueChange={(v) => isEditing && updateReqs({ min_work_links: parseInt(v) })}
                                                             disabled={!isEditing || reqs.work_links_visibility === "hidden"}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 {[0, 1, 2, 3, 4, 5].map((n) => (
                                                                     <SelectItem key={n} value={String(n)}>{n} Link(s)</SelectItem>
                                                                 ))}
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                 </div>
                                             </div>
                                         </div>

                                         {/* Portfolio (General) Images */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Portfolio (General) Images
                                                 </label>
                                                 <div className="space-y-3">
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Visibility</span>
                                                         <Select
                                                             value={reqs.portfolio_image_visibility}
                                                             onValueChange={(v) => {
                                                                 if (!isEditing) return;
                                                                 const patch = { portfolio_image_visibility: v };
                                                                 if (v === "hidden") {
                                                                     updateReqs({
                                                                         ...patch,
                                                                         portfolio: {
                                                                             ...reqs.portfolio,
                                                                             image: 0
                                                                         }
                                                                     });
                                                                 } else {
                                                                     updateReqs(patch);
                                                                 }
                                                             }}
                                                             disabled={!isEditing}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 <SelectItem value="required">Required</SelectItem>
                                                                 <SelectItem value="optional">Optional</SelectItem>
                                                                 <SelectItem value="hidden">Hidden</SelectItem>
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Min Count</span>
                                                         <input
                                                             type="number"
                                                             min="0"
                                                             max="10"
                                                             value={reqs.portfolio.image}
                                                             onChange={(e) => updatePortfolio("image", e.target.value)}
                                                             disabled={!isEditing || reqs.portfolio_image_visibility === "hidden"}
                                                             className="w-full bg-white border border-black/[0.10] focus:border-black/40 outline-none rounded-sm px-3 text-xs h-9 text-black/85"
                                                         />
                                                     </div>
                                                 </div>
                                             </div>
                                         </div>

                                         {/* Indian Look Images */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Indian Look Images
                                                 </label>
                                                 <div className="space-y-3">
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Visibility</span>
                                                         <Select
                                                             value={reqs.portfolio_indian_visibility}
                                                             onValueChange={(v) => {
                                                                 if (!isEditing) return;
                                                                 const patch = { portfolio_indian_visibility: v };
                                                                 if (v === "hidden") {
                                                                     updateReqs({
                                                                         ...patch,
                                                                         portfolio: {
                                                                             ...reqs.portfolio,
                                                                             indian: 0
                                                                         }
                                                                     });
                                                                 } else {
                                                                     updateReqs(patch);
                                                                 }
                                                             }}
                                                             disabled={!isEditing}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 <SelectItem value="required">Required</SelectItem>
                                                                 <SelectItem value="optional">Optional</SelectItem>
                                                                 <SelectItem value="hidden">Hidden</SelectItem>
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Min Count</span>
                                                         <input
                                                             type="number"
                                                             min="0"
                                                             max="10"
                                                             value={reqs.portfolio.indian}
                                                             onChange={(e) => updatePortfolio("indian", e.target.value)}
                                                             disabled={!isEditing || reqs.portfolio_indian_visibility === "hidden"}
                                                             className="w-full bg-white border border-black/[0.10] focus:border-black/40 outline-none rounded-sm px-3 text-xs h-9 text-black/85"
                                                         />
                                                     </div>
                                                 </div>
                                             </div>
                                         </div>

                                         {/* Western Look Images */}
                                         <div className="bg-slate-50/30 border border-[#eaeaea] rounded-xl p-4 flex flex-col justify-between min-h-[140px]">
                                             <div>
                                                 <label className="block text-[11px] text-black/45 tracking-widest uppercase mb-2 font-semibold">
                                                     Western Look Images
                                                 </label>
                                                 <div className="space-y-3">
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Visibility</span>
                                                         <Select
                                                             value={reqs.portfolio_western_visibility}
                                                             onValueChange={(v) => {
                                                                 if (!isEditing) return;
                                                                 const patch = { portfolio_western_visibility: v };
                                                                 if (v === "hidden") {
                                                                     updateReqs({
                                                                         ...patch,
                                                                         portfolio: {
                                                                             ...reqs.portfolio,
                                                                             western: 0
                                                                         }
                                                                     });
                                                                 } else {
                                                                     updateReqs(patch);
                                                                 }
                                                             }}
                                                             disabled={!isEditing}
                                                         >
                                                             <SelectTrigger className="w-full bg-white border border-black/[0.10] rounded-sm text-xs h-9 px-3">
                                                                 <SelectValue />
                                                             </SelectTrigger>
                                                             <SelectContent className="bg-white border border-[#eaeaea] text-black shadow-xl">
                                                                 <SelectItem value="required">Required</SelectItem>
                                                                 <SelectItem value="optional">Optional</SelectItem>
                                                                 <SelectItem value="hidden">Hidden</SelectItem>
                                                             </SelectContent>
                                                         </Select>
                                                     </div>
                                                     <div>
                                                         <span className="text-[10px] text-black/45 uppercase tracking-wider block mb-1">Min Count</span>
                                                         <input
                                                             type="number"
                                                             min="0"
                                                             max="10"
                                                             value={reqs.portfolio.western}
                                                             onChange={(e) => updatePortfolio("western", e.target.value)}
                                                             disabled={!isEditing || reqs.portfolio_western_visibility === "hidden"}
                                                             className="w-full bg-white border border-black/[0.10] focus:border-black/40 outline-none rounded-sm px-3 text-xs h-9 text-black/85"
                                                         />
                                                     </div>
                                                 </div>
                                             </div>
                                         </div>
                                     </div>
                                 </div>

                                {/* Required Skill Categories */}
                                <div className="border-t border-[#eaeaea] pt-6">
                                    <h3 className="text-sm font-semibold text-black/85 mb-2">Mandatory Skill Categories</h3>
                                    <p className="text-xs text-black/45 mb-4">
                                        If active, the talent must select at least one skill in that category.
                                    </p>
                                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                                        {Object.keys(defaultRequirements.skills).map((cat) => (
                                            <label key={cat} className="flex items-center gap-2.5 text-xs text-black/75 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={reqs.skills[cat] || false}
                                                    onChange={(e) => isEditing && updateSkill(cat, e.target.checked)}
                                                    disabled={!isEditing}
                                                    className="w-4 h-4 accent-black disabled:opacity-60"
                                                />
                                                <span>{cat}</span>
                                            </label>
                                        ))}
                                    </div>
                                </div>

                                {/* Conditional Rules Builder */}
                                <div className="border-t border-[#eaeaea] pt-6">
                                    <h3 className="text-sm font-semibold text-black/85 mb-2">Conditional Rules Engine</h3>
                                    <p className="text-xs text-black/45 mb-4">
                                        Require specific video tasks based on the talent's custom question answers.
                                    </p>

                                    {/* Existing Rules List */}
                                    <div className="space-y-3 mb-4">
                                        {reqs.conditional_rules.map((rule, idx) => {
                                            const q = (project.custom_questions || []).find((cq) => cq.id === rule.question_id);
                                            return (
                                                <div key={idx} className="flex items-center gap-3 bg-black/[0.02] border border-black/[0.05] rounded-lg p-3 text-xs">
                                                    <span className="flex-1">
                                                        If <strong>"{q ? q.question : "Deleted Question"}"</strong> equals <strong>"{rule.trigger_value}"</strong>,
                                                        then require video upload labeled <strong>"{rule.video_label}"</strong>.
                                                    </span>
                                                    {isEditing && (
                                                        <button
                                                            type="button"
                                                            onClick={() => {
                                                                updateReqs({
                                                                    conditional_rules: reqs.conditional_rules.filter((_, i) => i !== idx),
                                                                });
                                                            }}
                                                            className="text-black/40 hover:text-red-600 transition-colors"
                                                        >
                                                            <X className="w-4 h-4" />
                                                        </button>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>

                                    {/* Add New Rule Form */}
                                    {isEditing && (project.custom_questions || []).length > 0 && (
                                        <ConditionalRuleForm
                                            questions={project.custom_questions}
                                            onAdd={(newRule) => {
                                                updateReqs({
                                                    conditional_rules: [...reqs.conditional_rules, newRule],
                                                });
                                            }}
                                        />
                                    )}
                                    {isEditing && (project.custom_questions || []).length === 0 && (
                                        <p className="text-xs text-black/45 italic">
                                            Create custom questions above first to define conditional rules.
                                        </p>
                                    )}
                                </div>
                            </div>
                        );
                    })()}
                </section>
            )}

            {/* Share submission link */}
            {isEdit && (
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl p-6 md:p-8 mt-6"
                    data-testid="submission-link-section"
                >
                    <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
                        <div>
                            <p className="eyebrow">Talent Submission Link</p>
                            <p className="text-xs text-black/45 mt-1">
                                Share this public link with talents. They can
                                submit intro video, takes and images.
                            </p>
                        </div>
                        <div className="flex gap-2 flex-wrap items-center">
                            <a
                                href={submissionUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-[#eaeaea] hover:border-black/[0.20] rounded-sm text-black/70 hover:text-black transition-colors"
                            >
                                <ExternalLink className="w-3.5 h-3.5" /> Preview
                            </a>
                            <button
                                onClick={copySubmitLink}
                                data-testid="copy-submit-link-btn"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-[#eaeaea] hover:border-black/[0.20] rounded-sm text-black/70 hover:text-black transition-colors"
                            >
                                <Copy className="w-3.5 h-3.5" /> Copy
                            </button>
                            <WhatsAppShareButton
                                onClick={shareWhatsApp}
                                data-testid="whatsapp-submit-link-btn"
                            />
                            <button
                                type="button"
                                onClick={() => setCollapsedSections(prev => ({ ...prev, submissionLink: !prev.submissionLink }))}
                                className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors shrink-0"
                                aria-label={collapsedSections.submissionLink ? "Expand submission link" : "Collapse submission link"}
                            >
                                <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.submissionLink ? "-rotate-90" : ""}`} />
                            </button>
                        </div>
                    </div>
                    {!collapsedSections.submissionLink && (
                        <div className="border border-[#eaeaea] bg-[#fafaf8] px-4 py-3 tg-mono text-xs text-black/70 break-all">
                            {submissionUrl}
                        </div>
                    )}
                </section>
            )}

            </div>

            {/* Submissions Review */}
            {visitedTabs.has("submissions") && isEdit && (
                <div style={{ display: activeTab === "submissions" ? "block" : "none" }} data-testid="project-tab-panel-submissions">
                <section
                    className="border border-[#eaeaea] bg-white rounded-xl mt-6"
                    data-testid="submissions-review-section"
                >
                    <div className="px-6 py-4 border-b border-[#eaeaea] flex items-center justify-between gap-3 flex-wrap">
                        <div>
                            <p className="eyebrow">Submissions</p>
                            <p className="text-xs text-black/45 mt-1">
                                {submissions.length} total · {approvedCount} approved · {rejectedCount} rejected
                            </p>
                        </div>
                        <div className="flex items-center gap-2">
                            <Link
                                to={`/admin/projects/${id}/submissions`}
                                className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#eaeaea] hover:border-black/35 hover:bg-black/[0.02] rounded-md text-xs text-black/75 hover:text-black font-semibold transition-all shadow-sm"
                            >
                                <ExternalLink className="w-3.5 h-3.5" />
                                Open Review Center
                            </Link>
                            <button
                                type="button"
                                onClick={() => setCollapsedSections(prev => ({ ...prev, submissions: !prev.submissions }))}
                                className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-black/[0.02] rounded-md text-black/55 hover:text-black transition-colors shrink-0"
                                aria-label={collapsedSections.submissions ? "Expand submissions" : "Collapse submissions"}
                            >
                                <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${collapsedSections.submissions ? "-rotate-90" : ""}`} />
                            </button>
                        </div>
                    </div>

                    {!collapsedSections.submissions && (
                        <div className="w-full">
                            {submissions.length === 0 ? (
                                <div className="p-8 text-center text-black/40 text-sm">
                                    No submissions yet. Share the link above with
                                    talents.
                                </div>
                            ) : (
                                <>
                                    {/* Summary Stats Row */}
                                    <div className="px-4 md:px-6 py-4 bg-black/[0.01] border-b border-[#eaeaea] grid grid-cols-3 sm:grid-cols-6 gap-4 select-none">
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Total</p>
                                            <p className="text-lg font-bold text-black/90 mt-0.5">{submissions.length}</p>
                                        </div>
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Pending</p>
                                            <p className="text-lg font-bold text-amber-600 mt-0.5">{pendingCount}</p>
                                        </div>
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Approved</p>
                                            <p className="text-lg font-bold text-green-600 mt-0.5">{approvedCount}</p>
                                        </div>
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Rejected</p>
                                            <p className="text-lg font-bold text-red-600 mt-0.5">{rejectedCount}</p>
                                        </div>
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Updated</p>
                                            <p className="text-lg font-bold text-blue-600 mt-0.5">{updatedCount}</p>
                                        </div>
                                        <div className="text-center sm:text-left">
                                            <p className="text-[10px] font-semibold text-black/40 uppercase tracking-wider font-mono">Hold</p>
                                            <p className="text-lg font-bold text-purple-600 mt-0.5">
                                                {submissions.filter((s) => s.decision === "hold").length}
                                            </p>
                                        </div>
                                    </div>

                                    <div
                                        className="px-4 md:px-6 py-3 border-b border-[#eaeaea] flex items-center gap-1.5 flex-wrap"
                                        data-testid="submission-filters"
                                    >
                                        <button
                                            type="button"
                                            onClick={() => setSubmissionFilter("all")}
                                            data-testid="filter-chip-all"
                                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "all" ? "border-black bg-black text-white" : "border-[#eaeaea] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                        >
                                            All
                                            <span className={`ml-2 tg-mono ${submissionFilter === "all" ? "text-white/60" : "text-black/40"}`}>
                                                {submissions.length}
                                            </span>
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setSubmissionFilter("pending")}
                                            data-testid="filter-chip-pending"
                                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "pending" ? "border-black bg-black text-white" : "border-[#eaeaea] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                        >
                                            Pending
                                            <span className={`ml-2 tg-mono ${submissionFilter === "pending" ? "text-white/60" : "text-black/40"}`}>
                                                {pendingCount}
                                            </span>
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setSubmissionFilter("approved")}
                                            data-testid="filter-chip-approved"
                                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "approved" ? "border-black bg-black text-white" : "border-[#eaeaea] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                        >
                                            Approved
                                            <span className={`ml-2 tg-mono ${submissionFilter === "approved" ? "text-white/60" : "text-black/40"}`}>
                                                {approvedCount}
                                            </span>
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setSubmissionFilter("rejected")}
                                            data-testid="filter-chip-rejected"
                                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "rejected" ? "border-black bg-black text-white" : "border-[#eaeaea] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                        >
                                            Rejected
                                            <span className={`ml-2 tg-mono ${submissionFilter === "rejected" ? "text-white/60" : "text-black/40"}`}>
                                                {rejectedCount}
                                            </span>
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setSubmissionFilter("updated")}
                                            data-testid="filter-chip-updated"
                                            className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "updated" ? "border-black bg-black text-white" : "border-[#eaeaea] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                        >
                                            Updated
                                            <span className={`ml-2 tg-mono ${submissionFilter === "updated" ? "text-white/60" : "text-black/40"}`}>
                                                {updatedCount}
                                            </span>
                                        </button>
                                    </div>
                                    <div className="max-h-[500px] overflow-y-auto divide-y divide-black/[0.06] border-b border-black/[0.06]">
                                        {filteredSubmissions.map((s) => (
                                            <SubmissionRow
                                                key={s.id}
                                                submission={s}
                                                onDecision={(d) => setDecision(s.id, d)}
                                                onDelete={isAdminRole ? () => confirmDeleteSubmission(s.id) : null}
                                            />
                                        ))}
                                        {filteredSubmissions.length === 0 && (
                                            <div
                                                className="p-8 text-center text-black/35 text-sm"
                                                data-testid="filter-empty-state"
                                            >
                                                No submissions match this filter.
                                            </div>
                                        )}
                                    </div>
                                </>
                            )}
                        </div>
                    )}
                </section>
                </div>
            )}

            {/* Casting Pipeline */}
            {visitedTabs.has("pipeline") && isEdit && (
                <div style={{ display: activeTab === "pipeline" ? "block" : "none" }} data-testid="project-tab-panel-pipeline">
                    <ProjectPipeline projectId={id} />
                </div>
            )}

            {/* Material viewer modal */}
            {showMaterialModal && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterialModal(false)}
                    onRemove={confirmRemoveMaterial}
                />
            )}



            <ConfirmDeleteDialog
                open={confirmDeleteOpen}
                title={`Delete "${project.brand_name || "this project"}"?`}
                description="This permanently removes the project, all talent submissions for it, and all project materials. Client links that reference this project keep their snapshots. This cannot be undone."
                confirmLabel="Delete project"
                typeToConfirm="DELETE"
                onCancel={() => setConfirmDeleteOpen(false)}
                onConfirm={deleteProject}
            />
            
            <ConfirmDeleteDialog
                open={!!deleteSubmissionId}
                title="Delete Submission"
                description={`Are you sure you want to delete this submission? This action cannot be undone.`}
                confirmLabel="Delete Submission"
                typeToConfirm="DELETE"
                onCancel={() => setDeleteSubmissionId(null)}
                onConfirm={handleDeleteSubmission}
            />
            
            <ConfirmDeleteDialog
                open={!!deleteMaterialId}
                title="Remove Material"
                description={`Are you sure you want to remove this material? This action cannot be undone.`}
                confirmLabel="Remove"
                typeToConfirm="REMOVE"
                onCancel={() => setDeleteMaterialId(null)}
                onConfirm={handleRemoveMaterial}
            />
        </div>
    );
}

const SubmissionRow = React.memo(function SubmissionRow({ submission, onDecision, onDelete }) {
    const s = submission;
    const [expanded, setExpanded] = useState(false);
    const meta = {
        pending: { icon: Clock, label: "Pending", color: "text-black/60", bg: "bg-black/5" },
        approved: { icon: Check, label: "Approved", color: "text-green-600", bg: "bg-green-50 text-green-700 border-green-200" },
        rejected: {
            icon: XCircle,
            label: "Rejected",
            color: "text-red-600",
            bg: "bg-red-50 text-red-700 border-red-200",
        },
        hold: { icon: PauseCircle, label: "Hold", color: "text-purple-600", bg: "bg-purple-50 text-purple-700 border-purple-200" },
    }[s.decision || "pending"];
    
    const isUpdated = s.status === "updated";
    
    const borderColors = {
        approved: "border-l-[4px] border-l-green-500",
        rejected: "border-l-[4px] border-l-red-500",
        hold: "border-l-[4px] border-l-purple-500",
        updated: "border-l-[4px] border-l-blue-500",
        pending: "border-l-[4px] border-l-amber-500",
    };
    const borderKey = isUpdated ? "updated" : (s.decision || "pending");
    const borderClass = borderColors[borderKey];

    const mediaCounts = {
        intro: (s.media || []).filter((m) => m.category === "intro_video" || m.category === "video").length,
        takes: (s.media || []).filter(
            (m) =>
                m.category === "take" ||
                m.category === "take_1" ||
                m.category === "take_2" ||
                m.category === "take_3",
        ).length,
        images: (s.media || []).filter(
            (m) => m.category === "image" || m.category === "indian" || m.category === "western" || m.category === "portfolio",
        ).length,
    };

    const cover =
        (s.media || []).find((m) => m.id === s.cover_media_id) ||
        (s.media || []).find(
            (m) =>
                m.category === "profile" ||
                m.category === "headshot" ||
                m.category === "portfolio" ||
                m.category === "image" ||
                m.category === "indian" ||
                m.category === "western",
        );

    const heightVal = s.talent_height || s.form_data?.height || s.height || "—";
    const ageVal = s.effective_age !== undefined && s.effective_age !== null ? `${s.effective_age} yrs` : "—";

    return (
        <div data-testid={`submission-row-${s.id}`} className={`${borderClass}`}>
            {/* Desktop Row: Hidden on Mobile */}
            <div
                className="hidden md:flex px-6 py-4 items-center justify-between gap-4 flex-wrap hover:bg-black/[0.015] transition-colors"
            >
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-display text-lg text-black/90">
                            {s.talent_name}
                        </span>
                        <span
                            className={`inline-flex items-center gap-1 text-[10px] tracking-widest uppercase ${meta.color}`}
                        >
                            <meta.icon className="w-3 h-3" /> {meta.label}
                        </span>
                        {isUpdated && (
                            <span
                                className="inline-flex items-center gap-1 text-[10px] tracking-widest uppercase text-blue-700 bg-blue-50 border border-blue-200 px-1.5 py-0.5 rounded-sm"
                                data-testid={`updated-badge-${s.id}`}
                                title="Talent updated this submission after the previous decision"
                            >
                                Updated
                            </span>
                        )}
                        {s.status === "draft" && (
                            <span className="text-[10px] tracking-widest uppercase text-black/35">
                                · Draft
                            </span>
                        )}
                    </div>
                    <div className="text-xs text-black/45 tg-mono mt-1 truncate flex items-center gap-1.5 flex-wrap">
                        <span>{s.talent_email}</span>
                        {s.talent_phone && <span>· {s.talent_phone}</span>}
                        {s.effective_age !== undefined && s.effective_age !== null && <span>· {s.effective_age} yrs</span>}
                        {s.submitted_age_override !== undefined && s.submitted_age_override !== null && (
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[8px] font-mono font-medium bg-amber-50 text-amber-700 border border-amber-200 uppercase tracking-wider">
                                Override
                            </span>
                        )}
                    </div>
                    <div className="text-[11px] text-black/45 mt-2 flex items-center gap-3">
                        <span title="Intro Video">🎥 {mediaCounts.intro || 0} Intro</span>
                        <span className="opacity-40">·</span>
                        <span title="Audition Takes">🎬 {mediaCounts.takes || 0} Takes</span>
                        <span className="opacity-40">·</span>
                        <span title="Images">📷 {mediaCounts.images || 0} Images</span>
                    </div>
                </div>
                <div className="flex gap-1 flex-wrap">
                    <button
                        onClick={() => onDecision("approved")}
                        className="text-xs px-3 py-2 border border-[#eaeaea] hover:border-green-600 hover:text-green-600 rounded-sm text-black/70 transition-colors"
                        data-testid={`approve-${s.id}`}
                        title="Approve"
                    >
                        <Check className="w-3.5 h-3.5" />
                    </button>
                    <button
                        onClick={() => onDecision("hold")}
                        className="text-xs px-3 py-2 border border-[#eaeaea] hover:border-purple-600 hover:text-purple-600 rounded-sm text-black/70 transition-colors"
                        data-testid={`hold-${s.id}`}
                        title="Hold"
                    >
                        <PauseCircle className="w-3.5 h-3.5" />
                    </button>
                    <button
                        onClick={() => onDecision("rejected")}
                        className="text-xs px-3 py-2 border border-[#eaeaea] hover:border-red-600 hover:text-red-600 rounded-sm text-black/70 transition-colors"
                        data-testid={`reject-${s.id}`}
                        title="Reject"
                    >
                        <XCircle className="w-3.5 h-3.5" />
                    </button>
                    {onDelete && (
                        <button
                            onClick={onDelete}
                            className="text-xs px-3 py-2 border border-[#eaeaea] hover:border-black/40 text-black/50 rounded-sm transition-colors"
                            title="Delete"
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                        </button>
                    )}
                </div>
            </div>

            {/* Mobile Premium Card: Hidden on Desktop */}
            <div className="md:hidden p-3 border-b border-black/[0.06] bg-white space-y-2.5">
                {/* Row 1: Profile image, Name, Age, Height, Current stage badge */}
                <div className="flex items-start gap-2.5">
                    <div className="w-10 h-14 rounded overflow-hidden bg-black/[0.03] border border-black/[0.06] shrink-0 flex items-center justify-center">
                        {cover ? (
                            <img
                                src={cover.url}
                                alt={s.talent_name}
                                className="w-full h-full object-cover"
                            />
                        ) : (
                            <User className="w-4 h-4 text-black/20" />
                        )}
                    </div>
                    <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-1.5 flex-wrap">
                            <span className="font-display text-sm font-bold text-black/90 truncate">
                                {s.talent_name}
                            </span>
                            <span
                                className={`inline-flex items-center gap-0.5 text-[8.5px] px-2 py-0.5 rounded-full border font-mono uppercase tracking-wider ${meta.bg ? meta.bg : "border-[#eaeaea] text-black/60 bg-black/[0.02]"}`}
                            >
                                {meta.label}
                            </span>
                        </div>
                        <div className="text-[11px] text-black/55 mt-1 flex items-center gap-1.5 flex-wrap">
                            <span>Age: <strong className="text-black/85">{ageVal}</strong></span>
                            <span className="text-black/15">•</span>
                            <span>Ht: <strong className="text-black/85">{heightVal}</strong></span>
                            {isUpdated && (
                                <>
                                    <span className="text-black/15">•</span>
                                    <span className="text-[8px] font-semibold text-blue-600 uppercase tracking-wider">Updated</span>
                                </>
                            )}
                        </div>
                        <div className="text-[10px] text-black/55 mt-1.5 flex items-center gap-2 flex-wrap font-mono">
                            <span>🎥 {mediaCounts.intro || 0} Intro</span>
                            <span className="text-black/15">•</span>
                            <span>🎬 {mediaCounts.takes || 0} Takes</span>
                            <span className="text-black/15">•</span>
                            <span>📷 {mediaCounts.images || 0} Images</span>
                        </div>
                    </div>
                </div>

                {/* Row 2: Call, WhatsApp, Review (Grid layout with high targets but compact margins) */}
                <div className="grid grid-cols-3 gap-2 pt-0.5">
                    {s.talent_phone ? (
                        <a
                            href={`tel:${s.talent_phone}`}
                            className="flex items-center justify-center gap-1.5 py-2 px-2.5 border border-[#eaeaea] rounded text-xs font-semibold text-black/70 active:bg-black/5 transition-colors"
                        >
                            <Phone className="w-3.5 h-3.5 text-black/50" />
                            <span>Call</span>
                        </a>
                    ) : (
                        <span className="flex items-center justify-center gap-1.5 py-2 px-2.5 border border-black/[0.04] rounded text-xs text-black/30 bg-black/[0.01] cursor-not-allowed">
                            <Phone className="w-3.5 h-3.5 opacity-30" />
                            <span>Call</span>
                        </span>
                    )}

                    {s.talent_phone ? (
                        <a
                            href={`https://wa.me/${s.talent_phone.replace(/\D/g, "")}`}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center justify-center gap-1.5 py-2 px-2.5 border border-[#25D366]/35 rounded text-xs font-semibold text-[#25D366] bg-[#25D366]/5 active:bg-[#25D366]/10 transition-colors"
                        >
                            <MessageSquare className="w-3.5 h-3.5 text-[#25D366]" />
                            <span>WhatsApp</span>
                        </a>
                    ) : (
                        <span className="flex items-center justify-center gap-1.5 py-2 px-2.5 border border-black/[0.04] rounded text-xs text-black/30 bg-black/[0.01] cursor-not-allowed">
                            <MessageSquare className="w-3.5 h-3.5 opacity-30" />
                            <span>WhatsApp</span>
                        </span>
                    )}

                </div>

                {/* Row 3: Expand Details */}
                <div className="border-t border-black/[0.03] pt-1">
                    <button
                        type="button"
                        onClick={() => setExpanded(!expanded)}
                        className="w-full flex items-center justify-between py-1 text-[11px] text-black/45 hover:text-black transition-colors"
                    >
                        <span>{expanded ? "Hide details" : "Expand details"}</span>
                        <ChevronDown className={`w-3.5 h-3.5 transform transition-transform duration-200 ${expanded ? "rotate-180" : ""}`} />
                    </button>

                    {expanded && (
                        <div className="mt-2.5 space-y-2.5 pb-1 text-[11px] text-black/70 animate-fade-in">
                            <div className="space-y-0.5">
                                <p className="text-[9px] text-black/40 uppercase tracking-wider font-mono">Contact Details</p>
                                <p className="font-mono text-black/80">{s.talent_email}</p>
                                {s.talent_phone && <p className="text-black/80">{s.talent_phone}</p>}
                            </div>

                            <div className="space-y-0.5">
                                <p className="text-[9px] text-black/40 uppercase tracking-wider font-mono">Audition Summary</p>
                                <p className="text-black/80">
                                    {mediaCounts.intro ? "1 intro video" : "no intro video"} · {mediaCounts.takes} takes · {mediaCounts.images} images
                                </p>
                            </div>

                            {/* Mobile Decision Buttons inside collapsed drawer */}
                            <div className="pt-2 border-t border-black/[0.03] flex items-center gap-2 flex-wrap">
                                <button
                                    onClick={() => onDecision("approved")}
                                    className={`flex-1 flex items-center justify-center gap-1 py-1.5 border rounded text-xs font-semibold transition-colors ${s.decision === "approved" ? "bg-green-600 text-white border-green-600" : "border-[#eaeaea] text-black/70 active:bg-black/5"}`}
                                >
                                    <Check className="w-3.5 h-3.5" />
                                    <span>Approve</span>
                                </button>
                                <button
                                    onClick={() => onDecision("hold")}
                                    className={`flex-1 flex items-center justify-center gap-1 py-1.5 border rounded text-xs font-semibold transition-colors ${s.decision === "hold" ? "bg-purple-600 text-white border-purple-600" : "border-[#eaeaea] text-black/70 active:bg-black/5"}`}
                                >
                                    <PauseCircle className="w-3.5 h-3.5" />
                                    <span>Hold</span>
                                </button>
                                <button
                                    onClick={() => onDecision("rejected")}
                                    className={`flex-1 flex items-center justify-center gap-1 py-1.5 border rounded text-xs font-semibold transition-colors ${s.decision === "rejected" ? "bg-red-600 text-white border-red-600" : "border-[#eaeaea] text-black/70 active:bg-black/5"}`}
                                >
                                    <XCircle className="w-3.5 h-3.5" />
                                    <span>Reject</span>
                                </button>
                                {onDelete && (
                                    <button
                                        onClick={onDelete}
                                        className="p-1.5 border border-red-200 text-red-600 rounded active:bg-red-50"
                                        title="Delete Submission"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                )}
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
});

function UploadTile({
    title,
    icon: Icon,
    accept,
    multiple,
    onPick,
    inputRef,
    uploading,
    testid,
    hint,
}) {
    return (
        <button
            onClick={() => inputRef.current?.click()}
            data-testid={`${testid}-btn`}
            disabled={uploading}
            className="border border-dashed border-[#eaeaea] hover:border-black/[0.18] p-5 text-left transition-colors bg-white rounded-xl"
        >
            <div className="flex items-center justify-between mb-3">
                <Icon className="w-4 h-4 text-black/50" strokeWidth={1.5} />
                {uploading ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-black/50" />
                ) : (
                    <Upload className="w-3.5 h-3.5 text-black/40" />
                )}
            </div>
            <div className="text-sm font-display text-black/85">{title}</div>
            <div className="text-[10px] tg-mono text-black/45 mt-1">
                {hint || (multiple ? "Multiple allowed" : "Single file")}
            </div>
            <input
                ref={inputRef}
                type="file"
                accept={accept}
                multiple={multiple}
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length)
                        onPick(Array.from(e.target.files));
                    e.target.value = "";
                }}
            />
        </button>
    );
}

function ConditionalRuleForm({ questions, onAdd }) {
    const [questionId, setQuestionId] = useState(questions[0]?.id || "");
    const [triggerValue, setTriggerValue] = useState("");
    const [videoLabel, setVideoLabel] = useState("");

    const handleAdd = () => {
        if (!questionId || !triggerValue.trim() || !videoLabel.trim()) {
            toast.error("Please fill in all conditional rule fields");
            return;
        }
        onAdd({
            question_id: questionId,
            trigger_value: triggerValue.trim(),
            video_label: videoLabel.trim(),
        });
        setTriggerValue("");
        setVideoLabel("");
    };

    return (
        <div className="flex flex-wrap gap-4 items-end bg-black/[0.01] border border-dashed border-black/[0.1] rounded-xl p-4 mt-2">
            <div className="flex-1 min-w-[200px]">
                <label className="block text-[10px] text-black/45 uppercase tracking-wider mb-1">
                    If Question
                </label>
                <select
                    value={questionId}
                    onChange={(e) => setQuestionId(e.target.value)}
                    className="w-full bg-white border border-[#eaeaea] rounded-lg px-3 h-9 text-xs"
                >
                    {questions.map((q) => (
                        <option key={q.id} value={q.id}>
                            {q.question}
                        </option>
                    ))}
                </select>
            </div>
            <div className="w-[120px]">
                <label className="block text-[10px] text-black/45 uppercase tracking-wider mb-1">
                    Answer Equals
                </label>
                <input
                    type="text"
                    value={triggerValue}
                    onChange={(e) => setTriggerValue(e.target.value)}
                    placeholder="e.g. YES"
                    className="w-full bg-white border border-[#eaeaea] rounded-lg px-3 h-9 text-xs"
                />
            </div>
            <div className="flex-1 min-w-[200px]">
                <label className="block text-[10px] text-black/45 uppercase tracking-wider mb-1">
                    Require Video Labeled
                </label>
                <input
                    type="text"
                    value={videoLabel}
                    onChange={(e) => setVideoLabel(e.target.value)}
                    placeholder="e.g. Driving Reference"
                    className="w-full bg-white border border-[#eaeaea] rounded-lg px-3 h-9 text-xs"
                />
            </div>
            <button
                type="button"
                onClick={handleAdd}
                className="bg-black text-white hover:bg-black/90 px-4 h-9 rounded-lg text-xs font-medium inline-flex items-center gap-1.5 shrink-0"
            >
                <Plus className="w-3.5 h-3.5" /> Add Rule
            </button>
        </div>
    );
}

