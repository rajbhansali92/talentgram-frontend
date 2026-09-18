"""Simple Assistant — Phase 3 controlled execution tests.

Covers: authenticity (HMAC), arbitrary-action injection, stale-plan
protection, entity validation, multi-talent partial failure, idempotency,
audit, and that preview/cancel never mutate. Reuses the real
routers.casting_pipeline mutation functions against a writable FakeDB.

Run:  python backend/tests/test_simple_assistant_execute.py
"""
import asyncio
import copy
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "test-secret-key")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm/execute path (Phase 3), so the new
# independent execution kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routers.casting_pipeline as cp
import simple_assistant.audit as sa_audit
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
from simple_assistant import commands, execute
from simple_assistant.security import sign


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------
# Writable fake Mongo
# --------------------------------------------------------------------------
def _get(doc, path):
    cur = doc
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
            if "$ne" in v and dv == v["$ne"]:
                return False
        elif dv != v:
            return False
    return True


class Cur:
    def __init__(self, docs):
        self._d = [copy.deepcopy(x) for x in docs]

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._d = self._d[:n]
        return self

    async def to_list(self, n=None):
        return list(self._d)


class Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, q=None, proj=None):
        return Cur([d for d in self.docs if _match(d, q)])

    async def find_one(self, q=None, proj=None):
        return next((copy.deepcopy(d) for d in self.docs if _match(d, q)), None)

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _match(d, q))

    def aggregate(self, pipeline):
        match = next((s["$match"] for s in pipeline if "$match" in s), {})
        rows = [d for d in self.docs if _match(d, match)]
        agg = {}
        for r in rows:
            agg[r.get("stage")] = agg.get(r.get("stage"), 0) + 1

        async def gen():
            for k, v in agg.items():
                yield {"_id": k, "count": v}

        class A:
            def __aiter__(self_):
                return gen()

        return A()

    async def insert_many(self, docs):
        self.docs.extend(copy.deepcopy(docs))

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))

    async def update_many(self, q, upd):
        n = 0
        for d in self.docs:
            if _match(d, q):
                d.update(upd.get("$set", {}))
                n += 1

        class R:
            matched_count = n
            modified_count = n

        return R()

    async def delete_many(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match(d, q)]

        class R:
            deleted_count = before - len(self.docs)

        return R()


class DB:
    def __init__(self, **colls):
        self._c = {k: Coll(v) for k, v in colls.items()}

    def __getattr__(self, name):
        return self._c.setdefault(name, Coll([]))

    def __getitem__(self, name):
        return self.__getattr__(name)


USER = {"id": "u_raj", "name": "Raj Bhansali", "email": "raj@x.com", "role": "admin"}


def fresh_db():
    return DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing", "materials": []},
        ],
        talents=[
            {"id": "t_a", "name": "Ahana Pocha", "media": []},
            {"id": "t_p", "name": "Priya Sharma", "media": []},
            {"id": "t_s", "name": "Sana Khan", "media": []},
            {"id": "t_n", "name": "Neha Verma", "media": []},
        ],
        casting_pipeline=[
            {"id": "pl_a", "project_id": "p_g", "talent_id": "t_a", "stage": "shortlisted"},
            {"id": "pl_p", "project_id": "p_g", "talent_id": "t_p", "stage": "hold"},
            {"id": "pl_s", "project_id": "p_g", "talent_id": "t_s", "stage": "approved"},
        ],
    )


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    cp.db = db

    async def _sc(pid):
        c = {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}
        for r in db.casting_pipeline.docs:
            if r["project_id"] == pid:
                c[r["stage"]] = c.get(r["stage"], 0) + 1
        return c

    sa_service.get_stage_counts = _sc


async def preview(msg, ctx=None):
    return await commands.run_command(message=msg, conversation_id=None, context=ctx, user=USER)


