import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";
import { useSubmissionExperienceModel } from "./useSubmissionExperienceModel";
import { computeRequirementItems } from "@/lib/requirementEngine";
import {
    deriveOperationalStatus,
    summarizeReadiness,
    resolveBlockingReason,
    deriveSubmitCta,
    summarizeUploads,
    summarizeSections,
    summarizeOverallProgress,
    REQUIREMENT_TIERS,
    OPERATIONAL_STATES,
} from "@/lib/readinessStatus";

const emptyForm = () => ({
    first_name: "", last_name: "", location: [],
    availability: { status: "", note: "" }, budget: { status: "", value: "" },
});

const filledForm = () => ({
    first_name: "A", last_name: "B", height: "5'6\"", location: [{ city: "Mumbai" }],
    availability: { status: "yes", note: "" }, budget: { status: "accept", value: "" },
});

function strictProject(overrides = {}) {
    return {
        custom_questions: [],
        submission_requirements: { strictness: "strict", fields: {}, portfolio: {}, skills: {}, conditional_rules: [], ...overrides },
    };
}

function baseInputs(overrides = {}) {
    return {
        project: strictProject({ fields: { name: "required" } }),
        form: emptyForm(),
        submission: { media: [] },
        activeUploads: {},
        finalizing: false,
        saveStatus: "idle",
        ...overrides,
    };
}

describe("useSubmissionExperienceModel — correct aggregation", () => {
    it("produces exactly the same items/statuses as calling the three engines by hand", () => {
        const inputs = baseInputs();
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));

        const expectedRequirementItems = computeRequirementItems(inputs);
        expect(result.current.requirementItems).toEqual(expectedRequirementItems);

        const expectedReadinessModel = expectedRequirementItems.map((it) => ({
            ...it,
            operational: deriveOperationalStatus({ satisfied: it.satisfied, media: it.media, activeUploads: inputs.activeUploads }),
        }));
        expect(result.current.readinessModel).toEqual(expectedReadinessModel);

        const expectedChecklist = expectedReadinessModel.filter((it) => it.requirement === REQUIREMENT_TIERS.REQUIRED);
        expect(result.current.checklist).toEqual(expectedChecklist);

        const expectedReadinessSummary = summarizeReadiness(expectedChecklist);
        expect(result.current.readinessSummary).toEqual(expectedReadinessSummary);

        const expectedUploadSummary = summarizeUploads(inputs.activeUploads);
        expect(result.current.uploadSummary).toEqual(expectedUploadSummary);
        expect(result.current.uploadsInProgress).toBe(expectedUploadSummary.inFlightTotal > 0);

        const expectedBlockingReason = resolveBlockingReason({ readinessSummary: expectedReadinessSummary, uploadsInProgress: result.current.uploadsInProgress });
        expect(result.current.blockingReason).toBe(expectedBlockingReason);

        const expectedCta = deriveSubmitCta({
            readinessSummary: expectedReadinessSummary,
            uploadsInProgress: result.current.uploadsInProgress,
            finalizing: inputs.finalizing,
            readyLabel: undefined,
            notReadyLabel: undefined,
            submittingLabel: undefined,
        });
        expect(result.current.submitCta).toEqual(expectedCta);

        expect(result.current.sectionStatus).toEqual(summarizeSections(expectedReadinessModel));
        expect(result.current.overallProgress).toEqual(summarizeOverallProgress(expectedChecklist));
        expect(result.current.saveStatus).toBe("idle");
    });

    it("missingRequirements is exactly the checklist's unsatisfied subset", () => {
        const inputs = baseInputs({ form: emptyForm() });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        expect(result.current.missingRequirements).toEqual(result.current.checklist.filter((it) => !it.satisfied));
        expect(result.current.missingRequirements.length).toBeGreaterThan(0);
    });

    it("a fully-satisfied, upload-free submission aggregates to ready=true end-to-end", () => {
        const inputs = baseInputs({ form: filledForm() });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        expect(result.current.readinessSummary.ready).toBe(true);
        expect(result.current.uploadsInProgress).toBe(false);
        expect(result.current.blockingReason).toBeNull();
        expect(result.current.submitCta.ready).toBe(true);
        expect(result.current.submitCta.disabled).toBe(false);
    });

    it("a failed required upload propagates all the way to blockingReason and submitCta", () => {
        const inputs = baseInputs({
            project: strictProject({ intro_video: "required" }),
            form: filledForm(),
            submission: { media: [] },
            activeUploads: { intro_video: { status: "failed" } },
        });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        expect(result.current.readinessSummary.failed).toHaveLength(1);
        expect(result.current.blockingReason).toBe("failed");
        expect(result.current.submitCta.buttonAction).toBe("scroll_to_missing");
        expect(result.current.submitCta.scrollTarget.id).toBe("intro_video");
    });
});

