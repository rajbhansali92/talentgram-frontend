"""P5 (Cloudinary rearchitecture) — DELIVERY-time transformation elimination.

Where P4 stopped generating derivatives at UPLOAD time, P5 stops generating them
at DELIVERY time: the canonical Cloudinary asset is delivered directly to the
browser / download / ZIP, and a transformation is emitted only for a genuine
product need (roster/pipeline thumbnail, one canonical video poster) or a P4
browser-compatibility exception (HEVC / non-web container → one f_mp4).

These tests assert the ARCHITECTURAL RULE, not a specific implementation string,
and fail loudly if a delivery-time transcode / full-res AVIF / universal 720p /
download-time f_mp4 / multi-variant poster is reintroduced.
"""
import os
import re
import sys

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "talentgram")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")

sys.path.insert(0, os.path.abspath("backend"))

import core  # noqa: E402
from routers import links as links_router  # noqa: E402

CLOUD = "https://res.cloudinary.com/talentgram"

# Tokens that must never appear in a DELIVERY url unless explicitly justified.
TRANSCODE_TOKENS = ("vc_auto", "vc_h264", "vc_vp9", "sp_", "br_", "f_webm", "f_ogv")
DOWNSCALE_TOKENS = ("w_1280", "h_720", "w_1920", "h_1080", "w_1200", "w_1600")


# ---------------------------------------------------------------------------
# 1. Images — canonical delivery, no full-res AVIF
# ---------------------------------------------------------------------------
class TestImageDelivery:
    def test_media_url_only_two_presets_remain(self):
        # roster + thumb are the only genuinely-required small derivatives.
        pid = "talentgram/talents/t1/abc"
        roster = core.media_url(pid, preset="roster", resource_type="image")
        thumb = core.media_url(pid, preset="thumb", resource_type="image")
        assert "w_400" in roster and "c_fill" in roster
        assert "w_200" in thumb and "c_fill" in thumb

    def test_dead_presets_do_not_produce_large_transforms(self):
        pid = "talentgram/talents/t1/abc"
        # 'detail'/'full' were removed; an unknown preset must fall back to the
        # small roster transform, never an uncapped w_1200/w_1600.
        for dead in ("detail", "full", "poster", "anything"):
            url = core.media_url(pid, preset=dead, resource_type="image")
            assert not any(t in url for t in DOWNSCALE_TOKENS), f"{dead} -> {url}"

    def test_cloudinary_url_for_image_has_no_dpr_auto(self):
        url = core.cloudinary_url_for("talentgram/x/y", "image", width=400, crop="fill")
        assert "dpr_auto" not in url and "dpr_" not in url

    def test_thumbnail_transform_is_deterministic(self):
        pid = "talentgram/talents/t1/abc"
        a = core.media_url(pid, preset="roster", resource_type="image")
        b = core.media_url(pid, preset="roster", resource_type="image")
        assert a == b


# ---------------------------------------------------------------------------
# 2. Video posters — ONE canonical transform, reused
# ---------------------------------------------------------------------------
class TestPoster:
    def test_poster_is_single_canonical_string(self):
        u1 = core.video_poster_url("talentgram/submissions/s1/v1")
        u2 = core.video_poster_url(
            f"{CLOUD}/video/upload/w_1280,h_720,c_limit/v123/talentgram/submissions/s1/v1.mp4"
        )
        # Same public_id (bare id vs full transformed URL) -> identical poster.
        assert u1 == u2
        assert u1.endswith(".jpg")
        assert "c_fill" in u1 and "w_600" in u1 and "h_338" in u1
        assert "dpr_" not in u1

    def test_public_media_reuses_persisted_poster(self):
        m = {
            "id": "m1", "resource_type": "video", "public_id": "talentgram/s/v1",
            "url": f"{CLOUD}/video/upload/v1/talentgram/s/v1.mp4",
            "poster_url": f"{CLOUD}/video/upload/SOME_STORED/talentgram/s/v1.jpg",
        }
        out = core._public_media(m)
        assert out["poster_url"] == m["poster_url"]  # not recomputed


# ---------------------------------------------------------------------------
# 3. Video delivery — canonical original, never a universal downscale/transcode
# ---------------------------------------------------------------------------
class TestVideoDelivery:
    def test_stream_video_url_no_longer_transcodes(self):
        url = core.stream_video_url("talentgram/submissions/s1/v1")
        assert url is not None
        for t in DOWNSCALE_TOKENS + TRANSCODE_TOKENS + ("f_mp4",):
            assert t not in url, f"stream_video_url still emits {t}: {url}"

    def test_public_media_legacy_fallback_serves_canonical(self):
        # Legacy Cloudinary video, no stored url, web-safe -> canonical, no transform.
        m = {
            "id": "m1", "resource_type": "video",
            "public_id": "talentgram/submissions/s1/v1",
            "provider": "cloudinary", "url": None,
        }
        out = core._public_media(m)
        assert out["url"] and "res.cloudinary.com" in out["url"]
        for t in DOWNSCALE_TOKENS + TRANSCODE_TOKENS:
            assert t not in out["url"]

    def test_public_media_legacy_fallback_uses_compat_when_flagged(self):
        m = {
            "id": "m1", "resource_type": "video",
            "public_id": "talentgram/submissions/s1/v1",
            "provider": "cloudinary", "url": None,
            "needs_compat_delivery": True,
        }
        out = core._public_media(m)
        assert "f_mp4" in out["url"]  # the ONE sanctioned exception

    def test_stream_and_r2_urls_pass_through_public_media(self):
        for u in (
            "https://customer-abc.cloudflarestream.com/uid123/manifest/video.m3u8",
            "https://media.talentgramagency.com/r2/obj?sig=xyz",
        ):
            m = {"id": "m", "resource_type": "video", "url": u, "public_id": "pid"}
            assert core._public_media(m)["url"] == u


