import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { adminApi } from "@/lib/api";

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

export function useTalentDirectory({ pageSize = DEFAULT_PAGE_SIZE, initialFilters } = {}) {
    const [filters, setFilters] = useState({ ...DEFAULT_FILTERS, ...initialFilters });
    const [searchInput, setSearchInput] = useState(filters.search);
    const [sortBy, setSortBy] = useState(DEFAULT_SORT);
    const [page, setPage] = useState(1); // 1-indexed for display; converted to 0-indexed for the API
    const [talents, setTalents] = useState([]);
    const [total, setTotal] = useState(0);
    const [pages, setPages] = useState(0);
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
    const filterKey = JSON.stringify(filters);
    useEffect(() => {
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

    useEffect(() => {
        fetchPage();
        return () => abortRef.current?.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filterKey, sortBy, page, pageSize]);

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
