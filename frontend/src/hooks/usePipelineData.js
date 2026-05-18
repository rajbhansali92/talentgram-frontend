import { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";

/**
 * usePipelineData — owns the project pipeline fetch + cache.
 */
export function usePipelineData(projectId) {
    const [data, setData] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const fetchPipeline = useCallback(async () => {
        if (!projectId) return;

        try {
            setError(null);

            const res = await adminApi.get(
                `/projects/${projectId}/pipeline`
            );

            setData(res.data?.data || []);
        } catch (e) {
            console.error("Failed to fetch pipeline:", e);
            setError("Failed to load pipeline data");
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => {
        if (!projectId) return;

        let alive = true;

        (async () => {
            try {
                setError(null);

                const res = await adminApi.get(
                    `/projects/${projectId}/pipeline`
                );

                if (!alive) return;

                setData(res.data?.data || []);
            } catch (e) {
                if (!alive) return;

                console.error("Failed to fetch pipeline:", e);
                setError("Failed to load pipeline data");
            } finally {
                if (alive) setLoading(false);
            }
        })();

        return () => {
            alive = false;
        };
    }, [projectId]);

    return {
        data,
        setData,
        loading,
        error,
        fetchPipeline,
    };
}
