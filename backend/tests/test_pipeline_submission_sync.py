"""Pipeline ↔ Submission Review status synchronisation (2026-10-21).

Bug: a talent moved to Approved / Hold / Rejected on the PIPELINE (kanban, bulk-move, WhatsApp agent, Personal Assistant) kept showing "pending" in the
Submission Review Center, because only `casting_pipeline.stage` was written — never `submissions.decision`, the field the Review Center renders.

Real FastAPI app + real local dev Mongo, throwaway zzz rows. Nothing is sent to WhatsApp; no notification is created by the sync (asserted)."""
import os

os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from core import _now, db
from routers import casting_pipeline as CP
from test_personal_assistant import (  # noqa: F401  (fixtures + helpers of the existing suite)
    BASE, _isolate_each_test, _project, _talent, alice, anon, bob, team, cleanup, confirm, say, world, _real_db_for_this_module,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")
_made = {"subs": [], "projects": [], "talents": []}


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _tidy():
    yield
    ids = _made["subs"]
    await db.notifications.delete_many({"payload.submission_id": {"$in": ids}})
    await db.submissions.delete_many({"id": {"$in": ids}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": _made["projects"]}})
    await db.projects.delete_many({"id": {"$in": _made["projects"]}})
    await db.talents.delete_many({"id": {"$in": _made["talents"]}})


async def mk_project(name="ZZZ Sync Project"):
    pid = f"zzz-sync-p-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({"id": pid, "slug": pid, "brand_name": name, "status": "ongoing", "created_at": _now(), "materials": []})
    _made["projects"].append(pid)
    return pid


async def mk_talent(name="ZZZ Sync Talent"):
    tid = f"zzz-sync-t-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "email": f"{tid}@example.com", "phone": "919900000000", "status": "ACTIVE", "tags": [], "media": []})
    _made["talents"].append(tid)
    return tid


async def mk_row(pid, tid, stage="ask_to_test"):
    rid = uuid.uuid4().hex
    await db.casting_pipeline.insert_one({"id": rid, "project_id": pid, "talent_id": tid, "stage": stage, "created_at": _now(), "updated_at": _now()})
    return rid


async def mk_sub(pid, tid, *, linked=True, status="draft", decision="pending", media=None, **over):
    sid = f"zzz-sync-s-{uuid.uuid4().hex[:8]}"
    doc = {"id": sid, "project_id": pid, "project_slug": pid, "talent_name": "ZZZ Sync Talent", "talent_email": f"{tid}@example.com", "status": status, "decision": decision,
           "media": media or [], "form_data": {"first_name": "Zed"}, "created_at": _now(), "created_from": "talent_link", **over}
    if linked:
        doc["talent_id"] = tid
    await db.submissions.insert_one(doc)
    _made["subs"].append(sid)
    return sid


def hdr(who):
    return who["headers"]


async def listed(anon, who, pid):
    """What the Review Center fetches on load / refresh."""
    r = await anon.get(f"/api/projects/{pid}/submissions", headers=hdr(who))
    assert r.status_code == 200, r.text
    return {s["id"]: s for s in r.json()}


async def detail(anon, who, pid, sid):
    r = await anon.get(f"/api/projects/{pid}/submissions/{sid}", headers=hdr(who))
    assert r.status_code == 200, r.text
    return r.json()


async def move_ui(anon, who, pid, row_id, stage):
    return await anon.patch(f"/api/projects/{pid}/pipeline/move", headers=hdr(who), json={"ids": [row_id], "stage": stage})


async def bulk_ui(anon, who, pid, tids, stage):
    return await anon.post(f"/api/projects/{pid}/pipeline/bulk-move", headers=hdr(who), json={"talent_ids": tids, "stage": stage})


async def side_effects():
    return {"notifications": await db.notifications.count_documents({}), "jobs": await db.whatsapp_jobs.count_documents({}), "batches": await db.whatsapp_batches.count_documents({}),
            "audit": await db.assistant_audit.count_documents({})}


# ================================================================== the reported bug, per stage and per path
@pytest.mark.parametrize("stage,decision", [("approved", "approved"), ("hold", "hold"), ("rejected", "rejected")])
async def test_kanban_move_updates_what_the_review_center_shows_list_detail_filter_and_counts(anon, alice, stage, decision):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)                                                   # "pending" + a draft with no media: the screenshot's case
    assert (await move_ui(anon, alice, pid, row, stage)).json()["moved"] == 1
    for _ in range(2):                                                             # a fresh fetch, twice (a reload)
        assert (await listed(anon, alice, pid))[sid]["decision"] == decision
        assert (await detail(anon, alice, pid, sid))["decision"] == decision
    filtered = (await anon.get(f"/api/projects/{pid}/submissions", headers=hdr(alice), params={"decision": decision})).json()
    assert [s["id"] for s in filtered] == [sid]
    assert [s["id"] for s in (await anon.get(f"/api/projects/{pid}/submissions", headers=hdr(alice), params={"decision": "pending"})).json()] == []
    stats = (await anon.get(f"/api/projects/{pid}/submissions/stats", headers=hdr(alice))).json()
    assert stats[decision] == 1 and stats["pending"] == 0
    hist = (await db.submissions.find_one({"id": sid}))["status_history"]
    assert [(h["from_status"], h["to_status"], h["source"]) for h in hist] == [("pending", decision, "pipeline_sync")]          # audited, with its origin


@pytest.mark.parametrize("stage,decision", [("approved", "approved"), ("hold", "hold"), ("rejected", "rejected")])
async def test_bulk_move_endpoint_syncs_the_same_way(anon, alice, stage, decision):
    pid = await mk_project()
    tids = [await mk_talent(), await mk_talent()]
    for t in tids:
        await mk_row(pid, t)
    sids = [await mk_sub(pid, t) for t in tids]
    assert (await bulk_ui(anon, alice, pid, tids, stage)).json()["moved"] == 2
    got = await listed(anon, alice, pid)
    assert [got[s]["decision"] for s in sids] == [decision, decision]


async def test_moving_between_decision_stages_follows_each_move(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    for stage in ("approved", "hold", "rejected", "approved"):
        await move_ui(anon, alice, pid, row, stage)
        assert (await listed(anon, alice, pid))[sid]["decision"] == stage


# ================================================================== scoping
async def test_one_project_never_changes_the_same_talents_submission_in_another_project(anon, alice):
    pa, pb, tid = await mk_project("A"), await mk_project("B"), await mk_talent()
    ra, rb = await mk_row(pa, tid), await mk_row(pb, tid)
    sa, sb = await mk_sub(pa, tid), await mk_sub(pb, tid)
    await move_ui(anon, alice, pa, ra, "approved")
    assert (await listed(anon, alice, pa))[sa]["decision"] == "approved"
    assert (await listed(anon, alice, pb))[sb]["decision"] == "pending"
    assert (await db.casting_pipeline.find_one({"id": rb}))["stage"] == "ask_to_test"
    await bulk_ui(anon, alice, pb, [tid], "rejected")
    assert (await listed(anon, alice, pa))[sa]["decision"] == "approved" and (await listed(anon, alice, pb))[sb]["decision"] == "rejected"


async def test_a_row_id_from_another_project_matches_nothing_and_syncs_nothing(anon, alice):
    pa, pb, tid = await mk_project(), await mk_project(), await mk_talent()
    rb = await mk_row(pb, tid)
    sb = await mk_sub(pb, tid)
    r = await move_ui(anon, alice, pa, rb, "approved")                              # wrong project in the URL
    assert r.json()["moved"] == 0 and (await listed(anon, alice, pb))[sb]["decision"] == "pending"


# ================================================================== completeness is untouched
async def test_a_decision_never_makes_an_incomplete_submission_complete(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid, status="draft", media=[], form_data={"first_name": "Zed", "custom_answers": {}})
    before = await db.submissions.find_one({"id": sid}, {"_id": 0})
    await move_ui(anon, alice, pid, row, "approved")
    after = await db.submissions.find_one({"id": sid}, {"_id": 0})
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"decision", "decided_at", "status_history"}                 # media, form_data, status, submitted_at, … all identical
    assert after["media"] == [] and after["status"] == "draft" and not after.get("submitted_at")


async def test_a_complete_submission_keeps_its_media_too(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    media = [{"id": "m1", "category": "intro_video", "url": "https://x/v.mp4"}, {"id": "m2", "category": "image", "url": "https://x/i.jpg"}]
    sid = await mk_sub(pid, tid, status="submitted", media=media, submitted_at=_now())
    await move_ui(anon, alice, pid, row, "hold")
    d = await detail(anon, alice, pid, sid)
    assert d["decision"] == "hold" and [m["id"] for m in d["media"]] == ["m1", "m2"] and d["status"] == "submitted"


# ================================================================== matching rules
async def test_a_submission_linked_only_by_email_is_found_and_a_deleted_one_is_not(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    s_email = await mk_sub(pid, tid, linked=False)                                  # a talent-created draft whose talent_id was never resolved
    s_gone = await mk_sub(pid, tid, linked=True, lifecycle_state="deleted", talent_email=f"gone-{uuid.uuid4().hex[:6]}@example.com")    # unique (project, email): one live submission per talent
    await move_ui(anon, alice, pid, row, "approved")
    assert (await db.submissions.find_one({"id": s_email}))["decision"] == "approved"
    assert (await db.submissions.find_one({"id": s_gone}))["decision"] == "pending"


@pytest.mark.parametrize("stage", ["follow_up", "ask_to_test", "shortlisted", "already_tested", "locked", "not_available", "pitch", "not_interested"])
async def test_stages_without_an_unambiguous_review_equivalent_leave_the_decision_alone(anon, alice, stage):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid, "approved")
    sid = await mk_sub(pid, tid, decision="approved")
    pend = await mk_sub(pid, await mk_talent())
    await move_ui(anon, alice, pid, row, stage)
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "approved"           # approved then follow-up / locked …: still approved in review
    assert (await db.submissions.find_one({"id": pend}))["decision"] == "pending"


async def test_a_talent_with_no_submission_in_this_project_is_simply_moved(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    assert (await move_ui(anon, alice, pid, row, "approved")).json()["moved"] == 1
    assert (await db.casting_pipeline.find_one({"id": row}))["stage"] == "approved"


async def test_the_legacy_sent_stage_folds_into_approved(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    await move_ui(anon, alice, pid, row, "sent")
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "approved"


# ================================================================== idempotency and side effects
async def test_repeating_the_same_move_changes_nothing_further_and_creates_no_notification_job_or_audit(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    before = await side_effects()
    await move_ui(anon, alice, pid, row, "approved")
    first = await db.submissions.find_one({"id": sid})
    for _ in range(3):
        await move_ui(anon, alice, pid, row, "approved")
        await bulk_ui(anon, alice, pid, [tid], "approved")
    again = await db.submissions.find_one({"id": sid})
    assert again["decided_at"] == first["decided_at"] and len(again["status_history"]) == 1
    assert await side_effects() == before                                              # no notification, WhatsApp job/batch or assistant audit row


async def test_a_concurrent_pair_of_identical_syncs_records_one_transition(anon, alice):
    import asyncio
    pid, tid = await mk_project(), await mk_talent()
    await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    await asyncio.gather(*[CP.sync_submission_from_pipeline(pid, [tid], "hold") for _ in range(6)])
    assert len((await db.submissions.find_one({"id": sid}))["status_history"]) == 1


async def test_a_repeat_heals_a_row_that_was_already_at_the_stage_but_still_pending(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid, "approved")                                           # exactly the 64 production rows that disagree today
    sid = await mk_sub(pid, tid)
    await move_ui(anon, alice, pid, row, "approved")
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "approved"


# ================================================================== the existing Review Center path is unchanged
async def test_the_review_center_decision_path_still_syncs_the_pipeline_and_still_notifies_once(anon, alice):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    n0 = await db.notifications.count_documents({"payload.submission_id": sid})
    r = await anon.post(f"/api/projects/{pid}/submissions/{sid}/decision", headers=hdr(alice), json={"decision": "approved", "note": "ok"})
    assert r.status_code == 200
    assert (await db.casting_pipeline.find_one({"id": row}))["stage"] == "approved"            # submission → pipeline, as before
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "approved"
    assert await db.notifications.count_documents({"payload.submission_id": sid}) > n0          # the decision's own notification (unchanged)
    n1 = await db.notifications.count_documents({"payload.submission_id": sid})
    await move_ui(anon, alice, pid, row, "approved")                                           # no loop, no second notification, no second history entry
    assert await db.notifications.count_documents({"payload.submission_id": sid}) == n1
    assert [h["to_status"] for h in (await db.submissions.find_one({"id": sid}))["status_history"]] == ["approved"]
    await move_ui(anon, alice, pid, row, "hold")
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "hold"


# ================================================================== the Personal Assistant path
async def test_personal_assistant_move_to_approved_hold_and_rejected_updates_the_review_status(anon, alice, world):
    pid = await _project("Qzx Sync Film")
    tid = await _talent("Qzx Syncella Mover")
    _made["projects"].append(pid); _made["talents"].append(tid)
    await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    other_pid = await _project("Qzx Other Sync Film")
    _made["projects"].append(other_pid)
    await mk_row(other_pid, tid)
    other = await mk_sub(other_pid, tid)
    prev = "pending"
    for label, decision in (("Approved", "approved"), ("Hold", "hold"), ("Rejected", "rejected")):
        t = (await say(anon, alice, f"Move Syncella Mover to {label} in Qzx Sync Film"))["tasks"][0]
        assert t["status"] == "awaiting_confirmation"
        assert (await db.submissions.find_one({"id": sid}))["decision"] == prev                 # planning alone changes nothing: only the approved task does
        prev = decision
        done = (await confirm(anon, alice, t["id"]))["task"]
        assert done["status"] == "completed"
        assert (await listed(anon, alice, pid))[sid]["decision"] == decision
        assert (await listed(anon, alice, other_pid))[other]["decision"] == "pending"          # the same talent in another project is untouched


async def test_personal_assistant_planning_or_cancelling_changes_no_review_status(anon, alice, world):
    pid = await _project("Qzx Sync Plan Film")
    tid = await _talent("Qzx Planella Holder")
    _made["projects"].append(pid); _made["talents"].append(tid)
    await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    t = (await say(anon, alice, "Move Planella Holder to Approved in Qzx Sync Plan Film"))["tasks"][0]
    await anon.post(f"{BASE}/tasks/{t['id']}/cancel", headers=hdr(alice))
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "pending"


# ================================================================== the WhatsApp agent path (shared primitive + undo)
async def test_the_shared_move_primitive_used_by_the_whatsapp_agent_syncs_and_its_undo_reverts_only_its_own_sync():
    pid, t1, t2 = await mk_project(), await mk_talent(), await mk_talent()
    for t in (t1, t2):
        await mk_row(pid, t)
    s1, s2 = await mk_sub(pid, t1), await mk_sub(pid, t2, decision="approved")        # t2 was approved by a HUMAN in the review center
    await CP.bulk_move_by_talent_ids(pid, [t1, t2], "approved")
    assert (await db.submissions.find_one({"id": s1}))["decision"] == "approved"
    await CP.bulk_move_by_talent_ids(pid, [t1, t2], "ask_to_test", undo=True)               # the agent's "undo": back to where they were
    assert (await db.submissions.find_one({"id": s1}))["decision"] == "pending"            # the sync's own approval is undone …
    assert (await db.submissions.find_one({"id": s2}))["decision"] == "approved"           # … a human decision is not
    hist = [(h["to_status"], h["source"]) for h in (await db.submissions.find_one({"id": s1}))["status_history"]]
    assert hist == [("approved", "pipeline_sync"), ("pending", "pipeline_sync_undo")]


async def test_an_ordinary_move_out_of_approved_is_not_treated_as_an_undo():
    pid, t1 = await mk_project(), await mk_talent()
    await mk_row(pid, t1)
    s1 = await mk_sub(pid, t1)
    await CP.bulk_move_by_talent_ids(pid, [t1], "approved")
    await CP.bulk_move_by_talent_ids(pid, [t1], "follow_up")                              # progressing, not undoing
    assert (await db.submissions.find_one({"id": s1}))["decision"] == "approved"


async def test_the_agent_undo_executor_asks_for_the_undo_variant():
    src = Path(CP.__file__).resolve().parent.parent / "agents" / "modules" / "casting_pipeline.py"
    assert re.search(r"bulk_move_by_talent_ids\(project_id, ids, prev_stage, undo=True\)", src.read_text())


async def test_no_other_code_path_writes_a_decision_stage_around_the_shared_sync():
    """A guard against a future bypass: every writer of casting_pipeline.stage outside the two synced functions must be one of the known, reviewed
    sites (forward sync, auto-create, merge, casting-call follow_up move, production desk)."""
    root = Path(CP.__file__).resolve().parent.parent
    allowed = {"routers/casting_pipeline.py", "talent_merge_service.py", "routers/whatsapp.py", "routers/production_desk.py", "services/production_reminder_worker.py"}
    pat = re.compile(r"casting_pipeline\.(?:update_one|update_many|find_one_and_update|bulk_write|replace_one)\(")
    offenders = []
    for f in list(root.rglob("*.py")):
        rel = str(f.relative_to(root))
        if rel.startswith(("tests/", "scratch/", "migrations/", "scripts/")) or rel in allowed:
            continue
        if pat.search(f.read_text(errors="ignore")):
            offenders.append(rel)
    assert offenders == [], f"new pipeline writers must go through bulk_move_by_talent_ids / move_pipeline: {offenders}"


# ================================================================== authorization and existing transition rules
async def test_authentication_and_stage_validation_are_unchanged(anon, alice, team):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    sid = await mk_sub(pid, tid)
    for headers in ({}, {"Authorization": "Bearer not.a.token"}):
        assert (await anon.patch(f"/api/projects/{pid}/pipeline/move", headers=headers, json={"ids": [row], "stage": "approved"})).status_code in (401, 403)
        assert (await anon.post(f"/api/projects/{pid}/pipeline/bulk-move", headers=headers, json={"talent_ids": [tid], "stage": "approved"})).status_code in (401, 403)
    assert (await move_ui(anon, alice, pid, row, "not_a_stage")).status_code == 400
    assert (await db.submissions.find_one({"id": sid}))["decision"] == "pending"            # nothing leaked through a refused request
    assert (await move_ui(anon, team, pid, row, "approved")).status_code == 200              # team members keep their existing access
    assert (await db.submissions.find_one({"id": sid}))["status_history"][-1]["admin_email"] == team["email"]


async def test_a_sync_failure_never_breaks_the_pipeline_move(anon, alice, monkeypatch):
    pid, tid = await mk_project(), await mk_talent()
    row = await mk_row(pid, tid)
    await mk_sub(pid, tid)

    monkeypatch.setattr(CP, "active_only", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = await move_ui(anon, alice, pid, row, "approved")
    assert r.status_code == 200 and r.json()["moved"] == 1                                    # the stage still moved; the sync logged and gave up
