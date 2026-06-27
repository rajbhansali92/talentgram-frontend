import os
import sys
import pytest
import hmac
import hashlib

# Set required environment variables before importing core/server
os.environ["MONGO_URL"] = "mongodb://localhost:27017"
os.environ["DB_NAME"] = "test"
os.environ["JWT_SECRET"] = "dummy"
os.environ["CLOUDINARY_CLOUD_NAME"] = "dummy"
os.environ["CLOUDINARY_API_KEY"] = "dummy"
os.environ["CLOUDINARY_API_SECRET"] = "dummy"
os.environ["ADMIN_EMAIL"] = "admin@talentgram.co"
os.environ["ADMIN_PASSWORD"] = "dummy"

# Add backend directory to sys.path

sys.path.insert(0, os.path.abspath("backend"))

from fastapi.testclient import TestClient
from server import app
from providers import CloudflareStreamProvider
from routers.cloudflare_stream import verify_cloudflare_signature


client = TestClient(app)

def test_signature_verification():
    secret = "test_webhook_secret"
    body_bytes = b'{"uid": "test_uid", "status": {"state": "ready"}}'
    timestamp = "123456"
    
    to_sign = f"{timestamp}.".encode("utf-8") + body_bytes
    sig = hmac.new(secret.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()
    
    header_val = f"time={timestamp},sig1={sig}"
    
    # Assert signature passes validation
    assert verify_cloudflare_signature(body_bytes, header_val, secret) is True
    
    # Assert wrong signature fails
    assert verify_cloudflare_signature(body_bytes, header_val + "wrong", secret) is False

@pytest.mark.asyncio
async def test_cloudflare_stream_provider_creation(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account_id")
    monkeypatch.setenv("CLOUDFLARE_STREAM_API_TOKEN", "api_token")
    
    provider = CloudflareStreamProvider()
    
    # Mock network call to Cloudflare Copy API
    class MockResponse:
        status_code = 200
        def json(self):
            return {"result": {"uid": "mock_uid"}}
            
    async def mock_post(*args, **kwargs):
        return MockResponse()
        
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    
    res = await provider.create_processing_job(
        parent_id="parent_id",
        media_id="media_id",
        category="intro_video",
        scope="submission",
        r2_url="https://r2.com/video.mp4",
        folder="folder",
        public_id="public_id"
    )
    
    assert res["ok"] is True
    assert res["provider_data"]["uid"] == "mock_uid"
