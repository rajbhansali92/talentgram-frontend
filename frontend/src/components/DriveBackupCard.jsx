import React, { useEffect, useState } from "react";
import { adminApi, getAdmin } from "@/lib/api";
import { toast } from "sonner";
import { Cloud, Loader2, RefreshCw, AlertTriangle, CheckCircle2, ExternalLink } from "lucide-react";

/**
 * Drive Backup status pill — admin-only.
 *
 * Renders one of:
 *   - "Not configured"   (env vars missing — instruct dev)
 *   - "Connect Drive"    (configured, no refresh token yet)
 *   - "Connected as ..." (refresh token in DB; primary upload path)
 *
 * Connect flow opens Google's consent screen in a new tab; on the redirect
 * back the backend stores the refresh token and clears any "terminal" Drive
 * upload failures so they retry automatically.
 */
export default function DriveBackupCard() {
    const admin = getAdmin();
    const isAdmin = admin?.role === "admin";
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [acting, setActing] = useState(false);

    const refresh = async () => {
        try {
            const { data } = await adminApi.get("/admin/drive/status");
            setStatus(data);
        } catch (e) {
            // 403 → not admin; just hide the card silently
            setStatus(null);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (!isAdmin) {
            setLoading(false);
            return;
        }
        refresh();
        // Poll lightly while OAuth is in-flight (window blur/focus) — picks up
        // the connection without forcing the user to refresh.
        const onFocus = () => refresh();
        window.addEventListener("focus", onFocus);
        return () => window.removeEventListener("focus", onFocus);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isAdmin]);

    if (!isAdmin) return null;
    if (loading || !status) {
        return (
            <div className="border border-white/10 p-5 mb-8 flex items-center gap-3 text-white/50">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span className="text-xs tg-mono">Checking Google Drive backup…</span>
            </div>
        );
    }

    const { enabled, oauth_configured, connected, connected_email, pending_retries, terminal_failures } = status;

    const connect = async () => {
        setActing(true);
        try {
            const { data } = await adminApi.get("/admin/drive/oauth/start");
            window.open(data.authorization_url, "_blank", "noopener,noreferrer,width=520,height=700");
            toast.info("Complete consent in the new window — this card auto-updates.");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Could not start OAuth flow");
        } finally {
            setActing(false);
        }
    };

    const disconnect = async () => {
        if (!window.confirm("Disconnect Google Drive? Future uploads will pause until you reconnect.")) return;
        setActing(true);
        try {
            await adminApi.post("/admin/drive/oauth/disconnect");
            toast.success("Drive disconnected");
            refresh();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Disconnect failed");
        } finally {
            setActing(false);
        }
    };

    const retryFailed = async () => {
        setActing(true);
        try {
            const { data } = await adminApi.post("/admin/drive/retry");
            toast.success(`Re-queued ${data.requeued} pending uploads`);
            refresh();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Retry failed");
        } finally {
            setActing(false);
        }
    };

    const dotColor = !enabled
        ? "bg-white/30"
        : connected
        ? "bg-emerald-400"
        : "bg-amber-400";

    return (
        <div
            className="border border-white/10 p-5 md:p-6 mb-8 tg-fade-up"
            data-testid="drive-backup-card"
        >
            <div className="flex items-start gap-4 flex-wrap">
                <Cloud className="w-5 h-5 text-white/40 mt-0.5" strokeWidth={1.4} />
                <div className="flex-1 min-w-[220px]">
                    <p className="eyebrow mb-1">Backup Storage</p>
                    <div className="flex items-center gap-2 mb-1">
                        <span className={`inline-block w-2 h-2 rounded-full ${dotColor}`} />
                        <h3 className="font-display text-lg">
                            Google Drive Backup
                        </h3>
                    </div>
                    {!enabled && (
                        <p className="text-xs text-white/50 mt-1">
                            Not configured — set GOOGLE_DRIVE_PARENT_FOLDER_ID and
                            either Service Account or OAuth credentials in the backend.
                        </p>
                    )}
                    {enabled && !oauth_configured && (
                        <p className="text-xs text-white/50 mt-1">
                            OAuth not configured. Service-account fallback may not be able to upload files (zero quota).
                        </p>
                    )}
                    {enabled && oauth_configured && !connected && (
                        <p className="text-xs text-amber-300/80 mt-1">
                            Drive credentials configured, but no admin has connected yet — file backups are queued.
                        </p>
                    )}
                    {connected && (
                        <p className="text-xs text-white/60 mt-1" data-testid="drive-connected-email">
                            Connected as{" "}
                            <span className="text-white">{connected_email}</span>
                        </p>
                    )}

                    {(pending_retries > 0 || terminal_failures > 0) && (
                        <div className="text-[11px] tg-mono text-white/50 mt-2 inline-flex items-center gap-3">
                            {pending_retries > 0 && (
                                <span data-testid="drive-pending-count">
                                    {pending_retries} pending retry
                                </span>
                            )}
                            {terminal_failures > 0 && (
                                <span className="inline-flex items-center gap-1 text-amber-300/80" data-testid="drive-terminal-count">
                                    <AlertTriangle className="w-3 h-3" />
                                    {terminal_failures} stuck
                                </span>
                            )}
                        </div>
                    )}
                </div>

                <div className="flex gap-2 flex-wrap">
                    {oauth_configured && !connected && (
                        <button
                            type="button"
                            onClick={connect}
                            disabled={acting}
                            data-testid="drive-connect-btn"
                            className="px-4 py-2 bg-white text-black rounded-sm text-xs tracking-wide hover:opacity-90 inline-flex items-center gap-1.5 disabled:opacity-50"
                        >
                            {acting ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                            ) : (
                                <ExternalLink className="w-3 h-3" />
                            )}
                            Connect Drive
                        </button>
                    )}
                    {connected && (
                        <>
                            <button
                                type="button"
                                onClick={retryFailed}
                                disabled={acting}
                                data-testid="drive-retry-btn"
                                className="px-3 py-2 border border-white/15 hover:border-white/40 rounded-sm text-[11px] tg-mono inline-flex items-center gap-1.5 disabled:opacity-50"
                            >
                                <RefreshCw className="w-3 h-3" />
                                Retry pending
                            </button>
                            <button
                                type="button"
                                onClick={connect}
                                disabled={acting}
                                data-testid="drive-reconnect-btn"
                                className="px-3 py-2 border border-white/15 hover:border-white/40 rounded-sm text-[11px] tg-mono"
                            >
                                Reconnect
                            </button>
                            <button
                                type="button"
                                onClick={disconnect}
                                disabled={acting}
                                data-testid="drive-disconnect-btn"
                                className="px-3 py-2 border border-white/15 hover:border-[#FF3B30]/50 hover:text-[#FF3B30] rounded-sm text-[11px] tg-mono"
                            >
                                Disconnect
                            </button>
                        </>
                    )}
                    <button
                        type="button"
                        onClick={refresh}
                        disabled={acting}
                        data-testid="drive-refresh-status-btn"
                        title="Refresh status"
                        className="px-2 py-2 border border-white/10 hover:border-white/30 rounded-sm text-white/60"
                    >
                        <RefreshCw className="w-3 h-3" />
                    </button>
                </div>
            </div>

            {connected && (
                <div className="mt-4 pt-4 border-t border-white/5 text-[11px] text-white/40 tg-mono inline-flex items-center gap-1.5">
                    <CheckCircle2 className="w-3 h-3 text-emerald-400/70" />
                    All future media uploads will sync to your Drive automatically.
                </div>
            )}
        </div>
    );
}
