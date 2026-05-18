import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import { toast } from "sonner";
import MaterialModal from "@/components/MaterialModal";
import ForwardToLinkModal from "@/components/ForwardToLinkModal";
import BudgetLines from "@/components/BudgetLines";
import ProjectPipeline from "@/pages/ProjectPipeline";
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
    Sparkles,
    Cloud,
    PauseCircle,
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
};

// ISSUE 2 & 3: More robust file validation using startsWith()
const MAX_FILE_SIZE = {
    script: 10 * 1024 * 1024, // 10MB
    image: 20 * 1024 * 1024,  // 20MB
    audio: 30 * 1024 * 1024,  // 30MB
    video_file: 100 * 1024 * 1024, // 100MB
};

function TextField({ label, value, onChange, type = "text", ...rest }) {
    return (
        <label className="block">
            <span className="text-[11px] text-black/45 tracking-widest uppercase">
                {label}
            </span>
            <input
                type={type}
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
                className="mt-2 w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2.5 text-sm text-black/85 placeholder:text-black/30"
                {...rest}
            />
        </label>
    );
}

export default function ProjectEdit() {
    const { id } = useParams();
    const nav = useNavigate();
    const isEdit = Boolean(id);
    const isAdminRole = isAdmin();
    const isMounted = useRef(true);

    const [project, setProject] = useState(empty);
    const [saving, setSaving] = useState(false);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(null); // category string
    const [videoInput, setVideoInput] = useState("");
    const [showMaterialModal, setShowMaterialModal] = useState(false);
    const [submissions, setSubmissions] = useState([]);
    const [reviewingSid, setReviewingSid] = useState(null);
    const [showForwardModal, setShowForwardModal] = useState(false);
    const [submissionFilter, setSubmissionFilter] = useState("all");
    const [deleteSubmissionId, setDeleteSubmissionId] = useState(null);
    const [deleteMaterialId, setDeleteMaterialId] = useState(null);
    const scriptRef = useRef();
    const imageRef = useRef();
    const audioRef = useRef();
    const videoFileRef = useRef();

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
                    setProject({ ...empty, ...data });
                    setLoading(false);
                }
                loadSubmissions(id);
            } catch {
                if (isMounted.current) {
                    toast.error("Failed to load project");
                    setLoading(false);
                }
            }
        })();
    }, [id, isEdit, loadSubmissions]);

    const setDecision = async (sid, decision) => {
        await adminApi.post(`/projects/${id}/submissions/${sid}/decision`, {
            decision,
        });
        toast.success(`Marked ${decision}`);
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
        ? `${window.location.origin}/submit/${project.slug}`
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
        const msg = encodeURIComponent(
            `Talentgram x ${project.brand_name}\n\nAudition submission link: ${submissionUrl}`,
        );
        window.open(`https://wa.me/?text=${msg}`, "_blank");
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
        updateProject({
            custom_questions: [
                ...(project.custom_questions || []),
                { id: crypto.randomUUID(), question: q, type: "text" },
            ],
        });
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
                    <div className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mb-6">
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
                        {isEdit ? "Edit Project" : "New Project"}
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
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] hover:border-black/[0.20] rounded-sm text-xs transition-colors"
                            >
                                <FolderOpen className="w-3.5 h-3.5" /> View Audition
                                Material ({materialsCount})
                            </button>
                            <button
                                onClick={() => setConfirmDeleteOpen(true)}
                                className={`inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] text-black/60 hover:text-red-600 hover:border-red-600/40 rounded-sm text-xs ${isAdminRole ? "" : "hidden"}`}
                                data-testid="delete-project-btn"
                            >
                                <Trash2 className="w-3 h-3" /> Delete
                            </button>
                        </>
                    )}
                    <button
                        onClick={save}
                        disabled={saving}
                        data-testid="save-project-btn"
                        className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-sm text-xs font-medium hover:bg-black/90 transition-colors"
                    >
                        {saving && <Loader2 className="w-3 h-3 animate-spin" />}
                        {isEdit ? "Save changes" : "Create project"}
                    </button>
                </div>
            </div>

            {/* Project details */}
            <section className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mb-6">
                <p className="eyebrow mb-6">Project Details</p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
                    <TextField
                        label="Project / Brand Name"
                        value={project.brand_name}
                        onChange={(v) => updateProject({ brand_name: v })}
                        data-testid="project-brand-input"
                    />
                    <TextField
                        label="Project / Brand Link"
                        value={project.brand_link}
                        onChange={(v) => updateProject({ brand_link: v })}
                        placeholder="https://..."
                    />
                    <TextField
                        label="Character"
                        value={project.character}
                        onChange={(v) => updateProject({ character: v })}
                        placeholder="e.g. Young Mother, 28-35"
                    />
                    <TextField
                        label="Shoot Dates"
                        value={project.shoot_dates}
                        onChange={(v) => updateProject({ shoot_dates: v })}
                        placeholder="e.g. 15–18 March 2026"
                    />
                    <TextField
                        label="Budget per Day"
                        value={project.budget_per_day}
                        onChange={(v) => updateProject({ budget_per_day: v })}
                        placeholder="e.g. ₹50,000"
                    />
                    <div>
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Commission %
                        </span>
                        <div className="mt-2">
                            <Select
                                value={project.commission_percent || ""}
                                onValueChange={(v) => updateProject({ commission_percent: v })}
                            >
                                <SelectTrigger
                                    data-testid="commission-select-trigger"
                                    className="bg-transparent border-0 border-b border-black/[0.10] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select commission" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-black/[0.08] text-black shadow-xl">
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
                        </div>
                    </div>
                    <TextField
                        label="Medium / Usage"
                        value={project.medium_usage}
                        onChange={(v) => updateProject({ medium_usage: v })}
                        placeholder="e.g. TVC · Digital · Print — 1yr"
                    />
                    <TextField
                        label="Director"
                        value={project.director}
                        onChange={(v) => updateProject({ director: v })}
                    />
                    <TextField
                        label="Production House"
                        value={project.production_house}
                        onChange={(v) => updateProject({ production_house: v })}
                    />
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Additional Details
                    </span>
                    <textarea
                        value={project.additional_details || ""}
                        onChange={(e) => updateProject({ additional_details: e.target.value })}
                        rows={3}
                        className="mt-2 w-full bg-transparent border border-black/[0.08] focus:border-black/40 outline-none p-4 text-sm text-black/85 rounded-xl"
                    />
                </div>
            </section>

            {/* Audition Material uploads */}
            {isEdit && (
                <section
                    className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8"
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
                    </div>

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
                                    <button
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
                                </div>
                            ))}
                        </div>
                        <div className="flex gap-2">
                            <input
                                type="url"
                                value={videoInput}
                                onChange={(e) => setVideoInput(e.target.value)}
                                placeholder="https://youtube.com/..."
                                className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                            />
                            <button
                                onClick={addVideoLink}
                                className="text-xs px-3 py-2 border border-black/[0.10] hover:border-black/[0.20] rounded-sm inline-flex items-center gap-1 text-black/70 hover:text-black transition-colors"
                            >
                                <Plus className="w-3 h-3" /> Add link
                            </button>
                        </div>
                        <p className="text-[10px] text-black/35 mt-3 tg-mono">
                            Save project after adding video links
                        </p>
                    </div>
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
                    className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mt-6"
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

                    <div className="border-t border-black/[0.08] pt-6">
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
                    className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mt-6"
                    data-testid="form-config-section"
                >
                    <p className="eyebrow mb-6">Submission Form Configuration</p>
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
                            onChange={(e) => updateProject({ require_reapproval_on_edit: e.target.checked })}
                            data-testid="require-reapproval-toggle"
                            className="w-5 h-5 accent-black"
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
                            onClick={() =>
                                updateProject({
                                    competitive_brand_enabled: !project.competitive_brand_enabled,
                                })
                            }
                            data-testid="toggle-competitive-brand"
                            className={`w-10 h-5 rounded-full relative transition-colors shrink-0 ${project.competitive_brand_enabled ? "bg-black" : "bg-black/15"}`}
                        >
                            <span
                                className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${project.competitive_brand_enabled ? "translate-x-5 bg-white" : "bg-black"}`}
                            />
                        </button>
                    </label>

                    <div className="border-t border-black/[0.08] pt-6">
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
                                    <button
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
                                </div>
                            ))}
                        </div>
                        <div className="flex gap-2">
                            <input
                                value={cqInput}
                                onChange={(e) => setCqInput(e.target.value)}
                                placeholder="e.g. Can you ride a bike?"
                                data-testid="cq-input"
                                className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                            />
                            <button
                                onClick={addCustomQuestion}
                                data-testid="cq-add-btn"
                                className="text-xs px-3 py-2 border border-black/[0.10] hover:border-black/[0.20] rounded-sm inline-flex items-center gap-1 text-black/70 hover:text-black transition-colors"
                            >
                                <Plus className="w-3 h-3" /> Add
                            </button>
                        </div>
                        <p className="text-[10px] text-black/35 mt-3 tg-mono">
                            Save project to apply changes
                        </p>
                    </div>
                </section>
            )}

            {/* Share submission link */}
            {isEdit && (
                <section
                    className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mt-6"
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
                        <div className="flex gap-2 flex-wrap">
                            <a
                                href={submissionUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-black/[0.08] hover:border-black/[0.20] rounded-sm text-black/70 hover:text-black transition-colors"
                            >
                                <ExternalLink className="w-3.5 h-3.5" /> Preview
                            </a>
                            <button
                                onClick={copySubmitLink}
                                data-testid="copy-submit-link-btn"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-black/[0.08] hover:border-black/[0.20] rounded-sm text-black/70 hover:text-black transition-colors"
                            >
                                <Copy className="w-3.5 h-3.5" /> Copy
                            </button>
                            <button
                                onClick={shareWhatsApp}
                                data-testid="whatsapp-submit-link-btn"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 bg-[#25D366] text-black hover:opacity-90 rounded-sm transition-opacity"
                            >
                                <MessageCircle className="w-3.5 h-3.5" />{" "}
                                WhatsApp
                            </button>
                        </div>
                    </div>
                    <div className="border border-black/[0.08] bg-[#fafaf8] px-4 py-3 tg-mono text-xs text-black/70 break-all">
                        {submissionUrl}
                    </div>
                </section>
            )}

            {/* Submissions Review */}
            {isEdit && (
                <section
                    className="border border-black/[0.08] bg-white rounded-xl mt-6"
                    data-testid="submissions-review-section"
                >
                    <div className="px-6 py-4 border-b border-black/[0.08] flex items-center justify-between gap-3 flex-wrap">
                        <div>
                            <p className="eyebrow">Submissions</p>
                            <p className="text-xs text-black/45 mt-1">
                                {submissions.length} total · {approvedCount} approved · {rejectedCount} rejected
                            </p>
                        </div>
                        <button
                            onClick={() => setShowForwardModal(true)}
                            disabled={
                                submissions.filter(
                                    (s) =>
                                        s.decision === "approved" &&
                                        (s.status === "submitted" ||
                                            s.status === "updated"),
                                ).length === 0
                            }
                            data-testid="create-client-link-btn"
                            className="inline-flex items-center gap-2 text-xs px-4 py-2.5 bg-black text-white rounded-sm hover:bg-black/90 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                        >
                            <Sparkles className="w-3.5 h-3.5" /> Create Client
                            Link from Approved
                        </button>
                    </div>
                    {submissions.length === 0 ? (
                        <div className="p-8 text-center text-black/40 text-sm">
                            No submissions yet. Share the link above with
                            talents.
                        </div>
                    ) : (
                        <>
                            <div
                                className="px-6 py-3 border-b border-black/[0.08] flex items-center gap-2 flex-wrap"
                                data-testid="submission-filters"
                            >
                                <button
                                    type="button"
                                    onClick={() => setSubmissionFilter("all")}
                                    data-testid="filter-chip-all"
                                    className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "all" ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
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
                                    className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "pending" ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
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
                                    className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "approved" ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
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
                                    className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "rejected" ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
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
                                    className={`text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-sm border transition-colors ${submissionFilter === "updated" ? "border-black bg-black text-white" : "border-black/[0.08] text-black/60 hover:border-black/[0.20] hover:text-black"}`}
                                >
                                    Updated
                                    <span className={`ml-2 tg-mono ${submissionFilter === "updated" ? "text-white/60" : "text-black/40"}`}>
                                        {updatedCount}
                                    </span>
                                </button>
                            </div>
                            <div className="divide-y divide-black/[0.06]">
                                {filteredSubmissions.map((s) => (
                                    <SubmissionRow
                                        key={s.id}
                                        submission={s}
                                        onOpen={() => setReviewingSid(s.id)}
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
                </section>
            )}

            {/* Casting Pipeline */}
            {isEdit && (
                <ProjectPipeline projectId={id} />
            )}

            {/* Material viewer modal */}
            {showMaterialModal && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterialModal(false)}
                    onRemove={confirmRemoveMaterial}
                />
            )}

            {/* Submission review modal */}
            {reviewingSid && (
                <SubmissionReviewModal
                    submission={(() => {
                        const s = submissions.find((x) => x.id === reviewingSid);
                        return s
                            ? {
                                  ...s,
                                  project_custom_questions:
                                      project.custom_questions || [],
                              }
                            : null;
                    })()}
                    projectId={id}
                    onClose={() => setReviewingSid(null)}
                    onDecision={(d) => setDecision(reviewingSid, d)}
                    onChanged={() => loadSubmissions(id)}
                />
            )}

            {/* Forward to client link modal */}
            {showForwardModal && (
                <ForwardToLinkModal
                    project={project}
                    submissions={submissions}
                    onClose={() => setShowForwardModal(false)}
                    onDone={(link) => {
                        setShowForwardModal(false);
                        nav(`/admin/links/${link.id}/results`);
                    }}
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

function SubmissionRow({ submission, onOpen, onDecision, onDelete }) {
    const s = submission;
    const meta = {
        pending: { icon: Clock, label: "Pending", color: "text-black/60" },
        approved: { icon: Check, label: "Approved", color: "text-green-600" },
        rejected: {
            icon: XCircle,
            label: "Rejected",
            color: "text-red-600",
        },
        hold: { icon: PauseCircle, label: "Hold", color: "text-[#c9a961]" },
    }[s.decision || "pending"];
    const isUpdated = s.status === "updated";
    const mediaCounts = {
        intro: (s.media || []).filter((m) => m.category === "intro_video")
            .length,
        takes: (s.media || []).filter(
            (m) =>
                m.category === "take" ||
                m.category === "take_1" ||
                m.category === "take_2" ||
                m.category === "take_3",
        ).length,
        images: (s.media || []).filter(
            (m) => m.category === "image" || m.category === "indian" || m.category === "western",
        ).length,
    };
    return (
        <div
            className="px-6 py-4 flex items-center justify-between gap-4 flex-wrap hover:bg-black/[0.015] transition-colors"
            data-testid={`submission-row-${s.id}`}
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
                            className="inline-flex items-center gap-1 text-[10px] tracking-widest uppercase text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded-sm"
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
                <div className="text-xs text-black/45 tg-mono mt-1 truncate">
                    {s.talent_email}
                    {s.talent_phone ? ` · ${s.talent_phone}` : ""}
                </div>
                <div className="text-[11px] text-black/45 mt-2">
                    {mediaCounts.intro ? "1 intro video · " : "no intro · "}
                    {mediaCounts.takes} takes · {mediaCounts.images} images
                </div>
            </div>
            <div className="flex gap-1 flex-wrap">
                <button
                    onClick={onOpen}
                    className="text-xs px-3 py-2 border border-black/[0.08] hover:border-black/[0.20] rounded-sm text-black/70 hover:text-black transition-colors"
                    data-testid={`review-submission-${s.id}`}
                >
                    Review
                </button>
                <button
                    onClick={() => onDecision("approved")}
                    className="text-xs px-3 py-2 border border-black/[0.08] hover:border-green-600 hover:text-green-600 rounded-sm text-black/70 transition-colors"
                    data-testid={`approve-${s.id}`}
                    title="Approve"
                >
                    <Check className="w-3.5 h-3.5" />
                </button>
                <button
                    onClick={() => onDecision("hold")}
                    className="text-xs px-3 py-2 border border-black/[0.08] hover:border-[#c9a961] hover:text-[#c9a961] rounded-sm text-black/70 transition-colors"
                    data-testid={`hold-${s.id}`}
                    title="Hold"
                >
                    <PauseCircle className="w-3.5 h-3.5" />
                </button>
                <button
                    onClick={() => onDecision("rejected")}
                    className="text-xs px-3 py-2 border border-black/[0.08] hover:border-red-600 hover:text-red-600 rounded-sm text-black/70 transition-colors"
                    data-testid={`reject-${s.id}`}
                    title="Reject"
                >
                    <XCircle className="w-3.5 h-3.5" />
                </button>
                {onDelete && (
                    <button
                        onClick={onDelete}
                        className="text-xs px-3 py-2 border border-black/[0.08] hover:border-black/40 text-black/50 rounded-sm transition-colors"
                        title="Delete"
                    >
                        <Trash2 className="w-3.5 h-3.5" />
                    </button>
                )}
            </div>
        </div>
    );
}

function SubmissionReviewModal({ submission, onClose, onDecision, projectId, onChanged }) {
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
    const [form, setForm] = useState(normalize(submission?.form_data));
    const [fv, setFv] = useState(submission?.field_visibility || {});
    const [saving, setSaving] = useState(false);
    if (!submission) return null;
    const media = submission.media || [];
    const intro = media.find((m) => m.category === "intro_video");
    // Phase 3 v37j — split portfolio media into 3 buckets so admins can
    // see the Indian/Western/Portfolio sections separately during review.
    // Reuses existing media.category — no schema change.
    const portfolioImages = media.filter((m) => m.category === "image");
    const indianImages = media.filter((m) => m.category === "indian");
    const westernImages = media.filter((m) => m.category === "western");

    const save = async () => {
        setSaving(true);
        try {
            await adminApi.put(
                `/projects/${projectId}/submissions/${submission.id}`,
                { form_data: form, field_visibility: fv },
            );
            toast.success("Updated");
            onChanged?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally {
            setSaving(false);
        }
    };

    const openInDrive = async () => {
        try {
            const { data } = await adminApi.get(`/submissions/${submission.id}/drive`);
            window.open(data.url, "_blank", "noopener,noreferrer");
        } catch (e) {
            const msg = e?.response?.data?.detail || "Google Drive not configured";
            toast.error(msg);
        }
    };

    const FIELDS = [
        { key: "first_name", label: "First Name" },
        { key: "last_name", label: "Last Name" },
        { key: "age", label: "Age", type: "number" },
        { key: "height", label: "Height" },
        { key: "location", label: "Location" },
        { key: "competitive_brand", label: "Competitive Brand" },
    ];

    return (
        <div
            className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm overflow-y-auto"
            data-testid="submission-review-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-black/[0.08] hover:border-black/[0.20] rounded-sm flex items-center justify-center bg-white/80 text-black/70 hover:text-black transition-colors"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-6xl mx-auto px-5 md:px-12 py-10 md:py-14 text-black/85">
                <p className="eyebrow mb-3">Submission</p>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-2 text-black/90">
                    {submission.talent_name}
                </h2>
                <p className="text-sm text-black/45 tg-mono mb-6">
                    {submission.talent_email}
                    {submission.talent_phone
                        ? ` · ${submission.talent_phone}`
                        : ""}
                </p>

                <div className="flex flex-wrap gap-2 mb-10">
                    <button
                        type="button"
                        onClick={openInDrive}
                        data-testid="open-in-drive-btn"
                        className="inline-flex items-center gap-2 text-xs tg-mono px-3 py-2 border border-black/[0.08] hover:border-black/40 rounded-sm text-black/70 hover:text-black transition-colors"
                        title="Opens the Google Drive backup folder for this submission"
                    >
                        <Cloud className="w-3.5 h-3.5" />
                        Open in Drive
                    </button>
                </div>

                {/* Editable form data with per-field visibility toggles */}
                <section
                    className="mb-10 border border-black/[0.08] bg-white rounded-xl p-5 md:p-6"
                    data-testid="review-form-data-section"
                >
                    <div className="flex items-center justify-between mb-5">
                        <p className="eyebrow">Talent Details</p>
                        <span className="text-[10px] tg-mono text-black/45">
                            Toggle per-field to control client visibility
                        </span>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4 mb-6">
                        {FIELDS.map((f) => (
                            <div
                                key={f.key}
                                className="flex items-start gap-3"
                            >
                                <div className="flex-1 min-w-0">
                                    <label className="text-[11px] text-black/45 tracking-widest uppercase">
                                        {f.label}
                                    </label>
                                    <input
                                        type={f.type || "text"}
                                        value={form[f.key] ?? ""}
                                        onChange={(e) =>
                                            setForm({
                                                ...form,
                                                [f.key]: e.target.value,
                                            })
                                        }
                                        data-testid={`review-field-${f.key}`}
                                        className="mt-1 w-full bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85"
                                    />
                                </div>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setFv({
                                            ...fv,
                                            [f.key]: !fv[f.key],
                                        })
                                    }
                                    data-testid={`review-fv-${f.key}`}
                                    title={
                                        fv[f.key]
                                            ? "Visible to client"
                                            : "Hidden from client"
                                    }
                                    className={`mt-5 w-10 h-5 rounded-full relative transition-colors shrink-0 ${fv[f.key] ? "bg-black" : "bg-black/15"}`}
                                >
                                    <span
                                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv[f.key] ? "translate-x-5 bg-white" : "bg-black"}`}
                                    />
                                </button>
                            </div>
                        ))}

                        {/* Availability (structured) */}
                        <div className="md:col-span-2 border-t border-black/[0.08] pt-4">
                            <div className="flex items-center justify-between">
                                <label className="text-[11px] text-black/45 tracking-widest uppercase">
                                    Availability
                                </label>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setFv({
                                            ...fv,
                                            availability: fv.availability === false,
                                        })
                                    }
                                    data-testid="review-fv-availability"
                                    title={
                                        fv.availability === false
                                            ? "Hidden from client"
                                            : "Visible to client"
                                    }
                                    className={`w-10 h-5 rounded-full relative transition-colors shrink-0 ${fv.availability !== false ? "bg-black" : "bg-black/15"}`}
                                >
                                    <span
                                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.availability !== false ? "translate-x-5 bg-white" : "bg-black"}`}
                                    />
                                </button>
                            </div>
                            <div className="mt-2 flex items-center gap-3">
                                <select
                                    value={form.availability?.status || ""}
                                    onChange={(e) =>
                                        setForm({
                                            ...form,
                                            availability: {
                                                ...form.availability,
                                                status: e.target.value,
                                            },
                                        })
                                    }
                                    data-testid="review-avail-status"
                                    className="bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85"
                                >
                                    <option value="" className="bg-white text-black/85">
                                        —
                                    </option>
                                    {AVAILABILITY_OPTIONS.map((opt) => (
                                        <option
                                            key={opt.key}
                                            value={opt.key}
                                            className="bg-white text-black/85"
                                        >
                                            {opt.label}
                                        </option>
                                    ))}
                                </select>
                                <input
                                    type="text"
                                    value={form.availability?.note || ""}
                                    onChange={(e) =>
                                        setForm({
                                            ...form,
                                            availability: {
                                                ...form.availability,
                                                note: e.target.value,
                                            },
                                        })
                                    }
                                    placeholder="Note / reason"
                                    data-testid="review-avail-note"
                                    className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                                />
                            </div>
                        </div>

                        {/* Budget (structured) */}
                        <div className="md:col-span-2">
                            <div className="flex items-center justify-between">
                                <label className="text-[11px] text-black/45 tracking-widest uppercase">
                                    Budget
                                </label>
                                <button
                                    type="button"
                                    onClick={() =>
                                        setFv({
                                            ...fv,
                                            budget: !fv.budget,
                                        })
                                    }
                                    data-testid="review-fv-budget"
                                    title={
                                        fv.budget
                                            ? "Visible to client"
                                            : "Hidden from client"
                                    }
                                    className={`w-10 h-5 rounded-full relative transition-colors shrink-0 ${fv.budget ? "bg-black" : "bg-black/15"}`}
                                >
                                    <span
                                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${fv.budget ? "translate-x-5 bg-white" : "bg-black"}`}
                                    />
                                </button>
                            </div>
                            <div className="mt-2 flex items-center gap-3">
                                <select
                                    value={form.budget?.status || ""}
                                    onChange={(e) =>
                                        setForm({
                                            ...form,
                                            budget: {
                                                ...form.budget,
                                                status: e.target.value,
                                            },
                                        })
                                    }
                                    data-testid="review-budget-status"
                                    className="bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85"
                                >
                                    <option value="" className="bg-white text-black/85">
                                        —
                                    </option>
                                    {BUDGET_OPTIONS.map((opt) => (
                                        <option
                                            key={opt.key}
                                            value={opt.key}
                                            className="bg-white text-black/85"
                                        >
                                            {opt.label}
                                        </option>
                                    ))}
                                </select>
                                <input
                                    type="text"
                                    value={form.budget?.value || ""}
                                    onChange={(e) =>
                                        setForm({
                                            ...form,
                                            budget: {
                                                ...form.budget,
                                                value: e.target.value,
                                            },
                                        })
                                    }
                                    placeholder="Expected budget (if not accepting)"
                                    data-testid="review-budget-value"
                                    className="flex-1 bg-transparent border-b border-black/[0.10] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                                />
                            </div>
                        </div>
                    </div>

                    {Array.isArray(submission.project_custom_questions) &&
                        submission.project_custom_questions.length > 0 && (
                            <div className="border-t border-black/[0.08] pt-5">
                                <p className="eyebrow mb-3">
                                    Custom Answers
                                </p>
                                {submission.project_custom_questions.map(
                                    (q) => (
                                        <div
                                            key={q.id}
                                            className="mb-3 text-sm"
                                        >
                                            <div className="text-black/45 text-xs mb-1">
                                                {q.question}
                                            </div>
                                            <div className="text-black/85">
                                                {(form.custom_answers || {})[
                                                    q.id
                                                ] || "—"}
                                            </div>
                                        </div>
                                    ),
                                )}
                            </div>
                        )}

                    <button
                        onClick={save}
                        disabled={saving}
                        data-testid="review-save-btn"
                        className="mt-4 inline-flex items-center gap-2 px-4 py-2.5 bg-black text-white rounded-sm text-xs font-medium hover:bg-black/90 transition-colors"
                    >
                        {saving && (
                            <Loader2 className="w-3 h-3 animate-spin" />
                        )}
                        Save changes
                    </button>
                </section>

                {intro ? (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <video
                            src={intro.url}
                            controls
                            className="w-full max-w-3xl border border-black/[0.08] bg-[#fafaf8] rounded-lg"
                            data-testid="review-intro-video"
                        />
                    </section>
                ) : (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <div className="max-w-3xl border border-dashed border-black/[0.08] bg-[#fafaf8] aspect-video flex items-center justify-center text-black/45 text-xs tg-mono rounded-lg">
                            Not submitted
                        </div>
                    </section>
                )}
                <section className="mb-10" data-testid="review-takes-section">
                    <p className="eyebrow mb-3">Audition Takes</p>
                    {(() => {
                        // Merge new `take` media with legacy `take_1/2/3`;
                        // legacy entries get auto-labelled "Take N".
                        const takes = media
                            .filter(
                                (m) =>
                                    m.category === "take" ||
                                    m.category === "take_1" ||
                                    m.category === "take_2" ||
                                    m.category === "take_3",
                            )
                            .map((m) => {
                                if (m.category === "take") return m;
                                const n = m.category.replace("take_", "");
                                return { ...m, label: m.label || `Take ${n}` };
                            });
                        if (takes.length === 0) {
                            return (
                                <div className="max-w-3xl border border-dashed border-black/[0.08] bg-[#fafaf8] aspect-video flex items-center justify-center text-black/45 text-xs tg-mono rounded-lg">
                                    Not submitted
                                </div>
                            );
                        }
                        return (
                            <div className="grid md:grid-cols-2 gap-4">
                                {takes.map((t, i) => (
                                    <div
                                        key={t.id}
                                        data-testid={`review-take-${i}`}
                                    >
                                        <p className="text-xs text-black/45 mb-2 tg-mono truncate">
                                            {t.label || `Take ${i + 1}`}
                                        </p>
                                        <video
                                            src={t.url}
                                            controls
                                            preload="metadata"
                                            className="w-full border border-black/[0.08] bg-[#fafaf8] rounded-lg"
                                        />
                                    </div>
                                ))}
                            </div>
                        );
                    })()}
                </section>
                {/* Phase 3 v37j — Indian / Western / Portfolio image sections.
                    Each is independent and hidden when empty. Same grid UI
                    across all three. */}
                {indianImages.length > 0 && (
                    <section className="mb-10" data-testid="review-indian-images-section">
                        <p className="eyebrow mb-3">
                            Indian Look Images ({indianImages.length})
                        </p>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {indianImages.map((m) => (
                                <a
                                    key={m.id}
                                    href={m.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="aspect-square bg-[#fafaf8] overflow-hidden border border-black/[0.08]"
                                >
                                    <img
                                        src={m.url}
                                        alt=""
                                        loading="lazy"
                                        className="w-full h-full object-cover"
                                    />
                                </a>
                            ))}
                        </div>
                    </section>
                )}
                {westernImages.length > 0 && (
                    <section className="mb-10" data-testid="review-western-images-section">
                        <p className="eyebrow mb-3">
                            Western Look Images ({westernImages.length})
                        </p>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {westernImages.map((m) => (
                                <a
                                    key={m.id}
                                    href={m.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="aspect-square bg-[#fafaf8] overflow-hidden border border-black/[0.08]"
                                >
                                    <img
                                        src={m.url}
                                        alt=""
                                        loading="lazy"
                                        className="w-full h-full object-cover"
                                    />
                                </a>
                            ))}
                        </div>
                    </section>
                )}
                {portfolioImages.length > 0 && (
                    <section className="mb-10" data-testid="review-portfolio-images-section">
                        <p className="eyebrow mb-3">
                            Portfolio Images ({portfolioImages.length})
                        </p>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {portfolioImages.map((m) => (
                                <a
                                    key={m.id}
                                    href={m.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="aspect-square bg-[#fafaf8] overflow-hidden border border-black/[0.08]"
                                >
                                    <img
                                        src={m.url}
                                        alt=""
                                        loading="lazy"
                                        className="w-full h-full object-cover"
                                    />
                                </a>
                            ))}
                        </div>
                    </section>
                )}

                <div className="sticky bottom-4 flex gap-2 justify-end flex-wrap bg-white/90 backdrop-blur-sm border border-black/[0.08] rounded-xl px-4 py-3 shadow-sm">
                    <button
                        onClick={() => {
                            onDecision("approved");
                            onClose();
                        }}
                        data-testid="review-approve-btn"
                        className="inline-flex items-center gap-2 px-4 py-2.5 bg-green-600 text-white rounded-sm text-xs font-medium hover:bg-green-700 transition-colors"
                    >
                        <Check className="w-3.5 h-3.5" /> Approve — Forward
                    </button>
                    <button
                        onClick={() => {
                            onDecision("hold");
                            onClose();
                        }}
                        data-testid="review-hold-btn"
                        className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#c9a961]/40 text-[#c9a961] hover:bg-[#c9a961]/10 rounded-sm text-xs font-medium transition-colors"
                    >
                        <PauseCircle className="w-3.5 h-3.5" /> Hold
                    </button>
                    <button
                        onClick={() => {
                            onDecision("rejected");
                            onClose();
                        }}
                        data-testid="review-reject-btn"
                        className="inline-flex items-center gap-2 px-4 py-2.5 border border-red-600/60 text-red-600 hover:bg-red-50 rounded-sm text-xs transition-colors"
                    >
                        <XCircle className="w-3.5 h-3.5" /> Reject
                    </button>
                </div>
            </div>
        </div>
    );
}

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
            className="border border-dashed border-black/[0.08] hover:border-black/[0.18] p-5 text-left transition-colors bg-white rounded-xl"
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
