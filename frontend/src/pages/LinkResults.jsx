import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { adminApi } from "@/lib/api";
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
} from "lucide-react";

const ACTION_META = {
    shortlist: {
        label: "Shortlisted",
        icon: Star,
        color: "text-amber-600",
    },
    interested: {
        label: "Interested",
        icon: ThumbsUp,
        color: "text-green-600",
    },
    not_for_this: {
        label: "Not for this",
        icon: XCircle,
        color: "text-red-600",
    },
    not_sure: {
        label: "Not sure",
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
    const url = data ? `${window.location.origin}/l/${data.link.slug}` : "";
    const totalDownloads = data?.downloads?.length || 0;
    const viewers = data?.viewers || [];
    const downloads = data?.downloads || [];
    const actions = data?.actions || [];

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
                                    return (
                                        <div
                                            key={s.talent_id}
                                            className="p-6"
                                            data-testid={`summary-${s.talent_id}`}
                                        >
                                            <div className="flex items-start justify-between flex-wrap gap-4">
                                                <div>
                                                    <h3 className="font-display text-lg text-black/85">
                                                        {t?.name || s.talent_id}
                                                    </h3>
                                                    <div className="text-[11px] text-black/45 mt-1">
                                                        {t?.source === "submission" ? "Audition submission" : "Talent"}
                                                    </div>
                                                </div>
                                                <div className="flex gap-4 flex-wrap text-xs">
                                                    {Object.entries(
                                                        ACTION_META,
                                                    ).map(([k, m]) => (
                                                        <div
                                                            key={k}
                                                            className="flex items-center gap-1.5"
                                                        >
                                                            <m.icon
                                                                className={`w-3.5 h-3.5 ${m.color}`}
                                                            />
                                                            <span className="font-mono text-black/70">
                                                                {s[k] || 0}
                                                            </span>
                                                            <span className="text-black/45">
                                                                {m.label}
                                                            </span>
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
                                                            <div className="text-black/80">
                                                                "{c.comment}"
                                                            </div>
                                                            <div className="text-[10px] text-black/45 mt-1">
                                                                — {c.viewer_name}{" "}
                                                                ({c.viewer_email})
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
                            <p className="eyebrow">Client Activity Timeline</p>
                            <p className="text-xs text-black/45">
                                Real-time professional engagement
                            </p>
                        </div>
                        {!data?.events || data.events.length === 0 ? (
                            <div className="p-8 text-center text-black/45 text-sm">
                                No client engagement tracked yet.
                            </div>
                        ) : (
                            <div className="p-6 max-h-[500px] overflow-y-auto space-y-6">
                                {(() => {
                                    const grouped = {};
                                    data.events.forEach((ev) => {
                                        const key = ev.viewer_email || ev.session_id || "Anonymous";
                                        if (!grouped[key]) {
                                            grouped[key] = {
                                                name: ev.viewer_name || "Client",
                                                email: ev.viewer_email || "Anonymous Session",
                                                items: [],
                                            };
                                        }
                                        grouped[key].items.push(ev);
                                    });

                                    return Object.values(grouped).map((viewerGroup) => (
                                        <div key={viewerGroup.email} className="space-y-3">
                                            <div className="flex items-center justify-between border-b border-black/[0.03] pb-1.5">
                                                <h4 className="font-semibold text-sm text-black/80">
                                                    {viewerGroup.name}
                                                </h4>
                                                <span className="text-[10px] font-mono text-black/45">
                                                    {viewerGroup.email}
                                                </span>
                                            </div>
                                            <div className="space-y-2.5 pl-3 border-l border-black/[0.05]">
                                                {viewerGroup.items.slice(0, 15).map((item, idx) => {
                                                    const date = new Date(item.created_at);
                                                    const timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                                                    
                                                    let actionText = "";
                                                    let details = "";
                                                    const tName = subjects[item.talent_id]?.name || "Talent";
                                                    
                                                    if (item.event_type === "open") {
                                                        actionText = "Opened Link";
                                                        details = "Started review session";
                                                    } else if (item.event_type === "view_talent") {
                                                        actionText = "Viewed Profile";
                                                        details = `Opened ${tName}'s profile`;
                                                    } else if (item.event_type === "view_media") {
                                                        actionText = "Engaged";
                                                        details = `Checked portfolio assets of ${tName}`;
                                                    } else if (item.event_type === "watch_video") {
                                                        actionText = "Watched Video";
                                                        details = `Viewed audition takes of ${tName}`;
                                                    } else if (item.event_type === "review_talent") {
                                                        actionText = "Reviewed";
                                                        details = `Completed initial review of ${tName}`;
                                                    } else {
                                                        actionText = item.event_type;
                                                        details = tName;
                                                    }
                                                    
                                                    return (
                                                        <div key={item.id || idx} className="flex items-start gap-3 text-xs">
                                                            <span className="font-mono text-black/35 shrink-0 w-10">
                                                                {timeStr}
                                                            </span>
                                                            <div className="flex-1">
                                                                <span className="font-medium text-black/75 mr-1.5">
                                                                    {actionText}
                                                                </span>
                                                                <span className="text-black/45">
                                                                    {details}
                                                                </span>
                                                            </div>
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                    ));
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
                                    {downloads.map((d) => (
                                        <div
                                            key={d.id}
                                            className="px-6 py-4 text-sm transition-colors duration-150 hover:bg-black/[0.02]"
                                        >
                                            <div className="font-medium text-black/85">
                                                {d.viewer_name}
                                            </div>
                                            <div className="text-xs text-black/45 mt-0.5">
                                                {subjects[d.talent_id]?.name ||
                                                    d.talent_id}
                                            </div>
                                            <div className="text-[10px] text-black/35 mt-1">
                                                {formatDate(d.created_at)}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </section>
                </>
            )}
        </div>
    );
}
