"""Phase 1: Talent Identity Auto-fill — /api/public/prefill endpoint tests.

Covers:
- Known email returns safe-only fields (no media/bio/gender/work_links)
- Unknown email returns {} (200)
- Malformed email returns {} (200, no crash)
- Rate limit: 21st req/min/IP → 429
"""
import os
import time

import pytest
import requests
from pathlib import Path


def _load_frontend_env_url() -> str:
    env_path = Path("/app/frontend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("REACT_APP_BACKEND_URL not found in /app/frontend/.env")


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _load_frontend_env_url()).rstrip("/")
PREFILL_URL = f"{BASE_URL}/api/public/prefill"

KNOWN_EMAIL = "test_afd08d@ex.com"
UNKNOWN_EMAIL = "does-not-exist@ex.com"
MALFORMED_EMAIL = "notanemail"

# Disallow-list — these MUST NEVER appear in the prefill response
FORBIDDEN_KEYS = {
    "intro_video", "video", "video_url", "takes", "images",
    "media", "bio", "gender", "ethnicity", "work_links",
    "_id", "id", "submission_id", "email",
}

ALLOWED_KEYS = {
    "first_name", "last_name", "age", "dob", "height",
    "phone", "location", "instagram_handle", "instagram_followers",
}


@pytest.fixture
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


class TestPrefillEndpoint:
    def test_known_email_returns_safe_fields(self, client):
        # Use a unique IP marker via X-Forwarded-For so this test doesn't
        # consume the rate-limit budget for the rate-limit test below.
        r = client.get(PREFILL_URL, params={"email": KNOWN_EMAIL},
                       headers={"X-Forwarded-For": "10.20.0.1"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, dict)
        assert data, "Expected non-empty payload for seeded talent"

        # Known seed values per handoff: first='A', last='B', height=5'8", location=Mumbai
        assert data.get("first_name") == "A", f"first_name: {data.get('first_name')}"
        assert data.get("last_name") == "B", f"last_name: {data.get('last_name')}"
        assert data.get("location") == "Mumbai", f"location: {data.get('location')}"
        # Height stored as 5'8" — accept either string variant
        assert "5" in str(data.get("height") or ""), f"height: {data.get('height')}"

        # All keys must be from the allowlist
        unexpected = set(data.keys()) - ALLOWED_KEYS
        assert not unexpected, f"Unexpected keys leaked: {unexpected}"

        # Forbidden keys must NOT be present
        leaked = set(data.keys()) & FORBIDDEN_KEYS
        assert not leaked, f"Forbidden keys leaked: {leaked}"

    def test_unknown_email_returns_empty(self, client):
        r = client.get(PREFILL_URL, params={"email": UNKNOWN_EMAIL},
                       headers={"X-Forwarded-For": "10.20.0.2"})
        assert r.status_code == 200, r.text
        assert r.json() == {}

    def test_malformed_email_returns_empty(self, client):
        r = client.get(PREFILL_URL, params={"email": MALFORMED_EMAIL},
                       headers={"X-Forwarded-For": "10.20.0.3"})
        assert r.status_code == 200, r.text
        assert r.json() == {}

    def test_empty_email_returns_empty(self, client):
        r = client.get(PREFILL_URL, params={"email": ""},
                       headers={"X-Forwarded-For": "10.20.0.4"})
        assert r.status_code == 200, r.text
        assert r.json() == {}

    def test_rate_limit_429_after_20(self, client):
        """21st rapid request from same IP should return 429."""
        # Use a dedicated IP to avoid interference
        ip = f"10.30.{int(time.time()) % 250}.99"
        headers = {"X-Forwarded-For": ip}
        statuses = []
        for i in range(21):
            r = client.get(PREFILL_URL,
                           params={"email": f"probe{i}@ex.com"},
                           headers=headers)
            statuses.append(r.status_code)
        # First 20 should be 200; at least one (the 21st) should be 429
        assert statuses[:20].count(200) == 20, f"first 20 statuses: {statuses[:20]}"
        assert statuses[20] == 429, f"21st status: {statuses[20]} (all: {statuses})"

    def test_no_email_param_handled(self, client):
        """Missing email query param → 422 (FastAPI validation)."""
        r = client.get(PREFILL_URL,
                       headers={"X-Forwarded-For": "10.20.0.5"})
        assert r.status_code in (400, 422)
