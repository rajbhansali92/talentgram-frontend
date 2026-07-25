import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { putR2Once, putR2WithRetry } from "./directVideoUpload";

// Deterministic XHR double — lets each test script exactly what the R2 PUT
// does (succeed / network-error / reject-status / stall) without any real
// network or real timers, so retry/backoff/stall behaviour is provable on
// every CI run instead of relying on live device conditions.
class FakeXHR {
    constructor() {
        this.upload = {};
        this.status = 0;
        FakeXHR.instances.push(this);
    }
    open(method, url) {
        this.method = method;
        this.url = url;
    }
    send() {
        FakeXHR.onSend?.(this);
    }
    abort() {
        this.aborted = true;
        this.onabort?.();
    }
    // Test helpers to drive the double from outside.
    progress(loaded, total) {
        this.upload.onprogress?.({ lengthComputable: true, loaded, total });
    }
    // Fires when the request body has finished leaving the browser — after
    // this, no further upload-progress event can occur.
    bodySent() {
        this.upload.onload?.();
    }
    succeed() {
        this.status = 200;
        this.onload?.();
    }
    fail(status) {
        this.status = status;
        this.onload?.();
    }
    networkError() {
        this.onerror?.();
    }
}

describe("putR2Once", () => {
    let originalXHR;
    beforeEach(() => {
        originalXHR = global.XMLHttpRequest;
        FakeXHR.instances = [];
        FakeXHR.onSend = null;
        global.XMLHttpRequest = FakeXHR;
    });
    afterEach(() => {
        global.XMLHttpRequest = originalXHR;
    });

    it("resolves with bytes on a clean 2xx", async () => {
        FakeXHR.onSend = (xhr) => xhr.succeed();
        const file = { size: 1234 };
        const result = await putR2Once("https://r2.example/upload", file, () => {});
        expect(result.bytes).toBe(1234);
    });

    it("classifies xhr.onerror as network_interruption and marks it retryable", async () => {
        FakeXHR.onSend = (xhr) => xhr.networkError();
        await expect(putR2Once("https://r2.example/upload", { size: 10 }, () => {}))
            .rejects.toMatchObject({ errorType: "network_interruption", retryable: true });
    });

    it("classifies a non-2xx status as upload_rejected, 403 retryable via signature refresh", async () => {
        FakeXHR.onSend = (xhr) => xhr.fail(403);
        await expect(putR2Once("https://r2.example/upload", { size: 10 }, () => {}))
            .rejects.toMatchObject({ errorType: "upload_rejected", httpStatus: 403, retryable: true });
    });

    it("classifies a 400 as non-retryable (not a transient failure)", async () => {
        FakeXHR.onSend = (xhr) => xhr.fail(400);
        await expect(putR2Once("https://r2.example/upload", { size: 10 }, () => {}))
            .rejects.toMatchObject({ errorType: "upload_rejected", httpStatus: 400, retryable: false });
    });

    it("aborts and classifies as stalled when no progress event fires within the watchdog window", async () => {
        vi.useFakeTimers();
        FakeXHR.onSend = () => {}; // never resolves on its own
        const promise = putR2Once("https://r2.example/upload", { size: 10 }, () => {});
        const assertion = expect(promise).rejects.toMatchObject({ errorType: "stalled", retryable: true });
        await vi.advanceTimersByTimeAsync(60001);
        await assertion;
        vi.useRealTimers();
    });

    it("a progress event resets the stall watchdog (upload is genuinely alive)", async () => {
        vi.useFakeTimers();
        let xhrRef;
        FakeXHR.onSend = (xhr) => { xhrRef = xhr; };
        const promise = putR2Once("https://r2.example/upload", { size: 100 }, () => {});
        await vi.advanceTimersByTimeAsync(59000);
        xhrRef.progress(50, 100); // alive — rearms the 60s watchdog
        await vi.advanceTimersByTimeAsync(59000); // total elapsed 118s, but only 59s since last progress
        xhrRef.succeed();
        await expect(promise).resolves.toMatchObject({ bytes: 100 });
        vi.useRealTimers();
    });

    it("does not abort a finished upload that R2 takes longer than the progress watchdog to acknowledge", async () => {
        vi.useFakeTimers();
        let xhrRef;
        FakeXHR.onSend = (xhr) => { xhrRef = xhr; };
        const promise = putR2Once("https://r2.example/upload", { size: 100 }, () => {});
        xhrRef.progress(100, 100);
        xhrRef.bodySent(); // all bytes out — no progress event can fire again
        await vi.advanceTimersByTimeAsync(120000); // 2 min of legitimate commit time
        expect(xhrRef.aborted).toBeUndefined();
        xhrRef.succeed();
        await expect(promise).resolves.toMatchObject({ bytes: 100 });
        vi.useRealTimers();
    });

    it("still bounds the response wait so a server that never answers cannot hang forever", async () => {
        vi.useFakeTimers();
        let xhrRef;
        FakeXHR.onSend = (xhr) => { xhrRef = xhr; };
        const promise = putR2Once("https://r2.example/upload", { size: 100 }, () => {});
        const assertion = expect(promise).rejects.toMatchObject({ errorType: "stalled", retryable: true });
        xhrRef.bodySent();
        await vi.advanceTimersByTimeAsync(300001);
        await assertion;
        vi.useRealTimers();
    });
});

