"""Regression tests for the 2026-07-26 Roster/Pipeline perf sprint.

1. GET /talents (list) must never return the raw media[] array — it was
   measured at 30.8% of a real 40-item page's payload (67KB/218KB) despite
   no list card reading it; image_url/cover_thumbnail_url already carry
   what cards need. Quick View now lazily hydrates media on open instead
   (frontend: TalentBrowserModal.jsx's openPreview, mirroring the pattern
   PipelineCard.jsx already used).

2. GET /projects/{id}/pipeline's talent hydration and submission-aware
   follow_up lookup are independent (both keyed by talent_ids alone) but
   ran as two sequential Mongo round trips — proven to now run
   concurrently via asyncio.gather.
"""
import asyncio
import os
import sys

for k, v in {
    "MONGO_URL": "mongodb://localhost:27017", "DB_NAME": "test", "JWT_SECRET": "d",
    "RESEND_API_KEY": "d", "SENDGRID_API_KEY": "d", "CLOUDINARY_CLOUD_NAME": "d",
    "CLOUDINARY_API_KEY": "d", "CLOUDINARY_API_SECRET": "d",
    "ADMIN_EMAIL": "a@b.co", "ADMIN_PASSWORD": "d",
}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.abspath("backend"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import core
core.db = MagicMock()

from fastapi.testclient import TestClient
from server import app
import routers.talents as talents_module
import routers.casting_pipeline as pipeline_module

client = TestClient(app)


def _admin_headers():
    token = core.make_token({"email": "admin@test.com", "role": "admin", "id": "admin1", "tv": 0}, days=1)
    return {"Authorization": f"Bearer {token}"}


def test_talents_list_never_returns_media():
    """The list projection must exclude media — confirmed independently
    safe: every talent with media already has a denormalized cover_url/
    cover_thumbnail_url, and _enrich_list prefers those first."""
    talent_doc = {
        "id": "t1", "name": "Test Talent", "cover_url": "https://cdn/cover.jpg",
        "cover_thumbnail_url": "https://cdn/thumb.jpg", "media_count": 3,
        "status": "active", "created_at": "2026-01-01T00:00:00Z",
    }
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.skip.return_value = mock_cursor
    mock_cursor.limit.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[talent_doc])

    # current_user is baked into current_team_or_admin's Depends(...) default
    # at import time — patching the module attribute doesn't reach it.
    # dependency_overrides is FastAPI's own mechanism for exactly this: it's
    # keyed by the original callable, so it reaches every nested dependency.
    app.dependency_overrides[core.current_user] = lambda: {
        "email": "admin@test.com", "role": "admin", "id": "admin1",
    }
    try:
        with patch.object(talents_module, "db") as mock_db:
            mock_db.talents.find.return_value = mock_cursor
            mock_db.talents.count_documents = AsyncMock(return_value=1)

            resp = client.get("/api/talents?page=0&size=10", headers=_admin_headers())
    finally:
        app.dependency_overrides.pop(core.current_user, None)

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "media" not in items[0], "list response must not carry the raw media array"
    # The projection actually passed to Mongo must exclude it too — asserting
    # only on the response wouldn't catch a fix that filters it out in Python
    # after already paying the Mongo transfer cost.
    _, kwargs_or_projection = mock_db.talents.find.call_args[0]
    assert kwargs_or_projection.get("media") == 0, "Mongo projection must exclude media"
    # Denormalized fields still flow through correctly.
    assert items[0]["cover_url"] == "https://cdn/cover.jpg"
    assert items[0]["cover_thumbnail_url"] == "https://cdn/thumb.jpg"
    assert items[0]["image_url"] == "https://cdn/thumb.jpg"


@pytest.mark.asyncio
async def test_pipeline_talent_and_submission_lookups_run_concurrently(monkeypatch):
    """Proves the fix: both reads must be in-flight at the same time. A
    sequential implementation would only ever have one in-flight at once."""
    order = []

    class FakeCursor:
        def __init__(self, kind):
            self.kind = kind
        async def to_list(self, _n):
            order.append(f"{self.kind}:start")
            await asyncio.sleep(0.05)
            order.append(f"{self.kind}:end")
            if self.kind == "talents":
                return [{"id": "t1", "name": "Talent One"}]
            return [{"talent_id": "t1"}]

    class FakeCollection:
        def __init__(self, kind):
            self.kind = kind
        def find(self, *a, **k):
            return FakeCursor(self.kind)

    class FakePipelineCursor:
        def sort(self, *a, **k):
            return self
        async def to_list(self, _n):
            return [{"id": "row1", "project_id": "p1", "talent_id": "t1", "stage": "ask_to_test"}]

    class FakeDB:
        def __init__(self):
            self.casting_pipeline = MagicMock()
            self.casting_pipeline.find.return_value = FakePipelineCursor()
            self.talents = FakeCollection("talents")
            self.submissions = FakeCollection("submissions")

    monkeypatch.setattr(pipeline_module, "db", FakeDB())
    monkeypatch.setattr(pipeline_module, "current_team_or_admin", AsyncMock(return_value={"role": "admin"}))

    await pipeline_module.list_pipeline(project_id="p1", _admin={"role": "admin"})

    assert order.index("submissions:start") < order.index("talents:end"), (
        f"lookups ran sequentially, not concurrently: {order}"
    )
