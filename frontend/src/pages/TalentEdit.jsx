import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { toast } from "sonner";
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
    Plus,
    Star,
    Loader2,
    X,
    Play,
} from "lucide-react";

const emptyTalent = {
    name: "",
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

// 3'0" through 6'7"
const HEIGHT_OPTIONS = (() => {
    const out = [];
    for (let ft = 3; ft <= 6; ft++) {
        const maxIn = ft === 6 ? 7 : 11;
        for (let inch = 0; inch <= maxIn; inch++) {
            out.push(`${ft}'${inch}"`);
        }
    }
    return out;
})();

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

const FOLLOWER_TIERS = [
    {
        label: "Early Range",
        items: ["1K+", "10K+", "25K+", "50K+", "75K+", "100K+"],
    },
    {
        label: "Mid Range",
        items: ["150K+", "200K+", "300K+", "400K+", "500K+", "750K+", "1M+"],
    },
    {
        label: "High Range",
        items: ["2M+", "3M+", "4M+", "5M+", "7M+", "10M+"],
    },
    {
        label: "Premium Influencer",
        items: ["15M+", "20M+", "25M+", "30M+", "40M+", "50M+"],
    },
];

function Field({ label, value, onChange, type = "text", ...rest }) {
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

export default function TalentEdit() {
    const { id } = useParams();
    const nav = useNavigate();
    const isEdit = Boolean(id);
    const [talent, setTalent] = useState(emptyTalent);
    const [workInput, setWorkInput] = useState("");
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(null); // category string
    const fileRefs = {
        indian: useRef(),
        western: useRef(),
        portfolio: useRef(),
        video: useRef(),
    };

    useEffect(() => {
        if (!isEdit) return;
        (async () => {
            try {
                const { data } = await adminApi.get(`/talents/${id}`);
                setTalent({ ...emptyTalent, ...data });
            } catch {
                toast.error("Failed to load talent");
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
        if (!window.confirm("Delete this talent? This cannot be undone."))
            return;
        await adminApi.delete(`/talents/${id}`);
        toast.success("Talent deleted");
        nav("/admin/talents");
    };

    const uploadFiles = async (files, category) => {
        if (!isEdit) {
            toast.error("Save talent first before uploading media");
            return;
        }
        setUploading(category);
        try {
            for (const file of files) {
                const fd = new FormData();
                fd.append("file", file);
                fd.append("category", category);
                const { data } = await adminApi.post(
                    `/talents/${id}/media`,
                    fd,
                    {
                        headers: { "Content-Type": "multipart/form-data" },
                    },
                );
                setTalent(data);
            }
            toast.success(`${files.length} upload(s) added`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Upload failed");
        } finally {
            setUploading(null);
        }
    };

    const removeMedia = async (mid) => {
        if (!window.confirm("Remove this media?")) return;
        await adminApi.delete(`/talents/${id}/media/${mid}`);
        const { data } = await adminApi.get(`/talents/${id}`);
        setTalent(data);
    };

    const setCover = async (mid) => {
        await adminApi.post(`/talents/${id}/cover/${mid}`);
        const { data } = await adminApi.get(`/talents/${id}`);
        setTalent(data);
        toast.success("Cover updated");
    };

    const addWorkLink = () => {
        if (!workInput.trim()) return;
        setTalent({
            ...talent,
            work_links: [...(talent.work_links || []), workInput.trim()],
        });
        setWorkInput("");
    };

    const mediaBy = (cat) =>
        (talent.media || []).filter((m) => m.category === cat);

    return (
        <div
            className="p-6 md:p-12 max-w-6xl mx-auto"
            data-testid="talent-edit-page"
        >
            <Link
                to="/admin/talents"
                className="inline-flex items-center gap-2 text-xs text-white/50 hover:text-white mb-6"
            >
                <ArrowLeft className="w-3 h-3" /> Back to roster
            </Link>

            <div className="flex items-end justify-between flex-wrap gap-4 mb-10">
                <div>
                    <p className="eyebrow mb-3">
                        {isEdit ? "Edit Talent" : "New Talent"}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        {talent.name || "Untitled"}
                    </h1>
                </div>
                <div className="flex gap-2">
                    {isEdit && (
                        <button
                            onClick={deleteTalent}
                            data-testid="delete-talent-btn"
                            className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/15 text-white/60 hover:text-[var(--tg-danger)] hover:border-[var(--tg-danger)]/40 rounded-sm text-xs transition-all"
                        >
                            <Trash2 className="w-3 h-3" strokeWidth={1.5} />{" "}
                            Delete
                        </button>
                    )}
                    <button
                        onClick={save}
                        disabled={saving}
                        data-testid="save-talent-btn"
                        className="inline-flex items-center gap-2 bg-white text-black px-5 py-2.5 rounded-sm text-xs font-medium hover:opacity-90 transition-all"
                    >
                        {saving && <Loader2 className="w-3 h-3 animate-spin" />}{" "}
                        {isEdit ? "Save changes" : "Create talent"}
                    </button>
                </div>
            </div>

            {/* Basic info */}
            <section className="border border-white/10 p-6 md:p-8 mb-6">
                <p className="eyebrow mb-6">Profile</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-x-8 gap-y-6">
                    <Field
                        label="Full Name"
                        value={talent.name}
                        onChange={(v) => setTalent({ ...talent, name: v })}
                    />

                    {/* DOB + auto Age (admin-only) */}
                    <label className="block" data-testid="field-dob">
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Date of Birth
                        </span>
                        <input
                            type="date"
                            value={talent.dob || ""}
                            onChange={(e) =>
                                setTalent({ ...talent, dob: e.target.value })
                            }
                            max={new Date().toISOString().split("T")[0]}
                            data-testid="dob-input"
                            className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm [color-scheme:dark]"
                        />
                    </label>
                    <div data-testid="field-age-auto">
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Age (auto)
                        </span>
                        <div className="mt-2 border-b border-white/15 py-2.5 text-sm flex items-center justify-between">
                            <span
                                data-testid="computed-age"
                                className="font-display text-base"
                            >
                                {computedAge ?? "—"}
                            </span>
                            <span className="text-[10px] text-white/40 tg-mono">
                                {talent.dob
                                    ? "auto-computed"
                                    : "set DOB to auto-calc"}
                            </span>
                        </div>
                    </div>

                    {/* Gender pills */}
                    <div data-testid="field-gender">
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Gender
                        </span>
                        <div className="mt-2 flex gap-2">
                            {["Male", "Female"].map((g) => {
                                const active = talent.gender === g;
                                return (
                                    <button
                                        key={g}
                                        type="button"
                                        onClick={() =>
                                            setTalent({
                                                ...talent,
                                                gender: active ? "" : g,
                                            })
                                        }
                                        data-testid={`gender-${g.toLowerCase()}-btn`}
                                        className={`flex-1 px-4 py-2.5 rounded-full text-sm border transition-all ${
                                            active
                                                ? "bg-white text-black border-white"
                                                : "border-white/20 hover:border-white/50 text-white/80"
                                        }`}
                                    >
                                        {g}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {/* Height dropdown */}
                    <div data-testid="field-height">
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Height
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.height || ""}
                                onValueChange={(v) =>
                                    setTalent({ ...talent, height: v })
                                }
                            >
                                <SelectTrigger
                                    data-testid="height-select-trigger"
                                    className="bg-transparent border-0 border-b border-white/15 rounded-none px-0 focus:border-white focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select height" />
                                </SelectTrigger>
                                <SelectContent className="max-h-72">
                                    {HEIGHT_OPTIONS.map((h) => (
                                        <SelectItem
                                            key={h}
                                            value={h}
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
                        onChange={(v) => setTalent({ ...talent, location: v })}
                    />
                    <Field
                        label="Ethnicity"
                        value={talent.ethnicity}
                        onChange={(v) => setTalent({ ...talent, ethnicity: v })}
                    />
                    <Field
                        label="Instagram Handle"
                        value={talent.instagram_handle}
                        onChange={(v) =>
                            setTalent({ ...talent, instagram_handle: v })
                        }
                        placeholder="@username"
                    />
                    <div data-testid="field-followers">
                        <span className="text-[11px] text-white/50 tracking-widest uppercase">
                            Instagram Followers
                        </span>
                        <div className="mt-2">
                            <Select
                                value={talent.instagram_followers || ""}
                                onValueChange={(v) =>
                                    setTalent({
                                        ...talent,
                                        instagram_followers: v,
                                    })
                                }
                            >
                                <SelectTrigger
                                    data-testid="followers-select-trigger"
                                    className="bg-transparent border-0 border-b border-white/15 rounded-none px-0 focus:border-white focus:ring-0 shadow-none h-auto py-2.5"
                                >
                                    <SelectValue placeholder="Select range" />
                                </SelectTrigger>
                                <SelectContent className="max-h-80">
                                    {FOLLOWER_TIERS.map((tier, i) => (
                                        <React.Fragment key={tier.label}>
                                            {i > 0 && <SelectSeparator />}
                                            <SelectGroup>
                                                <SelectLabel className="text-[10px] tracking-[0.2em] uppercase text-muted-foreground font-medium px-2 py-2">
                                                    {tier.label}
                                                </SelectLabel>
                                                {tier.items.map((it) => (
                                                    <SelectItem
                                                        key={it}
                                                        value={it}
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
                    <span className="text-[11px] text-white/50 tracking-widest uppercase">
                        Bio
                    </span>
                    <textarea
                        value={talent.bio || ""}
                        onChange={(e) =>
                            setTalent({ ...talent, bio: e.target.value })
                        }
                        rows={3}
                        className="mt-2 w-full bg-transparent border border-white/15 focus:border-white outline-none p-3 text-sm rounded-sm"
                    />
                </div>
                <div className="mt-6">
                    <span className="text-[11px] text-white/50 tracking-widest uppercase">
                        Work Links (7–8)
                    </span>
                    <div className="mt-2 space-y-2">
                        {(talent.work_links || []).map((w, i) => (
                            <div key={w} className="flex items-center gap-2">
                                <span className="text-sm text-white/70 flex-1 truncate tg-mono">
                                    {w}
                                </span>
                                <button
                                    onClick={() =>
                                        setTalent({
                                            ...talent,
                                            work_links:
                                                talent.work_links.filter(
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
                        <div className="flex gap-2">
                            <input
                                type="url"
                                value={workInput}
                                onChange={(e) => setWorkInput(e.target.value)}
                                placeholder="https://..."
                                className="flex-1 bg-transparent border-b border-white/15 focus:border-white outline-none py-2 text-sm"
                            />
                            <button
                                onClick={addWorkLink}
                                className="text-xs px-3 py-2 border border-white/20 hover:border-white rounded-sm"
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
                            accept: "image/*",
                        },
                        {
                            key: "western",
                            label: "Western Look Images",
                            accept: "image/*",
                        },
                        {
                            key: "portfolio",
                            label: "Additional Portfolio",
                            accept: "image/*",
                        },
                        {
                            key: "video",
                            label: "Introduction Video",
                            accept: "video/*",
                        },
                    ].map((cat) => (
                        <section
                            key={cat.key}
                            className="border border-white/10 p-6 md:p-8 mb-6"
                            data-testid={`media-section-${cat.key}`}
                        >
                            <div className="flex items-center justify-between mb-6">
                                <p className="eyebrow">{cat.label}</p>
                                <button
                                    onClick={() =>
                                        fileRefs[cat.key].current?.click()
                                    }
                                    disabled={uploading === cat.key}
                                    data-testid={`upload-${cat.key}-btn`}
                                    className="inline-flex items-center gap-2 text-xs px-3 py-2 border border-white/20 hover:border-white rounded-sm transition-all"
                                >
                                    {uploading === cat.key ? (
                                        <Loader2 className="w-3 h-3 animate-spin" />
                                    ) : (
                                        <Upload className="w-3 h-3" />
                                    )}
                                    Upload
                                </button>
                                <input
                                    ref={fileRefs[cat.key]}
                                    type="file"
                                    accept={cat.accept}
                                    multiple={cat.key !== "video"}
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
                                <p className="text-white/30 text-sm">
                                    No files
                                </p>
                            ) : (
                                <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-3">
                                    {mediaBy(cat.key).map((m) => (
                                        <div
                                            key={m.id}
                                            className="relative group aspect-square bg-[#0a0a0a] border border-white/10"
                                        >
                                            {m.content_type?.startsWith(
                                                "video",
                                            ) ? (
                                                <div className="w-full h-full flex items-center justify-center">
                                                    <Play className="w-8 h-8 text-white/70" />
                                                </div>
                                            ) : (
                                                <img
                                                    src={FILE_URL(
                                                        m.storage_path,
                                                    )}
                                                    alt=""
                                                    className="w-full h-full object-cover"
                                                />
                                            )}
                                            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-all bg-black/60 flex items-center justify-center gap-2">
                                                {cat.key !== "video" && (
                                                    <button
                                                        onClick={() =>
                                                            setCover(m.id)
                                                        }
                                                        title="Set cover"
                                                        className="p-1.5 bg-white/10 hover:bg-white/20 rounded-sm"
                                                    >
                                                        <Star
                                                            className={`w-3.5 h-3.5 ${talent.cover_media_id === m.id ? "fill-white text-white" : "text-white"}`}
                                                        />
                                                    </button>
                                                )}
                                                <button
                                                    onClick={() =>
                                                        removeMedia(m.id)
                                                    }
                                                    title="Delete"
                                                    className="p-1.5 bg-white/10 hover:bg-[var(--tg-danger)]/80 rounded-sm"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </div>
                                            {talent.cover_media_id ===
                                                m.id && (
                                                <div className="absolute top-1 left-1 bg-white text-black text-[9px] px-1.5 py-0.5 tracking-widest uppercase">
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
                <p className="text-xs text-white/40 italic">
                    Save this talent first to start uploading media.
                </p>
            )}
        </div>
    );
}
