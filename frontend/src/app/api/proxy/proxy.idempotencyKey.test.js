// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { POST } from "./[...path]/route";

// Finalize idempotency (incident 2026-10-14): the proxy forwards only an
// explicit allow-list of request headers. If Idempotency-Key were dropped
// here, every submit would silently run unprotected.
const ctx = { params: Promise.resolve({ path: ["public", "submissions", "abc", "finalize"] }) };
let upstream;
beforeEach(() => {
    vi.stubEnv("BACKEND_INTERNAL_URL", "http://railway.test");
    upstream = vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", upstream);
});
afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
});

async function call(headers) {
    const req = new Request("http://localhost/api/proxy/public/submissions/abc/finalize", {
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
        body: "{}",
    });
    const res = await POST(req, ctx);
    await res.text();
    return upstream.mock.calls[0][1].headers;
}

describe("proxy forwards the finalize Idempotency-Key", () => {
    it("relays Idempotency-Key and Authorization to Railway", async () => {
        const h = await call({ "idempotency-key": "k-0123456789abcdef", authorization: "Bearer t" });
        expect(h.get("idempotency-key")).toBe("k-0123456789abcdef");
        expect(h.get("authorization")).toBe("Bearer t");
    });
    it("sends nothing when the browser sent no key (legacy clients unchanged)", async () => {
        const h = await call({ authorization: "Bearer t" });
        expect(h.get("idempotency-key")).toBeNull();
    });
});
