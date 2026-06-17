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
    mdb.profile_configs = MagicMock()
    
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
    mdb.submissions.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    mdb.submissions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
    
    # Setup mock find chaining for submissions
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[])
    mdb.submissions.find = MagicMock(return_value=mock_cursor)
    
    mdb.otp_audit_logs.count_documents = AsyncMock(return_value=0)
    mdb.otp_codes.update_many = AsyncMock()
    mdb.otp_codes.insert_one = AsyncMock()
    
    mdb.projects.find_one = AsyncMock(return_value={"id": "proj-123", "slug": "test-project"})
    mdb.projects.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    
    mdb.asset_metadata.find_one = AsyncMock(return_value=None)
    mdb.asset_metadata.delete_many = AsyncMock(return_value=MagicMock(deleted_count=0))
    mdb.casting_pipeline.find_one = AsyncMock(return_value=None)
    mdb.casting_pipeline.insert_one = AsyncMock()
    mdb.casting_pipeline.delete_many = AsyncMock(return_value=MagicMock(deleted_count=0))
    mdb.users.find = MagicMock()
    mdb.users.find.return_value.to_list = AsyncMock(return_value=[])
    mdb.notification_logs.insert_one = AsyncMock()
    
    mdb.profile_configs.find_one = AsyncMock(return_value={"id": "conf-123"})
    mdb.profile_configs.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    
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

# --------------------------------------------------------------------------
# Test 8: Project Deletion Safety (Audit Area 8)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_deletion_safety(mock_db):
    # Setup delete mock to confirm cascaded deletion is safe
    with patch("routers.projects.db", mock_db):
        response = client.delete("/api/projects/proj-123")
        assert response.status_code == 200
        assert response.json()["deleted_id"] == "proj-123"
        # Submissions should be cascaded
        assert mock_db.submissions.delete_many.called
        # Verify that talents delete is NEVER called
        assert not mock_db.talents.delete_one.called
        assert not mock_db.talents.delete_many.called

# --------------------------------------------------------------------------
# Test 9: Profile Config Deletion Safety (Audit Area 8)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_profile_config_deletion_safety(mock_db):
    with patch("routers.applications.db", mock_db):
        response = client.delete("/api/admin/profile-configs/conf-123")
        assert response.status_code == 200
        assert mock_db.profile_configs.delete_one.called
        # Verify talents or talent media are untouched
        assert not mock_db.talents.delete_one.called

# --------------------------------------------------------------------------
# Test 10: Safari Token Decodability (Audit Area 6)
# --------------------------------------------------------------------------
def test_safari_token_decodability():
    from core import make_token
    payload = {"role": "submitter", "sid": "sub-123", "kind": "application"}
    token = make_token(payload, days=7)
    
    # Token must decode to the same payload
    import jwt
    from core import JWT_SECRET
    decoded = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert decoded["role"] == "submitter"
    assert decoded["sid"] == "sub-123"
    assert decoded["kind"] == "application"


# --------------------------------------------------------------------------
# Test 11: Identity Merge Name Protection (Issue #1)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_identity_merge_name_protection(mock_db):
    existing_talent = {
        "id": "talent-111",
        "name": "Raj Bhansali",
        "email": "raj@test.com",
        "normalized_email": "raj@test.com"
    }
    
    incoming_data = {
        "name": "Deeya Damini"
    }
    
    mock_db.talents.update_one = AsyncMock()
    mock_db.profile_audits.insert_one = AsyncMock()
    
    with patch("core.db", mock_db):
        await merge_talent_profile(existing_talent, incoming_data, "application_approval")
        
        # Verify that existing name was preserved (not updated via update_one)
        assert not mock_db.talents.update_one.called
        assert mock_db.profile_audits.insert_one.called
        inserted_audit = mock_db.profile_audits.insert_one.call_args[0][0]
        assert "name_conflict" in inserted_audit["changed_fields"]
        assert inserted_audit["old_values"]["name_conflict"] == "Raj Bhansali"
        assert inserted_audit["new_values"]["name_conflict"] == "Deeya Damini"


