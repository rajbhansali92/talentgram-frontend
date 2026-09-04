import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { adminApi } from "@/lib/api";
import { getRosterSnapshot, setRosterSnapshot } from "@/lib/talentRosterCache";

/**
 * useTalentDirectory — the ONE talent-browsing data engine, shared by the
 * Global Talent page (/admin/talents) and Browse Roster (the pipeline "Add
 * Talents" modal). Both surfaces render the same criteria against the same
 * server-side query — this hook owns filter/sort/search/pagination state
 * and the single GET /api/talents call, so the two surfaces can never drift
 * apart into separate filtering logic again.
 *
 * All filtering, sorting, and pagination happen server-side (see
 * routers/talents.py list_talents) — this hook never fetches more than one
 * page at a time, regardless of roster size.
 */

export const DEFAULT_FILTERS = {
    search: "",
    gender: "any",
    ethnicity: "any",
    locations: [],
    ageMin: "",
    ageMax: "",
    heightMin: "",
    heightMax: "",
    followersMin: "",
    interestedIn: [],
    interestedInMode: "any",
    skills: [],
    skillsMode: "any",
    tags: [],
    tagsMode: "any",
};

const DEFAULT_SORT = "created_desc";
const DEFAULT_PAGE_SIZE = 40;

function buildParams(filters, sortBy, page, pageSize) {
    const params = { page, size: pageSize };
    if (filters.search.trim()) params.q = filters.search.trim();
    if (filters.gender !== "any") params.gender = filters.gender;
    if (filters.ethnicity !== "any") params.ethnicity = filters.ethnicity;
    if (filters.locations.length) params.location = filters.locations;
    if (filters.ageMin !== "") params.age_min = filters.ageMin;
    if (filters.ageMax !== "") params.age_max = filters.ageMax;
    if (filters.heightMin !== "") params.height_min = filters.heightMin;
    if (filters.heightMax !== "") params.height_max = filters.heightMax;
    if (filters.followersMin) params.followers_min = filters.followersMin;
    if (filters.interestedIn.length) {
        params.interested_in = filters.interestedIn;
        params.interested_in_mode = filters.interestedInMode;
    }
    if (filters.skills.length) {
        params.skills = filters.skills;
        params.skills_mode = filters.skillsMode;
    }
    if (filters.tags.length) {
        params.tags = filters.tags;
        params.tags_mode = filters.tagsMode;
    }
    if (sortBy && sortBy !== DEFAULT_SORT) params.sort_by = sortBy;
    return params;
}

