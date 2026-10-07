"""Agent-originated WhatsApp work stays on the worker the command ARRIVED through.

Scenario (2026-10-07): the same `whatsapp-campaign-agent` ("Talentgram Scouting Agent") is
reachable from a second worker's number through an extra `whatsapp_agent_config` row
(agent_id + worker_id is the real key — see registry.seed_agent_config). Routing to that agent
already worked per worker, but every batch the agent created fell back to BatchIn's default
worker, so a command issued in the second worker's group queued its sends (and its delivery
reports back into that group) on the OTHER, unrelated worker. These tests pin:

  * config: one agent, one agent definition, reachable from two workers; unmapped groups, the
    same name on the wrong worker, and non-members stay blocked (group_members security);
  * propagation: every batch-creation site the campaign / casting-pipeline modules own carries
    ExecContext.worker_id; no worker -> BatchIn's own default, exactly as before;
  * safety: unknown / sending-disabled worker rejected, no jobs, never a silent fallback.

Real FastAPI-layer code against the real local dev DB (no mocks of the batch engine): a pass
proves the real create_batch wrote the real documents. Workers/groups are throwaway `zzz-test-*`
rows removed afterwards; no worker process runs, so nothing is ever sent.
"""
import os

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL
os.environ.setdefault("RECIPIENT_SEARCH_POLL_INTERVAL_SEC", "0.05")
os.environ.setdefault("RECIPIENT_SEARCH_MAX_WAIT_SEC", "1.5")
os.environ.setdefault("SEND_PREVIEW_POLL_INTERVAL_SEC", "0.05")
os.environ.setdefault("SEND_PREVIEW_MAX_WAIT_SEC", "1.5")
os.environ.setdefault("SHARE_DELIVERY_POLL_INTERVAL_SEC", "0.05")

import asyncio
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from agents import modules as agent_modules
from agents import registry
from agents.dispatcher import handle_inbound_message
from agents.models import ExecContext, batch_worker_kwargs
from agents.modules import casting_pipeline as cp
from agents.modules import whatsapp_campaign_agent as wca
from core import _now, db
from real_db_isolation import install_real_db
from routers.casting_pipeline import PIPELINE_STAGE_ORDER
from routers.whatsapp import BatchIn, ManualContact, SourceParams

agent_modules.register_all()

_aio = pytest.mark.asyncio(loop_scope="module")
pytestmark = _aio

AGENT_ID = "whatsapp-campaign-agent"
_workers: list = []
_projects: list = []
_talents: list = []
_templates: list = []
_phones: list = []
_batch_ids: list = []


@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def _real_db_for_this_module():
    # See real_db_isolation.py — own real client for this module, previous objects restored after.
    real_db, restore = install_real_db(_MONGO_URL)
    # Agent routing rows live in a scratch collection with the PRODUCTION-shaped unique key
    # (agent_id, worker_id). The local dev DB can still carry the legacy single-field
    # {agent_id} unique index (only prod had the explicit migration), which would reject a
    # second worker's row for the same agent — and real config rows must never be touched by tests.
    original_collection = registry.CONFIG_COLLECTION
    scratch = f"zzz_test_agent_config_{uuid.uuid4().hex[:8]}"
    registry.CONFIG_COLLECTION = scratch
    await real_db[scratch].create_index([("agent_id", 1), ("worker_id", 1)], unique=True, name="agent_id_worker_id_unique")
    yield
    try:
        registry.CONFIG_COLLECTION = original_collection
        await real_db.drop_collection(scratch)
        await real_db.whatsapp_workers.delete_many({"id": {"$in": _workers}})
        await real_db.whatsapp_sessions.delete_many({"id": {"$in": _workers}})
        by_worker = await real_db.whatsapp_batches.find({"worker_id": {"$in": _workers}}, {"_id": 0, "id": 1}).to_list(1000)
        ids = {b["id"] for b in by_worker} | set(_batch_ids)
        await real_db.whatsapp_jobs.delete_many({"$or": [{"worker_id": {"$in": _workers}}, {"batch_id": {"$in": list(ids)}}]})
        await real_db.whatsapp_batches.delete_many({"id": {"$in": list(ids)}})
        await real_db.casting_pipeline.delete_many({"project_id": {"$in": _projects}})
        await real_db.projects.delete_many({"id": {"$in": _projects}})
        await real_db.talents.delete_many({"id": {"$in": _talents}})
        await real_db.whatsapp_templates.delete_many({"id": {"$in": _templates}})
        for ph in _phones:
            await real_db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": ph})
            await real_db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": ph})
            await real_db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": ph})
    finally:
        restore()


