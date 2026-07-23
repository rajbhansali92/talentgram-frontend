// Architecture C — direct browser → Cloudinary chunked audition-video upload.
// Activated only when NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD is enabled (see api.js).
// Railway never sees the video bytes: the browser uploads straight to Cloudinary
// using a short-lived signed payload, then notifies the backend to attach it.
import { api } from "./api";

const CHUNK_SIZE = 20 * 1024 * 1024; // 20 MB (>5 MB Cloudinary minimum)
const MAX_DURATION_SECONDS = 300; // 5 minutes
const MAX_CHUNK_ATTEMPTS = 4; // per-chunk network retries before failing the upload
const STALL_TIMEOUT_MS = 60000; // abort a chunk that makes NO upload progress for 60s
// Re-sign the (constant) public_id every 30 min — comfortably under Cloudinary's
// ~1h signed-request validity — so multi-hour uploads on weak mobile never fail
// with a stale signature/timestamp.
const SIGNATURE_REFRESH_MS = 30 * 60 * 1000;

// ── R2 direct-PUT transport (P0 upload-reliability fix) ──────────────────
// The R2 path was originally a single unchunked, unretried, unwatched PUT —
// unlike the Cloudinary path above it sits beside, which already had
// per-chunk retries and a stall watchdog. That asymmetry (not anything
// Cloudinary-specific) is what let one transient mobile network blip during
// a multi-hundred-MB upload become an unrecoverable "R2 upload network
// error". These constants intentionally mirror the Cloudinary path's
// already-proven values rather than inventing new tuning.
const R2_MAX_ATTEMPTS = 4;
const R2_STALL_TIMEOUT_MS = 60000; // no upload-progress event for 60s == stalled
const R2_RETRY_BASE_MS = 1000; // 1s, 2s, 4s …
// The final "attach to the app/submission" POST runs AFTER the bytes are
// already safely in R2 — retrying just this small call (instead of falling
// back to a full re-upload) avoids re-sending a 500 MB file because of one
// blipped POST at the very end.
const MAX_COMPLETE_ATTEMPTS = 3;
const COMPLETE_RETRY_BASE_MS = 1500;

function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
}

// Lightweight, dependency-free device/browser signal for telemetry — not a
// full UA-parser, just enough to tell "which real-world device reported
// this" apart in production logs.
function getClientInfo() {
    try {
        const ua = navigator.userAgent || "";
        let browser = "unknown";
        if (/SamsungBrowser/i.test(ua)) browser = "Samsung Internet";
        else if (/CriOS/i.test(ua)) browser = "Chrome (iOS)";
        else if (/FxiOS/i.test(ua)) browser = "Firefox (iOS)";
        else if (/EdgiOS|Edg\//i.test(ua)) browser = "Edge";
        else if (/Chrome/i.test(ua)) browser = "Chrome";
        else if (/CriOS/i.test(ua)) browser = "Chrome (iOS)";
        else if (/Safari/i.test(ua) && !/Chrome/i.test(ua)) browser = "Safari";
        else if (/Firefox/i.test(ua)) browser = "Firefox";

        let os = "unknown";
        if (/iPhone|iPad|iPod/i.test(ua)) os = "iOS";
        else if (/Android/i.test(ua)) os = "Android";
        else if (/Mac OS X/i.test(ua)) os = "macOS";
        else if (/Windows/i.test(ua)) os = "Windows";

        const conn = navigator.connection || navigator.webkitConnection || navigator.mozConnection;
        return {
            browser,
            os,
            device: /iPad|Tablet/i.test(ua) ? "tablet" : /Mobi/i.test(ua) ? "mobile" : "desktop",
            network_effective_type: conn?.effectiveType || null,
            network_downlink_mbps: conn?.downlink ?? null,
            user_agent: ua.slice(0, 200),
        };
    } catch {
        return {};
    }
}

// Fire-and-forget telemetry beacon. MUST NEVER throw or block the upload —
// a diagnostics call failing is not the user's problem.
function sendUploadTelemetry(endpoint, token, payload) {
    try {
        const headers = token ? { Authorization: `Bearer ${token}` } : {};
        api.post(endpoint, { ...payload, client: getClientInfo() }, { headers }).catch(() => {});
    } catch {
        /* noop — telemetry is best-effort only */
    }
}

// Single R2 PUT attempt with a fresh XHR + fresh stall watchdog every call —
// "retry" never reuses a prior attempt's transport or connection state.
export function putR2Once(uploadUrl, file, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", uploadUrl, true);

        let stallTimer = null;
        const armStall = () => {
            if (stallTimer) clearTimeout(stallTimer);
            stallTimer = setTimeout(() => {
                try { xhr.abort(); } catch (_) { /* noop */ }
            }, R2_STALL_TIMEOUT_MS);
        };
        const clearStall = () => {
            if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
        };

        xhr.upload.onprogress = (e) => {
            armStall(); // progress → still alive → reset the watchdog
            if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
        };
        xhr.onload = () => {
            clearStall();
            if (xhr.status >= 200 && xhr.status < 300) {
                resolve({ bytes: file.size });
            } else {
                const err = new Error(`R2 upload was rejected by the server (${xhr.status})`);
                err.errorType = "upload_rejected";
                err.httpStatus = xhr.status;
                // 403 on an R2 presigned PUT means the signature is expired
                // or invalid — retryable via a fresh signature, unlike other
                // 4xx which usually indicate a genuinely malformed request.
                err.retryable = xhr.status === 403 || xhr.status >= 500;
                reject(err);
            }
        };
        xhr.onerror = () => {
            clearStall();
            const err = new Error("R2 upload network error");
            err.errorType = "network_interruption";
            err.retryable = true;
            reject(err);
        };
        xhr.onabort = () => {
            clearStall();
            const err = new Error("R2 upload stalled — no progress for 60s");
            err.errorType = "stalled";
            err.retryable = true;
            reject(err);
        };
        armStall(); // start the watchdog before sending
        xhr.send(file);
    });
}

