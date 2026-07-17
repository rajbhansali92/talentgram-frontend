import { describe, it, expect, vi, afterEach } from "vitest";
import { CircuitBreaker, CircuitState, defaultCircuitKey } from "./circuitBreaker";

describe("CircuitBreaker", () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it("starts closed and allows requests", () => {
        const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000 });
        expect(cb.canRequest("k")).toBe(true);
        expect(cb.getState("k")).toBe(CircuitState.CLOSED);
    });

    it("opens after consecutive failures reach the threshold", () => {
        const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000 });
        cb.recordFailure("k");
        cb.recordFailure("k");
        expect(cb.getState("k")).toBe(CircuitState.CLOSED);
        cb.recordFailure("k");
        expect(cb.getState("k")).toBe(CircuitState.OPEN);
        expect(cb.canRequest("k")).toBe(false);
    });

    it("resets failure count on success", () => {
        const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000 });
        cb.recordFailure("k");
        cb.recordFailure("k");
        cb.recordSuccess("k");
        cb.recordFailure("k");
        cb.recordFailure("k");
        expect(cb.getState("k")).toBe(CircuitState.CLOSED);
    });

    it("transitions to half_open after the cooldown and back to closed on success", () => {
        vi.useFakeTimers();
        const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
        cb.recordFailure("k");
        expect(cb.getState("k")).toBe(CircuitState.OPEN);
        expect(cb.canRequest("k")).toBe(false);

        vi.advanceTimersByTime(1001);
        expect(cb.canRequest("k")).toBe(true);
        expect(cb.getState("k")).toBe(CircuitState.HALF_OPEN);

        cb.recordSuccess("k");
        expect(cb.getState("k")).toBe(CircuitState.CLOSED);
    });

    it("re-opens if the half_open trial also fails", () => {
        vi.useFakeTimers();
        const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
        cb.recordFailure("k");
        vi.advanceTimersByTime(1001);
        cb.canRequest("k"); // transitions to half_open
        cb.recordFailure("k");
        expect(cb.getState("k")).toBe(CircuitState.OPEN);
    });

    it("tracks independent keys separately", () => {
        const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
        cb.recordFailure("a");
        expect(cb.getState("a")).toBe(CircuitState.OPEN);
        expect(cb.getState("b")).toBe(CircuitState.CLOSED);
    });

    describe("HALF_OPEN probe behavior", () => {
        it("grants exactly one probe: a concurrent canRequest() while half_open fails fast", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
            cb.recordFailure("k");
            vi.advanceTimersByTime(1001);

            // First caller to check after cooldown wins the single probe.
            expect(cb.canRequest("k")).toBe(true);
            expect(cb.getState("k")).toBe(CircuitState.HALF_OPEN);

            // Every other concurrent caller, arriving while the probe is
            // still outstanding (no recordSuccess/recordFailure yet), must
            // fail fast rather than dispatch a second probe.
            expect(cb.canRequest("k")).toBe(false);
            expect(cb.canRequest("k")).toBe(false);
            expect(cb.getState("k")).toBe(CircuitState.HALF_OPEN);
        });

        it("a successful probe closes the breaker for subsequent callers", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
            cb.recordFailure("k");
            vi.advanceTimersByTime(1001);

            expect(cb.canRequest("k")).toBe(true); // wins the probe
            cb.recordSuccess("k");

            expect(cb.getState("k")).toBe(CircuitState.CLOSED);
            expect(cb.canRequest("k")).toBe(true); // normal traffic resumes
        });

        it("a failed probe reopens the breaker with a fresh cooldown", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
            cb.recordFailure("k");
            vi.advanceTimersByTime(1001);

            expect(cb.canRequest("k")).toBe(true); // wins the probe
            cb.recordFailure("k");

            expect(cb.getState("k")).toBe(CircuitState.OPEN);
            expect(cb.canRequest("k")).toBe(false); // still cooling down
            vi.advanceTimersByTime(1001);
            expect(cb.canRequest("k")).toBe(true); // fresh cooldown elapsed -> new probe
        });

        it("a cancelled probe reopens the breaker instead of deadlocking in half_open", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 1, cooldownMs: 1000 });
            cb.recordFailure("k");
            vi.advanceTimersByTime(1001);

            expect(cb.canRequest("k")).toBe(true); // wins the probe
            cb.recordCancelled("k");

            expect(cb.getState("k")).toBe(CircuitState.OPEN);
            expect(cb.canRequest("k")).toBe(false); // cooling down again, not stuck forever
            vi.advanceTimersByTime(1001);
            expect(cb.canRequest("k")).toBe(true);
        });

        it("recordCancelled on a healthy (closed) circuit is a no-op", () => {
            const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000 });
            cb.recordCancelled("k");
            expect(cb.getState("k")).toBe(CircuitState.CLOSED);
            expect(cb.canRequest("k")).toBe(true);
        });
    });

    describe("cleanup / eviction", () => {
        it("evicts a circuit that has been idle past entryTtlMs when a new key is created", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000, entryTtlMs: 5000 });
            cb.recordFailure("stale"); // creates + touches the "stale" entry
            expect(cb.size).toBe(1);

            vi.advanceTimersByTime(5001);
            cb.canRequest("fresh"); // triggers the amortized sweep on new-key creation

            expect(cb.size).toBe(1); // "stale" evicted, "fresh" remains
            expect(cb.getState("stale")).toBe(CircuitState.CLOSED); // re-created from scratch
        });

        it("does not evict a circuit that has been accessed within the TTL", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000, entryTtlMs: 5000 });
            cb.recordFailure("active");

            vi.advanceTimersByTime(3000);
            cb.canRequest("active"); // touch -> refreshes lastAccessedAt
            vi.advanceTimersByTime(3000); // 6000ms since creation, but only 3000ms since last touch

            cb.canRequest("other"); // triggers a sweep
            expect(cb.size).toBe(2); // "active" survives because it was touched recently
        });

        it("bounds Map growth for a churning set of keys instead of growing forever", () => {
            vi.useFakeTimers();
            const cb = new CircuitBreaker({ failureThreshold: 3, cooldownMs: 1000, entryTtlMs: 1000 });
            for (let i = 0; i < 50; i += 1) {
                cb.recordFailure(`key-${i}`);
                vi.advanceTimersByTime(100);
            }
            // Every key older than the 1000ms TTL should have been swept as
            // newer keys were created — the map never accumulates all 50.
            expect(cb.size).toBeLessThan(50);
        });
    });
});

