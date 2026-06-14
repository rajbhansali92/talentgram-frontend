import React from "react";
import { ArrowRight, Sparkles } from "lucide-react";
import Logo from "@/components/Logo";
import { getSubdomainUrl } from "@/lib/api";

/**
 * Landing surface — pure brand statement.
 *  · Standardized monochrome Logo (size 120, centered)
 *  · Two CTAs below: Enter Dashboard + Apply as Talent
 *  · Quiet, premium white luxury branding.
 */
export default function Landing() {
    return (
        <div
            className="min-h-screen bg-[#ffffff] text-black relative overflow-hidden"
            data-testid="landing-page"
        >
            <div className="relative z-10 min-h-screen flex flex-col">
                <header className="px-6 md:px-12 py-6 flex items-center justify-end gap-3">
                    <a
                        href={getSubdomainUrl("apply")}
                        data-testid="landing-apply-link"
                        className="hidden sm:inline-flex items-center gap-1.5 text-sm text-black/70 hover:text-black transition-colors duration-150 px-4 py-2 border border-black/15 rounded-lg hover:border-black/40"
                    >
                        <Sparkles className="w-3.5 h-3.5" />
                        Apply as Talent
                    </a>
                    <a
                        href={getSubdomainUrl("review") + "/admin/login"}
                        data-testid="landing-login-btn"
                        className="text-sm text-black/70 hover:text-black transition-colors duration-150 px-4 py-2 border border-black/15 rounded-lg hover:border-black/40"
                    >
                        Admin Sign in
                    </a>
                </header>

                <main className="flex-1 flex flex-col items-center justify-center px-6 md:px-12">
                    <div className="flex flex-col items-center w-full max-w-2xl">
                        <Logo size={120} className="mx-auto mb-12" forceVariant="black" />

                        <div className="flex items-center gap-4 flex-wrap justify-center">
                            <a
                                href={getSubdomainUrl("review") + "/admin/login"}
                                data-testid="landing-cta-btn"
                                className="inline-flex items-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 transition-all duration-150"
                            >
                                Enter Dashboard
                                <ArrowRight
                                    className="w-4 h-4"
                                    strokeWidth={1.5}
                                />
                            </a>
                            <a
                                href={getSubdomainUrl("apply")}
                                data-testid="landing-apply-card"
                                className="inline-flex items-center gap-2 px-6 py-3 border border-black/15 hover:border-black/40 text-black/85 hover:text-black text-sm rounded-lg transition-colors duration-150"
                            >
                                <Sparkles className="w-3.5 h-3.5" />
                                Apply as Talent
                            </a>
                        </div>
                    </div>
                </main>

                <footer className="px-6 md:px-12 py-6 flex items-center justify-between text-[11px] tracking-[0.08em] uppercase text-black/45">
                    <span>v1 · Phase 1 — Client Review</span>
                    <span>© Talentgram</span>
                </footer>
            </div>
        </div>
    );
}
