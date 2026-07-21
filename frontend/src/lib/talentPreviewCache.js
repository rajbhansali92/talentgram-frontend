/**
 * Session-scoped cache manager for hydrated talent profiles shown in
 * Quick View (Pipeline cards prime it on load; Browse Roster does not use
 * it). A single module-level instance, so all consumers share one cache
 * that survives client-side navigation but resets on a real page reload.
 * No backend changes, no persistence API — intentionally lives only as
 * long as the current tab's JS context.
 *
 * Nothing outside this file should touch the underlying storage directly;
 * everything goes through the methods below so the storage strategy can
 * change without touching call sites. Extension points, not implemented:
 *
 *   - TTL / staleness: stamp entries with a `fetchedAt` timestamp in
 *     setTalent(); getTalent() would check the age before returning a hit
 *     and treat expired entries as a miss.
 *   - LRU eviction: track access recency (re-inserting into `_cache` on
 *     read bumps a Map's iteration order for free) and cap `_cache.size`
 *     in setTalent(), evicting the oldest entry past the cap.
 *   - stale-while-revalidate: getTalent() could return the cached value
 *     immediately while also kicking off a background hydrateTalent()
 *     call, instead of callers deciding whether to hydrate.
 *   - persistence: swap the backing Maps for a sessionStorage-backed store
 *     inside getTalent()/setTalent() only — the public API wouldn't change.
 *   - cache metrics: increment hit/miss counters in getTalent() and
 *     network-call counters in hydrateTalent() for instrumentation.
 */
class TalentPreviewCacheManager {
    constructor() {
        this._cache = new Map(); // talent id -> cached talent object (partial or fully hydrated)
        this._inFlight = new Map(); // talent id -> Promise<talent object>, for request dedup
    }

    /** Returns the cached talent object for `id`, or null if absent. */
    getTalent(id) {
        return (id && this._cache.get(id)) || null;
    }

    /** Writes/overwrites the cached talent object for `id`. */
    setTalent(id, talent) {
        if (!id || !talent) return;
        this._cache.set(id, talent);
    }

    /**
     * Drops the cached entry (and any in-flight request) for `id`. Call
     * this whenever a talent's underlying record changes elsewhere in the
     * app, so the next Quick View open re-fetches instead of showing
     * stale data.
     */
    invalidateTalent(id) {
        if (!id) return;
        this._cache.delete(id);
        this._inFlight.delete(id);
    }

    /** Drops every cached entry and in-flight request. */
    clearCache() {
        this._cache.clear();
        this._inFlight.clear();
    }

    /**
     * Resolves the fully-hydrated talent for `id` via `fetcher`, caching
     * the result. Concurrent/rapid callers for the same id share the same
     * in-flight promise instead of issuing duplicate requests — a burst of
     * clicks (or two cards referencing the same talent) can never fire
     * more than one network call at a time for a given id.
     */
    hydrateTalent(id, fetcher) {
        if (!id) return Promise.reject(new Error("hydrateTalent: missing id"));

        const existing = this._inFlight.get(id);
        if (existing) return existing;

        const promise = fetcher()
            .then((talent) => {
                this.setTalent(id, talent);
                this._inFlight.delete(id);
                return talent;
            })
            .catch((err) => {
                this._inFlight.delete(id);
                throw err;
            });

        this._inFlight.set(id, promise);
        return promise;
    }
}

export const talentPreviewCache = new TalentPreviewCacheManager();
