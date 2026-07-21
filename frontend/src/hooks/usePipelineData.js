import { useCallback, useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { talentPreviewCache } from "@/lib/talentPreviewCache";

/**
 * Primes the Quick View cache with the partial talent fields the pipeline
 * payload already carries (name/instagram/image — see
 * backend/routers/casting_pipeline.py's _talent_merge_fields), so the Eye
 * click on a PipelineCard finds a cache hit instead of constructing that
 * object itself. Never overwrites an existing entry — a talent that's
 * already been fully hydrated (or was just invalidated on purpose after an
 * edit) must not be clobbered by this partial data on the next pipeline
 * load.
 */
function primeTalentPreviewCache(items) {
    for (const item of items) {
        if (!item?.talent_id || talentPreviewCache.getTalent(item.talent_id)) continue;
        talentPreviewCache.setTalent(item.talent_id, {
            id: item.talent_id,
            name: item.talent_name,
            instagram_handle: item.instagram_handle,
            image_url: item.image_url,
        });
    }
}

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

            const items = res.data?.data || [];
            setData(items);
            primeTalentPreviewCache(items);
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

                const items = res.data?.data || [];
                setData(items);
                primeTalentPreviewCache(items);
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
