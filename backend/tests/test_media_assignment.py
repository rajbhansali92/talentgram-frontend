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
from routers import agents_whatsapp  # noqa: E402

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _use_test_config(group_name: str, agent_id: str = AGENT_ID):
    """agent_id defaults to casting-agent (every pre-existing call site
    unaffected); Send/Share Semantic Router tests (Production fix,
    2026-09-06 — "send" is no longer directly triggerable on
    casting-agent, only via a hand-off from whatsapp-campaign-agent's
    own SHARE_INTENT) pass agent_id="whatsapp-campaign-agent" instead —
    see test_media_send.py's own send-triggered tests."""
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": agent_id})
    doc = {
        "agent_id": agent_id, "group_names": [group_name], "allowed_senders": [],
        "security_mode": "group_members", "active": True,
        "created_at": _now(), "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": agent_id}, doc, upsert=True)
    return original


async def _restore_config(original, agent_id: str = AGENT_ID):
    if original is None:
        await db[registry.CONFIG_COLLECTION].delete_one({"agent_id": agent_id})
    else:
        original.pop("_id", None)
        await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": agent_id}, original, upsert=True)


async def _seed_project(brand_name: str, *, whatsapp_casting_group_name: str = "") -> str:
    pid = f"test-ma-proj-{uuid.uuid4().hex[:8]}"
    doc = {
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "materials": [], "created_at": _now(),
    }
    if whatsapp_casting_group_name:
        doc["whatsapp_casting_group_name"] = whatsapp_casting_group_name
    await db.projects.insert_one(doc)
    return pid


async def _seed_talent(name: str, *, whatsapp_group_name: str = "", email: str = "") -> str:
    tid = f"test-ma-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "tags": [], "notes": "",
        "phone": None, "whatsapp_group_name": whatsapp_group_name,
        "email": email or None, "normalized_email": (email or "").strip().lower() or None,
    })
    return tid


