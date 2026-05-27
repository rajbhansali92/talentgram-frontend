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
        } catch (e) {
            console.error(e);
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
        } catch (e) { console.error(e); }
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
        } catch (e) { console.error(e); }
    };

    return (
        <div ref={ref} className="relative" data-testid="notification-bell">
            <button
                type="button"
                onClick={toggle}
                aria-label="Notifications"
                data-testid="notification-bell-btn"
                className="relative p-2 rounded-sm text-black/40 hover:text-black hover:bg-black/[0.03] transition-all focus:outline-none"
            >
                <Bell className="w-4 h-4" strokeWidth={1.5} />
                {count > 0 && (
                    <span
                        className="absolute top-0.5 right-0.5 min-w-[14px] h-[14px] px-1 text-[9px] font-medium leading-[14px] rounded-full bg-black text-white text-center shadow-sm"
                        data-testid="notification-unread-count"
                    >
                        {count > 99 ? "99+" : count}
                    </span>
                )}
            </button>

            {open && (
                <>
                    {/* Mobile-only backdrop sheet — full width, taps outside dismiss. */}
                    <div
                        className="md:hidden fixed inset-0 z-40 bg-black/20 backdrop-blur-[1px]"
                        onClick={() => setOpen(false)}
                        aria-hidden="true"
                    />
                    <div
                        className="fixed left-2 right-2 top-[60px] mt-0 md:absolute md:right-auto md:left-0 md:top-full md:mt-2 md:w-[360px] z-50 bg-white border border-black/[0.08] shadow-xl max-h-[80vh] overflow-hidden flex flex-col rounded-sm"
                        data-testid="notification-dropdown"
                    >
                        <div className="px-4 py-3 border-b border-black/[0.06] flex items-center justify-between shrink-0 bg-white">
                            <p className="text-xs font-semibold uppercase tracking-wider text-black/75">Notifications</p>
                            <div className="flex items-center gap-2">
                                {count > 0 && (
                                    <button
                                        onClick={markAll}
                                        className="text-[10px] text-black/40 hover:text-black hover:bg-black/[0.02] border border-black/[0.06] rounded-sm px-2 py-1 transition-all inline-flex items-center gap-1"
                                        data-testid="mark-all-read-btn"
                                    >
                                        <Check className="w-3 h-3" />
                                        Mark all read
                                    </button>
                                )}
                                <button
                                    onClick={() => setOpen(false)}
                                    className="md:hidden text-[10px] text-black/40 hover:text-black px-2 py-1"
                                    data-testid="notification-close-btn"
                                    aria-label="Close notifications"
                                >
                                    Close
                                </button>
                            </div>
                        </div>

                        {/* Scrollable region */}
                        <div
                            className="flex-1 overflow-y-auto overscroll-contain min-h-0 bg-white"
                            data-testid="notification-scroll-region"
                        >
                            {loading ? (
                                <div className="p-6 text-center text-black/40 text-xs inline-flex items-center gap-2 justify-center w-full">
                                    <Loader2 className="w-3 h-3 animate-spin" />
                                    Loading…
                                </div>
                            ) : items.length === 0 ? (
                                <div className="p-8 text-center text-black/40 text-xs">
                                    You're all caught up.
                                </div>
                            ) : (
                                items.map((n) => (
                                    <button
                                        key={n.id}
                                        onClick={() => onItemClick(n)}
                                        data-testid={`notification-item-${n.id}`}
                                        className={`w-full text-left px-4 py-3 border-b border-black/[0.04] hover:bg-black/[0.02] flex gap-3 transition-all ${
                                            !n.read_at ? "bg-black/[0.015]" : ""
                                        }`}
                                    >
                                        <span
                                            className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                                                !n.read_at ? "bg-black" : "bg-black/5"
                                            }`}
                                        />
                                        <span className="flex-1 min-w-0">
                                            <span className="block text-xs font-medium text-black/85 leading-snug">
                                                {n.title}
                                            </span>
                                            {n.body && (
                                                <span className="block text-[11px] text-black/50 mt-0.5">
                                                    {n.body}
                                                </span>
                                            )}
                                            <span className="block text-[9px] text-black/35 font-mono mt-1">
                                                {timeAgo(n.created_at)}
                                            </span>
                                        </span>
                                        <ExternalLink className="w-3 h-3 text-black/30 shrink-0 mt-1" />
                                    </button>
                                ))
                            )}
                        </div>

                        <Link
                            to="/admin/notifications"
                            onClick={() => setOpen(false)}
                            className="block text-center text-[11px] font-medium text-black/60 hover:text-black hover:bg-black/[0.02] py-3 border-t border-black/[0.06] transition-all bg-white shrink-0"
                            data-testid="see-all-notifications-link"
                        >
                            See all notifications
                        </Link>
                    </div>
                </>
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
