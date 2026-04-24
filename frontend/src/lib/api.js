import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;
export const FILE_URL = (path) => `${API}/files/${path}`;
/**
 * Prefer the 1600px JPEG copy if the backend has generated one (images only).
 * Falls back to the original upload — used for downloads + non-image media.
 */
export const IMAGE_URL = (media) => {
    const p = (media && (media.resized_storage_path || media.storage_path)) || media;
    return FILE_URL(p);
};

export const adminApi = axios.create({ baseURL: API });
adminApi.interceptors.request.use((cfg) => {
    const t = localStorage.getItem("tg_admin_token");
    if (t) cfg.headers.Authorization = `Bearer ${t}`;
    return cfg;
});
// Centralised 401 handler: any "Session expired" response from the backend
// (e.g. after a password change on another device, or a token-version bump)
// clears the local session and bounces the user to the login screen.
adminApi.interceptors.response.use(
    (r) => r,
    (err) => {
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail || "";
        const onAuthPage =
            typeof window !== "undefined" &&
            /\/admin\/login|\/forgot-password|\/reset-password|\/signup/.test(window.location.pathname);
        if (
            status === 401 &&
            !onAuthPage &&
            /session expired|invalid token|not authenticated/i.test(detail)
        ) {
            try {
                localStorage.removeItem("tg_admin_token");
                localStorage.removeItem("tg_admin");
            } catch {}
            window.location.href = "/admin/login";
        }
        return Promise.reject(err);
    },
);

export const viewerApi = axios.create({ baseURL: API });
viewerApi.interceptors.request.use((cfg) => {
    const slug = cfg.__slug;
    const t = slug ? localStorage.getItem(`tg_viewer_${slug}`) : null;
    if (t) cfg.headers.Authorization = `Bearer ${t}`;
    return cfg;
});

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
/**
 * Role check — never trust this for authorization (backend is the source of
 * truth). Safe to use for UI affordances like hiding buttons / routes.
 */
export function isAdmin() {
    const a = getAdmin();
    return a?.role === "admin";
}
export function getRole() {
    return getAdmin()?.role || null;
}
export function saveViewerToken(slug, token) {
    localStorage.setItem(`tg_viewer_${slug}`, token);
}
export function getViewerToken(slug) {
    return localStorage.getItem(`tg_viewer_${slug}`);
}
