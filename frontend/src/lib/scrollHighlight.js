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

/**
 * Scrolls to `el`, briefly flashes a highlight ring, and focuses the first
 * focusable descendant. Pass `highlight: false` for a plain scroll — used
 * when jumping to a section that has nothing unresolved to draw attention
 * to (e.g. a SectionStatusBadge click on an already-complete section).
 */
export function scrollToAndHighlight(el, { block = "center", highlight = true } = {}) {
    if (!el) return false;
    el.scrollIntoView({ behavior: "smooth", block });
    if (!highlight) return true;
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

const REVEAL_RETRY_DELAY_MS = 90;

/**
 * Same job as `jumpToRequirementItem`, but resilient to the target not being
 * visible yet — for whatever reason, at whatever depth. It has NO knowledge
 * of the page's layout, section hierarchy, or how visibility is achieved: it
 * just tries to resolve the target, and if that fails, asks the
 * caller-supplied `ensureVisible()` to make more of the page visible, waits
 * a paint for the re-render, and tries again. It keeps doing this until the
 * target resolves, `ensureVisible()` reports it could make no further
 * progress, or `maxAttempts` is reached.
 *
 * `ensureVisible()` is a black box from this function's point of view — a
 * single "reveal one more step, if you can" call that returns whether it
 * changed anything. Today's implementation happens to expand collapsed
 * accordions; a future one could just as easily switch tabs, open a drawer,
 * or advance a wizard step. This function, and every caller of it, would be
 * unaffected either way — that decoupling is the whole point.
 *
 * `ensureVisible` is optional — omit it to behave exactly like
 * `jumpToRequirementItem` (a single resolve-and-scroll attempt).
 */
export function revealAndJumpToRequirementItem(item, fieldRefs, ensureVisible, { maxAttempts = 6, block = "center", highlight = true } = {}) {
    const attempt = (attemptsLeft) => {
        const el = resolveRequirementElement(item, fieldRefs);
        if (el) {
            scrollToAndHighlight(el, { block, highlight });
            return true;
        }
        if (attemptsLeft <= 0 || !ensureVisible) return false;
        const madeProgress = ensureVisible();
        if (!madeProgress) return false; // nothing left to reveal — target genuinely isn't there
        window.setTimeout(() => attempt(attemptsLeft - 1), REVEAL_RETRY_DELAY_MS);
        return true; // a retry is scheduled
    };
    return attempt(maxAttempts);
}
