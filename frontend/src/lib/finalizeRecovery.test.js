import { describe, it, expect, vi } from "vitest";
import {
    FINALIZE_TIMEOUT_MS,
    KEY_TTL_MS,
    newIdempotencyKey,
    loadFinalizeKey,
    saveFinalizeKey,
    clearFinalizeKey,
    isFinalizeOutcomeUnknown,
    recoverFinalizeOutcome,
    finalizeWithRecovery,
    createSingleFlight,
    RECOVERY_MESSAGES,
} from "./finalizeRecovery";

const canceled = (extra = {}) =>
    Object.assign(new Error("canceled"), { name: "CanceledError", code: "ERR_CANCELED", ...extra });
const http = (status, detail) => Object.assign(new Error("Request failed"), { response: { status, data: { detail } } });
const memStorage = () => {
    const m = new Map();
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, v), removeItem: (k) => m.delete(k), _m: m };
};

describe("isFinalizeOutcomeUnknown", () => {
    it("unknown when there is no HTTP response", () => {
        expect(isFinalizeOutcomeUnknown(canceled())).toBe(true);
        expect(isFinalizeOutcomeUnknown(canceled({ classification: "timeout" }))).toBe(true);
        expect(isFinalizeOutcomeUnknown(Object.assign(new Error("x"), { code: "ECONNABORTED" }))).toBe(true);
        // iOS Safari's dropped-connection error: a TypeError with no response
        expect(isFinalizeOutcomeUnknown(new TypeError("Load failed"))).toBe(true);
        expect(isFinalizeOutcomeUnknown(Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" }))).toBe(true);
    });
    it("unknown for gateway failures and 'still processing'", () => {
        for (const s of [408, 409, 502, 503, 504]) expect(isFinalizeOutcomeUnknown(http(s))).toBe(true);
    });
    it("definitive for validation / auth / not-found / server rejections", () => {
        for (const s of [400, 401, 403, 404, 422, 500]) expect(isFinalizeOutcomeUnknown(http(s, "nope"))).toBe(false);
    });
    it("a missing error is not unknown", () => {
        expect(isFinalizeOutcomeUnknown(null)).toBe(false);
    });
});

describe("recoverFinalizeOutcome", () => {
    const fast = { sleep: async () => {}, intervalMs: 1 };
    it("done as soon as the server says this attempt is done", async () => {
        const fetchState = vi.fn().mockResolvedValue("done");
        expect(await recoverFinalizeOutcome({ fetchState, ...fast })).toBe("done");
        expect(fetchState).toHaveBeenCalledTimes(1);
    });
    it("none / failed → not_submitted (safe to retry)", async () => {
        expect(await recoverFinalizeOutcome({ fetchState: async () => "none", ...fast })).toBe("not_submitted");
        expect(await recoverFinalizeOutcome({ fetchState: async () => "failed", ...fast })).toBe("not_submitted");
    });
    it("polls while in progress, then reports done", async () => {
        const states = ["in_progress", "in_progress", "done"];
        const fetchState = vi.fn(async () => states.shift());
        expect(await recoverFinalizeOutcome({ fetchState, ...fast })).toBe("done");
        expect(fetchState).toHaveBeenCalledTimes(3);
    });
    it("gives up with pending when it never leaves in_progress", async () => {
        let t = 0;
        const out = await recoverFinalizeOutcome({
            fetchState: async () => "in_progress", sleep: async () => { t += 2000; }, now: () => t, maxWaitMs: 6000,
        });
        expect(out).toBe("pending");
    });
    it("gives up with unknown when the status check itself keeps failing", async () => {
        const fetchState = vi.fn().mockRejectedValue(new Error("Network Error"));
        expect(await recoverFinalizeOutcome({ fetchState, ...fast, maxErrors: 3 })).toBe("unknown");
        expect(fetchState).toHaveBeenCalledTimes(3);
    });
    it("a backend without attempt tracking (404) is reported as unsupported, immediately — never as 'safe to retry'", async () => {
        const fetchState = vi.fn().mockRejectedValue(http(404, "Not Found"));
        expect(await recoverFinalizeOutcome({ fetchState, ...fast })).toBe("unsupported");
        expect(fetchState).toHaveBeenCalledTimes(1);
        expect(RECOVERY_MESSAGES.unsupported).toMatch(/reload this page/i);
        expect(RECOVERY_MESSAGES.unsupported).not.toMatch(/safe/i);
    });
    it("tolerates a transient status-check failure", async () => {
        const fetchState = vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValue("done");
        expect(await recoverFinalizeOutcome({ fetchState, ...fast })).toBe("done");
    });
});

describe("finalizeWithRecovery", () => {
    const fast = { recoverOptions: { sleep: async () => {}, intervalMs: 1 } };
    it("plain success: one POST, no status check", async () => {
        const post = vi.fn().mockResolvedValue({ data: { ok: true } });
        const fetchState = vi.fn();
        expect(await finalizeWithRecovery({ post, fetchState, ...fast })).toEqual({ outcome: "done", data: { ok: true } });
        expect(post).toHaveBeenCalledTimes(1);
        expect(fetchState).not.toHaveBeenCalled();
    });

    it("TIMEOUT then server confirms done → replays once (no third call), reports success", async () => {
        const post = vi.fn()
            .mockRejectedValueOnce(canceled({ classification: "timeout" }))
            .mockResolvedValueOnce({ data: { ok: true, replayed: true } });
        const out = await finalizeWithRecovery({ post, fetchState: async () => "done", ...fast });
        expect(out).toEqual({ outcome: "done", data: { ok: true, replayed: true }, replayed: true });
        expect(post).toHaveBeenCalledTimes(2);
    });

    it("TIMEOUT then server says never ran → no blind re-POST, honest not_submitted", async () => {
        const post = vi.fn().mockRejectedValue(canceled({ classification: "timeout" }));
        const out = await finalizeWithRecovery({ post, fetchState: async () => "none", ...fast });
        expect(out).toEqual({ outcome: "not_submitted" });
        expect(post).toHaveBeenCalledTimes(1);
    });

    it("an earlier finalized submission does not count: only THIS attempt's state decides", async () => {
        // The submission itself may already be 'submitted' from before; the
        // status endpoint is keyed by attempt and answers 'none'.
        const post = vi.fn().mockRejectedValue(canceled());
        const fetchState = vi.fn().mockResolvedValue("none");
        expect((await finalizeWithRecovery({ post, fetchState, ...fast })).outcome).toBe("not_submitted");
    });

    it("409 'still processing' waits for the attempt instead of failing", async () => {
        const post = vi.fn().mockRejectedValueOnce(http(409, "Your submission is still being processed."))
            .mockResolvedValueOnce({ data: { ok: true } });
        const states = ["in_progress", "done"];
        const out = await finalizeWithRecovery({ post, fetchState: async () => states.shift(), ...fast });
        expect(out.outcome).toBe("done");
    });

    it("NEW frontend against an OLD backend: timeout then 404 status route → no replay, honest 'check first' outcome", async () => {
        const post = vi.fn().mockRejectedValue(canceled({ classification: "timeout" }));
        const fetchState = vi.fn().mockRejectedValue(http(404, "Not Found"));
        const out = await finalizeWithRecovery({ post, fetchState, ...fast });
        expect(out).toEqual({ outcome: "unsupported" });
        expect(post).toHaveBeenCalledTimes(1);
    });

    it("a definitive rejection is re-thrown untouched (and not retried)", async () => {
        const err = http(400, "Height is required");
        const post = vi.fn().mockRejectedValue(err);
        const fetchState = vi.fn();
        await expect(finalizeWithRecovery({ post, fetchState, ...fast })).rejects.toBe(err);
        expect(post).toHaveBeenCalledTimes(1);
        expect(fetchState).not.toHaveBeenCalled();
    });

    it("every POST of an attempt carries the same key (the page binds it once)", async () => {
        const key = newIdempotencyKey();
        const seen = [];
        const post = vi.fn(async () => {
            seen.push(key);
            if (seen.length === 1) throw canceled({ classification: "timeout" });
            return { data: { ok: true } };
        });
        await finalizeWithRecovery({ post, fetchState: async () => "done", ...fast });
        expect(new Set(seen).size).toBe(1);
    });
});

describe("createSingleFlight (synchronous double-tap lock)", () => {
    it("two taps in the same tick run the work once", async () => {
        const run = createSingleFlight();
        const work = vi.fn(() => new Promise((r) => setTimeout(r, 5)));
        await Promise.all([run(work), run(work), run(work)]);
        expect(work).toHaveBeenCalledTimes(1);
    });
    it("is released after completion, even after a failure", async () => {
        const run = createSingleFlight();
        await expect(run(async () => { throw new Error("x"); })).rejects.toThrow("x");
        const work = vi.fn(async () => 1);
        await run(work);
        expect(work).toHaveBeenCalledTimes(1);
    });
});

describe("attempt key persistence (reload during a failed submit)", () => {
    it("a reload within the TTL reuses the same attempt key", () => {
        const storage = memStorage();
        saveFinalizeKey("sid1", "key-A", { storage, now: () => 1000 });
        expect(loadFinalizeKey("sid1", { storage, now: () => 1000 + 60_000 })).toBe("key-A");
    });
    it("an old key expires so a deliberate later update gets a NEW key", () => {
        const storage = memStorage();
        saveFinalizeKey("sid1", "key-A", { storage, now: () => 1000 });
        expect(loadFinalizeKey("sid1", { storage, now: () => 1000 + KEY_TTL_MS + 1 })).toBeNull();
        expect(storage._m.size).toBe(0);
    });
    it("is cleared when the attempt ends, and scoped per submission", () => {
        const storage = memStorage();
        saveFinalizeKey("sid1", "key-A", { storage });
        saveFinalizeKey("sid2", "key-B", { storage });
        clearFinalizeKey("sid1", { storage });
        expect(loadFinalizeKey("sid1", { storage })).toBeNull();
        expect(loadFinalizeKey("sid2", { storage })).toBe("key-B");
    });
    it("iPhone Safari private mode (storage throws) degrades to in-memory, never throws", () => {
        const storage = {
            getItem() { throw new Error("SecurityError"); },
            setItem() { throw new Error("QuotaExceededError"); },
            removeItem() { throw new Error("SecurityError"); },
        };
        expect(() => saveFinalizeKey("s", "k", { storage })).not.toThrow();
        expect(loadFinalizeKey("s", { storage })).toBeNull();
        expect(() => clearFinalizeKey("s", { storage })).not.toThrow();
    });
    it("corrupt stored value is ignored", () => {
        const storage = memStorage();
        storage.setItem("tg_finalize_attempt_s", "{not json");
        expect(loadFinalizeKey("s", { storage })).toBeNull();
    });
});

describe("newIdempotencyKey (older iPhone Safari lacks crypto.randomUUID)", () => {
    it("produces distinct server-acceptable keys", () => {
        const re = /^[A-Za-z0-9_-]{8,128}$/;
        const keys = new Set(Array.from({ length: 50 }, newIdempotencyKey));
        expect(keys.size).toBe(50);
        keys.forEach((k) => expect(k).toMatch(re));
    });
    it("falls back to getRandomValues, then Math.random, when randomUUID is missing", () => {
        const re = /^[A-Za-z0-9_-]{8,128}$/;
        const real = globalThis.crypto;
        try {
            vi.stubGlobal("crypto", { getRandomValues: (a) => { a.fill(171); return a; } });
            expect(newIdempotencyKey()).toMatch(re);
            vi.stubGlobal("crypto", undefined);
            expect(newIdempotencyKey()).toMatch(re);
        } finally {
            vi.unstubAllGlobals();
            if (real && !globalThis.crypto) vi.stubGlobal("crypto", real);
        }
    });
});

describe("timeout choice", () => {
    it("sits above the proxy's 25 s idle cut so a clean 504 wins over a bare client abort", () => {
        expect(FINALIZE_TIMEOUT_MS).toBeGreaterThan(25_000);
    });
});
