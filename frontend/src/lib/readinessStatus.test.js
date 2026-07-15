import { describe, it, expect } from "vitest";
import {
    REQUIREMENT_TIERS,
    OPERATIONAL_STATES,
    SUBMIT_BLOCKING_REASONS,
    CTA_ACTIONS,
    SECTION_STATUS,
    IN_FLIGHT_OPERATIONAL_STATES,
    mapUploadManagerStatus,
    findActiveUploadStatus,
    deriveOperationalStatus,
    summarizeReadiness,
    resolveBlockingReason,
    deriveSubmitCta,
    summarizeUploads,
    summarizeSections,
    summarizeOverallProgress,
} from "./readinessStatus";

// ---------------------------------------------------------------------------
// Operational Engine
// ---------------------------------------------------------------------------

describe("mapUploadManagerStatus — every upload lifecycle state", () => {
    it.each([
        ["compressing", OPERATIONAL_STATES.UPLOADING],
        ["uploading", OPERATIONAL_STATES.UPLOADING],
        ["processing", OPERATIONAL_STATES.PROCESSING],
        ["completed", OPERATIONAL_STATES.COMPLETED],
        ["failed", OPERATIONAL_STATES.FAILED],
        ["queued", OPERATIONAL_STATES.QUEUED],
        ["retrying", OPERATIONAL_STATES.RETRYING],
        ["waiting", OPERATIONAL_STATES.WAITING],
    ])("maps raw status %s -> %s", (raw, expected) => {
        expect(mapUploadManagerStatus(raw)).toBe(expected);
    });

    it("returns null for an unknown/raw upload status the Upload Manager might one day introduce", () => {
        expect(mapUploadManagerStatus("some_future_engine_state")).toBeNull();
        expect(mapUploadManagerStatus(undefined)).toBeNull();
        expect(mapUploadManagerStatus("")).toBeNull();
    });
});

describe("findActiveUploadStatus", () => {
    it("returns null when nothing matches the prefix", () => {
        expect(findActiveUploadStatus({ "image:a.jpg": { status: "uploading" } }, "intro_video")).toBeNull();
    });

    it("matches an exact slot key (single-slot categories like intro_video)", () => {
        expect(findActiveUploadStatus({ intro_video: { status: "uploading" } }, "intro_video")).toBe(OPERATIONAL_STATES.UPLOADING);
    });

    it("matches a namespaced slot key (category:label, e.g. multi-image categories)", () => {
        const activeUploads = { "image:photo1.jpg": { status: "processing" } };
        expect(findActiveUploadStatus(activeUploads, "image:")).toBe(OPERATIONAL_STATES.PROCESSING);
    });

    it("ignores unrelated slots with an unknown raw status instead of matching them", () => {
        const activeUploads = { "take:Take 1": { status: "some_future_engine_state" } };
        expect(findActiveUploadStatus(activeUploads, "take")).toBeNull();
    });

    it("picks the highest-priority status when multiple slots share a prefix — failed wins over uploading", () => {
        const activeUploads = {
            "image:a.jpg": { status: "uploading" },
            "image:b.jpg": { status: "failed" },
        };
        expect(findActiveUploadStatus(activeUploads, "image:")).toBe(OPERATIONAL_STATES.FAILED);
    });

    it("picks retrying over waiting over uploading over queued over processing per priority order", () => {
        const activeUploads = {
            "take:a": { status: "queued" },
            "take:b": { status: "processing" },
            "take:c": { status: "waiting" },
        };
        expect(findActiveUploadStatus(activeUploads, "take")).toBe(OPERATIONAL_STATES.WAITING);
    });
});

describe("deriveOperationalStatus — combining validation + Upload Manager", () => {
    it("satisfied always wins, regardless of any concurrent upload activity", () => {
        const activeUploads = { intro_video: { status: "failed" } };
        expect(deriveOperationalStatus({ satisfied: true, media: { prefix: "intro_video" }, activeUploads })).toBe(OPERATIONAL_STATES.COMPLETED);
    });

    it("unsatisfied, no media tag at all -> MISSING (e.g. a plain text field)", () => {
        expect(deriveOperationalStatus({ satisfied: false, media: undefined, activeUploads: {} })).toBe(OPERATIONAL_STATES.MISSING);
    });

    it("unsatisfied, media tag present but nothing active in the Upload Manager -> MISSING", () => {
        expect(deriveOperationalStatus({ satisfied: false, media: { prefix: "intro_video" }, activeUploads: {} })).toBe(OPERATIONAL_STATES.MISSING);
    });

    it.each([
        ["queued", OPERATIONAL_STATES.QUEUED],
        ["uploading", OPERATIONAL_STATES.UPLOADING],
        ["compressing", OPERATIONAL_STATES.UPLOADING],
        ["processing", OPERATIONAL_STATES.PROCESSING],
        ["retrying", OPERATIONAL_STATES.RETRYING],
        ["waiting", OPERATIONAL_STATES.WAITING],
        ["failed", OPERATIONAL_STATES.FAILED],
    ])("unsatisfied + active upload with raw status %s -> %s", (rawStatus, expected) => {
        const activeUploads = { intro_video: { status: rawStatus } };
        expect(deriveOperationalStatus({ satisfied: false, media: { prefix: "intro_video" }, activeUploads })).toBe(expected);
    });

    it("unsatisfied + an active upload whose raw status this engine doesn't recognize -> falls back to MISSING, not a crash", () => {
        const activeUploads = { intro_video: { status: "some_future_engine_state" } };
        expect(deriveOperationalStatus({ satisfied: false, media: { prefix: "intro_video" }, activeUploads })).toBe(OPERATIONAL_STATES.MISSING);
    });
});

