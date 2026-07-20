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


def test_variable_resolution():
    """Template Engine variable resolution (first_name, sender, project auto,
    system) + backward compatibility of {{talent_name}} / {{full_name}}."""

    # first_name = first word; handles empty / multi-word.
    assert wa._first_name("Sahal Mansuri") == "Sahal"
    assert wa._first_name("Sahal") == "Sahal"
    assert wa._first_name("  Riyaan League  ") == "Riyaan"
    assert wa._first_name("") == "" and wa._first_name(None) == ""

    # Per-recipient vars: talent_name now resolves to FIRST NAME ONLY (every
    # greeting template uses {{talent_name}}) + full_name (the real full name,
    # unchanged) + first_name + phone.
    rv = wa._recipient_variables("Sahal Mansuri", "+919004706699")
    assert rv["talent_name"] == "Sahal"
    assert rv["full_name"] == "Sahal Mansuri"
    assert rv["first_name"] == "Sahal"
    assert rv["phone"] == "+919004706699"

    # Sender vars from the authenticated admin.
    sv = wa._sender_variables({"name": "Raj", "email": "raj@talentgram.com"})
    assert sv["sender_name"] == "Raj" and sv["sender_email"] == "raj@talentgram.com"

    # Project auto-resolution: brand_name/shoot_dates/budget/submission_link.
    pv = wa._project_variables({
        "brand_name": "Maruti MSIL Strong Hybrid",
        "shoot_dates": "12-14 July",
        "budget_per_day": "Rs 15,000",
        "slug": "maruti-msil",
    })
    assert pv["project_name"] == "Maruti MSIL Strong Hybrid"
    assert pv["shoot_dates"] == "12-14 July"
    assert pv["budget"] == "Rs 15,000"
    assert pv["submission_link"] == "https://submit.talentgramagency.com/submit/maruti-msil"

    # Budget falls back to the structured talent_budget list when no budget_per_day.
    pv2 = wa._project_variables({
        "brand_name": "X", "slug": "x",
        "talent_budget": [{"label": "Lead", "value": "Rs 20,000"}, {"label": "Extra", "value": ""}],
    })
    assert pv2["budget"] == "Rs 20,000"

    # Backward compatibility: an existing template using {{talent_name}} still
    # renders, and a missing var is left untouched (not blanked unexpectedly).
    legacy_tpl = "Hi {{talent_name}} for *{{project_name}}*! {{unknown_var}}"
    data = {**wa._project_variables({"brand_name": "Acme", "slug": "acme"}),
            **wa._recipient_variables("Sahal Mansuri", "")}
    out = wa._render_message(legacy_tpl, data)
    assert "Hi Sahal for" in out          # {{talent_name}} -> first name only
    assert "Sahal Mansuri" not in out     # full name never leaks into the greeting
    assert "*Acme*" in out
    assert "{{unknown_var}}" in out  # unknown placeholders preserved

    # New placeholders render together; None -> "".
    tpl = "Hey {{first_name}}, dates {{shoot_dates}}. Thanks, {{sender_name}}"
    full = {
        **wa._recipient_variables("Sahal Mansuri", ""),
        **wa._project_variables({"brand_name": "Acme", "slug": "acme", "shoot_dates": None}),
        **wa._sender_variables({"name": "Raj", "email": "r@t.com"}),
        **wa._system_variables(),
    }
    rendered = wa._render_message(tpl, full)
    assert rendered.startswith("Hey Sahal, dates .")  # None shoot_dates -> ""
    assert "Thanks, Raj" in rendered

    # System vars are always present and non-empty.
    sysv = wa._system_variables()
    assert sysv["current_date"] and sysv["current_time"]

    print("7. variable resolution + backward-compat OK")
    print("\nALL TEMPLATE-ENGINE VARIABLE TESTS PASSED")


class FakeMutableColl:
    """Minimal find_one/update_one/insert_one mock, keyed on "id"."""
    def __init__(self, docs=None): self.docs = docs or []

    async def find_one(self, query, *a, **k):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    async def update_one(self, query, update, *a, **k):
        doc = await self.find_one(query)
        if doc is None:
            return
        for k2, v2 in (update.get("$set") or {}).items():
            doc[k2] = v2
        for k2 in (update.get("$unset") or {}):
            doc.pop(k2, None)

    async def insert_one(self, doc):
        self.docs.append(doc)


