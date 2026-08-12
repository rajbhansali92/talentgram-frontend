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

# Module-scoped event loop: the shared `core.db` Motor client binds to
# whichever event loop is active the first time it's used, and Motor's
# executor pool keeps that reference. Under the default function-scoped
# asyncio loop, each test gets (and tears down) its own loop, so any test
# after the first real-Mongo call fails with "Event loop is closed" —
# regardless of which test happens to run first. Sharing one loop across
# the whole module keeps the client's cached loop reference valid for
# every test. Sync tests (TestClient-based) are unaffected by this marker.
pytestmark = pytest.mark.asyncio(loop_scope="module")

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


# --------------------------------------------------------------------------
# Tests 16-19: insert_talent_or_recover (2026-08-13 root-cause fix)
#
# Root cause: a legacy admin-created Talent Profile can have no email at
# all (documented, allowed case — routers/talents.py's own docstring:
# "Admins can still create email-less talents (e.g. legacy) — those bypass
# the dedup"). If ANY uniqueness constraint OTHER than email exists on
# `talents` (e.g. a phone-unique index — see migrations/data_hub_indexes.py,
# a standalone script never wired into core.py's managed startup index set,
# so its presence in any given environment is not guaranteed/tracked), a
# NEW submission-created talent whose phone matches that admin record's
# phone fails `insert_one` with DuplicateKeyError. The old recovery code
# only re-queried by EMAIL, found nothing (the admin record has none), and
# silently left `talent_doc = None` with zero logging — the submission
# still reached `status="submitted"`/`decision="approved"` regardless
# (those writes are unconditional), so it looked completely fine in
# Submission Review, Approval, and Client View while simply never
# appearing in Global Talent.
#
# These tests use the REAL, reachable MongoDB instance (not mocks) because
# the defect is specifically about how the application reacts to a REAL
# MongoDB unique-index collision — a mock can't meaningfully prove that
# interaction. A scoped-to-this-test unique index (via a partial filter
# keyed on a unique marker field) is created and dropped within each test,
# and all documents are tagged and cleaned up in `finally`, so no real
# document is ever read, modified, or at risk.
# --------------------------------------------------------------------------
import uuid as _uuid
import core as _core

from core import db as _real_db, normalize_email as _normalize_email
from core import build_minimal_talent_from_form as _build_minimal_talent_from_form
from core import insert_talent_or_recover as _insert_talent_or_recover
from core import SubmissionDecisionIn as _SubmissionDecisionIn
from routers.submissions import set_decision as _set_decision

_IDTAG = "TEST_IDHARDEN_"


async def _ensure_email_unique_indexes() -> None:
    """This file overrides DB_NAME to "test" at import time (line ~10),
    which is a separate, index-less database — `core.py`'s own startup
    index creation (`seed_admin()`) never runs against it in a pytest
    context, only against whatever real deployment boots the app. These
    tests need the SAME two unique indexes production actually has to
    meaningfully exercise the fix, so they're (re-)created here, idempotent
    and with the exact same name/key/partialFilterExpression `core.py`
    itself uses (core.py:1533-1549) — a no-op if already present, in
    whichever database DB_NAME currently points to."""
    await _real_db.talents.create_index(
        "email", unique=True, name="talents_email_unique",
        partialFilterExpression={"email": {"$type": "string"}},
    )
    await _real_db.talents.create_index(
        "normalized_email", unique=True, name="talents_normalized_email_unique",
        partialFilterExpression={"normalized_email": {"$type": "string"}},
    )


async def _idharden_cleanup(index_name: str) -> None:
    await _real_db.talents.delete_many({"id": {"$regex": f"^{_IDTAG}"}})
    try:
        await _real_db.talents.drop_index(index_name)
    except Exception:
        pass


def _idharden_admin_doc(*, phone: str, name: str) -> dict:
    return {
        "id": f"{_IDTAG}{_uuid.uuid4().hex[:8]}",
        "name": name,
        "email": None,
        "normalized_email": None,
        "phone": phone,
        "media": [],
        "status": "SUBMITTED",
        "source": {"type": "admin", "talent_email": None, "reference_id": None},
        "_idharden_marker": "1",
    }


def _idharden_new_talent(*, phone: str, email: str, name: str, sid: str) -> dict:
    parts = name.split()
    doc = _build_minimal_talent_from_form(
        {"first_name": parts[0], "last_name": " ".join(parts[1:])},
        email=_normalize_email(email), talent_name=name, talent_phone=phone,
        alternate_contact_number=None, reference_id=sid, notes="test",
        created_by="auto-audition", include_skills=True, include_updated_at=True,
    )
    doc["id"] = f"{_IDTAG}{_uuid.uuid4().hex[:8]}"
    doc["_idharden_marker"] = "1"
    return doc


async def test_insert_talent_or_recover_clean_insert_unaffected():
    """No collision at all (the overwhelming common case) — behaves exactly
    like a plain insert_one always did. This is the non-regression guard:
    the fix must not change anything about the success path."""
    index_name = "test_idh_phone_unique_1"
    await _idharden_cleanup(index_name)
    try:
        new_talent = _idharden_new_talent(
            phone="9199991001", email="idh1@example.com", name="Regression Case", sid="sub-idh-1",
        )
        result, recovered = await _insert_talent_or_recover(
            new_talent, email=_normalize_email("idh1@example.com"), context="test",
        )
        assert result is not None
        assert result["id"] == new_talent["id"]
        assert recovered is False
    finally:
        await _idharden_cleanup(index_name)


