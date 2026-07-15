import { describe, it, expect } from "vitest";
import { computeRequirementItems } from "./requirementEngine";
import { REQUIREMENT_TIERS } from "./readinessStatus";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const baseForm = () => ({
    first_name: "",
    last_name: "",
    email: "",
    phone: "",
    dob: "",
    age: "",
    height: "",
    location: [],
    gender: "",
    ethnicity: "",
    instagram_handle: "",
    instagram_followers: "",
    bio: "",
    competitive_brand: "",
    availability: { status: "", note: "" },
    budget: { status: "", value: "" },
    interested_in: [],
    custom_answers: {},
    skills: [],
    work_links: [],
});

const filledForm = () => ({
    ...baseForm(),
    first_name: "Priya",
    last_name: "Shah",
    email: "priya@example.com",
    phone: "9999999999",
    dob: "1995-01-01",
    age: 30,
    height: "5'6\"",
    location: [{ city: "Mumbai" }],
    gender: "female",
    ethnicity: "south_asian",
    instagram_handle: "priya",
    instagram_followers: "10k-50k",
    bio: "An actor.",
    competitive_brand: "None",
    availability: { status: "yes", note: "" },
    budget: { status: "accept", value: "" },
    interested_in: ["Acting"],
});

function strictProject(overrides = {}) {
    return {
        custom_questions: [],
        submission_requirements: {
            strictness: "strict",
            fields: {},
            portfolio: {},
            skills: {},
            conditional_rules: [],
            ...overrides,
        },
    };
}

function findItem(items, id) {
    return items.find((item) => item.id === id);
}

// ---------------------------------------------------------------------------
// Required / Optional / Hidden
// ---------------------------------------------------------------------------

describe("computeRequirementItems — requirement tiers", () => {
    it("marks a profile field REQUIRED when config says so, and computes satisfied from form data", () => {
        const project = strictProject({ fields: { name: "required" } });
        const items = computeRequirementItems({ project, form: baseForm(), submission: null });
        const firstName = findItem(items, "first_name");
        expect(firstName.requirement).toBe(REQUIREMENT_TIERS.REQUIRED);
        expect(firstName.satisfied).toBe(false);

        const itemsFilled = computeRequirementItems({ project, form: filledForm(), submission: null });
        expect(findItem(itemsFilled, "first_name").satisfied).toBe(true);
    });

    it("marks a profile field OPTIONAL (not hidden) when config doesn't say required — this engine has no per-field hidden tier", () => {
        const project = strictProject({ fields: {} });
        const items = computeRequirementItems({ project, form: baseForm(), submission: null });
        const bio = findItem(items, "bio");
        expect(bio.requirement).toBe(REQUIREMENT_TIERS.OPTIONAL);
        // Still present in the model (not omitted) so section rollups can count it.
        expect(bio).toBeDefined();
    });

    it("marks a media category HIDDEN when its visibility is explicitly hidden, and still computes satisfied", () => {
        const project = strictProject({ intro_video: "hidden" });
        const submission = { media: [] };
        const items = computeRequirementItems({ project, form: baseForm(), submission });
        const intro = findItem(items, "intro_video");
        expect(intro.requirement).toBe(REQUIREMENT_TIERS.HIDDEN);
        expect(intro.satisfied).toBe(false);
    });

    it("marks a media category REQUIRED and checks the real media list for satisfaction", () => {
        const project = strictProject({ intro_video: "required" });
        const noVideo = computeRequirementItems({ project, form: baseForm(), submission: { media: [] } });
        expect(findItem(noVideo, "intro_video").requirement).toBe(REQUIREMENT_TIERS.REQUIRED);
        expect(findItem(noVideo, "intro_video").satisfied).toBe(false);

        const withVideo = computeRequirementItems({
            project,
            form: baseForm(),
            submission: { media: [{ category: "intro_video" }] },
        });
        expect(findItem(withVideo, "intro_video").satisfied).toBe(true);
    });

    it("derives portfolio visibility from min_count when no explicit visibility key is set", () => {
        const project = strictProject({ portfolio: { image: 3 } });
        const items = computeRequirementItems({ project, form: baseForm(), submission: { media: [] } });
        const portfolio = findItem(items, "portfolio_image");
        expect(portfolio.requirement).toBe(REQUIREMENT_TIERS.REQUIRED); // min_count > 0 implies required
        expect(portfolio.satisfied).toBe(false);
        expect(portfolio.label).toContain("minimum 3");
    });

    it("portfolio satisfied flips true once the media count reaches the configured minimum", () => {
        const project = strictProject({ portfolio: { indian: 2 }, portfolio_indian_visibility: "required" });
        const under = computeRequirementItems({
            project,
            form: baseForm(),
            submission: { media: [{ category: "indian" }] },
        });
        expect(findItem(under, "portfolio_indian").satisfied).toBe(false);

        const met = computeRequirementItems({
            project,
            form: baseForm(),
            submission: { media: [{ category: "indian" }, { category: "indian" }] },
        });
        expect(findItem(met, "portfolio_indian").satisfied).toBe(true);
    });

    it("work links visibility falls back to OPTIONAL when min_work_links is 0 and no explicit key is set", () => {
        const project = strictProject({ min_work_links: 0 });
        const items = computeRequirementItems({ project, form: baseForm(), submission: null });
        expect(findItem(items, "work_links").requirement).toBe(REQUIREMENT_TIERS.OPTIONAL);
    });
});