# ── seed helpers ───────────────────────────────────────────────────────────

async def _worker(label: str, *, sending: bool = True) -> str:
    wid = f"zzz-test-ow-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_workers.insert_one({"id": wid, "label": label, "sending_enabled": sending, "created_at": _now()})
    await db.whatsapp_sessions.insert_one({"id": wid, "status": "authenticated", "last_heartbeat": _now()})
    _workers.append(wid)
    return wid


async def _map_scouting_group(worker_id: str, group: str) -> None:
    # The SAME mechanism the Fetcher (Divyani) row was created with.
    await registry.seed_agent_config(
        AGENT_ID, group_names=[group], security_mode="group_members", worker_id=worker_id,
    )


async def _project(label: str | None = None) -> tuple:
    pid = f"zzz-test-ow-proj-{uuid.uuid4().hex[:8]}"
    label = label or f"OW Brand {uuid.uuid4().hex[:6]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": label, "slug": pid, "status": "ongoing", "shoot_dates": "TBD",
        "budget": "TBD", "materials": [], "created_at": _now(), "updated_at": _now(),
    })
    _projects.append(pid)
    return pid, label


async def _talent(pid: str | None = None, *, name: str | None = None) -> str:
    tid = f"zzz-test-ow-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name or f"ZZZ_OW_Talent_{uuid.uuid4().hex[:6]}", "email": f"{tid}@example.com",
        "phone": "91" + str(uuid.uuid4().int)[:10], "tags": [], "media": [],
    })
    _talents.append(tid)
    if pid:
        await _in_pipeline(pid, tid)
    return tid


async def _in_pipeline(pid: str, tid: str) -> None:
    await db.casting_pipeline.insert_one({
        "id": f"zzz-test-ow-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid,
        "stage": "ask_to_test", "created_at": _now(), "updated_at": _now(),
    })


async def _template(name: str | None = None) -> tuple:
    tid = f"zzz-test-ow-tpl-{uuid.uuid4().hex[:8]}"
    name = name or f"OW Promo {uuid.uuid4().hex[:6]}"
    await db.whatsapp_templates.insert_one({
        "id": tid, "name": name, "slug": name.lower().replace(" ", "_") + uuid.uuid4().hex[:4],
        "body_text": "Hi {{talent_name}}, about {{project_name}} — reply to confirm.",
        "variables": [], "media_type": "none", "media_url": None, "media_cloudinary_id": None,
        "is_custom": False, "created_by": "test", "created_at": _now(), "updated_at": _now(),
    })
    _templates.append(tid)
    return tid, name


def _phone() -> str:
    ph = "91" + str(uuid.uuid4().int)[:9]
    _phones.append(ph)
    return ph


def _target(template_id: str, source_params: SourceParams, **extra) -> "wca._SendTarget":
    return wca._SendTarget(
        ok=True, source_type="PROJECT", source_params=source_params, recipient_label="ZZZ QA",
        template={"id": template_id, "name": "tpl"}, **extra,
    )


async def _jobs_for(batch_ids) -> list:
    return await db.whatsapp_jobs.find({"batch_id": {"$in": list(batch_ids)}}, {"_id": 0}).to_list(1000)


def _track(result: dict) -> dict:
    _batch_ids.extend(result.get("batch_ids") or [])
    return result


# ── config: one agent, two workers, security unchanged ─────────────────────

async def test_the_same_agent_definition_serves_both_workers_groups():
    w1, w2 = await _worker("QA Worker 1"), await _worker("QA Worker 2")
    g1, g2 = f"ZZZ Scouting {uuid.uuid4().hex[:6]}", f"ZZZ Scouting {uuid.uuid4().hex[:6]} (Divyani)"
    await _map_scouting_group(w1, g1)
    await _map_scouting_group(w2, g2)

    a1, cfg1 = await registry.resolve_agent_for_group(g1, w1)
    a2, cfg2 = await registry.resolve_agent_for_group(g2, w2)
    assert a1 is a2 is registry.get_agent(AGENT_ID)          # one implementation, not a fork
    assert (cfg1["agent_id"], cfg1["worker_id"]) == (AGENT_ID, w1)
    assert (cfg2["agent_id"], cfg2["worker_id"]) == (AGENT_ID, w2)
    assert cfg2["group_names"] == [g2] and cfg2["security_mode"] == "group_members" and cfg2["active"] is True