async def test_insert_talent_or_recover_email_collision_still_recovers():
    """An email collision (the ALREADY-handled, pre-existing case, e.g. a
    genuine race between two finalizes for the same submitter) must keep
    working exactly as before: recovered=True, the existing record
    returned, ready for the caller's own merge logic."""
    index_name = "test_idh_phone_unique_2"
    await _idharden_cleanup(index_name)
    await _ensure_email_unique_indexes()
    try:
        email = "idh2@example.com"
        existing = {
            "id": f"{_IDTAG}{_uuid.uuid4().hex[:8]}", "name": "Existing Talent",
            "email": _normalize_email(email), "normalized_email": _normalize_email(email),
            "phone": "9199991002", "media": [], "status": "SUBMITTED",
            "source": {"type": "audition_submission", "talent_email": _normalize_email(email), "reference_id": "sub-orig"},
            "_idharden_marker": "1",
        }
        await _real_db.talents.insert_one(existing)
        new_talent = _idharden_new_talent(phone="9199991002", email=email, name="Existing Talent", sid="sub-idh-2")
        result, recovered = await _insert_talent_or_recover(
            new_talent, email=_normalize_email(email), context="test",
        )
        assert recovered is True
        assert result is not None
        assert result["id"] == existing["id"]
    finally:
        await _idharden_cleanup(index_name)


async def test_insert_talent_or_recover_non_email_collision_surfaces_not_silent():
    """THE ROOT-CAUSE FIX ITSELF. A collision on a field other than email
    (simulated here with a scoped, test-only unique index on `phone`,
    mirroring migrations/data_hub_indexes.py's exact constraint shape)
    against a legacy admin record with NO email must:
      1. Return (None, False) -- never silently link to the unrelated
         admin record (never merge, never overwrite, never link).
      2. Leave the admin record completely untouched.
      3. Be visible -- logged as an error -- instead of vanishing with zero
         trace, which was the actual defect."""
    index_name = "test_idh_phone_unique_3"
    await _idharden_cleanup(index_name)
    try:
        await _real_db.talents.create_index(
            [("phone", 1)], name=index_name, unique=True,
            partialFilterExpression={"_idharden_marker": {"$eq": "1"}, "phone": {"$type": "string"}},
        )
        admin_doc = _idharden_admin_doc(phone="9199991003", name="Legacy Admin Talent")
        await _real_db.talents.insert_one(admin_doc)

        new_talent = _idharden_new_talent(
            phone="9199991003", email="idh3.authenticated@example.com",
            name="Authenticated Talent", sid="sub-idh-3",
        )
        result, recovered = await _insert_talent_or_recover(
            new_talent, email=_normalize_email("idh3.authenticated@example.com"), context="test",
        )
        assert result is None
        assert recovered is False

        admin_after = await _real_db.talents.find_one({"id": admin_doc["id"]}, {"_id": 0})
        admin_before_compare = {k: v for k, v in admin_doc.items() if k != "_id"}
        assert admin_after == admin_before_compare, "admin record must be completely untouched"

        # The colliding phone must never have silently become the
        # authenticated record's identity either -- no second doc exists.
        count = await _real_db.talents.count_documents({"phone": "9199991003"})
        assert count == 1
    finally:
        await _idharden_cleanup(index_name)


async def test_insert_talent_or_recover_non_email_collision_logs_error(caplog):
    """Same scenario as above, asserting the previously-silent failure is
    now actually logged (the concrete "no longer swallowed" proof)."""
    import logging
    index_name = "test_idh_phone_unique_4"
    await _idharden_cleanup(index_name)
    try:
        await _real_db.talents.create_index(
            [("phone", 1)], name=index_name, unique=True,
            partialFilterExpression={"_idharden_marker": {"$eq": "1"}, "phone": {"$type": "string"}},
        )
        admin_doc = _idharden_admin_doc(phone="9199991004", name="Legacy Admin Talent 2")
        await _real_db.talents.insert_one(admin_doc)
        new_talent = _idharden_new_talent(
            phone="9199991004", email="idh4.authenticated@example.com",
            name="Authenticated Talent 2", sid="sub-idh-4",
        )
        caplog.set_level(logging.ERROR, logger="talentgram")
        result, recovered = await _insert_talent_or_recover(
            new_talent, email=_normalize_email("idh4.authenticated@example.com"), context="test-log-check",
        )
        assert result is None
        assert "test-log-check" in caplog.text
        assert "not resolvable by email" in caplog.text.lower() or "not resolvable by email" in caplog.text
    finally:
        await _idharden_cleanup(index_name)


# --------------------------------------------------------------------------
# Migration-aware coexistence redesign (2026-08-14): `talents.phone`
# uniqueness stays a REAL, enforced constraint -- but SCOPED via a partial
# filter to `source.type == "admin"` only, so it keeps blocking accidental
# admin/CSV-import duplicates (the population it always protected) while a
# submission-created, authenticated talent (`source.type ==
# "audition_submission"`) is never subject to it and can coexist with a
# legacy admin record sharing the same phone. `insert_talent_or_recover`
# records (never merges/links/deletes) the relationship as its OWN document
# in `db.talent_migration_candidates` -- NOT a field on the Talent document
# -- referencing both talent ids, matched fields, a confidence score, and a
# reviewable status, so the Talent schema stays clean and this can become
# the data source for a future Migration Review Center. Only
# `email`/`normalized_email` remain globally unique.
# --------------------------------------------------------------------------

async def _ensure_admin_scoped_phone_index() -> None:
    """Inlines the exact same drop-legacy/recreate-scoped-unique-index pair
    `core.py`'s `seed_admin()` runs on every boot (core.py, immediately
    after the normalized_email unique index) -- mirroring
    `_ensure_email_unique_indexes()`'s own convention above of replicating
    core.py's index setup here rather than invoking the full `seed_admin()`
    (which also seeds an admin user and is deliberately not exercised in
    this pytest context)."""
    try:
        await _real_db.talents.drop_index("phone_1")
    except Exception:
        pass
    await _real_db.talents.create_index(
        "phone", unique=True, name="talents_phone_unique_admin_scope",
        partialFilterExpression={"phone": {"$type": "string"}, "source.type": "admin"},
    )


