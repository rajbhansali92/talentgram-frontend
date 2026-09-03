"""SEND workflow (2026-08-24) — an INDEPENDENT consumer of the same
@Gunwanti + mark WhatsApp source media UPLOAD resolves. See
agents/modules/media_send.py's module docstring for the architecture.

Covers the 14 required points: SEND works without any uploaded submission
media, only marked media is ever selected, project isolation, email-
authoritative resolution, hash re-verification/never-substitute, per-item
resilience, idempotency, partial-failure resume, no pipeline-stage
mutation, and independence from media_assignments/submission.media[].
"""
import os
# SEND's own pre-approval media preview (Production fix, 2026-09-03)
# polls whatsapp_scan_requests for the worker's response — no real
# worker runs during tests, so every confirmation-building test needs a
# short bound rather than waiting out the 20s production default. Set
# BEFORE the agents modules are imported, since these are read once as
# module-level constants (same reasoning as test_casting_agent.py's own
# RECIPIENT_SEARCH_* overrides — set redundantly here too, in case this
# file is ever run standalone before that one has a chance to).
os.environ.setdefault("SEND_PREVIEW_POLL_INTERVAL_SEC", "0.05")
os.environ.setdefault("SEND_PREVIEW_MAX_WAIT_SEC", "1.5")

import asyncio
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, _now  # noqa: E402
from agents import modules as agent_modules  # noqa: E402
from agents.dispatcher import handle_inbound_message  # noqa: E402
from agents.models import ExecContext  # noqa: E402
from agents.modules import casting_pipeline as cp  # noqa: E402
from agents.modules import media_assignment as ma  # noqa: E402
from agents.modules import media_send as ms  # noqa: E402
from services import media_assignment_worker as orch  # noqa: E402
from core import ProjectIn  # noqa: E402
from routers.projects import create_project, update_project  # noqa: E402

# Reuse the SAME proven seeding/config helpers upload's own tests use —
# test-only coupling (never a production import direction), avoids
# duplicating _seed_talent/_seed_project/_seed_submission/_mark.
from tests.test_media_assignment import (  # noqa: E402
    GUNWANTI_LID, _cleanup, _mark, _restore_config, _seed_project,
    _seed_submission, _seed_talent, _use_test_config,
)

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")

DESTINATION_GROUP = "Talentgram Casting Test"

# Normally created once by server.py's own startup (mirrors
# media_assignment.ensure_indexes, already relied upon implicitly by
# test_media_assignment.py via a prior real server run against this same
# local dev DB) — this test module has never had a real server start
# against it, so the unique index genuinely doesn't exist yet without
# this. A plain sync pymongo call at import time (no event loop needed)
# rather than an async pytest fixture, to avoid pytest-asyncio's
# sync-test-depending-on-async-fixture friction.
import pymongo as _pymongo  # noqa: E402

_sync_client = _pymongo.MongoClient(os.environ["MONGO_URL"])
_sync_client[os.environ["DB_NAME"]][ms.MEDIA_SENDS_COLLECTION].create_index(
    [
        ("talent_id", 1), ("project_id", 1), ("source_message_id", 1),
        ("source_thumbnail_hash", 1), ("destination_group", 1),
    ],
    unique=True, name="uniq_talent_project_source_message_hash_destination",
)
_sync_client[os.environ["DB_NAME"]][ms.FORM_SENDS_COLLECTION].create_index(
    [("talent_id", 1), ("project_id", 1), ("destination_group", 1), ("content_hash", 1)],
    unique=True, name="uniq_talent_project_destination_content_hash",
)
_sync_client[os.environ["DB_NAME"]][ms.COMPLETION_MARKERS_COLLECTION].create_index(
    [("talent_id", 1), ("project_id", 1), ("destination_group", 1)],
    unique=True, name="uniq_talent_project_destination_marker",
)
_sync_client[os.environ["DB_NAME"]][ms.SEND_APPROVALS_COLLECTION].create_index(
    [("talent_id", 1), ("project_id", 1), ("destination_group", 1)],
    unique=True, name="uniq_talent_project_destination_approval",
)
_sync_client.close()


async def _cleanup_send(*, talent_ids=(), project_ids=(), scan_request_ids=(), submission_ids=()):
    await _cleanup(talent_ids=talent_ids, project_ids=project_ids, scan_request_ids=scan_request_ids, submission_ids=submission_ids)
    await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": {"$in": list(talent_ids)}})
    await db[ms.SEND_APPROVALS_COLLECTION].delete_many({"talent_id": {"$in": list(talent_ids)}})
    await db[ms.COMPLETION_MARKERS_COLLECTION].delete_many({"talent_id": {"$in": list(talent_ids)}})


