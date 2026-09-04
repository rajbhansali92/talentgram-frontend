"""Live-API integration tests: "Choose from Global Profile" in Submission
Review Center — POST /projects/{pid}/submissions/{sid}/admin-media-from-talent.

Covers the two things that matter most for this feature: (1) it correctly
copies an existing Global Talent media item into the submission's
admin-added media by reference (same public_id, no re-upload, no talent
mutation), and (2) it can never pull in another talent's media even if
asked to — the endpoint derives its own view of "this talent's media"
from the submission, ignoring anything the request itself might imply
about a talent.

Runs against a live local backend + local Mongo (same convention as
test_competitive_brand_required.py / test_skill_category_legacy_alias.py).
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
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "changeme123")


@pytest.fixture(scope="module")
def db():
    client = pymongo.MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def _media_item(category, suffix):
    return {
        "id": str(uuid.uuid4()),
        "category": category,
        "url": f"https://res.cloudinary.com/talentgram/image/upload/zzz_test_{suffix}.jpg",
        "public_id": f"zzz_test_media_from_talent/{suffix}",
        "resource_type": "image",
        "content_type": "image/jpeg",
    }


@pytest.fixture()
def two_talents_one_project(db):
    """Talent A (has media), Talent B (has DIFFERENT media), and a strict
    project with one submission each — isolates the cross-talent-leakage
    assertion from every other concern."""
    project_id = str(uuid.uuid4())
    slug = f"zzz-test-media-from-talent-{uuid.uuid4().hex[:10]}"
    db.projects.insert_one({
        "id": project_id, "slug": slug, "brand_name": "ZZZ_TEST Media From Talent",
        "status": "ongoing", "submission_requirements": {"strictness": "strict", "fields": {}, "skills": {}, "portfolio": {}, "conditional_rules": []},
    })

    talent_a_id = str(uuid.uuid4())
    talent_a_media = [_media_item("indian", "talentA_indian")]
    db.talents.insert_one({"id": talent_a_id, "name": "ZZZ_TEST Talent A", "email": f"zzz-a-{uuid.uuid4().hex[:8]}@example.com", "media": talent_a_media})

    talent_b_id = str(uuid.uuid4())
    talent_b_media = [_media_item("indian", "talentB_indian")]
    db.talents.insert_one({"id": talent_b_id, "name": "ZZZ_TEST Talent B", "email": f"zzz-b-{uuid.uuid4().hex[:8]}@example.com", "media": talent_b_media})

    sub_a_id = str(uuid.uuid4())
    db.submissions.insert_one({"id": sub_a_id, "project_id": project_id, "talent_id": talent_a_id, "talent_name": "ZZZ_TEST Talent A", "talent_email": "", "media": [], "status": "submitted"})

    yield {
        "project_id": project_id,
        "sub_a_id": sub_a_id,
        "talent_a_id": talent_a_id,
        "talent_a_media_id": talent_a_media[0]["id"],
        "talent_a_public_id": talent_a_media[0]["public_id"],
        "talent_b_id": talent_b_id,
        "talent_b_media_id": talent_b_media[0]["id"],
    }

    db.projects.delete_one({"id": project_id})
    db.submissions.delete_many({"project_id": project_id})
    db.talents.delete_many({"id": {"$in": [talent_a_id, talent_b_id]}})


def test_copies_own_talents_media_by_reference_not_upload(two_talents_one_project, admin_token):
    ctx = two_talents_one_project
    r = requests.post(
        f"{API}/projects/{ctx['project_id']}/submissions/{ctx['sub_a_id']}/admin-media-from-talent",
        json={"media_ids": [ctx["talent_a_media_id"]]},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    added = [m for m in r.json()["media"] if m.get("from_global_profile")]
    assert len(added) == 1
    assert added[0]["public_id"] == ctx["talent_a_public_id"]  # same asset, not re-uploaded
    assert added[0]["category"] == "indian"
    assert added[0]["admin_added"] is True
    assert added[0]["source_talent_media_id"] == ctx["talent_a_media_id"]
    assert added[0]["id"] != ctx["talent_a_media_id"]  # a new item, not the same document


def test_cannot_pull_in_another_talents_media(two_talents_one_project, admin_token):
    """The whole point of the feature: asking for Talent B's media ID
    through Talent A's submission must not succeed — the endpoint only
    ever looks inside the submission's OWN resolved talent's media[]."""
    ctx = two_talents_one_project
    r = requests.post(
        f"{API}/projects/{ctx['project_id']}/submissions/{ctx['sub_a_id']}/admin-media-from-talent",
        json={"media_ids": [ctx["talent_b_media_id"]]},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 400, r.text  # nothing in the request resolved to a real item


def test_removing_the_copy_does_not_touch_the_talents_global_media(two_talents_one_project, admin_token, db):
    ctx = two_talents_one_project
    r = requests.post(
        f"{API}/projects/{ctx['project_id']}/submissions/{ctx['sub_a_id']}/admin-media-from-talent",
        json={"media_ids": [ctx["talent_a_media_id"]]},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    copied_id = [m for m in r.json()["media"] if m.get("from_global_profile")][0]["id"]

    r2 = requests.delete(
        f"{API}/projects/{ctx['project_id']}/submissions/{ctx['sub_a_id']}/media/{copied_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r2.status_code == 200, r2.text

    talent = db.talents.find_one({"id": ctx["talent_a_id"]}, {"_id": 0, "media": 1})
    assert len(talent["media"]) == 1
    assert talent["media"][0]["id"] == ctx["talent_a_media_id"]  # untouched, still there


def test_unknown_media_id_returns_400(two_talents_one_project, admin_token):
    ctx = two_talents_one_project
    r = requests.post(
        f"{API}/projects/{ctx['project_id']}/submissions/{ctx['sub_a_id']}/admin-media-from-talent",
        json={"media_ids": [str(uuid.uuid4())]},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert r.status_code == 400