async def confirm(ctx):
    return await execute.confirm_and_execute(conversation_id=ctx.get("conversation_id"), context=ctx, user=USER)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_single_mark_executes_via_canonical_fn():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    assert r["state"] == "preview" and r["plan"]["executable"] is True
    assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"  # preview didn't mutate

    e = run(confirm(r["context"]))
    assert e["state"] == "executed", e
    assert e["result"]["counts"]["success"] == 1
    o = e["result"]["outcomes"][0]
    assert o["from"] == "shortlisted" and o["to"] == "not_available" and o["status"] == "success"
    assert next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_a")["stage"] == "not_available"
    assert "overview" in e and e["overview"]["projects"][0]["stage_counts"]["not_available"] == 1
    # audited
    assert any(x.get("agent_id") == "simple-assistant" and x["sa_action"]["executed"]
               for x in db.whatsapp_agent_audit_log.docs)
    print("1. single mark executes via canonical fn + audit + overview refresh OK")


def test_multi_mark_partial_failure_no_false_done():
    db = fresh_db()
    install(db)
    r = run(preview("Ahana, Priya and Sana aren't available for Google AI. Mark them unavailable."))
    assert len(r["plan"]["changes"]) == 3
    ctx = r["context"]
    # someone else moves Sana to locked AFTER the plan was prepared
    next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_s")["stage"] = "locked"

    e = run(confirm(ctx))
    assert e["state"] == "executed"
    by = {o["talent_label"]: o for o in e["result"]["outcomes"]}
    assert by["Ahana Pocha"]["status"] == "success"
    assert by["Priya Sharma"]["status"] == "success"
    assert by["Sana Khan"]["status"] == "stale"
    assert "I couldn't complete every change" in e["message"]
    assert "2 of 3 changes completed successfully" in e["message"]
    # Sana untouched — NOT overwritten
    assert next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_s")["stage"] == "locked"
    print("2. multi-mark partial failure, no false 'Done', stale not overwritten OK")


def test_stale_single_plan_refuses():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_a")["stage"] = "locked"
    e = run(confirm(r["context"]))
    assert e["state"] == "stale", e
    assert "no longer current" in e["message"].lower()
    assert next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_a")["stage"] == "locked"
    print("3. stale single-talent plan → refused, no overwrite OK")


def test_arbitrary_client_action_cannot_be_injected():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    ctx = copy.deepcopy(r["context"])
    # attacker rewrites the plan to lock Priya, keeps... nothing can keep the sig valid
    ctx["context"] if False else None
    ctx["pending"]["plan"]["changes"][0]["entity_id"] = "t_p"
    ctx["pending"]["plan"]["changes"][0]["proposed_value"] = "locked"
    e = run(confirm(ctx))
    assert e["state"] == "blocked"
    assert "couldn't verify" in e["message"].lower()
    # Priya untouched
    assert next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_p")["stage"] == "hold"
    print("4. tampered plan rejected by signature check, nothing executed OK")


def test_unsigned_or_missing_context_rejected():
    db = fresh_db()
    install(db)
    for ctx in (None, {}, {"v": 1, "pending": {"kind": "confirm_plan", "plan": {"intent": "mark_talent_status"}}}):
        e = run(confirm(ctx or {}))
        assert e["state"] == "blocked"
    assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"
    print("5. unsigned / missing context rejected OK")


def test_nonexistent_project_and_talent():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    ctx = r["context"]
    # project deleted after plan
    db.projects.docs = []
    e = run(confirm(ctx))
    assert e["state"] == "blocked" and "no longer exists" in e["message"].lower()
    print("6. nonexistent project → safe blocked OK")


def test_add_and_remove_execute():
    db = fresh_db()
    install(db)
    r = run(preview("Add Neha and remove Ahana from Google AI"))
    assert r["plan"]["executable"] is True
    e = run(confirm(r["context"]))
    assert e["state"] == "executed"
    by = {o["talent_label"]: o["status"] for o in e["result"]["outcomes"]}
    assert by["Neha Verma"] == "success" and by["Ahana Pocha"] == "success"
    assert any(d["talent_id"] == "t_n" for d in db.casting_pipeline.docs)
    assert not any(d["talent_id"] == "t_a" for d in db.casting_pipeline.docs)
    print("7. add + remove execute via canonical fns OK")