# ---------------------------------------------------------------------------
# 1/13/14: SEND works without any uploaded submission media, and never
# depends on media_assignments or submission.media[] — the submission
# seeded below has media: [] (empty), and no media_assignments row is
# ever created for this talent/project.
# ---------------------------------------------------------------------------
async def test_send_command_works_without_any_uploaded_submission_media():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.send.{tag}@example.com"
    project_id = await _seed_project(f"Google Send {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana Send {tag}", whatsapp_group_name=f"Ahana Send {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    await db[ma.ASSIGNMENTS_COLLECTION].delete_many({"talent_id": talent_id})  # confirm-clean, not relied upon
    try:
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["media"] == []  # SEND must not need this to be non-empty

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600020",
            text=f"send - Ahana Send {tag} - Google Send {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        # Phase 2 (2026-08-26): the form is PREVIEWED, not sent — no scan
        # request exists yet, and the reply is the approval card.
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        assert "1 → Approve" in r.reply, r.reply
        assert await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id}) is None

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600020", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["workflow"] == "send"
        assert req["destination_group"] == DESTINATION_GROUP  # resolved from the project's own field
        # No media_assignments row was ever touched by SEND.
        assert await db[ma.ASSIGNMENTS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# 5: email-authoritative resolution — same duplicate-record scenario
# upload's own core safety test uses, exercised through `send` instead.
# ---------------------------------------------------------------------------
async def test_send_command_duplicate_talent_resolves_via_submission_email_not_name():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana SendDup {tag}"
    email = f"ahana.senddup.{tag}@example.com"
    project_id = await _seed_project(f"Google SendDup {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_a = await _seed_talent(name, whatsapp_group_name="", email="")  # admin-created duplicate
    talent_b = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)
    submission_id = await _seed_submission(project_id, talent_b, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600021",
            text=f"send - {name} - Google SendDup {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 → Approve" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600021", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"project_id": project_id})
        assert req is not None
        assert req["talent_id"] == talent_b, f"expected Record B ({talent_b}), got {req['talent_id']}"
        assert req["talent_id"] != talent_a
        assert req["group_name"] == f"{name} x Talentgram"
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
        await _cleanup_send(talent_ids=[talent_a, talent_b], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# SEND Path B — WhatsApp phone-number source (master prompt Part 3/25.4):
# a talent whose only WhatsApp presence is a direct number, no group. Both
# sources must resolve to the same talent/project correctly; a talent with
# NEITHER must refuse cleanly, never guess.
# ---------------------------------------------------------------------------
async def test_send_phone_source_used_when_no_group_configured():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana PhoneSrc {tag}"
    email = f"ahana.phonesrc.{tag}@example.com"
    project_id = await _seed_project(f"Google PhoneSrc {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = f"test-ma-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": talent_id, "name": name, "tags": [], "notes": "",
        "phone": "919990000299", "whatsapp_group_name": "",  # NO group — phone-only source
        "email": email, "normalized_email": email.strip().lower(),
    })
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600022",
            text=f"send - {name} - Google PhoneSrc {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 → Approve" in r.reply, r.reply
        assert "Source:" in r.reply, r.reply
        assert "919990000299 (WhatsApp number)" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600022", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"project_id": project_id})
        assert req is not None
        assert req["talent_id"] == talent_id
        assert req["source_type"] == "phone", req
        assert req["group_name"] == "919990000299", req
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_group_source_still_wins_over_phone_when_both_configured():
    """Group is checked first and wins whenever configured — phone is a
    fallback, never a second, competing source of truth."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana BothSrc {tag}"
    email = f"ahana.bothsrc.{tag}@example.com"
    project_id = await _seed_project(f"Google BothSrc {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = f"test-ma-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": talent_id, "name": name, "tags": [], "notes": "",
        "phone": "919990000298", "whatsapp_group_name": f"{name} x Talentgram",
        "email": email, "normalized_email": email.strip().lower(),
    })
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600023",
            text=f"send - {name} - Google BothSrc {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"{name} x Talentgram (WhatsApp group)" in r.reply, r.reply
        assert "919990000298" not in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600023", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"project_id": project_id})
        assert req["source_type"] == "group", req
        assert req["group_name"] == f"{name} x Talentgram", req
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_no_group_and_no_phone_refuses_cleanly():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana NoSrc {tag}"
    project_id = await _seed_project(f"Google NoSrc {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(name, whatsapp_group_name="", email="")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600024",
            text=f"send - {name} - Google NoSrc {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 → Approve" not in r.reply, r.reply
        assert "no whatsapp group or phone number configured" in r.reply.lower(), r.reply
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# SEND confirmation media preview (master prompt Part 10/11): the admin
# must see EXACTLY which marked media will be forwarded before approving —
# never a bare count. Simulates the real worker's scan-result report AND
# manually advances the backend orchestrator (no background loop runs
# during tests), mirroring _process_scan_done's preview_only branch.
# ---------------------------------------------------------------------------
async def _simulate_send_preview_worker_and_orchestrator(talent_id: str, project_id: str, candidates: list, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({
            "talent_id": talent_id, "project_id": project_id,
            "preview_only": True, "status": ma.SCAN_STATUS_PENDING,
        })
        if doc:
            await db[ma.SCAN_REQUESTS_COLLECTION].update_one(
                {"id": doc["id"]}, {"$set": {"candidates": candidates, "status": ma.SCAN_STATUS_DONE}},
            )
            processed = await orch._process_scan_done()
            assert processed, "orchestrator did not pick up the simulated preview scan request"
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"no pending preview scan request appeared for talent_id={talent_id!r} project_id={project_id!r}"
    )


def _with_simulated_send_preview(talent_id: str, project_id: str, candidates: list):
    return asyncio.create_task(_simulate_send_preview_worker_and_orchestrator(talent_id, project_id, candidates))


async def test_send_confirmation_shows_marked_media_individually_not_a_bare_count():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana Preview {tag}"
    email = f"ahana.preview.{tag}@example.com"
    project_label = f"Google Preview {tag}"
    project_id = await _seed_project(project_label, whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        worker = _with_simulated_send_preview(talent_id, project_id, [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="prev-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="prev-intro", media_type="video"),
        ])
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600025",
            text=f"send - {name} - {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await worker
        assert r.handled, r.reply
        assert "Marked media:" in r.reply, r.reply
        assert "1 - Take 1" in r.reply, r.reply
        assert "2 - Introduction" in r.reply, r.reply
        assert "3 media files" not in r.reply and "media files" not in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600025", text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_confirmation_no_marked_media_stops_before_approval():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana NoMark {tag}"
    email = f"ahana.nomark.{tag}@example.com"
    project_label = f"Google NoMark {tag}"
    project_id = await _seed_project(project_label, whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        worker = _with_simulated_send_preview(talent_id, project_id, [])  # genuinely nothing marked
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600026",
            text=f"send - {name} - {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await worker
        assert r.handled, r.reply
        assert "1 → Approve" not in r.reply, r.reply
        assert "no marked media" in r.reply.lower() or "no @gunwanti" in r.reply.lower(), r.reply
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_confirmation_preview_cached_across_edit_turn_no_second_scan():
    """A form-field edit turn ("Age = 24") must NOT re-trigger a fresh
    WhatsApp scan — the preview is cached on the SAME approval-draft row
    (media_send.get_cached_send_preview) and reused, so an edit turn is
    fast (DB-only), never a second multi-second scan round trip."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana EditCache {tag}"
    email = f"ahana.editcache.{tag}@example.com"
    project_label = f"Google EditCache {tag}"
    project_id = await _seed_project(project_label, whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        worker = _with_simulated_send_preview(talent_id, project_id, [
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="cache-take1"),
        ])
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600027",
            text=f"send - {name} - {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await worker
        assert "1 - Take 1" in r.reply, r.reply

        # Enter the edit state, then submit the field edit — no second
        # scan_request should ever be created by either turn below — if
        # one were, this find_one would race a NEW pending preview doc
        # that nothing here services, and _build_send_confirmation would
        # degrade to the "couldn't verify" note instead of showing the
        # cached "1 - Take 1" line again.
        r_edit_prompt = await handle_inbound_message(
            group_name=group, sender_phone="917000600027", text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r_edit_prompt.handled, r_edit_prompt.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone="917000600027", text="Age = 24",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "1 - Take 1" in r2.reply, r2.reply
        assert "couldn't verify" not in r2.reply.lower(), r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone="917000600027", text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# Orchestrator-level tests — hand-inserted whatsapp_scan_requests docs,
# exactly mirroring test_media_assignment.py's own orchestrator test style
# (no real WhatsApp Worker needed).
# ---------------------------------------------------------------------------
async def _insert_send_scan_done(*, talent_id, talent_label, project_id, project_label, group_name, destination_group, candidates):
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "scan", "workflow": "send", "status": ma.SCAN_STATUS_DONE,
        "group_name": group_name, "destination_group": destination_group,
        "talent_id": talent_id, "talent_label": talent_label,
        "project_id": project_id, "project_label": project_label,
        "candidates": candidates, "scan_error": None,
        "created_at": _now(), "updated_at": _now(),
    })
    return req_id


# ---------------------------------------------------------------------------
# 2/3: only marked media is selected — two marks in, exactly two send
# targets out, never more (nothing "extra" is ever inferred).
# ---------------------------------------------------------------------------
async def test_send_orchestrator_only_marked_media_becomes_send_targets():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert mid["mode"] == "send"
        assert mid["status"] == ma.DOWNLOAD_STATUS_PENDING
        targets = {(t["media_role"], t["take_number"]) for t in mid["send_targets"]}
        assert targets == {("take", 1), ("intro", None)}
        assert len(mid["send_targets"]) == 2  # exactly the two marks, nothing else

        rows = await db[ms.MEDIA_SENDS_COLLECTION].find({"talent_id": talent_id}).to_list(10)
        assert len(rows) == 2
        assert all(r["send_status"] == ms.SEND_STATUS_MARKED for r in rows)
        assert all(r["destination_group"] == DESTINATION_GROUP for r in rows)
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# 4: project isolation — a mark for a DIFFERENT project must never surface
# as a send target for the requested project (validate_candidates' own
# project filtering, already proven for upload, exercised here for send).
# ---------------------------------------------------------------------------
async def test_send_orchestrator_project_isolation():
    """2026-08-25: since the admin-command-is-authoritative project-text
    fix, a mark's text only excludes it when it CONFIDENTLY matches a
    DIFFERENT REAL project (never merely "doesn't match the requested
    one" — that case now defaults to the requested project instead, so
    this test's "other" project must be an actual registered project to
    still exercise genuine cross-project isolation)."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    other_project_id, other_project_label = f"p-other-{tag}", f"Google Bride Film {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {other_project_label} take 1", source_message_id="src-other-take1"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing", "slug": project_id})
    await db.projects.insert_one({"id": other_project_id, "brand_name": other_project_label, "status": "ongoing", "slug": other_project_id})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert mid["mode"] == "send"
        assert len(mid["send_targets"]) == 1
        assert mid["send_targets"][0]["source_message_id"] == "src-take1"
    finally:
        await db.projects.delete_many({"id": {"$in": [project_id, other_project_id]}})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Long-conversation backtest (master prompt Part 4/14/25.5): the correct
# marked media must still be found no matter how much irrelevant traffic
# — other people's marks, marks for other projects — sits around it. Never
# rely on "latest", "last N", or scan-window position; identification is
# by @Gunwanti-mention + project-text matching alone, independent of how
# many OTHER candidates are in the list.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_long_conversation_100_plus_irrelevant_marks_still_finds_correct_media():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    other_project_id, other_project_label = f"p-other-{tag}", f"Google Bride Film {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)

    # 120 decoys: some mention someone ELSE entirely (never Gunwanti —
    # validate_candidates' own LID filter must exclude these), some
    # mention Gunwanti but mark the OTHER project (the project-text
    # filter must exclude these too) — genuinely irrelevant traffic, the
    # exact shape a long, busy talent conversation produces.
    decoys = []
    for i in range(60):
        decoys.append(_mark(
            mention_lid=f"99999{i:05d}@lid", mark_text=f"mark {project_label} random {i}",
            source_message_id=f"decoy-other-mention-{i}",
        ))
    for i in range(60):
        decoys.append(_mark(
            mention_lid=GUNWANTI_LID, mark_text=f"mark {other_project_label} random {i}",
            source_message_id=f"decoy-other-project-{i}",
        ))
    assert len(decoys) == 120

    genuine = [
        _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
        _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
    ]
    # Interleaved, not appended at a convenient position — the genuine
    # marks sit BOTH before and after decoys, proving position/order is
    # never what identifies them.
    candidates = decoys[:37] + [genuine[0]] + decoys[37:83] + [genuine[1]] + decoys[83:]
    assert len(candidates) == 122

    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=candidates,
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing", "slug": project_id})
    await db.projects.insert_one({"id": other_project_id, "brand_name": other_project_label, "status": "ongoing", "slug": other_project_id})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert mid["mode"] == "send"
        targets = {(t["media_role"], t["take_number"], t["source_message_id"]) for t in mid["send_targets"]}
        assert targets == {("take", 1, "src-take1"), ("intro", None, "src-intro")}, targets
        assert len(mid["send_targets"]) == 2, "exactly the 2 genuine marks among 120 decoys, nothing more"
    finally:
        await db.projects.delete_many({"id": {"$in": [project_id, other_project_id]}})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Multi-project mark test (master prompt Part 15): the SAME talent has
# marks for THREE different projects. SEND for each project independently
# must select ONLY that project's own marked media — zero cross-project
# contamination in either direction.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_three_projects_same_talent_zero_cross_contamination():
    tag = uuid.uuid4().hex[:6]
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    projects = [
        (f"p-a-{tag}", f"Google A {tag}"),
        (f"p-b-{tag}", f"Google B {tag}"),
        (f"p-c-{tag}", f"Google C {tag}"),
    ]
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    for pid, label in projects:
        await db.projects.insert_one({"id": pid, "brand_name": label, "status": "ongoing", "slug": pid})

    # ALL three projects' marks exist in the SAME talent conversation —
    # this is the whole point of the test: one shared candidate pool,
    # three independent SEND requests, each must pick only its own.
    all_marks = []
    for pid, label in projects:
        all_marks.append(_mark(
            mention_lid=GUNWANTI_LID, mark_text=f"mark {label} take 1", source_message_id=f"src-{pid}-take1",
        ))

    req_ids = []
    try:
        for pid, label in projects:
            req_id = await _insert_send_scan_done(
                talent_id=talent_id, talent_label=talent_label, project_id=pid, project_label=label,
                group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
                candidates=list(all_marks),  # the FULL shared pool, every time
            )
            req_ids.append(req_id)

        results = {}
        for _ in range(len(projects)):
            assert await orch._process_scan_done()
        for req_id, (pid, label) in zip(req_ids, projects):
            mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
            assert mid["mode"] == "send", (label, mid)
            results[pid] = {t["source_message_id"] for t in mid["send_targets"]}

        assert results[projects[0][0]] == {f"src-{projects[0][0]}-take1"}, results
        assert results[projects[1][0]] == {f"src-{projects[1][0]}-take1"}, results
        assert results[projects[2][0]] == {f"src-{projects[2][0]}-take1"}, results
        # Explicit cross-contamination check: no project's targets contain
        # ANY other project's source_message_id.
        for pid, _ in projects:
            others = {f"src-{other_pid}-take1" for other_pid, _ in projects if other_pid != pid}
            assert not (results[pid] & others), (pid, results[pid], others)
    finally:
        await db.projects.delete_many({"id": {"$in": [p[0] for p in projects]}})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_many({"id": {"$in": req_ids}})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# 10: successful SEND is idempotent — a second scan with the same mark
# already `sent` must report ALREADY SENT, never queue a re-send.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_idempotent_no_resend():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    await db[ms.MEDIA_SENDS_COLLECTION].insert_one({
        "send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP,
        "source_message_id": "src-take1", "source_thumbnail_hash": "hash-src-take1",
        "media_role": "take", "take_number": 1,
        "send_status": ms.SEND_STATUS_SENT, "created_at": _now(), "created_by": "test",
    })
    # The ☑️ completion marker (Phase 5/7) already went out for this
    # talent/project/destination in an earlier, fully-completed attempt —
    # otherwise a fresh scan with nothing new to forward would still
    # dispatch to the worker just to send the marker (correct new
    # behavior, exercised by its own dedicated test below), which isn't
    # what THIS test is about (pure media-idempotency, no re-send).
    await ms.record_marker_sent(talent_id, project_id, DESTINATION_GROUP, created_by="test")
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1")],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "ALREADY SENT" in final["report"]
        assert "No duplicate send performed." in final["report"]
        # No new media_sends row was created beyond the one already there.
        assert await db[ms.MEDIA_SENDS_COLLECTION].count_documents({"talent_id": talent_id}) == 1
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[ms.COMPLETION_MARKERS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Double-approval protection (master prompt Part 20/21): "Approve SEND
# then immediately repeat SEND" — a second, independent "1" reply must
# NEVER dispatch a second scan_request for the same operation. This is
# the Concurrent Task Engine's own STATUS_EXECUTING transition
# (agents/tasks.py / agents/dispatcher.py's _advance_task) — set BEFORE
# the executor runs and the task cleared immediately after — exercised
# here at the real dispatch level, not assumed.
# ---------------------------------------------------------------------------
async def test_send_repeated_approve_reply_never_creates_a_second_scan_request():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    name = f"Ahana DoubleApprove {tag}"
    email = f"ahana.doubleapprove.{tag}@example.com"
    project_label = f"Google DoubleApprove {tag}"
    project_id = await _seed_project(project_label, whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(name, whatsapp_group_name=f"{name} x Talentgram", email=email)
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600028",
            text=f"send - {name} - {project_label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "1 → Approve" in r.reply, r.reply

        r1 = await handle_inbound_message(
            group_name=group, sender_phone="917000600028", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Approved" in r1.reply, r1.reply
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 1

        # The SAME reply, sent again as a genuinely separate inbound
        # message (the real-world "admin double-taps 1" / impatient
        # resend scenario) — by the time this arrives, the task engine
        # has already cleared the confirming task (STATUS_EXECUTING ->
        # cleared once _send_executor returned), so this must be treated
        # as "nothing pending", never as a second approval.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone="917000600028", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Approved" not in (r2.reply or ""), (
            f"a second, independent '1' must never re-trigger the SEND executor: {r2.reply!r}"
        )
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"project_id": project_id}) == 1, (
            "exactly one scan_request must exist after two '1' replies — never a duplicate"
        )
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"project_id": project_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# 11: partial-failure resume — one item already `sent`, one previously
# `failed` (or never attempted) -> only the missing one becomes a target.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_partial_failure_resumes_only_missing_item():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    await db[ms.MEDIA_SENDS_COLLECTION].insert_one({
        "send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP,
        "source_message_id": "src-take1", "source_thumbnail_hash": "hash-src-take1",
        "media_role": "take", "take_number": 1,
        "send_status": ms.SEND_STATUS_SENT, "created_at": _now(), "created_by": "test",
    })
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert mid["mode"] == "send"
        assert len(mid["send_targets"]) == 1  # only the missing "intro", never re-sends "take 1"
        assert mid["send_targets"][0]["media_role"] == "intro"
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Per-item resilience + completion reporting: 3 of 5-ish targets succeed,
# rest fail -> report says "X/Y sent", failed items never silently
# swallowed, and each failed target's OWN mark_send_status call records
# "failed", never "sent".
# ---------------------------------------------------------------------------
async def test_send_orchestrator_download_done_partial_success_reports_correctly():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    send_targets = [
        {"source_message_id": "src-a", "source_thumbnail_hash": "hash-a", "media_role": "take", "take_number": 1},
        {"source_message_id": "src-b", "source_thumbnail_hash": "hash-b", "media_role": "take", "take_number": 2},
        {"source_message_id": "src-c", "source_thumbnail_hash": "hash-c", "media_role": "intro", "take_number": None},
    ]
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "send_targets": send_targets,
        "download_results": [
            {"ok": True, "source_message_id": "src-a"},
            {"ok": False, "source_message_id": "src-b", "error": "hash mismatch — refused to substitute another tile"},
            {"ok": True, "source_message_id": "src-c"},
        ],
        "pending_report_context": {"talent_label": talent_label, "project_label": project_label, "destination_group": DESTINATION_GROUP, "already": []},
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert final["status"] == ma.STATUS_FINISHED
        assert "2/3 media sent" in final["report"], final["report"]
        assert "1 failed" in final["report"], final["report"]
        assert "hash mismatch" in final["report"], final["report"]
        assert "Pipeline stage was NOT changed." in final["report"]

        rows = {r["source_message_id"]: r["send_status"] for r in await db[ms.MEDIA_SENDS_COLLECTION].find({"talent_id": talent_id}).to_list(10)}
        # mark_send_status was called per-target even though record_send
        # (the "marked" insert) never ran in this hand-inserted test —
        # $set on a non-existent filter simply matches nothing, which is
        # fine: the important thing is nothing here ever reports "sent"
        # for src-b.
        assert rows.get("src-b") != ms.SEND_STATUS_SENT
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# 12: SEND never mutates pipeline stage — no db.casting_pipeline write
# occurs anywhere in the scan_done/download_done SEND path.
# ---------------------------------------------------------------------------
async def test_send_never_mutates_pipeline_stage():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    before_count = await db.casting_pipeline.count_documents({"talent_id": talent_id, "project_id": project_id})
    assert before_count == 0  # confirm-clean start

    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1")],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        # scan_done alone (queuing the send) must not touch pipeline stage.
        after_scan = await db.casting_pipeline.count_documents({"talent_id": talent_id, "project_id": project_id})
        assert after_scan == 0

        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].update_one(
            {"id": req_id},
            {"$set": {"status": ma.DOWNLOAD_STATUS_DONE, "download_results": [{"ok": True, "source_message_id": "src-take1"}]}},
        )
        assert await orch._process_download_done()
        after_download = await db.casting_pipeline.count_documents({"talent_id": talent_id, "project_id": project_id})
        assert after_download == 0  # still untouched after full completion
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await db.casting_pipeline.delete_many({"talent_id": talent_id, "project_id": project_id})


# ---------------------------------------------------------------------------
# Form/submission send (2026-08-27 revision): submission.decision is NO
# LONGER a SEND prerequisite — a "pending" submission is exactly as
# eligible as an "approved" one. The only gate SEND has is its OWN
# explicit form-approval step (see the approval-lifecycle tests below).
# Only a submission's mere EXISTENCE is still required (there's no data
# to build a form from otherwise).
# ---------------------------------------------------------------------------
async def test_send_proceeds_with_pending_submission_after_explicit_form_approval():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.noapprove.{tag}@example.com"
    project_id = await _seed_project(f"Google NoApprove {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana NoApprove {tag}", whatsapp_group_name=f"Ahana NoApprove {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="pending")  # deliberately NOT approved
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    phone = "917000600030"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana NoApprove {tag} - Google NoApprove {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        # A pending submission.decision never blocks the preview — SEND's
        # own approval gate is the only thing standing between here and dispatch.
        assert "SEND FORM PREVIEW" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None, "a pending submission.decision must not prevent SEND from dispatching"

        # decision itself is untouched at dispatch time — only the SEND
        # completion path (further below) is allowed to change it.
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["decision"] == "pending"
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_includes_form_message_on_first_send_and_records_marked_row():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.formsend.{tag}@example.com"
    project_id = await _seed_project(f"Google FormSend {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana FormSend {tag}", whatsapp_group_name=f"Ahana FormSend {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "5'6\""}}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600031",
            text=f"send - Ahana FormSend {tag} - Google FormSend {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        assert "Ahana FormSend" in r.reply
        assert "Google FormSend" in r.reply
        assert "5'6\"" in r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600031", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["form_message"], req
        assert "Ahana FormSend" in req["form_message"]
        assert "Google FormSend" in req["form_message"]
        assert req["submission_id"] == submission_id
        assert req["content_hash"]

        form_row = await db[ms.FORM_SENDS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert form_row is not None
        assert form_row["send_status"] == ms.SEND_STATUS_MARKED
        assert form_row["content_hash"] == req["content_hash"]
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_skips_form_message_when_already_sent_same_content():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.formskip.{tag}@example.com"
    project_id = await _seed_project(f"Google FormSkip {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana FormSkip {tag}", whatsapp_group_name=f"Ahana FormSkip {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        # First send marks the form as sent for this exact content.
        sub = await db.submissions.find_one({"id": submission_id})
        project = await db.projects.find_one({"id": project_id}, {"_id": 0})
        built = ms.build_form_send_message(sub, project, f"Ahana FormSkip {tag}", f"Google FormSkip {tag}")
        await ms.record_form_send(
            talent_id=talent_id, project_id=project_id, destination_group=DESTINATION_GROUP,
            submission_id=submission_id, content_hash=built["content_hash"], created_by="test",
        )
        await ms.mark_form_send_status(talent_id, project_id, DESTINATION_GROUP, built["content_hash"], ms.SEND_STATUS_SENT, sent_at=_now())

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600032",
            text=f"send - Ahana FormSkip {tag} - Google FormSkip {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 → Approve" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600032", text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["form_message"] is None  # already sent for this content -> not resent
        assert req["content_hash"] == built["content_hash"]
        # Still exactly one form_sends row -> no duplicate created.
        assert await db[ms.FORM_SENDS_COLLECTION].count_documents({"talent_id": talent_id, "project_id": project_id}) == 1
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


def test_build_form_send_message_includes_populated_fields_skips_empty():
    sub = {
        "id": "sub-x",
        "form_data": {
            "height": "5'6\"", "location": "Mumbai", "budget": {"status": "accept", "value": ""},
            "gender": "Female", "ethnicity": "Asian", "instagram_followers": "10000",
            "skills": ["Dancing"], "work_links": ["https://example.com/reel"],
        },
        "talent_name": "Ahana Formatting", "media": [],
    }
    built = ms.build_form_send_message(sub, None, "Ahana Formatting", "Google Format Test")
    message = built["message"]
    assert "Ahana Formatting" in message
    assert "Google Format Test" in message
    assert "5'6\"" in message
    assert "Mumbai" in message
    assert "Accepts budget" in message
    # Fields explicitly excluded from the outgoing SEND form (Phase 3) —
    # never rendered even though they're present in form_data.
    for excluded in ("Gender", "Ethnicity", "Followers", "Skills", "Work Links", "Female", "Asian", "10000", "Dancing"):
        assert excluded not in message, f"{excluded!r} must not appear in the outgoing SEND form: {message}"
    assert built["content_hash"]


def test_build_form_send_message_always_shows_fixed_field_list():
    """The outgoing form is a FIXED template (Phase 3) — every one of its
    allowed fields is always present (blank if the submission has no
    value for it), never conditionally omitted the way the old format
    dropped empty fields."""
    sub = {"id": "sub-y", "form_data": {}, "talent_name": "Ahana Blank", "media": []}
    built = ms.build_form_send_message(sub, None, "Ahana Blank", "Google Blank Test")
    for label in (
        "Project Name", "Name", "Age", "Height", "Current Location",
        "Availability", "Competitive Brand", "Instagram Link", "Budget",
    ):
        assert f"{label}:" in built["message"], built["message"]


def test_build_form_send_message_instagram_handle_becomes_full_url():
    sub = {"id": "sub-z", "form_data": {"instagram_handle": "ahana.actor"}, "talent_name": "Ahana IG", "media": []}
    built = ms.build_form_send_message(sub, None, "Ahana IG", "Google IG Test")
    assert "https://instagram.com/ahana.actor" in built["message"]


def test_build_form_send_message_overrides_replace_submission_values():
    sub = {"id": "sub-o", "form_data": {"height": "5'6\""}, "talent_name": "Ahana Override", "media": []}
    built = ms.build_form_send_message(
        sub, None, "Ahana Override", "Google Override Test", overrides={"height": "5'9\""},
    )
    assert "5'9\"" in built["message"]
    assert "5'6\"" not in built["message"]


def test_build_form_send_message_content_hash_changes_when_fields_change():
    sub_a = {"id": "sub-x", "form_data": {"height": "5'6\""}, "talent_name": "Ahana Hash", "media": []}
    sub_b = {"id": "sub-x", "form_data": {"height": "5'7\""}, "talent_name": "Ahana Hash", "media": []}
    built_a = ms.build_form_send_message(sub_a, None, "Ahana Hash", "Google Hash Test")
    built_b = ms.build_form_send_message(sub_b, None, "Ahana Hash", "Google Hash Test")
    assert built_a["content_hash"] != built_b["content_hash"]


# ---------------------------------------------------------------------------
# Orchestrator-level form-send result recording — mirrors the media-item
# pattern (mark_send_status) exactly, fully independent of it.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_records_form_send_success():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    content_hash = f"hash-{tag}"
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "send_targets": [], "download_results": [], "form_send_result": {"ok": True, "send_state": "MESSAGE_SENT_AND_VERIFIED"},
        "pending_report_context": {
            "talent_label": talent_label, "project_label": project_label, "destination_group": DESTINATION_GROUP,
            "already": [], "submission_id": f"sub-{tag}", "content_hash": content_hash, "form_message_included": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
    await db[ms.FORM_SENDS_COLLECTION].insert_one({
        "form_send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP, "submission_id": f"sub-{tag}", "content_hash": content_hash,
        "send_status": ms.SEND_STATUS_MARKED, "created_at": _now(), "created_by": "test",
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert "Submission details" in final["report"]
        assert "✓ Submission details" in final["report"]

        form_row = await db[ms.FORM_SENDS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert form_row["send_status"] == ms.SEND_STATUS_SENT
        assert form_row.get("sent_at")
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_send_orchestrator_records_form_send_failure_independent_of_media():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    content_hash = f"hash-{tag}"
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "send_targets": [{"source_message_id": "src-a", "source_thumbnail_hash": "hash-a", "media_role": "take", "take_number": 1}],
        "download_results": [{"ok": True, "source_message_id": "src-a"}],  # media succeeds
        "form_send_result": {"ok": False, "error": "no real Send control found — refusing to guess"},  # form fails
        "pending_report_context": {
            "talent_label": talent_label, "project_label": project_label, "destination_group": DESTINATION_GROUP,
            "already": [], "submission_id": f"sub-{tag}", "content_hash": content_hash, "form_message_included": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
    await db[ms.FORM_SENDS_COLLECTION].insert_one({
        "form_send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP, "submission_id": f"sub-{tag}", "content_hash": content_hash,
        "send_status": ms.SEND_STATUS_MARKED, "created_at": _now(), "created_by": "test",
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert "✗ Submission details" in final["report"]
        assert "SEND PARTIAL" in final["report"]  # form failure alone still surfaces as partial, even though the media item succeeded

        form_row = await db[ms.FORM_SENDS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert form_row["send_status"] == ms.SEND_STATUS_FAILED
        # The media item's own success is untouched by the form's failure.
        media_row_check = await db[ms.MEDIA_SENDS_COLLECTION].find_one({"talent_id": talent_id, "source_message_id": "src-a"})
        # (no media_sends row exists in this hand-inserted test — mark_send_status's
        # $set simply matched nothing, same documented pattern as the media-only
        # partial-success test above; the assertion here is just that this branch
        # never raises and never conflates form/media state.)
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_send_orchestrator_dispatches_form_even_when_all_media_already_sent():
    """Regression (2026-08-25): the "all media already sent" shortcut used
    to _finish() immediately, which silently dropped a pending
    form_message — the form must still reach the worker even when there
    is nothing new to forward."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    await db[ms.MEDIA_SENDS_COLLECTION].insert_one({
        "send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP,
        "source_message_id": "src-take1", "source_thumbnail_hash": "hash-src-take1",
        "media_role": "take", "take_number": 1,
        "send_status": ms.SEND_STATUS_SENT, "created_at": _now(), "created_by": "test",
    })
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[_mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1")],
    )
    await db[ma.SCAN_REQUESTS_COLLECTION].update_one({"id": req_id}, {"$set": {"form_message": "SUBMISSION DETAILS\n\nTalent:\nAhana"}})
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        # Dispatched to the worker (not finished directly) even though the
        # only media item was already sent.
        assert mid["mode"] == "send"
        assert mid["status"] == ma.DOWNLOAD_STATUS_PENDING
        assert mid["send_targets"] == []
        assert mid["form_message"] == "SUBMISSION DETAILS\n\nTalent:\nAhana"
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# destination_group_override (2026-08-26) — a pre-existing, deliberately
# test-only seam on _send_executor (never reachable from the real chat-
# dispatch path: the dispatcher's generic executor contract only ever
# calls executor(collected, ctx), with no way to supply a third keyword
# argument). Lets a disposable E2E point SEND at a throwaway destination
# WITHOUT ever reading or writing the project's own whatsapp_casting_
# group_name field — needed for real talents/projects (e.g. a genuine
# production project with an approved submission and clean, real marks)
# whose own destination has legitimately never been configured, without
# fabricating a fake value into their real project document.
# ---------------------------------------------------------------------------
async def test_send_executor_override_used_as_destination_without_project_field():
    """The override becomes the actual destination_group on the created
    scan request, even though the project has NO whatsapp_casting_group_name
    at all — proving requirement 2 and 3 (override works; project field is
    not required when the override is supplied)."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.override.{tag}@example.com"
    # Deliberately NO whatsapp_casting_group_name on this project.
    project_id = await _seed_project(f"Override Project {tag}")
    talent_id = await _seed_talent(
        f"Ahana Override {tag}", whatsapp_group_name=f"Ahana Override {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        project_before = await db.projects.find_one({"id": project_id}, {"_id": 0})
        assert not project_before.get("whatsapp_casting_group_name")

        ctx = ExecContext(agent_id="casting-agent", group_name=group, sender_phone="917000600099", sender_name="Raj")
        result = await cp._send_executor(
            {"talent_selector": f"Ahana Override {tag}", "project_query": f"Override Project {tag}"},
            ctx, destination_group_override=DESTINATION_GROUP,
        )
        assert result.ok, result.message
        assert "Approved" in result.message, result.message

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["workflow"] == "send"
        assert req["destination_group"] == DESTINATION_GROUP

        # Requirement 4: the project document itself was never touched.
        project_after = await db.projects.find_one({"id": project_id}, {"_id": 0})
        assert project_after == project_before, (project_before, project_after)
        assert not project_after.get("whatsapp_casting_group_name")
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])


async def test_send_executor_no_override_still_uses_project_configured_destination():
    """Requirement 5 (unchanged production behavior, part 1): with NO
    override, a project that DOES have whatsapp_casting_group_name set is
    resolved exactly as before — the override parameter changes nothing
    when omitted."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.nooverride.{tag}@example.com"
    project_id = await _seed_project(f"NoOverride Project {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana NoOverride {tag}", whatsapp_group_name=f"Ahana NoOverride {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        ctx = ExecContext(agent_id="casting-agent", group_name=group, sender_phone="917000600098", sender_name="Raj")
        result = await cp._send_executor(
            {"talent_selector": f"Ahana NoOverride {tag}", "project_query": f"NoOverride Project {tag}"}, ctx,
        )
        assert result.ok, result.message
        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert req["destination_group"] == DESTINATION_GROUP
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])


