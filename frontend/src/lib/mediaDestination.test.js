import { describe, it, expect } from "vitest";
import { destinationForCategory, splitPendingConsentByKnownDestination, groupByDestinationDecision } from "./mediaDestination";

describe("destinationForCategory", () => {
    it("reads intro_video's own destination, independent of the images destination", () => {
        const mediaDestination = { intro_video: "library", images: "project" };
        expect(destinationForCategory(mediaDestination, "intro_video")).toBe("library");
    });

    it("routes every image sub-category (generic/indian/western) through the single 'images' choice", () => {
        const mediaDestination = { intro_video: "project", images: "library" };
        expect(destinationForCategory(mediaDestination, "image")).toBe("library");
        expect(destinationForCategory(mediaDestination, "indian")).toBe("library");
        expect(destinationForCategory(mediaDestination, "western")).toBe("library");
    });
});

describe("splitPendingConsentByKnownDestination — the fix for the post-upload popup race", () => {
    it("puts every item into 'known' when the default destination (library) is in effect for both categories", () => {
        const mediaDestination = { intro_video: "library", images: "library" };
        const pending = [{ id: "1", category: "intro_video" }, { id: "2", category: "image" }];
        const { known, awaitingChoice } = splitPendingConsentByKnownDestination(pending, mediaDestination);
        expect(known).toHaveLength(2);
        expect(awaitingChoice).toHaveLength(0);
    });

    it("puts every item into 'known' when the talent explicitly chose 'project' instead", () => {
        const mediaDestination = { intro_video: "project", images: "project" };
        const pending = [{ id: "1", category: "intro_video" }];
        const { known, awaitingChoice } = splitPendingConsentByKnownDestination(pending, mediaDestination);
        expect(known).toHaveLength(1);
        expect(awaitingChoice).toHaveLength(0);
    });

    it("falls back to 'awaitingChoice' — the legacy dialog's only remaining audience — when a category's destination is unset (e.g. an old resumed draft predating this feature)", () => {
        const mediaDestination = { intro_video: "", images: "library" };
        const pending = [{ id: "1", category: "intro_video" }, { id: "2", category: "image" }];
        const { known, awaitingChoice } = splitPendingConsentByKnownDestination(pending, mediaDestination);
        expect(known.map((m) => m.id)).toEqual(["2"]);
        expect(awaitingChoice.map((m) => m.id)).toEqual(["1"]);
    });

    it("never drops or duplicates an item across the two groups", () => {
        const mediaDestination = { intro_video: "library", images: "" };
        const pending = [
            { id: "1", category: "intro_video" },
            { id: "2", category: "image" },
            { id: "3", category: "indian" },
        ];
        const { known, awaitingChoice } = splitPendingConsentByKnownDestination(pending, mediaDestination);
        expect(known.length + awaitingChoice.length).toBe(pending.length);
        expect(new Set([...known, ...awaitingChoice].map((m) => m.id))).toEqual(new Set(["1", "2", "3"]));
    });
});

describe("groupByDestinationDecision", () => {
    it("maps 'library' to the update_profile decision", () => {
        const mediaDestination = { intro_video: "library", images: "project" };
        const grouped = groupByDestinationDecision([{ id: "1", category: "intro_video" }], mediaDestination);
        expect(grouped).toEqual({ update_profile: ["1"] });
    });

    it("maps 'project' (and any other non-'library' value) to only_this_project", () => {
        const mediaDestination = { intro_video: "project", images: "project" };
        const grouped = groupByDestinationDecision([{ id: "1", category: "image" }], mediaDestination);
        expect(grouped).toEqual({ only_this_project: ["1"] });
    });

    it("batches multiple items sharing the same resolved decision into one id list", () => {
        const mediaDestination = { intro_video: "library", images: "library" };
        const items = [
            { id: "1", category: "intro_video" },
            { id: "2", category: "image" },
            { id: "3", category: "indian" },
        ];
        const grouped = groupByDestinationDecision(items, mediaDestination);
        expect(grouped).toEqual({ update_profile: ["1", "2", "3"] });
    });

    it("splits a mixed batch (some library, some project) into two separate decision groups", () => {
        const mediaDestination = { intro_video: "library", images: "project" };
        const items = [
            { id: "1", category: "intro_video" },
            { id: "2", category: "image" },
        ];
        const grouped = groupByDestinationDecision(items, mediaDestination);
        expect(grouped).toEqual({ update_profile: ["1"], only_this_project: ["2"] });
    });
});
