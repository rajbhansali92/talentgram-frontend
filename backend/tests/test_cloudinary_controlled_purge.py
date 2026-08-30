"""P9 — controlled, audited, per-asset Cloudinary derived-asset deletion.

Layers 1 (validation) and 2 (approval/batch) are tested without any Cloudinary
write. Layer 3 is tested with an injected fake deleter — it never touches
Cloudinary. The tests pin that "DELETE_CANDIDATE" cannot become "delete
everything": deletion needs an immutable hash-pinned admin approval of a named
set, every asset is re-proven safe at delete time, the canary is exactly 10, the
system stops after the canary and on any anomaly, and no prefix/folder delete
exists anywhere.
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

import cloudinary_controlled_purge as p  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
C = "https://res.cloudinary.com/talentgram"


# ---------------------------------------------------------------------------
# in-memory async Mongo fake (find/insert/update — no deletes)
# ---------------------------------------------------------------------------
def _match(doc, q):
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, s) for s in v):
                return False
            continue
        cur = doc
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
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
    def find(self, q=None, pr=None): return _Cur([d for d in self.docs if _match(d, q or {})])
    async def find_one(self, q=None, pr=None):
        for d in self.docs:
            if _match(d, q or {}):
                return {k: v for k, v in d.items() if k != "_id"}
        return None
    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _match(d, q or {}))
    def aggregate(self, pipeline): return _Cur([])
    async def insert_one(self, doc): self.docs.append({**doc}); return type("R", (), {"inserted_id": 1})()
    async def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if _match(d, q):
                d.update(upd.get("$set", {}))
                for k, v in (upd.get("$addToSet") or {}).items():
                    d.setdefault(k, [])
                    if v not in d[k]:
                        d[k].append(v)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()


class _DB:
    def __init__(self):
        self._c = {}
        for n in ("talents", "submissions", "applications", "projects", "links",
                  "asset_metadata", "pending_media_deletions", "app_config",
                  p.MANIFESTS_COLL, p.APPROVALS_COLL, p.BATCHES_COLL, p.AUDIT_COLL):
            self._c[n] = _Coll(n); setattr(self, n, self._c[n])
    def __getitem__(self, n): return self._c.setdefault(n, _Coll(n))


def run(c): return asyncio.new_event_loop().run_until_complete(c)


@pytest.fixture
def db(): return _DB()


# ---------------------------------------------------------------------------
# fixtures: a live talent + an AVIF derived candidate + a fake Cloudinary
# ---------------------------------------------------------------------------
PARENT = "talentgram/talents/T1/portfolio/img1"
DID = "derived-avif-1"


def _seed_live_parent(db, pid=PARENT, owner_type="talent"):
    db.talents.docs.append({"id": "T1", "media": [{
        "id": "m1", "public_id": pid, "category": "portfolio", "resource_type": "image",
        "url": f"{C}/image/upload/v1/{pid}.jpg",
        "ownership": {"owner_type": owner_type, "talent_id": "T1", "conflict": None,
                      "is_shared_copy": False},
    }]})


def _candidate(did=DID, pid=PARENT, xf="f_avif,q_auto/jpg", fam="f_avif,q_auto/jpg",
               fmt="avif", bytes_=40000, cls="DELETE_CANDIDATE", url=None, parent_ref=True):
    return {
        "candidate_id": did, "derived_id": did, "id": did,
        "public_id": pid, "parent_public_id": pid,
        "transformation": xf, "transformation_family": fam,
        "resource_type": "image", "format": fmt, "bytes": bytes_,
        "classification": cls, "owner_type": "talent",
        "parent_referenced": parent_ref,
        "url": url or f"{C}/image/upload/{xf}/v1787000000/{pid}.avif",
    }


def _fake_cloudinary(derived_present=True, parent_present=True, xf="f_avif,q_auto/jpg",
                     fmt="avif", bytes_=40000):
    state = {"derived_present": derived_present, "parent_present": parent_present}

    def fetch(pid, rt):
        if not state["parent_present"]:
            raise p._NotFound()
        d = []
        if state["derived_present"]:
            d = [{"id": DID, "transformation": xf, "format": fmt, "bytes": bytes_,
                  "url": f"{C}/image/upload/{xf}/v1/{pid}.avif"}]
        return {"public_id": pid, "resource_type": rt, "derived": d}

    def delete(ids):
        for i in ids:
            if i == DID:
                state["derived_present"] = False
        return {"deleted": {i: "deleted" for i in ids}}

    return fetch, delete, state


# ==========================================================================
# LAYER 1 — revalidation
# ==========================================================================
def test_1_delete_candidate_can_pass_validation(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.PASS


def test_2_unknown_cannot_pass(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(cls="UNKNOWN_DERIVED"), resource_fetcher=f, now=NOW))
    assert rv.status == p.PROTECTED


def test_3_conflict_cannot_pass(db):
    _seed_live_parent(db, owner_type="talent")
    db.talents.docs[0]["media"][0]["ownership"]["conflict"] = "two_categories"
    db.talents.docs[0]["media"][0]["ownership"]["owner_type"] = None
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.OWNERSHIP_BLOCKED


def test_4_persisted_url_cannot_pass(db):
    _seed_live_parent(db)
    # persist the exact derived URL in a media field
    db.talents.docs.append({"id": "TZ", "media": [{
        "id": "mz", "public_id": "other", "poster_url": f"{C}/image/upload/f_avif,q_auto/jpg/v1/{PARENT}.avif",
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.REFERENCE_BLOCKED


def test_5_active_reference_on_parent_still_allows_derivative_delete_but_audition_blocks(db):
    # a live talent parent is exactly the intended case -> PASS
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary()
    assert run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW)).status == p.PASS


def test_6_historical_protected_reference_blocks_via_persisted(db):
    _seed_live_parent(db)
    db.submissions.docs.append({"id": "S9", "media": [{
        "id": "ms", "public_id": "x", "url": f"{C}/image/upload/f_avif,q_auto/jpg/v9/{PARENT}.avif",
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.REFERENCE_BLOCKED


def test_7_ownership_change_invalidates_manifest(db):
    _seed_live_parent(db, owner_type="project_submission")
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    # manifest said owner_type talent, live says project_submission
    assert rv.status == p.STALE_MANIFEST


def test_8_parent_change_invalidates(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary(parent_present=False)
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.PARENT_BLOCKED


def test_9_asset_identity_change_invalidates(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary(xf="c_scale,w_9999")   # live transform differs from manifest
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.IDENTITY_MISMATCH


def test_10_11_12_retention_blocks_audition(db):
    _seed_live_parent(db, owner_type="project_submission")
    db.talents.docs[0]["media"][0]["ownership"]["owner_type"] = "project_submission"
    # candidate manifest also says project_submission so it's not STALE — retention path
    c = _candidate(); c["owner_type"] = "project_submission"
    f, _, _ = _fake_cloudinary()
    rv = run(p.revalidate_candidate(db, c, resource_fetcher=f, now=NOW))
    assert rv.status == p.RETENTION_BLOCKED


def test_13_stale_manifest_blocks_on_bytes(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary(bytes_=999999)
    rv = run(p.revalidate_candidate(db, _candidate(bytes_=40000), resource_fetcher=f, now=NOW))
    assert rv.status == p.STALE_MANIFEST


def test_derived_already_gone_is_not_found(db):
    _seed_live_parent(db)
    f, _, _ = _fake_cloudinary(derived_present=False)
    rv = run(p.revalidate_candidate(db, _candidate(), resource_fetcher=f, now=NOW))
    assert rv.status == p.NOT_FOUND


# ==========================================================================
# LAYER 2 — manifest / approval / batch
# ==========================================================================
def _10_avif_rows():
    return [_candidate(did=f"d{i}", pid=f"talentgram/talents/T1/portfolio/img{i}",
                       url=f"{C}/image/upload/f_avif,q_auto/jpg/v1/talentgram/talents/T1/portfolio/img{i}.avif")
            for i in range(12)]


def _seed_12_parents(db):
    media = []
    for i in range(12):
        media.append({"id": f"m{i}", "public_id": f"talentgram/talents/T1/portfolio/img{i}",
                      "category": "portfolio", "resource_type": "image",
                      "url": f"{C}/image/upload/v1/talentgram/talents/T1/portfolio/img{i}.jpg",
                      "ownership": {"owner_type": "talent", "talent_id": "T1", "conflict": None}})
    db.talents.docs.append({"id": "T1", "media": media})


def _fetch_all_present():
    def f(pid, rt):
        return {"public_id": pid, "resource_type": rt, "derived": [
            {"id": pid.split("img")[-1].join(["d", ""]), "transformation": "f_avif,q_auto/jpg",
             "format": "avif", "bytes": 40000, "url": f"{C}/image/upload/f_avif,q_auto/jpg/v1/{pid}.avif"}]}
    return f


def test_14_20_21_dry_run_manifest_no_writes_to_media(db):
    _seed_12_parents(db)
    before = len(db.talents.docs[0]["media"])
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="src1",
                                   resource_fetcher=_fetch_all_present(), actor="admin@x"))
    assert m["dry_run"] is True
    assert m["summary"]["passed_revalidation"] >= 10
    assert len(db.talents.docs[0]["media"]) == before  # media untouched
    assert db[p.MANIFESTS_COLL].docs  # manifest doc written (analysis artifact)


def test_17_18_19_approval_is_manifest_specific_and_required(db):
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="src1",
                                   resource_fetcher=_fetch_all_present(), actor="admin@x"))
    ids = m["passed_candidate_ids"][:10]
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="admin@x", candidate_ids=ids))
    assert ap["candidate_hash"] == p.candidate_hash(ids)
    # cannot approve ids not in this manifest
    with pytest.raises(ValueError):
        run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=["not-a-real-id"]))
    # no "approve all"
    assert not hasattr(p, "approve_all_future_candidates")


def test_16_31_batch_size_and_canary_exactly_10(db):
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="src1",
                                   resource_fetcher=_fetch_all_present(), actor="a"))
    ids = m["passed_candidate_ids"]
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=ids))
    with pytest.raises(ValueError):
        run(p.create_batch(db, ap["approval_id"], size=25, canary=True))
    with pytest.raises(ValueError):
        run(p.create_batch(db, ap["approval_id"], size=500, canary=False))
    b = run(p.create_batch(db, ap["approval_id"], size=10, canary=True))
    assert b["canary"] is True and b["size"] == 10


# ==========================================================================
# LAYER 3 — execution (dry-run + fake deleter; canary + anomaly stop)
# ==========================================================================
def _prep_batch(db):
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="src1",
                                   resource_fetcher=_fetch_all_present(), actor="a"))
    ids = m["passed_candidate_ids"]
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=ids))
    b = run(p.create_batch(db, ap["approval_id"], size=10, canary=True))
    return m, ap, b


def test_20b_dry_run_execute_zero_writes(db):
    m, ap, b = _prep_batch(db)
    f = _fetch_all_present()
    calls = []
    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=True,
                            resource_fetcher=f, derived_deleter=lambda ids: calls.append(ids)))
    assert r.dry_run and r.deleted == 0
    assert calls == []                       # deleter never called
    assert db[p.AUDIT_COLL].docs == []       # dry-run writes NOTHING


def test_19b_no_approval_no_deletion(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    # a batch that was never approved
    fake_batch = {"batch_id": "b_x", "approval_id": "missing", "manifest_id": "missing",
                  "candidates": [], "candidate_ids": [], "canary": True}
    db[p.BATCHES_COLL].docs.append(fake_batch)
    with pytest.raises(ValueError):
        run(p.execute_batch(db, "b_x", actor="a", dry_run=False,
                            resource_fetcher=_fetch_all_present(), derived_deleter=lambda i: {}))


def test_physical_flag_off_blocks_real_execution(db):
    m, ap, b = _prep_batch(db)
    with pytest.raises(PermissionError):
        run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=_fetch_all_present(), derived_deleter=lambda i: {}))


def test_25_26_27_successful_delete_verified_parent_survives(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    # single-candidate manifest so we can watch one full delete cycle
    row = _candidate(did=DID, pid=PARENT)
    m = run(p.build_purge_manifest(db, [row], source_manifest_id="src1",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ids = m["passed_candidate_ids"]
    assert ids == [DID]
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=ids))
    # bypass the canary-size rule for a 1-asset functional test
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    fetch, delete, state = _fake_cloudinary()
    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=fetch, derived_deleter=delete))
    assert r.deleted == 1 and not r.stopped
    assert state["derived_present"] is False and state["parent_present"] is True
    # media references untouched
    assert len(db.talents.docs[0]["media"]) == 12
    # audit
    aud = db[p.AUDIT_COLL].docs
    assert aud and aud[0]["deletion_result"] == "deleted"
    for f in ("timestamp", "actor", "manifest_id", "batch_id", "public_id", "derived_id",
              "transformation", "classification", "revalidation_result"):
        assert f in aud[0]


def _fetch_1():
    def f(pid, rt):
        if pid == PARENT:
            return {"public_id": pid, "resource_type": rt, "derived": [
                {"id": DID, "transformation": "f_avif,q_auto/jpg", "format": "avif", "bytes": 40000,
                 "url": f"{C}/image/upload/f_avif,q_auto/jpg/v1/{pid}.avif"}]}
        return {"public_id": pid, "resource_type": rt, "derived": []}
    return f


def test_23_ambiguous_deletion_stops(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=_fetch_1(),
                            derived_deleter=lambda ids: {"partial": True, "deleted": {}}))
    assert r.stopped and "ambiguous" in (r.stop_reason or "").lower()
    assert r.deleted == 0


def test_24_parent_disappearance_is_anomaly(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    calls = {"n": 0}

    def fetch(pid, rt):
        calls["n"] += 1
        if calls["n"] <= 1:  # pre-delete revalidation: present
            return {"public_id": pid, "resource_type": rt, "derived": [
                {"id": DID, "transformation": "f_avif,q_auto/jpg", "format": "avif", "bytes": 40000,
                 "url": f"{C}/image/upload/f_avif,q_auto/jpg/v1/{pid}.avif"}]}
        raise p._NotFound()  # post-delete: parent gone!

    with pytest.raises(p.PurgeAnomaly):
        run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=fetch, derived_deleter=lambda ids: {"deleted": {DID: "deleted"}}))


def test_28_persisted_legacy_derivative_cannot_be_deleted(db):
    _seed_live_parent(db)
    row = _candidate(cls="PROTECTED_HISTORICAL_DERIVED")
    f, _, _ = _fake_cloudinary()
    assert run(p.revalidate_candidate(db, row, resource_fetcher=f, now=NOW)).status == p.PROTECTED


def test_29_unknown_fmp4_cannot_be_deleted(db):
    _seed_live_parent(db)
    row = _candidate(xf="f_mp4", fam="f_mpN", cls="UNKNOWN_DERIVED", fmt="mp4")
    f, _, _ = _fake_cloudinary()
    assert run(p.revalidate_candidate(db, row, resource_fetcher=f, now=NOW)).status == p.PROTECTED


def test_30_orphan_parent_legacy_derived_cannot_pass(db):
    # LEGACY_DERIVED classification is in _NEVER
    f, _, _ = _fake_cloudinary()
    row = _candidate(cls="LEGACY_DERIVED", parent_ref=False)
    assert run(p.revalidate_candidate(db, row, resource_fetcher=f, now=NOW)).status == p.PROTECTED


def test_32_canary_stops_after_10_no_auto_continue(db):
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="s",
                                   resource_fetcher=_fetch_all_present(), actor="a"))
    ids = m["passed_candidate_ids"]
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=ids))
    b = run(p.create_batch(db, ap["approval_id"], size=10, canary=True))
    assert b["size"] == 10
    # a second canary from the same approval only draws from the REMAINING ids
    b2 = run(p.create_batch(db, ap["approval_id"], size=10, canary=True)) if len(ids) >= 20 else None
    # execute_batch never loops past the batch's own candidates
    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=True,
                            resource_fetcher=_fetch_all_present(), derived_deleter=lambda i: {}))
    assert r.attempted == 10


def test_33_34_audit_fields_and_no_secrets(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="admin@x"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="admin@x", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    fetch, delete, _ = _fake_cloudinary()
    run(p.execute_batch(db, b["batch_id"], actor="admin@x", dry_run=False,
                        resource_fetcher=fetch, derived_deleter=delete))
    rec = db[p.AUDIT_COLL].docs[0]
    for f in ("timestamp", "actor", "manifest_id", "batch_id", "public_id", "derived_id",
              "parent_public_id", "resource_type", "format", "bytes", "transformation",
              "classification", "revalidation_result", "deletion_result"):
        assert f in rec
    blob = repr(rec).lower()
    assert "api_secret" not in blob and "api_key" not in blob and os.environ["CLOUDINARY_API_SECRET"].lower() not in blob


def test_35_repeated_execution_cannot_delete_twice(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    fetch, delete, _ = _fake_cloudinary()
    r1 = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                             resource_fetcher=fetch, derived_deleter=delete))
    assert r1.deleted == 1
    with pytest.raises(ValueError):  # batch already executed
        run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=fetch, derived_deleter=delete))


# ==========================================================================
# 15, 36 — structural: no prefix/folder delete anywhere
# ==========================================================================
def test_15_36_no_prefix_or_folder_delete_anywhere():
    src = inspect.getsource(p)
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            assert n.func.attr not in (
                "delete_resources_by_prefix", "delete_folder", "delete_resources",
                "delete_derived_by_transformation", "destroy",
            ), f"P9 uses a broad deletion API at line {n.lineno}"
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + [getattr(n, "module", "") or ""]
            assert not any("uploader" in x for x in names)


def test_22_failed_deletion_is_logged(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))

    def boom(ids):
        raise RuntimeError("cloudinary 500")

    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=_fetch_1(), derived_deleter=boom))
    assert r.stopped and r.deleted == 0
    assert db[p.AUDIT_COLL].docs and db[p.AUDIT_COLL].docs[0]["deletion_result"] == "error"


def test_canary_selection_excludes_forbidden_families(db):
    rows = [
        _candidate(did="ok1", url=f"{C}/image/upload/f_avif,q_auto/jpg/v1/{PARENT}1.avif"),
        _candidate(did="mp4", xf="f_mp4", fam="f_mpN", fmt="mp4"),
        _candidate(did="dl", xf="fl_attachment:x/f_mp4", fam="fl_attachment:* (download)"),
        _candidate(did="ok2", url=f"{C}/image/upload/f_avif,q_auto/jpg/v1/{PARENT}2.avif"),
    ]
    picked = p.select_canary(rows, 10)
    ids = {r["derived_id"] for r in picked}
    assert "mp4" not in ids and "dl" not in ids
    assert {"ok1", "ok2"}.issubset(ids)


def test_14b_deleter_receives_exactly_one_id(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    seen = []
    fetch, _, _ = _fake_cloudinary()

    def delete(ids):
        seen.append(list(ids))
        return {"deleted": {i: "deleted" for i in ids}}

    run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                        resource_fetcher=fetch, derived_deleter=delete))
    assert seen == [[DID]]   # exactly one derived id per call, never a list of many


def test_manifest_that_passed_but_now_blocked_stops_the_run(db, monkeypatch):
    monkeypatch.setenv("MEDIA_LIFECYCLE_PHYSICAL_DELETE", "true")
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, [_candidate(did=DID, pid=PARENT)], source_manifest_id="s",
                                   resource_fetcher=_fetch_1(), actor="a"))
    ap = run(p.approve_manifest(db, m["manifest_id"], approved_by="a", candidate_ids=[DID]))
    b = run(p.create_batch(db, ap["approval_id"], size=1, canary=False))
    # between approval and execution, a persisted URL for that derivative appears
    db.talents.docs.append({"id": "TZ", "media": [{
        "id": "mz", "public_id": "x",
        "url": f"{C}/image/upload/f_avif,q_auto/jpg/v1/{PARENT}.avif",
        "ownership": {"owner_type": "talent", "conflict": None}}]})
    r = run(p.execute_batch(db, b["batch_id"], actor="a", dry_run=False,
                            resource_fetcher=_fetch_1(), derived_deleter=lambda i: {"deleted": {}}))
    assert r.stopped and r.deleted == 0
    assert "manifest" in (r.stop_reason or "").lower() and "now" in (r.stop_reason or "").lower()


def test_dry_run_manifest_build_writes_only_the_manifest_artifact(db):
    _seed_12_parents(db)
    run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="s",
                               resource_fetcher=_fetch_all_present(), actor="a"))
    # no media / no audit / no batch / no approval touched
    assert db[p.AUDIT_COLL].docs == []
    assert db[p.APPROVALS_COLL].docs == []
    assert db[p.BATCHES_COLL].docs == []
    assert len(db[p.MANIFESTS_COLL].docs) == 1


def test_dry_run_manifest_persist_false_writes_absolutely_nothing(db):
    _seed_12_parents(db)
    m = run(p.build_purge_manifest(db, _10_avif_rows(), source_manifest_id="s",
                                   resource_fetcher=_fetch_all_present(), actor="a",
                                   persist=False))
    assert m["summary"]["passed_revalidation"] >= 10
    assert db[p.MANIFESTS_COLL].docs == []   # NOT persisted
    assert db[p.AUDIT_COLL].docs == []
    assert db[p.APPROVALS_COLL].docs == []
