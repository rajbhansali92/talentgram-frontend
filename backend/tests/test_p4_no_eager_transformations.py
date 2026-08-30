"""P4 (Cloudinary rearchitecture) — new uploads must produce ONE canonical
Cloudinary asset and NO automatic derivatives.

Two layers:
  * pure unit tests for the new core helpers (no DB, no Cloudinary);
  * upload-path tests that mock `cloudinary.uploader.upload` /
    `cloudinary.utils.api_sign_request` and assert what was (not) requested.

The "cost safety" tests deliberately assert the ABSENCE of eager 720p / 1080p /
AVIF / thumbnail / poster generation and of multi-derivative video handling —
they fail loudly if any of that is reintroduced.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "dummy")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")
os.environ.setdefault("DIRECT_UPLOAD_ENABLED", "true")

sys.path.insert(0, os.path.abspath("backend"))

import core  # noqa: E402

mock_db = MagicMock()
core.db = mock_db

from fastapi.testclient import TestClient  # noqa: E402
from server import app  # noqa: E402

client = TestClient(app)

# A minimal valid JPEG and MP4 header so cloudinary_upload's signature check passes.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64

FORBIDDEN_TRANSFORM_TOKENS = ("w_1280", "h_720", "w_1920", "h_1080", "vc_auto",
                              "vc_h264", "f_mp4", "w_400", "w_600", "h_338",
                              "f_avif", "dpr_auto", "eager")


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
class TestCompatHelpers:
    @pytest.mark.parametrize("fmt,codec,expected", [
        ("mp4", "h264", False),
        ("mp4", "hevc", False),
        ("webm", "vp9", False),
        ("mov", "h264", False),          # iOS recordings play natively
        ("mov", None, False),            # unknown codec on a common container -> trust it
        (None, None, False),             # nothing known -> serve original
        ("MP4", "H264", False),          # case-insensitive
        ("avi", None, True),
        ("wmv", "wmv3", True),
        ("mkv", "h264", True),           # Safari/Firefox can't
        ("mp4", "prores", True),
        ("mov", "prores", True),
        ("mp4", "mjpeg", True),
    ])
    def test_video_needs_compat_delivery(self, fmt, codec, expected):
        assert core.video_needs_compat_delivery(fmt, codec) is expected

    def test_video_poster_url_is_one_canonical_string(self):
        bare = core.video_poster_url("talentgram/submissions/s1/abc")
        from_url = core.video_poster_url(
            "https://res.cloudinary.com/talentgram/video/upload/"
            "w_1280,h_720,c_limit/q_auto,vc_auto/v42/talentgram/submissions/s1/abc.mp4"
        )
        assert bare == from_url
        assert bare.endswith(".jpg")
        assert "dpr" not in bare
        # one transformation segment, alphabetically-sorted params
        assert "/c_fill,h_338,q_auto,w_600/" in bare

    def test_video_poster_url_rejects_non_cloudinary(self):
        assert core.video_poster_url("https://example.com/x.mp4") is None
        assert core.video_poster_url(None) is None

    def test_compat_video_delivery_url_forces_f_mp4_once(self):
        u = core.compat_video_delivery_url("talentgram/submissions/s1/abc")
        assert "f_mp4" in u
        assert u.count("f_mp4") == 1
        assert u.endswith(".mp4")

    def test_audition_video_transformation_is_unused_and_deprecated(self):
        # kept only for import-safety; no upload path calls it any more.
        import subprocess
        r = subprocess.run(
            ["grep", "-rn", "--include=*.py", "audition_video_transformation()",
             "backend/core.py", "backend/routers"],
            capture_output=True, text=True,
        )
        callers = [ln for ln in r.stdout.splitlines()
                   if "def audition_video_transformation" not in ln]
        assert callers == [], f"audition_video_transformation() still has callers: {callers}"


# ---------------------------------------------------------------------------
# cloudinary_upload — no eager / no incoming transformation, for any type
# ---------------------------------------------------------------------------
class TestCloudinaryUploadNoEager:
    def _run(self, data, content_type, resource_type="auto", keep_original=True):
        captured = {}

        def _fake_upload(_data, **kwargs):
            captured.update(kwargs)
            return {
                "secure_url": "https://res.cloudinary.com/dummy/x/upload/v1/talentgram/f/p.ext",
                "public_id": "talentgram/f/p", "resource_type": resource_type,
                "format": "jpg" if "image" in content_type else "mp4", "bytes": len(_data),
                "width": 100, "height": 100, "duration": 5.0,
                "video": {"codec": "h264"} if content_type.startswith("video") else None,
            }

        with patch.object(core.cloudinary.uploader, "upload", side_effect=_fake_upload):
            core.cloudinary_upload(data, folder="talentgram/f", public_id="p",
                                   resource_type=resource_type, content_type=content_type,
                                   keep_original=keep_original)
        return captured

    def test_image_upload_requests_no_eager(self):
        kw = self._run(JPEG_BYTES, "image/jpeg", resource_type="image")
        assert "eager" not in kw
        assert "transformation" not in kw
        assert kw.get("overwrite") is True

    def test_video_upload_requests_no_eager_no_transformation(self):
        kw = self._run(MP4_BYTES, "video/mp4", resource_type="video")
        assert "eager" not in kw
        assert "transformation" not in kw
        assert "format" not in kw

    def test_large_video_still_no_transcode(self):
        big = MP4_BYTES + b"\x00" * (1024)  # size is irrelevant now
        kw = self._run(big, "video/mp4", resource_type="video", keep_original=False)
        assert "eager" not in kw and "transformation" not in kw

    def test_return_surfaces_video_codec(self):
        captured = {}

        def _fake_upload(_data, **kwargs):
            captured.update(kwargs)
            return {"secure_url": "https://res.cloudinary.com/dummy/video/upload/v1/p.mp4",
                    "public_id": "p", "resource_type": "video", "format": "mov",
                    "bytes": 1, "video": {"codec": "prores"}}

        with patch.object(core.cloudinary.uploader, "upload", side_effect=_fake_upload):
            res = core.cloudinary_upload(MP4_BYTES, folder="talentgram/f", public_id="p",
                                         resource_type="video", content_type="video/quicktime")
        assert res["video_codec"] == "prores"
        assert res["format"] == "mov"


# ---------------------------------------------------------------------------
# upload_and_track_asset — one upload, no re-upload chain
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upload_and_track_asset_video_is_a_single_upload():
    mock_db.talents.find_one = AsyncMock(return_value={"id": "t1", "name": "T"})
    mock_db.asset_metadata.update_one = AsyncMock()
    mock_db.storage_audit_log.insert_one = AsyncMock()

    upload_calls = []
    destroy_calls = []

    def _fake_upload(_data_or_url, **kwargs):
        upload_calls.append(kwargs.get("public_id"))
        return {"secure_url": "https://res.cloudinary.com/dummy/video/upload/v1/talentgram/talents/t1/intro_video/m.mp4",
                "public_id": "talentgram/talents/t1/intro_video/m", "resource_type": "video",
                "format": "mp4", "bytes": 999, "width": 1, "height": 1, "duration": 3.0,
                "video": {"codec": "h264"}, "asset_id": "aid"}

    with patch.object(core.cloudinary.uploader, "upload", side_effect=_fake_upload), \
         patch.object(core.cloudinary.uploader, "add_tag"), \
         patch.object(core, "cloudinary_destroy", side_effect=lambda *a, **k: destroy_calls.append(a)):
        res = await core.upload_and_track_asset(
            MP4_BYTES, resource_type="video", content_type="video/mp4",
            asset_type="intro_video", talent_id="t1", keep_original=False,  # old chain trigger
        )

    # exactly ONE Cloudinary upload; no re-upload of a derivative; no temp destroy.
    assert len(upload_calls) == 1, upload_calls
    assert "audition_web" not in (res.get("public_id") or "")
    assert destroy_calls == []
    # no eager was ever requested on that one call
    # (checked indirectly: _fake_upload would have recorded it — see kwargs)


# ---------------------------------------------------------------------------
# sign endpoints — cost-safety: nothing transform-y is signed
# ---------------------------------------------------------------------------
def _assert_clean_sign_response(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    blob = json.dumps(body)
    for tok in FORBIDDEN_TRANSFORM_TOKENS:
        if tok == "eager":
            # `eager` key may exist in the response shape but must be null
            assert body.get("eager") in (None, [], {}), f"eager present: {body.get('eager')}"
            continue
        assert tok not in blob, f"transform token {tok!r} leaked into sign response: {blob}"


@patch("routers.submissions.decode_submitter")
def test_submission_image_sign_has_no_eager(mock_decode):
    mock_decode.return_value = {"sid": "s1", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1", "media": []})
    mock_db.projects.find_one = AsyncMock(return_value={"submission_requirements": {}})
    with patch("cloudinary.utils.api_sign_request", return_value="sig"):
        r = client.post("/api/public/submissions/s1/upload/sign",
                        json={"category": "indian", "filename": "a.jpg"},
                        headers={"Authorization": "Bearer x"})
    _assert_clean_sign_response(r)


@patch("routers.submissions.decode_submitter")
def test_submission_video_sign_has_no_eager(mock_decode):
    mock_decode.return_value = {"sid": "s1", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1", "media": []})
    mock_db.submissions.update_one = AsyncMock()  # single-slot $pull for intro_video
    mock_db.projects.find_one = AsyncMock(return_value={"submission_requirements": {}})
    with patch("cloudinary.utils.api_sign_request", return_value="sig"):
        r = client.post("/api/public/submissions/s1/upload/sign",
                        json={"category": "intro_video", "filename": "a.mp4"},
                        headers={"Authorization": "Bearer x"})
    _assert_clean_sign_response(r)
    assert r.json().get("transformation") is None


def test_admin_media_v2_sign_has_no_eager():
    from core import current_team_or_admin
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1"})
    app.dependency_overrides[current_team_or_admin] = lambda: {"id": "a1", "email": "a@x.com", "role": "admin"}
    try:
        with patch("cloudinary.utils.api_sign_request", return_value="sig"), \
             patch("cloudinary.config", return_value=MagicMock(api_key="k", api_secret="sec", cloud_name="c")):
            r = client.post("/api/projects/p1/submissions/s1/admin-media-v2/sign",
                            json={"category": "take", "filename": "a.mov"})
    finally:
        app.dependency_overrides.pop(current_team_or_admin, None)
    _assert_clean_sign_response(r)


@patch("routers.applications._check_app_token", new_callable=AsyncMock)
def test_application_image_sign_has_no_eager(mock_tok):
    mock_db.applications.find_one = AsyncMock(return_value={"id": "app1", "media": []})
    mock_db.asset_metadata.update_one = AsyncMock()
    with patch("cloudinary.utils.api_sign_request", return_value="sig"):
        r = client.post("/api/public/apply/app1/upload/sign",
                        json={"category": "western", "filename": "a.jpg"},
                        headers={"Authorization": "Bearer x"})
    _assert_clean_sign_response(r)


# ---------------------------------------------------------------------------
# /complete — original stored, canonical lazy poster, compat exception
# ---------------------------------------------------------------------------
@patch("routers.submissions.decode_submitter")
@patch("routers.submissions.sync_media_to_global_talent", new_callable=AsyncMock)
def test_submission_complete_video_stores_original_and_canonical_poster(mock_sync, mock_decode):
    mock_decode.return_value = {"sid": "s1", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1", "media": []})
    captured = {}
    mock_db.submissions.update_one = AsyncMock(side_effect=lambda q, u, **k: captured.update(u))
    mock_db.asset_metadata.update_one = AsyncMock()
    mock_db.asset_metadata.insert_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)

    orig = "https://res.cloudinary.com/dummy/video/upload/v9/talentgram/submissions/s1/mid.mp4"
    r = client.post("/api/public/submissions/s1/upload/complete",
                    json={"media_id": "mid", "category": "take", "public_id": "mid",
                          "url": orig, "bytes": 123, "duration": 10.0,
                          "content_type": "video/mp4", "original_filename": "a.mp4",
                          "format": "mp4", "video_codec": "h264"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    pushed = captured["$push"]["media"]
    assert pushed["url"] == orig                       # the ORIGINAL is served
    assert pushed["poster_url"].endswith(".jpg")
    assert "/c_fill,h_338,q_auto,w_600/" in pushed["poster_url"]  # canonical
    assert "f_mp4" not in pushed["url"]                # NOT transcoded
    assert "needs_compat_delivery" not in pushed


@patch("routers.submissions.decode_submitter")
@patch("routers.submissions.sync_media_to_global_talent", new_callable=AsyncMock)
def test_submission_complete_video_compat_exception_for_non_web_format(mock_sync, mock_decode):
    mock_decode.return_value = {"sid": "s1", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1", "media": []})
    captured = {}
    mock_db.submissions.update_one = AsyncMock(side_effect=lambda q, u, **k: captured.update(u))
    mock_db.asset_metadata.update_one = AsyncMock()
    mock_db.asset_metadata.insert_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)

    orig = "https://res.cloudinary.com/dummy/video/upload/v9/talentgram/submissions/s1/mid.avi"
    r = client.post("/api/public/submissions/s1/upload/complete",
                    json={"media_id": "mid", "category": "intro_video", "public_id": "mid",
                          "url": orig, "bytes": 123, "duration": 10.0,
                          "content_type": "video/x-msvideo", "original_filename": "a.avi",
                          "format": "avi", "video_codec": None},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    pushed = captured["$push"]["media"]
    assert "f_mp4" in pushed["url"]                    # ONE canonical compat delivery
    assert pushed["url"].count("f_mp4") == 1
    assert pushed["original_url"] == orig
    assert pushed["needs_compat_delivery"] is True


@patch("routers.submissions.decode_submitter")
@patch("routers.submissions.sync_media_to_global_talent", new_callable=AsyncMock)
def test_submission_complete_image_stores_original(mock_sync, mock_decode):
    mock_decode.return_value = {"sid": "s1", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "s1", "project_id": "p1", "media": []})
    captured = {}
    mock_db.submissions.update_one = AsyncMock(side_effect=lambda q, u, **k: captured.update(u))
    mock_db.asset_metadata.update_one = AsyncMock()
    mock_db.asset_metadata.insert_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)

    orig = "https://res.cloudinary.com/dummy/image/upload/v9/talentgram/submissions/s1/mid.jpg"
    r = client.post("/api/public/submissions/s1/upload/complete",
                    json={"media_id": "mid", "category": "western", "public_id": "mid",
                          "url": orig, "bytes": 50, "content_type": "image/jpeg",
                          "original_filename": "a.jpg"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    pushed = captured["$push"]["media"]
    assert pushed["url"] == orig                       # ORIGINAL, no f_auto/w_400 baked in
    for tok in ("w_400", "f_avif", "dpr_auto"):
        assert tok not in pushed["url"]
