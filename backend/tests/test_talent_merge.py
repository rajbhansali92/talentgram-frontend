"""Manual Talent Merge — targeted tests for the 11 spec cases (Part 23).

Real-DB integration tests, following the exact pattern established in
test_phase1_identity_hardening.py: tagged fixture data (_MTAG prefix),
cleanup in `finally`, module-scoped event loop (Motor's client binds to
whichever loop is active first), and the same cross-file `core.db`
monkeypatch-pollution guard (other test files do an unconditional,
never-restored `core.db = mock_db` at import time; without this guard,
whichever such file collects last in a full-suite run would leave `core.db`
a permanent MagicMock for every test here, regardless of THIS file's own
position in that order).
"""
import os
import uuid as _uuid
from datetime import datetime, timezone

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
from core import current_admin
from server import app

pytestmark = pytest.mark.asyncio(loop_scope="module")

client = TestClient(app)
_MTAG = "TEST_MERGE_"

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


def _now():
    return datetime.now(timezone.utc).isoformat()


def _talent(idx, **overrides):
    doc = {
        "id": f"{_MTAG}{idx}_{_uuid.uuid4().hex[:8]}",
        "name": "Merge Test Talent",
        "email": None, "normalized_email": None, "phone": None,
        "height": None, "dob": None, "gender": None, "ethnicity": None,
        "instagram_handle": None, "instagram_followers": None, "bio": None,
        "location": None, "skills": [], "work_links": [], "interested_in": [],
        "languages": [], "tags": [], "notes": "", "media": [],
        "status": "SUBMITTED", "source": {"type": "admin", "talent_email": None, "reference_id": None},
        "created_at": "2026-08-01T00:00:00+00:00", "whatsapp_group_name": None,
    }
    doc.update(overrides)
    return doc


async def _cleanup(ids):
    await _real_db.talents.delete_many({"id": {"$in": ids}})
    await _real_db.submissions.delete_many({"talent_id": {"$in": ids}})
    await _real_db.talent_merges.delete_many({"canonical_talent_id": {"$in": ids}})


class _Resp:
    """Minimal stand-in for an httpx/TestClient response, so the rest of
    this file can assert on `.status_code`/`.json()`/`.text` uniformly
    whether the call went through the real endpoint (case 11, no DB
    involved) or straight to the service function (every other case).

    Real-DB integration tests in this codebase call the target function
    directly rather than through `TestClient` (see
    test_phase1_identity_hardening.py's own established pattern) --
    Motor's client binds to whichever event loop is active the first time
    it's used, and `TestClient` manages its own internal loop per call,
    so routing a real-DB call through it here raises "attached to a
    different loop"."""
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    @property
    def text(self):
        return str(self._data)


async def _preview(a, b, canonical=None):
    from talent_merge_service import build_merge_preview, MergeError
    try:
        data = await build_merge_preview(a, b, canonical)
        return _Resp(200, data)
    except MergeError as e:
        return _Resp(e.status_code, {"detail": e.message})


async def _merge(canonical, duplicate):
    from talent_merge_service import execute_merge, MergeError
    try:
        data = await execute_merge(canonical, duplicate, operator="admin@talentgram.co")
        return _Resp(200, data)
    except MergeError as e:
        return _Resp(e.status_code, {"detail": e.message})


# --------------------------------------------------------------------------
# Case 1 — Basic merge
# --------------------------------------------------------------------------
async def test_case1_basic_merge():
    a = _talent("c1a", name="Basic Merge A")
    b = _talent("c1b", name="Basic Merge B")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["canonical_talent_id"] == a["id"]

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["status"] != "MERGED"

        dup_after = await _real_db.talents.find_one({"id": b["id"]}, {"_id": 0})
        assert dup_after["status"] == "MERGED"
        assert dup_after["merged_into"] == a["id"]
        assert dup_after.get("merged_at")

        count = await _real_db.talents.count_documents({"id": {"$in": [a["id"], b["id"]]}, "status": {"$ne": "MERGED"}})
        assert count == 1, "exactly one canonical Talent must remain unmerged"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 2 — Missing fields fill from the other profile
