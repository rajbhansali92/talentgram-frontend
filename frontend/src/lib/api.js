import axios from "axios";
import { createPublicApiClient } from "@/lib/publicApiTransport";

// ✅ STEP 1: Backend URL with fallback (CRITICAL FIX)
const BACKEND_URL =
    process.env.NEXT_PUBLIC_BACKEND_URL ||
    process.env.REACT_APP_BACKEND_URL ||
    "https://api.talentgramagency.com";

// ✅ STEP 2: API base
export const API = `${BACKEND_URL}/api`;

// Declared here (rather than down in the "PORTAL API" section below, where
// it conceptually lives) because the public `api` client, constructed
// further down, needs the value immediately — not just at request time —
// to pass into createPublicApiClient().
export const PORTAL_TOKEN_KEY = "talentgram_portal_token";

// ✅ Centralized Public Frontend URL to prevent Vercel preview auth wall on public links
export const PUBLIC_FRONTEND_URL = "https://talentgramagency.com";

/**
 * Resolves subdomain URLs dynamically, supporting local development.
 */
export function getSubdomainUrl(subdomain) {
    if (typeof window === "undefined") return `https://${subdomain}.talentgramagency.com`;
    const hostname = window.location.hostname;
    const port = window.location.port;
    if (hostname.includes("localhost") || hostname.includes("127.0.0.1") || hostname.includes("local")) {
        // e.g. review.localhost:3000
        const hostBase = hostname.includes("localhost") ? "localhost" : "talentgramagency.local";
        return `http://${subdomain}.${hostBase}${port ? ":" + port : ""}`;
    }
    return `https://${subdomain}.talentgramagency.com`;
}



