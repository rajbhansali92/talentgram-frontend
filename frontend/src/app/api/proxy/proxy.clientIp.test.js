// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { POST } from "./[...path]/route";

// The proxy is the only thing allowed to tell Railway the real client IP (header + shared
// secret, checked by backend core.get_client_ip). The secret below is a throwaway test value.
const TEST_SECRET = "unit-test-proxy-secret-not-real";
const ctx = { params: Promise.resolve({ path: ["auth", "otp", "send"] }) };

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

async function callProxy(headers) {
    const req = new Request("http://localhost/api/proxy/auth/otp/send", {
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
        body: JSON.stringify({ email: "a@example.com" }),
    });
    const res = await POST(req, ctx);
    await res.text(); // drain so the stream settles
    return upstream.mock.calls[0][1].headers; // Headers sent to Railway
}

describe("proxy -> Railway client-IP headers", () => {
    it("sends the real client IP and the shared secret when the secret is configured", async () => {
        vi.stubEnv("TG_PROXY_SHARED_SECRET", TEST_SECRET);
        const h = await callProxy({ "x-vercel-forwarded-for": "49.36.10.20", "x-forwarded-for": "49.36.10.20, 76.76.21.9" });
        expect(h.get("x-tg-client-ip")).toBe("49.36.10.20");
        expect(h.get("x-tg-proxy-secret")).toBe(TEST_SECRET);
        expect(h.get("x-forwarded-for")).toBe("49.36.10.20"); // pre-existing behaviour kept
    });

    it("never relays browser-supplied x-tg-* headers: the values come only from the server", async () => {
        vi.stubEnv("TG_PROXY_SHARED_SECRET", TEST_SECRET);
        const h = await callProxy({
            "x-vercel-forwarded-for": "49.36.10.20",
            "x-tg-client-ip": "6.6.6.6",
            "x-tg-proxy-secret": "attacker-guess",
        });
        expect(h.get("x-tg-client-ip")).toBe("49.36.10.20");
        expect(h.get("x-tg-proxy-secret")).toBe(TEST_SECRET);
    });

    it("sends nothing extra when no secret is configured, even if the browser supplies the headers", async () => {
        const h = await callProxy({ "x-vercel-forwarded-for": "49.36.10.20", "x-tg-client-ip": "6.6.6.6", "x-tg-proxy-secret": "attacker-guess" });
        expect(h.get("x-tg-client-ip")).toBeNull();
        expect(h.get("x-tg-proxy-secret")).toBeNull();
        expect(h.get("x-forwarded-for")).toBe("49.36.10.20");
    });

    it("does not send the secret when no client IP can be determined", async () => {
        vi.stubEnv("TG_PROXY_SHARED_SECRET", TEST_SECRET);
        const h = await callProxy({});
        expect(h.get("x-tg-client-ip")).toBeNull();
        expect(h.get("x-tg-proxy-secret")).toBeNull();
    });

    it("preserves the existing forwarded request headers", async () => {
        vi.stubEnv("TG_PROXY_SHARED_SECRET", TEST_SECRET);
        const h = await callProxy({ authorization: "Bearer abc", "x-vercel-forwarded-for": "49.36.10.20", cookie: "a=b" });
        expect(h.get("authorization")).toBe("Bearer abc");
        expect(h.get("cookie")).toBe("a=b");
        expect(h.get("content-type")).toBe("application/json");
    });
});
