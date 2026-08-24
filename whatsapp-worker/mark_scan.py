"""Media-Assignment Worker side (Phase 1, 2026-08-22) — bounded, on-demand
scan/download for the `@Gunwanti + mark` mechanism proven in the
"ticklish-cuddling-willow" Phase 0 spike.

Pure WhatsApp I/O layer, exactly like inbound.py's own stated design
principle: this module does not know what a "project" is, does not
validate identity against the configured Gunwanti LID, and does not decide
what counts as a valid assignment — it only (a) reports every reply in a
bounded window that has BOTH a real WhatsApp mention (any LID) and the
literal word "mark", resolved to its exact source media via the Phase 0
thumbnail-hash mechanism where possible, and (b) downloads exactly the
messages it's told to. All interpretation (identity check, project/role
parsing, ambiguity, idempotency) lives in the backend's
agents/modules/media_assignment.py + services/media_assignment_worker.py.

NOT continuous: this loop polls the backend for a scan/download REQUEST
(created only when an `upload` command is issued) — it never watches a
talent's group on its own initiative, and closes out (moves on to the next
poll) the instant one request is handled.

Request/response bridge: POST /api/agents/whatsapp/scan-requests/claim,
.../scan-result, .../download-result, and — for actual media bytes —
POST /api/agents/whatsapp/media-upload (multipart, one call per item).
Same X-Internal-Secret auth every other worker->backend call already uses.
"""
from __future__ import annotations

import asyncio
import base64 as b64mod
import hashlib
import logging
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config
import sender
import session as session_module

logger = logging.getLogger(__name__)

POLL_SEC = 3.0
MAX_MESSAGES_SCANNED_DEFAULT = 300
HTML_TRUNCATE = 60000

BASE = f"{config.AGENTS_BACKEND_URL}/api/agents/whatsapp"


def _auth_headers() -> dict:
    headers = {}
    if config.AGENTS_INBOUND_SECRET:
        headers["X-Internal-Secret"] = config.AGENTS_INBOUND_SECRET
    return headers


async def _evaluate(page, js: str, arg: Any = None, timeout: float = 10.0) -> Any:
    """page.evaluate() has NO built-in timeout in Playwright — unlike
    click()/etc. — so a single stalled call can hang indefinitely. Real
    evidence of this (2026-08-23): a claimed download_probe request sat in
    "processing" for 2h43m despite an outer asyncio.wait_for(timeout=150);
    the worker process itself stayed healthy and kept servicing other
    WhatsApp traffic throughout, so the hang was isolated to one stuck
    await inside that request's handling that resisted the outer
    cancellation. Every evaluate() call in this module should go through
    this wrapper so a stall becomes an ordinary catchable TimeoutError
    instead of an unbounded hang."""
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    return await asyncio.wait_for(coro, timeout=timeout)


# ---------------------------------------------------------------------------
# DOM extraction — same shape proven in the Phase 0 spike
# (spike_diagnostics.py), consolidated here as production code. Every
# message's own outerHTML (truncated) plus its quoted-message block (if
# any) outerHTML, truncated. All interpretation happens in Python below —
# never guessing selectors, matching this codebase's existing "read the
# real DOM, don't assume its shape" discipline.
# ---------------------------------------------------------------------------
_DOM_DUMP_JS = """
([sel, idx]) => {
  const els = document.querySelectorAll(sel);
  if (idx >= els.length) return null;
  const el = els[idx];
  const quoted = el.querySelector('[data-testid="quoted-message"]');
  return {
    messageHtml: el.outerHTML.slice(0, _TRUNC),
    quotedHtml: quoted ? quoted.outerHTML.slice(0, _TRUNC) : null,
  };
}
""".replace("_TRUNC", str(HTML_TRUNCATE))

_DATA_ID_RE = re.compile(r'data-id="([^"]+)"')
_LID_RE = re.compile(r'data-app-text-template="[^"]*?(\d+@lid)')
_MARK_RE = re.compile(r"\bmark\s+.+", re.IGNORECASE | re.DOTALL)
_B64_RE = re.compile(r"base64,([A-Za-z0-9+/=]{80,})")
_AUTHOR_RE = re.compile(r'data-testid="author"[^>]*>([^<]*)<')
_PRE_PLAIN_RE = re.compile(r'data-pre-plain-text="(\[[^\]]*\][^"]*):"')
_IMG_TAG_RE = re.compile(r"data-testid=\"(image-thumb|image-content)\"")
_VIDEO_TAG_RE = re.compile(r"data-testid=\"(video-thumb|video-content)\"")
_ALBUM_RE = re.compile(r'data-testid="media-album"')
_TILE_TYPE_RE = re.compile(r'data-testid="(video-content|image-content|image-thumb)"')
# Grouped-media albums (2026-08-23) — WhatsApp's native multi-select "send
# together" produces ONE message (one data-id) containing a media-album
# grid of N tiles, each with its own thumbnail but NO separate data-id.
# Proven (real captured data, see the grouped-media investigation): each
# tile's own CSS grid cell reliably starts with a `grid-area: r/c/r/c`
# style, cleanly delineating tile boundaries — splitting on that boundary
# and taking each chunk's smallest embedded blob (same rule _smallest_hash
# already uses per-message) gives each tile's own stable, byte-exact
# thumbnail hash. Position (chunk order) is used ONLY to locate/click a
# tile during download — never as its persisted identity.
_GRID_AREA_RE = re.compile(r"grid-area:\s*\d+\s*/\s*\d+\s*/\s*\d+\s*/\s*\d+")

# Whole-album batch marking (2026-08-23) — "mark google: take 1, take 2,
# take 3, intro" replying to the ALBUM ITSELF, not one tile. Proven via a
# real diagnostic reply ("mark google diagnostic") that a whole-album
# reply's quoted-message block carries NO thumbnail hash at all — just a
# generic icon and literal "N videos"/"N photos" summary text — so the
# existing hash-match resolution can never work for it. The colon requires
# an explicit, unambiguous separator between the project and the ordered
# role list — a plain "mark google take 1" (no colon) is left entirely to
# the existing single-tile-reply path, untouched.
_BATCH_COLON_RE = re.compile(r"^\s*mark\s+(.+?):\s*(.+)$", re.IGNORECASE | re.DOTALL)
_SUMMARY_QUOTE_RE = re.compile(r"\b(\d+)\s+(videos?|photos?|images?)\b", re.IGNORECASE)
# Worker-local, minimal check for "mark <project> photos" (no colon list) —
# NOT a full reimplementation of the backend's extract_role_and_project
# (which the worker can't import — separate service/deployment); this only
# needs to decide whether a whole-album reply's single role is "photos",
# so every tile in the jumped-to album gets expanded with that one role.
# The backend's own parser is still the authority on whether the resulting
# per-tile "mark <project> photos" text is valid.
_SINGLE_PHOTOS_RE = re.compile(
    r"^\s*mark\s+(.+?)\s+photos?\s*(?:\d{1,2}:\d{2}\s*[ap]m\b.*)?$", re.IGNORECASE | re.DOTALL,
)
# _mark_text() greedily captures everything after "mark" (DOTALL), which
# includes WhatsApp's own rendered timestamp text trailing the message
# body in the DOM — harmless for a single mark (the fuzzy project matcher
# already tolerates it), but for a comma-split batch list it lands
# entirely on the LAST item only (2026-08-23: "intro" arrived as
# "intro     1:57 pm         1:57 pm"), which would then become part of
# THAT tile's own synthesized project_fragment once "intro" is stripped
# out downstream. Stripped from every split item before use.
_TRAILING_TIMESTAMP_RE = re.compile(r"\s+\d{1,2}:\d{2}\s*[ap]m\b.*$", re.IGNORECASE | re.DOTALL)


def _parse_batch_role_list(mark_text: str) -> Optional[tuple]:
    """"mark google: take 1, take 2, take 3, intro" ->
    ("google", ["take 1", "take 2", "take 3", "intro"]). None if the text
    isn't the colon-delimited batch shape, or has fewer than 2 items (a
    single item after a colon isn't a batch)."""
    m = _BATCH_COLON_RE.match((mark_text or "").strip())
    if not m:
        return None
    project_fragment = m.group(1).strip()
    raw_items = m.group(2).split(",")
    items = []
    for i in raw_items:
        cleaned = _TRAILING_TIMESTAMP_RE.sub("", i).strip()
        if cleaned:
            items.append(cleaned)
    if len(items) < 2:
        return None
    return project_fragment, items


def _quoted_is_whole_item_summary(quoted_html: str) -> Optional[int]:
    """Returns the item count (e.g. 4 for "4 videos") if `quoted_html`
    looks like WhatsApp's collapsed multi-item quote summary — detected
    ONLY by the absence of any embedded thumbnail blob (a real single-tile
    quote always has one) combined with the literal "N videos/photos"
    text; returns None otherwise, including for an ordinary single-tile
    quote whose hash simply failed to match anything (that must remain a
    hard resolution failure, never silently reinterpreted as a batch)."""
    if _smallest_hash(quoted_html):
        return None
    m = _SUMMARY_QUOTE_RE.search(quoted_html or "")
    return int(m.group(1)) if m else None


def _smallest_hash(html: Optional[str]) -> Optional[str]:
    """Every image/video message embeds at least one base64 thumbnail
    directly in its rendered HTML (both as a standalone message and inside
    a reply's quoted-message block) — Phase 0 confirmed the SMALLEST one
    is what stays byte-identical between an original message and any
    reply quoting it; a second, larger blob (seen on videos) does not."""
    if not html:
        return None
    blobs = _B64_RE.findall(html)
    if not blobs:
        return None
    smallest = min(blobs, key=len)
    return hashlib.sha256(smallest.encode()).hexdigest()


def _own_data_id(html: str) -> Optional[str]:
    m = _DATA_ID_RE.search(html)
    return m.group(1) if m else None


def _is_album(html: str) -> bool:
    return bool(_ALBUM_RE.search(html or ""))


def _album_tile_hashes_and_types(html: str) -> List[tuple]:
    """One (hash, "video"|"image") pair per tile, in DOM/grid order (order
    is a locator only — see module note above). A chunk with no
    detectable hash is skipped entirely (never emits a partial/None
    entry), keeping this the single source of truth both
    _album_tile_hashes and Pass 1's per-tile media_type detection stay
    aligned with — a photo album's tiles carry data-testid="image-content"
    where a video album's carry "video-content"; previously Pass 1
    hardcoded every album tile as "video" regardless."""
    if not html:
        return []
    starts = [m.start() for m in _GRID_AREA_RE.finditer(html)]
    if not starts:
        return []
    starts.append(len(html))
    chunks = [html[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]
    result = []
    for chunk in chunks:
        h = _smallest_hash(chunk)
        if not h:
            continue
        tm = _TILE_TYPE_RE.search(chunk)
        media_type = "image" if (tm and tm.group(1) in ("image-content", "image-thumb")) else "video"
        result.append((h, media_type))
    return result


def _album_tile_hashes(html: str) -> List[str]:
    """One stable hash per tile, in DOM/grid order (order is a locator
    only — see module note above). Returns [] for a non-album message."""
    return [h for h, _ in _album_tile_hashes_and_types(html)]


def _media_type(html: str) -> Optional[str]:
    if _VIDEO_TAG_RE.search(html):
        return "video"
    if _IMG_TAG_RE.search(html):
        return "image"
    return None


def _mention_lid(html: str) -> Optional[str]:
    m = _LID_RE.search(html)
    return m.group(1) if m else None


def _mark_text(html: str) -> Optional[str]:
    m = _MARK_RE.search(html)
    if not m:
        return None
    # Strip any residual HTML tags from the matched tail (defensive — in
    # practice the match ends at the message's own closing tag boundary).
    return re.sub(r"<[^>]+>", " ", m.group(0)).strip()


def _sender_name(html: str) -> Optional[str]:
    m = _PRE_PLAIN_RE.search(html)
    if m:
        return m.group(1)
    m2 = _AUTHOR_RE.search(html)
    return m2.group(1).strip() if m2 else None


_LOAD_HISTORY_JS = """
async ([sel, targetCount, maxSteps]) => {
  const countNow = () => document.querySelectorAll(sel).length;
  let count = countNow();
  for (let i = 0; i < maxSteps && count < targetCount; i++) {
    const els = document.querySelectorAll(sel);
    if (!els.length) break;
    const el = els[0];
    let container = el;
    let maxOverflow = 0;
    let node = el.parentElement;
    for (let d = 0; d < 8 && node; d++) {
      const overflow = node.scrollHeight - node.clientHeight;
      if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
      node = node.parentElement;
    }
    if (maxOverflow <= 0) break;
    container.scrollTop = 0;
    await new Promise(r => setTimeout(r, 400));
    const newCount = countNow();
    if (newCount <= count) break;  // stopped growing -> reached the top of history
    count = newCount;
  }
  return count;
}
"""


async def _ensure_history_loaded(page, full_sel: str, max_messages: int, max_steps: int = 10) -> int:
    """WhatsApp Web virtualizes long message lists — a chat with more
    history than fits the viewport only renders the tail until the panel
    is scrolled toward the top (see sender.py's get_group_participants,
    which documents/handles the identical issue for the members drawer).
    Bounded: at most `max_steps` scroll-to-top + settle iterations, and
    never past `max_messages` — this loads MORE of a small, fixed history
    window reliably, it does not turn the scan into an unbounded one."""
    try:
        return await _evaluate(page, _LOAD_HISTORY_JS, [full_sel, max_messages, max_steps])
    except Exception:
        logger.exception("mark_scan: history-load scroll failed (continuing with whatever is rendered)")
        return 0


_SCROLL_DIAGNOSTIC_JS = """
([sel]) => {
  const els = document.querySelectorAll(sel);
  if (!els.length) return null;
  let container = els[0];
  let maxOverflow = 0;
  let node = els[0].parentElement;
  for (let d = 0; d < 8 && node; d++) {
    const overflow = node.scrollHeight - node.clientHeight;
    if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
    node = node.parentElement;
  }
  const last = els[els.length - 1];
  const first = els[0];
  return {
    renderedCount: els.length,
    scrollTop: container.scrollTop, scrollHeight: container.scrollHeight, clientHeight: container.clientHeight,
    atBottom: (container.scrollHeight - container.scrollTop - container.clientHeight) < 5,
    firstDataId: first.getAttribute('data-id'), lastDataId: last.getAttribute('data-id'),
  };
}
"""

# Single scroll-to-top STEP (not the full loop _LOAD_HISTORY_JS runs) — used
# by _dump_window's own capture-then-scroll loop below, which needs to dump
# the currently-rendered messages BETWEEN each scroll step, not just once
# at the very end.
_SCROLL_STEP_JS = """
([sel]) => {
  const els = document.querySelectorAll(sel);
  if (!els.length) return {moved: false};
  let container = els[0];
  let maxOverflow = 0;
  let node = els[0].parentElement;
  for (let d = 0; d < 8 && node; d++) {
    const overflow = node.scrollHeight - node.clientHeight;
    if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
    node = node.parentElement;
  }
  if (maxOverflow <= 0) return {moved: false};
  container.scrollTop = 0;
  return {moved: true};
}
"""


async def _dump_window(
    page, group_name: str, max_messages: int, diagnostic: Optional[Dict[str, Any]] = None, max_steps: int = 10,
) -> List[Dict[str, Any]]:
    """Captures messages at MULTIPLE scroll checkpoints, merging by data-id
    — not a single dump after scrolling as far as possible. Root-caused
    2026-08-23: a real, screenshot-confirmed reply (correct group, real
    @mention, two ticks) never appeared across several repeated scans.
    Direct evidence (_SCROLL_DIAGNOSTIC_JS): scrollTop=0, atBottom=false,
    scrollHeight=4963 vs. only ~23 actually-rendered DOM nodes at that
    scroll position — WhatsApp Web virtualizes the message list (a fixed-
    size sliding window of real DOM nodes, not the full history), and the
    OLD design (_ensure_history_loaded scrolls all the way to the top,
    _dump_window then reads whatever's rendered) captured the TOP of
    history, evicting the newest messages — including any reply sent since
    the last scan — from the DOM entirely before ever seeing them.

    Fix: capture the TAIL first (WhatsApp opens scrolled to the bottom —
    this is the newest messages, exactly where a just-sent mark reply
    lives), then scroll toward older history one step at a time, capturing
    again after each step and merging everything by unique data-id. Still
    fully bounded: at most `max_steps` scroll iterations, stops early once
    a step surfaces no new messages (reached the top) or `max_messages`
    unique messages have been captured — never an unbounded scan."""
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"

    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    checkpoints: List[Dict[str, Any]] = []

    async def _capture_step(label: str) -> None:
        loc = page.locator(full_sel)
        n = await loc.count()
        new_here = 0
        for i in range(n):
            try:
                dump = await _evaluate(page, _DOM_DUMP_JS, [full_sel, i])
            except Exception:
                continue
            if not dump:
                continue
            data_id = _own_data_id(dump.get("messageHtml") or "")
            if not data_id or data_id in merged:
                continue
            merged[data_id] = dump
            order.append(data_id)
            new_here += 1
        checkpoints.append({"label": label, "rendered_count": n, "new_unique": new_here, "merged_total": len(merged)})

    await _capture_step("tail")

    for step in range(max_steps):
        if len(merged) >= max_messages:
            break
        try:
            step_result = await _evaluate(page, _SCROLL_STEP_JS, [full_sel])
        except Exception:
            logger.exception("mark_scan: history-load scroll step failed (continuing with whatever is captured)")
            break
        if not step_result.get("moved"):
            break
        await page.wait_for_timeout(400)
        before_count = len(merged)
        await _capture_step(f"scroll_step_{step}")
        if len(merged) == before_count:
            break  # no new messages surfaced -> reached the top of history

    if diagnostic is not None:
        diagnostic["capture_checkpoints"] = checkpoints
        try:
            diagnostic["scroll_state_after_history_load"] = await _evaluate(page, _SCROLL_DIAGNOSTIC_JS, [full_sel])
        except Exception as exc:
            diagnostic["scroll_state_after_history_load"] = {"error": str(exc)}

    return [merged[i] for i in order[:max_messages]]


async def _run_scan(page, req: Dict[str, Any], session=None) -> Dict[str, Any]:
    group_name = req["group_name"]
    max_messages = req.get("max_messages") or MAX_MESSAGES_SCANNED_DEFAULT

    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"error": f"Could not open WhatsApp group {group_name!r} (status={status})"}

    scan_diagnostic: Dict[str, Any] = {
        "session_identity": {
            "own_phone_number": getattr(session, "own_phone_number", None),
            "session_id": getattr(session, "session_id", None),
            "generation": getattr(session, "generation", None),
        },
    }
    window = await _dump_window(page, group_name, max_messages, diagnostic=scan_diagnostic)

    # Pass 1: index every plain (non-reply) media message's own hash/id in
    # this window — the source-of-truth pool a candidate mark resolves
    # against.
    sources_by_hash: Dict[str, Dict[str, Any]] = {}
    for pos, item in enumerate(window):
        html = item.get("messageHtml") or ""
        if item.get("quotedHtml"):
            continue  # this is itself a reply, not a plain source message
        data_id = _own_data_id(html)
        if not data_id:
            continue
        if _is_album(html):
            # Grouped media (2026-08-23) — one message, N tiles, each its
            # own deterministic identity. tile_index is a LOCATOR (used
            # only to click/download the right tile), never the identity
            # itself — that's (data_id, hash), same as any other source.
            # Per-tile media_type (2026-08-23 fix): a photo album's tiles
            # are images, not videos — hardcoding "video" here routed
            # every album-tile download through the video-viewer path
            # regardless of actual type.
            for tile_index, (h, tile_media_type) in enumerate(_album_tile_hashes_and_types(html)):
                sources_by_hash[h] = {
                    "source_message_id": data_id, "source_media_type": tile_media_type,
                    "source_sender": _sender_name(html), "window_position": pos,
                    "album_tile_index": tile_index, "is_album_tile": True,
                }
            continue
        media_type = _media_type(html)
        if not media_type:
            continue
        h = _smallest_hash(html)
        if not h:
            continue
        sources_by_hash[h] = {
            "source_message_id": data_id, "source_media_type": media_type,
            "source_sender": _sender_name(html), "window_position": pos,
        }

    # Pass 2: every reply that has BOTH a real mention (any LID) and the
    # literal word "mark" is a candidate — resolved here if possible, but
    # reported either way (never silently dropped just because resolution
    # failed; that's the backend's "MEDIA RESOLUTION FAILED" report, not a
    # worker-side decision).
    candidates: List[Dict[str, Any]] = []
    batch_candidates: List[Dict[str, Any]] = []
    for item in window:
        quoted_html = item.get("quotedHtml")
        if not quoted_html:
            continue
        html = item.get("messageHtml") or ""
        lid = _mention_lid(html)
        if not lid:
            continue
        mark_text = _mark_text(html)
        if not mark_text:
            continue
        quoted_hash = _smallest_hash(quoted_html)

        if quoted_hash is None:
            # No thumbnail hash at all in the quoted block — proven
            # (2026-08-23, real diagnostic reply) to be WhatsApp's
            # collapsed "N videos"/"N photos" summary for a reply to a
            # WHOLE album, not one tile. Only treated as a batch mark if
            # the text ALSO parses as one — an ordinary single-tile quote
            # whose hash genuinely failed to match anything still falls
            # through to the unresolved candidate below, a hard failure,
            # never silently reinterpreted.
            item_count = _quoted_is_whole_item_summary(quoted_html)
            batch = _parse_batch_role_list(mark_text)
            single_photos_m = _SINGLE_PHOTOS_RE.match(mark_text.strip()) if item_count else None
            if item_count and (batch or single_photos_m):
                batch_candidates.append({
                    "mention_lid": lid, "mark_text": mark_text,
                    "reply_message_id": _own_data_id(html), "item_count": item_count,
                    "batch": batch, "single_photos_project": single_photos_m.group(1).strip() if single_photos_m else None,
                })
                continue

        source = sources_by_hash.get(quoted_hash) if quoted_hash else None
        candidates.append({
            "mention_lid": lid,
            "mark_text": mark_text,
            "reply_message_id": _own_data_id(html),
            "quoted_thumbnail_hash": quoted_hash,
            "resolved_source_message_id": (source or {}).get("source_message_id"),
            "source_media_type": (source or {}).get("source_media_type"),
            "source_sender": (source or {}).get("source_sender"),
            "source_timestamp": None,
            "is_album_tile": (source or {}).get("is_album_tile", False),
            "album_tile_index": (source or {}).get("album_tile_index"),
        })

    # Live resolution phase for whole-album batch marks (2026-08-23) — the
    # one part of this scan that needs real Playwright interaction rather
    # than the static window dump: click the reply's own quoted-message
    # block (a real WhatsApp button that jumps to/highlights the original
    # message) and observe which message ends up centered in the
    # viewport — proven via a real test (jumped to the exact known album,
    # distance 0.3px from dead-center, all 4 tile hashes matched). Each
    # resolved batch reply is expanded into one candidate PER TILE here,
    # so validate_candidates on the backend sees ordinary-looking
    # single-tile candidates and needs no batch-specific logic at all.
    def _batch_failure_candidate(bc: Dict[str, Any], error: str) -> Dict[str, Any]:
        # A failed batch mark must NEVER be able to fall through to the
        # backend's single-mark parser (a real production bug, 2026-08-23:
        # extract_role_and_project only looks for the FIRST role keyword
        # it finds, so a failed batch's raw "take 1, take 2, take 3,
        # intro" text was misread as an ordinary single "take 1" mark,
        # creating a bogus assignment with no resolved hash). Every field
        # a single-media assignment would need is explicitly None here —
        # resolved_source_message_id included, even when we DO know the
        # album's data_id, because "we found the album but not which
        # tiles" is still an unresolved batch, not a usable single-media
        # identity — plus the explicit resolution_status marker the
        # backend's validate_candidates checks before its parser ever
        # sees this candidate's mark_text.
        return {
            "mention_lid": bc["mention_lid"], "mark_text": bc["mark_text"],
            "reply_message_id": bc["reply_message_id"],
            "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
            "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
            "resolution_status": "BATCH_RESOLUTION_FAILED", "batch_resolution_error": error,
        }

    for bc in batch_candidates:
        jump = await _resolve_quoted_jump(page, group_name, bc["reply_message_id"])
        if not jump.get("ok"):
            reason = jump.get("reason") or "quoted-jump resolution failed"
            # Diagnostic-only enrichment (2026-08-24): a "no quoted-message
            # block" failure carries a resolved_index/cross_check pair (see
            # _resolve_quoted_jump) proving whether the same element,
            # checked via raw JS at the SAME moment, also sees nothing —
            # distinguishing a genuine DOM absence from a Playwright-
            # locator-specific miss within this multi-candidate scan
            # sequence. Folded into the error string (not a schema
            # change) purely so it surfaces in the real scan report while
            # investigating this live.
            if jump.get("cross_check") is not None:
                reason = f"{reason} | resolved_index={jump.get('resolved_index')} cross_check={jump.get('cross_check')}"
            if jump.get("restoration_log"):
                reason = f"{reason} | restoration_log={jump.get('restoration_log')}"
            candidates.append(_batch_failure_candidate(bc, reason))
            continue
        tile_hashes_and_types = jump["tile_hashes_and_types"]
        if len(tile_hashes_and_types) != bc["item_count"]:
            # WhatsApp's own summary count disagrees with what we can
            # actually see in the jumped-to album — never guess which
            # tiles to use; report as a resolution failure for the whole
            # batch mark, not a partial/best-effort match.
            candidates.append(_batch_failure_candidate(
                bc, f"summary said {bc['item_count']} items, album has {len(tile_hashes_and_types)}",
            ))
            continue

        if bc["batch"]:
            project_fragment, role_items = bc["batch"]
            if len(role_items) != len(tile_hashes_and_types):
                candidates.append(_batch_failure_candidate(
                    bc, f"{len(role_items)} roles given for {len(tile_hashes_and_types)} tiles — counts must match exactly",
                ))
                continue
            for tile_index, (role_item, (h, media_type)) in enumerate(zip(role_items, tile_hashes_and_types)):
                candidates.append({
                    "mention_lid": bc["mention_lid"], "mark_text": f"mark {project_fragment} {role_item}",
                    "reply_message_id": bc["reply_message_id"], "quoted_thumbnail_hash": h,
                    "resolved_source_message_id": jump["data_id"], "source_media_type": media_type,
                    "source_sender": None, "source_timestamp": None,
                    "is_album_tile": True, "album_tile_index": tile_index,
                })
        else:
            # "mark <project> photos" against a whole photo album — every
            # tile gets role="photos"; no order/number is inferred (never
            # guessed), each tile's own hash is its real, distinct identity.
            project_fragment = bc["single_photos_project"]
            for tile_index, (h, media_type) in enumerate(tile_hashes_and_types):
                candidates.append({
                    "mention_lid": bc["mention_lid"], "mark_text": f"mark {project_fragment} photos",
                    "reply_message_id": bc["reply_message_id"], "quoted_thumbnail_hash": h,
                    "resolved_source_message_id": jump["data_id"], "source_media_type": media_type,
                    "source_sender": None, "source_timestamp": None,
                    "is_album_tile": True, "album_tile_index": tile_index,
                })

    # TEMPORARY debug (2026-08-23) — grouped/batch-media investigation.
    # Full source inventory (every plain media message found in the
    # window, in DOM order) so a grouped/multi-select send's individual
    # message identities can be inspected directly.
    #
    # ALSO TEMPORARY (2026-08-23): every reply with quoted content,
    # regardless of whether it has a real mention/mark — Pass 2 above only
    # ever surfaces replies that already pass full validation, but a
    # whole-album-reply batch-mark syntax hasn't been designed yet and its
    # exact quoted-block DOM shape is unverified. This captures raw
    # evidence (truncated) for that investigation without assuming
    # anything about the shape in advance.
    all_replies_debug = []
    for item in window:
        quoted_html = item.get("quotedHtml")
        if not quoted_html:
            continue
        html = item.get("messageHtml") or ""
        quoted_hash = _smallest_hash(quoted_html)
        all_replies_debug.append({
            "reply_data_id": _own_data_id(html),
            "has_real_mention": bool(_mention_lid(html)),
            "mark_text_best_effort": _mark_text(html),
            "quoted_is_album": _is_album(quoted_html),
            "quoted_album_tile_hashes": _album_tile_hashes(quoted_html) if _is_album(quoted_html) else None,
            "quoted_smallest_hash": quoted_hash,
            "quoted_html_snippet": quoted_html[:3000],
            "message_html_snippet": html[:1500],
            # Diagnostic-only, read-only (2026-08-24): investigating why a
            # bounded quoted-message re-hydration retry sometimes still
            # fails. Structural fact worth recording precisely: this loop
            # only ever reaches a message whose _dump_window-captured
            # quotedHtml was already truthy — so if a candidate later
            # fails with "no quoted-message block" in the LIVE re-check,
            # that block existed at INITIAL capture time and must have
            # been torn down afterward (not "never captured"). These two
            # extra fields make that comparison exact rather than
            # inferred from a snippet's length.
            "initial_html_len": len(html),
            "initial_quoted_html_len": len(quoted_html),
        })

    debug = {
        "window_count": len(window),
        "sources": [
            {"hash": h, **v} for h, v in sources_by_hash.items()
        ],
        "all_replies": all_replies_debug,
        "scan_diagnostic": scan_diagnostic,
    }
    return {"candidates": candidates, "debug": debug}