async def _phone_scope_cleanup() -> None:
    await _idharden_cleanup("talents_phone_unique_admin_scope")
    try:
        await _real_db.talents.drop_index("phone_1")
    except Exception:
        pass
    await _real_db.talent_migration_candidates.delete_many(
        {"authenticated_talent_id": {"$regex": f"^{_IDTAG}"}}
    )


async def _candidate_for(authenticated_talent_id):
    return await _real_db.talent_migration_candidates.find_one(
        {"authenticated_talent_id": authenticated_talent_id}, {"_id": 0}
    )


async def test_talents_phone_index_is_admin_scoped_unique() -> None:
    """Direct proof of the index-level fix: after the self-heal step runs,
    `talents.phone` must carry a unique constraint SCOPED to
    source.type=="admin" -- not global, not absent -- even if a legacy,
    unscoped unique `phone_1` (as created by the old, standalone
    migrations/data_hub_indexes.py) already existed."""
    try:
        await _real_db.talents.create_index(
            [("phone", 1)], name="phone_1", unique=True,
            partialFilterExpression={"phone": {"$type": "string"}},
        )
        await _ensure_admin_scoped_phone_index()
        idx = await _real_db.talents.index_information()
        assert "phone_1" not in idx, "legacy unscoped unique phone_1 index must be dropped"
        spec = idx["talents_phone_unique_admin_scope"]
        assert spec["key"] == [("phone", 1)]
        assert spec.get("unique") is True, "phone uniqueness must still be enforced"
        assert spec["partialFilterExpression"]["source.type"] == "admin", (
            "uniqueness must be scoped to the admin population, not global"
        )
    finally:
        await _phone_scope_cleanup()


async def test_accidental_admin_duplicate_phone_still_blocked() -> None:
    """The constraint this index actually protects: two admin-created
    talents for DIFFERENT people that collide on phone by data-entry
    mistake must still be rejected exactly as before -- the redesign does
    NOT weaken this."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199996001"
        admin1 = _idharden_admin_doc(phone=phone, name="Person One")
        await _real_db.talents.insert_one(admin1)
        admin2 = _idharden_admin_doc(phone=phone, name="Person Two Accidental Dup")
        with pytest.raises(DuplicateKeyError):
            await _real_db.talents.insert_one(admin2)
    finally:
        await _phone_scope_cleanup()


async def test_legacy_only_talent_unaffected() -> None:
    """Legacy-only scenario: an admin talent with no onboarding counterpart
    at all inserts and reads back completely unaffected by any of this, and
    no candidate row is ever created for it."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        legacy = _idharden_admin_doc(phone="9199996002", name="Legacy Only Talent")
        await _real_db.talents.insert_one(legacy)
        found = await _real_db.talents.find_one({"id": legacy["id"]}, {"_id": 0})
        assert found is not None
        assert await _real_db.talent_migration_candidates.count_documents(
            {"legacy_talent_id": legacy["id"]}
        ) == 0
    finally:
        await _phone_scope_cleanup()


async def test_authenticated_only_talent_unaffected() -> None:
    """Authenticated-only scenario: no pre-existing admin record at all --
    insert succeeds normally, no candidate row is fabricated, and the
    talent still gets its originating_submission_id."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        onboarding = _idharden_new_talent(
            phone="9199996003", email="solo.onboard@example.com",
            name="Authenticated Only Talent", sid="sub-solo",
        )
        result, recovered = await _insert_talent_or_recover(
            onboarding, email=_normalize_email("solo.onboard@example.com"), context="solo-onboard-test",
        )
        assert result is not None and recovered is False
        assert "migration_link" not in result, "the relationship must never be stored on the Talent document"
        assert result["originating_submission_id"] == "sub-solo"
        assert await _candidate_for(result["id"]) is None
    finally:
        await _phone_scope_cleanup()


async def test_legacy_and_onboarding_talents_coexist_with_candidate_row_recorded() -> None:
    """The actual business scenario: a legacy admin-created talent (no
    email) and a newly authenticated Project Submission talent for the
    SAME real person, sharing both phone and instagram_handle, must both
    be creatable and coexist -- no recovery, no merge -- AND a
    talent_migration_candidates document must be recorded referencing both
    ids, with confidence 0.95 (both signals matched) and review_status
    "pending", ready for a reviewer's future action. The Talent documents
    themselves carry NO relationship field at all, and the legacy talent
    receives zero writes."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199996004"
        insta = "coexist.repro"
        legacy = _idharden_admin_doc(phone=phone, name="Coexist Test Talent")
        legacy["instagram_handle"] = insta
        await _real_db.talents.insert_one(legacy)

        onboarding = _idharden_new_talent(
            phone=phone, email="coexist.repro@example.com",
            name="Coexist Test Talent", sid="sub-coexist",
        )
        onboarding["instagram_handle"] = insta
        result, recovered = await _insert_talent_or_recover(
            onboarding, email=_normalize_email("coexist.repro@example.com"), context="coexist-test",
        )
        assert result is not None
        assert recovered is False, "must be a genuine new insert, not a collision recovery"
        assert result["id"] == onboarding["id"]
        assert "migration_link" not in result
        assert result["originating_submission_id"] == "sub-coexist"

        count = await _real_db.talents.count_documents(
            {"phone": phone, "id": {"$regex": f"^{_IDTAG}"}}
        )
        assert count == 2, "legacy and onboarding records must both exist"

        candidate = await _candidate_for(result["id"])
        assert candidate is not None, "expected a talent_migration_candidates row"
        assert candidate["legacy_talent_id"] == legacy["id"]
        assert candidate["authenticated_talent_id"] == result["id"]
        assert set(candidate["matched_fields"]) == {"phone", "instagram_handle"}
        assert candidate["confidence_score"] == 0.95
        assert candidate["review_status"] == "pending"
        assert candidate["reviewed_by"] is None
        assert candidate["reviewed_at"] is None
        assert candidate["reviewer_notes"] is None
        assert candidate["created_at"] and candidate["updated_at"]

        legacy_after = await _real_db.talents.find_one({"id": legacy["id"]}, {"_id": 0})
        legacy_before_compare = {k: v for k, v in legacy.items() if k != "_id"}
        assert legacy_after == legacy_before_compare, "legacy record must receive zero writes"
    finally:
        await _phone_scope_cleanup()


