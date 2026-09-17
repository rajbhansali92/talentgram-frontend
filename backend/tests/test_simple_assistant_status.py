"""Simple Assistant — Phase 6 WhatsApp delivery-status inspection (READ-ONLY).

Reads the EXISTING engine collections (whatsapp_agent_audit_log sa_action
rows → whatsapp_jobs / whatsapp_batches). Never sends, retries, replies, or
mutates any job/batch/worker state.

Run:  python3 backend/tests/test_simple_assistant_status.py
"""
import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase6-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inbound_messages as sa_inbound
import simple_assistant.audit as sa_audit
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
import simple_assistant.status as sa_status
import simple_assistant.whatsapp_send as wsend
from simple_assistant.commands import run_command


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _cmp(dv, v):
    if isinstance(v, dict):
        if "$in" in v:
            return dv in v["$in"]
        if "$ne" in v:
            return dv != v["$ne"]
    return dv == v


def _get(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        dv = _get(doc, k) if "." in k else doc.get(k)
        if not _cmp(dv, v):
            return False
    return True


class Cur:
    def __init__(s, d):
        s._d = [copy.deepcopy(x) for x in d]

    def sort(s, key, direction=1):
        if isinstance(key, str):
            s._d.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=(direction == -1))
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

    async def insert_one(s, d):
        s.docs.append(copy.deepcopy(d))

    async def update_one(s, *a, **k):
        raise AssertionError("Phase 6 is read-only — no update_one expected")

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


USER = {"id": "u1", "name": "Raj", "email": "raj@x.com", "role": "admin"}
GROUP = "Google AI Casting"


def _t(minutes_ago):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def _send_row(plan_id, batch_ids, media_labels, *, minutes_ago, tid="t_ahana", pid="p_g"):
    return {
        "timestamp": _t(minutes_ago),
        "agent_id": "simple-assistant",
        "sa_action": {
            "plan_id": plan_id, "executed": True, "action_type": "send_media",
            "talent_id": tid, "talent_label": "Ahana Pocha",
            "project_id": pid, "project_label": "Google AI",
            "destination": GROUP, "destination_type": "whatsapp_group",
            "batch_ids": batch_ids, "media_ids": ["m_x"] * len(media_labels),
            "outcomes": [{"label": l, "status": "queued"} for l in media_labels],
        },
    }


def _job(batch_id, label, status, *, verification=None, err=None, minutes_ago=8, sent_ago=None):
    return {
        "id": f"job_{batch_id}_{label}", "batch_id": batch_id, "status": status,
        "verification_status": verification, "error_message": err, "attempt_count": 1,
        "created_at": _t(minutes_ago),
        "sent_at": (_t(sent_ago) if sent_ago is not None else None),
        "message_body": f"Ahana Pocha — Google AI · {label}",
        "destination": GROUP, "talent_id": "t_ahana",
    }


def base_db(jobs=None, sends=None):
    return DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing", "materials": []},
            {"id": "p_l", "brand_name": "L'Oreal Glyco", "status": "ongoing", "materials": []},
        ],
        talents=[
            {"id": "t_ahana", "name": "Ahana Pocha", "email": "ahana@x.com", "media": []},
            {"id": "t_bhavna", "name": "Bhavna Roy", "email": "bhavna@x.com", "media": []},
        ],
        casting_pipeline=[{"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"}],
        submissions=[{"id": "sub_ahana_g", "project_id": "p_g", "talent_id": "t_ahana",
                      "talent_email": "ahana@x.com", "status": "submitted", "media": []}],
        whatsapp_agent_audit_log=list(sends or [_send_row("sap_1", ["b1"], ["Intro Video"], minutes_ago=10)]),
        whatsapp_jobs=list(jobs if jobs is not None else [_job("b1", "Intro Video", "sent", verification="verified", sent_ago=7)]),
        whatsapp_batches=[],
        whatsapp_conversations=[],
        links=[],
    )


_BATCH_CALLS = []


async def _no_send(*a, **k):
    _BATCH_CALLS.append(1)
    raise AssertionError("Phase 6 must never call _create_batch_internal")


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    sa_inbound.db = db
    wsend.db = db
    wsend._create_batch_internal = _no_send
    _BATCH_CALLS.clear()

    async def _sc(pid):
        return {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}

    sa_service.get_stage_counts = _sc


async def cmd(m, ctx=None):
    return await run_command(message=m, conversation_id=None, context=ctx, user=USER)


def _audit_rows(db):
    return [x for x in db.whatsapp_agent_audit_log.docs
            if (x.get("sa_action") or {}).get("action_type") == "inspect_whatsapp_status"]


