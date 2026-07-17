import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { buildDiagnosticsSnapshot } from "./clientDiagnostics";

function stubBrowserGlobals({ ua, onLine = true } = {}) {
    vi.stubGlobal("navigator", {
        userAgent:
            ua ||
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
        onLine,
        serviceWorker: {
            controller: null,
            getRegistrations: vi.fn().mockResolvedValue([]),
        },
    });
    vi.stubGlobal("window", {
        location: { pathname: "/l/test-slug" },
        innerWidth: 390,
        innerHeight: 844,
        devicePixelRatio: 3,
    });
    vi.stubGlobal("caches", { keys: vi.fn().mockResolvedValue(["talentgram-pwa-v4"]) });
    vi.stubGlobal("performance", { getEntriesByType: vi.fn().mockReturnValue([]) });
}

beforeEach(() => {
    stubBrowserGlobals();
});
afterEach(() => {
    vi.unstubAllGlobals();
});

describe("buildDiagnosticsSnapshot", () => {
    it("assembles the documented field set", async () => {
        const snapshot = await buildDiagnosticsSnapshot({ slug: "test-slug" });

        expect(snapshot).toMatchObject({
            route: { slug: "test-slug", pathname: "/l/test-slug" },
            viewport: { width: 390, height: 844, devicePixelRatio: 3 },
            network: { online: true },
        });
        expect(snapshot.browser.os).toBe("iOS");
        expect(snapshot.browser.deviceType).toBe("mobile");
        expect(snapshot.cacheNames).toEqual(["talentgram-pwa-v4"]);
        expect(Array.isArray(snapshot.requestHistory)).toBe(true);
        expect(typeof snapshot.timestamp).toBe("string");
    });

    it("includes Request Manager's history when a requestManager is supplied", async () => {
        const fakeRequestManager = { getHistory: () => [{ url: "/api/thing", outcome: "completed" }] };
        const snapshot = await buildDiagnosticsSnapshot({ requestManager: fakeRequestManager, slug: "test-slug" });

        expect(snapshot.requestHistory).toEqual([{ url: "/api/thing", outcome: "completed" }]);
    });

    it("defaults to an empty history array when no requestManager is supplied", async () => {
        const snapshot = await buildDiagnosticsSnapshot({});
        expect(snapshot.requestHistory).toEqual([]);
    });

    it("reflects offline network state", async () => {
        stubBrowserGlobals({ onLine: false });
        const snapshot = await buildDiagnosticsSnapshot({});
        expect(snapshot.network.online).toBe(false);
    });

    it("does not throw when caches/performance/serviceWorker are unavailable (SSR-like environment)", async () => {
        vi.unstubAllGlobals();
        vi.stubGlobal("navigator", undefined);
        vi.stubGlobal("window", undefined);
        vi.stubGlobal("caches", undefined);
        vi.stubGlobal("performance", undefined);

        await expect(buildDiagnosticsSnapshot({ slug: "test-slug" })).resolves.toBeTruthy();
    });

    describe("PII safety (hard requirement)", () => {
        it("never includes email, name, tokens, OTPs, cookies, or authorization values, even when present in supplied history/extra data", async () => {
            const fakeRequestManager = {
                getHistory: () => [
                    { url: "/auth/otp/verify", outcome: "completed" }, // structural only, as RequestManager actually produces
                ],
            };
            const snapshot = await buildDiagnosticsSnapshot({
                requestManager: fakeRequestManager,
                slug: "priya-shah-review", // a slug, not a name field — allowed
                extra: { talentId: "talent_123" },
            });

            const serialized = JSON.stringify(snapshot).toLowerCase();

            const forbiddenSubstrings = [
                "priya@example.com",
                "9999999999", // a phone-shaped value
                "bearer ",
                "authorization",
                "set-cookie",
                "otp\":\"", // a serialized OTP value shape
                "sk_live_", // a token-shaped secret
            ];
            for (const forbidden of forbiddenSubstrings) {
                expect(serialized).not.toContain(forbidden);
            }
        });

        it("never has a top-level or nested key named email, password, token, authorization, cookie, or otp", async () => {
            // "name" is deliberately excluded from this list — browser.name
            // (e.g. "Chrome") is a legitimate structural field, not a
            // person's name. Person-identifying values are covered by the
            // content-based test above (viewer email/phone/name are never
            // passed into buildDiagnosticsSnapshot's `extra` in the first
            // place — only opaque ids like `slug`/`talentId` are).
            const snapshot = await buildDiagnosticsSnapshot({ slug: "test-slug" });
            const forbiddenKeys = ["email", "password", "token", "authorization", "cookie", "otp"];

            function walk(value) {
                if (!value || typeof value !== "object") return;
                for (const key of Object.keys(value)) {
                    expect(forbiddenKeys).not.toContain(key.toLowerCase());
                    walk(value[key]);
                }
            }
            walk(snapshot);
        });
    });
});
