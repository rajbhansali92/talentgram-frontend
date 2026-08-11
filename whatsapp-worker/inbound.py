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
import difflib
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

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

    def clear(self) -> None:
        """Force the next .get() to refetch immediately, ignoring the TTL.
        Called on a detected re-authentication (see _rearm_for_new_generation)
        — the mapped group NAMES aren't account-specific, but re-fetching
        anyway costs one cheap HTTP call and guarantees this cache can never
        itself be the stale piece after a reconnect."""
        self._groups = []
        self._last_refresh = 0.0

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
            # This method re-fetches on a TIMER, so "a refresh happened" is
            # NOT evidence that anything changed — re-arming on every refresh
            # would just be the old infinite retry at a slower cadence
            # (measured: one failed lookup + snapshot every 120s). Re-arm only
            # on a genuine configuration change: the mapped group list itself
            # differs, or the operator cleared the flag through the admin UI
            # (PUT /agents/whatsapp/config/{id} unsets it). Worker restart is
            # handled separately by load_invalid_state().
            list_changed = groups != self._groups
            self._groups = groups
            self._last_refresh = now
            logger.info("inbound: known-groups refreshed -> %s", groups)
            await _update_worker_status(dispatcher_status="ready")
            if _INVALID_GROUPS and (list_changed or await _flag_cleared_externally()):
                logger.info("inbound: configuration changed — revalidating previously invalid "
                            "group(s) %s", sorted(_INVALID_GROUPS))
                # Probed exactly ONCE more; the DB flag is cleared only if
                # that probe succeeds, never optimistically, so the dashboard
                # never shows healthy while the group is still missing.
                _PENDING_REVALIDATION.update(_INVALID_GROUPS)
                _INVALID_GROUPS.clear()
        except Exception:
            await _update_worker_status(dispatcher_status="unreachable")
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

    def clear(self) -> None:
        """Called on a detected re-authentication (see
        _rearm_for_new_generation). Unlike KnownGroupsCache, this one IS
        genuinely account-sensitive — it's scraped from the currently
        authenticated account's own Group Info view and directly gates
        security_mode="group_members" access decisions — so a stale entry
        here after a switch is a real correctness risk, not just a
        performance one."""
        self._by_group = {}
        self._last_refresh = {}

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


async def _is_voice_note_msg(page, full_selector: str, index: int) -> bool:
    """Best-effort: does this (textless) message bubble render as a voice
    note? No STT engine is wired up to actually transcribe it (see
    backend/agents/dispatcher.py's transcript_confidence/media_type
    interface) — this only distinguishes "a voice note we can't listen to
    yet" from any other textless bubble (a reaction, a system message, an
    unsupported media type) so the former gets a graceful reply instead of
    being silently dropped like every textless bubble was before. Checks
    several of WhatsApp Web's known voice-note markers defensively, same
    spirit as this file's other DOM-shape checks — a build where none of
    these match just falls back to the old silent-drop behaviour for that
    message, not an error."""
    try:
        return await page.evaluate(
            """([sel, idx]) => {
                const els = document.querySelectorAll(sel);
                if (idx >= els.length) return false;
                const el = els[idx];
                return !!(
                    el.querySelector('[data-testid="audio-play"]') ||
                    el.querySelector('[data-icon="audio-play"]') ||
                    el.querySelector('[data-testid="ptt-canvas"]') ||
                    el.querySelector('audio')
                );
            }""",
            [full_selector, index],
        )
    except Exception:
        logger.exception("inbound: _is_voice_note_msg failed at index %d", index)
        return False


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


async def _extract_reply_context(page, full_selector: str, index: int) -> Optional[dict]:
    """Concurrent Task Engine Step 2B — structured extraction of a WhatsApp
    reply's "quoted message" context, replacing Step 2A's discovery-only
    dump now that real production samples have confirmed the shape:

        <div data-testid="quoted-message">
          ...
          <span ...>You</span>                          <- quoted sender
          ...
          <span data-testid="selectable-text" ...>
            {the quoted card's full rendered text}
          </span>
        </div>

    Two real production samples (2026-08-05, group "Talentgram Casting
    Pipeline") both showed a `[data-testid="quoted-message"]` block whose
    `[data-testid="selectable-text"]` innerText was an exact copy of the
    confirmation card's rendered text, with NO `data-id`/message-id
    attribute anywhere in the quoted block itself. Given that, this
    function still does a best-effort walk up a few ancestors for a
    `data-id` (the same pattern _extract_message_info already uses for
    phone extraction) in case a different WhatsApp Web build/version
    exposes one — but the caller (agents/tasks.find_task_by_quoted_text,
    backend side) treats quotedMessageId as optional and falls back to
    matching quotedText, which real evidence shows is reliably present.
    Returns None for a plain, non-reply message — never raises."""
    try:
        return await page.evaluate(
            """([sel, idx]) => {
                const els = document.querySelectorAll(sel);
                if (idx >= els.length) return null;
                const el = els[idx];
                const quoted = el.querySelector('[data-testid="quoted-message"]');
                if (!quoted) return null;
                const textEl = quoted.querySelector('[data-testid="selectable-text"]');
                const quotedText = textEl ? textEl.innerText : null;
                if (!quotedText) return null;
                // First span with visible text inside the quoted block is
                // the quoted message's own sender label ("You" for our own
                // sent cards, or a contact name for someone else's).
                let quotedSender = null;
                const spans = quoted.querySelectorAll('span[dir="auto"]');
                for (const s of spans) {
                    const t = (s.innerText || '').trim();
                    if (t) { quotedSender = t; break; }
                }
                // Best-effort id search — checked, not assumed present.
                let quotedMessageId = null;
                let node = quoted;
                for (let i = 0; i < 4 && node; i++) {
                    const did = node.getAttribute ? node.getAttribute('data-id') : null;
                    if (did) { quotedMessageId = did; break; }
                    node = node.parentElement;
                }
                return {quotedText, quotedSender, quotedMessageId};
            }""",
            [full_selector, index],
        )
    except Exception:
        logger.exception("inbound: _extract_reply_context failed at index %d", index)
        return None


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
    for i, p in enumerate(participants):
        raw = (p.get("raw_text") or "").strip().lower()
        matched = needle in raw or (needle_digits and needle_digits in (p.get("phone") or ""))
        if not matched:
            continue
        phone = p.get("phone")
        if not phone:
            # Live bug (2026-07-21): Group Info renders one participant's
            # name, about-text, business name, and phone as several
            # CONSECUTIVE rows, not one — matching by name lands on a row
            # with no phone of its own. Without this, the SAME person's
            # conversation could get keyed by a hashed pseudo-identity on
            # one message (name matched) and by their real phone on
            # another (phone matched directly), splitting their own
            # conversation state in two. Search a small window of
            # neighboring rows for whichever one carries a phone.
            for j in range(max(0, i - 3), min(len(participants), i + 4)):
                if participants[j].get("phone"):
                    phone = participants[j]["phone"]
                    break
        return True, phone
    return False, None


