import { useCallback, useEffect, useRef, useState } from "react";
import { fetchAssistantOverview } from "@/lib/simpleAssistant";

/**
 * Read-only data hook for the Simple Assistant Command Centre.
 * Fetches once on mount and exposes a manual refresh. No polling loop is
 * started by default (keeps it calm + cheap); the page may opt into a slow
 * refresh interval.
 *
 * fetchAssistantOverview never rejects — it resolves to `{ data }` or
 * `{ error }` — so there is no rejected-promise path out of this hook.
 */
export function useAssistantOverview({ refreshMs = 0 } = {}) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const abortRef = useRef(null);
    const mountedRef = useRef(true);

    const load = useCallback(async ({ quiet = false } = {}) => {
        if (abortRef.current) abortRef.current.abort();
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        if (!quiet) setLoading(true);
        setError(null);

        const result = await fetchAssistantOverview(ctrl.signal);
        if (!mountedRef.current || ctrl.signal.aborted) return;

        if (result.data) {
            setData(result.data);
        } else {
            const e = result.error;
            if (e?.name === "CanceledError" || e?.code === "ERR_CANCELED") return;
            setError(
                e?.response?.status === 404
                    ? "Simple Assistant is not enabled on this environment."
                    : e?.response?.data?.detail || "Could not reach Talentgram.",
            );
        }
        if (!quiet) setLoading(false);
    }, []);

    useEffect(() => {
        mountedRef.current = true;
        load();
        return () => {
            mountedRef.current = false;
            if (abortRef.current) abortRef.current.abort();
        };
    }, [load]);

    useEffect(() => {
        if (!refreshMs) return undefined;
        const id = setInterval(() => {
            if (document.visibilityState === "visible") load({ quiet: true });
        }, refreshMs);
        return () => clearInterval(id);
    }, [refreshMs, load]);

    return {
        data,
        loading,
        error,
        refresh: () => load({ quiet: true }),
        // The execute endpoint already returns a freshly-built overview —
        // apply it directly rather than paying a second round trip.
        applyExternal: (fresh) => {
            if (fresh && mountedRef.current) setData(fresh);
        },
    };
}
