import os
import sys
import pytest
import uuid
from datetime import datetime, timedelta, timezone
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

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath("backend"))

import core
# Mock database global
mock_db = MagicMock()
core.db = mock_db

from fastapi.testclient import TestClient
from server import app
from core import upload_and_track_asset

client = TestClient(app)

@pytest.mark.asyncio
async def test_upload_success_transitions_to_completed():
    """Verify that a successful upload sets upload_status to completed in db."""
    mock_db.talents.find_one = AsyncMock(return_value={"id": "t1", "name": "Test Talent"})
    mock_db.asset_metadata.update_one = AsyncMock()
    
    # Mock cloudinary upload methods
    with patch("core.cloudinary_upload") as mock_upload, \
         patch("core.cloudinary.uploader.add_tag") as mock_add_tag, \
         patch("core.log_storage_action") as mock_log:
        
        mock_upload.return_value = {
            "url": "http://res.cloudinary.com/demo/image/upload/v1/test.jpg",
            "original_url": "https://res.cloudinary.com/demo/image/upload/v1/test.jpg",
            "public_id": "talentgram/talents/t1_test-talent/profile_images/media1",
            "resource_type": "image",
            "format": "jpg",
            "bytes": 100,
            "width": 100,
            "height": 100,
            "duration": None,
            "asset_id": "asset1"
        }
        
        result = await upload_and_track_asset(
            data=b"testdata",
            resource_type="image",
            content_type="image/jpeg",
            asset_type="profile_image",
            talent_id="t1",
            submission_id="s1",
            project_id="p1"
        )
        
        assert result["public_id"] == "talentgram/talents/t1_test-talent/profile_images/media1"
        
        # Verify db updates: first update_one for pending, second update_one for completed
        assert mock_db.asset_metadata.update_one.call_count == 2
        
        # Verify first call set pending
        first_call_args = mock_db.asset_metadata.update_one.call_args_list[0][0]
        assert first_call_args[1]["$set"]["upload_status"] == "pending"
        
        # Verify second call set completed
        second_call_args = mock_db.asset_metadata.update_one.call_args_list[1][0]
        assert second_call_args[1]["$set"]["upload_status"] == "completed"

@pytest.mark.asyncio
async def test_upload_failure_transitions_to_failed():
    """Verify that a failed upload sets upload_status to failed and records error reason."""
    mock_db.talents.find_one = AsyncMock(return_value={"id": "t1", "name": "Test Talent"})
    mock_db.asset_metadata.update_one = AsyncMock()
    
    with patch("core.cloudinary_upload", side_effect=ValueError("Cloudinary authentication failed")):
        with pytest.raises(ValueError):
            await upload_and_track_asset(
                data=b"testdata",
                resource_type="image",
                content_type="image/jpeg",
                asset_type="profile_image",
                talent_id="t1",
                submission_id="s1",
                project_id="p1"
            )
            
        assert mock_db.asset_metadata.update_one.call_count == 2
        # Verify first call set pending
        assert mock_db.asset_metadata.update_one.call_args_list[0][0][1]["$set"]["upload_status"] == "pending"
        # Verify second call set failed on exception
        failed_set = mock_db.asset_metadata.update_one.call_args_list[1][0][1]["$set"]
        assert failed_set["upload_status"] == "failed"
        assert "Cloudinary authentication failed" in failed_set["error_reason"]

@pytest.mark.asyncio
async def test_finalize_validation_checks_and_auto_expiration():
    """Verify that finalize blocks active pending uploads, ignores orphan pending assets, and auto-expires old ones."""
    from core import make_token
    token = make_token({"role": "submitter", "email": "talent@test.com", "sid": "sub1"}, days=1)
    
    # Define mocks for sub, projects, etc.
    submission_doc = {
        "id": "sub1",
        "project_id": "p1",
        "project_slug": "test-slug",
        "status": "draft",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "height": "5'10",
            "location": "Delhi",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""}
        },
        "media": [
            {"public_id": "active_media_pid", "category": "image"}
        ]
    }
    
    submission_doc["access_token"] = token
    mock_db.submissions.find_one = AsyncMock(return_value=submission_doc)
    mock_db.submissions.update_one = AsyncMock()
    mock_db.projects.find_one = AsyncMock(return_value={"id": "p1", "require_reapproval_on_edit": False})
    
    # 1. Active pending upload exists -> finalize should 400
    mock_db.asset_metadata.update_many = AsyncMock()
    mock_db.asset_metadata.find_one = AsyncMock(return_value={"public_id": "active_media_pid", "upload_status": "pending"})
    
    response = client.post(
        "/api/public/submissions/sub1/finalize",
        headers={"Authorization": f"Bearer {token}"},
        json={}
    )
    assert response.status_code == 400
    assert "uploads are still in progress" in response.json()["detail"]
    
    # 2. Only orphan pending assets exist (not in submission.media) -> finalize should succeed
    mock_db.asset_metadata.find_one = AsyncMock(return_value=None)
    mock_db.talents.find_one = AsyncMock(return_value=None)
    mock_db.talents.insert_one = AsyncMock()
    mock_db.talents.update_one = AsyncMock()
    mock_db.submissions.find_one = AsyncMock(return_value=submission_doc)
    
    response = client.post(
        "/api/public/submissions/sub1/finalize",
        headers={"Authorization": f"Bearer {token}"},
        json={}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    
    # Verify auto-expiration update_many was called
    assert mock_db.asset_metadata.update_many.call_count == 2 # Called for both requests
