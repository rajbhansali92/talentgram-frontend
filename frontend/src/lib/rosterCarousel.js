// Pure helpers for the public Roster's per-talent image carousel. Each talent section owns
// its own carousel; nothing here knows about other talents, so changing one talent's image can
// never affect another's. Kept out of the component so the rules are unit-testable.

/** Clamp `i` into [0, length-1] (0 for an empty list). */
export function clampIndex(i, length) {
    if (!length || length < 1) return 0;
    return Math.max(0, Math.min(length - 1, Number.isFinite(i) ? Math.trunc(i) : 0));
}

/** Horizontal swipe -> +1 (next) / -1 (previous) / 0 (not a swipe). A swipe must be mostly
 *  horizontal so vertical page scrolling is never mistaken for an image change. */
export function swipeStep(dx, dy, { min = 45, ratio = 1.5 } = {}) {
    if (Math.abs(dx) < min || Math.abs(dx) < Math.abs(dy) * ratio) return 0;
    return dx < 0 ? 1 : -1;
}

/** ←/→ on a FOCUSED carousel -> +1/-1. Never a talent change, never with a modifier key. */
export function arrowStep(e) {
    if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return 0;
    if (e.key === "ArrowRight") return 1;
    if (e.key === "ArrowLeft") return -1;
    return 0;
}

/** The images worth warming for a talent whose carousel is on screen: its neighbours only. */
export function neighbourImages(images, index) {
    return [images[index + 1], images[index - 1]].filter(Boolean);
}

export const pad2 = (n) => String(n).padStart(2, "0");