# ---------------------------------------------------------------------------
# Full-resolution media download — the one mechanism Phase 0 never tested
# (it only ever hashed a small embedded thumbnail). Fetches the media's
# `blob:`/network URL from within the page context and returns it as
# base64, decoded back to real bytes here. Defensive: any failure is
# reported per-item, never silently treated as success.
# ---------------------------------------------------------------------------
_DOWNLOAD_JS = """
async ([sel, idx]) => {
  const els = document.querySelectorAll(sel);
  if (idx >= els.length) return {ok: false, reason: "message not found at index"};
  const el = els[idx];
  const img = el.querySelector('img[src^="blob:"]');
  const video = el.querySelector('video[src^="blob:"]');
  const srcEl = video || img;
  if (!srcEl) return {ok: false, reason: "no blob: media element found"};
  try {
    const resp = await fetch(srcEl.src);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    return {ok: true, base64: btoa(binary), contentType: resp.headers.get('content-type') || ''};
  } catch (e) {
    return {ok: false, reason: String(e && e.message || e)};
  }
}
"""


# ---------------------------------------------------------------------------
# Album-tile download via WhatsApp Web's native right-click "Download"
# (2026-08-23) — replaces the media-viewer/blob: approach above for album
# tiles specifically, after three attempts proved that approach was
# clicking a tile without ever opening any viewer (evidence: the "blob:"
# elements found afterward were unrelated pre-existing photo thumbnails,
# and dumping every direct <body> child showed nothing new was ever
# mounted). Diagnostic-first: always dump the context menu's actual
# structure, whether or not the Download click succeeds, so a failure is
# reported with real evidence, never another blind guess.
# ---------------------------------------------------------------------------
_CONTEXT_MENU_DUMP_JS = """
() => {
  const menus = Array.from(document.querySelectorAll(
    '[role="menu"], [role="listbox"], ul[data-testid], div[data-animate-dropdown]'
  ));
  return menus.map(m => ({
    tag: m.tagName, role: m.getAttribute('role'), testid: m.getAttribute('data-testid'),
    items: Array.from(m.querySelectorAll('li, [role="menuitem"], div[role="button"]')).map(i => ({
      text: (i.innerText || '').trim().slice(0, 60),
      testid: i.getAttribute('data-testid'), role: i.getAttribute('role'),
    })),
  }));
}
"""

# Broader fallback dump, used only when _CONTEXT_MENU_DUMP_JS finds nothing —
# every direct <body> child plus, separately, any element anywhere whose
# role/testid/class hints at a popup, so a truly empty result means "nothing
# new was mounted at all" rather than "our selector guess was wrong". Same
# technique that already conclusively ruled out the media-viewer approach.
_BODY_SNAPSHOT_JS = """
() => {
  const child = (el) => ({
    tag: el.tagName, id: el.id || null, testid: el.getAttribute('data-testid'),
    role: el.getAttribute('role'), cls: (el.className || '').toString().slice(0, 80),
    rect: (() => { const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })(),
  });
  const bodyChildren = Array.from(document.body.children).map(child);
  const popupLike = Array.from(document.querySelectorAll(
    '[class*="popup"], [class*="menu"], [class*="dropdown"], [class*="context"], [aria-haspopup]'
  )).map(child);
  return { bodyChildren, popupLike };
}
"""


# ---------------------------------------------------------------------------
# Individual-tile VIEWER workflow (2026-08-23) — reproduces, step for step,
# what the user's own manual screenshots showed: open the tile -> a real
# media viewer mounts -> the video buffers -> a three-dot menu inside that
# viewer (not a right-click context menu) offers "Download". The three
# earlier failed attempts at this were BEFORE the click-targeting fixes
# proven later the same day (message row is full-width, bounding_box()
# needs an explicit scroll-into-view first) — this redoes the open step
# with those fixes applied, then works entirely inside whatever mounts,
# diagnostic-first for the menu-button discovery (no blind selector guess).
# ---------------------------------------------------------------------------
_VIDEO_STATE_JS = """
() => {
  const v = document.querySelector('video');
  if (!v) return null;
  let buffered = [];
  try {
    for (let i = 0; i < v.buffered.length; i++) buffered.push([v.buffered.start(i), v.buffered.end(i)]);
  } catch (e) {}
  return {
    readyState: v.readyState, networkState: v.networkState, duration: v.duration,
    currentTime: v.currentTime, buffered: buffered, src: v.currentSrc || v.src || null,
    videoWidth: v.videoWidth, videoHeight: v.videoHeight, paused: v.paused, error: v.error ? String(v.error.code) : null,
  };
}
"""

# Dumps every button-like element inside the viewer overlay — located by
# walking UP from the actual mounted <video> element to its direct-child-
# of-<body> ancestor, not by assuming DOM position (2026-08-23: Tile 1's
# test happened to have the viewer as body's literal last child, but Tile
# 2's did not — document.body.lastElementChild resolved to an unrelated
# "has-finished-comet-page" tracking div instead, with zero real buttons.
# Anchoring on the video's own ancestor chain is structural, not
# positional, so it can't be fooled by sibling ordering). Captures aria
# label, data-icon, and any inner <svg><title> text (WhatsApp icons
# reliably carry one, e.g. "ic-search", "ic-close", seen already in this
# codebase's own PHASE26B diagnostics), plus each button's own rect so a
# "top-right" candidate can be identified from real measured positions,
# never assumed.
_VIEWER_BUTTONS_JS = """
() => {
  const v = document.querySelector('video');
  if (!v) return {rootFound: false, buttons: [], reason: 'no video element'};
  let root = v;
  while (root.parentElement && root.parentElement !== document.body) {
    root = root.parentElement;
  }
  if (!root.parentElement) return {rootFound: false, buttons: [], reason: 'video not attached under body'};
  const btns = Array.from(root.querySelectorAll('button, [role="button"]'));
  return {
    rootFound: true,
    rootInfo: {tag: root.tagName, id: root.id || null, testid: root.getAttribute('data-testid')},
    buttons: btns.map(b => {
      const r = b.getBoundingClientRect();
      const svgTitle = b.querySelector('svg title');
      return {
        ariaLabel: b.getAttribute('aria-label'), dataIcon: b.getAttribute('data-icon'),
        testid: b.getAttribute('data-testid'), svgTitle: svgTitle ? svgTitle.textContent : null,
        rect: [r.x, r.y, r.width, r.height],
      };
    }),
  };
}
"""

# Finds whichever currently-rendered message is closest to the viewport's
# vertical center — used right after clicking a quoted-message block's own
# "jump to original" button, to observe (not assume) which message
# WhatsApp scrolled to, for whole-album replies whose quoted block carries
# no thumbnail hash (see the "quoted_jump" probe_type module note).
_CENTERED_MESSAGE_JS = """
() => {
  const els = Array.from(document.querySelectorAll('[data-testid^="conv-msg-"]'));
  const viewportCenter = window.innerHeight / 2;
  let best = null, bestDist = Infinity;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.height === 0) continue;
    const elCenter = r.y + r.height / 2;
    const dist = Math.abs(elCenter - viewportCenter);
    if (dist < bestDist) {
      bestDist = dist;
      best = el;
    }
  }
  if (!best) return null;
  return { dataId: best.getAttribute('data-id'), distancePx: bestDist };
}
"""


async def _hash_album_tiles_live(message_locator) -> List[Dict[str, Any]]:
    """Authoritative album tile inventory — hashes each ACTUAL
    [data-testid="video-content"/"image-content"] element's own outerHTML
    directly, never by chunking the whole message HTML on `grid-area:`
    CSS boundary count. Proven necessary (2026-08-23,
    _diagnose_album_lifecycle): opening/downloading/closing a tile causes
    WhatsApp to append substantial extra content into the SAME message
    subtree (html length grew 46843 -> 75901 -> 108327 bytes across two
    tile interactions — same DOM node throughout, never replaced), which
    desyncs any boundary-counting heuristic and undercounts tiles (4 -> 3
    -> 2) even though every tile's own data never changes. This is the
    single shared source of truth both _identify_tile_index (probe-only)
    and _resolve_quoted_jump (real scan-time batch resolution) use —
    fixing the extraction once here covers both call sites, and any
    future one.

    Returns one entry per element in DOM order — {hash, media_type} —
    even when a specific element's own read fails (hash: None), so the
    returned list's length always matches the real, current element
    count; never silently drops an entry the way chunk-based extraction
    could."""
    # image-thumb (2026-08-24) — a pure-photo album's tiles carry THIS
    # testid, not image-content (which never appeared on any real photo
    # album observed this session; every "video-content"/"image-content"
    # pair this function was originally written for came from mixed/video
    # albums). Proven via direct diagnostic: querying only video-content/
    # image-content against a real photo album returned zero tiles.
    tiles = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"], [data-testid="image-thumb"]')
    try:
        n = await tiles.count()
    except Exception:
        return []
    result = []
    for i in range(n):
        tile = tiles.nth(i)
        try:
            media_type_attr = await tile.get_attribute("data-testid", timeout=10000)
        except Exception:
            media_type_attr = None
        media_type = "image" if media_type_attr in ("image-content", "image-thumb") else "video"
        try:
            tile_html = await tile.evaluate("(el) => el.outerHTML", timeout=10000)
        except Exception:
            result.append({"hash": None, "media_type": media_type})
            continue
        result.append({"hash": _smallest_hash(tile_html), "media_type": media_type})
    return result


STUB_HTML_LEN_THRESHOLD = 1000
MAX_HYDRATION_ATTEMPTS = 3
HYDRATION_RETRY_DELAY_MS = 900


_SCROLL_METRICS_JS = """
([sel, dataId]) => {
  const els = document.querySelectorAll(sel);
  let container = null, maxOverflow = 0;
  if (els.length) {
    let node = els[0].parentElement;
    for (let d = 0; d < 8 && node; d++) {
      const overflow = node.scrollHeight - node.clientHeight;
      if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
      node = node.parentElement;
    }
  }
  const target = Array.from(els).find(el => el.getAttribute('data-id') === dataId);
  const rect = target ? target.getBoundingClientRect() : null;
  return {
    scrollTop: container ? container.scrollTop : null,
    scrollHeight: container ? container.scrollHeight : null,
    clientHeight: container ? container.clientHeight : null,
    target_found: !!target,
    target_own_data_id: target ? target.getAttribute('data-id') : null,
    target_rect: rect ? [rect.x, rect.y, rect.width, rect.height] : null,
    target_html_len: target ? target.outerHTML.length : null,
    quoted_aria_found: target ? !!target.querySelector('[aria-label="Quoted message"]') : false,
  };
}
"""


async def _restore_message_to_viewport(page, full_sel: str, reply_data_id: str, reply_message) -> Dict[str, Any]:
    """State-restoration experiment (2026-08-24) — 5-run evidence proved
    the target message is ALWAYS fully hydrated in _dump_window's own
    capture (html_len=4959 in every run, pass or fail); the divergence is
    that _dump_window's own history-loading scroll always ends scrolled
    well away from the tail before Pass 2 runs, and the SAME message/
    data-id later re-renders as a 222-byte virtualized stub on the
    failing runs. Tests whether actively restoring the message into the
    active viewport (Playwright's own scroll_into_view_if_needed — the
    same safe, already-proven mechanism used elsewhere in this file, not
    a new scroll hack) reliably re-hydrates it, rather than merely
    waiting in place. Records full before/after scroll-container and
    target-element metrics for evidence, whether or not it works."""
    t0 = time.monotonic()
    try:
        before = await _evaluate(page, _SCROLL_METRICS_JS, [full_sel, reply_data_id])
    except Exception as exc:
        before = {"error": str(exc)}
    try:
        await reply_message.scroll_into_view_if_needed(timeout=5000)
        scroll_error = None
    except Exception as exc:
        scroll_error = str(exc)
    await page.wait_for_timeout(600)
    try:
        after = await _evaluate(page, _SCROLL_METRICS_JS, [full_sel, reply_data_id])
    except Exception as exc:
        after = {"error": str(exc)}
    return {
        "before": before, "after": after, "scroll_error": scroll_error,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "data_id_stable": (before.get("target_own_data_id") == after.get("target_own_data_id") == reply_data_id),
    }


async def _wait_for_quoted_message_block(page, group_name: str, reply_data_id: str, full_sel: str) -> Dict[str, Any]:
    """Bounded re-hydration wait (2026-08-24) — real evidence (cross_check
    html_len=222 vs. the same message's real ~4959-byte rendered size when
    checked in isolation) proved WhatsApp's virtualized list can
    momentarily mount a lightweight, not-yet-hydrated stub for a message
    in the middle of a busy multi-candidate scan, before the quoted-
    message block (and the rest of the reply's real content) has
    rendered. Re-locates the message fresh each attempt via
    _find_message_index_by_data_id (never trusts a stale index — that
    function's own tail-first-then-scroll search is what keeps this from
    losing the target to virtualization) and re-checks; never scrolls the
    conversation itself beyond what that existing search already does,
    and never falls back to picking a message by proximity — only ever
    the exact `reply_data_id` requested.

    2026-08-24 update: on detecting a stub, actively restores the message
    into the viewport (_restore_message_to_viewport) instead of merely
    waiting in place — a targeted experiment, bounded to the same 2
    retry opportunities this function already had.

    Returns {"ok": True, "reply_message": <locator>, "quoted": <locator>,
    "restoration_log": [...]} once a real quoted-message block is found,
    or {"ok": False, "reason", "resolved_index", "requested_data_id",
    "cross_check", "hydration_attempts", "restoration_log"} if it never
    hydrates (a genuine reply-to-a-non-media-message never has one
    either — this exhausts the same bounded retries and correctly
    reports failure, not stalls)."""
    last_cross_check: Optional[Dict[str, Any]] = None
    restoration_log: List[Dict[str, Any]] = []
    for attempt in range(MAX_HYDRATION_ATTEMPTS):
        idx = await _find_message_index_by_data_id(page, group_name, reply_data_id)
        if idx is None:
            return {"ok": False, "reason": f"reply message {reply_data_id!r} not found in scanned window", "restoration_log": restoration_log}
        reply_message = page.locator(full_sel).nth(idx)
        quoted = reply_message.locator('[data-testid="quoted-message"]')
        if await quoted.count() > 0:
            return {"ok": True, "reply_message": reply_message, "quoted": quoted, "restoration_log": restoration_log}
        try:
            last_cross_check = await reply_message.evaluate("""
                (el) => ({
                  own_data_id: el.getAttribute('data-id'),
                  js_quoted_found: !!el.querySelector('[data-testid="quoted-message"]'),
                  html_len: el.outerHTML.length,
                })
            """, timeout=10000)
        except Exception as exc:
            last_cross_check = {"cross_check_error": str(exc)}
        is_stub = bool(last_cross_check.get("html_len") is not None and last_cross_check["html_len"] < STUB_HTML_LEN_THRESHOLD)
        if attempt < MAX_HYDRATION_ATTEMPTS - 1 and is_stub:
            restored = await _restore_message_to_viewport(page, full_sel, reply_data_id, reply_message)
            restoration_log.append(restored)
            continue
        # Either not a stub (genuinely no quoted block — a real reply-to-
        # a-non-media-message case) or retries exhausted: stop, never
        # guess, report exactly what was observed on the last attempt.
        return {
            "ok": False, "reason": "reply message has no quoted-message block",
            "resolved_index": idx, "requested_data_id": reply_data_id, "cross_check": last_cross_check,
            "hydration_attempts": attempt + 1, "restoration_log": restoration_log,
        }
    return {"ok": False, "reason": "unreachable", "restoration_log": restoration_log}


async def _resolve_quoted_jump(page, group_name: str, reply_data_id: str) -> Dict[str, Any]:
    """Clicks a reply's own quoted-message block (a real WhatsApp button
    that jumps to/highlights the original message) and observes which
    message ends up closest to the viewport's vertical center afterward —
    the proven identity link (2026-08-23: jumped to the exact known
    album, 0.3px from dead-center, all 4 tile hashes matched) for a reply
    whose quoted block carries no thumbnail hash — WhatsApp's collapsed
    "N videos"/"N photos" summary for a reply to a WHOLE album, not one
    tile. Returns {ok, data_id, is_album, tile_hashes_and_types, reason}."""
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    located = await _wait_for_quoted_message_block(page, group_name, reply_data_id, full_sel)
    if not located.get("ok"):
        return located
    quoted = located["quoted"]
    try:
        await quoted.first.click(timeout=10000)
    except Exception as exc:
        return {"ok": False, "reason": f"click on quoted-message failed: {exc}"}
    await page.wait_for_timeout(1000)
    try:
        centered = await _evaluate(page, _CENTERED_MESSAGE_JS)
    except Exception as exc:
        return {"ok": False, "reason": f"centered-message evaluate failed: {exc}"}
    if not centered or not centered.get("dataId"):
        return {"ok": False, "reason": "no message found near viewport center after jump", "centered": centered}
    jumped_idx = await _find_message_index_by_data_id(page, group_name, centered["dataId"])
    if jumped_idx is None:
        return {"ok": False, "reason": "jumped-to message no longer found in window", "data_id": centered["dataId"]}
    jumped_message = page.locator(full_sel).nth(jumped_idx)
    try:
        jumped_html = await jumped_message.evaluate("(el) => el.outerHTML", timeout=10000)
    except Exception as exc:
        return {"ok": False, "reason": f"could not read jumped-to message HTML: {exc}", "data_id": centered["dataId"]}
    jumped_html = jumped_html[:HTML_TRUNCATE]
    if not _is_album(jumped_html):
        return {
            "ok": False, "data_id": centered["dataId"],
            "reason": "jumped-to message is not an album (batch marking only supports whole-album replies)",
        }
    # Per-element hashing (2026-08-23 fix), not grid-area chunking — see
    # _hash_album_tiles_live's own docstring for the full evidence trail.
    live_tiles = await _hash_album_tiles_live(jumped_message)
    tile_hashes_and_types = [(t["hash"], t["media_type"]) for t in live_tiles if t["hash"]]
    return {
        "ok": True, "data_id": centered["dataId"], "is_album": True,
        "tile_hashes_and_types": tile_hashes_and_types, "centered": centered,
    }


async def _identify_tile_index(page, group_name: str, data_id: str, expected_hash: str) -> Dict[str, Any]:
    """Re-derives which tile index currently corresponds to `expected_hash`,
    via the shared _hash_album_tiles_live (per-element hashing, not
    grid-area chunking — see its own docstring for the full evidence
    trail).

    Note: production's _run_download does NOT call this function at all —
    it uses the album_tile_index already recorded once during the
    original scan (never re-derived at download time). This function
    exists only for this probe tool's own single-tile ad-hoc testing
    convenience, where no prior scan result is available to draw an
    index from."""
    idx = await _find_message_index_by_data_id(page, group_name, data_id)
    if idx is None:
        return {"ok": False, "reason": "album message not found in scanned window"}
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    message = page.locator(full_sel).nth(idx)
    live_tiles = await _hash_album_tiles_live(message)
    tile_hashes = [t["hash"] for t in live_tiles]
    if expected_hash not in tile_hashes:
        return {"ok": False, "reason": "expected tile hash not found among current album tiles", "tile_hashes": tile_hashes, "message_idx": idx}
    tile_index = tile_hashes.index(expected_hash)
    return {"ok": True, "message_idx": idx, "tile_index": tile_index, "tile_hashes": tile_hashes}


async def _wait_for_video_readiness(page, min_ready_state: int = 3, timeout_s: float = 60.0) -> Dict[str, Any]:
    """Bounded poll on the ACTUAL <video> element's own readyState/
    networkState/buffered — never a fixed sleep. Returns the last observed
    state regardless of whether the threshold was reached, so a timeout
    still carries real evidence rather than silence.

    readyState alone is NOT sufficient (proven 2026-08-23, Tile 3): it
    reached 4 (HAVE_ENOUGH_DATA) after 1s with only [0, 3.839] of a
    14.7s video actually buffered, and WhatsApp's own viewer menu did
    NOT offer "Download" at that point — vs. Tiles 1-2, where the video
    had buffered its FULL duration and Download was present both times.
    Also require the buffered range to reach (near) the video's full
    duration before considering it ready."""
    elapsed = 0.0
    step = 1.0
    last_state = None
    while elapsed < timeout_s:
        try:
            last_state = await _evaluate(page, _VIDEO_STATE_JS)
        except Exception:
            last_state = None
        if last_state and last_state.get("readyState", 0) >= min_ready_state:
            duration = last_state.get("duration") or 0
            buffered = last_state.get("buffered") or []
            buffered_end = max((b[1] for b in buffered), default=0.0)
            if duration and buffered_end >= duration - 0.5:
                return {"reached": True, "elapsed_s": elapsed, "state": last_state}
        await page.wait_for_timeout(int(step * 1000))
        elapsed += step
    return {"reached": False, "elapsed_s": elapsed, "state": last_state}


# Viewer-close lifecycle diagnostic (2026-08-23) — the real E2E's Take 2/3/
# Introduction all failed identically at open_tile with "pointer events
# intercepted" by a background div, after Take 1's viewer successfully
# opened, buffered, and downloaded. _open_tile_viewer_and_download's
# success path never explicitly closes the viewer at all — this captures
# exactly what the user asked for (overlays, fixed/absolute/high-z-index
# elements, elementFromPoint/elementsFromPoint at the next tile's own
# click point, bounding boxes, pointer-events, data-testid/aria) at three
# points: before opening anything, while the viewer is still open, and
# after attempting to close it — never assumed, always captured fresh.
_OVERLAY_DIAGNOSTIC_JS = """
([x, y]) => {
  const describe = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName, id: el.id || null, testid: el.getAttribute('data-testid'),
      role: el.getAttribute('role'), ariaLabel: el.getAttribute('aria-label'),
      cls: (el.className || '').toString().slice(0, 100),
      position: cs.position, zIndex: cs.zIndex, pointerEvents: cs.pointerEvents,
      display: cs.display, visibility: cs.visibility,
      rect: [r.x, r.y, r.width, r.height],
    };
  };

  const all = Array.from(document.querySelectorAll('body *'));
  const suspects = [];
  for (const el of all) {
    const cs = getComputedStyle(el);
    const z = parseInt(cs.zIndex || '0', 10) || 0;
    if (cs.position === 'fixed' || cs.position === 'absolute' || z > 0) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) suspects.push(describe(el));
    }
  }

  let atPoint = null;
  if (typeof x === 'number' && typeof y === 'number') {
    atPoint = {
      elementFromPoint: describe(document.elementFromPoint(x, y)),
      elementsFromPoint: document.elementsFromPoint(x, y).slice(0, 8).map(describe),
    };
  }

  return { suspectOverlayCount: suspects.length, suspects: suspects.slice(0, 40), atPoint };
}
"""


async def _diagnose_viewer_close_lifecycle(page, message_locator, tile1_index: int, tile2_index: int) -> Dict[str, Any]:
    """Diagnostic-only: never assumes what's blocking tile 2's click —
    captures real DOM/style evidence at three checkpoints (baseline,
    viewer-open, after-close-attempt) so the actual lifecycle issue can be
    identified and fixed properly, not patched around with force=True or
    a longer timeout."""
    report: Dict[str, Any] = {}

    tile2 = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile2_index)
    try:
        box = await tile2.bounding_box()
    except Exception:
        box = None
    point = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2] if box else [None, None]
    report["tile2_click_point"] = point

    report["baseline_before_opening_tile1"] = await _evaluate(page, _OVERLAY_DIAGNOSTIC_JS, point)

    dl = await _open_tile_viewer_and_download(page, message_locator, tile1_index)
    report["tile1_download_result"] = {k: v for k, v in dl.items() if k != "downloads"}
    report["tile1_download_ok"] = dl.get("ok")

    report["while_viewer_open"] = await _evaluate(page, _OVERLAY_DIAGNOSTIC_JS, point)
    try:
        report["viewer_buttons_while_open"] = await _evaluate(page, _VIEWER_BUTTONS_JS)
    except Exception as exc:
        report["viewer_buttons_while_open"] = {"error": str(exc)}

    close_btn = None
    for b in (report["viewer_buttons_while_open"] or {}).get("buttons", []) if isinstance(report["viewer_buttons_while_open"], dict) else []:
        label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
        if re.search(r"close|back|dismiss", label):
            close_btn = b
            break
    report["close_button_found"] = close_btn

    if close_btn:
        cx = close_btn["rect"][0] + close_btn["rect"][2] / 2
        cy = close_btn["rect"][1] + close_btn["rect"][3] / 2
        try:
            await page.mouse.click(cx, cy, button="left")
            report["close_action"] = "clicked_close_button"
        except Exception as exc:
            report["close_action"] = f"close_button_click_failed: {exc}"
    else:
        try:
            await page.keyboard.press("Escape")
            report["close_action"] = "escape_key_fallback_no_close_button_found"
        except Exception as exc:
            report["close_action"] = f"escape_failed: {exc}"

    await page.wait_for_timeout(800)
    report["after_close_attempt"] = await _evaluate(page, _OVERLAY_DIAGNOSTIC_JS, point)

    # The actual proof the diagnostic is after: does the SAME click point
    # WhatsApp's own click-target logic would use for tile 2 now resolve
    # to the real tile, or is something else still on top of it?
    if box:
        try:
            live_at_point = await _evaluate(
                page,
                "([x, y]) => { const el = document.elementFromPoint(x, y); "
                "return el ? {tag: el.tagName, testid: el.getAttribute('data-testid')} : null; }",
                point,
            )
            report["tile2_point_resolves_to_tile"] = bool(
                live_at_point and live_at_point.get("testid") in ("video-content", "image-content")
            )
            report["tile2_point_actual_element"] = live_at_point
        except Exception as exc:
            report["tile2_point_check_error"] = str(exc)

    return report


# Album-lifecycle diagnostic (2026-08-23) — after the sequential 4-tile
# test, tiles 0 and 1 downloaded correctly (proving the viewer-close fix),
# but tiles 2 and 3 then failed with "expected tile hash not found among
# current album tiles" — a re-scan independently confirmed only 2 of 4
# tiles are visible in the album's OWN re-extracted HTML afterward. Never
# assumed the other two tiles vanished from WhatsApp itself: tags the
# live album DOM node with a custom attribute (persists ONLY if React did
# NOT remount the node — a direct identity test, not inferred from
# data-id alone, which could be reused by a replacement node) and, at
# each checkpoint, searches for EVERY base64 blob anywhere in the full
# message HTML (not just the grid-area-chunked structure
# _album_tile_hashes already relies on) so a tile that moved outside the
# expected shape would still be found by its known hash.
_ALBUM_IDENTITY_MARK_JS = """
([sel, dataId, marker]) => {
  const els = document.querySelectorAll(sel);
  for (const el of els) {
    if (el.getAttribute('data-id') === dataId) {
      el.setAttribute('data-diag-marker', marker);
      return true;
    }
  }
  return false;
}
"""

_ALBUM_MARKER_CHECK_JS = """
([marker]) => !!document.querySelector('[data-diag-marker="' + marker + '"]')
"""


async def _diagnose_album_lifecycle(page, group_name: str, album_data_id: str, known_tile_hashes: List[str]) -> Dict[str, Any]:
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    marker = f"diag-{uuid.uuid4().hex[:8]}"

    async def _snapshot(label: str) -> Dict[str, Any]:
        idx = await _find_message_index_by_data_id(page, group_name, album_data_id)
        if idx is None:
            return {"label": label, "found_by_data_id": False}
        loc = page.locator(full_sel).nth(idx)
        try:
            html_full = await loc.evaluate("(el) => el.outerHTML", timeout=10000)
        except Exception as exc:
            return {"label": label, "found_by_data_id": True, "error": str(exc)}
        html_trunc = html_full[:HTML_TRUNCATE]
        try:
            marker_present = await _evaluate(page, _ALBUM_MARKER_CHECK_JS, [marker])
        except Exception:
            marker_present = None
        structured = _album_tile_hashes_and_types(html_trunc)
        all_blobs = _B64_RE.findall(html_full)
        all_hashes_anywhere = {hashlib.sha256(b.encode()).hexdigest() for b in all_blobs}
        grid_area_marker_count = len(_GRID_AREA_RE.findall(html_full))
        try:
            testid_count = await loc.locator('[data-testid="video-content"], [data-testid="image-content"]').count()
        except Exception:
            testid_count = None
        return {
            "label": label, "found_by_data_id": True,
            "own_data_id": _own_data_id(html_trunc),
            "same_dom_node_as_before": marker_present,
            "is_album": _is_album(html_trunc),
            "structured_tile_count": len(structured),
            "structured_tile_hashes": [h for h, _ in structured],
            "grid_area_marker_count_in_full_html": grid_area_marker_count,
            "clickable_video_or_image_testid_count": testid_count,
            "total_base64_blobs_anywhere_in_message": len(all_blobs),
            "known_tile_hash_found_anywhere_in_message": {h: (h in all_hashes_anywhere) for h in known_tile_hashes},
            "message_html_length": len(html_full),
        }

    report: Dict[str, Any] = {"marker": marker, "known_tile_hashes": known_tile_hashes}

    try:
        report["marked_ok"] = await _evaluate(page, _ALBUM_IDENTITY_MARK_JS, [full_sel, album_data_id, marker])
    except Exception as exc:
        report["marked_ok"] = False
        report["mark_error"] = str(exc)

    report["before"] = await _snapshot("before")

    idx0 = await _find_message_index_by_data_id(page, group_name, album_data_id)
    if idx0 is None:
        report["error"] = "album not found before any interaction"
        return report
    message0 = page.locator(full_sel).nth(idx0)
    dl1 = await _open_tile_viewer_and_download(page, message0, 0)
    report["tile1_result"] = {"ok": dl1.get("ok"), "viewer_closed": dl1.get("viewer_closed"), "stage": dl1.get("stage")}

    report["after_tile1"] = await _snapshot("after_tile1")

    idx1 = await _find_message_index_by_data_id(page, group_name, album_data_id)
    if idx1 is None:
        report["error_after_tile1"] = "album not found by data-id after tile1 interaction"
        return report
    message1 = page.locator(full_sel).nth(idx1)
    dl2 = await _open_tile_viewer_and_download(page, message1, 1)
    report["tile2_result"] = {"ok": dl2.get("ok"), "viewer_closed": dl2.get("viewer_closed"), "stage": dl2.get("stage")}

    report["after_tile2"] = await _snapshot("after_tile2")

    return report


