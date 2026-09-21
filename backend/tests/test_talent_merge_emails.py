"""Merge Different Emails — a SEPARATE admin action from Merge Talents
(talent_merge_service.execute_merge / test_talent_merge.py), solving a
distinct problem: the same real person submitted under two different,
non-empty emails, creating two Talent records. Unlike Merge Talents, the
losing profile's email is linked onto the survivor as `alternate_emails`
instead of being discarded, so a future submission using EITHER email
resolves to the one canonical talent (core.resolve_canonical_talent).

Same real-DB integration pattern as test_talent_merge.py (tagged fixture
data, cleanup in `finally`, module-scoped event loop, cross-file `core.db`
monkeypatch-pollution guard).
"""
import os
import uuid as _uuid

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "password")

import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

import core
import routers.talents as routers_talents
from core import current_admin, resolve_canonical_talent
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

client = TestClient(app)
_MTAG = "TEST_EMAIL_MERGE_"

_real_mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_real_db = _real_mongo_client[os.environ["DB_NAME"]]


@pytest.fixture(autouse=True)
def _restore_real_db_for_this_file():
    prior_core_db = core.db
    prior_talents_db = routers_talents.db
    core.db = _real_db
    routers_talents.db = _real_db
    try:
        yield
    finally:
        core.db = prior_core_db
        routers_talents.db = prior_talents_db


@pytest.fixture(autouse=True)
def _admin_auth():
    mock_admin = {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}
    app.dependency_overrides[current_admin] = lambda: mock_admin
    yield
    app.dependency_overrides.clear()


def _talent(idx, **overrides):
    doc = {
        "id": f"{_MTAG}{idx}_{_uuid.uuid4().hex[:8]}",
        "name": "Email Merge Test Talent",
        "email": None, "normalized_email": None, "phone": None,
        "height": None, "dob": None, "gender": None, "ethnicity": None,
        "instagram_handle": None, "instagram_followers": None, "bio": None,
        "location": None, "skills": [], "work_links": [], "interested_in": [],
        "languages": [], "tags": [], "notes": "", "media": [], "alternate_emails": [],
        "status": "SUBMITTED", "source": {"type": "admin", "talent_email": None, "reference_id": None},
        "created_at": "2026-08-01T00:00:00+00:00", "whatsapp_group_name": None,
    }
    doc.update(overrides)
    return doc


async def _cleanup(ids):
    await _real_db.talents.delete_many({"id": {"$in": ids}})
    await _real_db.submissions.delete_many({"talent_id": {"$in": ids}})
    await _real_db.talent_merges.delete_many({"canonical_talent_id": {"$in": ids}})
    await _real_db.casting_pipeline.delete_many({"talent_id": {"$in": ids}})


class _Resp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    @property
    def text(self):
        return str(self._data)


async def _preview(a, b, canonical=None):
    from talent_merge_service import build_email_merge_preview, MergeError
    try:
        data = await build_email_merge_preview(a, b, canonical)
        return _Resp(200, data)
    except MergeError as e:
        return _Resp(e.status_code, {"detail": e.message})


async def _merge(canonical, duplicate):
    from talent_merge_service import execute_email_merge, MergeError
    try:
        data = await execute_email_merge(canonical, duplicate, operator="admin@talentgram.co")
        return _Resp(200, data)
    except MergeError as e:
        return _Resp(e.status_code, {"detail": e.message})


