import { describe, it, expect } from "vitest";
import { IMAGE_URL } from "@/lib/api";

// Cloudinary rearchitecture P5 (RULE #2): the canonical uploaded image is
// delivered DIRECTLY. `f_auto` is inserted ONLY for a HEIC/HEIF source (which
// only Safari can decode). Web-safe formats get zero transformation, so the
// full-resolution AVIF re-encode line (`extra_avif_mp_encoding`) disappears.

const C = "https://res.cloudinary.com/talentgram/image/upload";

describe("IMAGE_URL — P5 canonical image delivery", () => {
    it("returns web-safe JPEG/PNG/WebP Cloudinary URLs untouched", () => {
        for (const ext of ["jpg", "jpeg", "png", "webp"]) {
            const url = `${C}/v1712/talentgram/talents/t1/abc.${ext}`;
            expect(IMAGE_URL({ url })).toBe(url);
            expect(IMAGE_URL(url)).toBe(url);
        }
    });

    it("never inserts f_auto / q_auto / f_avif for a web-safe image", () => {
        const url = `${C}/v1/talentgram/x/y.jpg`;
        const out = IMAGE_URL({ url });
        expect(out).not.toMatch(/f_auto|q_auto|f_avif|dpr_/);
    });

    it("adds a single canonical f_auto segment for a .heic URL", () => {
        const url = `${C}/v1/talentgram/talents/t1/photo.heic`;
        const out = IMAGE_URL({ url });
        expect(out).toBe(`${C}/f_auto/v1/talentgram/talents/t1/photo.heic`);
        // format negotiation only — no q_auto / width / dpr
        expect(out).not.toMatch(/q_auto|w_\d|dpr_/);
    });

    it("detects HEIC from content_type and original_filename too", () => {
        const base = `${C}/v1/talentgram/x/y`; // extensionless public id
        expect(IMAGE_URL({ url: base, content_type: "image/heic" })).toContain("/f_auto/");
        expect(IMAGE_URL({ url: base, original_filename: "IMG_2201.HEIF" })).toContain("/f_auto/");
        expect(IMAGE_URL({ url: base, format: "heic" })).toContain("/f_auto/");
        // no HEIC signal -> untouched
        expect(IMAGE_URL({ url: base })).toBe(base);
    });

    it("is idempotent and leaves non-Cloudinary URLs alone", () => {
        const ext = "https://cdn.example.com/a/b.heic";
        expect(IMAGE_URL(ext)).toBe(ext);
        const already = `${C}/f_auto/v1/talentgram/x/y.heic`;
        expect(IMAGE_URL(already)).toBe(already);
    });

    it("handles empty / null input", () => {
        expect(IMAGE_URL(null)).toBe("");
        expect(IMAGE_URL({})).toBe("");
        expect(IMAGE_URL("")).toBe("");
    });
});
