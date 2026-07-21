"""Talentgram CRM Agent — the first registered WhatsApp agent.

Scoped to exactly one backend module: Marketing (backend/routers/marketing.py,
db.clients). This file is the only place in the whole agent platform that
knows what "name / phone / role" mean, what a supported role is, or how a
CRM contact gets written to the database. Every future agent (Projects,
Casting, Storage, Analytics, ...) follows this exact shape: field
validators + an executor + one register() call, registered from
agents/modules/__init__.py.
"""
from __future__ import annotations

import re

from core import db
from routers.marketing import insert_client_doc
from routers.whatsapp import _normalize_phone

from agents.models import AgentDefinition, ExecContext, ExecResult, FieldSpec, IntentDefinition, ValidationResult
from agents.registry import register_agent

# value -> canonical display label, mirrors frontend/src/pages-components/
# MarketingHub.jsx's CONTACT_TYPES exactly (kept in sync manually — small,
# stable list; promote to a shared backend constant if it starts drifting).
SUPPORTED_ROLES = {
    "brand manager": "Brand Manager",
    "marketing manager": "Marketing Manager",
    "influencer marketing manager": "Influencer Marketing Manager",
    "influencer marketing": "Influencer Marketing Manager",
    "creative director": "Creative Director",
    "agency producer": "Agency Producer",
    "casting director": "Casting Director",
    "casting assistant": "Casting Assistant",
    "casting company": "Casting Company",
    "producer": "Producer",
    "executive producer": "Executive Producer",
    "production house": "Production House",
    "line producer": "Line Producer",
    "talent agency": "Talent Agency",
    "modeling agency": "Modeling Agency",
    "casting agency": "Casting Agency",
}

# Label -> the snake_case slug MarketingHub.jsx's CONTACT_TYPES actually
# stores/filters/counts by (its <select> value, not its display label).
# SUPPORTED_ROLES' values stay as human-readable labels so the WhatsApp
# confirmation message keeps showing "Role: Casting Director" — this
# separate mapping is applied only at the point of writing contact_type to
# the database, so a WhatsApp-created contact shows up correctly in the
# Marketing UI's Contact Type filter/dropdown instead of silently not
# matching any option (found live 2026-07-21: the badge on the contact
# card fell back to displaying the raw stored label, masking that the
# dropdown/filter/count logic never matched it).
ROLE_LABEL_TO_SLUG = {
    "Brand Manager": "brand_manager",
    "Marketing Manager": "marketing_manager",
    "Influencer Marketing Manager": "influencer_marketing",
    "Creative Director": "creative_director",
    "Agency Producer": "agency_producer",
    "Casting Director": "casting_director",
    "Casting Assistant": "casting_assistant",
    "Casting Company": "casting_company",
    "Producer": "producer",
    "Executive Producer": "executive_producer",
    "Production House": "production_house",
    "Line Producer": "line_producer",
    "Talent Agency": "talent_agency",
    "Modeling Agency": "modeling_agency",
    "Casting Agency": "casting_agency",
}


def _validate_name(raw: str) -> ValidationResult:
    name = " ".join((raw or "").strip().split())
    if len(name) < 2:
        return ValidationResult(ok=False, error="That doesn't look like a valid name. Please send the contact's full name.")
    if len(name) > 200:
        return ValidationResult(ok=False, error="That name is too long (max 200 characters). Please shorten it.")
    if not re.search(r"[A-Za-z]", name):
        return ValidationResult(ok=False, error="That doesn't look like a valid name. Please send the contact's full name.")
    return ValidationResult(ok=True, value=name)


def _validate_phone(raw: str) -> ValidationResult:
    norm = _normalize_phone(raw)
    if not norm:
        return ValidationResult(
            ok=False,
            error="That doesn't look like a valid phone number. Please send 7-15 digits, e.g. 9876543210.",
        )
    return ValidationResult(ok=True, value=norm)


def _validate_role(raw: str) -> ValidationResult:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower()).replace("_", " ")
    if key in SUPPORTED_ROLES:
        return ValidationResult(ok=True, value=SUPPORTED_ROLES[key])
    supported = ", ".join(sorted(set(SUPPORTED_ROLES.values())))
    return ValidationResult(
        ok=False,
        error=f'"{raw.strip()}" isn\'t a supported role.\nSupported roles: {supported}',
    )


async def _create_contact_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    name = collected.get("name", "")
    phone = collected.get("phone", "")
    role = collected.get("role", "")

    existing_phone = await db.clients.find_one({"phone_number": phone, "deleted": {"$ne": True}})
    if existing_phone:
        return ExecResult(
            ok=False,
            error="duplicate_phone",
            message=(
                f"A contact with phone {phone} already exists: {existing_phone.get('name')}.\n"
                "Nothing saved."
            ),
        )

    existing_name = await db.clients.find_one(
        {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "deleted": {"$ne": True}}
    )
    if existing_name:
        return ExecResult(
            ok=False,
            error="duplicate_name",
            message=(
                f'A contact named "{name}" already exists '
                f"(phone {existing_name.get('phone_number') or '—'}).\n"
                "Nothing saved. Reply with a different name if this is a different person."
            ),
        )

    doc = await insert_client_doc(
        name=name,
        phone_number=phone,
        contact_type=ROLE_LABEL_TO_SLUG.get(role, role),
        source=f"whatsapp_agent:{ctx.agent_id}",
    )
    return ExecResult(ok=True, message=f"Saved successfully\nCRM ID: {doc['id']}", data=doc)


CREATE_CONTACT_INTENT = IntentDefinition(
    intent_id="crm.create_contact",
    triggers=["save", "add", "new contact"],
    fields=[
        FieldSpec(key="name", label="Name", question="What's the name?", validate=_validate_name),
        FieldSpec(key="phone", label="Phone", question="What's the phone?", validate=_validate_phone,
                   aliases=["phone number", "mobile", "number"]),
        FieldSpec(key="role", label="Role", question="What's the role?", validate=_validate_role,
                   aliases=["contact type", "designation"]),
    ],
    executor=_create_contact_executor,
)

CRM_AGENT = AgentDefinition(
    agent_id="crm-agent",
    name="Talentgram CRM",
    module="marketing",
    intents=[CREATE_CONTACT_INTENT],
)


def register() -> None:
    register_agent(CRM_AGENT)
