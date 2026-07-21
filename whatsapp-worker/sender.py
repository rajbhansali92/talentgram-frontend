"""
WhatsApp Worker — Sender Engine
Handles browser actions for locating a chat (personal or group) and sending messages with optional media attachments.
"""
from __future__ import annotations

import asyncio
import json as _p26b_json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

import config
from db import get_db
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from modals import dismiss_blocking_dialogs
from session import SEL, get_recent_console_errors, get_recent_network_errors

logger = logging.getLogger(__name__)


# ==========================================================================
# PHASE 26B — TEMPORARY chat-discovery investigation instrumentation.
# Every function/call in this block is additive logging, screenshots, and
# DOM capture ONLY — none of it changes a selector, timeout, retry count, or
# control-flow branch anywhere in this file. Call sites are tagged
# "# PHASE26B" so this can be stripped cleanly once the investigation is
# closed. Log lines are prefixed "PHASE26B " for easy grep.
# ==========================================================================

_P26B_DUMP_JS = """
() => {
    const grab = (sel) => { const el = document.querySelector(sel);
        return el ? el.outerHTML.slice(0, 6000) : null; };
    const active = document.activeElement;
    const spinnerText = (document.body.innerText || '').includes('Starting chat');
    return {
        url: window.location.href,
        title: document.title,
        active_element: active ? {
            tag: active.tagName,
            id: active.id || null,
            testid: active.getAttribute ? active.getAttribute('data-testid') : null,
            role: active.getAttribute ? active.getAttribute('role') : null,
        } : null,
        conversation_pane_exists: !!document.querySelector('#main'),
        composer_exists: !!document.querySelector('[data-testid="conversation-compose-box-input"]'),
        loading_spinner_exists: !!(
            document.querySelector('[data-testid="spinner"]')
            || document.querySelector('[role="progressbar"]')
            || spinnerText
        ),
        search_container_html: grab('#side'),
        conversation_container_html: grab('#main'),
        composer_html: grab('[data-testid="conversation-compose-box-input"]') || grab('footer'),
        search_results_html: grab('#pane-side'),
        body_text_excerpt: (document.body.innerText || '').slice(0, 500),
    };
}
"""


async def _p26b_dump(page: Page, stage: str, extra: Optional[dict] = None) -> dict:
    """PHASE26B: one structured checkpoint — URL, title, activeElement,
    conversation-pane/composer/spinner presence, truncated outerHTML of the
    search container / conversation container / composer / search-results
    list, a screenshot, and recent console/network errors. Logs a single
    JSON line so every stage is greppable as `PHASE26B stage=<name>`."""
    shot_path = f"/tmp/p26b_{stage}.png"
    await _safe_screenshot(page, shot_path)
    try:
        state = await page.evaluate(_P26B_DUMP_JS)
    except Exception as exc:
        state = {"error": str(exc)}
    state["stage"] = stage
    state["screenshot"] = shot_path
    if extra:
        state["extra"] = extra
    try:
        state["recent_console_errors"] = get_recent_console_errors()
        state["recent_network_errors"] = get_recent_network_errors()
    except Exception as exc:
        state["console_network_error"] = str(exc)
    logger.info("PHASE26B stage=%s snapshot=%s", stage,
                _p26b_json.dumps(state, ensure_ascii=False)[:6000])
    return state


async def _p26b_search_evidence(page: Page, stage: str, search_sel: Optional[str],
                                  search_term: str) -> dict:
    """PHASE26B: search-analysis checkpoint — search term entered, the value
    actually present in the search box, DOM result count vs visible result
    count, text of every visible result, and (via _p26b_dump) a screenshot +
    the search-results container HTML. If zero results, records why."""
    box_value = None
    if search_sel:
        try:
            box_value = await page.locator(search_sel).first.evaluate(
                "el => (el.value != null ? el.value : (el.innerText || ''))",
                timeout=2_000,
            )
        except Exception as exc:
            box_value = f"<unreadable:{exc}>"
    dom_count = 0
    visible_results = []
    matched_selector = None
    for sel in RESULT_TITLE_SELECTORS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
        except Exception:
            continue
        if not n:
            continue
        matched_selector = sel
        dom_count = n
        for i in range(min(n, 30)):
            try:
                if await loc.nth(i).is_visible():
                    visible_results.append((await loc.nth(i).inner_text()).strip()[:80])
            except Exception:
                pass
        break
    evidence = {
        "search_term_entered": search_term,
        "search_box_value": box_value,
        "result_selector_matched": matched_selector,
        "dom_result_count": dom_count,
        "visible_result_count": len(visible_results),
        "visible_result_texts": visible_results,
    }
    if dom_count == 0:
        evidence["zero_results_reason"] = (
            "no element matched any RESULT_TITLE_SELECTORS ("
            + ", ".join(RESULT_TITLE_SELECTORS)
            + ") — WhatsApp's own sidebar search returned nothing for this query"
        )
    await _p26b_dump(page, stage, extra=evidence)
    logger.info("PHASE26B SEARCH_ANALYSIS stage=%s %s", stage,
                _p26b_json.dumps(evidence, ensure_ascii=False)[:4000])
    return evidence

# Resilient send-button fallback chain. WhatsApp Web rotates data-testid/class
# names, so we try several signals in order and log which one matched. The
# data-icon="send" span has been the most stable signal historically.
SEND_BUTTON_SELECTORS = [
    "[data-testid='send']",                  # legacy (the selector that just went stale)
    "button[aria-label='Send']",
    "button[aria-label*='Send']",
    "footer button[aria-label*='Send']",
    "button:has(span[data-icon='send'])",
    "span[data-icon='send']",
    "[data-icon='send']",
]


async def _find_and_click_send(page: Page) -> str:
    """Locate and click the WhatsApp send button via the fallback chain.

    Returns the selector that worked (or 'keyboard:Enter'). Logs every probe so
    the live DOM tells us which selector is currently valid — no guessing.
    """
    for sel in SEND_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count == 0:
                logger.info("sender: send probe %-42s count=0", sel)
                continue
            visible = await loc.first.is_visible()
            logger.info("sender: send probe %-42s count=%d visible=%s", sel, count, visible)
            if visible:
                await loc.first.click(timeout=5_000)
                logger.info("sender: ✅ send button CLICKED via %s", sel)
                return sel
        except Exception as exc:
            logger.info("sender: send probe %-42s error=%s", sel, exc)

    # Resilient last resort: Enter sends a focused text message / media preview.
    logger.warning("sender: no send button matched any selector — falling back to Enter key")
    await page.keyboard.press("Enter")
    logger.info("sender: ✅ send executed via keyboard Enter fallback")
    return "keyboard:Enter"


async def _dump_send_dom(page: Page) -> None:
    """Instrumentation: log the live url/title and every visible candidate
    element (data-testid / aria-label / role=button / button / data-icon) so the
    real send-button selector is visible in the worker logs."""
    try:
        logger.info("sender: [chat] url=%s title=%r", page.url, await page.title())
    except Exception as exc:
        logger.info("sender: [chat] url/title error=%s", exc)
    try:
        elements = await page.evaluate(
            """() => {
                const pick = el => ({
                    tag: el.tagName.toLowerCase(),
                    testid: el.getAttribute('data-testid'),
                    aria: el.getAttribute('aria-label'),
                    role: el.getAttribute('role'),
                    icon: el.getAttribute('data-icon'),
                    text: (el.innerText || '').trim().slice(0, 24),
                });
                const q = '[data-testid],[aria-label],[role="button"],button,[data-icon]';
                return Array.from(document.querySelectorAll(q))
                    .filter(e => e.offsetParent !== null)
                    .slice(0, 80)
                    .map(pick);
            }"""
        )
        logger.info("sender: DOM dump — %d visible candidate elements:", len(elements))
        for e in elements:
            logger.info("sender:   %s", e)
    except Exception as exc:
        logger.info("sender: DOM dump failed: %s", exc)


async def _safe_screenshot(page: Page, path: str) -> None:
    try:
        await page.screenshot(path=path)
        logger.info("sender: screenshot saved %s", path)
    except Exception as exc:
        logger.info("sender: screenshot %s failed: %s", path, exc)


