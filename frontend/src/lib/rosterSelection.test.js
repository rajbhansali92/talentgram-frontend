import { describe, it, expect } from "vitest";
import {
    buildRosterPayload, makeHero, moveItem, pruneSelection, selectAllIds, shortTalentName, toggleId,
} from "./rosterSelection";

describe("rosterSelection", () => {
    it("moves items (talent / image reorder) without mutating the input", () => {
        const a = ["a", "b", "c", "d"];
        expect(moveItem(a, 3, 0)).toEqual(["d", "a", "b", "c"]);
        expect(moveItem(a, 0, 2)).toEqual(["b", "c", "a", "d"]);
        expect(moveItem(a, 1, 99)).toEqual(["a", "c", "d", "b"]);
        expect(moveItem(a, 1, -5)).toEqual(["b", "a", "c", "d"]);
        expect(moveItem(a, 9, 0)).toEqual(a);
        expect(a).toEqual(["a", "b", "c", "d"]);
    });

    it("toggles a selection on/off and respects the per-talent cap", () => {
        expect(toggleId([], "x")).toEqual(["x"]);
        expect(toggleId(["x", "y"], "x")).toEqual(["y"]);
        expect(toggleId(["x", "y"], "z", 2)).toEqual(["x", "y"]); // at cap: ignored
        expect(toggleId(["x", "y"], "y", 2)).toEqual(["x"]);       // removing still works at cap
    });

    it("makes an image the hero (first)", () => {
        expect(makeHero(["a", "b", "c"], "c")).toEqual(["c", "a", "b"]);
        expect(makeHero(["a", "b"], "a")).toEqual(["a", "b"]);
        expect(makeHero(["a", "b"], "zzz")).toEqual(["a", "b"]);
    });

    const groups = [
        { key: "indian", items: [{ id: "i1" }, { id: "i2" }] },
        { key: "western", items: [{ id: "w1" }] },
        { key: "portfolio", items: [{ id: "p1" }, { id: "p2" }] },
    ];
    it("select all keeps existing order first and honours the cap", () => {
        expect(selectAllIds(["w1"], groups)).toEqual(["w1", "i1", "i2", "p1", "p2"]);
        expect(selectAllIds([], groups, 3)).toEqual(["i1", "i2", "w1"]);
    });

    it("prunes ids that no longer exist on the profile", () => {
        expect(pruneSelection(["i1", "gone", "p2"], groups)).toEqual(["i1", "p2"]);
    });

    it("builds the generated-link payload for a roster", () => {
        const p = buildRosterPayload({
            title: "  Talentgram X Pepsi ", subtitle: " ", talentIds: ["t2", "t1"],
            mediaByTalent: { t1: ["a"], t2: ["b", "c"] },
        });
        expect(p.link_type).toBe("roster");
        expect(p.title).toBe("Talentgram X Pepsi");
        expect(p.talent_ids).toEqual(["t2", "t1"]);
        expect(p.roster.subtitle).toBeNull();
        expect(p.roster.talents).toEqual([{ talent_id: "t2", media_ids: ["b", "c"] }, { talent_id: "t1", media_ids: ["a"] }]);
        expect(p.auto_pull).toBe(false);
        expect(p.is_public).toBe(true);
    });

    it("formats names exactly like the backend (First + surname initial)", () => {
        expect(shortTalentName("Angela Kumar")).toBe("Angela K");
        expect(shortTalentName("Fanny Gandhi")).toBe("Fanny G");
        expect(shortTalentName("Jasmine Singh")).toBe("Jasmine S");
        expect(shortTalentName("Mary Ann smith")).toBe("Mary Ann S");
        expect(shortTalentName("Cher")).toBe("Cher");
        expect(shortTalentName("  ")).toBe("Talent");
    });
});
