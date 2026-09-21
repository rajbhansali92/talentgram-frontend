"""Submission-flow identity resolution must use core.resolve_canonical_talent
wherever a talent gets identified/created from an email, or a "Merge
Different Emails" talent's alternate email can create a third duplicate
Talent instead of attaching to the existing canonical one (2026-09-21 Juhi
incident's second half — see test_trusted_device_merge_resolution.py for the
trusted-device half).

Real-DB integration tests, same pattern as test_talent_merge_emails.py.
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
import routers.talents as routers_talents
import routers.submissions as rsub
from core import (
    db as _core_db_unused,  # noqa: F401 (imported for symmetry with other test files)
    current_admin, current_team_or_admin,
    create_or_resume_submission_doc, resolve_canonical_talent,
    SubmissionUpdateIn, SubmissionDecisionIn,
)
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MTAG = "TEST_SUB_EMAIL_MERGE_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]


@pytest.fixture(autouse=True)
def _restore_real_db_for_this_file():
    prior_core_db = core.db
    prior_talents_db = routers_talents.db
    prior_submissions_db = rsub.db
    core.db = _real_db
    routers_talents.db = _real_db
    rsub.db = _real_db
    try:
        yield
    finally:
        core.db = prior_core_db
        routers_talents.db = prior_talents_db
        rsub.db = prior_submissions_db


def _talent(idx, **overrides):
    doc = {
        "id": f"{_MTAG}{idx}_{_uuid.uuid4().hex[:8]}",
        "name": "Submission Merge Test Talent",
        "email": None, "normalized_email": None, "phone": None,
        "status": "SUBMITTED", "media": [], "alternate_emails": [],
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    doc.update(overrides)
    return doc


def _project(idx, **overrides):
    doc = {
        "id": f"{_MTAG}proj_{idx}_{_uuid.uuid4().hex[:8]}",
        "slug": f"{_MTAG.lower()}slug-{idx}-{_uuid.uuid4().hex[:6]}",
        "name": "Submission Merge Test Project",
        "brand_name": "Test Brand",
    }
    doc.update(overrides)
    return doc


_LEGACY_FORM = {
    "first_name": "First", "last_name": "Last", "height": "5'7\"",
    "location": "Mumbai", "availability": {"status": "yes"}, "budget": {"status": "accept"},
}


async def _cleanup(*, talent_ids=None, project_ids=None, submission_ids=None):
    if talent_ids:
        await _real_db.talents.delete_many({"id": {"$in": talent_ids}})
    if project_ids:
        await _real_db.projects.delete_many({"id": {"$in": project_ids}})
        await _real_db.casting_pipeline.delete_many({"project_id": {"$in": project_ids}})
    if submission_ids:
        await _real_db.submissions.delete_many({"id": {"$in": submission_ids}})


async def _finalize(project, email, name="First Last"):
    """Creates a draft (bypassing the talent-facing ownership gate, which is
    tested separately) and finalizes it with minimal legacy-fallback form
    data. Returns (submission_after, talent_id)."""
    start = await create_or_resume_submission_doc(
        project, email, name, None, None, {}, created_from="talent_link",
    )
    sid, token = start["id"], start["token"]
    await rsub.submission_update(sid, SubmissionUpdateIn(form_data=_LEGACY_FORM), authorization=f"Bearer {token}")
    result = await rsub.submission_finalize(sid, Response(), authorization=f"Bearer {token}")
    sub_after = await _real_db.submissions.find_one({"id": sid}, {"_id": 0})
    return sid, sub_after, result


# --------------------------------------------------------------------------
# B6/B7 — Primary and alternate email both resolve to the canonical talent
# via the resolver every one of these fixes now uses.
# --------------------------------------------------------------------------
async def test_b6_b7_primary_and_alternate_email_resolve_to_canonical():
    canonical = _talent("b1", name="Resolver Canonical", email="resolver-primary@example.com",
                         normalized_email="resolver-primary@example.com", alternate_emails=["resolver-alt@example.com"])
    await _real_db.talents.insert_one(canonical)
    try:
        primary = await resolve_canonical_talent(email="resolver-primary@example.com")
        alt = await resolve_canonical_talent(email="resolver-alt@example.com")
        assert primary and primary["id"] == canonical["id"]
        assert alt and alt["id"] == canonical["id"]
    finally:
        await _cleanup(talent_ids=[canonical["id"]])


# --------------------------------------------------------------------------
# B8 — Unknown email: normal new-talent behavior (nothing found; the
# finalize/decision paths still fall through to their existing creation
# logic, proven in C11/C12 below).
# --------------------------------------------------------------------------
async def test_b8_unknown_email_resolves_to_nothing():
    found = await resolve_canonical_talent(email=f"{_MTAG.lower()}totally-unknown@example.com")
    assert found is None


# --------------------------------------------------------------------------
# B9 — An absorbed/merged talent's OWN (now-cleared) email no longer
# resolves anywhere -- confirms the merge's unset step plus the resolver
# never resurrect the absorbed identity.
# --------------------------------------------------------------------------
async def test_b9_absorbed_talent_email_field_cleared_and_unresolvable():
    canonical = _talent("b9c", name="Absorbed Check Canonical", email="absorbed-check-canon@example.com",
                         normalized_email="absorbed-check-canon@example.com", alternate_emails=["absorbed-check-alt@example.com"])
    absorbed = _talent("b9a", name="Absorbed Check Absorbed", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    try:
        # The absorbed talent's own email field is gone -- nothing should
        # ever resolve directly to the absorbed id via email again.
        assert absorbed.get("email") is None
        via_alt = await resolve_canonical_talent(email="absorbed-check-alt@example.com")
        assert via_alt["id"] == canonical["id"], "must resolve to canonical, never the absorbed id"
    finally:
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]])


# --------------------------------------------------------------------------
# B10 — Existing unmerged-talent resolution behavior is unchanged.
# --------------------------------------------------------------------------
async def test_b10_unmerged_talent_unchanged():
    t = _talent("b10", name="Plain Talent", email="plain-talent@example.com", normalized_email="plain-talent@example.com")
    await _real_db.talents.insert_one(t)
    try:
        found = await resolve_canonical_talent(email="plain-talent@example.com")
        assert found and found["id"] == t["id"]
    finally:
        await _cleanup(talent_ids=[t["id"]])


# --------------------------------------------------------------------------
# C11 — A brand-new (never-seen) email can still start a submission with no
# ownership friction (the existing friction-free first-time flow).
# --------------------------------------------------------------------------
async def test_c11_new_talent_can_start_submission():
    project = _project("c11")
    await _real_db.projects.insert_one(project)
    email = f"{_MTAG.lower()}new-{_uuid.uuid4().hex[:6]}@example.com"
    try:
        from core import SubmissionStartIn
        result = await rsub.start_submission(
            project["slug"], SubmissionStartIn(name="New Person", email=email, phone=None, alternate_contact_number=None, form_data={}),
            request=None, authorization=None,
        )
        assert result["resumed"] is False
        sid = result["id"]
        try:
            sub = await _real_db.submissions.find_one({"id": sid}, {"_id": 0})
            assert sub["talent_email"] == email
        finally:
            await _cleanup(submission_ids=[sid])
    finally:
        await _cleanup(project_ids=[project["id"]])


# --------------------------------------------------------------------------
# C12 — A normal (never-merged) talent can finalize a submission exactly as
# before, auto-creating their Talent record.
# --------------------------------------------------------------------------
async def test_c12_new_talent_can_finalize_submission_normal_autocreate():
    project = _project("c12")
    await _real_db.projects.insert_one(project)
    email = f"{_MTAG.lower()}autocreate-{_uuid.uuid4().hex[:6]}@example.com"
    sid = None
    created_talent_id = None
    try:
        sid, sub_after, result = await _finalize(project, email, name="Auto Create")
        assert result["ok"] is True
        assert sub_after["status"] == "submitted"
        created_talent_id = sub_after.get("talent_id")
        assert created_talent_id, "finalize must still auto-create a talent for a genuinely new email"
        created = await _real_db.talents.find_one({"id": created_talent_id}, {"_id": 0})
        assert created and created.get("email") == email
    finally:
        await _cleanup(
            talent_ids=[created_talent_id] if created_talent_id else [],
            project_ids=[project["id"]],
            submission_ids=[sid] if sid else [],
        )


# --------------------------------------------------------------------------
# C13 — Canonical Juhi (an already-merged talent's SURVIVOR) can start/
# resume a submission using her own primary email with no regression.
# --------------------------------------------------------------------------
async def test_c13_canonical_talent_can_start_and_resume_submission():
    project = _project("c13")
    await _real_db.projects.insert_one(project)
    canonical = _talent("c13", name="Canonical Resume Person", email="canon-resume@example.com",
                         normalized_email="canon-resume@example.com", alternate_emails=["canon-resume-alt@example.com"])
    await _real_db.talents.insert_one(canonical)
    sid = None
    try:
        first = await create_or_resume_submission_doc(
            project, "canon-resume@example.com", canonical["name"], None, None, {}, created_from="talent_link",
        )
        sid = first["id"]
        assert first["resumed"] is False
        second = await create_or_resume_submission_doc(
            project, "canon-resume@example.com", canonical["name"], None, None, {}, created_from="talent_link",
        )
        assert second["resumed"] is True
        assert second["id"] == sid, "resuming must hit the SAME (project, email) submission, never a new one"
    finally:
        await _cleanup(talent_ids=[canonical["id"]], project_ids=[project["id"]], submission_ids=[sid] if sid else [])


# --------------------------------------------------------------------------
# C14/C15 — THE core regression: a new project submission using the
# ALTERNATE email attaches to the canonical talent at finalize, and does
# NOT create a third duplicate Talent.
# --------------------------------------------------------------------------
async def test_c14_c15_alternate_email_finalize_resolves_to_canonical_no_third_talent():
    project = _project("c14")
    await _real_db.projects.insert_one(project)
    canonical = _talent("c14c", name="Merged Person", email="merged-primary@example.com",
                         normalized_email="merged-primary@example.com", alternate_emails=["merged-alt@example.com"])
    absorbed = _talent("c14a", name="Merged Person", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    sid = None
    try:
        sid, sub_after, result = await _finalize(project, "merged-alt@example.com", name="Merged Person")
        assert result["ok"] is True
        assert sub_after["talent_id"] == canonical["id"], "must attach to the EXISTING canonical talent"

        all_named = [
            t async for t in _real_db.talents.find(
                {"id": {"$in": [canonical["id"], absorbed["id"]]}}, {"_id": 0, "id": 1}
            )
        ]
        assert len(all_named) == 2, "must remain exactly the original canonical + absorbed pair -- no third talent"
        no_new_talent = await _real_db.talents.count_documents({
            "id": {"$nin": [canonical["id"], absorbed["id"]]},
            "email": "merged-alt@example.com",
        })
        assert no_new_talent == 0, "no third talent may ever be created for the alternate email"
    finally:
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]], project_ids=[project["id"]], submission_ids=[sid] if sid else [])


# --------------------------------------------------------------------------
# C16 — A stale trusted device (bound to the absorbed id) combined with the
# prefill-response builder still surfaces the canonical talent's real,
# non-empty email end-to-end (closes the loop with the trusted-device fix).
# --------------------------------------------------------------------------
async def test_c16_stale_trusted_device_recognition_yields_canonical_email():
    canonical = _talent("c16c", name="Device Recognition Person", email="device-primary@example.com",
                         normalized_email="device-primary@example.com", alternate_emails=["device-alt@example.com"])
    absorbed = _talent("c16a", name="Device Recognition Person", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    try:
        raw = await core.mint_trusted_device(absorbed["id"])
        resolved = await core.resolve_trusted_device(raw)
        assert resolved["talent"]["id"] == canonical["id"]
        email = core.normalize_email(resolved["talent"].get("email") or resolved["talent"].get("normalized_email") or "")
        assert email == "device-primary@example.com", "recognition must surface the canonical's real email, never None"
        payload = await rsub._build_prefill_response(resolved["talent"], email)
        assert payload["first_name"] == "Device"
    finally:
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]])


# --------------------------------------------------------------------------
# Fix: start_submission's P0-2 ownership gate now recognizes a known
# alternate email (via resolve_canonical_talent) and still requires proof
# of ownership instead of silently taking the friction-free new-person path.
# --------------------------------------------------------------------------
async def test_start_submission_gate_requires_ownership_for_known_alternate_email():
    project = _project("gate")
    await _real_db.projects.insert_one(project)
    canonical = _talent("gate", name="Gate Person", email="gate-primary@example.com",
                         normalized_email="gate-primary@example.com", alternate_emails=["gate-alt@example.com"])
    await _real_db.talents.insert_one(canonical)
    try:
        from core import SubmissionStartIn
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await rsub.start_submission(
                project["slug"],
                SubmissionStartIn(name="Gate Person", email="gate-alt@example.com", phone=None, alternate_contact_number=None, form_data={}),
                request=None, authorization=None,
            )
        assert exc_info.value.status_code == 403
    finally:
        await _cleanup(talent_ids=[canonical["id"]], project_ids=[project["id"]])


# --------------------------------------------------------------------------
# Fix: the admin decision-endpoint's talent-resolution fallback (used when a
# submission somehow reaches decision-time with no talent_id yet) also uses
# the canonical resolver -- same duplicate-talent risk as finalize.
# --------------------------------------------------------------------------
async def test_decision_fallback_resolves_alternate_email_to_canonical():
    project = _project("dec")
    await _real_db.projects.insert_one(project)
    canonical = _talent("decc", name="Decision Person", email="decision-primary@example.com",
                         normalized_email="decision-primary@example.com", alternate_emails=["decision-alt@example.com"])
    absorbed = _talent("deca", name="Decision Person", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    sid = f"{_MTAG}sub_{_uuid.uuid4().hex[:8]}"
    sub_doc = {
        "id": sid, "project_id": project["id"], "project_slug": project["slug"],
        "talent_name": "Decision Person", "talent_email": "decision-alt@example.com",
        "talent_id": None, "status": "submitted", "decision": "pending",
        "form_data": {}, "media": [], "created_at": "2026-09-01T00:00:00+00:00",
    }
    await _real_db.submissions.insert_one(sub_doc)
    admin = {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}
    app.dependency_overrides[current_team_or_admin] = lambda: admin
    try:
        await rsub.set_decision(project["id"], sid, SubmissionDecisionIn(decision="ask_to_test"), admin=admin)
        sub_after = await _real_db.submissions.find_one({"id": sid}, {"_id": 0})
        assert sub_after["talent_id"] == canonical["id"]

        no_third = await _real_db.talents.count_documents({
            "id": {"$nin": [canonical["id"], absorbed["id"]]}, "email": "decision-alt@example.com",
        })
        assert no_third == 0
    finally:
        app.dependency_overrides.clear()
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]], project_ids=[project["id"]], submission_ids=[sid])
