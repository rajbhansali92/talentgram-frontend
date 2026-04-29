import React, { useEffect, useMemo, useState } from "react";
import { adminApi, COVER_URL } from "@/lib/api";
import { toast } from "sonner";
import {
    X,
    Check,
    XCircle,
    Mail,
    Phone,
    MapPin,
    Instagram,
    Loader2,
    Eye,
    Image as ImageIcon,
    UserPlus,
    Filter,
} from "lucide-react";

const STATUS_FILTERS = [
    { key: "all", label: "All" },
    { key: "pending", label: "Pending", query: { decision: "pending", status: "submitted" } },
    { key: "approved", label: "Approved", query: { decision: "approved" } },
    { key: "rejected", label: "Rejected", query: { decision: "rejected" } },
    { key: "drafts", label: "Drafts", query: { status: "draft" } },
];

export default function Applications() {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [filter, setFilter] = useState("pending");
    const [active, setActive] = useState(null);

    const load = async () => {
        setLoading(true);
        try {
            const f = STATUS_FILTERS.find((s) => s.key === filter);
            const params = f?.query || {};
            const { data } = await adminApi.get("/applications", { params });
            setItems(data);
        } catch (e) {
            toast.error("Failed to load applications");
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filter]);

    const decide = async (aid, decision) => {
        try {
            const { data } = await adminApi.post(
                `/applications/${aid}/decision`,
                { decision },
            );
            toast.success(
                decision === "approved"
                    ? data.merged
                        ? "Approved & merged with existing talent"
                        : "Approved & added to talent DB"
                    : "Rejected",
            );
            setActive(null);
            await load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    const counts = useMemo(() => {
        const c = { all: items.length, pending: 0, approved: 0, rejected: 0, drafts: 0 };
        for (const i of items) {
            if (i.status === "draft") c.drafts++;
            else if (i.decision === "approved") c.approved++;
            else if (i.decision === "rejected") c.rejected++;
            else if (i.status === "submitted" && i.decision === "pending") c.pending++;
        }
        return c;
    }, [items]);

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="applications-page"
        >
            <div className="mb-10 flex items-start justify-between flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Talent Applications</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Applicants
                    </h1>
                    <p className="text-white/50 text-sm mt-3 max-w-xl">
                        Open signups via <span className="tg-mono">/apply</span>.
                        Review, approve, or reject — approved talents land in
                        the master DB.
                    </p>
                </div>
                <a
                    href="/apply"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-2 border border-white/15 hover:border-white px-4 py-2.5 rounded-sm text-xs"
                >
                    <UserPlus className="w-3.5 h-3.5" /> Open /apply
                </a>
            </div>

            {/* Filter chips */}
            <div className="flex items-center gap-2 flex-wrap mb-6">
                <Filter className="w-3.5 h-3.5 text-white/40" />
                {STATUS_FILTERS.map((f) => (
                    <button
                        key={f.key}
                        onClick={() => setFilter(f.key)}
                        data-testid={`filter-${f.key}`}
                        className={`px-3 py-1.5 rounded-full border text-[11px] tracking-widest uppercase transition-all ${
                            filter === f.key
                                ? "border-white bg-white text-black"
                                : "border-white/15 text-white/60 hover:border-white/40"
                        }`}
                    >
                        {f.label}
                    </button>
                ))}
            </div>

            {loading ? (
                <div className="py-20 flex justify-center">
                    <Loader2 className="w-5 h-5 animate-spin text-white/40" />
                </div>
            ) : items.length === 0 ? (
                <div className="border border-dashed border-white/10 py-20 text-center text-white/40 text-sm">
                    No applications match this filter.
                </div>
            ) : (
                <div
                    className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"
                    data-testid="applications-grid"
                >
                    {items.map((a) => (
                        <ApplicationCard
                            key={a.id}
                            app={a}
                            onReview={() => setActive(a)}
                            onDecide={(d) => decide(a.id, d)}
                            counts={counts}
                        />
                    ))}
                </div>
            )}

            {active && (
                <ReviewModal
                    app={active}
                    onClose={() => setActive(null)}
                    onDecide={(d) => decide(active.id, d)}
                />
            )}
        </div>
    );
}

function ApplicationCard({ app, onReview, onDecide }) {
    const fd = app.form_data || {};
    const imgs = (app.media || []).filter((m) => m.category === "image");
    const coverUrl = COVER_URL(app);
    const badge = getStatusBadge(app);
    return (
        <div
            className="border border-white/10 hover:border-white/25 transition-all flex flex-col"
            data-testid={`app-card-${app.id}`}
        >
            <div className="relative aspect-[4/5] bg-[#0a0a0a] overflow-hidden">
                {coverUrl ? (
                    <img
                        src={coverUrl}
                        alt={app.talent_name}
                        loading="lazy"
                        className="w-full h-full object-cover"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-white/20">
                        <ImageIcon className="w-8 h-8" />
                    </div>
                )}
                <span
                    className={`absolute top-3 left-3 px-2 py-0.5 text-[10px] tg-mono uppercase rounded-sm ${badge.cls}`}
                >
                    {badge.label}
                </span>
                {imgs.length > 0 && (
                    <span className="absolute bottom-3 right-3 px-2 py-0.5 bg-black/60 text-white/70 text-[10px] tg-mono rounded-sm">
                        {imgs.length} img
                    </span>
                )}
            </div>

            <div className="p-4 flex-1 flex flex-col">
                <h3 className="font-display text-lg truncate">
                    {app.talent_name}
                </h3>
                <div className="mt-2 space-y-1 text-[11px] tg-mono text-white/50 min-w-0">
                    <div className="flex items-center gap-1.5 truncate">
                        <Mail className="w-3 h-3 shrink-0" />
                        <span className="truncate">{app.talent_email}</span>
                    </div>
                    {fd.location && (
                        <div className="flex items-center gap-1.5 truncate">
                            <MapPin className="w-3 h-3 shrink-0" />
                            <span>{fd.location}</span>
                        </div>
                    )}
                    {fd.instagram_handle && (
                        <div className="flex items-center gap-1.5 truncate">
                            <Instagram className="w-3 h-3 shrink-0" />
                            <span>{fd.instagram_handle}</span>
                        </div>
                    )}
                </div>

                <div className="mt-4 flex items-center gap-2">
                    <button
                        onClick={onReview}
                        data-testid={`review-${app.id}`}
                        className="flex-1 inline-flex items-center justify-center gap-1.5 border border-white/15 hover:border-white text-xs py-2 rounded-sm"
                    >
                        <Eye className="w-3.5 h-3.5" /> Review
                    </button>
                    {app.status === "submitted" && app.decision === "pending" && (
                        <>
                            <button
                                onClick={() => onDecide("approved")}
                                data-testid={`approve-${app.id}`}
                                title="Approve"
                                className="w-9 h-9 inline-flex items-center justify-center rounded-sm bg-[#34C759] text-black hover:opacity-90"
                            >
                                <Check className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => onDecide("rejected")}
                                data-testid={`reject-${app.id}`}
                                title="Reject"
                                className="w-9 h-9 inline-flex items-center justify-center rounded-sm border border-[#FF3B30]/60 text-[#FF3B30] hover:bg-[#FF3B30]/10"
                            >
                                <XCircle className="w-4 h-4" />
                            </button>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function getStatusBadge(app) {
    if (app.status === "draft")
        return { label: "Draft", cls: "bg-white/10 text-white/60" };
    if (app.decision === "approved")
        return {
            label: app.merged ? "Merged" : "Approved",
            cls: "bg-[#34C759]/15 text-[#34C759]",
        };
    if (app.decision === "rejected")
        return { label: "Rejected", cls: "bg-[#FF3B30]/15 text-[#FF3B30]" };
    return { label: "Pending", cls: "bg-[#FFCC00]/15 text-[#FFCC00]" };
}

function ReviewModal({ app, onClose, onDecide }) {
    const fd = app.form_data || {};
    const media = app.media || [];
    const intro = media.find((m) => m.category === "intro_video");
    const images = media.filter((m) => m.category === "image");
    const badge = getStatusBadge(app);

    return (
        <div
            className="fixed inset-0 z-50 bg-black/95 backdrop-blur-xl overflow-y-auto"
            data-testid="application-review-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
            >
                <X className="w-4 h-4" />
            </button>

            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14">
                <div className="flex items-center gap-3 mb-3">
                    <p className="eyebrow">Application</p>
                    <span
                        className={`px-2 py-0.5 text-[10px] tg-mono uppercase rounded-sm ${badge.cls}`}
                    >
                        {badge.label}
                    </span>
                </div>
                <h2 className="font-display text-3xl md:text-5xl tracking-tight mb-3">
                    {app.talent_name}
                </h2>
                <div className="flex items-center gap-5 flex-wrap text-sm text-white/60 tg-mono mb-10">
                    <span className="inline-flex items-center gap-1.5">
                        <Mail className="w-3.5 h-3.5" /> {app.talent_email}
                    </span>
                    {app.talent_phone && (
                        <span className="inline-flex items-center gap-1.5">
                            <Phone className="w-3.5 h-3.5" /> {app.talent_phone}
                        </span>
                    )}
                </div>

                {/* Profile details */}
                <section className="mb-10 border border-white/10 p-5 md:p-6 grid md:grid-cols-3 gap-6">
                    <Field label="Age" value={fd.dob ? calcAge(fd.dob) : null} />
                    <Field label="Height" value={fd.height} />
                    <Field label="Gender" value={fd.gender} />
                    <Field label="Location" value={fd.location} />
                    <Field label="Instagram" value={fd.instagram_handle} />
                    <Field label="Followers" value={fd.instagram_followers} />
                    {fd.bio && (
                        <div className="md:col-span-3 border-t border-white/10 pt-4">
                            <p className="text-[11px] tracking-widest uppercase text-white/40 mb-2">
                                Bio
                            </p>
                            <p className="text-sm text-white/80 whitespace-pre-wrap leading-relaxed">
                                {fd.bio}
                            </p>
                        </div>
                    )}
                </section>

                {/* Intro video */}
                {intro ? (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <video
                            src={intro.url}
                            controls
                            preload="metadata"
                            className="w-full max-w-3xl border border-white/10 bg-black rounded-sm"
                            data-testid="review-intro-video"
                        />
                    </section>
                ) : (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">Introduction Video</p>
                        <div className="max-w-3xl border border-dashed border-white/10 bg-black/40 aspect-video flex items-center justify-center text-white/30 text-xs tg-mono rounded-sm">
                            Not submitted
                        </div>
                    </section>
                )}

                {/* Images */}
                {images.length > 0 && (
                    <section className="mb-10">
                        <p className="eyebrow mb-3">
                            Images ({images.length})
                        </p>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {images.map((m) => (
                                <a
                                    key={m.id}
                                    href={m.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden border border-white/10"
                                >
                                    <img
                                        src={m.url}
                                        alt=""
                                        loading="lazy"
                                        className="w-full h-full object-cover"
                                    />
                                </a>
                            ))}
                        </div>
                    </section>
                )}

                {/* Sticky decision bar */}
                {app.status === "submitted" &&
                    app.decision === "pending" && (
                        <div className="sticky bottom-4 flex gap-2 justify-end">
                            <button
                                onClick={() => onDecide("approved")}
                                data-testid="review-approve-btn"
                                className="inline-flex items-center gap-2 px-5 py-2.5 bg-[#34C759] text-black rounded-sm text-xs font-medium hover:opacity-90"
                            >
                                <Check className="w-3.5 h-3.5" /> Approve
                            </button>
                            <button
                                onClick={() => onDecide("rejected")}
                                data-testid="review-reject-btn"
                                className="inline-flex items-center gap-2 px-5 py-2.5 border border-[#FF3B30]/60 text-[#FF3B30] hover:bg-[#FF3B30]/10 rounded-sm text-xs"
                            >
                                <XCircle className="w-3.5 h-3.5" /> Reject
                            </button>
                        </div>
                    )}
            </div>
        </div>
    );
}

function Field({ label, value }) {
    return (
        <div>
            <p className="text-[11px] tracking-widest uppercase text-white/40 mb-1.5">
                {label}
            </p>
            <p className="text-sm text-white/90">{value || "—"}</p>
        </div>
    );
}

function calcAge(dob) {
    if (!dob) return null;
    const [y, m, d] = dob.split("-").map((n) => parseInt(n, 10));
    if (!y || !m || !d) return null;
    const today = new Date();
    let age = today.getFullYear() - y;
    if (today.getMonth() + 1 < m || (today.getMonth() + 1 === m && today.getDate() < d))
        age -= 1;
    return age >= 0 && age <= 120 ? age : null;
}
