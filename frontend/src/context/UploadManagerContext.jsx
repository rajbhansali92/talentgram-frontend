"use client";

import React, { createContext, useContext, useState, useEffect, useRef } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import FloatingUploadManager from "../components/shared/FloatingUploadManager";
import axios from "axios";
import { directVideoUpload } from "../lib/directVideoUpload";

// Submission audition videos go through the chunked browser→Cloudinary
// transport (directVideoUpload). Detected by the submission upload endpoint;
// the apply flow keeps the single-POST path.
const CHUNKED_VIDEO_ENDPOINT_RE = /\/public\/(submissions|apply)\/([^/]+)\/upload\/?$/;

// Phase 6/7 — bounded concurrency. A minimal FIFO semaphore: `acquire()`
// resolves immediately if a slot is free, otherwise the caller queues until
// `release()` is called by whoever is holding a slot. Deliberately simple —
// no priority, no cancellation. This is Upload Engine plumbing only — it has
// no idea what "queued" means to the UI; that translation happens entirely
// in the Operational Engine (lib/readinessStatus.js), which already had a
// `queued` mapping waiting for this (see its RAW_STATUS_TO_OPERATIONAL
// table).
//
// Phase 7 correctness fix: compression and upload transport are gated by
// TWO SEPARATE instances of this, not one shared gate. lib/videoCompress.js
// holds a single module-level FFmpeg instance — one shared Web Worker, one
// shared virtual filesystem, one shared progress/log event-listener array
// with no per-job attribution. Two `compressVideoIfNeeded()` calls in
// flight at once can interleave their writeFile/exec/readFile sequence on
// that shared filesystem and cross-contaminate each other's progress
// events — a real data-corruption risk, not just a performance concern (see
// 08_DECISION_LOG.md / 09_PRESENTATION_MODELS.md context for the Phase 7
// audit that found this). Capping compression at concurrency 1 makes that
// interleaving impossible. Keeping it as a SEPARATE gate from upload
// transport (still concurrency 2) also means a video's compression never
// ties up a network-transport slot an image upload could be using.
function createConcurrencyGate(maxConcurrent) {
    let active = 0;
    const waiters = [];
    return {
        acquire() {
            return new Promise((resolve) => {
                if (active < maxConcurrent) {
                    active += 1;
                    resolve();
                } else {
                    waiters.push(resolve);
                }
            });
        },
        release() {
            const next = waiters.shift();
            if (next) {
                next(); // hand the freed slot straight to the next waiter — `active` count is unchanged
            } else {
                active = Math.max(0, active - 1);
            }
        },
    };
}

const MAX_CONCURRENT_UPLOADS = 2;
// Matches the true safe concurrency of the shared FFmpeg singleton in
// lib/videoCompress.js — see the comment above. Not a performance knob.
const MAX_CONCURRENT_COMPRESSIONS = 1;


const UploadManagerContext = createContext(null);

export function useUploadManager() {
    const context = useContext(UploadManagerContext);
    if (!context) {
        throw new Error("useUploadManager must be used within an UploadManagerProvider");
    }
    return context;
}