MAX_TILE_CLICK_ATTEMPTS = 3


async def _resolve_video_tile_locator(page, group_name: str, source_message_id: str, tile_index: int):
    """Fresh, position-independent resolution (2026-08-24 fix): re-finds
    the message by its immutable source_message_id — NEVER trusts a
    previously-computed positional index, which can go stale if the
    group receives new messages between when that index was computed and
    when the click actually fires (real 2026-08-24 finding: a live SEND
    attempt's Playwright error referenced index 3; a diagnostic moments
    later found the exact same message, unchanged, at index 10 — the
    group had received 8 new messages in between). Returns
    (tile_locator, message_locator) or (None, None) if the message isn't
    found at all — never substitutes a different message/tile."""
    idx = await _find_message_index_by_data_id(page, group_name, source_message_id)
    if idx is None:
        return None, None
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    message_locator = page.locator(full_sel).nth(idx)
    tile = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile_index)
    return tile, message_locator


async def _open_tile_viewer_and_download(
    page, message_locator, tile_index: int,
    *, group_name: Optional[str] = None, source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Reproduces the manual workflow exactly: open the tile -> wait for a
    real <video> to mount -> wait for it to actually buffer (bounded, not
    a fixed sleep) -> dump the viewer's own buttons (diagnostic-first,
    never a blind selector guess) -> if one looks like a menu trigger,
    click it, dump whatever menu appears, click "Download" if present, and
    collect the resulting download.

    `group_name`/`source_message_id` (2026-08-24, video-tile re-resolution
    fix): when BOTH are given, `message_locator` is only an initial
    reference — the tile the click actually targets is re-resolved fresh
    from `source_message_id` (see _resolve_video_tile_locator) immediately
    before the click, and again — bounded to MAX_TILE_CLICK_ATTEMPTS total
    attempts, never falling back to a different message or tile — if the
    click fails with a "not stable"/"detached" error specifically. A
    message that's genuinely gone fails cleanly once re-resolution itself
    returns nothing; this never infinitely retries. When either param is
    omitted (every existing diagnostic-only caller), behavior is exactly
    as before — message_locator is used directly, no re-resolution."""
    use_live_resolution = bool(group_name and source_message_id)

    if use_live_resolution:
        tile, _ = await _resolve_video_tile_locator(page, group_name, source_message_id, tile_index)
        if tile is None:
            return {"ok": False, "stage": "open_tile", "reason": "source message no longer found in window (live re-resolution)"}
    else:
        tile = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile_index)

    try:
        await tile.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        await _evaluate(page, _EVENT_CAPTURE_INSTALL_JS)
    except Exception:
        pass

    click_error: Optional[Exception] = None
    click_attempts: List[str] = []
    max_attempts = MAX_TILE_CLICK_ATTEMPTS if use_live_resolution else 1
    for attempt in range(max_attempts):
        try:
            await tile.click(timeout=10000)
            click_error = None
            break
        except Exception as exc:
            click_error = exc
            click_attempts.append(str(exc))
            is_stability_error = "not stable" in str(exc) or "detached" in str(exc)
            if not use_live_resolution or not is_stability_error or attempt == max_attempts - 1:
                break
            # Re-resolve fresh by source_message_id (never trust the
            # stale index) and retry — never falls back to a different
            # message or tile; if re-resolution can't find the SAME
            # message at all, that's a clean failure, not a substitution.
            tile, _ = await _resolve_video_tile_locator(page, group_name, source_message_id, tile_index)
            if tile is None:
                click_error = RuntimeError("source message no longer found in window during retry")
                break
            try:
                await tile.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass

    if click_error is not None:
        return {"ok": False, "stage": "open_tile", "reason": f"click failed: {click_error}", "click_attempts": click_attempts}

    video_mounted = False
    elapsed = 0.0
    while elapsed < 15.0:
        try:
            count = await page.locator("video").count()
        except Exception:
            count = 0
        if count > 0:
            video_mounted = True
            break
        await page.wait_for_timeout(500)
        elapsed += 0.5

    try:
        click_event_log = await _evaluate(page, _EVENT_CAPTURE_READ_JS)
    except Exception:
        click_event_log = None

    if not video_mounted:
        try:
            body_snapshot = await _evaluate(page, _BODY_SNAPSHOT_JS)
        except Exception:
            body_snapshot = None
        return {
            "ok": False, "stage": "open_tile", "reason": "no <video> element mounted within 15s of clicking the tile",
            "click_event_log": click_event_log, "body_snapshot": body_snapshot,
        }

    readiness = await _wait_for_video_readiness(page)

    try:
        viewer_buttons = await _evaluate(page, _VIEWER_BUTTONS_JS)
    except Exception:
        viewer_buttons = {"rootFound": False, "buttons": []}

    # The viewer is now genuinely open (video mounted) — from here on,
    # EVERY exit path must close it properly before returning, or the next
    # tile's open-click fails (proven root cause, 2026-08-23: the real
    # E2E's Take 2/3/Introduction all failed identically at open_tile with
    # "pointer events intercepted" by a background div, because Take 1's
    # successful download path never closed its viewer at all). Wrapped in
    # an inner function purely so every early return below still reaches
    # the shared _close_viewer() call at the bottom, without duplicating it
    # at each return site.
    async def _inner() -> Dict[str, Any]:
        menu_trigger = None
        for b in viewer_buttons.get("buttons", []):
            label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
            if re.search(r"menu|more|option|kebab", label):
                menu_trigger = b
                break
        if menu_trigger is None:
            # Fallback: the button positioned furthest toward the top-right
            # of the viewport, matching the user's own description
            # ("top-right three-dot menu") — measured from real captured
            # rects, not assumed.
            candidates = [b for b in viewer_buttons.get("buttons", []) if b.get("rect")]
            if candidates:
                menu_trigger = max(candidates, key=lambda b: b["rect"][0] - b["rect"][1])

        if menu_trigger is None:
            return {
                "ok": False, "stage": "find_menu_trigger", "reason": "video mounted but no plausible menu-trigger button found",
                "readiness": readiness, "viewer_buttons": viewer_buttons,
            }

        mx = menu_trigger["rect"][0] + menu_trigger["rect"][2] / 2
        my = menu_trigger["rect"][1] + menu_trigger["rect"][3] / 2
        try:
            await page.mouse.click(mx, my, button="left")
        except Exception as exc:
            return {"ok": False, "stage": "click_menu_trigger", "reason": str(exc), "readiness": readiness, "menu_trigger": menu_trigger}

        await page.wait_for_timeout(600)
        try:
            menu_dump = await _evaluate(page, _CONTEXT_MENU_DUMP_JS)
        except Exception:
            menu_dump = None
        if not menu_dump:
            try:
                body_snapshot = await _evaluate(page, _BODY_SNAPSHOT_JS)
            except Exception:
                body_snapshot = None
            return {
                "ok": False, "stage": "menu_after_trigger_click", "reason": "clicked menu trigger but no menu appeared",
                "readiness": readiness, "menu_trigger": menu_trigger, "body_snapshot": body_snapshot,
            }

        item = page.locator(
            '[role="menu"] :text-matches("download", "i"), '
            '[role="menuitem"]:has-text("Download"), '
            'li:has-text("Download"), '
            'div[role="button"]:has-text("Download")'
        ).first
        try:
            visible = await item.is_visible(timeout=3000)
        except Exception:
            visible = False
        if not visible:
            return {
                "ok": False, "stage": "find_download_item", "reason": "menu appeared but no 'Download' item found",
                "readiness": readiness, "menu_dump": menu_dump,
            }

        async def _click_item():
            await item.click(timeout=5000)

        downloads = await _collect_downloads(page, _click_item)
        return {
            "ok": bool(downloads) and any(d.get("ok") for d in downloads),
            "readiness": readiness, "menu_dump": menu_dump, "downloads": downloads,
        }

    result = await _inner()
    result["viewer_closed"] = await _close_viewer(page, viewer_buttons)
    return result


async def _close_viewer(page, viewer_buttons: Dict[str, Any]) -> Dict[str, Any]:
    """Finds and clicks the REAL WhatsApp "Close" control — reusing the
    SAME button dump already captured for menu-trigger discovery, never a
    second blind lookup — and waits until the viewer is actually gone (no
    <video> element remains), never a fixed sleep or force=True. Proven
    (2026-08-23): after clicking this exact real Close button
    (aria-label="Close", svg title "ic-close"), the next tile's own click
    point correctly resolves back to that tile via elementFromPoint —
    confirmed with real evidence via a diagnostic probe, not assumed."""
    close_btn = None
    for b in (viewer_buttons or {}).get("buttons", []):
        label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
        if re.search(r"close|back|dismiss", label):
            close_btn = b
            break

    if close_btn is None:
        # No real close control found in this viewer's own button dump —
        # Escape as a last resort, still verified below rather than trusted.
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    else:
        cx = close_btn["rect"][0] + close_btn["rect"][2] / 2
        cy = close_btn["rect"][1] + close_btn["rect"][3] / 2
        try:
            await page.mouse.click(cx, cy, button="left")
        except Exception as exc:
            return {"closed": False, "reason": f"close button click failed: {exc}", "used_close_button": True}

    elapsed = 0.0
    while elapsed < 8.0:
        try:
            count = await page.locator("video").count()
        except Exception:
            count = -1
        if count == 0:
            return {"closed": True, "elapsed_s": elapsed, "used_close_button": close_btn is not None}
        await page.wait_for_timeout(300)
        elapsed += 0.3
    return {
        "closed": False, "reason": "video element still present 8s after close attempt",
        "used_close_button": close_btn is not None,
    }


async def _find_message_index_by_data_id(page, group_name: str, data_id: str) -> Optional[int]:
    """Checks the currently-rendered tail FIRST (the group was just
    opened, which WhatsApp scrolls to the bottom by default) — only
    scrolls up to search older history if not found there. Scrolling
    unconditionally before searching would evict a tail-resident target
    from WhatsApp's virtualized DOM before ever finding it (2026-08-23:
    the exact same root cause proven for _dump_window — a real reply,
    freshly sent, resolved zero times because this function's old
    unconditional _ensure_history_loaded() scrolled straight to the top
    first, and the reply — living in the tail — was never rendered by
    the time the search actually ran)."""
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"

    async def _search_current() -> Optional[int]:
        loc = page.locator(full_sel)
        n = await loc.count()
        for i in range(n):
            try:
                testid = await loc.nth(i).get_attribute("data-testid")
            except Exception:
                continue
            if testid == f"conv-msg-{data_id}":
                return i
        return None

    found = await _search_current()
    if found is not None:
        return found

    await _ensure_history_loaded(page, full_sel, MAX_MESSAGES_SCANNED_DEFAULT)
    return await _search_current()


# ---------------------------------------------------------------------------
# Download-mechanism PROBE (2026-08-23) — diagnostic only, never uploads.
# The user manually confirmed via screenshots that a right-click on the
# *message/album itself* (not on an individual tile) opens a real WhatsApp
# context menu with "Download" (single message) or "Download all" (album) —
# a different target than the tile-scoped right-click above, which reached
# no menu at all. This probes that exact confirmed UI path against a real
# message and reports hard evidence (menu contents, downloaded file
# diagnostics) without assuming file count, order, or format.
# ---------------------------------------------------------------------------
async def _read_file_diagnostics(path: str) -> Dict[str, Any]:
    """Local-only diagnostics on a downloaded file — byte length, sha256,
    magic-byte signature, and (best-effort, via ffprobe/ffmpeg) container
    duration and a hash of the first extracted frame. Includes the raw
    bytes under "_raw_bytes" (underscore-prefixed: the real upload path
    needs them, but every diagnostic/report path MUST strip this key
    before JSON-serializing a result back to the backend — never ships
    raw bytes over HTTP/Mongo as part of a report)."""
    with open(path, "rb") as f:
        data = f.read()
    info: Dict[str, Any] = {
        "_raw_bytes": data,
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "magic_hex": data[:16].hex(),
        "is_zip": data[:4] == b"PK\x03\x04",
    }
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=20, text=True,
        )
        if probe.returncode == 0 and probe.stdout:
            import json as _json
            parsed = _json.loads(probe.stdout)
            fmt = parsed.get("format") or {}
            streams = parsed.get("streams") or []
            vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
            info["ffprobe"] = {
                "duration_s": fmt.get("duration"), "format_name": fmt.get("format_name"),
                "width": vstream.get("width") if vstream else None,
                "height": vstream.get("height") if vstream else None,
                "codec": vstream.get("codec_name") if vstream else None,
            }
        else:
            info["ffprobe_error"] = (probe.stderr or "")[:300]
    except Exception as exc:
        info["ffprobe_error"] = str(exc)
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as frame_f:
            frame_path = frame_f.name
        frame = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-frames:v", "1", "-q:v", "2", frame_path],
            capture_output=True, timeout=20,
        )
        if frame.returncode == 0:
            with open(frame_path, "rb") as f:
                frame_bytes = f.read()
            if frame_bytes:
                info["first_frame_sha256"] = hashlib.sha256(frame_bytes).hexdigest()
                info["first_frame_byte_length"] = len(frame_bytes)
    except Exception as exc:
        info["frame_extract_error"] = str(exc)
    return info


async def _collect_downloads(page, trigger, window_s: float = 25.0, quiet_s: float = 3.0) -> List[Dict[str, Any]]:
    """"Download all" can fire one OR several separate browser download
    events — never assume exactly one. Listens for every download event
    for a bounded window, stopping early once no NEW download has arrived
    for `quiet_s` seconds (so a single file doesn't wait the full budget)."""
    downloads: List[Any] = []

    def _on_download(dl):
        downloads.append(dl)

    page.on("download", _on_download)
    try:
        await trigger()
        elapsed = 0.0
        last_count = 0
        last_growth = 0.0
        step = 0.5
        while elapsed < window_s:
            await page.wait_for_timeout(int(step * 1000))
            elapsed += step
            if len(downloads) != last_count:
                last_count = len(downloads)
                last_growth = elapsed
            elif last_count > 0 and (elapsed - last_growth) >= quiet_s:
                break
    finally:
        page.remove_listener("download", _on_download)

    results = []
    for dl in downloads:
        try:
            path = await dl.path()
        except Exception as exc:
            results.append({"ok": False, "error": f"download.path() failed: {exc}"})
            continue
        if not path:
            results.append({"ok": False, "error": "download event fired but no file path available"})
            continue
        try:
            diag = await _read_file_diagnostics(path)
        except Exception as exc:
            diag = {"diagnostics_error": str(exc)}
        diag["ok"] = True
        try:
            diag["suggested_filename"] = dl.suggested_filename()
        except Exception:
            diag["suggested_filename"] = None
        results.append(diag)
    return results


_EVENT_CAPTURE_INSTALL_JS = """
() => {
  window.__ctxProbe = [];
  const record = (e) => {
    const t = e.target;
    window.__ctxProbe.push({
      type: e.type, button: e.button, x: e.clientX, y: e.clientY,
      targetTag: t && t.tagName, targetTestid: t && t.getAttribute && t.getAttribute('data-testid'),
      targetCls: t && t.className ? String(t.className).slice(0, 80) : null,
      defaultPrevented: e.defaultPrevented,
    });
  };
  if (!window.__ctxProbeInstalled) {
    document.addEventListener('mousedown', record, true);
    document.addEventListener('mouseup', record, true);
    document.addEventListener('contextmenu', record, true);
    document.addEventListener('auxclick', record, true);
    window.__ctxProbeInstalled = true;
  }
}
"""
_EVENT_CAPTURE_READ_JS = "() => { const ev = window.__ctxProbe || []; window.__ctxProbe = []; return ev; }"


async def _right_click_and_probe_download(page, relocate, menu_text_re: str = "download") -> Dict[str, Any]:
    """Right-click the element `relocate()` freshly resolves each attempt
    (never a locator captured before a prior attempt — a prior Escape/
    click can shift WhatsApp's virtualized message list, making a stale
    index miss) — trying its default center first, then a top-left-corner
    offset if that opens no menu at all. Also installs a raw
    mousedown/mouseup/contextmenu/auxclick capture on `document` before
    each attempt so a "no menu" result carries hard evidence of whether
    the browser-level events even reached WhatsApp's own handlers, not
    just another blind selector guess. Dumps whatever menu appears; if an
    item matching `menu_text_re` is visible, clicks it and collects every
    resulting download."""
    attempts = []
    # Fixed pixel offsets aren't safe here: the previous attempt's
    # event-capture evidence showed a {20,20} inset landing squarely on
    # the sender-name label WhatsApp renders above album/media content in
    # group chats (event.target had data-testid="author") — a header of
    # unknown height sits above the actual grid. Compute each attempt's
    # point from the element's OWN measured bounding box instead, biased
    # well below its top edge, so it lands on real media regardless of
    # header height.
    for label, frac in (("lower-center", (0.5, 0.7)), ("lower-left", (0.2, 0.85))):
        locator = await relocate()
        if locator is None:
            attempts.append({"position": label, "reason": "target message not found when re-resolving"})
            continue

        try:
            # bounding_box() does NOT auto-scroll (unlike locator.click());
            # without this the target can sit far below the viewport after
            # history-loading pushed it down, and the coordinates below
            # would silently click on nothing (2026-08-23 bug: y=3959 on an
            # ~800px-tall viewport, event target fell through to <html>).
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            box = await locator.bounding_box()
        except Exception as exc:
            box = None
        if not box:
            attempts.append({"position": label, "reason": "could not measure element bounding box"})
            continue
        click_x = box["x"] + box["width"] * frac[0]
        click_y = box["y"] + box["height"] * frac[1]

        # 2026-08-23: a preceding left-click was tried here as a way to
        # force WhatsApp to start fetching the media before opening the
        # menu (hypothesis: "Download all" needs it cached first, same as
        # the single-video case) — it was REMOVED after live testing
        # showed it actively breaks the menu (no menu at all afterward,
        # vs. the reliable 7-item menu without it). Reverted to the
        # right-click-only flow that reliably reproduces the user's
        # manually-observed menu; "Download all" itself is still missing
        # from it — see the session write-up for the current theory.
        try:
            await _evaluate(page, _EVENT_CAPTURE_INSTALL_JS)
        except Exception:
            pass

        try:
            await page.mouse.move(click_x, click_y)
            await page.mouse.click(click_x, click_y, button="right")
        except Exception as exc:
            attempts.append({"position": label, "reason": f"right-click failed: {exc}", "click_point": [click_x, click_y], "box": box})
            continue

        await page.wait_for_timeout(600)
        try:
            event_log = await _evaluate(page, _EVENT_CAPTURE_READ_JS)
        except Exception:
            event_log = None
        try:
            menu_dump = await _evaluate(page, _CONTEXT_MENU_DUMP_JS)
        except Exception:
            menu_dump = None
        body_snapshot = None
        if not menu_dump:
            try:
                body_snapshot = await _evaluate(page, _BODY_SNAPSHOT_JS)
            except Exception:
                body_snapshot = None
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            attempts.append({
                "position": label, "reason": "no context menu appeared", "click_point": [click_x, click_y],
                "menu_dump": menu_dump, "body_snapshot": body_snapshot, "event_log": event_log,
            })
            continue

        item = page.locator(
            f'[role="menu"] :text-matches("{menu_text_re}", "i"), '
            f'[role="menuitem"]:has-text("Download"), '
            f'li:has-text("Download"), '
            f'div[role="button"]:has-text("Download")'
        ).first
        try:
            visible = await item.is_visible(timeout=3000)
        except Exception:
            visible = False
        if not visible:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            attempts.append({"position": label, "reason": "menu appeared but no matching 'Download' item found", "menu_dump": menu_dump})
            continue

        try:
            item_text = (await item.inner_text()).strip()
        except Exception:
            item_text = None

        async def _click_item():
            await item.click(timeout=5000)

        downloads = await _collect_downloads(page, _click_item)
        return {
            "ok": bool(downloads) and any(d.get("ok") for d in downloads),
            "position_used": label, "menu_dump": menu_dump, "item_text_clicked": item_text,
            "event_log": event_log, "downloads": downloads,
        }

    return {"ok": False, "reason": "no 'Download' action reachable via right-click at any tried position", "attempts": attempts}


async def _run_download_probe(session, page, req: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnostic-only entry point: proves (or disproves) the native
    Download mechanism against one real message. Never uploads, never
    triggers a report into the real casting-agent WhatsApp group (see
    services/media_assignment_worker.py's `mode == "download_probe"`
    short-circuit). `probe_type`:
      - "tile_viewer" (default when a tile_index/expected_tile_hash is
        given): reproduces the manual open-tile -> viewer -> buffer ->
        three-dot -> Download workflow for one specific tile.
      - "album_menu" (default otherwise): right-click the album/message
        itself, looking for "Download all" / "Download"."""
    group_name = req["group_name"]
    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"results": [{"ok": False, "error": f"Could not open WhatsApp group {group_name!r} (status={status})"}]}

    probe_type = req.get("probe_type") or ("tile_viewer" if req.get("tile_index") is not None else "album_menu")
    _no_message_id_needed = {"album_discovery", "raw_tail_ids", "session_sync_check", "full_message_inventory", "group_participants_check", "attach_button_diagnostic", "attach_menu_after_click_diagnostic", "plus_rounded_locations_diagnostic", "attach_mechanism_full_diagnostic", "attach_photos_videos_filechooser_diagnostic", "attach_real_file_diagnostic", "attach_interceptor_diagnostic", "destination_media_inventory_diagnostic", "caption_field_diagnostic", "destination_deep_investigation_diagnostic", "session_identity_and_sync_boundary_diagnostic", "destination_incoming_message_diagnostic", "send_button_preview_diagnostic", "video_tile_stability_diagnostic", "video_tile_reresolution_live_diagnostic", "forward_readiness_diagnostic"}
    data_id = req.get("probe_message_id") if probe_type in _no_message_id_needed else req["probe_message_id"]

    session_identity = {
        "own_phone_number": getattr(session, "own_phone_number", None),
        "session_id": getattr(session, "session_id", None),
        "generation": getattr(session, "generation", None),
    }

    if probe_type == "attach_button_diagnostic":
        # Diagnostic-only (2026-08-24): a real disposable SEND E2E had the
        # media download succeed for all 6 items but every send failed at
        # `page.click(SEL["attach_btn"])` (data-testid="attach-menu-plus")
        # with a clean 30s "waiting for locator" timeout — never found at
        # all. Read-only: after the chat is open/ready, dump every
        # data-testid on screen that looks attach/plus/clip-related, plus
        # whether the exact expected testid exists anywhere in the DOM
        # (even off-screen/hidden), to tell a genuinely stale selector
        # apart from a timing/visibility issue. Never clicks anything.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)
        _js = """
            () => {
              const all = Array.from(document.querySelectorAll('[data-testid]'));
              const matches = all
                .filter(el => /attach|plus|clip|footer/i.test(el.getAttribute('data-testid') || ''))
                .map(el => ({
                  testid: el.getAttribute('data-testid'),
                  tag: el.tagName,
                  visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                  aria_label: el.getAttribute('aria-label'),
                }));
              return {
                total_testid_elements: all.length,
                attach_menu_plus_exists: !!document.querySelector('[data-testid="attach-menu-plus"]'),
                matches,
              };
            }
        """
        try:
            dom_result = await _evaluate(page, _js, [])
        except Exception as exc:
            dom_result = {"error": str(exc)}
        return {"results": [{
            "ok": True, "group_name": group_name,
            "chat_ready_error": ready_error,
            "dom": dom_result,
        }], "session_identity": session_identity}

    if probe_type == "send_button_preview_diagnostic":
        # Diagnostic-only (2026-08-24) — investigates the media-preview
        # screen's ACTUAL Send control before any verification-behavior
        # change is implemented. sender.py's existing SEND_BUTTON_SELECTORS
        # chain was already observed to return count=0 for every entry
        # during a real SEND, falling through to a raw Enter keypress —
        # unsafe, since Enter clearing the composer is not proof media was
        # submitted. Attaches a real disposable local JPEG through the
        # ALREADY-PROVEN mechanism (plus-rounded -> Photos & videos ->
        # expect_file_chooser -> set_files), stops BEFORE any Send
        # interaction, inventories every visible button-like element on
        # the preview screen (aria-label/testid/data-icon/role/text/
        # ancestor chain), probes each existing SEND_BUTTON_SELECTORS
        # entry's count/visibility (without clicking), and — if exactly
        # one strong candidate is found — captures elementsFromPoint at
        # its center to confirm it's the unique, real target. Never
        # clicks Send; cancels via Escape afterward.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)

        _tiny_jpeg_b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8Q"
            "EBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
            "EBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
            "FQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k="
        )
        temp_path = None
        attach_error = None
        button_inventory: Any = None
        selector_probe_results: Any = None
        candidate_efp: Any = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(b64mod.b64decode(_tiny_jpeg_b64))
            tmp.close()
            temp_path = tmp.name

            await page.click(sender.SEL["attach_btn"], timeout=10_000)
            await asyncio.sleep(0.5)
            async with page.expect_file_chooser(timeout=10_000) as fc_info:
                await page.click('button[aria-label="Photos & videos"]', timeout=10_000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(temp_path)
            await asyncio.sleep(2.0)

            # Probe each existing SEND_BUTTON_SELECTORS entry — count/visible
            # only, never click (mirrors _find_and_click_send's own probing
            # logic exactly, minus the click).
            selector_probe_results = []
            for sel in sender.SEND_BUTTON_SELECTORS:
                try:
                    loc = page.locator(sel)
                    count = await loc.count()
                    visible = await loc.first.is_visible() if count else False
                    selector_probe_results.append({"selector": sel, "count": count, "visible": visible})
                except Exception as exc:
                    selector_probe_results.append({"selector": sel, "error": str(exc)})

            _inventory_js = """
                () => {
                  function describe(el) {
                    const rect = el.getBoundingClientRect();
                    let anc = el.parentElement, chain = [];
                    for (let d = 0; d < 6 && anc; d++) {
                      chain.push({
                        testid: anc.getAttribute && anc.getAttribute('data-testid'),
                        role: anc.getAttribute && anc.getAttribute('role'),
                        tag: anc.tagName,
                      });
                      anc = anc.parentElement;
                    }
                    return {
                      tag: el.tagName,
                      testid: el.getAttribute('data-testid'),
                      aria_label: el.getAttribute('aria-label'),
                      data_icon: el.getAttribute('data-icon'),
                      role: el.getAttribute('role'),
                      text: (el.innerText || '').slice(0, 40),
                      rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                      ancestor_chain: chain,
                    };
                  }
                  const sel = 'button, [role="button"], span[data-icon], [data-testid]';
                  const all = Array.from(document.querySelectorAll(sel))
                    .filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const sendLike = all.filter(el => {
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    const icon = (el.getAttribute('data-icon') || '').toLowerCase();
                    const testid = (el.getAttribute('data-testid') || '').toLowerCase();
                    return aria.includes('send') || icon.includes('send') || testid.includes('send');
                  });
                  return {
                    total_visible_button_like: all.length,
                    send_like_candidates: sendLike.map(describe),
                  };
                }
            """
            button_inventory = await _evaluate(page, _inventory_js, [])

            candidates = (button_inventory or {}).get("send_like_candidates") or []
            if len(candidates) == 1:
                c = candidates[0]
                rect = c.get("rect") or {}
                cx = rect.get("x", 0) + rect.get("w", 0) // 2
                cy = rect.get("y", 0) + rect.get("h", 0) // 2
                efp_js = """
                    ([cx, cy]) => {
                      function describe(el) {
                        return {
                          tag: el.tagName, testid: el.getAttribute('data-testid'),
                          aria_label: el.getAttribute('aria-label'), data_icon: el.getAttribute('data-icon'),
                        };
                      }
                      return document.elementsFromPoint(cx, cy).slice(0, 6).map(describe);
                    }
                """
                candidate_efp = await _evaluate(page, efp_js, [cx, cy])
        except Exception as exc:
            attach_error = str(exc)
        finally:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
            except Exception:
                pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        return {"results": [{
            "ok": attach_error is None, "group_name": group_name,
            "chat_ready_error": ready_error, "attach_error": attach_error,
            "selector_probe_results": selector_probe_results,
            "button_inventory": button_inventory,
            "candidate_elements_from_point": candidate_efp,
        }], "session_identity": session_identity}

    if probe_type == "destination_incoming_message_diagnostic":
        # Diagnostic-only (2026-08-24) — classification C1/C2/C3/C4 test.
        # The user manually sends ONE disposable plain-text message from
        # THEIR OWN account (not the Gunwanti worker) into the destination
        # group, to determine whether the group's live sync is healthy for
        # a genuinely INCOMING message even though the worker's own 6
        # historical outgoing media messages remain invisible. Never sends
        # anything, never mutates media_sends/casting_pipeline, never
        # relinks/logs out. Checks BEFORE any reload first, then does one
        # controlled forced reload (reusing the same bounded settle-retry
        # already proven necessary — see destination_media_inventory_diagnostic)
        # and checks again.
        marker_text = req.get("marker_text") or "DESTINATION SYNC TEST 7f31c9 — ignore"

        def _tail_search_js():
            return """
                ([sel, marker]) => {
                  const els = Array.from(document.querySelectorAll(sel));
                  const found = els.find(el => (el.innerText || '').includes(marker));
                  if (!found) {
                    return {
                      found: false,
                      total_rendered: els.length,
                      last_5_ids: els.slice(-5).map(el => el.getAttribute('data-id')),
                    };
                  }
                  const ct = found.querySelector('[data-pre-plain-text]');
                  const html = found.outerHTML;
                  const testidCounts = {};
                  (html.match(/data-testid="[^"]+"/g) || []).forEach(m => {
                    testidCounts[m] = (testidCounts[m] || 0) + 1;
                  });
                  return {
                    found: true,
                    total_rendered: els.length,
                    data_id: found.getAttribute('data-id'),
                    pre_plain_text: ct ? ct.getAttribute('data-pre-plain-text') : null,
                    inner_text: (found.innerText || '').slice(0, 200),
                    html_len: html.length,
                    testid_counts: testidCounts,
                    outer_html_snippet: html.slice(0, 1500),
                  };
                }
            """

        result: Dict[str, Any] = {"marker_text": marker_text}

        # --- Step A: check BEFORE any reload -------------------------------
        try:
            scope = await sender._resolve_scope(page)
            full_sel = f"{scope} [data-testid^='conv-msg-']"
            result["before_reload"] = await _evaluate(page, _tail_search_js(), [full_sel, marker_text])
        except Exception as exc:
            result["before_reload_error"] = str(exc)

        # --- Step B: forced reload with bounded settle-retry ---------------
        reload_error = None
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
        except Exception as exc:
            reload_error = str(exc)
        result["reload_error"] = reload_error

        status_after_reload = "SEARCH_FAILED"
        reload_settle_attempts = []
        for attempt, extra_wait_ms in enumerate((0, 4000, 6000, 8000)):
            if extra_wait_ms:
                await page.wait_for_timeout(extra_wait_ms)
            status_after_reload = await sender._open_group_chat(page, group_name)
            reload_settle_attempts.append({"attempt": attempt, "extra_wait_ms": extra_wait_ms, "status": status_after_reload})
            if status_after_reload == "OPENED":
                break
        result["reload_settle_attempts"] = reload_settle_attempts
        result["status_after_reload"] = status_after_reload

        if status_after_reload == "OPENED":
            try:
                await sender._wait_for_chat_ready(page)
                scope = await sender._resolve_scope(page)
                full_sel = f"{scope} [data-testid^='conv-msg-']"
                result["after_reload"] = await _evaluate(page, _tail_search_js(), [full_sel, marker_text])
            except Exception as exc:
                result["after_reload_error"] = str(exc)

        marker_ever_found = bool(
            (result.get("before_reload") or {}).get("found")
            or (result.get("after_reload") or {}).get("found")
        )

        # --- Step C: additional read-only checks, only if the marker was
        # actually found (per the user's instructions) --------------------
        additional_checks: Dict[str, Any] = {}
        if marker_ever_found:
            # Media panel re-check.
            try:
                header_sel = None
                for sel in sender.GROUP_INFO_TRIGGER_SELECTORS:
                    if await page.locator(sel).count():
                        header_sel = sel
                        break
                if header_sel:
                    await page.click(header_sel, timeout=5_000)
                    await asyncio.sleep(0.6)
                    panel_sel = await sender._find_populated_panel(page, sender.GROUP_INFO_PANEL_SELECTORS)
                    if panel_sel:
                        try:
                            await page.click('text="Media, links and docs"', timeout=5_000)
                            await asyncio.sleep(1.0)
                            media_dump = await _evaluate(page, """
                                () => {
                                  const imgs = Array.from(document.querySelectorAll('img[src^="blob:"]'));
                                  return {blob_image_count: imgs.length, total_testid_elements: document.querySelectorAll('[data-testid]').length};
                                }
                            """, [])
                            additional_checks["media_panel_recheck"] = media_dump
                        except Exception as exc:
                            additional_checks["media_panel_recheck_error"] = str(exc)
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Escape")
            except Exception as exc:
                additional_checks["media_panel_error"] = str(exc)

            # In-chat search for a known caption fragment and a known timestamp.
            for label, query in (
                ("search_by_caption", "Google Test Take 1"),
                ("search_by_timestamp", "5:32 PM"),
            ):
                try:
                    search_icon_sel = None
                    for sel in ('[data-testid="search"]', 'button[aria-label="Search"]', '[aria-label="Search"]', 'span[data-icon="search"]'):
                        if await page.locator(sel).count():
                            search_icon_sel = sel
                            break
                    if not search_icon_sel:
                        additional_checks[label] = {"ok": False, "reason": "no in-chat search trigger found"}
                        continue
                    await page.click(search_icon_sel, timeout=5_000)
                    await asyncio.sleep(0.5)
                    await page.keyboard.type(query)
                    await asyncio.sleep(1.2)
                    search_result_js = """
                        () => {
                          const text = document.body.innerText || '';
                          return {
                            no_results_shown: /no messages found|no results/i.test(text),
                            visible_result_count_hint: (text.match(/result/gi) || []).length,
                          };
                        }
                    """
                    additional_checks[label] = await _evaluate(page, search_result_js, [])
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Escape")
                except Exception as exc:
                    additional_checks[label] = {"ok": False, "error": str(exc)}
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass

        result["marker_ever_found"] = marker_ever_found
        result["additional_checks"] = additional_checks

        return {"results": [result], "session_identity": session_identity}

    if probe_type == "session_identity_and_sync_boundary_diagnostic":
        # Diagnostic-only (2026-08-24) — read-only session-identity and
        # message-sync-boundary investigation. Never re-links, logs out, or
        # reloads the WhatsApp session. Identity is read via the SAME
        # existing, already-shipped session.py mechanism
        # (_read_own_phone_number's exact selectors/logic) already used for
        # the admin status panel — replicated inline here (not called
        # directly) because that method acquires session.page_lock
        # internally, which is already held for the whole duration of this
        # claimed request; calling it here would deadlock. Never exposes
        # credentials/tokens/cookies — only whatever phone-shaped text the
        # account's own profile panel already displays.
        #
        # Sync-boundary tests: (a) re-reads the SOURCE group's own tail
        # (Talentgram MEDIA SPIKE TEST) to prove incoming/other-participant
        # message visibility still works; (b) sends ONE disposable plain-
        # text diagnostic message into that SAME source group (never the
        # destination, never touching media_sends/casting_pipeline) and
        # immediately re-scans for it, to directly test whether the worker
        # can see its OWN outgoing messages anywhere at all; (c) re-checks
        # the destination group once more without a reload; (d) peeks
        # read-only at one other pre-existing real group's history; (e)
        # scans visible text for known WhatsApp sync/connection indicators.
        identity: Dict[str, Any] = {"session_id": getattr(session, "session_id", None), "generation": getattr(session, "generation", None)}
        try:
            trigger = None
            for sel in session_module.PROFILE_TRIGGER_SELECTORS:
                loc = page.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    trigger = loc.first
                    break
            if trigger is None:
                identity["own_phone_number"] = None
                identity["phone_read_note"] = "no profile trigger matched any PROFILE_TRIGGER_SELECTORS entry"
            else:
                await trigger.click(timeout=3000)
                panel = None
                for sel in session_module.PROFILE_PANEL_SELECTORS:
                    loc = page.locator(sel)
                    try:
                        await loc.first.wait_for(state="visible", timeout=2000)
                        panel = loc.first
                        break
                    except Exception:
                        continue
                phone = None
                if panel is not None:
                    panel_text = await panel.inner_text()
                    m = session_module._PHONE_LIKE_RE.search(panel_text)
                    if m:
                        phone = m.group(0).strip()
                await page.keyboard.press("Escape")
                identity["own_phone_number"] = phone
                identity["phone_read_note"] = "read from profile panel" if phone else "panel opened but no phone-shaped text found"
        except Exception as exc:
            identity["own_phone_number"] = None
            identity["phone_read_note"] = f"read failed: {exc}"
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        def _tail_snapshot_js():
            return """
                ([sel]) => {
                  const els = Array.from(document.querySelectorAll(sel));
                  return {
                    total_rendered: els.length,
                    last_5: els.slice(-5).map(el => ({
                      data_id: el.getAttribute('data-id'),
                      inner_text: (el.innerText || '').slice(0, 100),
                      img_count: el.querySelectorAll('img').length,
                    })),
                  };
                }
            """

        source_group = req.get("source_group_name") or "Talentgram MEDIA SPIKE TEST"
        dest_group = group_name
        third_group = req.get("third_group_name") or "Talentgram CRM"
        sync_investigation: Dict[str, Any] = {}

        # (a) source group tail — proves incoming/other-participant visibility.
        try:
            status = await sender._open_group_chat(page, source_group)
            if status != "OPENED":
                sync_investigation["source_tail_error"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                scope = await sender._resolve_scope(page)
                full_sel = f"{scope} [data-testid^='conv-msg-']"
                sync_investigation["source_tail_before_test_send"] = await _evaluate(page, _tail_snapshot_js(), [full_sel])
        except Exception as exc:
            sync_investigation["source_tail_error"] = str(exc)

        # (b) disposable self-visibility test: send ONE plain-text message
        # into the SOURCE group (never the destination), then immediately
        # re-scan for it.
        marker = f"SYNC DIAGNOSTIC TEST {uuid.uuid4().hex[:8]} — worker self-visibility check, ignore"
        try:
            send_result = await sender.send_whatsapp_message(
                page=page, destination_type="group", destination=source_group,
                message_body=marker,
            )
            sync_investigation["self_test_send_state"] = send_result.get("state")
        except Exception as exc:
            sync_investigation["self_test_send_error"] = str(exc)

        try:
            status = await sender._open_group_chat(page, source_group)
            if status != "OPENED":
                sync_investigation["source_tail_after_test_send_error"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                scope = await sender._resolve_scope(page)
                full_sel = f"{scope} [data-testid^='conv-msg-']"
                after = await _evaluate(page, _tail_snapshot_js(), [full_sel])
                sync_investigation["source_tail_after_test_send"] = after
                sync_investigation["self_test_message_visible"] = any(
                    marker in (m.get("inner_text") or "") for m in (after.get("last_5") or [])
                )
        except Exception as exc:
            sync_investigation["source_tail_after_test_send_error"] = str(exc)

        # (c) destination group, one more time, no reload.
        try:
            status = await sender._open_group_chat(page, dest_group)
            if status != "OPENED":
                sync_investigation["dest_tail_error"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                scope = await sender._resolve_scope(page)
                full_sel = f"{scope} [data-testid^='conv-msg-']"
                sync_investigation["dest_tail"] = await _evaluate(page, _tail_snapshot_js(), [full_sel])
        except Exception as exc:
            sync_investigation["dest_tail_error"] = str(exc)

        # (d) a third, pre-existing, unrelated real group — read-only peek.
        try:
            status = await sender._open_group_chat(page, third_group)
            if status != "OPENED":
                sync_investigation["third_group_error"] = f"status={status} (group={third_group!r})"
            else:
                await sender._wait_for_chat_ready(page)
                scope = await sender._resolve_scope(page)
                full_sel = f"{scope} [data-testid^='conv-msg-']"
                sync_investigation["third_group_tail"] = await _evaluate(page, _tail_snapshot_js(), [full_sel])
                sync_investigation["third_group_name"] = third_group
        except Exception as exc:
            sync_investigation["third_group_error"] = str(exc)

        # (e) visible sync/connection indicator scan (whole-page text).
        try:
            indicator_js = """
                () => {
                  const text = (document.body.innerText || '').toLowerCase();
                  const phrases = [
                    'connecting', 'reconnecting', 'waiting for this message',
                    'syncing', 'sync your', 'trying to reach phone',
                    'phone was not detected', 'this message was not sent',
                    'trying to connect', 'offline',
                  ];
                  return phrases.filter(p => text.includes(p));
                }
            """
            sync_investigation["visible_sync_indicators"] = await _evaluate(page, indicator_js, [])
        except Exception as exc:
            sync_investigation["indicator_scan_error"] = str(exc)

        return {"results": [{
            "ok": True, "identity": identity, "sync_investigation": sync_investigation,
        }], "session_identity": session_identity}

    if probe_type == "destination_deep_investigation_diagnostic":
        # Diagnostic-only (2026-08-24) — the user provided direct screenshot
        # evidence (WhatsApp's own conversation view AND its Group Info
        # "Media, links and docs" panel, both showing 6 real media items
        # sent by "Gunwanti Talentgram Team" into this exact group) proving
        # delivery genuinely happened, while every prior worker-side read
        # of the MAIN conversation timeline — including one with a proven-
        # fresh forced reload — showed zero media. This investigates why,
        # without sending anything: (A) repeatedly scrolls the message
        # container to the true bottom (not just once, like raw_tail_ids —
        # loops until scrollHeight stops growing) re-checking the rendered
        # message count after each scroll, since WhatsApp's virtualized
        # list may need more than one scroll pass to actually load the
        # tail; (B) opens the SAME Group Info -> "Media, links and docs"
        # panel the screenshot shows, via the exact click path a real user
        # would use, to see if that separately-loaded view is in sync even
        # when the main timeline isn't. Never sends, never mutates.
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"

        scroll_js = """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              let container = els[0] || null, maxOverflow = 0;
              let node = els[0] ? els[0].parentElement : null;
              for (let d = 0; d < 8 && node; d++) {
                const overflow = node.scrollHeight - node.clientHeight;
                if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
                node = node.parentElement;
              }
              return {
                total_rendered: els.length,
                scrollTop: container ? container.scrollTop : null,
                scrollHeight: container ? container.scrollHeight : null,
                clientHeight: container ? container.clientHeight : null,
                has_container: !!container,
              };
            }
        """
        scroll_to_bottom_js = """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              let container = els[0] || null, maxOverflow = 0;
              let node = els[0] ? els[0].parentElement : null;
              for (let d = 0; d < 8 && node; d++) {
                const overflow = node.scrollHeight - node.clientHeight;
                if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
                node = node.parentElement;
              }
              if (container) container.scrollTop = container.scrollHeight;
              return !!container;
            }
        """

        scroll_snapshots = []
        try:
            snap = await _evaluate(page, scroll_js, [full_sel])
            scroll_snapshots.append({"pass": 0, **snap})
            prev_height = snap.get("scrollHeight")
            for i in range(1, 8):
                did_scroll = await _evaluate(page, scroll_to_bottom_js, [full_sel])
                if not did_scroll:
                    break
                await page.wait_for_timeout(1200)
                snap = await _evaluate(page, scroll_js, [full_sel])
                scroll_snapshots.append({"pass": i, **snap})
                if snap.get("scrollHeight") == prev_height and snap.get("total_rendered") == scroll_snapshots[-2]["total_rendered"]:
                    break
                prev_height = snap.get("scrollHeight")
        except Exception as exc:
            scroll_snapshots.append({"error": str(exc)})

        # Final full inventory after scroll convergence, same rich capture
        # as destination_media_inventory_diagnostic.
        _inventory_js = """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              return {
                total_rendered: els.length,
                messages: els.map((el, i) => ({
                  index: i,
                  data_id: el.getAttribute('data-id'),
                  inner_text: (el.innerText || '').slice(0, 200),
                  img_count: el.querySelectorAll('img').length,
                  video_count: el.querySelectorAll('video').length,
                  has_media_album: !!el.querySelector('[data-testid="media-album"]'),
                })),
              };
            }
        """
        try:
            final_inventory = await _evaluate(page, _inventory_js, [full_sel])
        except Exception as exc:
            final_inventory = {"error": str(exc)}

        # --- Media panel (Group Info -> "Media, links and docs") ---------
        media_panel: Dict[str, Any] = {"opened": False}
        try:
            header_sel = None
            for sel in sender.GROUP_INFO_TRIGGER_SELECTORS:
                if await page.locator(sel).count():
                    header_sel = sel
                    break
            if header_sel:
                await page.click(header_sel, timeout=5_000)
                await asyncio.sleep(0.6)
                panel_sel = await sender._find_populated_panel(page, sender.GROUP_INFO_PANEL_SELECTORS)
                if panel_sel:
                    media_row_js = """
                        (panelSel) => {
                          const panel = document.querySelector(panelSel);
                          if (!panel) return null;
                          const rows = Array.from(panel.querySelectorAll('div, span'));
                          const row = rows.find(el => (el.innerText || '').trim() === 'Media, links and docs');
                          if (!row) return null;
                          let clickable = row;
                          for (let d = 0; d < 5 && clickable; d++) {
                            if (clickable.getAttribute('role') === 'button' || clickable.tagName === 'BUTTON') break;
                            clickable = clickable.parentElement;
                          }
                          return true;
                        }
                    """
                    row_found = await _evaluate(page, media_row_js, [panel_sel])
                    if row_found:
                        try:
                            await page.click(f'text="Media, links and docs"', timeout=5_000)
                            await asyncio.sleep(1.0)
                            media_dump_js = """
                                () => {
                                  const imgs = Array.from(document.querySelectorAll('img[src^="blob:"]'));
                                  return {
                                    blob_image_count: imgs.length,
                                    img_srcs: imgs.slice(0, 20).map(im => (im.src || '').slice(0, 30)),
                                    total_testid_elements: document.querySelectorAll('[data-testid]').length,
                                    url: location.href,
                                  };
                                }
                            """
                            media_dump = await _evaluate(page, media_dump_js, [])
                            media_panel = {"opened": True, "row_found": True, "dump": media_dump}
                        except Exception as exc:
                            media_panel = {"opened": False, "row_found": True, "click_error": str(exc)}
                    else:
                        media_panel = {"opened": False, "row_found": False, "panel_sel": panel_sel}
                else:
                    media_panel = {"opened": False, "reason": "group info panel never populated"}
            else:
                media_panel = {"opened": False, "reason": "no group info trigger found"}
        except Exception as exc:
            media_panel = {"opened": False, "error": str(exc)}
        finally:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Escape")
            except Exception:
                pass

        return {"results": [{
            "ok": True, "group_name": group_name,
            "scroll_snapshots": scroll_snapshots,
            "final_inventory": final_inventory,
            "media_panel": media_panel,
        }], "session_identity": session_identity}

    if probe_type == "forward_readiness_diagnostic":
        # Diagnostic-only (2026-08-24) — investigates a proposed SEND
        # architecture change: native WhatsApp Forward instead of
        # download+re-upload. NEVER clicks Download or Forward, never
        # sends anything. Opens each target's viewer using the already-
        # proven, already-fixed re-resolution logic, then repeatedly (up
        # to a bounded timeout) captures video/image readiness state AND
        # the full viewer button inventory — watching specifically for a
        # control whose aria-label/data-icon/testid/svg-title matches
        # /forward/i, and whether it tracks the same buffering signal
        # _wait_for_video_readiness already established for Download.
        _GENERIC_VIEWER_BUTTONS_JS = """
            () => {
              let anchor = document.querySelector('video');
              let anchorKind = 'video';
              if (!anchor) {
                const imgs = Array.from(document.querySelectorAll('img[src^="blob:"]'))
                  .filter(im => im.offsetWidth > 300 && im.offsetHeight > 300)
                  .sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight));
                anchor = imgs[0] || null;
                anchorKind = 'image';
              }
              if (!anchor) return {rootFound: false, buttons: [], reason: 'no video or large opened image found'};
              let root = anchor;
              while (root.parentElement && root.parentElement !== document.body) root = root.parentElement;
              if (!root.parentElement) return {rootFound: false, buttons: [], reason: 'anchor not attached under body'};
              const btns = Array.from(root.querySelectorAll('button, [role="button"]'));
              return {
                rootFound: true, anchorKind: anchorKind,
                buttons: btns.map(b => {
                  const r = b.getBoundingClientRect();
                  const svgTitle = b.querySelector('svg title');
                  return {
                    ariaLabel: b.getAttribute('aria-label'), dataIcon: b.getAttribute('data-icon'),
                    testid: b.getAttribute('data-testid'), svgTitle: svgTitle ? svgTitle.textContent : null,
                    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                    visible: !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length),
                  };
                }),
              };
            }
        """

        def _json_safe(obj):
            # A <video> element's own duration/currentTime are legitimately
            # NaN/Infinity before metadata loads (HTML5 spec) — not valid
            # JSON, and this diagnostic transports raw video state back
            # over HTTP (unlike _wait_for_video_readiness's own use of the
            # same _VIDEO_STATE_JS, which only ever consumes it in-process
            # and never serializes it). Recursively replaces any non-finite
            # float with None; everything else passes through unchanged.
            if isinstance(obj, float) and not math.isfinite(obj):
                return None
            if isinstance(obj, dict):
                return {k: _json_safe(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_json_safe(v) for v in obj]
            return obj

        def _classify_buttons(dump):
            if not dump or not dump.get("rootFound"):
                return {"forward": None, "download": None}
            def _match(pattern):
                for b in dump.get("buttons") or []:
                    hay = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("testid"), b.get("svgTitle")])).lower()
                    if re.search(pattern, hay):
                        return b
                return None
            return {"forward": _match(r"forward"), "download": _match(r"download")}

        def _is_onscreen(button):
            # The very first checkpoint (2026-08-24 finding) found Forward
            # present in the DOM immediately for both videos, but at wildly
            # off-screen y coordinates (-1426, -3451) — the viewer chrome's
            # own slide-in animation, not a readiness gate. A button is
            # only counted as genuinely available once its rect sits within
            # a sane on-screen range.
            if not button:
                return False
            rect = button.get("rect") or [0, 0, 0, 0]
            y = rect[1] if len(rect) > 1 else 0
            return -50 <= y <= 3000

        async def _readiness_checkpoints(page, max_checks=20, interval_s=1.0, stable_needed=2):
            checkpoints = []
            stable_run = 0
            for i in range(max_checks):
                try:
                    video_state = _json_safe(await _evaluate(page, _VIDEO_STATE_JS))
                except Exception as exc:
                    video_state = {"error": str(exc)}
                try:
                    button_dump = await _evaluate(page, _GENERIC_VIEWER_BUTTONS_JS)
                except Exception as exc:
                    button_dump = {"error": str(exc)}
                classified = _classify_buttons(button_dump)
                forward_onscreen = _is_onscreen(classified["forward"])
                checkpoints.append({
                    "t_s": round(i * interval_s, 1),
                    "video_state": video_state,
                    "forward_present": classified["forward"] is not None,
                    "forward_onscreen": forward_onscreen,
                    "forward_button": classified["forward"],
                    "download_present": classified["download"] is not None,
                    "download_button": classified["download"],
                    "total_buttons": len((button_dump or {}).get("buttons") or []),
                })
                stable_run = stable_run + 1 if forward_onscreen else 0
                if stable_run >= stable_needed:
                    break
                await page.wait_for_timeout(int(interval_s * 1000))
            return checkpoints

        async def _open_and_watch(target_id: str, tile_index: int, is_photo: bool = False):
            entry: Dict[str, Any] = {"target_id": target_id, "tile_index": tile_index, "is_photo": is_photo}
            idx = await _find_message_index_by_data_id(page, group_name, target_id)
            entry["initial_index"] = idx
            if idx is None:
                entry["error"] = "message not found in current window"
                return entry
            scope = await sender._resolve_scope(page)
            full_sel = f"{scope} [data-testid^='conv-msg-']"

            if not is_photo:
                tile, _ = await _resolve_video_tile_locator(page, group_name, target_id, tile_index)
                if tile is None:
                    entry["error"] = "video tile re-resolution failed"
                    return entry
                try:
                    await tile.scroll_into_view_if_needed(timeout=5000)
                    await tile.click(timeout=10000)
                except Exception as exc:
                    entry["error"] = f"tile click failed: {exc}"
                    return entry
                mounted = False
                for _ in range(30):
                    try:
                        if await page.locator("video").count() > 0:
                            mounted = True
                            break
                    except Exception:
                        pass
                    await page.wait_for_timeout(500)
                entry["mounted"] = mounted
                if not mounted:
                    entry["error"] = "no <video> mounted within 15s of click"
                    return entry
            else:
                # Single-photo click target: the message's own largest <img>
                # (no video-content/image-content testid exists for a
                # non-album photo — confirmed via prior diagnostic).
                message = page.locator(full_sel).nth(idx)
                try:
                    img_click_js = """
                        (el) => {
                          const imgs = Array.from(el.querySelectorAll('img'));
                          const big = imgs.filter(im => im.offsetWidth > 20).sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight))[0];
                          return !!big;
                        }
                    """
                    has_img = await message.evaluate(img_click_js)
                    entry["photo_click_target_found"] = has_img
                    if not has_img:
                        entry["error"] = "no clickable <img> found on photo message"
                        return entry
                    photo_loc = message.locator("img").first
                    await photo_loc.scroll_into_view_if_needed(timeout=5000)
                    await photo_loc.click(timeout=10000)
                except Exception as exc:
                    entry["error"] = f"photo click failed: {exc}"
                    return entry
                await page.wait_for_timeout(1000)

            entry["checkpoints"] = await _readiness_checkpoints(page)
            try:
                viewer_buttons_for_close = await _evaluate(page, _GENERIC_VIEWER_BUTTONS_JS)
            except Exception:
                viewer_buttons_for_close = {"buttons": []}
            entry["viewer_closed"] = await _close_viewer(page, viewer_buttons_for_close)
            return entry

        results: List[Dict[str, Any]] = []

        # A: the exact failed video (source_message_id given).
        target_id = req.get("probe_message_id") or "3B07252BFE7BC81FB956"
        results.append(await _open_and_watch(target_id, req.get("tile_index", 0), is_photo=False))

        # B: a known-good, previously-downloaded video, if provided.
        known_good_video_id = req.get("known_good_video_id")
        if known_good_video_id:
            results.append(await _open_and_watch(known_good_video_id, req.get("known_good_video_tile_index", 0), is_photo=False))

        # C: a known-good photo, if provided.
        known_good_photo_id = req.get("known_good_photo_id")
        if known_good_photo_id:
            results.append(await _open_and_watch(known_good_photo_id, 0, is_photo=True))

        return {"results": results, "session_identity": session_identity}

    if probe_type == "video_tile_reresolution_live_diagnostic":
        # Diagnostic-only (2026-08-24) — post-deploy verification of the
        # video-tile re-resolution fix against the EXACT real message that
        # failed twice (SendTest2 Intro, source_message_id
        # 3B07252BFE7BC81FB956). This calls the REAL, FIXED
        # _open_tile_viewer_and_download with group_name/source_message_id
        # (exercising live re-resolution + bounded retry exactly as
        # _send_one_target now does), which does open the native WhatsApp
        # viewer and perform a real local browser download — the same
        # locally-scoped, non-mutating mechanism already used safely by
        # every other download_probe in this file (nothing is sent/posted
        # to anyone; no message is created; no media_sends/casting_pipeline
        # write occurs here). Reports current index/data-id, the fix's own
        # outcome, downloaded byte count + sha256, and the mark's known
        # source_thumbnail_hash for reference.
        target_id = req.get("probe_message_id") or "3B07252BFE7BC81FB956"
        expected_thumbnail_hash = req.get("expected_thumbnail_hash")

        idx = await _find_message_index_by_data_id(page, group_name, target_id)
        result: Dict[str, Any] = {
            "target_id": target_id, "current_index": idx,
            "expected_thumbnail_hash": expected_thumbnail_hash,
        }
        if idx is None:
            result["error"] = "target message not found in current window"
            return {"results": [result], "session_identity": session_identity}

        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        data_id_at_index = await _evaluate(page, """
            ([sel, idx]) => {
              const els = document.querySelectorAll(sel);
              const el = els[idx];
              return el ? el.getAttribute('data-id') : null;
            }
        """, [full_sel, idx])
        result["data_id_at_current_index"] = data_id_at_index
        result["data_id_matches_target"] = data_id_at_index == target_id

        message = page.locator(full_sel).nth(idx)
        dl = await _open_tile_viewer_and_download(
            page, message, 0, group_name=group_name, source_message_id=target_id,
        )
        result["viewer_ok"] = dl.get("ok")
        result["stage"] = dl.get("stage")
        result["reason"] = dl.get("reason")
        result["click_attempts"] = dl.get("click_attempts")
        result["viewer_closed"] = dl.get("viewer_closed")

        downloads = dl.get("downloads") or []
        raw = next((d.get("_raw_bytes") for d in downloads if d.get("ok") and d.get("_raw_bytes")), None)
        if raw:
            result["downloaded_byte_length"] = len(raw)
            result["downloaded_sha256"] = hashlib.sha256(raw).hexdigest()
        else:
            result["downloaded_byte_length"] = 0

        return {"results": [_strip_raw_bytes(result)], "session_identity": session_identity}

    if probe_type == "video_tile_stability_diagnostic":
        # Diagnostic-only (2026-08-24) — investigates a PERSISTENT (not
        # transient — reproduced twice, identical error) failure clicking
        # the video tile on one specific source message
        # (3B07252BFE7BC81FB956, SendTest2 Intro) during
        # _open_tile_viewer_and_download: "element is not stable... element
        # was detached from the DOM, retrying". Never clicks the tile,
        # never opens the viewer, never sends anything — purely inspects
        # DOM identity/stability of the EXACT locator chain that function
        # uses (message_locator.locator('[data-testid="video-content"],
        # [data-testid="image-content"]').nth(tile_index)) over time, to
        # distinguish a genuine virtualization re-render (the node/index
        # gets swapped out) from something else entirely (never assumed).
        target_id = req.get("probe_message_id") or "3B07252BFE7BC81FB956"
        tile_index = req.get("tile_index", 0)

        def _tile_describe_js():
            return """
                ([sel, idx, marker]) => {
                  function describe(el) {
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    let anc = el.parentElement, chain = [];
                    for (let d = 0; d < 6 && anc; d++) {
                      chain.push({testid: anc.getAttribute && anc.getAttribute('data-testid'), tag: anc.tagName});
                      anc = anc.parentElement;
                    }
                    return {
                      tag: el.tagName, testid: el.getAttribute('data-testid'),
                      aria_label: el.getAttribute('aria-label'),
                      rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                      display: cs.display, visibility: cs.visibility, opacity: cs.opacity, pointer_events: cs.pointerEvents,
                      has_marker: el.getAttribute('data-diag-marker') === marker,
                      ancestor_chain: chain,
                    };
                  }
                  const msgEls = Array.from(document.querySelectorAll(sel));
                  const msgEl = msgEls[idx] || null;
                  const dataId = msgEl ? msgEl.getAttribute('data-id') : null;
                  let tile = null, pointFrom = null, stack = null, cx = null, cy = null;
                  if (msgEl) {
                    const tiles = msgEl.querySelectorAll('[data-testid="video-content"], [data-testid="image-content"]');
                    tile = tiles[0] || null;
                  }
                  const tileDesc = describe(tile);
                  if (tile) {
                    const rect = tile.getBoundingClientRect();
                    cx = Math.round(rect.x + rect.width / 2);
                    cy = Math.round(rect.y + rect.height / 2);
                    pointFrom = describe(document.elementFromPoint(cx, cy));
                    stack = document.elementsFromPoint(cx, cy).slice(0, 5).map(describe);
                  }
                  return {
                    total_rendered: msgEls.length,
                    index: idx, data_id_at_index: dataId, matches_target: dataId,
                    tile: tileDesc, click_point: {x: cx, y: cy},
                    element_from_point: pointFrom, elements_from_point: stack,
                  };
                }
            """

        def _mark_node_js():
            return """
                ([sel, idx, marker]) => {
                  const msgEls = Array.from(document.querySelectorAll(sel));
                  const msgEl = msgEls[idx];
                  if (!msgEl) return false;
                  const tiles = msgEl.querySelectorAll('[data-testid="video-content"], [data-testid="image-content"]');
                  const tile = tiles[0];
                  if (!tile) return false;
                  tile.setAttribute('data-diag-marker', marker);
                  return true;
                }
            """

        marker = f"diag-{uuid.uuid4().hex[:8]}"
        result: Dict[str, Any] = {"target_id": target_id, "tile_index": tile_index, "marker": marker}

        idx0 = await _find_message_index_by_data_id(page, group_name, target_id)
        result["initial_index_lookup"] = idx0
        if idx0 is None:
            result["error"] = "target message not found in current window at all"
            return {"results": [result], "session_identity": session_identity}

        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"

        # Snapshot 0: immediately after fresh lookup, before any interaction.
        snap0 = await _evaluate(page, _tile_describe_js(), [full_sel, idx0, marker])
        result["snapshot_0_immediate"] = snap0

        # Mark the actual DOM node so later snapshots can tell "same node,
        # still there" apart from "a different node now occupies this slot".
        marked = await _evaluate(page, _mark_node_js(), [full_sel, idx0, marker])
        result["marker_applied"] = marked

        # Try scroll_into_view_if_needed on the tile (same call the real
        # code makes) and re-snapshot immediately after.
        scroll_error = None
        try:
            message_loc = page.locator(full_sel).nth(idx0)
            tile_loc = message_loc.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile_index)
            await tile_loc.scroll_into_view_if_needed(timeout=5000)
        except Exception as exc:
            scroll_error = str(exc)
        result["scroll_error"] = scroll_error
        result["snapshot_1_after_scroll"] = await _evaluate(page, _tile_describe_js(), [full_sel, idx0, marker])

        # Timed checkpoints at 0.5s / 1.5s / 3s after the scroll — same
        # rough window Playwright's own actionability retry loop spans
        # before it reports "not stable" / "detached".
        timed_snapshots = []
        for wait_s in (0.5, 1.0, 1.5):
            await page.wait_for_timeout(int(wait_s * 1000))
            snap = await _evaluate(page, _tile_describe_js(), [full_sel, idx0, marker])
            fresh_idx = await _find_message_index_by_data_id(page, group_name, target_id)
            snap["fresh_index_lookup"] = fresh_idx
            snap["index_shifted"] = fresh_idx != idx0
            timed_snapshots.append({"elapsed_s": sum((0.5, 1.0, 1.5)[:len(timed_snapshots) + 1]), "snapshot": snap})
        result["timed_snapshots"] = timed_snapshots

        # --- Comparison targets: the known-good SendTest2 photo, and
        # whatever else is currently in the tail (read-only, same capture). ---
        comparisons: Dict[str, Any] = {}
        for label, cmp_id in (("known_good_photo_SendTest2", "3BA01C460FCEE3DD0B5B"),):
            cmp_idx = await _find_message_index_by_data_id(page, group_name, cmp_id)
            if cmp_idx is None:
                comparisons[label] = {"error": "not found in current window"}
                continue
            comparisons[label] = await _evaluate(page, _tile_describe_js(), [full_sel, cmp_idx, "no-marker"])
        result["comparisons"] = comparisons

        return {"results": [result], "session_identity": session_identity}

    if probe_type == "destination_media_inventory_diagnostic":
        # Diagnostic-only (2026-08-24) — the user directly confirmed via a
        # real screenshot that 6 media files WERE delivered to Talentgram
        # Casting Test, contradicting a prior worker readback that showed
        # only the 2 original system messages. This forces a real page
        # reload FIRST (same mechanism session_sync_check already proved
        # useful for exactly this "session hasn't caught up" class of
        # issue), then dumps a rich per-message inventory — data_id,
        # pre_plain_text, an evidence-based outgoing-direction guess (never
        # asserted as fact), media-album presence, best-effort caption
        # text, image/video counts and src prefixes — so the actual
        # destination DOM structure can be read directly rather than
        # inferred. Never sends, never clicks, never mutates anything.
        reload_error = None
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
        except Exception as exc:
            reload_error = str(exc)

        # A hard reload needs real settle time for WhatsApp's SPA to fully
        # re-init (auth/socket reconnect) before the sidebar search box is
        # usable — 3s alone wasn't enough (session_sync_check hit the same
        # SEARCH_FAILED). Bounded retry with backoff instead of a single
        # blind wait, same "prove it's ready, don't guess" spirit as the
        # rest of this file.
        status_after_reload = "SEARCH_FAILED"
        reload_settle_attempts = []
        for attempt, extra_wait_ms in enumerate((0, 4000, 6000, 8000)):
            if extra_wait_ms:
                await page.wait_for_timeout(extra_wait_ms)
            status_after_reload = await sender._open_group_chat(page, group_name)
            reload_settle_attempts.append({"attempt": attempt, "extra_wait_ms": extra_wait_ms, "status": status_after_reload})
            if status_after_reload == "OPENED":
                break

        if status_after_reload != "OPENED":
            return {"results": [{
                "ok": False, "reload_error": reload_error,
                "reload_settle_attempts": reload_settle_attempts,
                "error": f"group not open after reload (status={status_after_reload})",
            }], "session_identity": session_identity}

        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        _js = """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              return {
                total_rendered: els.length,
                messages: els.map((el, i) => {
                  const html = el.outerHTML;
                  const ct = el.querySelector('[data-pre-plain-text]');
                  const testidCounts = {};
                  (html.match(/data-testid="[^"]+"/g) || []).forEach(m => {
                    testidCounts[m] = (testidCounts[m] || 0) + 1;
                  });
                  const outgoingSignals = {
                    cls_message_out: el.className.toString().includes('message-out'),
                    has_dblcheck_testid: !!el.querySelector('[data-testid="msg-dblcheck"]'),
                    has_check_testid: !!el.querySelector('[data-testid="msg-check"]'),
                    has_dblcheck_icon: !!el.querySelector('[data-icon="msg-dblcheck"]'),
                    has_check_icon: !!el.querySelector('[data-icon="msg-check"]'),
                    has_tail_out_icon: !!el.querySelector('[data-icon="tail-out"]'),
                    html_has_message_out: html.includes('message-out'),
                  };
                  const albumEl = el.querySelector('[data-testid="media-album"]');
                  const captionCandidates = Array.from(
                    el.querySelectorAll('span.copyable-text, div.copyable-text span, [data-testid="media-caption"] span, [data-testid="media-caption"]')
                  ).map(c => (c.innerText || '').trim()).filter(t => t.length > 0);
                  const imgs = Array.from(el.querySelectorAll('img')).map(im => ({
                    src_prefix: (im.src || '').slice(0, 24), alt: im.alt || null,
                  }));
                  return {
                    index: i,
                    data_id: el.getAttribute('data-id'),
                    pre_plain_text: ct ? ct.getAttribute('data-pre-plain-text') : null,
                    inner_text: (el.innerText || '').slice(0, 300),
                    html_len: html.length,
                    testid_counts: testidCounts,
                    img_count: el.querySelectorAll('img').length,
                    video_count: el.querySelectorAll('video').length,
                    outgoing_signals: outgoingSignals,
                    has_media_album: !!albumEl,
                    caption_candidates: captionCandidates,
                    img_srcs: imgs,
                  };
                }),
              };
            }
        """
        try:
            inventory = await _evaluate(page, _js, [full_sel])
        except Exception as exc:
            inventory = {"error": str(exc)}

        return {"results": [{
            "ok": True, "reload_error": reload_error,
            "reload_settle_attempts": reload_settle_attempts,
            "status_after_reload": status_after_reload, "inventory": inventory,
        }], "session_identity": session_identity}

    if probe_type == "caption_field_diagnostic":
        # Diagnostic-only (2026-08-24) — the current caption code
        # (sender.py) clicks `//div[contains(@class, "lexical-rich-text")]`
        # wrapped in a try/except that only LOGS a warning on failure and
        # silently continues WITHOUT a caption if that selector doesn't
        # match — exactly the same class of staleness already found and
        # fixed for the attach button. This attaches a real disposable
        # local JPEG through the ALREADY-PROVEN mechanism (plus-rounded ->
        # Photos & videos -> expect_file_chooser -> set_files), stops
        # BEFORE any caption/send interaction, inventories every
        # contenteditable/textbox-like element on the preview screen (not
        # assuming the old selector is still right), types a disposable
        # test string into the best match, verifies it landed in the DOM,
        # then cancels via Escape — never clicks Send.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)

        _tiny_jpeg_b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8Q"
            "EBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
            "EBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
            "FQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k="
        )
        temp_path = None
        attach_error = None
        caption_inputs_before: Any = None
        caption_inputs_after: Any = None
        typed_ok = False
        type_error = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(b64mod.b64decode(_tiny_jpeg_b64))
            tmp.close()
            temp_path = tmp.name

            await page.click(sender.SEL["attach_btn"], timeout=10_000)
            await asyncio.sleep(0.5)
            async with page.expect_file_chooser(timeout=10_000) as fc_info:
                await page.click('button[aria-label="Photos & videos"]', timeout=10_000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(temp_path)
            await asyncio.sleep(2.0)

            _caption_inventory_js = """
                () => {
                  function describe(el) {
                    const rect = el.getBoundingClientRect();
                    return {
                      tag: el.tagName,
                      testid: el.getAttribute('data-testid'),
                      role: el.getAttribute('role'),
                      aria_label: el.getAttribute('aria-label'),
                      placeholder: el.getAttribute('data-placeholder') || el.getAttribute('placeholder'),
                      contenteditable: el.getAttribute('contenteditable'),
                      cls: (el.className || '').toString().slice(0, 160),
                      text: (el.innerText || '').slice(0, 60),
                      rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                      enabled: !el.disabled,
                    };
                  }
                  const sel = '[contenteditable="true"], div[role="textbox"], textarea, input[type="text"]';
                  return Array.from(document.querySelectorAll(sel)).map(describe);
                }
            """
            caption_inputs_before = await _evaluate(page, _caption_inventory_js, [])

            caption_xpath = '//div[contains(@class, "lexical-rich-text")]'
            try:
                await page.click(caption_xpath, timeout=5_000)
                await page.keyboard.type("CAPTION DIAGNOSTIC TEST")
                await asyncio.sleep(0.5)
                typed_ok = True
            except Exception as exc:
                type_error = f"lexical-rich-text click/type failed: {exc}"

            caption_inputs_after = await _evaluate(page, _caption_inventory_js, [])
        except Exception as exc:
            attach_error = str(exc)
        finally:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
            except Exception:
                pass
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        return {"results": [{
            "ok": attach_error is None, "group_name": group_name,
            "chat_ready_error": ready_error, "attach_error": attach_error,
            "caption_inputs_before_typing": caption_inputs_before,
            "caption_xpath_type_attempted": typed_ok, "caption_xpath_type_error": type_error,
            "caption_inputs_after_typing": caption_inputs_after,
        }], "session_identity": session_identity}

    if probe_type == "attach_interceptor_diagnostic":
        # Diagnostic-only (2026-08-24) — investigates a NEW failure: the
        # real SEND E2E downloaded all 6 source items successfully but
        # every send then failed at the FIRST destination interaction —
        # page.click([data-testid="plus-rounded"]) itself timed out with
        # Playwright reporting "element intercepts pointer events". This
        # is different from the earlier stale-selector problem (already
        # fixed and proven correct in isolation). Never clicks anything,
        # never calls set_files, never types a caption, never sends.
        #
        # Captures a full elementFromPoint/elementsFromPoint + computed-
        # style inventory of the attach button's exact click coordinates
        # at 5 checkpoints: (A) a FRESH destination-group open (baseline),
        # (B) immediately after a real video-tile retrieval while STILL in
        # the source group, (C) after switching to the destination
        # following that video retrieval, (D) immediately after a real
        # photo-tile retrieval while still in the source group, (E) after
        # switching to the destination following that photo retrieval —
        # so a diff between the fresh baseline (A) and the post-retrieval
        # destination states (C, E) shows exactly what (if anything) the
        # source-retrieval step leaves behind.
        source_group = req.get("source_group_name") or "Talentgram MEDIA SPIKE TEST"
        dest_group = group_name  # already opened by the outer code above

        _inventory_js = """
            () => {
              function describe(el) {
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                const cs = window.getComputedStyle(el);
                let anc = el.parentElement, chain = [];
                for (let d = 0; d < 10 && anc; d++) {
                  chain.push({
                    testid: anc.getAttribute && anc.getAttribute('data-testid'),
                    role: anc.getAttribute && anc.getAttribute('role'),
                    tag: anc.tagName,
                    cls: (anc.className || '').toString().slice(0, 60),
                  });
                  anc = anc.parentElement;
                }
                return {
                  tag: el.tagName,
                  testid: el.getAttribute ? el.getAttribute('data-testid') : null,
                  aria_label: el.getAttribute ? el.getAttribute('aria-label') : null,
                  role: el.getAttribute ? el.getAttribute('role') : null,
                  cls: (el.className || '').toString().slice(0, 120),
                  text: (el.innerText || '').slice(0, 60),
                  rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                  visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                  opacity: cs.opacity, pointer_events: cs.pointerEvents, z_index: cs.zIndex,
                  position: cs.position, display: cs.display, visibility: cs.visibility,
                  ancestor_chain: chain,
                };
              }
              const attachEl = document.querySelector('[data-testid="plus-rounded"]');
              const attachDesc = describe(attachEl);
              let stack = null, pointFrom = null, cx = null, cy = null;
              if (attachEl) {
                const rect = attachEl.getBoundingClientRect();
                cx = Math.round(rect.x + rect.width / 2);
                cy = Math.round(rect.y + rect.height / 2);
                pointFrom = describe(document.elementFromPoint(cx, cy));
                stack = document.elementsFromPoint(cx, cy).map(describe);
              }
              const modalSel = '[role="dialog"], [role="presentation"], [aria-modal="true"]';
              const modals = Array.from(document.querySelectorAll(modalSel)).map(describe);
              const bigFixed = Array.from(document.querySelectorAll('div')).filter(el => {
                const cs = window.getComputedStyle(el);
                if (cs.position !== 'fixed' && cs.position !== 'absolute') return false;
                const r = el.getBoundingClientRect();
                return r.width > 300 && r.height > 300;
              }).map(describe);
              return {
                attach_button: attachDesc,
                click_point: {x: cx, y: cy},
                element_from_point: pointFrom,
                elements_from_point: stack,
                modals_dialogs: modals,
                large_fixed_or_absolute: bigFixed,
                active_element: describe(document.activeElement),
                url: location.href,
                scroll: {x: window.scrollX, y: window.scrollY},
                viewport: {w: window.innerWidth, h: window.innerHeight},
                total_testid_elements: document.querySelectorAll('[data-testid]').length,
              };
            }
        """

        async def _capture(label):
            try:
                data = await _evaluate(page, _inventory_js, [])
            except Exception as exc:
                data = {"error": str(exc)}
            return {"checkpoint": label, "data": data}

        captures = []
        errors = {}

        try:
            await sender._wait_for_chat_ready(page)
            captures.append(await _capture("A_fresh_destination_baseline"))
        except Exception as exc:
            errors["A"] = str(exc)

        # --- Video retrieval (Take 1) while still in the source group -----
        try:
            status = await sender._open_group_chat(page, source_group)
            if status != "OPENED":
                errors["source_open_1"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                idx = await _find_message_index_by_data_id(page, source_group, "3B2E8E7ECFE51C927D01")
                if idx is None:
                    errors["video_locate"] = "Take 1 source message not found in window"
                else:
                    scope = await sender._resolve_scope(page)
                    full_sel = f"{scope} [data-testid^='conv-msg-']"
                    message = page.locator(full_sel).nth(idx)
                    dl = await _open_tile_viewer_and_download(page, message, 0)
                    errors["video_download_ok"] = bool(dl.get("ok"))
                    if not dl.get("ok"):
                        errors["video_download_reason"] = dl.get("reason") or dl.get("stage")
                    captures.append(await _capture("B_after_video_retrieval_still_in_source"))
        except Exception as exc:
            errors["B"] = str(exc)

        try:
            status = await sender._open_group_chat(page, dest_group)
            if status != "OPENED":
                errors["dest_reopen_1"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                captures.append(await _capture("C_after_video_retrieval_in_destination"))
        except Exception as exc:
            errors["C"] = str(exc)

        # --- Photo retrieval (Photo 1) while still in the source group ----
        try:
            status = await sender._open_group_chat(page, source_group)
            if status != "OPENED":
                errors["source_open_2"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                idx = await _find_message_index_by_data_id(page, source_group, "3B97692060C7060F1D5B")
                if idx is None:
                    errors["photo_locate"] = "Photos source message not found in window"
                else:
                    scope = await sender._resolve_scope(page)
                    full_sel = f"{scope} [data-testid^='conv-msg-']"
                    message = page.locator(full_sel).nth(idx)
                    dl = await _download_photo_album_tile_via_blob(
                        message, page, 0, "10aee85ac7d6b0294d8e8e833a60ba9d0bc17acc7ec7edd86e8b9345fe02ead8",
                    )
                    errors["photo_download_ok"] = bool(dl.get("ok"))
                    if not dl.get("ok"):
                        errors["photo_download_reason"] = dl.get("reason") or dl.get("stage")
                    captures.append(await _capture("D_after_photo_retrieval_still_in_source"))
        except Exception as exc:
            errors["D"] = str(exc)

        try:
            status = await sender._open_group_chat(page, dest_group)
            if status != "OPENED":
                errors["dest_reopen_2"] = f"status={status}"
            else:
                await sender._wait_for_chat_ready(page)
                captures.append(await _capture("E_after_photo_retrieval_in_destination"))
        except Exception as exc:
            errors["E"] = str(exc)

        return {"results": [{
            "ok": True, "source_group": source_group, "dest_group": dest_group,
            "captures": captures, "errors": errors,
        }], "session_identity": session_identity}

    if probe_type == "attach_real_file_diagnostic":
        # Diagnostic-only (2026-08-24) — final live verification of the
        # sender.py fix (real "plus-rounded" button -> real "Photos &
        # videos" menu item -> Playwright's own expect_file_chooser()),
        # exercising the EXACT code path sender.py now uses, against the
        # real live WhatsApp UI. Uses a tiny disposable 1x1 JPEG generated
        # on the worker's own local disk (never derived from any real
        # submission/production media) purely to prove attachment reaches
        # the preview screen. Stops immediately after that — never types a
        # caption, never clicks Send. The preview is explicitly cancelled
        # (Escape, the same safe close mechanism every other diagnostic in
        # this file already uses) before returning, so nothing is ever
        # introduced into the real conversation.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)

        _tiny_jpeg_b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8Q"
            "EBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
            "EBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
            "FQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k="
        )
        temp_path = None
        attach_error = None
        preview_dom = None
        cancel_error = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(b64mod.b64decode(_tiny_jpeg_b64))
            tmp.close()
            temp_path = tmp.name

            await page.click(sender.SEL["attach_btn"], timeout=10_000)
            await asyncio.sleep(0.5)
            async with page.expect_file_chooser(timeout=10_000) as fc_info:
                await page.click('button[aria-label="Photos & videos"]', timeout=10_000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(temp_path)
            await asyncio.sleep(2.0)

            _preview_js = """
                () => {
                  const imgs = document.querySelectorAll('img[src^="blob:"]');
                  const sendBtn = document.querySelector('[data-testid="send"]');
                  return {
                    blob_image_count: imgs.length,
                    send_button_present: !!sendBtn,
                    total_testid_elements: document.querySelectorAll('[data-testid]').length,
                  };
                }
            """
            try:
                preview_dom = await _evaluate(page, _preview_js, [])
            except Exception as exc:
                preview_dom = {"error": str(exc)}
        except Exception as exc:
            attach_error = str(exc)
        finally:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
            except Exception as exc:
                cancel_error = str(exc)
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        return {"results": [{
            "ok": attach_error is None, "group_name": group_name,
            "chat_ready_error": ready_error, "attach_error": attach_error,
            "preview_dom": preview_dom, "cancel_error": cancel_error,
        }], "session_identity": session_identity}

    if probe_type == "attach_photos_videos_filechooser_diagnostic":
        # Diagnostic-only (2026-08-24) — attach_mechanism_full_diagnostic
        # found the real attach menu (role="menu", items identified by
        # aria-label, NO data-testid at all — why the earlier testid-regex
        # probe found nothing) with a "Photos & videos" item that bundles
        # BOTH media types into one control. This uses Playwright's own
        # expect_file_chooser() — the correct, non-synthetic mechanism for
        # this exact situation — to positively identify which <input> that
        # menu item wires up, via its accept/multiple attributes. NEVER
        # calls file_chooser.set_files(...), so no file is ever selected
        # and nothing is attached to the conversation.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)

        open_menu_error = None
        try:
            await page.click('[data-testid="plus-rounded"]', timeout=10_000)
            await asyncio.sleep(0.8)
        except Exception as exc:
            open_menu_error = str(exc)

        chooser_error = None
        chooser_accept = None
        chooser_multiple = None
        chooser_input_visible = None
        try:
            async with page.expect_file_chooser(timeout=10_000) as fc_info:
                await page.click('button[aria-label="Photos & videos"]', timeout=10_000)
            file_chooser = await fc_info.value
            chooser_multiple = file_chooser.is_multiple()
            element = file_chooser.element
            chooser_accept = await element.get_attribute("accept")
            chooser_input_visible = await element.is_visible()
        except Exception as exc:
            chooser_error = str(exc)

        try:
            await page.keyboard.press("Escape")
            await page.keyboard.press("Escape")
        except Exception:
            pass

        return {"results": [{
            "ok": chooser_error is None, "group_name": group_name,
            "chat_ready_error": ready_error, "open_menu_error": open_menu_error,
            "chooser_error": chooser_error,
            "chooser_accept": chooser_accept, "chooser_multiple": chooser_multiple,
            "chooser_input_visible": chooser_input_visible,
        }], "session_identity": session_identity}

    if probe_type == "attach_mechanism_full_diagnostic":
        # Diagnostic-only (2026-08-24) — full before/after investigation of
        # the CURRENT WhatsApp Web attach mechanism, requested explicitly
        # before any sender.py change: captures composer-area buttons,
        # elementsFromPoint at the real attach button's location, and every
        # file-input/menu-like element BEFORE a real Playwright click on
        # [data-testid="plus-rounded"], then the SAME inventory AFTER, so a
        # diff shows exactly what (if anything) the click actually mounts.
        # Never sends anything — presses Escape at the end either way.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)

        _inventory_js = """
            () => {
              function describe(el) {
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return {
                  tag: el.tagName,
                  testid: el.getAttribute ? el.getAttribute('data-testid') : null,
                  aria_label: el.getAttribute ? el.getAttribute('aria-label') : null,
                  title: el.getAttribute ? el.getAttribute('title') : null,
                  role: el.getAttribute ? el.getAttribute('role') : null,
                  text: (el.innerText || '').slice(0, 80),
                  visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                  rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                };
              }
              function collectFileInputs() {
                return Array.from(document.querySelectorAll('input[type="file"]')).map((el, i) => {
                  const rect = el.getBoundingClientRect();
                  let anc = el.parentElement, chain = [];
                  for (let d = 0; d < 8 && anc; d++) {
                    const t = anc.getAttribute && anc.getAttribute('data-testid');
                    if (t) chain.push(t);
                    anc = anc.parentElement;
                  }
                  return {
                    index: i, id: el.id || null, accept: el.getAttribute('accept'), multiple: el.multiple,
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                    ancestor_testids: chain,
                  };
                });
              }
              function collectMenuLike() {
                const sels = '[role="menu"], [role="menuitem"], [role="listbox"], [role="option"], [role="dialog"], [data-testid*="menu" i], [data-testid*="popover" i], [data-testid*="dialog" i]';
                return Array.from(document.querySelectorAll(sels)).map(describe);
              }
              function collectComposerButtons() {
                const box = document.querySelector('[data-testid="conversation-compose-box-input"]');
                let footer = box;
                for (let d = 0; d < 8 && footer; d++) {
                  if (footer.tagName === 'FOOTER') break;
                  footer = footer.parentElement;
                }
                const scope = footer || document;
                return Array.from(scope.querySelectorAll('button, [role="button"], span[data-testid]')).map(describe);
              }
              const attachEl = document.querySelector('[data-testid="plus-rounded"]');
              const rect = attachEl ? attachEl.getBoundingClientRect() : null;
              const cx = rect ? Math.round(rect.x + rect.width / 2) : null;
              const cy = rect ? Math.round(rect.y + rect.height / 2) : null;
              const stack = (cx !== null) ? document.elementsFromPoint(cx, cy).map(describe) : null;
              return {
                attach_button: describe(attachEl),
                click_point: {x: cx, y: cy},
                elements_from_point: stack,
                composer_buttons: collectComposerButtons(),
                file_inputs: collectFileInputs(),
                menu_like: collectMenuLike(),
                total_testid_elements: document.querySelectorAll('[data-testid]').length,
              };
            }
        """
        try:
            before = await _evaluate(page, _inventory_js, [])
        except Exception as exc:
            before = {"error": str(exc)}

        click_error = None
        try:
            await page.click('[data-testid="plus-rounded"]', timeout=10_000)
            await asyncio.sleep(1.2)
        except Exception as exc:
            click_error = str(exc)

        try:
            after = await _evaluate(page, _inventory_js, [])
        except Exception as exc:
            after = {"error": str(exc)}

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

        return {"results": [{
            "ok": True, "group_name": group_name,
            "chat_ready_error": ready_error, "click_error": click_error,
            "before": before, "after": after,
        }], "session_identity": session_identity}

    if probe_type == "plus_rounded_locations_diagnostic":
        # Diagnostic-only (2026-08-24) — the previous probe's click on the
        # FIRST [data-testid="plus-rounded"] in the DOM revealed 2
        # image-only file inputs that look like profile/status pickers
        # (one sits right next to "navbar-item-me-tab-photo"), not a chat
        # attach menu — meaning a bare, unscoped click almost certainly
        # hit the WRONG "plus-rounded" (WhatsApp reuses this testid in
        # multiple places: sidebar nav, status composer, AND the actual
        # chat attach button). This is read-only: locates every
        # "plus-rounded" element without clicking any of them, and reports
        # each one's position/visibility/ancestor context so the real
        # chat-compose one can be identified and properly scoped.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)
        _js = """
            () => {
              const els = Array.from(document.querySelectorAll('[data-testid="plus-rounded"]'));
              return els.map((el, i) => {
                const rect = el.getBoundingClientRect();
                let anc = el.parentElement, chain = [];
                for (let d = 0; d < 6 && anc; d++) {
                  const t = anc.getAttribute && anc.getAttribute('data-testid');
                  if (t) chain.push(t);
                  anc = anc.parentElement;
                }
                return {
                  index: i,
                  visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                  rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                  ancestor_testids: chain,
                  aria_label: el.getAttribute('aria-label') || (el.closest('[aria-label]') && el.closest('[aria-label]').getAttribute('aria-label')),
                };
              });
            }
        """
        try:
            dom_result = await _evaluate(page, _js, [])
        except Exception as exc:
            dom_result = {"error": str(exc)}
        return {"results": [{
            "ok": True, "group_name": group_name,
            "chat_ready_error": ready_error,
            "plus_rounded_locations": dom_result,
        }], "session_identity": session_identity}

    if probe_type == "attach_menu_after_click_diagnostic":
        # Diagnostic-only (2026-08-24) — follow-up to attach_button_diagnostic:
        # data-testid="attach-menu-plus" no longer exists anywhere in the DOM;
        # the actual button is data-testid="plus-rounded". session.py also
        # defines (unused-in-code) "attach_doc"/"attach_img" testids
        # ("attach-document"/"attach-image-video"), suggesting the original
        # flow expected a 2-step menu (click plus -> click a specific
        # attach-type item) that got collapsed/simplified at some point.
        # This clicks the REAL plus button, dumps whatever menu appears,
        # then presses Escape to close it again — never selects a file,
        # never sends anything.
        ready_error = None
        try:
            await sender._wait_for_chat_ready(page)
        except Exception as exc:
            ready_error = str(exc)
        click_error = None
        try:
            await page.click('[data-testid="plus-rounded"]', timeout=10_000)
            await asyncio.sleep(1.0)
        except Exception as exc:
            click_error = str(exc)
        _js = """
            () => {
              const all = Array.from(document.querySelectorAll('[data-testid]'));
              return {
                total_testid_elements: all.length,
                attach_image_video_exists: !!document.querySelector('[data-testid="attach-image-video"]'),
                attach_document_exists: !!document.querySelector('[data-testid="attach-document"]'),
                file_inputs: Array.from(document.querySelectorAll('input[type="file"]')).map((el, i) => ({
                  index: i, accept: el.getAttribute('accept'), multiple: el.multiple,
                  visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                })),
                menu_like: all
                  .filter(el => /attach|menu|photo|video|document|camera/i.test(el.getAttribute('data-testid') || ''))
                  .map(el => ({
                    testid: el.getAttribute('data-testid'), tag: el.tagName,
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                    aria_label: el.getAttribute('aria-label'),
                  })),
              };
            }
        """
        try:
            dom_result = await _evaluate(page, _js, [])
        except Exception as exc:
            dom_result = {"error": str(exc)}
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return {"results": [{
            "ok": True, "group_name": group_name,
            "chat_ready_error": ready_error, "click_error": click_error,
            "dom": dom_result,
        }], "session_identity": session_identity}

    if probe_type == "group_participants_check":
        # Diagnostic-only (2026-08-24, Part 5 of SEND rollout): read-only
        # membership check via the SAME sender.get_group_participants
        # already used by the inbound listener's group_members security
        # mode — never sends anything, just scrapes the already-open
        # Group Info drawer's visible text so we can confirm Gunwanti's
        # account is actually a member of both the SEND source and
        # destination test groups before any real media is sent.
        participants = await sender.get_group_participants(page, group_name)
        return {"results": [{
            "ok": participants is not None,
            "group_name": group_name,
            "participants": participants,
            "error": None if participants is not None else "could not read group participants panel",
        }], "session_identity": session_identity}

    if probe_type == "reply_quote_diagnostic":
        # Diagnostic-only (2026-08-24): _resolve_quoted_jump reported
        # "reply message has no quoted-message block" for a reply the
        # user has directly confirmed (via screenshot) WAS sent as a
        # genuine native WhatsApp reply to the photo album, with a
        # visible quoted-media preview. Dumps the complete raw DOM of the
        # target message and every reply-related signal (not assuming
        # the answer is [data-testid="quoted-message"] specifically),
        # never clicking or navigating anything.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        try:
            dump = await message.evaluate("""
                (el) => {
                  const html = el.outerHTML;
                  const testidCounts = {};
                  (html.match(/data-testid="[^"]+"/g) || []).forEach(m => {
                    testidCounts[m] = (testidCounts[m] || 0) + 1;
                  });
                  const ariaLabels = Array.from(el.querySelectorAll('[aria-label]')).map(e => ({
                    tag: e.tagName, ariaLabel: e.getAttribute('aria-label'), testid: e.getAttribute('data-testid'), role: e.getAttribute('role'),
                  }));
                  const roleButtons = Array.from(el.querySelectorAll('[role="button"]')).map(e => ({
                    tag: e.tagName, testid: e.getAttribute('data-testid'), ariaLabel: e.getAttribute('aria-label'),
                    cls: (e.className || '').toString().slice(0, 100),
                  }));
                  const quotedMessageEl = el.querySelector('[data-testid="quoted-message"]');
                  const quoteMentionText = (html.match(/quot/gi) || []).length;
                  return {
                    data_id: el.getAttribute('data-id'),
                    html_len: html.length,
                    html_full: html,
                    testid_counts: testidCounts,
                    aria_labels: ariaLabels.slice(0, 30),
                    role_buttons: roleButtons.slice(0, 30),
                    quoted_message_found: !!quotedMessageEl,
                    quoted_message_html: quotedMessageEl ? quotedMessageEl.outerHTML.slice(0, 2000) : null,
                    inner_text: (el.innerText || '').slice(0, 500),
                    quot_substring_count: quoteMentionText,
                  };
                }
            """, timeout=15000)
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"evaluate failed: {exc}"}]}
        return {"results": [dump], "session_identity": session_identity}

    if probe_type == "message_text_snapshot":
        # Diagnostic-only (2026-08-23): confirms a specific message's ACTUAL
        # text content by data-id (no reload, no code touching the
        # downloader) — used to verify a candidate "new tail" entry really
        # is the expected control message rather than inferring from html
        # size alone.
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        result = await _evaluate(page, """
            ([sel, targetId]) => {
              const els = Array.from(document.querySelectorAll(sel));
              const el = els.find(e => e.getAttribute('data-id') === targetId);
              if (!el) return {found: false};
              const ct = el.querySelector('[data-pre-plain-text]');
              const html = el.outerHTML;
              const testidCounts = {};
              (html.match(/data-testid="[^"]+"/g) || []).forEach(m => {
                testidCounts[m] = (testidCounts[m] || 0) + 1;
              });
              return {
                found: true,
                inner_text: (el.innerText || '').slice(0, 300),
                pre_plain_text: ct ? ct.getAttribute('data-pre-plain-text') : null,
                html_len: html.length,
                testid_counts: testidCounts,
                img_count: el.querySelectorAll('img').length,
                img_srcs: Array.from(el.querySelectorAll('img')).slice(0, 5).map(i => (i.src || '').slice(0, 40)),
                html_snippet: html.slice(0, 800),
              };
            }
        """, [full_sel, req.get("probe_message_id")])
        return {"results": [result], "session_identity": session_identity}

    if probe_type == "session_sync_check":
        # Diagnostic (2026-08-23): the user confirmed via a real screenshot
        # that the 6-7-photo album exists in the correct WhatsApp group,
        # yet neither the bounded scan nor a raw, un-cached DOM query (with
        # a proven-stable scrollTop=scrollHeight bottom position) show it.
        # Rather than touch any application code, this investigates whether
        # the worker's own WhatsApp Web session/tab is simply stale — forces
        # a full page reload (a real re-sync from WhatsApp's servers, not a
        # resend/any data mutation) and re-reads the SAME group's tail
        # before and after, to distinguish "this session's in-page JS state
        # hasn't caught up" from "the session genuinely has nothing newer".
        scope_before = await sender._resolve_scope(page)
        full_sel_before = f"{scope_before} [data-testid^='conv-msg-']"

        _tail_js = """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              return {
                total_rendered: els.length,
                last_ids: els.slice(-8).map(el => {
                  const ct = el.querySelector('[data-pre-plain-text]');
                  return {
                    data_id: el.getAttribute('data-id'),
                    pre_plain_text: ct ? ct.getAttribute('data-pre-plain-text') : null,
                    media_album: el.outerHTML.includes('data-testid="media-album"'),
                    image_content_count: (el.outerHTML.match(/data-testid="image-content"/g) || []).length,
                  };
                }),
              };
            }
        """

        session_identity_before = {
            "own_phone_number": getattr(session, "own_phone_number", None),
            "session_id": getattr(session, "session_id", None),
            "generation": getattr(session, "generation", None),
        }
        try:
            before = await _evaluate(page, _tail_js, [full_sel_before])
        except Exception as exc:
            before = {"error": str(exc)}

        reload_error = None
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
        except Exception as exc:
            reload_error = str(exc)

        status_after_reload = await sender._open_group_chat(page, group_name)
        session_identity_after = {
            "own_phone_number": getattr(session, "own_phone_number", None),
            "session_id": getattr(session, "session_id", None),
            "generation": getattr(session, "generation", None),
        }
        after = {"error": f"group not open after reload (status={status_after_reload})"}
        if status_after_reload == "OPENED":
            scope_after = await sender._resolve_scope(page)
            full_sel_after = f"{scope_after} [data-testid^='conv-msg-']"
            try:
                after = await _evaluate(page, _tail_js, [full_sel_after])
            except Exception as exc:
                after = {"error": str(exc)}

        return {"results": [{
            "session_identity_before": session_identity_before,
            "session_identity_after": session_identity_after,
            "before_reload": before,
            "reload_error": reload_error,
            "status_after_reload": status_after_reload,
            "after_reload": after,
        }], "session_identity": session_identity_after}

    if probe_type == "blob_tile_download_diagnostic":
        # Diagnostic-only (2026-08-23): media_readiness_diagnostic proved
        # each album tile's full-resolution photo is ALREADY loaded as a
        # real, complete blob: URL directly in the tile's own DOM — no
        # gallery/viewer needed. This locates one exact tile by its
        # thumbnail hash (never by position/order), fetches that tile's
        # OWN full-res blob directly, and verifies the bytes (SHA-256,
        # magic-byte MIME, real parsed dimensions cross-checked against
        # the DOM-reported naturalWidth/naturalHeight) — never claims
        # identity merely from "found under the right element".
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        thumbs = message.locator('[data-testid="image-thumb"]')
        try:
            thumb_count = await thumbs.count()
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"count failed: {exc}"}]}

        tile_inventory = []
        for i in range(thumb_count):
            thumb = thumbs.nth(i)
            entry: Dict[str, Any] = {"index": i}
            try:
                thumb_html = await thumb.evaluate("(el) => el.outerHTML", timeout=10000)
                entry["hash"] = _smallest_hash(thumb_html)
            except Exception as exc:
                entry["hash"] = None
                entry["hash_error"] = str(exc)
            try:
                full_res = await thumb.evaluate("""
                    (tile) => {
                      const imgs = Array.from(tile.querySelectorAll('img'));
                      if (imgs.length === 0) return null;
                      const best = imgs.reduce((a, b) => (a.naturalWidth * a.naturalHeight) >= (b.naturalWidth * b.naturalHeight) ? a : b);
                      return { src: best.src, naturalWidth: best.naturalWidth, naturalHeight: best.naturalHeight, complete: best.complete, isBlob: best.src.startsWith('blob:') };
                    }
                """, timeout=10000)
            except Exception as exc:
                full_res = None
                entry["full_res_error"] = str(exc)
            entry["full_res"] = full_res
            tile_inventory.append(entry)

        target_hash = req.get("target_hash")
        result: Dict[str, Any] = {"ok": True, "data_id": data_id, "thumb_count": thumb_count, "tile_inventory": tile_inventory}

        if target_hash:
            match = next((t for t in tile_inventory if t.get("hash") == target_hash), None)
            if not match:
                result["download"] = {"ok": False, "reason": f"no tile matched target_hash {target_hash!r}"}
                return {"results": [result], "session_identity": session_identity}
            full_res = match.get("full_res")
            if not full_res or not full_res.get("isBlob"):
                result["download"] = {"ok": False, "reason": "matched tile has no blob: full-res image", "matched_tile": match}
                return {"results": [result], "session_identity": session_identity}
            try:
                fetch_result = await _evaluate(page, """
                    async ([src]) => {
                      try {
                        const resp = await fetch(src);
                        const buf = await resp.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let binary = '';
                        const chunkSize = 0x8000;
                        for (let i = 0; i < bytes.length; i += chunkSize) {
                          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                        }
                        return { ok: true, status: resp.status, base64: btoa(binary), contentType: resp.headers.get('content-type') || '' };
                      } catch (e) {
                        return { ok: false, reason: String(e && e.message || e) };
                      }
                    }
                """, [full_res["src"]])
            except Exception as exc:
                fetch_result = {"ok": False, "reason": str(exc)}

            download: Dict[str, Any] = {"matched_tile_index": match["index"], "matched_tile_hash": match["hash"], "fetch_status": fetch_result.get("status"), "fetch_content_type": fetch_result.get("contentType")}
            if not fetch_result.get("ok"):
                download["ok"] = False
                download["reason"] = fetch_result.get("reason")
            else:
                raw = b64mod.b64decode(fetch_result["base64"]) if fetch_result.get("base64") else b""
                parsed_w, parsed_h = _parse_image_dimensions(raw)
                download.update({
                    "ok": True, "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "detected_mime": _detect_mime_type(raw, "image"),
                    "parsed_width": parsed_w, "parsed_height": parsed_h,
                    "dom_reported_width": full_res.get("naturalWidth"), "dom_reported_height": full_res.get("naturalHeight"),
                    "dimensions_match_dom": (parsed_w == full_res.get("naturalWidth") and parsed_h == full_res.get("naturalHeight")),
                })
            result["download"] = download

        return {"results": [result], "session_identity": session_identity}

    if probe_type == "media_readiness_diagnostic":
        # Diagnostic-only (2026-08-23): final observation layer, after
        # ruling out selector/click-target/CSS/keyboard/album-size/general-
        # sync explanations. Observes DOM-visible media/readiness state
        # only — no internals, no synthetic events, no network calls to
        # WhatsApp endpoints beyond what the page itself already exposes.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        tile_index = req.get("tile_index", 0)
        thumbs = message.locator('[data-testid="image-thumb"]')
        try:
            thumb_count = await thumbs.count()
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"count failed: {exc}"}]}
        if tile_index >= thumb_count:
            return {"results": [{"ok": False, "error": f"tile_index {tile_index} out of range (count={thumb_count})"}]}
        tile = thumbs.nth(tile_index)

        snapshot_js = """
            (tile) => {
              const allAttrs = (el) => {
                const out = {};
                for (const a of el.attributes) out[a.name] = a.value.slice(0, 120);
                return out;
              };
              const imgState = (img) => ({
                src: (img.src || '').slice(0, 60), currentSrc: (img.currentSrc || '').slice(0, 60),
                naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight,
                complete: img.complete, loading: img.loading, decoding: img.decoding,
                attrs: allAttrs(img),
              });
              const imgs = Array.from(tile.querySelectorAll('img')).map(imgState);

              let provider = tile.closest('[data-testid="media-url-provider"]') || tile.querySelector('[data-testid="media-url-provider"]');
              const providerInfo = provider ? {
                attrs: allAttrs(provider), html_len: provider.outerHTML.length,
                html_snippet: provider.outerHTML.slice(0, 500),
              } : null;

              // Loading/pending/progress indicators anywhere in the tile.
              const loadingCandidates = Array.from(tile.querySelectorAll('*')).filter(el => {
                const testid = (el.getAttribute('data-testid') || '').toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                return /load|spinner|progress|pending|skeleton/.test(testid) || /load|spinner|progress|pending|skeleton/.test(cls);
              }).slice(0, 10).map(el => ({ tag: el.tagName, testid: el.getAttribute('data-testid'), cls: (el.className || '').toString().slice(0, 80) }));

              const ancestorAttrs = [];
              let el = tile;
              let hops = 0;
              while (el && hops < 6) {
                ancestorAttrs.push({ tag: el.tagName, testid: el.getAttribute('data-testid'), attrs: allAttrs(el) });
                el = el.parentElement;
                hops++;
              }

              return {
                tileAttrs: allAttrs(tile),
                imgs,
                mediaUrlProvider: providerInfo,
                loadingIndicators: loadingCandidates,
                ancestorAttrs,
                activeElement: document.activeElement ? { tag: document.activeElement.tagName, testid: document.activeElement.getAttribute('data-testid') } : null,
                videoCount: document.querySelectorAll('video').length,
                dialogCount: document.querySelectorAll('[role="dialog"]').length,
              };
            }
        """

        try:
            await tile.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            before = await tile.evaluate(snapshot_js, timeout=15000)
        except Exception as exc:
            return {"results": [{"ok": False, "stage": "before_snapshot", "error": str(exc)}]}

        # Message-level metadata — search the WHOLE message's attributes
        # (not just the tile's) for anything hinting at count/state/
        # readiness, without assuming any specific attribute exists.
        try:
            message_meta = await message.evaluate("""
                (msg) => {
                  const interesting = [];
                  const walk = (el, depth) => {
                    if (depth > 8) return;
                    for (const a of el.attributes || []) {
                      if (/count|state|status|ready|pending|load|batch|total/i.test(a.name) || /count|state|status|ready|pending|loading|batch/i.test(a.value)) {
                        interesting.push({ tag: el.tagName, testid: el.getAttribute('data-testid'), attr: a.name, value: a.value.slice(0, 100) });
                      }
                    }
                    for (const child of el.children) walk(child, depth + 1);
                  };
                  walk(msg, 0);
                  return interesting.slice(0, 30);
                }
            """, timeout=15000)
        except Exception as exc:
            message_meta = {"error": str(exc)}

        # perform_click defaults True, but the caller can skip the click
        # entirely (e.g. for the already-proven single photo, where a real
        # click would open its viewer and require closing it again — this
        # probe only needs its READINESS state, not a repeat of the
        # already-established click-opens-it fact).
        perform_click = req.get("perform_click", True)
        click_ok = None
        click_error = None
        after = None
        close_result = None
        if perform_click:
            try:
                baseline_srcs = await _evaluate(page, _PHOTO_BLOB_BASELINE_JS)
            except Exception:
                baseline_srcs = []
            try:
                await tile.click(timeout=10000)
                click_ok = True
            except Exception as exc:
                click_ok = False
                click_error = str(exc)

            await page.wait_for_timeout(3000)

            try:
                after = await tile.evaluate(snapshot_js, timeout=15000)
            except Exception as exc:
                after = {"error": str(exc)}

            # Safety net: if this click DID open a viewer (e.g. run against
            # the single photo for a control comparison), close it via the
            # real Close control rather than leaving it open.
            try:
                viewer_check = await _evaluate(page, _ALBUM_VIEWER_SNAPSHOT_JS, [baseline_srcs, False])
            except Exception:
                viewer_check = {"found": False}
            if viewer_check.get("found"):
                primary_src = (viewer_check.get("primary") or {}).get("src", "")
                close_result = await _close_photo_viewer(page, {"buttons": viewer_check.get("buttons", [])}, primary_src)

        return {
            "results": [{
                "ok": True, "data_id": data_id, "tile_index": tile_index,
                "before": before, "after": after,
                "message_level_metadata": message_meta,
                "click_performed": perform_click, "click_ok": click_ok, "click_error": click_error,
                "close_result": close_result,
            }],
            "session_identity": session_identity,
        }

    if probe_type == "keyboard_activation_diagnostic":
        # Diagnostic-only (2026-08-23): the DOM/CSS structure is byte-for-
        # byte identical between a single photo's image-thumb (which
        # opens a viewer on click) and an album tile's image-thumb (which
        # doesn't) — role="button" + tabindex="0" is the WAI-ARIA
        # convention for a control also activatable via Enter/Space, not
        # just click. Tests the browser's normal keyboard activation path
        # — no synthetic events, no internals, real Playwright focus +
        # real key presses only.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        tile_index = req.get("tile_index", 0)
        thumbs = message.locator('[data-testid="image-thumb"]')
        try:
            thumb_count = await thumbs.count()
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"count failed: {exc}"}]}
        if tile_index >= thumb_count:
            return {"results": [{"ok": False, "error": f"tile_index {tile_index} out of range (count={thumb_count})"}]}
        tile = thumbs.nth(tile_index)

        active_element_js = """
            () => {
              const el = document.activeElement;
              return el ? { tag: el.tagName, testid: el.getAttribute('data-testid'), role: el.getAttribute('role'), ariaLabel: el.getAttribute('aria-label') } : null;
            }
        """

        async def _test_key(key: str) -> Dict[str, Any]:
            try:
                await tile.scroll_into_view_if_needed(timeout=5000)
                await tile.focus(timeout=5000)
            except Exception as exc:
                return {"key": key, "ok": False, "stage": "focus", "reason": str(exc)}
            try:
                active_before = await _evaluate(page, active_element_js)
            except Exception:
                active_before = None
            focused_correctly = bool(active_before and active_before.get("testid") == "image-thumb")
            try:
                baseline_srcs = await _evaluate(page, _PHOTO_BLOB_BASELINE_JS)
            except Exception:
                baseline_srcs = []
            try:
                url_before = await page.evaluate("() => location.href")
            except Exception:
                url_before = None
            try:
                await _evaluate(page, """
                    () => {
                      window.__keyProbe = [];
                      const record = (e) => { window.__keyProbe.push({ type: e.type, key: e.key, defaultPrevented: e.defaultPrevented, target: e.target && e.target.getAttribute && e.target.getAttribute('data-testid') }); };
                      if (!window.__keyProbeInstalled) {
                        document.addEventListener('keydown', record, true);
                        document.addEventListener('keyup', record, true);
                        window.__keyProbeInstalled = true;
                      }
                    }
                """)
            except Exception:
                pass
            try:
                await page.keyboard.press(key)
            except Exception as exc:
                return {"key": key, "ok": False, "stage": "press", "reason": str(exc), "focused_correctly": focused_correctly, "active_before": active_before}
            try:
                key_event_log = await _evaluate(page, "() => { const ev = window.__keyProbe || []; window.__keyProbe = []; return ev; }")
            except Exception:
                key_event_log = None

            viewer_result = None
            elapsed = 0.0
            while elapsed < 6.0:
                try:
                    viewer_result = await _evaluate(page, _ALBUM_VIEWER_SNAPSHOT_JS, [baseline_srcs, False])
                except Exception as exc:
                    viewer_result = {"found": False, "error": str(exc)}
                if viewer_result.get("found"):
                    break
                await page.wait_for_timeout(500)
                elapsed += 0.5

            try:
                url_after = await page.evaluate("() => location.href")
            except Exception:
                url_after = None
            try:
                active_after = await _evaluate(page, active_element_js)
            except Exception:
                active_after = None

            close_result = None
            if viewer_result and viewer_result.get("found"):
                primary_src = (viewer_result.get("primary") or {}).get("src", "")
                close_result = await _close_photo_viewer(page, {"buttons": viewer_result.get("buttons", [])}, primary_src)

            return {
                "key": key, "ok": True, "focused_correctly": focused_correctly, "active_before": active_before,
                "active_after": active_after, "key_event_log": key_event_log,
                "url_changed": url_before != url_after, "url_before": url_before, "url_after": url_after,
                "viewer_found": bool(viewer_result and viewer_result.get("found")),
                "viewer_detail": viewer_result, "close_result": close_result,
            }

        enter_result = await _test_key("Enter")
        space_result = await _test_key("Space")

        return {
            "results": [{
                "ok": True, "data_id": data_id, "tile_index": tile_index,
                "enter_result": enter_result, "space_result": space_result,
            }],
            "session_identity": session_identity,
        }

    if probe_type == "click_internals_diagnostic":
        # Diagnostic-only (2026-08-23): the album's tile 0 receives a real,
        # unblocked, trusted click that mounts nothing, while the single-
        # photo message's own image-thumb opens a viewer from the exact
        # same testid/role/aria-label shape. This captures full ancestor-
        # chain computed styles, the complete elementsFromPoint() stack at
        # the real click coordinate (not just the topmost hit), and
        # searches the whole message for any OTHER interactive control
        # (a separate "open album" wrapper distinct from the per-tile
        # "Open picture" control) — structural comparison only, no click
        # performed here (the click+DOM-change test is a separate,
        # already-proven probe_type).
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        tile_index = req.get("tile_index", 0)
        thumbs = message.locator('[data-testid="image-thumb"]')
        try:
            thumb_count = await thumbs.count()
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"count failed: {exc}"}]}
        if tile_index >= thumb_count:
            return {"results": [{"ok": False, "error": f"tile_index {tile_index} out of range (count={thumb_count})"}]}
        tile = thumbs.nth(tile_index)
        try:
            await tile.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            inspection = await tile.evaluate("""
                (tile) => {
                  const styleOf = (el) => {
                    const cs = getComputedStyle(el);
                    return {
                      pointerEvents: cs.pointerEvents, cursor: cs.cursor, zIndex: cs.zIndex,
                      position: cs.position, overflow: cs.overflow, visibility: cs.visibility,
                      opacity: cs.opacity, display: cs.display,
                    };
                  };
                  const describe = (el) => {
                    const r = el.getBoundingClientRect();
                    return {
                      tag: el.tagName, testid: el.getAttribute('data-testid'), role: el.getAttribute('role'),
                      ariaLabel: el.getAttribute('aria-label'), tabindex: el.getAttribute('tabindex'),
                      cls: (el.className || '').toString().slice(0, 100),
                      rect: [r.x, r.y, r.width, r.height], style: styleOf(el),
                    };
                  };
                  const ancestors = [];
                  let el = tile;
                  let hops = 0;
                  while (el && hops < 15) {
                    ancestors.push(describe(el));
                    if (el.getAttribute && el.getAttribute('data-testid') && el.getAttribute('data-testid').startsWith('conv-msg-')) break;
                    el = el.parentElement;
                    hops++;
                  }
                  const r = tile.getBoundingClientRect();
                  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                  const stack = (document.elementsFromPoint ? document.elementsFromPoint(cx, cy) : []).slice(0, 10).map(describe);
                  // Search the WHOLE message for any OTHER interactive
                  // control besides the per-tile "Open picture" buttons —
                  // a separate "open album" wrapper, if one exists.
                  let msgRoot = tile;
                  hops = 0;
                  while (msgRoot && hops < 15) {
                    if (msgRoot.getAttribute && msgRoot.getAttribute('data-testid') && msgRoot.getAttribute('data-testid').startsWith('conv-msg-')) break;
                    msgRoot = msgRoot.parentElement;
                    hops++;
                  }
                  const otherControls = [];
                  if (msgRoot) {
                    const candidates = Array.from(msgRoot.querySelectorAll('[role="button"], [role="link"], button, a, [tabindex]'));
                    for (const c of candidates) {
                      if (c === tile) continue;
                      if (c.getAttribute('data-testid') === 'image-thumb') continue;
                      otherControls.push(describe(c));
                    }
                  }
                  const mediaAlbum = msgRoot ? msgRoot.querySelector('[data-testid="media-album"]') : null;
                  return {
                    tileDescribe: describe(tile),
                    ancestors,
                    clickPoint: [cx, cy],
                    elementsFromPointStack: stack,
                    otherInteractiveControlsInMessage: otherControls.slice(0, 20),
                    mediaAlbumContainer: mediaAlbum ? describe(mediaAlbum) : null,
                    getEventListenersAvailable: typeof window.getEventListeners === 'function',
                  };
                }
            """, timeout=15000)
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"inspection evaluate failed: {exc}"}]}
        return {"results": [{"ok": True, "data_id": data_id, "tile_index": tile_index, **inspection}], "session_identity": session_identity}

    if probe_type == "album_viewer_diagnostic":
        # Diagnostic-only (2026-08-23): the collapsed 2x2 preview only
        # shows 4 image-thumb slots (one an overflow placeholder) for an
        # album with more photos than that — this opens the album with
        # ONE real click and inspects whatever gallery/viewer mounts from
        # scratch, never uploads, never creates any request.
        result = await _diagnose_photo_album_viewer(page, group_name, data_id)
        result = _strip_raw_bytes(result)
        result["source_message_id"] = data_id
        result["session_identity"] = session_identity
        return {"results": [result]}

    if probe_type == "photo_thumb_hash_check":
        # Diagnostic-only (2026-08-23): full_message_inventory revealed a
        # message (media-album + image-thumb, img_count=8) that every
        # earlier probe had silently miscategorized, because those probes
        # only ever checked for image-content/video-content (the ALBUM-
        # TILE-GRID testids) — image-thumb is a DIFFERENT structure this
        # codebase has never hashed per-tile before. Mirrors
        # _hash_album_tiles_live's exact per-element outerHTML->smallest-
        # hash technique (proven immune to the whole-message-HTML-growth
        # bug) but targets [data-testid="image-thumb"] instead, to
        # determine whether these tiles carry the same kind of stable,
        # independently-hashable identity.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        thumbs = message.locator('[data-testid="image-thumb"]')
        try:
            n = await thumbs.count()
        except Exception as exc:
            return {"results": [{"ok": False, "error": f"count failed: {exc}"}]}
        tiles = []
        for i in range(n):
            thumb = thumbs.nth(i)
            entry: Dict[str, Any] = {"index": i}
            try:
                entry["rect"] = await thumb.evaluate("(el) => { const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }", timeout=10000)
            except Exception as exc:
                entry["rect"] = None
                entry["rect_error"] = str(exc)
            try:
                thumb_html = await thumb.evaluate("(el) => el.outerHTML", timeout=10000)
                entry["hash"] = _smallest_hash(thumb_html)
                entry["html_len"] = len(thumb_html)
            except Exception as exc:
                entry["hash"] = None
                entry["html_error"] = str(exc)
            tiles.append(entry)
        return {"results": [{"ok": True, "data_id": data_id, "tile_count": n, "tiles": tiles}], "session_identity": session_identity}

    if probe_type == "full_message_inventory":
        # Diagnostic-only (2026-08-23): the single-photo A/B test proved
        # media sync itself works (a plain photo arrived and rendered
        # correctly) while the 6-7-photo album still shows no trace at
        # all across every prior check. Rather than assume the album's
        # absence means "not received", this dumps EVERY currently-
        # rendered message node's COMPLETE testid inventory (not just the
        # image-content/video-content/media-album markers already known
        # to matter for a rendered album) — an unrendered/placeholder/
        # deferred representation would still need to be a message node
        # matching this same selector, so any such shell would appear
        # here even if it carries none of the markers already checked.
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        inventory = await _evaluate(page, """
            ([sel]) => {
              const els = Array.from(document.querySelectorAll(sel));
              return {
                total_rendered: els.length,
                messages: els.map((el, i) => {
                  const html = el.outerHTML;
                  const ct = el.querySelector('[data-pre-plain-text]');
                  const testidCounts = {};
                  (html.match(/data-testid="[^"]+"/g) || []).forEach(m => {
                    testidCounts[m] = (testidCounts[m] || 0) + 1;
                  });
                  return {
                    index: i,
                    data_id: el.getAttribute('data-id'),
                    pre_plain_text: ct ? ct.getAttribute('data-pre-plain-text') : null,
                    inner_text: (el.innerText || '').slice(0, 120),
                    html_len: html.length,
                    testid_counts: testidCounts,
                    img_count: el.querySelectorAll('img').length,
                  };
                }),
              };
            }
        """, [full_sel])
        return {"results": [inventory], "session_identity": session_identity}

    if probe_type == "raw_tail_ids":
        # Diagnostic (2026-08-23): a fresh 6-7-photo album didn't appear
        # anywhere in album_discovery's _dump_window-based scan (24 unique
        # messages total, none with any image-content tile) — bypasses
        # _dump_window/_DOM_DUMP_JS entirely (no per-message evaluate that
        # could silently except-and-skip a large message) and reads
        # exactly what's rendered in the DOM right now, to rule out
        # (or confirm) a scan-layer issue vs. the message genuinely not
        # being where expected.
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        raw = await _evaluate(page, """
            async ([sel]) => {
              const snapshot = () => {
                const els = Array.from(document.querySelectorAll(sel));
                let container = els[0] || null, maxOverflow = 0;
                let node = els[0] ? els[0].parentElement : null;
                for (let d = 0; d < 8 && node; d++) {
                  const overflow = node.scrollHeight - node.clientHeight;
                  if (overflow > maxOverflow) { maxOverflow = overflow; container = node; }
                  node = node.parentElement;
                }
                return {
                  total_rendered: els.length,
                  scrollTop: container ? container.scrollTop : null,
                  scrollHeight: container ? container.scrollHeight : null,
                  clientHeight: container ? container.clientHeight : null,
                  atBottom: container ? (container.scrollHeight - container.scrollTop - container.clientHeight) < 5 : null,
                  last_ids: els.slice(-15).map(el => ({
                    data_id: el.getAttribute('data-id'),
                    html_len: el.outerHTML.length,
                    image_content_count: (el.outerHTML.match(/data-testid="image-content"/g) || []).length,
                    video_content_count: (el.outerHTML.match(/data-testid="video-content"/g) || []).length,
                    media_album: el.outerHTML.includes('data-testid="media-album"'),
                  })),
                  _container: container,
                };
              };
              const before = snapshot();
              if (before._container) {
                before._container.scrollTop = before._container.scrollHeight;
                await new Promise(r => setTimeout(r, 1000));
              }
              const after = snapshot();
              delete before._container;
              delete after._container;
              return { before, after };
            }
        """, [full_sel])
        return {"results": [raw], "session_identity": session_identity}

    if probe_type == "album_discovery":
        # Diagnostic (2026-08-23): locates a freshly-sent, not-yet-marked
        # album in the group's current scan window without needing its
        # data-id in advance — reports every album-shaped message's tile
        # count/types/hashes so a specific one (e.g. the newest all-photo
        # album) can be identified before the photo-tile-viewer diagnostic.
        max_messages = req.get("max_messages") or MAX_MESSAGES_SCANNED_DEFAULT
        window = await _dump_window(page, group_name, max_messages)
        albums = []
        for item in window:
            html = item.get("messageHtml") or ""
            if not _is_album(html):
                continue
            tiles = _album_tile_hashes_and_types(html)
            albums.append({
                "data_id": _own_data_id(html),
                "tile_count": len(tiles),
                "tile_types": [t for _, t in tiles],
                "tile_hashes": [h for h, _ in tiles],
            })
        # Raw tail evidence regardless of _is_album's own match — 2026-08-23:
        # a photo-only album (unlike the previously-proven 4-video one)
        # might structure its DOM differently (e.g. an overflow "+N" tile
        # for >4 items); never assume _ALBUM_RE/grid-area chunking still
        # applies without checking the newest messages directly.
        raw_tail = []
        for item in window[-15:]:
            html = item.get("messageHtml") or ""
            raw_tail.append({
                "data_id": _own_data_id(html),
                "html_len": len(html),
                "is_album": _is_album(html),
                "image_content_count": len(re.findall(r'data-testid="image-content"', html)),
                "video_content_count": len(re.findall(r'data-testid="video-content"', html)),
                "grid_area_count": len(_GRID_AREA_RE.findall(html)),
                "html_snippet": html[:300],
            })
        # _run_download_probe's caller only forwards this function's
        # "results" key to the backend (see mark_scan_loop's download_probe
        # branch) — raw_tail/window_total are nested as one extra entry in
        # that same list rather than added as sibling top-level keys, so
        # they actually reach the stored scan_request doc for inspection.
        albums.append({"_diagnostic_raw_tail": raw_tail, "_diagnostic_window_total": len(window)})
        return {"results": albums, "session_identity": session_identity}

    if probe_type == "photo_tile_diagnostic":
        # Diagnostic (2026-08-23): does the video-viewer mechanism
        # generalize to a photo tile? Never assumed — see
        # _diagnose_photo_tile_open's own module note.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        result = await _diagnose_photo_tile_open(page, message, req["tile_index"])
        result = _strip_raw_bytes(result)
        result["source_message_id"] = data_id
        result["session_identity"] = session_identity
        return {"results": [result]}

    if probe_type == "quoted_jump":
        # Diagnostic (2026-08-23): proves _resolve_quoted_jump (used for
        # real by _run_scan's whole-album batch-mark resolution) against
        # one specific reply, for inspection.
        jump = await _resolve_quoted_jump(page, group_name, data_id)
        jump["session_identity"] = session_identity
        return {"results": [jump]}

    if probe_type == "tile_viewer":
        expected_hash = req["expected_tile_hash"]
        ident = await _identify_tile_index(page, group_name, data_id, expected_hash)
        if not ident.get("ok"):
            return {"results": [{"ok": False, "stage": "identify_tile", **ident, "session_identity": session_identity}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(ident["message_idx"])
        result = await _open_tile_viewer_and_download(page, message, ident["tile_index"])
        result["source_message_id"] = data_id
        result["tile_index"] = ident["tile_index"]
        result["expected_tile_hash"] = expected_hash
        result["session_identity"] = session_identity
        return {"results": [result]}

    if probe_type == "viewer_close_diagnostic":
        # Diagnostic (2026-08-23): the real E2E's Take 2/3/Introduction all
        # failed identically opening their tile after Take 1's viewer
        # successfully downloaded — never assumed why; captures real DOM/
        # style evidence around the open->download->close transition.
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        result = await _diagnose_viewer_close_lifecycle(page, message, req["tile1_index"], req["tile2_index"])
        result["session_identity"] = session_identity
        return {"results": [result]}

    if probe_type == "album_lifecycle_diagnostic":
        # Diagnostic (2026-08-23): after the sequential 4-tile test, tiles
        # 2/3 failed with "expected tile hash not found among current
        # album tiles" - a fresh re-scan independently confirmed the
        # album's own DOM only exposes 2 of 4 tiles afterward. Investigates
        # WHY before/after tile1/after tile2, never assuming the tiles
        # vanished from WhatsApp itself.
        result = await _diagnose_album_lifecycle(page, group_name, data_id, req["known_tile_hashes"])
        result["session_identity"] = session_identity
        return {"results": [result]}

    async def _relocate():
        idx = await _find_message_index_by_data_id(page, group_name, data_id)
        if idx is None:
            return None
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)
        # The message ROW (conv-msg-*) spans the full conversation width;
        # a right-click at its geometric center can land in empty space
        # beside the actual bubble rather than on any content at all —
        # confirmed directly (2026-08-23): a raw event-capture diagnostic
        # showed mousedown/contextmenu/mouseup/auxclick all fired correctly
        # at that center point with defaultPrevented=false, meaning nothing
        # in WhatsApp's own JS claimed the event — consistent with hitting
        # unclaimed row background, not any handler intentionally ignoring
        # it. Target the actual visible content instead, reusing the same
        # selectors already proven for album/media structural detection.
        album = message.locator('[data-testid="media-album"]')
        if await album.count() > 0:
            return album.first
        media = message.locator('[data-testid="video-content"], [data-testid="image-content"]')
        if await media.count() > 0:
            return media.first
        return message

    first = await _relocate()
    if first is None:
        return {"results": [{"ok": False, "error": f"message {data_id!r} not found in scanned window"}]}

    result = await _right_click_and_probe_download(page, _relocate)
    result["source_message_id"] = data_id
    result["session_identity"] = session_identity
    return {"results": [result]}


# ---------------------------------------------------------------------------
# Photo-tile viewer diagnostic (2026-08-23) — the video-tile mechanism
# (_open_tile_viewer_and_download) hard-anchors on a <video> element
# mounting; a photo tile has none, so it cannot simply be reused. This
# never assumes photos behave like videos: it diffs the document's
# blob: <img> elements before/after clicking a specific tile to find
# whichever one is newly mounted (if any), then inspects THAT element's
# own ancestor chain for Close/Download controls, exactly the same
# "dump real buttons, never guess a selector" discipline already proven
# for video.
# ---------------------------------------------------------------------------
_PHOTO_BLOB_BASELINE_JS = "() => Array.from(document.querySelectorAll('img[src^=\"blob:\"]')).map(img => img.src)"

_PHOTO_VIEWER_PROBE_JS = """
async ([baselineSrcs]) => {
  const baseline = new Set(baselineSrcs);
  const videoCount = document.querySelectorAll('video').length;
  const allImgs = Array.from(document.querySelectorAll('img[src^="blob:"]'));
  const newImgs = allImgs.filter(img => !baseline.has(img.src));
  const info = (img) => {
    const r = img.getBoundingClientRect();
    return { src: img.src, rect: [r.x, r.y, r.width, r.height], naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight };
  };
  let candidateEl = null;
  if (newImgs.length > 0) {
    candidateEl = newImgs.reduce((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) >= (rb.width * rb.height) ? a : b;
    });
  }
  let fetchResult = null;
  if (candidateEl) {
    try {
      const resp = await fetch(candidateEl.src);
      const buf = await resp.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let binary = '';
      const chunkSize = 0x8000;
      for (let i = 0; i < bytes.length; i += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
      }
      fetchResult = { ok: true, base64: btoa(binary), contentType: resp.headers.get('content-type') || '' };
    } catch (e) {
      fetchResult = { ok: false, reason: String(e && e.message || e) };
    }
  }
  let buttons = [];
  let rootInfo = null;
  if (candidateEl) {
    let root = candidateEl;
    while (root.parentElement && root.parentElement !== document.body) {
      root = root.parentElement;
    }
    if (root.parentElement) {
      rootInfo = { tag: root.tagName, id: root.id || null, testid: root.getAttribute('data-testid') };
      buttons = Array.from(root.querySelectorAll('button, [role="button"]')).map(b => {
        const r = b.getBoundingClientRect();
        const svgTitle = b.querySelector('svg title');
        return {
          ariaLabel: b.getAttribute('aria-label'), dataIcon: b.getAttribute('data-icon'),
          testid: b.getAttribute('data-testid'), svgTitle: svgTitle ? svgTitle.textContent : null,
          rect: [r.x, r.y, r.width, r.height],
        };
      });
    }
  }
  return {
    videoCount, totalBlobImgCount: allImgs.length, newImgCount: newImgs.length,
    candidate: candidateEl ? info(candidateEl) : null, fetch: fetchResult,
    buttons, rootInfo,
  };
}
"""

_PHOTO_STILL_PRESENT_JS = "([src]) => Array.from(document.querySelectorAll('img[src^=\"blob:\"]')).some(img => img.src === src)"


async def _close_photo_viewer(page, viewer_buttons: Dict[str, Any], candidate_src: str) -> Dict[str, Any]:
    """Same real-Close-button-first, verify-don't-trust discipline as
    _close_viewer, but verifies via the candidate blob: <img> disappearing
    rather than a <video> count (there is none for a photo viewer)."""
    close_btn = None
    for b in (viewer_buttons or {}).get("buttons", []):
        label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
        if re.search(r"close|back|dismiss", label):
            close_btn = b
            break
    if close_btn is None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    else:
        cx = close_btn["rect"][0] + close_btn["rect"][2] / 2
        cy = close_btn["rect"][1] + close_btn["rect"][3] / 2
        try:
            await page.mouse.click(cx, cy, button="left")
        except Exception as exc:
            return {"closed": False, "reason": f"close button click failed: {exc}", "used_close_button": True}

    elapsed = 0.0
    while elapsed < 8.0:
        try:
            still_present = await _evaluate(page, _PHOTO_STILL_PRESENT_JS, [candidate_src])
        except Exception:
            still_present = True
        if not still_present:
            return {"closed": True, "elapsed_s": elapsed, "used_close_button": close_btn is not None}
        await page.wait_for_timeout(300)
        elapsed += 0.3
    return {
        "closed": False, "reason": "candidate image still present 8s after close attempt",
        "used_close_button": close_btn is not None,
    }


async def _diagnose_photo_tile_open(page, message_locator, tile_index: int) -> Dict[str, Any]:
    """Diagnostic-only: determines whether opening a PHOTO tile inside a
    multi-photo album mounts a full-resolution viewer the same proven
    architecture (identify -> open -> download -> close) can be adapted
    to, WITHOUT assuming it behaves like the video path. Reports real
    evidence either way; never guesses a selector that wasn't observed."""
    tile = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile_index)
    try:
        await tile.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        baseline_srcs = await _evaluate(page, _PHOTO_BLOB_BASELINE_JS)
    except Exception:
        baseline_srcs = []
    try:
        await _evaluate(page, _EVENT_CAPTURE_INSTALL_JS)
    except Exception:
        pass
    try:
        await tile.click(timeout=10000)
    except Exception as exc:
        return {"ok": False, "stage": "open_tile", "reason": f"click failed: {exc}"}

    probe = None
    elapsed = 0.0
    while elapsed < 10.0:
        try:
            probe = await _evaluate(page, _PHOTO_VIEWER_PROBE_JS, [baseline_srcs])
        except Exception as exc:
            probe = {"error": str(exc)}
        if probe and probe.get("candidate"):
            break
        await page.wait_for_timeout(500)
        elapsed += 0.5

    try:
        click_event_log = await _evaluate(page, _EVENT_CAPTURE_READ_JS)
    except Exception:
        click_event_log = None

    if not probe or not probe.get("candidate"):
        try:
            body_snapshot = await _evaluate(page, _BODY_SNAPSHOT_JS)
        except Exception:
            body_snapshot = None
        return {
            "ok": False, "stage": "open_tile",
            "reason": "no new blob: <img> mounted within 10s of clicking the photo tile",
            "video_count": (probe or {}).get("videoCount"), "click_event_log": click_event_log,
            "body_snapshot": body_snapshot, "last_probe": probe,
        }

    candidate = probe["candidate"]
    fetch_result = probe.get("fetch") or {}
    raw_bytes = b64mod.b64decode(fetch_result["base64"]) if fetch_result.get("ok") and fetch_result.get("base64") else None
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None
    detected_mime = _detect_mime_type(raw_bytes, "image") if raw_bytes else None

    close_result = await _close_photo_viewer(page, {"buttons": probe.get("buttons", [])}, candidate["src"])

    return {
        "ok": True, "tile_index": tile_index,
        "video_count_during_view": probe.get("videoCount"),
        "total_blob_img_count": probe.get("totalBlobImgCount"),
        "new_img_count": probe.get("newImgCount"),
        "candidate_rect": candidate.get("rect"),
        "candidate_natural_size": [candidate.get("naturalWidth"), candidate.get("naturalHeight")],
        "root_info": probe.get("rootInfo"),
        "buttons": probe.get("buttons"),
        "fetch_ok": fetch_result.get("ok"),
        "fetch_error": fetch_result.get("reason"),
        "byte_length": len(raw_bytes) if raw_bytes else None,
        "sha256": sha256_hex,
        "detected_mime": detected_mime,
        "close_result": close_result,
        "_raw_bytes": raw_bytes,
    }


