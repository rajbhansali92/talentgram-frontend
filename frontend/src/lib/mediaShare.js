// ────────────────────────────────────────────────────────────────────────
// WhatsApp media sharing for the client review page.
//
// Hybrid strategy (capability detected automatically — never exposed to the user):
//   1. Native multi-file share  (mobile: navigator.share({ files: [...] }))
//   2. Native single-file share (mobile, one file)
//   3. WhatsApp link share       (desktop / unsupported): one wa.me message with
//      per-media SECURE Talentgram links (never raw Cloudinary/R2 URLs).
//
// This module is purely additive — it does not touch download logic.
// ────────────────────────────────────────────────────────────────────────
import { api as axios, getViewerToken, getSubdomainUrl } from "@/lib/api";

// Path/diagnostics logging — intentionally always on. WhatsApp sharing is a
// low-frequency, user-initiated action, and these logs are how we verify on a
// real device which execution path ran (native files vs native sheet vs wa.me)
// and, when it falls back, exactly why. Grep the device console for "[tg-share]".
const shareLog = (...args) => {
    try { console.info("[tg-share]", ...args); } catch { /* ignore */ }
};

export function canNativeShare() {
    return typeof navigator !== "undefined" && typeof navigator.share === "function";
}

export function canShareFiles(files) {
    try {
        return (
            typeof navigator !== "undefined" &&
            typeof navigator.canShare === "function" &&
            navigator.canShare({ files })
        );
    } catch {
        return false;
    }
}

/** True when the current browser can share actual files natively (mobile). */
export function deviceSupportsFileShare() {
    if (!canNativeShare() || typeof navigator.canShare !== "function") return false;
    // Probe with a tiny dummy file — Chrome/Safari return false on desktop.
    try {
        const probe = new File(["x"], "probe.txt", { type: "text/plain" });
        return navigator.canShare({ files: [probe] });
    } catch {
        return false;
    }
}

// Hard ceiling on the COMPLETE fetch→blob→File chain for one media item.
// This is independent of whatever timeout (if any) the underlying Request
// Manager / axios transport enforces internally — found live: with no
// explicit timeout passed to this call, a single item's promise that never
// settles (a hung connection, a service-worker interception, any transport
// edge case) left the Send button's readiness check waiting forever, since
// it can only react to a settled promise. This guarantees urlToFileTraced
// ALWAYS resolves within FILE_PREP_TIMEOUT_MS, no matter what the network
// or transport layer does.
export const FILE_PREP_TIMEOUT_MS = 25_000;