async def test_unmapped_groups_and_the_wrong_workers_name_stay_blocked():
    w1, w2 = await _worker("QA Worker 1"), await _worker("QA Worker 2")
    g1, g2 = f"ZZZ Scouting {uuid.uuid4().hex[:6]}", f"ZZZ Scouting {uuid.uuid4().hex[:6]} (Divyani)"
    await _map_scouting_group(w1, g1)
    await _map_scouting_group(w2, g2)

    assert await registry.resolve_agent_for_group(f"ZZZ Random {uuid.uuid4().hex[:6]}", w2) is None
    assert await registry.resolve_agent_for_group(g2, w1) is None     # Divyani's group is not Worker 1's
    assert await registry.resolve_agent_for_group(g1, w2) is None     # and vice versa: no cross-routing


async def test_group_members_security_applies_to_the_second_workers_row():
    w2 = await _worker("QA Worker 2")
    g2 = f"ZZZ Scouting {uuid.uuid4().hex[:6]} (Divyani)"
    await _map_scouting_group(w2, g2)
    _, cfg = await registry.resolve_agent_for_group(g2, w2)
    assert registry.is_sender_allowed(cfg, "919999999999", is_group_member=True) is True
    assert registry.is_sender_allowed(cfg, "919999999999", is_group_member=False) is False
    assert registry.is_sender_allowed(cfg, "919999999999", is_group_member=None) is False   # fail closed


async def test_seeding_the_second_workers_row_is_idempotent_and_leaves_the_first_untouched():
    w1, w2 = await _worker("QA Worker 1"), await _worker("QA Worker 2")
    g1, g2 = f"ZZZ Scouting {uuid.uuid4().hex[:6]}", f"ZZZ Scouting {uuid.uuid4().hex[:6]} (Divyani)"
    await _map_scouting_group(w1, g1)
    before = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID, "worker_id": w1}, {"_id": 0})
    await _map_scouting_group(w2, g2)
    await _map_scouting_group(w2, "ZZZ ignored second seed")           # never overwrites
    rows = await db[registry.CONFIG_COLLECTION].find({"worker_id": {"$in": [w1, w2]}}, {"_id": 0}).to_list(10)
    assert len(rows) == 2
    assert next(r for r in rows if r["worker_id"] == w1) == before
    assert next(r for r in rows if r["worker_id"] == w2)["group_names"] == [g2]


# ── propagation: the real dispatcher -> real executor -> real create_batch ──

