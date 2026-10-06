import { describe, it, expect } from "vitest";
import { isEmailVerificationRequired } from "./talentAuth";

const err = (status, detail) => ({ response: { status, data: detail === undefined ? {} : { detail } } });

// SubmissionPage turns this answer into "send a one-time code". It must be ONLY the backend's
// "prove you own this email" 403 — never another 403, and never any other status.
describe("isEmailVerificationRequired", () => {
    it("is true for the backend's verify-your-email 403", () => {
        expect(isEmailVerificationRequired(err(403, "Please verify your email to continue. We'll send you a one-time code."))).toBe(true);
        expect(isEmailVerificationRequired(err(403, "please VERIFY YOUR EMAIL"))).toBe(true);
    });

    it("is false for any other 403 (inactive link, permissions, ...)", () => {
        expect(isEmailVerificationRequired(err(403, "This link is no longer active"))).toBe(false);
        expect(isEmailVerificationRequired(err(403, "Forbidden"))).toBe(false);
        expect(isEmailVerificationRequired(err(403))).toBe(false);
        expect(isEmailVerificationRequired(err(403, ["verify your email"]))).toBe(false);
    });

    it("is false for other statuses even with matching text, and for non-HTTP errors", () => {
        for (const s of [400, 401, 404, 429, 500]) {
            expect(isEmailVerificationRequired(err(s, "Please verify your email"))).toBe(false);
        }
        expect(isEmailVerificationRequired(new Error("Network Error"))).toBe(false);
        expect(isEmailVerificationRequired(undefined)).toBe(false);
        expect(isEmailVerificationRequired(null)).toBe(false);
    });
});
