// Safe submit for the talent audition form (incident 2026-10-14: a request
// aborted client-side showed the raw text "canceled", and the talent had no
// way to know whether finalize had run).
//
// Finalize is NOT safe to repeat blindly — a second call counts as a retest
// (resets the admin decision, re-notifies). So every submit attempt carries
// one `Idempotency-Key`, reused for retries of THAT attempt only; the server
// replays a completed attempt without side effects. When the client does not
// know the outcome, it asks the server about this exact attempt (not "is the
// submission submitted?", which an earlier finalize would also answer yes to).

import { isCancelOrTimeoutError } from "./errorFormatter";

// Finalize normally takes ~7 s server-side (measured in production). The
// Vercel proxy cuts an idle upstream at 25 s and answers 504, so a client
// limit just above that lets the proxy's clean 504 win the race instead of
// a bare client abort.
export const FINALIZE_TIMEOUT_MS = 35_000;
export const STATUS_TIMEOUT_MS = 10_000;

// A remembered key survives a page reload (the talent's natural reaction to
// a failed submit) but must not outlive the attempt it belongs to — a
// deliberate "Update submission" later needs a NEW key.
export const KEY_TTL_MS = 10 * 60 * 1000;
const LS_PREFIX = "tg_finalize_attempt_";

export function newIdempotencyKey() {
    try {
        if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
            return crypto.randomUUID();
        }
        if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
            const b = new Uint8Array(16);
            crypto.getRandomValues(b);
            return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
        }
    } catch (e) {
        // fall through
    }
    return `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}${Math.random().toString(36).slice(2, 12)}`;
}

function safeStorage(storage) {
    if (storage) return storage;
    try {
        return typeof window !== "undefined" ? window.localStorage : null;
    } catch (e) {
        return null;
    }
}

export function loadFinalizeKey(sid, { storage, now = Date.now } = {}) {
    const st = safeStorage(storage);
    if (!st || !sid) return null;
    try {
        const raw = st.getItem(LS_PREFIX + sid);
        if (!raw) return null;
        const { key, at } = JSON.parse(raw);
        if (typeof key === "string" && typeof at === "number" && now() - at < KEY_TTL_MS) return key;
        st.removeItem(LS_PREFIX + sid);
    } catch (e) {
        // unreadable → behave as "no remembered key"
    }
    return null;
}

export function saveFinalizeKey(sid, key, { storage, now = Date.now } = {}) {
    const st = safeStorage(storage);
    if (!st || !sid || !key) return;
    try {
        st.setItem(LS_PREFIX + sid, JSON.stringify({ key, at: now() }));
    } catch (e) {
        // private mode / quota: the in-memory key still protects this page load
    }
}

export function clearFinalizeKey(sid, { storage } = {}) {
    const st = safeStorage(storage);
    if (!st || !sid) return;
    try {
        st.removeItem(LS_PREFIX + sid);
    } catch (e) {
        // ignore
    }
}

// True when the client cannot tell whether the server ran finalize: no HTTP
// response at all (abort, timeout, network drop, offline, circuit open), a
// gateway-level failure, or the server saying this very attempt is still
// being processed (409).
export function isFinalizeOutcomeUnknown(err) {
    if (!err) return false;
    if (isCancelOrTimeoutError(err)) return true;
    const status = err.response?.status;
    if (!err.response) return true;
    return status === 408 || status === 409 || status === 502 || status === 503 || status === 504;
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Resolves to:
//   "done"          this attempt completed on the server
//   "pending"       the server is still working on it
//   "not_submitted" the server never completed this attempt (safe to retry)
//   "unknown"       the status check itself kept failing
//   "unsupported"   the server has no attempt tracking (older backend): a
//                   repeat submit is not protected, so the user must check first
export async function recoverFinalizeOutcome({
    fetchState,
    sleep = defaultSleep,
    now = Date.now,
    maxWaitMs = 30_000,
    intervalMs = 2_000,
    maxErrors = 3,
}) {
    const deadline = now() + maxWaitMs;
    let errors = 0;
    for (;;) {
        let state = null;
        try {
            state = await fetchState();
        } catch (e) {
            // A backend that predates attempt tracking has no status route.
            // Repeating the submit there is NOT idempotent, so say so.
            if (e?.response?.status === 404) return "unsupported";
            errors += 1;
            if (errors >= maxErrors) return "unknown";
        }
        if (state === "done") return "done";
        if (state === "none" || state === "failed") return "not_submitted";
        if (now() >= deadline) return state === "in_progress" ? "pending" : "unknown";
        await sleep(intervalMs);
    }
}

export const RECOVERY_MESSAGES = {
    not_submitted:
        "We couldn't confirm your submission because the connection was interrupted. Nothing was lost — please tap Submit again.",
    pending:
        "Your submission is still being processed. Please wait a moment, then tap Submit again to confirm.",
    unknown:
        "We couldn't confirm whether your submission went through. Please check your connection and tap Submit again — it is safe to do so.",
    unsupported:
        "We couldn't confirm whether your submission went through. Please reload this page and check its status before submitting again.",
};

// The whole submit orchestration, kept out of the page so it can be tested
// without rendering it. `post()` performs the finalize request (always with
// the same Idempotency-Key for this attempt); `fetchState()` asks the server
// about that same attempt.
//
//   { outcome: "done", data }   finalized (possibly confirmed after a lost reply)
//   { outcome: "pending" | "not_submitted" | "unknown" }   no data; show the
//                                matching RECOVERY_MESSAGES entry
// A definitive server answer (e.g. 400 "Height is required") is re-thrown.
export async function finalizeWithRecovery({ post, fetchState, recoverOptions }) {
    try {
        const res = await post();
        return { outcome: "done", data: res.data };
    } catch (err) {
        if (!isFinalizeOutcomeUnknown(err)) throw err;
        const outcome = await recoverFinalizeOutcome({ fetchState, ...recoverOptions });
        if (outcome !== "done") return { outcome };
        // Completed on the server: the same key replays the stored result
        // (and portal session) without repeating any side effect.
        const res = await post();
        return { outcome: "done", data: res.data, replayed: true };
    }
}

// Synchronous single-flight guard (React state updates are asynchronous, so a
// second tap in the same moment would otherwise get through).
export function createSingleFlight() {
    let busy = false;
    return async function run(fn) {
        if (busy) return undefined;
        busy = true;
        try {
            return await fn();
        } finally {
            busy = false;
        }
    };
}
