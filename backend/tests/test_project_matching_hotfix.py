"""Hotfix (2026-08-05) — project name matching only. Scoped tests for:
  - extract_move_fields no longer misroutes an implied-stage project name
    ("Move X to Ahaan Film") into target_stage when it fails stage
    validation; it now falls back to interpreting it as project_query,
    but ONLY when no project was already named explicitly elsewhere in
    the sentence (see test_casting_agent.py's
    test_pipeline_suggestion_lists_available for the preserved case).
  - resolve_project_by_name's new project-only forgiveness layer (filler
    words, singular/plural folding, looser auto-resolve thresholds) —
    every example from the hotfix request, plus the "must still ask when
    genuinely ambiguous" guard.

Talent matching, the concurrent task engine, reply routing, undo, and CRM
are untouched by this hotfix — see test_casting_agent.py (talent matching,
undo, numbering) and test_concurrent_tasks.py (task engine) for the
full-suite regression evidence; this file only covers what's new.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid

import pytest

from core import db, _now
from agents import modules as agent_modules
from agents import registry
from agents.dispatcher import handle_inbound_message
from agents.modules import casting_pipeline_nlu as nlu

agent_modules.register_all()

AGENT_ID = "casting-agent"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _phone() -> str:
    return "91" + str(uuid.uuid4().int)[:9]


async def _use_test_config(group_name: str):
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [group_name],
        "allowed_senders": [],
        "security_mode": "group_members",
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, doc, upsert=True)
    return original


async def _restore_config(original):
    if original is None:
        await db[registry.CONFIG_COLLECTION].delete_one({"agent_id": AGENT_ID})
    else:
        original.pop("_id", None)
        await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": AGENT_ID}, original, upsert=True)


async def _seed_project(brand_name: str) -> str:
    pid = f"test-pmh-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str) -> str:
    tid = f"test-pmh-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "tags": [], "notes": ""})
    return tid


async def _seed_pipeline_row(project_id: str, talent_id: str, stage: str) -> None:
    await db.casting_pipeline.insert_one({
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "stage": stage, "created_at": _now(), "updated_at": _now(),
    })


async def _cleanup(phone: str, project_ids=(), talent_ids=()) -> None:
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_tasks.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})


async def _stage(project_id: str, talent_id: str) -> str:
    doc = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": talent_id})
    return doc["stage"]


# ---------------------------------------------------------------------------
# Unit level: resolve_project_by_name directly against every variant the
# hotfix request listed, all against the real example project name.
# ---------------------------------------------------------------------------
async def test_all_forgiving_variants_auto_resolve_unit_level():
    projects = [{"id": "p1", "label": "Tira - Ahaan's Film"}]
    variants = [
        "Tira Ahaan Film", "Tira Ahan Film", "Tira Ahaans Film", "Tira - Ahaan Film",
        "Tira Ahaan's film", "Tira Ahan's film", "Tira film", "Ahaan film",
        "Tira-Ahaan Film", "Tira ahaan",
        # Explicitly-requested general capabilities, not just the literal
        # example list: plural folding and filler-word tolerance.
        "Tira Films", "Ahaan Movie", "The Tira Ahaan Film",
    ]
    for q in variants:
        m = nlu.resolve_project_by_name(q, projects)
        assert m.project is not None, f"{q!r} should auto-resolve, got ambiguous={m.ambiguous} suggestions={m.suggestions} error={m.error}"
        assert m.project["id"] == "p1"
        assert m.ambiguous is None
        assert m.suggestions is None


async def test_genuinely_ambiguous_project_still_asks_unit_level():
    # A bare "Tira" with two real "Tira" projects — must ask, never guess.
    projects = [
        {"id": "p1", "label": "Tira - Ahaan's Film"},
        {"id": "p2", "label": "Tira Talkies"},
    ]
    m = nlu.resolve_project_by_name("Tira", projects)
    assert m.project is None
    assert m.ambiguous is not None
    assert {c["id"] for c in m.ambiguous} == {"p1", "p2"}

    # But a FULLY-specific query among the same two candidates still
    # resolves unambiguously — the ambiguity is about the query being too
    # short/generic, not about the presence of a decoy.
    m2 = nlu.resolve_project_by_name("Tira Ahaan Film", projects)
    assert m2.project is not None and m2.project["id"] == "p1"

    # A genuine near-tie fuzzy typo between two close labels must still ask
    # (unchanged safety property from before this hotfix).
    projects2 = [{"id": "p1", "label": "Toyota Glanza"}, {"id": "p2", "label": "Toyota Glanzo"}]
    m3 = nlu.resolve_project_by_name("Toyota Glanz", projects2)
    assert m3.project is None
    assert m3.ambiguous is not None or m3.suggestions is not None


# ---------------------------------------------------------------------------
# 2026-09-27 fix — _project_name_similarity's best_token computation was
# scoring short alphanumeric-ID fragments (a bare "c" or "10" split out of
# a query/label like "68c158" or "Phase1 A10" by the digit/letter-boundary
# tokenizer) as a trivial 1.0 SequenceMatcher tie against ANY unrelated
# label containing the same short fragment — in a large real candidate
# pool this reliably defeated the ambiguity-margin check and turned a
# should-be-unique typo match into a false "too many suggestions" outcome
# (root cause of tests/test_talentgram_fetcher.py::
# test_minor_project_spelling_variation's flakiness, reproduced
# empirically against the real ~800-project dev DB: near-100% failure
# rate before this fix, 0/150 after). These are the exact Part 1/2 test
# cases from that investigation, run directly at the unit level (no DB).
# ---------------------------------------------------------------------------
async def test_minor_typo_with_short_alphanumeric_tag_resolves_uniquely():
    """The exact reproduced bug: a query differing from the real label by
    one dropped letter, where BOTH strings end in a short alphanumeric
    fragment ("c158"-shaped) that a large candidate pool is statistically
    certain to also contain in a handful of unrelated labels via the
    tokenizer's digit/letter splitting — must still resolve to the real
    project alone."""
    projects = [
        {"id": "real", "label": "Hingle Project 68c158"},
        # Decoys sharing a short digit/letter-split token fragment with the
        # query ("c", "68", "158", "10") purely by coincidence — none of
        # these are remotely the same project name.
        {"id": "decoy1", "label": "Smoke Test Brand C"},
        {"id": "decoy2", "label": "Snap C"},
        {"id": "decoy3", "label": "FairLovely Edit c3eb55"},
        {"id": "decoy4", "label": "Phase1 A10"},
        {"id": "decoy5", "label": "StressA 11"},
    ]
    m = nlu.resolve_project_by_name("Hingle Projct 68c158", projects)
    assert m.project is not None, f"expected a unique match, got ambiguous={m.ambiguous} suggestions={m.suggestions} error={m.error}"
    assert m.project["id"] == "real"


async def test_project_typo_variants_all_resolve_when_unambiguous():
    """Part 2's own required variants, single-project pool (no real
    collision) — exact / minor typo / another typo / case / whitespace
    must all resolve to the one real project."""
    projects = [{"id": "p1", "label": "Hingle Project"}]
    for q in ("Hingle Project", "Hingle Projct", "Hingle Projec", "hingle project", "Hingle  Project"):
        m = nlu.resolve_project_by_name(q, projects)
        assert m.project is not None and m.project["id"] == "p1", f"{q!r} should resolve, got {m}"


async def test_similar_project_names_still_ask_never_silently_pick():
    """Part 2's explicit collision requirement: a weak/typo'd query against
    real, SIMILAR project names ("Hingle Project" / "Hingle Projects" /
    "Hingle Project India") must never silently auto-resolve to one of
    them — the core safety property is `m.project is None`, whether the
    system reports this as ambiguous/suggestions or as a clean "no match".

    Updated 2026-09-27 (forensic audit fix, Task 1/7): this test used to
    additionally require ALL THREE candidates to appear together as a
    tied "ambiguous"/"suggestions" set. That was itself a side effect of
    the exact bug this fix removes — the shared, non-distinguishing token
    "hingle" (present in all three labels) produced a spurious 1.0 tie
    across all of them, which happened to satisfy "ambiguous" for the
    wrong reason. Discounting a token shared across the candidate pool
    (required for the Snapdragon-family fix — see that test) now
    correctly recognizes that "Hingle Project"/"Hingle Projects" are
    genuinely near-identical (the only real collision here) while "Hingle
    Project India"'s extra word makes it measurably, legitimately less
    similar — so it's no longer forced into the same tie. The one
    property that must never change is the safety one below: never a
    silent single pick, and never dragging in the clearly-less-similar
    "India" variant as an equally-plausible suggestion it manifestly is
    not."""
    projects = [
        {"id": "p1", "label": "Hingle Project"},
        {"id": "p2", "label": "Hingle Projects"},
        {"id": "p3", "label": "Hingle Project India"},
    ]
    m = nlu.resolve_project_by_name("Hingle Projct", projects)
    assert m.project is None, f"should not silently auto-resolve among similar names, got {m.project}"
    surfaced = m.ambiguous or m.suggestions
    if surfaced is not None:
        ids = {c["id"] for c in surfaced}
        assert ids <= {"p1", "p2"}, f"must not suggest the clearly-less-similar 'India' variant, got {ids}"


async def test_no_match_behavior_unaffected():
    """An unrelated query against a real project must still cleanly report
    no match, not a spurious short-token tie."""
    projects = [{"id": "p1", "label": "Hingle Project"}]
    m = nlu.resolve_project_by_name("Totally Unrelated Name", projects)
    assert m.project is None
    assert m.ambiguous is None
    assert m.error is not None


# ---------------------------------------------------------------------------
# 2026-09-27 forensic audit fix (Phase 1, Task 1/2/7 A-D) — the shared
# _name_similarity function (unlike _project_name_similarity, which got a
# length guard on 2026-09-27) had NO guard against a single shared token
# producing a trivial 1.0 best_token tie, and _project_name_similarity's
# own length guard didn't help when the shared token itself was long (e.g.
# a project "family" base word like "snapdragon", shared by every sibling
# in a series). Reproduced live: "Snapdragon startup" against a 4-project
# Snapdragon family used to score every one of them at a tied 1.0.
# ---------------------------------------------------------------------------
def _snapdragon_family():
    return [
        {"id": "p1", "label": "Snapdragon Computer - Male Start-up Founder"},
        {"id": "p2", "label": "Snapdragon Computer - Researcher"},
        {"id": "p3", "label": "Snapdragon Film 2"},
        {"id": "p4", "label": "Snapdragon Film 4"},
    ]


async def test_snapdragon_family_never_ties_all_siblings_at_one():
    """The exact reproduced bug: a query sharing only the family base word
    ("snapdragon") with every sibling must never score them all as a
    perfect, undifferentiated tie — the discounted score must distinguish
    the genuinely closer sibling(s) from the unrelated ones."""
    projects = _snapdragon_family()
    for q in ("Snapdragon startup", "Snapdragon Male Startup"):
        m = nlu.resolve_project_by_name(q, projects)
        assert not (m.suggestions and len(m.suggestions) == 4), (
            f"{q!r} must not tie all four Snapdragon siblings — got suggestions={m.suggestions}"
        )
        assert not (m.ambiguous and len(m.ambiguous) == 4), (
            f"{q!r} must not tie all four Snapdragon siblings — got ambiguous={m.ambiguous}"
        )
        # The Film siblings share NOTHING with the query but the discounted
        # family word — they must never be the (sole) resolved pick.
        if m.project is not None:
            assert m.project["id"] in ("p1", "p2"), f"{q!r} resolved to an unrelated Film sibling: {m.project}"


async def test_snapdragon_bare_family_word_still_genuinely_ambiguous():
    """A query that names ONLY the shared family word, with nothing else to
    distinguish siblings, must still honestly report ambiguity (never
    silently pick one) — the fix discounts a shared token's contribution
    to fuzzy SCORING, it must not fabricate false confidence elsewhere."""
    projects = _snapdragon_family()
    m = nlu.resolve_project_by_name("Snapdragon", projects)
    assert m.project is None
    assert m.ambiguous is not None
    assert {c["id"] for c in m.ambiguous} == {"p1", "p2", "p3", "p4"}


async def test_snapdragon_computer_narrows_to_two_computer_siblings():
    """A query specific enough to exclude the Film siblings (shares
    "computer" too, not just "snapdragon") must narrow the candidate set —
    unaffected by the fuzzy-tier fix, exercised here as a regression guard
    since it hits Tier 3/4, not Tier 5."""
    projects = _snapdragon_family()
    m = nlu.resolve_project_by_name("Snapdragon computer", projects)
    ids = {c["id"] for c in (m.ambiguous or m.suggestions or [])}
    if m.project is not None:
        ids = {m.project["id"]}
    assert ids and ids <= {"p1", "p2"}, f"'Snapdragon computer' must exclude the Film siblings, got {m}"


async def test_snapdragon_exact_sibling_film_names_resolve_deterministically():
    """Task 7-B: exact sibling names, including the digit-fused/spaced
    "film2"/"film 2" variants (Task 7-C), must resolve deterministically to
    the correct sibling — never affected by the family-word discount, since
    these hit Tier 2 (normalized-exact) long before Tier 5 ever runs."""
    projects = _snapdragon_family()
    for q, expected_id in (
        ("Snapdragon Film 2", "p3"),
        ("Snapdragon film2", "p3"),
        ("Snapdragon Film 4", "p4"),
        ("Snapdragon film4", "p4"),
    ):
        m = nlu.resolve_project_by_name(q, projects)
        assert m.project is not None and m.project["id"] == expected_id, f"{q!r} -> {m}"


async def test_carter_apostrophe_and_plural_variants_normalize_consistently():
    """Task 2/7-D: "Carter", "Carters", "Carter's", and the curly-quote
    "Carter’s" must all resolve identically against the same single
    real project — previously "Carter's" and "Carters" only converged from
    Tier 4 onward; now the shared canonicalizer folds both the same way
    starting at Tier 2."""
    projects = [{"id": "p1", "label": "Carter's Big Launch"}]
    for q in ("Carter", "Carters", "Carter's", "Carter’s", "carters", "CARTER'S"):
        m = nlu.resolve_project_by_name(q, projects)
        assert m.project is not None and m.project["id"] == "p1", f"{q!r} -> {m}"


async def test_carter_variants_do_not_collapse_distinct_sibling_projects():
    """The normalization unification must not collapse two genuinely
    DIFFERENT projects that happen to share the folded "carter" token —
    "Carter's Big Launch" vs. an unrelated "Carter Family Reunion" must
    still be distinguishable once the query is specific enough."""
    projects = [
        {"id": "p1", "label": "Carter's Big Launch"},
        {"id": "p2", "label": "Carter Family Reunion"},
    ]
    m = nlu.resolve_project_by_name("Carter's Big Launch", projects)
    assert m.project is not None and m.project["id"] == "p1"
    m2 = nlu.resolve_project_by_name("Carter Family Reunion", projects)
    assert m2.project is not None and m2.project["id"] == "p2"


# ---------------------------------------------------------------------------
# End-to-end: the exact five commands from the hotfix's testing section,
# plus the "Tira" single/multiple-project cases.
# ---------------------------------------------------------------------------
async def test_hotfix_testing_section_all_five_commands_auto_resolve():
    group = f"Test PMHotfix {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    project_id = await _seed_project(f"Tira - Ahaan's Film {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    # label carries a random suffix for DB isolation; substitute it into
    # each command so "Tira"/"Ahaan"/"film" tokens still line up exactly
    # as the hotfix's literal examples do against the real project name.
    tag = label.split()[-1]
    talent_id = await _seed_talent("Ahana Pocha")
    talent_ids = [talent_id]
    phones = []
    try:
        commands = [
            f"Add Ahana Pocha to Tira Ahan's Film {tag}",
            f"Move Ahana Pocha to Approved in Tira Ahaan Film {tag}",
            f"Add Ahana Pocha to Tira-Ahaan film {tag}",
            f"Move Ahana Pocha to Ahaan Film {tag}",
            f"Move Ahana Pocha to Tira film {tag}",
        ]

        # Each command gets its OWN phone/conversation — this test is
        # about project-matching resolution per command, not multi-turn
        # conversation-lifecycle interaction (a fresh trigger legitimately
        # replaces an earlier still-pending confirmation, which is
        # existing, unrelated, already-tested behaviour — sharing one
        # phone across 5 independent commands would just be testing that
        # by accident).
        for text in commands:
            phone = _phone()
            phones.append(phone)
            await db.casting_pipeline.delete_many({"project_id": project_id, "talent_id": talent_id})
            await _seed_pipeline_row(project_id, talent_id, "ask_to_test")

            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, f"{text!r} was not handled"
            assert "did you mean" not in r.reply.lower(), f"{text!r} unexpectedly asked to disambiguate the project: {r.reply!r}"
            assert "couldn't find a project" not in r.reply.lower(), f"{text!r}: {r.reply!r}"

            if "Approved" in text and "in" in text:
                # Explicit stage in the command itself — a real
                # confirmation card, project already correctly shown.
                assert f"Project\n{label}" in r.reply, r.reply
                r2 = await handle_inbound_message(
                    group_name=group, sender_phone=phone, text="1",
                    sender_name="Raj", sender_is_group_member=True,
                )
                assert "Done." in r2.reply
                assert await _stage(project_id, talent_id) == "approved"
            elif text.startswith("Add"):
                # Already in ask_to_test — a correct, project-recognized
                # no-op reply naming the right project.
                assert label in r.reply, r.reply
            else:
                # No stage named at all in the command — a genuine,
                # expected missing-field question, NOT the old confusing
                # "I couldn't find a pipeline named 'Ahaan Film'" error
                # that misidentified the project as an invalid stage.
                assert r.reply == "Which pipeline should they move to?", r.reply
                r2 = await handle_inbound_message(
                    group_name=group, sender_phone=phone, text="Approved",
                    sender_name="Raj", sender_is_group_member=True,
                )
                assert f"Project\n{label}" in r2.reply, r2.reply
                r3 = await handle_inbound_message(
                    group_name=group, sender_phone=phone, text="1",
                    sender_name="Raj", sender_is_group_member=True,
                )
                assert "Done." in r3.reply
                assert await _stage(project_id, talent_id) == "approved"
    finally:
        for phone in phones:
            await _cleanup(phone, project_ids=[], talent_ids=[])
        await _cleanup(phones[0] if phones else _phone(), project_ids=[project_id], talent_ids=talent_ids)
        await _restore_config(original)


async def test_bare_tira_single_project_auto_resolves():
    group = f"Test PMHotfix {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(f"Tira Solo {uuid.uuid4().hex[:6]}")
    talent_id = await _seed_talent("Bare Tira Talent")
    talent_ids = [talent_id]
    try:
        await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Add Bare Tira Talent to Tira Solo",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "did you mean" not in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)


async def test_bare_tira_multiple_projects_asks_for_clarification():
    """The hotfix's explicit exception: 'If multiple "Tira" projects
    exist, then and only then should the assistant ask for clarification.'"""
    group = f"Test PMHotfix {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    tag = uuid.uuid4().hex[:6]
    p1 = await _seed_project(f"Tira Alpha {tag}")
    p2 = await _seed_project(f"Tira Beta {tag}")
    talent_id = await _seed_talent(f"Multi Tira Talent {tag}")
    talent_ids = [talent_id]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add Multi Tira Talent {tag} to Tira",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "did you mean" in r.reply.lower() or "found multiple" in r.reply.lower() or "reply with the number" in r.reply.lower()
        for p in (p1, p2):
            proj = await db.projects.find_one({"id": p})
            assert proj["brand_name"] in r.reply
    finally:
        await _cleanup(phone, project_ids=[p1, p2], talent_ids=talent_ids)
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Regression guard specific to the extraction-tier fix: an invalid stage
# word in "to X" position must still surface the old, specific rejection
# message when a project WAS already named explicitly elsewhere in the
# sentence (i.e. X really is meant to be a stage, not a project) — the
# hotfix must not turn a real "invalid pipeline name" typo into a silent,
# uninformative missing-field question.
# ---------------------------------------------------------------------------
async def test_invalid_stage_with_explicit_project_still_gives_specific_error():
    group = f"Test PMHotfix {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = _phone()
    project_id = await _seed_project(f"Explicit Stage Err Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Stage Err Talent {uuid.uuid4().hex[:6]}")
    talent_ids = [talent_id]
    try:
        await _seed_pipeline_row(project_id, talent_id, "hold")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move {(await db.talents.find_one({'id': talent_id}))['name']} to Zzzargled in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert 'I couldn\'t find a pipeline named "Zzzargled".' in r.reply
        assert await _stage(project_id, talent_id) == "hold"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_config(original)
