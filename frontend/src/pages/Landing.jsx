import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import BrandHero from "@/components/BrandHero";

/**
 * Landing surface — pure brand statement.
 *  · Logo as the hero (large, centred)
 *  · "WE SCOUT · WE MANAGE  /  INDIA | UAE" tagline directly under it
 *  · Two CTAs below: Enter Dashboard + Apply as Talent
 *  · Header keeps only utility actions (apply shortcut + sign-in).
 *
 * No marketing copy, no background image. Quiet by design.
 * Premium brand infrastructure — not cinematic SaaS or enterprise ATS.
 */
export default function Landing() {
    return (
        <div
            className="min-h-screen bg-[#151514] text-white relative overflow-hidden"
            data-testid="landing-page"
        >
            {/* Subtle radial vignette — refined, less dramatic */}
            <div className="absolute inset-0 pointer-events-none opacity-[0.35]"
                 style={{
                     background:
                         "radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.25) 65%, rgba(0,0,0,0.38) 100%)",
                 }}
            />

            <div className="relative z-10 min-h-screen flex flex-col">
                <header className="px-6 md:px-12 py-6 flex items-center justify-end gap-3">
                    <Link
                        to="/apply"
                        data-testid="landing-apply-link"
                        className="hidden sm:inline-flex items-center gap-1.5 text-sm text-white/70 hover:text-white transition-colors duration-150 px-4 py-2 border border-white/20 rounded-lg hover:border-white/50"
                    >
                        <Sparkles className="w-3.5 h-3.5" />
                        Apply as Talent
                    </Link>
                    <Link
                        to="/admin/login"
                        data-testid="landing-login-btn"
                        className="text-sm text-white/70 hover:text-white transition-colors duration-150 px-4 py-2 border border-white/20 rounded-lg hover:border-white/50"
                    >
                        Admin Sign in
                    </Link>
                </header>

                <main className="flex-1 flex flex-col items-center justify-center px-6 md:px-12">
                    <div className="flex flex-col items-center w-full max-w-2xl">
                        <BrandHero size="lg" />

                        <div className="mt-12 flex items-center gap-4 flex-wrap justify-center">
                            <Link
                                to="/admin/login"
                                data-testid="landing-cta-btn"
                                className="inline-flex items-center gap-2 bg-white text-black px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 transition-colors duration-150"
                            >
                                Enter Dashboard
                                <ArrowRight
                                    className="w-4 h-4"
                                    strokeWidth={1.5}
                                />
                            </Link>
                            <Link
                                to="/apply"
                                data-testid="landing-apply-card"
                                className="inline-flex items-center gap-2 px-6 py-3 border border-white/25 hover:border-white/50 text-white/85 hover:text-white text-sm rounded-lg transition-colors duration-150"
                            >
                                <Sparkles className="w-3.5 h-3.5" />
                                Apply as Talent
                            </Link>
                        </div>
                    </div>
                </main>

                <footer className="px-6 md:px-12 py-6 flex items-center justify-between text-[11px] tracking-[0.08em] uppercase text-white/40">
                    <span>v1 · Phase 1 — Client Review</span>
                    <span>© Talentgram</span>
                </footer>
            </div>
        </div>
    );
}