// ---------------------------------------------------------------------------
// Conditional requirements
// ---------------------------------------------------------------------------

describe("computeRequirementItems — conditional rules", () => {
    const project = strictProject({
        conditional_rules: [{ question_id: "q1", trigger_value: "Yes", video_label: "Comedy Take" }],
    });

    it("does not add a conditional item at all when the trigger question hasn't been answered with the trigger value", () => {
        const form = { ...baseForm(), custom_answers: { q1: "No" } };
        const items = computeRequirementItems({ project, form, submission: { media: [] } });
        expect(items.find((i) => i.id.startsWith("conditional_"))).toBeUndefined();
    });

    it("adds a REQUIRED conditional item once the trigger answer matches, unsatisfied until the labeled video exists", () => {
        const form = { ...baseForm(), custom_answers: { q1: "Yes" } };
        const withoutVideo = computeRequirementItems({ project, form, submission: { media: [] } });
        const conditional = withoutVideo.find((i) => i.id.startsWith("conditional_"));
        expect(conditional).toBeDefined();
        expect(conditional.requirement).toBe(REQUIREMENT_TIERS.REQUIRED);
        expect(conditional.satisfied).toBe(false);

        const withVideo = computeRequirementItems({
            project,
            form,
            submission: { media: [{ category: "take", label: "Comedy Take" }] },
        });
        expect(withVideo.find((i) => i.id.startsWith("conditional_")).satisfied).toBe(true);
    });

    it("trigger comparison is case- and whitespace-insensitive", () => {
        const form = { ...baseForm(), custom_answers: { q1: "  yes  " } };
        const items = computeRequirementItems({ project, form, submission: { media: [] } });
        expect(items.find((i) => i.id.startsWith("conditional_"))).toBeDefined();
    });
});

// ---------------------------------------------------------------------------
// Project configuration variations
// ---------------------------------------------------------------------------

describe("computeRequirementItems — project configuration variations", () => {
    it("falls back to the legacy 6-item rule set when the project has no submission_requirements at all", () => {
        const project = { custom_questions: [] };
        const items = computeRequirementItems({ project, form: baseForm(), submission: null });
        const ids = items.map((i) => i.id);
        expect(ids).toEqual(expect.arrayContaining(["first_name", "last_name", "height", "location", "availability", "budget"]));
        expect(items.every((i) => i.requirement === REQUIREMENT_TIERS.REQUIRED)).toBe(true);
    });

    it("returns an empty item list when submission_requirements exists but strictness isn't 'strict'", () => {
        const project = { custom_questions: [], submission_requirements: { strictness: "relaxed", fields: { name: "required" } } };
        const items = computeRequirementItems({ project, form: baseForm(), submission: null });
        expect(items).toEqual([]);
    });

    it("skills categories only appear as items when the project explicitly turns them on — no invented 'optional' entries", () => {
        const project = strictProject({ skills: { language: true, dance: false } });
        const form = { ...baseForm(), skills: [] };
        const items = computeRequirementItems({ project, form, submission: null });
        const skillItems = items.filter((i) => i.id.startsWith("skills_"));
        expect(skillItems).toHaveLength(1);
        expect(skillItems[0].id).toBe("skills_language");
        expect(skillItems[0].requirement).toBe(REQUIREMENT_TIERS.REQUIRED);
    });

    it("custom questions are enumerated from project.custom_questions, tiered by custom_questions config", () => {
        const project = strictProject({ custom_questions: { cq1: "required" } });
        project.custom_questions = [{ id: "cq1", question: "Why this role?" }, { id: "cq2", question: "Optional extra?" }];
        const form = { ...baseForm(), custom_answers: {} };
        const items = computeRequirementItems({ project, form, submission: null });
        expect(findItem(items, "cq_cq1").requirement).toBe(REQUIREMENT_TIERS.REQUIRED);
        expect(findItem(items, "cq_cq2").requirement).toBe(REQUIREMENT_TIERS.OPTIONAL);
    });
});

