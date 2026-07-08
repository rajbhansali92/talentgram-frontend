"""
WhatsApp Worker — Modal / Dialog Handling Framework

WhatsApp Web shows aria-modal dialogs (feature announcements, notification
nags, …) that intercept pointer events over the whole app. This module detects
them, identifies them by title/body, and dismisses ONLY dialogs recognized as
benign — via Escape, then an explicit Close/X button, then a whitelist of safe
buttons. Never force-clicks, never clicks an unrecognized button.

Every dismissal attempt emits a structured DIALOG_EVENT log line (JSON) with
the dialog title, truncated body, method, context, and outcome. Unrecognized
dialogs are never touched: they are captured (screenshot + dialog HTML +
text -> whatsapp_dom_snapshots) and logged as UNKNOWN_DIALOG so a human can
extend the registry deliberately.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from db import get_db

logger = logging.getLogger(__name__)

DIALOG_SELECTOR = 'div[role="dialog"]'

# Settle time after a dismissal attempt before re-checking (tests set to 0).
SETTLE_SEC = 0.7

# Recognition registry — a dialog may only be auto-dismissed when its
# title/body matches one of these patterns (matched against the casefolded,
# whitespace-collapsed text). Anything else is UNKNOWN_DIALOG: captured,
# logged, never clicked. Extend deliberately after inspecting the capture.
KNOWN_DIALOG_PATTERNS = [
    r"what.{0,3}s new on whatsapp",      # "What’s new on WhatsApp Web" (Jul 2026 rollout)
    r"turn on notifications",
    r"whatsapp is now faster",
]

# Explicit close/X controls, scoped INSIDE the dialog.
CLOSE_BUTTON_SELECTORS = [
    'button[aria-label="Close"]',
    '[role="button"][aria-label="Close"]',
]

# Benign acknowledgement buttons — exact-label whitelist, scoped to the dialog.
SAFE_BUTTON_LABELS = ["Continue", "Got it", "OK", "Not now", "Done", "Dismiss"]


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _is_recognized(title: str, body: str) -> bool:
    hay = _norm(f"{title} {body}")
    return any(re.search(p, hay) for p in KNOWN_DIALOG_PATTERNS)


def _log_event(
    context: str,
    title: str,
    body: str,
    method: Optional[str],
    success: bool,
    outcome: str,
) -> None:
    """Structured record of every dialog encounter — nothing is silent."""
    logger.info(
        "modals: DIALOG_EVENT %s",
        json.dumps(
            {
                "context": context,
                "title": title[:120],
                "body": body[:300],
                "method": method,
                "success": success,
                "outcome": outcome,
            },
            ensure_ascii=False,
        ),
    )


# WhatsApp's own in-chat "Group description" info panel is marked role="dialog"
# but is NOT a blocking overlay — its content is dynamic per-conversation data
# (the open group's own description/links) and it has its own Close affordance.
# It must never be treated as a blocking dialog: not whitelisted, not dismissed,
# not clicked — simply excluded from detection entirely.
CONVERSATION_SUBHEADER_SELECTOR = '[data-testid="conversation-subheader"]'


async def _is_conversation_subheader(item) -> bool:
    try:
        if await item.get_attribute("data-testid") == "conversation-subheader":
            return True
    except Exception:
        pass
    try:
        if await item.locator(CONVERSATION_SUBHEADER_SELECTOR).count():
            return True
    except Exception:
        pass
    return False


async def _visible_dialog(page):
    """First VISIBLE role=dialog element, or None. Skips WhatsApp's own
    in-chat conversation-subheader info panel (not a blocking overlay)."""
    try:
        loc = page.locator(DIALOG_SELECTOR)
        n = await loc.count()
        for i in range(min(n, 5)):
            item = loc.nth(i)
            try:
                if not await item.is_visible():
                    continue
                if await _is_conversation_subheader(item):
                    continue
                return item
            except Exception:
                continue
    except Exception:
        return None
    return None


async def _dialog_title_and_body(dialog) -> Tuple[str, str]:
    """Best-effort title + truncated body text of the dialog."""
    body = ""
    try:
        body = (await dialog.inner_text()).strip()
    except Exception:
        pass
    title = ""
    try:
        h = dialog.locator("h1, h2, [role='heading']")
        if await h.count():
            title = (await h.first.inner_text()).strip()
    except Exception:
        pass
    if not title:
        # First real text line; skip icon-name lines like "ic-close" / "wds-…".
        for ln in body.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith(("ic-", "wds-")):
                title = ln
                break
    return title[:120], body[:300]


_UNKNOWN_DIALOG_DIAGNOSTICS_JS = """
el => {
    const ancestry = [];
    let node = el;
    while (node && node !== document.body && node.nodeType === 1) {
        const tid = node.getAttribute ? node.getAttribute('data-testid') : null;
        ancestry.push({
            tag: node.tagName.toLowerCase(),
            id: node.id || null,
            testid: tid,
            cls: (node.className || '').toString().slice(0, 80),
        });
        node = node.parentElement;
    }
    ancestry.push({tag: 'body', id: null, testid: null, cls: null});

    const rect = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);

    const composer = document.querySelector('[data-testid="conversation-compose-box-input"]');
    const searchBox = document.querySelector('#side [aria-label="Search or start a new chat"]')
        || document.querySelector('[data-testid="chat-list-search"]');
    const titleEl = document.querySelector('[data-testid="conversation-info-header-chat-title"]');

    return {
        ancestry_path: ancestry.map(a => a.testid ? ('@' + a.testid) : (a.id ? ('#' + a.id) : a.tag)).join(' < '),
        ancestry_detail: ancestry,
        bounding_rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        computed_position: cs.position,
        computed_z_index: cs.zIndex,
        computed_pointer_events: cs.pointerEvents,
        computed_visibility: cs.visibility,
        composer_exists: !!composer,
        composer_enabled: composer ? (composer.getAttribute('contenteditable') === 'true') : false,
        search_box_exists: !!searchBox,
        conversation_title: titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : null,
    };
}
"""


async def _unknown_dialog_diagnostics(dialog) -> Optional[dict]:
    """DIAGNOSTIC ONLY — never affects control flow. Captures DOM ancestry to
    <body>, bounding rect, computed position/z-index/pointer-events/visibility,
    and page-level context (composer present/enabled, search box present,
    current conversation title) at the moment an UNKNOWN_DIALOG is captured, so
    future investigations don't require a follow-up production data pull."""
    try:
        return await dialog.evaluate(_UNKNOWN_DIALOG_DIAGNOSTICS_JS)
    except Exception as exc:
        logger.info("modals: unknown-dialog diagnostics capture failed: %s", exc)
        return None


