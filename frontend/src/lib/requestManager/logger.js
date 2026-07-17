// Structured JSON-line logger, deliberately matching the shape emitted by
// the Phase 1 proxy route's own log() (frontend/src/app/api/proxy/[...path]/route.js)
// so frontend request-manager logs and proxy access logs are grep-compatible
// once both are ever viewed side by side.

function defaultSink(level, line) {
    if (level === "error" || level === "warn") {
        // eslint-disable-next-line no-console
        console.error(line);
    } else {
        // eslint-disable-next-line no-console
        console.log(line);
    }
}

export function createLogger(onLog) {
    const sink = onLog || defaultSink;
    return function log(level, fields) {
        const line = JSON.stringify({ timestamp: new Date().toISOString(), level, ...fields });
        sink(level, line, fields);
    };
}
