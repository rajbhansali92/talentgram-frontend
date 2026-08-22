import { describe, it, expect } from "vitest";
import { WIZARD_STEPS, TOTAL_STEPS, stepForSection, sectionForStep, wizardStepsForDisplay } from "./wizardSteps";

describe("WIZARD_STEPS ordering (flow-simplification pass — media + skills-as-own-step removed)", () => {
    it("has exactly 2 steps", () => {
        expect(TOTAL_STEPS).toBe(2);
        expect(WIZARD_STEPS).toHaveLength(2);
    });

    it("puts Project Questions first and Basic Profile last", () => {
        expect(WIZARD_STEPS[0].section).toBe("projectQuestions");
        expect(WIZARD_STEPS[1].section).toBe("profile");
    });

    it("stepForSection/sectionForStep are inverses for every real WIZARD_STEPS section", () => {
        for (const step of WIZARD_STEPS) {
            expect(stepForSection(step.section)).toBe(step.id);
            expect(sectionForStep(step.id)).toBe(step.section);
        }
    });

    it("stepForSection('projectQuestions') is step 1 — the returning-talent silent-recognition target", () => {
        expect(stepForSection("projectQuestions")).toBe(1);
    });

    it("stepForSection('skills') resolves to the same step as 'profile' — skills content now renders alongside it, not on its own page", () => {
        expect(stepForSection("skills")).toBe(2);
        expect(stepForSection("profile")).toBe(2);
    });

    it("stepForSection('uploads') returns null — the media step no longer exists", () => {
        expect(stepForSection("uploads")).toBeNull();
    });

    it("returns null for an unknown section", () => {
        expect(stepForSection("nonexistent")).toBeNull();
        expect(sectionForStep(99)).toBeNull();
    });
});

describe("wizardStepsForDisplay (progress-indicator fix)", () => {
    it("shows both steps for a new talent, regardless of currentStep", () => {
        expect(wizardStepsForDisplay({ isReturningTalent: false, currentStep: 1 })).toHaveLength(2);
        expect(wizardStepsForDisplay({ isReturningTalent: false, currentStep: 2 })).toHaveLength(2);
    });

    it("hides Basic Profile for a returning talent sitting on Project Questions", () => {
        const atQuestions = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 1 });
        expect(atQuestions.map((s) => s.section)).toEqual(["projectQuestions"]);
    });

    it("reveals Basic Profile once a returning talent is actually routed there (something genuinely missing for this project)", () => {
        const atProfile = wizardStepsForDisplay({ isReturningTalent: true, currentStep: 2 });
        expect(atProfile.map((s) => s.section)).toEqual(["projectQuestions", "profile"]);
    });

    it("never affects currentStep or WIZARD_STEPS' own numbering — display-only", () => {
        wizardStepsForDisplay({ isReturningTalent: true, currentStep: 1 });
        expect(WIZARD_STEPS).toHaveLength(2);
        expect(stepForSection("profile")).toBe(2);
    });
});
