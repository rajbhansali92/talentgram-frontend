import React from "react";
import { useTheme } from "@/lib/theme";

/**
 * Talentgram brand logo — uses the original uploaded asset directly.
 * Two transparent PNG variants (black ink / white ink) are swapped by theme.
 * No CSS filters, no SVG recreation — pixel-perfect to source.
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
 * surfaces (e.g. white-ink logo on a near-black hero photo).
 */
const SIZES = {
    sm: 24,
    md: 40,
    lg: 64,
    xl: 110,
    "2xl": 150,
};

const SRC_DARK = "/brand/talentgram-white.png"; // white ink → for dark backgrounds
const SRC_LIGHT = "/brand/talentgram-black.png"; // black ink → for light backgrounds

export default function Logo({ size = "md", className = "", forceVariant = undefined }) {
    const { isLight } = useTheme();
    const h = typeof size === "number" ? size : SIZES[size] || SIZES.md;
    const useLight = forceVariant
        ? forceVariant === "black"
        : isLight;
    const src = useLight ? SRC_LIGHT : SRC_DARK;
    // Drop-shadow fallback: a faint contrast halo so the logo always
    // separates from the surface, even on edge-case backgrounds (e.g.
    // when a hero photo loads slowly and the logo paints on near-same
    // luminance). Direction is inverted per ink so the halo is always
    // toward the OPPOSITE colour.
    const shadow = useLight
        ? "drop-shadow(0 1px 2px rgba(255,255,255,0.6))"
        : "drop-shadow(0 1px 2px rgba(0,0,0,0.55))";
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
