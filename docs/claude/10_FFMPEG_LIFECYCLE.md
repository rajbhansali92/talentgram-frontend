# FFmpeg Lifecycle

This document describes the client-side video compression pipeline built
around `ffmpeg.wasm` — its concurrency model, the shared instance's lifecycle,
asset caching, and telemetry. It's the record of an incremental hardening
effort (audit → design → implement, repeated) that started from a single
question ("should the upload semaphore also gate compression?") and ended up
touching correctness, memory, caching, and observability. See
[08_DECISION_LOG.md](08_DECISION_LOG.md) and
[09_PRESENTATION_MODELS.md](09_PRESENTATION_MODELS.md) for the surrounding
architecture this pipeline feeds into (the Operational Engine consumes its
output; nothing about business rules or validation lives here).

**Source of truth**: `frontend/src/lib/videoCompress.js` (compression +
singleton + recycling + telemetry) and
`frontend/src/context/UploadManagerContext.jsx` (concurrency gates + upload
transport). Treat this document as a map of that code, not a replacement for
reading it.

## Architecture

```
Compression Pipeline (lib/videoCompress.js)
        |
        v
Compression Gate (concurrency = 1)
        |
        v
FFmpeg Singleton (one Web Worker, one virtual filesystem)
        |
        v
Upload Gate (concurrency = 2)
        |
        v
Transport (Cloudinary signed upload / chunked R2 upload)
```

Both gates live in `UploadManagerContext.jsx` (`compressionGate`,
`uploadGate` — plain instances of a small `createConcurrencyGate(maxConcurrent)`
FIFO semaphore, no priority, no cancellation). They are **independent pools**:
a video waiting on the compression gate never occupies an upload-transport
slot, and vice versa. This matters for responsiveness — an image upload is
never stuck behind an unrelated video's compression.

## Why compression is capped at concurrency 1

`lib/videoCompress.js` holds a single module-level FFmpeg instance
(`_ffmpeg`) for the entire page session — one dedicated Web Worker, one
virtual filesystem, one un-scoped progress/log listener array with no
per-job attribution in the library itself. Two compression jobs in flight at
once can interleave their `writeFile`/`exec`/`readFile` sequence on that
shared filesystem and cross-contaminate each other's progress events — a
real data-corruption risk, not just a performance concern. `MAX_CONCURRENT_COMPRESSIONS
= 1` in `UploadManagerContext.jsx` makes that interleaving structurally
impossible: only one compression job can ever be between `writeFile` and
cleanup at a time.

The upload-transport gate (`MAX_CONCURRENT_UPLOADS = 2`) is unrelated to this
constraint — network requests don't share mutable state the way the FFmpeg
singleton does, so 2 concurrent transports has always been safe.

## Unique virtual filenames (defense-in-depth)

Every `compressVideo()` call generates its own job id
(`generateCompressionJobId()`, via `crypto.randomUUID()` with a fallback for
older WebViews) and uses `compress-<uuid>-input.<ext>` /
`compress-<uuid>-output.mp4` instead of hardcoded `in.mp4`/`out.mp4`. This is
**not** the primary safety mechanism — the concurrency-1 gate above is what
actually guarantees only one job touches the shared instance at a time. Unique
filenames are cheap insurance against a hypothetical future code path that
calls `compressVideoIfNeeded()` without going through the gate.

## FFmpeg singleton lifecycle

`getFFmpeg()` is the sole entry point that creates/loads the singleton:

- First call in a session: `new FFmpeg()`, fetch `ffmpeg-core.js`/`ffmpeg-core.wasm`
  via `toBlobURL`, `ffmpeg.load(...)`, store in `_ffmpeg`.
- Every subsequent call while `_ffmpeg` is set: returns the same instance
  immediately, no reload.
- Concurrent callers during the *first* load share one `_loadingPromise` so
  the WASM module is never fetched/instantiated twice.

