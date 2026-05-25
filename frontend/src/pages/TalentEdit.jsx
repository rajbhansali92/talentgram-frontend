import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { toast } from "sonner";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import {
    Select,
    SelectContent,
    SelectGroup,
    SelectItem,
    SelectLabel,
    SelectSeparator,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    ArrowLeft,
    Upload,
    Trash2,
    Star,
    Loader2,
    X,
    Play,
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
} from "@/lib/talentSchema";

const emptyTalent = {
    name: "",
    email: "",
    phone: "",
    age: "",
    dob: "",
    height: "",
    location: "",
    ethnicity: "",
    gender: "",
    instagram_handle: "",
    instagram_followers: "",
    bio: "",
    work_links: [],
};

// ISSUE 2: File validation constants
const MAX_FILE_SIZE = 25 * 1024 * 1024; // 25MB
const ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/jpg"];
const ALLOWED_VIDEO_TYPES = ["video/mp4", "video/quicktime", "video/webm"];

function calcAge(dob) {
    if (!dob) return null;
    const [y, m, d] = dob.split("-").map((n) => parseInt(n, 10));
    if (!y || !m || !d) return null;
    const today = new Date();
    let age = today.getFullYear() - y;
    const mm = today.getMonth() + 1;
    const dd = today.getDate();
    if (mm < m || (mm === m && dd < d)) age -= 1;
    return age >= 0 && age <= 120 ? age : null;
}

function Field({ label, value, onChange, type = "text", ...rest }) {
    return (
        <label className="block">
            <span className="text-[11px] text-black/45 tracking-widest uppercase">
                {label}
            </span>
            <input
                type={type}
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
                className="mt-2 w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2.5 text-sm text-black/85 placeholder:text-black/30"
                {...rest}
            />
        </label>
    );
}