async def _capture_dialog(page, dialog, context, title, body, reason) -> None:
    """Screenshot + dialog HTML + text -> whatsapp_dom_snapshots (forensics).
    For reason="unknown_dialog" only, also captures DIAGNOSTIC-ONLY ancestry/
    geometry/context data (see _unknown_dialog_diagnostics) — never affects
    detection or dismissal behavior."""
    shot = f"/tmp/dialog_{reason}.png"
    try:
        await page.screenshot(path=shot)
        logger.info("modals: screenshot saved %s", shot)
    except Exception as exc:
        logger.info("modals: screenshot failed: %s", exc)
    html = ""
    try:
        html = await dialog.evaluate("el => el.outerHTML")
    except Exception as exc:
        logger.info("modals: dialog HTML capture failed: %s", exc)

    diagnostics = None
    if reason == "unknown_dialog":
        diagnostics = await _unknown_dialog_diagnostics(dialog)
        if diagnostics:
            logger.info("modals: UNKNOWN_DIALOG diagnostics %s", json.dumps(diagnostics, ensure_ascii=False))

    doc = {
        "id": str(uuid.uuid4()),
        "reason": reason,
        "context": context,
        "dialog_title": title,
        "dialog_body": body,
        "url": getattr(page, "url", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "html_excerpt": (html or "")[:40000],
        "diagnostics": diagnostics,
    }
    try:
        await get_db().whatsapp_dom_snapshots.insert_one(doc)
        logger.info("modals: stored dialog snapshot (reason=%s) -> whatsapp_dom_snapshots", reason)
    except Exception as exc:
        logger.info("modals: snapshot store error=%s (screenshot still at %s)", exc, shot)


async def _gone(page, title: str) -> bool:
    """True if the dialog with this title is no longer visible (a different,
    stacked dialog counts as gone — the outer loop handles it next)."""
    await asyncio.sleep(SETTLE_SEC)
    still = await _visible_dialog(page)
    if still is None:
        return True
    new_title, _ = await _dialog_title_and_body(still)
    return _norm(new_title) != _norm(title)


async def _try_dismiss(page, dialog, title: str) -> Optional[str]:
    """Escape -> explicit Close/X -> whitelisted buttons. Returns the method
    that worked, or None. No force-clicks; only dialog-scoped controls."""
    try:
        await page.keyboard.press("Escape")
        if await _gone(page, title):
            return "escape"
    except Exception as exc:
        logger.info("modals: Escape attempt error=%s", exc)

    for sel in CLOSE_BUTTON_SELECTORS:
        try:
            btn = dialog.locator(sel)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click(timeout=3_000)
                if await _gone(page, title):
                    return f"close:{sel}"
        except Exception as exc:
            logger.info("modals: close attempt %s error=%s", sel, exc)

    for label in SAFE_BUTTON_LABELS:
        try:
            btn = dialog.get_by_role("button", name=label, exact=True)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click(timeout=3_000)
                if await _gone(page, title):
                    return f"button:{label}"
        except Exception as exc:
            logger.info("modals: safe-button %r attempt error=%s", label, exc)

    return None


async def dismiss_blocking_dialogs(page, context: str, max_dialogs: int = 3) -> bool:
    """Clear any blocking dialogs before an interaction.

    Returns True when no dialog remains. Returns False when an UNKNOWN dialog
    is present (captured, logged, untouched) or a recognized dialog could not
    be dismissed — the caller must fail gracefully (retryable, nothing sent).
    """
    for _ in range(max_dialogs):
        dialog = await _visible_dialog(page)
        if dialog is None:
            return True
        title, body = await _dialog_title_and_body(dialog)
        if not _is_recognized(title, body):
            _log_event(context, title, body, None, False, "UNKNOWN_DIALOG")
            await _capture_dialog(page, dialog, context, title, body, "unknown_dialog")
            return False
        method = await _try_dismiss(page, dialog, title)
        if method is None:
            _log_event(context, title, body, None, False, "DISMISS_FAILED")
            await _capture_dialog(page, dialog, context, title, body, "dialog_undismissable")
            return False
        _log_event(context, title, body, method, True, "DISMISSED")

    if await _visible_dialog(page) is None:
        return True
    _log_event(context, "", "", None, False, "TOO_MANY_DIALOGS")
    return False