async def _scan_group_for_new_messages(
    page, group_name: str, participants_cache: "GroupParticipantsCache"
) -> Tuple[list[dict], float, float]:
    """Scan the ALREADY-OPEN, ALREADY-VERIFIED conversation for the most
    recent messages, returning ones that are new + incoming + not yet
    processed. Reuses sender._resolve_scope / sender._is_outgoing_msg
    directly rather than re-deriving selector logic.

    Returns (new_messages, dom_detection_sec, message_extraction_sec) — the
    latency-investigation split of the old single `scan_sec`: dom_detection
    is "how long until we know how many messages exist" (scope resolution +
    count), message_extraction is the per-message direction/text/sender
    extraction work in the loop below. Both are 0.0 when nothing new is
    found (the loop still runs over already-processed tail messages, so
    message_extraction is not literally zero unless start==n, but stays
    accurately attributed either way since it wraps the whole loop body,
    not just the messages that end up in `new_messages`)."""
    t_dom_start = time.monotonic()
    scope = await sender._resolve_scope(page)
    full_sel = f"{scope} [data-testid^='conv-msg-']"
    try:
        loc = page.locator(full_sel)
        n = await loc.count()
    except Exception:
        logger.exception("inbound: message count failed for group=%r", group_name)
        return [], time.monotonic() - t_dom_start, 0.0
    dom_detection_sec = time.monotonic() - t_dom_start
    logger.info("inbound: DIAG scan group=%r scope=%r n=%d", group_name, scope, n)
    # TEMPORARY (2026-08-11 inbound-detection investigation): per-cycle
    # newest-message marker — a real message physically arriving should
    # move this id every time the count (n, above) also moves; if n
    # increases but this id never changes, the count itself is stale.
    if n > 0:
        try:
            newest_id = await loc.nth(n - 1).get_attribute("data-testid")
        except Exception:
            newest_id = "<unreadable>"
        # Reuses the existing _extract_message_info parser (unmodified) —
        # no new selector/parsing logic, just an extra diagnostic call.
        # data-pre-plain-text carries WhatsApp Web's own bracketed
        # timestamp ("[10:30 AM, 21/07/2026] Name: "), the closest existing
        # signal to "latest timestamp" without inventing a new one.
        try:
            newest_info = await _extract_message_info(page, full_sel, n - 1)
            newest_pre_plain = newest_info.get("prePlainText")
        except Exception:
            newest_pre_plain = "<unreadable>"
        logger.info(
            "inbound: DIAG newest message group=%r newest_message_id=%r newest_pre_plain_text=%r",
            group_name, newest_id, newest_pre_plain,
        )

    # TEMPORARY (2026-08-11 Phase 5) — Stage 1: raw DOM snapshot, every
    # poll, of the last 5 bubbles exactly as rendered. Read-only, no
    # interpretation — dumped as-is for correlation against every later
    # stage. Independent of and does not affect the scan loop below.
    if n > 0:
        try:
            bubbles = await page.evaluate("""([sel, start]) => {
                const els = document.querySelectorAll(sel);
                const out = [];
                for (let i = start; i < els.length; i++) {
                    const el = els[i];
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const pp = el.querySelector('[data-pre-plain-text]');
                    out.push({
                        index: i,
                        dom_node_id: el.id || null,
                        data_testid: el.getAttribute('data-testid'),
                        data_id: el.getAttribute('data-id'),
                        aria_label: el.getAttribute('aria-label'),
                        class_list: el.className || null,
                        pre_plain_text: pp ? pp.getAttribute('data-pre-plain-text') : null,
                        text: (el.textContent || '').trim().slice(0, 200),
                        has_tail_out: !!el.querySelector('[data-icon="tail-out"], [data-testid="tail-out"]'),
                        has_tail_in: !!el.querySelector('[data-icon="tail-in"], [data-testid="tail-in"]'),
                        has_aria_you: !!el.querySelector('span[aria-label="You:"]'),
                        rect: {left: Math.round(rect.left), top: Math.round(rect.top),
                               right: Math.round(rect.right), bottom: Math.round(rect.bottom)},
                        visible: rect.width > 0 && rect.height > 0,
                        display: style.display, opacity: style.opacity,
                    });
                }
                return out;
            }""", [full_sel, max(0, n - 5)])
        except Exception as exc:
            bubbles = [{"error": str(exc)}]
        for b in bubbles:
            logger.info("inbound: RAW BUBBLE group=%r %s", group_name, b)

    # Only the tail — new messages always arrive at the bottom, and this
    # keeps the scan cheap regardless of how long the chat history is.
    start = max(0, n - 15)
    new_messages: list[dict] = []
    # Fetched at most once per scan call (not per message), only if a
    # message actually needs it — group_members security mode is the only
    # consumer, and most scan cycles find nothing new to check.
    participants: Optional[list] = None
    participants_fetched = False
    t_extraction_start = time.monotonic()

    for i in range(start, n):
        try:
            testid = await loc.nth(i).get_attribute("data-testid")
        except Exception:
            continue
        message_id = testid or None

        # TEMPORARY (2026-08-11 inbound-detection investigation): one snippet
        # fetch, reused for whichever branch below needs it (was previously
        # fetched separately/inconsistently per-branch, or not at all).
        # `diag_context` only enriches sender.py's DIRECTION DETECTION log —
        # it is not read by any decision logic in _is_outgoing_msg, and the
        # other existing caller (_find_outgoing_with_text) doesn't pass it,
        # so this is purely additive.
        try:
            snippet = (await loc.nth(i).inner_text()).strip()[:80]
        except Exception:
            snippet = "<unreadable>"

        # TEMPORARY (2026-08-11 Phase 5) — Stage 9: one correlation id per
        # scan candidate, carried through every subsequent log for this
        # message (direction/seen-cache/gate/dispatch/reply), so a single
        # trace_id can be grepped end-to-end. Nothing reads/writes trace_id
        # outside logging — no decision anywhere is based on it.
        trace_id = uuid.uuid4().hex[:8]
        diag_context = {"group": group_name, "message_id": message_id, "text": snippet, "trace_id": trace_id}

        # Stage 2 — this candidate is under consideration (it fell within
        # the tail-15 scan window, per the existing `start`/`n` bounds
        # computed above — unchanged selection logic, just logged now).
        logger.info(
            "inbound: SCAN CANDIDATE trace_id=%s group=%r message_id=%r text=%r "
            "reason_selected=%r",
            trace_id, group_name, message_id, snippet,
            f"tail-window index {i} of {n} (start={start})",
        )

        direction = await sender._is_outgoing_msg(page, full_sel, i, diag_context=diag_context)
        if direction is True:
            # Ours — mark seen (if we have a stable id) so we never
            # re-evaluate it, and never dispatch it. Full classification
            # detail (reason/geometry diag) is in sender.py's DIRECTION
            # DETECTION log immediately above this one.
            logger.info("inbound: TRACE trace_id=%s last_stage=DIRECTION_OUTGOING (dropped)", trace_id)
            if message_id:
                await _mark_processed(message_id)
            continue
        if direction is None:
            diag = await _direction_diag(page, full_sel, i)
            logger.warning(
                "inbound: direction undeterminable for group=%r index=%d testid=%r "
                "text=%r diag=%s — skipping (fail closed, not guessing)",
                group_name, i, testid, snippet, diag,
            )
            logger.info("inbound: TRACE trace_id=%s last_stage=DIRECTION_UNKNOWN (dropped)", trace_id)
            if message_id:
                await _mark_processed(message_id)
            continue

        try:
            text = (await loc.nth(i).inner_text()).strip()
        except Exception:
            text = ""

        # Step 2B — structured reply context (None for a plain message; a
        # log line only fires when this message actually IS a reply, so a
        # normal scan cycle stays quiet).
        reply_context = await _extract_reply_context(page, full_sel, i)
        if reply_context:
            logger.info(
                "inbound: DIAG reply context extracted group=%r index=%d testid=%r "
                "quoted_sender=%r quoted_message_id=%r quoted_text=%r",
                group_name, i, testid, reply_context.get("quotedSender"),
                reply_context.get("quotedMessageId"), (reply_context.get("quotedText") or "")[:200],
            )

        info = await _extract_message_info(page, full_sel, i)
        pre_plain = info.get("prePlainText")
        phone = info.get("phone")

        if not message_id:
            message_id = _fallback_message_id(group_name, text, pre_plain)

        # Stage 4 — seen-cache lookup. Peeked BEFORE calling the real
        # (unmodified) _already_processed, purely to report which layer
        # would answer — never changes what _already_processed itself does.
        in_memory_hit = message_id in _seen_cache
        already_seen = await _already_processed(message_id)
        mongo_hit = already_seen and not in_memory_hit
        logger.info(
            "inbound: SEEN-CACHE trace_id=%s group=%r message_id=%r | "
            "In-memory=%s Mongo=%s TTL=%s | Decision=%s",
            trace_id, group_name, message_id, in_memory_hit, mongo_hit,
            f"enforced by Mongo TTL index ({config.INBOUND_DEDUP_TTL_SEC}s), not independently queryable here",
            "ALREADY_SEEN" if already_seen else "NEW",
        )
        # Stage 5 — the actual gate, immediately before the branch it guards.
        logger.info(
            "inbound: NEW MESSAGE GATE trace_id=%s | %s | reason=%s",
            trace_id, "BLOCKED" if already_seen else "ALLOWED",
            "seen-cache hit" if already_seen else "not previously seen",
        )
        if already_seen:
            logger.info("inbound: TRACE trace_id=%s last_stage=SEEN_CACHE_BLOCKED (dropped)", trace_id)
            continue

        media_type = None
        if not text:
            if await _is_voice_note_msg(page, full_sel, i):
                # Can't transcribe it (no STT engine wired up), but it's
                # worth a graceful reply instead of the old silent drop —
                # forward it with media_type set and empty text so the
                # backend can tell the user plainly. Every OTHER textless
                # bubble (reactions, system messages, unrecognized media)
                # keeps the old silent-drop behaviour, unchanged.
                media_type = "voice_note"
            else:
                # TEMPORARY (2026-08-11): this drop was completely silent —
                # no log at any level. A non-text, non-voice-note bubble
                # (reaction, system message, unrecognized media) is dropped
                # here; logging it closes the last "vanishes with zero
                # trace" gap in this loop.
                logger.info(
                    "inbound: message DROPPED (textless, not a voice note) "
                    "trace_id=%s group=%r index=%d testid=%r pre_plain_text=%r",
                    trace_id, group_name, i, testid, pre_plain,
                )
                logger.info("inbound: TRACE trace_id=%s last_stage=TEXTLESS_DROPPED (dropped)", trace_id)
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
            "media_type": media_type,
            "reply_context": reply_context,
            "trace_id": trace_id,  # TEMPORARY (2026-08-11 Phase 5) — carried into poll_once's dispatch/reply stages
        })

    message_extraction_sec = time.monotonic() - t_extraction_start
    return new_messages, dom_detection_sec, message_extraction_sec


