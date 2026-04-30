import React from "react";

export const DEFAULT_VISIBILITY = {
    portfolio: true,
    intro_video: true,
    takes: true,
    instagram: true,
    instagram_followers: true,
    age: true,
    height: true,
    location: true,
    ethnicity: true,
    availability: true,
    budget: false,
    work_links: true,
    budget_form: false,
    download: false,
};

export const VIS_ITEMS = [
    { key: "portfolio", label: "Portfolio Images" },
    { key: "intro_video", label: "Introduction Video" },
    { key: "takes", label: "Audition Takes" },
    { key: "instagram", label: "Instagram (clickable)" },
    { key: "instagram_followers", label: "Instagram Followers" },
    { key: "age", label: "Age" },
    { key: "height", label: "Height" },
    { key: "location", label: "Location" },
    { key: "ethnicity", label: "Ethnicity" },
    { key: "availability", label: "Availability" },
    { key: "budget", label: "Budget" },
    { key: "work_links", label: "Work Links" },
    { key: "budget_form", label: "Budget Form" },
    { key: "download", label: "Download Option" },
];

export default function VisibilityToggles({ value, onChange }) {
    const v = { ...DEFAULT_VISIBILITY, ...(value || {}) };
    return (
        <div className="space-y-3" data-testid="visibility-toggles">
            {VIS_ITEMS.map((it) => (
                <label
                    key={it.key}
                    className="flex items-center justify-between cursor-pointer"
                >
                    <span className="text-sm text-white/80">{it.label}</span>
                    <button
                        type="button"
                        onClick={() => onChange({ ...v, [it.key]: !v[it.key] })}
                        data-testid={`vis-toggle-${it.key}`}
                        className={`w-10 h-5 rounded-full relative transition-all ${v[it.key] ? "bg-white" : "bg-white/15"}`}
                    >
                        <span
                            className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-all ${v[it.key] ? "translate-x-5 bg-black" : "bg-white"}`}
                        />
                    </button>
                </label>
            ))}
        </div>
    );
}
