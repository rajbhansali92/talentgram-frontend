import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import BrandHero from "@/components/BrandHero";

/**
 * Landing surface — pure brand statement.
 *  · Logo as the hero (large, centred)
 *  · "WE SCOUT · WE MANAGE  /  INDIA | UAE" tagline directly under it
 *  · Two CTAs below: Enter Dashboard + Apply as Talent
 *  · Header keeps only utility actions (sign-in shortcut + theme toggle).
 *
 * No marketing copy, no background image. Quiet by design.
 */
export default function Landing() {
    return (
        <div
            className="min-h-screen bg-[#050505] text-white relative overflow-hidden"
            data-testid="landing-page"
        >
            {/* Subtle radial vignette — works in both day & night */}
            <div className="absolute inset-0 pointer-events-none opacity-[0.55]"
                 style={{
                     background:
                         "radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.35) 65%, rgba(0,0,0,0.65) 100%)",
                 }}
            />

            <div className="relative z-10 min-h-screen flex flex-col">
                <header className="px-6 md:px-12 py-6 flex items-center justify-end gap-3">
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
                </header>

                <main className="flex-1 flex flex-col items-center justify-center px-6 md:px-12">
                    <div className="tg-fade-up flex flex-col items-center w-full max-w-2xl">
                        <BrandHero size="lg" />

                        <div className="mt-12 flex items-center gap-4 flex-wrap justify-center">
                            <Link
                                to="/admin/login"
                                data-testid="landing-cta-btn"
                                className="inline-flex items-center gap-2 bg-white text-black px-7 py-3.5 rounded-sm text-sm font-medium hover:opacity-90 transition-all"
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
                                className="inline-flex items-center gap-2 px-7 py-3.5 border border-white/25 hover:border-white text-white/85 hover:text-white text-sm rounded-sm transition-all"
                            >
                                <Sparkles className="w-3.5 h-3.5" />
                                Apply as Talent
                            </Link>
                        </div>
                    </div>
                </main>

                <footer className="px-6 md:px-12 py-6 flex items-center justify-between text-[11px] tg-mono text-white/40">
                    <span>v1 · Phase 1 — Client Review</span>
                    <span>© Talentgram</span>
                </footer>
            </div>
        </div>
    );
}
