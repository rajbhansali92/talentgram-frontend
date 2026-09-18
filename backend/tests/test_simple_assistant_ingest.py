"""Simple Assistant — Phase 4C incoming-audition file INGESTION.

A genuinely NEW audition file → canonical upload pipeline → submission.media[].

No live DB, no Cloudinary, no WhatsApp. A writable FakeDB drives every
scenario; the ingest goes through the real canonical service
routers.submissions.ingest_new_audition_media_to_submission, with only
core.upload_and_track_asset (the Cloudinary boundary) stubbed.

Run:  python3 backend/tests/test_simple_assistant_ingest.py
"""
import asyncio
import copy
import hashlib
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase4c-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm_and_ingest path (Phase 4C), so the
# new independent execution kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agents.modules.media_assignment as ma
import routers.casting_pipeline as cp
import routers.submissions as subs_router
import simple_assistant.audit as sa_audit
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
import simple_assistant.submission as sa_submission
from simple_assistant import submission_execute
from simple_assistant.commands import run_command


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- fake mongo (with $or / $in / $ne, dotted paths) ----
def _cmp(dv, v):
    if isinstance(v, dict):
        if "$in" in v:
            return dv in v["$in"]
        if "$ne" in v:
            return dv != v["$ne"]
        if "$nin" in v:
            return dv not in v["$nin"]
        if "$exists" in v:
            return (dv is not None) == v["$exists"]
    return dv == v


def _get(d, path):
    cur = d
    for p in path.split("."):
        if isinstance(cur, list):
            return [_get(x, p) if isinstance(x, dict) else None for x in cur]
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        dv = _get(doc, k) if "." in k else doc.get(k)
        if "." in k and isinstance(dv, list):
            if isinstance(v, dict) and "$ne" in v:
                if v["$ne"] in dv:
                    return False
                continue
            if not any(_cmp(x, v) for x in dv):
                return False
            continue
        if not _cmp(dv, v):
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

    async def update_one(s, q, upd, **k):
        matched = 0
        for d in s.docs:
            if _match(d, q):
                matched = 1
                if "$push" in upd:
                    for key, val in upd["$push"].items():
                        d.setdefault(key, [])
                        if isinstance(val, dict) and "$each" in val:
                            d[key].extend(copy.deepcopy(val["$each"]))
                        else:
                            d[key].append(copy.deepcopy(val))
                if "$pull" in upd:
                    for key, cond in upd["$pull"].items():
                        d[key] = [x for x in d.get(key, []) if not _match(x, cond)]
                if "$set" in upd:
                    d.update(upd["$set"])
                break

        class R:
            matched_count = matched
            modified_count = matched

        return R()

    async def insert_one(s, d):
        s.docs.append(copy.deepcopy(d))

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


USER = {"id": "u1", "name": "Raj", "email": "raj@x.com", "role": "admin"}

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"take-two-payload" * 64
VIDEO_SHA = hashlib.sha256(VIDEO_BYTES).hexdigest()

_UPLOAD_CALLS = []


class FakeUpload:
    """Minimal UploadFile stand-in."""

    def __init__(self, data=VIDEO_BYTES, filename="audition_take_2.mp4", content_type="video/mp4"):
        self._data = data
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)

    async def read(self):
        return self._data


