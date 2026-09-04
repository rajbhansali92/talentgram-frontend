"""Live-API integration test: submission_finalize's mandatory-skills check
must accept legacy/abbreviated submission_requirements.skills keys (e.g.
"sports") as aliases of the current full category name ("Sports & Fitness"),
matching what requirementEngine.js on the frontend already does via
lowercasing. Before the 2026-09 fix, an older project still configured with
one of these legacy keys would reject every submission for that category,
no matter what the talent selected, because SKILLS_CATEGORIES.get(cat)
returned nothing for an unrecognized key.

Runs against a live local backend + local Mongo (same convention as
test_competitive_brand_required.py).
"""
import os
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
def legacy_skill_project(db):
    """A minimal strict project whose mandatory skill category is configured
    with the legacy abbreviated key "sports" instead of the current full
    name "Sports & Fitness" — isolates the alias-resolution fix from every
    other required-field gate."""
    slug = f"legacy-skill-test-{uuid.uuid4().hex[:10]}"
    doc = {
        "id": str(uuid.uuid4()),
        "slug": slug,
        "title": "Legacy Skill Category Alias Test",
        "status": "ongoing",
        "submission_requirements": {
            "strictness": "strict",
            "fields": {
                "name": "optional",
                "email": "optional",
            },
            "portfolio": {},
            "skills": {"sports": True},
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
            "name": "Legacy Skill Tester",
            "email": email,
            "form_data": {"first_name": "Legacy", "last_name": "Tester", "location": []},
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


def test_legacy_key_rejects_when_no_qualifying_skill(legacy_skill_project):
    email = f"legacy-empty-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(legacy_skill_project["slug"], email)
    _patch_form(created["id"], created["token"], {"skills": []})
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 400
    assert "sports" in resp.json().get("detail", "").lower()


def test_legacy_key_accepts_skill_from_the_aliased_current_category(legacy_skill_project):
    email = f"legacy-filled-{uuid.uuid4().hex[:8]}@example.com"
    created = _create_submission(legacy_skill_project["slug"], email)
    # "Cycling" belongs to the current "Sports & Fitness" list — the legacy
    # "sports" key must resolve to it, not be treated as an unknown category.
    _patch_form(created["id"], created["token"], {"skills": ["Cycling"]})
    resp = _finalize(created["id"], created["token"])
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") in {"submitted", "updated"}
