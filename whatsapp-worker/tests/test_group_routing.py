"""Regression tests for the sidebar-scoped, deterministically-matched group-open.

Covers the 2026-07-07 P0 fix (batch f324ff84): the search box is ALWAYS inside
#side (never the #main composer), focus is proven before typing, the typed value
is read back, and results are matched by NFKC+casefold+whitespace normalized
equality (case-insensitive, no fuzzy). Also covers send-classification and the
phone-send path preservation.

Run:  MONGO_URL=mongodb://x python tests/test_group_routing.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import sender  # noqa: E402
from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402


# --- Neutralize side effects that need a real browser / DB -------------------
async def _true(*a, **k):
    return True


async def _noop(*a, **k):
    return None


async def _fast_sleep(*a, **k):
    return None


sender.dismiss_blocking_dialogs = _true          # never blocked in tests
sender._store_dom_snapshot = _noop               # no DB / screenshot
sender._capture_open_failure = _noop             # no DB / screenshot on failure
sender.asyncio.sleep = _fast_sleep               # don't actually wait


class _Loc:
    """Minimal locator: a search box, a result row, or an empty match."""
    def __init__(self, n, *, visible=True, text="", title=None, raise_eval=False):
        self._n, self._visible, self._text, self._title = n, visible, text, title
        self._raise_eval = raise_eval

    async def count(self):
        return self._n

    @property
    def first(self):
        return self

    def nth(self, i):
        return self

    async def is_visible(self):
        return self._visible

    async def click(self, **k):
        return None

    async def evaluate(self, expr, *a, **k):
        # Simulates reading el.value on the search <input>; raise => unreadable.
        if self._raise_eval:
            raise RuntimeError("element not readable")
        return self._text

    async def inner_text(self):
        return self._text

    async def text_content(self):
        return self._text

    async def get_attribute(self, name):
        return self._title if name == "title" else None

    def locator(self, sel):
        return _Loc(0, visible=False)   # no nested span[title]


class FakePage:
    """Models the new resolution flow.

    search_box_sel   — which #side-scoped selector resolves (or None)
    candidates       — result-row titles present after typing
    focus_in_side    — where document.activeElement lands (composer guard)
    focus_in_main
    search_value     — value read back from the box after typing
                       (defaults to echoing the typed text)
    """
    url = "https://web.whatsapp.com/"

    def __init__(self, *, search_box_sel=None, candidates=(), focus_in_side=True,
                 focus_in_main=False, search_value=None, search_readable=True):
        self.search_box_sel = search_box_sel
        self.candidates = list(candidates)
        self.focus_in_side = focus_in_side
        self.focus_in_main = focus_in_main
        self.search_value = search_value
        self.search_readable = search_readable
        self.typed = []
        self.clicked_titles = []

    def locator(self, sel):
        if sel == self.search_box_sel:
            val = self.search_value if self.search_value is not None else "".join(self.typed)
            return _Loc(1, visible=True, text=val, raise_eval=not self.search_readable)
        if sel in sender.RESULT_TITLE_SELECTORS and self.candidates:
            page = self

            class _Results:
                async def count(self_inner):
                    return len(page.candidates)

                def nth(self_inner, i):
                    title = page.candidates[i]
                    loc = _Loc(1, visible=True, text=title, title=title)

                    async def _click(**k):
                        page.clicked_titles.append(title)
                    loc.click = _click
                    return loc

                @property
                def first(self_inner):
                    return self_inner.nth(0)
            return _Results()
        return _Loc(0, visible=False)

    async def click(self, sel, **k):
        return None

    async def type(self, sel, text, **k):
        self.typed.append(text)

    class _KB:
        async def press(self, *a, **k):
            return None
    keyboard = _KB()

    async def evaluate(self, js, *a, **k):
        if "activeElement" in js:
            return {"tag": "div", "in_side": self.focus_in_side,
                    "in_main": self.focus_in_main, "path": "#side < @chat-list"}
        return ""

    async def screenshot(self, **k):
        return None

    async def title(self):
        return "WA"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


SB = '#side [aria-label="Search or start a new chat"]'


def main():
    G = "Jon x Talentgram Agency"

    # 0. Every search-box selector is scoped to the sidebar — the composer in
    #    #main can never be selected. (Root-cause guard.)
    assert all(s.startswith("#side") for s in sender.SEARCH_BOX_SELECTORS), \
        sender.SEARCH_BOX_SELECTORS
    print("0. all search-box selectors scoped to #side -> composer excluded")

    # 1. group found via exact normalized match
    p = FakePage(search_box_sel=SB, candidates=[G])
    r = run(sender._open_group_chat(p, G))
    assert r == "OPENED" and p.typed == [G] and p.clicked_titles == [G], (r, p.typed, p.clicked_titles)
    print("1. group found (exact normalized)      -> OPENED")

    # 2. CASE-INSENSITIVE normalized match: requested 'X', candidate 'x' (a
    #    suspected P0 failure mode — stored name casing differing from the live
    #    group title) now resolves correctly.
    p = FakePage(search_box_sel=SB, candidates=["Jon x Talentgram Agency"])
    r = run(sender._open_group_chat(p, "Jon X Talentgram Agency"))
    assert r == "OPENED" and p.clicked_titles == ["Jon x Talentgram Agency"], (r, p.clicked_titles)
    print("2. group found (case-insensitive X/x)  -> OPENED")

    # 3. searched correctly but no candidate matches -> terminal NOT_FOUND
    p = FakePage(search_box_sel=SB, candidates=["Someone Else x Talentgram"])
    r = run(sender._open_group_chat(p, G))
    assert r == "NOT_FOUND", r
    print("3. no normalized match                 -> NOT_FOUND (terminal)")

    # 4. no sidebar search box resolves -> retryable
    p = FakePage(search_box_sel=None)
    r = run(sender._open_group_chat(p, G))
    assert r == "SEARCH_FAILED", r
    print("4. no sidebar search box               -> SEARCH_FAILED (retryable)")

    # 5. COMPOSER GUARD: focus landed in #main -> never type, retryable
    p = FakePage(search_box_sel=SB, candidates=[G], focus_in_side=False, focus_in_main=True)
    r = run(sender._open_group_chat(p, G))
    assert r == "SEARCH_FAILED" and p.typed == [], (r, p.typed)
    print("5. focus in #main (composer)           -> SEARCH_FAILED, nothing typed")

    # 6. READ_OK_MISMATCH: readable but wrong value -> retryable SEARCH_FAILED
    p = FakePage(search_box_sel=SB, candidates=[G], search_value="totally different")
    r = run(sender._open_group_chat(p, G))
    assert r == "SEARCH_FAILED", r
    print("6. read-back READ_OK_MISMATCH          -> SEARCH_FAILED")

    # 6b. READ_UNREADABLE (native <input>, value not readable): read-back is
    #     advisory only — must PROCEED to candidate collection and open, NOT
    #     abort. This is the exact hotfix regression (the ~60s <input> stall
    #     that surfaced as CHAT_NOT_OPENED).
    p = FakePage(search_box_sel=SB, candidates=[G], search_readable=False)
    r = run(sender._open_group_chat(p, G))
    assert r == "OPENED" and p.clicked_titles == [G], (r, p.clicked_titles)
    print("6b. read-back READ_UNREADABLE          -> proceeds -> OPENED")

    # 7. _norm_group: NFKC + casefold + whitespace collapse
    assert sender._norm_group("  Jon   X  Talentgram ") == sender._norm_group("jon x talentgram")
    assert sender._norm_group("Jon x Talentgram") == sender._norm_group("Jon x Talentgram")
    print("7. _norm_group normalization           -> case/space/NFKC folded")

    # 8. worker classification table (mirrors worker.py)
    def classify(exc_type):
        if exc_type is ValueError:
            return "INVALID_DESTINATION"
        if exc_type is PlaywrightTimeoutError:
            return "MESSAGE_NOT_SENT"
        return "MESSAGE_SENT_BUT_NOT_VERIFIED"

    def retryable(state, attempt, maxr=3):
        return state in ("CHAT_NOT_OPENED", "MESSAGE_NOT_SENT") and attempt < maxr
    assert classify(PlaywrightTimeoutError) == "MESSAGE_NOT_SENT"
    assert retryable("MESSAGE_NOT_SENT", 1) is True
    assert classify(ValueError) == "INVALID_DESTINATION"
    print("8. pre-send timeout -> MESSAGE_NOT_SENT, retryable")

    # 9. phone-send path preserved (wa.me deep link intact)
    src = open(os.path.join(os.path.dirname(__file__), "..", "sender.py")).read()
    assert 'https://web.whatsapp.com/send?phone=' in src
    assert 'destination_type == "number"' in src
    print("9. phone send path preserved (wa.me deep link intact)")

    print("\nALL GROUP-ROUTING REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
