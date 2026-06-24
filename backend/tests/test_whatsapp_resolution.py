"""Slice 1 tests — Unified Recipient Resolution Engine (Feature 6).

Covers: phone normalization, MANUAL (valid/invalid/dupe/exclude), PROJECT and
CRM resolution (mocked db), the unified recipient shape, and BatchIn v2
back-compat. No live DB.

Run:  python backend/tests/test_whatsapp_resolution.py
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
from bson import ObjectId  # noqa: E402
from routers import whatsapp as wa  # noqa: E402


class FakeCursor:
    def __init__(self, docs): self._docs = docs
    async def to_list(self, n=None): return list(self._docs)


class FakeColl:
    def __init__(self, docs=None): self.docs = docs or []
    def find(self, query=None, projection=None): return FakeCursor(self.docs)
    async def find_one(self, *a, **k): return self.docs[0] if self.docs else None


class FakeDB:
    def __init__(self):
        self.casting_pipeline = FakeColl()
        self.talents = FakeColl()
        self.clients = FakeColl()
        self.projects = FakeColl()


def run(coro): return asyncio.new_event_loop().run_until_complete(coro)


def main():
    # 1. phone normalization (Feature 5)
    assert wa._normalize_phone("+91 98765-43210") == "+919876543210"
    assert wa._normalize_phone("919004706699") == "919004706699"
    assert wa._normalize_phone("+971 50 123 4567") == "+971501234567"
    assert wa._normalize_phone("not a phone") is None
    assert wa._normalize_phone("123") is None            # too short
    assert wa._normalize_phone("") is None
    print("1. phone normalization OK")

    # 2. MANUAL: valid + invalid + duplicate (no DB)
    SP = wa.SourceParams
    params = SP(contacts=[
        wa.ManualContact(name="Rahul Sharma", phone="+919876543210"),
        wa.ManualContact(name="Priya Jain", phone="+919123456789"),
        wa.ManualContact(name="Dup Rahul", phone="+91 98765 43210"),  # same as #1 -> dedup
        wa.ManualContact(name="Bad", phone="xxxx"),                    # invalid
    ])
    res = run(wa.resolve_recipients_engine("MANUAL", params))
    assert res["counts"]["resolved"] == 2, res["counts"]          # 2 unique valid
    assert res["counts"]["sending"] == 2
    assert len(res["unresolvable"]) == 1 and res["unresolvable"][0]["reason"] == "Invalid phone number"
    r0 = res["recipients"][0]
    assert set(r0) >= {"name","phone","destination_type","destination","source","source_id","recipient_kind","recipient_id"}
    assert r0["source"] == "MANUAL" and r0["destination_type"] == "number"
    print("2. MANUAL valid/invalid/dupe OK ->", res["counts"])

    # 3. exclusion
    rid = res["recipients"][0]["recipient_id"]
    res2 = run(wa.resolve_recipients_engine("MANUAL", params, excluded_ids=[rid]))
    assert res2["counts"]["sending"] == 1 and res2["counts"]["excluded"] == 1
    print("3. exclusion OK ->", res2["counts"])

    # 4. PROJECT (mocked db): talent w/ phone -> number, talent w/ group -> group
    wa.db = FakeDB()
    wa.db.casting_pipeline.docs = [{"talent_id": "t1"}, {"talent_id": "t2"}, {"talent_id": "t1"}]
    wa.db.talents.docs = [
        {"id": "t1", "name": "Ameya", "phone": "917208415717", "whatsapp_group_name": ""},
        {"id": "t2", "name": "Sahal", "phone": "919004706699", "whatsapp_group_name": "Jon x Talentgram Agency"},
    ]
    res = run(wa.resolve_recipients_engine("PROJECT", SP(project_id="p1", pipeline_stages=["locked"])))
    by = {r["name"]: r for r in res["recipients"]}
    assert by["Ameya"]["destination_type"] == "number" and by["Ameya"]["destination"] == "917208415717"
    assert by["Sahal"]["destination_type"] == "group" and by["Sahal"]["destination"] == "Jon x Talentgram Agency"
    assert by["Sahal"]["recipient_kind"] == "TALENT" and by["Sahal"]["source"] == "PROJECT"
    assert res["counts"]["sending"] == 2  # t1 dedup'd despite appearing twice
    print("4. PROJECT routing + dedup OK ->", res["counts"])

    # 5. CRM (mocked db): clients -> number route, source=CRM
    wa.db.clients.docs = [
        {"_id": ObjectId(), "name": "Rahul Sharma", "phone_number": "+919876543210", "contact_type": "Brand Manager"},
        {"_id": ObjectId(), "name": "No Phone", "phone_number": "", "contact_type": "Brand Manager"},
    ]
    res = run(wa.resolve_recipients_engine("CRM", SP(contact_type="Brand Manager")))
    assert res["counts"]["sending"] == 1 and len(res["unresolvable"]) == 1
    assert res["recipients"][0]["source"] == "CRM" and res["recipients"][0]["recipient_kind"] == "CRM_CLIENT"
    print("5. CRM resolution OK ->", res["counts"])

    # 6. BatchIn v2 back-compat: legacy project_id -> PROJECT source
    legacy = wa.BatchIn(project_id="p1", pipeline_stages=["locked"], template_id="tpl1")
    st, prm = wa._batch_source(legacy)
    assert st == "PROJECT" and prm.project_id == "p1" and prm.pipeline_stages == ["locked"]
    v2 = wa.BatchIn(source_type="CRM", source_params=SP(contact_type="Casting Director"), template_id="tpl1")
    st2, prm2 = wa._batch_source(v2)
    assert st2 == "CRM" and prm2.contact_type == "Casting Director"
    print("6. BatchIn v2 back-compat OK")

    print("\nALL SLICE-1 RESOLUTION ENGINE TESTS PASSED")


if __name__ == "__main__":
    main()
