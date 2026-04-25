import React, { useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { Link } from "react-router-dom";
import {
    Users,
    Link2,
    Eye,
    MousePointerClick,
    ArrowRight,
    Copy,
    Send,
    UserPlus,
} from "lucide-react";
import { toast } from "sonner";
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

            <OnboardingLinkCard />

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

/**
 * OnboardingLinkCard
 * --------------------
 * Surfaces the public Talent Application URL (`/apply`) with one-tap copy +
 * WhatsApp share. The team uses this to invite new talent into the open-call
 * funnel without hand-typing or emailing the URL each time.
 *
 * The link is intentionally project-independent — applicants self-onboard,
 * an admin reviews on `/admin/applications`, and approval merges into the
 * unified talent identity (Phase 0 dedup).
 */
function OnboardingLinkCard() {
    const url = `${window.location.origin}/apply`;

    const copy = async () => {
        try {
            await navigator.clipboard.writeText(url);
            toast.success("Application link copied");
        } catch {
            toast.error("Couldn't copy — long-press the URL to copy manually");
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
            className="border border-white/10 mb-6 p-6 md:p-7 flex flex-col md:flex-row md:items-center gap-5 md:gap-6"
        >
            <div className="flex items-start gap-4 min-w-0 flex-1">
                <div className="hidden md:flex shrink-0 w-11 h-11 items-center justify-center border border-white/15 rounded-sm">
                    <UserPlus
                        className="w-4 h-4 text-white/70"
                        strokeWidth={1.5}
                    />
                </div>
                <div className="min-w-0">
                    <p className="eyebrow mb-2">Public Application Link</p>
                    <h3 className="font-display text-xl md:text-2xl tracking-tight mb-1">
                        Invite new talent
                    </h3>
                    <p className="text-xs text-white/50 mb-2">
                        Share this link with anyone who wants to join Talentgram
                        — they self-onboard and you review on{" "}
                        <Link
                            to="/admin/applications"
                            className="underline underline-offset-2 hover:text-white"
                        >
                            Applications
                        </Link>
                        .
                    </p>
                    <code
                        data-testid="onboarding-link-url"
                        className="block text-[11px] md:text-xs tg-mono text-white/70 truncate bg-white/5 border border-white/10 rounded-sm px-2.5 py-1.5"
                    >
                        {url}
                    </code>
                </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
                <button
                    type="button"
                    onClick={copy}
                    data-testid="onboarding-copy-btn"
                    className="inline-flex items-center justify-center gap-2 bg-white text-black px-4 py-2.5 rounded-sm text-xs tracking-wide hover:opacity-90 active:scale-[0.97] transition-all min-h-[44px] flex-1 md:flex-none"
                >
                    <Copy className="w-3.5 h-3.5" /> Copy
                </button>
                <button
                    type="button"
                    onClick={share}
                    data-testid="onboarding-whatsapp-btn"
                    className="inline-flex items-center justify-center gap-2 border border-white/20 hover:border-white px-4 py-2.5 rounded-sm text-xs tracking-wide active:scale-[0.97] transition-all min-h-[44px] flex-1 md:flex-none"
                >
                    <Send className="w-3.5 h-3.5" /> WhatsApp
                </button>
            </div>
        </div>
    );
}