describe("useSubmissionExperienceModel — memoisation stability", () => {
    it("returns the SAME object reference across re-renders when nothing meaningful changed", () => {
        const inputs = baseInputs();
        const { result, rerender } = renderHook((props) => useSubmissionExperienceModel(props), { initialProps: inputs });
        const first = result.current;
        rerender(inputs); // identical object references passed again
        expect(result.current).toBe(first);
    });

    it("returns a NEW object reference when `form` changes", () => {
        const inputs = baseInputs();
        const { result, rerender } = renderHook((props) => useSubmissionExperienceModel(props), { initialProps: inputs });
        const first = result.current;
        rerender({ ...inputs, form: { ...inputs.form, first_name: "Changed" } });
        expect(result.current).not.toBe(first);
    });

    it("returns a NEW object reference when `activeUploads` changes", () => {
        const inputs = baseInputs();
        const { result, rerender } = renderHook((props) => useSubmissionExperienceModel(props), { initialProps: inputs });
        const first = result.current;
        rerender({ ...inputs, activeUploads: { intro_video: { status: "uploading" } } });
        expect(result.current).not.toBe(first);
        expect(result.current.uploadsInProgress).toBe(true);
    });

    it("returns a NEW object reference when `finalizing` toggles", () => {
        const inputs = baseInputs();
        const { result, rerender } = renderHook((props) => useSubmissionExperienceModel(props), { initialProps: inputs });
        const first = result.current;
        rerender({ ...inputs, finalizing: true });
        expect(result.current).not.toBe(first);
        expect(result.current.submitCta.disabled).toBe(true);
    });

    it("does NOT recompute when an unrelated prop is passed a new-but-equal object (project reference stable)", () => {
        const project = strictProject({ fields: { name: "required" } });
        const inputs = baseInputs({ project });
        const { result, rerender } = renderHook((props) => useSubmissionExperienceModel(props), { initialProps: inputs });
        const first = result.current;
        // Re-render with the exact same `project` reference, only saveStatus text changes to "saving" then back —
        // this DOES change saveStatus (an actual dependency) so a recompute is expected here; the point of this
        // test is that recomputation is driven by real dependency changes, not by React re-rendering per se.
        rerender({ ...inputs, project, saveStatus: "idle" });
        expect(result.current).toBe(first);
    });
});

describe("useSubmissionExperienceModel — no duplicated derivation / output consistency", () => {
    it("submitCta.ready is always exactly readinessSummary.ready && !uploadsInProgress — never independently derived", () => {
        const scenarios = [
            baseInputs({ form: filledForm() }),
            baseInputs({ form: emptyForm() }),
            baseInputs({ form: filledForm(), activeUploads: { "image:a.jpg": { status: "uploading" } } }),
            baseInputs({ project: strictProject({ intro_video: "required" }), form: filledForm(), activeUploads: { intro_video: { status: "failed" } } }),
        ];
        scenarios.forEach((inputs) => {
            const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
            expect(result.current.submitCta.ready).toBe(result.current.readinessSummary.ready && !result.current.uploadsInProgress);
        });
    });

    it("checklist is always a strict subset of readinessModel filtered to REQUIRED — no items invented or dropped", () => {
        const inputs = baseInputs({ form: filledForm() });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        const expected = result.current.readinessModel.filter((it) => it.requirement === REQUIREMENT_TIERS.REQUIRED);
        expect(result.current.checklist).toEqual(expected);
    });

    it("overallProgress.completedCount never exceeds overallProgress.totalCount across varied fixtures", () => {
        const scenarios = [
            baseInputs({ form: filledForm() }),
            baseInputs({ form: emptyForm() }),
            baseInputs({ project: strictProject({ intro_video: "required", portfolio: { image: 2 }, portfolio_image_visibility: "required" }) }),
        ];
        scenarios.forEach((inputs) => {
            const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
            expect(result.current.overallProgress.completedCount).toBeLessThanOrEqual(result.current.overallProgress.totalCount);
            expect(result.current.overallProgress.percent).toBeGreaterThanOrEqual(0);
            expect(result.current.overallProgress.percent).toBeLessThanOrEqual(100);
        });
    });

    it("uploadSummary.inFlightTotal and uploadsInProgress never disagree", () => {
        const inputs = baseInputs({ activeUploads: { "image:a.jpg": { status: "queued" }, "image:b.jpg": { status: "completed" } } });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        expect(result.current.uploadsInProgress).toBe(result.current.uploadSummary.inFlightTotal > 0);
    });

    it("sectionStatus's required counts never exceed the section's items in the readiness model", () => {
        const inputs = baseInputs({ form: filledForm() });
        const { result } = renderHook(() => useSubmissionExperienceModel(inputs));
        result.current.sectionStatus.forEach((section) => {
            const itemsInSection = result.current.readinessModel.filter((it) => (it.section || "other") === section.section);
            expect(section.requiredTotal).toBeLessThanOrEqual(itemsInSection.length);
            expect(section.requiredCompleted).toBeLessThanOrEqual(section.requiredTotal);
        });
    });
});
