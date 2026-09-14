"""Multi-Worker Phase 3 — worker.py/session.py parameterization tests.

Covers: config.WORKER_ID defaults to "default" (so the existing Worker 1
Railway service, which sets no WORKER_ID env var, is unaffected),
worker._worker_scope_filter()'s legacy-fallback shape, and
WhatsAppSession(worker_id=...) targeting the right whatsapp_sessions doc
id instead of the old hardcoded "default" literal. No live DB, no browser.

Run:  MONGO_URL=mongodb://x python tests/test_worker_scoped_queries.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import config  # noqa: E402
import worker  # noqa: E402
from session import WhatsAppSession  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main():
    # 1. config.WORKER_ID defaults to "default" when WORKER_ID is unset —
    # this is what makes deploying this code to Worker 1's EXISTING Railway
    # service (no new env var) a provable no-op.
    assert "WORKER_ID" not in os.environ
    assert config.WORKER_ID == "default"
    print("1. config.WORKER_ID defaults to 'default' when unset OK")

    # 2. _worker_scope_filter(): the default worker matches both an
    # explicit "default" AND a genuinely fieldless (pre-migration) document
    # — never a plain equality-only filter, or an in-flight production
    # batch from before this deploy would vanish from Worker 1's own queue.
    default_filter = worker._worker_scope_filter()
    assert default_filter == {
        "$or": [{"worker_id": "default"}, {"worker_id": {"$exists": False}}]
    }
    print("2. default worker_scope_filter includes legacy-fallback OK ->", default_filter)

    # 3. A non-default WORKER_ID (a second worker's own process) gets a
    # plain equality filter — it has no legacy documents to account for,
    # and critically must NEVER also match a fieldless (Worker 1's
    # pre-migration) document.
    config.WORKER_ID = "wa-worker-2"
    try:
        w2_filter = worker._worker_scope_filter()
        assert w2_filter == {"worker_id": "wa-worker-2"}
        print("3. non-default worker_scope_filter is a plain equality match OK ->", w2_filter)
    finally:
        config.WORKER_ID = "default"

    # 4. WhatsAppSession defaults its worker_id from config.WORKER_ID at
    # construction time (what worker.py's `WhatsAppSession(config.WORKER_ID)`
    # relies on), and an explicit worker_id always wins.
    s_default = WhatsAppSession()
    assert s_default.worker_id == "default"
    s_w2 = WhatsAppSession(worker_id="wa-worker-2")
    assert s_w2.worker_id == "wa-worker-2"
    print("4. WhatsAppSession worker_id defaulting + override OK")

    print("\nALL WORKER-SCOPED QUERY TESTS PASSED")


if __name__ == "__main__":
    main()
