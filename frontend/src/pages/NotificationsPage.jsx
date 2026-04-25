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
        } catch {}
        const pid = n?.payload?.project_id;
        const sid = n?.payload?.submission_id;
        if (pid && sid) nav(`/admin/projects/${pid}?sub=${sid}`);
        else if (pid) nav(`/admin/projects/${pid}`);
    };

    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

    return (
        <div className="space-y-6 max-w-3xl">
            <div className="flex items-center justify-between gap-4 flex-wrap">
                <div>
                    <p className="eyebrow mb-1">Activity</p>
                    <h1 className="font-display text-3xl md:text-4xl tracking-tight inline-flex items-center gap-3">
                        <Bell className="w-5 h-5 text-white/40" strokeWidth={1.4} />
                        Notifications
                    </h1>
                    <p className="text-xs text-white/40 mt-2 tg-mono">
                        {total} total · live across all team members
                    </p>
                </div>
                <button
                    onClick={() => nav(-1)}
                    className="text-xs text-white/50 hover:text-white inline-flex items-center gap-1"
                >
                    <ChevronLeft className="w-3.5 h-3.5" />
                    Back
                </button>
            </div>

            <div className="flex items-center gap-3 flex-wrap">
                <div className="inline-flex border border-white/10 rounded-sm">
                    <button
                        onClick={() => setFilter("all")}
                        data-testid="filter-all"
                        className={`px-4 py-2 text-xs tg-mono ${
                            filter === "all" ? "bg-white text-black" : "text-white/60 hover:text-white"
                        }`}
                    >
                        All
                    </button>
                    <button
                        onClick={() => setFilter("unread")}
                        data-testid="filter-unread"
                        className={`px-4 py-2 text-xs tg-mono border-l border-white/10 ${
                            filter === "unread" ? "bg-white text-black" : "text-white/60 hover:text-white"
                        }`}
                    >
                        Unread
                    </button>
                </div>
                <button
                    onClick={markAll}
                    data-testid="page-mark-all-read"
                    className="px-3 py-2 border border-white/10 hover:border-white/30 rounded-sm text-[11px] tg-mono inline-flex items-center gap-1.5"
                >
                    <Check className="w-3 h-3" />
                    Mark all as read
                </button>
            </div>

            <div className="border border-white/10 divide-y divide-white/5">
                {loading ? (
                    <div className="p-10 text-center text-white/50 text-xs">
                        <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                        Loading…
                    </div>
                ) : items.length === 0 ? (
                    <div className="p-10 text-center text-white/40 text-xs">
                        Nothing here yet.
                    </div>
                ) : (
                    items.map((n) => (
                        <button
                            key={n.id}
                            onClick={() => open(n)}
                            data-testid={`notif-row-${n.id}`}
                            className={`w-full text-left p-5 flex gap-3 hover:bg-white/[0.02] transition-all ${
                                !n.read_at ? "bg-white/[0.015]" : ""
                            }`}
                        >
                            <span
                                className={`mt-2 w-1.5 h-1.5 rounded-full shrink-0 ${
                                    !n.read_at ? "bg-emerald-400" : "bg-white/20"
                                }`}
                            />
                            <div className="flex-1 min-w-0">
                                <p className="text-sm text-white/90">{n.title}</p>
                                {n.body && (
                                    <p className="text-xs text-white/50 mt-1">{n.body}</p>
                                )}
                                <p className="text-[10px] text-white/30 mt-1.5 tg-mono uppercase tracking-wider">
                                    {n.type?.replace("_", " ")} · {new Date(n.created_at).toLocaleString()}
                                </p>
                            </div>
                        </button>
                    ))
                )}
            </div>

            {totalPages > 1 && (
                <div className="flex items-center justify-between text-xs tg-mono text-white/50">
                    <button
                        disabled={page === 0}
                        onClick={() => fetchPage(page - 1)}
                        className="px-3 py-2 border border-white/10 disabled:opacity-30"
                    >
                        Prev
                    </button>
                    <span>
                        Page {page + 1} / {totalPages}
                    </span>
                    <button
                        disabled={(page + 1) * PAGE_SIZE >= total}
                        onClick={() => fetchPage(page + 1)}
                        className="px-3 py-2 border border-white/10 disabled:opacity-30"
                    >
                        Next
                    </button>
                </div>
            )}
        </div>
    );
}
