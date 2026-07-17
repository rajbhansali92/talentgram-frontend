import axios from "axios";
import { createAxiosCompatibleClient } from "@/lib/requestManager/axiosShim";

// Routing/policy for the PUBLIC `api` instance only (frontend/src/lib/api.js)
// — the first real Request Manager adoption. adminApi/portalApi/viewerApi
// are untouched and stay exactly as they are today.
//
// Parameterized rather than importing `API`/`PORTAL_TOKEN_KEY` from
// lib/api.js, so this module has no dependency on api.js — api.js depends
// on this module (for the `api` export), not the other way around. That
// keeps the two files acyclic without duplicating the backend-URL /
// portal-token-key constants.

// Kept byte-for-byte identical to the interceptor api.js has always used —
// only relocated so it can be attached to both axios instances below.
function createAuthInterceptor(portalTokenKey) {
    return (cfg) => {
        try {
            const hasAuth =
                cfg.headers?.Authorization ||
                cfg.headers?.authorization ||
                cfg.headers?.common?.Authorization;
            if (!hasAuth && typeof window !== "undefined") {
                const t = localStorage.getItem(portalTokenKey);
                if (t) cfg.headers.Authorization = `Bearer ${t}`;
            }
        } catch (e) {
            // localStorage unavailable (SSR / privacy mode) — proceed unauthenticated.
        }
        return cfg;
    };
}

// Structural, not call-site-driven: a caller never has to say "this is a
// download" or "this is an upload" — the existing axios config shape
// (`responseType`, a FormData body) already tells us. Components stay
// completely unaware that routing exists.
export function classifyRequestKind(config) {
    if (config.responseType === "blob" || config.responseType === "arraybuffer") return "download";
    if (typeof FormData !== "undefined" && config.data instanceof FormData) return "upload";
    return "standard";
}

// Builds the public `api` client: standard JSON traffic goes through the
// same-origin reverse proxy (frontend/src/app/api/proxy/[...path]/route.js);
// downloads (blob/arraybuffer responses) and multipart uploads (FormData
// bodies) stay pointed directly at Railway for now.
//
// This split is interim, not permanent architecture: the proxy already
// streams request/response bodies byte-for-byte and should handle both
// cases correctly, but Vercel's function-duration limit for the proxy
// route isn't visible anywhere in this repo (no `maxDuration` export, no
// vercel.json) and can't be verified without a live deployment. Until
// large-ZIP and multipart streaming are validated end-to-end in staging
// with production-sized payloads, both stay on the direct path. Flipping
// either to the proxy afterward is a one-line change to ROUTES below — the
// routing table is the only thing that needs to change, not this module's
// structure.
export function createPublicApiClient({ backendApiUrl, portalTokenKey }) {
    const authInterceptor = createAuthInterceptor(portalTokenKey);

    const railwayAxios = axios.create({ baseURL: backendApiUrl });
    railwayAxios.interceptors.request.use(authInterceptor);

    // Relative and same-origin: resolves against the current page's own
    // origin in every environment (localhost, preview, production) with no
    // env var of its own — the proxy route itself is inherently same-origin.
    const proxyAxios = axios.create({ baseURL: "/api/proxy" });
    proxyAxios.interceptors.request.use(authInterceptor);

    const ROUTES = {
        standard: proxyAxios,
        download: railwayAxios,
        upload: railwayAxios,
    };

    function routingTransport(config) {
        return ROUTES[classifyRequestKind(config)].request(config);
    }

    // Stock Request Manager defaults, no policy overrides — this is
    // adoption of already-reviewed infrastructure, not new design.
    const client = createAxiosCompatibleClient(routingTransport);

    // Tags each outgoing config with which physical transport it was routed
    // to, purely so Request Manager's diagnostics history (getHistory())
    // can show it — classifyRequestKind() is already computed per-request
    // internally by routingTransport; this just makes that same decision
    // visible for support/diagnostics instead of staying purely internal.
    // Doesn't affect routing itself (ROUTES above is still what decides
    // where a request actually goes) or any retry/circuit/dedup behavior.
    function tag(config) {
        const kind = classifyRequestKind(config);
        return { ...config, transportKind: kind === "standard" ? "proxy" : "railway-direct" };
    }

    return {
        ...client,
        request: (config) => client.request(tag(config)),
        get: (url, config = {}) => client.get(url, tag(config)),
        post: (url, data, config = {}) => client.post(url, data, tag({ ...config, data })),
        put: (url, data, config = {}) => client.put(url, data, tag({ ...config, data })),
        patch: (url, data, config = {}) => client.patch(url, data, tag({ ...config, data })),
        delete: (url, config = {}) => client.delete(url, tag(config)),
    };
}
