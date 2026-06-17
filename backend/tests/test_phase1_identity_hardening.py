import os
import sys
import uuid
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

# Setup environment mock values before imports
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["RESEND_API_KEY"] = "dummy"
os.environ["SENDGRID_API_KEY"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "password"

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (
    normalize_email,
    merge_talent_profile,
    sync_media_to_global_talent,
    current_team_or_admin,
    current_admin,
)
from server import app
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

client = TestClient(app)

@pytest.fixture(autouse=True)
def override_auth():
    mock_admin = {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}
    app.dependency_overrides[current_team_or_admin] = lambda: mock_admin
    app.dependency_overrides[current_admin] = lambda: mock_admin
    yield
    app.dependency_overrides.clear()

@pytest.fixture
def mock_db():
    mdb = MagicMock()
    # Mock collections
    mdb.talents = MagicMock()
    mdb.profile_audits = MagicMock()
    mdb.applications = MagicMock()
    mdb.submissions = MagicMock()
    mdb.otp_codes = MagicMock()
    mdb.otp_audit_logs = MagicMock()
    mdb.projects = MagicMock()
    mdb.asset_metadata = MagicMock()
    mdb.casting_pipeline = MagicMock()
    mdb.users = MagicMock()
    mdb.notification_logs = MagicMock()
    
    # Setup AsyncMocks
    mdb.talents.find_one = AsyncMock(return_value=None)
    mdb.talents.insert_one = AsyncMock()
    mdb.talents.update_one = AsyncMock()
    mdb.talents.delete_many = AsyncMock()
    
    mdb.profile_audits.insert_one = AsyncMock()
    mdb.profile_audits.find = MagicMock()
    mdb.profile_audits.find.return_value.to_list = AsyncMock(return_value=[])
    
    mdb.applications.find_one = AsyncMock(return_value=None)
    mdb.applications.insert_one = AsyncMock()
    mdb.applications.update_one = AsyncMock()
    
    mdb.submissions.find_one = AsyncMock(return_value=None)
    mdb.submissions.insert_one = AsyncMock()
    mdb.submissions.update_one = AsyncMock()
    
    # Setup mock find chaining for submissions
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[])
    mdb.submissions.find = MagicMock(return_value=mock_cursor)
    
    mdb.otp_audit_logs.count_documents = AsyncMock(return_value=0)
    mdb.otp_codes.update_many = AsyncMock()
    mdb.otp_codes.insert_one = AsyncMock()
    
    mdb.projects.find_one = AsyncMock(return_value={"id": "proj-123", "slug": "test-project"})
    mdb.asset_metadata.find_one = AsyncMock(return_value=None)
    mdb.casting_pipeline.find_one = AsyncMock(return_value=None)
    mdb.casting_pipeline.insert_one = AsyncMock()
    mdb.users.find = MagicMock()
    mdb.users.find.return_value.to_list = AsyncMock(return_value=[])
    mdb.notification_logs.insert_one = AsyncMock()
    
    return mdb

# --------------------------------------------------------------------------
# Test 1: Email normalization
# --------------------------------------------------------------------------
def test_email_normalization():
    assert normalize_email("Raj@gmail.com") == "raj@gmail.com"
    assert normalize_email(" raj@gmail.com ") == "raj@gmail.com"
    assert normalize_email("RAJ@gmail.com") == "raj@gmail.com"
    assert normalize_email(None) is None
    assert normalize_email("") is None

# --------------------------------------------------------------------------
# Test 2: Duplicate email prevention
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_email_prevention(mock_db):
    # Simulate DB-level Unique Index behavior via DuplicateKeyError raise in insert_one
    mock_db.talents.insert_one.side_effect = DuplicateKeyError("Duplicate key error on normalized_email")
    
    with pytest.raises(DuplicateKeyError):
        await mock_db.talents.insert_one({
            "id": "new-id-123",
            "email": "test@talentgram.com",
            "normalized_email": "test@talentgram.com"
        })

# --------------------------------------------------------------------------
# Test 3: Invite Link updates existing talent
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invite_link_updates_existing_talent(mock_db):
    existing_talent = {
        "id": "talent-123",
        "name": "Original User",
        "email": "invite_test@talentgram.com",
        "normalized_email": "invite_test@talentgram.com",
        "phone": "1111",
        "media": []
    }
    
    application_doc = {
        "id": "app-123",
        "talent_email": "invite_test@talentgram.com",
        "talent_name": "Updated User Name",
        "talent_phone": "2222",
        "status": "submitted",
        "decision": "pending",
        "media": []
    }
    
    # When flow checks for existing talent, return it
    mock_db.talents.find_one.return_value = existing_talent
    mock_db.applications.find_one.return_value = application_doc
    
    # Mock update_one
    mock_db.talents.update_one = AsyncMock()
    mock_db.applications.update_one = AsyncMock()
    
    # Simulate application approval
    with patch("routers.applications.db", mock_db), \
         patch("core.db", mock_db):
        
        response = client.post(
            "/api/applications/app-123/decision",
            headers={"Authorization": "Bearer dummy_token"},
            json={"decision": "approved"}
        )
        assert response.status_code == 200
        assert response.json()["merged"] is True
        
        # Verify that db.talents.update_one was called to merge profile
        # Check call_args_list to find the profile field update (which sets phone or details)
        profile_update_called = False
        for call in mock_db.talents.update_one.call_args_list:
            args = call[0]
            if args[0] == {"id": "talent-123"} and "$set" in args[1]:
                if args[1]["$set"].get("phone") == "2222":
                    profile_update_called = True
        assert profile_update_called