async def _post_inbound(http: httpx.AsyncClient, *, group_name: str, sender_phone: str,
                         sender_name: Optional[str], text: str, message_id: str,
                         sender_is_group_member: Optional[bool] = None,
                         media_type: Optional[str] = None,
                         replied_to_message_id: Optional[str] = None,
                         replied_quoted_text: Optional[str] = None,
                         trace_id: Optional[str] = None) -> Optional[dict]:
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
                "media_type": media_type,
                "replied_to_message_id": replied_to_message_id,
                "replied_quoted_text": replied_quoted_text,
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
        logger.info(
            "inbound: DISPATCH RESULT trace_id=%s status=OK latency_ms=%d "
            "response_size_bytes=%d error=None",
            trace_id, latency_ms, len(resp.content or b""),
        )
        await _update_worker_status(dispatcher_status="ready")
        return body
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "inbound: DISPATCH RESULT trace_id=%s status=FAILED latency_ms=%d "
            "response_size_bytes=None error=%r",
            trace_id, latency_ms, str(exc),
        )
        logger.exception(
            "inbound: dispatch to backend FAILED group=%r sender=%r message_id=%r "
            "(message left unprocessed; will retry only if it re-appears unseen — "
            "it will not, since marking-seen happens after a successful dispatch)",
            group_name, sender_phone, message_id,
        )
        await _update_worker_status(dispatcher_status="unreachable")
        return None


async def _send_reply(
    page, group_name: str, reply_text: str
) -> Tuple[float, Dict[str, float], Optional[str]]:
    """Returns (elapsed seconds spent sending, per-stage SEND_TIMING
    breakdown from sender.send_whatsapp_message, the sent message's own
    WhatsApp message id if captured) — (0.0, {}, None) if there was nothing
    to send. The message id is what makes a task's confirmation card
    reply-addressable (Concurrent Task Engine) — see _post_task_sent."""
    if not reply_text:
        return 0.0, {}, None
    t0 = time.monotonic()
    try:
        result = await sender.send_whatsapp_message(
            page, destination_type="group", destination=group_name, message_body=reply_text,
            fast=True,
        )
        elapsed = time.monotonic() - t0
        logger.info("inbound: reply send result group=%r state=%s elapsed_sec=%.2f",
                     group_name, result.get("state"), elapsed)
        return elapsed, (result.get("timing") or {}), result.get("sent_message_id")
    except Exception:
        logger.exception("inbound: failed to send reply into group=%r", group_name)
        return time.monotonic() - t0, {}, None


async def _post_task_sent(http: httpx.AsyncClient, *, agent_id: str, operation_id: str,
                           message_id: str) -> None:
    """Concurrent Task Engine (2026-08-05) — reports the WhatsApp message id a
    task's confirmation/clarification card actually got once sent, so a later
    reply to that message can be routed back to this operation_id (see
    backend/agents/tasks.py, POST /api/agents/whatsapp/task-sent). Best-effort:
    a failure here just means that one task stays reply-unaddressable (still
    resolvable via explicit operation-id or the existing session fallback), so
    it must never raise into the caller's send/report flow."""
    try:
        resp = await http.post(
            f"{config.AGENTS_BACKEND_URL}/api/agents/whatsapp/task-sent",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"agent_id": agent_id, "operation_id": operation_id, "message_id": message_id},
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info("inbound: task-sent reported operation_id=%r message_id=%r",
                     operation_id, message_id)
    except Exception:
        logger.exception(
            "inbound: failed to report task-sent operation_id=%r message_id=%r "
            "(task stays reply-unaddressable; other resolution paths still work)",
            operation_id, message_id,
        )


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


