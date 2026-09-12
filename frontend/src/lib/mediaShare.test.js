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

describe("mediaShare.js — mixed image+video fallback (found live on a real Android device: canShare() rejects the COMBINED batch even though each type alone is accepted)", () => {
    beforeEach(() => {
        api.get.mockReset();
        api.post.mockReset();
        api.post.mockResolvedValue({ data: { share_id: "sh_1" } });
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    // Simulates the exact reported device behaviour: canShare() returns true
    // for an all-image or all-video array, but false the moment BOTH a
    // video/* and an image/* file appear in the same array.
    function stubMixedRejectingCanShare(shareMock) {
        vi.stubGlobal("navigator", {
            ...navigator,
            share: shareMock,
            canShare: vi.fn(({ files }) => {
                const types = new Set(files.map((f) => f.type.split("/")[0]));
                return types.size <= 1;
            }),
            userAgent: "android-chrome-agent",
        });
    }

    it("all three videos alone still share as real media in one operation (must stay unaffected)", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "video/mp4" }) });
        stubMixedRejectingCanShare(vi.fn().mockResolvedValue(undefined));
        const items = [1, 2, 3].map((n) => ({ id: `t${n}`, name: `Take ${n}`, type: "video", fileUrl: `https://x/t${n}.mp4`, filename: `Harshita - Take ${n}` }));
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X\n\nAudition Take: Take 1\nAudition Take: Take 2\nAudition Take: Take 3",
            allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(3);
        expect(navigator.share.mock.calls[0][0].files).toHaveLength(3);
    });

    it("images alone still share as real media in one operation (must stay unaffected)", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        stubMixedRejectingCanShare(vi.fn().mockResolvedValue(undefined));
        const items = [1, 2].map((n) => ({ id: `img${n}`, name: `Portfolio Image ${n}`, type: "image", fileUrl: `https://x/i${n}.jpg`, filename: `Harshita - Portfolio Image ${n}` }));
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(2);
    });

    it("3 videos + 1 image: combined canShare() is rejected, so it splits into two real native file shares — videos first, sent immediately; images reported as remaining, NOT silently dropped", async () => {
        api.get.mockImplementation((url) =>
            Promise.resolve({ status: 200, data: new Blob(["x"], { type: url.includes("img") ? "image/jpeg" : "video/mp4" }) })
        );
        const shareMock = vi.fn().mockResolvedValue(undefined);
        stubMixedRejectingCanShare(shareMock);
        const items = [
            { id: "t1", name: "Take 1", type: "video", fileUrl: "https://x/t1.mp4", filename: "Harshita - Take 1" },
            { id: "t2", name: "Take 2", type: "video", fileUrl: "https://x/t2.mp4", filename: "Harshita - Take 2" },
            { id: "t3", name: "Take 3", type: "video", fileUrl: "https://x/t3.mp4", filename: "Harshita - Take 3" },
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Harshita - Portfolio Image 1" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X\n\nAudition Take: Take 1\nAudition Take: Take 2\nAudition Take: Take 3",
            allowFiles: true, sessionId: "sess1",
        });

        expect(res.method).toBe("native_file_share_split");
        expect(res.sentType).toBe("video");
        expect(res.sentCount).toBe(3);
        expect(res.remainingType).toBe("image");
        expect(res.remainingCount).toBe(1);

        // Exactly one native share call happened (the video batch) — the
        // image was NOT silently attempted or dropped, only reported as
        // remaining for an explicit follow-up (see ClientView.jsx).
        expect(shareMock).toHaveBeenCalledTimes(1);
        const sentFiles = shareMock.mock.calls[0][0].files;
        expect(sentFiles).toHaveLength(3);
        expect(sentFiles.every((f) => f.type === "video/mp4")).toBe(true);
    });

    it("3 videos + 2 images: the follow-up share (image batch) sends the correct remaining files when invoked as its own call — proves the caller can simply re-invoke shareMediaViaWhatsApp with the remaining items, no special resume API needed", async () => {
        api.get.mockResolvedValue({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        const shareMock = vi.fn().mockResolvedValue(undefined);
        stubMixedRejectingCanShare(shareMock);
        const remainingImageItems = [
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Harshita - Portfolio Image 1" },
            { id: "img2", name: "Portfolio Image 2", type: "image", fileUrl: "https://x/img2.jpg", filename: "Harshita - Portfolio Image 2" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items: remainingImageItems,
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(2);
    });

    it("if EITHER homogeneous subset also fails canShare(), falls through to the existing combined secure-link fallback — no new failure mode", async () => {
        api.get.mockImplementation((url) =>
            Promise.resolve({ status: 200, data: new Blob(["x"], { type: url.includes("img") ? "image/jpeg" : "video/mp4" }) })
        );
        vi.stubGlobal("navigator", {
            ...navigator,
            share: vi.fn().mockResolvedValue(undefined),
            canShare: vi.fn().mockReturnValue(false), // rejects everything, combined AND each subset
            userAgent: "android-chrome-agent",
        });
        const items = [
            { id: "t1", name: "Take 1", type: "video", fileUrl: "https://x/t1.mp4", filename: "Harshita - Take 1" },
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Harshita - Portfolio Image 1" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X\n\nAudition Take: Take 1", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("whatsapp_link_share");
        // Falls back to ONE combined secure-link message for everything —
        // the existing, already-working behaviour, completely unchanged.
        const call = navigator.share.mock.calls[0][0];
        expect(call.text).toContain("Take 1");
        expect(call.text).toContain("Portfolio Image 1");
    });

    it("if the platform genuinely supports the combined mixed batch (canShare() returns true), it is still sent as ONE native share — no unnecessary split", async () => {
        api.get.mockImplementation((url) =>
            Promise.resolve({ status: 200, data: new Blob(["x"], { type: url.includes("img") ? "image/jpeg" : "video/mp4" }) })
        );
        const shareMock = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal("navigator", {
            ...navigator,
            share: shareMock,
            canShare: vi.fn().mockReturnValue(true), // this device/browser DOES support mixed combined sharing
            userAgent: "android-chrome-agent",
        });
        const items = [
            { id: "t1", name: "Take 1", type: "video", fileUrl: "https://x/t1.mp4", filename: "Harshita - Take 1" },
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Harshita - Portfolio Image 1" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X\n\nAudition Take: Take 1", allowFiles: true, sessionId: "sess1",
        });
        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(2);
        expect(shareMock).toHaveBeenCalledTimes(1);
    });
});

describe("mediaShare.js — partial-preparation reuse (the real fix: a mixed selection where videos are pre-warmed on talent-open but images only start preparing on selection must not discard the already-ready videos and re-fetch everything)", () => {
    beforeEach(() => {
        api.get.mockReset();
        api.post.mockReset();
        api.post.mockResolvedValue({ data: { share_id: "sh_1" } });
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("never re-fetches an item that is already a real File in preparedFiles — only the missing ones hit the network", async () => {
        // If api.get is called for the ALREADY-prepared video, the old
        // all-or-nothing behaviour has regressed.
        api.get.mockImplementation((url) => {
            if (url.includes("v1")) throw new Error("must not re-fetch an already-prepared item");
            return Promise.resolve({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) });
        });
        vi.stubGlobal("navigator", {
            ...navigator,
            share: vi.fn().mockResolvedValue(undefined),
            canShare: vi.fn().mockReturnValue(true),
            userAgent: "android-chrome-agent",
        });
        const preparedVideo = new File(["x"], "Harshita - Take 1.mp4", { type: "video/mp4" });
        const preparedFiles = new Map([["v1", preparedVideo]]);
        const items = [
            { id: "v1", name: "Take 1", type: "video", fileUrl: "https://x/v1.mp4", filename: "Harshita - Take 1" },
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Harshita - Portfolio Image 1" },
        ];
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X\n\nAudition Take: Take 1",
            allowFiles: true, sessionId: "sess1", preparedFiles,
        });
        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(2);
        // The pre-prepared video object itself was used — not a re-fetched copy.
        expect(navigator.share.mock.calls[0][0].files).toContain(preparedVideo);
    });

    it("realistic latency: with 3 pre-warmed videos ready instantly and 2 images each taking real time to fetch, navigator.share() is called almost immediately after the missing images resolve — not after a full 5-item re-fetch", async () => {
        const timeline = [];
        const record = (label) => timeline.push({ label, t: Date.now() });
        api.get.mockImplementation((url) => {
            record(`fetch:${url}`);
            // Simulate a realistic mobile-network image fetch delay.
            return new Promise((resolve) =>
                setTimeout(() => resolve({ status: 200, data: new Blob(["x"], { type: "image/jpeg" }) }), 40)
            );
        });
        const shareMock = vi.fn().mockImplementation(() => {
            record("navigator.share called");
            return Promise.resolve(undefined);
        });
        vi.stubGlobal("navigator", {
            ...navigator, share: shareMock, canShare: vi.fn().mockReturnValue(true), userAgent: "android-chrome-agent",
        });
        const preparedFiles = new Map([
            ["v1", new File(["x"], "Take 1.mp4", { type: "video/mp4" })],
            ["v2", new File(["x"], "Take 2.mp4", { type: "video/mp4" })],
            ["v3", new File(["x"], "Introduction.mp4", { type: "video/mp4" })],
        ]);
        const items = [
            { id: "v1", name: "Take 1", type: "video", fileUrl: "https://x/v1.mp4", filename: "Take 1" },
            { id: "v2", name: "Take 2", type: "video", fileUrl: "https://x/v2.mp4", filename: "Take 2" },
            { id: "v3", name: "Introduction", type: "video", fileUrl: "https://x/v3.mp4", filename: "Introduction" },
            { id: "img1", name: "Portfolio Image 1", type: "image", fileUrl: "https://x/img1.jpg", filename: "Portfolio Image 1" },
            { id: "img2", name: "Portfolio Image 2", type: "image", fileUrl: "https://x/img2.jpg", filename: "Portfolio Image 2" },
        ];
        const start = Date.now();
        const res = await shareMediaViaWhatsApp({
            slug: "s1", talentId: "t1", talentName: "Harshita", items,
            caption: "Harshita — Project X", allowFiles: true, sessionId: "sess1", preparedFiles,
        });
        const elapsed = Date.now() - start;

        expect(res.method).toBe("native_file_share");
        expect(res.count).toBe(5);
        // Only the 2 missing images were fetched — the 3 ready videos never hit the network.
        expect(api.get).toHaveBeenCalledTimes(2);
        // Both missing fetches ran in PARALLEL (not sequentially behind each
        // other or behind the ready videos) — total time close to one
        // fetch's delay (~40ms), not stacked (~80ms+) or inflated by
        // needlessly re-fetching the 3 already-ready videos.
        expect(elapsed).toBeLessThan(120);
    });
});
