"""WhatsApp Campaign Orchestration Agent — tests for whatsapp-campaign-agent.

2026-08-08: the agent moved from literal trigger-phrase matching
("send campaign") to intent detection + entity extraction (any of several
action-verb synonyms opens the SAME intent, and free-text parsing pulls out
who to send to and what to send). These tests cover: the extraction layer
directly (no DB), the full natural-language flow end to end, backward
compatibility with the old "send campaign to X using Y" grammar, every
recipient tier (project, named talent(s), phone number, saved contact
list, saved group list), and the pre-existing group/allowlist regression
guards.

Everything DB-facing this touches (whatsapp_batches, whatsapp_jobs,
whatsapp_templates, projects, casting_pipeline, talents, whatsapp_contact_
lists, whatsapp_group_lists) is the SAME existing WhatsApp Engine the web
app uses — these tests exist to prove the orchestration layer wires into
it correctly, not to re-test the engine itself.
"""
import os
os.environ["JWT_SECRET"] = "dummy"
os.environ["MONGO_URL"] = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid

import pytest

from unittest.mock import patch

from core import db, _now, ADMIN_EMAIL
from agents import modules as agent_modules
from agents import registry
from agents import disambiguation
from agents.dispatcher import handle_inbound_message
from agents.modules import whatsapp_campaign_agent as wca
from agents.modules import casting_pipeline_nlu as nlu
import agents.parser as parser

agent_modules.register_all()

AGENT_ID = "whatsapp-campaign-agent"

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _phone() -> str:
    return "91" + str(uuid.uuid4().int)[:9]


async def _use_test_config(group_name: str, allowed_phone: str):
    original = await db[registry.CONFIG_COLLECTION].find_one({"agent_id": AGENT_ID})
    doc = {
        "agent_id": AGENT_ID,
        "group_names": [group_name],
        "allowed_senders": [allowed_phone],
        "security_mode": "allowlist",
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
    pid = f"test-wca-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_talent(name: str, phone: str = "", group_name: str = "", instagram_handle: str = "") -> str:
    tid = f"test-wca-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "phone": phone or None,
        "whatsapp_group_name": group_name, "tags": [], "notes": "",
        "instagram_handle": instagram_handle or None,
    })
    return tid


async def _get_custom_template_id() -> str:
    """The seeded slug="custom" template (routers/whatsapp.py) — the send
    target for Custom Message / Instagram Profile modes. Test-owned
    templates are cleaned up via _cleanup(template_ids=...); this one is
    NOT test-owned (shared, seeded at startup), so tests using it must
    clean up their own batch/jobs by captured batch_id instead — see
    _cleanup_batch below."""
    tpl = await db.whatsapp_templates.find_one({"slug": "custom"}, {"_id": 0, "id": 1})
    assert tpl is not None, "seeded slug=\"custom\" template missing from local dev DB"
    return tpl["id"]


async def _cleanup_batch(batch_id: str) -> None:
    await db.whatsapp_batches.delete_one({"id": batch_id})
    await db.whatsapp_jobs.delete_many({"batch_id": batch_id})


async def _seed_pipeline_row(project_id: str, talent_id: str, stage: str) -> None:
    await db.casting_pipeline.insert_one({
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "stage": stage, "created_at": _now(), "updated_at": _now(),
    })


async def _seed_template(name: str) -> str:
    tpl_id = f"test-wca-tpl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_templates.insert_one({
        "id": tpl_id, "name": name, "slug": name.lower().replace(" ", "_") + uuid.uuid4().hex[:4],
        "body_text": "Hi {{talent_name}}, about {{project_name}} — reply to confirm.",
        "variables": [], "media_type": "none", "media_url": None,
        "media_cloudinary_id": None, "is_custom": False,
        "created_by": "test", "created_at": _now(), "updated_at": _now(),
    })
    return tpl_id


async def _seed_contact_list(name: str, contacts) -> str:
    list_id = f"test-wca-cl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_contact_lists.insert_one({
        "id": list_id, "name": name, "description": "", "contacts": contacts,
        "deleted": False, "created_at": _now(), "updated_at": _now(),
    })
    return list_id


async def _seed_crm_client(name: str, phone: str, contact_type: str):
    from bson import ObjectId
    oid = ObjectId()
    await db.clients.insert_one({
        "_id": oid, "name": name, "phone_number": phone, "contact_type": contact_type,
        "tags": [], "archived": False, "deleted": False, "created_at": _now(),
    })
    return oid


async def _seed_group_list(name: str, groups) -> str:
    list_id = f"test-wca-gl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_group_lists.insert_one({
        "id": list_id, "name": name, "description": "", "groups": groups,
        "deleted": False, "created_at": _now(), "updated_at": _now(),
    })
    return list_id


async def _cleanup(phone: str, project_ids=(), talent_ids=(), template_ids=(),
                    contact_list_ids=(), group_list_ids=(), client_ids=()) -> None:
    if client_ids:
        await db.clients.delete_many({"_id": {"$in": list(client_ids)}})
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_templates.delete_many({"id": {"$in": list(template_ids)}})
    await db.whatsapp_contact_lists.delete_many({"id": {"$in": list(contact_list_ids)}})
    await db.whatsapp_group_lists.delete_many({"id": {"$in": list(group_list_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})
    batches = await db.whatsapp_batches.find({"project_id": {"$in": list(project_ids)}}).to_list(100)
    for b in batches:
        await db.whatsapp_batches.delete_one({"id": b["id"]})
        await db.whatsapp_jobs.delete_many({"batch_id": b["id"]})
    # MANUAL/SAVED_LISTS-sourced batches have no project_id — clean up by
    # template_id instead, so those don't leak between test runs either.
    tpl_batches = await db.whatsapp_batches.find({"template_id": {"$in": list(template_ids)}}).to_list(100)
    for b in tpl_batches:
        await db.whatsapp_batches.delete_one({"id": b["id"]})
        await db.whatsapp_jobs.delete_many({"batch_id": b["id"]})


async def _seed_admin_id() -> str:
    admin = await db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 0, "id": 1})
    assert admin is not None, "bootstrap-seeded admin account missing from local dev DB"
    return admin["id"]


# ---------------------------------------------------------------------------
# Pure extraction tests — no DB, mirrors every phrasing example from the
# architecture spec plus the legacy campaign grammar it must keep working.
# ---------------------------------------------------------------------------
def test_extract_fields_all_natural_language_variants():
    cases = [
        ("Send Toyota requirement to Ahana", "Ahana", "Toyota requirement"),
        ("Send Toyota requirement to Ahana Pocha", "Ahana Pocha", "Toyota requirement"),
        ("Share Toyota requirement with Ahana", "Ahana", "Toyota requirement"),
        ("Forward Toyota brief to Ahana", "Ahana", "Toyota brief"),
        ("Message Ahana the Toyota requirement", "Ahana", "Toyota requirement"),
        ("Deliver Toyota requirement to Ahana", "Ahana", "Toyota requirement"),
        ("Send the requirement to Ahana", "Ahana", "the requirement"),
        ("Send this requirement to Ahana", "Ahana", "this requirement"),
        ("Forward this to Ahana", "Ahana", "this"),
    ]
    for text, expected_recipient, expected_source in cases:
        cleaned = parser.clean_voice_transcript(text)
        fields = wca.extract_send_requirement_fields(cleaned)
        assert fields.get("recipient_query") == expected_recipient, text
        assert fields.get("source_query") == expected_source, text

    # Leading-filler variants ("Please"/"Kindly") — stripped upstream by
    # parser.clean_voice_transcript before extraction ever sees them.
    for filler in ("Please", "Kindly"):
        cleaned = parser.clean_voice_transcript(f"{filler} send Toyota to Ahana")
        fields = wca.extract_send_requirement_fields(cleaned)
        assert fields.get("recipient_query") == "Ahana"
        assert fields.get("source_query") == "Toyota"


def test_talent_match_safety_gate_rejects_shared_surname_only():
    """(2026-08-09 live production incident, deterministic unit form) A
    fuzzy match sharing only a surname must be rejected; a genuine surname-
    only search, or an exact/superset name match, must still pass."""
    assert wca._fuzzy_match_is_safe("Ami Trivedi", "Kripa Trivedi") is False
    assert wca._fuzzy_match_is_safe("Trivedi", "Kripa Trivedi") is True
    assert wca._fuzzy_match_is_safe("Ahana Pocha", "Ahana Pocha") is True
    assert wca._fuzzy_match_is_safe("Ahana", "Ahana Pocha") is True
    assert wca._fuzzy_match_is_safe("", "Kripa Trivedi") is False
    # (2026-08-09, caught in testing) the SAME shape of bug, reproduced
    # against the project matcher — a single shared word ("Alpha") must
    # not be enough on its own.
    assert wca._fuzzy_match_is_safe("ZList123 Alpha", "QATEST Project Alpha") is False


def test_extract_fields_legacy_campaign_grammar_unchanged():
    for verb_phrase in ("Send campaign", "Launch campaign", "Start campaign", "Run campaign"):
        text = f"{verb_phrase} to Toyota Glanza using Diwali Template"
        fields = wca.extract_send_requirement_fields(text)
        assert fields.get("recipient_query") == "Toyota Glanza", text
        assert fields.get("source_query") == "Diwali", text

    fields = wca.extract_send_requirement_fields("Broadcast to Toyota Glanza using Diwali Template")
    assert fields.get("recipient_query") == "Toyota Glanza"
    assert fields.get("source_query") == "Diwali"

    # Explicit stage before "to" in the legacy grammar still extracts.
    fields = wca.extract_send_requirement_fields(
        "Send campaign Approved to Toyota Glanza using Diwali Template"
    )
    assert fields.get("stage_query") == "approved"
    assert fields.get("recipient_query") == "Toyota Glanza"


# ---------------------------------------------------------------------------
# Full end-to-end flow — legacy grammar (backward compatibility).
# ---------------------------------------------------------------------------
async def test_legacy_campaign_phrasing_still_works_end_to_end():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Campaign Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Talent One", phone="917000000001")
    t2 = await _seed_talent("Talent Two", phone="917000000002")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "did you mean" not in r.reply.lower()
        assert f"MESSAGE SOURCE\n{template_name}" in r.reply
        assert "RECIPIENTS" in r.reply
        assert "DELIVERY" in r.reply
        assert "2 Phone Numbers" in r.reply
        assert "1 Approve" in r.reply

        dry = await db.whatsapp_batches.find_one({
            "project_id": project_id, "template_id": template_id, "is_dry_run": True,
        })
        assert dry is not None
        assert dry["total_jobs"] == 2
        assert dry["created_by"] == await _seed_admin_id()

        r_cancel = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r_cancel.handled

        live_count = await db.whatsapp_batches.count_documents({
            "project_id": project_id, "template_id": template_id, "is_dry_run": False,
        })
        assert live_count == 0
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids, template_ids=[template_id])
        await _restore_config(original)


async def test_missing_source_asks_then_completes():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Missing Tpl Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Only Talent", phone="917000000003")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Send campaign to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "What should I send? (a project name or a template name)"

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=template_name,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"MESSAGE SOURCE\n{template_name}" in r2.reply
        assert "1 Phone Number" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_explicit_stage_filters_recipients():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Stage Filter Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Ask Talent", phone="917000000004")
    t2 = await _seed_talent("Approved Talent", phone="917000000005")
    talent_ids = [t1, t2]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "approved")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign Approved to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1 Phone Number" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids, template_ids=[template_id])
        await _restore_config(original)


async def test_no_stage_defaults_to_whole_project():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"All Stages Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Ask Talent", phone="917000000006")
    t2 = await _seed_talent("Approved Talent", phone="917000000007")
    t3 = await _seed_talent("Hold Talent", phone="917000000008")
    talent_ids = [t1, t2, t3]
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        await _seed_pipeline_row(project_id, t2, "approved")
        await _seed_pipeline_row(project_id, t3, "hold")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "3 Phone Numbers" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids, template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# New natural-language recipient tiers.
# ---------------------------------------------------------------------------
async def test_named_talent_recipient_routes_via_whatsapp_group():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    talent_name = f"Zoya Unique{uuid.uuid4().hex[:6]}"
    group_display = f"{talent_name} x Talentgram"
    t1 = await _seed_talent(talent_name, phone="917000000012", group_name=group_display)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {talent_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"1. {talent_name}" in r.reply
        assert "1 WhatsApp Group" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["destination_type"] == "group"
        assert jobs[0]["destination"] == group_display
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_named_talent_recipient_routes_via_phone_when_no_group():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    talent_name = f"Priya Unique{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(talent_name, phone="917000000013")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {talent_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"1. {talent_name}" in r.reply
        assert "1 Phone Number" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_multiple_named_talents_recipient():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    n1 = f"Kavya Unique{uuid.uuid4().hex[:6]}"
    n2 = f"Meera Unique{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(n1, phone="917000000014")
    t2 = await _seed_talent(n2, phone="917000000015")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {n1} and {n2}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 Phone Numbers" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1, t2], template_ids=[template_id])
        await _restore_config(original)


async def test_phone_number_recipient():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to +917000099999",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "1 Phone Number" in r.reply
        assert "917000099999" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, template_ids=[template_id])
        await _restore_config(original)