// Fully instrumented fetch→Blob→File, via the shared `api` transport (same
// Request Manager retry/circuit-breaker/dedup/request-ID coverage as every
// other Client View call — `url` is a relative path; `responseType: "blob"`
// makes publicApiTransport classify this as a "download" and route it
// direct-to-Railway, same as it always has). Writes every step's outcome
// into `rec` (mutated in place) so the caller can report the exact runtime
// reason, and returns the File on success or null on failure (never throws,
// and never stays pending longer than FILE_PREP_TIMEOUT_MS).
async function urlToFileTraced(url, filename, mimeHint, rec, init = {}) {
    rec.url = url;
    const controller = new AbortController();
    let timedOut = false;
    const timeoutId = setTimeout(() => {
        timedOut = true;
        try { controller.abort("client_timeout"); } catch { /* ignore */ }
    }, FILE_PREP_TIMEOUT_MS);

    // Step 5 — HTTP fetch
    let res;
    try {
        // Explicit circuitKey: without it, Request Manager's default grouping
        // (method + first path segment) puts every /public/* call — this
        // media fetch AND every unrelated /seen, /track, /links, /decision
        // call firing concurrently on the same page — on ONE shared circuit.
        // A multi-item share fires several of these fetches at once, so a
        // couple of transient retries (each retry counts as a failure) tips
        // the shared circuit's failureThreshold and short-circuits the
        // REMAINING media fetches in the same batch — files silently drop
        // out and the share falls back to link-only, while a single-item
        // share rarely accumulates enough failures alone to trip it. Scoping
        // media downloads to their own circuit isolates them from that
        // unrelated background traffic without removing breaker protection
        // for a genuinely broken media endpoint. `signal` + an EXPLICIT
        // `timeout` are both set to this same ceiling so the request is
        // genuinely cancelled (not just ignored) the moment it's exceeded,
        // instead of relying solely on whatever default the transport would
        // otherwise apply.
        res = await axios.get(url, {
            ...init,
            responseType: "blob",
            circuitKey: "get:media-download",
            signal: controller.signal,
            timeout: FILE_PREP_TIMEOUT_MS,
        });
    } catch (e) {
        clearTimeout(timeoutId);
        if (timedOut || e?.name === "AbortError" || e?.name === "CanceledError" || e?.code === "ECONNABORTED" || e?.classification === "timeout") {
            rec.fetchOk = false;
            rec.step = "timeout";
            rec.fetchThrew = `timed out after ${FILE_PREP_TIMEOUT_MS}ms`;
        } else if (e?.response) {
            // Server responded with a non-2xx status.
            rec.httpStatus = e.response.status;
            rec.fetchOk = false;
            rec.step = `http_${e.response.status}`;
        } else {
            // No response reached the app at all — CORS block or network
            // failure, the same shape axiosShim/RequestManager classify as
            // "network_error" (frontend/src/lib/requestManager/errorClassifier.js).
            rec.fetchThrew = `${e?.name || e?.classification || "Error"}: ${e?.message || ""}`.trim();
            rec.fetchOk = false;
            rec.step = "fetch_threw_cors_or_network";
        }
        return null;
    }
    clearTimeout(timeoutId);
    rec.httpStatus = res.status;
    rec.fetchOk = true;
    // Step 6 — Blob (axios's `responseType: "blob"` already materializes it —
    // no separate res.blob() step, so there's no "blob_failed" case distinct
    // from the fetch itself; the shape check below is the equivalent guard)
    const blob = res.data;
    if (!(blob instanceof Blob)) {
        rec.blobOk = false;
        rec.step = "blob_failed";
        return null;
    }
    rec.blobOk = true;
    rec.blobType = blob.type;
    rec.blobSize = blob.size;
    // Step 7 — File
    const type = blob.type || mimeHint || "application/octet-stream";
    let name = filename || "media";
    if (!/\.[a-z0-9]{2,4}$/i.test(name)) {
        name += type.startsWith("video/") ? ".mp4" : ".jpg";
    }
    try {
        const file = new File([blob], name, { type });
        rec.fileOk = true;
        rec.fileType = file.type;
        rec.fileName = file.name;
        rec.fileSize = file.size;
        rec.step = "ok";
        return file;
    } catch (e) {
        rec.fileOk = false;
        rec.fileThrew = `${e?.name || "Error"}: ${e?.message || ""}`.trim();
        rec.step = "file_construction_failed";
        return null;
    }
}

/**
 * Prepare a single share-ready `File` on genuine user intent, ahead of the tap.
 *
 * Reuses the exact same `urlToFileTraced` flow (same authenticated media proxy,
 * viewer token, and Download-permission gate) as the share-time fetch — no
 * second download implementation. Returns a `File` on success or `null` on
 * failure (never throws). The caller holds the result in transient memory only
 * and passes it back via `shareMediaViaWhatsApp({ preparedFiles })` so that
 * `navigator.share()` can run synchronously inside the tap's user activation
 * (the fix for iOS Safari's first-tap `NotAllowedError`).
 */
export async function prepareShareFile({ slug, talentId, item }) {
    if (!slug || !talentId || !item || !item.id) return null;
    return urlToFileTraced(
        `/public/links/${slug}/media/${talentId}/${item.id}`,
        item.filename || item.name,
        item.type === "video" ? "video/mp4" : "image/jpeg",
        { name: item.name, type: item.type },
        { headers: { Authorization: `Bearer ${getViewerToken(slug)}` } },
    );
}

/** Mint (or reuse) a secure, viewer-scoped Talentgram share link for one media item. */
async function mintShareUrl(slug, talentId, mediaId) {
    const { data } = await axios.post(
        `/public/links/${slug}/share`,
        { talent_id: talentId, media_id: mediaId },
        { headers: { Authorization: `Bearer ${getViewerToken(slug)}` } },
    );
    return {
        shareId: data.share_id,
        url: `${getSubdomainUrl("links")}/${slug}?share=${data.share_id}`,
    };
}

