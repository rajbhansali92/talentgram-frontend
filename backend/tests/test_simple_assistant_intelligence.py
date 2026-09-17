"""Simple Assistant — Phase 8 READ-ONLY deterministic inbound intelligence.

Deterministic classification / extraction / project-knowledge retrieval /
answerability / proposed (display-only) response over the Phase 7 canonical
inbound history. No LLM, no send, no mutation.

Run:  python3 backend/tests/test_simple_assistant_intelligence.py
"""
import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_URL", "mongodb://x")
os.environ.setdefault("DB_NAME", "talentgram")
os.environ.setdefault("JWT_SECRET", "phase8-secret")
os.environ.setdefault("ADMIN_EMAIL", "a@b.com")
os.environ.setdefault("ADMIN_PASSWORD", "x")
for _k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.setdefault(_k, "x")
os.environ["SIMPLE_ASSISTANT_ENABLED"] = "true"
os.environ["SA_INBOUND_CAPTURE_ENABLED"] = "true"
os.environ["SA_INBOUND_INTELLIGENCE_ENABLED"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inbound_messages as ibm
import simple_assistant.audit as sa_audit
import simple_assistant.inbound_intelligence as I
import simple_assistant.readonly_db as sa_rdb
import simple_assistant.service as sa_service
import simple_assistant.whatsapp_send as wsend
from simple_assistant.commands import run_command


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ---- fake mongo ----------------------------------------------------
def _cmp(dv, v):
    if isinstance(v, dict):
        if "$in" in v:
            return dv in v["$in"]
        if "$ne" in v:
            return dv != v["$ne"]
        if "$gte" in v:
            return dv is not None and dv >= v["$gte"]
        if "$gt" in v:
            return dv is not None and dv > v["$gt"]
        if "$regex" in v:
            import re as _re
            return dv is not None and _re.search(v["$regex"], str(dv)) is not None
    return dv == v


def _get(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_match(doc, s) for s in v):
                return False
            continue
        dv = _get(doc, k) if "." in k else doc.get(k)
        if not _cmp(dv, v):
            return False
    return True


class Cur:
    def __init__(s, d):
        s._d = [copy.deepcopy(x) for x in d]

    def sort(s, key, direction=1):
        if isinstance(key, str):
            s._d.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=(direction == -1))
        return s

    def limit(s, n):
        s._d = s._d[:n]
        return s

    async def to_list(s, n=None):
        return list(s._d) if n is None else list(s._d[:n])


class Coll:
    def __init__(s, name, d=None):
        s.name = name
        s.docs = list(d or [])

    def find(s, q=None, p=None):
        return Cur([d for d in s.docs if _match(d, q)])

    async def find_one(s, q=None, p=None, **k):
        return next((copy.deepcopy(d) for d in s.docs if _match(d, q)), None)

    async def count_documents(s, q=None):
        return sum(1 for d in s.docs if _match(d, q))

    async def insert_one(s, d):
        if s.name == "whatsapp_inbound_messages" and any(
            x.get("message_key") == d.get("message_key") for x in s.docs
        ):
            raise Exception("E11000 duplicate key")
        s.docs.append(copy.deepcopy(d))

    async def update_one(s, *a, **k):
        if s.name in ("casting_pipeline", "submissions", "talents", "projects"):
            raise AssertionError(f"Phase 8 must not write {s.name}")

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
        s._c = {k: Coll(k, v) for k, v in c.items()}

    def __getattr__(s, n):
        return s._c.setdefault(n, Coll(n, []))

    def __getitem__(s, n):
        return s.__getattr__(n)


USER = {"id": "u1", "name": "Raj", "email": "raj@x.com", "role": "admin"}
GROUP_G = "Google AI Casting"
GROUP_L = "L'Oreal Casting"
AHANA = "+919810000001"


def _t(m):
    return datetime.now(timezone.utc) - timedelta(minutes=m)


def _msg(text, *, minutes_ago=10, message_id=None, group=GROUP_G):
    message_id = message_id or f"W_{abs(hash(text)) % 99999}"
    return {"message_id": message_id, "sender_phone": AHANA, "text": text,
            "sender_name": "Ahana", "group_name": group, "received_at": _t(minutes_ago),
            "source": "whatsapp_worker"}


