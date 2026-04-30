import React, { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import axios from "axios";
import { toast } from "sonner";
import { saveAdminSession } from "@/lib/api";
import Logo from "@/components/Logo";
import { Loader2, Check, Sparkles } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export default function SignupPage() {
    const [params] = useSearchParams();
    const nav = useNavigate();
    const token = params.get("token") || "";

    const [state, setState] = useState("loading"); // loading | ready | expired | notfound
    const [invite, setInvite] = useState(null);
    const [password, setPassword] = useState("");
    const [confirm, setConfirm] = useState("");
    const [saving, setSaving] = useState(false);

    const validate = useCallback(async () => {
        if (!token) {
            setState("notfound");
            return;
        }
        try {
            const { data } = await axios.post(
                `${API}/public/signup/validate`,
                { token },
            );
            setInvite(data);
            setState("ready");
        } catch (e) {
            const status = e?.response?.status;
            if (status === 410) setState("expired");
            else setState("notfound");
        }
    }, [token]);

    useEffect(() => {
        validate();
    }, [validate]);

    const submit = async (e) => {
        e.preventDefault();
        if (password.length < 8) {
            toast.error("Password must be at least 8 characters");
            return;
        }
        if (password !== confirm) {
            toast.error("Passwords don't match");
            return;
        }
        setSaving(true);
        try {
            await axios.post(`${API}/public/signup/complete`, {
                token,
                password,
            });
            // Auto-login
            const { data } = await axios.post(`${API}/auth/login`, {
                email: invite.email,
                password,
            });
            saveAdminSession(data.token, data.admin);
            toast.success(`Welcome, ${invite.name.split(" ")[0]}`);
            nav("/admin");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Failed to complete signup");
        } finally {
            setSaving(false);
        }
    };

    return (
        <div
            className="min-h-screen bg-[#050505] text-white flex items-center justify-center px-4"
            data-testid="signup-page"
        >
            <div className="w-full max-w-md">
                <div className="text-center mb-10">
                    <Logo size="md" className="mx-auto" />
                    <p className="eyebrow mt-4">Invitation</p>
                </div>

                {state === "loading" && (
                    <div className="border border-white/10 p-10 text-center">
                        <Loader2 className="w-5 h-5 animate-spin mx-auto text-white/60" />
                        <p className="text-xs text-white/40 tg-mono mt-3">
                            Verifying invite…
                        </p>
                    </div>
                )}

                {state === "notfound" && (
                    <div
                        className="border border-white/10 p-10 text-center"
                        data-testid="signup-invalid"
                    >
                        <h2 className="font-display text-2xl mb-3">
                            Invite not found
                        </h2>
                        <p className="text-sm text-white/60 mb-6">
                            This invite link is invalid or has already been used.
                            Ask an admin to resend a fresh invite.
                        </p>
                        <a
                            href="/admin/login"
                            className="text-xs tg-mono text-white/60 hover:text-white inline-flex items-center gap-1"
                        >
                            Go to sign in →
                        </a>
                    </div>
                )}

                {state === "expired" && (
                    <div
                        className="border border-white/10 p-10 text-center"
                        data-testid="signup-expired"
                    >
                        <h2 className="font-display text-2xl mb-3">
                            Invite expired
                        </h2>
                        <p className="text-sm text-white/60 mb-6">
                            Invites expire after 7 days. Please ask an admin to
                            send a fresh invite.
                        </p>
                    </div>
                )}

                {state === "ready" && invite && (
                    <form
                        onSubmit={submit}
                        className="border border-white/10 p-8"
                        data-testid="signup-form"
                    >
                        <div className="mb-6">
                            <div className="inline-flex items-center gap-2 text-xs text-white/60 mb-2">
                                <Sparkles className="w-3 h-3" /> Invited as{" "}
                                <span className="uppercase tracking-widest text-[10px] tg-mono">
                                    {invite.role}
                                </span>
                            </div>
                            <h2 className="font-display text-3xl tracking-tight mb-1">
                                Welcome, {invite.name?.split(" ")[0] || "there"}.
                            </h2>
                            <p className="text-sm text-white/50 tg-mono">
                                {invite.email}
                            </p>
                        </div>

                        <label className="block mb-4">
                            <span className="text-[11px] tracking-widest uppercase text-white/50">
                                Choose password
                            </span>
                            <input
                                type="password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                minLength={8}
                                required
                                data-testid="signup-password"
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
                            />
                            <p className="text-[10px] text-white/40 mt-2 tg-mono">
                                Minimum 8 characters. Use a mix of letters, numbers
                                and symbols.
                            </p>
                        </label>

                        <label className="block mb-8">
                            <span className="text-[11px] tracking-widest uppercase text-white/50">
                                Confirm password
                            </span>
                            <input
                                type="password"
                                value={confirm}
                                onChange={(e) => setConfirm(e.target.value)}
                                minLength={8}
                                required
                                data-testid="signup-confirm"
                                className="mt-2 w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 text-sm"
                            />
                        </label>

                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="signup-submit"
                            className="w-full bg-white text-black py-3 rounded-sm text-sm hover:opacity-90 inline-flex items-center justify-center gap-2"
                        >
                            {saving ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                                <Check className="w-4 h-4" />
                            )}
                            Activate account
                        </button>
                    </form>
                )}
            </div>
        </div>
    );
}