# ---------------------------------------------------------------------------
# Full album-viewer/gallery diagnostic (2026-08-23) — a photo album's inline
# preview only exposes a collapsed 2x2 grid (image-thumb, +N overflow); the
# COMPLETE album has never been inspected. Never assumes this gallery
# behaves like the single-tile video viewer — every stage dumps real DOM
# evidence (buttons, images, innerText) from whatever new root actually
# appears, the same diagnostic-first discipline already proven for the
# video path and the single-photo-tile path.
# ---------------------------------------------------------------------------
_ALBUM_VIEWER_SNAPSHOT_JS = """
async ([baselineSrcs, fetchBytes]) => {
  const baseline = new Set(baselineSrcs);
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  const allImgs = Array.from(document.querySelectorAll('img[src^="blob:"]'));
  const newImgs = allImgs.filter(img => !baseline.has(img.src));

  let root = null;
  if (dialogs.length > 0) {
    root = dialogs.reduce((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) >= (rb.width * rb.height) ? a : b;
    });
  } else if (newImgs.length > 0) {
    let el = newImgs[0];
    while (el.parentElement && el.parentElement !== document.body) el = el.parentElement;
    root = el.parentElement ? el : null;
  }
  if (!root) {
    return { found: false, dialogCount: dialogs.length, newImgCount: newImgs.length, totalImgCount: allImgs.length };
  }

  const rootImgs = Array.from(root.querySelectorAll('img[src^="blob:"]'));
  const imgInfo = (img) => {
    const r = img.getBoundingClientRect();
    return { src: img.src, rect: [r.x, r.y, r.width, r.height], naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight };
  };
  const buttons = Array.from(root.querySelectorAll('button, [role="button"]')).map(b => {
    const r = b.getBoundingClientRect();
    const svgTitle = b.querySelector('svg title');
    return {
      ariaLabel: b.getAttribute('aria-label'), dataIcon: b.getAttribute('data-icon'),
      testid: b.getAttribute('data-testid'), svgTitle: svgTitle ? svgTitle.textContent : null,
      rect: [r.x, r.y, r.width, r.height],
    };
  });

  // The primary displayed photo is whichever blob img in the root has the
  // largest rendered area (the full-res view, not a filmstrip thumbnail).
  let primary = null;
  if (rootImgs.length > 0) {
    primary = rootImgs.reduce((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) >= (rb.width * rb.height) ? a : b;
    });
  }

  let fetchResult = null;
  if (primary && fetchBytes) {
    try {
      const resp = await fetch(primary.src);
      const buf = await resp.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let binary = '';
      const chunkSize = 0x8000;
      for (let i = 0; i < bytes.length; i += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
      }
      fetchResult = { ok: true, base64: btoa(binary), contentType: resp.headers.get('content-type') || '' };
    } catch (e) {
      fetchResult = { ok: false, reason: String(e && e.message || e) };
    }
  }

  const rootRect = root.getBoundingClientRect();
  return {
    found: true,
    dialogCount: dialogs.length,
    rootTag: root.tagName, rootTestid: root.getAttribute('data-testid'), rootRole: root.getAttribute('role'),
    rootRect: [rootRect.x, rootRect.y, rootRect.width, rootRect.height],
    rootInnerTextExcerpt: (root.innerText || '').slice(0, 200),
    rootImgCount: rootImgs.length,
    rootImgs: rootImgs.slice(0, 12).map(imgInfo),
    primary: primary ? imgInfo(primary) : null,
    fetch: fetchResult,
    buttons,
  };
}
"""


