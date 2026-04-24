import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { toast } from "sonner";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import {
    ExternalLink,
    Copy,
    Trash2,
    MessageCircle,
    Plus,
    Files,
} from "lucide-react";

export default function LinkHistory() {
    const [links, setLinks] = useState([]);
    const [loading, setLoading] = useState(true);
    const [pendingDelete, setPendingDelete] = useState(null);
    const canDelete = isAdmin();
    const canCreate = isAdmin();

    const load = async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/links");
            setLinks(data);
        } finally {
            setLoading(false);
        }
    };
    useEffect(() => {
        load();
    }, []);

    const copyLink = (slug) => {
        const url = `${window.location.origin}/l/${slug}`;
        navigator.clipboard.writeText(url);
        toast.success("Link copied");
    };

    const shareWhatsApp = (l) => {
        const url = `${window.location.origin}/l/${l.slug}`;
        const msg = encodeURIComponent(
            `${l.title}\n\nCurated portfolio review — ${url}`,
        );
        window.open(`https://wa.me/?text=${msg}`, "_blank");
    };

    const duplicate = async (id) => {
        await adminApi.post(`/links/${id}/duplicate`);
        toast.success("Duplicated");
        load();
    };

    const del = async (id) => {
        // Called from the confirm modal's onConfirm.
        try {
            const res = await adminApi.delete(`/links/${id}`);
             
            console.info("[delete link]", id, res?.data);
            toast.success("Link deleted");
            setPendingDelete(null);
            load();
        } catch (err) {
             
            console.error("[delete link] failed", err?.response?.data || err);
            toast.error(
                err?.response?.data?.detail ||
                    err?.message ||
                    "Delete failed — check console for details",
            );
            throw err; // keep modal open
        }
    };

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="link-history-page"
        >
            <div className="flex items-end justify-between flex-wrap gap-4 mb-10">
                <div>
                    <p className="eyebrow mb-3">History</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Generated Links
                    </h1>
                </div>
                <Link
                    to="/admin/links/new"
                    data-testid="new-link-btn"
                    className={`inline-flex items-center gap-2 bg-white text-black px-5 py-3 rounded-sm text-xs tracking-wide hover:opacity-90 ${canCreate ? "" : "pointer-events-none opacity-40"}`}
                    aria-disabled={!canCreate}
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> Generate Link
                </Link>
            </div>

            {loading ? (
                <div className="text-white/40 text-sm">Loading...</div>
            ) : links.length === 0 ? (
                <div className="border border-white/10 p-12 text-center">
                    <Files
                        className="w-10 h-10 text-white/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-white/60 mb-6">No links generated yet</p>
                    <Link
                        to="/admin/links/new"
                        className="inline-flex items-center gap-2 bg-white text-black px-5 py-2.5 rounded-sm text-xs"
                    >
                        Generate your first link
                    </Link>
                </div>
            ) : (
                <div className="border border-white/10">
                    <div className="hidden md:grid grid-cols-12 gap-4 px-6 py-3 border-b border-white/10 text-[10px] tracking-widest uppercase text-white/40">
                        <div className="col-span-5">Title</div>
                        <div className="col-span-2">Talents</div>
                        <div className="col-span-1">Views</div>
                        <div className="col-span-1">Unique</div>
                        <div className="col-span-3 text-right">Actions</div>
                    </div>
                    <div className="divide-y divide-white/10">
                        {links.map((l) => (
                            <div
                                key={l.id}
                                data-testid={`link-row-${l.id}`}
                                className="grid md:grid-cols-12 gap-4 items-center px-6 py-5 hover:bg-white/[0.02]"
                            >
                                <div className="md:col-span-5">
                                    <Link
                                        to={`/admin/links/${l.id}/results`}
                                        className="font-display text-xl hover:text-white"
                                    >
                                        {l.title}
                                    </Link>
                                    <div className="flex items-center gap-2 mt-1">
                                        <div className="text-[11px] text-white/40 tg-mono truncate">
                                            /l/{l.slug}
                                        </div>
                                        {(() => {
                                            const hasSubs = (l.submission_ids || []).length > 0;
                                            const hasTalents = (l.talent_ids || []).length > 0;
                                            const label = hasSubs && hasTalents ? "Mixed"
                                                : hasSubs ? "Audition"
                                                : "Manual";
                                            const cls = hasSubs && hasTalents ? "border-white/30 text-white/70"
                                                : hasSubs ? "border-[#34C759]/40 text-[#34C759]"
                                                : "border-white/15 text-white/50";
                                            return (
                                                <span
                                                    data-testid={`link-source-${l.id}`}
                                                    className={`text-[9px] tracking-widest uppercase border px-2 py-0.5 rounded-sm ${cls}`}
                                                    title="Curation source: Audition = submission-driven, Manual = picked from talent DB"
                                                >
                                                    {label}
                                                </span>
                                            );
                                        })()}
                                    </div>
                                </div>
                                <div className="md:col-span-2 text-sm text-white/70">
                                    {(l.talent_ids || []).length + (l.submission_ids || []).length}
                                </div>
                                <div className="md:col-span-1 text-sm">
                                    {l.view_count || 0}
                                </div>
                                <div className="md:col-span-1 text-sm">
                                    {l.unique_viewers || 0}
                                </div>
                                <div className="md:col-span-3 flex items-center justify-end gap-1 flex-wrap">
                                    <a
                                        href={`/l/${l.slug}`}
                                        target="_blank"
                                        rel="noreferrer"
                                        title="Open"
                                        className="p-2 hover:bg-white/10 rounded-sm"
                                    >
                                        <ExternalLink className="w-3.5 h-3.5" />
                                    </a>
                                    <button
                                        onClick={() => copyLink(l.slug)}
                                        title="Copy"
                                        className="p-2 hover:bg-white/10 rounded-sm"
                                    >
                                        <Copy className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        onClick={() => shareWhatsApp(l)}
                                        title="WhatsApp"
                                        data-testid={`whatsapp-share-${l.id}`}
                                        className="p-2 hover:bg-white/10 rounded-sm"
                                    >
                                        <MessageCircle className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        onClick={() => duplicate(l.id)}
                                        title="Duplicate"
                                        className={`p-2 hover:bg-white/10 rounded-sm ${canCreate ? "" : "hidden"}`}
                                    >
                                        <Files className="w-3.5 h-3.5" />
                                    </button>
                                    {canDelete && (
                                        <button
                                            onClick={() =>
                                                setPendingDelete({
                                                    id: l.id,
                                                    title: l.title,
                                                })
                                            }
                                            title="Delete"
                                            data-testid={`delete-link-${l.id}`}
                                            className="p-2 hover:bg-white/10 hover:text-[var(--tg-danger)] rounded-sm"
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}
            <ConfirmDeleteDialog
                open={!!pendingDelete}
                title={`Delete "${pendingDelete?.title || "this link"}"?`}
                description="This permanently removes the link and all associated views, actions, downloads and client comments. This cannot be undone."
                confirmLabel="Delete link"
                onCancel={() => setPendingDelete(null)}
                onConfirm={() => del(pendingDelete.id)}
            />
        </div>
    );
}