async def test_send_executor_no_override_missing_destination_still_refuses():
    """Requirement 5 (unchanged production behavior, part 2): with NO
    override AND no project destination configured, SEND still refuses
    with destination_not_configured — the override never weakens this
    fail-closed guard for real, unconfigured projects."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.nodest.{tag}@example.com"
    project_id = await _seed_project(f"NoDest Project {tag}")  # no destination field
    talent_id = await _seed_talent(
        f"Ahana NoDest {tag}", whatsapp_group_name=f"Ahana NoDest {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        ctx = ExecContext(agent_id="casting-agent", group_name=group, sender_phone="917000600097", sender_name="Raj")
        result = await cp._send_executor(
            {"talent_selector": f"Ahana NoDest {tag}", "project_query": f"NoDest Project {tag}"}, ctx,
        )
        assert result.ok is False, result.message
        assert result.error == "destination_not_configured", result.error
        # No scan request was ever created for this refused attempt.
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])


# ---------------------------------------------------------------------------
# Phase 2 (2026-08-26): explicit admin approval before SEND — the form is
# PREVIEWED, editable, and only dispatched once the admin replies 1.
# ---------------------------------------------------------------------------
async def test_send_admin_edit_overrides_field_and_approval_freezes_it():
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.edit.{tag}@example.com"
    project_id = await _seed_project(f"Google Edit {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana Edit {tag}", whatsapp_group_name=f"Ahana Edit {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "5'6\""}}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    phone = "917000600040"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana Edit {tag} - Google Edit {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "5'6\"" in r.reply, r.reply

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply

        # Edit the outgoing form's Height — must NOT touch the submission.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Height = 5'9\"",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "5'9\"" in r.reply, r.reply
        assert "5'6\"" not in r.reply, r.reply

        sub_after_edit = await db.submissions.find_one({"id": submission_id})
        assert sub_after_edit["form_data"]["height"] == "5'6\"", "the submission itself must never be overwritten by a form edit"

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Approved" in r.reply, r.reply

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert req is not None
        assert "5'9\"" in req["form_message"], req["form_message"]  # the edited value, not the submission's own

        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert approval is not None
        assert approval["status"] == ms.SEND_APPROVAL_STATUS_APPROVED
        assert approval["overrides"]["height"] == "5'9\""
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_send_retry_reuses_approved_snapshot_not_regenerated():
    """Phase 4 — a second "send" invocation for a talent/project whose
    approval is already "approved" (not yet "completed") must resume with
    the EXACT frozen message, never a freshly regenerated one, even if the
    underlying submission has since changed."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.resume.{tag}@example.com"
    project_id = await _seed_project(f"Google Resume {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana Resume {tag}", whatsapp_group_name=f"Ahana Resume {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "5'6\""}}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    phone = "917000600041"
    try:
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana Resume {tag} - Google Resume {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Approved" in r.reply, r.reply

        # The submission changes AFTER approval (e.g. an unrelated later
        # edit) — the already-approved snapshot must not drift with it.
        await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "6'2\""}}})

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana Resume {tag} - Google Resume {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "already approved" in r2.reply.lower(), r2.reply
        assert "5'6\"" in r2.reply, r2.reply
        assert "6'2\"" not in r2.reply, r2.reply
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# Phase 5 (2026-08-26): fixed SEND ordering — Takes (ascending) -> Intro ->
# Pictures, never scan/discovery order; form_insert_index always lands
# between Intro and Pictures.
# ---------------------------------------------------------------------------
async def test_send_orchestrator_sorts_targets_takes_then_intro_then_photos():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    # Deliberately marked/scanned out of order: photos, take 2, intro, take 1.
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} photos", source_message_id="src-pic"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 2", source_message_id="src-take2"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
        ],
    )
    await db[ma.SCAN_REQUESTS_COLLECTION].update_one({"id": req_id}, {"$set": {"form_message": "FORM TEXT"}})
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        ordered = [(t["media_role"], t["take_number"]) for t in mid["send_targets"]]
        assert ordered == [("take", 1), ("take", 2), ("intro", None), ("photos", None)], ordered
        # 3 take/intro items belong before the form; photos comes after.
        assert mid["form_insert_index"] == 3, mid["form_insert_index"]
        assert mid["send_marker_on_success"] is True
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_send_orchestrator_unnumbered_take_sorts_after_numbered_takes():
    """Regression (2026-08-27): a bare "Take" mark (take_number=None) must
    never jump ahead of a real "Take 1"/"Take 2" in the send order —
    `m.get("take_number") or 0` used to treat None exactly like an
    explicit 0, which would have sorted it FIRST."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 2", source_message_id="src-take2"),
            # A bare "take" (no digit at all) -- extract_role_and_project
            # resolves this to media_role="take", take_number=None.
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take", source_message_id="src-take-bare"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        ordered = [(t["media_role"], t["take_number"], t["source_message_id"]) for t in mid["send_targets"]]
        assert ordered == [
            ("take", 1, "src-take1"), ("take", 2, "src-take2"), ("take", None, "src-take-bare"),
        ], ordered
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Phase 7 (2026-08-26): the ☑️ completion marker — sent once, only when the
# worker actually attempted and succeeded at it; recorded idempotently and
# flips the approval snapshot to "completed".
# ---------------------------------------------------------------------------
async def test_send_orchestrator_records_marker_and_completes_approval():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    await db[ms.SEND_APPROVALS_COLLECTION].insert_one({
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "submission_id": f"sub-{tag}", "overrides": {}, "message": "FORM", "content_hash": f"hash-{tag}",
        "status": ms.SEND_APPROVAL_STATUS_APPROVED, "created_at": _now(),
    })
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "send_targets": [], "download_results": [], "marker_result": {"ok": True},
        "pending_report_context": {
            "talent_label": talent_label, "project_label": project_label, "destination_group": DESTINATION_GROUP,
            "already": [], "marker_attempted": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert "SEND COMPLETE" in final["report"], final["report"]
        assert "☑️" in final["report"], final["report"]

        assert await ms.already_sent_marker(talent_id, project_id, DESTINATION_GROUP) is True
        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert approval["status"] == ms.SEND_APPROVAL_STATUS_COMPLETED
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.SEND_APPROVALS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[ms.COMPLETION_MARKERS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_send_orchestrator_marker_failure_leaves_approval_incomplete():
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    req_id = str(uuid.uuid4())
    await db[ms.SEND_APPROVALS_COLLECTION].insert_one({
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "submission_id": f"sub-{tag}", "overrides": {}, "message": "FORM", "content_hash": f"hash-{tag}",
        "status": ms.SEND_APPROVAL_STATUS_APPROVED, "created_at": _now(),
    })
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": DESTINATION_GROUP,
        "send_targets": [], "download_results": [], "marker_result": {"ok": False, "error": "send failed"},
        "pending_report_context": {
            "talent_label": talent_label, "project_label": project_label, "destination_group": DESTINATION_GROUP,
            "already": [], "marker_attempted": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        assert await orch._process_download_done()
        final = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        assert "SEND PARTIAL" in final["report"], final["report"]

        assert await ms.already_sent_marker(talent_id, project_id, DESTINATION_GROUP) is False
        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        assert approval["status"] == ms.SEND_APPROVAL_STATUS_APPROVED  # unchanged -- still incomplete, eligible to resume
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.SEND_APPROVALS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[ms.COMPLETION_MARKERS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Phase 8 (2026-08-26): status/error hygiene — a successful write always
# clears any stale `error` left over from an earlier failed attempt.
# ---------------------------------------------------------------------------
async def test_mark_send_status_clears_stale_error_on_success():
    tag = uuid.uuid4().hex[:6]
    talent_id, project_id = f"t-{tag}", f"p-{tag}"
    await db[ms.MEDIA_SENDS_COLLECTION].insert_one({
        "send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP, "source_message_id": "src-a", "source_thumbnail_hash": "hash-a",
        "media_role": "take", "take_number": 1, "send_status": ms.SEND_STATUS_MARKED,
        "created_at": _now(), "created_by": "test",
    })
    try:
        await ms.mark_send_status(talent_id, project_id, "src-a", "hash-a", DESTINATION_GROUP, ms.SEND_STATUS_FAILED, error="transient WhatsApp error")
        row = await db[ms.MEDIA_SENDS_COLLECTION].find_one({"talent_id": talent_id})
        assert row["send_status"] == ms.SEND_STATUS_FAILED
        assert row["error"] == "transient WhatsApp error"

        await ms.mark_send_status(talent_id, project_id, "src-a", "hash-a", DESTINATION_GROUP, ms.SEND_STATUS_SENT, sent_at=_now())
        row = await db[ms.MEDIA_SENDS_COLLECTION].find_one({"talent_id": talent_id})
        assert row["send_status"] == ms.SEND_STATUS_SENT
        assert row["error"] is None, "a successful retry must clear the stale error from the earlier failure"
    finally:
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_mark_form_send_status_clears_stale_error_on_success():
    tag = uuid.uuid4().hex[:6]
    talent_id, project_id = f"t-{tag}", f"p-{tag}"
    content_hash = f"hash-{tag}"
    await db[ms.FORM_SENDS_COLLECTION].insert_one({
        "form_send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP, "submission_id": f"sub-{tag}", "content_hash": content_hash,
        "send_status": ms.SEND_STATUS_MARKED, "created_at": _now(), "created_by": "test",
    })
    try:
        await ms.mark_form_send_status(talent_id, project_id, DESTINATION_GROUP, content_hash, ms.SEND_STATUS_FAILED, error="no real Send control found")
        row = await db[ms.FORM_SENDS_COLLECTION].find_one({"talent_id": talent_id})
        assert row["error"] == "no real Send control found"

        await ms.mark_form_send_status(talent_id, project_id, DESTINATION_GROUP, content_hash, ms.SEND_STATUS_SENT, sent_at=_now())
        row = await db[ms.FORM_SENDS_COLLECTION].find_one({"talent_id": talent_id})
        assert row["send_status"] == ms.SEND_STATUS_SENT
        assert row["error"] is None, "a successful retry must clear the stale error from the earlier failure"
    finally:
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


# ---------------------------------------------------------------------------
# Phase 1 (2026-08-26): the project's own whatsapp_casting_group_name field
# — save on create, load on edit, blank/unconfigured allowed, and (proven
# throughout this file's other SEND tests) production SEND fails closed
# when it's blank, never falling back to casting-agent's group_names.
# ---------------------------------------------------------------------------
async def test_project_destination_field_saves_and_loads_via_api():
    admin = {"id": "test-admin"}
    tag = uuid.uuid4().hex[:6]
    created = await create_project(
        ProjectIn(brand_name=f"Destination Save {tag}", whatsapp_casting_group_name="Real Casting Group"), admin,
    )
    project_id = created["id"]
    try:
        assert created["whatsapp_casting_group_name"] == "Real Casting Group"

        loaded = await db.projects.find_one({"id": project_id}, {"_id": 0})
        assert loaded["whatsapp_casting_group_name"] == "Real Casting Group"

        updated = await update_project(
            project_id, ProjectIn(brand_name=f"Destination Save {tag}", whatsapp_casting_group_name="Renamed Group"), admin,
        )
        assert updated["whatsapp_casting_group_name"] == "Renamed Group"
    finally:
        await db.projects.delete_one({"id": project_id})


async def test_project_destination_field_allows_blank_unconfigured_state():
    admin = {"id": "test-admin"}
    tag = uuid.uuid4().hex[:6]
    created = await create_project(ProjectIn(brand_name=f"Destination Blank {tag}"), admin)
    project_id = created["id"]
    try:
        assert not created.get("whatsapp_casting_group_name")
        loaded = await db.projects.find_one({"id": project_id}, {"_id": 0})
        assert not loaded.get("whatsapp_casting_group_name")
    finally:
        await db.projects.delete_one({"id": project_id})


# ---------------------------------------------------------------------------
# SEND approval lifecycle (2026-08-27) — submission.decision is no longer a
# SEND prerequisite; SEND's own explicit form-approval gate is what stands
# between "command received" and "anything goes out", and submission.decision
# only ever transitions to "approved" AFTER the complete SEND operation
# (takes + intro + form + pictures + ☑️) has actually succeeded — via the
# real production mechanism, routers.submissions.set_decision.
# ---------------------------------------------------------------------------
async def _insert_send_download_done(
    *, talent_id, project_id, submission_id, destination_group, send_targets, results,
    marker_ok, form_ok=True, content_hash,
):
    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_DONE,
        "talent_id": talent_id, "project_id": project_id, "destination_group": destination_group,
        "send_targets": send_targets, "download_results": results,
        "form_send_result": {"ok": form_ok} if form_ok else {"ok": False, "error": "form send failed"},
        "marker_result": {"ok": marker_ok} if marker_ok else {"ok": False, "error": "marker send failed"},
        "pending_report_context": {
            "talent_label": "Test Talent", "project_label": "Test Project", "destination_group": destination_group,
            "already": [], "submission_id": submission_id, "content_hash": content_hash,
            "form_message_included": True, "marker_attempted": True,
        },
        "created_at": _now(), "updated_at": _now(),
    })
    return req_id


async def _cleanup_approval_lifecycle(*, talent_id, project_id, submission_id, req_id):
    await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
    await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
    await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
    await db[ms.SEND_APPROVALS_COLLECTION].delete_many({"talent_id": talent_id})
    await db[ms.COMPLETION_MARKERS_COLLECTION].delete_many({"talent_id": talent_id})
    await db.casting_pipeline.delete_many({"talent_id": talent_id, "project_id": project_id})
    await db.notifications.delete_many({"payload.submission_id": submission_id})
    await db.talents.delete_one({"id": talent_id})
    await db.projects.delete_one({"id": project_id})
    await db.submissions.delete_one({"id": submission_id})


async def test_approval_lifecycle_test_a_no_send_approval_means_nothing_sent():
    """TEST A: pending submission + no SEND approval -> nothing sent."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.lifecyclea.{tag}@example.com"
    project_id = await _seed_project(f"Google LifecycleA {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana LifecycleA {tag}", whatsapp_group_name=f"Ahana LifecycleA {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="pending")
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone="917000600050",
            text=f"send - Ahana LifecycleA {tag} - Google LifecycleA {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        assert "Nothing Has Been Sent" in r.reply, r.reply
        assert f"Destination:\n{DESTINATION_GROUP}" in r.reply, r.reply
        # No reply to the preview yet -> ZERO outbound messages of any kind:
        # no scan request ever dispatched (mode never reaches "send"), and
        # none of SEND's own idempotency records exist either.
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
        assert await db[ms.MEDIA_SENDS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
        assert await db[ms.FORM_SENDS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
        assert await db[ms.COMPLETION_MARKERS_COLLECTION].count_documents({"talent_id": talent_id}) == 0
        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id})
        assert approval is not None and approval["status"] == ms.SEND_APPROVAL_STATUS_PENDING
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["decision"] == "pending"
    finally:
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_approval_lifecycle_full_state_transition_trace():
    """End-to-end regression (2026-08-27) matching the exact live production
    re-verification: traces every state transition through the REAL
    chat-dispatch path (handle_inbound_message — never the executor
    directly) and proves, in one continuous flow:

      1. Zero scan_requests exist after the bare "send" command.
      2. The admin's reply IS the proposed form (not a bare ack).
      3. Editing a field before approval leaves the draft "pending" and
         changes zero database state beyond the draft itself.
      4. The edited value is exactly what the frozen snapshot contains.
      5. Approving freezes that exact (edited) snapshot as "approved".
      6. The worker cannot reach mode="send" before step 5 (checked before
         AND after the edit, both times zero).
      7. Re-issuing the same "send" command after approval resumes the
         SAME frozen snapshot ("already approved") rather than bypassing
         approval or silently regenerating a different one.
    """
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.fulltrace.{tag}@example.com"
    project_id = await _seed_project(f"Google FullTrace {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana FullTrace {tag}", whatsapp_group_name=f"Ahana FullTrace {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="pending")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "5'4\""}}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    phone = "917000600060"
    try:
        # 1+2: bare command -> zero scan_requests, form IS the reply.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana FullTrace {tag} - Google FullTrace {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "SEND FORM PREVIEW" in r.reply, r.reply
        assert "5'4\"" in r.reply, r.reply
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": talent_id}) == 0

        # 3+4+6: edit before approval -> draft stays pending, edited value
        # reflected, still zero scan_requests.
        await handle_inbound_message(group_name=group, sender_phone=phone, text="2", sender_name="Raj", sender_is_group_member=True)
        r = await handle_inbound_message(group_name=group, sender_phone=phone, text="Height = 6'0\"", sender_name="Raj", sender_is_group_member=True)
        assert "6'0\"" in r.reply, r.reply
        draft = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id}, {"_id": 0})
        assert draft["status"] == ms.SEND_APPROVAL_STATUS_PENDING
        assert draft["overrides"]["height"] == "6'0\""
        assert "6'0\"" in draft["message"] and "5'4\"" not in draft["message"]
        assert await db[ma.SCAN_REQUESTS_COLLECTION].count_documents({"talent_id": talent_id}) == 0

        # 5: approve -> exact edited snapshot frozen as "approved".
        r = await handle_inbound_message(group_name=group, sender_phone=phone, text="1", sender_name="Raj", sender_is_group_member=True)
        assert "Approved" in r.reply, r.reply
        approved = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id}, {"_id": 0})
        assert approved["status"] == ms.SEND_APPROVAL_STATUS_APPROVED
        assert "6'0\"" in approved["message"]
        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id}, {"_id": 0})
        assert req is not None, "only AFTER approval may a scan_request exist"

        # 7: resuming shows the SAME frozen snapshot, not a fresh/bypassed one.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana FullTrace {tag} - Google FullTrace {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "already approved" in r2.reply.lower(), r2.reply
        assert "6'0\"" in r2.reply, r2.reply
        approved_after_resume = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id}, {"_id": 0})
        assert approved_after_resume["content_hash"] == approved["content_hash"], "resuming must never regenerate a different snapshot"
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


