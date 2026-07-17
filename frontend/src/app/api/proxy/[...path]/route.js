import { NextResponse } from "next/server";
import { randomUUID } from "crypto";

// Generic same-origin proxy: browser -> this Route Handler -> Railway.
//
// INFRASTRUCTURE ONLY — nothing in the frontend points at /api/proxy/* yet.
// lib/api.js is untouched and every axios instance still calls Railway
// directly. This route exists so the proxy pattern can be built, deployed,
// and verified in complete isolation before any real traffic is migrated to
// it (see the Railway Proxy Migration Design — that migration is a later,
// separate phase).
//
// The one thing this file's existence changes today: BACKEND_INTERNAL_URL
// (server-side only, never NEXT_PUBLIC_-prefixed) is the only place that
// needs to know the Railway hostname going forward. Nothing here hardcodes
// it — if the env var is missing, the route fails closed with a 500 rather
// than silently falling back to a baked-in URL.

export const dynamic = "force-dynamic";

// Idle timeout, not a total-duration cap: re-armed on the initial connection
// AND on every chunk read during response streaming (see wrapUpstreamBody
// below). A slow-but-progressing large download is never punished; a
// connection that stops making progress — before headers, or mid-stream —
// is aborted after this many ms of silence. This is what makes the timeout
// cover the full request lifecycle rather than only time-to-first-byte.
const DEFAULT_UPSTREAM_IDLE_TIMEOUT_MS = 25_000;
const UPSTREAM_IDLE_TIMEOUT_MS =
    Number(process.env.PROXY_UPSTREAM_TIMEOUT_MS) || DEFAULT_UPSTREAM_IDLE_TIMEOUT_MS;

// Explicit allow-list, not a blanket copy — this proxy should never relay a
// Next.js/Vercel-internal header, or something we haven't reasoned about,
// into the upstream request.
const FORWARD_REQUEST_HEADERS = ["authorization", "cookie", "content-type", "accept", "user-agent"];

// Full RFC 7230 §6.1 hop-by-hop set, plus content-encoding/content-length
// (fetch() transparently decompresses the upstream body, so those values
// would be stale relative to what we actually stream back — Next.js
// recomputes the right ones for the new Response).
const STRIP_RESPONSE_HEADERS = new Set([
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
]);

// Vercel-specific headers, populated at their edge and not the same mutable
// header a client can freely set — preferred over the generic
// X-Forwarded-For, which (depending on whether Vercel overwrites or appends
// to a client-supplied value — not something this codebase can confirm
// without live traffic) could otherwise let a client inject an IP that
// Railway's own rate limiter (backend/routers/submissions.py,
// _diagnostics_rate_limit_ok) would trust. If neither Vercel-specific header
// is present, this falls back to the LAST entry of a raw X-Forwarded-For
// chain (the entry closest hop appended), never the first (closest to a
// possibly-spoofing client) — the safer of the two ambiguous readings when
// the exact chain semantics can't be confirmed from source alone.
function resolveClientIp(request) {
    const vercelForwardedFor = request.headers.get("x-vercel-forwarded-for");
    if (vercelForwardedFor) return vercelForwardedFor;
    const realIp = request.headers.get("x-real-ip");
    if (realIp) return realIp;
    const forwardedFor = request.headers.get("x-forwarded-for");
    if (forwardedFor) {
        const parts = forwardedFor.split(",").map((p) => p.trim()).filter(Boolean);
        if (parts.length) return parts[parts.length - 1];
    }
    return null;
}

// Rejects any decoded path segment that could cause the eventual URL parse
// to escape the intended /api/ prefix on the (fixed, server-controlled,
// never user-influenced) backend host.
function hasTraversalSegment(segments) {
    return (segments || []).some((seg) => seg === "." || seg === ".." || seg === "");
}

function log(level, fields) {
    const line = JSON.stringify({ timestamp: new Date().toISOString(), level, ...fields });
    if (level === "error" || level === "warn") {
        console.error(line);
    } else {
        console.log(line);
    }
}

// Classifies a failed upstream fetch into a stable reason + client-safe
// status code. Node's fetch (undici) wraps network failures as
// `TypeError: fetch failed` with the real errno-style code on `err.cause`.
function classifyUpstreamError(err) {
    if (err?.name === "AbortError") {
        return { status: 504, reason: "upstream_timeout" };
    }
    const code = err?.cause?.code || err?.code;
    if (code === "ENOTFOUND" || code === "EAI_AGAIN") {
        return { status: 502, reason: "dns_failure" };
    }
    if (code === "ECONNREFUSED") {
        return { status: 502, reason: "upstream_unavailable" };
    }
    if (code === "ECONNRESET" || code === "EPIPE") {
        return { status: 502, reason: "upstream_connection_reset" };
    }
    return { status: 502, reason: "upstream_unreachable" };
}

