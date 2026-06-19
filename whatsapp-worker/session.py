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
import time
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
    "attach_btn":   '[data-testid="attach-menu-plus"]',
    "attach_doc":   '[data-testid="attach-document"]',
    "attach_img":   '[data-testid="attach-image-video"]',
    "loading_anim": '[data-testid="startup-screen-loading-animation"]',
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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

        await self._authenticate()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._healthy = False
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
        """Navigate to WhatsApp Web, handle QR or reuse saved session."""
        logger.info("session: navigating to WhatsApp Web…")
        await self.page.goto(config.WHATSAPP_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check if QR is displayed (not logged in)
        qr_visible = False
        try:
            qr_visible = (
                await self.page.is_visible(SEL["qr_canvas"], timeout=6_000)
                or await self.page.is_visible(SEL["qr_alt"], timeout=3_000)
            )
        except PlaywrightTimeoutError:
            pass

        if qr_visible:
            logger.info("session: QR code detected — capturing for UI…")
            await self._capture_and_store_qr()
            logger.info("session: waiting up to %ds for QR scan…",
                        config.QR_SCAN_TIMEOUT_MS // 1000)
            try:
                await self.page.wait_for_selector(
                    SEL["chat_list"],
                    timeout=config.QR_SCAN_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                await self._update_session_doc(
                    "error", error_message="QR scan timed out. Restart worker."
                )
                raise RuntimeError("QR scan timed out")

            logger.info("session: QR scanned — authenticated!")
        else:
            logger.info("session: saved session detected — waiting for chat list…")
            try:
                await self.page.wait_for_selector(
                    SEL["chat_list"],
                    timeout=config.PAGE_LOAD_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                await self._update_session_doc(
                    "error", error_message="Chat list did not load. Session may be invalid."
                )
                raise RuntimeError("WhatsApp Web failed to load chat list")

        await asyncio.sleep(2)  # let chats hydrate
        self._healthy = True
        now = _utcnow()
        await self._update_session_doc(
            "authenticated",
            extra={"authenticated_at": now, "qr_code_base64": None, "qr_expires_at": None},
        )
        logger.info("session: ready ✅")

    async def _capture_and_store_qr(self) -> None:
        """Take a screenshot of the QR canvas and store as base64 in MongoDB."""
        try:
            # Try to screenshot the QR canvas element directly
            qr_el = self.page.locator(SEL["qr_canvas"]).first
            screenshot_bytes = await qr_el.screenshot(type="png")
        except Exception:
            # Fallback: full-page screenshot
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
            visible = await self.page.is_visible(SEL["chat_list"], timeout=8_000)
            if visible:
                self._healthy = True
                await get_db().whatsapp_sessions.update_one(
                    {"id": "default"},
                    {"$set": {"last_heartbeat": _utcnow()}},
                    upsert=True,
                )
                return True
            else:
                logger.warning("session: heartbeat FAILED — chat list not visible")
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