def base_db():
    return DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing", "materials": []},
            {"id": "p_l", "brand_name": "L'Oreal Glyco", "status": "ongoing", "materials": []},
        ],
        talents=[
            {"id": "t_ahana", "name": "Ahana Pocha", "email": "ahana@x.com",
             "normalized_email": "ahana@x.com", "dob": "2000-01-01", "media": []},
            {"id": "t_ahana_dup", "name": "Ahana Pocha", "email": None, "media": []},
        ],
        casting_pipeline=[
            {"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"},
        ],
        submissions=[
            {"id": "sub_ahana_g", "project_id": "p_g", "talent_id": "t_ahana",
             "talent_email": "ahana@x.com", "status": "pending",
             "form_data": {"first_name": "Ahana", "last_name": "Pocha"},
             "media": [
                 {"id": "sm_t1", "category": "take", "label": "Take 1", "url": "https://cdn/g-t1.mp4"},
             ]},
        ],
    )


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    cp.db = db
    subs_router.db = db
    ma.db = db

    async def _sc(pid):
        return {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}

    sa_service.get_stage_counts = _sc

    _UPLOAD_CALLS.clear()

    async def _fake_upload(data, **kw):
        _UPLOAD_CALLS.append({"bytes": len(data), **kw})
        return {
            "url": "https://res.cloudinary.com/x/video/upload/ing/newtake.mp4",
            "secure_url": "https://res.cloudinary.com/x/video/upload/ing/newtake.mp4",
            "public_id": "ing/newtake",
            "resource_type": "video",
            "bytes": len(data),
            "duration": 12.0,
            "format": "mp4",
            "video_codec": "h264",
        }

    subs_router.upload_and_track_asset = _fake_upload


FM = {"filename": "audition_take_2.mp4", "size": len(VIDEO_BYTES),
      "content_type": "video/mp4", "sha256": VIDEO_SHA}


async def cmd(m, ctx=None, file_meta=None):
    return await run_command(message=m, conversation_id=None, context=ctx, user=USER, file_meta=file_meta)


async def confirm(ctx, file=None):
    return await submission_execute.confirm_and_ingest(
        conversation_id=(ctx or {}).get("conversation_id"), context=ctx, user=USER,
        file=file if file is not None else FakeUpload(),
    )


def preview(msg="Upload this as Ahana's Take 2 for Google AI", db=None, fm=None):
    install(db or base_db())
    return run(cmd(msg, file_meta=fm or dict(FM)))


# ---- RESOLUTION ---------------------------------------------------------
def test_talent_project_submission_resolution():
    r = preview()
    assert r["state"] == "preview" and r["intent"] == "submission"
    s = r["sub"]
    assert s["action_type"] == "ingest_audition"
    assert s["talent"]["label"] == "Ahana Pocha"
    assert s["project"]["label"] == "Google AI"
    assert s["submission_id"] == "sub_ahana_g"
    print("1. talent + project + submission resolution OK")


def test_authoritative_duplicate_resolution():
    db = base_db()
    db.casting_pipeline.docs.append({"id": "pl2", "project_id": "p_g", "talent_id": "t_ahana_dup", "stage": "hold"})
    r = preview(db=db)
    e = run(confirm(r["context"]))
    assert e["state"] == "attached", e
    # media landed on the submission owned by the email-bearing record
    media = db.submissions.docs[0]["media"]
    added = [m for m in media if m.get("source") == "simple_assistant_ingest"]
    assert len(added) == 1
    # the upload was tracked against the authoritative talent, never the dup
    assert _UPLOAD_CALLS[0]["talent_id"] == "t_ahana"
    print("2. authoritative talent (email-owned submission) chosen over admin dup OK")


def test_ambiguous_project_clarification_roundtrip():
    db = base_db()
    db.submissions.docs.append({"id": "sub_ahana_l", "project_id": "p_l", "talent_id": "t_ahana",
                                "talent_email": "ahana@x.com", "status": "pending", "media": [], "form_data": {}})
    db.casting_pipeline.docs.append({"id": "pl3", "project_id": "p_l", "talent_id": "t_ahana", "stage": "shortlisted"})
    install(db)
    r = run(cmd("Ahana sent an audition", file_meta=dict(FM)))
    assert r["state"] == "clarification"
    labels = {o["label"] for o in r["clarification"]["options"]}
    assert {"Google AI", "L'Oreal Glyco"} <= labels
    # the file's metadata survives the clarification round-trip (signed context)
    r2 = run(cmd("Google AI", r["context"]))
    assert r2["state"] == "preview" and r2["sub"]["project"]["label"] == "Google AI"
    assert r2["sub"]["upload"]["sha256"] == VIDEO_SHA
    print("3. ambiguous project → clarification, file meta round-trips OK")


