import React from "react";
import { Check } from "lucide-react";

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

export default function SkillsSelector({ selectedSkills = [], onChange, readOnly = false }) {
    const handleToggleSkill = (skill) => {
        if (readOnly) return;
        const currentSelected = selectedSkills || [];
        const index = currentSelected.indexOf(skill);
        const updated = [...currentSelected];
        if (index > -1) {
            updated.splice(index, 1);
        } else {
            updated.push(skill);
        }
        if (onChange) onChange(updated);
    };

    const currentSelected = selectedSkills || [];

    return (
        <div className="space-y-6 text-left" data-testid="skills-selector">
            {Object.entries(SKILLS_CATEGORIES).map(([category, skills]) => (
                <div key={category} className="space-y-2.5">
                    <h4 className="text-[11px] font-mono font-semibold uppercase tracking-wider text-slate-500">
                        {category}
                    </h4>
                    <div className="flex flex-wrap gap-2">
                        {skills.map((skill) => {
                            const isSelected = currentSelected.includes(skill);
                            return (
                                <button
                                    key={skill}
                                    type="button"
                                    disabled={readOnly}
                                    onClick={() => handleToggleSkill(skill)}
                                    data-testid={`skill-chip-${skill.toLowerCase().replace(/\s+/g, "-")}`}
                                    className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-full text-xs font-medium border transition-all duration-150 active:scale-[0.98] ${
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
                        })}
                    </div>
                </div>
            ))}
        </div>
    );
}
