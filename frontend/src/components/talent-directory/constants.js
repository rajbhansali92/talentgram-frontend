// Shared constants for the talent-directory filter/sort engine (Global
// Talent page + Browse Roster). Keep this list in exact sync with the
// backend's FOLLOWER_BUCKET_ORDER (core.py) — position order is what
// determines "at least this many followers" filtering and the
// low->high/high->low sort.
export const FOLLOWER_BUCKETS = [
    "1K+", "10K+", "25K+", "50K+", "75K+", "100K+", "150K+", "200K+",
    "300K+", "400K+", "500K+", "750K+", "1M+", "2M+", "3M+", "4M+", "5M+",
    "7M+", "10M+", "15M+", "20M+", "25M+", "30M+", "40M+", "50M+",
];

// {value: inches, label: display} — same 4'0"-7'0" range TalentBrowserModal
// used, kept here so both surfaces render an identical height picker.
export const HEIGHT_INCH_OPTIONS = (() => {
    const opts = [];
    for (let feet = 4; feet <= 7; feet++) {
        const maxInches = feet === 7 ? 0 : 11;
        for (let inches = 0; inches <= maxInches; inches++) {
            opts.push({ value: feet * 12 + inches, label: `${feet}'${inches}"` });
        }
    }
    return opts;
})();

export const SORT_OPTIONS = [
    { value: "created_desc", label: "Recently Added" },
    { value: "updated_desc", label: "Recently Updated" },
    { value: "name_asc", label: "Name A-Z" },
    { value: "name_desc", label: "Name Z-A" },
    { value: "age_asc", label: "Youngest → Oldest" },
    { value: "age_desc", label: "Oldest → Youngest" },
    { value: "height_asc", label: "Shortest → Tallest" },
    { value: "height_desc", label: "Tallest → Shortest" },
    { value: "followers_asc", label: "Followers Low → High" },
    { value: "followers_desc", label: "Followers High → Low" },
    { value: "completeness_desc", label: "Profile Completeness" },
];