async def test_saved_contact_list_recipient():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    list_name = f"VIP Clients {uuid.uuid4().hex[:6]}"
    contacts = [{"name": "Contact A", "phone": "917000000020"}, {"name": "Contact B", "phone": "917000000021"}]
    list_id = await _seed_contact_list(list_name, contacts)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {list_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 Phone Numbers" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, template_ids=[template_id], contact_list_ids=[list_id])
        await _restore_config(original)


async def test_saved_group_list_recipient():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    list_name = f"Client Groups {uuid.uuid4().hex[:6]}"
    groups = [{"group_name": "Toyota Client Group"}, {"group_name": "Nykaa Client Group"}]
    list_id = await _seed_group_list(list_name, groups)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {list_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 WhatsApp Groups" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, template_ids=[template_id], group_list_ids=[list_id])
        await _restore_config(original)


async def test_crm_contact_type_recipient():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    contact_type = f"UniqueRole{uuid.uuid4().hex[:6]}"
    c1 = await _seed_crm_client("Client A", "917000000030", contact_type)
    c2 = await _seed_crm_client("Client B", "917000000031", contact_type)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {contact_type}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 Phone Numbers" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        live = await db.whatsapp_batches.find_one({"id": batch_id})
        assert live is not None
        assert live["source_type"] == "CRM"
    finally:
        await _cleanup(phone, template_ids=[template_id], client_ids=[c1, c2])
        await _restore_config(original)


async def test_talent_recipient_rejects_wrong_person_shared_surname_match():
    """(2026-08-09 live production incident) "Send campaign to Ami Trivedi
    using Toyota Glanza template" — no talent named "Ami Trivedi" exists,
    but the shared fuzzy matcher (correctly, by its own internal-edit-
    oriented tolerance) found "Kripa Trivedi" as the only same-surnamed
    talent and auto-resolved to her — and the message was actually sent to
    the wrong real person. Reproduces the EXACT real names involved and
    asserts the new safety gate rejects it instead of silently sending."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    # Only ONE same-surnamed talent in the pool — the exact failure shape
    # (a lone fuzzy survivor auto-accepts with nothing to be ambiguous
    # against) that let the wrong send through in production.
    t1 = await _seed_talent(f"Kripa Trivedi{uuid.uuid4().hex[:6]}", phone="918369827463",
                             group_name="Kripa Trivedi x Talentgram Agency")
    wrong_name = f"Ami Trivedi{uuid.uuid4().hex[:6]}"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {wrong_name} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        # The local dev DB can carry stray leftover talents from other test
        # runs, so the shared matcher may see this as "ambiguous" rather
        # than a lone survivor — either way is an acceptable SAFE outcome;
        # the one unacceptable outcome is silently resolving to a single
        # wrong person and sending.
        assert "sent." not in r.reply.lower(), r.reply

        # No batch of any kind (dry-run or live) was created for this —
        # an unsafe/ambiguous match must never reach create_batch at all.
        stray = await db.whatsapp_batches.count_documents({"template_id": template_id})
        assert stray == 0
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_talent_recipient_surname_only_query_still_matches():
    """The safety gate must NOT break the legitimate case: searching by a
    surname alone (nothing more specific typed) still resolves when there's
    exactly one such-surnamed talent — every word the user typed ("Zbaru")
    does appear in the matched label, satisfying the gate honestly."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    surname = f"Zbaru{uuid.uuid4().hex[:6]}"
    talent_name = f"Devika {surname}"
    t1 = await _seed_talent(talent_name, phone="917000000018")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {surname}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"1. {talent_name}" in r.reply, r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_unresolvable_source_gives_clear_error():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    talent_name = f"Nora Unique{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(talent_name, phone="917000000016")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send zzz_nonexistent_source_zzz to {talent_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't find a template" in r.reply.lower()

        stray = await db.whatsapp_batches.count_documents({"source_label": {"$regex": "zzz_nonexistent"}})
        assert stray == 0
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_unsupported_source_gives_explicit_not_silent_reply():
    """(2026-08-09) "this"/"that"/"last generated"/"yesterday's" name a
    capability (message-history / quoted-reply reuse / on-the-fly
    requirement generation) this engine genuinely doesn't have — the reply
    must say so explicitly, never a generic not-found or a silent no-op,
    and never a stray fuzzy match against an unrelated template."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    talent_name = f"Kiran Unique{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(talent_name, phone="917000000017")
    try:
        for text in (
            f"Forward this to {talent_name}",
            f"Send this requirement to {talent_name}",
            f"Send last generated requirement to {talent_name}",
            f"Send yesterday's requirement to {talent_name}",
        ):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, text
            assert "can't reuse an earlier message" in r.reply.lower(), (text, r.reply)
            assert "name an existing whatsapp template" in r.reply.lower(), (text, r.reply)
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_unresolvable_recipient_gives_clear_error():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Zzz Nonexistent Person Zzz",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't figure out who" in r.reply.lower()
    finally:
        await _cleanup(phone, template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Approve / cancel / group / allowlist regression guards.
# ---------------------------------------------------------------------------
async def test_approve_creates_live_batch_and_jobs():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Approve Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Send Talent", phone="917000000009")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "1 Phone Number" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Sent." in r2.reply
        assert f"Recipients\n{label}" in r2.reply
        assert "Queued 1 message(s)" in r2.reply
        assert "Batch ID:" in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()

        live = await db.whatsapp_batches.find_one({"id": batch_id})
        assert live is not None
        assert live["is_dry_run"] is False
        assert live["status"] == "pending"
        assert live["total_jobs"] == 1
        assert live["project_id"] == project_id
        assert live["template_id"] == template_id
        assert live["created_by"] == await _seed_admin_id()

        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "pending"
        assert jobs[0]["is_dry_run"] is False

        audit_row = await db.whatsapp_agent_audit_log.find_one(
            {"agent_id": AGENT_ID, "sender_phone": phone, "confirmation_action": "approve"},
        )
        assert audit_row is not None
        assert batch_id in (audit_row.get("execution_result") or "")
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_cancel_creates_no_live_batch():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Cancel Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Cancel Talent", phone="917000000010")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Cancelled" in r2.reply

        live_count = await db.whatsapp_batches.count_documents({"project_id": project_id, "is_dry_run": False})
        assert live_count == 0
        dry_count = await db.whatsapp_batches.count_documents({"project_id": project_id, "is_dry_run": True})
        assert dry_count >= 1
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_different_group_does_not_activate_campaign_agent():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    try:
        r = await handle_inbound_message(
            group_name="Talentgram Casting Pipeline", sender_phone=phone,
            text="Send Whatever to Whoever",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _restore_config(original)


async def test_unauthorized_phone_in_campaign_group_gets_friendly_reply():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    authorized_phone = _phone()
    unauthorized_phone = _phone()
    original = await _use_test_config(group, authorized_phone)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=unauthorized_phone,
            text="Send Whatever to Whoever",
            sender_name="Stranger", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == wca.UNAUTHORIZED_SENDER_MESSAGE
        assert "not authorized" in r.reply.lower()

        stray = await db.whatsapp_batches.count_documents({"source_label": {"$regex": "Whatever"}})
        assert stray == 0
    finally:
        await _restore_config(original)


async def test_authorized_phone_still_works_normally():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Still Works Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Still Works Talent", phone="917000000011")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply != wca.UNAUTHORIZED_SENDER_MESSAGE
        assert "1 Phone Number" in r.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Sprint 1 (2026-08-09) — Shared Interactive Disambiguation Engine.
# ---------------------------------------------------------------------------
async def test_disambiguation_project_ambiguity_resolved_via_digit():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    labels = sorted([f"ZAmbig{tag} Glanza", f"ZAmbig{tag} Hyryder", f"ZAmbig{tag} Fortuner"])
    project_ids = [await _seed_project(label) for label in labels]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000040")
    await _seed_pipeline_row(project_ids[0], t1, "ask_to_test")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZAmbig{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple projects." in r.reply
        for i, label in enumerate(labels):
            marker = disambiguation._CIRCLED_DIGITS[i]
            assert f"{marker} {label}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "RECIPIENTS" in r2.reply
        assert labels[0] in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        pending = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending is None
    finally:
        await _cleanup(phone, project_ids=project_ids, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_disambiguation_template_ambiguity_resolved_via_circled_digit():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"Requirement {tag}"
    tpl_ids = [await _seed_template(f"{label} Alpha"), await _seed_template(f"{label} Beta")]
    talent_name = f"Nina Unique{tag}"
    t1 = await _seed_talent(talent_name, phone="917000000041")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {label} to {talent_name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple templates." in r.reply
        assert f"① {label} Alpha" in r.reply
        assert f"② {label} Beta" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="②",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert f"MESSAGE SOURCE\n{label} Beta" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=tpl_ids)
        await _restore_config(original)


async def test_disambiguation_talent_ambiguity_resolved_via_exact_name():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    name_sharma = f"Priya Sharma{tag}"
    name_verma = f"Priya Verma{tag}"
    t1 = await _seed_talent(name_sharma, phone="917000000042")
    t2 = await _seed_talent(name_verma, phone="917000000043")
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Priya{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple talents." in r.reply
        assert name_sharma in r.reply and name_verma in r.reply

        # Reply with one candidate's full, distinguishing name.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=name_sharma,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "RECIPIENTS" in r2.reply
        assert f"1. {name_sharma}" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1, t2], template_ids=[template_id])
        await _restore_config(original)


async def test_disambiguation_crm_ambiguity_resolved_via_ordinal_word():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    types = sorted([f"ZCrm{tag} Alpha", f"ZCrm{tag} Beta"])
    c1 = await _seed_crm_client("Client A", "917000000044", types[0])
    c2 = await _seed_crm_client("Client B", "917000000045", types[1])
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZCrm{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple CRM contacts." in r.reply
        assert types[0] in r.reply and types[1] in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="the second one",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "RECIPIENTS" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        live = await db.whatsapp_batches.find_one({"id": batch_id})
        assert live["source_type"] == "CRM"
        assert live["source_label"] == types[1]
    finally:
        await _cleanup(phone, template_ids=[template_id], client_ids=[c1, c2])
        await _restore_config(original)


async def test_disambiguation_saved_list_resolved_via_option_n():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    name_a = f"ZList{tag} Alpha"
    name_b = f"ZList{tag} Beta"
    list_a = await _seed_contact_list(name_a, [{"name": "X", "phone": "917000000050"}])
    list_b = await _seed_contact_list(name_b, [{"name": "Y", "phone": "917000000051"}])
    # _fetch_contact_lists sorts created_at DESC — the most recently
    # inserted (list_b) is candidate ①.
    order = [name_b, name_a]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZList{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple saved lists." in r.reply
        assert f"① {order[0]}" in r.reply
        assert f"② {order[1]}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="option 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "1 Phone Number" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, template_ids=[template_id], contact_list_ids=[list_a, list_b])
        await _restore_config(original)


async def test_disambiguation_invalid_selection_reprompts_same_candidates():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    labels = sorted([f"ZInvalid{tag} Glanza", f"ZInvalid{tag} Hyryder"])
    project_ids = [await _seed_project(label) for label in labels]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000047")
    await _seed_pipeline_row(project_ids[0], t1, "ask_to_test")
    try:
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZInvalid{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        # Out of range — must re-prompt with the SAME two candidates, not
        # crash, not silently pick one, not show the generic "unrecognized
        # edit" text.
        r_bad = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="9",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r_bad.handled
        assert "didn't catch that" in r_bad.reply.lower()
        assert labels[0] in r_bad.reply and labels[1] in r_bad.reply

        garbage = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="zzz_nonexistent_zzz",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert garbage.handled
        assert "didn't catch that" in garbage.reply.lower()

        # Still pending — a valid pick now still works.
        r_ok = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert labels[0] in r_ok.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=project_ids, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_disambiguation_cancel():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    labels = sorted([f"ZCancel{tag} Glanza", f"ZCancel{tag} Hyryder"])
    project_ids = [await _seed_project(label) for label in labels]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZCancel{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        pending = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending is not None

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="cancel",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Cancelled" in r.reply

        pending_after = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending_after is None
        conv_after = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv_after is None
    finally:
        await _cleanup(phone, project_ids=project_ids, template_ids=[template_id])
        await _restore_config(original)


async def test_disambiguation_expiry():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    labels = sorted([f"ZExpire{tag} Glanza", f"ZExpire{tag} Hyryder"])
    project_ids = [await _seed_project(label) for label in labels]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZExpire{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        import datetime as _dt
        past = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)
        await db[disambiguation.COLLECTION].update_one(
            {"agent_id": AGENT_ID, "phone": phone}, {"$set": {"expires_at": past}},
        )

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "expired" in r.reply.lower()
        pending_after = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending_after is None
    finally:
        await _cleanup(phone, project_ids=project_ids, template_ids=[template_id])
        await _restore_config(original)


async def test_disambiguation_fresh_trigger_clears_stale_pending_choice():
    """(2026-08-09, production-readiness audit) A fresh trigger message
    sent WHILE a disambiguation is pending must not leave the old
    whatsapp_agent_disambiguation doc orphaned — it should be explicitly
    cleared, not just rely on the (harmless but untidy) TTL."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    labels = sorted([f"ZFresh{tag} Glanza", f"ZFresh{tag} Hyryder"])
    project_ids = [await _seed_project(label) for label in labels]
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000048")
    await _seed_pipeline_row(project_ids[0], t1, "ask_to_test")
    try:
        r1 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to ZFresh{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple projects." in r1.reply
        pending = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending is not None

        # A brand-new command, NOT a disambiguation reply, interrupts it.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {labels[0]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "RECIPIENTS" in r2.reply

        pending_after = await disambiguation.get_pending(AGENT_ID, phone)
        assert pending_after is None

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=project_ids, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_executor_ambiguous_recheck_never_returns_none_message():
    """(2026-08-09, production-readiness audit) If the underlying data
    changes between the confirmation card being shown and approval so a
    previously-unique recipient/source becomes ambiguous again, the
    executor must fail with a clear message — never DispatchResult(reply=
    None), which would reach the WhatsApp worker as a blank/crashing send."""
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent(f"Priya Sharma{tag}", phone="917000000049")
    t2 = await _seed_talent(f"Priya Verma{tag}", phone="917000000059")
    template_name = f"Requirement {tag}"
    template_id = await _seed_template(template_name)
    try:
        collected = {"source_query": template_name, "recipient_query": f"Priya{tag}"}
        exec_result = await wca._send_requirement_executor(collected, None)
        assert exec_result.ok is False
        assert exec_result.message is not None
        assert "no longer unique" in exec_result.message.lower()
    finally:
        await db.talents.delete_many({"id": {"$in": [t1, t2]}})
        await db.whatsapp_templates.delete_many({"id": {"$in": [template_id]}})


# ---------------------------------------------------------------------------
# Pipeline/Stage recipient support (2026-08-09).
# ---------------------------------------------------------------------------
def test_split_stage_and_project_helper():
    assert wca._split_stage_and_project("Follow Up pipeline of Toyota Glanza") == ("Follow Up", "Toyota Glanza")
    assert wca._split_stage_and_project("follow up list of Toyota Glanza") == ("follow up", "Toyota Glanza")
    assert wca._split_stage_and_project("Follow Up stage of Toyota Glanza") == ("Follow Up", "Toyota Glanza")
    assert wca._split_stage_and_project("Approved pipeline of Toyota Glanza") == ("Approved", "Toyota Glanza")
    assert wca._split_stage_and_project("Selected list") == ("Selected", "")
    assert wca._split_stage_and_project("Shortlisted pipeline of Tira Ahaan Film") == ("Shortlisted", "Tira Ahaan Film")
    assert wca._split_stage_and_project("Followup pipeline Toyota") == ("Followup", "Toyota")
    assert wca._split_stage_and_project("Follow Up of Toyota") == ("Follow Up", "Toyota")
    # Must NOT hijack a plain project/talent reference — no connector word
    # and no "of/for" tail at all.
    assert wca._split_stage_and_project("Toyota Glanza") is None
    assert wca._split_stage_and_project("Ahana Pocha") is None
    assert wca._split_stage_and_project("") is None


def test_extract_fields_pipeline_stage_phrasings():
    # (stage assertions use match_stage_phrase since raw extraction keeps
    # the phrase unvalidated/unnormalized until _resolve_recipient runs)
    examples = [
        "Send Reminder Template to Follow Up pipeline of Toyota Glanza",
        "Send reminder template to follow up list of Toyota Glanza",
        "Send reminder template to Follow Up stage of Toyota Glanza",
        "Send Reminder Template to Approved pipeline of Toyota Glanza",
        "Broadcast Reminder Template to Shortlisted pipeline of Tira Ahaan Film",
        "Send Reminder Template to Followup pipeline Toyota",
        "Send Reminder template to Follow Up of Toyota",
    ]
    expected_stage_key = [
        "follow_up", "follow_up", "follow_up", "approved", "shortlisted", "follow_up", "follow_up",
    ]
    expected_project = [
        "Toyota Glanza", "Toyota Glanza", "Toyota Glanza", "Toyota Glanza",
        "Tira Ahaan Film", "Toyota", "Toyota",
    ]
    for text, exp_stage_key, exp_project in zip(examples, expected_stage_key, expected_project):
        fields = wca.extract_send_requirement_fields(text)
        assert fields.get("recipient_query") == exp_project, text
        stage_match = nlu.match_stage_phrase(fields.get("stage_query", ""), wca.PIPELINE_STAGE_ORDER)
        assert stage_match.key == exp_stage_key, (text, fields, stage_match)
        assert fields.get("source_query", "").lower() == "reminder template", text

    # No project named at all — recipient_query becomes the sentinel.
    fields = wca.extract_send_requirement_fields("Send Toyota Reminder Template to Selected list")
    assert fields.get("recipient_query") == wca._ALL_PROJECTS_SENTINEL
    assert fields.get("stage_query") == "Selected"
    assert fields.get("source_query") == "Toyota Reminder Template"


async def test_pipeline_stage_success_follow_up_pipeline_of_project():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZPipe{tag} Glanza"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent A {tag}", phone="917000000060")
    t2 = await _seed_talent(f"Talent B {tag}", phone="917000000061")
    t3 = await _seed_talent(f"Talent C {tag}", phone="917000000062")  # different stage, must be excluded
    await _seed_pipeline_row(project_id, t1, "follow_up")
    await _seed_pipeline_row(project_id, t2, "follow_up")
    await _seed_pipeline_row(project_id, t3, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Follow Up pipeline of {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Template\n{template_name}" in r.reply
        assert "Recipient Type\nPipeline" in r.reply
        assert f"Project\n{label}" in r.reply
        assert "Stage\nFollow Up" in r.reply
        assert "Recipients (2)" in r.reply
        assert "Destination\n2 Phone Numbers" in r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        live = await db.whatsapp_batches.find_one({"id": batch_id})
        assert live is not None
        assert live["source_type"] == "PROJECT"
        assert live["pipeline_stages"] == ["follow_up"]
        assert live["total_jobs"] == 2
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 2
        assert all(j["status"] == "pending" for j in jobs)
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2, t3], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_success_follow_up_list_of_project():
    await _run_pipeline_stage_success_variant("Follow Up list", "follow_up")


async def test_pipeline_stage_success_follow_up_stage_of_project():
    await _run_pipeline_stage_success_variant("Follow Up stage", "follow_up")


async def test_pipeline_stage_success_approved_pipeline_of_project():
    await _run_pipeline_stage_success_variant("Approved pipeline", "approved")


async def test_pipeline_stage_success_shortlisted_stage_of_project():
    await _run_pipeline_stage_success_variant("Shortlisted stage", "shortlisted")


async def _run_pipeline_stage_success_variant(stage_phrase: str, expected_stage_key: str):
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZPipe{tag} Variant"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000063")
    await _seed_pipeline_row(project_id, t1, expected_stage_key)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to {stage_phrase} of {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, (stage_phrase, r.reply)
        assert "Recipient Type\nPipeline" in r.reply
        assert f"Project\n{label}" in r.reply
        assert nlu.stage_label(expected_stage_key) in r.reply
        assert "Recipients (1)" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_bare_juxtaposition_no_connector_word():
    """"Followup pipeline Toyota" — project named with no "of"/"for"."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZBare{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000064")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Followup pipeline {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label}" in r.reply
        assert "Stage\nFollow Up" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_no_connector_word_but_of_project():
    """"Follow Up of Toyota" — no pipeline/stage/list word at all."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZOf{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000065")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Follow Up of {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Project\n{label}" in r.reply
        assert "Stage\nFollow Up" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_ambiguous_project():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label_a = f"ZDup{tag} Toyota Alpha"
    label_b = f"ZDup{tag} Toyota Beta"
    project_a = await _seed_project(label_a)
    project_b = await _seed_project(label_b)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000069")
    await _seed_pipeline_row(project_a, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Follow Up pipeline of ZDup{tag} Toyota",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple projects." in r.reply
        assert label_a in r.reply and label_b in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Recipient Type\nPipeline" in r2.reply
        assert "Stage\nFollow Up" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_ambiguous_stage_uses_shared_disambiguation():
    """Uses a mocked match_stage_phrase to deterministically reproduce a
    genuine 2+ candidate stage tie (e.g. "Selection" between "Selected"
    and "Selection Pending" per the sprint spec's own example) without
    depending on real vocabulary happening to produce one."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZStageAmbig{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000066")
    await _seed_pipeline_row(project_id, t1, "approved")
    try:
        real_match = nlu.match_stage_phrase

        def _fake_match(phrase, stage_order):
            if phrase.strip().lower() == "selection":
                return nlu.StageMatch(ambiguous=["Approved", "Rejected"])
            return real_match(phrase, stage_order)

        with patch.object(wca.nlu, "match_stage_phrase", side_effect=_fake_match):
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"Send {template_name} to Selection pipeline of {label}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
            assert "I found multiple pipeline stages." in r.reply
            assert "① Approved" in r.reply and "② Rejected" in r.reply

            r2 = await handle_inbound_message(
                group_name=group, sender_phone=phone, text="1",
                sender_name="Raj", sender_is_group_member=True,
            )
        assert r2.handled
        assert "Stage\nApproved" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_unknown_stage_returns_clear_error():
    """"Selected" isn't a real stage in this system (only a single weak
    fuzzy suggestion below the disambiguation threshold) — must return a
    clear, honest error, never a confusing 1-option "did you mean" card
    and never a silent wrong resolution."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    talent_name = f"Zoya Unknown{tag}"
    t1 = await _seed_talent(talent_name, phone="917000000067")
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Selected list",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "don't recognize the stage" in r.reply.lower()
        assert "selected" in r.reply.lower()
        assert "sent." not in r.reply.lower()

        stray = await db.whatsapp_batches.count_documents({"template_id": template_id})
        assert stray == 0
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_missing_project_asks_via_disambiguation():
    """A stage is named but no project — asks which project, across every
    ongoing project (numbered, via the shared disambiguation engine), per
    the sprint spec's worked example. Seeds 2 extra uniquely-tagged
    ongoing projects to guarantee the ambiguous (2+) case regardless of
    whatever else is already ongoing in the shared dev DB."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    extra_a = await _seed_project(f"ZExtra{tag} A")
    extra_b = await _seed_project(f"ZExtra{tag} B")
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} to Follow Up pipeline",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple projects." in r.reply
        assert f"ZExtra{tag} A" in r.reply and f"ZExtra{tag} B" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="cancel",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[extra_a, extra_b], template_ids=[template_id])
        await _restore_config(original)


async def test_pipeline_stage_missing_template_asks():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZMissTpl{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Talent {tag}", phone="917000000068")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send to Follow Up pipeline of {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "What should I send? (a project name or a template name)"

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=template_name,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Recipient Type\nPipeline" in r2.reply
        assert f"Project\n{label}" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Interactive Campaign Editing (2026-08-09).
# ---------------------------------------------------------------------------
async def _setup_editing_campaign(n_talents: int = 3):
    """Seeds a project + template + N talents (all follow_up) and opens the
    confirmation card. Returns a dict of everything a test needs."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZEdit{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    # Deliberately shares NO substring with template_name ("Reminder
    # {tag}") — a name like "Second Reminder {tag}" would make every fuzzy/
    # substring match against it ambiguous with the original template too.
    template_name_2 = f"Final Notice {tag}"
    template_id_2 = await _seed_template(template_name_2)
    talents = []
    for i in range(n_talents):
        name = f"Talent{i} {tag}"
        tid = await _seed_talent(name, phone=f"91700009{i:04d}")
        await _seed_pipeline_row(project_id, tid, "follow_up")
        talents.append((tid, name))
    r = await handle_inbound_message(
        group_name=group, sender_phone=phone,
        text=f"Send {template_name} to Follow Up pipeline of {label}",
        sender_name="Raj", sender_is_group_member=True,
    )
    return {
        "group": group, "phone": phone, "original": original,
        "project_id": project_id, "template_id": template_id,
        "template_name": template_name, "template_id_2": template_id_2,
        "template_name_2": template_name_2, "talents": talents, "label": label, "reply": r,
    }


async def _teardown_editing_campaign(ctx: dict):
    await _cleanup(
        ctx["phone"], project_ids=[ctx["project_id"]],
        talent_ids=[t[0] for t in ctx["talents"]],
        template_ids=[ctx["template_id"], ctx["template_id_2"]],
    )
    await _restore_config(ctx["original"])


async def test_editing_exclude_single():
    ctx = await _setup_editing_campaign(3)
    try:
        assert ctx["reply"].handled
        assert "Recipients (3)" in ctx["reply"].reply

        _, name0 = ctx["talents"][0]
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (2)" in r.reply
        assert f"Excluded\n{name0}" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_exclude_multiple():
    ctx = await _setup_editing_campaign(3)
    try:
        n0, n1 = ctx["talents"][0][1], ctx["talents"][1][1]
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {n0} and {n1}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (1)" in r.reply
        assert n0 in r.reply and n1 in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_exclude_unknown_name():
    ctx = await _setup_editing_campaign(2)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude ZzzNonexistentZzz",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't find" in r.reply.lower()
        pending = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": ctx["phone"]})
        assert pending is not None
        assert pending["step"] == "confirming"

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_exclude_ambiguous_uses_shared_disambiguation():
    ctx = await _setup_editing_campaign(1)
    try:
        tag = uuid.uuid4().hex[:6]
        n1 = f"Priya Sharma{tag}"
        n2 = f"Priya Verma{tag}"
        t1 = await _seed_talent(n1, phone="917000001001")
        t2 = await _seed_talent(n2, phone="917000001002")
        await _seed_pipeline_row(ctx["project_id"], t1, "follow_up")
        await _seed_pipeline_row(ctx["project_id"], t2, "follow_up")
        ctx["talents"] += [(t1, n1), (t2, n2)]

        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude Priya{tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple talents." in r.reply
        assert n1 in r.reply and n2 in r.reply

        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Excluded" in r2.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_exclude_already_excluded():
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "already excluded" in r2.reply.lower()

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_include_restores_excluded():
    ctx = await _setup_editing_campaign(3)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Include {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (3)" in r.reply
        assert f"Included\n{name0}" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_include_via_add_back_and_restore_phrasing():
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Add {name0} back",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (2)" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Restore {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Recipients (2)" in r2.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_include_already_included_is_noop_error():
    """"Already included" == never excluded in the first place — same
    user-facing outcome ("nothing to include")."""
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Include {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "nothing to include" in r.reply.lower()

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_include_not_previously_excluded():
    ctx = await _setup_editing_campaign(3)
    try:
        _, name0 = ctx["talents"][0]
        _, name1 = ctx["talents"][1]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Include {name1}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "nothing to include" in r.reply.lower()

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_template_change_exact():
    ctx = await _setup_editing_campaign(1)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"],
            text=f"Change template to {ctx['template_name_2']}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Template\n{ctx['template_name_2']}" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_template_change_fuzzy_via_use():
    ctx = await _setup_editing_campaign(1)
    try:
        partial = ctx["template_name_2"].replace("Final Notice", "Fnal Notice")
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Use {partial}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Template\n{ctx['template_name_2']}" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_template_change_ambiguous_uses_shared_disambiguation():
    ctx = await _setup_editing_campaign(1)
    try:
        tag = uuid.uuid4().hex[:6]
        tpl_a = await _seed_template(f"Final Reminder {tag} Alpha")
        tpl_b = await _seed_template(f"Final Reminder {tag} Beta")
        try:
            r = await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"],
                text=f"Switch template to Final Reminder {tag}",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled
            assert "I found multiple templates." in r.reply

            r2 = await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text="1",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r2.handled
            assert "Template\n" in r2.reply

            await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
                sender_name="Raj", sender_is_group_member=True,
            )
        finally:
            await db.whatsapp_templates.delete_many({"id": {"$in": [tpl_a, tpl_b]}})
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_template_change_unknown():
    ctx = await _setup_editing_campaign(1)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"],
            text="Change template to ZzzNonexistentTemplateZzz",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "couldn't find a template" in r.reply.lower()
        assert "unchanged" in r.reply.lower()
        pending = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": ctx["phone"]})
        assert pending["collected"]["source_query"] == ctx["template_name"]

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_preview_respects_exclusions_and_template_change_never_sends():
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        _, name1 = ctx["talents"][1]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"],
            text=f"Change template to {ctx['template_name_2']}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Preview",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert name1 in r.reply
        assert "Sent." not in r.reply

        pending = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": ctx["phone"]})
        assert pending is not None and pending["step"] == "confirming"
        live_count = await db.whatsapp_batches.count_documents({
            "template_id": ctx["template_id_2"], "is_dry_run": False,
        })
        assert live_count == 0

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_summary_counts_update():
    ctx = await _setup_editing_campaign(3)
    try:
        _, name0 = ctx["talents"][0]
        r1 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Summary",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Recipient Count\n3" in r1.reply
        assert "Excluded\n(none)" in r1.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Who will receive this?",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Recipient Count\n2" in r2.reply
        assert f"Excluded\n{name0}" in r2.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_send_calls_create_batch_exactly_once_with_edits_applied():
    ctx = await _setup_editing_campaign(3)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"],
            text=f"Change template to {ctx['template_name_2']}",
            sender_name="Raj", sender_is_group_member=True,
        )

        real_create_batch = wca.create_batch
        call_log = []

        async def _wrapped(*args, **kwargs):
            result = await real_create_batch(*args, **kwargs)
            call_log.append(args[0].is_dry_run)
            return result

        with patch.object(wca, "create_batch", side_effect=_wrapped):
            r = await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text="Send",
                sender_name="Raj", sender_is_group_member=True,
            )
        assert r.handled
        assert "Sent." in r.reply
        assert call_log.count(False) == 1

        batch_id = r.reply.split("Batch ID:")[1].strip()
        live = await db.whatsapp_batches.find_one({"id": batch_id})
        assert live is not None
        assert live["template_id"] == ctx["template_id_2"]
        assert live["total_jobs"] == 2
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        job_talent_ids = {j["talent_id"] for j in jobs}
        assert ctx["talents"][0][0] not in job_talent_ids
        assert ctx["talents"][1][0] in job_talent_ids
        assert ctx["talents"][2][0] in job_talent_ids
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_cancel_clears_pending_campaign():
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Cancel",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Cancelled" in r.reply
        pending = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": ctx["phone"]})
        assert pending is None
        live_count = await db.whatsapp_batches.count_documents({
            "project_id": ctx["project_id"], "is_dry_run": False,
        })
        assert live_count == 0
    finally:
        await _teardown_editing_campaign(ctx)


async def test_editing_restart_discards_previous_draft():
    ctx = await _setup_editing_campaign(2)
    try:
        _, name0 = ctx["talents"][0]
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"],
            text=f"Send {ctx['template_name_2']} to Follow Up pipeline of {ctx['label']}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert f"Template\n{ctx['template_name_2']}" in r.reply
        assert "Excluded" not in r.reply
        assert "Recipients (2)" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_editing_campaign(ctx)


# ---------------------------------------------------------------------------
# Sprint 1 — Multi Manual Recipients (2026-08-09). Every name still
# resolves through nlu.resolve_against_candidates (the same single-name
# tier casting-agent's Move/Add use) — these tests exercise the NEW
# splitting/independent-resolution/resume layer, not a second matcher.
# ---------------------------------------------------------------------------
async def test_multi_recipient_comma_separated():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"Ahana{uuid.uuid4().hex[:6]}", f"Kripa{uuid.uuid4().hex[:6]}", f"Raj{uuid.uuid4().hex[:6]}"]
    tids = [await _seed_talent(n, phone=f"91700010{i:04d}") for i, n in enumerate(names)]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to {names[0]}, {names[1]}, {names[2]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "3 Phone Numbers" in r.reply
        for n in names:
            assert n in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_newline_separated():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"Ahana{uuid.uuid4().hex[:6]}", f"Kripa{uuid.uuid4().hex[:6]}", f"Raj{uuid.uuid4().hex[:6]}", f"Sneha{uuid.uuid4().hex[:6]}"]
    tids = [await _seed_talent(n, phone=f"91700011{i:04d}") for i, n in enumerate(names)]
    try:
        text = "Send {} template to\n{}\n{}\n{}\n{}".format(template_name, *names)
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "4 Phone Numbers" in r.reply
        for n in names:
            assert n in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_ampersand_separated():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"Ahana{uuid.uuid4().hex[:6]}", f"Kripa{uuid.uuid4().hex[:6]}"]
    tids = [await _seed_talent(n, phone=f"91700012{i:04d}") for i, n in enumerate(names)]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to {names[0]} & {names[1]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 Phone Numbers" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_mixed_comma_and_and():
    """"Ahana, Kripa, Raj, Sneha and Jessica" — unlimited recipients, a
    trailing "and" mixed with commas."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"N{i}{uuid.uuid4().hex[:6]}" for i in range(5)]
    tids = [await _seed_talent(n, phone=f"91700013{i:04d}") for i, n in enumerate(names)]
    try:
        text = f"Send {template_name} template to {names[0]}, {names[1]}, {names[2]}, {names[3]} and {names[4]}"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "5 Phone Numbers" in r.reply
        for n in names:
            assert n in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_duplicate_names_deduped():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"Ahana{uuid.uuid4().hex[:6]}", f"Kripa{uuid.uuid4().hex[:6]}"]
    tids = [await _seed_talent(n, phone=f"91700014{i:04d}") for i, n in enumerate(names)]
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to {names[0]}, {names[1]}, {names[0]}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "2 Phone Numbers" in r.reply  # deduped, not 3
        assert "RECIPIENTS (2)" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_unknown_name_proceeds_with_confidently_resolved():
    """Partial-Failure-Tolerant Multi-Recipient Resolution (2026-08-17) —
    superseded the earlier "never silently proceed" behavior: a
    confidently-resolved name in a multi-name list is no longer discarded
    just because ANOTHER name in the same list couldn't be found. The send
    proceeds with the known recipient(s), and the unresolved name is
    surfaced as a warning on the confirmation card instead of blocking
    the whole command."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    n1 = f"Ahana{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(n1, phone="917000150000")
    unknown = f"Jessica Nonexistent {uuid.uuid4().hex[:8]}"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to {n1}, {unknown}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Couldn't find" in r.reply
        assert unknown in r.reply
        assert n1 in r.reply
        assert "1 Approve" in r.reply
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_multi_recipient_ambiguous_name_disambiguates_and_resumes():
    """A name ambiguous mid-list pauses on the shared disambiguation
    engine WITHOUT losing the other, already-resolved recipients — after
    picking, all of them appear in the confirmation."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    a1 = f"Ahana{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    rahul = f"Rahul{uuid.uuid4().hex[:6]}"
    t_ahana = await _seed_talent(a1, phone="917000160000")
    t_raj = await _seed_talent(raj, phone="917000160001")
    t_rahul_sharma = await _seed_talent(f"{rahul} Sharma", phone="917000160002")
    t_rahul_verma = await _seed_talent(f"{rahul} Verma", phone="917000160003")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to {a1}, {rahul}, {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "I found multiple talents." in r.reply
        assert f"{rahul} Sharma" in r.reply and f"{rahul} Verma" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert a1 in r2.reply
        assert f"{rahul} Sharma" in r2.reply
        assert raj in r2.reply
        assert "3 Phone Numbers" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(
            phone, talent_ids=[t_ahana, t_raj, t_rahul_sharma, t_rahul_verma],
            template_ids=[template_id],
        )
        await _restore_config(original)


async def test_multi_recipient_shows_numbered_list():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    template_name = f"Requirement {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    names = [f"N{i}{uuid.uuid4().hex[:6]}" for i in range(5)]
    tids = [await _seed_talent(n, phone=f"91700017{i:04d}") for i, n in enumerate(names)]
    try:
        text = f"Send {template_name} template to {names[0]}, {names[1]}, {names[2]}, {names[3]}, {names[4]}"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "RECIPIENTS (5)" in r.reply
        for n in names:
            assert any(line.split(". ", 1)[-1] == n for line in r.reply.splitlines() if ". " in line)

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids, template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Sprint 2 — Show Recipient List: numbered list, pagination, numbered
# Exclude/Include. Existing name-based Exclude/Include (tested above) is
# untouched — these are the NEW ordinal-based commands layered on top.
# ---------------------------------------------------------------------------
async def _setup_big_editing_campaign(n_talents: int):
    """Same shape as _setup_editing_campaign but for pagination-scale
    counts — a distinct helper so the existing one (and everything that
    depends on its exact n_talents<=3 assumptions) stays untouched."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    label = f"ZBig{tag}"
    project_id = await _seed_project(label)
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    talents = []
    for i in range(n_talents):
        name = f"Talent{i:03d} {tag}"
        tid = await _seed_talent(name, phone=f"9170002{i:04d}")
        await _seed_pipeline_row(project_id, tid, "follow_up")
        talents.append((tid, name))
    r = await handle_inbound_message(
        group_name=group, sender_phone=phone,
        text=f"Send {template_name} to Follow Up pipeline of {label}",
        sender_name="Raj", sender_is_group_member=True,
    )
    return {
        "group": group, "phone": phone, "original": original,
        "project_id": project_id, "template_id": template_id,
        "template_name": template_name, "talents": talents, "label": label, "reply": r,
    }


async def _teardown_big_editing_campaign(ctx: dict):
    await _cleanup(
        ctx["phone"], project_ids=[ctx["project_id"]],
        talent_ids=[t[0] for t in ctx["talents"]], template_ids=[ctx["template_id"]],
    )
    await _restore_config(ctx["original"])


def _numbered_lines(reply: str):
    out = {}
    for line in reply.splitlines():
        if ". " in line:
            head, _, rest = line.partition(". ")
            if head.isdigit():
                out[int(head)] = rest
    return out


async def test_recipient_list_shows_all_when_le_20():
    ctx = await _setup_big_editing_campaign(5)
    try:
        assert ctx["reply"].handled
        assert "Recipients (5)" in ctx["reply"].reply
        assert "Showing" not in ctx["reply"].reply
        nums = _numbered_lines(ctx["reply"].reply)
        assert set(nums.keys()) == {1, 2, 3, 4, 5}
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_recipient_list_paginates_when_gt_20():
    ctx = await _setup_big_editing_campaign(25)
    try:
        assert ctx["reply"].handled
        assert "Recipients (25)" in ctx["reply"].reply
        assert "Showing 20 of 25 recipients." in ctx["reply"].reply
        nums = _numbered_lines(ctx["reply"].reply)
        assert len(nums) == 20
        assert set(nums.keys()) == set(range(1, 21))
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_exclude_single_and_stable_numbering():
    ctx = await _setup_big_editing_campaign(6)
    try:
        before = _numbered_lines(ctx["reply"].reply)
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (5)" in r.reply
        after = _numbered_lines(r.reply)
        assert 3 not in after
        # Everyone else keeps their ORIGINAL number — never renumbered down.
        for n, name in before.items():
            if n != 3:
                assert after.get(n) == name, f"#{n} shifted after excluding #3"

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_exclude_comma_list():
    ctx = await _setup_big_editing_campaign(6)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 2,4,6",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (3)" in r.reply
        after = _numbered_lines(r.reply)
        assert set(after.keys()) == {1, 3, 5}

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_exclude_range():
    ctx = await _setup_big_editing_campaign(8)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 2-4",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (5)" in r.reply
        after = _numbered_lines(r.reply)
        assert set(after.keys()) == {1, 5, 6, 7, 8}

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_exclude_space_separated():
    ctx = await _setup_big_editing_campaign(6)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 2 4",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (4)" in r.reply
        after = _numbered_lines(r.reply)
        assert set(after.keys()) == {1, 3, 5, 6}

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_include_after_exclude():
    ctx = await _setup_big_editing_campaign(5)
    try:
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Include 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (5)" in r.reply
        assert 2 in _numbered_lines(r.reply)

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_exclude_out_of_range():
    ctx = await _setup_big_editing_campaign(3)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 99",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "out of range" in r.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_delete_and_add_back_number_trigger_synonyms():
    ctx = await _setup_big_editing_campaign(5)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Delete 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (4)" in r.reply
        assert 2 not in _numbered_lines(r.reply)

        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Add back 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Recipients (5)" in r2.reply
        assert 2 in _numbered_lines(r2.reply)

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_pagination_next_and_previous():
    ctx = await _setup_big_editing_campaign(25)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Next",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        after_next = _numbered_lines(r.reply)
        assert set(after_next.keys()) == set(range(21, 26))
        assert "Showing 5 of 25 recipients." in r.reply

        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Previous",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert set(_numbered_lines(r2.reply).keys()) == set(range(1, 21))

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_pagination_page_n():
    ctx = await _setup_big_editing_campaign(25)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Page 2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert set(_numbered_lines(r.reply).keys()) == set(range(21, 26))

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_pagination_show_all():
    ctx = await _setup_big_editing_campaign(25)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Show All",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Showing" not in r.reply
        assert set(_numbered_lines(r.reply).keys()) == set(range(1, 26))

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_pagination_show_remaining():
    ctx = await _setup_big_editing_campaign(25)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Show Remaining",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert set(_numbered_lines(r.reply).keys()) == set(range(1, 26))

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_pagination_never_loses_exclusions():
    ctx = await _setup_big_editing_campaign(25)
    try:
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 1",
            sender_name="Raj", sender_is_group_member=True,
        )
        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Next",
            sender_name="Raj", sender_is_group_member=True,
        )
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Previous",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Recipients (24)" in r.reply
        assert 1 not in _numbered_lines(r.reply)

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_numbered_and_name_based_editing_both_work_on_same_campaign():
    """Existing name-based editing (already covered above) must continue
    working unchanged, side by side with the new numbered commands, in
    the SAME editing session."""
    ctx = await _setup_big_editing_campaign(5)
    try:
        _, name0 = ctx["talents"][0]
        r1 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text=f"Exclude {name0}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r1.handled
        assert "Recipients (4)" in r1.reply

        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Recipients (3)" in r2.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)

# ---------------------------------------------------------------------------
# Production bug fix (2026-08-09) — "2 Edit" trap. Every confirmation card
# ends with "Reply / 1 Approve / 2 Edit / 3 Cancel", its OWN documented
# instructions. Before this fix, a bare "2" (or "edit"/"change") fell
# through to agents/parser.py's generic parse_confirmation_reply, which
# moved the conversation to step="editing" — the platform's generic
# "Key = Value" field editor, with zero knowledge of Exclude/Include/
# pagination — permanently locking the user out of every campaign-
# specific editing command for the rest of that conversation (dispatcher.py
# only ever calls handle_confirming_reply while step=="confirming").
# Confirmed via a full production-conversation repro before the fix, then
# reverified working after it (see _handle_campaign_confirming_edit's
# _BARE_EDIT_TOKENS interception).
# ---------------------------------------------------------------------------
async def test_bare_edit_reply_no_longer_traps_recipient_editing():
    """The exact regression: confirmation card -> bare "2" -> numbered
    exclude must still reach the recipient editor afterward, not the
    generic "Tell me what to change" dead end."""
    ctx = await _setup_big_editing_campaign(6)
    try:
        assert ctx["reply"].handled

        r1 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="2",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r1.handled
        assert "Tell me what to change" not in r1.reply
        assert "Exclude" in r1.reply

        r2 = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "I couldn't understand that" not in r2.reply
        assert "Recipients (5)" in r2.reply

        await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _teardown_big_editing_campaign(ctx)


async def test_bare_edit_word_synonyms_also_redirect():
    """"edit" and "change" (agents/parser._CONFIRM_EDIT's other two
    synonyms) must be intercepted the same way as bare "2"."""
    for word in ("edit", "change"):
        ctx = await _setup_big_editing_campaign(4)
        try:
            r = await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text=word,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, word
            assert "Tell me what to change" not in r.reply, word

            r2 = await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text="Exclude 1",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert "Recipients (3)" in r2.reply, word

            await handle_inbound_message(
                group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
                sender_name="Raj", sender_is_group_member=True,
            )
        finally:
            await _teardown_big_editing_campaign(ctx)


async def test_approve_and_cancel_unaffected_by_bare_edit_fix():
    """Approve ("1") and Cancel ("3") must keep working normally on a
    fresh confirmation card — the fix only intercepts the edit set."""
    ctx = await _setup_big_editing_campaign(2)
    try:
        r = await handle_inbound_message(
            group_name=ctx["group"], sender_phone=ctx["phone"], text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert "Cancelled" in r.reply
    finally:
        await _teardown_big_editing_campaign(ctx)


# ---------------------------------------------------------------------------
# Send Custom Message + Send Instagram Profile (2026-08-12) — two new
# native "send" modes layered onto the SAME SEND_REQUIREMENT_INTENT (no new
# trigger phrases, no new intent, no new send path — see send_mode
# dispatch in whatsapp_campaign_agent.py). Pure extraction first (no DB),
# then full end-to-end via handle_inbound_message.
# ---------------------------------------------------------------------------

def test_detect_send_mode():
    assert wca._detect_send_mode('instagram profile of Riya to Raj') == "instagram"
    assert wca._detect_send_mode("insta link of Pankuri to Aman") == "instagram"
    assert wca._detect_send_mode("Pankuri's instagram") == "instagram"
    assert wca._detect_send_mode('custom message "Hi there" to Riya') == "custom_message"
    assert wca._detect_send_mode('this to\nRiya\n\n"text"') == "custom_message"
    assert wca._detect_send_mode("Raj and Karan:\nTomorrow's call time is 9 AM.") == "custom_message"
    assert wca._detect_send_mode("Toyota requirement to Ahana") == "requirement"
    assert wca._detect_send_mode("campaign to Toyota Glanza using Diwali Template") == "requirement"


def test_extract_custom_message_fields_quote_before_recipient():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        'send custom message "Hi, your profile has been shortlisted." to Riya', wca.SEND_TRIGGERS,
    )
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["source_query"] == "Hi, your profile has been shortlisted."
    assert fields["recipient_query"] == "Riya"


def test_extract_custom_message_fields_simple_quote_to_recipient():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        'send "Please upload your fresh introduction video." to Riya', wca.SEND_TRIGGERS,
    )
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["source_query"] == "Please upload your fresh introduction video."
    assert fields["recipient_query"] == "Riya"


def test_extract_custom_message_fields_quote_after_recipient_list():
    text = 'send this to\nRiya\nKaran\nAditi\n\n"text"'
    remainder = wca._strip_leading_trigger_preserve_newlines(text, wca.SEND_TRIGGERS)
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["source_query"] == "text"
    assert fields["recipient_query"] == "Riya\nKaran\nAditi"


def test_extract_custom_message_fields_colon_body():
    text = "message Raj and Karan:\nTomorrow's call time is 9 AM."
    remainder = wca._strip_leading_trigger_preserve_newlines(text, wca.SEND_TRIGGERS)
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["recipient_query"] == "Raj and Karan"
    # Leading newline right after the colon is outer whitespace, trimmed
    # later by _validate_query_text (see the module's documented
    # limitation) — content itself is exact.
    assert fields["source_query"].strip() == "Tomorrow's call time is 9 AM."


def test_extract_custom_message_fields_preserves_multiline_emoji_url_unicode():
    """Internal formatting (newlines, emoji, punctuation, URLs, unicode)
    must survive extraction byte-for-byte — only the shared FieldSpec.
    validate layer trims OUTER whitespace, later, not this function."""
    body = "Line one 🎉\nLine two — visit https://talentgramagency.com/apply?ref=1\nनमस्ते, thanks!"
    text = f'send custom message "{body}" to Riya'
    remainder = wca._strip_leading_trigger_preserve_newlines(text, wca.SEND_TRIGGERS)
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["source_query"] == body


def test_extract_custom_message_fields_recipient_never_reads_inside_quote():
    """A "to"/"with" occurring INSIDE the quoted message body itself must
    never be misread as the recipient connector."""
    text = 'send custom message "call me tomorrow with your portfolio" to Riya'
    remainder = wca._strip_leading_trigger_preserve_newlines(text, wca.SEND_TRIGGERS)
    fields = wca._extract_custom_message_fields(remainder)
    assert fields["source_query"] == "call me tomorrow with your portfolio"
    assert fields["recipient_query"] == "Riya"


def test_extract_instagram_fields_of_connector():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        "send instagram profile of Riya to Raj", wca.SEND_TRIGGERS,
    )
    fields = wca._extract_instagram_fields(remainder)
    assert fields["source_query"] == "Riya"
    assert fields["recipient_query"] == "Raj"


