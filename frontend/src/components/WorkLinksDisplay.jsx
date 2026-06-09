/**
 * WorkLinksDisplay — shared component for rendering talent work/portfolio links.
 *
 * Used in:
 *   - TalentEdit.jsx        (admin talent profile)
 *   - SubmissionPage.jsx    (admin submission view)
 *   - ClientView.jsx        (client review panel — mobile & desktop)
 *   - LinkResults.jsx       (public share link view)
 *
 * Stored format for each entry: "Label || https://..." or bare "https://..."
 *
 * Renders each link as:
 *   [icon] Label or platform name
 *          domain.com                    ← subtitle
 *   [Open ↗]                             ← clickable button
 */
import React from "react";

// ---------------------------------------------------------------------------
// Parser — same logic used by TalentEdit / SubmissionPage / ApplicationPage
// ---------------------------------------------------------------------------
export function parseStoredWorkLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        const url = stored.slice(idx + 4).trim().replace(/[.,;:!?)\]>]+$/, "");
        return { label: stored.slice(0, idx).trim(), url };
    }
    // Legacy: bare URL stored without label
    const url = (stored || "").replace(/[.,;:!?)\]>]+$/, "");
    return { label: "", url };
}


// ---------------------------------------------------------------------------
// Platform metadata resolver
// ---------------------------------------------------------------------------
export function getLinkMeta(url) {
    try {
        const u = new URL(url);
        const h = u.hostname;

        if (h.includes("youtube.com") || h.includes("youtu.be")) {
            return { platform: "YouTube",   icon: "🎥", color: "text-red-600  bg-red-50   border-red-100",   domain: h };
        }
        if (h.includes("instagram.com")) {
            return { platform: "Instagram", icon: "📸", color: "text-pink-600 bg-pink-50  border-pink-100",  domain: h };
        }
        if (h.includes("vimeo.com")) {
            return { platform: "Vimeo",     icon: "🎬", color: "text-blue-500 bg-blue-50  border-blue-100",  domain: h };
        }
        if (h.includes("tiktok.com")) {
            return { platform: "TikTok",    icon: "🎵", color: "text-black    bg-neutral-100 border-neutral-200", domain: h };
        }
        if (h.includes("facebook.com")) {
            return { platform: "Facebook",  icon: "👥", color: "text-blue-700 bg-blue-50  border-blue-100",  domain: h };
        }
        if (h.includes("twitter.com") || h.includes("x.com")) {
            return { platform: "Twitter/X", icon: "🐦", color: "text-sky-500  bg-sky-50   border-sky-100",   domain: h };
        }
        return { platform: "Website", icon: "🌐", color: "text-neutral-500 bg-neutral-50 border-neutral-100", domain: h };
    } catch {
        return { platform: "Link", icon: "🔗", color: "text-neutral-500 bg-neutral-50 border-neutral-100", domain: url };
    }
}

// ---------------------------------------------------------------------------
// WorkLinksDisplay — the single shared renderer
// ---------------------------------------------------------------------------
/**
 * @param {string[]}  links          Array of stored work link strings ("Label || URL" or bare URL)
 * @param {string}    [className]    Extra wrapper class
 * @param {Function}  [renderExtra]  Optional: (url, index) => ReactNode injected after the Open button (e.g. delete button)
 */
export default function WorkLinksDisplay({ links, className = "", renderExtra }) {
    if (!links || links.length === 0) return null;

    return (
        <div className={`grid grid-cols-1 sm:grid-cols-2 gap-3 ${className}`} data-testid="work-links-display">
            {links.map((stored, i) => {
                const { label, url } = parseStoredWorkLink(stored);
                const meta = getLinkMeta(url);

                // Guard: skip entries where URL is empty / unparseable
                if (!url) return null;

                return (
                    <div
                        key={i}
                        className="flex items-center gap-3 p-3 rounded-xl border border-black/[0.06] bg-neutral-50/50 hover:bg-neutral-50 transition-colors"
                        data-testid={`work-link-${i}`}
                    >
                        {/* Clickable area — platform icon + label + domain */}
                        <a
                            href={url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-3 min-w-0 flex-1 no-underline"
                        >
                            {/* Platform icon badge */}
                            <div className={`w-9 h-9 rounded-lg flex items-center justify-center border text-base shrink-0 ${meta.color}`}>
                                {meta.icon}
                            </div>

                            {/* Label + domain */}
                            <div className="min-w-0 flex-1">
                                <p className="text-xs font-semibold text-neutral-800 truncate leading-snug">
                                    {label || meta.platform}
                                </p>
                                <p className="text-[10px] text-neutral-400 truncate leading-snug mt-0.5" title={url}>
                                    {meta.domain}
                                </p>
                            </div>
                        </a>

                        {/* Open button + optional extra (e.g. delete) */}
                        <div className="flex items-center gap-1 shrink-0">
                            <a
                                href={url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-[10px] font-semibold bg-white border border-black/[0.08] hover:border-black/30 text-black px-2.5 py-1.5 rounded-lg transition-colors select-none"
                            >
                                Open
                            </a>
                            {renderExtra && renderExtra(url, i)}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}
