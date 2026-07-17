import {
    DEFAULT_RETRY_POLICY,
    IDEMPOTENT_METHODS,
    RETRYABLE_CLASSIFICATIONS,
    RETRYABLE_CLIENT_STATUSES,
} from "./constants";

// Decides whether a given attempt's failure should be retried, and computes
// the exponential-backoff-with-jitter delay before the next attempt.

export function isRetryable({ classification, status, method, idempotent }) {
    const methodIsIdempotent = IDEMPOTENT_METHODS.has(String(method || "get").toLowerCase()) || idempotent === true;
    if (!methodIsIdempotent) return false;

    if (classification === "client_error") {
        return typeof status === "number" && RETRYABLE_CLIENT_STATUSES.has(status);
    }
    return RETRYABLE_CLASSIFICATIONS.has(classification);
}

// attempt is 1-indexed (the attempt that just failed).
export function computeBackoffDelay(attempt, policy = {}) {
    const { baseDelayMs, factor, maxDelayMs, jitterRatio } = { ...DEFAULT_RETRY_POLICY, ...policy };
    const raw = Math.min(baseDelayMs * Math.pow(factor, attempt - 1), maxDelayMs);
    const jitter = raw * jitterRatio * (Math.random() * 2 - 1); // +/- jitterRatio
    return Math.max(0, Math.round(raw + jitter));
}

export function shouldRetry(attempt, policy = {}) {
    const { maxAttempts } = { ...DEFAULT_RETRY_POLICY, ...policy };
    return attempt < maxAttempts;
}
