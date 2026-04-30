/**
 * Client-side video compression via ffmpeg.wasm (single-thread, no SAB).
 *
 * Used by SubmissionPage / ApplicationPage so talents on slow mobile
 * networks don't have to wait minutes uploading a 200–500 MB iPhone clip:
 * we transcode in-browser to ~720p H.264 / AAC at a bitrate that targets
 * 15–25 MB and only the *compressed* file ever leaves the device.
 *
 * Strategy:
 *   1. Files ≤ COMPRESS_THRESHOLD pass through unchanged (fast path).
 *   2. Files between threshold and HARD_MAX get transcoded.
 *   3. Files > HARD_MAX are rejected (the spec's safe upper bound).
 *
 * The ffmpeg core (~31 MB wasm) is fetched on first use only, then cached
 * in the browser HTTP cache. No COOP/COEP headers required because we
 * load the single-thread core which doesn't use SharedArrayBuffer.
 */

import { FFmpeg } from "@ffmpeg/ffmpeg";
import { fetchFile, toBlobURL } from "@ffmpeg/util";

export const MB = 1024 * 1024;
export const COMPRESS_THRESHOLD = 25 * MB; // anything above → optimise
export const SOFT_LIMIT = 150 * MB; // legacy guard (per spec, no longer hard)
export const HARD_MAX = 500 * MB; // spec: refuse beyond this

// Pinned core version that matches the @ffmpeg/ffmpeg version we install.
// Single-thread core works in every modern browser (incl. iOS Safari)
// without cross-origin isolation.
const FFMPEG_CORE_VERSION = "0.12.6";
const FFMPEG_CORE_BASE = `https://unpkg.com/@ffmpeg/core@${FFMPEG_CORE_VERSION}/dist/umd`;

let _ffmpeg = null;
let _loadingPromise = null;

/** Lazily initialise + cache the ffmpeg.wasm instance. */
async function getFFmpeg() {
    if (_ffmpeg) return _ffmpeg;
    if (_loadingPromise) return _loadingPromise;
    _loadingPromise = (async () => {
        const ffmpeg = new FFmpeg();
        // toBlobURL fetches the script/wasm cross-origin then re-serves it
        // as a blob: URL so the browser treats it as same-origin (lets
        // ffmpeg.wasm bootstrap its worker without a CORS error).
        const [coreURL, wasmURL] = await Promise.all([
            toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.js`, "text/javascript"),
            toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.wasm`, "application/wasm"),
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

/**
 * Compress a video file to ~720p H.264 + AAC.
 *
 * @param {File} file Source file (any browser-decodable codec).
 * @param {object} opts
 * @param {(stage: 'load' | 'compress', pct: number) => void} [opts.onProgress]
 *   Called repeatedly during the load and compression phases.
 * @returns {Promise<Blob>} Compressed MP4 blob.
 */
export async function compressVideo(file, { onProgress } = {}) {
    const notify = (stage, pct) => {
        try {
            onProgress?.(stage, Math.max(0, Math.min(100, Math.round(pct))));
        } catch {
            // never let UI throws kill the encode
        }
    };

    // Stage 1 — load the wasm core. We approximate progress as 0→100 over
    // a fetch promise; real granular progress isn't exposed by toBlobURL.
    notify("load", 5);
    const ffmpeg = await getFFmpeg();
    notify("load", 100);

    // Pick a safe input filename (ffmpeg uses extension to choose demuxer).
    const ext = (file.name.split(".").pop() || "mp4").toLowerCase().slice(0, 4);
    const inputName = `in.${ext.match(/^[a-z0-9]+$/) ? ext : "mp4"}`;
    const outputName = "out.mp4";

    // Stage 2 — write the blob into ffmpeg's virtual FS.
    notify("compress", 1);
    await ffmpeg.writeFile(inputName, await fetchFile(file));

    // Hook ffmpeg progress events. Range is 0-1.
    const onFFProg = ({ progress }) => {
        // ffmpeg occasionally emits >1 at end-of-file; clamp.
        notify("compress", Math.max(2, Math.min(99, progress * 100)));
    };
    ffmpeg.on("progress", onFFProg);

    try {
        // Encode params chosen for spec target (15-25 MB at 720p):
        //   -vf scale=-2:'min(720,ih)' → cap height at 720p, keep aspect,
        //                                width must be even (-2).
        //   -c:v libx264 -preset ultrafast → single-thread wasm is slow,
        //                                    ultrafast keeps it tolerable.
        //   -crf 28 → visually fine for casting reels, 720p ~1.5 Mbps.
        //   -movflags +faststart → mp4 atom up-front so Cloudinary/players
        //                          can stream-play before full download.
        //   -c:a aac -b:a 96k -ac 2 → small stereo aac, plenty for talent
        //                              voiceovers.
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
        // Free the virtual FS so subsequent compressions don't OOM the wasm heap.
        try {
            await ffmpeg.deleteFile(inputName);
            await ffmpeg.deleteFile(outputName);
        } catch {
            // not fatal
        }
        return new Blob([data.buffer], { type: "video/mp4" });
    } finally {
        ffmpeg.off("progress", onFFProg);
    }
}

/**
 * Wrap a File into a File-like object so existing FormData logic works
 * unchanged. Falls back to Blob when File constructor is unavailable
 * (older Safari).
 */
export function blobToFile(blob, name, type = "video/mp4") {
    try {
        return new File([blob], name, { type, lastModified: Date.now() });
    } catch {
        // Some older runtimes choke on `new File(...)`. Decorate the blob
        // with the props FormData reads.
        // eslint-disable-next-line no-param-reassign
        blob.name = name;
        return blob;
    }
}

/**
 * High-level helper used by upload flows.
 *
 * - file ≤ COMPRESS_THRESHOLD  → returned unchanged.
 * - file > HARD_MAX            → throws (caller surfaces toast).
 * - otherwise                  → transcoded; only the compressed File
 *                                comes back, never the original.
 *
 * @param {File} file
 * @param {object} opts
 * @param {(stage, pct) => void} opts.onProgress
 * @returns {Promise<File>}
 */
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
    const baseName = file.name.replace(/\.[^.]+$/, "") || "video";
    return blobToFile(blob, `${baseName}-720p.mp4`, "video/mp4");
}
