import { describe, it, expect, beforeEach } from "vitest";
import { getRosterSnapshot, setRosterSnapshot, clearRosterSnapshot } from "./talentRosterCache";

const KEY = "test-roster";
const OTHER_KEY = "other-roster";

describe("talentRosterCache", () => {
    beforeEach(() => {
        clearRosterSnapshot(KEY);
        clearRosterSnapshot(OTHER_KEY);
    });

    it("returns null for a key that has never been written", () => {
        expect(getRosterSnapshot(KEY)).toBeNull();
    });

    it("round-trips a snapshot written with setRosterSnapshot", () => {
        setRosterSnapshot(KEY, { page: 4, scrollY: 1850, filters: { search: "priya" } });
        expect(getRosterSnapshot(KEY)).toEqual({ page: 4, scrollY: 1850, filters: { search: "priya" } });
    });

    it("shallow-merges successive writes instead of replacing the whole snapshot", () => {
        setRosterSnapshot(KEY, { page: 4, filters: { search: "priya" } });
        setRosterSnapshot(KEY, { scrollY: 1850 }); // e.g. captured later, on unmount
        expect(getRosterSnapshot(KEY)).toEqual({ page: 4, filters: { search: "priya" }, scrollY: 1850 });
    });

    it("keeps different keys fully independent", () => {
        setRosterSnapshot(KEY, { page: 4 });
        setRosterSnapshot(OTHER_KEY, { page: 1 });
        expect(getRosterSnapshot(KEY)).toEqual({ page: 4 });
        expect(getRosterSnapshot(OTHER_KEY)).toEqual({ page: 1 });
    });

    it("clearRosterSnapshot removes only the given key", () => {
        setRosterSnapshot(KEY, { page: 4 });
        setRosterSnapshot(OTHER_KEY, { page: 1 });
        clearRosterSnapshot(KEY);
        expect(getRosterSnapshot(KEY)).toBeNull();
        expect(getRosterSnapshot(OTHER_KEY)).toEqual({ page: 1 });
    });

    it("ignores calls with a falsy key rather than throwing", () => {
        expect(() => setRosterSnapshot(null, { page: 4 })).not.toThrow();
        expect(() => clearRosterSnapshot(undefined)).not.toThrow();
        expect(getRosterSnapshot(null)).toBeNull();
        expect(getRosterSnapshot("")).toBeNull();
    });
});
