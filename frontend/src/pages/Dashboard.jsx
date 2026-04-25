import React, { useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { Link } from "react-router-dom";
import { Users, Link2, Eye, MousePointerClick, ArrowRight } from "lucide-react";
import DriveBackupCard from "@/components/DriveBackupCard";

export default function Dashboard() {
    const [stats, setStats] = useState({
        talents: 0,
        links: 0,
        views: 0,
        actions: 0,
    });
    const [recent, setRecent] = useState([]);

    useEffect(() => {
        (async () => {
            try {
                const [talents, links] = await Promise.all([
                    adminApi.get("/talents"),
                    adminApi.get("/links"),
                ]);
                const totalViews = links.data.reduce(
                    (a, l) => a + (l.view_count || 0),
                    0,
                );
                const totalUnique = links.data.reduce(
                    (a, l) => a + (l.unique_viewers || 0),
                    0,
                );
                setStats({
                    talents: talents.data.length,
                    links: links.data.length,
                    views: totalViews,
                    actions: totalUnique,
                });
                setRecent(links.data.slice(0, 5));
            } catch (e) {
                // ignore
            }
        })();
    }, []);

    const cards = [
        { label: "Talents", value: stats.talents, icon: Users },
        { label: "Active Links", value: stats.links, icon: Link2 },
        { label: "Total Views", value: stats.views, icon: Eye },
        {
            label: "Unique Viewers",
            value: stats.actions,
            icon: MousePointerClick,
        },
    ];

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="admin-dashboard"
        >
            <div className="flex items-end justify-between flex-wrap gap-4 mb-12">
                <div>
                    <p className="eyebrow mb-3">Overview</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Control Room
                    </h1>
                </div>
                <div className="flex gap-2">
                    <Link
                        to="/admin/talents/new"
                        data-testid="dash-new-talent-btn"
                        className="px-4 py-2.5 border border-white/20 hover:border-white rounded-sm text-xs tracking-wide transition-all"
                    >
                        + New Talent
                    </Link>
                    <Link
                        to="/admin/links/new"
                        data-testid="dash-new-link-btn"
                        className="px-4 py-2.5 bg-white text-black rounded-sm text-xs tracking-wide hover:opacity-90 transition-all"
                    >
                        + Generate Link
                    </Link>
                </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-12">
                {cards.map((c) => (
                    <div
                        key={c.label}
                        className="border border-white/10 p-6 md:p-8 hover:border-white/25 transition-all tg-fade-up"
                    >
                        <c.icon
                            className="w-4 h-4 text-white/40 mb-6"
                            strokeWidth={1.5}
                        />
                        <div className="font-display text-4xl md:text-5xl tracking-tight">
                            {c.value}
                        </div>
                        <div className="eyebrow mt-3">{c.label}</div>
                    </div>
                ))}
            </div>

            <DriveBackupCard />

            <div className="border border-white/10">
                <div className="px-6 py-4 border-b border-white/10 flex items-center justify-between">
                    <p className="eyebrow">Recent Links</p>
                    <Link
                        to="/admin/links"
                        className="text-xs text-white/60 hover:text-white inline-flex items-center gap-1"
                    >
                        View all <ArrowRight className="w-3 h-3" />
                    </Link>
                </div>
                {recent.length === 0 ? (
                    <div className="p-12 text-center text-white/40 text-sm">
                        No links yet. Generate your first link.
                    </div>
                ) : (
                    <div className="divide-y divide-white/10">
                        {recent.map((l) => (
                            <Link
                                key={l.id}
                                to={`/admin/links/${l.id}/results`}
                                className="flex items-center justify-between px-6 py-4 hover:bg-white/5 transition-all"
                            >
                                <div>
                                    <div className="font-display text-lg">
                                        {l.title}
                                    </div>
                                    <div className="text-xs text-white/40 mt-1 tg-mono">
                                        /l/{l.slug}
                                    </div>
                                </div>
                                <div className="flex gap-8 text-xs text-white/60">
                                    <span>
                                        {l.view_count || 0}
                                        <span className="text-white/30">
                                            {" "}
                                            views
                                        </span>
                                    </span>
                                    <span>
                                        {l.unique_viewers || 0}
                                        <span className="text-white/30">
                                            {" "}
                                            viewers
                                        </span>
                                    </span>
                                    <span>
                                        {(l.talent_ids || []).length}
                                        <span className="text-white/30">
                                            {" "}
                                            talents
                                        </span>
                                    </span>
                                </div>
                            </Link>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
