"""WhatsApp Campaign Orchestration Agent (2026-08-05) — tests for the third
registered agent, whatsapp-campaign-agent. Everything DB-facing it touches
(whatsapp_batches, whatsapp_jobs, whatsapp_templates, projects,
casting_pipeline, talents) is the SAME existing WhatsApp Engine the web app
uses — these tests exist to prove the orchestration layer wires into it
correctly (compile preview -> confirmation -> launch), not to re-test the
engine itself (routers/whatsapp.py has its own test coverage).
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
from agents.dispatcher import handle_inbound_message
from agents.modules import whatsapp_campaign_agent as wca

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


async def _seed_talent(name: str, phone: str) -> str:
    tid = f"test-wca-tal-{uuid.uuid4().hex[:8]}"
    await db.talents.insert_one({"id": tid, "name": name, "phone": phone, "tags": [], "notes": ""})
    return tid


async def _seed_pipeline_row(project_id: str, talent_id: str, stage: str) -> None:
    await db.casting_pipeline.insert_one({
        "id": str(uuid.uuid4()), "project_id": project_id, "talent_id": talent_id,
        "stage": stage, "created_at": _now(), "updated_at": _now(),
    })


async def _seed_template(name: str) -> str:
    tpl_id = f"test-wca-tpl-{uuid.uuid4().hex[:8]}"
    await db.whatsapp_templates.insert_one({
        "id": tpl_id, "name": name, "slug": name.lower().replace(" ", "_"),
        "body_text": "Hi {{talent_name}}, about {{project_name}} — reply to confirm.",
        "variables": [], "media_type": "none", "media_url": None,
        "media_cloudinary_id": None, "is_custom": False,
        "created_by": "test", "created_at": _now(), "updated_at": _now(),
    })
    return tpl_id


async def _cleanup(phone: str, project_ids=(), talent_ids=(), template_ids=()) -> None:
    await db.projects.delete_many({"id": {"$in": list(project_ids)}})
    await db.talents.delete_many({"id": {"$in": list(talent_ids)}})
    await db.casting_pipeline.delete_many({"project_id": {"$in": list(project_ids)}})
    await db.whatsapp_templates.delete_many({"id": {"$in": list(template_ids)}})
    await db.whatsapp_conversations.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_sessions.delete_many({"agent_id": AGENT_ID, "phone": phone})
    await db.whatsapp_agent_audit_log.delete_many({"agent_id": AGENT_ID, "sender_phone": phone})
    batches = await db.whatsapp_batches.find({"project_id": {"$in": list(project_ids)}}).to_list(100)
    for b in batches:
        await db.whatsapp_batches.delete_one({"id": b["id"]})
        await db.whatsapp_jobs.delete_many({"batch_id": b["id"]})


async def _seed_admin_id() -> str:
    admin = await db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 0, "id": 1})
    assert admin is not None, "bootstrap-seeded admin account missing from local dev DB"
    return admin["id"]


# ---------------------------------------------------------------------------
async def test_one_line_command_auto_resolves_and_confirmation_card_correct():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Campaign Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Talent One", "917000000001")
    t2 = await _seed_talent("Talent Two", "917000000002")
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
        assert f"Campaign\n{template_name}" in r.reply
        assert f"Project\n{label}" in r.reply
        assert "Pipeline\nAll stages" in r.reply
        assert "Recipients: 2" in r.reply
        assert "1 → Send" in r.reply

        # A dry-run batch was written (the actual compile preview), never
        # picked up by the worker.
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


async def test_missing_template_asks_then_completes():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Missing Tpl Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Only Talent", "917000000003")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=f"Send campaign to {label}",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == "Which template should I use?"

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text=template_name,
            sender_name="Raj", sender_is_group_member=True,
        )
        assert f"Campaign\n{template_name}" in r2.reply
        assert "Recipients: 1" in r2.reply

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
    t1 = await _seed_talent("Ask Talent", "917000000004")
    t2 = await _seed_talent("Approved Talent", "917000000005")
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
        assert "Pipeline\nApproved" in r.reply
        assert "Recipients: 1" in r.reply

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
    t1 = await _seed_talent("Ask Talent", "917000000006")
    t2 = await _seed_talent("Approved Talent", "917000000007")
    t3 = await _seed_talent("Hold Talent", "917000000008")
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
        assert "Pipeline\nAll stages" in r.reply
        assert "Recipients: 3" in r.reply

        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=talent_ids, template_ids=[template_id])
        await _restore_config(original)


async def test_approve_creates_live_batch_and_jobs():
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Approve Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Send Talent", "917000000009")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")

        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert "Recipients: 1" in r.reply

        r2 = await handle_inbound_message(
            group_name=group, sender_phone=phone, text="1",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r2.handled
        assert "Campaign launched." in r2.reply
        assert f"Project\n{label}" in r2.reply
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

        # The dispatcher's own generic audit trail (agent-turn level, not
        # the campaign engine's own whatsapp_audit_log) still traces the
        # real WhatsApp sender back to this batch — the "attribution
        # without new infra" mechanism this agent relies on.
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
    t1 = await _seed_talent("Cancel Talent", "917000000010")
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
        # The dry-run preview batch is expected/harmless — same as clicking
        # "Preview" in the web app.
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
        # This phone is authorized for the campaign group above, but the
        # message is sent to a DIFFERENT group entirely — resolve_agent_
        # for_group must not route it to this agent (or any agent that
        # doesn't own that group).
        r = await handle_inbound_message(
            group_name="Talentgram Casting Pipeline", sender_phone=phone,
            text="Send campaign to Whatever using Whatever template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert not r.handled
    finally:
        await _restore_config(original)


async def test_unauthorized_phone_in_campaign_group_gets_friendly_reply():
    """(2026-08-06) The allowlist itself stays fail-closed — an
    unauthorized sender's message never reaches the campaign engine, never
    creates a dry-run batch, never resolves a project/template. Only the
    OUTCOME changed: a clear message instead of dead silence."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    authorized_phone = _phone()
    unauthorized_phone = _phone()
    original = await _use_test_config(group, authorized_phone)
    try:
        r = await handle_inbound_message(
            group_name=group, sender_phone=unauthorized_phone,
            text="Send campaign to Whatever using Whatever template",
            sender_name="Stranger", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply == wca.UNAUTHORIZED_SENDER_MESSAGE
        assert "not authorized" in r.reply.lower()

        # No dry-run batch was created for the rejected sender — the
        # allowlist check happens BEFORE the campaign engine is ever
        # touched, same as before this change.
        stray = await db.whatsapp_batches.count_documents({"source_label": {"$regex": "Whatever"}})
        assert stray == 0
    finally:
        await _restore_config(original)


async def test_authorized_phone_still_works_normally():
    """The fix is additive to the REJECTION path only — an authorized
    sender's flow is completely unaffected."""
    group = f"Test WA Campaign {uuid.uuid4().hex[:6]}"
    phone = _phone()
    original = await _use_test_config(group, phone)
    label = f"Still Works Brand {uuid.uuid4().hex[:6]}"
    project_id = await _seed_project(label)
    template_name = f"Promo {uuid.uuid4().hex[:6]}"
    template_id = await _seed_template(template_name)
    t1 = await _seed_talent("Still Works Talent", "917000000011")
    try:
        await _seed_pipeline_row(project_id, t1, "ask_to_test")
        r = await handle_inbound_message(
            group_name=group, sender_phone=phone,
            text=f"Send campaign to {label} using {template_name} template",
            sender_name="Raj", sender_is_group_member=True,
        )
        assert r.handled
        assert r.reply != wca.UNAUTHORIZED_SENDER_MESSAGE
        assert "Recipients: 1" in r.reply
        await handle_inbound_message(
            group_name=group, sender_phone=phone, text="3",
            sender_name="Raj", sender_is_group_member=True,
        )
    finally:
        await _cleanup(phone, project_ids=[project_id], talent_ids=[t1], template_ids=[template_id])
        await _restore_config(original)
