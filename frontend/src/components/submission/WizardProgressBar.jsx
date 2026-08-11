import React from "react";
import { Check } from "lucide-react";
import { SECTION_STATUS } from "@/lib/readinessStatus";

// Pure presentation layer, same contract as SubmissionReadinessPanel: every
// step it renders already carries its final resolved status — this
// component makes no decisions about what's required or satisfied. `steps`
// is lib/wizardSteps.js's WIZARD_STEPS; `stepStatusById` is a plain
// {stepId: SECTION_STATUS.*} map the host page derives from
// useSubmissionExperienceModel's `sectionStatus` — this file never imports
// or re-derives that logic itself, only renders the enum values it's given.
//
// One component renders both breakpoints (dots+labels on md:, a compact
// "Step N of 4" line + thin fill bar below md:) rather than two separate
// implementations, so they can never drift out of sync with each other.
export default function WizardProgressBar({
    steps,
    currentStep,
    stepStatusById = {},
    onStepClick,
    sticky = true,
}) {
    const currentDef = steps.find((s) => s.id === currentStep);
    // A step with no required items (SECTION_STATUS.OPTIONAL) never blocks
    // advancing past it, so it's jumpable — but only an actually-COMPLETE
    // step earns the visual checkmark; OPTIONAL just renders as upcoming.
    const isStepDone = (step) => stepStatusById[step.id] === SECTION_STATUS.COMPLETE;
    const canJumpTo = (step) =>
        step.id < currentStep || isStepDone(step) || stepStatusById[step.id] === SECTION_STATUS.OPTIONAL;

    return (
        <div
            // `sticky=false` in Admin Mode — the page already has its own
            // sticky `sticky top-0 z-50` AdminModeBanner; two sibling
            // `sticky top-0` bars fight for the same position on scroll, so
            // Admin Mode renders this in-flow instead (it also has its own
            // pinned readiness dashboard serving the "always visible
            // progress" role there).
            className={`${sticky ? "sticky top-0 z-40" : ""} bg-white/95 backdrop-blur-md border-b border-[#eaeaea]`}
            data-testid="wizard-progress-bar"
        >
            {/* Screen-reader announcement — CSS-hiding an inactive step already
                removes it from the tab order for free, but nothing else today
                announces the step change itself. */}
            <p className="sr-only" aria-live="polite" data-testid="wizard-step-announce">
                {currentDef ? `Step ${currentStep} of ${steps.length}: ${currentDef.label}` : ""}
            </p>

            {/* Desktop / tablet — full step row with labels */}
            <div className="hidden md:flex items-center justify-center gap-2 px-6 py-4 max-w-3xl mx-auto">
                {steps.map((step, idx) => {
                    const isCurrent = step.id === currentStep;
                    const isComplete = isStepDone(step) && !isCurrent;
                    const jumpable = canJumpTo(step);
                    return (
                        <React.Fragment key={step.id}>
                            <button
                                type="button"
                                disabled={!jumpable}
                                onClick={() => jumpable && onStepClick?.(step.id)}
                                data-testid={`wizard-step-chip-${step.id}`}
                                data-status={isCurrent ? "current" : isComplete ? "complete" : "upcoming"}
                                className={`flex items-center gap-2 shrink-0 ${jumpable ? "cursor-pointer" : "cursor-default"}`}
                            >
                                <span
                                    className={`w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-mono font-semibold shrink-0 transition-colors duration-200 ${
                                        isCurrent
                                            ? "bg-[#0c2340] text-white shadow-sm"
                                            : isComplete
                                                ? "bg-emerald-500 text-white"
                                                : "bg-white border-2 border-[#d4d4d4] text-[#999]"
                                    }`}
                                >
                                    {isComplete ? <Check className="w-3.5 h-3.5 stroke-[3]" /> : step.id}
                                </span>
                                <span
                                    className={`text-[11px] font-mono font-semibold uppercase tracking-wide whitespace-nowrap ${
                                        isCurrent ? "text-[#0c2340]" : isComplete ? "text-emerald-700" : "text-[#999]"
                                    }`}
                                >
                                    {step.label}
                                </span>
                            </button>
                            {idx < steps.length - 1 && (
                                <span
                                    className={`h-px w-8 shrink-0 transition-colors duration-200 ${
                                        step.id < currentStep || isComplete ? "bg-emerald-400" : "bg-[#e5e5e5]"
                                    }`}
                                />
                            )}
                        </React.Fragment>
                    );
                })}
            </div>

            {/* Mobile — simple step dots (checkmark/number), no percentage.
                Same jumpability rules as the desktop chips above, just
                condensed to dots + connecting lines with the current step's
                label shown alongside. */}
            <div className="md:hidden px-4 pt-2.5 pb-2.5">
                <div className="flex items-center justify-between mb-2">
                    <span className="text-[12px] font-mono font-semibold uppercase tracking-wide text-[#0c2340]" data-testid="wizard-step-label-mobile">
                        {currentDef?.label}
                    </span>
                    <span className="text-[10px] font-mono text-[#999]">Step {currentStep} of {steps.length}</span>
                </div>
                <div className="flex items-center gap-1.5" data-testid="wizard-stepper-mobile">
                    {steps.map((step, idx) => {
                        const isCurrent = step.id === currentStep;
                        const isComplete = isStepDone(step) && !isCurrent;
                        const jumpable = canJumpTo(step);
                        return (
                            <React.Fragment key={step.id}>
                                <button
                                    type="button"
                                    disabled={!jumpable}
                                    onClick={() => jumpable && onStepClick?.(step.id)}
                                    data-testid={`wizard-step-dot-${step.id}`}
                                    data-status={isCurrent ? "current" : isComplete ? "complete" : "upcoming"}
                                    aria-label={`${step.label}${isComplete ? " (complete)" : isCurrent ? " (current)" : ""}`}
                                    className={`w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-mono font-semibold shrink-0 transition-colors duration-200 ${
                                        isCurrent
                                            ? "bg-[#0c2340] text-white shadow-sm"
                                            : isComplete
                                                ? "bg-emerald-500 text-white"
                                                : "bg-white border-2 border-[#d4d4d4] text-[#999]"
                                    }`}
                                >
                                    {isComplete ? <Check className="w-3.5 h-3.5 stroke-[3]" /> : step.id}
                                </button>
                                {idx < steps.length - 1 && (
                                    <span
                                        className={`h-px flex-1 transition-colors duration-200 ${
                                            step.id < currentStep || isComplete ? "bg-emerald-400" : "bg-[#e5e5e5]"
                                        }`}
                                    />
                                )}
                            </React.Fragment>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}
