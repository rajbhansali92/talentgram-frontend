"""Trusted-device recognition must not keep resolving to a talent that has
since been absorbed by a merge (2026-09-21 Juhi incident: her browser's
trusted-device cookie stayed bound to her now-MERGED talent_id, whose email
fields the merge correctly cleared, so recognition silently returned
email=None and broke the submission-form flow).

Real-DB integration tests, same pattern as test_talent_merge.py /
test_talent_merge_emails.py.
"""
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

import core

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MTAG = "TEST_TD_MERGE_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]


@pytest.fixture(autouse=True)
def _restore_real_db_for_this_file():
    prior = core.db
    core.db = _real_db
    try:
        yield
    finally:
        core.db = prior


def _talent(idx, **overrides):
    doc = {
        "id": f"{_MTAG}{idx}_{_uuid.uuid4().hex[:8]}",
        "name": "Trusted Device Test Talent",
        "email": None, "normalized_email": None, "phone": None,
        "status": "SUBMITTED", "media": [], "alternate_emails": [],
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    doc.update(overrides)
    return doc


async def _mint_device(talent_id):
    """Mirrors core.mint_trusted_device but returns the row id too, so tests
    can assert on the specific row without needing the raw token's hash."""
    raw = await core.mint_trusted_device(talent_id)
    import hashlib
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    row = await _real_db.trusted_devices.find_one({"token_hash": token_hash}, {"_id": 0})
    return raw, row


async def _cleanup(talent_ids, device_ids=None):
    await _real_db.talents.delete_many({"id": {"$in": talent_ids}})
    await _real_db.trusted_devices.delete_many({"talent_id": {"$in": talent_ids}})
    if device_ids:
        await _real_db.trusted_devices.delete_many({"id": {"$in": device_ids}})


# --------------------------------------------------------------------------
# A1 — Normal (never-merged) talent: trusted device resolves normally.
# --------------------------------------------------------------------------
async def test_a1_normal_talent_trusted_device_resolves_normally():
    t = _talent("a1", name="Normal Talent", email="normal-td@example.com", normalized_email="normal-td@example.com")
    await _real_db.talents.insert_one(t)
    raw, row = await _mint_device(t["id"])
    try:
        peeked = await core.peek_trusted_device_talent(raw)
        assert peeked and peeked["id"] == t["id"]

        resolved = await core.resolve_trusted_device(raw)
        assert resolved and resolved["talent"]["id"] == t["id"]
        assert resolved["talent"]["email"] == "normal-td@example.com"
    finally:
        await _cleanup([t["id"]])


# --------------------------------------------------------------------------
# A2/A3 — Merged talent's stale trusted device follows merged_into and the
# canonical Talent returned has its correct (non-empty) email.
# --------------------------------------------------------------------------
async def test_a2_a3_merged_talent_trusted_device_follows_merged_into():
    canonical = _talent("a2c", name="Canonical Juhi", email="canon-td@example.com",
                         normalized_email="canon-td@example.com", alternate_emails=["absorbed-td@example.com"])
    absorbed = _talent("a2a", name="Absorbed Juhi", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    # The stale cookie is bound to the ABSORBED talent_id -- exactly the
    # real-world shape: minted before the merge happened.
    raw, row = await _mint_device(absorbed["id"])
    try:
        peeked = await core.peek_trusted_device_talent(raw)
        assert peeked is not None
        assert peeked["id"] == canonical["id"], "must follow merged_into, never return the absorbed record"
        assert peeked["email"] == "canon-td@example.com", "canonical Talent's real email must be present"

        resolved = await core.resolve_trusted_device(raw)
        assert resolved is not None
        assert resolved["talent"]["id"] == canonical["id"]
        assert resolved["talent"]["email"] == "canon-td@example.com"
    finally:
        await _cleanup([canonical["id"], absorbed["id"]])


# --------------------------------------------------------------------------
# A4 — Rotation must not keep re-minting a cookie for the absorbed id: the
# fresh cookie issued after a merged-talent recognition is bound to the
# CANONICAL id, so the device self-heals on its very next use.
# --------------------------------------------------------------------------
async def test_a4_rotation_stops_perpetuating_absorbed_id():
    canonical = _talent("a4c", name="Canonical Rotation", email="canon-rot@example.com", normalized_email="canon-rot@example.com")
    absorbed = _talent("a4a", name="Absorbed Rotation", email=None, normalized_email=None,
                        status="MERGED", merged_into=canonical["id"])
    await _real_db.talents.insert_many([canonical, absorbed])
    raw, row = await _mint_device(absorbed["id"])
    try:
        first = await core.resolve_trusted_device(raw)
        assert first["talent"]["id"] == canonical["id"]
        new_raw = first["new_raw_token"]

        # The just-rotated cookie must now be bound to the CANONICAL id in
        # the DB, not the absorbed one.
        import hashlib
        new_hash = hashlib.sha256(new_raw.encode()).hexdigest()
        new_row = await _real_db.trusted_devices.find_one({"token_hash": new_hash}, {"_id": 0})
        assert new_row is not None
        assert new_row["talent_id"] == canonical["id"], "rotation must mint the fresh cookie for the CANONICAL id, not re-perpetuate the absorbed one"

        # And using the NEW cookie again resolves straight to canonical,
        # with no further redirection needed.
        second = await core.resolve_trusted_device(new_raw)
        assert second["talent"]["id"] == canonical["id"]

        remaining_absorbed_devices = await _real_db.trusted_devices.count_documents(
            {"talent_id": absorbed["id"], "revoked": False}
        )
        assert remaining_absorbed_devices == 0, "no active device credential may remain bound to the absorbed id"
    finally:
        await _cleanup([canonical["id"], absorbed["id"]])


# --------------------------------------------------------------------------
# A5 — Existing trusted-device expiry/security behavior is unchanged:
# expired and revoked cookies are still rejected exactly as before.
# --------------------------------------------------------------------------
async def test_a5_expiry_and_revocation_unchanged():
    t = _talent("a5", name="Expiry Talent", email="expiry-td@example.com", normalized_email="expiry-td@example.com")
    await _real_db.talents.insert_one(t)
    raw, row = await _mint_device(t["id"])
    try:
        # Revoked cookie -> None.
        await _real_db.trusted_devices.update_one({"id": row["id"]}, {"$set": {"revoked": True}})
        assert await core.peek_trusted_device_talent(raw) is None
        assert await core.resolve_trusted_device(raw) is None

        # Expired cookie -> None.
        await _real_db.trusted_devices.update_one(
            {"id": row["id"]},
            {"$set": {"revoked": False, "expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
        )
        assert await core.peek_trusted_device_talent(raw) is None
        assert await core.resolve_trusted_device(raw) is None

        # Unknown/garbage token -> None.
        assert await core.peek_trusted_device_talent("not-a-real-token") is None
        assert await core.resolve_trusted_device(None) is None
    finally:
        await _cleanup([t["id"]])


# --------------------------------------------------------------------------
# Bonus — a MERGED talent whose merged_into target is itself missing (data
# corruption edge case) degrades safely: returns the original doc rather
# than raising or returning something worse.
# --------------------------------------------------------------------------
async def test_merged_into_target_missing_degrades_safely():
    orphan = _talent("orphan", name="Orphan Merge", email=None, normalized_email=None,
                      status="MERGED", merged_into=f"{_MTAG}nonexistent-id")
    await _real_db.talents.insert_one(orphan)
    raw, row = await _mint_device(orphan["id"])
    try:
        peeked = await core.peek_trusted_device_talent(raw)
        assert peeked is not None
        assert peeked["id"] == orphan["id"], "falls back to the original doc when the canonical target can't be found"
    finally:
        await _cleanup([orphan["id"]])
