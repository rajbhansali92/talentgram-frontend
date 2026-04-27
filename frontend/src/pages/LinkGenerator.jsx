import React, { useEffect, useState, useMemo } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { toast } from "sonner";
import BudgetLines from "@/components/BudgetLines";
import {
    ArrowLeft,
    Search,
    Check,
    Image as ImageIcon,
    Loader2,
    Users,
    Film,
    UserCircle,
    Folder,
    Settings2,
} from "lucide-react";

const VIS_ITEMS = [
    { key: "portfolio", label: "Portfolio Images" },
    // Phase 3 v37j — granular look-specific toggles. Independent of `portfolio`
    // so admins can ship "Indian only" or "Western only" curations.
    { key: "indian_images", label: "Indian Look Images" },
    { key: "western_images", label: "Western Look Images" },
    { key: "intro_video", label: "Introduction Video" },
    { key: "takes", label: "Audition Takes" },
    { key: "instagram", label: "Instagram (clickable)" },
    { key: "instagram_followers", label: "Instagram Followers" },
    { key: "age", label: "Age" },
    { key: "height", label: "Height" },
    { key: "location", label: "Location" },
    { key: "ethnicity", label: "Ethnicity" },
    { key: "availability", label: "Availability" },
    { key: "budget", label: "Budget" },
    { key: "work_links", label: "Work Links" },
    // Phase 1 cleanup (v37): "Budget Form" toggle removed — was redundant
    // with the single "Budget" toggle above. Existing links keep their
    // visibility.budget_form value in the DB but it's no longer surfaced.
    { key: "download", label: "Download Option" },
];

const DEFAULT_VIS = {
    portfolio: true,
    indian_images: true,
    western_images: true,
    intro_video: true,
    takes: true,
    instagram: true,
    instagram_followers: true,
    age: true,
    height: true,
    location: true,
    ethnicity: true,
    availability: true,
    budget: false,
    work_links: true,
    budget_form: false,
    download: false,
};

// M1 = Individual Talent Share · M2 = Project Showcase · M3 = Submission/Audition Link
const MODES = [
    {
        key: "individual",
        title: "Individual Talent Share",
        tag: "M1",
        desc: "Share one or more talent profiles with a brand. Per-talent field overrides supported.",
        icon: UserCircle,
    },
    {
        key: "showcase",
        title: "Project Showcase",
        tag: "M2",
        desc: "Auto-pull every approved submission for a project. New approvals appear automatically.",
        icon: Folder,
    },
    {
        key: "submission",
        title: "Submission / Audition Link",
        tag: "M3",
        desc: "Hand-pick approved submissions across any project. Static curation.",
        icon: Film,
    },
];

function inferMode(data) {
    if (data?.auto_pull && data?.auto_project_id) return "showcase";
    const t = (data?.talent_ids || []).length;
    const s = (data?.submission_ids || []).length;
    if (t && !s) return "individual";
    if (s && !t) return "submission";
    if (t && s) return "individual"; // mixed → land on individual; user can switch via tabs are now mode-locked
    return "individual";
}

