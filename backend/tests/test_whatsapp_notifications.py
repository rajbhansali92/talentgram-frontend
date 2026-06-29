import os
import sys
import pytest
import uuid
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Set required environment variables
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

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Define helper mocks
class FakeCursor:
    def __init__(self, docs):
        self._docs = docs
    def sort(self, *a, **k):
        return self
    async def to_list(self, length=None):
        return list(self._docs)

class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.insert_one = AsyncMock()
        self.update_one = AsyncMock(return_value=MagicMock(matched_count=1, modified_count=1))
        self.update_many = AsyncMock()
        self.delete_one = AsyncMock()
        self.delete_many = AsyncMock()

    def find(self, *a, **k):
        return FakeCursor(self.docs)

    async def find_one(self, *a, **k):
        return self.docs[0] if self.docs else None

class FakeDB:
    def __init__(self):
        self.users = FakeColl([{"id": "adm-1", "email": "admin@talentgram.co", "role": "admin"}])
        self.submissions = FakeColl()
        self.projects = FakeColl()
        self.talents = FakeColl()
        self.asset_metadata = FakeColl()
        self.whatsapp_config = FakeColl()
        self.whatsapp_batches = FakeColl()
        self.whatsapp_jobs = FakeColl()
        self.casting_pipeline = FakeColl()

# Mock database global
mock_db = FakeDB()
import core
core.db = mock_db

# Patch in submissions router's db too
from routers import submissions
submissions.db = mock_db

# Also patch pipeline
from routers import casting_pipeline
casting_pipeline.db = mock_db

from fastapi.testclient import TestClient
from server import app
from core import make_token

client = TestClient(app)

