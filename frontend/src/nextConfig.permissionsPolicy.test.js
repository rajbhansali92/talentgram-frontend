// @vitest-environment node
import { describe, it, expect } from "vitest";
import { createRequire } from "node:module";

// 2026-10-20: the assistant said "microphone is blocked" although Chrome's site setting was Allow, because every page was
// served with `Permissions-Policy: microphone=()`, which switches the microphone off for the document itself. The browser then
// reports it as denied (Permissions API, getUserMedia, SpeechRecognition) whatever the user allows. This pins the header.
const require = createRequire(import.meta.url);
const nextConfig = require("../next.config.js");

async function policyFor(path) {
    const rules = await nextConfig.headers();
    const hit = rules.filter((r) => new RegExp(`^${r.source.replace(/\(\.\*\)/g, ".*").replace(/:path\*/g, ".*")}$`).test(path));
    const all = hit.flatMap((r) => r.headers).filter((h) => h.key === "Permissions-Policy");
    return all[all.length - 1]?.value;
}

describe("Permissions-Policy header", () => {
    it.each(["/", "/admin", "/admin/projects/abc/submissions", "/submit/some-project", "/l/abc"])("%s lets the page's own origin use the microphone", async (path) => {
        const v = await policyFor(path);
        expect(v).toBeTruthy();
        expect(v).toMatch(/microphone=\(self\)/);
        expect(v).not.toMatch(/microphone=\(\)/);
    });

    it("never grants the microphone to other origins (no wildcard, no third-party origin)", async () => {
        const v = await policyFor("/admin");
        const mic = v.match(/microphone=\(([^)]*)\)/)[1];
        expect(mic).toBe("self");
    });

    it("keeps camera and geolocation switched off", async () => {
        const v = await policyFor("/admin");
        expect(v).toMatch(/camera=\(\)/);
        expect(v).toMatch(/geolocation=\(\)/);
    });

    it("is sent exactly once per response (a duplicate rule could re-block the microphone)", async () => {
        const rules = await nextConfig.headers();
        const count = rules.flatMap((r) => r.headers).filter((h) => h.key === "Permissions-Policy").length;
        expect(count).toBe(1);
    });
});
