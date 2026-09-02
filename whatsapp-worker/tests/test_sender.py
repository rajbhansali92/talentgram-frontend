"""Regression tests for sender.py's media-attach mechanism (2026-08-24).

Live read-only diagnostics against a real disposable WhatsApp group proved
that data-testid="attach-menu-plus" (the old SEL["attach_btn"] target) no
longer exists anywhere in the DOM. The real attach button is
data-testid="plus-rounded", and the menu it opens ("Document" / "Photos &
videos" / "Camera" / ...) carries NO data-testid at all on its items — only
role="menuitem" + aria-label. Clicking "Photos & videos" (via Playwright's
own page.expect_file_chooser(), the correct non-synthetic mechanism for
this) resolves to the real input, whose accept attribute
("image/*,video/mp4,video/3gpp,video/quicktime,video/webm,video/x-matroska")
covers both photos and videos in one control — see sender.py's attach block
and session.py's SEL["attach_btn"] comment for the full diagnostic trail.

These tests never touch a real browser: they replace every sender.py helper
send_whatsapp_message calls before/after the attach block with a fixed-
result fake (same style as test_group_routing.py), and give it a FakePage
whose expect_file_chooser() mimics Playwright's real async-context-manager
contract closely enough to prove the attach sequence itself is correct.

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_sender.py -q
"""
import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest  # noqa: E402

import sender  # noqa: E402


# --- Neutralize every non-attach step of send_whatsapp_message ---------------
async def _true(*a, **k):
    return True


async def _noop(*a, **k):
    return None


async def _opened(*a, **k):
    return "OPENED"


async def _chat_open(*a, **k):
    return True, True, True, "Talentgram Casting Test"


async def _resolve_scope(*a, **k):
    return "#main"


async def _baselines(*a, **k):
    return {}


async def _not_already_delivered(*a, **k):
    return False, None


async def _sent_via(*a, **k):
    return "fake_sent_via"


async def _verified_delivery(*a, **k):
    return True, True, "[data-testid=\"msg-outgoing\"]", "fake_msg_id"


sender.dismiss_blocking_dialogs = _true
# Saved BEFORE the fake overwrite below — the _open_group_chat tests
# (2026-08-25, near the end of this file) restore the REAL implementation
# for their duration, since they exercise its actual bounded-polling and
# post-click-verification logic directly.
_REAL_OPEN_GROUP_CHAT = sender._open_group_chat
sender._open_group_chat = _opened
sender._wait_for_chat_ready = _noop
sender._p26b_dump = _noop
sender._verify_chat_open = _chat_open
sender._resolve_scope = _resolve_scope
sender._snapshot_msg_baselines = _baselines
sender._dump_send_dom = _noop
sender._already_delivered = _not_already_delivered
# Saved BEFORE the fake overwrite below — the strict_send_confirmation
# tests (2026-08-24, near the end of this file) restore the REAL
# _find_and_click_send for their duration, since they exercise its
# actual selector-matching/Enter-fallback logic directly.
_REAL_FIND_AND_CLICK_SEND = sender._find_and_click_send
sender._find_and_click_send = _sent_via
sender._safe_screenshot = _noop
sender._dump_outgoing_dom = _noop
sender._verify_delivery = _verified_delivery
sender.asyncio.sleep = _noop


class _FakeLocator:
    async def count(self):
        return 1


class _FakeCaptionLocator:
    """Locator for one CAPTION_INPUT_SELECTORS entry — count/visible are
    set per-selector by FakePage so a test can control exactly which
    selector in the fallback chain "exists" on the fake preview screen."""
    def __init__(self, count, visible, on_click=None):
        self._count = count
        self._visible = visible
        self._on_click = on_click

    async def count(self):
        return self._count

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._visible

    async def click(self, timeout=None):
        if self._on_click:
            self._on_click()


class _FakeKeyboard:
    def __init__(self, log=None):
        self.typed = []
        self.pressed = []
        self._log = log

    async def type(self, text):
        self.typed.append(text)
        if self._log is not None:
            self._log.append(("keyboard_type", text))

    async def down(self, key):
        pass

    async def up(self, key):
        pass

    async def press(self, key):
        self.pressed.append(key)
        if self._log is not None:
            self._log.append(("keyboard_press", key))


class _FakeFileChooserElement:
    def __init__(self, accept):
        self._accept = accept

    async def get_attribute(self, name):
        return self._accept if name == "accept" else None

    async def is_visible(self):
        return False


class _FakeFileChooser:
    """Mirrors Playwright's real FileChooser API surface used by sender.py."""
    def __init__(self, accept="image/*,video/mp4,video/3gpp,video/quicktime,video/webm,video/x-matroska"):
        self.element = _FakeFileChooserElement(accept)
        self.set_files_calls = []

    def is_multiple(self):
        return True

    async def set_files(self, path):
        self.set_files_calls.append(path)


class _FileChooserCtx:
    """Mirrors page.expect_file_chooser()'s async-context-manager contract:
    `async with page.expect_file_chooser() as fc_info: <action that opens
    it>` then `file_chooser = await fc_info.value`."""
    def __init__(self, page, *, should_raise=False):
        self._page = page
        self._should_raise = should_raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def value(self):
        if self._should_raise:
            async def _raise():
                raise sender.PlaywrightTimeoutError("no file chooser event fired")
            return _raise()

        async def _resolve():
            chooser = _FakeFileChooser()
            self._page.file_choosers.append(chooser)
            return chooser
        return _resolve()