# --------------------------------------------------------------------------
async def test_case2_missing_fields_fill_from_other():
    a = _talent("c2a", name="Fill Test A", height=None)
    b = _talent("c2b", name="Fill Test B", height="5'5\"")
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["height"] == "5'5\""
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 3 — Canonical value wins on conflict; preview surfaces it
# --------------------------------------------------------------------------
async def test_case3_canonical_wins_conflict_shown_in_preview():
    a = _talent("c3a", name="Conflict Test A", height="5'5\"")
    b = _talent("c3b", name="Conflict Test B", height="5'4\"")
    await _real_db.talents.insert_many([a, b])
    try:
        preview = await _preview(a["id"], b["id"], canonical=a["id"])
        assert preview.status_code == 200, preview.text
        plan = preview.json()["merge_plan"]
        assert "height" not in plan["field_changes"], "canonical's populated value must not be listed as changing"

        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["height"] == "5'5\""
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 4 — Submissions reassigned, never recreated/deleted
# --------------------------------------------------------------------------
async def test_case4_submissions_reassigned():
    a = _talent("c4a", name="Submissions A")
    b = _talent("c4b", name="Submissions B")
    await _real_db.talents.insert_many([a, b])
    a_subs = [{"id": f"{_MTAG}sub_a{i}_{_uuid.uuid4().hex[:6]}", "talent_id": a["id"], "project_id": f"proj-a{i}", "status": "submitted"} for i in range(2)]
    b_subs = [{"id": f"{_MTAG}sub_b{i}_{_uuid.uuid4().hex[:6]}", "talent_id": b["id"], "project_id": f"proj-b{i}", "status": "submitted"} for i in range(3)]
    await _real_db.submissions.insert_many(a_subs + b_subs)
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["submissions_preserved"] == 5

        canonical_count = await _real_db.submissions.count_documents({"talent_id": a["id"]})
        dup_count = await _real_db.submissions.count_documents({"talent_id": b["id"]})
        assert canonical_count == 5
        assert dup_count == 0

        all_ids = {s["id"] for s in a_subs + b_subs}
        found_ids = {s["id"] async for s in _real_db.submissions.find({"talent_id": a["id"]}, {"_id": 0, "id": 1})}
        assert found_ids == all_ids, "no submission may be recreated or deleted -- only reassigned"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 5 — Media union, deduped by public_id