@pytest.mark.parametrize("which", ["worker_1", "worker_2"])
async def test_a_share_command_from_either_workers_group_queues_on_that_worker(which, monkeypatch):
    # The live send command (SHARE) end to end: real dispatcher -> same agent -> _share_executor ->
    # real create_batch. The delivery-report watcher (fire-and-forget) is shortened so its report
    # back into the group is created within the test and can be checked too.
    monkeypatch.setattr(cp, "_SHARE_DELIVERY_MAX_WAIT_SEC", 0.3)
    w_this, w_other = await _worker(f"QA {which}"), await _worker("QA other")
    group = f"ZZZ Scouting {uuid.uuid4().hex[:6]}"
    await _map_scouting_group(w_this, group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    pid, label = await _project(f"OWProj {tag}")
    t1 = await _talent(pid, name=f"OWTalent {tag}")

    r = await handle_inbound_message(
        group_name=group, sender_phone=phone, sender_name="QA", sender_is_group_member=True,
        text=f"share casting call for {label} to OWTalent {tag}", worker_id=w_this,
    )
    assert r.handled, r.reply
    assert "Shared." in r.reply and "1 WhatsApp message queued." in r.reply, r.reply

    jobs = await db.whatsapp_jobs.find({"talent_id": t1}, {"_id": 0}).to_list(10)
    assert len(jobs) == 1 and jobs[0]["worker_id"] == w_this           # exactly one, on the originating worker
    batch = await db.whatsapp_batches.find_one({"id": jobs[0]["batch_id"]}, {"_id": 0})
    assert batch["worker_id"] == w_this

    await asyncio.sleep(0.9)                                           # let the delivery-report watcher post back
    reports = await db.whatsapp_batches.find({"worker_id": w_this, "source_type": "MANUAL"}, {"_id": 0}).to_list(10)
    assert len(reports) == 1                                           # one report, not duplicated
    report_jobs = await _jobs_for([reports[0]["id"]])
    assert len(report_jobs) == 1 and report_jobs[0]["worker_id"] == w_this
    assert await db.whatsapp_batches.count_documents({"worker_id": w_other}) == 0
    assert await db.whatsapp_jobs.count_documents({"worker_id": w_other}) == 0


async def test_a_non_member_in_a_mapped_group_creates_nothing():
    w2 = await _worker("QA Worker 2")
    group = f"ZZZ Scouting {uuid.uuid4().hex[:6]} (Divyani)"
    await _map_scouting_group(w2, group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    pid, label = await _project(f"OWProj {tag}")
    t1 = await _talent(pid, name=f"OWTalent {tag}")
    r = await handle_inbound_message(
        group_name=group, sender_phone=phone, sender_name="Stranger", sender_is_group_member=False,
        text=f"share casting call for {label} to OWTalent {tag}", worker_id=w2,
    )
    assert "Shared." not in (r.reply or "")
    assert await db.whatsapp_jobs.count_documents({"talent_id": t1}) == 0


async def test_an_unmapped_group_is_ignored_by_the_agent():
    w2 = await _worker("QA Worker 2")
    r = await handle_inbound_message(
        group_name=f"ZZZ Random {uuid.uuid4().hex[:6]}", sender_phone=_phone(), sender_name="QA",
        sender_is_group_member=True, text="Send campaign to anything using anything template", worker_id=w2,
    )
    assert r.handled is False


# ── propagation: shared batch helper, fan-out and counts ───────────────────

async def test_three_talents_one_project_is_one_batch_three_jobs_all_on_the_originating_worker():
    w2 = await _worker("QA Worker 2")
    pid, _ = await _project()
    tid, _ = await _template()
    tals = [await _talent(pid) for _ in range(3)]
    result = _track(await wca._create_batch_for_target(
        _target(tid, SourceParams(project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER), talent_ids=tals)),
        {}, is_dry_run=False, worker_id=w2,
    ))
    jobs = await _jobs_for(result["batch_ids"])
    assert len(result["batch_ids"]) == 1 and len(jobs) == 3
    assert {j["worker_id"] for j in jobs} == {w2}
    assert (await db.whatsapp_batches.find_one({"id": result["batch_ids"][0]}))["worker_id"] == w2
    assert len({j["talent_id"] for j in jobs}) == 3


async def test_three_talents_two_projects_six_jobs_all_on_the_originating_worker_no_duplicates():
    w2 = await _worker("QA Worker 2")
    p1, _ = await _project()
    p2, _ = await _project()
    tid, _ = await _template()
    tals = []
    for _ in range(3):
        t = await _talent(p1)
        await _in_pipeline(p2, t)
        tals.append(t)
    target = _target(
        tid, SourceParams(project_id=p1, pipeline_stages=list(PIPELINE_STAGE_ORDER), talent_ids=tals),
        multi_project_targets=[(p1, "P1", tals), (p2, "P2", tals)],
    )
    result = _track(await wca._create_batch_for_target(target, {}, is_dry_run=False, worker_id=w2))
    jobs = await _jobs_for(result["batch_ids"])
    # The campaign agent's documented fan-out: one project's jobs per call, merged under ONE
    # batch document (so "one send command" is still "one batch" to every consumer).
    assert len(result["batch_ids"]) == 1 and len(jobs) == 6
    assert {j["worker_id"] for j in jobs} == {w2}
    assert len({(j["talent_id"], j["source_id"]) for j in jobs}) == 6        # no duplicates
    assert {j["source_id"] for j in jobs} == {p1, p2}                        # both projects were sent
    assert (await db.whatsapp_batches.find_one({"id": result["batch_ids"][0]}))["worker_id"] == w2


async def test_no_worker_keeps_batchins_default_exactly_as_before():
    assert batch_worker_kwargs(None) == {} and batch_worker_kwargs("") == {}
    assert batch_worker_kwargs("worker-x") == {"worker_id": "worker-x"}
    pid, _ = await _project()
    tid, _ = await _template()
    t = await _talent(pid)
    sp = SourceParams(project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER), talent_ids=[t])
    legacy = _track(await wca._create_batch_for_target(_target(tid, sp), {}, is_dry_run=False))
    explicit = _track(await wca._create_batch_for_target(_target(tid, sp), {}, is_dry_run=False, worker_id="default"))
    for res in (legacy, explicit):
        assert (await db.whatsapp_batches.find_one({"id": res["batch_ids"][0]}))["worker_id"] == "default"
        assert {j["worker_id"] for j in await _jobs_for(res["batch_ids"])} == {"default"}


