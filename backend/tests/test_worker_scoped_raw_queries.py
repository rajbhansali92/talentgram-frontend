"""Multi-Worker Step 3 — tests for the three previously worker-unscoped
whatsapp_agent_config queries found during the Phase 1/2 audit:

  1. backend/inbound_messages.py:resolve_group (was: find_one by
     group_names alone)
  2. backend/services/media_assignment_worker.py:_send_report (was:
     find_one by agent_id alone)
  3. backend/services/production_reminder_worker.py:_management_agent_group
     + _maybe_send_daily_briefing (was: find_one/find_one_and_update/
     update_one by agent_id alone, x4)

Each test proves TWO things together: the same agent_id can have
different, independent configs per worker, AND a worker's own lookup
never reads a different worker's configuration (mirroring
test_agent_routing_worker_scoped.py's pattern for the routing path these
raw queries sit alongside). No live DB.

Run:  python backend/tests/test_worker_scoped_raw_queries.py
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


def _matches(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
        elif isinstance(v, dict) and "$exists" in v:
            if (k in doc) != v["$exists"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        else:
            actual = doc.get(k)
            # Mirror MongoDB's implicit array-containment semantics: a
            # plain scalar query value matches a list FIELD if the value
            # is one of its elements (e.g. {"group_names": "X"} against a
            # document whose group_names is ["X", "Y"]) — not just exact
            # equality, which is what a real find_one({"group_names": g})
            # call (as used by inbound_messages.resolve_group) relies on.
            if isinstance(actual, list):
                if v not in actual:
                    return False
            elif actual != v:
                return False
    return True


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        query = query or {}
        matched = [d for d in self.docs if _matches(d, query)]

        class _Cur:
            def __init__(self, docs):
                self._docs = docs

            async def to_list(self, n=None):
                return list(self._docs)

        return _Cur(matched)

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None

    async def find_one_and_update(self, query, update, **kw):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDB:
    def __init__(self):
        self.whatsapp_agent_config = FakeColl()
        self.projects = FakeColl()
        self.talents = FakeColl()
        self.whatsapp_inbound_messages = FakeColl()
        self.whatsapp_agent_audit_log = FakeColl()
        self.casting_pipeline = FakeColl()

    def __getitem__(self, name):
        if not hasattr(self, name):
            setattr(self, name, FakeColl())
        return getattr(self, name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _two_configs_same_agent(agent_id: str, group_name_default: str, group_name_w2: str):
    """Same shape both fixed paths need: one config for 'default', one for
    'worker-2', same agent_id, deliberately different group_names — so a
    test can prove the RIGHT one was read, not just A doc."""
    return [
        {"agent_id": agent_id, "worker_id": "default", "group_names": [group_name_default],
         "allowed_senders": [], "active": True},
        {"agent_id": agent_id, "worker_id": "worker-2", "group_names": [group_name_w2],
         "allowed_senders": [], "active": True},
    ]


def test_inbound_messages_resolve_group():
    import inbound_messages as ibm

    ibm.db = FakeDB()
    ibm.db.whatsapp_agent_config.docs = _two_configs_same_agent(
        "whatsapp-campaign-agent", "Talentgram Scouting Agent", "Talentgram Scouting Agent 2"
    )

    # Worker 1's own group resolves as "registered" when asked as worker 1...
    pid, kind, hint = run(ibm.resolve_group("Talentgram Scouting Agent", "default"))
    assert kind == "registered", (pid, kind, hint)
    # ...but is INVISIBLE to worker-2's lookup (it's not worker-2's group).
    pid2, kind2, hint2 = run(ibm.resolve_group("Talentgram Scouting Agent", "worker-2"))
    assert kind2 == "unknown", (pid2, kind2, hint2)

    # And the reverse: worker-2's own group is invisible to worker 1.
    pid3, kind3, _ = run(ibm.resolve_group("Talentgram Scouting Agent 2", "worker-2"))
    assert kind3 == "registered", (pid3, kind3)
    pid4, kind4, _ = run(ibm.resolve_group("Talentgram Scouting Agent 2", "default"))
    assert kind4 == "unknown", (pid4, kind4)

    # Default worker_id (no arg) preserves existing callers' behavior —
    # matches the "default" config, not worker-2's.
    pid5, kind5, _ = run(ibm.resolve_group("Talentgram Scouting Agent"))
    assert kind5 == "registered"
    print("1. inbound_messages.resolve_group worker isolation OK")


def test_inbound_capture_threads_worker_id():
    import inbound_messages as ibm

    ibm.db = FakeDB()
    ibm.db.whatsapp_agent_config.docs = _two_configs_same_agent(
        "whatsapp-campaign-agent", "Group A", "Group B"
    )
    os.environ["SA_INBOUND_CAPTURE_ENABLED"] = "true"
    try:
        doc_w2 = run(ibm.capture_inbound(
            message_id="m1", sender_phone="+919999999999", text="hi",
            group_name="Group B", worker_id="worker-2",
        ))
        assert doc_w2["group_kind"] == "registered", doc_w2
        assert doc_w2["worker_id"] == "worker-2", doc_w2

        doc_w1_sees_w2_group = run(ibm.capture_inbound(
            message_id="m2", sender_phone="+919999999999", text="hi again",
            group_name="Group B", worker_id="default",
        ))
        # Worker 1 asking about Worker 2's group must NOT resolve it as
        # "registered" — this is the actual bug the fix closes.
        assert doc_w1_sees_w2_group["group_kind"] == "unknown", doc_w1_sees_w2_group
    finally:
        os.environ.pop("SA_INBOUND_CAPTURE_ENABLED", None)
    print("2. capture_inbound threads worker_id into resolve_group OK")


def test_production_reminder_worker_management_agent_group():
    import services.production_reminder_worker as prw

    prw.db = FakeDB()
    prw.db.whatsapp_agent_config = FakeColl(_two_configs_same_agent(
        "management-agent", "Talentgram Management Agent", "Talentgram Management Agent 2"
    ))

    group = run(prw._management_agent_group())
    assert group == "Talentgram Management Agent", group  # Worker 1's group, never worker-2's
    print("3. production_reminder_worker._management_agent_group targets Worker 1 only OK")


def test_production_reminder_daily_briefing_targets_correct_doc():
    import services.production_reminder_worker as prw

    prw.db = FakeDB()
    default_cfg = {"agent_id": "management-agent", "worker_id": "default", "group_names": ["G1"],
                   "allowed_senders": [], "active": True, "last_daily_briefing_date": None}
    w2_cfg = {"agent_id": "management-agent", "worker_id": "worker-2", "group_names": ["G2"],
              "allowed_senders": [], "active": True, "last_daily_briefing_date": "2026-09-12"}
    prw.db.whatsapp_agent_config = FakeColl([default_cfg, w2_cfg])

    async def _fake_daily_briefing_text():
        return "Good morning"

    async def _fake_send_reminder(text):
        return True

    prw._daily_briefing_text = _fake_daily_briefing_text
    prw._send_reminder = _fake_send_reminder

    # Force past the hour gate.
    real_datetime = prw.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 9, 13, 12, 0, tzinfo=tz)

    prw.datetime = _FrozenDatetime
    try:
        sent = run(prw._maybe_send_daily_briefing())
        assert sent == 1
        # ONLY the default-worker doc's last_daily_briefing_date changed —
        # worker-2's own config (a different agent-config doc entirely)
        # must be completely untouched by this claim.
        assert default_cfg["last_daily_briefing_date"] == "2026-09-13", default_cfg
        assert w2_cfg["last_daily_briefing_date"] == "2026-09-12", w2_cfg  # unchanged
    finally:
        prw.datetime = real_datetime
    print("4. daily briefing claim targets Worker 1's doc only, worker-2's untouched OK")


def test_media_assignment_worker_send_report_picks_correct_worker_group():
    import services.media_assignment_worker as maw

    maw.db = FakeDB()
    maw.db.whatsapp_agent_config = FakeColl(_two_configs_same_agent(
        "whatsapp-campaign-agent", "Casting Group Default", "Casting Group Worker2"
    ))
    maw.db.whatsapp_templates = FakeColl([{"id": "tpl1", "slug": "custom"}])

    captured_batches = []

    async def _fake_create_batch(batch_in, admin):
        captured_batches.append(batch_in)

    async def _fake_service_admin():
        return {"id": "admin-1", "name": "Admin", "email": "a@b.com"}

    maw.create_batch = _fake_create_batch
    maw._service_admin = _fake_service_admin

    run(maw._send_report("Upload complete", worker_id="worker-2"))
    assert len(captured_batches) == 1
    sent = captured_batches[0]
    assert sent.worker_id == "worker-2", sent.worker_id
    assert sent.source_params.contacts[0].whatsapp_group_name == "Casting Group Worker2", sent

    captured_batches.clear()
    run(maw._send_report("Upload complete", worker_id="default"))
    assert len(captured_batches) == 1
    sent2 = captured_batches[0]
    assert sent2.worker_id == "default"
    assert sent2.source_params.contacts[0].whatsapp_group_name == "Casting Group Default", sent2
    print("5. media_assignment_worker._send_report scopes both the group lookup AND the send OK")


def main():
    test_inbound_messages_resolve_group()
    test_inbound_capture_threads_worker_id()
    test_production_reminder_worker_management_agent_group()
    test_production_reminder_daily_briefing_targets_correct_doc()
    test_media_assignment_worker_send_report_picks_correct_worker_group()
    print("\nALL WORKER-SCOPED RAW-QUERY TESTS PASSED")


if __name__ == "__main__":
    main()
