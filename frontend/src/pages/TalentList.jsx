import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, isAdmin } from "@/lib/api";
import { Search, Plus, Image as ImageIcon, Check } from "lucide-react";
import { toast } from "sonner";
import BulkSelectBar from "@/components/BulkSelectBar";
import ConfirmDeleteDialog from "@/components/ConfirmDeleteDialog";

export default function TalentList() {
    const [talents, setTalents] = useState([]);
    const [q, setQ] = useState("");
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState(new Set());
    const [confirmOpen, setConfirmOpen] = useState(false);
    const canBulkDelete = isAdmin();

    const load = async (qq = "") => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/talents", {
                params: qq ? { q: qq } : {},
            });
            setTalents(data);
        } finally {
            setLoading(false);
        }
    };
    useEffect(() => {
        load();
    }, []);
    useEffect(() => {
        const t = setTimeout(() => load(q), 250);
        return () => clearTimeout(t);
    }, [q]);

    const toggle = (id) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const clear = () => setSelected(new Set());
    const selectAll = () => setSelected(new Set(talents.map((t) => t.id)));

    const bulkDelete = async () => {
        const ids = Array.from(selected);
        try {
            const res = await adminApi.post("/talents/bulk-delete", { ids });
             
            console.info("[bulk-delete talents]", ids, res?.data);
            toast.success(
                `Deleted ${res.data.deleted} talent${res.data.deleted === 1 ? "" : "s"}${res.data.missing ? ` (${res.data.missing} already gone)` : ""}`,
            );
            clear();
            setConfirmOpen(false);
            load(q);
        } catch (err) {
             
            console.error("[bulk-delete talents] failed", err?.response?.data || err);
            toast.error(
                err?.response?.data?.detail ||
                    err?.message ||
                    "Bulk delete failed",
            );
            throw err;
        }
    };

    const isSelectionMode = selected.size > 0;

    return (
        <div
            className="p-6 md:p-10 max-w-7xl mx-auto"
            data-testid="talent-list-page"
        >
            <div className="flex items-end justify-between mb-8 flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Roster</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight text-black/90">
                        Talents
                    </h1>
                </div>
                <Link
                    to="/admin/talents/new"
                    data-testid="new-talent-btn"
                    className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> Add Talent
                </Link>
            </div>

            <div className="mb-8 relative max-w-md">
                <Search className="absolute left-0 top-3 w-4 h-4 text-black/35" />
                <input
                    type="text"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Search by name..."
                    data-testid="talent-search-input"
                    className="w-full bg-transparent border-b border-black/[0.08] focus:border-black/40 outline-none py-3 pl-7 text-sm text-black/85 placeholder:text-black/30"
                />
            </div>

            {loading ? (
                <div className="text-black/45 text-sm">Loading...</div>
            ) : talents.length === 0 ? (
                <div className="border border-black/[0.08] bg-white rounded-xl p-12 text-center">
                    <ImageIcon
                        className="w-10 h-10 text-black/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-black/60 mb-6">No talents yet</p>
                    <Link
                        to="/admin/talents/new"
                        className="inline-flex items-center gap-2 bg-black text-white px-5 py-2.5 rounded-lg text-xs font-medium hover:bg-black/90 transition-colors duration-150"
                    >
                        Add your first talent
                    </Link>
                </div>
            ) : (
                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6 pb-20"
                    data-testid="talents-grid"
                >
                    {talents.map((t) => {
                        const cover = (t.media || []).find(
                            (m) => m.id === t.cover_media_id,
                        );
                        const anyImg =
                            cover ||
                            (t.media || []).find(
                                (m) =>
                                    m.category !== "video" &&
                                    m.content_type?.startsWith("image/"),
                            );
                        const checked = selected.has(t.id);
                        const goesToDetail = !isSelectionMode;
                        return (
                            <div
                                key={t.id}
                                data-testid={`talent-card-${t.id}`}
                                className={`group relative border rounded-xl transition-colors duration-150 ${
                                    checked 
                                        ? "border-black/40 bg-white" 
                                        : "border-black/[0.08] bg-white hover:border-black/[0.16]"
                                }`}
                            >
                                {canBulkDelete && (
                                    <button
                                        type="button"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            toggle(t.id);
                                        }}
                                        aria-label={
                                            checked ? "Deselect" : "Select"
                                        }
                                        data-testid={`talent-check-${t.id}`}
                                        className={`absolute top-3 left-3 z-10 w-5 h-5 rounded-md border flex items-center justify-center transition-colors duration-150 ${
                                            checked 
                                                ? "bg-black border-black text-white" 
                                                : "bg-white border-black/[0.2] text-transparent group-hover:border-black/40"
                                        } ${isSelectionMode ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}
                                    >
                                        {checked && (
                                            <Check className="w-3 h-3" />
                                        )}
                                    </button>
                                )}
                                {goesToDetail ? (
                                    <Link
                                        to={`/admin/talents/${t.id}`}
                                        className="block"
                                    >
                                        <Inner
                                            t={t}
                                            anyImg={anyImg}
                                        />
                                    </Link>
                                ) : (
                                    <button
                                        type="button"
                                        onClick={() => toggle(t.id)}
                                        className="block w-full text-left"
                                    >
                                        <Inner t={t} anyImg={anyImg} />
                                    </button>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {canBulkDelete && (
                <BulkSelectBar
                    count={selected.size}
                    total={talents.length}
                    allSelected={selected.size === talents.length}
                    onSelectAll={selectAll}
                    onClear={clear}
                    onDelete={() => setConfirmOpen(true)}
                    labelSingular="talent"
                    labelPlural="talents"
                    testid="talents-bulk-bar"
                />
            )}
            <ConfirmDeleteDialog
                open={confirmOpen}
                title={`Delete ${selected.size} talent${selected.size === 1 ? "" : "s"}?`}
                description="This permanently removes the selected talent records and all their portfolio media. Submissions attached to these talents remain (they live on the project). This cannot be undone."
                confirmLabel={`Delete ${selected.size}`}
                typeToConfirm="DELETE"
                onCancel={() => setConfirmOpen(false)}
                onConfirm={bulkDelete}
                testid="talents-bulk-confirm"
            />
        </div>
    );
}

function Inner({ t, anyImg }) {
    return (
        <>
            <div className="aspect-[3/4] bg-[#fafaf8] rounded-t-xl overflow-hidden">
                {anyImg ? (
                    <img
                        src={anyImg.url}
                        alt={t.name}
                        className="w-full h-full object-cover"
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-black/20">
                        <ImageIcon className="w-8 h-8" strokeWidth={1} />
                    </div>
                )}
            </div>
            <div className="p-4">
                <div className="font-display text-base tracking-tight text-black/85">
                    {t.name}
                </div>
                <div className="text-[11px] text-black/45 mt-1">
                    {t.location ? t.location + " · " : ""}
                    {(t.media || []).length} asset{(t.media || []).length === 1 ? "" : "s"}
                </div>
            </div>
        </>
    );
}
