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


@pytest.mark.asyncio
async def test_create_processing_job_includes_exact_r2_key_in_meta(monkeypatch):
    """P2 follow-up: the webhook can only update the correct asset_metadata
    row if it knows the EXACT object key this specific job was for — a
    category folder can now hold more than one raw object (one per
    intro_video upload attempt)."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account_id")
    monkeypatch.setenv("CLOUDFLARE_STREAM_API_TOKEN", "api_token")

    provider = CloudflareStreamProvider()
    captured = {}

    class MockResponse:
        status_code = 200
        def json(self):
            return {"result": {"uid": "mock_uid"}}

    async def mock_post(self, url, json=None, **kwargs):
        captured["json"] = json
        return MockResponse()

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    await provider.create_processing_job(
        parent_id="app123",
        media_id="media_id",
        category="intro_video",
        scope="application",
        r2_url="https://r2.com/video.mp4",
        folder="folder",
        public_id="intro_video_a1b2c3d4",
    )

    assert captured["json"]["meta"]["r2_key"] == (
        "raw-uploads/applications/app123/intro_video/intro_video_a1b2c3d4.mp4"
    )


class _FakeAssetMetadataCollection:
    """Records update_one/update_many calls without touching real Mongo."""
    def __init__(self, docs):
        self.docs = {d["public_id"]: d for d in docs}
        self.one_calls = []
        self.many_calls = []

    async def update_one(self, filt, update):
        self.one_calls.append((filt, update))
        pid = filt.get("public_id")
        if pid in self.docs:
            self.docs[pid].update(update.get("$set", {}))

    async def update_many(self, filt, update):
        self.many_calls.append((filt, update))
        import re as _re
        pattern = filt["public_id"]["$regex"]
        statuses = filt.get("upload_status", {}).get("$in", [])
        for pid, doc in self.docs.items():
            if _re.match(pattern, pid) and doc.get("upload_status") in statuses:
                doc.update(update.get("$set", {}))


class _FakeParentCollection:
    async def update_one(self, *a, **k):
        return None
    async def find_one(self, *a, **k):
        return None


class _FakeDB:
    def __init__(self, asset_docs):
        self.asset_metadata = _FakeAssetMetadataCollection(asset_docs)
        self.applications = _FakeParentCollection()
        self.submissions = _FakeParentCollection()


def _ready_payload(uid, meta):
    return {"uid": uid, "status": {"state": "ready"}, "duration": 5, "size": 1000, "meta": meta}


@pytest.mark.asyncio
async def test_webhook_ready_updates_only_the_exact_asset_metadata_row(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_STREAM_WEBHOOK_SECRET", raising=False)
    """Two intro_video attempts share one category folder (P2 fix). The
    'ready' webhook for video A's uid must update ONLY video A's row —
    not video B's, which may still be genuinely in-flight."""
    import routers.cloudflare_stream as cfs

    key_a = "raw-uploads/applications/app1/intro_video/intro_video_aaaaaaaa.mp4"
    key_b = "raw-uploads/applications/app1/intro_video/intro_video_bbbbbbbb.mp4"
    fake_db = _FakeDB([
        {"public_id": key_a, "upload_status": "pending"},
        {"public_id": key_b, "upload_status": "pending"},
    ])
    monkeypatch.setattr(cfs, "db", fake_db)

    payload = _ready_payload("uid-for-a", {
        "media_id": "media_a", "parent_id": "app1", "scope": "application",
        "category": "intro_video", "r2_key": key_a,
    })
    resp = client.post("/public/webhooks/cloudflare-stream", json=payload)
    assert resp.status_code == 200

    assert fake_db.asset_metadata.docs[key_a]["upload_status"] == "completed"
    assert fake_db.asset_metadata.docs[key_a]["stream_uid"] == "uid-for-a"
    # The regression this closes: video B must be untouched, not silently
    # stamped with video A's uid via a prefix match.
    assert fake_db.asset_metadata.docs[key_b]["upload_status"] == "pending"
    assert "stream_uid" not in fake_db.asset_metadata.docs[key_b]
    assert fake_db.asset_metadata.many_calls == [], "must use the exact-key path, not the prefix regex"


@pytest.mark.asyncio
async def test_webhook_error_updates_only_the_exact_asset_metadata_row(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_STREAM_WEBHOOK_SECRET", raising=False)
    import routers.cloudflare_stream as cfs

    key_a = "raw-uploads/applications/app1/intro_video/intro_video_aaaaaaaa.mp4"
    key_b = "raw-uploads/applications/app1/intro_video/intro_video_bbbbbbbb.mp4"
    fake_db = _FakeDB([
        {"public_id": key_a, "upload_status": "processing"},
        {"public_id": key_b, "upload_status": "processing"},
    ])
    monkeypatch.setattr(cfs, "db", fake_db)

    payload = {
        "uid": "uid-for-a", "status": {"state": "error", "errorReason": "boom"},
        "meta": {"media_id": "media_a", "parent_id": "app1", "scope": "application",
                 "category": "intro_video", "r2_key": key_a},
    }
    resp = client.post("/public/webhooks/cloudflare-stream", json=payload)
    assert resp.status_code == 200

    assert fake_db.asset_metadata.docs[key_a]["upload_status"] == "failed"
    assert fake_db.asset_metadata.docs[key_b]["upload_status"] == "processing"
    assert fake_db.asset_metadata.many_calls == []


@pytest.mark.asyncio
async def test_webhook_falls_back_to_prefix_match_when_r2_key_absent(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_STREAM_WEBHOOK_SECRET", raising=False)
    """Backward compat: a job already in-flight at deploy time has no r2_key
    in its meta. It must still resolve via the old prefix-regex path rather
    than silently updating nothing."""
    import routers.cloudflare_stream as cfs

    key_a = "raw-uploads/applications/app1/intro_video/intro_video.mp4"
    fake_db = _FakeDB([{"public_id": key_a, "upload_status": "pending"}])
    monkeypatch.setattr(cfs, "db", fake_db)

    payload = _ready_payload("uid-legacy", {
        "media_id": "media_a", "parent_id": "app1", "scope": "application",
        "category": "intro_video",
        # no "r2_key" — simulates a job queued before this field existed
    })
    resp = client.post("/public/webhooks/cloudflare-stream", json=payload)
    assert resp.status_code == 200

    assert fake_db.asset_metadata.docs[key_a]["upload_status"] == "completed"
    assert fake_db.asset_metadata.docs[key_a]["stream_uid"] == "uid-legacy"
    assert len(fake_db.asset_metadata.many_calls) == 1
