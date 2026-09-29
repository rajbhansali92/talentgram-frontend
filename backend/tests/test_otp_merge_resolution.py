"""OTP verification must use core.resolve_canonical_talent for its post-
verify talent lookup, exactly like submissions.py's finalize path and
trusted-device recognition already do — otherwise a "Merge Different
Emails" talent authenticating with their OLD (now-alternate) email passes
OTP verification but the app treats them as brand new, because the old
hand-rolled `db.talents.find_one({"$or": [{"normalized_email"...}, {"email"...}]})`
never checks `alternate_emails`.

This is the exact bug traced to a real production incident (Dia/"Diya"
Malik, talent 495018b3-d2db-458d-83af-7f437407ef54, merged from
759e0362-85a8-4e61-bd4f-5506beda1e6e on 2026-09-25): every OTP verify with
her old email `iamdiamalik@gmail.com` succeeded, but the response said
`existing: false`, so the frontend never recognized her, and she kept
re-triggering `otp/send` trying to make it "work" until she hit the
legitimate 5/hour rate limit.

Real-DB integration tests (local Mongo), same established pattern as
test_submission_email_merge_resolution.py / test_trusted_device_merge_resolution.py.
"""
import hashlib
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import httpx
import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

import core
import routers.auth as rauth
from core import resolve_canonical_talent
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MTAG = "TEST_OTP_MERGE_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _restore_real_db_for_this_file():
    prior_core_db = core.db
    prior_auth_db = rauth.db
    core.db = _real_db
    rauth.db = _real_db
    # This file's own test emails are per-run-unique (uuid suffix / timestamp),
    # so the email-side of the rate-limit $or never collides across runs — but
    # every request in this file shares the same local IP, and that side of
    # the bucket persists in real Mongo across repeated local runs within the
    # same rolling window. Clear just our own prior otp_send/otp_verify rows
    # for that IP before running, so re-running this file locally doesn't
    # trip the real 429 the fix is about.
    await _real_db.rate_limits.delete_many({"ip": "127.0.0.1", "endpoint": {"$in": ["otp_verify", "otp_send"]}})
    try:
        yield
    finally:
        core.db = prior_core_db
        rauth.db = prior_auth_db


