import { REQUEST_ID_HEADER, TIMEOUT_CATEGORIES, REQUEST_TIMEOUT_MS, DEFAULT_RETRY_POLICY } from "./constants";
import { createRequestId } from "./requestId";
import { classifyError, ErrorClassification } from "./errorClassifier";
import { DedupStore, defaultDedupKey } from "./dedupStore";
import { CircuitBreaker, defaultCircuitKey } from "./circuitBreaker";
import { isRetryable, computeBackoffDelay, shouldRetry } from "./retryPolicy";
import { createLogger } from "./logger";

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// Bounded in-memory timeline for diagnostics/support use — see getHistory().
// A fixed cap keeps this from growing unbounded over a long-lived session;
// 50 is generous for "what just happened" troubleshooting without holding
// onto history indefinitely.
const HISTORY_LIMIT = 50;

// The single shared networking primitive. Wraps a pluggable `transport`
// function — not a hardcoded axios instance — so the same manager instance
// can later point at the Phase 1 reverse proxy, a different backend, or an
// upload-specific transport without any change to this class. See
// frontend/src/lib/requestManager/axiosShim.js for the default axios adapter.
export class RequestManager {
    constructor({
        transport,
        dedupKeyFn = defaultDedupKey,
        circuitKeyFn = defaultCircuitKey,
        retryPolicy = {},
        circuitBreakerPolicy = {},
        onLog,
    } = {}) {
        if (typeof transport !== "function") {
            throw new Error("RequestManager requires a `transport` function: (config) => Promise<response>");
        }
        this.transport = transport;
        this.dedupKeyFn = dedupKeyFn;
        this.circuitKeyFn = circuitKeyFn;
        this.defaultRetryPolicy = { ...DEFAULT_RETRY_POLICY, ...retryPolicy };
        this.circuitBreaker = new CircuitBreaker(circuitBreakerPolicy);
        this.dedupStore = new DedupStore();
        this._log = createLogger(onLog);
        this._history = [];
    }

    // Read-only timeline for diagnostics/support tooling — see
    // frontend/src/lib/clientDiagnostics.js. Purely observational: records
    // what the retry/circuit/dedup logic above already decided, never
    // influences it. Newest first.
    getHistory() {
        return this._history.slice().reverse();
    }

    _recordHistory(entry) {
        this._history.push({ timestamp: new Date().toISOString(), ...entry });
        if (this._history.length > HISTORY_LIMIT) this._history.shift();
    }

    // config: { method, url, params, data, headers, key, mode, timeoutMs,
    //   timeoutCategory, retry, idempotent, signal, circuitKey, transportKind }
    //   transportKind is optional, caller-supplied diagnostic metadata only
    //   (e.g. "proxy" vs "railway-direct") — recorded in getHistory(), never
    //   read by any retry/circuit/dedup decision.
    // Resolves to { data, status, headers, requestId, attempt, durationMs, isStale }.
    // Rejects with an Error carrying { classification, status, requestId, cause }.
    async request(config) {
        const method = String(config.method || "get").toLowerCase();
        const fullConfig = { ...config, method };
        const key = config.key || this.dedupKeyFn(fullConfig);
        const circuitKey = config.circuitKey || this.circuitKeyFn(fullConfig);
        const mode = config.mode || "dedupe";

        if (!this.circuitBreaker.canRequest(circuitKey)) {
            this._log("warn", {
                method,
                url: config.url,
                key,
                circuit_state: this.circuitBreaker.getState(circuitKey),
                outcome: "short_circuited",
            });
            this._recordHistory({
                requestId: null,
                method,
                url: config.url,
                transport: config.transportKind,
                attempts: 0,
                totalDurationMs: 0,
                outcome: "short_circuited",
                classification: ErrorClassification.CIRCUIT_OPEN,
                circuitState: this.circuitBreaker.getState(circuitKey),
            });
            throw this._makeError({
                classification: ErrorClassification.CIRCUIT_OPEN,
                status: null,
                requestId: null,
                cause: null,
            });
        }

        if (mode === "dedupe") {
            const existing = this.dedupStore.get(key);
            if (existing) {
                this._log("info", { method, url: config.url, key, outcome: "dedup_joined" });
                this._recordHistory({
                    requestId: null,
                    method,
                    url: config.url,
                    transport: config.transportKind,
                    attempts: 0,
                    totalDurationMs: 0,
                    outcome: "dedup_joined",
                });
                return existing.promise;
            }
        }

        const masterController = new AbortController();
        if (config.signal) {
            if (config.signal.aborted) masterController.abort("cancelled");
            else config.signal.addEventListener("abort", () => masterController.abort("cancelled"), { once: true });
        }

        // Reserved synchronously (before any await) so the dedup entry —
        // including its generation number — exists before the async attempt
        // loop below ever checks it, and so a "replace" mode call aborts the
        // previous entry's controller before this one starts.
        const entry = this.dedupStore.start(key, { controller: masterController, promise: null, mode });
        const promise = this._executeWithRetry(fullConfig, masterController, key, circuitKey, entry.generation).finally(
            () => this.dedupStore.finish(key, entry.generation)
        );
        entry.promise = promise;
        return promise;
    }