async def _fetch_and_hash_blob(page, src: str) -> Dict[str, Any]:
    """Fetches one already-known blob: URL's bytes directly (no new DOM
    diffing needed — the src itself is the identity), for re-fetching a
    SPECIFIC previously-observed image (e.g. after navigating away and
    wanting the first photo's bytes again) without re-running the full
    viewer snapshot."""
    try:
        result = await _evaluate(page, """
            async ([src]) => {
              try {
                const resp = await fetch(src);
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                  binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                }
                return { ok: true, base64: btoa(binary) };
              } catch (e) {
                return { ok: false, reason: String(e && e.message || e) };
              }
            }
        """, [src])
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    return result


def _hash_and_mime(base64_data: Optional[str]) -> Dict[str, Any]:
    if not base64_data:
        return {"byte_length": None, "sha256": None, "detected_mime": None}
    raw = b64mod.b64decode(base64_data)
    return {
        "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "detected_mime": _detect_mime_type(raw, "image"), "_raw_bytes": raw,
    }


async def _diagnose_photo_album_viewer(page, group_name: str, data_id: str) -> Dict[str, Any]:
    """Full read-only diagnostic: records the album's collapsed preview
    state, opens it with one real click, inspects whatever viewer/gallery
    mounts from scratch (never assuming the video-viewer's shape),
    identifies + hashes two distinct photos, probes the download
    mechanism, then closes it — verifying the original message DOM is
    unchanged afterward. Never uploads, never creates any request."""
    idx = await _find_message_index_by_data_id(page, group_name, data_id)
    if idx is None:
        return {"ok": False, "stage": "locate_message", "reason": f"message {data_id!r} not found in scanned window"}
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    message = page.locator(full_sel).nth(idx)

    try:
        message_html_before = await message.evaluate("(el) => el.outerHTML", timeout=10000)
    except Exception as exc:
        return {"ok": False, "stage": "capture_before", "reason": str(exc)}

    overflow_match = re.search(r"\+(\d+)", message_html_before)
    thumbs = message.locator('[data-testid="image-thumb"]')
    try:
        thumb_count_before = await thumbs.count()
    except Exception:
        thumb_count_before = 0
    tiles_before = []
    for i in range(thumb_count_before):
        thumb = thumbs.nth(i)
        try:
            thumb_html = await thumb.evaluate("(el) => el.outerHTML", timeout=10000)
            tiles_before.append({"index": i, "hash": _smallest_hash(thumb_html)})
        except Exception as exc:
            tiles_before.append({"index": i, "hash": None, "error": str(exc)})

    try:
        baseline_srcs = await _evaluate(page, _PHOTO_BLOB_BASELINE_JS)
    except Exception:
        baseline_srcs = []

    async def _try_open(tile_locator, label: str) -> Dict[str, Any]:
        try:
            await _evaluate(page, _EVENT_CAPTURE_INSTALL_JS)
        except Exception:
            pass
        # Local-only "click"/pointer capture with full bubble-path — the
        # shared _EVENT_CAPTURE_INSTALL_JS only listens for mousedown/
        # mouseup/contextmenu/auxclick, none of which prove whether a real
        # "click" event fired or was stopped somewhere along its path to
        # whatever handler WhatsApp attaches it to.
        try:
            await _evaluate(page, """
                () => {
                  window.__clickProbe = [];
                  const describe = (t) => t ? { tag: t.tagName, testid: t.getAttribute && t.getAttribute('data-testid'), role: t.getAttribute && t.getAttribute('role') } : null;
                  const record = (e) => {
                    const path = (e.composedPath ? e.composedPath() : []).slice(0, 8).map(describe);
                    window.__clickProbe.push({ type: e.type, isTrusted: e.isTrusted, defaultPrevented: e.defaultPrevented, target: describe(e.target), path });
                  };
                  if (!window.__clickProbeInstalled) {
                    document.addEventListener('pointerdown', record, true);
                    document.addEventListener('pointerup', record, true);
                    document.addEventListener('click', record, true);
                    window.__clickProbeInstalled = true;
                  }
                }
            """)
        except Exception:
            pass
        try:
            await tile_locator.scroll_into_view_if_needed(timeout=5000)
            await tile_locator.click(timeout=10000)
        except Exception as exc:
            return {"clicked": False, "click_error": str(exc), "label": label}
        try:
            click_event_log = await _evaluate(page, _EVENT_CAPTURE_READ_JS)
        except Exception:
            click_event_log = None
        try:
            click_probe_log = await _evaluate(page, "() => { const ev = window.__clickProbe || []; window.__clickProbe = []; return ev; }")
        except Exception:
            click_probe_log = None
        viewer_result = None
        elapsed = 0.0
        while elapsed < 6.0:
            try:
                viewer_result = await _evaluate(page, _ALBUM_VIEWER_SNAPSHOT_JS, [baseline_srcs, True])
            except Exception as exc:
                viewer_result = {"found": False, "error": str(exc)}
            if viewer_result.get("found"):
                break
            await page.wait_for_timeout(500)
            elapsed += 0.5
        return {"clicked": True, "label": label, "click_event_log": click_event_log, "click_probe_log": click_probe_log, "viewer": viewer_result}

    attempt1 = await _try_open(thumbs.first, "first_tile(index_0)")
    viewer = (attempt1.get("viewer") or {}) if attempt1.get("clicked") else {}
    attempts = [attempt1]

    if not viewer.get("found"):
        # The overlay ("+N") badge is drawn over the LAST visible tile in
        # WhatsApp's collapsed preview grid — clicking index 0 may not be
        # the control that opens the full gallery. Try the last tile as a
        # distinct, reasoned second attempt (not a blind retry of the same
        # action) before concluding nothing opens at all.
        last_index = max(thumb_count_before - 1, 0)
        attempt2 = await _try_open(thumbs.nth(last_index), f"last_tile(index_{last_index})")
        attempts.append(attempt2)
        if attempt2.get("clicked"):
            viewer = attempt2.get("viewer") or {}

    if not viewer.get("found"):
        try:
            body_snapshot = await _evaluate(page, _BODY_SNAPSHOT_JS)
        except Exception:
            body_snapshot = None
        return {
            "ok": False, "stage": "open_album",
            "reason": "no viewer/gallery detected after trying both the first and last preview tile",
            "tiles_before": tiles_before, "overflow_badge": overflow_match.group(1) if overflow_match else None,
            "attempts": attempts, "body_snapshot": body_snapshot,
        }

    photo1_fetch = viewer.get("fetch") or {}
    photo1 = {
        "rect": (viewer.get("primary") or {}).get("rect"),
        "natural_size": [(viewer.get("primary") or {}).get("naturalWidth"), (viewer.get("primary") or {}).get("naturalHeight")],
        "src_present": bool(viewer.get("primary")),
        **_hash_and_mime(photo1_fetch.get("base64") if photo1_fetch.get("ok") else None),
    }
    # Note: the preview thumbnail hash and the full-resolution viewer
    # image are different assets (different resolutions) — no direct
    # hash-equality check against tiles_before is attempted here; the
    # report notes both hashes so the caller can judge identity linkage.
    download_button_found = None
    for b in viewer.get("buttons", []):
        label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
        if re.search(r"download", label):
            download_button_found = b
            break

    native_download = None
    if download_button_found:
        async def _click_download():
            cx = download_button_found["rect"][0] + download_button_found["rect"][2] / 2
            cy = download_button_found["rect"][1] + download_button_found["rect"][3] / 2
            await page.mouse.click(cx, cy, button="left")
        try:
            downloads = await _collect_downloads(page, _click_download, window_s=10.0, quiet_s=2.0)
            native_download = {"triggered": True, "download_event_count": len(downloads)}
            if downloads:
                try:
                    path = await downloads[0].path()
                    native_download["saved_path"] = str(path) if path else None
                    if path:
                        with open(path, "rb") as fh:
                            nd_bytes = fh.read()
                        native_download["byte_length"] = len(nd_bytes)
                        native_download["sha256"] = hashlib.sha256(nd_bytes).hexdigest()
                except Exception as exc:
                    native_download["read_error"] = str(exc)
        except Exception as exc:
            native_download = {"triggered": False, "error": str(exc)}

    # Navigate to a second photo — look for a next/right/forward-labeled
    # control among the SAME buttons already dumped; fall back to
    # ArrowRight (a real, common gallery keyboard shortcut) if none found.
    next_button = None
    for b in viewer.get("buttons", []):
        label = " ".join(filter(None, [b.get("ariaLabel"), b.get("dataIcon"), b.get("svgTitle"), b.get("testid")])).lower()
        if re.search(r"next|right|forward|chevron.?right|arrow.?right", label):
            next_button = b
            break
    nav_method = None
    if next_button:
        cx = next_button["rect"][0] + next_button["rect"][2] / 2
        cy = next_button["rect"][1] + next_button["rect"][3] / 2
        try:
            await page.mouse.click(cx, cy, button="left")
            nav_method = "button_click"
        except Exception as exc:
            nav_method = f"button_click_failed:{exc}"
    else:
        try:
            await page.keyboard.press("ArrowRight")
            nav_method = "arrow_right_key"
        except Exception as exc:
            nav_method = f"arrow_right_failed:{exc}"

    await page.wait_for_timeout(1200)
    try:
        viewer2 = await _evaluate(page, _ALBUM_VIEWER_SNAPSHOT_JS, [baseline_srcs, True])
    except Exception as exc:
        viewer2 = {"found": False, "error": str(exc)}

    photo2_fetch = viewer2.get("fetch") or {}
    photo2 = {
        "rect": (viewer2.get("primary") or {}).get("rect"),
        "natural_size": [(viewer2.get("primary") or {}).get("naturalWidth"), (viewer2.get("primary") or {}).get("naturalHeight")],
        "src_present": bool(viewer2.get("primary")),
        "same_src_as_photo1": (viewer2.get("primary") or {}).get("src") == (viewer.get("primary") or {}).get("src"),
        **_hash_and_mime(photo2_fetch.get("base64") if photo2_fetch.get("ok") else None),
    }

    close_result = await _close_photo_viewer(
        page, {"buttons": viewer2.get("buttons") or viewer.get("buttons", [])},
        (viewer2.get("primary") or viewer.get("primary") or {}).get("src", ""),
    )

    try:
        message_html_after = await message.evaluate("(el) => el.outerHTML", timeout=10000)
    except Exception as exc:
        message_html_after = None
    thumb_count_after = None
    tiles_after = []
    try:
        thumb_count_after = await thumbs.count()
        for i in range(thumb_count_after):
            thumb = thumbs.nth(i)
            thumb_html = await thumb.evaluate("(el) => el.outerHTML", timeout=10000)
            tiles_after.append({"index": i, "hash": _smallest_hash(thumb_html)})
    except Exception:
        pass

    return {
        "ok": True,
        "overflow_badge": overflow_match.group(1) if overflow_match else None,
        "tiles_before": tiles_before,
        "attempts": attempts,
        "viewer_root": {
            "tag": viewer.get("rootTag"), "testid": viewer.get("rootTestid"), "role": viewer.get("rootRole"),
            "rect": viewer.get("rootRect"), "dialog_count": viewer.get("dialogCount"),
            "innerText_excerpt": viewer.get("rootInnerTextExcerpt"),
        },
        "viewer_root_img_count": viewer.get("rootImgCount"),
        "viewer_buttons": viewer.get("buttons"),
        "download_button_found": download_button_found,
        "native_download": native_download,
        "photo1": photo1,
        "nav_method": nav_method,
        "photo2": photo2,
        "photos_distinct": (photo1.get("sha256") != photo2.get("sha256")) if photo1.get("sha256") and photo2.get("sha256") else None,
        "close_result": close_result,
        "message_unchanged_after": (message_html_before == message_html_after) if message_html_after else None,
        "thumb_count_before": thumb_count_before,
        "thumb_count_after": thumb_count_after,
        "tiles_after": tiles_after,
        "_raw_bytes_photo1": photo1.pop("_raw_bytes", None),
        "_raw_bytes_photo2": photo2.pop("_raw_bytes", None),
    }


