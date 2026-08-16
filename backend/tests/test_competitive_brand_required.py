"""Live-API integration tests: Competitive Brand "required" validation must
treat an explicit NONE answer (has_competitive_brand_experience=False) as a
complete, valid response — not as a missing required field just because the
free-text `competitive_brand` string is empty. Only an explicit YES with an
empty text response should still block finalize.

Runs against a live local backend + local Mongo (both already required by
several other test files in this suite — see test_apply_location_merge.py /
test_portal_lookup.py for the same MONGO_URL convention). Uses
REACT_APP_BACKEND_URL when set (CI/staging), otherwise localhost:8000.
"""
import os
import time
import uuid

import pytest
import requests
import pymongo

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "talentgram")


@pytest.fixture(scope="module")
def db():
    client = pymongo.MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture()
def project(db):
    """A minimal strict project where ONLY competitive_brand is required —
    isolates the assertion from every other required-field gate."""
    slug = f"cb-required-test-{uuid.uuid4().hex[:10]}"
    doc = {
        "id": str(uuid.uuid4()),
        "slug": slug,
        "title": "Competitive Brand Required Test",
        "status": "ongoing",
        "competitive_brand_enabled": True,
        "submission_requirements": {
            "strictness": "strict",
            "fields": {
                "name": "optional",
                "email": "optional",
                "competitive_brand": "required",
            },
            "portfolio": {},
            "skills": {},
            "conditional_rules": [],
        },
    }
    db.projects.insert_one(doc)
    yield doc
    db.projects.delete_one({"id": doc["id"]})
    db.submissions.delete_many({"project_id": doc["id"]})


def _create_submission(slug, email):
    r = requests.post(
        f"{API}/public/projects/{slug}/submission",
        json={
            "name": "CB Tester",
            "email": email,
            "form_data": {"first_name": "CB", "last_name": "Tester", "location": []},
        },
        timeout=30,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _patch_form(sid, token, form_data):
    r = requests.put(
        f"{API}/public/submissions/{sid}",
        json={"form_data": form_data},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _finalize(sid, token):
    return requests.post(
        f"{API}/public/submissions/{sid}/finalize",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def test_none_answer_satisfies_required_competitive_brand(project):
    email = f"cb-none-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(project["slug"], email)
    _patch_form(created["id"], created["token"], {
        "has_competitive_brand_experience": False,
        "competitive_brand": "",
    })
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") in {"submitted", "updated"}


def test_yes_answer_with_empty_text_still_blocks(project):
    email = f"cb-yes-empty-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(project["slug"], email)
    _patch_form(created["id"], created["token"], {
        "has_competitive_brand_experience": True,
        "competitive_brand": "",
    })
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 400
    assert "Competitive Brand" in resp.json().get("detail", "")


def test_yes_answer_with_text_satisfies_required(project):
    email = f"cb-yes-filled-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(project["slug"], email)
    _patch_form(created["id"], created["token"], {
        "has_competitive_brand_experience": True,
        "competitive_brand": "Brand A — 2025; Brand B — March 2026",
    })
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 200, resp.text


def test_unanswered_still_blocks(project):
    email = f"cb-unanswered-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(project["slug"], email)
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 400
    assert "Competitive Brand" in resp.json().get("detail", "")