# Chat-ready / delivery-verification constants.
CHAT_READY_TIMEOUT_MS = 60_000
# "Starting chat" loading modal/spinner shown by the /send?phone= deep link.
STARTING_CHAT_SELECTORS = [
    "text=Starting chat",
    "div[title='Starting chat']",
    "[data-testid='spinner']",
    "[role='progressbar']",
]


# =========================================================================
# SELECTOR REGISTRY — current WhatsApp DOM, primary + fallback.
# Verification prefers data-testid / aria-label / role attributes and is scoped
# to the ACTIVE conversation. The banned legacy selectors (.message-out,
# .copyable-text, div[data-id^='true_']) are intentionally NOT used.
# =========================================================================
SELECTOR_REGISTRY = {
    # Active-conversation container — scope for every message lookup.
    # conversation-panel-messages / -body are the real production containers
    # (confirmed from a live DOM snapshot); the rest are kept as extra layers.
    "active_conversation": {
        "primary": [
            "[data-testid='conversation-panel-messages']",
            "[data-testid='conversation-panel-body']",
            "[data-testid='conversation-panel-wrapper']",
            "[aria-label='Message list']",
            "div[role='application']",
        ],
        "fallback": ["#main"],
    },
    "conversation_header": {
        "primary": [
            "[data-testid='conversation-header']",
            "header[role='banner']",
        ],
        "fallback": ["#main header"],
    },
    # Individual message elements inside the active conversation.
    # conv-msg-* is the real production message testid (each bubble is
    # data-testid="conv-msg-<id>"); msg-container etc. kept as extra layers.
    "message_element": {
        "primary": [
            "[data-testid^='conv-msg-']",
            "[data-testid*='conv-msg']",
            "[data-testid='msg-container']",
            "div[role='row']",
            "[aria-label][data-testid*='msg']",
        ],
        "fallback": [
            "[data-testid*='message']",
            "div[tabindex='-1'][role]",
        ],
    },
}


def _needle(message_body: str) -> str:
    """Exact first 30 characters of the sent payload — the VERIFIED match key."""
    return (message_body or "")[:30]


def _norm(text: str) -> str:
    """Whitespace-collapsed text, so newline rendering differences still match."""
    return " ".join((text or "").split())


# TASK 4 — distinct send outcomes (never collapsed into a single FAILED).
CHAT_NOT_OPENED = "CHAT_NOT_OPENED"                       # conversation never opened -> retry
MESSAGE_NOT_SENT = "MESSAGE_NOT_SENT"                     # composer still full / send didn't fire -> retry
MESSAGE_SENT_BUT_NOT_VERIFIED = "MESSAGE_SENT_BUT_NOT_VERIFIED"  # left composer, no bubble -> DO NOT retry
MESSAGE_SENT_AND_VERIFIED = "MESSAGE_SENT_AND_VERIFIED"   # outgoing bubble confirmed -> SENT
INVALID_DESTINATION = "INVALID_DESTINATION"              # bad number / group missing -> terminal

# Conversation-open signals. #main is the open-chat pane (absent on the home
# screen), so it is a reliable "a chat is actually open" signal — unlike the
# compose box, which can exist on the home screen.
CONV_PANEL_SELECTORS = ["#main", "div#main", "[data-testid='conversation-panel-wrapper']"]
CONV_HEADER_SELECTORS = ["#main header", "header[data-testid='conversation-header']"]
RECIPIENT_SELECTORS = [
    "#main header span[title]",
    "#main header [data-testid='conversation-info-header-chat-title']",
    "#main header [title]",
]


def _first_line(message_body: str) -> str:
    for ln in (message_body or "").splitlines():
        if ln.strip():
            return ln.strip()[:40]
    return ""


async def _first_present(page: Page, selectors: list) -> Tuple[bool, Optional[str]]:
    for sel in selectors:
        try:
            if await page.locator(sel).count() and await page.locator(sel).first.is_visible():
                return True, sel
        except Exception:
            pass
    return False, None


def _norm_title(text: str) -> str:
    # Same deterministic key as group resolution (NFKC + whitespace + casefold)
    # so the post-open wrong-chat guard agrees with how the group was matched.
    return _norm_group(text)


async def _verify_chat_open(page: Page, expected_name: Optional[str] = None) -> Tuple[bool, bool, bool, str]:
    """TASK 1: prove a real conversation is open BEFORE typing.

    Requires the conversation panel (#main), a header inside it, and a visible
    recipient name/phone. The compose box alone is NOT accepted (it can exist on
    the home screen). When expected_name is given (group sends), the header
    title must also MATCH it — never type into the wrong chat.
    Returns (ready, header_found, recipient_found, recipient_text).
    """
    panel_found, panel_sel = await _first_present(page, CONV_PANEL_SELECTORS)
    header_found, _ = await _first_present(page, CONV_HEADER_SELECTORS)

    # Collect ALL header title candidates — group-chat headers contain several
    # span[title] elements (group name + "click here for group info" subtitle),
    # and the name is not guaranteed to be first.
    candidates = []
    for sel in RECIPIENT_SELECTORS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(min(n, 6)):
                item = loc.nth(i)
                t = (await item.inner_text()).strip() or \
                    (await item.get_attribute("title") or "").strip()
                if t and t not in candidates:
                    candidates.append(t)
        except Exception:
            pass
    recipient_found = bool(candidates)
    recipient_text = candidates[0] if candidates else ""

    conversation_ready = bool(panel_found and header_found)
    title_match = None
    if expected_name and candidates:
        title_match = any(_norm_title(t) == _norm_title(expected_name) for t in candidates)
        if title_match:
            recipient_text = next(t for t in candidates
                                  if _norm_title(t) == _norm_title(expected_name))
        else:
            conversation_ready = False
    logger.info("sender: CHAT OPEN VERIFICATION")
    logger.info("sender:   panel_found=%s (%s)", panel_found, panel_sel)
    logger.info("sender:   header_found=%s", header_found)
    logger.info("sender:   recipient_found=%s", recipient_found)
    logger.info("sender:   recipient_text=%r (candidates=%s)", recipient_text[:60],
                [c[:40] for c in candidates[:6]])
    if expected_name:
        logger.info("sender:   expected_name=%r title_match=%s", expected_name[:60], title_match)
    logger.info("sender:   conversation_ready=%s", conversation_ready)
    return conversation_ready, header_found, recipient_found, recipient_text


async def _dump_outgoing_dom(page: Page) -> None:
    """TASK 2 instrumentation: enumerate live message-like elements (data-id,
    class, data-testid) inside the thread so the real outgoing selector is
    captured in the worker logs — no guessing."""
    try:
        rows = await page.evaluate(
            """() => {
                const out = [];
                const nodes = document.querySelectorAll(
                    '#main [data-id], #main div[class*="message"], #main [data-testid]'
                );
                nodes.forEach(el => {
                    const did = el.getAttribute('data-id');
                    const cls = (el.getAttribute('class') || '').slice(0, 60);
                    const tid = el.getAttribute('data-testid');
                    if (did || tid || /message/.test(cls)) {
                        out.push({ data_id: did, testid: tid, cls,
                                   text: (el.innerText || '').trim().slice(0, 30) });
                    }
                });
                return out.slice(-25);  // most recent 25
            }"""
        )
        logger.info("sender: OUTGOING DOM DUMP — %d message-like elements (most recent):", len(rows))
        for r in rows:
            logger.info("sender:   %s", r)
    except Exception as exc:
        logger.info("sender: OUTGOING DOM DUMP failed: %s", exc)


async def _modal_visible(page: Page) -> Optional[str]:
    for sel in STARTING_CHAT_SELECTORS:
        try:
            loc = page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                return sel
        except Exception:
            pass
    return None


