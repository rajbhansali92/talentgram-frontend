"""Simple Assistant — Phase 5 approved WhatsApp casting forwarding.

Builds on the Phase 4A comm layer. Exercises destination ambiguity, whole-
submission send, "the audition I just uploaded", content-fingerprint stale
detection, and the full re-resolution / idempotency / audit contract.

No live DB, no real WhatsApp: a writable FakeDB + a stubbed
routers.whatsapp._create_batch_internal drive every scenario. The Playwright
worker is never involved — 0 real external messages.

Run:  python3 backend/tests/test_simple_assistant_send.py
"""
import asyncio
import copy
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase5-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm_and_send path, so the new independent
# execution kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agents.modules.media_assignment as ma
import routers.casting_pipeline as cp
import routers.submissions as subs_router
import simple_assistant.audit as sa_audit
import simple_assistant.comm as sa_comm
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
import simple_assistant.whatsapp_send as wsend
from simple_assistant import comm_execute
from simple_assistant.commands import run_command


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- fake mongo (with $or / $in / $ne, dotted paths) ----
def _cmp(dv, v):
    if isinstance(v, dict):
        if "$in" in v:
            return dv in v["$in"]
        if "$ne" in v:
            return dv != v["$ne"]
        if "$exists" in v:
            return (dv is not None) == v["$exists"]
    return dv == v


def _get(d, path):
    cur = d
    for p in path.split("."):
        if isinstance(cur, list):
            return [_get(x, p) if isinstance(x, dict) else None for x in cur]
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        dv = _get(doc, k) if "." in k else doc.get(k)
        if not _cmp(dv, v):
            return False
    return True


class Cur:
    def __init__(s, d):
        s._d = [copy.deepcopy(x) for x in d]

    def sort(s, *a, **k):
        return s

    def limit(s, n):
        s._d = s._d[:n]
        return s

    async def to_list(s, n=None):
        return list(s._d)


class Coll:
    def __init__(s, d=None):
        s.docs = list(d or [])

    def find(s, q=None, p=None):
        return Cur([d for d in s.docs if _match(d, q)])

    async def find_one(s, q=None, p=None, **k):
        return next((copy.deepcopy(d) for d in s.docs if _match(d, q)), None)

    async def count_documents(s, q=None):
        return sum(1 for d in s.docs if _match(d, q))

    async def update_one(s, q, upd, **k):
        matched = 0
        for d in s.docs:
            if _match(d, q):
                matched = 1
                if "$set" in upd:
                    d.update(upd["$set"])
                if "$push" in upd:
                    for key, val in upd["$push"].items():
                        d.setdefault(key, []).append(copy.deepcopy(val))
                break

        class R:
            matched_count = matched
            modified_count = matched

        return R()

    async def insert_one(s, d):
        s.docs.append(copy.deepcopy(d))

    def aggregate(s, pipe):
        async def g():
            for x in []:
                yield x

        class A:
            def __aiter__(self_):
                return g()

        return A()


class DB:
    def __init__(s, **c):
        s._c = {k: Coll(v) for k, v in c.items()}

    def __getattr__(s, n):
        return s._c.setdefault(n, Coll([]))

    def __getitem__(s, n):
        return s.__getattr__(n)


USER = {"id": "u1", "name": "Raj Bhansali", "email": "raj@x.com", "role": "admin"}
PROJECT_GROUP = "Google AI Casting"
AHANA_GROUP = "Ahana Pocha x Talentgram"