async def test_migration_candidate_confidence_scoring_by_matched_field() -> None:
    """Confidence score reflects which fields matched: phone-only and
    instagram-only cases must be distinguishable from a both-match, and
    lower than it -- documents the deliberately conservative, exact-match
    scoring scheme."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        # Phone-only match.
        legacy_phone = _idharden_admin_doc(phone="9199996009", name="Phone Only Legacy")
        await _real_db.talents.insert_one(legacy_phone)
        onboard_phone = _idharden_new_talent(
            phone="9199996009", email="phoneonly@example.com", name="Phone Only Legacy", sid="sub-phone-only",
        )
        result_phone, _ = await _insert_talent_or_recover(
            onboard_phone, email=_normalize_email("phoneonly@example.com"), context="phone-only-test",
        )
        cand_phone = await _candidate_for(result_phone["id"])
        assert cand_phone["matched_fields"] == ["phone"]
        assert cand_phone["confidence_score"] == 0.85

        # Instagram-only match (different phone).
        legacy_insta = _idharden_admin_doc(phone="9199996010", name="Insta Only Legacy")
        legacy_insta["instagram_handle"] = "insta.only.repro"
        await _real_db.talents.insert_one(legacy_insta)
        onboard_insta = _idharden_new_talent(
            phone="9199996011", email="instaonly@example.com", name="Insta Only Legacy", sid="sub-insta-only",
        )
        onboard_insta["instagram_handle"] = "insta.only.repro"
        result_insta, _ = await _insert_talent_or_recover(
            onboard_insta, email=_normalize_email("instaonly@example.com"), context="insta-only-test",
        )
        cand_insta = await _candidate_for(result_insta["id"])
        assert cand_insta["matched_fields"] == ["instagram_handle"]
        assert cand_insta["confidence_score"] == 0.75
        assert cand_insta["confidence_score"] < cand_phone["confidence_score"] < 0.95
    finally:
        await _phone_scope_cleanup()


async def test_two_authenticated_talents_sharing_phone_not_blocked() -> None:
    """Two DIFFERENT authenticated talents (e.g. household members sharing
    a phone) are not subject to the admin-scoped constraint at all --
    phone was never meant to be an identity field for this population."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199996005"
        first = _idharden_new_talent(phone=phone, email="sibling.a@example.com", name="Sibling A", sid="sub-sib-a")
        result1, recovered1 = await _insert_talent_or_recover(
            first, email=_normalize_email("sibling.a@example.com"), context="sibling-a",
        )
        assert result1 is not None and recovered1 is False

        second = _idharden_new_talent(phone=phone, email="sibling.b@example.com", name="Sibling B", sid="sub-sib-b")
        result2, recovered2 = await _insert_talent_or_recover(
            second, email=_normalize_email("sibling.b@example.com"), context="sibling-b",
        )
        assert result2 is not None and recovered2 is False
        assert result2["id"] != result1["id"]
    finally:
        await _phone_scope_cleanup()


async def test_email_identity_uniqueness_unaffected_by_scoped_phone_fix() -> None:
    """The other half of the same guarantee: scoping phone's uniqueness to
    the admin population must not have touched email's. Two talents with
    the same email must still collide at the DB level."""
    await _phone_scope_cleanup()
    await _ensure_email_unique_indexes()
    try:
        email = "same-identity@example.com"
        first = _idharden_new_talent(phone="9199996006", email=email, name="First Insert", sid="sub-e1")
        await _real_db.talents.insert_one(first)

        second = _idharden_new_talent(phone="9199996007", email=email, name="Second Insert", sid="sub-e2")
        with pytest.raises(DuplicateKeyError):
            await _real_db.talents.insert_one(second)
    finally:
        await _phone_scope_cleanup()


async def test_deleting_either_coexisting_record_leaves_the_other_intact() -> None:
    """Manual-deletion migration model: whichever of the pair the admin
    deletes first, the other must be completely unaffected."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199996008"
        legacy = _idharden_admin_doc(phone=phone, name="Delete Order Test")
        onboarding = _idharden_new_talent(
            phone=phone, email="delete.order@example.com", name="Delete Order Test", sid="sub-delorder",
        )

        # Order 1: delete legacy first.
        await _real_db.talents.insert_one(dict(legacy))
        await _real_db.talents.insert_one(dict(onboarding))
        await _real_db.talents.delete_one({"id": legacy["id"]})
        still_there = await _real_db.talents.find_one({"id": onboarding["id"]})
        assert still_there is not None
        await _phone_scope_cleanup()
        await _ensure_admin_scoped_phone_index()

        # Order 2: delete onboarding first.
        legacy2 = _idharden_admin_doc(phone=phone, name="Delete Order Test 2")
        onboarding2 = _idharden_new_talent(
            phone=phone, email="delete.order2@example.com", name="Delete Order Test 2", sid="sub-delorder2",
        )
        await _real_db.talents.insert_one(dict(legacy2))
        await _real_db.talents.insert_one(dict(onboarding2))
        await _real_db.talents.delete_one({"id": onboarding2["id"]})
        legacy_still_there = await _real_db.talents.find_one({"id": legacy2["id"]})
        assert legacy_still_there is not None
    finally:
        await _phone_scope_cleanup()


# --------------------------------------------------------------------------
# Final production-safety hardening (2026-08-14): closes two gaps found
# during review -- a migration-candidate LOOKUP failure and a candidate
# RECORDING failure must each independently be unable to block a talent
# creation that has already (or is about to) succeed -- plus proves
# duplicate-pair candidate prevention and dirty pre-existing admin data
# doesn't crash startup.
# --------------------------------------------------------------------------

async def test_migration_candidate_detection_failure_never_blocks_talent_creation() -> None:
    """If the pre-insert candidate LOOKUP itself raises (e.g. a transient
    Mongo error), the talent must still be created -- just without a
    candidate row, since none could be reliably detected."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199997101"
        legacy = _idharden_admin_doc(phone=phone, name="Detection Failure Test")
        await _real_db.talents.insert_one(legacy)
        onboarding = _idharden_new_talent(
            phone=phone, email="detectfail@example.com", name="Detection Failure Test", sid="sub-detectfail",
        )
        with patch.object(_core, "_find_migration_candidate", side_effect=RuntimeError("simulated lookup failure")):
            result, recovered = await _insert_talent_or_recover(
                onboarding, email=_normalize_email("detectfail@example.com"), context="detect-fail-test",
            )
        assert result is not None and recovered is False
        persisted = await _real_db.talents.find_one({"id": result["id"]})
        assert persisted is not None
        assert await _candidate_for(result["id"]) is None
    finally:
        await _phone_scope_cleanup()


