import { useCallback } from "react";

const applyTheme = () => {
    if (typeof window === "undefined") return;
    const html = document.documentElement;
    html.classList.add("light");
    html.classList.remove("dark");
};

export function useTheme() {
    // Permanently locked to light mode as per ATS operational design guidelines
    applyTheme();

    const toggle = useCallback(() => {
        // No-op to prevent theme switching
    }, []);

    return { theme: "light", toggle, isLight: true };
}
