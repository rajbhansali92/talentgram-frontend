// Pure helpers for the returning-talent "UPLOAD TEST" silent-recognition
// path (SubmissionPage.jsx's handleUploadTestClick). Extracted so the shape
// normalization has a regression test independent of the surrounding
// 5900-line component — see requirementEngine.js/readinessStatus.js for the
// same pattern elsewhere in this app.

// `/public/prefill` (the endpoint this recognition path calls) splits the
// canonical talent's single `name` field into `first_name`/`last_name`
// server-side — this rebuilds a single display name from that split, plus
// the lightweight location/height fields the Identity Confirmation card
// shows. Deliberately does NOT read `data.name` or `data.image_url` — the
// prefill response never contains those (that shape belongs to
// /portal/profile and /portal/lookup, different endpoints), so reading them
// here would silently produce "undefined" instead of a real name.
export function buildRecognizedIdentity(prefillData) {
    if (!prefillData) return null;
    const name = `${prefillData.first_name || ""} ${prefillData.last_name || ""}`.trim() || "there";
    return {
        name,
        location: prefillData.location || [],
        height: prefillData.height || "",
    };
}

// Whether the "UPLOAD TEST" CTA should even attempt silent recognition
// rather than just scrolling to the existing Step A UI. A pure decision
// over already-known inputs — no network/localStorage access here, so the
// caller (which does read localStorage/adminMode) stays a thin wrapper.
export function shouldAttemptSilentRecognition({ adminMode, emailGateUnlocked, token, email }) {
    if (adminMode) return false;
    if (emailGateUnlocked) return false;
    return !!token && !!email;
}

// UX-polish fix — a portal token only ever proves ownership of the ONE
// email it was minted for. Before letting it vouch for a candidate email
// (e.g. one the talent just typed into Step A's "Continue with Email"
// field, matching an existing talent), the two must agree — otherwise a
// token left over from a different account on a shared/previous device
// could silently authenticate an unrelated address. This is a client-side
// UX guard, not the real security boundary — /public/prefill's own
// verify_email_ownership check (server-side, against the token's embedded
// email claim) is what actually enforces this; this just avoids a wasted
// round-trip and an unnecessary "wrong person" recognition attempt.
// No candidateEmail (the "UPLOAD TEST" CTA has none to compare against —
// it just uses whatever email the token itself is paired with) or no
// stored tokenEmail (nothing to conflict with) both pass through.
export function tokenAuthenticatesEmail({ tokenEmail, candidateEmail }) {
    if (!candidateEmail || !tokenEmail) return true;
    return candidateEmail.trim().toLowerCase() === tokenEmail.trim().toLowerCase();
}

// Phase 2 (new-talent flow) — classifies a `POST /portal/lookup` response
// into exactly what handleInlineLookup needs to decide: is this a KNOWN
// talent (show "Is this you?", OTP still required), a CONFIRMED new one
// (unlock immediately, zero OTP friction — the entire point of deferring
// auth to the end of the new-talent flow), or UNKNOWN (the request itself
// failed — never skip OTP on ambiguous/error data, only on a positive
// "not found" answer from the backend).
//
// Security note: "known" only ever gates a UI reveal, never an unlock —
// the caller must still require OTP before trusting it. Only "new" is
// allowed to skip authentication, and only because there is nothing yet to
// prove ownership of.
export function classifyPortalLookupResult(data) {
    if (data?.exists === true && data.talent) return "known";
    if (data && data.exists === false) return "new";
    return "unknown";
}
