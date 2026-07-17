// Generates request ids compatible with the proxy/backend's existing
// `x-request-id` handling (both already honor an inbound id if present).
export function createRequestId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
    }
    // Fallback for environments without crypto.randomUUID (older Node/test
    // runners) — not cryptographically strong, but only ever used as a
    // correlation id, never a security token.
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