// Bounded, intelligent retry around putR2Once: fresh transport every attempt,
// exponential backoff, and — specifically for an expired/invalid signature
// (403) — a fresh presigned URL before the next attempt rather than blindly
// re-hammering a dead signature. Progress necessarily resets to 0 on each
// retry (this is a single whole-file PUT, not a resumable multipart upload —
// see the file-level note on why that's an explicit, separate future
// enhancement, not folded into this fix); onRetryStatus tells the caller so
// the UI can say so honestly instead of implying continuity that isn't real.
export async function putR2WithRetry({ uploadUrl, file, onProgress, onRetryStatus, refreshUploadUrl }) {
    let currentUrl = uploadUrl;
    let lastErr = null;
    for (let attempt = 1; attempt <= R2_MAX_ATTEMPTS; attempt++) {
        try {
            return await putR2Once(currentUrl, file, onProgress);
        } catch (err) {
            lastErr = err;
            const retryable = err.retryable !== false && attempt < R2_MAX_ATTEMPTS;
            if (!retryable) break;

            if (onRetryStatus) {
                onRetryStatus({ attempt: attempt + 1, maxAttempts: R2_MAX_ATTEMPTS, reason: err.errorType });
            }

            // Expired/invalid signature — get a fresh presigned URL before
            // retrying instead of re-sending against a dead one.
            if (err.errorType === "upload_rejected" && err.httpStatus === 403 && refreshUploadUrl) {
                try {
                    currentUrl = await refreshUploadUrl();
                } catch {
                    // Keep the old URL — the retry will just fail again and
                    // surface the real error rather than masking it.
                }
            }

            await sleep(R2_RETRY_BASE_MS * Math.pow(2, attempt - 1));
        }
    }
    throw lastErr;
}

// Read a video's duration from its file metadata (client-side guard).
function readVideoDuration(file) {
    return new Promise((resolve) => {
        try {
            const url = URL.createObjectURL(file);
            const v = document.createElement("video");
            v.preload = "metadata";
            v.onloadedmetadata = () => {
                const d = v.duration;
                URL.revokeObjectURL(url);
                resolve(Number.isFinite(d) ? d : null);
            };
            v.onerror = () => {
                URL.revokeObjectURL(url);
                resolve(null);
            };
            v.src = url;
        } catch {
            resolve(null);
        }
    });
}

