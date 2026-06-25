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
export async function directVideoUpload({ sid, token, category, label, file, isApplication, onProgress }) {
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

    // Fetch a signed payload. On the FIRST call public_id is minted server-side;
    // re-sign calls pass that SAME public_id so the chunked upload keeps the same
    // Cloudinary target (the backend validates it belongs to this target).
    const fetchSignature = async (publicId) => {
        const res = await api.post(
            signatureEndpoint,
            { category, label: label || null, content_type: file.type || null, public_id: publicId || null },
            { headers: authHeader }
        );
        const d = res.data;
        return {
            uploadUrl: d.upload_url,
            publicId: d.params.public_id,
            formParams: { ...d.params, api_key: d.api_key, timestamp: String(d.timestamp), signature: d.signature },
        };
    };

    // 1) Initial signed, server-pinned upload params.
    let sig = await fetchSignature(null);
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
        },
        { headers: authHeader }
    );
    return completeRes.data;
}
