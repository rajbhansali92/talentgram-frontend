"""Per-dispatch-turn ephemeral state — timing breakdown + a small memo
cache for reads that are safe to reuse WITHIN one turn (never across
turns/requests, so staleness is never a concern) — e.g. a session doc
fetched once by an early hook (parse_edits_async) and re-fetched moments
later by build_confirmation in the SAME turn, or _fetch_ongoing_projects
called twice by two different resolution branches in one request.

contextvars, not a plain module global: FastAPI/Motor run each inbound
request on its own asyncio task, so a contextvar-backed scope can never
leak between concurrent requests the way a module-level dict could.

dispatcher.handle_inbound_message calls reset() once, at the very top of
every turn — nothing here is ever valid across two different turns.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import contextvars

_cache: "contextvars.ContextVar[Optional[Dict[Any, Any]]]" = contextvars.ContextVar(
    "_agents_request_cache", default=None
)
_timings: "contextvars.ContextVar[Optional[List[Tuple[str, float]]]]" = contextvars.ContextVar(
    "_agents_request_timings", default=None
)
# Fine-grained per-operation log (2026-08-05 latency sprint) — each entry:
# {"name", "collection", "elapsed_ms", "cache"} where cache is "hit"/"miss"/
# None (None = not a cacheable op at all, e.g. a pure write). Separate from
# `_timings` (which only aggregates by coarse stage bucket) so the coarse
# ASCII table keeps working unchanged while also giving a per-operation
# Mongo Summary (count/total-ms/avg-RTT per collection) and a detailed
# operation trace for deep-dive investigation.
_ops: "contextvars.ContextVar[Optional[List[Dict[str, Any]]]]" = contextvars.ContextVar(
    "_agents_request_ops", default=None
)
_request_id: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "_agents_request_id", default=None
)


def reset() -> None:
    """Starts a fresh per-turn cache/timing scope. Call once, at the very
    top of handle_inbound_message, before anything else runs."""
    _cache.set({})
    _timings.set([])
    _ops.set([])
    _request_id.set(uuid.uuid4().hex[:12])


def get_request_id() -> str:
    """Short id identifying this one dispatch turn — correlates the coarse
    dispatch_timing/dispatch_breakdown log lines with the fine-grained op
    trace and Mongo summary below. Falls back to "no-request-scope" if
    called outside a reset() turn (e.g. a unit test), so callers never need
    a None-check."""
    return _request_id.get() or "no-request-scope"


def cache_get(key: Any) -> Tuple[bool, Any]:
    """Returns (found, value). `found=False` is distinct from a cached
    value that happens to BE None/empty (e.g. "no session exists yet for
    this phone") — a plain `.get(key)` can't tell those apart."""
    cache = _cache.get()
    if cache is None or key not in cache:
        return False, None
    return True, cache[key]


def cache_set(key: Any, value: Any) -> None:
    cache = _cache.get()
    if cache is not None:
        cache[key] = value


def cache_pop(key: Any) -> None:
    cache = _cache.get()
    if cache is not None:
        cache.pop(key, None)


@contextmanager
def stage(name: str):
    """Records how long the wrapped block took under `name` — several
    calls with the same name accumulate (e.g. two "mongo" reads in one
    turn sum into one "mongo" total) rather than overwriting each other,
    so the breakdown reflects total time per category, not just the last
    call."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000
        lst = _timings.get()
        if lst is not None:
            lst.append((name, elapsed_ms))


def record(name: str, stage_bucket: Optional[str] = None, *, elapsed_ms: float = 0.0,
           collection: Optional[str] = None, cache: Optional[str] = None) -> None:
    """Non-context-manager sibling of `op()` — records an ALREADY-MEASURED
    elapsed time (e.g. a sub-stage timing dict computed by a pure,
    request_scope-free function like nlu.resolve_against_candidates and
    handed back to its caller) instead of timing a live block. `stage_bucket`
    is optional here since some fine-grained sub-stages (e.g. nlu.py's
    "candidate_count") aren't real elapsed-time spans at all — passing
    None records the op-log entry only, without touching the coarse
    dict `stage()`/`op()` feed."""
    if stage_bucket:
        lst = _timings.get()
        if lst is not None:
            lst.append((stage_bucket, elapsed_ms))
    ops = _ops.get()
    if ops is not None:
        ops.append({
            "name": name, "collection": collection,
            "elapsed_ms": round(elapsed_ms, 3), "cache": cache,
        })


def get_timings() -> Dict[str, float]:
    """Aggregated {stage_name: total_ms}, rounded to whole ms — the shape
    dispatcher.py logs alongside the overall dispatch_ms total."""
    totals: Dict[str, float] = {}
    for name, ms in (_timings.get() or []):
        totals[name] = totals.get(name, 0.0) + ms
    return {name: round(ms, 1) for name, ms in totals.items()}


@contextmanager
def op(name: str, stage_bucket: str, *, collection: Optional[str] = None, cache: Optional[str] = None):
    """Fine-grained sibling of `stage()` — records the SAME elapsed time
    into the coarse `stage_bucket` (so the existing ASCII table/dict keep
    working exactly as before) AND appends one entry to the per-request op
    log: {name, collection, elapsed_ms, cache}. `cache` is "hit" (served
    from request_scope's cache, effectively free), "miss" (a real Mongo
    round trip happened), or None (not a cacheable read at all, e.g. a
    write). A "hit" still gets timed (the cache_get call itself is ~free,
    but recording it proves it really was a hit, not silently skipped)."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000
        lst = _timings.get()
        if lst is not None:
            lst.append((stage_bucket, elapsed_ms))
        ops = _ops.get()
        if ops is not None:
            ops.append({
                "name": name, "collection": collection,
                "elapsed_ms": round(elapsed_ms, 2), "cache": cache,
            })


def get_ops() -> List[Dict[str, Any]]:
    """The raw fine-grained operation trace for this turn, in call order."""
    return list(_ops.get() or [])


def format_op_trace() -> str:
    """One line per fine-grained operation, in call order — the literal
    "for every step record: elapsed time / Mongo collection / cache
    status / request id" breakdown."""
    rid = get_request_id()
    lines = []
    for entry in get_ops():
        cache_label = entry["cache"] or "n/a"
        coll_label = entry["collection"] or "-"
        lines.append(
            f"[{rid}] {entry['name']:<28} collection={coll_label:<20} "
            f"cache={cache_label:<4} elapsed={entry['elapsed_ms']:.1f}ms"
        )
    return "\n".join(lines)


def get_mongo_summary() -> Dict[str, Any]:
    """Aggregates the op trace into {collection: {"queries": N, "total_ms":
    X}} for every op that was an actual Mongo round trip (cache != "hit"
    AND collection is set — a cache hit did no Mongo work at all, so it's
    correctly excluded from the query count), plus overall totals/average
    RTT across every real round trip this turn."""
    by_collection: Dict[str, Dict[str, float]] = {}
    total_queries = 0
    total_ms = 0.0
    for entry in get_ops():
        if not entry.get("collection") or entry.get("cache") == "hit":
            continue
        coll = entry["collection"]
        bucket = by_collection.setdefault(coll, {"queries": 0, "total_ms": 0.0})
        bucket["queries"] += 1
        bucket["total_ms"] += entry["elapsed_ms"]
        total_queries += 1
        total_ms += entry["elapsed_ms"]
    return {
        "by_collection": by_collection,
        "total_queries": total_queries,
        "total_ms": round(total_ms, 1),
        "avg_rtt_ms": round(total_ms / total_queries, 1) if total_queries else 0.0,
    }


def format_mongo_summary() -> str:
    summary = get_mongo_summary()
    lines = ["Mongo Summary", ""]
    for coll, stats in summary["by_collection"].items():
        lines.append(f"{coll} ........... {stats['queries']} queries ({stats['total_ms']:.0f} ms)")
    lines.append("")
    lines.append(f"TOTAL queries ........... {summary['total_queries']}")
    lines.append(f"TOTAL Mongo time ........ {summary['total_ms']:.0f} ms")
    lines.append(f"Average RTT .............. {summary['avg_rtt_ms']:.1f} ms")
    return "\n".join(lines)


# Display order + labels for the human-readable summary table — deliberately
# NOT every stage name that might ever appear (e.g. a future domain module's
# custom stage still shows up via the dict, just without a friendly label/
# fixed position), just the ones this investigation cares about seeing in a
# stable, readable order every time.
_STAGE_DISPLAY = [
    ("auth", "Authentication"),
    ("conversation_state", "Conversation State"),
    ("nlu", "NLU"),
    ("project_lookup", "Project Lookup"),
    ("talent_lookup", "Talent Lookup"),
    ("mongo_query", "Mongo Query"),
    ("aggregation", "Aggregation"),
    ("fuzzy", "Fuzzy Matching"),
    ("business_logic", "Business Logic"),
    ("db_write", "Database Write"),
    ("response_formatting", "Response Formatting"),
]


def format_stage_table(stages: Dict[str, float], total_ms: float) -> str:
    """Renders the per-turn breakdown as an aligned ASCII table:

        Authentication ............... 3 ms
        Mongo Query ................... 8 ms
        ...
        TOTAL ....................... 8553 ms

    `business_logic` is NOT a real named stage anywhere (wrapping a
    resolver's whole body in its own stage would nest inside — and double-
    count against — the DB/fuzzy stages it calls internally). It is instead
    computed here as the remainder: total dispatch time minus every stage
    that WAS explicitly timed, so the table always sums to `total_ms`
    without any span being counted twice. Any stage name not in
    `_STAGE_DISPLAY` (e.g. a future domain module's own custom stage) is
    still included, appended after the known rows, so nothing is silently
    dropped from the table even if this list falls out of date.
    """
    known_ms = sum(ms for name, ms in stages.items() if name != "business_logic")
    remainder = max(0.0, total_ms - known_ms)

    rows: List[tuple] = []
    seen = set()
    for key, label in _STAGE_DISPLAY:
        if key == "business_logic":
            rows.append((label, remainder))
        else:
            rows.append((label, stages.get(key, 0.0)))
        seen.add(key)
    for key, ms in stages.items():
        if key not in seen:
            rows.append((key, ms))

    label_width = max([len("TOTAL")] + [len(label) for label, _ in rows]) + 2
    lines = []
    for label, ms in rows:
        dots = "." * max(1, label_width - len(label) + 3)
        lines.append(f"{label} {dots} {ms:.0f} ms")
    dots = "." * max(1, label_width - len("TOTAL") + 3)
    lines.append(f"TOTAL {dots} {total_ms:.0f} ms")
    return "\n".join(lines)
