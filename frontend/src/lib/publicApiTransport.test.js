import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { classifyRequestKind, createPublicApiClient } from "./publicApiTransport";

beforeEach(() => {
    vi.stubGlobal("navigator", { onLine: true });
});
afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe("classifyRequestKind", () => {
    it("classifies a blob responseType as download", () => {
        expect(classifyRequestKind({ responseType: "blob" })).toBe("download");
    });

    it("classifies an arraybuffer responseType as download", () => {
        expect(classifyRequestKind({ responseType: "arraybuffer" })).toBe("download");
    });

    it("classifies a FormData body as upload", () => {
        expect(classifyRequestKind({ data: new FormData() })).toBe("upload");
    });

    it("classifies a plain JSON body as standard", () => {
        expect(classifyRequestKind({ data: { email: "x@example.com" } })).toBe("standard");
    });

    it("classifies a GET with no body as standard", () => {
        expect(classifyRequestKind({ method: "get", url: "/thing" })).toBe("standard");
    });

    it("real call-site configs classify as expected", () => {
        // ClientView's ZIP download.
        expect(
            classifyRequestKind({ method: "get", url: "/public/links/slug/download/talent/123", responseType: "blob", timeout: 120000 })
        ).toBe("download");
        // ClientView's saveVoiceNote / FeedbackComposer voice-note upload.
        const form = new FormData();
        form.append("talent_id", "123");
        expect(classifyRequestKind({ method: "post", url: "/public/links/slug/feedback/voice", data: form })).toBe("upload");
        // OTP send.
        expect(classifyRequestKind({ method: "post", url: "/auth/otp/send", data: { email: "x@example.com" } })).toBe("standard");
        // ClientView's plain loadData GET.
        expect(classifyRequestKind({ method: "get", url: "/public/links/slug" })).toBe("standard");
    });
});

describe("createPublicApiClient — routes requests to the correct underlying axios instance", () => {
    // axios.create() is mocked so each instance's `.request` is a distinct,
    // inspectable spy — this is what actually proves "standard traffic hits
    // the proxy instance, download/upload traffic hits the Railway
    // instance", rather than just re-testing the classifier in isolation.
    let createdInstances;

    beforeEach(async () => {
        createdInstances = [];
        const axios = (await import("axios")).default;
        vi.spyOn(axios, "create").mockImplementation((config) => {
            const instance = {
                baseURL: config.baseURL,
                request: vi.fn().mockResolvedValue({ data: { ok: true }, status: 200, headers: {} }),
                interceptors: { request: { use: vi.fn() } },
            };
            createdInstances.push(instance);
            return instance;
        });
    });

    function getInstances() {
        const railway = createdInstances.find((i) => i.baseURL === "https://railway.example/api");
        const proxy = createdInstances.find((i) => i.baseURL === "/api/proxy");
        return { railway, proxy };
    }

    it("creates exactly one Railway-direct instance and one same-origin proxy instance", () => {
        createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();
        expect(railway).toBeDefined();
        expect(proxy).toBeDefined();
    });

    it("attaches the auth interceptor to both underlying instances", () => {
        createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();
        expect(railway.interceptors.request.use).toHaveBeenCalledTimes(1);
        expect(proxy.interceptors.request.use).toHaveBeenCalledTimes(1);
    });

    it("routes a standard JSON request through the proxy instance", async () => {
        const client = createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();

        await client.post("/auth/otp/send", { email: "x@example.com" });

        expect(proxy.request).toHaveBeenCalledTimes(1);
        expect(railway.request).not.toHaveBeenCalled();
    });

    it("routes a blob-responseType download through the Railway-direct instance", async () => {
        const client = createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();

        await client.get("/public/links/slug/download/talent/1", { responseType: "blob", timeout: 120000 });

        expect(railway.request).toHaveBeenCalledTimes(1);
        expect(proxy.request).not.toHaveBeenCalled();
    });

    it("routes a FormData upload through the Railway-direct instance", async () => {
        const client = createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });
        const { railway, proxy } = getInstances();

        const form = new FormData();
        form.append("talent_id", "1");
        await client.post("/public/links/slug/feedback/voice", form);

        expect(railway.request).toHaveBeenCalledTimes(1);
        expect(proxy.request).not.toHaveBeenCalled();
    });

    it("tags Request Manager's diagnostics history with which physical transport was used", async () => {
        const client = createPublicApiClient({ backendApiUrl: "https://railway.example/api", portalTokenKey: "tok" });

        await client.post("/auth/otp/send", { email: "x@example.com" });
        await client.get("/public/links/slug/download/talent/1", { responseType: "blob" });

        const history = client._requestManager.getHistory();
        const standardEntry = history.find((h) => h.url === "/auth/otp/send");
        const downloadEntry = history.find((h) => h.url === "/public/links/slug/download/talent/1");
        expect(standardEntry.transport).toBe("proxy");
        expect(downloadEntry.transport).toBe("railway-direct");
    });
});
