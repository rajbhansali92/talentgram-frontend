"use client";

import { useRef } from "react";

// Small, dependency-free swipe-to-navigate handler for the submission
// wizard's mobile step container. Deliberately NOT built on embla-carousel
// (already a dependency elsewhere in this repo, but its drag/momentum
// physics would visually move the view before validation gets a chance to
// block an invalid forward swipe) — a plain threshold check keeps forward
// navigation going through the exact same `onSwipeForward` handler as the
// Next button, so it's gated identically either way. Swiping back never
// needs gating (same as tapping Back), so `onSwipeBack` always fires.
//
// Returns touch handlers to spread onto the step container. Only commits to
// a horizontal swipe once the horizontal delta clearly exceeds the vertical
// one, so normal vertical scrolling inside a step is never hijacked.
const SWIPE_DISTANCE_THRESHOLD = 60; // px
const SWIPE_DIRECTION_RATIO = 1.5; // horizontal must beat vertical by this much

export function useSwipeStep({ onSwipeBack, onSwipeForward, disabled = false } = {}) {
    const start = useRef(null);

    const onTouchStart = (e) => {
        if (disabled || !e.touches?.[0]) return;
        start.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    };

    const onTouchEnd = (e) => {
        if (disabled || !start.current) return;
        const touch = e.changedTouches?.[0];
        if (touch) handleEnd(touch.clientX, touch.clientY);
        start.current = null;
    };

    const handleEnd = (endX, endY) => {
        const dx = endX - start.current.x;
        const dy = endY - start.current.y;
        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);
        if (absDx < SWIPE_DISTANCE_THRESHOLD) return;
        if (absDx < absDy * SWIPE_DIRECTION_RATIO) return; // too vertical — was a scroll

        if (dx > 0) {
            onSwipeBack?.();
        } else {
            onSwipeForward?.();
        }
    };

    return {
        onTouchStart,
        onTouchEnd,
    };
}