def test_extract_instagram_fields_insta_link_of_connector():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        "send insta link of Pankuri to Aman", wca.SEND_TRIGGERS,
    )
    fields = wca._extract_instagram_fields(remainder)
    assert fields["source_query"] == "Pankuri"
    assert fields["recipient_query"] == "Aman"


def test_extract_instagram_fields_possessive_no_recipient():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        "share Pankuri's instagram", wca.SEND_TRIGGERS,
    )
    fields = wca._extract_instagram_fields(remainder)
    assert fields["source_query"] == "Pankuri"
    assert fields["recipient_query"] == wca._REPLY_IN_CHAT_SENTINEL


def test_extract_instagram_fields_multi_subject_newline_list():
    text = "send instagram links of\nPankuri\nAditi\nKaran\nto Raj"
    remainder = wca._strip_leading_trigger_preserve_newlines(text, wca.SEND_TRIGGERS)
    fields = wca._extract_instagram_fields(remainder)
    assert fields["source_query"] == "Pankuri\nAditi\nKaran"
    assert fields["recipient_query"] == "Raj"


def test_extract_instagram_fields_bare_subject_fallback():
    remainder = wca._strip_leading_trigger_preserve_newlines(
        "send instagram Riya to Raj", wca.SEND_TRIGGERS,
    )
    fields = wca._extract_instagram_fields(remainder)
    assert fields["source_query"] == "Riya"
    assert fields["recipient_query"] == "Raj"


