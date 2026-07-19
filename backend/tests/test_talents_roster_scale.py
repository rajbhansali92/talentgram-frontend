"""Reproduces and verifies the fix for a silent-truncation bug in GET /talents.

Root cause: routers/talents.py's unpaginated branch (hit when a caller sends
neither `page` nor `limit` — as frontend/src/pages-components/LinkGenerator.jsx:128
and frontend/src/components/pipeline/TalentBrowserModal.jsx:362 both do, by
design, to load the whole roster for client-side filtering) hard-capped the
result at 2000 docs via `cursor.to_list(2000)`. Any roster past 2000 active
talents silently lost its oldest entries from both the Client Link Generator's
talent picker and the Casting Pipeline's Quick-Add browser, with no error and
no UI indication — directly contradicting TalentBrowserModal's own comment
that it's tuned for "extreme 5,000+ performance".
"""
import os
import sys
from pathlib import Path

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

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeCursor:
    """Mimics Motor's AsyncIOMotorCursor.to_list(length) truncation semantics:
    a finite `length` caps the results; `length=None` returns everything."""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs[:length])


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        return FakeCursor(self.docs)

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return d
        return None

    async def count_documents(self, query=None):
        return len(self.docs)


class FakeDB:
    def __init__(self, talent_count):
        self.users = FakeColl([{"id": "adm-1", "email": "admin@talentgram.co", "role": "admin"}])
        self.sessions = FakeColl([])
        self.talents = FakeColl([
            {
                "id": f"t-{i}",
                "name": f"Talent {i}",
                "email": f"talent{i}@example.com",
                "status": "ACTIVE",
                "created_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00",
            }
            for i in range(talent_count)
        ])


def _client_with_roster(talent_count):
    mock_db = FakeDB(talent_count)
    import core
    core.db = mock_db
    from routers import talents as talents_router
    talents_router.db = mock_db

    from fastapi.testclient import TestClient
    from server import app
    from core import make_token

    client = TestClient(app)
    token = make_token({"id": "adm-1", "role": "admin", "email": "admin@talentgram.co"}, days=1)
    return client, {"Authorization": f"Bearer {token}"}


def test_unpaginated_roster_fetch_returns_full_roster_past_2000():
    """GET /talents with no page/limit (the LinkGenerator / TalentBrowserModal
    call pattern) must return the entire matching roster, not silently cap at
    2000 and drop the rest."""
    talent_count = 2500
    client, headers = _client_with_roster(talent_count)

    resp = client.get("/api/talents", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == talent_count, (
        f"Expected all {talent_count} talents, got {len(data)} — "
        "the unbounded /talents branch is silently truncating the roster."
    )


def test_unpaginated_roster_fetch_small_roster_unaffected():
    """Sanity check: rosters under the old 2000 cap behave the same as before."""
    talent_count = 50
    client, headers = _client_with_roster(talent_count)

    resp = client.get("/api/talents", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == talent_count
