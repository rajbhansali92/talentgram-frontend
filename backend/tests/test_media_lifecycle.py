"""P6 — media lifecycle & deletion-safety service.

The safety gate decides deletion from the MongoDB ownership + reference graph,
never from the Cloudinary folder. These tests pin the architectural rules:

  * global talent media survives project removal / project deletion / submission
    deletion / one lost reference
  * project audition media -> PENDING_DELETION, eligible only after retention
  * unknown / conflicting ownership -> PROTECT
  * folder path can never override database ownership
  * Cloudinary destroy is never called when the gate fails, and never twice

Uses a tiny in-memory async Mongo fake (mongomock_motor is not installed here).
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "talentgram")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")
sys.path.insert(0, os.path.abspath("backend"))

import media_lifecycle as ml  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tiny in-memory async Mongo fake
# ---------------------------------------------------------------------------
def _match(doc, query):
    for k, v in query.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        cur = doc
        for part in k.split("."):
            if isinstance(cur, list):
                cur = [
                    (x.get(part) if isinstance(x, dict) else None) for x in cur
                ]
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
        if isinstance(v, dict) and "$in" in v:
            vals = cur if isinstance(cur, list) else [cur]
            if not any(x in v["$in"] for x in vals):
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if cur == v["$ne"]:
                return False
        elif isinstance(v, dict) and "$exists" in v:
            present = cur is not None
            if present != v["$exists"]:
                return False
        else:
            vals = cur if isinstance(cur, list) else [cur]
            if v not in vals:
                return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

    async def to_list(self, n=None):
        return list(self._docs)


class _Coll:
    def __init__(self, name):
        self.name = name
        self.docs = []

    def find(self, query=None, projection=None):
        query = query or {}
        return _Cursor([d for d in self.docs if _match(d, query)])

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _match(d, query or {}):
                return d
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _match(d, query or {}))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _match(d, query):
                if "$set" in update:
                    for k, v in update["$set"].items():
                        if k.startswith("media.$."):
                            field = k[len("media.$."):]
                            mid = query.get("media.id")
                            for m in d.get("media", []):
                                if m.get("id") == mid:
                                    m[field] = v
                        else:
                            d[k] = v
                return type("R", (), {"modified_count": 1})()
        if upsert:
            doc = {kk: vv for kk, vv in query.items() if "." not in kk and not isinstance(vv, dict)}
            doc.update(update.get("$set", {}))
            self.docs.append(doc)
            return type("R", (), {"modified_count": 0, "upserted_id": 1})()
        return type("R", (), {"modified_count": 0})()

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": 1})()


class _DB:
    def __init__(self):
        self._colls = {}
        for n in ("talents", "submissions", "applications", "links",
                  "casting_pipeline", "app_config", "pending_media_deletions"):
            self._colls[n] = _Coll(n)
            setattr(self, n, self._colls[n])

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _Coll(name)
            setattr(self, name, self._colls[name])
        return self._colls[name]


def _talent_media(mid="m-glob", public_id="talentgram/x/glob"):
    return {
        "id": mid, "public_id": public_id, "resource_type": "image", "category": "portfolio",
        "url": f"https://res.cloudinary.com/talentgram/image/upload/v1/{public_id}.jpg",
        "ownership": {"owner_type": "talent", "owner_id": "T1", "talent_id": "T1",
                      "is_shared_copy": False, "conflict": None},
    }


def _take_media(mid="m-take", public_id="talentgram/projects/P1/take", lifecycle=None):
    m = {
        "id": mid, "public_id": public_id, "resource_type": "video", "category": "take",
        "url": f"https://res.cloudinary.com/talentgram/video/upload/v1/{public_id}.mp4",
        "ownership": {"owner_type": "project_submission", "owner_id": "S1", "talent_id": "T1",
                      "project_id": "P1", "submission_id": "S1", "is_shared_copy": False, "conflict": None},
    }
    if lifecycle:
        m["lifecycle"] = lifecycle
    return m


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def db():
    return _DB()


# ---------------------------------------------------------------------------
# 1–3  Global talent media is never collaterally deletable
# ---------------------------------------------------------------------------
def test_1_global_media_survives_project_removal(db):
    # scenario: project P1 is being torn down; one of its submissions carried a
    # copy-by-value of talent T1's global portfolio photo. Evaluate that
    # submission-side copy — T1's library still owns the asset.
    db.talents.docs.append({"id": "T1", "name": "Tal", "media": [_talent_media()]})
    sub_copy = {**_talent_media(mid="sub-copy"), "source_talent_media_id": "m-glob"}
    d = run(ml.can_delete(
        db, sub_copy,
        ctx=ml.DeletionContext(project_deletion="P1", now=NOW,
                               exclude_collection="submissions", exclude_parent_id="S1")))
    assert not d.deletable
    assert any(x.kind == "talent" and x.id == "T1" and x.protects for x in d.dependencies)


def test_2_global_media_survives_project_deletion(db):
    db.talents.docs.append({"id": "T1", "name": "Tal", "media": [_talent_media()]})
    # even if a (now soft-deleted) submission also referenced the same asset
    db.submissions.docs.append({"id": "S9", "project_id": "P1", "deleted_at": NOW.isoformat(),
                                "media": [_talent_media()]})
    d = run(ml.can_delete(db, _talent_media(), ctx=ml.DeletionContext(project_deletion="P1", now=NOW)))
    assert not d.deletable


def test_3_shared_global_media_stays_protected(db):
    # same public_id on two talents
    db.talents.docs.append({"id": "T1", "name": "A", "media": [_talent_media()]})
    db.talents.docs.append({"id": "T2", "name": "B", "media": [_talent_media(mid="m2")]})
    d = run(ml.can_delete(db, _talent_media(),
                          ctx=ml.DeletionContext(talent_hard_delete="T1", now=NOW)))
    assert not d.deletable
    assert any(x.kind == "talent" and x.id == "T2" for x in d.dependencies)


# ---------------------------------------------------------------------------
# 4–6  Project audition media + retention
# ---------------------------------------------------------------------------
def test_4_audition_becomes_pending_deletion(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "deleted_at": NOW.isoformat(),
                                "media": [_take_media()]})
    d = run(ml.can_delete(db, _take_media(), ctx=ml.DeletionContext(submission_deletion="S1", now=NOW),
                          retention_days=30))
    assert not d.deletable
    assert d.state == ml.STATE_PENDING_DELETION


def test_5_retention_prevents_early_deletion(db):
    lc = {"state": ml.STATE_PENDING_DELETION, "marked_at": NOW.isoformat(), "retention_days": 30}
    m = _take_media(lifecycle=lc)
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "deleted_at": NOW.isoformat(), "media": [m]})
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(now=NOW + timedelta(days=10)), retention_days=30))
    assert not d.deletable and d.state == ml.STATE_PENDING_DELETION


def test_6_retention_expiry_permits_deletion_when_unreferenced(db):
    lc = {"state": ml.STATE_PENDING_DELETION, "marked_at": NOW.isoformat(), "retention_days": 30}
    m = _take_media(lifecycle=lc)
    # submission soft-deleted (does not protect); no other refs
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "deleted_at": NOW.isoformat(), "media": [m]})
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(
        now=NOW + timedelta(days=31), exclude_collection="submissions", exclude_parent_id="S1"),
        retention_days=30))
    assert d.deletable and d.state == ml.STATE_DELETED


# ---------------------------------------------------------------------------
# 7–10  Reference / ownership protections
# ---------------------------------------------------------------------------
def test_7_active_reference_prevents_deletion(db):
    m = _take_media()
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [m]})  # live, not soft-deleted
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(now=NOW), retention_days=0))
    assert not d.deletable
    assert any(x.protects for x in d.dependencies)


def test_8_historical_protected_reference_prevents_deletion(db):
    # removing this take from S1, but a DIFFERENT live submission S2 reused the
    # same public_id (Media Library reuse) — the physical asset must survive.
    m = _take_media()
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [m]})
    db.submissions.docs.append({"id": "S2", "project_id": "P2", "media": [_take_media(mid="m-take-2")]})
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(
        now=NOW, exclude_collection="submissions", exclude_parent_id="S1"), retention_days=0))
    assert not d.deletable
    assert any(x.kind == "submission" and x.id == "S2" for x in d.dependencies)


def test_9_unknown_ownership_prevents_deletion(db):
    m = {"id": "mu", "public_id": "talentgram/legacy/thing", "resource_type": "image", "category": "mystery"}
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(now=NOW)))
    assert not d.deletable
    assert "unknown" in d.reason.lower()


def test_10_conflicting_ownership_prevents_deletion(db):
    m = _talent_media()
    m["ownership"] = {**m["ownership"], "owner_type": None, "conflict": "two_categories"}
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(talent_hard_delete="T1", now=NOW)))
    assert not d.deletable
    assert "conflict" in d.reason.lower()


# ---------------------------------------------------------------------------
# 11  Folder can never override DB ownership
# ---------------------------------------------------------------------------
def test_11_folder_path_cannot_override_db_ownership(db):
    # public_id sits UNDER a project folder, but P3 ownership says it is a
    # talent-owned global asset (copy-by-value). Folder must be ignored.
    m = {
        "id": "mf", "public_id": "talentgram/projects/P1/submissions/S1/glob-photo",
        "resource_type": "image", "category": "portfolio",
        "url": "https://res.cloudinary.com/talentgram/image/upload/v1/talentgram/projects/P1/submissions/S1/glob-photo.jpg",
        "ownership": {"owner_type": "talent", "owner_id": "T1", "talent_id": "T1",
                      "is_shared_copy": True, "conflict": None},
    }
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(project_deletion="P1", now=NOW)))
    assert not d.deletable
    assert d.owner.owner_type == "talent"


# ---------------------------------------------------------------------------
# 12–15  Endpoint wiring routes through the service
# ---------------------------------------------------------------------------
def test_12_admin_remove_media_item_uses_lifecycle_service():
    import inspect
    from routers import submissions
    src = inspect.getsource(submissions.admin_remove_media_item)
    assert "media_lifecycle" in src or "delete_if_safe" in src


def test_13_delete_submission_uses_lifecycle_service():
    import inspect
    from routers import submissions
    src = inspect.getsource(submissions.delete_submission)
    assert "media_lifecycle" in src or "mark_pending_deletion" in src or "delete_if_safe" in src


def test_14_delete_project_uses_lifecycle_service_not_folder_prefix():
    import inspect
    from routers import projects
    src = inspect.getsource(projects.delete_project)
    assert "delete_resources_by_prefix" not in src
    assert "media_lifecycle" in src or "mark_pending_deletion" in src


def test_15_delete_talent_does_not_blindly_destroy_global_media():
    import inspect
    from routers import talents
    src = inspect.getsource(talents.delete_talent)
    assert "talent_hard_delete_blockers" in src or "media_lifecycle" in src
    # no cascade folder-prefix / destroy in the talent delete path
    assert "delete_resources_by_prefix" not in src


# ---------------------------------------------------------------------------
# 16–17  Legacy folder discoverability + canonical id preference
# ---------------------------------------------------------------------------
def test_16_legacy_folder_structures_remain_discoverable(db):
    # asset addressed by a legacy folder-scheme public_id is still matched by
    # the reference scan (which keys on the stored public_id verbatim).
    m = _take_media(public_id="admin_media/old-scheme/take_1")
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [m]})
    deps = run(ml.get_dependencies(db, m))
    assert any(d.kind == "submission" for d in deps)


def test_17_canonical_public_id_preferred_over_folder_slug(db):
    # classify_owner never recomputes a folder slug; it reads ownership.public_id
    m = _talent_media(public_id="talentgram/talents/T1_some-OLD-slug/photo")
    o = ml.classify_owner(m)
    assert o.owner_type == "talent" and o.talent_id == "T1"


# ---------------------------------------------------------------------------
# 18–20  Idempotency + destroy-gate
# ---------------------------------------------------------------------------
def test_18_idempotent_mark_pending(db):
    m = _take_media()
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [m]})
    r1 = run(ml.mark_pending_deletion(db, "submissions", "S1", "m-take",
                                      reason="x", retention_days=30, now=NOW))
    r2 = run(ml.mark_pending_deletion(db, "submissions", "S1", "m-take",
                                      reason="y", retention_days=90, now=NOW + timedelta(days=5)))
    assert r1["state"] == ml.STATE_PENDING_DELETION
    assert r2["unchanged"] is True
    assert r2["lifecycle"]["marked_at"] == NOW.isoformat()  # clock preserved


def test_19_repeated_deletion_request_cannot_destroy_twice(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    calls = []
    m = _take_media(lifecycle={"state": ml.STATE_PENDING_DELETION, "marked_at": NOW.isoformat(),
                               "retention_days": 30})
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "deleted_at": NOW.isoformat(), "media": [m]})
    ctx = ml.DeletionContext(now=NOW + timedelta(days=40),
                             exclude_collection="submissions", exclude_parent_id="S1")
    r1 = run(ml.delete_if_safe(db, m, ctx=ctx, collection_name="submissions", parent_id="S1",
                               destroyer=lambda mm: calls.append(mm), retention_days=30))
    r2 = run(ml.delete_if_safe(db, m, ctx=ctx, collection_name="submissions", parent_id="S1",
                               destroyer=lambda mm: calls.append(mm), retention_days=30))
    assert r1["outcome"] == "deleted"
    assert r2["outcome"] == "already_deleted"
    assert len(calls) == 1


def test_20_destroy_never_called_when_gate_fails(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    calls = []
    m = _talent_media()
    db.talents.docs.append({"id": "T1", "name": "A", "media": [m]})
    db.talents.docs.append({"id": "T2", "name": "B", "media": [_talent_media(mid="m2")]})  # shared ref
    r = run(ml.delete_if_safe(db, m, ctx=ml.DeletionContext(talent_hard_delete="T1", now=NOW),
                              collection_name="talents", parent_id="T1",
                              destroyer=lambda mm: calls.append(mm)))
    assert r["outcome"] == "protected"
    assert calls == []


# ---------------------------------------------------------------------------
# Retention-value safety + P6 physical-delete gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (None, 30), (0, 0), (30, 30), (90, 90), (-1, -1),
    (45, -1), (-5, -1), ("nonsense", -1), ("", -1), (7, -1),
])
def test_resolve_retention_days_safe(raw, expected):
    assert ml.resolve_retention_days(raw) == expected


def test_physical_delete_off_by_default_only_marks_pending(db):
    # env flag unset -> a deletable audition asset is only marked PENDING
    m = _take_media(lifecycle={"state": ml.STATE_PENDING_DELETION, "marked_at": NOW.isoformat(),
                               "retention_days": 30})
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "deleted_at": NOW.isoformat(), "media": [m]})
    calls = []
    r = run(ml.delete_if_safe(
        db, m, ctx=ml.DeletionContext(now=NOW + timedelta(days=99),
                                      exclude_collection="submissions", exclude_parent_id="S1"),
        collection_name="submissions", parent_id="S1",
        destroyer=lambda mm: calls.append(mm), retention_days=30))
    assert r["outcome"] == "would_delete"
    assert calls == []


def test_just_uploaded_reject_is_immediate(db):
    m = _take_media()
    d = run(ml.can_delete(db, m, ctx=ml.DeletionContext(just_uploaded_reject=True, now=NOW),
                          retention_days=30))
    assert d.deletable and d.state == ml.STATE_DELETED


def test_reject_rollback_destroys_even_with_physical_flag_off(db):
    # env flag NOT set -> normal deletes only mark pending, but a failed-upload
    # rollback still runs the destroyer (aborted transaction, not "existing" media)
    calls = []
    m = _take_media()
    r = run(ml.delete_if_safe(db, m, ctx=ml.DeletionContext(just_uploaded_reject=True),
                              destroyer=lambda mm: calls.append(mm)))
    assert r["outcome"] == "deleted"
    assert len(calls) == 1


def test_record_owner_teardown_splits_global_vs_audition(db):
    take = _take_media()
    glob = _talent_media()
    db.talents.docs.append({"id": "T1", "name": "A", "media": [glob]})   # global still owned
    summ = run(ml.record_owner_teardown(
        db, [take, glob, {"id": "noasset"}],
        context_kind="project", context_id="P1", now=NOW, retention_days=30))
    assert summ["audition_enqueued"] == 1
    assert summ["global_skipped"] == 1
    assert summ["no_asset"] == 1
    led = run(db.pending_media_deletions.find({}).to_list())
    assert len(led) == 1 and led[0]["owner_type"] == "project_submission"


def test_talent_hard_delete_blockers_lists_dependencies(db):
    db.talents.docs.append({"id": "T1", "email": "t@x.com", "normalized_email": "t@x.com",
                            "name": "Tal", "media": [_talent_media()]})
    db.submissions.docs.append({"id": "S1", "talent_id": "T1", "project_id": "P1", "media": []})
    db.links.docs.append({"slug": "r1", "talent_ids": ["T1"]})
    blockers = run(ml.talent_hard_delete_blockers(db, "T1"))
    kinds = {b["kind"] for b in blockers}
    assert "submissions" in kinds and "review_links" in kinds and "global_media" in kinds
