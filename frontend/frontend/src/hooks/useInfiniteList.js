import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Generic infinite-list loader.
 *
 * `fetchPage({ page, limit })` MUST resolve to either:
 *   - paginated shape: `{ data|items, total, page, pages, has_more }`
 *   - or a raw array (legacy non-paginated endpoints) — treated as a single page.
 *
 * `deps` triggers a full reset (e.g. when filter / search query changes).
 */
export default function useInfiniteList(fetchPage, deps = [], { limit = 30 } = {}) {
    const [items, setItems] = useState([]);
    const [page, setPage] = useState(0);
    const [total, setTotal] = useState(0);
    const [hasMore, setHasMore] = useState(true);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [error, setError] = useState(null);
    const reqId = useRef(0);

    const load = useCallback(
        async (nextPage = 0) => {
            const myReq = ++reqId.current;
            const isFirst = nextPage === 0;
            if (isFirst) setLoading(true);
            else setLoadingMore(true);
            setError(null);
            try {
                const res = await fetchPage({ page: nextPage, limit });
                if (myReq !== reqId.current) return; // stale response
                if (Array.isArray(res)) {
                    // Legacy non-paginated response — treat as single page.
                    setItems(res);
                    setTotal(res.length);
                    setHasMore(false);
                    setPage(0);
                } else {
                    const list = res.data || res.items || [];
                    setItems((prev) => (isFirst ? list : [...prev, ...list]));
                    setTotal(res.total ?? list.length);
                    const more =
                        typeof res.has_more === "boolean"
                            ? res.has_more
                            : (res.page ?? 0) + 1 < (res.pages ?? 0);
                    setHasMore(Boolean(more));
                    setPage(res.page ?? nextPage);
                }
            } catch (e) {
                if (myReq !== reqId.current) return;
                setError(e);
            } finally {
                if (myReq === reqId.current) {
                    setLoading(false);
                    setLoadingMore(false);
                }
            }
        },
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [limit, fetchPage],
    );

    // Reset & reload whenever deps change.
    useEffect(() => {
        load(0);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, deps);

    const loadMore = useCallback(() => {
        if (loading || loadingMore || !hasMore) return;
        load(page + 1);
    }, [loading, loadingMore, hasMore, page, load]);

    const reload = useCallback(() => load(0), [load]);

    return {
        items,
        setItems,
        total,
        page,
        hasMore,
        loading,
        loadingMore,
        error,
        loadMore,
        reload,
    };
}

/**
 * Attach an IntersectionObserver to a sentinel ref. When the sentinel
 * scrolls into view, `onIntersect` is called.
 */
export function useInfiniteScroll(onIntersect, { rootMargin = "400px" } = {}) {
    const ref = useRef(null);
    useEffect(() => {
        const node = ref.current;
        if (!node) return undefined;
        const obs = new IntersectionObserver(
            (entries) => {
                if (entries.some((e) => e.isIntersecting)) onIntersect();
            },
            { rootMargin },
        );
        obs.observe(node);
        return () => obs.disconnect();
    }, [onIntersect, rootMargin]);
    return ref;
}
