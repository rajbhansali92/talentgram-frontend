// Shared scroll-to-and-highlight helper for jump-to-requirement UX (readiness
// panel clicks, finalize-time validation scroll). Single source so the two
// call sites (guided-validation-on-submit, readiness-panel-click) never drift.

const HIGHLIGHT_CLASS = "tg-highlight-flash";
const HIGHLIGHT_DURATION_MS = 1600;

/**
 * Resolves a requirement item's target element: a live `fieldRefs`-style ref
 * takes precedence (most precise — the actual input), falling back to a CSS
 * selector on a section/field wrapper.
 */
export function resolveRequirementElement(item, fieldRefs) {
    const refEl = fieldRefs?.current?.[item.id];
    if (refEl) return refEl;
    if (item.selector) {
        return document.querySelector(item.selector);
    }
    return null;
}

/** Scrolls to `el`, briefly flashes a highlight ring, and focuses the first focusable descendant. */
export function scrollToAndHighlight(el, { block = "center" } = {}) {
    if (!el) return false;
    el.scrollIntoView({ behavior: "smooth", block });
    el.classList.add(HIGHLIGHT_CLASS);
    window.setTimeout(() => el.classList.remove(HIGHLIGHT_CLASS), HIGHLIGHT_DURATION_MS);
    try {
        const focusable = el.matches("input, select, textarea, button")
            ? el
            : el.querySelector("input, select, textarea, button");
        if (focusable) focusable.focus({ preventScroll: true });
    } catch (_) {
        // Focus is a nicety here — never let it block the scroll/highlight.
    }
    return true;
}

/** Combines resolve + scroll for a single requirement item. Returns whether a target was found. */
export function jumpToRequirementItem(item, fieldRefs) {
    const el = resolveRequirementElement(item, fieldRefs);
    return scrollToAndHighlight(el);
}