async function logDispatch(slug, talentId, method, fileCount, media, sessionId) {
    try {
        await axios.post(
            `/public/links/${slug}/share-log`,
            {
                talent_id: talentId,
                share_method: method,
                file_count: fileCount,
                media,
                session_id: sessionId,
            },
            { headers: { Authorization: `Bearer ${getViewerToken(slug)}` } },
        );
    } catch (e) {
        // Analytics must never break the share UX.
        console.error("share-log failed", e);
    }
}

// Attempts ONE native navigator.share({files}) call for an already-ready
// batch of Files, with the same NotAllowedError-retry-once policy used
// everywhere else in this module. Extracted so the combined-batch attempt
// and the mixed-media homogeneous-batch fallback (below) share this exact
// retry logic instead of duplicating it.
async function attemptNativeFileShare(filesForBatch, text, titleText) {
    const shareData = { files: filesForBatch, title: titleText, text };
    for (let attempt = 1; attempt <= 2; attempt++) {
        try {
            await navigator.share(shareData);
            return { ok: true, attempt };
        } catch (e) {
            if (e && e.name === "AbortError") return { ok: false, aborted: true };
            if (e && e.name === "NotAllowedError" && attempt === 1) continue; // retry immediately
            if (e && e.name === "NotAllowedError") return { ok: false, blocked: true };
            return { ok: false, error: e };
        }
    }
    return { ok: false, error: new Error("unreachable") };
}

function buildWhatsAppMessage(talentName, lines, formText) {
    const parts = [`*${talentName}*`, "Shared via Talentgram", ""];
    for (const l of lines) {
        parts.push(l.label);
        parts.push(l.url);
        parts.push("");
    }
    if (formText) {
        parts.push(formText);
        parts.push("");
    }
    parts.push("Talentgram Agency");
    return parts.join("\n").trim();
}

/**
 * Share one or more media items via WhatsApp.
 *
 * @param {object}  opts
 * @param {string}  opts.slug
 * @param {string}  opts.talentId
 * @param {string}  opts.talentName        already privatized display name
 * @param {Array}   opts.items             [{ id, name, type:'video'|'image', fileUrl, filename }]
 * @param {boolean} opts.allowFiles        whether native FILE sharing is permitted — pass the
 *                                         link's `visibility.download` flag here. See the gate
 *                                         comment below: this is an INTENTIONAL product decision.
 * @param {string=} opts.sessionId
 * @param {string=} opts.formText          optional Talent Details Form text, appended as its
 *                                         own segment (separate from `caption`) in both the
 *                                         native-share text and the link-fallback message. When
 *                                         `items` is empty and `formText` is set, this is a
 *                                         form-only share — the file-share attempt is skipped and
 *                                         the SAME link-fallback code path sends a text-only message.
 * @returns {Promise<{ method?: string, count?: number, aborted?: boolean }>}
 *          `method: "native_file_share_split"` is a distinct outcome: a mixed
 *          image+video selection whose COMBINED canShare() was rejected but
 *          whose two homogeneous subsets (images alone, videos alone) each
 *          pass — one subset (`sentType`/`sentCount`) has already been sent
 *          as a real native file share; the other (`remainingType`/
 *          `remainingCount`) was NOT sent and must be offered as an explicit,
 *          separately user-initiated follow-up share (see ClientView.jsx's
 *          handling of this method) — never silently dropped, never implied
 *          as already sent.
 *
 * Behaviour matrix (intentional — preserves the existing security model):
 *
 *   Downloads ENABLED   →  Mobile: attach actual media files via the OS share
 *                                  sheet (user picks WhatsApp / WhatsApp Business)
 *                          Desktop: secure Talentgram links (wa.me)
 *   Downloads DISABLED  →  Mobile: secure Talentgram links via the OS share sheet
 *                          Desktop: secure Talentgram links (wa.me)
 *
 * Mobile NEVER uses wa.me (which would deep-link straight into one WhatsApp
 * variant). It always goes through navigator.share so the OS presents a chooser.
 */
