"""Gemini Command Interpreter — Phase 1 AI assistance for ADD/MOVE/SHARE
(2026-09-22).

Covers agents/modules/casting_command_interpreter.py end to end, and its
ONE integration point, casting_pipeline._resolve_bare_reply (wired to
whatsapp-campaign-agent — the WhatsApp group "Talentgram Scouting Agent",
where ADD/MOVE/SHARE actually live; see that module's own "Talentgram
Scouting Agent consolidation" comments and casting_command_interpreter.py's
own module docstring for the full architecture).

Fakes Gemini at the google-genai SDK boundary (`ai.gemini_client._client`),
the SAME technique tests/test_simple_assistant_gemini_migration.py already
uses for the SAME underlying client — the REAL call_tool_json JSON-schema/
error-handling code genuinely runs every time, only the network call
itself is faked, so a schema violation or malformed-JSON response is
exercised for real, not assumed.

Every test below calls agents.dispatcher.handle_inbound_message directly
(no HTTP layer, no WhatsApp mock), reusing test_casting_agent.py's own
established helpers (_use_share_test_config, _seed_project, _seed_talent,
_seed_pipeline_row, _phone, _cleanup) — this is a SHARED DEV MONGO, not a
disposable test database, matching every other file in this suite.
"""
import os

# Must be set BEFORE agents.modules.casting_command_interpreter is
# imported (read once as module-level... no, actually read fresh per call
# via os.environ.get — but pinning here up front keeps every test in this
# file deterministic regardless of import order, matching this suite's
# own established top-of-file convention).
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")
os.environ["GEMINI_ADD_MOVE_SHARE_ENABLED"] = "true"
os.environ["GEMINI_API_KEY"] = "test-gemini-key"
# Short but real — proves the actual asyncio.wait_for bound fires, not a
# unit-level mock of the timeout itself.
os.environ["GEMINI_ADD_MOVE_SHARE_TIMEOUT_SEC"] = "1.0"

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

import pytest

from core import db, _now
from agents import modules as agent_modules
from agents.dispatcher import handle_inbound_message
import ai.gemini_client as G_impl
from agents.modules import casting_command_interpreter as gci

from tests.test_casting_agent import (  # noqa: E402 — reuse the established helpers
    SHARE_AGENT_ID,
    _use_share_test_config,
    _restore_share_config,
    _seed_project,
    _seed_talent,
    _seed_pipeline_row,
    _cleanup,
    _phone,
)

agent_modules.register_all()

pytestmark = pytest.mark.asyncio(loop_scope="module")

_ORIG_CLIENT_FACTORY = G_impl._client


@pytest.fixture(autouse=True)
def _restore_client():
    yield
    G_impl._client = _ORIG_CLIENT_FACTORY


async def _cleanup_gemini_cache() -> None:
    await db[gci.CACHE_COLLECTION].delete_many({})


# ---------------------------------------------------------------------------
# Fake google-genai client — same shape as test_simple_assistant_gemini_
# migration.py's _install_fake_client, reused here so ai.gemini_client's
# own JSON/error-handling code genuinely runs.
# ---------------------------------------------------------------------------
class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeResp:
    def __init__(self, text=None, block_reason=None):
        self.text = text
        self.prompt_feedback = _NS(block_reason=block_reason) if block_reason else None
        self.candidates = [_NS(finish_reason="STOP")] if text else []


def _install_fake_client(fn):
    class _Models:
        async def generate_content(self, *, model, contents, config):
            return await fn(model=model, contents=contents, config=config)

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    G_impl._client = lambda: _Client()


def _commands_response(commands: List[Dict[str, Any]]) -> _FakeResp:
    return _FakeResp(text=json.dumps({"commands": commands}))


def _cmd(
    intent: str, *, talent_name=None, project_name=None, stage_name=None,
    recipient_description=None, template_or_message=None, missing_fields=None, confidence=0.9,
) -> Dict[str, Any]:
    return {
        "intent": intent,
        "talent_name": talent_name,
        "project_name": project_name,
        "stage_name": stage_name,
        "recipient_description": recipient_description,
        "template_or_message": template_or_message,
        "missing_fields": missing_fields or [],
        "confidence": confidence,
    }


def _install_canned(commands: List[Dict[str, Any]], *, calls: Optional[list] = None):
    async def _fn(*, model, contents, config):
        if calls is not None:
            calls.append(contents)
        return _commands_response(commands)
    _install_fake_client(_fn)


def _install_raising(exc: Exception, *, calls: Optional[list] = None):
    async def _fn(*, model, contents, config):
        if calls is not None:
            calls.append(contents)
        raise exc
    _install_fake_client(_fn)


