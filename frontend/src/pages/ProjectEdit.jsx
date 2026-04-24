import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL, isAdmin } from "@/lib/api";
import { toast } from "sonner";
import MaterialModal from "@/components/MaterialModal";
import ForwardToLinkModal from "@/components/ForwardToLinkModal";
import BudgetLines from "@/components/BudgetLines";
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
};

function TextField({ label, value, onChange, type = "text", ...rest }) {
    return (
        <label className="block">
            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                {label}
            </span>
            <input
                type={type}
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
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

    const [project, setProject] = useState(empty);
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(null); // category string
    const [videoInput, setVideoInput] = useState("");
    const [showMaterialModal, setShowMaterialModal] = useState(false);
    const [submissions, setSubmissions] = useState([]);
    const [reviewingSid, setReviewingSid] = useState(null);
    const [showForwardModal, setShowForwardModal] = useState(false);
    const scriptRef = useRef();
    const imageRef = useRef();
    const audioRef = useRef();
    const videoFileRef = useRef();

    const loadSubmissions = async (pid) => {
        try {
            const { data } = await adminApi.get(`/projects/${pid}/submissions`);
            setSubmissions(data);
        } catch {}
    };

    useEffect(() => {
        if (!isEdit) return;
        (async () => {
            try {
                const { data } = await adminApi.get(`/projects/${id}`);
                setProject({ ...empty, ...data });
                loadSubmissions(id);
            } catch {
                toast.error("Failed to load project");
            }
        })();
    }, [id, isEdit]);

    const setDecision = async (sid, decision) => {
        await adminApi.post(`/projects/${id}/submissions/${sid}/decision`, {
            decision,
        });
        toast.success(`Marked ${decision}`);
        loadSubmissions(id);
    };

    const deleteSubmission = async (sid) => {
        if (!window.confirm("Delete this submission?")) return;
        await adminApi.delete(`/projects/${id}/submissions/${sid}`);
        loadSubmissions(id);
    };

    const submissionUrl = project?.slug
        ? `${window.location.origin}/submit/${project.slug}`
        : "";

    const copySubmitLink = () => {
        navigator.clipboard.writeText(submissionUrl);
        toast.success("Submission link copied");
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

    const deleteProject = async () => {
        if (!isEdit) return;
        if (!window.confirm("Delete this project?")) return;
        await adminApi.delete(`/projects/${id}`);
        toast.success("Project deleted");
        nav("/admin/projects");
    };

    const uploadMaterial = async (files, category) => {
        if (!isEdit) {
            toast.error("Save the project before uploading materials");
            return;
        }
        // Early size guard for reference videos (server also enforces)
        if (category === "video_file") {
            const MAX = 100 * 1024 * 1024;
            for (const f of files) {
                if (f.size > MAX) {
                    toast.error(
                        `${f.name} is ${(f.size / 1024 / 1024).toFixed(1)} MB — max 100 MB`,
                    );
                    return;
                }
                if (!f.type.startsWith("video/")) {
                    toast.error(`${f.name} is not a video file`);
                    return;
                }
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
                setProject({ ...empty, ...data });
            }
            toast.success(`${files.length} uploaded`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const removeMaterial = async (mid) => {
        if (!window.confirm("Remove this material?")) return;
        await adminApi.delete(`/projects/${id}/material/${mid}`);
        const { data } = await adminApi.get(`/projects/${id}`);
        setProject({ ...empty, ...data });
    };

    const addVideoLink = () => {
        const v = videoInput.trim();
        if (!v) return;
        setProject({
            ...project,
            video_links: [...(project.video_links || []), v],
        });
        setVideoInput("");
    };

    const [cqInput, setCqInput] = useState("");
    const addCustomQuestion = () => {
        const q = cqInput.trim();
        if (!q) return;
        setProject({
            ...project,
            custom_questions: [
                ...(project.custom_questions || []),
                { id: crypto.randomUUID(), question: q, type: "text" },
            ],
        });
        setCqInput("");
    };

    const materialsCount = (project.materials || []).length + (project.video_links || []).length;

    return (
        <div
            className="p-6 md:p-12 max-w-6xl mx-auto"
            data-testid="project-edit-page"
        >
            <Link
                to="/admin/projects"
                className="inline-flex items-center gap-2 text-xs text-white/50 hover:text-white mb-6"
            >
                <ArrowLeft className="w-3 h-3" /> Back to projects
            </Link>

            <div className="flex items-end justify-between flex-wrap gap-4 mb-10">
                <div>
                    <p className="eyebrow mb-3">
                        {isEdit ? "Edit Project" : "New Project"}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        {project.brand_name || "Untitled"}
                    </h1>
                </div>
                <div className="flex gap-2 flex-wrap">
                    {isEdit && (
                        <>
                            <button
                                onClick={() => setShowMaterialModal(true)}
                                data-testid="view-audition-material-btn"
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/15 hover:border-white rounded-sm text-xs transition-all"
                            >
                                <FolderOpen className="w-3.5 h-3.5" /> View Audition
                                Material ({materialsCount})
                            </button>
                            <button
                                onClick={deleteProject}
                                className={`inline-flex items-center gap-2 px-4 py-2.5 border border-white/15 text-white/60 hover:text-[var(--tg-danger)] hover:border-[var(--tg-danger)]/40 rounded-sm text-xs ${isAdminRole ? "" : "hidden"}`}
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
                        className="inline-flex items-center gap-2 bg-white text-black px-5 py-2.5 rounded-sm text-xs font-medium hover:opacity-90"
                    >
                        {saving && <Loader2 className="w-3 h-3 animate-spin" />}
                        {isEdit ? "Save changes" : "Create project"}
                    </button>
                </div>
            </div>

            {/* Project details */}
            <section className="border border-white/10 p-6 md:p-8 mb-6">
                <p className="eyebrow mb-6">Project Details</p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
                    <TextField
                        label="Project / Brand Name"
                        value={project.brand_name}
                        onChange={(v) =>
                            setProject({ ...project, brand_name: v })
                        }
                        data-testid="project-brand-input"
                    />
                    <TextField
                        label="Project / Brand Link"
                        value={project.brand_link}
                        onChange={(v) =>
                            setProject({ ...project, brand_link: v })
                        }
                        placeholder="https://..."
                    />
                    <TextField
                        label="Character"
                        value={project.character}
                        onChange={(v) =>
                            setProject({ ...project, character: v })
                        }
                        placeholder="e.g. Young Mother, 28-35"
                    />
                    <TextField
                        label="Shoot Dates"
                        value={project.shoot_dates}
                        onChange={(v) =>
                            setProject({ ...project, shoot_dates: v })
                        }
                        placeholder="e.g. 15–18 March 2026"
                    />
                    <TextField
                        label="Budget per Day"
                        value={project.budget_per_day}
                        onChange={(v) =>
                            setProject({ ...project, budget_per_day: v })
                        }
                        placeholder="e.g. ₹50,000"
                    />
                    <div>
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Commission %
                        </span>
                        <div className="mt-2">
                            <Select
                                value={project.commission_percent || ""}
                                onValueChange={(v) =>
                                    setProject({
                                        ...project,
                                        commission_percent: v,
                                    })
                                }
                            >
                                <SelectTrigger
                                    data-testid="commission-select-trigger"
                                    className="bg-transparent border-0 border-b border-white/15 rounded-none px-0 focus:border-white focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select commission" />
                                </SelectTrigger>
                                <SelectContent>
                                    {COMMISSION_OPTIONS.map((c) => (
                                        <SelectItem
                                            key={c}
                                            value={c}
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
                        onChange={(v) =>
                            setProject({ ...project, medium_usage: v })
                        }
                        placeholder="e.g. TVC · Digital · Print — 1yr"
                    />
                    <TextField
                        label="Director"
                        value={project.director}
                        onChange={(v) =>
                            setProject({ ...project, director: v })
                        }
                    />
                    <TextField
                        label="Production House"
                        value={project.production_house}
                        onChange={(v) =>
                            setProject({ ...project, production_house: v })
                        }
                    />
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-white/50 tracking-widest uppercase">
                        Additional Details
                    </span>
                    <textarea
                        value={project.additional_details || ""}
                        onChange={(e) =>
                            setProject({
                                ...project,
                                additional_details: e.target.value,
                            })
                        }
                        rows={3}
                        className="mt-2 w-full bg-transparent border border-white/15 focus:border-white outline-none p-3 text-sm rounded-sm"
                    />
                </div>
            </section>

            {/* Audition Material uploads */}
            {isEdit && (
                <section
                    className="border border-white/10 p-6 md:p-8"
                    data-testid="audition-material-section"
                >
                    <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
                        <div>
                            <p className="eyebrow">Audition Material</p>
                            <p className="text-xs text-white/40 mt-1">
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
                            accept="video/mp4,video/quicktime,video/*"
                            multiple
                            onPick={(files) => uploadMaterial(files, "video_file")}
                            inputRef={videoFileRef}
                            uploading={uploading === "video_file"}
                            testid="upload-video-file"
                            hint="Max 100 MB · mp4/mov"
                        />
                    </div>

                    {/* Video links list */}
                    <div>
                        <div className="flex items-center gap-2 mb-3">
                            <PlayCircle className="w-3.5 h-3.5 text-white/60" />
                            <p className="eyebrow">Video Links</p>
                        </div>
                        <div className="space-y-2 mb-3">
                            {(project.video_links || []).map((v, i) => (
                                <div
                                    key={v}
                                    className="flex items-center gap-2 border-b border-white/10 pb-2"
                                >
                                    <a
                                        href={v}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="flex-1 text-sm text-white/75 tg-mono truncate hover:text-white"
                                    >
                                        {v}
                                    </a>
                                    <button
                                        onClick={() =>
                                            setProject({
                                                ...project,
                                                video_links:
                                                    project.video_links.filter(
                                                        (_, j) => j !== i,
                                                    ),
                                            })
                                        }
                                        className="text-white/40 hover:text-[var(--tg-danger)]"
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
                                className="flex-1 bg-transparent border-b border-white/15 focus:border-white outline-none py-2 text-sm"
                            />
                            <button
                                onClick={addVideoLink}
                                className="text-xs px-3 py-2 border border-white/20 hover:border-white rounded-sm inline-flex items-center gap-1"
                            >
                                <Plus className="w-3 h-3" /> Add link
                            </button>
                        </div>
                        <p className="text-[10px] text-white/30 mt-3 tg-mono">
                            Save project after adding video links
                        </p>
                    </div>
                </section>
            )}
            {!isEdit && (
                <p className="text-xs text-white/40 italic">
                    Save this project first to upload audition materials.
                </p>
            )}

            {/* Budget Configuration */}
            {isEdit && (
                <section
                    className="border border-white/10 p-6 md:p-8 mt-6"
                    data-testid="budget-config-section"
                >
                    <p className="eyebrow mb-2">Project Budget</p>
                    <p className="text-xs text-white/40 mb-6">
                        Keep the talent-facing and client-facing breakdowns separate.
                        Talents see only the talent budget; clients see only the client
                        budget (gated by the link's Budget visibility toggle).
                    </p>

                    <div className="mb-8">
                        <p className="text-sm text-white/80 mb-1">
                            Talent-Facing Budget
                        </p>
                        <p className="text-xs text-white/40 mb-3">
                            Hint shown on the audition submission form so talents
                            understand the offer before they quote.
                        </p>
                        <BudgetLines
                            lines={project.talent_budget || []}
                            onChange={(lines) =>
                                setProject({ ...project, talent_budget: lines })
                            }
                            testidPrefix="talent-budget"
                        />
                    </div>

                    <div className="border-t border-white/10 pt-6">
                        <p className="text-sm text-white/80 mb-1">
                            Client-Facing Budget
                        </p>
                        <p className="text-xs text-white/40 mb-3">
                            Shown to clients on the shared link view. Individual
                            links can still override this via the Link Generator.
                        </p>
                        <BudgetLines
                            lines={project.client_budget || []}
                            onChange={(lines) =>
                                setProject({ ...project, client_budget: lines })
                            }
                            testidPrefix="client-budget"
                        />
                    </div>

                    <p className="text-[10px] text-white/30 mt-6 tg-mono">
                        Save project to apply changes
                    </p>
                </section>
            )}

            {/* Submission Form Configuration */}
            {isEdit && (
                <section
                    className="border border-white/10 p-6 md:p-8 mt-6"
                    data-testid="form-config-section"
                >
                    <p className="eyebrow mb-6">Submission Form Configuration</p>
                    <label className="flex items-center justify-between cursor-pointer mb-6">
                        <div>
                            <div className="text-sm text-white/80">
                                Ask "Competitive Brand" field
                            </div>
                            <div className="text-xs text-white/40 mt-1">
                                When enabled, talents must declare any brand
                                conflicts
                            </div>
                        </div>
                        <button
                            type="button"
                            onClick={() =>
                                setProject({
                                    ...project,
                                    competitive_brand_enabled:
                                        !project.competitive_brand_enabled,
                                })
                            }
                            data-testid="toggle-competitive-brand"
                            className={`w-10 h-5 rounded-full relative transition-all shrink-0 ${project.competitive_brand_enabled ? "bg-white" : "bg-white/15"}`}
                        >
                            <span
                                className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-all ${project.competitive_brand_enabled ? "translate-x-5 bg-black" : "bg-white"}`}
                            />
                        </button>
                    </label>

                    <div className="border-t border-white/10 pt-6">
                        <p className="text-sm text-white/80 mb-1">
                            Custom Questions
                        </p>
                        <p className="text-xs text-white/40 mb-4">
                            Ask project-specific questions. Shown on the talent
                            submission form.
                        </p>
                        <div className="space-y-2 mb-3">
                            {(project.custom_questions || []).map((q, i) => (
                                <div
                                    key={q.id}
                                    className="flex items-center gap-2 border-b border-white/10 pb-2"
                                    data-testid={`cq-row-${i}`}
                                >
                                    <span className="text-sm text-white/75 flex-1 truncate">
                                        {q.question}
                                    </span>
                                    <button
                                        onClick={() =>
                                            setProject({
                                                ...project,
                                                custom_questions:
                                                    project.custom_questions.filter(
                                                        (_, j) => j !== i,
                                                    ),
                                            })
                                        }
                                        className="text-white/40 hover:text-[var(--tg-danger)]"
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
                                className="flex-1 bg-transparent border-b border-white/15 focus:border-white outline-none py-2 text-sm"
                            />
                            <button
                                onClick={addCustomQuestion}
                                data-testid="cq-add-btn"
                                className="text-xs px-3 py-2 border border-white/20 hover:border-white rounded-sm inline-flex items-center gap-1"
                            >
                                <Plus className="w-3 h-3" /> Add
                            </button>
                        </div>
                        <p className="text-[10px] text-white/30 mt-3 tg-mono">
                            Save project to apply changes
                        </p>
                    </div>
                </section>
            )}

            {/* Share submission link */}
            {isEdit && (
                <section
                    className="border border-white/10 p-6 md:p-8 mt-6"
                    data-testid="submission-link-section"
                >
                    <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
                        <div>
                            <p className="eyebrow">Talent Submission Link</p>
                            <p className="text-xs text-white/40 mt-1">
                                Share this public link with talents. They can
                                submit intro video, takes and images.
                            </p>
                        </div>
                        <div className="flex gap-2 flex-wrap">
                            <a
                                href={submissionUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-white/15 hover:border-white rounded-sm"
                            >
                                <ExternalLink className="w-3.5 h-3.5" /> Preview
                            </a>
                            <button
                                onClick={copySubmitLink}
                                data-testid="copy-submit-link-btn"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-white/15 hover:border-white rounded-sm"
                            >
                                <Copy className="w-3.5 h-3.5" /> Copy
                            </button>
                            <button
                                onClick={shareWhatsApp}
                                data-testid="whatsapp-submit-link-btn"
                                className="inline-flex items-center gap-2 text-xs px-3 py-2 bg-[#25D366] text-black hover:opacity-90 rounded-sm"
                            >
                                <MessageCircle className="w-3.5 h-3.5" />{" "}
                                WhatsApp
                            </button>
                        </div>
                    </div>
                    <div className="border border-white/10 bg-white/[0.02] px-4 py-3 tg-mono text-xs text-white/70 break-all">
                        {submissionUrl}
                    </div>
                </section>
            )}

            {/* Submissions Review */}
            {isEdit && (
                <section
                    className="border border-white/10 mt-6"
                    data-testid="submissions-review-section"
                >
                    <div className="px-6 py-4 border-b border-white/10 flex items-center justify-between gap-3 flex-wrap">
                        <div>
                            <p className="eyebrow">Submissions</p>
                            <p className="text-xs text-white/40 mt-1">
                                {submissions.length} total ·{" "}
                                {
                                    submissions.filter(
                                        (s) => s.decision === "approved",
                                    ).length
                                }{" "}
                                approved ·{" "}
                                {
                                    submissions.filter(
                                        (s) => s.decision === "rejected",
                                    ).length
                                }{" "}
                                rejected
                            </p>
                        </div>
                        <button
                            onClick={() => setShowForwardModal(true)}
                            disabled={
                                submissions.filter(
                                    (s) =>
                                        s.decision === "approved" &&
                                        s.status === "submitted",
                                ).length === 0
                            }
                            data-testid="create-client-link-btn"
                            className="inline-flex items-center gap-2 text-xs px-4 py-2.5 bg-white text-black rounded-sm hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                            <Sparkles className="w-3.5 h-3.5" /> Create Client
                            Link from Approved
                        </button>
                    </div>
                    {submissions.length === 0 ? (
                        <div className="p-8 text-center text-white/40 text-sm">
                            No submissions yet. Share the link above with
                            talents.
                        </div>
                    ) : (
                        <div className="divide-y divide-white/10">
                            {submissions.map((s) => (
                                <SubmissionRow
                                    key={s.id}
                                    submission={s}
                                    onOpen={() => setReviewingSid(s.id)}
                                    onDecision={(d) => setDecision(s.id, d)}
                                    onDelete={isAdminRole ? () => deleteSubmission(s.id) : null}
                                />
                            ))}
                        </div>
                    )}
                </section>
            )}

            {/* Material viewer modal */}
            {showMaterialModal && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterialModal(false)}
                    onRemove={removeMaterial}
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
        </div>
    );
}

function SubmissionRow({ submission, onOpen, onDecision, onDelete }) {
    const s = submission;
    const meta = {
        pending: { icon: Clock, label: "Pending", color: "text-white/60" },
        approved: { icon: Check, label: "Approved", color: "text-[#34C759]" },
        rejected: {
            icon: XCircle,
            label: "Rejected",
            color: "text-[#FF3B30]",
        },
    }[s.decision || "pending"];
    const mediaCounts = {
        intro: (s.media || []).filter((m) => m.category === "intro_video")
            .length,
        takes: (s.media || []).filter((m) => m.category?.startsWith("take_"))
            .length,
        images: (s.media || []).filter((m) => m.category === "image").length,
    };
    return (
        <div
            className="px-6 py-4 flex items-center justify-between gap-4 flex-wrap"
            data-testid={`submission-row-${s.id}`}
        >
            <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-display text-lg">
                        {s.talent_name}
                    </span>
                    <span
                        className={`inline-flex items-center gap-1 text-[10px] tracking-widest uppercase ${meta.color}`}
                    >
                        <meta.icon className="w-3 h-3" /> {meta.label}
                    </span>
                    {s.status === "draft" && (
                        <span className="text-[10px] tracking-widest uppercase text-white/30">
                            · Draft
                        </span>
                    )}
                </div>
                <div className="text-xs text-white/40 tg-mono mt-1 truncate">
                    {s.talent_email}
                    {s.talent_phone ? ` · ${s.talent_phone}` : ""}
                </div>
                <div className="text-[11px] text-white/40 mt-2">
                    {mediaCounts.intro ? "1 intro video · " : "no intro · "}
                    {mediaCounts.takes} takes · {mediaCounts.images} images
                </div>
            </div>
            <div className="flex gap-1 flex-wrap">
                <button
                    onClick={onOpen}
                    className="text-xs px-3 py-2 border border-white/15 hover:border-white rounded-sm"
                    data-testid={`review-submission-${s.id}`}
                >
                    Review
                </button>
                <button
                    onClick={() => onDecision("approved")}
                    className="text-xs px-3 py-2 border border-white/15 hover:border-[#34C759] hover:text-[#34C759] rounded-sm"
                    data-testid={`approve-${s.id}`}
                >
                    <Check className="w-3.5 h-3.5" />
                </button>
                <button
                    onClick={() => onDecision("rejected")}
                    className="text-xs px-3 py-2 border border-white/15 hover:border-[#FF3B30] hover:text-[#FF3B30] rounded-sm"
                    data-testid={`reject-${s.id}`}
                >
                    <XCircle className="w-3.5 h-3.5" />
                </button>
                {onDelete && (
                    <button
                        onClick={onDelete}
                        className="text-xs px-3 py-2 border border-white/15 hover:border-white/40 text-white/50 rounded-sm"
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
    const images = media.filter((m) => m.category === "image");

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
            className="fixed inset-0 z-50 bg-background/80 backdrop-blur-xl overflow-y-auto"
            data-testid="submission-review-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-border hover:border-foreground rounded-sm flex items-center justify-center bg-background/80 text-foreground"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14 text-foreground">
                <p className="eyebrow mb-3">Submission</p>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-2">
                    {submission.talent_name}
                </h2>
                <p className="text-sm text-muted-foreground tg-mono mb-10">
                    {submission.talent_email}
                    {submission.talent_phone
                        ? ` · ${submission.talent_phone}`
                        : ""}
                </p>

                {/* Editable form data with per-field visibility toggles */}
                <section
                    className="mb-10 border border-border p-5 md:p-6"
                    data-testid="review-form-data-section"
                >
                    <div className="flex items-center justify-between mb-5">
                        <p className="eyebrow">Talent Details</p>
                        <span className="text-[10px] tg-mono text-muted-foreground">
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
                                    <label className="text-[11px] text-muted-foreground tracking-widest uppercase">
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
                                        className="mt-1 w-full bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
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
                                    className={`mt-5 w-10 h-5 rounded-full relative transition-all shrink-0 ${fv[f.key] ? "bg-foreground" : "bg-muted"}`}
                                >
                                    <span
                                        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-all ${fv[f.key] ? "translate-x-5 bg-background" : "bg-foreground"}`}
                                    />
                                </button>
                            </div>
                        ))}

                        {/* Availability (structured) */}
                        <div className="md:col-span-2 border-t border-border pt-4">
                            <label className="text-[11px] text-muted-foreground tracking-widest uppercase">
                                Availability
                            </label>
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
                                    className="bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
                                >
                                    <option value="" className="bg-background text-foreground">
                                        —
                                    </option>
                                    <option value="yes" className="bg-background text-foreground">
                                        Yes
                                    </option>
                                    <option value="no" className="bg-background text-foreground">
                                        No
                                    </option>
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
                                    className="flex-1 bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
                                />
                            </div>
                        </div>

                        {/* Budget (structured) */}
                        <div className="md:col-span-2">
                            <label className="text-[11px] text-muted-foreground tracking-widest uppercase">
                                Budget
                            </label>
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
                                    className="bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
                                >
                                    <option value="" className="bg-background text-foreground">
                                        —
                                    </option>
                                    <option value="accept" className="bg-background text-foreground">
                                        Accept
                                    </option>
                                    <option value="custom" className="bg-background text-foreground">
                                        Not accepting
                                    </option>
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
                                    className="flex-1 bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm"
                                />
                            </div>
                        </div>
                    </div>

                    {Array.isArray(submission.project_custom_questions) &&
                        submission.project_custom_questions.length > 0 && (
                            <div className="border-t border-border pt-5">
                                <p className="eyebrow mb-3">
                                    Custom Answers
                                </p>
                                {submission.project_custom_questions.map(
                                    (q) => (
                                        <div
                                            key={q.id}
                                            className="mb-3 text-sm"
                                        >
                                            <div className="text-muted-foreground text-xs mb-1">
                                                {q.question}
                                            </div>
                                            <div className="text-foreground">
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
                        className="mt-4 inline-flex items-center gap-2 px-4 py-2.5 bg-foreground text-background rounded-sm text-xs font-medium hover:opacity-90"
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
                            src={FILE_URL(intro.storage_path)}
                            controls
                            className="w-full max-w-3xl border border-border bg-muted rounded-lg"
                            data-testid="review-intro-video"
                        />
                    </section>
                ) : (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <div className="max-w-3xl border border-dashed border-border bg-muted/40 aspect-video flex items-center justify-center text-muted-foreground text-xs tg-mono rounded-lg">
                            Not submitted
                        </div>
                    </section>
                )}
                <section className="mb-10" data-testid="review-takes-section">
                    <p className="eyebrow mb-3">Audition Takes</p>
                    <div className="grid md:grid-cols-2 gap-4">
                        {["take_1", "take_2", "take_3"].map((key) => {
                            const t = media.find((m) => m.category === key);
                            const label = `Take ${key.split("_")[1]}`;
                            return (
                                <div key={key} data-testid={`review-${key}`}>
                                    <p className="text-xs text-muted-foreground mb-2 tg-mono">
                                        {label}
                                    </p>
                                    {t ? (
                                        <video
                                            src={FILE_URL(t.storage_path)}
                                            controls
                                            className="w-full border border-border bg-muted rounded-lg"
                                        />
                                    ) : (
                                        <div className="w-full border border-dashed border-border bg-muted/40 aspect-video flex items-center justify-center text-muted-foreground text-xs tg-mono rounded-lg">
                                            Not submitted
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </section>
                {images.length > 0 && (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">
                            Images ({images.length})
                        </p>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {images.map((m) => (
                                <a
                                    key={m.id}
                                    href={FILE_URL(m.storage_path)}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="aspect-square bg-muted overflow-hidden border border-border"
                                >
                                    <img
                                        src={FILE_URL(m.storage_path)}
                                        alt=""
                                        loading="lazy"
                                        className="w-full h-full object-cover"
                                    />
                                </a>
                            ))}
                        </div>
                    </section>
                )}

                <div className="sticky bottom-4 flex gap-2 justify-end">
                    <button
                        onClick={() => {
                            onDecision("approved");
                            onClose();
                        }}
                        data-testid="review-approve-btn"
                        className="inline-flex items-center gap-2 px-4 py-2.5 bg-[#34C759] text-black rounded-sm text-xs font-medium"
                    >
                        <Check className="w-3.5 h-3.5" /> Approve — Forward
                    </button>
                    <button
                        onClick={() => {
                            onDecision("rejected");
                            onClose();
                        }}
                        data-testid="review-reject-btn"
                        className="inline-flex items-center gap-2 px-4 py-2.5 border border-[#FF3B30]/60 text-[#FF3B30] hover:bg-[#FF3B30]/10 rounded-sm text-xs"
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
            className="border border-dashed border-white/15 hover:border-white/40 p-5 text-left transition-all"
        >
            <div className="flex items-center justify-between mb-3">
                <Icon className="w-4 h-4 text-white/60" strokeWidth={1.5} />
                {uploading ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                    <Upload className="w-3.5 h-3.5 text-white/40" />
                )}
            </div>
            <div className="text-sm font-display">{title}</div>
            <div className="text-[10px] tg-mono text-white/40 mt-1">
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