def base_db(**over):
    d = DB(
        projects=[
            {"id": "p_g", "brand_name": "Google AI", "status": "ongoing",
             "whatsapp_casting_group_name": GROUP_G, "budget_per_day": "₹35,000",
             "shoot_dates": "15 September 2026", "medium_usage": "Digital, 1 year",
             "commission_percent": "20%", "additional_details": "Lead role, tech ad film.",
             "character": "Lead, 25-30", "materials": []},
            {"id": "p_l", "brand_name": "L'Oreal Glyco", "status": "ongoing",
             "whatsapp_casting_group_name": GROUP_L, "materials": []},   # no budget/date
        ],
        talents=[
            {"id": "t_ahana", "name": "Ahana Pocha", "phone": AHANA, "status": "ACTIVE", "media": []},
        ],
        casting_pipeline=[
            {"id": "pl1", "project_id": "p_g", "talent_id": "t_ahana", "stage": "shortlisted"},
        ],
        whatsapp_agent_audit_log=[],
        whatsapp_agent_config=[{"agent_id": "casting-agent", "group_names": [GROUP_G, GROUP_L, "Team Ops"]}],
        whatsapp_inbound_messages=[],
        whatsapp_jobs=[], whatsapp_batches=[], asset_metadata=[], submissions=[],
    )
    for k, v in over.items():
        d._c[k] = Coll(k, v)
    return d


_BATCH = []


def install(db):
    sa_rdb._real_db = db
    sa_service.db = db
    sa_audit.db = db
    ibm.db = db
    wsend.db = db
    _BATCH.clear()

    async def _no_send(*a, **k):
        _BATCH.append(1)
        raise AssertionError("Phase 8 must never create an outbound batch")

    wsend._create_batch_internal = _no_send

    async def _sc(pid):
        return {s: 0 for s in sa_service.PIPELINE_STAGE_ORDER}

    sa_service.get_stage_counts = _sc


async def cap(m):
    return await ibm.capture_inbound(**m)


async def cmd(text, ctx=None):
    return await run_command(message=text, conversation_id=None, context=ctx, user=USER)


def analyze_text(text, project=None):
    """capture + analyze in one step, for the deterministic-layer assertions."""
    db = base_db()
    install(db)
    run(cap(_msg(text)))
    doc = db.whatsapp_inbound_messages.docs[-1]
    proj = project or db.projects.docs[0]
    return I.analyze(doc, proj)


# ---- CLASSIFICATION ----------------------------------------------
def test_topic_classification_operational():
    cases = {
        "I can do it but what is the budget?": "budget",
        "What date is the shoot?": "shoot_date",
        "Where is the shoot happening?": "location",
        "What is the payment schedule?": "payment",
        "I'm not available that week.": "availability",
        "I'm really interested in this one.": "interest",
        "I'll submit my tape tonight.": "submission",
        "Can you tell me more about the project?": "project_details",
        "sdkjfh random noise": "unknown",
    }
    for text, want in cases.items():
        got, conf = I.classify_topic(text)
        assert got == want, f"{text!r}: got {got}, want {want}"
    print("1-9. topic classification (budget/date/location/payment/availability/interest/submission/details/unknown) OK")


def test_generic_question_and_no_forced_category():
    got, conf = I.classify_topic("Hey, quick one for you?")
    assert got == "question" and conf == "low"
    got2, _ = I.classify_topic("Okay noted, thanks!")
    assert got2 == "confirmation"
    print("+. bare question → 'question' low; never forces a category OK")


# ---- EXTRACTION -------------------------------------------------
def test_extraction_rules():
    s = I.extract_signals("The shoot is on 15 September, right?")
    assert s["is_question"] is True
    assert s["date"]["resolution"] == "resolved" and "15 September" in s["date"]["raw"]

    a = I.extract_signals("Is it the 15th or the 16th?")
    assert a["date"]["resolution"] == "ambiguous" and a["date"]["value"] is None

    rel = I.extract_signals("I can start tomorrow.")
    assert rel["date"]["resolution"] == "relative"

    none = I.extract_signals("Sounds great, count me in.")
    assert none["date"]["resolution"] == "none"          # no false extraction
    assert none["interest_signal"] == "interested"

    av = I.extract_signals("Sorry, I'm not available on those dates.")
    assert av["availability_signal"] == "unavailable"
    print("10-14. date (clear/ambiguous/relative/none) + question + availability + no false extraction OK")