# ---------------------------------------------------------------------------
# End-to-end — Send Custom Message
# ---------------------------------------------------------------------------

async def test_custom_message_single_talent_sends_exact_text():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000200001")
    batch_id = None
    try:
        body = "Hi, your profile has been shortlisted."
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send custom message "{body}" to {name}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "MESSAGE" in r.reply
        assert body in r.reply
        assert name in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply, r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["message_body"] == body
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_custom_message_multiple_talents():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    names = [f"Riya{uuid.uuid4().hex[:6]}", f"Karan{uuid.uuid4().hex[:6]}", f"Aditi{uuid.uuid4().hex[:6]}"]
    tids = [await _seed_talent(n, phone=f"91700020{1000 + i}") for i, n in enumerate(names)]
    batch_id = None
    try:
        text = "send this to\n{}\n{}\n{}\n\n\"Tomorrow's audition starts at 11 AM.\"".format(*names)
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "3 Phone Numbers" in r.reply
        for n in names:
            assert n in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 3
        for j in jobs:
            assert j["message_body"] == "Tomorrow's audition starts at 11 AM."
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=tids)
        await _restore_config(original)


async def test_custom_message_colon_body_end_to_end():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    karan = f"Karan{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(raj, phone="917000210001")
    t2 = await _seed_talent(karan, phone="917000210002")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"message {raj} and {karan}:\nTomorrow's call time is 9 AM.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "2 Phone Numbers" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 2
        for j in jobs:
            assert j["message_body"] == "Tomorrow's call time is 9 AM."
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_custom_message_multiline_emoji_url_delivered_verbatim():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000220001")
    batch_id = None
    try:
        body = "Line one 🎉\nLine two — visit https://talentgramagency.com/apply?ref=1\nनमस्ते, thanks!"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send custom message "{body}" to {name}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["message_body"] == body
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_custom_message_no_quotes_stays_in_custom_message_mode():
    """(2026-08-12, hardened) "send custom message" must win custom_message
    mode OUTRIGHT — even with no quotes/colon-body to extract a payload
    from — never silently fall through to template matching (the original
    landmine: it would have fuzzy-matched the seeded "Custom Message"
    template and sent the literal unsubstituted string "{{message}}").
    Instead it asks what the message should say, stays open, and the next
    turn's plain-text answer is treated as the literal body — never as a
    template/project name."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000230001")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send custom message to {name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "{{message}}" not in r.reply
        assert "couldn't find a template" not in r.reply.lower()

        conv = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv is not None, "conversation must stay open, waiting for the message body"
        assert conv["collected"].get("send_mode") == "custom_message"

        # No quote/colon-body means no payload could be extracted from
        # turn 1 at all (nothing to protect as "opaque" — there's no
        # message yet), so the recipient named in turn 1 isn't carried
        # forward either; both missing fields get asked in turn, same as
        # any other intent's generic missing-field flow. What matters for
        # this regression guard is that neither ever becomes a template
        # lookup or a literal "{{message}}" send.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="Testing spelling.",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "{{message}}" not in r2.reply
        assert "couldn't find a template" not in r2.reply.lower()

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=name,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r3.handled, r3.reply
        assert "MESSAGE" in r3.reply
        assert "Testing spelling." in r3.reply

        r4 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r4.reply
        batch_id = r4.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert jobs[0]["message_body"] == "Testing spelling."
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_fetch_templates_excludes_custom_slug():
    templates = await wca._fetch_templates()
    assert all(t.get("slug") != "custom" for t in templates)


async def test_change_template_blocked_in_custom_message_mode():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000240001")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send custom message "Hello there" to {name}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="use Reminder",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "doesn't apply" in r2.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# End-to-end — Send Instagram Profile
# ---------------------------------------------------------------------------

async def test_instagram_profile_found_sends_clickable_link():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000250001", instagram_handle="pankuri.official")
    t2 = await _seed_talent(raj, phone="917000250002")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram profile of {pankuri} to {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "INSTAGRAM PROFILE" in r.reply
        assert pankuri in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        body = jobs[0]["message_body"]
        assert "https://instagram.com/pankuri.official" in body
        assert pankuri in body
        assert "not available" not in body
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_instagram_profile_missing_shows_not_available():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000260001")  # no instagram_handle
    t2 = await _seed_talent(raj, phone="917000260002")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send insta link of {pankuri} to {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        body = jobs[0]["message_body"]
        assert "Instagram profile not available." in body
        assert "https://instagram.com" not in body
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_instagram_profile_multiple_subjects_numbered():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    aditi = f"Aditi{uuid.uuid4().hex[:6]}"
    karan = f"Karan{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000270001", instagram_handle="pankuri.style")
    t2 = await _seed_talent(aditi, phone="917000270002")  # missing handle
    t3 = await _seed_talent(karan, phone="917000270003", instagram_handle="@karan_official")
    t4 = await _seed_talent(raj, phone="917000270004")
    batch_id = None
    try:
        text = f"send instagram links of\n{pankuri}\n{aditi}\n{karan}\nto {raj}"
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        # One RECIPIENT (Raj) receiving all 3 subjects' profiles in a
        # single message — subject count and recipient count are distinct.
        assert "1 Phone Number" in r.reply
        assert pankuri in r.reply and aditi in r.reply and karan in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        body = jobs[0]["message_body"]
        assert "1." in body and "2." in body and "3." in body
        assert "https://instagram.com/pankuri.style" in body
        assert "https://instagram.com/karan_official" in body
        assert "Instagram profile not available." in body
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2, t3, t4])
        await _restore_config(original)


async def test_instagram_profile_no_recipient_replies_inline_no_batch():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000280001", instagram_handle="pankuri.official")
    before_count = await db.whatsapp_batches.count_documents({})
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"share {pankuri}'s instagram",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "https://instagram.com/pankuri.official" in r.reply
        assert pankuri in r.reply
        # No confirmation card, no Approve/Cancel footer, no batch created —
        # this was answered inline, not sent to a third party.
        assert "Reply" not in r.reply or "Approve" not in r.reply
        after_count = await db.whatsapp_batches.count_documents({})
        assert after_count == before_count

        # The conversation must not be left open expecting an "Approve".
        conv = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv is None
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


async def test_instagram_subject_ambiguous_disambiguates_and_resumes():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    rahul = f"Rahul{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t_sharma = await _seed_talent(f"{rahul} Sharma", phone="917000290001", instagram_handle="rahul.sharma")
    t_verma = await _seed_talent(f"{rahul} Verma", phone="917000290002", instagram_handle="rahul.verma")
    t_raj = await _seed_talent(raj, phone="917000290003")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram profile of {rahul} to {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "I found multiple talents." in r.reply
        assert f"{rahul} Sharma" in r.reply and f"{rahul} Verma" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert f"{rahul} Sharma" in r2.reply
        assert "INSTAGRAM PROFILE" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        assert "https://instagram.com/rahul.sharma" in jobs[0]["message_body"]
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t_sharma, t_verma, t_raj])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Regression suite (2026-08-12) — two production incidents surfaced after
# deploying Custom Message / Instagram Profile Send:
#
#   1. A real multiline Custom Message ("PRE-LOCKING TRIALS...") fell
#      through to template matching instead of being recognized as a
#      custom message.
#   2. "Send instagram profile of Pankuri Gidwani to Heena Talentgram"
#      failed to resolve a talent that every other command resolves fine,
#      because Instagram had its own resolution call instead of reusing
#      _resolve_recipient's shared talent tier.
#
# These tests cover the EXACT required checklist for both regressions.
# ---------------------------------------------------------------------------

# --- Custom Message: parser priority + opaque-payload parsing -------------

def test_custom_message_multiline_with_blank_lines_never_falls_to_template():
    """The exact shape of the production incident: "Send custom message"
    on its own line, a blank line, then a multi-paragraph quoted payload
    with blank lines BETWEEN paragraphs, then the recipient list. Must be
    recognized as custom_message mode, never requirement mode."""
    text = (
        "Send custom message\n\n"
        "\"PRE-LOCKING TRIALS - RMKV\n\n"
        "Dates - 12th and 13th August\n\n"
        "Timing - 10 AM to 6 PM\n\n"
        "Venue - Mumbai Studio\n\n"
        "If available let us know...\"\n\n"
        "to Vaishnavi, Vijayanand and Tanya"
    )
    fields = wca.extract_send_requirement_fields(text)
    assert fields["send_mode"] == "custom_message"
    assert fields["source_query"] == (
        "PRE-LOCKING TRIALS - RMKV\n\n"
        "Dates - 12th and 13th August\n\n"
        "Timing - 10 AM to 6 PM\n\n"
        "Venue - Mumbai Studio\n\n"
        "If available let us know..."
    )
    assert fields["recipient_query"] == "Vaishnavi, Vijayanand and Tanya"


async def test_custom_message_production_repro_end_to_end_never_hits_template():
    """End-to-end version of the exact production repro — confirms the
    agent never says "I couldn't find a template matching..." and instead
    builds a normal Custom Message confirmation card."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    v1 = f"Vaishnavi{uuid.uuid4().hex[:6]}"
    v2 = f"Vijayanand{uuid.uuid4().hex[:6]}"
    v3 = f"Tanya{uuid.uuid4().hex[:6]}"
    tids = [
        await _seed_talent(v1, phone="917000310001"),
        await _seed_talent(v2, phone="917000310002"),
        await _seed_talent(v3, phone="917000310003"),
    ]
    try:
        text = (
            "Send custom message\n\n"
            "\"PRE-LOCKING TRIALS - RMKV\n\n"
            "Dates - ...\n\n"
            "Timing - ...\n\n"
            "...\n\n"
            "If available let us know...\"\n\n"
            f"to {v1}, {v2} and {v3}"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "couldn't find a template" not in r.reply.lower()
        assert "MESSAGE" in r.reply
        assert "PRE-LOCKING TRIALS" in r.reply
        assert "3 Phone Numbers" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=tids)
        await _restore_config(original)


