import { RequestManager } from "./RequestManager";
import { ErrorClassification } from "./errorClassifier";

// A Request Manager's own `.request()` resolves/rejects with its own
// normalized envelope/error shape (see RequestManager.js), not an axios
// response/error. That's the right general-purpose contract, but it breaks
// every existing call site that reads `err.response.status`,
// `err.code === "ECONNABORTED"`, etc. This shim sits on top of a Request
// Manager and translates back to an axios-compatible surface, so an axios
// instance can be swapped for "the same instance, backed by Request
// Manager" with zero call-site changes anywhere in the app.
//
// Timeout backstop, applied consistently across every method below: if the
// caller passes an axios-style `timeout` (a number of ms), it's forwarded
// to the underlying transport UNCHANGED — so if that transport is a real
// axios instance, axios's own native timeout mechanism remains the one
// that actually fires, preserving its native `ECONNABORTED` error shape
// exactly. Request Manager's own timeoutMs is set to
// `timeout + TIMEOUT_BACKSTOP_MS` — a safety net only, arranged to lose
// the race under normal conditions. If the caller passes no `timeout` at
// all, Request Manager's own default timeout applies as a *new* safety
// net for a call that could otherwise hang indefinitely — a deliberate
// resilience improvement, not a regression: no existing call site has
// timeout-specific error handling for a call that never had a timeout.
const TIMEOUT_BACKSTOP_MS = 5000;

function withTimeoutBackstop(config) {
    if (typeof config.timeout !== "number") return config;
    return { ...config, timeoutMs: config.timeout + TIMEOUT_BACKSTOP_MS };
}

// The only Request Manager failure with no underlying transport error to
// unwrap: the circuit was open, so the call never reached a transport at
// all. Synthesized to look enough like a real axios network-style error
// (`isAxiosError: true`, no `.response`) that existing `!err.response`
// checks and `formatErrorDetail()`'s fallback chain degrade exactly the
// way they already do for a genuine network error, rather than crashing
// on an unfamiliar shape.
function synthesizeAxiosLikeError(rmError, config) {
    const err = new Error(
        rmError.classification === ErrorClassification.CIRCUIT_OPEN
            ? "This service is temporarily unavailable. Please try again in a moment."
            : rmError.message
    );
    err.isAxiosError = true;
    err.isRequestManagerSynthesized = true;
    err.config = config;
    err.response = undefined;
    err.request = undefined;
    err.code = rmError.classification === ErrorClassification.CIRCUIT_OPEN ? "ERR_CIRCUIT_OPEN" : "ERR_NETWORK";
    return err;
}

// Request Manager's own requestId/attempt/durationMs/classification exist
// only transiently inside RequestManager.js today — they get serialized
// into its structured log line and are otherwise discarded once translated
// back to an axios-compatible shape. Attaching them here (as additional,
// non-conflicting properties — never touching `.data`/`.status`/`.headers`
// or any existing error field) is what makes them reachable by a caller
// for diagnostics, without changing anything about the existing response/
// error shape callers already depend on.
function attachDiagnostics(target, rmResultOrError) {
    if (!target || !rmResultOrError) return target;
    target.requestId = rmResultOrError.requestId;
    if (rmResultOrError.attempt !== undefined) target.attempt = rmResultOrError.attempt;
    if (rmResultOrError.durationMs !== undefined) target.durationMs = rmResultOrError.durationMs;
    if (rmResultOrError.classification !== undefined) target.classification = rmResultOrError.classification;
    return target;
}

// Wraps `transportFn` (a Request Manager transport: `(config) =>
// Promise<response>`) in a Request Manager, then exposes an
// axios-instance-compatible surface on top of it.
export function createAxiosCompatibleClient(transportFn, requestManagerOptions = {}) {
    const requestManager = new RequestManager({ transport: transportFn, ...requestManagerOptions });

    async function request(config) {
        try {
            const result = await requestManager.request(withTimeoutBackstop(config));
            // Reconstructed, not the original AxiosResponse reference —
            // audit-verified every consumer of this instance only ever
            // reads `.data`/`.status`/`.headers` on success (never
            // `.statusText`/`.request`), so this is sufficient fidelity
            // without Request Manager's core envelope needing to carry the
            // raw response through.
            return attachDiagnostics(
                {
                    data: result.data,
                    status: result.status,
                    headers: result.headers,
                    statusText: "",
                    config,
                    request: undefined,
                },
                result
            );
        } catch (rmError) {
            // `.cause` is the original, untouched error the transport
            // itself threw (see RequestManager.js's _makeError) — for a
            // real axios transport, that's the literal axios error axios
            // threw, so every existing `err.response`/`err.code` check
            // downstream keeps working unmodified. Diagnostics from the
            // Request Manager layer (requestId/attempt/classification) are
            // attached on top, as additional properties only.
            throw attachDiagnostics(rmError?.cause || synthesizeAxiosLikeError(rmError, config), rmError);
        }
    }

    return {
        request,
        get: (url, config = {}) => request({ ...config, method: "get", url }),
        post: (url, data, config = {}) => request({ ...config, method: "post", url, data }),
        put: (url, data, config = {}) => request({ ...config, method: "put", url, data }),
        patch: (url, data, config = {}) => request({ ...config, method: "patch", url, data }),
        delete: (url, config = {}) => request({ ...config, method: "delete", url }),
        // Escape hatch for tests / future direct use — not part of the
        // axios-compatible surface itself.
        _requestManager: requestManager,
    };
}
