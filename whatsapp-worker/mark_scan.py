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


def _album_tile_hashes(html: str) -> List[str]:
    """One stable hash per tile, in DOM/grid order (order is a locator
    only — see module note above). Returns [] for a non-album message."""
    if not html:
        return []
    starts = [m.start() for m in _GRID_AREA_RE.finditer(html)]
    if not starts:
        return []
    starts.append(len(html))
    chunks = [html[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]
    hashes = []
    for chunk in chunks:
        h = _smallest_hash(chunk)
        if h:
            hashes.append(h)
    return hashes


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
        return await page.evaluate(_LOAD_HISTORY_JS, [full_sel, max_messages, max_steps])
    except Exception:
        logger.exception("mark_scan: history-load scroll failed (continuing with whatever is rendered)")
        return 0


async def _dump_window(page, group_name: str, max_messages: int) -> List[Dict[str, Any]]:
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    await _ensure_history_loaded(page, full_sel, max_messages)
    loc = page.locator(full_sel)
    n = await loc.count()
    start = max(0, n - max_messages)
    out = []
    for i in range(start, n):
        try:
            dump = await page.evaluate(_DOM_DUMP_JS, [full_sel, i])
        except Exception:
            logger.exception("mark_scan: DOM dump failed group=%r index=%d", group_name, i)
            continue
        if dump:
            out.append(dump)
    return out


async def _run_scan(page, req: Dict[str, Any]) -> Dict[str, Any]:
    group_name = req["group_name"]
    max_messages = req.get("max_messages") or MAX_MESSAGES_SCANNED_DEFAULT

    status = await sender._open_group_chat(page, group_name)
    if status != "OPENED":
        return {"error": f"Could not open WhatsApp group {group_name!r} (status={status})"}

    window = await _dump_window(page, group_name, max_messages)

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
            for tile_index, h in enumerate(_album_tile_hashes(html)):
                sources_by_hash[h] = {
                    "source_message_id": data_id, "source_media_type": "video",
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

    # TEMPORARY debug (2026-08-23) — grouped/batch-media investigation.
    # Full source inventory (every plain media message found in the
    # window, in DOM order) so a grouped/multi-select send's individual
    # message identities can be inspected directly.
    debug = {
        "window_count": len(window),
        "sources": [
            {"hash": h, **v} for h, v in sources_by_hash.items()
        ],
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


async def _find_message_index_by_data_id(page, group_name: str, data_id: str) -> Optional[int]:
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    await _ensure_history_loaded(page, full_sel, MAX_MESSAGES_SCANNED_DEFAULT)
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


async def _download_album_tile_via_context_menu(page, full_sel: str, idx: int, tile_index: int) -> Dict[str, Any]:
    """Right-click the specific tile, dump the resulting context menu
    (always — evidence either way), and if a "Download" item is found,
    click it inside page.expect_download() to capture WhatsApp Web's own
    native download rather than reverse-engineering a viewer/blob: URL.
    Returns {ok, data (raw bytes), suggested_filename, menu_dump, reason}."""
    tile_locator = (
        page.locator(full_sel).nth(idx)
        .locator('[data-testid="video-content"], [data-testid="image-content"]')
        .nth(tile_index)
    )
    try:
        await tile_locator.click(button="right", timeout=10000)
    except Exception as exc:
        return {"ok": False, "reason": f"right-click failed: {exc}", "menu_dump": None}

    await page.wait_for_timeout(500)
    try:
        menu_dump = await page.evaluate(_CONTEXT_MENU_DUMP_JS)
    except Exception:
        menu_dump = None

    download_item = page.locator(
        '[role="menu"] :text-matches("download", "i"), '
        '[role="menuitem"]:has-text("Download"), '
        'li:has-text("Download"), '
        'div[role="button"]:has-text("Download")'
    ).first

    try:
        visible = await download_item.is_visible(timeout=3000)
    except Exception:
        visible = False
    if not visible:
        await page.keyboard.press("Escape")
        return {"ok": False, "reason": "no visible 'Download' menu item found after right-click", "menu_dump": menu_dump}

    try:
        async with page.expect_download(timeout=15000) as download_info:
            await download_item.click(timeout=5000)
        download = await download_info.value
        path = await download.path()
        if not path:
            return {"ok": False, "reason": "download event fired but no file path available", "menu_dump": menu_dump}
        with open(path, "rb") as f:
            data = f.read()
        return {
            "ok": True, "data": data, "suggested_filename": download.suggested_filename(),
            "menu_dump": menu_dump,
        }
    except Exception as exc:
        return {"ok": False, "reason": f"download did not complete: {exc}", "menu_dump": menu_dump}
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def _upload_one(http: httpx.AsyncClient, target: Dict[str, Any], base64_data: str, content_type: str) -> Dict[str, Any]:
    raw = b64mod.b64decode(base64_data)
    ext = "mp4" if target.get("source_media_type") == "video" else "jpg"
    files = {"file": (f"{target['source_message_id']}.{ext}", raw, content_type or "application/octet-stream")}
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
        if target.get("album_tile_index") is not None:
            # Right-click -> native "Download" (2026-08-23) — replaces the
            # media-viewer/blob: approach, which proved to never actually
            # open a viewer for an album tile (see module comment above
            # _CONTEXT_MENU_DUMP_JS). Authoritative identity stays
            # (album data_id, tile's own thumbnail hash) — tile_index here
            # is only used to locate/click the right element.
            dl = await _download_album_tile_via_context_menu(page, full_sel, idx, target["album_tile_index"])
            if not dl.get("ok"):
                results.append({
                    "ok": False, "source_message_id": target["source_message_id"], "error": dl.get("reason"),
                    "menu_dump": dl.get("menu_dump"),
                })
                continue
            raw = dl["data"]
            if not raw:
                results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "downloaded zero bytes"})
                continue
            b64 = b64mod.b64encode(raw).decode()
            upload_result = await _upload_one(http, target, b64, "")
            upload_result["byte_length"] = len(raw)
            upload_result["suggested_filename"] = dl.get("suggested_filename")
            results.append(upload_result)
            continue

        try:
            fetched = await page.evaluate(_DOWNLOAD_JS, [full_sel, idx])
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
                                result = await asyncio.wait_for(_run_scan(page, req), timeout=90.0)
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
                                    json={"results": result.get("results", []), "error": result.get("error")},
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
