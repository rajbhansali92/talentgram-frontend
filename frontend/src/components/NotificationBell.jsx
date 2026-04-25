import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { adminApi } from "@/lib/api";
import { Bell, Check, ExternalLink, Loader2 } from "lucide-react";

/**
 * Notification bell — shown in `AdminLayout`.
 *
 * - Polls `unread-count` every 30s while the page is visible
 * - Dropdown lists the latest 8 notifications (read & unread mixed)
 * - Clicking an item marks it as read and navigates to the deep-link
 *   (project review modal for submission events)
 * - "Mark all as read" empties the badge instantly
 */
export default function NotificationBell() {
    const nav = useNavigate();
    const [open, setOpen] = useState(false);
    const [count, setCount] = useState(0);
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(false);
    const ref = useRef(null);

    const fetchCount = async () => {
        try {
            const { data } = await adminApi.get("/notifications/unread-count");
            setCount(data?.count || 0);
        } catch {
            // silent — bell just shows stale count
        }
    };

    const fetchItems = async () => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/notifications", {
                params: { page: 0, size: 8 },
            });
            setItems(data?.items || []);
        } catch {
            setItems([]);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchCount();
        const id = setInterval(() => {
            if (document.visibilityState === "visible") fetchCount();
        }, 30000);
        return () => clearInterval(id);
    }, []);

    // Close dropdown when clicking outside
    useEffect(() => {
        if (!open) return;
        const onClick = (e) => {
            if (ref.current && !ref.current.contains(e.target)) setOpen(false);
        };
        document.addEventListener("mousedown", onClick);
        return () => document.removeEventListener("mousedown", onClick);
    }, [open]);

    const toggle = async () => {
        const next = !open;
        setOpen(next);
        if (next) await fetchItems();
    };

    const onItemClick = async (n) => {
        try {
            if (!n.read_at) {
                await adminApi.post(`/notifications/${n.id}/read`);
                setCount((c) => Math.max(0, c - 1));
            }
        } catch {}
        const type = n?.type || "";
        const pid = n?.payload?.project_id;
        const sid = n?.payload?.submission_id;
        if (type.startsWith("client_feedback") || type.startsWith("feedback_")) {
            nav("/admin/feedback");
        } else if (pid && sid) {
            nav(`/admin/projects/${pid}?sub=${sid}`);
        } else if (pid) {
            nav(`/admin/projects/${pid}`);
        }
        setOpen(false);
    };

    const markAll = async () => {
        try {
            await adminApi.post("/notifications/read-all");
            setCount(0);
            setItems((arr) => arr.map((n) => ({ ...n, read_at: new Date().toISOString() })));
        } catch {}
    };

    return (
        <div ref={ref} className="relative" data-testid="notification-bell">
            <button
                type="button"
                onClick={toggle}
                aria-label="Notifications"
                data-testid="notification-bell-btn"
                className="relative p-2 rounded-sm text-white/60 hover:text-white hover:bg-white/5 transition-all"
            >
                <Bell className="w-4 h-4" strokeWidth={1.5} />
                {count > 0 && (
                    <span
                        className="absolute top-1 right-1 min-w-[16px] h-[16px] px-1 text-[10px] tg-mono leading-[16px] rounded-full bg-[#FF3B30] text-white text-center"
                        data-testid="notification-unread-count"
                    >
                        {count > 99 ? "99+" : count}
                    </span>
                )}
            </button>

            {open && (
                <div
                    className="absolute right-0 top-full mt-2 w-[320px] md:w-[360px] z-50 bg-[#0a0a0a] border border-white/10 shadow-2xl"
                    data-testid="notification-dropdown"
                >
                    <div className="px-4 py-3 border-b border-white/5 flex items-center justify-between">
                        <p className="eyebrow">Notifications</p>
                        {count > 0 && (
                            <button
                                onClick={markAll}
                                className="text-[10px] text-white/50 hover:text-white tg-mono inline-flex items-center gap-1"
                                data-testid="mark-all-read-btn"
                            >
                                <Check className="w-3 h-3" />
                                Mark all read
                            </button>
                        )}
                    </div>

                    <div className="max-h-[420px] overflow-y-auto">
                        {loading ? (
                            <div className="p-6 text-center text-white/40 text-xs inline-flex items-center gap-2 justify-center w-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Loading…
                            </div>
                        ) : items.length === 0 ? (
                            <div className="p-8 text-center text-white/40 text-xs">
                                You're all caught up.
                            </div>
                        ) : (
                            items.map((n) => (
                                <button
                                    key={n.id}
                                    onClick={() => onItemClick(n)}
                                    data-testid={`notification-item-${n.id}`}
                                    className={`w-full text-left px-4 py-3 border-b border-white/5 hover:bg-white/5 flex gap-3 transition-all ${
                                        !n.read_at ? "bg-white/[0.02]" : ""
                                    }`}
                                >
                                    <span
                                        className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                                            !n.read_at ? "bg-emerald-400" : "bg-white/20"
                                        }`}
                                    />
                                    <span className="flex-1 min-w-0">
                                        <span className="block text-xs text-white/90 leading-snug">
                                            {n.title}
                                        </span>
                                        {n.body && (
                                            <span className="block text-[11px] text-white/50 mt-0.5">
                                                {n.body}
                                            </span>
                                        )}
                                        <span className="block text-[10px] text-white/30 tg-mono mt-1">
                                            {timeAgo(n.created_at)}
                                        </span>
                                    </span>
                                    <ExternalLink className="w-3 h-3 text-white/30 shrink-0 mt-1" />
                                </button>
                            ))
                        )}
                    </div>

                    <Link
                        to="/admin/notifications"
                        onClick={() => setOpen(false)}
                        className="block text-center text-[11px] tg-mono text-white/60 hover:text-white py-3 border-t border-white/5"
                        data-testid="see-all-notifications-link"
                    >
                        See all notifications
                    </Link>
                </div>
            )}
        </div>
    );
}

function timeAgo(iso) {
    if (!iso) return "";
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}
