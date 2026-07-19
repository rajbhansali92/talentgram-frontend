import React, { useEffect, useState } from "react";
import { adminApi, getSubdomainUrl } from "@/lib/api";
import { Link } from "react-router-dom";
import {
    Clapperboard,
    UserCheck,
    ClipboardCheck,
    Link2,
    ArrowRight,
    Copy,
    Send,
    UserPlus,
    Activity,
} from "lucide-react";
import { toast } from "sonner";
import WhatsAppShareButton from "@/components/WhatsAppShareButton";
import { generateApplicationMessage } from "@/lib/whatsappShare";

export default function Dashboard() {
    const [stats, setStats] = useState({
        activeProjects: 0,
        pendingApps: 0,
        pendingReviews: 0,
        activeLinks: 0,
    });
    const [recent, setRecent] = useState([]);
    const [activities, setActivities] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        (async () => {
            try {
                setLoading(true);
                const [projectsRes, appsStatsRes, linksRes, notificationsRes] = await Promise.all([
                    adminApi.get("/projects"),
                    adminApi.get("/applications/stats"),
                    adminApi.get("/links"),
                    adminApi.get("/notifications?size=6").catch(() => ({ data: { items: [] } })),
                ]);

                const activeProjects = projectsRes.data.filter(p => (p.status || "ongoing") === "ongoing");

                // Single aggregation instead of one /submissions/stats call per
                // active project (was N+1 — hundreds of parallel requests at scale).
                const pendingReviewsRes = activeProjects.length
                    ? await adminApi
                        .post("/projects/submissions/pending-count", { project_ids: activeProjects.map(p => p.id) })
                        .catch(() => ({ data: { pending: 0 } }))
                    : { data: { pending: 0 } };
                const totalPendingReviews = pendingReviewsRes.data.pending || 0;

                setStats({
                    activeProjects: activeProjects.length,
                    pendingApps: appsStatsRes.data.pending || 0,
                    pendingReviews: totalPendingReviews,
                    activeLinks: linksRes.data.length,
                });

                setRecent(linksRes.data.slice(0, 5));
                setActivities(notificationsRes.data.items || []);
            } catch (e) {
                console.error("Failed to load dashboard stats", e);
            } finally {
                setLoading(false);
            }
        })();
    }, []);

    const cards = [
        { label: "Active Projects", value: stats.activeProjects, icon: Clapperboard },
        { label: "Pending Applications", value: stats.pendingApps, icon: UserCheck },
        { label: "Pending Reviews", value: stats.pendingReviews, icon: ClipboardCheck },
        { label: "Active Client Links", value: stats.activeLinks, icon: Link2 },
    ];

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto pb-20"
            data-testid="admin-dashboard"
        >
            <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                <div>
                    <p className="eyebrow mb-3">Overview</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        Operations
                    </h1>
                </div>
                <div className="flex gap-3">
                    <Link
                        to="/admin/talents/new"
                        data-testid="dash-new-talent-btn"
                        className="px-4 py-2.5 border border-black/[0.08] hover:border-black/[0.16] rounded-md text-xs font-medium text-black/70 hover:text-black transition-colors duration-150"
                    >
                        + New Talent
                    </Link>
                    <Link
                        to="/admin/links/new"
                        data-testid="dash-new-link-btn"
                        className="px-4 py-2.5 bg-black text-white rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                    >
                        + Generate Link
                    </Link>
                </div>
            </div>

            {loading ? (
                <div className="text-black/45 text-sm mb-6">Loading operations deck...</div>
            ) : (
                <>
                    {/* Top KPI Cards Grid */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
                        {cards.map((c) => (
                            <div
                                key={c.label}
                                className="bg-white border border-black/[0.06] rounded-xl p-4 transition-colors duration-150 hover:border-black/[0.12]"
                            >
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-[10px] text-black/45 tracking-widest uppercase font-semibold">
                                        {c.label}
                                    </span>
                                    <c.icon
                                        className="w-3.5 h-3.5 text-black/35"
                                        strokeWidth={1.5}
                                    />
                                </div>
                                <div className="font-display text-2xl md:text-3xl tracking-tight text-black/90 font-medium">
                                    {c.value}
                                </div>
                            </div>
                        ))}
                    </div>
                </>
            )}

            {/* Rendered unconditionally (not gated on Dashboard's `loading`)
                so its own fetch starts in parallel with the KPI stats fetch
                above instead of waiting for it to finish first. */}
            <OnboardingLinkCard />

            {!loading && (
                <>
                    {/* Recent Links + Recent Activity Row */}
                    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                        {/* Recent Links */}
                        <div className="lg:col-span-2 bg-white border border-black/[0.06] rounded-xl overflow-hidden self-start">
                            <div className="px-5 py-3.5 border-b border-black/[0.06] flex items-center justify-between">
                                <p className="eyebrow">Recent Links</p>
                                <Link
                                    to="/admin/links"
                                    className="text-xs text-black/60 hover:text-black inline-flex items-center gap-1 transition-colors duration-150"
                                >
                                    View all <ArrowRight className="w-3 h-3" />
                                </Link>
                            </div>
                            {recent.length === 0 ? (
                                <div className="p-10 text-center text-black/40 text-sm">
                                    No links generated yet.
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.04]">
                                    {recent.map((l) => (
                                        <Link
                                            key={l.id}
                                            to={`/admin/links/${l.id}/results`}
                                            className="flex items-center justify-between px-5 py-3.5 hover:bg-black/[0.01] transition-colors duration-150"
                                        >
                                            <div className="min-w-0">
                                                <div className="font-medium text-sm text-black/90 truncate">
                                                    {l.title}
                                                </div>
                                                <div className="text-[11px] text-black/40 font-mono mt-0.5 truncate">
                                                    /l/{l.slug}
                                                </div>
                                            </div>
                                            <div className="flex gap-4 sm:gap-6 text-[11px] text-black/55 shrink-0 ml-4">
                                                <span>
                                                    {l.view_count || 0}{" "}
                                                    <span className="text-black/35">views</span>
                                                </span>
                                                <span>
                                                    {l.unique_viewers || 0}{" "}
                                                    <span className="text-black/35">unique viewers</span>
                                                </span>
                                                <span>
                                                    {(l.talent_ids || []).length}{" "}
                                                    <span className="text-black/35">submissions</span>
                                                </span>
                                            </div>
                                        </Link>
                                    ))}
                                </div>
                            )}
                        </div>

                        {/* Recent Activity Feed */}
                        <div className="bg-white border border-black/[0.06] rounded-xl overflow-hidden self-start">
                            <div className="px-5 py-3.5 border-b border-black/[0.06] flex items-center justify-between">
                                <p className="eyebrow">Recent Activity</p>
                                <Activity className="w-3.5 h-3.5 text-black/40" />
                            </div>
                            {activities.length === 0 ? (
                                <div className="p-10 text-center text-black/40 text-sm">
                                    No recent activity logs.
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.04]">
                                    {activities.map((n, idx) => (
                                        <div key={n.id || idx} className="p-4 flex flex-col gap-1 hover:bg-black/[0.01]">
                                            <div className="flex items-start justify-between gap-2">
                                                <span className="text-xs font-medium text-black/80 leading-snug">
                                                    {n.title}
                                                </span>
                                                <span className="text-[10px] text-black/40 font-mono whitespace-nowrap mt-0.5">
                                                    {formatTimeAgo(n.created_at)}
                                                </span>
                                            </div>
                                            {n.body && (
                                                <span className="text-[11px] text-black/45 leading-relaxed truncate">
                                                    {n.body}
                                                </span>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}

function formatTimeAgo(dateStr) {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / (1000 * 60));
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    
    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function OnboardingLinkCard() {
    const [configs, setConfigs] = useState([]);
    const [selectedId, setSelectedId] = useState("default"); // "default" maps to global, or custom UUIDs
    const [globalConfig, setGlobalConfig] = useState({
        profile_requirements: { name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" },
        portfolio_requirements: { portfolio: "required", indian: "required", western: "required", video: "required" }
    });
    const [showSettings, setShowSettings] = useState(false);
    const [newTitle, setNewTitle] = useState("");
    const [loadingConfigs, setLoadingConfigs] = useState(false);

    // Temp editor state for whatever config is being edited
    const [editorTitle, setEditorTitle] = useState("");
    const [editorProfile, setEditorProfile] = useState({ name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" });
    const [editorPortfolio, setEditorPortfolio] = useState({ portfolio: "required", indian: "required", western: "required", video: "required" });

    const loadConfigs = async () => {
        setLoadingConfigs(true);
        try {
            const [globalRes, listRes] = await Promise.all([
                adminApi.get("/admin/onboarding-config"),
                adminApi.get("/admin/profile-configs")
            ]);
            setGlobalConfig(globalRes.data);
            setConfigs(listRes.data || []);
        } catch (err) {
            console.error("Failed to load onboarding configurations:", err);
            toast.error("Failed to load onboarding configurations");
        } finally {
            setLoadingConfigs(false);
        }
    };

    useEffect(() => {
        loadConfigs();
    }, []);

    // Set editor state when selected config changes
    useEffect(() => {
        if (selectedId === "default") {
            setEditorTitle("Global Onboarding Requirements");
            setEditorProfile(globalConfig.profile_requirements || { name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" });
            setEditorPortfolio(globalConfig.portfolio_requirements || { portfolio: "required", indian: "required", western: "required", video: "required" });
        } else {
            const cfg = configs.find(c => c.id === selectedId);
            if (cfg) {
                setEditorTitle(cfg.title || "Custom Configuration");
                setEditorProfile(cfg.profile_requirements || { name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" });
                setEditorPortfolio(cfg.portfolio_requirements || { portfolio: "required", indian: "required", western: "required", video: "required" });
            }
        }
    }, [selectedId, globalConfig, configs]);

    const handleSaveConfig = async () => {
        try {
            if (selectedId === "default") {
                await adminApi.put("/admin/onboarding-config", {
                    profile_requirements: editorProfile,
                    portfolio_requirements: editorPortfolio
                });
                toast.success("Global onboarding configuration updated");
                await loadConfigs();
            } else {
                await adminApi.put(`/admin/profile-configs/${selectedId}`, {
                    title: editorTitle,
                    profile_requirements: editorProfile,
                    portfolio_requirements: editorPortfolio
                });
                toast.success("Custom onboarding configuration updated");
                await loadConfigs();
            }
        } catch (err) {
            console.error("Failed to save config:", err);
            toast.error("Failed to save configuration");
        }
    };

    const handleCreateConfig = async (e) => {
        if (e) e.preventDefault();
        if (!newTitle.trim()) {
            toast.error("Please enter a title for the configuration");
            return;
        }
        try {
            const res = await adminApi.post("/admin/profile-configs", {
                title: newTitle,
                profile_requirements: { name: "required", location: "required", instagram_handle: "required", instagram_followers: "required" },
                portfolio_requirements: { portfolio: "required", indian: "required", western: "required", video: "required" }
            });
            toast.success("Custom configuration created");
            setNewTitle("");
            await loadConfigs();
            setSelectedId(res.data.id);
        } catch (err) {
            console.error("Failed to create configuration:", err);
            toast.error("Failed to create configuration");
        }
    };

    const handleDeleteConfig = async () => {
        if (selectedId === "default") return;
        if (!window.confirm("Are you sure you want to delete this custom configuration?")) return;
        try {
            await adminApi.delete(`/admin/profile-configs/${selectedId}`);
            toast.success("Custom configuration deleted");
            setSelectedId("default");
            await loadConfigs();
        } catch (err) {
            console.error("Failed to delete config:", err);
            toast.error("Failed to delete configuration");
        }
    };

    const toggleProfileField = (field) => {
        setEditorProfile(prev => ({
            ...prev,
            [field]: prev[field] === "required" ? "optional" : "required"
        }));
    };

    const togglePortfolioField = (field) => {
        setEditorPortfolio(prev => ({
            ...prev,
            [field]: prev[field] === "required" ? "optional" : "required"
        }));
    };

    const inviteUrl = selectedId === "default"
        ? getSubdomainUrl("apply")
        : `${getSubdomainUrl("apply")}?profile=${selectedId}`;

    const copy = async () => {
        try {
            await navigator.clipboard.writeText(inviteUrl);
            toast.success("Invite link copied");
        } catch (e) {
            toast.error("Couldn't copy link");
        }
    };

    const share = () => {
        window.open(generateApplicationMessage(inviteUrl), "_blank");
    };

    return (
        <div
            data-testid="onboarding-link-card"
            className="bg-white border border-black/[0.06] rounded-xl mb-6 p-5 transition-colors duration-150 hover:border-black/[0.12]"
        >
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div className="flex items-start gap-4 min-w-0 flex-1">
                    <div className="hidden md:flex shrink-0 w-9 h-9 items-center justify-center border border-black/[0.06] rounded-lg bg-[#fafaf8]">
                        <UserPlus className="w-4 h-4 text-black/60" strokeWidth={1.5} />
                    </div>
                    <div className="min-w-0 flex-1">
                        <p className="eyebrow mb-1.5">Invite Talent & Link Generator</p>
                        <h3 className="font-display text-lg tracking-tight text-black/85 mb-0.5">
                            Generate Invite Link
                        </h3>
                        <p className="text-xs text-black/55 mb-3 leading-relaxed">
                            Select an onboarding configuration to generate a custom invite link.
                        </p>
                        <div className="flex flex-wrap items-center gap-2 mb-3">
                            <select
                                value={selectedId}
                                onChange={(e) => setSelectedId(e.target.value)}
                                className="text-xs border border-black/[0.08] bg-white rounded-md p-1.5 focus:outline-none focus:ring-1 focus:ring-black/20"
                            >
                                <option value="default">Default Global Onboarding</option>
                                {configs.filter(c => c.id).map(c => (
                                    <option key={c.id} value={c.id}>{c.title || "Untitled Config"}</option>
                                ))}
                            </select>

                            <button
                                type="button"
                                onClick={() => setShowSettings(!showSettings)}
                                className="text-xs text-black/60 hover:text-black transition-colors py-1.5 px-3 border border-black/[0.08] hover:bg-black/[0.02] rounded-md font-medium"
                            >
                                {showSettings ? "Hide Configurations Editor" : "Edit / Manage Requirements"}
                            </button>
                        </div>

                        <code
                            data-testid="onboarding-link-url"
                            className="block text-[11px] font-mono text-black/55 bg-[#fafaf8] border border-black/[0.06] rounded-md px-2.5 py-1 w-fit max-w-full truncate"
                        >
                            {inviteUrl}
                        </code>
                    </div>
                </div>

                <div className="flex items-center gap-2 shrink-0 self-end md:self-center w-full md:w-auto">
                    <button
                        type="button"
                        onClick={copy}
                        data-testid="onboarding-copy-btn"
                        className="inline-flex items-center justify-center gap-2 bg-black text-white px-4 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 active:scale-[0.98] transition-all min-h-[40px] flex-1 md:flex-none"
                    >
                        <Copy className="w-3.5 h-3.5" /> Copy Link
                    </button>
                    <WhatsAppShareButton
                        onClick={share}
                        data-testid="onboarding-whatsapp-btn"
                    />
                </div>
            </div>

            {/* Show configuration editor settings */}
            {showSettings && (
                <div className="mt-5 pt-5 border-t border-black/[0.06] space-y-5 animate-in fade-in duration-200">
                    <div className="flex flex-col md:flex-row gap-6">
                        {/* Editor Config Panel */}
                        <div className="flex-1 space-y-4">
                            <div className="flex items-center justify-between">
                                <h4 className="font-semibold text-xs tracking-wider uppercase text-black/55">
                                    Configure: {selectedId === "default" ? "Global Defaults" : "Custom Configuration"}
                                </h4>
                                {selectedId !== "default" && (
                                    <button
                                        type="button"
                                        onClick={handleDeleteConfig}
                                        className="text-xs text-red-600 hover:text-red-700 font-medium"
                                    >
                                        Delete Configuration
                                    </button>
                                )}
                            </div>

                            {selectedId !== "default" && (
                                <div>
                                    <label className="block text-[10px] text-black/45 tracking-widest uppercase font-semibold mb-1">
                                        Configuration Title
                                    </label>
                                    <input
                                        type="text"
                                        value={editorTitle}
                                        onChange={(e) => setEditorTitle(e.target.value)}
                                        className="w-full text-xs border border-black/[0.08] bg-white rounded-md p-2 focus:outline-none focus:ring-1 focus:ring-black/20"
                                    />
                                </div>
                            )}

                            {/* Section 1: Profile Fields */}
                            <div>
                                <label className="block text-[10px] text-black/45 tracking-widest uppercase font-semibold mb-2">
                                    Profile Fields Requirements
                                </label>
                                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                                    {Object.entries(editorProfile).map(([field, val]) => (
                                        <button
                                            key={field}
                                            type="button"
                                            onClick={() => toggleProfileField(field)}
                                            className={`p-2.5 rounded-lg border text-left flex flex-col justify-between transition-all duration-150 ${
                                                val === "required"
                                                    ? "bg-black text-white border-black"
                                                    : "bg-white text-black/60 border-black/[0.08] hover:border-black/20"
                                            }`}
                                        >
                                            <span className="text-[10px] tracking-wider uppercase font-semibold block mb-1">
                                                {field.replace("_", " ")}
                                            </span>
                                            <span className="text-xs font-medium block">
                                                {val === "required" ? "Required" : "Optional"}
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Section 2: Media Requirements */}
                            <div>
                                <label className="block text-[10px] text-black/45 tracking-widest uppercase font-semibold mb-2">
                                    Media Requirements
                                </label>
                                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                                    {Object.entries(editorPortfolio).map(([field, val]) => (
                                        <button
                                            key={field}
                                            type="button"
                                            onClick={() => togglePortfolioField(field)}
                                            className={`p-2.5 rounded-lg border text-left flex flex-col justify-between transition-all duration-150 ${
                                                val === "required"
                                                    ? "bg-black text-white border-black"
                                                    : "bg-white text-black/60 border-black/[0.08] hover:border-black/20"
                                            }`}
                                        >
                                            <span className="text-[10px] tracking-wider uppercase font-semibold block mb-1">
                                                {field.replace("_", " ")}
                                            </span>
                                            <span className="text-xs font-medium block">
                                                {val === "required" ? "Required" : "Optional"}
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            <button
                                type="button"
                                onClick={handleSaveConfig}
                                className="w-full py-2 bg-black hover:bg-black/90 text-white font-medium text-xs rounded-lg transition-colors"
                            >
                                Save Configuration Changes
                            </button>
                        </div>

                        {/* Create Custom Config Form */}
                        <div className="w-full md:w-64 border-t md:border-t-0 md:border-l border-black/[0.06] pt-5 md:pt-0 md:pl-6 space-y-4">
                            <h4 className="font-semibold text-xs tracking-wider uppercase text-black/55">
                                Create New Config
                            </h4>
                            <form onSubmit={handleCreateConfig} className="space-y-3">
                                <div>
                                    <label className="block text-[10px] text-black/45 tracking-widest uppercase font-semibold mb-1">
                                        Config Title
                                    </label>
                                    <input
                                        type="text"
                                        placeholder="e.g. Teen Auditions Config"
                                        value={newTitle}
                                        onChange={(e) => setNewTitle(e.target.value)}
                                        className="w-full text-xs border border-black/[0.08] bg-white rounded-md p-2 focus:outline-none focus:ring-1 focus:ring-black/20"
                                    />
                                </div>
                                <button
                                    type="submit"
                                    className="w-full py-2 border border-black/[0.08] hover:bg-black/[0.02] text-black text-xs font-medium rounded-lg transition-colors"
                                >
                                    + Create Config
                                </button>
                            </form>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