export async function shareMediaViaWhatsApp({
    slug,
    talentId,
    talentName,
    items,
    allowFiles = true,
    sessionId,
    caption,
    formText,
    preparedFiles,
}) {
    items = items || [];
    if (items.length === 0 && !formText) return { aborted: true };

    const hasShare = typeof navigator !== "undefined" && typeof navigator.share === "function";
    const hasCanShare = typeof navigator !== "undefined" && typeof navigator.canShare === "function";

    // ── Full per-attempt diagnostic trace (the 9 requested data points) ──────
    const trace = {
        when: new Date().toISOString(),
        itemCount: items.length,
        allowFiles,                    // Download permission (visibility.download)
        q1_navigatorShare: hasShare,   // 1. navigator.share exists?
        q2_navigatorCanShare: hasCanShare, // 2. navigator.canShare exists?
        q3_canShareFiles: null,        // 3. navigator.canShare({files}) result
        q4_allFetched: null,           // 4. every media fetched?
        q5_allHttp200: null,           // 5. every fetch HTTP 200?
        q6_allBlobOk: null,            // 6. blob creation succeeded (all)?
        q7_allFileOk: null,            // 7. File construction succeeded (all)?
        q8_path: null,                 // 8. "Native File Share" | "Link Fallback"
        q9_reason: null,               // 9. exact reason if Link Fallback
        perItem: [],
        ua: typeof navigator !== "undefined" ? navigator.userAgent : "n/a",
    };
    const emit = (result) => {
        shareLog("TRACE", trace);
        return { ...result, trace };
    };

    // INTENTIONAL PRODUCT DECISION — the Download permission (`allowFiles`) is the
    // single control over whether an ORIGINAL file may leave Talentgram. Native
    // file sharing is download-equivalent, so it is only offered when downloads
    // are enabled; otherwise we share secure links on every device.
    const willTryFiles = allowFiles && hasShare && items.length > 0;

    // Desktop popup-safety: window.open() after an await is popup-blocked. We
    // only use window.open on DESKTOP (no native sheet), so pre-open there and
    // redirect after minting. On mobile we use navigator.share (no window).
    let linkWin = null;
    if (!hasShare && typeof window !== "undefined") {
        linkWin = window.open("", "_blank");
        if (linkWin) { try { linkWin.opener = null; } catch { /* ignore */ } }
    }

    // ── 1 & 2: native FILE share (attach the real media) ─────────────────────
    if (willTryFiles) {
        // Pre-seed one record per item so we always report all of them.
        const recs = items.map((it) => ({ name: it.name, type: it.type }));
        trace.perItem = recs;

        // ── Fast path: reuse whatever is ALREADY prepared, per item ──────────
        // Every item that has a real pre-fetched File (from genuine intent —
        // opening the talent pre-warms videos, selecting an item pre-warms it
        // too) is used WITHOUT any fetch/await, so navigator.share() has as
        // little async work as possible between the tap and the call —
        // WebKit invalidates transient user activation across an await, and
        // Chrome's own activation window can equally be exceeded when a
        // multi-file, video-heavy selection has to fetch everything fresh.
        // A PARTIALLY prepared selection (the common case for a mixed
        // image+video pick — videos are pre-warmed well before the tap,
        // images only start preparing on selection) must still use the
        // videos that ARE ready rather than discarding them and re-fetching
        // everything: only the few still-missing items are actually
        // fetched here, in parallel with each other.
        const authHeader = { Authorization: `Bearer ${getViewerToken(slug)}` };
        const files = await Promise.all(
            items.map((it, i) => {
                const already = preparedFiles && preparedFiles.get(it.id);
                if (already instanceof File) {
                    recs[i].fetchOk = true; recs[i].httpStatus = 200; recs[i].blobOk = true; recs[i].fileOk = true; recs[i].step = "ok";
                    recs[i].prepared = true;
                    return already;
                }
                return urlToFileTraced(
                    `/public/links/${slug}/media/${talentId}/${it.id}`,
                    it.filename || it.name,
                    it.type === "video" ? "video/mp4" : "image/jpeg",
                    recs[i],
                    { headers: authHeader },
                );
            }),
        );
        trace.preparedFastPath = recs.every((r) => r.prepared === true);
        trace.preparedCount = recs.filter((r) => r.prepared === true).length;

        trace.q4_allFetched = recs.every((r) => r.fetchOk === true);
        trace.q5_allHttp200 = recs.every((r) => r.httpStatus === 200);
        trace.q6_allBlobOk = recs.every((r) => r.blobOk === true);
        trace.q7_allFileOk = recs.every((r) => r.fileOk === true);

        const goodFiles = files.filter(Boolean);
        const allFilesReady = goodFiles.length === items.length && goodFiles.length > 0;

        if (!allFilesReady) {
            // Derive the exact reason from the first item that failed.
            const bad = recs.find((r) => r.step && r.step !== "ok");
            trace.q3_canShareFiles = false;
            trace.q8_path = "Link Fallback";
            trace.q9_reason = bad
                ? {
                      opaque_response_no_cors: "CORS blocked (opaque response — media host sent no Access-Control-Allow-Origin)",
                      fetch_threw_cors_or_network: `fetch failed / CORS blocked (${bad.fetchThrew || "network error"})`,
                      blob_failed: `Blob creation failed (${bad.blobThrew || ""})`,
                      file_construction_failed: `File construction failed (${bad.fileThrew || ""})`,
                  }[bad.step] || `fetch failed (${bad.step}${bad.httpStatus ? " HTTP " + bad.httpStatus : ""})`
                : "one or more files could not be prepared";
        } else {
            // Files are ready — ask the platform if it will share them.
            const canFiles = hasCanShare ? canShareFiles(goodFiles) : false;
            trace.q3_canShareFiles = canFiles;
            const shareMeta = items.map((it) => ({ id: it.id, type: it.type, name: it.name }));
            const text = [caption || `${talentName} · Shared via Talentgram`, formText].filter(Boolean).join("\n\n");
            const titleText = text.split("\n")[0];

            if (canFiles) {
                // iOS Safari intermittently throws NotAllowedError on the first
                // attempt even though an immediate retry succeeds. Retry once
                // before giving up, and NEVER surface the raw exception.
                const attemptResult = await attemptNativeFileShare(goodFiles, text, titleText);
                if (attemptResult.ok) {
                    trace.q8_path = attemptResult.attempt === 1 ? "Native File Share" : "Native File Share (retry)";
                    await logDispatch(slug, talentId, "native_file_share", goodFiles.length, shareMeta, sessionId);
                    return emit({ method: "native_file_share", count: goodFiles.length });
                }
                if (attemptResult.aborted) {
                    trace.q8_path = "Native File Share (cancelled)";
                    trace.q9_reason = "user cancelled the share sheet";
                    return emit({ aborted: true });
                }
                if (attemptResult.blocked) {
                    // Still blocked after the retry — ask the user to tap Send
                    // again (a manual second tap attaches the files). Do NOT
                    // fall back to links here.
                    trace.q8_path = "Share sheet blocked (retry failed)";
                    trace.q9_reason = "share sheet blocked by the browser after retry";
                    return emit({ method: "share_blocked" });
                }
                // Any other error → fall through to the secure-link path.
                trace.q8_path = "Link Fallback";
                trace.q9_reason = `navigator.share({files}) threw: ${attemptResult.error?.name}: ${attemptResult.error?.message || ""}`.trim();
            } else {
                // ── Mixed-media fallback ─────────────────────────────────────
                // A combined image+video batch can be rejected by canShare()
                // even when browsers/devices happily share EACH type alone —
                // found live on a real Android device: 3 videos alone, and
                // images alone, share fine; adding even one image to the video
                // selection makes the combined payload fail canShare(). This is
                // a genuine platform/device capability boundary, not something
                // our own file/MIME construction controls (images and videos
                // already go through identical, correctly-typed File
                // construction above). Rather than silently downgrading a
                // mixed selection straight to a links-only message, try the
                // two type-homogeneous batches as separate native shares —
                // never claiming more was sent than actually was.
                const videoPairs = items.map((it, i) => ({ it, file: files[i] })).filter((p) => p.it.type === "video" && p.file);
                const imagePairs = items.map((it, i) => ({ it, file: files[i] })).filter((p) => p.it.type !== "video" && p.file);
                const isMixed = videoPairs.length > 0 && imagePairs.length > 0;
                const videoFiles = videoPairs.map((p) => p.file);
                const imageFiles = imagePairs.map((p) => p.file);
                const canVideosAlone = isMixed && hasCanShare ? canShareFiles(videoFiles) : false;
                const canImagesAlone = isMixed && hasCanShare ? canShareFiles(imageFiles) : false;

                if (isMixed && canVideosAlone && canImagesAlone) {
                    // Videos first — the larger, empirically-working batch —
                    // using the SAME retry policy as a normal share.
                    const videoMeta = videoPairs.map((p) => ({ id: p.it.id, type: p.it.type, name: p.it.name }));
                    const firstAttempt = await attemptNativeFileShare(videoFiles, text, titleText);
                    if (firstAttempt.ok) {
                        trace.q8_path = "Native File Share (split: videos first)";
                        trace.q9_reason = "combined mixed-media canShare() rejected; shared as two homogeneous batches";
                        await logDispatch(slug, talentId, "native_file_share", videoFiles.length, videoMeta, sessionId);
                        return emit({
                            method: "native_file_share_split",
                            sentType: "video",
                            sentCount: videoFiles.length,
                            remainingType: "image",
                            remainingCount: imageFiles.length,
                        });
                    }
                    if (firstAttempt.aborted) {
                        trace.q8_path = "Native File Share (cancelled)";
                        trace.q9_reason = "user cancelled the share sheet (split attempt, first batch)";
                        return emit({ aborted: true });
                    }
                    if (firstAttempt.blocked) {
                        trace.q8_path = "Share sheet blocked (retry failed, split attempt)";
                        trace.q9_reason = "share sheet blocked by the browser after retry (split attempt, first batch)";
                        return emit({ method: "share_blocked" });
                    }
                    // Any other error on the split attempt → fall through to
                    // the ordinary combined secure-link fallback below (never a
                    // second, different failure mode to reason about).
                    trace.q8_path = "Link Fallback";
                    trace.q9_reason = `split-share attempt threw: ${firstAttempt.error?.name}: ${firstAttempt.error?.message || ""}`.trim();
                } else {
                    trace.q8_path = "Link Fallback";
                    trace.q9_reason = hasCanShare
                        ? isMixed
                            ? "navigator.canShare({files}) rejected the combined batch AND at least one homogeneous subset (images/videos alone)"
                            : "navigator.canShare({files}) returned false (browser won't share these file types)"
                        : "navigator.canShare is unavailable (can't offer file sharing safely)";
                }
            }
        }
    } else {
        trace.q8_path = "Link Fallback";
        trace.q9_reason = items.length === 0
            ? "form-only share (no media selected)"
            : !allowFiles
            ? "downloads disabled for this link (files intentionally not shared)"
            : "navigator.share unavailable (no native sharing)";
    }

    // ── 3: secure LINK share ─────────────────────────────────────────────────
    try {
        const lines = [];
        const mediaMeta = [];
        for (const it of items) {
            const { shareId, url } = await mintShareUrl(slug, talentId, it.id);
            lines.push({ label: it.name, url });
            mediaMeta.push({ id: it.id, type: it.type, name: it.name, share_id: shareId });
        }
        const text = buildWhatsAppMessage(talentName, lines, formText);

        // Mobile: share the message via the OS sheet (user picks WhatsApp vs
        // Business). wa.me deep-links one app — reserved for DESKTOP.
        if (hasShare && !linkWin) {
            try {
                await navigator.share({ title: `${talentName} — Talentgram`, text });
                await logDispatch(slug, talentId, "whatsapp_link_share", items.length, mediaMeta, sessionId);
                return emit({ method: "whatsapp_link_share", via: "native_sheet", count: items.length, reason: trace.q9_reason });
            } catch (e) {
                if (e && e.name === "AbortError") return emit({ aborted: true });
                shareLog("native sheet for links failed → wa.me", { name: e?.name });
            }
        }

        const waUrl = `https://wa.me/?text=${encodeURIComponent(text)}`;
        if (linkWin) linkWin.location.href = waUrl;
        else window.open(waUrl, "_blank", "noopener,noreferrer");
        await logDispatch(slug, talentId, "whatsapp_link_share", items.length, mediaMeta, sessionId);
        return emit({ method: "whatsapp_link_share", via: "wa_me", count: items.length, reason: trace.q9_reason });
    } catch (e) {
        if (linkWin) { try { linkWin.close(); } catch { /* ignore */ } }
        trace.q9_reason = (trace.q9_reason ? trace.q9_reason + " | " : "") + `link share failed: ${e?.name}: ${e?.message || ""}`;
        shareLog("link share FAILED", { name: e?.name, message: e?.message });
        return emit({ method: "error", error: String(e?.message || e) });
    }
}
