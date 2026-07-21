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

# WhatsApp always renders an inline timestamp as the last line of a message
# bubble's own text (e.g. "...\n2:15 pm") — .inner_text() picks it up as
# plain content indistinguishable from the message itself. Strict parsers on
# the Agent Platform side (e.g. the confirmation reply matcher, which checks
# exact membership like text.strip().lower() == "1") break on this, so it's
# stripped here — a WhatsApp rendering quirk is a transport concern, not
# something the platform should need to know about.
_TRAILING_TIMESTAMP_RE = re.compile(r"\n\d{1,2}:\d{2}\s*[ap]m\s*$", re.IGNORECASE)


def _clean_message_text(text: str, sender_name: Optional[str]) -> str:
    """Strip WhatsApp rendering artifacts that leak into .inner_text() but
    are never part of what the sender actually typed: the trailing inline
    timestamp (always present), and a leading author-label block (only
    rendered on the first bubble of a consecutive run — see the
    direction-detection fix earlier in this file for the same grouping
    behavior).

    Live bug (2026-07-21): for at least one WhatsApp Business account, the
    author label is TWO lines — the business name, then the phone-number
    identifier that data-pre-plain-text actually uses as sender_name (e.g.
    "Candor General Trading\n+971 54 329 9197\nSave\n..."), not the single
    line every other observed sender renders. A message left with
    "Candor General Trading" as its first line fails detect_trigger's
    first-line check and gets silently treated as unrelated chatter — no
    error, just never dispatched. Strips everything up to and including
    whichever of the first two lines equals sender_name, not just an exact
    single-line prefix."""
    cleaned = _TRAILING_TIMESTAMP_RE.sub("", text)
    if sender_name:
        target = sender_name.strip()
        lines = cleaned.split("\n")
        for i, line in enumerate(lines[:2]):
            if line.strip() == target:
                cleaned = "\n".join(lines[i + 1:])
                break
    return cleaned.strip()


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


def _pseudo_identity_from_name(group_name: str, sender_name: str) -> str:
    """Stable digits-only fallback identifier for a confirmed group member
    whose real phone number couldn't be read from either the message DOM or
    the Group Info panel (WhatsApp's member list shows display names, not
    numbers, once a push name is set). ONLY ever called after
    sender_is_group_member has already resolved True — this is purely a
    conversation-state key (dispatcher.py keys multi-turn state by this
    string; the CRM module never reads sender_phone at all), never a
    security decision — group membership already gated that. Deterministic
    per (group, name) so the same sender's conversation state stays stable
    across turns, digits-only so dispatcher.py's _normalize_sender leaves it
    untouched."""
    digest = hashlib.sha256(f"{group_name}|{sender_name.strip().lower()}".encode("utf-8")).hexdigest()
    return str(int(digest[:16], 16))[:15]


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


class GroupParticipantsCache:
    """Per-group cache of the WhatsApp group's current participant list,
    refreshed on a timer rather than on every message — same pattern as
    KnownGroupsCache. Only consumed by agents configured with
    security_mode="group_members" (see backend/agents/registry.py); reading
    it costs a real page navigation (open Group Info, close it), so it must
    never be fetched more than once per scan cycle per group."""

    def __init__(self):
        self._by_group: dict[str, list] = {}
        self._last_refresh: dict[str, float] = {}

    async def get(self, page, group_name: str, force: bool = False) -> Optional[list]:
        now = time.monotonic()
        last = self._last_refresh.get(group_name, 0.0)
        if not force and now - last < config.PARTICIPANTS_REFRESH_SEC and group_name in self._by_group:
            return self._by_group[group_name]
        participants = await sender.get_group_participants(page, group_name)
        if participants is None:
            # Transient read failure — keep serving whatever we last had
            # (possibly None, if we've never successfully fetched) rather
            # than fabricate an empty list, which would silently lock
            # every sender out.
            return self._by_group.get(group_name)
        self._by_group[group_name] = participants
        self._last_refresh[group_name] = now
        return participants


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


_PHONE_LIKE_RE = re.compile(r"^\+?[\d\s\-]{7,20}$")