AGENT_CONFIG_COLLECTION = "whatsapp_agent_config"
INVALID_CONFIGURATION = "INVALID_CONFIGURATION"

# ── INVALID_CONFIGURATION: single source of truth ───────────────────────
# The DATABASE is authoritative: whatsapp_agent_config.config_status is what
# the admin dashboard reads, and it is only ever written by
# _mark_invalid_configuration / _clear_invalid_configuration below.
#
# The two sets here are a pure in-memory CACHE of that state, held only to
# avoid re-searching a group we already know is missing (each such search
# costs a full sidebar lookup plus a ~39 KB forensic DOM snapshot — measured
# at ~500/hour, which filled the Atlas quota and blocked writes cluster-wide).
# They never hold state the DB does not, so they cannot drift from it.
#
#   _INVALID_GROUPS       — skip these; refreshed from reality, not trusted.
#   _PENDING_REVALIDATION — known-flagged in the DB and due one probe; a
#                           successful open clears the DB flag.
#
# Revalidation happens on exactly four events, never on a timer:
#   worker start        -> load_invalid_state() seeds from the DB
#   Refresh Groups      -> KnownGroupsCache.get() moves invalid -> pending
#   config change       -> same refresh path (and the API clears the flag too)
#   re-authentication   -> _rearm_for_new_generation() (2026-08-11) — the
#                          account was switched (or the same account
#                          reconnected); see that function's docstring for
#                          why a switch specifically needs this on top of
#                          the three events above.
_INVALID_GROUPS: set[str] = set()
_PENDING_REVALIDATION: set[str] = set()

# Reconnect reliability (2026-08-11) — the generation this process last saw
# from WhatsAppSession.generation. None until the first check, so the very
# first poll cycle always treats itself as "a fresh (re-)auth" and runs the
# rebuild path once — cheap (everything it clears starts empty anyway) and
# means first boot and every subsequent reconnect go through the exact same
# code path, rather than first boot being a special case that's never
# actually exercised until the first real reconnect happens in production.
_last_seen_generation: Optional[int] = None

# Grace window start (monotonic) — see config.REAUTH_GRACE_SEC. Sharing one
# module-level timestamp (rather than per-group) is deliberate: the race
# this guards against (WhatsApp Web's chat list still syncing right after a
# fresh login) affects every group identically, not one at a time.
_state_rebuilt_at: float = 0.0

# Rate-limits the previously-completely-silent _INVALID_GROUPS skip log
# (see poll_once) to once per group per this many seconds, so a poisoned
# group is now visible in logs without spamming one line per 2s poll.
_INVALID_GROUP_SKIP_LOG_INTERVAL_SEC = 300.0
_invalid_group_last_logged: Dict[str, float] = {}

# Live worker-health sub-statuses, mirrored onto whatsapp_sessions so the
# admin UI can show them (see _update_worker_status). Kept in-process too so
# each write only happens on an actual transition, not every 2s poll cycle.
_last_written_status: Dict[str, object] = {}


async def _update_worker_status(**fields) -> None:
    """Upsert onto the SAME whatsapp_sessions singleton doc session.py
    already owns (session.py:_update_session_doc) — deliberately not a new
    collection. Only writes when something actually changed, so this never
    turns into a write on every 2s poll cycle for fields that rarely move."""
    changed = {k: v for k, v in fields.items() if _last_written_status.get(k) != v}
    if not changed:
        return
    _last_written_status.update(changed)
    try:
        await get_db().whatsapp_sessions.update_one(
            {"id": "default"}, {"$set": changed}, upsert=True,
        )
    except Exception:
        logger.exception("inbound: failed to write worker status %s", changed)


async def _rearm_for_new_generation(
    session, groups_cache: "KnownGroupsCache", participants_cache: "GroupParticipantsCache",
) -> None:
    """The account just (re-)authenticated (WhatsAppSession.generation
    changed) — every piece of state this module holds that could plausibly
    depend on WHICH account is authenticated gets rebuilt here, unconditionally,
    with no assumption about whether the account is actually different from
    before (cheap either way — everything it touches self-heals in at most
    one more probe/refresh cycle if nothing actually changed).

    This is the fix for the bug where switching accounts via a fresh QR
    scan left the inbound pipeline permanently silent: previously nothing
    connected "an authentication just happened" to "account-specific state
    must be rebuilt" — only a full process restart reset these Python
    module globals. Now it's a single trigger this module checks on every
    poll cycle, so a reconnect that happens entirely in-process (the
    existing worker.py heartbeat-failure restart path, session.stop() +
    session.start(), no OS-level restart) still gets picked up here on the
    very next 2s tick."""
    global _state_rebuilt_at
    # Same safe pattern the existing config-change path already uses
    # (KnownGroupsCache.get): move to pending-revalidation rather than
    # blindly clearing, so each previously-flagged group still gets exactly
    # one real probe before the dashboard reports it healthy again.
    if _INVALID_GROUPS:
        _PENDING_REVALIDATION.update(_INVALID_GROUPS)
        _INVALID_GROUPS.clear()
    _seen_cache.clear()
    _invalid_group_last_logged.clear()
    groups_cache.clear()
    participants_cache.clear()
    _state_rebuilt_at = time.monotonic()
    logger.warning(
        "inbound: (re-)authentication detected — generation=%d session_id=%s — "
        "rebuilding all account-specific state (invalid-group cache, message-seen "
        "cache, group-participants cache, known-groups cache)",
        session.generation, session.session_id,
    )
    await _update_worker_status(
        generation=session.generation,
        session_id=session.session_id,
        connected_phone_number=session.own_phone_number,
        listener_status="active",
    )


async def load_invalid_state() -> None:
    """Seed the cache from the DB at worker start so previously-flagged groups
    are revalidated exactly once, and recover automatically if they now exist."""
    _INVALID_GROUPS.clear()
    _PENDING_REVALIDATION.clear()
    try:
        cursor = get_db()[AGENT_CONFIG_COLLECTION].find(
            {"config_status": INVALID_CONFIGURATION}, {"config_error_group": 1}
        )
        async for doc in cursor:
            group = doc.get("config_error_group")
            if group:
                _PENDING_REVALIDATION.add(group)
    except Exception:
        logger.exception("inbound: failed to load INVALID_CONFIGURATION state (will revalidate on refresh)")
    if _PENDING_REVALIDATION:
        logger.info("inbound: revalidating previously invalid group(s) %s on startup",
                    sorted(_PENDING_REVALIDATION))


async def _flag_cleared_externally() -> bool:
    """True when the operator cleared an INVALID_CONFIGURATION flag themselves.

    Updating an agent's config through the admin API unsets the flag, which is
    the operator's explicit "I fixed it, try again" signal — the DB is
    authoritative, so the worker follows it rather than holding a stale
    in-memory verdict.
    """
    try:
        still_flagged = await get_db()[AGENT_CONFIG_COLLECTION].count_documents(
            {"config_status": INVALID_CONFIGURATION,
             "config_error_group": {"$in": list(_INVALID_GROUPS)}},
            limit=1,
        )
        return still_flagged == 0
    except Exception:
        logger.exception("inbound: failed to check INVALID_CONFIGURATION flags")
        return False


