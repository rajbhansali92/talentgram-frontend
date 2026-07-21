"""
WhatsApp Worker — Inbound Agent Platform Transport Adapter.

Pure transport, zero business logic. Everything this module does is:

  1. Periodically ask the backend's Agent Registry which WhatsApp group
     names currently matter (GET /api/agents/whatsapp/known-groups) — a
     group name is NEVER hardcoded here; if no agent is mapped to a group,
     this module never looks at it.
  2. For each such group, open the chat (reusing sender._open_group_chat —
     the exact same proven, battle-tested code path outbound sending
     already uses) and scan its most recent messages for ones that are
     new, INCOMING (not sent by us), and not already processed.
  3. POST each one to POST /api/agents/whatsapp/inbound and, if a reply
     comes back, send it into the same chat via sender.send_whatsapp_message
     — called directly (not through the batch job queue), so an agent
     reply goes out immediately with none of the batch-campaign pacing
     delay (that delay lives in worker.py's job loop, not in
     send_whatsapp_message itself).
  4. Remembers what has already been processed, both in-memory (fast path)
     and in a Mongo TTL collection (whatsapp_inbound_seen) so a worker
     restart never reprocesses — and never re-executes — a message it has
     already acted on.

Nothing in here knows what "CRM" or "contact" or "Marketing" means. It
only knows: group name, sender, text, message id.

Concurrency: this module shares WhatsAppSession's single Page with the
outbound sender. Every page-touching operation is wrapped in
`session.page_lock` (an asyncio.Lock owned by WhatsAppSession) so inbound
scanning and outbound sending — which both drive page navigation — can
never interleave. worker.py wraps the existing outbound send call in the
same lock; see worker.py's poll_and_process_jobs.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import config
import sender
from db import get_db

logger = logging.getLogger(__name__)

SEEN_COLLECTION = "whatsapp_inbound_seen"

# In-memory fast path — avoids a Mongo round-trip for every element on
# every poll of an already-scanned chat. The Mongo collection (TTL-indexed)
# is the durable backstop across restarts.
_seen_cache: set[str] = set()

# Sender identity: WhatsApp Web renders "[10:30 AM, 21/07/2026] John Doe: "
# into data-pre-plain-text on group messages — this regex pulls the display
# name out of that. 1:1 chats don't carry a name here (nothing to attribute
# beyond "the chat itself"), which is fine — the CRM group is what matters.
_PRE_PLAIN_NAME_RE = re.compile(r"^\[[^\]]*\]\s*(.+?):\s*$")

# Best-effort phone extraction from a WhatsApp JID embedded in a data-id
# attribute (e.g. "false_120363.....@g.us_3EB0.....@c.us" style ids some
# WhatsApp Web builds use for group messages). NOT guaranteed present in
# every build/version — see the module docstring in dispatcher usage notes
# and the Files Changed section of the deployment report for what was
# actually observed live.
_JID_PHONE_RE = re.compile(r"(\d{7,15})@(?:c\.us|s\.whatsapp\.net)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_indexes() -> None:
    db = get_db()
    try:
        await db[SEEN_COLLECTION].create_index("message_id", unique=True)
        await db[SEEN_COLLECTION].create_index(
            "created_at", expireAfterSeconds=config.INBOUND_DEDUP_TTL_SEC
        )
    except Exception:
        logger.exception("inbound: failed to create whatsapp_inbound_seen indexes (non-fatal)")


async def _already_processed(message_id: str) -> bool:
    if message_id in _seen_cache:
        return True
    db = get_db()
    try:
        doc = await db[SEEN_COLLECTION].find_one({"message_id": message_id}, {"_id": 1})
    except Exception:
        logger.exception("inbound: dedup lookup failed for %s (treating as unseen)", message_id)
        return False
    if doc:
        _seen_cache.add(message_id)
        return True
    return False


async def _mark_processed(message_id: str) -> None:
    _seen_cache.add(message_id)
    # Cap unbounded in-memory growth over a long-lived process; the Mongo
    # TTL collection remains the source of truth once this cache rotates.
    if len(_seen_cache) > 5000:
        _seen_cache.clear()
    db = get_db()
    try:
        await db[SEEN_COLLECTION].update_one(
            {"message_id": message_id},
            {"$setOnInsert": {"message_id": message_id, "created_at": _now()}},
            upsert=True,
        )
    except Exception:
        logger.exception("inbound: failed to persist dedup record for %s", message_id)


def _fallback_message_id(group_name: str, text: str, pre_plain: Optional[str]) -> str:
    raw = f"{group_name}|{pre_plain or ''}|{text}"
    return "fallback:" + hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:24]


class KnownGroupsCache:
    """Refreshes the agent-mapped group name list from the backend on a
    timer, so we don't hit the Agent Registry on every poll cycle."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http
        self._groups: list[str] = []
        self._last_refresh: float = 0.0

    async def get(self) -> list[str]:
        now = time.monotonic()
        if now - self._last_refresh < config.INBOUND_GROUPS_REFRESH_SEC and self._groups:
            return self._groups
        try:
            resp = await self._http.get(
                f"{config.AGENTS_BACKEND_URL}/api/agents/whatsapp/known-groups",
                headers=_auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            groups = resp.json().get("groups") or []
            self._groups = groups
            self._last_refresh = now
            logger.info("inbound: known-groups refreshed -> %s", groups)
        except Exception:
            logger.exception(
                "inbound: failed to refresh known-groups from backend; "
                "keeping previous list %s", self._groups
            )
        return self._groups


def _auth_headers() -> dict:
    headers = {}
    if config.AGENTS_INBOUND_SECRET:
        headers["X-Internal-Secret"] = config.AGENTS_INBOUND_SECRET
    return headers


async def _extract_message_info(page, full_selector: str, index: int) -> dict:
    """Best-effort: sender display name + phone (if derivable) + the
    data-pre-plain-text blob, read via a single page.evaluate for the
    matched message element."""
    try:
        return await page.evaluate(
            """([sel, idx]) => {
                const els = document.querySelectorAll(sel);
                if (idx >= els.length) return {prePlainText: null, phone: null};
                const el = els[idx];
                let prePlainText = null;
                let node = el;
                for (let i = 0; i < 4 && node; i++) {
                    const attr = node.getAttribute && node.getAttribute('data-pre-plain-text');
                    if (attr) { prePlainText = attr; break; }
                    const inner = node.querySelector && node.querySelector('[data-pre-plain-text]');
                    if (inner) { prePlainText = inner.getAttribute('data-pre-plain-text'); break; }
                    node = node.parentElement;
                }
                let phone = null;
                node = el;
                for (let i = 0; i < 6 && node; i++) {
                    const did = node.getAttribute ? node.getAttribute('data-id') : null;
                    if (did) {
                        const m = did.match(/(\\d{7,15})@(c\\.us|s\\.whatsapp\\.net)/);
                        if (m) { phone = m[1]; break; }
                    }
                    node = node.parentElement;
                }
                return {prePlainText, phone};
            }""",
            [full_selector, index],
        )
    except Exception:
        logger.exception("inbound: _extract_message_info failed at index %d", index)
        return {"prePlainText": None, "phone": None}


async def _direction_diag(page, css_selector: str, index: int) -> dict:
    """Diagnostic-only mirror of sender._is_outgoing_msg's ancestor walk — logs
    what it actually saw (classNames + data-id at each level, checkmark count)
    instead of collapsing straight to True/False/None, so a live 'undeterminable'
    verdict is debuggable without guessing."""
    try:
        return await page.evaluate("""([sel, idx]) => {
            const els = document.querySelectorAll(sel);
            if (idx >= els.length) return {error: 'index out of range'};
            const el = els[idx];
            const chain = [];
            let node = el;
            for (let i = 0; i < 6 && node && node !== document; i++) {
                chain.push({
                    tag: node.tagName || null,
                    className: typeof node.className === 'string' ? node.className : null,
                    dataId: node.getAttribute ? node.getAttribute('data-id') : null,
                });
                node = node.parentElement;
            }
            const checks = el.querySelectorAll(
                '[data-icon="msg-check"], [data-icon="msg-dblcheck"],'
                + '[data-testid="msg-check"], [data-testid="msg-dblcheck"]'
            );
            const main = document.querySelector('#main');
            const elRect = el.getBoundingClientRect();
            const mainRect = main ? main.getBoundingClientRect() : null;
            return {
                chain: chain,
                checkCount: checks.length,
                outerHtml: el.outerHTML.slice(0, 2500),
                elRect: {left: elRect.left, right: elRect.right, width: elRect.width},
                mainRect: mainRect ? {left: mainRect.left, right: mainRect.right, width: mainRect.width} : null,
            };
        }""", [css_selector, index])
    except Exception as exc:
        return {"error": str(exc)}


async def _scan_group_for_new_messages(page, group_name: str) -> list[dict]:
    """Scan the ALREADY-OPEN, ALREADY-VERIFIED conversation for the most
    recent messages, returning ones that are new + incoming + not yet
    processed. Reuses sender._resolve_scope / sender._is_outgoing_msg
    directly rather than re-deriving selector logic."""
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    try:
        loc = page.locator(full_sel)
        n = await loc.count()
    except Exception:
        logger.exception("inbound: message count failed for group=%r", group_name)
        return []

    # Only the tail — new messages always arrive at the bottom, and this
    # keeps the scan cheap regardless of how long the chat history is.
    start = max(0, n - 15)
    new_messages: list[dict] = []

    for i in range(start, n):
        try:
            testid = await loc.nth(i).get_attribute("data-testid")
        except Exception:
            continue
        message_id = testid or None

        direction = await sender._is_outgoing_msg(page, full_sel, i)
        if direction is True:
            # TEMP research logging (2026-07-21): capture what a confirmed-
            # outgoing message's DOM looks like so the incoming-side fallback
            # (for messages with no tail element) can target the real
            # structure. Remove once the fallback rule is confirmed.
            diag = await _direction_diag(page, full_sel, i)
            logger.info("inbound: RESEARCH outgoing-confirmed index=%d testid=%r diag=%s",
                        i, testid, diag)
            # Ours — mark seen (if we have a stable id) so we never
            # re-evaluate it, and never dispatch it.
            if message_id:
                await _mark_processed(message_id)
            continue
        if direction is None:
            try:
                snippet = (await loc.nth(i).inner_text()).strip()[:80]
            except Exception:
                snippet = "<unreadable>"
            diag = await _direction_diag(page, full_sel, i)
            logger.warning(
                "inbound: direction undeterminable for group=%r index=%d testid=%r "
                "text=%r diag=%s — skipping (fail closed, not guessing)",
                group_name, i, testid, snippet, diag,
            )
            if message_id:
                await _mark_processed(message_id)
            continue

        try:
            text = (await loc.nth(i).inner_text()).strip()
        except Exception:
            text = ""

        info = await _extract_message_info(page, full_sel, i)
        pre_plain = info.get("prePlainText")
        phone = info.get("phone")

        if not message_id:
            message_id = _fallback_message_id(group_name, text, pre_plain)

        if await _already_processed(message_id):
            continue
        if not text:
            await _mark_processed(message_id)
            continue

        sender_name = None
        if pre_plain:
            m = _PRE_PLAIN_NAME_RE.match(pre_plain)
            if m:
                sender_name = m.group(1).strip()

        new_messages.append({
            "message_id": message_id,
            "text": text,
            "sender_name": sender_name,
            "sender_phone": phone,
            "raw_pre_plain_text": pre_plain,
        })

    return new_messages


async def _post_inbound(http: httpx.AsyncClient, *, group_name: str, sender_phone: str,
                         sender_name: Optional[str], text: str, message_id: str) -> Optional[dict]:
    t0 = time.monotonic()
    try:
        resp = await http.post(
            f"{config.AGENTS_BACKEND_URL}/api/agents/whatsapp/inbound",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "group_name": group_name,
                "sender_phone": sender_phone,
                "sender_name": sender_name,
                "text": text,
                "message_id": message_id,
            },
            timeout=20.0,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        body = resp.json()
        logger.info(
            "inbound: dispatched group=%r sender=%r message_id=%r handled=%s latency_ms=%d",
            group_name, sender_phone, message_id, body.get("handled"), latency_ms,
        )
        return body
    except Exception:
        logger.exception(
            "inbound: dispatch to backend FAILED group=%r sender=%r message_id=%r "
            "(message left unprocessed; will retry only if it re-appears unseen — "
            "it will not, since marking-seen happens after a successful dispatch)",
            group_name, sender_phone, message_id,
        )
        return None


async def _send_reply(page, group_name: str, reply_text: str) -> None:
    if not reply_text:
        return
    try:
        result = await sender.send_whatsapp_message(
            page, destination_type="group", destination=group_name, message_body=reply_text,
        )
        logger.info("inbound: reply send result group=%r state=%s", group_name, result.get("state"))
    except Exception:
        logger.exception("inbound: failed to send reply into group=%r", group_name)


async def poll_once(session, http: httpx.AsyncClient, groups_cache: KnownGroupsCache) -> None:
    groups = await groups_cache.get()
    if not groups:
        return

    for group_name in groups:
        async with session.page_lock:
            page = session.page
            if page is None:
                logger.warning("inbound: no active page (session restarting?) — skipping this cycle")
                return
            try:
                status = await sender._open_group_chat(page, group_name)
            except Exception:
                logger.exception("inbound: failed to open group=%r", group_name)
                continue
            if status != "OPENED":
                logger.info("inbound: group=%r not opened (status=%s) — skipping this cycle", group_name, status)
                continue

            try:
                new_messages = await _scan_group_for_new_messages(page, group_name)
            except Exception:
                logger.exception("inbound: scan failed for group=%r", group_name)
                continue

            for msg in new_messages:
                phone = msg["sender_phone"]
                if not phone:
                    logger.warning(
                        "inbound: could not determine sender phone for message_id=%r "
                        "group=%r sender_name=%r text=%r pre_plain_text=%r — "
                        "declining to dispatch (fail closed: never guess a security-relevant "
                        "identity). Marking seen so we do not retry it forever.",
                        msg["message_id"], group_name, msg["sender_name"], msg["text"][:80],
                        msg["raw_pre_plain_text"],
                    )
                    await _mark_processed(msg["message_id"])
                    continue

                logger.info(
                    "inbound: new message group=%r sender_name=%r sender_phone=%r message_id=%r text=%r",
                    group_name, msg["sender_name"], phone, msg["message_id"], msg["text"][:120],
                )
                result = await _post_inbound(
                    http,
                    group_name=group_name,
                    sender_phone=phone,
                    sender_name=msg["sender_name"],
                    text=msg["text"],
                    message_id=msg["message_id"],
                )
                if result is None:
                    # Backend call failed — do NOT mark as processed, so a
                    # transient outage gets a chance to be retried on the
                    # next poll (the message is still "new" in the DOM).
                    continue

                await _mark_processed(msg["message_id"])

                reply = result.get("reply")
                if reply:
                    await _send_reply(page, group_name, reply)


async def inbound_listener_loop(session) -> None:
    """Spawned once as an asyncio task from worker.py, alongside the
    existing heartbeat_task, right after session.start(). Runs for the
    lifetime of the process; worker.py cancels it in its shutdown finally
    block exactly like heartbeat_task."""
    if not config.INBOUND_LISTENER_ENABLED:
        logger.info("inbound: listener disabled via WA_INBOUND_LISTENER_ENABLED — outbound sending unaffected")
        return

    await _ensure_indexes()
    async with httpx.AsyncClient() as http:
        groups_cache = KnownGroupsCache(http)
        logger.info(
            "inbound: listener started (poll_sec=%d, groups_refresh_sec=%d)",
            config.INBOUND_POLL_SEC, config.INBOUND_GROUPS_REFRESH_SEC,
        )
        while True:
            try:
                if session.is_healthy:
                    await poll_once(session, http, groups_cache)
                else:
                    logger.info("inbound: session unhealthy — skipping this poll cycle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("inbound: unexpected error in poll cycle — will retry next cycle")
            await asyncio.sleep(config.INBOUND_POLL_SEC)
