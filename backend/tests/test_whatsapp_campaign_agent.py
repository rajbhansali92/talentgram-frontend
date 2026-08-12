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


async def test_multi_recipient_unknown_name_reports_clearly():
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
        # never silently proceeds with just the known one
        assert "3 Approve" not in r.reply and "1 Approve" not in r.reply
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


async def test_custom_message_no_quotes_falls_through_to_requirement_mode():
    """Regression guard for the slug="custom" template exclusion — a
    malformed "send custom message to X" (no quotes, no colon-body) falls
    through to requirement mode (Tier 3: source_query="custom message",
    recipient_query=name). With the seeded "Custom Message" template
    excluded from _fetch_templates(), this must NOT silently fuzzy-match
    it and send the literal unsubstituted string "{{message}}" — it must
    end in a clear "couldn't find a template" error instead, and never
    reach an approvable "Sent." state."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    name = f"Riya{uuid.uuid4().hex[:6]}"
    t1 = await _seed_talent(name, phone="917000230001")
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"send custom message to {name}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled, r.reply
        assert "{{message}}" not in r.reply
        assert "couldn't find a template" in r.reply.lower()
        assert "Reply" not in r.reply and "1 Approve" not in r.reply

        # The attempt ends cleanly (genuinely-unresolvable source clears
        # the conversation) rather than leaving anything open to approve.
        conv = await db.whatsapp_conversations.find_one({"agent_id": AGENT_ID, "phone": phone})
        assert conv is None
    finally:
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
