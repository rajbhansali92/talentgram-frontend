import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { RequestManager } from "./RequestManager";

// Phase 8 — production validation / chaos engineering. These tests exercise
// Request Manager at volumes/patterns the existing unit and chaos suites
// don't: many concurrent duplicates, rapid cancellation across many
// in-flight requests, sustained online/offline cycling, growth-boundedness
// over hundreds of requests, and randomized failure injection. Nothing
// here changes RequestManager's behavior — these are read-only proofs
// against the existing implementation.

const fastRetry = { maxAttempts: 2, baseDelayMs: 1, factor: 1, maxDelayMs: 2, jitterRatio: 0 };

beforeEach(() => {
    vi.stubGlobal("navigator", { onLine: true });
});
afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
});

function makeHangingTransport() {
    return vi.fn(
        (config) =>
            new Promise((_resolve, reject) => {
                config.signal.addEventListener("abort", () => {
                    const err = new Error("canceled");
                    err.name = "CanceledError";
                    err.code = "ERR_CANCELED";
                    reject(err);
                });
            })
    );
}

describe("Part 6 — Request Manager stress tests", () => {
    it("100 concurrent identical requests collapse into exactly one transport call", async () => {
        const transport = vi.fn().mockResolvedValue({ data: { ok: true }, status: 200, headers: {} });
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const calls = Array.from({ length: 100 }, () => rm.get("/thing", { key: "shared" }));
        const results = await Promise.all(calls);

        expect(transport).toHaveBeenCalledTimes(1);
        expect(results).toHaveLength(100);
        for (const r of results) expect(r.data).toEqual({ ok: true });
    });

    it("rapid cancelAll() across many concurrent in-flight requests settles every one (no hang)", async () => {
        const transport = makeHangingTransport();
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const promises = Array.from({ length: 50 }, (_, i) => rm.get(`/thing-${i}`, { key: `k-${i}` }));
        await Promise.resolve(); // let all 50 register in the dedup store
        rm.cancelAll("rapid-navigation");

        const settled = await Promise.allSettled(promises);
        expect(settled.every((s) => s.status === "rejected")).toBe(true);
        expect(settled.every((s) => s.reason?.classification === "cancelled")).toBe(true);
    });

    it("sustained online/offline cycling does not corrupt circuit or dedup state", async () => {
        // RequestManager doesn't preemptively gate calls on navigator.onLine
        // — it only classifies an ALREADY-FAILED request as "offline" to
        // explain the cause (confirmed by reading errorClassifier.js: the
        // offline check only runs inside the catch branch). So a transport
        // that fails is what exercises the offline classification; a
        // transport that succeeds should succeed regardless of onLine.
        const transport = vi.fn((config) => {
            if (config.url === "/fails") {
                return Promise.reject(Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" }));
            }
            return Promise.resolve({ data: { ok: true }, status: 200, headers: {} });
        });
        const rm = new RequestManager({ transport, retryPolicy: { maxAttempts: 1 } });

        for (let i = 0; i < 20; i += 1) {
            vi.stubGlobal("navigator", { onLine: i % 2 === 0 ? false : true });
            if (i % 2 === 0) {
                // Distinct circuitKey per iteration — otherwise repeated
                // failures against the SAME circuit key would trip the
                // breaker partway through (a real, separately-verified
                // behavior in the next test, not what this one measures)
                // and later calls would short-circuit as "circuit_open"
                // before ever reaching the offline classification.
                // eslint-disable-next-line no-await-in-loop
                const err = await rm.get("/fails", { key: `cycle-${i}`, circuitKey: `circuit-${i}` }).catch((e) => e);
                expect(err?.classification).toBe("offline");
            } else {
                // eslint-disable-next-line no-await-in-loop
                const result = await rm.get("/thing", { key: `cycle-${i}` });
                expect(result?.data).toEqual({ ok: true });
            }
        }

        // Confirm the manager is still servicing requests normally
        // afterward — no corrupted state left over from the cycling.
        vi.stubGlobal("navigator", { onLine: true });
        const final = await rm.get("/thing", { key: "final" });
        expect(final.data).toEqual({ ok: true });
    });

    it("sustained failures against the SAME circuit key eventually short-circuit, even while offline", async () => {
        // Discovered while writing the previous test: recordFailure() is
        // called for offline-classified failures the same as any other
        // (only `cancelled` is excluded), so an extended offline period
        // hitting the same endpoint trips its circuit exactly like any
        // other sustained failure would — after that point, further calls
        // classify as circuit_open instead of offline, since they never
        // reach a transport attempt to classify in the first place. Correct
        // and intentional (the breaker can't know a cause it never
        // observed) — verifying it explicitly rather than leaving it as an
        // implicit side effect of the test above.
        vi.stubGlobal("navigator", { onLine: false });
        const transport = vi.fn().mockRejectedValue(Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" }));
        const rm = new RequestManager({
            transport,
            retryPolicy: { maxAttempts: 1 },
            circuitBreakerPolicy: { failureThreshold: 5, cooldownMs: 60_000 },
        });

        const classifications = [];
        for (let i = 0; i < 8; i += 1) {
            // eslint-disable-next-line no-await-in-loop
            const err = await rm.get("/fails", { key: `attempt-${i}` }).catch((e) => e);
            classifications.push(err.classification);
        }

        expect(classifications.slice(0, 5)).toEqual(Array(5).fill("offline"));
        expect(classifications.slice(5)).toEqual(Array(3).fill("circuit_open"));
    });
});

describe("Part 7 — bounded growth over volume", () => {
    it("getHistory() stays capped at 50 after hundreds of requests", async () => {
        const transport = vi.fn().mockResolvedValue({ data: { ok: true }, status: 200, headers: {} });
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        for (let i = 0; i < 300; i += 1) {
            // eslint-disable-next-line no-await-in-loop
            await rm.get(`/thing-${i}`, { key: `k-${i}` });
        }

        expect(rm.getHistory()).toHaveLength(50);
    });

    it("circuit breaker map is TTL-evicted rather than growing unbounded across many distinct keys", async () => {
        vi.useFakeTimers();
        const transport = vi.fn().mockResolvedValue({ data: { ok: true }, status: 200, headers: {} });
        const rm = new RequestManager({
            transport,
            retryPolicy: fastRetry,
            circuitBreakerPolicy: { failureThreshold: 3, cooldownMs: 1000, entryTtlMs: 5000 },
        });

        for (let i = 0; i < 100; i += 1) {
            // eslint-disable-next-line no-await-in-loop
            await rm.get(`/thing-${i}`, { key: `k-${i}`, circuitKey: `circuit-${i}` });
            vi.advanceTimersByTime(200); // 100 iterations * 200ms = 20s of simulated time, well past the 5s TTL
        }

        expect(rm.circuitBreaker.size).toBeLessThan(100);
    });

    it("dedup store has no leaked entries once everything settles", async () => {
        const transport = vi.fn().mockResolvedValue({ data: { ok: true }, status: 200, headers: {} });
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await Promise.all(Array.from({ length: 30 }, (_, i) => rm.get(`/thing-${i}`, { key: `k-${i}` })));

        // A brand-new call with a previously-used key must NOT dedup-join a
        // stale entry (it would if the store leaked finished entries) — it
        // should hit the transport again.
        const before = transport.mock.calls.length;
        await rm.get("/thing-0", { key: "k-0" });
        expect(transport.mock.calls.length).toBe(before + 1);
    });
});

describe("Part 9 — randomized chaos: the system always reaches a terminal state", () => {
    it("200 random failure injections each settle (resolve or reject), none hang", async () => {
        const outcomes = ["success", "server_error", "timeout", "network_error", "cancel"];
        const transport = vi.fn((config) => {
            const outcome = outcomes[Math.floor(Math.random() * outcomes.length)];
            const delay = Math.floor(Math.random() * 5);
            return new Promise((resolve, reject) => {
                const t = setTimeout(() => {
                    if (outcome === "success") {
                        resolve({ data: { ok: true }, status: 200, headers: {} });
                    } else if (outcome === "server_error") {
                        reject(Object.assign(new Error("server error"), { response: { status: 503 } }));
                    } else if (outcome === "network_error") {
                        reject(Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" }));
                    } else if (outcome === "timeout") {
                        // Never resolves on its own — only the manager's own timeout/abort ends it.
                    } else if (outcome === "cancel") {
                        const err = new Error("canceled");
                        err.code = "ERR_CANCELED";
                        reject(err);
                    }
                }, delay);
                config.signal.addEventListener("abort", () => {
                    clearTimeout(t);
                    const err = new Error("aborted");
                    err.code = "ERR_CANCELED";
                    reject(err);
                });
            });
        });

        const rm = new RequestManager({
            transport,
            retryPolicy: { maxAttempts: 2, baseDelayMs: 1, factor: 1, maxDelayMs: 2, jitterRatio: 0 },
        });

        const N = 200;
        const results = await Promise.allSettled(
            Array.from({ length: N }, (_, i) => rm.get(`/thing-${i}`, { key: `k-${i}`, timeoutMs: 20 }))
        );

        // The only assertion that matters for "always reaches a recoverable
        // state": every single promise settled — none are still pending.
        expect(results).toHaveLength(N);
        for (const r of results) {
            expect(["fulfilled", "rejected"]).toContain(r.status);
        }
    }, 15000);
});
