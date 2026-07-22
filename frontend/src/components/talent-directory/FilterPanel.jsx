import React from "react";
import { GENDER_OPTIONS, ETHNICITY_OPTIONS } from "@/lib/talentSchema";
import { SKILLS_CATEGORIES } from "@/components/SkillsSelector";
import { FOLLOWER_BUCKETS, HEIGHT_INCH_OPTIONS } from "./constants";
import LocationMultiSelect from "./LocationMultiSelect";

const INTERESTED_IN_OPTIONS = ["Acting", "Modeling", "Influencer Campaigns"];

function MatchModeToggle({ mode, onChange }) {
    return (
        <div className="flex items-center gap-1 bg-gray-100 rounded-md p-0.5 shrink-0">
            {["any", "all"].map((m) => (
                <button
                    key={m}
                    type="button"
                    onClick={() => onChange(m)}
                    className={`px-2 py-0.5 rounded text-[10px] font-medium transition-all ${
                        mode === m ? "bg-white text-[#111111] shadow-sm" : "text-[#333333] hover:text-[#111111]"
                    }`}
                >
                    {m === "any" ? "ANY (OR)" : "ALL (AND)"}
                </button>
            ))}
        </div>
    );
}

function MultiChipGroup({ options, selected, onToggle, labelFor = (v) => v }) {
    return (
        <div className="flex flex-wrap gap-2">
            {options.map((opt) => {
                const active = selected.includes(opt);
                return (
                    <button
                        key={opt}
                        type="button"
                        onClick={() => onToggle(opt)}
                        className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${
                            active
                                ? "bg-[#0c2340] text-white border-[#0c2340]"
                                : "bg-white text-[#333333] border-gray-200 hover:border-gray-300"
                        }`}
                    >
                        {labelFor(opt)}
                    </button>
                );
            })}
        </div>
    );
}

const selectCls = "w-full px-3 py-2 text-sm bg-white border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300";
const labelCls = "text-xs font-semibold text-[#222222] uppercase tracking-wider";

/**
 * FilterPanel — the shared filter criteria form. Renders identically inside
 * the desktop panel (TalentList.jsx, TalentBrowserModal.jsx) and the mobile
 * bottom sheet (MobileFilterSheet.jsx) — same props, same behavior, so the
 * two surfaces (and mobile vs desktop within each) can never drift.
 */
export default function FilterPanel({ filters, setFilter, availableTags = [], availableLocations = [] }) {
    const toggleArrayValue = (key, value) => {
        const current = filters[key];
        setFilter(key, current.includes(value) ? current.filter((v) => v !== value) : [...current, value]);
    };

    return (
        <div className="space-y-5">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                    <label className={labelCls}>Gender</label>
                    <select value={filters.gender} onChange={(e) => setFilter("gender", e.target.value)} className={`${selectCls} mt-1.5`}>
                        <option value="any">Any</option>
                        {GENDER_OPTIONS.map((g) => <option key={g.key} value={g.key}>{g.label}</option>)}
                    </select>
                </div>
                <div>
                    <label className={labelCls}>Ethnicity</label>
                    <select value={filters.ethnicity} onChange={(e) => setFilter("ethnicity", e.target.value)} className={`${selectCls} mt-1.5`}>
                        <option value="any">Any</option>
                        {ETHNICITY_OPTIONS.map((e) => <option key={e.key} value={e.key}>{e.label}</option>)}
                    </select>
                </div>
                <div>
                    <label className={labelCls}>Location</label>
                    <div className="mt-1.5">
                        <LocationMultiSelect
                            value={filters.locations}
                            onChange={(v) => setFilter("locations", v)}
                            options={availableLocations}
                        />
                    </div>
                </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div>
                    <label className={labelCls}>Age Range</label>
                    <div className="flex gap-2 mt-1.5">
                        <input type="number" min="0" max="120" value={filters.ageMin} onChange={(e) => setFilter("ageMin", e.target.value)} placeholder="Min" className={selectCls} />
                        <span className="text-[#333333] self-center font-medium">–</span>
                        <input type="number" min="0" max="120" value={filters.ageMax} onChange={(e) => setFilter("ageMax", e.target.value)} placeholder="Max" className={selectCls} />
                    </div>
                </div>
                <div>
                    <label className={labelCls}>Height Range</label>
                    <div className="flex gap-2 mt-1.5">
                        <select value={filters.heightMin} onChange={(e) => setFilter("heightMin", e.target.value)} className={selectCls}>
                            <option value="">Min</option>
                            {HEIGHT_INCH_OPTIONS.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                        </select>
                        <span className="text-[#333333] self-center font-medium">–</span>
                        <select value={filters.heightMax} onChange={(e) => setFilter("heightMax", e.target.value)} className={selectCls}>
                            <option value="">Max</option>
                            {HEIGHT_INCH_OPTIONS.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                        </select>
                    </div>
                </div>
                <div>
                    <label className={labelCls}>Instagram Followers</label>
                    <select value={filters.followersMin} onChange={(e) => setFilter("followersMin", e.target.value)} className={`${selectCls} mt-1.5`}>
                        <option value="">Any</option>
                        {FOLLOWER_BUCKETS.map((b) => <option key={b} value={b}>{b}+</option>)}
                    </select>
                </div>
            </div>

            <div className="pt-2 border-t border-gray-100">
                <div className="flex items-center justify-between mb-2">
                    <label className={labelCls}>Interested In</label>
                    <MatchModeToggle mode={filters.interestedInMode} onChange={(m) => setFilter("interestedInMode", m)} />
                </div>
                <MultiChipGroup options={INTERESTED_IN_OPTIONS} selected={filters.interestedIn} onToggle={(v) => toggleArrayValue("interestedIn", v)} />
            </div>

            <div className="pt-2 border-t border-gray-100">
                <div className="flex items-center justify-between mb-2">
                    <label className={labelCls}>Skills & Special Abilities</label>
                    <MatchModeToggle mode={filters.skillsMode} onChange={(m) => setFilter("skillsMode", m)} />
                </div>
                <div className="space-y-3 max-h-56 overflow-y-auto pr-1">
                    {Object.entries(SKILLS_CATEGORIES).map(([category, options]) => (
                        <div key={category}>
                            <p className="text-[10px] font-mono uppercase tracking-wider text-[#8b8b8b] mb-1.5">{category}</p>
                            <MultiChipGroup options={options} selected={filters.skills} onToggle={(v) => toggleArrayValue("skills", v)} />
                        </div>
                    ))}
                </div>
            </div>

            {availableTags.length > 0 && (
                <div className="pt-2 border-t border-gray-100">
                    <div className="flex items-center justify-between mb-2">
                        <label className={labelCls}>Internal Tags</label>
                        <MatchModeToggle mode={filters.tagsMode} onChange={(m) => setFilter("tagsMode", m)} />
                    </div>
                    <MultiChipGroup
                        options={availableTags.map((t) => t.id)}
                        selected={filters.tags}
                        onToggle={(v) => toggleArrayValue("tags", v)}
                        labelFor={(id) => availableTags.find((t) => t.id === id)?.name || id}
                    />
                </div>
            )}
        </div>
    );
}
