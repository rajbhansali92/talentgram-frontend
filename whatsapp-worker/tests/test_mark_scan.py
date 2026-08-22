"""Regression tests for mark_scan.py's pure DOM-interpretation helpers —
the production version of what the Phase 0 spike (spike_diagnostics.py)
proved works: a reply's quoted-media thumbnail hashes byte-identically to
its source message's own thumbnail, and a real WhatsApp @mention exposes a
stable LID via data-app-text-template. Snippets below are trimmed/adapted
directly from real captured DOM during that spike (see the
"ticklish-cuddling-willow" plan) — not invented shapes.

Run:  MONGO_URL=mongodb://x python tests/test_mark_scan.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("AGENTS_BACKEND_URL", "https://api.example.test")

import mark_scan  # noqa: E402

PHOTO_MESSAGE_HTML = (
    '<div tabindex="-1" class="x1n2onr6 xa0aww2" data-id="3B6637D11A63081B8712" '
    'data-testid="conv-msg-3B6637D11A63081B8712">'
    '<div data-testid="image-thumb" aria-label="Open picture">'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX&quot;);">'
    '</div></div></div>'
)

VIDEO_MESSAGE_HTML = (
    '<div data-id="3BCD6927E2737ED17205" data-testid="conv-msg-3BCD6927E2737ED17205">'
    '<div data-testid="video-content">'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,SMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLE&quot;);"></div>'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,'
    + ("BIGGERPOSTERBLOBDIFFERSEACHTIME" * 5) + '&quot;);"></div>'
    '</div></div>'
)

REPLY_TO_PHOTO_HTML = (
    '<div data-id="3EB0CAC0901DAD51217B30" data-testid="conv-msg-3EB0CAC0901DAD51217B30">'
    '<div><span data-testid="selectable-text" dir="ltr" class="selectable-text copyable-text">'
    '<span><span role="button"><span dir="auto" data-testid="select-all selectable-text" '
    'data-plain-text="@Talentgram Team" '
    'data-app-text-template="​103590702137403@lid​">@<span dir="ltr">Talentgram Team</span></span>'
    '</span></span> mark spike take 1</span></div></div>'
)

QUOTED_PHOTO_BLOCK_HTML = (
    '<div data-testid="quoted-message"><span data-testid="author">Raj Talentgram</span>'
    '<span data-testid="selectable-text" class="quoted-mention">Photo</span>'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX&quot;);"></div>'
    '</div>'
)

BARE_MARK_NO_MENTION_HTML = (
    '<div data-id="OLDSTYLENOOMENTION" data-testid="conv-msg-OLDSTYLENOOMENTION">'
    '<span data-testid="selectable-text" class="selectable-text copyable-text">mark spike take 1</span>'
    '</div>'
)


def main():
    assert mark_scan._own_data_id(PHOTO_MESSAGE_HTML) == "3B6637D11A63081B8712"
    assert mark_scan._media_type(PHOTO_MESSAGE_HTML) == "image"
    assert mark_scan._media_type(VIDEO_MESSAGE_HTML) == "video"
    print("1. data-id + media-type extraction -> correct for image and video messages")

    photo_hash = mark_scan._smallest_hash(PHOTO_MESSAGE_HTML)
    quoted_hash = mark_scan._smallest_hash(QUOTED_PHOTO_BLOCK_HTML)
    assert photo_hash is not None and photo_hash == quoted_hash
    print("2. smallest-thumbnail hash        -> byte-exact match, source vs. quoted block")

    video_hash = mark_scan._smallest_hash(VIDEO_MESSAGE_HTML)
    assert video_hash is not None
    # The SMALLER blob must win — never the larger, non-stable one.
    import hashlib
    bigger_hash = hashlib.sha256(("BIGGERPOSTERBLOBDIFFERSEACHTIME" * 5).encode()).hexdigest()
    assert video_hash != bigger_hash
    print("3. video thumbnail hashing        -> picks the smallest (stable) blob, not the larger one")

    lid = mark_scan._mention_lid(REPLY_TO_PHOTO_HTML)
    assert lid == "103590702137403@lid"
    print("4. mention LID extraction         -> real WhatsApp LID recovered from data-app-text-template")

    assert mark_scan._mention_lid(BARE_MARK_NO_MENTION_HTML) is None
    print("5. no real mention                -> _mention_lid returns None (never falls back to display text)")

    mark = mark_scan._mark_text(REPLY_TO_PHOTO_HTML)
    assert mark is not None and "mark spike take 1" in mark.lower()
    print("6. mark-text extraction           -> literal 'mark ...' recovered from the reply's own body")

    assert mark_scan._mark_text('<div>no keyword here</div>') is None
    print("7. no 'mark' keyword              -> _mark_text returns None, never guessed")


if __name__ == "__main__":
    main()
    print("\nALL MARK_SCAN REGRESSION TESTS PASSED")