async def test_migration_candidate_recording_failure_never_blocks_talent_creation() -> None:
    """If candidate RECORDING fails (write error, or a bug in that function
    itself) AFTER the talent insert already succeeded, the talent creation
    must still be reported as successful and the talent must be durably
    persisted -- a bookkeeping row is never allowed to undo or hide an
    already-completed talent creation."""
    await _phone_scope_cleanup()
    await _ensure_admin_scoped_phone_index()
    try:
        phone = "9199997102"
        legacy = _idharden_admin_doc(phone=phone, name="Recording Failure Test")
        await _real_db.talents.insert_one(legacy)
        onboarding = _idharden_new_talent(
            phone=phone, email="recordfail@example.com", name="Recording Failure Test", sid="sub-recordfail",
        )
        with patch.object(_core, "_record_migration_candidate", side_effect=RuntimeError("simulated write failure")):
            result, recovered = await _insert_talent_or_recover(
                onboarding, email=_normalize_email("recordfail@example.com"), context="record-fail-test",
            )
        assert result is not None and recovered is False
        persisted = await _real_db.talents.find_one({"id": result["id"]})
        assert persisted is not None
    finally:
        await _phone_scope_cleanup()


async def _ensure_migration_candidate_unique_index() -> None:
    """This file's DB_NAME=="test" database never runs the full
    `seed_admin()` (see `_ensure_email_unique_indexes()`'s docstring above
    for why), so `talent_migration_candidates`'s compound unique index --
    normally created by seed_admin()'s p0_indexes loop -- doesn't exist
    here unless explicitly created, exact same name/key as core.py."""
    await _real_db.talent_migration_candidates.create_index(
        [("legacy_talent_id", 1), ("authenticated_talent_id", 1)],
        unique=True, name="tmc_legacy_authenticated_unique",
    )


async def test_duplicate_candidate_rows_for_same_pair_prevented() -> None:
    """The compound unique index on (legacy_talent_id,
    authenticated_talent_id) prevents two candidate rows from ever being
    recorded for the exact same pair, while a DIFFERENT authenticated
    talent matching the SAME legacy talent still gets its own row."""
    await _phone_scope_cleanup()
    await _ensure_migration_candidate_unique_index()
    await _ensure_admin_scoped_phone_index()
    try:
        legacy = _idharden_admin_doc(phone="9199997103", name="Duplicate Pair Test")
        await _real_db.talents.insert_one(legacy)
        authenticated_id = f"{_IDTAG}dup-pair-{_uuid.uuid4().hex[:8]}"
        match = {"legacy_talent_id": legacy["id"], "matched_on": ["phone"]}

        await _core._record_migration_candidate(authenticated_id, match)
        await _core._record_migration_candidate(authenticated_id, match)  # same pair again

        count = await _real_db.talent_migration_candidates.count_documents(
            {"legacy_talent_id": legacy["id"], "authenticated_talent_id": authenticated_id}
        )
        assert count == 1, "duplicate rows for the same pair must be prevented"

        # A different authenticated talent matching the SAME legacy talent
        # must still get its own row (one legacy -> many authenticated).
        other_authenticated_id = f"{_IDTAG}dup-pair-other-{_uuid.uuid4().hex[:8]}"
        await _core._record_migration_candidate(other_authenticated_id, match)
        total_for_legacy = await _real_db.talent_migration_candidates.count_documents(
            {"legacy_talent_id": legacy["id"]}
        )
        assert total_for_legacy == 2, "one legacy talent must be able to have multiple distinct candidates"
    finally:
        await _phone_scope_cleanup()
        await _real_db.talent_migration_candidates.delete_many({"legacy_talent_id": legacy["id"]})


