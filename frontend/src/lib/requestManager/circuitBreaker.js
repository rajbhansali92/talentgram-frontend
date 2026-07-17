import { DEFAULT_CIRCUIT_BREAKER_POLICY, CIRCUIT_GROUP_DEPTH } from "./constants";

// Per-key circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED|OPEN.
// Prevents retry storms against an endpoint that's consistently failing
// (e.g. Railway/DNS outage) by short-circuiting new attempts once a
// consecutive-failure threshold is hit, instead of letting every caller's
// own retry policy hammer the dead endpoint in parallel.

export const CircuitState = Object.freeze({
    CLOSED: "closed",
    OPEN: "open",
    HALF_OPEN: "half_open",
});

class Circuit {
    constructor(policy) {
        this.policy = policy;
        this.state = CircuitState.CLOSED;
        this.consecutiveFailures = 0;
        this.openedAt = null;
        this.lastAccessedAt = Date.now();
    }

    // Returns true if a request may proceed, and performs whatever state
    // transition that decision implies.
    //
    // HALF_OPEN correctness: this method has no `await` inside it, so a
    // single call is indivisible with respect to every other caller — there
    // is no interleaving window where two callers can both observe
    // "cooldown elapsed" and both win. The OPEN -> HALF_OPEN transition and
    // "grant exactly one probe" happen as ONE atomic synchronous step below:
    // only the specific canRequest() call that performs the transition
    // returns true. By construction, HALF_OPEN is only ever entered
    // together with granting its single probe in that same step, so any
    // call that observes state === HALF_OPEN already lost the race and
    // fails fast instead of dispatching a second concurrent probe.
    canRequest(now = Date.now()) {
        if (this.state === CircuitState.CLOSED) return true;

        if (this.state === CircuitState.OPEN) {
            if (now - this.openedAt < this.policy.cooldownMs) return false;
            this.state = CircuitState.HALF_OPEN;
            return true; // this call IS the single probe
        }

        // HALF_OPEN: a probe is already outstanding. Fail fast rather than
        // queue callers behind it — keeps the whole state machine
        // synchronous instead of introducing promise-based coordination
        // into what everything else here treats as instant bookkeeping.
        return false;
    }

    recordSuccess() {
        this.consecutiveFailures = 0;
        this.state = CircuitState.CLOSED;
        this.openedAt = null;
    }

    recordFailure(now = Date.now()) {
        if (this.state === CircuitState.HALF_OPEN) {
            // The probe failed — the endpoint hasn't recovered. Reopen with
            // a fresh cooldown rather than folding into consecutiveFailures,
            // so the next probe is always exactly one cooldown away.
            this.state = CircuitState.OPEN;
            this.openedAt = now;
            return;
        }
        this.consecutiveFailures += 1;
        if (this.consecutiveFailures >= this.policy.failureThreshold) {
            this.state = CircuitState.OPEN;
            this.openedAt = now;
        }
    }

    // The single outstanding HALF_OPEN probe was cancelled (e.g. the user
    // navigated away) before it could report a real success or failure. We
    // can't treat "we don't know" as a health confirmation (risks closing a
    // still-broken circuit) or as a genuine failure (the endpoint never
    // actually rejected anything) — the safe choice is to stay cautious and
    // reopen with a fresh cooldown, which also releases the probe slot so a
    // later request gets a real chance to check again. Without this, a
    // cancelled probe would leave the circuit stuck in HALF_OPEN forever,
    // since only recordSuccess/recordFailure ever move state out of it.
    recordCancelled(now = Date.now()) {
        if (this.state === CircuitState.HALF_OPEN) {
            this.state = CircuitState.OPEN;
            this.openedAt = now;
        }
        // CLOSED: a cancelled request tells us nothing about endpoint
        // health — no state change.
    }
}

export class CircuitBreaker {
    constructor(policy = {}) {
        this.policy = { ...DEFAULT_CIRCUIT_BREAKER_POLICY, ...policy };
        this._circuits = new Map();
    }

