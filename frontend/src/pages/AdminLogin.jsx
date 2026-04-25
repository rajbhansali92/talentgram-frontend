import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { adminApi, saveAdminSession } from "@/lib/api";
import ThemeToggle from "@/components/ThemeToggle";
import BrandHero from "@/components/BrandHero";
import { Loader2 } from "lucide-react";

export default function AdminLogin() {
    const [email, setEmail] = useState("admin@talentgram.com");
    const [password, setPassword] = useState("Admin@123");
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
        <div className="min-h-screen grid md:grid-cols-2 bg-[#050505] relative">
            <div className="absolute top-5 right-5 z-20">
                <ThemeToggle />
            </div>
            <div className="hidden md:block relative">
                <div
                    className="absolute inset-0"
                    style={{
                        backgroundImage:
                            "url('https://images.pexels.com/photos/6699772/pexels-photo-6699772.jpeg')",
                        backgroundSize: "cover",
                        backgroundPosition: "center",
                    }}
                />
                <div className="absolute inset-0 bg-gradient-to-br from-black/55 via-black/65 to-black/85" />
                <div className="relative h-full flex flex-col items-center justify-center p-12">
                    <BrandHero size="md" inverted />
                </div>
            </div>

            <div className="flex items-center justify-center p-6 md:p-12">
                <form
                    onSubmit={submit}
                    className="w-full max-w-sm tg-fade-up"
                    data-testid="admin-login-form"
                >
                    {/* Mobile-only brand block — desktop has the dark left rail.
                        Keeps brand continuity on phones where the rail is hidden. */}
                    <div
                        className="md:hidden flex flex-col items-center text-center mb-10"
                        data-testid="admin-login-mobile-brand"
                    >
                        <BrandHero size="md" />
                    </div>

                    <p className="eyebrow mb-6">Admin Access</p>
                    <h1 className="font-display text-3xl md:text-4xl tracking-tight mb-10">
                        Sign in to Talentgram.
                    </h1>

                    <label className="block mb-5">
                        <span className="text-xs text-white/60 tracking-wide">
                            Email
                        </span>
                        <input
                            type="email"
                            value={email}
                            data-testid="login-email-input"
                            onChange={(e) => setEmail(e.target.value)}
                            required
                            className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 text-white placeholder:text-white/30"
                        />
                    </label>
                    <label className="block mb-8">
                        <span className="text-xs text-white/60 tracking-wide">
                            Password
                        </span>
                        <input
                            type="password"
                            value={password}
                            data-testid="login-password-input"
                            onChange={(e) => setPassword(e.target.value)}
                            required
                            className="mt-2 w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 text-white placeholder:text-white/30"
                        />
                    </label>
                    <button
                        type="submit"
                        disabled={loading}
                        data-testid="login-submit-btn"
                        className="w-full bg-white text-black py-3.5 rounded-sm text-sm font-medium hover:opacity-90 transition-all inline-flex items-center justify-center gap-2"
                    >
                        {loading && (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        )}
                        Enter Dashboard
                    </button>
                    <div className="flex items-center justify-between mt-4">
                        <p className="text-[11px] text-white/40 tg-mono">
                            Default · admin@talentgram.com · Admin@123
                        </p>
                        <a
                            href="/forgot-password"
                            data-testid="login-forgot-link"
                            className="text-[11px] text-white/60 hover:text-white underline-offset-4 hover:underline"
                        >
                            Forgot password?
                        </a>
                    </div>
                </form>
            </div>
        </div>
    );
}
