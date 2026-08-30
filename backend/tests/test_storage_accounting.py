"""P7 — storage accounting model + soft-delete query audit.

The accounting model must keep Cloudinary (authoritative bytes/objects/derived)
strictly separate from MongoDB (authoritative ownership/references), never
present a MongoDB size sum as "Cloudinary storage", classify assets by the P3
ownership sub-document, and stay read-only.
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

import storage_accounting as sa  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# in-memory async Mongo fake (aggregate + find + count + update upsert)
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
        if isinstance(v, dict) and "$ne" in v:
            if cur == v["$ne"]:
                return False
        elif isinstance(v, dict) and "$nin" in v:
            if cur in v["$nin"]:
                return False
        elif isinstance(v, dict) and "$exists" in v:
            if (cur is not None) != v["$exists"]:
                return False
        elif isinstance(v, dict) and "$in" in v:
            if cur not in v["$in"]:
                return False
        else:
            if cur != v:
                return False
    return True


class _Cur:
    def __init__(self, docs): self._d = docs
    def __aiter__(self):
        async def g():
            for x in self._d:
                yield x
        return g()
    async def to_list(self, n=None, length=None): return list(self._d)


def _apply_pipeline(docs, pipeline):
    rows = list(docs)
    for stage in pipeline:
        if "$match" in stage:
            rows = [d for d in rows if _match(d, stage["$match"])]
        elif "$unwind" in stage:
            field = stage["$unwind"].lstrip("$")
            out = []
            for d in rows:
                for item in (d.get(field) or []):
                    nd = dict(d); nd[field] = item
                    out.append(nd)
            rows = out
        elif "$project" in stage:
            out = []
            for d in rows:
                nd = {}
                for k, expr in stage["$project"].items():
                    if k == "_id":
                        continue
                    nd[k] = _eval(expr, d)
                out.append(nd)
            rows = out
    return rows


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        cur = doc
        for p in expr[1:].split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        return cur
    if isinstance(expr, dict) and "$ifNull" in expr:
        a, b = expr["$ifNull"]
        val = _eval(a, doc)
        return val if val is not None else b
    return expr


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
    def aggregate(self, pipeline):
        return _Cur(_apply_pipeline(self.docs, pipeline))
    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if _match(d, q):
                d.update(update.get("$set", {}))
                return
        if upsert:
            nd = {k: v for k, v in q.items() if not isinstance(v, dict)}
            nd.update(update.get("$set", {}))
            self.docs.append(nd)


class _DB:
    def __init__(self):
        self._c = {}
        for n in ("talents", "submissions", "applications", "pending_media_deletions",
                  "storage_metrics_cache"):
            self._c[n] = _Coll(n); setattr(self, n, self._c[n])
    def __getitem__(self, n):
        if n not in self._c:
            self._c[n] = _Coll(n); setattr(self, n, self._c[n])
        return self._c[n]


def run(c): return asyncio.new_event_loop().run_until_complete(c)


@pytest.fixture
def db(): return _DB()


USAGE = {
    "plan": "Advanced", "last_updated": "2026-09-01",
    "storage": {"usage": 20_000_000_000},
    "objects": {"usage": 12_741},
    "bandwidth": {"usage": 34_000_000_000},
    "transformations": {"usage": 70_940},
    "derived_resources": 8_457,
    "resources": 4_324,
    "credits": {"usage": 70.9, "limit": 60, "used_percent": 118.2},
}


def _talent_media(pid, size, lc=None):
    m = {"id": f"m-{pid}", "public_id": pid, "resource_type": "image", "size": size,
         "ownership": {"owner_type": "talent", "conflict": None}}
    if lc:
        m["lifecycle"] = {"state": lc}
    return m


def _take_media(pid, size, lc=None):
    m = {"id": f"m-{pid}", "public_id": pid, "resource_type": "video", "size": size,
         "ownership": {"owner_type": "project_submission", "conflict": None}}
    if lc:
        m["lifecycle"] = {"state": lc}
    return m


def _unknown_media(pid, size):
    return {"id": f"m-{pid}", "public_id": pid, "resource_type": "image", "size": size,
            "ownership": {"owner_type": None, "conflict": "no_metadata"}}


def _seed(db):
    db.talents.docs.append({"id": "T1", "media": [_talent_media("glob/a", 100), _talent_media("glob/b", 200)]})
    db.submissions.docs.append({"id": "S1", "media": [
        _take_media("proj/take1", 5000),
        _talent_media("glob/a", 100),   # copy-by-value — SAME public_id as T1
        _unknown_media("legacy/x", 42),
    ]})
    db.applications.docs.append({"id": "A1", "media": [_talent_media("glob/c", 300)]})


# ---------------------------------------------------------------------------
# 1–2  Cloudinary vs MongoDB separation
# ---------------------------------------------------------------------------
def test_1_cloudinary_bytes_are_cloudinary_storage(db):
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    assert acct["cloudinary"]["storage_bytes"] == 20_000_000_000
    assert acct["cloudinary"]["source"] == "cloudinary_usage_api"


def test_2_mongo_size_not_labeled_cloudinary_storage(db):
    _seed(db)
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    ar = acct["application_references"]
    assert "NOT Cloudinary storage" in ar["label"]
    assert ar["reference_bytes"] != acct["cloudinary"]["storage_bytes"]
    assert ar["reference_bytes"] <= ar["reference_bytes_raw_with_shared_copies"]


# ---------------------------------------------------------------------------
# 3–5  all three media collections counted
# ---------------------------------------------------------------------------
def test_3_4_5_all_media_collections_included(db):
    _seed(db)
    acct = run(sa.aggregate_ownership(db))
    pc = acct["application_references"]["per_collection"]
    assert pc["talents"]["items"] == 2
    assert pc["submissions"]["items"] == 3
    assert pc["applications"]["items"] == 1


# ---------------------------------------------------------------------------
# 6  derived assets distinguishable
# ---------------------------------------------------------------------------
def test_6_derived_assets_distinguishable(db):
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    assert acct["cloudinary"]["derived_objects"] == 8_457
    assert acct["cloudinary"]["original_objects"] == 4_324


# ---------------------------------------------------------------------------
# 7–10  ownership classification
# ---------------------------------------------------------------------------
def test_7_8_orphan_and_unknown_distinguishable(db):
    _seed(db)
    o = run(sa.aggregate_ownership(db))["ownership"]
    assert o["unknown_or_conflicting"]["distinct_assets"] == 1   # legacy/x


def test_9_global_talent_media_not_project_media(db):
    _seed(db)
    o = run(sa.aggregate_ownership(db))["ownership"]
    # glob/a (shared), glob/b, glob/c = 3 distinct talent-owned
    assert o["global_talent_media"]["distinct_assets"] == 3
    assert o["project_audition_media"]["distinct_assets"] == 1


def test_10_project_audition_media_not_global(db):
    _seed(db)
    o = run(sa.aggregate_ownership(db))["ownership"]
    assert o["project_audition_media"]["distinct_assets"] == 1
    assert o["project_audition_media"]["reference_bytes"] == 5000


# ---------------------------------------------------------------------------
# 13  read-only
# ---------------------------------------------------------------------------
def test_13_accounting_is_read_only(db):
    _seed(db)
    before = (len(db.talents.docs), len(db.submissions.docs), len(db.applications.docs))
    run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    after = (len(db.talents.docs), len(db.submissions.docs), len(db.applications.docs))
    assert before == after


# ---------------------------------------------------------------------------
# 15–16  bounded Cloudinary calls + freshness
# ---------------------------------------------------------------------------
def test_15_repeated_loads_do_not_refetch_within_ttl(db):
    calls = []
    def fetch():
        calls.append(1); return USAGE
    run(sa.build_accounting(db, usage_fetcher=fetch, now=NOW))
    run(sa.build_accounting(db, usage_fetcher=fetch, now=NOW + timedelta(seconds=60)))
    run(sa.build_accounting(db, usage_fetcher=fetch, now=NOW + timedelta(seconds=120)))
    assert len(calls) == 1  # served from cache


def test_15b_refetch_after_ttl(db):
    calls = []
    def fetch():
        calls.append(1); return USAGE
    run(sa.build_accounting(db, usage_fetcher=fetch, now=NOW))
    run(sa.build_accounting(db, usage_fetcher=fetch, now=NOW + timedelta(seconds=600)))
    assert len(calls) == 2


def test_16_freshness_exposed(db):
    run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW + timedelta(seconds=120)))
    f = acct["freshness"]["cloudinary_usage"]
    assert f["age_seconds"] == 120
    assert f["ttl_seconds"] == 300
    assert f["stale"] is False
    assert acct["freshness"]["full_inventory_scan"]["status"] == "never_run"


# ---------------------------------------------------------------------------
# 17  no double-count of shared global media
# ---------------------------------------------------------------------------
def test_17_shared_global_media_not_double_counted(db):
    _seed(db)
    o = run(sa.aggregate_ownership(db))
    # glob/a appears in T1 AND S1 — one distinct asset, flagged shared
    assert o["application_references"]["distinct_backing_assets"] == 5  # a,b,c,take1,legacy
    assert o["application_references"]["shared_backing_assets"] == 1


# ---------------------------------------------------------------------------
# 18  ownership uses P3 sub-document
# ---------------------------------------------------------------------------
def test_18_classification_uses_p3_ownership_not_folder(db):
    # public_id under a PROJECT folder but P3 says talent-owned
    db.submissions.docs.append({"id": "S2", "media": [{
        "id": "mf", "public_id": "talentgram/projects/P9/submissions/S2/photo",
        "resource_type": "image", "size": 10,
        "ownership": {"owner_type": "talent", "conflict": None},
    }]})
    o = run(sa.aggregate_ownership(db))["ownership"]
    assert o["global_talent_media"]["distinct_assets"] == 1
    assert o["project_audition_media"]["distinct_assets"] == 0


# ---------------------------------------------------------------------------
# 19  pending deletion visible, not deleted
# ---------------------------------------------------------------------------
def test_19_pending_deletion_visible_in_lifecycle(db):
    db.submissions.docs.append({"id": "S3", "media": [_take_media("proj/t", 100, lc="pending_deletion")]})
    db.pending_media_deletions.docs.append({
        "public_id": "proj/orphaned-take", "owner_type": "project_submission",
        "eligible_at": (NOW - timedelta(days=1)).isoformat(),
    })
    db.pending_media_deletions.docs.append({
        "public_id": "proj/waiting", "owner_type": "project_submission",
        "eligible_at": (NOW + timedelta(days=10)).isoformat(),
    })
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    assert acct["lifecycle"]["pending_deletion"]["distinct_assets"] == 1
    led = acct["lifecycle"]["ledger"]
    assert led["total"] == 2
    assert led["eligible_for_cleanup"] == 1
    assert led["waiting_on_retention"] == 1


# ---------------------------------------------------------------------------
# 20  reconciliation gap is explained, never claims precision it lacks
# ---------------------------------------------------------------------------
def test_20_reconciliation_gap_explained(db):
    _seed(db)
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    rec = acct["reconciliation"]
    assert rec["cloudinary_actual_storage_bytes"] == 20_000_000_000
    assert rec["unaccounted_bytes"] > 0
    assert "derived" in rec["explanation"] and "orphan" in rec["explanation"]


def test_usage_falls_back_to_stale_cache_on_fetch_failure(db):
    run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    def boom():
        raise RuntimeError("cloudinary down")
    acct = run(sa.build_accounting(db, usage_fetcher=boom, now=NOW + timedelta(hours=2)))
    assert acct["cloudinary"]["storage_bytes"] == 20_000_000_000
    assert acct["cloudinary"]["stale"] is True


# ---------------------------------------------------------------------------
# soft-delete query audit — core.active_only helper
# ---------------------------------------------------------------------------
def test_active_only_excludes_deleted_by_default():
    from core import active_only
    q = active_only({"project_id": "P1"})
    assert q["lifecycle_state"] == {"$ne": "deleted"}


def test_active_only_include_deleted_passthrough():
    from core import active_only
    q = active_only({"project_id": "P1"}, include_deleted=True)
    assert "lifecycle_state" not in q


def test_active_only_exclude_archived():
    from core import active_only
    q = active_only({}, exclude_archived=True)
    assert q["lifecycle_state"] == {"$nin": ["deleted", "archived"]}


def test_active_only_respects_caller_lifecycle_constraint():
    from core import active_only
    q = active_only({"lifecycle_state": "deleted"})
    assert q["lifecycle_state"] == "deleted"


def test_11_soft_deleted_project_excluded_from_list(monkeypatch):
    # projects.list_projects builds its query via active_only — a deleted project
    # is filtered unless include_deleted is passed.
    import inspect
    from routers import projects
    src = inspect.getsource(projects.list_projects)
    assert "active_only" in src and "include_deleted" in src


def test_12_historical_query_can_include_soft_deleted():
    from core import active_only
    hist = active_only({"talent_id": "T1"}, include_deleted=True)
    assert hist == {"talent_id": "T1"}


def test_14_no_credentials_in_accounting_payload(db):
    _seed(db)
    acct = run(sa.build_accounting(db, usage_fetcher=lambda: USAGE, now=NOW))
    blob = repr(acct).lower()
    for secret in ("api_secret", "api_key", "cloudinary://", os.environ["CLOUDINARY_API_SECRET"].lower()):
        assert secret not in blob


def test_client_link_queries_filter_soft_deleted():
    import inspect
    from routers import links
    src = inspect.getsource(links.get_public_link)
    assert "_NOT_DELETED" in src
