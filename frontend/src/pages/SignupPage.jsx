import React, { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import axios from "axios";
import { toast } from "sonner";
import { saveAdminSession } from "@/lib/api";
import Logo from "@/components/Logo";
import { Loader2, Check, Sparkles, Eye, EyeOff, RefreshCw } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export default function SignupPage() {
    const [params] = useSearchParams();
    const nav = useNavigate();
    const token = params.get("token") || "";

    const [state, setState] = useState("loading");
    const [invite, setInvite] = useState(null);
    const [password, setPassword] = useState("");
    const [confirm, setConfirm] = useState("");
    const [saving, setSaving] = useState(false);
    const [showPassword, setShowPassword] = useState(false);
    const [showConfirm, setShowConfirm] = useState(false);
    const [passwordsMatch, setPasswordsMatch] = useState(true);

    const validate = useCallback(async () => {
        if (!token) {
            setState("notfound");
            return;
        }

        setState("loading");

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
            else if (e?.request && !e?.response) setState("error");
            else setState("notfound");
        }
    }, [token]);

    useEffect(() => {
        validate();
    }, [validate]);

    // Check password match in real-time
    useEffect(() => {
        if (confirm.length > 0) {
            setPasswordsMatch(password === confirm);
        } else {
            setPasswordsMatch(true);
        }
    }, [password, confirm]);

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

    const getPasswordStrength = () => {
        if (password.length === 0) return "";
        if (password.length < 8) return "Too short";
        const hasLetter = /[A-Za-z]/.test(password);
        const hasNumber = /[0-9]/.test(password);
        const hasSymbol = /[^A-Za-z0-9]/.test(password);
        if (hasLetter && hasNumber && hasSymbol) return "Strong";
        if ((hasLetter && hasNumber) || (hasLetter && hasSymbol) || (hasNumber && hasSymbol)) return "Medium";
        return "Weak";
    };

    const strength = getPasswordStrength();
    const strengthColor = 
        strength === "Strong" ? "text-green-400" :
        strength === "Medium" ? "text-yellow-400" :
        strength === "Weak" ? "text-orange-400" :
        "text-white/40";

    return (
        <div
            className="min-h-screen bg-[#050505] text-white flex items-center justify-center px-4"
            data-testid="signup-page"
        >
            {/* ThemeToggle removed for auth pages to maintain dark, cinematic identity */}
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

                {state === "error" && (
                    <div
                        className="border border-white/10 p-10 text-center"
                        data-testid="signup-error"
                    >
                        <h2 className="font-display text-2xl mb-3">
                            Network error
                        </h2>
                        <p className="text-sm text-white/60 mb-6">
                            Unable to verify invitation right now. Please check your connection and try again.
                        </p>
                        <button
                            onClick={validate}
                            className="text-xs tg-mono text-white/60 hover:text-white inline-flex items-center gap-1"
                        >
                            <RefreshCw className="w-3 h-3" /> Retry verification
                        </button>
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
                            <p className="text-[10px] text-white/30 mt-3 tracking-wider tg-mono">
                                Private Talentgram workspace access
                            </p>
                        </div>

                        <label className="block mb-4">
                            <span className="text-[11px] tracking-widest uppercase text-white/50">
                                Choose password
                            </span>
                            <div className="relative mt-2">
                                <input
                                    type={showPassword ? "text" : "password"}
                                    value={password}
                                    onChange={(e) => setPassword(e.target.value)}
                                    minLength={8}
                                    required
                                    autoComplete="new-password"
                                    data-testid="signup-password"
                                    className="w-full bg-transparent border-b border-white/15 focus:border-white outline-none py-2.5 pr-10 text-sm focus:ring-0"
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowPassword(!showPassword)}
                                    className="absolute right-0 top-1/2 -translate-y-1/2 text-white/40 hover:text-white/80"
                                    tabIndex={-1}
                                >
                                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                </button>
                            </div>
                            <div className="flex justify-between items-center mt-2">
                                <p className="text-[10px] text-white/40 tg-mono">
                                    Minimum 8 characters. Use a mix of letters, numbers and symbols.
                                </p>
                                {password.length > 0 && (
                                    <span className={`text-[10px] tg-mono ${strengthColor}`}>
                                        {strength}
                                    </span>
                                )}
                            </div>
                        </label>

                        <label className="block mb-6">
                            <span className="text-[11px] tracking-widest uppercase text-white/50">
                                Confirm password
                            </span>
                            <div className="relative mt-2">
                                <input
                                    type={showConfirm ? "text" : "password"}
                                    value={confirm}
                                    onChange={(e) => setConfirm(e.target.value)}
                                    minLength={8}
                                    required
                                    autoComplete="new-password"
                                    data-testid="signup-confirm"
                                    className="w-full bg-transparent border-b focus:border-white outline-none py-2.5 pr-10 text-sm focus:ring-0"
                                    style={{
                                        borderBottomColor: !passwordsMatch && confirm.length > 0 ? "#ef4444" : "rgba(255,255,255,0.15)"
                                    }}
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowConfirm(!showConfirm)}
                                    className="absolute right-0 top-1/2 -translate-y-1/2 text-white/40 hover:text-white/80"
                                    tabIndex={-1}
                                >
                                    {showConfirm ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                                </button>
                            </div>
                            {!passwordsMatch && confirm.length > 0 && (
                                <p className="text-[10px] text-red-400 mt-2 tg-mono">
                                    Passwords do not match
                                </p>
                            )}
                        </label>

                        <button
                            type="submit"
                            disabled={saving}
                            data-testid="signup-submit"
                            className="w-full bg-white text-black py-3 rounded-sm text-sm hover:opacity-90 transition-opacity inline-flex items-center justify-center gap-2"
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
