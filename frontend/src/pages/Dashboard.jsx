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
                console.error(e);
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
            className="p-6 md:p-10 max-w-7xl mx-auto"
            data-testid="admin-dashboard"
        >
            <div className="flex items-end justify-between flex-wrap gap-4 mb-10">
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

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
                {cards.map((c) => (
                    <div
                        key={c.label}
                        className="bg-white border border-black/[0.08] rounded-xl p-5 md:p-6 transition-colors duration-150 hover:border-black/[0.12]"
                    >
                        <c.icon
                            className="w-4 h-4 text-black/40 mb-4"
                            strokeWidth={1.5}
                        />
                        <div className="font-display text-3xl md:text-4xl tracking-tight text-black/85">
                            {c.value}
                        </div>
                        <div className="text-[11px] text-black/45 tracking-widest uppercase mt-2">
                            {c.label}
                        </div>
                    </div>
                ))}
            </div>

            <DriveBackupCard />

            <OnboardingLinkCard />

            <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden">
                <div className="px-6 py-4 border-b border-black/[0.08] flex items-center justify-between">
                    <p className="eyebrow">Recent Links</p>
                    <Link
                        to="/admin/links"
                        className="text-xs text-black/60 hover:text-black inline-flex items-center gap-1 transition-colors duration-150"
                    >
                        View all <ArrowRight className="w-3 h-3" />
                    </Link>
                </div>
                {recent.length === 0 ? (
                    <div className="p-12 text-center text-black/45 text-sm">
                        No links yet. Generate your first link.
                    </div>
                ) : (
                    <div className="divide-y divide-black/[0.06]">
                        {recent.map((l) => (
                            <Link
                                key={l.id}
                                to={`/admin/links/${l.id}/results`}
                                className="flex items-center justify-between px-6 py-4 hover:bg-black/[0.02] transition-colors duration-150"
                            >
                                <div>
                                    <div className="font-display text-base text-black/85">
                                        {l.title}
                                    </div>
                                    <div className="text-xs text-black/45 mt-1 font-mono">
                                        /l/{l.slug}
                                    </div>
                                </div>
                                <div className="flex gap-6 text-xs text-black/60">
                                    <span>
                                        {l.view_count || 0}
                                        <span className="text-black/30">
                                            {" "}
                                            views
                                        </span>
                                    </span>
                                    <span>
                                        {l.unique_viewers || 0}
                                        <span className="text-black/30">
                                            {" "}
                                            viewers
                                        </span>
                                    </span>
                                    <span>
                                        {(l.talent_ids || []).length}
                                        <span className="text-black/30">
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
            className="bg-white border border-black/[0.08] rounded-xl mb-6 p-6 md:p-7 flex flex-col md:flex-row md:items-center gap-5 md:gap-6 transition-colors duration-150 hover:border-black/[0.12]"
        >
            <div className="flex items-start gap-4 min-w-0 flex-1">
                <div className="hidden md:flex shrink-0 w-11 h-11 items-center justify-center border border-black/[0.08] rounded-lg bg-[#fafaf8]">
                    <UserPlus
                        className="w-4 h-4 text-black/60"
                        strokeWidth={1.5}
                    />
                </div>
                <div className="min-w-0">
                    <p className="eyebrow mb-2">Public Application Link</p>
                    <h3 className="font-display text-xl md:text-2xl tracking-tight text-black/85 mb-1">
                        Invite new talent
                    </h3>
                    <p className="text-xs text-black/60 mb-2">
                        Share this link with anyone who wants to join Talentgram
                        — they self-onboard and you review on{" "}
                        <Link
                            to="/admin/applications"
                            className="underline underline-offset-2 hover:text-black transition-colors duration-150"
                        >
                            Applications
                        </Link>
                        .
                    </p>
                    <code
                        data-testid="onboarding-link-url"
                        className="block text-[11px] md:text-xs font-mono text-black/60 bg-[#fafaf8] border border-black/[0.08] rounded-md px-2.5 py-1.5"
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
                    className="inline-flex items-center justify-center gap-2 bg-black text-white px-4 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 active:scale-[0.98] transition-all min-h-[44px] flex-1 md:flex-none"
                >
                    <Copy className="w-3.5 h-3.5" /> Copy
                </button>
                <button
                    type="button"
                    onClick={share}
                    data-testid="onboarding-whatsapp-btn"
                    className="inline-flex items-center justify-center gap-2 border border-black/[0.08] hover:border-black/[0.16] px-4 py-2.5 rounded-md text-xs font-medium text-black/70 hover:text-black transition-colors duration-150 active:scale-[0.98] min-h-[44px] flex-1 md:flex-none"
                >
                    <Send className="w-3.5 h-3.5" /> WhatsApp
                </button>
            </div>
        </div>
    );
}
