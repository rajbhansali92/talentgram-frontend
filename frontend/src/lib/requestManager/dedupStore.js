// In-flight request registry. Provides two related-but-distinct primitives:
//
// 1. Deduplication — a second call with the same key while one is already
//    in-flight joins the same promise instead of firing a second request.
// 2. Race-condition protection — every key has a monotonically increasing
//    "generation" counter. A caller that wants to replace (not join) an
//    in-flight request bumps the generation and aborts the old one; when the
//    old request's response arrives late anyway, it carries a stale
//    generation number so the caller can tell it's no longer current.

// Fast, deterministic, non-cryptographic 32-bit hash (FNV-1a). Used only to
// fingerprint request params/body for the dedup key below — NOT a security
// control (see the comment on defaultDedupKey for what that does and
// doesn't protect against).
function fnv1aHash(str) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < str.length; i += 1) {
        hash ^= str.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
}

function extractPathname(url) {
    try {
        return new URL(url, "http://placeholder.local").pathname;
    } catch {
        return url;
    }
}

// ---------------------------------------------------------------------------
// Dedup key algorithm
//
// Key shape: `${method}:${path}:${paramsFingerprint}:${dataFingerprint}`
//
// `method` and `path` are structural (route identity) — never sensitive.
// `paramsFingerprint`/`dataFingerprint` are FNV-1a hashes of the
// canonicalized (key-sorted, so field order never matters) params/body —
// never the raw values. This keeps the key able to tell two DIFFERENT
// requests to the same route apart (different body/params -> different
// hash, so they're correctly treated as distinct calls rather than wrongly
// deduped into one) without the key — or anything derived from it,
// including every structured log line that includes `key` — ever
// containing a request body, an OTP, a phone number, an email, a token, a
// custom_answers value, or an uploaded filename in readable form.
//
// Explicitly NOT a security boundary: FNV-1a is not cryptographic, and a
// hash of a LOW-ENTROPY value (e.g. a 6-digit OTP) could in principle be
// brute-forced by someone who already has read access to this key/log
// data — a much smaller exposure than today's plaintext leak, but not
// zero. For an endpoint whose body IS the sensitive secret (e.g. an
// OTP-verify call), don't rely on the default: pass an explicit,
// non-content-derived `key` (e.g. `key: "otp-verify"`) instead — deduping
// by exact OTP value doesn't make semantic sense anyway, since two
// different OTP attempts are never "the same request".
export function defaultDedupKey({ method, url, params, data }) {
    const path = extractPathname(url);
    const paramsFingerprint = params ? fnv1aHash(JSON.stringify(sortKeys(params))) : "0";
    const dataFingerprint =
        data === undefined || data === null
            ? "0"
            : fnv1aHash(typeof data === "object" ? JSON.stringify(sortKeys(data)) : String(data));
    return `${String(method || "get").toLowerCase()}:${path}:${paramsFingerprint}:${dataFingerprint}`;
}

function sortKeys(obj) {
    if (Array.isArray(obj)) return obj.map(sortKeys);
    if (obj && typeof obj === "object") {
        return Object.keys(obj)
            .sort()
            .reduce((acc, k) => {
                acc[k] = sortKeys(obj[k]);
                return acc;
            }, {});
    }
    return obj;
}

export class DedupStore {
    constructor() {
        this._entries = new Map();
    }

    // Returns the existing entry for `key` if one is in-flight, else null.
    get(key) {
        return this._entries.get(key) || null;
    }

    // Registers a new in-flight entry for `key`, bumping its generation.
    // `mode: "replace"` aborts and discards any previous in-flight entry for
    // the same key first (used by typeahead-style callers); the default
    // "dedupe" mode assumes the caller already checked get() and found none.
    start(key, { controller, promise, mode = "dedupe" } = {}) {
        const previous = this._entries.get(key);
        if (mode === "replace" && previous) {
            previous.controller?.abort();
        }
        const generation = (previous?.generation || 0) + 1;
        const entry = { controller, promise, generation };
        this._entries.set(key, entry);
        return entry;
    }

    // Clears the entry for `key` only if it's still the one identified by
    // `generation` — prevents a late-finishing stale request from wiping out
    // a newer in-flight entry for the same key.
    finish(key, generation) {
        const current = this._entries.get(key);
        if (current && current.generation === generation) {
            this._entries.delete(key);
        }
    }

    isStale(key, generation) {
        const current = this._entries.get(key);
        return !current || current.generation !== generation;
    }

    cancel(key, reason) {
        const entry = this._entries.get(key);
        if (entry) {
            entry.controller?.abort(reason);
            this._entries.delete(key);
        }
    }

    cancelAll(reason) {
        for (const [key, entry] of this._entries) {
            entry.controller?.abort(reason);
            this._entries.delete(key);
        }
    }
}