// Formats a browser <img> cannot decode without a Cloudinary format
// negotiation. HEIC/HEIF is the default iPhone camera format and an accepted
// upload; only Safari renders it natively.
const _NON_WEB_IMAGE_RE = /\.(heic|heif)(\?|#|$)/i;

const _isHeicSource = (media, url) => {
    if (_NON_WEB_IMAGE_RE.test(url || "")) return true;
    if (media && typeof media === "object") {
        const ct = (media.content_type || "").toLowerCase();
        if (ct === "image/heic" || ct === "image/heif") return true;
        const fmt = (media.format || "").toLowerCase();
        if (fmt === "heic" || fmt === "heif") return true;
        if (_NON_WEB_IMAGE_RE.test(media.original_filename || "")) return true;
    }
    return false;
};

/**
 * Resolve the display URL for an image media object.
 *
 * Cloudinary rearchitecture P5 (RULE #2): the canonical uploaded image is the
 * primary asset and is delivered DIRECTLY. We used to inject `f_auto,q_auto`
 * at full resolution into every Cloudinary image URL — that forced a full-res
 * AVIF/WebP re-encode of every image on every rendering surface (the
 * `extra_avif_mp_encoding` line, ~11K units). JPEG/PNG/WebP are already
 * universally renderable, so we serve them untouched.
 *
 * The ONE exception is a HEIC/HEIF source (only Safari decodes it): those get a
 * single canonical `f_auto` segment — format negotiation only, no `q_auto`, no
 * width, no `dpr` — so a broken image never ships to a non-Safari browser.
 */
export const IMAGE_URL = (media) => {
    if (!media) return "";
    const url = typeof media === "string" ? media : media.url || "";
    if (!url) return url;
    // Only Cloudinary image-delivery URLs are rewritable; leave everything else.
    if (!/res\.cloudinary\.com\/[^/]+\/image\/upload\//.test(url)) return url;
    if (/(^|\/)f_[^/]*\//.test(url)) return url; // already carries a format transform
    if (!_isHeicSource(media, url)) return url; // web-safe → canonical asset, zero transform
    return url.replace(
        /(res\.cloudinary\.com\/[^/]+\/image\/upload\/)/,
        "$1f_auto/"
    );
};

// ================= PUBLIC CLIENT API =================

// Backed by Request Manager underneath (retry/timeout/dedup/circuit-breaker/
// cancellation/logging — see frontend/src/lib/publicApiTransport.js and
// frontend/src/lib/requestManager/), exposing the identical axios-instance
// surface (`.get/.post/.put/.patch/.delete`) it always has. Every existing
// call site (ClientView, SubmissionPage, ApplicationPage, and everything
// else that imports `api`) requires zero changes — the auth interceptor
// below (attaching the talent's portal session token, minted only after
// OTP/Google email-ownership verification, to calls that need to prove
// ownership of an email — /public/prefill, /public/apply, the project
// submission start endpoint; the backend ignores it where it isn't needed;
// an explicit per-request Authorization header always wins because axios
// only fills headers that are not already set) now lives in
// publicApiTransport.js so it can be attached to both of that module's
// underlying axios instances, but is byte-for-byte the same logic that used
// to be registered directly here.
export const api = createPublicApiClient({ backendApiUrl: API, portalTokenKey: PORTAL_TOKEN_KEY });

// ================= ADMIN API =================

// Axios's default array serialization is `key[]=a&key[]=b` (bracket
// notation), but FastAPI's `List[str] = Query(...)` params only recognize
// the repeated-key form `key=a&key=b` — the object-form
// `paramsSerializer: { indexes: false }` config does NOT override this (only
// takes effect via axios's own internal serializer path, not the one
// actually used when building requests — confirmed empirically), so a
// literal serializer function is required. Left broken, every array-valued
// query param (talent directory filters: tags/skills/interested_in/location)
// silently arrives server-side as an empty list — the param name never
// matches `key[]` vs `key`, so it just doesn't filter anything, with no
// error anywhere in the chain to surface it.
function repeatKeyParamsSerializer(params) {
    const parts = [];
    Object.entries(params).forEach(([key, value]) => {
        if (value === undefined || value === null) return;
        const values = Array.isArray(value) ? value : [value];
        values.forEach((v) => parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(v)}`));
    });
    return parts.join("&");
}

export const adminApi = axios.create({ baseURL: API, paramsSerializer: repeatKeyParamsSerializer });

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

// ================= PORTAL API =================
// Talent self-service portal. Auth is a signed portal session token minted by
// the backend only after OTP/Google email-ownership verification. The token —
// not the localStorage email — is the credential.

// Architecture C — direct browser→Cloudinary audition-video upload. OFF unless
// NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD is explicitly enabled at build time.
export const DIRECT_VIDEO_UPLOAD =
    String(process.env.NEXT_PUBLIC_DIRECT_VIDEO_UPLOAD || "").toLowerCase() === "true";

// PORTAL_TOKEN_KEY is declared near the top of this file (needed earlier,
// by the public `api` client's construction) — see the comment there.

export const portalApi = axios.create({ baseURL: API });

portalApi.interceptors.request.use((cfg) => {
    const t = localStorage.getItem(PORTAL_TOKEN_KEY);
    if (t) cfg.headers.Authorization = `Bearer ${t}`;
    return cfg;
});

// On an invalid/expired portal session, clear it and bounce to sign-in.
portalApi.interceptors.response.use(
    (r) => r,
    (err) => {
        if (err?.response?.status === 401) {
            try {
                localStorage.removeItem(PORTAL_TOKEN_KEY);
                localStorage.removeItem("talentgram_portal_email");
            } catch (e) {
                console.error(e);
            }
        }
        return Promise.reject(err);
    }
);

// ================= SESSION HELPERS =================

export function saveAdminSession(token, admin) {
    if (typeof window === "undefined") return;
    localStorage.setItem("tg_admin_token", token);
    localStorage.setItem("tg_admin", JSON.stringify(admin));
}

export function clearAdminSession() {
    if (typeof window === "undefined") return;
    localStorage.removeItem("tg_admin_token");
    localStorage.removeItem("tg_admin");
}

export function getAdmin() {
    if (typeof window === "undefined") return null;
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
    if (typeof window === "undefined") return;
    localStorage.setItem(`tg_viewer_${slug}`, token);
}

export function getViewerToken(slug) {
    if (typeof window === "undefined") return null;
    return localStorage.getItem(`tg_viewer_${slug}`);
}