/**
 * Client-side video compression via ffmpeg.wasm (single-thread, no SAB).
 *
 * Strategy:
 *   1. Files ≤ COMPRESS_THRESHOLD pass through unchanged (fast path).
 *   2. Files between threshold and HARD_MAX get transcoded.
 *   3. Files > HARD_MAX are rejected (the spec's safe upper bound).
 */

import { FFmpeg } from "@ffmpeg/ffmpeg";
import { fetchFile, toBlobURL } from "@ffmpeg/util";

export const MB = 1024 * 1024;
export const COMPRESS_THRESHOLD = 25 * MB; // anything above → optimise
export const HARD_MAX = 1024 * MB; // refuse beyond this (1GB)

let _ffmpeg = null;
let _loadingPromise = null;
let _hasInitializedOnce = false;

// Lightweight dev-only logging, matching the existing
// `process.env.NODE_ENV === "development"` convention used elsewhere in this
// codebase (see pages-components/TalentEdit.jsx) — avoids adding noise to
// production console output for a lifecycle detail end users never need to
// see.
function devLog(...args) {
    if (process.env.NODE_ENV === "development") {
        console.log(...args);
    }
}

// Lightweight, framework-free telemetry for the FFmpeg lifecycle — purely
// observational, never affects behavior. This codebase has no generic
// analytics/telemetry infrastructure today (no gtag/Segment/PostHog/etc.,
// checked at the package.json and app-layout level; the only existing
// "track" mechanism is the Client Review Link's slug-scoped
// `POST /public/links/{slug}/track`, a different bounded context this
// module has no access to). Per "do not add a new telemetry framework,"
// this dispatches a plain, native DOM CustomEvent instead of introducing an
// SDK or a new backend endpoint — any analytics integration (present or
// future) can subscribe with a single `window.addEventListener(...)` call
// without this module ever needing to know it exists. Wrapped so a failure
// here can NEVER throw into (or block) the calling upload/compression code.
const FFMPEG_TELEMETRY_EVENT = "tg:ffmpeg-telemetry";

export function emitFFmpegTelemetry(event, data = {}) {
    try {
        if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") return;
        window.dispatchEvent(new CustomEvent(FFMPEG_TELEMETRY_EVENT, {
            detail: { event, ...data, timestamp: Date.now() },
        }));
    } catch {
        // Telemetry must never affect uploads or compression — swallow silently.
    }
}

// Phase 7 correctness fix: `_ffmpeg` above is a single shared instance for
// the whole page — one Web Worker, one virtual filesystem. Every compression
// job must use its OWN input/output filenames on that shared filesystem, so
// that even if this module is ever called from more than one place (now, or
// in the future) without going through the compression concurrency gate in
// UploadManagerContext.jsx, two jobs can't stomp on each other's `in`/`out`
// files. This is defense-in-depth, not the primary fix — the gate (capped at
// 1 concurrent compression) is what actually guarantees only one job ever
// touches the shared instance at a time.
function generateCompressionJobId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
    }
    // Fallback for browsers without crypto.randomUUID (older WebViews) —
    // still unique enough for filesystem namespacing, just not cryptographic.
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function getCompressionProfile() {
    if (typeof window === "undefined") {
        return {
            deviceType: "DESKTOP",
            memory: 8,
            profile: "HIGH_END",
            crf: "26"
        };
    }

    const ua = navigator.userAgent || "";
    
    // 1. Determine deviceType (Mobile, Tablet, Desktop)
    let deviceType = "DESKTOP";
    const isTablet = /(ipad|tablet|(android(?!.*mobile))|(windows(?!.*phone)(.*touch))|kindle|playbook|silk)/i.test(ua);
    const isMobile = /Mobile|iP(hone|od)|Android|BlackBerry|IEMobile|Kindle|NetFront|Silk-Accelerated|(hpw|web)OS|Fennec|Minimo|Opera M(obi|ini)|Blazer|Dolfin|Dolphin|Skyfire|Zune/i.test(ua);
    
    if (isTablet) {
        deviceType = "TABLET";
    } else if (isMobile) {
        deviceType = "MOBILE";
    } else if (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 0) {
        // iPad Pro presents as Macintosh with touch support
        deviceType = "TABLET";
    }

    // 2. Determine memory where available (navigator.deviceMemory)
    let memory = navigator.deviceMemory;
    
    // 3. Fallback for browsers that don't support navigator.deviceMemory (like Safari)
    if (memory === undefined) {
        if (deviceType === "MOBILE" || deviceType === "TABLET") {
            // Mobile devices/tablets are RAM-constrained, safely default to 4GB to trigger LOW_END/MID_RANGE profiles
            memory = 4; 
        } else {
            // Desktop safe default
            memory = 8;
        }
    }

    // 4. Map profiles
    let profile = "HIGH_END";
    let crf = "26";

    if (memory <= 4) {
        profile = "LOW_END";
        crf = "30";
    } else if (memory <= 8) {
        profile = "MID_RANGE";
        crf = "28";
    } else {
        profile = "HIGH_END";
        crf = "26";
    }

    return {
        deviceType,
        memory,
        profile,
        crf
    };
}