export function useTalentDirectory({ pageSize = DEFAULT_PAGE_SIZE, initialFilters, persistKey = null } = {}) {
    // Browsing-state restoration (2026-09-04): when a `persistKey` is given
    // and a snapshot from an earlier mount exists (e.g. the admin opened a
    // talent profile and just came Back), every piece of state below
    // hydrates from that snapshot instead of the usual defaults, and the
    // effects further down skip their normal "first mount" fetch/reset so
    // the restored page/filters/sort/results are never clobbered or
    // refetched. Opt-in only — without persistKey this hook behaves exactly
    // as before (no other current caller passes it).
    // Lazy initializer form (() => ...) so getRosterSnapshot is read AT
    // MOST once per component instance — a plain `useRef(expr)` would
    // otherwise re-evaluate `expr` on every render (React only USES the
    // first result, but still pays for the call every time).
    const cachedRef = useRef(() => (persistKey ? getRosterSnapshot(persistKey) : null));
    if (typeof cachedRef.current === "function") cachedRef.current = cachedRef.current();
    const cached = cachedRef.current;
    // `cachedRef` is never reassigned again after the line above, so the
    // two effects below can safely compare the CURRENT filters/sort/page
    // against it, on every render, to decide whether anything has actually
    // changed since restoring from cache. This is deliberately NOT a
    // "consumed once" flag: React 18/19 Strict Mode double-invokes effects
    // once on mount (dev only) specifically to catch effects that aren't
    // safe to re-run — a mutable "skip the first call" ref would be
    // correct on its first (Strict Mode) invocation but then wrongly fire
    // for real on the immediate second invocation, clobbering the very
    // state we just restored. Comparing against a frozen, unchanging
    // snapshot instead gives the exact same answer no matter how many
    // times it's evaluated.

    const [filters, setFilters] = useState(cached?.filters || { ...DEFAULT_FILTERS, ...initialFilters });
    const [searchInput, setSearchInput] = useState(cached?.filters?.search ?? filters.search);
    const [sortBy, setSortBy] = useState(cached?.sortBy || DEFAULT_SORT);
    const [page, setPage] = useState(cached?.page || 1); // 1-indexed for display; converted to 0-indexed for the API
    const [talents, setTalents] = useState(cached?.talents || []);
    const [total, setTotal] = useState(cached?.total || 0);
    const [pages, setPages] = useState(cached?.pages || 0);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const abortRef = useRef(null);
    const reqIdRef = useRef(0);

    // Debounce the free-text search box (250ms — matches TalentList.jsx's
    // existing debounce so the two surfaces feel identical while typing).
    useEffect(() => {
        const t = setTimeout(() => {
            setFilters((f) => (f.search === searchInput ? f : { ...f, search: searchInput }));
        }, 250);
        return () => clearTimeout(t);
    }, [searchInput]);

    // Any filter change resets to page 1 — a stale page number past the new
    // (smaller) result set would otherwise silently show an empty page.
    // No-ops on a cache-hydrated mount, as long as filters/sort still
    // exactly match what was restored (see cachedRef above).
    const filterKey = JSON.stringify(filters);
    useEffect(() => {
        const c = cachedRef.current;
        const unchangedSinceRestore = !!c
            && filterKey === JSON.stringify(c.filters || {})
            && (sortBy || DEFAULT_SORT) === (c.sortBy || DEFAULT_SORT);
        if (unchangedSinceRestore) return;
        setPage(1);
    }, [filterKey, sortBy]);

    const setFilter = useCallback((key, value) => {
        setFilters((f) => ({ ...f, [key]: value }));
    }, []);

    const clearAllFilters = useCallback(() => {
        setSearchInput("");
        setFilters({ ...DEFAULT_FILTERS });
        setSortBy(DEFAULT_SORT);
    }, []);

    const removeFilter = useCallback((key) => {
        setFilters((f) => ({ ...f, [key]: Array.isArray(DEFAULT_FILTERS[key]) ? [] : DEFAULT_FILTERS[key] }));
    }, []);

    const activeFilterCount = useMemo(() => {
        let count = 0;
        if (filters.search.trim()) count++;
        if (filters.gender !== "any") count++;
        if (filters.ethnicity !== "any") count++;
        if (filters.locations.length) count++;
        if (filters.ageMin !== "" || filters.ageMax !== "") count++;
        if (filters.heightMin !== "" || filters.heightMax !== "") count++;
        if (filters.followersMin) count++;
        if (filters.interestedIn.length) count++;
        if (filters.skills.length) count++;
        if (filters.tags.length) count++;
        return count;
    }, [filters]);

    const filtersActive = activeFilterCount > 0;

    const fetchPage = useCallback(async () => {
        if (abortRef.current) abortRef.current.abort();
        const controller = new AbortController();
        abortRef.current = controller;
        const reqId = ++reqIdRef.current;

        setLoading(true);
        setError(null);
        try {
            const params = buildParams(filters, sortBy, page - 1, pageSize);
            const { data } = await adminApi.get("/talents", { params, signal: controller.signal });
            if (reqId !== reqIdRef.current) return; // superseded by a newer request
            setTalents(data.data || data.items || []);
            setTotal(data.total || 0);
            setPages(data.pages || 0);
        } catch (err) {
            if (err?.name === "CanceledError" || err?.code === "ERR_CANCELED") return;
            if (reqId !== reqIdRef.current) return;
            setError(err);
        } finally {
            if (reqId === reqIdRef.current) setLoading(false);
        }
    }, [filters, sortBy, page, pageSize]);

    // No-ops on a cache-hydrated mount, as long as filters/sort/page still
    // exactly match what was restored — same reasoning as the effect above.
    useEffect(() => {
        const c = cachedRef.current;
        const unchangedSinceRestore = !!c
            && filterKey === JSON.stringify(c.filters || {})
            && (sortBy || DEFAULT_SORT) === (c.sortBy || DEFAULT_SORT)
            && page === (c.page || 1);
        if (unchangedSinceRestore) return;
        fetchPage();
        return () => abortRef.current?.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filterKey, sortBy, page, pageSize]);

    // Keep the roster snapshot current as the admin browses, so whenever
    // they navigate away (open a talent profile) the latest page/filters/
    // sort/results are already saved — no separate "on navigate away"
    // hook needed. No-op unless persistKey was given.
    useEffect(() => {
        if (!persistKey) return;
        setRosterSnapshot(persistKey, { filters, sortBy, page, talents, total, pages });
    }, [persistKey, filters, sortBy, page, talents, total, pages]);

    return {
        // Search box (raw input for immediate typing feedback + the debounced
        // value actually sent to the server, mirroring usePipelineFilters's shape).
        search: searchInput,
        setSearch: setSearchInput,

        // Structured filters
        filters,
        setFilter,
        removeFilter,
        clearAllFilters,
        activeFilterCount,
        filtersActive,

        // Sort
        sortBy,
        setSortBy,

        // Pagination (1-indexed for display)
        page,
        setPage,
        pageSize,
        total,
        pages,

        // Result data
        talents,
        loading,
        error,
        refetch: fetchPage,
    };
}