async def test_an_unknown_or_sending_disabled_worker_is_rejected_with_no_jobs_and_no_fallback():
    w_off = await _worker("QA disabled", sending=False)
    pid, _ = await _project()
    tid, _ = await _template()
    t = await _talent(pid)
    sp = SourceParams(project_id=pid, pipeline_stages=list(PIPELINE_STAGE_ORDER), talent_ids=[t])
    with pytest.raises(HTTPException) as unknown:
        await wca._create_batch_for_target(_target(tid, sp), {}, is_dry_run=False, worker_id="zzz-test-ow-nope")
    with pytest.raises(HTTPException) as disabled:
        await wca._create_batch_for_target(_target(tid, sp), {}, is_dry_run=False, worker_id=w_off)
    assert unknown.value.status_code == 404 and disabled.value.status_code == 403
    assert await db.whatsapp_jobs.count_documents({"talent_id": t}) == 0     # not even on the default worker


# ── propagation: casting_pipeline sites (group send, share, delivery reports) ──

async def test_the_compound_plans_group_send_uses_the_originating_worker():
    w2 = await _worker("QA Worker 2")
    pid, label = await _project()
    _tid, tname = await _template()
    t = await _talent(pid)
    lines: list = []
    await cp._flush_group_send(
        lines, tname, {pid: cp._GroupSendBucket(project_label=label, talent_ids=[t], talent_labels=["QA"])},
        is_dry_run=False, worker_id=w2,
    )
    jobs = await db.whatsapp_jobs.find({"talent_id": t}, {"_id": 0}).to_list(10)
    assert len(jobs) == 1 and jobs[0]["worker_id"] == w2
    _batch_ids.append(jobs[0]["batch_id"])


async def test_share_sends_use_the_originating_worker_and_default_without_one():
    w2 = await _worker("QA Worker 2")
    pid, label = await _project()
    tid, tname = await _template()
    t_a, t_b = await _talent(pid), await _talent(pid)
    for worker, talent, expected in ((w2, t_a, w2), (None, t_b, "default")):
        resolved = cp._ShareResolution(
            ok=True, template={"id": tid, "name": tname}, template_label=tname,
            project_ids=[pid], project_labels=[label], talent_ids=[talent], talent_labels=["QA"],
        )
        _lines, queued, batch_ids = await cp._run_share_sends(resolved, worker)
        _batch_ids.extend(batch_ids)
        jobs = await _jobs_for(batch_ids)
        assert queued == 1 and len(jobs) == 1 and jobs[0]["worker_id"] == expected


async def test_share_delivery_reports_go_back_out_through_the_originating_worker():
    w2 = await _worker("QA Worker 2")
    # the finished share's jobs (already terminal, so the watcher reports at once)
    done_batch = f"zzz-test-ow-done-{uuid.uuid4().hex[:6]}"
    await db.whatsapp_jobs.insert_one({
        "id": f"zzz-test-ow-job-{uuid.uuid4().hex[:6]}", "batch_id": done_batch, "status": "sent",
        "talent_name": "QA", "worker_id": w2, "created_at": _now(),
    })
    _batch_ids.append(done_batch)
    await cp._watch_and_report_share_delivery(
        batch_ids=[done_batch], group_name="ZZZ QA Scouting (Divyani)", project_label="P",
        content_label="C", worker_id=w2,
    )
    await cp._watch_and_report_instagram_share_delivery(
        batch_id=done_batch, group_name="ZZZ QA Scouting (Divyani)", recipient_label="R",
        talent_labels=["QA"], worker_id=w2,
    )
    reports = await db.whatsapp_batches.find({"worker_id": w2, "source_type": "MANUAL"}, {"_id": 0}).to_list(10)
    assert len(reports) == 2                                   # one per watcher, exactly
    jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": [r["id"] for r in reports]}}, {"_id": 0}).to_list(10)
    assert len(jobs) == 2 and {j["worker_id"] for j in jobs} == {w2}