async def _wait_for_chat_ready(page: Page) -> None:
    """PROBLEM #1: do not type until the conversation is genuinely open.

    Waits for: (1) the 'Starting chat' modal/spinner to clear, (2) the compose
    box to be visible AND editable, (3) the footer composer to be attached.
    Fails loudly if the modal never clears or the composer never becomes
    interactive — the caller must NOT proceed to type.
    """
    deadline = time.monotonic() + CHAT_READY_TIMEOUT_MS / 1000

    modal = await _modal_visible(page)
    if modal:
        logger.info("sender: detected starting-chat modal via %s", modal)
        logger.info("sender: waiting for modal to disappear...")
        while time.monotonic() < deadline:
            if not await _modal_visible(page):
                logger.info("sender: modal disappeared")
                break
            await asyncio.sleep(0.5)
        else:
            await _safe_screenshot(page, "/tmp/chat_not_ready.png")
            raise RuntimeError("starting-chat modal never cleared — chat not ready")

    # Compose box visible + editable.
    box_ready = False
    while time.monotonic() < deadline:
        try:
            loc = page.locator(SEL["msg_box"]).first
            if await loc.count() and await loc.is_visible():
                editable = (await loc.get_attribute("contenteditable")) == "true"
                if editable:
                    box_ready = True
                    break
        except Exception:
            pass
        await asyncio.sleep(0.3)
    if not box_ready:
        await _safe_screenshot(page, "/tmp/chat_not_ready.png")
        raise RuntimeError("compose box not interactive — chat not ready")
    logger.info("sender: compose box verified (visible + editable)")

    try:
        footer_n = await page.locator("footer").count()
        logger.info("sender: footer composer attached? %s", footer_n > 0)
    except Exception:
        pass
    logger.info("sender: conversation ready")


async def _resolve_scope(page: Page) -> str:
    """Resolve the active-conversation container selector (registry order)."""
    reg = SELECTOR_REGISTRY["active_conversation"]
    for tier in ("primary", "fallback"):
        for sel in reg[tier]:
            try:
                if await page.locator(sel).count():
                    return sel
            except Exception:
                pass
    return "#main"


async def _msg_timestamp(page: Page, full_selector: str) -> str:
    """Timestamp of the matched message (data-pre-plain-text), best-effort."""
    try:
        return await page.evaluate(
            """(sel) => {
                const els = document.querySelectorAll(sel);
                if (!els.length) return '';
                const el = els[els.length - 1];
                const cp = el.querySelector('[data-pre-plain-text]')
                          || (el.matches && el.matches('[data-pre-plain-text]') ? el : null);
                return cp ? (cp.getAttribute('data-pre-plain-text') || '') : '';
            }""",
            full_selector,
        )
    except Exception:
        return ""


async def _is_outgoing_msg(
    page: Page, css_selector: str, index: int, self_display_name: Optional[str] = None
) -> Optional[bool]:
    """Best-effort directional check on a matched message element.
    Returns True (outgoing), False (incoming), or None (can't determine).
    Uses multiple heuristics and walks up to 6 ancestors. None means the DOM
    changed and we can't tell; callers should accept-with-warning rather than
    reject (except the inbound listener, which fails closed on None).

    Live testing (2026-07-21) against a current WhatsApp Web build found the
    old class-name/data-id-prefix heuristics no longer match anything — that
    build renders every message row with the same generic atomic CSS classes
    regardless of direction, and data-id no longer carries a true_/false_
    prefix. Two markers did survive the rewrite: the bubble's tail element
    (data-icon="tail-out"/"tail-in") and an accessibility label WhatsApp
    stamps on every one of the account's own messages (aria-label="You:").
    Both, however, are only rendered on the FIRST bubble of a consecutive run
    from the same sender — two messages sent back-to-back by the same person
    (human or the worker itself), with no reply from the other side in
    between, render with neither marker on the second one. The
    data-pre-plain-text attribute (already used elsewhere to extract the
    sender's display name) does NOT depend on grouping — it's present on
    every message — so when self_display_name is supplied and neither marker
    above resolved anything, the parsed name is compared against it as a
    final fallback before giving up and returning None."""
    try:
        result = await page.evaluate("""([sel, idx]) => {
            const els = document.querySelectorAll(sel);
            if (idx >= els.length) return null;
            const el = els[idx];
            if (el.querySelector('span[aria-label="You:"]')) return {dir: true};
            if (el.querySelector('[data-icon="tail-out"], [data-testid="tail-out"]')) return {dir: true};
            if (el.querySelector('[data-icon="tail-in"], [data-testid="tail-in"]')) return {dir: false};
            let node = el;
            for (let i = 0; i < 6 && node && node !== document; i++) {
                const cls = typeof node.className === 'string' ? node.className : '';
                if (cls.includes('message-out')) return {dir: true};
                if (cls.includes('message-in')) return {dir: false};
                const did = node.getAttribute ? node.getAttribute('data-id') : null;
                if (did) {
                    if (did.startsWith('true_')) return {dir: true};
                    if (did.startsWith('false_')) return {dir: false};
                }
                node = node.parentElement;
            }
            const checks = el.querySelectorAll(
                '[data-icon="msg-check"], [data-icon="msg-dblcheck"],'
                + '[data-testid="msg-check"], [data-testid="msg-dblcheck"]'
            );
            if (checks.length > 0) return {dir: true};
            const pp = el.querySelector('[data-pre-plain-text]');
            return {dir: null, prePlainText: pp ? pp.getAttribute('data-pre-plain-text') : null};
        }""", [css_selector, index])
    except Exception as exc:
        logger.info("sender: direction check error: %s", exc)
        return None

    if result is None:
        return None
    if result.get("dir") is not None:
        return result["dir"]
    if self_display_name:
        pre_plain = result.get("prePlainText")
        m = re.match(r"^\[[^\]]*\]\s*(.+?):\s*$", pre_plain) if pre_plain else None
        if m:
            return m.group(1).strip().lower() == self_display_name.strip().lower()
    return None


async def _snapshot_msg_baselines(page: Page) -> dict:
    """Snapshot message element counts for every registry selector BEFORE typing.
    Verification uses these baselines to only check NEW messages that appeared
    after the send action, preventing false positives from old campaign messages."""
    scope = await _resolve_scope(page)
    chain = (SELECTOR_REGISTRY["message_element"]["primary"]
             + SELECTOR_REGISTRY["message_element"]["fallback"])
    baselines = {}
    for sel in chain:
        full = f"{scope} {sel}"
        try:
            baselines[full] = await page.locator(full).count()
        except Exception:
            baselines[full] = 0
    logger.info("sender: baseline snapshot: %s", baselines)
    return baselines


async def _find_outgoing_with_text(page: Page, needle: str, baselines: Optional[dict] = None) -> Tuple[Optional[str], str]:
    """Find a message element in the ACTIVE conversation whose text contains the
    exact first-30-char needle. When baselines is provided, only checks elements
    that appeared AFTER the baseline snapshot (new messages only)."""
    scope = await _resolve_scope(page)
    chain = (SELECTOR_REGISTRY["message_element"]["primary"]
             + SELECTOR_REGISTRY["message_element"]["fallback"])
    logger.info("sender: verify — SELECTOR CHAIN scope=%r message_element=%s baselines=%s",
                scope, chain, "provided" if baselines else "none")
    norm_needle = _norm(needle)
    for sel in chain:
        full = f"{scope} {sel}"
        try:
            loc = page.locator(full)
            n = await loc.count()
        except Exception as exc:
            logger.info("sender: verify — chain[%s] error=%s", full, exc)
            continue
        logger.info("sender: verify — chain[%s] count=%d", full, n)
        if not n:
            continue
        if baselines is not None and full in baselines:
            start = baselines[full]
        else:
            start = max(0, n - 8)
        logger.info("sender: verify — chain[%s] checking elements [%d..%d)", full, start, n)
        for i in range(start, n):
            try:
                t = (await loc.nth(i).inner_text()).strip()
            except Exception:
                continue
            if needle and (needle in t or (norm_needle and norm_needle in _norm(t))):
                direction = await _is_outgoing_msg(
                    page, full, i, self_display_name=config.WA_SELF_DISPLAY_NAME
                )
                if direction is False:
                    logger.info("sender: verify — text matched at element#%d but message is "
                                "INCOMING — skipping", i)
                    continue
                if direction is None:
                    logger.warning("sender: verify — text matched at element#%d but direction "
                                   "UNKNOWN — accepting (DOM may have changed)", i)
                logger.info("sender: verify — ✅ MATCHED layer=%r element#%d text[:60]=%r "
                            "outgoing=%s", full, i, t[:60], direction)
                return full, t

    return None, ""