describe("summarizeUploads", () => {
    it("counts every recognized status across ALL activeUploads, required or optional", () => {
        const activeUploads = {
            "image:a.jpg": { status: "uploading" },
            "image:b.jpg": { status: "completed" },
            "take:c": { status: "failed" },
            "take:d": { status: "queued" },
        };
        const summary = summarizeUploads(activeUploads);
        expect(summary.total).toBe(4);
        expect(summary.counts[OPERATIONAL_STATES.UPLOADING]).toBe(1);
        expect(summary.counts[OPERATIONAL_STATES.COMPLETED]).toBe(1);
        expect(summary.failedTotal).toBe(1);
        expect(summary.inFlightTotal).toBe(2); // uploading + queued
    });

    it("handles an empty/undefined activeUploads map without throwing", () => {
        expect(summarizeUploads({})).toEqual({
            counts: expect.any(Object),
            total: 0,
            inFlightTotal: 0,
            failedTotal: 0,
            completedTotal: 0,
        });
        expect(summarizeUploads(undefined).total).toBe(0);
    });
});

// ---------------------------------------------------------------------------
// Readiness Engine
// ---------------------------------------------------------------------------

function item(overrides) {
    return { id: "x", label: "X", section: "profile", requirement: REQUIREMENT_TIERS.REQUIRED, satisfied: false, ...overrides };
}

describe("summarizeReadiness", () => {
    it("READY: every item COMPLETED", () => {
        const items = [item({ operational: OPERATIONAL_STATES.COMPLETED }), item({ id: "y", operational: OPERATIONAL_STATES.COMPLETED })];
        const summary = summarizeReadiness(items);
        expect(summary.ready).toBe(true);
        expect(summary.blockingReason).toBeNull();
        expect(summary.failed).toHaveLength(0);
        expect(summary.inFlight).toHaveLength(0);
        expect(summary.missing).toHaveLength(0);
    });

    it("MISSING: an item with no data and no active upload", () => {
        const items = [item({ operational: OPERATIONAL_STATES.MISSING })];
        const summary = summarizeReadiness(items);
        expect(summary.ready).toBe(false);
        expect(summary.blockingReason).toBe(SUBMIT_BLOCKING_REASONS.MISSING);
        expect(summary.missing).toHaveLength(1);
    });

    it.each([OPERATIONAL_STATES.QUEUED, OPERATIONAL_STATES.UPLOADING, OPERATIONAL_STATES.RETRYING, OPERATIONAL_STATES.WAITING, OPERATIONAL_STATES.PROCESSING])(
        "WAITING: an item with operational=%s counts as in-flight, not missing",
        (operational) => {
            const items = [item({ operational })];
            const summary = summarizeReadiness(items);
            expect(summary.ready).toBe(false);
            expect(summary.blockingReason).toBe(SUBMIT_BLOCKING_REASONS.WAITING);
            expect(summary.inFlight).toHaveLength(1);
            expect(summary.missing).toHaveLength(0);
        }
    );

    it("FAILED: an item whose upload failed", () => {
        const items = [item({ operational: OPERATIONAL_STATES.FAILED })];
        const summary = summarizeReadiness(items);
        expect(summary.ready).toBe(false);
        expect(summary.blockingReason).toBe(SUBMIT_BLOCKING_REASONS.FAILED);
        expect(summary.failed).toHaveLength(1);
    });

    it("blocking reason priority: FAILED beats WAITING beats MISSING when all three occur simultaneously", () => {
        const items = [
            item({ id: "a", operational: OPERATIONAL_STATES.MISSING }),
            item({ id: "b", operational: OPERATIONAL_STATES.UPLOADING }),
            item({ id: "c", operational: OPERATIONAL_STATES.FAILED }),
        ];
        const summary = summarizeReadiness(items);
        expect(summary.blockingReason).toBe(SUBMIT_BLOCKING_REASONS.FAILED);
        expect(summary.failed).toHaveLength(1);
        expect(summary.inFlight).toHaveLength(1);
        expect(summary.missing).toHaveLength(1);
    });

    it("blocking reason priority: WAITING beats MISSING when both occur but nothing failed", () => {
        const items = [
            item({ id: "a", operational: OPERATIONAL_STATES.MISSING }),
            item({ id: "b", operational: OPERATIONAL_STATES.PROCESSING }),
        ];
        const summary = summarizeReadiness(items);
        expect(summary.blockingReason).toBe(SUBMIT_BLOCKING_REASONS.WAITING);
    });

    it("multiple simultaneous failures are all collected, not just the first", () => {
        const items = [
            item({ id: "a", operational: OPERATIONAL_STATES.FAILED }),
            item({ id: "b", operational: OPERATIONAL_STATES.FAILED }),
            item({ id: "c", operational: OPERATIONAL_STATES.COMPLETED }),
        ];
        const summary = summarizeReadiness(items);
        expect(summary.failed.map((i) => i.id)).toEqual(["a", "b"]);
    });

    it("an empty item list is trivially ready (nothing required at all)", () => {
        expect(summarizeReadiness([]).ready).toBe(true);
    });
});