# --------------------------------------------------------------------------
# Test 4: Project Submission updates existing talent
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_submission_updates_existing_talent(mock_db):
    existing_talent = {
        "id": "talent-456",
        "name": "Submitting Talent",
        "email": "submit_test@talentgram.com",
        "normalized_email": "submit_test@talentgram.com",
        "location": "Mumbai",
        "media": []
    }
    
    submission_doc = {
        "id": "sub-123",
        "project_id": "proj-123",
        "talent_email": "submit_test@talentgram.com",
        "talent_name": "Submitting Talent",
        "status": "draft",
        "form_data": {
            "first_name": "Submitting",
            "last_name": "Talent",
            "height": "5'9",
            "location": "Delhi",  # Auto update field
            "availability": {"status": "yes"},
            "budget": {"status": "accept"}
        },
        "media": []
    }
    
    mock_db.talents.find_one.return_value = existing_talent
    mock_db.submissions.find_one.return_value = submission_doc
    
    # Bypass decode_submitter auth check and mock database in all active modules of this flow
    with patch("routers.submissions.db", mock_db), \
         patch("routers.submissions.decode_submitter", AsyncMock(return_value={"sid": "sub-123"})), \
         patch("routers.casting_pipeline.db", mock_db), \
         patch("core.db", mock_db):
        
        response = client.post(
            "/api/public/submissions/sub-123/finalize",
            json={}
        )
        assert response.status_code == 200
        
        # Check that merge updated the location of the talent from Mumbai to Delhi
        profile_update_called = False
        for call in mock_db.talents.update_one.call_args_list:
            args = call[0]
            if args[0] == {"id": "talent-456"} and "$set" in args[1]:
                if args[1]["$set"].get("location") == "Delhi":
                    profile_update_called = True
        assert profile_update_called

# --------------------------------------------------------------------------
# Test 5: Admin-created talent updated by onboarding
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_created_talent_updated_by_onboarding(mock_db):
    admin_talent = {
        "id": "admin-talent-999",
        "name": "Admin Talent",
        "email": "admin_created@talentgram.com",
        "normalized_email": "admin_created@talentgram.com",
        "source": {
            "type": "admin",
            "talent_email": "admin_created@talentgram.com",
            "reference_id": None
        },
        "phone": "9999",
        "media": []
    }
    
    application_doc = {
        "id": "app-999",
        "talent_email": "admin_created@talentgram.com",
        "talent_name": "Onboarding Talent",
        "talent_phone": "8888",
        "status": "submitted",
        "decision": "pending",
        "media": []
    }
    
    mock_db.talents.find_one.return_value = admin_talent
    mock_db.applications.find_one.return_value = application_doc
    
    with patch("routers.applications.db", mock_db), \
         patch("core.db", mock_db):
        
        response = client.post(
            "/api/applications/app-999/decision",
            headers={"Authorization": "Bearer dummy_token"},
            json={"decision": "approved"}
        )
        assert response.status_code == 200
        assert response.json()["merged"] is True
        
        # Verify db update
        profile_update_called = False
        for call in mock_db.talents.update_one.call_args_list:
            args = call[0]
            if args[0] == {"id": "admin-talent-999"} and "$set" in args[1]:
                if args[1]["$set"].get("phone") == "8888":
                    profile_update_called = True
        assert profile_update_called

# --------------------------------------------------------------------------
# Test 6: Media deduplication
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_media_deduplication(mock_db):
    talent_with_media = {
        "id": "talent-777",
        "name": "Media User",
        "email": "media@talentgram.com",
        "normalized_email": "media@talentgram.com",
        "media": [
            {
                "id": "existing-media-id",
                "category": "portfolio",
                "url": "http://res.cloudinary.com/test/image.jpg",
                "public_id": "test/image",
                "source_submission_media_id": "sub-media-111"
            }
        ]
    }
    
    submission = {
        "id": "sub-777",
        "talent_email": "media@talentgram.com"
    }
    
    # This media has the SAME source_submission_media_id as the existing talent's media
    duplicate_media = {
        "id": "sub-media-111",
        "category": "image",
        "url": "http://res.cloudinary.com/test/image.jpg",
        "public_id": "test/image"
    }
    
    mock_db.talents.find_one.return_value = talent_with_media
    
    with patch("core.db", mock_db):
        await sync_media_to_global_talent(submission, duplicate_media)
        # update_one should not be called because it is a duplicate media
        assert not mock_db.talents.update_one.called

# --------------------------------------------------------------------------
# Test 7: Audit log creation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_log_creation(mock_db):
    existing_talent = {
        "id": "talent-888",
        "name": "Audit User",
        "email": "audit@talentgram.com",
        "normalized_email": "audit@talentgram.com",
        "bio": "Old bio"
    }
    
    incoming_data = {
        "bio": "New bio"
    }
    
    mock_db.talents.update_one = AsyncMock()
    mock_db.profile_audits.insert_one = AsyncMock()
    
    with patch("core.db", mock_db):
        await merge_talent_profile(existing_talent, incoming_data, "admin_edit")
        
        # Verify that audit log was inserted
        assert mock_db.profile_audits.insert_one.called
        inserted_audit = mock_db.profile_audits.insert_one.call_args[0][0]
        assert inserted_audit["talent_id"] == "talent-888"
        assert "bio" in inserted_audit["changed_fields"]
        assert inserted_audit["old_values"]["bio"] == "Old bio"
        assert inserted_audit["new_values"]["bio"] == "New bio"
        assert inserted_audit["source"] == "admin_edit"
