import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { adminApi, saveAdminSession } from "@/lib/api";
import BrandHero from "@/components/BrandHero";
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
        <div className="min-h-screen grid md:grid-cols-2 bg-[#f3f3f1]">
            {/* Left hero panel - operational brand presence */}
            <div className="hidden md:block relative bg-white border-r border-black/[0.08]">
                <div
                    className="absolute inset-0"
                    style={{
                        backgroundImage:
                            "url('https://images.pexels.com/photos/6699772/pexels-photo-6699772.jpeg')",
                        backgroundSize: "cover",
                        backgroundPosition: "center",
                    }}
                />
                <div className="absolute inset-0 bg-white/85" />
                <div className="relative h-full flex flex-col items-center justify-center p-12">
                    <BrandHero size="md" />
                </div>
            </div>

            {/* Right panel - login form */}
            <div className="flex items-center justify-center p-6 md:p-12">
                <div className="w-full max-w-md">
                    {/* Mobile-only brand block — desktop has the left panel.
                        Keeps brand continuity on phones where the panel is hidden. */}
                    <div
                        className="md:hidden flex flex-col items-center text-center mb-8"
                        data-testid="admin-login-mobile-brand"
                    >
                        <BrandHero size="md" />
                    </div>

                    <div className="bg-white border border-black/[0.08] rounded-xl p-6 md:p-8">
                        <form
                            onSubmit={submit}
                            data-testid="admin-login-form"
                        >
                            <p className="eyebrow mb-3">Secure Access</p>
                            <h1 className="font-display text-2xl md:text-3xl tracking-tight text-black/90 mb-6">
                                Sign in to Talentgram
                            </h1>

                            <label className="block mb-5">
                                <span className="text-xs text-black/60 tracking-wide">
                                    Email
                                </span>
                                <input
                                    type="email"
                                    value={email}
                                    data-testid="login-email-input"
                                    onChange={(e) => setEmail(e.target.value)}
                                    required
                                    className="mt-2 w-full bg-[#fafaf8] border border-black/[0.08] focus:border-black/[0.16] rounded-lg outline-none py-2.5 px-3 text-sm text-black/85 placeholder:text-black/30 transition-colors duration-150"
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
                                    className="mt-2 w-full bg-[#fafaf8] border border-black/[0.08] focus:border-black/[0.16] rounded-lg outline-none py-2.5 px-3 text-sm text-black/85 placeholder:text-black/30 transition-colors duration-150"
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
                            <div className="flex items-center justify-end mt-4">
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
        </div>
    );
}
