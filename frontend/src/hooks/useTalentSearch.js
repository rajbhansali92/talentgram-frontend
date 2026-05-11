import { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";

/**
 * useTalentSearch — debounced talent search + selection set.
 *
 * Mirrors the original ProjectPipeline behaviour exactly:
 *   • 300ms debounce
 *   • `alive` flag to swallow stale responses
 *   • selectedTalents is a Set; toggle / clear helpers are stable.
 */
export function useTalentSearch() {
    const [searchQuery, setSearchQuery] = useState("");
    const [searchResults, setSearchResults] = useState([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [selectedTalents, setSelectedTalents] = useState(new Set());

    useEffect(() => {
        if (!searchQuery) {
            setSearchResults([]);
            return;
        }
        let alive = true;
        const timer = setTimeout(async () => {
            setSearchLoading(true);
            try {
                const res = await adminApi.get(
                    `/talents/search?q=${encodeURIComponent(searchQuery)}`,
                );
                if (alive) setSearchResults(res.data?.data || []);
            } catch (e) {
                console.error("Search failed", e);
                if (alive) setSearchResults([]);
            } finally {
                if (alive) setSearchLoading(false);
            }
        }, 300);
        return () => {
            alive = false;
            clearTimeout(timer);
        };
    }, [searchQuery]);

    const toggleTalentSelect = useCallback((id) => {
        setSelectedTalents((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const resetSearch = useCallback(() => {
        setSelectedTalents(new Set());
        setSearchResults([]);
        setSearchQuery("");
    }, []);

    return {
        searchQuery,
        setSearchQuery,
        searchResults,
        searchLoading,
        selectedTalents,
        toggleTalentSelect,
        resetSearch,
    };
}
