"""Casting Pipeline → selected talents → "Send Template" (bulk WhatsApp inside ONE project).

The pipeline reuses the existing POST /api/whatsapp/casting-call/send (same templates, same worker validation, same
recipient resolution, same job/batch shape) with the CURRENT project as the only project. This file pins what that
integration relies on: only the selected talents are queued — from any pipeline stage; the template renders from
the current project; the pipeline stage is never moved as a side effect (apply_pipeline_move=false) while the Global
Talent default is unchanged; recipients without a usable WhatsApp destination are reported by name, not silently
dropped; an unavailable worker rejects everything; auth is enforced.

Real FastAPI app + real local dev DB; NOTHING is sent to WhatsApp — only `whatsapp_jobs` rows are queued for a worker
that is offline in tests. Throwaway ZZZ_TEST rows are removed afterwards."""
import os

os.environ["JWT_SECRET"] = "dummy"
_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["MONGO_URL"] = _MONGO_URL

import uuid

import httpx
import pytest
import pytest_asyncio

from core import _now, db, make_token, hash_password
from server import app
from test_casting_call_send import (  # noqa: F401  (reuse the module-scoped admin client + helpers)
    _cleanup, _make_custom_template, _make_project, client,
)
from test_casting_call_worker_select import (  # noqa: F401  (throwaway worker helper + real-db isolation fixture)
    _real_db_for_this_module, _worker,
)
import routers.whatsapp as W

_aio = pytest.mark.asyncio(loop_scope="module")
URL = "/api/whatsapp/casting-call/send"
STAGES = ["ask_to_test", "follow_up", "approved", "shortlisted", "locked", "hold", "rejected", "not_interested", "already_tested"]


async def _talent(pid, stage="ask_to_test", phone="911234500000", name=None, **extra):
    tid = f"zzz-test-pw-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name or f"ZZZ_TEST_PW Talent {tid[-4:]}", "email": f"{tid}@example.com",
                                 "phone": phone, "tags": [], "media": [], **extra})
    await db.casting_pipeline.insert_one({"id": f"zzz-test-pw-row-{uuid.uuid4().hex[:8]}", "project_id": pid, "talent_id": tid,
                                          "stage": stage, "created_at": _now(), "updated_at": _now()})
    return tid


async def _jobs(tids):
    return await db.whatsapp_jobs.find({"talent_id": {"$in": tids}}, {"_id": 0}).to_list(500)


async def _stage(pid, tid):
    return (await db.casting_pipeline.find_one({"project_id": pid, "talent_id": tid}))["stage"]


@pytest_asyncio.fixture
async def spy_move(monkeypatch):
    """Replace the background pipeline-move watcher with a recorder (it would otherwise poll for up to 45 minutes)."""
    calls = []

    async def fake(batch_ids):
        calls.append(list(batch_ids))

    monkeypatch.setattr(W, "_watch_and_apply_casting_call_move", fake)
    return calls


async def _yield_to_tasks():
    import asyncio
    await asyncio.sleep(0.05)


