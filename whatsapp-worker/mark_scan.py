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


async def _dump_window(page, group_name: str, max_messages: int) -> List[Dict[str, Any]]:
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
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
    for item in window:
        html = item.get("messageHtml") or ""
        if item.get("quotedHtml"):
            continue  # this is itself a reply, not a plain source message
        media_type = _media_type(html)
        if not media_type:
            continue
        h = _smallest_hash(html)
        data_id = _own_data_id(html)
        if not h or not data_id:
            continue
        sources_by_hash[h] = {
            "source_message_id": data_id, "source_media_type": media_type,
            "source_sender": _sender_name(html),
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
        })

    # TEMPORARY debug (2026-08-23) — investigating a resolution mismatch
    # between isolated function tests and the live scan; remove once root
    # cause is found and fixed.
    debug = {
        "window_count": len(window),
        "source_hashes": list(sources_by_hash.keys()),
        "source_message_ids": [v["source_message_id"] for v in sources_by_hash.values()],
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


async def _find_message_index_by_data_id(page, group_name: str, data_id: str) -> Optional[int]:
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
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
        idx = await _find_message_index_by_data_id(page, group_name, target["source_message_id"])
        if idx is None:
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": "source message no longer found in window"})
            continue
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        try:
            fetched = await page.evaluate(_DOWNLOAD_JS, [full_sel, idx])
        except Exception as exc:
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": f"download failed: {exc}"})
            continue
        if not fetched.get("ok"):
            results.append({"ok": False, "source_message_id": target["source_message_id"], "error": fetched.get("reason")})
            continue
        upload_result = await _upload_one(http, target, fetched["base64"], fetched.get("contentType", ""))
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
                    async with session.page_lock:
                        page = session.page
                        if page is None:
                            logger.warning("mark_scan: no active page, skipping this claim")
                        elif req.get("mode") == "scan":
                            result = await _run_scan(page, req)
                            await http.post(
                                f"{BASE}/scan-requests/{req['id']}/scan-result",
                                json={
                                    "candidates": result.get("candidates", []), "error": result.get("error"),
                                    "debug": result.get("debug"),
                                },
                                headers=_auth_headers(), timeout=30.0,
                            )
                        elif req.get("mode") == "download":
                            result = await _run_download(page, http, req)
                            await http.post(
                                f"{BASE}/scan-requests/{req['id']}/download-result",
                                json={"results": result.get("results", []), "error": result.get("error")},
                                headers=_auth_headers(), timeout=30.0,
                            )
                    continue  # check for more work immediately, no sleep
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mark_scan: unexpected error in poll cycle")
        await asyncio.sleep(POLL_SEC)
