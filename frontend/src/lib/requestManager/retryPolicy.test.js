import { describe, it, expect } from "vitest";
import { isRetryable, computeBackoffDelay, shouldRetry } from "./retryPolicy";

describe("isRetryable", () => {
    it("retries server_error on idempotent GET", () => {
        expect(isRetryable({ classification: "server_error", status: 500, method: "get" })).toBe(true);
    });

    it("never retries client_error except 408/429", () => {
        expect(isRetryable({ classification: "client_error", status: 400, method: "get" })).toBe(false);
        expect(isRetryable({ classification: "client_error", status: 404, method: "get" })).toBe(false);
        expect(isRetryable({ classification: "client_error", status: 408, method: "get" })).toBe(true);
        expect(isRetryable({ classification: "client_error", status: 429, method: "get" })).toBe(true);
    });

    it("does not retry non-idempotent POST by default", () => {
        expect(isRetryable({ classification: "server_error", status: 500, method: "post" })).toBe(false);
    });

    it("retries POST when explicitly marked idempotent", () => {
        expect(
            isRetryable({ classification: "server_error", status: 500, method: "post", idempotent: true })
        ).toBe(true);
    });

    it("never retries cancelled or aborted", () => {
        expect(isRetryable({ classification: "cancelled", method: "get" })).toBe(false);
        expect(isRetryable({ classification: "aborted", method: "get" })).toBe(false);
    });

    it("retries timeout, network_error, and dns_failure", () => {
        expect(isRetryable({ classification: "timeout", method: "get" })).toBe(true);
        expect(isRetryable({ classification: "network_error", method: "get" })).toBe(true);
        expect(isRetryable({ classification: "dns_failure", method: "get" })).toBe(true);
    });
});

describe("computeBackoffDelay", () => {
    it("grows exponentially and respects the max delay cap", () => {
        const policy = { baseDelayMs: 100, factor: 2, maxDelayMs: 1000, jitterRatio: 0 };
        expect(computeBackoffDelay(1, policy)).toBe(100);
        expect(computeBackoffDelay(2, policy)).toBe(200);
        expect(computeBackoffDelay(3, policy)).toBe(400);
        expect(computeBackoffDelay(10, policy)).toBe(1000); // capped
    });

    it("stays within jitter bounds and never goes negative", () => {
        const policy = { baseDelayMs: 100, factor: 2, maxDelayMs: 1000, jitterRatio: 0.2 };
        for (let i = 0; i < 50; i += 1) {
            const delay = computeBackoffDelay(1, policy);
            expect(delay).toBeGreaterThanOrEqual(0);
            expect(delay).toBeLessThanOrEqual(120);
        }
    });
});

describe("shouldRetry", () => {
    it("allows retries under maxAttempts", () => {
        expect(shouldRetry(1, { maxAttempts: 3 })).toBe(true);
        expect(shouldRetry(2, { maxAttempts: 3 })).toBe(true);
    });

    it("stops at maxAttempts", () => {
        expect(shouldRetry(3, { maxAttempts: 3 })).toBe(false);
    });
});
