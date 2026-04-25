import React from "react";
import Logo from "@/components/Logo";

/**
 * BrandHero — the centered logo + dual-line tagline that anchors the
 * Landing and Admin Login surfaces.
 *
 *   [ Talentgram logo, hero size ]
 *   ───────────────
 *   WE SCOUT  ·  WE MANAGE
 *      INDIA  |  UAE
 *
 * Typography rationale:
 *   • The logo asset already swaps between black/white ink by theme
 *     (handled inside <Logo />), so the surrounding text only needs to
 *     follow `text-foreground` opacity tiers — Tailwind classes adapt
 *     to light/dark automatically via `index.css` overrides.
 *   • The two tagline rows lean on the same monospace + serif duo used
 *     across the app (`tg-mono` and `font-display`) so nothing competes
 *     with the logo's serif weight.
 *
 * Sizes:
 *   md  → admin-login left rail (compact)
 *   lg  → landing hero (full-bleed feel)
 *
 * Props:
 *   inverted — forces white logo + white text regardless of the active
 *              theme. Used on surfaces that always paint over a dark
 *              backdrop (e.g. admin-login's left rail with dark image).
 */
export default function BrandHero({ size = "lg", inverted = false, className = "" }) {
    const logoH = size === "md" ? 140 : 220;
    const gap = size === "md" ? "mt-7" : "mt-9";
    const wScout = size === "md" ? "text-[11px]" : "text-xs";
    const wRegion =
        size === "md"
            ? "text-base md:text-lg"
            : "text-lg md:text-xl";

    return (
        <div
            className={`flex flex-col items-center text-center ${inverted ? "text-white" : ""} ${className}`}
            data-testid="brand-hero"
        >
            <Logo size={logoH} className="mx-auto" forceVariant={inverted ? "white" : undefined} />

            {/* Hairline separator — colour follows current text opacity tier
                so it works in both day and night without a hard-coded value. */}
            <div
                className={`${gap} h-px w-12 bg-current opacity-25`}
                aria-hidden="true"
            />

            <p
                className={`tg-mono ${wScout} mt-5 tracking-[0.4em] uppercase opacity-60`}
                data-testid="brand-hero-tagline-1"
            >
                We Scout&nbsp;&nbsp;·&nbsp;&nbsp;We Manage
            </p>
            <p
                className={`font-display ${wRegion} mt-2 tracking-[0.25em] uppercase`}
                data-testid="brand-hero-tagline-2"
            >
                India&nbsp;&nbsp;|&nbsp;&nbsp;UAE
            </p>
        </div>
    );
}