export function UploadManagerProvider({ children }) {
    const [activeUploads, setActiveUploads] = useState({});
    const [retryQueue, setRetryQueue] = useState({});
    const inFlightUploads = useRef({});
    // Two independent gates per provider instance (singletons, same lifetime
    // as activeUploads) — every uploadFile() call across the page shares
    // both, so a burst of selected files is throttled globally, not per call
    // site. Kept SEPARATE (Phase 7): a video waiting on the compression gate
    // never occupies an upload-transport slot, and vice versa.
    const compressionGate = useRef(createConcurrencyGate(MAX_CONCURRENT_COMPRESSIONS)).current;
    const uploadGate = useRef(createConcurrencyGate(MAX_CONCURRENT_UPLOADS)).current;

    // Session-lifetime "N Completed" tally for the Upload Activity Panel
    // (FloatingUploadManager). Purely a UI concern — the upload engine itself
    // still prunes a completed `activeUploads[slotKey]` entry 3s after
    // success (unchanged below), so the raw map alone can't answer "how many
    // uploads has this session completed" once that grace window passes.
    // This just watches the same state transitions and keeps a running count;
    // it doesn't feed back into, alter, or duplicate any upload/retry logic.
    const [completedCount, setCompletedCount] = useState(0);
    const countedCompletionsRef = useRef({}); // slotKey -> already counted for its current lifecycle
    useEffect(() => {
        let newlyCompleted = 0;
        Object.entries(activeUploads).forEach(([slotKey, upload]) => {
            if (upload.status === "completed") {
                if (!countedCompletionsRef.current[slotKey]) {
                    countedCompletionsRef.current[slotKey] = true;
                    newlyCompleted += 1;
                }
            } else {
                // Not (or no longer) completed — e.g. a re-upload to the same
                // slot has started a fresh lifecycle. Clear the flag so its
                // next completion is counted again.
                delete countedCompletionsRef.current[slotKey];
            }
        });
        if (newlyCompleted > 0) {
            setCompletedCount((c) => c + newlyCompleted);
        }
        // Forget slots that have been pruned from activeUploads entirely
        // (the engine's 3s post-success cleanup), so a future re-upload to
        // the same slot counts as a new completion rather than being seen
        // as "already counted".
        Object.keys(countedCompletionsRef.current).forEach((slotKey) => {
            if (!(slotKey in activeUploads)) delete countedCompletionsRef.current[slotKey];
        });
    }, [activeUploads]);

    // Guard against accidental navigation/refresh while an upload is in flight.
    // The in-flight File cannot be re-read after a reload, so warn before the
    // page unloads. (Mitigates the controllable-loss case; OS-level mobile tab
    // eviction can still occur and is handled by re-upload + finalize reconcile.)
    useEffect(() => {
        const hasActive = Object.values(activeUploads).some(
            (u) => ["uploading", "processing", "queued", "waiting", "retrying"].includes(u.status)
        );
        if (!hasActive) return;
        const handler = (e) => {
            e.preventDefault();
            e.returnValue = "An upload is still in progress. Leaving now will lose it.";
            return e.returnValue;
        };
        window.addEventListener("beforeunload", handler);
        return () => window.removeEventListener("beforeunload", handler);
    }, [activeUploads]);

    // Background Tab Detection: warn when tab goes background during active video compression
    useEffect(() => {
        const handleVisibilityChange = () => {
            if (document.visibilityState === "hidden") {
                const hasActiveCompression = Object.values(activeUploads).some(
                    (u) => u.status === "compressing"
                );
                if (hasActiveCompression) {
                    toast.warning("Keep this page open while video optimization is running.", {
                        id: "visibility-warning", // prevent duplicate toasts
                        duration: 8000
                    });
                    console.log("[ANALYTICS] Tab went to background during active video compression.");
                }
            }
        };
        document.addEventListener("visibilitychange", handleVisibilityChange);
        return () => {
            document.removeEventListener("visibilitychange", handleVisibilityChange);
        };
    }, [activeUploads]);

    const uploadFile = async (file, category, label, options = {}) => {
        const { endpoint, token, onSuccess, onBeforeUpload } = options;

        if (!endpoint && !onBeforeUpload) {
            toast.error("Upload endpoint is missing.");
            return;
        }

        // Size & type validation
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(category);
        // Submission and application videos use the chunked transport (no single-POST size
        // ceiling) → allow large video files. A null endpoint also resolves
        // to a submissions endpoint via onBeforeUpload (SubmissionPage). Duration is guarded
        // (300s) by directVideoUpload; this is just a sane upper safety bound.
        const isChunkedVideo =
            isVideoSlot && (!endpoint || CHUNKED_VIDEO_ENDPOINT_RE.test(endpoint));
                const CAP_MB = isVideoSlot ? 500 : 20;

        if (file) {
            const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
            if (isVideoSlot) {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Video is too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                const allowedVideoExts = [".mp4", ".mov", ".avi", ".webm", ".mkv", ".3gp"];
                if (!allowedVideoExts.includes(ext) && !file.type.startsWith("video/")) {
                    toast.error(`Unsupported video format. Please upload MP4, MOV, or WEBM.`);
                    return;
                }
            } else {
                if (file.size > CAP_MB * 1024 * 1024) {
                    toast.error(`Image too large (${Math.round(file.size / 1024 / 1024)} MB). Max ${CAP_MB} MB.`);
                    return;
                }
                if ([".bmp", ".tiff"].includes(ext) || ["image/bmp", "image/tiff"].includes(file.type)) {
                    toast.error(`BMP and TIFF formats are not supported. Please upload JPEG, PNG, or HEIC.`);
                    return;
                }
                if (!file.type.startsWith("image/") && ![".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"].includes(ext)) {
                    toast.error(`Unsupported image format. Please upload JPG, PNG, WEBP, or HEIC.`);
                    return;
                }
            }
        }

        // Run any custom hooks before starting (e.g. creating/saving profile drafts)
        let dynamicToken = token;
        let dynamicEndpoint = endpoint;
        if (onBeforeUpload) {
            const result = await onBeforeUpload();
            if (!result) return; // Hook failed or returned falsy
            if (result.token) dynamicToken = result.token;
            if (result.endpoint) dynamicEndpoint = result.endpoint;
        }

        const slotKey = label ? `${category}:${label}` : category;
        if (inFlightUploads.current[slotKey]) {
            console.warn(`[UPLOAD BLOCK] Sync block: upload for ${slotKey} already in progress.`);
            return;
        }
        inFlightUploads.current[slotKey] = true;

        // Phase 6/7 — bounded concurrency: mark this slot QUEUED immediately.
        // It may wait on the COMPRESSION gate below (if it's a video that
        // needs compression) and/or the UPLOAD gate further down (network
        // transport) before doing real work — the two are separate pools
        // (see the gate definitions above), so a burst of selected files
        // can't spin up dozens of simultaneous FFmpeg/upload jobs on mobile.
        setActiveUploads((prev) => ({
            ...prev,
            [slotKey]: {
                status: "queued",
                pct: 0,
                statusText: "Queued",
                fileName: file.name,
                category,
                label,
                file,
                options,
            },
        }));

        let fileToUpload = file;
        let skipCompression = false;
        let compressedFile = null;
        let preFetchedSig = null;

        if (file && isVideoSlot) {
            // R2 Ingestion Check: Retrieve signature and determine if R2 is active
            const chunkedVideoMatch = CHUNKED_VIDEO_ENDPOINT_RE.exec(dynamicEndpoint || "");
            if (chunkedVideoMatch) {
                const isApp = chunkedVideoMatch[1] === "apply";
                const targetId = chunkedVideoMatch[2];
                const signatureEndpoint = isApp
                    ? `/public/apply/${targetId}/video-signature`
                    : `/public/submissions/${targetId}/video-signature`;
                try {
                    const headers = dynamicToken ? { Authorization: `Bearer ${dynamicToken}` } : {};
                    const res = await api.post(
                        signatureEndpoint,
                        { category, label: label || null, content_type: file.type || null, public_id: null },
                        { headers }
                    );
                    if (res.data && res.data.use_r2) {
                        preFetchedSig = {
                            use_r2: true,
                            uploadUrl: res.data.upload_url,
                            publicId: res.data.public_id,
                        };
                        skipCompression = true;
                        console.log("[R2 PIPELINE] R2 is active. Bypassing client-side FFmpeg compression.");
                    }
                } catch (err) {
                    console.warn("[R2 PIPELINE WARNING] Signature pre-fetch failed:", err);
                }
            }

            if (!skipCompression) {
                try {
                    const { getCompressionProfile, COMPRESS_THRESHOLD } = await import("../lib/videoCompress");
                    const { deviceType } = getCompressionProfile();
                    const isMobileOrTablet = deviceType === "MOBILE" || deviceType === "TABLET";
                    
                    if (file.size <= COMPRESS_THRESHOLD) {
                        skipCompression = true;
                    } else if (isMobileOrTablet && file.size > 300 * 1024 * 1024) {
                        skipCompression = true;
                        toast.info("Large video detected. Uploading directly.");
                        console.log(`[FFMPEG BYPASS] Mobile video of size ${Math.round(file.size / 1024 / 1024)}MB exceeds 300MB. Direct upload activated.`);
                    } else if (!isMobileOrTablet && file.size > 700 * 1024 * 1024) {
                        skipCompression = true;
                        console.log(`[FFMPEG BYPASS] Desktop video of size ${Math.round(file.size / 1024 / 1024)}MB exceeds 700MB. Direct upload activated.`);
                    }
                } catch (err) {
                    console.error("Failed to check compression profile:", err);
                }
            }
        }

        if (file && isVideoSlot && !skipCompression) {
            // Phase 7 — at most ONE compression job touches the shared
            // FFmpeg singleton at a time (see lib/videoCompress.js and the
            // gate comment above). Everything inside this block — the
            // status writes, the profile/timeout/progress behavior, the
            // TIMEOUT fallback-to-original — is unchanged from Phase 6;
            // only the acquire/release around it is new.
            await compressionGate.acquire();
            // Stage 2 — a compression job is starting: cancel any pending
            // idle-recycle timer immediately. The actual timer/teardown logic
            // lives entirely in lib/videoCompress.js (the compression
            // infrastructure); this is just notifying it of the gate
            // transition, at the exact point the gate is acquired.
            const { cancelIdleRecycle, scheduleIdleRecycle } = await import("../lib/videoCompress");
            cancelIdleRecycle();
            try {
                setActiveUploads((prev) => ({
                    ...prev,
                    [slotKey]: {
                        status: "compressing",
                        pct: 0,
                        statusText: "Preparing video...",
                        fileName: file.name,
                        category,
                        label,
                        file,
                        options
                    }
                }));

                try {
                    console.log("Compression started");
                    const { compressVideoIfNeeded } = await import("../lib/videoCompress");
                    compressedFile = await compressVideoIfNeeded(file, {
                        onProgress: (stage, pct, estTimeRemaining) => {
                            setActiveUploads((prev) => {
                                if (!prev[slotKey]) return prev;

                                let stageText = "Optimizing video...";
                                if (stage === "load") {
                                    stageText = "Preparing video...";
                                } else if (stage === "compress") {
                                    stageText = estTimeRemaining
                                        ? `Optimizing video... (${estTimeRemaining} remaining)`
                                        : "Optimizing video...";
                                }

                                return {
                                    ...prev,
                                    [slotKey]: {
                                        ...prev[slotKey],
                                        pct: pct,
                                        statusText: stageText
                                    }
                                };
                            });
                        }
                    });
                    console.log("Compression finished");
                    if (compressedFile) {
                        console.log("Compressed size:", compressedFile.size);
                        fileToUpload = compressedFile;
                    }
                } catch (err) {
                    console.warn("[FFMPEG FALLBACK WARNING]", err);
                    // Purely observational — never let a telemetry failure
                    // affect the fallback-to-original-file path below.
                    try {
                        const { emitFFmpegTelemetry } = await import("../lib/videoCompress");
                        emitFFmpegTelemetry("compression_fallback", { reason: err?.code || "unknown" });
                    } catch (telemetryErr) {
                        console.warn("[FFMPEG TELEMETRY] compression_fallback emit failed (non-fatal):", telemetryErr);
                    }
                    if (err?.code === "TIMEOUT") {
                        toast.warning("Video optimization is taking longer than expected. Uploading original video.");
                    } else {
                        toast.info("Optimizing video unavailable. Uploading original file.");
                    }
                    fileToUpload = file;
                }
            } finally {
                compressionGate.release();
                // Stage 2 — the compression gate is now free: start (or
                // reset) the idle-recycle timer. Only actually tears anything
                // down after FFMPEG_IDLE_RECYCLE_MS of genuine inactivity —
                // cancelIdleRecycle() above will cancel it the moment another
                // compression begins.
                scheduleIdleRecycle();
            }
        }

        console.log("ORIGINAL", file.size);
        console.log("COMPRESSED", compressedFile?.size);
        console.log("UPLOADED", fileToUpload?.size);
        console.log("Upload starting with:", fileToUpload?.size);

        // Phase 7 — the file may now need to wait for a network-transport
        // slot (separate pool from compression, still capped at
        // MAX_CONCURRENT_UPLOADS). For non-video files, or a video that
        // skipped compression, this is the first gate they ever wait on.
        setActiveUploads((prev) => {
            if (!prev[slotKey]) return prev;
            return { ...prev, [slotKey]: { ...prev[slotKey], status: "queued", statusText: "Queued" } };
        });
        await uploadGate.acquire();

        // Validate final file size before upload (must be under 500MB R2 limit)
        if (fileToUpload && fileToUpload.size > 500 * 1024 * 1024) {
            const sizeMB = Math.round(fileToUpload.size / (1024 * 1024));
            console.error(`[SIZE LIMIT EXCEEDED] File size ${sizeMB}MB exceeds 500MB R2 limit.`);
            toast.error(`File is too large to upload (${sizeMB} MB). Maximum allowed size is 500 MB.`);
            
            setActiveUploads((prev) => ({
                ...prev,
                [slotKey]: {
                    status: "failed",
                    pct: 0,
                    statusText: "Upload failed: file too large",
                    fileName: fileToUpload.name,
                    category,
                    label,
                    error: `File size (${sizeMB} MB) exceeds 500 MB limit.`,
                    file: fileToUpload,
                    options
                }
            }));
            
            // Release synchronization lock
            delete inFlightUploads.current[slotKey];
            uploadGate.release();
            return;
        }

        setActiveUploads((prev) => ({
            ...prev,
            [slotKey]: {
                status: "uploading",
                pct: 0,
                statusText: "Uploading video...",
                fileName: fileToUpload.name,
                category,
                label,
                file: fileToUpload,
                options
            }
        }));

        setRetryQueue((q) => ({
            ...q,
            [slotKey]: { category, label, attempt: 0, fileName: fileToUpload.name, fileSize: fileToUpload.size, file: fileToUpload, options }
        }));

        // ── Chunked transport for submission and application videos ──────────
        // Reuses directVideoUpload (chunked, resumable, duration-guarded) while
        // driving the SAME activeUploads state machine, so FloatingUploadManager
        // and the upload cards/progress bars are unchanged.
        const chunkedVideoMatch = isVideoSlot && CHUNKED_VIDEO_ENDPOINT_RE.exec(dynamicEndpoint || "");
        if (chunkedVideoMatch) {
            const isApp = chunkedVideoMatch[1] === "apply";
            const targetId = chunkedVideoMatch[2];
            try {
                await directVideoUpload({
                    sid: targetId,
                    token: dynamicToken,
                    category,
                    label,
                    file: fileToUpload,
                    isApplication: isApp,
                    preFetchedSig: preFetchedSig,
                    onProgress: (loaded, total) => {
                        const pct = total ? Math.round((loaded / total) * 100) : 0;
                        setActiveUploads((prev) => {
                            if (!prev[slotKey]) return prev;
                            return {
                                ...prev,
                                [slotKey]: {
                                    ...prev[slotKey],
                                    status: pct >= 100 ? "processing" : "uploading",
                                    pct,
                                    statusText: pct >= 100 ? "Processing complete" : `Uploading video... (${pct}%)`
                                },
                            };
                        });
                    },
                });

                // Re-fetch the full document so onSuccess receives the updated state
                if (onSuccess) {
                    try {
                        const headers = dynamicToken ? { Authorization: `Bearer ${dynamicToken}` } : {};
                        const fetchUrl = isApp ? `/public/apply/${targetId}` : `/public/submissions/${targetId}`;
                        const res = await api.get(fetchUrl, { headers });
                        onSuccess(res.data);
                    } catch (_) {
                        // Non-fatal: the asset is attached server-side
                    }
                }

                setRetryQueue((q) => {
                    const n = { ...q };
                    delete n[slotKey];
                    return n;
                });
                setActiveUploads((prev) => ({
                    ...prev,
                    [slotKey]: { ...prev[slotKey], status: "completed", pct: 100, statusText: "Processing complete" },
                }));
                setTimeout(() => {
                    setActiveUploads((prev) => {
                        const next = { ...prev };
                        if (next[slotKey]?.status === "completed") delete next[slotKey];
                        return next;
                    });
                }, 3000);
                delete inFlightUploads.current[slotKey]; // release lock on success
                uploadGate.release();
                return;
            } catch (err) {
                const msg = err?.message || err?.response?.data?.detail || "Upload failed";
                setRetryQueue((q) => ({
                    ...q,
                    [slotKey]: { ...(q[slotKey] || {}), failed: true, error: msg },
                }));
                setActiveUploads((prev) => ({
                    ...prev,
                    // statusText must be cleared here — it may still hold an
                    // in-flight label (e.g. "Processing complete", set the
                    // instant upload bytes finish leaving the browser, before
                    // the server response is known) from just before this
                    // failure. Left stale, the view model prefers it over the
                    // canonical "Failed" label, so the panel showed that
                    // in-flight text simultaneously with the failure reason.
                    [slotKey]: { ...prev[slotKey], status: "failed", error: msg, statusText: undefined },
                }));
                toast.error(`${msg} — tap Retry to try again`);
                delete inFlightUploads.current[slotKey]; // release lock on failure
                uploadGate.release();
                return;
            }
        }

        const MAX_ATTEMPTS = 3;
        let lastErr = null;

        for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try {
                // Phase 6 — surface that this attempt is an automatic retry
                // (not a fresh "Uploading" start) before re-attempting the
                // same sign→upload→complete sequence. Reuses this exact
                // existing retry loop/backoff — nothing about the retry
                // mechanism itself changes here, only what's visible.
                if (attempt > 1) {
                    setActiveUploads((prev) => {
                        if (!prev[slotKey]) return prev;
                        return {
                            ...prev,
                            [slotKey]: {
                                ...prev[slotKey],
                                status: "retrying",
                                statusText: `Retrying… (attempt ${attempt} of ${MAX_ATTEMPTS})`,
                            },
                        };
                    });
                }

                // 1. Get upload signature from backend
                const headers = {};
                if (dynamicToken) {
                    headers["Authorization"] = `Bearer ${dynamicToken}`;
                }
                const signRes = await api.post(`${dynamicEndpoint}/sign`, {
                    category,
                    filename: fileToUpload.name
                }, { headers });
                const signData = signRes.data;

                // 2. Build signed upload FormData for Cloudinary
                const fd = new FormData();
                fd.append("file", fileToUpload);
                fd.append("api_key", signData.api_key);
                fd.append("timestamp", signData.timestamp);
                fd.append("signature", signData.signature);
                fd.append("folder", signData.folder);
                fd.append("public_id", signData.public_id);
                if (signData.eager) {
                    fd.append("eager", signData.eager);
                }
                if (signData.transformation) {
                    fd.append("transformation", signData.transformation);
                }

                // 3. Upload directly to Cloudinary
                const cloudinaryUrl = `https://api.cloudinary.com/v1_1/${signData.cloud_name}/${signData.resource_type}/upload`;
                const uploadRes = await axios.post(cloudinaryUrl, fd, {
                    headers: {
                        "Content-Type": "multipart/form-data",
                    },
                    timeout: 0,
                    onUploadProgress: (e) => {
                        if (e.total) {
                            const pct = Math.round((e.loaded / e.total) * 100);
                            setActiveUploads((prev) => {
                                if (!prev[slotKey]) return prev;
                                return {
                                    ...prev,
                                    [slotKey]: {
                                        ...prev[slotKey],
                                        status: pct >= 100 ? "processing" : "uploading",
                                        pct,
                                        statusText: pct >= 100 ? "Processing complete" : `${isVideoSlot ? "Uploading video" : "Uploading"}... (${pct}%)`
                                    }
                                };
                            });
                        }
                    }
                });

                // 4. Submit completed metadata to backend to save
                const completeRes = await api.post(`${dynamicEndpoint}/complete`, {
                    media_id: signData.media_id,
                    category,
                    label: label && category === "take" ? label : undefined,
                    public_id: signData.public_id,
                    url: uploadRes.data.secure_url,
                    bytes: uploadRes.data.bytes,
                    duration: uploadRes.data.duration,
                    content_type: fileToUpload.type,
                    original_filename: fileToUpload.name,
                    eager: uploadRes.data.eager
                }, { headers });

                if (onSuccess) onSuccess(completeRes.data);

                setRetryQueue((q) => {
                    const n = { ...q };
                    delete n[slotKey];
                    return n;
                });

                setActiveUploads((prev) => ({
                    ...prev,
                    [slotKey]: {
                        ...prev[slotKey],
                        status: "completed",
                        pct: 100,
                        statusText: "Processing complete"
                    }
                }));

                setTimeout(() => {
                    setActiveUploads((prev) => {
                        const next = { ...prev };
                        if (next[slotKey]?.status === "completed") {
                            delete next[slotKey];
                        }
                        return next;
                    });
                }, 3000);

                if (attempt > 1) toast.success(`Recovered after ${attempt} attempts`);
                delete inFlightUploads.current[slotKey]; // release lock on success
                uploadGate.release();
                return;
            } catch (err) {
                lastErr = err;
                const isNetwork = !err?.response;
                if (!isNetwork || attempt === MAX_ATTEMPTS) break;

                const wait = 1000 * Math.pow(2, attempt - 1);
                toast.message(`Network blip — retrying in ${wait / 1000}s (attempt ${attempt}/${MAX_ATTEMPTS})`);

                setRetryQueue((q) => ({
                    ...q,
                    [slotKey]: { ...(q[slotKey] || {}), attempt }
                }));

                // Phase 6 — paused during the backoff delay, distinct from
                // "Retrying" (which means the retry request is actually in
                // flight right now). Purely a status label around the
                // existing exponential-backoff sleep — the wait duration and
                // attempt count are unchanged.
                setActiveUploads((prev) => {
                    if (!prev[slotKey]) return prev;
                    return {
                        ...prev,
                        [slotKey]: {
                            ...prev[slotKey],
                            status: "waiting",
                            statusText: `Waiting to retry… (attempt ${attempt} of ${MAX_ATTEMPTS})`,
                        },
                    };
                });

                await new Promise((r) => setTimeout(r, wait));
            }
        }

        // Failed after all retries
        const formattedErr = formatErrorDetail(lastErr, "Upload failed");
        setRetryQueue((q) => ({
            ...q,
            [slotKey]: { ...(q[slotKey] || {}), failed: true, error: formattedErr }
        }));

        setActiveUploads((prev) => ({
            ...prev,
            [slotKey]: {
                ...prev[slotKey],
                status: "failed",
                error: formattedErr,
                // See the identical clear in the chunked-transport catch
                // block above: without this, a stale in-flight statusText
                // (e.g. "Uploading video... (100%)") from just before the
                // failure keeps rendering alongside the failure reason.
                statusText: undefined
            }
        }));

        toast.error(formattedErr + " — tap Retry to try again");
        delete inFlightUploads.current[slotKey]; // release lock on failure
        uploadGate.release();
    };

    const retryUpload = async (slotKey) => {
        const entry = retryQueue[slotKey];
        if (!entry?.file) {
            toast.error("Re-select the file to retry");
            return;
        }
        await uploadFile(entry.file, entry.category, entry.label, entry.options);
    };

    const dismissUpload = (slotKey) => {
        setActiveUploads((prev) => {
            const n = { ...prev };
            delete n[slotKey];
            return n;
        });
    };

    return (
        <UploadManagerContext.Provider value={{ activeUploads, retryQueue, uploadFile, retryUpload, dismissUpload, completedCount }}>
            {children}
            <FloatingUploadManager
                activeUploads={activeUploads}
                completedCount={completedCount}
                onRetry={retryUpload}
                onDismiss={dismissUpload}
            />
        </UploadManagerContext.Provider>
    );
}