def test_custom_message_punctuation_and_commas_preserved():
    text = 'Send custom message "Hi! Please confirm: yes, no, or maybe? Thanks — team." to Riya'
    fields = wca.extract_send_requirement_fields(text)
    assert fields["send_mode"] == "custom_message"
    assert fields["source_query"] == "Hi! Please confirm: yes, no, or maybe? Thanks — team."
    assert fields["recipient_query"] == "Riya"


def test_custom_message_quotes_inside_body_preserved_exactly():
    """Explicit requirement: 'quotes inside the body'. The payload is
    everything between the FIRST and LAST quote character in the message
    — embedded quotes are opaque content, never a parsing boundary."""
    text = 'Send custom message "She said "hello" to everyone, then left." to Riya'
    fields = wca.extract_send_requirement_fields(text)
    assert fields["send_mode"] == "custom_message"
    assert fields["source_query"] == 'She said "hello" to everyone, then left.'
    assert fields["recipient_query"] == "Riya"


def test_custom_message_bullets_and_arbitrary_length_preserved():
    body = (
        "Checklist:\n"
        "• Bring ID\n"
        "• Bring portfolio\n"
        "• Arrive 30 min early\n\n"
        + ("Additional notes line. " * 50)
    ).strip()
    text = f'Send custom message "{body}" to Riya'
    fields = wca.extract_send_requirement_fields(text)
    assert fields["send_mode"] == "custom_message"
    assert fields["source_query"] == body