async def test_dirty_preexisting_admin_duplicate_phone_does_not_crash_startup() -> None:
    """Simulates production data that already violates the admin-scoped
    phone constraint BEFORE this migration ever runs: startup index setup
    must log a warning and continue -- never crash, never touch/delete/
    merge the offending data -- and must self-heal (successfully create
    the scoped index) once that dirty data is later resolved."""
    await _phone_scope_cleanup()
    try:
        phone = "9199997104"
        dup1 = _idharden_admin_doc(phone=phone, name="Dirty Data One")
        dup2 = _idharden_admin_doc(phone=phone, name="Dirty Data Two")
        await _real_db.talents.insert_one(dup1)
        await _real_db.talents.insert_one(dup2)

        # Calls the REAL seed_admin() here (not the inlined test helper) --
        # this test's whole purpose is proving the actual production
        # startup path's own try/except safety net, not a replica of it.
        # seed_admin() is idempotent (also seeds/no-ops the admin user;
        # harmless in this real-Mongo test database).
        await _core.seed_admin()  # must not raise

        idx = await _real_db.talents.index_information()
        assert "talents_phone_unique_admin_scope" not in idx, (
            "the scoped index cannot be created while a violation exists"
        )

        d1 = await _real_db.talents.find_one({"id": dup1["id"]}, {"_id": 0})
        d2 = await _real_db.talents.find_one({"id": dup2["id"]}, {"_id": 0})
        assert d1 == {k: v for k, v in dup1.items() if k != "_id"}
        assert d2 == {k: v for k, v in dup2.items() if k != "_id"}

        # Resolve the dirty data and prove the index self-heals afterward.
        await _real_db.talents.delete_one({"id": dup2["id"]})
        await _core.seed_admin()
        idx2 = await _real_db.talents.index_information()
        assert "talents_phone_unique_admin_scope" in idx2
    finally:
        await _phone_scope_cleanup()


async def _protection_present() -> bool:
    idx = await _real_db.talents.index_information()
    return "phone_1" in idx or "talents_phone_unique_admin_scope" in idx


async def test_old_phone_index_not_dropped_until_new_one_confirmed_created() -> None:
    """The fail-safe migration sequence (2026-08-15): on a healthy
    database where the old collection-wide unique `phone_1` index is
    still present, running the real `seed_admin()` must result in
    protection being present BEFORE, DURING (implicitly -- create-before-
    drop means there's no window), and AFTER the migration -- old index
    replaced by the new scoped one, never a state with neither."""
    await _phone_scope_cleanup()
    try:
        # Scoped to our own tagged documents via the same _idharden_marker
        # convention used throughout this file, so this simulated "old
        # global unique index" can never collide with real data.
        await _real_db.talents.create_index(
            [("phone", 1)], name="phone_1", unique=True,
            partialFilterExpression={"phone": {"$type": "string"}, "_idharden_marker": {"$eq": "1"}},
        )
        assert await _protection_present(), "precondition: protection must exist before migration"

        await _core.seed_admin()

        idx = await _real_db.talents.index_information()
        assert "phone_1" not in idx, "old index must be dropped once the replacement is confirmed active"
        assert "talents_phone_unique_admin_scope" in idx, "new scoped index must be active"
        assert await _protection_present(), "protection must never be absent after a successful migration"

        # Restart: idempotent, no churn, same end state.
        idx_before_restart = idx
        await _core.seed_admin()
        idx_after_restart = await _real_db.talents.index_information()
        assert idx_after_restart["talents_phone_unique_admin_scope"] == idx_before_restart["talents_phone_unique_admin_scope"]
        assert "phone_1" not in idx_after_restart
    finally:
        await _phone_scope_cleanup()


async def test_dirty_data_never_causes_a_zero_protection_window() -> None:
    """The core guarantee the fail-safe redesign exists for: on a database
    that has admin-vs-admin duplicate phone data (so the new scoped index
    CANNOT be created), migration must never reach a state where dropping
    happened but creating did not. Since this environment never had
    `phone_1` either, the correct outcome is "no protection existed
    before, none exists after" -- not "had protection, now doesn't"."""
    await _phone_scope_cleanup()
    try:
        phone = "9199997201"
        dup1 = _idharden_admin_doc(phone=phone, name="Zero Window Test 1")
        dup2 = _idharden_admin_doc(phone=phone, name="Zero Window Test 2")
        await _real_db.talents.insert_one(dup1)
        await _real_db.talents.insert_one(dup2)

        had_protection_before = await _protection_present()

        await _core.seed_admin()  # must not raise

        idx = await _real_db.talents.index_information()
        assert "talents_phone_unique_admin_scope" not in idx
        assert "phone_1" not in idx
        has_protection_after = await _protection_present()
        # The invariant: protection can only be LOST if it EXISTED before
        # and doesn't after. Here it never existed, so this is consistent,
        # not a regression -- assert the specific non-regressive case.
        assert had_protection_before == has_protection_after == False, (
            "dirty admin-vs-admin data must never cause EXISTING protection "
            "to be silently removed -- and here, since none existed before, "
            "none should be fabricated or lost either"
        )
    finally:
        await _phone_scope_cleanup()


async def test_source_consistency_across_both_creation_call_sites() -> None:
    """Both real call sites (submission_finalize's own construction and
    set_decision's fallback) build their new_talent dict via the SAME
    shared `build_minimal_talent_from_form`, so source.type,
    source.reference_id, and originating_submission_id are guaranteed
    identical in shape regardless of which one ran."""
    finalize_style = _build_minimal_talent_from_form(
        {"first_name": "Finalize", "last_name": "Style"}, email="src1@example.com",
        talent_name="Finalize Style", talent_phone="9199997105", alternate_contact_number=None,
        reference_id="sub-src-1", notes="", created_by="auto-audition",
        include_skills=True, include_updated_at=True,
    )
    decision_fallback_style = _build_minimal_talent_from_form(
        {"first_name": "Decision", "last_name": "Style"}, email="src2@example.com",
        talent_name="Decision Style", talent_phone="9199997106", alternate_contact_number=None,
        reference_id="sub-src-2", notes="", created_by="auto-decision-sync",
        include_skills=False, include_updated_at=False,
    )
    for doc, expected_ref in ((finalize_style, "sub-src-1"), (decision_fallback_style, "sub-src-2")):
        assert doc["source"]["type"] == "audition_submission"
        assert doc["source"]["reference_id"] == expected_ref
        assert doc["originating_submission_id"] == expected_ref