# ---------------------------------------------------------------------------
# 4. Downloads — canonical asset, no download-time transcode (RULE #6/#7)
# ---------------------------------------------------------------------------
class TestDownloads:
    def test_get_video_download_url_never_appends_f_mp4(self):
        src = f"{CLOUD}/video/upload/v1712/talentgram/submissions/s1/v1.mp4"
        out = links_router._get_video_download_url(src)
        assert "f_mp4" not in out
        assert out == src  # canonical asset, untouched

    def test_get_video_download_url_strips_stale_transform_segment(self):
        src = f"{CLOUD}/video/upload/w_1280,h_720,c_limit,vc_auto,f_mp4/v1/talentgram/s/v1.mp4"
        out = links_router._get_video_download_url(src)
        assert "f_mp4" not in out and "w_1280" not in out and "vc_auto" not in out
        assert out == f"{CLOUD}/video/upload/v1/talentgram/s/v1.mp4"

    def test_get_video_download_url_keeps_non_mp4_extension(self):
        src = f"{CLOUD}/video/upload/v1/talentgram/s/v1.webm"
        out = links_router._get_video_download_url(src)
        assert out.endswith(".webm")  # no forced .mp4

    def test_get_video_download_url_passes_through_stream_and_r2(self):
        for u in (
            "https://customer-abc.cloudflarestream.com/uid/manifest/video.m3u8",
            "https://x.r2.example/obj?sig=1",
        ):
            assert links_router._get_video_download_url(u) == u

    @pytest.mark.asyncio
    async def test_resolve_download_web_safe_video_is_canonical(self):
        raw = {
            "provider": "cloudinary",
            "url": f"{CLOUD}/video/upload/v1/talentgram/s/v1.mp4",
        }
        out = await links_router._resolve_video_download_url(None, raw, None)
        assert "f_mp4" not in out
        assert out == raw["url"]

    @pytest.mark.asyncio
    async def test_resolve_download_compat_flagged_serves_compat_url_verbatim(self):
        # P4 pointed media.url at the canonical f_mp4 delivery URL; download must
        # serve it verbatim (not strip the f_mp4, not add another transform).
        compat = f"{CLOUD}/video/upload/f_mp4/v1/talentgram/s/v1.mp4"
        raw = {
            "provider": "cloudinary",
            "url": compat,
            "original_url": f"{CLOUD}/video/upload/v1/talentgram/s/v1.mov",
            "needs_compat_delivery": True,
        }
        out = await links_router._resolve_video_download_url(None, raw, None)
        assert out == compat

    @pytest.mark.asyncio
    async def test_resolve_download_r2_passes_through(self):
        raw = {"provider": "r2", "url": "https://x.r2.example/o?sig=1"}
        out = await links_router._resolve_video_download_url(None, raw, None)
        assert out == raw["url"]


# ---------------------------------------------------------------------------
# 5. Cost-regression guards
# ---------------------------------------------------------------------------
class TestCostRegressionGuards:
    def test_no_helper_emits_full_res_avif(self):
        # None of the surviving delivery helpers should hard-code f_avif.
        for fn, args in (
            (core.media_url, ("pid",)),
            (core.video_poster_url, ("pid",)),
            (core.stream_video_url, ("pid",)),
            (core.cloudinary_url_for, ("pid",)),
        ):
            out = fn(*args) or ""
            assert "f_avif" not in out

    def test_compat_delivery_is_the_only_video_f_mp4_path(self):
        # compat_video_delivery_url is allowed to emit f_mp4; nothing else is.
        assert "f_mp4" in (core.compat_video_delivery_url("pid") or "")
        assert "f_mp4" not in (core.stream_video_url("pid") or "")
        assert "f_mp4" not in (links_router._get_video_download_url(
            f"{CLOUD}/video/upload/v1/talentgram/s/v.mp4") or "")

    def test_media_url_thumbs_are_small(self):
        for preset in ("roster", "thumb"):
            url = core.media_url("pid", preset=preset, resource_type="image")
            m = re.search(r"w_(\d+)", url)
            assert m and int(m.group(1)) <= 400, url
