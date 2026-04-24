import React, { useEffect, useState, useMemo } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { toast } from "sonner";
import {
    ArrowLeft,
    Search,
    Check,
    Image as ImageIcon,
    Loader2,
} from "lucide-react";

const VIS_ITEMS = [
    { key: "portfolio", label: "Portfolio Images" },
    { key: "intro_video", label: "Introduction Video" },
    { key: "instagram", label: "Instagram (clickable)" },
    { key: "instagram_followers", label: "Instagram Followers" },
    { key: "age", label: "Age" },
    { key: "height", label: "Height" },
    { key: "location", label: "Location" },
    { key: "ethnicity", label: "Ethnicity" },
    { key: "work_links", label: "Work Links" },
    { key: "budget_form", label: "Budget Form" },
    { key: "download", label: "Download Option" },
];

const DEFAULT_VIS = {
    portfolio: true,
    intro_video: true,
    instagram: true,
    instagram_followers: true,
    age: true,
    height: true,
    location: true,
    ethnicity: true,
    work_links: true,
    budget_form: false,
    download: false,
};

export default function LinkGenerator() {
    const nav = useNavigate();
    const { id } = useParams();
    const isEdit = Boolean(id);

    const [talents, setTalents] = useState([]);
    const [selected, setSelected] = useState(new Set());
    const [q, setQ] = useState("");
    const [title, setTitle] = useState("Talentgram x ");
    const [brand, setBrand] = useState("");
    const [visibility, setVisibility] = useState(DEFAULT_VIS);
    const [isPublic, setIsPublic] = useState(true);
    const [notes, setNotes] = useState("");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        (async () => {
            const { data } = await adminApi.get("/talents");
            setTalents(data);
        })();
        if (isEdit) {
            (async () => {
                const { data } = await adminApi.get(`/links/${id}`);
                setTitle(data.title);
                setBrand(data.brand_name || "");
                setSelected(new Set(data.talent_ids));
                setVisibility({ ...DEFAULT_VIS, ...(data.visibility || {}) });
                setIsPublic(data.is_public);
                setNotes(data.notes || "");
            })();
        }
    }, [id, isEdit]);

    const filtered = useMemo(() => {
        if (!q) return talents;
        return talents.filter((t) =>
            t.name.toLowerCase().includes(q.toLowerCase()),
        );
    }, [q, talents]);

    const toggle = (tid) => {
        const next = new Set(selected);
        if (next.has(tid)) next.delete(tid);
        else next.add(tid);
        setSelected(next);
    };

    const submit = async () => {
        if (!title.trim() || title.trim() === "Talentgram x") {
            toast.error("Please provide a title like 'Talentgram x Nike'");
            return;
        }
        if (selected.size === 0) {
            toast.error("Select at least one talent");
            return;
        }
        setSaving(true);
        try {
            const payload = {
                title: title.trim(),
                brand_name: brand || null,
                talent_ids: Array.from(selected),
                visibility,
                is_public: isPublic,
                notes,
            };
            const { data } = isEdit
                ? await adminApi.put(`/links/${id}`, payload)
                : await adminApi.post("/links", payload);
            toast.success(isEdit ? "Link updated" : "Link generated");
            nav(`/admin/links/${data.id}/results`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        } finally {
            setSaving(false);
        }
    };

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="link-generator-page"
        >
            <Link
                to="/admin/links"
                className="inline-flex items-center gap-2 text-xs text-white/50 hover:text-white mb-6"
            >
                <ArrowLeft className="w-3 h-3" /> Back
            </Link>

            <div className="mb-10">
                <p className="eyebrow mb-3">
                    {isEdit ? "Edit Link" : "New Link"}
                </p>
                <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                    Generate a client link
                </h1>
            </div>

            <div className="grid lg:grid-cols-3 gap-6">
                {/* Left: Config */}
                <div className="lg:col-span-1 space-y-6">
                    <section className="border border-white/10 p-6">
                        <p className="eyebrow mb-5">Identity</p>
                        <label className="block mb-4">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Title
                            </span>
                            <input
                                value={title}
                                onChange={(e) => setTitle(e.target.value)}
                                placeholder="Talentgram x Nike"
                                data-testid="link-title-input"
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm font-display"
                            />
                        </label>
                        <label className="block mb-4">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Brand / Project (optional)
                            </span>
                            <input
                                value={brand}
                                onChange={(e) => setBrand(e.target.value)}
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>
                        <label className="flex items-center gap-3 mt-4">
                            <input
                                type="checkbox"
                                checked={isPublic}
                                onChange={(e) => setIsPublic(e.target.checked)}
                                data-testid="link-public-toggle"
                                className="accent-white"
                            />
                            <span className="text-sm text-white/70">
                                Public link (anyone with link + identity)
                            </span>
                        </label>
                    </section>

                    <section className="border border-white/10 p-6">
                        <p className="eyebrow mb-5">Visibility Controls</p>
                        <div className="space-y-3">
                            {VIS_ITEMS.map((v) => (
                                <label
                                    key={v.key}
                                    className="flex items-center justify-between cursor-pointer group"
                                >
                                    <span className="text-sm text-white/80">
                                        {v.label}
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setVisibility({
                                                ...visibility,
                                                [v.key]: !visibility[v.key],
                                            })
                                        }
                                        data-testid={`vis-toggle-${v.key}`}
                                        className={`w-10 h-5 rounded-full relative transition-all ${visibility[v.key] ? "bg-white" : "bg-white/15"}`}
                                    >
                                        <span
                                            className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-all ${visibility[v.key] ? "translate-x-5 bg-black" : "bg-white"}`}
                                        />
                                    </button>
                                </label>
                            ))}
                        </div>
                    </section>

                    <section className="border border-white/10 p-6">
                        <p className="eyebrow mb-3">Internal Notes</p>
                        <textarea
                            value={notes}
                            onChange={(e) => setNotes(e.target.value)}
                            rows={3}
                            placeholder="Only visible to your team"
                            className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none"
                        />
                    </section>

                    <button
                        onClick={submit}
                        disabled={saving}
                        data-testid="generate-link-btn"
                        className="w-full bg-white text-black py-3.5 rounded-sm text-sm font-medium hover:opacity-90 transition-all inline-flex items-center justify-center gap-2"
                    >
                        {saving && (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        )}
                        {isEdit ? "Save & View" : "Generate Link"}
                    </button>
                </div>

                {/* Right: Talent Selector */}
                <div className="lg:col-span-2 border border-white/10 p-6 min-h-[480px]">
                    <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
                        <div>
                            <p className="eyebrow">Select Talents</p>
                            <p className="text-xs text-white/40 mt-1">
                                {selected.size} selected
                            </p>
                        </div>
                        <div className="relative">
                            <Search className="absolute left-0 top-2.5 w-4 h-4 text-white/40" />
                            <input
                                value={q}
                                onChange={(e) => setQ(e.target.value)}
                                placeholder="Search..."
                                data-testid="talent-select-search"
                                className="bg-transparent border-b border-white/15 focus:border-white outline-none py-2 pl-7 text-sm w-56"
                            />
                        </div>
                    </div>

                    {filtered.length === 0 ? (
                        <div className="text-white/40 text-sm py-10 text-center">
                            No talents found.{" "}
                            <Link
                                to="/admin/talents/new"
                                className="underline hover:text-white"
                            >
                                Add talents first
                            </Link>
                            .
                        </div>
                    ) : (
                        <div
                            className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
                            data-testid="talent-select-grid"
                        >
                            {filtered.map((t) => {
                                const cover =
                                    (t.media || []).find(
                                        (m) => m.id === t.cover_media_id,
                                    ) ||
                                    (t.media || []).find((m) =>
                                        m.content_type?.startsWith("image/"),
                                    );
                                const active = selected.has(t.id);
                                return (
                                    <button
                                        key={t.id}
                                        onClick={() => toggle(t.id)}
                                        data-testid={`select-talent-${t.id}`}
                                        className={`relative group text-left border transition-all ${active ? "border-white" : "border-white/10 hover:border-white/30"}`}
                                    >
                                        <div className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden">
                                            {cover ? (
                                                <img
                                                    src={FILE_URL(
                                                        cover.storage_path,
                                                    )}
                                                    alt={t.name}
                                                    className="w-full h-full object-cover"
                                                />
                                            ) : (
                                                <div className="w-full h-full flex items-center justify-center text-white/20">
                                                    <ImageIcon className="w-6 h-6" />
                                                </div>
                                            )}
                                        </div>
                                        <div className="p-2.5">
                                            <div className="text-sm font-display truncate">
                                                {t.name}
                                            </div>
                                            <div className="text-[10px] text-white/40 tg-mono truncate">
                                                {t.location || "—"}
                                            </div>
                                        </div>
                                        {active && (
                                            <div className="absolute top-2 right-2 w-6 h-6 bg-white text-black flex items-center justify-center rounded-sm">
                                                <Check className="w-3.5 h-3.5" />
                                            </div>
                                        )}
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
