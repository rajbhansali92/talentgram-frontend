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
      "script-src 'self' 'unsafe-inline' https://accounts.google.com https://apis.google.com",
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
      "font-src 'self' https://fonts.gstatic.com data:",
      "img-src 'self' data: blob: https://res.cloudinary.com https://lh3.googleusercontent.com https://*.talentgramagency.com",
      "media-src 'self' blob: https://res.cloudinary.com",
      // nominatim.openstreetmap.org powers the city/location autocomplete in
      // LocationSelector (apply, submit, talent edit). It is a fetch() XHR, so
      // it must be allow-listed in connect-src or the browser blocks it with a
      // CSP error. Scoped to the exact host — no wildcard, no other directive
      // weakened.
      "connect-src 'self' https://*.railway.app https://talentgram-app-production.up.railway.app https://oauth2.googleapis.com https://accounts.google.com https://api.resend.com https://nominatim.openstreetmap.org",
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
    REACT_APP_BACKEND_URL: process.env.REACT_APP_BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || "",
    REACT_APP_GOOGLE_CLIENT_ID: process.env.REACT_APP_GOOGLE_CLIENT_ID || process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "",
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: securityHeaders,
      },
    ];
  },
};

module.exports = nextConfig;