async def _dump_conv_msgs(page: Page) -> None:
    """TASK 2: dump the first 20 conv-msg-* nodes and their text from the live
    conversation so the real message DOM is always visible in the logs."""
    try:
        rows = await page.evaluate(
            """() => {
                const nodes = document.querySelectorAll("[data-testid^='conv-msg-']");
                return Array.from(nodes).slice(0, 20).map(el => ({
                    testid: el.getAttribute('data-testid'),
                    cls: (el.getAttribute('class') || '').slice(0, 40),
                    text: (el.innerText || '').trim().slice(0, 50),
                }));
            }"""
        )
        logger.info("sender: CONV-MSG DUMP — %d conv-msg-* nodes (first 20):", len(rows))
        for r in rows:
            logger.info("sender:   %s", r)
    except Exception as exc:
        logger.info("sender: CONV-MSG DUMP failed: %s", exc)


async def _selector_health(page: Page) -> dict:
    """Count resolution for every registry selector (DOM health)."""
    report = {}
    for tiers in SELECTOR_REGISTRY.values():
        for sels in tiers.values():
            for sel in sels:
                try:
                    report[sel] = await page.locator(sel).count()
                except Exception as exc:
                    report[sel] = f"err:{exc}"
    return report


async def dom_health_check(page: Page, label: str = "startup") -> dict:
    """Startup/diagnostic DOM health check — logs which registry selectors
    resolve against the live DOM (no guessing about what WhatsApp uses today)."""
    logger.info("sender: DOM HEALTH CHECK (%s) — registry selector resolution:", label)
    report = await _selector_health(page)
    for sel, cnt in report.items():
        logger.info("sender:   [%s] %-55s -> %s", label, sel, cnt)
    return report


async def _store_dom_snapshot(page: Page, reason: str, extra: Optional[dict] = None) -> None:
    """Persist a DOM snapshot (screenshot + #main HTML excerpt + selector health)
    to Mongo whenever verification fails, for offline selector forensics."""
    shot = f"/tmp/dom_snapshot_{reason}.png"
    await _safe_screenshot(page, shot)
    html = ""
    try:
        html = await page.evaluate(
            "() => { const m = document.querySelector('#main');"
            " return (m ? m.outerHTML : document.body.innerHTML).slice(0, 40000); }"
        )
    except Exception as exc:
        logger.info("sender: snapshot html error=%s", exc)
    health = await _selector_health(page)
    doc = {
        "id": str(uuid.uuid4()),
        "reason": reason,
        "url": page.url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "html_excerpt": html,
        "selector_health": {str(k): v for k, v in health.items()},
        "extra": extra or {},
    }
    try:
        await get_db().whatsapp_dom_snapshots.insert_one(doc)
        logger.info("sender: stored DOM snapshot (reason=%s) -> whatsapp_dom_snapshots + %s", reason, shot)
    except Exception as exc:
        logger.info("sender: snapshot store error=%s (screenshot still at %s)", exc, shot)


async def _verify_delivery(page: Page, message_body: str, baselines: Optional[dict] = None) -> Tuple[bool, bool, Optional[str]]:
    """VERIFIED iff a NEW message element (appeared after baselines snapshot)
    contains the exact first 30 characters of the sent payload. A cleared compose
    box is NOT proof (logged for diagnosis only). On failure, a DOM snapshot is
    stored. Returns (verified, composer_cleared, matched_selector)."""
    composer_cleared = False
    try:
        txt = (await page.locator(SEL["msg_box"]).first.inner_text()).strip()
        composer_cleared = (txt == "")
        logger.info("sender: verify — compose box after send=%r (cleared=%s) [DIAGNOSTIC ONLY]",
                    txt[:40], composer_cleared)
    except Exception as exc:
        logger.info("sender: verify — compose read error=%s", exc)

    # TASK 2: always dump the live conv-msg-* nodes before matching.
    await _dump_conv_msgs(page)

    needle = _needle(message_body)
    matched_sel, matched_text = await _find_outgoing_with_text(page, needle, baselines=baselines)
    verified = matched_sel is not None

    if verified:
        ts = await _msg_timestamp(page, matched_sel)
        logger.info("sender: verify — VERIFIED via chain=%r | text[:80]=%r | timestamp=%r",
                    matched_sel, matched_text[:80], ts)
    else:
        logger.warning("sender: verify — NOT VERIFIED: no message element contains the first-30-char "
                       "needle=%r — storing DOM snapshot", needle)
        await _store_dom_snapshot(page, "verify_failed", {"needle": needle})

    return verified, composer_cleared, matched_sel


async def _already_delivered(page: Page, message_body: str, is_retry: bool = False) -> bool:
    """Duplicate guard: ONLY active on retries (attempt_count > 0). On first
    attempt, never skip — old messages from prior campaigns must not prevent
    delivery. On retry, checks last 8 messages for the prior attempt's bubble."""
    if not is_retry:
        return False
    needle = _needle(message_body)
    if not needle:
        return False
    matched_sel, _ = await _find_outgoing_with_text(page, needle)
    return matched_sel is not None


# ==========================================================================
# GROUP RESOLUTION — sidebar-scoped, focus-verified, deterministically matched.
#
# BACKGROUND (Railway log 2026-07-07, batch f324ff84): once a chat was open,
# the sidebar-specific search selectors returned count=0 and the previous chain
# fell through to a page-global 'div[role="textbox"][contenteditable="true"]'
# fallback. That generic selector could resolve to unintended editable elements
# (such as the #main conversation composer), after which the group query never
# reached the sidebar search and the exact-title match timed out -> repeated
# NOT_FOUND -> circuit breaker. The separate exact case-sensitive title match
# also rejected titles differing only in case/whitespace/Unicode form.
#
# FIX: every search-box selector is structurally scoped under #side so it
# cannot resolve outside the sidebar; focus is verified to be inside the
# sidebar before typing; the typed value is read back; and results are matched
# by deterministic normalized (NFKC + casefold + whitespace) equality — no
# fuzzy matching. Diagnostics below log every attempt so future failures are
# provable from the logs rather than inferred.
# ==========================================================================

# Sidebar container. #side is the left column (chat list + search); #main is the
# open conversation. Scoping to #side structurally excludes the composer.
SIDEBAR_SCOPE = "#side"

# Search-box selectors — ALL scoped under #side so the conversation composer in
# #main can never be selected. Ordered most-specific first.
SEARCH_BOX_SELECTORS = [
    # WhatsApp Web migrated the sidebar search box from a contenteditable div to
    # a native <input data-tab="3">; keep this first since it's the current DOM.
    '#side input[data-tab="3"]',
    '#side div[contenteditable="true"][data-tab="3"]',
    '#side [aria-label="Search input textbox"]',
    '#side [aria-label="Search or start a new chat"]',
    '#side div[role="textbox"][contenteditable="true"]',
    '#side [data-testid="chat-list-search"]',
]

# Search-result title cells inside the sidebar results pane.
RESULT_TITLE_SELECTORS = [
    '#pane-side [data-testid="cell-frame-title"]',
    '#pane-side span[title]',
    '#side [data-testid="cell-frame-title"]',
]

# Clicking the conversation header opens the Group Info drawer. Ordered
# most-specific first, same convention as SEARCH_BOX_SELECTORS.
GROUP_INFO_TRIGGER_SELECTORS = [
    '#main header[data-testid="conversation-header"]',
    '#main header',
]

# The Group Info drawer itself, once opened.
GROUP_INFO_PANEL_SELECTORS = [
    '[data-testid="drawer-right"]',
    'div[role="complementary"]',
]



