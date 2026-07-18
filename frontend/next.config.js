/** @type {import('next').NextConfig} */

const securityHeaders = [
  {
    key: "X-DNS-Prefetch-Control",
    value: "on",
  },
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains",
  },
  {
    key: "X-Frame-Options",
    value: "DENY",
  },
  {
    key: "X-Content-Type-Options",
    value: "nosniff",
  },
  // NOTE: X-XSS-Protection intentionally removed — it is deprecated and the
  // legacy auditor heuristic can itself introduce XSS on some browsers. CSP
  // below is the real XSS control.
  {
    key: "Referrer-Policy",
    value: "strict-origin-when-cross-origin",
  },
  {
    key: "Permissions-Policy",
    value: "geolocation=(), microphone=(), camera=()",
  },
  {
    // CSP: allows Next.js inline scripts/styles + Cloudinary media + Google OAuth.
    // P2-9: 'unsafe-eval' removed from script-src — modern Next.js App Router
    // production bundles do not require eval(). 'unsafe-inline' is retained
    // because Next injects inline bootstrap scripts; migrating to per-request
    // nonces is the documented follow-up. Verify with `next build && next start`
    // before promoting (if a dependency needs eval the console will report it).
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob: https://accounts.google.com https://apis.google.com",
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
      "font-src 'self' https://fonts.gstatic.com data:",
      // Cloudflare migration: R2 serves raw video posters/previews while a clip
      // is still transcoding (presigned GET on *.r2.cloudflarestorage.com), and
      // Cloudflare Stream serves the final HLS playlist + poster thumbnails on
      // *.cloudflarestream.com. Both must be allow-listed for img-src (posters)
      // and media-src (<video> playback) or the browser blocks them with a CSP
      // violation — see the R2/Stream playback regression.
      "img-src 'self' data: blob: https://res.cloudinary.com https://lh3.googleusercontent.com https://*.talentgramagency.com https://*.r2.cloudflarestorage.com https://*.cloudflarestream.com",
      "media-src 'self' blob: https://res.cloudinary.com https://*.r2.cloudflarestorage.com https://*.cloudflarestream.com",
      // nominatim.openstreetmap.org powers the city/location autocomplete in
      // LocationSelector (apply, submit, talent edit). It is a fetch() XHR, so
      // it must be allow-listed in connect-src or the browser blocks it with a
      // CSP error. Scoped to the exact host — no wildcard, no other directive
      // weakened.
      // api.cloudinary.com is the direct browser→Cloudinary signed-upload host
      // (image /upload/sign path + chunked audition-video transport). It is an
      // XHR POST, so without it in connect-src the browser blocks every upload
      // even after the backend returns a valid signature.
      // *.cloudflarestream.com is required in connect-src because hls.js fetches
      // the HLS manifest (video.m3u8) and media segments over XHR/fetch when the
      // browser lacks native HLS (Chrome/Firefox). Without it, Stream playback
      // fails even though media-src allows the <video> element.
      // Local dev only: adminApi/viewerApi/portalApi (src/lib/api.js) call
      // NEXT_PUBLIC_BACKEND_URL directly (not through the same-origin
      // /api/proxy/* route), so a local backend origin must be allow-listed
      // here or the browser silently blocks the request with no network
      // entry at all — confirmed live: admin login POSTs to
      // http://localhost:8000/api/auth/login failed with no response despite
      // the backend and CORS both being correctly configured. Production's
      // backend already lives on the allow-listed *.railway.app origin, so
      // this never widens the production policy.
      `connect-src 'self' blob: https://*.railway.app https://talentgram-app-production.up.railway.app https://oauth2.googleapis.com https://accounts.google.com https://api.resend.com https://nominatim.openstreetmap.org https://api.cloudinary.com https://*.r2.cloudflarestorage.com https://*.cloudflarestream.com${process.env.NODE_ENV !== "production" ? " http://localhost:8000" : ""}`,
      "worker-src 'self' blob:",
      "frame-src https://accounts.google.com",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
    ].join("; "),
  },
];

const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_BACKEND_URL: process.env.NEXT_PUBLIC_BACKEND_URL || process.env.REACT_APP_BACKEND_URL || "",
    REACT_APP_BACKEND_URL: process.env.NEXT_PUBLIC_BACKEND_URL || process.env.REACT_APP_BACKEND_URL || "",
    REACT_APP_GOOGLE_CLIENT_ID: process.env.REACT_APP_GOOGLE_CLIENT_ID || process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "",
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: securityHeaders,
      },
      {
        // Versioned FFmpeg WASM/JS assets only (lib/videoCompress.js's
        // FFMPEG_CORE_ASSET_PATH). Vercel's default for public/ static
        // assets is `max-age=0, must-revalidate`, which forces a network
        // round-trip before every load of the 32MB ffmpeg-core.wasm — a
        // real latency/reliability cost on mobile, since `must-revalidate`
        // also means a failed revalidation can't fall back to the stale
        // cached copy. Immutable long-lived caching is safe here ONLY
        // because the version segment (v1, v2, ...) changes on every future
        // @ffmpeg/core upgrade instead of overwriting files in place — a
        // future upgrade MUST add a new /ffmpeg/v2/ directory (and bump
        // FFMPEG_CORE_ASSET_PATH), never replace the contents of /ffmpeg/v1/.
        // Scoped to this exact path — no other public asset's caching changes.
        source: "/ffmpeg/v1/:path*",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=31536000, immutable",
          },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
