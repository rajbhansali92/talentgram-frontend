import React from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/lib/theme";

export default function ThemeToggle({ className = "", size = "md" }) {
    const { isLight, toggle } = useTheme();
    const dim = size === "sm" ? "w-8 h-8" : "w-9 h-9";
    return (
        <button
            onClick={toggle}
            data-testid="theme-toggle-btn"
            aria-label={isLight ? "Switch to night mode" : "Switch to day mode"}
            title={isLight ? "Night mode" : "Day mode"}
            className={`${dim} inline-flex items-center justify-center rounded-sm border border-white/15 hover:border-white transition-all shrink-0 ${className}`}
        >
            {isLight ? (
                <Moon className="w-4 h-4" strokeWidth={1.5} />
            ) : (
                <Sun className="w-4 h-4" strokeWidth={1.5} />
            )}
        </button>
    );
}
