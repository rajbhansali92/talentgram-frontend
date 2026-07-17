import { describe, it, expect, vi, afterEach } from "vitest";
import { classifyError, ErrorClassification } from "./errorClassifier";

describe("classifyError", () => {
    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it("classifies explicit cancellation via reason", () => {
        expect(classifyError(new Error("boom"), { reason: "cancelled" }).classification).toBe(
            ErrorClassification.CANCELLED
        );
    });

    it("classifies circuit_open via reason", () => {
        expect(classifyError(null, { reason: "circuit_open" }).classification).toBe(
            ErrorClassification.CIRCUIT_OPEN
        );
    });

    it("classifies offline when navigator.onLine is false", () => {
        expect(classifyError(new Error("boom"), { offline: true }).classification).toBe(
            ErrorClassification.OFFLINE
        );
    });

    it("classifies timeout when timedOut flag is set", () => {
        expect(classifyError(new Error("boom"), { timedOut: true, offline: false }).classification).toBe(
            ErrorClassification.TIMEOUT
        );
    });

    it("classifies AbortError as aborted", () => {
        const err = Object.assign(new Error("aborted"), { name: "AbortError" });
        expect(classifyError(err, { offline: false }).classification).toBe(ErrorClassification.ABORTED);
    });

    it("classifies SyntaxError as malformed_response", () => {
        const err = new SyntaxError("Unexpected token < in JSON");
        expect(classifyError(err, { offline: false }).classification).toBe(
            ErrorClassification.MALFORMED_RESPONSE
        );
    });

    it("classifies 5xx as server_error with status", () => {
        const err = { response: { status: 503 } };
        const result = classifyError(err, { offline: false });
        expect(result.classification).toBe(ErrorClassification.SERVER_ERROR);
        expect(result.status).toBe(503);
    });

    it("classifies 4xx as client_error with status", () => {
        const err = { response: { status: 404 } };
        const result = classifyError(err, { offline: false });
        expect(result.classification).toBe(ErrorClassification.CLIENT_ERROR);
        expect(result.status).toBe(404);
    });

    it("classifies DNS failure codes", () => {
        const err = { code: "ENOTFOUND" };
        expect(classifyError(err, { offline: false }).classification).toBe(ErrorClassification.DNS_FAILURE);
    });

    it("classifies connection-reset/refused as network_error", () => {
        expect(classifyError({ code: "ECONNREFUSED" }, { offline: false }).classification).toBe(
            ErrorClassification.NETWORK_ERROR
        );
        expect(classifyError({ code: "ECONNRESET" }, { offline: false }).classification).toBe(
            ErrorClassification.NETWORK_ERROR
        );
    });

    it("falls back to network_error for unrecognized failures", () => {
        expect(classifyError(new Error("mystery"), { offline: false }).classification).toBe(
            ErrorClassification.NETWORK_ERROR
        );
    });
});
