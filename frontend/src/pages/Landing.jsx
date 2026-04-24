import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";

export default function Landing() {
    return (
        <div className="min-h-screen bg-[#050505] text-white relative overflow-hidden">
            {/* Background image */}
            <div
                className="absolute inset-0 opacity-40"
                style={{
                    backgroundImage:
                        "url('https://images.pexels.com/photos/15128321/pexels-photo-15128321.jpeg')",
                    backgroundSize: "cover",
                    backgroundPosition: "center",
                }}
            />
            <div className="absolute inset-0 bg-gradient-to-b from-black/30 via-black/70 to-black" />

            <div className="relative z-10 min-h-screen flex flex-col">
                <header className="px-6 md:px-12 py-6 flex items-center justify-between">
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
                    <Link
                        to="/admin/login"
                        data-testid="landing-login-btn"
                        className="text-sm text-white/70 hover:text-white transition-all px-4 py-2 border border-white/20 rounded-sm hover:border-white"
                    >
                        Admin Sign in
                    </Link>
                </header>

                <div className="flex-1 flex items-center px-6 md:px-16 max-w-6xl">
                    <div className="tg-fade-up">
                        <p className="eyebrow mb-8">
                            Talentgram × Portfolio Engine
                        </p>
                        <h1 className="font-display text-4xl sm:text-5xl lg:text-7xl leading-[0.95] tracking-tight mb-8">
                            Curated portfolios.
                            <br />
                            <span className="text-white/50">
                                Decisive presentations.
                            </span>
                        </h1>
                        <p className="text-white/60 max-w-xl text-base md:text-lg font-light leading-relaxed mb-12">
                            A decision-focused client review system for casting
                            teams, brand agencies and production houses.
                            Generate clean, secure web links, track engagement,
                            and close selections faster.
                        </p>
                        <div className="flex items-center gap-4 flex-wrap">
                            <Link
                                to="/admin/login"
                                data-testid="landing-cta-btn"
                                className="inline-flex items-center gap-2 bg-white text-black px-6 py-3.5 rounded-sm text-sm font-medium hover:opacity-90 transition-all"
                            >
                                Enter Dashboard
                                <ArrowRight
                                    className="w-4 h-4"
                                    strokeWidth={1.5}
                                />
                            </Link>
                            <div className="eyebrow">
                                v1 · Phase 1 — Client Review
                            </div>
                        </div>
                    </div>
                </div>

                <footer className="px-6 md:px-12 py-6 border-t border-white/10 flex items-center justify-between text-xs text-white/40">
                    <span>Netflix meets Casting — built for decisions.</span>
                    <span>© Talentgram</span>
                </footer>
            </div>
        </div>
    );
}
