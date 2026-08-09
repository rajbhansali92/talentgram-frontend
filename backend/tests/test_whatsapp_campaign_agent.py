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

from core import db, _now, ADMIN_EMAIL
from agents import modules as agent_modules
from agents import registry
from agents import disambiguation
from agents.dispatcher import handle_inbound_message
from agents.modules import whatsapp_campaign_agent as wca
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


async def _seed_talent(name: str, phone: str = "", group_name: str = "") -> str:
    tid = f"test-wca-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({
        "id": tid, "name": name, "phone": phone or None,
        "whatsapp_group_name": group_name, "tags": [], "notes": "",
    })
    return tid


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
        assert f"✓ {talent_name} → {group_display}" in r.reply
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
        assert f"✓ {talent_name} → 917000000013" in r.reply
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
        assert f"✓ {talent_name}" in r.reply, r.reply

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
        assert f"✓ {name_sharma}" in r2.reply

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
