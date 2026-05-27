import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { adminApi, saveAdminSession } from "@/lib/api";
import Logo from "@/components/Logo";
import { Loader2 } from "lucide-react";

export default function AdminLogin() {
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [loading, setLoading] = useState(false);
    const nav = useNavigate();

    const submit = async (e) => {
        e.preventDefault();
        setLoading(true);
        try {
            const { data } = await adminApi.post("/auth/login", {
                email,
                password,
            });
            saveAdminSession(data.token, data.admin);
            toast.success("Welcome back");
            nav("/admin");
        } catch (err) {
            toast.error(err?.response?.data?.detail || "Login failed");
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#ffffff] p-6 text-black/85">
            <div className="w-full max-w-md flex flex-col items-center">
                {/* Logo top-centered */}
                <div className="mb-10 text-center">
                    <Logo size={120} className="mx-auto" forceVariant="black" />
                </div>

                <div className="w-full border border-black/[0.06] rounded-2xl p-8 bg-white">
                    <form
                        onSubmit={submit}
                        data-testid="admin-login-form"
                    >
                        <p className="eyebrow mb-2">Secure Access</p>
                        <h1 className="font-display text-2xl tracking-tight text-black/90 mb-6">
                            Sign in to Talentgram
                        </h1>

                        <label className="block mb-4">
                            <span className="text-xs text-black/60 tracking-wide">
                                Email
                            </span>
                            <input
                                type="email"
                                value={email}
                                data-testid="login-email-input"
                                onChange={(e) => setEmail(e.target.value)}
                                required
                                className="mt-1.5 w-full bg-[#fafaf8] border border-black/[0.08] focus:border-black/[0.16] rounded-lg outline-none py-2.5 px-3 text-sm text-black/85 placeholder:text-black/30 transition-colors duration-150"
                            />
                        </label>
                        <label className="block mb-6">
                            <span className="text-xs text-black/60 tracking-wide">
                                Password
                            </span>
                            <input
                                type="password"
                                value={password}
                                data-testid="login-password-input"
                                onChange={(e) => setPassword(e.target.value)}
                                required
                                className="mt-1.5 w-full bg-[#fafaf8] border border-black/[0.08] focus:border-black/[0.16] rounded-lg outline-none py-2.5 px-3 text-sm text-black/85 placeholder:text-black/30 transition-colors duration-150"
                            />
                        </label>
                        <button
                            type="submit"
                            disabled={loading}
                            data-testid="login-submit-btn"
                            className="w-full bg-black text-white py-3 rounded-lg text-sm font-medium hover:bg-black/90 transition-colors duration-150 inline-flex items-center justify-center gap-2"
                        >
                            {loading && (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            )}
                            Enter Dashboard
                        </button>
                        <div className="flex items-center justify-center mt-5">
                            <a
                                href="/forgot-password"
                                data-testid="login-forgot-link"
                                className="text-[11px] text-black/45 hover:text-black/80 underline-offset-4 hover:underline transition-colors duration-150"
                            >
                                Forgot password?
                            </a>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    );
}
