import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import ThemeToggle from "@/components/ThemeToggle";
import Logo from "@/components/Logo";

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
                    <Logo size="md" />
                    <div className="flex items-center gap-3">
                        <Link
                            to="/apply"
                            data-testid="landing-apply-link"
                            className="hidden sm:inline-flex items-center gap-1.5 text-sm text-white/70 hover:text-white transition-all px-4 py-2 border border-white/20 rounded-sm hover:border-white"
                        >
                            <Sparkles className="w-3.5 h-3.5" />
                            Apply as Talent
                        </Link>
                        <Link
                            to="/admin/login"
                            data-testid="landing-login-btn"
                            className="text-sm text-white/70 hover:text-white transition-all px-4 py-2 border border-white/20 rounded-sm hover:border-white"
                        >
                            Admin Sign in
                        </Link>
                        <ThemeToggle />
                    </div>
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
                        <div className="flex items-center gap-4 flex-wrap mb-10">
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

                        {/* Public applicant CTA card */}
                        <Link
                            to="/apply"
                            data-testid="landing-apply-card"
                            className="group inline-flex items-center gap-5 max-w-xl p-5 border border-white/15 hover:border-white/50 bg-white/[0.02] backdrop-blur-sm transition-all"
                        >
                            <div className="w-10 h-10 rounded-full bg-white/10 flex items-center justify-center shrink-0">
                                <Sparkles className="w-4 h-4 text-white" />
                            </div>
                            <div className="flex-1 min-w-0">
                                <div className="text-[10px] tracking-widest uppercase text-white/40 mb-1">
                                    Are you a talent?
                                </div>
                                <div className="font-display text-lg leading-tight">
                                    Apply to join Talentgram
                                </div>
                                <div className="text-xs text-white/50 mt-1">
                                    Submit once — get considered for every
                                    brand, film, and campaign we cast.
                                </div>
                            </div>
                            <ArrowRight className="w-4 h-4 text-white/40 group-hover:text-white group-hover:translate-x-1 transition-all" />
                        </Link>
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