# ---- STATUS VALUES --------------------------------------------------
def _card_for(jobs):
    db = base_db(jobs=jobs)
    install(db)
    r = run(cmd("Did Ahana's Google AI send go through?"))
    return r


def test_queued_and_pending_status():
    for st in ("pending", "dry_run_preview"):
        r = _card_for([_job("b1", "Intro Video", st)])
        assert r["state"] == "answer" and r["status"]["overall"] == "queued", (st, r)
        assert "not confirmed as sent" in r["status"]["overall_copy"].lower()
    print("1/2. queued + pending → 'queued' with correct copy OK")


def test_sending_status():
    r = _card_for([_job("b1", "Intro Video", "sending")])
    assert r["status"]["overall"] == "sending"
    assert "worker" in r["status"]["overall_copy"].lower()
    print("3. sending status OK")


def test_sent_status():
    r = _card_for([_job("b1", "Intro Video", "sent", sent_ago=5)])
    assert r["status"]["overall"] == "sent"
    assert r["status"]["sent_at"] is not None
    print("4. sent status OK")


def test_verified_status():
    r = _card_for([_job("b1", "Intro Video", "sent", verification="verified", sent_ago=5)])
    assert r["status"]["overall"] == "verified"
    assert "verified" in r["status"]["overall_copy"].lower()
    print("5. verified status OK")


def test_failed_status_and_reason():
    r = _card_for([_job("b1", "Intro Video", "failed", err="INVALID_DESTINATION: group not found")])
    assert r["status"]["overall"] == "failed"
    assert r["status"]["failure_reason"] == "INVALID_DESTINATION: group not found"
    assert "no automatic retry" in r["message"].lower()
    print("6/7. failed status + verbatim reason + 'no retry' OK")


def test_multiple_jobs_and_media():
    db = base_db(
        sends=[_send_row("sap_1", ["b1"], ["Intro Video", "Take 2"], minutes_ago=10)],
        jobs=[_job("b1", "Intro Video", "sent", verification="verified", sent_ago=4),
              _job("b1", "Take 2", "pending")],
    )
    install(db)
    r = run(cmd("What is the status of Ahana's Google AI casting message?"))
    assert r["status"]["job_count"] == 2
    by = {m["label"]: m["status"] for m in r["status"]["media"]}
    assert by["Intro Video"] == "verified" and by["Take 2"] == "queued"
    # overall is worst-first → not fully out yet
    assert r["status"]["overall"] == "queued"
    print("8/9. multiple jobs + per-media status + worst-first overall OK")


def test_latest_send_resolution_activity_list():
    db = base_db(sends=[
        _send_row("sap_old", ["b_old"], ["Intro Video"], minutes_ago=1440),
        _send_row("sap_new", ["b_new"], ["Intro Video", "Take 2"], minutes_ago=30),
    ], jobs=[_job("b_old", "Intro Video", "sent", sent_ago=1439),
             _job("b_new", "Intro Video", "sent", sent_ago=25),
             _job("b_new", "Take 2", "sent", sent_ago=25)])
    install(db)
    r = run(cmd("Show me the latest WhatsApp activity for Ahana"))
    assert r["answer"]["kind"] == "whatsapp_activity"
    # newest first
    assert "Take 2" in r["answer"]["rows"][0]["media"]
    print("10. latest send / activity list newest-first OK")


# ---- AMBIGUITY -----------------------------------------------------
def test_multiple_sends_clarification():
    db = base_db(sends=[
        _send_row("sap_a", ["b_a"], ["Intro Video", "Take 2"], minutes_ago=20),
        _send_row("sap_b", ["b_b"], ["Intro Video"], minutes_ago=900),
    ], jobs=[_job("b_a", "Intro Video", "sent"), _job("b_a", "Take 2", "sent"),
             _job("b_b", "Intro Video", "sent")])
    install(db)
    r = run(cmd("Did Ahana's Google AI send go through?"))
    assert r["state"] == "clarification"
    assert len(r["clarification"]["options"]) == 2
    # pick #1 → its card
    r2 = run(cmd("1", r["context"]))
    assert r2["state"] == "answer" and r2["status"]["plan_id"] == "sap_a"
    print("11. multiple sends → clarification → pick resolves OK")


def test_wrong_project_no_send():
    install(base_db())
    r = run(cmd("Did Ahana's L'Oreal Glyco send go through?"))
    assert r["state"] == "answer"
    assert "don't see" in r["message"].lower()
    assert "not replied" not in r["message"].lower()
    print("12. wrong project → honest 'no send', never a wrong one OK")


