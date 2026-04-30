import React from "react";

/**
 * Talentgram brand logo — uses the original uploaded asset directly.
 *
 * v38f — Light-mode-only. Default ink is BLACK (talentgram-black.png) for
 * the standard light surfaces. Callers that paint the logo over a dark
 * hero photo (e.g. a tinted cover image) can override with
 * `forceVariant="white"` to use the white-ink asset.
 *
 * Sizes (height in px) — width auto-derives from intrinsic aspect ratio
 * (≈ 1413 × 711 → 1.99:1):
 *   sm  = 24    (compact nav / footer)
 *   md  = 40    (sidebar / form headers)
 *   lg  = 64    (modal hero, mid-section anchors)
 *   xl  = 110   (mobile hero per spec — width ≈ 110 × 1.99 ≈ 220px)
 *   2xl = 150   (desktop hero per spec — width ≈ 150 × 1.99 ≈ 300px)
 *
 * A subtle drop-shadow is applied so the logo never blends into adjacent
 * surfaces of similar luminance.
 */
const SIZES = {
    sm: 24,
    md: 40,
    lg: 64,
    xl: 110,
    "2xl": 150,
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
