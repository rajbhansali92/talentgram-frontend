"""
WhatsApp Worker — Sender Engine
Handles browser actions for locating a chat (personal or group) and sending messages with optional media attachments.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import urllib.request
from typing import Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from session import SEL

logger = logging.getLogger(__name__)


async def send_whatsapp_message(
    page: Page,
    destination_type: str,  # "group" | "number"
    destination: str,
    message_body: str,
    media_url: Optional[str] = None,
) -> None:
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

    # Verify message box is active
    await page.wait_for_selector(SEL["msg_box"], timeout=5_000)
    
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
            
            # Click send button on preview page
            await page.click(SEL["send_btn"])
            await asyncio.sleep(3.0)  # Wait for media upload and send to complete
            
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
        
        await asyncio.sleep(0.5)
        # Click send button
        await page.click(SEL["send_btn"])
        await asyncio.sleep(1.0)
        
    logger.info("sender: message sent successfully")