async def test_the_instagram_share_executor_and_its_report_watcher_use_the_originating_worker(monkeypatch):
    w2 = await _worker("QA Worker 2")
    resolved = cp._InstagramShareResolution(
        ok=True, talent_ids=["zzz"], talent_labels=["QA"], instagram_urls=["https://instagram.com/qa"],
        recipient_label="ZZZ QA Group", recipient_destination_type="group",
        recipient_destination="ZZZ QA Group", recipient_source_type="MANUAL",
        recipient_source_params=SourceParams(contacts=[
            ManualContact(name="QA", phone="", whatsapp_group_name="ZZZ QA Group"),
        ]),
    )
    monkeypatch.setattr(cp, "_resolve_share_instagram", AsyncMock(return_value=resolved))
    monkeypatch.setattr(cp, "_format_instagram_share_body", lambda _r: "QA instagram body")
    watcher = AsyncMock()
    monkeypatch.setattr(cp, "_watch_and_report_instagram_share_delivery", watcher)

    ctx = ExecContext(agent_id=AGENT_ID, group_name="ZZZ Scouting (Divyani)", sender_phone="919999999999", worker_id=w2)
    out = await cp._share_instagram_executor({}, ctx)
    assert out.ok, out.message
    batch = await db.whatsapp_batches.find_one({"worker_id": w2}, {"_id": 0})
    assert batch is not None and batch["worker_id"] == w2
    assert watcher.call_args.kwargs["worker_id"] == w2          # the report back to the group stays on w2


# ── SEND / marked-media preview / dispatch: the same worker, end to end ─────
#
# The SEND command's scan requests and its approved dispatch document are claimed by the worker
# named in their `worker_id`; before this change the preview scans and the fast-path dispatch
# were created without one (default worker), so a command issued through the second worker's
# group queued its media work for the other, offline worker.

class _Recorder:
    """Wraps media_send.create_send_scan_request: calls the REAL function (real DB document) and
    records the worker_id the document was actually written with."""
    def __init__(self, monkeypatch):
        from agents.modules import media_send
        self.worker_ids: list = []
        real = media_send.create_send_scan_request

        async def wrapper(**kwargs):
            req_id = await real(**kwargs)
            doc = await db.whatsapp_scan_requests.find_one({"id": req_id}, {"_id": 0, "worker_id": 1})
            self.worker_ids.append(doc["worker_id"])
            return req_id
        monkeypatch.setattr(media_send, "create_send_scan_request", wrapper)


@pytest.mark.parametrize("worker, expected", [(None, "default"), ("param", "param")])
async def test_the_marked_media_preview_scans_are_queued_for_the_originating_worker(monkeypatch, worker, expected):
    monkeypatch.setattr(cp, "_SEND_PREVIEW_MAX_WAIT_SEC", 0.15)
    monkeypatch.setattr(cp, "_SEND_PREVIEW_TOTAL_MAX_WAIT_SEC", 0.4)
    rec = _Recorder(monkeypatch)
    w = await _worker("QA Worker 2") if worker == "param" else None
    kwargs = {"worker_id": w} if w else {}
    outcome, err = await cp._preview_send_marks(
        talent_id="zzz-tal", talent_label="QA", project_id="zzz-proj", project_label="QA Project",
        destination_group="ZZZ Dest", sources=[("group", "ZZZ Source A"), ("phone", "ZZZ Source B")], **kwargs,
    )
    assert (outcome, err) == (None, None)                       # no worker is running: pure timeout, as in production
    assert len(rec.worker_ids) == 2                             # one preview scan per configured source
    assert set(rec.worker_ids) == {w if w else expected}


async def test_the_bulk_send_preview_passes_the_originating_worker_down(monkeypatch):
    seen = []

    async def fake_preview(**kw):
        seen.append(kw["worker_id"])
        return None, None
    monkeypatch.setattr(cp, "_preview_send_marks", fake_preview)
    target = {
        "authoritative_talent_id": "t", "authoritative_talent_label": "T", "project": {"id": "p", "label": "P"},
        "destination_group": "D", "all_sources": [("group", "G")],
    }
    await cp._preview_one_bulk_target(target, "worker-x")
    await cp._preview_one_bulk_target(target)
    assert seen == ["worker-x", "default"]                      # unchanged for callers that pass nothing


