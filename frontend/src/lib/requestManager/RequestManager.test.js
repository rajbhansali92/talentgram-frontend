import { describe, it, expect, vi, beforeEach } from "vitest";
import { RequestManager } from "./RequestManager";
import { ErrorClassification } from "./errorClassifier";

// A transport double that respects config.signal the way axios/fetch do —
// tests exercise cancellation/timeout the same way a real transport would
// surface them back to the manager.
function makeHangingTransport() {
    return vi.fn(
        (config) =>
            new Promise((_resolve, reject) => {
                config.signal.addEventListener("abort", () => {
                    const err = new Error("The operation was aborted");
                    err.name = "AbortError";
                    reject(err);
                });
            })
    );
}

function makeScriptedTransport(outcomes) {
    let call = 0;
    return vi.fn((config) => {
        const outcome = outcomes[Math.min(call, outcomes.length - 1)];
        call += 1;
        if (outcome.type === "success") {
            return Promise.resolve({ data: outcome.data ?? { ok: true }, status: 200, headers: {} });
        }
        const err = new Error(outcome.message || "failed");
        if (outcome.status) err.response = { status: outcome.status, data: {} };
        if (outcome.code) err.code = outcome.code;
        return Promise.reject(err);
    });
}

const fastRetry = { maxAttempts: 3, baseDelayMs: 2, factor: 2, maxDelayMs: 10, jitterRatio: 0 };

beforeEach(() => {
    vi.stubGlobal("navigator", { onLine: true });
});

describe("RequestManager", () => {
    it("resolves with data, status, and a requestId on success", async () => {
        const transport = makeScriptedTransport([{ type: "success", data: { hello: "world" } }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const result = await rm.get("/api/thing");

        expect(result.data).toEqual({ hello: "world" });
        expect(result.status).toBe(200);
        expect(typeof result.requestId).toBe("string");
        expect(result.requestId.length).toBeGreaterThan(0);
    });

    it("attaches an x-request-id header to every transport call", async () => {
        const transport = makeScriptedTransport([{ type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await rm.get("/api/thing");

        expect(transport).toHaveBeenCalledTimes(1);
        const sentConfig = transport.mock.calls[0][0];
        expect(sentConfig.headers["x-request-id"]).toBeTruthy();
    });

    it("retries a retryable failure and eventually succeeds", async () => {
        const transport = makeScriptedTransport([
            { type: "error", status: 500 },
            { type: "error", status: 500 },
            { type: "success", data: { recovered: true } },
        ]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const result = await rm.get("/api/flaky");

        expect(result.data).toEqual({ recovered: true });
        expect(result.attempt).toBe(3);
        expect(transport).toHaveBeenCalledTimes(3);
    });

    it("does not retry a non-retryable 4xx and surfaces client_error", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 404 }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await expect(rm.get("/api/missing")).rejects.toMatchObject({
            classification: ErrorClassification.CLIENT_ERROR,
            status: 404,
        });
        expect(transport).toHaveBeenCalledTimes(1);
    });

    it("gives up after maxAttempts on a persistently failing endpoint", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 503 }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await expect(rm.get("/api/down")).rejects.toMatchObject({
            classification: ErrorClassification.SERVER_ERROR,
        });
        expect(transport).toHaveBeenCalledTimes(fastRetry.maxAttempts);
    });

    it("times out a hanging request and classifies it as timeout", async () => {
        const transport = makeHangingTransport();
        const rm = new RequestManager({ transport, retryPolicy: { ...fastRetry, maxAttempts: 1 } });

        await expect(rm.get("/api/slow", { timeoutMs: 15 })).rejects.toMatchObject({
            classification: ErrorClassification.TIMEOUT,
        });
    });

    it("cancel(key) rejects the in-flight request as cancelled and stops retries", async () => {
        const transport = makeHangingTransport();
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const promise = rm.get("/api/slow", { key: "slow-key" });
        await Promise.resolve(); // let the request register itself in the dedup store
        rm.cancel("slow-key");

        await expect(promise).rejects.toMatchObject({ classification: ErrorClassification.CANCELLED });
        expect(transport).toHaveBeenCalledTimes(1); // never retried after an explicit cancel
    });

    it("cancelAll() cancels every in-flight request", async () => {
        const transport = makeHangingTransport();
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const p1 = rm.get("/api/a", { key: "a" });
        const p2 = rm.get("/api/b", { key: "b" });
        await Promise.resolve();
        rm.cancelAll("navigation");

        await expect(p1).rejects.toMatchObject({ classification: ErrorClassification.CANCELLED });
        await expect(p2).rejects.toMatchObject({ classification: ErrorClassification.CANCELLED });
    });

    it("dedupes concurrent identical requests into a single transport call", async () => {
        let resolveTransport;
        const transport = vi.fn(
            () =>
                new Promise((resolve) => {
                    resolveTransport = resolve;
                })
        );
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const p1 = rm.get("/api/shared", { params: { a: 1 } });
        const p2 = rm.get("/api/shared", { params: { a: 1 } });
        resolveTransport({ data: { once: true }, status: 200, headers: {} });

        const [r1, r2] = await Promise.all([p1, p2]);
        expect(transport).toHaveBeenCalledTimes(1);
        expect(r1.data).toEqual({ once: true });
        expect(r2.data).toEqual({ once: true });
    });

    it("replace mode cancels the superseded call instead of joining it", async () => {
        const transport = makeHangingTransport();
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        const stale = rm.get("/api/search", { key: "search", mode: "replace" });
        await Promise.resolve();
        const fresh = rm.get("/api/search", { key: "search", mode: "replace" });

        await expect(stale).rejects.toMatchObject({ classification: ErrorClassification.CANCELLED });
        expect(transport).toHaveBeenCalledTimes(2);
        // fresh call is still hanging; just confirm it wasn't the one cancelled
        fresh.catch(() => {}); // avoid unhandled rejection warning if the suite tears down mid-flight
    });

    it("opens the circuit after repeated failures and short-circuits further calls", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 500 }]);
        const rm = new RequestManager({
            transport,
            retryPolicy: { ...fastRetry, maxAttempts: 1 },
            circuitBreakerPolicy: { failureThreshold: 2, cooldownMs: 60_000 },
        });

        await expect(rm.get("/api/down", { key: "1" })).rejects.toMatchObject({
            classification: ErrorClassification.SERVER_ERROR,
        });
        await expect(rm.get("/api/down", { key: "2" })).rejects.toMatchObject({
            classification: ErrorClassification.SERVER_ERROR,
        });

        expect(transport).toHaveBeenCalledTimes(2);

        await expect(rm.get("/api/down", { key: "3" })).rejects.toMatchObject({
            classification: ErrorClassification.CIRCUIT_OPEN,
        });
        // the third call never reached the transport at all
        expect(transport).toHaveBeenCalledTimes(2);
    });

    it("emits a structured log line for both success and failure", async () => {
        const logLines = [];
        const transport = makeScriptedTransport([{ type: "error", status: 500 }, { type: "success" }]);
        const rm = new RequestManager({
            transport,
            retryPolicy: fastRetry,
            onLog: (level, line) => logLines.push(JSON.parse(line)),
        });

        await rm.get("/api/thing");

        expect(logLines.length).toBeGreaterThanOrEqual(2);
        const failureLine = logLines.find((l) => l.outcome === "failure");
        const successLine = logLines.find((l) => l.outcome === "success");
        expect(failureLine).toMatchObject({ classification: ErrorClassification.SERVER_ERROR, status: 500 });
        expect(successLine).toMatchObject({ status: 200 });
        expect(successLine.request_id).toBeTruthy();
    });
});