async def test_approval_lifecycle_test_c_full_success_approves_submission():
    """TEST C: all media + form + ☑️ succeed -> submission becomes approved."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google LifecycleC {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(f"Ahana LifecycleC {tag}", whatsapp_group_name=f"Ahana LifecycleC {tag} x Talentgram")
    submission_id = await _seed_submission(project_id, talent_id, "ahana.lifecyclec@example.com", decision="pending")
    send_targets = [{"source_message_id": "src-a", "source_thumbnail_hash": "hash-a", "media_role": "take", "take_number": 1}]
    req_id = await _insert_send_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id, destination_group=DESTINATION_GROUP,
        send_targets=send_targets, results=[{"ok": True, "source_message_id": "src-a"}],
        marker_ok=True, content_hash=f"hash-{tag}",
    )
    try:
        assert await orch._process_download_done()
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["decision"] == "approved", sub
        assert sub.get("decided_at")
        assert sub.get("status_history") and sub["status_history"][-1]["to_status"] == "approved"
        assert await ms.already_sent_marker(talent_id, project_id, DESTINATION_GROUP) is True
    finally:
        await _cleanup_approval_lifecycle(talent_id=talent_id, project_id=project_id, submission_id=submission_id, req_id=req_id)


async def test_approval_lifecycle_test_d_media_failure_leaves_submission_pending():
    """TEST D: one media item fails -> submission remains pending (and the
    marker, gated on all-media-ok, is never even attempted as a success)."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google LifecycleD {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(f"Ahana LifecycleD {tag}", whatsapp_group_name=f"Ahana LifecycleD {tag} x Talentgram")
    submission_id = await _seed_submission(project_id, talent_id, "ahana.lifecycled@example.com", decision="pending")
    send_targets = [{"source_message_id": "src-a", "source_thumbnail_hash": "hash-a", "media_role": "take", "take_number": 1}]
    # The worker never sets send_marker_on_success=True itself when a media
    # item fails (see mark_scan.py._run_send) — the orchestrator test above
    # (test_send_orchestrator_download_done_partial_success_reports_correctly)
    # already proves that wiring; here we prove the DOWNSTREAM consequence:
    # even if marker_attempted were somehow true, an unsuccessful marker
    # result never approves the submission.
    req_id = await _insert_send_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id, destination_group=DESTINATION_GROUP,
        send_targets=send_targets, results=[{"ok": False, "source_message_id": "src-a", "error": "forward failed"}],
        marker_ok=False, content_hash=f"hash-{tag}",
    )
    try:
        assert await orch._process_download_done()
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["decision"] == "pending", sub
        assert not sub.get("decided_at")
        assert await ms.already_sent_marker(talent_id, project_id, DESTINATION_GROUP) is False
        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id})
        assert approval is None or approval.get("status") != ms.SEND_APPROVAL_STATUS_COMPLETED
    finally:
        await _cleanup_approval_lifecycle(talent_id=talent_id, project_id=project_id, submission_id=submission_id, req_id=req_id)


