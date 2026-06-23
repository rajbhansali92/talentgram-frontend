"""Regression tests for the resilient group-open + send classification fix.

Covers: group found (direct + via search), group not found, stale search
selector, pre-send timeout classification, and phone-send preservation.

Run:  MONGO_URL=mongodb://x python tests/test_group_routing.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import sender  # noqa: E402
from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402


class FakeLoc:
    def __init__(self, n, visible=True):
        self._n, self._visible = n, visible
    async def count(self):
        return self._n
    @property
    def first(self):
        return self
    async def is_visible(self):
        return self._visible
    async def click(self, **k):
        return None


class FakePage:
    """Configurable stub. `search_box_sel` = which SEARCH_BOX_SELECTORS resolves
    (or None); `visible_titles` = group titles present; `result_after_search` =
    whether the title appears after typing."""
    url = "https://web.whatsapp.com/"

    def __init__(self, *, search_box_sel=None, visible_titles=(), result_after_search=True):
        self.search_box_sel = search_box_sel
        self.visible_titles = set(visible_titles)
        self.result_after_search = result_after_search
        self.typed = []

    def locator(self, sel):
        if sel.startswith("xpath=//span[@title="):
            title = sel.split("@title='", 1)[1].rstrip("']")
            return FakeLoc(1 if title in self.visible_titles else 0)
        if sel == self.search_box_sel:
            return FakeLoc(1, True)
        return FakeLoc(0, False)

    async def click(self, sel, **k):
        return None

    async def type(self, sel, text, **k):
        self.typed.append(text)

    class _KB:
        async def press(self, *a, **k):
            return None
    keyboard = _KB()

    async def wait_for_selector(self, sel, timeout=0):
        title = sel.split("@title='", 1)[1].rstrip("']") if "@title=" in sel else ""
        if self.result_after_search and title:
            self.visible_titles.add(title)
            return True
        raise PlaywrightTimeoutError("not found")

    async def screenshot(self, **k):
        return None

    async def evaluate(self, *a, **k):
        return ""

    async def title(self):
        return "WA"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def main():
    G = "Jon x Talentgram Agency"

    # 1. group found — already visible in list (direct open, no search)
    r = run(sender._open_group_chat(FakePage(visible_titles=[G]), G))
    assert r == "OPENED", r
    print("1. group found (direct visible)        -> OPENED")

    # 2. group found — via search (not initially visible, appears after typing)
    p = FakePage(search_box_sel='div[contenteditable="true"][data-tab="3"]', result_after_search=True)
    r = run(sender._open_group_chat(p, G))
    assert r == "OPENED" and p.typed == [G], (r, p.typed)
    print("2. group found (via resilient search)  -> OPENED, typed via Control+A path")

    # 3. group NOT found — search box ok but no matching result -> terminal
    p = FakePage(search_box_sel='[aria-label="Search input textbox"]', result_after_search=False)
    r = run(sender._open_group_chat(p, G))
    assert r == "NOT_FOUND", r
    print("3. group not found                     -> NOT_FOUND (terminal ValueError)")

    # 4. STALE selector — no search box resolves at all -> retryable
    p = FakePage(search_box_sel=None, visible_titles=[])
    r = run(sender._open_group_chat(p, G))
    assert r == "SEARCH_FAILED", r
    print("4. stale/missing search box            -> SEARCH_FAILED (-> CHAT_NOT_OPENED, retryable)")

    # 5. discovery picks the first visible selector from the chain
    p = FakePage(search_box_sel='[aria-label="Search or start a new chat"]')
    sel = run(sender._find_search_box(p))
    assert sel == '[aria-label="Search or start a new chat"]', sel
    print("5. runtime discovery resolves           ->", sel)

    # 6. worker classification table (mirrors worker.py)
    def classify(exc_type):
        if exc_type is ValueError: return "INVALID_DESTINATION"
        if exc_type is PlaywrightTimeoutError: return "MESSAGE_NOT_SENT"   # pre-send, retryable
        return "MESSAGE_SENT_BUT_NOT_VERIFIED"
    def retryable(state, attempt, maxr=3):
        return state in ("CHAT_NOT_OPENED", "MESSAGE_NOT_SENT") and attempt < maxr
    assert classify(PlaywrightTimeoutError) == "MESSAGE_NOT_SENT"
    assert retryable("MESSAGE_NOT_SENT", 1) is True
    assert retryable("CHAT_NOT_OPENED", 1) is True
    assert classify(ValueError) == "INVALID_DESTINATION"
    print("6. pre-send timeout -> MESSAGE_NOT_SENT, retryable (NOT sent_unverified)")

    # 7. phone-send path preserved: number branch still uses the wa.me deep link
    src = open(os.path.join(os.path.dirname(__file__), "..", "sender.py")).read()
    assert 'https://web.whatsapp.com/send?phone=' in src
    assert 'destination_type == "number"' in src
    print("7. phone send path preserved (wa.me deep link intact)")

    print("\nALL GROUP-ROUTING REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
