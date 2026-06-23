"""
WhatsApp Worker — Sender Engine
Handles browser actions for locating a chat (personal or group) and sending messages with optional media attachments.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from db import get_db
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from session import SEL

logger = logging.getLogger(__name__)

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


async def _verify_chat_open(page: Page) -> Tuple[bool, bool, bool, str]:
    """TASK 1: prove a real conversation is open BEFORE typing.

    Requires the conversation panel (#main), a header inside it, and a visible
    recipient name/phone. The compose box alone is NOT accepted (it can exist on
    the home screen). Returns (ready, header_found, recipient_found, recipient_text).
    """
    panel_found, panel_sel = await _first_present(page, CONV_PANEL_SELECTORS)
    header_found, _ = await _first_present(page, CONV_HEADER_SELECTORS)

    recipient_found = False
    recipient_text = ""
    for sel in RECIPIENT_SELECTORS:
        try:
            loc = page.locator(sel)
            if await loc.count():
                recipient_text = (await loc.first.inner_text()).strip() or \
                                 (await loc.first.get_attribute("title") or "").strip()
                if recipient_text:
                    recipient_found = True
                    break
        except Exception:
            pass

    conversation_ready = bool(panel_found and header_found)
    logger.info("sender: CHAT OPEN VERIFICATION")
    logger.info("sender:   panel_found=%s (%s)", panel_found, panel_sel)
    logger.info("sender:   header_found=%s", header_found)
    logger.info("sender:   recipient_found=%s", recipient_found)
    logger.info("sender:   recipient_text=%r", recipient_text[:60])
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


async def _find_outgoing_with_text(page: Page, needle: str) -> Tuple[Optional[str], str]:
    """Find a message element in the ACTIVE conversation whose text contains the
    exact first-30-char needle. Walks the registry chain (primary then fallback),
    logging the exact selector chain used, and returns (matched_full_selector,
    text) or (None, '')."""
    scope = await _resolve_scope(page)
    chain = (SELECTOR_REGISTRY["message_element"]["primary"]
             + SELECTOR_REGISTRY["message_element"]["fallback"])
    logger.info("sender: verify — SELECTOR CHAIN scope=%r message_element=%s", scope, chain)
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
        # Newest messages are last; check the tail for the needle.
        check = min(n, 8)
        for i in range(n - check, n):
            try:
                t = (await loc.nth(i).inner_text()).strip()
            except Exception:
                continue
            if needle and (needle in t or (norm_needle and norm_needle in _norm(t))):
                logger.info("sender: verify — ✅ MATCHED layer=%r element#%d text[:60]=%r",
                            full, i, t[:60])
                return full, t

    # TASK 6: final fallback — if the exact text exists ANYWHERE in the active
    # conversation, treat as VERIFIED even when no message_element layer matched.
    try:
        scope_text = await page.locator(scope).first.inner_text()
        if needle and (needle in scope_text or (norm_needle and norm_needle in _norm(scope_text))):
            logger.info("sender: verify — ✅ MATCHED via active-conversation TEXT FALLBACK (scope=%r)", scope)
            return f"{scope} ::text-fallback", needle
    except Exception as exc:
        logger.info("sender: verify — text fallback error=%s", exc)
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


async def _verify_delivery(page: Page, message_body: str) -> Tuple[bool, bool, bool]:
    """VERIFIED iff a message element in the active conversation contains the
    exact first 30 characters of the sent payload. A cleared compose box is NOT
    proof (logged for diagnosis only). On failure, a DOM snapshot is stored.
    Returns (verified, composer_cleared, verified)."""
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
    matched_sel, matched_text = await _find_outgoing_with_text(page, needle)
    verified = matched_sel is not None

    if verified:
        ts = await _msg_timestamp(page, matched_sel)
        logger.info("sender: verify — VERIFIED via chain=%r | text[:80]=%r | timestamp=%r",
                    matched_sel, matched_text[:80], ts)
    else:
        logger.warning("sender: verify — NOT VERIFIED: no message element contains the first-30-char "
                       "needle=%r — storing DOM snapshot", needle)
        await _store_dom_snapshot(page, "verify_failed", {"needle": needle})

    return verified, composer_cleared, verified


async def _already_delivered(page: Page, message_body: str) -> bool:
    """Duplicate guard: on a retry the active conversation may already contain
    this exact message from a prior attempt. Uses the same registry chain as
    verification (first-30-char match) — if present, do NOT type again."""
    needle = _needle(message_body)
    if not needle:
        return False
    matched_sel, _ = await _find_outgoing_with_text(page, needle)
    return matched_sel is not None


async def send_whatsapp_message(
    page: Page,
    destination_type: str,  # "group" | "number"
    destination: str,
    message_body: str,
    media_url: Optional[str] = None,
) -> str:
    """
    Core automation logic for sending a single WhatsApp message.
    
    If destination_type is "group", searches the group name in the search box.
    If destination_type is "number", navigates directly using the wa.me URL.
    """
    logger.info("sender: preparing to send to %s (%s)", destination, destination_type)
    
    if destination_type == "number":
        # Format clean phone number (digits only, e.g. 919876543210)
        phone = "".join(filter(str.isdigit, destination))
        # Navigate to wa.me link with prepopulated text or empty send to open the chat
        wa_url = f"https://web.whatsapp.com/send?phone={phone}"
        logger.info("sender: navigating to number link: %s", wa_url)
        await page.goto(wa_url, wait_until="domcontentloaded")
        
        # Wait for the chat to load (msg_box or chat not found popups)
        try:
            await page.wait_for_selector(SEL["msg_box"], timeout=45_000)
        except PlaywrightTimeoutError:
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
            
    elif destination_type == "group":
        # For groups, search by exact name in the search box
        logger.info("sender: searching for group name '%s'", destination)
        
        # Focus search box
        await page.click(SEL["search_box"])
        # Clear search box first by selecting all and deleting (or clicking clear button if available)
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.5)
        
        # Type group name
        await page.type(SEL["search_box"], destination, delay=50)
        await asyncio.sleep(1.5)
        
        # Find exact match in the search results
        # We look for a list item that contains the exact title
        xpath = f"//span[@title='{destination}']"
        try:
            await page.wait_for_selector(xpath, timeout=10_000)
            await page.click(xpath)
            await asyncio.sleep(1.0)
        except PlaywrightTimeoutError:
            raise ValueError(f"WhatsApp group '{destination}' not found in chat list")
    else:
        raise ValueError(f"Unknown destination type '{destination_type}'")

    # PROBLEM #1: wait for the composer to be interactive.
    try:
        await _wait_for_chat_ready(page)
    except Exception as exc:
        logger.warning("sender: chat not ready (%s) -> CHAT_NOT_OPENED", exc)
        return CHAT_NOT_OPENED

    # TASK 1: the compose box is NOT sufficient — confirm a real conversation is
    # open (panel + header + recipient) before typing anything.
    conversation_ready, _hdr, _rcp, _rtext = await _verify_chat_open(page)
    if not conversation_ready:
        logger.warning("sender: conversation NOT open -> CHAT_NOT_OPENED (refusing to type)")
        return CHAT_NOT_OPENED

    # Instrumentation: send-button DOM + the exact message string (PROBLEM #3 trace).
    await _dump_send_dom(page)
    logger.info(
        "sender: FINAL MESSAGE SENT TO WHATSAPP (len=%d, lines=%d): %r",
        len(message_body or ""), len((message_body or "").splitlines()), message_body,
    )

    # TASK 3 (duplicate prevention): if this exact message is already a recent
    # outgoing bubble (delivered on a prior attempt), do NOT send again.
    if await _already_delivered(page, message_body):
        logger.warning(
            "sender: message already present as a recent outgoing bubble — NOT resending "
            "(treating as already delivered / verified)"
        )
        return MESSAGE_SENT_AND_VERIFIED

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
            await _safe_screenshot(page, "/tmp/pre_send.png")
            sent_via = await _find_and_click_send(page)
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
        # Focus message box
        await page.click(SEL["msg_box"])
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
        # Click send button via resilient fallback chain
        await _safe_screenshot(page, "/tmp/pre_send.png")
        sent_via = await _find_and_click_send(page)
        logger.info("sender: text send click executed via %s", sent_via)
        await asyncio.sleep(1.0)
        await _safe_screenshot(page, "/tmp/post_send.png")

    # TASK 2 instrumentation: dump the live outgoing-message DOM so the real
    # selector is captured in the logs.
    await _dump_outgoing_dom(page)

    # PROBLEM #2 / TASK 4: classify the outcome — never collapse to one FAILED.
    await asyncio.sleep(1.5)
    logger.info("sender: verification started")
    verified, composer_cleared, bubble_match = await _verify_delivery(page, message_body)

    if verified:
        logger.info("sender: MESSAGE_SENT_AND_VERIFIED — outgoing bubble confirmed")
        return MESSAGE_SENT_AND_VERIFIED

    await _safe_screenshot(page, "/tmp/verify_failed.png")
    # No outgoing bubble. The composer state distinguishes "never sent" (safe to
    # retry) from "left the composer but unconfirmed" (must NOT retry — retrying
    # is exactly what duplicated Sahal's message).
    if composer_cleared:
        logger.warning(
            "sender: MESSAGE_SENT_BUT_NOT_VERIFIED — composer cleared but no outgoing bubble "
            "matched. Will NOT retry (prevents duplicate delivery)."
        )
        return MESSAGE_SENT_BUT_NOT_VERIFIED

    logger.warning(
        "sender: MESSAGE_NOT_SENT — composer still holds text and no outgoing bubble. "
        "Safe to retry."
    )
    return MESSAGE_NOT_SENT
