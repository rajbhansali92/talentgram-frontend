import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock
import sys
import os

# Set environment variables for config
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "password"

# Adjust path to find backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import app
from core import current_team_or_admin, current_admin

client = TestClient(app)

@pytest.fixture(autouse=True)
def override_auth():
    mock_admin = {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}
    app.dependency_overrides[current_team_or_admin] = lambda: mock_admin
    app.dependency_overrides[current_admin] = lambda: mock_admin
    yield
    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_get_public_onboarding_config_default():
    mock_db = MagicMock()
    mock_db.profile_configs = MagicMock()
    mock_db.profile_configs.find_one = AsyncMock(return_value=None)
    
    with patch("routers.applications.db", mock_db):
        response = client.get("/api/public/onboarding-config")
        assert response.status_code == 200
        data = response.json()
        assert data["profile_requirements"]["name"] == "required"
        assert data["portfolio_requirements"]["video"] == "required"

@pytest.mark.asyncio
async def test_get_public_onboarding_config_custom_query():
    mock_db = MagicMock()
    mock_db.profile_configs = MagicMock()
    
    custom_config = {
        "id": "custom-link-123",
        "title": "Special Casting Call",
        "profile_requirements": {
            "name": "required",
            "location": "optional",
            "instagram_handle": "optional",
            "instagram_followers": "optional",
        },
        "portfolio_requirements": {
            "portfolio": "required",
            "indian": "optional",
            "western": "optional",
            "video": "optional",
        }
    }
    mock_db.profile_configs.find_one = AsyncMock(side_effect=lambda query: custom_config if query.get("id") == "custom-link-123" else None)
    
    with patch("routers.applications.db", mock_db):
        response = client.get("/api/public/onboarding-config", params={"profile": "custom-link-123"})
        assert response.status_code == 200
        data = response.json()
        assert data["profile_requirements"]["location"] == "optional"
        assert data["portfolio_requirements"]["video"] == "optional"

@pytest.mark.asyncio
async def test_admin_profile_configs_crud():
    mock_db = MagicMock()
    mock_db.profile_configs = MagicMock()
    
    mock_db.profile_configs.insert_one = AsyncMock()
    mock_db.profile_configs.update_one = AsyncMock()
    mock_db.profile_configs.delete_one = AsyncMock()
    mock_db.profile_configs.find_one = AsyncMock(return_value={"id": "conf-1", "title": "Old"})
    
    # Mock find().to_list() helper for lists
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[{"id": "conf-1", "title": "Conf 1"}])
    mock_db.profile_configs.find = MagicMock(return_value=mock_cursor)
    
    with patch("routers.applications.db", mock_db):
        # List
        r_list = client.get("/api/admin/profile-configs")
        assert r_list.status_code == 200
        assert len(r_list.json()) == 1
        
        # Get one
        r_get = client.get("/api/admin/profile-configs/conf-1")
        assert r_get.status_code == 200
        assert r_get.json()["title"] == "Old"
        
        # Create
        payload = {
            "title": "New Requirement Config",
            "profile_requirements": {
                "name": "required",
                "location": "required",
                "instagram_handle": "optional",
                "instagram_followers": "optional",
            },
            "portfolio_requirements": {
                "portfolio": "required",
                "indian": "required",
                "western": "required",
                "video": "required",
            }
        }
        r_post = client.post("/api/admin/profile-configs", json=payload)
        assert r_post.status_code == 200
        assert "id" in r_post.json()
        assert r_post.json()["title"] == "New Requirement Config"
        
        # Update
        r_put = client.put("/api/admin/profile-configs/conf-1", json=payload)
        assert r_put.status_code == 200
        assert r_put.json()["ok"] is True
        
        # Delete
        r_del = client.delete("/api/admin/profile-configs/conf-1")
        assert r_del.status_code == 200
        assert r_del.json()["ok"] is True

@pytest.mark.asyncio
async def test_start_application_with_profile_id():
    mock_db = MagicMock()
    mock_db.applications = MagicMock()
    mock_db.talents = MagicMock()
    
    mock_db.applications.find_one = AsyncMock(return_value=None)
    mock_db.applications.insert_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)
    
    payload = {
        "email": "candidate@example.com",
        "first_name": "Jane",
        "last_name": "Smith",
        "phone": "+919999999999",
        "profile_id": "custom-config-456"
    }
    
    with patch("routers.applications.db", mock_db):
        response = client.post("/api/public/apply", json=payload)
        assert response.status_code == 200
        assert "id" in response.json()
        
        # Check that profile_id was stored in the insert doc
        called_args = mock_db.applications.insert_one.call_args[0][0]
        assert called_args["profile_id"] == "custom-config-456"

@pytest.mark.asyncio
async def test_finalize_with_linked_custom_config():
    mock_db = MagicMock()
    mock_db.profile_configs = MagicMock()
    mock_db.applications = MagicMock()
    mock_db.talents = MagicMock()
    
    # Custom config: Only location is required, name is optional
    custom_config = {
        "id": "custom-config-456",
        "profile_requirements": {
            "name": "optional",
            "location": "required",
            "instagram_handle": "optional",
            "instagram_followers": "optional",
        },
        "portfolio_requirements": {
            "portfolio": "optional",
            "indian": "optional",
            "western": "optional",
            "video": "optional",
        }
    }
    
    # Success application with location but no last name
    app_doc = {
        "id": "app-123",
        "talent_email": "jane@example.com",
        "profile_id": "custom-config-456",
        "form_data": {
            "first_name": "Jane",
            "last_name": "", # empty but optional!
            "location": "Bangalore" # required and present!
        },
        "media": []
    }
    
    mock_db.profile_configs.find_one = AsyncMock(return_value=custom_config)
    mock_db.applications.find_one = AsyncMock(return_value=app_doc)
    mock_db.applications.update_one = AsyncMock()
    mock_db.talents.find_one = AsyncMock(return_value=None)
    
    mock_token_data = {"sid": "app-123", "kind": "application"}
    
    with patch("routers.applications.db", mock_db), \
         patch("routers.applications.decode_submitter", AsyncMock(return_value=mock_token_data)):
        response = client.post("/api/public/apply/app-123/finalize", headers={"Authorization": "Bearer dummy"})
        assert response.status_code == 200
        assert response.json()["ok"] is True