export default function LinkGenerator() {
    const nav = useNavigate();
    const { id } = useParams();
    const isEdit = Boolean(id);

    const [mode, setMode] = useState(null); // null until chosen
    const [talents, setTalents] = useState([]);
    const [submissions, setSubmissions] = useState([]);
    const [projects, setProjects] = useState([]);
    const [selectedTalents, setSelectedTalents] = useState(new Set());
    const [selectedSubs, setSelectedSubs] = useState(new Set());
    const [autoProjectId, setAutoProjectId] = useState("");
    const [perTalentVis, setPerTalentVis] = useState({}); // {talentId: {key: bool}}
    const [editingTalentId, setEditingTalentId] = useState(null);
    const [q, setQ] = useState("");
    const [title, setTitle] = useState("Talentgram x ");
    const [brand, setBrand] = useState("");
    const [visibility, setVisibility] = useState(DEFAULT_VIS);
    const [isPublic, setIsPublic] = useState(true);
    const [notes, setNotes] = useState("");
    const [clientBudgetOverride, setClientBudgetOverride] = useState([]);
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        (async () => {
            const [tRes, sRes, pRes] = await Promise.all([
                adminApi.get("/talents"),
                adminApi.get("/submissions/approved"),
                adminApi.get("/projects"),
            ]);
            setTalents(tRes.data || []);
            setSubmissions(sRes.data || []);
            setProjects(pRes.data || []);
        })();
        if (isEdit) {
            (async () => {
                const { data } = await adminApi.get(`/links/${id}`);
                setMode(inferMode(data));
                setTitle(data.title);
                setBrand(data.brand_name || "");
                setSelectedTalents(new Set(data.talent_ids || []));
                setSelectedSubs(new Set(data.submission_ids || []));
                setAutoProjectId(data.auto_project_id || "");
                setPerTalentVis(data.talent_field_visibility || {});
                setVisibility({ ...DEFAULT_VIS, ...(data.visibility || {}) });
                setIsPublic(data.is_public);
                setNotes(data.notes || "");
                setClientBudgetOverride(data.client_budget_override || []);
            })();
        }
    }, [id, isEdit]);

    const filteredTalents = useMemo(() => {
        if (!q) return talents;
        return talents.filter((t) =>
            t.name?.toLowerCase().includes(q.toLowerCase()),
        );
    }, [q, talents]);

    const filteredSubs = useMemo(() => {
        if (!q) return submissions;
        const lc = q.toLowerCase();
        return submissions.filter(
            (s) =>
                s.talent_name?.toLowerCase().includes(lc) ||
                s.project_brand?.toLowerCase().includes(lc),
        );
    }, [q, submissions]);

    const toggleTalent = (tid) => {
        const next = new Set(selectedTalents);
        if (next.has(tid)) next.delete(tid);
        else next.add(tid);
        setSelectedTalents(next);
    };
    const toggleSub = (sid) => {
        const next = new Set(selectedSubs);
        if (next.has(sid)) next.delete(sid);
        else next.add(sid);
        setSelectedSubs(next);
    };

    const totalSelected =
        mode === "individual"
            ? selectedTalents.size
            : mode === "submission"
              ? selectedSubs.size
              : autoProjectId
                ? 1
                : 0;

    const submit = async () => {
        if (!mode) {
            toast.error("Pick a link type to continue");
            return;
        }
        if (!title.trim() || title.trim() === "Talentgram x") {
            toast.error("Please provide a title like 'Talentgram x Nike'");
            return;
        }
        if (mode === "individual" && selectedTalents.size === 0) {
            toast.error("Select at least one talent");
            return;
        }
        if (mode === "submission" && selectedSubs.size === 0) {
            toast.error("Select at least one approved submission");
            return;
        }
        if (mode === "showcase" && !autoProjectId) {
            toast.error("Pick a project to auto-pull approved submissions from");
            return;
        }
        setSaving(true);
        try {
            const payload = {
                title: title.trim(),
                brand_name: brand || null,
                talent_ids:
                    mode === "individual" ? Array.from(selectedTalents) : [],
                submission_ids:
                    mode === "submission" ? Array.from(selectedSubs) : [],
                auto_pull: mode === "showcase",
                auto_project_id: mode === "showcase" ? autoProjectId : null,
                talent_field_visibility:
                    mode === "individual"
                        ? Object.fromEntries(
                              Object.entries(perTalentVis).filter(([tid]) =>
                                  selectedTalents.has(tid),
                              ),
                          )
                        : {},
                visibility,
                is_public: isPublic,
                notes,
                client_budget_override:
                    clientBudgetOverride && clientBudgetOverride.length > 0
                        ? clientBudgetOverride
                        : null,
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

    /* ----------------------- Mode picker (entry screen) ---------------------- */
    if (!mode) {
        return (
            <div
                className="p-6 md:p-12 max-w-5xl mx-auto"
                data-testid="link-generator-page"
            >
                <Link
                    to="/admin/links"
                    className="inline-flex items-center gap-2 text-xs text-white/50 hover:text-white mb-6"
                >
                    <ArrowLeft className="w-3 h-3" /> Back
                </Link>
                <div className="mb-10">
                    <p className="eyebrow mb-3">New Link</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        How do you want to share?
                    </h1>
                    <p className="text-sm text-white/50 mt-3 max-w-xl">
                        Pick the link type. Each mode shapes the curation
                        experience and the client view differently.
                    </p>
                </div>
                <div
                    className="grid md:grid-cols-3 gap-4"
                    data-testid="mode-picker-grid"
                >
                    {MODES.map((m) => (
                        <button
                            key={m.key}
                            onClick={() => setMode(m.key)}
                            data-testid={`mode-pick-${m.key}`}
                            className="group text-left border border-white/10 hover:border-white p-7 transition-all hover:bg-white/[0.02]"
                        >
                            <div className="flex items-center justify-between mb-5">
                                <m.icon
                                    className="w-7 h-7 text-white/80"
                                    strokeWidth={1.4}
                                />
                                <span className="tg-mono text-[10px] text-white/30 tracking-widest">
                                    {m.tag}
                                </span>
                            </div>
                            <h3 className="font-display text-xl mb-2">
                                {m.title}
                            </h3>
                            <p className="text-xs text-white/50 leading-relaxed">
                                {m.desc}
                            </p>
                        </button>
                    ))}
                </div>
            </div>
        );
    }

    const activeMode = MODES.find((m) => m.key === mode);

    /* ----------------------------- Form (per mode) --------------------------- */
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

            <div className="mb-8 flex items-end justify-between flex-wrap gap-4">
                <div>
                    <p
                        className="eyebrow mb-3"
                        data-testid="active-mode-eyebrow"
                    >
                        {isEdit ? "Edit Link" : "New Link"} · {activeMode.tag}{" "}
                        — {activeMode.title}
                    </p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Generate a client link
                    </h1>
                </div>
                {!isEdit && (
                    <button
                        type="button"
                        onClick={() => {
                            setMode(null);
                            setSelectedTalents(new Set());
                            setSelectedSubs(new Set());
                            setAutoProjectId("");
                        }}
                        data-testid="change-mode-btn"
                        className="text-xs text-white/50 hover:text-white inline-flex items-center gap-1 underline-offset-4 hover:underline"
                    >
                        Change link type
                    </button>
                )}
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
                        {mode === "individual" && (
                            <p className="text-[10px] text-white/30 mt-4 leading-relaxed">
                                Tip: hover any selected talent and click the
                                gear icon to override these toggles for that
                                specific talent.
                            </p>
                        )}
                    </section>

                    {/* Client Budget Override — Phase 1 cleanup (v37):
                        hidden from UI. The budget shown on the link view now
                        comes solely from each talent's submitted budget
                        (admin-editable). Existing override values persist in
                        the DB untouched but are no longer presented or
                        edited. Re-enable by restoring this section. */}
                    {false && (
                    <section
                        className="border border-white/10 p-6"
                        data-testid="link-budget-override-section"
                    >
                        <p className="eyebrow mb-1">Client Budget Override</p>
                        <p className="text-[11px] text-white/40 mb-4">
                            Optional. When non-empty, replaces the project's
                            client-facing budget for this link only.
                        </p>
                        <BudgetLines
                            lines={clientBudgetOverride}
                            onChange={setClientBudgetOverride}
                            testidPrefix="link-client-budget"
                        />
                        <p className="text-[10px] text-white/30 mt-4 tg-mono">
                            Requires "Budget" visibility toggle ON for clients.
                        </p>
                    </section>
                    )}

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

                {/* Right: Subject Picker (per-mode) */}
                <div className="lg:col-span-2 border border-white/10 p-6 min-h-[480px]">
                    <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
                        <div>
                            <p className="eyebrow">
                                {mode === "individual"
                                    ? "Pick Talents"
                                    : mode === "submission"
                                      ? "Pick Approved Submissions"
                                      : "Pick a Project"}
                            </p>
                            <p
                                className="text-xs text-white/40 mt-1"
                                data-testid="selection-count"
                            >
                                {mode === "individual" &&
                                    `${selectedTalents.size} talent${selectedTalents.size === 1 ? "" : "s"} selected`}
                                {mode === "submission" &&
                                    `${selectedSubs.size} submission${selectedSubs.size === 1 ? "" : "s"} selected`}
                                {mode === "showcase" &&
                                    (autoProjectId
                                        ? "Project selected — approved submissions will auto-pull."
                                        : "No project selected yet.")}
                            </p>
                        </div>
                        {mode !== "showcase" && (
                            <div className="relative">
                                <Search className="absolute left-0 top-2.5 w-4 h-4 text-white/40" />
                                <input
                                    value={q}
                                    onChange={(e) => setQ(e.target.value)}
                                    placeholder="Search..."
                                    data-testid="subject-select-search"
                                    className="bg-transparent border-b border-white/15 focus:border-white outline-none py-2 pl-7 text-sm w-56"
                                />
                            </div>
                        )}
                    </div>

                    {/* M1 — Individual Talents */}
                    {mode === "individual" && (
                        filteredTalents.length === 0 ? (
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
                                {filteredTalents.map((t) => {
                                    const cover =
                                        (t.media || []).find(
                                            (m) =>
                                                m.id === t.cover_media_id,
                                        ) ||
                                        (t.media || []).find((m) =>
                                            m.content_type?.startsWith(
                                                "image/",
                                            ),
                                        );
                                    const active = selectedTalents.has(t.id);
                                    const hasOverride =
                                        active &&
                                        perTalentVis[t.id] &&
                                        Object.keys(perTalentVis[t.id])
                                            .length > 0;
                                    return (
                                        <div
                                            key={t.id}
                                            className={`relative border transition-all ${active ? "border-white" : "border-white/10 hover:border-white/30"}`}
                                            data-testid={`select-talent-${t.id}`}
                                        >
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    toggleTalent(t.id)
                                                }
                                                className="block w-full text-left"
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
                                            {active && (
                                                <button
                                                    type="button"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        setEditingTalentId(
                                                            t.id,
                                                        );
                                                    }}
                                                    title="Per-talent visibility override"
                                                    data-testid={`per-talent-override-${t.id}`}
                                                    className={`absolute bottom-2 right-2 p-1.5 rounded-sm transition-all ${hasOverride ? "bg-[#c9a961] text-black" : "bg-black/70 text-white/70 hover:text-white"}`}
                                                >
                                                    <Settings2 className="w-3.5 h-3.5" />
                                                </button>
                                            )}
                                        </div>
                                    );
                                })}
                            </div>
                        )
                    )}

                    {/* M3 — Submission picker */}
                    {mode === "submission" && (
                        filteredSubs.length === 0 ? (
                            <div className="text-white/40 text-sm py-10 text-center">
                                No approved submissions yet. Approve
                                submissions from{" "}
                                <Link
                                    to="/admin/projects"
                                    className="underline hover:text-white"
                                >
                                    a project
                                </Link>{" "}
                                to use them here.
                            </div>
                        ) : (
                            <div
                                className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
                                data-testid="submission-select-grid"
                            >
                                {filteredSubs.map((s) => {
                                    const cover =
                                        (s.media || []).find(
                                            (m) =>
                                                m.id === s.cover_media_id,
                                        ) ||
                                        (s.media || []).find(
                                            (m) =>
                                                m.category === "portfolio" ||
                                                m.category === "image",
                                        );
                                    const active = selectedSubs.has(s.id);
                                    return (
                                        <button
                                            key={s.id}
                                            type="button"
                                            onClick={() => toggleSub(s.id)}
                                            data-testid={`select-submission-${s.id}`}
                                            className={`relative group text-left border transition-all ${active ? "border-[#34C759]" : "border-white/10 hover:border-white/30"}`}
                                        >
                                            <div className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden">
                                                {cover ? (
                                                    <img
                                                        src={FILE_URL(
                                                            cover.storage_path,
                                                        )}
                                                        alt={s.talent_name}
                                                        className="w-full h-full object-cover"
                                                    />
                                                ) : (
                                                    <div className="w-full h-full flex items-center justify-center text-white/20">
                                                        <ImageIcon className="w-6 h-6" />
                                                    </div>
                                                )}
                                                <div className="absolute top-2 left-2 text-[9px] tg-mono tracking-widest uppercase bg-[#34C759]/90 text-black px-2 py-0.5 rounded-sm">
                                                    Audition
                                                </div>
                                            </div>
                                            <div className="p-2.5">
                                                <div className="text-sm font-display truncate">
                                                    {s.talent_name}
                                                </div>
                                                <div className="text-[10px] text-white/40 tg-mono truncate">
                                                    {s.project_brand || "—"}
                                                </div>
                                            </div>
                                            {active && (
                                                <div className="absolute top-2 right-2 w-6 h-6 bg-[#34C759] text-black flex items-center justify-center rounded-sm">
                                                    <Check className="w-3.5 h-3.5" />
                                                </div>
                                            )}
                                        </button>
                                    );
                                })}
                            </div>
                        )
                    )}

                    {/* M2 — Project Showcase (auto-pull) */}
                    {mode === "showcase" && (
                        <div className="space-y-4">
                            <p className="text-sm text-white/60 leading-relaxed">
                                Pick a project. Every submission you approve
                                inside that project will appear in this client
                                link automatically — no need to re-curate.
                            </p>
                            {projects.length === 0 ? (
                                <div className="text-white/40 text-sm py-8 text-center border border-white/10">
                                    No projects yet.{" "}
                                    <Link
                                        to="/admin/projects/new"
                                        className="underline hover:text-white"
                                    >
                                        Create a project
                                    </Link>
                                </div>
                            ) : (
                                <div
                                    className="grid sm:grid-cols-2 gap-3"
                                    data-testid="project-select-grid"
                                >
                                    {projects.map((p) => {
                                        const active =
                                            autoProjectId === p.id;
                                        const approvedCount = submissions.filter(
                                            (s) => s.project_id === p.id,
                                        ).length;
                                        return (
                                            <button
                                                key={p.id}
                                                type="button"
                                                onClick={() =>
                                                    setAutoProjectId(p.id)
                                                }
                                                data-testid={`select-project-${p.id}`}
                                                className={`text-left border p-5 transition-all ${active ? "border-white bg-white/[0.04]" : "border-white/10 hover:border-white/30"}`}
                                            >
                                                <div className="flex items-start justify-between gap-3">
                                                    <div className="min-w-0">
                                                        <div className="font-display text-base truncate">
                                                            {p.brand_name ||
                                                                "Untitled Project"}
                                                        </div>
                                                        <div className="text-[11px] text-white/40 tg-mono mt-1 truncate">
                                                            {p.character ||
                                                                "—"}
                                                        </div>
                                                    </div>
                                                    {active && (
                                                        <div className="w-6 h-6 bg-white text-black flex items-center justify-center rounded-sm shrink-0">
                                                            <Check className="w-3.5 h-3.5" />
                                                        </div>
                                                    )}
                                                </div>
                                                <div className="text-[10px] text-white/30 tg-mono mt-3 inline-flex items-center gap-1.5">
                                                    <Users className="w-3 h-3" />
                                                    {approvedCount} approved
                                                </div>
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Per-talent visibility override modal */}
            {editingTalentId && (
                <PerTalentVisibilityModal
                    talent={talents.find((t) => t.id === editingTalentId)}
                    baseVisibility={visibility}
                    overrides={perTalentVis[editingTalentId] || {}}
                    onSave={(next) => {
                        setPerTalentVis((cur) => ({
                            ...cur,
                            [editingTalentId]: next,
                        }));
                        setEditingTalentId(null);
                    }}
                    onClear={() => {
                        setPerTalentVis((cur) => {
                            const n = { ...cur };
                            delete n[editingTalentId];
                            return n;
                        });
                        setEditingTalentId(null);
                    }}
                    onClose={() => setEditingTalentId(null)}
                />
            )}
        </div>
    );
}

function PerTalentVisibilityModal({
    talent,
    baseVisibility,
    overrides,
    onSave,
    onClear,
    onClose,
}) {
    const [draft, setDraft] = useState(() => ({ ...overrides }));

    const setKey = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
    const reset = (k) =>
        setDraft((d) => {
            const n = { ...d };
            delete n[k];
            return n;
        });

    if (!talent) return null;
    return (
        <div
            className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
            data-testid="per-talent-vis-modal"
            onClick={onClose}
        >
            <div
                className="bg-[#0a0a0a] border border-white/10 max-w-lg w-full max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="px-6 py-4 border-b border-white/10 flex items-center justify-between">
                    <div>
                        <p className="eyebrow">Per-talent override</p>
                        <p className="text-sm font-display mt-1">
                            {talent.name}
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-white/50 hover:text-white text-xs"
                    >
                        Close
                    </button>
                </div>
                <div className="p-6 space-y-3">
                    <p className="text-[11px] text-white/40 leading-relaxed mb-2">
                        Override the link-level visibility for{" "}
                        <strong>{talent.name}</strong> only. Leave a row on
                        "inherit" to keep the link-level setting.
                    </p>
                    {VIS_ITEMS.map((v) => {
                        const overridden = draft[v.key] !== undefined;
                        const effective = overridden
                            ? draft[v.key]
                            : baseVisibility[v.key];
                        return (
                            <div
                                key={v.key}
                                className="flex items-center justify-between gap-3 py-1.5"
                            >
                                <div>
                                    <div className="text-sm text-white/80">
                                        {v.label}
                                    </div>
                                    <div className="text-[10px] text-white/30 tg-mono">
                                        {overridden
                                            ? `Override → ${effective ? "ON" : "OFF"}`
                                            : `Inherit (${baseVisibility[v.key] ? "ON" : "OFF"})`}
                                    </div>
                                </div>
                                <div className="flex items-center gap-1">
                                    <button
                                        type="button"
                                        onClick={() => setKey(v.key, true)}
                                        data-testid={`tfv-${v.key}-on`}
                                        className={`text-[10px] px-2 py-1 border rounded-sm transition-all ${overridden && draft[v.key] === true ? "border-white bg-white text-black" : "border-white/15 text-white/60 hover:border-white/40"}`}
                                    >
                                        ON
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setKey(v.key, false)}
                                        data-testid={`tfv-${v.key}-off`}
                                        className={`text-[10px] px-2 py-1 border rounded-sm transition-all ${overridden && draft[v.key] === false ? "border-white bg-white text-black" : "border-white/15 text-white/60 hover:border-white/40"}`}
                                    >
                                        OFF
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => reset(v.key)}
                                        data-testid={`tfv-${v.key}-inherit`}
                                        className={`text-[10px] px-2 py-1 border rounded-sm transition-all ${!overridden ? "border-[#c9a961] text-[#c9a961]" : "border-white/15 text-white/40 hover:text-white/70"}`}
                                    >
                                        INHERIT
                                    </button>
                                </div>
                            </div>
                        );
                    })}
                </div>
                <div className="px-6 py-4 border-t border-white/10 flex items-center justify-between gap-3">
                    <button
                        onClick={onClear}
                        className="text-xs text-white/50 hover:text-white"
                        data-testid="tfv-clear-btn"
                    >
                        Clear all overrides
                    </button>
                    <button
                        onClick={() => onSave(draft)}
                        data-testid="tfv-save-btn"
                        className="bg-white text-black px-5 py-2 text-sm rounded-sm hover:opacity-90"
                    >
                        Save overrides
                    </button>
                </div>
            </div>
        </div>
    );
}