def base_db():
    return DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing",
             "character": "Lead 25-30", "shoot_dates": "18 Sep", "budget_per_day": "25000",
             "medium_usage": "Digital", "whatsapp_casting_group_name": PROJECT_GROUP, "materials": []},
            {"id": "p_l", "brand_name": "L'Oreal Glyco", "status": "ongoing",
             "whatsapp_casting_group_name": "L'Oreal Casting", "materials": []},
        ],
        talents=[
            # Ahana has her OWN casting group AND the project has one → ambiguity
            {"id": "t_ahana", "name": "Ahana Pocha", "email": "ahana@x.com",
             "normalized_email": "ahana@x.com",
             "whatsapp_group_name": AHANA_GROUP, "phone": "+919810000001", "media": []},
            # Riya has only a number
            {"id": "t_riya", "name": "Riya Shah", "email": "riya@x.com",
             "normalized_email": "riya@x.com", "phone": "+919820000002", "media": []},
        ],
        casting_pipeline=[
            {"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"},
            {"id": "pl2", "project_id": "p_g", "talent_id": "t_riya", "stage": "shortlisted"},
        ],
        submissions=[
            {"id": "sub_ahana_g", "project_id": "p_g", "talent_id": "t_ahana",
             "talent_email": "ahana@x.com", "status": "submitted",
             "media": [
                 {"id": "m_intro", "category": "intro_video", "url": "https://cdn/intro.mp4",
                  "public_id": "pi_intro", "resource_type": "video"},
                 {"id": "m_t1", "category": "take", "label": "Take 1", "url": "https://cdn/t1.mp4",
                  "public_id": "pi_t1", "created_at": "2026-09-01T00:00:00Z"},
                 {"id": "m_t2", "category": "take", "label": "Take 2", "url": "https://cdn/t2.mp4",
                  "public_id": "pi_t2", "created_at": "2026-09-03T00:00:00Z"},
             ]},
            {"id": "sub_riya_g", "project_id": "p_g", "talent_id": "t_riya",
             "talent_email": "riya@x.com", "status": "submitted",
             "media": [
                 {"id": "r_t1", "category": "take", "label": "Take 1", "url": "https://cdn/rt1.mp4",
                  "public_id": "pi_rt1", "created_at": "2026-09-02T00:00:00Z"},
             ]},
        ],
        links=[{"id": "lnk1", "slug": "ahana-pocha", "title": "Ahana Pocha",
                "talent_ids": ["t_ahana"], "created_at": "2026-09-01"}],
        whatsapp_templates=[{"id": "tmpl_custom", "slug": "custom", "body_text": "{{message}}", "is_custom": True}],
        whatsapp_jobs=[],
        whatsapp_batches=[],
    )


_BATCH_CALLS = []
_BATCH_MODE = {"mode": "ok"}


async def _fake_create_batch_internal(payload, admin):
    _BATCH_CALLS.append({
        "source_type": payload.source_type,
        "contacts": [c.model_dump() for c in payload.source_params.contacts],
        "template_id": payload.template_id,
        "message": payload.variable_data.get("message"),
        "media_url": payload.media_url,
        "dry_run": payload.is_dry_run,
        "admin_email": (admin or {}).get("email"),
    })
    if _BATCH_MODE["mode"] == "raise":
        raise RuntimeError("WhatsApp session not connected")
    n = len(_BATCH_CALLS)
    bid = f"batch_{n}"
    # mirror the real engine: a batch + job row land in the DB
    return {"batch": {"id": bid}, "jobs": [{"id": f"job_{n}", "talent_id": None}]}


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    cp.db = db
    subs_router.db = db
    ma.db = db
    wsend.db = db
    wsend._create_batch_internal = _fake_create_batch_internal
    _BATCH_CALLS.clear()
    _BATCH_MODE["mode"] = "ok"

    async def _sc(pid):
        return {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}

    sa_service.get_stage_counts = _sc


async def cmd(m, ctx=None):
    return await run_command(message=m, conversation_id=None, context=ctx, user=USER)


async def confirm(ctx):
    return await comm_execute.confirm_and_send(
        conversation_id=(ctx or {}).get("conversation_id"), context=ctx, user=USER)


def _real_sends():
    return [c for c in _BATCH_CALLS if not c["dry_run"]]


# ---- RESOLUTION -------------------------------------------------------
def test_talent_project_submission_media_resolution():
    install(base_db())
    r = run(cmd("Send Ahana's intro and Take 2 to the Google AI casting group"))
    assert r["state"] == "preview" and r["intent"] == "communication"
    c = r["comm"]
    assert c["talent"]["id"] == "t_ahana" and c["project"]["id"] == "p_g"
    assert c["submission_id"] == "sub_ahana_g"
    assert [m["label"] for m in c["media"]] == ["Intro Video", "Take 2"]
    assert [m["media_id"] for m in c["media"]] == ["m_intro", "m_t2"]
    print("1-4. talent/project/submission/media resolution OK")


