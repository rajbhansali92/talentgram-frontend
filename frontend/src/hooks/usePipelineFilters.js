import { useCallback, useEffect, useMemo, useState } from "react";
import { DEFAULT_FILTERS, normaliseStage } from "@/components/pipeline/constants";

/**
 * usePipelineFilters — view-layer-only filtering with debounced search.
 *
 * ISSUE 5 FIX: Added proper debouncing (180ms) for search input
 * Prevents excessive re-renders while typing
 * 
 * Filters NEVER mutate `data`. We compute `filteredData` lazily via
 * useMemo, keyed on the four filter inputs. `hiddenStages` is a Set
 * applied at the section/column render level (column stops being
 * rendered, not filtered out of `data`). `statusFocus === "follow_up"`
 * is special: it collapses every section except the follow-up lane,
 * giving the casting team a single-click "needs attention" view.
 *
 * `has_submission` is a best-effort inference from the existing API
 * surface — we use `is_follow_up` as the proxy (true ⇒ no submission;
 * false ⇒ submission has been received OR the talent has progressed
 * beyond ask_to_test). Reasonable because once a talent leaves
 * ask_to_test, they were necessarily acted on by an admin or by
 * the auto-sync, which requires a submission.
 */
export function usePipelineFilters(data) {
    // Search with debounce
    const [searchInput, setSearchInput] = useState(DEFAULT_FILTERS.search);
    const [debouncedSearch, setDebouncedSearch] = useState(DEFAULT_FILTERS.search);
    
    // Other filters
    const [statusFocus, setStatusFocus] = useState(DEFAULT_FILTERS.statusFocus);
    const [hasSubmission, setHasSubmission] = useState(DEFAULT_FILTERS.hasSubmission);
    const [hasIg, setHasIg] = useState(DEFAULT_FILTERS.hasIg);
    const [hiddenStages, setHiddenStages] = useState(() => new Set());

    // ISSUE 5: Debounce search input (180ms)
    useEffect(() => {
        const timer = setTimeout(() => {
            setDebouncedSearch(searchInput);
        }, 180);

        return () => clearTimeout(timer);
    }, [searchInput]);

    // Check if any filters are active
    const filtersActive = useMemo(() => {
        return Boolean(searchInput) ||
            statusFocus !== DEFAULT_FILTERS.statusFocus ||
            hasSubmission !== DEFAULT_FILTERS.hasSubmission ||
            hasIg !== DEFAULT_FILTERS.hasIg ||
            hiddenStages.size > 0;
    }, [searchInput, statusFocus, hasSubmission, hasIg, hiddenStages.size]);

    // Filter the data based on all criteria
    const filteredData = useMemo(() => {
        // SAFETY FIX: Handle undefined or null data gracefully
        if (!data || data.length === 0) return [];
        
        let rows = data;

        // Apply debounced search filter
        if (debouncedSearch && debouncedSearch.trim()) {
            const q = debouncedSearch.trim().toLowerCase();
            rows = rows.filter((r) => {
                const hay = [
                    r.talent_name,
                    r.instagram_handle,
                    r.talent_email,
                    r.email,
                    r.talent_phone,
                    r.talent_id,
                ]
                    .filter(Boolean)
                    .join(" ")
                    .toLowerCase();
                return hay.includes(q);
            });
        }

        // Apply status focus filter
        if (statusFocus !== "all") {
            rows = rows.filter((r) => {
                if (statusFocus === "follow_up") return r.is_follow_up === true;
                const s = normaliseStage(r.stage);
                if (statusFocus === "pending") return s === "ask_to_test" || s === "hold";
                return s === statusFocus;
            });
        }

        // Apply hasSubmission filter (tristate)
        if (hasSubmission !== "any") {
            rows = rows.filter((r) =>
                hasSubmission === "yes" ? r.is_follow_up === false : r.is_follow_up === true,
            );
        }

        // Apply hasIg filter (tristate)
        if (hasIg !== "any") {
            rows = rows.filter((r) =>
                hasIg === "yes" ? Boolean(r.instagram_handle) : !r.instagram_handle,
            );
        }

        return rows;
    }, [data, debouncedSearch, statusFocus, hasSubmission, hasIg]);

    // Toggle stage visibility for column hiding
    const toggleStageVisibility = useCallback((stage) => {
        setHiddenStages((prev) => {
            const next = new Set(prev);
            if (next.has(stage)) next.delete(stage);
            else next.add(stage);
            return next;
        });
    }, []);

    // Clear all filters
    const clearAllFilters = useCallback(() => {
        setSearchInput(DEFAULT_FILTERS.search);
        setDebouncedSearch(DEFAULT_FILTERS.search);
        setStatusFocus(DEFAULT_FILTERS.statusFocus);
        setHasSubmission(DEFAULT_FILTERS.hasSubmission);
        setHasIg(DEFAULT_FILTERS.hasIg);
        setHiddenStages(new Set());
    }, []);

    // Helper: Check if we should show only follow-up view
    const showOnlyFollowUp = useMemo(() => {
        return statusFocus === "follow_up";
    }, [statusFocus]);

    // SAFETY FIX: Check if filtered results are zero but data exists
    // Prevents crash when data is undefined/null
    const hasZeroAfterFilter = useMemo(() => {
        const dataLength = data?.length || 0;
        const hasFilters = filtersActive;
        return dataLength > 0 && filteredData.length === 0 && hasFilters;
    }, [data, filteredData.length, filtersActive]);

    // Helper: Get total unfiltered count safely
    const totalCount = useMemo(() => {
        return data?.length || 0;
    }, [data]);

    // Helper: Get filtered count
    const filteredCount = useMemo(() => {
        return filteredData.length;
    }, [filteredData]);

    // Helper: Check if a specific filter is active
    const isFilterActive = useCallback((filterName) => {
        switch(filterName) {
            case "search":
                return searchInput !== "";
            case "status":
                return statusFocus !== "all";
            case "submission":
                return hasSubmission !== "any";
            case "ig":
                return hasIg !== "any";
            default:
                return false;
        }
    }, [searchInput, statusFocus, hasSubmission, hasIg]);

    // Helper: Get count of active filters
    const activeFilterCount = useMemo(() => {
        let count = 0;
        if (searchInput) count++;
        if (statusFocus !== "all") count++;
        if (hasSubmission !== "any") count++;
        if (hasIg !== "any") count++;
        return count;
    }, [searchInput, statusFocus, hasSubmission, hasIg]);

    // Helper: Get current filter summary for display/debugging
    const filterSummary = useMemo(() => {
        const active = [];
        if (searchInput) active.push(`search: "${searchInput}"`);
        if (statusFocus !== "all") active.push(`status: ${statusFocus}`);
        if (hasSubmission !== "any") active.push(`submission: ${hasSubmission}`);
        if (hasIg !== "any") active.push(`ig: ${hasIg}`);
        return active;
    }, [searchInput, statusFocus, hasSubmission, hasIg]);

    return {
        // Search (raw input for immediate UI updates)
        search: searchInput,
        setSearch: setSearchInput,
        debouncedSearch,
        
        // Status focus
        statusFocus,
        setStatusFocus,
        
        // Tristate filters
        hasSubmission,
        setHasSubmission,
        hasIg,
        setHasIg,
        
        // Stage visibility
        hiddenStages,
        toggleStageVisibility,
        
        // Derived state
        filteredData,
        showOnlyFollowUp,
        hasZeroAfterFilter,
        filtersActive,
        
        // Count helpers
        totalCount,
        filteredCount,
        activeFilterCount,
        
        // Filter metadata
        filterSummary,
        
        // Helpers
        clearAllFilters,
        isFilterActive,
        
        // Original data reference (safe copy)
        originalData: data || [],
    };
}
