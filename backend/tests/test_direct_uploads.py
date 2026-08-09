import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Set required environment variables before importing core/server
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "dummy"
os.environ["DIRECT_UPLOAD_ENABLED"] = "true"


# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath("backend"))

import core
# Mock database global
mock_db = MagicMock()
core.db = mock_db

from fastapi.testclient import TestClient
from server import app

client = TestClient(app)

def test_submission_sign_upload_feature_flag_disabled():
    """Verify that request returns 400 when feature flag is disabled."""
    with patch("core.DIRECT_UPLOAD_ENABLED", False):
        response = client.post(
            "/api/public/submissions/sid123/upload/sign",
            json={"category": "take", "filename": "video.mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"]

def test_submission_sign_upload_auth_fail():
    """Verify that request without valid submitter token returns 401 when flag is enabled."""
    response = client.post(
        "/api/public/submissions/sid123/upload/sign",
        json={"category": "take", "filename": "video.mp4"}
    )
    assert response.status_code == 401


@patch("routers.submissions.decode_submitter")
def test_submission_sign_upload_success(mock_decode):
    """Verify that a valid submission token generates a Cloudinary upload signature successfully."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_email": "test@test.com",
        "media": []
    })
    
    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"
        
        response = client.post(
            "/api/public/submissions/sid123/upload/sign",
            json={"category": "take", "filename": "video.mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["signature"] == "mocked_sig"
        assert data["cloud_name"] == "dummy"
        assert data["api_key"] == "dummy"
        assert data["folder"] == "talentgram/submissions/sid123"

@patch("routers.submissions.decode_submitter")
@patch("routers.submissions.sync_media_to_global_talent")
def test_submission_complete_upload(mock_sync, mock_decode):
    """Verify that completing upload successfully records media metadata in DB."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    mock_sync.return_value = AsyncMock()
    
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.submissions.update_one = AsyncMock()
    mock_db.asset_metadata.insert_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)
    
    payload = {
        "media_id": "mid123",
        "category": "take",
        "public_id": "pub123",
        "url": "https://res.cloudinary.com/dummy/video/upload/v1/video.mp4",
        "bytes": 5000000,
        "duration": 12.5,
        "content_type": "video/mp4",
        "original_filename": "video.mp4",
        "eager": [
            {"format": "jpg", "secure_url": "https://res.cloudinary.com/dummy/video/upload/e_poster.jpg"}
        ]
    }
    
    response = client.post(
        "/api/public/submissions/sid123/upload/complete",
        json=payload,
        headers={"Authorization": "Bearer dummy_token"}
    )
    
    assert response.status_code == 200
    assert mock_db.submissions.update_one.call_count == 1
    assert mock_db.asset_metadata.insert_one.call_count == 1


@patch("routers.submissions.decode_submitter")
def test_video_signature_label_encoding(mock_decode):
    """Verify that video-signature endpoint URL-encodes labels with spaces in context."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_id": "t123",
        "talent_name": "Test Talent",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.asset_metadata.update_one = AsyncMock()
    
    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"
        
        response = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "take", "label": "Take 1", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["params"]["context"] == "category=take|label=Take%201"


@patch("routers.submissions.decode_submitter")
def test_video_signature_public_id_is_leaf_only_intro(mock_decode):
    """Verify that video-signature returns leaf-only public_id for intro_video,
    NOT the full folder path (which would double when Cloudinary prepends folder)."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}

    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_id": "t123",
        "talent_name": "Test Talent",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.asset_metadata.update_one = AsyncMock()

    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"

        response = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "intro_video", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )

        assert response.status_code == 200
        data = response.json()
        public_id = data["params"]["public_id"]
        folder = data["params"]["folder"]

        # public_id must be leaf-only — NOT contain folder path
        assert public_id == "intro_video", f"Expected leaf 'intro_video', got '{public_id}'"
        assert "/" not in public_id, "public_id must not contain folder separators"

        # folder must still be the full hierarchy
        assert folder.startswith("talentgram/"), f"folder should start with talentgram/, got '{folder}'"

        # Combined path (what Cloudinary stores) must be under 255 chars
        combined = f"{folder}/{public_id}"
        assert len(combined) <= 255, f"Combined path is {len(combined)} chars (max 255): {combined}"


