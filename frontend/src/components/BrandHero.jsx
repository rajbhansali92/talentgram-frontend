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
    // Per-size base height + a responsive cap on small screens so the hero
    // never pushes CTAs below the fold on iPhone SE (320–375 px wide).
    const config =
        size === "md"
            ? { logoH: 110, mdLogoH: 140, gap: "mt-6", scout: "text-[11px]", region: "text-base sm:text-lg" }
            : { logoH: 140, mdLogoH: 220, gap: "mt-7 sm:mt-9", scout: "text-[11px] sm:text-xs", region: "text-base sm:text-lg md:text-xl" };

    return (
        <div
            className={`flex flex-col items-center text-center ${inverted ? "text-white" : ""} ${className}`}
            data-testid="brand-hero"
        >
            {/* Render two logos and let CSS pick — keeps the JSX stable across breakpoints */}
            <div className="block sm:hidden">
                <Logo size={config.logoH} className="mx-auto" forceVariant={inverted ? "white" : undefined} />
            </div>
            <div className="hidden sm:block">
                <Logo size={config.mdLogoH} className="mx-auto" forceVariant={inverted ? "white" : undefined} />
            </div>

            {/* Hairline separator — colour follows current text opacity tier
                so it works in both day and night without a hard-coded value. */}
            <div
                className={`${config.gap} h-px w-12 bg-current opacity-25`}
                aria-hidden="true"
            />

            <p
                className={`tg-mono ${config.scout} mt-5 tracking-[0.4em] uppercase opacity-60`}
                data-testid="brand-hero-tagline-1"
            >
                We Scout&nbsp;&nbsp;·&nbsp;&nbsp;We Manage
            </p>
            <p
                className={`font-display ${config.region} mt-2 tracking-[0.25em] uppercase`}
                data-testid="brand-hero-tagline-2"
            >
                India&nbsp;&nbsp;|&nbsp;&nbsp;UAE
            </p>
        </div>
    );
}