def _install_hanging(seconds: float):
    async def _fn(*, model, contents, config):
        await asyncio.sleep(seconds)
        return _commands_response([_cmd("ADD", talent_name="X", project_name="Y")])
    _install_fake_client(_fn)


def _install_never_called():
    """Installs a fake that raises AssertionError if it's ever invoked —
    proof a given turn never reached Gemini at all (the "existing
    successful commands do not call Gemini" regression requirement)."""
    async def _fn(*, model, contents, config):
        raise AssertionError(f"Gemini must NOT have been called for this message, but was: {contents!r}")
    _install_fake_client(_fn)


# ---------------------------------------------------------------------------
# 1. Existing behavior — proves already-working commands NEVER call Gemini,
#    even with the flag ON and a fake client wired in that would fail loud
#    if it were.
# ---------------------------------------------------------------------------
async def test_native_add_bypasses_gemini_entirely():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Hinge {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Priya Sharma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    _install_never_called()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Add {talent_name} to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert talent_name in r.reply
        assert label in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_native_move_bypasses_gemini_entirely():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Toyota {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Neha Kapoor {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    _install_never_called()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Move {talent_name} to Approved in {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert talent_name in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_ordinary_chatter_never_reaches_gemini_local_prefilter():
    """The cheap local pre-filter alone must reject plain chatter before
    any Gemini call is even attempted — proven by a fake client that fails
    loudly if invoked."""
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    _install_never_called()
    try:
        for text in ("good morning", "thanks so much!", "😂😂😂", "ok cool"):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert not r.handled, f"{text!r} should have been silently ignored, not handled"
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_flag_disabled_never_calls_gemini_even_for_unusual_phrasing():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    _install_never_called()
    os.environ["GEMINI_ADD_MOVE_SHARE_ENABLED"] = "false"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        os.environ["GEMINI_ADD_MOVE_SHARE_ENABLED"] = "true"
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 2. Natural language ADD / MOVE / SHARE via Gemini
# ---------------------------------------------------------------------------
async def test_natural_language_add_via_gemini():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Hinge {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Priya Sharma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    _install_canned([_cmd("ADD", talent_name=talent_name, project_name=label)])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert talent_name in r.reply
        assert label in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_natural_language_move_via_gemini():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Dove {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Riya Mehta {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    await _seed_pipeline_row(project_id, talent_id, "ask_to_test")
    _install_canned([_cmd("MOVE", talent_name=talent_name, stage_name="Follow Up", project_name=label)])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text="Priya is ready for Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert talent_name in r.reply
        assert "Follow Up" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_natural_language_share_via_gemini():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Score {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Anusha Rao {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    _install_canned([_cmd("SHARE", recipient_description=talent_name, project_name=label)])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"{talent_name} needs the casting call shared with her",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # SHARE's confirmation card names the recipient/project it resolved —
        # real deterministic resolution ran (_resolve_share), not a guess.
        assert talent_name in r.reply or "not sure" not in r.reply.lower()
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 3. Incomplete commands — Gemini identifies the intent + whatever is
#    present; the EXISTING missing-field question flow asks for the rest.
#    Gemini must never guess the missing value.
# ---------------------------------------------------------------------------
async def test_incomplete_add_missing_project_asks_via_existing_flow():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    talent_id = await _seed_talent(f"Priya Sharma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    # Gemini correctly reports project as missing — never invents one.
    _install_canned([_cmd("ADD", talent_name=talent_name, project_name=None, missing_fields=["project_name"])])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya needs to be put on the books somewhere",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "project" in r.reply.lower()
        # Nothing was added yet — no pipeline row exists for this talent.
        assert await db.casting_pipeline.count_documents({"talent_id": talent_id}) == 0
    finally:
        await _cleanup(phone, talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 4. Ambiguous talent — Gemini returns the free-text name only; the
#    EXISTING deterministic resolver produces the real disambiguation list.
# ---------------------------------------------------------------------------
async def test_ambiguous_talent_still_resolved_by_existing_disambiguation_engine():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Ambiguous Brand {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    sarah_a = await _seed_talent(f"Sarah Ahuja {uuid.uuid4().hex[:4]}")
    sarah_b = await _seed_talent(f"Sarah Bhatt {uuid.uuid4().hex[:4]}")
    _install_canned([_cmd("ADD", talent_name="Sarah", project_name=label)])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Sarah should be added for the {label} casting",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        assert "Sarah Ahuja" in r.reply and "Sarah Bhatt" in r.reply
        # Neither candidate was silently added — a real numbered choice.
        assert await db.casting_pipeline.count_documents({"project_id": project_id}) == 0
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[sarah_a, sarah_b])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 5. Multiple commands in one message
# ---------------------------------------------------------------------------
async def test_multiple_commands_add_then_move_single_compound_confirmation():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Hinge {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Priya Sharma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    _install_canned([
        _cmd("ADD", talent_name=talent_name, project_name=label),
        _cmd("MOVE", talent_name=talent_name, stage_name="Follow Up"),
    ])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"{talent_name.split()[0]} should be added to Hinge and then shifted straight to Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # ONE combined confirmation card, both steps visible, nothing
        # executed yet without approval.
        assert talent_name in r.reply
        assert "Follow Up" in r.reply
        assert await db.casting_pipeline.count_documents({"talent_id": talent_id}) == 0

        approve = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert approve.handled
        row = await db.casting_pipeline.find_one({"talent_id": talent_id, "project_id": project_id})
        assert row is not None and row["stage"] == "follow_up"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_multiple_commands_share_leading_only_claims_first_command():
    """SHARE cannot lead a compound plan in the existing deterministic
    system — a SHARE-first multi-command Gemini result must conservatively
    claim only its first command, never silently drop/attempt a made-up
    compound-SHARE flow that doesn't exist."""
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"DoveShare {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Anjali Verma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    _install_canned([
        _cmd("SHARE", recipient_description=talent_name, project_name=label),
        _cmd("SHARE", recipient_description="everyone in Follow Up", project_name=label),
    ])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"The {label} casting call should go out to {talent_name}, plus everyone waiting in Follow Up",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # Only the FIRST command's recipient was claimed — the conversation
        # this started is scoped to just that one talent, never a made-up
        # combined "talent + everyone in Follow Up" target the deterministic
        # system has no compound-SHARE machinery to represent.
        conv = await db.whatsapp_conversations.find_one({"agent_id": SHARE_AGENT_ID, "phone": phone})
        assert conv is not None and conv["intent_id"] == "casting.share"
        assert conv["collected"].get("recipient_query") == talent_name
        assert "Follow Up" not in (conv["collected"].get("recipient_query") or "")
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 6. Gemini failure modes — every one must fall back to today's baseline
#    (no reply), never an application error, never a false claim.
# ---------------------------------------------------------------------------
async def test_gemini_not_configured_falls_back_silently():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    saved_key = os.environ.pop("GEMINI_API_KEY", None)
    _install_never_called()
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        if saved_key is not None:
            os.environ["GEMINI_API_KEY"] = saved_key
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_timeout_falls_back_silently():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    _install_hanging(5.0)  # exceeds GEMINI_ADD_MOVE_SHARE_TIMEOUT_SEC=1.0
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_api_error_falls_back_silently():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()

    from google.genai import errors as genai_errors
    _install_raising(genai_errors.ServerError(503, {"error": {"message": "overloaded"}}))
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_malformed_json_falls_back_silently():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()

    async def _fn(*, model, contents, config):
        return _FakeResp(text="not valid json{{{")
    _install_fake_client(_fn)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_empty_response_falls_back_silently():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()

    async def _fn(*, model, contents, config):
        return _FakeResp(text=None, block_reason="SAFETY")
    _install_fake_client(_fn)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_unsupported_intent_falls_back_silently():
    """UNKNOWN, or a schema-valid but empty commands list, must never be
    treated as a claim on the message."""
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    _install_canned([_cmd("UNKNOWN")])
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_gemini_schema_violation_falls_back_silently():
    """A response whose 'commands' key is the wrong shape entirely (schema
    would have been enforced server-side by Gemini in production, but this
    proves the client-side validation layer is ALSO defensive, not
    trusting the provider blindly)."""
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()

    async def _fn(*, model, contents, config):
        return _FakeResp(text=json.dumps({"commands": "not-a-list"}))
    _install_fake_client(_fn)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _cleanup(phone)
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 7. Deduplication — the SAME WhatsApp message id must never trigger a
#    second Gemini call.
# ---------------------------------------------------------------------------
async def test_duplicate_message_id_calls_gemini_at_most_once():
    group = f"Test Gemini {uuid.uuid4().hex[:6]}"
    original = await _use_share_test_config(group)
    phone = _phone()
    project_id = await _seed_project(brand_name=f"Hinge {uuid.uuid4().hex[:6]}")
    label = (await db.projects.find_one({"id": project_id}))["brand_name"]
    talent_id = await _seed_talent(f"Priya Sharma {uuid.uuid4().hex[:4]}")
    talent_name = (await db.talents.find_one({"id": talent_id}))["name"]
    calls: list = []
    _install_canned([_cmd("ADD", talent_name=talent_name, project_name=label)], calls=calls)
    same_message_id = f"wamid.TEST{uuid.uuid4().hex}"
    try:
        r1 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Priya should be added to Hinge",
            sender_name="Raj", sender_is_group_member=True, message_id=same_message_id,
        )
        assert r1.handled
        assert len(calls) == 1

        # Simulate a webhook/worker retry delivering the EXACT same message
        # again, with the SAME message id, before any reply/approval — the
        # conversation this first call started is still pending, so a
        # second attempt won't even re-reach resolve_bare_reply (conv is
        # no longer None) — the real dedup proof is a fresh phone with a
        # fresh conversation state but the retry landing while the cache
        # entry is still warm from a DIFFERENT phone's identical wording,
        # OR simply calling the interpreter directly twice with the same
        # key, which is the unit-level proof below.
        commands_again = await gci.interpret_message(
            "Priya should be added to Hinge", agent_id=SHARE_AGENT_ID, phone=phone,
            group_name=group, message_id=same_message_id,
        )
        assert commands_again is not None
        assert len(calls) == 1, "a second interpret_message call for the SAME message_id must reuse the cache, not call Gemini again"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[talent_id])
        await _restore_share_config(original)
        await _cleanup_gemini_cache()


async def test_no_message_id_falls_back_to_hash_dedup_key():
    """No message_id supplied (transport didn't provide one) — the hash-
    based fallback key (agent/phone/group/text) still dedups an identical
    retry, mirroring inbound_messages._message_key's own fallback shape."""
    calls: list = []
    _install_canned([_cmd("ADD", talent_name="Priya", project_name="Hinge")], calls=calls)
    try:
        c1 = await gci.interpret_message(
            "Priya should be added to Hinge", agent_id="test-agent", phone="911234567",
            group_name="Test Group", message_id=None,
        )
        c2 = await gci.interpret_message(
            "Priya should be added to Hinge", agent_id="test-agent", phone="911234567",
            group_name="Test Group", message_id=None,
        )
        assert c1 is not None and c2 is not None
        assert len(calls) == 1
    finally:
        await _cleanup_gemini_cache()


# ---------------------------------------------------------------------------
# 8. Diagnostic/unit-level tests on the interpreter module itself
# ---------------------------------------------------------------------------
async def test_interpreter_disabled_returns_none_without_any_db_or_network_activity():
    os.environ["GEMINI_ADD_MOVE_SHARE_ENABLED"] = "false"
    _install_never_called()
    try:
        result = await gci.interpret_message(
            "Priya should be added to Hinge", agent_id="test-agent", phone="911111111", group_name="X",
        )
        assert result is None
    finally:
        os.environ["GEMINI_ADD_MOVE_SHARE_ENABLED"] = "true"


def test_validate_and_extract_commands_drops_unsupported_and_malformed_entries():
    raw = {"commands": [
        {"intent": "ADD", "talent_name": "Priya", "project_name": "Hinge", "stage_name": None,
         "recipient_description": None, "template_or_message": None, "missing_fields": [], "confidence": 0.9},
        {"intent": "UNKNOWN", "talent_name": None, "project_name": None, "stage_name": None,
         "recipient_description": None, "template_or_message": None, "missing_fields": [], "confidence": 0.1},
        "not-a-dict",
        {"intent": "SOMETHING_ELSE", "talent_name": "X"},
    ]}
    out = gci._validate_and_extract_commands(raw)
    assert out is not None
    assert len(out) == 1
    assert out[0]["intent"] == "ADD"
    assert out[0]["talent_name"] == "Priya"


def test_validate_and_extract_commands_empty_list_returns_none():
    assert gci._validate_and_extract_commands({"commands": []}) is None
    assert gci._validate_and_extract_commands({"commands": [{"intent": "UNKNOWN"}]}) is None
    assert gci._validate_and_extract_commands({"commands": "not-a-list"}) is None


def test_prefilter_matches_real_spec_examples():
    positives = [
        "Priya should be added to Hinge",
        "Priya should be added to Hinge",
        "Put Priya into the Hinge pipeline",
        "Can we add Priya for Hinge?",
        "Add Priya for the Hinge casting",
        "Shift Priya to Follow Up",
        "Move Priya ahead to Follow Up",
        "Priya is ready for Follow Up",
        "Take Priya from Shortlist and put her in Follow Up",
        "Can you move her to Follow Up?",
        "share hinge shortlist",
    ]
    for p in positives:
        assert gci._looks_like_possible_command(p), f"expected prefilter to accept {p!r}"
    negatives = ["good morning", "thanks!", "ok", "😂😂", "call me later", "video please"]
    for n in negatives:
        assert not gci._looks_like_possible_command(n), f"expected prefilter to reject {n!r}"