**Teardown** always goes through one shared helper, `terminateSharedFFmpeg(instance)`:
attempt `instance.terminate()` (best-effort, errors are caught and logged,
never rethrown), then unconditionally null out `_ffmpeg` and `_loadingPromise`.
Because the reset happens regardless of whether `terminate()` succeeded, the
singleton can never be left in a partially-torn-down state — the next
`getFFmpeg()` call always sees a clean slate and reloads through the exact
same path as a first-ever load. Two triggers currently call this helper:

1. **Compression timeout** (existing, pre-dates idle recycling): if a
   transcode exceeds its device-tier timeout (3 min mobile / 5 min desktop),
   the worker is terminated and the error propagates so the caller falls back
   to uploading the original file.
2. **Idle-timeout recycling** (see below).

## Idle-timeout recycling

**Why idle-time, not a compression counter**: a submission caps at 1 intro
video + up to `MAX_TAKES = 5` takes (Application flow allows only 1 intro
video) — a session realistically never compresses more than a handful of
videos, so a count-based trigger would rarely fire, and when it did, it would
force a reload mid-flow, the worst possible moment. The actual mobile-memory
risk is a long *idle* dwell time with the instance sitting loaded at whatever
peak memory it last reached — that's what this targets.

**Mechanism** (`lib/videoCompress.js`):

- `FFMPEG_IDLE_RECYCLE_MS = 5 * 60 * 1000` (5 minutes) — the one configurable
  constant governing this; not a magic number buried in logic.
- `cancelIdleRecycle()` — clears any pending recycle timer. Called from
  `UploadManagerContext.jsx` the instant `compressionGate.acquire()` resolves
  (a new compression job is starting).
- `scheduleIdleRecycle()` — (re)starts the timer, no-ops if nothing is
  currently loaded. Called from `UploadManagerContext.jsx` in the `finally`
  block right after `compressionGate.release()` (a compression job just
  ended).

Because the compression gate is single-concurrency, "the gate is free" and
"nothing is compressing or queued" are the same fact — recycling can
structurally never interrupt an active job or preempt a queued one. `videoCompress.js`
owns no knowledge of the gate itself; `UploadManagerContext.jsx` only ever
notifies it of the two transition points. No new instances, worker pools, or
`@ffmpeg/core-mt` were introduced — this reuses the exact `terminate()` /
`_ffmpeg = null` / `_loadingPromise = null` path described above.

## Telemetry events

`emitFFmpegTelemetry(event, data)` dispatches a plain DOM `CustomEvent`
(`"tg:ffmpeg-telemetry"`, `detail: { event, ...data, timestamp }`) — no SDK,
no new backend endpoint, wrapped so a dispatch failure can never throw into
or block an upload. This codebase has no generic analytics infrastructure
today (checked at the `package.json` and root-layout level); the only
existing "track" mechanism is the Client Review Link's slug-scoped
`POST /public/links/{slug}/track`, a different bounded context this pipeline
has no access to. Any future analytics integration can subscribe with
`window.addEventListener("tg:ffmpeg-telemetry", handler)`.

| Event | Fired from | Payload |
|---|---|---|
| `ffmpeg_initialized` | `getFFmpeg()`, after a successful load | `{ reinitialized }` — `false` on the session's first load, `true` on any load after a recycle |
| `ffmpeg_recycled` | `scheduleIdleRecycle()`'s timer callback, after `terminateSharedFFmpeg()` | `{ idleMs }` |
| `ffmpeg_timeout` | `compressVideo()`'s existing timeout-recovery branch | `{ deviceType, fileSizeMb, timeoutMs }` |
| `compression_fallback` | `UploadManagerContext.jsx`'s compression `catch` block | `{ reason }` — the error's `code`, or `"unknown"` |

These are purely observational — no event handler exists in the app today
that changes behavior based on them.

## Asset versioning (`/ffmpeg/v1`)

`ffmpeg-core.js` and `ffmpeg-core.wasm` (32MB) live under
`frontend/public/ffmpeg/v1/`, referenced via `FFMPEG_CORE_ASSET_PATH = "/ffmpeg/v1"`
in `videoCompress.js`. `next.config.js`'s `headers()` has a rule scoped to
`source: "/ffmpeg/v1/:path*"` setting:

