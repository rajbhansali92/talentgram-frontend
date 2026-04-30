import { useCallback, useEffect } from "react";

/**
 * v38f — Light-mode-only.
 *
 * The dark/light toggle was removed app-wide. We keep the `useTheme` API
 * shape (`{ theme, toggle, isLight }`) so existing callers don't need
 * updates, but `theme` is locked to `"light"` and `toggle` is a no-op.
 *
 * Effect: every render, force `<html>` into the `light` class and clear
 * any stale `dark` class (e.g. from a prior visit before the change).
 */
const applyLight = () => {
    const html = document.documentElement;
    html.classList.add("light");
    html.classList.remove("dark");
};

export function useTheme() {
    useEffect(() => {
        applyLight();
        try {
            localStorage.setItem("tg_theme", "light");
        } catch {
            // localStorage may be unavailable in private mode — non-fatal.
        }
    }, []);

    const toggle = useCallback(() => {
        // Intentionally a no-op. Kept for API compatibility with callers
        // that import { useTheme } and destructure `toggle`.
    }, []);

    return { theme: "light", toggle, isLight: true };
}
