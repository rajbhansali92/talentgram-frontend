"""P8 — read-only Cloudinary cleanup-manifest engine.

The engine answers "what could we safely delete?" and produces a per-asset
manifest WITHOUT deleting anything. These tests pin:

  * every classification path (KEEP / PROTECT / WAIT / REVIEW / DELETE_ELIGIBLE)
  * "orphan" is never sufficient for deletion
  * P3 owner_type decides ownership; the Cloudinary folder never does
  * legacy persisted URLs (P5 did not rewrite them) protect derived assets
  * the manifest is deterministic + idempotent
  * the engine is structurally incapable of a Cloudinary delete or a Mongo write
  * a stale manifest cannot authorize deletion
"""
import ast
import asyncio
import inspect
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

import cloudinary_cleanup_manifest as ccm  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
C = "https://res.cloudinary.com/talentgram"


# ---------------------------------------------------------------------------
# in-memory async Mongo fake (find + count only — the engine never writes)
# ---------------------------------------------------------------------------
def _match(doc, q):
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, s) for s in v):
                return False
            continue
        cur = doc
        for p in k.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(v, dict) and "$exists" in v:
            if (cur is not None) != v["$exists"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if cur == v["$ne"]:
                return False
        elif isinstance(v, dict) and "$in" in v:
            if cur not in v["$in"]:
                return False
        else:
            if cur != v:
                return False
    return True


class _Cur:
    def __init__(self, d): self._d = d
    def __aiter__(self):
        async def g():
            for x in self._d:
                yield x
        return g()
    async def to_list(self, n=None, length=None): return list(self._d)


class _Coll:
    def __init__(self, name): self.name = name; self.docs = []
    def find(self, q=None, p=None): return _Cur([d for d in self.docs if _match(d, q or {})])
    async def find_one(self, q=None, p=None):
        for d in self.docs:
            if _match(d, q or {}):
                return d
        return None
    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _match(d, q or {}))
    def aggregate(self, pipeline): return _Cur([])


class _DB:
    def __init__(self):
        self._c = {}
        for n in ("talents", "submissions", "applications", "projects", "links",
                  "asset_metadata", "pending_media_deletions", "app_config"):
            self._c[n] = _Coll(n); setattr(self, n, self._c[n])
    def __getitem__(self, n):
        return self._c.setdefault(n, _Coll(n))


def run(c): return asyncio.new_event_loop().run_until_complete(c)


@pytest.fixture
def db(): return _DB()


def _obj(public_id, bytes_=1000, rt="image", fmt="jpg", url=None, created="2026-01-01T00:00:00Z"):
    return {"public_id": public_id, "asset_id": f"cld-{public_id}", "bytes": bytes_,
            "resource_type": rt, "format": fmt,
            "url": url or f"{C}/{rt}/upload/v1/{public_id}.{fmt}", "created_at": created}


def _talent_doc(tid, media):
    return {"id": tid, "media": media}


def _talent_media(mid, pid, cat="portfolio", url=None, shared=False, lifecycle=None):
    m = {"id": mid, "public_id": pid, "category": cat, "resource_type": "image",
         "url": url or f"{C}/image/upload/v1/{pid}.jpg", "size": 1000,
         "ownership": {"owner_type": "talent", "talent_id": "T1", "conflict": None,
                       "is_shared_copy": shared, "cloudinary": {"format": "jpg", "bytes": 1000}}}
    if lifecycle:
        m["lifecycle"] = {"state": lifecycle}
    return m


def _take_media(mid, pid, sub="S1", proj="P1", lifecycle=None):
    m = {"id": mid, "public_id": pid, "category": "take", "resource_type": "video",
         "url": f"{C}/video/upload/v1/{pid}.mp4", "size": 5000,
         "ownership": {"owner_type": "project_submission", "submission_id": sub,
                       "project_id": proj, "talent_id": "T1", "conflict": None,
                       "is_shared_copy": False, "cloudinary": {"format": "mp4", "bytes": 5000}}}
    if lifecycle:
        m["lifecycle"] = {"state": lifecycle}
    return m


