import React, { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { getAdmin } from "@/lib/api";
import { getAssistantAccess } from "@/lib/personalAssistant";
import { useAssistantContext } from "@/lib/assistantContext";

// The panel (and everything it pulls in) is only downloaded the first time the icon is tapped.
const AssistantPanel = lazy(() => import("./AssistantPanel"));

const cacheKey = () => `pa_access:${getAdmin()?.id || "anon"}`;
const readCache = () => { try { return sessionStorage.getItem(cacheKey()); } catch { return null; } };
const writeCache = (v) => { try { sessionStorage.setItem(cacheKey(), v ? "1" : "0"); } catch { /* storage blocked */ } };

// A bottom-docked bulk bar (BulkSelectBar, marked data-pa-dock) can be any height — a phone wraps it to several rows —
// so the launcher sits just above the bar's MEASURED top when they overlap horizontally, instead of a guessed offset.
function useDockClearance(active) {
    const [px, setPx] = useState(null);
    useEffect(() => {
        if (!active) { setPx(null); return undefined; }
        const measure = () => {
            // only a bar that reaches the launcher's column (right edge, ~88px) matters
            const bars = [...document.querySelectorAll('[data-pa-dock="bottom"]')].filter((b) => b.getBoundingClientRect().right >= window.innerWidth - 88);
            if (!bars.length) { setPx(null); return; }
            const top = Math.min(...bars.map((b) => b.getBoundingClientRect().top));
            const next = Math.max(0, Math.round(window.innerHeight - top + 12));
            setPx((cur) => (cur === next ? cur : next));
        };
        measure();
        const t = setInterval(measure, 400);        // the bar's height changes as its buttons wrap / the keyboard moves
        window.addEventListener("resize", measure);
        return () => { clearInterval(t); window.removeEventListener("resize", measure); };
    }, [active]);
    return px;
}

/**
 * Floating assistant entry point, mounted ONCE in the authenticated admin shell. Closed by default. Visible only
 * when the backend says this account may use it (every endpoint enforces that again — this is cosmetic).
 * Stays mounted while the panel is closed/minimised so the conversation and task view survive navigation.
 */
export default function PersonalAssistant() {
    const [allowed, setAllowed] = useState(() => readCache() === "1");
    const [open, setOpen] = useState(false);
    const [everOpened, setEverOpened] = useState(false);
    const [attention, setAttention] = useState(0);
    // A selection on screen means a bulk-action bar is docked at the bottom: lift the launcher / panel above it.
    const lifted = (useAssistantContext().talentIds || []).length > 0;
    const dock = useDockClearance(lifted);

    useEffect(() => {
        let live = true;
        if (readCache() === null) {
            getAssistantAccess().then((a) => { writeCache(!!a?.allowed); if (live) setAllowed(!!a?.allowed); }).catch(() => {});
        }
        return () => { live = false; };
    }, []);

    const openPanel = useCallback(() => { setOpen(true); setEverOpened(true); }, []);
    if (!allowed) return null;
    return (
        <>
            {!open && (
                <button type="button" onClick={openPanel} aria-label="Open assistant" data-testid="pa-launcher"
                    style={lifted && dock ? { bottom: `${dock}px` } : undefined}
                    className={`fixed z-30 right-4 sm:right-6 ${lifted ? "bottom-[calc(9rem+env(safe-area-inset-bottom))] sm:bottom-28" : "bottom-[calc(5.5rem+env(safe-area-inset-bottom))] sm:bottom-6"} w-12 h-12 rounded-full bg-black text-white shadow-[0_6px_20px_rgba(0,0,0,0.3)] flex items-center justify-center hover:bg-black/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black`}>
                    <Sparkles className="w-5 h-5" aria-hidden />
                    {attention > 0 && <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-amber-500 text-white text-[10px] font-semibold flex items-center justify-center" data-testid="pa-badge">{attention}</span>}
                </button>
            )}
            {everOpened && (
                <Suspense fallback={null}>
                    <AssistantPanel open={open} onClose={() => setOpen(false)} onAttention={setAttention} lifted={lifted} />
                </Suspense>
            )}
        </>
    );
}
