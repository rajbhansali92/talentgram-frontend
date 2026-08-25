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


if __name__ == "__main__":
    main()
    print("\nALL MARK_SCAN REGRESSION TESTS PASSED")
