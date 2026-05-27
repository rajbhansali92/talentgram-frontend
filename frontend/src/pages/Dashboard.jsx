import React, { useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
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
                
                // Fetch submission stats in parallel for active projects to sum pending reviews
                const statsPromises = activeProjects.map(p => 
                    adminApi.get(`/projects/${p.id}/submissions/stats`).catch(() => ({ data: { pending: 0 } }))
                );
                const statsResults = await Promise.all(statsPromises);
                const totalPendingReviews = statsResults.reduce((acc, curr) => acc + (curr.data.pending || 0), 0);

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
                <div className="text-black/45 text-sm">Loading operations deck...</div>
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

                    {/* Invite New Talent Card */}
                    <OnboardingLinkCard />

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
    const url = `${window.location.origin}/apply`;

    const copy = async () => {
        try {
            await navigator.clipboard.writeText(url);
            toast.success("Application link copied");
        } catch (e) {
            toast.error("Couldn't copy link");
        }
    };

    const share = () => {
        const msg = encodeURIComponent(
            `Hi! Apply to join Talentgram — share your portfolio with us:\n${url}`,
        );
        window.open(`https://wa.me/?text=${msg}`, "_blank");
    };

    return (
        <div
            data-testid="onboarding-link-card"
            className="bg-white border border-black/[0.06] rounded-xl mb-6 p-4 sm:p-5 flex flex-col md:flex-row md:items-center justify-between gap-4 transition-colors duration-150 hover:border-black/[0.12]"
        >
            <div className="flex items-start gap-4 min-w-0 flex-1">
                <div className="hidden md:flex shrink-0 w-9 h-9 items-center justify-center border border-black/[0.06] rounded-lg bg-[#fafaf8]">
                    <UserPlus
                        className="w-4 h-4 text-black/60"
                        strokeWidth={1.5}
                    />
                </div>
                <div className="min-w-0 flex-1">
                    <p className="eyebrow mb-1.5">Public Application Link</p>
                    <h3 className="font-display text-lg tracking-tight text-black/85 mb-0.5">
                        Invite new talent
                    </h3>
                    <p className="text-xs text-black/55 mb-3 leading-relaxed">
                        Share this link with anyone to self-onboard and review on{" "}
                        <Link
                            to="/admin/applications"
                            className="underline underline-offset-2 hover:text-black transition-all duration-150"
                        >
                            Applications
                        </Link>
                        .
                    </p>
                    <code
                        data-testid="onboarding-link-url"
                        className="block text-[11px] font-mono text-black/55 bg-[#fafaf8] border border-black/[0.06] rounded-md px-2.5 py-1 w-fit max-w-full truncate"
                    >
                        {url}
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
                    <Copy className="w-3.5 h-3.5" /> Copy
                </button>
                <button
                    type="button"
                    onClick={share}
                    data-testid="onboarding-whatsapp-btn"
                    className="inline-flex items-center justify-center gap-2 border border-black/[0.06] hover:border-black/[0.16] px-4 py-2.5 rounded-md text-xs font-medium text-black/70 hover:text-black transition-colors duration-150 active:scale-[0.98] min-h-[40px] flex-1 md:flex-none"
                >
                    <Send className="w-3.5 h-3.5" /> WhatsApp
                </button>
            </div>
        </div>
    );
}
