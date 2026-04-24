import React, { useEffect, useState } from "react";
import axios from "axios";
import { useSearchParams, useNavigate, Link } from "react-router-dom";
import { API } from "@/lib/api";
import { toast } from "sonner";
import Logo from "@/components/Logo";
import { Loader2, Lock, Check, XCircle, Eye, EyeOff } from "lucide-react";

/**
 * Public /reset-password?token=... page.
 * Consumes a single-use admin-generated token. On success the backend bumps
 * the user's token_version so every existing JWT dies; we send the user to
 * /admin/login to sign in afresh.
 */
export default function ResetPasswordPage() {
    const [params] = useSearchParams();
    const nav = useNavigate();
    const token = params.get("token") || "";

    const [state, setState] = useState("validating"); // validating | ready | invalid
    const [email, setEmail] = useState("");
    const [next, setNext] = useState("");
    const [confirm, setConfirm] = useState("");
    const [showNext, setShowNext] = useState(false);
    const [busy, setBusy] = useState(false);
    const [errMsg, setErrMsg] = useState("");

    useEffect(() => {
        if (!token) {
            setState("invalid");
            setErrMsg("Missing reset token.");
            return;
        }
        (async () => {
            try {
                const { data } = await axios.post(`${API}/public/reset-password/validate`, { token });
                setEmail(data.email || "");
                setState("ready");
            } catch (err) {
                setState("invalid");
                setErrMsg(
                    err?.response?.data?.detail ||
                        "This reset link is invalid or has expired.",
                );
            }
        })();
    }, [token]);

    const submit = async (e) => {
        e.preventDefault();
        if (next !== confirm) {
            toast.error("Passwords do not match");
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
            await axios.post(`${API}/public/reset-password`, {
                token,
                new_password: next,
            });
            toast.success("Password updated. Please sign in with your new password.");
            nav("/admin/login", { replace: true });
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Could not reset password");
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="min-h-screen bg-[#050505] text-[var(--tg-text)] tg-grain flex items-center justify-center px-4">
            <div className="w-full max-w-md">
                <div className="mb-8">
                    <Logo size="md" />
                </div>
                <p className="eyebrow mb-2">Account recovery</p>
                <h1 className="font-display text-3xl md:text-4xl mb-6">
                    Reset password.
                </h1>

                {state === "validating" && (
                    <div
                        className="flex items-center gap-2 text-sm text-white/60"
                        data-testid="reset-validating"
                    >
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Checking link…
                    </div>
                )}

                {state === "invalid" && (
                    <div
                        className="border border-[#FF3B30]/30 bg-[#FF3B30]/5 p-5 rounded-sm"
                        data-testid="reset-invalid"
                    >
                        <div className="flex items-center gap-2 text-[#FF3B30] mb-2">
                            <XCircle className="w-4 h-4" />
                            <p className="text-sm font-medium">Link cannot be used</p>
                        </div>
                        <p className="text-sm text-white/70">{errMsg}</p>
                        <p className="text-[11px] text-white/40 mt-3">
                            Ask an administrator to generate a fresh reset link.
                        </p>
                    </div>
                )}

                {state === "ready" && (
                    <>
                        <p className="text-sm text-white/60 mb-6 leading-relaxed">
                            Setting a new password for{" "}
                            <span className="text-white/90">{email}</span>. This will sign out all
                            active sessions.
                        </p>
                        <form onSubmit={submit}>
                            <label className="block text-xs tg-mono text-white/50 mb-1">
                                New password
                            </label>
                            <div className="relative mb-2">
                                <Lock className="absolute left-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/30" />
                                <input
                                    type={showNext ? "text" : "password"}
                                    value={next}
                                    onChange={(e) => setNext(e.target.value)}
                                    autoComplete="new-password"
                                    required
                                    minLength={8}
                                    disabled={busy}
                                    data-testid="reset-pw-new"
                                    className="w-full bg-transparent border-b border-white/10 focus:border-white/40 pl-6 pr-8 py-2 outline-none text-sm"
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowNext((v) => !v)}
                                    tabIndex={-1}
                                    className="absolute right-1 top-1/2 -translate-y-1/2 p-1 text-white/40 hover:text-white"
                                >
                                    {showNext ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                                </button>
                            </div>
                            <p className="text-[11px] text-white/40 mb-4">
                                Min 8 characters · include a number or special character.
                            </p>

                            <label className="block text-xs tg-mono text-white/50 mb-1">
                                Confirm new password
                            </label>
                            <div className="relative mb-6">
                                <Lock className="absolute left-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-white/30" />
                                <input
                                    type={showNext ? "text" : "password"}
                                    value={confirm}
                                    onChange={(e) => setConfirm(e.target.value)}
                                    autoComplete="new-password"
                                    required
                                    disabled={busy}
                                    data-testid="reset-pw-confirm"
                                    className="w-full bg-transparent border-b border-white/10 focus:border-white/40 pl-6 pr-2 py-2 outline-none text-sm"
                                />
                            </div>

                            <button
                                type="submit"
                                disabled={busy}
                                data-testid="reset-submit-btn"
                                className="w-full bg-white text-black py-3 rounded-sm text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50"
                            >
                                {busy ? (
                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                ) : (
                                    <Check className="w-3.5 h-3.5" />
                                )}
                                Set new password
                            </button>
                        </form>
                    </>
                )}

                <Link
                    to="/admin/login"
                    className="mt-6 inline-block text-xs text-white/60 hover:text-white"
                    data-testid="reset-back-link"
                >
                    Back to sign in
                </Link>
            </div>
        </div>
    );
}