    _get(key) {
        const existing = this._circuits.get(key);
        if (existing) {
            existing.lastAccessedAt = Date.now();
            return existing;
        }
        // Cleanup: amortized TTL eviction. Rather than running a background
        // setInterval (another timer to manage and dispose of), the sweep
        // piggybacks on the one event that actually grows the map — a
        // brand-new circuit key being created. This is O(n) only on that
        // (comparatively rare, especially with the bounded grouping in
        // defaultCircuitKey below) event, keeps the map self-bounding, and
        // needs no dispose()/teardown lifecycle on CircuitBreaker itself.
        this._evictStale();
        const circuit = new Circuit(this.policy);
        this._circuits.set(key, circuit);
        return circuit;
    }

    _evictStale(now = Date.now()) {
        const ttl = this.policy.entryTtlMs;
        for (const [key, circuit] of this._circuits) {
            if (now - circuit.lastAccessedAt >= ttl) {
                this._circuits.delete(key);
            }
        }
    }

    canRequest(key) {
        return this._get(key).canRequest();
    }

    getState(key) {
        return this._get(key).state;
    }

    recordSuccess(key) {
        this._get(key).recordSuccess();
    }

    recordFailure(key) {
        this._get(key).recordFailure();
    }

    recordCancelled(key) {
        this._get(key).recordCancelled();
    }

    reset(key) {
        this._circuits.delete(key);
    }

    // Observability/test hook — current number of tracked circuit keys.
    get size() {
        return this._circuits.size;
    }
}

// ---------------------------------------------------------------------------
// Default circuit-breaker key: logical endpoint GROUP, not the exact
// resource path.
//
// Grouping strategy: method + the first CIRCUIT_GROUP_DEPTH (default 1)
// non-empty path segments, e.g.:
//   GET  /videos/64f2a9.../comments   -> "get:videos"
//   GET  /videos/71b3c0.../thumbnail  -> "get:videos"   (same group)
//   GET  /talent/9c11.../notes        -> "get:talent"
//   POST /public/apply                -> "post:public"
//   POST /portal/otp/verify           -> "post:portal"
//
// Why depth-capping instead of pattern-matching ids (numeric/UUID/ObjectId
// regexes): this codebase commonly uses human-readable SLUGS as resource
// identifiers (project slugs, talent ids), which are not reliably
// distinguishable from a legitimate static route segment by format alone —
// any regex that tries to guess "does this segment look like an id" will
// have false positives/negatives. Capping the grouping depth is mechanical
// and bounded instead: every request under `/videos/*`, no matter how many
// distinct video ids a session ever touches, shares the SAME circuit. This
// is the fix for the production-blocker finding that per-exact-path keying
// created one circuit per resource id — meaning a full backend outage on a
// resource-heavy page (many distinct ids) spread failures across hundreds
// of individually-healthy-looking circuits instead of tripping the small,
// bounded set of real logical endpoint groups.
//
// Trade-off, stated explicitly: this groups ALL sub-resources of a service
// together, so an isolated bug in one route under `/videos/*` can trip the
// breaker for unrelated `/videos/*` routes too. That's the standard,
// accepted trade-off circuit breakers make between outage-detection
// sensitivity and per-route isolation — a caller that needs finer isolation
// for a specific high-value endpoint can still pass an explicit
// `circuitKey` override per call.
export function defaultCircuitKey({ method, url }, { depth = CIRCUIT_GROUP_DEPTH } = {}) {
    let pathname = url;
    try {
        pathname = new URL(url, "http://placeholder.local").pathname;
    } catch {
        // Relative path strings without a valid URL parse fall back to the
        // raw url; still fine as input to the grouping below.
    }
    const segments = pathname.split("/").filter(Boolean).slice(0, depth);
    return `${String(method || "get").toLowerCase()}:${segments.join("/")}`;
}
