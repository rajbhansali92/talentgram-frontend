"use client";

import { useEffect } from "react";

// Publishes an element's live rendered height (padding/border included) onto
// a CSS custom property on <html>, so other fixed/floating UI can react to
// it without hardcoding a pixel value. Keeps working if the footer's height
// changes for any reason — safe-area inset, extra warning text, iOS toolbar
// show/hide triggering a reflow, future redesign, etc.
export function useStickyFooterHeightVar(ref, cssVarName) {
    useEffect(() => {
        const el = ref.current;
        if (!el || typeof ResizeObserver === "undefined") return;

        const publish = () => {
            document.documentElement.style.setProperty(cssVarName, `${el.offsetHeight}px`);
        };

        publish();
        const observer = new ResizeObserver(publish);
        observer.observe(el);

        return () => {
            observer.disconnect();
            document.documentElement.style.removeProperty(cssVarName);
        };
    }, [ref, cssVarName]);
}
