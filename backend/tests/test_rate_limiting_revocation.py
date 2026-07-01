import os
import sys
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport
import asyncio

# Setup environment variables needed by core.py
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy-secret-value-longer-than-expected")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app
from core import db, make_token, hash_password

@pytest.mark.asyncio
async def test_security_hardening_suite():
    # -------------------------------------------------------------
    # Test 1: JWT Session Revocation
    # -------------------------------------------------------------
    # 1. Clear database sessions
    await db.sessions.delete_many({})
    await db.users.delete_many({"email": "test-admin@talentgram.co"})
    
    # 2. Register mock user
    user_doc = {
        "id": "user-123",
        "email": "test-admin@talentgram.co",
        "password_hash": hash_password("Password@123"),
        "role": "admin",
        "status": "active",
        "token_version": 0
    }
    await db.users.insert_one(user_doc)
    
    # 3. Request token
    payload = {
        "id": "user-123",
        "email": "test-admin@talentgram.co",
        "role": "admin",
        "tv": 0
    }
    token = make_token(payload)
    
    # Wait briefly for async session creation
    await asyncio.sleep(0.1)
    
    # 4. Access protected route
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        headers = {"Authorization": f"Bearer {token}"}
        r = await ac.get("/api/auth/me", headers=headers)
        assert r.status_code == 200, f"Access failed: {r.text}"
        
        # 5. Revoke (logout)
        r = await ac.post("/api/auth/logout", headers=headers)
        assert r.status_code == 200
        
        # 6. Reuse token -> should return 401
        r = await ac.get("/api/auth/me", headers=headers)
        assert r.status_code == 401
        assert "Session has been revoked" in r.text

    # -------------------------------------------------------------
    # Test 2: Authentication Rate Limiting
    # -------------------------------------------------------------
    await db.rate_limits.delete_many({})
    
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for i in range(5):
            r = await ac.post("/api/auth/login", json={
                "email": "nonexistent@test.com",
                "password": "WrongPassword"
            })
            assert r.status_code == 401
            
        # 6th attempt -> should return 429
        r = await ac.post("/api/auth/login", json={
            "email": "nonexistent@test.com",
            "password": "WrongPassword"
        })
        assert r.status_code == 429
        assert "Retry-After" in r.headers