class FakePage:
    def __init__(self, *, chooser_should_raise=False, attach_click_should_raise=False,
                 caption_selector_that_matches="__default__",
                 send_button_selector_that_matches="__default__"):
        """`caption_selector_that_matches`: which entry of
        sender.CAPTION_INPUT_SELECTORS "exists and is visible" on this fake
        preview screen. "__default__" (the sentinel, not a real selector)
        means the CURRENT real one (index 0) — matching the fixed
        production behavior. Pass a specific selector string to simulate
        only the legacy fallback matching, or None to simulate neither
        matching at all (no caption input found anywhere).

        `send_button_selector_that_matches`: same idea for
        sender.SEND_BUTTON_SELECTORS — "__default__" means the confirmed-
        live entry (index 0) matches, matching the fixed production
        behavior. Pass None to simulate NO selector matching (the real
        2026-08-24 bug scenario that used to fall through to Enter)."""
        self.action_log = []
        self.clicks = []
        self.keyboard = _FakeKeyboard(log=self.action_log)
        self.file_choosers = []
        self.evaluate_calls = []
        self.caption_clicks = []
        self._chooser_should_raise = chooser_should_raise
        self._attach_click_should_raise = attach_click_should_raise
        self._caption_selector_that_matches = (
            sender.CAPTION_INPUT_SELECTORS[0]
            if caption_selector_that_matches == "__default__"
            else caption_selector_that_matches
        )
        self._send_button_selector_that_matches = (
            sender.SEND_BUTTON_SELECTORS[0]
            if send_button_selector_that_matches == "__default__"
            else send_button_selector_that_matches
        )
        self.send_button_clicks = []

    async def click(self, selector, timeout=None):
        if self._attach_click_should_raise and selector == sender.SEL["attach_btn"]:
            raise sender.PlaywrightTimeoutError(
                f'Page.click: Timeout {timeout or 30000}ms exceeded.\n'
                f'  - element intercepts pointer events'
            )
        self.clicks.append(selector)
        self.action_log.append(("page_click", selector))

    def expect_file_chooser(self, timeout=None):
        return _FileChooserCtx(self, should_raise=self._chooser_should_raise)

    def locator(self, sel):
        if sel in sender.CAPTION_INPUT_SELECTORS:
            matches = sel == self._caption_selector_that_matches

            def _on_caption_click(_sel=sel):
                self.caption_clicks.append(_sel)
                self.action_log.append(("caption_click", _sel))
            return _FakeCaptionLocator(count=1 if matches else 0, visible=matches, on_click=_on_caption_click if matches else None)
        if sel in sender.SEND_BUTTON_SELECTORS:
            matches = sel == self._send_button_selector_that_matches

            def _on_send_click(_sel=sel):
                self.send_button_clicks.append(_sel)
                self.action_log.append(("send_button_click", _sel))
            return _FakeCaptionLocator(count=1 if matches else 0, visible=matches, on_click=_on_send_click if matches else None)
        return _FakeLocator()

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append((js, arg))
        return {
            "attach_button": {"testid": "plus-rounded", "visible": True},
            "click_point": {"x": 614, "y": 762},
            "element_from_point": {"tag": "DIV", "testid": "some-overlay"},
            "elements_from_point": [{"tag": "DIV", "testid": "some-overlay"}],
            "overlays_dialogs_menus": [],
            "fixed_or_absolute_intersecting_attach": [],
            "active_element": None,
            "chat_title": "Talentgram Casting Test",
            "url": "https://web.whatsapp.com/",
            "scroll": {"x": 0, "y": 0},
            "viewport": {"w": 1280, "h": 800},
        }


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _real_temp_file(suffix):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(b"fake media bytes")
    return path


# --- 1/2: JPEG and MP4 attach identically through the real menu click -------
def test_jpeg_attaches_via_photos_videos_menu():
    page = FakePage()
    path = _real_temp_file(".jpg")
    try:
        result = run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Take 1", local_file_path=path,
        ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        assert page.clicks[0] == sender.SEL["attach_btn"]
        assert 'button[aria-label="Photos & videos"]' in page.clicks
        assert len(page.file_choosers) == 1
        assert page.file_choosers[0].set_files_calls == [path]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_mp4_attaches_via_the_same_menu_no_type_branching():
    page = FakePage()
    path = _real_temp_file(".mp4")
    try:
        result = run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Take 1", local_file_path=path,
        ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        # Same exact click sequence as the JPEG case — the real "Photos &
        # videos" input accepts both, so there is no media-type branching
        # in the attach code path at all.
        assert page.clicks[0] == sender.SEL["attach_btn"]
        assert 'button[aria-label="Photos & videos"]' in page.clicks
        assert page.file_choosers[0].set_files_calls == [path]
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- 3: local_file_path is never deleted by sender.py itself ----------------
def test_local_file_path_never_deleted_on_success():
    page = FakePage()
    path = _real_temp_file(".jpg")
    try:
        run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="caption", local_file_path=path,
        ))
        assert os.path.exists(path), "sender.py must never delete a caller-owned local_file_path"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_local_file_path_never_deleted_even_on_attach_failure():
    page = FakePage(chooser_should_raise=True)
    path = _real_temp_file(".jpg")
    try:
        with pytest.raises(Exception):
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="caption", local_file_path=path,
            ))
        assert os.path.exists(path), "a failed attach must still never delete the caller's file"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- 4: a failed attachment never reports a sent state ----------------------
