"""Regression tests for the whatsapp_dom_snapshots TTL index (P1 retention fix).

Covers: the index is created with the configured expireAfterSeconds, and a
create_index failure (e.g. insufficient privileges, or the exact Atlas quota
OperationFailure seen in the 2026-07-25 outage) never propagates out of
worker startup.

Run:  MONGO_URL=mongodb://x python tests/test_dom_snapshot_ttl.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://x")

import worker  # noqa: E402
import config  # noqa: E402


class FakeCollection:
    def __init__(self, side_effect=None):
        self.calls = []
        self._side_effect = side_effect

    async def create_index(self, field, **kwargs):
        self.calls.append((field, kwargs))
        if self._side_effect:
            raise self._side_effect


class FakeDB:
    def __init__(self, coll):
        self._coll = coll

    def __getitem__(self, name):
        assert name == "whatsapp_dom_snapshots"
        return self._coll


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def main():
    # 1. Happy path — index created with the configured TTL, on the right field.
    coll = FakeCollection()
    worker.get_db = lambda: FakeDB(coll)
    run(worker._ensure_dom_snapshot_ttl_index())
    assert len(coll.calls) == 1
    field, kwargs = coll.calls[0]
    assert field == "created_at"
    assert kwargs.get("expireAfterSeconds") == config.DOM_SNAPSHOT_TTL_SEC
    print("1. happy path                   -> TTL index created on created_at")

    # 2. The exact production failure (Atlas quota OperationFailure) must not
    #    propagate — worker startup must survive it, same as it must for R2/
    #    Stream/session errors elsewhere in main().
    quota_err = Exception(
        "you are over your space quota, using 512 MB of 512 MB. "
        "Writes are blocked on your cluster."
    )
    coll = FakeCollection(side_effect=quota_err)
    worker.get_db = lambda: FakeDB(coll)
    run(worker._ensure_dom_snapshot_ttl_index())  # must not raise
    assert len(coll.calls) == 1
    print("2. write-blocked cluster        -> exception swallowed, startup unaffected")

    print("\nALL DOM-SNAPSHOT-TTL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