export default function TalentEdit() {
    const { id } = useParams();
    const nav = useNavigate();
    const isEdit = Boolean(id);
    const isAdminRole = isAdmin();
    const [loading, setLoading] = useState(isEdit); // ISSUE 1: Fixed loading state
    const [talent, setTalent] = useState(emptyTalent);
    const [workInput, setWorkInput] = useState("");
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(null);
    const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
    const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false);
    const [mediaToRemove, setMediaToRemove] = useState(null);
    
    // File refs
    const fileRefs = useRef({
        indian: null,
        western: null,
        portfolio: null,
        video: null,
    });
    
    const setFileRef = (key) => (el) => {
        fileRefs.current[key] = el;
    };

    const updateTalent = useCallback((patch) => {
        setTalent(prev => ({ ...prev, ...patch }));
    }, []);

    useEffect(() => {
        if (!isEdit) return;
        (async () => {
            try {
                const { data } = await adminApi.get(`/talents/${id}`);
                setTalent({ ...emptyTalent, ...data });
            } catch {
                toast.error("Failed to load talent");
            } finally {
                setLoading(false); // ISSUE 1: Set loading false after fetch
            }
        })();
    }, [id, isEdit]);

    const save = async () => {
        setSaving(true);
        try {
            const payload = {
                ...talent,
                dob: talent.dob || null,
                age: talent.dob
                    ? calcAge(talent.dob)
                    : talent.age
                      ? parseInt(talent.age, 10)
                      : null,
                work_links: (talent.work_links || []).filter(Boolean),
            };
            if (isEdit) {
                await adminApi.put(`/talents/${id}`, payload);
                toast.success("Saved");
            } else {
                const { data } = await adminApi.post(`/talents`, payload);
                toast.success("Talent created");
                nav(`/admin/talents/${data.id}`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally {
            setSaving(false);
        }
    };

    const computedAge = useMemo(
        () => calcAge(talent.dob) ?? (talent.age || null),
        [talent.dob, talent.age],
    );

    const deleteTalent = async () => {
        if (!isEdit) return;
        try {
            const res = await adminApi.delete(`/talents/${id}`);
            if (process.env.NODE_ENV === "development") {
                console.info("[delete talent]", id, res?.data);
            }
            toast.success("Talent deleted");
            setConfirmDeleteOpen(false);
            nav("/admin/talents");
        } catch (err) {
            if (process.env.NODE_ENV === "development") {
                console.error("[delete talent] failed", err?.response?.data || err);
            }
            toast.error(
                err?.response?.data?.detail ||
                    err?.message ||
                    "Delete failed — check console for details",
            );
            throw err;
        }
    };

    // ISSUE 2: File validation function
    const validateFile = (file, category) => {
        // Check file size
        if (file.size > MAX_FILE_SIZE) {
            toast.error(`${file.name} is too large. Max size is 25MB`);
            return false;
        }
        
        // Check file type based on category
        const isVideo = category === "video";
        const allowedTypes = isVideo ? ALLOWED_VIDEO_TYPES : ALLOWED_IMAGE_TYPES;
        
        if (!allowedTypes.includes(file.type)) {
            const allowedExtensions = isVideo ? "MP4, MOV, WEBM" : "JPEG, PNG, WEBP";
            toast.error(`${file.name} has invalid format. Allowed: ${allowedExtensions}`);
            return false;
        }
        
        return true;
    };

    const uploadFiles = async (files, category) => {
        if (!isEdit) {
            toast.error("Save talent first before uploading media");
            return;
        }
        
        // ISSUE 2: Validate all files first
        const validFiles = [];
        for (const file of files) {
            if (validateFile(file, category)) {
                validFiles.push(file);
            }
        }
        
        if (validFiles.length === 0) {
            toast.error("No valid files to upload");
            return;
        }
        
        setUploading(category);
        try {
            const uploadPromises = validFiles.map(async (file) => {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                const { data } = await adminApi.post(`/talents/${id}/media`, fd, {
                    headers: { "Content-Type": "multipart/form-data" },
                });
                return data;
            });
            
            const results = await Promise.all(uploadPromises);
            
            const newMedia = results.flatMap(result => result.media || []);
            updateTalent({ 
                media: [...(talent.media || []), ...newMedia]
            });
            
            toast.success(`${validFiles.length} upload(s) added`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const removeMedia = async () => {
        if (!mediaToRemove) return;
        
        try {
            await adminApi.delete(`/talents/${id}/media/${mediaToRemove}`);
            const wasCover = talent.cover_media_id === mediaToRemove;
            updateTalent({
                media: (talent.media || []).filter(m => m.id !== mediaToRemove),
                // If the deleted item was the cover, clear both cover fields
                // to stay in sync with the backend $unset operation.
                cover_media_id: wasCover ? null : talent.cover_media_id,
                cover_url: wasCover ? null : talent.cover_url,
            });
            toast.success("Media removed");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Remove failed");
        } finally {
            setConfirmRemoveOpen(false);
            setMediaToRemove(null);
        }
    };

    const setCover = async (mid) => {
        try {
            const { data } = await adminApi.post(`/talents/${id}/cover/${mid}`);
            // Sync both cover_media_id and the denormalized cover_url returned
            // by the updated set_cover endpoint. This keeps local state aligned
            // with the roster list without requiring a full re-fetch.
            updateTalent({
                cover_media_id: mid,
                cover_url: data?.cover_url ?? null,
            });
            toast.success("Cover updated");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to set cover");
        }
    };

    const addWorkLink = () => {
        if (!workInput.trim()) return;
        updateTalent({
            work_links: [...(talent.work_links || []), workInput.trim()]
        });
        setWorkInput("");
    };

    const mediaBy = (cat) =>
        (talent.media || []).filter((m) => m.category === cat);

    // Unsaved changes warning
    useEffect(() => {
        if (!isEdit || loading) return;
        
        const handleBeforeUnload = (e) => {
            const hasChanges = JSON.stringify(talent) !== JSON.stringify(emptyTalent);
            if (hasChanges) {
                e.preventDefault();
                e.returnValue = "You have unsaved changes. Are you sure you want to leave?";
                return e.returnValue;
            }
        };
        
        window.addEventListener("beforeunload", handleBeforeUnload);
        return () => window.removeEventListener("beforeunload", handleBeforeUnload);
    }, [talent, isEdit, loading]);

    // ISSUE 1: Fixed loading skeleton
    if (loading) {
        return (
            <div className="p-6 md:p-10 max-w-7xl mx-auto">
                <div className="animate-pulse">
                    <div className="h-8 bg-black/5 rounded w-32 mb-6"></div>
                    <div className="h-12 bg-black/5 rounded w-64 mb-8"></div>
                    <div className="space-y-4">
                        <div className="h-64 bg-black/5 rounded-xl"></div>
                        <div className="h-64 bg-black/5 rounded-xl"></div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto text-black/85"
            data-testid="talent-edit-page"
        >
            <Link
                to="/admin/talents"
                className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6 transition-colors"
            >
                <ArrowLeft className="w-3 h-3" /> Back to roster
            </Link>

            <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                <div>
                    <p className="eyebrow mb-3">
                        {isEdit ? "Edit Talent" : "New Talent"}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        {talent.name || "Untitled"}
                    </h1>
                </div>
                <div className="flex gap-2">
                    {isEdit && isAdminRole && (
                        <button
                            onClick={() => setConfirmDeleteOpen(true)}
                            data-testid="delete-talent-btn"
                            className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] text-black/60 hover:text-red-600 hover:border-red-600/40 rounded-md text-xs transition-colors"
                        >
                            <Trash2 className="w-3 h-3" strokeWidth={1.5} /> Delete
                        </button>
                    )}
                    <button
                        onClick={save}
                        disabled={saving}
                        data-testid="save-talent-btn"
                        className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors"
                    >
                        {saving && <Loader2 className="w-3 h-3 animate-spin" />}{" "}
                        {isEdit ? "Save changes" : "Create talent"}
                    </button>
                </div>
            </div>

            {/* Basic info */}
            <section className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mb-6">
                <p className="eyebrow mb-6">Profile</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-x-8 gap-y-6">
                    <Field
                        label="Full Name"
                        value={talent.name}
                        onChange={(v) => updateTalent({ name: v })}
                    />
                    <Field
                        label="Email"
                        type="email"
                        value={talent.email}
                        onChange={(v) => updateTalent({ email: v })}
                        data-testid="talent-email-input"
                        placeholder="optional"
                    />
                    <Field
                        label="Phone"
                        type="tel"
                        value={talent.phone}
                        onChange={(v) => updateTalent({ phone: v })}
                        data-testid="talent-phone-input"
                        placeholder="optional"
                    />

                    <label className="block" data-testid="field-dob">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Date of Birth
                        </span>
                        <input
                            type="date"
                            value={talent.dob || ""}
                            onChange={(e) => updateTalent({ dob: e.target.value })}
                            max={new Date().toISOString().split("T")[0]}
                            data-testid="dob-input"
                            className="mt-2 w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2.5 text-sm text-black/85"
                        />
                    </label>
                    <div data-testid="field-age-auto">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Age (auto)
                        </span>
                        <div className="mt-2 border-b border-black/[0.08] py-2.5 text-sm flex items-center justify-between">
                            <span
                                data-testid="computed-age"
                                className="font-display text-base text-black/85"
                            >
                                {computedAge ?? "—"}
                            </span>
                            <span className="text-[10px] text-black/40">
                                {talent.dob
                                    ? "auto-computed"
                                    : "set DOB to auto-calc"}
                            </span>
                        </div>
                    </div>

                    <div data-testid="field-gender">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Gender
                        </span>
                        <div className="mt-2 grid grid-cols-2 gap-2">
                            {GENDER_OPTIONS.map((g) => {
                                const active = talent.gender === g.key;
                                return (
                                    <button
                                        key={g.key}
                                        type="button"
                                        onClick={() =>
                                            updateTalent({
                                                gender: active ? "" : g.key,
                                            })
                                        }
                                        data-testid={`gender-${g.key}-btn`}
                                        className={`px-3 py-2.5 text-sm rounded-full border transition-colors ${
                                            active
                                                ? "bg-black text-white border-black"
                                                : "border-black/[0.15] hover:border-black/[0.30] text-black/70 hover:text-black"
                                        }`}
                                    >
                                        {g.label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    <div data-testid="field-height">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Height
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.height || ""}
                                onValueChange={(v) => updateTalent({ height: v })}
                            >
                                <SelectTrigger
                                    data-testid="height-select-trigger"
                                    className="bg-transparent border-0 border-b border-black/[0.08] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select height" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-black/[0.08] text-black shadow-xl max-h-72">
                                    {HEIGHT_OPTIONS.map((h) => (
                                        <SelectItem
                                            key={h}
                                            value={h}
                                            className="focus:bg-black/5 focus:text-black"
                                            data-testid={`height-option-${h}`}
                                        >
                                            {h}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>

                    <Field
                        label="Location"
                        value={talent.location}
                        onChange={(v) => updateTalent({ location: v })}
                    />
                    <div data-testid="field-ethnicity">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Ethnicity
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.ethnicity || ""}
                                onValueChange={(v) => updateTalent({ ethnicity: v })}
                            >
                                <SelectTrigger
                                    data-testid="ethnicity-select-trigger"
                                    className="bg-transparent border-0 border-b border-black/[0.08] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select ethnicity" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-black/[0.08] text-black shadow-xl max-h-72">
                                    {ETHNICITY_OPTIONS.map((e) => (
                                        <SelectItem
                                            key={e.key}
                                            value={e.key}
                                            className="focus:bg-black/5 focus:text-black"
                                            data-testid={`ethnicity-option-${e.key}`}
                                        >
                                            {e.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>
                    <Field
                        label="Instagram Handle"
                        value={talent.instagram_handle}
                        onChange={(v) => updateTalent({ instagram_handle: v })}
                        placeholder="@username"
                    />
                    <div data-testid="field-followers">
                        <span className="text-[11px] text-black/45 tracking-widest uppercase">
                            Instagram Followers
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.instagram_followers || ""}
                                onValueChange={(v) => updateTalent({ instagram_followers: v })}
                            >
                                <SelectTrigger
                                    data-testid="followers-select-trigger"
                                    className="bg-transparent border-0 border-b border-black/[0.08] rounded-none px-0 focus:border-black/40 focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select range" />
                                </SelectTrigger>
                                <SelectContent className="bg-white border border-black/[0.08] text-black shadow-xl max-h-80">
                                    {FOLLOWER_TIERS.map((tier, i) => (
                                        <React.Fragment key={tier.label}>
                                            {i > 0 && <SelectSeparator className="bg-black/[0.08]" />}
                                            <SelectGroup>
                                                <SelectLabel className="text-[10px] tracking-[0.2em] uppercase text-black/45 font-medium px-2 py-2">
                                                    {tier.label}
                                                </SelectLabel>
                                                {tier.items.map((it) => (
                                                    <SelectItem
                                                        key={it}
                                                        value={it}
                                                        className="focus:bg-black/5 focus:text-black"
                                                        data-testid={`followers-option-${it}`}
                                                    >
                                                        {it}
                                                    </SelectItem>
                                                ))}
                                            </SelectGroup>
                                        </React.Fragment>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Bio
                    </span>
                    <textarea
                        value={talent.bio || ""}
                        onChange={(e) => updateTalent({ bio: e.target.value })}
                        rows={3}
                        className="mt-2 w-full bg-transparent border border-black/[0.08] focus:border-black/40 outline-none p-4 text-sm text-black/85 rounded-xl resize-none"
                    />
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-black/45 tracking-widest uppercase">
                        Work Links (7–8)
                    </span>
                    <div className="mt-2 space-y-2">
                        {(talent.work_links || []).map((w, i) => (
                            <div key={w} className="flex items-center gap-2">
                                <span className="text-sm text-black/75 flex-1 truncate">
                                    {w}
                                </span>
                                <button
                                    onClick={() =>
                                        updateTalent({
                                            work_links: talent.work_links.filter((_, j) => j !== i),
                                        })
                                    }
                                    className="text-black/40 hover:text-red-600 transition-colors"
                                >
                                    <X className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        ))}
                        <div className="flex gap-2">
                            <input
                                type="url"
                                value={workInput}
                                onChange={(e) => setWorkInput(e.target.value)}
                                placeholder="https://..."
                                className="flex-1 bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-2 text-sm text-black/85 placeholder:text-black/30"
                            />
                            <button
                                onClick={addWorkLink}
                                className="text-xs px-3 py-2 border border-black/[0.08] hover:border-black/[0.16] rounded-md text-black/70 hover:text-black transition-colors"
                            >
                                + Add link
                            </button>
                        </div>
                    </div>
                </div>
            </section>

            {/* Media */}
            {isEdit && (
                <>
                    {[
                        {
                            key: "indian",
                            label: "Indian Look Images",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "western",
                            label: "Western Look Images",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "portfolio",
                            label: "Additional Portfolio",
                            accept: "image/jpeg,image/png,image/webp,image/jpg",
                            isVideo: false,
                        },
                        {
                            key: "video",
                            label: "Introduction Video",
                            accept: "video/mp4,video/quicktime,video/webm",
                            isVideo: true,
                        },
                    ].map((cat) => (
                        <section
                            key={cat.key}
                            className="border border-black/[0.08] bg-white rounded-xl p-6 md:p-8 mb-6"
                            data-testid={`media-section-${cat.key}`}
                        >
                            <div className="flex items-center justify-between mb-6">
                                <p className="eyebrow">{cat.label}</p>
                                <button
                                    onClick={() => fileRefs.current[cat.key]?.click()}
                                    disabled={uploading === cat.key}
                                    data-testid={`upload-${cat.key}-btn`}
                                    className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-black/[0.08] hover:border-black/[0.16] rounded-md text-black/70 hover:text-black transition-colors"
                                >
                                    {uploading === cat.key ? (
                                        <Loader2 className="w-3 h-3 animate-spin" />
                                    ) : (
                                        <Upload className="w-3 h-3" />
                                    )}
                                    Upload
                                </button>
                                <input
                                    ref={setFileRef(cat.key)}
                                    type="file"
                                    accept={cat.accept}
                                    multiple={!cat.isVideo}
                                    className="hidden"
                                    onChange={(e) => {
                                        if (e.target.files?.length)
                                            uploadFiles(
                                                Array.from(e.target.files),
                                                cat.key,
                                            );
                                        e.target.value = "";
                                    }}
                                />
                            </div>
                            {mediaBy(cat.key).length === 0 ? (
                                <p className="text-black/40 text-sm">
                                    No files
                                </p>
                            ) : (
                                <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-3">
                                    {mediaBy(cat.key).map((m) => (
                                        <div
                                            key={m.id}
                                            className="relative group aspect-square bg-[#fafaf8] border border-black/[0.08] rounded-lg overflow-hidden"
                                        >
                                            {m.content_type?.startsWith(
                                                "video",
                                            ) ? (
                                                <div className="w-full h-full flex items-center justify-center">
                                                    <Play className="w-8 h-8 text-black/60" />
                                                </div>
                                            ) : (
                                                <img
                                                    src={m.url}
                                                    alt=""
                                                    loading="lazy" // ISSUE 11: Lazy loading
                                                    className="w-full h-full object-cover"
                                                />
                                            )}
                                            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-150 bg-black/40 flex items-center justify-center gap-2">
                                                {!cat.isVideo && (
                                                    <button
                                                        onClick={() => setCover(m.id)}
                                                        title="Set cover"
                                                        className="p-1.5 bg-white/20 hover:bg-white/30 rounded-md transition-colors"
                                                    >
                                                        <Star
                                                            className={`w-3.5 h-3.5 ${talent.cover_media_id === m.id ? "fill-black text-black" : "text-white"}`}
                                                        />
                                                    </button>
                                                )}
                                                <button
                                                    onClick={() => {
                                                        setMediaToRemove(m.id);
                                                        setConfirmRemoveOpen(true);
                                                    }}
                                                    title="Delete"
                                                    className="p-1.5 bg-white/20 hover:bg-red-600/80 rounded-md transition-colors"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5 text-white" />
                                                </button>
                                            </div>
                                            {talent.cover_media_id ===
                                                m.id && (
                                                <div className="absolute top-1 left-1 bg-black text-white text-[9px] px-1.5 py-0.5 tracking-widest uppercase rounded">
                                                    Cover
                                                </div>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </section>
                    ))}
                </>
            )}
            {!isEdit && (
                <p className="text-xs text-black/45 italic">
                    Save this talent first to start uploading media.
                </p>
            )}
            
            <ConfirmDeleteDialog
                open={confirmRemoveOpen}
                title={`Remove this media?`}
                description="This will permanently delete this media file from the talent's portfolio. This action cannot be undone."
                confirmLabel="Remove media"
                typeToConfirm="REMOVE"
                onCancel={() => {
                    setConfirmRemoveOpen(false);
                    setMediaToRemove(null);
                }}
                onConfirm={removeMedia}
            />
            
            <ConfirmDeleteDialog
                open={confirmDeleteOpen}
                title={`Delete "${talent.name || "this talent"}"?`}
                description="This permanently removes the talent record and all their portfolio media. Any submissions linked to this talent remain (they live on the project). This cannot be undone."
                confirmLabel="Delete talent"
                typeToConfirm="DELETE"
                onCancel={() => setConfirmDeleteOpen(false)}
                onConfirm={deleteTalent}
            />
        </div>
    );
}
