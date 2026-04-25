"""Unit contract for Google Drive backup helpers.

No network calls — these test the pure-function plumbing (filename
sanitisation, folder-path derivation, per-category naming rules) so the
on-Drive layout is locked against accidental regressions.
"""
import pytest

from drive_backup import (
    _build_filename,
    _folder_for_category,
    sanitize_filename,
)


# --------------------------------------------------------------------------
# sanitize_filename
# --------------------------------------------------------------------------
class TestSanitize:
    def test_preserves_talent_label_verbatim(self):
        assert sanitize_filename("Scene 1 - Emotional") == "Scene 1 - Emotional"

    def test_strips_filesystem_illegal_chars(self):
        assert sanitize_filename('my/take\\with:bad*chars?"<>|') == "mytakewithbadchars"

    def test_trims_whitespace(self):
        assert sanitize_filename("   Dialogue Closeup   ") == "Dialogue Closeup"

    def test_collapses_internal_spaces(self):
        assert sanitize_filename("Take  1    Final") == "Take 1 Final"

    def test_empty_becomes_fallback(self):
        assert sanitize_filename("", fallback="file") == "file"
        assert sanitize_filename("  ", fallback="file") == "file"

    def test_truncates_at_max_len(self):
        out = sanitize_filename("x" * 250)
        assert len(out) <= 100

    def test_all_illegal_becomes_fallback(self):
        assert sanitize_filename('/\\:*?"<>|', fallback="fb") == "fb"


# --------------------------------------------------------------------------
# category → subfolder
# --------------------------------------------------------------------------
def test_folder_for_category():
    assert _folder_for_category("intro_video") == "intro"
    assert _folder_for_category("take") == "takes"
    assert _folder_for_category("take_1") == "takes"
    assert _folder_for_category("take_2") == "takes"
    assert _folder_for_category("take_3") == "takes"
    assert _folder_for_category("image") == "images"
    assert _folder_for_category("mystery") is None


# --------------------------------------------------------------------------
# filename builder — the critical "preserve talent label" contract
# --------------------------------------------------------------------------
class TestBuildFilename:
    def test_intro_video_uses_intro_dot_mp4(self):
        sub = {"id": "s1", "media": []}
        media = {"category": "intro_video", "original_filename": "some-clip.mov"}
        assert _build_filename(media, sub) == "intro.mov"

    def test_intro_video_falls_back_to_mp4_ext(self):
        sub = {"id": "s1", "media": []}
        media = {"category": "intro_video", "original_filename": None}
        assert _build_filename(media, sub) == "intro.mp4"

    def test_take_uses_exact_label(self):
        sub = {"id": "s1", "media": []}
        media = {"category": "take", "label": "Scene 1 - Emotional", "original_filename": "x.mp4"}
        assert _build_filename(media, sub) == "Scene 1 - Emotional.mp4"

    def test_take_sanitises_but_does_not_genericise(self):
        sub = {"id": "s1", "media": []}
        media = {"category": "take", "label": 'Scene/1*"bad"', "original_filename": "x.mp4"}
        # Illegal chars stripped, talent label core preserved — NOT 'take_1'
        assert _build_filename(media, sub) == "Scene1bad.mp4"

    def test_take_missing_label_falls_back_to_numbered(self):
        sub = {"id": "s1", "media": []}
        media = {"category": "take_2", "original_filename": "x.mp4"}
        assert _build_filename(media, sub) == "Take 2.mp4"

    def test_image_order_based_naming(self):
        # 3 images already on submission — the 4th being uploaded now
        existing = [
            {"id": "m1", "category": "image"},
            {"id": "m2", "category": "image"},
            {"id": "m3", "category": "image"},
        ]
        new_media = {"id": "m4", "category": "image", "original_filename": "portrait.jpeg"}
        sub = {"id": "s1", "media": existing + [new_media]}
        assert _build_filename(new_media, sub) == "image_4.jpeg"

    def test_image_first_in_submission(self):
        new_media = {"id": "m1", "category": "image", "original_filename": "headshot.png"}
        sub = {"id": "s1", "media": [new_media]}
        assert _build_filename(new_media, sub) == "image_1.png"

    def test_image_fallback_extension(self):
        new_media = {"id": "m1", "category": "image", "original_filename": None}
        sub = {"id": "s1", "media": [new_media]}
        assert _build_filename(new_media, sub) == "image_1.jpg"