def _talent(idx, **overrides):
    doc = {
        "id": f"{_MTAG}{idx}_{_uuid.uuid4().hex[:8]}",
        "name": "OTP Merge Test Talent",
        "email": None, "normalized_email": None, "phone": None,
        "status": "SUBMITTED", "media": [], "alternate_emails": [],
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    doc.update(overrides)
    return doc


async def _cleanup(*, talent_ids=None, submission_ids=None, project_ids=None):
    if talent_ids:
        await _real_db.talents.delete_many({"id": {"$in": talent_ids}})
    if submission_ids:
        await _real_db.submissions.delete_many({"id": {"$in": submission_ids}})
    if project_ids:
        await _real_db.projects.delete_many({"id": {"$in": project_ids}})


async def _seed_valid_otp(email: str) -> str:
    """Inserts a real, valid, unused OTP code for `email` and returns the
    plaintext code — the same shape /auth/otp/verify expects."""
    otp = f"{_uuid.uuid4().int % 900000 + 100000}"
    await _real_db.otp_codes.update_many({"email": email, "used": False}, {"$set": {"used": True}})
    await _real_db.otp_codes.insert_one({
        "email": email,
        "hashed_otp": hashlib.sha256(otp.encode()).hexdigest(),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "attempts": 0,
        "used": False,
        "ip_address": "127.0.0.1",
        "created_at": datetime.now(timezone.utc),
    })
    return otp


async def _verify(client, email, otp, slug="portal"):
    return await client.post("/api/auth/otp/verify", json={"email": email, "otp": otp, "slug": slug})


# TEST 1 — Primary email resolves to the existing canonical talent.
async def test_primary_email_resolves_to_canonical_talent(client):
    email = f"{_MTAG.lower()}primary1@example.com"
    t = _talent("t1", name="Primary Resolver", email=email, normalized_email=email)
    await _real_db.talents.insert_one(t)
    try:
        otp = await _seed_valid_otp(email)
        resp = await _verify(client, email, otp)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["existing"] is True
        assert data["talent"]["first_name"] == "Primary"
        # The real, observable side effect: _grant_portal_session persists a
        # token onto the RESOLVED talent's own doc.
        refreshed = await _real_db.talents.find_one({"id": t["id"]}, {"_id": 0})
        assert refreshed.get("portal_access_token")
    finally:
        await _cleanup(talent_ids=[t["id"]])


# TEST 2 — Alternate (merged-in) email resolves to the SAME canonical talent.
async def test_alternate_merged_email_resolves_to_canonical_talent(client):
    primary_email = f"{_MTAG.lower()}canon2@example.com"
    alt_email = f"{_MTAG.lower()}alt2@example.com"
    canonical = _talent("t2", name="Alt Resolver Canonical", email=primary_email,
                         normalized_email=primary_email, alternate_emails=[alt_email])
    await _real_db.talents.insert_one(canonical)
    try:
        otp = await _seed_valid_otp(alt_email)
        resp = await _verify(client, alt_email, otp)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["existing"] is True
        assert data["talent"]["first_name"] == "Alt"
        refreshed = await _real_db.talents.find_one({"id": canonical["id"]}, {"_id": 0})
        assert refreshed.get("portal_access_token")
    finally:
        await _cleanup(talent_ids=[canonical["id"]])


# TEST 3 — A genuinely unknown email still behaves as a brand-new user
# (unchanged: draft created, no talent found, existing: false).
async def test_unknown_email_behaves_as_new_user(client):
    email = f"{_MTAG.lower()}totally-unknown-{_uuid.uuid4().hex[:6]}@example.com"
    otp = await _seed_valid_otp(email)
    resp = await _verify(client, email, otp)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["existing"] is False
    assert data["email"] == email
    draft = await _real_db.submission_drafts.find_one({"email": email, "project_id": "portal"})
    assert draft is not None
    await _real_db.submission_drafts.delete_one({"email": email, "project_id": "portal"})


# TEST 4 — The response resolves to the CANONICAL talent id, never the
# old/absorbed (MERGED) id. Verified via the observable side effect:
# _grant_portal_session only ever writes to the id it was handed.
#
# The absorbed doc here intentionally reproduces Diya Malik's exact real
# shape: `source.talent_email` still equals the alternate email (the
# merge only clears `email`/`normalized_email`, not `source`), and it's
# inserted BEFORE the canonical doc — both details matter, because
# without core.resolve_canonical_talent's status != MERGED exclusion,
# Mongo's unsorted find_one matched BOTH documents and returned this
# older, absorbed one instead of the canonical.
async def test_merged_email_resolves_to_canonical_id_not_old_id(client):
    primary_email = f"{_MTAG.lower()}canon4@example.com"
    alt_email = f"{_MTAG.lower()}alt4@example.com"
    absorbed = _talent("t4a", name="Absorbed Four", email=None, normalized_email=None,
                        status="MERGED", source={"type": "audition_submission", "talent_email": alt_email})
    await _real_db.talents.insert_one(absorbed)
    canonical = _talent("t4c", name="Canonical Four", email=primary_email,
                         normalized_email=primary_email, alternate_emails=[alt_email],
                         merged_from=[absorbed["id"]])
    await _real_db.talents.insert_one(canonical)
    try:
        otp = await _seed_valid_otp(alt_email)
        resp = await _verify(client, alt_email, otp)
        assert resp.status_code == 200, resp.text
        assert resp.json()["existing"] is True

        canonical_after = await _real_db.talents.find_one({"id": canonical["id"]}, {"_id": 0})
        absorbed_after = await _real_db.talents.find_one({"id": absorbed["id"]}, {"_id": 0})
        assert canonical_after.get("portal_access_token"), "portal session must land on the CANONICAL talent"
        assert not absorbed_after.get("portal_access_token"), "the absorbed/old talent must never be touched"
    finally:
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]])


# TEST 5 — Alternate-email verification returns existing: true AND real
# canonical talent information (not just a bare boolean).
async def test_alternate_email_verify_returns_existing_true_with_talent_info(client):
    primary_email = f"{_MTAG.lower()}canon5@example.com"
    alt_email = f"{_MTAG.lower()}alt5@example.com"
    canonical = _talent("t5", name="Info Carrying Canonical", email=primary_email,
                         normalized_email=primary_email, alternate_emails=[alt_email],
                         location="Mumbai")
    await _real_db.talents.insert_one(canonical)
    try:
        otp = await _seed_valid_otp(alt_email)
        resp = await _verify(client, alt_email, otp)
        data = resp.json()
        assert data["existing"] is True
        assert data["talent"]["first_name"] == "Info"
        assert data["talent"]["last_name"] == "Carrying Canonical"
        assert data["talent"]["location"] == "Mumbai"
        assert data.get("portal_token")
    finally:
        await _cleanup(talent_ids=[canonical["id"]])