    async _executeWithRetry(config, masterController, key, circuitKey, generation) {
        const timeoutMs = config.timeoutMs || TIMEOUT_CATEGORIES[config.timeoutCategory] || REQUEST_TIMEOUT_MS;
        const retryPolicy = { ...this.defaultRetryPolicy, ...config.retry };
        const requestId = createRequestId();
        const requestStartedAt = Date.now();
        let attempt = 0;

        // eslint-disable-next-line no-constant-condition
        while (true) {
            attempt += 1;
            const attemptStartedAt = Date.now();
            const attemptController = new AbortController();
            let timedOut = false;

            const onMasterAbort = () => attemptController.abort(masterController.signal.reason);
            if (masterController.signal.aborted) attemptController.abort(masterController.signal.reason);
            else masterController.signal.addEventListener("abort", onMasterAbort);

            const timeoutId = setTimeout(() => {
                timedOut = true;
                attemptController.abort("timeout");
            }, timeoutMs);

            try {
                const headers = { ...(config.headers || {}), [REQUEST_ID_HEADER]: requestId };
                const response = await this.transport({ ...config, headers, signal: attemptController.signal });

                clearTimeout(timeoutId);
                masterController.signal.removeEventListener("abort", onMasterAbort);
                const durationMs = Date.now() - attemptStartedAt;
                this.circuitBreaker.recordSuccess(circuitKey);
                this._log("info", {
                    request_id: requestId,
                    method: config.method,
                    url: config.url,
                    key,
                    attempt,
                    duration_ms: durationMs,
                    status: response?.status ?? null,
                    outcome: "success",
                    circuit_state: this.circuitBreaker.getState(circuitKey),
                });
                this._recordHistory({
                    requestId,
                    method: config.method,
                    url: config.url,
                    transport: config.transportKind,
                    attempts: attempt,
                    totalDurationMs: Date.now() - requestStartedAt,
                    outcome: "completed",
                    status: response?.status ?? null,
                    circuitState: this.circuitBreaker.getState(circuitKey),
                });

                return {
                    data: response?.data,
                    status: response?.status ?? null,
                    headers: response?.headers,
                    requestId,
                    attempt,
                    durationMs,
                    isStale: this.dedupStore.isStale(key, generation),
                };
            } catch (err) {
                clearTimeout(timeoutId);
                masterController.signal.removeEventListener("abort", onMasterAbort);
                const durationMs = Date.now() - attemptStartedAt;
                const cancelled = masterController.signal.aborted;

                const { classification, status } = classifyError(err, {
                    timedOut,
                    reason: cancelled ? "cancelled" : undefined,
                });
                const normalizedErr = this._makeError({ classification, status, requestId, cause: err });

                if (classification === ErrorClassification.CANCELLED) {
                    // If this attempt was the circuit's single HALF_OPEN
                    // probe, it needs an explicit outcome — otherwise a
                    // cancelled probe leaves the circuit stuck in
                    // HALF_OPEN forever, since only recordSuccess/
                    // recordFailure ever move it out of that state.
                    this.circuitBreaker.recordCancelled(circuitKey);
                } else {
                    this.circuitBreaker.recordFailure(circuitKey);
                }

                this._log(classification === ErrorClassification.CANCELLED ? "info" : "error", {
                    request_id: requestId,
                    method: config.method,
                    url: config.url,
                    key,
                    attempt,
                    duration_ms: durationMs,
                    status,
                    classification,
                    outcome: "failure",
                    circuit_state: this.circuitBreaker.getState(circuitKey),
                });

                if (classification === ErrorClassification.CANCELLED) {
                    this._recordHistory({
                        requestId,
                        method: config.method,
                        url: config.url,
                        transport: config.transportKind,
                        attempts: attempt,
                        totalDurationMs: Date.now() - requestStartedAt,
                        outcome: "cancelled",
                        classification,
                        circuitState: this.circuitBreaker.getState(circuitKey),
                    });
                    throw normalizedErr;
                }

                const retryable =
                    isRetryable({ classification, status, method: config.method, idempotent: config.idempotent }) &&
                    this.circuitBreaker.canRequest(circuitKey);

                if (retryable && shouldRetry(attempt, retryPolicy)) {
                    await sleep(computeBackoffDelay(attempt, retryPolicy));
                    continue;
                }

                this._recordHistory({
                    requestId,
                    method: config.method,
                    url: config.url,
                    transport: config.transportKind,
                    attempts: attempt,
                    totalDurationMs: Date.now() - requestStartedAt,
                    outcome: "failed",
                    status,
                    classification,
                    circuitState: this.circuitBreaker.getState(circuitKey),
                });
                throw normalizedErr;
            }
        }
    }

    _makeError({ classification, status, requestId, cause }) {
        const err = new Error(`Request failed: ${classification}${status ? ` (${status})` : ""}`);
        err.isRequestManagerError = true;
        err.classification = classification;
        err.status = status;
        err.requestId = requestId;
        err.cause = cause;
        return err;
    }

    cancel(key, reason = "cancelled") {
        this.dedupStore.cancel(key, reason);
    }

    cancelAll(reason = "cancelled") {
        this.dedupStore.cancelAll(reason);
    }

    get(url, config = {}) {
        return this.request({ ...config, method: "get", url });
    }
    post(url, data, config = {}) {
        return this.request({ ...config, method: "post", url, data });
    }
    put(url, data, config = {}) {
        return this.request({ ...config, method: "put", url, data });
    }
    patch(url, data, config = {}) {
        return this.request({ ...config, method: "patch", url, data });
    }
    delete(url, config = {}) {
        return this.request({ ...config, method: "delete", url });
    }
}
