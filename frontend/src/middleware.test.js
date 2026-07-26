import { describe, it, expect } from "vitest";
import { NextRequest } from "next/server";
import { middleware } from "./middleware";

// P3 fix: /apply and /submit/* on the bare production hostname (apex already
// 308s to www at the DNS/platform level, so this is really the www case) must
// canonical-redirect to their real subdomain BEFORE any upload session can
// begin — Cloudflare R2's CORS allowlist only permits apply./submit., so a
// video upload started from here fails with "R2 upload network error" while
// the rest of the page (images, which don't go direct-to-R2) works fine.
function reqFor(host, path) {
    return new NextRequest(`https://${host}${path}`, { headers: { host } });
}

describe("middleware — canonical apply/submit redirect", () => {
    it("redirects www.talentgramagency.com/apply to the apply subdomain", () => {
        const res = middleware(reqFor("www.talentgramagency.com", "/apply"));
        expect(res.status).toBe(308);
        expect(res.headers.get("location")).toBe("https://apply.talentgramagency.com/apply");
    });

    it("redirects a nested /apply path and preserves the query string", () => {
        const res = middleware(reqFor("www.talentgramagency.com", "/apply?profile=abc123"));
        expect(res.status).toBe(308);
        expect(res.headers.get("location")).toBe(
            "https://apply.talentgramagency.com/apply?profile=abc123"
        );
    });

    it("redirects talentgramagency.com/submit/{slug} to the submit subdomain", () => {
        const res = middleware(reqFor("talentgramagency.com", "/submit/my-project-slug"));
        expect(res.status).toBe(308);
        expect(res.headers.get("location")).toBe(
            "https://submit.talentgramagency.com/submit/my-project-slug"
        );
    });

    it("does not touch other paths on the apex/www host (e.g. the landing page)", () => {
        const res = middleware(reqFor("www.talentgramagency.com", "/"));
        expect(res.status).not.toBe(308);
    });

    it("is a no-op on the real apply subdomain (no redirect loop)", () => {
        const res = middleware(reqFor("apply.talentgramagency.com", "/apply"));
        expect(res.status).not.toBe(308);
    });

    it("is a no-op on the real submit subdomain (no redirect loop)", () => {
        const res = middleware(reqFor("submit.talentgramagency.com", "/submit/my-project-slug"));
        expect(res.status).not.toBe(308);
    });

    it("does not redirect on localhost — local dev must be unaffected", () => {
        const res = middleware(reqFor("localhost:3000", "/apply"));
        expect(res.status).not.toBe(308);
    });

    it("does not redirect on a Vercel preview host", () => {
        const res = middleware(
            reqFor("talentgram-frontend-mdzvsmsox-talentgram-s-projects.vercel.app", "/apply")
        );
        expect(res.status).not.toBe(308);
    });
});
