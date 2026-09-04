import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { adminApi } from "@/lib/api";
import { useTalentDirectory } from "./useTalentDirectory";
import { setRosterSnapshot, getRosterSnapshot } from "@/lib/talentRosterCache";

vi.mock("@/lib/api", () => ({
    adminApi: { get: vi.fn() },
}));

const FRESH_RESPONSE = {
    data: { data: [{ id: "t-fresh", name: "Fresh Fetch" }], total: 1, pages: 1 },
};

describe("useTalentDirectory — browsing-state restoration (persistKey)", () => {
    // Each test uses its own persistKey (rather than one shared constant) so
    // a slow-resolving fetch/cache-write effect from one test can never leak
    // into the next — the module cache is a real, shared singleton by design
    // (that's the whole point of it), so test isolation has to come from the
    // key, not from the store.
    beforeEach(() => {
        adminApi.get.mockReset();
        adminApi.get.mockResolvedValue(FRESH_RESPONSE);
    });

    it("without persistKey, behaves exactly as before: starts at page 1 and always fetches on mount", async () => {
        const { result } = renderHook(() => useTalentDirectory({ pageSize: 40 }));
        expect(result.current.page).toBe(1);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledTimes(1));
        await waitFor(() => expect(result.current.talents).toEqual(FRESH_RESPONSE.data.data));
    });

    it("with persistKey but no prior snapshot, behaves like a normal first visit (fetches once, page 1)", async () => {
        const key = "test-roster-fresh-visit";
        const { result } = renderHook(() => useTalentDirectory({ pageSize: 40, persistKey: key }));
        expect(result.current.page).toBe(1);
        await waitFor(() => expect(adminApi.get).toHaveBeenCalledTimes(1));
        await waitFor(() => expect(result.current.talents).toEqual(FRESH_RESPONSE.data.data));
    });

    it("with persistKey and a cached snapshot, hydrates page/filters/sort/results from cache and does NOT refetch on mount", async () => {
        const key = "test-roster-hydrate";
        const cachedTalents = [{ id: "t1", name: "Cached Talent" }];
        setRosterSnapshot(key, {
            page: 4,
            sortBy: "name_asc",
            filters: { search: "priya", gender: "female", ethnicity: "any", locations: [], ageMin: "", ageMax: "", heightMin: "", heightMax: "", followersMin: "", interestedIn: [], interestedInMode: "any", skills: [], skillsMode: "any", tags: [], tagsMode: "any" },
            talents: cachedTalents,
            total: 121,
            pages: 4,
        });

        const { result } = renderHook(() => useTalentDirectory({ pageSize: 40, persistKey: key }));

        // Restored synchronously on first render — no loading flash, no fetch.
        expect(result.current.page).toBe(4);
        expect(result.current.sortBy).toBe("name_asc");
        expect(result.current.filters.search).toBe("priya");
        expect(result.current.filters.gender).toBe("female");
        expect(result.current.search).toBe("priya"); // search box mirrors the restored filter
        expect(result.current.talents).toEqual(cachedTalents);
        expect(result.current.total).toBe(121);
        expect(result.current.pages).toBe(4);
        expect(result.current.loading).toBe(false);

        // Give any stray effects a tick, then confirm no fetch happened.
        await new Promise((r) => setTimeout(r, 10));
        expect(adminApi.get).not.toHaveBeenCalled();
    });

    it("after hydrating from cache, a real filter change still fetches normally and resets to page 1", async () => {
        const key = "test-roster-filter-after-hydrate";
        setRosterSnapshot(key, {
            page: 4,
            sortBy: "created_desc",
            filters: { search: "", gender: "any", ethnicity: "any", locations: [], ageMin: "", ageMax: "", heightMin: "", heightMax: "", followersMin: "", interestedIn: [], interestedInMode: "any", skills: [], skillsMode: "any", tags: [], tagsMode: "any" },
            talents: [{ id: "t1", name: "Cached Talent" }],
            total: 121,
            pages: 4,
        });

        const { result } = renderHook(() => useTalentDirectory({ pageSize: 40, persistKey: key }));
        expect(result.current.page).toBe(4);
        expect(adminApi.get).not.toHaveBeenCalled();

        result.current.setFilter("gender", "female");

        // The pre-existing (unrelated to this change) two-effect design
        // fires a fetch, then a separate effect resets page -> 1, which
        // triggers a second fetch for page 1 — that's normal, unchanged
        // behaviour; what matters here is that filtering after a cache
        // hydration still works and still lands on page 1.
        await waitFor(() => expect(adminApi.get).toHaveBeenCalled());
        await waitFor(() => expect(result.current.page).toBe(1));
        await waitFor(() => expect(result.current.filters.gender).toBe("female"));
    });

    it("keeps the cache updated as the admin browses, so a later mount picks up the latest state", async () => {
        const key = "test-roster-keeps-updated";
        let call = 0;
        adminApi.get.mockImplementation(async () => {
            call += 1;
            return call === 1
                ? { data: { data: [{ id: "t1", name: "Page 1 Talent" }], total: 50, pages: 2 } }
                : { data: { data: [{ id: "t2", name: "Page 2 Talent" }], total: 50, pages: 2 } };
        });
        const { result } = renderHook(() => useTalentDirectory({ pageSize: 40, persistKey: key }));
        await waitFor(() => expect(result.current.talents[0]?.id).toBe("t1"));

        result.current.setPage(2);
        await waitFor(() => expect(result.current.page).toBe(2));
        await waitFor(() => expect(result.current.talents[0]?.id).toBe("t2"));

        expect(getRosterSnapshot(key).page).toBe(2);
        expect(getRosterSnapshot(key).talents[0].id).toBe("t2");
    });
});