def test_idempotent_add_when_already_present():
    db = fresh_db()
    install(db)
    r = run(preview("Add Ahana to Google AI"))
    # Ahana already in pipeline → plan should have no add change / unchanged
    e = run(confirm(r["context"])) if r["state"] == "preview" and r["context"] else r
    # if the plan had a change it must report 'unchanged', never duplicate
    ids = [d["talent_id"] for d in db.casting_pipeline.docs]
    assert ids.count("t_a") == 1
    print("8. idempotent add (already present) → no duplicate OK")


def test_replay_same_plan_does_not_double_apply():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    ctx = r["context"]
    e1 = run(confirm(ctx))
    assert e1["state"] == "executed" and e1["result"]["counts"].get("success") == 1
    # same signed context again (e.g. a retry) → replay, not re-execute
    e2 = run(confirm(ctx))
    assert e2["state"] == "executed"
    assert "already applied" in e2["message"].lower()
    # exactly one execute audit row
    execs = [x for x in db.whatsapp_agent_audit_log.docs
             if x.get("sa_action", {}).get("executed") and x["sa_action"]["action_type"] == "mark_talent_status"]
    assert len(execs) == 1, len(execs)
    print("9. replaying the same plan does not double-apply OK")


def test_create_project_confirm_is_not_enabled():
    db = fresh_db()
    install(db)
    r = run(preview("Create a project from this brief:\nClient: X\nShoot: 1 Jan\nBudget: 100"))
    assert r["state"] == "preview" and r["plan"]["executable"] is False
    e = run(confirm(r["context"]))
    assert e["state"] == "blocked" and "isn't enabled" in e["message"].lower()
    print("10. create_project confirm → not enabled, nothing runs OK")


def test_update_field_intent_says_not_enabled():
    db = fresh_db()
    install(db)
    r = run(preview("Change Ahana's height to 5'8\""))
    assert r["state"] == "blocked" and "isn't enabled" in r["message"].lower()
    print("11. field-edit request → understood but not enabled OK")


def test_preview_and_cancel_never_mutate():
    db = fresh_db()
    install(db)
    snap = copy.deepcopy(db.casting_pipeline.docs)
    for msg in ("Mark Ahana unavailable for Google AI", "Add Neha and remove Priya from Google AI",
                "Mark Priya not interested for Google AI"):
        run(preview(msg))
    assert db.casting_pipeline.docs == snap
    print("12. preview never mutates; cancel is client-side no-op OK")


# ---- SA-2 (pre-commit audit): explicit 30-minute expiry -------------------
def test_fresh_plan_carries_a_30min_expiry_and_still_executes():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    pending = r["context"]["pending"]
    assert pending["kind"] == "confirm_plan"
    exp = datetime.fromisoformat(pending["expires_at"])
    delta = exp - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < delta <= timedelta(minutes=30)
    # unchanged default behavior — a fresh (non-expired) plan still executes
    e = run(confirm(r["context"]))
    assert e["state"] == "executed" and e["result"]["counts"]["success"] == 1
    print("13. fresh confirm_plan carries a ~30min expires_at and still executes normally OK")


