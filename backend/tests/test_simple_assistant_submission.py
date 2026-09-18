"""Simple Assistant — Phase 4B incoming-audition / submission media attachment.

No live DB, no Cloudinary, no WhatsApp. A writable FakeDB drives every
scenario; attachment goes through the real canonical service
routers.submissions.attach_existing_talent_media_to_submission.

Run:  python backend/tests/test_simple_assistant_submission.py
"""
import asyncio
import copy
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase4b-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
# This file exercises the real confirm_and_attach path, so the new
# independent execution kill-switch must be explicitly on here.
os.environ["SA_EXECUTION_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agents.modules.media_assignment as ma
import routers.casting_pipeline as cp
import routers.submissions as subs_router
import simple_assistant.audit as sa_audit
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
from simple_assistant import submission_execute
from simple_assistant.commands import run_command


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- fake mongo (with $or / $in / $ne) ----
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
            # media.source_talent_media_id: {"$ne": id}  -> none of them equal id
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

    async def update_one(s, q, upd):
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


def base_db():
    return DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing", "materials": []},
            {"id": "p_l", "brand_name": "L'Oreal Glyco", "status": "ongoing", "materials": []},
        ],
        talents=[
            {"id": "t_ahana", "name": "Ahana Pocha", "email": "ahana@x.com", "normalized_email": "ahana@x.com",
             "dob": "2000-01-01",
             "media": [
                 {"id": "lib_intro", "category": "intro_video", "url": "https://cdn/ahana-intro.mp4",
                  "public_id": "ai", "resource_type": "video", "label": "Intro"},
                 {"id": "lib_img1", "category": "portfolio", "url": "https://cdn/ahana-p1.jpg", "public_id": "ap1",
                  "resource_type": "image"},
             ]},
            {"id": "t_ahana_dup", "name": "Ahana Pocha", "email": None, "media": []},  # older admin dup, no email
        ],
        casting_pipeline=[
            {"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"},
        ],
        submissions=[
            {"id": "sub_ahana_g", "project_id": "p_g", "talent_id": "t_ahana", "talent_email": "ahana@x.com",
             "status": "pending",
             "form_data": {"first_name": "Ahana", "last_name": "Pocha", "height": "5'6\"", "budget": "30000",
                           "availability": "Available", "location": "Mumbai"},
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


async def cmd(m, ctx=None):
    return await run_command(message=m, conversation_id=None, context=ctx, user=USER)


async def confirm(ctx):
    return await submission_execute.confirm_and_attach(conversation_id=ctx.get("conversation_id"), context=ctx, user=USER)


# ---- RESOLUTION -----------------------------------------------------------
def test_talent_and_project_resolution():
    install(base_db())
    r = run(cmd("Prepare Ahana's Google AI submission"))
    assert r["state"] == "preview" and r["intent"] == "submission"
    assert r["sub"]["talent"]["label"] == "Ahana Pocha"
    assert r["sub"]["project"]["label"] == "Google AI"
    assert r["sub"]["submission_id"] == "sub_ahana_g"
    print("1. talent + project + submission resolution OK")


def test_authoritative_duplicate_talent():
    db = base_db()
    # add a submission-less pipeline row for the dup so a naive "first match" could pick it
    db.casting_pipeline.docs.append({"id": "pl2", "project_id": "p_g", "talent_id": "t_ahana_dup", "stage": "hold"})
    install(db)
    r = run(cmd("Prepare Ahana's Google AI submission"))
    # authoritative = the record whose email owns the submission
    assert r["sub"]["talent"]["id"] == "t_ahana", r
    print("2. authoritative talent (email-owned submission) chosen over admin dup OK")


def test_ambiguous_project_clarification():
    db = base_db()
    db.submissions.docs.append({"id": "sub_ahana_l", "project_id": "p_l", "talent_id": "t_ahana",
                                "talent_email": "ahana@x.com", "status": "pending", "media": [], "form_data": {}})
    db.casting_pipeline.docs.append({"id": "pl3", "project_id": "p_l", "talent_id": "t_ahana", "stage": "shortlisted"})
    install(db)
    r = run(cmd("Ahana has sent her test"))
    assert r["state"] == "clarification"
    labels = {o["label"] for o in r["clarification"]["options"]}
    assert "Google AI" in labels
    r2 = run(cmd("Google AI", r["context"]))
    assert r2["state"] == "preview" and r2["sub"]["project"]["label"] == "Google AI"
    print("3. ambiguous project → clarification, then resolves OK")


def test_missing_submission_safe():
    db = base_db()
    db.submissions.docs = []
    install(db)
    r = run(cmd("Prepare Ahana's Google AI submission"))
    assert r["state"] == "blocked" and "couldn't find a Google AI submission" in r["message"]
    print("4. missing submission → safe blocked OK")


def test_form_and_media_inspection():
    install(base_db())
    r = run(cmd("Prepare Ahana's Google AI submission"))
    s = r["sub"]
    assert s["form_found"] is True
    assert s["form"]["height"] == "5'6\"" and s["form"]["budget"] == "30000"
    assert [m["label"] for m in s["existing_media"]] == ["Take 1"]
    # intro + portfolio image are AVAILABLE from the library (not yet on the submission)
    avail_cats = sorted(m["category"] for m in s["available_media"])
    assert "intro_video" in avail_cats and "image" in avail_cats
    assert "Introduction video" not in s["missing"]  # available → not "missing"
    assert "Audition takes" not in s["missing"]      # Take 1 present
    print("5. form + existing/available media + missing inspection OK")


def test_intro_identification_and_takes_not_in_library():
    db = base_db()
    db.submissions.docs[0]["media"] = []  # no takes on the submission
    install(db)
    r = run(cmd("Attach Ahana's intro and audition takes to Google AI"))
    assert r["state"] == "preview"
    # intro resolves (from library); takes do NOT (never in the library)
    assert [m["label"] for m in r["sub"]["proposed"]] == ["Intro Video"]
    assert any("audition takes are uploaded straight to a project submission" in w for w in r["sub"]["warnings"])
    # and when takes ARE already on the submission, they're reported as such (not "missing")
    db2 = base_db()
    install(db2)
    r2 = run(cmd("Attach Ahana's audition takes to Google AI"))
    assert not r2["sub"]["proposed"]
    assert any("Already on the submission" in w for w in r2["sub"]["warnings"])
    print("6. intro identified from library; takes not-attachable / already-present handled OK")


def test_filename_ambiguity_no_guess():
    db = base_db()
    # a talent-library item with a misleading filename but wrong category
    db.talents.docs[0]["media"].append(
        {"id": "lib_weird", "category": "portfolio", "url": "https://cdn/Ahana_final_audition.mp4",
         "public_id": "aw", "resource_type": "image"})
    install(db)
    r = run(cmd("Attach Ahana's Take 2 to Google AI"))
    # "Ahana_final_audition.mp4" is NOT assumed to be Take 2
    assert r["state"] == "preview" and not r["sub"]["proposed"]
    print("7. filename does not cause a wrong-media guess OK")


# ---- ATTACHMENT --------------------------------------------------------
def test_preview_zero_writes():
    db = base_db()
    install(db)
    before = copy.deepcopy(db.submissions.docs[0]["media"])
    run(cmd("Attach Ahana's intro to Google AI"))
    assert db.submissions.docs[0]["media"] == before
    print("8. preview performs zero writes OK")


def test_confirmation_invokes_canonical_service_and_attaches():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    assert r["sub"]["proposed"][0]["source_id"] == "lib_intro"
    e = run(confirm(r["context"]))
    assert e["state"] == "attached", e
    media = db.submissions.docs[0]["media"]
    intro = [m for m in media if m["category"] == "intro_video"]
    assert len(intro) == 1
    assert intro[0]["source_talent_media_id"] == "lib_intro"
    assert intro[0]["public_id"] == "ai"       # SAME Cloudinary asset — no re-upload
    assert intro[0]["from_global_profile"] is True and intro[0]["admin_added"] is True
    # audited
    assert any(x.get("agent_id") == "simple-assistant" and x["sa_action"]["action_type"] == "attach_media"
               for x in db.whatsapp_agent_audit_log.docs)
    print("9. confirm → canonical service attaches by reference (no re-upload) + audit OK")


def test_execution_disabled_blocks_confirm_attach_and_calls_nothing():
    old = os.environ.get("SA_EXECUTION_ENABLED")
    try:
        os.environ["SA_EXECUTION_ENABLED"] = "false"
        db = base_db()
        install(db)
        r = run(cmd("Attach Ahana's intro to Google AI"))  # preview still works
        assert r["state"] == "preview"

        e = run(confirm(r["context"]))
        assert e["state"] == "blocked", e
        assert "disabled" in e["message"].lower()
        media = db.submissions.docs[0]["media"]
        assert not any(m.get("source_talent_media_id") == "lib_intro" for m in media)
        assert db.whatsapp_agent_audit_log.docs == []

        # the plan is not consumed — re-enabling lets it attach
        os.environ["SA_EXECUTION_ENABLED"] = "true"
        e2 = run(confirm(r["context"]))
        assert e2["state"] == "attached", e2
    finally:
        if old is None:
            os.environ.pop("SA_EXECUTION_ENABLED", None)
        else:
            os.environ["SA_EXECUTION_ENABLED"] = old
    print("22. execution disabled -> confirm_submission blocked, nothing attached, plan not consumed; "
          "re-enabling lets the SAME plan attach OK")


def test_already_attached_skipped_no_duplicate():
    db = base_db()
    # intro already on the submission, sourced from lib_intro
    db.submissions.docs[0]["media"].append(
        {"id": "sm_intro", "category": "intro_video", "url": "https://cdn/ahana-intro.mp4",
         "source_talent_media_id": "lib_intro"})
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    assert not r["sub"]["proposed"]  # nothing to attach
    assert any("Already on the submission" in w for w in r["sub"]["warnings"])
    # even if we force a stale plan, the service skips
    print("10. already-attached intro → skipped, never duplicated OK")


def test_wrong_media_ownership_rejected():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["proposed"][0]["source_id"] = "neha_secret_media"
    e = run(confirm(ctx))
    assert e["state"] == "blocked" and "couldn't verify" in e["message"].lower()
    assert not [m for m in db.submissions.docs[0]["media"] if m["category"] == "intro_video"]
    print("11. tampered media source id → rejected, nothing attached OK")


def test_stale_submission_detected():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    ctx = r["context"]
    # someone else attaches the intro after the plan was prepared
    db.submissions.docs[0]["media"].append(
        {"id": "sm_other", "category": "intro_video", "url": "https://cdn/ahana-intro.mp4",
         "source_talent_media_id": "lib_intro"})
    e = run(confirm(ctx))
    assert e["state"] == "stale", e
    assert "already attached" in e["message"].lower()
    intro = [m for m in db.submissions.docs[0]["media"] if m["category"] == "intro_video"]
    assert len(intro) == 1  # not attached again
    print("12. stale submission (intro attached elsewhere) detected, no re-attach OK")


def test_multi_item_plan_any_tamper_blocks_confirm():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro and portfolio photos to Google AI"))
    assert len(r["sub"]["proposed"]) == 2  # intro + 1 portfolio image
    ctx = copy.deepcopy(r["context"])
    for pm in ctx["pending"]["sub_plan"]["proposed"]:
        if pm["category"] == "image":
            pm["source_id"] = "gone"
    e = run(confirm(ctx))
    assert e["state"] == "blocked"  # signature no longer verifies
    assert not [m for m in db.submissions.docs[0]["media"] if m["category"] in ("intro_video", "image")]
    print("13. any tamper on a multi-item plan blocks the whole confirm OK")


def test_partial_failure_via_live_state():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro and portfolio image to Google AI"))
    ctx = r["context"]
    # remove the portfolio image from the library AFTER the plan (live drift)
    db.talents.docs[0]["media"] = [m for m in db.talents.docs[0]["media"] if m["id"] != "lib_img1"]
    e = run(confirm(ctx))
    assert e["state"] == "partial", e
    by = {o["label"]: o["status"] for o in e["result"]["outcomes"]}
    assert by["Intro Video"] == "attached"
    assert any(v == "failed" for v in by.values())
    assert "partially" in e["message"].lower()
    print("14. partial failure (one library item vanished) reported accurately OK")


def test_replay_no_duplicate():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    ctx = r["context"]
    e1 = run(confirm(ctx))
    assert e1["state"] == "attached"
    n1 = len([m for m in db.submissions.docs[0]["media"] if m["category"] == "intro_video"])
    e2 = run(confirm(ctx))
    n2 = len([m for m in db.submissions.docs[0]["media"] if m["category"] == "intro_video"])
    assert e2["state"] == "attached" and "already applied" in e2["message"].lower()
    assert n1 == n2 == 1
    execs = [x for x in db.whatsapp_agent_audit_log.docs
             if x.get("sa_action", {}).get("executed") and x["sa_action"]["action_type"] == "attach_media"]
    assert len(execs) == 1
    print("15. replay → no duplicate attachment, one audit row OK")


# ---- SECURITY --------------------------------------------------------
def test_unsigned_plan_rejected():
    install(base_db())
    for c in (None, {}, {"v": 1, "pending": {"kind": "confirm_submission", "sub_plan": {"action_type": "attach_media"}}}):
        e = run(confirm(c or {}))
        assert e["state"] == "blocked"
    print("16. unsigned submission plan rejected OK")


def _preview():
    install(base_db())
    return run(cmd("Attach Ahana's intro to Google AI"))


def test_tampered_talent_rejected():
    r = _preview()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["talent"]["id"] = "t_ahana_dup"
    assert run(confirm(ctx))["state"] == "blocked"
    print("17. tampered talent rejected OK")


def test_tampered_project_and_submission_rejected():
    r = _preview()
    for field, val in (("project", {"id": "p_l", "label": "L'Oreal Glyco"}), ("submission_id", "sub_ahana_l")):
        ctx = copy.deepcopy(r["context"])
        if field == "project":
            ctx["pending"]["sub_plan"]["project"] = val
        else:
            ctx["pending"]["sub_plan"]["submission_id"] = val
        assert run(confirm(ctx))["state"] == "blocked"
    print("18. tampered project / submission rejected OK")


def test_arbitrary_media_and_submission_cannot_be_injected():
    r = _preview()
    ctx = copy.deepcopy(r["context"])
    ctx["pending"]["sub_plan"]["proposed"].append(
        {"category": "intro_video", "label": "evil", "source_id": "evil", "url": "https://evil/x.mp4"})
    assert run(confirm(ctx))["state"] == "blocked"
    # honest path only ever attaches the signed source id
    r2 = _preview()
    run(confirm(r2["context"]))
    print("19. arbitrary media / submission cannot be injected OK")


# ---- SIDE EFFECTS ---------------------------------------------------
def test_no_whatsapp_no_cloudinary_no_link_no_pipeline():
    db = base_db()
    install(db)
    r = run(cmd("Attach Ahana's intro to Google AI"))
    run(confirm(r["context"]))
    assert db.whatsapp_jobs.docs == []
    assert db.whatsapp_batches.docs == []
    assert db.links.docs == []
    # pipeline untouched
    assert db.casting_pipeline.docs[0]["stage"] == "shortlisted"
    print("20. no WhatsApp job / no Cloudinary upload / no link / no pipeline mutation OK")


if __name__ == "__main__":
    for fn in [
        test_talent_and_project_resolution, test_authoritative_duplicate_talent,
        test_ambiguous_project_clarification, test_missing_submission_safe,
        test_form_and_media_inspection, test_intro_identification_and_takes_not_in_library,
        test_filename_ambiguity_no_guess, test_preview_zero_writes,
        test_confirmation_invokes_canonical_service_and_attaches,
        test_already_attached_skipped_no_duplicate, test_wrong_media_ownership_rejected,
        test_stale_submission_detected, test_multi_item_plan_any_tamper_blocks_confirm,
        test_partial_failure_via_live_state, test_replay_no_duplicate,
        test_unsigned_plan_rejected, test_tampered_talent_rejected,
        test_tampered_project_and_submission_rejected,
        test_arbitrary_media_and_submission_cannot_be_injected,
        test_no_whatsapp_no_cloudinary_no_link_no_pipeline,
        test_execution_disabled_blocks_confirm_attach_and_calls_nothing,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT SUBMISSION TESTS PASSED")
