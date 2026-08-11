import React, { useRef } from "react";
import { ArrowLeft, ArrowRight, Loader2 } from "lucide-react";
import { useStickyFooterHeightVar } from "@/hooks/useStickyFooterHeightVar";

// Sticky Back/Next footer for wizard Steps 1-3. Step 4 keeps its own
// existing Submit footer (SubmissionPage.jsx's `stickyFooterRef`,
// `--tg-sticky-cta-h`) untouched — this is a separate footer with its own
// CSS var (`--tg-wizard-nav-h`) so the two never collide; only one is ever
// visible at a time (this one on steps 1-3, the Submit footer on step 4),
// and FloatingUploadManager sums both vars to always clear whichever is
// actually rendered.
//
// Pure renderer: `nextDisabled`/`nextLabel` are handed in already resolved
// by the host page's per-step filter of the Submission Experience Model's
// `checklist` — this component makes no readiness decisions of its own,
// same contract as SubmissionReadinessPanel and WizardProgressBar.
export default function WizardStepNav({
    showBack,
    onBack,
    onNext,
    nextDisabled,
    nextBusy,
    nextLabel = "Next",
}) {
    const navRef = useRef(null);
    useStickyFooterHeightVar(navRef, "--tg-wizard-nav-h");

    return (
        <div
            ref={navRef}
            data-sticky-footer
            data-testid="wizard-step-nav"
            className="sticky bottom-0 z-30 bg-gradient-to-t from-white via-white/95 to-transparent pt-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))] pb-safe"
        >
            <div className="flex items-center gap-3 max-w-md mx-auto">
                {showBack && (
                    <button
                        type="button"
                        onClick={onBack}
                        data-testid="wizard-back-btn"
                        className="shrink-0 border border-[#eaeaea] hover:border-[#d4d4d4] text-[#333333] rounded-full inline-flex items-center justify-center gap-2 min-h-[52px] px-5 text-[13px] font-medium active:scale-[0.97] transition-all duration-200 bg-white/80"
                        style={{ WebkitTapHighlightColor: "transparent" }}
                    >
                        <ArrowLeft className="w-4 h-4" /> Back
                    </button>
                )}
                <button
                    type="button"
                    onClick={onNext}
                    disabled={nextDisabled}
                    data-testid="wizard-next-btn"
                    className="flex-1 bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                    style={{ WebkitTapHighlightColor: "transparent" }}
                >
                    {nextBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                    {nextLabel}
                    {!nextBusy && <ArrowRight className="w-4 h-4" />}
                </button>
            </div>
        </div>
    );
}
