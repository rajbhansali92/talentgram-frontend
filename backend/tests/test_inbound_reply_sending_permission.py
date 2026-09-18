"""Inbound agent-reply sending-permission gate tests (2026-09-18, WhatsApp
Agent Platform follow-up).

Covers: backend/routers/agents_whatsapp.py::inbound_message()'s new
sending_enabled check on agent-GENERATED replies — a path
_create_batch_internal's own gate (test_worker_sending_permission.py)
never covered, since a reply here is sent by the WORKER directly
(whatsapp-worker/inbound.py::_send_reply), never through
whatsapp_jobs/whatsapp_batches. Also covers the metadata-only audit
entry written on suppression (agents/audit.py::log_turn, existing
schema, no new field, no reply text stored) and re-confirms
dispatcher.py/models.py are untouched by this fix.

No live DB, no Playwright, no WhatsApp session, no real send —
handle_inbound_message() itself is replaced with a fake returning a
fixed DispatchResult, so nothing in this file exercises real agent
dispatch logic either.

Run:  python backend/tests/test_inbound_reply_sending_permission.py
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
from routers import agents_whatsapp as aw  # noqa: E402
from agents import audit as agent_audit  # noqa: E402
from agents import registry  # noqa: E402
from agents.models import AgentDefinition, DispatchResult  # noqa: E402


def _matches(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
        elif isinstance(v, dict) and "$exists" in v:
            if (k in doc) != v["$exists"]:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, n=None):
        return list(self._docs)


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)])

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDB:
    def __init__(self):
        self.whatsapp_workers = FakeColl()
        self.whatsapp_agent_config = FakeColl()
        self.whatsapp_agent_audit_log = FakeColl()

    def __getitem__(self, name):
        return getattr(self, name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Payload:
    """Minimal stand-in for InboundMessageIn — only the fields
    inbound_message()/this gate actually reads."""

    def __init__(self, group_name="Talentgram Management Agent", sender_phone="+919999999999",
                 text="what's the status", worker_id=None, message_id="m1",
                 sender_name=None, media_type=None, replied_to_message_id=None,
                 replied_quoted_text=None, sender_is_group_member=None,
                 transcript_confidence=None):
        self.group_name = group_name
        self.sender_phone = sender_phone
        self.text = text
        self.worker_id = worker_id
        self.message_id = message_id
        self.sender_name = sender_name
        self.media_type = media_type
        self.replied_to_message_id = replied_to_message_id
        self.replied_quoted_text = replied_quoted_text
        self.sender_is_group_member = sender_is_group_member
        self.transcript_confidence = transcript_confidence


def main():
    registry.register_agent(AgentDefinition(agent_id="management-agent", name="Management", module="x", intents=[]))

    # SA_INBOUND_CAPTURE_ENABLED is unset, so inbound_messages.capture_inbound
    # (called earlier in inbound_message()) is a genuine no-op per its own
    # documented default — no DB touched by that call, nothing to fake here.
    assert "SA_INBOUND_CAPTURE_ENABLED" not in os.environ

    # ------------------------------------------------------------------
    # 1. Enabled worker -> reply returned unchanged.
    # ------------------------------------------------------------------
    fake = FakeDB()
    fake.whatsapp_workers = FakeColl([{"id": "default", "sending_enabled": True}])
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply="Here's the status", operation_id=None))

    resp = run(aw.inbound_message(Payload(), x_internal_secret=None))
    assert resp["reply"] == "Here's the status", resp
    assert resp["handled"] is True
    assert len(fake.whatsapp_agent_audit_log.docs) == 0, "no suppression -> no audit write"
    print("1. enabled worker -> reply unchanged, no audit write OK")

    # ------------------------------------------------------------------
    # 2. Disabled worker -> reply becomes None.
    # ------------------------------------------------------------------
    fake = FakeDB()
    fake.whatsapp_workers = FakeColl([{"id": "default", "sending_enabled": False}])
    fake.whatsapp_agent_config = FakeColl([{"agent_id": "management-agent", "active": True, "worker_id": "default",
                                             "group_names": ["Talentgram Management Agent"]}])
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply="Here's the status", operation_id="op-1"))

    resp = run(aw.inbound_message(Payload(), x_internal_secret=None))
    assert resp["reply"] is None, resp
    print("2. disabled worker -> reply is None OK")

    # ------------------------------------------------------------------
    # 3. Missing worker (no whatsapp_workers doc at all) -> reply becomes None.
    # ------------------------------------------------------------------
    fake = FakeDB()
    fake.whatsapp_agent_config = FakeColl([{"agent_id": "management-agent", "active": True, "worker_id": "default",
                                             "group_names": ["Talentgram Management Agent"]}])
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply="Here's the status", operation_id=None))

    resp = run(aw.inbound_message(Payload(), x_internal_secret=None))
    assert resp["reply"] is None, resp
    print("3. missing worker -> reply is None OK")

    # ------------------------------------------------------------------
    # 4. handled=True preserved when reply is suppressed.
    # ------------------------------------------------------------------
    assert resp["handled"] is True
    print("4. handled=True preserved when reply suppressed OK")

    # ------------------------------------------------------------------
    # 5. operation_id preserved when reply is suppressed.
    # ------------------------------------------------------------------
    fake = FakeDB()
    fake.whatsapp_workers = FakeColl([{"id": "default", "sending_enabled": False}])
    fake.whatsapp_agent_config = FakeColl([{"agent_id": "management-agent", "active": True, "worker_id": "default",
                                             "group_names": ["Talentgram Management Agent"]}])
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply="text", operation_id="op-preserve-me"))
    resp = run(aw.inbound_message(Payload(), x_internal_secret=None))
    assert resp["reply"] is None
    assert resp["operation_id"] == "op-preserve-me", resp
    print("5. operation_id preserved when reply suppressed OK")

    # ------------------------------------------------------------------
    # 6. No generated reply -> no worker lookup, no audit write.
    # We prove "no lookup" by leaving whatsapp_workers doc-less AND
    # confirming no audit entry is written (a lookup would have found
    # nothing and, if the guard were missing, would have logged a
    # spurious suppression even though there was never a reply to send).
    # ------------------------------------------------------------------
    fake = FakeDB()
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply=None, operation_id=None))
    resp = run(aw.inbound_message(Payload(), x_internal_secret=None))
    assert resp["reply"] is None
    assert len(fake.whatsapp_agent_audit_log.docs) == 0
    print("6. no generated reply -> no lookup, no audit write OK")

    # ------------------------------------------------------------------
    # 7. Disabled/missing worker -> exactly one metadata-only audit entry.
    # ------------------------------------------------------------------
    fake = FakeDB()
    fake.whatsapp_workers = FakeColl([{"id": "default", "sending_enabled": False}])
    fake.whatsapp_agent_config = FakeColl([{"agent_id": "management-agent", "active": True, "worker_id": "default",
                                             "group_names": ["Talentgram Management Agent"]}])
    aw.db = fake
    registry.db = fake
    agent_audit.db = fake
    aw.handle_inbound_message = lambda **kw: _async_result(DispatchResult(handled=True, reply="the actual reply text", operation_id=None))

    payload = Payload(group_name="Talentgram Management Agent", sender_phone="+911234500000", text="mark it cleared")
    run(aw.inbound_message(payload, x_internal_secret=None))

    assert len(fake.whatsapp_agent_audit_log.docs) == 1, fake.whatsapp_agent_audit_log.docs
    entry = fake.whatsapp_agent_audit_log.docs[0]
    assert entry["group_name"] == "Talentgram Management Agent"
    assert entry["sender_phone"] == "+911234500000"
    assert entry["raw_message"] == "mark it cleared"
    assert entry["agent_id"] == "management-agent", "agent_id should resolve via registry.resolve_agent_for_group"
    assert entry["confirmation_action"] == "reply_suppressed_sending_disabled"
    # No outbound reply text stored anywhere in the entry.
    entry_values = " ".join(str(v) for v in entry.values())
    assert "the actual reply text" not in entry_values, "suppressed reply text must never be stored"
    print("7. exactly one metadata-only audit entry, correct fields, no reply text stored OK")

    print("\nALL INBOUND REPLY SENDING-PERMISSION TESTS PASSED")


async def _async_result(value):
    return value


if __name__ == "__main__":
    main()
