import { useMemo } from "react";
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
} from "@/lib/readinessStatus";

/**
 * THE SUBMISSION EXPERIENCE MODEL — the single presentation model for the
 * submission page. It aggregates the outputs of the three engines:
 *
 *   Requirement Engine  (lib/requirementEngine.js)  — what's required, is it
 *                                                      satisfied?
 *   Operational Engine  (lib/readinessStatus.js)     — what's the Upload
 *                                                      Manager actually doing?
 *   Readiness Engine    (lib/readinessStatus.js)     — combine the two into
 *                                                      submission/section/CTA
 *                                                      summaries.
 *
 * This hook does NOT decide any business rule itself — it only calls the
 * engines, in order, and hands their outputs to the caller as one object. No
 * UI component (SubmissionReadinessPanel, the Submit button, the sticky
 * footer, the Upload Manager, section headers, validation messages) should
 * independently re-derive any of this — they should all read from the one
 * model this hook returns.
 *
 * @returns {{
 *   requirementItems: object[],   // raw Requirement Engine output (requirement + satisfied, no operational yet)
 *   readinessModel: object[],     // requirementItems + `operational`, ALL tiers (required/optional/hidden)
 *   checklist: object[],          // readinessModel filtered to REQUIRED only — what SubmissionReadinessPanel renders
 *   missingRequirements: object[],// checklist items that aren't satisfied yet (the Submit-click validation gate)
 *   readinessSummary: object,     // summarizeReadiness(checklist) — { ready, blockingReason, failed, inFlight, missing }
 *   uploadSummary: object,        // summarizeUploads(activeUploads) — counts across ALL uploads, any category
 *   uploadsInProgress: boolean,   // uploadSummary.inFlightTotal > 0
 *   blockingReason: string|null,  // resolveBlockingReason(...) — SUBMIT_BLOCKING_REASONS.* or null
 *   submitCta: object,            // deriveSubmitCta(...) — the Submit button's entire state
 *   sectionStatus: object[],      // summarizeSections(readinessModel) — per-section rollup
 *   overallProgress: object,      // summarizeOverallProgress(checklist) — { completedCount, totalCount, percent }
 *   saveStatus: string,           // passthrough (autosave indicator state — not a readiness concern, kept here so
 *                                 // every page-level UI signal lives in one place)
 * }}
 */
export function useSubmissionExperienceModel({
    project,
    form,
    submission,
    activeUploads,
    finalizing,
    saveStatus,
    readyLabel,
    notReadyLabel,
    submittingLabel,
}) {
    return useMemo(() => {
        const requirementItems = computeRequirementItems({ project, form, submission });

        const readinessModel = requirementItems.map((item) => ({
            ...item,
            operational: deriveOperationalStatus({ satisfied: item.satisfied, media: item.media, activeUploads }),
        }));

        const checklist = readinessModel.filter((item) => item.requirement === REQUIREMENT_TIERS.REQUIRED);
        const missingRequirements = checklist.filter((item) => !item.satisfied);

        const readinessSummary = summarizeReadiness(checklist);
        const uploadSummary = summarizeUploads(activeUploads);
        const uploadsInProgress = uploadSummary.inFlightTotal > 0;
        const blockingReason = resolveBlockingReason({ readinessSummary, uploadsInProgress });

        const submitCta = deriveSubmitCta({
            readinessSummary,
            uploadsInProgress,
            finalizing,
            readyLabel,
            notReadyLabel,
            submittingLabel,
        });

        const sectionStatus = summarizeSections(readinessModel);
        const overallProgress = summarizeOverallProgress(checklist);

        return {
            requirementItems,
            readinessModel,
            checklist,
            missingRequirements,
            readinessSummary,
            uploadSummary,
            uploadsInProgress,
            blockingReason,
            submitCta,
            sectionStatus,
            overallProgress,
            saveStatus,
        };
    }, [project, form, submission, activeUploads, finalizing, saveStatus, readyLabel, notReadyLabel, submittingLabel]);
}
