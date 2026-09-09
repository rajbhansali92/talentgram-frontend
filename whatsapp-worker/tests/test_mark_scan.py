"""Regression tests for mark_scan.py's pure DOM-interpretation helpers —
the production version of what the Phase 0 spike (spike_diagnostics.py)
proved works: a reply's quoted-media thumbnail hashes byte-identically to
its source message's own thumbnail, and a real WhatsApp @mention exposes a
stable LID via data-app-text-template. Snippets below are trimmed/adapted
directly from real captured DOM during that spike (see the
"ticklish-cuddling-willow" plan) — not invented shapes.

Run:  MONGO_URL=mongodb://x python tests/test_mark_scan.py
"""
import asyncio
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("AGENTS_BACKEND_URL", "https://api.example.test")

import mark_scan  # noqa: E402
import sender  # noqa: E402

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


class _FakeResponse:
    status_code = 200
    text = "{}"


class _FakeHTTPClient:
    """Records what _upload_one would actually send to /media-upload,
    without a real backend — enough to assert on the content_type/
    filename it computed from the downloaded bytes."""
    def __init__(self):
        self.calls = []

    async def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "files": files})
        return _FakeResponse()


def _upload_target(**overrides):
    base = {
        "source_message_id": "test-msg-id", "talent_id": "t1", "project_id": "p1",
        "media_role": "take", "take_number": 1, "original_label": "Take 1",
        "source_media_type": "video",
    }
    base.update(overrides)
    return base


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

    # 2026-08-23 real production bug (Test A): _identify_tile_index and
    # _resolve_quoted_jump both used to chunk the WHOLE message HTML by
    # grid-area boundary count, which a real diagnostic proved desyncs
    # once WhatsApp appends extra content to the message after tile
    # interactions (html length grew 46843 -> 75901 -> 108327 bytes,
    # tile count undercounted 4 -> 3 -> 2 even though nothing was
    # actually missing). _hash_album_tiles_live fixes this by hashing
    # each ACTUAL clickable element directly - proven here by giving it
    # a message locator whose tile elements are correct regardless of
    # how much unrelated "message" content exists around them (the
    # fake locator here has NO surrounding HTML at all, the extreme
    # case of "grown DOM" - the function still finds exactly 4).
    class _FakeTileElement:
        def __init__(self, testid, html):
            self._testid = testid
            self._html = html
        async def get_attribute(self, name, timeout=None):
            return self._testid if name == "data-testid" else None
        async def evaluate(self, js, timeout=None):
            return self._html

    class _FakeTilesLocator:
        def __init__(self, elements):
            self._elements = elements
        async def count(self):
            return len(self._elements)
        def nth(self, i):
            return self._elements[i]

    class _FakeMessageLocator:
        def __init__(self, elements):
            self._elements = elements
        def locator(self, selector):
            assert "video-content" in selector and "image-content" in selector
            return _FakeTilesLocator(self._elements)

    def _tile_element(seed: str, media_type: str = "video"):
        testid = "video-content" if media_type == "video" else "image-content"
        blob = (seed * 20)[:80]
        html = f'<div data-testid="{testid}"><div style="background-image: url(&quot;data:image/jpeg;base64,{blob}&quot;);"></div></div>'
        return _FakeTileElement(testid, html), hashlib.sha256(blob.encode()).hexdigest()

    elements, expected_hashes = [], []
    for seed in ("TILE1", "TILE2", "TILE3", "TILE4"):
        el, h = _tile_element(seed)
        elements.append(el)
        expected_hashes.append(h)
    message = _FakeMessageLocator(elements)
    live_tiles = asyncio.run(mark_scan._hash_album_tiles_live(message))
    assert [t["hash"] for t in live_tiles] == expected_hashes, live_tiles
    assert all(t["media_type"] == "video" for t in live_tiles)
    assert len(live_tiles) == 4  # Test A: grown-DOM equivalent still yields exactly 4 tiles
    print("15. _hash_album_tiles_live (Test A) -> per-element hashing yields exactly 4 tiles, immune to message-HTML growth")

    # 2026-08-23 real E2E bug: _run_download's video path called
    # _upload_one(..., "") - an empty content_type - which defaulted to
    # generic application/octet-stream and the backend's own (strict,
    # unweakened) signature validation correctly rejected it against the
    # real MP4 bytes: "MIME type header does not match detected file
    # signature." Detection must come from the ACTUAL bytes' own magic
    # signature, never a label or extension.
    MP4_MAGIC = bytes.fromhex("00000018667479706d70343200000000") + b"\x00" * 32  # real shape, see this session's own captured magic_hex
    assert mark_scan._detect_mime_type(MP4_MAGIC, media_type_hint="video") == "video/mp4"
    JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    assert mark_scan._detect_mime_type(JPEG_MAGIC, media_type_hint="image") == "image/jpeg"
    PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    assert mark_scan._detect_mime_type(PNG_MAGIC, media_type_hint="image") == "image/png"
    WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
    assert mark_scan._detect_mime_type(WEBP_MAGIC, media_type_hint="image") == "image/webp"
    # Bytes that don't match ANY known signature must never be silently
    # assigned a specific type we can't verify - the label is a fallback,
    # not an override of a real (mis)match.
    UNKNOWN_BYTES = b"\x00\x01\x02\x03" * 10
    assert mark_scan._detect_mime_type(UNKNOWN_BYTES, media_type_hint="video") == "video/mp4"  # honest hint fallback, not a specific codec guess
    assert mark_scan._detect_mime_type(UNKNOWN_BYTES, media_type_hint=None) == "application/octet-stream"
    print("16. MIME detection from bytes     -> real magic-byte signatures recognized, unknown bytes never given a specific guessed type")

    upload_client = _FakeHTTPClient()
    mp4_b64 = base64.b64encode(MP4_MAGIC).decode()
    upload_result = asyncio.run(mark_scan._upload_one(upload_client, _upload_target(), mp4_b64, ""))
    assert upload_result["ok"] is True, upload_result
    sent_filename, sent_bytes, sent_content_type = upload_client.calls[0]["files"]["file"]
    assert sent_content_type == "video/mp4", sent_content_type
    assert sent_filename.endswith(".mp4"), sent_filename
    assert sent_bytes == MP4_MAGIC
    print("17. _upload_one MIME wiring       -> real MP4 bytes uploaded with content_type=video/mp4 (not octet-stream), .mp4 filename")

    # 2026-08-23 real E2E bug: Take 2/3/Introduction all failed identically
    # opening their tile after Take 1's viewer successfully downloaded -
    # "pointer events intercepted" by a background div, because the
    # download path never closed the viewer at all. Proven fix (real
    # diagnostic probe): click the actual "Close" button
    # (aria-label="Close", svg title "ic-close") found in the same button
    # dump already used for menu-trigger discovery, then wait for the
    # <video> element to actually detach - never force=True, never assumed.
    class _FakeLocator:
        def __init__(self, video_count_sequence):
            self._seq = video_count_sequence
        async def count(self):
            return self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]

    class _FakeMouse:
        def __init__(self):
            self.clicks = []
        async def click(self, x, y, button=None):
            self.clicks.append((x, y, button))

    class _FakeKeyboard:
        def __init__(self):
            self.presses = []
        async def press(self, key):
            self.presses.append(key)

    class _FakePage:
        def __init__(self, video_count_sequence):
            self.mouse = _FakeMouse()
            self.keyboard = _FakeKeyboard()
            self._video_loc = _FakeLocator(video_count_sequence)
        def locator(self, sel):
            assert sel == "video"
            return self._video_loc
        async def wait_for_timeout(self, ms):
            pass

    REAL_CLOSE_BUTTONS = {
        "buttons": [
            {"ariaLabel": "Reply", "dataIcon": None, "svgTitle": None, "testid": None, "rect": [100, 10, 40, 40]},
            {"ariaLabel": "Close", "dataIcon": None, "svgTitle": "ic-close", "testid": None, "rect": [1222, 10, 40, 40]},
        ],
    }
    page1 = _FakePage([1, 0])  # video present, then gone after close
    close1 = asyncio.run(mark_scan._close_viewer(page1, REAL_CLOSE_BUTTONS))
    assert close1["closed"] is True and close1["used_close_button"] is True, close1
    assert page1.mouse.clicks == [(1242.0, 30.0, "left")], page1.mouse.clicks  # center of the real Close button's rect
    assert page1.keyboard.presses == []  # real button found -> no Escape fallback needed
    print("18. _close_viewer real button     -> clicks the actual discovered Close button, waits for <video> to detach")

    page2 = _FakePage([0])  # no video ever present (edge case: already closed)
    close2 = asyncio.run(mark_scan._close_viewer(page2, {"buttons": []}))
    assert close2["closed"] is True and close2["used_close_button"] is False, close2
    assert page2.keyboard.presses == ["Escape"]  # no close button in dump -> Escape fallback used
    print("19. _close_viewer no button found -> falls back to Escape, still verifies via <video> count")

    # 2026-08-24: WhatsApp's own gallery viewer never mounts for a
    # media-album (exhaustively proven live this session: real trusted
    # clicks, keyboard activation, byte-identical DOM/CSS to a working
    # single photo). _download_photo_album_tile_via_blob bypasses the
    # viewer entirely, fetching each tile's own already-loaded full-res
    # blob: URL directly - re-hashing tiles live and requiring an EXACT
    # match, never trusting album_tile_index as identity (only as a
    # navigation hint).
    def _fake_jpeg(width: int, height: int) -> bytes:
        data = b"\xff\xd8\xff\xc0"
        data += (17).to_bytes(2, "big")
        data += bytes([8])
        data += height.to_bytes(2, "big")
        data += width.to_bytes(2, "big")
        data += bytes([1, 1, 0x11, 0])
        data += b"\x00" * 20
        return data

    def _photo_tile_html(seed: str) -> str:
        blob = (seed * 20)[:80]
        return (
            f'<div data-testid="image-thumb" aria-label="Open picture">'
            f'<div style="background-image: url(&quot;data:image/jpeg;base64,{blob}&quot;);"></div></div>'
        )

    def _hash_of(seed: str) -> str:
        blob = (seed * 20)[:80]
        return hashlib.sha256(blob.encode()).hexdigest()

    class _FakePhotoTile:
        def __init__(self, html, full_res):
            self._html = html
            self._full_res = full_res
        async def evaluate(self, js, timeout=None):
            if js == "(el) => el.outerHTML":
                return self._html
            return self._full_res

    class _FakePhotoTilesLocator:
        def __init__(self, tiles):
            self._tiles = tiles
        async def count(self):
            return len(self._tiles)
        def nth(self, i):
            return self._tiles[i]

    class _FakePhotoMessageLocator:
        def __init__(self, tiles):
            self._tiles = tiles
        def locator(self, selector):
            assert selector == '[data-testid="image-thumb"]'
            return _FakePhotoTilesLocator(self._tiles)

    class _FakeBlobPage:
        def __init__(self, bytes_by_src, content_type="image/jpeg"):
            self._bytes_by_src = bytes_by_src
            self._content_type = content_type
        async def evaluate(self, js, arg=None):
            src = arg[0]
            data = self._bytes_by_src.get(src)
            if data is None:
                return {"ok": False, "reason": "unknown src"}
            return {"ok": True, "status": 200, "base64": base64.b64encode(data).decode(), "contentType": self._content_type}

    tile_a_jpeg = _fake_jpeg(1076, 1297)
    tile_a = _FakePhotoTile(
        _photo_tile_html("TILEA"),
        {"src": "blob:https://web.whatsapp.com/tile-a", "naturalWidth": 1076, "naturalHeight": 1297, "complete": True},
    )
    tile_b_jpeg = _fake_jpeg(1066, 1600)
    tile_b = _FakePhotoTile(
        _photo_tile_html("TILEB"),
        {"src": "blob:https://web.whatsapp.com/tile-b", "naturalWidth": 1066, "naturalHeight": 1600, "complete": True},
    )
    message = _FakePhotoMessageLocator([tile_a, tile_b])
    page = _FakeBlobPage({
        "blob:https://web.whatsapp.com/tile-a": tile_a_jpeg,
        "blob:https://web.whatsapp.com/tile-b": tile_b_jpeg,
    })

    result_a = asyncio.run(mark_scan._download_photo_album_tile_via_blob(message, page, 0, _hash_of("TILEA")))
    assert result_a["ok"] is True, result_a
    assert result_a["matched_tile_index"] == 0
    assert result_a["sha256"] == hashlib.sha256(tile_a_jpeg).hexdigest()
    assert result_a["parsed_width"] == 1076 and result_a["parsed_height"] == 1297
    assert result_a["detected_mime"] == "image/jpeg"
    print("20. exact tile hash matching      -> correct tile located by hash, real bytes fetched and verified")

    result_wrong_hash = asyncio.run(mark_scan._download_photo_album_tile_via_blob(message, page, 0, "0" * 64))
    assert result_wrong_hash["ok"] is False and result_wrong_hash["stage"] == "hash_match", result_wrong_hash
    print("21. wrong hash rejection          -> no tile matches -> fails cleanly, never substitutes another tile")

    # album_tile_index is a HINT only, never identity: hint says index 0
    # but the requested hash actually belongs to tile 1 (simulating
    # WhatsApp reordering/appending to the album's DOM after interaction,
    # the same bug class already root-caused for the video path) - must
    # still find it by scanning every tile.
    result_reordered = asyncio.run(mark_scan._download_photo_album_tile_via_blob(message, page, 0, _hash_of("TILEB")))
    assert result_reordered["ok"] is True and result_reordered["matched_tile_index"] == 1, result_reordered
    print("22. album_tile_index is a hint    -> wrong hint index still finds the correct tile via live hash re-scan")

    result_b = asyncio.run(mark_scan._download_photo_album_tile_via_blob(message, page, 1, _hash_of("TILEB")))
    assert result_b["ok"] is True, result_b
    assert result_b["sha256"] == hashlib.sha256(tile_b_jpeg).hexdigest()
    assert result_b["sha256"] != result_a["sha256"]
    assert result_b["parsed_width"] == 1066 and result_b["parsed_height"] == 1600
    print("23. distinct album tiles          -> tile A and tile B download to different SHA-256/dimensions, never the same blob twice")

    tile_no_blob = _FakePhotoTile(
        _photo_tile_html("NOBLOB"),
        {"src": "data:image/jpeg;base64,notablob", "naturalWidth": 100, "naturalHeight": 100, "complete": True},
    )
    msg_no_blob = _FakePhotoMessageLocator([tile_no_blob])
    result_no_blob = asyncio.run(mark_scan._download_photo_album_tile_via_blob(msg_no_blob, page, 0, _hash_of("NOBLOB")))
    assert result_no_blob["ok"] is False and result_no_blob["stage"] == "verify_blob", result_no_blob
    print("24. missing blob rejection        -> full-res src not blob: -> refuses rather than fetching a placeholder")

    tile_incomplete = _FakePhotoTile(
        _photo_tile_html("INCOMP"),
        {"src": "blob:https://web.whatsapp.com/incomplete", "naturalWidth": 1000, "naturalHeight": 1000, "complete": False},
    )
    msg_incomplete = _FakePhotoMessageLocator([tile_incomplete])
    result_incomplete = asyncio.run(mark_scan._download_photo_album_tile_via_blob(msg_incomplete, page, 0, _hash_of("INCOMP")))
    assert result_incomplete["ok"] is False and result_incomplete["stage"] == "verify_complete", result_incomplete
    print("25. incomplete image rejection    -> DOM reports complete=false -> refuses rather than fetching a partial image")

    tile_wrong_mime = _FakePhotoTile(
        _photo_tile_html("WRONGMIME"),
        {"src": "blob:https://web.whatsapp.com/wrong-mime", "naturalWidth": 500, "naturalHeight": 500, "complete": True},
    )
    msg_wrong_mime = _FakePhotoMessageLocator([tile_wrong_mime])
    page_wrong_mime = _FakeBlobPage({"blob:https://web.whatsapp.com/wrong-mime": b"\x00\x01\x02\x03" * 20})
    result_wrong_mime = asyncio.run(mark_scan._download_photo_album_tile_via_blob(msg_wrong_mime, page_wrong_mime, 0, _hash_of("WRONGMIME")))
    assert result_wrong_mime["ok"] is False and result_wrong_mime["stage"] == "mime_validate", result_wrong_mime
    print("26. invalid JPEG / MIME mismatch  -> bytes with no recognizable image signature -> refuses, never assumes image/jpeg")

    tile_dim_mismatch = _FakePhotoTile(
        _photo_tile_html("DIMMISMATCH"),
        {"src": "blob:https://web.whatsapp.com/dim-mismatch", "naturalWidth": 9999, "naturalHeight": 9999, "complete": True},
    )
    msg_dim_mismatch = _FakePhotoMessageLocator([tile_dim_mismatch])
    page_dim_mismatch = _FakeBlobPage({"blob:https://web.whatsapp.com/dim-mismatch": _fake_jpeg(1076, 1297)})
    result_dim_mismatch = asyncio.run(mark_scan._download_photo_album_tile_via_blob(msg_dim_mismatch, page_dim_mismatch, 0, _hash_of("DIMMISMATCH")))
    assert result_dim_mismatch["ok"] is False and result_dim_mismatch["stage"] == "dimension_cross_check", result_dim_mismatch
    print("27. dimension mismatch rejection  -> parsed byte dimensions disagree with DOM naturalWidth/Height -> refuses, never trusts either blindly")

    # image-thumb (2026-08-24 fix): a pure-photo album's tiles never
    # carried video-content/image-content at all - _hash_album_tiles_live
    # would have found ZERO tiles for a real photo album before this fix.
    class _FakeImageThumbTilesLocator:
        def __init__(self, elements):
            self._elements = elements
        async def count(self):
            return len(self._elements)
        def nth(self, i):
            return self._elements[i]

    class _FakeImageThumbMessageLocator:
        def __init__(self, elements):
            self._elements = elements
        def locator(self, selector):
            assert "image-thumb" in selector
            return _FakeImageThumbTilesLocator(self._elements)

    def _image_thumb_tile(seed: str):
        blob = (seed * 20)[:80]
        html = f'<div data-testid="image-thumb"><div style="background-image: url(&quot;data:image/jpeg;base64,{blob}&quot;);"></div></div>'
        return _FakeTileElement("image-thumb", html), hashlib.sha256(blob.encode()).hexdigest()

    thumb_elements, thumb_hashes = [], []
    for seed in ("PTILE1", "PTILE2"):
        el, h = _image_thumb_tile(seed)
        thumb_elements.append(el)
        thumb_hashes.append(h)
    photo_album_message = _FakeImageThumbMessageLocator(thumb_elements)
    live_photo_tiles = asyncio.run(mark_scan._hash_album_tiles_live(photo_album_message))
    assert [t["hash"] for t in live_photo_tiles] == thumb_hashes, live_photo_tiles
    assert all(t["media_type"] == "image" for t in live_photo_tiles), live_photo_tiles
    print("28. _hash_album_tiles_live(image-thumb) -> pure-photo albums are found and typed 'image', not silently zero-tiled")

    # 2026-08-24 real production bug: a reply's quoted-message block
    # (proven present, ~4959 bytes, when checked in isolation) was found
    # ABSENT during a real multi-candidate scan - real cross_check
    # evidence showed the SAME message rendering as a 222-byte
    # virtualized stub mid-scan. _wait_for_quoted_message_block re-checks
    # a bounded number of times, re-locating the message fresh each
    # attempt (never a stale index, never a different data-id), only
    # waiting between attempts when the observed HTML is small enough to
    # look like a genuine not-yet-hydrated stub.
    class _FakeQuotedLocator:
        def __init__(self, present):
            self._present = present
        async def count(self):
            return 1 if self._present else 0

    class _FakeReplyMessage:
        def __init__(self, step):
            self._step = step
        def locator(self, sel):
            assert sel == '[data-testid="quoted-message"]'
            return _FakeQuotedLocator(self._step["quoted_present"])
        async def evaluate(self, js, timeout=None):
            return self._step["cross_check"]
        async def scroll_into_view_if_needed(self, timeout=None):
            pass

    class _FakeLocatorRoot:
        def __init__(self, state, sequence):
            self._state = state
            self._sequence = sequence
        def nth(self, idx):
            return _FakeReplyMessage(self._sequence[self._state["attempt"]])

    class _FakeHydrationPage:
        def __init__(self, state, sequence):
            self._state = state
            self._sequence = sequence
            self.waits = []
        def locator(self, sel):
            return _FakeLocatorRoot(self._state, self._sequence)
        async def wait_for_timeout(self, ms):
            self.waits.append(ms)
        async def evaluate(self, js, arg=None):
            # _restore_message_to_viewport's before/after scroll-metrics
            # capture - a benign no-op snapshot is enough for these tests,
            # which only assert on the outer retry/hydration behavior.
            return {"scrollTop": 0, "scrollHeight": 0, "clientHeight": 0, "target_found": True, "target_own_data_id": "REPLY123"}

    def _make_fake_find_idx(state, sequence, expected_data_id, received_ids):
        async def fake_find_idx(page, group, data_id):
            received_ids.append(data_id)
            state["attempt"] += 1
            if state["attempt"] >= len(sequence):
                return None
            return state["attempt"]
        return fake_find_idx

    orig_find_idx = mark_scan._find_message_index_by_data_id

    # A: stub on attempts 1-2, real quoted block hydrates on attempt 3.
    seq_a = [
        {"quoted_present": False, "cross_check": {"own_data_id": "REPLY123", "js_quoted_found": False, "html_len": 222}},
        {"quoted_present": False, "cross_check": {"own_data_id": "REPLY123", "js_quoted_found": False, "html_len": 240}},
        {"quoted_present": True, "cross_check": None},
    ]
    state_a = {"attempt": -1}
    received_a: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx(state_a, seq_a, "REPLY123", received_a)
    try:
        page_a = _FakeHydrationPage(state_a, seq_a)
        result_a = asyncio.run(mark_scan._wait_for_quoted_message_block(page_a, "GROUP", "REPLY123", "SEL"))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert result_a["ok"] is True, result_a
    assert len(page_a.waits) == 2, page_a.waits  # waited between attempt 1->2 and 2->3, none after success
    print("29. hydration retry succeeds       -> stub on attempts 1-2, real quoted block found on attempt 3 -> resolves")

    # B: stub persists across ALL bounded retries -> clean failure, never stalls, never guesses.
    seq_b = [
        {"quoted_present": False, "cross_check": {"own_data_id": "REPLY123", "js_quoted_found": False, "html_len": 222}},
        {"quoted_present": False, "cross_check": {"own_data_id": "REPLY123", "js_quoted_found": False, "html_len": 230}},
        {"quoted_present": False, "cross_check": {"own_data_id": "REPLY123", "js_quoted_found": False, "html_len": 222}},
    ]
    state_b = {"attempt": -1}
    received_b: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx(state_b, seq_b, "REPLY123", received_b)
    try:
        page_b = _FakeHydrationPage(state_b, seq_b)
        result_b = asyncio.run(mark_scan._wait_for_quoted_message_block(page_b, "GROUP", "REPLY123", "SEL"))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert result_b["ok"] is False, result_b
    assert result_b["hydration_attempts"] == 3, result_b
    assert result_b["reason"] == "reply message has no quoted-message block", result_b
    assert len(page_b.waits) == 2, page_b.waits  # waited between 1->2 and 2->3, never a 3rd wait after exhausting retries
    print("30. hydration retry exhausted      -> stub on every attempt -> clean BATCH_RESOLUTION_FAILED, never stalls or guesses")

    # C: every retry attempt requests the EXACT same reply_data_id - never
    # substitutes a different/nearby message id across retries.
    assert received_a == ["REPLY123"] * 3, received_a
    assert received_b == ["REPLY123"] * 3, received_b
    print("31. hydration retry identity       -> every attempt re-requests the exact same data-id, never resolves by proximity")

    # D/E: _wait_for_quoted_message_block is a pure retry wrapper around
    # the SAME _find_message_index_by_data_id/locator calls already in
    # use - it does not touch _hash_album_tiles_live at all, so the
    # existing video (test 15) and photo (test 28) tile-hashing coverage
    # above already proves both album types remain unaffected by this fix.
    print("32. video/photo batch marking      -> unaffected by the hydration fix (see tests 15 and 28, unchanged)")

    # 2026-08-25: native-Forward SEND replaces the old download+reattach
    # _send_one entirely — SEND now forwards media in place via WhatsApp's
    # own Forward control, never downloading it. These tests cover the new
    # destination-picker matching (_select_forward_destination), proven
    # live via forward_destination_flow_diagnostic: WhatsApp concatenates
    # each row's icon name + chat name + member list into one text node,
    # so matching is substring containment (never exact equality), and
    # every real row renders twice in the DOM (dedupe by shared y-rect).

    class _FakeMouse:
        def __init__(self):
            self.clicks = []
        async def click(self, x, y, button="left"):
            self.clicks.append((x, y, button))

    class _FakeKeyboard:
        def __init__(self):
            self.typed = []
            self.pressed = []
        async def type(self, text, delay=None):
            self.typed.append(text)
        async def press(self, key):
            self.pressed.append(key)

    class _FakeForwardPage:
        def __init__(self):
            self.mouse = _FakeMouse()
            self.keyboard = _FakeKeyboard()
            self.wait_calls = 0
        async def wait_for_timeout(self, ms):
            self.wait_calls += 1

    def _dialog_dump(list_items):
        return {
            "dialogFound": True,
            "textboxes": [{"testid": None, "role": "textbox", "text": "", "rect": [490, 121, 296, 20]}],
            "listItems": list_items,
        }

    orig_evaluate_fs = mark_scan._evaluate

    async def _fake_evaluate_one_match(page, js, arg=None, timeout=10.0):
        return _dialog_dump([
            {"testid": "list-item-2", "role": "listitem",
             "text": "ic-checkdefault-group-refreshedTalentgram Casting TestMyself, Raj, You", "rect": [422, 300, 436, 72]},
            {"testid": "cell-frame-container", "role": None,
             "text": "default-group-refreshedTalentgram Casting TestMyself, Raj, You", "rect": [432, 300, 416, 72]},
        ])
    mark_scan._evaluate = _fake_evaluate_one_match
    try:
        result_33 = asyncio.run(mark_scan._select_forward_destination(_FakeForwardPage(), "Talentgram Casting Test"))
    finally:
        mark_scan._evaluate = orig_evaluate_fs
    assert result_33["ok"] is True, result_33
    print("33. forward destination match      -> substring-contains match across duplicate DOM rows dedupes to exactly one, selects it")

    async def _fake_evaluate_zero_match(page, js, arg=None, timeout=10.0):
        return _dialog_dump([{"testid": "list-item-1", "role": "listitem", "text": "Recent chats", "rect": [422, 228, 436, 72]}])
    mark_scan._evaluate = _fake_evaluate_zero_match
    try:
        result_34 = asyncio.run(mark_scan._select_forward_destination(_FakeForwardPage(), "Talentgram Casting Test"))
    finally:
        mark_scan._evaluate = orig_evaluate_fs
    assert result_34["ok"] is False, result_34
    assert "found 0" in result_34["reason"], result_34
    print("34. forward destination zero match -> clean failure, never guesses at a different group")

    async def _fake_evaluate_two_matches(page, js, arg=None, timeout=10.0):
        return _dialog_dump([
            {"testid": "list-item-2", "role": "listitem", "text": "Talentgram Casting Test AMyself, Raj, You", "rect": [422, 300, 436, 72]},
            {"testid": "list-item-3", "role": "listitem", "text": "Talentgram Casting Test BMyself, Raj, You", "rect": [422, 372, 436, 72]},
        ])
    mark_scan._evaluate = _fake_evaluate_two_matches
    try:
        result_35 = asyncio.run(mark_scan._select_forward_destination(_FakeForwardPage(), "Talentgram Casting Test"))
    finally:
        mark_scan._evaluate = orig_evaluate_fs
    assert result_35["ok"] is False, result_35
    assert "found 2" in result_35["reason"], result_35
    print("35. forward destination ambiguous -> two genuinely distinct rows both match -> clean failure, never auto-picks")

    # 35b: _enter_forward_caption_and_send's strict Send contract — success
    # is ONLY "a real Send selector was matched and clicked" (delegated to
    # sender._find_and_click_send(allow_enter_fallback=False), covered
    # directly in test_sender.py); here we confirm the wrapper propagates
    # that refusal rather than inventing its own success signal when no
    # real control is found.
    orig_sender_click = mark_scan.sender._find_and_click_send

    async def _fake_no_send_control(page, allow_enter_fallback=True):
        assert allow_enter_fallback is False  # SEND must never allow the Enter fallback
        return None

    mark_scan.sender._find_and_click_send = _fake_no_send_control
    try:
        result_35b = asyncio.run(mark_scan._enter_forward_caption_and_send(_FakeForwardPage(), ""))
    finally:
        mark_scan.sender._find_and_click_send = orig_sender_click
    assert result_35b["ok"] is False, result_35b
    print("35b. forward send refuses to guess -> no real Send control found -> ok=False, never a fabricated success")

    # 35c: REGRESSION (2026-08-25 — found via a real SEND E2E). The
    # "Forward media" decoy CAN land on-screen for video (not always
    # off-screen as first observed), and clicking it opens no destination
    # picker at all. _find_onscreen_forward_button must exclude it
    # outright and prefer an exact "Forward" match when one is present.
    dump_with_decoy_only = {
        "rootFound": True,
        "buttons": [{"ariaLabel": "Forward media", "dataIcon": None, "testid": None, "svgTitle": "ic-fast-forward", "rect": [980, 372, 26, 26]}],
    }
    assert mark_scan._find_onscreen_forward_button(dump_with_decoy_only) is None, "decoy alone must never be picked"

    dump_with_both = {
        "rootFound": True,
        "buttons": [
            {"ariaLabel": "Forward media", "dataIcon": None, "testid": None, "svgTitle": "ic-fast-forward", "rect": [980, 372, 26, 26]},
            {"ariaLabel": "Forward", "dataIcon": None, "testid": None, "svgTitle": "ic-fast-forward", "rect": [1126, 10, 40, 40]},
        ],
    }
    picked = mark_scan._find_onscreen_forward_button(dump_with_both)
    assert picked is not None and picked["ariaLabel"] == "Forward", picked
    print("35c. Forward-media decoy excluded  -> exact 'Forward' preferred, decoy never picked even when on-screen")

    # 35d: SEND performance fix (Production, 2026-09-08) — the destination
    # search filter used to be a flat 1000ms wait_for_timeout before ever
    # checking the results once. Now bounded-polled (5 x 200ms, same
    # 1000ms ceiling) — proves the function returns as soon as a match
    # appears, WITHOUT exhausting every attempt, and that the ceiling
    # behavior (genuinely zero matches even after the full window) is
    # unchanged from before.
    match_on_attempt = {"n": 0}

    async def _fake_evaluate_match_on_third_attempt(page, js, arg=None, timeout=10.0):
        match_on_attempt["n"] += 1
        if match_on_attempt["n"] < 3:
            return _dialog_dump([{"testid": "list-item-1", "role": "listitem", "text": "Recent chats", "rect": [422, 228, 436, 72]}])
        return _dialog_dump([
            {"testid": "list-item-2", "role": "listitem",
             "text": "ic-checkdefault-group-refreshedTalentgram Casting TestMyself, Raj, You", "rect": [422, 300, 436, 72]},
        ])

    mark_scan._evaluate = _fake_evaluate_match_on_third_attempt
    page_35d = _FakeForwardPage()
    try:
        result_35d = asyncio.run(mark_scan._select_forward_destination(page_35d, "Talentgram Casting Test"))
    finally:
        mark_scan._evaluate = orig_evaluate_fs
    assert result_35d["ok"] is True, result_35d
    # 3 dump attempts total, but only 2 waits BETWEEN them (never a wait
    # after the match is finally found) — proves the early-break, never
    # the full 5-attempt/1000ms ceiling once a match genuinely appears.
    assert match_on_attempt["n"] == 3, match_on_attempt
    assert page_35d.wait_calls == 2, page_35d.wait_calls
    print("35d. SEND performance: destination search bounded-polled -> returns as soon as a match appears, never waits out the full ceiling once found")

    # 35e: the ceiling itself is unchanged — genuinely zero matches even
    # after every bounded attempt still fails cleanly exactly as before
    # (never an infinite retry, never a longer wait than the original
    # 1000ms fixed budget).
    async def _fake_evaluate_never_matches(page, js, arg=None, timeout=10.0):
        return _dialog_dump([{"testid": "list-item-1", "role": "listitem", "text": "Recent chats", "rect": [422, 228, 436, 72]}])

    mark_scan._evaluate = _fake_evaluate_never_matches
    page_35e = _FakeForwardPage()
    try:
        result_35e = asyncio.run(mark_scan._select_forward_destination(page_35e, "Talentgram Casting Test"))
    finally:
        mark_scan._evaluate = orig_evaluate_fs
    assert result_35e["ok"] is False, result_35e
    assert "found 0" in result_35e["reason"], result_35e
    assert page_35e.wait_calls == 5, page_35e.wait_calls  # 5 bounded attempts, one wait after each — never more, never unbounded
    print("35e. SEND performance: destination search ceiling unchanged -> genuinely zero matches still fails cleanly after the same bounded budget, never longer")

    # ------------------------------------------------------------------
    # 36-40: video-tile re-resolution fix (2026-08-24). Real finding: a
    # live SEND's Playwright error referenced index 3; a diagnostic
    # moments later found the SAME message, completely unchanged,
    # sitting at index 10 — the group had received 8 new messages in
    # between the initial positional lookup and the actual click.
    # _open_tile_viewer_and_download now re-resolves the message fresh by
    # its immutable source_message_id (never a stale positional index)
    # immediately before clicking, and again — bounded, never
    # substituting a different message/tile — if the click fails with a
    # "not stable"/"detached" error specifically.
    # ------------------------------------------------------------------
    class _FakeVideoTileElement:
        def __init__(self, tile_id, fail_times=0):
            self.tile_id = tile_id
            self._fail_times = fail_times
            self.click_count = 0
        async def scroll_into_view_if_needed(self, timeout=None):
            pass
        async def click(self, timeout=None):
            self.click_count += 1
            if self.click_count <= self._fail_times:
                raise Exception(
                    "Locator.click: Timeout 10000ms exceeded.\n"
                    "  - element is not stable\n"
                    "  - element was detached from the DOM, retrying"
                )

    class _FakeVideoTilesLocator:
        def __init__(self, tile):
            self._tile = tile
        def nth(self, i):
            return self._tile

    class _FakeVideoMessageLocator:
        def __init__(self, tile):
            self._tile = tile
        def locator(self, sel):
            assert "video-content" in sel and "image-content" in sel
            return _FakeVideoTilesLocator(self._tile)

    class _CountLocator:
        def __init__(self, n):
            self._n = n
        async def count(self):
            return self._n

    class _FakeVideoConvLocator:
        def __init__(self, message_by_index):
            self._message_by_index = message_by_index
        def nth(self, idx):
            return self._message_by_index.get(idx)

    class _FakeVideoPage:
        def __init__(self, message_by_index):
            self._message_by_index = message_by_index
            self.waits = 0
        def locator(self, sel):
            if sel == "video":
                return _CountLocator(0)  # never mounts -> _inner() never reached, keeps these tests focused on click/resolution only
            return _FakeVideoConvLocator(self._message_by_index)
        async def wait_for_timeout(self, ms):
            self.waits += 1

    orig_evaluate = mark_scan._evaluate
    orig_resolve_scope = mark_scan.sender._resolve_scope

    async def _fake_evaluate(page, js, arg=None, timeout=10.0):
        return None  # _EVENT_CAPTURE_INSTALL_JS / _EVENT_CAPTURE_READ_JS — irrelevant to these tests

    async def _fake_resolve_scope(page):
        return "#main"

    def _make_fake_find_idx(index_sequence, received):
        calls = {"n": -1}
        async def fake_find_idx(page, group, data_id):
            received.append(data_id)
            calls["n"] += 1
            return index_sequence[min(calls["n"], len(index_sequence) - 1)]
        return fake_find_idx

    mark_scan._evaluate = _fake_evaluate
    mark_scan.sender._resolve_scope = _fake_resolve_scope
    orig_find_idx = mark_scan._find_message_index_by_data_id

    # 36: the message's CURRENT live index (10) is what actually gets
    # used, completely independent of whatever positional index a caller
    # might have separately computed earlier (the real 2026-08-24 bug: a
    # live error referenced index 3 for this exact message while it was
    # actually sitting at index 10 by click time) — proven by never
    # passing any such stale index in at all: only group_name/
    # source_message_id are given, and the function's own fresh lookup
    # supplies the current truth (10) directly.
    tile_36 = _FakeVideoTileElement("TILE-36")
    received_36: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx([10], received_36)
    try:
        page_36 = _FakeVideoPage({10: _FakeVideoMessageLocator(tile_36)})
        dl_36 = asyncio.run(mark_scan._open_tile_viewer_and_download(
            page_36, message_locator=None, tile_index=0,
            group_name="Talentgram MEDIA SPIKE TEST", source_message_id="3B07252BFE7BC81FB956",
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert tile_36.click_count == 1, tile_36.click_count
    assert "click failed" not in (dl_36.get("reason") or ""), dl_36  # never failed at the click stage
    assert received_36 == ["3B07252BFE7BC81FB956"], received_36  # always looked up by the immutable identity, never by position
    print("36. video tile index drift         -> re-resolved by source_message_id at click time, not a stale positional index")

    # 37: the tile detaches once (mid-click failure), then re-resolution
    # finds the SAME message again and the retry succeeds.
    tile_37 = _FakeVideoTileElement("TILE-37", fail_times=1)
    received_37: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx([5, 5], received_37)
    try:
        page_37 = _FakeVideoPage({5: _FakeVideoMessageLocator(tile_37)})
        dl_37 = asyncio.run(mark_scan._open_tile_viewer_and_download(
            page_37, message_locator=None, tile_index=0,
            group_name="Talentgram MEDIA SPIKE TEST", source_message_id="3B07252BFE7BC81FB956",
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert tile_37.click_count == 2, tile_37.click_count  # failed once, retried once, succeeded
    assert "click failed" not in (dl_37.get("reason") or ""), dl_37
    assert len(received_37) == 2, received_37  # re-resolved by data-id before the retry, not reused
    print("37. video tile detaches once       -> re-resolution + bounded retry recovers, same message re-clicked")

    # 38: the source message is genuinely gone (removed/scrolled beyond
    # reach) on every lookup attempt -> clean bounded failure, no infinite loop.
    received_38: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx([None], received_38)
    try:
        page_38 = _FakeVideoPage({})
        dl_38 = asyncio.run(mark_scan._open_tile_viewer_and_download(
            page_38, message_locator=None, tile_index=0,
            group_name="Talentgram MEDIA SPIKE TEST", source_message_id="3B07252BFE7BC81FB956",
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert dl_38["ok"] is False, dl_38
    assert dl_38["stage"] == "open_tile", dl_38
    assert "no longer found" in dl_38["reason"], dl_38
    assert len(received_38) == 1, received_38  # exactly one lookup attempt — no infinite retry against a message that's simply gone
    print("38. source message genuinely gone  -> clean bounded failure, never an infinite retry loop")

    # 39: a persistently-unstable tile (fails every attempt) is bounded to
    # MAX_TILE_CLICK_ATTEMPTS total clicks, then fails cleanly — never
    # substituting a different message/tile along the way.
    tile_39 = _FakeVideoTileElement("TILE-39", fail_times=99)
    received_39: list = []
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx([7, 7, 7], received_39)
    try:
        page_39 = _FakeVideoPage({7: _FakeVideoMessageLocator(tile_39)})
        dl_39 = asyncio.run(mark_scan._open_tile_viewer_and_download(
            page_39, message_locator=None, tile_index=0,
            group_name="Talentgram MEDIA SPIKE TEST", source_message_id="3B07252BFE7BC81FB956",
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx
    assert dl_39["ok"] is False, dl_39
    assert tile_39.click_count == mark_scan.MAX_TILE_CLICK_ATTEMPTS, tile_39.click_count
    assert all(rid == "3B07252BFE7BC81FB956" for rid in received_39), received_39  # every re-resolution used the SAME identity, never a substitute
    print("39. persistently unstable tile     -> bounded to MAX_TILE_CLICK_ATTEMPTS, fails cleanly, never substitutes another item")

    # 40: diagnostic-only callers that omit group_name/source_message_id
    # get EXACTLY the old behavior — the passed message_locator is used
    # directly, no re-resolution, no retry (proves this fix is additive,
    # not a behavior change for existing callers).
    tile_40 = _FakeVideoTileElement("TILE-40")
    message_40 = _FakeVideoMessageLocator(tile_40)
    page_40 = _FakeVideoPage({})
    dl_40 = asyncio.run(mark_scan._open_tile_viewer_and_download(page_40, message_40, 0))
    assert tile_40.click_count == 1, tile_40.click_count
    assert "click failed" not in (dl_40.get("reason") or ""), dl_40
    print("40. legacy callers unaffected      -> omitting group_name/source_message_id preserves the exact pre-fix behavior")

    mark_scan._evaluate = orig_evaluate
    mark_scan.sender._resolve_scope = orig_resolve_scope

    # 41: the photo/blob path (_download_photo_album_tile_via_blob) is
    # completely untouched by this fix — same exact-hash-match behavior
    # as tests 20-27, re-verified after the video-tile change (same
    # fixtures/pattern as test 20 above).
    tile_c_jpeg = _fake_jpeg(800, 600)
    tile_c = _FakePhotoTile(
        _photo_tile_html("TILEC41"),
        {"src": "blob:https://web.whatsapp.com/tile-c41", "naturalWidth": 800, "naturalHeight": 600, "complete": True},
    )
    photo_message_41 = _FakePhotoMessageLocator([tile_c])
    photo_page_41 = _FakeBlobPage({"blob:https://web.whatsapp.com/tile-c41": tile_c_jpeg})
    photo_result_41 = asyncio.run(mark_scan._download_photo_album_tile_via_blob(
        photo_message_41, photo_page_41, 0, _hash_of("TILEC41"),
    ))
    assert photo_result_41["ok"] is True, photo_result_41
    assert photo_result_41["matched_tile_index"] == 0
    assert photo_result_41["sha256"] == hashlib.sha256(tile_c_jpeg).hexdigest()
    print("41. photo/blob path unaffected     -> _download_photo_album_tile_via_blob behavior unchanged by the video-tile fix")

    # 42: REGRESSION (2026-08-25 — found via a real live scan against a
    # genuine no-mention mark, which never appeared in the candidates
    # list at all). _run_scan's own Pass 2 used to `continue` (silently
    # drop) any reply with no real @mention BEFORE mark_text was even
    # checked — a no-mention mark never reached the backend's
    # validate_candidates at all, making that function's own mention-
    # optional fix moot. Fixed: mark_text presence (not mention presence)
    # is now the only gate; mention_lid is captured as None when absent.
    class _FakeScanSender:
        async def _open_group_chat(self, page, group_name):
            return "OPENED"

    orig_sender_scan = mark_scan.sender
    orig_dump_window = mark_scan._dump_window

    async def _fake_dump_window(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return [{
            "messageHtml": BARE_MARK_NO_MENTION_HTML,
            "quotedHtml": QUOTED_PHOTO_BLOCK_HTML,
        }]

    mark_scan.sender = _FakeScanSender()
    mark_scan._dump_window = _fake_dump_window
    try:
        scan_result_42 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Talentgram MEDIA SPIKE TEST"}))
    finally:
        mark_scan.sender = orig_sender_scan
        mark_scan._dump_window = orig_dump_window
    cands_42 = scan_result_42.get("candidates") or []
    assert len(cands_42) == 1, scan_result_42
    assert cands_42[0]["mention_lid"] is None, cands_42[0]
    assert cands_42[0]["mark_text"] == "mark spike take 1", cands_42[0]
    print("42. no-mention mark reaches backend -> _run_scan captures it as a candidate with mention_lid=None, never silently dropped")

    # ------------------------------------------------------------------
    # 43-51: UPLOAD video-retrieval hardening (2026-08-25). Real finding:
    # UPLOAD's own production call site (_run_download) never actually
    # wired group_name/source_message_id into _open_tile_viewer_and_download,
    # so the existing re-resolution+bounded-retry fix was never active for
    # real UPLOAD runs — the SAME video that native-Forward SEND opened
    # successfully twice still failed UPLOAD's download with "element is
    # not stable"/"detached from the DOM". _open_tile_viewer_and_download_hardened
    # is a NEW, independent entry point (never touches SEND's own
    # _open_media_and_get_forward_button or _resolve_video_tile_locator)
    # that adds live tile-hash verification (never trusts tile_index
    # alone) plus a bounded close/reopen round when Download never
    # appears, wired into _run_download's real video call site.
    # ------------------------------------------------------------------
    def _video_tile_html(seed: str, testid: str = "video-content") -> str:
        blob = (seed * 20)[:80]
        return (
            f'<div data-testid="{testid}">'
            f'<div style="background-image: url(&quot;data:image/jpeg;base64,{blob}&quot;);"></div></div>'
        )

    class _FakeHashTile:
        def __init__(self, testid, html, fail_times=0):
            self._testid = testid
            self._html = html
            self._fail_times = fail_times
            self.click_count = 0
        async def get_attribute(self, name, timeout=None):
            assert name == "data-testid"
            return self._testid
        async def evaluate(self, js, timeout=None):
            assert js == "(el) => el.outerHTML"
            return self._html
        async def scroll_into_view_if_needed(self, timeout=None):
            pass
        async def click(self, timeout=None):
            self.click_count += 1
            if self.click_count <= self._fail_times:
                raise Exception(
                    "Locator.click: Timeout 10000ms exceeded.\n"
                    "  - element is not stable\n"
                    "  - element was detached from the DOM, retrying"
                )

    class _FakeHashTilesLocator:
        def __init__(self, tiles):
            self._tiles = tiles
        async def count(self):
            return len(self._tiles)
        def nth(self, i):
            return self._tiles[i]

    class _FakeHashMessageLocator:
        def __init__(self, tiles):
            self._tiles = tiles
        def locator(self, selector):
            assert "video-content" in selector and "image-content" in selector
            return _FakeHashTilesLocator(self._tiles)

    class _FakeHashConvLocator:
        def __init__(self, message_by_index):
            self._message_by_index = message_by_index
        def nth(self, idx):
            return self._message_by_index.get(idx)

    class _FakeHashPage:
        def __init__(self, message_by_index, video_mounted=False):
            self._message_by_index = message_by_index
            self._video_mounted = video_mounted
            self.waits = 0
        def locator(self, sel):
            if sel == "video":
                return _CountLocator(1 if self._video_mounted else 0)
            return _FakeHashConvLocator(self._message_by_index)
        async def wait_for_timeout(self, ms):
            self.waits += 1

    orig_evaluate_up = mark_scan._evaluate
    orig_resolve_scope_up = mark_scan.sender._resolve_scope
    orig_find_idx_up = mark_scan._find_message_index_by_data_id

    async def _fake_evaluate_up(page, js, arg=None, timeout=10.0):
        return None

    async def _fake_resolve_scope_up(page):
        return "#main"

    def _make_fake_find_idx_up(index_sequence):
        calls = {"n": -1}
        async def fake_find_idx(page, group, data_id):
            calls["n"] += 1
            return index_sequence[min(calls["n"], len(index_sequence) - 1)]
        return fake_find_idx

    mark_scan._evaluate = _fake_evaluate_up
    mark_scan.sender._resolve_scope = _fake_resolve_scope_up

    # 43: message index changes between initial resolution and click ->
    # recovery succeeds. The message's CURRENT live index (10) is what
    # actually gets used — the function never receives, or needs, any
    # separately-computed positional index at all.
    tile_43 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE43"))
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([10])
    try:
        page_43 = _FakeHashPage({10: _FakeHashMessageLocator([tile_43])}, video_mounted=True)
        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        mark_scan._wait_for_video_readiness = lambda page, **kw: asyncio.sleep(0, result={"reached": True})
        mark_scan._click_download_in_open_viewer = lambda page, vb, r: asyncio.sleep(0, result={"ok": True, "downloads": [{"ok": True, "_raw_bytes": b"X"}]})
        mark_scan._close_viewer = lambda page, vb: asyncio.sleep(0, result={"closed": True})
        try:
            dl_43 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_43, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEOTILE43"), 0,
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_43["ok"] is True, dl_43
    assert dl_43["resolved_tile_index"] == 0, dl_43
    assert tile_43.click_count == 1, tile_43.click_count
    print("43. UPLOAD index drift recovery    -> resolves by current live index, not a stale positional one")

    # 44: video DOM node is detached once -> re-resolution succeeds.
    tile_44 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE44"), fail_times=1)
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([5, 5])
    try:
        page_44 = _FakeHashPage({5: _FakeHashMessageLocator([tile_44])}, video_mounted=True)
        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        mark_scan._wait_for_video_readiness = lambda page, **kw: asyncio.sleep(0, result={"reached": True})
        mark_scan._click_download_in_open_viewer = lambda page, vb, r: asyncio.sleep(0, result={"ok": True, "downloads": [{"ok": True, "_raw_bytes": b"X"}]})
        mark_scan._close_viewer = lambda page, vb: asyncio.sleep(0, result={"closed": True})
        try:
            dl_44 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_44, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEOTILE44"), 0,
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_44["ok"] is True, dl_44
    assert tile_44.click_count == 2, tile_44.click_count  # failed once, retried once, succeeded
    print("44. UPLOAD tile detaches once      -> re-resolution + bounded retry recovers, same message re-clicked")

    # 45: exact source message disappears -> clean failure, no retry
    # across rounds (an identity failure is never click-retryable).
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([None])
    try:
        page_45 = _FakeHashPage({})
        dl_45 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
            page_45, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("V45"), 0,
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_45["ok"] is False, dl_45
    assert dl_45["stage"] == "resolve_tile", dl_45
    assert dl_45["reason"] == "message_not_found", dl_45
    assert dl_45["round"] == 0, dl_45  # exactly one attempt — never retried against a message that's simply gone
    print("45. UPLOAD source message gone     -> clean bounded failure, never an infinite retry loop")

    # 46: wrong neighboring video at the hinted index MUST NOT be
    # selected -> the real match (elsewhere in the same message) is found
    # by a full-message hash search instead.
    wrong_tile_46 = _FakeHashTile("video-content", _video_tile_html("VIDEOWRONG46"))
    right_tile_46 = _FakeHashTile("video-content", _video_tile_html("VIDEORIGHT46"))
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([3])
    try:
        page_46 = _FakeHashPage({3: _FakeHashMessageLocator([wrong_tile_46, right_tile_46])}, video_mounted=True)
        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        mark_scan._wait_for_video_readiness = lambda page, **kw: asyncio.sleep(0, result={"reached": True})
        mark_scan._click_download_in_open_viewer = lambda page, vb, r: asyncio.sleep(0, result={"ok": True, "downloads": [{"ok": True, "_raw_bytes": b"X"}]})
        mark_scan._close_viewer = lambda page, vb: asyncio.sleep(0, result={"closed": True})
        try:
            dl_46 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_46, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEORIGHT46"), 0,  # hint says index 0 (wrong)
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_46["ok"] is True, dl_46
    assert dl_46["resolved_tile_index"] == 1, dl_46  # NOT the hinted index 0
    assert wrong_tile_46.click_count == 0, "the wrong neighboring tile must never be clicked"
    assert right_tile_46.click_count == 1, right_tile_46.click_count
    print("46. UPLOAD wrong neighbor rejected -> hint index ignored once its hash disagrees, correct tile found and clicked instead")

    # 47: hash/source identity mismatch (no tile anywhere in the message
    # matches) -> MUST NOT proceed, never clicks anything.
    only_tile_47 = _FakeHashTile("video-content", _video_tile_html("VIDEOONLY47"))
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([7])
    try:
        page_47 = _FakeHashPage({7: _FakeHashMessageLocator([only_tile_47])}, video_mounted=True)
        dl_47 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
            page_47, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("NOTHING_MATCHES_47"), 0,
        ))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_47["ok"] is False, dl_47
    assert dl_47["stage"] == "resolve_tile", dl_47
    assert dl_47["reason"] == "hash_mismatch", dl_47
    assert only_tile_47.click_count == 0, "must never click when identity can't be verified"
    print("47. UPLOAD hash mismatch           -> refuses to proceed, never clicks an unverified tile")

    # 48: video not fully buffered -> the hardened path still calls the
    # SAME bounded readiness wait (never skipped, never a fixed sleep
    # substituted in its place).
    tile_48 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE48"))
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([1])
    readiness_calls_48 = []
    try:
        page_48 = _FakeHashPage({1: _FakeHashMessageLocator([tile_48])}, video_mounted=True)
        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        async def _fake_readiness_not_reached(page, **kw):
            readiness_calls_48.append(kw)
            return {"reached": False, "elapsed_s": 60.0, "state": None}
        mark_scan._wait_for_video_readiness = _fake_readiness_not_reached
        mark_scan._click_download_in_open_viewer = lambda page, vb, r: asyncio.sleep(0, result={"ok": False, "stage": "find_download_item", "reason": "menu appeared but no 'Download' item found"})
        mark_scan._close_viewer = lambda page, vb: asyncio.sleep(0, result={"closed": True})
        try:
            dl_48 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_48, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEOTILE48"), 0,
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert len(readiness_calls_48) == mark_scan.MAX_DOWNLOAD_READINESS_ROUNDS, readiness_calls_48  # called every round, never skipped
    assert dl_48["ok"] is False, dl_48
    print("48. UPLOAD video not buffered      -> the bounded readiness wait is called every round, never skipped or replaced with a fixed sleep")

    # 49: video fully buffered but Download isn't immediately visible ->
    # bounded close/reopen round recovers, re-resolving by hash fresh
    # each time (never reusing the same tile locator across rounds).
    tile_49_round0 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE49"))
    tile_49_round1 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE49"))
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([2, 2])
    close_calls_49 = []
    try:
        page_49 = _FakeHashPage(
            {2: _FakeHashMessageLocator([tile_49_round0])},  # overwritten mid-test below for round 2
            video_mounted=True,
        )
        # Swap the message locator between rounds so round 2 hits a DIFFERENT
        # (freshly re-resolved) tile object — proving the function
        # re-resolves rather than reusing the same locator across rounds.
        call_state = {"n": 0}
        def _locator_router(sel):
            if sel == "video":
                return _CountLocator(1)
            call_state["n"] += 1
            tiles = [tile_49_round0] if call_state["n"] == 1 else [tile_49_round1]
            return _FakeHashConvLocator({2: _FakeHashMessageLocator(tiles)})
        page_49.locator = _locator_router

        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        orig_open_group_49 = mark_scan.sender._open_group_chat
        mark_scan._wait_for_video_readiness = lambda page, **kw: asyncio.sleep(0, result={"reached": True})
        click_dl_calls = {"n": 0}
        async def _fake_click_dl(page, vb, r):
            click_dl_calls["n"] += 1
            if click_dl_calls["n"] == 1:
                return {"ok": False, "stage": "find_download_item", "reason": "menu appeared but no 'Download' item found"}
            return {"ok": True, "downloads": [{"ok": True, "_raw_bytes": b"X"}]}
        mark_scan._click_download_in_open_viewer = _fake_click_dl
        async def _fake_close(page, vb):
            close_calls_49.append(True)
            return {"closed": True}
        mark_scan._close_viewer = _fake_close
        reopen_calls_49 = []
        async def _fake_reopen_49(page, group_name):
            reopen_calls_49.append(group_name)
            return "OPENED"
        mark_scan.sender._open_group_chat = _fake_reopen_49
        try:
            dl_49 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_49, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEOTILE49"), 0,
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
            mark_scan.sender._open_group_chat = orig_open_group_49
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_49["ok"] is True, dl_49
    assert dl_49["round"] == 1, dl_49  # succeeded on the SECOND round, not the first
    assert len(close_calls_49) == 2, close_calls_49  # once to leave the failed round, once more on the successful round's own exit
    assert reopen_calls_49 == ["Talentgram MEDIA SPIKE TEST"], reopen_calls_49  # re-verified the group before round 1, never before round 0
    assert tile_49_round0.click_count == 1, tile_49_round0.click_count
    assert tile_49_round1.click_count == 1, tile_49_round1.click_count  # round 2 clicked a FRESH tile, not round 1's
    print("49. UPLOAD Download not visible    -> bounded close/reopen round recovers, re-resolving fresh each round")

    # 50: correct Download control is selected — the round-1 success path
    # returns the real downloads list from _click_download_in_open_viewer
    # unchanged (already exercised structurally by tests 43/44/46 above;
    # this confirms the top-level result shape a real caller consumes).
    assert dl_43["downloads"][0]["_raw_bytes"] == b"X", dl_43
    assert dl_49["downloads"][0]["ok"] is True, dl_49
    print("50. UPLOAD correct Download used   -> the real Download result (downloads[]) reaches the top-level return unchanged")

    mark_scan._evaluate = orig_evaluate_up
    mark_scan.sender._resolve_scope = orig_resolve_scope_up

    # 51: photo/album path is completely unaffected — re-affirms test 41
    # (_download_photo_album_tile_via_blob is never called from, and
    # shares no code with, the new hardened video path).
    tile_51_jpeg = _fake_jpeg(640, 480)
    tile_51 = _FakePhotoTile(
        _photo_tile_html("TILE51"),
        {"src": "blob:https://web.whatsapp.com/tile-51", "naturalWidth": 640, "naturalHeight": 480, "complete": True},
    )
    photo_message_51 = _FakePhotoMessageLocator([tile_51])
    photo_page_51 = _FakeBlobPage({"blob:https://web.whatsapp.com/tile-51": tile_51_jpeg})
    photo_result_51 = asyncio.run(mark_scan._download_photo_album_tile_via_blob(
        photo_message_51, photo_page_51, 0, _hash_of("TILE51"),
    ))
    assert photo_result_51["ok"] is True, photo_result_51
    assert photo_result_51["sha256"] == hashlib.sha256(tile_51_jpeg).hexdigest()
    print("51. UPLOAD photo path unaffected   -> _download_photo_album_tile_via_blob shares no code with the new hardened video path")

    # 52: REGRESSION (2026-08-25 — found via a real live UPLOAD E2E). The
    # FIRST plausible menu-trigger candidate opened a message-level
    # "Reply privately"/"Report <sender>" context menu instead of the
    # viewer's own Download menu, on the SAME open video viewer. The
    # (wrong) menu is dismissed via Escape and the NEXT candidate is
    # tried, which succeeds — self-healing without a blind/unbounded retry.
    class _FakeDownloadItemLocator:
        def __init__(self, page):
            self._page = page
            self.first = self
        async def is_visible(self, timeout=None):
            return self._page.click_count >= 2  # only the SECOND trigger's menu has Download
        async def click(self, timeout=None):
            pass

    class _FakeMenuPage:
        def __init__(self):
            self.mouse = _FakeMouse()
            self.keyboard = _FakeKeyboard()
            self.click_count = 0
        async def wait_for_timeout(self, ms):
            pass
        def locator(self, sel):
            return _FakeDownloadItemLocator(self)

    wrong_trigger = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [900, 20, 24, 24]}
    right_trigger = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [850, 20, 24, 24]}
    viewer_buttons_52 = {"buttons": [wrong_trigger, right_trigger]}

    page_52 = _FakeMenuPage()
    orig_evaluate_menu = mark_scan._evaluate
    orig_collect_downloads = mark_scan._collect_downloads

    async def _fake_evaluate_menu(page, js, arg=None, timeout=10.0):
        page.click_count += 1
        return {"items": [{"text": "Reply privately"}]} if page.click_count == 1 else {"items": [{"text": "Download"}]}

    async def _fake_collect_downloads(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    mark_scan._evaluate = _fake_evaluate_menu
    mark_scan._collect_downloads = _fake_collect_downloads
    try:
        result_52 = asyncio.run(mark_scan._click_download_in_open_viewer(page_52, viewer_buttons_52, {"reached": True}))
    finally:
        mark_scan._evaluate = orig_evaluate_menu
        mark_scan._collect_downloads = orig_collect_downloads

    assert result_52["ok"] is True, result_52
    assert page_52.click_count == 2, page_52.click_count  # wrong candidate tried first, then the right one
    assert "Escape" in page_52.keyboard.pressed, page_52.keyboard.pressed  # the wrong menu was dismissed before retrying
    print("52. UPLOAD menu self-heals         -> wrong candidate (no Download) dismissed via Escape, next candidate tried and succeeds")

    # 53: REGRESSION (2026-08-25 — found via a real live UPLOAD E2E). A
    # zero-size, off-screen phantom button (rect [x, y, 0, 0]) is a real
    # element but never actually clickable — it must never be tried as a
    # menu-trigger candidate at all, not even as a last resort.
    zero_size_trigger = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [-1160.88, 724.08, 0, 0]}
    real_trigger_53 = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [850, 20, 24, 24]}
    viewer_buttons_53 = {"buttons": [zero_size_trigger, real_trigger_53]}

    class _FakeAlwaysVisibleItemLocator:
        def __init__(self):
            self.first = self
        async def is_visible(self, timeout=None):
            return True
        async def click(self, timeout=None):
            pass

    class _FakeMenuPage53(_FakeMenuPage):
        def locator(self, sel):
            return _FakeAlwaysVisibleItemLocator()

    page_53 = _FakeMenuPage53()

    async def _fake_evaluate_menu_53(page, js, arg=None, timeout=10.0):
        page.click_count += 1
        return {"items": [{"text": "Download"}]}

    async def _fake_collect_downloads_53(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    mark_scan._evaluate = _fake_evaluate_menu_53
    mark_scan._collect_downloads = _fake_collect_downloads_53
    try:
        result_53 = asyncio.run(mark_scan._click_download_in_open_viewer(page_53, viewer_buttons_53, {"reached": True}))
    finally:
        mark_scan._evaluate = orig_evaluate_menu
        mark_scan._collect_downloads = orig_collect_downloads

    assert result_53["ok"] is True, result_53
    assert page_53.click_count == 1, page_53.click_count  # the zero-size phantom was never tried at all
    assert result_53["menu_trigger"] == real_trigger_53, result_53
    print("53. UPLOAD skips phantom buttons   -> a zero-size/off-screen 'button' is never tried as a menu-trigger candidate")

    # 54: REGRESSION (2026-08-25 — found via a real live UPLOAD E2E). A
    # message that resolved successfully moments earlier briefly failed
    # to re-resolve immediately after a close/reopen round. A bounded
    # rehydration retry (MESSAGE_REHYDRATION_ATTEMPTS) recovers rather
    # than treating the first "not found" as final.
    tile_54 = _FakeHashTile("video-content", _video_tile_html("VIDEOTILE54"))
    # Sequence: None (miss), None (miss), then 4 (found) — the THIRD
    # attempt succeeds, still within the bounded retry budget.
    mark_scan._find_message_index_by_data_id = _make_fake_find_idx_up([None, None, 4])
    try:
        page_54 = _FakeHashPage({4: _FakeHashMessageLocator([tile_54])}, video_mounted=True)
        orig_readiness = mark_scan._wait_for_video_readiness
        orig_click_dl = mark_scan._click_download_in_open_viewer
        orig_close = mark_scan._close_viewer
        mark_scan._wait_for_video_readiness = lambda page, **kw: asyncio.sleep(0, result={"reached": True})
        mark_scan._click_download_in_open_viewer = lambda page, vb, r: asyncio.sleep(0, result={"ok": True, "downloads": [{"ok": True, "_raw_bytes": b"X"}]})
        mark_scan._close_viewer = lambda page, vb: asyncio.sleep(0, result={"closed": True})
        try:
            dl_54 = asyncio.run(mark_scan._open_tile_viewer_and_download_hardened(
                page_54, "Talentgram MEDIA SPIKE TEST", "3B07252BFE7BC81FB956", _hash_of("VIDEOTILE54"), 0,
            ))
        finally:
            mark_scan._wait_for_video_readiness = orig_readiness
            mark_scan._click_download_in_open_viewer = orig_click_dl
            mark_scan._close_viewer = orig_close
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_up
    assert dl_54["ok"] is True, dl_54
    assert tile_54.click_count == 1, tile_54.click_count
    print("54. UPLOAD message rehydration     -> a transient post-close 'not found' recovers within the bounded rehydration retry")

    # ------------------------------------------------------------------
    # 55-60: mark-scanner scroll-to-bottom fix (2026-08-25). Real finding:
    # _dump_window's own "tail" checkpoint silently assumed the chat was
    # already scrolled to the bottom — a prior operation (e.g. UPLOAD's
    # own hardened video retries scrolling an OLDER message into view)
    # could leave the chat scrolled mid-history, and _open_group_chat's
    # fast path never corrects it. Every scan then only moved UPWARD from
    # wherever the chat happened to be, so a mark sent after that point
    # was permanently invisible — this is why 23 older marks kept
    # resolving while brand-new ones silently disappeared. Fix:
    # _scroll_to_true_bottom runs before the tail checkpoint, with a
    # bounded readiness loop (never a blind single scroll, never
    # unbounded). Never touches mark parsing, mention handling, hash
    # identity, album logic, UPLOAD's download path, or SEND's Forward
    # path — proven by tests 33-35c/36-54 above still passing unchanged.
    # ------------------------------------------------------------------
    class _FakeBottomPage:
        def __init__(self, has_container=True):
            self.has_container = has_container
            self.scroll_attempts = 0
            self.at_bottom_after_attempt = 1  # becomes true once this many scroll calls have happened
            self.waits = 0
        async def wait_for_timeout(self, ms):
            self.waits += 1

    orig_evaluate_bottom = mark_scan._evaluate

    # 55: starts mid-scroll -> reaches the true bottom in one attempt.
    page_55 = _FakeBottomPage()
    async def _fake_evaluate_55(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scroll_attempts += 1
            return page.has_container
        if js == mark_scan._BOTTOM_READINESS_CHECK_JS:
            at_bottom = page.scroll_attempts >= page.at_bottom_after_attempt
            return {"hasContainer": True, "atBottom": at_bottom, "scrollTop": 999, "scrollHeight": 1000, "clientHeight": 200}
        return None
    mark_scan._evaluate = _fake_evaluate_55
    try:
        result_55 = asyncio.run(mark_scan._scroll_to_true_bottom(page_55, "SEL"))
    finally:
        mark_scan._evaluate = orig_evaluate_bottom
    assert result_55["ok"] is True, result_55
    assert page_55.scroll_attempts == 1, page_55.scroll_attempts
    print("55. scanner scroll-to-bottom       -> mid-scroll chat reaches the true bottom in one bounded attempt")

    # 56: already at the bottom -> succeeds immediately, still calls the
    # SAME code path (no special-casing), behavior otherwise unchanged.
    page_56 = _FakeBottomPage()
    page_56.at_bottom_after_attempt = 1  # first scroll call already lands at bottom (no-op scroll, already there)
    async def _fake_evaluate_56(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scroll_attempts += 1
            return page.has_container
        if js == mark_scan._BOTTOM_READINESS_CHECK_JS:
            return {"hasContainer": True, "atBottom": True, "scrollTop": 800, "scrollHeight": 1000, "clientHeight": 200}
        return None
    mark_scan._evaluate = _fake_evaluate_56
    try:
        result_56 = asyncio.run(mark_scan._scroll_to_true_bottom(page_56, "SEL"))
    finally:
        mark_scan._evaluate = orig_evaluate_bottom
    assert result_56["ok"] is True, result_56
    assert page_56.scroll_attempts == 1, page_56.scroll_attempts
    print("56. scanner already at bottom      -> succeeds on the first check, no behavior change for the already-correct case")

    # 57: no scrollable container found -> fails immediately, never loops.
    page_57 = _FakeBottomPage(has_container=False)
    async def _fake_evaluate_57(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scroll_attempts += 1
            return False
        return None
    mark_scan._evaluate = _fake_evaluate_57
    try:
        result_57 = asyncio.run(mark_scan._scroll_to_true_bottom(page_57, "SEL"))
    finally:
        mark_scan._evaluate = orig_evaluate_bottom
    assert result_57["ok"] is False, result_57
    assert page_57.scroll_attempts == 1, page_57.scroll_attempts  # never retries a genuinely missing container
    print("57. scanner no container found     -> fails cleanly on the first attempt, never loops against nothing")

    # 58: never reaches bottom -> bounded to MAX_SCROLL_TO_BOTTOM_ATTEMPTS,
    # then fails cleanly rather than looping forever.
    page_58 = _FakeBottomPage()
    async def _fake_evaluate_58(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scroll_attempts += 1
            return True
        if js == mark_scan._BOTTOM_READINESS_CHECK_JS:
            return {"hasContainer": True, "atBottom": False, "scrollTop": 500, "scrollHeight": 2000, "clientHeight": 200}
        return None
    mark_scan._evaluate = _fake_evaluate_58
    try:
        result_58 = asyncio.run(mark_scan._scroll_to_true_bottom(page_58, "SEL"))
    finally:
        mark_scan._evaluate = orig_evaluate_bottom
    assert result_58["ok"] is False, result_58
    assert page_58.scroll_attempts == mark_scan.MAX_SCROLL_TO_BOTTOM_ATTEMPTS, page_58.scroll_attempts
    print("58. scanner never reaches bottom   -> bounded to MAX_SCROLL_TO_BOTTOM_ATTEMPTS, never an infinite loop")

    # 59/60: full _dump_window + _run_scan integration — a chat scrolled
    # mid-history (only an OLDER source+mark rendered) reveals a NEWER
    # source+mark (no @mention) only AFTER the scroll-to-bottom fix runs;
    # the older mark is still found too, and mention_lid is None for the
    # no-mention one.
    def _plain_video_html(data_id, seed):
        blob = (seed * 20)[:80]
        return (
            f'<div data-id="{data_id}" data-testid="conv-msg-{data_id}">'
            f'<div data-testid="video-content">'
            f'<div style="background-image: url(&quot;data:image/jpeg;base64,{blob}&quot;);"></div>'
            f'</div></div>'
        )

    def _reply_mark_html(data_id, mark_text, quoted_seed):
        quoted_blob = (quoted_seed * 20)[:80]
        return (
            f'<div data-id="{data_id}" data-testid="conv-msg-{data_id}">'
            f'<span data-testid="selectable-text">{mark_text}</span>'
            f'</div>',
            f'<div data-testid="quoted-message">'
            f'<div style="background-image: url(&quot;data:image/jpeg;base64,{quoted_blob}&quot;);"></div>'
            f'</div>',
        )

    old_src_html = _plain_video_html("OLDSRC01", "OLDVIDEOSEED")
    old_mark_html, old_quoted_html = _reply_mark_html("OLDMARKREPLY01", "mark CleanTest take 1", "OLDVIDEOSEED")
    new_src_html = _plain_video_html("NEWSRC01", "NEWVIDEOSEED")
    new_mark_html, new_quoted_html = _reply_mark_html("NEWMARKREPLY01", "mark CleanScan Test Take 1", "NEWVIDEOSEED")

    message_by_id = {
        "OLDSRC01": {"messageHtml": old_src_html, "quotedHtml": None},
        "OLDMARKREPLY01": {"messageHtml": old_mark_html, "quotedHtml": old_quoted_html},
        "NEWSRC01": {"messageHtml": new_src_html, "quotedHtml": None},
        "NEWMARKREPLY01": {"messageHtml": new_mark_html, "quotedHtml": new_quoted_html},
    }
    before_ids = ["OLDSRC01", "OLDMARKREPLY01"]  # mid-scroll: only the OLDER pair is rendered
    after_ids = ["OLDSRC01", "OLDMARKREPLY01", "NEWSRC01", "NEWMARKREPLY01"]  # true bottom reveals the newer pair too

    class _FakeScrollConvLocator:
        def __init__(self, page):
            self._page = page
        async def count(self):
            return len(self._page.after_ids if self._page.scrolled else self._page.before_ids)

    class _FakeScrollPage:
        def __init__(self, before_ids, after_ids):
            self.before_ids = before_ids
            self.after_ids = after_ids
            self.scrolled = False
            self.scroll_calls = 0
        def locator(self, sel):
            return _FakeScrollConvLocator(self)
        async def wait_for_timeout(self, ms):
            pass

    async def _fake_evaluate_59(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scroll_calls += 1
            page.scrolled = True
            return True
        if js == mark_scan._BOTTOM_READINESS_CHECK_JS:
            return {"hasContainer": True, "atBottom": True}
        if js == mark_scan._DOM_DUMP_JS:
            sel, idx = arg
            ids = page.after_ids if page.scrolled else page.before_ids
            if idx >= len(ids):
                return None
            return message_by_id[ids[idx]]
        if js == mark_scan._SCROLL_STEP_JS:
            return {"moved": False}
        return None

    page_59 = _FakeScrollPage(before_ids, after_ids)
    orig_resolve_scope_59 = mark_scan.sender._resolve_scope
    async def _fake_resolve_scope_59(page):
        return "#main"
    mark_scan._evaluate = _fake_evaluate_59
    mark_scan.sender._resolve_scope = _fake_resolve_scope_59
    try:
        window_59 = asyncio.run(mark_scan._dump_window(page_59, "Talentgram MEDIA SPIKE TEST", 300))
    finally:
        mark_scan._evaluate = orig_evaluate_bottom
        mark_scan.sender._resolve_scope = orig_resolve_scope_59
    found_ids_59 = set()
    for item in window_59:
        html = item.get("messageHtml") or ""
        data_id = mark_scan._own_data_id(html)
        if data_id:
            found_ids_59.add(data_id)
    assert page_59.scroll_calls == 1, page_59.scroll_calls  # scrolled to bottom exactly once, before capturing
    assert found_ids_59 == set(after_ids), found_ids_59  # BOTH the older and the newer pair are found
    print("59. scanner reveals hidden newer content -> _dump_window scrolls to bottom BEFORE capturing, finds messages invisible before the fix")

    # 60: run the mark-extraction logic (mirroring _run_scan's own Pass
    # 1/2) over that same window — the older mark AND the newer no-
    # mention mark both resolve, with the correct source identity/hash,
    # and mention_lid=None for the no-mention one.
    sources_by_hash_60: Dict[str, Dict[str, Any]] = {}
    for item in window_59:
        html = item.get("messageHtml") or ""
        if item.get("quotedHtml"):
            continue
        data_id = mark_scan._own_data_id(html)
        if not data_id:
            continue
        media_type = mark_scan._media_type(html)
        if not media_type:
            continue
        h = mark_scan._smallest_hash(html)
        if h:
            sources_by_hash_60[h] = {"source_message_id": data_id, "source_media_type": media_type}
    marks_60 = []
    for item in window_59:
        quoted_html = item.get("quotedHtml")
        if not quoted_html:
            continue
        html = item.get("messageHtml") or ""
        mark_text = mark_scan._mark_text(html)
        if not mark_text:
            continue
        quoted_hash = mark_scan._smallest_hash(quoted_html)
        source = sources_by_hash_60.get(quoted_hash)
        marks_60.append({
            "mention_lid": mark_scan._mention_lid(html), "mark_text": mark_text,
            "resolved_source_message_id": (source or {}).get("source_message_id"),
            "source_thumbnail_hash": quoted_hash,
        })
    by_text_60 = {m["mark_text"]: m for m in marks_60}
    assert "mark CleanTest take 1" in by_text_60, by_text_60  # older mark still found
    assert "mark CleanScan Test Take 1" in by_text_60, by_text_60  # newer no-mention mark now found
    new_mark_60 = by_text_60["mark CleanScan Test Take 1"]
    assert new_mark_60["mention_lid"] is None, new_mark_60  # no @mention -> mention_lid is None, never required
    assert new_mark_60["resolved_source_message_id"] == "NEWSRC01", new_mark_60  # exact source_message_id captured
    assert new_mark_60["source_thumbnail_hash"] is not None, new_mark_60  # exact source_thumbnail_hash captured
    old_mark_60 = by_text_60["mark CleanTest take 1"]
    assert old_mark_60["resolved_source_message_id"] == "OLDSRC01", old_mark_60  # older mark's identity unaffected
    print("60. old + new marks both resolve   -> no-mention mark: mention_lid=None, exact source_message_id/hash captured; older mark unaffected")

    print("61. UPLOAD/SEND untouched (scanner)-> tests 33-35c (SEND) and 36-54 (UPLOAD) above pass unchanged from the _dump_window fix alone")

    # ------------------------------------------------------------------
    # 62-67: same root cause, shared message-resolution layer (2026-08-25).
    # A real live UPLOAD run against fresh CleanScan Test marks hit
    # "message_not_found" inside _open_tile_viewer_and_download_hardened
    # -> _resolve_video_tile_by_hash -> _find_message_index_by_data_id — a
    # COMPLETELY SEPARATE function from _dump_window (used by the
    # scanner), sharing the identical false premise ("the group was just
    # opened, which WhatsApp scrolls to the bottom by default") in its own
    # docstring. _find_message_index_by_data_id now calls the SAME
    # _scroll_to_true_bottom helper before its own "search current tail"
    # step — no new JS, no duplicated logic, no change to its existing
    # upward _ensure_history_loaded fallback for genuinely older targets.
    # This is used by BOTH UPLOAD's hardened retry loop and SEND's own
    # _resolve_video_tile_locator, so both benefit — SEND's own Forward/
    # destination/caption/send-click logic is completely untouched (proven
    # by tests 33-35c above still passing unchanged).
    # ------------------------------------------------------------------
    class _FakeIdxTile:
        def __init__(self, data_id):
            self._data_id = data_id
        async def get_attribute(self, name):
            return f"conv-msg-{self._data_id}"

    class _FakeIdxLocator:
        def __init__(self, ids):
            self._ids = ids
        async def count(self):
            return len(self._ids)
        def nth(self, i):
            return _FakeIdxTile(self._ids[i])

    class _FakeIdxPage:
        def __init__(self, before_ids, after_ids, older_ids=None):
            self.before_ids = before_ids
            self.after_ids = after_ids
            self.older_ids = older_ids or []  # only revealed via _ensure_history_loaded (scroll UP)
            self.scrolled_to_bottom = False
            self.history_loaded = False
        def locator(self, sel):
            ids = self.after_ids if self.scrolled_to_bottom else self.before_ids
            if self.history_loaded:
                ids = self.older_ids + ids
            return _FakeIdxLocator(ids)
        async def wait_for_timeout(self, ms):
            pass

    orig_resolve_scope_idx = mark_scan.sender._resolve_scope
    async def _fake_resolve_scope_idx(page):
        return "#main"

    async def _fake_evaluate_idx(page, js, arg=None, timeout=10.0):
        if js == mark_scan._SCROLL_TO_BOTTOM_JS:
            page.scrolled_to_bottom = True
            return True
        if js == mark_scan._BOTTOM_READINESS_CHECK_JS:
            return {"hasContainer": True, "atBottom": True}
        if js == mark_scan._LOAD_HISTORY_JS:
            page.history_loaded = True
            ids = page.older_ids + (page.after_ids if page.scrolled_to_bottom else page.before_ids)
            return len(ids)
        return None

    mark_scan.sender._resolve_scope = _fake_resolve_scope_idx
    mark_scan._evaluate = _fake_evaluate_idx

    # 62: chat starts scrolled mid-history (target only rendered AFTER
    # scrolling to the bottom) -> resolver moves to bottom and finds it.
    page_62 = _FakeIdxPage(before_ids=["OLD1", "OLD2"], after_ids=["OLD1", "OLD2", "NEW1"])
    try:
        idx_62 = asyncio.run(mark_scan._find_message_index_by_data_id(page_62, "Talentgram MEDIA SPIKE TEST", "NEW1"))
    finally:
        pass
    assert idx_62 == 2, idx_62
    assert page_62.scrolled_to_bottom is True, "must have scrolled to bottom to find it"
    print("62. resolver scrolls to bottom     -> mid-scroll chat: target only found after moving to the true bottom")

    # 63: chat already at the bottom (before == after) -> behaves
    # normally, same result either way.
    page_63 = _FakeIdxPage(before_ids=["OLD1", "TARGET1"], after_ids=["OLD1", "TARGET1"])
    idx_63 = asyncio.run(mark_scan._find_message_index_by_data_id(page_63, "Talentgram MEDIA SPIKE TEST", "TARGET1"))
    assert idx_63 == 1, idx_63
    print("63. resolver already at bottom     -> no functional difference, target found exactly as before")

    # 64: target is OLDER than the current rendered window (not present
    # even after scrolling to the true bottom) -> the EXISTING upward
    # _ensure_history_loaded fallback still finds it, completely unchanged.
    page_64 = _FakeIdxPage(before_ids=["MID1"], after_ids=["MID1"], older_ids=["ANCIENT1"])
    idx_64 = asyncio.run(mark_scan._find_message_index_by_data_id(page_64, "Talentgram MEDIA SPIKE TEST", "ANCIENT1"))
    assert idx_64 == 0, idx_64
    assert page_64.history_loaded is True, "upward history-load fallback must still have run"
    print("64. older target still found       -> existing upward _ensure_history_loaded fallback unchanged for genuinely older messages")

    # 65: the SAME data_id sits at a DIFFERENT index than a previous call
    # would have seen (new messages pushed it down) -> still found by its
    # own data-id, never a stale/wrong positional index.
    page_65 = _FakeIdxPage(before_ids=["X1"], after_ids=["A", "B", "C", "TARGET2"])
    idx_65 = asyncio.run(mark_scan._find_message_index_by_data_id(page_65, "Talentgram MEDIA SPIKE TEST", "TARGET2"))
    assert idx_65 == 3, idx_65
    print("65. index drift handled            -> found by its own data-id at whatever index it currently sits, never a stale one")

    # 66: a genuinely missing data_id -> returns None, never substitutes
    # a neighboring message.
    page_66 = _FakeIdxPage(before_ids=["A", "B"], after_ids=["A", "B", "C"])
    idx_66 = asyncio.run(mark_scan._find_message_index_by_data_id(page_66, "Talentgram MEDIA SPIKE TEST", "GENUINELY_MISSING"))
    assert idx_66 is None, idx_66
    print("66. genuinely missing target       -> returns None cleanly, never selects a neighboring message")

    mark_scan.sender._resolve_scope = orig_resolve_scope_idx
    mark_scan._evaluate = orig_evaluate_bottom

    print("67. no-mention Mark unaffected     -> already proven end-to-end in test 60 (mention_lid=None, exact source_message_id/hash)")
    print("68. UPLOAD/SEND video+photo unaffected -> tests 36-54 (UPLOAD hardening) and 33-35c (SEND) above pass unchanged with this fix applied")

    # 69-72: direct Download-button fix (2026-08-25). A read-only 5-checkpoint
    # diagnostic proved WhatsApp Web exposes Download as its OWN standalone
    # header button (ariaLabel="Download"/svgTitle="ic-download") sitting
    # alongside "Menu" — present and stable from the moment the viewer opens
    # through readiness, a settle period, and a close/reopen — never hidden
    # inside a submenu. The old code only ever searched for a menu TRIGGER
    # then required a role="menu" popup to appear; clicking a direct-action
    # button never produces one, so it kept cycling through every other
    # button on the page until exhausted (explaining the real E2E's "You"
    # button failure). These tests exercise the new direct-button check.
    class _FakeDownloadMouse:
        def __init__(self):
            self.clicks = []
        async def click(self, x, y, button=None):
            self.clicks.append((x, y, button))

    class _FakeDownloadKeyboard:
        def __init__(self):
            self.pressed = []
        async def press(self, key):
            self.pressed.append(key)

    class _FakeDownloadPage:
        def __init__(self):
            self.mouse = _FakeDownloadMouse()
            self.keyboard = _FakeDownloadKeyboard()
        async def wait_for_timeout(self, ms):
            pass
        def locator(self, sel):
            raise AssertionError("menu-trigger/menu-item locator path must not run when a direct Download button exists")

    download_btn_69 = {"ariaLabel": "Download", "dataIcon": None, "testid": None, "svgTitle": "ic-download", "rect": [1126, 10, 40, 40]}
    menu_btn_69 = {"ariaLabel": "Menu", "dataIcon": None, "testid": None, "svgTitle": "ic-more-vert", "rect": [1174, 10, 40, 40]}
    viewer_buttons_69 = {"buttons": [menu_btn_69, download_btn_69]}

    page_69 = _FakeDownloadPage()

    async def _evaluate_must_not_run_69(page, js, arg=None, timeout=10.0):
        raise AssertionError("menu-dump evaluate must not run when a direct Download button is clicked")

    async def _fake_collect_downloads_69(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    orig_evaluate_69, orig_collect_69 = mark_scan._evaluate, mark_scan._collect_downloads
    mark_scan._evaluate, mark_scan._collect_downloads = _evaluate_must_not_run_69, _fake_collect_downloads_69
    try:
        result_69 = asyncio.run(mark_scan._click_download_in_open_viewer(page_69, viewer_buttons_69, {"reached": True}))
    finally:
        mark_scan._evaluate, mark_scan._collect_downloads = orig_evaluate_69, orig_collect_69

    assert result_69["ok"] is True, result_69
    assert result_69["stage_used"] == "direct_download_button", result_69
    assert result_69["menu_trigger"] == download_btn_69, result_69
    assert page_69.mouse.clicks == [(1146.0, 30.0, "left")], page_69.mouse.clicks  # clicked the Download button's own center, not Menu's
    print("69. UPLOAD direct Download button  -> clicked directly by identity (ariaLabel=Download); menu-trigger/menu-dump path never runs")

    # 70: the direct-Download-button check must fall through to the
    # EXISTING menu-trigger-and-submenu flow, completely unchanged, when no
    # such button exists (a different WhatsApp UI variant) — reusing the
    # exact same fixture shape as test 52's self-healing scenario.
    class _FakeItemLocator70:
        def __init__(self):
            self.first = self
        async def is_visible(self, timeout=None):
            return True
        async def click(self, timeout=None):
            pass

    class _FakeMenuOnlyPage70:
        def __init__(self):
            self.mouse = _FakeDownloadMouse()
            self.keyboard = _FakeDownloadKeyboard()
        async def wait_for_timeout(self, ms):
            pass
        def locator(self, sel):
            return _FakeItemLocator70()

    menu_only_trigger_70 = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [850, 20, 24, 24]}
    viewer_buttons_70 = {"buttons": [menu_only_trigger_70]}  # no Download-labeled button at all

    page_70 = _FakeMenuOnlyPage70()

    async def _fake_evaluate_70(page, js, arg=None, timeout=10.0):
        return {"items": [{"text": "Download"}]}

    async def _fake_collect_downloads_70(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    orig_evaluate_70, orig_collect_70 = mark_scan._evaluate, mark_scan._collect_downloads
    mark_scan._evaluate, mark_scan._collect_downloads = _fake_evaluate_70, _fake_collect_downloads_70
    try:
        result_70 = asyncio.run(mark_scan._click_download_in_open_viewer(page_70, viewer_buttons_70, {"reached": True}))
    finally:
        mark_scan._evaluate, mark_scan._collect_downloads = orig_evaluate_70, orig_collect_70

    assert result_70["ok"] is True, result_70
    assert "stage_used" not in result_70, result_70  # took the OLD menu-trigger path, not the new direct one
    print("70. UPLOAD menu-trigger fallback    -> unchanged when no direct Download button exists (defensive fallback for a different UI variant)")

    # 71: a zero-size/off-screen "Download"-labeled element is a real DOM
    # node but never actually clickable — must be skipped by the SAME
    # positive-rect filter as every other candidate, never clicked, and
    # must still fall through correctly to the menu-trigger flow.
    phantom_download_71 = {"ariaLabel": "Download", "dataIcon": None, "testid": None, "svgTitle": "ic-download", "rect": [-100, 700, 0, 0]}
    menu_trigger_71 = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [850, 20, 24, 24]}
    viewer_buttons_71 = {"buttons": [phantom_download_71, menu_trigger_71]}

    page_71 = _FakeMenuOnlyPage70()

    async def _fake_evaluate_71(page, js, arg=None, timeout=10.0):
        return {"items": [{"text": "Download"}]}

    async def _fake_collect_downloads_71(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    orig_evaluate_71, orig_collect_71 = mark_scan._evaluate, mark_scan._collect_downloads
    mark_scan._evaluate, mark_scan._collect_downloads = _fake_evaluate_71, _fake_collect_downloads_71
    try:
        result_71 = asyncio.run(mark_scan._click_download_in_open_viewer(page_71, viewer_buttons_71, {"reached": True}))
    finally:
        mark_scan._evaluate, mark_scan._collect_downloads = orig_evaluate_71, orig_collect_71

    assert result_71["ok"] is True, result_71
    assert "stage_used" not in result_71, result_71  # the phantom was skipped, real work happened via the menu-trigger path
    assert (-100.0, 700.0, "left") not in page_71.mouse.clicks, page_71.mouse.clicks  # the phantom's own coordinates were never clicked
    assert page_71.mouse.clicks == [(862.0, 32.0, "left")], page_71.mouse.clicks  # only the real menu trigger was clicked
    print("71. UPLOAD skips phantom Download   -> a zero-size/off-screen 'Download'-labeled element is never clicked; falls through cleanly")

    # 72: a button whose label merely CONTAINS "download" (e.g. an album's
    # "Download all") is NOT an exact identity match and must never be
    # substituted for the real per-item Download button — only an exact
    # aria-label "Download" or svg-title "ic-download" qualifies.
    download_all_72 = {"ariaLabel": "Download all", "dataIcon": None, "testid": None, "svgTitle": None, "rect": [1126, 10, 40, 40]}
    menu_trigger_72 = {"ariaLabel": None, "dataIcon": None, "testid": None, "svgTitle": "menu", "rect": [850, 20, 24, 24]}
    viewer_buttons_72 = {"buttons": [download_all_72, menu_trigger_72]}

    page_72 = _FakeMenuOnlyPage70()

    async def _fake_evaluate_72(page, js, arg=None, timeout=10.0):
        return {"items": [{"text": "Download"}]}

    async def _fake_collect_downloads_72(page, trigger, window_s=25.0, quiet_s=3.0):
        await trigger()
        return [{"ok": True, "_raw_bytes": b"X"}]

    orig_evaluate_72, orig_collect_72 = mark_scan._evaluate, mark_scan._collect_downloads
    mark_scan._evaluate, mark_scan._collect_downloads = _fake_evaluate_72, _fake_collect_downloads_72
    try:
        result_72 = asyncio.run(mark_scan._click_download_in_open_viewer(page_72, viewer_buttons_72, {"reached": True}))
    finally:
        mark_scan._evaluate, mark_scan._collect_downloads = orig_evaluate_72, orig_collect_72

    assert result_72["ok"] is True, result_72
    assert "stage_used" not in result_72, result_72  # "Download all" is not an exact match -> old menu-trigger path handled it instead
    assert (1146.0, 30.0, "left") not in page_72.mouse.clicks, page_72.mouse.clicks  # "Download all"'s own coordinates never clicked directly
    print("72. UPLOAD exact-match only         -> a 'Download all' label is never treated as the direct per-item Download button")

    # ------------------------------------------------------------------
    # 73-77: SEND ordering/idempotency/speed (2026-08-26, Phase 5/6/7) —
    # _run_send is the single continuous operation that sequences Takes ->
    # Intro -> Form -> Pictures -> completion marker; the backend
    # orchestrator (services/media_assignment_worker.py, tested separately
    # in backend/tests/test_media_send.py) pre-sorts send_targets and
    # computes form_insert_index/send_marker_on_success, so these tests
    # only need to prove _run_send OBEYS those instructions faithfully.
    # ------------------------------------------------------------------

    class _FakePage73:
        pass

    def _install_send_fakes(*, forward_results=None, text_results=None):
        """Records call order across native-forward items and text
        (form/marker) sends into one shared list, `calls`, so ordering can
        be asserted directly — mirrors the module-attribute monkeypatch
        style every other test in this file already uses."""
        calls = []
        forward_results = forward_results or {}
        text_results = text_results or {}

        async def _fake_open_group_chat(page, group_name):
            calls.append(("open_group", group_name))
            return "OPENED"

        async def _fake_forward(page, group_name, target, item_label="", source_type="group"):
            calls.append(("forward", target["source_message_id"]))
            result = forward_results.get(target["source_message_id"], {"ok": True})
            return {"source_message_id": target["source_message_id"], **result}

        async def _fake_text(page, destination_group, message, *, destination_type="group"):
            # destination_type accepted (Production feature: the talent
            # acknowledgement passes "number" for a phone source) but not
            # part of the recorded tuple shape — every pre-existing
            # assertion in this file asserts on the plain ("text", message)
            # shape and none of them need to distinguish it; a dedicated
            # ack test below checks destination_type directly instead.
            calls.append(("text", message))
            return text_results.get(message, {"ok": True})

        return calls, _fake_open_group_chat, _fake_forward, _fake_text

    def _send_target(mid, role, take=None):
        return {
            "source_message_id": mid, "media_role": role, "take_number": take,
            "source_media_type": "image", "album_tile_index": 0, "destination_group": "Dest Group",
            "caption": mid,
        }

    # 73: fixed ordering — Takes then Intro, FORM inserted at
    # form_insert_index (between Intro and Pictures), Pictures last, then
    # the ☑️ marker — never scan/discovery order.
    calls_73, open_73, forward_73, text_73 = _install_send_fakes()
    orig_open_73, orig_forward_73, orig_text_73 = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_73, forward_73, text_73
    try:
        req_73 = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Vaseline",
            "send_targets": [
                _send_target("take1", "take", 1), _send_target("intro1", "intro"), _send_target("pic1", "photos"),
            ],
            "form_insert_index": 2, "form_message": "SEND FORM TEXT", "send_marker_on_success": True,
        }
        result_73 = asyncio.run(mark_scan._run_send(_FakePage73(), req_73))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_open_73, orig_forward_73, orig_text_73

    assert all(r["ok"] for r in result_73["results"]), result_73
    assert result_73["form_send_result"]["ok"] is True, result_73
    assert result_73["marker_result"]["ok"] is True, result_73
    # Talent acknowledgement (Production feature) — sent after Pictures,
    # BEFORE the ☑️ marker (see calls_73's own ordering below).
    assert result_73["ack_result"]["ok"] is True, result_73
    assert calls_73 == [
        ("open_group", "Source Group"),
        ("forward", "take1"), ("forward", "intro1"),
        ("text", "SEND FORM TEXT"),
        ("forward", "pic1"),
        ("text", "Thanks, shared for Vaseline."),
        ("text", mark_scan.SEND_MARKER_TEXT),
    ], calls_73
    print("73. SEND fixed ordering             -> Takes -> Intro -> Form -> Pictures -> marker, form/marker never reshuffled relative to media")

    # 74: no takes, no intro (form_insert_index=0) and no pictures at all
    # — form still sends (at position 0) and the marker still sends last,
    # even though send_targets is completely empty. The source group is
    # never opened when there is nothing to forward.
    calls_74, open_74, forward_74, text_74 = _install_send_fakes()
    orig_open_74, orig_forward_74, orig_text_74 = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_74, forward_74, text_74
    try:
        req_74 = {
            "group_name": "Source Group", "destination_group": "Dest Group",
            "send_targets": [], "form_insert_index": 0, "form_message": "ONLY THE FORM", "send_marker_on_success": True,
        }
        result_74 = asyncio.run(mark_scan._run_send(_FakePage73(), req_74))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_open_74, orig_forward_74, orig_text_74

    assert result_74["results"] == [], result_74
    assert result_74["form_send_result"]["ok"] is True, result_74
    assert result_74["marker_result"]["ok"] is True, result_74
    assert calls_74 == [("text", "ONLY THE FORM"), ("text", mark_scan.SEND_MARKER_TEXT)], calls_74
    assert not any(c[0] == "open_group" for c in calls_74), calls_74  # source group never opened -- nothing to forward
    print("74. SEND skips source group          -> no media to forward means the source group is never opened, only form+marker sent")

    # 75: one media item fails -> the marker is WITHHELD even though
    # send_marker_on_success=True and the form succeeded — a single
    # failure anywhere in the run means the marker is never sent early.
    calls_75, open_75, forward_75, text_75 = _install_send_fakes(forward_results={"take1": {"ok": False, "error": "boom"}})
    orig_open_75, orig_forward_75, orig_text_75 = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_75, forward_75, text_75
    try:
        req_75 = {
            "group_name": "Source Group", "destination_group": "Dest Group",
            "send_targets": [_send_target("take1", "take", 1)],
            "form_insert_index": 0, "form_message": "FORM", "send_marker_on_success": True,
        }
        result_75 = asyncio.run(mark_scan._run_send(_FakePage73(), req_75))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_open_75, orig_forward_75, orig_text_75

    assert result_75["results"][0]["ok"] is False, result_75
    assert result_75["form_send_result"]["ok"] is True, result_75
    assert result_75["marker_result"] is None, result_75  # withheld -- never sent when a media item failed
    assert not any(c == ("text", mark_scan.SEND_MARKER_TEXT) for c in calls_75), calls_75
    print("75. SEND marker withheld on failure  -> one failed media item means the ☑️ marker is never sent this run")

    # 76: send_marker_on_success=False (backend already recorded the
    # marker as sent in an earlier attempt) -> never sent again even
    # though everything else succeeds this run.
    calls_76, open_76, forward_76, text_76 = _install_send_fakes()
    orig_open_76, orig_forward_76, orig_text_76 = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_76, forward_76, text_76
    try:
        req_76 = {
            "group_name": "Source Group", "destination_group": "Dest Group",
            "send_targets": [_send_target("pic1", "photos")],
            "form_insert_index": 0, "form_message": None, "send_marker_on_success": False,
        }
        result_76 = asyncio.run(mark_scan._run_send(_FakePage73(), req_76))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_open_76, orig_forward_76, orig_text_76

    assert result_76["marker_result"] is None, result_76
    assert not any(c == ("text", mark_scan.SEND_MARKER_TEXT) for c in calls_76), calls_76
    print("76. SEND marker not re-sent          -> send_marker_on_success=False means the marker is never sent again, idempotent across retries")

    # ------------------------------------------------------------------
    # 76b-76e (Production feature — talent acknowledgement): "Thanks,
    # shared for <project>." into the SOURCE chat, only after a full
    # success, before the ☑️ marker, never on partial failure or a
    # marker-only resume.
    # ------------------------------------------------------------------
    calls_76b, open_76b, forward_76b, text_76b = _install_send_fakes()
    orig_o76b, orig_f76b, orig_t76b = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_76b, forward_76b, text_76b
    try:
        req_76b = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Airtel Kick Boxing",
            "send_targets": [_send_target("take1", "take", 1)],
            "form_insert_index": 1, "form_message": "FORM", "send_marker_on_success": True,
        }
        result_76b = asyncio.run(mark_scan._run_send(_FakePage73(), req_76b))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_o76b, orig_f76b, orig_t76b
    assert result_76b["ack_result"]["ok"] is True, result_76b
    assert ("text", "Thanks, shared for Airtel Kick Boxing.") in calls_76b, calls_76b
    # Sent before the marker, into the SOURCE group (not Dest Group).
    ack_idx = calls_76b.index(("text", "Thanks, shared for Airtel Kick Boxing."))
    marker_idx = calls_76b.index(("text", mark_scan.SEND_MARKER_TEXT))
    assert ack_idx < marker_idx, calls_76b
    print("76b. Talent acknowledgement sent     -> 'Thanks, shared for <project>.' sent to source group, before the ☑️ marker")

    # 76c: partial failure (one media item fails) -> acknowledgement is
    # NEVER sent, even though the form succeeded — never falsely tell the
    # talent everything was shared.
    calls_76c, open_76c, forward_76c, text_76c = _install_send_fakes(forward_results={"take1": {"ok": False, "error": "boom"}})
    orig_o76c, orig_f76c, orig_t76c = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_76c, forward_76c, text_76c
    try:
        req_76c = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Airtel Kick Boxing",
            "send_targets": [_send_target("take1", "take", 1)],
            "form_insert_index": 0, "form_message": "FORM", "send_marker_on_success": True,
        }
        result_76c = asyncio.run(mark_scan._run_send(_FakePage73(), req_76c))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_o76c, orig_f76c, orig_t76c
    assert result_76c["ack_result"] is None, result_76c
    assert not any(c[0] == "text" and c[1].startswith("Thanks, shared") for c in calls_76c), calls_76c
    print("76c. Ack withheld on partial failure -> talent is never told 'shared' when a media item actually failed")

    # 76d: marker-only resume (send_targets empty, e.g. everything already
    # forwarded in an earlier run and only the ☑️ marker is retried) ->
    # acknowledgement is NEVER re-sent — it already went out on the
    # earlier run that actually did the forwarding.
    calls_76d, open_76d, forward_76d, text_76d = _install_send_fakes()
    orig_o76d, orig_f76d, orig_t76d = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_76d, forward_76d, text_76d
    try:
        req_76d = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Airtel Kick Boxing",
            "send_targets": [], "form_insert_index": 0, "form_message": None, "send_marker_on_success": True,
        }
        result_76d = asyncio.run(mark_scan._run_send(_FakePage73(), req_76d))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_o76d, orig_f76d, orig_t76d
    assert result_76d["ack_result"] is None, result_76d
    assert not any(c[0] == "text" and c[1].startswith("Thanks, shared") for c in calls_76d), calls_76d
    print("76d. Ack not re-sent on marker-only resume -> zero new media/form this run means the earlier run's ack already covered it")

    # 76e: phone-source SEND (SEND Path B) -> the acknowledgement uses
    # destination_type="number", exactly like _open_source_chat's own
    # group/phone dispatch for OPENING the source chat.
    captured_dtype = {}

    async def _fake_text_capture_dtype(page, destination_group, message, *, destination_type="group"):
        if message.startswith("Thanks, shared"):
            captured_dtype["value"] = destination_type
        return {"ok": True}

    async def _fake_open_source_phone(page, source_type, group_name):
        return "OPENED"

    _, _, forward_76e_fake, _ = _install_send_fakes()
    orig_open_source_76e = mark_scan._open_source_chat
    orig_forward_76e = mark_scan._send_one_target_native_forward
    orig_text_76e = mark_scan._send_text_message
    mark_scan._open_source_chat = _fake_open_source_phone
    mark_scan._send_one_target_native_forward = forward_76e_fake
    mark_scan._send_text_message = _fake_text_capture_dtype
    try:
        req_76e = {
            "group_name": "85100696", "source_type": "phone", "destination_group": "Dest Group",
            "project_label": "Airtel Kick Boxing", "send_targets": [_send_target("take1", "take", 1)],
            "form_insert_index": 1, "form_message": "FORM", "send_marker_on_success": False,
        }
        result_76e = asyncio.run(mark_scan._run_send(_FakePage73(), req_76e))
    finally:
        mark_scan._open_source_chat = orig_open_source_76e
        mark_scan._send_one_target_native_forward = orig_forward_76e
        mark_scan._send_text_message = orig_text_76e
    assert result_76e["ack_result"]["ok"] is True, result_76e
    assert captured_dtype.get("value") == "number", captured_dtype
    print("76e. Ack uses phone destination_type -> a phone-source SEND acknowledges via destination_type='number', same as opening it")

    # ------------------------------------------------------------------
    # 77-78 (2026-08-27, real production incident — Siddhi Bankhele / TVS
    # Jupiter live SEND): the forward composer's caption box occasionally
    # isn't mounted yet on the very first check (video forward dialogs
    # render measurably slower), and a caption/send failure was leaving
    # the Forward dialog open, which then made the UNRELATED next
    # operation (opening the destination chat to send the form) fail too.
    # ------------------------------------------------------------------
    class _FakeLocator77:
        def __init__(self, click_results):
            self._click_results = list(click_results)
            self.click_calls = 0
            self.typed = None

        @property
        def first(self):
            return self

        async def click(self, timeout=5000):
            self.click_calls += 1
            result = self._click_results[min(self.click_calls, len(self._click_results)) - 1]
            if isinstance(result, Exception):
                raise result
            return result

        async def type(self, text, delay=10):
            self.typed = text

    class _FakePage77:
        def __init__(self, locator):
            self._locator = locator

        def locator(self, selector):
            return self._locator

        async def wait_for_timeout(self, ms):
            pass

    # 77: the caption box's click fails twice (simulating a not-yet-mounted
    # composer), then succeeds on the third bounded attempt -- overall
    # result is still success, proving this is a retry loop, not a
    # single shot.
    locator_77 = _FakeLocator77([TimeoutError("not ready"), TimeoutError("not ready"), None])
    page_77 = _FakePage77(locator_77)

    async def _fake_find_and_click_send_77(page, allow_enter_fallback=False):
        return "[aria-label^=\"Send\"][role=\"button\"]"

    orig_send_77 = sender._find_and_click_send
    sender._find_and_click_send = _fake_find_and_click_send_77
    try:
        result_77 = asyncio.run(mark_scan._enter_forward_caption_and_send(page_77, "a caption"))
    finally:
        sender._find_and_click_send = orig_send_77

    assert result_77["ok"] is True, result_77
    assert locator_77.click_calls == 3, locator_77.click_calls  # two failures, then success -- not a single shot
    assert locator_77.typed == "a caption", locator_77.typed
    print("77. SEND caption entry retries      -> a not-yet-mounted composer box is retried up to 3 bounded attempts, not a single 5s shot")

    # 77b (2026-08-27, real production incident): a video whose forward
    # preview already carries SOME existing caption shows a "Remove
    # caption" (X) control instead of our expected empty compose box --
    # confirmed via a real dialog dump (no append-message-compose-box at
    # all). The retry loop must clear it (once) and then find the box.
    class _FakePage77b(_FakePage77):
        def __init__(self, locator):
            super().__init__(locator)
            self.clicks = []

        class _Mouse:
            def __init__(self, outer):
                self._outer = outer

            async def click(self, x, y, button="left"):
                self._outer.clicks.append((x, y, button))

    locator_77b = _FakeLocator77([TimeoutError("no compose box yet"), None])
    page_77b = _FakePage77b(locator_77b)
    page_77b.mouse = _FakePage77b._Mouse(page_77b)
    remove_caption_dump = {"dialogFound": True, "buttons": [{"ariaLabel": "Remove caption", "testid": None, "rect": [818, 595, 32, 32]}]}

    async def _fake_evaluate_77b(page, js, arg=None, timeout=10.0):
        return remove_caption_dump

    orig_evaluate_77b = mark_scan._evaluate
    orig_send_77b = sender._find_and_click_send
    mark_scan._evaluate = _fake_evaluate_77b
    sender._find_and_click_send = _fake_find_and_click_send_77
    try:
        result_77b = asyncio.run(mark_scan._enter_forward_caption_and_send(page_77b, "a caption"))
    finally:
        mark_scan._evaluate = orig_evaluate_77b
        sender._find_and_click_send = orig_send_77b

    assert result_77b["ok"] is True, result_77b
    assert page_77b.clicks == [(834.0, 611.0, "left")], page_77b.clicks  # center of the Remove caption button's rect
    assert locator_77b.typed == "a caption", locator_77b.typed
    print("77b. SEND clears existing caption   -> a 'Remove caption' control is cleared once, revealing the real compose box, before retrying")

    # 78-80 (2026-08-27): _ensure_forward_dialog_closed -- a failed
    # caption/send must never leave the Forward dialog open to poison the
    # NEXT unrelated operation (proven live: this exact gap made the
    # form's own destination-chat-open fail right after an intro
    # forward's caption entry failed -- a single unverified Escape press
    # was not enough, the dialog was still open 40+ seconds later).
    class _FakePage78:
        def __init__(self, dump_sequence):
            self._dump_sequence = list(dump_sequence)
            self.dump_calls = 0
            self.escape_presses = 0
            self.clicks = []
            self.keyboard = self

        async def press(self, key):
            if key == "Escape":
                self.escape_presses += 1

        async def wait_for_timeout(self, ms):
            pass

        class _Mouse:
            def __init__(self, outer):
                self._outer = outer

            async def click(self, x, y, button="left"):
                self._outer.clicks.append((x, y, button))

        def _next_dump(self):
            idx = min(self.dump_calls, len(self._dump_sequence) - 1)
            self.dump_calls += 1
            return self._dump_sequence[idx]

    def _make_page_78(dump_sequence):
        p = _FakePage78(dump_sequence)
        p.mouse = _FakePage78._Mouse(p)
        return p

    # 78: a real "Close" button is found in the dialog's own button dump
    # -> clicked by identity (never Escape, mirroring _close_viewer's own
    # proven "find by identity, click it" pattern) -- and the close is
    # VERIFIED (a second dump call reporting no dialog) rather than assumed.
    close_button_dump = {"dialogFound": True, "buttons": [{"ariaLabel": "Close", "testid": None, "rect": [10, 20, 30, 30]}]}
    gone_dump = {"dialogFound": False}
    page_78 = _make_page_78([close_button_dump, gone_dump])

    async def _fake_evaluate_78(page, js, arg=None, timeout=10.0):
        return page._next_dump()

    orig_evaluate_78 = mark_scan._evaluate
    mark_scan._evaluate = _fake_evaluate_78
    try:
        closed_78 = asyncio.run(mark_scan._ensure_forward_dialog_closed(page_78))
    finally:
        mark_scan._evaluate = orig_evaluate_78

    assert closed_78 is True, closed_78
    assert page_78.clicks == [(25.0, 35.0, "left")], page_78.clicks  # center of the close button's rect
    assert page_78.escape_presses == 0, "a real close button must be used instead of Escape when one is found"
    print("78. SEND dialog-close prefers real button -> a real Close control is clicked by identity, never Escape, when one exists")

    # 79: no close/cancel button in the dump (e.g. a plain Escape-only
    # dialog variant) -> falls back to Escape, still verified afterward.
    no_button_dump = {"dialogFound": True, "buttons": [{"ariaLabel": "Send", "testid": None, "rect": [0, 0, 10, 10]}]}
    page_79 = _make_page_78([no_button_dump, gone_dump])
    mark_scan._evaluate = _fake_evaluate_78
    try:
        closed_79 = asyncio.run(mark_scan._ensure_forward_dialog_closed(page_79))
    finally:
        mark_scan._evaluate = orig_evaluate_78

    assert closed_79 is True, closed_79
    assert page_79.escape_presses == 1, page_79.escape_presses
    assert page_79.clicks == [], "must never click an unrelated button (e.g. Send) as if it were a close control"
    print("79. SEND dialog-close falls back to Escape -> only when no real close/cancel control is found in the dump")

    # 80: the dialog genuinely never closes (stuck through every bounded
    # round) -> reports False rather than pretending success; the caller
    # (_send_one_target_native_forward) folds this into its own error
    # message rather than silently proceeding into a guaranteed failure.
    stuck_dump = {"dialogFound": True, "buttons": []}
    page_80 = _make_page_78([stuck_dump])
    mark_scan._evaluate = _fake_evaluate_78
    try:
        closed_80 = asyncio.run(mark_scan._ensure_forward_dialog_closed(page_80))
    finally:
        mark_scan._evaluate = orig_evaluate_78

    assert closed_80 is False, closed_80
    assert page_80.escape_presses == 5, page_80.escape_presses  # all 5 bounded rounds attempted, never an infinite retry
    print("80. SEND dialog-close bounded and honest -> a genuinely stuck dialog is reported as NOT closed, never assumed fixed")

    # 81 (2026-08-27, acceptance requirement): no unnecessary ~1-minute
    # inter-item delays. With every sub-operation mocked to return
    # instantly, _run_send's own orchestration must add no artificial
    # sleep between items -- if a fixed/artificial wait were ever
    # reintroduced between stages, this test's wall-clock budget would
    # catch it immediately (real WhatsApp-required waits live INSIDE the
    # mocked functions, not in _run_send's own loop, so mocking them
    # away isolates exactly what _run_send itself adds).
    import time as _time_mod
    calls_81, open_81, forward_81, text_81 = _install_send_fakes()
    orig_open_81, orig_forward_81, orig_text_81 = sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = open_81, forward_81, text_81
    try:
        req_81 = {
            "group_name": "Source Group", "destination_group": "Dest Group",
            "send_targets": [
                _send_target("take1", "take", 1), _send_target("take2", "take", 2),
                _send_target("intro1", "intro"), _send_target("pic1", "photos"),
            ],
            "form_insert_index": 3, "form_message": "SEND FORM TEXT", "send_marker_on_success": True,
        }
        t_start_81 = _time_mod.monotonic()
        result_81 = asyncio.run(mark_scan._run_send(_FakePage73(), req_81))
        elapsed_81 = _time_mod.monotonic() - t_start_81
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = orig_open_81, orig_forward_81, orig_text_81

    assert all(r["ok"] for r in result_81["results"]), result_81
    assert result_81["marker_result"]["ok"] is True, result_81
    assert elapsed_81 < 1.0, f"_run_send took {elapsed_81:.2f}s with everything mocked instant -- an artificial delay was reintroduced between items"
    print(f"81. SEND no artificial inter-item delay -> 4 items + form + marker processed in {elapsed_81*1000:.0f}ms with instant mocks (budget: 1000ms)")

    # 2026-08-27: a live SEND E2E against a real 2-tile photo album found
    # BOTH _identify_tile_index AND a plain generic <img> search returning
    # ZERO matches immediately after _find_message_index_by_data_id
    # located the message -- proven live (photo_render_timing_diagnostic)
    # to be a virtualized-DOM rendering-timing gap: the message's own
    # container renders as a bare stub (outerHTML ~222 chars) until
    # scrolled into view and given a moment to settle. These tests cover
    # the fix: _ensure_message_content_rendered (bounded poll, never a
    # blind fixed sleep) and _open_media_and_get_forward_button's photo
    # branch now targeting the SPECIFIC album tile at tile_index (via the
    # same video-content/image-content/image-thumb identity the original
    # scan hashed) instead of always grabbing message.locator("img").first.

    class _FakeStubThenReal82:
        def __init__(self):
            self.calls = 0
            self.scrolled = False
        async def scroll_into_view_if_needed(self, timeout=5000):
            self.scrolled = True
        async def evaluate(self, js):
            self.calls += 1
            return 222 if self.calls < 3 else 7411

    class _FakePage82:
        def __init__(self):
            self.waits = []
        async def wait_for_timeout(self, ms):
            self.waits.append(ms)

    msg_82 = _FakeStubThenReal82()
    page_82 = _FakePage82()
    asyncio.run(mark_scan._ensure_message_content_rendered(page_82, msg_82, max_rounds=8, interval_ms=500))
    assert msg_82.scrolled is True, "must scroll the message container into view before polling"
    assert msg_82.calls == 3, f"expected exactly 3 evaluate calls (stub, stub, rendered), got {msg_82.calls}"
    assert page_82.waits == [500, 500], f"expected exactly 2 waits (between stub rounds, none after success), got {page_82.waits}"
    print("82. photo render-timing wait       -> bounded poll waits only until the stub becomes real content, then stops immediately")

    class _FakeStubForever82:
        async def scroll_into_view_if_needed(self, timeout=5000):
            pass
        async def evaluate(self, js):
            return 222

    page_82b = _FakePage82()
    asyncio.run(mark_scan._ensure_message_content_rendered(page_82b, _FakeStubForever82(), max_rounds=4, interval_ms=100))
    assert page_82b.waits == [100, 100, 100, 100], page_82b.waits
    print("82b. photo render-timing bounded   -> a message that never renders real content is polled exactly max_rounds times, never infinitely")

    class _FakeImgLoc83:
        def __init__(self, rec, label):
            self.rec, self.label = rec, label
        async def scroll_into_view_if_needed(self, timeout=5000):
            self.rec.append((self.label, "scroll"))
        async def click(self, timeout=10000):
            self.rec.append((self.label, "click"))

    class _FakeImgCollection83:
        def __init__(self, rec, label):
            self.first = _FakeImgLoc83(rec, label)

    class _FakeTileLoc83:
        def __init__(self, rec, index):
            self.rec, self.index = rec, index
        async def evaluate(self, js, timeout=10000):
            return True
        def locator(self, sel):
            assert sel == "img"
            return _FakeImgCollection83(self.rec, f"tile{self.index}_img")

    class _FakeTilesCollection83:
        def __init__(self, rec, count):
            self.rec, self._count = rec, count
        async def count(self):
            return self._count
        def nth(self, i):
            self.rec.append(("tiles.nth", i))
            return _FakeTileLoc83(self.rec, i)

    class _FakeMessageLoc83:
        def __init__(self, rec):
            self.rec = rec
        async def scroll_into_view_if_needed(self, timeout=5000):
            self.rec.append(("message", "scroll"))
        async def evaluate(self, js, timeout=10000):
            return 7411  # already fully rendered -- isolates tile-selection from the render-timing test above
        def locator(self, sel):
            assert "image-thumb" in sel and "image-content" in sel and "video-content" in sel
            return _FakeTilesCollection83(self.rec, 2)

    class _FakeLocatorRoot83:
        def __init__(self, rec):
            self.rec = rec
        def nth(self, idx):
            return _FakeMessageLoc83(self.rec)

    class _FakePage83:
        def __init__(self, rec):
            self.rec = rec
        def locator(self, sel):
            return _FakeLocatorRoot83(self.rec)
        async def wait_for_timeout(self, ms):
            pass

    orig_find_idx_83 = mark_scan._find_message_index_by_data_id
    orig_resolve_scope_83 = sender._resolve_scope
    orig_evaluate_83 = mark_scan._evaluate

    async def _fake_find_idx_83(page, group, data_id):
        return 5

    async def _fake_resolve_scope_83(page):
        return "SCOPE"

    async def _fake_evaluate_forward_btn_83(page, js, arg=None, timeout=10.0):
        return {
            "rootFound": True,
            "buttons": [{"ariaLabel": "Forward", "dataIcon": None, "testid": None, "svgTitle": None, "rect": [100, 200, 24, 24]}],
        }

    mark_scan._find_message_index_by_data_id = _fake_find_idx_83
    sender._resolve_scope = _fake_resolve_scope_83
    mark_scan._evaluate = _fake_evaluate_forward_btn_83
    rec_83: list = []
    try:
        page_83 = _FakePage83(rec_83)
        result_83 = asyncio.run(mark_scan._open_media_and_get_forward_button(page_83, "GROUP", "MSGID", 1, True))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_83
        sender._resolve_scope = orig_resolve_scope_83
        mark_scan._evaluate = orig_evaluate_83

    assert result_83["ok"] is True, result_83
    assert ("tiles.nth", 1) in rec_83, rec_83
    assert ("tiles.nth", 0) not in rec_83, rec_83
    assert ("tile1_img", "click") in rec_83, rec_83
    assert ("tile0_img", "click") not in rec_83, rec_83
    print("83. photo album correct tile targeted -> tile_index=1 of 2 clicks the SECOND tile's own <img>, never always the first")

    # 2026-08-27: a real live SEND proved via a captured whatsapp_dom_snapshot
    # that the "Forward message to" dialog was STILL fully open (Send button
    # visible, caption/destination intact) immediately after a successful
    # send -- the very next operation (_send_text_message opening the
    # destination group for the marker) then failed with CHAT_NOT_OPENED
    # because an unknown blocking dialog was still covering the view.
    # _send_one_target_native_forward's SUCCESS path never verified the
    # dialog had actually closed (only the two FAILURE paths did) -- this
    # test proves the fix: success now also calls _ensure_forward_dialog_closed
    # before returning, without turning the already-real success into a
    # failure even if closing takes a moment.

    class _FakePage84:
        async def wait_for_timeout(self, ms):
            pass
        class mouse:
            @staticmethod
            async def click(x, y, button="left"):
                pass

    orig_open_84 = sender._open_group_chat
    orig_ready_84 = mark_scan._open_media_and_get_forward_button
    orig_select_84 = mark_scan._select_forward_destination
    orig_caption_84 = mark_scan._enter_forward_caption_and_send
    orig_closed_84 = mark_scan._ensure_forward_dialog_closed
    orig_find_text_84 = sender._find_outgoing_with_text

    close_calls_84 = []

    async def _fake_open_group_84(page, group):
        return "OPENED"

    async def _fake_ready_84(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_84(page, dest):
        return {"ok": True}

    async def _fake_caption_send_84(page, caption):
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_84(page):
        close_calls_84.append(True)
        return True

    async def _fake_find_text_84(page, needle, baselines=None):
        # Delivery verification's own destination-chat check — simulates
        # the forwarded photo's caption having landed, so this test keeps
        # proving its OWN original point (dialog-close verification)
        # rather than failing on the newer, separate verification step.
        return "SCOPE conv-msg", needle, "conv-msg-VERIFIED84"

    sender._open_group_chat = _fake_open_group_84
    mark_scan._open_media_and_get_forward_button = _fake_ready_84
    mark_scan._select_forward_destination = _fake_select_84
    mark_scan._enter_forward_caption_and_send = _fake_caption_send_84
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_84
    sender._find_outgoing_with_text = _fake_find_text_84
    try:
        target_84 = _send_target("photo1", "photos")
        result_84 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePage84(), "Source Group", target_84))
    finally:
        sender._open_group_chat = orig_open_84
        mark_scan._open_media_and_get_forward_button = orig_ready_84
        mark_scan._select_forward_destination = orig_select_84
        mark_scan._enter_forward_caption_and_send = orig_caption_84
        mark_scan._ensure_forward_dialog_closed = orig_closed_84
        sender._find_outgoing_with_text = orig_find_text_84

    assert result_84["ok"] is True, result_84
    assert len(close_calls_84) == 1, "dialog-close verification must run exactly once after a successful send"
    print("84. SEND verifies dialog closed on success -> a still-open Forward dialog after Send is now caught before the NEXT operation runs")

    # ------------------------------------------------------------------
    # 85-96: REAL PRODUCTION BUG (2026-09-07) — "Sneha Varghese / Vaseline
    # (Birthday film)". Screenshots proved BOTH marks were genuine, valid
    # replies-to-media (correct quoted thumbnail hash each), yet SEND
    # reported "Some marked media could not be matched to an exact
    # WhatsApp source message." Root cause: _run_scan's Pass 2 only ever
    # looked up a reply's quoted_hash inside `sources_by_hash`, which is
    # built ENTIRELY from Pass 1's plain-message scan of the SAME bounded
    # window (_dump_window, capped at whatever's currently rendered). If
    # the ORIGINAL media scrolled out of that window by the time SEND ran
    # (real chat activity between the mark and the send) the lookup
    # failed even though the reply's own quoted block unambiguously
    # named one exact source message. Fix: _resolve_single_media_via_jump
    # reuses the EXISTING, already-proven "click quoted block -> jump to
    # original message" mechanism (previously wired for whole-album
    # marks only) as a bounded, per-candidate fallback, with a HARD
    # re-verification of the jumped-to message's own hash against the
    # reply's expected hash before ever trusting it — never latest-
    # media/caption/timestamp guessing, exactly as required.
    # ------------------------------------------------------------------

    class _FakeJumpQuotedBlock:
        def __init__(self, count=1):
            self._count = count
            self.click_count = 0
        async def count(self):
            return self._count
        @property
        def first(self):
            return self
        async def click(self, timeout=None):
            self.click_count += 1

    class _FakeJumpReplyMessage:
        def __init__(self, quoted_block):
            self._quoted_block = quoted_block
        def locator(self, sel):
            assert sel == '[data-testid="quoted-message"]'
            return self._quoted_block

    class _FakeJumpTargetMessage:
        def __init__(self, html):
            self._html = html
        async def evaluate(self, js, timeout=None):
            return self._html

    class _FakeJumpLocatorRoot:
        def __init__(self, by_idx):
            self._by_idx = by_idx
        def nth(self, idx):
            return self._by_idx[idx]

    class _FakeJumpPage:
        def __init__(self, by_idx):
            self._by_idx = by_idx
            self.waits = []
        def locator(self, sel):
            return _FakeJumpLocatorRoot(self._by_idx)
        async def wait_for_timeout(self, ms):
            self.waits.append(ms)

    def _fake_blob(seed: str) -> str:
        # Exactly 80 chars — the minimum _B64_RE requires to be treated
        # as a real embedded thumbnail blob (letters only, safe either
        # way).
        return (seed * 20)[:78] + "AX"

    def _make_fake_find_idx_map(idx_by_data_id):
        async def fake(page, group_name, data_id):
            return idx_by_data_id.get(data_id)
        return fake

    async def _fake_resolve_scope_jump(page):
        return "#main"

    orig_resolve_scope_jump = mark_scan.sender._resolve_scope
    orig_find_idx_jump = mark_scan._find_message_index_by_data_id
    orig_evaluate_jump = mark_scan._evaluate
    mark_scan.sender._resolve_scope = _fake_resolve_scope_jump

    def _setup_jump_fixture(reply_id, jumped_id, jumped_html, quoted_present=True):
        reply_quoted = _FakeJumpQuotedBlock(count=1 if quoted_present else 0)
        reply_message = _FakeJumpReplyMessage(reply_quoted)
        jumped_message = _FakeJumpTargetMessage(jumped_html)
        page = _FakeJumpPage({0: reply_message, 1: jumped_message})
        mark_scan._find_message_index_by_data_id = _make_fake_find_idx_map({reply_id: 0, jumped_id: 1})

        async def _fake_evaluate(p, js, arg=None, timeout=10.0):
            return {"dataId": jumped_id}
        mark_scan._evaluate = _fake_evaluate
        return page, reply_quoted

    # 85: exact match — jumped-to message's OWN hash matches the reply's
    # quoted hash exactly -> resolves with the correct source identity.
    original_video_85 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "SNEHA_AUDITION_SRC")
    expected_hash_85 = mark_scan._smallest_hash(QUOTED_PHOTO_BLOCK_HTML)
    # Reuse a photo-shaped source so its hash matches the QUOTED_PHOTO_BLOCK_HTML fixture exactly.
    jumped_photo_85 = PHOTO_MESSAGE_HTML.replace("3B6637D11A63081B8712", "SNEHA_AUDITION_SRC")
    page_85, quoted_85 = _setup_jump_fixture("REPLY85", "SNEHA_AUDITION_SRC", jumped_photo_85)
    result_85 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_85, "Sneha Varghese", "REPLY85", expected_hash_85,
    ))
    assert result_85["ok"] is True, result_85
    assert result_85["source_message_id"] == "SNEHA_AUDITION_SRC", result_85
    assert result_85["source_media_type"] == "image", result_85
    assert quoted_85.click_count == 1, "must click the quoted block exactly once to trigger the jump"
    print("85. live-jump fallback resolves exact source -> jumped-to message's own hash matches the reply's quoted hash -> exact source_message_id returned")

    # 86: hash MISMATCH — jump lands near, but not on, the right message
    # (or a genuinely different one) -> NEVER accepted as a substitute.
    jumped_wrong_86 = PHOTO_MESSAGE_HTML.replace("3B6637D11A63081B8712", "WRONG_MESSAGE").replace(
        "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
        _fake_blob("totallydifferentphoto"),
    )
    page_86, _ = _setup_jump_fixture("REPLY86", "WRONG_MESSAGE", jumped_wrong_86)
    result_86 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_86, "Sneha Varghese", "REPLY86", expected_hash_85,
    ))
    assert result_86["ok"] is False, result_86
    assert "does not match" in result_86["reason"], result_86
    print("86. live-jump fallback safety: hash mismatch -> jumped-to message rejected, never substituted as the source")

    # 87: jumped-to message is a whole ALBUM, not the single expected
    # media item -> rejected as a fallback failure, never guesses a tile.
    page_87, _ = _setup_jump_fixture("REPLY87", "3B99A3545E173C9DA2C8", ALBUM_MESSAGE_HTML)
    result_87 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_87, "Sneha Varghese", "REPLY87", expected_hash_85,
    ))
    assert result_87["ok"] is False, result_87
    assert "album" in result_87["reason"], result_87
    print("87. live-jump fallback safety: jumped-to album -> rejected, not treated as a single-media match")

    # 88: jumped-to message's hash matches (same embedded blob) but it
    # carries no recognizable image/video testid marker at all -> clean
    # fallback failure, never assumed to be media just because a hash
    # happened to match.
    no_media_marker_88 = (
        '<div data-id="NOMEDIAMARKER88" data-testid="conv-msg-NOMEDIAMARKER88">'
        '<div style="background-image: url(&quot;data:image/jpeg;base64,'
        'AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX&quot;);"></div>'
        '</div>'
    )
    page_88, _ = _setup_jump_fixture("REPLY88", "NOMEDIAMARKER88", no_media_marker_88)
    result_88 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_88, "Sneha Varghese", "REPLY88", expected_hash_85,
    ))
    assert result_88["ok"] is False, result_88
    assert "no recognizable media" in result_88["reason"], result_88
    print("88. live-jump fallback safety: jumped-to message has no media -> clean failure, never guessed")

    mark_scan.sender._resolve_scope = orig_resolve_scope_jump
    mark_scan._find_message_index_by_data_id = orig_find_idx_jump
    mark_scan._evaluate = orig_evaluate_jump

    # 89-94: full _run_scan Pass-2 integration — the ACTUAL Sneha Varghese
    # scenario. The window _dump_window captured contains ONLY the two
    # mark replies (the two original videos have scrolled out of the
    # bounded render window by the time this scan runs) — exactly what a
    # real production scan sees. sources_by_hash therefore has NO entry
    # for either quoted hash via the cheap in-window path; only the live-
    # jump fallback (mocked here, at the _run_scan integration level) can
    # resolve them.
    class _FakeScanSenderForRunScan:
        async def _open_group_chat(self, page, group_name):
            return "OPENED"

    orig_sender_for_run_scan = mark_scan.sender
    mark_scan.sender = _FakeScanSenderForRunScan()

    audition_reply_89 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_AUDITION").replace(
        "mark spike take 1", "mark audition take for vaseline",
    )
    audition_quoted_89 = QUOTED_PHOTO_BLOCK_HTML.replace(
        "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
        _fake_blob("auditionvaselinehash"),
    )
    intro_reply_89 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_INTRO").replace(
        "mark spike take 1", "mark introduction take for vaseline",
    )
    intro_quoted_89 = QUOTED_PHOTO_BLOCK_HTML.replace(
        "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
        _fake_blob("introvaselinehash"),
    )
    window_89 = [
        {"messageHtml": audition_reply_89, "quotedHtml": audition_quoted_89},
        {"messageHtml": intro_reply_89, "quotedHtml": intro_quoted_89},
    ]
    audition_hash_89 = mark_scan._smallest_hash(audition_quoted_89)
    intro_hash_89 = mark_scan._smallest_hash(intro_quoted_89)

    fallback_calls_89: list = []

    async def _fake_jump_fallback_89(page, group_name, reply_id, expected_hash, expected_media_type=None):
        fallback_calls_89.append((reply_id, expected_hash))
        if expected_hash == audition_hash_89:
            return {"ok": True, "source_message_id": "SNEHA_AUDITION_ORIGINAL", "source_media_type": "video", "source_sender": "Sneha Varghese"}
        if expected_hash == intro_hash_89:
            return {"ok": True, "source_message_id": "SNEHA_INTRO_ORIGINAL", "source_media_type": "video", "source_sender": "Sneha Varghese"}
        return {"ok": False, "reason": "no match"}

    orig_jump_fallback = mark_scan._resolve_single_media_via_jump
    orig_dump_window_89 = mark_scan._dump_window

    async def _fake_dump_window_89(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_89

    mark_scan._resolve_single_media_via_jump = _fake_jump_fallback_89
    mark_scan._dump_window = _fake_dump_window_89
    try:
        scan_result_89 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Sneha Varghese"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback
        mark_scan._dump_window = orig_dump_window_89
    cands_89 = scan_result_89.get("candidates") or []
    by_text_89 = {c["mark_text"]: c for c in cands_89}
    assert len(cands_89) == 2, cands_89
    audition_cand_89 = by_text_89["mark audition take for vaseline"]
    assert audition_cand_89["resolved_source_message_id"] == "SNEHA_AUDITION_ORIGINAL", audition_cand_89
    assert audition_cand_89["resolved_via_jump_fallback"] is True, audition_cand_89
    print("89. Sneha/Vaseline audition mark resolves -> original scrolled out of window, live-jump fallback finds the EXACT source, never guessed")

    # 90: the SECOND mark (introduction take), in the SAME scan, resolves
    # to its OWN distinct source — no cross-contamination between the two
    # marks for the same talent/project.
    intro_cand_90 = by_text_89["mark introduction take for vaseline"]
    assert intro_cand_90["resolved_source_message_id"] == "SNEHA_INTRO_ORIGINAL", intro_cand_90
    assert intro_cand_90["resolved_via_jump_fallback"] is True, intro_cand_90
    assert intro_cand_90["resolved_source_message_id"] != audition_cand_89["resolved_source_message_id"], "audition and intro must never resolve to the same source"
    print("90. Sneha/Vaseline introduction mark resolves distinctly -> same scan, same project, two media items -> two distinct exact sources, no cross-contamination")

    # 91: genuinely unresolvable — the live-jump fallback ITSELF fails
    # (hash mismatch / message gone) -> candidate stays unresolved, never
    # a guessed/substituted source. This is the safe-failure path SEND's
    # caller must still report as "could not be matched", not silently
    # accept.
    unresolvable_reply_91 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_UNRESOLVABLE").replace(
        "mark spike take 1", "mark audition take for unresolvableproject",
    )
    unresolvable_quoted_91 = QUOTED_PHOTO_BLOCK_HTML.replace(
        "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
        _fake_blob("genuinelynomatchhash"),
    )
    window_91 = [{"messageHtml": unresolvable_reply_91, "quotedHtml": unresolvable_quoted_91}]

    async def _fake_jump_fallback_fails_91(page, group_name, reply_id, expected_hash, expected_media_type=None):
        return {"ok": False, "reason": "jumped-to message's own hash does not match the reply's quoted hash"}

    async def _fake_dump_window_91(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_91

    mark_scan._resolve_single_media_via_jump = _fake_jump_fallback_fails_91
    mark_scan._dump_window = _fake_dump_window_91
    try:
        scan_result_91 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback
        mark_scan._dump_window = orig_dump_window_89
    cands_91 = scan_result_91.get("candidates") or []
    assert len(cands_91) == 1, cands_91
    assert cands_91[0]["resolved_source_message_id"] is None, cands_91[0]
    assert cands_91[0]["resolved_via_jump_fallback"] is False, cands_91[0]
    print("91. genuinely unresolvable mark fails safely -> live-jump fallback itself fails -> source stays None, never guessed, reported as unresolved")

    # 92: EXCEPTION-SAFETY — the live-jump fallback raises for ONE
    # candidate (real page/session trouble) -> that candidate degrades to
    # unresolved gracefully, but a SECOND, unrelated candidate in the SAME
    # window (which resolves via the ordinary cheap in-window hash match,
    # no fallback needed at all) is completely unaffected -> one
    # candidate's failure must never crash or poison the whole scan.
    ok_source_92 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "OK_SOURCE_92")
    ok_reply_92 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_OK_92").replace(
        "mark spike take 1", "mark take 1 for regressionproject",
    )
    ok_quoted_92 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "OK_SOURCE_92")
    crash_reply_92 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_CRASH_92").replace(
        "mark spike take 1", "mark intro for crashproject",
    )
    crash_quoted_92 = QUOTED_PHOTO_BLOCK_HTML.replace(
        "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
        _fake_blob("willraiseanexception"),
    )
    window_92 = [
        {"messageHtml": ok_source_92, "quotedHtml": None},  # plain source message (Pass 1)
        {"messageHtml": ok_reply_92, "quotedHtml": ok_quoted_92},   # resolves via the cheap in-window path
        {"messageHtml": crash_reply_92, "quotedHtml": crash_quoted_92},  # fallback raises for this one
    ]

    async def _fake_jump_fallback_raises_92(page, group_name, reply_id, expected_hash, expected_media_type=None):
        raise RuntimeError("simulated page/session trouble during live jump")

    async def _fake_dump_window_92(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_92

    mark_scan._resolve_single_media_via_jump = _fake_jump_fallback_raises_92
    mark_scan._dump_window = _fake_dump_window_92
    try:
        scan_result_92 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback
        mark_scan._dump_window = orig_dump_window_89
    cands_92 = scan_result_92.get("candidates") or []
    by_text_92 = {c["mark_text"]: c for c in cands_92}
    assert len(cands_92) == 2, cands_92
    ok_cand_92 = by_text_92["mark take 1 for regressionproject"]
    assert ok_cand_92["resolved_source_message_id"] == "OK_SOURCE_92", ok_cand_92  # unaffected by the OTHER candidate's exception
    crash_cand_92 = by_text_92["mark intro for crashproject"]
    assert crash_cand_92["resolved_source_message_id"] is None, crash_cand_92  # degrades gracefully, never crashes the scan
    print("92. live-jump exception-safety -> one candidate's fallback raises -> degrades to unresolved for THAT one only, unrelated candidate in the same scan resolves normally")

    # 93: BOUNDED attempts — construct more unresolved-quoted-hash
    # candidates than MAX_JUMP_FALLBACK_ATTEMPTS_PER_SCAN; confirm the
    # fallback is attempted AT MOST that many times, never once per
    # candidate unconditionally (a real safety rail against a
    # pathological number of unresolved marks turning one scan into many
    # sequential live WhatsApp interactions).
    n_over_bound_93 = mark_scan.MAX_JUMP_FALLBACK_ATTEMPTS_PER_SCAN + 5
    window_93 = []
    for i in range(n_over_bound_93):
        reply_html = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", f"MARKREPLY_BOUND_{i}").replace(
            "mark spike take 1", f"mark take 1 for boundproject{i}",
        )
        quoted_html = QUOTED_PHOTO_BLOCK_HTML.replace(
            "AAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAABBBBCCCCDDDDsamephotoAAAAX",
            (f"bound{i}" * 20)[:78] + "AX",
        )
        window_93.append({"messageHtml": reply_html, "quotedHtml": quoted_html})

    call_count_93 = {"n": 0}

    async def _fake_jump_fallback_counts_93(page, group_name, reply_id, expected_hash, expected_media_type=None):
        call_count_93["n"] += 1
        return {"ok": False, "reason": "no match"}

    async def _fake_dump_window_93(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_93

    mark_scan._resolve_single_media_via_jump = _fake_jump_fallback_counts_93
    mark_scan._dump_window = _fake_dump_window_93
    try:
        scan_result_93 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback
        mark_scan._dump_window = orig_dump_window_89
    assert call_count_93["n"] == mark_scan.MAX_JUMP_FALLBACK_ATTEMPTS_PER_SCAN, call_count_93
    cands_93 = scan_result_93.get("candidates") or []
    assert len(cands_93) == n_over_bound_93, cands_93  # every candidate still reported, just not all attempted a live jump
    print("93. live-jump fallback bounded -> attempted at most MAX_JUMP_FALLBACK_ATTEMPTS_PER_SCAN times per scan, remaining candidates reported unresolved rather than triggering unbounded live jumps")

    # 94: the cheap in-window path stays authoritative and fast — when a
    # candidate's quoted hash IS already found among the plain source
    # messages captured in the SAME bounded window, the live-jump
    # fallback is never even attempted for it (proves the fix is a
    # fallback, not a replacement for the existing fast path).
    plain_source_94 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "ALREADY_IN_WINDOW_94")
    reply_94 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_94").replace(
        "mark spike take 1", "mark take 1 for fastpathproject",
    )
    quoted_94 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "ALREADY_IN_WINDOW_94")
    window_94 = [
        {"messageHtml": plain_source_94, "quotedHtml": None},
        {"messageHtml": reply_94, "quotedHtml": quoted_94},
    ]
    fallback_call_count_94 = {"n": 0}

    async def _fake_jump_fallback_counts_94(page, group_name, reply_id, expected_hash, expected_media_type=None):
        fallback_call_count_94["n"] += 1
        return {"ok": False, "reason": "should never be called"}

    async def _fake_dump_window_94(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_94

    mark_scan._resolve_single_media_via_jump = _fake_jump_fallback_counts_94
    mark_scan._dump_window = _fake_dump_window_94
    try:
        scan_result_94 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback
        mark_scan._dump_window = orig_dump_window_89
    cands_94 = scan_result_94.get("candidates") or []
    assert len(cands_94) == 1, cands_94
    assert cands_94[0]["resolved_source_message_id"] == "ALREADY_IN_WINDOW_94", cands_94[0]
    assert cands_94[0]["resolved_via_jump_fallback"] is False, cands_94[0]  # cheap path resolved it -> fallback never invoked
    assert fallback_call_count_94["n"] == 0, "live-jump fallback must never be attempted when the cheap in-window hash lookup already succeeded"
    print("94. cheap in-window match stays authoritative -> live-jump fallback never even attempted when the plain scan already resolved the source (no unnecessary live interaction)")

    # 95: FORWARDED media behaves identically to original media — a
    # forwarded video source (WhatsApp marks forwarded messages with a
    # distinct "Forwarded" label/icon in the real DOM, but this does not
    # change its own outerHTML's media-type/thumbnail-hash extraction) is
    # resolved by the exact same live-jump fallback, same hash-
    # reverification safety check, same result shape — proving the fix
    # does not depend on the source being un-forwarded.
    forwarded_source_html_95 = (
        '<div data-id="SNEHA_FORWARDED_SRC" data-testid="conv-msg-SNEHA_FORWARDED_SRC">'
        '<div data-testid="forwarded" aria-label="Forwarded"><span>Forwarded</span></div>'
        '<div data-testid="video-content">'
        '<div style="background-image: url(&quot;data:image/jpeg;base64,'
        'FORWARDEDHASHSTABLEFORWARDEDHASHSTABLEFORWARDEDHASHSTABLEFORWARDEDHASHSTABLE&quot;);"></div>'
        '<div style="background-image: url(&quot;data:image/jpeg;base64,'
        + ("FORWARDEDPOSTERBLOBDIFFERSEACHTIME" * 5) + '&quot;);"></div>'
        '</div></div>'
    )
    expected_hash_95 = mark_scan._smallest_hash(forwarded_source_html_95)
    page_95, quoted_95 = _setup_jump_fixture("REPLY95", "SNEHA_FORWARDED_SRC", forwarded_source_html_95)
    mark_scan.sender._resolve_scope = _fake_resolve_scope_jump
    result_95 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_95, "Sneha Varghese", "REPLY95", expected_hash_95,
    ))
    mark_scan.sender._resolve_scope = orig_resolve_scope_jump
    mark_scan._find_message_index_by_data_id = orig_find_idx_jump
    mark_scan._evaluate = orig_evaluate_jump
    assert result_95["ok"] is True, result_95
    assert result_95["source_message_id"] == "SNEHA_FORWARDED_SRC", result_95
    assert result_95["source_media_type"] == "video", result_95
    print("95. forwarded media resolves identically -> forwarded-message marker in the DOM does not change hash/media-type extraction, same exact-match resolution as original media")

    # 96: SIMILAR-CAPTION disambiguation — two source videos in the SAME
    # window with visually similar captions/context but genuinely
    # DIFFERENT thumbnail hashes; a reply quoting one of them resolves to
    # THAT exact one via the cheap in-window path (the caption text is
    # never consulted — only the thumbnail hash identity is), proving
    # captions never substitute for exact WhatsApp message identity.
    similar_a_96 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "SIMILAR_CAPTION_A")
    similar_b_96 = VIDEO_MESSAGE_HTML.replace("3BCD6927E2737ED17205", "SIMILAR_CAPTION_B").replace(
        "SMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLESMALLTHUMBHASHSTABLE",
        _fake_blob("DIFFERENTHASHENTIRELY"),
    )
    reply_to_b_96 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "MARKREPLY_96").replace(
        "mark spike take 1", "mark take 1 for similarcaptionproject",
    )
    window_96 = [
        {"messageHtml": similar_a_96, "quotedHtml": None},
        {"messageHtml": similar_b_96, "quotedHtml": None},
        {"messageHtml": reply_to_b_96, "quotedHtml": similar_b_96},  # quotes B's exact hash, not A's
    ]

    async def _fake_dump_window_96(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_96

    mark_scan._dump_window = _fake_dump_window_96
    try:
        scan_result_96 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._dump_window = orig_dump_window_89
    cands_96 = scan_result_96.get("candidates") or []
    assert len(cands_96) == 1, cands_96
    assert cands_96[0]["resolved_source_message_id"] == "SIMILAR_CAPTION_B", cands_96[0]  # never A, despite similar surrounding context
    print("96. similar-caption disambiguation -> reply quoting B's exact hash resolves to B, never to A, despite near-identical surrounding context/captions")

    # ------------------------------------------------------------------
    # 97-103: ROUND 2 — real recurrence of the SAME production error after
    # round 1 (tests 85-96) was deployed. Traced: round 1's live-jump
    # fallback only ever triggered when a thumbnail hash WAS extracted
    # from the quoted block but simply didn't match anything in-window.
    # Real-world evidence (this exact recurrence for Sneha Varghese's
    # marks, live, AFTER round 1 was deployed) proves a quoted-message
    # block does not always embed an inline base64 thumbnail at all
    # (_smallest_hash's regex finds nothing) — e.g. a forwarded video's
    # quoted preview — while still being an ordinary single-item media
    # quote, never WhatsApp's "N videos/photos" whole-album summary.
    # Round 1's fallback gate (`quoted_hash is not None`) skipped this
    # case ENTIRELY: no fallback was even attempted, so it degraded
    # straight to "could not be matched" exactly like before round 1
    # existed. Round 2 also triggers the fallback when the quote's own
    # media-type marker (video-thumb/video-content/image-thumb/
    # image-content — detected independently of any hash) is present,
    # verifying the jump result by media-type match instead of hash
    # equality when no hash is available.
    # ------------------------------------------------------------------

    mark_scan.sender._resolve_scope = _fake_resolve_scope_jump

    # 97: expected_hash=None, expected_media_type="video" -> jumped-to
    # message IS a video -> resolves via media-type verification alone.
    jumped_video_97 = (
        '<div data-id="NOHASH_VIDEO_97" data-testid="conv-msg-NOHASH_VIDEO_97">'
        '<div data-testid="video-content"></div></div>'
    )
    page_97, quoted_97 = _setup_jump_fixture("REPLY97", "NOHASH_VIDEO_97", jumped_video_97)
    result_97 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_97, "Sneha Varghese", "REPLY97", None, expected_media_type="video",
    ))
    assert result_97["ok"] is True, result_97
    assert result_97["source_message_id"] == "NOHASH_VIDEO_97", result_97
    assert result_97["source_media_type"] == "video", result_97
    print("97. live-jump media-type-only verification succeeds -> no hash extractable from the quote at all, jumped-to message's own media type matches -> resolves")

    # 98: expected_hash=None, expected_media_type="video" -> jumped-to
    # message is actually an IMAGE -> media-type mismatch -> rejected,
    # never accepted as a substitute.
    jumped_image_98 = (
        '<div data-id="WRONG_TYPE_98" data-testid="conv-msg-WRONG_TYPE_98">'
        '<div data-testid="image-thumb"></div></div>'
    )
    page_98, _ = _setup_jump_fixture("REPLY98", "WRONG_TYPE_98", jumped_image_98)
    result_98 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_98, "Sneha Varghese", "REPLY98", None, expected_media_type="video",
    ))
    assert result_98["ok"] is False, result_98
    assert "does not match" in result_98["reason"], result_98
    print("98. live-jump media-type-only verification safety: media-type mismatch -> jumped-to message rejected, never substituted")

    # 99: neither expected_hash NOR expected_media_type available -> the
    # function itself refuses to guess (defensive — _run_scan's own
    # can_attempt_jump gate should never actually call it this way, but
    # the function itself must not silently succeed if it ever is).
    jumped_99 = '<div data-id="ANYTHING_99" data-testid="conv-msg-ANYTHING_99"><div data-testid="video-content"></div></div>'
    page_99, _ = _setup_jump_fixture("REPLY99", "ANYTHING_99", jumped_99)
    result_99 = asyncio.run(mark_scan._resolve_single_media_via_jump(
        page_99, "Sneha Varghese", "REPLY99", None, expected_media_type=None,
    ))
    assert result_99["ok"] is False, result_99
    assert "no expected hash or media type" in result_99["reason"], result_99
    print("99. live-jump refuses with neither signal -> no hash and no media-type to verify against -> clean refusal, never a blind accept")

    mark_scan.sender._resolve_scope = orig_resolve_scope_jump
    mark_scan._find_message_index_by_data_id = orig_find_idx_jump
    mark_scan._evaluate = orig_evaluate_jump

    # 100-103: full _run_scan integration, REAL (unmocked)
    # _resolve_single_media_via_jump — the actual Sneha Varghese /
    # Vaseline scenario as it recurred in production: BOTH marks' quoted
    # blocks carry NO embeddable hash at all (modeling a forwarded
    # video's quoted preview), only a real video-content testid marker.
    class _FakeScanSenderWithScope:
        async def _open_group_chat(self, page, group_name):
            return "OPENED"
        async def _resolve_scope(self, page):
            return "#main"

    orig_sender_100 = mark_scan.sender
    mark_scan.sender = _FakeScanSenderWithScope()

    audition_reply_100 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "REPLY100A").replace(
        "mark spike take 1", "mark audition take for vaseline",
    )
    intro_reply_100 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "REPLY100B").replace(
        "mark spike take 1", "mark introduction take for vaseline",
    )
    # No embedded base64 blob at all in either quoted block — only the
    # real data-testid marker WhatsApp renders regardless of thumbnail
    # availability. _smallest_hash(...) returns None for both; _media_type
    # still finds "video" for both, purely from the testid.
    no_hash_video_quote = '<div data-testid="quoted-message"><span data-testid="author">Raj Talentgram</span><div data-testid="video-content"></div></div>'
    window_100 = [
        {"messageHtml": audition_reply_100, "quotedHtml": no_hash_video_quote},
        {"messageHtml": intro_reply_100, "quotedHtml": no_hash_video_quote},
    ]

    jumped_audition_100 = '<div data-id="JUMPED100A" data-testid="conv-msg-JUMPED100A"><div data-testid="video-content"></div></div>'
    jumped_intro_100 = '<div data-id="JUMPED100B" data-testid="conv-msg-JUMPED100B"><div data-testid="video-content"></div></div>'
    idx_by_data_id_100 = {"REPLY100A": 0, "JUMPED100A": 1, "REPLY100B": 2, "JUMPED100B": 3}
    by_idx_100 = {
        0: _FakeJumpReplyMessage(_FakeJumpQuotedBlock(count=1)),
        1: _FakeJumpTargetMessage(jumped_audition_100),
        2: _FakeJumpReplyMessage(_FakeJumpQuotedBlock(count=1)),
        3: _FakeJumpTargetMessage(jumped_intro_100),
    }
    reply_to_jumped_100 = {"REPLY100A": "JUMPED100A", "REPLY100B": "JUMPED100B"}
    find_idx_calls_100: list = []

    async def _fake_find_idx_100(page, group_name, data_id):
        find_idx_calls_100.append(data_id)
        return idx_by_data_id_100.get(data_id)

    async def _fake_evaluate_100(p, js, arg=None, timeout=10.0):
        last_reply = next(d for d in reversed(find_idx_calls_100) if d in reply_to_jumped_100)
        return {"dataId": reply_to_jumped_100[last_reply]}

    orig_find_idx_100 = mark_scan._find_message_index_by_data_id
    orig_evaluate_100 = mark_scan._evaluate
    orig_dump_window_100 = mark_scan._dump_window

    async def _fake_dump_window_100(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_100

    mark_scan._find_message_index_by_data_id = _fake_find_idx_100

    class _FakeJumpPage100:
        def __init__(self):
            pass
        def locator(self, sel):
            return _FakeJumpLocatorRoot(by_idx_100)
        async def wait_for_timeout(self, ms):
            pass

    mark_scan._evaluate = _fake_evaluate_100
    mark_scan._dump_window = _fake_dump_window_100
    try:
        scan_result_100 = asyncio.run(mark_scan._run_scan(page=_FakeJumpPage100(), req={"group_name": "Sneha Varghese"}))
    finally:
        mark_scan._find_message_index_by_data_id = orig_find_idx_100
        mark_scan._evaluate = orig_evaluate_100
        mark_scan._dump_window = orig_dump_window_100
        mark_scan.sender = orig_sender_100
    cands_100 = scan_result_100.get("candidates") or []
    by_text_100 = {c["mark_text"]: c for c in cands_100}
    assert len(cands_100) == 2, cands_100
    audition_cand_100 = by_text_100["mark audition take for vaseline"]
    assert audition_cand_100["quoted_thumbnail_hash"] is None, audition_cand_100  # confirms this IS the no-hash-at-all case, not the round-1 scrolled-out case
    assert audition_cand_100["resolved_source_message_id"] == "JUMPED100A", audition_cand_100
    assert audition_cand_100["resolved_via_jump_fallback"] is True, audition_cand_100
    print("100. Sneha/Vaseline audition mark resolves with NO extractable quote hash at all -> live-jump fallback triggers on media-type signal alone -> exact source found (the REAL round-2 recurrence, reproduced and fixed)")

    # 101: the SECOND mark (introduction), SAME scan, SAME no-hash
    # condition -> resolves to its OWN distinct source, no cross-
    # contamination, proving round 2 handles multiple no-hash marks in
    # one scan correctly (not by accidentally reusing the first jump).
    intro_cand_101 = by_text_100["mark introduction take for vaseline"]
    assert intro_cand_101["resolved_source_message_id"] == "JUMPED100B", intro_cand_101
    assert intro_cand_101["resolved_via_jump_fallback"] is True, intro_cand_101
    assert intro_cand_101["resolved_source_message_id"] != audition_cand_100["resolved_source_message_id"], "audition and intro must never resolve to the same source even in the no-hash path"
    print("101. Sneha/Vaseline introduction mark resolves distinctly under the same no-hash condition -> two marks, two distinct exact sources, no cross-contamination")

    # 102: quoted block has NEITHER a hash NOR any recognizable media-type
    # marker at all (e.g. a reply to a plain text message, or a
    # genuinely unrecognized quote shape) -> can_attempt_jump is False ->
    # the live-jump fallback is never even attempted (verified via a
    # call-counting fake) -> stays a clean, honest unresolved failure.
    text_quote_102 = '<div data-testid="quoted-message"><span data-testid="author">Raj Talentgram</span><span data-testid="selectable-text">just a text message, not media</span></div>'
    reply_102 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "REPLY102").replace(
        "mark spike take 1", "mark take 1 for noquotecontentproject",
    )
    window_102 = [{"messageHtml": reply_102, "quotedHtml": text_quote_102}]
    jump_call_count_102 = {"n": 0}

    async def _fake_jump_counts_102(page, group_name, reply_id, expected_hash, expected_media_type=None):
        jump_call_count_102["n"] += 1
        return {"ok": True, "source_message_id": "SHOULD_NEVER_BE_USED", "source_media_type": "video"}

    async def _fake_dump_window_102(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_102

    mark_scan.sender = _FakeScanSenderForRunScan()
    orig_jump_fallback_102 = mark_scan._resolve_single_media_via_jump
    mark_scan._resolve_single_media_via_jump = _fake_jump_counts_102
    mark_scan._dump_window = _fake_dump_window_102
    try:
        scan_result_102 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback_102
        mark_scan._dump_window = orig_dump_window_89
        mark_scan.sender = orig_sender_100
    cands_102 = scan_result_102.get("candidates") or []
    assert len(cands_102) == 1, cands_102
    assert jump_call_count_102["n"] == 0, "no hash AND no media-type marker means nothing to verify a jump against -> must never even attempt one"
    assert cands_102[0]["resolved_source_message_id"] is None, cands_102[0]
    print("102. no hash AND no media-type marker in the quote -> live-jump fallback never even attempted (nothing to verify against), reported unresolved honestly")

    # 103: media-type-only verification's safety check IN THE FULL
    # _run_scan integration (not just the unit-level test 98) — the jump
    # lands on a message whose media type does NOT match the quote's own
    # marker -> rejected, mark stays unresolved, never a wrong forward.
    reply_103 = REPLY_TO_PHOTO_HTML.replace("3EB0CAC0901DAD51217B30", "REPLY103").replace(
        "mark spike take 1", "mark audition take for mismatchproject",
    )
    window_103 = [{"messageHtml": reply_103, "quotedHtml": no_hash_video_quote}]  # quote says "video"

    async def _fake_jump_mismatch_103(page, group_name, reply_id, expected_hash, expected_media_type=None):
        # Simulates a real jump landing on a genuinely different message
        # whose own media type doesn't match — exactly what
        # _resolve_single_media_via_jump's own media-type check rejects.
        return {"ok": False, "reason": "jumped-to message's media type does not match the quoted block's own media type"}

    async def _fake_dump_window_103(page, group_name, max_messages, diagnostic=None, max_steps=10):
        return window_103

    mark_scan.sender = _FakeScanSenderForRunScan()
    mark_scan._resolve_single_media_via_jump = _fake_jump_mismatch_103
    mark_scan._dump_window = _fake_dump_window_103
    try:
        scan_result_103 = asyncio.run(mark_scan._run_scan(page=object(), req={"group_name": "Test Group"}))
    finally:
        mark_scan._resolve_single_media_via_jump = orig_jump_fallback_102
        mark_scan._dump_window = orig_dump_window_89
        mark_scan.sender = orig_sender_100
    cands_103 = scan_result_103.get("candidates") or []
    assert len(cands_103) == 1, cands_103
    assert cands_103[0]["resolved_source_message_id"] is None, cands_103[0]
    assert cands_103[0]["resolved_via_jump_fallback"] is False, cands_103[0]
    print("103. media-type mismatch in full _run_scan integration -> jump result rejected, mark stays unresolved, never a wrong forward")

    mark_scan.sender = orig_sender_for_run_scan

    # ------------------------------------------------------------------
    # 104-107: REAL PRODUCTION FAILURE ("Shivi Rajput / Vaseline", SEND
    # PARTIAL, 0/2 media sent, both failed with "tile click failed:
    # Locator.scroll_into_view_if_needed: Timeout 5000ms exceeded" at
    # conv-msg-46/conv-msg-47). Root cause, found via direct comparison
    # against UPLOAD's own equivalent tile-click path
    # (_open_tile_viewer_and_download, tests 36-54 above): THAT path
    # already (a) swallows a scroll_into_view_if_needed failure as a
    # best-effort nudge only (Playwright's own .click() performs its own
    # actionability wait, including auto-scroll) and (b) retries on ANY
    # click exception, not just ones whose text happens to contain the
    # literal substrings "not stable"/"detached". SEND's OWN tile-click
    # path (_open_media_and_get_forward_button) never had either fix —
    # a scroll failure with different wording (exactly what a real
    # virtualized/far-off-screen element produces) broke the retry loop
    # on the FIRST attempt, exhausting zero of the 3 available retries.
    # These tests were previously entirely absent — this exact code path
    # had no direct regression coverage before this recurrence.
    # ------------------------------------------------------------------

    class _FakeTileVideo:
        def __init__(self, fail_scroll_times=0, fail_click_times=0, scroll_error="Locator.scroll_into_view_if_needed: Timeout 5000ms exceeded"):
            self.fail_scroll_times = fail_scroll_times
            self.fail_click_times = fail_click_times
            self.scroll_error = scroll_error
            self.scroll_calls = 0
            self.click_calls = 0

        async def scroll_into_view_if_needed(self, timeout=None):
            self.scroll_calls += 1
            if self.scroll_calls <= self.fail_scroll_times:
                raise Exception(self.scroll_error)

        async def click(self, timeout=None):
            self.click_calls += 1
            if self.click_calls <= self.fail_click_times:
                raise Exception("Locator.click: Timeout 10000ms exceeded waiting for element to be visible")

    class _FakeVideoCountLocator:
        def __init__(self, n):
            self.n = n
        async def count(self):
            return self.n

    class _FakeGenericMessageLocator104:
        def nth(self, idx):
            return self
        async def evaluate(self, js, timeout=None):
            return 1000  # arbitrary "hydrated" outerHTML length, unused since _ensure_message_content_rendered is mocked

    class _FakeSendPage104:
        def __init__(self, video_count=1):
            self.video_count = video_count
        def locator(self, sel):
            if sel == "video":
                return _FakeVideoCountLocator(self.video_count)
            return _FakeGenericMessageLocator104()
        async def wait_for_timeout(self, ms):
            pass

    orig_find_idx_104 = mark_scan._find_message_index_by_data_id
    orig_resolve_scope_104 = mark_scan.sender._resolve_scope
    orig_ensure_rendered_104 = mark_scan._ensure_message_content_rendered
    orig_resolve_tile_104 = mark_scan._resolve_video_tile_locator
    orig_wait_readiness_104 = mark_scan._wait_for_video_readiness
    orig_find_forward_btn_104 = mark_scan._find_onscreen_forward_button
    orig_evaluate_104 = mark_scan._evaluate

    find_idx_calls_104: list = []

    async def _fake_find_idx_104(page, group_name, data_id):
        find_idx_calls_104.append(data_id)
        return 0

    async def _fake_resolve_scope_104(page):
        return "#main"

    async def _fake_ensure_rendered_104(page, message, max_rounds=8, interval_ms=500):
        pass

    async def _fake_wait_readiness_104(page, min_ready_state=3, timeout_s=60.0):
        return {"ok": True}

    async def _fake_evaluate_104(page, js, arg=None, timeout=10.0):
        return {}

    def _install_tile_104(tile):
        async def fake_resolve(page, group_name, source_message_id, tile_index):
            return tile, None
        mark_scan._resolve_video_tile_locator = fake_resolve

    mark_scan._find_message_index_by_data_id = _fake_find_idx_104
    mark_scan.sender._resolve_scope = _fake_resolve_scope_104
    mark_scan._ensure_message_content_rendered = _fake_ensure_rendered_104
    mark_scan._wait_for_video_readiness = _fake_wait_readiness_104
    mark_scan._find_onscreen_forward_button = lambda dump: {"rect": [0, 0, 10, 10]}
    mark_scan._evaluate = _fake_evaluate_104

    # 104: scroll_into_view_if_needed fails with the EXACT real production
    # wording (no "not stable"/"detached" substring at all) -> swallowed
    # as a best-effort nudge, click still proceeds and succeeds
    # immediately -> forward ready, only ONE click attempt needed.
    tile_104 = _FakeTileVideo(fail_scroll_times=1, fail_click_times=0)
    _install_tile_104(tile_104)
    find_idx_calls_104.clear()
    result_104 = asyncio.run(mark_scan._open_media_and_get_forward_button(
        _FakeSendPage104(), "Shivi Rajput", "SRC46", 0, False,
    ))
    assert result_104["ok"] is True, result_104
    assert tile_104.click_calls == 1, tile_104.click_calls
    print("104. SEND tile scroll failure swallowed -> real 'Timeout 5000ms exceeded' scroll error (no special substring) never blocks the click, forward becomes ready")

    # 105: the click itself fails on attempt 1 with a GENERIC timeout
    # error (the exact real production shape — no "not stable"/"detached"
    # substring) -> the OLD code would have broken immediately, reporting
    # 0/2 sent exactly as happened in production; the NEW code retries
    # unconditionally and succeeds on attempt 2 via fresh re-resolution.
    tile_105 = _FakeTileVideo(fail_scroll_times=0, fail_click_times=1)
    _install_tile_104(tile_105)
    find_idx_calls_104.clear()
    result_105 = asyncio.run(mark_scan._open_media_and_get_forward_button(
        _FakeSendPage104(), "Shivi Rajput", "SRC46", 0, False,
    ))
    assert result_105["ok"] is True, result_105
    assert tile_105.click_calls == 2, tile_105.click_calls  # failed once, retried, succeeded
    assert len(find_idx_calls_104) == 2, find_idx_calls_104  # re-resolved fresh by identity on the retry, never reused a stale index
    print("105. SEND tile click retries on a generic (non-'not stable'/'detached') failure -> re-acquires by identity and succeeds on attempt 2, exactly the production recurrence now fixed")

    # 106: click fails on EVERY attempt -> bounded failure after
    # MAX_TILE_CLICK_ATTEMPTS, never an infinite retry, and the reported
    # reason names the attempt count for diagnosability.
    tile_106 = _FakeTileVideo(fail_scroll_times=0, fail_click_times=99)
    _install_tile_104(tile_106)
    find_idx_calls_104.clear()
    result_106 = asyncio.run(mark_scan._open_media_and_get_forward_button(
        _FakeSendPage104(), "Shivi Rajput", "SRC47", 0, False,
    ))
    assert result_106["ok"] is False, result_106
    assert tile_106.click_calls == mark_scan.MAX_TILE_CLICK_ATTEMPTS, tile_106.click_calls
    assert f"after {mark_scan.MAX_TILE_CLICK_ATTEMPTS} attempts" in result_106["reason"], result_106
    print("106. SEND tile click bounded -> a persistently failing tile is retried exactly MAX_TILE_CLICK_ATTEMPTS times, never more, and fails cleanly with a diagnosable reason")

    # 107: the source message is genuinely not found (idx is None) ->
    # immediate clean failure, never wastes retries re-searching for a
    # message that isn't there.
    async def _fake_find_idx_107(page, group_name, data_id):
        find_idx_calls_104.append(data_id)
        return None

    mark_scan._find_message_index_by_data_id = _fake_find_idx_107
    find_idx_calls_104.clear()
    result_107 = asyncio.run(mark_scan._open_media_and_get_forward_button(
        _FakeSendPage104(), "Shivi Rajput", "SRC_GONE", 0, False,
    ))
    assert result_107["ok"] is False, result_107
    assert "no longer found" in result_107["reason"], result_107
    assert len(find_idx_calls_104) == 1, find_idx_calls_104  # exactly one lookup, never a wasted retry against nothing
    print("107. SEND tile resolution: source message genuinely gone -> clean immediate failure, no wasted retry attempts")

    mark_scan._find_message_index_by_data_id = orig_find_idx_104
    mark_scan.sender._resolve_scope = orig_resolve_scope_104
    mark_scan._ensure_message_content_rendered = orig_ensure_rendered_104
    mark_scan._resolve_video_tile_locator = orig_resolve_tile_104
    mark_scan._wait_for_video_readiness = orig_wait_readiness_104
    mark_scan._find_onscreen_forward_button = orig_find_forward_btn_104
    mark_scan._evaluate = orig_evaluate_104

    # 108: MIXED-SOURCE SEND (Production fix, 2026-09-08) — one target's
    # marked media lives in the talent's individual WhatsApp chat, the
    # other in their WhatsApp group, in the SAME send_targets list (see
    # casting_pipeline.build_send_targets/media_assignment_worker.
    # _finish_multi_source_scan_sibling on the backend side, which is
    # what actually produces a mixed send_targets list like this in
    # production). _run_send must (a) open EACH item's own correct
    # source chat, never assuming the request-level group_name/
    # source_type applies to every item, and (b) SKIP the single upfront
    # "open once, abort the whole send if it fails" gate when targets
    # span more than one distinct source — since _send_one_target_
    # native_forward already re-opens the correct source per item
    # regardless, that upfront open is a redundant optimization for the
    # single-source case only, never a correctness requirement, and for
    # a mixed send it would incorrectly abort a working source's items
    # just because a DIFFERENT source failed to open upfront.
    calls_108: list = []

    async def _fake_open_group_108(page, group_name):
        calls_108.append(("open_group", group_name))
        return "OPENED"

    async def _fake_open_phone_108(page, phone):
        calls_108.append(("open_phone", phone))
        return "OPENED"

    async def _fake_forward_108(page, group_name, target, item_label="", source_type="group"):
        calls_108.append(("forward", group_name, source_type, target["source_message_id"]))
        return {"source_message_id": target["source_message_id"], "ok": True}

    async def _fake_text_108(page, destination_group, message, *, destination_type="group"):
        calls_108.append(("text", message))
        return {"ok": True}

    orig_open_group_108 = sender._open_group_chat
    orig_open_phone_108 = sender._open_chat_by_phone
    orig_forward_108 = mark_scan._send_one_target_native_forward
    orig_text_108 = mark_scan._send_text_message
    sender._open_group_chat = _fake_open_group_108
    sender._open_chat_by_phone = _fake_open_phone_108
    mark_scan._send_one_target_native_forward = _fake_forward_108
    mark_scan._send_text_message = _fake_text_108
    try:
        req_108 = {
            "group_name": "Shivi Rajput x Talentgram", "source_type": "group",
            "destination_group": "Dest Group", "project_label": "Vaseline",
            "send_targets": [
                {
                    "source_message_id": "phone-take1", "media_role": "take", "take_number": 1,
                    "source_media_type": "video", "destination_group": "Dest Group", "caption": "Audition Take",
                    "source_type": "phone", "source_group_name": "919990000111",
                },
                {
                    "source_message_id": "group-intro1", "media_role": "intro", "take_number": None,
                    "source_media_type": "video", "destination_group": "Dest Group", "caption": "Introduction Take",
                    "source_type": "group", "source_group_name": "Shivi Rajput x Talentgram",
                },
            ],
            "form_insert_index": 2, "form_message": None, "send_marker_on_success": False,
        }
        result_108 = asyncio.run(mark_scan._run_send(object(), req_108))
    finally:
        sender._open_group_chat = orig_open_group_108
        sender._open_chat_by_phone = orig_open_phone_108
        mark_scan._send_one_target_native_forward = orig_forward_108
        mark_scan._send_text_message = orig_text_108

    assert all(r["ok"] for r in result_108["results"]), result_108
    # The upfront single-source gate never ran at all (neither open_group
    # nor open_phone appears before the per-item forwards) — only the
    # per-item forwards, each carrying its OWN correct source.
    assert ("open_group", "Shivi Rajput x Talentgram") not in calls_108, calls_108
    assert ("open_phone", "919990000111") not in calls_108, calls_108
    assert calls_108 == [
        ("forward", "919990000111", "phone", "phone-take1"),
        ("forward", "Shivi Rajput x Talentgram", "group", "group-intro1"),
        ("text", "Thanks, shared for Vaseline."),
    ], calls_108
    print("108. SEND mixed-source per-target routing -> Take opened via the individual WhatsApp chat, Introduction via the WhatsApp group, in ONE send — upfront single-source gate correctly skipped")

    # ------------------------------------------------------------------
    # 109-121: SEND self-healing (Production fix, 2026-09-09) — real
    # incident: Padm Rautela / Mahindra Thar Film 1 & 2, Introduction
    # Take failed "could not reopen the marked media" while Take 1/Take
    # 2 succeeded moments apart in the SAME send. Root cause: nothing
    # above _open_media_and_get_forward_button's own internal
    # MAX_FORWARD_READINESS_ROUNDS ever retried the WHOLE item once that
    # exhausted. _send_one_target_native_forward is now a bounded
    # (MAX_SEND_ITEM_ATTEMPTS) retry wrapper around
    # _send_one_target_native_forward_attempt (the exact original
    # per-item logic, now with delivery verification appended) — these
    # tests exercise the wrapper's own orchestration directly (mocking
    # the attempt function, mirroring test 73's own style of mocking
    # _send_one_target_native_forward to test _run_send's orchestration
    # in isolation), plus one deeper integration test using REAL attempt
    # internals (mirroring test 84's mocking style) for the closest
    # available proxy to a live reproduction in this environment.
    # ------------------------------------------------------------------

    class _FakePageWithWait:
        def __init__(self):
            self.waits: list = []

        async def wait_for_timeout(self, ms):
            self.waits.append(ms)

        class mouse:
            @staticmethod
            async def click(x, y, button="left"):
                pass

    # 109 (Requirement A): reopen fails ONCE, second attempt succeeds.
    calls_109: list = []

    async def _fake_attempt_109(page, group_name, target, item_label="", source_type="group"):
        calls_109.append(target["source_message_id"])
        if len(calls_109) == 1:
            return {"ok": False, "source_message_id": target["source_message_id"], "error": "forward not ready: tile click failed after 3 attempts"}
        return {"ok": True, "source_message_id": target["source_message_id"], "send_state": "MESSAGE_SENT"}

    orig_attempt_109 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_109
    try:
        target_109 = _send_target("intro1", "intro")
        result_109 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePage73(), "Source Group", target_109))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_109

    assert result_109["ok"] is True, result_109
    assert len(calls_109) == 2, calls_109
    assert all(c == "intro1" for c in calls_109), calls_109  # exact same identity every attempt, never a substitute
    print("109. SEND self-healing: reopen fails once -> second attempt succeeds -> item ultimately SENT (Requirement A)")

    # 110 (Requirement B): reopen fails TWICE, third (final) attempt succeeds.
    calls_110: list = []

    async def _fake_attempt_110(page, group_name, target, item_label="", source_type="group"):
        calls_110.append(target["source_message_id"])
        if len(calls_110) < 3:
            return {"ok": False, "source_message_id": target["source_message_id"], "error": "forward not ready: no <video> mounted within 15s of click"}
        return {"ok": True, "source_message_id": target["source_message_id"], "send_state": "MESSAGE_SENT"}

    orig_attempt_110 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_110
    try:
        target_110 = _send_target("intro1", "intro")
        result_110 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePageWithWait(), "Source Group", target_110))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_110

    assert result_110["ok"] is True, result_110
    assert len(calls_110) == mark_scan.MAX_SEND_ITEM_ATTEMPTS == 3, calls_110
    print("110. SEND self-healing: reopen fails twice -> third/final attempt succeeds -> item ultimately SENT (Requirement B)")

    # 111 (Requirement C): permanently unavailable -> every attempt fails,
    # exhausted, reports a real actionable failure — never silently drops
    # the item, never claims success.
    calls_111: list = []

    async def _fake_attempt_111(page, group_name, target, item_label="", source_type="group"):
        calls_111.append(target["source_message_id"])
        return {"ok": False, "source_message_id": target["source_message_id"], "error": "forward not ready: source message no longer found in window"}

    orig_attempt_111 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_111
    try:
        target_111 = _send_target("intro1", "intro")
        result_111 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePageWithWait(), "Source Group", target_111))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_111

    assert result_111["ok"] is False, result_111
    assert len(calls_111) == mark_scan.MAX_SEND_ITEM_ATTEMPTS == 3, calls_111
    assert "no longer found" in result_111["error"], result_111  # the real, diagnosable reason survives, never genericized away
    print("111. SEND self-healing: permanently unavailable -> all bounded attempts exhausted -> real actionable failure reported, never a false success (Requirement C)")

    # 112: identity never drifts across attempts — every attempt is asked
    # for the EXACT SAME source_message_id, take_number, and media_role;
    # the wrapper itself carries no opportunity to substitute a different/
    # nearby item (Requirement Q).
    calls_112: list = []

    async def _fake_attempt_112(page, group_name, target, item_label="", source_type="group"):
        calls_112.append((target["source_message_id"], target["media_role"], target["take_number"]))
        return {"ok": False, "source_message_id": target["source_message_id"], "error": "forward not ready: tile click failed"} if len(calls_112) < 3 else {"ok": True, "source_message_id": target["source_message_id"]}

    orig_attempt_112 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_112
    try:
        target_112 = _send_target("take2-msgid", "take", 2)
        result_112 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePageWithWait(), "Source Group", target_112))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_112

    assert result_112["ok"] is True, result_112
    assert calls_112 == [("take2-msgid", "take", 2)] * 3, calls_112
    print("112. SEND self-healing: every recovery attempt targets the EXACT same source message/role/take — never a substitute nearby item (Requirement Q)")

    # 113: bounded backoff — a real page.wait_for_timeout(SEND_ITEM_RECOVERY_
    # BACKOFF_MS) happens exactly once, only before the FINAL attempt, never
    # before the first retry and never an unbounded/repeated wait.
    calls_113: list = []

    async def _fake_attempt_113(page, group_name, target, item_label="", source_type="group"):
        calls_113.append(1)
        return {"ok": False, "source_message_id": target["source_message_id"], "error": "x"} if len(calls_113) < 3 else {"ok": True, "source_message_id": target["source_message_id"]}

    orig_attempt_113 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_113
    try:
        page_113 = _FakePageWithWait()
        target_113 = _send_target("intro1", "intro")
        result_113 = asyncio.run(mark_scan._send_one_target_native_forward(page_113, "Source Group", target_113))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_113

    assert result_113["ok"] is True, result_113
    assert page_113.waits == [mark_scan.SEND_ITEM_RECOVERY_BACKOFF_MS], page_113.waits
    print("113. SEND self-healing: exactly one bounded backoff, only before the final attempt — never on the first retry, never unbounded")

    # 114: the normal, successful-on-first-try path pays ZERO extra
    # cost — no backoff wait at all when attempt 1 already succeeds
    # (Requirement 21 — do not slow down the common path).
    async def _fake_attempt_114(page, group_name, target, item_label="", source_type="group"):
        return {"ok": True, "source_message_id": target["source_message_id"]}

    orig_attempt_114 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_114
    try:
        page_114 = _FakePageWithWait()
        target_114 = _send_target("intro1", "intro")
        result_114 = asyncio.run(mark_scan._send_one_target_native_forward(page_114, "Source Group", target_114))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_114

    assert result_114["ok"] is True, result_114
    assert page_114.waits == [], page_114.waits
    print("114. SEND self-healing: a first-attempt success pays zero recovery overhead -> no backoff wait at all")

    # 115-117: source-type-agnostic recovery (Requirements E/F/G) — the
    # wrapper itself never inspects source_type at all; it just retries
    # whatever _send_one_target_native_forward_attempt is given, so group,
    # phone, and mixed-source items all recover identically. Proven here
    # by driving the SAME fails-once-then-succeeds sequence through each
    # source_type value.
    for source_type_11x, group_name_11x, test_num in (("group", "Talent Group", "115"), ("phone", "919990000222", "116")):
        calls_11x: list = []

        async def _fake_attempt_11x(page, group_name, target, item_label="", source_type="group"):
            calls_11x.append(source_type)
            return {"ok": False, "source_message_id": target["source_message_id"], "error": "x"} if len(calls_11x) == 1 else {"ok": True, "source_message_id": target["source_message_id"]}

        orig_attempt_11x = mark_scan._send_one_target_native_forward_attempt
        mark_scan._send_one_target_native_forward_attempt = _fake_attempt_11x
        try:
            target_11x = _send_target("intro1", "intro")
            result_11x = asyncio.run(mark_scan._send_one_target_native_forward(
                _FakePage73(), group_name_11x, target_11x, source_type=source_type_11x,
            ))
        finally:
            mark_scan._send_one_target_native_forward_attempt = orig_attempt_11x
        assert result_11x["ok"] is True, result_11x
        assert calls_11x == [source_type_11x, source_type_11x], calls_11x
        label = "group source" if source_type_11x == "group" else "individual phone source"
        print(f"{test_num}. SEND self-healing recovers identically for {label} (Requirement " + ("E)" if source_type_11x == "group" else "F)"))

    # 117 (Requirement G, mixed-source): each item in a mixed-source send
    # carries its OWN source_type — a Take marked in the phone chat that
    # needs recovery must reopen the PHONE source, never the group, and
    # vice versa for an Introduction marked in the group. Exercised via
    # _run_send's own real per-target source_type threading (unchanged),
    # with _send_one_target_native_forward itself mocked at the OUTER
    # level (as test 108 already does) plus a nested real-wrapper check:
    # this test instead drives the wrapper directly per item, mirroring
    # _run_send's own call shape for each of the two source types.
    calls_117: list = []

    async def _fake_attempt_117(page, group_name, target, item_label="", source_type="group"):
        calls_117.append((source_type, group_name, target["source_message_id"]))
        # The PHONE item fails once then recovers; the GROUP item
        # succeeds immediately — independent recovery per item, per
        # source, never conflated.
        if target["source_message_id"] == "phone-take1" and calls_117.count(("phone", "919990000111", "phone-take1")) == 1:
            return {"ok": False, "source_message_id": target["source_message_id"], "error": "x"}
        return {"ok": True, "source_message_id": target["source_message_id"]}

    orig_attempt_117 = mark_scan._send_one_target_native_forward_attempt
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_117
    try:
        phone_target_117 = _send_target("phone-take1", "take", 1)
        group_target_117 = _send_target("group-intro1", "intro")
        result_phone_117 = asyncio.run(mark_scan._send_one_target_native_forward(
            _FakePage73(), "919990000111", phone_target_117, source_type="phone",
        ))
        result_group_117 = asyncio.run(mark_scan._send_one_target_native_forward(
            _FakePage73(), "Shivi Rajput x Talentgram", group_target_117, source_type="group",
        ))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_117

    assert result_phone_117["ok"] is True, result_phone_117
    assert result_group_117["ok"] is True, result_group_117
    assert calls_117[0] == ("phone", "919990000111", "phone-take1"), calls_117
    assert calls_117[1] == ("phone", "919990000111", "phone-take1"), calls_117  # phone item recovered on its OWN source
    assert calls_117[2] == ("group", "Shivi Rajput x Talentgram", "group-intro1"), calls_117  # group item independent, never touched the phone recovery
    print("117. SEND self-healing: mixed-source recovery reopens each item's OWN correct source, independently (Requirement G)")

    # 118: delivery verification unit tests — _verify_forward_delivered
    # itself, in isolation (Requirement O — delivery verification failure
    # must not be silently accepted).
    async def _fake_open_118(page, source_type, group):
        return "OPENED"

    async def _fake_find_text_verified_118(page, needle, baselines=None):
        return "SCOPE conv-msg", needle, "conv-msg-REAL118"

    orig_open_118 = mark_scan._open_source_chat
    orig_find_118 = sender._find_outgoing_with_text
    mark_scan._open_source_chat = _fake_open_118
    sender._find_outgoing_with_text = _fake_find_text_verified_118
    try:
        result_118 = asyncio.run(mark_scan._verify_forward_delivered(_FakePage73(), "Dest Group", "Introduction Take"))
    finally:
        mark_scan._open_source_chat = orig_open_118
        sender._find_outgoing_with_text = orig_find_118
    assert result_118["verified"] is True, result_118
    assert result_118["message_id"] == "conv-msg-REAL118", result_118
    print("118. SEND delivery verification: caption found in destination -> verified=True with the matched message id")

    async def _fake_find_text_never_118b(page, needle, baselines=None):
        return None, "", None

    class _FakePage118b:
        async def wait_for_timeout(self, ms):
            pass

    orig_open_118b = mark_scan._open_source_chat
    orig_find_118b = sender._find_outgoing_with_text
    mark_scan._open_source_chat = _fake_open_118
    sender._find_outgoing_with_text = _fake_find_text_never_118b
    try:
        result_118b = asyncio.run(mark_scan._verify_forward_delivered(_FakePage118b(), "Dest Group", "Introduction Take"))
    finally:
        mark_scan._open_source_chat = orig_open_118b
        sender._find_outgoing_with_text = orig_find_118b
    assert result_118b["verified"] is False, result_118b
    assert "no NEW matching outgoing message" in result_118b["reason"], result_118b
    print("118b. SEND delivery verification: caption never found in destination -> verified=False, never assumed sent")

    # 119: a real, full-stack recovery reproduction — the closest available
    # proxy to a live test in this environment (no live WhatsApp browser
    # access here — see this task's final report). Uses REAL attempt
    # internals (mirroring test 84's own mocking style, one level lower
    # than the pure-wrapper tests above): _open_media_and_get_forward_button
    # fails ONCE (reproducing "could not reopen the marked media" exactly
    # as reported for Padm Rautela's Introduction) then succeeds, proving
    # the REAL _send_one_target_native_forward (source reopen + reacquire
    # + forward + destination-select + caption/send + delivery
    # verification) recovers end-to-end, not just the mocked-attempt
    # orchestration tested above.
    class _FakePage119:
        async def wait_for_timeout(self, ms):
            pass

        class mouse:
            @staticmethod
            async def click(x, y, button="left"):
                pass

    ready_calls_119: list = []

    async def _fake_open_group_119(page, group):
        return "OPENED"

    async def _fake_ready_119(page, group, msg_id, tile_index, is_photo):
        ready_calls_119.append(msg_id)
        if len(ready_calls_119) == 1:
            return {"ok": False, "reason": "tile click failed after 3 attempts: Locator.scroll_into_view_if_needed: Timeout 5000ms exceeded"}
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_119(page, dest):
        return {"ok": True}

    async def _fake_caption_send_119(page, caption):
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_119(page):
        return True

    async def _fake_find_text_119(page, needle, baselines=None):
        return "SCOPE conv-msg", needle, "conv-msg-VERIFIED119"

    orig_open_119 = sender._open_group_chat
    orig_ready_119 = mark_scan._open_media_and_get_forward_button
    orig_select_119 = mark_scan._select_forward_destination
    orig_caption_119 = mark_scan._enter_forward_caption_and_send
    orig_closed_119 = mark_scan._ensure_forward_dialog_closed
    orig_find_119 = sender._find_outgoing_with_text
    sender._open_group_chat = _fake_open_group_119
    mark_scan._open_media_and_get_forward_button = _fake_ready_119
    mark_scan._select_forward_destination = _fake_select_119
    mark_scan._enter_forward_caption_and_send = _fake_caption_send_119
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_119
    sender._find_outgoing_with_text = _fake_find_text_119
    try:
        target_119 = _send_target("intro-padm-rautela", "intro")
        result_119 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePage119(), "Padm Rautela x Talentgram", target_119))
    finally:
        sender._open_group_chat = orig_open_119
        mark_scan._open_media_and_get_forward_button = orig_ready_119
        mark_scan._select_forward_destination = orig_select_119
        mark_scan._enter_forward_caption_and_send = orig_caption_119
        mark_scan._ensure_forward_dialog_closed = orig_closed_119
        sender._find_outgoing_with_text = orig_find_119

    assert result_119["ok"] is True, result_119
    assert len(ready_calls_119) == 2, ready_calls_119  # exactly one failed reacquisition, one successful retry
    assert all(m == "intro-padm-rautela" for m in ready_calls_119), ready_calls_119
    print("119. SEND self-healing FULL-STACK reproduction (Padm Rautela / Mahindra Thar) — Introduction's exact reported failure now recovers on retry via the REAL attempt path, not just the mocked wrapper")

    # 120: the SAME full-stack scenario, but the media genuinely never
    # becomes available (every attempt's own _open_media_and_get_forward_
    # button fails) -> exhausted, real actionable failure, never a false
    # SENT.
    ready_calls_120: list = []

    async def _fake_ready_120(page, group, msg_id, tile_index, is_photo):
        ready_calls_120.append(msg_id)
        return {"ok": False, "reason": "source message no longer found in window"}

    orig_open_120 = sender._open_group_chat
    orig_ready_120 = mark_scan._open_media_and_get_forward_button
    sender._open_group_chat = _fake_open_group_119
    mark_scan._open_media_and_get_forward_button = _fake_ready_120
    try:
        target_120 = _send_target("intro-gone", "intro")
        result_120 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePage119(), "Some Group", target_120))
    finally:
        sender._open_group_chat = orig_open_120
        mark_scan._open_media_and_get_forward_button = orig_ready_120

    assert result_120["ok"] is False, result_120
    assert len(ready_calls_120) == mark_scan.MAX_SEND_ITEM_ATTEMPTS == 3, ready_calls_120
    assert "no longer found" in result_120["error"], result_120
    print("120. SEND self-healing FULL-STACK: media genuinely never available -> all bounded attempts exhausted, real reason reported, no false success")

    # 121: idempotency/RETRY behavior — already-sent items are resumed
    # correctly by the EXISTING architecture (Requirement D/P), proven at
    # the backend level in backend/tests/test_media_send.py's own
    # test_send_orchestrator_partial_failure_resumes_only_missing_item —
    # this worker-side test only confirms the piece that lives here:
    # _run_send never re-attempts an item that mode="send" dispatch
    # (backend-computed send_targets) simply never included in the first
    # place — i.e. this worker trusts send_targets completely and adds no
    # SECOND, competing notion of "already done" of its own.
    calls_121: list = []

    async def _fake_forward_121(page, group_name, target, item_label="", source_type="group"):
        calls_121.append(target["source_message_id"])
        return {"source_message_id": target["source_message_id"], "ok": True}

    async def _fake_text_121(page, destination_group, message, *, destination_type="group"):
        calls_121.append(("text", message))
        return {"ok": True}

    async def _fake_open_group_121(page, group):
        return "OPENED"

    orig_open_121 = sender._open_group_chat
    orig_forward_121 = mark_scan._send_one_target_native_forward
    orig_text_121 = mark_scan._send_text_message
    sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = (
        _fake_open_group_121, _fake_forward_121, _fake_text_121,
    )
    try:
        # Backend already excluded "take1"/"take2" from send_targets
        # (already SENT) — only "intro1" (previously FAILED/unverified)
        # is present, exactly what a RETRY re-dispatch of the SAME
        # approved plan produces via media_send.prepare_send_targets's
        # existing already_sent() filtering (backend/tests/test_media_
        # send.py's own idempotency tests cover THAT filtering directly).
        req_121 = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Vaseline",
            "send_targets": [_send_target("intro1", "intro")],
            "form_insert_index": 1, "form_message": None, "send_marker_on_success": True,
        }
        result_121 = asyncio.run(mark_scan._run_send(_FakePage73(), req_121))
    finally:
        sender._open_group_chat, mark_scan._send_one_target_native_forward, mark_scan._send_text_message = (
            orig_open_121, orig_forward_121, orig_text_121,
        )
    assert all(r["ok"] for r in result_121["results"]), result_121
    forward_calls_121 = [c for c in calls_121 if c == "intro1" or (isinstance(c, tuple) and c[0] != "text")]
    assert forward_calls_121 == ["intro1"], calls_121  # take1/take2 never re-appear here — RETRY only ever touches what the backend re-includes
    print("121. SEND self-healing/RETRY: the worker only ever forwards what send_targets actually contains — a RETRY re-dispatch (backend-filtered to just the failed item) never re-touches already-sent media (Requirement D/P)")

    # ------------------------------------------------------------------
    # 122-131: STRENGTHENED delivery verification (Production fix,
    # 2026-09-10 — follow-up audit). Finding: a caption-only check with
    # NO baseline proves only "this caption exists somewhere in the last
    # few destination messages" — NOT "the CURRENT attempt created a new
    # message". media_assignment.simple_role_label's own captions are
    # deliberately generic ("Introduction Take"/"Photo" carry no talent/
    # project/take distinction at all — see that function's own
    # docstring), so an un-baselined check would accept a COMPLETELY
    # DIFFERENT talent's earlier Introduction/Photo forward to the SAME
    # shared casting-group destination as "proof" THIS talent's item
    # delivered. Conclusion: (B) delivery verification COULD false-
    # positive without a baseline — fixed by _capture_destination_
    # baseline + threading `baselines` through _verify_forward_delivered
    # (see both docstrings for the full writeup).
    #
    # These tests fake sender._find_outgoing_with_text/sender.
    # _snapshot_msg_baselines against a tiny SIMULATED destination
    # message list (_SimDestination — a plain Python list of strings,
    # not a real DOM) so the baseline-vs-no-baseline distinction is
    # provable directly and deterministically. sender.py's own real DOM-
    # matching primitives (_is_outgoing_msg, the actual selector chain)
    # are unchanged and already covered by test_sender.py — these tests
    # are about THIS module's own integration (does it correctly thread
    # a fresh, per-item, per-destination baseline through and treat
    # "before" vs "after" correctly), not sender.py's DOM heuristics.
    # ------------------------------------------------------------------

    class _SimDestination:
        """A tiny simulated destination chat's message list — one string
        per outgoing message. A deterministic stand-in for "what
        messages currently exist", never a DOM fake."""
        def __init__(self, initial=None):
            self.messages: list = list(initial or [])

        def send(self, text):
            self.messages.append(text)

    def _install_sim_destination_fakes(sims_by_dest: dict):
        """`sims_by_dest` maps destination_group -> _SimDestination. The
        fakes track which destination is "currently open" (mirroring
        _open_source_chat's own real effect of making #main show that
        chat) so a snapshot/find call always operates on the RIGHT
        destination's own simulated list — this is what makes the
        multi-talent/multi-destination isolation test (130) meaningful
        rather than trivially true."""
        state = {"open": None}

        async def _fake_open_dest(page, source_type, group):
            state["open"] = group
            return "OPENED"

        async def _fake_snapshot(page):
            sim = sims_by_dest[state["open"]]
            return {"SIM": len(sim.messages)}

        async def _fake_find_text(page, needle, baselines=None):
            sim = sims_by_dest[state["open"]]
            start = baselines.get("SIM", 0) if baselines else max(0, len(sim.messages) - 8)
            for i in range(start, len(sim.messages)):
                if needle in sim.messages[i]:
                    return "SIM", sim.messages[i], f"conv-msg-SIM{i}"
            return None, "", None

        return state, _fake_open_dest, _fake_snapshot, _fake_find_text

    # 122: an OLD matching caption (already there before this item's own
    # baseline) must NOT count as this attempt's delivery.
    sim_122 = _SimDestination(["Introduction Take"])  # a different talent's earlier, unrelated send
    _, fake_open_122, fake_snap_122, fake_find_122 = _install_sim_destination_fakes({"Dest Group": sim_122})
    orig_open_122 = mark_scan._open_source_chat
    orig_snap_122 = sender._snapshot_msg_baselines
    orig_find_122 = sender._find_outgoing_with_text
    mark_scan._open_source_chat, sender._snapshot_msg_baselines, sender._find_outgoing_with_text = (
        fake_open_122, fake_snap_122, fake_find_122,
    )
    try:
        baseline_122 = asyncio.run(mark_scan._capture_destination_baseline(_FakePageWithWait(), "Dest Group"))
        # No new message is ever added to sim_122 — the old one is all
        # there is.
        result_122 = asyncio.run(mark_scan._verify_forward_delivered(
            _FakePageWithWait(), "Dest Group", "Introduction Take", baselines=baseline_122.get("baselines"),
        ))
    finally:
        mark_scan._open_source_chat, sender._snapshot_msg_baselines, sender._find_outgoing_with_text = (
            orig_open_122, orig_snap_122, orig_find_122,
        )
    assert baseline_122["ok"] is True, baseline_122
    assert result_122["verified"] is False, result_122
    print("122. Delivery verification: an OLD matching caption (present before this item's own baseline) does NOT count as delivery")

    # 123: a genuinely NEW matching outgoing message (appended AFTER the
    # baseline was captured) DOES count.
    sim_123 = _SimDestination(["Introduction Take"])  # same old message still present
    _, fake_open_123, fake_snap_123, fake_find_123 = _install_sim_destination_fakes({"Dest Group": sim_123})
    orig_open_123 = mark_scan._open_source_chat
    orig_snap_123 = sender._snapshot_msg_baselines
    orig_find_123 = sender._find_outgoing_with_text
    mark_scan._open_source_chat, sender._snapshot_msg_baselines, sender._find_outgoing_with_text = (
        fake_open_123, fake_snap_123, fake_find_123,
    )
    try:
        baseline_123 = asyncio.run(mark_scan._capture_destination_baseline(_FakePageWithWait(), "Dest Group"))
        sim_123.send("Introduction Take")  # THIS item's own forward actually lands now
        result_123 = asyncio.run(mark_scan._verify_forward_delivered(
            _FakePageWithWait(), "Dest Group", "Introduction Take", baselines=baseline_123.get("baselines"),
        ))
    finally:
        mark_scan._open_source_chat, sender._snapshot_msg_baselines, sender._find_outgoing_with_text = (
            orig_open_123, orig_snap_123, orig_find_123,
        )
    assert result_123["verified"] is True, result_123
    print("123. Delivery verification: a NEWLY appeared matching outgoing message (after this item's own baseline) DOES count as delivery")

    # 124: full-stack — attempt 1's Send click does NOT actually land
    # (sim never receives the message, simulating a silent forward
    # failure past the point of clicking Send), so verification
    # correctly reports unverified and the bounded recovery wrapper
    # retries; attempt 2's click DOES land -> verified, ultimately SENT.
    # Never falsely marked SENT on attempt 1.
    sim_124 = _SimDestination()
    dest_state_124, fake_open_dest_124, fake_snap_124, fake_find_124 = _install_sim_destination_fakes({"Padm Group": sim_124})
    attempt_count_124 = {"n": 0}

    async def _fake_open_group_124(page, group):
        return "OPENED"

    async def _fake_ready_124(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_124(page, dest):
        return {"ok": True}

    async def _fake_caption_send_124(page, caption):
        attempt_count_124["n"] += 1
        if attempt_count_124["n"] == 2:
            sim_124.send(caption)  # only the SECOND attempt's click actually delivers
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_124(page):
        return True

    orig_open_group_124 = sender._open_group_chat
    orig_ready_124 = mark_scan._open_media_and_get_forward_button
    orig_select_124 = mark_scan._select_forward_destination
    orig_caption_124 = mark_scan._enter_forward_caption_and_send
    orig_closed_124 = mark_scan._ensure_forward_dialog_closed
    orig_opensrc_124 = mark_scan._open_source_chat
    orig_snap_124_ = sender._snapshot_msg_baselines
    orig_find_124_ = sender._find_outgoing_with_text

    async def _dispatching_open_source_124(page, source_type, group_name):
        # Source opens are always "group"/talent-group here; destination
        # opens (delivery verification) go through the SAME _open_source_
        # chat call but always with source_type="group" and the
        # destination's own name — the sim-destination fake keys on
        # "which destination is currently open", so route THOSE calls to
        # it while any other (source) open trivially succeeds.
        if group_name == "Padm Group":
            return await fake_open_dest_124(page, source_type, group_name)
        return "OPENED"

    sender._open_group_chat = _fake_open_group_124
    mark_scan._open_media_and_get_forward_button = _fake_ready_124
    mark_scan._select_forward_destination = _fake_select_124
    mark_scan._enter_forward_caption_and_send = _fake_caption_send_124
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_124
    mark_scan._open_source_chat = _dispatching_open_source_124
    sender._snapshot_msg_baselines = fake_snap_124
    sender._find_outgoing_with_text = fake_find_124
    try:
        target_124 = _send_target("intro-padm-124", "intro")
        target_124["destination_group"] = "Padm Group"
        result_124 = asyncio.run(mark_scan._send_one_target_native_forward(_FakePageWithWait(), "Padm Rautela x Talentgram", target_124))
    finally:
        sender._open_group_chat = orig_open_group_124
        mark_scan._open_media_and_get_forward_button = orig_ready_124
        mark_scan._select_forward_destination = orig_select_124
        mark_scan._enter_forward_caption_and_send = orig_caption_124
        mark_scan._ensure_forward_dialog_closed = orig_closed_124
        mark_scan._open_source_chat = orig_opensrc_124
        sender._snapshot_msg_baselines = orig_snap_124_
        sender._find_outgoing_with_text = orig_find_124_

    assert result_124["ok"] is True, result_124
    assert attempt_count_124["n"] == 2, attempt_count_124  # attempt 1's click didn't verify -> real recovery, never a false SENT
    print("124. Delivery verification full-stack: attempt 1's Send click doesn't actually deliver -> unverified -> real bounded recovery -> attempt 2 delivers and verifies -> SENT (never falsely marked SENT on attempt 1)")

    # 125: while Introduction is being recovered (multiple attempts),
    # Take 1 — already verified SENT earlier in the SAME _run_send call —
    # is never re-invoked. Each item's own attempt function call count is
    # independent.
    calls_125: list = []

    async def _fake_attempt_125(page, group_name, target, item_label="", source_type="group"):
        calls_125.append(target["source_message_id"])
        if target["source_message_id"] == "intro1" and calls_125.count("intro1") < 2:
            return {"ok": False, "source_message_id": target["source_message_id"], "error": "x"}
        return {"ok": True, "source_message_id": target["source_message_id"]}

    async def _fake_open_group_125(page, group):
        return "OPENED"

    orig_attempt_125 = mark_scan._send_one_target_native_forward_attempt
    orig_open_125 = sender._open_group_chat
    mark_scan._send_one_target_native_forward_attempt = _fake_attempt_125
    sender._open_group_chat = _fake_open_group_125
    try:
        req_125 = {
            "group_name": "Source Group", "destination_group": "Dest Group", "project_label": "Vaseline",
            "send_targets": [_send_target("take1", "take", 1), _send_target("intro1", "intro")],
            "form_insert_index": 2, "form_message": None, "send_marker_on_success": False,
        }
        result_125 = asyncio.run(mark_scan._run_send(_FakePageWithWait(), req_125))
    finally:
        mark_scan._send_one_target_native_forward_attempt = orig_attempt_125
        sender._open_group_chat = orig_open_125

    assert all(r["ok"] for r in result_125["results"]), result_125
    assert calls_125.count("take1") == 1, calls_125  # Take 1's own successful attempt is never repeated
    assert calls_125.count("intro1") == 2, calls_125  # only Introduction's own recovery re-attempts
    print("125. SEND self-healing: Take 1 (already verified SENT) is never re-invoked while Introduction is independently recovered")

    # 126: repeated SEND remains idempotent — this worker-side property
    # (never re-forwarding an item send_targets doesn't include) is
    # already proven by test 121 above; the BACKEND-side idempotency
    # that actually computes/filters send_targets
    # (media_send.prepare_send_targets' already_sent() filtering) is
    # covered directly and extensively in backend/tests/test_media_send.py
    # (e.g. test_send_orchestrator_idempotent_no_resend,
    # test_send_orchestrator_partial_failure_resumes_only_missing_item) —
    # not duplicated here.
    print("126. SEND idempotency on repeated SEND: covered by test 121 (worker) + backend/tests/test_media_send.py's own idempotency tests (backend) — not duplicated here")

    # 127-129: source-type-agnostic STRENGTHENED verification — group,
    # phone, and mixed source, each using the REAL _capture_destination_
    # baseline + _verify_forward_delivered flow (not the higher-level
    # mock used by tests 115-117) against a sim destination, proving the
    # baseline logic itself is unaffected by which chat the SOURCE media
    # came from (the destination-side baseline/verify logic never reads
    # source_type at all — only _open_source_chat's SOURCE-opening call
    # does, which is a completely separate call from the destination
    # baseline/verify opens).
    for source_type_12x, source_name_12x, test_num_12x in (("group", "Talent Group", "127"), ("phone", "919990000333", "128")):
        sim_12x = _SimDestination(["Audition Take 1"])  # an unrelated older message already present
        _, fake_open_12x, fake_snap_12x, fake_find_12x = _install_sim_destination_fakes({"Dest Group": sim_12x})

        async def _fake_ready_12x(page, group, msg_id, tile_index, is_photo):
            return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

        async def _fake_select_12x(page, dest):
            return {"ok": True}

        async def _fake_caption_send_12x(page, caption, _sim=sim_12x):
            _sim.send(caption)
            return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

        async def _fake_ensure_closed_12x(page):
            return True

        async def _fake_open_group_12x(page, group):
            return "OPENED"

        async def _fake_open_phone_12x(page, phone):
            return "OPENED"

        async def _dispatching_open_12x(page, source_type, group_name, _fake_open_dest=fake_open_12x):
            if group_name == "Dest Group":
                return await _fake_open_dest(page, source_type, group_name)
            return "OPENED"

        orig_open_group_12x = sender._open_group_chat
        orig_open_phone_12x = sender._open_chat_by_phone
        orig_ready_12x = mark_scan._open_media_and_get_forward_button
        orig_select_12x = mark_scan._select_forward_destination
        orig_caption_12x = mark_scan._enter_forward_caption_and_send
        orig_closed_12x = mark_scan._ensure_forward_dialog_closed
        orig_opensrc_12x = mark_scan._open_source_chat
        orig_snap_12x = sender._snapshot_msg_baselines
        orig_find_12x = sender._find_outgoing_with_text
        sender._open_group_chat = _fake_open_group_12x
        sender._open_chat_by_phone = _fake_open_phone_12x
        mark_scan._open_media_and_get_forward_button = _fake_ready_12x
        mark_scan._select_forward_destination = _fake_select_12x
        mark_scan._enter_forward_caption_and_send = _fake_caption_send_12x
        mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_12x
        mark_scan._open_source_chat = _dispatching_open_12x
        sender._snapshot_msg_baselines = fake_snap_12x
        sender._find_outgoing_with_text = fake_find_12x
        try:
            target_12x = _send_target("take2-msgid", "take", 2)
            target_12x["destination_group"] = "Dest Group"
            target_12x["caption"] = "Audition Take 2"  # the real role caption, not the test helper's default (msg id)
            result_12x = asyncio.run(mark_scan._send_one_target_native_forward(
                _FakePageWithWait(), source_name_12x, target_12x, source_type=source_type_12x,
            ))
        finally:
            sender._open_group_chat = orig_open_group_12x
            sender._open_chat_by_phone = orig_open_phone_12x
            mark_scan._open_media_and_get_forward_button = orig_ready_12x
            mark_scan._select_forward_destination = orig_select_12x
            mark_scan._enter_forward_caption_and_send = orig_caption_12x
            mark_scan._ensure_forward_dialog_closed = orig_closed_12x
            mark_scan._open_source_chat = orig_opensrc_12x
            sender._snapshot_msg_baselines = orig_snap_12x
            sender._find_outgoing_with_text = orig_find_12x
        assert result_12x["ok"] is True, result_12x
        # The pre-existing "Audition Take 1" (a different item entirely,
        # already present before this item's own baseline) is never
        # mistaken for THIS item's ("Audition Take 2") delivery — the
        # match found is strictly the newly appended one.
        assert sim_12x.messages == ["Audition Take 1", "Audition Take 2"], sim_12x.messages
        label = "group source" if source_type_12x == "group" else "individual phone source"
        print(f"{test_num_12x}. Strengthened delivery verification works identically for {label} — baseline logic never depends on source_type")

    # 129: mixed source — Take from phone, Introduction from group, in
    # ONE send, both verified via their own fresh per-item baseline
    # against the SAME shared destination.
    sim_129 = _SimDestination()

    async def _fake_ready_129(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_129(page, dest):
        return {"ok": True}

    async def _fake_caption_send_129(page, caption):
        sim_129.send(caption)
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_129(page):
        return True

    async def _fake_open_group_129(page, group):
        return "OPENED"

    async def _fake_open_phone_129(page, phone):
        return "OPENED"

    dest_state_129, fake_open_dest_129, fake_snap_129, fake_find_129 = _install_sim_destination_fakes({"Dest Group": sim_129})

    async def _dispatching_open_129(page, source_type, group_name):
        if group_name == "Dest Group":
            return await fake_open_dest_129(page, source_type, group_name)
        return "OPENED"

    orig_open_group_129 = sender._open_group_chat
    orig_open_phone_129 = sender._open_chat_by_phone
    orig_ready_129 = mark_scan._open_media_and_get_forward_button
    orig_select_129 = mark_scan._select_forward_destination
    orig_caption_129 = mark_scan._enter_forward_caption_and_send
    orig_closed_129 = mark_scan._ensure_forward_dialog_closed
    orig_opensrc_129 = mark_scan._open_source_chat
    orig_snap_129 = sender._snapshot_msg_baselines
    orig_find_129 = sender._find_outgoing_with_text
    sender._open_group_chat = _fake_open_group_129
    sender._open_chat_by_phone = _fake_open_phone_129
    mark_scan._open_media_and_get_forward_button = _fake_ready_129
    mark_scan._select_forward_destination = _fake_select_129
    mark_scan._enter_forward_caption_and_send = _fake_caption_send_129
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_129
    mark_scan._open_source_chat = _dispatching_open_129
    sender._snapshot_msg_baselines = fake_snap_129
    sender._find_outgoing_with_text = fake_find_129
    try:
        phone_target_129 = _send_target("phone-take1-129", "take", 1)
        phone_target_129["destination_group"] = "Dest Group"
        phone_target_129["caption"] = "Audition Take 1"
        group_target_129 = _send_target("group-intro1-129", "intro")
        group_target_129["destination_group"] = "Dest Group"
        group_target_129["caption"] = "Introduction Take"
        result_phone_129 = asyncio.run(mark_scan._send_one_target_native_forward(
            _FakePageWithWait(), "919990000111", phone_target_129, source_type="phone",
        ))
        result_group_129 = asyncio.run(mark_scan._send_one_target_native_forward(
            _FakePageWithWait(), "Shivi Rajput x Talentgram", group_target_129, source_type="group",
        ))
    finally:
        sender._open_group_chat = orig_open_group_129
        sender._open_chat_by_phone = orig_open_phone_129
        mark_scan._open_media_and_get_forward_button = orig_ready_129
        mark_scan._select_forward_destination = orig_select_129
        mark_scan._enter_forward_caption_and_send = orig_caption_129
        mark_scan._ensure_forward_dialog_closed = orig_closed_129
        mark_scan._open_source_chat = orig_opensrc_129
        sender._snapshot_msg_baselines = orig_snap_129
        sender._find_outgoing_with_text = orig_find_129

    assert result_phone_129["ok"] is True, result_phone_129
    assert result_group_129["ok"] is True, result_group_129
    assert sim_129.messages == ["Audition Take 1", "Introduction Take"], sim_129.messages
    print("129. Strengthened delivery verification works for a mixed-source send — Take (phone) and Introduction (group) each verified against their own fresh baseline in the same shared destination")

    # 130: MULTI-TALENT ISOLATION — the exact false-positive scenario
    # this whole fix targets. Talent A's Introduction is already
    # delivered to the shared destination casting group. Talent B's
    # Introduction (a COMPLETELY different item, same generic caption)
    # is then sent to the SAME destination — B's own baseline is
    # captured AFTER A's message already exists, so B's verification
    # only accepts a message strictly newer than that baseline: it can
    # never be satisfied by A's older message, only by B's own new one.
    sim_130 = _SimDestination(["Introduction Take"])  # Talent A's own, already-delivered Introduction
    _, fake_open_130, fake_snap_130, fake_find_130 = _install_sim_destination_fakes({"Shared Casting Group": sim_130})

    async def _fake_ready_130(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_130(page, dest):
        return {"ok": True}

    async def _fake_caption_send_130(page, caption):
        sim_130.send(caption)  # Talent B's OWN forward actually lands here
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_130(page):
        return True

    async def _fake_open_group_130(page, group):
        return "OPENED"

    async def _dispatching_open_130(page, source_type, group_name):
        if group_name == "Shared Casting Group":
            return await fake_open_130(page, source_type, group_name)
        return "OPENED"

    orig_open_group_130 = sender._open_group_chat
    orig_ready_130 = mark_scan._open_media_and_get_forward_button
    orig_select_130 = mark_scan._select_forward_destination
    orig_caption_130 = mark_scan._enter_forward_caption_and_send
    orig_closed_130 = mark_scan._ensure_forward_dialog_closed
    orig_opensrc_130 = mark_scan._open_source_chat
    orig_snap_130 = sender._snapshot_msg_baselines
    orig_find_130 = sender._find_outgoing_with_text
    sender._open_group_chat = _fake_open_group_130
    mark_scan._open_media_and_get_forward_button = _fake_ready_130
    mark_scan._select_forward_destination = _fake_select_130
    mark_scan._enter_forward_caption_and_send = _fake_caption_send_130
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_130
    mark_scan._open_source_chat = _dispatching_open_130
    sender._snapshot_msg_baselines = fake_snap_130
    sender._find_outgoing_with_text = fake_find_130
    try:
        talent_b_target_130 = _send_target("intro-talentB-130", "intro")
        talent_b_target_130["destination_group"] = "Shared Casting Group"
        talent_b_target_130["caption"] = "Introduction Take"
        result_130 = asyncio.run(mark_scan._send_one_target_native_forward(
            _FakePageWithWait(), "Talent B x Talentgram", talent_b_target_130,
        ))
    finally:
        sender._open_group_chat = orig_open_group_130
        mark_scan._open_media_and_get_forward_button = orig_ready_130
        mark_scan._select_forward_destination = orig_select_130
        mark_scan._enter_forward_caption_and_send = orig_caption_130
        mark_scan._ensure_forward_dialog_closed = orig_closed_130
        mark_scan._open_source_chat = orig_opensrc_130
        sender._snapshot_msg_baselines = orig_snap_130
        sender._find_outgoing_with_text = orig_find_130

    assert result_130["ok"] is True, result_130
    # Exactly ONE new "Introduction Take" was added — Talent B's own.
    # Talent A's older one is untouched, never double-counted or reused.
    assert sim_130.messages == ["Introduction Take", "Introduction Take"], sim_130.messages
    assert result_130["verified_message_id"] == "conv-msg-SIM1", result_130  # the SECOND (index 1, Talent B's own) message — never index 0 (Talent A's)
    print("130. Multi-talent isolation: Talent B's Introduction verifies against ITS OWN new message, never Talent A's already-delivered, identically-captioned Introduction to the same shared destination (the exact false-positive this fix targets)")

    # ------------------------------------------------------------------
    # 131-138: INTRODUCTION-vs-AUDITION-TAKE native-forward audit
    # (Production fix, 2026-09-10 — real recurring incident, e.g.
    # Krishnaa Kilikar/Lava: Audition Take reliably sends, Introduction
    # repeatedly fails with "Send control could not be confirmed").
    #
    # AUDIT FINDING: source reopen, media-viewer readiness, the Forward
    # button, and destination selection are ALL role-agnostic and were
    # ALL already confirmed working (the reported failure is never
    # "forward not ready"/"destination selection failed" — see the
    # humanized error text itself). The concrete, code-level difference
    # is entirely at the caption/compose-box step
    # (_enter_forward_caption_and_send): when the SOURCE message being
    # forwarded already carries its OWN caption (as posted by the
    # talent), WhatsApp shows a "Remove caption" (X) control over the
    # video instead of our own compose box. Introduction videos are a
    # presentational piece of content a talent is naturally more likely
    # to caption when originally posting it than a raw Audition Take
    # clip — this is the concrete difference, not "WhatsApp Web can be
    # unreliable". The fix (already applied above): the removal
    # check+click now retries on EVERY bounded round (previously only
    # once), the round budget was raised 3->5 to give this genuinely
    # multi-step condition room to complete, and the removal control's
    # own search was widened from button-only to any icon/aria-label
    # element (_FORWARD_DIALOG_DUMP_JS's new `iconControls`).
    # ------------------------------------------------------------------

    class _FakeComposeBoxPage:
        """Simulates the forward dialog's own compose-box readiness:
        `.locator(...).first.click()` raises until `clear_after` real
        "remove existing caption" clicks (via page.mouse.click, the SAME
        mechanism the real removal control uses) have registered — 0
        means the box is available immediately (no existing caption at
        all, the Audition Take case); N>0 means N rounds are needed
        first (the Introduction-with-existing-caption case)."""
        def __init__(self, clear_after: int = 0):
            self.clear_after = clear_after
            self.remove_clicks = 0
            self.box_click_attempts = 0
            self.waits: list = []
            self.typed_caption = None
            outer = self

            class _Mouse:
                @staticmethod
                async def click(x, y, button="left"):
                    # Only a click at the simulated "Remove caption"
                    # control's own center (its rect is [50, 50, 20, 20]
                    # in _fake_evaluate_existing_caption_factory below)
                    # counts here — the SAME page.mouse.click primitive
                    # is also used, unrelated, by _send_one_target_
                    # native_forward_attempt's own Forward-button click
                    # (a different rect/center entirely), which must
                    # never be conflated with a caption-removal attempt.
                    if (x, y) == (60, 60):
                        outer.remove_clicks += 1

            self.mouse = _Mouse()

        def locator(self, sel):
            return self

        @property
        def first(self):
            return self

        async def click(self, timeout=3000):
            self.box_click_attempts += 1
            if self.remove_clicks < self.clear_after:
                raise Exception("compose box not yet available (existing caption still showing)")

        async def type(self, text, delay=10):
            self.typed_caption = text

        async def wait_for_timeout(self, ms):
            self.waits.append(ms)

    def _fake_evaluate_existing_caption_factory(via: str = "iconControls"):
        """`via` chooses whether the simulated "Remove caption" control
        is found through the ORIGINAL buttons-only search or the NEW
        widened iconControls search — both are exercised across tests
        131-138 so the widening itself (not just the retry-every-round
        fix) is directly proven."""
        async def _fake_evaluate(page, js, arg=None, timeout=10.0):
            if page.remove_clicks < page.clear_after:
                control = {"ariaLabel": "Remove caption", "testid": None, "dataIcon": "x-viewer", "role": None, "rect": [50, 50, 20, 20], "text": ""}
                return {
                    "dialogFound": True, "textboxes": [], "listItems": [],
                    "buttons": [control] if via == "buttons" else [],
                    "iconControls": [control] if via == "iconControls" else [],
                }
            return {"dialogFound": True, "textboxes": [], "listItems": [], "buttons": [], "iconControls": []}
        return _fake_evaluate

    # 131: Introduction with NO existing caption (clear_after=0) — the
    # box is available immediately, same as an ordinary Audition Take;
    # zero removal attempts needed.
    page_131 = _FakeComposeBoxPage(clear_after=0)
    orig_evaluate_131 = mark_scan._evaluate
    orig_find_send_131 = sender._find_and_click_send

    async def _fake_find_send_131(page, allow_enter_fallback=False):
        return "[aria-label^=\"Send\"]"

    mark_scan._evaluate = _fake_evaluate_existing_caption_factory()
    sender._find_and_click_send = _fake_find_send_131
    try:
        result_131 = asyncio.run(mark_scan._enter_forward_caption_and_send(page_131, "Introduction Take"))
    finally:
        mark_scan._evaluate = orig_evaluate_131
        sender._find_and_click_send = orig_find_send_131
    assert result_131["ok"] is True, result_131
    assert page_131.remove_clicks == 0, page_131.remove_clicks
    print("131. Introduction with no existing caption follows the exact same immediate-success path as Audition Take")

    # 132: Introduction WITH an existing caption that takes 2 rounds to
    # clear — succeeds within the (now 5-round) budget, retrying the
    # removal click on EVERY round rather than just once.
    page_132 = _FakeComposeBoxPage(clear_after=2)
    orig_evaluate_132 = mark_scan._evaluate
    orig_find_send_132 = sender._find_and_click_send
    mark_scan._evaluate = _fake_evaluate_existing_caption_factory()
    sender._find_and_click_send = _fake_find_send_131
    try:
        result_132 = asyncio.run(mark_scan._enter_forward_caption_and_send(page_132, "Introduction Take"))
    finally:
        mark_scan._evaluate = orig_evaluate_132
        sender._find_and_click_send = orig_find_send_132
    assert result_132["ok"] is True, result_132
    assert page_132.remove_clicks == 2, page_132.remove_clicks
    print("132. Introduction whose existing caption takes multiple removal-click rounds to clear now succeeds — the removal click retries every round, not just once")

    # 133: the widened iconControls search itself — the SAME scenario as
    # 132, but the "Remove caption" control is ONLY discoverable via the
    # NEW iconControls collection (not buttons), proving the widening in
    # _FORWARD_DIALOG_DUMP_JS/_find_remove_caption_button is what makes
    # this case findable at all, not just the retry-every-round change.
    page_133 = _FakeComposeBoxPage(clear_after=1)
    orig_evaluate_133 = mark_scan._evaluate
    orig_find_send_133 = sender._find_and_click_send
    mark_scan._evaluate = _fake_evaluate_existing_caption_factory(via="iconControls")
    sender._find_and_click_send = _fake_find_send_131
    try:
        result_133 = asyncio.run(mark_scan._enter_forward_caption_and_send(page_133, "Introduction Take"))
    finally:
        mark_scan._evaluate = orig_evaluate_133
        sender._find_and_click_send = orig_find_send_133
    assert result_133["ok"] is True, result_133
    assert page_133.remove_clicks == 1, page_133.remove_clicks
    print("133. The existing-caption removal control is found via the WIDENED iconControls search even when it is not a real button/[role=\"button\"] element")

    # 134: existing caption that NEVER clears within the bounded 5-round
    # budget -> a real, actionable failure (never a false success),
    # with the actual removal-attempt count reported.
    page_134 = _FakeComposeBoxPage(clear_after=99)
    orig_evaluate_134 = mark_scan._evaluate
    mark_scan._evaluate = _fake_evaluate_existing_caption_factory()
    try:
        result_134 = asyncio.run(mark_scan._enter_forward_caption_and_send(page_134, "Introduction Take"))
    finally:
        mark_scan._evaluate = orig_evaluate_134
    assert result_134["ok"] is False, result_134
    assert "caption entry failed" in result_134["reason"], result_134
    assert page_134.remove_clicks == mark_scan._CAPTION_BOX_MAX_ROUNDS, page_134.remove_clicks
    print("134. An existing caption that genuinely never clears within the bounded round budget is a real, actionable failure — never a false success")

    # 135 — THE KEY regression scenario: Take 1 succeeds immediately (no
    # existing caption); Introduction's FIRST outer attempt never clears
    # its existing caption within ITS OWN 5 rounds (a genuinely transient
    # WhatsApp Web state — the caption UI hadn't finished settling yet);
    # the bounded OUTER recovery wrapper retries the WHOLE item; the
    # SECOND attempt's compose-box state has since settled and clears
    # after 1 round -> succeeds. Final result: COMPLETE, Take 1 sent
    # EXACTLY ONCE (never re-forwarded while Introduction recovers).
    outer_attempt_135 = {"n": 0}
    intro_page_by_attempt_135: dict = {}

    async def _fake_open_source_135(page, source_type, group_name):
        return "OPENED"

    async def _fake_ready_135(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_135(page, dest):
        return {"ok": True}

    call_log_135: list = []
    sim_135_ref: list = []  # populated once sim_135 exists, below

    async def _fake_caption_send_take1_135(page, caption):
        call_log_135.append("take1")
        sim_135_ref[0].send(caption)
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_caption_send_intro_135(page, caption):
        outer_attempt_135["n"] += 1
        n = outer_attempt_135["n"]
        call_log_135.append(f"intro-attempt-{n}")
        # Attempt 1: existing caption never clears within this attempt's
        # own bounded rounds -> the REAL _enter_forward_caption_and_send
        # logic runs (not mocked here — this fake stands in for the
        # WHOLE caption+send step per outer attempt, mirroring how
        # _send_one_target_native_forward_attempt's OTHER steps are
        # already mocked in every earlier full-stack test in this file).
        # Attempt 2: clears immediately (the transient condition
        # resolved itself between attempts, exactly like a real
        # WhatsApp Web hiccup would).
        if n == 1:
            return {"ok": False, "reason": "caption entry failed: compose box not yet available (existing caption still showing)"}
        sim_135_ref[0].send(caption)
        return {"ok": True, "selector_used": "[aria-label^=\"Send\"]"}

    async def _fake_ensure_closed_135(page):
        return True

    sim_135 = _SimDestination()
    sim_135_ref.append(sim_135)
    _, fake_open_dest_135, fake_snap_135, fake_find_135 = _install_sim_destination_fakes({"Dest Group": sim_135})

    async def _dispatching_open_135(page, source_type, group_name):
        if group_name == "Dest Group":
            return await fake_open_dest_135(page, source_type, group_name)
        return "OPENED"

    orig_ready_135 = mark_scan._open_media_and_get_forward_button
    orig_select_135 = mark_scan._select_forward_destination
    orig_closed_135 = mark_scan._ensure_forward_dialog_closed
    orig_opensrc_135 = mark_scan._open_source_chat
    orig_snap_135 = sender._snapshot_msg_baselines
    orig_find_text_135 = sender._find_outgoing_with_text
    orig_caption_135 = mark_scan._enter_forward_caption_and_send

    mark_scan._open_media_and_get_forward_button = _fake_ready_135
    mark_scan._select_forward_destination = _fake_select_135
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_135
    mark_scan._open_source_chat = _dispatching_open_135
    sender._snapshot_msg_baselines = fake_snap_135
    sender._find_outgoing_with_text = fake_find_135
    try:
        # Take 1 — no existing caption, one clean attempt.
        mark_scan._enter_forward_caption_and_send = _fake_caption_send_take1_135
        take1_target_135 = _send_target("take1-135", "take", 1)
        take1_target_135["destination_group"] = "Dest Group"
        take1_target_135["caption"] = "Audition Take 1"
        result_take1_135 = asyncio.run(mark_scan._send_one_target_native_forward(_FakeComposeBoxPage(), "Talent Group", take1_target_135))

        # Introduction — existing caption, needs the OUTER bounded
        # recovery wrapper to succeed.
        mark_scan._enter_forward_caption_and_send = _fake_caption_send_intro_135
        intro_target_135 = _send_target("intro-135", "intro")
        intro_target_135["destination_group"] = "Dest Group"
        intro_target_135["caption"] = "Introduction Take"
        result_intro_135 = asyncio.run(mark_scan._send_one_target_native_forward(_FakeComposeBoxPage(), "Talent Group", intro_target_135))
    finally:
        mark_scan._open_media_and_get_forward_button = orig_ready_135
        mark_scan._select_forward_destination = orig_select_135
        mark_scan._ensure_forward_dialog_closed = orig_closed_135
        mark_scan._open_source_chat = orig_opensrc_135
        sender._snapshot_msg_baselines = orig_snap_135
        sender._find_outgoing_with_text = orig_find_text_135
        mark_scan._enter_forward_caption_and_send = orig_caption_135

    assert result_take1_135["ok"] is True, result_take1_135
    assert result_intro_135["ok"] is True, result_intro_135
    assert call_log_135.count("take1") == 1, call_log_135  # Take 1's own caption/send step is invoked EXACTLY once
    assert call_log_135 == ["take1", "intro-attempt-1", "intro-attempt-2"], call_log_135  # Introduction recovered on the bounded wrapper's 2nd attempt
    assert sim_135.messages == ["Audition Take 1", "Introduction Take"], sim_135.messages  # exactly one delivery per item, never duplicated
    print("135. KEY regression: Take 1 = SUCCESS, Introduction = temporary existing-caption failure, RECOVERY (bounded wrapper) = Introduction SUCCESS, FINAL RESULT = both SENT, Take 1 sent EXACTLY ONCE")

    # 136/137: group and individual-phone source, each with an existing
    # caption on the Introduction item, using the REAL caption-handling
    # code (not mocked) — proves the fix is source-type-agnostic, same
    # as every other piece of this SEND pipeline.
    for source_type_13x, source_name_13x, test_num_13x in (("group", "Talent Group", "136"), ("phone", "919990000444", "137")):
        page_13x = _FakeComposeBoxPage(clear_after=1)
        sim_13x = _SimDestination()
        _, fake_open_dest_13x, fake_snap_13x, fake_find_13x = _install_sim_destination_fakes({"Dest Group": sim_13x})

        async def _fake_ready_13x(page, group, msg_id, tile_index, is_photo):
            return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

        async def _fake_select_13x(page, dest):
            return {"ok": True}

        async def _fake_ensure_closed_13x(page):
            return True

        async def _dispatching_open_13x(page, source_type, group_name, _fake_open_dest=fake_open_dest_13x):
            if group_name == "Dest Group":
                return await _fake_open_dest(page, source_type, group_name)
            return "OPENED"

        async def _fake_find_send_13x(page, allow_enter_fallback=False, _sim=sim_13x):
            # Simulates clicking the real Send control actually delivering
            # whatever was typed into the compose box — the SAME "type
            # then click Send" sequence _enter_forward_caption_and_send
            # itself performs, unmocked, in this test.
            _sim.send(page.typed_caption)
            return "[aria-label^=\"Send\"]"

        orig_evaluate_13x = mark_scan._evaluate
        orig_find_send_13x = sender._find_and_click_send
        orig_ready_13x = mark_scan._open_media_and_get_forward_button
        orig_select_13x = mark_scan._select_forward_destination
        orig_closed_13x = mark_scan._ensure_forward_dialog_closed
        orig_opensrc_13x = mark_scan._open_source_chat
        orig_snap_13x = sender._snapshot_msg_baselines
        orig_find_text_13x = sender._find_outgoing_with_text

        mark_scan._evaluate = _fake_evaluate_existing_caption_factory()
        sender._find_and_click_send = _fake_find_send_13x
        mark_scan._open_media_and_get_forward_button = _fake_ready_13x
        mark_scan._select_forward_destination = _fake_select_13x
        mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_13x
        mark_scan._open_source_chat = _dispatching_open_13x
        sender._snapshot_msg_baselines = fake_snap_13x
        sender._find_outgoing_with_text = fake_find_13x
        try:
            intro_target_13x = _send_target("intro-13x", "intro")
            intro_target_13x["destination_group"] = "Dest Group"
            intro_target_13x["caption"] = "Introduction Take"
            result_13x = asyncio.run(mark_scan._send_one_target_native_forward(
                page_13x, source_name_13x, intro_target_13x, source_type=source_type_13x,
            ))
        finally:
            mark_scan._evaluate = orig_evaluate_13x
            sender._find_and_click_send = orig_find_send_13x
            mark_scan._open_media_and_get_forward_button = orig_ready_13x
            mark_scan._select_forward_destination = orig_select_13x
            mark_scan._ensure_forward_dialog_closed = orig_closed_13x
            mark_scan._open_source_chat = orig_opensrc_13x
            sender._snapshot_msg_baselines = orig_snap_13x
            sender._find_outgoing_with_text = orig_find_text_13x
        assert result_13x["ok"] is True, result_13x
        assert sim_13x.messages == ["Introduction Take"], sim_13x.messages
        label = "group source" if source_type_13x == "group" else "individual phone source"
        print(f"{test_num_13x}. Introduction with an existing caption succeeds identically for {label}")

    # 138: mixed source — Take (phone, no existing caption) succeeds
    # immediately; Introduction (group, WITH an existing caption)
    # recovers via the widened removal search — in ONE send, each
    # keeping its own source and its own caption-handling outcome.
    page_take_138 = _FakeComposeBoxPage(clear_after=0)
    page_intro_138 = _FakeComposeBoxPage(clear_after=1)
    sim_138 = _SimDestination()
    _, fake_open_dest_138, fake_snap_138, fake_find_138 = _install_sim_destination_fakes({"Dest Group": sim_138})

    async def _fake_ready_138(page, group, msg_id, tile_index, is_photo):
        return {"ok": True, "forward_button": {"rect": [10, 10, 20, 20]}}

    async def _fake_select_138(page, dest):
        return {"ok": True}

    async def _fake_ensure_closed_138(page):
        return True

    async def _dispatching_open_138(page, source_type, group_name):
        if group_name == "Dest Group":
            return await fake_open_dest_138(page, source_type, group_name)
        return "OPENED"

    async def _fake_find_send_138(page, allow_enter_fallback=False):
        sim_138.send(page.typed_caption)
        return "[aria-label^=\"Send\"]"

    orig_evaluate_138 = mark_scan._evaluate
    orig_find_send_138 = sender._find_and_click_send
    orig_ready_138 = mark_scan._open_media_and_get_forward_button
    orig_select_138 = mark_scan._select_forward_destination
    orig_closed_138 = mark_scan._ensure_forward_dialog_closed
    orig_opensrc_138 = mark_scan._open_source_chat
    orig_snap_138 = sender._snapshot_msg_baselines
    orig_find_text_138 = sender._find_outgoing_with_text

    mark_scan._evaluate = _fake_evaluate_existing_caption_factory()
    sender._find_and_click_send = _fake_find_send_138
    mark_scan._open_media_and_get_forward_button = _fake_ready_138
    mark_scan._select_forward_destination = _fake_select_138
    mark_scan._ensure_forward_dialog_closed = _fake_ensure_closed_138
    mark_scan._open_source_chat = _dispatching_open_138
    sender._snapshot_msg_baselines = fake_snap_138
    sender._find_outgoing_with_text = fake_find_138
    try:
        take_target_138 = _send_target("take-phone-138", "take", 1)
        take_target_138["destination_group"] = "Dest Group"
        take_target_138["caption"] = "Audition Take 1"
        result_take_138 = asyncio.run(mark_scan._send_one_target_native_forward(
            page_take_138, "919990000555", take_target_138, source_type="phone",
        ))
        intro_target_138 = _send_target("intro-group-138", "intro")
        intro_target_138["destination_group"] = "Dest Group"
        intro_target_138["caption"] = "Introduction Take"
        result_intro_138 = asyncio.run(mark_scan._send_one_target_native_forward(
            page_intro_138, "Talent Group", intro_target_138, source_type="group",
        ))
    finally:
        mark_scan._evaluate = orig_evaluate_138
        sender._find_and_click_send = orig_find_send_138
        mark_scan._open_media_and_get_forward_button = orig_ready_138
        mark_scan._select_forward_destination = orig_select_138
        mark_scan._ensure_forward_dialog_closed = orig_closed_138
        mark_scan._open_source_chat = orig_opensrc_138
        sender._snapshot_msg_baselines = orig_snap_138
        sender._find_outgoing_with_text = orig_find_text_138

    assert result_take_138["ok"] is True, result_take_138
    assert result_intro_138["ok"] is True, result_intro_138
    assert page_take_138.remove_clicks == 0, page_take_138.remove_clicks  # Take never needed the removal path at all
    assert page_intro_138.remove_clicks == 1, page_intro_138.remove_clicks
    assert sim_138.messages == ["Audition Take 1", "Introduction Take"], sim_138.messages
    print("138. Mixed source: phone-sourced Take (no existing caption) and group-sourced Introduction (existing caption, recovered) both succeed independently in the same send")

    # Requirements 1/4/5/7/8/12/13/14 of this audit's own test list are
    # already covered by existing tests elsewhere in this file: 1 (Take
    # follows the unaffected path) by test 131 and every pre-existing
    # SEND test; 4/5 (detached DOM / late Forward button) by tests
    # 104-107 and _open_media_and_get_forward_button's own bounded
    # readiness loop, both role-agnostic and untouched by this fix; 7/8
    # (no cross-item resend / no cross-talent drift) by tests 125/130
    # (now proven together with the existing-caption fix via test 135's
    # own combined scenario); 12/13 (idempotency / no stale-message
    # proof) by test 121/126/122/130; 14 (genuine failure -> ATTENTION
    # REQUIRED) by test 111/120/134 — never duplicated here.
    print("(Requirements 1/4/5/7/8/12/13/14 of this audit's test list: covered by existing tests 104-107/111/120-122/125/126/130/131/134, not duplicated)")


if __name__ == "__main__":
    main()
    print("\nALL MARK_SCAN REGRESSION TESTS PASSED")
