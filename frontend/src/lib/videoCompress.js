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

async function getFFmpeg() {
    if (_ffmpeg) return _ffmpeg;
    if (_loadingPromise) return _loadingPromise;
    _loadingPromise = (async () => {
        const ffmpeg = new FFmpeg();
        const base = typeof window !== "undefined" ? window.location.origin : "";
        const [coreURL, wasmURL] = await Promise.all([
            toBlobURL(`${base}/ffmpeg/ffmpeg-core.js`, "text/javascript"),
            toBlobURL(`${base}/ffmpeg/ffmpeg-core.wasm`, "application/wasm"),
        ]);
        console.log("[FFMPEG] Initializing");
        await ffmpeg.load({ coreURL, wasmURL });
        console.log("[FFMPEG] Loaded successfully");
        _ffmpeg = ffmpeg;
        return ffmpeg;
    })();
    try {
        return await _loadingPromise;
    } catch (e) {
        _loadingPromise = null;
        throw e;
    }
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
        const inputName = `in.${ext.match(/^[a-z0-9]+$/) ? ext : "mp4"}`;
        const outputName = "out.mp4";

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
                    try {
                        await ffmpeg.terminate();
                    } catch (termErr) {
                        console.error("Failed to terminate FFmpeg worker:", termErr);
                    }
                    _ffmpeg = null;
                    _loadingPromise = null;
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