describe("resolveBlockingReason", () => {
    it("FAILED from readinessSummary always wins, even if uploadsInProgress is also true", () => {
        const readinessSummary = { ready: false, blockingReason: SUBMIT_BLOCKING_REASONS.FAILED, failed: [item({})], inFlight: [], missing: [] };
        expect(resolveBlockingReason({ readinessSummary, uploadsInProgress: true })).toBe(SUBMIT_BLOCKING_REASONS.FAILED);
    });

    it("uploadsInProgress alone (e.g. an optional upload) produces WAITING even when readinessSummary itself is ready", () => {
        const readinessSummary = { ready: true, blockingReason: null, failed: [], inFlight: [], missing: [] };
        expect(resolveBlockingReason({ readinessSummary, uploadsInProgress: true })).toBe(SUBMIT_BLOCKING_REASONS.WAITING);
    });

    it("MISSING passes through when nothing is failed or in flight", () => {
        const readinessSummary = { ready: false, blockingReason: SUBMIT_BLOCKING_REASONS.MISSING, failed: [], inFlight: [], missing: [item({})] };
        expect(resolveBlockingReason({ readinessSummary, uploadsInProgress: false })).toBe(SUBMIT_BLOCKING_REASONS.MISSING);
    });

    it("returns null when nothing is blocking submission", () => {
        const readinessSummary = { ready: true, blockingReason: null, failed: [], inFlight: [], missing: [] };
        expect(resolveBlockingReason({ readinessSummary, uploadsInProgress: false })).toBeNull();
    });
});

describe("summarizeSections", () => {
    it("a section with zero required items is OPTIONAL regardless of its optional items' state", () => {
        const model = [item({ id: "a", section: "profile", requirement: REQUIREMENT_TIERS.OPTIONAL, operational: OPERATIONAL_STATES.MISSING })];
        const [section] = summarizeSections(model);
        expect(section.status).toBe(SECTION_STATUS.OPTIONAL);
        expect(section.requiredTotal).toBe(0);
    });

    it("a section with a failed required item is ATTENTION even if other required items are complete", () => {
        const model = [
            item({ id: "a", section: "uploads", operational: OPERATIONAL_STATES.COMPLETED }),
            item({ id: "b", section: "uploads", operational: OPERATIONAL_STATES.FAILED }),
        ];
        const [section] = summarizeSections(model);
        expect(section.status).toBe(SECTION_STATUS.ATTENTION);
    });

    it("a section with all required items complete is COMPLETE", () => {
        const model = [item({ id: "a", section: "profile", operational: OPERATIONAL_STATES.COMPLETED })];
        const [section] = summarizeSections(model);
        expect(section.status).toBe(SECTION_STATUS.COMPLETE);
    });

    it("a section with a required item still uploading (nothing failed) is IN_PROGRESS", () => {
        const model = [item({ id: "a", section: "uploads", operational: OPERATIONAL_STATES.UPLOADING })];
        const [section] = summarizeSections(model);
        expect(section.status).toBe(SECTION_STATUS.IN_PROGRESS);
    });

    it("a section with a plain missing required item (nothing failed/in-flight) is INCOMPLETE", () => {
        const model = [item({ id: "a", section: "profile", operational: OPERATIONAL_STATES.MISSING })];
        const [section] = summarizeSections(model);
        expect(section.status).toBe(SECTION_STATUS.INCOMPLETE);
    });
});

