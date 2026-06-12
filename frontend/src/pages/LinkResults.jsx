import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { adminApi, PUBLIC_FRONTEND_URL } from "@/lib/api";
import { toast } from "sonner";
import {
    ArrowLeft,
    ExternalLink,
    Copy,
    MessageCircle,
    Star,
    ThumbsUp,
    XCircle,
    HelpCircle,
    Download,
    Settings,
    Lock,
    ClipboardCheck,
    Flame,
    Thermometer,
    Activity,
} from "lucide-react";

// ── Engagement scoring weights ──────────────────────────────────────────────
const SCORE_WEIGHTS = {
    open: 1,
    view_talent: 1,
    view_media: 2,
    watch_video: 2,
    watch_video_completion: 4,
    log_download: 6,
    zip_folder: 10,
    zip_bundle: 10,
};

function computeHeat(score) {
    if (score >= 12) return { label: "Very Interested", icon: Flame, cls: "text-rose-600", bg: "bg-rose-50 border-rose-200" };
    if (score >= 6)  return { label: "Hot",             icon: Flame, cls: "text-orange-500", bg: "bg-orange-50 border-orange-200" };
    if (score >= 2)  return { label: "Warm",            icon: Thermometer, cls: "text-amber-500", bg: "bg-amber-50 border-amber-200" };
    return null;
}

const ACTION_META = {
    ask_for_test: {
        label: "Ask for Test",
        icon: ClipboardCheck,
        color: "text-amber-600",
    },
    interested: {
        label: "Audition Approved",
        icon: ThumbsUp,
        color: "text-green-600",
    },
    not_for_this: {
        label: "Does Not Work For This Project",
        icon: XCircle,
        color: "text-red-600",
    },
    shortlist: {
        label: "Shortlist",
        icon: Star,
        color: "text-amber-600",
    },
    lock: {
        label: "Lock",
        icon: Lock,
        color: "text-indigo-600",
    },
    not_sure: {
        label: "Unsure",
        icon: HelpCircle,
        color: "text-black/45",
    },
};

// Helper function for safe clipboard operations
const copyToClipboard = async (text) => {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (err) {
        // Fallback for older browsers or permission issues
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.top = "-9999px";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand("copy");
            return true;
        } catch (e) {
            return false;
        } finally {
            document.body.removeChild(textarea);
        }
    }
};

// Centralized date formatter
const formatDate = (dateString) => {
    if (!dateString) return "N/A";
    try {
        return new Date(dateString).toLocaleString();
    } catch {
        return "Invalid date";
    }
};

// Simple loading skeleton
const LoadingSkeleton = () => (
    <div className="animate-pulse">
        <div className="flex items-start justify-between flex-wrap gap-6 mb-10">
            <div className="flex-1">
                <div className="h-4 bg-black/[0.08] rounded w-20 mb-3"></div>
                <div className="h-12 bg-black/[0.08] rounded w-3/4 mb-3"></div>
                <div className="h-4 bg-black/[0.08] rounded w-1/2"></div>
            </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
            {[1, 2, 3, 4].map((i) => (
                <div key={i} className="bg-white border border-black/[0.08] rounded-xl p-5">
                    <div className="h-10 bg-black/[0.08] rounded w-20 mb-2"></div>
                    <div className="h-3 bg-black/[0.08] rounded w-16"></div>
                </div>
            ))}
        </div>
        <div className="bg-white border border-black/[0.08] rounded-xl mb-10 overflow-hidden">
            <div className="px-6 py-4 border-b border-black/[0.06]">
                <div className="h-4 bg-black/[0.08] rounded w-32"></div>
            </div>
            <div className="p-8">
                <div className="h-4 bg-black/[0.08] rounded w-48"></div>
            </div>
        </div>
    </div>
);