def test_failed_attachment_raises_rather_than_reporting_sent():
    page = FakePage(chooser_should_raise=True)
    path = _real_temp_file(".mp4")
    try:
        with pytest.raises(sender.PlaywrightTimeoutError):
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="caption", local_file_path=path,
            ))
        # The click sequence still happens (menu opens, "Photos & videos" is
        # clicked) — the failure is that no file chooser EVENT ever resolved
        # (e.g. WhatsApp's UI didn't respond), so no file was ever attached.
        assert 'button[aria-label="Photos & videos"]' in page.clicks
        assert page.file_choosers == [], "a failed chooser must never produce an attached file"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- bonus: media_url (URL-download) ownership is unaffected by this fix ----
def test_media_url_path_still_deletes_its_own_temp_file(monkeypatch):
    """Regression guard: the attach-mechanism fix only replaced the click/
    file-input lines, not the surrounding owns_temp_file ownership logic —
    a media_url download must still clean up after itself."""
    import urllib.request

    class _FakeResponse:
        def read(self):
            return b"downloaded bytes"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req: _FakeResponse())

    page = FakePage()
    result = run(sender.send_whatsapp_message(
        page=page, destination_type="group", destination="Talentgram Casting Test",
        message_body="caption", media_url="https://example.com/photo.jpg",
    ))
    assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
    assert len(page.file_choosers) == 1
    sent_path = page.file_choosers[0].set_files_calls[0]
    assert not os.path.exists(sent_path), "sender.py must delete a media_url temp file it downloaded itself"


# --- diagnostic helper: fires on attach-click failure, never changes flow ---
def test_attach_click_failure_captures_diagnostics_then_reraises_unchanged():
    """2026-08-24: when the real attach-button click itself times out
    ("element intercepts pointer events"), sender.py must capture a live
    DOM diagnostic (via page.evaluate) BEFORE re-raising the exact same
    exception — never swallowed, never retried, never replaced."""
    page = FakePage(attach_click_should_raise=True)
    path = _real_temp_file(".jpg")
    try:
        with pytest.raises(sender.PlaywrightTimeoutError) as exc_info:
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="Ahana Test — Google Test Take 1", local_file_path=path,
                diagnostic_meta={"item": "1/6", "source_media_type": "video"},
            ))
        assert "intercepts pointer events" in str(exc_info.value)
        # The diagnostic capture ran exactly once (the failure-triggered
        # page.evaluate call) and nothing was ever attached or sent.
        assert len(page.evaluate_calls) == 1
        assert page.file_choosers == []
        assert 'button[aria-label="Photos & videos"]' not in page.clicks
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_attach_click_failure_diagnostic_capture_itself_failing_does_not_mask_error():
    """A bug inside the diagnostic capture (e.g. page.evaluate itself
    raising) must never replace or hide the real click failure it was
    trying to explain."""
    class _BrokenEvaluatePage(FakePage):
        async def evaluate(self, js, arg=None):
            raise RuntimeError("diagnostic capture blew up")

    page = _BrokenEvaluatePage(attach_click_should_raise=True)
    path = _real_temp_file(".jpg")
    try:
        with pytest.raises(sender.PlaywrightTimeoutError) as exc_info:
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="caption", local_file_path=path,
            ))
        assert "intercepts pointer events" in str(exc_info.value)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_attach_click_failure_without_diagnostic_meta_still_works():
    """diagnostic_meta is optional — every existing caller (campaign sends
    via media_url) that never passes it must behave identically."""
    page = FakePage(attach_click_should_raise=True)
    path = _real_temp_file(".mp4")
    try:
        with pytest.raises(sender.PlaywrightTimeoutError):
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="caption", local_file_path=path,
            ))
        assert len(page.evaluate_calls) == 1
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- caption fix (2026-08-24): media-caption-input-container -----------------
def test_caption_types_via_the_confirmed_live_selector():
    """The real selector confirmed via a live diagnostic-only attach+inspect
    run: data-testid="media-caption-input-container". Default FakePage
    config simulates exactly this — the fixed production behavior."""
    page = FakePage()
    path = _real_temp_file(".jpg")
    try:
        result = run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Take 1", local_file_path=path,
        ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        assert page.caption_clicks == [sender.CAPTION_INPUT_SELECTORS[0]]
        assert page.keyboard.typed == ["Ahana Test — Google Test Take 1"]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_caption_falls_back_to_legacy_selector_if_it_ever_matches_again():
    """CAPTION_INPUT_SELECTORS keeps the old xpath as a last-resort fallback
    — if some WhatsApp Web build ever renders it again, the caption should
    still be typed rather than silently dropped."""
    legacy_xpath = sender.CAPTION_INPUT_SELECTORS[1]
    page = FakePage(caption_selector_that_matches=legacy_xpath)
    path = _real_temp_file(".jpg")
    try:
        run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Take 2", local_file_path=path,
        ))
        assert page.caption_clicks == [legacy_xpath]
        assert page.keyboard.typed == ["Ahana Test — Google Test Take 2"]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_caption_missing_never_blocks_the_send():
    """The real 2026-08-24 bug: when NO caption selector matches, the send
    must still proceed (matching the pre-existing "never block a send over
    a caption failure" design) — it just goes out without a caption,
    logged, not silently pretending to have typed one."""
    page = FakePage(caption_selector_that_matches=None)
    path = _real_temp_file(".jpg")
    try:
        result = run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Take 3", local_file_path=path,
        ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        assert page.caption_clicks == []
        assert page.keyboard.typed == []
        # send still proceeds — attach + "Photos & videos" + set_files all happened.
        assert len(page.file_choosers) == 1
        assert 'button[aria-label="Photos & videos"]' in page.clicks
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_caption_prefers_new_selector_over_legacy_when_both_match():
    """If both selectors happen to match, the confirmed-live one (index 0)
    must be tried first — it's listed first in CAPTION_INPUT_SELECTORS for
    exactly this reason."""
    class _BothMatchPage(FakePage):
        def locator(self, sel):
            if sel in sender.CAPTION_INPUT_SELECTORS:
                return _FakeCaptionLocator(
                    count=1, visible=True,
                    on_click=(lambda _sel=sel: self.caption_clicks.append(_sel)),
                )
            return super().locator(sel)

    page = _BothMatchPage()
    path = _real_temp_file(".jpg")
    try:
        run(sender.send_whatsapp_message(
            page=page, destination_type="group", destination="Talentgram Casting Test",
            message_body="Ahana Test — Google Test Intro", local_file_path=path,
        ))
        assert page.caption_clicks == [sender.CAPTION_INPUT_SELECTORS[0]]
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- strict_send_confirmation (2026-08-24): SEND's own send-click safety ---
# Live diagnostics on the real media-preview screen (send_button_preview_diagnostic,
# never sent anything) confirmed the real Send control: a
# div[role="button"][aria-label^="Send"] (aria-label reflects selection
# count, e.g. "Send 1 selected") wrapping a
# span[data-icon="wds-ic-send-filled"] icon — both now the first two
# entries in SEND_BUTTON_SELECTORS. Every pre-existing entry returned
# count=0 during a real SEND, which fell through to a blind Enter
# keypress — unsafe, since a cleared composer is not proof of submission.
#
# These tests need the REAL _find_and_click_send (the earlier attach/
# caption tests above replaced it with a fixed-result fake to isolate
# THOSE tests from send-click behavior entirely) — restored for exactly
# the duration of each test below via this context manager.
@contextlib.contextmanager
def _real_send_click():
    sender._find_and_click_send = _REAL_FIND_AND_CLICK_SEND
    try:
        yield
    finally:
        sender._find_and_click_send = _sent_via