def _send_one_pair_stubs(monkeypatch, *, stale: bool):
    """Drive the REAL _send_one_pair with only its I/O collaborators stubbed; capture the worker
    that the preview refresh and the approved dispatch are given."""
    from agents.modules import media_send
    captured: dict = {"preview": [], "dispatch": []}
    target = {
        "authoritative_talent_id": "t", "authoritative_talent_label": "T", "project": {"id": "p", "label": "P"},
        "destination_group": "D", "submission": {"id": "s"}, "project_doc": {}, "all_sources": [("group", "G")],
    }
    monkeypatch.setattr(cp, "_resolve_send_target", AsyncMock(return_value=(target, None)))
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - (timedelta(days=1) if stale else timedelta(seconds=1))
    monkeypatch.setattr(media_send, "get_send_approval", AsyncMock(return_value={
        "preview_assignments": [{"mark_intent_id": "m"}], "preview_computed_at": old,
    }))
    monkeypatch.setattr(media_send, "build_form_send_message", lambda *a, **k: {"message": "m", "content_hash": "h"})
    for name in ("save_send_approval_draft", "approve_send_form", "record_form_send", "save_send_preview_cache"):
        monkeypatch.setattr(media_send, name, AsyncMock())
    monkeypatch.setattr(media_send, "already_sent_form", AsyncMock(return_value=True))

    async def fake_preview(**kw):
        captured["preview"].append(kw["worker_id"])
        return [{"mark_intent_id": "m"}], None

    async def fake_dispatch(**kw):
        captured["dispatch"].append(kw["worker_id"])
        return "req"
    monkeypatch.setattr(cp, "_preview_send_marks", fake_preview)
    monkeypatch.setattr(media_send, "create_send_dispatch_from_approved_plan", fake_dispatch)
    return captured


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("worker", ["worker-2-like", "default"])
async def test_an_approved_send_dispatches_and_refreshes_its_preview_on_the_originating_worker(monkeypatch, worker, stale):
    cap = _send_one_pair_stubs(monkeypatch, stale=stale)
    ctx = ExecContext(agent_id=AGENT_ID, group_name="G", sender_phone="919999999999", worker_id=worker)
    out = await cp._send_one_pair({}, ctx)
    assert out.ok, out.message
    assert cap["dispatch"] == [worker]                           # the approved SEND document
    assert cap["preview"] == ([worker] if stale else [])         # the stale-preview refresh scan


async def test_the_submission_action_queues_verification_scan_uses_the_actions_own_worker(monkeypatch):
    from agents.modules import submission_action_queue as saq
    seen = []

    async def fake_scan(**kw):
        seen.append(kw["worker_id"])
        return None, "scan error (test)"
    monkeypatch.setattr(cp, "_scan_and_validate_multi_source", fake_scan)

    def action(**extra):
        return {"id": f"zzz-test-ow-act-{uuid.uuid4().hex[:6]}", "state": "verifying", "source_group_name": "G",
                "talent_id": "t", "talent_label": "T", "project_id": "p", "project_label": "P",
                "destination_group": "D", "attempt_count": 0, **extra}
    await saq.advance_send_action(action(worker_id="worker-2-like"))
    await saq.advance_send_action(action())                      # legacy action without a worker
    assert seen == ["worker-2-like", "default"]


async def test_share_across_two_projects_makes_one_batch_per_project_all_on_the_originating_worker():
    w2 = await _worker("QA Worker 2")
    p1, l1 = await _project()
    p2, l2 = await _project()
    tid, tname = await _template()
    tals = []
    for _ in range(3):
        t = await _talent(p1)
        await _in_pipeline(p2, t)
        tals.append(t)
    resolved = cp._ShareResolution(
        ok=True, template={"id": tid, "name": tname}, template_label=tname,
        project_ids=[p1, p2], project_labels=[l1, l2], talent_ids=tals, talent_labels=["A", "B", "C"],
    )
    _lines, queued, batch_ids = await cp._run_share_sends(resolved, w2)
    _batch_ids.extend(batch_ids)
    jobs = await _jobs_for(batch_ids)
    assert len(batch_ids) == 2 and queued == 6 and len(jobs) == 6
    assert {j["worker_id"] for j in jobs} == {w2}
    assert len({(j["talent_id"], j["source_id"]) for j in jobs}) == 6          # no duplicates
    for b in await db.whatsapp_batches.find({"id": {"$in": batch_ids}}, {"_id": 0}).to_list(5):
        assert b["worker_id"] == w2
