import React, { useEffect, useState } from "react";

/**
 * BrandSplash — premium full-screen brand intro for first-time entries.
 *
 * Behaviour:
 *   1. Mounts INSTANTLY at z-9999 over a cream surface.
 *   2. Logo fades + scales in (≈400ms).
 *   3. Holds at full opacity for ~1000ms.
 *   4. Whole overlay fades out (≈400ms) and unmounts.
 *   5. `sessionStorage` flag prevents replays on internal navigation
 *      within the same tab session (closing/reopening replays it).
 *
 * Critical guarantees:
 *   - The splash NEVER blocks data fetching. Pages that mount this still
 *     run their `useEffect` data loaders in parallel — by the time the
 *     splash finishes fading out (~1.5s total), the page below is fully
 *     hydrated, so the reveal is seamless (no spinner flash).
 *   - Pure CSS transitions, no animation libraries. GPU-accelerated
 *     opacity + transform → smooth on iPhone SE / mid-range Android.
 *   - `pointer-events: none` during fade-out so users can interact with
 *     the page underneath the moment the curtain starts lifting.
 *
 * Per-route gating: the parent page decides whether to render this. We
 * only mount it on public surfaces (ClientView /l/, SubmissionPage /s/,
 * ApplicationPage /apply). Admin internal nav never sees it.
 */

const SS_KEY = "tg_brand_splash_v1"; // bump key to force re-show on schema change

const SPLASH_TIMINGS = {
    fadeIn: 400, // ms
    hold: 1000, // ms
    fadeOut: 400, // ms
};

export default function BrandSplash({ enabled = true }) {
    // 4-state machine: pre → in → hold → out → hidden
    //   pre  — initial paint (opacity:0, ready to transition)
    //   in   — opacity:1 (CSS transition ramps up)
    //   hold — opacity:1 stable
    //   out  — opacity:0 (CSS transition ramps down)
    //   hidden — unmounted
    const [phase, setPhase] = useState(() => {
        if (!enabled) return "hidden";
        if (typeof window === "undefined") return "hidden";
        try {
            if (sessionStorage.getItem(SS_KEY)) return "hidden";
        } catch {
            // storage unavailable (private mode) — show splash, just won't dedupe
        }
        return "pre";
    });

    useEffect(() => {
        if (phase === "hidden") return undefined;
        // Mark seen so internal nav within this tab session skips replay.
        try {
            sessionStorage.setItem(SS_KEY, "1");
        } catch {
            // non-fatal
        }
        const timers = [];
        // Two RAFs ensure the initial opacity:0 paints before we flip it
        // to 1. Without this, React batches the state set and we'd skip
        // the CSS transition entirely.
        const raf = requestAnimationFrame(() => {
            const raf2 = requestAnimationFrame(() => {
                setPhase("in");
                timers.push(setTimeout(() => setPhase("hold"), SPLASH_TIMINGS.fadeIn));
                timers.push(
                    setTimeout(
                        () => setPhase("out"),
                        SPLASH_TIMINGS.fadeIn + SPLASH_TIMINGS.hold,
                    ),
                );
                timers.push(
                    setTimeout(
                        () => setPhase("hidden"),
                        SPLASH_TIMINGS.fadeIn + SPLASH_TIMINGS.hold + SPLASH_TIMINGS.fadeOut,
                    ),
                );
            });
            timers.push({ cancel: () => cancelAnimationFrame(raf2) });
        });
        return () => {
            cancelAnimationFrame(raf);
            timers.forEach((t) => {
                if (typeof t === "number") clearTimeout(t);
                else if (t && typeof t.cancel === "function") t.cancel();
            });
        };
        // Only run once on mount — phase is intentionally an initial-only deciders here.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    if (phase === "hidden") return null;

    const opacity = phase === "in" || phase === "hold" ? 1 : 0;

    return (
        <div
            role="presentation"
            aria-hidden="true"
            data-testid="brand-splash"
            data-splash-phase={phase}
            style={{
                position: "fixed",
                inset: 0,
                zIndex: 9999,
                backgroundColor: "#fafaf7",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                opacity,
                transition: `opacity ${phase === "out" ? SPLASH_TIMINGS.fadeOut : SPLASH_TIMINGS.fadeIn}ms cubic-bezier(0.22, 1, 0.36, 1)`,
                pointerEvents: phase === "out" ? "none" : "auto",
                // willChange hints GPU compositing for the cheap fade.
                willChange: "opacity",
            }}
        >
            <img
                src="/brand/talentgram-black.png"
                alt="Talentgram"
                draggable={false}
                decoding="sync"
                fetchpriority="high"
                style={{
                    // 28vmin keeps it visually consistent on phones + desktops.
                    // Cap at 240px so 4K monitors don't render an oversized hero.
                    height: "min(240px, 28vmin)",
                    width: "auto",
                    opacity: phase === "in" || phase === "hold" ? 1 : 0,
                    transform:
                        phase === "in" || phase === "hold"
                            ? "scale(1)"
                            : "scale(0.97)",
                    transition:
                        "opacity 500ms cubic-bezier(0.22, 1, 0.36, 1), transform 600ms cubic-bezier(0.22, 1, 0.36, 1)",
                    filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.06))",
                    willChange: "opacity, transform",
                }}
            />
        </div>
    );
}