// ---------------------------------------------------------------------------
// Regression: identical results to the pre-extraction implementation.
//
// Before extraction, `getMissingRequirements()` only ever returned the
// UNSATISFIED + REQUIRED subset (it never surfaced optional/hidden items or
// satisfied ones). These cases reproduce specific scenarios reasoned through
// by hand during the extraction — especially the two-tier availability/
// budget conditional logic, which was the trickiest to get byte-identical —
// and assert the exact missing-item id set the old function would have
// produced, so a future change to this engine can't silently drift from that
// contract.
// ---------------------------------------------------------------------------

function missingRequiredIds(items) {
    return items
        .filter((i) => i.requirement === REQUIREMENT_TIERS.REQUIRED && !i.satisfied)
        .map((i) => i.id)
        .sort();
}

describe("computeRequirementItems — parity with the pre-extraction implementation", () => {
    it("legacy fallback: availability status invalid → only 'availability' is missing, not 'availability_note'", () => {
        const project = { custom_questions: [] };
        const form = { ...baseForm(), first_name: "A", last_name: "B", height: "5'0\"", location: [{ city: "X" }] };
        const items = computeRequirementItems({ project, form, submission: null });
        expect(missingRequiredIds(items)).toEqual(["availability", "budget"]);
    });

    it("legacy fallback: availability = 'no' without a note → only 'availability_note' missing, not 'availability'", () => {
        const project = { custom_questions: [] };
        const form = {
            ...baseForm(),
            first_name: "A", last_name: "B", height: "5'0\"", location: [{ city: "X" }],
            availability: { status: "no", note: "" },
            budget: { status: "accept", value: "" },
        };
        const items = computeRequirementItems({ project, form, submission: null });
        expect(missingRequiredIds(items)).toEqual(["availability_note"]);
    });

    it("legacy fallback: availability = 'no' WITH a note, budget = 'custom' WITH a value → fully satisfied", () => {
        const project = { custom_questions: [] };
        const form = {
            ...baseForm(),
            first_name: "A", last_name: "B", height: "5'0\"", location: [{ city: "X" }],
            availability: { status: "no", note: "Booked until June" },
            budget: { status: "custom", value: "$500/day" },
        };
        const items = computeRequirementItems({ project, form, submission: null });
        expect(missingRequiredIds(items)).toEqual([]);
    });

    it("strict mode: budget = 'custom' without a value → only 'budget_value' missing, 'budget' itself satisfied", () => {
        const project = strictProject({ fields: { budget_expectation: "required" } });
        const form = { ...baseForm(), budget: { status: "custom", value: "" } };
        const items = computeRequirementItems({ project, form, submission: null });
        expect(missingRequiredIds(items)).toEqual(["budget_value"]);
    });

    it("strict mode: a fully-configured required set with everything filled in produces zero missing items", () => {
        const project = strictProject({
            fields: {
                name: "required", height: "required", location: "required",
                availability: "required", budget_expectation: "required",
            },
            intro_video: "required",
            portfolio: { image: 1 },
            portfolio_image_visibility: "required",
        });
        const items = computeRequirementItems({
            project,
            form: filledForm(),
            submission: { media: [{ category: "intro_video" }, { category: "image" }] },
        });
        expect(missingRequiredIds(items)).toEqual([]);
    });

    it("strict mode: the same fully-configured set with nothing filled in produces the full expected missing set", () => {
        const project = strictProject({
            fields: {
                name: "required", height: "required", location: "required",
                availability: "required", budget_expectation: "required",
            },
            intro_video: "required",
            portfolio: { image: 1 },
            portfolio_image_visibility: "required",
        });
        const items = computeRequirementItems({ project, form: baseForm(), submission: { media: [] } });
        expect(missingRequiredIds(items)).toEqual(
            ["availability", "budget", "first_name", "height", "intro_video", "last_name", "location", "portfolio_image"].sort()
        );
    });
});