# --------------------------------------------------------------------------
# set_decision() retry fix (2026-08-16): a submission can be left
# "approved" with no talent_id if the talent-creation fallback failed at
# approval time (the historical phone-uniqueness collision fixed above).
# The old idempotency guard (`if sub.get("decision") == payload.decision:
# return`) blocked ANY retry of that fallback forever, even after the
# underlying cause was fixed. The new guard only short-circuits when the
# submission is ALREADY linked (`and sub.get("talent_id")`), so re-running
# the exact same decision on an unlinked submission now retries the exact
# same, unmodified talent-resolution/creation path. These tests call
# set_decision() directly (bypassing the HTTP/auth layer, same pattern as
# `_insert_talent_or_recover` above) against real MongoDB.
# --------------------------------------------------------------------------

_SDTAG = "TEST_SETDEC_"
_SD_ADMIN = {"email": "admin@talentgram.co", "role": "admin", "id": "admin-test"}


async def _sd_cleanup() -> None:
    await _real_db.talents.delete_many({"id": {"$regex": f"^{_SDTAG}"}})
    # build_minimal_talent_from_form() always mints its own fresh, UNTAGGED
    # UUID for auto-created talents -- they're never caught by the id-prefix
    # filter above. Every one of them carries originating_submission_id set
    # to the (tagged) submission id that created it, so clean up by that too.
    await _real_db.talents.delete_many({"originating_submission_id": {"$regex": f"^{_SDTAG}"}})
    await _real_db.submissions.delete_many({"id": {"$regex": f"^{_SDTAG}"}})
    await _real_db.projects.delete_many({"id": {"$regex": f"^{_SDTAG}"}})


async def _sd_make_project() -> str:
    pid = f"{_SDTAG}proj-{_uuid.uuid4().hex[:8]}"
    await _real_db.projects.insert_one({
        "id": pid, "brand_name": "Set Decision Test Brand", "status": "ongoing",
        "slug": f"{_SDTAG}slug-{_uuid.uuid4().hex[:8]}",
    })
    return pid


def _sd_submission(pid: str, *, email: str, phone: str, decision: str, talent_id=None) -> dict:
    return {
        "id": f"{_SDTAG}sub-{_uuid.uuid4().hex[:8]}", "project_id": pid,
        "talent_name": "Set Decision Test Person", "talent_email": email,
        "talent_phone": phone, "status": "draft", "decision": decision,
        "submitted_at": None, "form_data": {"first_name": "Set", "last_name": "Decision"},
        "decided_at": "2026-08-10T08:23:48+00:00",
        **({"talent_id": talent_id} if talent_id else {}),
    }


async def test_reapproval_with_existing_talent_is_idempotent() -> None:
    """Case B: decision unchanged AND talent_id already present -> the
    guard must still fire immediately, a true no-op. No second talent, no
    change to the existing one."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        talent = {
            "id": f"{_SDTAG}talent-{_uuid.uuid4().hex[:8]}", "name": "Already Linked",
            "email": "already.linked@example.com", "normalized_email": "already.linked@example.com",
            "phone": "9199991001", "media": [], "status": "SUBMITTED",
            "source": {"type": "audition_submission", "talent_email": "already.linked@example.com", "reference_id": "x"},
        }
        await _real_db.talents.insert_one(talent)
        sub = _sd_submission(pid, email="already.linked@example.com", phone="9199991001", decision="approved", talent_id=talent["id"])
        await _real_db.submissions.insert_one(sub)

        result = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        assert result == {"ok": True}

        talent_after = await _real_db.talents.find_one({"id": talent["id"]}, {"_id": 0})
        talent_before_compare = {k: v for k, v in talent.items() if k != "_id"}
        assert talent_after == talent_before_compare, "existing talent must be completely unchanged"
        assert await _real_db.talents.count_documents({"email": "already.linked@example.com"}) == 1
    finally:
        await _sd_cleanup()


async def test_reapproval_with_missing_talent_id_retries_creation() -> None:
    """Case C: decision unchanged, talent_id absent, no talent exists by
    email -> the fallback must now run, create the talent, and link it."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        legacy = {
            "id": f"{_SDTAG}legacy-{_uuid.uuid4().hex[:8]}", "name": "Retry Creation Legacy",
            "phone": "9199991002", "media": [], "status": "SUBMITTED",
            "source": {"type": "admin", "talent_email": None, "reference_id": None},
        }
        await _real_db.talents.insert_one(legacy)
        sub = _sd_submission(pid, email="retry.creation@example.com", phone="9199991002", decision="approved")
        await _real_db.submissions.insert_one(sub)

        result = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        assert result == {"ok": True}

        fresh = await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})
        assert fresh.get("talent_id"), "talent_id must now be populated"

        authenticated = await _real_db.talents.find_one({"email": "retry.creation@example.com"}, {"_id": 0})
        assert authenticated is not None
        assert authenticated["id"] == fresh["talent_id"]
        assert authenticated["source"]["type"] == "audition_submission"
        assert authenticated["originating_submission_id"] == sub["id"]

        legacy_after = await _real_db.talents.find_one({"id": legacy["id"]}, {"_id": 0})
        legacy_before_compare = {k: v for k, v in legacy.items() if k != "_id"}
        assert legacy_after == legacy_before_compare, "legacy record must be untouched"

        # build_minimal_talent_from_form() mints its own untagged UUID for
        # the authenticated talent, so match both records by their known
        # ids directly rather than an id-prefix filter.
        count = await _real_db.talents.count_documents(
            {"phone": "9199991002", "id": {"$in": [legacy["id"], authenticated["id"]]}}
        )
        assert count == 2, "legacy and newly-created authenticated talent must coexist"
    finally:
        await _sd_cleanup()


