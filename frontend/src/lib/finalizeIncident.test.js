import { describe, it, expect, vi } from "vitest";
import { createAxiosCompatibleClient } from "./requestManager/axiosShim";
import { formatErrorDetail, INTERRUPTED_REQUEST_MESSAGE } from "./errorFormatter";
import { finalizeWithRecovery, isFinalizeOutcomeUnknown } from "./finalizeRecovery";

// Reproduces the 2026-10-14 incident chain with the REAL Request Manager and
// axios shim: a finalize POST that stalls (never reaches the server) is
// aborted by the Request Manager's timeout; the shim rethrows the raw axios
// CanceledError; before the fix the toast then read "canceled".
function stallingTransport() {
    return vi.fn((config) => new Promise((_resolve, reject) => {
        config.signal.addEventListener("abort", () => {
            const err = new Error("canceled");
            err.name = "CanceledError";
            err.code = "ERR_CANCELED";
            reject(err);
        });
    }));
}

describe("incident chain: stalled finalize → Request Manager timeout → toast", () => {
    it("surfaces as an interrupted-connection message, never 'canceled'", async () => {
        const client = createAxiosCompatibleClient(stallingTransport(), {});
        let caught;
        try {
            await client.post("/public/submissions/s/finalize", {}, { timeoutMs: 30 });
        } catch (e) {
            caught = e;
        }
        expect(caught).toBeTruthy();
        // The raw error really is the misleading one…
        expect(caught.message).toBe("canceled");
        expect(caught.classification).toBe("timeout");
        // …and the user-facing text no longer is.
        expect(formatErrorDetail(caught, "Please complete all required fields")).toBe(INTERRUPTED_REQUEST_MESSAGE);
        // The page treats it as "outcome unknown" → asks the server, never blind-repeats.
        expect(isFinalizeOutcomeUnknown(caught)).toBe(true);
    });

    it("the page flow: stalled POST → server confirms this attempt → replay, one success", async () => {
        const stalled = stallingTransport();
        const client = createAxiosCompatibleClient(stalled, {});
        let calls = 0;
        const post = async () => {
            calls += 1;
            if (calls === 1) return client.post("/public/submissions/s/finalize", {}, { timeoutMs: 30 });
            return { data: { ok: true, replayed: true } };
        };
        const out = await finalizeWithRecovery({
            post,
            fetchState: async () => "done",
            recoverOptions: { sleep: async () => {} },
        });
        expect(out.outcome).toBe("done");
        expect(calls).toBe(2);
    });
});
