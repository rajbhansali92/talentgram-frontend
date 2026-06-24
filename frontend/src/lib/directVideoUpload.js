// Architecture C — direct browser → Cloudinary chunked audition-video upload.
// Activated only when NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD is enabled (see api.js).
// Railway never sees the video bytes: the browser uploads straight to Cloudinary
// using a short-lived signed payload, then notifies the backend to attach it.
import { api } from "./api";

const CHUNK_SIZE = 20 * 1024 * 1024; // 20 MB (>5 MB Cloudinary minimum)
const MAX_DURATION_SECONDS = 300; // 5 minutes
const MAX_CHUNK_ATTEMPTS = 4; // per-chunk network retries before failing the upload

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
        const fd = new FormData();
        Object.entries(formParams).forEach(([k, v]) => fd.append(k, v));
        fd.append("file", blob);
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && onProgress) onProgress(baseLoaded + e.loaded, total);
        };
        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
                } catch {
                    resolve({});
                }
            } else {
                reject(new Error(`Cloudinary upload failed (${xhr.status})`));
            }
        };
        xhr.onerror = () => reject(new Error("Network error during upload"));
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
export async function directVideoUpload({ sid, token, category, label, file, onProgress }) {
    // Client-side duration guard (server re-validates authoritatively).
    const duration = await readVideoDuration(file);
    if (duration != null && duration > MAX_DURATION_SECONDS) {
        const err = new Error(`Audition video must be ${MAX_DURATION_SECONDS / 60} minutes or less.`);
        err.noRetry = true;
        throw err;
    }

    const authHeader = token ? { Authorization: `Bearer ${token}` } : {};

    // 1) Signed, server-pinned upload params.
    const sigRes = await api.post(
        `/public/submissions/${sid}/video-signature`,
        { category, label: label || null, content_type: file.type || null },
        { headers: authHeader }
    );
    const s = sigRes.data;
    const formParams = {
        ...s.params,
        api_key: s.api_key,
        timestamp: String(s.timestamp),
        signature: s.signature,
    };

    // 2) Chunked direct upload to Cloudinary.
    const uploadId = `tg-${sid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const total = file.size;
    let lastResponse = {};
    for (let start = 0; start < total; start += CHUNK_SIZE) {
        const end = Math.min(start + CHUNK_SIZE, total);
        const blob = file.slice(start, end);
        lastResponse = await uploadChunkWithRetry(
            s.upload_url, formParams, blob, start, total, uploadId, onProgress, start
        );
    }

    // 3) Notify the backend to attach (finalize reconciliation is the safety net).
    const completeRes = await api.post(
        `/public/submissions/${sid}/video-complete`,
        {
            public_id: lastResponse.public_id || s.params.public_id,
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