def test_explicit_casting_group_destination():
    install(base_db())
    r = run(cmd("Send Ahana's intro to the Google AI casting group"))
    assert r["state"] == "preview"
    assert r["comm"]["destination"] == PROJECT_GROUP
    assert r["comm"]["destination_source"] == "project_casting_group"
    print("5. explicit casting-group destination OK")


def test_ambiguous_destination_clarification_then_pick():
    install(base_db())
    # "to Google AI" (no "casting group") + Ahana has her own group → ambiguous
    r = run(cmd("Send Ahana's Take 2 to Google AI"))
    assert r["state"] == "clarification", r
    labels = [o["label"] for o in r["clarification"]["options"]]
    assert PROJECT_GROUP in labels and AHANA_GROUP in labels
    assert _BATCH_CALLS == []
    # pick #2 — the talent's own group
    r2 = run(cmd("2", r["context"]))
    assert r2["state"] == "preview"
    assert r2["comm"]["destination"] == AHANA_GROUP
    assert r2["comm"]["destination_source"] == "talent_own"
    # pick #1 — project casting group
    r3 = run(cmd("1", r["context"]))
    assert r3["comm"]["destination"] == PROJECT_GROUP
    print("6/8. ambiguous destination → clarification → pick group OK")


def test_number_fallback_when_no_groups():
    db = base_db()
    db.projects.docs[0]["whatsapp_casting_group_name"] = ""   # project: no group
    install(db)
    r = run(cmd("Send Riya's Take 1 to Google AI"))            # Riya: number only
    assert r["state"] == "preview"
    assert r["comm"]["destination"] == "+919820000002"
    assert r["comm"]["destination_type"] == "whatsapp_number"
    assert r["comm"]["destination_source"] == "talent_own"
    assert any("would go to" in w.lower() for w in r["comm"]["warnings"])
    print("7. number fallback (no groups anywhere) OK")


def test_whole_submission_send():
    install(base_db())
    r = run(cmd("Send Ahana's Google AI submission to the casting group"))
    assert r["state"] == "preview", r
    labels = [m["label"] for m in r["comm"]["media"]]
    assert labels[0] == "Intro Video"
    assert "Take 1" in labels and "Take 2" in labels
    assert r["comm"]["destination"] == PROJECT_GROUP
    print("+. whole-submission send resolves intro + every take OK")


def test_latest_upload_reference():
    install(base_db())
    r = run(cmd("Send the audition I just uploaded for Ahana to the Google AI casting group"))
    assert r["state"] == "preview", r
    # newest take by created_at is m_t2 (2026-09-03)
    assert [m["media_id"] for m in r["comm"]["media"]] == ["m_t2"]
    print("+. 'the audition I just uploaded' → newest take OK")


def test_bulk_is_refused():
    install(base_db())
    r = run(cmd("Send these talents to Google AI"))
    assert r["state"] != "preview"
    assert _BATCH_CALLS == []
    print("+. bulk send refused, zero jobs OK")


# ---- PREVIEW --------------------------------------------------------
def test_preview_zero_jobs_zero_batches_zero_mutation():
    db = base_db()
    install(db)
    before_sub = copy.deepcopy(db.submissions.docs[0])
    before_pipe = copy.deepcopy(db.casting_pipeline.docs)
    run(cmd("Send Ahana's intro to the Google AI casting group"))
    assert _real_sends() == []
    assert db.whatsapp_jobs.docs == [] and db.whatsapp_batches.docs == []
    assert db.submissions.docs[0] == before_sub
    assert db.casting_pipeline.docs == before_pipe
    assert db.links.docs == [db.links.docs[0]]  # unchanged, none created
    print("9-11. preview → 0 jobs, 0 batches, 0 mutation OK")


