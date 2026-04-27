import { useCallback, useEffect, useState } from "react";

const applyTheme = (theme) => {
    const html = document.documentElement;
    html.classList.toggle("light", theme === "light");
    html.classList.toggle("dark", theme !== "light");
};

export function useTheme() {
    const [theme, setTheme] = useState(() => {
        try {
            return localStorage.getItem("tg_theme") === "light"
                ? "light"
                : "dark";
        } catch {
            return "dark";
        }
    });

    useEffect(() => {
        applyTheme(theme);
        try {
            localStorage.setItem("tg_theme", theme);
        } catch (e) { console.error(e); }
    }, [theme]);

    const toggle = useCallback(() => {
        setTheme((t) => (t === "light" ? "dark" : "light"));
    }, []);

    return { theme, toggle, isLight: theme === "light" };
}
