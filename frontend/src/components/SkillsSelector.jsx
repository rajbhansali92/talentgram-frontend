import React, { useMemo, useState } from "react";
import { Check, Search, ChevronDown, X } from "lucide-react";

export const SKILLS_CATEGORIES = {
    "Dance": [
        "Hip Hop", "Contemporary", "Bollywood", "Bharatanatyam", "Kathak",
        "Salsa", "Ballet"
    ],
    "Music": [
        "Singer", "Piano", "Keyboard", "Guitar", "Violin", "Drums",
        "Flute", "Ukulele", "DJ", "Beatboxing", "Rapper", "Composer",
        "Music Producer"
    ],
    "Sports & Fitness": [
        "Athlete", "Gymnastics", "Yoga", "Swimming", "Cycling", "Boxing",
        "Kickboxing", "Wrestling", "CrossFit", "Calisthenics", "Cricket",
        "Football", "Basketball", "Tennis", "Badminton"
    ],
    "Action & Stunts": [
        "Martial Arts", "Karate", "Taekwondo", "Judo", "Kung Fu",
        "Fight Choreography", "Horse Riding", "Rock Climbing", "Parkour",
        "Sword Fighting"
    ],
    "Vehicle Skills": [
        "Drive Manual Car", "Drive Automatic Car", "Ride Motorcycle",
        "Ride Scooter", "Ride Bicycle", "Drive Truck", "Operate Boat",
        "Ride Jet Ski"
    ],
    "Performance": [
        "Actor", "Voice Artist", "Dancer", "Singer", "Host", "Anchor",
        "Model", "Theatre Artist", "Improvisation", "Stand-up Comedy"
    ],
    "Special Skills": [
        "Skateboarding", "Roller Skating", "Ice Skating", "Surfing",
        "Scuba Diving", "Fire Performance", "Juggling"
    ],
    "Languages": [
        "English", "Hindi", "Spanish", "French", "Mandarin Chinese",
        "Japanese", "Russian", "German", "Arabic", "Marathi", "Gujarati",
        "Punjabi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali",
        "Urdu", "Other"
    ]
};

// Sprint 2 — frequently-used skills surfaced first so the common case needs no
// scrolling. Every entry must exist in SKILLS_CATEGORIES above (same data model).
export const POPULAR_SKILLS = [
    "Actor", "Model", "Dancer", "Singer", "Hip Hop",
    "Yoga", "Martial Arts", "Drive Manual Car", "English", "Hindi",
];

// Chip — unchanged visual styling so the design language is preserved.
function SkillChip({ skill, isSelected, readOnly, onToggle }) {
    return (
        <button
            type="button"
            disabled={readOnly}
            onClick={() => onToggle(skill)}
            data-testid={`skill-chip-${skill.toLowerCase().replace(/\s+/g, "-")}`}
            className={`inline-flex items-center gap-1.5 px-4 py-2.5 min-h-[44px] rounded-full text-xs font-medium border transition-all duration-150 active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#0c2340]/40 focus-visible:ring-offset-1 ${
                isSelected
                    ? "bg-[#0c2340] border-[#0c2340] text-white shadow-sm"
                    : readOnly
                        ? "bg-slate-50/50 border-slate-200 text-slate-400 cursor-default"
                        : "bg-white border-[#0c2340]/40 text-[#0c2340] hover:bg-[#0c2340]/5"
            }`}
        >
            {isSelected && <Check className="w-3.5 h-3.5 shrink-0" />}
            <span>{skill}</span>
        </button>
    );
}

