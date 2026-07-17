// Classifies any failure the Request Manager can encounter into a stable,
// small vocabulary. Mirrors the reason strings already used by the Phase 1
// proxy's classifyUpstreamError (frontend/src/app/api/proxy/[...path]/route.js)
// where they overlap (e.g. "dns_failure"), so logs read consistently across
// the client and the proxy layer.

export const ErrorClassification = Object.freeze({
    TIMEOUT: "timeout",
    OFFLINE: "offline",
    DNS_FAILURE: "dns_failure",
    CLIENT_ERROR: "client_error",
    SERVER_ERROR: "server_error",
    MALFORMED_RESPONSE: "malformed_response",
    CANCELLED: "cancelled",
    ABORTED: "aborted",
    NETWORK_ERROR: "network_error",
    CIRCUIT_OPEN: "circuit_open",
});

function isOffline() {
    return typeof navigator !== "undefined" && navigator.onLine === false;
}

// `reason` lets callers short-circuit classification for cases the manager
// already knows about (explicit cancel(), circuit breaker open) instead of
// re-deriving them from the underlying error shape.
export function classifyError(err, { reason, timedOut = false, offline } = {}) {
    const isOfflineNow = offline !== undefined ? offline : isOffline();

    if (reason === "cancelled") return { classification: ErrorClassification.CANCELLED, status: null };
    if (reason === "circuit_open") return { classification: ErrorClassification.CIRCUIT_OPEN, status: null };

    if (isOfflineNow) return { classification: ErrorClassification.OFFLINE, status: null };

    if (timedOut) return { classification: ErrorClassification.TIMEOUT, status: null };

    if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") {
        return { classification: ErrorClassification.ABORTED, status: null };
    }

    if (err?.name === "SyntaxError" || err?.isMalformedResponse) {
        return { classification: ErrorClassification.MALFORMED_RESPONSE, status: null };
    }

    const status = err?.response?.status ?? err?.status ?? null;
    if (typeof status === "number") {
        if (status >= 500) return { classification: ErrorClassification.SERVER_ERROR, status };
        if (status >= 400) return { classification: ErrorClassification.CLIENT_ERROR, status };
    }

    const code = err?.cause?.code || err?.code;
    if (code === "ENOTFOUND" || code === "EAI_AGAIN") {
        return { classification: ErrorClassification.DNS_FAILURE, status: null };
    }
    if (
        code === "ECONNREFUSED" ||
        code === "ECONNRESET" ||
        code === "EPIPE" ||
        code === "ERR_NETWORK" ||
        err?.message === "Network Error"
    ) {
        return { classification: ErrorClassification.NETWORK_ERROR, status: null };
    }

    return { classification: ErrorClassification.NETWORK_ERROR, status: null };
}
