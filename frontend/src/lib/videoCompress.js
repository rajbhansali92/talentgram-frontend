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
export const HARD_MAX = 500 * MB; // refuse beyond this

let _ffmpeg = null;
let _loadingPromise = null;

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
        await ffmpeg.load({ coreURL, wasmURL });
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
    const notify = (stage, pct) => {
        try {
            onProgress?.(stage, Math.max(0, Math.min(100, Math.round(pct))));
        } catch {
            // ignore
        }
    };

    notify("load", 5);
    const ffmpeg = await getFFmpeg();
    notify("load", 100);

    const ext = (file.name.split(".").pop() || "mp4").toLowerCase().slice(0, 4);
    const inputName = `in.${ext.match(/^[a-z0-9]+$/) ? ext : "mp4"}`;
    const outputName = "out.mp4";

    notify("compress", 1);
    await ffmpeg.writeFile(inputName, await fetchFile(file));

    const onFFProg = ({ progress }) => {
        notify("compress", Math.max(2, Math.min(99, progress * 100)));
    };
    ffmpeg.on("progress", onFFProg);

    try {
        await ffmpeg.exec([
            "-i", inputName,
            "-vf", "scale='trunc(iw/2)*2':'min(720,ih)'",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-y", outputName,
        ]);
        notify("compress", 100);
        const data = await ffmpeg.readFile(outputName);
        try {
            await ffmpeg.deleteFile(inputName);
            await ffmpeg.deleteFile(outputName);
        } catch {
            // ignore
        }
        return new Blob([data.buffer], { type: "video/mp4" });
    } finally {
        ffmpeg.off("progress", onFFProg);
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
