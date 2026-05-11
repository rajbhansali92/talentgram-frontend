import React, { memo } from "react";

/**
 * TalentAvatar — premium cinematic avatar with initial-letter fallback.
 *
 * Sizes:
 *   sm — 24px (legacy thumb, used in SearchResultRow / bulk-mode cards)
 *   md — 44px (default Card avatar)
 *   lg — 56px (reserved for compact-mode hero rows)
 *
 * Initial is computed once; fallback tile uses a soft radial gradient
 * so the empty state still reads "premium", not "broken image".
 */
const TalentAvatar = memo(function TalentAvatar({ src, name, size = "md" }) {
    const initial = (name || "?").trim().charAt(0).toUpperCase() || "?";
    const dims =
        size === "sm"
            ? "w-6 h-6 text-[10px] rounded"
            : size === "lg"
              ? "w-14 h-14 text-base rounded-xl"
              : "w-11 h-11 text-sm rounded-lg";

    if (src) {
        return (
            <img
                src={src}
                alt=""
                loading="lazy"
                className={`${dims} object-cover shrink-0 bg-white/5 ring-1 ring-white/10 shadow-[0_4px_12px_-4px_rgba(0,0,0,0.6)]`}
            />
        );
    }
    return (
        <div
            aria-hidden
            className={`${dims} shrink-0 flex items-center justify-center font-medium text-white/75
                bg-gradient-to-br from-white/[0.08] to-white/[0.02]
                ring-1 ring-white/10
                shadow-[0_4px_12px_-4px_rgba(0,0,0,0.6),inset_0_1px_0_0_rgba(255,255,255,0.05)]`}
        >
            {initial}
        </div>
    );
});

export default TalentAvatar;
