import { useCallback, useState } from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";
import { getStageLabel, normaliseStage } from "@/components/pipeline/constants";

/**
 * usePipelineDnD — native HTML5 drag-and-drop with optimistic updates.
 *
 * Architecture (PATCH 4D, preserved verbatim):
 *   • `dragId` state — which pipeline row id is currently being dragged.
 *     Stored at the parent so every Column can render its own drag-over
 *     highlight without prop-drilling complex state.
 *   • `dragSupported` — gated by `matchMedia('(hover:hover) and
 *     (pointer:fine)')` so touch devices fall back to taps + buttons.
 *   • `handleCardDrop(targetStage, droppedId)` — optimistic update:
 *     mutate local `data` in-place (set new stage), call backend,
 *     refetch on failure to roll back cleanly.
 */
export function usePipelineDnD({ projectId, setData, refresh }) {
    const [dragId, setDragId] = useState(null);

    const dragSupported =
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(hover: hover) and (pointer: fine)").matches;

    const handleCardDragStart = useCallback((id) => {
        setDragId(id);
    }, []);

    const handleCardDragEnd = useCallback(() => {
        setDragId(null);
    }, []);

    const handleCardDrop = useCallback(
        async (targetStage, droppedId) => {
            // Hard-clear drag state up front — independent of network result.
            setDragId(null);
            if (!droppedId || !targetStage) return;

            // Capture pre-move snapshot for clean rollback if backend fails.
            let snapshot = null;
            let toastUndo = null;
            setData((prev) => {
                snapshot = prev;
                const row = prev.find((r) => r.id === droppedId);
                if (!row) return prev;
                const current = normaliseStage(row.stage);
                if (current === targetStage) return prev; // no-op drops
                toastUndo = `Moving ${row.talent_name || "talent"} → ${getStageLabel(
                    targetStage,
                )}`;
                // Functional setter: clone the row with the new stage; leave
                // other rows untouched so memoised Cards skip re-render.
                return prev.map((r) =>
                    r.id === droppedId ? { ...r, stage: targetStage } : r,
                );
            });
            if (!toastUndo) return; // no-op drop (same stage or row not found)

            try {
                await adminApi.patch(`/projects/${projectId}/pipeline/move`, {
                    ids: [droppedId],
                    stage: targetStage,
                });
                // Soft confirmation — single line, no spam.
                toast.success(`Moved to ${getStageLabel(targetStage)}`);
                // Refresh in background to pick up `is_follow_up`
                // recomputation + updated_at — no await so the drop feels
                // instant.
                refresh();
            } catch (e) {
                console.error("Drag move failed:", e);
                // Roll back to the pre-drop snapshot. Cheap because we
                // captured the exact reference before mutation.
                if (snapshot) setData(snapshot);
                toast.error(e?.response?.data?.detail || "Move failed — reverted");
            }
        },
        [projectId, setData, refresh],
    );

    return {
        dragSupported,
        dragId,
        handleCardDragStart,
        handleCardDragEnd,
        handleCardDrop,
    };
}
