// Pure helpers for the "what's this upload for?" destination pre-choice
// (SubmissionPage.jsx's MediaDestinationToggle / mediaDestination state).
// Extracted so the resolution logic has a regression test independent of
// the surrounding component — see requirementEngine.js/readinessStatus.js
// for the same pattern elsewhere in this app.

// `mediaDestination` is keyed by the two independent decisions a talent
// can make in one sitting: `intro_video` and `images` (one choice covers
// whichever image sub-category — generic/Indian/Western — they upload
// into). Audition takes are never reusable outside a project by design and
// never appear in `pendingMediaConsent` at all, so they're not represented
// here.
export function destinationForCategory(mediaDestination, category) {
    return category === "intro_video" ? mediaDestination.intro_video : mediaDestination.images;
}

// Splits a submission's pending-consent items into those whose destination
// is already known (silently auto-resolved, never shown to the talent) and
// those that genuinely still need to ask (the legacy consent dialog's only
// remaining audience — e.g. a draft resumed from before this feature
// existed, where no destination was ever chosen). This is the single
// source of truth both the auto-resolve effect and the dialog's own render
// condition read from in SubmissionPage.jsx, so the two can never disagree
// about what's actually still awaiting a choice — which is what closes the
// race that used to let the dialog flash into view after every upload
// regardless of whether a destination had already been chosen.
export function splitPendingConsentByKnownDestination(pendingMediaConsent, mediaDestination) {
    const known = [];
    const awaitingChoice = [];
    for (const item of pendingMediaConsent) {
        if (destinationForCategory(mediaDestination, item.category)) {
            known.push(item);
        } else {
            awaitingChoice.push(item);
        }
    }
    return { known, awaitingChoice };
}

// Groups a set of pending items by the backend decision string each one
// should resolve to. "library" maps to "update_profile" (the talent's
// reusable Media Library/Dashboard); anything else — including the default
// "project" — maps to "only_this_project". Returns { decision: [ids] },
// ready to feed one POST /media-consent call per decision.
export function groupByDestinationDecision(items, mediaDestination) {
    return items.reduce((acc, item) => {
        const decision = destinationForCategory(mediaDestination, item.category) === "library"
            ? "update_profile"
            : "only_this_project";
        (acc[decision] = acc[decision] || []).push(item.id);
        return acc;
    }, {});
}