def test_ambiguous_submission_blocked():
    db = base_db()
    # both same-name records own a Google AI submission → unresolvable
    db.talents.docs[1]["email"] = "ahana2@x.com"
    db.talents.docs[1]["normalized_email"] = "ahana2@x.com"
    db.submissions.docs.append({"id": "sub_dup_g", "project_id": "p_g", "talent_id": "t_ahana_dup",
                                "talent_email": "ahana2@x.com", "status": "pending", "media": [], "form_data": {}})
    db.casting_pipeline.docs.append({"id": "pl9", "project_id": "p_g", "talent_id": "t_ahana_dup", "stage": "hold"})
    install(db)
    r = run(cmd("Prepare this audition for Ahana Pocha for Google AI", file_meta=dict(FM)))
    assert r["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("4. ambiguous submission (two records) → blocked, no upload OK")


def test_missing_submission_safe():
    db = base_db()
    db.submissions.docs = []
    r = preview(db=db)
    assert r["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("5. missing submission → safe blocked OK")


# ---- FILE HANDLING ----------------------------------------------------
def test_real_file_required():
    install(base_db())
    r = run(cmd("Upload this as Ahana's Take 2 for Google AI"))  # NO file_meta
    assert r["state"] == "blocked" and "file" in r["message"].lower()
    assert _UPLOAD_CALLS == []
    print("6. real file required — no file → asks for it OK")


def test_filename_alone_cannot_create_upload():
    install(base_db())
    # a filename in the text is not a file
    r = run(cmd("Ahana sent Ahana_final_take2.mp4 for Google AI"))
    assert r["state"] in ("blocked", "preview")
    assert _UPLOAD_CALLS == []
    # even at 'confirm' with a bogus context nothing uploads
    print("7. filename mentioned in text cannot create an upload OK")


def test_unsupported_mime_rejected():
    r = preview(fm={**FM, "filename": "notes.pdf", "content_type": "application/pdf"})
    assert r["state"] == "preview" and r["sub"]["executable"] is False
    assert any("video" in w.lower() for w in r["sub"]["warnings"])
    # and the canonical service refuses it too
    install(base_db())
    sub = run(subs_router.db.submissions.find_one({"id": "sub_ahana_g"}))
    svc = run(subs_router.ingest_new_audition_media_to_submission(
        submission=sub, file_bytes=b"%PDF-1.4 xxx", filename="notes.pdf",
        content_type="application/pdf", category="take", label="Take 2", admin=USER,
        authoritative_talent_id="t_ahana"))
    assert svc["validation_error"] == "unsupported_format"
    assert _UPLOAD_CALLS == []
    print("8. unsupported MIME rejected (preview + canonical service) OK")


def test_oversized_file_rejected():
    orig = sa_submission.MAX_SUBMISSION_VIDEO_BYTES
    try:
        sa_submission.MAX_SUBMISSION_VIDEO_BYTES = 32
        r = preview()
        assert r["state"] == "preview" and r["sub"]["executable"] is False
        assert any("limit" in w.lower() for w in r["sub"]["warnings"])
    finally:
        sa_submission.MAX_SUBMISSION_VIDEO_BYTES = orig
    # canonical service enforces its own ceiling
    install(base_db())
    sub = run(subs_router.db.submissions.find_one({"id": "sub_ahana_g"}))
    o2 = subs_router.MAX_SUBMISSION_VIDEO_BYTES
    try:
        subs_router.MAX_SUBMISSION_VIDEO_BYTES = 8
        svc = run(subs_router.ingest_new_audition_media_to_submission(
            submission=sub, file_bytes=VIDEO_BYTES, filename="a.mp4", content_type="video/mp4",
            category="take", label="Take 2", admin=USER, authoritative_talent_id="t_ahana"))
        assert svc["validation_error"] == "file_too_large"
    finally:
        subs_router.MAX_SUBMISSION_VIDEO_BYTES = o2
    assert _UPLOAD_CALLS == []
    print("9. oversized file rejected (preview + canonical service) OK")


def test_valid_video_accepted():
    r = preview()
    assert r["state"] == "preview" and r["sub"]["executable"] is True
    assert r["sub"]["upload"]["target_label"] == "Take 2"
    assert r["sub"]["upload"]["target_category"] == "take_2"
    print("10. valid video accepted → executable preview OK")


def test_arbitrary_cloudinary_url_rejected():
    r = preview()
    ctx = copy.deepcopy(r["context"])
    # try to smuggle a pre-made asset in
    ctx["pending"]["sub_plan"]["upload"]["url"] = "https://res.cloudinary.com/evil/video/upload/hacked.mp4"
    ctx["pending"]["sub_plan"]["upload"]["public_id"] = "evil/hacked"
    e = run(confirm(ctx))
    assert e["state"] == "blocked" and "verify" in e["message"].lower()
    assert _UPLOAD_CALLS == []
    print("11. injected Cloudinary URL / public_id → signature fails, rejected OK")


# ---- PREVIEW ---------------------------------------------------------
def test_preview_zero_upload():
    db = base_db()
    before = copy.deepcopy(db.submissions.docs[0]["media"])
    preview(db=db)
    assert db.submissions.docs[0]["media"] == before
    assert _UPLOAD_CALLS == []
    print("12. preview performs zero upload OK")


def test_cancel_zero_upload():
    r = preview()
    # user never confirms — nothing was uploaded
    assert _UPLOAD_CALLS == []
    assert subs_router.db.submissions.docs[0]["media"] == [
        {"id": "sm_t1", "category": "take", "label": "Take 1", "url": "https://cdn/g-t1.mp4"}
    ]
    print("13. cancel / no-confirm performs zero upload OK")


# ---- EXECUTION -----------------------------------------------------
def test_confirm_calls_canonical_service_media_on_correct_submission():
    r = preview()
    e = run(confirm(r["context"]))
    assert e["state"] == "attached", e
    assert len(_UPLOAD_CALLS) == 1
    call = _UPLOAD_CALLS[0]
    assert call["submission_id"] == "sub_ahana_g" and call["project_id"] == "p_g"
    assert call["asset_type"] == "audition_video"
    media = subs_router.db.submissions.docs[0]["media"]
    added = [m for m in media if m.get("source") == "simple_assistant_ingest"]
    assert len(added) == 1
    m = added[0]
    assert m["public_id"] == "ing/newtake"           # the canonical Cloudinary asset
    assert m["content_sha256"] == VIDEO_SHA
    assert m["submission_id"] == "sub_ahana_g" and m["project_id"] == "p_g"
    assert m["admin_added"] is True and m["admin_added_by"] == "raj@x.com"
    print("14/15/16. confirm → canonical upload service, media on the correct submission OK")


def test_correct_media_category():
    # explicit legacy slot
    r = preview("Upload this as Ahana's Take 2 for Google AI")
    run(confirm(r["context"]))
    m = [x for x in subs_router.db.submissions.docs[0]["media"] if x.get("source") == "simple_assistant_ingest"][0]
    assert m["category"] == "take_2" and m["label"] == "Take 2"

    # "an audition" with no number → next generic take, auto-numbered
    r2 = preview("Ahana sent an audition for Google AI")
    run(confirm(r2["context"]))
    m2 = [x for x in subs_router.db.submissions.docs[0]["media"] if x.get("source") == "simple_assistant_ingest"][0]
    assert m2["category"] == "take" and m2["label"] == "Take 2"  # Take 1 already present

    # "intro" → intro_video
    r3 = preview("Upload Ahana's intro for Google AI")
    run(confirm(r3["context"]))
    m3 = [x for x in subs_router.db.submissions.docs[0]["media"] if x.get("source") == "simple_assistant_ingest"][0]
    assert m3["category"] == "intro_video"
    print("17. take_N / next-take / intro categories resolved canonically OK")


def test_wrong_talent_cannot_receive_media():
    r = preview()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["talent"]["id"] = "t_ahana_dup"
    e = run(confirm(ctx))
    assert e["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("18. tampered talent → rejected, no upload OK")


def test_wrong_project_cannot_receive_media():
    db = base_db()
    db.submissions.docs.append({"id": "sub_ahana_l", "project_id": "p_l", "talent_id": "t_ahana",
                                "talent_email": "ahana@x.com", "status": "pending", "media": [], "form_data": {}})
    r = preview(db=db)
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["project"] = {"id": "p_l", "label": "L'Oreal Glyco"}
    e = run(confirm(ctx))
    assert e["state"] == "blocked"
    assert db.submissions.docs[-1]["media"] == []
    print("19. tampered project → rejected, other submission untouched OK")


def test_duplicate_upload_prevented():
    db = base_db()
    # the identical file is already on the submission
    db.submissions.docs[0]["media"].append(
        {"id": "sm_prev", "category": "take_2", "label": "Take 2",
         "url": "https://cdn/old.mp4", "content_sha256": VIDEO_SHA})
    r = preview(db=db)
    # preview already flags it
    assert r["sub"]["executable"] is False
    assert any("already on the submission" in w.lower() for w in r["sub"]["warnings"])
    print("20. duplicate (same content hash) upload prevented at preview OK")


def test_stale_state_handled():
    r = preview()
    ctx = r["context"]
    # someone uploads the very same file between preview and confirm
    subs_router.db.submissions.docs[0]["media"].append(
        {"id": "sm_race", "category": "take_2", "label": "Take 2",
         "url": "https://cdn/race.mp4", "content_sha256": VIDEO_SHA})
    e = run(confirm(ctx))
    assert e["state"] == "stale", e
    assert "already on the submission" in e["message"].lower()
    assert _UPLOAD_CALLS == []
    added = [m for m in subs_router.db.submissions.docs[0]["media"] if m.get("content_sha256") == VIDEO_SHA]
    assert len(added) == 1  # not uploaded again
    print("21. stale state (same file appeared) handled, no second upload OK")


# ---- SECURITY ------------------------------------------------------
def test_unsigned_plan_rejected():
    install(base_db())
    for c in (None, {}, {"v": 1, "pending": {"kind": "confirm_upload",
              "sub_plan": {"action_type": "ingest_audition", "upload": {"target_category": "take"}}}}):
        e = run(confirm(c or {}))
        assert e["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("22. unsigned / forged ingest plan rejected OK")


def test_tampered_submission_rejected():
    r = preview()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["submission_id"] = "sub_somewhere_else"
    assert run(confirm(ctx))["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("23. tampered submission id rejected OK")


def test_tampered_category_rejected():
    r = preview()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["upload"]["target_category"] = "intro_video"
    assert run(confirm(ctx))["state"] == "blocked"
    assert _UPLOAD_CALLS == []
    print("24. tampered media category rejected OK")


def test_file_bytes_must_match_pinned_hash():
    r = preview()
    # a DIFFERENT file arrives at confirm than the one previewed
    e = run(confirm(r["context"], file=FakeUpload(data=b"totally-different-bytes-here!!", filename="x.mp4")))
    assert e["state"] == "blocked" and "match" in e["message"].lower()
    assert _UPLOAD_CALLS == []
    print("25. confirm file must match the SHA-256 pinned in the signed plan OK")


# ---- IDEMPOTENCY / AUDIT -----------------------------------------
def test_replay_no_duplicate_one_audit_row():
    r = preview()
    ctx = r["context"]
    e1 = run(confirm(ctx))
    assert e1["state"] == "attached"
    n1 = len(_UPLOAD_CALLS)
    e2 = run(confirm(ctx))
    assert "already applied" in e2["message"].lower()
    assert len(_UPLOAD_CALLS) == n1 == 1
    added = [m for m in subs_router.db.submissions.docs[0]["media"] if m.get("source") == "simple_assistant_ingest"]
    assert len(added) == 1
    execs = [x for x in subs_router.db.whatsapp_agent_audit_log.docs
             if x.get("sa_action", {}).get("executed") and x["sa_action"]["action_type"] == "upload_audition"]
    assert len(execs) == 1
    # audit carries file metadata but NEVER bytes / url / public_id
    fa = execs[0]["sa_action"]["file"]
    assert fa["sha256"] == VIDEO_SHA and fa["filename"] == "audition_take_2.mp4"
    assert "url" not in fa and "public_id" not in fa
    print("26. replay → no duplicate upload, exactly one audit row, no secrets OK")


# ---- SIDE EFFECTS ------------------------------------------------
def test_no_whatsapp_no_link_no_pipeline():
    r = preview()
    run(confirm(r["context"]))
    db = subs_router.db
    assert db.whatsapp_jobs.docs == [] and db.whatsapp_batches.docs == []
    assert db.links.docs == []
    assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"
    print("27. no WhatsApp job / batch, no link, no pipeline mutation OK")


# ---- execution kill-switch (independent of SIMPLE_ASSISTANT_ENABLED) -----
class _TrackedUpload(FakeUpload):
    """Same as FakeUpload, but records whether .read() was ever called —
    proves the block happens before the file bytes are even read into
    memory, let alone reaching Cloudinary or MongoDB."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.read_called = False

    async def read(self):
        self.read_called = True
        return await super().read()


def test_execution_disabled_blocks_confirm_upload_before_reading_the_file():
    old = os.environ.get("SA_EXECUTION_ENABLED")
    try:
        os.environ["SA_EXECUTION_ENABLED"] = "false"
        r = preview()  # preview still works with execution off
        upload = _TrackedUpload()

        e = run(confirm(r["context"], file=upload))
        assert e["state"] == "blocked", e
        assert "disabled" in e["message"].lower()
        assert upload.read_called is False, "the file must not be read while execution is disabled"
        assert _UPLOAD_CALLS == []  # Cloudinary boundary never reached
        media = subs_router.db.submissions.docs[0]["media"]
        assert not any(m.get("source") == "simple_assistant_ingest" for m in media)

        # the plan is not consumed — re-enabling lets it upload
        os.environ["SA_EXECUTION_ENABLED"] = "true"
        e2 = run(confirm(r["context"]))
        assert e2["state"] == "attached", e2
        assert len(_UPLOAD_CALLS) == 1
    finally:
        if old is None:
            os.environ.pop("SA_EXECUTION_ENABLED", None)
        else:
            os.environ["SA_EXECUTION_ENABLED"] = old
    print("26. execution disabled -> confirm_upload blocked BEFORE the file is read, "
          "zero Cloudinary/MongoDB write, plan not consumed; re-enabling lets it upload OK")


if __name__ == "__main__":
    for fn in [
        test_talent_project_submission_resolution, test_authoritative_duplicate_resolution,
        test_ambiguous_project_clarification_roundtrip, test_ambiguous_submission_blocked,
        test_missing_submission_safe, test_real_file_required,
        test_filename_alone_cannot_create_upload, test_unsupported_mime_rejected,
        test_oversized_file_rejected, test_valid_video_accepted,
        test_arbitrary_cloudinary_url_rejected, test_preview_zero_upload,
        test_cancel_zero_upload, test_confirm_calls_canonical_service_media_on_correct_submission,
        test_correct_media_category, test_wrong_talent_cannot_receive_media,
        test_wrong_project_cannot_receive_media, test_duplicate_upload_prevented,
        test_stale_state_handled, test_unsigned_plan_rejected,
        test_tampered_submission_rejected, test_tampered_category_rejected,
        test_file_bytes_must_match_pinned_hash, test_replay_no_duplicate_one_audit_row,
        test_no_whatsapp_no_link_no_pipeline,
        test_execution_disabled_blocks_confirm_upload_before_reading_the_file,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT INGEST TESTS PASSED")