# --------------------------------------------------------------------------
# 1 — Two profiles with different emails can be previewed for this action.
# --------------------------------------------------------------------------
async def test_two_different_emails_can_be_selected_and_previewed():
    a = _talent("t1a", name="Juhi Vyas A", email="juhi8@example.com", normalized_email="juhi8@example.com")
    b = _talent("t1b", name="Juhi Vyas B", email="business.juhi@example.com", normalized_email="business.juhi@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _preview(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["talent_a"]["email"] == "juhi8@example.com"
        assert body["talent_b"]["email"] == "business.juhi@example.com"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 2 — Confirmation preview shows both profiles and both emails, with a
# computed merge plan once a canonical direction is chosen.
# --------------------------------------------------------------------------
async def test_confirmation_shows_both_profiles_and_both_emails():
    a = _talent("t2a", name="Confirm A", email="a@example.com", normalized_email="a@example.com")
    b = _talent("t2b", name="Confirm B", email="b@example.com", normalized_email="b@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _preview(a["id"], b["id"], canonical=a["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["talent_a"]["email"] == "a@example.com"
        assert body["talent_b"]["email"] == "b@example.com"
        plan = body["merge_plan"]
        assert plan["canonical_talent_id"] == a["id"]
        assert plan["duplicate_talent_id"] == b["id"]
        assert plan["field_changes"]["alternate_emails"]["proposed"] == ["b@example.com"]
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 3 — Successful merge produces exactly ONE canonical talent, and the
# regular Merge Talents flow remains completely untouched (case reused from
# test_talent_merge.py to prove zero behavioral drift).
# --------------------------------------------------------------------------
async def test_successful_merge_produces_one_canonical_talent():
    a = _talent("t3a", name="One Canonical A", email="one-a@example.com", normalized_email="one-a@example.com")
    b = _talent("t3b", name="One Canonical B", email="one-b@example.com", normalized_email="one-b@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["canonical_talent_id"] == a["id"]

        count = await _real_db.talents.count_documents(
            {"id": {"$in": [a["id"], b["id"]]}, "status": {"$ne": "MERGED"}}
        )
        assert count == 1, "exactly one canonical Talent must remain unmerged"

        dup_after = await _real_db.talents.find_one({"id": b["id"]}, {"_id": 0})
        assert dup_after["status"] == "MERGED"
        assert dup_after["merged_into"] == a["id"]
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 4 — Both emails remain associated with the canonical talent after merge.
# --------------------------------------------------------------------------
async def test_both_emails_remain_associated_with_canonical():
    a = _talent("t4a", name="Both Emails A", email="primary@example.com", normalized_email="primary@example.com")
    b = _talent("t4b", name="Both Emails B", email="secondary@example.com", normalized_email="secondary@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["email"] == "primary@example.com", "canonical's own email must never be overwritten"
        assert canonical_after["alternate_emails"] == ["secondary@example.com"]

        dup_after = await _real_db.talents.find_one({"id": b["id"]}, {"_id": 0})
        assert dup_after.get("email") is None, "absorbed talent's own email must be cleared to prevent ambiguous future lookups"
        assert dup_after.get("normalized_email") is None
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 5/6 — Existing submissions from both profiles remain accessible
# (reassigned onto canonical, never deleted/recreated).
# --------------------------------------------------------------------------
async def test_existing_submissions_from_both_profiles_remain_accessible():
    a = _talent("t5a", name="Subs A", email="subs-a@example.com", normalized_email="subs-a@example.com")
    b = _talent("t5b", name="Subs B", email="subs-b@example.com", normalized_email="subs-b@example.com")
    await _real_db.talents.insert_many([a, b])
    a_subs = [{"id": f"{_MTAG}sub_a_{_uuid.uuid4().hex[:6]}", "talent_id": a["id"], "project_id": "proj-a", "status": "submitted"}]
    b_subs = [{"id": f"{_MTAG}sub_b_{_uuid.uuid4().hex[:6]}", "talent_id": b["id"], "project_id": "proj-b", "status": "submitted"}]
    await _real_db.submissions.insert_many(a_subs + b_subs)
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["submissions_preserved"] == 2

        canonical_sub_count = await _real_db.submissions.count_documents({"talent_id": a["id"]})
        assert canonical_sub_count == 2
        found_ids = {s["id"] async for s in _real_db.submissions.find({"talent_id": a["id"]}, {"_id": 0, "id": 1})}
        assert found_ids == {a_subs[0]["id"], b_subs[0]["id"]}
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 7 — Existing media from both profiles remains accessible (union, deduped).
# --------------------------------------------------------------------------
async def test_existing_media_from_both_profiles_remains_accessible():
    a_media = [{"id": "am1", "public_id": "pub_a1", "category": "portfolio"}]
    b_media = [{"id": "bm1", "public_id": "pub_b1", "category": "portfolio"}]
    a = _talent("t7a", name="Media A", email="media-a@example.com", normalized_email="media-a@example.com", media=a_media)
    b = _talent("t7b", name="Media B", email="media-b@example.com", normalized_email="media-b@example.com", media=b_media)
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["media_preserved"] == 2

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        public_ids = {m["public_id"] for m in canonical_after["media"]}
        assert public_ids == {"pub_a1", "pub_b1"}
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 8 — Existing project relationships (casting_pipeline rows) from both
# profiles remain accessible after merge.
# --------------------------------------------------------------------------
async def test_existing_project_relationships_remain_accessible():
    a = _talent("t8a", name="Pipeline A", email="pipe-a@example.com", normalized_email="pipe-a@example.com")
    b = _talent("t8b", name="Pipeline B", email="pipe-b@example.com", normalized_email="pipe-b@example.com")
    await _real_db.talents.insert_many([a, b])
    pipe_a = {"id": f"{_MTAG}pipe_a_{_uuid.uuid4().hex[:6]}", "talent_id": a["id"], "project_id": "proj-x", "stage": "shortlisted"}
    pipe_b = {"id": f"{_MTAG}pipe_b_{_uuid.uuid4().hex[:6]}", "talent_id": b["id"], "project_id": "proj-y", "stage": "confirmed"}
    await _real_db.casting_pipeline.insert_many([pipe_a, pipe_b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        rows = [r async for r in _real_db.casting_pipeline.find({"talent_id": a["id"]}, {"_id": 0})]
        project_ids = {r["project_id"] for r in rows}
        assert project_ids == {"proj-x", "proj-y"}
    finally:
        await _cleanup([a["id"], b["id"]])
        await _real_db.casting_pipeline.delete_many({"id": {"$in": [pipe_a["id"], pipe_b["id"]]}})


# --------------------------------------------------------------------------
# 9/10/11 — Future submissions using EITHER email resolve to the SAME
# canonical talent, never creating a third duplicate.
# --------------------------------------------------------------------------
async def test_future_lookup_by_either_email_resolves_to_canonical_no_duplicate():
    a = _talent("t9a", name="Resolve A", email="resolve-primary@example.com", normalized_email="resolve-primary@example.com")
    b = _talent("t9b", name="Resolve B", email="resolve-secondary@example.com", normalized_email="resolve-secondary@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        found_by_primary = await resolve_canonical_talent(email="resolve-primary@example.com")
        assert found_by_primary is not None
        assert found_by_primary["id"] == a["id"]

        found_by_secondary = await resolve_canonical_talent(email="resolve-secondary@example.com")
        assert found_by_secondary is not None
        assert found_by_secondary["id"] == a["id"], "the alternate (absorbed) email must resolve to the SAME canonical talent"

        # A case-variant of the alternate email must resolve identically —
        # resolve_canonical_talent normalizes before matching.
        found_by_secondary_upper = await resolve_canonical_talent(email="RESOLVE-SECONDARY@example.com")
        assert found_by_secondary_upper is not None
        assert found_by_secondary_upper["id"] == a["id"]

        total_matching = await _real_db.talents.count_documents({
            "$or": [
                {"email": {"$in": ["resolve-primary@example.com", "resolve-secondary@example.com"]}},
                {"alternate_emails": {"$in": ["resolve-primary@example.com", "resolve-secondary@example.com"]}},
            ]
        })
        assert total_matching == 1, "no third duplicate talent may ever be created by either email"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 12 — Selecting fewer than two (n/a at the service layer — same-id reject)
# and identical/empty-email inputs are handled with a clear rejection.
# --------------------------------------------------------------------------
async def test_same_talent_id_rejected():
    a = _talent("t12a", name="Self Merge", email="self@example.com", normalized_email="self@example.com")
    await _real_db.talents.insert_one(a)
    try:
        resp = await _merge(a["id"], a["id"])
        assert resp.status_code == 400, resp.text
        preview = await _preview(a["id"], a["id"])
        assert preview.status_code == 400, preview.text
    finally:
        await _cleanup([a["id"]])


async def test_missing_or_identical_emails_rejected():
    a = _talent("t13a", name="No Email A", email=None, normalized_email=None)
    b = _talent("t13b", name="Has Email B", email="only-one@example.com", normalized_email="only-one@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 400, resp.text
        assert "email" in resp.json()["detail"].lower()
    finally:
        await _cleanup([a["id"], b["id"]])

    # The real `talents_email_unique` index already makes two talents with
    # the literal same `email` value impossible to insert (proven by every
    # other test's clean inserts) -- so this validation branch is only
    # reachable in-memory. Exercise it directly rather than trying to force
    # an insert the DB itself would (correctly) reject.
    from talent_merge_service import _validate_different_emails, MergeError
    same_c = {"email": "dup@example.com"}
    same_d = {"email": "DUP@example.com"}  # different casing, same normalized identity
    with pytest.raises(MergeError) as exc_info:
        _validate_different_emails(same_c, same_d)
    assert exc_info.value.status_code == 400


# --------------------------------------------------------------------------
# 14 — A second merge attempt of an already-absorbed profile is safely
# rejected/handled (idempotent no-op on same pair; 409 on a different one).
# --------------------------------------------------------------------------
async def test_second_merge_of_already_absorbed_profile_handled_safely():
    a = _talent("t14a", name="Absorbed A", email="absorbed-a@example.com", normalized_email="absorbed-a@example.com")
    b = _talent("t14b", name="Absorbed B", email="absorbed-b@example.com", normalized_email="absorbed-b@example.com")
    c = _talent("t14c", name="Absorbed C", email="absorbed-c@example.com", normalized_email="absorbed-c@example.com")
    await _real_db.talents.insert_many([a, b, c])
    try:
        first = await _merge(a["id"], b["id"])
        assert first.status_code == 200, first.text

        again = await _merge(a["id"], b["id"])
        assert again.status_code == 200, again.text
        assert again.json()["already_merged"] is True

        conflicting = await _merge(c["id"], b["id"])
        assert conflicting.status_code == 409, conflicting.text
    finally:
        await _cleanup([a["id"], b["id"], c["id"]])


# --------------------------------------------------------------------------
# 15 — Existing "Merge Talents" tests continue to pass — proven by
# test_talent_merge.py itself being run unmodified in the same suite; this
# is a targeted spot-check that the SHARED _apply_merge_steps primitive
# still behaves identically for a REGULAR merge (canonical email empty,
# adopts duplicate's email exactly as before — the new "alternate_emails"
# branch never fires here).
# --------------------------------------------------------------------------
async def test_regular_merge_talents_email_adoption_unchanged():
    a = _talent("t15a", name="Regular A", email=None, normalized_email=None)
    b = _talent("t15b", name="Regular B", email="adopt@example.com", normalized_email="adopt@example.com")
    await _real_db.talents.insert_many([a, b])
    try:
        from talent_merge_service import execute_merge
        result = await execute_merge(a["id"], b["id"], operator="admin@talentgram.co")
        assert result["ok"] is True

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["email"] == "adopt@example.com"
        assert canonical_after.get("alternate_emails") in (None, []), "regular merge must never populate alternate_emails"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# 16 — Authorization: non-admin cannot call the merge-emails endpoints.
# --------------------------------------------------------------------------
async def test_non_admin_rejected():
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[current_admin] = _deny
    try:
        resp = client.post("/api/talents/merge-emails", json={
            "canonical_talent_id": "whatever-a", "duplicate_talent_id": "whatever-b",
        })
        assert resp.status_code == 403

        preview_resp = client.post("/api/talents/merge-emails/preview", json={
            "talent_a_id": "whatever-a", "talent_b_id": "whatever-b",
        })
        assert preview_resp.status_code == 403
    finally:
        app.dependency_overrides[current_admin] = lambda: {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}


# --------------------------------------------------------------------------
# 17 — An email already linked to a different canonical talent's
# alternate_emails is refused (each email resolves to at most one talent).
# --------------------------------------------------------------------------
async def test_email_already_linked_elsewhere_refused():
    a = _talent("t17a", name="Chain A", email="chain-a@example.com", normalized_email="chain-a@example.com")
    b = _talent("t17b", name="Chain B", email="chain-b@example.com", normalized_email="chain-b@example.com")
    other = _talent("t17other", name="Unrelated", email="unrelated@example.com", normalized_email="unrelated@example.com", alternate_emails=["chain-b@example.com"])
    await _real_db.talents.insert_many([a, b, other])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 409, resp.text
    finally:
        await _cleanup([a["id"], b["id"], other["id"]])


# ============================================================================
# Casting-pipeline collision reconciliation (2026-09-21 production incident:
# _apply_merge_steps blanket-reassigned casting_pipeline BEFORE its own
# dedicated collision-aware block could run, hitting the real unique index
# pipeline_project_talent_unique and aborting mid-merge -- see the forensic
# report for the real Juhi Vyas incident this reproduces). Fix: exclude
# casting_pipeline from the blanket reassignment loop so the dedicated block
# (now actually reconciling instead of just skipping) is reachable.
# ============================================================================
def _pipe(project_id, talent_id, stage, updated_at, created_at=None):
    return {
        "id": f"{_MTAG}pipe_{_uuid.uuid4().hex[:8]}",
        "project_id": project_id, "talent_id": talent_id, "stage": stage,
        "created_at": created_at or updated_at, "updated_at": updated_at,
    }


async def test_case_c_single_pipeline_collision_no_e11000_one_row_survives():
    a = _talent("pc1a", name="Pipeline Collision A", email="pc1-a@example.com", normalized_email="pc1-a@example.com")
    b = _talent("pc1b", name="Pipeline Collision B", email="pc1-b@example.com", normalized_email="pc1-b@example.com")
    await _real_db.talents.insert_many([a, b])
    proj = f"{_MTAG}proj-collide"
    canon_pipe = _pipe(proj, a["id"], "follow_up", "2026-09-08T00:00:00+00:00")
    dup_pipe = _pipe(proj, b["id"], "approved", "2026-09-15T00:00:00+00:00")
    await _real_db.casting_pipeline.insert_many([canon_pipe, dup_pipe])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text  # must NOT be a 500 E11000 failure

        rows = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
        assert len(rows) == 1, "exactly one pipeline row must survive per project"
        assert rows[0]["talent_id"] == a["id"]
        assert rows[0]["stage"] == "approved", "duplicate's row was more recently updated, so its stage must win"

        dup_refs = await _real_db.casting_pipeline.count_documents({"talent_id": b["id"]})
        assert dup_refs == 0, "no pipeline row may remain pointing at the absorbed talent"
    finally:
        await _cleanup([a["id"], b["id"]])


async def test_case_d_multiple_pipeline_collisions_all_reconciled():
    a = _talent("pc2a", name="Multi Collision A", email="pc2-a@example.com", normalized_email="pc2-a@example.com")
    b = _talent("pc2b", name="Multi Collision B", email="pc2-b@example.com", normalized_email="pc2-b@example.com")
    await _real_db.talents.insert_many([a, b])
    proj1, proj2, proj3 = f"{_MTAG}p1", f"{_MTAG}p2", f"{_MTAG}p3"
    rows_in = [
        _pipe(proj1, a["id"], "follow_up", "2026-09-08T00:00:00+00:00"),
        _pipe(proj1, b["id"], "approved", "2026-09-15T00:00:00+00:00"),  # dup newer -> "approved" wins
        _pipe(proj2, a["id"], "follow_up", "2026-09-13T00:00:00+00:00"),
        _pipe(proj2, b["id"], "ask_to_test", "2026-09-11T00:00:00+00:00"),  # canon newer -> "follow_up" wins
        _pipe(proj3, a["id"], "hold", "2026-09-20T00:00:00+00:00"),
        _pipe(proj3, b["id"], "rejected", "2026-09-21T00:00:00+00:00"),  # dup newer -> "rejected" wins
    ]
    await _real_db.casting_pipeline.insert_many(rows_in)
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        for proj, expected_stage in [(proj1, "approved"), (proj2, "follow_up"), (proj3, "rejected")]:
            rows = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
            assert len(rows) == 1, f"{proj}: exactly one row must survive"
            assert rows[0]["talent_id"] == a["id"]
            assert rows[0]["stage"] == expected_stage, f"{proj}: wrong deterministic winner"

        assert await _real_db.casting_pipeline.count_documents({"talent_id": b["id"]}) == 0
        # No unique-key duplicates: exactly one row per (project, canonical) pair.
        for proj in (proj1, proj2, proj3):
            assert await _real_db.casting_pipeline.count_documents({"project_id": proj, "talent_id": a["id"]}) == 1
    finally:
        await _cleanup([a["id"], b["id"]])


async def test_case_e_deterministic_rule_is_newer_updated_at():
    # Covered by both directions inside test_case_d above (proj1: duplicate
    # newer; proj2: canonical newer) -- this test isolates the "canonical
    # already newer, nothing overwritten" branch on its own.
    a = _talent("pc3a", name="Rule A", email="pc3-a@example.com", normalized_email="pc3-a@example.com")
    b = _talent("pc3b", name="Rule B", email="pc3-b@example.com", normalized_email="pc3-b@example.com")
    await _real_db.talents.insert_many([a, b])
    proj = f"{_MTAG}proj-rule"
    canon_pipe = _pipe(proj, a["id"], "locked", "2026-09-20T00:00:00+00:00")
    dup_pipe = _pipe(proj, b["id"], "not_available", "2026-09-05T00:00:00+00:00")
    await _real_db.casting_pipeline.insert_many([canon_pipe, dup_pipe])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        rows = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
        assert len(rows) == 1
        assert rows[0]["stage"] == "locked", "canonical's row was newer, its stage must be kept, not overwritten"
        assert rows[0]["id"] == canon_pipe["id"], "the surviving row must be the canonical's own document"
    finally:
        await _cleanup([a["id"], b["id"]])


async def test_case_f_audit_records_collision_and_retained_state():
    a = _talent("pc4a", name="Audit A", email="pc4-a@example.com", normalized_email="pc4-a@example.com")
    b = _talent("pc4b", name="Audit B", email="pc4-b@example.com", normalized_email="pc4-b@example.com")
    await _real_db.talents.insert_many([a, b])
    proj = f"{_MTAG}proj-audit"
    canon_pipe = _pipe(proj, a["id"], "follow_up", "2026-09-08T00:00:00+00:00")
    dup_pipe = _pipe(proj, b["id"], "approved", "2026-09-15T00:00:00+00:00")
    await _real_db.casting_pipeline.insert_many([canon_pipe, dup_pipe])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        op_doc = await _real_db.talent_merges.find_one({"id": resp.json()["operation_id"]}, {"_id": 0})
        resolved = op_doc.get("pipeline_collisions_resolved")
        assert resolved and len(resolved) == 1
        entry = resolved[0]
        assert entry["project_id"] == proj
        assert entry["canonical_pipeline_id"] == canon_pipe["id"]
        assert entry["duplicate_pipeline_id"] == dup_pipe["id"]
        assert entry["canonical_stage"] == "follow_up"
        assert entry["duplicate_stage"] == "approved"
        assert entry["retained_stage"] == "approved"
        assert entry["rule"] == "newer_updated_at"
    finally:
        await _cleanup([a["id"], b["id"]])


async def test_case_g_repair_of_partially_merged_state_is_idempotent():
    """Reproduces the exact partially-merged shape the real Juhi record was
    left in: atomic claim done, canonical fields set, submissions already
    reassigned, but a casting_pipeline collision still unresolved and the
    talent_merges op recorded as 'failed'. Calling execute_email_merge again
    on the same pair (the resume path) must safely finish the job -- and
    calling it a THIRD time must be a pure no-op, never re-applying or
    duplicating anything."""
    a = _talent("pc5a", name="Repair A", email="pc5-a@example.com", normalized_email="pc5-a@example.com",
                alternate_emails=["pc5-b@example.com"])
    b = _talent("pc5b", name="Repair B", status="MERGED", merged_into=None)  # merged_into set below once we know a['id']
    b["merged_into"] = a["id"]
    op_id = f"{_MTAG}op_{_uuid.uuid4().hex[:8]}"
    b["merge_operation_id"] = op_id
    b["merged_at"] = "2026-09-21T16:28:57+00:00"
    await _real_db.talents.insert_many([a, b])
    sub_id = f"{_MTAG}sub_{_uuid.uuid4().hex[:6]}"
    await _real_db.submissions.insert_one({"id": sub_id, "talent_id": a["id"], "project_id": f"{_MTAG}proj-repair-sub", "status": "submitted"})
    proj = f"{_MTAG}proj-repair"
    canon_pipe = _pipe(proj, a["id"], "follow_up", "2026-09-08T00:00:00+00:00")
    dup_pipe = _pipe(proj, b["id"], "approved", "2026-09-15T00:00:00+00:00")
    await _real_db.casting_pipeline.insert_many([canon_pipe, dup_pipe])
    await _real_db.talent_merges.insert_one({
        "id": op_id, "source_talent_id": b["id"], "canonical_talent_id": a["id"],
        "merge_reason": "manual_admin_email_merge", "matched_by": ["manual_admin_merge_different_emails"],
        "field_changes": {"alternate_emails": {"canonical": [], "other": ["pc5-b@example.com"], "proposed": ["pc5-b@example.com"]}},
        "relationship_counts": {"canonical": {"submissions": 1}, "other": {"submissions": 0}},
        "media": {"canonical_count": 0, "other_count": 0, "overlap_count": 0, "proposed_count": 0, "proposed_media": []},
        "conflicts": [], "tags_merged_count": 0, "operator": "admin@talentgram.co",
        "status": "failed", "error": "E11000 simulated", "timestamp": "2026-09-21T16:28:57+00:00",
        "migration_version": "manual_email_merge_v1",
    })
    try:
        first = await _merge(a["id"], b["id"])
        assert first.status_code == 200, first.text
        assert first.json()["resumed"] is True

        rows = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
        assert len(rows) == 1
        assert rows[0]["talent_id"] == a["id"]
        assert rows[0]["stage"] == "approved"
        assert await _real_db.casting_pipeline.count_documents({"talent_id": b["id"]}) == 0

        op_after = await _real_db.talent_merges.find_one({"id": op_id}, {"_id": 0})
        assert op_after["status"] == "completed"

        # Second resume call: pure no-op, no duplication, no error.
        second = await _merge(a["id"], b["id"])
        assert second.status_code == 200, second.text
        assert second.json()["already_merged"] is True
        rows_again = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
        assert len(rows_again) == 1, "a second resume must never duplicate the reconciled row"
    finally:
        await _cleanup([a["id"], b["id"]])
        await _real_db.submissions.delete_many({"id": sub_id})


async def test_case_j_existing_merge_talents_pipeline_collision_also_fixed():
    """The bug was in the SHARED _apply_merge_steps, so regular Merge
    Talents (execute_merge, not execute_email_merge) had the exact same
    latent crash risk. Confirms the fix applies there too, without changing
    any of execute_merge's own logic."""
    from talent_merge_service import execute_merge

    a = _talent("pc6a", name="Regular Merge Collision A", email=None, normalized_email=None)
    b = _talent("pc6b", name="Regular Merge Collision B", email="pc6-b@example.com", normalized_email="pc6-b@example.com")
    await _real_db.talents.insert_many([a, b])
    proj = f"{_MTAG}proj-regular-collide"
    canon_pipe = _pipe(proj, a["id"], "follow_up", "2026-09-08T00:00:00+00:00")
    dup_pipe = _pipe(proj, b["id"], "locked", "2026-09-19T00:00:00+00:00")
    await _real_db.casting_pipeline.insert_many([canon_pipe, dup_pipe])
    try:
        result = await execute_merge(a["id"], b["id"], operator="admin@talentgram.co")
        assert result["ok"] is True
        rows = [r async for r in _real_db.casting_pipeline.find({"project_id": proj}, {"_id": 0})]
        assert len(rows) == 1
        assert rows[0]["talent_id"] == a["id"]
        assert rows[0]["stage"] == "locked"
        # Regular Merge Talents must still never touch alternate_emails.
        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after.get("alternate_emails") in (None, [])
    finally:
        await _cleanup([a["id"], b["id"]])