// Wraps the upstream response body so that (a) the idle timeout is re-armed
// on every chunk actually received — extending timeout coverage across the
// full streaming phase, not just until headers arrive — and (b) the TRUE
// outcome of the transfer (completed / errored mid-stream / cancelled by a
// disconnecting client) is what gets logged, not just the outcome at the
// moment fetch() resolved. Cancelling the returned stream (which Next.js
// does when the downstream client disconnects) cancels the upstream reader
// too, so an abandoned browser tab doesn't leave the Railway connection
// dangling.
function wrapUpstreamBody(upstreamBody, { onSettle, armIdleTimeout, clearIdleTimeout }) {
    if (!upstreamBody) {
        clearIdleTimeout();
        onSettle({ outcome: "empty_body" });
        return null;
    }
    const reader = upstreamBody.getReader();
    let bytes = 0;
    let settled = false;
    const settleOnce = (fields) => {
        if (settled) return;
        settled = true;
        clearIdleTimeout();
        onSettle({ bytes, ...fields });
    };
    return new ReadableStream({
        async pull(controller) {
            try {
                const { done, value } = await reader.read();
                if (done) {
                    controller.close();
                    settleOnce({ outcome: "completed" });
                    return;
                }
                bytes += value.byteLength;
                armIdleTimeout(); // progress -> push the deadline back out
                controller.enqueue(value);
            } catch (err) {
                controller.error(err);
                settleOnce({ outcome: "stream_error", error: err });
            }
        },
        cancel(reason) {
            // Downstream (browser) disconnected mid-stream — propagate the
            // cancellation upstream instead of leaving Railway's connection
            // open for a client that is no longer listening.
            reader.cancel(reason).catch(() => {});
            settleOnce({ outcome: "client_disconnected_mid_stream", reason: String(reason || "") });
        },
    });
}

