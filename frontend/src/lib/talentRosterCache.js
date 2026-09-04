/**
 * Session-scoped snapshot of a talent-roster browsing session — filters,
 * sort, page, the already-fetched result page, and scroll position — so
 * navigating away (e.g. opening a talent profile) and back can restore the
 * exact previous view instantly instead of remounting to page 1 with a
 * fresh fetch. Same pattern as talentPreviewCache.js: one module-level
 * store, survives client-side route changes, resets on a real page reload.
 * No backend changes, no persistence API, no new dependency.
 *
 * Opt-in only: useTalentDirectory reads/writes this ONLY when called with
 * a `persistKey`. Global Talent (/admin/talents) passes one; no other
 * current caller of the hook does, so this cannot change behaviour for
 * any other consumer (e.g. a future Browse Roster caller).
 */
const _snapshots = new Map(); // key -> snapshot object

/** Returns the stored snapshot for `key`, or null if none exists yet. */
export function getRosterSnapshot(key) {
    return (key && _snapshots.get(key)) || null;
}

/** Shallow-merges `patch` into the stored snapshot for `key` (creating it if absent). */
export function setRosterSnapshot(key, patch) {
    if (!key) return;
    _snapshots.set(key, { ..._snapshots.get(key), ...patch });
}

/** Drops the stored snapshot for `key`, if any. */
export function clearRosterSnapshot(key) {
    if (!key) return;
    _snapshots.delete(key);
}
