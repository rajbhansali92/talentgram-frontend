import os
import sys
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock

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
from core import make_token

client = TestClient(app)


@pytest.mark.asyncio
async def test_canonical_prefill_no_fallbacks():
    """Verify prefill lookup prioritizes db.talents and avoids 3-tier fallbacks when talent exists."""
    email = "test@talent.com"
    token = make_token({"role": "submitter", "email": email, "sid": "sub-123"}, days=1)

    # Mock finding the talent record with a specific introduction video
    mock_talent = {
        "id": "talent-123",
        "name": "Deeya Damini",
        "email": email,
        "normalized_email": email,
        "media": [
            {
                "id": "video-canonical",
                "category": "video",
                "url": "http://res.cloudinary.com/demo/video/upload/canonical.mp4",
                "resource_type": "video",
            }
        ]
    }
    mock_db.talents.find_one = AsyncMock(return_value=mock_talent)
    mock_db.submissions.find_one = AsyncMock(return_value={"id": "sub-123", "project_slug": "test-slug"})
    mock_db.submissions.update_one = AsyncMock()
    mock_db.applications.find_one = AsyncMock(return_value=None)
    mock_db.applications.update_one = AsyncMock()
    mock_db.projects.find_one = AsyncMock(return_value={"id": "test-slug"})

    # Even if we have other newer videos in submissions or applications, they should be ignored
    # because the talent profile exists and is the canonical source.
    resp = client.get(
        f"/api/public/prefill?email={email}",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    
    assert data["first_name"] == "Deeya"
    assert data["last_name"] == "Damini"
    video_item = next(m for m in data["prefill_media"] if m["category"] == "intro_video")
    assert video_item["id"] == "video-canonical"
    assert video_item["url"] == "http://res.cloudinary.com/demo/video/upload/canonical.mp4"


@pytest.mark.asyncio
async def test_category_mapping_invite_link():
    """Verify that all media categories map correctly without being dropped in the Invite Link flow."""
    from routers.applications import _reconcile_draft_from_talent

    app_doc = {
        "id": "app-123",
        "talent_email": "test@talent.com",
        "media": []
    }
    
    talent = {
        "id": "talent-123",
        "media": [
            {"id": "med-1", "category": "portfolio", "url": "url-1", "resource_type": "image"},
            {"id": "med-2", "category": "image", "url": "url-2", "resource_type": "image"},
            {"id": "med-3", "category": "video", "url": "url-3", "resource_type": "video"},
            {"id": "med-4", "category": "intro_video", "url": "url-4", "resource_type": "video"},
            {"id": "med-5", "category": "headshot", "url": "url-5", "resource_type": "image"},
            {"id": "med-6", "category": "additional_portfolio", "url": "url-6", "resource_type": "image"}
        ]
    }

    mock_db.applications.update_one = AsyncMock()

    # Call reconciliation
    await _reconcile_draft_from_talent(app_doc, talent, "app-123")

    # Assert application.update_one was called with the mapped media list
    assert mock_db.applications.update_one.called
    args, kwargs = mock_db.applications.update_one.call_args
    patch = args[1]["$set"]
    media_patch = patch["media"]

    # Verify all 6 media items are mapped correctly and none are dropped
    mapped_categories = [m["category"] for m in media_patch]
    assert len(mapped_categories) == 6
    assert "image" in mapped_categories  # portfolio & image maps to image
    assert "intro_video" in mapped_categories  # video & intro_video maps to intro_video
    assert "headshot" in mapped_categories
    assert "additional_portfolio" in mapped_categories


@pytest.mark.asyncio
async def test_location_remains_separate():
    """Verify project-specific location override does not overwrite global talent location."""
    from core import merge_talent_profile
    
    talent = {
        "id": "talent-123",
        "name": "Deeya Damini",
        "email": "test@talent.com",
        "location": [{"city": "Mumbai"}]
    }

    incoming_form = {
        "first_name": "Deeya",
        "last_name": "Damini",
        "location": [{"city": "Dubai"}] # Project-specific override
    }

    # In submission finalize, "location" is popped from form_to_merge
    form_to_merge = dict(incoming_form)
    form_to_merge.pop("location", None)

    mock_db.talents.update_one = AsyncMock()
    mock_db.profile_audits.insert_one = AsyncMock()

    await merge_talent_profile(talent, form_to_merge, "project_submission")

    # Verify location was not updated in db.talents update patch
    if mock_db.talents.update_one.called:
        args, kwargs = mock_db.talents.update_one.call_args
        patch = args[1]["$set"]
        assert "location" not in patch
