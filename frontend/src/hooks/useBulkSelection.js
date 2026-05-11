import { useCallback, useEffect, useState } from "react";

/**
 * useBulkSelection — owns the bulk-mode toggle + selection Set.
 *
 * Behaviour preserved bit-for-bit from ProjectPipeline:
 *   • toggleBulkSelect adds/removes a single id via functional setter.
 *   • clearBulkSelection wipes the Set AND exits bulk mode.
 *   • selectAllInColumn intelligently toggles: if every visible row in
 *     the column is selected, deselect just those; otherwise add them.
 *     Auto-enters bulk mode so it's a single click.
 *   • ESC is only listened for while there's an active selection, so
 *     it doesn't fight other shortcuts when bulk-mode is idle.
 */
export function useBulkSelection() {
    const [bulkIds, setBulkIds] = useState(new Set());
    const [bulkMode, setBulkMode] = useState(false);

    const toggleBulkSelect = useCallback((id) => {
        setBulkIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    }, []);

    const clearBulkSelection = useCallback(() => {
        setBulkIds(new Set());
        setBulkMode(false);
    }, []);

    // Select-all-in-column — passed down to Column header. The function is
    // pure (no closure over column items) — Column passes its own items in.
    const selectAllInColumn = useCallback((items) => {
        // Filter defensively — Column already filters out readOnly lanes
        // before invoking, but a defensive check keeps the contract robust.
        const visibleIds = (items || []).map((i) => i.id).filter(Boolean);
        if (visibleIds.length === 0) return;
        // Enter bulk mode automatically — saves a click.
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
    };
}