@patch("routers.submissions.decode_submitter")
def test_video_signature_public_id_is_leaf_only_take(mock_decode):
    """Verify that video-signature returns leaf-only public_id for take,
    with an 8-char UUID suffix (not full 32 chars)."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}

    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_id": "t123",
        "talent_name": "Test Talent",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.asset_metadata.update_one = AsyncMock()

    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"

        response = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "take", "label": "Take 1", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )

        assert response.status_code == 200
        data = response.json()
        public_id = data["params"]["public_id"]
        folder = data["params"]["folder"]

        # public_id must be leaf-only: take_{8 hex chars}
        assert public_id.startswith("take_"), f"Expected take_* leaf, got '{public_id}'"
        assert "/" not in public_id, "public_id must not contain folder separators"
        suffix = public_id[len("take_"):]
        assert len(suffix) == 8, f"Take UUID suffix should be 8 chars, got {len(suffix)}: '{suffix}'"

        # Combined path must be under 255 chars
        combined = f"{folder}/{public_id}"
        assert len(combined) <= 255, f"Combined path is {len(combined)} chars (max 255): {combined}"


def test_public_id_length_worst_case():
    """Regression: even with maximum-length UUIDs and long talent names,
    the combined Cloudinary path (folder/public_id) stays under 255 chars."""
    from core import audition_submission_folder

    # Worst-case: all UUIDs are full 36-char, long talent name
    long_name = "Padmavathi Krishnamurthy-Ramanathan"  # 35 chars
    folder = audition_submission_folder(
        talent_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        talent_name=long_name,
        project_id="ffffffff-gggg-hhhh-iiii-jjjjjjjjjjjj",
        submission_id="kkkkkkkk-llll-mmmm-nnnn-oooooooooooo",
    )

    # intro_video leaf
    combined_intro = f"{folder}/intro_video"
    assert len(combined_intro) <= 255, (
        f"intro_video combined path is {len(combined_intro)} chars (max 255): {combined_intro}"
    )

    # take leaf (8 hex chars)
    combined_take = f"{folder}/take_abcd1234"
    assert len(combined_take) <= 255, (
        f"take combined path is {len(combined_take)} chars (max 255): {combined_take}"
    )


@patch("routers.submissions.decode_submitter")
def test_video_signature_asynchronous_transformations(mock_decode):
    """Verify that video-signature uses eager_async and does not return synchronous transformation or format."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_id": "t123",
        "talent_name": "Test Talent",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.asset_metadata.update_one = AsyncMock()
    
    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"
        
        response = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "take", "label": "Take 1", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        
        assert response.status_code == 200
        data = response.json()
        params = data["params"]
        
        # Verify eager and eager_async parameters are present
        assert "eager" in params
        assert "eager_async" in params
        assert params["eager_async"] == "true"
        
        # Verify the 720p scaling and poster transforms are in the eager chain
        assert "c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4" in params["eager"]
        assert "c_fill,h_338,w_600,q_auto/f_jpg" in params["eager"]
        
        # Verify that synchronous transformation and format parameters are absent
        assert "transformation" not in params
        assert "format" not in params


