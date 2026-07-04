"""Regression tests for the modal/dialog handling framework (modals.py).

Covers: no dialog, recognized dialog dismissed via Escape / Close / whitelist
button, UNKNOWN dialog (no interaction, captured, logged), undismissable
recognized dialog, and dismissal-order safety (Escape before any click).

Run:  MONGO_URL=mongodb://x python tests/test_modals.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import modals  # noqa: E402

modals.SETTLE_SEC = 0  # no settle waits in tests

WHATS_NEW_BODY = (
    "wds-picto-whatsapp-outline\n"
    "What’s new on WhatsApp Web\n"
    "ic-close\n"
    "Ask Meta AI questions, brainstorm ideas, or create images in your chats.\n"
    "Continue"
)


class FakeButton:
    def __init__(self, page, effect, visible=True):
        self._page, self._effect, self._visible = page, effect, visible
    async def count(self):
        return 1 if self._visible else 0
    @property
    def first(self):
        return self
    async def is_visible(self):
        return self._visible
    async def click(self, **k):
        self._page.actions.append(self._effect)
        if self._effect in self._page.dismisses_on:
            self._page.dialog_open = False


class FakeDialog:
    """A role=dialog element with configurable body text and controls."""
    def __init__(self, page, body, has_close=True, safe_labels=("Continue",)):
        self._page, self._body = page, body
        self._has_close, self._safe = has_close, set(safe_labels)
    async def is_visible(self):
        return self._page.dialog_open
    async def inner_text(self):
        return self._body
    def locator(self, sel):
        if "aria-label=\"Close\"" in sel:
            return FakeButton(self._page, "click:close", visible=self._has_close)
        return FakeButton(self._page, "none", visible=False)  # no headings in fake
    def get_by_role(self, role, name=None, exact=False):
        return FakeButton(self._page, f"click:{name}", visible=(name in self._safe))
    async def evaluate(self, script):
        return f"<div role=\"dialog\">{self._body}</div>"


class FakeDialogList:
    def __init__(self, page):
        self._page = page
    async def count(self):
        return 1 if self._page.dialog_open else 0
    def nth(self, i):
        return self._page.dialog


class FakeKeyboard:
    def __init__(self, page):
        self._page = page
    async def press(self, key):
        self._page.actions.append(f"key:{key}")
        if f"key:{key}" in self._page.dismisses_on:
            self._page.dialog_open = False


class FakePage:
    url = "https://web.whatsapp.com/"

    def __init__(self, body=None, dismisses_on=(), has_close=True, safe_labels=("Continue",)):
        self.dialog_open = body is not None
        self.dialog = FakeDialog(self, body or "", has_close, safe_labels) if body else None
        self.dismisses_on = set(dismisses_on)
        self.actions = []
        self.keyboard = FakeKeyboard(self)
        self.screenshots = []

    def locator(self, sel):
        if sel == modals.DIALOG_SELECTOR:
            return FakeDialogList(self)
        return FakeDialogList(self)  # count 0 unless dialog matches

    async def screenshot(self, path=None, **k):
        self.screenshots.append(path)


class FakeDB:
    def __init__(self):
        self.docs = []
        self.whatsapp_dom_snapshots = self
    async def insert_one(self, doc):
        self.docs.append(doc)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def main():
    fake_db = FakeDB()
    modals.get_db = lambda: fake_db

    # 1. No dialog -> True, no interactions.
    p = FakePage()
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is True
    assert p.actions == [], p.actions
    print("1. no dialog                    -> True, zero interactions")

    # 2. Recognized dialog, Escape works -> True; nothing clicked.
    p = FakePage(body=WHATS_NEW_BODY, dismisses_on={"key:Escape"})
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is True
    assert p.actions == ["key:Escape"], p.actions
    print("2. what's-new via Escape        -> dismissed, no clicks needed")

    # 3. Recognized dialog, Escape no-op, Close/X works -> True.
    p = FakePage(body=WHATS_NEW_BODY, dismisses_on={"click:close"})
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is True
    assert p.actions == ["key:Escape", "click:close"], p.actions
    print("3. what's-new via Close/X       -> dismissed, Escape tried first")

    # 4. Recognized dialog, only the whitelisted 'Continue' works -> True.
    p = FakePage(body=WHATS_NEW_BODY, dismisses_on={"click:Continue"}, has_close=False)
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is True
    assert p.actions == ["key:Escape", "click:Continue"], p.actions
    print("4. what's-new via 'Continue'    -> dismissed via whitelist button")

    # 5. UNKNOWN dialog -> False, ZERO interactions, captured + logged.
    n_before = len(fake_db.docs)
    p = FakePage(body="Log out of all devices?\nCancel\nOK", dismisses_on=set(),
                 safe_labels=("OK",))  # 'OK' exists but must never be clicked
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is False
    assert p.actions == [], p.actions          # nothing pressed, nothing clicked
    assert p.screenshots, "screenshot not captured"
    assert len(fake_db.docs) == n_before + 1
    assert fake_db.docs[-1]["reason"] == "unknown_dialog"
    assert "Log out" in fake_db.docs[-1]["dialog_title"]
    print("5. UNKNOWN dialog               -> False, ZERO interactions, snapshot stored")

    # 6. Recognized but undismissable -> False, captured as dialog_undismissable.
    n_before = len(fake_db.docs)
    p = FakePage(body=WHATS_NEW_BODY, dismisses_on=set())
    assert run(modals.dismiss_blocking_dialogs(p, "test")) is False
    assert len(fake_db.docs) == n_before + 1
    assert fake_db.docs[-1]["reason"] == "dialog_undismissable"
    print("6. undismissable recognized     -> False, snapshot stored, graceful")

    # 7. Recognition patterns: real production title matches; casefold + curly
    # apostrophe handled.
    assert modals._is_recognized("What’s new on WhatsApp Web", "") is True
    assert modals._is_recognized("", WHATS_NEW_BODY) is True
    assert modals._is_recognized("Delete this chat?", "This cannot be undone") is False
    print("7. recognition registry         -> whats-new matches, dangerous text does not")

    print("\nALL MODAL-FRAMEWORK REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
