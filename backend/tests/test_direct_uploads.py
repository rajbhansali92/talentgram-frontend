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
