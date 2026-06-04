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
            className={`
                relative mt-10 first:mt-0
                ${divider ? "pt-6 border-t border-white/[0.05]" : ""}
            `}
        >
            {/* Section header with atmospheric editorial accent */}
            <div className="flex items-baseline justify-between mb-4 px-1">
                <div className="flex items-center gap-2">
                    {/* Editorial accent line */}
                    <div 
                        className="hidden sm:block w-6 h-px bg-white/[0.08]"
                        aria-hidden="true"
                    />
                    <h3
                        className={`
                            text-[10px] tracking-[0.18em] uppercase font-medium
                            ${muted ? "text-white/30" : "text-white/45"}
                        `}
                    >
                        {eyebrow}
                    </h3>
                </div>
                {helper && (
                    <span className="text-[9px] font-mono text-white/28 hidden sm:inline">
                        {helper}
                    </span>
                )}
            </div>
            {children}
        </section>
    );
}

export function BoardRow({ children, testid }) {
    const isSingleChild = React.Children.count(children) === 1;
    return (
        <div
            data-testid={testid}
            className="
                relative
                flex flex-col md:flex-row gap-4 pb-3
                overflow-y-visible md:overflow-x-auto tg-pipeline-scroll
                items-stretch md:items-start
                snap-none md:snap-x md:snap-proximity md:snap-none
                md:-mx-2 md:px-2
            "
            style={{
                scrollBehavior: "smooth",
                WebkitOverflowScrolling: "touch",
                maskImage: "linear-gradient(to right, transparent, black 2%, black 98%, transparent)",
                WebkitMaskImage: "linear-gradient(to right, transparent, black 2%, black 98%, transparent)",
            }}
        >
            {React.Children.map(children, (child, idx) => (
                <div key={idx} className={`w-full md:w-auto snap-start md:snap-none ${isSingleChild ? "w-full flex-1 min-w-0" : "shrink-0 md:shrink-0"}`}>
                    {child}
                </div>
            ))}
            
            {/* Subtle inner top light - optional cinematic touch */}
            <div 
                className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-white/[0.06] to-transparent pointer-events-none"
                aria-hidden="true"
            />
        </div>
    );
}