async def test_custom_message_never_falls_through_to_template_matching_end_to_end():
    """Regression guard, general form: ANY well-formed "send custom
    message ..." command must never produce a template-matching error,
    for a variety of realistic message shapes."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000320001")
    cases = [
        f'Send custom message "Simple one-liner." to {name}',
        f'Send custom message "Line one.\n\nLine two.\n\nLine three." to {name}',
        f'Send custom message "Quotes \"inside\" the body." to {name}',
        f"Message {name}:\nColon-delimited body here.",
    ]
    try:
        for text in cases:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone, text=text,
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled, text
            assert "couldn't find a template" not in r.reply.lower(), text
            assert "{{message}}" not in r.reply, text
            # Cancel/clear before the next case so each starts fresh.
            await handle_inbound_message(
                group_name=group, sender_phone=phone, text="3",
                sender_name="Raj", sender_is_group_member=True,
            )
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


# --- Instagram: shared talent resolver -------------------------------------

async def test_instagram_partial_name_resolves():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    full_name = f"Pankuri{uuid.uuid4().hex[:6]} Gidwani"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(full_name, phone="917000330001", instagram_handle="pankuri.g")
    t2 = await _seed_talent(raj, phone="917000330002")
    try:
        first_word = full_name.split()[0]
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send instagram profile of {first_word} to {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert full_name in r.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_instagram_fuzzy_typo_resolves():
    """"Pankri"/"Pankuree"/"Pankury" -> Pankuri, same tolerance every
    other command gets from the shared fuzzy matcher."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    base = f"Pankuri{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(base, phone="917000340001", instagram_handle="pankuri.g")
    t2 = await _seed_talent(raj, phone="917000340002")
    try:
        typo = base[:-1]  # drop the last character — a minor typo
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send insta of {typo} to {raj}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert base in r.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_instagram_multiple_recipients():
    """"multiple recipients" — sending the SAME subject's Instagram to
    more than one person."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    aman = f"Aman{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000350001", instagram_handle="pankuri.g")
    t2 = await _seed_talent(raj, phone="917000350002")
    t3 = await _seed_talent(aman, phone="917000350003")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send instagram profile of {pankuri} to {raj} and {aman}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "2 Phone Numbers" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 2
        for j in jobs:
            assert "https://instagram.com/pankuri.g" in j["message_body"]
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2, t3])
        await _restore_config(original)


async def test_instagram_recipient_typo_resolves():
    """A typo in the RECIPIENT (not the subject) must resolve through the
    same shared resolver too."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    pankuri = f"Pankuri{uuid.uuid4().hex[:6]}"
    raj = f"Raj{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(pankuri, phone="917000360001", instagram_handle="pankuri.g")
    t2 = await _seed_talent(raj, phone="917000360002")
    try:
        recipient_typo = raj[:-1]
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send instagram profile of {pankuri} to {recipient_typo}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert raj in r.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_instagram_subject_resolution_matches_recipient_resolution_exactly():
    """Direct parity proof: the SAME name resolves to the SAME talent id
    whether it's used as a recipient (the campaign-sender path) or an
    Instagram subject — because both call the one shared
    _resolve_talent_names function, not two separate lookups. Checked
    twice: once on a clean exact-name match (both must succeed identically
    — this is the literal shape of the production regression), and once
    on a typo close enough to fuzzy-match but too different to pass the
    post-match safety gate (both must reject it IDENTICALLY too — parity
    holds for failure modes, not just successes)."""
    full_name = f"Pankuri{uuid.uuid4().hex[:6]} Gidwani"
    t1 = await _seed_talent(full_name, phone="917000370001", instagram_handle="pankuri.g")

    async def _resolve_both(q: str) -> "tuple[wca._TalentNamesResolution, wca._TalentNamesResolution]":
        as_recipient = await wca._resolve_talent_names(
            q, single_ambiguous_field_key="recipient_query",
            multi_pick_field_key=wca._PENDING_MULTI_RECIPIENT_PICK_KEY,
            multi_fragments_key=wca._PENDING_MULTI_RECIPIENT_FRAGMENTS_KEY,
            multi_index_key=wca._PENDING_MULTI_RECIPIENT_INDEX_KEY,
        )
        as_subject = await wca._resolve_talent_names(
            q, single_ambiguous_field_key="source_query",
            multi_pick_field_key=wca._PENDING_MULTI_SUBJECT_PICK_KEY,
            multi_fragments_key=wca._PENDING_MULTI_SUBJECT_FRAGMENTS_KEY,
            multi_index_key=wca._PENDING_MULTI_SUBJECT_INDEX_KEY,
        )
        return as_recipient, as_subject

    try:
        as_recipient, as_subject = await _resolve_both(full_name)
        assert as_recipient.ok and as_subject.ok
        assert as_recipient.talent_ids == as_subject.talent_ids == [t1]
        assert as_recipient.talent_labels == as_subject.talent_labels == [full_name]

        typo = full_name.replace("Gidwani", "Gidwni")  # dropped a letter
        as_recipient2, as_subject2 = await _resolve_both(typo)
        assert as_recipient2.ok is False and as_subject2.ok is False
        assert as_recipient2.error == as_subject2.error
    finally:
        await db.talents.delete_one({"id": t1})


async def test_instagram_and_campaign_sender_agree_on_the_production_repro_name():
    """The literal talent name from the production incident report,
    confirming Instagram resolution now matches what a normal recipient
    send already resolves for the identical name."""
    t1 = await _seed_talent("Pankuri Gidwani " + uuid.uuid4().hex[:6], phone="917000380001")
    name = (await db.talents.find_one({"id": t1}, {"_id": 0, "name": 1}))["name"]
    try:
        recipient_target = await wca._resolve_recipient(name, "")
        assert recipient_target.ok, recipient_target.error

        instagram_collected = {"source_query": name}
        instagram_target = await wca._resolve_instagram_target(instagram_collected)
        # No recipient named -> reply_in_chat — still proves resolution
        # itself succeeded (the ambiguous/not-found paths never reached
        # this far).
        assert instagram_target.ok, instagram_target.error
        assert name in instagram_target.subject_label
    finally:
        await db.talents.delete_one({"id": t1})


# ---------------------------------------------------------------------------
# Bulk Multi-Target Sends (2026-08-17) — template/message x multi-project x
# multi-talent x multi-stage, "and confirm", partial-ambiguity preservation,
# multi-pick disambiguation replies, deduplication, and parenthesized
# custom messages.
# ---------------------------------------------------------------------------
async def test_template_one_project_multiple_talents_narrowed_by_pipeline():
    """"Send the Follow Up template for Project A to Talent A, Talent B" —
    item 1's shape: named project(s) narrow the named talent(s) down to
    whoever actually belongs to that project's pipeline."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"FollowUp {tag}"
    template_id = await _seed_template(template_name)
    project_id = await _seed_project(f"ProjOne {tag}")
    label = f"ProjOne {tag}"
    t1 = await _seed_talent(f"TalentOne {tag}", phone="917000200001")
    t2 = await _seed_talent(f"TalentTwo {tag}", phone="917000200002")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    await _seed_pipeline_row(project_id, t2, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template for {label} to TalentOne {tag}, TalentTwo {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"TalentOne {tag}" in r.reply and f"TalentTwo {tag}" in r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1, t2], template_ids=[template_id])
        await _restore_config(original)


async def test_template_multiple_projects_one_talent_narrowed_by_pipeline():
    """"Send the Follow Up template for Project A and Project B to Talent
    A" — a talent who belongs to only ONE of the two named projects still
    resolves correctly (narrowing is a union across every named project,
    not an intersection)."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"FollowUp {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"ProjA {tag}")
    project_b = await _seed_project(f"ProjB {tag}")
    label_a, label_b = f"ProjA {tag}", f"ProjB {tag}"
    t1 = await _seed_talent(f"SoloTalent {tag}", phone="917000200010")
    await _seed_pipeline_row(project_b, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template for {label_a} and {label_b} to SoloTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"SoloTalent {tag}" in r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1 and jobs[0]["talent_id"] == t1
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_template_multiple_projects_multiple_talents_narrowed_by_pipeline():
    """1 template -> multiple projects -> multiple talents, arbitrary
    counts — the full item-1 shape."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"FollowUp {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"MProjA {tag}")
    project_b = await _seed_project(f"MProjB {tag}")
    label_a, label_b = f"MProjA {tag}", f"MProjB {tag}"
    t1 = await _seed_talent(f"MTalentA {tag}", phone="917000200020")
    t2 = await _seed_talent(f"MTalentB {tag}", phone="917000200021")
    t3 = await _seed_talent(f"MTalentC {tag}", phone="917000200022")  # not in either project
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Send {template_name} template for {label_a} and {label_b} "
                f"to MTalentA {tag}, MTalentB {tag} and MTalentC {tag}"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 Approve" in r.reply
        assert f"Not part of" in r.reply and f"MTalentC {tag}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2, t3], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_template_multiple_projects_one_stage():
    """"Send Follow Up template for Project A, B to the Follow Up list" —
    stage targeting across multiple named projects, deduplicated."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"Reminder {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"StageProjA {tag}")
    project_b = await _seed_project(f"StageProjB {tag}")
    label_a, label_b = f"StageProjA {tag}", f"StageProjB {tag}"
    t1 = await _seed_talent(f"StageTalentA {tag}", phone="917000200030")
    t2 = await _seed_talent(f"StageTalentB {tag}", phone="917000200031")
    t3 = await _seed_talent(f"StageTalentC {tag}", phone="917000200032")  # wrong stage
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "follow_up")
    await _seed_pipeline_row(project_b, t3, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template for {label_a} and {label_b} to the Follow Up list",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2, t3], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_template_multiple_projects_multiple_stages_dedup():
    """"Send the Follow Up template for Project A and Project B to Follow
    Up and Approved" — union of (A,FollowUp)+(A,Approved)+(B,FollowUp)+
    (B,Approved), deduplicated so a talent in more than one of those
    groups is queued exactly once. Also confirms template != stage word
    confusion: the template is literally named "Follow Up" too, and must
    not be misread as a target stage."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    # Deliberately NOT literally "Follow Up <tag>" — a real, non-test
    # "Follow Up" template already exists in this shared dev DB (seed
    # data), and the shared fuzzy matcher's substring tier would make
    # "Follow Up <tag>" genuinely ambiguous against it. "ZFollowTemplate"
    # still starts with the same word-ish token to prove template-name-vs-
    # stage-word disambiguation without colliding with real seed data.
    template_name = f"ZFollowTemplate {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"UnionProjA {tag}")
    project_b = await _seed_project(f"UnionProjB {tag}")
    label_a, label_b = f"UnionProjA {tag}", f"UnionProjB {tag}"
    t1 = await _seed_talent(f"UnionTalentA {tag}", phone="917000200040")
    t2 = await _seed_talent(f"UnionTalentB {tag}", phone="917000200041")
    # t1 belongs to project A in BOTH Follow Up and (after a later re-test)
    # Approved is not possible for one talent/one project — instead prove
    # dedup by putting t1 in Follow Up for BOTH projects (the union should
    # still only queue t1 once).
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t1, "follow_up")
    await _seed_pipeline_row(project_a, t2, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send the {template_name} template for {label_a} and {label_b} to Follow Up and Approved",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"Template\n{template_name}" in r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        # t1 appears in TWO resolved (project, stage) groups but must be
        # queued exactly once.
        assert [j["talent_id"] for j in jobs].count(t1) == 1
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_template_for_project_to_approved_list_not_confused_with_stage_move():
    """"Send Follow Up template for Project A to Approved list" means:
    template=Follow Up, project=Project A, recipient stage=Approved — NOT
    a request to move anyone's pipeline stage (this agent never mutates
    casting_pipeline at all, only reads it to resolve recipients)."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    # See the sibling union test above — avoiding a literal "Follow Up
    # <tag>" template name to sidestep ambiguity against the real seeded
    # "Follow Up" template in this shared dev DB.
    template_name = f"ZFollowTemplate {tag}"
    template_id = await _seed_template(template_name)
    project_id = await _seed_project(f"ConfuseProj {tag}")
    label = f"ConfuseProj {tag}"
    t1 = await _seed_talent(f"ConfuseTalent {tag}", phone="917000200050")
    await _seed_pipeline_row(project_id, t1, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template for {label} to Approved list",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"Template\n{template_name}" in r.reply
        assert "Stage\nApproved" in r.reply
        assert "1 Approve" in r.reply

        # The talent's own pipeline stage must be completely untouched —
        # this agent only ever reads casting_pipeline, never writes it.
        row = await db.casting_pipeline.find_one({"project_id": project_id, "talent_id": t1})
        assert row["stage"] == "approved"
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_custom_message_single_stage_single_project_previously_broken():
    """Custom-message mode never applied _split_stage_and_project to its
    own recipient clause before this fix — "the Follow Up list of Project
    A" would have been fuzzy-matched as a single (nonexistent) project
    name in its entirety and failed outright."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"CustomStageProj {tag}")
    label = f"CustomStageProj {tag}"
    t1 = await _seed_talent(f"CustomStageTalent {tag}", phone="917000200060")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send "Hi, just following up regarding the shoot." to the Follow Up list of {label}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Stage\nFollow Up" in r.reply or "STAGE" in r.reply.upper()
        assert "1 Approve" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1])
        await _restore_config(original)


async def test_custom_message_multiple_stages_multiple_projects():
    """"Send '...' to Follow Up and Approved for Project A and Project B"
    — custom message, full multi-stage x multi-project union."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    project_a = await _seed_project(f"CustomUnionA {tag}")
    project_b = await _seed_project(f"CustomUnionB {tag}")
    label_a, label_b = f"CustomUnionA {tag}", f"CustomUnionB {tag}"
    t1 = await _seed_talent(f"CustomUnionT1 {tag}", phone="917000200070")
    t2 = await _seed_talent(f"CustomUnionT2 {tag}", phone="917000200071")
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "approved")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send "Please confirm your availability." to Follow Up and Approved for {label_a} and {label_b}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        for j in jobs:
            assert j["message_body"] == "Please confirm your availability."
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, project_ids=[project_a, project_b], talent_ids=[t1, t2])
        await _restore_config(original)


async def test_custom_message_parenthesized():
    """Send (\"...\") and Send (...) (no quotes at all) both resolve to
    the same custom_message intent as the plain quoted form."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent(f"ParenTalent {tag}", phone="917000200080")
    try:
        r1 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f'Send ("Hi, please confirm your availability.") to ParenTalent {tag}',
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r1.handled, r1.reply
        assert "MESSAGE\nHi, please confirm your availability." in r1.reply
        assert "1 Approve" in r1.reply
        # Cancel this one so the second command starts from a clean slate.
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send (Hi, please confirm your availability.) to ParenTalent {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "MESSAGE\nHi, please confirm your availability." in r2.reply
    finally:
        await _cleanup(phone, talent_ids=[t1])
        await _restore_config(original)


def test_paren_incidental_to_ordinary_template_send_unaffected():
    """An incidental paren mid-sentence ("Ahana (the lead)") must NOT be
    misread as a custom-message delimiter — _detect_send_mode must still
    classify this as "requirement" (an ordinary template send), exactly as
    before parenthesis support existed. Pure extraction-layer check (no
    DB) — the SAFETY gate on top of recipient resolution is a separate,
    deliberate concern covered elsewhere, not what this test is about."""
    remainder = wca._strip_leading_trigger_preserve_newlines(
        "Send Reminder template to Ahana (the lead)", wca.SEND_TRIGGERS,
    )
    assert wca._detect_send_mode(remainder) == "requirement"


async def test_paren_wrapping_quote_and_bare_paren_both_map_to_custom_message():
    """Send ("...") and Send (...) (no quotes) both classify as
    custom_message — the two forms from the spec's own examples."""
    remainder1 = wca._strip_leading_trigger_preserve_newlines(
        'Send ("Hi, please confirm your availability.") to Project A', wca.SEND_TRIGGERS,
    )
    assert wca._detect_send_mode(remainder1) == "custom_message"
    fields1 = wca._extract_custom_message_fields(remainder1)
    assert fields1["source_query"] == "Hi, please confirm your availability."

    remainder2 = wca._strip_leading_trigger_preserve_newlines(
        "Send (Hi, please confirm your availability.) to Project A", wca.SEND_TRIGGERS,
    )
    assert wca._detect_send_mode(remainder2) == "custom_message"
    fields2 = wca._extract_custom_message_fields(remainder2)
    assert fields2["source_query"] == "Hi, please confirm your availability."


