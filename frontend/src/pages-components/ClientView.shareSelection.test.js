import { describe, it, expect } from "vitest";
import { isShareSelectionTypeAllowed } from "./ClientView";

// Product rule (rolled back from the earlier mixed-media split-batch
// attempt): one WhatsApp share is all-images or all-videos, never both.
// toggleShareSel, selectAllOfType, and the per-tile SelectCheck in
// ClientView.jsx all defer to this single pure function to decide whether a
// selection attempt is allowed — these tests cover every input combination
// directly, without mounting the (large) TalentDetail component, so the one
// rule that keeps a mixed array from ever reaching navigator.share() has
// real, isolated coverage.
describe("ClientView.jsx — isShareSelectionTypeAllowed (same-type-only share selection rule)", () => {
    it("allows selecting an image when nothing is selected yet", () => {
        expect(isShareSelectionTypeAllowed("image", null)).toBe(true);
    });

    it("allows selecting a video when nothing is selected yet", () => {
        expect(isShareSelectionTypeAllowed("video", null)).toBe(true);
    });

    it("allows selecting another image when images are already selected", () => {
        expect(isShareSelectionTypeAllowed("image", "image")).toBe(true);
    });

    it("allows selecting another video when videos are already selected", () => {
        expect(isShareSelectionTypeAllowed("video", "video")).toBe(true);
    });

    it("blocks selecting a video when images are already selected", () => {
        expect(isShareSelectionTypeAllowed("video", "image")).toBe(false);
    });

    it("blocks selecting an image when videos are already selected", () => {
        expect(isShareSelectionTypeAllowed("image", "video")).toBe(false);
    });
});
