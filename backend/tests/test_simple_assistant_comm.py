"""Simple Assistant — Phase 4A communication + media resolution/send tests.

No live DB, no real WhatsApp. A writable FakeDB + a stubbed
routers.whatsapp._create_batch_internal drive every scenario.

Run:  python backend/tests/test_simple_assistant_comm.py
"""
import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase4a-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm_and_send path (Phase 4A), so the new
# independent execution kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import simple_assistant.audit as sa_audit
import simple_assistant.comm as sa_comm
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
import simple_assistant.whatsapp_send as wsend
from simple_assistant import comm_execute
from simple_assistant.commands import run_command
from simple_assistant.security import sign


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- fake mongo -----------------------------------------------------------
def _get(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        dv = _get(doc, k) if "." in k else doc.get(k)
        if isinstance(v, dict):
            if "$in" in v and dv not in v["$in"]:
                return False
        elif dv != v:
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

    async def insert_one(s, d):
        s.docs.append(copy.deepcopy(d))

    async def insert_many(s, ds):
        s.docs.extend(copy.deepcopy(ds))

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
CASTING_GROUP = "Google AI Casting"


def base_db():
    return DB(
        projects=[{
            "id": "p_g", "brand_name": "Google AI", "status": "ongoing",
            "character": "Lead 25-30", "shoot_dates": "18 Sep", "budget_per_day": "25000",
            "medium_usage": "Digital", "whatsapp_casting_group_name": CASTING_GROUP, "materials": [],
        }],
        talents=[
            {"id": "t_neha", "name": "Neha Sharma", "whatsapp_group_name": "Neha Sharma x Talentgram",
             "phone": "+919812345678", "media": []},
            {"id": "t_riya", "name": "Riya Shah", "phone": "+919800000000", "media": []},   # number only
            {"id": "t_sana", "name": "Sana Khan", "media": []},                             # neither
        ],
        casting_pipeline=[
            {"id": "pl1", "project_id": "p_g", "talent_id": "t_neha", "stage": "shortlisted"},
        ],
        submissions=[{
            "id": "sub_neha_g", "project_id": "p_g", "talent_id": "t_neha", "status": "submitted",
            "media": [
                {"id": "m_intro", "category": "intro_video", "url": "https://cdn/intro.mp4",
                 "public_id": "pi", "resource_type": "video"},
                {"id": "m_t1", "category": "take", "label": "Take 1", "url": "https://cdn/t1.mp4"},
                {"id": "m_t2", "category": "take", "label": "Take 2", "url": "https://cdn/t2.mp4"},
            ],
        }],
        links=[{"id": "lnk1", "slug": "neha-sharma-portfolio", "title": "Neha Sharma",
                "talent_ids": ["t_neha"], "created_at": "2026-09-01"}],
        whatsapp_templates=[{"id": "tmpl_custom", "slug": "custom", "body_text": "{{message}}", "is_custom": True}],
        whatsapp_jobs=[],
    )


_BATCH_CALLS = []
_BATCH_MODE = {"mode": "ok"}   # "ok" | "raise" | "fail_take2" | "empty"


async def _fake_create_batch_internal(payload, admin):
    _BATCH_CALLS.append({"contacts": [c.model_dump() for c in payload.source_params.contacts],
                         "message": payload.variable_data.get("message"),
                         "media_url": payload.media_url, "dry_run": payload.is_dry_run})
    if _BATCH_MODE["mode"] == "raise":
        raise RuntimeError("WhatsApp session not connected")
    if _BATCH_MODE["mode"] == "empty":
        return {"batch": {"id": "b?"}, "jobs": []}
    msg = payload.variable_data.get("message", "")
    if _BATCH_MODE["mode"] == "fail_take2" and "Take 2" in msg:
        from fastapi import HTTPException
        raise HTTPException(400, "media too large")
    bid = f"batch_{len(_BATCH_CALLS)}"
    return {"batch": {"id": bid}, "jobs": [{"id": f"job_{len(_BATCH_CALLS)}", "talent_id": None}]}


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
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
    return await comm_execute.confirm_and_send(conversation_id=ctx.get("conversation_id"), context=ctx, user=USER)


# ---- RESOLUTION ---------------------------------------------------------
def test_talent_with_group_uses_group():
    install(base_db())
    r = run(cmd("Send Neha the Google AI project"))
    assert r["state"] == "preview", r
    assert r["comm"]["destination_type"] == "whatsapp_group"
    assert r["comm"]["destination"] == "Neha Sharma x Talentgram"
    print("1. talent with WhatsApp group → group OK")


def test_talent_with_only_number():
    install(base_db())
    r = run(cmd("Send Riya the Google AI project"))
    assert r["state"] == "preview", r
    assert r["comm"]["destination_type"] == "whatsapp_number"
    assert any("no saved Talentgram WhatsApp group" in w for w in r["comm"]["warnings"])
    print("2. talent with only number → number + warning OK")


def test_talent_with_neither():
    install(base_db())
    r = run(cmd("Send Sana the Google AI project"))
    assert r["state"] == "blocked"
    assert "doesn't have a saved WhatsApp group or number" in r["message"]
    print("3. talent with neither → safe blocked OK")


def test_project_casting_group_resolution():
    install(base_db())
    r = run(cmd("Send Neha's intro to the Google AI casting group"))
    assert r["state"] == "preview"
    assert r["comm"]["destination_type"] == "whatsapp_group"
    assert r["comm"]["destination"] == CASTING_GROUP        # project's group, NOT Neha's own
    assert r["comm"]["destination"] != "Neha Sharma x Talentgram"
    print("4. project casting group ≠ talent's own group OK")


def test_missing_casting_group():
    db = base_db()
    db.projects.docs[0]["whatsapp_casting_group_name"] = ""
    install(db)
    r = run(cmd("Send Neha's intro to the Google AI casting group"))
    assert r["state"] == "blocked" and "no WhatsApp casting group configured" in r["message"]
    print("5. missing casting group → blocked (no guess) OK")


def test_missing_intro():
    db = base_db()
    db.submissions.docs[0]["media"] = [m for m in db.submissions.docs[0]["media"] if m["category"] != "intro_video"]
    install(db)
    r = run(cmd("Send Neha's intro to Google AI casting"))
    assert r["state"] == "blocked" and "couldn't find Intro Video" in r["message"]
    print("6. missing intro → not found, no substitute OK")


def test_missing_audition_take_partial_clarification():
    install(base_db())  # has intro + take1 + take2, not take 3
    r = run(cmd("Send Neha's intro and audition take 3 to Google AI casting"))
    assert r["state"] == "clarification", r
    assert "Intro Video available" in r["clarification"]["prompt"]
    assert "Audition Take 3 missing" in r["clarification"]["prompt"]
    # picking "send only available" produces an executable preview with just the intro
    r2 = run(cmd("1", r["context"]))
    assert r2["state"] == "preview" and r2["comm"]["executable"] is True
    assert [m["label"] for m in r2["comm"]["media"]] == ["Intro Video"]
    print("7. missing take → clarification, never silent substitution OK")


def test_missing_profile_link():
    db = base_db()
    db.links.docs = []
    install(db)
    r = run(cmd("Send Neha's profile to Google AI casting"))
    assert r["state"] == "blocked"
    assert "doesn't currently have a generated profile link" in r["message"]
    assert "isn't enabled" in r["message"]
    print("8. missing profile link → preview-only message, no auto-generate OK")


def test_project_talent_submission_relationship():
    install(base_db())
    r = run(cmd("Send Neha's intro and audition take 2 to Google AI casting"))
    assert r["comm"]["submission_id"] == "sub_neha_g"
    assert r["comm"]["talent"]["id"] == "t_neha" and r["comm"]["project"]["id"] == "p_g"
    labels = [m["label"] for m in r["comm"]["media"]]
    assert labels == ["Intro Video", "Take 2"]
    assert [m["media_id"] for m in r["comm"]["media"]] == ["m_intro", "m_t2"]  # canonical ids, not filenames
    print("9. project+talent+submission+media resolved canonically OK")


def test_upload_vs_whatsapp_send_distinction():
    install(base_db())
    # Phase 4C: "upload ... audition ... submission" is now handled by the
    # submission-ingest layer, not the comm layer. With no file attached it
    # asks for the file — and never touches WhatsApp.
    ru = run(cmd("Upload Neha's audition take 2 to her Google AI submission"))
    assert ru["comm"] is None
    assert ru["state"] == "blocked"
    assert "file" in ru["message"].lower()
    assert _BATCH_CALLS == []  # NOTHING touched WhatsApp
    rs = run(cmd("Send Neha's audition take 2 to Google AI casting"))
    assert rs["comm"]["action_type"] == "send_media" and rs["comm"]["destination_type"] == "whatsapp_group"
    print("10. upload (submission) vs send (WhatsApp) are distinct OK")


# ---- SECURITY ----------------------------------------------------------
def test_unsigned_plan_rejected():
    install(base_db())
    for ctx in (None, {}, {"v": 1, "pending": {"kind": "confirm_comm", "comm_plan": {"action_type": "send_media"}}}):
        r = run(confirm(ctx or {}))
        assert r["state"] == "blocked"
    assert _BATCH_CALLS == []
    print("11. unsigned communication plan rejected OK")


def _preview_media():
    install(base_db())
    return run(cmd("Send Neha's intro to Google AI casting"))


def test_tampered_destination_rejected():
    r = _preview_media()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["comm_plan"]["destination"] = "Attacker Group"
    rr = run(confirm(ctx))
    assert rr["state"] == "blocked" and "couldn't verify" in rr["message"].lower()
    assert _BATCH_CALLS == []
    print("12. tampered destination → rejected, nothing sent OK")


def test_tampered_media_id_rejected():
    r = _preview_media()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["comm_plan"]["media"][0]["media_id"] = "m_someone_else"
    ctx["pending"]["comm_plan"]["media"][0]["url"] = "https://evil/x.mp4"
    rr = run(confirm(ctx))
    assert rr["state"] == "blocked" and "couldn't verify" in rr["message"].lower()
    print("13. tampered media id/url → rejected OK")


def test_tampered_talent_project_rejected():
    r = _preview_media()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["comm_plan"]["talent"]["id"] = "t_riya"
    rr = run(confirm(ctx))
    assert rr["state"] == "blocked" and "couldn't verify" in rr["message"].lower()
    print("14. tampered talent/project → rejected OK")


def test_arbitrary_phone_cannot_be_injected():
    # send_project_info to Riya (number) — attacker swaps the number
    install(base_db())
    r = run(cmd("Send Riya the Google AI project"))
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["comm_plan"]["destination"] = "+910000000000"
    rr = run(confirm(ctx))
    assert rr["state"] == "blocked"  # signature breaks
    # even a VALID sig can't inject: the executor re-derives the number from t_riya
    r2 = run(cmd("Send Riya the Google AI project"))
    ok = run(confirm(r2["context"]))
    assert ok["result"]["destination"] == "+919800000000"  # the real record value
    print("15. arbitrary phone number cannot be injected OK")


def test_arbitrary_media_url_cannot_be_injected():
    r = _preview_media()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["comm_plan"]["media"][0]["url"] = "https://evil/x.mp4"
    rr = run(confirm(ctx))
    assert rr["state"] == "blocked"
    # honest path re-derives the URL from the submission media id
    r2 = _preview_media()
    ok = run(confirm(r2["context"]))
    assert _BATCH_CALLS[-1]["media_url"] == "https://cdn/intro.mp4"
    print("16. arbitrary media URL cannot be injected OK")


# ---- APPROVAL --------------------------------------------------------
def test_preview_no_external_send():
    install(base_db())
    run(cmd("Send Neha's intro and audition take 2 to Google AI casting"))
    assert _BATCH_CALLS == [] or all(c["dry_run"] for c in _BATCH_CALLS)
    print("17. preview causes no external send OK")


def test_cancel_no_external_send():
    r = _preview_media()
    # frontend cancel is local; server never told to send
    assert r["state"] == "preview"
    assert _BATCH_CALLS == [] or all(c["dry_run"] for c in _BATCH_CALLS)
    print("18. cancel causes no external send OK")


def test_confirmation_sends_signed_plan_only():
    r = _preview_media()
    e = run(confirm(r["context"]))
    assert e["state"] == "queued", e
    real = [c for c in _BATCH_CALLS if not c["dry_run"]]
    assert len(real) == 1
    assert real[0]["media_url"] == "https://cdn/intro.mp4"
    assert real[0]["contacts"][0]["whatsapp_group_name"] == CASTING_GROUP
    # audited
    assert any(x.get("agent_id") == "simple-assistant" and x["sa_action"]["action_type"] == "send_media"
               for x in sa_rdb._real_db.whatsapp_agent_audit_log.docs)
    print("19. confirmation sends exactly the signed, server-resolved plan OK")


def test_replay_does_not_send_twice():
    r = _preview_media()
    ctx = r["context"]
    e1 = run(confirm(ctx))
    real_after_1 = len([c for c in _BATCH_CALLS if not c["dry_run"]])
    e2 = run(confirm(ctx))
    real_after_2 = len([c for c in _BATCH_CALLS if not c["dry_run"]])
    assert e2["state"] == "queued"
    assert "already sent" in e2["message"].lower()
    assert real_after_2 == real_after_1  # no second send
    print("20. replay does not send twice OK")


# ---- FAILURE ---------------------------------------------------------
def test_whatsapp_failure_reported():
    r = _preview_media()
    _BATCH_MODE["mode"] = "raise"
    e = run(confirm(r["context"]))
    assert e["state"] == "failed"
    assert "couldn't send" in e["message"].lower()
    assert "no successful send was recorded" in e["message"].lower()
    print("21. WhatsApp failure reported, never success OK")


def test_partial_media_failure_reported():
    install(base_db())
    r = run(cmd("Send Neha's intro and audition take 2 to Google AI casting"))
    _BATCH_MODE["mode"] = "fail_take2"
    e = run(confirm(r["context"]))
    assert e["state"] == "partial", e
    by = {o["label"]: o["status"] for o in e["result"]["outcomes"]}
    assert by["Intro Video"] == "queued" and by["Take 2"] == "failed"
    assert "couldn't complete the entire set" in e["message"]
    print("22. partial media failure reported accurately OK")


def test_missing_destination_safe():
    db = base_db()
    db.projects.docs[0]["whatsapp_casting_group_name"] = ""
    install(db)
    r = run(cmd("Send Neha's intro to Google AI casting"))
    assert r["state"] == "blocked"
    assert _BATCH_CALLS == []
    print("23. missing destination handled safely OK")


# ---- SA-2 (pre-commit audit): explicit 30-minute expiry -------------------
def test_fresh_comm_plan_carries_a_30min_expiry_and_still_sends():
    r = _preview_media()
    pending = r["context"]["pending"]
    assert pending["kind"] == "confirm_comm"
    exp = datetime.fromisoformat(pending["expires_at"])
    delta = exp - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < delta <= timedelta(minutes=30)
    # unchanged default behavior — a fresh (non-expired) plan still sends
    e = run(confirm(r["context"]))
    assert e["state"] == "queued", e
    assert len([c for c in _BATCH_CALLS if not c["dry_run"]]) == 1
    print("24. fresh confirm_comm carries a ~30min expires_at and still sends normally OK")


def test_expired_comm_plan_refuses_even_with_a_valid_signature():
    r = _preview_media()
    # a GENUINELY expired-but-validly-signed context — not merely tampered.
    # Re-sign after backdating expires_at so this exercises the expiry check
    # itself, not the (already separately tested) signature-mismatch path.
    pending = copy.deepcopy(r["context"]["pending"])
    pending["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ctx = {"v": r["context"]["v"], "conversation_id": r["context"]["conversation_id"],
           "pending": pending, "sig": sign(pending)}
    real_before = len([c for c in _BATCH_CALLS if not c["dry_run"]])
    e = run(confirm(ctx))
    assert e["state"] == "blocked", e
    assert "expired" in e["message"].lower()
    assert e["need_refresh"] is True
    # no REAL send — the preview step's own destination dry-run (if any)
    # doesn't count, exactly as test_confirmation_sends_signed_plan_only
    # distinguishes real vs. dry-run calls
    assert len([c for c in _BATCH_CALLS if not c["dry_run"]]) == real_before
    print("25. genuinely expired-but-validly-signed confirm_comm → blocked, nothing sent OK")


# ---- execution kill-switch (independent of SIMPLE_ASSISTANT_ENABLED) -----
def test_execution_disabled_blocks_confirm_comm_and_calls_nothing():
    old = os.environ.get("SA_EXECUTION_ENABLED")
    try:
        os.environ["SA_EXECUTION_ENABLED"] = "false"
        r = _preview_media()  # preview still works with execution off
        real_before = len([c for c in _BATCH_CALLS if not c["dry_run"]])

        e = run(confirm(r["context"]))
        assert e["state"] == "blocked", e
        assert "disabled" in e["message"].lower()
        # no real send — only the preview's own dry-run (if any) is allowed
        assert len([c for c in _BATCH_CALLS if not c["dry_run"]]) == real_before

        # the plan is not consumed by the block — re-enabling lets it send
        os.environ["SA_EXECUTION_ENABLED"] = "true"
        e2 = run(confirm(r["context"]))
        assert e2["state"] == "queued", e2
        assert len([c for c in _BATCH_CALLS if not c["dry_run"]]) == real_before + 1
    finally:
        if old is None:
            os.environ.pop("SA_EXECUTION_ENABLED", None)
        else:
            os.environ["SA_EXECUTION_ENABLED"] = old
    print("26. execution disabled -> confirm_comm blocked, nothing sent, plan not consumed; "
          "re-enabling lets the SAME plan send OK")


if __name__ == "__main__":
    for fn in [
        test_talent_with_group_uses_group, test_talent_with_only_number, test_talent_with_neither,
        test_project_casting_group_resolution, test_missing_casting_group, test_missing_intro,
        test_missing_audition_take_partial_clarification, test_missing_profile_link,
        test_project_talent_submission_relationship, test_upload_vs_whatsapp_send_distinction,
        test_unsigned_plan_rejected, test_tampered_destination_rejected, test_tampered_media_id_rejected,
        test_tampered_talent_project_rejected, test_arbitrary_phone_cannot_be_injected,
        test_arbitrary_media_url_cannot_be_injected, test_preview_no_external_send,
        test_cancel_no_external_send, test_confirmation_sends_signed_plan_only,
        test_replay_does_not_send_twice, test_whatsapp_failure_reported,
        test_partial_media_failure_reported, test_missing_destination_safe,
        test_fresh_comm_plan_carries_a_30min_expiry_and_still_sends,
        test_expired_comm_plan_refuses_even_with_a_valid_signature,
        test_execution_disabled_blocks_confirm_comm_and_calls_nothing,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT COMM TESTS PASSED")
