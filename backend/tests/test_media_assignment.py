"""Media-Assignment (Phase 1, 2026-08-22) — @Gunwanti + mark -> exact
WhatsApp media -> upload - talent - project -> Talentgram submission.

Covers:
  - mark-text parsing (agents/modules/media_assignment.extract_role_and_project)
  - candidate validation (validate_candidates): valid, ambiguous duplicate
    marks, unresolved marks, LID/project filtering
  - the `upload` command's immediate ACK + scan-request creation, and its
    talent/project/no-group/identity error paths (agents/modules/
    casting_pipeline.py's UPLOAD_INTENT)
  - the backend orchestrator's scan_done -> pending_download/finished and
    download_done -> finished transitions (services/media_assignment_worker.py),
    exercised directly against hand-inserted whatsapp_scan_requests docs —
    no real WhatsApp Worker needed, since that side of the contract is
    just "whichever process claims a request does what its mode says".

Idempotency, ambiguity, and resolution-failure reporting are the safety
rules this whole feature exists for — each gets its own test, not folded
into a single "happy path" test.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents import registry  # noqa: E402
from agents.dispatcher import handle_inbound_message  # noqa: E402
from agents.modules import media_assignment as ma  # noqa: E402
from agents.modules.casting_pipeline import AGENT_ID  # noqa: E402
from services import media_assignment_worker as orch  # noqa: E402

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _use_test_config(group_name: str):
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID, "group_names": [group_name], "allowed_senders": [],
        "security_mode": "group_members", "active": True,
        "created_at": _now(), "updated_at": _now(),
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
    pid = f"test-ma-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str, *, whatsapp_group_name: str = "", email: str = "") -> str:
    tid = f"test-ma-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": None, "whatsapp_group_name": whatsapp_group_name,
        "email": email or None, "normalized_email": (email or "").strip().lower() or None,
    })
    return tid


async def _seed_submission(project_id: str, talent_id: str, email: str) -> str:
    """The project's submission for a talent, keyed on (project_id,
    talent_email) — the single source of truth
    resolve_authoritative_talent_for_upload relies on."""
    sid = f"test-ma-sub-{uuid.uuid4().hex[:8]}"
    await db.submissions.insert_one({
        "id": sid, "project_id": project_id, "talent_id": talent_id,
        "talent_email": email.strip().lower(), "media": [], "created_at": _now(),
    })
    return sid


async def _cleanup(*, talent_ids=(), project_ids=(), scan_request_ids=(), submission_ids=()):
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": list(scan_request_ids)}})
    await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": {"$in": list(talent_ids)}})
    if submission_ids:
        await db.submissions.delete_many({"id": {"$in": list(submission_ids)}})


def _mark(*, mention_lid, mark_text, source_message_id, media_type="image", sender="Raj Talentgram"):
    return {
        "mention_lid": mention_lid,
        "mark_text": mark_text,
        "reply_message_id": f"reply-{uuid.uuid4().hex[:8]}",
        "quoted_thumbnail_hash": f"hash-{source_message_id}",
        "resolved_source_message_id": source_message_id,
        "source_media_type": media_type,
        "source_sender": sender,
        "source_timestamp": "2026-08-22T00:00:00Z",
    }


GUNWANTI_LID = "103590702137403@lid"


# ---------------------------------------------------------------------------
# Mark-text parsing (pure, no DB)
# ---------------------------------------------------------------------------
def test_extract_role_and_project_take_variants():
    for text, expected_take in [
        ("mark google take 1", 1), ("MARK GOOGLE TAKE 1", 1),
        ("Mark Google Take 1", 1), ("mark google take2", 2),
    ]:
        parsed = ma.extract_role_and_project(text)
        assert parsed is not None, text
        assert parsed.media_role == "take"
        assert parsed.take_number == expected_take
        assert parsed.project_fragment.lower() == "google"


def test_extract_role_and_project_intro_variants():
    for text in ["mark google intro", "mark intro google", "mark introduction google", "mark google introduction"]:
        parsed = ma.extract_role_and_project(text)
        assert parsed is not None, text
        assert parsed.media_role == "intro"
        assert parsed.take_number is None
        assert parsed.project_fragment.lower() == "google"


def test_extract_role_and_project_photos():
    parsed = ma.extract_role_and_project("mark google photos")
    assert parsed is not None
    assert parsed.media_role == "photos"
    assert parsed.project_fragment.lower() == "google"


def test_extract_role_and_project_no_mark_keyword_returns_none():
    assert ma.extract_role_and_project("google take 1") is None  # no literal "mark"


def test_extract_role_and_project_no_role_keyword_returns_none():
    assert ma.extract_role_and_project("mark google") is None  # "mark" present, no recognized role


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------
def _projects():
    return [{"id": "p-google", "label": "Google"}, {"id": "p-netflix", "label": "Netflix"}]


def test_validate_candidates_happy_path_filters_wrong_project_and_bad_lid():
    candidates = [
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-take1"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google intro", source_message_id="src-intro"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark netflix take 1", source_message_id="src-netflix-take1"),
        _mark(mention_lid="999999@lid", mark_text="mark google take 2", source_message_id="src-fake-mention"),
        _mark(mention_lid="", mark_text="mark google take 3", source_message_id="src-no-mention"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    slots = {(a["media_role"], a["take_number"]) for a in outcome.assignments}
    assert slots == {("take", 1), ("intro", None)}


def test_validate_candidates_ambiguous_duplicate_marks_different_source():
    candidates = [
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-a"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-b"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert not outcome.ok
    assert outcome.ambiguous is not None
    assert outcome.ambiguous["media_role"] == "take"
    assert outcome.ambiguous["take_number"] == 1


def test_validate_candidates_same_source_marked_twice_is_not_ambiguous():
    """The exact same source media marked twice is harmless idempotent
    duplication, never ambiguity — only DIFFERENT sources for the same
    slot are."""
    candidates = [
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-a"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-a"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 1


def test_slot_key_distinguishes_photos_by_source_but_not_takes():
    # "take"/"intro" have exactly one real slot per (role, take_number) —
    # source identity must NOT be part of their key, or two different
    # source tiles both claiming "Take 1" would never be caught as
    # ambiguous (each getting its own singleton slot instead).
    assert ma.slot_key("take", 1, "src-a", "hash-a") == ma.slot_key("take", 1, "src-b", "hash-b")
    # "photos" has no natural slot — two distinct photos (even from the
    # same album, i.e. same source_message_id) must get DIFFERENT keys.
    assert ma.slot_key("photos", None, "album-1", "hash-photo-1") != ma.slot_key("photos", None, "album-1", "hash-photo-2")


def test_validate_candidates_multiple_photos_same_album_not_ambiguous():
    """A batch "mark google photos" against a whole photo album resolves
    to several DISTINCT photos sharing role="photos" and the SAME
    source_message_id (the album's own data-id) but different tile
    hashes — this must never be treated as ambiguous duplication of one
    slot, unlike two different videos both claiming "Take 1"."""
    candidates = [
        {**_mark(mention_lid=GUNWANTI_LID, mark_text="mark google photos", source_message_id="album-1"),
         "quoted_thumbnail_hash": "hash-photo-1"},
        {**_mark(mention_lid=GUNWANTI_LID, mark_text="mark google photos", source_message_id="album-1"),
         "quoted_thumbnail_hash": "hash-photo-2"},
        {**_mark(mention_lid=GUNWANTI_LID, mark_text="mark google photos", source_message_id="album-1"),
         "quoted_thumbnail_hash": "hash-photo-3"},
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 3
    assert {a["quoted_thumbnail_hash"] for a in outcome.assignments} == {"hash-photo-1", "hash-photo-2", "hash-photo-3"}


def test_validate_candidates_batch_resolution_failure_creates_no_single_media_candidate():
    """Real production bug (2026-08-23 E2E, Tests C & D): a batch mark
    ("mark Google Test: take 1, take 2, take 3, intro") that failed to
    resolve to its album's tiles still carried resolved_source_message_id
    and its raw, unparsed text through to the single-mark parser, which
    matched the FIRST role keyword it found ("take 1") and created a
    bogus single-take assignment with no resolved hash. The worker now
    marks such candidates with resolution_status="BATCH_RESOLUTION_FAILED"
    and explicit Nones on every field a single-media assignment would
    need; validate_candidates must reject them before extract_role_and_
    project ever sees the raw text — never a chance at becoming
    ("take", 1) or any other slot."""
    candidates = [{
        "mention_lid": GUNWANTI_LID,
        "mark_text": "mark Google Test: take 1, take 2, take 3, intro     2:08 pm         2:08 pm",
        "reply_message_id": "reply-1",
        "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
        "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
        "resolution_status": "BATCH_RESOLUTION_FAILED",
        "batch_resolution_error": "summary said 4 items, album has 3",
    }]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert not outcome.ok
    assert len(outcome.batch_failures) == 1
    assert outcome.assignments == []  # Test C: ZERO single-media candidates ever created
    assert outcome.ambiguous is None
    assert outcome.unresolved == []
    # Test D: the raw "take 1, take 2, take 3, intro" text is never
    # reinterpreted as a single "take 1" mark.
    assert not any(a.get("media_role") == "take" and a.get("take_number") == 1 for a in outcome.assignments)


def test_validate_candidates_batch_photos_failure_creates_no_single_media_candidate():
    """Test E: the same invariant for a failed "mark <project> photos"
    whole-album batch — must not fall through as a single "photos"
    assignment either."""
    candidates = [{
        "mention_lid": GUNWANTI_LID,
        "mark_text": "mark google photos     3:00 pm         3:00 pm",
        "reply_message_id": "reply-2",
        "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
        "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
        "resolution_status": "BATCH_RESOLUTION_FAILED",
        "batch_resolution_error": "summary said 7 items, album has 5",
    }]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert not outcome.ok
    assert len(outcome.batch_failures) == 1
    assert outcome.assignments == []


def test_validate_candidates_batch_failure_for_different_project_is_filtered_out():
    """A batch-resolution-failure candidate for a DIFFERENT project must
    not spuriously block or clutter this project's report."""
    candidates = [{
        "mention_lid": GUNWANTI_LID,
        "mark_text": "mark netflix: take 1, take 2",
        "reply_message_id": "reply-3",
        "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
        "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
        "resolution_status": "BATCH_RESOLUTION_FAILED",
        "batch_resolution_error": "summary said 2 items, album has 1",
    }]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok  # nothing relevant to Google in this scan — not a failure for THIS request
    assert outcome.batch_failures == []
    assert outcome.assignments == []


def test_validate_candidates_four_tile_album_all_independent_and_idempotent():
    """Test F: the four legitimate tile assignments for one album (same
    source_message_id, four distinct thumbnail hashes) resolve
    independently — no false ambiguity — and re-validating the identical
    candidate set again (simulating a retry) produces the exact same
    four slots, never a fifth."""
    candidates = [
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="album-x"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 2", source_message_id="album-x"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 3", source_message_id="album-x"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google intro", source_message_id="album-x"),
    ]
    # _mark() ties quoted_thumbnail_hash to source_message_id 1:1, so all
    # four would collide on hash unless distinguished explicitly — give
    # each its own real tile hash, matching the actual album shape.
    for c, h in zip(candidates, ["hash-t1", "hash-t2", "hash-t3", "hash-intro"]):
        c["quoted_thumbnail_hash"] = h

    def _slots(outcome):
        return {
            ma.slot_key(a["media_role"], a["take_number"], a["resolved_source_message_id"], a["quoted_thumbnail_hash"])
            for a in outcome.assignments
        }

    outcome1 = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome1.ok
    assert len(outcome1.assignments) == 4
    slots1 = _slots(outcome1)
    assert len(slots1) == 4  # all four independently distinct, no collision

    # Re-run with the identical candidate set (simulating a retry) — must
    # produce the exact same four slots, never a fifth.
    outcome2 = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome2.ok
    assert _slots(outcome2) == slots1


def test_validate_candidates_unresolved_mark_reports_failure_not_guess():
    candidates = [
        {**_mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 2", source_message_id="whatever"),
         "resolved_source_message_id": None},
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert not outcome.ok
    assert len(outcome.unresolved) == 1
    assert outcome.unresolved[0]["media_role"] == "take"


# ---------------------------------------------------------------------------
# `upload - talent - project` — immediate ACK + scan-request creation, and
# the never-guess error paths.
# ---------------------------------------------------------------------------
async def test_upload_command_creates_scan_request_and_acks_immediately():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    phone = "917000600001"
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.upload.{tag}@example.com"
    project_id = await _seed_project(f"Google Upload {tag}")
    talent_id = await _seed_talent(
        f"Ahana Upload {tag}", whatsapp_group_name=f"Ahana Upload {tag} x Talentgram", email=email,
    )
    # 2026-08-23: the upload command now requires the project's own
    # submission (keyed on (project_id, talent_email)) to exist and its
    # email to resolve back to this exact talent — see
    # resolve_authoritative_talent_for_upload.
    submission_id = await _seed_submission(project_id, talent_id, email)
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"upload - Ahana Upload {tag} - Google Upload {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Scanning" in r.reply
        assert f"Google Upload {tag}" in r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["status"] == ma.SCAN_STATUS_PENDING
        assert req["group_name"] == f"Ahana Upload {tag} x Talentgram"
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original)


async def test_upload_command_talent_not_found_never_guesses():
    # 2026-08-23: project is now resolved BEFORE talent (the new
    # authoritative-talent-resolution step needs project_id) — a real
    # project is seeded here so this test isolates the talent-not-found
    # path specifically, rather than incidentally hitting
    # "ambiguous project"/"project not found" first.
    #
    # The name-query below deliberately avoids any real dictionary word
    # ("Person", "Test", "Talent", ...) — the fuzzy resolver's token-match
    # tier (casting_pipeline_nlu.py's single-clearing-candidate rule)
    # correctly auto-resolves a query that shares an exact whole-word
    # token with exactly one DB candidate, even if the rest of the query
    # is nonsense. The shared local/dev Mongo used by this suite carries
    # cross-run leftover talents (e.g. a real "Repro Person" record from
    # an earlier test file), so a query containing "Person" is NOT a safe
    # probe for "matches nothing" — it can legitimately match by design.
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google TalentNotFound {tag}")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600002",
            text=f"upload - Zzzargled9942xyz Qwoplectrix{tag} - Google TalentNotFound {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "no matching" in r.reply.lower() or "couldn't" in r.reply.lower()
    finally:
        await _cleanup(project_ids=[project_id])
        await _restore_config(original)


async def test_upload_command_no_whatsapp_group_reports_clearly():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google NoGroup {tag}")
    talent_id = await _seed_talent(f"NoGroup Talent {tag}", whatsapp_group_name="")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600003",
            text=f"upload - NoGroup Talent {tag} - Google NoGroup {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "no whatsapp group" in r.reply.lower()
    finally:
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Authoritative talent resolution for uploads (2026-08-23) — real
# production risk: an admin manually adds "Ahana Test" (no/different
# email), and separately the same person later submits a project's public
# form with their own real email, creating a SECOND "Ahana Test" talent
# record (routers/submissions.py's submission_finalize looks up by email
# only, never name — an admin record with no email is invisible to it).
# Both records can coexist. Name-based resolution (used everywhere else)
# must NEVER be the thing that decides where audition media lands — only
# the project's own submission, re-verified via its submitted email, is
# authoritative.
# ---------------------------------------------------------------------------
async def test_upload_command_duplicate_talent_resolves_via_submission_email_not_name():
    """THE core safety test: two talent records share the exact name
    "Ahana Test". Record A is the admin-created duplicate (no email, no
    WhatsApp group — exactly what an admin quick-add looks like). Record B
    is the submission-associated one (real email, real WhatsApp group —
    exactly what exists once the talent actually interacts). The upload
    command must resolve to Record B via the project's submission email,
    never by picking either one by name."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group)
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana Dup {tag}"
    email = f"ahana.dup.{tag}@example.com"
    project_id = await _seed_project(f"Google Dup {tag}")
    talent_a = await _seed_talent(name, whatsapp_group_name="", email="")  # admin-created duplicate
    talent_b = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)  # real, submission-associated
    submission_id = await _seed_submission(project_id, talent_b, email)
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600010",
            text=f"upload - {name} - Google Dup {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Scanning" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"project_id": project_id})
        assert req is not None
        assert req["talent_id"] == talent_b, f"expected Record B ({talent_b}), got {req['talent_id']}"
        assert req["talent_id"] != talent_a
        assert req["group_name"] == f"{name} x Talentgram"  # the WhatsApp source, from whichever candidate has one
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
        await _cleanup(talent_ids=[talent_a, talent_b], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original)


async def test_resolve_authoritative_talent_no_submission_stops():
    project_id = await _seed_project(f"Google NoSub {uuid.uuid4().hex[:6]}")
    talent_id = await _seed_talent(f"NoSub Talent {uuid.uuid4().hex[:6]}", email="nosub@example.com")
    try:
        result = await ma.resolve_authoritative_talent_for_upload(project_id, [talent_id])
        assert not result.ok
        assert result.error == "no_submission_found"
    finally:
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id])


async def test_resolve_authoritative_talent_ambiguous_submission_stops():
    """Two DIFFERENT candidate talents each have their own submission for
    this exact project — genuinely ambiguous, never auto-picked."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google Ambig {tag}")
    talent_a = await _seed_talent(f"Ambig Talent {tag}", email=f"ambig.a.{tag}@example.com")
    talent_b = await _seed_talent(f"Ambig Talent {tag}", email=f"ambig.b.{tag}@example.com")
    sub_a = await _seed_submission(project_id, talent_a, f"ambig.a.{tag}@example.com")
    sub_b = await _seed_submission(project_id, talent_b, f"ambig.b.{tag}@example.com")
    try:
        result = await ma.resolve_authoritative_talent_for_upload(project_id, [talent_a, talent_b])
        assert not result.ok
        assert result.error == "ambiguous_submission"
    finally:
        await _cleanup(talent_ids=[talent_a, talent_b], project_ids=[project_id], submission_ids=[sub_a, sub_b])


