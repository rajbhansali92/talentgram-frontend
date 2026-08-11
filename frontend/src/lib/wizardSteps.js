// THE WIZARD STEP CONFIG — single source of truth for how the Submission
// Wizard's 4 steps map onto the Requirement Engine's `section` tags
// (see lib/requirementEngine.js). Nothing here decides what's required or
// satisfied — that's still entirely the Requirement/Readiness Engines. This
// module only decides step ORDER and LABELS, plus how the current step
// persists across a refresh.
//
// WIZARD_STEPS is consumed by:
//   - WizardProgressBar (renders the step list + progress)
//   - WizardStepNav (Back/Next, gates Next on the current step's `section`)
//   - SubmissionPage.jsx's step-visibility gates and `ensureRequirementVisible`
export const WIZARD_STEPS = Object.freeze([
    { id: 1, key: "profile", label: "Basic Profile", section: "profile" },
    { id: 2, key: "skills", label: "Skills & Attributes", section: "skills" },
    { id: 3, key: "projectQuestions", label: "Project Info", section: "projectQuestions" },
    { id: 4, key: "uploads", label: "Media Upload", section: "uploads" },
]);

export const TOTAL_STEPS = WIZARD_STEPS.length;

export function stepForSection(section) {
    const step = WIZARD_STEPS.find((s) => s.section === section);
    return step ? step.id : null;
}

export function sectionForStep(stepId) {
    const step = WIZARD_STEPS.find((s) => s.id === stepId);
    return step ? step.section : null;
}

// Persisted current-step position — a NEW, separate localStorage key from
// LS_DRAFT_KEY (SubmissionPage.jsx). The existing draft blob's shape is never
// touched; this is purely additive so "resume on the same step" works
// without risking the existing draft-restore logic.
export const LS_STEP_KEY = (slug) => `tg_step_${slug}`;

export function readStep(slug) {
    if (typeof window === "undefined") return null;
    try {
        const raw = localStorage.getItem(LS_STEP_KEY(slug));
        const n = raw ? parseInt(raw, 10) : NaN;
        return Number.isInteger(n) && n >= 1 && n <= TOTAL_STEPS ? n : null;
    } catch {
        return null;
    }
}

export function writeStep(slug, stepId) {
    if (typeof window === "undefined") return;
    try {
        localStorage.setItem(LS_STEP_KEY(slug), String(stepId));
    } catch {
        /* best-effort — same tolerance as readDraft/writeDraft */
    }
}

export function clearStep(slug) {
    if (typeof window === "undefined") return;
    try {
        localStorage.removeItem(LS_STEP_KEY(slug));
    } catch {
        /* best-effort */
    }
}
