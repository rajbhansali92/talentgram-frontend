import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createAxiosCompatibleClient } from "./axiosShim";
import { createPublicApiClient } from "../publicApiTransport";

// Controlled-failure ("chaos") scenarios for the Phase 4 public API
// migration. Each test simulates one named failure mode from the
// verification checklist against a fake transport shaped exactly like the
// real failure would look coming out of axios, and asserts on what the
// caller (an existing, unmodified component) would actually observe.
//
// Two known, pre-existing limitations carried over unfixed from the Phase 3
// architecture review (not introduced or claimed fixed by this phase):
// (1) DNS_FAILURE classification only matches Node/undici-style error codes
// (ENOTFOUND/EAI_AGAIN); a real browser never exposes these for an actual
// DNS failure, so this only verifies the classifier's own documented
// handling of that code shape, not real browser DNS-failure behavior.
// (2) MALFORMED_RESPONSE classification requires a SyntaxError (or an
// explicit `isMalformedResponse` flag) on the rejected error; axios's
// default JSON parsing swallows parse failures and returns raw text rather
// than throwing, so this tests the classifier's existing handling of that
// shape being supplied, not a claim that axios triggers it automatically.

function scriptedTransport(script) {
    let call = 0;
    return vi.fn((config) => {
        const step = script[Math.min(call, script.length - 1)];
        call += 1;
        if (step.type === "success") {
            return Promise.resolve({ data: step.data ?? { ok: true }, status: step.status ?? 200, headers: {} });
        }
        if (step.type === "hang") {
            return new Promise((_resolve, reject) => {
                config.signal.addEventListener("abort", () => {
                    const err = new Error("canceled");
                    err.name = "CanceledError";
                    err.code = "ERR_CANCELED";
                    reject(err);
                });
            });
        }
        const err = new Error(step.message || "Request failed");
        err.isAxiosError = true;
        err.config = config;
        if (step.status) err.response = { status: step.status, data: step.data ?? {}, headers: {} };
        if (step.code) err.code = step.code;
        if (step.name) err.name = step.name;
        return Promise.reject(err);
    });
}

const noRetry = { maxAttempts: 1 };
const fastRetry = { maxAttempts: 3, baseDelayMs: 2, factor: 2, maxDelayMs: 10, jitterRatio: 0 };

beforeEach(() => {
    vi.stubGlobal("navigator", { onLine: true });
});
afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
});

