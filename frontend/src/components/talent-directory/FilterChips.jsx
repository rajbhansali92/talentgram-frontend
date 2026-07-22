import React from "react";
import { X } from "lucide-react";
import { GENDER_OPTIONS, ETHNICITY_OPTIONS } from "@/lib/talentSchema";
import { HEIGHT_INCH_OPTIONS } from "./constants";

const heightLabel = (inches) => HEIGHT_INCH_OPTIONS.find((o) => String(o.value) === String(inches))?.label || inches;

function Chip({ label, onRemove }) {
    return (
        <span className="inline-flex items-center gap-1.5 pl-3 pr-2 py-1 bg-gray-100 text-[#111111] rounded-full text-xs font-medium">
            {label}
            <button type="button" onClick={onRemove} className="hover:bg-gray-200 rounded-full p-0.5 transition-colors">
                <X className="w-3 h-3" />
            </button>
        </span>
    );
}

/**
 * FilterChips — removable summary of every active filter + Clear All.
 * Reads directly off the useTalentDirectory `filters` shape so it never
 * drifts from what FilterPanel actually renders.
 */
export default function FilterChips({ filters, setFilter, removeFilter, clearAllFilters, activeFilterCount, availableTags = [] }) {
    if (activeFilterCount === 0) return null;

    const chips = [];
    if (filters.gender !== "any") {
        chips.push({ key: "gender", label: `Gender: ${GENDER_OPTIONS.find((g) => g.key === filters.gender)?.label || filters.gender}` });
    }
    if (filters.ethnicity !== "any") {
        chips.push({ key: "ethnicity", label: `Ethnicity: ${ETHNICITY_OPTIONS.find((e) => e.key === filters.ethnicity)?.label || filters.ethnicity}` });
    }
    if (filters.location.trim()) {
        chips.push({ key: "location", label: `Location: ${filters.location}` });
    }
    if (filters.ageMin !== "" || filters.ageMax !== "") {
        chips.push({ key: "age", label: `Age: ${filters.ageMin || "0"}–${filters.ageMax || "∞"}` });
    }
    if (filters.heightMin !== "" || filters.heightMax !== "") {
        const min = filters.heightMin !== "" ? heightLabel(filters.heightMin) : "Any";
        const max = filters.heightMax !== "" ? heightLabel(filters.heightMax) : "Any";
        chips.push({ key: "height", label: `Height: ${min}–${max}` });
    }
    if (filters.followersMin) {
        chips.push({ key: "followersMin", label: `Followers: ${filters.followersMin}+` });
    }
    filters.interestedIn.forEach((v) => chips.push({ key: "interestedIn", value: v, label: `Interested: ${v}` }));
    filters.skills.forEach((v) => chips.push({ key: "skills", value: v, label: `Skill: ${v}` }));
    filters.tags.forEach((v) => chips.push({ key: "tags", value: v, label: `Tag: ${availableTags.find((t) => t.id === v)?.name || v}` }));

    const remove = (chip) => {
        if (chip.value !== undefined) {
            setFilter(chip.key, filters[chip.key].filter((v) => v !== chip.value));
        } else if (chip.key === "age") {
            setFilter("ageMin", "");
            setFilter("ageMax", "");
        } else if (chip.key === "height") {
            setFilter("heightMin", "");
            setFilter("heightMax", "");
        } else {
            removeFilter(chip.key);
        }
    };

    return (
        <div className="flex flex-wrap items-center gap-2" data-testid="filter-chips">
            {chips.map((chip, i) => (
                <Chip key={`${chip.key}-${chip.value || i}`} label={chip.label} onRemove={() => remove(chip)} />
            ))}
            <button
                type="button"
                data-testid="clear-all-filters"
                onClick={clearAllFilters}
                className="text-xs font-medium text-[#333333] hover:text-[#111111] underline underline-offset-2 ml-1"
            >
                Clear All
            </button>
        </div>
    );
}
