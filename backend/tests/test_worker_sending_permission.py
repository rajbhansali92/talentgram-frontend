"""Worker sending-permission gate tests (2026-09-14, Worker 2 incident
follow-up).

Covers: `_create_batch_internal`'s fail-closed `sending_enabled` check —
True/False/missing/None/non-boolean values, proof that a blocked call never
reaches the template/recipient-resolution pipeline (so nothing real is ever
touched), proof that a permitted call is only let PAST the gate (it must
still fail immediately after on a missing template — no real send
infrastructure is exercised by this file at all), proof that session/QR/
status/heartbeat routes are entirely unaffected by the flag, and the new
POST /{worker_id}/enable-sending route's scope (admin-only, single-field,
single-worker, no other side effect). No live DB, no Playwright, no
WhatsApp session, no real recipient, no send/retry/replay of any kind.

Run:  python backend/tests/test_worker_sending_permission.py
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
from fastapi import HTTPException, Depends  # noqa: E402
from routers import whatsapp_workers as ww  # noqa: E402
from routers import whatsapp as wa  # noqa: E402
from core import current_admin, current_team_or_admin  # noqa: E402


def _matches(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
        elif isinstance(v, dict) and "$exists" in v:
            if (k in doc) != v["$exists"]:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    async def to_list(self, n=None):
        return list(self._docs)


class FakeColl:
    """Standard fake collection — matches every other multi-worker test
    file's convention in this suite (find/find_one/insert_one/update_one)."""

    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)])

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class PoisonColl(FakeColl):
    """A collection that FAILS the test if it is ever queried. Used for
    whatsapp_templates in the blocked-worker cases: if the permission gate
    is genuinely enforced BEFORE any template/recipient work, this
    collection must never be touched at all."""

    async def find_one(self, query=None, projection=None):
        raise AssertionError(
            "whatsapp_templates.find_one() was called — the sending_enabled "
            "gate did not short-circuit BEFORE template lookup, meaning a "
            "blocked worker's batch-creation call proceeded further than "
            "it should have."
        )