# MIME detection from actual bytes (2026-08-23) — the disposable E2E's
# first real upload failed with "MIME type header does not match detected
# file signature": _upload_one was passing an empty content_type through
# for video downloads, which defaulted to generic application/octet-
# stream, and the backend's own (deliberately strict, unweakened here)
# signature validation correctly rejected the mismatch against the real
# MP4 bytes. Detects from the file's own magic bytes — never trusts the
# WhatsApp media_type label or a file extension alone; the label is used
# only to decide which signature family to report if none match (an
# honest "we don't know" rather than a specific type we can't verify,
# which the backend's own validation will then reject on its own terms,
# exactly as it should for genuinely anomalous bytes).
_MIME_BY_EXT = {"video/mp4": "mp4", "video/webm": "webm", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _detect_mime_type(data: bytes, media_type_hint: Optional[str] = None) -> str:
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "video/mp4" if media_type_hint == "video" else "application/octet-stream"


def _parse_image_dimensions(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Dependency-free width/height parser for JPEG/PNG/WEBP (the worker
    has no Pillow/ImageMagick installed) — reads real file structure, not
    any DOM-reported value, so it's an independent cross-check against the
    browser's own naturalWidth/naturalHeight rather than trusting it."""
    if not data:
        return None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > n:
                break
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 <= n:
                    height = int.from_bytes(data[i + 5:i + 7], "big")
                    width = int.from_bytes(data[i + 7:i + 9], "big")
                    return width, height
            if marker == 0xDA:
                break
            i += 2 + seg_len
        return None, None
    if data[:4] == b"RIFF" and len(data) >= 30 and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
    return None, None


_BLOB_FETCH_JS = """
async ([src]) => {
  try {
    const resp = await fetch(src);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
    }
    return { ok: true, status: resp.status, base64: btoa(binary), contentType: resp.headers.get('content-type') || '' };
  } catch (e) {
    return { ok: false, reason: String(e && e.message || e) };
  }
}
"""

_LARGEST_IMG_JS = """
(tile) => {
  const imgs = Array.from(tile.querySelectorAll('img'));
  if (imgs.length === 0) return null;
  const best = imgs.reduce((a, b) => (a.naturalWidth * a.naturalHeight) >= (b.naturalWidth * b.naturalHeight) ? a : b);
  return { src: best.src, naturalWidth: best.naturalWidth, naturalHeight: best.naturalHeight, complete: best.complete };
}
"""


async def _download_photo_album_tile_via_blob(message_locator, page, tile_index: Optional[int], expected_hash: str) -> Dict[str, Any]:
    """Downloads ONE exact photo album tile via its own already-loaded
    full-resolution blob: URL — proven live (2026-08-24) via
    blob_tile_download_diagnostic: every image-thumb tile's larger <img>
    is already a complete, full-resolution blob, no gallery/viewer needed
    at all. `tile_index` (from the original scan) is used ONLY as a
    navigation hint — the tried-first candidate — never as identity: this
    re-hashes tiles live and requires an EXACT match to `expected_hash`
    (source_thumbnail_hash) before touching anything, falling back to
    scanning every tile in the album if the hinted index no longer holds
    the expected content (WhatsApp can reflow/append to an album's DOM
    after interaction — the same class of bug already root-caused for the
    video path this session). Never substitutes another tile: if no tile
    matches, this fails cleanly rather than guessing."""
    thumbs = message_locator.locator('[data-testid="image-thumb"]')
    try:
        count = await thumbs.count()
    except Exception as exc:
        return {"ok": False, "stage": "locate_tiles", "reason": str(exc)}
    if count == 0:
        return {"ok": False, "stage": "locate_tiles", "reason": "message has no image-thumb tiles"}

    hint = tile_index if (tile_index is not None and 0 <= tile_index < count) else None
    order = ([hint] if hint is not None else []) + [i for i in range(count) if i != hint]

    matched_tile = None
    matched_index = None
    checked: List[Dict[str, Any]] = []
    for i in order:
        tile = thumbs.nth(i)
        try:
            tile_html = await tile.evaluate("(el) => el.outerHTML", timeout=10000)
        except Exception as exc:
            checked.append({"index": i, "error": str(exc)})
            continue
        live_hash = _smallest_hash(tile_html)
        checked.append({"index": i, "hash": live_hash})
        if live_hash == expected_hash:
            matched_tile = tile
            matched_index = i
            break

    if matched_tile is None:
        return {
            "ok": False, "stage": "hash_match",
            "reason": f"no tile among {count} matched expected_hash {expected_hash!r}",
            "hint_index": tile_index, "checked": checked,
        }

    try:
        full_res = await matched_tile.evaluate(_LARGEST_IMG_JS, timeout=10000)
    except Exception as exc:
        return {"ok": False, "stage": "find_full_res", "reason": str(exc), "matched_tile_index": matched_index}
    if not full_res:
        return {"ok": False, "stage": "find_full_res", "reason": "no <img> found in matched tile", "matched_tile_index": matched_index}
    if not (full_res.get("src") or "").startswith("blob:"):
        return {"ok": False, "stage": "verify_blob", "reason": "full-res image src is not a blob: URL", "matched_tile_index": matched_index, "full_res": full_res}
    if not full_res.get("complete"):
        return {"ok": False, "stage": "verify_complete", "reason": "full-res image is not complete", "matched_tile_index": matched_index, "full_res": full_res}
    if not full_res.get("naturalWidth") or not full_res.get("naturalHeight"):
        return {"ok": False, "stage": "verify_dimensions", "reason": "full-res image has invalid/zero dimensions", "matched_tile_index": matched_index, "full_res": full_res}

    try:
        fetch_result = await _evaluate(page, _BLOB_FETCH_JS, [full_res["src"]])
    except Exception as exc:
        return {"ok": False, "stage": "fetch", "reason": str(exc), "matched_tile_index": matched_index}
    if not fetch_result.get("ok"):
        return {"ok": False, "stage": "fetch", "reason": fetch_result.get("reason"), "matched_tile_index": matched_index}
    raw = b64mod.b64decode(fetch_result["base64"]) if fetch_result.get("base64") else b""
    if not raw:
        return {"ok": False, "stage": "fetch", "reason": "downloaded zero bytes", "matched_tile_index": matched_index}

    detected_mime = _detect_mime_type(raw, "image")
    if not detected_mime.startswith("image/"):
        return {
            "ok": False, "stage": "mime_validate",
            "reason": f"detected MIME {detected_mime!r} is not an image type",
            "matched_tile_index": matched_index, "byte_length": len(raw),
        }

    parsed_w, parsed_h = _parse_image_dimensions(raw)
    if parsed_w != full_res.get("naturalWidth") or parsed_h != full_res.get("naturalHeight"):
        return {
            "ok": False, "stage": "dimension_cross_check",
            "reason": "parsed byte dimensions do not match the DOM's naturalWidth/naturalHeight",
            "matched_tile_index": matched_index,
            "parsed": [parsed_w, parsed_h], "dom": [full_res.get("naturalWidth"), full_res.get("naturalHeight")],
        }

    return {
        "ok": True, "matched_tile_index": matched_index, "expected_hash": expected_hash,
        "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "detected_mime": detected_mime, "content_type": fetch_result.get("contentType"),
        "parsed_width": parsed_w, "parsed_height": parsed_h,
        "_raw_bytes": raw,
    }


async def _upload_one(http: httpx.AsyncClient, target: Dict[str, Any], base64_data: str, content_type: str) -> Dict[str, Any]:
    raw = b64mod.b64decode(base64_data)
    # content_type (the caller's hint, e.g. from a blob: fetch's own HTTP
    # response header) is only a fallback — the bytes' own signature is
    # authoritative whenever a known one is detected.
    detected = _detect_mime_type(raw, target.get("source_media_type"))
    if detected == "application/octet-stream" and content_type:
        detected = content_type
    ext = _MIME_BY_EXT.get(detected, "bin")
    files = {"file": (f"{target['source_message_id']}.{ext}", raw, detected)}
    data = {
        "talent_id": target["talent_id"], "project_id": target["project_id"],
        "media_role": target["media_role"],
        "take_number": str(target["take_number"]) if target.get("take_number") else "",
        "original_label": target["original_label"],
        "source_message_id": target["source_message_id"],
        "source_thumbnail_hash": target.get("source_thumbnail_hash") or "",
        "source_media_type": target.get("source_media_type") or "",
        "source_sender": target.get("source_sender") or "",
        "source_timestamp": target.get("source_timestamp") or "",
        "mark_reply_message_id": target.get("mark_reply_message_id") or "",
        "mark_reply_text": target.get("mark_reply_text") or "",
        "mark_target_contact_id": target.get("mark_target_contact_id") or "",
    }
    resp = await http.post(f"{BASE}/media-upload", data=data, files=files, headers=_auth_headers(), timeout=120.0)
    if resp.status_code >= 400:
        return {"ok": False, "source_message_id": target["source_message_id"], "error": resp.text[:300]}
    return {"ok": True, "source_message_id": target["source_message_id"]}


async def _run_download(page, http: httpx.AsyncClient, req: Dict[str, Any]) -> Dict[str, Any]:
    group_name = req["group_name"]
    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"error": f"Could not open WhatsApp group {group_name!r} (status={status})"}

    results = []
    for target in req.get("download_targets") or []:
        # Re-verify the conversation is still the right one before each
        # target — a prior tile's viewer-open/Escape cycle can leave the
        # DOM in a state where a fresh index lookup misses (2026-08-23
        # bug); this fast-paths straight through when already fine.
        status = await sender._open_group_chat(page, group_name)
        if status != "OPENED":
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": f"group not open (status={status})"})
            continue
        idx = await _find_message_index_by_data_id(page, group_name, target["source_message_id"])
        if idx is None:
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "source message no longer found in window"})
            continue
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        message = page.locator(full_sel).nth(idx)

        if target.get("source_media_type") == "video":
            # Proven mechanism (2026-08-23): open the tile/message's own
            # viewer, wait for the actual <video> to buffer its FULL
            # duration (readyState alone is not sufficient — see
            # _wait_for_video_readiness), use the viewer's own native
            # "Download" item. Tested 4/4 on a real album, restart-stable.
            # Works uniformly for a single (non-album) video message too —
            # tile_index 0 addresses that message's own sole media element.
            tile_index = target.get("album_tile_index")
            if tile_index is None:
                tile_index = 0
            dl = await _open_tile_viewer_and_download(page, message, tile_index)
            if not dl.get("ok"):
                results.append(_strip_raw_bytes({
                    "ok": False, "source_message_id": target["source_message_id"],
                    "error": dl.get("reason") or f"failed at stage {dl.get('stage')}", "detail": dl,
                }))
                continue
            downloads = dl.get("downloads") or []
            raw = next((d.get("_raw_bytes") for d in downloads if d.get("ok") and d.get("_raw_bytes")), None)
            if not raw:
                results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "downloaded zero bytes"})
                continue
            b64 = b64mod.b64encode(raw).decode()
            upload_result = await _upload_one(http, target, b64, "")
            upload_result["byte_length"] = len(raw)
            results.append(upload_result)
            continue

        if target.get("album_tile_index") is not None:
            # Photo album tile (2026-08-24) — WhatsApp's gallery viewer
            # never mounts for a media-album (proven exhaustively: real
            # trusted clicks, keyboard activation, byte-identical DOM/CSS
            # to a working single photo — root cause is inside WhatsApp's
            # own non-DOM runtime, not anything fixable here). Bypasses
            # the viewer entirely: each image-thumb tile's full-resolution
            # photo is already a complete, loaded blob: URL in its own
            # DOM — fetched directly, with hash-based tile identity
            # re-verified live (never trusted from album_tile_index alone).
            dl = await _download_photo_album_tile_via_blob(
                message, page, target.get("album_tile_index"), target["source_thumbnail_hash"],
            )
            if not dl.get("ok"):
                results.append(_strip_raw_bytes({
                    "ok": False, "source_message_id": target["source_message_id"],
                    "error": dl.get("reason") or f"failed at stage {dl.get('stage')}", "detail": dl,
                }))
                continue
            raw = dl.get("_raw_bytes")
            if not raw:
                results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "downloaded zero bytes"})
                continue
            b64 = b64mod.b64encode(raw).decode()
            upload_result = await _upload_one(http, target, b64, dl.get("content_type") or "")
            upload_result["byte_length"] = len(raw)
            results.append(upload_result)
            continue

        # Single (non-album) photo: the simpler message-level blob-fetch
        # path — unchanged since Phase 0, no buffering/viewer-menu
        # complexity has been observed for a static image the way it was
        # for video.
        try:
            fetched = await _evaluate(page, _DOWNLOAD_JS, [full_sel, idx])
        except Exception as exc:
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": f"download failed: {exc}"})
            continue
        if not fetched.get("ok"):
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": fetched.get("reason")})
            continue
        if not fetched.get("base64"):
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "downloaded zero bytes"})
            continue
        upload_result = await _upload_one(http, target, fetched["base64"], fetched.get("contentType", ""))
        upload_result["byte_length"] = fetched.get("byteLength")
        results.append(upload_result)

    return {"results": results}