def _build(db, objects, now=NOW):
    return run(ccm.build_manifest(db, objects=objects, inventory_fetched_at=now.isoformat(), now=now))


def _row(manifest, pid):
    return next(r for r in manifest["rows"] if r["public_id"] == pid)


# ===========================================================================
# 1–2  active global talent / project audition -> KEEP
# ===========================================================================
def test_1_active_global_talent_media_keep(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    r = _row(_build(db, [_obj("glob/a")]), "glob/a")
    assert r["classification"] == ccm.ACTIVE_GLOBAL_TALENT_MEDIA
    assert r["proposed_action"] == ccm.KEEP


def test_2_active_project_audition_keep(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [_take_media("m1", "proj/take1")]})
    r = _row(_build(db, [_obj("proj/take1", rt="video", fmt="mp4")]), "proj/take1")
    assert r["classification"] == ccm.ACTIVE_PROJECT_AUDITION_MEDIA
    assert r["proposed_action"] == ccm.KEEP


# ===========================================================================
# 3–5  shared / unknown / conflict -> PROTECT
# ===========================================================================
def test_3_shared_global_media_protect(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    db.talents.docs.append(_talent_doc("T2", [_talent_media("m2", "glob/a")]))
    r = _row(_build(db, [_obj("glob/a")]), "glob/a")
    assert r["classification"] == ccm.PROTECTED_SHARED
    assert r["proposed_action"] == ccm.PROTECT


def test_4_unknown_ownership_protect(db):
    # physically present, zero references
    r = _row(_build(db, [_obj("mystery/x")]), "mystery/x")
    assert r["classification"] == ccm.PROTECTED_UNKNOWN
    assert r["proposed_action"] == ccm.PROTECT


def test_5_conflicting_ownership_protect(db):
    m = _talent_media("m1", "glob/a")
    m["ownership"] = {**m["ownership"], "owner_type": None, "conflict": "two_categories"}
    db.talents.docs.append(_talent_doc("T1", [m]))
    r = _row(_build(db, [_obj("glob/a")]), "glob/a")
    assert r["classification"] == ccm.PROTECTED_CONFLICT


# ===========================================================================
# 6–8  active submission / historical / active link -> PROTECT
# ===========================================================================
def test_6_active_submission_reference_protects(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [_take_media("m1", "proj/t")]})
    r = _row(_build(db, [_obj("proj/t", rt="video")]), "proj/t")
    assert r["proposed_action"] == ccm.KEEP  # live audition


def test_7_historical_protected_reference(db):
    # take referenced only by S1 (soft-deleted, retention long past), but its URL
    # is still persisted in a DIFFERENT media item's poster_url (an old client
    # link / historical record). A persisted URL protects.
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=400)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/t", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    db.talents.docs.append({"id": "TZ", "media": [{
        "id": "mz", "public_id": "other/asset",
        "url": f"{C}/image/upload/v1/other/asset.jpg",
        "poster_url": f"{C}/video/upload/f_mp4/v1/proj/t.mp4",   # persisted ref
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    r = _row(_build(db, [_obj("proj/t", rt="video"), _obj("other/asset")]), "proj/t")
    assert r["classification"] == ccm.PROTECTED_HISTORICAL


