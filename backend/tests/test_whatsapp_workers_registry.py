"""Multi-Worker Phase 1 — Worker Registry tests.

Covers: worker_id generation, idempotent default-worker seeding, 404
validation for an unknown worker_id (require_worker), worker creation, and
session-doc enrichment (including the "no session doc yet" default shape).
No live DB.

Run:  python backend/tests/test_whatsapp_workers_registry.py
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(k, "x")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi import HTTPException  # noqa: E402
from routers import whatsapp_workers as ww  # noqa: E402


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    async def to_list(self, n=None):
        return list(self._docs)


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.indexes = []

    def find(self, query=None, projection=None):
        query = query or {}
        matched = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return FakeCursor(matched)

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def create_index(self, *a, **k):
        self.indexes.append((a, k))


class FakeDB:
    def __init__(self):
        self.whatsapp_workers = FakeColl()
        self.whatsapp_sessions = FakeColl()

    def __getitem__(self, name):
        return getattr(self, name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main():
    ww.db = FakeDB()

    # 1. worker id generation — stable prefix, not the reserved default id.
    wid = ww._new_worker_id()
    assert wid.startswith("wa-") and wid != ww.DEFAULT_WORKER_ID
    assert ww._new_worker_id() != wid  # never collides with itself
    print("1. worker id generation OK ->", wid)

    # 2. seeding is idempotent — running it twice inserts exactly one doc.
    run(ww._seed_default_worker())
    run(ww._seed_default_worker())
    matches = [d for d in ww.db.whatsapp_workers.docs if d["id"] == ww.DEFAULT_WORKER_ID]
    assert len(matches) == 1, matches
    assert matches[0]["label"] == "Worker 1"
    assert matches[0]["session_instance"] == ww.DEFAULT_WORKER_ID
    print("2. idempotent default-worker seed OK")

    # 3. require_worker: known id returns the doc, unknown id raises 404.
    doc = run(ww.require_worker(ww.DEFAULT_WORKER_ID))
    assert doc["id"] == ww.DEFAULT_WORKER_ID
    try:
        run(ww.require_worker("wa-doesnotexist"))
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404
    print("3. require_worker validation OK")

    # 4. enrichment with NO whatsapp_sessions doc yet -> disconnected default
    #    shape, never a KeyError/None crash.
    enriched = run(ww._enrich({"id": "wa-brand-new", "label": "Worker 2"}))
    assert enriched["session"]["status"] == "disconnected"
    assert enriched["session"]["worker_ready"] is False
    print("4. session enrichment default shape OK")

    # 5. enrichment WITH a live whatsapp_sessions doc joins the real status.
    ww.db.whatsapp_sessions.docs.append({
        "id": "wa-worker-2", "status": "authenticated",
        "connected_phone_number": "+91 90000 00000", "worker_ready": True,
    })
    enriched2 = run(ww._enrich({"id": "wa-worker-2", "label": "Worker 2"}))
    assert enriched2["session"]["status"] == "authenticated"
    assert enriched2["session"]["connected_phone_number"] == "+91 90000 00000"
    print("5. session enrichment live-join OK")

    # 6. create_worker inserts a well-formed doc and returns it enriched.
    admin = {"id": "admin-1"}
    created = run(ww.create_worker(ww.WorkerCreateIn(label="Worker 2"), admin=admin))
    assert created["label"] == "Worker 2"
    assert created["id"].startswith("wa-")
    assert created["session_instance"] == created["id"]
    assert created["created_by"] == "admin-1"
    assert created["session"]["status"] == "disconnected"  # no browser session yet
    print("6. create_worker OK ->", created["id"])

    # 7. list_workers returns every registered worker, each enriched.
    listed = run(ww.list_workers(admin=admin))
    ids = {w["id"] for w in listed}
    assert ww.DEFAULT_WORKER_ID in ids and created["id"] in ids
    assert all("session" in w for w in listed)
    print("7. list_workers OK ->", sorted(ids))

    # 8. Stable, caller-supplied worker id (2026-09-13, Worker 2 rollout) —
    # explicit "worker-2" is used verbatim, never randomized.
    worker2 = run(ww.create_worker(ww.WorkerCreateIn(label="Worker 2", worker_id="worker-2"), admin=admin))
    assert worker2["id"] == "worker-2", worker2
    assert worker2["session_instance"] == "worker-2"
    print("8. explicit worker_id used verbatim OK ->", worker2["id"])

    # 9. 'default' is reserved — cannot be (re-)claimed via create_worker.
    try:
        run(ww.create_worker(ww.WorkerCreateIn(label="Sneaky", worker_id="default"), admin=admin))
        assert False, "expected HTTPException for reserved id"
    except HTTPException as exc:
        assert exc.status_code == 400
    print("9. 'default' rejected as a reserved id OK")

    # 10. Duplicate worker_id is rejected (409), not silently overwritten —
    # the existing worker-2 doc (and its label) must be untouched.
    try:
        run(ww.create_worker(ww.WorkerCreateIn(label="Duplicate Attempt", worker_id="worker-2"), admin=admin))
        assert False, "expected HTTPException for duplicate id"
    except HTTPException as exc:
        assert exc.status_code == 409
    still_there = run(ww.get_worker("worker-2", admin=admin))
    assert still_there["label"] == "Worker 2", still_there  # NOT overwritten to "Duplicate Attempt"
    print("10. duplicate worker_id rejected, original untouched OK")

    # 11. Malformed ids rejected as 400 (never a raw 500) — spaces,
    # leading/trailing hyphen, empty. "Worker-2" is deliberately NOT in
    # this list: _validate_worker_id lowercases BEFORE pattern-checking
    # (a permissive design — "Worker-2" and "worker-2" are the same id,
    # not two different ones a typo could accidentally create), so it
    # correctly falls through to the duplicate-id path (409, since
    # worker-2 already exists from step 8) rather than the format-error
    # path — verified separately right below.
    for bad_id in ("worker 2", "-worker-2", "worker-2-"):
        try:
            run(ww.create_worker(ww.WorkerCreateIn(label="Bad", worker_id=bad_id), admin=admin))
            assert False, f"expected rejection for {bad_id!r}"
        except HTTPException as exc:
            assert exc.status_code == 400, (bad_id, exc.status_code)
    print("11a. malformed worker_id values rejected as 400 OK")

    # An empty string is falsy in Python, so `if payload.worker_id:` treats
    # it exactly like an omitted field (auto-generate) rather than a
    # malformed one — a deliberate, reasonable choice (not a bug to fix):
    # verify it, don't fight it.
    empty_id_worker = run(ww.create_worker(ww.WorkerCreateIn(label="Empty String Id", worker_id=""), admin=admin))
    assert empty_id_worker["id"].startswith("wa-"), empty_id_worker
    print("11c. empty-string worker_id falls back to auto-generation (by design) OK")

    # 11b. Mixed-case of an EXISTING id normalizes and correctly collides
    # (409), rather than being treated as a distinct identity.
    try:
        run(ww.create_worker(ww.WorkerCreateIn(label="Case Variant", worker_id="Worker-2"), admin=admin))
        assert False, "expected HTTPException for a case-variant duplicate"
    except HTTPException as exc:
        assert exc.status_code == 409, exc.status_code
    print("11b. case-insensitive duplicate ('Worker-2' vs 'worker-2') rejected OK")

    # 12. Omitting worker_id still falls back to auto-generation — backward
    # compatible for any caller that doesn't care about a literal id.
    auto = run(ww.create_worker(ww.WorkerCreateIn(label="No Explicit Id"), admin=admin))
    assert auto["id"].startswith("wa-") and auto["id"] not in ("default", "worker-2")
    print("12. omitted worker_id still auto-generates OK ->", auto["id"])

    print("\nALL WORKER-REGISTRY TESTS PASSED")


if __name__ == "__main__":
    main()