# SEND-state success/failure classification (2026-08-24) — matches
# worker.py's own outbound-job policy exactly: MESSAGE_SENT_AND_VERIFIED
# and MESSAGE_SENT_BUT_NOT_VERIFIED both mean the send genuinely
# happened (the same reason worker.py never retries "sent+unverified");
# everything else is a real failure, safe to retry on a later SEND run.
_SEND_SUCCESS_STATES = {"MESSAGE_SENT_AND_VERIFIED", "MESSAGE_SENT_BUT_NOT_VERIFIED"}


async def _send_one(page, target: Dict[str, Any], raw: bytes, diagnostic_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Writes already-downloaded WhatsApp source bytes to a local temp
    file and sends them via the existing, proven send_whatsapp_message —
    local_file_path skips any URL/Cloudinary round-trip entirely. This
    function owns the temp file's full lifecycle (create, pass, delete) —
    send_whatsapp_message never deletes a local_file_path it didn't
    create itself (see its own docstring).

    `diagnostic_meta` (2026-08-24, diagnostic-only): forwarded unchanged
    to send_whatsapp_message, which logs it ONLY if the attach-button
    click itself fails — never read here, never affects the result."""
    detected = _detect_mime_type(raw, target.get("source_media_type"))
    ext = _MIME_BY_EXT.get(detected, "bin")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tf:
            tf.write(raw)
            temp_path = tf.name
        result = await sender.send_whatsapp_message(
            page=page, destination_type="group", destination=target["destination_group"],
            message_body=target.get("caption") or "", local_file_path=temp_path,
            diagnostic_meta=diagnostic_meta, strict_send_confirmation=True,
        )
        state = result.get("state")
        if state in _SEND_SUCCESS_STATES:
            return {"ok": True, "source_message_id": target["source_message_id"], "send_state": state}
        return {"ok": False, "source_message_id": target["source_message_id"], "error": f"send state {state!r}", "send_state": state}
    except Exception as exc:
        return {"ok": False, "source_message_id": target["source_message_id"], "error": f"send failed: {exc}"}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


PER_ITEM_SEND_TIMEOUT = 90.0


async def _send_one_target(page, group_name: str, target: Dict[str, Any], item_label: str = "") -> Dict[str, Any]:
    """One SEND item end-to-end: reopen the source group, locate the exact
    marked message, retrieve its original bytes via the same proven
    mechanism its media type requires, then hand off to _send_one. Split
    out from _run_send so each item can be individually time-bounded —
    one truly stuck item must fail on its own, never silently swallow the
    rest of the batch's already-real results (see _run_send's docstring).

    `item_label` (2026-08-24, diagnostic-only): e.g. "3/6" — folded into
    the diagnostic_meta passed through to _send_one/send_whatsapp_message,
    purely for the attach-click-failure evidence log; never affects
    control flow."""
    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"ok": False, "source_message_id": target["source_message_id"], "error": f"source group not open (status={status})"}
    idx = await _find_message_index_by_data_id(page, group_name, target["source_message_id"])
    if idx is None:
        return {"ok": False, "source_message_id": target["source_message_id"], "error": "source message no longer found in window"}
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    message = page.locator(full_sel).nth(idx)

    raw: Optional[bytes] = None
    fetch_error: Optional[str] = None
    viewer_used = False
    viewer_closed: Optional[Dict[str, Any]] = None

    if target.get("source_media_type") == "video":
        viewer_used = True
        tile_index = target.get("album_tile_index")
        if tile_index is None:
            tile_index = 0
        dl = await _open_tile_viewer_and_download(
            page, message, tile_index,
            group_name=group_name, source_message_id=target["source_message_id"],
        )
        viewer_closed = dl.get("viewer_closed")
        if not dl.get("ok"):
            fetch_error = dl.get("reason") or f"failed at stage {dl.get('stage')}"
        else:
            downloads = dl.get("downloads") or []
            raw = next((d.get("_raw_bytes") for d in downloads if d.get("ok") and d.get("_raw_bytes")), None)
            if not raw:
                fetch_error = "downloaded zero bytes"
    elif target.get("album_tile_index") is not None:
        dl = await _download_photo_album_tile_via_blob(
            message, page, target.get("album_tile_index"), target["source_thumbnail_hash"],
        )
        if not dl.get("ok"):
            fetch_error = dl.get("reason") or f"failed at stage {dl.get('stage')}"
        else:
            raw = dl.get("_raw_bytes")
            if not raw:
                fetch_error = "downloaded zero bytes"
    else:
        try:
            fetched = await _evaluate(page, _DOWNLOAD_JS, [full_sel, idx])
        except Exception as exc:
            fetched = None
            fetch_error = f"download failed: {exc}"
        if fetched is not None:
            if not fetched.get("ok"):
                fetch_error = fetched.get("reason")
            elif not fetched.get("base64"):
                fetch_error = "downloaded zero bytes"
            else:
                raw = b64mod.b64decode(fetched["base64"])

    if raw is None:
        return {"ok": False, "source_message_id": target["source_message_id"], "error": fetch_error or "download failed"}

    diagnostic_meta = {
        "item": item_label,
        "destination_group": target.get("destination_group"),
        "source_group": group_name,
        "source_media_type": target.get("source_media_type"),
        "media_role": target.get("media_role"),
        "album_tile_index": target.get("album_tile_index"),
        "source_retrieval_ok": True,
        "viewer_used": viewer_used,
        "viewer_closed": viewer_closed,
    }
    send_result = await _send_one(page, target, raw, diagnostic_meta=diagnostic_meta)
    send_result["byte_length"] = len(raw)
    return send_result


async def _run_send(page, req: Dict[str, Any]) -> Dict[str, Any]:
    """SEND workflow (2026-08-24) — independent of UPLOAD: retrieves the
    ORIGINAL WhatsApp source media using the exact same proven mechanisms
    (_download_photo_album_tile_via_blob for photo album tiles,
    _open_tile_viewer_and_download for video/album-video, the plain
    blob-fetch path for a single non-album photo) and sends the resulting
    bytes directly to the destination Casting group — never touching
    Cloudinary, /media-upload, submissions, or media_assignments. Each
    target is independently resilient: one item's failure never prevents
    the rest from being attempted, and never substitutes another tile.

    Each item runs under its own PER_ITEM_SEND_TIMEOUT bound (2026-08-24
    fix — a real disposable E2E against 6 items hit the OUTER 180s
    mark_scan_loop timeout, which cancels this whole function and reports
    zero results for every item, discarding any that had already
    genuinely sent; a per-item bound means one slow/stuck item is reported
    as its own failure while every other item's real result is preserved)."""
    group_name = req["group_name"]
    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"error": f"Could not open WhatsApp source group {group_name!r} (status={status})"}

    send_targets = req.get("send_targets") or []
    results = []
    for i, target in enumerate(send_targets):
        item_label = f"{i + 1}/{len(send_targets)}"
        try:
            result = await asyncio.wait_for(_send_one_target(page, group_name, target, item_label), timeout=PER_ITEM_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"ok": False, "source_message_id": target["source_message_id"], "error": f"timed out after {PER_ITEM_SEND_TIMEOUT}s"}
        except Exception as exc:
            result = {"ok": False, "source_message_id": target["source_message_id"], "error": f"item failed: {exc}"}
        results.append(result)

    return {"results": results}