def test_send_control_identified_and_clicked_correctly():
    """1/2: the current media-preview Send control is identified via the
    confirmed-live selector and is the ONLY thing clicked — no Enter."""
    page = FakePage()
    path = _real_temp_file(".jpg")
    try:
        with _real_send_click():
            result = run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="Ahana Test — Google Test Take 1", local_file_path=path,
                strict_send_confirmation=True,
            ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        assert page.send_button_clicks == [sender.SEND_BUTTON_SELECTORS[0]]
        assert "Enter" not in page.keyboard.pressed
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_strict_mode_never_falls_back_to_enter():
    """3: under strict_send_confirmation, when no send-control selector
    matches, Enter must NEVER be pressed — this is the exact real bug
    (blind Enter treated as a send)."""
    page = FakePage(send_button_selector_that_matches=None)
    path = _real_temp_file(".jpg")
    try:
        with _real_send_click():
            result = run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="Ahana Test — Google Test Take 2", local_file_path=path,
                strict_send_confirmation=True,
            ))
        assert result["state"] == sender.MESSAGE_NOT_SENT
        assert "Enter" not in page.keyboard.pressed
        assert page.send_button_clicks == []
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_non_strict_mode_still_allows_enter_fallback_for_other_callers():
    """Pre-existing callers (campaign/job-queue sends) that never pass
    strict_send_confirmation must be completely unaffected — Enter
    fallback still works exactly as before."""
    page = FakePage(send_button_selector_that_matches=None)
    path = _real_temp_file(".jpg")
    try:
        with _real_send_click():
            result = run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="some campaign message", local_file_path=path,
            ))
        assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
        assert "Enter" in page.keyboard.pressed
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_strict_mode_unverified_submission_never_produces_sent_status():
    """4: a caller-level check for native-Forward SEND (2026-08-25) —
    mark_scan.py's _enter_forward_caption_and_send treats
    _find_and_click_send(allow_enter_fallback=False) as its ONLY success
    signal: a real selector matched and clicked, never a secondary signal
    like the composer clearing or a dialog disappearing. Confirms that
    contract holds at its actual dependency: with no real Send selector on
    the page, the strict call returns None (never "keyboard:Enter" or any
    other falsy-but-truthy value that could be mistaken for success)."""
    page = FakePage(send_button_selector_that_matches=None)
    with _real_send_click():
        result = run(sender._find_and_click_send(page, allow_enter_fallback=False))
    assert result is None
    assert "Enter" not in page.keyboard.pressed
    assert page.send_button_clicks == []


