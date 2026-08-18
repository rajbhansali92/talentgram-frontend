"""Static Help Command — tests for the exact-match "help"/"menu"/"commands"/
"please help"/"show commands"/"what can you do" trigger shared by all three
WhatsApp agents (crm-agent, casting-agent, whatsapp-campaign-agent).

Covers: every trigger phrase (case-insensitively) on every agent, each
agent showing only its own static text, ordinary conversation staying
silent (no accidental trigger on a sentence that merely contains "help"),
and — the one behavior with real regression risk — that the check never
fires while a conversation/disambiguation is already in flight.
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
from agents.modules.crm import HELP_TEXT as CRM_HELP_TEXT
from agents.modules.casting_pipeline import HELP_TEXT as CASTING_HELP_TEXT
from agents.modules.whatsapp_campaign_agent import HELP_TEXT as CAMPAIGN_HELP_TEXT

agent_modules.register_all()

CRM_AGENT_ID = "crm-agent"
CASTING_AGENT_ID = "casting-agent"
CAMPAIGN_AGENT_ID = "whatsapp-campaign-agent"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _phone() -> str:
    return "91" + str(uuid.uuid4().int)[:9]


async def _use_test_config(agent_id: str, group_name: str, phone: str, *, group_members: bool) -> dict:
    """Point one agent at a throwaway group for this test, saving whatever
    config already existed so it can be restored. `group_members=True`
    mirrors casting-agent's real "group_members" security mode (any group
    participant may issue commands); False mirrors crm-agent's and
    whatsapp-campaign-agent's real "allowlist" mode (only `phone` may)."""
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": agent_id})
    doc = {
        "agent_id": agent_id,
        "group_names": [group_name],
        "allowed_senders": [] if group_members else [phone],
        "security_mode": "group_members" if group_members else "allowlist",
        "active": True,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": agent_id}, doc, upsert=True)
    return original


async def _restore_config(agent_id: str, original) -> None:
    if original is None:
        await db[registry.CONFIG_COLLECTION].delete_one({"agent_id": agent_id})
    else:
        original.pop("_id", None)
        await db[registry.CONFIG_COLLECTION].replace_one({"agent_id": agent_id}, original, upsert=True)


async def _cleanup_conversation_state(agent_id: str, phone: str) -> None:
    await db.whatsapp_conversations.delete_many({"agent_id": agent_id, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": agent_id, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": agent_id, "sender_phone": phone})
    await db.whatsapp_agent_tasks.delete_many({"agent_id": agent_id, "phone": phone})


HELP_PHRASES = [
    "help", "Help", "HELP",
    "commands", "Commands",
    "menu", "Menu",
    "please help", "Please Help",
    "show commands", "Show Commands",
    "what can you do", "What can you do",
]


# ---------------------------------------------------------------------------
# Every trigger phrase, on every agent, returns that agent's own static text
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", HELP_PHRASES)
async def test_crm_help_trigger_phrases(phrase):
    group = f"Test CRM Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CRM_AGENT_ID, group, phone, group_members=False)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=phrase,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == CRM_HELP_TEXT
    finally:
        await _cleanup_conversation_state(CRM_AGENT_ID, phone)
        await _restore_config(CRM_AGENT_ID, original)


@pytest.mark.parametrize("phrase", HELP_PHRASES)
async def test_casting_help_trigger_phrases(phrase):
    group = f"Test Casting Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CASTING_AGENT_ID, group, phone, group_members=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=phrase,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == CASTING_HELP_TEXT
    finally:
        await _cleanup_conversation_state(CASTING_AGENT_ID, phone)
        await _restore_config(CASTING_AGENT_ID, original)


@pytest.mark.parametrize("phrase", HELP_PHRASES)
async def test_campaign_help_trigger_phrases(phrase):
    group = f"Test Campaign Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CAMPAIGN_AGENT_ID, group, phone, group_members=False)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=phrase,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == CAMPAIGN_HELP_TEXT
    finally:
        await _cleanup_conversation_state(CAMPAIGN_AGENT_ID, phone)
        await _restore_config(CAMPAIGN_AGENT_ID, original)


# ---------------------------------------------------------------------------
# Each agent shows ONLY its own capabilities — no cross-contamination and
# no accidental sharing of a single generic help string.
# ---------------------------------------------------------------------------
async def test_each_agent_has_distinct_help_text():
    assert CRM_HELP_TEXT != CASTING_HELP_TEXT
    assert CRM_HELP_TEXT != CAMPAIGN_HELP_TEXT
    assert CASTING_HELP_TEXT != CAMPAIGN_HELP_TEXT

    assert "Talentgram CRM" in CRM_HELP_TEXT
    assert "Save a Contact" in CRM_HELP_TEXT
    assert "Talent Search" not in CRM_HELP_TEXT
    assert "Send a Campaign" not in CRM_HELP_TEXT

    # Simplified Command Language (2026-08-17) — the help text now leads
    # with the concise "Action - Talent - Project - Pipeline" grammar
    # rather than the old per-feature walkthrough; these checks confirm
    # it's still distinctly casting-flavored and still mentions the
    # older natural-language features (talent search, selection, undo)
    # remain available, without requiring the old verbose phrasing.
    assert "Talentgram Casting Commands" in CASTING_HELP_TEXT
    assert "Action - Talent - Project - Pipeline" in CASTING_HELP_TEXT
    assert "pending test" in CASTING_HELP_TEXT
    assert "testing?" in CASTING_HELP_TEXT
    assert "talent search" in CASTING_HELP_TEXT.lower()
    assert "Move" in CASTING_HELP_TEXT
    assert "Save a Contact" not in CASTING_HELP_TEXT
    assert "Send a Campaign" not in CASTING_HELP_TEXT

    assert "Talentgram WhatsApp Agent" in CAMPAIGN_HELP_TEXT
    assert "Send a Campaign" in CAMPAIGN_HELP_TEXT
    assert "Action - Who - What - Where" in CAMPAIGN_HELP_TEXT
    assert "and confirm" in CAMPAIGN_HELP_TEXT
    assert "custom message" in CAMPAIGN_HELP_TEXT.lower()
    assert "instagram" in CAMPAIGN_HELP_TEXT.lower()
    assert "Save a Contact" not in CAMPAIGN_HELP_TEXT
    assert "Talent Search" not in CAMPAIGN_HELP_TEXT


# ---------------------------------------------------------------------------
# Ordinary conversation is never mistaken for a help trigger — exact-match
# only, and messages that merely CONTAIN a trigger word don't count.
# ---------------------------------------------------------------------------
async def test_ordinary_conversation_does_not_trigger_help():
    group = f"Test CRM Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CRM_AGENT_ID, group, phone, group_members=False)
    try:
        for text in (
            "help me move Sarah to shortlist",  # contains "help" but isn't it
            "can you help with this",
            "what can you do about the delay",  # contains the phrase but not exactly
            "hi there, how's it going",
        ):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.reply != CRM_HELP_TEXT
    finally:
        await _cleanup_conversation_state(CRM_AGENT_ID, phone)
        await _restore_config(CRM_AGENT_ID, original)


async def test_casting_chatter_still_silently_ignored():
    """Regression guard: this exact behavior is asserted in
    test_casting_agent.py::test_verbless_query_ignores_ordinary_group_chatter
    — re-checked here specifically against the new help-trigger code path."""
    group = f"Test Casting Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CASTING_AGENT_ID, group, phone, group_members=True)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="lunch is ready guys",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled is False
        assert r.reply != CASTING_HELP_TEXT
    finally:
        await _cleanup_conversation_state(CASTING_AGENT_ID, phone)
        await _restore_config(CASTING_AGENT_ID, original)


# ---------------------------------------------------------------------------
# Clarification flows are never interrupted by "help".
# ---------------------------------------------------------------------------
async def test_help_does_not_interrupt_crm_field_collection():
    group = f"Test CRM Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CRM_AGENT_ID, group, phone, group_members=False)
    try:
        # Opens the intent with no fields — puts the conversation in
        # step="collecting", waiting for a name.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Save",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply != CRM_HELP_TEXT

        conv = await db.whatsapp_conversations.find_one({"agent_id": CRM_AGENT_ID, "phone": phone})
        assert conv is not None and conv.get("step") == "collecting"

        # "help" mid-flow must be treated as the answer to "what's the
        # name?", NOT as a request for the static Help Center.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="help",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply != CRM_HELP_TEXT
        assert "phone" in (r.reply or "").lower()
    finally:
        await _cleanup_conversation_state(CRM_AGENT_ID, phone)
        await _restore_config(CRM_AGENT_ID, original)


async def test_help_does_not_interrupt_casting_disambiguation():
    group = f"Test Casting Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CASTING_AGENT_ID, group, phone, group_members=True)
    project_id = f"test-cp-proj-{uuid.uuid4().hex[:8]}"
    label = f"Help Guard Brand {uuid.uuid4().hex[:6]}"
    sarah_a = f"test-cp-tal-{uuid.uuid4().hex[:8]}"
    sarah_b = f"test-cp-tal-{uuid.uuid4().hex[:8]}"
    try:
        await db.projects.insert_one({
            "id": project_id, "brand_name": label, "status": "ongoing",
            "slug": project_id, "materials": [], "created_at": _now(),
        })
        await db.talents.insert_one({"id": sarah_a, "name": "Sarah Ahuja", "tags": [], "notes": ""})
        await db.talents.insert_one({"id": sarah_b, "name": "Sarah Bhatt", "tags": [], "notes": ""})
        for tid in (sarah_a, sarah_b):
            await db.casting_pipeline.insert_one({
                "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": tid,
                "stage": "hold", "created_at": _now(), "updated_at": _now(),
            })

        # Ambiguous — two "Sarah"s in this project — opens a disambiguation.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Move Sarah to Approved in {label}.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple matching talents." in r.reply
        assert r.reply != CASTING_HELP_TEXT

        conv = await db.whatsapp_conversations.find_one({"agent_id": CASTING_AGENT_ID, "phone": phone})
        assert conv is not None  # needs_clarification kept the conversation alive

        # "help" mid-disambiguation must NOT show the static Help Center.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="help",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.reply != CASTING_HELP_TEXT

        # Neither pipeline row was touched by any of this.
        row_a = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_a})
        row_b = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": sarah_b})
        assert row_a["stage"] == "hold"
        assert row_b["stage"] == "hold"
    finally:
        await db.projects.delete_many({"id": project_id})
        await db.talents.delete_many({"id": {"$in": [sarah_a, sarah_b]}})
        await db.casting_pipeline.delete_many({"project_id": project_id})
        await _cleanup_conversation_state(CASTING_AGENT_ID, phone)
        await _restore_config(CASTING_AGENT_ID, original)


# ---------------------------------------------------------------------------
# Existing commands keep working exactly as before (light smoke check —
# the full behavior is already covered by test_casting_agent.py /
# test_whatsapp_campaign_agent.py / test_talent_search_agent.py; this just
# proves the new dispatcher branch doesn't shadow them).
# ---------------------------------------------------------------------------
async def test_existing_casting_query_command_still_works():
    group = f"Test Casting Help {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(CASTING_AGENT_ID, group, phone, group_members=True)
    project_id = f"test-cp-proj-{uuid.uuid4().hex[:8]}"
    label = f"Smoke Brand {uuid.uuid4().hex[:6]}"
    try:
        await db.projects.insert_one({
            "id": project_id, "brand_name": label, "status": "ongoing",
            "slug": project_id, "materials": [], "created_at": _now(),
        })
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Show ongoing projects",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert label in r.reply
        assert r.reply != CASTING_HELP_TEXT
    finally:
        await db.projects.delete_many({"id": project_id})
        await _cleanup_conversation_state(CASTING_AGENT_ID, phone)
        await _restore_config(CASTING_AGENT_ID, original)
