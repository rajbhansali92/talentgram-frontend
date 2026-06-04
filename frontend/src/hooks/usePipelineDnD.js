import { useCallback, useState } from "react";
import { toast } from "sonner";
import { adminApi } from "@/lib/api";
import {
    getStageLabel,
    normaliseStage,
} from "@/components/pipeline/constants";

export function usePipelineDnD({
    projectId,
    setData,
    refresh,
}) {
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
            setDragId(null);

            if (!droppedId || !targetStage) return;

            const actualStage = targetStage === "follow_up" ? "ask_to_test" : targetStage;

            let snapshot = null;
            let toastUndo = null;

            setData((prev) => {
                snapshot = prev;

                const row = prev.find((r) => r.id === droppedId);

                if (!row) return prev;

                const current = normaliseStage(row.stage);

                if (current === actualStage) return prev;

                toastUndo = `Moving ${
                    row.talent_name || "talent"
                } → ${getStageLabel(actualStage)}`;

                return prev.map((r) =>
                    r.id === droppedId
                        ? { ...r, stage: actualStage }
                        : r
                );
            });

            if (!toastUndo) return;

            try {
                await adminApi.patch(
                    `/projects/${projectId}/pipeline/move`,
                    {
                        ids: [droppedId],
                        stage: actualStage,
                    }
                );

                toast.success(
                    `Moved to ${getStageLabel(actualStage)}`
                );

                refresh();
            } catch (e) {
                console.error("Drag move failed:", e);

                if (snapshot) setData(snapshot);

                toast.error(
                    e?.response?.data?.detail ||
                        "Move failed — reverted"
                );
            }
        },
        [projectId, setData, refresh]
    );

    return {
        dragSupported,
        dragId,
        handleCardDragStart,
        handleCardDragEnd,
        handleCardDrop,
    };
}
