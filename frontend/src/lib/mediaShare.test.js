import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { shareMediaViaWhatsApp } from "./mediaShare";

vi.mock("@/lib/api", () => ({
    api: { get: vi.fn(), post: vi.fn() },
    getViewerToken: vi.fn(() => "fake-token"),
    getSubdomainUrl: vi.fn((sub) => `https://${sub}.talentgramagency.com`),
}));

import { api } from "@/lib/api";

describe("mediaShare.js — never leaks a raw filename via its own logic", () => {
    it("has no reference to original_filename anywhere in this module (the caller — ClientView.jsx — always computes the clean name before calling in)", () => {
        // This is a structural guard: mediaShare.js must keep receiving
        // filenames already computed by its callers, never reaching into a
        // media object's own original_filename field itself.
        const src = shareMediaViaWhatsApp.toString();
        expect(src).not.toMatch(/original_filename/);
    });
});

describe("mediaShare.js — formText threading (Include Talent Details Form)", () => {
    beforeEach(() => {
        api.get.mockReset();
        api.post.mockReset();
        api.post.mockResolvedValue({ data: { share_id: "sh_1" } });
        // Stub only the specific properties this suite needs — replacing the
        // whole navigator/window object (vi.stubGlobal with a bare object)
        // strips everything jsdom provides and leaks into OTHER test files
        // sharing this worker's environment.
        vi.stubGlobal("navigator", {
            ...navigator,
            share: vi.fn().mockResolvedValue(undefined),
            canShare: vi.fn().mockReturnValue(true),
            userAgent: "test-agent",
        });
        vi.spyOn(window, "open").mockReturnValue({ location: {} });
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("form-only share (no items) sends a text-only native share containing the form text", async () => {
        const res = await shareMediaViaWhatsApp({
            slug: "s1",
            talentId: "t1",
            talentName: "Harshita",
            items: [],
            caption: "Harshita — Project X",
            formText: "Name: Harshita\nAge: 29",
            allowFiles: true,
            sessionId: "sess1",
        });
        expect(res.aborted).not.toBe(true);
        expect(navigator.share).toHaveBeenCalledTimes(1);
        const call = navigator.share.mock.calls[0][0];
        expect(call.text).toContain("Name: Harshita\nAge: 29");
        // No files key is expected for a form-only share (the native
        // file-share branch must never be attempted with an empty items list).
        expect(call.files).toBeUndefined();
    });

    it("media-only share (no formText) is unaffected — caption alone, no form segment", async () => {
        const fakeFile = new File(["x"], "Harshita - Introduction", { type: "video/mp4" });
        const preparedFiles = new Map([["m1", fakeFile]]);
        const res = await shareMediaViaWhatsApp({
            slug: "s1",
            talentId: "t1",
            talentName: "Harshita",
            items: [{ id: "m1", name: "Introduction", type: "video", fileUrl: "https://x/v.mp4", filename: "Harshita - Introduction" }],
            caption: "Harshita — Project X\n\nIntroduction Video",
            allowFiles: true,
            sessionId: "sess1",
            preparedFiles,
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.text).toBe("Harshita — Project X\n\nIntroduction Video");
        expect(call.files).toEqual([fakeFile]);
    });

    it("media+form share appends the form text as its own segment after the caption", async () => {
        const fakeFile = new File(["x"], "Harshita - Introduction", { type: "video/mp4" });
        const preparedFiles = new Map([["m1", fakeFile]]);
        const res = await shareMediaViaWhatsApp({
            slug: "s1",
            talentId: "t1",
            talentName: "Harshita",
            items: [{ id: "m1", name: "Introduction", type: "video", fileUrl: "https://x/v.mp4", filename: "Harshita - Introduction" }],
            caption: "Harshita — Project X\n\nIntroduction Video",
            formText: "Name: Harshita\nAge: 29",
            allowFiles: true,
            sessionId: "sess1",
            preparedFiles,
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.text).toBe("Harshita — Project X\n\nIntroduction Video\n\nName: Harshita\nAge: 29");
    });

    it("link-fallback (no native share) still includes formText in the wa.me message", async () => {
        // No navigator.share/canShare at all — simulates desktop.
        vi.stubGlobal("navigator", { ...navigator, share: undefined, canShare: undefined, userAgent: "desktop-agent" });
        const winMock = { location: {} };
        vi.spyOn(window, "open").mockReturnValue(winMock);
        const res = await shareMediaViaWhatsApp({
            slug: "s1",
            talentId: "t1",
            talentName: "Harshita",
            items: [],
            formText: "Name: Harshita\nAge: 29",
            allowFiles: true,
            sessionId: "sess1",
        });
        expect(res.method).toBe("whatsapp_link_share");
        expect(res.via).toBe("wa_me");
        expect(winMock.location.href).toContain(encodeURIComponent("Name: Harshita"));
    });
});

describe("mediaShare.js — actual runtime File payload (fetch-fallback path, no preparedFiles)", () => {
    // These tests exercise the REAL fetch→Blob→File construction inside
    // urlToFileTraced (not just a structural grep) — api.get is mocked to
    // return a blob, and we inspect the actual File objects handed to
    // navigator.share(). Every filename here is exactly what ClientView.jsx
    // computes today (talent name + label, never original_filename).
    beforeEach(() => {
        api.get.mockReset();
        api.post.mockReset();
        api.post.mockResolvedValue({ data: { share_id: "sh_1" } });
        vi.stubGlobal("navigator", {
            ...navigator,
            share: vi.fn().mockResolvedValue(undefined),
            canShare: vi.fn().mockReturnValue(true),
            userAgent: "test-agent",
        });
        vi.spyOn(window, "open").mockReturnValue({ location: {} });
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("single image: File.name is the clean generated name + extension, no caption line in text", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["fake-bytes"], { type: "image/jpeg" }) });
        const res = await shareMediaViaWhatsApp({
            slug: "s1",
            talentId: "t1",
            talentName: "Harshita",
            items: [{ id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/i1.jpg", filename: "Harshita - Portfolio Image 1" }],
            caption: "Harshita — Project X", // images never contribute a caption line — see ClientView's shareableMedia builder
            allowFiles: true,
            sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.files).toHaveLength(1);
        expect(call.files[0].name).toBe("Harshita - Portfolio Image 1.jpg");
        expect(call.files[0].name).not.toMatch(/IMG_|DSC_|\.MOV$|^\d{8}_\d{6}/); // never a raw camera-style name
        // The text is only the header — no per-image line, no filename anywhere in it.
        expect(call.text).toBe("Harshita — Project X");
        expect(call.text).not.toContain(".jpg");
    });

    it("multiple images: every File gets its own clean name, text still has zero image lines", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        const items = [
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/i1.jpg", filename: "Harshita - Portfolio Image 1" },
            { id: "img2", name: "Portfolio Image 2", type: "image", fileUrl: "https://x/i2.jpg", filename: "Harshita - Portfolio Image 2" },
            { id: "img3", name: "Portfolio Image 3", type: "image", fileUrl: "https://x/i3.jpg", filename: "Harshita - Portfolio Image 3" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.files.map((f) => f.name)).toEqual([
            "Harshita - Portfolio Image 1.jpg",
            "Harshita - Portfolio Image 2.jpg",
            "Harshita - Portfolio Image 3.jpg",
        ]);
        expect(call.text).toBe("Harshita — Project X"); // no "Portfolio Image N" lines at all
    });

    it("single video (introduction): File.name is clean, text has exactly one clean caption line", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "video/mp4" }) });
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita",
            items: [{ id: "v1", name: "Introduction", type: "video", fileUrl: "https://x/v1.mp4", filename: "Harshita - Introduction" }],
            caption: "Harshita — Project X\n\nIntroduction Video",
            allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.files[0].name).toBe("Harshita - Introduction.mp4");
        expect(call.text).toBe("Harshita — Project X\n\nIntroduction Video");
    });

    it("audition take: File.name and caption both use the clean take label, never a raw filename", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "video/mp4" }) });
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita",
            items: [{ id: "t1id", name: "Take 2", type: "video", fileUrl: "https://x/t2.mp4", filename: "Harshita - Take 2" }],
            caption: "Harshita — Project X\n\nAudition Take: Take 2",
            allowFiles: true, sessionId: "sess1",
        });
        const call = navigator.share.mock.calls[0][0];
        expect(call.files[0].name).toBe("Harshita - Take 2.mp4");
        expect(call.text).toBe("Harshita — Project X\n\nAudition Take: Take 2");
    });

    it("mixed images + video: images contribute zero text lines, the video's caption is the only content line, all filenames clean", async () => {
        api.get.mockImplementation((url) =>
            Promise.resolve({
                status: 200,
                data: new Blob(["x"], { type: url.includes("v1") ? "video/mp4" : "image/jpeg" }),
            })
        );
        const items = [
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/i1.jpg", filename: "Harshita - Portfolio Image 1" },
            { id: "img2", name: "Portfolio Image 2", type: "image", fileUrl: "https://x/i2.jpg", filename: "Harshita - Portfolio Image 2" },
            { id: "v1", name: "Take 1", type: "video", fileUrl: "https://x/v1.mp4", filename: "Harshita - Take 1" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            // Matches ClientView's runShare(): only the video contributes a caption line.
            caption: "Harshita — Project X\n\nAudition Take: Take 1",
            allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        const call = navigator.share.mock.calls[0][0];
        expect(call.files).toHaveLength(3);
        expect(call.files.map((f) => f.name)).toEqual([
            "Harshita - Portfolio Image 1.jpg",
            "Harshita - Portfolio Image 2.jpg",
            "Harshita - Take 1.mp4",
        ]);
        // Exactly one content line beyond the header — from the video, not the images.
        expect(call.text).toBe("Harshita — Project X\n\nAudition Take: Take 1");
        const contentLines = call.text.split("\n\n");
        expect(contentLines).toHaveLength(2); // header, then the single video caption
    });

    it("cancelling the share sheet (AbortError) is reported as aborted, not an error, and never falls back to links", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        navigator.share = vi.fn().mockRejectedValue(Object.assign(new Error("cancelled"), { name: "AbortError" }));
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita",
            items: [{ id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/i1.jpg", filename: "Harshita - Portfolio Image 1" }],
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1",
        });
        expect(res.aborted).toBe(true);
        expect(api.post).not.toHaveBeenCalled(); // no share-log, no link-mint — a cancel is a true no-op
    });

    it("navigator.canShare({files}) === false falls back to the secure-link path, never claims files were attached", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        navigator.canShare = vi.fn().mockReturnValue(false);
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita",
            items: [{ id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/i1.jpg", filename: "Harshita - Portfolio Image 1" }],
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("whatsapp_link_share");
        expect(res.via).toBe("native_sheet");
        // The fallback message references the media by its clean label, not a filename.
        const call = navigator.share.mock.calls[0][0];
        expect(call.text).toContain("Portfolio Image 1");
    });
});