async def _mark_invalid_configuration(group_name: str) -> None:
    """Flag every agent mapped to a missing group as INVALID_CONFIGURATION.

    Outbound sending already treats "group not in chat list" as terminal (the
    job is failed). This loop had no equivalent, so it re-searched the group
    every poll cycle forever. Persisted so the admin dashboard shows the
    operator exactly which group is wrong, without reading worker logs.
    """
    _INVALID_GROUPS.add(group_name)
    _PENDING_REVALIDATION.discard(group_name)
    message = (
        f"Configured WhatsApp group '{group_name}' could not be found.\n\n"
        "Update whatsapp_agent_config.group_names or restore the group."
    )
    logger.error("inbound: %s", message.replace("\n\n", " "))
    try:
        await get_db()[AGENT_CONFIG_COLLECTION].update_many(
            {"group_names": group_name},
            {"$set": {
                "config_status": INVALID_CONFIGURATION,
                "config_error": message,
                "config_error_group": group_name,
                "config_error_at": _now(),
            }},
        )
    except Exception:
        logger.exception("inbound: failed to persist INVALID_CONFIGURATION for %r", group_name)


async def _clear_invalid_configuration(group_name: str) -> None:
    """The group opened successfully — clear the flag and resume polling.

    Called only for a group that was actually flagged, so a healthy group
    never issues a redundant write on its normal 2s poll cycle.
    """
    _INVALID_GROUPS.discard(group_name)
    _PENDING_REVALIDATION.discard(group_name)
    try:
        await get_db()[AGENT_CONFIG_COLLECTION].update_many(
            {"config_error_group": group_name, "config_status": INVALID_CONFIGURATION},
            {"$unset": {"config_status": "", "config_error": "",
                        "config_error_group": "", "config_error_at": ""}},
        )
        logger.info("inbound: group=%r found again — INVALID_CONFIGURATION cleared, polling resumed",
                    group_name)
    except Exception:
        logger.exception("inbound: failed to clear INVALID_CONFIGURATION for %r", group_name)


async def poll_once(
    session, http: httpx.AsyncClient, groups_cache: KnownGroupsCache,
    participants_cache: "GroupParticipantsCache",
) -> None:
    global _last_seen_generation
    if session.generation != _last_seen_generation:
        _last_seen_generation = session.generation
        await _rearm_for_new_generation(session, groups_cache, participants_cache)

    groups = await groups_cache.get()
    if not groups:
        return
    # TEMPORARY (2026-08-11 inbound-detection investigation): per-cycle
    # summary — "number of chats found" per the requested instrumentation,
    # adapted to this architecture (configured-group polling, not a
    # chat-list-unread scan). session.generation/session_id included so a
    # log pull can be correlated against a specific authenticated session
    # without cross-referencing the diagnostics doc separately.
    logger.info(
        "inbound: POLL CYCLE configured_groups=%d groups=%s generation=%d session_id=%s",
        len(groups), groups, session.generation, session.session_id,
    )

    for group_name in groups:
        if group_name in _INVALID_GROUPS:
            now = time.monotonic()
            if now - _invalid_group_last_logged.get(group_name, 0.0) > _INVALID_GROUP_SKIP_LOG_INTERVAL_SEC:
                _invalid_group_last_logged[group_name] = now
                logger.warning(
                    "inbound: group=%r still flagged INVALID_CONFIGURATION — skipping "
                    "every poll cycle until the operator fixes the mapping or a "
                    "re-authentication re-arms it (this line is rate-limited to once "
                    "per %.0fs per group)", group_name, _INVALID_GROUP_SKIP_LOG_INTERVAL_SEC,
                )
            continue
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
                if status == "NOT_FOUND":
                    grace_remaining = config.REAUTH_GRACE_SEC - (time.monotonic() - _state_rebuilt_at)
                    if grace_remaining > 0:
                        # A fresh (re-)auth happened recently enough that
                        # WhatsApp Web's chat list may still be syncing —
                        # this is exactly the race that used to poison every
                        # group permanently on an account switch. Don't
                        # judge it yet; just retry next cycle.
                        logger.info(
                            "inbound: group=%r NOT_FOUND within %.0fs of last (re-)auth "
                            "(%.0fs of grace remaining) — treating as still-settling, "
                            "not marking invalid", group_name, config.REAUTH_GRACE_SEC,
                            grace_remaining,
                        )
                    else:
                        await _mark_invalid_configuration(group_name)
                continue
            if group_name in _PENDING_REVALIDATION:
                # Flagged in the DB, but it opened this time — the operator
                # fixed the mapping or restored the group. Auto-recover.
                await _clear_invalid_configuration(group_name)

            try:
                new_messages, dom_detection_sec, message_extraction_sec = await _scan_group_for_new_messages(
                    page, group_name, participants_cache
                )
            except Exception:
                logger.exception("inbound: scan failed for group=%r", group_name)
                continue
            # DOM scan/parse cost for this cycle — same for every message
            # found in it (one scan can surface 0+ new messages), included
            # in each one's own TIMING line below so the backend/DOM split
            # of end-to-end latency is visible without guessing.

            for msg in new_messages:
                trace_id = msg.get("trace_id")
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
                    logger.info("inbound: TRACE trace_id=%s last_stage=NO_PHONE_RESOLVED (dropped)", trace_id)
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
                    "inbound: new message trace_id=%s group=%r sender_name=%r sender_phone=%r "
                    "sender_is_group_member=%r message_id=%r text=%r",
                    trace_id, group_name, msg["sender_name"], phone, msg["sender_is_group_member"],
                    msg["message_id"], msg["text"][:120],
                )
                await _update_worker_status(last_incoming_at=_now().isoformat())
                # Stage 6 — before backend POST.
                logger.info(
                    "inbound: DISPATCH START trace_id=%s message_id=%r text=%r group=%r timestamp=%r",
                    trace_id, msg["message_id"], msg["text"][:120], group_name,
                    datetime.now(timezone.utc).isoformat(),
                )

                reply_context = msg.get("reply_context") or {}
                backend_task = asyncio.create_task(_post_inbound(
                    http,
                    group_name=group_name,
                    sender_phone=phone,
                    sender_name=msg["sender_name"],
                    text=msg["text"],
                    message_id=msg["message_id"],
                    sender_is_group_member=msg["sender_is_group_member"],
                    media_type=msg.get("media_type"),
                    replied_to_message_id=reply_context.get("quotedMessageId"),
                    replied_quoted_text=reply_context.get("quotedText"),
                    trace_id=trace_id,
                ))
                done, _ = await asyncio.wait({backend_task}, timeout=ACK_THRESHOLD_SEC)
                ack_sent_sec = None
                if backend_task not in done:
                    ack_elapsed, _ack_timing, _ack_message_id = await _send_reply(page, group_name, ACK_TEXT)
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
                    logger.info("inbound: TRACE trace_id=%s last_stage=DISPATCH_FAILED (will retry)", trace_id)
                    continue

                await _mark_processed(msg["message_id"])
                await _update_worker_status(last_processed_at=_now().isoformat())

                reply = result.get("reply")
                operation_id = result.get("operation_id")
                reply_elapsed = 0.0
                send_timing: Dict[str, float] = {}
                if reply:
                    # Stage 7 — reply generated.
                    logger.info(
                        "inbound: REPLY GENERATED trace_id=%s message_id=%r characters=%d preview=%r",
                        trace_id, msg["message_id"], len(reply), reply[:120],
                    )
                    reply_elapsed, send_timing, sent_message_id = await _send_reply(page, group_name, reply)
                    if sent_message_id:
                        await _update_worker_status(last_reply_at=_now().isoformat())
                        logger.info(
                            "inbound: REPLY SENT trace_id=%s message_id=%r sent_message_id=%r "
                            "reply_sent=True failure=None",
                            trace_id, msg["message_id"], sent_message_id,
                        )
                        logger.info("inbound: TRACE trace_id=%s last_stage=REPLY_SENT (complete)", trace_id)
                    else:
                        logger.info(
                            "inbound: REPLY SEND FAILED trace_id=%s message_id=%r reply_sent=False "
                            "failure=%r", trace_id, msg["message_id"], "send_whatsapp_message returned no sent_message_id",
                        )
                        logger.info("inbound: TRACE trace_id=%s last_stage=REPLY_SEND_FAILED", trace_id)
                    # Concurrent Task Engine (2026-08-05) — operation_id is only
                    # set when `reply` is a task's confirmation/clarification
                    # card; report the WhatsApp message id it actually got so a
                    # later reply-to-this-message can be routed back to it.
                    if operation_id and sent_message_id:
                        asyncio.create_task(_post_task_sent(
                            http, agent_id="casting-agent",
                            operation_id=operation_id, message_id=sent_message_id,
                        ))
                else:
                    logger.info(
                        "inbound: TRACE trace_id=%s last_stage=DISPATCHED_NO_REPLY "
                        "(backend handled it but returned no reply text — complete)", trace_id,
                    )
                t_total = time.monotonic() - t_detected
                logger.info(
                    "inbound: TIMING message_id=%r dom_detection_sec=%.2f "
                    "message_extraction_sec=%.2f backend_sec=%.2f reply_send_sec=%.2f "
                    "reply_open_or_verify_chat=%.2f reply_composer_wait=%.2f reply_typing=%.2f "
                    "reply_send_click=%.2f reply_delivery_verify=%.2f ack_sent_at_sec=%s "
                    "total_sec=%.2f",
                    msg["message_id"], dom_detection_sec, message_extraction_sec,
                    t_backend_done - t_detected, reply_elapsed,
                    send_timing.get("open_or_verify_chat", 0.0), send_timing.get("composer_wait", 0.0),
                    send_timing.get("typing", 0.0), send_timing.get("send_click", 0.0),
                    send_timing.get("delivery_verify", 0.0), ack_sent_sec, t_total,
                )
                _record_latency(t_total)