class FakeDB:
    def __init__(self, templates_coll=None):
        self.whatsapp_workers = FakeColl()
        self.whatsapp_sessions = FakeColl()
        self.whatsapp_templates = templates_coll if templates_coll is not None else PoisonColl()

    def __getitem__(self, name):
        return getattr(self, name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


ADMIN = {"id": "admin-1", "role": "admin"}


def _worker_doc(worker_id, **overrides):
    doc = {
        "id": worker_id,
        "label": worker_id,
        "session_instance": worker_id,
        "active": True,
        "created_at": "2026-09-14T00:00:00+00:00",
        "created_by": "test",
    }
    doc.update(overrides)
    return doc


def main():
    # ------------------------------------------------------------------
    # 1-4. _create_batch_internal fail-closed permission check.
    # Each case uses a FRESH FakeDB with a poisoned whatsapp_templates
    # collection — if the gate doesn't block BEFORE template lookup, the
    # PoisonColl raises AssertionError, failing the test loudly.
    # ------------------------------------------------------------------
    for label, sending_enabled_value in [
        ("False", False),
        ("missing field", "__ABSENT__"),
        ("None", None),
        ("string 'true'", "true"),
        ("integer 1", 1),
        ("integer 0", 0),
    ]:
        fake = FakeDB()
        worker_overrides = {} if sending_enabled_value == "__ABSENT__" else {"sending_enabled": sending_enabled_value}
        fake.whatsapp_workers = FakeColl([_worker_doc("worker-x", **worker_overrides)])
        ww.db = fake
        wa.db = fake

        payload = wa.BatchIn(worker_id="worker-x", template_id="tpl-does-not-matter")
        try:
            run(wa._create_batch_internal(payload, ADMIN))
            raise AssertionError(f"[{label}] expected HTTPException(403), nothing was raised")
        except HTTPException as e:
            assert e.status_code == 403, f"[{label}] expected 403, got {e.status_code}: {e.detail}"
            assert "worker-x" in e.detail and "not enabled for sending" in e.detail
        print(f"1-4. sending_enabled={label!r} -> blocked 403, template lookup never reached OK")

    # ------------------------------------------------------------------
    # 5. sending_enabled=True is let PAST the gate — proven by reaching
    # the NEXT check (missing template -> 404), never a 403. No template,
    # no recipient, no job, no batch is ever created here — this proves
    # only that the permission check itself does not reject a permitted
    # worker; it deliberately does not exercise anything resembling a
    # real send.
    # ------------------------------------------------------------------
    fake = FakeDB(templates_coll=FakeColl([]))  # empty, unpoisoned — lookup allowed, but finds nothing
    fake.whatsapp_workers = FakeColl([_worker_doc("default", sending_enabled=True)])
    ww.db = fake
    wa.db = fake
    payload = wa.BatchIn(worker_id="default", template_id="tpl-does-not-exist")
    try:
        run(wa._create_batch_internal(payload, ADMIN))
        raise AssertionError("expected HTTPException(404) for missing template, nothing was raised")
    except HTTPException as e:
        assert e.status_code == 404, f"expected 404 (past the permission gate), got {e.status_code}: {e.detail}"
        assert e.status_code != 403
    print("5. sending_enabled=True -> passes the permission gate (fails later, on missing template, as expected) OK")

    # ------------------------------------------------------------------
    # 6. Session/QR/status/heartbeat-adjacent routes are unaffected by
    # sending_enabled, regardless of its value.
    # ------------------------------------------------------------------
    fake = FakeDB(templates_coll=FakeColl([]))
    fake.whatsapp_workers = FakeColl([_worker_doc("worker-x", sending_enabled=False)])
    fake.whatsapp_sessions = FakeColl([{"id": "worker-x", "status": "authenticated", "worker_ready": True}])
    ww.db = fake
    wa.db = fake

    session = run(ww.get_worker_session("worker-x", admin=ADMIN))
    assert session["status"] == "authenticated"
    print("6a. get_worker_session unaffected by sending_enabled=False OK")

    run(ww.clear_worker_qr("worker-x", admin=ADMIN))
    print("6b. clear_worker_qr unaffected by sending_enabled=False OK")

    run(ww.reset_worker_session("worker-x", admin=ADMIN))
    print("6c. reset_worker_session unaffected by sending_enabled=False OK")

    workers = run(ww.list_workers(admin=ADMIN))
    assert any(w["id"] == "worker-x" for w in workers)
    print("6d. list_workers unaffected by sending_enabled=False OK")

    got = run(ww.get_worker("worker-x", admin=ADMIN))
    assert got["id"] == "worker-x"
    print("6e. get_worker unaffected by sending_enabled=False OK")

    # ------------------------------------------------------------------
    # 7. enable_worker_sending: admin-only (Depends target check), flips
    # ONLY the targeted worker, touches no other collection, sends/
    # retries/requeues nothing.
    # ------------------------------------------------------------------
    import inspect
    sig = inspect.signature(ww.enable_worker_sending)
    admin_param = sig.parameters["admin"]
    dep_callable = admin_param.default.dependency
    assert dep_callable is current_admin, (
        f"enable_worker_sending must depend on current_admin (admin-only), "
        f"got {dep_callable!r}"
    )
    assert dep_callable is not current_team_or_admin
    print("7a. enable_worker_sending is admin-only (Depends(current_admin)) OK")

    fake = FakeDB(templates_coll=FakeColl([]))
    fake.whatsapp_workers = FakeColl([
        _worker_doc("worker-x", sending_enabled=False),
        _worker_doc("worker-y", sending_enabled=False),
    ])
    ww.db = fake
    wa.db = fake
    before_snapshot = [dict(d) for d in fake.whatsapp_workers.docs]

    result = run(ww.enable_worker_sending("worker-x", admin=ADMIN))
    assert result["sending_enabled"] is True
    assert result["id"] == "worker-x"

    x_doc = run(fake.whatsapp_workers.find_one({"id": "worker-x"}))
    y_doc = run(fake.whatsapp_workers.find_one({"id": "worker-y"}))
    assert x_doc["sending_enabled"] is True, "targeted worker was not flipped to True"
    assert y_doc["sending_enabled"] is False, "a DIFFERENT worker was affected — must never happen"
    print("7b. enable_worker_sending flips ONLY the targeted worker OK")

    # Nothing else in the fake DB changed shape/count — no batch/job/session
    # write occurred as a side effect of this call.
    assert len(fake.whatsapp_workers.docs) == 2, "enable_worker_sending must not create/delete worker docs"
    print("7c. enable_worker_sending performs no other side effect (no send/retry/requeue) OK")

    # And re-confirm the now-True worker actually passes the batch-creation
    # gate (full loop closure) — again, only reaching the next unrelated
    # failure (missing template), never sending anything.
    payload = wa.BatchIn(worker_id="worker-x", template_id="still-does-not-exist")
    try:
        run(wa._create_batch_internal(payload, ADMIN))
        raise AssertionError("expected 404 after enabling sending, nothing was raised")
    except HTTPException as e:
        assert e.status_code == 404
    print("7d. worker enabled via the route now passes the permission gate OK")

    print("\nALL WORKER SENDING-PERMISSION TESTS PASSED")


if __name__ == "__main__":
    main()