def test_successful_submission_is_deterministic_not_incidental():
    """5: a successful send under strict mode is driven by a real,
    positively-identified click — re-running with the same fake state
    produces the same selector/state every time (deterministic), not a
    coincidental Enter-based clearing."""
    for _ in range(3):
        page = FakePage()
        path = _real_temp_file(".jpg")
        try:
            with _real_send_click():
                result = run(sender.send_whatsapp_message(
                    page=page, destination_type="group", destination="Talentgram Casting Test",
                    message_body="Ahana Test — Google Test Take 3", local_file_path=path,
                    strict_send_confirmation=True,
                ))
            assert result["state"] == sender.MESSAGE_SENT_AND_VERIFIED
            assert page.send_button_clicks == [sender.SEND_BUTTON_SELECTORS[0]]
        finally:
            if os.path.exists(path):
                os.unlink(path)


def test_caption_entered_before_send_is_clicked():
    """6: the caption must be typed BEFORE the send control is clicked —
    verified via the shared action_log's temporal ordering, not just that
    both happened."""
    page = FakePage()
    path = _real_temp_file(".jpg")
    try:
        with _real_send_click():
            run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="Ahana Test — Google Test Intro", local_file_path=path,
                strict_send_confirmation=True,
            ))
        kinds = [entry[0] for entry in page.action_log]
        assert "caption_click" in kinds and "send_button_click" in kinds
        assert kinds.index("caption_click") < kinds.index("send_button_click")
        # the actual typed text is the caption, and it happened before the click too.
        type_events = [e for e in page.action_log if e[0] == "keyboard_type"]
        assert type_events and type_events[0][1] == "Ahana Test — Google Test Intro"
        assert kinds.index("keyboard_type") < kinds.index("send_button_click")
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_local_file_path_survives_strict_mode_not_sent_path():
    """7: local_file_path must never be deleted by sender.py even on the
    NEW strict-mode MESSAGE_NOT_SENT early-return path — the finally
    block's owns_temp_file gating still applies."""
    page = FakePage(send_button_selector_that_matches=None)
    path = _real_temp_file(".jpg")
    try:
        with _real_send_click():
            result = run(sender.send_whatsapp_message(
                page=page, destination_type="group", destination="Talentgram Casting Test",
                message_body="caption", local_file_path=path,
                strict_send_confirmation=True,
            ))
        assert result["state"] == sender.MESSAGE_NOT_SENT
        assert os.path.exists(path), "local_file_path must survive the strict-mode MESSAGE_NOT_SENT path"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------------------
# _open_group_chat (2026-08-25) — real evidence (whatsapp_dom_snapshots,
# reason="group_not_found") showed zero candidate rows for SEVERAL
# DIFFERENT groups (including this worker's own concurrent inbound-
# listener searches) within the same narrow multi-second window, with the
# search box itself having focused/typed/read-back correctly every time —
# WhatsApp's own results simply hadn't rendered yet under load, and the
# old code checked exactly once. These tests exercise the REAL
# implementation directly (restored via _real_open_group_chat() below),
# never the file-wide `_opened` stub every other test in this file relies
# on.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _real_open_group_chat():
    sender._open_group_chat = _REAL_OPEN_GROUP_CHAT
    try:
        yield
    finally:
        sender._open_group_chat = _opened


class _FakeGroupCandidateLocator:
    def __init__(self):
        self.click_count = 0

    async def click(self, timeout=None):
        self.click_count += 1


class _FakeAllFilterLocator:
    """Simulates #all-filter's aria-selected state, toggled by click() —
    used to test _ensure_all_filter_selected's bounded reset-before-search
    logic (2026-08-25 fix)."""
    def __init__(self, initially_selected=True, clicks_to_select=1, never_selects=False):
        self._selected = initially_selected
        self._clicks_to_select = clicks_to_select
        self._never_selects = never_selects
        self.click_count = 0
        self.first = self

    async def count(self):
        return 1

    async def get_attribute(self, name):
        assert name == "aria-selected"
        return "true" if self._selected else "false"

    async def click(self, timeout=None):
        self.click_count += 1
        if not self._never_selects and self.click_count >= self._clicks_to_select:
            self._selected = True