# TEMPORARY (2026-08-11 Phase 4, "why does the DOM never show new
# messages" investigation) — a deliberately SEPARATE, read-only diagnostic
# routine. Does NOT call _scan_group_for_new_messages, _is_outgoing_msg,
# _already_processed, sender._open_group_chat, or any other inbound-
# detection/seen-cache/direction-detection/group-routing code in this
# codebase — every DOM read below is freshly written, independent of all
# of that. The only "side effect" is clicking a chat-list row directly
# (bypassing the search box entirely, so it shares no code with
# sender._open_group_chat) to read its latest message content — the
# existing poll loop already opens these same chats far more often (every
# ~2-6s) via the normal search path, so this adds no new class of effect,
# just an independent second observation on a 120s cadence. Never calls
# _mark_processed / writes to any cache / touches config — pure inspection.
LIVE_SESSION_VERIFY_SEC = 120.0
_last_live_verify_at: float = 0.0
_PROCESS_START_MONOTONIC = time.monotonic()
# Previous cycle's per-group snapshot, for the requested "did anything
# change since last cycle" comparison. Diagnostic-only — distinct from
# _seen_cache/_INVALID_GROUPS and everything else the real pipeline reads.
_prev_verify_group_state: Dict[str, dict] = {}

_RECONNECT_BANNER_PHRASES = [
    "Trying to reconnect", "Reconnecting", "Connecting",
    "Phone not connected", "Computer not connected to the internet",
    "Server error", "Unable to connect",
]
_TIMESTAMP_LIKE_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM|am|pm)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")


def _match_kinds(configured: str, candidate: str) -> Tuple[bool, bool, bool, float]:
    """(exact, case_insensitive_exact, substring, fuzzy_ratio) — pure
    Python string comparison, no DOM/selector involvement, so this can
    never be confused with (or accidentally reuse) the real group-matching
    code in sender.py."""
    exact = configured == candidate
    case_match = configured.strip().lower() == candidate.strip().lower()
    substring = configured.strip().lower() in candidate.strip().lower() or candidate.strip().lower() in configured.strip().lower()
    ratio = difflib.SequenceMatcher(None, configured.lower(), candidate.lower()).ratio()
    return exact, case_match, substring, ratio


