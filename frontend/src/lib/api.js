import axios from "axios";

// ✅ STEP 1: Backend URL with fallback (CRITICAL FIX)
const BACKEND_URL =
    process.env.REACT_APP_BACKEND_URL ||
    "https://talentgram-app-production.up.railway.app";

// ✅ STEP 2: API base
export const API = `${BACKEND_URL}/api`;

// 🔍 Debug (remove later if needed)
console.log("🚀 Backend URL:", BACKEND_URL);
console.log("🚀 API URL:", API);

// v37m — Cloudinary migration. Media is served directly from Cloudinary
// via `media.url` (secure_url). Legacy `/api/files/*` proxy is removed.
// `IMAGE_URL` is kept as a thin helper that prefers `media.url`.
export const IMAGE_URL = (media) => {
    if (!media) return "";
    if (typeof media === "string") return media;
    return media.url || "";
};

/**
 * Resolve the best Cloudinary thumbnail for a talent/submission/application.
 * Order:
 *   1. The new top-level `image_url` field (set by backend `_resolve_cover_url`)
 *   2. The media[] item whose id == cover_media_id
 *   3. The first media[] item with a portfolio/indian/western/image category
 *   4. The first media[] item with any non-empty url
 *   5. null  (callers MUST handle null — never returns "undefined" or "")
 */
export const COVER_URL = (subject) => {
    if (!subject || typeof subject !== "object") return null;
    if (subject.image_url) return subject.image_url;
    const media = subject.media || [];
    if (subject.cover_media_id) {
        const c = media.find((m) => m.id === subject.cover_media_id);
        if (c?.url) return c.url;
    }
    const PRIMARY = new Set(["portfolio", "indian", "western", "image"]);
    const first = media.find((m) => PRIMARY.has(m.category) && m.url);
    if (first) return first.url;
    const any = media.find((m) => m.url);
    return any?.url || null;
};

/**
 * v37r — Video delivery transform.
 * Inserts Cloudinary's `f_auto,q_auto,w_1280,vc_auto,c_limit` transform into
 * the URL path so every viewer downloads a 720p-capped, auto-codec, auto-
 * quality version of the video — regardless of how large the original was.
 *
 * Cloudinary applies the transform on the FIRST request and caches the
 * derivative forever. Subsequent viewers get the cached optimized copy.
 *
 * Falls back gracefully if `media` doesn't have a Cloudinary URL.
 */
export const VIDEO_URL = (media) => {
    if (!media) return "";
    const raw = typeof media === "string" ? media : media.url || "";
    if (!raw) return "";
    // Only transform Cloudinary video URLs. Pattern:
    //   https://res.cloudinary.com/<cloud>/video/upload/<rest>
    //   https://res.cloudinary.com/<cloud>/video/upload/v123/<rest>
    const marker = "/video/upload/";
    const idx = raw.indexOf(marker);
    if (idx === -1) return raw;
    const transform = "f_auto,q_auto,w_1280,vc_auto,c_limit/";
    const head = raw.slice(0, idx + marker.length);
    const tail = raw.slice(idx + marker.length);
    // Avoid double-applying if already transformed
    if (tail.startsWith(transform) || /^[fq]_/.test(tail)) return raw;
    return head + transform + tail;
};

/**
 * v37r — Auto-generated video poster thumbnail.
 * Uses Cloudinary's `so_2` (start-offset 2s) frame extraction + JPG output
 * so each video has a fast-loading poster image without uploading one.
 * Returns "" for non-Cloudinary or non-video URLs.
 */
export const VIDEO_POSTER_URL = (media) => {
    if (!media) return "";
    const raw = typeof media === "string" ? media : media.url || "";
    if (!raw) return "";
    const marker = "/video/upload/";
    const idx = raw.indexOf(marker);
    if (idx === -1) return "";
    // Replace path's extension with .jpg and inject thumbnail transform.
    const head = raw.slice(0, idx + marker.length);
    let tail = raw.slice(idx + marker.length);
    tail = tail.replace(/\.[a-z0-9]{2,5}$/i, ".jpg");
    return head + "so_2,w_640,h_360,c_fill,q_auto,f_jpg/" + tail;
};

// ================= ADMIN API =================

export const adminApi = axios.create({ baseURL: API });

adminApi.interceptors.request.use((cfg) => {
    const t = localStorage.getItem("tg_admin_token");
    if (t) cfg.headers.Authorization = `Bearer ${t}`;
    return cfg;
});

// Handle expired / invalid session
adminApi.interceptors.response.use(
    (r) => r,
    (err) => {
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail || "";

        const onAuthPage =
            typeof window !== "undefined" &&
            /\/admin\/login|\/forgot-password|\/reset-password|\/signup/.test(
                window.location.pathname
            );

        if (
            status === 401 &&
            !onAuthPage &&
            /session expired|invalid token|not authenticated/i.test(detail)
        ) {
            try {
                localStorage.removeItem("tg_admin_token");
                localStorage.removeItem("tg_admin");
            } catch (e) {
                console.error(e);
            }
            window.location.href = "/admin/login";
        }

        return Promise.reject(err);
    }
);

// ================= VIEWER API =================

export const viewerApi = axios.create({ baseURL: API });

viewerApi.interceptors.request.use((cfg) => {
    const slug = cfg.__slug;
    const t = slug ? localStorage.getItem(`tg_viewer_${slug}`) : null;

    if (t) cfg.headers.Authorization = `Bearer ${t}`;
    return cfg;
});

// ================= SESSION HELPERS =================

export function saveAdminSession(token, admin) {
    localStorage.setItem("tg_admin_token", token);
    localStorage.setItem("tg_admin", JSON.stringify(admin));
}

export function clearAdminSession() {
    localStorage.removeItem("tg_admin_token");
    localStorage.removeItem("tg_admin");
}

export function getAdmin() {
    try {
        return JSON.parse(localStorage.getItem("tg_admin") || "null");
    } catch {
        return null;
    }
}

// ================= ROLE HELPERS =================

export function isAdmin() {
    const a = getAdmin();
    return a?.role === "admin";
}

export function getRole() {
    return getAdmin()?.role || null;
}

// ================= VIEWER TOKEN =================

export function saveViewerToken(slug, token) {
    localStorage.setItem(`tg_viewer_${slug}`, token);
}

export function getViewerToken(slug) {
    return localStorage.getItem(`tg_viewer_${slug}`);
}