class _FakeGroupPage:
    """Minimal page for _open_group_chat's own orchestration — every
    lower-level helper it calls (_collect_search_candidates,
    _verify_chat_open, _read_search_value) is monkeypatched directly per
    test instead of being simulated through real DOM selectors, so these
    tests isolate the NEW bounded-retry/verification logic itself."""
    def __init__(self, all_filter=None):
        self.keyboard = _FakeKeyboard()
        self.url = "https://web.whatsapp.com/"
        # Defaults to "already selected" so every pre-existing test in this
        # file (none of which know about the all-filter fix) is unaffected.
        self.all_filter = all_filter if all_filter is not None else _FakeAllFilterLocator(initially_selected=True)

    async def title(self):
        return "WhatsApp"

    def locator(self, sel):
        if sel == sender.SEARCH_BOX_SELECTORS[0]:
            return _FakeSearchBoxLocator()
        if sel == sender.ALL_FILTER_SELECTOR:
            return self.all_filter
        return _FakeLocator()

    async def click(self, selector, timeout=None):
        pass

    async def type(self, selector, text, delay=None):
        pass

    async def evaluate(self, js, arg=None):
        if "document.activeElement" in js:
            return {"tag": "input", "id": "_r_a_", "in_side": True, "in_main": False, "value": "Test Group", "path": "#_r_a_"}
        return {}


class _FakeSearchBoxLocator:
    def __init__(self):
        self.first = self

    async def count(self):
        return 1

    async def is_visible(self):
        return True


GROUP_NAME = "Test Group"


def _fake_candidate(title, locator):
    return {"index": 0, "locator": locator, "title": title, "raw": title, "norm": sender._norm_group(title)}


def test_open_group_chat_bounded_candidate_polling_recovers():
    """Candidates take a few rounds to render (simulating the real
    evidence: search succeeds, results are momentarily empty under load)
    -> bounded polling recovers rather than concluding NOT_FOUND
    prematurely."""
    page = _FakeGroupPage()
    row = _FakeGroupCandidateLocator()

    verify_calls = {"n": 0}
    async def _fake_verify(page, expected_name=None):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            return False, False, False, ""  # fast-path: not already open
        return True, True, True, expected_name  # post-click verify: succeeds

    collect_calls = {"n": 0}
    async def _fake_collect(page):
        collect_calls["n"] += 1
        if collect_calls["n"] < 3:
            return None, []
        return "sel", [_fake_candidate(GROUP_NAME, row)]

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "OPENED", result
    assert collect_calls["n"] == 3, collect_calls  # took 3 bounded attempts to find candidates
    assert row.click_count == 1, row.click_count


def test_open_group_chat_bounded_candidate_polling_exhausted_is_not_found():
    """Candidates never render across every bounded attempt (a genuinely
    absent group) -> NOT_FOUND, never an infinite retry."""
    page = _FakeGroupPage()

    async def _fake_verify(page, expected_name=None):
        return False, False, False, ""

    collect_calls = {"n": 0}
    async def _fake_collect(page):
        collect_calls["n"] += 1
        return None, []

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "NOT_FOUND", result
    assert collect_calls["n"] == sender.MAX_CANDIDATE_COLLECT_ATTEMPTS, collect_calls


def test_open_group_chat_post_click_verification_recovers():
    """The clicked chat's header takes a moment to render -> bounded
    post-click verification recovers rather than failing immediately."""
    page = _FakeGroupPage()
    row = _FakeGroupCandidateLocator()

    verify_calls = {"n": 0}
    async def _fake_verify(page, expected_name=None):
        verify_calls["n"] += 1
        if verify_calls["n"] <= 2:  # call 1 = fast-path (not open); call 2 = first post-click attempt (not yet rendered)
            return False, False, False, ""
        return True, True, True, expected_name

    async def _fake_collect(page):
        return "sel", [_fake_candidate(GROUP_NAME, row)]

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "OPENED", result
    assert verify_calls["n"] == 3, verify_calls  # fast-path + 2 post-click attempts


def test_open_group_chat_never_verifies_is_search_failed_not_opened():
    """The clicked chat's header never verifies as the requested group
    (e.g. clicked the wrong row, or WhatsApp never rendered it) -> the
    click is NEVER blindly trusted; returns SEARCH_FAILED (retryable),
    not a false OPENED."""
    page = _FakeGroupPage()
    row = _FakeGroupCandidateLocator()

    async def _fake_verify(page, expected_name=None):
        return False, False, False, ""  # never verifies, on any call

    async def _fake_collect(page):
        return "sel", [_fake_candidate(GROUP_NAME, row)]

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "SEARCH_FAILED", result
    assert row.click_count == 1, row.click_count  # the row WAS clicked — verification is what refused to trust it


def test_open_group_chat_already_open_fast_path_unaffected():
    """The requested group is already the active chat -> the fast path
    still short-circuits straight to OPENED, without ever touching
    candidate collection at all (completely unchanged by this fix)."""
    page = _FakeGroupPage()

    async def _fake_verify(page, expected_name=None):
        return True, True, True, expected_name  # already open on the very first (fast-path) call

    collect_calls = {"n": 0}
    async def _fake_collect(page):
        collect_calls["n"] += 1
        return "sel", []

    orig_verify, orig_collect = sender._verify_chat_open, sender._collect_search_candidates
    sender._verify_chat_open, sender._collect_search_candidates = _fake_verify, _fake_collect
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates = orig_verify, orig_collect

    assert result == "OPENED", result
    assert collect_calls["n"] == 0, collect_calls  # fast path never reaches candidate collection at all


