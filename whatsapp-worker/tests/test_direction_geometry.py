"""Regression tests for removing the account-identity dependency from
outgoing-message detection (2026-08-11).

_is_outgoing_msg's last-resort tier used to compare the sender name parsed
from data-pre-plain-text against config.WA_SELF_DISPLAY_NAME — a static,
per-account config value that went stale the moment the authenticated
account changed, silently misclassifying real incoming messages from
whoever the old account belonged to (now just another group member) as
"ours" and dropping them. Replaced with a DOM-geometry check (which half of
its row a message bubble's center falls in) that WhatsApp Web computes the
same way for any authenticated account — nothing in this codebase has to
know or assume an identity for it.

These tests exercise the Python-side contract (the function's signature no
longer accepts/requires any identity, and it correctly maps whatever the
page returns to True/False/None) — the actual JS geometry computation can
only be verified against a real WhatsApp Web DOM, which is exactly why the
source-level checks at the bottom of this file also assert the account-
identity comparison is gone from the source, not just unused.

Run:  MONGO_URL=mongodb://x python -m pytest tests/test_direction_geometry.py -q
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import pytest

import sender  # noqa: E402

pytestmark = pytest.mark.asyncio


class FakePage:
    """evaluate() returns whatever the geometry/marker JS would have
    produced — the Python wrapper's own job is just to map that dict to
    True/False/None, which is what these tests check."""
    def __init__(self, eval_result):
        self._eval_result = eval_result

    async def evaluate(self, js, args):
        return self._eval_result


async def test_tail_marker_outgoing():
    page = FakePage({"dir": True})
    assert await sender._is_outgoing_msg(page, "sel", 0) is True


async def test_tail_marker_incoming():
    page = FakePage({"dir": False})
    assert await sender._is_outgoing_msg(page, "sel", 0) is False


async def test_geometry_fallback_inconclusive_returns_none():
    page = FakePage({"dir": None})
    assert await sender._is_outgoing_msg(page, "sel", 0) is None


async def test_index_out_of_range_returns_none():
    page = FakePage(None)
    assert await sender._is_outgoing_msg(page, "sel", 99) is None


async def test_evaluate_exception_returns_none_not_raises():
    class RaisingPage:
        async def evaluate(self, js, args):
            raise RuntimeError("DOM changed")
    assert await sender._is_outgoing_msg(RaisingPage(), "sel", 0) is None


async def test_signature_no_longer_accepts_self_display_name():
    """The whole point of this fix: nothing can pass account identity into
    this function any more, even by accident — the parameter is gone, not
    just unused."""
    sig = inspect.signature(sender._is_outgoing_msg)
    assert "self_display_name" not in sig.parameters
    assert list(sig.parameters) == ["page", "css_selector", "index"]

    page = FakePage({"dir": True})
    with pytest.raises(TypeError):
        await sender._is_outgoing_msg(page, "sel", 0, self_display_name="anything")


async def test_source_contains_no_account_identity_comparison():
    """Belt-and-suspenders source check — the geometry JS itself can only
    be verified against a real WhatsApp Web DOM, but this at least proves
    the account-identity string-comparison path is fully gone, not just
    dead-but-present."""
    src = inspect.getsource(sender._is_outgoing_msg)
    assert "self_display_name" not in src
    assert "prePlainText" not in src
    assert "getBoundingClientRect" in src, "geometry fallback must still be present"


async def test_config_no_longer_defines_wa_self_display_name():
    import config
    assert not hasattr(config, "WA_SELF_DISPLAY_NAME")


async def test_inbound_no_longer_references_wa_self_display_name():
    import inbound
    src = inspect.getsource(inbound)
    assert "WA_SELF_DISPLAY_NAME" not in src
    assert "self_display_name" not in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
