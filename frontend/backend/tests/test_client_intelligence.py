"""Tests for the Client Viewing Intelligence System (M5).

Coverage:
  - link.subject_added_at is initialised on create + preserved on update
  - identify rotates prev_visit_at <- last_visit_at and persists client_state
  - POST /public/links/{slug}/seen idempotently $addToSet's talent_id
  - GET /public/links/{slug} surfaces client_state + subject_added_at
"""
import os
import time

import pytest
import requests

BASE = os.environ.get("PYTEST_API_BASE", "http://localhost:8001/api")
ADMIN_EMAIL = "admin@talentgram.com"
ADMIN_PASS = "Admin@123"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def two_talents(admin_h):
    out = []
    for nm in ("CIS Alpha", "CIS Beta"):
        r = requests.post(
            f"{BASE}/talents",
            headers=admin_h,
            json={"name": nm, "location": "Mumbai"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        out.append(r.json())
    yield out
    for t in out:
        requests.delete(f"{BASE}/talents/{t['id']}", headers=admin_h, timeout=10)


@pytest.fixture
def link_with_two(admin_h, two_talents):
    r = requests.post(
        f"{BASE}/links",
        headers=admin_h,
        json={
            "title": f"Talentgram x CIS-{int(time.time())}",
            "talent_ids": [t["id"] for t in two_talents],
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    link = r.json()
    yield link
    requests.delete(f"{BASE}/links/{link['id']}", headers=admin_h, timeout=10)


def _identify(slug, email="client@example.com", name="Client Test"):
    r = requests.post(
        f"{BASE}/public/links/{slug}/identify",
        json={"email": email, "name": name},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_subject_added_at_initialised_on_create(link_with_two, admin_h):
    r = requests.get(
        f"{BASE}/links/{link_with_two['id']}",
        headers=admin_h,
        timeout=10,
    )
    assert r.status_code == 200
    saa = r.json().get("subject_added_at") or {}
    # Both talent ids should be timestamped.
    for tid in r.json()["talent_ids"]:
        assert tid in saa


def test_subject_added_at_preserved_on_update(
    link_with_two, two_talents, admin_h
):
    """Adding a third talent stamps it `now`; existing two keep their times."""
    # Capture original timestamps
    orig = requests.get(
        f"{BASE}/links/{link_with_two['id']}", headers=admin_h, timeout=10
    ).json()
    orig_saa = orig["subject_added_at"]

    # Create a third talent + add to link
    r = requests.post(
        f"{BASE}/talents",
        headers=admin_h,
        json={"name": "CIS Gamma", "location": "Delhi"},
        timeout=10,
    )
    third = r.json()
    try:
        time.sleep(1.05)  # ensure clock advances
        upd = requests.put(
            f"{BASE}/links/{link_with_two['id']}",
            headers=admin_h,
            json={
                "title": orig["title"],
                "talent_ids": orig["talent_ids"] + [third["id"]],
                "submission_ids": [],
                "visibility": orig.get("visibility") or {},
                "is_public": True,
            },
            timeout=10,
        )
        assert upd.status_code == 200, upd.text
        new_saa = (
            requests.get(
                f"{BASE}/links/{link_with_two['id']}",
                headers=admin_h,
                timeout=10,
            )
            .json()["subject_added_at"]
        )
        # Existing entries preserved verbatim
        for tid, ts in orig_saa.items():
            assert new_saa.get(tid) == ts
        # New one stamped LATER than originals
        new_ts = new_saa[third["id"]]
        assert new_ts > max(orig_saa.values())
    finally:
        requests.delete(f"{BASE}/talents/{third['id']}", headers=admin_h, timeout=10)


def test_identify_rotates_visit_timestamps(link_with_two):
    slug = link_with_two["slug"]
    email = f"cis_visit_{int(time.time())}@x.com"
    # First identify — prev_visit_at should be None
    t1 = _identify(slug, email)
    r = requests.get(
        f"{BASE}/public/links/{slug}",
        headers={"Authorization": f"Bearer {t1}"},
        timeout=10,
    )
    assert r.status_code == 200
    cs1 = r.json()["client_state"]
    assert cs1["prev_visit_at"] is None
    assert cs1["last_visit_at"] is not None

    time.sleep(1.05)

    # Second identify — prev_visit_at should now equal the previous last_visit_at
    t2 = _identify(slug, email)
    r2 = requests.get(
        f"{BASE}/public/links/{slug}",
        headers={"Authorization": f"Bearer {t2}"},
        timeout=10,
    )
    cs2 = r2.json()["client_state"]
    assert cs2["prev_visit_at"] == cs1["last_visit_at"]
    assert cs2["last_visit_at"] >= cs1["last_visit_at"]


def test_seen_endpoint_is_idempotent(link_with_two, two_talents):
    slug = link_with_two["slug"]
    tok = _identify(slug, f"cis_seen_{int(time.time())}@x.com")
    h = {"Authorization": f"Bearer {tok}"}
    tid = two_talents[0]["id"]

    # POST seen twice
    for _ in range(2):
        r = requests.post(
            f"{BASE}/public/links/{slug}/seen",
            headers=h,
            json={"talent_id": tid},
            timeout=10,
        )
        assert r.status_code == 200

    # State should contain it ONCE
    r2 = requests.get(f"{BASE}/public/links/{slug}", headers=h, timeout=10)
    seen = r2.json()["client_state"]["seen_talent_ids"]
    assert seen.count(tid) == 1
    assert tid in seen


def test_seen_requires_viewer_token(link_with_two, two_talents):
    slug = link_with_two["slug"]
    r = requests.post(
        f"{BASE}/public/links/{slug}/seen",
        json={"talent_id": two_talents[0]["id"]},
        timeout=10,
    )
    assert r.status_code == 401


def test_subject_added_at_in_public_payload(link_with_two, two_talents):
    slug = link_with_two["slug"]
    tok = _identify(slug, f"cis_payload_{int(time.time())}@x.com")
    r = requests.get(
        f"{BASE}/public/links/{slug}",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=10,
    )
    assert r.status_code == 200
    saa = r.json().get("subject_added_at") or {}
    for t in two_talents:
        assert t["id"] in saa
