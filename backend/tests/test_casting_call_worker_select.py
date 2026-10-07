"""POST /api/whatsapp/casting-call/send — "Send via" worker selection.

The admin now picks which WhatsApp worker sends a casting call. The chosen `worker_id` rides the
EXISTING send pipeline (same _create_batch_internal, same templates, same recipient resolution): the
batch and every job it creates carry that worker, and the worker process that owns it is the only one
that claims them. The backend validates the choice (registered, sending enabled, currently connected),
never substitutes another worker, and rejects before creating anything.

Real FastAPI app + real local dev DB (same approach as test_casting_call_send.py); the throwaway workers
live in the real `whatsapp_workers` / `whatsapp_sessions` collections and are removed afterwards.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL

import uuid

import pytest
import pytest_asyncio

import core
from core import _now, db
from dotenv import dotenv_values
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from routers.whatsapp_workers import DEFAULT_WORKER_ID
from test_casting_call_send import (  # noqa: F401  (reuse the module-scoped client + helpers)
    _cleanup, _make_custom_template, _make_project, _make_talent_in_pipeline, client,
)

_aio = pytest.mark.asyncio(loop_scope="module")
URL = "/api/whatsapp/casting-call/send"
_made_workers: list = []


async def _worker(label, status="authenticated", sending=True):
    wid = f"zzz-test-cc-w-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_workers.insert_one({"id": wid, "label": label, "sending_enabled": sending, "created_at": _now()})
    await db.whatsapp_sessions.insert_one({"id": wid, "status": status, "last_heartbeat": _now()})
    _made_workers.append(wid)
    return wid


def _is_db_or_stand_in(value) -> bool:
    """A module-level `db` that is a real motor database, or a stand-in some other test module
    installed over it at import time (MagicMock / hand-rolled FakeDB classes)."""
    return isinstance(value, (MagicMock, AsyncIOMotorDatabase)) or type(value).__name__.startswith("Fake")


def _intended_db_name() -> str:
    """The DB an un-polluted run of this file uses (backend/.env, where the seeded admin lives).
    Not core.DB_NAME: when the whole suite runs in one process an earlier-collected module
    (e.g. test_ai_scout, test_apply_location_merge) has already forced DB_NAME to its own."""
    return dotenv_values(Path(core.__file__).parent / ".env").get("DB_NAME") or core.DB_NAME


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _real_db_for_this_module():
    # Other test modules leave global state behind when the whole suite runs in one process:
    # some replace `core.db` (and every `from core import db` copy) with a MagicMock / FakeDB, others
    # leave a motor client bound to an event loop that is already closed. Either way a
    # real-DB test collected later errors before it asserts anything (the same reason
    # test_casting_call_send.py errors in a full run). Give THIS module its own real client on
    # its own loop, point every module-level `db` at it for the module's duration, and put the
    # previous objects back afterwards so no other module sees any difference.
    real_client = AsyncIOMotorClient(_MONGO_URL, serverSelectionTimeoutMS=10_000)
    real_db = real_client[_intended_db_name()]
    swapped = [
        (mod, mod.db) for mod in list(sys.modules.values())
        if _is_db_or_stand_in(getattr(mod, "db", None))
    ]
    for mod, _ in swapped:
        mod.db = real_db
    yield
    try:
        await real_db.whatsapp_workers.delete_many({"id": {"$in": _made_workers}})
        await real_db.whatsapp_sessions.delete_many({"id": {"$in": _made_workers}})
        await real_db.whatsapp_jobs.delete_many({"worker_id": {"$in": _made_workers}})
        await real_db.whatsapp_batches.delete_many({"worker_id": {"$in": _made_workers}})
    finally:
        for mod, original in swapped:
            mod.db = original
        real_client.close()


async def _setup(n_talents=1, n_projects=1):
    pids = [(await _make_project())[0] for _ in range(n_projects)]
    tids = []
    for _ in range(n_talents):
        t = None
        for p in pids:
            t_id, _ = await _make_talent_in_pipeline(p) if t is None else (t, None)
            if t is None:
                t = t_id
            else:  # same talent in the other project(s)
                await db.casting_pipeline.insert_one({"id": f"zzz-test-cc-row-{uuid.uuid4().hex[:8]}", "project_id": p, "talent_id": t, "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()})
        tids.append(t)
    tmpl_id, _ = await _make_custom_template("Hi {{first_name}} — {{project_name}}")
    return pids, tids, tmpl_id


async def _done(pids, tids, tmpl):
    for p in pids:
        await _cleanup(p)
    await _cleanup(None, tids, tmpl)


async def _jobs(tids):
    return await db.whatsapp_jobs.find({"talent_id": {"$in": tids}}, {"_id": 0}).to_list(500)


async def _batches(pids):
    return await db.whatsapp_batches.find({"project_id": {"$in": pids}}, {"_id": 0}).to_list(100)


# ------------------------------------------------------------------ the selected worker is the one on the job
@_aio
async def test_a_valid_worker_is_accepted_and_stamped_on_the_batch_and_every_job(client):
    w = await _worker("Worker A")
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": w})
        assert r.status_code == 201, r.text
        assert r.json()["worker_id"] == w
        jobs, batches = await _jobs(tids), await _batches(pids)
        assert len(jobs) == 1 and len(batches) == 1
        assert [j["worker_id"] for j in jobs] == [w] and batches[0]["worker_id"] == w
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_switching_workers_routes_each_send_to_its_own_worker(client):
    w1, w2 = await _worker("Worker One"), await _worker("Worker Two")
    for chosen in (w1, w2, w1):
        pids, tids, tmpl = await _setup(1, 1)
        try:
            r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": chosen})
            assert r.status_code == 201, r.text
            assert {j["worker_id"] for j in await _jobs(tids)} == {chosen}
        finally:
            await _done(pids, tids, tmpl)


@_aio
async def test_multiple_talents_x_multiple_projects_all_use_the_chosen_worker_and_the_job_count_is_unchanged(client):
    w = await _worker("Worker B")
    pids, tids, tmpl = await _setup(3, 2)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": w})
        assert r.status_code == 201, r.text
        jobs, batches = await _jobs(tids), await _batches(pids)
        assert len(jobs) == 3 * 2                                   # exactly talents x projects, no duplicates
        assert len(batches) == 2
        assert {j["worker_id"] for j in jobs} == {w} and {b["worker_id"] for b in batches} == {w}
        assert len({(j["talent_id"], j["batch_id"]) for j in jobs}) == 6
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_choosing_a_worker_changes_nothing_but_the_worker_same_jobs_as_the_legacy_call(client):
    w = await _worker("Worker C")
    shape = {}
    for label, extra in (("legacy", {}), ("chosen", {"worker_id": w})):
        pids, tids, tmpl = await _setup(2, 2)
        try:
            r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, **extra})
            assert r.status_code == 201, r.text
            jobs = await _jobs(tids)
            shape[label] = (len(jobs), sorted(j["status"] for j in jobs), sorted(j["source"] for j in jobs),
                            sorted(b["queued"] for b in r.json()["batches"]), sorted(b["skipped"] for b in r.json()["batches"]))
        finally:
            await _done(pids, tids, tmpl)
    assert shape["legacy"] == shape["chosen"]


# ------------------------------------------------------------------ validation: reject, never substitute
@_aio
async def test_unknown_worker_is_rejected_and_creates_nothing(client):
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": "no-such-worker"})
        assert r.status_code == 404 and "no-such-worker" in r.json()["detail"]
        assert await _jobs(tids) == [] and await _batches(pids) == []
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_a_disconnected_or_unauthenticated_worker_is_rejected_with_no_silent_fallback(client):
    connected = await _worker("Connected one")
    down, qr = await _worker("Disconnected one", status="disconnected"), await _worker("Needs QR", status="qr_pending")
    for bad, label in ((down, "Disconnected one"), (qr, "Needs QR")):
        pids, tids, tmpl = await _setup(1, 1)
        try:
            r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": bad})
            assert r.status_code == 409, r.text
            assert label in r.json()["detail"] and "not connected" in r.json()["detail"]
            # nothing queued ANYWHERE: not under the bad worker, not under the connected one, not under the default
            assert await _jobs(tids) == [] and await _batches(pids) == []
            assert await db.whatsapp_jobs.count_documents({"worker_id": {"$in": [connected, bad, DEFAULT_WORKER_ID]}, "talent_id": {"$in": tids}}) == 0
        finally:
            await _done(pids, tids, tmpl)


@_aio
async def test_a_worker_whose_session_record_is_missing_counts_as_not_connected(client):
    wid = f"zzz-test-cc-w-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_workers.insert_one({"id": wid, "label": "No session yet", "sending_enabled": True, "created_at": _now()})
    _made_workers.append(wid)
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": wid})
        assert r.status_code == 409 and await _jobs(tids) == []
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_a_connected_worker_that_is_not_enabled_for_sending_is_rejected(client):
    w = await _worker("Not enabled", sending=False)
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": w})
        assert r.status_code == 403 and "not enabled" in r.json()["detail"]
        assert await _jobs(tids) == [] and await _batches(pids) == []
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_an_unavailable_worker_rejects_the_whole_multi_project_send_no_partial_jobs(client):
    down = await _worker("Down", status="disconnected")
    pids, tids, tmpl = await _setup(2, 3)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": down})
        assert r.status_code == 409
        assert await _jobs(tids) == [] and await _batches(pids) == []
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_a_blank_worker_id_is_rejected_not_defaulted(client):
    pids, tids, tmpl = await _setup(1, 1)
    try:
        for blank in ("", "   "):
            r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, "worker_id": blank})
            assert r.status_code == 400 and "worker" in r.json()["detail"].lower()
        assert await _jobs(tids) == []
    finally:
        await _done(pids, tids, tmpl)


# ------------------------------------------------------------------ nothing existing changed
@_aio
async def test_without_worker_id_the_legacy_behaviour_is_preserved(client):
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl})
        assert r.status_code == 201, r.text
        assert "worker_id" not in r.json()
        assert {j["worker_id"] for j in await _jobs(tids)} == {DEFAULT_WORKER_ID}      # BatchIn's pre-existing default
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_existing_template_and_selection_validation_is_intact_with_a_worker_present(client):
    w = await _worker("Worker D")
    pids, tids, tmpl = await _setup(1, 1)
    try:
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "worker_id": w})
        assert r.status_code == 400 and "template" in r.json()["detail"].lower()
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": "does-not-exist", "worker_id": w})
        assert r.status_code == 404
        r = await client.post(URL, json={"talent_ids": [], "project_ids": pids, "template_id": tmpl, "worker_id": w})
        assert r.status_code == 400
        r = await client.post(URL, json={"talent_ids": tids, "project_ids": [], "template_id": tmpl, "worker_id": w})
        assert r.status_code == 400
        assert await _jobs(tids) == []
    finally:
        await _done(pids, tids, tmpl)


@_aio
async def test_a_repeated_send_does_not_duplicate_and_a_new_chosen_worker_does_not_resend_past_the_existing_rules(client):
    """The send path's own duplicate behaviour is untouched: sending the same talent x project twice
    yields the same job count whether or not a worker is chosen."""
    w = await _worker("Worker E")
    counts = {}
    for label, extra in (("legacy", {}), ("chosen", {"worker_id": w})):
        pids, tids, tmpl = await _setup(1, 1)
        try:
            first = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, **extra})
            second = await client.post(URL, json={"talent_ids": tids, "project_ids": pids, "template_id": tmpl, **extra})
            counts[label] = (first.status_code, second.status_code, len(await _jobs(tids)))
        finally:
            await _done(pids, tids, tmpl)
    assert counts["legacy"] == counts["chosen"]
