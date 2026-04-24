import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { toast } from "sonner";
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
    const scriptRef = useRef();
    const imageRef = useRef();
    const audioRef = useRef();

    useEffect(() => {
        if (!isEdit) return;
        (async () => {
            try {
                const { data } = await adminApi.get(`/projects/${id}`);
                setProject({ ...empty, ...data });
            } catch {
                toast.error("Failed to load project");
            }
        })();
    }, [id, isEdit]);

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

            {/* Material viewer modal */}
            {showMaterialModal && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterialModal(false)}
                    onRemove={removeMaterial}
                />
            )}
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

function MaterialModal({ project, onClose, onRemove }) {
    const materials = project.materials || [];
    const videos = project.video_links || [];
    const byCat = (c) => materials.filter((m) => m.category === c);

    return (
        <div
            className="fixed inset-0 z-50 bg-black/90 backdrop-blur-xl overflow-y-auto"
            data-testid="audition-material-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
            >
                <X className="w-4 h-4" />
            </button>
            <div className="max-w-5xl mx-auto px-6 md:px-12 py-12">
                <p className="eyebrow mb-3">Audition Material</p>
                <h2 className="font-display text-4xl md:text-5xl tracking-tight mb-10">
                    {project.brand_name}
                </h2>

                <MaterialGroup
                    title="Script (PDF)"
                    items={byCat("script")}
                    onRemove={onRemove}
                    render={(m) => (
                        <a
                            href={FILE_URL(m.storage_path)}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-3 flex-1"
                        >
                            <FileText className="w-5 h-5 text-white/60" />
                            <div className="min-w-0">
                                <div className="text-sm truncate">
                                    {m.original_filename || "script.pdf"}
                                </div>
                                <div className="text-[10px] tg-mono text-white/40">
                                    Open PDF →
                                </div>
                            </div>
                        </a>
                    )}
                />

                <MaterialGroup
                    title="Images"
                    items={byCat("image")}
                    onRemove={onRemove}
                    grid
                    render={(m) => (
                        <a
                            href={FILE_URL(m.storage_path)}
                            target="_blank"
                            rel="noreferrer"
                            className="block aspect-square bg-[#0a0a0a] overflow-hidden"
                        >
                            <img
                                src={FILE_URL(m.storage_path)}
                                alt=""
                                className="w-full h-full object-cover"
                            />
                        </a>
                    )}
                />

                <MaterialGroup
                    title="Audio Notes"
                    items={byCat("audio")}
                    onRemove={onRemove}
                    render={(m) => (
                        <div className="flex items-center gap-3 flex-1">
                            <Music className="w-5 h-5 text-white/60 shrink-0" />
                            <audio
                                src={FILE_URL(m.storage_path)}
                                controls
                                className="w-full max-w-md"
                            />
                        </div>
                    )}
                />

                {videos.length > 0 && (
                    <div className="mb-10">
                        <p className="eyebrow mb-4">Video Links</p>
                        <div className="space-y-2">
                            {videos.map((v, i) => (
                                <a
                                    key={i}
                                    href={v}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="flex items-center gap-3 p-3 border border-white/10 hover:border-white/30 transition-all"
                                >
                                    <PlayCircle className="w-5 h-5 text-white/60" />
                                    <span className="text-sm tg-mono truncate">
                                        {v}
                                    </span>
                                </a>
                            ))}
                        </div>
                    </div>
                )}

                {materials.length === 0 && videos.length === 0 && (
                    <p className="text-white/40 text-sm">
                        No audition materials yet.
                    </p>
                )}
            </div>
        </div>
    );
}

function MaterialGroup({ title, items, onRemove, render, grid }) {
    if (!items || items.length === 0) return null;
    return (
        <div className="mb-10">
            <p className="eyebrow mb-4">{title}</p>
            <div
                className={
                    grid
                        ? "grid grid-cols-2 md:grid-cols-4 gap-3"
                        : "space-y-2"
                }
            >
                {items.map((m) => (
                    <div
                        key={m.id}
                        className={
                            grid
                                ? "relative group"
                                : "flex items-center gap-3 p-3 border border-white/10"
                        }
                    >
                        {render(m)}
                        <button
                            onClick={() => onRemove(m.id)}
                            className={
                                grid
                                    ? "absolute top-2 right-2 opacity-0 group-hover:opacity-100 p-1.5 bg-black/70 hover:bg-[var(--tg-danger)] rounded-sm transition-all"
                                    : "text-white/40 hover:text-[var(--tg-danger)]"
                            }
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                        </button>
                    </div>
                ))}
            </div>
        </div>
    );
}
