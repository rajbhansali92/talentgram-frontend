import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from routers.marketing import (
    ClientCreate,
    ClientUpdate,
    _serialise_client,
    create_client,
    update_client,
)


def test_client_create_pydantic_validation():
    # Valid input with advanced CRM fields
    c = ClientCreate(
        name="Audrey Hepburn",
        company_name="Classic Hollywood",
        phone_number="+1 555-1954",
        email="audrey@classic.com",
        tags=["High Value", "Key Account"],
        stage="active",
        value=150000.0,
    )
    assert c.name == "Audrey Hepburn"
    assert c.company_name == "Classic Hollywood"
    assert c.phone_number == "+1 555-1954"
    assert c.email == "audrey@classic.com"
    assert c.tags == ["High Value", "Key Account"]
    assert c.stage == "active"
    assert c.value == 150000.0


def test_client_update_pydantic_validation():
    c = ClientUpdate(
        name="Audrey Hepburn Edited",
        tags=["Key Account"],
        value=200000.0,
    )
    assert c.name == "Audrey Hepburn Edited"
    assert c.tags == ["Key Account"]
    assert c.value == 200000.0
    assert c.email is None  # optional update fields remain None


def test_serialise_client_graceful_degradation():
    # Verifies legacy client documents (missing new fields) serialize safely
    legacy_doc = {
        "_id": ObjectId("60a1f2e9d5e3c8b4a0f12345"),
        "name": "Legacy Client",
        "company_name": "Old Corp",
        "phone_number": "+123",
        "created_at": "2026-05-26T12:00:00Z",
        "last_contacted_date": "2026-05-26T12:00:00Z",
    }
    res = _serialise_client(legacy_doc)
    assert res["id"] == "60a1f2e9d5e3c8b4a0f12345"
    assert res["name"] == "Legacy Client"
    assert res["company_name"] == "Old Corp"
    assert res["phone_number"] == "+123"
    assert res["email"] is None
    assert res["tags"] == []
    assert res["stage"] == "lead"  # default lifecycle stage
    assert res["value"] is None


@pytest.mark.anyio
async def test_create_client_endpoint(monkeypatch):
    mock_db = MagicMock()
    mock_db.clients.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId("60a1f2e9d5e3c8b4a0f12345")))
    monkeypatch.setattr("routers.marketing.db", mock_db)

    payload = ClientCreate(
        name="Marilyn Monroe",
        company_name="Fox Studios",
        phone_number="+1 555-1959",
        email="marilyn@fox.com",
        tags=["Key Account", "Hot Lead"],
        stage="active",
        value=250000.0,
    )

    result = await create_client(payload)
    assert result["name"] == "Marilyn Monroe"
    assert result["company_name"] == "Fox Studios"
    assert result["phone_number"] == "+1 555-1959"
    assert result["email"] == "marilyn@fox.com"
    assert result["tags"] == ["Key Account", "Hot Lead"]
    assert result["stage"] == "active"
    assert result["value"] == 250000.0
    assert result["id"] == "60a1f2e9d5e3c8b4a0f12345"


@pytest.mark.anyio
async def test_update_client_endpoint(monkeypatch):
    mock_db = MagicMock()
    
    # Mock find_one for existing client
    existing_doc = {
        "_id": ObjectId("60a1f2e9d5e3c8b4a0f12345"),
        "name": "Original Name",
        "company_name": "Original Corp",
        "phone_number": "+123",
        "email": "orig@corp.com",
        "tags": ["Lead"],
        "stage": "lead",
        "value": 10000.0,
    }
    
    # We update Fox Studios doc
    updated_doc = {
        "_id": ObjectId("60a1f2e9d5e3c8b4a0f12345"),
        "name": "Updated Name",
        "company_name": "Fox Studios",
        "phone_number": "+123",
        "email": "orig@corp.com",
        "tags": ["Key Account", "Hot Lead"],
        "stage": "active",
        "value": 250000.0,
    }
    
    async def mock_find_one(query, projection=None):
        if query == {"_id": ObjectId("60a1f2e9d5e3c8b4a0f12345")}:
            return updated_doc
        return None

    mock_db.clients.find_one = mock_find_one
    mock_db.clients.update_one = AsyncMock()
    
    monkeypatch.setattr("routers.marketing.db", mock_db)

    payload = ClientUpdate(
        name="Updated Name",
        company_name="Fox Studios",
        tags=["Key Account", "Hot Lead"],
        stage="active",
        value=250000.0,
    )

    result = await update_client("60a1f2e9d5e3c8b4a0f12345", payload)
    assert result["name"] == "Updated Name"
    assert result["company_name"] == "Fox Studios"
    assert result["tags"] == ["Key Account", "Hot Lead"]
    assert result["stage"] == "active"
    assert result["value"] == 250000.0