def test_open_group_chat_exact_matching_unaffected():
    """A near-miss candidate title (e.g. a DIFFERENT group sharing a
    prefix) is never treated as a match — only an exact normalized-title
    equality opens a chat, completely unchanged by the bounded-polling
    fix (which only changes WHEN candidates are collected, never HOW
    they're matched)."""
    page = _FakeGroupPage()
    wrong_row = _FakeGroupCandidateLocator()

    async def _fake_verify(page, expected_name=None):
        return False, False, False, ""

    async def _fake_collect(page):
        return "sel", [_fake_candidate("Test Group Extra", wrong_row)]  # NOT an exact match for "Test Group"

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "NOT_FOUND", result
    assert wrong_row.click_count == 0, wrong_row.click_count  # the near-miss row was never clicked


# ---------------------------------------------------------------------------
# search_whatsapp_chats / classify_chat_type (SHARE Instagram real
# recipient resolution, Production fix, 2026-09-10) — the search-only
# variant of _open_group_chat's own steps 1-5: reuses the SAME lower-
# level helpers (_collect_search_candidates, _read_search_value), so
# these tests isolate ONLY the new function's own orchestration (never
# clicking, always resetting to neutral, returning every real result
# verbatim) exactly like the _open_group_chat tests above isolate ITS
# bounded-retry/verification logic.
# ---------------------------------------------------------------------------
def test_search_whatsapp_chats_returns_every_real_result_deduped():
    page = _FakeGroupPage()
    row_a, row_b = _FakeGroupCandidateLocator(), _FakeGroupCandidateLocator()

    async def _fake_collect(page):
        return "sel", [
            _fake_candidate("Heena Talentgram", row_a),
            _fake_candidate("Heena Talentgram", row_a),  # duplicate row, same title
            _fake_candidate("Heena Varde", row_b),
        ]

    async def _fake_read_value(page, sel):
        return True, "Heena Talentgram"

    orig_collect, orig_read = sender._collect_search_candidates, sender._read_search_value
    sender._collect_search_candidates, sender._read_search_value = _fake_collect, _fake_read_value
    try:
        result = run(sender.search_whatsapp_chats(page, "Heena Talentgram"))
    finally:
        sender._collect_search_candidates, sender._read_search_value = orig_collect, orig_read

    assert result["error"] is None, result
    names = [c["name"] for c in result["candidates"]]
    assert names == ["Heena Talentgram", "Heena Varde"], names  # deduped, never clicked
    assert row_a.click_count == 0 and row_b.click_count == 0


def test_search_whatsapp_chats_genuinely_empty_is_not_an_error():
    page = _FakeGroupPage()

    async def _fake_collect(page):
        return None, []

    async def _fake_read_value(page, sel):
        return True, "Zzzargled Nonexistent"

    orig_collect, orig_read = sender._collect_search_candidates, sender._read_search_value
    sender._collect_search_candidates, sender._read_search_value = _fake_collect, _fake_read_value
    try:
        result = run(sender.search_whatsapp_chats(page, "Zzzargled Nonexistent"))
    finally:
        sender._collect_search_candidates, sender._read_search_value = orig_collect, orig_read

    assert result == {"candidates": [], "error": None}, result


def test_search_whatsapp_chats_missing_search_box_is_an_infra_error():
    """No sidebar search box resolvable at all -> a real infrastructure
    error, distinct from "WhatsApp genuinely has nothing matching"."""
    class _NoSearchBoxPage(_FakeGroupPage):
        def locator(self, sel):
            if sel == sender.ALL_FILTER_SELECTOR:
                return self.all_filter
            return _FakeLocatorAlwaysAbsent()

    class _FakeLocatorAlwaysAbsent:
        first = None
        async def count(self):
            return 0

    page = _NoSearchBoxPage()
    result = run(sender.search_whatsapp_chats(page, "Anyone"))
    assert result["candidates"] == []
    assert result["error"], result


def test_classify_chat_type_group_when_participants_found():
    page = _FakeGroupPage()

    async def _fake_open(page, name):
        return "OPENED"

    async def _fake_participants(page, name):
        return [{"raw_text": "Alice", "phone": None}, {"raw_text": "Bob", "phone": None}]

    orig_open, orig_participants = sender._open_group_chat, sender.get_group_participants
    sender._open_group_chat, sender.get_group_participants = _fake_open, _fake_participants
    try:
        result = run(sender.classify_chat_type(page, "Some Group"))
    finally:
        sender._open_group_chat, sender.get_group_participants = orig_open, orig_participants

    assert result == "group", result


def test_classify_chat_type_contact_when_no_participants():
    page = _FakeGroupPage()

    async def _fake_open(page, name):
        return "OPENED"

    async def _fake_participants(page, name):
        return None  # Group Info never populated -> not a group

    orig_open, orig_participants = sender._open_group_chat, sender.get_group_participants
    sender._open_group_chat, sender.get_group_participants = _fake_open, _fake_participants
    try:
        result = run(sender.classify_chat_type(page, "Some Contact"))
    finally:
        sender._open_group_chat, sender.get_group_participants = orig_open, orig_participants

    assert result == "contact", result


def test_classify_chat_type_none_when_chat_never_opens():
    page = _FakeGroupPage()

    async def _fake_open(page, name):
        return "NOT_FOUND"

    orig_open = sender._open_group_chat
    sender._open_group_chat = _fake_open
    try:
        result = run(sender.classify_chat_type(page, "Whoever"))
    finally:
        sender._open_group_chat = orig_open

    assert result is None, result