def _strip_raw_bytes(obj: Any) -> Any:
    """Recursively removes any "_raw_bytes" key before a result is
    JSON-serialized for an HTTP report — a safety net independent of which
    code path produced the dict, so raw media bytes can never accidentally
    leave the worker as part of a scan/download-result report (the real
    upload path consumes them directly via _upload_one, never through this
    JSON-serialized report)."""
    if isinstance(obj, dict):
        return {k: _strip_raw_bytes(v) for k, v in obj.items() if k != "_raw_bytes"}
    if isinstance(obj, list):
        return [_strip_raw_bytes(v) for v in obj]
    return obj


async def mark_scan_loop(session, http: httpx.AsyncClient) -> None:
    """Spawned once from worker.py, alongside the inbound listener — polls
    for a claimed scan/download request; a fully idle system does nothing
    but one cheap HTTP poll every POLL_SEC, matching "bounded/triggered,
    never continuous" for every talent group."""
    logger.info("mark_scan: starting media-assignment poll loop...")
    while True:
        try:
            if session.is_healthy:
                resp = await http.post(f"{BASE}/scan-requests/claim", headers=_auth_headers(), timeout=15.0)
                req = resp.json() if resp.status_code == 200 else {}
                if req and req.get("id"):
                    # A claimed request must NEVER be left stuck in
                    # "processing" forever (2026-08-23 bug: an unexpected
                    # hang inside a page.evaluate/click during album-tile
                    # download left a request claimed but never reported
                    # back) — bound the whole attempt and always report
                    # SOMETHING, even just a timeout, so the backend
                    # orchestrator can move on rather than wait forever.
                    try:
                        async with session.page_lock:
                            page = session.page
                            if page is None:
                                logger.warning("mark_scan: no active page, skipping this claim")
                            elif req.get("mode") == "scan":
                                result = await asyncio.wait_for(_run_scan(page, req, session=session), timeout=90.0)
                                await http.post(
                                    f"{BASE}/scan-requests/{req['id']}/scan-result",
                                    json={
                                        "candidates": result.get("candidates", []), "error": result.get("error"),
                                        "debug": result.get("debug"),
                                    },
                                    headers=_auth_headers(), timeout=30.0,
                                )
                            elif req.get("mode") == "download":
                                result = await asyncio.wait_for(_run_download(page, http, req), timeout=180.0)
                                await http.post(
                                    f"{BASE}/scan-requests/{req['id']}/download-result",
                                    json={"results": _strip_raw_bytes(result.get("results", [])), "error": result.get("error")},
                                    headers=_auth_headers(), timeout=30.0,
                                )
                            elif req.get("mode") == "send":
                                # SEND workflow (2026-08-24) — independent
                                # of UPLOAD; reuses the SAME generic
                                # download-result endpoint/status values
                                # (the backend orchestrator branches on
                                # this doc's own "mode" field to interpret
                                # results as sends, not uploads — no wire
                                # protocol change needed).
                                # Outer safety net only — normal completion
                                # happens well inside this via _run_send's own
                                # per-item PER_ITEM_SEND_TIMEOUT bound; sized
                                # generously above the per-item sum so it
                                # essentially never fires for a real batch.
                                n_send_targets = len(req.get("send_targets") or [])
                                send_timeout = 60.0 + PER_ITEM_SEND_TIMEOUT * max(1, n_send_targets)
                                result = await asyncio.wait_for(_run_send(page, req), timeout=send_timeout)
                                await http.post(
                                    f"{BASE}/scan-requests/{req['id']}/download-result",
                                    json={"results": _strip_raw_bytes(result.get("results", [])), "error": result.get("error")},
                                    headers=_auth_headers(), timeout=30.0,
                                )
                            elif req.get("mode") == "download_probe":
                                result = await asyncio.wait_for(_run_download_probe(session, page, req), timeout=150.0)
                                await http.post(
                                    f"{BASE}/scan-requests/{req['id']}/download-result",
                                    json={"results": _strip_raw_bytes(result.get("results", [])), "error": result.get("error")},
                                    headers=_auth_headers(), timeout=30.0,
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("mark_scan: claimed request %r failed/hung — reporting failure so it isn't stuck", req.get("id"))
                        endpoint = "scan-result" if req.get("mode") == "scan" else "download-result"
                        body = (
                            {"candidates": [], "error": f"worker exception: {exc}"} if endpoint == "scan-result"
                            else {"results": [], "error": f"worker exception: {exc}"}
                        )
                        try:
                            await http.post(f"{BASE}/scan-requests/{req['id']}/{endpoint}", json=body, headers=_auth_headers(), timeout=30.0)
                        except Exception:
                            logger.exception("mark_scan: failed to even report the failure for %r", req.get("id"))
                    continue  # check for more work immediately, no sleep
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mark_scan: unexpected error in poll cycle")
        await asyncio.sleep(POLL_SEC)