# ---- SECURITY ----------------------------------------------------
def test_tampered_clarification_context_rejected():
    db = base_db(sends=[
        _send_row("sap_a", ["b_a"], ["Intro Video"], minutes_ago=20),
        _send_row("sap_b", ["b_b"], ["Take 2"], minutes_ago=900),
    ], jobs=[_job("b_a", "Intro Video", "sent"), _job("b_b", "Take 2", "sent")])
    install(db)
    r = run(cmd("Did Ahana's Google AI send go through?"))
    assert r["state"] == "clarification"
    # tamper the signed talent / project / plan_id
    for mut in (lambda p: p["talent"].update(id="t_bhavna"),
                lambda p: p.update(project={"id": "p_l", "label": "L'Oreal Glyco"}),
                lambda p: p["options"][0].update(plan_id="sap_evil")):
        ctx = copy.deepcopy(r["context"])
        mut(ctx["pending"])
        rr = run(cmd("1", ctx))
        # signature no longer verifies → the pending is dropped → "1" is not a
        # status command → UNKNOWN help, and NO card for the wrong entity
        assert rr.get("status") is None, (mut, rr)
    print("13-17. tampered talent / project / plan_id in signed context → no card OK")


# ---- READ-ONLY --------------------------------------------------
def test_status_query_is_read_only():
    db = base_db(jobs=[_job("b1", "Intro Video", "sent", verification="verified", sent_ago=3)])
    before_sub = copy.deepcopy(db.submissions.docs[0])
    before_pipe = copy.deepcopy(db.casting_pipeline.docs)
    before_jobs = copy.deepcopy(db.whatsapp_jobs.docs)
    before_batches = copy.deepcopy(db.whatsapp_batches.docs)
    install(db)
    run(cmd("What happened to Ahana's Google AI casting message?"))
    assert db.whatsapp_jobs.docs == before_jobs
    assert db.whatsapp_batches.docs == before_batches
    assert db.submissions.docs[0] == before_sub
    assert db.casting_pipeline.docs == before_pipe
    assert _BATCH_CALLS == []                      # 0 outbound
    # the ONLY write is one read-only inspection audit row
    rows = _audit_rows(db)
    assert len(rows) == 1 and rows[0]["sa_action"]["executed"] is False
    assert rows[0]["sa_action"]["action_type"] == "inspect_whatsapp_status"
    print("18-22. status query: 0 jobs/batches/outbound, no submission/pipeline mutation, 1 inspect row OK")


# ---- RETRY (STEP 9) -------------------------------------------
def test_retry_not_enabled():
    install(base_db())
    r = run(cmd("Retry it."))
    assert r["state"] == "blocked"
    assert "retry isn't enabled" in r["message"].lower()
    assert _BATCH_CALLS == []
    assert base_db().whatsapp_jobs is not None  # sanity
    print("STEP 9. 'Retry it.' → not enabled, no job created OK")


# ---- INBOUND (Phase 7 supersedes the Phase 6 limitation) ----------
def test_inbound_no_captured_message_is_safe():
    # capture is OFF by default → no rows → honest "I don't see a reply yet",
    # never a false "has not replied".
    install(base_db())
    for q in ("Did Ahana reply?", "Show Ahana's latest reply", "Did Ahana reply about Google AI?"):
        r = run(cmd(q))
        assert r["state"] in ("answer", "blocked")
        assert "has not replied" not in r["message"].lower()
        assert "hasn't replied" not in r["message"].lower()
        assert r.get("inbound") is None
    print("23-25. no captured inbound → safe 'don't see a reply yet' OK")


def test_no_llm_and_no_reply_side_effect():
    # status.py must not pull in any LLM client
    src = open(os.path.join(os.path.dirname(__file__), "..", "simple_assistant", "status.py")).read()
    assert "ai.client" not in src and "anthropic" not in src.lower() and "openai" not in src.lower()
    ib = open(os.path.join(os.path.dirname(__file__), "..", "inbound_messages.py")).read()
    assert "anthropic" not in ib.lower() and "openai" not in ib.lower() and "gemini" not in ib.lower()
    install(base_db())
    run(cmd("What did Ahana say?"))
    assert _BATCH_CALLS == []                      # no auto-reply / outbound
    print("26/27. no LLM interpretation, no automatic reply OK")


if __name__ == "__main__":
    for fn in [
        test_queued_and_pending_status, test_sending_status, test_sent_status,
        test_verified_status, test_failed_status_and_reason, test_multiple_jobs_and_media,
        test_latest_send_resolution_activity_list, test_multiple_sends_clarification,
        test_wrong_project_no_send, test_tampered_clarification_context_rejected,
        test_status_query_is_read_only, test_retry_not_enabled,
        test_inbound_no_captured_message_is_safe, test_no_llm_and_no_reply_side_effect,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT STATUS (PHASE 6) TESTS PASSED")