async def _match_group_member(participants: Optional[list], sender_name: Optional[str]):
    """Match a message's sender_name against a fetched participant list.
    Returns (is_member: Optional[bool], matched_phone: Optional[str]).
    None for is_member means "couldn't determine" (participants unavailable,
    or no sender_name to match against) — callers must treat that as
    not-a-member for any security decision, never guess a yes.

    Live bug (2026-07-21): WhatsApp's data-pre-plain-text sometimes carries
    the sender's raw phone number instead of their display name (observed
    for a WhatsApp Business account where the group's own member list shows
    the business name, e.g. "Candor General Trading LLC..."; the message
    itself is attributed to "+971 54 329 9197") — the same person, two
    different representations depending on which part of the UI is asked.
    A plain substring match on `sender_name` alone misses this, so when it
    looks phone-shaped, its digits are also compared against each
    participant's already-extracted phone."""
    if participants is None or not sender_name:
        return None, None
    needle = sender_name.strip().lower()
    if not needle:
        return None, None
    needle_digits = None
    if _PHONE_LIKE_RE.match(sender_name.strip()):
        digits = "".join(ch for ch in sender_name if ch.isdigit())
        if len(digits) >= 7:
            needle_digits = digits
    for p in participants:
        raw = (p.get("raw_text") or "").strip().lower()
        if needle in raw:
            return True, p.get("phone")
        if needle_digits and needle_digits in (p.get("phone") or ""):
            return True, p.get("phone")
    return False, None


async def _scan_group_for_new_messages(
    page, group_name: str, participants_cache: "GroupParticipantsCache"
) -> list[dict]:
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
    logger.info("inbound: DIAG scan group=%r scope=%r n=%d", group_name, scope, n)

    # Only the tail — new messages always arrive at the bottom, and this
    # keeps the scan cheap regardless of how long the chat history is.
    start = max(0, n - 15)
    new_messages: list[dict] = []
    # Fetched at most once per scan call (not per message), only if a
    # message actually needs it — group_members security mode is the only
    # consumer, and most scan cycles find nothing new to check.
    participants: Optional[list] = None
    participants_fetched = False

    for i in range(start, n):
        try:
            testid = await loc.nth(i).get_attribute("data-testid")
        except Exception:
            continue
        message_id = testid or None

        direction = await sender._is_outgoing_msg(
            page, full_sel, i, self_display_name=config.WA_SELF_DISPLAY_NAME
        )
        if direction is True:
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

        clean_text = _clean_message_text(text, sender_name)

        if not participants_fetched:
            participants = await participants_cache.get(page, group_name)
            participants_fetched = True
        is_member, matched_phone = await _match_group_member(participants, sender_name)
        if not is_member and sender_name:
            # Self-healing: don't let a merely-stale cache (this sender may
            # have just been added to the group) cost them a whole
            # PARTICIPANTS_REFRESH_SEC wait — force one fresh read before
            # concluding non-membership. Bounded cost: only happens on an
            # actual match miss, not on every scan.
            participants = await participants_cache.get(page, group_name, force=True)
            is_member, matched_phone = await _match_group_member(participants, sender_name)
        phone = phone or matched_phone
        if not phone and sender_name and _PHONE_LIKE_RE.match(sender_name.strip()):
            # data-pre-plain-text sometimes carries the sender's actual
            # phone number instead of a display name — prefer it outright
            # over a hashed pseudo-identity when available.
            digits = "".join(ch for ch in sender_name if ch.isdigit())
            if len(digits) >= 7:
                phone = digits
        if not phone and is_member and sender_name:
            phone = _pseudo_identity_from_name(group_name, sender_name)

        new_messages.append({
            "message_id": message_id,
            "text": clean_text,
            "sender_name": sender_name,
            "sender_phone": phone,
            "sender_is_group_member": is_member,
            "raw_pre_plain_text": pre_plain,
        })

    return new_messages


async def _post_inbound(http: httpx.AsyncClient, *, group_name: str, sender_phone: str,
                         sender_name: Optional[str], text: str, message_id: str,
                         sender_is_group_member: Optional[bool] = None) -> Optional[dict]:
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
                "sender_is_group_member": sender_is_group_member,
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


async def _send_reply(page, group_name: str, reply_text: str) -> float:
    """Returns elapsed seconds spent sending (0.0 if there was nothing to send)."""
    if not reply_text:
        return 0.0
    t0 = time.monotonic()
    try:
        result = await sender.send_whatsapp_message(
            page, destination_type="group", destination=group_name, message_body=reply_text,
        )
        elapsed = time.monotonic() - t0
        logger.info("inbound: reply send result group=%r state=%s elapsed_sec=%.2f",
                     group_name, result.get("state"), elapsed)
        return elapsed
    except Exception:
        logger.exception("inbound: failed to send reply into group=%r", group_name)
        return time.monotonic() - t0


# If the backend hasn't responded within this long, send an immediate
# acknowledgment so the user isn't left wondering whether their message was
# seen — the backend is normally fast (~1-1.5s observed live); this only
# fires on genuinely slow calls, so the common case never sends the extra
# message (which would itself cost a full Playwright send+verify cycle).
ACK_THRESHOLD_SEC = 2.0
ACK_TEXT = "Got it — processing..."

# Rolling latency stats for observability (Part 6) — a fixed-size window is
# enough to see current trends in production logs without standing up a
# separate metrics store.
_LATENCY_WINDOW: list[float] = []
_LATENCY_WINDOW_MAX = 50


