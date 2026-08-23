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
import re
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List, Optional

import httpx

import config
import sender

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
_TILE_TYPE_RE = re.compile(r'data-testid="(video-content|image-content)"')
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
        media_type = "image" if (tm and tm.group(1) == "image-content") else "video"
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
            candidates.append(_batch_failure_candidate(bc, jump.get("reason") or "quoted-jump resolution failed"))
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
    tiles = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]')
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
        media_type = "image" if media_type_attr == "image-content" else "video"
        try:
            tile_html = await tile.evaluate("(el) => el.outerHTML", timeout=10000)
        except Exception:
            result.append({"hash": None, "media_type": media_type})
            continue
        result.append({"hash": _smallest_hash(tile_html), "media_type": media_type})
    return result


async def _resolve_quoted_jump(page, group_name: str, reply_data_id: str) -> Dict[str, Any]:
    """Clicks a reply's own quoted-message block (a real WhatsApp button
    that jumps to/highlights the original message) and observes which
    message ends up closest to the viewport's vertical center afterward —
    the proven identity link (2026-08-23: jumped to the exact known
    album, 0.3px from dead-center, all 4 tile hashes matched) for a reply
    whose quoted block carries no thumbnail hash — WhatsApp's collapsed
    "N videos"/"N photos" summary for a reply to a WHOLE album, not one
    tile. Returns {ok, data_id, is_album, tile_hashes_and_types, reason}."""
    idx = await _find_message_index_by_data_id(page, group_name, reply_data_id)
    if idx is None:
        return {"ok": False, "reason": f"reply message {reply_data_id!r} not found in scanned window"}
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    reply_message = page.locator(full_sel).nth(idx)
    quoted = reply_message.locator('[data-testid="quoted-message"]')
    if await quoted.count() == 0:
        return {"ok": False, "reason": "reply message has no quoted-message block"}
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


async def _open_tile_viewer_and_download(page, message_locator, tile_index: int) -> Dict[str, Any]:
    """Reproduces the manual workflow exactly: open the tile -> wait for a
    real <video> to mount -> wait for it to actually buffer (bounded, not
    a fixed sleep) -> dump the viewer's own buttons (diagnostic-first,
    never a blind selector guess) -> if one looks like a menu trigger,
    click it, dump whatever menu appears, click "Download" if present, and
    collect the resulting download."""
    tile = message_locator.locator('[data-testid="video-content"], [data-testid="image-content"]').nth(tile_index)
    try:
        await tile.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        await _evaluate(page, _EVENT_CAPTURE_INSTALL_JS)
    except Exception:
        pass
    try:
        await tile.click(timeout=10000)
    except Exception as exc:
        return {"ok": False, "stage": "open_tile", "reason": f"click failed: {exc}"}

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
    data_id = req.get("probe_message_id") if probe_type == "album_discovery" else req["probe_message_id"]

    session_identity = {
        "own_phone_number": getattr(session, "own_phone_number", None),
        "session_id": getattr(session, "session_id", None),
        "generation": getattr(session, "generation", None),
    }

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

        # Photos: the simpler blob-fetch path — unchanged since Phase 0,
        # no buffering/viewer-menu complexity has been observed for a
        # static image the way it was for video.
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
