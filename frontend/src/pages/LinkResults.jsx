import React, { useEffect, useState, useMemo } from "react";
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

export default function LinkResults() {
    const { id } = useParams();
    const [data, setData] = useState(null);

    useEffect(() => {
        (async () => {
            const res = await adminApi.get(`/links/${id}/results`);
            setData(res.data);
        })();
    }, [id]);

    const subjects = data?.subjects || {};

    const url = data ? `${window.location.origin}/l/${data.link.slug}` : "";

    const copyLink = () => {
        navigator.clipboard.writeText(url);
        toast.success("Link copied");
    };
    const whatsApp = () => {
        const msg = encodeURIComponent(
            `${data.link.title}\n\nCurated portfolio review — ${url}`,
        );
        window.open(`https://wa.me/?text=${msg}`, "_blank");
    };

    const summary = data?.summary || [];
    const totalDownloads = data?.downloads?.length || 0;

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

            {!data ? (
                <div className="text-black/45 text-sm">Loading...</div>
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

                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
                        {[
                            { label: "Total Views", value: data.view_count },
                            {
                                label: "Unique Viewers",
                                value: data.unique_viewers,
                            },
                            {
                                label: "Total Actions",
                                value: (data.actions || []).filter(
                                    (a) => a.action,
                                ).length,
                            },
                            { label: "Downloads", value: totalDownloads },
                        ].map((s) => (
                            <div
                                key={s.label}
                                className="bg-white border border-black/[0.08] rounded-xl p-5 transition-colors duration-150 hover:border-black/[0.12]"
                            >
                                <div className="font-display text-3xl md:text-4xl tracking-tight text-black/85">
                                    {s.value}
                                </div>
                                <div className="text-[11px] text-black/45 tracking-widest uppercase mt-2">
                                    {s.label}
                                </div>
                            </div>
                        ))}
                    </div>

                    <section className="bg-white border border-black/[0.08] rounded-xl mb-10 overflow-hidden">
                        <div className="px-6 py-4 border-b border-black/[0.06] flex items-center justify-between">
                            <p className="eyebrow">Talent Breakdown</p>
                            <p className="text-xs text-black/45">
                                {summary.length} talents
                            </p>
                        </div>
                        {summary.length === 0 ? (
                            <div className="p-8 text-black/45 text-sm">
                                No feedback yet.
                            </div>
                        ) : (
                            <div className="divide-y divide-black/[0.06]">
                                {summary.map((s) => {
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
                                                <div className="mt-4 space-y-2">
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

                    <section className="grid md:grid-cols-2 gap-6">
                        <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden">
                            <div className="px-6 py-4 border-b border-black/[0.06] flex items-center gap-2">
                                <p className="eyebrow">Viewers</p>
                            </div>
                            {data.viewers.length === 0 ? (
                                <div className="p-6 text-black/45 text-sm">
                                    No viewers yet
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                    {data.viewers.map((v) => (
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
                                                {new Date(
                                                    v.created_at,
                                                ).toLocaleString()}
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
                            {data.downloads.length === 0 ? (
                                <div className="p-6 text-black/45 text-sm">
                                    No downloads yet
                                </div>
                            ) : (
                                <div className="divide-y divide-black/[0.06] max-h-96 overflow-y-auto">
                                    {data.downloads.map((d) => (
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
                                                {new Date(
                                                    d.created_at,
                                                ).toLocaleString()}
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