async def _deep_whatsapp_verification(session, groups: list[str]) -> None:
    """Read-only diagnostic dump — see this file's module docstring header
    above for the full non-interference guarantee. Builds one big,
    human-readable block per the requested format rather than scattering
    many small log lines."""
    lines: list[str] = ["", "========== WHATSAPP LIVE VERIFICATION =========="]
    page = session.page
    if page is None:
        lines.append("Page: None — session not currently attached to a browser page.")
        lines.append("========== END VERIFICATION ==========")
        logger.warning("\n".join(lines))
        return

    now_mono = time.monotonic()

    # --- 1. Account information --------------------------------------
    phone = session.own_phone_number or "Unknown"
    display_name = "Unknown"  # never derived anywhere in this codebase; see session.py's own-phone docstring for why
    lines.append("-- Account --")
    lines.append(f"Session ID: {session.session_id}")
    lines.append(f"Generation: {session.generation}")
    lines.append(f"Phone: {phone}")
    lines.append(f"Display Name: {display_name}")

    # --- 7/8. Browser connection + page freshness (one evaluate call) --
    page_state: dict = {}
    try:
        page_state = await page.evaluate("""(phrases) => {
            return (async () => {
                const bodyText = document.body.innerText || '';
                const banners = phrases.filter(p => bodyText.includes(p));
                let serviceWorkers = [];
                try {
                    if (navigator.serviceWorker) {
                        const regs = await navigator.serviceWorker.getRegistrations();
                        serviceWorkers = regs.map(r => ({
                            scope: r.scope, active: !!r.active,
                            installing: !!r.installing, waiting: !!r.waiting,
                        }));
                    }
                } catch (e) {}
                let memory = null;
                try {
                    if (performance.memory) {
                        memory = {
                            used_js_heap_mb: Math.round(performance.memory.usedJSHeapSize / 1048576),
                            total_js_heap_mb: Math.round(performance.memory.totalJSHeapSize / 1048576),
                        };
                    }
                } catch (e) {}
                return {
                    url: location.href, title: document.title,
                    visibility_state: document.visibilityState, has_focus: document.hasFocus(),
                    online: navigator.onLine, banners_found: banners,
                    service_workers: serviceWorkers, memory: memory,
                };
            })();
        }""", _RECONNECT_BANNER_PHRASES)
    except Exception as exc:
        page_state = {"error": str(exc)}

    context_pages = 0
    try:
        context_pages = len(page.context.pages)
    except Exception:
        pass

    lines.append("")
    lines.append("-- Browser Connection --")
    lines.append(f"navigator.onLine: {page_state.get('online')}")
    lines.append(f"document.visibilityState: {page_state.get('visibility_state')}")
    lines.append(f"Service worker(s): {page_state.get('service_workers')}")
    lines.append(f"WebSocket status: not directly observable from outside the page's own JS "
                 f"(WhatsApp Web doesn't expose one on `window`); using service worker "
                 f"active-state + absence of reconnect banners as the closest proxies")
    lines.append(f"Page title: {page_state.get('title')!r}")
    lines.append(f"Loading/connection/offline banners found: {page_state.get('banners_found')}")

    lines.append("")
    lines.append("-- Page Freshness --")
    lines.append(f"Current URL: {page_state.get('url')}")
    lines.append(f"Page title: {page_state.get('title')!r}")
    lines.append(f"Context count: 1 (this process launches exactly one BrowserContext — see session.py)")
    lines.append(f"Tab count (pages in context): {context_pages} (expect exactly 1)")
    lines.append(f"Focused tab (document.hasFocus()): {page_state.get('has_focus')}")
    lines.append(f"Memory usage: {page_state.get('memory') or 'unavailable (performance.memory not exposed)'}")
    lines.append(f"Browser process uptime: {now_mono - _PROCESS_START_MONOTONIC:.0f}s")
    lines.append(f"Session (since last auth) uptime: {now_mono - _state_rebuilt_at:.0f}s")

    # --- 9. Worker configuration --------------------------------------
    lines.append("")
    lines.append("-- Worker Configuration --")
    lines.append(f"Configured groups: {groups}")
    lines.append(f"Configured backend URL: {config.AGENTS_BACKEND_URL}")
    lines.append(f"Polling interval: {config.INBOUND_POLL_SEC}s")
    lines.append(f"Current generation: {session.generation}")
    lines.append(f"Current session ID: {session.session_id}")
    lines.append(f"Current browser context pages: {context_pages}")

    # --- Search-box active-filter check (added after cycles 1-3 showed
    # only ONE chat ever visible, consistently, across 4.5+ minutes of a
    # settled session — the sidebar may be FILTERED by leftover text in
    # the chat-list search box from the real poll loop's own group search,
    # not because other chats don't exist. Independent selector list, not
    # imported from sender.py, per "do not reuse group routing". ---------
    search_box_value = None
    try:
        search_box_value = await page.evaluate("""() => {
            const candidates = [
                '#side input[data-tab="3"]',
                '#side div[contenteditable="true"][data-tab="3"]',
                '[data-testid="chat-list-search"] input',
                '[data-testid="chat-list-search"]',
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el) {
                    const v = el.value !== undefined ? el.value : (el.textContent || '');
                    if (v) return {selector: sel, value: v};
                }
            }
            return {selector: null, value: null};
        }""")
    except Exception as exc:
        search_box_value = {"error": str(exc)}
    lines.append("")
    lines.append("-- Chat-List Search Box (active-filter check) --")
    lines.append(f"Current value: {search_box_value}")
    lines.append("(a non-empty value here means the visible chat list below is FILTERED to "
                 "matches of this text, not the full chat list — this does not belong to this "
                 "diagnostic; it can only have been left behind by the real poll loop's own "
                 "group search, which this routine does not call or clear)")

    # --- 2. First 20 visible chats -------------------------------------
    try:
        rows = await page.evaluate("""() => {
            const out = [];
            const titleEls = document.querySelectorAll('[data-testid="cell-frame-title"]');
            const n = Math.min(20, titleEls.length);
            for (let i = 0; i < n; i++) {
                const el = titleEls[i];
                const title = el.textContent || '';
                let row = el;
                for (let j = 0; j < 6 && row && row.parentElement; j++) row = row.parentElement;
                const unreadEl = row ? row.querySelector('[aria-label*="unread" i]') : null;
                const pinnedEl = row ? row.querySelector('[data-icon*="pin" i], [aria-label*="pinned" i]') : null;
                const mutedEl = row ? row.querySelector('[data-icon*="mute" i], [aria-label*="muted" i]') : null;
                const spans = row ? row.querySelectorAll('span') : [];
                let lastTs = null;
                for (const s of spans) {
                    const t = (s.textContent || '').trim();
                    if (t) lastTs = lastTs === null ? t : lastTs;
                }
                out.push({
                    index: i, title: title,
                    unread_label: unreadEl ? unreadEl.getAttribute('aria-label') : null,
                    pinned: !!pinnedEl, muted: !!mutedEl,
                    last_visible_text: lastTs,
                });
            }
            return out;
        }""")
    except Exception as exc:
        rows = []
        lines.append(f"\n-- Visible Chats -- (FAILED: {exc})")

    lines.append("")
    lines.append(f"-- First {len(rows)} Visible Chats --")
    for r in rows:
        title = r.get("title") or ""
        codepoints = " ".join(f"{ord(c):02x}" for c in title)
        lines.append(f"Chat #{r['index'] + 1}")
        lines.append(f"  Title: {title!r}")
        lines.append(f"  Length: {len(title)}")
        lines.append(f"  Codepoints: {codepoints}")
        lines.append(f"  Unread: {r.get('unread_label') or 'None'}")
        lines.append(f"  Pinned: {r.get('pinned')}")
        lines.append(f"  Muted: {r.get('muted')}")
        # Anything rendered in the main list is, by WhatsApp Web's own
        # definition, NOT in the Archived folder — that's the only basis
        # for this field; the Archived folder itself is never opened here.
        lines.append(f"  Archived: False (present in main list)")
        lines.append(f"  Last visible timestamp/text: {r.get('last_visible_text')!r}")

    # --- 3. Compare against configured groups --------------------------
    lines.append("")
    lines.append("-- Configured-Group Matching --")
    found_index_by_group: Dict[str, Optional[int]] = {}
    for group_name in groups:
        best = None  # (ratio, row)
        exact_any = case_any = substring_any = False
        for r in rows:
            title = r.get("title") or ""
            exact, case_match, substring, ratio = _match_kinds(group_name, title)
            exact_any = exact_any or exact
            case_any = case_any or case_match
            substring_any = substring_any or substring
            if best is None or ratio > best[0]:
                best = (ratio, r)
        lines.append(f"Configured: {group_name!r}")
        lines.append(f"  Exact Match: {'YES' if exact_any else 'NO'}")
        lines.append(f"  Case Match: {'YES' if case_any else 'NO'}")
        lines.append(f"  Substring Match: {'YES' if substring_any else 'NO'}")
        if best and best[0] > 0.5:
            lines.append(f"  Fuzzy Match: YES")
            lines.append(f"  Matched Chat: {best[1].get('title')!r}")
            lines.append(f"  Similarity: {best[0] * 100:.0f}%")
            found_index_by_group[group_name] = best[1]["index"]
        else:
            lines.append(f"  Fuzzy Match: NO")
            lines.append(f"  Matched Chat: None")
            lines.append(f"  Similarity: {(best[0] * 100 if best else 0):.0f}%")
            found_index_by_group[group_name] = None
        # --- 4. Chat position ---
        idx = found_index_by_group[group_name]
        lines.append(f"  Visible index: {'#' + str(idx + 1) if idx is not None else 'Not visible'}")

    # --- 5. Latest message per configured group (inspect only) --------
    lines.append("")
    lines.append("-- Latest Message Per Configured Group (inspect only, never processed) --")
    current_group_state: Dict[str, dict] = {}
    for group_name in groups:
        idx = found_index_by_group.get(group_name)
        if idx is None:
            lines.append(f"Group: {group_name!r} -> Not visible, cannot inspect latest message.")
            current_group_state[group_name] = {"visible": False}
            continue
        msg_info: dict = {}
        try:
            # Independent open: click the visible row directly. Does NOT
            # go through sender._open_group_chat / the search box — a
            # completely separate code path, sharing no group-routing logic.
            await page.locator('[data-testid="cell-frame-title"]').nth(idx).click(timeout=5000)
            await asyncio.sleep(0.6)
            msg_info = await page.evaluate("""() => {
                const nodes = document.querySelectorAll('[data-testid^="conv-msg-"]');
                if (!nodes.length) return null;
                const el = nodes[nodes.length - 1];
                const testid = el.getAttribute('data-testid');
                const pp = el.querySelector('[data-pre-plain-text]');
                const prePlain = pp ? pp.getAttribute('data-pre-plain-text') : null;
                const text = (el.textContent || '').trim().slice(0, 200);
                return {message_id: testid, pre_plain_text: prePlain, text_preview: text};
            }""") or {}
        except Exception as exc:
            msg_info = {"error": str(exc)}
        pre_plain = msg_info.get("pre_plain_text")
        m = _PRE_PLAIN_NAME_RE.match(pre_plain) if pre_plain else None
        sender_name = m.group(1).strip() if m else None
        lines.append(f"Group: {group_name!r}")
        lines.append(f"  Latest message id: {msg_info.get('message_id')}")
        lines.append(f"  Latest timestamp/sender-line (raw): {pre_plain!r}")
        lines.append(f"  Latest sender (parsed): {sender_name}")
        lines.append(f"  Latest text preview: {msg_info.get('text_preview')!r}")
        row = rows[idx] if idx < len(rows) else {}
        lines.append(f"  Latest unread state (from list row before opening): {row.get('unread_label') or 'None'}")
        lines.append(f"  Latest DOM node id: {msg_info.get('message_id')}")
        current_group_state[group_name] = {
            "visible": True, "message_id": msg_info.get("message_id"),
            "pre_plain_text": pre_plain, "unread_label": row.get("unread_label"),
        }

    # --- 6. Change vs previous cycle -----------------------------------
    lines.append("")
    lines.append("-- Change Since Previous Cycle --")
    for group_name in groups:
        prev = _prev_verify_group_state.get(group_name) or {}
        cur = current_group_state.get(group_name) or {}
        new_msg = bool(cur.get("message_id")) and cur.get("message_id") != prev.get("message_id")
        dom_changed = cur != prev
        unread_changed = cur.get("unread_label") != prev.get("unread_label")
        ts_changed = cur.get("pre_plain_text") != prev.get("pre_plain_text")
        lines.append(f"Group: {group_name!r}")
        lines.append(f"  New message detected: {'YES' if new_msg else 'NO'}")
        lines.append(f"  DOM changed: {'YES' if dom_changed else 'NO'}")
        lines.append(f"  Unread changed: {'YES' if unread_changed else 'NO'}")
        lines.append(f"  Timestamp changed: {'YES' if ts_changed else 'NO'}")
    _prev_verify_group_state.clear()
    _prev_verify_group_state.update(current_group_state)

    lines.append("========== END VERIFICATION ==========")
    logger.info("\n".join(lines))