def _record_latency(total_sec: float) -> None:
    _LATENCY_WINDOW.append(total_sec)
    if len(_LATENCY_WINDOW) > _LATENCY_WINDOW_MAX:
        _LATENCY_WINDOW.pop(0)
    avg = sum(_LATENCY_WINDOW) / len(_LATENCY_WINDOW)
    logger.info(
        "inbound: METRICS window=%d avg_total_sec=%.2f max_total_sec=%.2f latest_sec=%.2f",
        len(_LATENCY_WINDOW), avg, max(_LATENCY_WINDOW), total_sec,
    )


async def poll_once(
    session, http: httpx.AsyncClient, groups_cache: KnownGroupsCache,
    participants_cache: "GroupParticipantsCache",
) -> None:
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
                new_messages = await _scan_group_for_new_messages(page, group_name, participants_cache)
            except Exception:
                logger.exception("inbound: scan failed for group=%r", group_name)
                continue

            for msg in new_messages:
                phone = msg["sender_phone"]
                if not phone:
                    logger.warning(
                        "inbound: could not determine sender phone for message_id=%r "
                        "group=%r sender_name=%r sender_is_group_member=%r text=%r "
                        "pre_plain_text=%r — declining to dispatch (fail closed: never "
                        "guess a security-relevant identity). Marking seen so we do not "
                        "retry it forever.",
                        msg["message_id"], group_name, msg["sender_name"],
                        msg["sender_is_group_member"], msg["text"][:80],
                        msg["raw_pre_plain_text"],
                    )
                    try:
                        await get_db().whatsapp_dispatch_failures.insert_one({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "group_name": group_name,
                            "message_id": msg["message_id"],
                            "sender_name": msg["sender_name"],
                            "sender_is_group_member": msg["sender_is_group_member"],
                            "text": msg["text"],
                            "raw_pre_plain_text": msg["raw_pre_plain_text"],
                            "reason": "no_phone_resolved",
                        })
                    except Exception:
                        logger.exception("inbound: failed to persist dispatch-failure record")
                    await _mark_processed(msg["message_id"])
                    continue

                t_detected = time.monotonic()
                logger.info(
                    "inbound: new message group=%r sender_name=%r sender_phone=%r "
                    "sender_is_group_member=%r message_id=%r text=%r",
                    group_name, msg["sender_name"], phone, msg["sender_is_group_member"],
                    msg["message_id"], msg["text"][:120],
                )

                backend_task = asyncio.create_task(_post_inbound(
                    http,
                    group_name=group_name,
                    sender_phone=phone,
                    sender_name=msg["sender_name"],
                    text=msg["text"],
                    message_id=msg["message_id"],
                    sender_is_group_member=msg["sender_is_group_member"],
                ))
                done, _ = await asyncio.wait({backend_task}, timeout=ACK_THRESHOLD_SEC)
                ack_sent_sec = None
                if backend_task not in done:
                    ack_elapsed = await _send_reply(page, group_name, ACK_TEXT)
                    ack_sent_sec = round(time.monotonic() - t_detected, 2)
                    logger.info(
                        "inbound: TIMING backend exceeded %.1fs — sent ack (took %.2fs) "
                        "message_id=%r", ACK_THRESHOLD_SEC, ack_elapsed, msg["message_id"],
                    )
                result = await backend_task
                t_backend_done = time.monotonic()

                if result is None:
                    # Backend call failed — do NOT mark as processed, so a
                    # transient outage gets a chance to be retried on the
                    # next poll (the message is still "new" in the DOM).
                    continue

                await _mark_processed(msg["message_id"])

                reply = result.get("reply")
                reply_elapsed = 0.0
                if reply:
                    reply_elapsed = await _send_reply(page, group_name, reply)
                t_total = time.monotonic() - t_detected
                logger.info(
                    "inbound: TIMING message_id=%r backend_sec=%.2f reply_send_sec=%.2f "
                    "ack_sent_at_sec=%s total_sec=%.2f",
                    msg["message_id"], t_backend_done - t_detected, reply_elapsed,
                    ack_sent_sec, t_total,
                )
                _record_latency(t_total)


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
        participants_cache = GroupParticipantsCache()
        logger.info(
            "inbound: listener started (poll_sec=%d, groups_refresh_sec=%d)",
            config.INBOUND_POLL_SEC, config.INBOUND_GROUPS_REFRESH_SEC,
        )
        while True:
            try:
                if session.is_healthy:
                    await poll_once(session, http, groups_cache, participants_cache)
                else:
                    logger.info("inbound: session unhealthy — skipping this poll cycle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("inbound: unexpected error in poll cycle — will retry next cycle")
            await asyncio.sleep(config.INBOUND_POLL_SEC)
