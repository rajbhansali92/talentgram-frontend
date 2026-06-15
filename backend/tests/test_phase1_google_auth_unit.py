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

client = TestClient(app)

@pytest.mark.asyncio
async def test_google_auth_endpoint_new_talent():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id_token": "mocked_id_token"}
    
    mock_db = MagicMock()
    mock_db.talents = MagicMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)
    mock_db.submission_drafts = MagicMock()
    mock_db.submission_drafts.find_one = AsyncMock(return_value=None)
    mock_db.submission_drafts.insert_one = AsyncMock()
    
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", mock_post), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value={
             "email": "new@talentgram.com",
             "sub": "google123",
             "name": "New Talent",
             "picture": "avatar_url"
         }), \
         patch("routers.auth.db", mock_db):
        
        response = client.post("/api/auth/google", json={
            "code": "mock_code",
            "redirect_uri": "http://localhost:3000/google-callback",
            "slug": "test-project"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["existing"] is False
        assert data["email"] == "new@talentgram.com"
        assert data["google_id"] == "google123"
        assert data["name"] == "New Talent"
        mock_db.submission_drafts.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_google_auth_endpoint_existing_talent_no_submission():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id_token": "mocked_id_token"}
    
    mock_db = MagicMock()
    mock_db.talents = MagicMock()
    mock_db.talents.find_one = AsyncMock(return_value={
        "id": "talent_id_123",
        "name": "Existing Talent",
        "email": "existing@talentgram.com",
        "location": "Mumbai",
        "height": "5'8\"",
        "phone": "+123456"
    })
    mock_db.projects = MagicMock()
    mock_db.projects.find_one = AsyncMock(return_value={
        "id": "project_id_123",
        "slug": "test-project"
    })
    mock_db.submissions = MagicMock()
    mock_db.submissions.find_one = AsyncMock(return_value=None)
    
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", mock_post), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value={
             "email": "existing@talentgram.com",
             "sub": "google456",
             "name": "Existing Talent",
             "picture": "avatar_url"
         }), \
         patch("routers.auth.db", mock_db):
        
        response = client.post("/api/auth/google", json={
            "code": "mock_code",
            "redirect_uri": "http://localhost:3000/google-callback",
            "slug": "test-project"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["existing"] is True
        assert data["email"] == "existing@talentgram.com"
        assert data["first_name"] == "Existing"
        assert data["last_name"] == "Talent"
        assert data["location"] == "Mumbai"
        assert "token" not in data

@pytest.mark.asyncio
async def test_google_auth_endpoint_existing_talent_with_submission():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id_token": "mocked_id_token"}
    
    mock_db = MagicMock()
    mock_db.talents = MagicMock()
    mock_db.talents.find_one = AsyncMock(return_value={
        "id": "talent_id_123",
        "name": "Existing Talent",
        "email": "existing@talentgram.com",
        "location": "Mumbai",
        "height": "5'8\"",
        "phone": "+123456"
    })
    mock_db.projects = MagicMock()
    mock_db.projects.find_one = AsyncMock(return_value={
        "id": "project_id_123",
        "slug": "test-project"
    })
    mock_db.submissions = MagicMock()
    mock_db.submissions.find_one = AsyncMock(return_value={
        "id": "submission_id_456",
        "project_id": "project_id_123",
        "talent_email": "existing@talentgram.com",
        "status": "draft"
    })
    
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", mock_post), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value={
             "email": "existing@talentgram.com",
             "sub": "google456",
             "name": "Existing Talent",
             "picture": "avatar_url"
         }), \
         patch("routers.auth.db", mock_db):
        
        response = client.post("/api/auth/google", json={
            "code": "mock_code",
            "redirect_uri": "http://localhost:3000/google-callback",
            "slug": "test-project"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["existing"] is True
        assert data["email"] == "existing@talentgram.com"
        assert data["token"] is not None
        assert data["submission_id"] == "submission_id_456"
        assert data["status"] == "draft"