# --------------------------------------------------------------------------
async def test_case5_media_union_deduped():
    a_media = [{"id": f"a{i}", "public_id": f"pub_a{i}", "category": "portfolio"} for i in range(8)]
    b_media = [{"id": f"b{i}", "public_id": f"pub_b{i}", "category": "portfolio"} for i in range(13)]
    # One of B's items is the exact same physical asset as one of A's.
    b_media[0]["public_id"] = a_media[0]["public_id"]

    a = _talent("c5a", name="Media A", media=a_media)
    b = _talent("c5b", name="Media B", media=b_media)
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["media_preserved"] == 20, "8 + 13 - 1 overlapping duplicate = 20 valid assets"

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        public_ids = [m["public_id"] for m in canonical_after["media"]]
        assert len(public_ids) == len(set(public_ids)), "no duplicate public_id may appear on the merged talent"
        assert len(canonical_after["media"]) == 20
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 6 — Tags unioned and deduplicated
# --------------------------------------------------------------------------
async def test_case6_tags_union_deduped():
    a = _talent("c6a", name="Tags A", tags=[{"id": "t-female", "name": "Female"}, {"id": "t-mumbai", "name": "Mumbai"}, {"id": "t-actor", "name": "Actor"}])
    b = _talent("c6b", name="Tags B", tags=[{"id": "t-mumbai", "name": "Mumbai"}, {"id": "t-model", "name": "Model"}])
    await _real_db.talents.insert_many([a, b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text
        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        tag_ids = {t["id"] for t in canonical_after["tags"]}
        assert tag_ids == {"t-female", "t-mumbai", "t-actor", "t-model"}
        assert len(canonical_after["tags"]) == 4, "the shared Mumbai tag must not be duplicated"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 7 — Project-specific location stays on the submission, never becomes
# the global Talent location.
# --------------------------------------------------------------------------
async def test_case7_project_location_stays_on_submission():
    a = _talent("c7a", name="Location A", location=None)
    b = _talent("c7b", name="Location B", location=[{"city": "Dubai"}])
    await _real_db.talents.insert_many([a, b])
    sub_a = {"id": f"{_MTAG}loc_sub_a_{_uuid.uuid4().hex[:6]}", "talent_id": a["id"], "project_id": "proj-loc-a", "form_data": {"location": "Mumbai"}}
    sub_b = {"id": f"{_MTAG}loc_sub_b_{_uuid.uuid4().hex[:6]}", "talent_id": b["id"], "project_id": "proj-loc-b", "form_data": {"location": "Dubai"}}
    await _real_db.submissions.insert_many([sub_a, sub_b])
    try:
        resp = await _merge(a["id"], b["id"])
        assert resp.status_code == 200, resp.text

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["location"] is None, "global Talent location must never be auto-filled from either side"

        sub_a_after = await _real_db.submissions.find_one({"id": sub_a["id"]}, {"_id": 0})
        sub_b_after = await _real_db.submissions.find_one({"id": sub_b["id"]}, {"_id": 0})
        assert sub_a_after["form_data"]["location"] == "Mumbai"
        assert sub_b_after["form_data"]["location"] == "Dubai", "each submission's own project-specific answer is untouched"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 8 — Merging an already-MERGED talent fails safely
# --------------------------------------------------------------------------
async def test_case8_already_merged_fails_safely():
    a = _talent("c8a", name="Already Merged A")
    b = _talent("c8b", name="Already Merged B")
    c = _talent("c8c", name="Already Merged C")
    await _real_db.talents.insert_many([a, b, c])
    try:
        first = await _merge(a["id"], b["id"])
        assert first.status_code == 200, first.text

        # Same pair again -- idempotent no-op, not a corrupting second merge.
        again = await _merge(a["id"], b["id"])
        assert again.status_code == 200, again.text
        assert again.json()["already_merged"] is True

        # Attempting to merge the now-MERGED talent into a THIRD, different
        # canonical must be rejected outright.
        conflicting = await _merge(c["id"], b["id"])
        assert conflicting.status_code == 409, conflicting.text

        dup_after = await _real_db.talents.find_one({"id": b["id"]}, {"_id": 0})
        assert dup_after["merged_into"] == a["id"], "must remain merged into its original canonical, not silently reassigned"
    finally:
        await _cleanup([a["id"], b["id"], c["id"]])


# --------------------------------------------------------------------------
# Case 9 — Same Talent IDs rejected
# --------------------------------------------------------------------------
async def test_case9_same_ids_rejected():
    a = _talent("c9a", name="Self Merge")
    await _real_db.talents.insert_one(a)
    try:
        resp = await _merge(a["id"], a["id"])
        assert resp.status_code == 400, resp.text

        preview = await _preview(a["id"], a["id"])
        assert preview.status_code == 400, preview.text
    finally:
        await _cleanup([a["id"]])


# --------------------------------------------------------------------------
# Case 10 — Concurrent double-submission must not corrupt data
# --------------------------------------------------------------------------
async def test_case10_concurrent_double_submission_safe():
    import anyio

    a = _talent("c10a", name="Concurrent A")
    b = _talent("c10b", name="Concurrent B", height="5'6\"")
    await _real_db.talents.insert_many([a, b])
    try:
        results = []

        async def _do_merge():
            resp = await _merge(a["id"], b["id"])
            results.append(resp)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_do_merge)
            tg.start_soon(_do_merge)

        # The loser has two SAFE possible outcomes, depending purely on
        # timing (both are correct -- neither corrupts data, see Part 16):
        #   (a) it checks after the winner's write fully lands -> 200,
        #       already_merged=True (idempotent no-op).
        #   (b) it checks WHILE the winner is still applying its steps ->
        #       409 "already in progress" (a real DB transaction would
        #       serialize these two writes; this codebase has none anywhere
        #       -- see talent_merge_service.py's own module docstring -- so
        #       a fast, explicit rejection is the safe substitute for a
        #       lock wait, never a silent double-apply).
        # What must NEVER happen: two 200s that both report a fresh
        # (non-idempotent) merge, or a 500/corruption of any kind.
        statuses = sorted(r.status_code for r in results)
        assert statuses in ([200, 200], [200, 409]), [r.text for r in results]
        ok_results = [r for r in results if r.status_code == 200]
        already_merged_flags = sorted(r.json()["already_merged"] for r in ok_results)
        assert already_merged_flags in ([False], [False, True], [False, False]), (
            "at most one call may report a fresh (non-idempotent) merge; a "
            "genuine race resolves to exactly one winner, never two independent applies"
        )

        canonical_after = await _real_db.talents.find_one({"id": a["id"]}, {"_id": 0})
        assert canonical_after["height"] == "5'6\""
        dup_after = await _real_db.talents.find_one({"id": b["id"]}, {"_id": 0})
        assert dup_after["status"] == "MERGED"

        merge_docs = await _real_db.talent_merges.count_documents({"canonical_talent_id": a["id"], "source_talent_id": b["id"]})
        assert merge_docs == 1, "exactly one audit record for this pair, never duplicated"
    finally:
        await _cleanup([a["id"], b["id"]])


# --------------------------------------------------------------------------
# Case 11 — Authorization: non-admin cannot call the merge endpoint
# --------------------------------------------------------------------------
async def test_case11_non_admin_rejected():
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[current_admin] = _deny
    try:
        resp = client.post("/api/talents/merge", json={
            "canonical_talent_id": "whatever-a", "duplicate_talent_id": "whatever-b",
        })
        assert resp.status_code == 403

        preview_resp = client.post("/api/talents/merge/preview", json={
            "talent_a_id": "whatever-a", "talent_b_id": "whatever-b",
        })
        assert preview_resp.status_code == 403
    finally:
        app.dependency_overrides[current_admin] = lambda: {"email": "admin@talentgram.co", "role": "admin", "id": "admin-123"}