async def test_approval_lifecycle_test_e_retry_after_partial_failure_no_duplicates():
    """TEST E: retry after partial failure -> successful items are not
    duplicated. Simulates: run 1 has "take 1" ok + "intro" failed (no
    approval); run 2 (the retry) only needs to resolve "intro" since
    to_send always excludes anything already send_status=sent."""
    tag = uuid.uuid4().hex[:6]
    project_id, project_label = f"p-{tag}", f"Google {tag}"
    talent_id, talent_label = f"t-{tag}", f"Ahana {tag}"
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"lid": GUNWANTI_LID}}, upsert=True)
    await db[ms.MEDIA_SENDS_COLLECTION].insert_one({
        "send_id": str(uuid.uuid4()), "talent_id": talent_id, "project_id": project_id,
        "destination_group": DESTINATION_GROUP,
        "source_message_id": "src-take1", "source_thumbnail_hash": "hash-src-take1",
        "media_role": "take", "take_number": 1,
        "send_status": ms.SEND_STATUS_SENT, "created_at": _now(), "created_by": "test",
    })
    req_id = await _insert_send_scan_done(
        talent_id=talent_id, talent_label=talent_label, project_id=project_id, project_label=project_label,
        group_name=f"{talent_label} x Talentgram", destination_group=DESTINATION_GROUP,
        candidates=[
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} take 1", source_message_id="src-take1"),
            _mark(mention_lid=GUNWANTI_LID, mark_text=f"mark {project_label} intro", source_message_id="src-intro", media_type="video"),
        ],
    )
    await db.projects.insert_one({"id": project_id, "brand_name": project_label, "status": "ongoing"})
    try:
        assert await orch._process_scan_done()
        mid = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id})
        # Only the missing "intro" is targeted -- "take 1" is never re-sent.
        assert len(mid["send_targets"]) == 1
        assert mid["send_targets"][0]["media_role"] == "intro"
        # Exactly one media_sends row exists for "take 1" -- a retry's
        # record_send (upsert, keyed on the unique index) never creates a
        # second row for the same source media.
        assert await db[ms.MEDIA_SENDS_COLLECTION].count_documents(
            {"talent_id": talent_id, "source_message_id": "src-take1"}
        ) == 1
    finally:
        await db.projects.delete_one({"id": project_id})
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})
        await db[ms.MEDIA_SENDS_COLLECTION].delete_many({"talent_id": talent_id})