# TEST 6 — An existing, never-merged talent's OTP flow is byte-for-byte
# unchanged (regression safety for the primary-email path).
async def test_unmerged_talent_flow_unchanged(client):
    email = f"{_MTAG.lower()}plain6@example.com"
    t = _talent("t6", name="Plain Unmerged", email=email, normalized_email=email)
    await _real_db.talents.insert_one(t)
    try:
        otp = await _seed_valid_otp(email)
        resp = await _verify(client, email, otp)
        assert resp.status_code == 200
        data = resp.json()
        assert data["existing"] is True
        assert data["talent"]["first_name"] == "Plain"
        via_resolver = await resolve_canonical_talent(email=email)
        assert via_resolver["id"] == t["id"]
    finally:
        await _cleanup(talent_ids=[t["id"]])


# TEST 7 — OTP verification never creates a duplicate talent record for an
# email that already resolves to an existing (canonical or alternate) one.
async def test_verify_does_not_create_duplicate_talent(client):
    primary_email = f"{_MTAG.lower()}canon7@example.com"
    alt_email = f"{_MTAG.lower()}alt7@example.com"
    canonical = _talent("t7", name="No Duplicate Canonical", email=primary_email,
                         normalized_email=primary_email, alternate_emails=[alt_email])
    await _real_db.talents.insert_one(canonical)
    try:
        before = await _real_db.talents.count_documents({"$or": [{"email": {"$in": [primary_email, alt_email]}}, {"alternate_emails": alt_email}]})
        otp = await _seed_valid_otp(alt_email)
        resp = await _verify(client, alt_email, otp)
        assert resp.status_code == 200
        after = await _real_db.talents.count_documents({"$or": [{"email": {"$in": [primary_email, alt_email]}}, {"alternate_emails": alt_email}]})
        assert after == before == 1
    finally:
        await _cleanup(talent_ids=[canonical["id"]])


# TEST 8 — After OTP verification via the alternate email, the same
# canonical identity is what the rest of the submission flow (the shared
# resolver every other entry point already uses) sees too — end-to-end
# identity consistency, not just this one endpoint in isolation.
async def test_post_verification_identity_consistent_with_submission_flow(client):
    primary_email = f"{_MTAG.lower()}canon8@example.com"
    alt_email = f"{_MTAG.lower()}alt8@example.com"
    canonical = _talent("t8", name="End To End Canonical", email=primary_email,
                         normalized_email=primary_email, alternate_emails=[alt_email])
    await _real_db.talents.insert_one(canonical)
    try:
        otp = await _seed_valid_otp(alt_email)
        resp = await _verify(client, alt_email, otp)
        assert resp.json()["existing"] is True

        # The same identity a subsequent submission-start/prefill call would
        # resolve, using the exact shared helper those call sites use.
        downstream = await resolve_canonical_talent(email=alt_email)
        assert downstream["id"] == canonical["id"]
    finally:
        await _cleanup(talent_ids=[canonical["id"]])


# TEST 9 (additional, resolver-level) — core.resolve_canonical_talent itself
# must never return an absorbed (status=MERGED) talent, even when its
# uncleared `source.talent_email` still matches the query. This is the
# second bug found while pre-deployment-verifying Diya's real production
# data: the merge clears email/normalized_email on the absorbed doc but
# NOT source.talent_email, so both documents matched and Mongo's unsorted
# find_one returned the absorbed one. Direct unit coverage of the fix,
# independent of the OTP endpoint.
async def test_resolver_excludes_merged_talent_even_with_matching_source_email():
    email = f"{_MTAG.lower()}sourcematch9@example.com"
    absorbed = _talent("t9a", name="Absorbed Nine", email=None, normalized_email=None,
                        status="MERGED", source={"type": "audition_submission", "talent_email": email})
    await _real_db.talents.insert_one(absorbed)
    canonical = _talent("t9c", name="Canonical Nine", email=f"{_MTAG.lower()}canon9@example.com",
                         normalized_email=f"{_MTAG.lower()}canon9@example.com", alternate_emails=[email],
                         merged_from=[absorbed["id"]])
    await _real_db.talents.insert_one(canonical)
    try:
        resolved = await resolve_canonical_talent(email=email)
        assert resolved is not None
        assert resolved["id"] == canonical["id"], "must resolve to the canonical talent, never the absorbed one"
        assert resolved["status"] != "MERGED"
    finally:
        await _cleanup(talent_ids=[canonical["id"], absorbed["id"]])