async def inbound_listener_loop(session) -> None:
    """Spawned once as an asyncio task from worker.py, alongside the
    existing heartbeat_task, right after session.start(). Runs for the
    lifetime of the process; worker.py cancels it in its shutdown finally
    block exactly like heartbeat_task.

    Startup/readiness verification (2026-08-11): rather than a one-shot
    check run once at boot, `worker_ready` is a LIVE value recomputed every
    poll cycle from the current sub-statuses (session healthy, dispatcher
    reachable, reply pipeline structurally available) — self-correcting by
    construction, so it can never get stuck reporting healthy after
    something actually breaks, and never needs a separate retry-with-
    backoff mechanism: the sub-statuses it's built from are already
    refreshed every cycle by the existing poll loop."""
    global _state_rebuilt_at, _last_live_verify_at
    if not config.INBOUND_LISTENER_ENABLED:
        logger.info("inbound: listener disabled via WA_INBOUND_LISTENER_ENABLED — outbound sending unaffected")
        return

    await _ensure_indexes()
    # DB is authoritative: seed the cache so groups flagged by a previous
    # process are revalidated once here, and recover with no manual cleanup.
    await load_invalid_state()
    # First boot gets the same grace window a reconnect does — a freshly
    # launched, persisted-profile browser's chat list can race the exact
    # same way a re-authentication's does.
    _state_rebuilt_at = time.monotonic()
    await _update_worker_status(listener_status="starting", worker_ready=False)

    async with httpx.AsyncClient() as http:
        groups_cache = KnownGroupsCache(http)
        participants_cache = GroupParticipantsCache()
        logger.info(
            "inbound: listener started (poll_sec=%d, groups_refresh_sec=%d, reauth_grace_sec=%d)",
            config.INBOUND_POLL_SEC, config.INBOUND_GROUPS_REFRESH_SEC, config.REAUTH_GRACE_SEC,
        )
        # "Listener attached" / "incoming events subscribed" — true from here
        # on: this loop IS the listener, and it's about to start polling.
        await _update_worker_status(listener_status="active")
        while True:
            reply_pipeline_status = "not_ready"
            try:
                if session.is_healthy:
                    await poll_once(session, http, groups_cache, participants_cache)
                    reply_pipeline_status = "ready" if session.page is not None else "not_ready"
                    # TEMPORARY (2026-08-11) — separate from and does not
                    # affect poll_once/detection/seen-cache above; own slow
                    # cadence so it doesn't add per-2s-cycle overhead.
                    now = time.monotonic()
                    if now - _last_live_verify_at > LIVE_SESSION_VERIFY_SEC:
                        _last_live_verify_at = now
                        try:
                            await _deep_whatsapp_verification(session, await groups_cache.get())
                        except Exception:
                            logger.exception("inbound: LIVE-SESSION-VERIFY failed — will retry next cadence")
                else:
                    logger.info("inbound: session unhealthy — skipping this poll cycle")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("inbound: unexpected error in poll cycle — will retry next cycle")
            dispatcher_status = _last_written_status.get("dispatcher_status", "unknown")
            worker_ready = (
                session.is_healthy
                and reply_pipeline_status == "ready"
                and dispatcher_status == "ready"
            )
            await _update_worker_status(
                reply_pipeline_status=reply_pipeline_status,
                worker_ready=worker_ready,
            )
            await asyncio.sleep(config.INBOUND_POLL_SEC)
