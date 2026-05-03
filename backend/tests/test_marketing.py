"""Backend tests for Marketing CRM (/api/marketing).

Covers:
 - Auth guards (401 without admin token) on all four endpoints
 - Client CRUD + persistence via GET
 - Interaction logging + last_contacted_date bump + re-sort
 - 400 for invalid ObjectId, 404 for non-existent client
"""
import os
from pathlib import Path

import pytest
import requests
from bson import ObjectId


def _load_url() -> str:
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if url:
        return url.rstrip("/")
    env_file = Path("/app/frontend/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not configured")


BASE_URL = _load_url()

ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASSWORD = "Admin@123"


# ----- Fixtures -----------------------------------------------------------
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("token") or data.get("access_token")
    assert token, f"no token in response: {data}"
    return token


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ----- Auth guard tests ---------------------------------------------------
class TestMarketingAuthGuards:
    def test_list_clients_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/marketing/clients", timeout=10)
        assert r.status_code in (401, 403), r.text

    def test_create_client_requires_auth(self):
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_NoAuth"},
            timeout=10,
        )
        assert r.status_code in (401, 403), r.text

    def test_get_client_requires_auth(self):
        r = requests.get(
            f"{BASE_URL}/api/marketing/clients/{ObjectId()}", timeout=10
        )
        assert r.status_code in (401, 403), r.text

    def test_create_interaction_requires_auth(self):
        r = requests.post(
            f"{BASE_URL}/api/marketing/interactions",
            json={"client_id": str(ObjectId()), "type": "call"},
            timeout=10,
        )
        assert r.status_code in (401, 403), r.text

    def test_list_interactions_requires_auth(self):
        r = requests.get(
            f"{BASE_URL}/api/marketing/interactions/{ObjectId()}", timeout=10
        )
        assert r.status_code in (401, 403), r.text


# ----- Happy path + CRUD --------------------------------------------------
class TestMarketingClientsCRUD:
    created_ids = []

    def test_list_clients_with_auth(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/marketing/clients", headers=auth_headers, timeout=15
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_create_client_and_persist(self, auth_headers):
        payload = {
            "name": "TEST_Marketing Client",
            "company_name": "TEST_Corp",
            "phone_number": "+15551234567",
        }
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json=payload,
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["name"] == payload["name"]
        assert data["company_name"] == payload["company_name"]
        assert data["phone_number"] == payload["phone_number"]
        assert "id" in data and len(data["id"]) == 24
        assert data.get("last_contacted_date")
        TestMarketingClientsCRUD.created_ids.append(data["id"])

        # GET-by-id persistence
        rid = data["id"]
        g = requests.get(
            f"{BASE_URL}/api/marketing/clients/{rid}",
            headers=auth_headers,
            timeout=15,
        )
        assert g.status_code == 200, g.text
        gd = g.json()
        assert gd["id"] == rid
        assert gd["name"] == payload["name"]

        # row present in list
        lst = requests.get(
            f"{BASE_URL}/api/marketing/clients",
            headers=auth_headers,
            timeout=15,
        ).json()
        assert any(c["id"] == rid for c in lst)

    def test_create_client_requires_name(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"company_name": "NoName"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code in (400, 422), r.text

    def test_get_client_bad_objectid(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/marketing/clients/not-an-oid",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400, r.text

    def test_get_client_nonexistent(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/marketing/clients/{ObjectId()}",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 404, r.text


# ----- Interactions -------------------------------------------------------
class TestMarketingInteractions:
    def test_create_interaction_bad_objectid(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/interactions",
            json={"client_id": "bad-oid", "type": "call"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400, r.text

    def test_create_interaction_nonexistent_client(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/interactions",
            json={"client_id": str(ObjectId()), "type": "call"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 404, r.text

    def test_full_interaction_flow(self, auth_headers):
        # Create a client
        c = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_Interaction Client"},
            headers=auth_headers,
            timeout=15,
        ).json()
        cid = c["id"]
        orig_last = c["last_contacted_date"]

        # Log interaction
        import time
        time.sleep(1.1)
        ir = requests.post(
            f"{BASE_URL}/api/marketing/interactions",
            json={"client_id": cid, "type": "email", "notes": "TEST_Sent intro"},
            headers=auth_headers,
            timeout=15,
        )
        assert ir.status_code == 201, ir.text
        idata = ir.json()
        assert idata["client_id"] == cid
        assert idata["type"] == "email"
        assert idata["notes"] == "TEST_Sent intro"

        # list interactions — new one present
        lst = requests.get(
            f"{BASE_URL}/api/marketing/interactions/{cid}",
            headers=auth_headers,
            timeout=15,
        )
        assert lst.status_code == 200
        items = lst.json()
        assert any(i["id"] == idata["id"] for i in items)

        # last_contacted_date bumped
        g = requests.get(
            f"{BASE_URL}/api/marketing/clients/{cid}",
            headers=auth_headers,
            timeout=15,
        ).json()
        assert g["last_contacted_date"] > orig_last

        # client now at/near top of list (sorted desc by last_contacted_date)
        full = requests.get(
            f"{BASE_URL}/api/marketing/clients",
            headers=auth_headers,
            timeout=15,
        ).json()
        assert full[0]["id"] == cid, (
            f"expected {cid} at top after interaction, got {full[0]['id']}"
        )
