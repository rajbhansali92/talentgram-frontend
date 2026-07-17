import { describe, it, expect, vi, beforeEach } from "vitest";
import { createAxiosCompatibleClient } from "./axiosShim";

// A transport double shaped like axios: resolves/rejects exactly the way
// axios.request() would, and honors config.signal the same way axios does
// (rejecting with a CanceledError-shaped object on abort) so the shim's
// interaction with Request Manager's cancellation/timeout machinery is
// exercised realistically.
function makeAxiosLikeTransport(script) {
    let call = 0;
    return vi.fn((config) => {
        const step = script[Math.min(call, script.length - 1)];
        call += 1;
        return new Promise((resolve, reject) => {
            if (step.type === "success") {
                resolve({
                    data: step.data ?? { ok: true },
                    status: step.status ?? 200,
                    headers: step.headers ?? { "content-type": "application/json" },
                    statusText: "OK",
                    config,
                    request: {},
                });
                return;
            }
            if (step.type === "hang") {
                config.signal.addEventListener("abort", () => {
                    const err = new Error(step.abortMessage || "canceled");
                    err.name = "CanceledError";
                    err.code = "ERR_CANCELED";
                    reject(err);
                });
                return;
            }
            // Plain axios-shaped rejection.
            const err = new Error(step.message || "Request failed");
            err.isAxiosError = true;
            err.config = config;
            if (step.status) {
                err.response = { status: step.status, data: step.data ?? {}, headers: {} };
            }
            if (step.code) err.code = step.code;
            reject(err);
        });
    });
}

const fastRetry = { maxAttempts: 1 }; // isolate shim behavior from retry behavior in most tests

beforeEach(() => {
    vi.stubGlobal("navigator", { onLine: true });
});

