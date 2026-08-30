"""LAYER 2 — P3 ownership-migration integration tests.

Verifies that the migration correctly FEEDS documents into the pure classifier
and WRITES the result back:

    MongoDB documents  ->  do_migrate()  ->  media[i].ownership written
                                          ->  p3_ownership_migration_backup

Uses an in-memory async Mongo (``mongomock_motor``) — no real database, no
network. The pure classification logic itself is covered by
``test_media_classification.py``; this file only checks the plumbing:
snapshot-before-write, idempotency, conflict handling, dry-run, and rollback.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# core.py hard-reads these at import; give it throwaway values so the module
# imports without a real environment. No connection is made until first use,
# and every DB reference is patched to the in-memory mock below.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test")
os.environ.setdefault("CLOUDINARY_API_KEY", "test")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test")

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

import core  # noqa: E402
from migrations import p3_media_ownership as mig  # noqa: E402


@pytest.fixture
def mock_db(monkeypatch):
    client = AsyncMongoMockClient()
    db = client["test"]
    monkeypatch.setattr(mig, "db", db)
    monkeypatch.setattr(core, "db", db)
    return db


async def _seed(db):
    """3 healthy docs + 1 abandoned draft (the P3 UNKNOWN condition)."""
    await db.talents.insert_one({
        "id": "talent-1", "media": [
            {"id": "tm1", "category": "portfolio", "public_id": "talentgram/talents/talent-1/portfolio/tm1",
             "resource_type": "image", "size": 100},
            {"id": "tm2", "category": "video", "public_id": "talentgram/talents/talent-1/intro_video/tm2",
             "resource_type": "video", "size": 200},
        ],
    })
    await db.submissions.insert_one({
        "id": "sub-1", "project_id": "proj-1", "talent_id": "talent-1", "media": [
            {"id": "sm1", "category": "western", "public_id": "talentgram/submissions/sub-1/sm1",
             "resource_type": "image", "size": 50, "source_talent_media_id": "tm1"},
            {"id": "sm2", "category": "take", "public_id": "talentgram/projects/proj-1/auditions/x/submission_sub-1/take_abcd1234",
             "resource_type": "video", "size": 300},
        ],
    })
    await db.applications.insert_one({
        "id": "app-1", "talent_id": "talent-2", "media": [
            {"id": "am1", "category": "indian", "public_id": "talentgram/applications/app-1/am1",
             "resource_type": "image", "size": 25},
        ],
    })
    # abandoned draft — media but no talent_id, no talent_email -> UNKNOWN
    await db.applications.insert_one({
        "id": "app-draft", "status": "draft", "media": [
            {"id": "dm1", "category": "intro_video", "public_id": "talentgram/applications/app-draft/dm1",
             "resource_type": "video", "size": 10},
        ],
    })


async def _all_media(db):
    out = {}
    for coll in ("talents", "submissions", "applications"):
        async for d in db[coll].find({}):
            for m in d.get("media") or []:
                out[m["id"]] = m
    return out


# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_writes_ownership_to_every_item(mock_db):
    await _seed(mock_db)
    report = await mig.do_migrate(apply=True)

    media = await _all_media(mock_db)
    assert set(media) == {"tm1", "tm2", "sm1", "sm2", "am1", "dm1"}
    for m in media.values():
        assert isinstance(m.get("ownership"), dict)
        assert m["ownership"]["migration_version"] == mig.VERSION

    assert media["tm1"]["ownership"]["owner_type"] == "talent"
    assert media["tm1"]["ownership"]["owner_id"] == "talent-1"
    assert media["sm1"]["ownership"]["owner_type"] == "talent"
    assert media["sm1"]["ownership"]["is_shared_copy"] is True          # source_talent_media_id
    assert media["sm2"]["ownership"]["owner_type"] == "project_submission"
    assert media["sm2"]["ownership"]["owner_id"] == "sub-1"
    assert media["sm2"]["ownership"]["project_id"] == "proj-1"
    assert media["am1"]["ownership"]["owner_type"] == "talent"
    assert media["am1"]["ownership"]["owner_id"] == "talent-2"

    # report plumbing
    assert report["totals"]["media_items_total"] == 6
    assert report["totals"]["items_assigned_owner_type"] == 5
    assert report["totals"]["items_conflict_left_unassigned"] == 1
    assert report["assignable_GLOBAL_TALENT_MEDIA"] == 4
    assert report["assignable_PROJECT_AUDITION_MEDIA"] == 1


@pytest.mark.asyncio
async def test_abandoned_draft_left_unknown_not_guessed(mock_db):
    await _seed(mock_db)
    report = await mig.do_migrate(apply=True)

    dm1 = (await _all_media(mock_db))["dm1"]
    assert dm1["ownership"]["owner_type"] is None
    assert dm1["ownership"]["owner_id"] is None
    assert dm1["ownership"]["conflict"] == "talent-owned item with no resolvable talent_id"

    assert report["remaining_UNKNOWN_conflict"] == 1
    assert report["detail"]["conflicts"][0]["doc_id"] == "app-draft"
    assert report["detail"]["conflicts"][0]["media_id"] == "dm1"


@pytest.mark.asyncio
async def test_migration_snapshots_media_before_writing(mock_db):
    await _seed(mock_db)
    before = await _all_media(mock_db)
    before_ids_no_ownership = {mid: ("ownership" not in m) for mid, m in before.items()}
    assert all(before_ids_no_ownership.values())  # sanity: no ownership yet

    await mig.do_migrate(apply=True)

    backups = [b async for b in mock_db[mig.BACKUP_COLL].find({})]
    assert len(backups) == 4  # one per document that had media
    by_doc = {(b["collection"], b["doc_id"]): b for b in backups}
    assert set(by_doc) == {
        ("talents", "talent-1"), ("submissions", "sub-1"),
        ("applications", "app-1"), ("applications", "app-draft"),
    }
    # the snapshot is the PRE-migration array (no `ownership` key on any item)
    for b in backups:
        for m in b["media_before"]:
            assert "ownership" not in m


@pytest.mark.asyncio
async def test_migration_is_idempotent(mock_db):
    await _seed(mock_db)
    await mig.do_migrate(apply=True)
    second = await mig.do_migrate(apply=True)

    assert second["totals"]["items_assigned_owner_type"] == 0
    assert second["totals"]["items_conflict_left_unassigned"] == 0
    assert second["totals"]["items_skipped_already_migrated"] == 6
    assert second["totals"]["documents_touched"] == 0
    # backup collection not doubled
    assert await mock_db[mig.BACKUP_COLL].count_documents({}) == 4


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(mock_db):
    await _seed(mock_db)
    report = await mig.do_migrate(apply=False)

    for m in (await _all_media(mock_db)).values():
        assert "ownership" not in m
    assert await mock_db[mig.BACKUP_COLL].count_documents({}) == 0
    # but the report still computes the full picture
    assert report["totals"]["items_assigned_owner_type"] == 5
    assert report["totals"]["items_conflict_left_unassigned"] == 1


@pytest.mark.asyncio
async def test_rollback_restores_exact_pre_migration_state(mock_db):
    await _seed(mock_db)
    original = await _all_media(mock_db)
    original_snapshot = {mid: dict(m) for mid, m in original.items()}

    await mig.do_migrate(apply=True)
    assert all("ownership" in m for m in (await _all_media(mock_db)).values())

    await mig.do_rollback()

    restored = await _all_media(mock_db)
    assert set(restored) == set(original_snapshot)
    for mid, m in restored.items():
        assert "ownership" not in m
        assert m == original_snapshot[mid]   # byte-for-byte identical to pre-migration
    # backup collection is dropped after a successful rollback
    assert await mock_db[mig.BACKUP_COLL].count_documents({}) == 0


@pytest.mark.asyncio
async def test_migration_does_not_alter_any_existing_field(mock_db):
    await _seed(mock_db)
    before = await _all_media(mock_db)
    before_snapshot = {mid: dict(m) for mid, m in before.items()}

    await mig.do_migrate(apply=True)

    after = await _all_media(mock_db)
    for mid, m in after.items():
        stripped = {k: v for k, v in m.items() if k != "ownership"}
        assert stripped == before_snapshot[mid], f"{mid}: an existing field changed"


@pytest.mark.asyncio
async def test_take_never_becomes_talent_owned_through_the_migration(mock_db):
    """End-to-end regression for the 'audition media must not become global' rule."""
    await mock_db.submissions.insert_one({
        "id": "sub-x", "project_id": "proj-x", "talent_id": "talent-present", "media": [
            {"id": "t1", "category": "take_1", "public_id": "talentgram/projects/proj-x/auditions/y/submission_sub-x/take_1",
             "resource_type": "video", "size": 1},
        ],
    })
    await mig.do_migrate(apply=True)
    t1 = (await _all_media(mock_db))["t1"]
    assert t1["ownership"]["owner_type"] == "project_submission"
    assert t1["ownership"]["owner_id"] == "sub-x"
    assert t1["ownership"]["talent_id"] == "talent-present"  # recorded, not owning