async def get_group_participants(page: Page, group_name: str) -> Optional[list]:
    """Open the Group Info drawer for the ALREADY-OPEN conversation and scrape
    every participant row's visible text (plus any phone-number pattern found
    in it). Used only by the inbound listener's group_members security mode
    (2026-07-21) to determine, at message-processing time, whether a sender
    is a CURRENT group participant — so a removed member loses access
    immediately rather than staying allowed forever via a stale allowlist.

    Returns None if the panel can't be opened or read at all (DOM drifted,
    a dialog is blocking, etc.) — callers MUST treat None as "couldn't
    determine" and fail closed, never guess membership. Always closes the
    drawer (Escape) before returning, whether it succeeded or not, so the
    conversation is left in the same state it was found in.

    Live testing (2026-07-21) found that scoping to specific row-container
    selectors (e.g. [data-testid="cell-frame-container"]) is unreliable —
    WhatsApp renders action rows like "Invite to group via link" through the
    exact same generic list-cell component as real participant rows, so a
    structural selector alone can't tell them apart. Instead this reads the
    ENTIRE drawer's rendered text and splits it into lines: matching only
    needs substring containment of a sender's name, so extra noise lines
    (headings, buttons) are harmless, while missing a real participant due
    to an overly-narrow selector would not be."""
    header_sel = None
    for sel in GROUP_INFO_TRIGGER_SELECTORS:
        try:
            if await page.locator(sel).count():
                header_sel = sel
                break
        except Exception:
            pass
    if not header_sel:
        logger.warning("sender: group-info: no conversation header found for %r", group_name)
        return None

    try:
        await page.click(header_sel, timeout=5_000)
        await asyncio.sleep(0.6)
    except Exception as exc:
        logger.warning("sender: group-info: header click failed for %r: %s", group_name, exc)
        return None

    panel_sel = None
    for sel in GROUP_INFO_PANEL_SELECTORS:
        try:
            if await page.locator(sel).count():
                panel_sel = sel
                break
        except Exception:
            pass
    if not panel_sel:
        logger.warning(
            "sender: group-info: panel did not open for %r (header clicked, no drawer found)",
            group_name,
        )
        await _capture_open_failure(page, "group_info_panel_absent", {"group": group_name})
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return None

    try:
        rows = await page.evaluate(
            """(panelSel) => {
                const panel = document.querySelector(panelSel);
                if (!panel) return [];
                const seen = new Set();
                const out = [];
                (panel.innerText || '').split('\\n').forEach(line => {
                    const t = line.trim();
                    if (t && t.length < 200 && !seen.has(t)) {
                        seen.add(t);
                        out.push(t);
                    }
                });
                return out;
            }""",
            panel_sel,
        )
    except Exception:
        logger.exception("sender: group-info: failed reading participant rows for %r", group_name)
        rows = None
    finally:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    if rows is None:
        return None
    if not rows:
        logger.warning(
            "sender: group-info: drawer opened but no participant rows matched for %r "
            "(panel_selector=%r) — selectors likely need updating",
            group_name, panel_sel,
        )
        return None

    phone_re = re.compile(r"(\+?\d[\d\s\-]{6,17}\d)")
    participants = []
    for raw in rows:
        m = phone_re.search(raw)
        phone = "".join(ch for ch in m.group(1) if ch.isdigit()) if m else None
        participants.append({"raw_text": raw, "phone": phone})
    logger.info(
        "sender: group-info: read %d participant row(s) for %r via panel=%r rows=%r",
        len(participants), group_name, panel_sel, rows,
    )
    return participants


