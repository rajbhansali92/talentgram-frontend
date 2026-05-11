import React from "react";

/**
 * Cinematic board section helpers — pure layout, no state.
 *   BoardSection: eyebrow + helper + optional faint top divider.
 *   BoardRow:     horizontally scrolling flex strip + custom scrollbar.
 */

export function BoardSection({
    eyebrow,
    helper,
    children,
    muted = false,
    divider = false,
}) {
    return (
        <section
            className={`mt-10 ${divider ? "pt-10 border-t border-white/[0.05]" : ""}`}
        >
            <div className="flex items-baseline justify-between mb-4 px-1">
                <h3
                    className={`text-[10px] tracking-[0.28em] uppercase font-medium ${
                        muted ? "text-white/40" : "text-white/70"
                    }`}
                >
                    {eyebrow}
                </h3>
                {helper && (
                    <span className="text-[10px] tg-mono text-white/30 hidden sm:inline">
                        {helper}
                    </span>
                )}
            </div>
            {children}
        </section>
    );
}

export function BoardRow({ children, testid }) {
    // Horizontal scroll mechanism — pure CSS, no library. Columns set their
    // own fixed widths; `flex-nowrap + overflow-x-auto` does the rest.
    // Snap points only on small viewports so swiping feels deliberate on
    // mobile; on desktop free-scroll feels more cinematic.
    return (
        <div
            data-testid={testid}
            className="
                flex gap-4 pb-3
                overflow-x-auto tg-pipeline-scroll
                flex-nowrap
                snap-x snap-mandatory md:snap-none
                -mx-1 px-1
            "
            style={{ scrollBehavior: "smooth" }}
        >
            {React.Children.map(children, (child, idx) => (
                <div key={idx} className="snap-start md:snap-none shrink-0">
                    {child}
                </div>
            ))}
        </div>
    );
}
