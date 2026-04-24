import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { toast } from "sonner";
import MaterialModal from "@/components/MaterialModal";
import ForwardToLinkModal from "@/components/ForwardToLinkModal";
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
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/15 text-white/60 hover:text-[var(--tg-danger)] hover:border-[var(--tg-danger)]/40 rounded-sm text-xs"
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

                    <div className="grid md:grid-cols-3 gap-4 mb-8">
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
                                    key={i}
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
                                    onDelete={() => deleteSubmission(s.id)}
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
                    submission={submissions.find((s) => s.id === reviewingSid)}
                    onClose={() => setReviewingSid(null)}
                    onDecision={(d) => setDecision(reviewingSid, d)}
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
                <button
                    onClick={onDelete}
                    className="text-xs px-3 py-2 border border-white/15 hover:border-white/40 text-white/50 rounded-sm"
                >
                    <Trash2 className="w-3.5 h-3.5" />
                </button>
            </div>
        </div>
    );
}

function SubmissionReviewModal({ submission, onClose, onDecision }) {
    if (!submission) return null;
    const media = submission.media || [];
    const intro = media.find((m) => m.category === "intro_video");
    const takes = ["take_1", "take_2", "take_3"]
        .map((k) => media.find((m) => m.category === k))
        .filter(Boolean);
    const images = media.filter((m) => m.category === "image");
    return (
        <div
            className="fixed inset-0 z-50 bg-black/95 backdrop-blur-xl overflow-y-auto"
            data-testid="submission-review-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14">
                <p className="eyebrow mb-3">Submission</p>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-2">
                    {submission.talent_name}
                </h2>
                <p className="text-sm text-white/50 tg-mono mb-10">
                    {submission.talent_email}
                    {submission.talent_phone
                        ? ` · ${submission.talent_phone}`
                        : ""}
                </p>

                {intro && (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <video
                            src={FILE_URL(intro.storage_path)}
                            controls
                            className="w-full max-w-3xl border border-white/10 bg-black"
                        />
                    </section>
                )}
                {takes.length > 0 && (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Audition Takes</p>
                        <div className="grid md:grid-cols-2 gap-4">
                            {takes.map((t, i) => (
                                <div key={t.id}>
                                    <p className="text-xs text-white/50 mb-2 tg-mono">
                                        Take {t.category.split("_")[1]}
                                    </p>
                                    <video
                                        src={FILE_URL(t.storage_path)}
                                        controls
                                        className="w-full border border-white/10 bg-black"
                                    />
                                </div>
                            ))}
                        </div>
                    </section>
                )}
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
                                    className="aspect-square bg-[#0a0a0a] overflow-hidden border border-white/10"
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
                {multiple ? "Multiple allowed" : "Single file"}
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
