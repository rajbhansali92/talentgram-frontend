import { describe, it, expect } from "vitest";
import { arrowStep, clampIndex, neighbourImages, pad2, swipeStep } from "./rosterCarousel";

describe("clampIndex", () => {
    it("stays inside [0, length-1]", () => {
        expect(clampIndex(-3, 5)).toBe(0);
        expect(clampIndex(2, 5)).toBe(2);
        expect(clampIndex(9, 5)).toBe(4);
        expect(clampIndex(1.9, 5)).toBe(1);
        expect(clampIndex(NaN, 5)).toBe(0);
        expect(clampIndex(3, 0)).toBe(0);
    });
});

describe("swipeStep", () => {
    it("left swipe = next image, right swipe = previous", () => {
        expect(swipeStep(-80, 5)).toBe(1);
        expect(swipeStep(80, -5)).toBe(-1);
    });
    it("ignores short moves and mostly-vertical drags, so page scrolling never changes an image", () => {
        expect(swipeStep(20, 0)).toBe(0);
        expect(swipeStep(-50, 60)).toBe(0);
        expect(swipeStep(-60, 50)).toBe(0);
        expect(swipeStep(0, -200)).toBe(0);
        expect(swipeStep(-10, 400)).toBe(0);
    });
});

describe("arrowStep", () => {
    it("only ←/→ change the image; nothing else, and never with a modifier", () => {
        expect(arrowStep({ key: "ArrowRight" })).toBe(1);
        expect(arrowStep({ key: "ArrowLeft" })).toBe(-1);
        for (const key of ["ArrowUp", "ArrowDown", "PageDown", "PageUp", "Home", "End", " ", "a"]) expect(arrowStep({ key })).toBe(0);
        for (const m of ["metaKey", "ctrlKey", "altKey", "shiftKey"]) expect(arrowStep({ key: "ArrowRight", [m]: true })).toBe(0);
    });
});

describe("neighbourImages", () => {
    const imgs = ["a", "b", "c"].map((id) => ({ id }));
    it("is the next and previous image of the SAME talent only", () => {
        expect(neighbourImages(imgs, 1).map((x) => x.id)).toEqual(["c", "a"]);
        expect(neighbourImages(imgs, 0).map((x) => x.id)).toEqual(["b"]);
        expect(neighbourImages(imgs, 2).map((x) => x.id)).toEqual(["b"]);
        expect(neighbourImages([{ id: "only" }], 0)).toEqual([]);
        expect(neighbourImages([], 0)).toEqual([]);
    });
});

describe("pad2", () => { it("zero-pads", () => { expect(pad2(3)).toBe("03"); expect(pad2(12)).toBe("12"); }); });
