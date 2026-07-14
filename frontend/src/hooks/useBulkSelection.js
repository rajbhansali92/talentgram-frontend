import { useCallback, useEffect, useState } from "react";

/**
 * useBulkSelection — owns the bulk-mode toggle + selection Set.
 */
export function useBulkSelection() {
    const [bulkIds, setBulkIds] = useState(new Set());
    const [bulkMode, setBulkMode] = useState(false);
    const [lastSelectedId, setLastSelectedId] = useState(null);

    const toggleBulkSelect = useCallback((id, shiftKey = false, items = []) => {
        setBulkMode(true);
        setBulkIds((prev) => {
            const next = new Set(prev);
            if (shiftKey && lastSelectedId && items && items.length > 0) {
                const ids = items.map((i) => i.id).filter(Boolean);
                const idx1 = ids.indexOf(lastSelectedId);
                const idx2 = ids.indexOf(id);
                if (idx1 !== -1 && idx2 !== -1) {
                    const start = Math.min(idx1, idx2);
                    const end = Math.max(idx1, idx2);
                    const rangeIds = ids.slice(start, end + 1);
                    const shouldSelect = !prev.has(id);
                    rangeIds.forEach((rid) => {
                        if (shouldSelect) next.add(rid);
                        else next.delete(rid);
                    });
                    setLastSelectedId(id);
                    return next;
                }
            }
            if (next.has(id)) {
                next.delete(id);
            } else {
                next.add(id);
            }
            setLastSelectedId(id);
            return next;
        });
    }, [lastSelectedId]);

    const clearBulkSelection = useCallback(() => {
        setBulkIds(new Set());
        setBulkMode(false);
        setLastSelectedId(null);
    }, []);

    // Select-all-in-column — passed down to Column header.
    const selectAllInColumn = useCallback((items) => {
        const visibleIds = (items || []).map((i) => i.id).filter(Boolean);
        if (visibleIds.length === 0) return;
        setBulkMode(true);
        setBulkIds((prev) => {
            const next = new Set(prev);
            const allSelected = visibleIds.every((id) => next.has(id));
            if (allSelected) {
                visibleIds.forEach((id) => next.delete(id));
            } else {
                visibleIds.forEach((id) => next.add(id));
            }
            return next;
        });
    }, []);

    // ESC clears selection — only attached when a selection exists.
    useEffect(() => {
        if (bulkIds.size === 0) return;
        const onKey = (e) => {
            if (e.key === "Escape") {
                setBulkIds(new Set());
                setBulkMode(false);
                setLastSelectedId(null);
            }
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [bulkIds.size]);

    return {
        bulkIds,
        setBulkIds,
        bulkMode,
        setBulkMode,
        toggleBulkSelect,
        clearBulkSelection,
        selectAllInColumn,
        lastSelectedId,
        setLastSelectedId,
    };
}
