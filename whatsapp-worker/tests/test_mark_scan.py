"""Regression tests for mark_scan.py's pure DOM-interpretation helpers —
the production version of what the Phase 0 spike (spike_diagnostics.py)
proved works: a reply's quoted-media thumbnail hashes byte-identically to
its source message's own thumbnail, and a real WhatsApp @mention exposes a
stable LID via data-app-text-template. Snippets below are trimmed/adapted
directly from real captured DOM during that spike (see the
"ticklish-cuddling-willow" plan) — not invented shapes.

Run:  MONGO_URL=mongodb://x python tests/test_mark_scan.py
"""
import hashlib
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

# Grouped-album structure (2026-08-23) — trimmed directly from a real
# captured "send 4 videos together" multi-select album: ONE data-id, a
# media-album container, N tiles each starting with its own
# `grid-area: r/c/r/c` style boundary and its own embedded thumbnail. Tile
# 2 here also carries a second, larger "extraneous" blob (same pattern
# already known from single video messages) that must NOT be picked.
ALBUM_MESSAGE_HTML = (
    '<div data-id="3B99A3545E173C9DA2C8" data-testid="conv-msg-3B99A3545E173C9DA2C8">'
    '<div data-testid="media-album">'
    '<div style="grid-area: 1 / 1 / 2 / 2;"><div data-testid="video-content">'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,'
    'TILE1AAAABBBBCCCCDDDDTILE1AAAABBBBCCCCDDDDTILE1AAAABBBBCCCCDDDDTILE1AAAAXXXXXXXXX&quot;);"></div>'
    '</div></div>'
    '<div style="grid-area: 1 / 2 / 2 / 3;"><div data-testid="video-content">'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,'
    'TILE2AAAABBBBCCCCDDDDTILE2AAAABBBBCCCCDDDDTILE2AAAABBBBCCCCDDDDTILE2AAAAXXXXXXXXX&quot;);"></div>'
    '<div style="background-image: url(&quot;data:image/jpeg;base64,'
    + ("TILE2BIGPOSTERBLOBDIFFERSEACHTIME" * 5) +
    '&quot;);"></div>'
    '</div></div>'
    '</div></div>'
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

    assert mark_scan._is_album(ALBUM_MESSAGE_HTML) is True
    assert mark_scan._is_album(PHOTO_MESSAGE_HTML) is False
    print("8. album detection                -> media-album correctly distinguished from a plain message")

    tile_hashes = mark_scan._album_tile_hashes(ALBUM_MESSAGE_HTML)
    assert len(tile_hashes) == 2, tile_hashes
    tile1_expected = hashlib.sha256(
        "TILE1AAAABBBBCCCCDDDDTILE1AAAABBBBCCCCDDDDTILE1AAAABBBBCCCCDDDDTILE1AAAAXXXXXXXXX".encode()
    ).hexdigest()
    tile2_expected = hashlib.sha256(
        "TILE2AAAABBBBCCCCDDDDTILE2AAAABBBBCCCCDDDDTILE2AAAABBBBCCCCDDDDTILE2AAAAXXXXXXXXX".encode()
    ).hexdigest()
    assert tile_hashes == [tile1_expected, tile2_expected]
    assert tile_hashes[0] != tile_hashes[1]  # distinct tiles, never collapsed to one
    print("9. album tile hashing             -> each tile gets its own distinct hash, larger blob ignored")

    types = [t for _, t in mark_scan._album_tile_hashes_and_types(ALBUM_MESSAGE_HTML)]
    assert types == ["video", "video"], types
    print("10. album tile media type         -> per-tile type detected (not hardcoded 'video')")

    # Whole-album batch marking (2026-08-23) — "mark google: take 1, take
    # 2, take 3, intro" replying to the ALBUM ITSELF, not one tile.
    assert mark_scan._parse_batch_role_list("mark google: take 1, take 2, take 3, intro") == (
        "google", ["take 1", "take 2", "take 3", "intro"]
    )
    print("11. batch role-list parsing       -> colon-delimited ordered list split correctly")

    assert mark_scan._parse_batch_role_list("mark google take 1") is None  # no colon -> not a batch
    assert mark_scan._parse_batch_role_list("mark google: take 1") is None  # single item -> not a batch
    print("12. batch role-list non-match     -> plain single marks never misparsed as a batch")

    assert mark_scan._SINGLE_PHOTOS_RE.match("mark google photos")
    assert mark_scan._SINGLE_PHOTOS_RE.match("mark google photo")
    assert not mark_scan._SINGLE_PHOTOS_RE.match("mark google take 1")
    print("13. single-photos detection       -> 'mark <project> photos' recognized, takes are not")

    # 2026-08-23 real-test bug: _mark_text() greedily captures WhatsApp's
    # own rendered timestamp trailing the message body — for a batch list
    # this lands entirely on the LAST item ("intro     1:57 pm  1:57 pm"),
    # which would otherwise pollute that tile's synthesized project name.
    assert mark_scan._parse_batch_role_list(
        "mark google: take 1, take 2, take 3, intro     1:57 pm         1:57 pm"
    ) == ("google", ["take 1", "take 2", "take 3", "intro"])
    m = mark_scan._SINGLE_PHOTOS_RE.match("mark google photos     1:57 pm         1:57 pm")
    assert m and m.group(1).strip() == "google"
    print("13b. trailing-timestamp tolerance -> real WhatsApp DOM timestamp text stripped, not baked into project name")

    WHOLE_ALBUM_QUOTE_HTML = (
        '<div data-testid="quoted-message"><span data-testid="author">Raj Talentgram</span>'
        '<div data-testid="chat-msg-symbol"></div>'
        '<span data-testid="selectable-text" class="quoted-mention">4 videos</span></div>'
    )
    assert mark_scan._quoted_is_whole_item_summary(WHOLE_ALBUM_QUOTE_HTML) == 4
    # A real single-tile quote (has an embedded thumbnail blob) must NEVER
    # be misidentified as a whole-album summary, even if its text happens
    # to contain a number.
    assert mark_scan._quoted_is_whole_item_summary(QUOTED_PHOTO_BLOCK_HTML) is None
    print("14. whole-album quote detection   -> 'N videos/photos' summary recognized only when no thumbnail hash exists")


if __name__ == "__main__":
    main()
    print("\nALL MARK_SCAN REGRESSION TESTS PASSED")
