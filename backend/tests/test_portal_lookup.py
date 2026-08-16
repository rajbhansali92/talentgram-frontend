import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

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

sys.path.insert(0, os.path.abspath("backend"))

import core
mock_db = MagicMock()
core.db = mock_db

from fastapi.testclient import TestClient
from server import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_portal_lookup_finds_talent_by_normalized_email():
    """portal_lookup must use the canonical resolver ($or on normalized_email/
    email/source.talent_email), not a bare `email` match, so it recognizes a
    talent whose canonical `email` field differs from what they typed."""
    typed_email = "priya@gmail.com"
    talent = {
        "id": "talent-1",
        "name": "Priya Shah",
        "email": "priya.shah.old@example.com",
        "normalized_email": typed_email,
        "media": [],
    }
    mock_db.talents.find_one = AsyncMock(return_value=talent)

    resp = client.post("/api/portal/lookup", json={"email": typed_email})

    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["talent"]["name"] == "Priya Shah"

    # Confirm the actual query sent to Mongo uses the canonical $or, not a
    # bare {"email": ...} match.
    called_query = mock_db.talents.find_one.call_args[0][0]
    assert "$or" in called_query
    ors = called_query["$or"]
    assert {"normalized_email": typed_email} in ors
    assert {"email": typed_email} in ors
    assert {"source.talent_email": typed_email} in ors


@pytest.mark.asyncio
async def test_portal_lookup_returns_exists_false_for_unknown_email():
    mock_db.talents.find_one = AsyncMock(return_value=None)

    resp = client.post("/api/portal/lookup", json={"email": "nobody@example.com"})

    assert resp.status_code == 200
    assert resp.json() == {"exists": False}