describe("summarizeOverallProgress", () => {
    it("computes a percentage from completed/total required items", () => {
        const checklist = [
            item({ id: "a", operational: OPERATIONAL_STATES.COMPLETED }),
            item({ id: "b", operational: OPERATIONAL_STATES.MISSING }),
        ];
        expect(summarizeOverallProgress(checklist)).toEqual({ completedCount: 1, totalCount: 2, percent: 50 });
    });

    it("treats zero required items as 100% (nothing left to do)", () => {
        expect(summarizeOverallProgress([])).toEqual({ completedCount: 0, totalCount: 0, percent: 100 });
    });
});

// ---------------------------------------------------------------------------
// Submit CTA
// ---------------------------------------------------------------------------

describe("deriveSubmitCta", () => {
    const readyToSubmit = { ready: true, blockingReason: null, failed: [], inFlight: [], missing: [] };

    it("READY: submittable, action SUBMIT, not disabled", () => {
        const cta = deriveSubmitCta({ readinessSummary: readyToSubmit, uploadsInProgress: false, finalizing: false });
        expect(cta).toMatchObject({
            ready: true,
            buttonLabel: "Submit Application",
            buttonAction: CTA_ACTIONS.SUBMIT,
            disabled: false,
            firstMissingRequirement: null,
            scrollTarget: null,
        });
    });

    it("MISSING: not ready, action SCROLL_TO_MISSING, points at the missing item, not disabled", () => {
        const missingItem = item({ id: "bio", operational: OPERATIONAL_STATES.MISSING });
        const readinessSummary = { ready: false, blockingReason: SUBMIT_BLOCKING_REASONS.MISSING, failed: [], inFlight: [], missing: [missingItem] };
        const cta = deriveSubmitCta({ readinessSummary, uploadsInProgress: false, finalizing: false });
        expect(cta.ready).toBe(false);
        expect(cta.buttonAction).toBe(CTA_ACTIONS.SCROLL_TO_MISSING);
        expect(cta.buttonLabel).toBe("Complete Remaining Items");
        expect(cta.disabled).toBe(false);
        expect(cta.scrollTarget).toBe(missingItem);
        expect(cta.firstMissingRequirement).toBe(missingItem);
    });

    it("WAITING (required item mid-upload): not ready, points at the in-flight item so the talent can go check on it", () => {
        const inFlightItem = item({ id: "intro_video", operational: OPERATIONAL_STATES.UPLOADING });
        const readinessSummary = { ready: false, blockingReason: SUBMIT_BLOCKING_REASONS.WAITING, failed: [], inFlight: [inFlightItem], missing: [] };
        const cta = deriveSubmitCta({ readinessSummary, uploadsInProgress: true, finalizing: false });
        expect(cta.ready).toBe(false);
        expect(cta.buttonAction).toBe(CTA_ACTIONS.SCROLL_TO_MISSING);
        expect(cta.disabled).toBe(false);
        expect(cta.scrollTarget).toBe(inFlightItem);
    });

    it("FAILED: not ready, points at the failed item first (ahead of any missing item)", () => {
        const failedItem = item({ id: "intro_video", operational: OPERATIONAL_STATES.FAILED });
        const missingItem = item({ id: "bio", operational: OPERATIONAL_STATES.MISSING });
        const readinessSummary = { ready: false, blockingReason: SUBMIT_BLOCKING_REASONS.FAILED, failed: [failedItem], inFlight: [], missing: [missingItem] };
        const cta = deriveSubmitCta({ readinessSummary, uploadsInProgress: false, finalizing: false });
        expect(cta.buttonAction).toBe(CTA_ACTIONS.SCROLL_TO_MISSING);
        expect(cta.scrollTarget).toBe(failedItem);
    });

    it("FINALIZING: overrides everything else, always disabled with the submitting label, even if otherwise ready", () => {
        const cta = deriveSubmitCta({ readinessSummary: readyToSubmit, uploadsInProgress: false, finalizing: true });
        expect(cta).toMatchObject({
            ready: false,
            buttonLabel: "Submitting…",
            buttonAction: CTA_ACTIONS.SUBMIT,
            disabled: true,
        });
    });

    it("UPLOADS IN PROGRESS with an otherwise-fully-satisfied readiness model (e.g. an optional photo mid-upload): not ready, and disabled — nothing actionable to scroll to", () => {
        const cta = deriveSubmitCta({ readinessSummary: readyToSubmit, uploadsInProgress: true, finalizing: false });
        expect(cta.ready).toBe(false);
        expect(cta.buttonAction).toBe(CTA_ACTIONS.SCROLL_TO_MISSING);
        expect(cta.disabled).toBe(true);
        expect(cta.scrollTarget).toBeNull();
    });

    it("respects custom ready/not-ready/submitting labels per flow", () => {
        const cta = deriveSubmitCta({
            readinessSummary: readyToSubmit,
            uploadsInProgress: false,
            finalizing: false,
            readyLabel: "Submit Audition",
        });
        expect(cta.buttonLabel).toBe("Submit Audition");
    });
});
