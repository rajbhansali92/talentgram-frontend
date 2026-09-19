"""Backend tests for the CRM talent-industry adaptation (2026-09-19):
Company directory + Contact Type lookup lists, and the new company_id/
designation/notes fields on clients.

Focused regression coverage for the changed behavior only — the existing
CRUD/auth/interaction/bulk-action surface is already covered by
test_marketing.py and test_marketing_unit.py and is untouched here.
"""
import os
import uuid
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

from _fixtures import ADMIN_EMAIL, ADMIN_PASSWORD


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json().get("token")
    assert token
    return token


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def team_token(admin_token):
    """A throwaway team user, self-contained (doesn't depend on any
    pre-seeded fixture account existing in the target database)."""
    email = f"TEST_crm_team_{uuid.uuid4().hex[:8]}@example.com"
    r = requests.post(
        f"{BASE_URL}/api/users/invite",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "TEST CRM Team", "email": email, "role": "team"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    invite_token = r.json()["invite_token"]
    uid = r.json()["user"]["id"]
    pw = "CrmTeam@2026"
    r = requests.post(
        f"{BASE_URL}/api/public/signup/complete",
        json={"token": invite_token, "password": pw},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    r = requests.post(
        f"{BASE_URL}/api/auth/login", json={"email": email, "password": pw}, timeout=15
    )
    assert r.status_code == 200, r.text
    yield r.json()["token"]
    requests.delete(
        f"{BASE_URL}/api/users/{uid}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=15,
    )


# ---------------------------------------------------------------------------
class TestExistingClientsUnaffected:
    def test_existing_clients_still_load(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/marketing/clients", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        items = r.json()
        assert isinstance(items, list)
        # Every item — old or new — must carry the new fields without crashing,
        # even when the underlying doc predates this change.
        for c in items:
            assert "company_id" in c
            assert "designation" in c
            assert "notes" in c


class TestCompanyDirectory:
    def test_admin_can_add_company_and_it_appears_in_list(self, auth_headers):
        name = f"TEST_CRM Company {uuid.uuid4().hex[:8]}"
        r = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": name}, headers=auth_headers, timeout=15)
        assert r.status_code == 201, r.text
        company = r.json()
        assert company["name"] == name
        assert company["active"] is True

        lst = requests.get(f"{BASE_URL}/api/marketing/companies", headers=auth_headers, timeout=15).json()
        assert any(c["id"] == company["id"] for c in lst)

    def test_creating_same_company_twice_does_not_duplicate(self, auth_headers):
        name = f"TEST_CRM Dedup {uuid.uuid4().hex[:8]}"
        first = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": name}, headers=auth_headers, timeout=15).json()
        # Different casing/whitespace — must resolve to the SAME entry.
        second = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": f"  {name.upper()}  "}, headers=auth_headers, timeout=15).json()
        assert first["id"] == second["id"]

    def test_team_member_cannot_add_company(self, team_token):
        r = requests.post(
            f"{BASE_URL}/api/marketing/companies",
            json={"name": "TEST_Should Not Be Created"},
            headers={"Authorization": f"Bearer {team_token}"},
            timeout=15,
        )
        assert r.status_code == 403, r.text

    def test_team_member_can_list_companies(self, team_token):
        r = requests.get(
            f"{BASE_URL}/api/marketing/companies",
            headers={"Authorization": f"Bearer {team_token}"},
            timeout=15,
        )
        assert r.status_code == 200, r.text

    def test_deactivate_and_reactivate_company(self, auth_headers):
        name = f"TEST_CRM Toggle {uuid.uuid4().hex[:8]}"
        company = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": name}, headers=auth_headers, timeout=15).json()
        cid = company["id"]

        r = requests.post(f"{BASE_URL}/api/marketing/companies/{cid}/deactivate", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        active_only = requests.get(f"{BASE_URL}/api/marketing/companies", headers=auth_headers, timeout=15).json()
        assert not any(c["id"] == cid for c in active_only)
        with_inactive = requests.get(f"{BASE_URL}/api/marketing/companies", params={"include_inactive": True}, headers=auth_headers, timeout=15).json()
        assert any(c["id"] == cid and c["active"] is False for c in with_inactive)

        r = requests.post(f"{BASE_URL}/api/marketing/companies/{cid}/reactivate", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        active_only = requests.get(f"{BASE_URL}/api/marketing/companies", headers=auth_headers, timeout=15).json()
        assert any(c["id"] == cid for c in active_only)


class TestContactTypeLookup:
    def test_seeded_defaults_present(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/marketing/contact-types", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        values = {t["value"] for t in r.json()}
        # The 15 defaults the WhatsApp CRM agent's ROLE_LABEL_TO_SLUG relies on.
        assert {"producer", "casting_director", "production_house", "talent_agency"} <= values

    def test_admin_can_add_contact_type_and_it_appears_in_list(self, auth_headers):
        label = f"TEST_CRM Role {uuid.uuid4().hex[:8]}"
        r = requests.post(f"{BASE_URL}/api/marketing/contact-types", json={"label": label}, headers=auth_headers, timeout=15)
        assert r.status_code == 201, r.text
        ct = r.json()
        assert ct["label"] == label
        assert ct["value"]  # slug auto-generated

        lst = requests.get(f"{BASE_URL}/api/marketing/contact-types", headers=auth_headers, timeout=15).json()
        assert any(t["id"] == ct["id"] for t in lst)

    def test_team_member_cannot_add_contact_type(self, team_token):
        r = requests.post(
            f"{BASE_URL}/api/marketing/contact-types",
            json={"label": "TEST_Should Not Be Created"},
            headers={"Authorization": f"Bearer {team_token}"},
            timeout=15,
        )
        assert r.status_code == 403, r.text

    def test_rename_keeps_slug_stable(self, auth_headers):
        label = f"TEST_CRM Rename {uuid.uuid4().hex[:8]}"
        ct = requests.post(f"{BASE_URL}/api/marketing/contact-types", json={"label": label}, headers=auth_headers, timeout=15).json()
        original_value = ct["value"]
        renamed = requests.put(
            f"{BASE_URL}/api/marketing/contact-types/{ct['id']}",
            json={"label": f"{label} Renamed"},
            headers=auth_headers,
            timeout=15,
        ).json()
        assert renamed["value"] == original_value
        assert renamed["label"] == f"{label} Renamed"


class TestClientCompanyAndTypeIntegration:
    def test_create_client_with_company_id_syncs_company_name(self, auth_headers):
        company = requests.post(
            f"{BASE_URL}/api/marketing/companies",
            json={"name": f"TEST_CRM Client Co {uuid.uuid4().hex[:8]}"},
            headers=auth_headers,
            timeout=15,
        ).json()
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client A", "company_id": company["id"], "designation": "Line Producer"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 201, r.text
        client = r.json()
        assert client["company_id"] == company["id"]
        assert client["company_name"] == company["name"]
        assert client["designation"] == "Line Producer"

    def test_create_client_with_contact_type(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client B", "contact_type": "producer"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 201, r.text
        assert r.json()["contact_type"] == "producer"

    def test_relationship_status_saves_correctly(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client C", "stage": "project_based"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 201, r.text
        assert r.json()["stage"] == "project_based"

    def test_edit_client_to_a_different_company(self, auth_headers):
        co1 = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": f"TEST_CRM Co1 {uuid.uuid4().hex[:8]}"}, headers=auth_headers, timeout=15).json()
        co2 = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": f"TEST_CRM Co2 {uuid.uuid4().hex[:8]}"}, headers=auth_headers, timeout=15).json()
        client = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client D", "company_id": co1["id"]},
            headers=auth_headers,
            timeout=15,
        ).json()
        assert client["company_name"] == co1["name"]

        updated = requests.put(
            f"{BASE_URL}/api/marketing/clients/{client['id']}",
            json={"company_id": co2["id"]},
            headers=auth_headers,
            timeout=15,
        )
        assert updated.status_code == 200, updated.text
        ud = updated.json()
        assert ud["company_id"] == co2["id"]
        assert ud["company_name"] == co2["name"]

    def test_clearing_company_id_with_empty_string(self, auth_headers):
        co = requests.post(f"{BASE_URL}/api/marketing/companies", json={"name": f"TEST_CRM Co3 {uuid.uuid4().hex[:8]}"}, headers=auth_headers, timeout=15).json()
        client = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client E", "company_id": co["id"]},
            headers=auth_headers,
            timeout=15,
        ).json()
        cleared = requests.put(
            f"{BASE_URL}/api/marketing/clients/{client['id']}",
            json={"company_id": ""},
            headers=auth_headers,
            timeout=15,
        ).json()
        assert cleared["company_id"] is None

    def test_invalid_company_id_returns_404_not_500(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/marketing/clients",
            json={"name": "TEST_CRM Client F", "company_id": str(ObjectId())},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 404, r.text