@patch("routers.applications.decode_submitter")
def test_app_video_signature_success(mock_decode):
    """Verify that app-video-signature endpoint generates signature with eager async and matches schemas."""
    mock_decode.return_value = {"sid": "aid123", "role": "submitter", "kind": "application"}
    
    mock_db.applications.find_one = AsyncMock(return_value={
        "id": "aid123",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.applications.update_one = AsyncMock()
    mock_db.asset_metadata.update_one = AsyncMock()
    
    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"
        
        response = client.post(
            "/api/public/apply/aid123/video-signature",
            json={"category": "intro_video", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        
        assert response.status_code == 200
        data = response.json()
        params = data["params"]
        
        assert data["signature"] == "mocked_sig"
        assert params["public_id"] == "intro_video"
        assert params["folder"] == "talentgram/applications/aid123"
        assert params["eager_async"] == "true"
        assert "c_limit,h_720,w_1280/q_auto,vc_auto/f_mp4" in params["eager"]
        assert "transformation" not in params
        assert "format" not in params


@patch("routers.applications.decode_submitter")
def test_app_video_complete_success(mock_decode):
    """Verify that completing application video upload attaches it to the database."""
    mock_decode.return_value = {"sid": "aid123", "role": "submitter", "kind": "application"}
    
    mock_db.applications.find_one = AsyncMock(return_value={
        "id": "aid123",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.applications.update_one = AsyncMock()
    mock_db.asset_metadata.update_one = AsyncMock()
    
    payload = {
        "public_id": "talentgram/applications/aid123/intro_video",
        "url": "https://res.cloudinary.com/dummy/video/upload/v1/intro_video.mov",
        "bytes": 6000000,
        "duration": 15.0,
    }
    
    with patch("cloudinary.api.resource") as mock_resource:
        mock_resource.return_value = {
            "public_id": "talentgram/applications/aid123/intro_video",
            "bytes": 6000000,
            "duration": 15.0,
            "secure_url": "https://res.cloudinary.com/dummy/video/upload/v1/intro_video.mov",
            "url": "http://res.cloudinary.com/dummy/video/upload/v1/intro_video.mov"
        }
        with patch("core.sync_media_to_global_talent") as mock_sync:
            mock_sync.return_value = AsyncMock()
            
            response = client.post(
                "/api/public/apply/aid123/video-complete",
                json=payload,
                headers={"Authorization": "Bearer dummy_token"}
            )
            
            assert response.status_code == 200
            assert response.json()["ok"] is True
            # Single-slot replacement: the old intro_video is pulled and the
            # new one pushed only now that the upload is confirmed complete
            # (never at video-signature time — see app_video_signature) —
            # that's 2 calls, matching the R2 branch and
            # routers/submissions.py's attach_video_media.
            assert mock_db.applications.update_one.call_count == 2
            pull_call, push_call = mock_db.applications.update_one.call_args_list
            assert pull_call.args[1] == {"$pull": {"media": {"category": "intro_video"}}}
            assert "$push" in push_call.args[1]
            assert mock_db.asset_metadata.update_one.call_count == 1


@patch("routers.applications.decode_submitter")
def test_legacy_app_upload_sign_rejects_video(mock_decode):
    """Verify that the legacy sign and complete endpoints reject intro_video."""
    mock_decode.return_value = {"sid": "aid123", "role": "submitter", "kind": "application"}
    
    # 1. Sign endpoint rejects video
    response = client.post(
        "/api/public/apply/aid123/upload/sign",
        json={"category": "intro_video", "filename": "video.mov"},
        headers={"Authorization": "Bearer dummy_token"}
    )
    assert response.status_code == 400
    assert "chunked" in response.json()["detail"]

    # 2. Complete endpoint rejects video
    response = client.post(
        "/api/public/apply/aid123/upload/complete",
        json={
            "media_id": "mid123",
            "category": "intro_video",
            "public_id": "pub123",
            "url": "https://res.cloudinary.com/dummy/video/upload/v1/video.mp4",
            "bytes": 1000
        },
        headers={"Authorization": "Bearer dummy_token"}
    )
    assert response.status_code == 400
    assert "chunked" in response.json()["detail"]


@patch("routers.submissions.decode_submitter")
def test_submission_video_complete_ownership_failure_reproduction(mock_decode):
    """Verify that video-signature folder prefix matches video-complete folder prefix."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}

    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123",
        "project_id": "pid123",
        "talent_id": "t123",
        "talent_name": "Test Talent",
        "talent_email": "test@test.com",
        "media": []
    })
    mock_db.asset_metadata.update_one = AsyncMock()

    # 1. Get the signature / folder
    with patch("cloudinary.utils.api_sign_request") as mock_sign:
        mock_sign.return_value = "mocked_sig"
        response_sig = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "take", "label": "Take 1", "content_type": "video/mp4"},
            headers={"Authorization": "Bearer dummy_token"}
        )
        assert response_sig.status_code == 200
        data_sig = response_sig.json()
        folder = data_sig["params"]["folder"]
        public_id_leaf = data_sig["params"]["public_id"]
        full_public_id = f"{folder}/{public_id_leaf}"

    # 2. Call video-complete using the full public_id
    payload = {
        "public_id": full_public_id,
        "url": f"https://res.cloudinary.com/dummy/video/upload/v1/{full_public_id}.mp4",
        "bytes": 5000000,
        "duration": 10.0
    }

    with patch("cloudinary.api.resource") as mock_resource:
        mock_resource.return_value = {
            "public_id": full_public_id,
            "bytes": 5000000,
            "duration": 10.0,
            "secure_url": f"https://res.cloudinary.com/dummy/video/upload/v1/{full_public_id}.mp4",
            "url": f"http://res.cloudinary.com/dummy/video/upload/v1/{full_public_id}.mp4",
            "tags": ["category=take"]
        }
        mock_attach = AsyncMock(return_value={"id": "media123", "category": "take"})
        with patch("routers.submissions.attach_video_media", mock_attach):
            response_complete = client.post(
                "/api/public/submissions/sid123/video-complete",
                json=payload,
                headers={"Authorization": "Bearer dummy_token"}
            )
            # This asserts ownership matching
            assert response_complete.status_code == 200


# ── R2 upload compatibility layer fully retired (2026-08-09) ────────────────
# The temporary `resign_r2_key` branch (kept for in-flight sessions that
# started on R2 before the CORS-fragility fix deployed) has been removed:
# production evidence showed zero live sessions could still reach it — every
# pending R2 asset_metadata row's presigned PUT URL had already expired
# (default 60 min) with no new ones possible, and the two most-recent
# candidates' parent application/submission documents no longer exist in
# production (deleted), making the resign endpoint 404 before any R2 logic
# could run. video-signature now ALWAYS returns the Cloudinary chunked path,
# unconditionally — a resign with an R2-shaped public_id is rejected as
# invalid, same as any other malformed id. R2 itself is untouched for
# playback (presigned GET, Cloudflare Stream webhook, storage cleanup) —
# only the upload-initiation path was removed.

@patch("routers.submissions.decode_submitter")
def test_submission_video_signature_always_cloudinary_even_with_r2_flag_on(mock_decode):
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123", "project_id": "pid123", "talent_id": "tid1", "media": [],
    })
    mock_db.asset_metadata.update_one = AsyncMock(return_value=None)
    mock_db.talents.find_one = AsyncMock(return_value={"id": "tid1", "name": "Test"})

    with patch("core.ENABLE_R2_MEDIA_PIPELINE", True):
        res = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "intro_video", "label": None, "public_id": None},
            headers={"Authorization": "Bearer dummy_token"},
        ).json()

    assert "use_r2" not in res
    assert res["upload_url"].startswith("https://api.cloudinary.com/")
    assert res["params"]["public_id"] == "intro_video"


@patch("routers.applications._check_app_token")
def test_application_video_signature_always_cloudinary_even_with_r2_flag_on(mock_check):
    mock_check.return_value = None
    mock_db.applications.find_one = AsyncMock(return_value={"id": "aid123", "status": "draft"})
    mock_db.applications.update_one = AsyncMock(return_value=None)
    mock_db.asset_metadata.update_one = AsyncMock(return_value=None)

    with patch("core.ENABLE_R2_MEDIA_PIPELINE", True):
        res = client.post(
            "/api/public/apply/aid123/video-signature",
            json={"category": "intro_video", "label": None, "public_id": None},
            headers={"Authorization": "Bearer dummy_token"},
        ).json()

    assert "use_r2" not in res
    assert res["upload_url"].startswith("https://api.cloudinary.com/")
    assert res["params"]["public_id"] == "intro_video"


@patch("routers.submissions.decode_submitter")
def test_submission_resign_with_r2_shaped_public_id_is_rejected(mock_decode):
    """A resign carrying an R2-shaped key (from a session that started
    before the compatibility layer was removed) must now be rejected as an
    invalid public_id, not silently signed for Cloudinary."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123", "project_id": "pid123", "talent_id": "tid1", "media": [],
    })
    mock_db.talents.find_one = AsyncMock(return_value={"id": "tid1", "name": "Test"})

    with patch("core.ENABLE_R2_MEDIA_PIPELINE", True):
        res = client.post(
            "/api/public/submissions/sid123/video-signature",
            json={"category": "intro_video", "label": None,
                  "public_id": "raw-uploads/submissions/sid123/intro_video/intro_video_abcd1234.mp4"},
            headers={"Authorization": "Bearer dummy_token"},
        )

    assert res.status_code == 400


@patch("routers.applications._check_app_token")
def test_application_resign_with_r2_shaped_public_id_is_rejected(mock_check):
    mock_check.return_value = None
    mock_db.applications.find_one = AsyncMock(return_value={"id": "aid123", "status": "draft"})

    with patch("core.ENABLE_R2_MEDIA_PIPELINE", True):
        res = client.post(
            "/api/public/apply/aid123/video-signature",
            json={"category": "intro_video", "label": None,
                  "public_id": "raw-uploads/applications/aid123/intro_video/intro_video_abcd1234.mp4"},
            headers={"Authorization": "Bearer dummy_token"},
        )

    assert res.status_code == 400


# ── video-complete R2 branch: reject fabricated public_ids (API hardening) ──
# video-signature can never mint a fresh R2 key anymore, but video-complete's
# R2 branch had no check that the caller-supplied public_id was ever actually
# issued — any valid submitter/applicant token (self-issued for free on the
# apply flow) could POST a made-up raw-uploads/... id and still trigger a
# real Cloudflare Stream transcode enqueue for an object that was never
# uploaded. Hardened to require a matching pending/processing asset_metadata
# row — the exact row a genuine video-signature call would have upserted.

@patch("routers.submissions.decode_submitter")
def test_submission_video_complete_r2_rejects_unknown_public_id(mock_decode):
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123", "project_id": "pid123", "talent_id": "tid1", "media": [],
    })
    mock_db.talents.find_one = AsyncMock(return_value={"id": "tid1", "name": "Test"})
    mock_db.asset_metadata.find_one = AsyncMock(return_value=None)  # never issued

    res = client.post(
        "/api/public/submissions/sid123/video-complete",
        json={
            "public_id": "raw-uploads/submissions/sid123/take/take_deadbeef.mp4",
            "bytes": 12345,
        },
        headers={"Authorization": "Bearer dummy_token"},
    )
    assert res.status_code == 400


@patch("routers.applications._check_app_token")
def test_application_video_complete_r2_rejects_unknown_public_id(mock_check):
    mock_check.return_value = None
    mock_db.applications.find_one = AsyncMock(return_value={"id": "aid123", "status": "draft"})
    mock_db.asset_metadata.find_one = AsyncMock(return_value=None)  # never issued

    res = client.post(
        "/api/public/apply/aid123/video-complete",
        json={
            "public_id": "raw-uploads/applications/aid123/intro_video/intro_video_deadbeef.mp4",
            "bytes": 12345,
        },
        headers={"Authorization": "Bearer dummy_token"},
    )
    assert res.status_code == 400


@patch("routers.submissions.decode_submitter")
def test_submission_video_complete_r2_accepts_genuinely_issued_session(mock_decode):
    """A pre-existing legitimate session (asset_metadata row a real
    video-signature call upserted before the R2 write-path was removed)
    must still be able to complete."""
    mock_decode.return_value = {"sid": "sid123", "role": "submitter"}
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "sid123", "project_id": "pid123", "talent_id": "tid1", "media": [],
    })
    mock_db.talents.find_one = AsyncMock(return_value={"id": "tid1", "name": "Test"})
    mock_db.asset_metadata.find_one = AsyncMock(return_value={
        "public_id": "raw-uploads/submissions/sid123/take/take_abcd1234.mp4",
        "upload_status": "pending",
    })
    mock_db.asset_metadata.update_one = AsyncMock()

    with patch("core.trigger_cloudinary_transcode", new=AsyncMock()), \
         patch("core.generate_r2_presigned_url", return_value="https://r2.example/get?sig=x"):
        res = client.post(
            "/api/public/submissions/sid123/video-complete",
            json={
                "public_id": "raw-uploads/submissions/sid123/take/take_abcd1234.mp4",
                "bytes": 12345,
            },
            headers={"Authorization": "Bearer dummy_token"},
        )
    assert res.status_code == 200
    assert res.json()["ok"] is True