function getVideoMetadata(file) {
    return new Promise((resolve) => {
        try {
            const url = URL.createObjectURL(file);
            const video = document.createElement("video");
            video.preload = "metadata";
            video.muted = true;
            video.playsInline = true;

            video.onloadedmetadata = () => {
                const duration = video.duration;
                const width = video.videoWidth;
                const height = video.videoHeight;
                let codec = null;
                const type = file.type || "";
                const match = type.match(/codecs="([^"]+)"/);
                if (match) {
                    codec = match[1];
                }
                URL.revokeObjectURL(url);
                resolve({
                    duration: Number.isFinite(duration) ? duration : null,
                    width: width || null,
                    height: height || null,
                    size: file.size,
                    codec
                });
            };

            video.onerror = () => {
                URL.revokeObjectURL(url);
                resolve({
                    duration: null,
                    width: null,
                    height: null,
                    size: file.size,
                    codec: null
                });
            };

            video.src = url;
        } catch {
            resolve({
                duration: null,
                width: null,
                height: null,
                size: file.size,
                codec: null
            });
        }
    });
}

const logMemory = (stage) => {
    console.log(`[MEMORY] ${stage}`);
    console.log("[MEMORY]", {
        used: typeof performance !== "undefined" ? performance.memory?.usedJSHeapSize : undefined,
        total: typeof performance !== "undefined" ? performance.memory?.totalJSHeapSize : undefined
    });
};

// Versioned asset directory (see next.config.js's headers() for the
// matching immutable Cache-Control rule scoped to this path). A future
// @ffmpeg/core upgrade must copy its files into a NEW version directory
// (e.g. FFMPEG_CORE_ASSET_PATH = "/ffmpeg/v2") rather than overwriting the
// files at this URL — immutable caching means browsers holding a
// long-lived cached copy of /ffmpeg/v1/* would otherwise never see the
// update.
const FFMPEG_CORE_ASSET_PATH = "/ffmpeg/v1";

async function getFFmpeg() {
    if (_ffmpeg) return _ffmpeg;
    if (_loadingPromise) return _loadingPromise;
    _loadingPromise = (async () => {
        const ffmpeg = new FFmpeg();
        const base = typeof window !== "undefined" ? window.location.origin : "";
        const [coreURL, wasmURL] = await Promise.all([
            toBlobURL(`${base}${FFMPEG_CORE_ASSET_PATH}/ffmpeg-core.js`, "text/javascript"),
            toBlobURL(`${base}${FFMPEG_CORE_ASSET_PATH}/ffmpeg-core.wasm`, "application/wasm"),
        ]);
        console.log("[FFMPEG] Initializing");
        await ffmpeg.load({ coreURL, wasmURL });
        console.log("[FFMPEG] Loaded successfully");
        _ffmpeg = ffmpeg;
        devLog(_hasInitializedOnce ? "[FFMPEG] Reinitialized" : "[FFMPEG] Initialized");
        emitFFmpegTelemetry("ffmpeg_initialized", { reinitialized: _hasInitializedOnce });
        _hasInitializedOnce = true;
        return ffmpeg;
    })();
    try {
        return await _loadingPromise;
    } catch (e) {
        _loadingPromise = null;
        throw e;
    }
}

// ---------------------------------------------------------------------------
// Stage 2 — idle-timeout recycling of the shared FFmpeg singleton.
//
// The WASM heap this singleton holds only ever grows (see the Phase 8 audit
// in 08_DECISION_LOG.md/09_PRESENTATION_MODELS.md): `terminate()` is the only
// way to actually give that memory back to the browser. It was already used
// reactively, on a compression timeout, below. This adds the same teardown
// on a second trigger — prolonged genuine compression inactivity — so a long
// session doesn't keep a peak-memory WASM instance loaded indefinitely once
// the user has moved on to other parts of the form.
//
// `scheduleIdleRecycle()`/`cancelIdleRecycle()` are called from
// UploadManagerContext.jsx at the exact points it already acquires/releases
// its compression concurrency gate — that gate is single-concurrency
// (MAX_CONCURRENT_COMPRESSIONS = 1), so "the gate is free" and "no compression
// is running or queued" are the same fact. This module owns no knowledge of
// the gate itself, only of what to do when told compression has gone idle.
// ---------------------------------------------------------------------------

// Configurable, not a magic number: how long compression must be genuinely
// idle (no job running, none queued) before the shared instance is recycled.
export const FFMPEG_IDLE_RECYCLE_MS = 5 * 60 * 1000; // 5 minutes

