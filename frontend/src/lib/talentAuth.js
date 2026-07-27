/**
 * Shared talent-facing OTP/Google auth mechanics — extracted from the three
 * near-identical implementations previously inlined in SubmissionPage.jsx,
 * ApplicationPage.jsx, and PortalGateway.jsx (see
 * docs/TALENT_MIGRATION_PLAN.md Phase 1 item 3).
 *
 * Deliberately NOT a stateful hook: each caller owns different local state
 * (otpSent/otpCode/loading flags differ per UI) and different post-auth
 * business logic (resuming a submission vs. an application vs. entering the
 * dashboard). This module only extracts the actual duplicated mechanics —
 * the raw network calls, the Google OAuth URL construction, and the portal
 * token persistence — every caller keeps its own state, error copy, and
 * (per the Phase 1 item 3 scope) all routing decisions.
 */
import { api as axios, PORTAL_TOKEN_KEY } from "@/lib/api";

/** POST /auth/otp/send. Callers keep their own loading state and error toast copy. */
export function sendOtp(email) {
    return axios.post("/auth/otp/send", { email });
}

/** POST /auth/otp/verify. Returns the raw response data — every caller
 * interprets it differently (submission resume vs. application resume vs.
 * portal profile), so no shared post-processing happens here. */
export async function verifyOtp({ email, otp, slug }) {
    const { data } = await axios.post("/auth/otp/verify", { email, otp, slug });
    return data;
}

/** Builds the Google OAuth authorize URL. Pure — does not navigate. Callers
 * decide when/whether to assign it to window.location.href, keeping the
 * actual navigation (and any routing decision around it) in the caller. */
export function buildGoogleAuthUrl(state) {
    const clientId = process.env.REACT_APP_GOOGLE_CLIENT_ID || "339414275037-rrm7uugj1t4gq2b02q9r51d9l6m39vbe.apps.googleusercontent.com";
    const redirectUri = `${window.location.origin}/google-callback`;
    const scope = "openid profile email";
    return `https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=${clientId}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scope)}&state=${encodeURIComponent(state)}`;
}

/** Persists a portal session token returned by either /auth/otp/verify or
 * /auth/google, if present. Mirrors the identical inline pattern previously
 * repeated in all three OTP verify handlers. */
export function persistPortalToken(data) {
    if (data?.portal_token) {
        localStorage.setItem(PORTAL_TOKEN_KEY, data.portal_token);
    }
}