# ---- CONFIRMATION --------------------------------------------------
def test_confirm_invokes_engine_with_correct_everything():
    install(base_db())
    r = run(cmd("Send Ahana's intro and Take 2 to the Google AI casting group"))
    e = run(confirm(r["context"]))
    assert e["state"] == "queued", e
    real = _real_sends()
    assert len(real) == 2
    for call in real:
        assert call["source_type"] == "MANUAL"                       # correct source_type
        assert call["template_id"] == "tmpl_custom"                  # existing custom template
        assert call["contacts"][0]["whatsapp_group_name"] == PROJECT_GROUP  # correct destination
        assert call["admin_email"] == "raj@x.com"
    assert real[0]["media_url"] == "https://cdn/intro.mp4"           # existing hosted URL, not re-uploaded
    assert real[1]["media_url"] == "https://cdn/t2.mp4"
    # audit
    row = next(x for x in sa_rdb._real_db.whatsapp_agent_audit_log.docs
               if x.get("sa_action", {}).get("action_type") == "send_media")
    sa = row["sa_action"]
    assert sa["talent_id"] == "t_ahana" and sa["project_id"] == "p_g"
    assert sa["destination"] == PROJECT_GROUP
    assert sorted(sa["media_ids"]) == ["m_intro", "m_t2"]
    assert len(sa["batch_ids"]) == 2
    print("12-18/20. confirm → existing engine, correct talent/project/media/dest/template/source OK")


def test_confirm_reports_queued_not_sent():
    install(base_db())
    r = run(cmd("Send Ahana's intro to the Google AI casting group"))
    e = run(confirm(r["context"]))
    assert e["state"] == "queued"
    assert "queued" in e["message"].lower() and "sent" not in e["result"]["outcomes"][0]["status"]
    assert e["result"]["outcomes"][0]["status"] == "queued"
    print("16 (status). confirm reports 'queued', never 'sent' OK")


# ---- SECURITY -----------------------------------------------------
def _preview():
    install(base_db())
    return run(cmd("Send Ahana's intro to the Google AI casting group"))


def test_unsigned_plan_rejected():
    install(base_db())
    for c in (None, {}, {"v": 1, "pending": {"kind": "confirm_comm", "comm_plan": {"action_type": "send_media"}}}):
        e = run(confirm(c or {}))
        assert e["state"] == "blocked"
    assert _real_sends() == []
    print("19. unsigned plan rejected OK")


def test_tampered_talent_project_submission_rejected():
    for field, val in (("talent", {"id": "t_riya", "label": "Riya Shah"}),
                       ("project", {"id": "p_l", "label": "L'Oreal Glyco"}),
                       ("submission_id", "sub_riya_g")):
        r = _preview()
        ctx = copy.deepcopy(r["context"])
        ctx["pending"]["comm_plan"][field] = val
        e = run(confirm(ctx))
        assert e["state"] == "blocked" and "verify" in e["message"].lower(), (field, e)
        assert _real_sends() == []
    print("20-22. tampered talent / project / submission rejected OK")


def test_tampered_media_and_destination_rejected():
    for mut in (lambda p: p["media"][0].update(media_id="m_evil", url="https://evil/x.mp4"),
                lambda p: p.update(destination="Attacker Group"),
                lambda p: p["media"][0].update(url="https://evil/x.mp4"),
                lambda p: p["media"][0].update(public_id="pi_evil")):
        r = _preview()
        ctx = copy.deepcopy(r["context"])
        mut(ctx["pending"]["comm_plan"])
        e = run(confirm(ctx))
        assert e["state"] == "blocked", e
        assert _real_sends() == []
    print("23-25. tampered media id/url/public_id + destination rejected OK")


# ---- STALE STATE ------------------------------------------------
def test_changed_media_detected():
    r = _preview()
    # someone replaces the intro (new id + new public_id) after the preview
    sub = sa_rdb._real_db.submissions.docs[0]
    sub["media"] = [m for m in sub["media"] if m["category"] != "intro_video"]
    sub["media"].append({"id": "m_intro_v2", "category": "intro_video",
                         "url": "https://cdn/intro-v2.mp4", "public_id": "pi_intro_v2"})
    e = run(confirm(r["context"]))
    assert e["state"] == "stale", e
    assert "no longer current" in e["message"].lower()
    assert _real_sends() == []
    print("26. changed media (replaced intro) → stale, nothing sent OK")