# ---- PROJECT KNOWLEDGE ------------------------------------------
def test_project_knowledge_present_and_missing():
    r = analyze_text("What is the budget?")
    assert r["project_knowledge"]["available"] is True
    assert r["project_knowledge"]["value"] == "₹35,000"

    # L'Oreal has no budget / no shoot date
    db = base_db()
    install(db)
    run(cap(_msg("What is the budget?", group=GROUP_L)))
    doc = db.whatsapp_inbound_messages.docs[-1]
    r2 = I.analyze(doc, db.projects.docs[1])
    assert r2["project_knowledge"]["available"] is False
    assert r2["proposed_response"] and "don't have the budget" in r2["proposed_response"].lower()

    d = analyze_text("What date is the shoot?")
    assert d["project_knowledge"]["value"] == "15 September 2026"

    p = analyze_text("What's the payment schedule?")
    assert p["project_knowledge"]["available"] is False        # no canonical field
    assert p["project_knowledge"]["no_canonical_field"] is True
    print("15-19. project knowledge: budget present/missing, shoot date present, payment has no field OK")


def test_never_invents_or_borrows_from_another_project():
    # message for L'Oreal (no budget); the analyzer must NOT pull Google AI's
    db = base_db()
    install(db)
    run(cap(_msg("How much does it pay?", group=GROUP_L)))
    doc = db.whatsapp_inbound_messages.docs[-1]
    r = I.analyze(doc, db.projects.docs[1])
    assert "35,000" not in (r["proposed_response"] or "")
    assert r["project_knowledge"]["value"] is None
    print("+. never borrows a value from an unrelated project OK")


def test_budget_hidden_from_talent():
    db = base_db()
    db.projects.docs[0]["hide_budget_from_talent"] = True
    install(db)
    run(cap(_msg("what's the budget?")))
    doc = db.whatsapp_inbound_messages.docs[-1]
    r = I.analyze(doc, db.projects.docs[0])
    assert r["project_knowledge"]["hidden_from_talent"] is True
    assert r["proposed_response"] is None                       # not proposed to the talent
    assert "hidden" in (r["next_step"] or "").lower()
    print("+. budget marked hidden-from-talent → no proposed response, admin decision OK")


# ---- ANSWERABILITY --------------------------------------------
def test_answerability_states():
    assert analyze_text("What is the budget?")["answerability"] == "answerable"
    # payment: no field → insufficient
    db = base_db(); install(db)
    run(cap(_msg("What is the payment schedule?")))
    r = I.analyze(db.whatsapp_inbound_messages.docs[-1], db.projects.docs[0])
    assert r["answerability"] == "insufficient_information"
    # availability with a shoot date on file → partially answerable
    r2 = analyze_text("I might not be free that week.")
    assert r2["answerability"] == "partially_answerable"
    print("20-23. answerability: answerable / insufficient / partially_answerable OK")


def test_ambiguous_project_answerability_uses_no_data():
    db = base_db()
    db.whatsapp_agent_audit_log = Coll("whatsapp_agent_audit_log", [
        {"timestamp": _t(30), "agent_id": "simple-assistant",
         "sa_action": {"action_type": "send_media", "talent_id": "t_ahana", "project_id": "p_g", "batch_ids": ["b"]}},
        {"timestamp": _t(35), "agent_id": "simple-assistant",
         "sa_action": {"action_type": "send_media", "talent_id": "t_ahana", "project_id": "p_l", "batch_ids": ["b"]}},
    ])
    db._c["whatsapp_agent_audit_log"] = db.whatsapp_agent_audit_log
    install(db)
    run(cap(_msg("what is the budget?", group="Team Ops")))    # not a casting group → ambiguous project
    doc = db.whatsapp_inbound_messages.docs[-1]
    assert doc["project_resolution"] == "ambiguous_project"
    r = I.analyze(doc, None)
    assert r["answerability"] == "ambiguous"
    assert r["project_knowledge"]["available"] is False
    assert r["proposed_response"] is None
    print("+. ambiguous project → answerability 'ambiguous', no project data used OK")


# ---- PROPOSED RESPONSE --------------------------------------
def test_proposed_response_deterministic():
    assert analyze_text("What is the budget?")["proposed_response"] == "The budget for Google AI is ₹35,000."
    assert analyze_text("What date is the shoot?")["proposed_response"] == "The shoot is scheduled for 15 September 2026."
    assert analyze_text("What's the usage?")["proposed_response"] == "The usage for this project is Digital, 1 year."
    print("24. deterministic proposed response — exact templates, from canonical fields OK")