let _idleRecycleTimer = null;

// Single shared teardown path — reused by both the existing timeout-recovery
// branch (in compressVideo(), below) and idle-timeout recycling. Resets the
// module state unconditionally, whether or not terminate() itself succeeds,
// so a failure here can never leave the singleton in a partially-reset state
// — the next getFFmpeg() call always sees a clean "nothing loaded" state and
// reloads through the exact same path as a first load.
async function terminateSharedFFmpeg(ffmpegInstance) {
    try {
        await ffmpegInstance.terminate();
    } catch (termErr) {
        console.error("Failed to terminate FFmpeg worker:", termErr);
    }
    _ffmpeg = null;
    _loadingPromise = null;
}

/**
 * Cancels any pending idle-recycle timer. Call the moment a new compression
 * job begins (i.e. as soon as the compression concurrency gate is acquired)
 * — guarantees recycling can never fire while a job is running, and (since
 * the gate is single-concurrency) never while another job is queued behind
 * it either, because being queued implies something else currently holds
 * the gate, which implies this was already called for that job.
 */
export function cancelIdleRecycle() {
    if (_idleRecycleTimer) {
        clearTimeout(_idleRecycleTimer);
        _idleRecycleTimer = null;
    }
}

/**
 * Starts (or restarts) the idle-recycle timer. Call the moment a compression
 * job ends (i.e. as soon as the compression concurrency gate is released).
 * Only actually tears anything down after `FFMPEG_IDLE_RECYCLE_MS` of
 * uninterrupted quiet — any subsequent `cancelIdleRecycle()` call (a new job
 * starting) cancels it first.
 */
export function scheduleIdleRecycle() {
    cancelIdleRecycle();
    if (!_ffmpeg) return; // nothing loaded — nothing to recycle
    _idleRecycleTimer = setTimeout(() => {
        _idleRecycleTimer = null;
        const toRecycle = _ffmpeg;
        if (!toRecycle) return; // already gone (e.g. a timeout recycle beat us to it)
        terminateSharedFFmpeg(toRecycle).then(() => {
            devLog(`[FFMPEG] Recycled after ${FFMPEG_IDLE_RECYCLE_MS / 1000}s idle timeout`);
            emitFFmpegTelemetry("ffmpeg_recycled", { idleMs: FFMPEG_IDLE_RECYCLE_MS });
        });
        // Deliberately not awaited: this fires from a setTimeout callback with
        // nothing waiting on it, and terminateSharedFFmpeg() never throws (it
        // catches its own terminate() failure) — there is no unhandled-
        // rejection or uploads-interruption risk either way.
    }, FFMPEG_IDLE_RECYCLE_MS);
}

