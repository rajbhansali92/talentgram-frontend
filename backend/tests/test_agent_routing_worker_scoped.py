"""Multi-Worker Phase 2 — worker-scoped agent-group routing tests.

Covers: registry.resolve_agent_for_group/get_agent_config/seed_agent_config
all becoming (agent_id, worker_id)-keyed instead of agent_id-alone, with a
worker_id default that preserves every existing (single-worker) caller's
behavior byte-for-byte, and legacy documents (no worker_id field at all)
still resolving correctly for the default worker only. No live DB.

Run:  python backend/tests/test_agent_routing_worker_scoped.py
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
from agents import registry  # noqa: E402


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

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDB:
    def __init__(self):
        self.whatsapp_agent_config = FakeColl()

    def __getitem__(self, name):
        return getattr(self, name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main():
    registry.db = FakeDB()
    registry._AGENTS.clear()
    from agents.models import AgentDefinition
    registry.register_agent(AgentDefinition(agent_id="casting-agent", name="Casting", module="x", intents=[]))
    registry.register_agent(AgentDefinition(agent_id="crm-agent", name="CRM", module="x", intents=[]))

    GROUP = "Talentgram Casting Pipeline"

    # 1. Two DIFFERENT workers, each with an agent mapped to the SAME group
    #    display name — must never cross-resolve.
    run(registry.seed_agent_config("casting-agent", group_names=[GROUP], worker_id="default"))
    run(registry.seed_agent_config("crm-agent", group_names=[GROUP], worker_id="wa-worker-2"))

    resolved_default = run(registry.resolve_agent_for_group(GROUP, "default"))
    resolved_w2 = run(registry.resolve_agent_for_group(GROUP, "wa-worker-2"))
    assert resolved_default is not None and resolved_default[0].agent_id == "casting-agent"
    assert resolved_w2 is not None and resolved_w2[0].agent_id == "crm-agent"
    print("1. same group name, different workers -> no cross-routing OK")

    # 2. resolve_agent_for_group with NO worker_id arg defaults to "default"
    #    — every pre-existing production/test call site is unaffected.
    resolved_implicit = run(registry.resolve_agent_for_group(GROUP))
    assert resolved_implicit[0].agent_id == "casting-agent"
    print("2. worker_id default preserves existing call sites OK")

    # 3. Legacy doc (predates worker_id entirely, field truly absent) still
    #    resolves for the DEFAULT worker, but never for a different one.
    registry.db.whatsapp_agent_config.docs.append({
        "agent_id": "crm-agent", "group_names": ["Legacy Group"],
        "allowed_senders": [], "active": True,
        # no "worker_id" key at all — simulates a pre-migration document
    })
    legacy_default = run(registry.resolve_agent_for_group("Legacy Group", "default"))
    legacy_w2 = run(registry.resolve_agent_for_group("Legacy Group", "wa-worker-2"))
    assert legacy_default is not None and legacy_default[0].agent_id == "crm-agent"
    assert legacy_w2 is None
    print("3. legacy (fieldless) doc resolves only for default worker OK")

    # 4. seed_agent_config is idempotent PER WORKER, but a different
    #    worker_id for the SAME agent_id creates a second, independent doc
    #    (agent_id alone is no longer the key).
    before = len(registry.db.whatsapp_agent_config.docs)
    run(registry.seed_agent_config("casting-agent", group_names=[GROUP], worker_id="default"))  # no-op
    assert len(registry.db.whatsapp_agent_config.docs) == before
    run(registry.seed_agent_config("casting-agent", group_names=["Worker 2 Casting"], worker_id="wa-worker-2"))
    assert len(registry.db.whatsapp_agent_config.docs) == before + 1
    cfg_default = run(registry.get_agent_config("casting-agent", "default"))
    cfg_w2 = run(registry.get_agent_config("casting-agent", "wa-worker-2"))
    assert cfg_default["group_names"] == [GROUP]
    assert cfg_w2["group_names"] == ["Worker 2 Casting"]
    print("4. per-worker config docs coexist for the same agent_id OK")

    print("\nALL WORKER-SCOPED AGENT ROUTING TESTS PASSED")


if __name__ == "__main__":
    main()
