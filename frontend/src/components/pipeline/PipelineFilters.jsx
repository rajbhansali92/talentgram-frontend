import { useCallback, useEffect, useMemo, useState } from "react";
import { DEFAULT_FILTERS, normaliseStage } from "@/components/pipeline/constants";

/**
 * usePipelineFilters — filter state + debounced search for pipeline board.
 *
 * ISSUE 5 FIX: Added proper debouncing (180ms) for search input
 * Prevents excessive re-renders while typing
 * Features:
 *   • 180ms debounce for smooth typing experience
 *   • Memoised filtered results for performance
 *   • Follow-up only view toggle
 *   • Hidden stages based on statusFocus
 *   • Tristate filters (yes/no/any) for submission and IG
 *   • Safe handling of undefined/null data
 */
export function usePipelineFilters(data) {
    // Search with debounce
    const [searchInput, setSearchInput] = useState(DEFAULT_FILTERS.search);
    const [debouncedSearch, setDebouncedSearch] = useState(DEFAULT_FILTERS.search);
    
    // Other filters
    const [statusFocus, setStatusFocus] = useState(DEFAULT_FILTERS.statusFocus);
    const [hasSubmission, setHasSubmission] = useState(DEFAULT_FILTERS.hasSubmission);
    const [hasIg, setHasIg] = useState(DEFAULT_FILTERS.hasIg);

    // ISSUE 5: Debounce search input (180ms)
    useEffect(() => {
        const timer = setTimeout(() => {
            setDebouncedSearch(searchInput);
        }, 180);

        return () => clearTimeout(timer);
    }, [searchInput]);

    // Determine which stages are hidden based on statusFocus
    const hiddenStages = useMemo(() => {
        if (statusFocus === "all") return new Set();
        
        // Show only specific stage
        if (statusFocus === "follow_up") {
            return new Set(["all"]); // Special handling - show follow-up only separately
        }
        
        // Hide all stages except the focused one
        const allStages = [
            "ask_to_test", "approved", "hold", "shortlisted", 
            "already_tested", "locked", "rejected", 
            "not_available", "not_interested", "pitch"
        ];
        const hidden = new Set(allStages);
        hidden.delete(statusFocus);
        return hidden;
    }, [statusFocus]);

    // Filter the data based on all criteria
    const filteredData = useMemo(() => {
        // SAFETY FIX: Handle undefined or null data gracefully
        if (!data || data.length === 0) return [];
        
        // First, filter by statusFocus (stage filtering)
        let filtered = data;
        
        if (statusFocus !== "all" && statusFocus !== "follow_up") {
            filtered = filtered.filter(item => {
                const stage = normaliseStage(item.stage);
                return stage === statusFocus;
            });
        }
        
        // Apply follow-up filter
        if (statusFocus === "follow_up") {
            filtered = filtered.filter(item => item.is_follow_up === true);
        }
        
        // Apply debounced search filter
        if (debouncedSearch && debouncedSearch.trim()) {
            const searchLower = debouncedSearch.trim().toLowerCase();
            filtered = filtered.filter(item => {
                const name = (item.talent_name || "").toLowerCase();
                const email = (item.talent_email || item.email || "").toLowerCase();
                const phone = (item.talent_phone || "").toLowerCase();
                const ig = (item.instagram_handle || "").toLowerCase();
                const id = (item.talent_id || "").toLowerCase();
                
                return name.includes(searchLower) ||
                       email.includes(searchLower) ||
                       phone.includes(searchLower) ||
                       ig.includes(searchLower) ||
                       id.includes(searchLower);
            });
        }
        
        // Apply hasSubmission filter (tristate)
        if (hasSubmission !== "any") {
            const hasValue = hasSubmission === "yes";
            filtered = filtered.filter(item => {
                const hasSubmissionValue = Boolean(item.has_submission || item.portfolio_url);
                return hasSubmissionValue === hasValue;
            });
        }
        
        // Apply hasIg filter (tristate)
        if (hasIg !== "any") {
            const hasValue = hasIg === "yes";
            filtered = filtered.filter(item => {
                const hasIgValue = Boolean(item.instagram_handle);
                return hasIgValue === hasValue;
            });
        }
        
        return filtered;
    }, [data, statusFocus, debouncedSearch, hasSubmission, hasIg]);

    // Check if we should show only follow-up view
    const showOnlyFollowUp = useMemo(() => {
        return statusFocus === "follow_up";
    }, [statusFocus]);

    // Check if any filters are active
    const filtersActive = useMemo(() => {
        return searchInput !== "" ||
               statusFocus !== "all" ||
               hasSubmission !== "any" ||
               hasIg !== "any";
    }, [searchInput, statusFocus, hasSubmission, hasIg]);

    // SAFETY FIX: Check if filtered results are zero but data exists
    // Prevents crash when data is undefined/null
    const hasZeroAfterFilter = useMemo(() => {
        const dataLength = data?.length || 0;
        return dataLength > 0 && filteredData.length === 0;
    }, [data, filteredData.length]);

    // Clear all filters
    const clearAllFilters = useCallback(() => {
        setSearchInput(DEFAULT_FILTERS.search);
        setDebouncedSearch(DEFAULT_FILTERS.search);
        setStatusFocus(DEFAULT_FILTERS.statusFocus);
        setHasSubmission(DEFAULT_FILTERS.hasSubmission);
        setHasIg(DEFAULT_FILTERS.hasIg);
    }, []);

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

    // Helper: Get total unfiltered count safely
    const totalCount = useMemo(() => {
        return data?.length || 0;
    }, [data]);

    // Helper: Get filtered count
    const filteredCount = useMemo(() => {
        return filteredData.length;
    }, [filteredData]);

    return {
        // Search
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
        
        // Derived state
        hiddenStages,
        filteredData,
        showOnlyFollowUp,
        filtersActive,
        hasZeroAfterFilter,
        activeFilterCount,
        
        // Count helpers
        totalCount,
        filteredCount,
        
        // Filter metadata
        filterSummary,
        
        // Helpers
        clearAllFilters,
        isFilterActive,
        
        // Original data reference (safe copy)
        originalData: data || [],
    };
}
