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
        self.delete_one = AsyncMock()

    def find(self, *a, **k):
        return FakeCursor(self.docs)

    async def find_one(self, *a, **k):
        query = a[0] if a else {}
        for d in self.docs:
            match = True
            for k, v in query.items():
                if k == "id" and d.get("id") != v:
                    match = False
                if k == "deleted" and v == {"$ne": True} and d.get("deleted") is True:
                    match = False
            if match:
                return d
        return None

class FakeDB:
    def __init__(self):
        self.users = FakeColl([{"id": "adm-1", "email": "admin@talentgram.co", "role": "admin"}])
        self.whatsapp_contact_lists = FakeColl()
        self.whatsapp_audit_log = FakeColl()

mock_db = FakeDB()
import core
core.db = mock_db

# Patch router's db reference
from routers import whatsapp
whatsapp.db = mock_db

from fastapi.testclient import TestClient
from server import app
from core import make_token

client = TestClient(app)
admin_token = make_token({"id": "adm-1", "role": "admin", "email": "admin@talentgram.co"}, days=1)


@pytest.mark.asyncio
async def test_crud_contact_list():
    # 1. Create List
    payload = {
        "name": "Mumbai Clients",
        "description": "Mumbai regional coordinators",
        "contacts": [
            {"name": "Rahul Sharma", "phone": "+91 98765 43210"},
            {"name": "Rahul Duplicate", "phone": "+91 98765 43210"}, # duplicate phone number
            {"name": "Priya", "phone": "+919123456789"}
        ]
    }
    
    mock_db.whatsapp_contact_lists.docs = []
    mock_db.whatsapp_contact_lists.insert_one.reset_mock()
    
    resp = client.post(
        "/api/whatsapp/contact-lists",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=payload
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Mumbai Clients"
    assert len(data["contacts"]) == 2 # duplicate removed and normalized
    assert data["contacts"][0]["phone"] == "+919876543210" # normalized E.164-ish format
    assert data["contacts"][1]["phone"] == "+919123456789"
    assert "id" in data
    
    created_id = data["id"]
    
    # Mock search doc
    mock_db.whatsapp_contact_lists.docs = [data]
    
    # 2. Get list details
    resp_get = client.get(
        f"/api/whatsapp/contact-lists/{created_id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == created_id
    
    # 3. Update list
    update_payload = {
        "name": "Mumbai & UAE Clients",
        "description": "Mumbai and UAE regional coordinators",
        "contacts": [
            {"name": "Rahul Sharma", "phone": "+919876543210"},
            {"name": "Dubai coordinator", "phone": "+971501234567"}
        ]
    }
    resp_put = client.put(
        f"/api/whatsapp/contact-lists/{created_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_payload
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["name"] == "Mumbai & UAE Clients"
    assert len(resp_put.json()["contacts"]) == 2
    
    # 4. Soft Delete
    resp_delete = client.delete(
        f"/api/whatsapp/contact-lists/{created_id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp_delete.status_code == 200
    from unittest.mock import ANY
    mock_db.whatsapp_contact_lists.update_one.assert_called_with(
        {"id": created_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "updated_at": ANY}}
    )


@pytest.mark.asyncio
async def test_resolve_saved_lists():
    # Mock data setup
    list_1 = {
        "id": "list-1",
        "name": "Casting Directors",
        "description": "",
        "contacts": [
            {"name": "Casting A", "phone": "+919999999999"},
            {"name": "Casting B", "phone": "+918888888888"}
        ],
        "deleted": False
    }
    list_2 = {
        "id": "list-2",
        "name": "Production Houses",
        "description": "",
        "contacts": [
            {"name": "Producer A", "phone": "+919999999999"}, # duplicate from list-1
            {"name": "Producer B", "phone": "+917777777777"}
        ],
        "deleted": False
    }
    
    mock_db.whatsapp_contact_lists.docs = [list_1, list_2]
    
    resolve_payload = {
        "source_type": "SAVED_LISTS",
        "source_params": {
            "contact_list_ids": ["list-1", "list-2"]
        },
        "excluded_recipient_ids": []
    }
    
    resp = client.post(
        "/api/whatsapp/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=resolve_payload
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    
    # Verification of unique recipients (deduplicated by phone / recipient_id)
    # Casting A (+919999999999) and Producer A (+919999999999) resolve to the same E.164-ish number.
    # Therefore, count should be 3 unique targets.
    assert len(data["recipients"]) == 3
    
    phones = [r["phone"] for r in data["recipients"]]
    assert "+919999999999" in phones
    assert "+918888888888" in phones
    assert "+917777777777" in phones