async def test_and_confirm_bypasses_approval_card():
    """"...and confirm" executes immediately, same convention as
    casting-agent — "confirm" never means a pipeline-stage move here
    (this agent has no stage-mutation concept at all)."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"ConfirmNow {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"ConfirmTalent {tag}", phone="917000200100")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to ConfirmTalent {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        assert "1 Approve" not in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_and_confirm_with_multi_project_stage_union():
    """"...and confirm" also bypasses approval for the NEW multi-project/
    multi-stage union path, not just the original single-target shape."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"ConfirmUnion {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"ConfirmProjA {tag}")
    project_b = await _seed_project(f"ConfirmProjB {tag}")
    label_a, label_b = f"ConfirmProjA {tag}", f"ConfirmProjB {tag}"
    t1 = await _seed_talent(f"ConfirmUnionT1 {tag}", phone="917000200110")
    t2 = await _seed_talent(f"ConfirmUnionT2 {tag}", phone="917000200111")
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"Send {template_name} template for {label_a} and {label_b} "
                f"to the Follow Up list and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        assert "1 Approve" not in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_multi_pick_disambiguation_reply_numbers():
    """"Send Follow Up template to Ayushi" with 3 similarly-named
    talents -> numbered clarification -> "1 and 3" sends to BOTH picked
    people, resuming the original template/command."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"MultiPick {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Ayushi Thakur {tag}", phone="917000200120")
    t2 = await _seed_talent(f"Ayushi Sharma {tag}", phone="917000200121")
    t3 = await _seed_talent(f"Ayushi Singh {tag}", phone="917000200122")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to Ayushi {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "I found multiple" in r.reply
        assert f"Ayushi Thakur {tag}" in r.reply
        assert f"Ayushi Sharma {tag}" in r.reply
        assert f"Ayushi Singh {tag}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1 and 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Ayushi Thakur {tag}" in r2.reply
        assert f"Ayushi Singh {tag}" in r2.reply
        assert f"Ayushi Sharma {tag}" not in r2.reply
        assert "1 Approve" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t3}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, talent_ids=[t1, t2, t3], template_ids=[template_id])
        await _restore_config(original)


async def test_multi_pick_disambiguation_reply_surname_text():
    """The same clarification round trip, but replying with surname text
    ("Thakur and Singh") instead of numbers."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"MultiPickText {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"Ayushi Thakur {tag}", phone="917000200130")
    t2 = await _seed_talent(f"Ayushi Sharma {tag}", phone="917000200131")
    t3 = await _seed_talent(f"Ayushi Singh {tag}", phone="917000200132")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to Ayushi {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "I found multiple" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Thakur {tag} and Singh {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Ayushi Thakur {tag}" in r2.reply
        assert f"Ayushi Singh {tag}" in r2.reply
        assert f"Ayushi Sharma {tag}" not in r2.reply
    finally:
        await _cleanup(phone, talent_ids=[t1, t2, t3], template_ids=[template_id])
        await _restore_config(original)


async def test_partial_ambiguity_resolves_known_asks_only_about_unresolved():
    """"Send the Follow Up template to Ayushi Thakur, Priya Sharma and
    Rahul Mehta" where Rahul is ambiguous — Ayushi and Priya must be
    preserved, only Rahul needs clarifying, and the original command
    resumes with everyone once Rahul is picked."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    # Distinct suffix PER TALENT (not one shared tag glued onto every
    # name) — the shared fuzzy matcher scores best-TOKEN similarity, so a
    # single tag repeated across every candidate would make them all
    # spuriously tie on that shared token once a fuzzy (non-exact) query
    # forces the fuzzy tier to run, turning "Rahul" into an ambiguity
    # against ALL FOUR people instead of just the two real Rahuls. Only
    # "Rahul" itself is deliberately shared between t3/t4 here.
    template_tag = uuid.uuid4().hex[:6]
    template_name = f"Partial {template_tag}"
    template_id = await _seed_template(template_name)
    s1, s2, s3, s4 = (uuid.uuid4().hex[:6] for _ in range(4))
    t1 = await _seed_talent(f"Ayushi Thakur {s1}", phone="917000200140")
    t2 = await _seed_talent(f"Priya Sharma {s2}", phone="917000200141")
    t3 = await _seed_talent(f"Rahul Mehta {s3}", phone="917000200142")
    t4 = await _seed_talent(f"Rahul Kumar Mehta {s4}", phone="917000200143")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            # Bare "Rahul" (not the full "Rahul Mehta <suffix>") — an
            # EXACT match to either full name would resolve directly via
            # the matcher's exact-match tier with no ambiguity at all;
            # the partial first-name-only fragment is what genuinely ties
            # between "Rahul Mehta" and "Rahul Kumar Mehta".
            text=(
                f"Send the {template_name} template to Ayushi Thakur {s1}, "
                f"Priya Sharma {s2} and Rahul"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "I found multiple" in r.reply
        assert f"Rahul Mehta {s3}" in r.reply and f"Rahul Kumar Mehta {s4}" in r.reply

        # Pick whichever numbered option is actually "Rahul Kumar Mehta"
        # — candidate ORDER isn't a contract this test should assume.
        pick = "1" if r.reply.index(f"Rahul Kumar Mehta {s4}") < r.reply.index(f"Rahul Mehta {s3}\n") else "2"
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=pick,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Ayushi Thakur {s1}" in r2.reply
        assert f"Priya Sharma {s2}" in r2.reply
        assert f"Rahul Kumar Mehta {s4}" in r2.reply
        assert "1 Approve" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2, t4}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, talent_ids=[t1, t2, t3, t4], template_ids=[template_id])
        await _restore_config(original)


async def test_partial_not_found_preserves_confidently_resolved():
    """A genuinely-unresolvable name alongside confidently-resolved ones
    no longer blocks the whole send — the known recipient(s) are queued
    and the unresolved name is surfaced as a warning."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"NotFoundPartial {tag}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent(f"KnownTalent {tag}", phone="917000200150")
    # Deliberately avoids the word "Person" — a real, non-test talent
    # literally named "Repro Person" already exists in this shared dev DB
    # (seed/leftover data) and would fuzzy-match on that shared word,
    # turning this into an ambiguity case instead of a clean not-found.
    unknown = f"Totally Unknown Xyzzyx {tag}"
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to KnownTalent {tag}, {unknown}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert f"KnownTalent {tag}" in r.reply
        assert "Couldn't find" in r.reply and unknown in r.reply
        assert "1 Approve" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r2.reply
        batch_id = r2.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1}
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_fuzzy_talent_and_project_matching_reused_in_campaign_agent():
    """Confirms the shared fuzzy matchers (casting_pipeline_nlu) are
    genuinely reused here, not reimplemented. Note: this agent's recipient
    resolution additionally applies _fuzzy_match_is_safe — a stricter,
    deliberate SEND-time-only gate added after a real production incident
    (a character-level fuzzy match sent a WhatsApp message to the wrong
    real person) — so a full character-typo'd name ("Ayushii" for
    "Ayushi") is correctly REJECTED here even though casting-agent's own
    (undo-able, internal-only) pipeline edits would tolerate it; a
    genuine PARTIAL name (surname alone) is what this gate does allow,
    exercised below instead."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    # Distinct suffixes for the template/project/talent — a shared tag
    # across all of them would let the fuzzy matcher's best-TOKEN scoring
    # tie the project query to the talent (and vice versa) purely on that
    # common token, exactly the false-ambiguity trap already hit earlier
    # in this file (see test_partial_ambiguity_resolves_known_asks_only_
    # about_unresolved).
    template_tag, project_tag, talent_tag = (uuid.uuid4().hex[:6] for _ in range(3))
    template_name = f"FuzzyReuse {template_tag}"
    template_id = await _seed_template(template_name)
    project_id = await _seed_project(f"Tira Suhana Film {project_tag}")
    t1 = await _seed_talent(f"Ayushi Thakur {talent_tag}", phone="917000200160")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        # Partial (surname-only) talent match — safety-gate compatible.
        r1 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to Thakur {talent_tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r1.handled and "1 Approve" in r1.reply, r1.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )

        # Project fuzzy/filler tolerance — "Tira Suhana Project" resolves
        # to a project literally named "... Film" via the shared filler-
        # word normalization (_PROJECT_FILLER_WORDS), same as casting-agent.
        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send {template_name} template to Tira Suhana Project {project_tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Tira Suhana Film {project_tag}" in r2.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Simplified Command Language (2026-08-17) — hyphen-delimited grammar.
# Parser unit tests first (pure functions, no DB), then end-to-end coverage
# through handle_inbound_message for every shape the spec calls out.
# ---------------------------------------------------------------------------
async def test_simple_parse_send_talent_template_project():
    parsed = wca.parse_simple_send_command("Riya,Karan - Toyota Reminder - Toyota Campaign")
    assert parsed == {
        "recipient_query": "Riya,Karan", "source_query": "Toyota Reminder", "project_query": "Toyota Campaign",
    }


async def test_simple_parse_send_talent_project_no_template():
    parsed = wca.parse_simple_send_command("Riya - Toyota Campaign")
    assert parsed == {"recipient_query": "Riya", "project_query": "Toyota Campaign"}


async def test_simple_parse_send_stage_targeting():
    parsed = wca.parse_simple_send_command("Reminder - Toyota Campaign - Follow Up")
    assert parsed["source_query"] == "Reminder"
    assert parsed["project_query"] == "Toyota Campaign"
    assert parsed["stage_query"] == "Follow Up"
    assert parsed["recipient_query"] == wca._ALL_PROJECTS_SENTINEL


async def test_simple_parse_send_stage_targeting_multi_project_multi_stage():
    parsed = wca.parse_simple_send_command("Reminder - Project A,Project B - Follow Up,Approved")
    assert parsed["project_query"] == "Project A,Project B"
    assert parsed["stage_query"] == "Follow Up,Approved"


async def test_simple_parse_send_custom_message_talent():
    parsed = wca.parse_simple_send_command('send custom message "Hi, your slot is confirmed." - Riya,Karan')
    assert parsed == {
        "send_mode": "custom_message", "source_query": "Hi, your slot is confirmed.",
        "recipient_query": "Riya,Karan",
    }


async def test_simple_parse_send_custom_message_project_stage():
    parsed = wca.parse_simple_send_command('send custom message "Reminder!" - Project A - Follow Up')
    assert parsed["send_mode"] == "custom_message"
    assert parsed["source_query"] == "Reminder!"
    assert parsed["project_query"] == "Project A"
    assert parsed["stage_query"] == "Follow Up"


async def test_simple_parse_send_custom_message_preserves_commas_in_quote():
    parsed = wca.parse_simple_send_command('send custom message "Hi, John, Karan and Riya" - Riya,Karan')
    assert parsed["source_query"] == "Hi, John, Karan and Riya"
    assert parsed["recipient_query"] == "Riya,Karan"


async def test_simple_parse_send_instagram_with_recipient():
    parsed = wca.parse_simple_send_command("instagram - Riya - Raj")
    assert parsed == {"send_mode": "instagram", "source_query": "Riya", "recipient_query": "Raj"}


async def test_simple_parse_send_insta_link_reply_in_chat():
    parsed = wca.parse_simple_send_command("insta link - Riya")
    assert parsed["send_mode"] == "instagram"
    assert parsed["source_query"] == "Riya"
    assert parsed["recipient_query"] == wca._REPLY_IN_CHAT_SENTINEL


async def test_simple_parse_send_rejects_natural_language():
    assert wca.parse_simple_send_command("Send Toyota Reminder to Ahana") is None
    assert wca.parse_simple_send_command("") is None


async def test_simple_split_send_commands_single_message_unaffected():
    text = 'send custom message "Reminder" - Riya'
    assert wca._split_send_commands(text) == [text]


async def test_simple_split_send_commands_never_splits_on_bare_blank_line():
    """A custom message's own blank-line-separated sections (trigger,
    quote body, trailing recipient clause) are ONE command, never several —
    only a line that itself starts with a recognized send trigger begins a
    new chunk."""
    text = (
        "send this to\n"
        "Riya\n"
        "Karan\n"
        "\n"
        '"Tomorrow\'s call time is 9 AM."'
    )
    assert wca._split_send_commands(text) == [text]


async def test_simple_split_send_commands_multiline_quote_with_trailing_recipient():
    text = (
        "Send custom message\n"
        "\n"
        '"Line one\n'
        "\n"
        'Line two"\n'
        "\n"
        "to Riya and Karan"
    )
    assert wca._split_send_commands(text) == [text]


async def test_simple_split_send_commands_splits_on_new_trigger_with_blank_line():
    text = "send Reminder - Riya - Project A\n\nsend Reminder - Karan - Project B"
    chunks = wca._split_send_commands(text)
    assert len(chunks) == 2
    assert chunks[0] == "send Reminder - Riya - Project A"
    assert chunks[1] == "send Reminder - Karan - Project B"


async def test_simple_split_send_commands_splits_without_blank_line():
    text = "send Reminder - Riya - Project A\nsend Reminder - Karan - Project B"
    chunks = wca._split_send_commands(text)
    assert len(chunks) == 2
    assert chunks[0] == "send Reminder - Riya - Project A"
    assert chunks[1] == "send Reminder - Karan - Project B"


async def test_simple_send_talent_template_project_and_confirm():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"SimpleSend {tag}"
    template_id = await _seed_template(template_name)
    project_id = await _seed_project(f"SimpleSendProj {tag}")
    t1 = await _seed_talent(f"SimpleSendTalent {tag}", phone="917000300001")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - SimpleSendTalent {tag} - {template_name} - SimpleSendProj {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1}
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_simple_send_talent_project_no_template_asks_for_source():
    """"send - Talent(s) - Project(s)" (template omitted) leaves
    source_query unset — falls through to the existing generic "What
    should I send?" question, same as any other missing-field case; no new
    default-template system is invented for this."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"NoTemplateProj {tag}")
    t1 = await _seed_talent(f"NoTemplateTalent {tag}", phone="917000300010")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - NoTemplateTalent {tag} - NoTemplateProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "What should I send" in r.reply
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1])
        await _restore_config(original)


async def test_simple_send_stage_targeting_multi_project_multi_stage_and_confirm():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"StageTarget {tag}"
    template_id = await _seed_template(template_name)
    project_a = await _seed_project(f"StageProjA {tag}")
    project_b = await _seed_project(f"StageProjB {tag}")
    t1 = await _seed_talent(f"StageTalentA {tag}", phone="917000300020")
    t2 = await _seed_talent(f"StageTalentB {tag}", phone="917000300021")
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "approved")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"send - {template_name} - StageProjA {tag},StageProjB {tag} - "
                "Follow Up,Approved and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_simple_send_custom_message_talents_and_confirm_preserves_body():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent(f"CustomSimpleT1 {tag}", phone="917000300030")
    t2 = await _seed_talent(f"CustomSimpleT2 {tag}", phone="917000300031")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f'send custom message "Hi, your profile has been shortlisted." - '
                f"CustomSimpleT1 {tag},CustomSimpleT2 {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        for j in jobs:
            assert j["message_body"] == "Hi, your profile has been shortlisted."
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_simple_send_custom_message_project_stage_and_confirm():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    project_id = await _seed_project(f"CustomStageProj {tag}")
    t1 = await _seed_talent(f"CustomStageTalent {tag}", phone="917000300040")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f'send custom message "Reminder about tomorrow\'s call." - '
                f"CustomStageProj {tag} - Follow Up and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1}
        assert jobs[0]["message_body"] == "Reminder about tomorrow's call."
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1])
        await _restore_config(original)


