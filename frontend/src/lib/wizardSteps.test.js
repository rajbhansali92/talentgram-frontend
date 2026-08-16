import { describe, it, expect } from "vitest";
import { WIZARD_STEPS, TOTAL_STEPS, stepForSection, sectionForStep, wizardStepsForDisplay } from "./wizardSteps";

describe("WIZARD_STEPS ordering (Phase 2 — new-talent flow reorder)", () => {
    it("has exactly 4 steps", () => {
        expect(TOTAL_STEPS).toBe(4);
        expect(WIZARD_STEPS).toHaveLength(4);
    });

    it("puts Project Questions first and Skills last, so a new talent starts submitting before any personal-info typing", () => {
        expect(WIZARD_STEPS[0].section).toBe("projectQuestions");
        expect(WIZARD_STEPS[1].section).toBe("uploads");
        expect(WIZARD_STEPS[2].section).toBe("profile");
        expect(WIZARD_STEPS[3].section).toBe("skills");
    });

    it("stepForSection/sectionForStep are inverses for every section", () => {
        for (const step of WIZARD_STEPS) {
            expect(stepForSection(step.section)).toBe(step.id);
            expect(sectionForStep(step.id)).toBe(step.section);
        }
    });

    it("stepForSection('projectQuestions') is step 1 — the returning-talent silent-recognition target", () => {
        expect(stepForSection("projectQuestions")).toBe(1);
    });

    it("stepForSection('uploads') is step 2 — where the submission draft must exist by (Media needs saved.id/token)", () => {
        expect(stepForSection("uploads")).toBe(2);
    });

    it("returns null for an unknown section", () => {
        expect(stepForSection("nonexistent")).toBeNull();
        expect(sectionForStep(99)).toBeNull();
    });
});

describe("wizardStepsForDisplay (Phase 3 — progress-indicator fix)", () => {
    it("shows all 4 steps for a new talent, regardless of currentStep", () => {
        expect(wizardStepsForDisplay({ isReturningTalent: false, currentStep: 1 })).toHaveLength(4);
        expect(wizardStepsForDisplay({ isReturningTalent: false, currentStep: 4 })).toHaveLength(4);
    });

    it("hides Basic Profile/Skills for a returning talent sitting on Project Questions or Media", () => {
        const atQuestions = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 1 });
        expect(atQuestions.map((s) => s.section)).toEqual(["projectQuestions", "uploads"]);

        const atMedia = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 2 });
        expect(atMedia.map((s) => s.section)).toEqual(["projectQuestions", "uploads"]);
    });

    it("reveals Basic Profile once a returning talent is actually routed there (something genuinely missing for this project)", () => {
        const atProfile = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 3 });
        expect(atProfile.map((s) => s.section)).toEqual(["projectQuestions", "uploads", "profile"]);
    });

    it("reveals all 4 once a returning talent is actually routed to Skills", () => {
        const atSkills = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 4 });
        expect(atSkills.map((s) => s.section)).toEqual(["projectQuestions", "uploads", "profile", "skills"]);
    });

    it("never affects currentStep or WIZARD_STEPS' own numbering — display-only", () => {
        wizardStepsForDisplay({ isReturningTalent: true, currentStep: 1 });
        expect(WIZARD_STEPS).toHaveLength(4);
        expect(stepForSection("profile")).toBe(3);
    });
});