def _norm_group(text: str) -> str:
    """Deterministic group-name key: Unicode NFKC + whitespace-collapse + casefold.
    This is the ONLY matching key used for group resolution. It is exact-equality
    on normalized strings — NOT fuzzy. It folds case and compatibility characters
    and collapses whitespace, but does not treat distinct characters (e.g. 'x' vs
    the multiplication sign '×') as equal; the diagnostics expose any such
    mismatch so a future decision can be made from evidence, not assumption."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = " ".join(t.split())
    return t.casefold()


async def _focus_report(page: Page) -> dict:
    """Structured report on document.activeElement — records where focus landed
    (#side vs #main) so mistargeted typing is observable in the logs instead of
    being an assumption."""
    try:
        return await page.evaluate(
            """() => {
                const a = document.activeElement;
                if (!a) return {tag: null, in_side: false, in_main: false, path: '<none>'};
                const side = document.querySelector('#side');
                const main = document.querySelector('#main');
                const parts = [];
                let n = a;
                for (let i = 0; i < 6 && n && n !== document; i++) {
                    const tid = n.getAttribute ? n.getAttribute('data-testid') : null;
                    parts.push(n.id ? ('#' + n.id) : (tid ? ('@' + tid) : (n.tagName || '?').toLowerCase()));
                    n = n.parentElement;
                }
                return {
                    tag: a.tagName ? a.tagName.toLowerCase() : null,
                    id: a.id || null,
                    testid: a.getAttribute ? a.getAttribute('data-testid') : null,
                    role: a.getAttribute ? a.getAttribute('role') : null,
                    editable: a.getAttribute ? a.getAttribute('contenteditable') : null,
                    value: (a.value !== undefined ? a.value : null),
                    in_side: !!(side && side.contains(a)),
                    in_main: !!(main && main.contains(a)),
                    path: parts.join(' < '),
                };
            }"""
        )
    except Exception as exc:
        return {"tag": None, "in_side": False, "in_main": False, "path": f"<err:{exc}>"}


async def _read_search_value(page: Page, sel: str) -> Tuple[bool, str]:
    """Read the search box's current text for BOTH a native <input> (.value) and a
    contenteditable (innerText). Uses ONE fast evaluate() with a short timeout so
    it never incurs Playwright's ~30s actionability wait — the previous
    inner_text()/text_content() path stalled ~60s on the current <input>-based
    box and produced a false '<unreadable>' mismatch. Returns (readable, value)."""
    try:
        val = await page.locator(sel).first.evaluate(
            "el => (el.value != null ? el.value : (el.innerText != null ? el.innerText : (el.textContent || '')))",
            timeout=2_000,
        )
        return True, (val or "").strip()
    except Exception as exc:
        logger.info("sender: search value read error (advisory): %s", exc)
        return False, ""


async def _collect_search_candidates(page: Page):
    """Return the visible sidebar result rows as Playwright locators + titles.
    Reads the title attribute (full, untruncated name) with innerText fallback."""
    for sel in RESULT_TITLE_SELECTORS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
        except Exception:
            continue
        if not n:
            continue
        candidates = []
        for i in range(min(n, 30)):
            el = loc.nth(i)
            try:
                raw = (await el.inner_text()).strip()
            except Exception:
                raw = ""
            title_attr = None
            try:
                title_attr = await el.get_attribute("title")
                if not title_attr:
                    child = el.locator("span[title]")
                    if await child.count():
                        title_attr = await child.first.get_attribute("title")
            except Exception:
                pass
            value = (title_attr or raw).strip()
            if value:
                candidates.append({"index": i, "locator": el, "title": value,
                                   "raw": raw, "norm": _norm_group(value)})
        if candidates:
            return sel, candidates
    return None, []


async def _reset_to_chat_list(page: Page) -> None:
    """Return WhatsApp to a neutral chat-list state before every search so we are
    never operating relative to a previously-opened conversation. Escape clears an
    open search / closes an open chat; we then confirm the sidebar is present."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
    except Exception as exc:
        logger.info("sender: reset-to-chat-list escape error=%s", exc)


async def _capture_open_failure(page: Page, reason: str, extra: Optional[dict] = None) -> None:
    """On any group-open / chat-open failure, save chat_not_opened.png and dump
    URL, page title, activeElement, and #side / #main / header / composer HTML to
    the logs + whatsapp_dom_snapshots so the exact live DOM is inspectable
    offline (never guess which selector drifted)."""
    await _safe_screenshot(page, "/tmp/chat_not_opened.png")
    try:
        url, title = page.url, await page.title()
    except Exception as exc:
        url, title = f"<err:{exc}>", ""
    focus = await _focus_report(page)
    try:
        html = await page.evaluate(
            """() => {
                const grab = (sel) => { const el = document.querySelector(sel);
                    return el ? el.outerHTML.slice(0, 12000) : null; };
                return {
                    side: grab('#side'),
                    main: grab('#main'),
                    header: grab('#main header') || grab('header[role="banner"]'),
                    composer: grab('[contenteditable="true"][data-tab="10"]')
                              || grab('footer [contenteditable="true"]') || grab('footer'),
                };
            }"""
        )
    except Exception as exc:
        html = {"error": str(exc)}
    logger.info("sender: OPEN-FAILURE reason=%s url=%s title=%r activeElement=%s",
                reason, url, title, focus)
    logger.info("sender: OPEN-FAILURE present: #side=%s #main=%s header=%s composer=%s",
                bool(html.get("side")), bool(html.get("main")),
                bool(html.get("header")), bool(html.get("composer")))
    try:
        await get_db().whatsapp_dom_snapshots.insert_one({
            "id": str(uuid.uuid4()),
            "reason": f"open_failure:{reason}",
            "url": url,
            "page_title": title,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_element": focus,
            "side_html": html.get("side"),
            "main_html": html.get("main"),
            "header_html": html.get("header"),
            "composer_html": html.get("composer"),
            "extra": extra or {},
        })
        logger.info("sender: OPEN-FAILURE DOM snapshot stored (reason=%s)", reason)
    except Exception as exc:
        logger.info("sender: OPEN-FAILURE snapshot store error=%s", exc)


async def _open_group_chat(page: Page, group_name: str) -> str:
    """Open a group conversation deterministically. Returns:
      'OPENED'        — group chat opened
      'NOT_FOUND'     — searched correctly, group genuinely absent (terminal)
      'SEARCH_FAILED' — could not focus/operate the sidebar search (retryable)

    Every attempt emits a structured diagnostic record (Investigation 6). On
    NOT_FOUND a DOM snapshot is stored. The search box is ALWAYS inside #side, so
    typing can never land in the conversation composer.
    """
    diag = {
        "requested_group": group_name,
        "requested_norm": _norm_group(group_name),
        "selector_chosen": None,
        "focus": None,
        "value_after_clear": None,
        "value_after_type": None,
        "candidates": [],
        "candidates_norm": [],
        "selected": None,
        "rejection_reason": None,
        "worker_stage": "reset",
    }

    def _emit(stage: str) -> None:
        diag["worker_stage"] = stage
        logger.info("sender: GROUP-RESOLVE DIAG %s", diag)

    # PHASE26B STEP 3: full selector inventory for this discovery attempt —
    # every selector that MIGHT be tried, logged once up front.
    logger.info(
        "PHASE26B STEP3_SELECTORS search_box_selectors=%s result_title_selectors=%s",
        SEARCH_BOX_SELECTORS, RESULT_TITLE_SELECTORS,
    )

    # Neutral state first — never search relative to a previously-open chat.
    await _reset_to_chat_list(page)
    if not await dismiss_blocking_dialogs(page, "search"):
        diag["rejection_reason"] = "blocking dialog before search"
        _emit("reset")
        return "SEARCH_FAILED"

    # 1) Resolve a sidebar-scoped search box (probes logged; composer excluded).
    diag["worker_stage"] = "focus_search"
    search_sel = None
    for sel in SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            vis = (await loc.first.is_visible()) if n else False
            logger.info("sender: search-box probe %-52s count=%d visible=%s", sel, n, vis)
            if vis and search_sel is None:
                search_sel = sel
        except Exception as exc:
            logger.info("sender: search-box probe %-52s error=%s", sel, exc)
    diag["selector_chosen"] = search_sel
    if not search_sel:
        diag["rejection_reason"] = "no sidebar search box resolved"
        _emit("focus_search")
        await _capture_open_failure(page, "search_box_absent", diag)
        return "SEARCH_FAILED"
    logger.info("sender: STEP 1 search textbox located via %s", search_sel)
    await _safe_screenshot(page, "/tmp/before_search.png")
    await _p26b_dump(page, "group_before_search", extra={"search_selector": search_sel})  # PHASE26B

    # 2) Focus it and PROVE focus is inside the sidebar before typing.
    try:
        await page.click(search_sel, timeout=5_000)
        await asyncio.sleep(0.2)
    except Exception as exc:
        diag["rejection_reason"] = f"search click failed: {exc}"
        _emit("focus_search")
        return "SEARCH_FAILED"
    focus = await _focus_report(page)
    diag["focus"] = focus
    if not focus.get("in_side") or focus.get("in_main"):
        # Never type unless focus is verifiably in the sidebar (guards against the
        # composer-targeting regression). One re-click, then bail as retryable.
        logger.warning("sender: focus NOT in sidebar after click (focus=%s) — re-clicking", focus)
        try:
            await page.click(search_sel, timeout=5_000)
            await asyncio.sleep(0.2)
        except Exception:
            pass
        focus = await _focus_report(page)
        diag["focus"] = focus
        if not focus.get("in_side") or focus.get("in_main"):
            diag["rejection_reason"] = "focus not in sidebar (would type into composer)"
            _emit("focus_search")
            return "SEARCH_FAILED"

    # 3) Clear, then read back to confirm empty (never type blind).
    try:
        await page.keyboard.press("Control+A")   # Linux worker — NOT macOS Meta+A
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.4)
    except Exception as exc:
        diag["rejection_reason"] = f"clear failed: {exc}"
        _emit("type_query")
        return "SEARCH_FAILED"
    _, diag["value_after_clear"] = await _read_search_value(page, search_sel)

    # 4) Type the group name; read the value back (fast, input-aware).
    try:
        await page.type(search_sel, group_name, delay=40)
        await asyncio.sleep(1.2)
    except Exception as exc:
        diag["rejection_reason"] = f"type failed: {exc}"
        _emit("type_query")
        await _capture_open_failure(page, "type_failed", diag)
        return "SEARCH_FAILED"
    logger.info("sender: STEP 2 search text entered (%r)", group_name)

    # Prove whether React replaced the input after typing (activeElement value).
    post = await _focus_report(page)
    logger.info("sender: STEP 2b activeElement after type: tag=%s id=%s value=%r in_side=%s",
                post.get("tag"), post.get("id"), post.get("value"), post.get("in_side"))
    await _safe_screenshot(page, "/tmp/after_search.png")
    await _p26b_search_evidence(page, "group_after_type", search_sel, group_name)  # PHASE26B

    # STEP 3 — read-back classification: READ_OK_MATCH / READ_OK_MISMATCH /
    # READ_UNREADABLE. The read-back is DEFENCE-IN-DEPTH only: an unreadable box
    # must NOT abort (focus-in-sidebar is already verified and the normalized
    # candidate match still gates the click). Only a readable, genuinely
    # different value means we typed the wrong thing -> SEARCH_FAILED.
    readable, typed = await _read_search_value(page, search_sel)
    diag["value_after_type"] = typed if readable else "<unreadable>"
    if not readable:
        readback = "READ_UNREADABLE"
    elif _norm_group(typed) == _norm_group(group_name):
        readback = "READ_OK_MATCH"
    else:
        readback = "READ_OK_MISMATCH"
    diag["readback"] = readback
    logger.info("sender: STEP 3 read-back classification=%s (value=%r)", readback, typed)
    if readback == "READ_OK_MISMATCH":
        diag["rejection_reason"] = (f"search value mismatch: typed={typed!r} "
                                    f"expected={group_name!r}")
        _emit("type_query")
        await _capture_open_failure(page, "search_value_mismatch", diag)
        return "SEARCH_FAILED"
    if readback == "READ_UNREADABLE":
        logger.warning("sender: search value UNREADABLE — proceeding to candidate "
                       "collection (sidebar focus verified; candidate match still gates click)")

    # 5) Collect candidates, log them (raw + normalized), match deterministically.
    diag["worker_stage"] = "await_results"
    await _p26b_search_evidence(page, "group_after_settle", search_sel, group_name)  # PHASE26B
    result_sel, candidates = await _collect_search_candidates(page)
    diag["candidates"] = [c["title"] for c in candidates]
    diag["candidates_norm"] = [c["norm"] for c in candidates]
    logger.info("sender: STEP 4 candidate rows collected count=%d via %s",
                len(candidates), result_sel)
    target = _norm_group(group_name)
    match = next((c for c in candidates if c["norm"] == target), None)

    if not match:
        diag["rejection_reason"] = (
            "no candidate title equals the normalized requested name"
            if candidates else "no visible search-result rows"
        )
        _emit("match")
        await _store_dom_snapshot(page, "group_not_found", {
            "group": group_name, "requested_norm": target,
            "candidates": diag["candidates"], "candidates_norm": diag["candidates_norm"],
            "result_selector": result_sel, "search_selector": search_sel,
        })
        # PHASE26B: WHY zero/no-match results — explicit, evidence-backed reason.
        await _p26b_dump(page, "group_not_found", extra={
            "why": diag["rejection_reason"],
            "dom_candidates_count": len(candidates),
            "candidates_raw": diag["candidates"],
            "candidates_normalized": diag["candidates_norm"],
            "requested_normalized": target,
        })
        return "NOT_FOUND"

    # 6) Open the matched row.
    diag["selected"] = match["title"]
    diag["worker_stage"] = "open"
    _emit("open")
    logger.info("sender: STEP 5 candidate selected %r", match["title"])
    try:
        await match["locator"].click(timeout=5_000)
    except Exception as exc:
        logger.warning("sender: matched-row click blocked (%s) — dismissing dialogs, retry once", exc)
        if not await dismiss_blocking_dialogs(page, "open-chat"):
            return "SEARCH_FAILED"
        try:
            await match["locator"].click(timeout=5_000)
        except Exception as exc2:
            logger.warning("sender: matched-row click retry failed: %s", exc2)
            return "SEARCH_FAILED"
    logger.info("sender: STEP 6 candidate clicked")
    await _safe_screenshot(page, "/tmp/after_click.png")
    await asyncio.sleep(1.0)
    await _p26b_dump(page, "group_after_select", extra={"selected": match["title"]})  # PHASE26B
    return "OPENED"


