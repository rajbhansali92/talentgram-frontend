import { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";

/**
 * usePipelineData — owns the project pipeline fetch + cache.
 *
 * Preserves the original double-pattern from ProjectPipeline.jsx:
 *   • `fetchPipeline` is a stable callback for post-mutation refresh.
 *   • The initial-mount effect runs an INLINE fetch with an `alive` flag
 *     so React StrictMode's double-invoke stays idempotent and we never
 *     setState after unmount.
 *
 * Returns `setData` so callers (drag handlers, etc.) can perform
 * optimistic local mutations without re-fetching.
 */
export function usePipelineData(projectId) {
    const [data, setData] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    // Stable identity. Used by Card.move + bulk handlers + post-mutation refresh.
    // Depends only on the primitive `projectId`, so re-creates exactly once per route.
    const fetchPipeline = useCallback(async () => {
        if (!projectId) return;
        try {
            setError(null);
            const res = await adminApi.get(`/projects/${projectId}/pipeline`);
            setData(res.data?.data || []);
        } catch (e) {
            console.error("Failed to fetch pipeline:", e);
            setError("Failed to load pipeline data");
        } finally {
            setLoading(false);
        }
    }, [projectId]);

    // Initial mount fetch with `alive` guard so we never call setState after
    // unmount (StrictMode double-invokes effects in dev — this makes that idempotent).
    useEffect(() => {
        if (!projectId) return;
        let alive = true;
        (async () => {
            try {
                setError(null);
                const res = await adminApi.get(`/projects/${projectId}/pipeline`);
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

    return { data, setData, loading, error, fetchPipeline };
}
