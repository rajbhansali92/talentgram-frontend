import { describe, it, expect } from "vitest";
import { buildRecognizedIdentity, shouldAttemptSilentRecognition, classifyPortalLookupResult, tokenAuthenticatesEmail } from "./returningTalent";

describe("buildRecognizedIdentity", () => {
    it("returns null when there is no prefill data", () => {
        expect(buildRecognizedIdentity(null)).toBeNull();
        expect(buildRecognizedIdentity(undefined)).toBeNull();
    });

    it("joins first_name/last_name into a single display name (the /public/prefill shape, not /portal/profile's `name`)", () => {
        const identity = buildRecognizedIdentity({ first_name: "Priya", last_name: "Shah" });
        expect(identity.name).toBe("Priya Shah");
    });

    it("falls back to 'there' when both name parts are empty", () => {
        const identity = buildRecognizedIdentity({ first_name: "", last_name: "" });
        expect(identity.name).toBe("there");
    });

    it("carries location and height through, defaulting to empty/blank when absent", () => {
        const withData = buildRecognizedIdentity({
            first_name: "Priya",
            last_name: "Shah",
            location: [{ city: "Mumbai" }],
            height: "5'6\"",
        });
        expect(withData.location).toEqual([{ city: "Mumbai" }]);
        expect(withData.height).toBe("5'6\"");

        const withoutData = buildRecognizedIdentity({ first_name: "Priya", last_name: "Shah" });
        expect(withoutData.location).toEqual([]);
        expect(withoutData.height).toBe("");
    });

    it("does not read `name` or `image_url` — /public/prefill never returns those fields", () => {
        const identity = buildRecognizedIdentity({ name: "Should Not Be Used", image_url: "http://x", first_name: "", last_name: "" });
        expect(identity.name).toBe("there");
        expect(identity.image_url).toBeUndefined();
    });
});

describe("shouldAttemptSilentRecognition", () => {
    it("is false in Admin Mode regardless of token/email", () => {
        expect(shouldAttemptSilentRecognition({ adminMode: true, emailGateUnlocked: false, token: "t", email: "e@x.com" })).toBe(false);
    });

    it("is false once the gate is already unlocked", () => {
        expect(shouldAttemptSilentRecognition({ adminMode: false, emailGateUnlocked: true, token: "t", email: "e@x.com" })).toBe(false);
    });

    it("is false when the token is missing", () => {
        expect(shouldAttemptSilentRecognition({ adminMode: false, emailGateUnlocked: false, token: null, email: "e@x.com" })).toBe(false);
    });

    it("is false when the paired email is missing (even with a token present)", () => {
        expect(shouldAttemptSilentRecognition({ adminMode: false, emailGateUnlocked: false, token: "t", email: null })).toBe(false);
    });

    it("is true only when not admin, gate is locked, and both token and email are present", () => {
        expect(shouldAttemptSilentRecognition({ adminMode: false, emailGateUnlocked: false, token: "t", email: "e@x.com" })).toBe(true);
    });
});

describe("tokenAuthenticatesEmail (UX-polish fix — trusted-session Step A typed-email lookup)", () => {
    it("passes when there is no candidate email to check (e.g. the UPLOAD TEST CTA, which has none)", () => {
        expect(tokenAuthenticatesEmail({ tokenEmail: "raj@x.com", candidateEmail: null })).toBe(true);
        expect(tokenAuthenticatesEmail({ tokenEmail: "raj@x.com", candidateEmail: "" })).toBe(true);
    });

    it("passes when there is no stored token email to conflict with", () => {
        expect(tokenAuthenticatesEmail({ tokenEmail: null, candidateEmail: "raj@x.com" })).toBe(true);
    });

    it("passes when the candidate email matches the token's own email, case- and whitespace-insensitively", () => {
        expect(tokenAuthenticatesEmail({ tokenEmail: "Raj@X.com", candidateEmail: " raj@x.com " })).toBe(true);
    });

    it("fails when the candidate email belongs to a different account than the stored token — never let a token vouch for someone else's address", () => {
        expect(tokenAuthenticatesEmail({ tokenEmail: "raj@x.com", candidateEmail: "someone-else@x.com" })).toBe(false);
    });
});

describe("classifyPortalLookupResult (Phase 2 — new-talent no-OTP unlock)", () => {
    it("classifies a matched talent as 'known' — must still require OTP, never unlock directly", () => {
        expect(classifyPortalLookupResult({ exists: true, talent: { name: "Priya", email: "p@x.com" } })).toBe("known");
    });

    it("classifies exists:true with no talent payload as 'unknown', not 'known' — never trust a malformed positive match", () => {
        expect(classifyPortalLookupResult({ exists: true })).toBe("unknown");
    });

    it("classifies a confirmed no-match as 'new' — the only classification allowed to skip OTP", () => {
        expect(classifyPortalLookupResult({ exists: false })).toBe("new");
    });

    it("classifies missing/malformed data as 'unknown' — network errors and unexpected shapes must never be treated as 'new'", () => {
        expect(classifyPortalLookupResult(null)).toBe("unknown");
        expect(classifyPortalLookupResult(undefined)).toBe("unknown");
        expect(classifyPortalLookupResult({})).toBe("unknown");
    });
});