describe("createAxiosCompatibleClient — success shape", () => {
    it("resolves with an axios-response-shaped object", async () => {
        const transport = makeAxiosLikeTransport([{ type: "success", data: { hello: "world" }, status: 201 }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/thing");

        expect(response).toMatchObject({ data: { hello: "world" }, status: 201 });
        expect(response.headers).toBeDefined();
        expect(response.config).toBeDefined();
        expect(typeof response.statusText).toBe("string");
    });

    it("get/post/put/patch/delete place method, url, and data correctly", async () => {
        const transport = makeAxiosLikeTransport([{ type: "success" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await client.post("/thing", { a: 1 }, { headers: { "X-Extra": "1" } });

        const sent = transport.mock.calls[0][0];
        expect(sent.method).toBe("post");
        expect(sent.url).toBe("/thing");
        expect(sent.data).toEqual({ a: 1 });
        expect(sent.headers["X-Extra"]).toBe("1");
    });

    it("attaches Request Manager diagnostics (requestId/attempt/durationMs) without disturbing the existing shape", async () => {
        const transport = makeAxiosLikeTransport([{ type: "success", data: { hello: "world" }, status: 201 }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/thing");

        // Existing, already-relied-upon fields are untouched.
        expect(response.data).toEqual({ hello: "world" });
        expect(response.status).toBe(201);
        // New diagnostic fields are reachable for the first time.
        expect(typeof response.requestId).toBe("string");
        expect(response.attempt).toBe(1);
        expect(typeof response.durationMs).toBe("number");
    });

    it("preserves a Blob response body untouched (download responseType)", async () => {
        const blob = new Blob(["zip-bytes"]);
        const transport = makeAxiosLikeTransport([{ type: "success", data: blob }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/download", { responseType: "blob" });
        expect(response.data).toBe(blob);
    });
});

describe("createAxiosCompatibleClient — error unwrapping", () => {
    it("rethrows the original axios error unmodified on a real HTTP failure", async () => {
        const transport = makeAxiosLikeTransport([
            { type: "error", status: 404, data: { detail: "Not found" } },
        ]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await expect(client.get("/missing")).rejects.toMatchObject({
            isAxiosError: true,
            response: { status: 404, data: { detail: "Not found" } },
        });
    });

    it("also attaches requestId/classification onto the unwrapped original error", async () => {
        const transport = makeAxiosLikeTransport([{ type: "error", status: 404, data: { detail: "Not found" } }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const err = await client.get("/missing").catch((e) => e);
        expect(err.response.status).toBe(404); // existing shape untouched
        expect(typeof err.requestId).toBe("string");
        expect(err.classification).toBe("client_error");
    });

    it("preserves err.response.data.detail as a FastAPI validation array untouched", async () => {
        const detail = [{ loc: ["body", "email"], msg: "field required", type: "value_error.missing" }];
        const transport = makeAxiosLikeTransport([{ type: "error", status: 422, data: { detail } }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        try {
            await client.post("/apply", { name: "x" });
            throw new Error("expected rejection");
        } catch (err) {
            expect(err.response.data.detail).toEqual(detail);
        }
    });

    it("preserves err.response.data instanceof Blob for a failed blob-responseType download", async () => {
        const errorBlob = new Blob(['{"detail":"Not found"}']);
        const transport = vi.fn((config) => {
            const err = new Error("Request failed with status code 404");
            err.isAxiosError = true;
            err.config = config;
            err.response = { status: 404, data: errorBlob, headers: {} };
            return Promise.reject(err);
        });
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        try {
            await client.get("/download", { responseType: "blob" });
            throw new Error("expected rejection");
        } catch (err) {
            expect(err.response.data).toBeInstanceOf(Blob);
        }
    });

    it("preserves err.code === 'ECONNABORTED' for axios's own native timeout", async () => {
        const transport = makeAxiosLikeTransport([{ type: "error", code: "ECONNABORTED", message: "timeout of 12000ms exceeded" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await expect(client.get("/slow", { timeout: 12000 })).rejects.toMatchObject({ code: "ECONNABORTED" });
    });

    it("preserves err.code === 'ERR_CANCELED' / err.name === 'CanceledError' for a caller-driven AbortController", async () => {
        const transport = makeAxiosLikeTransport([{ type: "hang" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const controller = new AbortController();
        const promise = client.get("/slow", { signal: controller.signal });
        controller.abort();

        await expect(promise).rejects.toMatchObject({ code: "ERR_CANCELED", name: "CanceledError" });
    });

    it("synthesizes an axios-shaped error (no .response) when the circuit is open", async () => {
        const transport = makeAxiosLikeTransport([{ type: "error", status: 503 }]);
        const client = createAxiosCompatibleClient(transport, {
            retryPolicy: fastRetry,
            circuitBreakerPolicy: { failureThreshold: 1, cooldownMs: 60_000 },
        });

        // First call trips the circuit.
        await expect(client.get("/down", { key: "a" })).rejects.toMatchObject({ response: { status: 503 } });
        // Second call is short-circuited — never reaches the transport.
        await expect(client.get("/down", { key: "b" })).rejects.toMatchObject({
            isAxiosError: true,
            response: undefined,
            code: "ERR_CIRCUIT_OPEN",
        });
        expect(transport).toHaveBeenCalledTimes(1);
    });
});

describe("createAxiosCompatibleClient — timeout backstop", () => {
    it("passes an explicit config.timeout straight through to the transport unmodified", async () => {
        const transport = makeAxiosLikeTransport([{ type: "success" }]);
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        await client.get("/thing", { timeout: 120000 });

        expect(transport.mock.calls[0][0].timeout).toBe(120000);
    });

    it("does not let Request Manager's own timeout fire before a longer explicit axios timeout resolves", async () => {
        // Transport takes 30ms to resolve; explicit config.timeout is tiny
        // (10ms) but Request Manager's backstop (10ms + 5000ms) must not
        // preempt it — the call should still succeed.
        const transport = vi.fn(
            (config) =>
                new Promise((resolve, reject) => {
                    const t = setTimeout(() => resolve({ data: { ok: true }, status: 200, headers: {} }), 30);
                    config.signal.addEventListener("abort", () => {
                        clearTimeout(t);
                        const err = new Error("aborted");
                        err.code = "ERR_CANCELED";
                        reject(err);
                    });
                })
        );
        const client = createAxiosCompatibleClient(transport, { retryPolicy: fastRetry });

        const response = await client.get("/thing", { timeout: 10 });
        expect(response.data).toEqual({ ok: true });
    });
});
