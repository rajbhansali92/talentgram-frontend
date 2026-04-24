import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { adminApi, saveAdminSession } from "@/lib/api";
import { Sparkles, Loader2 } from "lucide-react";

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
        <div className="min-h-screen grid md:grid-cols-2 bg-[#050505]">
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
                <div className="absolute inset-0 bg-gradient-to-br from-black/40 to-black/80" />
                <div className="relative h-full flex flex-col justify-between p-12 text-white">
                    <div className="flex items-center gap-2">
                        <div className="w-7 h-7 rounded-sm bg-white flex items-center justify-center">
                            <Sparkles
                                className="w-4 h-4 text-black"
                                strokeWidth={1.5}
                            />
                        </div>
                        <span className="font-display text-xl tracking-tight">
                            Talentgram
                        </span>
                    </div>
                    <div>
                        <p className="eyebrow mb-4">Client Review System</p>
                        <h2 className="font-display text-4xl lg:text-5xl leading-tight tracking-tight">
                            Curated decisions.
                            <br />
                            <span className="text-white/50">
                                Quietly powerful.
                            </span>
                        </h2>
                    </div>
                </div>
            </div>

            <div className="flex items-center justify-center p-6 md:p-12">
                <form
                    onSubmit={submit}
                    className="w-full max-w-sm tg-fade-up"
                    data-testid="admin-login-form"
                >
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
                    <p className="text-[11px] text-white/40 mt-6 tg-mono">
                        Default · admin@talentgram.com · Admin@123
                    </p>
                </form>
            </div>
        </div>
    );
}
