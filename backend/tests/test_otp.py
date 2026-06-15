import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock
import sys
import os

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

# Adjust path to find backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import app
from datetime import datetime, timezone, timedelta

client = TestClient(app)

@pytest.mark.asyncio
async def test_send_otp_invalid_email():
    response = client.post("/api/auth/otp/send", json={"email": "invalid-email"})
    assert response.status_code == 400
    assert "Please enter a valid email address." in response.json()["detail"]

@pytest.mark.asyncio
async def test_send_otp_success():
    mock_db = MagicMock()
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.count_documents = AsyncMock(return_value=0)
    mock_db.otp_audit_logs.find_one = AsyncMock(return_value=None)
    mock_db.otp_audit_logs.insert_one = AsyncMock()
    
    mock_db.otp_codes = MagicMock()
    mock_db.otp_codes.update_many = AsyncMock()
    mock_db.otp_codes.insert_one = AsyncMock()

    with patch("routers.auth.db", mock_db), \
         patch("routers.auth.send_otp_email", AsyncMock(return_value=True)):
        
        response = client.post("/api/auth/otp/send", json={"email": "actor@yahoo.com"})
        assert response.status_code == 200
        assert response.json()["message"] == "Verification code sent successfully."
        
        # Verify db insert call
        mock_db.otp_codes.insert_one.assert_called_once()
        mock_db.otp_audit_logs.insert_one.assert_called_once()

@pytest.mark.asyncio
async def test_send_otp_rate_limit():
    mock_db = MagicMock()
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.count_documents = AsyncMock(return_value=5)

    with patch("routers.auth.db", mock_db):
        response = client.post("/api/auth/otp/send", json={"email": "actor@yahoo.com"})
        assert response.status_code == 429
        assert "Too many verification requests" in response.json()["detail"]

@pytest.mark.asyncio
async def test_verify_otp_invalid_or_expired():
    mock_db = MagicMock()
    mock_db.otp_codes = MagicMock()
    mock_db.otp_codes.find_one = AsyncMock(return_value=None) # No active record
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.insert_one = AsyncMock()

    with patch("routers.auth.db", mock_db):
        response = client.post("/api/auth/otp/verify", json={
            "email": "actor@yahoo.com",
            "otp": "123456",
            "slug": "test-slug"
        })
        assert response.status_code == 400
        assert "Invalid or expired verification code." in response.json()["detail"]

@pytest.mark.asyncio
async def test_verify_otp_too_many_attempts():
    mock_db = MagicMock()
    mock_db.otp_codes = MagicMock()
    mock_db.otp_codes.find_one = AsyncMock(return_value={
        "_id": "record_id",
        "email": "actor@yahoo.com",
        "hashed_otp": "hashed_otp_here",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "attempts": 5,
        "used": False
    })
    mock_db.otp_codes.update_one = AsyncMock()
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.insert_one = AsyncMock()

    with patch("routers.auth.db", mock_db):
        response = client.post("/api/auth/otp/verify", json={
            "email": "actor@yahoo.com",
            "otp": "123456",
            "slug": "test-slug"
        })
        assert response.status_code == 400
        assert "Too many failed attempts." in response.json()["detail"]

@pytest.mark.asyncio
async def test_verify_otp_success_new_user():
    import hashlib
    otp = "492813"
    hashed_otp = hashlib.sha256(otp.encode()).hexdigest()
    
    mock_db = MagicMock()
    mock_db.otp_codes = MagicMock()
    mock_db.otp_codes.find_one = AsyncMock(return_value={
        "_id": "record_id",
        "email": "actor@yahoo.com",
        "hashed_otp": hashed_otp,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "attempts": 0,
        "used": False
    })
    mock_db.otp_codes.update_one = AsyncMock()
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.insert_one = AsyncMock()
    
    mock_db.projects = MagicMock()
    mock_db.projects.find_one = AsyncMock(return_value={"id": "project_id_123"})
    mock_db.submissions = MagicMock()
    mock_db.submissions.find_one = AsyncMock(return_value=None)
    mock_db.talents = MagicMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)
    mock_db.talents.insert_one = AsyncMock()
    mock_db.submission_drafts = MagicMock()
    mock_db.submission_drafts.find_one = AsyncMock(return_value=None)
    mock_db.submission_drafts.insert_one = AsyncMock()

    with patch("routers.auth.db", mock_db):
        response = client.post("/api/auth/otp/verify", json={
            "email": "actor@yahoo.com",
            "otp": otp,
            "slug": "test-slug"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["existing"] is False
        assert data["email"] == "actor@yahoo.com"
        mock_db.talents.insert_one.assert_not_called()
        mock_db.submission_drafts.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_verify_otp_success_returning_user():
    import hashlib
    otp = "492813"
    hashed_otp = hashlib.sha256(otp.encode()).hexdigest()
    
    mock_db = MagicMock()
    mock_db.otp_codes = MagicMock()
    mock_db.otp_codes.find_one = AsyncMock(return_value={
        "_id": "record_id",
        "email": "returning@outlook.com",
        "hashed_otp": hashed_otp,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "attempts": 0,
        "used": False
    })
    mock_db.otp_codes.update_one = AsyncMock()
    mock_db.otp_audit_logs = MagicMock()
    mock_db.otp_audit_logs.insert_one = AsyncMock()
    
    mock_db.projects = MagicMock()
    mock_db.projects.find_one = AsyncMock(return_value={"id": "project_id_123"})
    
    mock_db.submissions = MagicMock()
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "submission_id_456",
        "project_id": "project_id_123",
        "talent_email": "returning@outlook.com",
        "status": "draft"
    })
    
    mock_db.talents = MagicMock()
    mock_db.talents.find_one = AsyncMock(return_value={
        "id": "talent_id_123",
        "name": "Returning Talent",
        "email": "returning@outlook.com",
        "location": "Delhi"
    })

    with patch("routers.auth.db", mock_db):
        response = client.post("/api/auth/otp/verify", json={
            "email": "returning@outlook.com",
            "otp": otp,
            "slug": "test-slug"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["existing"] is True
        assert data["email"] == "returning@outlook.com"
        assert data["token"] is not None
        assert data["submission_id"] == "submission_id_456"
        assert data["status"] == "draft"
        assert data["talent"]["first_name"] == "Returning"
        assert data["talent"]["last_name"] == "Talent"
        assert data["talent"]["location"] == "Delhi"