async def test_resolve_authoritative_talent_email_maps_to_multiple_talents_stops():
    # `talents.email`/`normalized_email` are both uniquely indexed in this
    # database — two talent docs can never literally share the SAME
    # `email` field. The $or lookup also matches `source.talent_email`
    # (not uniquely constrained), which is how two distinct talent docs
    # can genuinely both match the same address in practice.
    tag = uuid.uuid4().hex[:6]
    email = f"shared.{tag}@example.com"
    project_id = await _seed_project(f"Google Shared {tag}")
    talent_id = await _seed_talent(f"Shared Talent {tag}", email=email)
    other_id = f"test-ma-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": other_id, "name": f"Someone Else {tag}", "tags": [], "notes": "",
        "phone": None, "whatsapp_group_name": "", "email": None, "normalized_email": None,
        "source": {"talent_email": email},
    })
    submission_id = await _seed_submission(project_id, talent_id, email)
    try:
        result = await ma.resolve_authoritative_talent_for_upload(project_id, [talent_id])
        assert not result.ok
        assert result.error == "email_maps_to_multiple_talents"
    finally:
        await _cleanup(talent_ids=[talent_id, other_id], project_ids=[project_id], submission_ids=[submission_id])


async def test_resolve_authoritative_talent_unexpected_person_stops():
    """The submission's own email resolves to a talent that isn't even
    among the name-matched candidates — a different person than the
    employee's command referred to. Never silently substituted."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google Unexpected {tag}")
    named_candidate = await _seed_talent(f"Requested Talent {tag}", email="")
    other_email = f"someone.else.{tag}@example.com"
    other_talent = await _seed_talent(f"Totally Different Person {tag}", email=other_email)
    # A submission exists for the requested candidate, but its OWN
    # submitted email actually belongs to a completely different talent
    # record — a real data inconsistency, not a normal case, but must
    # never be silently trusted either way.
    submission_id = await _seed_submission(project_id, named_candidate, other_email)
    try:
        result = await ma.resolve_authoritative_talent_for_upload(project_id, [named_candidate])
        assert not result.ok
        assert result.error in ("submission_talent_mismatch", "email_resolved_to_unexpected_talent")
    finally:
        await _cleanup(talent_ids=[named_candidate, other_talent], project_ids=[project_id], submission_ids=[submission_id])


async def test_resolve_authoritative_talent_ordinary_single_match_succeeds():
    """The common, non-duplicate case: exactly one talent record, one
    submission, matching email — must still succeed (this new
    verification step is not supposed to add friction to the ordinary
    path, only close the duplicate-record gap)."""
    tag = uuid.uuid4().hex[:6]
    email = f"ordinary.{tag}@example.com"
    project_id = await _seed_project(f"Google Ordinary {tag}")
    talent_id = await _seed_talent(f"Ordinary Talent {tag}", email=email)
    submission_id = await _seed_submission(project_id, talent_id, email)
    try:
        result = await ma.resolve_authoritative_talent_for_upload(project_id, [talent_id])
        assert result.ok, result.error
        assert result.talent_id == talent_id
        assert result.email == email
    finally:
        await _cleanup(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])


# ---------------------------------------------------------------------------
# Backend orchestrator — scan_done -> pending_download/finished,
# download_done -> finished. Exercised directly against hand-inserted
# whatsapp_scan_requests docs (standing in for whatever the WhatsApp Worker
# would have written) so this is testable with no real WhatsApp session.
# ---------------------------------------------------------------------------
async def _insert_scan_done(*, talent_id, talent_label, project_id, project_label, group_name, candidates):
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "scan", "status": ma.SCAN_STATUS_DONE,
        "group_name": group_name, "talent_id": talent_id, "talent_label": talent_label,
        "project_id": project_id, "project_label": project_label,
        "candidates": candidates, "scan_error": None,
        "created_at": _now(), "updated_at": _now(),
    })
    return req_id


async def test_orchestrator_scan_done_ambiguous_marks_finished_with_report():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-a"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-b"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        did_work = await orch._process_scan_done()
        assert did_work
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "AMBIGUOUS MEDIA ASSIGNMENT" in final["report"]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})


async def test_orchestrator_scan_done_unresolved_mark_finished_with_report():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[
            {**_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 2", source_message_id="x"),
             "resolved_source_message_id": None},
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "MEDIA RESOLUTION FAILED" in final["report"]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})


async def test_orchestrator_scan_done_valid_marks_moves_to_pending_download():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert mid["status"] == ma.DOWNLOAD_STATUS_PENDING
        assert mid["mode"] == "download"
        targets = {(t["media_role"], t["take_number"]) for t in mid["download_targets"]}
        assert targets == {("take", 1), ("intro", None)}

        rows = await db[ma.ASSIGNMENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert len(rows) == 2
        assert all(r["assignment_status"] == ma.ASSIGN_STATUS_MARKED for r in rows)
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_orchestrator_download_done_reports_upload_complete():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "download", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id,
        "download_targets": [
            {"source_message_id": "src-take1", "media_role": "take", "take_number": 1,
             "original_label": f"{project_label} Take 1"},
        ],
        "pending_report_context": {"talent_label": talent_label, "project_label": project_label, "already": []},
        "created_at": _now(), "updated_at": _now(),
    })
    # Simulate /media-upload having already marked the assignment uploaded.
    await db[ma.ASSIGNMENTS_COLLECTION].insert_one({
        "assignment_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "source_message_id": "src-take1", "media_role": "take", "take_number": 1,
        "assignment_status": ma.ASSIGN_STATUS_UPLOADED, "created_at": _now(), "created_by": "test",
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "UPLOAD COMPLETE" in final["report"]
        assert f"{project_label} Take 1" in final["report"]
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_orchestrator_already_uploaded_is_idempotent_no_redownload():
    """Running scan_done validation again for a project already fully
    uploaded must go straight to ALREADY COMPLETED, never queue a fresh
    download."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    await db[ma.ASSIGNMENTS_COLLECTION].insert_one({
        "assignment_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "source_message_id": "src-take1", "media_role": "take", "take_number": 1,
        "assignment_status": ma.ASSIGN_STATUS_UPLOADED, "created_at": _now(), "created_by": "test",
    })
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1")],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "ALREADY COMPLETED" in final["report"]
        assert "No duplicate upload performed." in final["report"]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