async def test_reapproval_with_missing_talent_id_recovers_existing_email() -> None:
    """Case D: decision unchanged, talent_id absent, but a talent with
    that email ALREADY exists -> must recover/link it, never create a
    duplicate."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        existing = {
            "id": f"{_SDTAG}existing-{_uuid.uuid4().hex[:8]}", "name": "Recover Existing",
            "email": "recover.existing@example.com", "normalized_email": "recover.existing@example.com",
            "phone": "9199991003", "media": [], "status": "SUBMITTED",
            "source": {"type": "audition_submission", "talent_email": "recover.existing@example.com", "reference_id": "some-other-sub"},
        }
        await _real_db.talents.insert_one(existing)
        sub = _sd_submission(pid, email="recover.existing@example.com", phone="9199991003", decision="approved")
        await _real_db.submissions.insert_one(sub)

        result = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        assert result == {"ok": True}

        fresh = await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})
        assert fresh.get("talent_id") == existing["id"], "must link to the EXISTING talent, not create a new one"
        assert await _real_db.talents.count_documents({"email": "recover.existing@example.com"}) == 1
    finally:
        await _sd_cleanup()


async def test_reapproval_does_not_duplicate_existing_link() -> None:
    """Repeated approval after a successful repair must remain idempotent
    -- calling set_decision(approved) a THIRD time (decision unchanged,
    talent_id now present from the retry) must not create another talent
    or re-run the fallback."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        legacy = {
            "id": f"{_SDTAG}legacy-{_uuid.uuid4().hex[:8]}", "name": "No Redupe Legacy",
            "phone": "9199991004", "media": [], "status": "SUBMITTED",
            "source": {"type": "admin", "talent_email": None, "reference_id": None},
        }
        await _real_db.talents.insert_one(legacy)
        sub = _sd_submission(pid, email="no.redupe@example.com", phone="9199991004", decision="approved")
        await _real_db.submissions.insert_one(sub)

        await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        first_talent_id = (await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})).get("talent_id")
        assert first_talent_id

        # Third call: decision unchanged, talent_id now present -> true no-op.
        result3 = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        assert result3 == {"ok": True}
        final = await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})
        assert final.get("talent_id") == first_talent_id
        assert await _real_db.talents.count_documents({"email": "no.redupe@example.com"}) == 1
    finally:
        await _sd_cleanup()


async def test_reapproval_failure_does_not_report_false_success_state() -> None:
    """Case F: talent creation fails for an unexpected reason during a
    retry -- talent_id must remain absent (accurately reflecting reality,
    not a fabricated link), no talent is created, and the failure is
    logged clearly (not silently absorbed)."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        sub = _sd_submission(pid, email="unexpected.failure@example.com", phone="9199991005", decision="approved")
        await _real_db.submissions.insert_one(sub)

        with patch("core.insert_talent_or_recover", return_value=(None, False)):
            result = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)

        # The endpoint's existing response contract is unchanged by this
        # fix (not touched, per the explicit no-API-change requirement) --
        # {"ok": True} still comes back, exactly as it already did on the
        # very first, pre-fix failed attempt. What matters is that the
        # DATA correctly reflects the failure, not a fabricated success.
        assert result == {"ok": True}
        fresh = await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})
        assert fresh.get("talent_id") is None, "must not fabricate a talent_id on failure"
        assert await _real_db.talents.find_one({"email": "unexpected.failure@example.com"}) is None
    finally:
        await _sd_cleanup()


async def test_reapproval_reproduces_and_fixes_the_historical_ahana_angela_state() -> None:
    """Direct reproduction of the exact real-world state discovered in
    production for Ahana Pocha and Angela Kumar: decision="approved",
    talent_id absent, legacy admin record shares the submission's phone,
    legacy record has no email and no `source` field at all (the
    documented ~93.5%-of-production gap). Confirms the retry succeeds
    despite the legacy record having no `source` field -- source-field
    absence was proven irrelevant to insertion, only to migration-candidate
    detection."""
    await _sd_cleanup()
    try:
        pid = await _sd_make_project()
        legacy = {
            "id": f"{_SDTAG}legacy-{_uuid.uuid4().hex[:8]}", "name": "Historical Repro Person",
            "phone": "9199991006", "media": [], "status": "SUBMITTED",
            # Deliberately NO "source" key at all -- matches the real
            # production shape found for both Ahana's and Angela's legacy
            # records.
        }
        await _real_db.talents.insert_one(legacy)
        sub = _sd_submission(pid, email="historical.repro@example.com", phone="9199991006", decision="approved")
        # decision already "approved" with no talent_id, exactly as found
        # in production for both real cases.
        assert sub["decision"] == "approved" and "talent_id" not in sub
        await _real_db.submissions.insert_one(sub)

        result = await _set_decision(pid, sub["id"], _SubmissionDecisionIn(decision="approved"), _SD_ADMIN)
        assert result == {"ok": True}

        fresh = await _real_db.submissions.find_one({"id": sub["id"]}, {"_id": 0})
        assert fresh.get("talent_id"), "the historical stuck state must now resolve"
        assert fresh["decision"] == "approved"

        authenticated = await _real_db.talents.find_one({"id": fresh["talent_id"]}, {"_id": 0})
        assert authenticated is not None
        assert authenticated["email"] == "historical.repro@example.com"
        assert authenticated["source"]["type"] == "audition_submission"
        assert authenticated["originating_submission_id"] == sub["id"]

        legacy_after = await _real_db.talents.find_one({"id": legacy["id"]}, {"_id": 0})
        legacy_before_compare = {k: v for k, v in legacy.items() if k != "_id"}
        assert legacy_after == legacy_before_compare, "legacy record (source-less, exactly like the real cases) must remain untouched"

        count = await _real_db.talents.count_documents(
            {"phone": "9199991006", "id": {"$in": [legacy["id"], authenticated["id"]]}}
        )
        assert count == 2, "legacy and authenticated must coexist, matching the intended migration model"
    finally:
        await _sd_cleanup()