function uploadChunk(uploadUrl, formParams, blob, start, total, uploadId, onProgress, baseLoaded) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", uploadUrl, true);
        xhr.setRequestHeader("X-Unique-Upload-Id", uploadId);
        xhr.setRequestHeader("Content-Range", `bytes ${start}-${start + blob.size - 1}/${total}`);

        // No-progress watchdog: a chunk that uploads bytes is "slow but alive"
        // (each progress event resets the timer); a chunk that makes NO progress
        // for STALL_TIMEOUT_MS is aborted → surfaced as a retryable error. This
        // bounds stalled connections (and backgrounded/suspended uploads) so they
        // never hang forever and never deadlock the Submit button.
        let stallTimer = null;
        const armStall = () => {
            if (stallTimer) clearTimeout(stallTimer);
            stallTimer = setTimeout(() => {
                try { xhr.abort(); } catch (_) { /* noop */ }
            }, STALL_TIMEOUT_MS);
        };
        const clearStall = () => {
            if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
        };

        const fd = new FormData();
        Object.entries(formParams).forEach(([k, v]) => fd.append(k, v));
        fd.append("file", blob);
        xhr.upload.onprogress = (e) => {
            armStall(); // progress → still alive → reset the watchdog
            if (e.lengthComputable && onProgress) onProgress(baseLoaded + e.loaded, total);
        };
        xhr.onload = () => {
            clearStall();
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
                } catch {
                    resolve({});
                }
            } else {
                let errorBody = "";
                try { errorBody = xhr.responseText || ""; } catch (_) { /* noop */ }
                console.error(`[directVideoUpload] Cloudinary upload failed (${xhr.status}):`, errorBody);
                const err = new Error(`Cloudinary upload failed (${xhr.status}): ${errorBody}`);
                err.cloudinaryResponse = errorBody;
                err.httpStatus = xhr.status;
                reject(err);
            }
        };
        xhr.onerror = () => { clearStall(); reject(new Error("Network error during upload")); };
        xhr.onabort = () => { clearStall(); reject(new Error("Upload stalled — no progress")); };
        armStall(); // start the watchdog before sending
        xhr.send(fd);
    });
}

// Upload one chunk with bounded retries + exponential backoff. Because the
// caller's loop only advances `start` after a chunk resolves, retrying the
// SAME chunk here means a network blip resumes from the last successful chunk
// (never restarts the whole video from byte 0).
async function uploadChunkWithRetry(uploadUrl, formParams, blob, start, total, uploadId, onProgress, baseLoaded) {
    let lastErr = null;
    for (let attempt = 1; attempt <= MAX_CHUNK_ATTEMPTS; attempt++) {
        try {
            return await uploadChunk(uploadUrl, formParams, blob, start, total, uploadId, onProgress, baseLoaded);
        } catch (err) {
            lastErr = err;
            if (err?.noRetry || attempt === MAX_CHUNK_ATTEMPTS) break;
            // 1s, 2s, 4s …
            await new Promise((r) => setTimeout(r, 1000 * Math.pow(2, attempt - 1)));
        }
    }
    throw lastErr;
}