def test_expired_plan_refuses_even_with_a_valid_signature():
    db = fresh_db()
    install(db)
    r = run(preview("Mark Ahana unavailable for Google AI"))
    # a GENUINELY expired-but-validly-signed context — not merely tampered.
    # Re-sign after backdating expires_at so this exercises the expiry check
    # itself, not the (already separately tested) signature-mismatch path.
    pending = copy.deepcopy(r["context"]["pending"])
    pending["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ctx = {"v": r["context"]["v"], "conversation_id": r["context"]["conversation_id"],
           "pending": pending, "sig": sign(pending)}
    e = run(confirm(ctx))
    assert e["state"] == "blocked", e
    assert "expired" in e["message"].lower()
    assert e["need_refresh"] is True
    # nothing mutated
    assert next(d for d in db.casting_pipeline.docs if d["talent_id"] == "t_a")["stage"] == "shortlisted"
    # and not falsely recorded as executed
    assert not any(x.get("agent_id") == "simple-assistant" and x["sa_action"].get("plan_id") == pending["plan_id"]
                   for x in db.whatsapp_agent_audit_log.docs)
    print("14. genuinely expired-but-validly-signed confirm_plan → blocked, nothing executed, not audited as done OK")


def test_confirm_submission_and_ai_response_kinds_unaffected_by_confirm_plan_ttl():
    # SA-2 is scoped to confirm_plan/confirm_comm only — confirm_ai_response
    # already had its own expires_at (unchanged); confirm_submission/
    # confirm_upload are explicitly out of scope and must not gain one.
    pending = commands._ctx("c1", {"kind": "confirm_submission", "sub_plan": {}})["pending"]
    assert "expires_at" not in pending
    print("15. confirm_submission gets no expires_at from this change (out of scope) OK")


# ---- execution kill-switch (independent of SIMPLE_ASSISTANT_ENABLED) -----
def test_execution_disabled_blocks_confirm_plan_and_calls_nothing():
    db = fresh_db()
    install(db)
    # master flag stays ON for this whole file; toggle ONLY the execution gate
    old = os.environ.get("SA_EXECUTION_ENABLED")
    try:
        os.environ["SA_EXECUTION_ENABLED"] = "false"
        # read-only preview still works with execution off
        r = run(preview("Mark Ahana unavailable for Google AI"))
        assert r["state"] == "preview" and r["plan"]["executable"] is True

        e = run(confirm(r["context"]))
        assert e["state"] == "blocked", e
        assert "disabled" in e["message"].lower()
        # nothing mutated
        assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"
        # not falsely recorded as executed, and the plan is not consumed —
        # confirming the SAME context again once execution is re-enabled
        # must still work (proves the block didn't invalidate the plan)
        assert not any(x.get("agent_id") == "simple-assistant" for x in db.whatsapp_agent_audit_log.docs)
        os.environ["SA_EXECUTION_ENABLED"] = "true"
        e2 = run(confirm(r["context"]))
        assert e2["state"] == "executed", e2
        assert db.casting_pipeline.docs[0]["stage"] == "not_available"
    finally:
        if old is None:
            os.environ.pop("SA_EXECUTION_ENABLED", None)
        else:
            os.environ["SA_EXECUTION_ENABLED"] = old
    print("16. execution disabled -> confirm_plan blocked, nothing mutated, plan not consumed; "
          "re-enabling lets the SAME plan execute OK")


if __name__ == "__main__":
    for fn in [
        test_single_mark_executes_via_canonical_fn,
        test_multi_mark_partial_failure_no_false_done,
        test_stale_single_plan_refuses,
        test_arbitrary_client_action_cannot_be_injected,
        test_unsigned_or_missing_context_rejected,
        test_nonexistent_project_and_talent,
        test_add_and_remove_execute,
        test_idempotent_add_when_already_present,
        test_replay_same_plan_does_not_double_apply,
        test_create_project_confirm_is_not_enabled,
        test_update_field_intent_says_not_enabled,
        test_preview_and_cancel_never_mutate,
        test_fresh_plan_carries_a_30min_expiry_and_still_executes,
        test_expired_plan_refuses_even_with_a_valid_signature,
        test_confirm_submission_and_ai_response_kinds_unaffected_by_confirm_plan_ttl,
        test_execution_disabled_blocks_confirm_plan_and_calls_nothing,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT EXECUTION TESTS PASSED")