def test_no_response_when_data_missing():
    db = base_db(); install(db)
    run(cap(_msg("Where exactly is the shoot?")))
    r = I.analyze(db.whatsapp_inbound_messages.docs[-1], db.projects.docs[0])
    assert r["proposed_response"] and "don't have the shoot location" in r["proposed_response"].lower()
    assert "invent" not in r["proposed_response"]
    print("25-26. missing data → honest 'I don't have …', nothing invented OK")


# ---- NEGOTIATION (STEP 10) --------------------------------
def test_negotiation_never_counteroffers():
    r = analyze_text("Can you increase the budget a bit?")
    assert r["topic"] == "budget" and r["signals"]["negotiation_request"] is True
    assert r["proposed_response"] is None
    assert "admin decision" in (r["next_step"] or "").lower()
    assert r["project_knowledge"]["value"] == "₹35,000"      # current shown, not changed
    print("STEP 10. negotiation → shows current budget, proposes nothing, admin decision OK")


# ---- PIPELINE / MUTATION (STEP 11 / 17) -------------------
def test_no_pipeline_mutation_even_for_not_available():
    db = base_db()
    install(db)
    before = copy.deepcopy(db.casting_pipeline.docs)
    run(cap(_msg("I'm not available for this, sorry.")))
    run(cmd("What is Ahana asking about Google AI?"))
    assert db.casting_pipeline.docs == before                 # STEP 11
    r = I.analyze(db.whatsapp_inbound_messages.docs[-1], db.projects.docs[0])
    assert r["pipeline_mutation"] == "none" and r["no_message_sent"] is True
    print("STEP 11/17. 'I'm not available' → signal only, 0 pipeline mutation OK")


def test_intelligence_query_zero_side_effects():
    db = base_db()
    install(db)
    run(cap(_msg("What is the budget?")))
    before_j, before_b = list(db.whatsapp_jobs.docs), list(db.whatsapp_batches.docs)
    before_s, before_p = list(db.submissions.docs), copy.deepcopy(db.casting_pipeline.docs)
    r = run(cmd("Analyze Ahana's latest WhatsApp message."))
    assert r["intelligence"] is not None
    assert db.whatsapp_jobs.docs == before_j and db.whatsapp_batches.docs == before_b
    assert db.submissions.docs == before_s and db.casting_pipeline.docs == before_p
    assert db.asset_metadata.docs == []
    assert _BATCH == []
    print("STEP 17. 0 jobs / batches / submission / pipeline / cloudinary / outbound OK")


def test_no_llm_anywhere():
    src = open(os.path.join(os.path.dirname(__file__), "..", "simple_assistant", "inbound_intelligence.py")).read()
    low = "\n".join(l for l in src.splitlines() if l.strip().startswith(("import ", "from "))).lower()
    for bad in ("anthropic", "openai", "gemini", "ai.client", "ai import", "embed", "vector"):
        assert bad not in low, bad
    assert "messages.create" not in src and ".chat.completions" not in src and "model=" not in src
    print("STEP 3. inbound_intelligence.py — no LLM import, no model call OK")


# ---- VERBATIM (STEP 5) -----------------------------------
def test_original_message_preserved():
    txt = "I can do it but what is the budget? (asking for a friend)"
    r = analyze_text(txt)
    assert r["message_text"] == txt                            # exact, not normalized
    assert r["topic"] == "budget"                              # interpretation is metadata
    print("STEP 5. verbatim message preserved; interpretation is metadata OK")


# ---- COMMANDS + CARD ------------------------------------
def test_command_returns_intelligence_card():
    db = base_db()
    install(db)
    run(cap(_msg("What is the budget for this?")))
    r = run(cmd("What is Ahana asking about Google AI?"))
    c = r["intelligence"]
    assert c["talent"]["label"] == "Ahana Pocha" and c["project"]["label"] == "Google AI"
    assert c["topic"] == "budget" and c["answerability"] == "answerable"
    assert c["proposed_response"] == "The budget for Google AI is ₹35,000."
    assert c["no_message_sent"] is True
    assert "no message was sent" in r["message"].lower()
    print("STEP 13/15. 'What is Ahana asking about Google AI?' → intelligence card OK")