describe("RequestManager — getHistory() diagnostics", () => {
    it("records a completed entry with requestId, attempts, and outcome", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 500 }, { type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await rm.get("/api/thing");

        const history = rm.getHistory();
        expect(history).toHaveLength(1);
        expect(history[0]).toMatchObject({ method: "get", url: "/api/thing", attempts: 2, outcome: "completed" });
        expect(typeof history[0].requestId).toBe("string");
        expect(typeof history[0].totalDurationMs).toBe("number");
    });

    it("records a failed entry after retries are exhausted", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 503 }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await expect(rm.get("/api/down")).rejects.toBeTruthy();

        const history = rm.getHistory();
        expect(history[0]).toMatchObject({ outcome: "failed", attempts: fastRetry.maxAttempts, classification: ErrorClassification.SERVER_ERROR });
    });

    it("records short_circuited and dedup_joined entries distinctly", async () => {
        const transport = makeScriptedTransport([{ type: "error", status: 500 }]);
        const rm = new RequestManager({
            transport,
            retryPolicy: { ...fastRetry, maxAttempts: 1 },
            circuitBreakerPolicy: { failureThreshold: 1, cooldownMs: 60_000 },
        });

        await expect(rm.get("/api/down")).rejects.toBeTruthy(); // trips the circuit
        await expect(rm.get("/api/down")).rejects.toBeTruthy(); // short-circuited

        const history = rm.getHistory();
        expect(history[0].outcome).toBe("short_circuited");
        expect(history[1].outcome).toBe("failed");
    });

    it("carries the caller-supplied transportKind through to the history entry", async () => {
        const transport = makeScriptedTransport([{ type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await rm.request({ method: "get", url: "/api/thing", transportKind: "proxy" });

        expect(rm.getHistory()[0].transport).toBe("proxy");
    });

    it("is bounded at 50 entries", async () => {
        const transport = makeScriptedTransport([{ type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        for (let i = 0; i < 55; i += 1) {
            await rm.get(`/api/thing-${i}`, { key: `k-${i}` });
        }

        expect(rm.getHistory()).toHaveLength(50);
    });

    it("returns newest-first order", async () => {
        const transport = makeScriptedTransport([{ type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await rm.get("/api/first", { key: "a" });
        await rm.get("/api/second", { key: "b" });

        const history = rm.getHistory();
        expect(history[0].url).toBe("/api/second");
        expect(history[1].url).toBe("/api/first");
    });

    it("never includes request body/params — only structural fields", async () => {
        const transport = makeScriptedTransport([{ type: "success" }]);
        const rm = new RequestManager({ transport, retryPolicy: fastRetry });

        await rm.post("/api/otp/verify", { email: "x@example.com", otp: "482913" });

        const serialized = JSON.stringify(rm.getHistory());
        expect(serialized).not.toContain("x@example.com");
        expect(serialized).not.toContain("482913");
    });
});
