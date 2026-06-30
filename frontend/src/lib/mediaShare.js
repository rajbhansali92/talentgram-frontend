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
}) {
    if (!items || items.length === 0) return { aborted: true };

    const nativeShare = canNativeShare();
    // INTENTIONAL PRODUCT DECISION — not a technical limitation:
    // The link's Download permission (`allowFiles` = `visibility.download`) is the
    // single control over whether an ORIGINAL file may leave Talentgram. Native
    // file sharing hands the real file to the recipient, so it is download-
    // equivalent and only offered when downloads are enabled. When disabled we
    // fall through to secure links on EVERY device. Do not remove this guard
    // without product sign-off.
    const willTryFiles = allowFiles && nativeShare;
    shareLog("start", {
        items: items.length,
        allowFiles,
        nativeShare,
        canShareFilesApi: typeof navigator !== "undefined" && typeof navigator.canShare === "function",
        willTryFiles,
        ua: typeof navigator !== "undefined" ? navigator.userAgent : "n/a",
    });

    // Desktop popup-safety: window.open() after an await is blocked by popup
    // blockers (it's no longer in the click gesture). We only use window.open on
    // DESKTOP (no native share sheet), so pre-open a tab synchronously here and
    // redirect it after minting. On mobile we use navigator.share() (no window),
    // so we must NOT pre-open — that would orphan a blank tab.
    let linkWin = null;
    if (!nativeShare && typeof window !== "undefined") {
        linkWin = window.open("", "_blank");
        if (linkWin) {
            try { linkWin.opener = null; } catch { /* ignore */ }
        }
    }

    // Reason native file sharing was abandoned (only set when we actually tried
    // and failed) — surfaced on-screen so the cause is visible without devtools.
    let fileFallbackReason = null;

    // ── 1 & 2: native FILE share (attach the real media) ─────────────────────
    if (willTryFiles) {
        try {
            shareLog("fetching files", items.map((i) => ({ name: i.name, url: i.fileUrl })));
            // Parallel fetch keeps total time short so the transient user
            // activation (needed by navigator.share, ~5s on Android) doesn't
            // expire while we download.
            const files = await Promise.all(
                items.map((it) =>
                    urlToFile(
                        it.fileUrl,
                        it.filename || it.name,
                        it.type === "video" ? "video/mp4" : "image/jpeg",
                    ),
                ),
            );
            shareLog("fetched files", files.map((f) => ({ name: f.name, type: f.type, size: f.size })));

            const shareMeta = items.map((it) => ({ id: it.id, type: it.type, name: it.name }));
            const baseData = {
                title: `${talentName} — Talentgram`,
                text: `${talentName} · Shared via Talentgram`,
            };

            if (files.length > 0 && canShareFiles(files)) {
                shareLog("path → native_file_share (all)", files.length);
                await navigator.share({ ...baseData, files });
                await logDispatch(slug, talentId, "native_file_share", files.length, shareMeta, sessionId);
                return { method: "native_file_share", count: files.length };
            }
            if (files.length === 1 && canShareFiles([files[0]])) {
                shareLog("path → native_file_share (single)");
                await navigator.share({ ...baseData, files: [files[0]] });
                await logDispatch(slug, talentId, "native_file_share", 1, [shareMeta[0]], sessionId);
                return { method: "native_file_share", count: 1 };
            }
            // Files fetched but the browser won't share them as attachments.
            fileFallbackReason = "browser can't share these files";
            shareLog("files not shareable → link fallback", {
                count: files.length,
                canShareAll: canShareFiles(files),
            });
        } catch (e) {
            if (e && e.name === "AbortError") {
                shareLog("user cancelled native sheet");
                return { aborted: true };
            }
            // TypeError "Failed to fetch" here almost always means the media
            // host (Cloudinary/R2) didn't return CORS headers for the fetch.
            fileFallbackReason =
                e?.name === "TypeError"
                    ? `couldn't fetch file (likely storage CORS): ${e?.message || ""}`.trim()
                    : `${e?.name || "error"}${e?.message ? ": " + e.message : ""}`;
            shareLog("native file share FAILED → link fallback", {
                name: e?.name,
                message: e?.message,
            });
        }
    } else {
        shareLog("skipping native files", {
            reason: !allowFiles ? "downloads_disabled" : "no_navigator_share",
        });
    }

    // ── 3: secure LINK share ─────────────────────────────────────────────────
    try {
        shareLog("minting secure links", items.length);
        const lines = [];
        const mediaMeta = [];
        for (const it of items) {
            const { shareId, url } = await mintShareUrl(slug, talentId, it.id);
            lines.push({ label: it.name, url });
            mediaMeta.push({ id: it.id, type: it.type, name: it.name, share_id: shareId });
        }
        const text = buildWhatsAppMessage(talentName, lines);

        // On mobile, share the message through the OS share sheet so the user
        // can choose WhatsApp vs WhatsApp Business (and any other target).
        // wa.me deep-links straight into one app — we reserve it for DESKTOP.
        if (nativeShare && !linkWin) {
            try {
                shareLog("path → link share via native sheet");
                await navigator.share({ title: `${talentName} — Talentgram`, text });
                await logDispatch(slug, talentId, "whatsapp_link_share", items.length, mediaMeta, sessionId);
                return { method: "whatsapp_link_share", via: "native_sheet", count: items.length, fileFallbackReason };
            } catch (e) {
                if (e && e.name === "AbortError") {
                    shareLog("user cancelled link sheet");
                    return { aborted: true };
                }
                shareLog("native sheet for links failed → wa.me", { name: e?.name });
            }
        }

        const waUrl = `https://wa.me/?text=${encodeURIComponent(text)}`;
        if (linkWin) {
            shareLog("path → link share via wa.me (pre-opened tab)");
            linkWin.location.href = waUrl;
        } else {
            shareLog("path → link share via wa.me (window.open)");
            window.open(waUrl, "_blank", "noopener,noreferrer");
        }
        await logDispatch(slug, talentId, "whatsapp_link_share", items.length, mediaMeta, sessionId);
        return { method: "whatsapp_link_share", via: "wa_me", count: items.length, fileFallbackReason };
    } catch (e) {
        if (linkWin) {
            try { linkWin.close(); } catch { /* ignore */ }
        }
        shareLog("link share FAILED", { name: e?.name, message: e?.message });
        throw e;
    }
}
