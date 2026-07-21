/**
 * Session-only in-memory cache of fully-hydrated talent profiles, keyed by
 * talent id. Module-level state (not React state) so it is shared across
 * every consumer that imports it and survives client-side navigation —
 * but resets on a real page reload, since the module is re-evaluated then.
 * No backend changes, no persistence API (sessionStorage/localStorage) —
 * this intentionally lives only as long as the current tab's JS context.
 *
 * Built for Pipeline's Quick View optimization: open instantly from the
 * partial data a card already has, hydrate the full profile in the
 * background exactly once per talent per session, and never issue a
 * second request for a talent that's already cached or already in flight.
 */

const cache = new Map();    // talent_id -> hydrated talent object
const inFlight = new Map(); // talent_id -> Promise<talent object>

export function getCachedTalent(id) {
    return (id && cache.get(id)) || null;
}

export function setCachedTalent(id, talent) {
    if (id && talent) cache.set(id, talent);
}

/**
 * Returns the SAME in-flight promise for concurrent/rapid callers
 * requesting the same talent id, so a burst of clicks (or two cards
 * referencing the same talent) can never fire more than one network
 * request. Resolves to the fetched talent and caches it as a side effect.
 */
export function fetchTalentOnce(id, fetcher) {
    if (!id) return Promise.reject(new Error("fetchTalentOnce: missing id"));
    const existing = inFlight.get(id);
    if (existing) return existing;

    const promise = fetcher()
        .then((talent) => {
            setCachedTalent(id, talent);
            inFlight.delete(id);
            return talent;
        })
        .catch((err) => {
            inFlight.delete(id);
            throw err;
        });
    inFlight.set(id, promise);
    return promise;
}