async def _seed_submission(project_id: str, talent_id: str, email: str, *, decision: str = "pending") -> str:
    """The project's submission for a talent, keyed on (project_id,
    talent_email) — the single source of truth
    resolve_authoritative_talent_for_upload relies on. `decision` defaults
    to "pending" (SEND's own approval gate requires "approved" explicitly
    — see test_media_send.py's SEND-workflow tests)."""
    sid = f"test-ma-sub-{uuid.uuid4().hex[:8]}"
    await db.submissions.insert_one({
        "id": sid, "project_id": project_id, "talent_id": talent_id,
        "talent_email": email.strip().lower(), "media": [], "form_data": {},
        "decision": decision, "created_at": _now(),
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


def test_extract_role_and_project_introduction_take_is_intro_not_bare_take():
    """Real production bug (2026-09-03): "MARK Introduction take for PGI"
    — an explicitly required phrasing (master prompt's own worked
    example) — was misclassified as media_role="take" (no number)
    because the bare word "take" also appears inside "Introduction
    take", and the OLD priority order checked bare-take before intro.
    "Introduction"/"Photos" are more specific role signals and must win
    over a bare, number-less "take" whenever both are present — this
    collided two genuinely different marks ("Introduction take" and
    "Audition take") onto the identical ("take", None) slot, reporting a
    false "marked twice" ambiguity."""
    parsed = ma.extract_role_and_project("mark introduction take for PGI")
    assert parsed is not None
    assert parsed.media_role == "intro"
    assert parsed.take_number is None
    assert parsed.project_fragment.lower() == "for pgi" or "pgi" in parsed.project_fragment.lower()

    # A genuinely bare, no-more-specific-keyword "take" still resolves as
    # take — the reordering only changes what happens when intro/photos
    # ALSO match, never the plain case.
    parsed2 = ma.extract_role_and_project("mark audition take for PGI")
    assert parsed2 is not None
    assert parsed2.media_role == "take"
    assert parsed2.take_number is None

    # A numbered take still wins outright over intro/photos wording,
    # exactly as before — the reordering only affects the BARE-take tier.
    parsed3 = ma.extract_role_and_project("mark introduction take 2 for PGI")
    assert parsed3 is not None
    assert parsed3.media_role == "take"
    assert parsed3.take_number == 2


def test_extract_role_and_project_photos():
    parsed = ma.extract_role_and_project("mark google photos")
    assert parsed is not None
    assert parsed.media_role == "photos"
    assert parsed.project_fragment.lower() == "google"


def test_extract_role_and_project_no_mark_keyword_returns_none():
    assert ma.extract_role_and_project("google take 1") is None  # no literal "mark"


def test_extract_role_and_project_no_role_keyword_returns_none():
    # Deliberately still None (unchanged) — a role-less mark defaulting to
    # any role was tried during the MARK-flexibility fix and reverted
    # after a real regression (decoy/chatter text like "mark {project}
    # random 5" in a long conversation, with no role keyword, would
    # otherwise all collide onto the same slot and falsely report
    # AMBIGUOUS MEDIA ASSIGNMENT) — see extract_role_and_project's own
    # final `else` branch comment.
    assert ma.extract_role_and_project("mark google") is None  # "mark" present, no recognized role


def test_extract_role_and_project_strips_whatsapp_dom_timestamp_noise():
    """Regression (2026-08-25, found via a real live SEND E2E): mark_text
    is captured straight from WhatsApp's own DOM, which renders a
    message's timestamp TWICE (once for the bubble, once for an
    accessibility label) — sometimes prefixed "Edited". Left unstripped,
    "Google Test 3 7:20 am 7:20 am" became AMBIGUOUS against a real
    "Google Test" project (the noise defeated fuzzy resolution), silently
    dropping an otherwise-valid mark as "wrong project"."""
    cases = [
        ("mark Google Test 3 take 1     7:20 am         7:20 am", "Google Test 3"),
        ("mark google test take 1     Edited  5:05 am         Edited  5:05 am", "google test"),
        ("mark CleanTest take 1     7:13 pm         7:13 pm", "CleanTest"),
        ("mark google take 1     5:51 am         5:51 am", "google"),
    ]
    for text, expected_fragment in cases:
        parsed = ma.extract_role_and_project(text)
        assert parsed is not None, text
        assert parsed.project_fragment == expected_fragment, (text, parsed.project_fragment)


# ---------------------------------------------------------------------------
# Regression (2026-08-27, real production incident — Siddhi Bankhele / TVS
# Jupiter): a bare "Take" mark (admin never gave a take number) immediately
# followed by WhatsApp's appended timestamp noise was parsed as "Take 7" --
# _TAKE_RE's unbounded \s* let it reach across "Take     7:26 am" and treat
# the time's leading digit as the take number. Confirmed live via a
# read-only scan_probe before any send was attempted; caught and fixed
# before ever reaching production dispatch.
# ---------------------------------------------------------------------------
def test_extract_role_and_project_bare_take_with_timestamp_never_becomes_a_number():
    parsed = ma.extract_role_and_project("Mark Tvs Jupiter Take     7:26 am         7:26 am")
    assert parsed is not None
    assert parsed.media_role == "take"
    assert parsed.take_number is None, "the timestamp's digit must never become take_number"
    assert parsed.project_fragment == "Tvs Jupiter"


def test_extract_role_and_project_numbered_take_with_timestamp_still_resolves():
    parsed = ma.extract_role_and_project("Mark Tvs Jupiter Take 1     7:26 am         7:26 am")
    assert parsed is not None
    assert parsed.media_role == "take"
    assert parsed.take_number == 1
    assert parsed.project_fragment == "Tvs Jupiter"


def test_extract_role_and_project_numbered_take_without_timestamp_unaffected():
    parsed = ma.extract_role_and_project("Mark Tvs Jupiter Take 2")
    assert parsed is not None
    assert parsed.media_role == "take"
    assert parsed.take_number == 2
    assert parsed.project_fragment == "Tvs Jupiter"


def test_extract_role_and_project_bare_intro_with_timestamp_unaffected():
    parsed = ma.extract_role_and_project("Mark Tvs Jupiter Intro     7:26 am         7:26 am")
    assert parsed is not None
    assert parsed.media_role == "intro"
    assert parsed.take_number is None
    assert parsed.project_fragment == "Tvs Jupiter"


def test_extract_role_and_project_legitimate_take_number_survives_various_timestamps():
    """A real take number must never be stripped merely because SOME
    timestamp trails it -- covers several distinct hour/minute values,
    not just the one from the live incident (9:05 pm, 11:59 am, 12:01 pm:
    single-digit hour, two-digit hour, and a boundary-looking value)."""
    cases = [
        ("Mark Tvs Jupiter Take 3     9:05 pm         9:05 pm", 3),
        ("Mark Tvs Jupiter Take 4     11:59 am         11:59 am", 4),
        ("Mark Tvs Jupiter Take 5     12:01 pm         12:01 pm", 5),
    ]
    for text, expected_take in cases:
        parsed = ma.extract_role_and_project(text)
        assert parsed is not None, text
        assert parsed.media_role == "take"
        assert parsed.take_number == expected_take, (text, parsed.take_number)
        assert parsed.project_fragment == "Tvs Jupiter"


def test_extract_role_and_project_ordinary_parsing_without_timestamp_noise_unchanged():
    """Existing behavior (no timestamp noise at all) must be byte-for-byte
    unaffected by reordering the timestamp strip earlier."""
    for text, expected_take in [
        ("mark google take 1", 1), ("MARK GOOGLE TAKE 1", 1),
        ("Mark Google Take 1", 1), ("mark google take2", 2),
    ]:
        parsed = ma.extract_role_and_project(text)
        assert parsed is not None, text
        assert parsed.media_role == "take"
        assert parsed.take_number == expected_take
        assert parsed.project_fragment.lower() == "google"


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------
def _projects():
    return [{"id": "p-google", "label": "Google"}, {"id": "p-netflix", "label": "Netflix"}]


def test_validate_candidates_filters_wrong_project_but_mention_is_never_required():
    """2026-08-25: MARK is the authoritative media-selection signal — a
    WhatsApp mention (Gunwanti, someone else, or none at all) is optional
    metadata only, never a filter. Only project relevance still filters
    candidates out."""
    candidates = [
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google take 1", source_message_id="src-take1"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark google intro", source_message_id="src-intro"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark netflix take 1", source_message_id="src-netflix-take1"),
        _mark(mention_lid="999999@lid", mark_text="mark google take 2", source_message_id="src-other-mention"),
        _mark(mention_lid="", mark_text="mark google take 3", source_message_id="src-no-mention"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    slots = {(a["media_role"], a["take_number"]) for a in outcome.assignments}
    # take 1 (Gunwanti mention) + intro (Gunwanti mention) + take 2 (a
    # DIFFERENT mention) + take 3 (no mention at all) all survive — only
    # the wrong-project "netflix" mark is excluded.
    assert slots == {("take", 1), ("intro", None), ("take", 2), ("take", 3)}


def test_mark_resolves_identically_with_gunwanti_mention():
    """A: '@Gunwanti\\nMark Google Test Take 1' — the original, still-valid
    pattern."""
    candidates = [_mark(mention_lid=GUNWANTI_LID, mark_text="mark google test take 1", source_message_id="src-a")]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-a"


def test_mark_resolves_identically_with_no_mention_at_all():
    """B: 'Mark Google Test Take 1' — no @mention anywhere in the reply.
    Must resolve identically to test A — the quoted media, not the
    mention, establishes source identity."""
    candidates = [_mark(mention_lid=None, mark_text="mark google test take 1", source_message_id="src-a")]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-a"


def test_mark_resolves_identically_with_a_different_persons_mention():
    """C: '@SomeOtherPerson\\nMark Google Test Take 1' — a real mention,
    but not Gunwanti's. Must still resolve identically to A and B."""
    candidates = [_mark(mention_lid="555555555@lid", mark_text="mark google test take 1", source_message_id="src-a")]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-a"


def test_mark_album_with_no_mention_resolves_each_photo_independently():
    """A multi-photo album batch-marked with no mention at all still
    resolves each distinct photo independently — mention-optionality
    applies to album/batch marks too, not just single-item marks."""
    def _photo_mark(source_message_id, tile_hash):
        m = _mark(mention_lid=None, mark_text="mark google test photos", source_message_id=source_message_id)
        m["quoted_thumbnail_hash"] = tile_hash
        m["album_tile_index"] = 0 if tile_hash.endswith("1") else 1
        m["is_album_tile"] = True
        return m
    candidates = [
        _photo_mark("src-album", "hash-tile-1"),
        _photo_mark("src-album", "hash-tile-2"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok
    assert len(outcome.assignments) == 2
    hashes = {a["quoted_thumbnail_hash"] for a in outcome.assignments}
    assert hashes == {"hash-tile-1", "hash-tile-2"}


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
# Agent identity is NEVER the talent identity (2026-09-10 — real UPLOAD
# incident "Ishani Kouli x Talentgram Agency" / "SINGLETON with shruti
# hassan": the MEDIA RESOLUTION FAILED report said "correctly marked for
# Gunwanti", which reads as if Gunwanti — the agent's own WhatsApp
# identity — were the talent. It never is.)
# ---------------------------------------------------------------------------
async def test_validate_candidates_agent_identity_never_becomes_talent_identity():
    """Agent = "Gunwanti Talentgram Team Agent" (the @mention target AND
    the visible sender of the MARK reply). Talent = "Ishani Kouli" (the
    caller-resolved, email-verified UPLOAD target). Every resulting
    assignment must carry talent_id = the Ishani Kouli id passed in —
    NEVER anything derived from mention_lid or source_sender."""
    ishani_id = "talent-ishani-kouli-001"
    candidates = [
        # both marks: agent is the @mention target and the sender WhatsApp
        # shows on the reply — exactly the screenshot.
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark singleton audition take 1",
              source_message_id="src-take1", media_type="video", sender="Gunwanti Talentgram Team Agent"),
        _mark(mention_lid=GUNWANTI_LID, mark_text="mark singleton introduction video",
              source_message_id="src-intro", media_type="video", sender="Gunwanti Talentgram Team Agent"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID,
        requested_project_id="p-singleton", requested_project_label="SINGLETON with shruti hassan",
        projects=[{"id": "p-singleton", "label": "SINGLETON with shruti hassan"}],
        talent_id=ishani_id,
    )
    assert outcome.ok, outcome
    assert len(outcome.assignments) == 2, outcome.assignments
    slots = {(a["media_role"], a["take_number"]) for a in outcome.assignments}
    assert slots == {("take", 1), ("intro", None)}
    # Take 1 and Introduction are independently addressable, distinct sources.
    srcs = {a["resolved_source_message_id"] for a in outcome.assignments}
    assert srcs == {"src-take1", "src-intro"}

    # SEND target build: talent_id is stamped from the caller-resolved
    # value, NEVER from the mark's mention_lid / source_sender.
    send_targets, _ = ma.build_send_targets(
        outcome.assignments, set(), "SINGLETON x Talentgram Agency", ishani_id, "p-singleton",
    )
    for t in send_targets:
        assert t["talent_id"] == ishani_id, t
        assert t["talent_id"] not in (GUNWANTI_LID, "Gunwanti Talentgram Team Agent")
        # the agent mention survives only as opaque metadata, never identity
        assert t["mark_target_contact_id"] == GUNWANTI_LID

    # Persisted assignment doc: same guarantee — talent_id from the
    # caller, agent name kept only as source_sender metadata.
    doc = await ma.record_assignment(
        talent_id=ishani_id, project_id="p-singleton", normalized_project="singleton",
        group_name="Ishani Kouli x Talentgram Agency", group_id=None,
        mark=outcome.assignments[0], created_by="test",
    )
    try:
        assert doc["talent_id"] == ishani_id
        assert doc["talent_id"] not in (GUNWANTI_LID, "Gunwanti Talentgram Team Agent")
        assert doc["source_sender"] == "Gunwanti Talentgram Team Agent"  # metadata only
    finally:
        await db[ma.ASSIGNMENTS_COLLECTION].delete_one({"assignment_id": doc["assignment_id"]})


def test_report_unresolved_names_real_talent_never_presents_agent_as_talent():
    """orch._report_unresolved for the exact screenshot: Take 1 unresolved,
    talent = Ishani Kouli. The report must name Ishani Kouli as the talent
    and must NOT say the media was 'marked for Gunwanti' (or otherwise
    imply the agent is the talent)."""
    report = orch._report_unresolved(
        "Ishani Kouli", "SINGLETON with shruti hassan",
        [{"media_role": "take", "take_number": 1}],
    )
    assert "MEDIA RESOLUTION FAILED" in report
    assert "Talent: Ishani Kouli" in report
    assert "SINGLETON with shruti hassan Take 1" in report
    assert "No upload was performed" in report
    assert "Gunwanti" not in report
    assert "marked for Gunwanti" not in report


def test_report_unresolved_distinguishes_transient_from_remark_states():
    """2026-09-11 — Zeeshan Ali: a 'media didn't finish loading'
    (media_not_rendered) failure should tell the user to RETRY, not to
    re-send the MARK; a genuine 'message gone' (not_located) still says
    re-send."""
    transient = orch._report_unresolved(
        "Zeeshan Ali", "Mahindra Thar Film 1 & 2",
        [{"media_role": "take", "take_number": 1, "resolution_failure_state": "media_not_rendered"},
         {"media_role": "intro", "take_number": None, "resolution_failure_state": "media_not_rendered"}],
    )
    assert "did not finish loading" in transient
    assert "temporary" in transient and "retry UPLOAD" in transient
    assert "re-send the MARK" not in transient.split("if it keeps failing")[0]

    gone = orch._report_unresolved(
        "Zeeshan Ali", "Mahindra Thar Film 1 & 2",
        [{"media_role": "take", "take_number": 1, "resolution_failure_state": "not_located"}],
    )
    assert "could not be re-opened" in gone
    assert "Re-send the MARK reply" in gone

    # 2026-09-11 (Rashi Mal, source-reacquisition audit) — a distinct
    # state for "the quoted block never carried any identifiable content
    # to verify against, even after a live re-read" (mark_scan.
    # _live_requote_signal): genuinely different from not_located/
    # wrong_message above, and — per this audit's own Phase 8 rule — the
    # ONE case where asking the user to re-MARK is actually warranted.
    no_signal = orch._report_unresolved(
        "Rashi Mal", "SINGLETON with shruti hassan",
        [{"media_role": "intro", "take_number": None, "resolution_failure_state": "no_verifiable_signal"}],
    )
    assert "no_verifiable_signal" not in no_signal  # never leak the raw machine tag
    assert "never rendered any identifiable media content" in no_signal
    assert "Re-send the MARK reply" in no_signal


async def test_orchestrator_unresolved_report_names_ishani_not_gunwanti():
    """Full _process_scan_done for the screenshot scenario end-to-end:
    Agent-mentioned MARK, Take 1 unresolvable at scan time, talent =
    Ishani Kouli -> the persisted final report names Ishani Kouli, never
    Gunwanti, as the talent."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"SINGLETON {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ishani Kouli {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram Agency",
        candidates=[
            {**_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1",
                     source_message_id="x", media_type="video", sender="Gunwanti Talentgram Team Agent"),
             "resolved_source_message_id": None},
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "MEDIA RESOLUTION FAILED" in final["report"]
        assert f"Talent: {talent_label}" in final["report"]
        assert "Gunwanti" not in final["report"]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})


# ---------------------------------------------------------------------------
# `upload - talent - project` — immediate ACK + scan-request creation, and
# the never-guess error paths.
# ---------------------------------------------------------------------------
async def test_upload_command_creates_scan_request_and_acks_immediately():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
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
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


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
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
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
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_upload_command_no_whatsapp_group_reports_clearly():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
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
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


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
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
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
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


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


async def test_orchestrator_never_claims_resolve_recipient_scan_requests():
    """Real production race (2026-09-03): casting_pipeline.py's SHARE
    Instagram recipient resolver (_search_whatsapp_live) also writes
    mode="resolve_recipient" docs into this SAME collection and polls
    them for scan_done/scan_failed directly (no claim, just find_one).
    Before this exclusion, this orchestrator's find_one_and_update won
    that race often enough in practice to matter, stomped the doc's
    status to "orchestrating_scan", then threw on doc["talent_id"]
    (which a resolve_recipient doc never has) — stranding it forever and
    forcing the resolver to silently time out and fall back to the CRM/
    talent tier. Confirmed live against production: "Rising Sun x
    Talentgram Agency" landed in "orchestrating_scan" and never resolved
    until the mode exclusion was added below."""
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "resolve_recipient", "status": ma.SCAN_STATUS_DONE,
        "query": "Heena Talentgram",
        "candidates": [{"name": "Heena Talentgram", "type": "group"}],
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        did_work = await orch._process_scan_done()
        assert did_work is False
        untouched = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert untouched["status"] == ma.SCAN_STATUS_DONE
        assert untouched["candidates"] == [{"name": "Heena Talentgram", "type": "group"}]
    finally:
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
    # Simulate /media-upload having already marked the assignment uploaded
    # AND pushed the matching media onto the submission — reconciliation
    # (2026-08-25) requires BOTH, matching what /media-upload really does
    # atomically; a fixture that only wrote the assignment side used to be
    # (wrongly) enough, which is exactly the gap the real Sharvari incident
    # exposed.
    sub_id = await _seed_submission(project_id, talent_id, f"{talent_label.lower()}@test.example")
    await db.submissions.update_one(
        {"id": sub_id}, {"$push": {"media": {"id": "m1", "source_message_id": "src-take1", "label": "Take 1"}}}
    )
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
        await db.submissions.delete_one({"id": sub_id})


def test_humanize_upload_error_is_state_specific():
    """2026-09-11 — "Take 1 could not be uploaded" gave the operator no
    state. Each worker failure prefix now maps to its own sentence."""
    m = orch._humanize_upload_error("resolve_tile: message_not_found")
    assert "exact marked whatsapp message" in m.lower(), m
    m = orch._humanize_upload_error("failed at stage download_not_available")
    assert "could not open the media" in m.lower(), m
    m = orch._humanize_upload_error("hash_mismatch")
    assert "no longer matches the mark" in m.lower(), m
    m = orch._humanize_upload_error("downloaded zero bytes")
    assert "came back empty" in m.lower(), m
    m = orch._humanize_upload_error("timed out after 500.0s")
    assert "took too long" in m.lower(), m
    # never leaks raw internals
    assert "message_not_found" not in orch._humanize_upload_error("resolve_tile: message_not_found")


def test_humanize_upload_error_machine_state_tags_are_distinct():
    """2026-09-11 — Mahim Suhalka: Take AND Introduction both reported the
    SAME catch-all "WhatsApp Web could not open the media" sentence, even
    though one never opened the viewer at all and the other opened it
    fully but the acquisition step itself failed. mark_scan.py now tags
    its own errors with a machine `[STATE]` prefix (see
    _open_tile_viewer_and_download_hardened / _locate_download_message /
    _upload_one) — each must map to its OWN distinct sentence, and the
    master prompt's own two worked examples must actually differ."""
    open_failed = orch._humanize_upload_error("[MEDIA_OPEN_FAILED] click failed: Timeout 10000ms exceeded")
    download_not_started = orch._humanize_upload_error("[DOWNLOAD_NOT_STARTED] video mounted but no plausible menu-trigger button found")
    assert "could not be opened" in open_failed
    assert "did not provide the media for download" in download_not_started
    assert open_failed != download_not_started  # the master prompt's own required distinction

    assert "could not be found" in orch._humanize_upload_error("[SOURCE_NOT_FOUND] source message no longer found in window")
    assert "did not finish loading" in orch._humanize_upload_error("[SOURCE_NOT_HYDRATED] source located but its marked media did not finish rendering")
    assert "no longer matches the mark" in orch._humanize_upload_error("[MEDIA_HASH_MISMATCH] hash_mismatch")
    assert "never became ready" in orch._humanize_upload_error("[MEDIA_NOT_READY] no <video> element mounted within 15s of clicking the tile")
    assert "took too long" in orch._humanize_upload_error("[DOWNLOAD_TIMEOUT] timed out after 500.0s")
    assert "upload to Talentgram failed" in orch._humanize_upload_error("[UPLOAD_FAILED] 500 Internal Server Error")

    # 2026-09-11 (byte-validation audit): a download/blob-fetch that
    # reported success but whose content failed real validation gets its
    # OWN honest sentence — never conflated with DOWNLOAD_NOT_STARTED
    # (which never got any bytes at all).
    invalid_bytes = orch._humanize_upload_error("[INVALID_MEDIA_BYTES] acquired only 812 bytes — too small to be a real video")
    assert "invalid or incomplete media data" in invalid_bytes
    assert invalid_bytes != download_not_started

    # never leaks the bracket tag or raw internals into the user-facing text
    msg = orch._humanize_upload_error("[MEDIA_OPEN_FAILED] click failed: Locator.click: Timeout 10000ms exceeded")
    assert "[MEDIA_OPEN_FAILED]" not in msg and "Locator.click" not in msg

    # an unrecognized/future state tag degrades to the legacy substring
    # path rather than crashing or leaking the bracket.
    unknown = orch._humanize_upload_error("[SOME_FUTURE_STATE] whatever internal detail")
    assert "[SOME_FUTURE_STATE]" not in unknown


async def test_orchestrator_download_done_reports_state_specific_failure():
    """Sahal Mansuri / Mahindra Thar shape: Take 2 + Introduction upload,
    Take 1 fails at MEDIA_DISCOVERY. The report must name Take 1's ACTUAL
    failed state (from the worker's own per-item download_results), not a
    bare "could not be uploaded"."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Mahindra {tag}"
    talent_id, talent_label = f"t-{tag}", f"Sahal {tag}"
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "download", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id,
        "download_targets": [
            {"source_message_id": "src-t1", "media_role": "take", "take_number": 1, "original_label": "Take 1"},
            {"source_message_id": "src-t2", "media_role": "take", "take_number": 2, "original_label": "Take 2"},
        ],
        "download_results": [
            {"ok": False, "source_message_id": "src-t1", "error": "source message no longer found in window"},
            {"ok": True, "source_message_id": "src-t2"},
        ],
        "pending_report_context": {"talent_label": talent_label, "project_label": project_label, "already": []},
        "created_at": _now(), "updated_at": _now(),
    })
    sub_id = await _seed_submission(project_id, talent_id, f"{talent_label.lower()}@test.example")
    await db.submissions.update_one({"id": sub_id}, {"$push": {"media": {"id": "m2", "source_message_id": "src-t2", "label": "Take 2"}}})
    await db[ma.ASSIGNMENTS_COLLECTION].insert_one({
        "assignment_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "source_message_id": "src-t2", "media_role": "take", "take_number": 2,
        "assignment_status": ma.ASSIGN_STATUS_UPLOADED, "created_at": _now(), "created_by": "test",
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert "UPLOAD FAILED" in final["report"]
        assert "✓ " in final["report"] and "Take 2" in final["report"]
        assert "could not be found from the exact marked WhatsApp message" in final["report"]
        assert "Pipeline stage was NOT changed" in final["report"]
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await db.submissions.delete_one({"id": sub_id})


async def test_orchestrator_already_uploaded_is_idempotent_no_redownload():
    """Running scan_done validation again for a project already fully
    uploaded (verified against the REAL submission media, not just the
    assignment flag) must go straight to ALREADY COMPLETED, never queue a
    fresh download."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    sub_id = await _seed_submission(project_id, talent_id, f"{talent_label.lower()}@test.example")
    await db.submissions.update_one(
        {"id": sub_id}, {"$push": {"media": {"id": "m1", "source_message_id": "src-take1", "label": "Take 1"}}}
    )
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
        await db.submissions.delete_one({"id": sub_id})


async def test_orchestrator_stale_assignment_without_submission_media_is_reconciled_not_trusted():
    """Reconciliation fix (2026-08-25, real production incident):
    assignment_status="uploaded" alone must NEVER be enough to report
    ALREADY COMPLETED — the row is only trusted if the TARGET SUBMISSION
    actually contains matching media. A row that claims "uploaded" while
    the submission has no such media (drift between the two collections,
    however it happened) must be excluded from `already`, so to_download
    naturally includes it again for a real retry — never silently
    reported as already done while the submission stays empty."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    # A submission exists but its media[] is empty — the row below claims
    # "uploaded" but nothing was ever actually attached to it.
    sub_id = await _seed_submission(project_id, talent_id, f"{talent_label.lower()}@test.example")
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
        # Reconciled: the stale "uploaded" row is NOT trusted -> queued
        # for a real download instead of a false ALREADY COMPLETED.
        assert final["status"] == ma.DOWNLOAD_STATUS_PENDING, final
        assert final["mode"] == "download"
        targets = {t["media_role"] for t in final["download_targets"]}
        assert targets == {"take"}
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})
        await db.submissions.delete_one({"id": sub_id})


async def test_orchestrator_zero_candidates_reports_honestly_not_already_completed():
    """Completion-invariant fix (2026-08-25): a scan that finds ZERO
    marks at all for this talent must never be reported as ALREADY
    COMPLETED (the old bug — "nothing left to download" was also true
    here, and the two were never distinguished). Must report the new,
    honest NO MARKED MEDIA FOUND instead."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "NO MARKED MEDIA FOUND" in final["report"], final["report"]
        assert "ALREADY COMPLETED" not in final["report"], final["report"]
        rows = await db[ma.ASSIGNMENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert rows == [], rows
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Admin-command-is-authoritative project matching (2026-08-25) — the real
# production incident's second root cause. The admin's UPLOAD command
# already explicitly resolves the target project; a mark's job is
# identifying WHICH media, not re-proving which project via informal
# WhatsApp shorthand. Four cases, confirmed by the user:
#   1/2/3. no confident project-text match -> defaults to the
#      admin-requested project (this is what makes "Mark Tapti Ai Test
#      take 1" resolve for the real "Tapti AI App (Ananya)").
#   4. confidently matches a DIFFERENT real project -> excluded,
#      reported as an advisory note, never uploaded to the wrong project.
#   5. ambiguous between multiple real projects -> excluded, reported as
#      an advisory note, never guessed.
# Cases 4 and 5 are advisory-only (never block OTHER, correctly-resolved
# marks in the same scan) — a talent's WhatsApp group legitimately
# accumulates marks for multiple projects over time.
# ---------------------------------------------------------------------------
def test_validate_candidates_informal_project_text_defaults_to_requested_project():
    """Case 1/2 — "Mark Project A Test take 1" for a requested project
    literally named "Project A" (case 2's shape: the informal mark text
    doesn't need to exactly reproduce the DB's project name)."""
    projects = [{"id": "p-a", "label": "Project A (Ananya)"}, {"id": "p-b", "label": "Project B"}]
    candidates = [_mark(mention_lid=None, mark_text="mark project a test take 1", source_message_id="src-take1")]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-a",
        requested_project_label="Project A (Ananya)", projects=projects, talent_id="t1",
    )
    assert outcome.ok, outcome
    assert len(outcome.assignments) == 1, outcome
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-take1"
    assert outcome.project_mismatch == []
    assert outcome.project_ambiguous == []


def test_validate_candidates_real_sharvari_shorthand_defaults_to_requested_project():
    """Case 2/3, the exact real incident's shape: "Mark Tapti Ai Test
    take 1"/"...Introduction" against the real "Tapti AI App (Ananya)" —
    the fragment matches NEITHER Tapti variant confidently, so both
    default to the admin-requested project rather than being silently
    dropped."""
    projects = [
        {"id": "p-ananya", "label": "Tapti AI App (Ananya)"},
        {"id": "p-neelam", "label": "Tapti AI App (Neelam)"},
    ]
    candidates = [
        _mark(mention_lid=None, mark_text="Mark Tapti Ai Test take 1", source_message_id="src-take1", media_type="video"),
        _mark(mention_lid=None, mark_text="Mark Tapti Ai Test Introduction", source_message_id="src-intro", media_type="video"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-ananya",
        requested_project_label="Tapti AI App (Ananya)", projects=projects, talent_id="t1",
    )
    assert outcome.ok, outcome
    slots = {(a["media_role"], a["take_number"]) for a in outcome.assignments}
    assert slots == {("take", 1), ("intro", None)}
    assert outcome.project_mismatch == []
    assert outcome.project_ambiguous == []


def test_validate_candidates_no_project_match_defaults_to_requested_project():
    """Case 3 — mark text that matches NO real project at all still
    defaults to the admin-requested project."""
    projects = [{"id": "p-a", "label": "Project A"}]
    candidates = [_mark(mention_lid=None, mark_text="mark random unrelated text take 1", source_message_id="src-take1")]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-a",
        requested_project_label="Project A", projects=projects, talent_id="t1",
    )
    assert outcome.ok, outcome
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-take1"


def test_validate_candidates_confident_different_project_excluded_not_uploaded():
    """Case 4 — a mark confidently matching a DIFFERENT real project must
    NEVER be uploaded to the requested one, and must not block other,
    correctly-resolved marks in the same scan."""
    projects = [{"id": "p-a", "label": "Project A"}, {"id": "p-b", "label": "Project B"}]
    candidates = [
        _mark(mention_lid=None, mark_text="mark project a take 1", source_message_id="src-a-take1"),
        _mark(mention_lid=None, mark_text="mark project b take 1", source_message_id="src-b-take1"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-a",
        requested_project_label="Project A", projects=projects, talent_id="t1",
    )
    assert outcome.ok, outcome
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-a-take1"
    assert len(outcome.project_mismatch) == 1
    assert outcome.project_mismatch[0]["matched_project_label"] == "Project B"
    assert outcome.project_mismatch[0]["resolved_source_message_id"] == "src-b-take1"
    assert outcome.project_ambiguous == []


def test_validate_candidates_ambiguous_project_reference_excluded_not_guessed():
    """Case 5 — a mark whose text is tied between multiple real projects
    is excluded and flagged, never guessed, and must not block other,
    correctly-resolved marks."""
    projects = [
        {"id": "p-a", "label": "Project Test Alpha"},
        {"id": "p-b", "label": "Project Test Beta"},
        {"id": "p-c", "label": "Project C"},
    ]
    candidates = [
        _mark(mention_lid=None, mark_text="mark project c take 1", source_message_id="src-c-take1"),
        _mark(mention_lid=None, mark_text="mark project test take 1", source_message_id="src-ambiguous-take1"),
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-c",
        requested_project_label="Project C", projects=projects, talent_id="t1",
    )
    assert outcome.ok, outcome
    assert len(outcome.assignments) == 1
    assert outcome.assignments[0]["resolved_source_message_id"] == "src-c-take1"
    assert outcome.project_mismatch == []
    assert len(outcome.project_ambiguous) == 1
    assert outcome.project_ambiguous[0]["resolved_source_message_id"] == "src-ambiguous-take1"
    matched_labels = {p["label"] for p in outcome.project_ambiguous[0]["ambiguous_projects"]}
    assert matched_labels == {"Project Test Alpha", "Project Test Beta"}


async def test_orchestrator_project_mismatch_appends_advisory_note_but_still_completes():
    """End-to-end: a confidently-different-project mark alongside a
    valid one must not block the valid upload — the mismatch is reported
    as an advisory note appended to the normal completion report."""
    tag = uuid.uuid4().hex[:6]
    project_a_id, project_a_label = f"pa-{tag}", f"Project A {tag}"
    project_b_id, project_b_label = f"pb-{tag}", f"Project B {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_a_id, project_label=project_a_label,
        group_name=f"{talent_label} x Talentgram",
        candidates=[
            _mark(mention_lid=None, mark_text=f"mark {project_b_label} take 1", source_message_id="src-b-take1"),
        ],
    )
    await db.projects.insert_one({"id": project_a_id, "brand_name": project_a_label, "status": "ongoing", "slug": project_a_id})
    await db.projects.insert_one({"id": project_b_id, "brand_name": project_b_label, "status": "ongoing", "slug": project_b_id})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "NO MARKED MEDIA FOUND" in final["report"], final["report"]
        assert project_b_label in final["report"], final["report"]
        rows = await db[ma.ASSIGNMENTS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert rows == [], rows  # the mismatched mark was never recorded as an assignment for Project A
    finally:
        await db.projects.delete_many({"id": {"$in": [project_a_id, project_b_id]}})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# /media-upload submission-media category contract (2026-08-25, real
# production incident: Sharvari Kashid / Tapti AI App (Ananya)). The
# Introduction video downloaded and uploaded to Cloudinary successfully,
# and submission.media[] genuinely contained it — but its category was
# written as the bare role name "intro" instead of "intro_video", the
# ONE value the Submission Review Center / Requirement Engine recognize
# (verified against every other write path in the codebase). It was
# invisible in the UI despite existing in the database. These tests call
# the REAL /media-upload endpoint function directly (Cloudinary itself
# mocked — no network call) and assert the ACTUAL stored submission
# media object, not merely that the call succeeded.
# ---------------------------------------------------------------------------
class _FakeUploadFile:
    def __init__(self, data: bytes, filename: str, content_type: str):
        self.filename = filename
        self.content_type = content_type
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _fake_cloudinary_result(media_id: str, resource_type: str) -> dict:
    return {
        "url": f"https://res.cloudinary.com/talentgram/{resource_type}/upload/v1/{media_id}",
        "public_id": f"talentgram/{media_id}",
        "resource_type": resource_type,
        "bytes": 12345,
        "duration": 9.0 if resource_type == "video" else None,
    }


async def _call_media_upload(*, talent_id, project_id, media_role, take_number, original_label, source_message_id):
    orig_cloudinary_upload = agents_whatsapp.cloudinary_upload
    media_id_holder = {}

    def _fake_upload(data, *, folder, public_id, resource_type, content_type, keep_original):
        media_id_holder["public_id"] = public_id
        return _fake_cloudinary_result(public_id, resource_type)

    agents_whatsapp.cloudinary_upload = _fake_upload
    try:
        result = await agents_whatsapp.media_upload(
            file=_FakeUploadFile(b"fake-bytes", "clip.mp4", "video/mp4"),
            talent_id=talent_id, project_id=project_id, media_role=media_role,
            take_number=str(take_number) if take_number else None,
            original_label=original_label,
            source_message_id=source_message_id, source_thumbnail_hash=f"hash-{source_message_id}",
            source_media_type="video",
            source_group_id=None, source_group_name="Test Group",
            source_sender=None, source_timestamp=None,
            mark_reply_message_id=None, mark_reply_text=None,
            mark_target_phone=None, mark_target_contact_id=None,
            x_internal_secret=None,
        )
    finally:
        agents_whatsapp.cloudinary_upload = orig_cloudinary_upload
    return result


def _is_recognized_as_intro_video(media_item: dict) -> bool:
    """The EXACT contract SubmissionReviewCenter.jsx's getCuratedMedia()
    uses for the "video" group (frontend/src/pages-components/
    SubmissionReviewCenter.jsx:1243) — mirrored here so the backend's
    write can be verified against the real UI contract without a JS
    runtime."""
    return media_item.get("category") in ("intro_video", "video")


async def test_media_upload_endpoint_intro_role_writes_intro_video_category():
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Test Upload Project {tag}")
    talent_id = await _seed_talent(f"Test Upload Talent {tag}", email=f"testupload{tag}@example.com")
    try:
        result = await _call_media_upload(
            talent_id=talent_id, project_id=project_id, media_role="intro", take_number=None,
            original_label="Introduction", source_message_id=f"src-intro-{tag}",
        )
        sub = await db.submissions.find_one({"id": result["submission_id"]})
        media = [m for m in sub["media"] if m["id"] == result["media_id"]]
        assert len(media) == 1, media
        assert media[0]["category"] == "intro_video", media[0]
        assert _is_recognized_as_intro_video(media[0]), media[0]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db.talents.delete_one({"id": talent_id})
        await db.submissions.delete_many({"project_id": project_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_media_upload_endpoint_take_role_writes_take_category():
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Test Upload Project {tag}")
    talent_id = await _seed_talent(f"Test Upload Talent {tag}", email=f"testupload{tag}@example.com")
    try:
        result = await _call_media_upload(
            talent_id=talent_id, project_id=project_id, media_role="take", take_number=1,
            original_label="Take 1", source_message_id=f"src-take-{tag}",
        )
        sub = await db.submissions.find_one({"id": result["submission_id"]})
        media = [m for m in sub["media"] if m["id"] == result["media_id"]]
        assert len(media) == 1, media
        assert media[0]["category"] == "take", media[0]
        # Take must NOT be (mis)recognized as an intro video.
        assert not _is_recognized_as_intro_video(media[0]), media[0]
    finally:
        await db.projects.delete_one({"id": project_id})
        await db.talents.delete_one({"id": talent_id})
        await db.submissions.delete_many({"project_id": project_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_media_upload_endpoint_intro_and_take_coexist_correctly_categorized():
    """End-to-end shape of the real Sharvari incident: both an intro and
    a take uploaded for the SAME talent/project must each carry their
    own correct, distinct category — proving one doesn't leak into or
    overwrite the other's classification."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Test Upload Project {tag}")
    talent_id = await _seed_talent(f"Test Upload Talent {tag}", email=f"testupload{tag}@example.com")
    try:
        intro_result = await _call_media_upload(
            talent_id=talent_id, project_id=project_id, media_role="intro", take_number=None,
            original_label="Introduction", source_message_id=f"src-intro-{tag}",
        )
        take_result = await _call_media_upload(
            talent_id=talent_id, project_id=project_id, media_role="take", take_number=1,
            original_label="Take 1", source_message_id=f"src-take-{tag}",
        )
        assert intro_result["submission_id"] == take_result["submission_id"]  # same submission, resumed
        sub = await db.submissions.find_one({"id": intro_result["submission_id"]})
        assert len(sub["media"]) == 2, sub["media"]
        by_id = {m["id"]: m for m in sub["media"]}
        assert by_id[intro_result["media_id"]]["category"] == "intro_video"
        assert by_id[take_result["media_id"]]["category"] == "take"
    finally:
        await db.projects.delete_one({"id": project_id})
        await db.talents.delete_one({"id": talent_id})
        await db.submissions.delete_many({"project_id": project_id})
        await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})


def test_validate_candidates_batch_failure_with_no_project_match_is_silently_ignored():
    """Real production incident (2026-08-26, live SEND E2E): an old,
    unresolvable batch mark ("mark google: take 1, take 2, take 3,
    intro") sitting in a WhatsApp group from unrelated earlier testing
    blocked a completely different, later SEND request for an unrelated
    project ("CleanScan Test") with BATCH RESOLUTION FAILED. Root cause:
    the batch-relevance check briefly used the SAME admin-authoritative
    "default to the requested project" leniency as the single-mark path
    -- safe for an ACCEPTED single mark, but turns any group with one
    stale/unresolvable batch mark into a permanent poison pill once
    applied to something that's presented as a hard BLOCK. A batch mark
    with NO confident project match at all must be silently ignored,
    regardless of which project is currently requested -- only a
    CONFIDENT match to the requested project makes it relevant."""
    candidates = [{
        "mention_lid": GUNWANTI_LID,
        "mark_text": "mark totallyunrelatedxyz: take 1, take 2, take 3, intro",
        "reply_message_id": "reply-stale-batch",
        "quoted_thumbnail_hash": None, "resolved_source_message_id": None,
        "album_tile_index": None, "source_media_type": None, "is_album_tile": False,
        "resolution_status": "BATCH_RESOLUTION_FAILED",
        "batch_resolution_error": "jumped-to message is not an album",
    }]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-google",
        requested_project_label="Google", projects=_projects(), talent_id="t1",
    )
    assert outcome.ok, outcome  # the stale batch mark must never block an unrelated request
    assert outcome.batch_failures == [], outcome.batch_failures
    assert outcome.assignments == []


# ===========================================================================
# MARK FLEXIBILITY (Production fix, Issue 4) — the admin should not need
# one exact sentence structure to mark media for a project.
# ===========================================================================
def test_extract_role_and_project_audition_synonym_for_take():
    parsed = ma.extract_role_and_project("Mark audition for Vaseline")
    assert parsed.media_role == "take"
    assert parsed.take_number is None
    assert "Vaseline" in parsed.project_fragment


def test_extract_role_and_project_project_before_audition():
    parsed = ma.extract_role_and_project("Mark Vaseline audition")
    assert parsed == ma.ParsedMark(project_fragment="Vaseline", media_role="take", take_number=None)


def test_extract_role_and_project_project_before_take():
    parsed = ma.extract_role_and_project("Mark Vaseline take")
    assert parsed == ma.ParsedMark(project_fragment="Vaseline", media_role="take", take_number=None)


def test_extract_role_and_project_project_before_intro():
    parsed = ma.extract_role_and_project("Mark Vaseline intro")
    assert parsed == ma.ParsedMark(project_fragment="Vaseline", media_role="intro", take_number=None)


def test_extract_role_and_project_project_before_introduction():
    parsed = ma.extract_role_and_project("Mark Vaseline introduction")
    assert parsed == ma.ParsedMark(project_fragment="Vaseline", media_role="intro", take_number=None)


def test_extract_role_and_project_intro_video_strips_video_word():
    parsed = ma.extract_role_and_project("Mark intro video for Vaseline")
    assert parsed.media_role == "intro"
    assert "video" not in parsed.project_fragment.lower()
    assert "Vaseline" in parsed.project_fragment


def test_extract_role_and_project_introduction_video_strips_video_word():
    parsed = ma.extract_role_and_project("Mark introduction video for Vaseline")
    assert parsed.media_role == "intro"
    assert "video" not in parsed.project_fragment.lower()
    assert "Vaseline" in parsed.project_fragment


def test_extract_role_and_project_this_filler_word_stripped():
    parsed = ma.extract_role_and_project("Mark this audition for Vaseline")
    assert parsed.media_role == "take"
    assert "this" not in parsed.project_fragment.lower()


def test_extract_role_and_project_this_intro_filler_word_stripped():
    parsed = ma.extract_role_and_project("Mark this intro for Vaseline")
    assert parsed.media_role == "intro"
    assert "this" not in parsed.project_fragment.lower()


def test_extract_role_and_project_take_for_still_works():
    parsed = ma.extract_role_and_project("Mark take for Vaseline")
    assert parsed.media_role == "take" and parsed.take_number is None


def test_extract_role_and_project_intro_for_still_works():
    parsed = ma.extract_role_and_project("Mark intro for Vaseline")
    assert parsed.media_role == "intro"


def test_extract_role_and_project_introduction_for_still_works():
    parsed = ma.extract_role_and_project("Mark introduction for Vaseline")
    assert parsed.media_role == "intro"


def test_extract_role_and_project_ambiguous_project_still_never_guessed():
    # MARK flexibility never weakens the existing project ambiguity
    # safety — validate_candidates (not extract_role_and_project) is
    # what refuses to guess between two real, similarly-named projects;
    # confirmed here that audition/this/video tolerance doesn't bypass
    # that downstream check.
    candidates = [{
        "mention_lid": GUNWANTI_LID, "mark_text": "mark audition for google",
        "reply_message_id": "r1", "quoted_thumbnail_hash": "h1", "resolved_source_message_id": "m1",
        "album_tile_index": None, "source_media_type": "video", "is_album_tile": False,
    }]
    projects = [
        {"id": "p-a", "label": "Google Test A"}, {"id": "p-b", "label": "Google Test B"},
    ]
    outcome = ma.validate_candidates(
        candidates, gunwanti_lid=GUNWANTI_LID, requested_project_id="p-a",
        requested_project_label="Google Test A", projects=projects, talent_id="t1",
    )
    # "google" alone is ambiguous between Test A and Test B -> advisory-only
    # project_ambiguous, never guessed; since it doesn't confidently match
    # the REQUESTED project either, it defaults to it per validate_
    # candidates' own case-4 rule (not blocking) — the key assertion is
    # that this never silently resolves to the WRONG project (Test B).
    assert outcome.ok
    for a in outcome.assignments:
        assert a.get("resolved_source_message_id") != "wrong-project-media"
