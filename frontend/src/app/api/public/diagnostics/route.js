import { NextResponse } from "next/server";

// Same-origin receiver for SubmissionPage's diagnostics beacon
// (fetch("/api/public/diagnostics")). Forwards the payload verbatim to the
// existing backend endpoint (POST /api/public/diagnostics in
// backend/routers/submissions.py) so it survives even when the client's
// network can't reach the Railway domain directly. No payload/schema change.
export async function POST(request) {
    const backendUrl =
        process.env.NEXT_PUBLIC_BACKEND_URL ||
        process.env.REACT_APP_BACKEND_URL ||
        "https://talentgram-app-production.up.railway.app";

    const body = await request.text();
    // Preserve the real client IP so the backend's existing per-IP rate
    // limiter (_diagnostics_rate_limit_ok) keys on the end user, not Vercel.
    const forwardedFor =
        request.headers.get("x-forwarded-for") ||
        request.headers.get("x-real-ip") ||
        "";

    try {
        const backendRes = await fetch(`${backendUrl}/api/public/diagnostics`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                ...(forwardedFor ? { "x-forwarded-for": forwardedFor } : {}),
            },
            body,
        });
        const data = await backendRes.json().catch(() => ({}));
        return NextResponse.json(data, { status: backendRes.status });
    } catch (_) {
        // Diagnostics must never surface an error to the caller — the beacon
        // already does .catch(() => {}) on its end, this mirrors that.
        return NextResponse.json({ ok: false }, { status: 502 });
    }
}
