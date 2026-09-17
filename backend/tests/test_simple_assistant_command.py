"""Simple Assistant — Phase 2 command layer (dry-run) unit tests.

No live DB, no network, no LLM. A FakeDB with controlled data drives every
scenario; a write-trap FakeDB proves the command path never mutates.

Run:  python backend/tests/test_simple_assistant_command.py   (or via pytest)
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import simple_assistant
from simple_assistant import commands, readonly_db
from simple_assistant import service as sa_service


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------
# Fake Mongo
# --------------------------------------------------------------------------
def _match(doc, query):
    for k, v in (query or {}).items():
        dv = doc.get(k)
        if isinstance(v, dict):
            if "$in" in v and dv not in v["$in"]:
                return False
            if "$nin" in v and dv in v["$nin"]:
                return False
            if "$ne" in v and dv == v["$ne"]:
                return False
        elif dv != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None):
        return list(self._docs)


class FakeColl:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query=None, projection=None):
        return FakeCursor([d for d in self.docs if _match(d, query)])

    def aggregate(self, pipeline):
        # only get_stage_counts uses this: [{$match},{$group by $stage}]
        match = next((s["$match"] for s in pipeline if "$match" in s), {})
        rows = [d for d in self.docs if _match(d, match)]
        agg = {}
        for r in rows:
            agg[r.get("stage")] = agg.get(r.get("stage"), 0) + 1

        async def _gen():
            for k, v in agg.items():
                yield {"_id": k, "count": v}

        class _A:
            def __aiter__(self_):
                return _gen()

        return _A()

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _match(d, query))


class FakeDB:
    def __init__(self, **collections):
        self._c = {name: FakeColl(docs) for name, docs in collections.items()}

    def __getattr__(self, name):
        return self._c.setdefault(name, FakeColl([]))

    def __getitem__(self, name):
        return self.__getattr__(name)


class WriteTrapDB(FakeDB):
    """Every collection raises on any non-read attribute access."""

    class _Trap(FakeColl):
        def __getattr__(self, item):
            raise AssertionError(f"WRITE ATTEMPTED: {item}()")

    def __getattr__(self, name):
        if name not in self._c:
            self._c[name] = WriteTrapDB._Trap([])
        elif not isinstance(self._c[name], WriteTrapDB._Trap):
            self._c[name] = WriteTrapDB._Trap(self._c[name].docs)
        return self._c[name]


# --------------------------------------------------------------------------
# Fixture data
# --------------------------------------------------------------------------
def make_db(cls=FakeDB):
    projects = [
        {"id": "p_g", "brand_name": "Google AI", "status": "ongoing", "character": "Lead 25-30", "materials": []},
        {"id": "p_m", "brand_name": "Microsoft AI Campaign", "status": "ongoing", "materials": []},
        {"id": "p_n", "brand_name": "Nike Run", "status": "ongoing", "materials": []},
        {"id": "p_done", "brand_name": "Old Thing", "status": "complete", "materials": []},
    ]
    talents = [
        {"id": "t_ahana", "name": "Ahana Pocha", "dob": "1998-01-01", "height": "5'6\"", "media": []},
        {"id": "t_priya", "name": "Priya Sharma", "media": []},
        {"id": "t_sana", "name": "Sana Khan", "media": []},
        {"id": "t_neha", "name": "Neha Verma", "media": []},
        {"id": "t_riya1", "name": "Riya Sharma", "media": []},
        {"id": "t_riya2", "name": "Riya Mehta", "media": []},
        {"id": "t_riya3", "name": "Riya Kapoor", "media": []},
    ]
    pipeline = [
        {"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"},
        {"id": "pl2", "project_id": "p_g", "talent_id": "t_priya", "stage": "hold"},
        {"id": "pl3", "project_id": "p_g", "talent_id": "t_sana", "stage": "approved"},
        {"id": "pl4", "project_id": "p_g", "talent_id": "t_riya2", "stage": "ask_to_test"},
    ]
    submissions = []
    return cls(projects=projects, talents=talents, casting_pipeline=pipeline, submissions=submissions)


USER = {"name": "Raj Bhansali", "email": "raj@x.com", "role": "admin"}


def _install(db):
    readonly_db._real_db = db
    sa_service.db = db

    async def _stage_counts(pid):
        counts = {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}
        for r in db.casting_pipeline.docs:
            if r["project_id"] == pid:
                counts[r["stage"]] = counts.get(r["stage"], 0) + 1
        return counts

    sa_service.get_stage_counts = _stage_counts


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_feature_flag_off_by_default():
    old = os.environ.pop("SIMPLE_ASSISTANT_ENABLED", None)
    try:
        assert simple_assistant.is_enabled() is False
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
        assert simple_assistant.is_enabled() is True
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "1"
        assert simple_assistant.is_enabled() is True
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "yes"
        assert simple_assistant.is_enabled() is True
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "on"
        assert simple_assistant.is_enabled() is True
        for v in ("false", "0", "", "no", "nope"):
            os.environ["SIMPLE_ASSISTANT_ENABLED"] = v
            assert simple_assistant.is_enabled() is False, v
    finally:
        os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true" if old is None else old
    print("1. feature flag OFF by default OK")


def test_deterministic_mark_resolves_and_previews():
    _install(make_db())
    r = run(commands.run_command(
        message="Ahana, Priya and Sana aren't available for Google AI. Mark them unavailable.",
        conversation_id=None, context=None, user=USER,
    ))
    assert r["state"] == "preview", r
    assert r["intent"] == "mark_talent_status"
    assert r["plan"]["project"]["label"] == "Google AI"
    changes = {c["entity_label"]: c for c in r["plan"]["changes"]}
    assert set(changes) == {"Ahana Pocha", "Priya Sharma", "Sana Khan"}
    assert changes["Ahana Pocha"]["current_value"] == "shortlisted"
    assert changes["Ahana Pocha"]["proposed_value"] == "not_available"
    assert changes["Priya Sharma"]["current_label"] == "On hold"
    assert r["requires_confirmation"] is True
    assert "ready to make this change" in r["message"].lower()
    assert r["plan"]["executable"] is True  # Phase 3: this plan CAN be executed on Confirm
    print("2. mark: resolve + preview OK")


def test_dry_run_contains_current_and_proposed():
    _install(make_db())
    r = run(commands.run_command(
        message="Mark Ahana not interested for Google AI", conversation_id=None, context=None, user=USER,
    ))
    c = r["plan"]["changes"][0]
    assert c["current_value"] == "shortlisted" and c["current_label"] == "Shortlisted"
    assert c["proposed_value"] == "not_interested" and c["proposed_label"] == "Not interested"
    assert c["op"] == "update" and c["field"] == "pipeline_stage"
    print("3. plan has current + proposed state OK")


def test_ambiguous_talent_asks():
    db = make_db()
    db.casting_pipeline.docs += [
        {"id": "x1", "project_id": "p_g", "talent_id": "t_riya1", "stage": "hold"},
        {"id": "x2", "project_id": "p_g", "talent_id": "t_riya3", "stage": "hold"},
    ]
    _install(db)
    r = run(commands.run_command(
        message="Mark Riya unavailable for Google AI", conversation_id=None, context=None, user=USER,
    ))
    assert r["state"] == "clarification", r
    assert r["clarification"]["kind"] == "talent"
    labels = {o["name"] for o in r["clarification"]["options"]}
    assert {"Riya Sharma", "Riya Mehta", "Riya Kapoor"} <= labels
    assert r["plan"] is None
    print("4. ambiguous talent → clarification OK")


def test_ambiguous_project_asks():
    _install(make_db())
    r = run(commands.run_command(
        message="Riya Mehta isn't available for the AI project", conversation_id=None, context=None, user=USER,
    ))
    assert r["state"] == "clarification", r
    assert r["clarification"]["kind"] == "project"
    labels = {o["label"] for o in r["clarification"]["options"]}
    assert {"Google AI", "Microsoft AI Campaign"} <= labels
    print("5. ambiguous project → clarification OK")


def test_multi_turn_project_clarification_preserves_context():
    _install(make_db())
    r1 = run(commands.run_command(
        message="Ahana isn't available for the AI project", conversation_id=None, context=None, user=USER,
    ))
    assert r1["state"] == "clarification" and r1["context"]["pending"]["kind"] == "clarify_project"
    # user replies "1" (Google AI, sorted first)
    r2 = run(commands.run_command(
        message="1", conversation_id=r1["conversation_id"], context=r1["context"], user=USER,
    ))
    assert r2["state"] == "preview", r2
    assert r2["plan"]["project"]["label"] == "Google AI"
    assert r2["plan"]["changes"][0]["entity_label"] == "Ahana Pocha"
    print("6. multi-turn project clarification preserves context OK")


def test_multi_turn_talent_clarification_by_name():
    db = make_db()
    db.casting_pipeline.docs += [
        {"id": "x1", "project_id": "p_g", "talent_id": "t_riya1", "stage": "hold"},
        {"id": "x2", "project_id": "p_g", "talent_id": "t_riya3", "stage": "hold"},
    ]
    _install(db)
    r1 = run(commands.run_command(
        message="Mark Riya unavailable for Google AI", conversation_id=None, context=None, user=USER,
    ))
    assert r1["state"] == "clarification"
    r2 = run(commands.run_command(
        message="Riya Mehta", conversation_id=r1["conversation_id"], context=r1["context"], user=USER,
    ))
    assert r2["state"] == "preview", r2
    assert [c["entity_label"] for c in r2["plan"]["changes"]] == ["Riya Mehta"]
    print("7. multi-turn talent clarification by name OK")


def test_add_remove_dry_run():
    _install(make_db())
    r = run(commands.run_command(
        message="Add Neha and remove Ahana from Google AI", conversation_id=None, context=None, user=USER,
    ))
    assert r["state"] == "preview", r
    ops = {c["entity_label"]: c["op"] for c in r["plan"]["changes"]}
    assert ops == {"Neha Verma": "add", "Ahana Pocha": "remove"}
    add_c = next(c for c in r["plan"]["changes"] if c["op"] == "add")
    assert add_c["current_label"] == "Not in pipeline"
    assert add_c["proposed_value"] == "ask_to_test"
    rm_c = next(c for c in r["plan"]["changes"] if c["op"] == "remove")
    assert rm_c["current_value"] == "shortlisted"
    assert rm_c["proposed_label"] == "Removed from pipeline"
    print("8. add/remove dry-run OK")


def test_add_remove_talent_clarification_locks_in_pick():
    db = make_db()
    # two "Neha" in the roster → add is ambiguous
    db.talents.docs.append({"id": "t_neha2", "name": "Neha Kapoor", "media": []})
    db.talents.docs[3]["name"] = "Neha Rao"
    _install(db)
    r1 = run(commands.run_command(
        message="Add Neha to Google AI", conversation_id=None, context=None, user=USER,
    ))
    assert r1["state"] == "clarification" and r1["clarification"]["kind"] == "talent"
    r2 = run(commands.run_command(
        message="1", conversation_id=r1["conversation_id"], context=r1["context"], user=USER,
    ))
    assert r2["state"] == "preview", r2
    assert r2["plan"]["changes"][0]["op"] == "add"
    print("8b. add/remove talent clarification locks in the pick OK")


def test_create_project_preview_only():
    _install(make_db())
    brief = ("Create a project from this brief:\nClient: XYZ Films\nShoot: 18 September\n"
             "Location: Mumbai\nBudget: 25000\nUsage: Digital")
    r = run(commands.run_command(message=brief, conversation_id=None, context=None, user=USER))
    assert r["state"] == "preview" and r["intent"] == "create_project"
    fields = r["plan"]["changes"][0]["fields"]
    assert fields["Client / brand"] == "XYZ Films"
    assert fields["Shoot dates"] == "18 September"
    assert fields["Budget"] == "25000"
    assert r["plan"]["changes"][0]["op"] == "create"
    assert "no project has been created" in r["message"].lower()
    assert r["plan"]["executable"] is False
    print("9. create project preview only OK")


def test_preview_carries_signed_confirm_context():
    _install(make_db())
    r = run(commands.run_command(
        message="Mark Ahana unavailable for Google AI", conversation_id=None, context=None, user=USER,
    ))
    assert r["state"] == "preview"
    ctx = r["context"]
    assert ctx["pending"]["kind"] == "confirm_plan"
    assert "plan_id" in ctx["pending"] and ctx["pending"]["plan_id"].startswith("sap_")
    # the pending payload is HMAC-signed and tamper-evident
    from simple_assistant.security import verify
    assert verify(ctx["pending"], ctx["sig"]) is True
    tampered = {**ctx["pending"], "plan": {**ctx["pending"]["plan"], "intent": "evil"}}
    assert verify(tampered, ctx["sig"]) is False
    # execution lives in execute.py — commands has no confirm/execute path
    assert not hasattr(commands, "confirm_plan")
    print("10. preview carries a signed confirm context OK")


def test_unclear_command_is_safe():
    _install(make_db())
    for msg in ("florble the wibbit", "", "do something cool", "asdkjhaskjd"):
        r = run(commands.run_command(message=msg, conversation_id=None, context=None, user=USER))
        assert r["state"] == "blocked", (msg, r)
        assert r["plan"] is None
    print("11. unclear command → safe blocked OK")


def test_read_queries_return_real_data():
    _install(make_db())
    r = run(commands.run_command(message="How many active projects do we have?",
                                 conversation_id=None, context=None, user=USER))
    assert r["state"] == "answer"
    assert r["answer"]["summary"]["active_projects"] == 3  # p_done excluded
    r2 = run(commands.run_command(message="Show me the Google AI pipeline",
                                  conversation_id=None, context=None, user=USER))
    assert r2["state"] == "answer" and r2["answer"]["kind"] == "project_pipeline"
    assert r2["answer"]["project"]["pipeline_total"] == 4
    print("12. read queries return real data OK")


def test_preview_never_writes():
    """run_command (understand → resolve → preview) against a DB that
    raises on any write op. Execution is a separate endpoint (execute.py)
    with its own tests (test_simple_assistant_execute.py)."""
    _install(make_db(cls=WriteTrapDB))
    scenarios = [
        "Ahana, Priya aren't available for Google AI. Mark them unavailable.",
        "Mark Riya unavailable for Google AI",
        "Riya isn't available for the AI project",
        "Add Neha and remove Ahana from Google AI",
        "Create a project from this brief: Client: X\nShoot: 1 Jan\nBudget: 100",
        "Change Ahana's height to 5'8\"",
        "How many active projects?",
        "What needs my attention?",
        "Which talents have tests received?",
        "gibberish nonsense",
    ]
    for s in scenarios:
        r = run(commands.run_command(message=s, conversation_id=None, context=None, user=USER))
        assert r["state"] in ("preview", "clarification", "answer", "blocked"), (s, r)
    print("13. run_command preview never writes OK")


def test_readonly_db_guard_blocks_writes():
    from simple_assistant.readonly_db import RDB
    try:
        RDB.talents.update_one({}, {})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "read-only" in str(e)
    # reads are fine
    assert hasattr(RDB.talents, "find")
    print("14. readonly_db guard blocks writes, allows reads OK")


if __name__ == "__main__":
    test_feature_flag_off_by_default()
    test_deterministic_mark_resolves_and_previews()
    test_dry_run_contains_current_and_proposed()
    test_ambiguous_talent_asks()
    test_ambiguous_project_asks()
    test_multi_turn_project_clarification_preserves_context()
    test_multi_turn_talent_clarification_by_name()
    test_add_remove_dry_run()
    test_add_remove_talent_clarification_locks_in_pick()
    test_create_project_preview_only()
    test_preview_carries_signed_confirm_context()
    test_unclear_command_is_safe()
    test_read_queries_return_real_data()
    test_preview_never_writes()
    test_readonly_db_guard_blocks_writes()
    print("\nALL SIMPLE ASSISTANT COMMAND TESTS PASSED")