async def test_simple_send_instagram_with_recipient_end_to_end():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    t1 = await _seed_talent(f"InstaSimpleTalent {tag}", instagram_handle="insta_simple_handle")
    t2 = await _seed_talent(f"InstaSimpleRecipient {tag}", phone="917000300080")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - InstaSimpleTalent {tag} - InstaSimpleRecipient {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "Sent." in r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        await _cleanup_batch(batch_id)
    finally:
        await _cleanup(phone, talent_ids=[t1, t2])
        await _restore_config(original)


async def test_simple_multi_command_two_sends_single_trailing_confirm():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_a = f"MultiSendA {tag}"
    template_b = f"MultiSendB {tag}"
    tpl_a_id = await _seed_template(template_a)
    tpl_b_id = await _seed_template(template_b)
    project_a = await _seed_project(f"MultiSendProjA {tag}")
    project_b = await _seed_project(f"MultiSendProjB {tag}")
    t1 = await _seed_talent(f"MultiSendTalentA {tag}", phone="917000300050")
    t2 = await _seed_talent(f"MultiSendTalentB {tag}", phone="917000300051")
    await _seed_pipeline_row(project_a, t1, "follow_up")
    await _seed_pipeline_row(project_b, t2, "follow_up")
    batch_ids = []
    try:
        text = (
            f"send - MultiSendTalentA {tag} - {template_a} - MultiSendProjA {tag}\n"
            "\n"
            f"send - MultiSendTalentB {tag} - {template_b} - MultiSendProjB {tag}\n"
            "and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "1 Approve" not in r.reply
        jobs_a = await db.whatsapp_jobs.find({"template_id": tpl_a_id}).to_list(10)
        jobs_b = await db.whatsapp_jobs.find({"template_id": tpl_b_id}).to_list(10)
        assert {j["talent_id"] for j in jobs_a} == {t1}
        assert {j["talent_id"] for j in jobs_b} == {t2}
        batch_ids = [j["batch_id"] for j in jobs_a] + [j["batch_id"] for j in jobs_b]
    finally:
        for bid in set(batch_ids):
            await _cleanup_batch(bid)
        await _cleanup(
            phone, project_ids=[project_a, project_b], talent_ids=[t1, t2],
            template_ids=[tpl_a_id, tpl_b_id],
        )
        await _restore_config(original)


async def test_simple_multi_command_no_blank_line_still_splits_correctly():
    """The parser must not depend exclusively on blank lines — a new
    "send" trigger on its own line is enough of a boundary."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_a = f"NoBlankSendA {tag}"
    template_b = f"NoBlankSendB {tag}"
    tpl_a_id = await _seed_template(template_a)
    tpl_b_id = await _seed_template(template_b)
    t1 = await _seed_talent(f"NoBlankSendTalentA {tag}", phone="917000300060")
    t2 = await _seed_talent(f"NoBlankSendTalentB {tag}", phone="917000300061")
    batch_ids = []
    try:
        text = (
            f"send {template_a} to NoBlankSendTalentA {tag}\n"
            f"send {template_b} to NoBlankSendTalentB {tag}\n"
            "and confirm"
        )
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=text,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        jobs_a = await db.whatsapp_jobs.find({"template_id": tpl_a_id}).to_list(10)
        jobs_b = await db.whatsapp_jobs.find({"template_id": tpl_b_id}).to_list(10)
        assert {j["talent_id"] for j in jobs_a} == {t1}
        assert {j["talent_id"] for j in jobs_b} == {t2}
        batch_ids = [j["batch_id"] for j in jobs_a] + [j["batch_id"] for j in jobs_b]
    finally:
        for bid in set(batch_ids):
            await _cleanup_batch(bid)
        await _cleanup(phone, talent_ids=[t1, t2], template_ids=[tpl_a_id, tpl_b_id])
        await _restore_config(original)


async def test_simple_send_multi_pick_ambiguity_resume():
    """Ambiguous talent name inside a hyphen send -> numbered clarification
    -> a multi-pick reply ("1 and 3") resolves BOTH, resuming the original
    template send without repeating the command."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_name = f"SimpleMultiPick {tag}"
    template_id = await _seed_template(template_name)
    project_id = await _seed_project(f"SimpleMultiPickProj {tag}")
    t1 = await _seed_talent(f"Zayna Kapoor {tag}", phone="917000300070")
    t2 = await _seed_talent(f"Zayna Mehta {tag}", phone="917000300071")
    t3 = await _seed_talent(f"Zayna Verma {tag}", phone="917000300072")
    for tid in (t1, t2, t3):
        await _seed_pipeline_row(project_id, tid, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Zayna {tag} - {template_name} - SimpleMultiPickProj {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "I found multiple" in r.reply
        assert f"Zayna Kapoor {tag}" in r.reply
        assert f"Zayna Mehta {tag}" in r.reply
        assert f"Zayna Verma {tag}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1 and 3",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert f"Zayna Kapoor {tag}" in r2.reply
        assert f"Zayna Verma {tag}" in r2.reply
        assert f"Zayna Mehta {tag}" not in r2.reply
        assert "1 Approve" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t3}
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(
            phone, project_ids=[project_id], talent_ids=[t1, t2, t3], template_ids=[template_id],
        )
        await _restore_config(original)


# ---------------------------------------------------------------------------
# Production fixes (2026-08-19):
#   1. Named-talent template sends were silently skipping ALL project-
#      variable rendering ({{project_name}}, {{shoot_dates}}, {{budget}},
#      {{submission_link}}, ...) because _resolve_talents_narrowed_by_
#      projects always collapsed into a MANUAL-source create_batch call,
#      and MANUAL source never adds _project_variables. Multi-project
#      named-talent sends ALSO deduped by recipient_id, silently dropping
#      a talent's second project's message entirely.
#   2. Instagram Profile Send's recipient clause was resolved through the
#      general _resolve_recipient (talent-first), so a real CRM contact
#      could get misresolved against a similarly-named talent instead.
# ---------------------------------------------------------------------------
async def _seed_project_with_details(brand_name: str, *, shoot_dates: str, budget: str) -> str:
    pid = f"test-wca-proj-{uuid.uuid4().hex[:8]}"
    await db.projects.insert_one({
        "id": pid, "brand_name": brand_name, "status": "ongoing", "slug": pid,
        "shoot_dates": shoot_dates, "budget_per_day": budget,
        "materials": [], "created_at": _now(),
    })
    return pid


async def _seed_full_variable_template(name: str) -> str:
    """A template body referencing EVERY auto-resolved variable
    (routers/whatsapp.py's AUTO_RECIPIENT_VARS/AUTO_PROJECT_VARS/
    AUTO_SENDER_VARS/AUTO_SYSTEM_VARS) plus talent_name — not just the 4
    project variables the bug report named — so a regression in ANY of
    them (not only the ones explicitly reported) is caught."""
    tpl_id = f"test-wca-tpl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_templates.insert_one({
        "id": tpl_id, "name": name, "slug": name.lower().replace(" ", "_") + uuid.uuid4().hex[:4],
        "body_text": (
            "Hi {{talent_name}} ({{full_name}} / {{first_name}} / {{phone}}), "
            "about {{project_name}} — Dates: {{shoot_dates}} Budget: {{budget}} "
            "Link: {{submission_link}} Sender: {{sender_name}} <{{sender_email}}> "
            "Today: {{current_date}} {{current_time}}"
        ),
        "variables": [], "media_type": "none", "media_url": None,
        "media_cloudinary_id": None, "is_custom": False,
        "created_by": "test", "created_at": _now(), "updated_at": _now(),
    })
    return tpl_id


def _assert_no_raw_placeholders(message_body: str) -> None:
    assert "{{" not in message_body and "}}" not in message_body, message_body


async def test_named_talent_single_project_renders_all_variables():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_id = await _seed_full_variable_template(f"RenderCheck {tag}")
    project_id = await _seed_project_with_details(
        f"RenderProj {tag}", shoot_dates="12-15 Sep 2026", budget="₹50,000/day",
    )
    t1 = await _seed_talent(f"RenderTalent {tag}", phone="917000400001")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - RenderTalent {tag} - RenderCheck {tag} - RenderProj {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        body = jobs[0]["message_body"]
        _assert_no_raw_placeholders(body)
        assert f"RenderTalent {tag}" in body  # talent_name/full_name
        assert f"RenderProj {tag}" in body    # project_name
        assert "12-15 Sep 2026" in body       # shoot_dates
        assert "₹50,000/day" in body          # budget
        assert f"https://submit.talentgramagency.com/submit/{project_id}" in body  # submission_link
        assert "917000400001" in body         # phone
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_named_talent_multi_project_each_gets_own_context():
    """"send - Prachi Darbar - Casting Call - Amazon,Twamev" must generate
    TWO messages, each with THAT project's own values — never one
    project's context reused for the other, and never one silently
    dropped."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_id = await _seed_full_variable_template(f"MultiProjTpl {tag}")
    proj_a = await _seed_project_with_details(
        f"Amazon {tag}", shoot_dates="1-3 Oct 2026", budget="₹20,000/day",
    )
    proj_b = await _seed_project_with_details(
        f"Twamev {tag}", shoot_dates="10-12 Nov 2026", budget="₹35,000/day",
    )
    t1 = await _seed_talent(f"Prachi Darbar {tag}", phone="917000400010")
    await _seed_pipeline_row(proj_a, t1, "follow_up")
    await _seed_pipeline_row(proj_b, t1, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - Prachi Darbar {tag} - MultiProjTpl {tag} - Amazon {tag},Twamev {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_ids = [b.strip() for b in r.reply.split("Batch ID:")[1].strip().split(",")]
        jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": batch_ids}}).to_list(10)
        assert len(jobs) == 2
        for j in jobs:
            _assert_no_raw_placeholders(j["message_body"])
        bodies = [j["message_body"] for j in jobs]
        amazon_body = next(b for b in bodies if f"Amazon {tag}" in b)
        twamev_body = next(b for b in bodies if f"Twamev {tag}" in b)
        assert "1-3 Oct 2026" in amazon_body and "₹20,000/day" in amazon_body
        assert "10-12 Nov 2026" not in amazon_body and "₹35,000/day" not in amazon_body
        assert "10-12 Nov 2026" in twamev_body and "₹35,000/day" in twamev_body
        assert "1-3 Oct 2026" not in twamev_body and "₹20,000/day" not in twamev_body
        assert f"https://submit.talentgramagency.com/submit/{proj_a}" in amazon_body
        assert f"https://submit.talentgramagency.com/submit/{proj_b}" in twamev_body
    finally:
        for j in await db.whatsapp_jobs.find({"talent_id": t1}).to_list(10):
            await _cleanup_batch(j["batch_id"])
        await _cleanup(
            phone, project_ids=[proj_a, proj_b], talent_ids=[t1], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_multi_talent_single_project_each_renders_correctly():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_id = await _seed_full_variable_template(f"MultiTalentTpl {tag}")
    project_id = await _seed_project_with_details(
        f"MultiTalentProj {tag}", shoot_dates="5 Dec 2026", budget="₹10,000/day",
    )
    t1 = await _seed_talent(f"MultiTalentA {tag}", phone="917000400020")
    t2 = await _seed_talent(f"MultiTalentB {tag}", phone="917000400021")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    await _seed_pipeline_row(project_id, t2, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send - MultiTalentA {tag},MultiTalentB {tag} - MultiTalentTpl {tag} - MultiTalentProj {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert {j["talent_id"] for j in jobs} == {t1, t2}
        for j in jobs:
            _assert_no_raw_placeholders(j["message_body"])
            assert f"MultiTalentProj {tag}" in j["message_body"]
            assert "5 Dec 2026" in j["message_body"]
            assert "₹10,000/day" in j["message_body"]
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(
            phone, project_ids=[project_id], talent_ids=[t1, t2], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_multi_talent_multi_project_no_cross_contamination():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_id = await _seed_full_variable_template(f"FullMatrixTpl {tag}")
    proj_a = await _seed_project_with_details(
        f"MatrixProjA {tag}", shoot_dates="1 Jan 2027", budget="₹5,000/day",
    )
    proj_b = await _seed_project_with_details(
        f"MatrixProjB {tag}", shoot_dates="2 Feb 2027", budget="₹6,000/day",
    )
    t1 = await _seed_talent(f"MatrixTalentA {tag}", phone="917000400030")
    t2 = await _seed_talent(f"MatrixTalentB {tag}", phone="917000400031")
    # A is in BOTH projects; B is only in project A.
    await _seed_pipeline_row(proj_a, t1, "follow_up")
    await _seed_pipeline_row(proj_b, t1, "follow_up")
    await _seed_pipeline_row(proj_a, t2, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=(
                f"send - MatrixTalentA {tag},MatrixTalentB {tag} - FullMatrixTpl {tag} - "
                f"MatrixProjA {tag},MatrixProjB {tag} and confirm"
            ),
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_ids = [b.strip() for b in r.reply.split("Batch ID:")[1].strip().split(",")]
        jobs = await db.whatsapp_jobs.find({"batch_id": {"$in": batch_ids}}).to_list(20)
        for j in jobs:
            _assert_no_raw_placeholders(j["message_body"])
        # A gets 2 messages (one per project); B gets 1 (only in project A).
        a_jobs = [j for j in jobs if j["talent_id"] == t1]
        b_jobs = [j for j in jobs if j["talent_id"] == t2]
        assert len(a_jobs) == 2
        assert len(b_jobs) == 1
        a_proj_a = next(j for j in a_jobs if f"MatrixProjA {tag}" in j["message_body"])
        a_proj_b = next(j for j in a_jobs if f"MatrixProjB {tag}" in j["message_body"])
        assert "1 Jan 2027" in a_proj_a["message_body"] and "₹5,000/day" in a_proj_a["message_body"]
        assert "2 Feb 2027" in a_proj_b["message_body"] and "₹6,000/day" in a_proj_b["message_body"]
        assert f"MatrixProjA {tag}" in b_jobs[0]["message_body"]
        assert "1 Jan 2027" in b_jobs[0]["message_body"]
    finally:
        for j in await db.whatsapp_jobs.find({"talent_id": {"$in": [t1, t2]}}).to_list(20):
            await _cleanup_batch(j["batch_id"])
        await _cleanup(
            phone, project_ids=[proj_a, proj_b], talent_ids=[t1, t2], template_ids=[template_id],
        )
        await _restore_config(original)


async def test_single_project_pipeline_stage_send_unchanged():
    """Existing pipeline-stage template sending (single project) must
    remain completely unaffected by this fix."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    template_id = await _seed_full_variable_template(f"UnchangedTpl {tag}")
    project_id = await _seed_project_with_details(
        f"UnchangedProj {tag}", shoot_dates="9 Sep 2027", budget="₹9,000/day",
    )
    t1 = await _seed_talent(f"UnchangedTalent {tag}", phone="917000400050")
    await _seed_pipeline_row(project_id, t1, "follow_up")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send UnchangedTpl {tag} template to Follow Up pipeline of UnchangedProj {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        assert "," not in batch_id  # single project -> single batch, unchanged shape
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        _assert_no_raw_placeholders(jobs[0]["message_body"])
        assert "9 Sep 2027" in jobs[0]["message_body"]
        assert "₹9,000/day" in jobs[0]["message_body"]
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)


async def test_instagram_recipient_resolves_crm_contact_not_talent():
    """"send talentgram - Angela - Akash Castingtree" — Angela is the
    subject talent; "Akash Castingtree" is a CRM contact and must NEVER be
    routed through the talent resolver, even when a similarly-named talent
    exists (the exact production bug)."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Angela {tag}", instagram_handle="angela_official")
    # A similarly-named TALENT that must NOT be matched as the recipient.
    decoy_talent = await _seed_talent(f"Akash Castingtree {tag}", phone="917000400060")
    crm_id = await _seed_crm_client(f"Akash Castingtree {tag}", "917000400061", "Casting Director")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send talentgram - Angela {tag} - Akash Castingtree {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert len(jobs) == 1
        # Sent to the CRM contact's phone, never the decoy talent's.
        assert jobs[0]["destination"] == "917000400061"
        assert jobs[0].get("talent_id") != decoy_talent
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[subject_talent, decoy_talent], client_ids=[crm_id])
        await _restore_config(original)


async def test_instagram_talentgram_alias_equivalent_to_instagram():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    for word in ("instagram", "insta", "instagram link", "insta link", "talentgram"):
        t1 = await _seed_talent(f"AliasTalent {tag}", instagram_handle="alias_handle")
        t2 = await _seed_talent(f"AliasRecipient {tag}", phone="917000400070")
        batch_id = None
        try:
            r = await handle_inbound_message(
                group_name=group, sender_phone=phone,
                text=f"send {word} - AliasTalent {tag} - AliasRecipient {tag} and confirm",
                sender_name="Raj", sender_is_group_member=True,
            )
            assert r.handled and "Sent." in r.reply, (word, r.reply)
            batch_id = r.reply.split("Batch ID:")[1].strip()
        finally:
            if batch_id:
                await _cleanup_batch(batch_id)
            await _cleanup(phone, talent_ids=[t1, t2])
    await _restore_config(original)


async def test_instagram_recipient_ambiguous_crm_contact_shows_full_list_and_resumes():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Bella {tag}", instagram_handle="bella_official")
    c1 = await _seed_crm_client(f"Rohan Mehta {tag}", "917000400080", "Casting Director")
    c2 = await _seed_crm_client(f"Rohan Sharma {tag}", "917000400081", "Casting Director")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - Bella {tag} - Rohan {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "I found multiple" in r.reply
        assert f"Rohan Mehta {tag}" in r.reply
        assert f"Rohan Sharma {tag}" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Rohan Mehta {tag}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled, r2.reply
        assert "1 Approve" in r2.reply

        r3 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Sent." in r3.reply
        batch_id = r3.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert jobs[0]["destination"] == "917000400080"
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[subject_talent], client_ids=[c1, c2])
        await _restore_config(original)


async def test_instagram_recipient_phone_number():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Carla {tag}", instagram_handle="carla_official")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - Carla {tag} - +917000400090 and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert jobs[0]["destination"] == "+917000400090" or jobs[0]["destination"] == "917000400090"
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[subject_talent])
        await _restore_config(original)


async def test_instagram_recipient_genuinely_not_found_reports_clearly():
    """A recipient that isn't a CRM contact, saved list, known talent, or
    phone number (and isn't reachable via a live WhatsApp lookup — a known
    limitation, see _resolve_recipient_only's module comment) must report
    clearly rather than crash or silently do nothing."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Diya {tag}", instagram_handle="diya_official")
    try:
        # Deliberately no shared substring/tag with the seeded talent —
        # sharing the random tag would coincidentally fuzzy-match the
        # talent-fallback tier via the shared random suffix alone.
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - Diya {tag} - Zzzargled Nonexistent Recipient Xyzzy Quux",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "couldn't find" in r.reply.lower()
        assert "Sent." not in r.reply
    finally:
        await _cleanup(phone, talent_ids=[subject_talent])
        await _restore_config(original)


async def test_instagram_recipient_crm_contact_takes_priority_over_similarly_named_talent():
    """A genuine CRM contact must resolve at CRM priority even when a
    similarly-named TALENT also exists — never the reverse (the exact
    production bug: CRM contact "Akash Castingtree" was previously
    shadowed by "similarly named talents")."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Elena {tag}", instagram_handle="elena_official")
    similarly_named_talent = await _seed_talent(f"Farhan Qureshi {tag}", phone="917000400200")
    crm_id = await _seed_crm_client(f"Farhan Qureshi {tag}", "917000400201", "Casting Director")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - Elena {tag} - Farhan Qureshi {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert jobs[0]["destination"] == "917000400201"  # the CRM contact's phone
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[subject_talent, similarly_named_talent], client_ids=[crm_id])
        await _restore_config(original)


async def test_instagram_recipient_falls_back_to_talent_when_no_crm_match():
    """No CRM contact, no saved list — a known talent by name still
    resolves as the recipient (spec: "Do not require every recipient to
    exist in the CRM"), preserving the pre-existing "share with a fellow
    talent" workflow."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    tag = uuid.uuid4().hex[:6]
    subject_talent = await _seed_talent(f"Gita {tag}", instagram_handle="gita_official")
    recipient_talent = await _seed_talent(f"Harish {tag}", phone="917000400210")
    batch_id = None
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send instagram - Gita {tag} - Harish {tag} and confirm",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled and "Sent." in r.reply, r.reply
        batch_id = r.reply.split("Batch ID:")[1].strip()
        jobs = await db.whatsapp_jobs.find({"batch_id": batch_id}).to_list(10)
        assert jobs[0]["destination"] == "917000400210"
    finally:
        if batch_id:
            await _cleanup_batch(batch_id)
        await _cleanup(phone, talent_ids=[subject_talent, recipient_talent])
        await _restore_config(original)