@_aio
async def test_only_the_selected_talents_are_queued_from_any_stage_and_nobody_else_in_the_project(client, spy_move):
    w = await _worker("PW A")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi {{first_name}} — {{project_name}}")
    everyone = [await _talent(pid, st) for st in STAGES]
    chosen = [everyone[0], everyone[2], everyone[4], everyone[6], everyone[8]]       # ask_to_test, approved, locked, rejected, already_tested
    try:
        r = await client.post(URL, json={"talent_ids": chosen, "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["errors"] == [] and body["batches"][0]["queued"] == 5 and body["batches"][0]["skipped"] == 0
        jobs = await _jobs(everyone)
        assert sorted(j["talent_id"] for j in jobs) == sorted(chosen)             # exactly the selection
        assert {j["worker_id"] for j in jobs} == {w}
        assert len({j["batch_id"] for j in jobs}) == 1                            # one batch for the one (current) project
        assert all(j["status"] == "pending" for j in jobs)                        # queued, never sent by the test
    finally:
        await _cleanup(pid, everyone, tmpl)


@_aio
async def test_the_message_is_rendered_from_the_current_project_only(client, spy_move):
    w = await _worker("PW B")
    pid, name = await _make_project(shoot_dates="28th Oct", budget_per_day="50k", slug=f"amazon-{uuid.uuid4().hex[:6]}")
    other, other_name = await _make_project()
    tmpl, _t = await _make_custom_template("{{project_name}} | {{shoot_dates}} | {{budget}} | {{submission_link}}")
    tid = await _talent(pid)
    await db.casting_pipeline.insert_one({"id": f"zzz-test-pw-row-{uuid.uuid4().hex[:8]}", "project_id": other, "talent_id": tid, "stage": "ask_to_test", "created_at": _now(), "updated_at": _now()})
    try:
        r = await client.post(URL, json={"talent_ids": [tid], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
        assert r.status_code == 201, r.text
        jobs = await _jobs([tid])
        assert len(jobs) == 1                                                       # NOT one per project the talent is in
        msg = jobs[0]["message_body"]
        assert name in msg and "28th Oct" in msg and "50k" in msg and "/submit/amazon-" in msg
        assert other_name not in msg
        assert (await db.whatsapp_batches.find_one({"id": jobs[0]["batch_id"]}))["project_id"] == pid
    finally:
        await _cleanup(pid, [tid], tmpl)
        await _cleanup(other)


@_aio
async def test_pipeline_send_never_moves_a_stage_while_the_global_talent_default_still_does(client, spy_move):
    w = await _worker("PW C")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi {{first_name}}")
    a, b = await _talent(pid), await _talent(pid)
    try:
        r = await client.post(URL, json={"talent_ids": [a], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
        assert r.status_code == 201
        await _yield_to_tasks()
        assert spy_move == []                                                       # no stage-move watcher started
        assert await _stage(pid, a) == "ask_to_test"
        r2 = await client.post(URL, json={"talent_ids": [b], "project_ids": [pid], "template_id": tmpl, "worker_id": w})   # Global Talent shape
        assert r2.status_code == 201
        await _yield_to_tasks()
        assert len(spy_move) == 1 and len(spy_move[0]) == 1                         # unchanged default: watcher is started
    finally:
        await _cleanup(pid, [a, b], tmpl)


@_aio
async def test_recipients_with_no_usable_whatsapp_destination_are_reported_by_name_not_dropped_silently(client, spy_move):
    w = await _worker("PW D")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi {{first_name}}")
    good = await _talent(pid)
    no_phone = await _talent(pid, phone="", name="ZZZ_TEST_PW NoPhone")
    group_only = await _talent(pid, phone="", name="ZZZ_TEST_PW GroupOnly", whatsapp_group_name="ZZZ Group")   # group routing is enough
    ghost = f"zzz-test-pw-ghost-{uuid.uuid4().hex[:6]}"                              # selected id that is not in this project at all
    try:
        r = await client.post(URL, json={"talent_ids": [good, no_phone, group_only, ghost], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
        assert r.status_code == 201, r.text
        res = r.json()["batches"][0]
        assert res["queued"] == 2 and res["skipped"] == 1              # existing rule: a phone OR a WhatsApp group is enough
        assert [x["name"] for x in res["skipped_recipients"]] == ["ZZZ_TEST_PW NoPhone"]
        assert all(x["reason"] for x in res["skipped_recipients"])
        assert res["not_in_project"] == [ghost]
        assert sorted(j["talent_id"] for j in await _jobs([good, no_phone, group_only])) == sorted([good, group_only])
    finally:
        await _cleanup(pid, [good, no_phone, group_only], tmpl)


@_aio
async def test_when_nobody_can_be_queued_nothing_is_created_and_it_says_so(client, spy_move):
    w = await _worker("PW E")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi {{first_name}}")
    no_phone = await _talent(pid, phone="")
    try:
        r = await client.post(URL, json={"talent_ids": [no_phone], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
        assert r.status_code == 400
        assert await _jobs([no_phone]) == []
    finally:
        await _cleanup(pid, [no_phone], tmpl)


@_aio
async def test_an_unavailable_worker_rejects_the_whole_send_and_creates_nothing(client, spy_move):
    off = await _worker("PW Off", status="disconnected")
    nosend = await _worker("PW NoSend", sending=False)
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi")
    t = await _talent(pid)
    try:
        for w in (off, nosend):
            r = await client.post(URL, json={"talent_ids": [t], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})
            assert r.status_code in (403, 409), (w, r.text)
        assert await _jobs([t]) == []
        assert (await client.post(URL, json={"talent_ids": [t], "project_ids": [pid], "template_id": tmpl, "worker_id": "  ", "apply_pipeline_move": False})).status_code == 400
    finally:
        await _cleanup(pid, [t], tmpl)


@_aio
async def test_selection_template_and_project_are_required_and_validated(client, spy_move):
    w = await _worker("PW F")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi")
    t = await _talent(pid)
    try:
        base = {"worker_id": w, "apply_pipeline_move": False}
        assert (await client.post(URL, json={**base, "talent_ids": [], "project_ids": [pid], "template_id": tmpl})).status_code == 400
        assert (await client.post(URL, json={**base, "talent_ids": [t], "project_ids": [], "template_id": tmpl})).status_code == 400
        assert (await client.post(URL, json={**base, "talent_ids": [t], "project_ids": [pid], "template_id": ""})).status_code == 400
        assert (await client.post(URL, json={**base, "talent_ids": [t], "project_ids": [pid], "template_id": "nope"})).status_code == 404
        assert (await client.post(URL, json={**base, "talent_ids": [t], "project_ids": ["no-such-project"], "template_id": tmpl})).status_code == 400
        assert await _jobs([t]) == []
    finally:
        await _cleanup(pid, [t], tmpl)


@_aio
async def test_auth_is_enforced_and_team_members_can_send_like_everywhere_else(client, spy_move):
    w = await _worker("PW G")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi")
    t = await _talent(pid)
    uid = f"zzz-test-pw-team-{uuid.uuid4().hex[:6]}"
    await db.users.insert_one({"id": uid, "email": f"{uid}@example.com", "name": "ZZZ_TEST_PW team", "password_hash": hash_password("Password@123"),
                               "role": "team", "status": "active", "token_version": 0})
    payload = {"talent_ids": [t], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anon:
            assert (await anon.post(URL, json=payload)).status_code in (401, 403)
            assert await _jobs([t]) == []
            team = {"Authorization": f"Bearer {make_token({'id': uid, 'email': f'{uid}@example.com', 'role': 'team', 'tv': 0})}"}
            assert (await anon.post(URL, json=payload, headers=team)).status_code == 201
        assert len(await _jobs([t])) == 1
    finally:
        await db.users.delete_one({"id": uid})
        await _cleanup(pid, [t], tmpl)


@_aio
async def test_sending_changes_no_talent_pipeline_or_project_data(client, spy_move):
    w = await _worker("PW H")
    pid, _ = await _make_project()
    tmpl, _t = await _make_custom_template("Hi {{first_name}}")
    t = await _talent(pid, "shortlisted")
    try:
        before = (await db.talents.find_one({"id": t}, {"_id": 0}), await db.casting_pipeline.find_one({"project_id": pid, "talent_id": t}, {"_id": 0}),
                  await db.projects.find_one({"id": pid}, {"_id": 0}))
        assert (await client.post(URL, json={"talent_ids": [t], "project_ids": [pid], "template_id": tmpl, "worker_id": w, "apply_pipeline_move": False})).status_code == 201
        await _yield_to_tasks()
        after = (await db.talents.find_one({"id": t}, {"_id": 0}), await db.casting_pipeline.find_one({"project_id": pid, "talent_id": t}, {"_id": 0}),
                 await db.projects.find_one({"id": pid}, {"_id": 0}))
        assert before == after
    finally:
        await _cleanup(pid, [t], tmpl)