```
Cache-Control: public, max-age=31536000, immutable
```

This is additive to (not a replacement for) the existing catch-all security
headers (`/(.*)` — CSP/HSTS/etc.); no other public asset's caching changed.

**Why immutable caching is safe here**: it's only safe because the version
segment in the URL changes whenever the file contents change. `ffmpeg-core.js`/
`ffmpeg-core.wasm` are not content-hashed filenames (unlike Next.js's own
`_next/static/chunks/*`, which get the same `immutable` treatment
automatically because their filenames already encode a content hash) — a
year-long browser cache of a fixed, non-hashed URL is only correct if nothing
ever changes what that URL serves. The versioned-directory convention is what
guarantees that.

**What was verified before choosing this** (live against production):
before this change, both files were served with Vercel's default
`Cache-Control: public, max-age=0, must-revalidate` and `x-vercel-cache: MISS`
— every load required a network round-trip before the browser could use its
cached bytes, and `must-revalidate` meant a failed revalidation (e.g. on a
flaky mobile connection) could not fall back to the stale cached copy at all.
Next.js's own hashed build chunks, by contrast, already got
`max-age=31536000, immutable` with `x-vercel-cache: HIT`. This gap is what the
versioned-path + explicit header rule closes.

## Upgrade procedure (bumping `@ffmpeg/core`)

A future `@ffmpeg/core` version upgrade must **never** overwrite the files at
`/ffmpeg/v1/`. Any browser holding last year's immutably-cached copy would
never see the update.

1. Update the `@ffmpeg/core` (and `@ffmpeg/ffmpeg`/`@ffmpeg/util` if needed)
   dependency in `package.json`.
2. Copy the new build's `ffmpeg-core.js` / `ffmpeg-core.wasm` into a **new**
   directory: `frontend/public/ffmpeg/v2/` (do not touch `v1/`).
3. Update `FFMPEG_CORE_ASSET_PATH` in `lib/videoCompress.js` from `/ffmpeg/v1`
   to `/ffmpeg/v2`.
4. Add a matching `next.config.js` `headers()` rule for
   `source: "/ffmpeg/v2/:path*"` with the same immutable `Cache-Control` —
   don't just widen the existing `v1` rule's `source` pattern, since `v1`
   should keep serving (harmlessly, unreferenced) for any client with a
   stale cached page still pointing at it.
5. Verify: `curl -I` the new `/ffmpeg/v2/*` URLs return `200` with the
   immutable header; the old `/ffmpeg/v1/*` URLs still resolve (they're
   simply no longer referenced by fresh page loads); a full compression run
   works end-to-end against the new core.
6. Only remove the `v1/` directory and its `next.config.js` rule after
   confidence that no meaningfully-cached old page/session is still relying
   on it (this is a cleanup step, not a release blocker).

## Future version bump procedure (general pattern)

The same five-step shape applies to any future asset change under
`/ffmpeg/*`, not just a core version bump:

- New directory (`vN+1`), never edit an existing version's files in place.
- Update the one constant (`FFMPEG_CORE_ASSET_PATH`) that points at it.
- Add a new scoped `headers()` rule for the new path; don't repurpose the old one.
- Verify the new path serves correctly with the immutable header before
  relying on it.
- Leave the old version directory in place until it's safe to assume nothing
  is still referencing it.

## Related documents

- [08_DECISION_LOG.md](08_DECISION_LOG.md) — architectural decisions and their rationale, including the audits that led to this pipeline's current shape.
- [09_PRESENTATION_MODELS.md](09_PRESENTATION_MODELS.md) — how `summarizeUploads()`/`OPERATIONAL_STATES` (consumed by this pipeline's status writes) fit into the Operational Engine / Upload Activity Model layering.
- [07_OPEN_ISSUES.md](07_OPEN_ISSUES.md) — open items, including the legacy `/ffmpeg/814.ffmpeg.js` and `/ffmpeg/ffmpeg.js` files (unreferenced by any app code, left untouched during the versioning work — candidates for removal in a future cleanup, not currently tracked as a numbered issue).
