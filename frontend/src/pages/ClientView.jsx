import React, { useEffect, useState, useMemo } from "react";
import { useParams } from "react-router-dom";
import { viewerApi, FILE_URL, getViewerToken, saveViewerToken } from "@/lib/api";
import ThemeToggle from "@/components/ThemeToggle";
import axios from "axios";
import { toast } from "sonner";
import {
    Instagram,
    ExternalLink,
    Star,
    ThumbsUp,
    XCircle,
    HelpCircle,
    ChevronLeft,
    ChevronRight,
    X,
    Download,
    Play,
    Sparkles,
    Loader2,
    MessageSquare,
} from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const ACTIONS = [
    { key: "shortlist", label: "Shortlist", icon: Star, color: "#FFCC00" },
    { key: "interested", label: "Interested", icon: ThumbsUp, color: "#34C759" },
    { key: "not_for_this", label: "Not for this", icon: XCircle, color: "#FF3B30" },
    { key: "not_sure", label: "Not sure", icon: HelpCircle, color: "#9CA3AF" },
];

export default function ClientView() {
    const { slug } = useParams();
    const [identified, setIdentified] = useState(!!getViewerToken(slug));
    const [name, setName] = useState("");
    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);

    const [data, setData] = useState(null);
    const [activeTalent, setActiveTalent] = useState(null);
    const [commentDrafts, setCommentDrafts] = useState({});

    const loadData = async () => {
        try {
            const { data } = await axios.get(`${API}/public/links/${slug}`, {
                headers: {
                    Authorization: `Bearer ${getViewerToken(slug)}`,
                },
            });
            setData(data);
        } catch (e) {
            if (e?.response?.status === 401) {
                setIdentified(false);
            } else {
                toast.error("Failed to load portfolio");
            }
        }
    };

    useEffect(() => {
        if (identified) loadData();
    }, [identified]);

    const identify = async (e) => {
        e.preventDefault();
        setLoading(true);
        try {
            const { data } = await axios.post(
                `${API}/public/links/${slug}/identify`,
                { name, email },
            );
            saveViewerToken(slug, data.token);
            setIdentified(true);
            toast.success(`Welcome, ${name.split(" ")[0]}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to continue");
        } finally {
            setLoading(false);
        }
    };

    const viewerActions = useMemo(() => {
        const m = {};
        (data?.actions || []).forEach((a) => (m[a.talent_id] = a));
        return m;
    }, [data]);

    const setAction = async (talentId, action) => {
        try {
            await axios.post(
                `${API}/public/links/${slug}/action`,
                { talent_id: talentId, action },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            await loadData();
        } catch {
            toast.error("Action failed");
        }
    };

    const saveComment = async (talentId) => {
        const text = commentDrafts[talentId];
        if (text === undefined) return;
        try {
            const existing = viewerActions[talentId];
            await axios.post(
                `${API}/public/links/${slug}/action`,
                {
                    talent_id: talentId,
                    action: existing?.action || null,
                    comment: text,
                },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
            toast.success("Comment saved");
            await loadData();
        } catch {
            toast.error("Failed to save");
        }
    };

    const logDownload = async (talentId, mediaId) => {
        try {
            await axios.post(
                `${API}/public/links/${slug}/download-log`,
                { talent_id: talentId, media_id: mediaId },
                {
                    headers: {
                        Authorization: `Bearer ${getViewerToken(slug)}`,
                    },
                },
            );
        } catch {}
    };

    // ---------------- Identity Gate ----------------
    if (!identified) {
        return (
            <div className="min-h-screen bg-[#050505] relative">
                <div className="absolute top-5 right-5 z-20">
                    <ThemeToggle />
                </div>
                <div
                    className="absolute inset-0 opacity-30"
                    style={{
                        backgroundImage:
                            "url('https://images.pexels.com/photos/15128321/pexels-photo-15128321.jpeg')",
                        backgroundSize: "cover",
                        backgroundPosition: "center",
                    }}
                />
                <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-black/80 to-black" />
                <div className="relative z-10 min-h-screen flex items-center justify-center px-6">
                    <form
                        onSubmit={identify}
                        className="w-full max-w-md bg-black/60 backdrop-blur-xl border border-white/10 p-8 md:p-10 tg-fade-up"
                        data-testid="identity-gate-form"
                    >
                        <div className="flex items-center gap-2 mb-8">
                            <div className="w-6 h-6 rounded-sm bg-white flex items-center justify-center">
                                <Sparkles
                                    className="w-3.5 h-3.5 text-black"
                                    strokeWidth={1.5}
                                />
                            </div>
                            <span className="font-display tracking-tight">
                                Talentgram
                            </span>
                        </div>
                        <p className="eyebrow mb-3">Curated Portfolio</p>
                        <h1 className="font-display text-3xl tracking-tight mb-3">
                            A private review awaits you.
                        </h1>
                        <p className="text-white/50 text-sm mb-8">
                            Please share your name and email to continue. This
                            helps us follow up on your selections.
                        </p>
                        <label className="block mb-4">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Your Name
                            </span>
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                required
                                data-testid="identity-name-input"
                                className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>
                        <label className="block mb-8">
                            <span className="text-[11px] text-white/50 tracking-widest uppercase">
                                Email
                            </span>
                            <input
                                type="email"
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                                required
                                data-testid="identity-email-input"
                                className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>
                        <button
                            type="submit"
                            disabled={loading}
                            data-testid="identity-submit-btn"
                            className="w-full bg-white text-black py-3.5 rounded-sm text-sm font-medium hover:opacity-90 inline-flex items-center justify-center gap-2"
                        >
                            {loading && (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            )}
                            Enter Review
                        </button>
                    </form>
                </div>
            </div>
        );
    }

    if (!data) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] text-white/40">
                <Loader2 className="w-6 h-6 animate-spin" />
            </div>
        );
    }

    const { link, talents, viewer } = data;
    const vis = link.visibility || {};

    return (
        <div className="min-h-screen bg-[#050505] text-white" data-testid="client-view-page">
            {/* Header */}
            <header className="sticky top-0 z-30 bg-black/80 backdrop-blur-xl border-b border-white/10">
                <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-5 flex items-center justify-between gap-4">
                    <div>
                        <p className="eyebrow">Curated Review</p>
                        <h1 className="font-display text-xl md:text-2xl tracking-tight mt-1">
                            {link.title}
                        </h1>
                    </div>
                    <div className="text-right">
                        <p className="text-xs text-white/50">Viewing as</p>
                        <p className="text-sm font-medium">{viewer.name}</p>
                    </div>
                    <ThemeToggle />
                </div>
            </header>

            {/* Grid */}
            <div className="max-w-[1600px] mx-auto px-6 md:px-12 py-10 md:py-16">
                <div className="mb-10 flex items-center justify-between flex-wrap gap-3">
                    <div>
                        <p className="eyebrow mb-2">{talents.length} Talents</p>
                        <h2 className="font-display text-3xl md:text-5xl tracking-tight">
                            Pick your winners.
                        </h2>
                    </div>
                    <p className="text-xs text-white/40 max-w-sm">
                        Tap any card to view the full portfolio. Actions and
                        comments are saved instantly.
                    </p>
                </div>

                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6"
                    data-testid="client-talents-grid"
                >
                    {talents.map((t, i) => {
                        const cover =
                            (t.media || []).find(
                                (m) => m.id === t.cover_media_id,
                            ) ||
                            (t.media || []).find((m) =>
                                m.content_type?.startsWith("image/"),
                            );
                        const act = viewerActions[t.id]?.action;
                        return (
                            <button
                                key={t.id}
                                onClick={() => setActiveTalent(t)}
                                data-testid={`client-talent-${t.id}`}
                                style={{ animationDelay: `${i * 40}ms` }}
                                className="group relative text-left tg-fade-up"
                            >
                                <div className="aspect-[3/4] bg-[#0a0a0a] overflow-hidden border border-white/10 group-hover:border-white/30 transition-all">
                                    {cover ? (
                                        <img
                                            src={FILE_URL(cover.storage_path)}
                                            alt={t.name}
                                            loading="lazy"
                                            className="w-full h-full object-cover group-hover:scale-[1.03] transition-all duration-700"
                                        />
                                    ) : (
                                        <div className="w-full h-full flex items-center justify-center text-white/20">
                                            <Sparkles className="w-8 h-8" />
                                        </div>
                                    )}
                                    <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black via-black/40 to-transparent p-4">
                                        <div className="font-display text-lg md:text-xl tracking-tight">
                                            {t.name}
                                        </div>
                                        <div className="text-[11px] text-white/50 tg-mono mt-1">
                                            {vis.location && t.location
                                                ? t.location
                                                : ""}
                                        </div>
                                    </div>
                                    {act && (
                                        <div className="absolute top-2 left-2 bg-white text-black px-2 py-1 text-[10px] tracking-widest uppercase">
                                            {
                                                ACTIONS.find(
                                                    (a) => a.key === act,
                                                )?.label
                                            }
                                        </div>
                                    )}
                                </div>
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* Detail Overlay */}
            {activeTalent && (
                <TalentDetail
                    talent={activeTalent}
                    link={link}
                    slug={slug}
                    viewerAction={viewerActions[activeTalent.id]}
                    onClose={() => setActiveTalent(null)}
                    setAction={setAction}
                    commentDraft={
                        commentDrafts[activeTalent.id] ??
                        viewerActions[activeTalent.id]?.comment ??
                        ""
                    }
                    setCommentDraft={(text) =>
                        setCommentDrafts({
                            ...commentDrafts,
                            [activeTalent.id]: text,
                        })
                    }
                    saveComment={() => saveComment(activeTalent.id)}
                    logDownload={logDownload}
                />
            )}
        </div>
    );
}

function TalentDetail({
    talent,
    link,
    slug,
    viewerAction,
    onClose,
    setAction,
    commentDraft,
    setCommentDraft,
    saveComment,
    logDownload,
}) {
    const vis = link.visibility || {};
    const images = (talent.media || []).filter((m) =>
        m.content_type?.startsWith("image/"),
    );
    const videos = (talent.media || []).filter((m) =>
        m.content_type?.startsWith("video/"),
    );
    const [idx, setIdx] = useState(0);

    const prev = () => setIdx((i) => (i - 1 + images.length) % images.length);
    const next = () => setIdx((i) => (i + 1) % images.length);

    const download = async (m) => {
        await logDownload(talent.id, m.id);
        const url = FILE_URL(m.storage_path);
        const a = document.createElement("a");
        a.href = url;
        a.download = m.original_filename || "file";
        a.target = "_blank";
        a.click();
    };

    return (
        <div
            className="fixed inset-0 z-50 bg-black/95 backdrop-blur-2xl overflow-y-auto"
            data-testid="talent-detail-overlay"
        >
            <button
                onClick={onClose}
                className="fixed top-5 right-5 z-50 w-10 h-10 border border-white/20 hover:border-white rounded-sm flex items-center justify-center bg-black/50"
                data-testid="detail-close-btn"
            >
                <X className="w-4 h-4" />
            </button>

            <div className="max-w-[1400px] mx-auto px-6 md:px-12 py-12">
                <div className="grid lg:grid-cols-5 gap-8 lg:gap-12">
                    {/* Main slider */}
                    <div className="lg:col-span-3">
                        {images.length > 0 ? (
                            <div className="relative bg-[#0a0a0a] aspect-[3/4] border border-white/10 overflow-hidden">
                                <img
                                    src={FILE_URL(images[idx].storage_path)}
                                    alt={talent.name}
                                    className="w-full h-full object-contain"
                                />
                                {images.length > 1 && (
                                    <>
                                        <button
                                            onClick={prev}
                                            className="absolute left-2 top-1/2 -translate-y-1/2 w-10 h-10 bg-black/50 border border-white/20 hover:bg-black flex items-center justify-center"
                                        >
                                            <ChevronLeft className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={next}
                                            className="absolute right-2 top-1/2 -translate-y-1/2 w-10 h-10 bg-black/50 border border-white/20 hover:bg-black flex items-center justify-center"
                                        >
                                            <ChevronRight className="w-4 h-4" />
                                        </button>
                                        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 px-3 py-1 bg-black/60 border border-white/15 text-[10px] tg-mono">
                                            {idx + 1} / {images.length}
                                        </div>
                                    </>
                                )}
                                {vis.download && (
                                    <button
                                        onClick={() => download(images[idx])}
                                        className="absolute top-2 right-2 w-9 h-9 bg-black/60 border border-white/20 hover:bg-white hover:text-black flex items-center justify-center"
                                        data-testid="detail-download-btn"
                                    >
                                        <Download className="w-4 h-4" />
                                    </button>
                                )}
                            </div>
                        ) : (
                            <div className="aspect-[3/4] bg-[#0a0a0a] border border-white/10 flex items-center justify-center text-white/30">
                                No portfolio
                            </div>
                        )}

                        {/* Thumbs */}
                        {images.length > 1 && (
                            <div className="mt-3 flex gap-2 overflow-x-auto tg-scroll pb-2">
                                {images.map((m, i) => (
                                    <button
                                        key={m.id}
                                        onClick={() => setIdx(i)}
                                        className={`shrink-0 w-16 h-20 border ${i === idx ? "border-white" : "border-white/10"}`}
                                    >
                                        <img
                                            src={FILE_URL(m.storage_path)}
                                            alt=""
                                            className="w-full h-full object-cover"
                                        />
                                    </button>
                                ))}
                            </div>
                        )}

                        {vis.intro_video && videos.length > 0 && (
                            <div className="mt-8">
                                <p className="eyebrow mb-3">Introduction</p>
                                <video
                                    src={FILE_URL(videos[0].storage_path)}
                                    controls
                                    className="w-full border border-white/10 bg-black"
                                />
                            </div>
                        )}
                    </div>

                    {/* Info */}
                    <div className="lg:col-span-2">
                        <p className="eyebrow mb-3">Talent</p>
                        <h2 className="font-display text-4xl md:text-5xl tracking-tight mb-6">
                            {talent.name}
                        </h2>

                        <div className="grid grid-cols-2 gap-y-5 mb-8 text-sm">
                            {vis.age && talent.age && (
                                <InfoRow label="Age" value={talent.age} />
                            )}
                            {vis.height && talent.height && (
                                <InfoRow label="Height" value={talent.height} />
                            )}
                            {vis.location && talent.location && (
                                <InfoRow label="Location" value={talent.location} />
                            )}
                            {vis.ethnicity && talent.ethnicity && (
                                <InfoRow label="Ethnicity" value={talent.ethnicity} />
                            )}
                            {vis.instagram_followers &&
                                talent.instagram_followers && (
                                    <InfoRow
                                        label="Followers"
                                        value={talent.instagram_followers}
                                    />
                                )}
                        </div>

                        <div className="flex gap-3 mb-8 flex-wrap">
                            {vis.instagram && talent.instagram_handle && (
                                <a
                                    href={`https://instagram.com/${talent.instagram_handle.replace("@", "")}`}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="inline-flex items-center gap-2 px-4 py-2.5 border border-white/20 hover:border-white rounded-sm text-xs"
                                >
                                    <Instagram className="w-3.5 h-3.5" />{" "}
                                    {talent.instagram_handle}
                                </a>
                            )}
                        </div>

                        {vis.work_links &&
                            (talent.work_links || []).length > 0 && (
                                <div className="mb-8">
                                    <p className="eyebrow mb-3">Work</p>
                                    <div className="space-y-2">
                                        {talent.work_links.map((w, i) => (
                                            <a
                                                key={i}
                                                href={w}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="flex items-center gap-2 text-sm text-white/70 hover:text-white tg-mono truncate"
                                            >
                                                <ExternalLink className="w-3 h-3 shrink-0" />
                                                <span className="truncate">
                                                    {w}
                                                </span>
                                            </a>
                                        ))}
                                    </div>
                                </div>
                            )}

                        {/* Actions */}
                        <div className="border-t border-white/10 pt-6 mt-6">
                            <p className="eyebrow mb-4">Your Decision</p>
                            <div className="grid grid-cols-2 gap-2 mb-6">
                                {ACTIONS.map((a) => {
                                    const active = viewerAction?.action === a.key;
                                    return (
                                        <button
                                            key={a.key}
                                            onClick={() =>
                                                setAction(
                                                    talent.id,
                                                    active ? null : a.key,
                                                )
                                            }
                                            data-testid={`action-${a.key}-${talent.id}`}
                                            className={`flex items-center gap-2 px-4 py-3 border rounded-sm text-sm transition-all ${active ? "bg-white text-black border-white" : "border-white/15 hover:border-white/40"}`}
                                        >
                                            <a.icon
                                                className="w-4 h-4"
                                                style={{
                                                    color: active
                                                        ? "#000"
                                                        : a.color,
                                                }}
                                            />
                                            {a.label}
                                        </button>
                                    );
                                })}
                            </div>

                            <div>
                                <div className="flex items-center gap-2 mb-2">
                                    <MessageSquare className="w-3.5 h-3.5 text-white/60" />
                                    <p className="eyebrow">Comment</p>
                                </div>
                                <textarea
                                    value={commentDraft}
                                    onChange={(e) =>
                                        setCommentDraft(e.target.value)
                                    }
                                    rows={3}
                                    placeholder="Share any notes about this talent..."
                                    data-testid="detail-comment-input"
                                    className="w-full bg-transparent border border-white/15 focus:border-white rounded-sm p-3 text-sm outline-none"
                                />
                                <button
                                    onClick={saveComment}
                                    data-testid="detail-save-comment-btn"
                                    className="mt-3 text-xs px-4 py-2 border border-white/20 hover:border-white rounded-sm"
                                >
                                    Save comment
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

function InfoRow({ label, value }) {
    return (
        <div>
            <div className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                {label}
            </div>
            <div className="text-sm font-medium">{value}</div>
        </div>
    );
}
