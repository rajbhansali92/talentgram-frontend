import { useCallback, useEffect, useRef, useState } from "react";
import { adminApi } from "@/lib/api";

/**
 * useTalentSearch — debounced talent search + selection set.
 *
 * ISSUE 5 FIX: Added proper debouncing (180ms) for search input
 * Prevents excessive API calls while typing
 * Features:
 *   • 180ms debounce (optimal for quick typing)
 *   • AbortController for cancelling stale requests
 *   • `alive` flag to swallow stale responses
 *   • selectedTalents is a Set; toggle / clear helpers are stable
 *   • Error handling with user-friendly fallbacks
 */
export function useTalentSearch() {
    const [searchQuery, setSearchQuery] = useState("");
    const [debouncedQuery, setDebouncedQuery] = useState("");
    const [searchResults, setSearchResults] = useState([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [searchError, setSearchError] = useState(null);
    const [selectedTalents, setSelectedTalents] = useState(new Set());
    const abortControllerRef = useRef(null);

    // ISSUE 5: Debounce the search query (180ms)
    useEffect(() => {
        const timer = setTimeout(() => {
            setDebouncedQuery(searchQuery);
        }, 180);

        return () => clearTimeout(timer);
    }, [searchQuery]);

    // Clear error when query changes
    useEffect(() => {
        if (searchError) setSearchError(null);
    }, [searchQuery, searchError]);

    // Perform search when debounced query changes
    useEffect(() => {
        // Cancel any in-flight request
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
        }

        if (!debouncedQuery || debouncedQuery.trim().length < 2) {
            setSearchResults([]);
            setSearchLoading(false);
            return;
        }

        let isMounted = true;
        const controller = new AbortController();
        abortControllerRef.current = controller;

        const performSearch = async () => {
            setSearchLoading(true);
            setSearchError(null);
            
            try {
                const res = await adminApi.get(
                    `/talents/search?q=${encodeURIComponent(debouncedQuery.trim())}`,
                    { signal: controller.signal }
                );
                
                if (isMounted) {
                    setSearchResults(res.data?.data || []);
                    if (res.data?.data?.length === 0) {
                        setSearchError("No talents found");
                    }
                }
            } catch (e) {
                // Don't set error if request was aborted
                if (e.name !== "AbortError" && isMounted) {
                    console.error("Search failed", e);
                    setSearchError(e?.response?.data?.detail || "Search failed");
                    setSearchResults([]);
                }
            } finally {
                if (isMounted) {
                    setSearchLoading(false);
                }
            }
        };

        performSearch();

        return () => {
            isMounted = false;
            if (controller.signal.aborted) return;
            controller.abort();
        };
    }, [debouncedQuery]);

    const toggleTalentSelect = useCallback((id) => {
        setSelectedTalents((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const clearSelected = useCallback(() => {
        setSelectedTalents(new Set());
    }, []);

    const resetSearch = useCallback(() => {
        setSelectedTalents(new Set());
        setSearchResults([]);
        setSearchQuery("");
        setDebouncedQuery("");
        setSearchError(null);
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
        }
    }, []);

    const isSearching = searchLoading || (debouncedQuery && debouncedQuery.trim().length >= 2 && searchResults.length === 0 && !searchError);

    return {
        searchQuery,
        setSearchQuery,
        debouncedQuery,
        searchResults,
        searchLoading,
        searchError,
        selectedTalents,
        toggleTalentSelect,
        clearSelected,
        resetSearch,
        isSearching,
        hasResults: searchResults.length > 0,
        resultsCount: searchResults.length,
    };
}