@pytest.mark.asyncio
async def test_new_submission_notification():
    mock_db.whatsapp_batches.insert_one.reset_mock()
    mock_db.whatsapp_jobs.insert_one.reset_mock()
    
    sid = "sub-test-new"
    pid = "proj-test-new"
    email = "test-talent@example.com"
    token = make_token({"role": "submitter", "email": email, "sid": sid}, days=1)
    
    # Mock documents
    mock_sub = {
        "id": sid,
        "project_id": pid,
        "talent_email": email,
        "talent_name": "Test Talent",
        "talent_phone": "+919999999999",
        "status": "draft",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "phone": "+919999999999",
            "height": "5'9\"",
            "location": "Mumbai",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""},
        },
        "media": [
            {"category": "intro_video", "public_id": "intro_vid_1"},
            {"category": "image", "public_id": "image_1"},
            {"category": "take_1", "public_id": "take_1"},
        ]
    }
    
    mock_proj = {
        "id": pid,
        "title": "Super Cool Brand Shoot",
        "brand_name": "Super Cool Brand",
    }
    
    # Populate mock collections
    mock_db.submissions.docs = [mock_sub]
    mock_db.projects.docs = [mock_proj]
    mock_db.talents.docs = []
    mock_db.asset_metadata.docs = []
    mock_db.whatsapp_config.docs = [{"key": "internal_notification_group_name", "value": "Talentgram Operations Team"}]
    
    # Make API request to finalize
    resp = client.post(
        f"/api/public/submissions/{sid}/finalize",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    
    # Let background tasks run
    await asyncio.sleep(0.1)
    
    # Assert WhatsApp batch and job were enqueued
    mock_db.whatsapp_batches.insert_one.assert_called_once()
    mock_db.whatsapp_jobs.insert_one.assert_called_once()
    
    # Verify content
    batch_arg = mock_db.whatsapp_batches.insert_one.call_args[0][0]
    job_arg = mock_db.whatsapp_jobs.insert_one.call_args[0][0]
    
    assert batch_arg["source_type"] == "INTERNAL_NOTIFICATION"
    assert batch_arg["template_id"] == "internal_notification"
    assert batch_arg["project_id"] == pid
    assert batch_arg["status"] == "pending"
    
    assert job_arg["batch_id"] == batch_arg["id"]
    assert job_arg["status"] == "pending"
    assert job_arg["destination"] == "Talentgram Operations Team"
    assert job_arg["destination_type"] == "group"
    assert "NEW SUBMISSION RECEIVED" in job_arg["message_body"]
    # Project name comes from brand_name (the real project field), not title.
    assert "Super Cool Brand" in job_arg["message_body"]
    # Clean format — no asset counts / internal details (Part 3).
    assert "Audition Takes" not in job_arg["message_body"]
    assert "Portfolio Images" not in job_arg["message_body"]
    assert "Assets" not in job_arg["message_body"]
    assert "Test Talent" in job_arg["message_body"]


@pytest.mark.asyncio
async def test_retest_submission_notification():
    mock_db.whatsapp_batches.insert_one.reset_mock()
    mock_db.whatsapp_jobs.insert_one.reset_mock()
    
    sid = "sub-test-retest"
    pid = "proj-test-retest"
    email = "test-talent@example.com"
    token = make_token({"role": "submitter", "email": email, "sid": sid}, days=1)
    
    # Mock documents: already submitted status
    mock_sub = {
        "id": sid,
        "project_id": pid,
        "talent_email": email,
        "talent_name": "Test Talent",
        "talent_phone": "+919999999999",
        "status": "submitted",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "phone": "+919999999999",
            "height": "5'9\"",
            "location": "Mumbai",
            "availability": {"status": "yes", "note": ""},
            "budget": {"status": "accept", "value": ""},
        },
        "media": [
            {"category": "intro_video", "public_id": "intro_vid_1"},
        ]
    }
    
    mock_proj = {
        "id": pid,
        "title": "Super Cool Brand Shoot",
        "brand_name": "Super Cool Brand",
    }
    
    # Populate mock collections
    mock_db.submissions.docs = [mock_sub]
    mock_db.projects.docs = [mock_proj]
    mock_db.talents.docs = []
    mock_db.asset_metadata.docs = []
    mock_db.whatsapp_config.docs = [] # triggers fallback
    
    # Make API request to finalize
    resp = client.post(
        f"/api/public/submissions/{sid}/finalize",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    
    # Let background tasks run
    await asyncio.sleep(0.1)
    
    # Assert WhatsApp batch and job were enqueued
    mock_db.whatsapp_batches.insert_one.assert_called_once()
    mock_db.whatsapp_jobs.insert_one.assert_called_once()
    
    # Verify content
    batch_arg = mock_db.whatsapp_batches.insert_one.call_args[0][0]
    job_arg = mock_db.whatsapp_jobs.insert_one.call_args[0][0]
    
    assert batch_arg["status"] == "pending"
    assert job_arg["destination"] == "Talentgram Operations" # fallback
    assert "SUBMISSION UPDATED" in job_arg["message_body"]


@pytest.mark.asyncio
async def test_decision_changed_notification():
    mock_db.whatsapp_batches.insert_one.reset_mock()
    mock_db.whatsapp_jobs.insert_one.reset_mock()
    
    sid = "sub-test-decision"
    pid = "proj-test-decision"
    admin_token = make_token({"id": "adm-1", "role": "admin", "email": "admin@talentgram.co"}, days=1)
    
    # Mock documents
    mock_sub = {
        "id": sid,
        "project_id": pid,
        "talent_email": "test-talent@example.com",
        "talent_name": "Test Talent",
        "talent_phone": "+919999999999",
        "status": "submitted",
        "decision": "pending",
        "form_data": {
            "first_name": "Test",
            "last_name": "Talent",
            "phone": "+919999999999",
        }
    }
    
    mock_proj = {
        "id": pid,
        "title": "Super Cool Brand Shoot",
        "brand_name": "Super Cool Brand",
    }
    
    # Populate mock collections
    mock_db.submissions.docs = [mock_sub]
    mock_db.projects.docs = [mock_proj]
    mock_db.talents.docs = []
    mock_db.whatsapp_config.docs = [{"key": "internal_notification_group_name", "value": "Talentgram Operations Team"}]
    
    # Make API request to set decision to approved
    resp = client.post(
        f"/api/projects/{pid}/submissions/{sid}/decision",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"decision": "approved", "note": "Looks great"}
    )
    assert resp.status_code == 200, resp.text
    
    # Let background tasks run
    await asyncio.sleep(0.1)
    
    # Assert no WhatsApp batch or job was enqueued
    mock_db.whatsapp_batches.insert_one.assert_not_called()
    mock_db.whatsapp_jobs.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_admin_test_internal_notification_endpoint():
    mock_db.whatsapp_batches.insert_one.reset_mock()
    mock_db.whatsapp_jobs.insert_one.reset_mock()
    
    admin_token = make_token({"id": "adm-1", "role": "admin", "email": "admin@talentgram.co"}, days=1)
    mock_db.whatsapp_config.docs = [{"key": "internal_notification_group_name", "value": "Talentgram Operations Test Group"}]
    
    # 1. Check admin protection (no auth header)
    resp_no_auth = client.post("/api/admin/whatsapp/test-internal-notification")
    assert resp_no_auth.status_code == 401
    
    # 2. Check auth with valid admin
    resp = client.post(
        "/api/admin/whatsapp/test-internal-notification",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "batch_id" in data
    assert "job_id" in data
    assert data["group_name"] == "Talentgram Operations Test Group"
    
    # Assert database inserts
    mock_db.whatsapp_batches.insert_one.assert_called_once()
    mock_db.whatsapp_jobs.insert_one.assert_called_once()
    
    batch_arg = mock_db.whatsapp_batches.insert_one.call_args[0][0]
    job_arg = mock_db.whatsapp_jobs.insert_one.call_args[0][0]
    
    assert batch_arg["source_type"] == "INTERNAL_NOTIFICATION"
    assert batch_arg["status"] == "pending"
    assert job_arg["destination_type"] == "group"
    assert job_arg["destination"] == "Talentgram Operations Test Group"
    assert "TALENTGRAM INTERNAL NOTIFICATION TEST" in job_arg["message_body"]

