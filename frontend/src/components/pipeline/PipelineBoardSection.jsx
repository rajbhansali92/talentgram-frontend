import React from "react";

export function BoardSection({
    eyebrow,
    helper,
    children,
    muted = false,
    divider = false,
}) {
    return (
        <section
            className={`mt-8 ${divider ? "pt-6 border-t border-white/[0.03]" : ""}`}
        >
            <div className="flex items-baseline justify-between mb-3 px-1">
                <h3
                    className={`text-[8px] tracking-wide uppercase ${
                        muted ? "text-white/25" : "text-white/40"
                    }`}
                >
                    {eyebrow}
                </h3>
                {helper && (
                    <span className="text-[8px] font-mono text-white/15 hidden sm:inline">
                        {helper}
                    </span>
                )}
            </div>
            {children}
        </section>
    );
}

export function BoardRow({ children, testid }) {
    return (
        <div
            data-testid={testid}
            className="
                flex gap-3 pb-2
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
