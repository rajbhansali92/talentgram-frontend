import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";

// Heavy page bodies are irrelevant here — only each route's metadata is under test.
vi.mock("@/index.css", () => ({}));
vi.mock("@/App.css", () => ({}));
vi.mock("sonner", () => ({ Toaster: () => null }));
vi.mock("@/context/UploadManagerContext", () => ({ UploadManagerProvider: ({ children }) => children }));
vi.mock("@/components/PWAInitializer", () => ({ default: () => null }));
vi.mock("@/components/ErrorBoundary", () => ({ default: ({ children }) => children }));
vi.mock("@/pages-components/ApplicationPage", () => ({ default: () => null }));
vi.mock("@/pages-components/SubmissionPage", () => ({ default: () => null }));
vi.mock("@/pages-components/ClientView", () => ({ default: () => null }));
vi.mock("@/pages-components/RosterView", () => ({ default: () => null }));
vi.mock("@/pages-components/TalentMediaPage", () => ({ default: () => null }));

let mockHost = "talentgramagency.com";
vi.mock("next/headers", () => ({ headers: async () => new Map([["host", mockHost]]) }));

import { OG_IMAGE_PATH, OG_IMAGE_URL } from "@/lib/ogBrand";
import { generateMetadata as layoutMetadata } from "./layout";
import { metadata as applyMetadata } from "./(apply)/apply/page";
import { generateMetadata as submitMetadata } from "./(submit)/submit/[slug]/page";
import { generateMetadata as linksMetadata } from "./(links)/l/[slug]/page";
import { metadata as talentMediaMetadata } from "./talent-media/[token]/page";
import { GET as legacyOgImage } from "./og-image/route";

const ROOT = path.resolve(__dirname, "../..");
const first = (v) => (Array.isArray(v) ? v[0] : v);
const ogUrl = (md) => first(md.openGraph.images).url;
const twUrl = (md) => first(md.twitter.images);

afterEach(() => {
    vi.restoreAllMocks();
});

describe("canonical external-share logo asset", () => {
    const file = path.join(ROOT, "public", OG_IMAGE_PATH);

    it("exists under /public at the path every page references", () => {
        expect(fs.existsSync(file)).toBe(true);
        expect(OG_IMAGE_URL.startsWith(OG_IMAGE_PATH)).toBe(true);
    });

    it("is a 1200x630 PNG (the dimensions the pages advertise — no stretching)", () => {
        const buf = fs.readFileSync(file);
        expect(buf.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
        expect(buf.readUInt32BE(16)).toBe(1200);
        expect(buf.readUInt32BE(20)).toBe(630);
    });
});

describe("every public link family advertises the canonical logo", () => {
    beforeEach(() => {
        mockHost = "talentgramagency.com";
    });

    for (const [host, titlePart] of [
        ["talentgramagency.com", "Talentgram Agency"],
        ["apply.talentgramagency.com", "Apply Portal"],
        ["review.talentgramagency.com", "Review Centre"],
        ["submit.talentgramagency.com", "Submission Portal"],
        ["links.talentgramagency.com", "Portfolios"],
    ]) {
        it(`root layout on ${host}: og:image + twitter:image canonical, host-specific title kept`, async () => {
            mockHost = host;
            const md = await layoutMetadata();
            expect(ogUrl(md)).toBe(OG_IMAGE_URL);
            expect(twUrl(md)).toBe(OG_IMAGE_URL);
            expect(md.title).toContain(titlePart);
            expect(String(md.metadataBase)).toBe(`https://${host}/`);
            // relative URL + per-host metadataBase => same-host absolute image URL (no cross-host redirect)
            expect(new URL(ogUrl(md), md.metadataBase).href).toBe(`https://${host}${OG_IMAGE_URL}`);
        });
    }

    it("apply / invite page", () => {
        expect(ogUrl(applyMetadata)).toBe(OG_IMAGE_URL);
        expect(twUrl(applyMetadata)).toBe(OG_IMAGE_URL);
        expect(applyMetadata.title).toBe("Talentgram Agency");
    });

    it("talent submission page (/submit/<slug>) — no per-slug image route any more", async () => {
        const md = await submitMetadata({ params: Promise.resolve({ slug: "tyaani-jewellery-abc" }) });
        expect(ogUrl(md)).toBe(OG_IMAGE_URL);
        expect(twUrl(md)).toBe(OG_IMAGE_URL);
        expect(ogUrl(md)).not.toContain("tyaani-jewellery-abc");
        expect(md.title).toBe("Talentgram Agency");
        expect(md.description).toBe("India - UAE");
    });

    it("client review / generated / individual-talent link (/l/<slug>): canonical image, default title", async () => {
        vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ link_type: "client" }) })));
        const md = await linksMetadata({ params: Promise.resolve({ slug: "talentgram-x-bkb-123" }) });
        expect(ogUrl(md)).toBe(OG_IMAGE_URL);
        expect(twUrl(md)).toBe(OG_IMAGE_URL);
        expect(ogUrl(md)).not.toContain("talentgram-x-bkb-123");
        expect(md.title).toBe("Talentgram Agency");
        expect(md.openGraph.title).toBe("Talentgram Agency");
        expect(md.description).toBe("India - UAE");
        vi.unstubAllGlobals();
    });

    it("roster link (/l/<slug>): canonical image AND its dynamic title/description preserved", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => ({ ok: true, json: async () => ({ link_type: "roster", title: "Talentgram X Pepsi", subtitle: "Summer cast" }) })),
        );
        const md = await linksMetadata({ params: Promise.resolve({ slug: "talentgram-x-pepsi-roster" }) });
        expect(ogUrl(md)).toBe(OG_IMAGE_URL);
        expect(twUrl(md)).toBe(OG_IMAGE_URL);
        expect(md.title).toBe("Talentgram X Pepsi");
        expect(md.openGraph.title).toBe("Talentgram X Pepsi");
        expect(md.twitter.title).toBe("Talentgram X Pepsi");
        expect(md.description).toBe("Summer cast");
        vi.unstubAllGlobals();
    });

    it("a link whose meta probe fails still gets the canonical image (never throws)", async () => {
        vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network"); }));
        const md = await linksMetadata({ params: Promise.resolve({ slug: "x" }) });
        expect(ogUrl(md)).toBe(OG_IMAGE_URL);
        expect(md.title).toBe("Talentgram Agency");
        vi.unstubAllGlobals();
    });

    it("talent media / download page: private + noindex, defines no image of its own (inherits the layout's canonical one)", () => {
        expect(talentMediaMetadata.robots).toMatchObject({ index: false, follow: false });
        expect(talentMediaMetadata.openGraph).toBeUndefined();
        expect(talentMediaMetadata.twitter).toBeUndefined();
    });

    it("legacy /og-image URL (may be cached by WhatsApp) redirects to the canonical logo", () => {
        const res = legacyOgImage({ url: "https://www.talentgramagency.com/og-image?v=123" });
        expect(res.status).toBe(307);
        expect(new URL(res.headers.get("location")).pathname).toBe(OG_IMAGE_PATH);
    });
});

describe("no second, different preview image can come back", () => {
    it("the per-link / per-project dynamic opengraph-image routes and the embedded old logo are gone", () => {
        for (const rel of [
            "src/app/(links)/l/[slug]/opengraph-image.jsx",
            "src/app/(submit)/submit/[slug]/opengraph-image.jsx",
            "src/lib/logoBlackBase64.js",
        ]) {
            expect(fs.existsSync(path.join(ROOT, rel)), rel).toBe(false);
        }
    });
});