async def send_whatsapp_message(
    page: Page,
    destination_type: str,  # "group" | "number"
    destination: str,
    message_body: str,
    media_url: Optional[str] = None,
    is_retry: bool = False,
) -> dict:
    """
    Core automation logic for sending a single WhatsApp message.
    Returns {"state": str, "evidence": dict} with structured decision evidence.
    """
    evidence = {
        "chat_opened": False,
        "header_verified": False,
        "compose_found": False,
        "send_clicked": False,
        "dom_verified": False,
        "verification_method": None,
        "verification_selector": None,
    }
    logger.info("sender: preparing to send to %s (%s)", destination, destination_type)

    # PHASE26B STEP 1/2: raw destination, detected type, normalized value,
    # and which discovery strategy this destination_type will use.
    _p26b_normalized = (
        "".join(filter(str.isdigit, destination)) if destination_type == "number"
        else _norm_group(destination)
    )
    _p26b_strategy = (
        "PHONE_DEEPLINK (page.goto https://web.whatsapp.com/send?phone=<digits>)"
        if destination_type == "number"
        else "SIDEBAR_SEARCH (#side search box -> #pane-side results list)"
    )
    logger.info(
        "PHASE26B STEP1_2 raw_destination=%r destination_type=%r normalized_destination=%r "
        "discovery_strategy=%r",
        destination, destination_type, _p26b_normalized, _p26b_strategy,
    )

    # Clear any blocking dialog BEFORE any interaction (login nags, feature
    # announcements). Unknown/undismissable dialogs -> retryable, nothing sent.
    if not await dismiss_blocking_dialogs(page, "pre-open"):
        logger.warning("sender: blocking dialog could not be handled -> CHAT_NOT_OPENED")
        return {"state": CHAT_NOT_OPENED, "evidence": evidence}

    if destination_type == "number":
        # Format clean phone number (digits only, e.g. 919876543210)
        phone = "".join(filter(str.isdigit, destination))
        # Navigate to wa.me link with prepopulated text or empty send to open the chat
        wa_url = f"https://web.whatsapp.com/send?phone={phone}"
        logger.info("sender: navigating to number link: %s", wa_url)
        await _p26b_dump(page, "number_before_navigate")  # PHASE26B
        _p26b_nav_started = time.monotonic()  # PHASE26B
        await page.goto(wa_url, wait_until="domcontentloaded")
        await _p26b_dump(page, "number_after_navigate",
                          extra={"elapsed_sec": round(time.monotonic() - _p26b_nav_started, 2)})  # PHASE26B

        # Wait for the chat to load (msg_box or chat not found popups)
        try:
            await page.wait_for_selector(SEL["msg_box"], timeout=45_000)
        except PlaywrightTimeoutError:
            # PHASE26B: capture ALL failure evidence BEFORE any raise below —
            # URL/DOM/screenshot/activeElement/pane+composer+spinner presence/
            # console+network errors, exactly as required by the investigation.
            await _p26b_dump(page, "number_composer_wait_timeout", extra={
                "elapsed_sec": round(time.monotonic() - _p26b_nav_started, 2),
                "timeout_ms": 45_000,
            })
            # Check if there is an error dialog (e.g. "Phone number shared via url is invalid")
            # Usually it says "Use WhatsApp on your phone..." or "Phone number... is invalid"
            dialog_exists = await page.is_visible("text=Phone number shared via url is invalid") or \
                            await page.is_visible("text=Invalid phone number") or \
                            await page.is_visible('[data-testid="popup-controls-ok"]')
            if dialog_exists:
                # Click OK if possible to clear
                try:
                    await page.click('[data-testid="popup-controls-ok"]', timeout=3_000)
                except Exception:
                    pass
                raise ValueError(f"Phone number {phone} is invalid on WhatsApp")
            raise RuntimeError("Timed out waiting for chat window to load via wa.me link")

        await _p26b_dump(page, "number_composer_appeared",
                          extra={"elapsed_sec": round(time.monotonic() - _p26b_nav_started, 2)})  # PHASE26B
        evidence["chat_opened"] = True

    elif destination_type == "group":
        logger.info("sender: searching for group name '%s'", destination)
        result = await _open_group_chat(page, destination)
        if result == "SEARCH_FAILED":
            # PHASE26B: full evidence capture BEFORE returning the terminal
            # CHAT_NOT_OPENED state (mission: "capture ALL evidence before throwing").
            await _p26b_dump(page, "chat_not_opened_search_failed", extra={"group": destination})
            # Pre-send failure (stale/missing search box). Nothing was sent —
            # snapshot the DOM and return a RETRYABLE state (never 'sent').
            await _store_dom_snapshot(page, "group_search_failed", {"group": destination})
            logger.warning("sender: group open SEARCH_FAILED for %r -> CHAT_NOT_OPENED (retryable)",
                           destination)
            return {"state": CHAT_NOT_OPENED, "evidence": evidence}
        if result == "NOT_FOUND":
            # PHASE26B: full evidence capture BEFORE raising (terminal, not retried).
            await _p26b_dump(page, "group_not_found_before_raise", extra={"group": destination})
            # Group genuinely absent — terminal (do not retry).
            raise ValueError(f"WhatsApp group '{destination}' not found in chat list")
        # result == "OPENED" -> fall through to chat-ready + typing + verify (unchanged)
        evidence["chat_opened"] = True
    else:
        raise ValueError(f"Unknown destination type '{destination_type}'")

    # STEP 7 / STEP 11: wait for the composer to be interactive.
    logger.info("sender: STEP 7 waiting for chat ready (composer interactive)...")
    try:
        await _wait_for_chat_ready(page)
        evidence["compose_found"] = True
        logger.info("sender: STEP 11 composer found and interactive")
        await _p26b_dump(page, "composer_ready")  # PHASE26B
    except Exception as exc:
        logger.warning("sender: STEP 7/11 FAILED — chat not ready (%s) -> CHAT_NOT_OPENED", exc)
        # PHASE26B: full evidence capture BEFORE returning CHAT_NOT_OPENED.
        await _p26b_dump(page, "composer_wait_failed", extra={"destination": destination, "error": str(exc)})
        await _capture_open_failure(page, "chat_not_ready",
                                    {"destination": destination, "error": str(exc)})
        return {"state": CHAT_NOT_OPENED, "evidence": evidence}

    # STEP 8-10, 12-13: the compose box is NOT sufficient — confirm a real
    # conversation is open (panel + header + recipient) before typing. For groups
    # the header title must equal the target group name (wrong-chat guard).
    expected_name = destination if destination_type == "group" else None
    conversation_ready, hdr_found, rcp_found, rcp_text = await _verify_chat_open(page, expected_name)
    logger.info("sender: STEP 8 conversation header found=%s", hdr_found)
    logger.info("sender: STEP 9 header title=%r", rcp_text)
    logger.info("sender: STEP 10 expected title=%r", expected_name)
    try:
        body_scope = await _resolve_scope(page)
        body_present = bool(await page.locator(body_scope).count())
    except Exception:
        body_present = False
    logger.info("sender: STEP 12 conversation body present=%s", body_present)
    logger.info("sender: STEP 13 verification passed=%s", conversation_ready)
    if not conversation_ready:
        logger.warning("sender: conversation NOT open -> CHAT_NOT_OPENED (refusing to type)")
        # PHASE26B: full evidence capture BEFORE returning CHAT_NOT_OPENED.
        await _p26b_dump(page, "conversation_verify_failed", extra={
            "destination": destination, "expected": expected_name,
            "header_found": hdr_found, "recipient_found": rcp_found,
            "recipient_text": rcp_text, "body_present": body_present,
        })
        await _capture_open_failure(page, "conversation_not_open",
                                    {"destination": destination, "expected": expected_name,
                                     "header_found": hdr_found, "recipient_found": rcp_found,
                                     "recipient_text": rcp_text, "body_present": body_present})
        return {"state": CHAT_NOT_OPENED, "evidence": evidence}
    evidence["header_verified"] = True
    logger.info("sender: STEP 14 ready to send")
    await _p26b_dump(page, "conversation_verified")  # PHASE26B

    # Snapshot message counts BEFORE typing — verification will only check NEW
    # messages that appear after this point, preventing false positives from old
    # campaign messages with the same template text.
    baselines = await _snapshot_msg_baselines(page)

    await _p26b_dump(page, "before_send")  # PHASE26B
    # Instrumentation: send-button DOM + the exact message string (PROBLEM #3 trace).
    await _dump_send_dom(page)
    logger.info(
        "sender: FINAL MESSAGE SENT TO WHATSAPP (len=%d, lines=%d): %r",
        len(message_body or ""), len((message_body or "").splitlines()), message_body,
    )

    # Duplicate prevention: ONLY on retries (attempt_count > 0). On first attempt
    # old messages from prior campaigns must never suppress a new send.
    if await _already_delivered(page, message_body, is_retry=is_retry):
        logger.warning(
            "sender: RETRY — message already present as a recent outgoing bubble — "
            "NOT resending (treating as already delivered / verified)"
        )
        evidence["dom_verified"] = True
        evidence["verification_method"] = "already_delivered"
        return {"state": MESSAGE_SENT_AND_VERIFIED, "evidence": evidence}

    # Handle media attachment if present
    if media_url:
        logger.info("sender: media_url provided, downloading %s", media_url)
        temp_file_path = None
        try:
            # Download file to a secure temporary path
            suffix = os.path.splitext(media_url.split("?")[0])[1] or ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                temp_file_path = temp_file.name
                
            # Perform download
            headers = {"User-Agent": "Mozilla/5.0"}
            req = urllib.request.Request(media_url, headers=headers)
            with urllib.request.urlopen(req) as response, open(temp_file_path, "wb") as out_file:
                out_file.write(response.read())
                
            logger.info("sender: downloaded media to %s", temp_file_path)
            
            # Click attachment button (+)
            await page.click(SEL["attach_btn"])
            await asyncio.sleep(1.0)
            
            # Use file input. WhatsApp has file inputs for doc or image/video
            # Let's inspect file type to decide which input to use
            lower_suffix = suffix.lower()
            is_image_or_video = lower_suffix in [".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webp"]
            
            input_selector = 'input[type="file"]'
            # Playwright allows setting files on input elements directly
            # We wait for file input to become active
            file_input = page.locator(input_selector).first
            await file_input.set_input_files(temp_file_path)
            await asyncio.sleep(2.0)
            
            # Wait for the media preview send screen to appear
            # The media send button is usually different or we can find it by aria-label / test-id
            # On WhatsApp Web, the media caption box input is different or we can just press Enter
            # In the preview screen, there is a send button: [data-testid="send"]
            # Let's write the message body as the caption!
            if message_body:
                # In media preview screen, there is a caption input field. Let's find it.
                # It usually has a placeholder or test-id
                caption_xpath = '//div[contains(@class, "lexical-rich-text")]'
                try:
                    await page.click(caption_xpath, timeout=5_000)
                    await page.keyboard.type(message_body)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning("sender: could not set caption, sending caption as separate message later: %s", e)
                    # We will send message_body as a second message if caption fails
                    # Let's clear message_body so we don't send it as caption AND separate message
                    # But we'll keep it to send separately.
            
            # Click send button on preview page (resilient fallback chain)
            if not await dismiss_blocking_dialogs(page, "send"):
                logger.warning("sender: blocking dialog before media send — not pressing "
                               "anything -> MESSAGE_NOT_SENT")
                return {"state": MESSAGE_NOT_SENT, "evidence": evidence}
            await _safe_screenshot(page, "/tmp/pre_send.png")
            sent_via = await _find_and_click_send(page)
            evidence["send_clicked"] = True
            logger.info("sender: media send click executed via %s", sent_via)
            await asyncio.sleep(3.0)  # Wait for media upload and send to complete
            await _safe_screenshot(page, "/tmp/post_send.png")
            
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass
    else:
        # Text-only message
        # Focus message box and clear any stale draft (a prior blocked attempt
        # may have left text in the composer — retyping on top would double it).
        await page.click(SEL["msg_box"])
        await page.keyboard.press("Control+A")   # Linux worker — NOT macOS Meta+A
        await page.keyboard.press("Backspace")
        # Standard approach: paste or type text. Since we want to preserve newlines, we can use copy-paste
        # or press Shift+Enter for newlines. Let's type line by line or paste via keyboard.
        # Typing character by character can be slow, but using page.type with delay 0 is fast enough
        # and doesn't require clipboard access.
        # For newlines, we split the message and send Shift+Enter
        lines = message_body.split("\n")
        for i, line in enumerate(lines):
            if line:
                await page.keyboard.type(line)
            if i < len(lines) - 1:
                await page.keyboard.down("Shift")
                await page.keyboard.press("Enter")
                await page.keyboard.up("Shift")
        
        logger.info("sender: message text inserted? True (%d line(s))", len(lines))
        await asyncio.sleep(0.5)
        # Click send button via resilient fallback chain. If a dialog is
        # blocking, do NOT press anything (Enter could activate a dialog
        # button) — composer still holds the text, so this is retryable.
        if not await dismiss_blocking_dialogs(page, "send"):
            logger.warning("sender: blocking dialog before send — not pressing anything "
                           "-> MESSAGE_NOT_SENT")
            return {"state": MESSAGE_NOT_SENT, "evidence": evidence}
        await _safe_screenshot(page, "/tmp/pre_send.png")
        sent_via = await _find_and_click_send(page)
        evidence["send_clicked"] = True
        logger.info("sender: text send click executed via %s", sent_via)
        await asyncio.sleep(1.0)
        await _safe_screenshot(page, "/tmp/post_send.png")

    # TASK 2 instrumentation: dump the live outgoing-message DOM so the real
    # selector is captured in the logs.
    await _dump_outgoing_dom(page)

    # PROBLEM #2 / TASK 4: classify the outcome — never collapse to one FAILED.
    await asyncio.sleep(1.5)
    logger.info("sender: verification started (baselines=%s)", "provided" if baselines else "none")
    verified, composer_cleared, matched_sel = await _verify_delivery(page, message_body, baselines=baselines)

    if verified:
        evidence["dom_verified"] = True
        evidence["verification_method"] = "outgoing_bubble_match"
        evidence["verification_selector"] = matched_sel
        logger.info("sender: MESSAGE_SENT_AND_VERIFIED — outgoing bubble confirmed")
        return {"state": MESSAGE_SENT_AND_VERIFIED, "evidence": evidence}

    await _safe_screenshot(page, "/tmp/verify_failed.png")
    # No outgoing bubble. The composer state distinguishes "never sent" (safe to
    # retry) from "left the composer but unconfirmed" (must NOT auto-retry —
    # retrying could duplicate a delivered message).
    if composer_cleared:
        logger.warning(
            "sender: MESSAGE_SENT_BUT_NOT_VERIFIED — composer cleared but no outgoing bubble "
            "matched. Marking as sent+unverified (no retry — message likely delivered)."
        )
        return {"state": MESSAGE_SENT_BUT_NOT_VERIFIED, "evidence": evidence}

    logger.warning(
        "sender: MESSAGE_NOT_SENT — composer still holds text and no outgoing bubble. "
        "Safe to retry."
    )
    return {"state": MESSAGE_NOT_SENT, "evidence": evidence}