def test_low_confidence_needs_review():
    db = base_db()
    install(db)
    run(cap(_msg("hmm ok maybe, we'll see")))
    r = run(cmd("Analyze Ahana's latest message"))
    c = r["intelligence"]
    assert c["topic_confidence"] != "high"
    assert c["needs_review"] is True
    print("STEP 16. low-confidence classification → needs_review OK")


def test_ambiguous_project_command_lists_candidates():
    db = base_db()
    db._c["whatsapp_agent_audit_log"] = Coll("whatsapp_agent_audit_log", [
        {"timestamp": _t(30), "agent_id": "simple-assistant",
         "sa_action": {"action_type": "send_media", "talent_id": "t_ahana", "project_id": "p_g", "batch_ids": ["b"]}},
        {"timestamp": _t(31), "agent_id": "simple-assistant",
         "sa_action": {"action_type": "send_media", "talent_id": "t_ahana", "project_id": "p_l", "batch_ids": ["b"]}},
    ])
    install(db)
    run(cap(_msg("what is the budget?", group="Team Ops")))
    r = run(cmd("Why did Ahana message?"))
    assert "cannot reliably determine the project" in r["message"].lower()
    assert "Google AI" in r["message"] and "L'Oreal" in r["message"]
    assert r["intelligence"]["project"] is None
    assert r["intelligence"].get("project_knowledge", {}).get("available") in (False, None)
    print("STEP 12. ambiguous project → candidates listed, no project data used OK")


def test_flag_off_falls_back_to_plain_card():
    os.environ["SA_INBOUND_INTELLIGENCE_ENABLED"] = "false"
    try:
        db = base_db()
        install(db)
        run(cap(_msg("What is the budget?")))
        r = run(cmd("What is Ahana asking about Google AI?"))
        assert r.get("intelligence") is None
        assert r.get("inbound") is not None                    # plain Phase 7 card
        assert "isn't enabled" in r["message"].lower()
    finally:
        os.environ["SA_INBOUND_INTELLIGENCE_ENABLED"] = "true"
    print("STEP 22. flag OFF → graceful fall-back to the plain inbound card OK")


def test_audit_marker_is_inspection_not_action():
    db = base_db()
    install(db)
    run(cap(_msg("What is the budget?")))
    run(cmd("Analyze Ahana's latest WhatsApp message"))
    rows = [x for x in db.whatsapp_agent_audit_log.docs
            if (x.get("sa_action") or {}).get("action_type") == "inspect_inbound_intelligence"]
    assert rows and rows[0]["sa_action"]["executed"] is False
    assert rows[0]["sa_action"]["query_kind"] == "inbound_intelligence"
    # the inbound message row itself is untouched, still direction "in"
    assert db.whatsapp_inbound_messages.docs[0]["direction"] == "in"
    print("STEP 18. audit distinguishes inspection (executed:false) from an action OK")


def test_unresolved_talent_not_analyzed_as_one():
    db = base_db()
    install(db)
    run(cap({"message_id": "UU", "sender_phone": "+15550001234", "text": "what is the budget?",
             "sender_name": None, "group_name": GROUP_G, "received_at": _t(5), "source": "whatsapp_worker"}))
    r = run(cmd("Analyze the latest question from Google AI"))
    # project-scoped list still surfaces it, sender shown as unresolved
    assert r["answer"]["kind"] == "inbound_question_list"
    assert any(row["talent"] in ("Unresolved", None) for row in r["answer"]["rows"])
    print("STEP 12. unresolved sender is not analyzed as a specific talent OK")


if __name__ == "__main__":
    for fn in [
        test_topic_classification_operational, test_generic_question_and_no_forced_category,
        test_extraction_rules, test_project_knowledge_present_and_missing,
        test_never_invents_or_borrows_from_another_project, test_budget_hidden_from_talent,
        test_answerability_states, test_ambiguous_project_answerability_uses_no_data,
        test_proposed_response_deterministic, test_no_response_when_data_missing,
        test_negotiation_never_counteroffers, test_no_pipeline_mutation_even_for_not_available,
        test_intelligence_query_zero_side_effects, test_no_llm_anywhere,
        test_original_message_preserved, test_command_returns_intelligence_card,
        test_low_confidence_needs_review, test_ambiguous_project_command_lists_candidates,
        test_flag_off_falls_back_to_plain_card, test_audit_marker_is_inspection_not_action,
        test_unresolved_talent_not_analyzed_as_one,
    ]:
        fn()
    print("\nALL SIMPLE ASSISTANT INTELLIGENCE (PHASE 8) TESTS PASSED")