# ---------------------------------------------------------------------------
# _ensure_all_filter_selected / _open_group_chat all-filter reset
# (2026-08-25) — root-caused via LIVE worker log correlation (not a new
# diagnostic dispatch): a real batch-send job's own _open_group_chat call
# for "Talentgram MEDIA SPIKE TEST" returned zero candidate rows while the
# page was demonstrably fully loaded and logged in (title, sidebar, header
# all present) — the search box's OWN placeholder read "Search unread
# chats" and #all-filter's aria-selected was "false", proving the sidebar
# search was scoped to a non-"All" tab. A group with no unread messages —
# the normal state for anything the worker has already read — then can
# NEVER appear in results, no matter how many bounded retries poll for it.
# This superseded an earlier (incorrect) theory that WhatsApp Web was
# periodically reloading mid-operation; the captured "splash_screen_show"/
# "bootstrapWebSession" JSON blamed for that theory turned out to be
# WhatsApp's own static server-bootstrap <script> payload, present in the
# DOM for the entire life of the page, not a live reload signal.
# ---------------------------------------------------------------------------
def test_ensure_all_filter_selected_already_selected_no_click():
    """The common case — "All" is already active — never clicks anything."""
    flt = _FakeAllFilterLocator(initially_selected=True)
    page = _FakeGroupPage(all_filter=flt)
    run(sender._ensure_all_filter_selected(page))
    assert flt.click_count == 0, flt.click_count


def test_ensure_all_filter_selected_clicks_when_not_selected():
    """"All" is NOT the active tab -> clicked, and confirmed selected
    afterward (the root-cause scenario from the live evidence)."""
    flt = _FakeAllFilterLocator(initially_selected=False, clicks_to_select=1)
    page = _FakeGroupPage(all_filter=flt)
    run(sender._ensure_all_filter_selected(page))
    assert flt.click_count == 1, flt.click_count
    assert flt._selected is True


def test_ensure_all_filter_selected_bounded_retry_recovers():
    """The click doesn't register as selected immediately (a WhatsApp
    render delay) -> bounded retry recovers within MAX_ALL_FILTER_ATTEMPTS,
    never an infinite loop."""
    flt = _FakeAllFilterLocator(initially_selected=False, clicks_to_select=2)
    page = _FakeGroupPage(all_filter=flt)
    run(sender._ensure_all_filter_selected(page))
    assert flt.click_count == 2, flt.click_count
    assert flt._selected is True


def test_ensure_all_filter_selected_exhausted_falls_through_not_fatal():
    """"All" never confirms selected across every bounded attempt ->
    the helper logs a warning and returns WITHOUT raising, so a genuinely
    stuck filter degrades to a legible search failure downstream rather
    than blocking _open_group_chat outright."""
    flt = _FakeAllFilterLocator(initially_selected=False, never_selects=True)
    page = _FakeGroupPage(all_filter=flt)
    run(sender._ensure_all_filter_selected(page))  # must not raise
    assert flt.click_count == sender.MAX_ALL_FILTER_ATTEMPTS, flt.click_count


def test_open_group_chat_resets_all_filter_before_searching():
    """End-to-end through the real _open_group_chat: a non-"All" active
    tab is reset to "All" BEFORE the sidebar search runs, and the group is
    then found normally — proving the fix actually sits ahead of the
    search step, not merely unit-tested in isolation."""
    flt = _FakeAllFilterLocator(initially_selected=False, clicks_to_select=1)
    page = _FakeGroupPage(all_filter=flt)
    row = _FakeGroupCandidateLocator()

    verify_calls = {"n": 0}
    async def _fake_verify(page, expected_name=None):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            return False, False, False, ""  # fast-path: not already open
        return True, True, True, expected_name  # post-click verify: succeeds

    async def _fake_collect(page):
        assert flt._selected is True, "search ran before the All filter was confirmed selected"
        return "sel", [_fake_candidate(GROUP_NAME, row)]

    async def _fake_read_value(page, sel):
        return True, GROUP_NAME

    orig_verify, orig_collect, orig_read = sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value
    sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = _fake_verify, _fake_collect, _fake_read_value
    try:
        with _real_open_group_chat():
            result = run(sender._open_group_chat(page, GROUP_NAME))
    finally:
        sender._verify_chat_open, sender._collect_search_candidates, sender._read_search_value = orig_verify, orig_collect, orig_read

    assert result == "OPENED", result
    assert flt.click_count == 1, flt.click_count


def test_open_group_chat_all_filter_absent_is_harmless():
    """A page/build where #all-filter simply doesn't exist (e.g. a
    selector drift, or a personal-account UI without custom labels) must
    never block _open_group_chat — the reset is best-effort."""
    page = _FakeGroupPage()
    page.locator = lambda sel: (
        _FakeSearchBoxLocator() if sel == sender.SEARCH_BOX_SELECTORS[0]
        else (_NoCountLocator() if sel == sender.ALL_FILTER_SELECTOR else _FakeLocator())
    )
    run(sender._ensure_all_filter_selected(page))  # must not raise


class _NoCountLocator:
    async def count(self):
        return 0


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
