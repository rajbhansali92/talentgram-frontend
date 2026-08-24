"""
WhatsApp Worker — Playwright Session Manager

Responsibilities:
  - Launch Chromium with a persistent user-data-dir (Railway volume)
  - Detect whether already authenticated or QR scan needed
  - Capture QR code as base64 and write to MongoDB for UI display
  - Run heartbeat checks every HEARTBEAT_SEC seconds
  - Signal the worker loop when session is lost
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

import config
from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WhatsApp Web selectors — aria-label / data-testid are more stable than
# CSS classes which WhatsApp rotates frequently.
# ---------------------------------------------------------------------------
SEL = {
    "qr_canvas":    'canvas[aria-label="Scan this QR code to link a device"]',
    "qr_alt":       '[data-testid="qrcode"]',
    "chat_list":    '[data-testid="chat-list"]',
    "search_box":   '[data-testid="chat-list-search"]',
    "chat_title":   '[data-testid="cell-frame-title"]',
    "msg_box":      '[data-testid="conversation-compose-box-input"]',
    "send_btn":     '[data-testid="send"]',
    # 2026-08-24: "attach-menu-plus" no longer exists in WhatsApp Web's DOM
    # (confirmed via live read-only diagnostics) — the real button is
    # "plus-rounded" (aria-label="Attach"). The attach menu it opens no
    # longer carries any data-testid on its items at all (only
    # role="menuitem"/aria-label), so "attach_doc"/"attach_img" below are
    # stale and unused; sender.py now identifies the real file input via
    # Playwright's own expect_file_chooser() instead of a testid selector.
    "attach_btn":   '[data-testid="plus-rounded"]',
    "attach_doc":   '[data-testid="attach-document"]',
    "attach_img":   '[data-testid="attach-image-video"]',
    "loading_anim": '[data-testid="startup-screen-loading-animation"]',
}

# ---------------------------------------------------------------------------
# Login-state detection. WhatsApp Web rotates data-testid / class names, so we
# probe SEVERAL resilient candidates and require a POSITIVE match. We must never
# infer "logged in" from the absence of a QR — that misread a fresh QR screen as
# a "saved session" and then failed waiting for a stale chat-list selector.
# Ordered most-stable-first; the worker logs which candidate matched so a live
# run reveals the real DOM instead of guessing.
# ---------------------------------------------------------------------------
QR_SELECTORS = [
    'div[data-ref] canvas',                 # QR canvas wrapped in a data-ref div
    'canvas[aria-label*="Scan"]',           # aria-label on the QR canvas
    '[data-testid="qrcode"]',               # legacy
    'div[aria-label*="QR"]',
    'div[data-ref]',                        # the QR wrapper itself
]
LOGGED_IN_SELECTORS = [
    '#pane-side',                           # chat-list scroll pane (logged-in only; long-stable id)
    'div[aria-label="Chat list"]',
    'div[aria-label="Chats"]',
    '[data-testid="chat-list"]',            # legacy
    '#side',                                # left column container
    'header [data-icon="new-chat-outline"]',
]

# ---------------------------------------------------------------------------
# Diagnostics-only (2026-08-11): best-effort read of the connected account's
# OWN phone number, purely for the admin status panel — nothing in the
# message-processing pipeline reads this value; see _read_own_phone_number's
# docstring for why. These candidates are our best guess at WhatsApp Web's
# current own-profile trigger/panel — unlike QR_SELECTORS/LOGGED_IN_SELECTORS
# above (which are load-bearing and were verified against live behavior),
# these have NOT been verified against a live session. A miss here just
# means the diagnostic shows "unknown" — it cannot affect authentication or
# message processing either way.
PROFILE_TRIGGER_SELECTORS = [
    '[data-testid="menu-bar-avatar"]',
    'header [data-testid="avatar-image"]',
    'header [aria-label="Profile"]',
    'header img[draggable="false"]',
]
PROFILE_PANEL_SELECTORS = [
    '[data-testid="drawer-right"]',
    'div[data-animate-drawer-title]',
]
_PHONE_LIKE_RE = re.compile(r"\+\d[\d\s\-()]{7,}\d")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PHASE 26B — TEMPORARY chat-discovery investigation instrumentation.
# Rolling buffers of recent browser console messages / failed network
# requests, so a CHAT_NOT_OPENED failure capture (sender.py) can include
# "console errors" / "network errors" without a live remote-debugging
# session. Read-only logging support — registering these listeners does not
# change any WhatsApp Web interaction. Safe to delete this block (and the
# listener registration in start()) once the investigation is closed.
# ---------------------------------------------------------------------------
_P26B_CONSOLE_BUFFER: deque = deque(maxlen=50)
_P26B_NETWORK_ERROR_BUFFER: deque = deque(maxlen=50)


def get_recent_console_errors() -> list:
    """Console messages of type error/warning seen in the last ~50 events."""
    return [m for m in _P26B_CONSOLE_BUFFER if m.get("type") in ("error", "warning")]


def get_recent_network_errors() -> list:
    """Failed network requests (requestfailed) seen in the last ~50 events."""
    return list(_P26B_NETWORK_ERROR_BUFFER)


def _p26b_register_listeners(page: Page) -> None:
    def _on_console(msg) -> None:
        try:
            _P26B_CONSOLE_BUFFER.append(
                {"type": msg.type, "text": msg.text[:300], "time": _utcnow()}
            )
        except Exception:
            pass

    def _on_request_failed(req) -> None:
        try:
            _P26B_NETWORK_ERROR_BUFFER.append(
                {"url": req.url[:200], "failure": str(req.failure), "time": _utcnow()}
            )
        except Exception:
            pass

    page.on("console", _on_console)
    page.on("requestfailed", _on_request_failed)


class WhatsAppSession:
    """
    Manages a single persistent Playwright / WhatsApp Web session.

    Usage:
        session = WhatsAppSession()
        await session.start()          # launches browser, handles QR if needed
        page = session.page            # ready-to-use authenticated page
        await session.heartbeat()      # call periodically to verify still connected
        await session.stop()           # clean shutdown
    """

    def __init__(self) -> None:
        self._pw = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._healthy: bool = False
        # Shared between outbound sending (worker.py's job loop) and the
        # inbound Agent Platform listener (inbound.py) — both drive page
        # navigation on the same single Page, so every page-touching
        # operation from either side must hold this lock. Created once and
        # reused across start()/stop() restarts (unlike `page`, which gets
        # replaced on restart) so callers holding a reference to the
        # session never need to re-fetch the lock.
        self.page_lock: asyncio.Lock = asyncio.Lock()
        # Reconnect reliability (2026-08-11) — `generation` increments every
        # time `_authenticate()` succeeds, first boot AND every subsequent
        # re-auth (e.g. the account was switched via a fresh QR scan).
        # inbound.py polls this each cycle and rebuilds every account-
        # specific cache/global the moment it changes — this counter is the
        # ONLY signal that connects "an authentication just happened" to
        # "account-specific state must be rebuilt", so nothing downstream
        # ever needs to know or assume WHICH account is now authenticated.
        # session_id is a fresh opaque id regenerated alongside it, purely
        # for diagnostics (lets an operator see in the admin UI that a
        # reconnect actually took effect, distinct from the process just
        # sitting on the same session).
        self.generation: int = 0
        self.session_id: str = ""
        # Diagnostics-only (2026-08-11) — best-effort, may be None. Never
        # read by any matching/routing/direction-detection logic anywhere
        # in this codebase; see _read_own_phone_number's docstring.
        self.own_phone_number: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch browser. Handles QR auth if no saved session exists."""
        Path(config.SESSION_DIR).mkdir(parents=True, exist_ok=True)

        await self._update_session_doc("qr_pending", error_message=None)

        self._pw = await async_playwright().start()

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=config.SESSION_DIR,
            headless=True,
            # Media-Assignment album-tile download (2026-08-23) — needed so
            # page.expect_download() can capture WhatsApp Web's native
            # right-click "Download" action; explicit rather than relying
            # on Playwright's own default, which has varied across
            # versions. No effect on anything else this session does.
            accept_downloads=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
            ],
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        self.page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

        _p26b_register_listeners(self.page)  # PHASE 26B — temporary, logging only

        await self._authenticate()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._healthy = False
        # Diagnostics only — the account may be different by the time we
        # come back up (start() -> _authenticate() re-derives this and
        # bumps generation); showing a stale phone number while genuinely
        # disconnected would be actively misleading.
        self.own_phone_number = None
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception as exc:
            logger.warning("session.stop error: %s", exc)
        finally:
            self._context = None
            self.page = None
            self._pw = None
        await self._update_session_doc("disconnected")
        logger.info("session: stopped")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        """Detect login state from POSITIVE signals only.

        Never infer "logged in" from the absence of a QR (the previous logic
        did, which misread a fresh QR screen as a saved session). We poll the
        live page for either a logged-in signal (chat list) or a QR screen,
        probing several resilient selectors and logging exactly what is present
        so the real DOM is visible in the worker logs.
        """
        logger.info("session: navigating to WhatsApp Web…")
        await self.page.goto(config.WHATSAPP_URL, wait_until="domcontentloaded")

        overall_deadline = time.monotonic() + (config.PAGE_LOAD_TIMEOUT_MS / 1000)
        matched_login = None

        while time.monotonic() < overall_deadline:
            await asyncio.sleep(2)
            await self._log_page_state("detect")

            matched_login = await self._probe("logged-in", LOGGED_IN_SELECTORS)
            if matched_login:
                logger.info("session: authenticated state detected via %s", matched_login)
                break

            qr_sel = await self._probe("qr", QR_SELECTORS)
            if qr_sel:
                logger.info("session: QR code detected via %s — capturing for UI…", qr_sel)
                await self._capture_and_store_qr(qr_sel)
                matched_login = await self._wait_for_login_after_qr()
                if matched_login:
                    break
                await self._update_session_doc(
                    "error", error_message="QR scan timed out. Restart worker."
                )
                raise RuntimeError("QR scan timed out")

            logger.info("session: neither QR nor chat-list visible yet — page still loading, retrying…")

        if not matched_login:
            # Dump the live DOM so the real selectors are visible, then fail
            # loudly instead of silently mislabelling the state.
            await self._log_page_state("FAILED-no-signal", body=True)
            await self._update_session_doc(
                "error",
                error_message=(
                    "Could not detect a QR or chat-list element. WhatsApp Web DOM "
                    "may have changed — check worker logs for the live selectors."
                ),
            )
            raise RuntimeError(
                "WhatsApp Web: neither QR nor chat-list detected (see instrumented logs)"
            )

        await asyncio.sleep(2)  # let chats hydrate
        # Sweep post-login dialogs (feature announcements etc.) so the first
        # job doesn't hit a blocked chat list. Best-effort: an unknown dialog
        # is captured+logged by the framework and each send fails gracefully.
        try:
            from modals import dismiss_blocking_dialogs
            await dismiss_blocking_dialogs(self.page, "login")
        except Exception as exc:
            logger.warning("session: post-login dialog sweep failed: %s", exc)

        # Reconnect reliability (2026-08-11) — this fires on EVERY successful
        # authentication, first boot and every subsequent re-auth alike. The
        # generation bump is what lets inbound.py notice "an authentication
        # just happened" and rebuild every account-specific cache/global —
        # see inbound_listener_loop's re-arm logic. Order matters: bump
        # generation/session_id and persist BEFORE setting self._healthy so
        # a concurrent heartbeat/poll cycle never observes "healthy" without
        # the new generation already being visible.
        self.generation += 1
        self.session_id = uuid.uuid4().hex[:12]
        self.own_phone_number = await self._read_own_phone_number()
        self._healthy = True
        await self._update_session_doc(
            "authenticated",
            extra={
                "authenticated_at": _utcnow(),
                "qr_code_base64": None,
                "qr_expires_at": None,
                "generation": self.generation,
                "session_id": self.session_id,
                "connected_phone_number": self.own_phone_number,
            },
        )
        logger.info(
            "session: ready ✅ generation=%d session_id=%s connected_phone_number=%s",
            self.generation, self.session_id, self.own_phone_number or "unknown",
        )

    async def _read_own_phone_number(self) -> Optional[str]:
        """Best-effort, diagnostics-ONLY read of the connected account's own
        phone number from WhatsApp Web's own profile panel — displayed in the
        admin status panel so an operator can see at a glance which account
        is currently linked. Deliberately never used by any matching/
        routing/direction-detection logic anywhere in this codebase: the
        whole point of the reconnect fix this belongs to is that the
        pipeline must not depend on which account is authenticated, so this
        stays purely decorative. Any failure (trigger not found, panel
        didn't open, no phone-shaped text inside it, timeout) is swallowed
        and logged at INFO — returns None, never raises, never blocks
        authentication. Scoped to read text only from within the opened
        panel (not the whole page), so a miss degrades to "unknown" rather
        than ever risking picking up an unrelated contact's number from the
        visible chat list."""
        if not self.page:
            return None
        async with self.page_lock:
            try:
                trigger = None
                for sel in PROFILE_TRIGGER_SELECTORS:
                    loc = self.page.locator(sel)
                    if await loc.count() and await loc.first.is_visible():
                        trigger = loc.first
                        break
                if trigger is None:
                    logger.info("session: own-phone-number read skipped — no profile trigger found "
                                "(diagnostics only, not fatal)")
                    return None
                await trigger.click(timeout=3000)
                panel = None
                for sel in PROFILE_PANEL_SELECTORS:
                    loc = self.page.locator(sel)
                    try:
                        await loc.first.wait_for(state="visible", timeout=2000)
                        panel = loc.first
                        break
                    except PlaywrightTimeoutError:
                        continue
                phone = None
                if panel is not None:
                    panel_text = await panel.inner_text()
                    m = _PHONE_LIKE_RE.search(panel_text)
                    if m:
                        phone = m.group(0).strip()
                await self.page.keyboard.press("Escape")
                if phone:
                    logger.info("session: own phone number read from profile panel")
                else:
                    logger.info("session: own-phone-number read — panel did not yield a phone-shaped "
                                "value (diagnostics only, not fatal)")
                return phone
            except Exception as exc:
                logger.info("session: own-phone-number read failed (non-fatal, diagnostics only): %s", exc)
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                return None

    async def _wait_for_login_after_qr(self) -> Optional[str]:
        """After a QR is shown, poll for a logged-in signal and refresh the
        stored QR as WhatsApp rotates it. Returns the matched selector or None."""
        deadline = time.monotonic() + (config.QR_SCAN_TIMEOUT_MS / 1000)
        logger.info("session: waiting up to %ds for QR scan…",
                    config.QR_SCAN_TIMEOUT_MS // 1000)
        last_qr_refresh = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            matched = await self._probe("logged-in", LOGGED_IN_SELECTORS)
            if matched:
                logger.info("session: QR scanned — authenticated via %s!", matched)
                return matched
            # WhatsApp rotates the QR ~every 20s; refresh the stored image.
            if time.monotonic() - last_qr_refresh > 20:
                qr_sel = await self._probe("qr", QR_SELECTORS)
                if qr_sel:
                    await self._capture_and_store_qr(qr_sel)
                    last_qr_refresh = time.monotonic()
        return None

    async def _probe(self, label: str, selectors: list) -> Optional[str]:
        """Return the first present+visible selector, logging every candidate's
        count/visibility so a live run reveals the real DOM (no guessing)."""
        found: Optional[str] = None
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                count = await loc.count()
                visible = False
                if count:
                    try:
                        visible = await loc.first.is_visible()
                    except Exception:
                        visible = False
                logger.info("session: probe[%s] %-34s count=%d visible=%s",
                            label, sel, count, visible)
                if visible and found is None:
                    found = sel
            except Exception as exc:
                logger.info("session: probe[%s] %-34s error=%s", label, sel, exc)
        return found

    async def _log_page_state(self, tag: str, body: bool = False) -> None:
        """Instrumentation: log live url/title (and optionally a body snippet)
        so the actual WhatsApp Web screen is visible in the worker logs."""
        try:
            url = self.page.url
            title = await self.page.title()
            logger.info("session: [%s] url=%s title=%r", tag, url, title)
            if body:
                try:
                    text = (await self.page.inner_text("body"))[:600]
                except Exception:
                    text = "<unavailable>"
                logger.info("session: [%s] body[:600]=%r", tag, text)
        except Exception as exc:
            logger.info("session: [%s] page-state error=%s", tag, exc)

    async def _capture_and_store_qr(self, qr_selector: Optional[str] = None) -> None:
        """Screenshot the QR (matched element if known, else full page) and
        store as base64 in MongoDB for the admin UI."""
        screenshot_bytes = None
        if qr_selector:
            try:
                screenshot_bytes = await self.page.locator(qr_selector).first.screenshot(type="png")
            except Exception as exc:
                logger.info("session: QR element screenshot failed (%s) — using full page", exc)
        if screenshot_bytes is None:
            screenshot_bytes = await self.page.screenshot(type="png", full_page=False)

        qr_b64 = "data:image/png;base64," + base64.b64encode(screenshot_bytes).decode()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()

        await self._update_session_doc(
            "qr_pending",
            extra={"qr_code_base64": qr_b64, "qr_expires_at": expires_at},
        )
        logger.info("session: QR stored in DB (expires in 90s)")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(self) -> bool:
        """
        Verify WhatsApp Web is still on the chat view.
        Returns True if healthy, False if session is lost.
        """
        if not self.page:
            self._healthy = False
            return False
        try:
            matched = await self._probe("heartbeat", LOGGED_IN_SELECTORS)
            if matched:
                self._healthy = True
                await get_db().whatsapp_sessions.update_one(
                    {"id": "default"},
                    {"$set": {"last_heartbeat": _utcnow()}},
                    upsert=True,
                )
                return True
            else:
                logger.warning("session: heartbeat FAILED — no chat-list signal")
                self._healthy = False
                await self._update_session_doc(
                    "error", error_message="Session lost — chat list not visible"
                )
                return False
        except Exception as exc:
            logger.warning("session: heartbeat exception: %s", exc)
            self._healthy = False
            await self._update_session_doc("error", error_message=str(exc))
            return False

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _update_session_doc(
        self,
        status: str,
        error_message: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """Upsert the singleton session document in MongoDB."""
        try:
            updates: dict = {
                "status": status,
                "error_message": error_message,
                "last_heartbeat": _utcnow(),
            }
            if extra:
                updates.update(extra)
            await get_db().whatsapp_sessions.update_one(
                {"id": "default"},
                {"$set": updates},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("session: failed to update session doc: %s", exc)