export default function SkillsSelector({ selectedSkills = [], onChange, readOnly = false }) {
    const currentSelected = selectedSkills || [];
    // P0-3: search + collapsible groups to cut scrolling. Hooks run
    // unconditionally (before any early return) per the rules of hooks.
    const [query, setQuery] = useState("");
    const [expanded, setExpanded] = useState({}); // category -> bool (default collapsed)

    const handleToggleSkill = (skill) => {
        if (readOnly) return;
        const index = currentSelected.indexOf(skill);
        const updated = [...currentSelected];
        if (index > -1) updated.splice(index, 1);
        else updated.push(skill);
        if (onChange) onChange(updated);
    };

    const q = query.trim().toLowerCase();
    const searchMatches = useMemo(() => {
        if (!q) return [];
        const out = [];
        for (const skills of Object.values(SKILLS_CATEGORIES)) {
            for (const s of skills) {
                if (s.toLowerCase().includes(q) && !out.includes(s)) out.push(s);
            }
        }
        return out;
    }, [q]);

    // Read-only / review context keeps the original always-expanded layout.
    if (readOnly) {
        return (
            <div className="space-y-6 text-left" data-testid="skills-selector">
                {Object.entries(SKILLS_CATEGORIES).map(([category, skills]) => (
                    <div key={category} className="space-y-2.5">
                        <h4 className="text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-500">
                            {category}
                        </h4>
                        <div className="flex flex-wrap gap-2">
                            {skills.map((skill) => (
                                <SkillChip
                                    key={skill}
                                    skill={skill}
                                    isSelected={currentSelected.includes(skill)}
                                    readOnly
                                    onToggle={() => {}}
                                />
                            ))}
                        </div>
                    </div>
                ))}
            </div>
        );
    }

    return (
        <div className="space-y-4 text-left" data-testid="skills-selector">
            {/* Search */}
            <div className="relative">
                <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
                <input
                    type="text"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search skills..."
                    data-testid="skills-search"
                    className="w-full bg-white border border-[#eaeaea] rounded-lg pl-9 pr-3 h-11 text-[16px] md:text-[15px] text-[#1a1a1a] placeholder:text-[#b0aea6] focus:ring-1 focus:ring-[#b0aea6] focus:border-[#d4d4d4] outline-none transition-all duration-150"
                />
            </div>

            {/* Selected tray — keeps choices visible without scrolling */}
            {currentSelected.length > 0 && (
                <div className="space-y-2" data-testid="skills-selected-tray">
                    <p className="text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-500">
                        Selected ({currentSelected.length})
                    </p>
                    <div className="flex flex-wrap gap-2">
                        {currentSelected.map((skill) => (
                            <button
                                key={skill}
                                type="button"
                                onClick={() => handleToggleSkill(skill)}
                                data-testid={`skill-selected-${skill.toLowerCase().replace(/\s+/g, "-")}`}
                                aria-label={`Remove ${skill}`}
                                className="inline-flex items-center gap-1.5 px-4 py-2.5 min-h-[44px] rounded-full text-xs font-medium border bg-[#0c2340] border-[#0c2340] text-white shadow-sm active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#0c2340]/40 focus-visible:ring-offset-1"
                            >
                                <span>{skill}</span>
                                <X className="w-3.5 h-3.5 shrink-0" />
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* Search results (flat) OR collapsible categories */}
            {q ? (
                searchMatches.length === 0 ? (
                    <p className="text-xs text-slate-400">No skills match “{query}”.</p>
                ) : (
                    <div className="flex flex-wrap gap-2">
                        {searchMatches.map((skill) => (
                            <SkillChip
                                key={skill}
                                skill={skill}
                                isSelected={currentSelected.includes(skill)}
                                readOnly={false}
                                onToggle={handleToggleSkill}
                            />
                        ))}
                    </div>
                )
            ) : (
              <>
                {/* Popular skills — always visible, no scroll needed for the common case */}
                <div className="space-y-2" data-testid="skills-popular">
                    <p className="text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-500">
                        Popular
                    </p>
                    <div className="flex flex-wrap gap-2">
                        {POPULAR_SKILLS.map((skill) => (
                            <SkillChip
                                key={skill}
                                skill={skill}
                                isSelected={currentSelected.includes(skill)}
                                readOnly={false}
                                onToggle={handleToggleSkill}
                            />
                        ))}
                    </div>
                </div>
                {Object.entries(SKILLS_CATEGORIES).map(([category, skills]) => {
                    const isOpen = !!expanded[category];
                    const selectedCount = skills.filter((s) => currentSelected.includes(s)).length;
                    return (
                        <div key={category} className="border-b border-[#f0efe9] pb-2">
                            <button
                                type="button"
                                onClick={() => setExpanded((p) => ({ ...p, [category]: !p[category] }))}
                                aria-expanded={isOpen}
                                data-testid={`skills-group-${category.toLowerCase().replace(/\s+/g, "-")}`}
                                className="w-full flex items-center justify-between py-2 text-left"
                            >
                                <span className="text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-500">
                                    {category}
                                    <span className="ml-2 text-slate-400 normal-case font-normal">
                                        ({skills.length}{selectedCount > 0 ? ` · ${selectedCount} selected` : ""})
                                    </span>
                                </span>
                                <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform duration-150 ${isOpen ? "rotate-180" : ""}`} />
                            </button>
                            {isOpen && (
                                <div className="flex flex-wrap gap-2 pt-1 pb-2">
                                    {skills.map((skill) => (
                                        <SkillChip
                                            key={skill}
                                            skill={skill}
                                            isSelected={currentSelected.includes(skill)}
                                            readOnly={false}
                                            onToggle={handleToggleSkill}
                                        />
                                    ))}
                                </div>
                            )}
                        </div>
                    );
                })}
              </>
            )}
        </div>
    );
}