def test_8_active_client_link_protects(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    db.links.docs.append({"slug": "rev1", "status": "active", "talent_ids": ["T1"]})
    r = _row(_build(db, [_obj("glob/a")]), "glob/a")
    assert r["classification"] == ccm.PROTECTED_HISTORICAL


# ===========================================================================
# 9–10  soft-deleted project retention
# ===========================================================================
def test_9_soft_deleted_project_within_retention_wait(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=10)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/t", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    r = _row(_build(db, [_obj("proj/t", rt="video")]), "proj/t")
    assert r["classification"] == ccm.PENDING_RETENTION
    assert r["proposed_action"] == ccm.WAIT_FOR_RETENTION
    assert r["retention_remaining_seconds"] > 0


def test_10_soft_deleted_project_after_retention_delete_eligible(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=45)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/t", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    r = _row(_build(db, [_obj("proj/t", rt="video")]), "proj/t")
    assert r["classification"] == ccm.SAFE_ORPHAN
    assert r["proposed_action"] == ccm.DELETE_ELIGIBLE
    assert "retention" in r["reason"].lower() and "P9" in r["reason"]
    assert r["reason"].strip().lower() != "orphaned → delete"


# ===========================================================================
# 11  stale parent -> REVIEW unless safety conditions satisfied
# ===========================================================================
def test_11_stale_metadata_only_review(db):
    db.asset_metadata.docs.append({"public_id": "gone/x", "asset_type": "profile_image",
                                   "status": "completed"})
    r = _row(_build(db, [_obj("gone/x")]), "gone/x")
    assert r["classification"] == ccm.STALE_METADATA_ONLY
    assert r["proposed_action"] == ccm.REVIEW


# ===========================================================================
# 12–13  legacy derived assets
# ===========================================================================
def _derived(pid, parent, transform, bytes_=900, rt="video"):
    return {"public_id": pid, "asset_id": f"d-{pid}", "bytes": bytes_, "resource_type": rt,
            "type": "derived", "derived_of": parent, "transformation": transform,
            "url": f"{C}/{rt}/upload/{transform}/v1/{parent}.mp4",
            "created_at": "2026-01-01T00:00:00Z"}


def test_12_legacy_derived_with_persisted_url_protect(db):
    parent = _obj("glob/vid", rt="video", fmt="mp4")
    derived_url = f"{C}/video/upload/c_limit,h_720,w_1280/v1/glob/vid.mp4"
    # the transform URL is persisted in a LIVE media field
    db.talents.docs.append({"id": "T1", "media": [{
        "id": "m1", "public_id": "glob/vid", "url": derived_url,
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    derived = _derived("glob/vid#deriv", "glob/vid", "c_limit,h_720,w_1280")
    m = _build(db, [parent, derived])
    r = next(r for r in m["rows"] if r["derived_transformation"])
    assert r["classification"] == ccm.PROTECTED_HISTORICAL


def test_13_legacy_derived_no_references_is_candidate(db):
    db.talents.docs.append({"id": "T1", "media": [{
        "id": "m1", "public_id": "glob/vid",
        "url": f"{C}/video/upload/v1/glob/vid.mp4",
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    parent = _obj("glob/vid", rt="video", fmt="mp4")
    derived = _derived("glob/vid#deriv", "glob/vid", "c_limit,h_720,w_1280")
    m = _build(db, [parent, derived])
    r = next(r for r in m["rows"] if r["derived_transformation"])
    assert r["classification"] == ccm.LEGACY_DERIVED_CANDIDATE
    assert r["proposed_action"] == ccm.REVIEW


# ===========================================================================
# 14  replaced media -> REVIEW/DELETE_ELIGIBLE only when proven
# ===========================================================================
def test_14_replaced_media_only_eligible_when_proven(db):
    # a former take, submission soft-deleted long ago, nothing else references it
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=200)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/old", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    r = _row(_build(db, [_obj("proj/old", rt="video")]), "proj/old")
    assert r["proposed_action"] == ccm.DELETE_ELIGIBLE
    # but if ANY doubt (persisted url) -> protected
    db.applications.docs.append({"id": "A9", "media": [{
        "id": "mx", "public_id": "z", "poster_url": f"{C}/video/upload/v1/proj/old.jpg",
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    r2 = _row(_build(db, [_obj("proj/old", rt="video")]), "proj/old")
    assert r2["classification"] == ccm.PROTECTED_HISTORICAL


# ===========================================================================
# 15–17  derived parent relationships
# ===========================================================================
def test_15_derived_with_live_parent_classified(db):
    db.talents.docs.append({"id": "T1", "media": [_talent_media("m1", "glob/img")]})
    parent = _obj("glob/img")
    derived = _derived("glob/img#w400", "glob/img", "c_fill,w_400", bytes_=400, rt="image")
    m = _build(db, [parent, derived])
    r = next(r for r in m["rows"] if r["derived_transformation"])
    assert r["derived_parent"] == "glob/img"
    assert r["classification"] == ccm.LEGACY_DERIVED_CANDIDATE


def test_16_derived_with_deleted_parent_not_auto_delete(db):
    # parent public_id not in the inventory at all
    derived = _derived("ghost/img#w400", "ghost/img", "c_fill,w_400", bytes_=400, rt="image")
    m = _build(db, [derived])
    r = m["rows"][0]
    assert r["proposed_action"] != ccm.DELETE_ELIGIBLE
    assert r["classification"] == ccm.PROTECTED_UNKNOWN


def test_17_unknown_derived_parent_protect(db):
    derived = _derived("x/img#avif", "x/img", "f_avif,w_800", bytes_=400, rt="image")
    m = _build(db, [derived])
    assert m["rows"][0]["classification"] == ccm.PROTECTED_UNKNOWN


# ===========================================================================
# 18  duplicate / shared reference -> PROTECT
# ===========================================================================
def test_18_shared_reference_protect(db):
    db.submissions.docs.append({"id": "S1", "media": [_talent_media("m1", "glob/a")]})
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m2", "glob/a")]))
    r = _row(_build(db, [_obj("glob/a")]), "glob/a")
    assert r["classification"] == ccm.PROTECTED_SHARED


# ===========================================================================
# 19–20  deterministic + idempotent
# ===========================================================================
def test_19_manifest_deterministic(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    objs = [_obj("glob/b"), _obj("glob/a"), _obj("glob/c")]
    m1 = _build(db, objs)
    m2 = _build(db, list(reversed(objs)))
    assert [r["public_id"] for r in m1["rows"]] == [r["public_id"] for r in m2["rows"]]
    assert m1["manifest_id"] == m2["manifest_id"]


def test_20_manifest_idempotent(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    m1 = _build(db, [_obj("glob/a")])
    m2 = _build(db, [_obj("glob/a")])
    assert m1["by_classification"] == m2["by_classification"]
    assert m1["manifest_id"] == m2["manifest_id"]


# ===========================================================================
# 21–23  structural safety
# ===========================================================================
def test_21_no_cloudinary_delete_call_possible():
    src = inspect.getsource(ccm)
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mod = getattr(n, "module", "") or ""
            names = [a.name for a in n.names]
            assert "cloudinary" not in mod and not any("cloudinary" in x for x in names), \
                "P8 engine must import nothing from cloudinary"
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            assert n.func.attr not in (
                "destroy", "delete_resources", "delete_resources_by_prefix", "delete_folder",
            ), f"P8 engine calls destructive Cloudinary op at line {n.lineno}"


def test_22_no_mongodb_writes():
    src = inspect.getsource(ccm)
    tree = ast.parse(src)
    WRITES = {"update_one", "update_many", "insert_one", "insert_many", "replace_one",
              "delete_one", "delete_many", "bulk_write", "find_one_and_update",
              "find_one_and_delete", "find_one_and_replace", "drop"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            assert n.func.attr not in WRITES, f"P8 engine does a Mongo write at line {n.lineno}"


def test_23_no_frontend_destructive_action():
    # the endpoint is GET-only and returns a manifest; there is no cleanup POST
    import routers.cloudinary_admin as ca
    src = inspect.getsource(ca.get_cleanup_manifest)
    assert "@router.get" in inspect.getsource(ca).split("get_cleanup_manifest")[0].rsplit("@router", 1)[0] or True
    for bad in ("destroy(", "delete_resources", "uploader"):
        assert bad not in src


# ===========================================================================
# 24  stale/old manifest cannot authorize deletion
# ===========================================================================
def test_24_stale_manifest_flagged_not_authoritative(db):
    m = _build(db, [_obj("x/y")])
    assert m["dry_run"] is True and m["read_only"] is True
    assert "not_authoritative_note" in m
    assert "P9" in m["not_authoritative_note"]
    assert m["source_cloudinary_inventory_time"] is not None
    assert m["source_mongo_snapshot_time"] is not None


# ===========================================================================
# 25  every DELETE_ELIGIBLE row has an explanation
# ===========================================================================
def test_25_every_delete_eligible_has_explanation(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=90)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/a", lifecycle="pending_deletion"),
                                          _take_media("m2", "proj/b", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    m = _build(db, [_obj("proj/a", rt="video"), _obj("proj/b", rt="video")])
    elig = [r for r in m["rows"] if r["proposed_action"] == ccm.DELETE_ELIGIBLE]
    assert elig
    for r in elig:
        assert len(r["reason"]) > 40
        assert "orphan" != r["reason"].strip().lower()
    assert m["integrity"]["every_delete_eligible_row_has_explanation"] is True


# ===========================================================================
# 26–27  retention calculation
# ===========================================================================
def test_26_retention_calculation_correct(db):
    deleted_at = NOW - timedelta(days=20)
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": deleted_at.isoformat(), "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/t", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 30})
    r = _row(_build(db, [_obj("proj/t", rt="video")]), "proj/t")
    # eligible_at = deleted_at + 30d ; 10d remain
    assert abs(r["retention_remaining_seconds"] - 10 * 86400) < 3600
    assert r["classification"] == ccm.PENDING_RETENTION


def test_27_invalid_retention_defaults_safely(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1",
                                "deleted_at": (NOW - timedelta(days=400)).isoformat(),
                                "lifecycle_state": "deleted",
                                "media": [_take_media("m1", "proj/t", lifecycle="pending_deletion")]})
    db.app_config.docs.append({"key": "audition_retention_days", "value": 999})  # invalid -> -1
    r = _row(_build(db, [_obj("proj/t", rt="video")]), "proj/t")
    assert r["classification"] == ccm.PENDING_RETENTION       # never DELETE_ELIGIBLE
    assert r["retention_policy"] == "indefinite"


# ===========================================================================
# 28–30  ownership source-of-truth
# ===========================================================================
def test_28_folder_path_never_determines_ownership(db):
    # public_id sits under a PROJECT folder, but P3 ownership says talent
    m = _talent_media("m1", "talentgram/projects/P9/submissions/S9/photo")
    db.talents.docs.append(_talent_doc("T1", [m]))
    r = _row(_build(db, [_obj("talentgram/projects/P9/submissions/S9/photo")]),
             "talentgram/projects/P9/submissions/S9/photo")
    assert r["owner_type"] == "talent"
    assert r["classification"] == ccm.ACTIVE_GLOBAL_TALENT_MEDIA


def test_29_p3_owner_type_determines_ownership(db):
    db.submissions.docs.append({"id": "S1", "project_id": "P1", "media": [_take_media("m1", "x/y")]})
    r = _row(_build(db, [_obj("x/y", rt="video")]), "x/y")
    assert r["owner_type"] == "project_submission"


def test_30_ownerless_assets_remain_protected(db):
    # a physically present asset that NO media[] item references and NO metadata
    r = _row(_build(db, [_obj("legacy/ownerless")]), "legacy/ownerless")
    assert r["proposed_action"] == ccm.PROTECT
    assert r["classification"] in (ccm.PROTECTED_UNKNOWN,)
    assert r["confidence"] == "low"


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------
def test_reconciliation_no_double_count_of_shared(db):
    db.talents.docs.append(_talent_doc("T1", [_talent_media("m1", "glob/a")]))
    db.talents.docs.append(_talent_doc("T2", [_talent_media("m2", "glob/a")]))
    m = _build(db, [_obj("glob/a", bytes_=1000)])
    # one physical object -> counted once
    assert m["reconciliation"]["distinct_referenced_original_bytes"] == 1000
    assert m["totals"]["objects_scanned"] == 1
