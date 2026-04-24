import React from "react";

/**
 * Talentgram brand logo — wordmark with two diagonal slashes (top-right + bottom-left)
 * forming the signature off-axis frame. Uses currentColor → automatically
 * adapts to light/dark theme via the parent's text color.
 *
 * size: 'sm' (chrome/header) | 'md' (default) | 'lg' (hero)
 */
export default function Logo({ size = "md", className = "", showSlashes = true }) {
    const sizes = {
        sm: { wordmark: "text-[13px]", slash: "h-3.5" },
        md: { wordmark: "text-base", slash: "h-4" },
        lg: { wordmark: "text-2xl md:text-3xl", slash: "h-7 md:h-8" },
        xl: { wordmark: "text-4xl md:text-6xl", slash: "h-12 md:h-16" },
    };
    const s = sizes[size] || sizes.md;
    return (
        <div
            data-testid="brand-logo"
            className={`relative inline-flex flex-col items-center justify-center leading-none select-none ${className}`}
            aria-label="Talentgram"
        >
            {showSlashes && (
                <Slash
                    className={`${s.slash} self-end -mb-1 mr-[8%] opacity-90`}
                />
            )}
            <span
                className={`font-logo text-current ${s.wordmark} whitespace-nowrap`}
                style={{ lineHeight: 1 }}
            >
                Talentgram
            </span>
            {showSlashes && (
                <Slash
                    className={`${s.slash} self-start -mt-1 ml-[8%] opacity-90`}
                />
            )}
        </div>
    );
}

function Slash({ className = "" }) {
    return (
        <svg
            viewBox="0 0 12 40"
            preserveAspectRatio="none"
            className={`block ${className}`}
            aria-hidden="true"
        >
            <line
                x1="11"
                y1="0"
                x2="1"
                y2="40"
                stroke="currentColor"
                strokeWidth="1.2"
                strokeLinecap="round"
            />
        </svg>
    );
}
