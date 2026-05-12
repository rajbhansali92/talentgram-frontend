import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { toast } from "sonner";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";
import BulkSelectBar from "@/components/BulkSelectBar";
import {
    ExternalLink,
    Copy,
    Trash2,
    MessageCircle,
    Plus,
    Files,
    Check,
} from "lucide-react";

export default function LinkHistory() {
    const [links, setLinks] = useState([]);
    const [loading, setLoading] = useState(true);
    const [pendingDelete, setPendingDelete] = useState(null);
    const [selected, setSelected] = useState(new Set());
    const [bulkConfirm, setBulkConfirm] = useState(false);
    const canDelete = isAdmin();
    const canCreate = isAdmin();

    const toggle = (id) =>
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    const clearSel = () => setSelected(new Set());
    const selectAll = () => setSelected(new Set(links.map((l) => l.id)));

    const bulkDelete = async () => {
        const ids = Array.from(selected);
        try {
            const res = await adminApi.post("/links/bulk-delete", { ids });
             
            console.info("[bulk-delete links]", ids, res?.data);
            toast.success(
                `Deleted ${res.data.deleted} link${res.data.deleted === 1 ? "" : "s"}`,
            );
            clearSel();
            setBulkConfirm(false);
            load();
        } catch (err) {
             
            console.error("[bulk-delete links] failed", err?.response?.data || err);
            toast.error(err?.response?.data?.detail || "Bulk delete failed");
            throw err;
        }
    };

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
            className="p-6 md:p-10 max-w-7xl mx-auto"
            data-testid="link-history-page"
        >
            <div className="flex items-end justify-between flex-wrap gap-4 mb-8">
                <div>
                    <p className="eyebrow mb-3">History</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        Generated Links
                    </h1>
                </div>
                <Link
                    to="/admin/links/new"
                    data-testid="new-link-btn"
                    className={`inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150 ${canCreate ? "" : "pointer-events-none opacity-40"}`}
                    aria-disabled={!canCreate}
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> Generate Link
                </Link>
            </div>

            {loading ? (
                <div className="text-black/45 text-sm">Loading...</div>
            ) : links.length === 0 ? (
                <div className="bg-white border border-black/[0.08] rounded-xl p-12 text-center">
                    <Files
                        className="w-10 h-10 text-black/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-black/60 mb-6">No links generated yet</p>
                    <Link
                        to="/admin/links/new"
                        className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                    >
                        Generate your first link
                    </Link>
                </div>
            ) : (
                <div className="bg-white border border-black/[0.08] rounded-xl overflow-hidden">
                    <div className="hidden md:grid grid-cols-12 gap-4 px-6 py-3 border-b border-black/[0.06] text-[10px] tracking-widest uppercase text-black/40">
                        <div className="col-span-5 flex items-center gap-3">
                            {canDelete && (
                                <button
                                    type="button"
                                    onClick={() =>
                                        selected.size === links.length
                                            ? clearSel()
                                            : selectAll()
                                    }
                                    aria-label="Toggle select all"
                                    data-testid="link-select-all"
                                    className={`w-4 h-4 rounded border flex items-center justify-center transition-colors duration-150 ${selected.size === links.length && links.length > 0 ? "bg-black border-black text-white" : "border-black/30"}`}
                                >
                                    {selected.size === links.length &&
                                        links.length > 0 && (
                                            <Check className="w-3 h-3" />
                                        )}
                                </button>
                            )}
                            Title
                        </div>
                        <div className="col-span-2">Talents</div>
                        <div className="col-span-1">Views</div>
                        <div className="col-span-1">Unique</div>
                        <div className="col-span-3 text-right">Actions</div>
                    </div>
                    <div className="divide-y divide-black/[0.06] pb-20">
                        {links.map((l) => (
                            <div
                                key={l.id}
                                data-testid={`link-row-${l.id}`}
                                className={`grid md:grid-cols-12 gap-4 items-center px-6 py-4 transition-colors duration-150 ${selected.has(l.id) ? "bg-black/[0.03]" : "hover:bg-black/[0.02]"}`}
                            >
                                <div className="md:col-span-5 flex items-start gap-3">
                                    {canDelete && (
                                        <button
                                            type="button"
                                            onClick={() => toggle(l.id)}
                                            aria-label={
                                                selected.has(l.id)
                                                    ? "Deselect"
                                                    : "Select"
                                            }
                                            data-testid={`link-check-${l.id}`}
                                            className={`mt-1 shrink-0 w-4 h-4 rounded border flex items-center justify-center transition-colors duration-150 ${selected.has(l.id) ? "bg-black border-black text-white" : "border-black/30 hover:border-black/60"}`}
                                        >
                                            {selected.has(l.id) && (
                                                <Check className="w-3 h-3" />
                                            )}
                                        </button>
                                    )}
                                    <div className="min-w-0">
                                    <Link
                                        to={`/admin/links/${l.id}/results`}
                                        className="font-display text-lg text-black/85 hover:text-black/60 transition-colors duration-150"
                                    >
                                        {l.title}
                                    </Link>
                                    <div className="flex items-center gap-2 mt-1">
                                        <div className="text-[11px] text-black/45 font-mono truncate">
                                            /l/{l.slug}
                                        </div>
                                        {(() => {
                                            const hasSubs = (l.submission_ids || []).length > 0;
                                            const hasTalents = (l.talent_ids || []).length > 0;
                                            const label = hasSubs && hasTalents ? "Mixed"
                                                : hasSubs ? "Audition"
                                                : "Manual";
                                            const cls = hasSubs && hasTalents ? "border-black/30 text-black/60"
                                                : hasSubs ? "border-green-600/40 text-green-700"
                                                : "border-black/20 text-black/50";
                                            return (
                                                <span
                                                    data-testid={`link-source-${l.id}`}
                                                    className={`text-[9px] tracking-widest uppercase border px-2 py-0.5 rounded-md ${cls}`}
                                                    title="Curation source: Audition = submission-driven, Manual = picked from talent DB"
                                                >
                                                    {label}
                                                </span>
                                            );
                                        })()}
                                    </div>
                                    </div>
                                </div>
                                <div className="md:col-span-2 text-sm text-black/70">
                                    {(l.talent_ids || []).length + (l.submission_ids || []).length}
                                </div>
                                <div className="md:col-span-1 text-sm text-black/70">
                                    {l.view_count || 0}
                                </div>
                                <div className="md:col-span-1 text-sm text-black/70">
                                    {l.unique_viewers || 0}
                                </div>
                                <div className="md:col-span-3 flex items-center justify-end gap-1 flex-wrap">
                                    <a
                                        href={`/l/${l.slug}`}
                                        target="_blank"
                                        rel="noreferrer"
                                        title="Open"
                                        className="p-2 text-black/50 hover:text-black/80 hover:bg-black/[0.04] rounded-md transition-colors duration-150"
                                    >
                                        <ExternalLink className="w-3.5 h-3.5" />
                                    </a>
                                    <button
                                        onClick={() => copyLink(l.slug)}
                                        title="Copy"
                                        className="p-2 text-black/50 hover:text-black/80 hover:bg-black/[0.04] rounded-md transition-colors duration-150"
                                    >
                                        <Copy className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        onClick={() => shareWhatsApp(l)}
                                        title="WhatsApp"
                                        data-testid={`whatsapp-share-${l.id}`}
                                        className="p-2 text-black/50 hover:text-black/80 hover:bg-black/[0.04] rounded-md transition-colors duration-150"
                                    >
                                        <MessageCircle className="w-3.5 h-3.5" />
                                    </button>
                                    <button
                                        onClick={() => duplicate(l.id)}
                                        title="Duplicate"
                                        className={`p-2 text-black/50 hover:text-black/80 hover:bg-black/[0.04] rounded-md transition-colors duration-150 ${canCreate ? "" : "hidden"}`}
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
                                            className="p-2 text-black/50 hover:text-red-600 hover:bg-red-50 rounded-md transition-colors duration-150"
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
            {canDelete && (
                <BulkSelectBar
                    count={selected.size}
                    total={links.length}
                    allSelected={selected.size === links.length}
                    onSelectAll={selectAll}
                    onClear={clearSel}
                    onDelete={() => setBulkConfirm(true)}
                    labelSingular="link"
                    labelPlural="links"
                    testid="links-bulk-bar"
                />
            )}
            <ConfirmDeleteDialog
                open={bulkConfirm}
                title={`Delete ${selected.size} link${selected.size === 1 ? "" : "s"}?`}
                description="This permanently removes the selected links and all their views, actions, downloads and client comments. This cannot be undone."
                confirmLabel={`Delete ${selected.size}`}
                typeToConfirm="DELETE"
                onCancel={() => setBulkConfirm(false)}
                onConfirm={bulkDelete}
                testid="links-bulk-confirm"
            />
        </div>
    );
}
