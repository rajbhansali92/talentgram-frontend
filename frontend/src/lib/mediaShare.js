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
import { api as axios, getViewerToken, PUBLIC_FRONTEND_URL, API } from "@/lib/api";

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

// Fully instrumented fetch→Blob→File. Writes every step's outcome into `rec`
// (mutated in place) so the caller can report the exact runtime reason, and
// returns the File on success or null on failure (never throws).
async function urlToFileTraced(url, filename, mimeHint, rec, init = {}) {
    rec.url = url;
    // Step 5 — HTTP fetch
    let res;
    try {
        res = await fetch(url, { mode: "cors", ...init });
    } catch (e) {
        // A thrown fetch (TypeError "Failed to fetch") is the classic signature
        // of a CORS block or network failure — the response never arrives.
        rec.fetchThrew = `${e?.name || "Error"}: ${e?.message || ""}`.trim();
        rec.fetchOk = false;
        rec.step = "fetch_threw_cors_or_network";
        return null;
    }
    rec.httpStatus = res.status;
    rec.fetchOk = res.ok;
    rec.responseType = res.type; // "cors" | "opaque" | "basic" — opaque means no CORS access
    if (res.type === "opaque") {
        // Opaque responses (no CORS headers) can't be read into a usable Blob.
        rec.step = "opaque_response_no_cors";
        return null;
    }
    if (!res.ok) {
        rec.step = `http_${res.status}`;
        return null;
    }
    // Step 6 — Blob
    let blob;
    try {
        blob = await res.blob();
    } catch (e) {
        rec.blobOk = false;
        rec.blobThrew = `${e?.name || "Error"}: ${e?.message || ""}`.trim();
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
        `${API}/public/links/${slug}/media/${talentId}/${item.id}`,
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
        url: `${PUBLIC_FRONTEND_URL}/l/${slug}?share=${data.share_id}`,
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

function buildWhatsAppMessage(talentName, lines) {
    const parts = [`*${talentName}*`, "Shared via Talentgram", ""];
    for (const l of lines) {
        parts.push(l.label);
        parts.push(l.url);
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
 * @returns {Promise<{ method?: string, count?: number, aborted?: boolean }>}
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
    preparedFiles,
}) {
    if (!items || items.length === 0) return { aborted: true };

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
    const willTryFiles = allowFiles && hasShare;

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

        // ── iOS Safari fast path (SYNCHRONOUS) ───────────────────────────────
        // If the caller pre-prepared a real File for EVERY item (on genuine
        // intent, via prepareShareFile), use them WITHOUT any fetch/await so
        // navigator.share() runs inside the tap's transient user activation —
        // which WebKit invalidates across an await. When any File is missing we
        // fall through to the exact same fetch path as before (never regresses
        // Android; still works via the manual second tap on iOS).
        let files;
        const allPrepared =
            preparedFiles &&
            items.length > 0 &&
            items.every((it) => preparedFiles.get(it.id) instanceof File);
        if (allPrepared) {
            files = items.map((it) => preparedFiles.get(it.id));
            recs.forEach((r) => { r.fetchOk = true; r.httpStatus = 200; r.blobOk = true; r.fileOk = true; r.step = "ok"; });
            trace.preparedFastPath = true;
        } else {
            // Fetch through Talentgram's own authenticated media proxy (same backend,
            // CORS-enabled) instead of the raw Cloudflare Stream / R2 URL, which sends
            // no CORS headers and fails the browser fetch. The proxy resolves the real
            // storage URL server-side (never exposed) and enforces the same viewer
            // auth + Download permission. urlToFileTraced never throws (records the
            // failure in its rec), so every item is reported. Parallel keeps total
            // time short so the transient user-activation for navigator.share holds.
            const authHeader = { Authorization: `Bearer ${getViewerToken(slug)}` };
            files = await Promise.all(
                items.map((it, i) =>
                    urlToFileTraced(
                        `${API}/public/links/${slug}/media/${talentId}/${it.id}`,
                        it.filename || it.name,
                        it.type === "video" ? "video/mp4" : "image/jpeg",
                        recs[i],
                        { headers: authHeader },
                    ),
                ),
            );
        }

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
            const text = caption || `${talentName} · Shared via Talentgram`;
            const shareData = {
                files: goodFiles,
                title: text.split("\n")[0],
                text,
            };
            if (canFiles) {
                // iOS Safari intermittently throws NotAllowedError on the first
                // attempt even though an immediate retry succeeds. Retry once
                // before giving up, and NEVER surface the raw exception.
                for (let attempt = 1; attempt <= 2; attempt++) {
                    try {
                        await navigator.share(shareData);
                        trace.q8_path = attempt === 1 ? "Native File Share" : "Native File Share (retry)";
                        await logDispatch(slug, talentId, "native_file_share", goodFiles.length, shareMeta, sessionId);
                        return emit({ method: "native_file_share", count: goodFiles.length });
                    } catch (e) {
                        if (e && e.name === "AbortError") {
                            trace.q8_path = "Native File Share (cancelled)";
                            trace.q9_reason = "user cancelled the share sheet";
                            return emit({ aborted: true });
                        }
                        if (e && e.name === "NotAllowedError" && attempt === 1) {
                            trace.q9_reason = "share sheet NotAllowedError (retrying once)";
                            continue; // retry immediately
                        }
                        if (e && e.name === "NotAllowedError") {
                            // Still blocked after the retry — ask the user to tap
                            // Send again (a manual second tap attaches the files).
                            // Do NOT fall back to links here.
                            trace.q8_path = "Share sheet blocked (retry failed)";
                            trace.q9_reason = "share sheet blocked by the browser after retry";
                            return emit({ method: "share_blocked" });
                        }
                        // Any other error → fall through to the secure-link path.
                        trace.q8_path = "Link Fallback";
                        trace.q9_reason = `navigator.share({files}) threw: ${e?.name}: ${e?.message || ""}`.trim();
                        break;
                    }
                }
            } else {
                trace.q8_path = "Link Fallback";
                trace.q9_reason = hasCanShare
                    ? "navigator.canShare({files}) returned false (browser won't share these file types)"
                    : "navigator.canShare is unavailable (can't offer file sharing safely)";
            }
        }
    } else {
        trace.q8_path = "Link Fallback";
        trace.q9_reason = !allowFiles
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
        const text = buildWhatsAppMessage(talentName, lines);

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