describe("defaultCircuitKey", () => {
    it("groups requests by method + first path segment, not the full path", () => {
        expect(defaultCircuitKey({ method: "GET", url: "https://x.test/videos/64f2a9/comments" })).toBe(
            "get:videos"
        );
    });

    it("ignores query params", () => {
        expect(defaultCircuitKey({ method: "GET", url: "https://x.test/videos?sort=recent" })).toBe("get:videos");
    });

    it("groups two different resource ids under the same service into one key", () => {
        const a = defaultCircuitKey({ method: "get", url: "https://x.test/videos/64f2a9/comments" });
        const b = defaultCircuitKey({ method: "get", url: "https://x.test/videos/71b3c0/thumbnail" });
        expect(a).toBe(b);
    });

    it("keeps different services on independent keys", () => {
        const videos = defaultCircuitKey({ method: "get", url: "https://x.test/videos/1" });
        const talent = defaultCircuitKey({ method: "get", url: "https://x.test/talent/1" });
        expect(videos).not.toBe(talent);
    });

    it("keeps different methods on independent keys for the same service", () => {
        const get = defaultCircuitKey({ method: "get", url: "https://x.test/videos/1" });
        const post = defaultCircuitKey({ method: "post", url: "https://x.test/videos/1" });
        expect(get).not.toBe(post);
    });

    it("respects a custom grouping depth", () => {
        expect(
            defaultCircuitKey({ method: "get", url: "https://x.test/api/videos/1" }, { depth: 2 })
        ).toBe("get:api/videos");
    });

    it("falls back to the raw url when it can't be parsed", () => {
        expect(defaultCircuitKey({ method: "post", url: "/relative/path" })).toBe("post:relative");
    });
});
