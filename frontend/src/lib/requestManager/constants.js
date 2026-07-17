// Shared defaults for the Request Manager. See
// frontend/src/lib/requestManager/axiosShim.js and
// frontend/src/lib/publicApiTransport.js for how these are wired into the
// public `api` instance's live traffic.

export const REQUEST_TIMEOUT_MS = 15_000;
export const UPLOAD_TIMEOUT_MS = 120_000;
export const DOWNLOAD_TIMEOUT_MS = 120_000;

export const TIMEOUT_CATEGORIES = {
    request: REQUEST_TIMEOUT_MS,
    upload: UPLOAD_TIMEOUT_MS,
    download: DOWNLOAD_TIMEOUT_MS,
};

export const DEFAULT_RETRY_POLICY = {
    maxAttempts: 3,
    baseDelayMs: 300,
    factor: 2,
    maxDelayMs: 5_000,
    jitterRatio: 0.2,
};

// Idempotent-by-default HTTP methods. POST is only retried when a call
// explicitly opts in via `idempotent: true` on the request config.
export const IDEMPOTENT_METHODS = new Set(["get", "head", "put", "delete"]);

// Failure classifications worth retrying. Client errors (4xx) are never
// retried except the two that represent transient/rate-limit conditions.
export const RETRYABLE_CLASSIFICATIONS = new Set([
    "timeout",
    "server_error",
    "network_error",
    "dns_failure",
]);
export const RETRYABLE_CLIENT_STATUSES = new Set([408, 429]);

export const DEFAULT_CIRCUIT_BREAKER_POLICY = {
    failureThreshold: 5,
    cooldownMs: 30_000,
    // Circuit entries untouched for this long are evicted the next time a
    // new circuit key is created (see CircuitBreaker._evictStale in
    // circuitBreaker.js) — bounds Map growth without a background timer.
    entryTtlMs: 30 * 60_000,
};

// Default path-segment depth used to derive a circuit-breaker "logical
// endpoint group" key (see defaultCircuitKey in circuitBreaker.js). Depth 1
// groups e.g. /videos/{id}/comments and /videos/{id}/thumbnail under the
// same "videos" circuit instead of one circuit per resource id.
export const CIRCUIT_GROUP_DEPTH = 1;

// Matches the header name already minted/honored by the Phase 1 reverse
// proxy (frontend/src/app/api/proxy/[...path]/route.js) and the backend's
// RequestIdMiddleware (backend/server.py) — a client-generated id now flows
// end-to-end with zero backend changes, since both already prefer an
// inbound id over minting their own.
export const REQUEST_ID_HEADER = "x-request-id";
