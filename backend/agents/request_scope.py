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
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import contextvars

_cache: "contextvars.ContextVar[Optional[Dict[Any, Any]]]" = contextvars.ContextVar(
    "_agents_request_cache", default=None
)
_timings: "contextvars.ContextVar[Optional[List[Tuple[str, float]]]]" = contextvars.ContextVar(
    "_agents_request_timings", default=None
)


def reset() -> None:
    """Starts a fresh per-turn cache/timing scope. Call once, at the very
    top of handle_inbound_message, before anything else runs."""
    _cache.set({})
    _timings.set([])


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


def get_timings() -> Dict[str, float]:
    """Aggregated {stage_name: total_ms}, rounded to whole ms — the shape
    dispatcher.py logs alongside the overall dispatch_ms total."""
    totals: Dict[str, float] = {}
    for name, ms in (_timings.get() or []):
        totals[name] = totals.get(name, 0.0) + ms
    return {name: round(ms, 1) for name, ms in totals.items()}


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