describe("chaos: transport-level failures", () => {
    it("Railway unavailable (ECONNREFUSED) surfaces as a network error with no .response", async () => {
        const transport = scriptedTransport([{ type: "error", code: "ECONNREFUSED", message: "connect ECONNREFUSED" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const err = await client.get("/public/links/x").catch((e) => e);
        expect(err.code).toBe("ECONNREFUSED");
        expect(err.response).toBeUndefined();
    });

    it("502 from the proxy classifies as a server error and is retried (idempotent GET)", async () => {
        const transport = scriptedTransport([{ type: "error", status: 502 }, { type: "success" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/public/links/x");
        expect(response.status).toBe(200);
        expect(transport).toHaveBeenCalledTimes(2);
    });

    it("504 from the proxy classifies as a server error and is retried the same as 502", async () => {
        const transport = scriptedTransport([{ type: "error", status: 504 }, { type: "success" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/public/links/x");
        expect(response.status).toBe(200);
        expect(transport).toHaveBeenCalledTimes(2);
    });

    it("timeout (axios ECONNABORTED) preserves its native error shape and is retried on GET", async () => {
        const transport = scriptedTransport([
            { type: "error", code: "ECONNABORTED", message: "timeout of 12000ms exceeded" },
            { type: "success" },
        ]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/public/projects/x", { timeout: 12000 });
        expect(response.status).toBe(200);
        expect(transport).toHaveBeenCalledTimes(2);
    });

    it("offline fails fast without burning a retry (offline is not in the retryable set)", async () => {
        vi.stubGlobal("navigator", { onLine: false });
        const transport = scriptedTransport([{ type: "error", code: "ERR_NETWORK", message: "Network Error" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await expect(client.get("/public/links/x")).rejects.toBeTruthy();
        expect(transport).toHaveBeenCalledTimes(1); // no retry attempted while offline
    });

    it("DNS failure (Node-style ENOTFOUND code) is classified and treated as retryable", async () => {
        const transport = scriptedTransport([{ type: "error", code: "ENOTFOUND" }, { type: "success" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/public/links/x");
        expect(response.status).toBe(200);
        expect(transport).toHaveBeenCalledTimes(2);
    });

    it("malformed JSON (SyntaxError-shaped rejection) surfaces to the caller without crashing the client", async () => {
        const transport = scriptedTransport([{ type: "error", name: "SyntaxError", message: "Unexpected token <" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        await expect(client.get("/public/links/x")).rejects.toMatchObject({ name: "SyntaxError" });
    });

    it("retry exhaustion: a persistently failing endpoint surfaces the original axios error after maxAttempts", async () => {
        const transport = scriptedTransport([{ type: "error", status: 503 }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await expect(client.get("/public/links/x")).rejects.toMatchObject({ response: { status: 503 } });
        expect(transport).toHaveBeenCalledTimes(fastRetry.maxAttempts);
    });
});

describe("chaos: circuit breaker opening and recovery", () => {
    it("opens after repeated failures and short-circuits further calls without touching the transport", async () => {
        const transport = scriptedTransport([{ type: "error", status: 500 }]);
        const client = createAxiosCompatibleClient(transport, {
            retryPolicy: noRetry,
            circuitBreakerPolicy: { failureThreshold: 2, cooldownMs: 60_000 },
        });

        await expect(client.get("/public/links/a", { key: "1" })).rejects.toBeTruthy();
        await expect(client.get("/public/links/b", { key: "2" })).rejects.toBeTruthy();
        expect(transport).toHaveBeenCalledTimes(2);

        await expect(client.get("/public/links/c", { key: "3" })).rejects.toMatchObject({ code: "ERR_CIRCUIT_OPEN" });
        expect(transport).toHaveBeenCalledTimes(2); // third call never reached the transport
    });

    it("recovers: after cooldown, a single successful probe closes the breaker for subsequent calls", async () => {
        vi.useFakeTimers();
        const transport = scriptedTransport([{ type: "error", status: 500 }, { type: "success" }, { type: "success" }]);
        const client = createAxiosCompatibleClient(transport, {
            retryPolicy: noRetry,
            circuitBreakerPolicy: { failureThreshold: 1, cooldownMs: 1000 },
        });

        await expect(client.get("/public/links/a", { key: "1" })).rejects.toBeTruthy();
        // Circuit is now open; an immediate call short-circuits.
        await expect(client.get("/public/links/a", { key: "2" })).rejects.toMatchObject({ code: "ERR_CIRCUIT_OPEN" });

        await vi.advanceTimersByTimeAsync(1001);

        // Cooldown elapsed: the next call is the probe and succeeds.
        const probe = await client.get("/public/links/a", { key: "3" });
        expect(probe.status).toBe(200);

        // Breaker is closed again: normal traffic resumes.
        const next = await client.get("/public/links/a", { key: "4" });
        expect(next.status).toBe(200);
        expect(transport).toHaveBeenCalledTimes(3); // 1 failure + 1 probe + 1 normal call
    });
});

describe("chaos: proxy vs Railway-direct isolation", () => {
    let createdInstances;

    beforeEach(async () => {
        createdInstances = [];
        const axios = (await import("axios")).default;
        vi.spyOn(axios, "create").mockImplementation((config) => {
            const instance = { baseURL: config.baseURL, interceptors: { request: { use: vi.fn() } } };
            createdInstances.push(instance);
            return instance;
        });
    });

    function getInstances() {
        return {
            railway: createdInstances.find((i) => i.baseURL === "https://railway.example/api"),
            proxy: createdInstances.find((i) => i.baseURL === "/api/proxy"),
        };
    }

    it("proxy unavailable: a standard call fails via the proxy instance while an unrelated Railway-direct download still succeeds", async () => {
        const client = createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();
        proxy.request = vi.fn().mockRejectedValue(Object.assign(new Error("Bad Gateway"), { response: { status: 502 } }));
        railway.request = vi.fn().mockResolvedValue({ data: new Blob(["zip"]), status: 200, headers: {} });

        await expect(client.post("/auth/otp/send", { email: "x@example.com" })).rejects.toMatchObject({
            response: { status: 502 },
        });

        const downloadResponse = await client.get("/public/links/x/download/talent/1", { responseType: "blob", timeout: 120000 });
        expect(downloadResponse.status).toBe(200);
        expect(railway.request).toHaveBeenCalledTimes(1);
    });
});

describe("chaos: cancellation and staleness", () => {
    it("client disconnect / cancelled request rejects with a CanceledError-shaped error, not a stale success", async () => {
        const transport = scriptedTransport([{ type: "hang" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const promise = client.get("/public/links/x", { key: "download-1" });
        await Promise.resolve();
        client._requestManager.cancel("download-1");

        await expect(promise).rejects.toMatchObject({ code: "ERR_CANCELED", name: "CanceledError" });
    });

    it("no stale responses: cancelAll() rejects every in-flight call rather than letting a late response resolve", async () => {
        const transport = scriptedTransport([{ type: "hang" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const a = client.get("/public/links/a", { key: "a" });
        const b = client.get("/public/links/b", { key: "b" });
        await Promise.resolve();
        client._requestManager.cancelAll("navigation");

        await expect(a).rejects.toMatchObject({ code: "ERR_CANCELED" });
        await expect(b).rejects.toMatchObject({ code: "ERR_CANCELED" });
    });
});

describe("chaos: no duplicate submissions / no duplicate OTP verification", () => {
    it("two concurrent identical submission-start POSTs collapse into a single transport call", async () => {
        const transport = scriptedTransport([{ type: "success", data: { submissionId: "sub_1" } }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const payload = { slug: "proj-x", talentId: "t1" };
        const [first, second] = await Promise.all([
            client.post("/public/projects/proj-x/submission", payload),
            client.post("/public/projects/proj-x/submission", payload),
        ]);

        expect(transport).toHaveBeenCalledTimes(1);
        expect(first.data).toEqual({ submissionId: "sub_1" });
        expect(second.data).toEqual({ submissionId: "sub_1" });
    });

    it("two concurrent identical OTP-verify POSTs collapse into a single transport call", async () => {
        const transport = scriptedTransport([{ type: "success", data: { verified: true } }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const payload = { email: "x@example.com", otp: "482913" };
        const [first, second] = await Promise.all([
            client.post("/auth/otp/verify", payload),
            client.post("/auth/otp/verify", payload),
        ]);

        expect(transport).toHaveBeenCalledTimes(1);
        expect(first.data).toEqual({ verified: true });
        expect(second.data).toEqual({ verified: true });
    });

    it("a second, sequential OTP-verify with a genuinely different OTP is NOT deduped", async () => {
        const transport = scriptedTransport([
            { type: "success", data: { verified: false } },
            { type: "success", data: { verified: true } },
        ]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: noRetry });

        const first = await client.post("/auth/otp/verify", { email: "x@example.com", otp: "111111" });
        const second = await client.post("/auth/otp/verify", { email: "x@example.com", otp: "222222" });

        expect(transport).toHaveBeenCalledTimes(2);
        expect(first.data.verified).toBe(false);
        expect(second.data.verified).toBe(true);
    });

    it("submission POST bodies are never auto-retried by Request Manager (no accidental double-submit from retry)", async () => {
        const transport = scriptedTransport([{ type: "error", status: 503 }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry }); // retries allowed in general...

        await expect(client.post("/public/projects/x/submission", { a: 1 })).rejects.toBeTruthy();
        // ...but POST is not idempotent-by-default, so exactly one attempt was made.
        expect(transport).toHaveBeenCalledTimes(1);
    });
});
