/**
 * Talentgram — Unified Talent Schema (single source of truth).
 *
 * Every talent-facing form in the app — Admin TalentEdit, public /apply
 * onboarding, and project /submit auditions — imports its dropdown values
 * from this file. Do NOT inline option lists in the form components: that
 * is exactly the drift this module exists to prevent.
 *
 * The Pydantic schema (`TalentIn` in /app/backend/core.py) is the canonical
 * persistence contract. The constants below mirror it 1:1.
 */

// ────────────────────────────────────────────────────────────────────────
// Heights (3'0" → 6'7")
// ────────────────────────────────────────────────────────────────────────
export const HEIGHT_OPTIONS = (() => {
    const out = [];
    for (let ft = 3; ft <= 6; ft++) {
        const maxIn = ft === 6 ? 7 : 11;
        for (let inch = 0; inch <= maxIn; inch++) {
            out.push(`${ft}'${inch}"`);
        }
    }
    return out;
})();

// ────────────────────────────────────────────────────────────────────────
// Gender — 4 canonical values. The `key` is what we persist; `label` is UX.
// ────────────────────────────────────────────────────────────────────────
export const GENDER_OPTIONS = [
    { key: "female", label: "Female" },
    { key: "male", label: "Male" },
    { key: "non_binary", label: "Non-binary" },
    { key: "prefer_not_say", label: "Prefer not to say" },
];

// ────────────────────────────────────────────────────────────────────────
// Ethnicity — high-level buckets. Stored as the lowercase key.
// ────────────────────────────────────────────────────────────────────────
export const ETHNICITY_OPTIONS = [
    { key: "indian", label: "Indian" },
    { key: "south_asian", label: "South Asian" },
    { key: "east_asian", label: "East Asian" },
    { key: "caucasian", label: "Caucasian" },
    { key: "african", label: "African" },
    { key: "middle_eastern", label: "Middle Eastern" },
    { key: "latino", label: "Latino" },
    { key: "mixed", label: "Mixed" },
    { key: "other", label: "Other" },
];

// ────────────────────────────────────────────────────────────────────────
// Instagram followers — 30 tiered buckets. Mirrors the legacy admin lists
// (Phase 0 backfill keeps existing free-text values; new submissions pick
// from this list).
// ────────────────────────────────────────────────────────────────────────
export const FOLLOWER_TIERS = [
    {
        label: "Early Range",
        items: ["1K+", "10K+", "25K+", "50K+", "75K+", "100K+"],
    },
    {
        label: "Mid Range",
        items: ["150K+", "200K+", "300K+", "400K+", "500K+", "750K+", "1M+"],
    },
    {
        label: "High Range",
        items: ["2M+", "3M+", "4M+", "5M+", "7M+", "10M+"],
    },
    {
        label: "Premium Influencer",
        items: ["15M+", "20M+", "25M+", "30M+", "40M+", "50M+"],
    },
];

// Flattened list — handy for snapshot tests and validation.
export const FOLLOWER_OPTIONS = FOLLOWER_TIERS.flatMap((t) => t.items);

// ────────────────────────────────────────────────────────────────────────
// Media categories — accepted by both /api/public/apply/.../upload and
// /api/public/projects/.../submission/.../upload. The backend allows the
// same set of categories so nothing has to be remapped at merge time.
//
//   intro_video — single slot, mp4/mov, ≤150MB
//   take        — renamable audition take, up to MAX_SUBMISSION_TAKES
//   indian      — "Indian look" portfolio image
//   western     — "Western look" portfolio image
//   image       — generic portfolio image (still supported for back-compat)
// ────────────────────────────────────────────────────────────────────────
export const MEDIA_CATEGORIES = {
    INTRO_VIDEO: "intro_video",
    TAKE: "take",
    INDIAN: "indian",
    WESTERN: "western",
    PORTFOLIO: "image", // generic
};

export const PORTFOLIO_LOOK_CATEGORIES = ["indian", "western", "image"];

// ────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────
export function calcAge(dob) {
    if (!dob) return null;
    const [y, m, d] = String(dob).split("-").map((n) => parseInt(n, 10));
    if (!y || !m || !d) return null;
    const today = new Date();
    let age = today.getFullYear() - y;
    const mm = today.getMonth() + 1;
    const dd = today.getDate();
    if (mm < m || (mm === m && dd < d)) age -= 1;
    return age >= 0 && age <= 120 ? age : null;
}

export function genderLabel(key) {
    return GENDER_OPTIONS.find((g) => g.key === key)?.label || "";
}

export function ethnicityLabel(key) {
    return ETHNICITY_OPTIONS.find((e) => e.key === key)?.label || key || "";
}