async function handleProxy(request, context) {
    const startedAt = Date.now();
    const { path } = await context.params;
    const requestId = request.headers.get("x-request-id") || randomUUID();

    if (hasTraversalSegment(path)) {
        log("error", {
            request_id: requestId,
            method: request.method,
            path: `/${(path || []).join("/")}`,
            status: 400,
            failure_reason: "invalid_path_segment",
        });
        return NextResponse.json(
            { error: "Invalid path", request_id: requestId },
            { status: 400, headers: { "x-request-id": requestId } }
        );
    }
    const targetPath = (path || []).join("/");

    const backendInternalUrl = process.env.BACKEND_INTERNAL_URL;
    if (!backendInternalUrl) {
        log("error", {
            request_id: requestId,
            method: request.method,
            path: `/${targetPath}`,
            status: 500,
            failure_reason: "proxy_misconfigured_missing_backend_internal_url",
        });
        return NextResponse.json(
            { error: "Proxy misconfigured", request_id: requestId },
            { status: 500, headers: { "x-request-id": requestId } }
        );
    }

    const incomingUrl = new URL(request.url);
    const targetUrl = `${backendInternalUrl.replace(/\/+$/, "")}/api/${targetPath}${incomingUrl.search}`;

    const upstreamHeaders = new Headers();
    for (const name of FORWARD_REQUEST_HEADERS) {
        const value = request.headers.get(name);
        if (value) upstreamHeaders.set(name, value);
    }
    upstreamHeaders.set("x-request-id", requestId);
    const clientIp = resolveClientIp(request);
    if (clientIp) upstreamHeaders.set("x-forwarded-for", clientIp);
    upstreamHeaders.set("x-forwarded-proto", incomingUrl.protocol.replace(":", ""));
    upstreamHeaders.set("x-forwarded-host", request.headers.get("host") || incomingUrl.host);

    const hasBody = request.body !== null && !["GET", "HEAD"].includes(request.method);

    const controller = new AbortController();
    const onClientAbort = () => controller.abort();
    request.signal?.addEventListener("abort", onClientAbort);

    let idleTimeoutId = null;
    const armIdleTimeout = () => {
        if (idleTimeoutId) clearTimeout(idleTimeoutId);
        idleTimeoutId = setTimeout(() => controller.abort(), UPSTREAM_IDLE_TIMEOUT_MS);
    };
    const clearIdleTimeout = () => {
        if (idleTimeoutId) clearTimeout(idleTimeoutId);
        idleTimeoutId = null;
    };
    armIdleTimeout();

    const upstreamStartedAt = Date.now();
    let upstreamRes;
    try {
        upstreamRes = await fetch(targetUrl, {
            method: request.method,
            headers: upstreamHeaders,
            // Streamed straight through, never parsed/reserialized — this is
            // what keeps multipart/form-data (and any other body shape)
            // byte-for-byte intact regardless of content type.
            body: hasBody ? request.body : undefined,
            duplex: hasBody ? "half" : undefined,
            signal: controller.signal,
            // Relay redirects to the browser as-is rather than silently
            // following them server-side.
            redirect: "manual",
        });
    } catch (err) {
        clearIdleTimeout();
        request.signal?.removeEventListener("abort", onClientAbort);
        const durationMs = Date.now() - startedAt;
        const upstreamDurationMs = Date.now() - upstreamStartedAt;

        const clientAborted = request.signal?.aborted && err?.name === "AbortError";
        const { status, reason } = clientAborted
            ? { status: 499, reason: "client_aborted" }
            : classifyUpstreamError(err);

        log("error", {
            request_id: requestId,
            method: request.method,
            path: `/${targetPath}`,
            status,
            duration_ms: durationMs,
            upstream_duration_ms: upstreamDurationMs,
            upstream_host: backendInternalUrl,
            failure_reason: reason,
        });

        if (clientAborted) {
            // Browser already disconnected — no one is listening for this
            // response, but the handler still returns one so the Vercel
            // function invocation exits cleanly instead of erroring.
            return new NextResponse(null, { status });
        }

        return NextResponse.json(
            { error: "Upstream request failed", reason, request_id: requestId },
            { status, headers: { "x-request-id": requestId } }
        );
    }

    // Headers arrived — that's progress too, so the body-streaming phase
    // starts with a fresh full idle window rather than inheriting whatever
    // was left over from the connection phase.
    request.signal?.removeEventListener("abort", onClientAbort);
    armIdleTimeout();
    const upstreamDurationMs = Date.now() - upstreamStartedAt;

    const responseHeaders = new Headers();
    for (const [key, value] of upstreamRes.headers.entries()) {
        if (!STRIP_RESPONSE_HEADERS.has(key.toLowerCase())) {
            responseHeaders.append(key, value);
        }
    }
    // Headers.entries() can collapse multiple Set-Cookie lines into one —
    // getSetCookie() (Node 18.14+/Undici) preserves each cookie separately.
    if (typeof upstreamRes.headers.getSetCookie === "function") {
        responseHeaders.delete("set-cookie");
        for (const cookie of upstreamRes.headers.getSetCookie()) {
            responseHeaders.append("set-cookie", cookie);
        }
    }
    responseHeaders.set("x-request-id", requestId);

    // The access-log line now reflects the TRUE end of the transfer — logged
    // once the stream actually finishes, errors, or is cancelled by a
    // disconnecting client — not just the moment headers arrived.
    const wrappedBody = wrapUpstreamBody(upstreamRes.body, {
        armIdleTimeout,
        clearIdleTimeout,
        onSettle: ({ outcome, bytes, error, reason }) => {
            const durationMs = Date.now() - startedAt;
            const failed = outcome === "stream_error";
            const level = failed ? "error" : outcome === "client_disconnected_mid_stream" ? "warn" : "info";
            log(level, {
                request_id: requestId,
                method: request.method,
                path: `/${targetPath}`,
                status: upstreamRes.status,
                duration_ms: durationMs,
                upstream_duration_ms: upstreamDurationMs,
                upstream_host: backendInternalUrl,
                response_bytes: bytes,
                stream_outcome: outcome,
                ...(failed ? { failure_reason: "upstream_stream_error", error_message: String(error?.message || error) } : {}),
                ...(reason ? { client_disconnect_reason: reason } : {}),
            });
        },
    });

    // Streamed straight through — never buffered into memory — so large
    // downloads (media, ZIP bundles) don't get fully materialized in the
    // function before the browser sees a single byte.
    return new NextResponse(wrappedBody, {
        status: upstreamRes.status,
        statusText: upstreamRes.statusText,
        headers: responseHeaders,
    });
}

export async function GET(request, context) {
    return handleProxy(request, context);
}
export async function POST(request, context) {
    return handleProxy(request, context);
}
export async function PUT(request, context) {
    return handleProxy(request, context);
}
export async function PATCH(request, context) {
    return handleProxy(request, context);
}
export async function DELETE(request, context) {
    return handleProxy(request, context);
}