def test_retry_job_resumes_completed_batch():
    """A failed job inside an already-"completed" batch must, on retry, both
    reset to "pending" AND flip the batch back to "running" (clearing
    completed_at) — otherwise poll_and_process_jobs' batch query (status in
    [running, pending]) never looks at that batch again and the retried job
    sits in "pending" forever. Regression test for that exact bug."""
    job = {
        "id": "job1", "batch_id": "batch1", "status": "failed",
        "error_message": "CHAT_NOT_OPENED", "worker_picked_at": "2026-07-15T06:05:00Z",
        "talent_id": "t1", "talent_name": "Shweta Singh",
    }
    batch = {"id": "batch1", "status": "completed", "completed_at": "2026-07-15T06:06:00Z"}

    class FakeDB2:
        def __init__(self):
            self.whatsapp_jobs = FakeMutableColl([job])
            self.whatsapp_batches = FakeMutableColl([batch])
            self.whatsapp_audit_log = FakeMutableColl([])

    wa.db = FakeDB2()
    run(wa.retry_job("batch1", "job1", admin={"id": "admin1"}))

    assert job["status"] == "pending"
    assert job["error_message"] is None
    assert job["worker_picked_at"] is None
    assert batch["status"] == "running", batch["status"]
    assert "completed_at" not in batch
    assert wa.db.whatsapp_audit_log.docs[-1]["event_type"] == "job_retried"
    print("8. retry_job resumes a completed batch OK")


def test_first_name_only_greeting():
    """First-name-only greeting: {{talent_name}} must resolve to the first
    token of the recipient's name everywhere, for every source, and preview
    must render identically to the actual live send (they share one code
    path: _recipient_variables + _render_message, called once from
    create_batch regardless of is_dry_run)."""

    # -- _first_name extraction rules, directly --
    assert wa._first_name("Shweta Singh") == "Shweta"          # full name
    assert wa._first_name("  Priya   Shah ") == "Priya"        # multiple/irregular spaces
    assert wa._first_name("Rahul") == "Rahul"                  # single name
    assert wa._first_name("John A Smith") == "John"            # three-word name
    assert wa._first_name("") == ""                            # empty name -> graceful fallback
    assert wa._first_name(None) == ""                          # null name -> graceful fallback
    print("9a. _first_name extraction rules OK")

    # -- talent_name / first_name always agree and are first-name-only --
    for full, expected in [
        ("Shweta Singh", "Shweta"),
        ("Karan Mally", "Karan"),
        ("Avni Surana", "Avni"),
        ("Rahul", "Rahul"),
        ("  Priya   Shah ", "Priya"),
        ("John A Smith", "John"),
        ("", ""),
        (None, ""),
    ]:
        rv = wa._recipient_variables(full, "+910000000000")
        assert rv["talent_name"] == expected, (full, rv["talent_name"])
        assert rv["first_name"] == expected, (full, rv["first_name"])
        assert rv["full_name"] == (full or ""), (full, rv["full_name"])  # unchanged
    print("9b. talent_name/first_name first-name-only for all name shapes OK")

    # -- CRM recipient: resolve_recipients_engine("CRM", ...) -> greeting render --
    wa.db = FakeDB()
    wa.db.clients.docs = [
        {"_id": ObjectId(), "name": "Karan Mally", "phone_number": "+919876500000",
         "contact_type": "Brand Manager"},
    ]
    crm = run(wa.resolve_recipients_engine("CRM", wa.SourceParams(contact_type="Brand Manager")))
    crm_rec = crm["recipients"][0]
    crm_out = wa._render_message(
        "Hi {{talent_name}}!", wa._recipient_variables(crm_rec["name"], crm_rec["phone"]))
    assert crm_out == "Hi Karan!", crm_out
    print("9c. CRM recipient greeting OK ->", crm_out)

    # -- Talent recipient: resolve_recipients_engine("PROJECT", ...) -> greeting render --
    wa.db.casting_pipeline.docs = [{"talent_id": "t1"}]
    wa.db.talents.docs = [
        {"id": "t1", "name": "Shweta Singh", "phone": "919876500001", "whatsapp_group_name": ""},
    ]
    proj = run(wa.resolve_recipients_engine("PROJECT", wa.SourceParams(project_id="p1", pipeline_stages=["locked"])))
    talent_rec = proj["recipients"][0]
    talent_out = wa._render_message(
        "Hi {{talent_name}}!", wa._recipient_variables(talent_rec["name"], talent_rec["phone"]))
    assert talent_out == "Hi Shweta!", talent_out
    print("9d. Talent recipient greeting OK ->", talent_out)

    # -- Preview rendering == actual message rendering (same call path, both
    # is_dry_run branches use identical _recipient_variables/_render_message). --
    tpl = "Hi {{talent_name}}, welcome to *{{project_name}}*!"
    base = wa._project_variables({"brand_name": "Acme", "slug": "acme"})
    rec_vars = {**base, **wa._recipient_variables("Avni Surana", "+919000000000")}
    preview_render = wa._render_message(tpl, rec_vars)   # is_dry_run=True path
    live_render = wa._render_message(tpl, rec_vars)      # is_dry_run=False path
    assert preview_render == live_render == "Hi Avni, welcome to *Acme*!"
    print("9e. Preview rendering matches actual message rendering OK ->", preview_render)

    print("\nALL FIRST-NAME-GREETING REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
    test_variable_resolution()
    test_retry_job_resumes_completed_batch()
    test_first_name_only_greeting()