// Returns the backend /video-complete response: { ok, media }.
export async function directVideoUpload({ sid, token, category, label, file, isApplication, onProgress, onRetryStatus, preFetchedSig }) {
    // Client-side duration guard (server re-validates authoritatively).
    const duration = await readVideoDuration(file);
    if (duration != null && duration > MAX_DURATION_SECONDS) {
        const err = new Error(`Audition video must be ${MAX_DURATION_SECONDS / 60} minutes or less.`);
        err.noRetry = true;
        throw err;
    }

    const authHeader = token ? { Authorization: `Bearer ${token}` } : {};

    const signatureEndpoint = isApplication
        ? `/public/apply/${sid}/video-signature`
        : `/public/submissions/${sid}/video-signature`;

    const completeEndpoint = isApplication
        ? `/public/apply/${sid}/video-complete`
        : `/public/submissions/${sid}/video-complete`;

    const uploadEventEndpoint = isApplication
        ? `/public/apply/${sid}/video-upload-event`
        : `/public/submissions/${sid}/video-upload-event`;

    const fetchSignature = async (publicId) => {
        let res;
        try {
            res = await api.post(
                signatureEndpoint,
                { category, label: label || null, content_type: file.type || null, public_id: publicId || null },
                { headers: authHeader }
            );
        } catch (err) {
            const wrapped = new Error("Couldn't start the upload — please check your connection and try again.");
            wrapped.errorType = "signature_failure";
            wrapped.cause = err;
            throw wrapped;
        }
        const d = res.data;
        if (d.use_r2) {
            return {
                use_r2: true,
                uploadUrl: d.upload_url,
                publicId: d.public_id,
            };
        }
        return {
            use_r2: false,
            uploadUrl: d.upload_url,
            publicId: d.params.public_id,
            formParams: { ...d.params, api_key: d.api_key, timestamp: String(d.timestamp), signature: d.signature },
        };
    };

    // 1) Initial signed, server-pinned upload params.
    let sig = preFetchedSig || await fetchSignature(null);

    if (sig.use_r2) {
        const uploadStartedAt = Date.now();
        let retryCount = 0;

        sendUploadTelemetry(uploadEventEndpoint, token, {
            public_id: sig.publicId,
            stage: "upload_started",
            bytes_transferred: 0,
        });

        let putResult;
        try {
            putResult = await putR2WithRetry({
                uploadUrl: sig.uploadUrl,
                file,
                onProgress,
                onRetryStatus: (info) => {
                    retryCount = info.attempt - 1;
                    sendUploadTelemetry(uploadEventEndpoint, token, {
                        public_id: sig.publicId,
                        stage: "upload_retry",
                        error_type: info.reason,
                        retry_count: retryCount,
                    });
                    if (onRetryStatus) {
                        onRetryStatus({ ...info, phase: "uploading" });
                    }
                },
                refreshUploadUrl: async () => {
                    const fresh = await fetchSignature(sig.publicId);
                    return fresh.uploadUrl;
                },
            });
        } catch (err) {
            sendUploadTelemetry(uploadEventEndpoint, token, {
                public_id: sig.publicId,
                stage: "upload_failed",
                error_type: err.errorType || "network_interruption",
                error_message: err.message,
                retry_count: retryCount,
                upload_duration_ms: Date.now() - uploadStartedAt,
            });
            // Re-throw with the classification intact so the UI (and the
            // caller's telemetry-free retry button) can show the real reason
            // instead of a generic message.
            throw err;
        }

        sendUploadTelemetry(uploadEventEndpoint, token, {
            public_id: sig.publicId,
            stage: "r2_upload_complete",
            retry_count: retryCount,
            bytes_transferred: putResult.bytes,
            upload_duration_ms: Date.now() - uploadStartedAt,
        });

        // 3) Notify the backend to attach. The bytes are already safely in
        // R2 at this point — retry just this small call a few times before
        // giving up, rather than forcing a full re-upload of a possibly
        // 500 MB file over a blipped final POST. (Finalize-time reconcile
        // remains the last-resort safety net if even this exhausts.)
        let completeErr = null;
        for (let attempt = 1; attempt <= MAX_COMPLETE_ATTEMPTS; attempt++) {
            try {
                const completeRes = await api.post(
                    completeEndpoint,
                    {
                        public_id: sig.publicId,
                        secure_url: null,
                        resource_type: "video",
                        bytes: putResult.bytes || 0,
                        duration: duration,
                        format: null,
                        label: label || null,
                    },
                    { headers: authHeader }
                );
                sendUploadTelemetry(uploadEventEndpoint, token, {
                    public_id: sig.publicId,
                    stage: "completed",
                    retry_count: retryCount,
                    upload_duration_ms: Date.now() - uploadStartedAt,
                });
                return completeRes.data;
            } catch (err) {
                completeErr = err;
                if (attempt === MAX_COMPLETE_ATTEMPTS) break;
                await sleep(COMPLETE_RETRY_BASE_MS * Math.pow(2, attempt - 1));
            }
        }

        sendUploadTelemetry(uploadEventEndpoint, token, {
            public_id: sig.publicId,
            stage: "completion_callback_failed",
            error_type: "backend_persistence_failure",
            error_message: completeErr?.message,
            retry_count: retryCount,
            upload_duration_ms: Date.now() - uploadStartedAt,
        });
        const wrapped = new Error(
            "Your video uploaded successfully, but we couldn't save it to your application — tap Retry to finish."
        );
        wrapped.errorType = "backend_persistence_failure";
        wrapped.cause = completeErr;
        throw wrapped;
    }

    let signedAt = Date.now();
    const publicId = sig.publicId; // constant target for all chunks + re-signs

    // 2) Chunked direct upload to Cloudinary, re-signing proactively so a long
    //    upload never carries a stale timestamp on a late chunk.
    const uploadId = `tg-${sid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const total = file.size;
    let lastResponse = {};
    for (let start = 0; start < total; start += CHUNK_SIZE) {
        if (Date.now() - signedAt > SIGNATURE_REFRESH_MS) {
            sig = await fetchSignature(publicId); // refresh signature, same public_id
            signedAt = Date.now();
        }
        const end = Math.min(start + CHUNK_SIZE, total);
        const blob = file.slice(start, end);
        lastResponse = await uploadChunkWithRetry(
            sig.uploadUrl, sig.formParams, blob, start, total, uploadId, onProgress, start
        );
    }

    // 3) Notify the backend to attach (finalize reconciliation is the safety net).
    const completeRes = await api.post(
        completeEndpoint,
        {
            public_id: lastResponse.public_id || publicId,
            secure_url: lastResponse.secure_url || lastResponse.url || null,
            resource_type: "video",
            bytes: lastResponse.bytes || 0,
            duration: lastResponse.duration != null ? lastResponse.duration : duration,
            format: lastResponse.format || null,
            label: label || null,
        },
        { headers: authHeader }
    );
    return completeRes.data;
}
