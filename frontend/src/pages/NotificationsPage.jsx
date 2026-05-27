import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { adminApi } from "@/lib/api";
import { Bell, Check, ChevronLeft, Loader2 } from "lucide-react";
import { toast } from "sonner";

const PAGE_SIZE = 30;

export default function NotificationsPage() {
    const nav = useNavigate();
    const [items, setItems] = useState([]);
    const [page, setPage] = useState(0);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [filter, setFilter] = useState("all"); // all | unread

    const fetchPage = async (p, f = filter) => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/notifications", {
                params: { page: p, size: PAGE_SIZE, unread_only: f === "unread" },
            });
            setItems(data?.items || []);
            setTotal(data?.total || 0);
            setPage(p);
        } catch {
            setItems([]);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchPage(0, filter);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filter]);

    const markAll = async () => {
        try {
            const { data } = await adminApi.post("/notifications/read-all");
            toast.success(`${data.marked} marked as read`);
            fetchPage(page, filter);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed");
        }
    };

    const open = async (n) => {
        try {
            if (!n.read_at) await adminApi.post(`/notifications/${n.id}/read`);
        } catch (e) { console.error(e); }
        const pid = n?.payload?.project_id;
        const sid = n?.payload?.submission_id;
        if (pid && sid) nav(`/admin/projects/${pid}?sub=${sid}`);
        else if (pid) nav(`/admin/projects/${pid}`);
    };

    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

    return (
        <div className="space-y-4 max-w-3xl">
            <div className="flex items-center justify-between gap-4 flex-wrap">
                <div>
                    <p className="eyebrow mb-1">Activity</p>
                    <h1 className="font-display text-2xl md:text-3xl tracking-tight inline-flex items-center gap-3 text-black/90">
                        <Bell className="w-5 h-5 text-black/40" strokeWidth={1.4} />
                        Notifications
                    </h1>
                    <p className="text-xs text-black/45 mt-1.5 tg-mono">
                        {filter === "unread" 
                            ? (total === 0 ? "No unread activity" : `${total} unread notification${total > 1 ? "s" : ""}`) 
                            : `${total} total notification${total > 1 ? "s" : ""}`}
                    </p>
                </div>
                <button
                    onClick={() => nav(-1)}
                    className="text-xs text-black/60 hover:text-black inline-flex items-center gap-1 focus:outline-none"
                >
                    <ChevronLeft className="w-3.5 h-3.5" />
                    Back
                </button>
            </div>

            <div className="flex items-center gap-3 flex-wrap">
                <div className="inline-flex border border-black/[0.06] bg-[#fafaf8] rounded-sm overflow-hidden">
                    <button
                        onClick={() => setFilter("all")}
                        data-testid="filter-all"
                        className={`px-3 py-1.5 text-xs tg-mono transition-colors focus:outline-none ${
                            filter === "all" ? "bg-black text-white" : "text-black/60 hover:text-black"
                        }`}
                    >
                        All
                    </button>
                    <button
                        onClick={() => setFilter("unread")}
                        data-testid="filter-unread"
                        className={`px-3 py-1.5 text-xs tg-mono border-l border-black/[0.06] transition-colors focus:outline-none ${
                            filter === "unread" ? "bg-black text-white" : "text-black/60 hover:text-black"
                        }`}
                    >
                        Unread
                    </button>
                </div>
                <button
                    onClick={markAll}
                    data-testid="page-mark-all-read"
                    className="px-3 py-1.5 border border-black/[0.06] hover:border-black/[0.12] hover:bg-black/[0.02] rounded-sm text-[11px] tg-mono inline-flex items-center gap-1.5 transition-all text-black/70 focus:outline-none"
                >
                    <Check className="w-3 h-3" />
                    Mark all as read
                </button>
            </div>

            <div className="border border-black/[0.06] bg-white rounded-md divide-y divide-black/[0.04] shadow-sm overflow-hidden">
                {loading ? (
                    <div className="py-8 text-center text-black/45 text-xs">
                        <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                        Loading…
                    </div>
                ) : items.length === 0 ? (
                    <div className="py-8 px-4 text-center">
                        <p className="text-xs font-medium text-black/70">Nothing here yet.</p>
                        <p className="text-[11px] text-black/40 mt-1">New submissions and applications will appear here.</p>
                    </div>
                ) : (
                    items.map((n) => (
                        <button
                            key={n.id}
                            onClick={() => open(n)}
                            data-testid={`notif-row-${n.id}`}
                            className={`w-full text-left px-4 py-3.5 border-b border-black/[0.04] flex gap-3 hover:bg-black/[0.015] transition-all focus:outline-none ${
                                !n.read_at ? "bg-black/[0.015]" : "bg-white"
                            }`}
                        >
                            <span
                                className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                                    !n.read_at ? "bg-black" : "bg-black/5"
                                }`}
                            />
                            <div className="flex-1 min-w-0">
                                <p className="text-xs font-semibold text-black/85 leading-snug">{n.title}</p>
                                {n.body && (
                                    <p className="text-[11px] text-black/50 mt-0.5 leading-relaxed">{n.body}</p>
                                )}
                                <p className="text-[9px] text-black/35 mt-1 font-mono uppercase tracking-wider">
                                    {n.type?.replace("_", " ")} · {new Date(n.created_at).toLocaleDateString()} {new Date(n.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                                </p>
                            </div>
                        </button>
                    ))
                )}
            </div>

            {totalPages > 1 && (
                <div className="flex items-center justify-between text-xs tg-mono text-black/60 pt-4">
                    <button
                        disabled={page === 0}
                        onClick={() => fetchPage(page - 1)}
                        className="px-3 py-1.5 border border-black/[0.06] hover:border-black/[0.12] hover:bg-black/[0.02] rounded-sm disabled:opacity-30 transition-all text-black/75 focus:outline-none"
                    >
                        Prev
                    </button>
                    <span>
                        Page {page + 1} / {totalPages}
                    </span>
                    <button
                        disabled={(page + 1) * PAGE_SIZE >= total}
                        onClick={() => fetchPage(page + 1)}
                        className="px-3 py-1.5 border border-black/[0.06] hover:border-black/[0.12] hover:bg-black/[0.02] rounded-sm disabled:opacity-30 transition-all text-black/75 focus:outline-none"
                    >
                        Next
                    </button>
                </div>
            )}
        </div>
    );
}