def test_changed_media_same_id_different_content_detected():
    r = _preview()
    sub = sa_rdb._real_db.submissions.docs[0]
    for m in sub["media"]:
        if m["category"] == "intro_video":
            m["public_id"] = "pi_intro_retranscoded"
            m["url"] = "https://cdn/intro-new.mp4"
    e = run(confirm(r["context"]))
    assert e["state"] == "stale"
    assert _real_sends() == []
    print("26b. same media id, changed content fingerprint → stale OK")


def test_changed_destination_detected():
    r = _preview()
    sa_rdb._real_db.projects.docs[0]["whatsapp_casting_group_name"] = "Renamed Casting Group"
    e = run(confirm(r["context"]))
    assert e["state"] == "stale", e
    assert "destination" in e["message"].lower()
    assert _real_sends() == []
    print("27. changed destination → stale, nothing sent OK")


def test_changed_submission_detected():
    r = _preview()
    # the submission the plan named is gone / replaced
    sa_rdb._real_db.submissions.docs[0]["id"] = "sub_ahana_g_v2"
    e = run(confirm(r["context"]))
    assert e["state"] == "stale", e
    assert _real_sends() == []
    print("28. changed submission → stale, nothing sent OK")


# ---- IDEMPOTENCY --------------------------------------------------
def test_replay_creates_no_second_job_or_batch():
    r = _preview()
    ctx = r["context"]
    e1 = run(confirm(ctx))
    assert e1["state"] == "queued"
    n1 = len(_real_sends())
    e2 = run(confirm(ctx))
    n2 = len(_real_sends())
    assert n1 == n2 == 1
    assert "already" in e2["message"].lower()
    execs = [x for x in sa_rdb._real_db.whatsapp_agent_audit_log.docs
             if x.get("sa_action", {}).get("executed") and x["sa_action"]["action_type"] == "send_media"]
    assert len(execs) == 1
    print("29-30. replay → no 2nd job / batch, one audit row OK")


# ---- SIDE EFFECTS ----------------------------------------------
def test_no_cloudinary_no_submission_no_pipeline_no_link_mutation():
    db = base_db()
    install(db)
    before_sub = copy.deepcopy(db.submissions.docs[0])
    r = run(cmd("Send Ahana's intro and Take 2 to the Google AI casting group"))
    run(confirm(r["context"]))
    assert db.asset_metadata.docs == []               # no Cloudinary upload tracked
    assert db.submissions.docs[0]["media"] == before_sub["media"]  # submission untouched
    assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"   # pipeline untouched
    assert len(db.links.docs) == 1                    # no profile link created
    print("31-34. no Cloudinary / submission / pipeline / link mutation OK")


def test_profile_link_send_uses_existing_link_only():
    db = base_db()
    db.links.docs = []
    install(db)
    r = run(cmd("Send Ahana's profile to the Google AI casting group"))
    assert r["state"] == "blocked"
    assert "isn't enabled" in r["message"]
    assert len(sa_rdb._real_db.links.docs) == 0       # nothing generated
    print("34b. profile-link send never auto-generates a link OK")


if __name__ == "__main__":
    for fn in [
        test_talent_project_submission_media_resolution, test_explicit_casting_group_destination,
        test_ambiguous_destination_clarification_then_pick, test_number_fallback_when_no_groups,
        test_whole_submission_send, test_latest_upload_reference, test_bulk_is_refused,
        test_preview_zero_jobs_zero_batches_zero_mutation,
        test_confirm_invokes_engine_with_correct_everything, test_confirm_reports_queued_not_sent,
        test_unsigned_plan_rejected, test_tampered_talent_project_submission_rejected,
        test_tampered_media_and_destination_rejected, test_changed_media_detected,
        test_changed_media_same_id_different_content_detected, test_changed_destination_detected,
        test_changed_submission_detected, test_replay_creates_no_second_job_or_batch,
        test_no_cloudinary_no_submission_no_pipeline_no_link_mutation,
        test_profile_link_send_uses_existing_link_only,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT SEND (PHASE 5) TESTS PASSED")
