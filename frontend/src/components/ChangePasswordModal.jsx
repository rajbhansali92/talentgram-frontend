import React, { useState } from "react";
import { toast } from "sonner";
import { adminApi, clearAdminSession } from "@/lib/api";
import { Lock, Eye, EyeOff, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";

/**
 * Self-service "Change Password" modal.
 * After a successful change, the backend bumps token_version — every existing
 * JWT (including this session's) is now invalid. We proactively log the user
 * out and bounce them to /admin/login to sign in with the new password.
 */
export default function ChangePasswordModal({ open, onClose }) {
    const nav = useNavigate();
    const [current, setCurrent] = useState("");
    const [next, setNext] = useState("");
    const [confirm, setConfirm] = useState("");
    const [showNext, setShowNext] = useState(false);
    const [busy, setBusy] = useState(false);

    if (!open) return null;

    const reset = () => {
        setCurrent("");
        setNext("");
        setConfirm("");
        setShowNext(false);
    };

    const close = () => {
        if (busy) return;
        reset();
        onClose?.();
    };

    const submit = async (e) => {
        e.preventDefault();
        if (!current || !next) {
            toast.error("Fill both current and new passwords");
            return;
        }
        if (next !== confirm) {
            toast.error("New passwords do not match");
            return;
        }
        if (next.length < 8) {
            toast.error("New password must be at least 8 characters");
            return;
        }
        if (!/[0-9]/.test(next) && !/[^a-zA-Z0-9]/.test(next)) {
            toast.error("Must include at least one number or special character");
            return;
        }
        setBusy(true);
        try {
            await adminApi.post("/auth/change-password", {
                current_password: current,
                new_password: next,
            });
            toast.success("Password updated. Please sign in again.");
            clearAdminSession();
            reset();
            onClose?.();
            nav("/admin/login", { replace: true });
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Could not change password");
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            className="fixed inset-0 z-50 bg-black/80 backdrop-blur flex items-center justify-center p-4"
            data-testid="change-password-modal"
        >
            <form
                onSubmit={submit}
                className="w-full max-w-md border border-border bg-background p-6 md:p-8"
            >
                <div className="flex items-center gap-2 mb-2">
                    <Lock className="w-4 h-4 text-muted-foreground" />
                    <p className="eyebrow">Security</p>
                </div>
                <h2 className="font-display text-2xl mb-2">Change password</h2>
                <p className="text-sm text-muted-foreground mb-5">
                    After you confirm, every active session (including this one) will be signed out.
                </p>

                <label className="block text-xs tg-mono text-muted-foreground mb-1">
                    Current password
                </label>
                <input
                    type="password"
                    value={current}
                    onChange={(e) => setCurrent(e.target.value)}
                    autoComplete="current-password"
                    className="w-full bg-transparent border-b border-border focus:border-foreground/60 py-2 mb-4 outline-none text-sm"
                    data-testid="change-pw-current"
                    disabled={busy}
                    required
                />

                <label className="block text-xs tg-mono text-muted-foreground mb-1">
                    New password
                </label>
                <div className="relative mb-1">
                    <input
                        type={showNext ? "text" : "password"}
                        value={next}
                        onChange={(e) => setNext(e.target.value)}
                        autoComplete="new-password"
                        className="w-full bg-transparent border-b border-border focus:border-foreground/60 py-2 pr-8 outline-none text-sm"
                        data-testid="change-pw-new"
                        disabled={busy}
                        minLength={8}
                        required
                    />
                    <button
                        type="button"
                        onClick={() => setShowNext((v) => !v)}
                        className="absolute right-1 top-1/2 -translate-y-1/2 p-1 text-muted-foreground hover:text-foreground"
                        tabIndex={-1}
                    >
                        {showNext ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                    </button>
                </div>
                <p className="text-[11px] text-muted-foreground mb-4">
                    Min 8 characters · must include a number or special character.
                </p>

                <label className="block text-xs tg-mono text-muted-foreground mb-1">
                    Confirm new password
                </label>
                <input
                    type={showNext ? "text" : "password"}
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    autoComplete="new-password"
                    className="w-full bg-transparent border-b border-border focus:border-foreground/60 py-2 mb-6 outline-none text-sm"
                    data-testid="change-pw-confirm"
                    disabled={busy}
                    required
                />

                <div className="flex gap-2">
                    <button
                        type="button"
                        onClick={close}
                        className="flex-1 border border-border hover:border-foreground/60 py-3 rounded-sm text-sm"
                        data-testid="change-pw-cancel"
                        disabled={busy}
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        className="flex-1 bg-foreground text-background py-3 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-50"
                        data-testid="change-pw-submit"
                        disabled={busy}
                    >
                        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                        Update password
                    </button>
                </div>
            </form>
        </div>
    );
}
