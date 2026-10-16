import { describe, it, expect } from "vitest";
import { formatErrorDetail, isCancelOrTimeoutError, INTERRUPTED_REQUEST_MESSAGE } from "./errorFormatter";

// Incident 2026-10-14: a Request Manager timeout aborts via AbortController, the
// axios shim rethrows the raw CanceledError, and the toast read "canceled".
function canceledError(extra = {}) {
    const e = new Error("canceled");
    e.name = "CanceledError";
    e.code = "ERR_CANCELED";
    return Object.assign(e, extra);
}

describe("formatErrorDetail — aborted / timed-out requests", () => {
    it("never shows the raw word 'canceled'", () => {
        expect(formatErrorDetail(canceledError(), "fallback")).toBe(INTERRUPTED_REQUEST_MESSAGE);
        expect(formatErrorDetail(canceledError(), "fallback")).not.toMatch(/^canceled$/i);
    });

    it("treats a Request Manager timeout (CanceledError + classification) as interrupted", () => {
        const e = canceledError({ classification: "timeout", requestId: "r1" });
        expect(isCancelOrTimeoutError(e)).toBe(true);
        expect(formatErrorDetail(e)).toBe(INTERRUPTED_REQUEST_MESSAGE);
    });

    it("treats axios' own timeout (ECONNABORTED) as interrupted, not 'timeout of 35000ms exceeded'", () => {
        const e = Object.assign(new Error("timeout of 35000ms exceeded"), { code: "ECONNABORTED" });
        expect(formatErrorDetail(e)).toBe(INTERRUPTED_REQUEST_MESSAGE);
    });

    it("treats a bare AbortError (fetch / iOS Safari) as interrupted", () => {
        const e = Object.assign(new Error("The operation was aborted."), { name: "AbortError" });
        expect(formatErrorDetail(e)).toBe(INTERRUPTED_REQUEST_MESSAGE);
    });

    it("recognises the cancel message even when only the text survives", () => {
        expect(isCancelOrTimeoutError(new Error("canceled"))).toBe(true);
    });
});

describe("formatErrorDetail — genuine errors are unchanged", () => {
    it("shows the server's string detail", () => {
        const e = { response: { status: 400, data: { detail: "Height is required" } }, message: "Request failed" };
        expect(formatErrorDetail(e)).toBe("Height is required");
    });

    it("a server response is never mistaken for a cancel, even with a cancel-ish message", () => {
        const e = canceledError({ response: { status: 500, data: { detail: "Boom" } } });
        expect(isCancelOrTimeoutError(e)).toBe(false);
        expect(formatErrorDetail(e)).toBe("Boom");
    });

    it("keeps the existing fallback for a plain network error and for no error", () => {
        expect(formatErrorDetail({ message: "Network Error" })).toBe("Network Error");
        expect(formatErrorDetail(null, "fallback")).toBe("fallback");
        expect(isCancelOrTimeoutError(null)).toBe(false);
    });

    it("formats FastAPI validation arrays as before", () => {
        const e = { response: { data: { detail: [{ loc: ["body", "email"], msg: "bad" }] } } };
        expect(formatErrorDetail(e)).toContain("Email — bad");
    });
});