export default function LinkResults() {
    const { id } = useParams();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(() => {
        const fetchResults = async () => {
            try {
                setLoading(true);
                setError(null);
                const res = await adminApi.get(`/links/${id}/results`);
                setData(res.data);
            } catch (err) {
                console.error("Failed to fetch link results:", err);
                setError(err.message || "Failed to load results");
                toast.error("Failed to load results. Please try again.");
            } finally {
                setLoading(false);
            }
        };

        fetchResults();
    }, [id]);

    const subjects = data?.subjects || {};
    const url = data ? `${PUBLIC_FRONTEND_URL}/l/${data.link.slug}` : "";
    const viewers = data?.viewers || [];
    const downloads = data?.downloads || [];
    const actions = data?.actions || [];

    // ── Use server-aggregated counts from summary (not the capped raw actions array).
    // This ensures ask_for_test + lock are accurate at scale.
    const summaryByTalent = {};
    if (data?.summary) {
        data.summary.forEach((s) => {
            summaryByTalent[s.talent_id] = s;
        });
    }

    // ── Compute per-talent engagement scores from events + downloads ──────────
    const engagementByTalent = {};
    if (data?.events) {
        data.events.forEach((ev) => {
            const tid = ev.talent_id;
            if (!tid) return;
            if (!engagementByTalent[tid]) engagementByTalent[tid] = 0;
            if (ev.event_type === "watch_video" && ev.video_action === "completion") {
                engagementByTalent[tid] += SCORE_WEIGHTS.watch_video_completion;
            } else {
                engagementByTalent[tid] += SCORE_WEIGHTS[ev.event_type] || 0;
            }
        });
    }
    if (data?.downloads) {
        data.downloads.forEach((d) => {
            const tid = d.talent_id;
            if (!tid) return;
            if (!engagementByTalent[tid]) engagementByTalent[tid] = 0;
            if (d.media_id === "zip:campaign_bundle") {
                engagementByTalent[tid] = (engagementByTalent[tid] || 0) + SCORE_WEIGHTS.zip_bundle;
            } else if (d.media_id === "zip:talent_folder") {
                engagementByTalent[tid] = (engagementByTalent[tid] || 0) + SCORE_WEIGHTS.zip_folder;
            } else {
                engagementByTalent[tid] = (engagementByTalent[tid] || 0) + SCORE_WEIGHTS.log_download;
            }
        });
    }

    // ── Build talent-centric timeline: group events by talent, then by viewer ─
    const talentTimeline = {};
    if (data?.events) {
        data.events.forEach((ev) => {
            const tid = ev.talent_id || "__global__";
            if (!talentTimeline[tid]) talentTimeline[tid] = {};
            const viewer = ev.viewer_email || ev.session_id || "Anonymous";
            if (!talentTimeline[tid][viewer]) {
                talentTimeline[tid][viewer] = {
                    name: ev.viewer_name || "Client",
                    email: ev.viewer_email || "Anonymous Session",
                    items: [],
                };
            }
            talentTimeline[tid][viewer].items.push(ev);
        });
    }

    const totalDownloads = data?.downloads?.length || 0;

    const analytics = data?.link?.analytics || {};
    const trackingViews = analytics.total_views !== undefined ? analytics.total_views : (data?.view_count || data?.link?.view_count || 0);
    const trackingUniq = analytics.unique_views !== undefined ? (analytics.unique_views.length || 0) : (data?.unique_viewers || data?.link?.unique_viewers || 0);
    const viewedTalentsCount = Object.keys(analytics.viewed_talents || {}).length;
    const totalWatchSeconds = Object.values(analytics.watch_durations || {}).reduce((a, b) => a + b, 0);
    const watchMinutes = Math.round(totalWatchSeconds / 60);

    const copyLink = async () => {
        const success = await copyToClipboard(url);
        if (success) {
            toast.success("Link copied");
        } else {
            toast.error("Failed to copy link. Please copy manually.");
        }
    };

    const whatsApp = () => {
        const msg = encodeURIComponent(
            `${data.link.title}\n\nCurated portfolio review — ${url}`,
        );
        window.open(`https://wa.me/?text=${msg}`, "_blank");
    };

    if (error) {
        return (
            <div className="p-6 md:p-10 max-w-7xl mx-auto">
                <Link
                    to="/admin/links"
                    className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6 transition-colors duration-150"
                >
                    <ArrowLeft className="w-3 h-3" /> Back to links
                </Link>
                <div className="bg-red-50 border border-red-200 rounded-xl p-8 text-center">
                    <div className="text-red-600 mb-2">Failed to load results</div>
                    <div className="text-sm text-red-500 mb-4">{error}</div>
                    <button
                        onClick={() => window.location.reload()}
                        className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700 transition-colors"
                    >
                        Try Again
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto"
            data-testid="link-results-page"
        >
            <Link
                to="/admin/links"
                className="inline-flex items-center gap-2 text-xs text-black/45 hover:text-black/80 mb-6 transition-colors duration-150"
            >
                <ArrowLeft className="w-3 h-3" /> Back to links
            </Link>

            {loading ? (
                <LoadingSkeleton />
            ) : (
                <>
                    <div className="flex items-start justify-between flex-wrap gap-6 mb-10">
                        <div>
                            <p className="eyebrow mb-3">Results</p>
                            <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90 mb-3">
                                {data.link.title}
                            </h1>
                            <p className="text-xs text-black/45 font-mono">
                                {url}
                            </p>
                        </div>
                        <div className="flex gap-2 flex-wrap">
                            <a
                                href={`/l/${data.link.slug}`}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs text-black/70 hover:text-black transition-colors duration-150"
                            >
                                <ExternalLink className="w-3.5 h-3.5" /> Open
                            </a>
                            <button
                                onClick={copyLink}
                                data-testid="results-copy-btn"
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs text-black/70 hover:text-black transition-colors duration-150"
                            >
                                <Copy className="w-3.5 h-3.5" /> Copy
                            </button>
                            <button
                                onClick={whatsApp}
                                data-testid="results-whatsapp-btn"
                                className="inline-flex items-center gap-2 px-4 py-2.5 bg-[#25D366] text-white rounded-lg text-xs font-medium hover:opacity-90 transition-colors duration-150"
                            >
                                <MessageCircle className="w-3.5 h-3.5" />{" "}
                                WhatsApp
                            </button>
                            <Link
                                to={`/admin/links/${id}/edit`}
                                className="inline-flex items-center gap-2 px-4 py-2.5 border border-black/[0.08] hover:border-black/[0.16] rounded-lg text-xs text-black/70 hover:text-black transition-colors duration-150"
                            >
                                <Settings className="w-3.5 h-3.5" /> Edit
                            </Link>
                        </div>
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-10">
                        {[
                            { label: "Total Views", value: trackingViews },
                            {
                                label: "Unique Viewers",
                                value: trackingUniq,
                            },
                            {
                                label: "Profiles Viewed",
                                value: viewedTalentsCount,
                            },
                            {
                                label: "Video Watched",
                                value: `${watchMinutes} min`,
                            },
                            {
                                label: "Total Actions",
                                value: actions.filter((a) => a.action).length,
                            },
                            { label: "Downloads", value: totalDownloads },
                        ].map((s) => (
                            <div
                                key={s.label}
                                className="bg-white border border-black/[0.08] rounded-xl p-4 transition-colors duration-150 hover:border-black/[0.12]"
                            >
                                <div className="font-display text-2xl md:text-3xl tracking-tight text-black/85">
                                    {s.value}
                                </div>
                                <div className="text-[10px] text-black/45 tracking-widest uppercase mt-2">
                                    {s.label}
                                </div>
                            </div>
                        ))}
                    </div>

                    <section className="bg-white border border-black/[0.08] rounded-xl mb-10 overflow-hidden">
                        <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                            <p className="eyebrow">Talent Breakdown</p>
                            <p className="text-xs text-black/45">
                                {data.summary?.length || 0} talents
                            </p>
                        </div>
                        {!data.summary || data.summary.length === 0 ? (
                            <div className="p-8 text-center">
                                <div className="text-black/70 text-sm mb-2">
                                    No viewer feedback recorded yet.
                                </div>
                                <div className="text-black/45 text-xs">
                                    Share the link with clients to begin collecting responses.
                                </div>
                            </div>
                        ) : (
                            <div className="divide-y divide-black/[0.06]">
                                {data.summary.map((s) => {
                                    const t = subjects[s.talent_id];
                                    const score = engagementByTalent[s.talent_id] || 0;
                                    const heat = computeHeat(score);
                                    return (
                                        <div
                                            key={s.talent_id}
                                            className="p-6"
                                            data-testid={`summary-${s.talent_id}`}
                                        >
                                            <div className="flex items-start justify-between flex-wrap gap-4">
                                                <div>
                                                    <div className="flex items-center gap-2 flex-wrap">
                                                        <h3 className="font-display text-lg text-black/85 font-medium">
                                                            {t?.name || "Unnamed Talent"}
                                                        </h3>
                                                        {heat && (
                                                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold rounded-full border ${heat.bg}`}>
                                                                <heat.icon className={`w-3 h-3 ${heat.cls}`} />
                                                                <span className={heat.cls}>{heat.label}</span>
                                                            </span>
                                                        )}
                                                    </div>
                                                    <div className="text-[11px] text-black/45 mt-1">
                                                        {t?.source === "submission" ? "Audition submission" : "Talent"}
                                                        {score > 0 && (
                                                            <span className="ml-2 font-mono text-black/30">engagement: {score}pts</span>
                                                        )}
                                                    </div>
                                                </div>
                                                {/* Use server-aggregated counts — accurate at scale, not capped at 500 */}
                                                <div className="flex gap-4 flex-wrap text-xs">
                                                    {Object.entries(ACTION_META).map(([k, m]) => (
                                                        <div key={k} className="flex items-center gap-1.5">
                                                            <m.icon className={`w-3.5 h-3.5 ${m.color}`} />
                                                            <span className="font-mono text-black/70">
                                                                {summaryByTalent[s.talent_id]?.[k] ?? s[k] ?? 0}
                                                            </span>
                                                            <span className="text-black/45">{m.label}</span>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                            {s.comments?.length > 0 && (
                                                <div className="mt-4 space-y-2 max-h-96 overflow-y-auto">
                                                    {s.comments.map((c, i) => (
                                                        <div
                                                            key={`${c.viewer_email}-${c.updated_at || i}`}
                                                            className="border-l-2 border-black/[0.08] pl-3 text-sm"
                                                        >
                                                            <div className="text-black/80">"{c.comment}"</div>
                                                            <div className="text-[10px] text-black/45 mt-1">
                                                                — {c.viewer_name} ({c.viewer_email})
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    );
                                })}
                            </div>
                        )}
                    </section>

                    <section className="bg-white border border-black/[0.08] rounded-xl mb-10 overflow-hidden">
                        <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                            <p className="eyebrow">Talent Engagement Timeline</p>
                            <p className="text-xs text-black/45">Grouped by talent · real-time</p>
                        </div>
                        {!data?.events || data.events.length === 0 ? (
                            <div className="p-8 text-center text-black/45 text-sm">
                                No client engagement tracked yet.
                            </div>
                        ) : (
                            <div className="p-6 max-h-[600px] overflow-y-auto space-y-8">
                                {(() => {
                                    // Sort talent groups: those with a name first, then global
                                    const talentOrder = Object.keys(talentTimeline).sort((a, b) => {
                                        if (a === "__global__") return 1;
                                        if (b === "__global__") return -1;
                                        const nameA = subjects[a]?.name || "";
                                        const nameB = subjects[b]?.name || "";
                                        return nameA.localeCompare(nameB);
                                    });

                                    return talentOrder.map((tid) => {
                                        const tName = tid === "__global__" ? "General" : (subjects[tid]?.name || "Talent");
                                        const viewerGroups = talentTimeline[tid];
                                        const score = tid !== "__global__" ? (engagementByTalent[tid] || 0) : 0;
                                        const heat = computeHeat(score);

                                        return (
                                            <div key={tid} className="space-y-3">
                                                {/* Talent header */}
                                                <div className="flex items-center gap-2 border-b border-black/[0.05] pb-2">
                                                    <Activity className="w-3.5 h-3.5 text-black/30" />
                                                    <h4 className="font-semibold text-sm text-black/80">{tName}</h4>
                                                    {heat && (
                                                        <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-semibold rounded-full border ${heat.bg}`}>
                                                            <heat.icon className={`w-2.5 h-2.5 ${heat.cls}`} />
                                                            <span className={heat.cls}>{heat.label}</span>
                                                        </span>
                                                    )}
                                                </div>
                                                {/* Viewer sub-groups */}
                                                <div className="space-y-4 pl-4">
                                                    {Object.values(viewerGroups).map((viewerGroup) => (
                                                        <div key={viewerGroup.email} className="space-y-2">
                                                            <div className="flex items-center justify-between">
                                                                <span className="text-[11px] font-medium text-black/60">{viewerGroup.name}</span>
                                                                <span className="text-[10px] font-mono text-black/35">{viewerGroup.email}</span>
                                                            </div>
                                                            <div className="space-y-1.5 pl-3 border-l border-black/[0.05]">
                                                                {viewerGroup.items.slice(0, 12).map((item, idx) => {
                                                                    const timeStr = new Date(item.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                                                                    let actionText = "";
                                                                    let details = "";

                                                                    if (item.event_type === "open") {
                                                                        actionText = "Opened Link";
                                                                        details = "Started review session";
                                                                    } else if (item.event_type === "view_talent") {
                                                                        actionText = "Viewed Profile";
                                                                        details = tName !== "General" ? tName : "";
                                                                    } else if (item.event_type === "view_media") {
                                                                        actionText = "Portfolio View";
                                                                        details = "Opened portfolio image";
                                                                    } else if (item.event_type === "watch_video") {
                                                                        const va = item.video_action;
                                                                        if (va === "play") { actionText = "Played Video"; details = "Started watching"; }
                                                                        else if (va === "replay") { actionText = "Replayed Video"; details = "Watched again"; }
                                                                        else if (va === "completion") { actionText = "Completed Video"; details = "Watched to end ✓"; }
                                                                        else {
                                                                            actionText = "Watched Video";
                                                                            details = item.watch_time ? `${Math.round(item.watch_time)}s watched` : "";
                                                                        }
                                                                    } else if (item.event_type === "review_talent") {
                                                                        actionText = "Reviewed";
                                                                        details = "Completed review";
                                                                    } else {
                                                                        actionText = item.event_type;
                                                                        details = "";
                                                                    }

                                                                    return (
                                                                        <div key={item.id || idx} className="flex items-start gap-2 text-xs">
                                                                            <span className="font-mono text-black/30 shrink-0 w-10">{timeStr}</span>
                                                                            <div className="flex-1">
                                                                                <span className="font-medium text-black/70 mr-1">{actionText}</span>
                                                                                {details && <span className="text-black/40">{details}</span>}
                                                                            </div>
                                                                        </div>
                                                                    );
                                                                })}
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        );
                                    });
                                })()}
                            </div>
                        )}
                    </section>

                    <section className="grid md:grid-cols-2 gap-6">
                        <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center gap-2">
                                <p className="eyebrow">Viewers</p>
                            </div>
                            {viewers.length === 0 ? (
                                <div className="p-6 text-black/45 text-sm">
                                    No viewers yet
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                    {viewers.map((v) => (
                                        <div
                                            key={v.id}
                                            className="px-6 py-4 text-sm transition-colors duration-150 hover:bg-black/[0.02]"
                                        >
                                            <div className="font-medium text-black/85">
                                                {v.viewer_name}
                                            </div>
                                            <div className="text-xs text-black/45 mt-0.5">
                                                {v.viewer_email}
                                            </div>
                                            <div className="text-[10px] text-black/35 mt-1">
                                                {formatDate(v.created_at)}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                        <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center gap-2">
                                <Download className="w-3.5 h-3.5 text-black/45" />
                                <p className="eyebrow">Download Log</p>
                            </div>
                            {downloads.length === 0 ? (
                                <div className="p-6 text-black/45 text-sm">
                                    No downloads yet
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                    {downloads.map((d) => {
                                        const isBundle = d.media_id === "zip:campaign_bundle";
                                        const isFolder = d.media_id === "zip:talent_folder";
                                        const talentName = d.talent_id === "all"
                                            ? "All Talents"
                                            : subjects[d.talent_id]?.name || d.talent_id;
                                        return (
                                            <div
                                                key={d.id}
                                                className="px-6 py-4 text-sm transition-colors duration-150 hover:bg-black/[0.02]"
                                            >
                                                <div className="flex items-center gap-2">
                                                    <span className="font-medium text-black/85">{d.viewer_name}</span>
                                                    {(isBundle || isFolder) && (
                                                        <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded-full border ${
                                                            isBundle ? "bg-indigo-50 text-indigo-600 border-indigo-200" : "bg-blue-50 text-blue-600 border-blue-200"
                                                        }`}>
                                                            {isBundle ? "Campaign Bundle" : "Folder ZIP"}
                                                        </span>
                                                    )}
                                                </div>
                                                <div className="text-xs text-black/45 mt-0.5">{talentName}</div>
                                                <div className="text-[10px] text-black/35 mt-1">{formatDate(d.created_at)}</div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    </section>
                </>
            )}
        </div>
    );
}
