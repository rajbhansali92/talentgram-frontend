// Production-support diagnostics snapshot — Phase 7. Assembles exactly the
// fields a support engineer needs to diagnose "my review page isn't
// loading" without reproducing it locally: app version, route context,
// browser/OS/viewport, network status, Service Worker state, cache
// presence, page timing, and Request Manager's own request history.
//
// Hard rule, enforced by clientDiagnostics.test.js: this NEVER includes
// email, name, any header value, cookie, token, or OTP. Only a route slug
// (an opaque identifier, not personal data) and Request Manager's history
// entries — which are themselves already free of request bodies/headers by
// construction (see RequestManager.js's _recordHistory calls).

// Lightweight UA parse — enough to answer "which browser/OS" for support
// triage, not a fingerprinting-grade parser. Written fresh rather than
// importing SubmissionPage.jsx's near-identical `parseUserAgent` to avoid
// touching a file outside this phase's scope; a future pass could
// consolidate the two.
function parseUserAgent(ua) {
    if (!ua) return { browser: "unknown", browserVersion: "unknown", os: "unknown", osVersion: "unknown", deviceType: "desktop" };

    let deviceType = "desktop";
    if (/ipad|tablet/i.test(ua)) deviceType = "tablet";
    else if (/mobi|android|iphone|ipod/i.test(ua)) deviceType = "mobile";

    let browser = "unknown";
    let browserVersion = "unknown";
    if (/chrome|crios/i.test(ua)) {
        browser = "Chrome";
        browserVersion = ua.match(/(?:chrome|crios)\/([0-9.]+)/i)?.[1] || "unknown";
    } else if (/safari/i.test(ua) && !/chrome|crios/i.test(ua)) {
        browser = "Safari";
        browserVersion = ua.match(/version\/([0-9.]+)/i)?.[1] || "unknown";
    } else if (/firefox|fxios/i.test(ua)) {
        browser = "Firefox";
        browserVersion = ua.match(/(?:firefox|fxios)\/([0-9.]+)/i)?.[1] || "unknown";
    }

    let os = "unknown";
    let osVersion = "unknown";
    if (/iphone|ipad|ipod/i.test(ua)) {
        os = "iOS";
        osVersion = ua.match(/os\s+([0-9_]+)/i)?.[1]?.replace(/_/g, ".") || "unknown";
    } else if (/android/i.test(ua)) {
        os = "Android";
        osVersion = ua.match(/android\s+([0-9.]+)/i)?.[1] || "unknown";
    } else if (/windows/i.test(ua)) {
        os = "Windows";
    } else if (/mac os x/i.test(ua)) {
        os = "macOS";
    }

    return { browser, browserVersion, os, osVersion, deviceType };
}

async function getServiceWorkerStatus() {
    if (typeof navigator === "undefined" || !navigator.serviceWorker) {
        return { supported: false };
    }
    try {
        const registrations = await navigator.serviceWorker.getRegistrations();
        const reg = registrations[0];
        return {
            supported: true,
            registered: registrations.length > 0,
            controllerScriptURL: navigator.serviceWorker.controller?.scriptURL || null,
            waiting: !!reg?.waiting,
            installing: !!reg?.installing,
            scope: reg?.scope || null,
        };
    } catch {
        return { supported: true, registered: false, error: "getRegistrations_failed" };
    }
}

async function getCacheNames() {
    if (typeof caches === "undefined") return null;
    try {
        // Names only — never contents (a cache's entries are full URLs
        // that could include tokens in query strings; names are just the
        // versioned bucket label, e.g. "talentgram-pwa-v4").
        return await caches.keys();
    } catch {
        return null;
    }
}

function getPageTiming() {
    if (typeof performance === "undefined" || typeof performance.getEntriesByType !== "function") return null;
    const [nav] = performance.getEntriesByType("navigation");
    if (!nav) return null;
    return {
        pageLoadMs: Math.round(nav.loadEventEnd - nav.startTime) || null,
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd - nav.startTime) || null,
        // "Hydration duration" isn't directly exposed by the Navigation
        // Timing API — domInteractive is the closest standard proxy for
        // "when the DOM was ready for React to attach to."
        domInteractiveMs: Math.round(nav.domInteractive - nav.startTime) || null,
    };
}

// `requestManager` is optional — pass the `_requestManager` escape hatch
// already exposed by axiosShim.js (e.g. `api._requestManager`). `slug` is
// an opaque route identifier only — never pass email/name/tokens in `extra`.
export async function buildDiagnosticsSnapshot({ requestManager, slug, extra } = {}) {
    const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
    const { browser, browserVersion, os, osVersion, deviceType } = parseUserAgent(ua);

    return {
        timestamp: new Date().toISOString(),
        appVersion: {
            commitSha: process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA || null,
        },
        route: {
            slug: slug || null,
            pathname: typeof window !== "undefined" ? window.location.pathname : null,
        },
        browser: { name: browser, version: browserVersion, os, osVersion, deviceType },
        viewport:
            typeof window !== "undefined"
                ? { width: window.innerWidth, height: window.innerHeight, devicePixelRatio: window.devicePixelRatio || 1 }
                : null,
        network: {
            online: typeof navigator !== "undefined" ? navigator.onLine : null,
        },
        serviceWorker: await getServiceWorkerStatus(),
        cacheNames: await getCacheNames(),
        pageTiming: getPageTiming(),
        requestHistory: requestManager && typeof requestManager.getHistory === "function" ? requestManager.getHistory() : [],
        ...(extra && typeof extra === "object" ? { extra } : {}),
    };
}
