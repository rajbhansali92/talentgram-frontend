import React from "react";
import { useTheme } from "@/lib/theme";

/**
 * Talentgram brand logo — uses the original uploaded asset directly.
 * Two transparent PNG variants (black ink / white ink) are swapped by theme.
 * No CSS filters, no SVG recreation — pixel-perfect to source.
 *
 * size controls only height; width auto-derives from intrinsic aspect ratio
 * (≈ 1413 × 711 → 1.99:1).
 */
const SIZES = {
    sm: 22,
    md: 36,
    lg: 56,
    xl: 96,
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
            }}
        />
    );
}