export async function compressVideo(file, { onProgress } = {}) {
    const notify = (stage, pct, estTimeRemaining = null) => {
        try {
            onProgress?.(stage, Math.max(0, Math.min(100, Math.round(pct))), estTimeRemaining);
        } catch {
            // ignore
        }
    };

    try {
        // Retrieve and log video characteristics before compression starts
        try {
            const { duration, width, height, size, codec } = await getVideoMetadata(file);
            console.log("[VIDEO META]", {
                duration,
                width,
                height,
                size,
                ...(codec ? { codec } : {})
            });
        } catch (metaErr) {
            console.warn("Could not read HTML5 video metadata:", metaErr);
        }

        notify("load", 5);
        const ffmpeg = await getFFmpeg();
        notify("load", 100);

        const ext = (file.name.split(".").pop() || "mp4").toLowerCase().slice(0, 4);
        const safeExt = ext.match(/^[a-z0-9]+$/) ? ext : "mp4";
        const jobId = generateCompressionJobId();
        const inputName = `compress-${jobId}-input.${safeExt}`;
        const outputName = `compress-${jobId}-output.mp4`;

        const onFFLog = ({ message }) => {
            console.log("[FFMPEG LOG]", message);
        };

        let compressStartTime = null;

        const onFFProg = ({ progress }) => {
            if (!compressStartTime) {
                compressStartTime = Date.now();
            }
            console.log("[FFMPEG PROGRESS]", progress);
            const pct = Math.max(2, Math.min(99, progress * 100));
            
            let estTimeRemaining = null;
            if (pct > 5) {
                const elapsed = Date.now() - compressStartTime;
                const totalEst = elapsed / (pct / 100);
                const remaining = totalEst - elapsed;
                
                const totalSeconds = Math.round(remaining / 1000);
                if (totalSeconds > 0) {
                    const minutes = Math.floor(totalSeconds / 60);
                    const seconds = totalSeconds % 60;
                    if (minutes > 0) {
                        estTimeRemaining = `${minutes}m ${seconds}s`;
                    } else {
                        estTimeRemaining = `${seconds}s`;
                    }
                }
            }
            
            notify("compress", pct, estTimeRemaining);
        };

        ffmpeg.on("log", onFFLog);
        ffmpeg.on("progress", onFFProg);

        try {
            notify("compress", 1);
            
            logMemory("before writeFile");
            console.log("[FFMPEG] Writing input file", {
                name: file.name,
                size: file.size,
                type: file.type
            });
            await ffmpeg.writeFile(inputName, await fetchFile(file));
            console.log("[FFMPEG] Input file written");
            logMemory("after writeFile");

            logMemory("before exec");
            console.log("[FFMPEG] Starting transcode");
            
            // Device profile and dynamic settings
            const profile = getCompressionProfile();
            const crf = profile.crf;
            const isMobileOrTablet = profile.deviceType === "MOBILE" || profile.deviceType === "TABLET";
            const timeoutMs = isMobileOrTablet ? 180000 : 300000; // 3 min on mobile, 5 min on desktop
            
            console.log(`[FFMPEG EXEC] Device type: ${profile.deviceType}, Profile: ${profile.profile}, CRF: ${crf}, Timeout: ${timeoutMs / 60000}m`);

            const execPromise = ffmpeg.exec([
                "-i", inputName,
                "-vf", "scale='if(gte(iw,ih),trunc(iw*trunc(min(ih,720)/2)*2/ih/2)*2,trunc(min(iw,720)/2)*2)':'if(gte(iw,ih),trunc(min(ih,720)/2)*2,trunc(ih*trunc(min(iw,720)/2)*2/iw/2)*2)'",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", crf,
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", "96k",
                "-ac", "2",
                "-y", outputName,
            ]);

            let timerId;
            const timeoutPromise = new Promise((_, reject) => {
                timerId = setTimeout(() => {
                    const err = new Error(`FFmpeg compression timed out after ${timeoutMs / 60000} minutes`);
                    err.code = "TIMEOUT";
                    reject(err);
                }, timeoutMs);
            });

            try {
                const result = await Promise.race([execPromise, timeoutPromise]);
                clearTimeout(timerId);
                
                console.log("[FFMPEG] Transcode completed");
                console.log("[FFMPEG EXIT]", result);
                logMemory("after exec");

                if (result !== 0) {
                    throw new Error(`FFmpeg execution failed with exit code ${result}`);
                }
            } catch (err) {
                clearTimeout(timerId);
                if (err.code === "TIMEOUT") {
                    console.warn(`[FFMPEG TIMEOUT] Terminating worker due to timeout. Device: ${profile.deviceType}, File size: ${Math.round(file.size / MB)}MB`);
                    emitFFmpegTelemetry("ffmpeg_timeout", {
                        deviceType: profile.deviceType,
                        fileSizeMb: Math.round(file.size / MB),
                        timeoutMs,
                    });
                    await terminateSharedFFmpeg(ffmpeg);
                }
                throw err;
            }

            notify("compress", 100);
            
            const data = await ffmpeg.readFile(outputName);
            console.log("[FFMPEG] Output size", data.length);
            logMemory("after readFile");

            try {
                await ffmpeg.deleteFile(inputName);
                await ffmpeg.deleteFile(outputName);
            } catch {
                // ignore
            }
            return new Blob([data.buffer], { type: "video/mp4" });
        } finally {
            ffmpeg.off("log", onFFLog);
            ffmpeg.off("progress", onFFProg);
        }
    } catch (err) {
        console.error("[FFMPEG ERROR]", err);
        console.error("[FFMPEG STACK]", err?.stack);
        throw err;
    }
}

export function blobToFile(blob, name, type = "video/mp4") {
    try {
        return new File([blob], name, { type, lastModified: Date.now() });
    } catch {
        blob.name = name;
        return blob;
    }
}

export async function compressVideoIfNeeded(file, { onProgress } = {}) {
    if (!file) return file;
    if (file.size > HARD_MAX) {
        const mb = Math.round(file.size / MB);
        const err = new Error(
            `Video is too large (${mb} MB). The maximum is ${Math.round(HARD_MAX / MB)} MB.`,
        );
        err.code = "VIDEO_TOO_LARGE";
        throw err;
    }
    if (file.size <= COMPRESS_THRESHOLD) return file;
    const blob = await compressVideo(file, { onProgress });
    
    if (blob.size > 100 * MB) {
        const mb = Math.round(blob.size / MB);
        const err = new Error(
            `Compressed video size (${mb} MB) still exceeds the 100 MB limit. Please try recording a shorter video.`,
        );
        err.code = "COMPRESSED_TOO_LARGE";
        throw err;
    }

    const baseName = file.name.replace(/\.[^.]+$/, "") || "video";
    return blobToFile(blob, `${baseName}-720p.mp4`, "video/mp4");
}