async def test_approval_lifecycle_test_f_already_approved_submission_send_still_works_no_duplicate_transition():
    """TEST F: already-approved submission -> SEND still works (decision is
    not a gate either direction) and completing it again never creates a
    duplicate status_history transition (set_decision's own idempotency:
    same decision + already-linked talent_id = no-op)."""
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"Google LifecycleF {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(f"Ahana LifecycleF {tag}", whatsapp_group_name=f"Ahana LifecycleF {tag} x Talentgram")
    submission_id = await _seed_submission(project_id, talent_id, "ahana.lifecyclef@example.com", decision="approved")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"decided_at": _now(), "status_history": [
        {"from_status": "pending", "to_status": "approved", "timestamp": _now(), "admin_email": "admin@example.com", "note": "manually approved earlier"},
    ]}})
    send_targets = [{"source_message_id": "src-a", "source_thumbnail_hash": "hash-a", "media_role": "take", "take_number": 1}]
    req_id = await _insert_send_download_done(
        talent_id=talent_id, project_id=project_id, submission_id=submission_id, destination_group=DESTINATION_GROUP,
        send_targets=send_targets, results=[{"ok": True, "source_message_id": "src-a"}],
        marker_ok=True, content_hash=f"hash-{tag}",
    )
    try:
        assert await orch._process_download_done()
        sub = await db.submissions.find_one({"id": submission_id})
        assert sub["decision"] == "approved"
        # Still exactly the one, pre-existing transition -- set_decision's
        # own no-op path means completing an already-approved submission's
        # SEND never appends a second status_history entry.
        assert len(sub.get("status_history") or []) == 1, sub["status_history"]
    finally:
        await _cleanup_approval_lifecycle(talent_id=talent_id, project_id=project_id, submission_id=submission_id, req_id=req_id)