describe("putR2WithRetry", () => {
    let originalXHR;
    beforeEach(() => {
        originalXHR = global.XMLHttpRequest;
        FakeXHR.instances = [];
        global.XMLHttpRequest = FakeXHR;
    });
    afterEach(() => {
        global.XMLHttpRequest = originalXHR;
    });

    it("recovers from a transient network error on the second attempt using a fresh XHR", async () => {
        let call = 0;
        FakeXHR.onSend = (xhr) => {
            call += 1;
            if (call === 1) xhr.networkError();
            else xhr.succeed();
        };
        const onRetryStatus = vi.fn();
        vi.useFakeTimers();
        const promise = putR2WithRetry({
            uploadUrl: "https://r2.example/upload",
            file: { size: 55 },
            onProgress: () => {},
            onRetryStatus,
        });
        await vi.advanceTimersByTimeAsync(1001); // first backoff (1s)
        const result = await promise;
        vi.useRealTimers();

        expect(result.bytes).toBe(55);
        expect(call).toBe(2); // exactly one retry, each a fresh FakeXHR instance
        expect(FakeXHR.instances.length).toBe(2);
        expect(onRetryStatus).toHaveBeenCalledWith(
            expect.objectContaining({ attempt: 2, maxAttempts: 4, reason: "network_interruption" })
        );
    });

    it("refreshes the presigned URL on a 403 before retrying", async () => {
        let call = 0;
        const urlsUsed = [];
        FakeXHR.onSend = (xhr) => {
            urlsUsed.push(xhr.url);
            call += 1;
            if (call === 1) xhr.fail(403);
            else xhr.succeed();
        };
        const refreshUploadUrl = vi.fn().mockResolvedValue("https://r2.example/upload-FRESH");
        vi.useFakeTimers();
        const promise = putR2WithRetry({
            uploadUrl: "https://r2.example/upload-STALE",
            file: { size: 9 },
            onProgress: () => {},
            refreshUploadUrl,
        });
        await vi.advanceTimersByTimeAsync(1001);
        await promise;
        vi.useRealTimers();

        expect(refreshUploadUrl).toHaveBeenCalledTimes(1);
        expect(urlsUsed).toEqual(["https://r2.example/upload-STALE", "https://r2.example/upload-FRESH"]);
    });

    it("gives up after the bounded attempt count and surfaces the last classified error", async () => {
        FakeXHR.onSend = (xhr) => xhr.networkError();
        vi.useFakeTimers();
        const promise = putR2WithRetry({
            uploadUrl: "https://r2.example/upload",
            file: { size: 9 },
            onProgress: () => {},
        });
        const assertion = expect(promise).rejects.toMatchObject({ errorType: "network_interruption" });
        // 4 attempts total → 3 backoff waits: 1s, 2s, 4s
        await vi.advanceTimersByTimeAsync(1001);
        await vi.advanceTimersByTimeAsync(2001);
        await vi.advanceTimersByTimeAsync(4001);
        await assertion;
        vi.useRealTimers();
        expect(FakeXHR.instances.length).toBe(4); // bounded, not infinite
    });

    it("does not retry a non-retryable rejection (e.g. 400)", async () => {
        FakeXHR.onSend = (xhr) => xhr.fail(400);
        await expect(
            putR2WithRetry({ uploadUrl: "https://r2.example/upload", file: { size: 9 }, onProgress: () => {} })
        ).rejects.toMatchObject({ errorType: "upload_rejected", httpStatus: 400 });
        expect(FakeXHR.instances.length).toBe(1); // no retry attempted
    });
});