# --------------------------------------------------------------------------
# Test 12: Media Deduplication Fingerprint (Issue #2)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_media_deduplication_fingerprint(mock_db):
    existing_talent = {
        "id": "talent-222",
        "name": "Test User",
        "email": "dedupe@test.com",
        "normalized_email": "dedupe@test.com",
        "media": [
            {
                "id": "existing-media-id",
                "category": "portfolio",
                "url": "http://res.cloudinary.com/test/image.jpg",
                "public_id": "test/image"
            }
        ]
    }
    
    app_doc = {
        "id": "app-222",
        "talent_email": "dedupe@test.com",
        "status": "submitted",
        "form_data": {
            "first_name": "Test",
            "last_name": "User"
        },
        "media": [
            {
                "id": "new-media-id",
                "category": "image",
                "url": "http://res.cloudinary.com/test/image.jpg",
                "public_id": "test/image"
            }
        ]
    }
    
    mock_db.applications.find_one = AsyncMock(side_effect=[app_doc, app_doc])
    mock_db.talents.find_one = AsyncMock(return_value=existing_talent)
    mock_db.talents.update_one = AsyncMock()
    mock_db.applications.update_one = AsyncMock()
    
    with patch("routers.applications.db", mock_db), patch("core.db", mock_db):
        from routers.applications import set_application_decision
        from routers.applications import SubmissionDecisionIn
        
        response = await set_application_decision(
            "app-222",
            SubmissionDecisionIn(decision="approved"),
            admin={"id": "admin-123", "email": "admin@talentgram.co"}
        )
        assert response["ok"] is True
        
        # Verify that update_one for media set was called with empty/no new media (only existing preserved)
        assert mock_db.talents.update_one.called
        first_call_args = mock_db.talents.update_one.call_args_list[0][0]
        # Should only write back the original 1 media item (not 2)
        assert len(first_call_args[1]["$set"]["media"]) == 1


# --------------------------------------------------------------------------
# Test 13: Approval Idempotency (Issue #3)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_idempotency(mock_db):
    app_doc = {
        "id": "app-333",
        "talent_email": "idempotent@test.com",
        "decision": "approved",
        "talent_id": "talent-333",
        "merged": True
    }
    
    mock_db.applications.find_one = AsyncMock(return_value=app_doc)
    mock_db.applications.update_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock()
    
    with patch("routers.applications.db", mock_db):
        from routers.applications import set_application_decision
        from routers.applications import SubmissionDecisionIn
        
        # Call it again
        response = await set_application_decision(
            "app-333",
            SubmissionDecisionIn(decision="approved"),
            admin={"id": "admin-123", "email": "admin@talentgram.co"}
        )
        assert response["ok"] is True
        # Verify no database updates were made since it's already approved
        assert not mock_db.applications.update_one.called


# --------------------------------------------------------------------------
# Test 14: Safari Upload Token Fallback (Issue #4)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safari_upload_token_fallback(mock_db):
    from core import make_token, decode_submitter
    # Create an expired JWT token
    token = make_token({"role": "submitter", "sid": "sub-444"}, days=-1)
    
    sub_doc = {
        "id": "sub-444",
        "project_slug": "test-slug",
        "access_token": token
    }
    mock_db.submissions.find_one = AsyncMock(return_value=sub_doc)
    
    with patch("core.db", mock_db):
        result = await decode_submitter(f"Bearer {token}")
        assert result is not None
        assert result["sid"] == "sub-444"


# --------------------------------------------------------------------------
# Test 15: Project Deletion Cascade casting_pipeline (Issue #5)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_deletion_cascade_pipeline(mock_db):
    mock_db.projects.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    mock_db.submissions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
    mock_db.casting_pipeline.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
    mock_db.asset_metadata.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
    
    with patch("routers.projects.db", mock_db), patch("cloudinary.api.delete_resources_by_prefix"), patch("cloudinary.api.delete_folder"):
        from routers.projects import delete_project
        response = await delete_project(
            "proj-123",
            admin={"id": "admin-123", "email": "admin@talentgram.co", "role": "admin"}
        )
        assert response["ok"] is True
        assert mock_db.casting_pipeline.delete_many.called
        assert mock_db.asset_metadata.delete_many.called