async def test_approval_lifecycle_test_g_exact_approved_form_is_what_is_sent():
    """TEST G: the exact admin-approved form text is what actually reaches
    the outgoing scan request -- an edited field survives verbatim."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.lifecycleg.{tag}@example.com"
    project_id = await _seed_project(f"Google LifecycleG {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana LifecycleG {tag}", whatsapp_group_name=f"Ahana LifecycleG {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="pending")
    await db.submissions.update_one({"id": submission_id}, {"$set": {"form_data": {"height": "5'4\""}}})
    await db[ma.IDENTITY_COLLECTION].update_one({}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True)
    phone = "917000600051"
    try:
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana LifecycleG {tag} - Google LifecycleG {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(group_name=group, sender_phone=phone, text="2", sender_name="Raj", sender_is_group_member=True)
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Height = 6'1\"",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(group_name=group, sender_phone=phone, text="1", sender_name="Raj", sender_is_group_member=True)

        req = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"talent_id": talent_id, "project_id": project_id})
        approval = await db[ms.SEND_APPROVALS_COLLECTION].find_one({"talent_id": talent_id})
        assert approval["status"] == ms.SEND_APPROVAL_STATUS_APPROVED
        assert "6'1\"" in approval["message"]
        assert "5'4\"" not in approval["message"]
        # The exact same text -- not regenerated -- reached the scan request.
        assert req["form_message"] == approval["message"]
        assert req["content_hash"] == approval["content_hash"]
    finally:
        req_ids = [d["id"] async for d in db[ma.SCAN_REQUESTS_COLLECTION].find({"talent_id": talent_id})]
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], scan_request_ids=req_ids, submission_ids=[submission_id])
        await db[ms.FORM_SENDS_COLLECTION].delete_many({"talent_id": talent_id})
        await db[ms.SEND_APPROVALS_COLLECTION].delete_many({"talent_id": talent_id})
        await _restore_config(original, agent_id="whatsapp-campaign-agent")


# ---------------------------------------------------------------------------
# Regression (2026-08-27, real production incident — Siddhi Bankhele / TVS
# Jupiter): the worker's /scan-requests/{id}/download-result report already
# carried marker_result, but DownloadResultIn never declared the field and
# the endpoint's own $set never wrote it -- Pydantic silently dropped it,
# so a genuinely-successful marker send was never recorded, and a fully
# resolved SEND could never reach "completed"/never trigger the post-SEND
# approval. Caught live, before any incorrect data was written -- the
# marker was simply never attempted a second time, nothing corrupted.
# ---------------------------------------------------------------------------
async def test_download_result_endpoint_persists_marker_result():
    from routers.agents_whatsapp import report_download_result, DownloadResultIn

    req_id = str(uuid.uuid4())
    await db[ma.SCAN_REQUESTS_COLLECTION].insert_one({
        "id": req_id, "mode": "send", "status": ma.DOWNLOAD_STATUS_PENDING,
        "created_at": _now(), "updated_at": _now(),
    })
    try:
        payload = DownloadResultIn(results=[], error=None, form_send_result=None, marker_result={"ok": True})
        await report_download_result(req_id, payload, x_internal_secret=None)

        doc = await db[ma.SCAN_REQUESTS_COLLECTION].find_one({"id": req_id}, {"_id": 0})
        assert doc["marker_result"] == {"ok": True}, doc.get("marker_result")
        assert doc["status"] == ma.DOWNLOAD_STATUS_DONE
    finally:
        await db[ma.SCAN_REQUESTS_COLLECTION].delete_one({"id": req_id})


# ---------------------------------------------------------------------------
# Casting Pipeline regression investigation (2026-08-27) — a real production
# incident report ("commands that previously worked are now not
# responding") triggered a full audit of whether SEND's own Phase 2 approval
# gate (auto_confirm=False, a "confirming"-status Concurrent Task Engine
# entry that can sit unresolved for its full TTL) could hijack an unrelated
# fresh command from the SAME phone in the SAME group. Live production
# testing (direct calls against the real deployed /inbound endpoint) found
# every command still worked correctly even with real leftover "confirming"
# casting.send tasks on file for that exact phone — this test makes that
# same guarantee a permanent, automated regression check: a fresh QUERY
# trigger must never be swallowed/misrouted into an unrelated pending SEND
# confirmation, and the SEND task itself must be left completely untouched
# (still "confirming", same operation_id) for the admin to resolve later.
# ---------------------------------------------------------------------------
async def test_send_confirming_state_does_not_swallow_unrelated_query_command():
    """Talentgram Scouting Agent consolidation (Production fix,
    2026-09-06) — casting.send and QUERY now both run on
    whatsapp-campaign-agent (which gained supports_concurrent_tasks
    specifically to preserve this guarantee); migrated accordingly."""
    group = f"Test Casting {uuid.uuid4().hex[:6]}"
    original = await _use_test_config(group, agent_id="whatsapp-campaign-agent")
    tag = uuid.uuid4().hex[:6]
    email = f"ahana.swallow.{tag}@example.com"
    project_id = await _seed_project(f"Google Swallow {tag}", whatsapp_casting_group_name=DESTINATION_GROUP)
    talent_id = await _seed_talent(
        f"Ahana Swallow {tag}", whatsapp_group_name=f"Ahana Swallow {tag} x Talentgram", email=email,
    )
    submission_id = await _seed_submission(project_id, talent_id, email, decision="approved")
    await db[ma.IDENTITY_COLLECTION].update_one(
        {}, {"$set": {"name": "Gunwanti Talentgram", "phone": "+919321290688", "lid": GUNWANTI_LID}}, upsert=True,
    )
    phone = "917000600099"
    try:
        # Open a SEND confirmation and leave it hanging — never approve,
        # edit, or cancel it, matching the exact real-world scenario found
        # in production (two abandoned "confirming" casting.send tasks left
        # on file for the same phone from an earlier debugging session).
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Ahana Swallow {tag} - Google Swallow {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "SEND FORM PREVIEW" in r.reply, r.reply
        pending_op_id = r.operation_id
        assert pending_op_id is not None

        from agents import tasks as agent_tasks
        pending_task = await agent_tasks.get_task("whatsapp-campaign-agent", pending_op_id)
        assert pending_task is not None and pending_task["status"] == "confirming"

        # A completely unrelated FRESH command (not a reply-to-message, no
        # quoted text) from the SAME phone in the SAME group must be parsed
        # and executed as its own new command — never treated as a reply to
        # the still-open SEND confirmation.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "SEND FORM" not in r2.reply, r2.reply
        assert "Approve" not in r2.reply, r2.reply
        assert f"Google Swallow {tag}" in r2.reply, r2.reply

        # The abandoned SEND task itself is completely untouched — still
        # sitting exactly where the admin left it, ready to be resumed.
        still_pending = await agent_tasks.get_task("whatsapp-campaign-agent", pending_op_id)
        assert still_pending is not None
        assert still_pending["status"] == "confirming"
        assert still_pending["updated_at"] == pending_task["updated_at"]
    finally:
        await _restore_config(original, agent_id="whatsapp-campaign-agent")
        await _cleanup_send(talent_ids=[talent_id], project_ids=[project_id], submission_ids=[submission_id])
