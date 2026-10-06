import { describe, it, expect } from "vitest";
import { talentMediaCounts } from "./talentMediaCounts";

const img = (cat = "indian") => ({ category: cat, content_type: "image/jpeg" });
const vid = () => ({ category: "video", content_type: "video/mp4" });

describe("talentMediaCounts (Global Talent list row)", () => {
    it("uses the server-computed counters (the list never receives media[])", () => {
        expect(talentMediaCounts({ image_count: 6, video_count: 1, media_count: 7 })).toEqual({ imageCount: 6, videoCount: 1 });
    });

    it("talent WITH an Introduction Video -> video 1", () => {
        expect(talentMediaCounts({ image_count: 6, video_count: 1 }).videoCount).toBe(1);
    });
    it("talent WITHOUT one -> video 0, and images stay correct", () => {
        expect(talentMediaCounts({ image_count: 2, video_count: 0, media_count: 2 })).toEqual({ imageCount: 2, videoCount: 0 });
    });
    it("images + video -> images unchanged (video is not double counted), video 1", () => {
        const c = talentMediaCounts({ image_count: 16, video_count: 1, media_count: 17 });
        expect(c).toEqual({ imageCount: 16, videoCount: 1 });
        expect(c.imageCount + c.videoCount).toBe(17);
    });
    it("a talent with nothing shows 0 / 0 (no false video count)", () => {
        expect(talentMediaCounts({ image_count: 0, video_count: 0, media_count: 0 })).toEqual({ imageCount: 0, videoCount: 0 });
        expect(talentMediaCounts({})).toEqual({ imageCount: 0, videoCount: 0 });
        expect(talentMediaCounts(null)).toEqual({ imageCount: 0, videoCount: 0 });
    });

    it("derives the same numbers from media[] when the counters are absent (hydrated talents)", () => {
        const t = { media: [img(), img("western"), img("portfolio"), vid()] };
        expect(talentMediaCounts(t)).toEqual({ imageCount: 3, videoCount: 1 });
        expect(talentMediaCounts({ media: [img(), img()] })).toEqual({ imageCount: 2, videoCount: 0 });
    });
    it("only the canonical Introduction Video (category 'video') counts — not any file that happens to be a video", () => {
        const t = { media: [img(), { category: "portfolio", content_type: "video/quicktime" }] };
        expect(talentMediaCounts(t).videoCount).toBe(0);
    });
    it("more than one video-category item still reads as one Introduction Video", () => {
        expect(talentMediaCounts({ media: [vid(), vid()] }).videoCount).toBe(1);
        expect(talentMediaCounts({ image_count: 3, video_count: 1 }).videoCount).toBe(1);
    });
    it("falls back to the stored total only when nothing else is known (previous behaviour)", () => {
        expect(talentMediaCounts({ media_count: 5 })).toEqual({ imageCount: 5, videoCount: 0 });
    });
});
