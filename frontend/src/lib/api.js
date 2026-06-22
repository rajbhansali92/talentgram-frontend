import axios from "axios";

// ✅ STEP 1: Backend URL with fallback (CRITICAL FIX)
const BACKEND_URL =
    process.env.REACT_APP_BACKEND_URL ||
    "https://talentgram-app-production.up.railway.app";

// ✅ STEP 2: API base
export const API = `${BACKEND_URL}/api`;

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

// 🔍 Debug (remove later if needed)
console.log("🚀 Backend URL:", BACKEND_URL);
console.log("🚀 API URL:", API);

/**
 * Resolve the display URL for a media object.
 * Post-Cloudinary migration every record carries a canonical full URL
 * (https://res.cloudinary.com/talentgram/...) on the `url` field — we
 * just return that.
 */
export const IMAGE_URL = (media) => {
    if (!media) return "";
    if (typeof media === "string") return media;
    return media.url || "";
};

// ================= PUBLIC CLIENT API =================

export const api = axios.create({ baseURL: API });

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

// ================= PORTAL API =================
// Talent self-service portal. Auth is a signed portal session token minted by
// the backend only after OTP/Google email-ownership verification. The token —
// not the localStorage email — is the credential.

export const PORTAL_TOKEN_KEY = "talentgram_portal_token";

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