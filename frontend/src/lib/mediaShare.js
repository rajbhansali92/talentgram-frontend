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
import { api as axios, getViewerToken, PUBLIC_FRONTEND_URL } from "@/lib/api";

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

async function urlToFile(url, filename, mimeHint) {
    const res = await fetch(url, { mode: "cors" });
    if (!res.ok) throw new Error(`fetch ${res.status}`);
    const blob = await res.blob();
    const type = blob.type || mimeHint || "application/octet-stream";
    // Ensure the filename has an extension WhatsApp recognises.
    let name = filename || "media";
    if (!/\.[a-z0-9]{2,4}$/i.test(name)) {
        name += type.startsWith("video/") ? ".mp4" : ".jpg";
    }
    return new File([blob], name, { type });
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
 *   Downloads ENABLED   →  Mobile: attach actual media files via native WhatsApp
 *                          Desktop: secure Talentgram links
 *   Downloads DISABLED  →  Mobile: secure Talentgram links ONLY
 *                          Desktop: secure Talentgram links ONLY
 */
export async function shareMediaViaWhatsApp({
    slug,
    talentId,
    talentName,
    items,
    allowFiles = true,
    sessionId,
}) {
    if (!items || items.length === 0) return { aborted: true };

    const willTryNative = allowFiles && canNativeShare();

    // Popup-blocker safety: minting secure links is async, and browsers (esp.
    // Safari) block window.open() that runs after an await — it's no longer
    // "inside" the click gesture. When we already know the link path will be
    // used (no native sharing), open the tab synchronously now and redirect it
    // after the links are ready. (When native sharing will run we skip this so
    // we don't leave a stray blank tab behind.)
    let linkWin = null;
    if (!willTryNative && typeof window !== "undefined") {
        linkWin = window.open("", "_blank");
        if (linkWin) {
            try { linkWin.opener = null; } catch { /* ignore */ }
        }
    }

    // ── 1 & 2: native FILE share (mobile) ────────────────────────────────────
    // INTENTIONAL PRODUCT DECISION — not a technical limitation:
    // The link's Download permission (`visibility.download`, passed in as
    // `allowFiles`) is the single control that governs whether an ORIGINAL media
    // file is allowed to leave Talentgram. Native file sharing hands the real
    // file to the recipient (who can then save/forward it), so it is treated as
    // download-equivalent and only offered when downloads are enabled.
    // When downloads are disabled we deliberately skip this branch and fall
    // through to secure-link sharing on EVERY device (mobile + desktop), so the
    // download restriction can never be bypassed via the share sheet.
    // Do not remove the `allowFiles` guard without an explicit product sign-off.
    if (willTryNative) {
        try {
            const files = [];
            for (const it of items) {
                files.push(
                    await urlToFile(
                        it.fileUrl,
                        it.filename || it.name,
                        it.type === "video" ? "video/mp4" : "image/jpeg",
                    ),
                );
            }
            const shareData = {
                files,
                title: `${talentName} — Talentgram`,
                text: `${talentName} · Shared via Talentgram`,
            };
            if (files.length > 0 && canShareFiles(files)) {
                await navigator.share(shareData);
                await logDispatch(
                    slug,
                    talentId,
                    "native_file_share",
                    files.length,
                    items.map((it) => ({ id: it.id, type: it.type, name: it.name })),
                    sessionId,
                );
                return { method: "native_file_share", count: files.length };
            }
            // Multi-file unsupported but a single file is — share just the first.
            if (files.length === 1 && canShareFiles([files[0]])) {
                await navigator.share({ ...shareData, files: [files[0]] });
                await logDispatch(
                    slug,
                    talentId,
                    "native_file_share",
                    1,
                    [{ id: items[0].id, type: items[0].type, name: items[0].name }],
                    sessionId,
                );
                return { method: "native_file_share", count: 1 };
            }
        } catch (e) {
            // User dismissed the native sheet — respect that, don't pop WhatsApp web.
            if (e && e.name === "AbortError") return { aborted: true };
            // Any other failure (fetch/CORS/gesture) → fall through to link share.
            console.warn("native file share failed, falling back to links", e);
        }
    }

    // ── 3: secure WhatsApp LINK share (desktop / fallback) ───────────────────
    try {
        const lines = [];
        const mediaMeta = [];
        for (const it of items) {
            const { shareId, url } = await mintShareUrl(slug, talentId, it.id);
            lines.push({ label: it.name, url });
            mediaMeta.push({ id: it.id, type: it.type, name: it.name, share_id: shareId });
        }
        const waUrl = `https://wa.me/?text=${encodeURIComponent(buildWhatsAppMessage(talentName, lines))}`;
        if (linkWin) {
            // Redirect the tab we pre-opened inside the click gesture.
            linkWin.location.href = waUrl;
        } else {
            // Fallback (e.g. native share failed and we landed here): best-effort.
            window.open(waUrl, "_blank", "noopener,noreferrer");
        }
        await logDispatch(slug, talentId, "whatsapp_link_share", items.length, mediaMeta, sessionId);
        return { method: "whatsapp_link_share", count: items.length };
    } catch (e) {
        if (linkWin) {
            try { linkWin.close(); } catch { /* ignore */ }
        }
        throw e;
    }
}
