import React from "react";

/**
 * Talentgram brand logo — uses the original uploaded asset directly.
 *
 * v38f — Light-mode-only. Default ink is BLACK (talentgram-black.png) for
 * the standard light surfaces. Callers that paint the logo over a dark
 * hero photo (e.g. a tinted cover image) can override with
 * `forceVariant="white"` to use the white-ink asset.
 *
 * v38g — New brand asset (user-uploaded, high-res). Aspect ratio is now
 * ≈ 1.435:1 (1155 × 805) — taller per unit width than the previous
 * wordmark, so size calls below were nudged upward for stronger presence
 * at the same visual weight.
 *
 * Sizes (height in px) — width auto-derives from aspect ratio:
 *   sm  = 28    (compact nav / footer)         → ~40 px wide
 *   md  = 48    (sidebar / form headers)        → ~69 px wide
 *   lg  = 72    (modal hero, mid-section)       → ~103 px wide
 *   xl  = 130   (mobile hero per spec)          → ~187 px wide
 *   2xl = 180   (desktop hero per spec)         → ~258 px wide
 *
 * A subtle drop-shadow is applied so the logo never blends into adjacent
 * surfaces of similar luminance.
 */
const SIZES = {
    sm: 28,
    md: 48,
    lg: 72,
    xl: 130,
    "2xl": 180,
};

const SRC_BLACK = "/brand/talentgram-black.png"; // black ink → for light backgrounds (default)
const SRC_WHITE = "/brand/talentgram-white.png"; // white ink → only when explicitly forced

export default function Logo({ size = "md", className = "", forceVariant = undefined }) {
    const h = typeof size === "number" ? size : SIZES[size] || SIZES.md;
    const useWhite = forceVariant === "white";
    const src = useWhite ? SRC_WHITE : SRC_BLACK;
    // Drop-shadow halo points toward the OPPOSITE colour so the logo
    // separates from any near-luminance background (e.g. white-ink over
    // a dim hero photo, or black-ink over an off-white card).
    const shadow = useWhite
        ? "drop-shadow(0 1px 2px rgba(0,0,0,0.55))"
        : "drop-shadow(0 1px 2px rgba(255,255,255,0.6))";
    return (
        <img
            src={src}
            alt="Talentgram"
            data-testid="brand-logo"
            draggable={false}
            decoding="async"
            className={`block select-none ${className}`}
            style={{
                height: `${h}px`,
                width: "auto",
                maxHeight: `${h}px`,
                objectFit: "contain",
                filter: shadow,
            }}
        />
    );
}
