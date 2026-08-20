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
// Phase 2 (new-talent flow) — reordered so Project Questions and Media come
// FIRST for everyone, with Basic Profile/Skills last. This lets a new
// talent start submitting immediately (Questions -> Media) before any
// personal-info typing, and lets a returning talent's silent recognition
// still land on step 1 (Project Questions) exactly like before the
// reorder — `stepForSection`/`sectionForStep` below are the only things
// any caller should depend on, never a raw numeric literal, so this
// reorder needed no changes outside this file's own step ids.
export const WIZARD_STEPS = Object.freeze([
    { id: 1, key: "projectQuestions", label: "Project Info", section: "projectQuestions" },
    { id: 2, key: "uploads", label: "Media Upload", section: "uploads" },
    { id: 3, key: "profile", label: "Basic Profile", section: "profile" },
    { id: 4, key: "skills", label: "Skills & Attributes", section: "skills" },
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

// Phase 3 (progress-indicator fix) — WHICH step chips WizardProgressBar
// should display, as distinct from WIZARD_STEPS' own fixed technical
// numbering (which currentStep/navigation/validation keep using
// unchanged). A returning talent's actual journey never includes Basic
// Profile/Skills — Identity Confirmation follows Media directly — so a bar
// reading "Step 1 of 4" when only 2 steps will ever be shown is stale/
// misleading. Kept dynamic rather than a one-time snapshot: if a returning
// talent DOES get routed into Profile/Skills because something for this
// specific project is genuinely missing there, `step.id <= currentStep`
// keeps that chip visible the moment it's actually reached, so the bar
// never hides the step the talent is currently standing on.
export function wizardStepsForDisplay({ steps = WIZARD_STEPS, isReturningTalent, currentStep }) {
    if (!isReturningTalent) return steps;
    return steps.filter(
        (step) => step.id <= currentStep || (step.section !== "profile" && step.section !== "skills"),
    );
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

// Persisted "reached the final step's auth card" flag — separate from
// LS_STEP_KEY because the final step's own inner state (plain "Next" button
// vs. the actual Google/OTP auth card) isn't captured by the step NUMBER
// alone. Without this, a talent who reaches the auth card, tries Google,
// and comes back after a failed/abandoned redirect (a full app reload)
// correctly lands back on the same step number but sees the plain "Next"
// button again instead of the auth card they were just using.
export const LS_FINAL_STEP_REACHED_KEY = (slug) => `tg_final_step_reached_${slug}`;

export function readFinalStepReached(slug) {
    if (typeof window === "undefined") return false;
    try {
        return localStorage.getItem(LS_FINAL_STEP_REACHED_KEY(slug)) === "1";
    } catch {
        return false;
    }
}

export function writeFinalStepReached(slug) {
    if (typeof window === "undefined") return;
    try {
        localStorage.setItem(LS_FINAL_STEP_REACHED_KEY(slug), "1");
    } catch {
        /* best-effort — same tolerance as readStep/writeStep */
    }
}

export function clearFinalStepReached(slug) {
    if (typeof window === "undefined") return;
    try {
        localStorage.removeItem(LS_FINAL_STEP_REACHED_KEY(slug));
    } catch {
        /* best-effort */
    }
}
