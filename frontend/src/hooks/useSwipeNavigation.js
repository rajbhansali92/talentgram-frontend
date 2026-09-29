import { useEffect } from "react";

// Edge-swipe back/forward for touch devices — mirrors the native
// iOS-Safari-style affordance (swipe from within a narrow strip of the
// left/right screen edge) rather than listening across the whole page.
// Deliberately edge-gated: a full-page swipe listener would fight normal
// vertical scrolling, text selection, and horizontal drags inside cards,
// so only gestures that START within EDGE_ZONE px of the viewport edge are
// tracked at all — everywhere else, touch events pass through untouched.
//
// Fires window.history.back()/forward() (the real browser APIs, not
// react-router's navigate(-1)) so any popstate-based guard already
// listening on the page (e.g. TalentEdit's unsaved-changes confirm) still
// runs exactly as it would for a real back-button press.
const EDGE_ZONE = 24; // px from the edge a gesture must start within
const MIN_DISTANCE = 60; // px of horizontal travel required to fire
const MAX_VERTICAL_RATIO = 0.5; // vertical drift allowed, relative to horizontal travel

export function useSwipeNavigation({ enabled = true } = {}) {
    useEffect(() => {
        if (!enabled) return;
        if (typeof window === "undefined") return;
        // Coarse-pointer check — skip entirely on mouse/trackpad devices so
        // there's no listener overhead where the gesture can't occur anyway.
        if (window.matchMedia && !window.matchMedia("(pointer: coarse)").matches) return;

        let startX = null;
        let startY = null;
        let edge = null; // "left" | "right" | null

        const onTouchStart = (e) => {
            const t = e.touches[0];
            if (!t) return;
            // Let a page opt a region out entirely (e.g. a fullscreen media
            // lightbox with its own prev/next swipe) rather than have both
            // gestures fire off the same touch.
            if (e.target.closest && e.target.closest("[data-swipe-nav-ignore]")) { edge = null; return; }
            if (t.clientX <= EDGE_ZONE) edge = "left";
            else if (t.clientX >= window.innerWidth - EDGE_ZONE) edge = "right";
            else edge = null;
            startX = t.clientX;
            startY = t.clientY;
        };

        const onTouchEnd = (e) => {
            if (!edge || startX === null) { startX = null; startY = null; edge = null; return; }
            const t = e.changedTouches[0];
            if (!t) return;
            const dx = t.clientX - startX;
            const dy = Math.abs(t.clientY - startY);
            if (Math.abs(dx) >= MIN_DISTANCE && dy <= Math.abs(dx) * MAX_VERTICAL_RATIO) {
                if (edge === "left" && dx > 0) window.history.back();
                else if (edge === "right" && dx < 0) window.history.forward();
            }
            startX = null;
            startY = null;
            edge = null;
        };

        document.addEventListener("touchstart", onTouchStart, { passive: true });
        document.addEventListener("touchend", onTouchEnd, { passive: true });
        return () => {
            document.removeEventListener("touchstart", onTouchStart);
            document.removeEventListener("touchend", onTouchEnd);
        };
    }, [enabled]);
}
