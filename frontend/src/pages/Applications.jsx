import React, { useEffect, useMemo, useState } from "react";
import { adminApi } from "@/lib/api";
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
            className="min-h-screen bg-[#f5f5f2] p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="applications-page"
        >
            <div className="mb-8 flex items-start justify-between flex-wrap gap-4">
                <div>
                    <p className="text-xs font-medium text-black/45 uppercase tracking-wide mb-2">
                        Talent Applications
                    </p>
                    <h1 className="text-3xl md:text-4xl font-semibold tracking-tight text-black/85">
                        Applicants
                    </h1>
                    <p className="text-black/50 text-sm mt-2 max-w-xl">
                        Open signups via <span className="font-mono">/apply</span>.
                        Review, approve, or reject — approved talents land in
                        the master DB.
                    </p>
                </div>
                <a
                    href="/apply"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium border border-black/[0.08] rounded-lg bg-white text-black/80 hover:bg-black/[0.02] transition-colors duration-150"
                >
                    <UserPlus className="w-4 h-4" /> Open /apply
                </a>
            </div>

            {/* Filter chips */}
            <div className="flex items-center gap-2 flex-wrap mb-6">
                <Filter className="w-4 h-4 text-black/45" />
                {STATUS_FILTERS.map((f) => (
                    <button
                        key={f.key}
                        onClick={() => setFilter(f.key)}
                        data-testid={`filter-${f.key}`}
                        className={`px-3 py-1.5 rounded-full border text-xs font-medium transition-colors duration-150 ${
                            filter === f.key
                                ? "border-black/20 bg-black/5 text-black/85"
                                : "border-black/[0.08] text-black/60 hover:bg-black/[0.02] hover:border-black/15"
                        }`}
                    >
                        {f.label}
                    </button>
                ))}
            </div>

            {loading ? (
                <div className="py-20 flex justify-center">
                    <Loader2 className="w-6 h-6 animate-spin text-black/40" />
                </div>
            ) : items.length === 0 ? (
                <div className="border border-dashed border-black/[0.08] rounded-xl bg-white py-20 text-center text-black/45 text-sm">
                    No applications match this filter.
                </div>
            ) : (
                <div
                    className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5"
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
    const cover = imgs[0];
    const badge = getStatusBadge(app);
    return (
        <div
            className="bg-white border border-black/[0.08] rounded-xl overflow-hidden transition-colors duration-150 hover:bg-black/[0.01] flex flex-col"
            data-testid={`app-card-${app.id}`}
        >
            <div className="relative aspect-[4/5] bg-[#fafaf8] overflow-hidden">
                {cover ? (
                    <img
                        src={cover.url}
                        alt={app.talent_name}
                        loading="lazy"
                        className="w-full h-full object-cover"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-black/30">
                        <ImageIcon className="w-8 h-8" />
                    </div>
                )}
                <span
                    className={`absolute top-3 left-3 px-2 py-0.5 text-[10px] font-mono uppercase rounded-md ${badge.cls}`}
                >
                    {badge.label}
                </span>
                {imgs.length > 0 && (
                    <span className="absolute bottom-3 right-3 px-2 py-0.5 bg-white/80 backdrop-blur-sm text-black/60 text-[10px] font-mono rounded-md">
                        {imgs.length} img
                    </span>
                )}
            </div>

            <div className="p-4 flex-1 flex flex-col">
                <h3 className="font-semibold text-lg text-black/85 truncate">
                    {app.talent_name}
                </h3>
                <div className="mt-2 space-y-1 text-xs text-black/60 min-w-0">
                    <div className="flex items-center gap-1.5 truncate">
                        <Mail className="w-3 h-3 shrink-0 text-black/45" />
                        <span className="truncate">{app.talent_email}</span>
                    </div>
                    {fd.location && (
                        <div className="flex items-center gap-1.5 truncate">
                            <MapPin className="w-3 h-3 shrink-0 text-black/45" />
                            <span>{fd.location}</span>
                        </div>
                    )}
                    {fd.instagram_handle && (
                        <div className="flex items-center gap-1.5 truncate">
                            <Instagram className="w-3 h-3 shrink-0 text-black/45" />
                            <span>{fd.instagram_handle}</span>
                        </div>
                    )}
                </div>

                <div className="mt-4 flex items-center gap-2">
                    <button
                        onClick={onReview}
                        data-testid={`review-${app.id}`}
                        className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium border border-black/[0.08] rounded-lg bg-white text-black/80 hover:bg-black/[0.02] transition-colors duration-150"
                    >
                        <Eye className="w-3.5 h-3.5" /> Review
                    </button>
                    {app.status === "submitted" && app.decision === "pending" && (
                        <>
                            <button
                                onClick={() => onDecide("approved")}
                                data-testid={`approve-${app.id}`}
                                title="Approve"
                                className="w-9 h-9 inline-flex items-center justify-center rounded-lg bg-black/5 border border-black/[0.08] text-black/70 hover:bg-black/10 hover:text-black/90 transition-colors duration-150"
                            >
                                <Check className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => onDecide("rejected")}
                                data-testid={`reject-${app.id}`}
                                title="Reject"
                                className="w-9 h-9 inline-flex items-center justify-center rounded-lg border border-red-500/20 text-red-600/80 hover:bg-red-50 hover:text-red-700 transition-colors duration-150"
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
        return { label: "Draft", cls: "bg-black/5 text-black/50" };
    if (app.decision === "approved")
        return {
            label: app.merged ? "Merged" : "Approved",
            cls: "bg-emerald-50 text-emerald-700",
        };
    if (app.decision === "rejected")
        return { label: "Rejected", cls: "bg-red-50 text-red-700" };
    return { label: "Pending", cls: "bg-amber-50 text-amber-700" };
}

function ReviewModal({ app, onClose, onDecide }) {
    const fd = app.form_data || {};
    const media = app.media || [];
    const intro = media.find((m) => m.category === "intro_video");
    const images = media.filter((m) => m.category === "image");
    const badge = getStatusBadge(app);

    return (
        <div
            className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm overflow-y-auto"
            data-testid="application-review-modal"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-10 w-10 h-10 border border-black/[0.08] rounded-lg flex items-center justify-center bg-white text-black/70 hover:bg-black/[0.02] transition-colors duration-150"
            >
                <X className="w-4 h-4" />
            </button>

            <div className="max-w-5xl mx-auto px-5 md:px-12 py-10 md:py-14">
                <div className="bg-white rounded-xl border border-black/[0.08] shadow-sm p-6 md:p-8">
                    <div className="flex items-center gap-3 mb-3">
                        <p className="text-xs font-medium text-black/45 uppercase tracking-wide">
                            Application
                        </p>
                        <span
                            className={`px-2 py-0.5 text-[10px] font-mono uppercase rounded-md ${badge.cls}`}
                        >
                            {badge.label}
                        </span>
                    </div>
                    <h2 className="text-3xl md:text-4xl font-semibold tracking-tight text-black/85 mb-3">
                        {app.talent_name}
                    </h2>
                    <div className="flex items-center gap-5 flex-wrap text-sm text-black/60 mb-6">
                        <span className="inline-flex items-center gap-1.5">
                            <Mail className="w-3.5 h-3.5 text-black/45" /> {app.talent_email}
                        </span>
                        {app.talent_phone && (
                            <span className="inline-flex items-center gap-1.5">
                                <Phone className="w-3.5 h-3.5 text-black/45" /> {app.talent_phone}
                            </span>
                        )}
                    </div>

                    {/* Profile details */}
                    <section className="mb-8 border border-black/[0.08] rounded-lg p-5 bg-[#fafaf8] grid md:grid-cols-3 gap-6">
                        <Field label="Age" value={fd.dob ? calcAge(fd.dob) : null} />
                        <Field label="Height" value={fd.height} />
                        <Field label="Gender" value={fd.gender} />
                        <Field label="Location" value={fd.location} />
                        <Field label="Instagram" value={fd.instagram_handle} />
                        <Field label="Followers" value={fd.instagram_followers} />
                        {fd.bio && (
                            <div className="md:col-span-3 border-t border-black/[0.08] pt-4">
                                <p className="text-xs font-medium text-black/45 uppercase tracking-wide mb-2">
                                    Bio
                                </p>
                                <p className="text-sm text-black/80 whitespace-pre-wrap leading-relaxed">
                                    {fd.bio}
                                </p>
                            </div>
                        )}
                    </section>

                    {/* Intro video */}
                    <section className="mb-8">
                        <p className="text-xs font-medium text-black/45 uppercase tracking-wide mb-3">
                            Introduction Video
                        </p>
                        {intro ? (
                            <video
                                src={intro.url}
                                controls
                                preload="metadata"
                                className="w-full max-w-3xl border border-black/[0.08] rounded-lg bg-black/5"
                                data-testid="review-intro-video"
                            />
                        ) : (
                            <div className="max-w-3xl border border-dashed border-black/[0.08] rounded-lg bg-[#fafaf8] aspect-video flex items-center justify-center text-black/40 text-xs font-mono">
                                Not submitted
                            </div>
                        )}
                    </section>

                    {/* Images */}
                    {images.length > 0 && (
                        <section className="mb-8">
                            <p className="text-xs font-medium text-black/45 uppercase tracking-wide mb-3">
                                Images ({images.length})
                            </p>
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                                {images.map((m) => (
                                    <a
                                        key={m.id}
                                        href={m.url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="aspect-[3/4] bg-[#fafaf8] overflow-hidden border border-black/[0.08] rounded-lg"
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
                            <div className="sticky bottom-4 flex gap-3 justify-end mt-8 pt-4 border-t border-black/[0.08]">
                                <button
                                    onClick={() => onDecide("approved")}
                                    data-testid="review-approve-btn"
                                    className="inline-flex items-center gap-2 px-5 py-2.5 bg-black/5 border border-black/[0.08] rounded-lg text-sm font-medium text-black/80 hover:bg-black/10 transition-colors duration-150"
                                >
                                    <Check className="w-3.5 h-3.5" /> Approve
                                </button>
                                <button
                                    onClick={() => onDecide("rejected")}
                                    data-testid="review-reject-btn"
                                    className="inline-flex items-center gap-2 px-5 py-2.5 border border-red-500/30 rounded-lg text-sm font-medium text-red-600/80 hover:bg-red-50 hover:text-red-700 transition-colors duration-150"
                                >
                                    <XCircle className="w-3.5 h-3.5" /> Reject
                                </button>
                            </div>
                        )}
                </div>
            </div>
        </div>
    );
}

function Field({ label, value }) {
    return (
        <div>
            <p className="text-xs font-medium text-black/45 uppercase tracking-wide mb-1.5">
                {label}
            </p>
            <p className="text-sm text-black/80">{value || "—"}</p>
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
