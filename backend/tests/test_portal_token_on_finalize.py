"""A first-time-ever submitter/applicant's OTP verification happens before
any Talent record exists, so the existing `_grant_portal_session` call at
that point has nothing to grant a portal_token for. The Talent record is
only created later, inside finalize — but until this fix, nothing at
finalize ever granted a portal session either. Result: "View My Talent
Dashboard" / "Update Portfolio" on the post-submission success screen led
nowhere for a first-time talent (PortalHome.jsx finds no session, bounces
to "/").

Real-DB integration tests (local Mongo), same established pattern as
test_submission_email_merge_resolution.py.
"""
import os
import uuid as _uuid

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest
from fastapi import Response
from motor.motor_asyncio import AsyncIOMotorClient

import core
import routers.applications as rapp
import routers.submissions as rsub
from core import (
    current_portal_talent, mint_portal_token,
    create_or_resume_submission_doc, SubmissionUpdateIn, ApplicationStartIn,
)
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MTAG = "TEST_PORTAL_TOKEN_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]


@pytest.fixture(autouse=True)
def _restore_real_db_for_this_file():
    prior_core_db = core.db
    prior_app_db = rapp.db
    prior_sub_db = rsub.db
    core.db = _real_db
    rapp.db = _real_db
    rsub.db = _real_db
    try:
        yield
    finally:
        core.db = prior_core_db
        rapp.db = prior_app_db
        rsub.db = prior_sub_db


def _project(idx, **overrides):
    doc = {
        "id": f"{_MTAG}proj_{idx}_{_uuid.uuid4().hex[:8]}",
        "slug": f"{_MTAG.lower()}slug-{idx}-{_uuid.uuid4().hex[:6]}",
        "brand_name": "Portal Token Test Project",
    }
    doc.update(overrides)
    return doc


_LEGACY_FORM = {
    "first_name": "First", "last_name": "Last", "height": "5'7\"",
    "location": "Mumbai", "availability": {"status": "yes"}, "budget": {"status": "accept"},
}


async def _cleanup(*, talent_ids=None, project_ids=None, submission_ids=None, application_ids=None, config_ids=None):
    if talent_ids:
        await _real_db.talents.delete_many({"id": {"$in": talent_ids}})
    if project_ids:
        await _real_db.projects.delete_many({"id": {"$in": project_ids}})
    if submission_ids:
        await _real_db.submissions.delete_many({"id": {"$in": submission_ids}})
    if application_ids:
        await _real_db.applications.delete_many({"id": {"$in": application_ids}})
    if config_ids:
        await _real_db.profile_configs.delete_many({"id": {"$in": config_ids}})


# TEST 1 — First-time project submission: finalize grants a portal_token,
# and that token actually authenticates against current_portal_talent.
async def test_first_time_submission_finalize_grants_working_portal_session():
    project = _project("t1")
    await _real_db.projects.insert_one(project)
    email = f"{_MTAG.lower()}sub1-{_uuid.uuid4().hex[:6]}@example.com"
    sid = None
    talent_id = None
    try:
        start = await create_or_resume_submission_doc(
            project, email, "Portal Token", None, None, {}, created_from="talent_link",
        )
        sid, token = start["id"], start["token"]
        await rsub.submission_update(sid, SubmissionUpdateIn(form_data=_LEGACY_FORM), authorization=f"Bearer {token}")
        result = await rsub.submission_finalize(sid, Response(), authorization=f"Bearer {token}")

        assert result["ok"] is True
        talent_id = result["talent_id"]
        assert talent_id, "finalize must have created a Talent record"
        assert result["portal_token"], "a first-time submitter must be granted a portal session at finalize"

        # The token must actually work — the whole point of the fix.
        resolved = await current_portal_talent(authorization=f"Bearer {result['portal_token']}")
        assert resolved["id"] == talent_id
    finally:
        await _cleanup(talent_ids=[talent_id] if talent_id else [], project_ids=[project["id"]],
                        submission_ids=[sid] if sid else [])


# TEST 2 — First-time onboarding/profile application: finalize grants a
# working portal_token the exact same way.
async def test_first_time_application_finalize_grants_working_portal_session():
    config = {
        "id": f"{_MTAG}cfg_{_uuid.uuid4().hex[:8]}",
        "profile_requirements": {"name": "required", "location": "optional",
                                  "instagram_handle": "optional", "instagram_followers": "optional"},
        "portfolio_requirements": {"portfolio": "optional", "indian": "optional",
                                    "western": "optional", "video": "optional"},
    }
    await _real_db.profile_configs.insert_one(config)
    email = f"{_MTAG.lower()}app2-{_uuid.uuid4().hex[:6]}@example.com"
    aid = None
    talent_id = None
    try:
        start = await rapp.start_application(
            ApplicationStartIn(first_name="Portal", last_name="Token", email=email, profile_id=config["id"]),
            request=None, authorization=None,
        )
        aid, token = start["id"], start["token"]
        await rapp.update_application(
            aid, SubmissionUpdateIn(form_data={"first_name": "Portal", "last_name": "Token", "location": "Mumbai"}),
            authorization=f"Bearer {token}",
        )
        result = await rapp.finalize_application(aid, authorization=f"Bearer {token}")

        assert result["ok"] is True
        talent_id = result["talent_id"]
        assert talent_id, "finalize must have created/linked a Talent record"
        assert result["portal_token"], "a first-time applicant must be granted a portal session at finalize"

        resolved = await current_portal_talent(authorization=f"Bearer {result['portal_token']}")
        assert resolved["id"] == talent_id
    finally:
        await _cleanup(talent_ids=[talent_id] if talent_id else [], application_ids=[aid] if aid else [],
                        config_ids=[config["id"]])


# TEST 3 — A returning talent who already has an active portal session
# (portal_access_token already set) must NOT have it silently rotated by
# an unrelated project finalize — their existing session must keep working.
async def test_returning_talent_finalize_does_not_rotate_existing_session():
    project = _project("t3")
    await _real_db.projects.insert_one(project)
    email = f"{_MTAG.lower()}sub3-{_uuid.uuid4().hex[:6]}@example.com"
    talent_id = f"{_MTAG}talent3_{_uuid.uuid4().hex[:8]}"
    existing_token = mint_portal_token(email)
    await _real_db.talents.insert_one({
        "id": talent_id, "name": "Existing Talent", "email": email, "normalized_email": email,
        "status": "SUBMITTED", "media": [], "alternate_emails": [],
        "portal_access_token": existing_token,
        "created_at": "2026-08-01T00:00:00+00:00",
    })
    sid = None
    try:
        start = await create_or_resume_submission_doc(
            project, email, "Existing Talent", None, None, {}, created_from="talent_link",
        )
        sid, token = start["id"], start["token"]
        await rsub.submission_update(sid, SubmissionUpdateIn(form_data=_LEGACY_FORM), authorization=f"Bearer {token}")
        result = await rsub.submission_finalize(sid, Response(), authorization=f"Bearer {token}")

        assert result["ok"] is True
        assert result["talent_id"] == talent_id
        assert result["portal_token"] is None, "an already-sessioned talent must not get a freshly rotated token here"

        # The ORIGINAL session must still work unchanged.
        resolved = await current_portal_talent(authorization=f"Bearer {existing_token}")
        assert resolved["id"] == talent_id
    finally:
        await _cleanup(talent_ids=[talent_id], project_ids=[project["id"]], submission_ids=[sid] if sid else [])
